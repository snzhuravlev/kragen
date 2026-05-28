"""Telegram long-polling transport."""

from __future__ import annotations

import asyncio

import httpx

from kragen.channels.telegram.adapter import handle_update_with_timeout
from kragen.channels.telegram.api_client import tg_get_updates
from kragen.channels.telegram.dedup import dedup_cleanup_worker
from kragen.channels.telegram.settings import read_settings
from kragen.logging_config import get_logger

logger = get_logger(__name__)


async def run_telegram_channel() -> None:
    """Main long-polling loop."""
    settings = read_settings()
    logger.info(
        "telegram_channel_start",
        api_base=settings.kragen_api_base_url,
        workspace_id=str(settings.default_workspace_id),
        auth_user_id=str(settings.auth_user_id),
    )

    offset: int | None = None
    cleanup_task = asyncio.create_task(dedup_cleanup_worker(settings=settings))
    pending_updates: set[asyncio.Task[None]] = set()
    try:
        async with httpx.AsyncClient() as tg_client, httpx.AsyncClient() as kragen_client:
            while True:
                try:
                    updates = await tg_get_updates(
                        tg_client,
                        settings=settings,
                        offset=offset,
                    )
                    for update in updates:
                        update_id_val = update.get("update_id")
                        if not isinstance(update_id_val, int):
                            continue
                        task = asyncio.create_task(
                            handle_update_with_timeout(
                                tg_client,
                                kragen_client,
                                settings=settings,
                                update=update,
                            )
                        )
                        pending_updates.add(task)
                        task.add_done_callback(pending_updates.discard)
                        if len(pending_updates) >= 32:
                            _done, pending = await asyncio.wait(
                                pending_updates, return_when=asyncio.FIRST_COMPLETED
                            )
                            pending_updates = set(pending)
                            for done_task in _done:
                                try:
                                    done_task.result()
                                except Exception:
                                    pass
                        offset = update_id_val + 1
                except httpx.HTTPError as exc:
                    logger.warning("telegram_http_error", error=str(exc))
                    await asyncio.sleep(2.0)
                except Exception:  # noqa: BLE001
                    logger.exception("telegram_loop_error")
                    await asyncio.sleep(2.0)
                await asyncio.sleep(settings.loop_delay_seconds)
    finally:
        cleanup_task.cancel()
        for task in list(pending_updates):
            task.cancel()
        if pending_updates:
            await asyncio.gather(*pending_updates, return_exceptions=True)
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
