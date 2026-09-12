#!/usr/bin/env bash
# install-macos-services.sh — install launchd services for mac hub on macOS.
#
# Installs:
#   com.mac.qdrant       — Qdrant vector store, persistent, port 6333
#   com.mac.dream-cycle  — hourly dream cycle (freeze→extract→promote)
#
# Idempotent. Run again to update after config changes.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -P -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHD_DIR="$SCRIPT_DIR/launchd"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
LOG_DIR="$MAC_HOME/logs"

mkdir -p "$LOG_DIR"

install_service() {
  local label="$1"
  local plist="$LAUNCHD_DIR/${label}.plist"
  local dest="$LAUNCH_AGENTS/${label}.plist"

  if [ ! -f "$plist" ]; then
    echo "[mac] ERROR: plist not found: $plist" >&2
    return 1
  fi

  # Unload if already loaded (ignore errors — may not be loaded yet)
  launchctl unload "$dest" 2>/dev/null || true

  cp "$plist" "$dest"
  launchctl load "$dest"
  echo "[mac] installed and started: $label"
}

echo "[mac] Installing macOS launchd services..."
install_service "com.mac.qdrant"
install_service "com.mac.dream-cycle"

echo "[mac] Waiting for Qdrant to start..."
for i in $(seq 1 10); do
  if curl -sf http://127.0.0.1:6333/health >/dev/null 2>&1; then
    echo "[mac] Qdrant is healthy."
    break
  fi
  sleep 1
done

echo "[mac] Done. Services installed:"
launchctl list | grep com.mac || true
