"""File upload, logical storage tree, and document/artifact access."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from kragen.api.deps import (
    CorrelationId,
    DbSession,
    FileTaskAuth,
    FileTaskAuthDep,
    UserId,
    ensure_workspace_access,
)
from kragen.api.schemas import (
    ArtifactOut,
    DocumentOut,
    StorageEntryOut,
    StorageEntryUpdate,
    StorageFileImport,
    StorageFolderCreate,
    StorageFolderEnsure,
)
from kragen.config import get_settings
from kragen.models.core import Artifact
from kragen.models.memory import Document
from kragen.services import file_storage
from kragen.services.audit_service import write_audit
from kragen.services.url_import import UrlImportError, fetch_url_bytes
from kragen.storage import object_store

router = APIRouter(tags=["files"])


def _normalize_dest_folder_path(raw: str) -> str:
    """Return a normalized absolute logical folder path (e.g. /library/docs)."""
    s = raw.strip().replace("\\", "/")
    if not s.startswith("/"):
        s = "/" + s
    s = s.rstrip("/")
    return s if s else "/"


def _storage_http_error(exc: file_storage.FileStorageError) -> HTTPException:
    if isinstance(exc, file_storage.StorageEntryNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, file_storage.StorageEntryConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, file_storage.InvalidStorageMove):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, file_storage.InvalidStorageName):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/files", response_model=list[StorageEntryOut])
async def list_storage_entries(
    db: DbSession,
    user_id: UserId,
    workspace_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
) -> list[StorageEntryOut]:
    """List direct children of a folder (root when parent_id is omitted)."""
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)
    rows = await file_storage.list_entries(
        db,
        workspace_id=workspace_id,
        parent_id=parent_id,
    )
    return rows


def _reject_import_only_task(task_auth: FileTaskAuth, *, detail: str) -> None:
    if task_auth.task_workspace_id is not None and not task_auth.can_write_file_tree:
        raise HTTPException(status_code=403, detail=detail)


@router.post("/files/folders", response_model=StorageEntryOut)
async def create_storage_folder(
    db: DbSession,
    task_auth: FileTaskAuthDep,
    correlation_id: CorrelationId,
    body: StorageFolderCreate,
) -> StorageEntryOut:
    """Create one folder in the logical file tree."""
    if task_auth.task_workspace_id is not None and task_auth.task_workspace_id != body.workspace_id:
        raise HTTPException(
            status_code=403,
            detail="This token is bound to a different workspace",
        )
    _reject_import_only_task(
        task_auth,
        detail="This token is limited to file import; use a files:task token for folder creation",
    )
    await ensure_workspace_access(db, user_id=task_auth.user_id, workspace_id=body.workspace_id)
    try:
        folder = await file_storage.create_folder(
            db,
            workspace_id=body.workspace_id,
            parent_id=body.parent_id,
            name=body.name,
            created_by_user_id=task_auth.user_id,
            source_type="api",
        )
    except file_storage.FileStorageError as exc:
        raise _storage_http_error(exc) from exc
    await write_audit(
        db,
        event_type="storage.folder_created",
        payload={"entry_id": str(folder.id), "path": folder.path_cache},
        workspace_id=body.workspace_id,
        actor_user_id=task_auth.user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(folder)
    return folder


@router.post("/files/folders/ensure", response_model=StorageEntryOut)
async def ensure_storage_folders(
    db: DbSession,
    task_auth: FileTaskAuthDep,
    correlation_id: CorrelationId,
    body: StorageFolderEnsure,
) -> StorageEntryOut:
    """Create all missing path segments and return the deepest folder (mkdir -p semantics)."""
    if task_auth.task_workspace_id is not None and task_auth.task_workspace_id != body.workspace_id:
        raise HTTPException(
            status_code=403,
            detail="This token is bound to a different workspace",
        )
    _reject_import_only_task(
        task_auth,
        detail="This token is limited to file import; use a files:task token for folder ensure",
    )
    await ensure_workspace_access(db, user_id=task_auth.user_id, workspace_id=body.workspace_id)
    path = _normalize_dest_folder_path(body.path)
    if path in ("/", ""):
        raise HTTPException(status_code=400, detail="path must be a non-root folder")
    try:
        folder = await file_storage.ensure_folder_path(
            db,
            workspace_id=body.workspace_id,
            path=path,
            created_by_user_id=task_auth.user_id,
            source_type="api_ensure",
        )
    except file_storage.FileStorageError as exc:
        raise _storage_http_error(exc) from exc
    if folder is None:
        raise HTTPException(status_code=400, detail="Could not create folder path")
    await write_audit(
        db,
        event_type="storage.folder_ensured",
        payload={"entry_id": str(folder.id), "path": folder.path_cache},
        workspace_id=body.workspace_id,
        actor_user_id=task_auth.user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(folder)
    return folder


@router.post("/files/upload", response_model=StorageEntryOut)
async def upload_file(
    db: DbSession,
    task_auth: FileTaskAuthDep,
    correlation_id: CorrelationId,
    workspace_id: uuid.UUID = Form(...),
    parent_id: Annotated[uuid.UUID | None, Form()] = None,
    create_document: bool = Form(True),
    file: UploadFile = File(...),
) -> StorageEntryOut:
    """Upload bytes to object storage and register a storage_entries row (optional Document)."""
    if task_auth.task_workspace_id is not None and task_auth.task_workspace_id != workspace_id:
        raise HTTPException(
            status_code=403,
            detail="This token is bound to a different workspace",
        )
    _reject_import_only_task(
        task_auth,
        detail="This token is limited to file import; use a files:task token for upload",
    )
    await ensure_workspace_access(db, user_id=task_auth.user_id, workspace_id=workspace_id)

    data = await file.read()
    raw_name = file.filename or "upload.bin"
    name = Path(raw_name).name or "upload.bin"
    try:
        entry, _document = await file_storage.create_file_from_bytes(
            db,
            workspace_id=workspace_id,
            parent_id=parent_id,
            name=name,
            body=data,
            mime_type=file.content_type,
            created_by_user_id=task_auth.user_id,
            source_type="upload",
            metadata={"filename": raw_name},
            create_document=create_document,
        )
    except file_storage.FileStorageError as exc:
        raise _storage_http_error(exc) from exc

    await write_audit(
        db,
        event_type="storage.file_uploaded",
        payload={"entry_id": str(entry.id), "uri": entry.uri, "path": entry.path_cache},
        workspace_id=workspace_id,
        actor_user_id=task_auth.user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(entry)
    return entry


@router.post("/files/import", response_model=StorageEntryOut)
async def import_file_from_url(
    body: StorageFileImport,
    db: DbSession,
    task_auth: FileTaskAuthDep,
    correlation_id: CorrelationId,
) -> StorageEntryOut:
    """Download a file from a URL (server-side) and store it under a logical path."""
    if task_auth.task_workspace_id is not None and task_auth.task_workspace_id != body.workspace_id:
        raise HTTPException(
            status_code=403,
            detail="This token is bound to a different workspace",
        )
    await ensure_workspace_access(
        db, user_id=task_auth.user_id, workspace_id=body.workspace_id
    )
    try:
        fetched = await fetch_url_bytes(
            body.url, settings=get_settings().file_import
        )
    except UrlImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.parent_id is not None:
        # Mode B: known parent folder + file name
        name_raw = (body.file_name or "").strip()
        if not name_raw:
            raise HTTPException(status_code=400, detail="file_name is required for parent_id mode")
        name = Path(name_raw).name
        if not name:
            name = "download.bin"
        try:
            parent = await file_storage.get_entry(
                db, workspace_id=body.workspace_id, entry_id=body.parent_id
            )
        except file_storage.FileStorageError as exc:
            raise _storage_http_error(exc) from exc
        if parent.kind != "folder":
            raise HTTPException(status_code=400, detail="parent_id must refer to a folder")
        parent_id: uuid.UUID | None = body.parent_id
    else:
        # Mode A: dest_folder_path + optional filename
        assert body.dest_folder_path is not None
        dest = _normalize_dest_folder_path(body.dest_folder_path)
        name_raw = (body.filename or fetched.filename_hint or "download.bin").strip()
        name = Path(name_raw).name
        if not name:
            name = "download.bin"
        if dest in ("/", ""):
            parent_id = None
        else:
            folder = await file_storage.ensure_folder_path(
                db,
                workspace_id=body.workspace_id,
                path=dest,
                created_by_user_id=task_auth.user_id,
                source_type="import_url",
            )
            if folder is None:
                raise HTTPException(
                    status_code=400, detail="Could not ensure destination folder"
                )
            parent_id = folder.id

    try:
        entry, _doc = await file_storage.create_file_from_bytes(
            db,
            workspace_id=body.workspace_id,
            parent_id=parent_id,
            name=name,
            body=fetched.body,
            mime_type=fetched.content_type,
            created_by_user_id=task_auth.user_id,
            source_type="import_url",
            metadata={"source_url": body.url},
            create_document=body.create_document,
        )
    except file_storage.FileStorageError as exc:
        raise _storage_http_error(exc) from exc

    await write_audit(
        db,
        event_type="storage.file_imported",
        payload={
            "entry_id": str(entry.id),
            "source_url": body.url,
            "path": entry.path_cache,
        },
        workspace_id=body.workspace_id,
        actor_user_id=task_auth.user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(entry)
    return entry


@router.get("/files/{entry_id}", response_model=StorageEntryOut)
async def get_storage_entry(
    entry_id: uuid.UUID,
    db: DbSession,
    user_id: UserId,
    workspace_id: uuid.UUID,
) -> StorageEntryOut:
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)
    try:
        row = await file_storage.get_entry(db, workspace_id=workspace_id, entry_id=entry_id)
    except file_storage.FileStorageError as exc:
        raise _storage_http_error(exc) from exc
    return row


@router.patch("/files/{entry_id}", response_model=StorageEntryOut)
async def update_storage_entry(
    entry_id: uuid.UUID,
    db: DbSession,
    user_id: UserId,
    correlation_id: CorrelationId,
    workspace_id: uuid.UUID,
    body: StorageEntryUpdate,
) -> StorageEntryOut:
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)
    try:
        current = await file_storage.get_entry(db, workspace_id=workspace_id, entry_id=entry_id)
        name_kw = body.name if "name" in body.model_fields_set else None
        parent_kw = (
            body.parent_id if "parent_id" in body.model_fields_set else current.parent_id
        )
        updated = await file_storage.update_entry(
            db,
            workspace_id=workspace_id,
            entry_id=entry_id,
            name=name_kw,
            parent_id=parent_kw,
        )
    except file_storage.FileStorageError as exc:
        raise _storage_http_error(exc) from exc
    await write_audit(
        db,
        event_type="storage.entry_updated",
        payload={"entry_id": str(updated.id), "path": updated.path_cache},
        workspace_id=workspace_id,
        actor_user_id=user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(updated)
    return updated


@router.delete("/files/{entry_id}", response_model=StorageEntryOut)
async def delete_storage_entry(
    entry_id: uuid.UUID,
    db: DbSession,
    user_id: UserId,
    correlation_id: CorrelationId,
    workspace_id: uuid.UUID,
) -> StorageEntryOut:
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)
    try:
        deleted = await file_storage.soft_delete_entry(
            db,
            workspace_id=workspace_id,
            entry_id=entry_id,
        )
    except file_storage.FileStorageError as exc:
        raise _storage_http_error(exc) from exc
    await write_audit(
        db,
        event_type="storage.entry_deleted",
        payload={"entry_id": str(deleted.id), "path": deleted.path_cache},
        workspace_id=workspace_id,
        actor_user_id=user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(deleted)
    return deleted


@router.get("/files/{entry_id}/download")
async def download_storage_file(
    entry_id: uuid.UUID,
    db: DbSession,
    user_id: UserId,
    workspace_id: uuid.UUID,
) -> Response:
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)
    try:
        entry = await file_storage.get_entry(db, workspace_id=workspace_id, entry_id=entry_id)
    except file_storage.FileStorageError as exc:
        raise _storage_http_error(exc) from exc
    if entry.kind != "file":
        raise HTTPException(status_code=400, detail="Entry is not a file")
    if not entry.object_key:
        raise HTTPException(status_code=404, detail="File has no object key")
    blob = await object_store.get_bytes(key=entry.object_key)
    filename = entry.name.replace('"', "_")
    return Response(
        content=blob,
        media_type=entry.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/artifacts/{artifact_id}", response_model=ArtifactOut)
async def get_artifact(artifact_id: uuid.UUID, db: DbSession, user_id: UserId) -> Artifact:
    """Return artifact metadata (download URL to be signed in production)."""
    result = await db.execute(select(Artifact).where(Artifact.id == artifact_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    await ensure_workspace_access(db, user_id=user_id, workspace_id=row.workspace_id)
    return row


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(document_id: uuid.UUID, db: DbSession, user_id: UserId) -> Document:
    """Return document metadata."""
    result = await db.execute(select(Document).where(Document.id == document_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    await ensure_workspace_access(db, user_id=user_id, workspace_id=row.workspace_id)
    return row
