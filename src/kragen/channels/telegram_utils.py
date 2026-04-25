"""Pure helper functions for the Telegram channel adapter."""

from __future__ import annotations

import re
from typing import Any

from kragen.channels.telegram_settings import TelegramChannelSettings

TELEGRAM_MESSAGE_MAX = 4096


def split_telegram_message(text: str, max_len: int = TELEGRAM_MESSAGE_MAX) -> list[str]:
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


def headers(settings: TelegramChannelSettings) -> dict[str, str]:
    """Return Kragen API auth headers for Telegram-originated requests."""
    return {"Authorization": f"Bearer {settings.api_bearer_token or settings.auth_user_id}"}


def safe_filename(name: str | None) -> str:
    """Return filename safe for object-storage key component."""
    if not name:
        return "file"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    cleaned = cleaned.strip("._")
    return cleaned or "file"


def extract_message_payload(message: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Extract text/caption and optional document payload from Telegram message."""
    text_value = message.get("text")
    text = str(text_value).strip() if isinstance(text_value, str) else None
    if text == "":
        text = None
    if text is None:
        caption_value = message.get("caption")
        if isinstance(caption_value, str):
            caption = caption_value.strip()
            text = caption or None

    document = message.get("document")
    if not isinstance(document, dict):
        return text, None
    return text, document


def health_payload(settings: TelegramChannelSettings) -> dict[str, str]:
    """Return static health payload for adapter HTTP probes."""
    return {
        "status": "ok",
        "service": "kragen-telegram-channel",
        "mode": settings.mode,
    }


def looks_like_storage_check_query(text: str) -> bool:
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
