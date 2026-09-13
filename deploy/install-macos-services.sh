#!/usr/bin/env bash
# install-macos-services.sh — install launchd services for mac hub on macOS.
#
# Qdrant uses the shared installer. Dream scheduling belongs to the hub's
# NapTicker, which already calls run_dream_cycle against the hub authority.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -P -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
LOG_DIR="${LOG_DIR:-$MAC_HOME/logs}"
WORKSPACE="${WORKSPACE:-$(dirname "$SCRIPT_DIR")}"
FLEET_NAME="${FLEET_NAME:-mac}"
export MAC_HOME LOG_DIR WORKSPACE FLEET_NAME

if [ "$#" -ne 0 ]; then
  echo "Usage: install-macos-services.sh (configure with the shared Qdrant installer's environment variables)" >&2
  exit 2
fi

if [ "$(uname -s)" != Darwin ]; then
  echo "[mac] ERROR: this entrypoint requires macOS; use install-qdrant-service.sh on other hosts." >&2
  exit 1
fi

# Do not silently leave a second dream scheduler, or remove an operator's job.
# Inspect before the shared installer can stop or replace any Qdrant service.
# shellcheck source=lib/launchd-lifecycle.sh
. "$SCRIPT_DIR/lib/launchd-lifecycle.sh"
dream_label="com.mac.dream-cycle"
dream_plist="$HOME/Library/LaunchAgents/$dream_label.plist"
dream_state="$(mac_launchd_job_state "gui/$(id -u)/$dream_label" "$dream_label")"
if [ -e "$dream_plist" ] || [ -L "$dream_plist" ] || [ "$dream_state" = active ]; then
  echo "[mac] ERROR: standalone $dream_label exists. Reconcile its ownership with the hub nap scheduler before installing; no services changed." >&2
  exit 1
fi

# This installer renders the selected paths, preserves the Qdrant data directory,
# creates LaunchAgents, and rolls back the launchd transaction on failed health.
QDRANT_SUPERVISOR=launchd bash "$SCRIPT_DIR/install-qdrant-service.sh"
echo "[mac] Qdrant installation verified. Dream scheduling remains owned by the hub (MAC_NAP_TICK_ENABLED); no separate database or timer was installed."
