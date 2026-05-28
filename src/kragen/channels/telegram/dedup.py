"""Background dedup retention for Telegram message processing."""

from __future__ import annotations

import asyncio

from kragen.channels.telegram.settings import TelegramChannelSettings
from kragen.db.session import async_session_factory
from kragen.logging_config import get_logger
from kragen.services.telegram_bindings import (
    cleanup_processed_messages,
    reap_stuck_processing_messages,
)

logger = get_logger(__name__)


async def dedup_cleanup_worker(*, settings: TelegramChannelSettings) -> None:
    """Periodically reap stuck ``processing`` rows and purge old dedup records."""
    interval = max(60, settings.dedup_cleanup_interval_seconds)
    retention = max(1, settings.dedup_retention_hours)
    processing_timeout = max(1, settings.dedup_processing_timeout_minutes)
    while True:
        try:
            async with async_session_factory() as db:
                reaped = await reap_stuck_processing_messages(
                    db,
                    older_than_minutes=processing_timeout,
                )
                deleted = await cleanup_processed_messages(
                    db,
                    older_than_hours=retention,
                )
                await db.commit()
                if reaped > 0 or deleted > 0:
                    logger.info(
                        "telegram_dedup_cleanup",
                        reaped=reaped,
                        deleted=deleted,
                        retention_hours=retention,
                        processing_timeout_minutes=processing_timeout,
                    )
        except Exception:  # noqa: BLE001
            logger.exception("telegram_dedup_cleanup_failed")
        await asyncio.sleep(interval)
