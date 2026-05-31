#!/usr/bin/env bash
# install-fleet-context-service.sh - systemd timer (every 3 min) that runs
# `mac fleet refresh-context`, refreshing the live "Fleet — your teammates"
# block in this agent's runtime-context markdown (fleet-02). Makes each agent
# passively aware of what the others are doing at all times; without it an
# operator would have to refresh the fleet view by hand.
set -euo pipefail

FLEET_NAME="${FLEET_NAME:-mac}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
UNIT_DIR="${WORKSPACE}/deploy/systemd"
SERVICE_TEMPLATE="${UNIT_DIR}/mac-fleet-context.service"
TIMER_TEMPLATE="${UNIT_DIR}/mac-fleet-context.timer"
SERVICE_DEST="/etc/systemd/system/${FLEET_NAME}-fleet-context.service"
TIMER_DEST="/etc/systemd/system/${FLEET_NAME}-fleet-context.timer"
ENV_DEST="/etc/${FLEET_NAME}/fleet-context.env"

# Mirror the install-observability-prune pattern: detect the user
# mac.service runs as and substitute into the unit template.
MAC_USER="${MAC_USER:-}"
MAC_HOME_DIR="${MAC_HOME_DIR:-}"
if [ -z "$MAC_USER" ] && [ -f "/etc/systemd/system/${FLEET_NAME}.service" ]; then
  MAC_USER="$(awk -F= '/^User=/{print $2; exit}' "/etc/systemd/system/${FLEET_NAME}.service" 2>/dev/null || true)"
fi
if [ -z "$MAC_USER" ]; then
  echo "[fleet-context] ERROR: could not detect MAC_USER; set it explicitly." >&2
  exit 1
fi
if [ -z "$MAC_HOME_DIR" ]; then
  MAC_HOME_DIR="$(getent passwd "$MAC_USER" | cut -d: -f6)/.mac"
fi

if [ ! -f "$SERVICE_TEMPLATE" ] || [ ! -f "$TIMER_TEMPLATE" ]; then
  echo "[fleet-context] ERROR: unit templates not found under $UNIT_DIR" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  echo "[fleet-context] ERROR: systemd not detected; this installer is Linux-only." >&2
  exit 1
fi

sudo install -d -m 0755 "$(dirname "$ENV_DEST")"
if [ ! -f "$ENV_DEST" ]; then
  sudo tee "$ENV_DEST" >/dev/null <<'ENV'
# Optional overrides for the fleet-context refresher. The agent id + runtime
# context markdown path are normally read from mac.env; set them here only to
# override.
#MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN=
ENV
  sudo chmod 0644 "$ENV_DEST"
fi

rendered="$(mktemp)"
sed -e "s|__MAC_USER__|${MAC_USER}|g" -e "s|__MAC_HOME__|${MAC_HOME_DIR}|g" \
    "$SERVICE_TEMPLATE" > "$rendered"
sudo install -m 0644 "$rendered" "$SERVICE_DEST"
rm -f "$rendered"
sudo install -m 0644 "$TIMER_TEMPLATE" "$TIMER_DEST"

sudo systemctl daemon-reload
sudo systemctl enable --now "$(basename "$TIMER_DEST")"

echo "[fleet-context] installed (User=${MAC_USER}, mac home=${MAC_HOME_DIR}); next run:"
sudo systemctl list-timers --no-pager "$(basename "$TIMER_DEST")" || true
