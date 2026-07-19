#!/usr/bin/env bash
# install-firecrawl-gateway.sh - install/start the hub web-search gateway.
#
# This is a lightweight Firecrawl v2-compatible service. Hermes agents point
# FIRECRAWL_API_URL at the hub instead of each host carrying its own search
# stack.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -P -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
FLEET_NAME="${FLEET_NAME:-mac}"
SERVICE_NAME="${FLEET_NAME}-firecrawl-gateway.service"
SUPERVISOR_KIND="${FIRECRAWL_SUPERVISOR:-${MAC_SUPERVISOR_KIND:-auto}}"
LOG_DIR="${LOG_DIR:-$MAC_HOME/logs}"

# Platform handling. macOS has no /etc service-config tree and no passwordless
# sudo over a non-interactive deploy, so service env files live under $MAC_HOME.
OS_NAME="$(uname -s)"
if [ "$OS_NAME" = "Darwin" ]; then
  ENV_CONF_DIR="$MAC_HOME/service-env"
else
  ENV_CONF_DIR="/etc/${FLEET_NAME}"
fi
ENV_DEST="$ENV_CONF_DIR/firecrawl-gateway.env"

maybe_sudo() {
  if [ "$OS_NAME" = "Darwin" ]; then
    "$@"
  else
    sudo "$@"
  fi
}

detect_tailscale_ip() {
  if command -v tailscale >/dev/null 2>&1; then
    tailscale ip -4 2>/dev/null | head -1
  fi
}

default_firecrawl_bind_addr() {
  local ts_ip
  ts_ip="$(detect_tailscale_ip || true)"
  if [ -n "$ts_ip" ]; then
    printf '%s\n' "$ts_ip"
  else
    printf '%s\n' "127.0.0.1"
  fi
}

FIRECRAWL_BIND_ADDR="${FIRECRAWL_BIND_ADDR:-$(default_firecrawl_bind_addr)}"
FIRECRAWL_PORT="${FIRECRAWL_PORT:-3002}"

detect_supervisor() {
  case "$SUPERVISOR_KIND" in
    systemd|launchd|supervisord)
      printf '%s\n' "$SUPERVISOR_KIND"
      return
      ;;
    auto|"")
      ;;
    *)
      echo "[firecrawl-gateway] ERROR: unsupported supervisor: $SUPERVISOR_KIND" >&2
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
  echo "[firecrawl-gateway] ERROR: could not detect systemd, launchd, or supervisord" >&2
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

if [ -z "$WORKSPACE" ] || [ ! -f "$WORKSPACE/src/mac/firecrawl_gateway.py" ]; then
  echo "[firecrawl-gateway] ERROR: cannot locate mac.firecrawl_gateway under $WORKSPACE" >&2
  exit 1
fi

if [ ! -x "$MAC_HOME/venv/bin/python" ]; then
  echo "[firecrawl-gateway] ERROR: $MAC_HOME/venv/bin/python is missing; install the mac package first" >&2
  exit 1
fi

case "$FIRECRAWL_BIND_ADDR" in
  0.0.0.0|::|\[::\])
    echo "[firecrawl-gateway] ERROR: refusing unsafe all-interface bind address: $FIRECRAWL_BIND_ADDR" >&2
    exit 1
    ;;
esac

if [ "${1:-}" = "--print-bind-addr" ]; then
  printf '%s\n' "$FIRECRAWL_BIND_ADDR"
  exit 0
fi

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

service_url="http://${FIRECRAWL_BIND_ADDR}:${FIRECRAWL_PORT}"
SUPERVISOR_KIND="$(detect_supervisor)"

echo "[firecrawl-gateway] Installing gateway under ${SUPERVISOR_KIND}"
echo "[firecrawl-gateway] Binding gateway to ${FIRECRAWL_BIND_ADDR}:${FIRECRAWL_PORT}"
maybe_sudo install -d -m 0755 "$ENV_CONF_DIR"
mkdir -p "$MAC_HOME/bin" "$LOG_DIR"

tmp_env="$(mktemp)"
cat > "$tmp_env" <<EOF
FIRECRAWL_BIND_ADDR=${FIRECRAWL_BIND_ADDR}
FIRECRAWL_PORT=${FIRECRAWL_PORT}
FIRECRAWL_API_URL=${service_url}
FIRECRAWL_GATEWAY_URL=${service_url}
FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY:-none}
EOF
maybe_sudo install -m 0644 "$tmp_env" "$ENV_DEST"
rm -f "$tmp_env"

set_env_key "${MAC_HOME}/mac.env" FIRECRAWL_API_URL "$service_url"
set_env_key "${MAC_HOME}/mac.env" FIRECRAWL_GATEWAY_URL "$service_url"
set_env_key "${MAC_HOME}/mac.env" FIRECRAWL_API_KEY "${FIRECRAWL_API_KEY:-none}"
set_env_key "${MAC_HOME}/mac.env" MAC_WEB_SEARCH_PROVIDER "firecrawl"
set_env_key "${MAC_HOME}/mac.env" MAC_WEB_SEARCH_URL "$service_url"

write_gateway_wrapper() {
  local wrapper="${1:-$MAC_HOME/bin/mac-firecrawl-gateway-run}"
  cat > "$wrapper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a
[ -f ${ENV_DEST} ] && . ${ENV_DEST}
[ -f "\$HOME/.mac/mac.env" ] && . "\$HOME/.mac/mac.env"
set +a
export PYTHONPATH="${WORKSPACE}/src:\${PYTHONPATH:-}"
exec "${MAC_HOME}/venv/bin/python" -m mac.firecrawl_gateway --host "\${FIRECRAWL_BIND_ADDR:-127.0.0.1}" --port "\${FIRECRAWL_PORT:-3002}"
EOF
  chmod 700 "$wrapper"
}

