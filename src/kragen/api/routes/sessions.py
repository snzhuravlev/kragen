"""Session CRUD endpoints."""

import uuid

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from kragen.api.deps import CorrelationId, DbSession, UserId, ensure_workspace_access
from kragen.api.schemas import MessageOut, SessionCreate, SessionOut
from kragen.config import get_settings
from kragen.models.core import Channel, Session
from kragen.services.audit_service import write_audit

router = APIRouter(prefix="/sessions", tags=["sessions"])
settings = get_settings()


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    db: DbSession,
    user_id: UserId,
    workspace_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SessionOut]:
    """List sessions visible to the current user."""
    stmt = select(Session)
    if workspace_id is not None:
        await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)
        stmt = stmt.where(Session.workspace_id == workspace_id)
    else:
        stmt = stmt.where(Session.user_id == user_id)
    stmt = stmt.order_by(Session.updated_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("", response_model=SessionOut)
async def create_session(
    body: SessionCreate,
    db: DbSession,
    user_id: UserId,
    correlation_id: CorrelationId,
) -> Session:
    """Open a new chat session in a workspace."""
    if body.channel_type == "openclaw" and not settings.channels.openclaw_enabled:
        raise HTTPException(
            status_code=403,
            detail="openclaw channel is disabled in this environment",
        )

    ch = Channel(
        workspace_id=body.workspace_id,
        user_id=user_id,
        type=body.channel_type,
        external_ref=None,
    )
    db.add(ch)
    await db.flush()

    sess = Session(
        workspace_id=body.workspace_id,
        user_id=user_id,
        channel_id=ch.id,
        title=body.title,
    )
    db.add(sess)
    await write_audit(
        db,
        event_type="session.created",
        payload={"session_id": str(sess.id)},
        workspace_id=body.workspace_id,
        actor_user_id=user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(sess)
    return sess


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: uuid.UUID, db: DbSession, user_id: UserId) -> Session:
    """Fetch session metadata."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    sess = result.scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await ensure_workspace_access(db, user_id=user_id, workspace_id=sess.workspace_id)
    return sess


@router.get("/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(
    session_id: uuid.UUID,
    db: DbSession,
    user_id: UserId,
) -> list[MessageOut]:
    """Return ordered messages for a session."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    sess = result.scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await ensure_workspace_access(db, user_id=user_id, workspace_id=sess.workspace_id)

    from kragen.models.core import Message

    msg_result = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.created_at)
    )
    return list(msg_result.scalars().all())
