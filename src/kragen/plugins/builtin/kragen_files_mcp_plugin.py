"""Plugin: Kragen files MCP (import_url) for the Cursor worker."""

from __future__ import annotations

import sys

from kragen.plugins.base import BasePlugin, MCPServerSpec, PluginManifest
from kragen.plugins.context import PluginContext


class KragenFilesMcpPlugin(BasePlugin):
    """Registers a stdio MCP server that calls POST /files/import."""

    def __init__(self) -> None:
        super().__init__(
            PluginManifest(
                id="kragen-mcp-kragen-files",
                version="0.1.0",
                kind="tool",
                name="Kragen files MCP",
                description="MCP tools to import URLs into Kragen logical file storage.",
                author="Kragen",
            )
        )

    def setup(self, ctx: PluginContext) -> None:
        ctx.register_mcp_server(
            MCPServerSpec(
                id="kragen-files",
                command=sys.executable,
                args=["-m", "kragen.mcp.kragen_files_mcp"],
                env={},
            )
        )


def plugin() -> KragenFilesMcpPlugin:
    return KragenFilesMcpPlugin()
