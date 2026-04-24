# Kragen Operations Runbook

This runbook is for operators who run Kragen with Telegram integration and
systemd. It focuses on day-2 operations: validation, troubleshooting, safe
config changes, and incident response.

## Scope

This document assumes:

- the repository lives at `/home/srg/projects/kragen` (adjust paths as needed),
- service management is done with `systemd`,
- Telegram channel runs in polling mode by default,
- secrets are stored in `/etc/kragen/kragen-service.env`,
- `configs/kragen.yaml` contains placeholders, not production secrets.

## Service Topology

`kragen-service` is a parent process that starts and supervises:

- API child: `uvicorn kragen.api.main:app`
- Telegram child: `python -m kragen.channels.telegram_adapter`

If one child exits unexpectedly, the parent stops the other and exits non-zero.
`systemd` then restarts the unit according to restart policy.

## Configuration Model (Critical)

Runtime precedence (highest first):

1. process environment (including `Environment=` and `EnvironmentFile=` in unit)
2. `.env` in working directory
3. `configs/kragen.yaml`

Operational rule:

- keep secrets in `/etc/kragen/kragen-service.env`,
- keep placeholders in `configs/kragen.yaml`,
- keep `.env` minimal for local interactive development only.

## Canonical Runtime Files

- Unit file: `/etc/systemd/system/kragen-service.service`
- Runtime env file: `/etc/kragen/kragen-service.env`
- Application config template: `/home/srg/projects/kragen/configs/kragen.yaml`

## Quick Health Checklist

Run these in order:

```bash
sudo systemctl status kragen-service --no-pager
curl -sS http://127.0.0.1:8000/health
journalctl -u kragen-service -n 80 --no-pager
```

Healthy signals:

- unit is `active (running)`,
- API `/health` returns `{"status":"ok"}`,
- Telegram logs show `getUpdates ... "HTTP/1.1 200 OK"`.

## Telegram Integration Validation

### 1) Verify bot token

```bash
source /home/srg/projects/kragen/.venv/bin/activate
PYTHONPATH=/home/srg/projects/kragen/src python - <<'PY'
import asyncio, httpx, os
token = os.environ.get("KRAGEN_TELEGRAM_BOT_TOKEN")
if not token:
    raise SystemExit("KRAGEN_TELEGRAM_BOT_TOKEN is missing")
async def main():
    async with httpx.AsyncClient() as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/getMe", json={}, timeout=20)
        r.raise_for_status()
        print(r.json())
asyncio.run(main())
PY
```

Expected: `ok: true` and your bot username.

### 2) Verify webhook state when using polling

Polling and webhook must not compete.

```bash
source /home/srg/projects/kragen/.venv/bin/activate
PYTHONPATH=/home/srg/projects/kragen/src python - <<'PY'
import asyncio, httpx, os
token = os.environ["KRAGEN_TELEGRAM_BOT_TOKEN"]
async def main():
    async with httpx.AsyncClient() as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/getWebhookInfo", json={}, timeout=20)
        r.raise_for_status()
        print(r.json())
asyncio.run(main())
PY
```

For polling, `result.url` should be empty.

### 3) Verify adapter uniqueness (avoid 409 conflicts)

```bash
ps -ef | rg "kragen.channels.telegram_adapter"
```

Only one active adapter process should exist for the same token.

## MinIO / S3 Validation

### 1) Endpoint liveness

```bash
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9000/minio/health/live
```

Expected: `200`.

### 2) Port listeners

```bash
ss -ltnp | rg ":9000\\b|:9001\\b"
```

Expected:

- `9000` for S3 API
- `9001` for MinIO console

### 3) Credential-level S3 test

```bash
source /home/srg/projects/kragen/.venv/bin/activate
PYTHONPATH=/home/srg/projects/kragen/src python - <<'PY'
import asyncio
import aioboto3
from botocore.config import Config
from kragen.config import get_settings

s = get_settings().storage

async def main():
    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=s.endpoint_url,
        aws_access_key_id=s.access_key,
        aws_secret_access_key=s.secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    ) as c:
        print(await c.list_buckets())
asyncio.run(main())
PY
```

Expected: no `InvalidAccessKeyId` and bucket list output.

## Incident Catalog and Fixes

