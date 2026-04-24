# Kragen

Multi-channel agent platform (MVP): HTTP API, sessions and tasks, file uploads to object storage, MCP tool servers, minimal Web UI, and CLI.

Long-term memory for Cursor is provided through `mcp_servers/memory_mcp`, backed by PostgreSQL.

## Documentation


| Document                                     | Description                            |
| -------------------------------------------- | -------------------------------------- |
| [docs/INSTALL.md](docs/INSTALL.md)                       | Installation and first run              |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)             | Architecture and data flow              |
| [docs/ARCHITECTURE_REVIEW.md](docs/ARCHITECTURE_REVIEW.md) | Architecture review: strengths, risks, backlog |
| [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md)             | Python and external dependencies        |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)                 | Configuration, migrations, production   |
| [docs/OPERATIONS.md](docs/OPERATIONS.md)                 | Detailed runbooks and troubleshooting   |
| [docs/PLUGINS.md](docs/PLUGINS.md)                       | Plugin contract (skills, MCP, routers)  |


## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Edit `configs/kragen.yaml` (at minimum `database.url`), then:

```bash
alembic upgrade head
python scripts/seed_data.py
uvicorn kragen.api.main:app --host 0.0.0.0 --port 8000
```

- API docs: `http://localhost:8000/docs`
- Web UI: `http://localhost:8000/ui/index.html` (override API and token with `?api=` and `?token=`)

Cursor MCP example: `configs/mcp/cursor-mcp.example.json`.

### Web UI (summary)

The static UI under `/ui` is a two-column console: **Chat** (workspace, composer, message history) and **Configuration** (API URL, Bearer token, connection check). Use the sidebar for session actions and **Cursor Agent Authentication**:

- **Check Auth Status** — current Cursor CLI login state on the API host
- **Start Login Flow** — browser URL for `cursor agent login` (headless)

Message history is rendered as **Markdown** (sanitized in the browser). Task output is still delivered over SSE in the background until the task finishes; there is no separate “live stream” panel.

The **Configuration** tab can show the **resolved `kragen.yaml`** file as served by the API (`GET /admin/config/kragen-yaml`, authenticated). That endpoint reads the same path as application settings (`KRAGEN_CONFIG_FILE` or `configs/kragen.yaml` relative to the process).

To execute real tasks through Cursor Agent, authenticate once on the host:

```bash
cursor agent login
```

The OpenClaw channel is disabled by default (`channels.openclaw_enabled: false` in `configs/kragen.yaml`). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for admin endpoints and security notes.

For a single-process supervisor that starts both API and Telegram channel
adapter, use `kragen-service` (documented in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).