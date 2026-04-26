"""Unit tests for Telegram binding deduplication helpers."""

from __future__ import annotations

import uuid

import pytest

from kragen.models.core import TelegramBinding
from kragen.services.telegram_bindings import mark_update_processed, is_stale_telegram_update


class _FakeDb:
    """Tiny async session stub for unit tests."""

    def __init__(self) -> None:
        self.flush_calls = 0

    async def flush(self) -> None:
        self.flush_calls += 1


def _binding(last_update_id: int | None) -> TelegramBinding:
    return TelegramBinding(
        chat_id=12345,
        workspace_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        user_id=None,
        username="tester",
        first_name="Test",
        last_name="User",
        last_update_id=last_update_id,
    )


def test_is_stale_telegram_update() -> None:
    assert is_stale_telegram_update(last_update_id=None, incoming_update_id=10) is False
    assert is_stale_telegram_update(last_update_id=10, incoming_update_id=10) is True
    assert is_stale_telegram_update(last_update_id=10, incoming_update_id=9) is True
    assert is_stale_telegram_update(last_update_id=10, incoming_update_id=11) is False
    assert is_stale_telegram_update(last_update_id=200, incoming_update_id=150) is True
    assert (
        is_stale_telegram_update(last_update_id=209_999_996, incoming_update_id=103_697_736)
        is False
    )


@pytest.mark.asyncio
async def test_mark_update_processed_accepts_new_update() -> None:
    binding = _binding(last_update_id=10)
    db = _FakeDb()

    accepted = await mark_update_processed(db, binding=binding, incoming_update_id=11)

    assert accepted is True
    assert binding.last_update_id == 11
    assert db.flush_calls == 1


@pytest.mark.asyncio
async def test_mark_update_processed_rejects_duplicate_update() -> None:
    binding = _binding(last_update_id=10)
    db = _FakeDb()

    accepted = await mark_update_processed(db, binding=binding, incoming_update_id=10)

    assert accepted is False
    assert binding.last_update_id == 10
    assert db.flush_calls == 0