### `409 Conflict` on `getUpdates`

Symptom:

- logs repeatedly show `getUpdates ... 409 Conflict`.

Root cause:

- more than one polling consumer for the same bot token, or webhook enabled.

Fix:

1. stop duplicate adapter processes,
2. ensure only `kragen-service` runs adapter,
3. if polling mode: clear webhook URL,
4. restart service.

### `403 Forbidden` on `/sessions/{id}/messages` from Telegram adapter

Symptom:

- adapter logs show:
  - `POST /sessions/.../messages ... 403 Forbidden`
  - `telegram_update_handle_failed` with `HTTPStatusError 403`.

Typical causes:

- API running in dev auth mode (`KRAGEN_AUTH__DISABLED=true`) while adapter uses
  Bearer UUID auth user,
- bound session belongs to a different user than
  `KRAGEN_TELEGRAM_AUTH_USER_ID`.

Fix:

1. set `KRAGEN_AUTH__DISABLED=false` for `kragen-service`,
2. ensure `KRAGEN_TELEGRAM_AUTH_USER_ID` and
   `KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID` point to real DB entities,
3. restart service,
4. validate by posting a test message and checking for `200 OK`.

### MinIO `InvalidAccessKeyId` during startup

Symptom:

- API startup logs show `object_storage_bucket_init_failed` with
  `InvalidAccessKeyId`.

Root cause:

- mismatch between Kragen S3 credentials and MinIO service credentials.

Fix:

1. inspect MinIO credentials source (e.g. `/etc/default/minio`),
2. set matching `KRAGEN_STORAGE__ACCESS_KEY` and `KRAGEN_STORAGE__SECRET_KEY`
   in `/etc/kragen/kragen-service.env`,
3. restart `kragen-service`.

## Safe Change Procedure

For all runtime config changes:

1. edit `/etc/kragen/kragen-service.env`,
2. run `sudo systemctl daemon-reload` only if unit file changed,
3. restart service: `sudo systemctl restart kragen-service`,
4. verify health and logs,
5. run a Telegram smoke test (`/start` + regular message).

## Telegram Smoke Test Script

Manual smoke test:

1. send `/start` to bot,
2. send `smoke test`,
3. expect:
   - processing stub in Telegram,
   - final assistant response,
   - no `403`/`409` errors in logs.

Log verification command:

```bash
journalctl -u kragen-service --since "5 min ago" --no-pager \
  | rg "403 Forbidden|409 Conflict|telegram_update_handle_failed|getUpdates|sendMessage|editMessageText"
```

## Recovery Actions

### Full service recycle

```bash
sudo systemctl restart kragen-service
sudo systemctl status kragen-service --no-pager
```

### Stop old manual processes that interfere with systemd

```bash
pkill -f "kragen.channels.telegram_adapter" || true
pkill -f "uvicorn kragen.api.main:app" || true
sudo systemctl restart kragen-service
```

### Validate DB entities for Telegram auth user/workspace

```bash
source /home/srg/projects/kragen/.venv/bin/activate
PYTHONPATH=/home/srg/projects/kragen/src python - <<'PY'
import asyncio
from sqlalchemy import text
from kragen.db.session import async_session_factory

USER_ID = "00000000-0000-0000-0000-000000001111"
WS_ID = "00000000-0000-0000-0000-000000001111"

async def main():
    async with async_session_factory() as db:
        u = await db.execute(text("select id from users where id=:id"), {"id": USER_ID})
        w = await db.execute(text("select id from workspaces where id=:id"), {"id": WS_ID})
        print("user_exists", u.first() is not None)
        print("workspace_exists", w.first() is not None)
asyncio.run(main())
PY
```

## Security Notes

- Do not commit real bot tokens, DB passwords, or S3 secrets.
- Keep secrets in root-owned files under `/etc/kragen` with restricted permissions.
- Prefer secret manager integration for production.
- Treat `/admin/*` as privileged and non-public.

## Change Log Guidance

When updating runtime behavior, update these documents together:

- `docs/DEPLOYMENT.md` for architecture-level deployment guidance,
- `docs/OPERATIONS.md` for troubleshooting and runbooks,
- `scripts/systemd/README.md` for operator command quick-reference.
