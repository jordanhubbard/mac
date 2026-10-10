#!/usr/bin/env bash
# install-fleet-context-service.sh - install or repair the periodic
# `mac admin fleet refresh-context` job on this host (systemd timer on Linux,
# LaunchAgent on macOS), which keeps the live "Fleet — your teammates" block in
# this agent's runtime-context markdown current (fleet-02). Run it as the user
# the MAC agent runs as. scripts/fleet-update runs the same module on every
# host; see src/mac/fleet_context_service.py.
set -euo pipefail

MAC_HOME_DIR="${MAC_HOME_DIR:-$HOME/.mac}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
[ -n "$WORKSPACE" ] || { echo "[fleet-context] ERROR: set WORKSPACE to the MAC checkout." >&2; exit 1; }

exec "$MAC_HOME_DIR/venv/bin/python" -m mac.fleet_context_service \
  --source "$WORKSPACE" --mac-home "$MAC_HOME_DIR"
