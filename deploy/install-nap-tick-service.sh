#!/usr/bin/env bash
# install-nap-tick-service.sh - systemd timer that wakes every 15 minutes,
# runs `mac nap due` to find agents whose window has opened, and fires
# `mac nap cycle` for each. Makes the mem-08 nap consolidator truly
# autonomous; without this an operator has to call `mac nap cycle` by hand.
#
# Configure embeddings in /etc/mac/nap-tick.env (defaults to the hash
# stub when unset). With "auto" (the default) a model + base_url + key
# turns on real semantic embeddings:
#   MAC_MEMORY_EMBED_BACKEND=auto
#   MAC_MEMORY_EMBED_MODEL=nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2
#   MAC_QDRANT_URL=http://...:6333
# If MAC_QDRANT_URL is unset, the service falls back through
# QDRANT_URL, QDRANT_ADDRESS, and QDRANT_FLEET_URL before localhost.
set -euo pipefail

FLEET_NAME="${FLEET_NAME:-mac}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
UNIT_DIR="${WORKSPACE}/deploy/systemd"
SERVICE_TEMPLATE="${UNIT_DIR}/mac-nap-tick.service"
TIMER_TEMPLATE="${UNIT_DIR}/mac-nap-tick.timer"
SERVICE_DEST="/etc/systemd/system/${FLEET_NAME}-nap-tick.service"
TIMER_DEST="/etc/systemd/system/${FLEET_NAME}-nap-tick.timer"
ENV_DEST="/etc/${FLEET_NAME}/nap-tick.env"

# Mirror the install-observability-prune pattern: detect the user
# mac.service runs as and substitute into the unit template.
MAC_USER="${MAC_USER:-}"
MAC_HOME_DIR="${MAC_HOME_DIR:-}"
if [ -z "$MAC_USER" ] && [ -f "/etc/systemd/system/${FLEET_NAME}.service" ]; then
  MAC_USER="$(awk -F= '/^User=/{print $2; exit}' "/etc/systemd/system/${FLEET_NAME}.service" 2>/dev/null || true)"
fi
if [ -z "$MAC_USER" ]; then
  echo "[nap-tick] ERROR: could not detect MAC_USER; set it explicitly." >&2
  exit 1
fi
if [ -z "$MAC_HOME_DIR" ]; then
  MAC_HOME_DIR="$(getent passwd "$MAC_USER" | cut -d: -f6)/.mac"
fi

if [ ! -f "$SERVICE_TEMPLATE" ] || [ ! -f "$TIMER_TEMPLATE" ]; then
  echo "[nap-tick] ERROR: unit templates not found under $UNIT_DIR" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  echo "[nap-tick] ERROR: systemd not detected; this installer is Linux-only." >&2
  exit 1
fi

sudo install -d -m 0755 "$(dirname "$ENV_DEST")"
if [ ! -f "$ENV_DEST" ]; then
  sudo tee "$ENV_DEST" >/dev/null <<'ENV'
# Set MAC_MEMORY_EMBED_MODEL (with MAC_MEMORY_EMBED_BACKEND=auto, the
# default) to get real semantic recall. Leaving these unset uses the
# deterministic hash stub — round-trip works but recall isn't semantic.
#MAC_MEMORY_EMBED_BACKEND=auto
#MAC_MEMORY_EMBED_MODEL=nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2
#MAC_MEMORY_EMBED_INPUT_TYPE=passage
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

echo "[nap-tick] installed (User=${MAC_USER}, mac home=${MAC_HOME_DIR}); next run:"
sudo systemctl list-timers --no-pager "$(basename "$TIMER_DEST")" || true
