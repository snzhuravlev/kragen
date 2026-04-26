# Deploying Kragen

This guide describes recommended deployment patterns for Kragen API, Telegram
adapter, and object storage integration. For day-2 troubleshooting runbooks,
see `docs/OPERATIONS.md`.

## Configuration

1. **Primary file**: `configs/kragen.yaml` — keep non-secret defaults and placeholders only.
2. **Environment variables**: prefix `KRAGEN_`, nesting via `__`, for example:
   - `KRAGEN_DATABASE__URL`
   - `KRAGEN_STORAGE__ENDPOINT_URL`, `KRAGEN_STORAGE__ACCESS_KEY`, `KRAGEN_STORAGE__SECRET_KEY`, `KRAGEN_STORAGE__BUCKET`
   - `KRAGEN_AUTH__DISABLED`, `KRAGEN_AUTH__DEV_USER_ID` (debug only)
3. **`.env`** in the process working directory (optional) — same keys as environment variables.
4. **YAML path override**: `KRAGEN_CONFIG_FILE=/absolute/path/kragen.yaml` when needed.

Precedence on conflicting keys (highest wins): process arguments → environment variables → `.env` → YAML.

### Recommended secret layout

Use a strict separation:

- `configs/kragen.yaml`: safe defaults, no real secrets.
- `/etc/kragen/kragen-service.env`: real runtime secrets for systemd.
- repository `.env`: local development convenience only.

If both `.env` and systemd env define the same key, systemd process environment
wins.

## Migrations before first run

On every new environment:

```bash
alembic upgrade head
```

Ensure the database URL is available to the process (YAML or `KRAGEN_DATABASE__URL`).

## Runtime profiles

### Local workstation + systemd

This profile is optimized for one host that runs:

- PostgreSQL
- MinIO
- Kragen API + Telegram adapter via one unit (`kragen-service`)

Required files:

- `/etc/systemd/system/kragen-service.service`
- `/etc/kragen/kragen-service.env`

Example `/etc/kragen/kragen-service.env` (replace placeholders):

```bash
KRAGEN_AUTH__DISABLED=false
KRAGEN_DATABASE__URL=postgresql+asyncpg://kragen:CHANGE_ME@127.0.0.1:5432/kragen
KRAGEN_STORAGE__ENDPOINT_URL=http://127.0.0.1:9000
KRAGEN_STORAGE__ACCESS_KEY=CHANGE_ME
KRAGEN_STORAGE__SECRET_KEY=CHANGE_ME
KRAGEN_STORAGE__BUCKET=kragen
KRAGEN_TELEGRAM_BOT_TOKEN=<telegram-bot-token>
KRAGEN_TELEGRAM_API_BASE_URL=http://127.0.0.1:8000
KRAGEN_TELEGRAM_AUTH_USER_ID=00000000-0000-0000-0000-000000001111
KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID=00000000-0000-0000-0000-000000001111
KRAGEN_TELEGRAM_MODE=polling
```

Operational requirement: do not run extra manual adapter/API processes when
systemd unit is active.

## Application process

Run behind a **reverse proxy** (nginx, Caddy, Traefik) with TLS and request body limits.

Example:

```bash
uvicorn kragen.api.main:app --host 0.0.0.0 --port 8000 --workers 1
```

For the current MVP, **one** worker is recommended because task SSE is in-memory; horizontal scaling requires shared storage for task streams.

`app` / `api` keys in YAML configure host and port for the built-in `kragen-api` runner (`kragen.api.main:run`).

## Cursor worker, egress, and file import

- Set **`api.public_base_url`** in `configs/kragen.yaml` to the public origin clients and workers should use (for example `https://api.example.com`). The orchestrator and injected tooling default **`KRAGEN_API_URL`** to this value. If unset, the default is `http://127.0.0.1:{api.port}`.
- **Outbound HTTPS** from the **API** process is required for **`POST /files/import`** (server-side download of remote URLs). If you do not use import, you may block egress; otherwise open egress to the hosts you need or restrict with **`file_import.allowed_host_suffixes`**.
- The **Cursor Agent** CLI (`cursor agent`) needs a valid Cursor session: run **`cursor agent login`** for the service user or set **`CURSOR_API_KEY`**. The API logs and task stream add a hint when the worker prints `Authentication required`.
- **MCP tool approval** can block the **`kragen-files`** server: enable the **`kragen-mcp-kragen-files`** plugin in `plugins.enabled` and allow that MCP server in Cursor if prompted.
- For **`POST /files/import`**, set **`file_import.allowed_host_suffixes`** in production to trusted host suffixes (for example `postgresql.org`). An empty list allows any host and is only suitable for development.
- **Task-scoped tokens** (`worker.task_token_enabled`): the worker mints a short JWT for the task user, passes **`KRAGEN_TASK_TOKEN`**, **`KRAGEN_API_URL`**, and **`KRAGEN_WORKSPACE_ID`** into the subprocess and into the `kragen-files` MCP `env` so tools can call the import API without manual secrets.

