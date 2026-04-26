"""Shared file tree operations for API and channel adapters."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kragen.models.memory import Document
from kragen.models.storage import StorageEntry
from kragen.storage import object_store


class FileStorageError(Exception):
    """Base error for logical file storage operations."""


class InvalidStorageName(FileStorageError):
    """Raised when an entry name cannot be represented as one path segment."""


class StorageEntryNotFound(FileStorageError):
    """Raised when a requested entry does not exist in the workspace."""


class StorageEntryConflict(FileStorageError):
    """Raised when an operation would duplicate an active sibling name."""


class InvalidStorageMove(FileStorageError):
    """Raised when an entry cannot be moved to the requested parent."""


def validate_entry_name(name: str) -> str:
    """Return a normalized single path segment or raise InvalidStorageName."""
    normalized = name.strip()
    if not normalized or normalized in {".", ".."}:
        raise InvalidStorageName("Entry name must not be empty, '.' or '..'")
    if "/" in normalized or "\x00" in normalized:
        raise InvalidStorageName("Entry name must not contain '/' or NUL")
    return normalized


def _join_path(parent_path: str | None, name: str) -> str:
    if not parent_path or parent_path == "/":
        return f"/{name}"
    return f"{parent_path.rstrip('/')}/{name}"


async def get_entry(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    entry_id: uuid.UUID,
    include_deleted: bool = False,
) -> StorageEntry:
    """Load one entry scoped to a workspace."""
    stmt = select(StorageEntry).where(
        StorageEntry.workspace_id == workspace_id,
        StorageEntry.id == entry_id,
    )
    if not include_deleted:
        stmt = stmt.where(StorageEntry.deleted_at.is_(None))
    result = await db.execute(stmt)
    entry = result.scalar_one_or_none()
    if entry is None:
        raise StorageEntryNotFound("Storage entry not found")
    return entry


async def _get_parent_folder(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    parent_id: uuid.UUID | None,
) -> StorageEntry | None:
    if parent_id is None:
        return None
    parent = await get_entry(db, workspace_id=workspace_id, entry_id=parent_id)
    if parent.kind != "folder":
        raise InvalidStorageMove("Parent entry must be a folder")
    return parent


async def _active_sibling_with_name(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    name: str,
    exclude_id: uuid.UUID | None = None,
) -> StorageEntry | None:
    stmt = select(StorageEntry).where(
        StorageEntry.workspace_id == workspace_id,
        StorageEntry.name == name,
        StorageEntry.deleted_at.is_(None),
    )
    if parent_id is None:
        stmt = stmt.where(StorageEntry.parent_id.is_(None))
    else:
        stmt = stmt.where(StorageEntry.parent_id == parent_id)
    if exclude_id is not None:
        stmt = stmt.where(StorageEntry.id != exclude_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _ensure_available_name(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    name: str,
    exclude_id: uuid.UUID | None = None,
) -> None:
    if await _active_sibling_with_name(
        db,
        workspace_id=workspace_id,
        parent_id=parent_id,
        name=name,
        exclude_id=exclude_id,
    ):
        raise StorageEntryConflict("An active entry with this name already exists")


async def list_entries(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    parent_id: uuid.UUID | None,
) -> list[StorageEntry]:
    """List active direct children in a folder."""
    if parent_id is not None:
        await _get_parent_folder(db, workspace_id=workspace_id, parent_id=parent_id)
    stmt = select(StorageEntry).where(
        StorageEntry.workspace_id == workspace_id,
        StorageEntry.deleted_at.is_(None),
    )
    if parent_id is None:
        stmt = stmt.where(StorageEntry.parent_id.is_(None))
    else:
        stmt = stmt.where(StorageEntry.parent_id == parent_id)
    stmt = stmt.order_by(StorageEntry.kind.desc(), StorageEntry.name.asc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_folder(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    name: str,
    created_by_user_id: uuid.UUID | None,
    source_type: str = "api",
    metadata: dict[str, Any] | None = None,
) -> StorageEntry:
    """Create one folder under parent_id."""
    clean_name = validate_entry_name(name)
    parent = await _get_parent_folder(db, workspace_id=workspace_id, parent_id=parent_id)
    await _ensure_available_name(
        db,
        workspace_id=workspace_id,
        parent_id=parent_id,
        name=clean_name,
    )
    entry = StorageEntry(
        workspace_id=workspace_id,
        parent_id=parent_id,
        kind="folder",
        name=clean_name,
        path_cache=_join_path(parent.path_cache if parent else None, clean_name),
        source_type=source_type,
        created_by_user_id=created_by_user_id,
        metadata_=metadata or {},
    )
    db.add(entry)
    await db.flush()
    return entry


async def ensure_folder_path(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    path: str,
    created_by_user_id: uuid.UUID | None,
    source_type: str = "api",
) -> StorageEntry | None:
    """Create missing folders for an absolute path and return the deepest folder."""
    parts = [validate_entry_name(part) for part in path.strip("/").split("/") if part.strip()]
    parent_id: uuid.UUID | None = None
    current: StorageEntry | None = None
    for part in parts:
        existing = await _active_sibling_with_name(
            db,
            workspace_id=workspace_id,
            parent_id=parent_id,
            name=part,
        )
        if existing is not None:
            if existing.kind != "folder":
                raise StorageEntryConflict(f"Path segment is not a folder: {part}")
            current = existing
        else:
            current = await create_folder(
                db,
                workspace_id=workspace_id,
                parent_id=parent_id,
                name=part,
                created_by_user_id=created_by_user_id,
                source_type=source_type,
            )
        parent_id = current.id
    return current


async def create_file_from_bytes(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    name: str,
    body: bytes,
    mime_type: str | None,
    created_by_user_id: uuid.UUID | None,
    source_type: str,
    metadata: dict[str, Any] | None = None,
    create_document: bool = False,
) -> tuple[StorageEntry, Document | None]:
    """Upload bytes to object storage and register a logical file entry."""
    if not body:
        raise InvalidStorageName("File body must not be empty")
    clean_name = validate_entry_name(name)
    parent = await _get_parent_folder(db, workspace_id=workspace_id, parent_id=parent_id)
    await _ensure_available_name(
        db,
        workspace_id=workspace_id,
        parent_id=parent_id,
        name=clean_name,
    )
    entry_id = uuid.uuid4()
    digest = object_store.sha256_hex(body)
    object_key = f"workspaces/{workspace_id}/files/{entry_id}/{digest}"
    uri = await object_store.put_bytes(
        key=object_key,
        body=body,
        content_type=mime_type or "application/octet-stream",
    )
    entry = StorageEntry(
        id=entry_id,
        workspace_id=workspace_id,
        parent_id=parent_id,
        kind="file",
        name=clean_name,
        path_cache=_join_path(parent.path_cache if parent else None, clean_name),
        object_key=object_key,
        uri=uri,
        size_bytes=len(body),
        mime_type=mime_type,
        content_hash=digest,
        source_type=source_type,
        created_by_user_id=created_by_user_id,
        metadata_=metadata or {},
    )
    db.add(entry)
    document: Document | None = None
    if create_document:
        document = Document(
            workspace_id=workspace_id,
            user_id=created_by_user_id,
            source_type=source_type,
            source_ref=uri,
            title=clean_name,
            mime_type=mime_type,
            metadata_={"storage_entry_id": str(entry_id), **(metadata or {})},
            content_hash=digest,
        )
        db.add(document)
    await db.flush()
    return entry, document


async def update_entry(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    entry_id: uuid.UUID,
    name: str | None = None,
    parent_id: uuid.UUID | None = None,
) -> StorageEntry:
    """Rename and/or move an entry, updating cached descendant paths."""
    entry = await get_entry(db, workspace_id=workspace_id, entry_id=entry_id)
    clean_name = validate_entry_name(name) if name is not None else entry.name
    parent = await _get_parent_folder(db, workspace_id=workspace_id, parent_id=parent_id)
    if parent is not None and parent.id == entry.id:
        raise InvalidStorageMove("Entry cannot be moved under itself")
    old_path = entry.path_cache
    if parent is not None and entry.kind == "folder":
        if parent.path_cache == old_path or parent.path_cache.startswith(f"{old_path}/"):
            raise InvalidStorageMove("Folder cannot be moved under its own descendant")
    await _ensure_available_name(
        db,
        workspace_id=workspace_id,
        parent_id=parent_id,
        name=clean_name,
        exclude_id=entry.id,
    )
    new_path = _join_path(parent.path_cache if parent else None, clean_name)
    entry.name = clean_name
    entry.parent_id = parent_id
    entry.path_cache = new_path
    entry.updated_at = datetime.now(UTC)
    if entry.kind == "folder" and new_path != old_path:
        result = await db.execute(
            select(StorageEntry).where(
                StorageEntry.workspace_id == workspace_id,
                StorageEntry.deleted_at.is_(None),
                StorageEntry.path_cache.startswith(f"{old_path}/"),
            )
        )
        for child in result.scalars().all():
            child.path_cache = new_path + child.path_cache[len(old_path) :]
            child.updated_at = datetime.now(UTC)
    await db.flush()
    return entry


async def soft_delete_entry(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    entry_id: uuid.UUID,
) -> StorageEntry:
    """Mark one entry and folder descendants as deleted."""
    entry = await get_entry(db, workspace_id=workspace_id, entry_id=entry_id)
    now = datetime.now(UTC)
    stmt = select(StorageEntry).where(
        StorageEntry.workspace_id == workspace_id,
        StorageEntry.deleted_at.is_(None),
        or_(
            StorageEntry.id == entry.id,
            StorageEntry.path_cache.startswith(f"{entry.path_cache}/"),
        ),
    )
    result = await db.execute(stmt)
    for row in result.scalars().all():
        row.deleted_at = now
        row.updated_at = now
    await db.flush()
    return entry
