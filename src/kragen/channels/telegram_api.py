"""Backward-compatible re-export."""

from kragen.channels.telegram.api_client import (
    tg_call,
    tg_edit_text,
    tg_get_updates,
    tg_send_processing_stub,
    tg_send_text,
    tg_set_webhook,
)

__all__ = [
    "tg_call",
    "tg_edit_text",
    "tg_get_updates",
    "tg_send_processing_stub",
    "tg_send_text",
    "tg_set_webhook",
]
