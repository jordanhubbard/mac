#!/usr/bin/env bash
# install-webdav-server.sh - install/start the hub public artifact WebDAV server.
#
# Public traffic gets static HTTP GET/HEAD reads only. Agents publish by
# writing files into MAC_PUBLISH_DIR on the hub, then recording publish CRUD
# through MAC/AgentBus.
set -euo pipefail

MAC_HOME="${MAC_HOME:-$HOME/.mac}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
FLEET_NAME="${FLEET_NAME:-mac}"
SERVICE_NAME="${FLEET_NAME}-webdav.service"
SUPERVISOR_KIND="${WEBDAV_SUPERVISOR:-${MAC_SUPERVISOR_KIND:-auto}}"
LOG_DIR="${LOG_DIR:-$MAC_HOME/logs}"

# Platform handling. macOS has no /etc service-config tree and no passwordless
# sudo over a non-interactive deploy, so service env files live under $MAC_HOME.
OS_NAME="$(uname -s)"
if [ "$OS_NAME" = "Darwin" ]; then
  ENV_CONF_DIR="$MAC_HOME/service-env"
else
  ENV_CONF_DIR="/etc/${FLEET_NAME}"
fi
ENV_DEST="$ENV_CONF_DIR/webdav.env"

maybe_sudo() {
  if [ "$OS_NAME" = "Darwin" ]; then
    "$@"
  else
    sudo "$@"
  fi
}

WEBDAV_BIND_ADDR="${WEBDAV_BIND_ADDR:-0.0.0.0}"
WEBDAV_PORT="${WEBDAV_PORT:-80}"
WEBDAV_ROOT="${WEBDAV_ROOT:-$MAC_HOME/public-artifacts}"
WEBDAV_PUBLIC_PATH="${WEBDAV_PUBLIC_PATH:-/artifacts/}"
WEBDAV_PUBLIC_URL="${WEBDAV_PUBLIC_URL:-}"
WEBDAV_MAX_UPLOAD_BYTES="${WEBDAV_MAX_UPLOAD_BYTES:-536870912}"

normalize_public_path() {
  local path="$1"
  case "$path" in
    /*) ;;
    *) path="/$path" ;;
  esac
  case "$path" in
    */) ;;
    *) path="$path/" ;;
  esac
  printf '%s\n' "$path"
}

detect_supervisor() {
  case "$SUPERVISOR_KIND" in
    systemd|launchd|supervisord)
      printf '%s\n' "$SUPERVISOR_KIND"
      return
      ;;
    auto|"")
      ;;
    *)
      echo "[webdav] ERROR: unsupported supervisor: $SUPERVISOR_KIND" >&2
      exit 1
      ;;
  esac
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    printf '%s\n' "systemd"
    return
  fi
  if command -v launchctl >/dev/null 2>&1; then
    printf '%s\n' "launchd"
    return
  fi
  if command -v supervisorctl >/dev/null 2>&1; then
    printf '%s\n' "supervisord"
    return
  fi
  echo "[webdav] ERROR: could not detect systemd, launchd, or supervisord" >&2
  exit 1
}

run_supervisorctl() {
  if command -v sudo >/dev/null 2>&1; then
    sudo supervisorctl "$@" || supervisorctl "$@"
  else
    supervisorctl "$@"
  fi
}

supervisord_conf_dir() {
  if [ -n "${MAC_DEPLOY_SUPERVISOR_CONF_DIR:-}" ]; then
    printf '%s\n' "$MAC_DEPLOY_SUPERVISOR_CONF_DIR"
  elif [ -d /etc/supervisor/conf.d ]; then
    printf '%s\n' "/etc/supervisor/conf.d"
  elif [ -d /etc/supervisord.d ]; then
    printf '%s\n' "/etc/supervisord.d"
  else
    printf '%s\n' "/etc/supervisor/conf.d"
  fi
}

