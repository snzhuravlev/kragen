"""Backward-compatible re-export."""

from kragen.channels.telegram.settings import TelegramChannelSettings, read_settings

__all__ = ["TelegramChannelSettings", "read_settings"]
