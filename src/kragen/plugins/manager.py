"""PluginManager: the runtime registry consumed by core (orchestrator + app).

Lifecycle:

1. ``PluginManager.initialize()`` is called once during FastAPI ``lifespan``
   startup. It runs entry-point discovery, filters by ``plugins.enabled`` from
   ``kragen.yaml``, and invokes ``plugin.setup(ctx)`` for each accepted plugin.
2. The API process queries the manager for backend routers right after init
   and mounts them on the FastAPI app.
3. The orchestrator queries the manager per task:
   * ``compose_prompt(...)`` appends active skill fragments;
   * ``materialize_mcp_config(workspace_path)`` writes ``.cursor/mcp.json``
     with all active MCP servers.
4. ``/admin/plugins/*`` toggles the ``enabled`` flag at runtime. Skill and MCP
   toggles take effect on the next task. Backend routers cannot be unmounted
   from a running FastAPI app, so toggling a backend plugin requires restart.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from kragen.config import KragenSettings, get_settings
from kragen.logging_config import get_logger
from kragen.plugins.base import (
    BackendSpec,
    ChannelSpec,
    KragenPlugin,
    MCPServerSpec,
    PluginManifest,
    SkillSpec,
)
from kragen.plugins.context import PluginContext
from kragen.plugins.errors import (
    PluginAlreadyRegisteredError,
    PluginNotFoundError,
)
from kragen.plugins.loader import DiscoveredPlugin, discover_plugins

logger = get_logger(__name__)


class _PluginRecord:
    """Internal bookkeeping for one loaded plugin."""

    __slots__ = (
        "manifest",
        "instance",
        "config",
        "enabled",
        "ep_name",
        "dist_name",
        "skills",
        "mcp_servers",
        "backends",
        "channels",
        "setup_error",
    )

    def __init__(
        self,
        *,
        manifest: PluginManifest,
        instance: KragenPlugin,
        config: dict[str, Any],
        enabled: bool,
        ep_name: str,
        dist_name: str | None,
    ) -> None:
        self.manifest = manifest
        self.instance = instance
        self.config = config
        self.enabled = enabled
        self.ep_name = ep_name
        self.dist_name = dist_name
        self.skills: list[SkillSpec] = []
        self.mcp_servers: list[MCPServerSpec] = []
        self.backends: list[BackendSpec] = []
        self.channels: list[ChannelSpec] = []
        self.setup_error: str | None = None


class PluginManager:
    """Central registry; normally accessed via ``get_plugin_manager()``."""

    def __init__(self) -> None:
        self._records: dict[str, _PluginRecord] = {}
        self._lock = threading.RLock()
        self._initialized = False
        self._settings: KragenSettings = get_settings()
        # Plugin id currently performing `setup()` — used by the registration
        # helpers to associate specs with the right record.
        self._current_plugin_id: str | None = None

    # --- initialization -----------------------------------------------------

    def initialize(self) -> None:
        """Discover entry points and run setup for enabled plugins."""
        with self._lock:
            if self._initialized:
                return
            settings = get_settings()
            self._settings = settings

            enabled_cfg = {
                item.id: item for item in settings.plugins.enabled
            }
            autoload = settings.plugins.autoload_entry_points

            discovered: list[DiscoveredPlugin] = (
                discover_plugins() if autoload else []
            )
            seen_ids: set[str] = set()
            for found in discovered:
                manifest = found.instance.manifest
                if manifest.id in seen_ids:
                    logger.warning(
                        "plugin_duplicate_id_skipped",
                        plugin_id=manifest.id,
                        entry_point=found.ep_name,
                    )
                    continue
                seen_ids.add(manifest.id)

                cfg_entry = enabled_cfg.get(manifest.id)
                is_enabled = cfg_entry is not None
                plugin_config = dict(cfg_entry.config) if cfg_entry else {}

                record = _PluginRecord(
                    manifest=manifest,
                    instance=found.instance,
                    config=plugin_config,
                    enabled=is_enabled,
                    ep_name=found.ep_name,
                    dist_name=found.dist_name,
                )
                self._records[manifest.id] = record

            self._run_setup_for_enabled()
            self._initialized = True
            logger.info(
                "plugin_manager_initialized",
                total=len(self._records),
                enabled=sum(1 for r in self._records.values() if r.enabled),
            )

    def _run_setup_for_enabled(self) -> None:
        """Invoke ``setup()`` on every enabled plugin, collecting registered specs."""
        for plugin_id, record in self._records.items():
            if not record.enabled:
                continue
            ctx = PluginContext(
                manager=self,
                manifest=record.manifest,
                config=record.config,
                settings=self._settings,
            )
            self._current_plugin_id = plugin_id
            try:
                record.instance.setup(ctx)
                record.setup_error = None
            except Exception as exc:  # noqa: BLE001
                record.setup_error = f"{type(exc).__name__}: {exc}"
                record.enabled = False
                logger.exception("plugin_setup_failed", plugin_id=plugin_id)
            finally:
                self._current_plugin_id = None

    # --- registration helpers (called from PluginContext) -------------------

    def _require_current(self) -> _PluginRecord:
        if self._current_plugin_id is None:
            raise RuntimeError(
                "PluginContext registration called outside of plugin.setup()."
            )
        return self._records[self._current_plugin_id]

    def _register_skill(self, plugin_id: str, spec: SkillSpec) -> None:
        record = self._records[plugin_id]
        if any(s.id == spec.id for s in record.skills):
            raise PluginAlreadyRegisteredError(
                f"Skill '{spec.id}' already registered by plugin '{plugin_id}'."
            )
        record.skills.append(spec)

    def _register_mcp(self, plugin_id: str, spec: MCPServerSpec) -> None:
        record = self._records[plugin_id]
        if any(s.id == spec.id for s in record.mcp_servers):
            raise PluginAlreadyRegisteredError(
                f"MCP server '{spec.id}' already registered by plugin '{plugin_id}'."
            )
        record.mcp_servers.append(spec)

    def _register_backend(self, plugin_id: str, spec: BackendSpec) -> None:
        record = self._records[plugin_id]
        record.backends.append(spec)

    def _register_channel(self, plugin_id: str, spec: ChannelSpec) -> None:
        record = self._records[plugin_id]
        record.channels.append(spec)

    # --- read-side API used by core ----------------------------------------

    def active_skills(self, *, user_message: str | None = None) -> list[SkillSpec]:
        """Return skill specs applicable to the current task, sorted by priority."""
        text = (user_message or "").lower()
        out: list[SkillSpec] = []
        with self._lock:
            for record in self._records.values():
                if not record.enabled:
                    continue
                for spec in record.skills:
                    if spec.when == "always":
                        out.append(spec)
                    elif spec.when == "mention":
                        if any(t.lower() in text for t in spec.triggers):
                            out.append(spec)
                    # 'manual' requires an explicit session binding — handled
                    # separately once /sessions/{id}/skills is wired to storage.
        out.sort(key=lambda s: s.priority)
        return out

    def active_mcp_servers(self) -> list[MCPServerSpec]:
        """Return MCP server specs from all enabled plugins."""
        out: list[MCPServerSpec] = []
        with self._lock:
            for record in self._records.values():
                if not record.enabled:
                    continue
                out.extend(record.mcp_servers)
        return out

    def all_backends(self) -> list[BackendSpec]:
        """Return backend specs from all plugins that ever registered one.

        Backend routers are mounted once at app start, so this is returned
        regardless of runtime ``enabled`` state; subsequent toggling takes
        effect only after a restart.
        """
        out: list[BackendSpec] = []
        with self._lock:
            for record in self._records.values():
                out.extend(record.backends)
        return out

    def compose_prompt(self, *, base: str, user_message: str) -> str:
        """Append enabled skill prompt fragments to ``base``."""
        skills = self.active_skills(user_message=user_message)
        if not skills:
            return base
        fragments = [base.rstrip()]
        fragments.append("\n\nActive skills for this task:")
        for skill in skills:
            fragments.append(f"\n\n[skill:{skill.id}] {skill.title}\n{skill.prompt.strip()}")
        return "".join(fragments)

    async def materialize_mcp_config(self, workspace_path: Path) -> Path | None:
        """Write ``.cursor/mcp.json`` into the per-task workspace directory.

        Returns the written path, or ``None`` when no MCP servers are active.
        Cursor CLI picks up ``.cursor/mcp.json`` automatically when run with
        ``--workspace`` pointed at the parent directory.
        """
        servers = self.active_mcp_servers()
        if not servers:
            return None

        payload: dict[str, Any] = {"mcpServers": {}}
        for spec in servers:
            entry: dict[str, Any] = {"command": spec.command}
            if spec.args:
                entry["args"] = list(spec.args)
            if spec.cwd:
                entry["cwd"] = spec.cwd
            if spec.env:
                entry["env"] = dict(spec.env)
            payload["mcpServers"][spec.id] = entry

        target_dir = workspace_path / ".cursor"
        target = target_dir / "mcp.json"

        def _write() -> None:
            target_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        await asyncio.to_thread(_write)
        return target

    # --- introspection & admin ---------------------------------------------

    def list_plugins(self) -> list[dict[str, Any]]:
        """Serialize all known plugins for admin UI."""
        with self._lock:
            return [self._serialize_record(r) for r in self._records.values()]

    def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(plugin_id)
            if record is None:
                raise PluginNotFoundError(plugin_id)
            return self._serialize_record(record)

    def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        """Toggle a plugin; re-runs ``setup()`` when enabling a previously-disabled one."""
        with self._lock:
            record = self._records.get(plugin_id)
            if record is None:
                raise PluginNotFoundError(plugin_id)
            if enabled == record.enabled:
                return self._serialize_record(record)

            if enabled:
                record.skills.clear()
                record.mcp_servers.clear()
                record.channels.clear()
                record.enabled = True
                self._current_plugin_id = plugin_id
                try:
                    ctx = PluginContext(
                        manager=self,
                        manifest=record.manifest,
                        config=record.config,
                        settings=self._settings,
                    )
                    record.instance.setup(ctx)
                    record.setup_error = None
                except Exception as exc:  # noqa: BLE001
                    record.setup_error = f"{type(exc).__name__}: {exc}"
                    record.enabled = False
                    logger.exception("plugin_enable_failed", plugin_id=plugin_id)
                finally:
                    self._current_plugin_id = None
            else:
                # Drop non-backend specs immediately; backend routers stay
                # mounted until the API is restarted.
                record.skills.clear()
                record.mcp_servers.clear()
                record.channels.clear()
                record.enabled = False

            return self._serialize_record(record)

    def update_config(self, plugin_id: str, new_config: dict[str, Any]) -> dict[str, Any]:
        """Replace a plugin's runtime configuration and re-run setup if enabled."""
        with self._lock:
            record = self._records.get(plugin_id)
            if record is None:
                raise PluginNotFoundError(plugin_id)
            record.config = dict(new_config)
            if record.enabled:
                record.skills.clear()
                record.mcp_servers.clear()
                record.channels.clear()
                self._current_plugin_id = plugin_id
                try:
                    ctx = PluginContext(
                        manager=self,
                        manifest=record.manifest,
                        config=record.config,
                        settings=self._settings,
                    )
                    record.instance.setup(ctx)
                    record.setup_error = None
                except Exception as exc:  # noqa: BLE001
                    record.setup_error = f"{type(exc).__name__}: {exc}"
                    record.enabled = False
                    logger.exception("plugin_reconfigure_failed", plugin_id=plugin_id)
                finally:
                    self._current_plugin_id = None
            return self._serialize_record(record)

    # --- shutdown -----------------------------------------------------------

    async def shutdown(self) -> None:
        """Call optional ``async shutdown()`` on every enabled plugin."""
        for plugin_id, record in list(self._records.items()):
            if not record.enabled:
                continue
            shutdown_fn = getattr(record.instance, "shutdown", None)
            if shutdown_fn is None:
                continue
            try:
                result = shutdown_fn()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.exception("plugin_shutdown_failed", plugin_id=plugin_id)

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _serialize_record(record: _PluginRecord) -> dict[str, Any]:
        return {
            "id": record.manifest.id,
            "version": record.manifest.version,
            "kind": record.manifest.kind,
            "name": record.manifest.name or record.manifest.id,
            "description": record.manifest.description,
            "author": record.manifest.author,
            "homepage": record.manifest.homepage,
            "requires": list(record.manifest.requires),
            "enabled": record.enabled,
            "entry_point": record.ep_name,
            "distribution": record.dist_name,
            "config": dict(record.config),
            "setup_error": record.setup_error,
            "skills": [s.model_dump() for s in record.skills],
            "mcp_servers": [s.model_dump() for s in record.mcp_servers],
            "backends": [
                {"id": b.id, "prefix": b.prefix, "tags": list(b.tags)}
                for b in record.backends
            ],
            "channels": [c.model_dump() for c in record.channels],
        }


_manager: PluginManager | None = None


def get_plugin_manager() -> PluginManager:
    """Process-wide singleton. Created lazily; initialized by ``lifespan``."""
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager


def reset_plugin_manager_for_tests() -> None:
    """Drop the singleton so tests can rebuild a fresh manager."""
    global _manager
    _manager = None
