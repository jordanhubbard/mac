#!/usr/bin/env bash
# install-headscale.sh — install headscale control plane on the hub.
#
# Headscale is the self-hosted Tailscale control plane. This script is used
# only when a fleet explicitly selects network.provider=headscale.
# After this script runs, mac.env contains:
#   HEADSCALE_URL=http://localhost:<port>      (used by hub's own tailscale up)
#   HEADSCALE_FLEET_URL=<login-server-url>     (used by worker tailscale up)
#   HEADSCALE_PREAUTHKEY=<reusable-key>        (used by all agents)
set -euo pipefail

AGENT_NAME="${AGENT:-$(hostname)}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
LOG_DIR="${LOG_DIR:-$MAC_HOME/logs}"
ENV_FILE="${ENV_FILE:-$MAC_HOME/mac.env}"
SUPERVISOR_KIND="${HEADSCALE_SUPERVISOR:-${MAC_SUPERVISOR_KIND:-auto}}"
FLEET_NAME="${FLEET_NAME:-mac}"

HEADSCALE_VERSION="${HEADSCALE_VERSION:-0.25.1}"
HEADSCALE_PORT="${HEADSCALE_PORT:-8080}"
# The address workers use to reach headscale (hub's publicly routable addr)
HEADSCALE_PUBLIC_ADDR="${HEADSCALE_PUBLIC_ADDR:-}"
HEADSCALE_FLEET_URL="${HEADSCALE_FLEET_URL:-}"
HEADSCALE_USER="${HEADSCALE_USER:-${FLEET_NAME}-fleet}"
HEADSCALE_IP_PREFIX="${HEADSCALE_IP_PREFIX:-100.64.0.0/10}"
HEADSCALE_DNS="${HEADSCALE_DNS:-magicdns}"
HEADSCALE_DATA_DIR="${HEADSCALE_DATA_DIR:-/var/lib/${FLEET_NAME}/headscale}"
HEADSCALE_CONFIG_DIR="${HEADSCALE_CONFIG_DIR:-/etc/${FLEET_NAME}/headscale}"
HEADSCALE_BIN="${HEADSCALE_BIN:-/usr/local/bin/headscale}"

set_env_key() {
  local file="$1" key="$2" value="$3"
  mkdir -p "$(dirname "$file")"
  if [ ! -f "$file" ]; then
    : > "$file"
    chmod 600 "$file"
  fi
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

detect_supervisor() {
  case "$SUPERVISOR_KIND" in
    systemd|launchd|supervisord) printf '%s\n' "$SUPERVISOR_KIND"; return ;;
    auto|"") ;;
    *) echo "[headscale] ERROR: unsupported supervisor: $SUPERVISOR_KIND" >&2; exit 1 ;;
  esac
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    printf '%s\n' "systemd"; return
  fi
  if command -v launchctl >/dev/null 2>&1; then
    printf '%s\n' "launchd"; return
  fi
  if command -v supervisorctl >/dev/null 2>&1; then
    printf '%s\n' "supervisord"; return
  fi
  echo "[headscale] ERROR: could not detect systemd, launchd, or supervisord" >&2
  exit 1
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

run_supervisorctl() {
  if command -v sudo >/dev/null 2>&1; then
    sudo supervisorctl "$@" || supervisorctl "$@"
  else
    supervisorctl "$@"
  fi
}

headscale_running() {
  command -v headscale >/dev/null 2>&1 || return 1
  headscale version >/dev/null 2>&1 || return 1
  # Check if headscale API is reachable
  curl -fsS --connect-timeout 3 --max-time 5 "http://127.0.0.1:${HEADSCALE_PORT}/health" >/dev/null 2>&1
}