case "$SUPERVISOR_KIND" in
  systemd)
    echo "[firecrawl-gateway] Installing systemd unit"
    write_gateway_wrapper
    sudo tee "/etc/systemd/system/${SERVICE_NAME}" >/dev/null <<EOF
[Unit]
Description=mac Firecrawl-compatible web search gateway
After=network-online.target
Wants=network-online.target
Before=${FLEET_NAME}.service ${FLEET_NAME}-hermes-gateway.service ${FLEET_NAME}-agent.service

[Service]
Type=simple
User=${USER}
WorkingDirectory=${MAC_HOME}
EnvironmentFile=-${ENV_DEST}
EnvironmentFile=-${MAC_HOME}/mac.env
ExecStart=${MAC_HOME}/bin/mac-firecrawl-gateway-run
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}" >/dev/null
    echo "[firecrawl-gateway] Starting ${SERVICE_NAME}"
    sudo systemctl restart "${SERVICE_NAME}"
    ;;
  supervisord)
    echo "[firecrawl-gateway] Installing supervisord program"
    write_gateway_wrapper
    conf_dir="$(supervisord_conf_dir)"
    sudo install -d -m 0755 "$conf_dir"
    sudo tee "$conf_dir/${FLEET_NAME}-firecrawl-gateway.conf" >/dev/null <<EOF
[program:${FLEET_NAME}-firecrawl-gateway]
command=${MAC_HOME}/bin/mac-firecrawl-gateway-run
directory=${MAC_HOME}
user=${USER}
autostart=true
autorestart=true
startsecs=3
stopwaitsecs=20
stdout_logfile=${LOG_DIR}/mac-firecrawl-gateway.log
stderr_logfile=${LOG_DIR}/mac-firecrawl-gateway.log
environment=HOME="${HOME}"
EOF
    run_supervisorctl reread >/dev/null
    run_supervisorctl update >/dev/null
    run_supervisorctl restart "${FLEET_NAME}-firecrawl-gateway" >/dev/null 2>&1 || run_supervisorctl start "${FLEET_NAME}-firecrawl-gateway" >/dev/null
    ;;
  launchd)
    echo "[firecrawl-gateway] Installing launchd agent"
    launchd_lib="$SCRIPT_DIR/lib/launchd-lifecycle.sh"
    if [ ! -r "$launchd_lib" ]; then
      echo "[firecrawl-gateway] ERROR: launchd lifecycle helper is missing: $launchd_lib" >&2
      exit 1
    fi
    MAC_LAUNCHD_LOG_PREFIX="[firecrawl-gateway]"
    # shellcheck source=lib/launchd-lifecycle.sh
    . "$launchd_lib"
    uid="$(id -u)"
    launchd_domain="gui/$uid"
    launchd_label="com.${FLEET_NAME}.firecrawl-gateway"
    launchd_target="$launchd_domain/$launchd_label"
    plist="$HOME/Library/LaunchAgents/${launchd_label}.plist"
    wrapper="$MAC_HOME/bin/mac-firecrawl-gateway-run"
    mkdir -p "$HOME/Library/LaunchAgents"
    mac_launchd_transaction_begin \
      "$launchd_domain" "$plist" "$launchd_target" "$launchd_label"
    mac_launchd_transaction_track_file "$wrapper"
    tmp_wrapper="$(mktemp "$MAC_HOME/bin/.mac-firecrawl-gateway-run.XXXXXX")"
    mac_launchd_transaction_track_temporary "$tmp_wrapper"
    write_gateway_wrapper "$tmp_wrapper"
    /bin/bash -n "$tmp_wrapper"
    tmp_plist="$(mktemp "$HOME/Library/LaunchAgents/.${launchd_label}.XXXXXX")"
    mac_launchd_transaction_track_temporary "$tmp_plist"
    cat > "$tmp_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.${FLEET_NAME}.firecrawl-gateway</string>
  <key>ProgramArguments</key>
  <array><string>${MAC_HOME}/bin/mac-firecrawl-gateway-run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>${MAC_HOME}</string>
  <key>StandardOutPath</key><string>${LOG_DIR}/mac-firecrawl-gateway.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/mac-firecrawl-gateway.log</string>
</dict>
</plist>
EOF
    if command -v plutil >/dev/null 2>&1; then
      plutil -lint "$tmp_plist"
    fi
    mac_launchd_transaction_mark_mutating
    mac_launchd_stop_job_if_present "$launchd_target" "$launchd_label"
    mac_launchd_transaction_replace "$tmp_wrapper" "$wrapper"
    mac_launchd_transaction_replace "$tmp_plist" "$plist"
    mac_launchd_bootstrap_job \
      "$launchd_domain" "$plist" "$launchd_target" "$launchd_label"
    ;;
esac

health_url="${service_url}/health"
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if curl -fsS --connect-timeout 2 --max-time 5 "$health_url" >/dev/null 2>&1; then
    echo "[firecrawl-gateway] Gateway ready at $health_url"
    if [ "$SUPERVISOR_KIND" = launchd ]; then
      mac_launchd_transaction_commit
    fi
    exit 0
  fi
  sleep 2
done

echo "[firecrawl-gateway] ERROR: gateway did not become ready at $health_url" >&2
case "$SUPERVISOR_KIND" in
  systemd) systemctl status "${SERVICE_NAME}" --no-pager -n 40 >&2 || true ;;
  supervisord) supervisorctl status "${FLEET_NAME}-firecrawl-gateway" >&2 || true ;;
  launchd)
    mac_run_bounded 5 launchctl print "$launchd_target" >&2 || true
    ;;
esac
exit 1