set_env_key() {
  local file="$1" key="$2" value="$3"
  mkdir -p "$(dirname "$file")"
  if [ ! -f "$file" ]; then
    : > "$file"
    chmod 600 "$file"
  fi
  if grep -q "^${key}=" "$file"; then
    local tmp
    tmp="$(mktemp)"
    grep -v "^${key}=" "$file" > "$tmp"
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

if [ -z "$WORKSPACE" ] || [ ! -f "$WORKSPACE/src/mac/webdav_server.py" ]; then
  echo "[webdav] ERROR: cannot locate mac.webdav_server under $WORKSPACE" >&2
  exit 1
fi

if [ ! -x "$MAC_HOME/venv/bin/python" ]; then
  echo "[webdav] ERROR: $MAC_HOME/venv/bin/python is missing; install the mac package first" >&2
  exit 1
fi

WEBDAV_PUBLIC_PATH="$(normalize_public_path "$WEBDAV_PUBLIC_PATH")"
if [ -z "$WEBDAV_PUBLIC_URL" ]; then
  if [ "$WEBDAV_PORT" = "80" ]; then
    WEBDAV_PUBLIC_URL="http://${WEBDAV_BIND_ADDR}${WEBDAV_PUBLIC_PATH}"
  else
    WEBDAV_PUBLIC_URL="http://${WEBDAV_BIND_ADDR}:${WEBDAV_PORT}${WEBDAV_PUBLIC_PATH}"
  fi
fi

if [ "${1:-}" = "--print-public-url" ]; then
  printf '%s\n' "$WEBDAV_PUBLIC_URL"
  exit 0
fi

SUPERVISOR_KIND="$(detect_supervisor)"

echo "[webdav] Installing public artifact server under ${SUPERVISOR_KIND}"
echo "[webdav] Binding server to ${WEBDAV_BIND_ADDR}:${WEBDAV_PORT}"
echo "[webdav] Public read URL ${WEBDAV_PUBLIC_URL}"
maybe_sudo install -d -m 0755 "$ENV_CONF_DIR"
mkdir -p "$MAC_HOME/bin" "$LOG_DIR" "$WEBDAV_ROOT"
chmod 0755 "$WEBDAV_ROOT"

tmp_env="$(mktemp)"
cat > "$tmp_env" <<EOF
MAC_WEBDAV_BIND_ADDR=${WEBDAV_BIND_ADDR}
MAC_WEBDAV_PORT=${WEBDAV_PORT}
MAC_WEBDAV_ROOT=${WEBDAV_ROOT}
MAC_WEBDAV_PUBLIC_PATH=${WEBDAV_PUBLIC_PATH}
MAC_WEBDAV_PUBLIC_URL=${WEBDAV_PUBLIC_URL}
MAC_PUBLISH_DIR=${WEBDAV_ROOT}
MAC_PUBLISH_PUBLIC_URL=${WEBDAV_PUBLIC_URL}
MAC_PUBLISH_METHOD=hub_directory_http
MAC_PUBLISH_WEBDAV_ENABLED=1
MAC_PUBLISH_WEBDAV_URL=${WEBDAV_PUBLIC_URL}
MAC_WEBDAV_MAX_UPLOAD_BYTES=${WEBDAV_MAX_UPLOAD_BYTES}
EOF
maybe_sudo install -m 0644 "$tmp_env" "$ENV_DEST"
rm -f "$tmp_env"

set_env_key "${MAC_HOME}/mac.env" MAC_PUBLISH_WEBDAV_ENABLED "1"
set_env_key "${MAC_HOME}/mac.env" MAC_PUBLISH_WEBDAV_URL "$WEBDAV_PUBLIC_URL"
set_env_key "${MAC_HOME}/mac.env" MAC_PUBLISH_DIR "$WEBDAV_ROOT"
set_env_key "${MAC_HOME}/mac.env" MAC_PUBLISH_PUBLIC_URL "$WEBDAV_PUBLIC_URL"
set_env_key "${MAC_HOME}/mac.env" MAC_PUBLISH_METHOD "hub_directory_http"
set_env_key "${MAC_HOME}/mac.env" MAC_WEBDAV_PUBLIC_URL "$WEBDAV_PUBLIC_URL"
set_env_key "${MAC_HOME}/mac.env" MAC_WEBDAV_PUBLIC_PATH "$WEBDAV_PUBLIC_PATH"
set_env_key "${MAC_HOME}/mac.env" MAC_WEBDAV_ROOT "$WEBDAV_ROOT"
set_env_key "${MAC_HOME}/mac.env" MAC_WEBDAV_MAX_UPLOAD_BYTES "$WEBDAV_MAX_UPLOAD_BYTES"

write_webdav_wrapper() {
  local wrapper="$MAC_HOME/bin/mac-webdav-server-run"
  cat > "$wrapper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a
[ -f ${ENV_DEST} ] && . ${ENV_DEST}
[ -f "\$HOME/.mac/mac.env" ] && . "\$HOME/.mac/mac.env"
set +a
export PYTHONPATH="${WORKSPACE}/src:\${PYTHONPATH:-}"
exec "${MAC_HOME}/venv/bin/python" -m mac.webdav_server \
  --host "\${MAC_WEBDAV_BIND_ADDR:-0.0.0.0}" \
  --port "\${MAC_WEBDAV_PORT:-80}" \
  --root "\${MAC_WEBDAV_ROOT:-\$HOME/.mac/public-artifacts}" \
  --public-prefix "\${MAC_WEBDAV_PUBLIC_PATH:-/artifacts/}"
EOF
  chmod 700 "$wrapper"
}

write_webdav_wrapper

case "$SUPERVISOR_KIND" in
  systemd)
    echo "[webdav] Installing systemd unit"
    sudo tee "/etc/systemd/system/${SERVICE_NAME}" >/dev/null <<EOF