# -- Install headscale binary --
if ! command -v "$HEADSCALE_BIN" >/dev/null 2>&1 || \
   ! "$HEADSCALE_BIN" version 2>/dev/null | grep -q "$HEADSCALE_VERSION"; then
  echo "[headscale] Installing headscale ${HEADSCALE_VERSION}"
  arch="$(uname -m)"
  case "$arch" in
    x86_64) hs_arch="amd64" ;;
    aarch64|arm64) hs_arch="arm64" ;;
    *) echo "[headscale] ERROR: unsupported architecture: $arch" >&2; exit 1 ;;
  esac
  os_lower="$(uname -s | tr '[:upper:]' '[:lower:]')"
  hs_url="https://github.com/juanfont/headscale/releases/download/v${HEADSCALE_VERSION}/headscale_${HEADSCALE_VERSION}_${os_lower}_${hs_arch}"
  tmp_bin="$(mktemp)"
  curl -fsSL "$hs_url" -o "$tmp_bin"
  chmod 755 "$tmp_bin"
  sudo mv "$tmp_bin" "$HEADSCALE_BIN"
  echo "[headscale] Installed headscale ${HEADSCALE_VERSION} at ${HEADSCALE_BIN}"
fi

# -- Configure headscale --
sudo install -d -m 0755 "$HEADSCALE_CONFIG_DIR"
sudo install -d -m 0750 "$HEADSCALE_DATA_DIR"
sudo chown "$USER" "$HEADSCALE_DATA_DIR" 2>/dev/null || true

# Derive login server from explicit HEADSCALE_FLEET_URL first. If only a
# public address is supplied, build the URL from HEADSCALE_PORT.
if [ -z "$HEADSCALE_FLEET_URL" ] && [ -z "$HEADSCALE_PUBLIC_ADDR" ]; then
  # Try to get the public IP via a metadata service or hostname
  HEADSCALE_PUBLIC_ADDR="$(curl -fsS --connect-timeout 3 http://checkip.amazonaws.com 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")"
fi

if [ -z "$HEADSCALE_FLEET_URL" ]; then
  HEADSCALE_FLEET_URL="http://${HEADSCALE_PUBLIC_ADDR}:${HEADSCALE_PORT}"
fi
HEADSCALE_LOCAL_URL="http://127.0.0.1:${HEADSCALE_PORT}"
HEADSCALE_MAGIC_DNS="true"
case "$HEADSCALE_DNS" in
  none|disabled|off|false|FALSE|0) HEADSCALE_MAGIC_DNS="false" ;;
esac

echo "[headscale] Configuring server_url=${HEADSCALE_FLEET_URL}"

sudo tee "$HEADSCALE_CONFIG_DIR/config.yaml" >/dev/null <<EOF
---
server_url: ${HEADSCALE_FLEET_URL}
listen_addr: 0.0.0.0:${HEADSCALE_PORT}
grpc_listen_addr: 127.0.0.1:50443
grpc_allow_insecure: true
metrics_listen_addr: 127.0.0.1:9090

private_key_path: ${HEADSCALE_CONFIG_DIR}/private.key
noise:
  private_key_path: ${HEADSCALE_CONFIG_DIR}/noise_private.key

ip_prefixes:
  - ${HEADSCALE_IP_PREFIX}

derp:
  server:
    enabled: false
  urls:
    - https://controlplane.tailscale.com/derpmap/default
  auto_update_enabled: true
  update_frequency: 24h

disable_check_updates: true
ephemeral_node_inactivity_timeout: 30m
node_update_check_interval: 10s

database:
  type: sqlite
  sqlite:
    path: ${HEADSCALE_DATA_DIR}/db.sqlite

log:
  level: warn

dns:
  magic_dns: ${HEADSCALE_MAGIC_DNS}
  base_domain: mac.internal
EOF

# -- Start headscale under supervisor --
SUPERVISOR_KIND="$(detect_supervisor)"
echo "[headscale] Starting headscale under ${SUPERVISOR_KIND}"
mkdir -p "$LOG_DIR"

case "$SUPERVISOR_KIND" in
  systemd)
    # Create a simple systemd unit
    sudo tee /etc/systemd/system/headscale.service >/dev/null <<EOF
