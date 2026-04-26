"""Skill: route file downloads and /library/... requests to Kragen API, not local disk."""

from __future__ import annotations

from kragen.plugins.base import BasePlugin, PluginManifest, SkillSpec
from kragen.plugins.context import PluginContext


class KragenStorageSkillPlugin(BasePlugin):
    """
    Instructs the worker to use server-side import and Kragen MCP for logical paths.

    Activates on mention when the user message contains storage/download-related tokens.
    """

    def __init__(self) -> None:
        super().__init__(
            PluginManifest(
                id="kragen-skill-kragen-storage",
                version="0.1.0",
                kind="skill",
                name="Kragen logical file storage",
                description=(
                    "Tells the agent to use POST /files/import and kragen-files MCP instead of "
                    "local workspace mkdir for paths like /library/..."
                ),
                author="Kragen",
            )
        )

    def setup(self, ctx: PluginContext) -> None:
        ctx.register_skill(
            SkillSpec(
                id="kragen-storage",
                title="Use Kragen storage for /library/ and URL imports",
                description="Prefer API import and MCP; avoid local-only workflows.",
                priority=40,
                when="mention",
                triggers=[
                    "/library",
                    "library",
                    "import",
                    "upload",
                    "download",
                    "http://",
                    "https://",
                    "pdf",
                    "postgresql",
                    "mcp",
                    "url",
                    "storage",
                    "kragen",
                    "скач",
                    "каталог",
                    "файл",
                    "документ",
                    "полож",
                    "загруз",
                ],
                prompt=(
                    "Kragen file placement (MANDATORY for paths like /library/... or saving from a URL):\n"
                    "1) Do NOT treat /library/... or similar as a subfolder to create under the "
                    "Cursor --workspace on disk. Do NOT use mkdir, touch .gitkeep, or curl/wget to "
                    "the public internet from the local shell to satisfy a request to 'put' a file in "
                    "Kragen logical storage — that is the wrong target.\n"
                    "2) The download from the internet is performed by the Kragen API (server-side), "
                    "not by your process fetching bytes from the site. You do not need outbound "
                    "internet access in this subprocess if you call the API or MCP tool.\n"
                    "3) If KRAGEN_TASK_TOKEN, KRAGEN_API_URL, and KRAGEN_WORKSPACE_ID are in the "
                    "environment, use the 'kragen-files' MCP tool `import_url` with the exact HTTPS URL "
                    "and dest_folder_path (e.g. /library/postgresql) and optional filename. Approve the "
                    "MCP if prompted. If MCP is unavailable, use curl against KRAGEN_API_URL with header "
                    "Authorization: Bearer $KRAGEN_TASK_TOKEN, POST /files/import, JSON body: "
                    '{ "url", "workspace_id" (string UUID), "dest_folder_path" } and optional "filename".\n'
                    "4) After success, report the created storage entry (path_cache, id) from the response.\n"
                ),
            )
        )


def plugin() -> KragenStorageSkillPlugin:
    return KragenStorageSkillPlugin()
