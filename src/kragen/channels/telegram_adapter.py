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
from dataclasses import dataclass
from typing import Any

import aioboto3
import httpx
import uvicorn
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Request

from kragen.config import get_settings as get_kragen_settings
from kragen.db.session import async_session_factory
from kragen.logging_config import configure_logging, get_logger
from kragen.models.core import Message
from kragen.services.telegram_bindings import (
    claim_message_processing,
    cleanup_processed_messages,
    get_binding_by_chat_id,
    is_stale_telegram_update,
    mark_message_status,
    mark_update_processed,
    resolve_or_create_binding,
    start_new_chat_session,
)

logger = get_logger(__name__)

_TELEGRAM_MESSAGE_MAX = 4096
_STREAM_EDIT_INTERVAL_SECONDS = 1.0
_S3_PATH_STYLE_CONFIG = Config(s3={"addressing_style": "path"})


@dataclass(frozen=True)
class TelegramChannelSettings:
    """Runtime settings for Telegram channel adapter."""

    bot_token: str
    kragen_api_base_url: str
    auth_user_id: uuid.UUID
    default_workspace_id: uuid.UUID
    poll_timeout_seconds: int = 20
    loop_delay_seconds: float = 0.4
    task_poll_interval_seconds: float = 1.0
    task_wait_timeout_seconds: int = 300
    mode: str = "polling"
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8081
    webhook_path: str = "/telegram/webhook"
    webhook_public_url: str | None = None
    webhook_secret_token: str | None = None
    dedup_retention_hours: int = 168
    dedup_cleanup_interval_seconds: int = 3600

    @property
    def telegram_api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"


def _read_settings() -> TelegramChannelSettings:
    """Read settings from environment variables."""
    yaml_cfg = get_kragen_settings().telegram_channel

    token = os.environ.get("KRAGEN_TELEGRAM_BOT_TOKEN", yaml_cfg.bot_token).strip()
    if not token:
        raise RuntimeError("KRAGEN_TELEGRAM_BOT_TOKEN is required")

    api_base = os.environ.get("KRAGEN_TELEGRAM_API_BASE_URL", yaml_cfg.api_base_url).strip()
    auth_user = os.environ.get("KRAGEN_TELEGRAM_AUTH_USER_ID", yaml_cfg.auth_user_id).strip()
    workspace = os.environ.get(
        "KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID", yaml_cfg.default_workspace_id
    ).strip()
    if not auth_user:
        raise RuntimeError("KRAGEN_TELEGRAM_AUTH_USER_ID is required")
    if not workspace:
        raise RuntimeError("KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID is required")

    return TelegramChannelSettings(
        bot_token=token,
        kragen_api_base_url=api_base,
        auth_user_id=uuid.UUID(auth_user),
        default_workspace_id=uuid.UUID(workspace),
        poll_timeout_seconds=int(
            os.environ.get("KRAGEN_TELEGRAM_POLL_TIMEOUT_SECONDS", str(yaml_cfg.poll_timeout_seconds))
        ),
        loop_delay_seconds=float(
            os.environ.get("KRAGEN_TELEGRAM_LOOP_DELAY_SECONDS", str(yaml_cfg.loop_delay_seconds))
        ),
        task_poll_interval_seconds=float(
            os.environ.get(
                "KRAGEN_TELEGRAM_TASK_POLL_INTERVAL_SECONDS",
                str(yaml_cfg.task_poll_interval_seconds),
            )
        ),
        task_wait_timeout_seconds=int(
            os.environ.get(
                "KRAGEN_TELEGRAM_TASK_WAIT_TIMEOUT_SECONDS",
                str(yaml_cfg.task_wait_timeout_seconds),
            )
        ),
        mode=os.environ.get("KRAGEN_TELEGRAM_MODE", yaml_cfg.mode).strip().lower(),
        webhook_host=os.environ.get("KRAGEN_TELEGRAM_WEBHOOK_HOST", yaml_cfg.webhook_host).strip(),
        webhook_port=int(os.environ.get("KRAGEN_TELEGRAM_WEBHOOK_PORT", str(yaml_cfg.webhook_port))),
        webhook_path=os.environ.get("KRAGEN_TELEGRAM_WEBHOOK_PATH", yaml_cfg.webhook_path).strip(),
        webhook_public_url=(
            os.environ.get("KRAGEN_TELEGRAM_WEBHOOK_PUBLIC_URL", yaml_cfg.webhook_public_url or "").strip()
            or None
        ),
        webhook_secret_token=(
            os.environ.get(
                "KRAGEN_TELEGRAM_WEBHOOK_SECRET_TOKEN",
                yaml_cfg.webhook_secret_token or "",
            ).strip()
            or None
        ),
        dedup_retention_hours=int(
            os.environ.get("KRAGEN_TELEGRAM_DEDUP_RETENTION_HOURS", str(yaml_cfg.dedup_retention_hours))
        ),
        dedup_cleanup_interval_seconds=int(
            os.environ.get(
                "KRAGEN_TELEGRAM_DEDUP_CLEANUP_INTERVAL_SECONDS",
                str(yaml_cfg.dedup_cleanup_interval_seconds),
            )
        ),
    )


