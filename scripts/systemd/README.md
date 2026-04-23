# systemd templates

This directory contains ready-to-copy templates for running Kragen under
`systemd`.

## Files

- `kragen-service.service` — one parent service that starts both API and
  Telegram adapter (`kragen-service`).
- `kragen-service.env` — optional environment override file loaded by the unit.

## Quick setup checklist

1) Copy templates to system locations:

```bash
sudo mkdir -p /etc/kragen
sudo cp scripts/systemd/kragen-service.service /etc/systemd/system/kragen-service.service
sudo cp scripts/systemd/kragen-service.env /etc/kragen/kragen-service.env
```

2) Edit runtime values:

```bash
sudo nano /etc/kragen/kragen-service.env
```

At minimum, set:

- `KRAGEN_TELEGRAM_BOT_TOKEN`
- `KRAGEN_TELEGRAM_AUTH_USER_ID`
- `KRAGEN_TELEGRAM_DEFAULT_WORKSPACE_ID`

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

## Common operations

Restart after config/env changes:

```bash
sudo systemctl restart kragen-service
```

Disable service:

```bash
sudo systemctl disable --now kragen-service
```
