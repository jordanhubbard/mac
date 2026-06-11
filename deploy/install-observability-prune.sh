#!/usr/bin/env bash
# install-observability-prune.sh - install the systemd timer that prunes
# mac's observability_events table daily.
#
# Background: ObservabilityService.prune() is unused. Without it mac.db grows
# unbounded (3.1GB and 2M+ rows observed on hosta as of 2026-05-28). This
# timer calls `mac observability prune` once a day to bound the table.
#
# The hub agent must be running so the CLI can reach the control plane.
# Configure retention via /etc/mac/observability-prune.env:
#   MAC_PRUNE_LOG_DAYS=7
#   MAC_PRUNE_KEEP_LAST=500000
set -euo pipefail

FLEET_NAME="${FLEET_NAME:-mac}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
UNIT_DIR="${WORKSPACE}/deploy/systemd"
SERVICE_TEMPLATE="${UNIT_DIR}/mac-observability-prune.service"
TIMER_TEMPLATE="${UNIT_DIR}/mac-observability-prune.timer"
SERVICE_DEST="/etc/systemd/system/${FLEET_NAME}-observability-prune.service"
TIMER_DEST="/etc/systemd/system/${FLEET_NAME}-observability-prune.timer"
ENV_DEST="/etc/${FLEET_NAME}/observability-prune.env"

# Detect the user mac.service runs as (the deploy convention bakes
# User=<user> into the unit; we mirror it so the prune timer runs as the
# same user and can read ~/.mac/mac.env and call ~/.mac/venv/bin/mac).
MAC_USER="${MAC_USER:-}"
MAC_HOME_DIR="${MAC_HOME_DIR:-}"
if [ -z "$MAC_USER" ]; then
  if [ -f "/etc/systemd/system/${FLEET_NAME}.service" ]; then
    MAC_USER="$(awk -F= '/^User=/{print $2; exit}' "/etc/systemd/system/${FLEET_NAME}.service" 2>/dev/null || true)"
  fi
fi
if [ -z "$MAC_USER" ]; then
  echo "[observability-prune] ERROR: could not detect MAC_USER from ${FLEET_NAME}.service; set MAC_USER explicitly." >&2
  exit 1
fi
if [ -z "$MAC_HOME_DIR" ]; then
  MAC_HOME_DIR="$(getent passwd "$MAC_USER" | cut -d: -f6)/.mac"
fi

if [ ! -f "$SERVICE_TEMPLATE" ] || [ ! -f "$TIMER_TEMPLATE" ]; then
  echo "[observability-prune] ERROR: unit templates not found under $UNIT_DIR" >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  echo "[observability-prune] ERROR: systemd not detected; this installer is Linux-only." >&2
  exit 1
fi

sudo install -d -m 0755 "$(dirname "$ENV_DEST")"
if [ ! -f "$ENV_DEST" ]; then
  sudo tee "$ENV_DEST" >/dev/null <<'ENV'
# Retention for mac observability_events table. Adjust per fleet.
MAC_PRUNE_LOG_DAYS=7
MAC_PRUNE_KEEP_LAST=500000
ENV
  sudo chmod 0644 "$ENV_DEST"
fi

# Render the service template with the deploy-time user / home.
rendered="$(mktemp)"
sed -e "s|__MAC_USER__|${MAC_USER}|g" -e "s|__MAC_HOME__|${MAC_HOME_DIR}|g" \
    "$SERVICE_TEMPLATE" > "$rendered"
sudo install -m 0644 "$rendered" "$SERVICE_DEST"
rm -f "$rendered"
sudo install -m 0644 "$TIMER_TEMPLATE" "$TIMER_DEST"

sudo systemctl daemon-reload
sudo systemctl enable --now "$(basename "$TIMER_DEST")"

echo "[observability-prune] installed (User=${MAC_USER}, mac home=${MAC_HOME_DIR}); next run:"
sudo systemctl list-timers --no-pager "$(basename "$TIMER_DEST")" || true
