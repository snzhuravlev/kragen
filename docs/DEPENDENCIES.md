# Kragen dependencies

## Python (runtime)

Declared in `**pyproject.toml**` under `dependencies`.


| Package                             | Purpose                                       |
| ----------------------------------- | --------------------------------------------- |
| **fastapi**                         | HTTP API, request validation                  |
| **uvicorn[standard]**               | ASGI server                                   |
| **sqlalchemy[asyncio]**             | ORM, async engine                             |
| **asyncpg**                         | PostgreSQL driver for async SQLAlchemy        |
| **psycopg[binary]**                 | Sync PostgreSQL driver (Alembic + memory MCP) |
| **alembic**                         | Database schema migrations                    |
| **pydantic**, **pydantic-settings** | Settings and schemas                          |
| **httpx**                           | HTTP client (`agentctl` CLI)                  |
| **python-multipart**                | Multipart file uploads                        |
| **structlog**                       | Structured logging                            |
| **pyyaml**                          | Loading `configs/kragen.yaml`                 |
| **pgvector**                        | Vector types/helpers (migrations, future RAG) |
| **aioboto3**                        | Async S3-compatible API client                |
| **mcp**                             | MCP servers under `mcp_servers/`              |


Interpreter: `**requires-python = ">=3.12"`**.

## Python (development)

Optional group `**[project.optional-dependencies] dev`**:


| Package                        | Purpose                                         |
| ------------------------------ | ----------------------------------------------- |
| **pytest**, **pytest-asyncio** | Tests                                           |
| **ruff**                       | Linter                                          |
| **httpx**                      | Listed again for explicit CI installs if needed |


Install everything for development:

```bash
pip install -e ".[dev]"
```

## External services (not from pip)


| Service                          | Purpose                              |
| -------------------------------- | ------------------------------------ |
| **PostgreSQL** with **pgvector** | Primary store and vector type        |
| **S3-compatible API**            | Uploaded files (MinIO, AWS S3, etc.) |


Redis and similar components are **not required** for a minimal API run in the current version.

## Web UI (browser only)

The static page `web/index.html` loads **marked** (Markdown) and **DOMPurify** (HTML sanitization) from a public CDN. They are **not** Python package dependencies and are not required for API-only or CLI workflows. If your environment blocks CDNs, replace or bundle those scripts locally and adjust the HTML.

## Transitive dependencies

`pip` resolves the full tree at install time. To pin versions for reproducibility:

```bash
pip freeze > requirements-lock.txt
```

Whether to commit the lockfile is a team policy decision.