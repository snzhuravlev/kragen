# Kragen architecture review

Snapshot of the current state of the codebase, its strengths, and the risks
worth addressing before the project is promoted beyond MVP.

This document complements [ARCHITECTURE.md](ARCHITECTURE.md): `ARCHITECTURE.md`
is the canonical description of what the system is, while this file records the
reviewed assessment, priorities, and open risks.

## Current architecture (as-is)

### Entry points

- `kragen-api` — FastAPI HTTP gateway (`src/kragen/api/main.py`).
- `kragen-telegram-channel` — standalone Telegram adapter process
  (`src/kragen/channels/telegram_adapter.py`).
- `kragen-service` — combined supervisor that runs API and Telegram adapter
  as two child processes (`src/kragen/cli/service_runner.py`).
- `agentctl` — HTTP CLI client to the API.
- Built-in plugin entry point: `kragen-skill-concise`.

### Layered flow

```
Client (Web UI / Telegram / agentctl)
        │
        ▼
FastAPI gateway
  ├── middleware: CorrelationID, CORS
  ├── auth: Bearer=UUID (MVP) / X-Dev-User-ID
  ├── RBAC: admin role via auth.admin_user_ids allow-list
  └── routes: health, workspaces, sessions, messages, tasks, files,
             admin, admin/plugins
        │
        ▼
Orchestrator (services/orchestrator.py)
  ├── prompt builder + long-term memory SQL (same tables as memory-mcp)
  ├── PluginManager: compose_prompt + materialize_mcp_config
  ├── subprocess: cursor agent --print (timeout, retries)
  └── task_stream facade → TaskStreamBackend (in-memory default)
        │
        ▼
PostgreSQL (SQLAlchemy async)  +  S3/MinIO (aioboto3)  +  FS workspaces
```

### Subsystems

- **Plugins** (`src/kragen/plugins/*`):
  - kinds: `skill`, `tool` (MCP), `backend` (FastAPI router), `channel` (descriptor).
  - discovery via `kragen.plugins` entry points; activation gated by
    `plugins.enabled` allow-list in `kragen.yaml`.
  - orchestrator consumes `active_skills` for prompt composition and
    `active_mcp_servers` to materialize `.cursor/mcp.json` per task workspace.
- **Telegram channel**:
  - chat-to-session bindings in `telegram_bindings`.
  - idempotency by `(chat_id, message_id)` in `telegram_processed_messages`.
  - polling and webhook modes, webhook secret token verification.
  - background retention plus a reaper that times out stuck `processing` rows.
  - pseudo-streaming to Telegram by editing a single "Processing…" message
    driven by the task SSE stream.
- **Task stream** (`services/task_stream.py` + `task_stream_backends.py`):
  - public facade (`register_task`, `push_chunk`, `iter_chunks`, ...) delegates
    to a `TaskStreamBackend`.
  - default implementation is in-memory (MVP). Alternative backends (e.g. Redis)
    can be installed via `task_stream.set_backend(...)` without touching callers.
- **Data**:
  - PostgreSQL: users, workspaces, sessions, messages, tasks, artifacts,
    audit, documents/chunks/embeddings, semantic_facts, session_summaries,
    telegram bindings, processed messages.
  - Object storage: binary artifacts via S3 API.
  - Filesystem: `~/.kragen/workspaces/<id>/` (Cursor workspace + `.cursor/mcp.json`).
  - In-memory: SSE buffers per task, structlog ring buffer.

## Strengths

- Clear layering: HTTP routes stay thin, business logic lives in
  `services/orchestrator.py`.
- Unified extension surface (skills/MCP/backend/channel) behind a single
  `PluginManager` plus entry points plus allow-list plus admin API.
- Telegram adapter has real idempotency, webhook secret validation, retention,
  a reaper for stuck `processing` rows, and a streaming preview via
  `editMessageText`.
- **RBAC**: admin endpoints are gated by an explicit `auth.admin_user_ids`
  allow-list; audit and retrieval lists are workspace-scoped and verified via
  ownership or `workspace_members`.
- **Admin YAML endpoint masks secrets**: database URL password, S3 keys, JWT
  secret, Telegram tokens are replaced with `***masked***` before returning
  `kragen.yaml` to operators.
- **Async admin subprocess**: `cursor-auth/*` uses `asyncio.create_subprocess_exec`
  so the CLI call never blocks the event loop.
- **Pluggable task stream**: in-memory backend for MVP, clear seam for Redis.
- Combined service (`kragen-service`) gives operators a single systemd unit.
- Observability primitives in place: structlog, ring buffer, correlation ID,
  audit events.
