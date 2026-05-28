"""Telegram webhook transport."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

from kragen.channels.telegram.adapter import handle_update_with_timeout
from kragen.channels.telegram.api_client import tg_set_webhook
from kragen.channels.telegram.dedup import dedup_cleanup_worker
from kragen.channels.telegram.settings import TelegramChannelSettings, read_settings
from kragen.channels.telegram.utils import health_payload
from kragen.logging_config import get_logger

logger = get_logger(__name__)


def is_valid_webhook_secret(
    *,
    configured_secret: str | None,
    received_secret: str | None,
) -> bool:
    """Validate Telegram webhook secret header when configured."""
    if not configured_secret:
        return True
    return received_secret == configured_secret


async def webhook_worker(
    *,
    queue: asyncio.Queue[dict[str, Any]],
    settings: TelegramChannelSettings,
) -> None:
    """Consume webhook updates and run the same handler as polling mode."""
    async with httpx.AsyncClient() as tg_client, httpx.AsyncClient() as kragen_client:
        while True:
            update = await queue.get()
            try:
                await handle_update_with_timeout(
                    tg_client,
                    kragen_client,
                    settings=settings,
                    update=update,
                )
            except Exception:
                logger.exception("telegram_webhook_update_failed")
            finally:
                queue.task_done()


async def run_telegram_channel_webhook() -> None:
    """Webhook mode: FastAPI receiver + background worker."""
    settings = read_settings()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    app = FastAPI(title="kragen-telegram-channel")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return health_payload(settings)

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        worker_task = getattr(app.state, "worker_task", None)
        if worker_task is None or worker_task.done():
            raise HTTPException(status_code=503, detail="Webhook worker is not running")
        return health_payload(settings)

    @app.post(settings.webhook_path)
    async def telegram_webhook(request: Request) -> dict[str, bool]:
        received_secret = request.headers.get("x-telegram-bot-api-secret-token")
        if not is_valid_webhook_secret(
            configured_secret=settings.webhook_secret_token,
            received_secret=received_secret,
        ):
            raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret token")
        payload = await request.json()
        if isinstance(payload, dict):
            await queue.put(payload)
        return {"ok": True}

    @app.on_event("startup")
    async def _startup() -> None:
        async with httpx.AsyncClient() as tg_client:
            await tg_set_webhook(tg_client, settings=settings)
        app.state.worker_task = asyncio.create_task(webhook_worker(queue=queue, settings=settings))
        app.state.cleanup_task = asyncio.create_task(dedup_cleanup_worker(settings=settings))

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task = getattr(app.state, "worker_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        cleanup_task = getattr(app.state, "cleanup_task", None)
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass

    config = uvicorn.Config(
        app=app,
        host=settings.webhook_host,
        port=settings.webhook_port,
        log_level=os.environ.get("KRAGEN_TELEGRAM_LOG_LEVEL", "info").lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()
