"""Backward-compatible re-export."""

from kragen.channels.telegram.utils import (
    TELEGRAM_MESSAGE_MAX,
    extract_message_payload,
    headers,
    health_payload,
    looks_like_storage_check_query,
    safe_filename,
    split_telegram_message,
)

__all__ = [
    "TELEGRAM_MESSAGE_MAX",
    "extract_message_payload",
    "headers",
    "health_payload",
    "looks_like_storage_check_query",
    "safe_filename",
    "split_telegram_message",
]
