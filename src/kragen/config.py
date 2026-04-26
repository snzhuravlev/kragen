"""Application configuration: YAML file + .env + environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, PostgresDsn, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

EnvironmentName = Literal["local", "dev", "staging", "prod"]


def _resolve_yaml_path() -> Path:
    """Locate kragen.yaml: KRAGEN_CONFIG_FILE, then ./configs/kragen.yaml, then repo-relative."""
    explicit = os.environ.get("KRAGEN_CONFIG_FILE")
    if explicit:
        return Path(explicit).expanduser().resolve()
    cwd_cfg = Path.cwd() / "configs" / "kragen.yaml"
    if cwd_cfg.is_file():
        return cwd_cfg.resolve()
    # Development: src/kragen/config.py -> parents[3] == project root when layout is project/src/kragen/
    here = Path(__file__).resolve()
    repo_cfg = here.parents[3] / "configs" / "kragen.yaml"
    if repo_cfg.is_file():
        return repo_cfg.resolve()
    return cwd_cfg


def get_config_yaml_path() -> Path:
    """Return absolute path to the Kragen YAML file used for settings (same resolution as loading)."""
    return _resolve_yaml_path()


class AppSettings(BaseModel):
    """Process and logging."""

    model_config = SettingsConfigDict(extra="forbid")

    name: str = "kragen"
    environment: EnvironmentName = "local"
    log_level: str = "INFO"


class ApiSettings(BaseModel):
    """HTTP server binding."""

    model_config = SettingsConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = 8000
    # Public URL for clients and Cursor worker (import/MCP). If unset, worker uses
    # http://127.0.0.1:{port} for KRAGEN_API_URL.
    public_base_url: str | None = None


class DatabaseSettings(BaseModel):
    """Primary database (async SQLAlchemy URL)."""

    model_config = SettingsConfigDict(extra="forbid")

    url: PostgresDsn


class StorageSettings(BaseModel):
    """S3-compatible object storage (MinIO, AWS S3, etc.)."""

    model_config = SettingsConfigDict(extra="forbid")

    endpoint_url: str = "http://127.0.0.1:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket: str = "kragen-artifacts"


class AuthSettings(BaseModel):
    """Authentication, development shortcuts, and RBAC."""

    model_config = SettingsConfigDict(extra="forbid")

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    oidc_jwks_url: str | None = None
    raw_uuid_bearer_enabled: bool = True
    disabled: bool = False
    dev_user_id: str | None = None
    admin_user_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit allow-list of user UUIDs that may reach admin-only endpoints "
            "(/admin/config/*, /admin/cursor-auth/*, /admin/plugins/*, /admin/workers, "
            "/admin/logs*, /admin/memory/status). Empty = no one is admin (safe default)."
        ),
    )


class HttpSettings(BaseModel):
    """HTTP middleware (CORS)."""

    model_config = SettingsConfigDict(extra="forbid")

    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])


class FileImportSettings(BaseModel):
    """Server-side download from URL into logical storage (POST /files/import)."""

    model_config = SettingsConfigDict(extra="forbid")

    max_bytes: int = 104_857_600  # 100 MiB
    timeout_seconds: float = 60.0
    # If non-empty, remote host must match one of these suffixes (e.g. "postgresql.org").
    # Empty list = allow all hosts (suitable for local dev only).
    allowed_host_suffixes: list[str] = Field(default_factory=list)


class WorkerSettings(BaseModel):
    """Cursor / worker process paths."""

    model_config = SettingsConfigDict(extra="forbid")

    cursor_cli_path: str = "cursor"
    workspace_root: str = "~/.kragen/workspaces"
    timeout_seconds: int = 180
    retries: int = 1
    stuck_task_timeout_seconds: int = 900
    task_reap_interval_seconds: int = 60
    memory_context_enabled: bool = True
    memory_top_k: int = 4
    # Short-lived JWT for the Cursor worker to call /files/import (and MCP tools) as the user.
    task_token_enabled: bool = True
    task_token_ttl_seconds: int = 900


class TaskStreamSettings(BaseModel):
    """Transport for task output consumed by SSE clients."""

    model_config = SettingsConfigDict(extra="forbid")

    backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_prefix: str = "kragen:task-stream"
    ttl_seconds: int = 3600
    block_timeout_ms: int = 5000


class TaskQueueSettings(BaseModel):
    """Queue used to dispatch Cursor worker jobs."""

    model_config = SettingsConfigDict(extra="forbid")

    backend: Literal["inline", "redis"] = "inline"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_key: str = "kragen:task-queue"
    block_timeout_seconds: int = 5


class ChannelsSettings(BaseModel):
    """Channel-level feature flags."""

    model_config = SettingsConfigDict(extra="forbid")

    openclaw_enabled: bool = False


class PluginEnableEntry(BaseModel):
    """Single enabled-plugin entry in ``plugins.enabled``.

    Plugins not listed here are discovered but remain disabled at boot.
    """

    model_config = SettingsConfigDict(extra="forbid")

    id: str
    config: dict[str, object] = Field(default_factory=dict)


class PluginsSettings(BaseModel):
    """Plugin subsystem configuration."""

    model_config = SettingsConfigDict(extra="forbid")

    autoload_entry_points: bool = Field(
        default=True,
        description="Scan the 'kragen.plugins' entry-point group at startup.",
    )
    enabled: list[PluginEnableEntry] = Field(
        default_factory=list,
        description="Ordered allow-list of plugin ids with per-plugin config blocks.",
    )


class TelegramChannelSettings(BaseModel):
    """Telegram adapter profile used by deployment tooling and local defaults."""

    model_config = SettingsConfigDict(extra="forbid")

    bot_token: str = ""
    api_base_url: str = "http://127.0.0.1:8000"
    api_bearer_token: str | None = None
    auth_user_id: str = "00000000-0000-0000-0000-000000000001"
    default_workspace_id: str = "00000000-0000-0000-0000-000000000001"
    mode: str = "polling"
    poll_timeout_seconds: int = 20
    loop_delay_seconds: float = 0.4
    task_poll_interval_seconds: float = 1.0
    task_wait_timeout_seconds: int = 300
    dedup_retention_hours: int = 168
    dedup_cleanup_interval_seconds: int = 3600
    dedup_processing_timeout_minutes: int = 30
    webhook_public_url: str | None = None
    webhook_path: str = "/telegram/webhook"
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8081
    webhook_secret_token: str | None = None


class KragenSettings(BaseSettings):
    """
    Root settings loaded from YAML, then `.env`, then environment variables.

    Environment variables use prefix ``KRAGEN_`` and ``__`` for nesting, e.g.
    ``KRAGEN_DATABASE__URL``, ``KRAGEN_AUTH__DISABLED``.
    """

    model_config = SettingsConfigDict(
        env_prefix="KRAGEN_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        # Ignore legacy flat keys (e.g. DATABASE_URL) left in .env; use KRAGEN_* or YAML only.
        extra="ignore",
        case_sensitive=False,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    database: DatabaseSettings
    storage: StorageSettings = Field(default_factory=StorageSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    file_import: FileImportSettings = Field(default_factory=FileImportSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    task_stream: TaskStreamSettings = Field(default_factory=TaskStreamSettings)
    task_queue: TaskQueueSettings = Field(default_factory=TaskQueueSettings)
    channels: ChannelsSettings = Field(default_factory=ChannelsSettings)
    plugins: PluginsSettings = Field(default_factory=PluginsSettings)
    telegram_channel: TelegramChannelSettings = Field(default_factory=TelegramChannelSettings)

    @model_validator(mode="after")
    def validate_production_profile(self) -> "KragenSettings":
        """Reject development-only settings in production."""
        if self.app.environment != "prod":
            return self

        errors: list[str] = []
        if self.auth.disabled:
            errors.append("auth.disabled must be false")
        if self.auth.dev_user_id:
            errors.append("auth.dev_user_id must be unset")
        if self.auth.jwt_secret in {"", "change-me-in-production"}:
            errors.append("auth.jwt_secret must be set to a production secret")
        if self.auth.raw_uuid_bearer_enabled:
            errors.append("auth.raw_uuid_bearer_enabled must be false")
        if self.auth.jwt_algorithm.upper().startswith(("RS", "ES")) and not self.auth.oidc_jwks_url:
            errors.append("auth.oidc_jwks_url must be set for asymmetric JWT algorithms")
        if self.api.host in {"0.0.0.0", "::"}:
            errors.append("api.host must not bind to all interfaces behind a reverse proxy")

        if errors:
            joined = "; ".join(errors)
            raise ValueError(f"Invalid production configuration: {joined}")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """
        Priority (first wins on conflicts): CLI/init > environment variables > `.env` > `configs/kragen.yaml` > defaults.

        See https://docs.pydantic.dev/latest/concepts/pydantic_settings/#customize-settings-sources
        """
        yaml_path = _resolve_yaml_path()
        yaml_source = YamlConfigSettingsSource(
            settings_cls,
            yaml_file=yaml_path,
            yaml_file_encoding="utf-8",
        )
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            yaml_source,
            file_secret_settings,
        )


@lru_cache
def get_settings() -> KragenSettings:
    """Return cached settings singleton."""
    return KragenSettings()  # type: ignore[call-arg]


def clear_settings_cache() -> None:
    """Invalidate cached settings (call after updating kragen.yaml on disk)."""
    get_settings.cache_clear()


def api_public_base_url() -> str:
    """
    Public HTTP origin for the API (for worker/MCP/CLI), without trailing slash.
    """
    s = get_settings()
    if s.api.public_base_url:
        return str(s.api.public_base_url).rstrip("/")
    return f"http://127.0.0.1:{s.api.port}"
