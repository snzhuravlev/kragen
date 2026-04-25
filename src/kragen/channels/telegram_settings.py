"""Runtime settings for the Telegram channel adapter."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from kragen.config import get_settings as get_kragen_settings


@dataclass(frozen=True)
class TelegramChannelSettings:
    """Runtime settings for Telegram channel adapter."""

    bot_token: str
    kragen_api_base_url: str
    api_bearer_token: str | None
    auth_user_id: uuid.UUID
    default_workspace_id: uuid.UUID
    poll_timeout_seconds: int = 20
    loop_delay_seconds: float = 0.4
    task_poll_interval_seconds: float = 1.0
    task_wait_timeout_seconds: int = 300
    mode: str = "polling"
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8081
    webhook_path: str = "/telegram/webhook"
    webhook_public_url: str | None = None
    webhook_secret_token: str | None = None
    dedup_retention_hours: int = 168
    dedup_cleanup_interval_seconds: int = 3600
    dedup_processing_timeout_minutes: int = 30

    @property
    def telegram_api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"


def read_settings() -> TelegramChannelSettings:
    """Read Telegram adapter settings from YAML plus environment overrides."""
    yaml_cfg = get_kragen_settings().telegram_channel

    token = os.environ.get("KRAGEN_TELEGRAM_BOT_TOKEN", yaml_cfg.bot_token).strip()
    if not token:
        raise RuntimeError("KRAGEN_TELEGRAM_BOT_TOKEN is required")

    api_base = os.environ.get("KRAGEN_TELEGRAM_API_BASE_URL", yaml_cfg.api_base_url).strip()
    api_bearer_token = (
        os.environ.get("KRAGEN_TELEGRAM_API_BEARER_TOKEN", yaml_cfg.api_bearer_token or "").strip()
        or None
    )
    auth_user = os.environ.get("KRAGEN_TELEGRAM_AUTH_USER_ID", yaml_cfg.auth_user_id).strip()
    workspace = os.environ.get(
        "KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID", yaml_cfg.default_workspace_id
    ).strip()
    if not auth_user:
        raise RuntimeError("KRAGEN_TELEGRAM_AUTH_USER_ID is required")
    if not workspace:
        raise RuntimeError("KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID is required")

    return TelegramChannelSettings(
        bot_token=token,
        kragen_api_base_url=api_base,
        api_bearer_token=api_bearer_token,
        auth_user_id=uuid.UUID(auth_user),
        default_workspace_id=uuid.UUID(workspace),
        poll_timeout_seconds=int(
            os.environ.get("KRAGEN_TELEGRAM_POLL_TIMEOUT_SECONDS", str(yaml_cfg.poll_timeout_seconds))
        ),
        loop_delay_seconds=float(
            os.environ.get("KRAGEN_TELEGRAM_LOOP_DELAY_SECONDS", str(yaml_cfg.loop_delay_seconds))
        ),
        task_poll_interval_seconds=float(
            os.environ.get(
                "KRAGEN_TELEGRAM_TASK_POLL_INTERVAL_SECONDS",
                str(yaml_cfg.task_poll_interval_seconds),
            )
        ),
        task_wait_timeout_seconds=int(
            os.environ.get(
                "KRAGEN_TELEGRAM_TASK_WAIT_TIMEOUT_SECONDS",
                str(yaml_cfg.task_wait_timeout_seconds),
            )
        ),
        mode=os.environ.get("KRAGEN_TELEGRAM_MODE", yaml_cfg.mode).strip().lower(),
        webhook_host=os.environ.get("KRAGEN_TELEGRAM_WEBHOOK_HOST", yaml_cfg.webhook_host).strip(),
        webhook_port=int(os.environ.get("KRAGEN_TELEGRAM_WEBHOOK_PORT", str(yaml_cfg.webhook_port))),
        webhook_path=os.environ.get("KRAGEN_TELEGRAM_WEBHOOK_PATH", yaml_cfg.webhook_path).strip(),
        webhook_public_url=(
            os.environ.get("KRAGEN_TELEGRAM_WEBHOOK_PUBLIC_URL", yaml_cfg.webhook_public_url or "").strip()
            or None
        ),
        webhook_secret_token=(
            os.environ.get(
                "KRAGEN_TELEGRAM_WEBHOOK_SECRET_TOKEN",
                yaml_cfg.webhook_secret_token or "",
            ).strip()
            or None
        ),
        dedup_retention_hours=int(
            os.environ.get("KRAGEN_TELEGRAM_DEDUP_RETENTION_HOURS", str(yaml_cfg.dedup_retention_hours))
        ),
        dedup_cleanup_interval_seconds=int(
            os.environ.get(
                "KRAGEN_TELEGRAM_DEDUP_CLEANUP_INTERVAL_SECONDS",
                str(yaml_cfg.dedup_cleanup_interval_seconds),
            )
        ),
        dedup_processing_timeout_minutes=int(
            os.environ.get(
                "KRAGEN_TELEGRAM_DEDUP_PROCESSING_TIMEOUT_MINUTES",
                str(yaml_cfg.dedup_processing_timeout_minutes),
            )
        ),
    )
