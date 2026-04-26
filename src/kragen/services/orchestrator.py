"""
Task orchestrator: builds context, launches Cursor worker.

Channels must remain thin; reasoning stays here and in the worker runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aioboto3
from botocore.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from kragen.config import KragenSettings, api_public_base_url, get_settings
from kragen.models.core import Message, Task
from kragen.logging_config import get_logger
from kragen.plugins.manager import get_plugin_manager
from kragen.services import task_token, task_stream
from kragen.services.audit_service import write_audit
from kragen.services.task_queue import TaskJob, enqueue

logger = get_logger(__name__)
_S3_PATH_STYLE_CONFIG = Config(s3={"addressing_style": "path"})


def _format_exception_for_user(exc: BaseException) -> str:
    """
    Human-readable exception text for UI and task.error.

    SQLAlchemy's NoResultFound and similar use an empty str(exc), which produced
    bare \"[worker-error]\" in the web UI.
    """
    name = type(exc).__name__
    msg = str(exc).strip()
    if msg in ("", "()"):
        msg = ""
    if msg:
        return f"{name}: {msg}"
    rep = repr(exc)
    if rep and rep not in (name, f"{name}()", f"{name}('')"):
        return f"{name}: {rep}"
    return name


_BATCH_MAX_LINES = 8
_BATCH_MAX_CHARS = 4096

_LOAD_MEMORY_SQL = text(
    """
    SELECT
      (SELECT summary_text FROM session_summaries
       WHERE session_id = :session_id ORDER BY updated_at DESC LIMIT 1) AS summary_text,
      COALESCE(
        (SELECT json_agg(json_build_object(
            'entity', f.entity,
            'fact_text', f.fact_text,
            'source_ref', f.source_ref
        ))
        FROM (
          SELECT entity, fact_text, source_ref FROM semantic_facts
          WHERE workspace_id = :workspace_id
            AND (
                  fact_text ILIKE '%' || :query || '%'
               OR entity ILIKE '%' || :query || '%'
               OR to_tsvector('simple', fact_text) @@ plainto_tsquery('simple', :query)
            )
          ORDER BY updated_at DESC
          LIMIT :top_k
        ) f),
        '[]'::json
      )::text AS facts_json,
      COALESCE(
        (SELECT json_agg(json_build_object(
            'source_ref', source_label,
            'content', content_snip
        ))
        FROM (
          SELECT COALESCE(d.title, d.source_ref, 'document') AS source_label,
                 LEFT(dc.content, 420) AS content_snip
          FROM document_chunks dc
          INNER JOIN documents d ON d.id = dc.document_id
          WHERE d.workspace_id = :workspace_id
            AND (
                  dc.content ILIKE '%' || :query || '%'
               OR to_tsvector('simple', dc.content) @@ plainto_tsquery('simple', :query)
            )
          ORDER BY dc.created_at DESC
          LIMIT :top_k
        ) c),
        '[]'::json
      )::text AS chunks_json
    """
)


def _settings() -> KragenSettings:
    """Fresh settings (supports cache clear after kragen.yaml updates)."""
    return get_settings()


def _workspace_path(workspace_id: uuid.UUID) -> Path:
    """Return and ensure a deterministic workspace path for Cursor Agent."""
    base = Path(_settings().worker.workspace_root).expanduser().resolve()
    path = base / str(workspace_id)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # Fallback for local runs where /var/lib/... is not writable.
        fallback = (Path.cwd() / ".kragen" / "workspaces" / str(workspace_id)).resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return path


def _build_prompt(
    *,
    session_id: uuid.UUID,
    workspace_id: uuid.UUID,
    api_public_url: str,
    context_messages: list[Message],
    user_message: str,
    memory_context: str,
    runtime_checks_context: str,
    memory_load_failed: bool = False,
) -> str:
    """Build a compact prompt from recent session history."""
    history_lines: list[str] = []
    for msg in context_messages:
        history_lines.append(f"[{msg.role}] {msg.content}")
    history = "\n".join(history_lines[-20:])
    if memory_load_failed:
        memory_block = (
            "Long-term memory lookup failed: the worker could not query PostgreSQL. "
            "Check API logs and GET /admin/memory/status (Diagnostics in the Web UI)."
        )
    else:
        memory_block = memory_context.strip() or "No relevant long-term memory found."
    channel_policy = ""
    if not _settings().channels.openclaw_enabled:
        channel_policy = (
            "Channel policy: OpenClaw channel is disabled in this environment. "
            "Do not call OpenClaw MCP tools or rely on OpenClaw routes.\n\n"
        )
    storage_model = (
        f"Workspace ID: {workspace_id}\n"
        f"Kragen API base URL: {api_public_url}\n"
        "Logical storage paths (for example /library/postgresql) are entries in Kragen object storage; "
        "they are not local paths under the Cursor --workspace directory. "
        "To put a file from a known public URL into logical storage, use POST /files/import with a "
        "Bearer token (or the kragen-files MCP import_url). Task tokens (files:task) also allow "
        "POST /files/folders, POST /files/folders/ensure, and POST /files/upload (see MCP: "
        "ensure_folder_path, upload_from_workspace). When KRAGEN_TASK_TOKEN is set, use it as the "
        "Bearer value. KRAGEN_TASK_WORKSPACE_DIR is the Cursor on-disk workspace for uploads.\n\n"
    )
    return (
        "You are the execution agent for Kragen Web channel.\n"
        "Provide concise, actionable answers.\n"
        "If you need tools, prefer MCP servers configured in Cursor.\n\n"
        f"{channel_policy}"
        f"{storage_model}"
        f"Session ID: {session_id}\n"
        "Long-term memory context:\n"
        f"{memory_block}\n\n"
        "Runtime checks (host-side):\n"
        f"{runtime_checks_context.strip() or 'No host runtime checks were requested for this message.'}\n\n"
        "Recent context:\n"
        f"{history}\n\n"
        "Current user request:\n"
        f"{user_message}\n"
    )


def _cursor_command(prompt: str, workspace_path: Path) -> list[str]:
    """Build Cursor Agent command line."""
    command = [
        _settings().worker.cursor_cli_path,
        "agent",
        "--print",
        "--output-format",
        "text",
        "--trust",
        "--workspace",
        str(workspace_path),
        prompt,
    ]
    # Optional override for advanced deployments:
    # KRAGEN_WORKER_COMMAND supports shell-style string and replaces default command.
    override = os.environ.get("KRAGEN_WORKER_COMMAND")
    if override:
        return shlex.split(override) + [prompt]
    return command


def _is_transient_error(text: str) -> bool:
    """Return True for retryable transient worker errors."""
    lower = text.lower()
    transient_markers = (
        "timeout",
        "timed out",
        "temporarily unavailable",
        "connection reset",
        "connection refused",
        "network error",
        "rate limit",
        "econnreset",
        "502",
        "503",
        "504",
    )
    return any(marker in lower for marker in transient_markers)


def _is_mcp_approval_error(text: str) -> bool:
    """Detect MCP approval/permission denials to provide actionable guidance."""
    lower = text.lower()
    return "mcp" in lower and any(
        m in lower
        for m in (
            "approval",
            "permission",
            "declined",
            "rejected",
            "not allowed",
            "denied",
        )
    )


def _parse_json_rows(raw: object) -> list[dict[str, object]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    return []


async def _phase(task_stream_id: str, message: str) -> None:
    """Emit a short progress line to the task SSE stream (shown in Live Stream)."""
    await task_stream.push_chunk(task_stream_id, f"[kragen] {message}\n")


async def _terminate_cursor_subprocess(proc: asyncio.subprocess.Process) -> None:
    """
    Kill subprocess after timeout; avoid raising ProcessLookupError when the process
    already exited (race between wait_for cancellation and natural exit).
    """
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=30.0)
    except (ProcessLookupError, asyncio.TimeoutError):
        pass


async def _read_stream_lines(
    *,
    stream: asyncio.StreamReader,
    task_stream_id: str,
    collect: list[str],
    prefix: str = "",
) -> None:
    """Read subprocess stream line by line, batching SSE pushes to reduce event-loop churn."""
    batch: list[str] = []
    char_count = 0

    async def flush() -> None:
        nonlocal batch, char_count
        if not batch:
            return
        await task_stream.push_chunk(task_stream_id, "".join(batch))
        batch = []
        char_count = 0

    while True:
        line = await stream.readline()
        if not line:
            await flush()
            break
        text = line.decode("utf-8", errors="replace")
        collect.append(text)
        chunk = f"{prefix}{text}"
        batch.append(chunk)
        char_count += len(chunk)
        if len(batch) >= _BATCH_MAX_LINES or char_count >= _BATCH_MAX_CHARS:
            await flush()


async def _inject_kragen_files_mcp_env(
    workspace_path: Path, env: dict[str, str], server_key: str = "kragen-files"
) -> None:
    """Merge per-task env into the kragen-files MCP entry in .cursor/mcp.json if present."""
    mcp_file = workspace_path / ".cursor" / "mcp.json"
    if not mcp_file.is_file():
        return

    def _apply() -> None:
        data = json.loads(mcp_file.read_text(encoding="utf-8"))
        servers = data.get("mcpServers")
        if not isinstance(servers, dict) or server_key not in servers:
            return
        entry = servers[server_key]
        if not isinstance(entry, dict):
            return
        base = entry.get("env")
        if not isinstance(base, dict):
            base = {}
        merged = {str(k): str(v) for k, v in base.items()}
        merged.update(env)
        entry["env"] = merged
        mcp_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    await asyncio.to_thread(_apply)


async def _run_cursor_attempt(
    *,
    command: list[str],
    task_stream_id: str,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> dict[str, str | int | bool]:
    """Run one Cursor Agent attempt and stream stdout/stderr."""
    child_env = {**os.environ, **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    out_lines: list[str] = []
    err_lines: list[str] = []
    try:
        await asyncio.wait_for(
            asyncio.gather(
                _read_stream_lines(stream=proc.stdout, task_stream_id=task_stream_id, collect=out_lines),
                _read_stream_lines(
                    stream=proc.stderr,
                    task_stream_id=task_stream_id,
                    collect=err_lines,
                    prefix="[cursor-agent stderr] ",
                ),
                proc.wait(),
            ),
            timeout=timeout_seconds,
        )
        timed_out = False
    except TimeoutError:
        timed_out = True
        await _terminate_cursor_subprocess(proc)
        await task_stream.push_chunk(
            task_stream_id,
            f"[worker-timeout] Cursor agent exceeded {timeout_seconds}s and was terminated.\n",
        )

    return {
        "exit_code": int(proc.returncode if proc.returncode is not None else -1),
        "stdout": "".join(out_lines).strip(),
        "stderr": "".join(err_lines).strip(),
        "timed_out": timed_out,
    }


async def _load_long_term_memory_context(
    *,
    db: AsyncSession,
    session_id: uuid.UUID,
    workspace_id: uuid.UUID,
    query: str,
    top_k: int,
) -> tuple[str, bool]:
    """
    Build a compact memory context for prompt injection.

    Uses the same memory tables as memory-mcp:
    - session_summaries
    - semantic_facts
    - documents + document_chunks

    Returns (text_for_prompt, load_failed). load_failed is True only when the SQL query errors.
    """
    query = query.strip()
    if not query:
        return "", False

    try:
        result = await db.execute(
            _LOAD_MEMORY_SQL,
            {"session_id": session_id, "workspace_id": workspace_id, "query": query, "top_k": top_k},
        )
        mem_row = result.mappings().first()
        if not mem_row:
            return "", False
        summary_row = {"summary_text": mem_row.get("summary_text")}
        facts_rows = _parse_json_rows(mem_row.get("facts_json"))
        chunk_rows = _parse_json_rows(mem_row.get("chunks_json"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "memory_context_load_failed",
            session_id=str(session_id),
            workspace_id=str(workspace_id),
            error=str(exc),
        )
        return "", True

    lines: list[str] = []
    if summary_row and summary_row.get("summary_text"):
        lines.append("Latest session summary:")
        lines.append(str(summary_row["summary_text"])[:1200])

    if facts_rows:
        lines.append("Relevant semantic facts:")
        for fact_row in facts_rows:
            entity = str(fact_row.get("entity") or "unknown")
            fact = str(fact_row.get("fact_text") or "")
            source_ref = fact_row.get("source_ref")
            if source_ref:
                lines.append(f"- [{entity}] {fact} (source: {source_ref})")
            else:
                lines.append(f"- [{entity}] {fact}")

    if chunk_rows:
        lines.append("Relevant document snippets:")
        for chunk_row in chunk_rows:
            source_ref = str(chunk_row.get("source_ref") or "document")
            content = str(chunk_row.get("content") or "")
            lines.append(f"- ({source_ref}) {content}")

    return "\n".join(lines), False


def _looks_like_storage_check_query(text: str) -> bool:
    """Return True when user asks to verify storage/MinIO reachability."""
    lowered = text.lower()
    markers = (
        "minio",
        "s3",
        "object storage",
        "storage",
        "bucket",
        "сторедж",
        "хранилищ",
        "минио",
        "бакет",
    )
    return any(marker in lowered for marker in markers)


async def _load_storage_runtime_context() -> str:
    """
    Run storage checks from the API host and return text for prompt injection.

    This avoids misleading answers when the worker runtime sandbox cannot reach
    localhost services directly.
    """
    s = _settings().storage
    lines: list[str] = [
        "Storage check source: Kragen API host runtime (not worker sandbox).",
        f"Configured endpoint: {s.endpoint_url}",
        f"Configured bucket: {s.bucket}",
    ]
    session = aioboto3.Session()
    try:
        async with session.client(
            "s3",
            endpoint_url=s.endpoint_url,
            aws_access_key_id=s.access_key,
            aws_secret_access_key=s.secret_key,
            region_name="us-east-1",
            config=_S3_PATH_STYLE_CONFIG,
        ) as client:
            try:
                await asyncio.wait_for(client.head_bucket(Bucket=s.bucket), timeout=6.0)
                lines.append("head_bucket: ok")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"head_bucket: error ({type(exc).__name__}: {exc})")

            try:
                response = await asyncio.wait_for(client.list_buckets(), timeout=6.0)
                names = [b.get("Name") for b in response.get("Buckets", []) if isinstance(b, dict)]
                lines.append(f"list_buckets: ok ({len(names)} bucket(s))")
                if names:
                    lines.append("buckets: " + ", ".join(str(name) for name in names[:20]))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"list_buckets: error ({type(exc).__name__}: {exc})")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"storage_client_init: error ({type(exc).__name__}: {exc})")
    return "\n".join(lines)


async def run_cursor_worker(
    *,
    db: AsyncSession,
    task_id: uuid.UUID,
    session_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    correlation_id: str | None,
) -> None:
    """Run Cursor Agent CLI and stream output to clients."""
    tid = str(task_id)
    db_task: Task | None = None
    try:
        task_result = await db.execute(select(Task).where(Task.id == task_id))
        db_task = task_result.scalar_one()
        db_task.status = "running"
        await db.commit()

        await _phase(tid, "Task accepted — loading history and building prompt for Cursor agent…")

        await write_audit(
            db,
            event_type="task.started",
            payload={"task_id": tid, "session_id": str(session_id)},
            workspace_id=workspace_id,
            actor_user_id=user_id,
            correlation_id=correlation_id,
        )
        await db.commit()

        # Build context
        msg_result = await db.execute(
            select(Message).where(Message.session_id == session_id).order_by(Message.created_at.desc()).limit(20)
        )
        recent = list(reversed(list(msg_result.scalars().all())))
        user_message = ""
        for msg in reversed(recent):
            if msg.role == "user":
                user_message = msg.content
                break
        memory_context = ""
        memory_load_failed = False
        runtime_checks_context = ""
        if _settings().worker.memory_context_enabled:
            top_k = max(1, min(_settings().worker.memory_top_k, 12))
            memory_context, memory_load_failed = await _load_long_term_memory_context(
                db=db,
                session_id=session_id,
                workspace_id=workspace_id,
                query=user_message,
                top_k=top_k,
            )
            if memory_load_failed:
                await _phase(
                    tid,
                    "Long-term memory query failed (see logs). Continuing without injected snippets.",
                )
                await task_stream.push_chunk(
                    tid,
                    "[kragen] Long-term memory query failed; continuing without memory snippets. "
                    "Use Diagnostics -> Check long-term memory (DB) to verify PostgreSQL from the API.\n",
                )
            elif memory_context.strip():
                await _phase(tid, "Long-term memory snippets loaded for this prompt.")
            else:
                await _phase(
                    tid,
                    "No long-term memory snippets matched (empty user text or no rows).",
                )
        else:
            await _phase(tid, "Long-term memory injection is disabled in config.")

        if user_message and _looks_like_storage_check_query(user_message):
            await _phase(tid, "Running storage health checks from API host runtime…")
            runtime_checks_context = await _load_storage_runtime_context()
            await _phase(tid, "Storage runtime checks completed and injected into prompt.")
        public_url = api_public_base_url()
        prompt = _build_prompt(
            session_id=session_id,
            workspace_id=workspace_id,
            api_public_url=public_url,
            context_messages=recent,
            user_message=user_message,
            memory_context=memory_context,
            runtime_checks_context=runtime_checks_context,
            memory_load_failed=memory_load_failed,
        )
        ws_path = _workspace_path(workspace_id)

        # Plugin hooks: skill prompt fragments + MCP servers in .cursor/mcp.json.
        plugin_manager = get_plugin_manager()
        try:
            active_skills = plugin_manager.active_skills(user_message=user_message)
            if active_skills:
                prompt = plugin_manager.compose_prompt(
                    base=prompt, user_message=user_message
                )
                await _phase(
                    tid,
                    f"Injected {len(active_skills)} skill fragment(s): "
                    + ", ".join(s.id for s in active_skills),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin_compose_prompt_failed", error=str(exc))

        try:
            mcp_path = await plugin_manager.materialize_mcp_config(ws_path)
            if mcp_path is not None:
                await _phase(
                    tid,
                    f"Wrote MCP config for plugins: {mcp_path.relative_to(ws_path)}",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin_materialize_mcp_failed", error=str(exc))

        task_env: dict[str, str] | None = None
        if _settings().worker.task_token_enabled and user_id is not None:
            try:
                tkn = task_token.mint_task_token(
                    user_id=user_id, workspace_id=workspace_id, task_id=task_id
                )
                task_env = {
                    "KRAGEN_API_URL": public_url,
                    "KRAGEN_TASK_TOKEN": tkn,
                    "KRAGEN_WORKSPACE_ID": str(workspace_id),
                    "KRAGEN_TASK_WORKSPACE_DIR": str(ws_path),
                }
                await _inject_kragen_files_mcp_env(ws_path, task_env)
            except Exception as exc:  # noqa: BLE001
                logger.warning("task_token_mint_or_mcp_inject_failed", error=str(exc))
                task_env = None

        command = _cursor_command(prompt, ws_path)

        prompt_fp = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        logger.info(
            "cursor_worker_start",
            task_id=tid,
            workspace=str(ws_path),
            prompt_sha256_prefix=prompt_fp,
            command_argv=command[:-1],
        )

        max_attempts = max(1, _settings().worker.retries + 1)
        attempts_used = 0
        final_output = ""
        final_error = ""
        final_exit = -1
        timed_out = False
        run_timeout = max(15, _settings().worker.timeout_seconds)
        await _phase(
            tid,
            f"Spawning Cursor CLI (`{_settings().worker.cursor_cli_path} agent …`), "
            f"timeout {run_timeout}s, up to {max_attempts} attempt(s). "
            "Agent stdout/stderr appear below as they arrive.",
        )
        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            if attempt > 1:
                await task_stream.push_chunk(
                    tid,
                    f"[worker-retry] Starting retry {attempt}/{max_attempts}...\n",
                )
            if max_attempts > 1 or attempt > 1:
                await _phase(tid, f"Attempt {attempt}/{max_attempts}: Cursor agent subprocess running…")
            result = await _run_cursor_attempt(
                command=command,
                task_stream_id=tid,
                timeout_seconds=run_timeout,
                env=task_env,
            )
            final_output = str(result["stdout"])
            final_error = str(result["stderr"])
            final_exit = int(result["exit_code"])
            timed_out = bool(result["timed_out"])

            if final_exit == 0:
                break
            combined = "\n".join(p for p in (final_output, final_error) if p)
            if attempt < max_attempts and (timed_out or _is_transient_error(combined)):
                await task_stream.push_chunk(
                    tid, "[worker-retry] Transient failure detected, retrying...\n"
                )
                continue
            break

        await _phase(tid, "Subprocess finished — assembling reply for Messages…")

        full_output = final_output or final_error
        full_error = final_error
        if not full_output:
            full_output = "[cursor-agent] No output was produced."

        if final_exit != 0:
            if "Authentication required" in full_error or "Authentication required" in full_output:
                auth_hint = (
                    "\n\n[cursor-agent setup] Run `cursor agent login` "
                    "or set CURSOR_API_KEY for the service user."
                )
                full_output += auth_hint
                await task_stream.push_chunk(tid, auth_hint + "\n")
            elif _is_mcp_approval_error(full_output + "\n" + full_error):
                mcp_hint = (
                    "\n\n[mcp-permissions] The request was blocked by MCP approval/policy. "
                    "Open Cursor MCP settings, allow the server/tool, then retry."
                )
                full_output += mcp_hint
                await task_stream.push_chunk(tid, mcp_hint + "\n")
            elif timed_out:
                timeout_hint = (
                    "\n\n[worker-timeout] Request exceeded execution timeout. "
                    "Try a simpler prompt or increase worker.timeout_seconds."
                )
                full_output += timeout_hint
                await task_stream.push_chunk(tid, timeout_hint + "\n")

            if db_task is not None:
                db_task.status = "failed"
                db_task.error = full_error or f"Cursor Agent exited with code {final_exit}"
                db_task.updated_at = datetime.now(UTC)

            assistant = Message(
                session_id=session_id,
                role="assistant",
                content=full_output,
                metadata_={
                    "worker": "cursor-agent",
                    "exit_code": final_exit,
                    "ok": False,
                    "timed_out": timed_out,
                    "attempts": attempts_used,
                },
            )
            db.add(assistant)
            await db.commit()
            return

        assistant = Message(
            session_id=session_id,
            role="assistant",
            content=full_output,
            metadata_={"worker": "cursor-agent", "exit_code": 0, "ok": True, "attempts": attempts_used},
        )
        db.add(assistant)

        if db_task is not None:
            db_task.status = "completed"
            db_task.updated_at = datetime.now(UTC)

        await write_audit(
            db,
            event_type="task.completed",
            payload={"task_id": tid},
            workspace_id=workspace_id,
            actor_user_id=user_id,
            correlation_id=correlation_id,
        )
        await db.commit()
        await _phase(tid, "Done — task completed. Check Messages for the saved assistant reply.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("worker_failed", task_id=tid)
        detail = _format_exception_for_user(exc)
        if isinstance(exc, ProcessLookupError):
            detail = (
                f"{detail} — Usually a harmless race while stopping the Cursor subprocess after a "
                "timeout; retry once. If it persists, check Diagnostics -> Server logs."
            )
        try:
            error_text = (
                f"[worker-error] {detail}\n\n"
                "Tip: open Diagnostics -> Server logs for the full traceback (this API process)."
            )
            await task_stream.push_chunk(tid, error_text + "\n")
            if db_task is not None:
                db_task.status = "failed"
                db_task.error = detail
                db_task.updated_at = datetime.now(UTC)
                await db.commit()
            else:
                failed_task_result = await db.execute(select(Task).where(Task.id == task_id))
                task_orphan = failed_task_result.scalar_one_or_none()
                if task_orphan:
                    task_orphan.status = "failed"
                    task_orphan.error = detail
                    task_orphan.updated_at = datetime.now(UTC)
                    await db.commit()
            assistant = Message(
                session_id=session_id,
                role="assistant",
                content=error_text,
                metadata_={"worker": "cursor-agent", "ok": False, "exception": True},
            )
            db.add(assistant)
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
    finally:
        await task_stream.complete_task(tid)


def _log_scheduled_worker_done(asyncio_task: asyncio.Task[None]) -> None:
    try:
        exc = asyncio_task.exception()
        if exc is not None:
            logger.error("scheduled_worker_failed", exc_info=exc)
    except asyncio.CancelledError:
        pass


async def schedule_task(
    *,
    task_id: uuid.UUID,
    session_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    correlation_id: str | None,
) -> None:
    """Dispatch a Cursor worker job via the configured task backend."""

    job = TaskJob(
        task_id=task_id,
        session_id=session_id,
        workspace_id=workspace_id,
        user_id=user_id,
        correlation_id=correlation_id,
    )
    if _settings().task_queue.backend == "redis":
        await enqueue(job)
        return

    async def _runner() -> None:
        from kragen.db.session import async_session_factory

        async with async_session_factory() as session:
            await run_cursor_worker(
                db=session,
                task_id=task_id,
                session_id=session_id,
                workspace_id=workspace_id,
                user_id=user_id,
                correlation_id=correlation_id,
            )

    t = asyncio.create_task(_runner(), name=f"kragen-cursor-worker-{task_id}")
    t.add_done_callback(_log_scheduled_worker_done)
