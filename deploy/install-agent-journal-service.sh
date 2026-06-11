#!/usr/bin/env bash
# install-agent-journal-service.sh - systemd timer that runs `mac journal
# snapshot` once a day, capturing this agent's soul + memory state (SOUL.md,
# USER.md, MEMORY.md, memories/, mood, config) into $HOME/.mac/journal/<date>/.
#
# An agent's evolved personality is irreplaceable; this keeps dated, restorable
# backups so a wiped host or a bad redeploy can't silently erase it.
#
# To also ship copies off-host (e.g. a cloud blob store), set a backup hook in
# /etc/mac/agent-journal.env — a shell command run after each snapshot with
# MAC_JOURNAL_PATH / _DATE / _AGENT / _MANIFEST in the environment, e.g.:
#   MAC_JOURNAL_BACKUP_HOOK='aws s3 cp --recursive "$MAC_JOURNAL_PATH" "s3://mac-agent-journals/$MAC_JOURNAL_AGENT/$MAC_JOURNAL_DATE/"'
set -euo pipefail

FLEET_NAME="${FLEET_NAME:-mac}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
UNIT_DIR="${WORKSPACE}/deploy/systemd"
SERVICE_TEMPLATE="${UNIT_DIR}/mac-agent-journal.service"
TIMER_TEMPLATE="${UNIT_DIR}/mac-agent-journal.timer"
SERVICE_DEST="/etc/systemd/system/${FLEET_NAME}-agent-journal.service"
TIMER_DEST="/etc/systemd/system/${FLEET_NAME}-agent-journal.timer"
ENV_DEST="/etc/${FLEET_NAME}/agent-journal.env"

# Detect the user mac.service runs as and substitute into the unit template.
MAC_USER="${MAC_USER:-}"
MAC_HOME_DIR="${MAC_HOME_DIR:-}"
if [ -z "$MAC_USER" ] && [ -f "/etc/systemd/system/${FLEET_NAME}.service" ]; then
  MAC_USER="$(awk -F= '/^User=/{print $2; exit}' "/etc/systemd/system/${FLEET_NAME}.service" 2>/dev/null || true)"
fi
if [ -z "$MAC_USER" ]; then
  echo "[agent-journal] ERROR: could not detect MAC_USER; set it explicitly." >&2
  exit 1
fi
if [ -z "$MAC_HOME_DIR" ]; then
  MAC_HOME_DIR="$(getent passwd "$MAC_USER" | cut -d: -f6)/.mac"
fi

if [ ! -f "$SERVICE_TEMPLATE" ] || [ ! -f "$TIMER_TEMPLATE" ]; then
  echo "[agent-journal] ERROR: unit templates not found under $UNIT_DIR" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
  echo "[agent-journal] ERROR: systemd not detected; this installer is Linux-only." >&2
  exit 1
fi

sudo install -d -m 0755 "$(dirname "$ENV_DEST")"
if [ ! -f "$ENV_DEST" ]; then
  sudo tee "$ENV_DEST" >/dev/null <<'ENV'
# Optional off-host archival. MAC_JOURNAL_BACKUP_HOOK runs after each daily
# snapshot with MAC_JOURNAL_PATH / MAC_JOURNAL_DATE / MAC_JOURNAL_AGENT /
# MAC_JOURNAL_MANIFEST set. Leave unset to keep journals local only.
#MAC_JOURNAL_BACKUP_HOOK='aws s3 cp --recursive "$MAC_JOURNAL_PATH" "s3://mac-agent-journals/$MAC_JOURNAL_AGENT/$MAC_JOURNAL_DATE/"'
#MAC_JOURNAL_DIR=
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

echo "[agent-journal] installed (User=${MAC_USER}, mac home=${MAC_HOME_DIR}); next run:"
sudo systemctl list-timers --no-pager "$(basename "$TIMER_DEST")" || true
