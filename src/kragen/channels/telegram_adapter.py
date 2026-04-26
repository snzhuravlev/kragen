"""Telegram channel adapter process (polling + webhook modes).

This process reads Telegram updates from Bot API, maps chats to Kragen sessions
through ``telegram_bindings``, posts user messages into Kragen HTTP API, polls
task status, and sends the assistant reply back to Telegram.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aioboto3
import httpx
import uvicorn
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select

from kragen.channels.telegram_api import (
    tg_call as _tg_call,
    tg_edit_text as _tg_edit_text,
    tg_get_updates as _tg_get_updates,
    tg_send_processing_stub as _tg_send_processing_stub,
    tg_send_text as _tg_send_text,
    tg_set_commands as _tg_set_commands,
    tg_set_webhook as _tg_set_webhook,
)
from kragen.channels.telegram_settings import TelegramChannelSettings, read_settings
from kragen.channels.telegram_utils import (
    extract_message_payload as _extract_message_payload,
    headers as _headers,
    health_payload as _health_payload,
    looks_like_storage_check_query as _looks_like_storage_check_query,
    safe_filename as _safe_filename,
    split_telegram_message as _split_telegram_message,
)
from kragen.config import get_settings as get_kragen_settings
from kragen.db.session import async_session_factory
from kragen.logging_config import configure_logging, get_logger
from kragen.models.core import Message, Session, Task
from kragen.services import file_storage
from kragen.services.telegram_bindings import (
    claim_message_processing,
    cleanup_processed_messages,
    reap_stuck_processing_messages,
    get_binding_by_chat_id,
    mark_message_status,
    mark_update_processed,
    resolve_or_create_binding,
    start_new_chat_session,
)

logger = get_logger(__name__)

_STREAM_EDIT_INTERVAL_SECONDS = 1.0
_S3_PATH_STYLE_CONFIG = Config(s3={"addressing_style": "path"})
_COMMAND_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Chat and sessions",
        (
            ("/start", "connect this chat to Kragen"),
            ("/new", "start a new session"),
            ("/whoami", "show chat, session, workspace and user binding"),
            ("/sessions", "list recent sessions"),
            ("/tasks", "list recent tasks for the current session"),
        ),
    ),
    (
        "Storage",
        (
            ("/files", "list root files and folders"),
            ("/ls", "alias for /files"),
            ("/mkdir <name>", "create a root folder"),
            ("send document", "upload a Telegram document into /Inbox/Telegram"),
        ),
    ),
    (
        "Help",
        (
            ("/help", "show this help"),
            ("/commands", "show all commands with descriptions"),
        ),
    ),
)
_BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Connect this chat to Kragen"),
    ("new", "Start a new session"),
    ("whoami", "Show chat/session binding"),
    ("sessions", "List recent sessions"),
    ("tasks", "List recent tasks"),
    ("files", "List root files and folders"),
    ("ls", "Alias for /files"),
    ("mkdir", "Create a root folder"),
    ("help", "Show help"),
    ("commands", "Show all commands"),
)


def _read_settings() -> TelegramChannelSettings:
    """Read settings from environment variables."""
    return read_settings()


async def _build_storage_check_reply() -> str:
    """Run storage checks from host runtime and return user-facing report."""
    cfg = get_kragen_settings().storage
    lines: list[str] = [
        "Storage check source: kragen-telegram-channel host runtime.",
        f"Endpoint: {cfg.endpoint_url}",
        f"Bucket: {cfg.bucket}",
    ]
    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
        region_name="us-east-1",
        config=_S3_PATH_STYLE_CONFIG,
    ) as client:
        try:
            await asyncio.wait_for(client.head_bucket(Bucket=cfg.bucket), timeout=6.0)
            lines.append("head_bucket: OK")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"head_bucket: ERROR ({type(exc).__name__}: {exc})")
        try:
            result = await asyncio.wait_for(client.list_buckets(), timeout=6.0)
            names = [b.get("Name") for b in result.get("Buckets", []) if isinstance(b, dict)]
            lines.append(f"list_buckets: OK ({len(names)} bucket(s))")
            if names:
                lines.append("Buckets: " + ", ".join(str(name) for name in names[:20]))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"list_buckets: ERROR ({type(exc).__name__}: {exc})")
    return "\n".join(lines)


async def _persist_direct_telegram_exchange(
    *,
    session_id: uuid.UUID,
    user_text: str,
    assistant_text: str,
    metadata: dict[str, Any],
) -> None:
    """Persist direct adapter reply flow so Web and Telegram stay in sync."""
    async with async_session_factory() as db:
        db.add(
            Message(
                session_id=session_id,
                role="user",
                content=user_text,
                metadata_=metadata,
            )
        )
        db.add(
            Message(
                session_id=session_id,
                role="assistant",
                content=assistant_text,
                metadata_={
                    "channel": "telegram",
                    "source": "telegram_adapter_direct_check",
                },
            )
        )
        await db.commit()


def _is_valid_webhook_secret(
    *,
    configured_secret: str | None,
    received_secret: str | None,
) -> bool:
    """Validate Telegram webhook secret header when configured."""
    if not configured_secret:
        return True
    return received_secret == configured_secret


async def _kragen_post_message(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    session_id: uuid.UUID,
    text: str,
    metadata: dict[str, Any],
) -> uuid.UUID:
    response = await client.post(
        f"{settings.kragen_api_base_url}/sessions/{session_id}/messages",
        json={"role": "user", "content": text, "metadata": metadata},
        headers=_headers(settings),
        timeout=30.0,
    )
    response.raise_for_status()
    payload = response.json()
    return uuid.UUID(payload["task"]["id"])


async def _kragen_wait_task(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    task_id: uuid.UUID,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + settings.task_wait_timeout_seconds
    while True:
        response = await client.get(
            f"{settings.kragen_api_base_url}/tasks/{task_id}",
            headers=_headers(settings),
            timeout=20.0,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected task payload shape for task {task_id}: {type(data)}")
        status = str(data.get("status", "")).lower()
        if status in {"completed", "failed"}:
            return data
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Task {task_id} did not complete within timeout")
        await asyncio.sleep(settings.task_poll_interval_seconds)


async def _kragen_stream_task_progress(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    task_id: uuid.UUID,
    on_text: Callable[[str], Awaitable[None]],
) -> None:
    """Consume task SSE stream and call ``on_text`` with aggregated text."""
    url = f"{settings.kragen_api_base_url}/tasks/{task_id}/stream"
    headers = _headers(settings)
    headers["Accept"] = "text/event-stream"
    buffer = ""
    last_emit = 0.0
    async with client.stream("GET", url, headers=headers, timeout=None) as response:
        response.raise_for_status()
        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(decoded, str):
                continue
            buffer += decoded
            now = asyncio.get_running_loop().time()
            if now - last_emit >= _STREAM_EDIT_INTERVAL_SECONDS:
                await on_text(buffer)
                last_emit = now
        if buffer:
            await on_text(buffer)


async def _kragen_last_assistant_message(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    session_id: uuid.UUID,
) -> str:
    response = await client.get(
        f"{settings.kragen_api_base_url}/sessions/{session_id}/messages",
        headers=_headers(settings),
        timeout=20.0,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        return ""
    for item in reversed(data):
        if isinstance(item, dict) and item.get("role") == "assistant":
            content = item.get("content")
            if isinstance(content, str):
                return content
    return ""


async def _handle_command_new(
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
) -> str:
    async with async_session_factory() as db:
        binding = await get_binding_by_chat_id(db, chat_id=chat_id)
        if binding is None:
            binding = await resolve_or_create_binding(
                db,
                chat_id=chat_id,
                workspace_id=settings.default_workspace_id,
                user_id=settings.auth_user_id,
            )
        session = await start_new_chat_session(db, binding=binding)
        await db.commit()
    return f"Started a new session: `{session.id}`"


async def _handle_command_whoami(
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> str:
    """Return diagnostic identity info for the current Telegram chat binding."""
    async with async_session_factory() as db:
        binding = await resolve_or_create_binding(
            db,
            chat_id=chat_id,
            workspace_id=settings.default_workspace_id,
            user_id=settings.auth_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        await db.commit()
    return (
        "Telegram binding diagnostics:\n"
        f"chat_id: `{binding.chat_id}`\n"
        f"session_id: `{binding.session_id}`\n"
        f"workspace_id: `{binding.workspace_id}`\n"
        f"user_id: `{binding.user_id}`\n"
        f"last_update_id: `{binding.last_update_id}`"
    )


async def _handle_command_sessions(
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> str:
    """Return sessions list with title and short description."""
    async with async_session_factory() as db:
        await resolve_or_create_binding(
            db,
            chat_id=chat_id,
            workspace_id=settings.default_workspace_id,
            user_id=settings.auth_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        result = await db.execute(
            select(Session.id, Session.title, Session.updated_at, Session.created_at)
            .where(Session.user_id == settings.auth_user_id)
            .where(Session.workspace_id == settings.default_workspace_id)
            .order_by(Session.updated_at.desc())
            .limit(50)
        )
        await db.commit()
    rows = result.fetchall()
    if not rows:
        return "*Sessions*\n- `(empty)`"
    lines = ["*Sessions*"]
    for idx, row in enumerate(rows, start=1):
        sid = str(row[0])
        title = row[1] or "Untitled session"
        updated_at = row[2].isoformat() if row[2] is not None else "n/a"
        created_at = row[3].isoformat() if row[3] is not None else "n/a"
        lines.append(f"- *{idx}.* `{sid}`")
        lines.append(f"  title: {_escape_md(title)}")
        lines.append(
            "  description: "
            + _escape_md(f"created {created_at}, updated {updated_at}")
        )
    return "\n".join(lines)


async def _handle_command_tasks(
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> str:
    """Return tasks list with status and short description."""
    async with async_session_factory() as db:
        binding = await resolve_or_create_binding(
            db,
            chat_id=chat_id,
            workspace_id=settings.default_workspace_id,
            user_id=settings.auth_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        result = await db.execute(
            select(Task.id, Task.status, Task.error, Task.created_at, Task.updated_at)
            .where(Task.session_id == binding.session_id)
            .order_by(Task.created_at.desc())
            .limit(50)
        )
        await db.commit()
    rows = result.fetchall()
    if not rows:
        return "*Tasks*\n- `(empty)`"
    lines = ["*Tasks*"]
    for idx, row in enumerate(rows, start=1):
        tid = str(row[0])
        status = str(row[1])
        error = str(row[2]) if row[2] else "none"
        created_at = row[3].isoformat() if row[3] is not None else "n/a"
        updated_at = row[4].isoformat() if row[4] is not None else "n/a"
        lines.append(f"- *{idx}.* `{tid}`")
        lines.append(f"  title: `{_escape_md(status)}`")
        lines.append(
            "  description: "
            + _escape_md(f"created {created_at}, updated {updated_at}, error {error}")
        )
    return "\n".join(lines)


async def _handle_command_files(
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> str:
    """Return root file tree entries for the bound workspace."""
    async with async_session_factory() as db:
        await resolve_or_create_binding(
            db,
            chat_id=chat_id,
            workspace_id=settings.default_workspace_id,
            user_id=settings.auth_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        entries = await file_storage.list_entries(
            db,
            workspace_id=settings.default_workspace_id,
            parent_id=None,
        )
        await db.commit()
    if not entries:
        return "*Files*\n- `(empty)`"
    lines = ["*Files*"]
    for entry in entries[:30]:
        marker = "folder" if entry.kind == "folder" else "file"
        size = f", {entry.size_bytes} bytes" if entry.size_bytes is not None else ""
        lines.append(
            f"- `{entry.id}` {_escape_md(entry.name)} "
            f"({_escape_md(marker)}{_escape_md(size)})"
        )
    return "\n".join(lines)


async def _handle_command_mkdir(
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
    name: str,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> str:
    """Create a root folder from Telegram."""
    async with async_session_factory() as db:
        await resolve_or_create_binding(
            db,
            chat_id=chat_id,
            workspace_id=settings.default_workspace_id,
            user_id=settings.auth_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        entry = await file_storage.create_folder(
            db,
            workspace_id=settings.default_workspace_id,
            parent_id=None,
            name=name,
            created_by_user_id=settings.auth_user_id,
            source_type="telegram",
            metadata={"telegram_chat_id": chat_id},
        )
        await db.commit()
    return f"Папка создана: `{_escape_md(entry.path_cache)}`"


def _help_text() -> str:
    """Return all supported commands grouped by area."""
    lines = ["*Available commands*"]
    for section, commands in _COMMAND_SECTIONS:
        lines.append("")
        lines.append(f"*{_escape_md(section)}*")
        for command, description in commands:
            lines.append(f"- `{_escape_md(command)}` — {_escape_md(description)}")
    return "\n".join(lines)


def _bot_commands_payload() -> list[dict[str, str]]:
    """Return Telegram Bot API command descriptors."""
    return [
        {"command": command, "description": description}
        for command, description in _BOT_COMMANDS
    ]


def _escape_md(value: str) -> str:
    """Escape Telegram Markdown special chars in plain text fragments."""
    escaped = value.replace("\\", "\\\\")
    for ch in ("`", "*", "_", "[", "]"):
        escaped = escaped.replace(ch, f"\\{ch}")
    return escaped


async def _handle_user_text(
    tg_client: httpx.AsyncClient,
    kragen_client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    update_id: int,
    chat_id: int,
    message_id: int,
    text: str,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> None:
    async with async_session_factory() as db:
        binding = await resolve_or_create_binding(
            db,
            chat_id=chat_id,
            workspace_id=settings.default_workspace_id,
            user_id=settings.auth_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        await db.commit()

    metadata = {
        "channel": "telegram",
        "telegram_chat_id": chat_id,
        "telegram_message_id": message_id,
        "telegram_update_id": update_id,
        "telegram_username": username,
    }

    if _looks_like_storage_check_query(text):
        reply = await _build_storage_check_reply()
        await _persist_direct_telegram_exchange(
            session_id=binding.session_id,
            user_text=text,
            assistant_text=reply,
            metadata=metadata,
        )
        await _tg_send_text(tg_client, settings=settings, chat_id=chat_id, text=reply)
        async with async_session_factory() as db:
            binding_after = await get_binding_by_chat_id(db, chat_id=chat_id)
            if binding_after is not None:
                accepted = await mark_update_processed(
                    db,
                    binding=binding_after,
                    incoming_update_id=update_id,
                )
                if accepted:
                    await db.commit()
                else:
                    await db.rollback()
        return

    processing_message_id = await _tg_send_processing_stub(
        tg_client,
        settings=settings,
        chat_id=chat_id,
    )

    task_id = await _kragen_post_message(
        kragen_client,
        settings=settings,
        session_id=binding.session_id,
        text=text,
        metadata=metadata,
    )

    if processing_message_id is not None:
        last_preview = ""

        async def _update_preview(aggregated: str) -> None:
            nonlocal last_preview
            preview = aggregated.strip()
            if not preview:
                return
            if preview == last_preview:
                return
            last_preview = preview
            try:
                await _tg_edit_text(
                    tg_client,
                    settings=settings,
                    chat_id=chat_id,
                    message_id=processing_message_id,
                    text=preview,
                )
            except Exception:
                logger.debug("telegram_edit_preview_failed", task_id=str(task_id))

        try:
            await _kragen_stream_task_progress(
                kragen_client,
                settings=settings,
                task_id=task_id,
                on_text=_update_preview,
            )
        except Exception:
            # Best effort streaming; final response path still covers delivery.
            logger.debug("task_stream_preview_failed", task_id=str(task_id))

    task_data = await _kragen_wait_task(kragen_client, settings=settings, task_id=task_id)

    reply = await _kragen_last_assistant_message(
        kragen_client,
        settings=settings,
        session_id=binding.session_id,
    )
    if not reply:
        if str(task_data.get("status", "")).lower() == "failed":
            reply = f"Task failed: {task_data.get('error') or 'unknown error'}"
        else:
            reply = "Task finished without assistant output."

    if processing_message_id is not None:
        chunks = _split_telegram_message(reply)
        try:
            await _tg_edit_text(
                tg_client,
                settings=settings,
                chat_id=chat_id,
                message_id=processing_message_id,
                text=chunks[0],
            )
            for tail in chunks[1:]:
                await _tg_send_text(tg_client, settings=settings, chat_id=chat_id, text=tail)
        except Exception:
            await _tg_send_text(tg_client, settings=settings, chat_id=chat_id, text=reply)
    else:
        await _tg_send_text(tg_client, settings=settings, chat_id=chat_id, text=reply)

    async with async_session_factory() as db:
        binding_after = await get_binding_by_chat_id(db, chat_id=chat_id)
        if binding_after is not None:
            accepted = await mark_update_processed(
                db,
                binding=binding_after,
                incoming_update_id=update_id,
            )
            if accepted:
                await db.commit()
            else:
                await db.rollback()


async def _handle_user_document(
    tg_client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    update_id: int,
    chat_id: int,
    message_id: int,
    document: dict[str, Any],
    text: str | None,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> None:
    """Download Telegram document, upload to object storage, and confirm in chat."""
    file_id = document.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        raise RuntimeError("Telegram document payload is missing file_id")

    file_name_value = document.get("file_name")
    file_name = str(file_name_value) if isinstance(file_name_value, str) else "document.bin"
    mime_value = document.get("mime_type")
    mime_type = str(mime_value) if isinstance(mime_value, str) else "application/octet-stream"
    unique_id_value = document.get("file_unique_id")
    file_unique_id = str(unique_id_value) if isinstance(unique_id_value, str) else file_id

    async with async_session_factory() as db:
        binding = await resolve_or_create_binding(
            db,
            chat_id=chat_id,
            workspace_id=settings.default_workspace_id,
            user_id=settings.auth_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        await db.commit()

    get_file_payload = await _tg_call(
        tg_client,
        settings=settings,
        method="getFile",
        payload={"file_id": file_id},
    )
    file_path = get_file_payload.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        raise RuntimeError("Telegram getFile did not return file_path")

    file_response = await tg_client.get(
        f"https://api.telegram.org/file/bot{settings.bot_token}/{file_path}",
        timeout=60.0,
    )
    file_response.raise_for_status()
    file_bytes = file_response.content
    if not file_bytes:
        raise RuntimeError("Downloaded Telegram document is empty")

    safe_name = _safe_filename(file_name)
    async with async_session_factory() as db:
        inbox = await file_storage.ensure_folder_path(
            db,
            workspace_id=binding.workspace_id,
            path="/Inbox/Telegram",
            created_by_user_id=binding.user_id,
            source_type="telegram",
        )
        try:
            storage_entry, document_row = await file_storage.create_file_from_bytes(
                db,
                workspace_id=binding.workspace_id,
                parent_id=inbox.id if inbox is not None else None,
                name=safe_name,
                body=file_bytes,
                mime_type=mime_type,
                created_by_user_id=binding.user_id,
                source_type="telegram",
                metadata={
                    "telegram_chat_id": chat_id,
                    "telegram_message_id": message_id,
                    "telegram_update_id": update_id,
                    "telegram_username": username,
                    "telegram_document_file_id": file_id,
                    "telegram_document_file_unique_id": file_unique_id,
                    "telegram_document_file_name": file_name,
                },
                create_document=True,
            )
        except file_storage.StorageEntryConflict:
            stem, ext = os.path.splitext(safe_name)
            retry_name = f"{stem}-{file_unique_id[:12]}{ext}"
            storage_entry, document_row = await file_storage.create_file_from_bytes(
                db,
                workspace_id=binding.workspace_id,
                parent_id=inbox.id if inbox is not None else None,
                name=retry_name,
                body=file_bytes,
                mime_type=mime_type,
                created_by_user_id=binding.user_id,
                source_type="telegram",
                metadata={
                    "telegram_chat_id": chat_id,
                    "telegram_message_id": message_id,
                    "telegram_update_id": update_id,
                    "telegram_username": username,
                    "telegram_document_file_id": file_id,
                    "telegram_document_file_unique_id": file_unique_id,
                    "telegram_document_file_name": file_name,
                },
                create_document=True,
            )
        await db.commit()

    user_text = text or f"[document] {file_name}"
    assistant_text = (
        "Документ сохранён в файловое хранилище.\n"
        f"File: {file_name}\n"
        f"Size: {len(file_bytes)} bytes\n"
        f"Path: {storage_entry.path_cache}\n"
        f"ID: {storage_entry.id}"
    )
    metadata = {
        "channel": "telegram",
        "telegram_chat_id": chat_id,
        "telegram_message_id": message_id,
        "telegram_update_id": update_id,
        "telegram_username": username,
        "telegram_document_file_id": file_id,
        "telegram_document_file_name": file_name,
        "telegram_document_mime_type": mime_type,
        "storage_entry_id": str(storage_entry.id),
        "document_id": str(document_row.id) if document_row is not None else None,
        "telegram_document_uri": storage_entry.uri,
    }
    await _persist_direct_telegram_exchange(
        session_id=binding.session_id,
        user_text=user_text,
        assistant_text=assistant_text,
        metadata=metadata,
    )
    await _tg_send_text(
        tg_client,
        settings=settings,
        chat_id=chat_id,
        text=assistant_text,
    )

    async with async_session_factory() as db:
        binding_after = await get_binding_by_chat_id(db, chat_id=chat_id)
        if binding_after is not None:
            accepted = await mark_update_processed(
                db,
                binding=binding_after,
                incoming_update_id=update_id,
            )
            if accepted:
                await db.commit()
            else:
                await db.rollback()


async def _handle_update(
    tg_client: httpx.AsyncClient,
    kragen_client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    update: dict[str, Any],
) -> None:
    message = update.get("message")
    if not isinstance(message, dict):
        return
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return
    text, document = _extract_message_payload(message)
    if text is None and document is None:
        return

    chat_id_val = chat.get("id")
    update_id_val = update.get("update_id")
    message_id_val = message.get("message_id")
    if not isinstance(chat_id_val, int):
        return
    if not isinstance(update_id_val, int):
        return
    if not isinstance(message_id_val, int):
        return
    chat_id = chat_id_val
    update_id = update_id_val
    message_id = message_id_val
    from_user = message.get("from")
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    if isinstance(from_user, dict):
        username_value = from_user.get("username")
        first_name_value = from_user.get("first_name")
        last_name_value = from_user.get("last_name")
        username = str(username_value) if username_value is not None else None
        first_name = str(first_name_value) if first_name_value is not None else None
        last_name = str(last_name_value) if last_name_value is not None else None

    command_text = text.strip() if isinstance(text, str) else ""
    command = command_text.lower()
    async with async_session_factory() as db:
        claimed = await claim_message_processing(
            db,
            chat_id=chat_id,
            message_id=message_id,
            update_id=update_id,
        )
        if not claimed:
            await db.rollback()
            return
        await db.commit()

    try:
        if command == "/start":
            async with async_session_factory() as db:
                binding = await resolve_or_create_binding(
                    db,
                    chat_id=chat_id,
                    workspace_id=settings.default_workspace_id,
                    user_id=settings.auth_user_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                )
                await mark_update_processed(
                    db,
                    binding=binding,
                    incoming_update_id=update_id,
                )
                await db.commit()
            await _tg_send_text(
                tg_client,
                settings=settings,
                chat_id=chat_id,
                text=(
                    "Connected to Kragen.\n"
                    "Use /commands to see all commands.\n"
                    "Send any text to run it via Kragen worker, or send a document "
                    "to store it in /Inbox/Telegram."
                ),
            )
            async with async_session_factory() as db:
                await mark_message_status(
                    db,
                    chat_id=chat_id,
                    message_id=message_id,
                    status="completed",
                )
                await db.commit()
            return
        if command == "/new":
            message_text = await _handle_command_new(
                settings=settings,
                chat_id=chat_id,
            )
            await _tg_send_text(tg_client, settings=settings, chat_id=chat_id, text=message_text)
            async with async_session_factory() as db:
                maybe_binding = await get_binding_by_chat_id(db, chat_id=chat_id)
                if maybe_binding is not None:
                    await mark_update_processed(
                        db,
                        binding=maybe_binding,
                        incoming_update_id=update_id,
                    )
                    await db.commit()
            async with async_session_factory() as db:
                await mark_message_status(
                    db,
                    chat_id=chat_id,
                    message_id=message_id,
                    status="completed",
                )
                await db.commit()
            return
        if command == "/whoami":
            message_text = await _handle_command_whoami(
                settings=settings,
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            await _tg_send_text(tg_client, settings=settings, chat_id=chat_id, text=message_text)
            async with async_session_factory() as db:
                maybe_binding = await get_binding_by_chat_id(db, chat_id=chat_id)
                if maybe_binding is not None:
                    await mark_update_processed(
                        db,
                        binding=maybe_binding,
                        incoming_update_id=update_id,
                    )
                    await db.commit()
            async with async_session_factory() as db:
                await mark_message_status(
                    db,
                    chat_id=chat_id,
                    message_id=message_id,
                    status="completed",
                )
                await db.commit()
            return
        if command == "/sessions":
            message_text = await _handle_command_sessions(
                settings=settings,
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            await _tg_send_text(
                tg_client,
                settings=settings,
                chat_id=chat_id,
                text=message_text,
                parse_mode="Markdown",
            )
            async with async_session_factory() as db:
                maybe_binding = await get_binding_by_chat_id(db, chat_id=chat_id)
                if maybe_binding is not None:
                    await mark_update_processed(
                        db,
                        binding=maybe_binding,
                        incoming_update_id=update_id,
                    )
                    await db.commit()
            async with async_session_factory() as db:
                await mark_message_status(
                    db,
                    chat_id=chat_id,
                    message_id=message_id,
                    status="completed",
                )
                await db.commit()
            return
        if command == "/tasks":
            message_text = await _handle_command_tasks(
                settings=settings,
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            await _tg_send_text(
                tg_client,
                settings=settings,
                chat_id=chat_id,
                text=message_text,
                parse_mode="Markdown",
            )
            async with async_session_factory() as db:
                maybe_binding = await get_binding_by_chat_id(db, chat_id=chat_id)
                if maybe_binding is not None:
                    await mark_update_processed(
                        db,
                        binding=maybe_binding,
                        incoming_update_id=update_id,
                    )
                    await db.commit()
            async with async_session_factory() as db:
                await mark_message_status(
                    db,
                    chat_id=chat_id,
                    message_id=message_id,
                    status="completed",
                )
                await db.commit()
            return
        if command in {"/files", "/ls"}:
            message_text = await _handle_command_files(
                settings=settings,
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            await _tg_send_text(
                tg_client,
                settings=settings,
                chat_id=chat_id,
                text=message_text,
                parse_mode="Markdown",
            )
            async with async_session_factory() as db:
                maybe_binding = await get_binding_by_chat_id(db, chat_id=chat_id)
                if maybe_binding is not None:
                    await mark_update_processed(
                        db,
                        binding=maybe_binding,
                        incoming_update_id=update_id,
                    )
                    await db.commit()
            async with async_session_factory() as db:
                await mark_message_status(
                    db,
                    chat_id=chat_id,
                    message_id=message_id,
                    status="completed",
                )
                await db.commit()
            return
        if command == "/mkdir" or command.startswith("/mkdir "):
            folder_name = command_text.removeprefix("/mkdir").strip()
            if not folder_name:
                message_text = "Usage: `/mkdir folder-name`"
            else:
                try:
                    message_text = await _handle_command_mkdir(
                        settings=settings,
                        chat_id=chat_id,
                        name=folder_name,
                        username=username,
                        first_name=first_name,
                        last_name=last_name,
                    )
                except file_storage.FileStorageError as exc:
                    message_text = f"Не удалось создать папку: {_escape_md(str(exc))}"
            await _tg_send_text(
                tg_client,
                settings=settings,
                chat_id=chat_id,
                text=message_text,
                parse_mode="Markdown",
            )
            async with async_session_factory() as db:
                maybe_binding = await get_binding_by_chat_id(db, chat_id=chat_id)
                if maybe_binding is not None:
                    await mark_update_processed(
                        db,
                        binding=maybe_binding,
                        incoming_update_id=update_id,
                    )
                    await db.commit()
            async with async_session_factory() as db:
                await mark_message_status(
                    db,
                    chat_id=chat_id,
                    message_id=message_id,
                    status="completed",
                )
                await db.commit()
            return
        if command in {"/help", "/commands"}:
            await _tg_send_text(
                tg_client,
                settings=settings,
                chat_id=chat_id,
                text=_help_text(),
                parse_mode="Markdown",
            )
            async with async_session_factory() as db:
                maybe_binding = await get_binding_by_chat_id(db, chat_id=chat_id)
                if maybe_binding is not None:
                    await mark_update_processed(
                        db,
                        binding=maybe_binding,
                        incoming_update_id=update_id,
                    )
                    await db.commit()
            async with async_session_factory() as db:
                await mark_message_status(
                    db,
                    chat_id=chat_id,
                    message_id=message_id,
                    status="completed",
                )
                await db.commit()
            return

        if document is not None:
            await _handle_user_document(
                tg_client,
                settings=settings,
                update_id=update_id,
                chat_id=chat_id,
                message_id=message_id,
                document=document,
                text=text,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
        elif text is not None:
            await _handle_user_text(
                tg_client,
                kragen_client,
                settings=settings,
                update_id=update_id,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
        async with async_session_factory() as db:
            await mark_message_status(
                db,
                chat_id=chat_id,
                message_id=message_id,
                status="completed",
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.exception("telegram_update_handle_failed", error=str(exc))
        async with async_session_factory() as db:
            await mark_message_status(
                db,
                chat_id=chat_id,
                message_id=message_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            await db.commit()
        await _tg_send_text(
            tg_client,
            settings=settings,
            chat_id=chat_id,
            text=f"Error while processing request: {type(exc).__name__}: {exc}",
        )


async def _handle_update_with_timeout(
    tg_client: httpx.AsyncClient,
    kragen_client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    update: dict[str, Any],
) -> None:
    """Run one update handler with timeout so one stuck task won't block others."""
    update_id_val = update.get("update_id")
    update_id = str(update_id_val) if update_id_val is not None else "unknown"
    message = update.get("message")
    chat_id: int | None = None
    message_id: int | None = None
    if isinstance(message, dict):
        chat = message.get("chat")
        chat_id_val = chat.get("id") if isinstance(chat, dict) else None
        message_id_val = message.get("message_id")
        if isinstance(chat_id_val, int):
            chat_id = chat_id_val
        if isinstance(message_id_val, int):
            message_id = message_id_val
    try:
        await asyncio.wait_for(
            _handle_update(
                tg_client,
                kragen_client,
                settings=settings,
                update=update,
            ),
            timeout=max(30, settings.task_wait_timeout_seconds + 20),
        )
    except asyncio.TimeoutError:
        logger.warning("telegram_update_timeout", update_id=update_id)
        if chat_id is not None and message_id is not None:
            async with async_session_factory() as db:
                await mark_message_status(
                    db,
                    chat_id=chat_id,
                    message_id=message_id,
                    status="failed",
                    error="TimeoutError: telegram update timed out",
                )
                await db.commit()
            try:
                await _tg_send_text(
                    tg_client,
                    settings=settings,
                    chat_id=chat_id,
                    text=(
                        "Запрос выполнялся слишком долго и был остановлен. "
                        "Попробуйте повторить короче."
                    ),
                )
            except Exception:
                logger.debug("telegram_timeout_notification_failed", update_id=update_id)
    except Exception:  # noqa: BLE001
        logger.exception("telegram_update_wrapper_failed", update_id=update_id)


