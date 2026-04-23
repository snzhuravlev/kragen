"""Unit tests for the Kragen plugin subsystem."""

from __future__ import annotations

import json
from pathlib import Path

from kragen.plugins.base import (
    BasePlugin,
    MCPServerSpec,
    PluginManifest,
    SkillSpec,
)
from kragen.plugins.context import PluginContext
from kragen.plugins.manager import PluginManager


class _StubPlugin(BasePlugin):
    """Minimal plugin for tests: one always-on skill + one MCP server."""

    def __init__(self) -> None:
        super().__init__(
            PluginManifest(
                id="kragen-test-stub",
                version="0.0.1",
                kind="composite",
                name="Test stub",
            )
        )

    def setup(self, ctx: PluginContext) -> None:
        ctx.register_skill(
            SkillSpec(
                id="stub-skill",
                title="Stub",
                prompt="STUB_PROMPT_FRAGMENT",
                priority=50,
                when="always",
            )
        )
        ctx.register_mcp_server(
            MCPServerSpec(
                id="stub-mcp",
                command="python",
                args=["-c", "print('hi')"],
            )
        )


def _make_manager_with_plugin(plugin: BasePlugin) -> PluginManager:
    """Bypass entry-point discovery: inject a record directly."""
    manager = PluginManager()
    from kragen.plugins.manager import _PluginRecord

    record = _PluginRecord(
        manifest=plugin.manifest,
        instance=plugin,
        config={},
        enabled=True,
        ep_name="test",
        dist_name=None,
    )
    manager._records[plugin.manifest.id] = record
    manager._run_setup_for_enabled()
    manager._initialized = True
    return manager


def test_active_skills_appear_in_prompt() -> None:
    manager = _make_manager_with_plugin(_StubPlugin())
    composed = manager.compose_prompt(base="BASE", user_message="hello")
    assert "BASE" in composed
    assert "STUB_PROMPT_FRAGMENT" in composed
    assert "skill:stub-skill" in composed


async def test_materialize_mcp_config_writes_cursor_json(tmp_path: Path) -> None:
    manager = _make_manager_with_plugin(_StubPlugin())

    result_path = await manager.materialize_mcp_config(tmp_path)

    assert result_path is not None
    assert result_path == tmp_path / ".cursor" / "mcp.json"

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert "mcpServers" in payload
    assert "stub-mcp" in payload["mcpServers"]
    entry = payload["mcpServers"]["stub-mcp"]
    assert entry["command"] == "python"
    assert entry["args"] == ["-c", "print('hi')"]


def test_toggle_disable_clears_specs() -> None:
    manager = _make_manager_with_plugin(_StubPlugin())
    assert manager.active_skills() != []
    manager.set_enabled("kragen-test-stub", False)
    assert manager.active_skills() == []
    assert manager.active_mcp_servers() == []

    manager.set_enabled("kragen-test-stub", True)
    assert len(manager.active_skills()) == 1


def test_mention_trigger_only_on_match() -> None:
    class _MentionPlugin(BasePlugin):
        def __init__(self) -> None:
            super().__init__(
                PluginManifest(
                    id="kragen-mention",
                    version="0.0.1",
                    kind="skill",
                )
            )

        def setup(self, ctx: PluginContext) -> None:
            ctx.register_skill(
                SkillSpec(
                    id="jira",
                    title="Jira helper",
                    prompt="JIRA_HINT",
                    when="mention",
                    triggers=["jira", "ticket"],
                )
            )

    manager = _make_manager_with_plugin(_MentionPlugin())

    assert manager.active_skills(user_message="how is the weather") == []
    skills = manager.active_skills(user_message="Check my JIRA ticket please")
    assert [s.id for s in skills] == ["jira"]
