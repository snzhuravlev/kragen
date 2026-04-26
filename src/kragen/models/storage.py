"""File tree ORM models backed by object storage blobs."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kragen.models.base import Base


class StorageEntry(Base):
    """A workspace-scoped file or folder in the logical file tree."""

    __tablename__ = "storage_entries"
    __table_args__ = (
        CheckConstraint("kind in ('file', 'folder')", name="ck_storage_entries_kind"),
        Index("ix_storage_entries_workspace_parent", "workspace_id", "parent_id"),
        Index("ix_storage_entries_workspace_path", "workspace_id", "path_cache"),
        Index("ix_storage_entries_content_hash", "content_hash"),
        Index(
            "uq_storage_entries_child_name_active",
            "workspace_id",
            "parent_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND parent_id IS NOT NULL"),
        ),
        Index(
            "uq_storage_entries_root_name_active",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND parent_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("workspaces.id"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("storage_entries.id", ondelete="CASCADE"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(512))
    path_cache: Mapped[str] = mapped_column(String(4096))
    object_key: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_type: Mapped[str] = mapped_column(String(64), default="api")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    parent: Mapped["StorageEntry | None"] = relationship(
        remote_side=[id],
        back_populates="children",
    )
    children: Mapped[list["StorageEntry"]] = relationship(back_populates="parent")
