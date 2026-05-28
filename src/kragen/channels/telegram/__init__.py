"""Telegram channel adapter package entrypoint."""

from __future__ import annotations

import asyncio
import os

from kragen.channels.telegram.polling import run_telegram_channel
from kragen.channels.telegram.settings import read_settings
from kragen.channels.telegram.webhook import run_telegram_channel_webhook
from kragen.logging_config import configure_logging


def main() -> None:
    """Console entrypoint."""
    configure_logging(os.environ.get("KRAGEN_TELEGRAM_LOG_LEVEL", "INFO"))
    settings = read_settings()
    if settings.mode == "webhook":
        asyncio.run(run_telegram_channel_webhook())
        return
    asyncio.run(run_telegram_channel())
