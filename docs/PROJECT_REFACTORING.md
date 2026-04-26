# Project structure, extensions, and refactoring backlog

This document is a **snapshot-oriented** guide: repository layout, how extensibility
works, every registered plugin, and **what should still be fixed or evolved**.
It complements [ARCHITECTURE.md](ARCHITECTURE.md) (what the system is) and
[PLUGINS.md](PLUGINS.md) (plugin author guide). [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md)
contains an earlier review; some items there are **stale** — this file is
refreshed against the current tree.

---

## 1. Repository structure (code)

| Area | Path | Role |
| ---- | ---- | ---- |
| HTTP API | `src/kragen/api/` | FastAPI app, `deps`, route modules (health, workspaces, sessions, messages, tasks, files, admin, plugins). |
| CLI | `src/kragen/cli/` | `agentctl`, `service_runner` (supervisor), `web_server_ctl`. |
| Channels | `src/kragen/channels/` | Telegram adapter (`telegram_adapter.py`), API client (`telegram_api.py`), settings, helpers. |
| Plugins core | `src/kragen/plugins/` | `base`, `context`, `loader`, `manager`, `errors`, `builtin/`. |
| Services | `src/kragen/services/` | Orchestrator, task stream + backends, **task queue** (Redis/inline), file storage, audit, Telegram bindings, task reaper. |
| Data | `src/kragen/db/`, `src/kragen/models/` | Async SQLAlchemy, ORM models (core, memory, retrieval, storage). |
| Storage I/O | `src/kragen/storage/` | S3-compatible object store. |
| Config | `src/kragen/config.py` | Pydantic settings (YAML, `.env`, `KRAGEN_*`, production validator). |
| Worker process | `src/kragen/worker.py` | Dequeues `TaskJob` from Redis when `task_queue.backend=redis`, runs cursor worker. |
| Migrations | `alembic/` | Schema evolution. |
| Tests | `tests/` | Pytest coverage for API, services, channels, plugins. |
| Deployment | `scripts/systemd/` | Unit/env templates for API-only or combined `kragen-service`. |
| Config template | `configs/kragen.yaml` | Non-secret defaults and placeholders. |

**Static UI** (if present) is served from the API static mount (see `api/main.py`).

---

## 2. Runtime entry points (console scripts)

Defined in `pyproject.toml` under `[project.scripts]`:

| Script | Target | Purpose |
| ------ | ------ | ------- |
| `kragen-api` | `kragen.api.main:run` | Uvicorn: main HTTP gateway. |
| `kragen-worker` | `kragen.worker:main` | Optional separate process: Redis-backed task consumer for `cursor` worker runs. |
| `kragen-telegram-channel` | `kragen.channels.telegram_adapter:main` | Telegram long-polling or small webhook app (depending on config). |
| `kragen-service` | `kragen.cli.service_runner:main` | Supervisor: API + Telegram as child processes. |
| `agentctl` | `kragen.cli.agentctl:main` | CLI client against the API. |

**Plugin discovery** is not a separate script: plugins load in-process when
`kragen-api` (or any process that calls `create_app()`) starts.

---

## 3. Extension model: plugins

### 3.1 Discovery and activation

