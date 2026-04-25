"""Unit tests for file route RBAC checks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from kragen.api.routes import files
from kragen.models.core import Artifact
from kragen.models.memory import Document


class _ScalarResult:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object | None:
        return self._row


class _Db:
    def __init__(self, row: object | None = None) -> None:
        self.row = row
        self.added: list[object] = []
        self.flush_calls = 0
        self.commit_calls = 0
        self.refresh_calls = 0

    async def execute(self, _stmt: object) -> _ScalarResult:
        return _ScalarResult(self.row)

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flush_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1

    async def refresh(self, _row: object) -> None:
        self.refresh_calls += 1


class _Upload:
    filename = "notes.txt"
    content_type = "text/plain"

    async def read(self) -> bytes:
        return b"hello"


@pytest.mark.asyncio
async def test_get_document_checks_workspace_access(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    document = Document(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        user_id=user_id,
        source_type="upload",
        title="doc",
        created_at=datetime.now(UTC),
    )
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def fake_ensure(_db: object, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        calls.append((user_id, workspace_id))

    monkeypatch.setattr(files, "ensure_workspace_access", fake_ensure)

    returned = await files.get_document(document.id, _Db(document), user_id)

    assert returned is document
    assert calls == [(user_id, workspace_id)]


@pytest.mark.asyncio
async def test_get_artifact_checks_workspace_access(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    artifact = Artifact(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        path="out.txt",
        uri="s3://bucket/out.txt",
        created_at=datetime.now(UTC),
    )
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def fake_ensure(_db: object, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        calls.append((user_id, workspace_id))

    monkeypatch.setattr(files, "ensure_workspace_access", fake_ensure)

    returned = await files.get_artifact(artifact.id, _Db(artifact), user_id)

    assert returned is artifact
    assert calls == [(user_id, workspace_id)]


@pytest.mark.asyncio
async def test_upload_file_checks_workspace_before_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    events: list[str] = []

    async def fake_ensure(_db: object, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        events.append(f"rbac:{user_id}:{workspace_id}")

    async def fake_put_bytes(**_kwargs: object) -> str:
        events.append("storage")
        return "s3://bucket/doc"

    async def fake_write_audit(*_args: object, **_kwargs: object) -> None:
        events.append("audit")

    monkeypatch.setattr(files, "ensure_workspace_access", fake_ensure)
    monkeypatch.setattr(files.object_store, "sha256_hex", lambda _data: "digest")
    monkeypatch.setattr(files.object_store, "put_bytes", fake_put_bytes)
    monkeypatch.setattr(files, "write_audit", fake_write_audit)

    db = _Db()
    returned = await files.upload_file(db, user_id, "cid", workspace_id, _Upload())

    assert returned in db.added
    assert events[0].startswith("rbac:")
    assert events[1:] == ["storage", "audit"]
    assert db.flush_calls == 1
    assert db.commit_calls == 1
    assert db.refresh_calls == 1