def _split_telegram_message(text: str, max_len: int = _TELEGRAM_MESSAGE_MAX) -> list[str]:
    """Split long text into Telegram-compatible chunks."""
    normalized = text.strip()
    if not normalized:
        return ["(empty response)"]
    if len(normalized) <= max_len:
        return [normalized]

    chunks: list[str] = []
    start = 0
    length = len(normalized)
    while start < length:
        end = min(start + max_len, length)
        if end < length:
            pivot = normalized.rfind("\n", start, end)
            if pivot <= start:
                pivot = normalized.rfind(" ", start, end)
            if pivot > start:
                end = pivot
        piece = normalized[start:end].strip()
        if piece:
            chunks.append(piece)
        start = end
    return chunks or ["(empty response)"]


async def _tg_call(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Call Telegram Bot API and return decoded payload."""
    response = await client.post(
        f"{settings.telegram_api_base}/{method}",
        json=payload,
        timeout=max(30.0, float(settings.poll_timeout_seconds) + 10.0),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {data}")
    result = data.get("result")
    if isinstance(result, dict):
        return result
    return {"result": result}


async def _tg_get_updates(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    offset: int | None,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "timeout": settings.poll_timeout_seconds,
        "allowed_updates": ["message"],
    }
    if offset is not None:
        payload["offset"] = offset
    resp = await _tg_call(client, settings=settings, method="getUpdates", payload=payload)
    result = resp.get("result")
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    return []


async def _tg_send_text(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
    text: str,
) -> None:
    for chunk in _split_telegram_message(text):
        await _tg_call(
            client,
            settings=settings,
            method="sendMessage",
            payload={
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            },
        )


async def _tg_edit_text(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
    message_id: int,
    text: str,
) -> None:
    """Edit a Telegram message in-place (best effort)."""
    chunk = _split_telegram_message(text)[0]
    await _tg_call(
        client,
        settings=settings,
        method="editMessageText",
        payload={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": chunk,
            "disable_web_page_preview": True,
        },
    )


async def _tg_send_processing_stub(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
) -> int | None:
    """Send initial status message and return Telegram message id."""
    resp = await _tg_call(
        client,
        settings=settings,
        method="sendMessage",
        payload={
            "chat_id": chat_id,
            "text": "Processing your request...",
            "disable_web_page_preview": True,
        },
    )
    message_id_val = resp.get("message_id")
    if isinstance(message_id_val, int):
        return message_id_val
    return None


def _headers(settings: TelegramChannelSettings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.auth_user_id}"}


def _health_payload(settings: TelegramChannelSettings) -> dict[str, str]:
    """Return static health payload for adapter HTTP probes."""
    return {
        "status": "ok",
        "service": "kragen-telegram-channel",
        "mode": settings.mode,
    }


def _looks_like_storage_check_query(text: str) -> bool:
    """Return True when user asks to check MinIO/S3 storage status."""
    lowered = text.lower()
    markers = (
        "minio",
        "s3",
        "object storage",
        "storage",
        "store",
        "bucket",
        "сторедж",
        "сторейдж",
        "сторадж",
        "хранилищ",
        "минио",
        "бакет",
        "записать",
    )
    return any(marker in lowered for marker in markers)


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
        if is_stale_telegram_update(
            last_update_id=binding.last_update_id,
            incoming_update_id=update_id,
        ):
            await db.rollback()
            return
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
    text = message.get("text")
    if not isinstance(text, str):
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

    command = text.strip().lower()
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
                    "Commands:\n"
                    "/new - start a new session\n"
                    "Send any text to run it via Kragen worker."
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
    try:
        async with httpx.AsyncClient() as tg_client, httpx.AsyncClient() as kragen_client:
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
                        await _handle_update(
                            tg_client,
                            kragen_client,
                            settings=settings,
                            update=update,
                        )
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
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


async def _tg_set_webhook(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
) -> None:
    """Register webhook URL in Telegram Bot API when configured."""
    if not settings.webhook_public_url:
        raise RuntimeError("KRAGEN_TELEGRAM_WEBHOOK_PUBLIC_URL is required in webhook mode")
    normalized_path = settings.webhook_path
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path
    await _tg_call(
        client,
        settings=settings,
        method="setWebhook",
        payload={
            "url": settings.webhook_public_url.rstrip("/") + normalized_path,
            **(
                {"secret_token": settings.webhook_secret_token}
                if settings.webhook_secret_token
                else {}
            ),
        },
    )


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
                await _handle_update(
                    tg_client,
                    kragen_client,
                    settings=settings,
                    update=update,
                )
            except Exception:
                logger.exception("telegram_webhook_update_failed")
            finally:
                queue.task_done()


async def _dedup_cleanup_worker(*, settings: TelegramChannelSettings) -> None:
    """Periodically purge old telegram_processed_messages rows."""
    interval = max(60, settings.dedup_cleanup_interval_seconds)
    retention = max(1, settings.dedup_retention_hours)
    while True:
        try:
            async with async_session_factory() as db:
                deleted = await cleanup_processed_messages(
                    db,
                    older_than_hours=retention,
                )
                await db.commit()
                if deleted > 0:
                    logger.info(
                        "telegram_dedup_cleanup",
                        deleted=deleted,
                        retention_hours=retention,
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
