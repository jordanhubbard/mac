#!/usr/bin/env bash
# install-wildcard-refresh-service.sh - systemd timer that runs weekly and
# calls `mac tokenhub refresh-wildcards` to refresh TokenHub's wildcard model
# ladder from current availability/quality/cost (mac-nyx7). Without this an
# operator has to refresh the ladder by hand.
#
# The /admin/v1/wildcard-models route needs admin auth. Put the TokenHub admin
# token in /etc/mac/wildcard-refresh.env:
#   MAC_TOKENHUB_ADMIN_TOKEN=...        # or TOKENHUB_ADMIN_TOKEN
#   # MAC_TOKENHUB_WILDCARD_URL=...     # override; default TOKENHUB_URL/admin/v1/wildcard-models
#   # MAC_TOKENHUB_WILDCARD_METHOD=GET  # POST if your TokenHub treats it as a recompute trigger
set -euo pipefail

FLEET_NAME="${FLEET_NAME:-mac}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
UNIT_DIR="${WORKSPACE}/deploy/systemd"
SERVICE_TEMPLATE="${UNIT_DIR}/mac-wildcard-refresh.service"
TIMER_TEMPLATE="${UNIT_DIR}/mac-wildcard-refresh.timer"
SERVICE_DEST="/etc/systemd/system/${FLEET_NAME}-wildcard-refresh.service"
TIMER_DEST="/etc/systemd/system/${FLEET_NAME}-wildcard-refresh.timer"
ENV_DEST="/etc/${FLEET_NAME}/wildcard-refresh.env"

# Detect the user mac.service runs as and substitute into the unit template.
MAC_USER="${MAC_USER:-}"
MAC_HOME_DIR="${MAC_HOME_DIR:-}"
if [ -z "$MAC_USER" ] && [ -f "/etc/systemd/system/${FLEET_NAME}.service" ]; then
  MAC_USER="$(awk -F= '/^User=/{print $2; exit}' "/etc/systemd/system/${FLEET_NAME}.service" 2>/dev/null || true)"
fi
if [ -z "$MAC_USER" ]; then
  echo "[wildcard-refresh] ERROR: could not detect MAC_USER; set it explicitly." >&2
  exit 1
fi
if [ -z "$MAC_HOME_DIR" ]; then
  MAC_HOME_DIR="$(getent passwd "$MAC_USER" | cut -d: -f6)/.mac"
fi

if [ ! -f "$SERVICE_TEMPLATE" ] || [ ! -f "$TIMER_TEMPLATE" ]; then
  echo "[wildcard-refresh] ERROR: unit templates not found under $UNIT_DIR" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  echo "[wildcard-refresh] ERROR: systemd not detected; this installer is Linux-only." >&2
  exit 1
fi

sudo install -d -m 0755 "$(dirname "$ENV_DEST")"
if [ ! -f "$ENV_DEST" ]; then
  sudo tee "$ENV_DEST" >/dev/null <<'ENV'
# TokenHub admin token for /admin/v1/wildcard-models (admin auth, not the
# agent chat key). Until this is set, the timer is a clean no-op.
#MAC_TOKENHUB_ADMIN_TOKEN=
#MAC_TOKENHUB_WILDCARD_URL=
#MAC_TOKENHUB_WILDCARD_METHOD=GET
ENV
  sudo chmod 0640 "$ENV_DEST"
fi

rendered="$(mktemp)"
sed -e "s|__MAC_USER__|${MAC_USER}|g" -e "s|__MAC_HOME__|${MAC_HOME_DIR}|g" \
    "$SERVICE_TEMPLATE" > "$rendered"
sudo install -m 0644 "$rendered" "$SERVICE_DEST"
rm -f "$rendered"
sudo install -m 0644 "$TIMER_TEMPLATE" "$TIMER_DEST"

sudo systemctl daemon-reload
sudo systemctl enable --now "$(basename "$TIMER_DEST")"

echo "[wildcard-refresh] installed (User=${MAC_USER}, mac home=${MAC_HOME_DIR}); next run:"
sudo systemctl list-timers --no-pager "$(basename "$TIMER_DEST")" || true
