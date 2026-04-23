"""Add telegram_processed_messages idempotency table."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003_telegram_processed_messages"
down_revision: Union[str, None] = "002_telegram_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_processed_messages",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="processing"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "uq_telegram_processed_messages_chat_message",
        "telegram_processed_messages",
        ["chat_id", "message_id"],
        unique=True,
    )
    op.create_index(
        "ix_telegram_processed_messages_status",
        "telegram_processed_messages",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_telegram_processed_messages_status", table_name="telegram_processed_messages")
    op.drop_index(
        "uq_telegram_processed_messages_chat_message",
        table_name="telegram_processed_messages",
    )
    op.drop_table("telegram_processed_messages")
