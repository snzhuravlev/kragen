"""Unit tests for file route RBAC checks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from kragen.api.routes import files
from kragen.api.schemas import StorageEntryUpdate, StorageFolderCreate
from kragen.models.core import Artifact
from kragen.models.memory import Document
from kragen.models.storage import StorageEntry


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
    monkeypatch.setattr(files.file_storage.object_store, "sha256_hex", lambda _data: "digest")
    monkeypatch.setattr(files.file_storage.object_store, "put_bytes", fake_put_bytes)
    monkeypatch.setattr(files, "write_audit", fake_write_audit)

    db = _Db()
    returned = await files.upload_file(db, user_id, "cid", workspace_id, None, True, _Upload())

    assert returned in db.added
    assert events[0].startswith("rbac:")
    assert events[1:] == ["storage", "audit"]
    assert db.flush_calls == 1
    assert db.commit_calls == 1
    assert db.refresh_calls == 1


@pytest.mark.asyncio
async def test_list_files_checks_workspace_before_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    events: list[str] = []
    entry = StorageEntry(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        kind="folder",
        name="Inbox",
        path_cache="/Inbox",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    async def fake_ensure(_db: object, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        events.append(f"rbac:{user_id}:{workspace_id}")

    async def fake_list(*_args: object, **_kwargs: object) -> list[StorageEntry]:
        events.append("list")
        return [entry]

    monkeypatch.setattr(files, "ensure_workspace_access", fake_ensure)
    monkeypatch.setattr(files.file_storage, "list_entries", fake_list)

    returned = await files.list_files(_Db(), user_id, workspace_id, None)

    assert returned == [entry]
    assert events[0].startswith("rbac:")
    assert events[1:] == ["list"]


@pytest.mark.asyncio
async def test_create_folder_checks_workspace_and_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    events: list[str] = []
    entry = StorageEntry(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        kind="folder",
        name="Docs",
        path_cache="/Docs",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    async def fake_ensure(_db: object, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        events.append(f"rbac:{user_id}:{workspace_id}")

    async def fake_create_folder(*_args: object, **_kwargs: object) -> StorageEntry:
        events.append("create")
        return entry

    async def fake_write_audit(*_args: object, **_kwargs: object) -> None:
        events.append("audit")

    monkeypatch.setattr(files, "ensure_workspace_access", fake_ensure)
    monkeypatch.setattr(files.file_storage, "create_folder", fake_create_folder)
    monkeypatch.setattr(files, "write_audit", fake_write_audit)

    db = _Db()
    returned = await files.create_folder(
        StorageFolderCreate(workspace_id=workspace_id, name="Docs"),
        db,
        user_id,
        "cid",
    )

    assert returned is entry
    assert events[0].startswith("rbac:")
    assert events[1:] == ["create", "audit"]
    assert db.commit_calls == 1
    assert db.refresh_calls == 1


@pytest.mark.asyncio
async def test_update_and_delete_check_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    entry_id = uuid.uuid4()
    events: list[str] = []
    entry = StorageEntry(
        id=entry_id,
        workspace_id=workspace_id,
        kind="file",
        name="old.txt",
        path_cache="/old.txt",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    async def fake_ensure(_db: object, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        events.append(f"rbac:{user_id}:{workspace_id}")

    async def fake_get(*_args: object, **_kwargs: object) -> StorageEntry:
        events.append("get")
        return entry

    async def fake_update(*_args: object, **_kwargs: object) -> StorageEntry:
        events.append("update")
        return entry

    async def fake_delete(*_args: object, **_kwargs: object) -> StorageEntry:
        events.append("delete")
        return entry

    async def fake_write_audit(*_args: object, **_kwargs: object) -> None:
        events.append("audit")

    monkeypatch.setattr(files, "ensure_workspace_access", fake_ensure)
    monkeypatch.setattr(files.file_storage, "get_entry", fake_get)
    monkeypatch.setattr(files.file_storage, "update_entry", fake_update)
    monkeypatch.setattr(files.file_storage, "soft_delete_entry", fake_delete)
    monkeypatch.setattr(files, "write_audit", fake_write_audit)

    db = _Db()
    assert (
        await files.update_file_entry(
            entry_id,
            StorageEntryUpdate(name="new.txt"),
            db,
            user_id,
            "cid",
            workspace_id,
        )
        is entry
    )
    assert await files.delete_file_entry(entry_id, db, user_id, "cid", workspace_id) is entry
    assert events[0].startswith("rbac:")
    assert events[1:4] == ["get", "update", "audit"]
    assert events[4].startswith("rbac:")
    assert events[5:] == ["delete", "audit"]
    assert db.commit_calls == 2
    assert db.refresh_calls == 2


@pytest.mark.asyncio
async def test_download_file_checks_workspace_before_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    entry = StorageEntry(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        kind="file",
        name="notes.txt",
        path_cache="/notes.txt",
        object_key="workspaces/ws/files/id/hash",
        mime_type="text/plain",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    events: list[str] = []

    async def fake_ensure(_db: object, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        events.append(f"rbac:{user_id}:{workspace_id}")

    async def fake_get(*_args: object, **_kwargs: object) -> StorageEntry:
        events.append("get")
        return entry

    async def fake_get_bytes(**_kwargs: object) -> bytes:
        events.append("storage")
        return b"hello"

    monkeypatch.setattr(files, "ensure_workspace_access", fake_ensure)
    monkeypatch.setattr(files.file_storage, "get_entry", fake_get)
    monkeypatch.setattr(files.object_store, "get_bytes", fake_get_bytes)

    response = await files.download_file_entry(entry.id, _Db(), user_id, workspace_id)

    assert response.body == b"hello"
    assert events[0].startswith("rbac:")
    assert events[1:] == ["get", "storage"]
