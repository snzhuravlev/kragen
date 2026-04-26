# Installing Kragen

## Requirements

- **Python** 3.12 or newer
- **PostgreSQL** 16+ with the **pgvector** extension (use a dedicated instance in production; migrations assume a database where `CREATE EXTENSION vector` is allowed)
- **S3-compatible storage** for file uploads (e.g. MinIO or cloud S3)

## Clone and virtual environment

```bash
cd /path/to/kragen
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e ".[dev]"
```

The `kragen` package installs in editable mode; console scripts `kragen-api` and `agentctl` become available.

## Database

1. Create a role and database; enable the `vector` extension in the target database (superuser or equivalent).
2. Set the connection string in `**configs/kragen.yaml**` under `database.url` (`postgresql+asyncpg://...`), or set `**KRAGEN_DATABASE__URL**` in the environment.

## Schema migrations

From the repository root, with the virtual environment active and database URL configured via YAML or env:

```bash
alembic upgrade head
```

Alembic and `memory-mcp` use the synchronous `**psycopg**` driver included in runtime dependencies.

## Seed data (optional)

```bash
python scripts/seed_data.py
```

Creates a development user and workspace with a fixed UUID (see the script).

## Run the API

```bash
uvicorn kragen.api.main:app --reload --host 0.0.0.0 --port 8000
```

or:

```bash
kragen-api
```

Health check: `GET http://localhost:8000/health` → `{"status":"ok"}`.

## Web UI

Open `http://localhost:8000/ui/index.html` (or `/ui/` if static routing serves `index.html`).

- **Query parameters**: `?api=http://127.0.0.1:8000` and `?token=<user-uuid>` override the API base URL and Bearer token (useful for bookmarks).
- **Chat**: pick workspace and session, compose a message, **Send Message**. Messages are shown as Markdown (the page loads **marked** and **DOMPurify** from a CDN for rendering and sanitization).
- **Configuration**: set API URL and token, run **Verify connection** (`GET /health`). The **kragen.yaml** section loads the server file via `**GET /admin/config/kragen-yaml`** (requires the same Bearer token as other authenticated routes). Use **Reload kragen.yaml** after changing the file on disk (the API must be restarted to apply YAML changes to the running process).

Sidebar actions (**Refresh**, new session, Cursor auth) apply to the current API context.

## Enable real Cursor execution

The orchestrator now calls `cursor agent` in headless mode for task execution.

One-time setup for the service user:

```bash
cursor agent login
```

Alternative for non-interactive environments:

- set `CURSOR_API_KEY`

Optional worker command override:

- `KRAGEN_WORKER_COMMAND` (shell-style command prefix; prompt is appended automatically)

## Telegram channel adapter (MVP, long polling)

Kragen now includes a separate process entrypoint:

```bash
kragen-telegram-channel
```

It can be started either:

- as a standalone channel process (`kragen-telegram-channel`), or
- under one combined supervisor process with API (`kragen-service`).

Combined startup:

```bash
kragen-service
```

Systemd template for this mode: `scripts/systemd/kragen-service.service`.
Optional env template: `scripts/systemd/kragen-service.env`.
Setup checklist: `scripts/systemd/README.md`.

`kragen-service` supervises both child processes:

- API (`uvicorn kragen.api.main:app`)
- Telegram adapter (`kragen.channels.telegram_adapter`)

If one child exits or the parent receives `SIGTERM`/`SIGINT`, both children are
stopped together.

Required environment variables:

- `KRAGEN_TELEGRAM_BOT_TOKEN` — Telegram bot token from BotFather.
- `KRAGEN_TELEGRAM_API_BASE_URL` — Kragen API base URL (default `http://127.0.0.1:8000`).
- `KRAGEN_TELEGRAM_AUTH_USER_ID` — UUID sent as Bearer token to Kragen API.
- `KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID` — workspace UUID used for new chat bindings.

Optional tuning:

- `KRAGEN_TELEGRAM_POLL_TIMEOUT_SECONDS` (default `20`)
- `KRAGEN_TELEGRAM_LOOP_DELAY_SECONDS` (default `0.4`)
- `KRAGEN_TELEGRAM_TASK_POLL_INTERVAL_SECONDS` (default `1.0`)
- `KRAGEN_TELEGRAM_TASK_WAIT_TIMEOUT_SECONDS` (default `300`)
- `KRAGEN_TELEGRAM_DEDUP_RETENTION_HOURS` (default `168`) — keep processed
message ids for this amount of time.
- `KRAGEN_TELEGRAM_DEDUP_CLEANUP_INTERVAL_SECONDS` (default `3600`) —
background cleanup interval for old dedup rows.
- `KRAGEN_TELEGRAM_DEDUP_PROCESSING_TIMEOUT_MINUTES` (default `30`) — stuck
`processing` rows older than this threshold are marked as `failed` so the same
`(chat_id, message_id)` can be retried.

