# systemd templates

This directory contains ready-to-copy templates for running Kragen under
`systemd`.

## Files

- `kragen-service.service` — one parent service that starts both API and
  Telegram adapter (`kragen-service`).
- `kragen-service.env` — optional environment override file loaded by the unit.
- `install-local-service.sh` — helper script to install unit + env for local
  workstation setup.

## Quick setup checklist

1) Copy templates to system locations:

```bash
sudo mkdir -p /etc/kragen
sudo cp scripts/systemd/kragen-service.service /etc/systemd/system/kragen-service.service
sudo cp scripts/systemd/kragen-service.env /etc/kragen/kragen-service.env
```

Alternative (local helper):

```bash
./scripts/systemd/install-local-service.sh
```

2) Edit runtime values:

```bash
sudo nano /etc/kragen/kragen-service.env
```

At minimum, set:

- `KRAGEN_DATABASE__URL`
- `KRAGEN_STORAGE__ENDPOINT_URL`
- `KRAGEN_STORAGE__ACCESS_KEY`
- `KRAGEN_STORAGE__SECRET_KEY`
- `KRAGEN_STORAGE__BUCKET`
- `KRAGEN_TELEGRAM_BOT_TOKEN`
- `KRAGEN_TELEGRAM_AUTH_USER_ID`
- `KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID`
- `KRAGEN_AUTH__DISABLED=false`

3) Ensure config path points to production YAML:

- In unit or env, set `KRAGEN_CONFIG_FILE=/etc/kragen/kragen.yaml`.

4) Reload and enable service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kragen-service
```

5) Verify service health:

```bash
sudo systemctl status kragen-service
journalctl -u kragen-service -f
```

6) Verify API endpoint:

```bash
curl -sS http://127.0.0.1:8000/health
```

Expected: `{"status":"ok"}`.

7) If Telegram mode is `webhook`, verify adapter probes:

```bash
curl -sS http://127.0.0.1:8081/health
curl -sS http://127.0.0.1:8081/ready
```

## Troubleshooting quick map

- `409 Conflict` on Telegram `getUpdates`
  - More than one polling adapter instance uses the same token.
  - Stop duplicate processes and keep only `kragen-service`.

- `403 Forbidden` on `/sessions/{id}/messages` from adapter
  - Service auth mode/user mismatch.
  - Ensure `KRAGEN_AUTH__DISABLED=false` and correct Telegram auth UUIDs.

- `InvalidAccessKeyId` / object storage init warning
  - MinIO credentials in `kragen-service.env` do not match MinIO server.
  - Update keys and restart service.

For full runbooks, see `docs/OPERATIONS.md`.

## Common operations

Restart after config/env changes:

```bash
sudo systemctl restart kragen-service
```

Disable service:

```bash
sudo systemctl disable --now kragen-service
```
