"""Telegram Bot API client helpers."""

from __future__ import annotations

from typing import Any

import httpx

from kragen.channels.telegram_settings import TelegramChannelSettings
from kragen.channels.telegram_utils import split_telegram_message


async def tg_call(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Call Telegram Bot API and return decoded payload."""
    if method == "getUpdates":
        timeout = httpx.Timeout(
            connect=5.0,
            read=max(10.0, float(settings.poll_timeout_seconds) + 5.0),
            write=10.0,
            pool=5.0,
        )
    else:
        timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)

    response = await client.post(
        f"{settings.telegram_api_base}/{method}",
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {data}")
    result = data.get("result")
    if isinstance(result, dict):
        return result
    return {"result": result}


async def tg_get_updates(
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
    resp = await tg_call(client, settings=settings, method="getUpdates", payload=payload)
    result = resp.get("result")
    if isinstance(result, list):
        return [x for x in result if isinstance(x, dict)]
    return []


async def tg_send_text(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
) -> None:
    for chunk in split_telegram_message(text):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        await tg_call(
            client,
            settings=settings,
            method="sendMessage",
            payload=payload,
        )


async def tg_edit_text(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
    message_id: int,
    text: str,
) -> None:
    """Edit a Telegram message in-place (best effort)."""
    chunk = split_telegram_message(text)[0]
    await tg_call(
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


async def tg_send_processing_stub(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    chat_id: int,
) -> int | None:
    """Send initial status message and return Telegram message id."""
    resp = await tg_call(
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


async def tg_set_commands(
    client: httpx.AsyncClient,
    *,
    settings: TelegramChannelSettings,
    commands: list[dict[str, str]],
) -> None:
    """Register slash commands shown by Telegram clients."""
    await tg_call(
        client,
        settings=settings,
        method="setMyCommands",
        payload={"commands": commands},
    )


async def tg_set_webhook(
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
    await tg_call(
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