## Secrets and security

- In production, **`auth.disabled`** must be `false`; use real authentication (JWT/OIDC) instead of dev shortcuts.
- Store S3 keys and DB passwords in a secret manager or environment variables, not in Git.
- Restrict network access to PostgreSQL and the object storage endpoint.
- **`GET /admin/config/kragen-yaml`** returns the full contents of the resolved YAML file (often including DB URLs and keys). Treat `/admin/*` as **privileged**: restrict by network policy, reverse-proxy auth, or VPN; do not expose admin routes to the public Internet without additional controls.
- Set **`KRAGEN_CONFIG_FILE`** to an absolute path when the service user’s working directory is not the repository root, so configuration and the admin YAML viewer stay consistent.

## Object storage

Ensure the bucket from `storage.bucket` exists or that the process may create it (see `ensure_bucket_exists` in code).

### MinIO compatibility notes

Kragen storage client is configured for S3 path-style requests, which improves
compatibility with local MinIO deployments.

When startup logs show `InvalidAccessKeyId`:

1. verify MinIO credentials source (for example `/etc/default/minio`),
2. verify Kragen storage env keys match exactly,
3. restart `kragen-service`,
4. re-check logs.

## systemd (example)

```ini
[Unit]
Description=Kragen API
After=network.target

[Service]
User=kragen
WorkingDirectory=/opt/kragen
Environment=KRAGEN_CONFIG_FILE=/etc/kragen/kragen.yaml
ExecStart=/opt/kragen/.venv/bin/uvicorn kragen.api.main:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Adjust paths to your venv and config.

## systemd for Telegram channel

Kragen Telegram adapter runs as a **separate process** from API:

- `kragen-telegram-channel` in polling mode, or
- `kragen-telegram-channel` in webhook mode (with its own HTTP listener).

### Polling mode unit example

```ini
[Unit]
Description=Kragen Telegram Channel (polling)
After=network.target

[Service]
User=kragen
WorkingDirectory=/opt/kragen
Environment=KRAGEN_TELEGRAM_MODE=polling
Environment=KRAGEN_TELEGRAM_BOT_TOKEN=<telegram-bot-token>
Environment=KRAGEN_TELEGRAM_API_BASE_URL=http://127.0.0.1:8000
Environment=KRAGEN_TELEGRAM_AUTH_USER_ID=00000000-0000-0000-0000-000000000001
Environment=KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID=00000000-0000-0000-0000-000000000001
Environment=KRAGEN_TELEGRAM_DEDUP_RETENTION_HOURS=168
Environment=KRAGEN_TELEGRAM_DEDUP_CLEANUP_INTERVAL_SECONDS=3600
ExecStart=/opt/kragen/.venv/bin/kragen-telegram-channel
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### Webhook mode unit example

