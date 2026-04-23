"""Built-in example plugin: 'concise' skill.

Demonstrates the minimum viable shape of a Kragen plugin — a single ``SkillSpec``
registered through ``PluginContext``. Useful for smoke-testing the plugin
pipeline end-to-end without any external dependencies.
"""

from __future__ import annotations

from kragen.plugins.base import BasePlugin, PluginManifest, SkillSpec
from kragen.plugins.context import PluginContext


class ConciseSkillPlugin(BasePlugin):
    """Appends a 'be concise' instruction to every worker prompt."""

    def __init__(self) -> None:
        super().__init__(
            PluginManifest(
                id="kragen-skill-concise",
                version="0.1.0",
                kind="skill",
                name="Concise replies",
                description=(
                    "Built-in example skill. Instructs the worker to answer "
                    "in 3–5 bullet points with actionable next steps."
                ),
                author="Kragen",
            )
        )

    def setup(self, ctx: PluginContext) -> None:
        ctx.register_skill(
            SkillSpec(
                id="concise",
                title="Be concise and actionable",
                description="Prefer bullet points and explicit next steps.",
                priority=100,
                when="always",
                prompt=(
                    "Style directive: keep the reply under ~150 words. "
                    "Use 3–5 bullet points. End with a 'Next steps' list "
                    "when the user asked for something to do."
                ),
            )
        )


def plugin() -> ConciseSkillPlugin:
    """Entry-point target (see pyproject.toml)."""
    return ConciseSkillPlugin()
