"""Telegram channel binding helpers (chat <-> workspace/session mapping)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from kragen.models.core import Channel, Session, TelegramBinding

DedupStatus = Literal["processing", "completed", "failed"]


def is_stale_telegram_update(*, last_update_id: int | None, incoming_update_id: int) -> bool:
    """Return True if an incoming Telegram update id should be skipped."""
    return last_update_id is not None and incoming_update_id <= last_update_id


async def get_binding_by_chat_id(db: AsyncSession, *, chat_id: int) -> TelegramBinding | None:
    """Fetch Telegram binding by chat id."""
    result = await db.execute(
        select(TelegramBinding).where(TelegramBinding.chat_id == chat_id)
    )
    return result.scalar_one_or_none()


async def resolve_or_create_binding(
    db: AsyncSession,
    *,
    chat_id: int,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> TelegramBinding:
    """Resolve an existing Telegram binding or create one with a fresh session."""
    binding = await get_binding_by_chat_id(db, chat_id=chat_id)
    now = datetime.now(UTC)
    if binding is None:
        channel = Channel(
            workspace_id=workspace_id,
            user_id=user_id,
            type="telegram",
            external_ref=str(chat_id),
            metadata_={},
        )
        db.add(channel)
        await db.flush()
        session = Session(
            workspace_id=workspace_id,
            user_id=user_id,
            channel_id=channel.id,
            title=f"Telegram chat {chat_id}",
        )
        db.add(session)
        await db.flush()
        binding = TelegramBinding(
            chat_id=chat_id,
            workspace_id=workspace_id,
            session_id=session.id,
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            updated_at=now,
        )
        db.add(binding)
        await db.flush()
        return binding

    # Existing binding may point to a session created with another user/workspace
    # (for example after operator changed Telegram auth_user_id/default_workspace_id).
    session_result = await db.execute(select(Session).where(Session.id == binding.session_id))
    bound_session = session_result.scalar_one_or_none()
    if (
        bound_session is None
        or bound_session.user_id != user_id
        or bound_session.workspace_id != workspace_id
    ):
        channel = Channel(
            workspace_id=workspace_id,
            user_id=user_id,
            type="telegram",
            external_ref=str(chat_id),
            metadata_={},
        )
        db.add(channel)
        await db.flush()
        session = Session(
            workspace_id=workspace_id,
            user_id=user_id,
            channel_id=channel.id,
            title=f"Telegram chat {chat_id}",
        )
        db.add(session)
        await db.flush()
        binding.session_id = session.id

    binding.workspace_id = workspace_id
    binding.user_id = user_id
    binding.username = username
    binding.first_name = first_name
    binding.last_name = last_name
    binding.updated_at = now
    await db.flush()
    return binding


async def mark_update_processed(
    db: AsyncSession, *, binding: TelegramBinding, incoming_update_id: int
) -> bool:
    """Persist the latest processed Telegram update id.

    Returns ``True`` when the update id was accepted and stored, ``False`` when
    the id is stale (duplicate/out-of-order).
    """
    if is_stale_telegram_update(
        last_update_id=binding.last_update_id,
        incoming_update_id=incoming_update_id,
    ):
        return False
    binding.last_update_id = incoming_update_id
    binding.updated_at = datetime.now(UTC)
    await db.flush()
    return True


async def start_new_chat_session(
    db: AsyncSession, *, binding: TelegramBinding, title: str | None = None
) -> Session:
    """Create and bind a fresh session for `/new` Telegram command semantics."""
    now = datetime.now(UTC)
    channel = Channel(
        workspace_id=binding.workspace_id,
        user_id=binding.user_id,
        type="telegram",
        external_ref=str(binding.chat_id),
        metadata_={},
    )
    db.add(channel)
    await db.flush()
    session = Session(
        workspace_id=binding.workspace_id,
        user_id=binding.user_id,
        channel_id=channel.id,
        title=title or f"Telegram chat {binding.chat_id}",
    )
    db.add(session)
    await db.flush()
    binding.session_id = session.id
    binding.updated_at = now
    await db.flush()
    return session


async def claim_message_processing(
    db: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
    update_id: int,
) -> bool:
    """Try to claim a Telegram message for processing exactly once.

    Returns ``True`` when this worker should process the message, ``False`` when
    another attempt already completed or is currently processing it.
    """
    inserted = await db.execute(
        text(
            """
            INSERT INTO telegram_processed_messages (chat_id, message_id, update_id, status)
            VALUES (:chat_id, :message_id, :update_id, 'processing')
            ON CONFLICT (chat_id, message_id) DO NOTHING
            RETURNING id
            """
        ),
        {"chat_id": chat_id, "message_id": message_id, "update_id": update_id},
    )
    if inserted.first() is not None:
        await db.flush()
        return True

    claimed_retry = await db.execute(
        text(
            """
            UPDATE telegram_processed_messages
            SET status = 'processing',
                update_id = :update_id,
                error = NULL,
                updated_at = now()
            WHERE chat_id = :chat_id
              AND message_id = :message_id
              AND status = 'failed'
            RETURNING id
            """
        ),
        {"chat_id": chat_id, "message_id": message_id, "update_id": update_id},
    )
    await db.flush()
    return claimed_retry.first() is not None


async def mark_message_status(
    db: AsyncSession,
    *,
    chat_id: int,
    message_id: int,
    status: DedupStatus,
    error: str | None = None,
) -> None:
    """Persist terminal status for a claimed Telegram message."""
    await db.execute(
        text(
            """
            UPDATE telegram_processed_messages
            SET status = :status,
                error = :error,
                updated_at = now()
            WHERE chat_id = :chat_id
              AND message_id = :message_id
            """
        ),
        {
            "status": status,
            "error": error,
            "chat_id": chat_id,
            "message_id": message_id,
        },
    )
    await db.flush()


async def cleanup_processed_messages(
    db: AsyncSession,
    *,
    older_than_hours: int,
    statuses: tuple[DedupStatus, ...] = ("completed", "failed"),
) -> int:
    """Delete old idempotency rows and return deleted row count."""
    if older_than_hours <= 0:
        return 0
    if not statuses:
        return 0
    placeholders: list[str] = []
    params: dict[str, object] = {"older_than_hours": older_than_hours}
    for idx, status in enumerate(statuses):
        key = f"status{idx}"
        placeholders.append(f":{key}")
        params[key] = status
    status_clause = f"status IN ({', '.join(placeholders)})"
    result = await db.execute(
        text(
            f"""
            DELETE FROM telegram_processed_messages
            WHERE {status_clause}
              AND updated_at < (now() - make_interval(hours => :older_than_hours))
            RETURNING id
            """
        ),
        params,
    )
    rows = result.fetchall()
    await db.flush()
    return len(rows)


async def reap_stuck_processing_messages(
    db: AsyncSession,
    *,
    older_than_minutes: int,
) -> int:
    """Mark ``processing`` rows older than ``older_than_minutes`` as ``failed``.

    Claims that never reached ``mark_message_status`` (e.g. because the adapter
    process was killed) would otherwise block the same ``(chat_id, message_id)``
    from being retried. Moving them to ``failed`` lets ``claim_message_processing``
    re-acquire them on the next retry and signals the issue for metrics/audit.

    Returns the number of rows marked as failed.
    """
    if older_than_minutes <= 0:
        return 0
    result = await db.execute(
        text(
            """
            UPDATE telegram_processed_messages
            SET status = 'failed',
                error = COALESCE(error, '') ||
                        CASE WHEN COALESCE(error, '') = '' THEN '' ELSE '\n' END ||
                        'reaper: processing timeout',
                updated_at = now()
            WHERE status = 'processing'
              AND updated_at < (now() - make_interval(mins => :older_than_minutes))
            RETURNING id
            """
        ),
        {"older_than_minutes": older_than_minutes},
    )
    rows = result.fetchall()
    await db.flush()
    return len(rows)