```ini
[Unit]
Description=Kragen Telegram Channel (webhook)
After=network.target

[Service]
User=kragen
WorkingDirectory=/opt/kragen
Environment=KRAGEN_TELEGRAM_MODE=webhook
Environment=KRAGEN_TELEGRAM_BOT_TOKEN=<telegram-bot-token>
Environment=KRAGEN_TELEGRAM_API_BASE_URL=http://127.0.0.1:8000
Environment=KRAGEN_TELEGRAM_AUTH_USER_ID=00000000-0000-0000-0000-000000000001
Environment=KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID=00000000-0000-0000-0000-000000000001
Environment=KRAGEN_TELEGRAM_WEBHOOK_PUBLIC_URL=https://bot.example.com
Environment=KRAGEN_TELEGRAM_WEBHOOK_PATH=/telegram/webhook
Environment=KRAGEN_TELEGRAM_WEBHOOK_HOST=127.0.0.1
Environment=KRAGEN_TELEGRAM_WEBHOOK_PORT=8081
Environment=KRAGEN_TELEGRAM_WEBHOOK_SECRET_TOKEN=TEST_WEBHOOK_SECRET_REPLACE_ME
ExecStart=/opt/kragen/.venv/bin/kragen-telegram-channel
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### Environment file pattern

Instead of many `Environment=` lines, use an env file:

```ini
[Service]
EnvironmentFile=/etc/kragen/telegram-channel.env
ExecStart=/opt/kragen/.venv/bin/kragen-telegram-channel
```

Then place the same `KRAGEN_TELEGRAM_*` keys in `/etc/kragen/telegram-channel.env`.

### Health checks in webhook mode

Webhook mode exposes probe endpoints on `KRAGEN_TELEGRAM_WEBHOOK_HOST:PORT`:

- `GET /health` — process alive
- `GET /ready` — background worker ready (`503` otherwise)

## One service for API + Telegram

If you want a single systemd unit to start both API and Telegram adapter, use
`kragen-service`:

```ini
[Unit]
Description=Kragen Combined Service (API + Telegram)
After=network.target

[Service]
User=kragen
WorkingDirectory=/opt/kragen
Environment=KRAGEN_CONFIG_FILE=/etc/kragen/kragen.yaml
# Optional overrides (otherwise values from kragen.yaml telegram_channel.* are used):
# Environment=KRAGEN_TELEGRAM_MODE=webhook
# Environment=KRAGEN_TELEGRAM_WEBHOOK_PUBLIC_URL=https://bot.example.com
ExecStart=/opt/kragen/.venv/bin/kragen-service
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Behavior:

- starts API and Telegram child processes under one parent;
- if either child crashes, the parent stops the other child and exits non-zero;
- on service stop/restart, both children are terminated together.

### Important runtime guardrails

- Keep `KRAGEN_AUTH__DISABLED=false` in service runtime for Telegram Bearer auth.
- Ensure `KRAGEN_TELEGRAM_AUTH_USER_ID` and
  `KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID` are valid DB UUIDs.
- Run only one polling adapter instance per bot token to avoid Telegram
  `409 Conflict`.

Repository template:

- `scripts/systemd/kragen-service.service`
- `scripts/systemd/kragen-service.env`
- `scripts/systemd/README.md` (copy/edit/start checklist)

Install template as a real unit:

```bash
sudo cp scripts/systemd/kragen-service.service /etc/systemd/system/kragen-service.service
sudo mkdir -p /etc/kragen
sudo cp scripts/systemd/kragen-service.env /etc/kragen/kragen-service.env
sudo systemctl daemon-reload
sudo systemctl enable --now kragen-service
sudo systemctl status kragen-service
```

The unit loads optional overrides from:

- `/etc/kragen/kragen-service.env` (via `EnvironmentFile=-...`)

The leading `-` means the file is optional; if absent, startup still works and
settings fall back to `kragen.yaml`.

### Recommended enablement flow

```bash
sudo mkdir -p /etc/kragen
sudo cp scripts/systemd/kragen-service.service /etc/systemd/system/kragen-service.service
sudo cp scripts/systemd/kragen-service.env /etc/kragen/kragen-service.env
sudo nano /etc/kragen/kragen-service.env
sudo systemctl daemon-reload
sudo systemctl enable --now kragen-service
sudo systemctl status kragen-service --no-pager
```

### Post-start validation

```bash
curl -sS http://127.0.0.1:8000/health
journalctl -u kragen-service -n 80 --no-pager
```

In healthy polling mode you should see repeated `getUpdates ... 200 OK` lines.

## Upgrading

```bash
git pull
pip install -e ".[dev]"
alembic upgrade head
systemctl restart kragen   # or your process manager
```

## Monitoring and logs

The app writes structured logs to stdout. Collect them with your infrastructure (journald, container log driver, Kubernetes agent). Prometheus metrics and OpenTelemetry tracing are **not** bundled in the application; add them via middleware or sidecars if needed.

### High-value log patterns

- `telegram_channel_start` — adapter boot completed.
- `telegram_http_error` + `409 Conflict` — duplicate polling consumers.
- `telegram_update_handle_failed` + `403 Forbidden` — auth/user mismatch for session posting.
- `object_storage_bucket_init_failed` — MinIO/S3 credentials or connectivity issue.

See `docs/OPERATIONS.md` for precise diagnosis and fixes for each pattern.
