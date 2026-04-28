"""Plugin: scripts MCP (bash/python) for the Cursor worker."""

from __future__ import annotations

import sys

from kragen.plugins.base import BasePlugin, MCPServerSpec, PluginManifest
from kragen.plugins.context import PluginContext


class KragenScriptsMcpPlugin(BasePlugin):
    """Registers stdio MCP tools for running scripts in task workspace."""

    def __init__(self) -> None:
        super().__init__(
            PluginManifest(
                id="kragen-mcp-scripts",
                version="0.1.0",
                kind="tool",
                name="Kragen scripts MCP",
                description="MCP tools to run bash/python scripts inside task workspace.",
                author="Kragen",
            )
        )

    def setup(self, ctx: PluginContext) -> None:
        ctx.register_mcp_server(
            MCPServerSpec(
                id="kragen-scripts",
                command=sys.executable,
                args=["-m", "kragen.mcp.kragen_scripts_mcp"],
                env={},
            )
        )


def plugin() -> KragenScriptsMcpPlugin:
    return KragenScriptsMcpPlugin()
