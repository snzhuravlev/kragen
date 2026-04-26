"""Stdio MCP server: import files into Kragen logical storage via POST /files/import."""

from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kragen-files")


@mcp.tool()
def import_url(
    url: str,
    dest_folder_path: str,
    filename: str | None = None,
) -> str:
    """
    Download a file from a public HTTP(S) URL into Kragen logical storage.

    Uses KRAGEN_API_URL, KRAGEN_TASK_TOKEN, and KRAGEN_WORKSPACE_ID from the environment
    (injected by the task worker when the kragen-mcp-kragen-files plugin is enabled).
    """
    base = (os.environ.get("KRAGEN_API_URL") or "").rstrip("/")
    token = os.environ.get("KRAGEN_TASK_TOKEN", "")
    ws = os.environ.get("KRAGEN_WORKSPACE_ID", "")
    if not base or not token or not ws:
        return (
            "error: KRAGEN_API_URL, KRAGEN_TASK_TOKEN, and KRAGEN_WORKSPACE_ID must be set "
            "(the Kragen worker injects these for enabled tasks)."
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload: dict[str, object] = {
        "url": url,
        "workspace_id": ws,
        "dest_folder_path": dest_folder_path,
        "create_document": True,
    }
    if filename:
        payload["filename"] = filename
    with httpx.Client(timeout=300.0) as client:
        r = client.post(f"{base}/files/import", json=payload, headers=headers)
    if r.is_success:
        return json.dumps(r.json(), indent=2)
    return f"error: HTTP {r.status_code} {r.text[:2000]}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
