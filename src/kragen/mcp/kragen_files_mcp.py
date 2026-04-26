"""Stdio MCP server: Kragen logical file storage (import, ensure path, upload)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kragen-files")


def _env_ok() -> tuple[str, str, str] | None:
    base = (os.environ.get("KRAGEN_API_URL") or "").rstrip("/")
    token = os.environ.get("KRAGEN_TASK_TOKEN", "")
    ws = os.environ.get("KRAGEN_WORKSPACE_ID", "")
    if not base or not token or not ws:
        return None
    return base, token, ws


@mcp.tool()
def import_url(
    url: str,
    dest_folder_path: str,
    filename: str | None = None,
) -> str:
    """
    Download a file from a public HTTP(S) URL into Kragen logical storage (POST /files/import).

    Uses dest_folder_path (e.g. /library/postgresql) plus optional filename. Requires task env.
    """
    ok = _env_ok()
    if ok is None:
        return (
            "error: KRAGEN_API_URL, KRAGEN_TASK_TOKEN, and KRAGEN_WORKSPACE_ID must be set "
            "(injected by the task worker for enabled runs)."
        )
    base, token, ws = ok
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
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


@mcp.tool()
def ensure_folder_path(path: str) -> str:
    """
    Create missing folders for a logical path (POST /files/folders/ensure, mkdir -p).
    """
    ok = _env_ok()
    if ok is None:
        return "error: missing KRAGEN_API_URL, KRAGEN_TASK_TOKEN, or KRAGEN_WORKSPACE_ID"
    base, token, ws = ok
    headers: dict[str, str] = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"workspace_id": ws, "path": path}
    with httpx.Client(timeout=60.0) as client:
        r = client.post(f"{base}/files/folders/ensure", json=payload, headers=headers)
    if r.is_success:
        return json.dumps(r.json(), indent=2)
    return f"error: HTTP {r.status_code} {r.text[:2000]}"


@mcp.tool()
def upload_from_workspace(
    local_relative_path: str,
    parent_id: str | None = None,
) -> str:
    """
    Upload a file from the Cursor task workspace on disk to Kragen storage (POST /files/upload).

    Paths are relative to KRAGEN_TASK_WORKSPACE_DIR and must not escape that directory.
    parent_id: optional existing folder entry UUID, or null for the workspace root.
    """
    ok = _env_ok()
    if ok is None:
        return "error: missing API env (KRAGEN_API_URL, KRAGEN_TASK_TOKEN, KRAGEN_WORKSPACE_ID)"
    base, token, ws = ok
    root = os.environ.get("KRAGEN_TASK_WORKSPACE_DIR", "")
    if not root:
        return "error: KRAGEN_TASK_WORKSPACE_DIR is not set (expected from the task worker)"
    try:
        full = (Path(root) / local_relative_path).resolve()
        root_p = Path(root).resolve()
        full.relative_to(root_p)
    except ValueError:
        return "error: path escapes KRAGEN_TASK_WORKSPACE_DIR"
    if not full.is_file():
        return f"error: not a file: {local_relative_path!r}"
    name = full.name
    with open(full, "rb") as fh:
        data = fh.read()
    headers = {"Authorization": f"Bearer {token}"}
    files = {"file": (name, data)}
    form: dict[str, str] = {
        "workspace_id": str(ws),
        "create_document": "true",
    }
    if parent_id:
        form["parent_id"] = str(parent_id)
    with httpx.Client(timeout=300.0) as client:
        r = client.post(
            f"{base}/files/upload",
            data=form,
            files=files,
            headers=headers,
        )
    if r.is_success:
        return json.dumps(r.json(), indent=2)
    return f"error: HTTP {r.status_code} {r.text[:2000]}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
