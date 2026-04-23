"""Audit event writer."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kragen.models.core import AuditEvent
from kragen.logging_config import get_logger

logger = get_logger(__name__)


async def write_audit(
    db: AsyncSession,
    *,
    event_type: str,
    payload: dict[str, Any],
    workspace_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> uuid.UUID:
    """Persist an audit row and return its id."""
    row = AuditEvent(
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        payload=payload,
        correlation_id=correlation_id,
    )
    db.add(row)
    await db.flush()
    logger.info(
        "audit_event",
        event_type=event_type,
        audit_id=str(row.id),
        correlation_id=correlation_id,
    )
    return row.id
