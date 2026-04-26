# Kragen architecture

## Purpose

Kragen is a **backend platform**: HTTP request handling, sessions and tasks, PostgreSQL persistence, artifact uploads to object storage, and audit logging. **Task execution** is handled by an orchestrator that runs the **Cursor Agent** CLI (`cursor agent`) in a subprocess, streams output over SSE, and persists assistant messages. Long-term memory can be **injected into the worker prompt** from PostgreSQL (same tables as `memory-mcp`); MCP tools are used from the Cursor side when configured.

## Logical layers

```
Clients (Web UI, agentctl, external integrations)
        │
        ▼
┌───────────────────┐
│  FastAPI (gateway) │  routes, CORS, correlation id, DB dependencies
└─────────┬─────────┘
          │
          ├── Session / Messages / Tasks API
          ├── Files → logical file tree + object storage + optional documents
          │
          ▼
┌───────────────────┐
│    Orchestrator    │  enqueue task, Cursor worker (`orchestrator.py`)
│  services/         │
└─────────┬─────────┘
          │
          ├── task_stream (in-process SSE buffer)
          ├── audit_service
          └── SQLAlchemy → PostgreSQL
```

## Main code components


| Component     | Path                                          | Role                                            |
| ------------- | --------------------------------------------- | ----------------------------------------------- |
| HTTP entry    | `src/kragen/api/main.py`                      | FastAPI app, static `/ui`                       |
| Routes        | `src/kragen/api/routes/`                      | health, sessions, messages, tasks, files, admin (incl. Cursor auth, `kragen.yaml` read) |
| Configuration | `src/kragen/config.py`, `configs/kragen.yaml` | Pydantic settings, YAML + env                   |
| Database      | `src/kragen/db/session.py`, `alembic/`        | async SQLAlchemy, migrations                    |
| Models        | `src/kragen/models/`                          | ORM entities                                    |
| Orchestration | `src/kragen/services/orchestrator.py`         | Cursor Agent subprocess, SSE stream, optional memory context |
| File storage  | `src/kragen/services/file_storage.py`, `src/kragen/storage/object_store.py` | Logical file tree + S3 API (aioboto3). See [docs/STORAGE.md](STORAGE.md). |
| CLI           | `src/kragen/cli/agentctl.py`                  | HTTP client to the API                          |


## Web UI

Static assets are mounted at **`/ui`** from `web/` (`src/kragen/api/main.py`). The UI talks to the REST API and optional admin routes (Bearer token in the MVP). It does not embed server secrets; **`GET /admin/config/kragen-yaml`** returns the YAML file contents from the API host for operators who can authenticate.

## MCP

The `mcp_servers/` directory holds separate stdio processes (memory, workspace, db, secrets). IDE wiring is described in `configs/mcp/cursor-mcp.example.json`. The API runtime does not start MCP servers; the client (Cursor) or another process does.

`memory-mcp` is wired to PostgreSQL and provides long-term memory tools:

- `search_memory`
- `get_document`
- `get_chunk`
- `get_related_context`
- `save_session_summary`
- `upsert_semantic_fact`
- `pin_memory`
- `forget_memory`

## Data

- **PostgreSQL**: users, workspaces, sessions, messages, tasks, artifacts, audit, logical file entries (`storage_entries`), documents/chunks/embeddings (schema from Alembic).
- **Object storage**: uploaded blobs; logical file tree metadata and `content_hash` live in PostgreSQL.

## Current MVP limitations

- **Single API process**: task SSE is in-memory; multiple workers need a shared bus (e.g. Redis Streams).
- **Horizontal scaling** of the API requires a shared task/stream backend; the current design assumes one process.
- **OpenClaw** integration is disabled by default (`channels.openclaw_enabled`); channel type `openclaw` is rejected by the API when disabled.

## Admin API (selected)

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/admin/cursor-auth/status` | Cursor CLI auth status (JSON from `cursor agent status`) |
| `POST` | `/admin/cursor-auth/login` | Start headless login; returns browser URL when possible |
| `GET` | `/admin/config/kragen-yaml` | Raw `kragen.yaml` from disk (same path as settings); **sensitive** |

All `/admin/*` routes use the same authentication dependency as other protected routes (see `src/kragen/api/deps.py`).

## Extensibility

Kragen has a first-class **plugin subsystem** (`src/kragen/plugins/`) that lets
third-party packages register:

- **Skills** — prompt fragments injected by the orchestrator into the Cursor worker.
- **MCP tools** — MCP servers serialized into a per-task `.cursor/mcp.json`, making them available to the server-side Cursor agent (not only to the IDE).
- **Backend routers** — FastAPI sub-routers mounted under `/plugins/<id>/...`.
- **Channels** — descriptors of external channel processes (advisory only).

Discovery is via the `kragen.plugins` Python entry-point group; activation is
allow-listed in `configs/kragen.yaml` under the `plugins:` section; lifecycle
is administered through `/admin/plugins/*`. See [docs/PLUGINS.md](PLUGINS.md)
for the contract and examples.

Other extension seams kept intentionally simple:

- New channels (e.g. Telegram) as separate processes normalizing requests into the same HTTP endpoints or an internal service layer.
- Observability (metrics, traces, centralized logs) can be added outside the app (reverse proxy, collectors) without mandatory in-repo dependencies.

