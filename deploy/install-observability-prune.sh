#!/usr/bin/env bash
# install-observability-prune.sh - install the systemd timer that prunes
# mac's observability_events table daily.
#
# Background: ObservabilityService.prune() is unused. Without it mac.db grows
# unbounded (3.1GB and 2M+ rows observed on rocky as of 2026-05-28). This
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

# When FLEET_NAME != "mac", rewrite the unit's user-home reference (%h) is
# resolved by systemd at run-time; nothing template-specific to rewrite here.
sudo install -m 0644 "$SERVICE_TEMPLATE" "$SERVICE_DEST"
sudo install -m 0644 "$TIMER_TEMPLATE" "$TIMER_DEST"

sudo systemctl daemon-reload
sudo systemctl enable --now "$(basename "$TIMER_DEST")"

echo "[observability-prune] installed; next run:"
sudo systemctl list-timers --no-pager "$(basename "$TIMER_DEST")" || true
