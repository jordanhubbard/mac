#!/usr/bin/env bash
# install-tailscale.sh — install Tailscale and join the fleet mesh network.
#
# Supports two control plane modes:
#   tailscale cloud — Tailscale SaaS (requires TAILSCALE_AUTH_KEY)
#   headscale       — self-hosted control plane (requires explicit HEADSCALE_URL)
#
# In headscale mode HEADSCALE_URL and HEADSCALE_PREAUTHKEY must be set.
# For the hub, these are written by install-headscale.sh. For workers they
# are read from the hub's mac.env during deploy.
#
# Supports two data plane modes:
#   kernel    — tailscaled owns a TUN device and programs netfilter (default
#               on any node that has /dev/net/tun and NET_ADMIN)
#   userspace — tailscaled runs its network stack in-process and needs
#               neither TUN nor netfilter, at the cost of the host not
#               routing into the mesh implicitly
# The mode is probed automatically; MAC_DEPLOY_TAILSCALE_NETWORKING pins it.
set -euo pipefail

AGENT_NAME="${AGENT:-$(hostname)}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
LOG_DIR="${LOG_DIR:-$MAC_HOME/logs}"
ENV_FILE="${ENV_FILE:-$MAC_HOME/mac.env}"
SUPERVISOR_KIND="${TAILSCALE_SUPERVISOR:-${MAC_SUPERVISOR_KIND:-auto}}"
FLEET_NAME="${FLEET_NAME:-mac}"

# Headscale mode (preferred): point tailscale at a self-hosted control plane
HEADSCALE_URL="${HEADSCALE_URL:-}"
HEADSCALE_PREAUTHKEY="${HEADSCALE_PREAUTHKEY:-}"

# Cloud Tailscale fallback: used when HEADSCALE_URL is not set
TAILSCALE_AUTH_KEY="${MAC_DEPLOY_TAILSCALE_AUTH_KEY:-}"

TAILSCALE_HOSTNAME_PREFIX="${TAILSCALE_HOSTNAME_PREFIX:-}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME_PREFIX}${AGENT_NAME}"

# Under supervisord the daemon runs with a fleet-scoped socket path so that
# multiple fleets can coexist on the same host without socket collisions.
# All tailscale(1) client commands must point at the same socket; we compute
# it once after detect_supervisor() so every helper reads the same value.
TAILSCALE_SOCKET=""  # filled by compute_tailscale_socket() after supervisor detection

# Data plane selection: auto (probe for TUN), kernel (force TUN), or
# userspace (force Tailscale's in-process network stack).
MAC_DEPLOY_TAILSCALE_NETWORKING="${MAC_DEPLOY_TAILSCALE_NETWORKING:-auto}"
# In userspace mode nothing on the node routes into the mesh implicitly, so
# tailscaled publishes SOCKS5 and HTTP proxies on this loopback port instead.
# tailscaled multiplexes both protocols on a single listener.
MAC_DEPLOY_TAILSCALE_PROXY_PORT="${MAC_DEPLOY_TAILSCALE_PROXY_PORT:-1055}"
TAILSCALE_NETWORKING_MODE=""  # filled by detect_networking_mode()

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
    *) echo "[tailscale] ERROR: unsupported supervisor: $SUPERVISOR_KIND" >&2; exit 1 ;;
  esac
  # Prefer systemd only when it really is the init system (PID 1 owns
  # /run/systemd/system).  On container nodes (e.g. GKE pods) systemctl may be
  # present on PATH but PID 1 is supervisord; the directory check correctly
  # excludes those nodes so they fall through to the supervisord branch.
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    printf '%s\n' "systemd"; return
  fi
  if command -v launchctl >/dev/null 2>&1; then
    printf '%s\n' "launchd"; return
  fi
  if command -v supervisorctl >/dev/null 2>&1; then
    printf '%s\n' "supervisord"; return
  fi
  echo "[tailscale] ERROR: could not detect systemd, launchd, or supervisord" >&2
  exit 1
}

# Compute the tailscaled unix socket path for this fleet.
# Under supervisord we use a fleet-scoped path to avoid collisions.
# Under systemd/launchd tailscaled uses its default path; passing the flag
# with the same default is harmless but we omit it to stay close to stock.
compute_tailscale_socket() {
  local supervisor="$1"
  case "$supervisor" in
    supervisord)
      printf '%s\n' "/run/tailscale/${FLEET_NAME}.sock"
      ;;
    *)
      # Default tailscaled socket; empty means "let tailscale use its default"
      printf '%s\n' ""
      ;;
  esac
}

