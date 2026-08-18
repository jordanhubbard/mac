#!/usr/bin/env bash
set -euo pipefail

# Install the Darwin hub's durable, credential-free transport to a Linux
# OpenShell gateway. The certifier talks only to the local endpoint; SSH owns
# transport confidentiality and host authentication. The Linux gateway must
# separately restrict :17670 to loopback and Docker bridges.

TARGET=""
SSH_PORT="22"
LOCAL_PORT="17671"
REMOTE_PORT="17670"
LABEL="com.mac.certifier-openshell-tunnel"
OPENSH_BIN="${MAC_OPENSHELL_BIN:-}"
REMOVE=0

usage() {
  cat <<'EOF'
Usage:
  install-certifier-gateway-tunnel.sh --target user@host [options]
  install-certifier-gateway-tunnel.sh --remove [--label label]

Options:
  --target user@host       Linux gateway SSH destination (required to install)
  --ssh-port port          SSH port (default: 22)
  --local-port port        Darwin loopback port (default: 17671)
  --remote-port port       Linux gateway loopback port (default: 17670)
  --label label            launchd label
  --openshell-bin path     Exact OpenShell CLI used for the health proof
  --remove                 Remove the launchd tunnel
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:?--target requires user@host}"; shift 2 ;;
    --target=*) TARGET="${1#--target=}"; shift ;;
    --ssh-port) SSH_PORT="${2:?--ssh-port requires a port}"; shift 2 ;;
    --ssh-port=*) SSH_PORT="${1#--ssh-port=}"; shift ;;
    --local-port) LOCAL_PORT="${2:?--local-port requires a port}"; shift 2 ;;
    --local-port=*) LOCAL_PORT="${1#--local-port=}"; shift ;;
    --remote-port) REMOTE_PORT="${2:?--remote-port requires a port}"; shift 2 ;;
    --remote-port=*) REMOTE_PORT="${1#--remote-port=}"; shift ;;
    --label) LABEL="${2:?--label requires a value}"; shift 2 ;;
    --label=*) LABEL="${1#--label=}"; shift ;;
    --openshell-bin) OPENSH_BIN="${2:?--openshell-bin requires a path}"; shift 2 ;;
    --openshell-bin=*) OPENSH_BIN="${1#--openshell-bin=}"; shift ;;
    --remove) REMOVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
  echo "ERROR: invalid launchd label" >&2
  exit 2
}
for port_name in SSH_PORT LOCAL_PORT REMOTE_PORT; do
  port_value="${!port_name}"
  [[ "$port_value" =~ ^[1-9][0-9]{0,4}$ ]] \
    && (( 10#$port_value <= 65535 )) || {
      port_label="$(printf '%s' "$port_name" | tr '[:upper:]' '[:lower:]')"
      echo "ERROR: invalid ${port_label}" >&2
      exit 2
    }
done
[ "$(uname -s)" = "Darwin" ] || {
  echo "ERROR: the certifier gateway tunnel installer requires Darwin" >&2
  exit 2
}

domain="gui/$(id -u)"
launch_agents="$HOME/Library/LaunchAgents"
plist="$launch_agents/$LABEL.plist"
SCRIPT_DIR="$(CDPATH= cd -P -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAC_LAUNCHD_LOG_PREFIX="[certifier-tunnel]"
# shellcheck source=../lib/launchd-lifecycle.sh
[ -r "$SCRIPT_DIR/../lib/launchd-lifecycle.sh" ] || {
  echo "ERROR: shared launchd lifecycle library is missing" >&2
  exit 1
}
. "$SCRIPT_DIR/../lib/launchd-lifecycle.sh"

if [ "$REMOVE" = "1" ]; then
  mac_launchd_stop_job_if_present "$domain/$LABEL" "$LABEL"
  rm -f "$plist"
  echo "removed $LABEL"
  exit 0
fi

[[ "$TARGET" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9][A-Za-z0-9._:-]*$ ]] || {
  echo "ERROR: --target must be a non-interactive user@host destination" >&2
  exit 2
}

