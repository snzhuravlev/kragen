"""agentctl: minimal CLI for sessions, chat, and uploads."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any

import httpx


def _base_url() -> str:
    return os.environ.get("KRAGEN_API_URL", "http://127.0.0.1:8000").rstrip("/")


def _headers() -> dict[str, str]:
    token = os.environ.get("KRAGEN_TOKEN", "")
    if not token:
        print("Set KRAGEN_TOKEN to a user UUID (or enable AUTH_DISABLED on server).", file=sys.stderr)
        sys.exit(1)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _stream_task(task_id: str) -> None:
    url = f"{_base_url()}/tasks/{task_id}/stream"
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", url, headers=_headers()) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    raw = line.removeprefix("data: ").strip()
                    if raw == "{}":
                        continue
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        print(raw, end="", flush=True)
                    else:
                        if isinstance(parsed, str):
                            print(parsed, end="", flush=True)
                        elif isinstance(parsed, dict) and not parsed:
                            continue
                        else:
                            print(str(parsed), end="", flush=True)
    print()


def cmd_ask(args: argparse.Namespace) -> None:
    """Send a one-shot user message (requires session id in env)."""
    session_id = args.session or os.environ.get("KRAGEN_SESSION_ID")
    if not session_id:
        print("Provide --session or KRAGEN_SESSION_ID", file=sys.stderr)
        sys.exit(1)
    workspace = args.workspace or os.environ.get("KRAGEN_WORKSPACE_ID")
    if not workspace:
        print("Provide --workspace or KRAGEN_WORKSPACE_ID", file=sys.stderr)
        sys.exit(1)

    payload = {"role": "user", "content": args.text, "metadata": {}}
    r = httpx.post(
        f"{_base_url()}/sessions/{session_id}/messages",
        headers=_headers(),
        json=payload,
        timeout=120.0,
    )
    r.raise_for_status()
    data = r.json()
    task_id = data["task"]["id"]
    print(f"task_id={task_id}")
    asyncio.run(_stream_task(task_id))


def cmd_session_list(_args: argparse.Namespace) -> None:
    """List sessions (not yet implemented server-side listing)."""
    print("Not implemented: use SQL or future GET /sessions?workspace_id=")


def cmd_upload(args: argparse.Namespace) -> None:
    """Upload a file into document store."""
    workspace = args.workspace or os.environ.get("KRAGEN_WORKSPACE_ID")
    if not workspace:
        print("Provide --workspace or KRAGEN_WORKSPACE_ID", file=sys.stderr)
        sys.exit(1)
    path = args.file
    with open(path, "rb") as f:
        files = {"file": (os.path.basename(path), f)}
        data = {"workspace_id": workspace}
        headers = {"Authorization": _headers()["Authorization"]}
        r = httpx.post(
            f"{_base_url()}/files/upload",
            headers=headers,
            data=data,
            files=files,
            timeout=120.0,
        )
    r.raise_for_status()
    print(json.dumps(r.json(), indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentctl")
    sub = p.add_subparsers(dest="cmd", required=True)

    ask = sub.add_parser("ask", help="Send a message and stream the task output")
    ask.add_argument("text")
    ask.add_argument("--session", default=None)
    ask.add_argument("--workspace", default=None)
    ask.set_defaults(func=cmd_ask)

    sl = sub.add_parser("session", help="Session commands")
    sl_sub = sl.add_subparsers(dest="session_cmd", required=True)
    sl_list = sl_sub.add_parser("list")
    sl_list.set_defaults(func=cmd_session_list)

    up = sub.add_parser("upload", help="Upload a document")
    up.add_argument("file")
    up.add_argument("--workspace", default=None)
    up.set_defaults(func=cmd_upload)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    func: Any = getattr(args, "func", None)
    if not func:
        parser.print_help()
        sys.exit(1)
    func(args)


if __name__ == "__main__":
    main()