# Can tailscaled get a kernel TUN device on this node?
#
# A container that is not granted NET_ADMIN has no /dev/net/tun and cannot
# modprobe one, which is exactly how tailscaled fails on an hgx-provisioned
# GKE pod: "CreateTUN(\"tailscale0\") failed; /dev/net/tun does not exist".
# The same missing capability also makes tailscaled's iptables calls fail with
# "Permission denied (you must be root)" despite running as root, so one probe
# answers both questions.
tun_device_available() {
  # Only Linux exposes TUN as /dev/net/tun. macOS synthesises utun interfaces
  # on demand and never has that path, so a probe there would wrongly force
  # every Mac node into userspace mode.
  [ "$(uname -s)" = "Linux" ] || return 0
  [ -c /dev/net/tun ] && return 0
  # The node may simply not have the module loaded yet. Loading it needs the
  # capability we are testing for, so a failure here is the signal we want.
  if sudo -n modprobe tun >/dev/null 2>&1 || modprobe tun >/dev/null 2>&1; then
    [ -c /dev/net/tun ] && return 0
  fi
  return 1
}

detect_networking_mode() {
  case "$MAC_DEPLOY_TAILSCALE_NETWORKING" in
    kernel|userspace) printf '%s\n' "$MAC_DEPLOY_TAILSCALE_NETWORKING"; return ;;
    auto|"") ;;
    *)
      echo "[tailscale] ERROR: unsupported MAC_DEPLOY_TAILSCALE_NETWORKING:" \
        "$MAC_DEPLOY_TAILSCALE_NETWORKING (want auto, kernel, or userspace)" >&2
      exit 1
      ;;
  esac
  if tun_device_available; then
    printf '%s\n' "kernel"
  else
    printf '%s\n' "userspace"
  fi
}

# Extra tailscaled(8) flags for the resolved mode. Empty in kernel mode so the
# stock daemon invocation is unchanged on nodes that do have TUN.
tailscaled_networking_args() {
  if [ "$TAILSCALE_NETWORKING_MODE" = "userspace" ]; then
    printf '%s\n' "--tun=userspace-networking --socks5-server=localhost:${MAC_DEPLOY_TAILSCALE_PROXY_PORT} --outbound-http-proxy-listen=localhost:${MAC_DEPLOY_TAILSCALE_PROXY_PORT}"
  fi
}

# Extra join flags for the resolved mode. Userspace mode has no
# kernel routing table entry and no netfilter rules to program, so asking for
# either is what produces the permission-denied storm we are avoiding.
tailscale_up_networking_flags() {
  if [ "$TAILSCALE_NETWORKING_MODE" = "userspace" ]; then
    printf '%s\n' "--netfilter-mode=off --accept-dns=false"
  else
    printf '%s\n' "--accept-routes --accept-dns=true"
  fi
}

# Build --socket flag (empty string -> no flag, so systemd/launchd are unaffected)
tailscale_socket_flag() {
  if [ -n "$TAILSCALE_SOCKET" ]; then
    printf '%s\n' "--socket=$TAILSCALE_SOCKET"
  fi
}

run_tailscale() {
  # supervisord owns a root-scoped fleet socket. Use non-interactive privilege
  # for every client operation in that topology so a failed `up` cannot print
  # an auth-bearing suggested command to the deploy log.
  if [ "$SUPERVISOR_KIND" = "supervisord" ]; then
    sudo -n tailscale "$@"
  else
    tailscale "$@"
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

run_supervisorctl() {
  if command -v sudo >/dev/null 2>&1; then
    sudo -n supervisorctl "$@" || supervisorctl "$@"
  else
    supervisorctl "$@"
  fi
}

tailscale_connected() {
  command -v tailscale >/dev/null 2>&1 || return 1
  # shellcheck disable=SC2046
  run_tailscale $(tailscale_socket_flag) status --json 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('BackendState')=='Running' else 1)" 2>/dev/null
}

wait_for_tailscale_ip() {
  local i
  for i in $(seq 1 20); do
    local ip
    # shellcheck disable=SC2046
    ip="$(run_tailscale $(tailscale_socket_flag) ip -4 2>/dev/null | head -1 || true)"
    if [ -n "$ip" ]; then
      printf '%s\n' "$ip"
      return 0
    fi
    sleep 2
  done
  return 1
}

