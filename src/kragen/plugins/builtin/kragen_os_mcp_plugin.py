"""Plugin: OS command MCP for the Cursor worker."""

from __future__ import annotations

import sys

from kragen.plugins.base import BasePlugin, MCPServerSpec, PluginManifest
from kragen.plugins.context import PluginContext


class KragenOsMcpPlugin(BasePlugin):
    """Registers stdio MCP tools for running OS commands in task workspace."""

    def __init__(self) -> None:
        super().__init__(
            PluginManifest(
                id="kragen-mcp-os",
                version="0.1.0",
                kind="tool",
                name="Kragen OS MCP",
                description="MCP tools to run OS commands inside task workspace.",
                author="Kragen",
                config_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "security_profile": {
                            "type": "string",
                            "enum": ["open_dev", "balanced", "strict"],
                        },
                        "max_timeout_seconds": {"type": "integer", "minimum": 1},
                        "max_output_bytes": {"type": "integer", "minimum": 1000},
                        "allowed_command_prefixes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "allowed_env_prefixes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            )
        )

    def setup(self, ctx: PluginContext) -> None:
        cfg = dict(ctx.config)
        env: dict[str, str] = {}
        if cfg.get("security_profile"):
            env["KRAGEN_OS_SECURITY_PROFILE"] = str(cfg["security_profile"])
        if cfg.get("max_timeout_seconds") is not None:
            env["KRAGEN_OS_MAX_TIMEOUT_SECONDS"] = str(cfg["max_timeout_seconds"])
        if cfg.get("max_output_bytes") is not None:
            env["KRAGEN_OS_MAX_OUTPUT_BYTES"] = str(cfg["max_output_bytes"])
        allowed_command_prefixes = cfg.get("allowed_command_prefixes") or []
        if allowed_command_prefixes:
            env["KRAGEN_OS_ALLOWED_COMMAND_PREFIXES"] = ",".join(
                str(value) for value in allowed_command_prefixes
            )
        allowed_env_prefixes = cfg.get("allowed_env_prefixes") or []
        if allowed_env_prefixes:
            env["KRAGEN_OS_ALLOWED_ENV_PREFIXES"] = ",".join(
                str(value) for value in allowed_env_prefixes
            )
        ctx.register_mcp_server(
            MCPServerSpec(
                id="kragen-os",
                command=sys.executable,
                args=["-m", "kragen.mcp.kragen_os_mcp"],
                env=env,
            )
        )


def plugin() -> KragenOsMcpPlugin:
    return KragenOsMcpPlugin()
