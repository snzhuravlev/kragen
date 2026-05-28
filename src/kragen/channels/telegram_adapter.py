"""Backward-compatible re-exports for the Telegram channel adapter."""

from __future__ import annotations

from kragen.channels.telegram import main
from kragen.channels.telegram.adapter import (
    _disambiguate_storage_filename,
    _extract_storage_target_path,
    _mkdir_alias_command_line,
    _normalized_folder_path_from_mkdir_arg,
    _parse_command_arg,
    _telegram_command_body,
    handle_update_with_timeout,
)
from kragen.channels.telegram.settings import TelegramChannelSettings, read_settings
from kragen.channels.telegram.utils import (
    extract_message_payload as _extract_message_payload,
    health_payload as _health_payload,
    safe_filename as _safe_filename,
    split_telegram_message as _split_telegram_message,
)
from kragen.channels.telegram.webhook import is_valid_webhook_secret as _is_valid_webhook_secret

__all__ = [
    "TelegramChannelSettings",
    "_disambiguate_storage_filename",
    "_extract_message_payload",
    "_extract_storage_target_path",
    "_health_payload",
    "_is_valid_webhook_secret",
    "_mkdir_alias_command_line",
    "_normalized_folder_path_from_mkdir_arg",
    "_parse_command_arg",
    "_safe_filename",
    "_split_telegram_message",
    "_telegram_command_body",
    "handle_update_with_timeout",
    "main",
    "read_settings",
]