# -- Validate credentials --
if [ -n "$HEADSCALE_URL" ]; then
  if [ -z "$HEADSCALE_PREAUTHKEY" ]; then
    echo "[tailscale] ERROR: HEADSCALE_URL is set but HEADSCALE_PREAUTHKEY is empty" >&2
    exit 1
  fi
  control_mode="headscale"
  echo "[tailscale] Using headscale control plane: ${HEADSCALE_URL}"
elif [ -n "$TAILSCALE_AUTH_KEY" ]; then
  control_mode="cloud"
  echo "[tailscale] Using Tailscale cloud control plane"
else
  echo "[tailscale] ERROR: neither HEADSCALE_URL nor TAILSCALE_AUTH_KEY is set" >&2
  exit 1
fi

# -- Already connected? --
# Note: TAILSCALE_SOCKET is empty at this point (set after detect_supervisor below),
# so tailscale_connected uses the default socket path for the early idempotency check.
# This is intentional: if tailscale is already connected we skip everything including
# supervisor detection, which avoids unnecessary socket/conf-dir probing.
if tailscale_connected; then
  # shellcheck disable=SC2046
  ts_ip="$(run_tailscale $(tailscale_socket_flag) ip -4 2>/dev/null | head -1 || true)"
  echo "[tailscale] Already connected (IP: ${ts_ip:-unknown})"
  if [ -n "$ts_ip" ]; then
    set_env_key "$ENV_FILE" MAC_TAILSCALE_IP "$ts_ip"
    set_env_key "$ENV_FILE" MAC_TAILSCALE_HOSTNAME "$TAILSCALE_HOSTNAME"
  fi
  exit 0
fi

# -- Install tailscale package if missing --
if ! command -v tailscale >/dev/null 2>&1; then
  echo "[tailscale] Installing Tailscale"
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    curl -fsSL https://tailscale.com/install.sh | sudo -n sh
  elif command -v brew >/dev/null 2>&1; then
    brew install tailscale
  elif command -v yum >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sudo -n sh
  else
    echo "[tailscale] ERROR: unsupported platform; install tailscale manually" >&2
    exit 1
  fi
fi

if ! command -v tailscale >/dev/null 2>&1; then
  echo "[tailscale] ERROR: tailscale not found after install" >&2
  exit 1
fi

# -- Start tailscaled under the detected supervisor --
SUPERVISOR_KIND="$(detect_supervisor)"
TAILSCALE_SOCKET="$(compute_tailscale_socket "$SUPERVISOR_KIND")"
TAILSCALE_NETWORKING_MODE="$(detect_networking_mode)"
if [ "$TAILSCALE_NETWORKING_MODE" = "userspace" ]; then
  echo "[tailscale] No usable /dev/net/tun (container without NET_ADMIN?);" \
    "falling back to Tailscale userspace networking"
  echo "[tailscale] NOTE: this node joins the mesh but the host does not route" \
    "into it — mesh traffic must go through the local SOCKS5/HTTP proxy on" \
    "localhost:${MAC_DEPLOY_TAILSCALE_PROXY_PORT}, and only ports published" \
    "with 'tailscale serve' are reachable from other nodes."
fi
echo "[tailscale] Starting tailscaled under ${SUPERVISOR_KIND} (${TAILSCALE_NETWORKING_MODE} networking)"
mkdir -p "$LOG_DIR"

case "$SUPERVISOR_KIND" in
  systemd)
    if [ "$TAILSCALE_NETWORKING_MODE" = "userspace" ]; then
      # The packaged tailscaled.service appends $FLAGS from this file to its
      # ExecStart, so it is the supported way to add daemon flags without
      # editing a package-owned unit. Rewrite then restart, because a daemon
      # that is already up would ignore `systemctl start`.
      sudo -n install -d -m 0755 /etc/default
      sudo -n tee /etc/default/tailscaled >/dev/null <<EOF
# Managed by mac deploy/install-tailscale.sh
PORT="41641"
FLAGS="$(tailscaled_networking_args)"
EOF
      sudo -n systemctl enable tailscaled >/dev/null 2>&1 || true
      sudo -n systemctl restart tailscaled
    else
      sudo -n systemctl enable tailscaled >/dev/null 2>&1 || true
      sudo -n systemctl start tailscaled
    fi
    ;;
  supervisord)
    conf_dir="$(supervisord_conf_dir)"
    sudo -n install -d -m 0755 "$conf_dir"
    sudo -n tee "$conf_dir/${FLEET_NAME}-tailscaled.conf" >/dev/null <<EOF