[Unit]
Description=headscale Tailscale control plane
After=network.target

[Service]
User=root
ExecStart=${HEADSCALE_BIN} serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable headscale >/dev/null
    sudo systemctl restart headscale
    ;;
  supervisord)
    conf_dir="$(supervisord_conf_dir)"
    sudo install -d -m 0755 "$conf_dir"
    sudo tee "$conf_dir/${FLEET_NAME}-headscale.conf" >/dev/null <<EOF
[program:${FLEET_NAME}-headscale]
command=${HEADSCALE_BIN} serve
directory=${HEADSCALE_DATA_DIR}
user=root
autostart=true
autorestart=true
startsecs=3
stopwaitsecs=15
stdout_logfile=$LOG_DIR/headscale.log
stderr_logfile=$LOG_DIR/headscale.log
EOF
    run_supervisorctl reread >/dev/null
    run_supervisorctl update >/dev/null
    run_supervisorctl restart "${FLEET_NAME}-headscale" >/dev/null 2>&1 \
      || run_supervisorctl start "${FLEET_NAME}-headscale" >/dev/null
    ;;
  launchd)
    plist="$HOME/Library/LaunchAgents/com.${FLEET_NAME}.headscale.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.${FLEET_NAME}.headscale</string>
  <key>ProgramArguments</key>
  <array><string>${HEADSCALE_BIN}</string><string>serve</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/headscale.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/headscale.log</string>
</dict>
</plist>
EOF
    uid="$(id -u)"
    launchctl bootout "gui/$uid/com.${FLEET_NAME}.headscale" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$uid" "$plist"
    ;;
esac

# -- Wait for headscale to become ready --
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS --connect-timeout 2 --max-time 5 "${HEADSCALE_LOCAL_URL}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! curl -fsS --connect-timeout 2 --max-time 5 "${HEADSCALE_LOCAL_URL}/health" >/dev/null 2>&1; then
  echo "[headscale] ERROR: headscale did not become ready at ${HEADSCALE_LOCAL_URL}/health" >&2
  case "$SUPERVISOR_KIND" in
    systemd) sudo journalctl -u headscale -n 40 --no-pager >&2 || true ;;
    supervisord) supervisorctl tail -f headscale >&2 2>/dev/null || true ;;
  esac
  exit 1
fi

echo "[headscale] headscale ready at ${HEADSCALE_LOCAL_URL}"

# -- Create user and pre-auth key --
if ! "$HEADSCALE_BIN" users list 2>/dev/null | grep -q "^[[:space:]]*${HEADSCALE_USER}[[:space:]]"; then
  echo "[headscale] Creating user ${HEADSCALE_USER}"
  "$HEADSCALE_BIN" users create "$HEADSCALE_USER"
fi

echo "[headscale] Generating reusable pre-auth key for user ${HEADSCALE_USER}"
preauthkey="$("$HEADSCALE_BIN" preauthkeys create \
  --user "$HEADSCALE_USER" \
  --reusable \
  --expiration "8760h" \
  --output json | python3 -c "import json,sys; print(json.load(sys.stdin)['key'])")"

if [ -z "$preauthkey" ]; then
  echo "[headscale] ERROR: failed to generate pre-auth key" >&2
  exit 1
fi

echo "[headscale] Pre-auth key generated (reusable, 1 year expiry)"

# Write to mac.env for use by tailscale install and worker deploys
set_env_key "$ENV_FILE" HEADSCALE_URL "$HEADSCALE_LOCAL_URL"
set_env_key "$ENV_FILE" HEADSCALE_FLEET_URL "$HEADSCALE_FLEET_URL"
set_env_key "$ENV_FILE" HEADSCALE_PREAUTHKEY "$preauthkey"
set_env_key "$ENV_FILE" HEADSCALE_PORT "$HEADSCALE_PORT"

echo "[headscale] Wrote credentials to ${ENV_FILE}"
echo "[headscale] Fleet URL (for workers): ${HEADSCALE_FLEET_URL}"