Mode selection:

- `KRAGEN_TELEGRAM_MODE=polling` (default) — uses `getUpdates` long polling.
- `KRAGEN_TELEGRAM_MODE=webhook` — runs a local FastAPI webhook receiver and
registers webhook URL via Telegram API.

Webhook mode variables:

- `KRAGEN_TELEGRAM_WEBHOOK_PUBLIC_URL` (required for webhook mode), e.g.
`https://bot.example.com`
- `KRAGEN_TELEGRAM_WEBHOOK_PATH` (default `/telegram/webhook`)
- `KRAGEN_TELEGRAM_WEBHOOK_HOST` (default `0.0.0.0`)
- `KRAGEN_TELEGRAM_WEBHOOK_PORT` (default `8081`)
- `KRAGEN_TELEGRAM_WEBHOOK_SECRET_TOKEN` (optional but recommended) — verified
against `X-Telegram-Bot-Api-Secret-Token` header and passed to Telegram
`setWebhook` as `secret_token`.

Webhook probe endpoints (served by `kragen-telegram-channel` in webhook mode):

- `GET /health` — static process health (`status=ok`).
- `GET /ready` — readiness check; returns `503` when background webhook worker
is not running.

First-message behavior:

- `/start` creates (or reuses) a Telegram binding and replies with usage hints.
- `/new` starts a fresh Kragen session bound to the same Telegram chat.
- Any other text is posted into the bound Kragen session and the assistant reply
is returned to Telegram.
- While the task is running, adapter streams task output from
`GET /tasks/{id}/stream` and updates the same Telegram message via
`editMessageText` (best effort), then replaces it with the final assistant
message.
- Adapter tracks processed messages in `telegram_processed_messages` and enforces
idempotency by unique `(chat_id, message_id)`.

## `agentctl` CLI

Set environment variables (see `src/kragen/cli/agentctl.py`), at minimum:

- `KRAGEN_TOKEN` — user UUID (Bearer)
- `KRAGEN_WORKSPACE_ID`, `KRAGEN_SESSION_ID` — for `agentctl ask`

API base URL: `KRAGEN_API_URL` (default `http://127.0.0.1:8000`).

## Configuration

Primary file: `**configs/kragen.yaml**`. Overrides: variables with prefix `**KRAGEN_**` and nested keys via `**__**`, e.g. `KRAGEN_DATABASE__URL`, `KRAGEN_AUTH__DISABLED`. Optional `**.env**` in the project root.

Recommended layout:

- keep placeholders/defaults in `configs/kragen.yaml`,
- keep real secrets in system environment (for example `/etc/kragen/kragen-service.env`),
- keep `.env` for local developer convenience only.

Alternate YAML path: `**KRAGEN_CONFIG_FILE=/absolute/path/kragen.yaml**`. The Web UI’s YAML viewer resolves the same path as the running API process.

### Telegram adapter test section in YAML

`configs/kragen.yaml` now includes a dedicated `telegram_channel:` section with
**test values**. It is meant as an operator template, not as a production
secret store.

```yaml
telegram_channel:
  bot_token: ""
  api_base_url: "http://127.0.0.1:8000"
  auth_user_id: "00000000-0000-0000-0000-000000000001"
  default_workspace_id: "00000000-0000-0000-0000-000000000001"
  mode: "polling"  # polling | webhook
  # ... see full block in configs/kragen.yaml
```

Adapter runtime reads `KRAGEN_TELEGRAM_*` environment variables first. When a
variable is missing, it falls back to `telegram_channel.*` from `kragen.yaml`.

For operation and incident runbooks (Telegram 409/403, MinIO credential issues,
systemd recovery), see `docs/OPERATIONS.md`.

### Cursor long-term memory (memory-mcp)

`mcp_servers/memory_mcp/main.py` now persists and retrieves memory from PostgreSQL.

Use `configs/mcp/cursor-mcp.example.json` and set:

- `MEMORY_MCP_DATABASE_URL` (or `KRAGEN_DATABASE__URL`)
- `MEMORY_MCP_WORKSPACE_ID` for default workspace-scoped retrieval

## Verify installation

```bash
pytest -q
```

At least one smoke test should cover `/health`.