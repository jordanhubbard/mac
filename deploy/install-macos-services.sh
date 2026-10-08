#!/usr/bin/env bash
# install-macos-services.sh — install launchd services for mac hub on macOS.
#
# Qdrant uses the shared installer.
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

# This installer renders the selected paths, preserves the Qdrant data directory,
# creates LaunchAgents, and rolls back the launchd transaction on failed health.
QDRANT_SUPERVISOR=launchd bash "$SCRIPT_DIR/install-qdrant-service.sh"
echo "[mac] Qdrant installation verified. No separate database or timer was installed."