- **Discovery:** Python [entry points](https://packaging.python.org/en/latest/specifications/entry-points/)
  in group `kragen.plugins` (see `src/kragen/plugins/loader.py`).
- **Activation:** Allow-list in `configs/kragen.yaml` under `plugins.enabled`
  (each entry: `id` + optional `config`).
- **Autoload flag:** `plugins.autoload_entry_points` (scan entry points at startup).
- **Runtime API:** `GET/POST` under `/admin/plugins/*` (admin RBAC) for
  list/status/enable/disable — **disabling a `backend` plugin does not unmount
  its router until process restart** (see §5).

### 3.2 Plugin kinds (`PluginKind`)

From `src/kragen/plugins/base.py`:

| Kind | Effect |
| ---- | ------ |
| `skill` | Prompt fragments (composed by `PluginManager` / orchestrator). |
| `tool` | MCP server specs materialized into per-workspace `.cursor/mcp.json`. |
| `backend` | `APIRouter` mounted on the main FastAPI app. |
| `channel` | Advisory descriptor (actual channel is an external process). |
| `composite` | Combination of the above. |

### 3.3 Registered plugins in *this* repository

Only one plugin is registered in-tree via `pyproject.toml`:

| Entry point name | Module | Id | Kind |
| ---------------- | ------ | -- | ---- |
| `kragen-skill-concise` | `kragen.plugins.builtin.concise_skill:plugin` | `kragen-skill-concise` | `skill` |

Third-party packages add their own `[project.entry-points."kragen.plugins"]`
rows; they are not listed here.

### 3.4 “Extensions” that are *not* entry-point plugins

These extend behavior but are **core code paths**, not `kragen.plugins` packages:

- **Telegram channel** — `channels/telegram_adapter.py` (uses public API as a client).
- **Task execution** — `services/orchestrator.py` (Cursor CLI subprocess, prompts, memory).
- **Task stream** — pluggable: `InMemoryTaskStreamBackend` or `RedisTaskStreamBackend` (`services/task_stream_backends.py`), selected via `task_stream.backend` in settings.
- **Task queue dispatch** — `services/task_queue.py`: `inline` (asyncio in API process) or `redis` (API enqueues, `kragen-worker` dequeues).
- **Object / file storage** — S3 API + logical tree in DB (`services/file_storage.py`, `storage/object_store.py`).

---

## 4. Data and cross-cutting concerns

- **PostgreSQL:** Users, workspaces, sessions, messages, tasks, storage entries,
  documents, vectors, audit, Telegram bindings, dedup table, etc.
- **S3-compatible storage:** Binary payloads referenced by `storage_entries` / artifacts.
- **Workspace filesystem:** Under `~/.kragen/workspaces/<uuid>/` for Cursor
  workspaces and generated MCP config.
- **Auth (MVP):** Bearer as user UUID, optional dev header; **production**
  validator in `KragenSettings.validate_production_profile` (e.g. `auth.disabled`,
  `raw_uuid_bearer`, `api.host` not `0.0.0.0`, JWT secret).
- **Admin RBAC:** `auth.admin_user_ids` for `/admin/*` and `/admin/plugins/*`.

---

## 5. What should be fixed or improved (refactoring backlog)

Prioritized by impact. Several items in `ARCHITECTURE_REVIEW.md` are **out of date**;
this section supersedes them.

### 5.1 Plugin subsystem hardening

- **`manifest.requires`:** Not enforced / no cycle detection at `PluginManager.initialize()`.
- **MCP `id` collisions:** Multiple plugins can register the same `MCPServerSpec.id`;
  last writer wins in generated `mcp.json` — add detection and fail-fast or
  namespacing rules.
- **Backend routers:** Disabling a plugin after boot does not remove its routes;
  surface this in `/admin/plugins` responses and operator docs.
- **Manual skills:** `when="manual"` has no first-class “bind to session” API yet
  (documented in `PLUGINS.md` as future work).
- **Trust / sandboxing:** None — same posture as “install trusted extensions only”.

### 5.2 Authentication and channel integration

- **MVP token model:** Raw UUID bearer is convenient for development; **production**
  should use real JWT/OIDC; `validate_production_profile` already blocks some
  unsafe combinations when `app.environment=prod`.
- **Telegram ↔ API:** Adapter sends `Authorization: Bearer …` aligned with the
  API user; when JWT lands, the adapter and `telegram_channel.api_bearer_token`
  (or token refresh) must follow the new scheme.

### 5.3 Telegram and process architecture

- **Monolithic adapter file:** `telegram_adapter.py` is large — split into
  submodules (webhook app, polling loop, message pipeline, streaming updates)
  for testability and reviewability.
- **Channel abstraction:** Introduce an internal `ChannelGateway`-style
  contract so future channels (Slack, webhooks) do not each re-implement HTTP
  client + auth details.
- **Supervisor behavior:** `kragen-service` exits when a child dies; recovery
  is expected from systemd `Restart=` — document operational expectations.

### 5.4 Task streaming and scale-out

- **Defaults:** In-memory task stream and inline queue remain valid for
  single-node dev; **Redis** backends exist for both stream and queue — ops
  must set URLs and run `kragen-worker` when using Redis queue.
- **In-memory stream semantics:** Chunk overflow drops old chunks; first
  disconnect can dispose the buffer — document limits for production SSE
  consumers.
- **`RedisTaskStreamBackend.is_complete`:** Local flag may not reflect remote
  workers in all cases — verify consumers rely on stream end markers, not
  only `is_complete()`.

### 5.5 Files and metadata APIs

- **Workspace checks:** Storage routes and `get_document` / `get_artifact` use
  `ensure_workspace_access` — the **older** “files API has no membership check”
  finding is **addressed** in the current `files.py`. Keep new routes consistent
  with the same pattern.

### 5.6 Documentation hygiene

- **Sync `ARCHITECTURE_REVIEW.md`:** Update strengths/risks/backlog to match
  Redis task stream, Redis task queue, files RBAC, and `validate_production_profile`.
- **Deprecate duplicate drift:** Point readers from the review file to this
  document for structure + extension inventory, or merge content in a later edit.

---

## 6. How to maintain this document

Update this file when:

- New `[project.entry-points."kragen.plugins"]` or route modules are added.
- A major subsystem is split, renamed, or a new process (e.g. extra worker type)
  is introduced.
- A backlog item is completed — move it to a short “Recently addressed”
  subsection or to `ARCHITECTURE.md` as stable architecture fact.

Last reviewed: project tree and `pyproject.toml` as of the document creation date
in the repository.
