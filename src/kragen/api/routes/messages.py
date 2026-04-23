"""Message posting: thin adapter → orchestrator."""

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from kragen.api.deps import CorrelationId, DbSession, UserId
from kragen.api.schemas import MessageCreate, MessageOut, MessagePostResponse, TaskOut
from kragen.models.core import Message, Session, Task
from kragen.services import orchestrator
from kragen.services.audit_service import write_audit
from kragen.services import task_stream

router = APIRouter(prefix="/sessions", tags=["messages"])


@router.post("/{session_id}/messages", response_model=MessagePostResponse)
async def post_message(
    session_id: uuid.UUID,
    body: MessageCreate,
    db: DbSession,
    user_id: UserId,
    correlation_id: CorrelationId,
) -> MessagePostResponse:
    """Append a user message and enqueue execution task."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    sess = result.scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if sess.user_id is not None and sess.user_id != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    user_msg = Message(
        session_id=session_id,
        role=body.role,
        content=body.content,
        metadata_=body.metadata,
    )
    db.add(user_msg)
    await db.flush()

    task = Task(
        session_id=session_id,
        status="queued",
        correlation_id=correlation_id,
        policy_profile="default",
        input_payload={"last_message_id": str(user_msg.id)},
    )
    db.add(task)
    await db.flush()

    task_stream.register_task(str(task.id))

    await write_audit(
        db,
        event_type="message.user",
        payload={"session_id": str(session_id), "task_id": str(task.id)},
        workspace_id=sess.workspace_id,
        actor_user_id=user_id,
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(user_msg)
    await db.refresh(task)

    orchestrator.schedule_task(
        task_id=task.id,
        session_id=session_id,
        workspace_id=sess.workspace_id,
        user_id=user_id,
        correlation_id=correlation_id,
    )

    return MessagePostResponse(
        message=MessageOut.model_validate(user_msg),
        task=TaskOut.model_validate(task),
    )
