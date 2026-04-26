"""File upload and document/artifact access."""

import uuid

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from starlette.responses import Response

from kragen.api.deps import CorrelationId, DbSession, UserId, ensure_workspace_access
from kragen.api.schemas import (
    ArtifactOut,
    DocumentOut,
    StorageEntryOut,
    StorageEntryUpdate,
    StorageFolderCreate,
)
from kragen.models.core import Artifact
from kragen.models.memory import Document
from kragen.models.storage import StorageEntry
from kragen.services.audit_service import write_audit
from kragen.services import file_storage
from kragen.storage import object_store

router = APIRouter(tags=["files"])


def _storage_error_to_http(exc: file_storage.FileStorageError) -> HTTPException:
    if isinstance(exc, file_storage.StorageEntryNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, file_storage.StorageEntryConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/files", response_model=list[StorageEntryOut])
async def list_files(
    db: DbSession,
    user_id: UserId,
    workspace_id: uuid.UUID = Query(...),
    parent_id: uuid.UUID | None = Query(default=None),
) -> list[StorageEntry]:
    """List active file-tree entries under a parent folder."""
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)
    try:
        return await file_storage.list_entries(db, workspace_id=workspace_id, parent_id=parent_id)
    except file_storage.FileStorageError as exc:
        raise _storage_error_to_http(exc) from exc


@router.post("/files/folders", response_model=StorageEntryOut)
async def create_folder(
    body: StorageFolderCreate,
    db: DbSession,
    user_id: UserId,
    correlation_id: CorrelationId,
) -> StorageEntry:
    """Create a logical folder."""
    await ensure_workspace_access(db, user_id=user_id, workspace_id=body.workspace_id)
    try:
        entry = await file_storage.create_folder(
            db,
            workspace_id=body.workspace_id,
            parent_id=body.parent_id,
            name=body.name,
            created_by_user_id=user_id,
            source_type="web",
        )
    except file_storage.FileStorageError as exc:
        raise _storage_error_to_http(exc) from exc
    await write_audit(
        db,
        event_type="storage.folder_created",
        payload={"entry_id": str(entry.id), "path": entry.path_cache},
        workspace_id=body.workspace_id,
        actor_user_id=user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(entry)
    return entry


@router.post("/files/upload", response_model=StorageEntryOut)
async def upload_file(
    db: DbSession,
    user_id: UserId,
    correlation_id: CorrelationId,
    workspace_id: uuid.UUID = Form(...),
    parent_id: uuid.UUID | None = Form(default=None),
    create_document: bool = Form(default=True),
    file: UploadFile = File(...),
) -> StorageEntry:
    """Store raw bytes in object storage and register a logical file entry."""
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)

    data = await file.read()
    try:
        entry, doc = await file_storage.create_file_from_bytes(
            db,
            workspace_id=workspace_id,
            parent_id=parent_id,
            name=file.filename or "upload.bin",
            body=data,
            mime_type=file.content_type,
            created_by_user_id=user_id,
            source_type="upload",
            metadata={"filename": file.filename},
            create_document=create_document,
        )
    except file_storage.FileStorageError as exc:
        raise _storage_error_to_http(exc) from exc
    await write_audit(
        db,
        event_type="storage.file_uploaded",
        payload={
            "entry_id": str(entry.id),
            "document_id": str(doc.id) if doc is not None else None,
            "uri": entry.uri,
            "path": entry.path_cache,
        },
        workspace_id=workspace_id,
        actor_user_id=user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(entry)
    return entry


@router.get("/files/{entry_id}", response_model=StorageEntryOut)
async def get_file_entry(
    entry_id: uuid.UUID,
    db: DbSession,
    user_id: UserId,
    workspace_id: uuid.UUID = Query(...),
) -> StorageEntry:
    """Return logical file-tree metadata."""
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)
    try:
        return await file_storage.get_entry(db, workspace_id=workspace_id, entry_id=entry_id)
    except file_storage.FileStorageError as exc:
        raise _storage_error_to_http(exc) from exc


@router.patch("/files/{entry_id}", response_model=StorageEntryOut)
async def update_file_entry(
    entry_id: uuid.UUID,
    body: StorageEntryUpdate,
    db: DbSession,
    user_id: UserId,
    correlation_id: CorrelationId,
    workspace_id: uuid.UUID = Query(...),
) -> StorageEntry:
    """Rename or move a logical file-tree entry."""
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)
    try:
        current = await file_storage.get_entry(db, workspace_id=workspace_id, entry_id=entry_id)
        parent_id = body.parent_id if "parent_id" in body.model_fields_set else current.parent_id
        entry = await file_storage.update_entry(
            db,
            workspace_id=workspace_id,
            entry_id=entry_id,
            name=body.name,
            parent_id=parent_id,
        )
    except file_storage.FileStorageError as exc:
        raise _storage_error_to_http(exc) from exc
    await write_audit(
        db,
        event_type="storage.entry_updated",
        payload={"entry_id": str(entry.id), "path": entry.path_cache},
        workspace_id=workspace_id,
        actor_user_id=user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/files/{entry_id}", response_model=StorageEntryOut)
async def delete_file_entry(
    entry_id: uuid.UUID,
    db: DbSession,
    user_id: UserId,
    correlation_id: CorrelationId,
    workspace_id: uuid.UUID = Query(...),
) -> StorageEntry:
    """Soft-delete a logical file-tree entry."""
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)
    try:
        entry = await file_storage.soft_delete_entry(
            db,
            workspace_id=workspace_id,
            entry_id=entry_id,
        )
    except file_storage.FileStorageError as exc:
        raise _storage_error_to_http(exc) from exc
    await write_audit(
        db,
        event_type="storage.entry_deleted",
        payload={"entry_id": str(entry.id), "path": entry.path_cache},
        workspace_id=workspace_id,
        actor_user_id=user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(entry)
    return entry


@router.get("/files/{entry_id}/download")
async def download_file_entry(
    entry_id: uuid.UUID,
    db: DbSession,
    user_id: UserId,
    workspace_id: uuid.UUID = Query(...),
) -> Response:
    """Download a file entry from object storage."""
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)
    try:
        entry = await file_storage.get_entry(db, workspace_id=workspace_id, entry_id=entry_id)
    except file_storage.FileStorageError as exc:
        raise _storage_error_to_http(exc) from exc
    if entry.kind != "file" or not entry.object_key:
        raise HTTPException(status_code=400, detail="Entry is not a downloadable file")
    data = await object_store.get_bytes(key=entry.object_key)
    headers = {"Content-Disposition": f'attachment; filename="{entry.name}"'}
    return Response(
        content=data,
        media_type=entry.mime_type or "application/octet-stream",
        headers=headers,
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