[Unit]
Description=mac public artifact WebDAV server
After=network-online.target
Wants=network-online.target
Before=${FLEET_NAME}.service ${FLEET_NAME}-hermes-gateway.service ${FLEET_NAME}-agent.service

[Service]
Type=simple
User=${USER}
WorkingDirectory=${MAC_HOME}
EnvironmentFile=-${ENV_DEST}
EnvironmentFile=-${MAC_HOME}/mac.env
ExecStart=${MAC_HOME}/bin/mac-webdav-server-run
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}" >/dev/null
    echo "[webdav] Starting ${SERVICE_NAME}"
    sudo systemctl restart "${SERVICE_NAME}"
    ;;
  supervisord)
    echo "[webdav] Installing supervisord program"
    conf_dir="$(supervisord_conf_dir)"
    sudo install -d -m 0755 "$conf_dir"
    sudo tee "$conf_dir/${FLEET_NAME}-webdav.conf" >/dev/null <<EOF
[program:${FLEET_NAME}-webdav]
command=${MAC_HOME}/bin/mac-webdav-server-run
directory=${MAC_HOME}
user=${USER}
autostart=true
autorestart=true
startsecs=3
stopwaitsecs=20
stdout_logfile=${LOG_DIR}/mac-webdav.log
stderr_logfile=${LOG_DIR}/mac-webdav.log
environment=HOME="${HOME}"
EOF
    run_supervisorctl reread >/dev/null
    run_supervisorctl update >/dev/null
    run_supervisorctl restart "${FLEET_NAME}-webdav" >/dev/null 2>&1 || run_supervisorctl start "${FLEET_NAME}-webdav" >/dev/null
    ;;
  launchd)
    echo "[webdav] Installing launchd agent"
    plist="$HOME/Library/LaunchAgents/com.${FLEET_NAME}.webdav.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.${FLEET_NAME}.webdav</string>
  <key>ProgramArguments</key>
  <array><string>${MAC_HOME}/bin/mac-webdav-server-run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>${MAC_HOME}</string>
  <key>StandardOutPath</key><string>${LOG_DIR}/mac-webdav.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/mac-webdav.log</string>
</dict>
</plist>
EOF
    if command -v plutil >/dev/null 2>&1; then
      plutil -lint "$plist"
    fi
    uid="$(id -u)"
    launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
    launchctl bootout "gui/$uid/com.${FLEET_NAME}.webdav" >/dev/null 2>&1 || true
    launchctl enable "gui/$uid/com.${FLEET_NAME}.webdav"
    if ! launchctl bootstrap "gui/$uid" "$plist"; then
      launchctl kickstart -k "gui/$uid/com.${FLEET_NAME}.webdav"
    fi
    ;;
esac

health_url="http://${WEBDAV_BIND_ADDR}:${WEBDAV_PORT}/health"
if [ "$WEBDAV_BIND_ADDR" = "0.0.0.0" ]; then
  health_url="http://127.0.0.1:${WEBDAV_PORT}/health"
fi
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if curl -fsS --connect-timeout 2 --max-time 5 "$health_url" >/dev/null 2>&1; then
    echo "[webdav] Server ready at $health_url"
    exit 0
  fi
  sleep 2
done

echo "[webdav] ERROR: server did not become ready at $health_url" >&2
exit 1
