"""Admin API: admin-only operations, audit and retrieval listings with scoping."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, text

from kragen.api.deps import AdminUserId, DbSession, UserId, ensure_workspace_access, is_admin_user
from kragen.api.schemas import AuditEventOut, RetrievalLogOut
from kragen.config import WorkerSettings, clear_settings_cache, get_config_yaml_path, get_settings
from kragen.models.core import AuditEvent
from kragen.models.retrieval import RetrievalLog
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

# YAML sections/keys masked before returning kragen.yaml over the admin API.
_SENSITIVE_YAML_PATHS: tuple[tuple[str, ...], ...] = (
    ("database", "url"),
    ("storage", "access_key"),
    ("storage", "secret_key"),
    ("auth", "jwt_secret"),
    ("telegram_channel", "bot_token"),
    ("telegram_channel", "webhook_secret_token"),
)
_MASK_VALUE = "***masked***"
_DSN_MASK_RE = re.compile(r"(?P<scheme>[a-z0-9+\-]+://[^:@\s/]+):[^@\s/]+@")


async def _run_command(
    args: list[str],
    *,
    timeout_seconds: int = 12,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a command asynchronously and return captured output and timeout flag."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
        timed_out = False
    except TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        stdout_bytes = b""
        stderr_bytes = b""
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (ProcessLookupError, TimeoutError):
            pass

    return {
        "exit_code": proc.returncode,
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "timed_out": timed_out,
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
    rendered = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    path.write_text(rendered, encoding="utf-8")


def _mask_dsn_password(dsn: str) -> str:
    """Replace password in a DSN-looking value with ``***masked***``.

    Leaves non-DSN strings untouched.
    """
    if "://" not in dsn or "@" not in dsn:
        return _MASK_VALUE
    return _DSN_MASK_RE.sub(r"\g<scheme>:" + _MASK_VALUE + "@", dsn, count=1)


def _mask_sensitive_yaml(text_content: str) -> str:
    """Load YAML, mask sensitive values, and render it back.

    On parse failure returns ``_MASK_VALUE`` to avoid leaking raw secrets.
    """
    try:
        data = yaml.safe_load(text_content)
    except yaml.YAMLError:
        return f"# {_MASK_VALUE}\n"

    if not isinstance(data, dict):
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

    for path in _SENSITIVE_YAML_PATHS:
        cursor: Any = data
        for key in path[:-1]:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(key)
        if not isinstance(cursor, dict):
            continue
        leaf = path[-1]
        if leaf not in cursor or cursor[leaf] is None:
            continue
        value = cursor[leaf]
        if isinstance(value, str) and "://" in value:
            cursor[leaf] = _mask_dsn_password(value)
        else:
            cursor[leaf] = _MASK_VALUE

    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


@router.get("/workers")
async def list_workers(admin_id: AdminUserId) -> list[dict[str, Any]]:
    """Placeholder worker registry until separate worker pool exists."""
    _ = admin_id
    return [
        {"id": "local-stub", "status": "idle", "runtime": "stub"},
    ]


@router.get("/logs")
async def get_recent_logs(
    admin_id: AdminUserId,
    limit: int = Query(default=800, ge=1, le=5000, description="Max lines to return (newest last)."),
) -> dict[str, Any]:
    """
    Recent structured log lines (JSON) from this API process.

    In-memory ring buffer only; multi-worker deployments see one worker per process.
    """
    _ = admin_id
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
async def clear_log_buffer(admin_id: AdminUserId) -> dict[str, bool]:
    """Clear the in-memory log buffer (does not affect stdout logging)."""
    _ = admin_id
    log_buffer.clear()
    return {"ok": True}


@router.get("/memory/status")
async def long_term_memory_status(db: DbSession, admin_id: AdminUserId) -> dict[str, Any]:
    """
    Verify PostgreSQL connectivity and tables used for worker prompt memory injection.

    Admin-only: this endpoint exposes the presence of internal tables and raw error text.
    """
    _ = admin_id
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
    workspace_id: uuid.UUID | None = Query(
        default=None,
        description="Scope events to a single workspace. Required for non-admin callers.",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[AuditEventOut]:
    """Recent audit events scoped by workspace membership (admin may list all)."""
    is_admin = is_admin_user(user_id)
    if workspace_id is None and not is_admin:
        raise HTTPException(
            status_code=400,
            detail="workspace_id query parameter is required",
        )
    if workspace_id is not None:
        await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)

    stmt = select(AuditEvent)
    if workspace_id is not None:
        stmt = stmt.where(AuditEvent.workspace_id == workspace_id)
    stmt = stmt.order_by(AuditEvent.created_at.desc()).limit(limit)

    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/retrieval/logs", response_model=list[RetrievalLogOut])
async def list_retrieval_logs(
    db: DbSession,
    user_id: UserId,
    workspace_id: uuid.UUID | None = Query(
        default=None,
        description="Scope retrieval logs to a single workspace. Required for non-admin callers.",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[RetrievalLogOut]:
    """Recent retrieval operations scoped by workspace membership."""
    is_admin = is_admin_user(user_id)
    if workspace_id is None and not is_admin:
        raise HTTPException(
            status_code=400,
            detail="workspace_id query parameter is required",
        )
    if workspace_id is not None:
        await ensure_workspace_access(db, user_id=user_id, workspace_id=workspace_id)

    stmt = select(RetrievalLog)
    if workspace_id is not None:
        stmt = stmt.where(RetrievalLog.workspace_id == workspace_id)
    stmt = stmt.order_by(RetrievalLog.created_at.desc()).limit(limit)

    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/config/kragen-yaml")
async def get_kragen_yaml_file(admin_id: AdminUserId) -> dict[str, Any]:
    """
    Return the resolved kragen.yaml contents with sensitive values masked.

    The response masks DSN passwords and secret fields (database URL, storage
    keys, JWT secret, Telegram tokens). This endpoint is admin-only.
    """
    _ = admin_id
    path: Path = get_config_yaml_path()
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Config file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot read config file: {exc}") from exc
    return {"path": str(path), "content": _mask_sensitive_yaml(raw)}


@router.get("/config/worker")
async def get_worker_config(admin_id: AdminUserId) -> dict[str, Any]:
    """Effective worker section (admin-only)."""
    _ = admin_id
    w = get_settings().worker
    return {
        "worker": w.model_dump(),
        "config_path": str(get_config_yaml_path()),
    }


@router.put("/config/worker")
async def put_worker_config(body: WorkerSettings, admin_id: AdminUserId) -> dict[str, Any]:
    """Replace the ``worker`` key in kragen.yaml (admin-only)."""
    _ = admin_id
    path = get_config_yaml_path()
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"Config file not found: {path}. Create configs/kragen.yaml before "
                "editing worker settings."
            ),
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
async def cursor_auth_status(admin_id: AdminUserId) -> dict[str, Any]:
    """Return Cursor Agent authentication status for the current host user."""
    _ = admin_id
    run = await _run_command(
        [_cursor_cli(), "agent", "status", "--format", "json"],
        timeout_seconds=10,
    )
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
async def cursor_auth_login(admin_id: AdminUserId) -> dict[str, Any]:
    """
    Start Cursor Agent login and return the browser URL for authentication.

    Admin-only: wraps a host-level CLI invocation.
    """
    status = await cursor_auth_status(admin_id)
    if status["ok"]:
        return {
            "ok": True,
            "already_authenticated": True,
            "message": "Cursor Agent is already authenticated.",
        }

    run = await _run_command(
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
