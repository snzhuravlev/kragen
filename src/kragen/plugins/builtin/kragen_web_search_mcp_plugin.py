"""Plugin: web search MCP for the Cursor worker."""

from __future__ import annotations

import sys

from kragen.plugins.base import BasePlugin, MCPServerSpec, PluginManifest
from kragen.plugins.context import PluginContext


class KragenWebSearchMcpPlugin(BasePlugin):
    """Registers stdio MCP tools for web search and page fetch."""

    def __init__(self) -> None:
        super().__init__(
            PluginManifest(
                id="kragen-mcp-web-search",
                version="0.1.0",
                kind="tool",
                name="Kragen web search MCP",
                description="MCP tools for web search and page fetch with safe defaults.",
                author="Kragen",
                config_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "timeout_seconds": {"type": "number", "minimum": 1},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                        "max_content_chars": {"type": "integer", "minimum": 500, "maximum": 100000},
                        "safe_search": {"type": "string", "enum": ["off", "moderate", "strict"]},
                        "allow_http": {"type": "boolean"},
                        "allowed_domains": {"type": "array", "items": {"type": "string"}},
                    },
                },
            )
        )

    def setup(self, ctx: PluginContext) -> None:
        cfg = dict(ctx.config)
        env: dict[str, str] = {}
        if cfg.get("timeout_seconds") is not None:
            env["KRAGEN_WEB_SEARCH_TIMEOUT_SECONDS"] = str(cfg["timeout_seconds"])
        if cfg.get("max_results") is not None:
            env["KRAGEN_WEB_SEARCH_MAX_RESULTS"] = str(cfg["max_results"])
        if cfg.get("max_content_chars") is not None:
            env["KRAGEN_WEB_SEARCH_MAX_CONTENT_CHARS"] = str(cfg["max_content_chars"])
        if cfg.get("safe_search") is not None:
            env["KRAGEN_WEB_SEARCH_SAFE_SEARCH"] = str(cfg["safe_search"])
        if cfg.get("allow_http") is not None:
            env["KRAGEN_WEB_SEARCH_ALLOW_HTTP"] = "true" if bool(cfg["allow_http"]) else "false"
        allowed_domains = cfg.get("allowed_domains") or []
        if allowed_domains:
            env["KRAGEN_WEB_SEARCH_ALLOWED_DOMAINS"] = ",".join(str(value) for value in allowed_domains)
        ctx.register_mcp_server(
            MCPServerSpec(
                id="kragen-web-search",
                command=sys.executable,
                args=["-m", "kragen.mcp.kragen_web_search_mcp"],
                env=env,
            )
        )


def plugin() -> KragenWebSearchMcpPlugin:
    return KragenWebSearchMcpPlugin()