async def run_telegram_channel() -> None:
    """Main long-polling loop."""
    settings = _read_settings()
    logger.info(
        "telegram_channel_start",
        api_base=settings.kragen_api_base_url,
        workspace_id=str(settings.default_workspace_id),
        auth_user_id=str(settings.auth_user_id),
    )

    offset: int | None = None
    cleanup_task = asyncio.create_task(_dedup_cleanup_worker(settings=settings))
    pending_updates: set[asyncio.Task[None]] = set()
    try:
        async with httpx.AsyncClient() as tg_client, httpx.AsyncClient() as kragen_client:
            await _tg_set_commands(
                tg_client,
                settings=settings,
                commands=_bot_commands_payload(),
            )
            while True:
                try:
                    updates = await _tg_get_updates(
                        tg_client,
                        settings=settings,
                        offset=offset,
                    )
                    for update in updates:
                        update_id_val = update.get("update_id")
                        if not isinstance(update_id_val, int):
                            continue
                        task = asyncio.create_task(
                            _handle_update_with_timeout(
                                tg_client,
                                kragen_client,
                                settings=settings,
                                update=update,
                            )
                        )
                        pending_updates.add(task)
                        task.add_done_callback(pending_updates.discard)
                        if len(pending_updates) >= 32:
                            done, pending = await asyncio.wait(
                                pending_updates, return_when=asyncio.FIRST_COMPLETED
                            )
                            pending_updates = set(pending)
                            for done_task in done:
                                try:
                                    done_task.result()
                                except Exception:
                                    # _handle_update_with_timeout already logs internals.
                                    pass
                        offset = update_id_val + 1
                except httpx.HTTPError as exc:
                    logger.warning("telegram_http_error", error=str(exc))
                    await asyncio.sleep(2.0)
                except Exception:  # noqa: BLE001
                    logger.exception("telegram_loop_error")
                    await asyncio.sleep(2.0)
                await asyncio.sleep(settings.loop_delay_seconds)
    finally:
        cleanup_task.cancel()
        for task in list(pending_updates):
            task.cancel()
        if pending_updates:
            await asyncio.gather(*pending_updates, return_exceptions=True)
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