if [ -z "$OPENSH_BIN" ]; then
  for candidate in \
    "$HOME/.mac/bin/openshell" \
    /opt/homebrew/bin/openshell \
    /usr/local/bin/openshell; do
    if [ -x "$candidate" ]; then
      OPENSH_BIN="$candidate"
      break
    fi
  done
fi
[ -n "$OPENSH_BIN" ] && [ "${OPENSH_BIN#/}" != "$OPENSH_BIN" ] \
  && [ -x "$OPENSH_BIN" ] || {
    echo "ERROR: --openshell-bin must name an absolute executable" >&2
    exit 2
  }

python_bin="$HOME/.mac/venv/bin/python"
if [ ! -x "$python_bin" ]; then
  python_bin="$(command -v python3 || true)"
fi
[ -n "$python_bin" ] || {
  echo "ERROR: Python 3 is required to write the launchd property list" >&2
  exit 2
}

mkdir -p "$launch_agents" "$HOME/.mac/logs"
chmod 700 "$launch_agents" "$HOME/.mac/logs" 2>/dev/null || true
mac_launchd_transaction_begin "$domain" "$plist" "$domain/$LABEL" "$LABEL"
tmp_plist="$(mktemp "$launch_agents/.${LABEL}.XXXXXX")"
mac_launchd_transaction_track_temporary "$tmp_plist"

"$python_bin" - \
  "$tmp_plist" "$LABEL" "$TARGET" "$SSH_PORT" "$LOCAL_PORT" "$REMOTE_PORT" \
  "$HOME/.mac/logs/certifier-openshell-tunnel.out.log" \
  "$HOME/.mac/logs/certifier-openshell-tunnel.err.log" <<'PY'
import plistlib
import sys

(
    path,
    label,
    target,
    ssh_port,
    local_port,
    remote_port,
    stdout_path,
    stderr_path,
) = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [
        "/usr/bin/ssh",
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ConnectTimeout=10",
        "-p",
        ssh_port,
        "-L",
        f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        "--",
        target,
    ],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 5,
    "ProcessType": "Background",
    "StandardOutPath": stdout_path,
    "StandardErrorPath": stderr_path,
}
with open(path, "wb") as stream:
    plistlib.dump(payload, stream, sort_keys=True)
PY

chmod 600 "$tmp_plist"
mac_launchd_transaction_mark_mutating
mac_launchd_stop_job_if_present "$domain/$LABEL" "$LABEL"
mac_launchd_transaction_replace "$tmp_plist" "$plist"
mac_launchd_bootstrap_job "$domain" "$plist" "$domain/$LABEL" "$LABEL"

endpoint="http://127.0.0.1:$LOCAL_PORT"
if OPENSHELL_GATEWAY_ENDPOINT="$endpoint" mac_retry_bounded \
  "${MAC_CERTIFIER_TUNNEL_HEALTH_TIMEOUT_SECONDS:-20}" \
  "${MAC_CERTIFIER_STATUS_COMMAND_TIMEOUT_SECONDS:-5}" 1 \
  "$OPENSH_BIN" status >/dev/null 2>&1; then
  launchd_state="$(mac_launchd_job_state "$domain/$LABEL" "$LABEL")"
  if [ "$launchd_state" != active ]; then
    echo "ERROR: certifier OpenShell endpoint is healthy but launchd job is absent: $LABEL" >&2
    exit 1
  fi
  mac_launchd_transaction_commit
  echo "certifier OpenShell tunnel healthy: $endpoint -> $TARGET:$REMOTE_PORT"
  exit 0
fi

echo "ERROR: certifier OpenShell tunnel did not become healthy at $endpoint" >&2
mac_run_bounded 5 launchctl print "$domain/$LABEL" >&2 || true
tail -40 "$HOME/.mac/logs/certifier-openshell-tunnel.err.log" >&2 2>/dev/null || true
exit 1
