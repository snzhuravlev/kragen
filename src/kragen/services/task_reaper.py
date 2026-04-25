"""Background reaper for stale running tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import text

from kragen.config import get_settings
from kragen.db.session import async_session_factory
from kragen.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class TaskReaperConfig:
    """Runtime config for stale task cleanup."""

    stale_after_seconds: int
    sweep_interval_seconds: int


def compute_stale_after_seconds(
    *, timeout_seconds: int, retries: int, minimum_stale_after_seconds: int
) -> int:
    """Compute stale timeout with buffer from worker runtime limits."""
    expected_run = max(1, timeout_seconds) * max(1, retries + 1)
    return max(minimum_stale_after_seconds, expected_run + 120)


def build_task_reaper_config() -> TaskReaperConfig:
    """Derive stale-task policy from worker settings."""
    settings = get_settings().worker
    # Keep generous buffer to avoid racing with normal long runs/retries.
    stale_after = compute_stale_after_seconds(
        timeout_seconds=settings.timeout_seconds,
        retries=settings.retries,
        minimum_stale_after_seconds=settings.stuck_task_timeout_seconds,
    )
    interval = max(15, settings.task_reap_interval_seconds)
    return TaskReaperConfig(
        stale_after_seconds=stale_after,
        sweep_interval_seconds=interval,
    )


async def reap_stuck_running_tasks(*, stale_after_seconds: int) -> int:
    """Mark old running tasks as failed and return reaped row count."""
    if stale_after_seconds <= 0:
        return 0
    async with async_session_factory() as db:
        result = await db.execute(
            text(
                """
                UPDATE tasks
                SET status = 'failed',
                    error = COALESCE(error, '') ||
                            CASE WHEN COALESCE(error, '') = '' THEN '' ELSE '\n' END ||
                            'task reaper: stale running task timeout',
                    updated_at = now()
                WHERE status = 'running'
                  AND updated_at < (now() - make_interval(secs => :stale_after_seconds))
                RETURNING id
                """
            ),
            {"stale_after_seconds": stale_after_seconds},
        )
        rows = result.fetchall()
        await db.commit()
    return len(rows)


async def run_task_reaper() -> None:
    """Periodic stale-task cleanup loop for API process."""
    cfg = build_task_reaper_config()
    while True:
        try:
            reaped = await reap_stuck_running_tasks(stale_after_seconds=cfg.stale_after_seconds)
            if reaped > 0:
                logger.warning(
                    "task_reaper_reaped",
                    reaped=reaped,
                    stale_after_seconds=cfg.stale_after_seconds,
                )
        except Exception:  # noqa: BLE001
            logger.exception("task_reaper_failed")
        await asyncio.sleep(cfg.sweep_interval_seconds)