[program:${FLEET_NAME}-tailscaled]
command=/usr/sbin/tailscaled --state=/var/lib/${FLEET_NAME}/tailscale/tailscaled.state --socket=/run/tailscale/${FLEET_NAME}.sock --port=41641 $(tailscaled_networking_args)
directory=/var/lib/${FLEET_NAME}/tailscale
user=root
autostart=true
autorestart=true
startsecs=3
stopwaitsecs=15
stdout_logfile=$LOG_DIR/tailscaled.log
stderr_logfile=$LOG_DIR/tailscaled.log
EOF
    sudo -n mkdir -p /var/lib/${FLEET_NAME}/tailscale /run/tailscale
    run_supervisorctl reread >/dev/null
    run_supervisorctl update >/dev/null
    run_supervisorctl restart "${FLEET_NAME}-tailscaled" >/dev/null 2>&1 \
      || run_supervisorctl start "${FLEET_NAME}-tailscaled" >/dev/null
    ;;
  launchd)
    sudo -n launchctl enable system/com.tailscale.tailscaled 2>/dev/null || true
    sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.tailscale.tailscaled.plist 2>/dev/null || true
    sudo -n launchctl kickstart -k system/com.tailscale.tailscaled 2>/dev/null || true
    ;;
esac

# Wait for tailscaled socket to be ready
for i in $(seq 1 10); do
  # shellcheck disable=SC2046
  if run_tailscale $(tailscale_socket_flag) status >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

# -- Join the network --
echo "[tailscale] Joining as hostname='${TAILSCALE_HOSTNAME}'"

if [ "$control_mode" = "headscale" ]; then
  # shellcheck disable=SC2046
  run_tailscale $(tailscale_socket_flag) up \
    --login-server="$HEADSCALE_URL" \
    --auth-key="$HEADSCALE_PREAUTHKEY" \
    --hostname="$TAILSCALE_HOSTNAME" \
    $(tailscale_up_networking_flags) >/dev/null 2>&1 || {
      echo "[tailscale] ERROR: headscale join failed (credential-bearing output suppressed)" >&2
      exit 1
    }
else
  # shellcheck disable=SC2046
  run_tailscale $(tailscale_socket_flag) up \
    --auth-key="$TAILSCALE_AUTH_KEY" \
    --hostname="$TAILSCALE_HOSTNAME" \
    $(tailscale_up_networking_flags) >/dev/null 2>&1 || {
      echo "[tailscale] ERROR: tailscale join failed (credential-bearing output suppressed)" >&2
      exit 1
    }
fi

# -- Wait for Tailscale IP --
ts_ip="$(wait_for_tailscale_ip || true)"
if [ -z "$ts_ip" ]; then
  echo "[tailscale] ERROR: did not get a Tailscale IP after joining" >&2
  # shellcheck disable=SC2046
  run_tailscale $(tailscale_socket_flag) status >&2 || true
  exit 1
fi

echo "[tailscale] Connected — hostname=${TAILSCALE_HOSTNAME} IP=${ts_ip} networking=${TAILSCALE_NETWORKING_MODE}"

set_env_key "$ENV_FILE" MAC_TAILSCALE_IP "$ts_ip"
set_env_key "$ENV_FILE" MAC_TAILSCALE_HOSTNAME "$TAILSCALE_HOSTNAME"
# Record the data plane so anything reading mac.env can tell a node that routes
# into the mesh from one that can only reach it through the local proxy.
set_env_key "$ENV_FILE" MAC_TAILSCALE_NETWORKING_MODE "$TAILSCALE_NETWORKING_MODE"
if [ "$TAILSCALE_NETWORKING_MODE" = "userspace" ]; then
  set_env_key "$ENV_FILE" MAC_TAILSCALE_SOCKS5_PROXY "socks5://localhost:${MAC_DEPLOY_TAILSCALE_PROXY_PORT}"
  set_env_key "$ENV_FILE" MAC_TAILSCALE_HTTP_PROXY "http://localhost:${MAC_DEPLOY_TAILSCALE_PROXY_PORT}"
fi
