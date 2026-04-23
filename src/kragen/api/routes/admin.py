"""Admin API: workers stub, audit and retrieval listings."""

import json
import os
import re
import subprocess
from typing import Any

from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, text

from kragen.api.deps import DbSession, UserId
from kragen.api.schemas import AuditEventOut, RetrievalLogOut
from kragen.config import WorkerSettings, clear_settings_cache, get_config_yaml_path, get_settings
from kragen.models.retrieval import RetrievalLog
from kragen.models.core import AuditEvent
from kragen.services import log_buffer

router = APIRouter(prefix="/admin", tags=["admin"])
LOGIN_URL_RE = re.compile(r"https://cursor\.com/\S+")

# Tables read by the worker long-term memory SQL (same schema as memory-mcp).
_MEMORY_STATUS_TABLES = (
    "session_summaries",
    "semantic_facts",
    "documents",
    "document_chunks",
)


def _run_command(
    args: list[str],
    *,
    timeout_seconds: int = 12,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a command and return captured output including timeout metadata."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timed_out": True,
        }


def _cursor_cli() -> str:
    """Resolve cursor executable path from settings."""
    return get_settings().worker.cursor_cli_path


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load YAML file as a mapping; empty or missing returns {}."""
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    data = yaml.safe_load(raw)
    return data if isinstance(data, dict) else {}


def _write_yaml_mapping(path: Path, data: dict[str, Any]) -> None:
    """Write YAML mapping to file (atomic replace via same path)."""
    text = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    path.write_text(text, encoding="utf-8")


@router.get("/workers")
async def list_workers() -> list[dict[str, Any]]:
    """Placeholder worker registry until separate worker pool exists."""
    return [
        {"id": "local-stub", "status": "idle", "runtime": "stub"},
    ]


@router.get("/logs")
async def get_recent_logs(
    user_id: UserId,
    limit: int = Query(default=800, ge=1, le=5000, description="Max lines to return (newest last)."),
) -> dict[str, Any]:
    """
    Recent structured log lines (JSON) from this API process.

    In-memory ring buffer only; multi-worker deployments see one worker per process.
    """
    _ = user_id
    lines = log_buffer.get_recent_lines(limit=limit)
    st = log_buffer.stats()
    return {
        "lines": lines,
        "limit_requested": limit,
        "lines_returned": len(lines),
        "buffered_in_memory": st["count"],
        "buffer_capacity": st["maxlen"],
    }


@router.post("/logs/clear")
async def clear_log_buffer(user_id: UserId) -> dict[str, bool]:
    """Clear the in-memory log buffer (does not affect stdout logging)."""
    _ = user_id
    log_buffer.clear()
    return {"ok": True}


@router.get("/memory/status")
async def long_term_memory_status(db: DbSession, user_id: UserId) -> dict[str, Any]:
    """
    Verify PostgreSQL connectivity and tables used for worker prompt memory injection.

    Use this from the same host as the API process. Cursor IDE agents cannot reach your DB
    from their sandbox; this endpoint reflects what the Kragen worker actually can query.
    """
    _ = user_id
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "database": "error",
            "error": str(exc),
            "hint": "Ensure DATABASE_URL is set and PostgreSQL is reachable from the API process.",
        }

    tables: dict[str, str] = {}
    all_ok = True
    for name in _MEMORY_STATUS_TABLES:
        try:
            await db.execute(text(f"SELECT 1 FROM {name} LIMIT 1"))
            tables[name] = "ok"
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            tables[name] = f"{type(exc).__name__}: {exc}"

    return {
        "ok": all_ok,
        "database": "connected",
        "tables": tables,
        "hint": None if all_ok else "Run Alembic migrations if tables are missing.",
    }


@router.get("/audit/events", response_model=list[AuditEventOut])
async def list_audit(
    db: DbSession,
    user_id: UserId,
    limit: int = 50,
) -> list[AuditEventOut]:
    """Recent audit events (scope by workspace in production)."""
    _ = user_id
    result = await db.execute(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit))
    return list(result.scalars().all())


@router.get("/retrieval/logs", response_model=list[RetrievalLogOut])
async def list_retrieval_logs(
    db: DbSession,
    user_id: UserId,
    limit: int = 50,
) -> list[RetrievalLogOut]:
    """Recent retrieval operations."""
    _ = user_id
    result = await db.execute(
        select(RetrievalLog).order_by(RetrievalLog.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


@router.get("/config/kragen-yaml")
async def get_kragen_yaml_file(user_id: UserId) -> dict[str, Any]:
    """
    Return the raw contents of the resolved kragen.yaml file (same path as app settings).

    Used by the Web UI Configuration tab. Requires authentication like other /admin routes.
    """
    _ = user_id
    path: Path = get_config_yaml_path()
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Config file not found: {path}")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read config file: {exc}") from exc
    return {"path": str(path), "content": content}


@router.get("/config/worker")
async def get_worker_config(user_id: UserId) -> dict[str, Any]:
    """
    Effective worker section (Cursor agent timeouts, paths, memory context).

    Values reflect env + YAML merge. Updating via PUT writes `worker` into kragen.yaml and
    clears the settings cache so new tasks pick up changes without restarting the process.
    """
    _ = user_id
    w = get_settings().worker
    return {
        "worker": w.model_dump(),
        "config_path": str(get_config_yaml_path()),
    }


@router.put("/config/worker")
async def put_worker_config(body: WorkerSettings, user_id: UserId) -> dict[str, Any]:
    """
    Replace the `worker` key in the resolved kragen.yaml file.

    Other YAML sections are preserved. Environment variables such as ``KRAGEN_WORKER__TIMEOUT_SECONDS``
    still override file values on load.
    """
    _ = user_id
    path = get_config_yaml_path()
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Config file not found: {path}. Create configs/kragen.yaml before editing worker settings.",
        )
    try:
        data = _read_yaml_mapping(path)
        data["worker"] = body.model_dump()
        _write_yaml_mapping(path, data)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot write config file: {exc}") from exc
    clear_settings_cache()
    return {
        "ok": True,
        "path": str(path),
        "worker": get_settings().worker.model_dump(),
    }


@router.get("/cursor-auth/status")
async def cursor_auth_status(user_id: UserId) -> dict[str, Any]:
    """Return Cursor Agent authentication status for the current host user."""
    _ = user_id
    run = _run_command([_cursor_cli(), "agent", "status", "--format", "json"], timeout_seconds=10)
    raw = (run["stdout"] or "").strip()
    parsed: dict[str, Any] | None = None
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
    ok = bool(parsed and parsed.get("isAuthenticated"))
    return {
        "ok": ok,
        "parsed": parsed,
        "raw_stdout": run["stdout"],
        "raw_stderr": run["stderr"],
        "exit_code": run["exit_code"],
    }


@router.post("/cursor-auth/login")
async def cursor_auth_login(user_id: UserId) -> dict[str, Any]:
    """
    Start Cursor Agent login and return the browser URL for authentication.

    The command may wait for browser completion; the API captures initial output
    and returns quickly, then the client can poll /cursor-auth/status.
    """
    _ = user_id

    # Fast path: already authenticated.
    status = await cursor_auth_status(user_id)
    if status["ok"]:
        return {
            "ok": True,
            "already_authenticated": True,
            "message": "Cursor Agent is already authenticated.",
        }

    run = _run_command(
        [_cursor_cli(), "agent", "login"],
        timeout_seconds=15,
        extra_env={"NO_OPEN_BROWSER": "1"},
    )
    merged_output = f"{run['stdout']}\n{run['stderr']}".strip()
    match = LOGIN_URL_RE.search(merged_output)
    return {
        "ok": bool(match),
        "already_authenticated": False,
        "authentication_url": match.group(0) if match else None,
        "timed_out": run["timed_out"],
        "exit_code": run["exit_code"],
        "output_excerpt": merged_output[-4000:],
        "message": (
            "Open authentication_url in browser and complete login, then refresh status."
            if match
            else "Could not extract login URL. Check output_excerpt."
        ),
    }
