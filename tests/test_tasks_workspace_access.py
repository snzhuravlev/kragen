"""Unit tests for workspace-scoped task access."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from kragen.api.routes import tasks
from kragen.models.core import Session, Task


class _ScalarResult:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object | None:
        return self._row


class _Db:
    def __init__(self, task_row: Task | None, session_row: Session | None) -> None:
        self.task_row = task_row
        self.session_row = session_row
        self.execute_calls = 0

    async def execute(self, _stmt: object) -> _ScalarResult:
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _ScalarResult(self.task_row)
        return _ScalarResult(self.session_row)


@pytest.mark.asyncio
async def test_get_task_uses_workspace_access_for_foreign_session_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    sess = Session(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        user_id=uuid.uuid4(),
        title="foreign-owned session",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    task_row = Task(
        id=uuid.uuid4(),
        session_id=sess.id,
        status="queued",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def fake_ensure(_db: object, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        calls.append((user_id, workspace_id))

    monkeypatch.setattr(tasks, "ensure_workspace_access", fake_ensure)

    returned = await tasks.get_task(task_row.id, _Db(task_row, sess), user_id)

    assert returned is task_row
    assert calls == [(user_id, workspace_id)]
