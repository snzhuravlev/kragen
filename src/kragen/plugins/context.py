"""PluginContext: the only surface a plugin sees from Kragen core.

Trust model: plugin code runs in-process with full Python privileges, but
registration still goes through this narrow API so the runtime keeps a single
source of truth for active specs. This also prepares the ground for future
permission enforcement without breaking plugin signatures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kragen.logging_config import get_logger
from kragen.plugins.base import (
    BackendSpec,
    ChannelSpec,
    MCPServerSpec,
    PluginManifest,
    SkillSpec,
)

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter

    from kragen.config import KragenSettings
    from kragen.plugins.manager import PluginManager


class PluginContext:
    """Per-plugin handle passed into ``plugin.setup(ctx)``."""

    def __init__(
        self,
        *,
        manager: "PluginManager",
        manifest: PluginManifest,
        config: dict[str, Any],
        settings: "KragenSettings",
    ) -> None:
        self._manager = manager
        self.manifest = manifest
        self.config: dict[str, Any] = dict(config)
        self.settings = settings
        self.logger = get_logger(f"kragen.plugin.{manifest.id}")

    # --- registration API ---------------------------------------------------

    def register_skill(self, spec: SkillSpec) -> None:
        """Advertise a prompt-level skill for this plugin."""
        self._manager._register_skill(self.manifest.id, spec)

    def register_mcp_server(self, spec: MCPServerSpec) -> None:
        """Advertise an MCP server to be spawned per task workspace."""
        self._manager._register_mcp(self.manifest.id, spec)

    def include_router(
        self,
        router: "APIRouter",
        *,
        prefix: str | None = None,
        tags: list[str] | None = None,
        backend_id: str | None = None,
    ) -> None:
        """Mount a FastAPI sub-router.

        Default prefix is ``/plugins/<plugin_id>``. Backend plugins take effect
        on the next API process start; toggling ``enabled`` at runtime does not
        remove already-mounted routes — see docs/PLUGINS.md.
        """
        spec = BackendSpec(
            id=backend_id or self.manifest.id,
            router=router,
            prefix=prefix if prefix is not None else f"/plugins/{self.manifest.id}",
            tags=list(tags or [self.manifest.id]),
        )
        self._manager._register_backend(self.manifest.id, spec)

    def register_channel(self, spec: ChannelSpec) -> None:
        """Record a descriptor of an external channel process."""
        self._manager._register_channel(self.manifest.id, spec)
