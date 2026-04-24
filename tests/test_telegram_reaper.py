"""Unit tests for telegram dedup reaper SQL shape."""

from __future__ import annotations

import pytest

from kragen.services.telegram_bindings import reap_stuck_processing_messages


class _RecordingResult:
    """Stand-in for SQLAlchemy Result used to inspect statement parameters."""

    def __init__(self, row_count: int) -> None:
        self._row_count = row_count

    def fetchall(self) -> list[object]:
        return [object()] * self._row_count


class _RecordingSession:
    """Tiny async session stub that captures compiled SQL and params."""

    def __init__(self, row_count: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.flushed = 0
        self._row_count = row_count

    async def execute(self, statement, params: dict[str, object]) -> _RecordingResult:
        self.calls.append((str(statement), params))
        return _RecordingResult(self._row_count)

    async def flush(self) -> None:
        self.flushed += 1


@pytest.mark.asyncio
async def test_reap_stuck_noop_when_threshold_non_positive() -> None:
    db = _RecordingSession()
    assert await reap_stuck_processing_messages(db, older_than_minutes=0) == 0
    assert db.calls == []


@pytest.mark.asyncio
async def test_reap_stuck_issues_update_with_interval() -> None:
    db = _RecordingSession(row_count=3)
    reaped = await reap_stuck_processing_messages(db, older_than_minutes=15)
    assert reaped == 3
    assert len(db.calls) == 1
    sql, params = db.calls[0]
    assert "UPDATE telegram_processed_messages" in sql
    assert "status = 'failed'" in sql
    assert "status = 'processing'" in sql
    assert "make_interval(mins =>" in sql
    assert params == {"older_than_minutes": 15}
    assert db.flushed == 1