- Worker-level resilience: timeouts, retries with transient detection,
  classification of MCP approval/auth/timeout errors, human-readable hints.
- Settings fallback chain: env → `telegram_channel.*` in YAML.
- Repository is sanitized: no real secrets, placeholders in `kragen.yaml`,
  `.kragen/` ignored.

## Risks and gaps

### Security / authorization

- **MVP auth**: Bearer token is a raw user UUID; `AUTH_DISABLED` accepts any
  `X-Dev-User-ID`. Production needs JWT/OIDC.
- **Files API**: `upload_file` does not verify that `user_id` is a member of
  the target `workspace_id`; `get_document`/`get_artifact` ignore `user_id`.

### Scalability / multi-process

- **In-memory task stream still default**: the facade is pluggable but no
  distributed backend ships yet — operate on one API process until a Redis
  (or similar) backend is added.
- **`schedule_task`** is `asyncio.create_task` in the same process — no
  queue, no distribution. Production needs a shared task bus (Redis
  Streams, Celery, arq, or similar).
- **Chunk drop on overflow** (`DEFAULT_MAX_QUEUED_CHUNKS=4096`) is silent.
- **Buffer lifecycle**: `iter_chunks` removes the buffer on the first client
  disconnect — a second reader cannot continue to read a still-live task.

### Plugins

- **Backend routes cannot be unmounted at runtime**: disabling a plugin
  drops skills/MCP/channels but keeps the FastAPI router until restart.
  Documented but easy to miss.
- **MCP id collisions** between plugins silently overwrite each other in
  the generated `.cursor/mcp.json`.
- **`manifest.requires` is unused**: no topological sorting or validation on
  `initialize`.
- **`when="manual"` skills** have no attached session-binding API
  (`/sessions/{id}/skills` is still a placeholder idea).
- **No MCP-kind plugins ship by default**: the server-side Cursor worker only
  gets MCP servers if an operator installs a plugin that registers them.
  `configs/mcp/cursor-mcp.example.json` is only IDE-side — this expectation
  gap must be explicit in the docs.

### Telegram channel

- **`Authorization: Bearer {auth_user_id}`** works only while the API accepts
  UUIDs as tokens (MVP auth). The adapter must be updated whenever real JWT
  auth lands.
- **Combined service does not restart children** internally: if either child
  exits, the parent terminates both. Recovery relies on systemd-level
  `Restart=`.

### Configuration / secrets

- `configs/kragen.yaml` and `scripts/systemd/kragen-service.env` ship with
  safe placeholders (`CHANGE_ME`, `TEST_*`) — good for version control but
  production values must come from `/etc/kragen/...` with `0600` permissions.
- Periodic reminder: verify `.env` stays untracked when new secrets are added.

## Prioritized backlog

1. **Files API**: enforce workspace membership on upload and read endpoints.
2. **Distributed task stream backend**: ship a Redis (or similar) implementation
   of `TaskStreamBackend` and wire it via configuration.
3. **Plugin hardening**:
   - validate `requires` and detect cycles at `initialize`
   - detect `MCPServerSpec.id` collisions across plugins
   - document and surface the "backend routers need restart" rule in
     `/admin/plugins` output
4. **Channel abstraction**: extract an internal `ChannelGateway` contract
   that Telegram and future channels implement, instead of leaking HTTP +
   auth shape into each adapter.
5. **Authentication hardening**: add a proper JWT/OIDC path and keep
   `AUTH_DISABLED` purely for development.

## Admin RBAC quick reference

- All admin endpoints listed below require the caller's user id to appear in
  `auth.admin_user_ids`:
  - `/admin/workers`, `/admin/logs`, `/admin/logs/clear`, `/admin/memory/status`
  - `/admin/config/kragen-yaml`, `/admin/config/worker`
  - `/admin/cursor-auth/status`, `/admin/cursor-auth/login`
  - all `/admin/plugins/*`
- Audit and retrieval listings remain open to regular users but require a
  `workspace_id` query parameter and verify the caller owns or is a member
  of that workspace. Admins may omit `workspace_id` to list across workspaces.
- `GET /admin/config/kragen-yaml` returns YAML with sensitive values replaced
  by `***masked***`.

## How this document is maintained

- Revise this file when a significant architectural change lands (new layer,
  new channel, new data store, auth model change).
- Move each completed item from the backlog into the "Strengths" section with
  a short note on how the concern was addressed.
