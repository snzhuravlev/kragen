"""Unit tests for task authorization."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from kragen.api.routes import tasks
from kragen.models.core import Task


class _ScalarResult:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object | None:
        return self._row


class _Db:
    def __init__(self, rows: list[object | None]) -> None:
        self._rows = rows
        self._call = 0

    async def execute(self, _stmt: object) -> _ScalarResult:
        row = self._rows[self._call] if self._call < len(self._rows) else None
        self._call += 1
        return _ScalarResult(row)


@pytest.mark.asyncio
async def test_get_authorized_task_rejects_orphan_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = Task(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        status="queued",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db = _Db([task, None])
    user_id = uuid.uuid4()

    with pytest.raises(HTTPException) as exc_info:
        await tasks._get_authorized_task(db, task.id, user_id)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Session not found"