async def _webhook_worker(
    *,
    queue: asyncio.Queue[dict[str, Any]],
    settings: TelegramChannelSettings,
) -> None:
    """Consume webhook updates and run the same handler as polling mode."""
    async with httpx.AsyncClient() as tg_client, httpx.AsyncClient() as kragen_client:
        while True:
            update = await queue.get()
            try:
                await _handle_update_with_timeout(
                    tg_client,
                    kragen_client,
                    settings=settings,
                    update=update,
                )
            except Exception:
                logger.exception("telegram_webhook_update_failed")
            finally:
                queue.task_done()


async def run_telegram_channel_webhook() -> None:
    """Webhook mode: FastAPI receiver + background worker."""
    settings = _read_settings()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    app = FastAPI(title="kragen-telegram-channel")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return _health_payload(settings)

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        worker_task = getattr(app.state, "worker_task", None)
        if worker_task is None or worker_task.done():
            raise HTTPException(status_code=503, detail="Webhook worker is not running")
        return _health_payload(settings)

    @app.post(settings.webhook_path)
    async def telegram_webhook(request: Request) -> dict[str, bool]:
        received_secret = request.headers.get("x-telegram-bot-api-secret-token")
        if not _is_valid_webhook_secret(
            configured_secret=settings.webhook_secret_token,
            received_secret=received_secret,
        ):
            raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret token")
        payload = await request.json()
        if isinstance(payload, dict):
            await queue.put(payload)
        return {"ok": True}

    @app.on_event("startup")
    async def _startup() -> None:
        async with httpx.AsyncClient() as tg_client:
            await _tg_set_commands(
                tg_client,
                settings=settings,
                commands=_bot_commands_payload(),
            )
            await _tg_set_webhook(tg_client, settings=settings)
        app.state.worker_task = asyncio.create_task(_webhook_worker(queue=queue, settings=settings))
        app.state.cleanup_task = asyncio.create_task(_dedup_cleanup_worker(settings=settings))

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task = getattr(app.state, "worker_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        cleanup_task = getattr(app.state, "cleanup_task", None)
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

    config = uvicorn.Config(
        app=app,
        host=settings.webhook_host,
        port=settings.webhook_port,
        log_level=os.environ.get("KRAGEN_TELEGRAM_LOG_LEVEL", "info").lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()




async def _dedup_cleanup_worker(*, settings: TelegramChannelSettings) -> None:
    """Periodically reap stuck ``processing`` rows and purge old dedup records."""
    interval = max(60, settings.dedup_cleanup_interval_seconds)
    retention = max(1, settings.dedup_retention_hours)
    processing_timeout = max(1, settings.dedup_processing_timeout_minutes)
    while True:
        try:
            async with async_session_factory() as db:
                reaped = await reap_stuck_processing_messages(
                    db,
                    older_than_minutes=processing_timeout,
                )
                deleted = await cleanup_processed_messages(
                    db,
                    older_than_hours=retention,
                )
                await db.commit()
                if reaped > 0 or deleted > 0:
                    logger.info(
                        "telegram_dedup_cleanup",
                        reaped=reaped,
                        deleted=deleted,
                        retention_hours=retention,
                        processing_timeout_minutes=processing_timeout,
                    )
        except Exception:  # noqa: BLE001
            logger.exception("telegram_dedup_cleanup_failed")
        await asyncio.sleep(interval)


def main() -> None:
    """Console entrypoint."""
    configure_logging(os.environ.get("KRAGEN_TELEGRAM_LOG_LEVEL", "INFO"))
    settings = _read_settings()
    if settings.mode == "webhook":
        asyncio.run(run_telegram_channel_webhook())
        return
    asyncio.run(run_telegram_channel())


if __name__ == "__main__":
    main()
