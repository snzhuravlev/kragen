"""Pydantic schemas for public HTTP API."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    """Create session request."""

    workspace_id: uuid.UUID
    user_id: uuid.UUID | None = None
    title: str | None = None
    channel_type: str = "rest"


class SessionOut(BaseModel):
    """Session response."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID | None
    title: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WorkspaceOut(BaseModel):
    """Workspace response."""

    id: uuid.UUID
    name: str
    slug: str
    owner_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MessageCreate(BaseModel):
    """Post message to session."""

    role: str = Field(..., pattern="^(user|system)$")
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessageOut(BaseModel):
    """Message row."""

    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskOut(BaseModel):
    """Task status."""

    id: uuid.UUID
    session_id: uuid.UUID
    status: str
    correlation_id: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MessagePostResponse(BaseModel):
    """Returned after posting a user message that triggers a task."""

    message: MessageOut
    task: TaskOut


class DocumentOut(BaseModel):
    """Document metadata."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str | None
    source_type: str
    content_hash: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class StorageEntryOut(BaseModel):
    """Logical file tree entry metadata."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    parent_id: uuid.UUID | None
    kind: str
    name: str
    path_cache: str
    uri: str | None
    size_bytes: int | None
    mime_type: str | None
    content_hash: str | None
    source_type: str
    metadata_: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="metadata_",
        serialization_alias="metadata",
    )
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    model_config = {"from_attributes": True, "populate_by_name": True}


class StorageFolderCreate(BaseModel):
    """Create a folder in the logical file tree."""

    workspace_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    name: str = Field(..., min_length=1, max_length=512)


class StorageEntryUpdate(BaseModel):
    """Rename or move an entry."""

    name: str | None = Field(default=None, min_length=1, max_length=512)
    parent_id: uuid.UUID | None = None


class ArtifactOut(BaseModel):
    """Artifact metadata."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    path: str | None
    uri: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditEventOut(BaseModel):
    """Audit event row."""

    id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    correlation_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RetrievalLogOut(BaseModel):
    """Retrieval log row."""

    id: uuid.UUID
    query: str
    latency_ms: int | None
    created_at: datetime

    model_config = {"from_attributes": True}
