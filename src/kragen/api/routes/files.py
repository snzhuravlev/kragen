"""File upload and document/artifact access."""

import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sqlalchemy import select

from kragen.api.deps import CorrelationId, DbSession, UserId, ensure_workspace_access
from kragen.api.schemas import ArtifactOut, DocumentOut
from kragen.models.core import Artifact
from kragen.models.memory import Document
from kragen.services.audit_service import write_audit
from kragen.storage import object_store

router = APIRouter(tags=["files"])


@router.post("/files/upload", response_model=DocumentOut)
async def upload_file(
    db: DbSession,
    user_id: UserId,
    correlation_id: CorrelationId,
    workspace_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
) -> Document:
    """Store raw bytes in object storage and register a document row (ingestion follows async)."""
    await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)

    data = await file.read()
    digest = object_store.sha256_hex(data)
    key = f"workspaces/{workspace_id}/documents/{digest}"
    uri = await object_store.put_bytes(
        key=key,
        body=data,
        content_type=file.content_type or "application/octet-stream",
    )

    doc = Document(
        workspace_id=workspace_id,
        user_id=user_id,
        source_type="upload",
        source_ref=uri,
        title=file.filename,
        mime_type=file.content_type,
        metadata_={"filename": file.filename},
        content_hash=digest,
    )
    db.add(doc)
    await db.flush()
    await write_audit(
        db,
        event_type="document.uploaded",
        payload={"document_id": str(doc.id), "uri": uri},
        workspace_id=workspace_id,
        actor_user_id=user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(doc)
    return doc


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
