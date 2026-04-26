"""Unit tests for workspace-scoped session access."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from kragen.api.routes import messages, sessions
from kragen.api.schemas import MessageCreate
from kragen.models.core import Message, Session


class _ScalarResult:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object | None:
        return self._row


class _ScalarsResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _ListResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarsResult:
        return _ScalarsResult(self._rows)


class _Db:
    def __init__(self, rows: list[object] | None = None) -> None:
        self.rows = rows or []
        self.execute_calls = 0
        self.added: list[object] = []
        self.flush_calls = 0
        self.commit_calls = 0
        self.refresh_calls = 0

    async def execute(self, _stmt: object) -> _ScalarResult | _ListResult:
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _ScalarResult(self.rows[0] if self.rows else None)
        return _ListResult(self.rows[1:])

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flush_calls += 1
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()
            if getattr(row, "created_at", None) is None:
                row.created_at = datetime.now(UTC)
            if getattr(row, "updated_at", None) is None:
                row.updated_at = datetime.now(UTC)

    async def commit(self) -> None:
        self.commit_calls += 1

    async def refresh(self, _row: object) -> None:
        self.refresh_calls += 1


@pytest.mark.asyncio
async def test_list_sessions_uses_workspace_access_when_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    session = Session(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        user_id=uuid.uuid4(),
        title="Telegram chat",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def fake_ensure(_db: object, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        calls.append((user_id, workspace_id))

    class _DbList(_Db):
        async def execute(self, _stmt: object) -> _ListResult:
            return _ListResult([session])

    monkeypatch.setattr(sessions, "ensure_workspace_access", fake_ensure)

    returned = await sessions.list_sessions(_DbList(), user_id, workspace_id, 100)

    assert returned == [session]
    assert calls == [(user_id, workspace_id)]


@pytest.mark.asyncio
async def test_list_messages_allows_workspace_member_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    session = Session(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        user_id=uuid.uuid4(),
        title="Telegram chat",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    message = Message(
        id=uuid.uuid4(),
        session_id=session.id,
        role="user",
        content="hello",
        created_at=datetime.now(UTC),
    )
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def fake_ensure(_db: object, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        calls.append((user_id, workspace_id))

    monkeypatch.setattr(sessions, "ensure_workspace_access", fake_ensure)

    returned = await sessions.list_messages(session.id, _Db([session, message]), user_id)

    assert returned == [message]
    assert calls == [(user_id, workspace_id)]


@pytest.mark.asyncio
async def test_post_message_allows_workspace_member_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    session = Session(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        user_id=uuid.uuid4(),
        title="Telegram chat",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def fake_ensure(_db: object, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        calls.append((user_id, workspace_id))

    async def fake_write_audit(*_args: object, **_kwargs: object) -> None:
        return None

    async def fake_schedule_task(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(messages, "ensure_workspace_access", fake_ensure)
    monkeypatch.setattr(messages, "write_audit", fake_write_audit)
    monkeypatch.setattr(messages.orchestrator, "schedule_task", fake_schedule_task)
    monkeypatch.setattr(messages.task_stream, "register_task", lambda _task_id: None)

    db = _Db([session])
    returned = await messages.post_message(
        session.id,
        MessageCreate(role="user", content="hello"),
        db,
        user_id,
        "cid",
    )

    assert returned.message.content == "hello"
    assert calls == [(user_id, workspace_id)]
    assert db.commit_calls == 1
