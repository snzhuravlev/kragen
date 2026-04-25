"""Task queue dispatch for Cursor worker jobs."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass

from kragen.config import get_settings
from kragen.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class TaskJob:
    """Serializable worker job payload."""

    task_id: uuid.UUID
    session_id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID | None
    correlation_id: str | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "task_id": str(self.task_id),
                "session_id": str(self.session_id),
                "workspace_id": str(self.workspace_id),
                "user_id": str(self.user_id) if self.user_id is not None else None,
                "correlation_id": self.correlation_id,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "TaskJob":
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return cls(
            task_id=uuid.UUID(str(data["task_id"])),
            session_id=uuid.UUID(str(data["session_id"])),
            workspace_id=uuid.UUID(str(data["workspace_id"])),
            user_id=uuid.UUID(str(data["user_id"])) if data.get("user_id") else None,
            correlation_id=data.get("correlation_id"),
        )


def _redis_client():
    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - depends on runtime install
        raise RuntimeError("Redis task queue requires the 'redis' package") from exc
    settings = get_settings().task_queue
    return Redis.from_url(settings.redis_url, decode_responses=False)


async def enqueue(job: TaskJob) -> None:
    """Push a worker job to the configured queue."""
    settings = get_settings().task_queue
    if settings.backend != "redis":
        raise RuntimeError("enqueue() is only available for the Redis task queue backend")

    client = _redis_client()
    try:
        await client.rpush(settings.redis_key, job.to_json())
    finally:
        await client.aclose()


async def dequeue_once() -> TaskJob | None:
    """Pop one worker job, returning None on timeout."""
    settings = get_settings().task_queue
    if settings.backend != "redis":
        raise RuntimeError("dequeue_once() is only available for the Redis task queue backend")

    client = _redis_client()
    try:
        item = await client.blpop([settings.redis_key], timeout=settings.block_timeout_seconds)
    finally:
        await client.aclose()
    if item is None:
        return None
    _key, payload = item
    return TaskJob.from_json(payload)


async def run_worker_loop(*, stop_after_one: bool = False) -> None:
    """Run queued Cursor jobs in a dedicated worker process."""
    from kragen.db.session import async_session_factory, engine
    from kragen.services import task_stream
    from kragen.services.orchestrator import run_cursor_worker

    task_stream.configure_from_settings()
    try:
        while True:
            job = await dequeue_once()
            if job is None:
                if stop_after_one:
                    return
                continue

            logger.info("task_queue_job_received", task_id=str(job.task_id))
            try:
                async with async_session_factory() as session:
                    await run_cursor_worker(
                        db=session,
                        task_id=job.task_id,
                        session_id=job.session_id,
                        workspace_id=job.workspace_id,
                        user_id=job.user_id,
                        correlation_id=job.correlation_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("task_queue_job_failed", task_id=str(job.task_id), error=str(exc))

            if stop_after_one:
                return
    finally:
        await engine.dispose()


def run_worker_process() -> None:
    """Synchronous entrypoint for the queued worker."""
    asyncio.run(run_worker_loop())
