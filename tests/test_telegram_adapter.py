"""Unit tests for Telegram adapter utility helpers."""

from __future__ import annotations

import uuid

from kragen.channels.telegram_adapter import (
    TelegramChannelSettings,
    _health_payload,
    _is_valid_webhook_secret,
    _split_telegram_message,
)


def test_split_telegram_message_short_text() -> None:
    chunks = _split_telegram_message("hello", max_len=10)
    assert chunks == ["hello"]


def test_split_telegram_message_long_text() -> None:
    text = "A" * 30
    chunks = _split_telegram_message(text, max_len=10)
    assert chunks == ["A" * 10, "A" * 10, "A" * 10]


def test_split_telegram_message_preserves_words_when_possible() -> None:
    text = "one two three four five six"
    chunks = _split_telegram_message(text, max_len=12)
    assert len(chunks) >= 2
    assert all(len(chunk) <= 12 for chunk in chunks)
    assert chunks[0] == "one two"


def test_is_valid_webhook_secret() -> None:
    assert _is_valid_webhook_secret(configured_secret=None, received_secret=None) is True
    assert _is_valid_webhook_secret(configured_secret="", received_secret="x") is True
    assert _is_valid_webhook_secret(configured_secret="abc", received_secret="abc") is True
    assert _is_valid_webhook_secret(configured_secret="abc", received_secret="def") is False


def test_health_payload() -> None:
    settings = TelegramChannelSettings(
        bot_token="token",
        kragen_api_base_url="http://127.0.0.1:8000",
        auth_user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        default_workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        mode="webhook",
    )
    payload = _health_payload(settings)
    assert payload["status"] == "ok"
    assert payload["service"] == "kragen-telegram-channel"
    assert payload["mode"] == "webhook"
