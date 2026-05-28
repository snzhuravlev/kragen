# Project structure, extensions, and refactoring backlog

Snapshot guide: repository layout, extensibility, built-in plugins, recently
completed work, and remaining backlog. See also [ARCHITECTURE.md](ARCHITECTURE.md),
[ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md), and [PLUGINS.md](PLUGINS.md).

---

## 1. Repository structure (code)

| Area | Path | Role |
| ---- | ---- | ---- |
| HTTP API | `src/kragen/api/` | FastAPI app, `deps`, route modules. |
| CLI | `src/kragen/cli/` | `agentctl`, `service_runner`, `web_server_ctl`. |
| Channels | `src/kragen/channels/` | `base.py` (`ChannelGateway`), `telegram/` package, legacy re-exports. |
| Plugins | `src/kragen/plugins/` | Entry-point discovery, `PluginManager`, `builtin/`. |
| Services | `src/kragen/services/` | Orchestrator, task stream/queue, file storage, audit, bindings. |
| Worker | `src/kragen/worker.py` | Redis task consumer (`bootstrap_plugins` + `run_worker_process`). |
| Data | `src/kragen/db/`, `models/`, `alembic/` | Async SQLAlchemy, ORM, migrations (001–004). |
| Config | `src/kragen/config.py`, `configs/kragen.yaml` | Settings + production validator. |

---

## 2. Runtime entry points

| Script | Target | Role |
| ------ | ------ | ---- |
| `kragen-api` | `kragen.api.main:run` | HTTP gateway, plugin init, inline or Redis enqueue. |
| `kragen-worker` | `kragen.worker:main` | Redis consumer; **must** run when `task_queue.backend=redis`. |
| `kragen-telegram-channel` | `kragen.channels.telegram_adapter:main` | Telegram polling/webhook (re-exports `telegram` package). |
| `kragen-service` | `kragen.cli.service_runner:main` | Supervisor: API + Telegram children. |
| `agentctl` | `kragen.cli.agentctl:main` | HTTP CLI client. |

### Scale-out checklist (Redis)

When running more than one API process or separating workers:

1. `task_queue.backend: redis` and run **`kragen-worker`** (calls `bootstrap_plugins()`).
2. `task_stream.backend: redis` so SSE in the API sees chunks from the worker.
3. Same Redis URL (or coordinated DB/prefix/key settings) for both backends.

---

## 3. Built-in plugins (`pyproject.toml`)

| Entry point | Id | Kind |
| ----------- | -- | ---- |
| `kragen-skill-concise` | `kragen-skill-concise` | skill |
| `kragen-skill-kragen-storage` | `kragen-skill-kragen-storage` | skill |
| `kragen-mcp-kragen-files` | `kragen-mcp-kragen-files` | tool |
| `kragen-mcp-scripts` | `kragen-mcp-scripts` | tool |
| `kragen-mcp-os` | `kragen-mcp-os` | tool |
| `kragen-mcp-web-search` | `kragen-mcp-web-search` | tool |

All are allow-listed in `configs/kragen.yaml` under `plugins.enabled`.

---

## 4. Telegram channel layout

```
src/kragen/channels/
  base.py                 # ChannelGateway protocol + KragenApiGateway
  telegram/
    adapter.py            # Update handlers, commands, documents
    polling.py            # Long-polling loop
    webhook.py            # Webhook FastAPI app + secret validation
    dedup.py              # Retention / stuck-processing reaper
    gateway.py              # (via base.KragenApiGateway)
    settings.py, api_client.py, utils.py
    __init__.py             # main()
  telegram_adapter.py       # Backward-compatible re-exports
```

Production Telegram auth: set `telegram_channel.api_bearer_token` (or
`KRAGEN_TELEGRAM_API_BEARER_TOKEN`) to a **JWT** for the service user; dev may
use raw UUID bearer when `auth.raw_uuid_bearer_enabled=true`.

---

## 5. Recently addressed (architecture backlog)

- **Session RBAC:** `POST /sessions` checks `ensure_workspace_access`.
- **Task RBAC:** Orphan tasks (missing session) return 404.
- **Production validator:** Rejects empty `file_import.allowed_host_suffixes`,
  CORS `*`, and webhook mode without `webhook_secret_token`.
- **Worker plugins:** `kragen-worker` calls `bootstrap_plugins()` before dequeuing.
- **Plugin `requires`:** Topological setup order + cycle detection; cyclic plugins disabled.
- **Admin plugins API:** `runtime_notes`, `router_mounted`, `requires_restart` fields.
- **MCP/skill id collisions:** Fail-fast at plugin setup (not silent overwrite).
- **Telegram refactor:** Package split + `ChannelGateway` / `KragenApiGateway`.
- **Files API:** Workspace membership on storage and document/artifact routes.

---

## 6. Remaining backlog

| Priority | Item |
| -------- | ---- |
| P1 | **`when="manual"` skills** — session binding API (`/sessions/{id}/skills`). |
| P1 | **Backend hot-unmount** — still requires API restart; only documented via admin fields. |
| P2 | **Task queue reliability** — no ack/retry/dead-letter for Redis jobs. |
| P2 | **In-memory stream limits** — chunk drop on overflow; single-reader buffer lifecycle. |
| P2 | **`/admin/audit` URL semantics** — member-scoped but under `/admin` prefix. |
| P2 | **OpenClaw channel** — disabled by default; no production adapter in-tree. |

---

## 7. Maintenance

Update this file when entry points, plugins, or process topology change. Move
completed items from §6 into §5 with a one-line note.
