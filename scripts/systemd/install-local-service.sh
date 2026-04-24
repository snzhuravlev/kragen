#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="kragen-service"
UNIT_SOURCE="scripts/systemd/kragen-service.service"
ENV_SOURCE="scripts/systemd/kragen-service.env"
UNIT_TARGET="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_TARGET_DIR="/etc/kragen"
ENV_TARGET="${ENV_TARGET_DIR}/kragen-service.env"

if [[ ! -f "${UNIT_SOURCE}" ]]; then
  echo "Missing ${UNIT_SOURCE}"
  exit 1
fi

echo "Installing ${SERVICE_NAME} unit..."
sudo mkdir -p "${ENV_TARGET_DIR}"
sudo cp "${UNIT_SOURCE}" "${UNIT_TARGET}"

if [[ ! -f "${ENV_TARGET}" ]]; then
  echo "Installing default env file..."
  sudo cp "${ENV_SOURCE}" "${ENV_TARGET}"
else
  echo "Keeping existing env file: ${ENV_TARGET}"
fi

echo "Reloading systemd and enabling service..."
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}"
sudo systemctl status "${SERVICE_NAME}" --no-pager

echo
echo "Service installed: ${SERVICE_NAME}"
echo "Follow logs: sudo journalctl -u ${SERVICE_NAME} -f"
