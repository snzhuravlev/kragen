"""Add logical file tree storage entries."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "004_storage_entries"
down_revision: Union[str, None] = "003_telegram_processed_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "storage_entries",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_id",
            sa.Uuid(),
            sa.ForeignKey("storage_entries.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("path_cache", sa.String(4096), nullable=False),
        sa.Column("object_key", sa.String(2048), nullable=True),
        sa.Column("uri", sa.String(2048), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("mime_type", sa.String(256), nullable=True),
        sa.Column("content_hash", sa.String(128), nullable=True),
        sa.Column("source_type", sa.String(64), nullable=False, server_default="api"),
        sa.Column(
            "created_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("metadata", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind in ('file', 'folder')", name="ck_storage_entries_kind"),
    )
    op.create_index(
        "ix_storage_entries_workspace_parent",
        "storage_entries",
        ["workspace_id", "parent_id"],
    )
    op.create_index(
        "ix_storage_entries_workspace_path",
        "storage_entries",
        ["workspace_id", "path_cache"],
    )
    op.create_index("ix_storage_entries_content_hash", "storage_entries", ["content_hash"])
    op.create_index(
        "uq_storage_entries_child_name_active",
        "storage_entries",
        ["workspace_id", "parent_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND parent_id IS NOT NULL"),
    )
    op.create_index(
        "uq_storage_entries_root_name_active",
        "storage_entries",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND parent_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_storage_entries_root_name_active", table_name="storage_entries")
    op.drop_index("uq_storage_entries_child_name_active", table_name="storage_entries")
    op.drop_index("ix_storage_entries_content_hash", table_name="storage_entries")
    op.drop_index("ix_storage_entries_workspace_path", table_name="storage_entries")
    op.drop_index("ix_storage_entries_workspace_parent", table_name="storage_entries")
    op.drop_table("storage_entries")
