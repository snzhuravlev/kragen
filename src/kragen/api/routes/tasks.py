"""Task status and SSE stream."""

import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kragen.api.deps import DbSession, UserId, ensure_workspace_access
from kragen.api.schemas import TaskOut
from kragen.models.core import Session, Task
from kragen.services import task_stream

router = APIRouter(tags=["tasks"])


async def _get_authorized_task(db: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID) -> Task:
    """Load a task and enforce session ownership."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    sess_result = await db.execute(select(Session).where(Session.id == row.session_id))
    sess = sess_result.scalar_one_or_none()
    if sess is not None:
        await ensure_workspace_access(db, user_id=user_id, workspace_id=sess.workspace_id)
    return row


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: uuid.UUID, db: DbSession, user_id: UserId) -> Task:
    """Return task record."""
    return await _get_authorized_task(db, task_id, user_id)


async def _sse_iter(task_id: uuid.UUID) -> Any:
    """Format stream chunks as SSE events (JSON string per line so newlines in text are safe)."""
    async for chunk in task_stream.iter_chunks(str(task_id)):
        yield f"data: {json.dumps(chunk)}\n\n"
    yield "event: end\ndata: {}\n\n"


@router.get("/tasks/{task_id}/stream")
async def stream_task(task_id: uuid.UUID, db: DbSession, user_id: UserId) -> StreamingResponse:
    """Server-Sent Events stream of assistant output chunks."""
    await _get_authorized_task(db, task_id, user_id)

    return StreamingResponse(
        _sse_iter(task_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
