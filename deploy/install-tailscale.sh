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
# It also supports two data plane modes, selected by probing the node instead
# of by assuming a privileged host:
#   tun       — the stock kernel TUN engine (needs /dev/net/tun + CAP_NET_ADMIN)
#   userspace — Tailscale's userspace-networking engine, which needs neither
#
# Run with MAC_TAILSCALE_PROBE_ONLY=1 (or --print-network-capability) to print
# the capability classification as JSON and exit without installing anything.
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

# Data plane mode: auto (probe the node), tun, or userspace.
TAILSCALE_NETWORK_MODE="${TAILSCALE_NETWORK_MODE:-auto}"
# Where the userspace engine publishes its outbound proxy. Both the SOCKS5 and
# the HTTP CONNECT listener share one address, which is what tailscaled's own
# userspace-networking documentation does.
TAILSCALE_USERSPACE_PROXY_HOST="${TAILSCALE_USERSPACE_PROXY_HOST:-127.0.0.1}"
TAILSCALE_USERSPACE_PROXY_PORT="${TAILSCALE_USERSPACE_PROXY_PORT:-1055}"
# Test/override seams for the capability probe. Production values are the
# real paths; the tests point them at fixtures so the probe is checkable on a
# host whose own capabilities differ from the node being modelled.
TAILSCALE_TUN_DEVICE="${TAILSCALE_TUN_DEVICE:-/dev/net/tun}"
TAILSCALE_PROC_STATUS="${TAILSCALE_PROC_STATUS:-/proc/self/status}"

# Filled in by select_network_mode(); empty means "stock TUN behavior".
TAILSCALED_EXTRA_FLAGS=""
TAILSCALE_UP_EXTRA_FLAGS=""
# MagicDNS is installed into the host resolver through the TUN interface. A
# userspace node has no such interface, so accepting DNS there would point the
# host resolver at a nameserver nothing can route to.
TAILSCALE_ACCEPT_DNS="true"

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

# Build --socket flag (empty string -> no flag, so systemd/launchd are unaffected)
tailscale_socket_flag() {
  if [ -n "$TAILSCALE_SOCKET" ]; then
    printf '%s\n' "--socket=$TAILSCALE_SOCKET"
  fi
}

# -- Node network capability probe ----------------------------------------
#
# tailscaled's default engine needs two things a container is not guaranteed:
# a TUN device (/dev/net/tun, or a loadable tun module) and CAP_NET_ADMIN to
# program netfilter. A pod created without an explicit capability request has
# neither, and reports it twice: CreateTUN fails because /dev/net/tun does not
# exist, and every iptables call fails with "Permission denied (you must be
# root)" even for uid 0 — because the capability is missing from the container
# bounding set, not from the process. Probing both up front lets such a node
# join the mesh in userspace-networking mode instead of hard-failing.

node_has_tun_device() {
  [ -c "$TAILSCALE_TUN_DEVICE" ] && return 0
  # tailscaled tries this itself before giving up; a node that can load the
  # module is a TUN node.
  if command -v modprobe >/dev/null 2>&1; then
    sudo -n modprobe tun >/dev/null 2>&1 || true
  fi
  [ -c "$TAILSCALE_TUN_DEVICE" ]
}

node_has_net_admin() {
  # The bounding set is the honest check: a root process cannot acquire a
  # capability the container's bounding set does not contain, so an effective
  # set can look adequate on a node that still cannot program netfilter.
  [ -r "$TAILSCALE_PROC_STATUS" ] || return 1
  python3 - "$TAILSCALE_PROC_STATUS" <<'PY'
import sys

CAP_NET_ADMIN = 12
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("CapBnd:"):
                mask = int(line.split()[1], 16)
                sys.exit(0 if (mask >> CAP_NET_ADMIN) & 1 else 1)
except (OSError, ValueError, IndexError):
    pass
sys.exit(1)
PY
}

# Classify the node and print 'tun' or 'userspace'.
classify_network_mode() {
  case "$TAILSCALE_NETWORK_MODE" in
    tun|userspace) printf '%s\n' "$TAILSCALE_NETWORK_MODE"; return ;;
    auto|"") ;;
    *)
      # Return rather than exit: this function is called through command
      # substitution, where an exit would only kill the subshell and leave the
      # caller with an empty mode.
      echo "[tailscale] ERROR: unsupported TAILSCALE_NETWORK_MODE: $TAILSCALE_NETWORK_MODE" >&2
      return 1
      ;;
  esac
  # The /dev/net/tun + CAP_NET_ADMIN contract is Linux-specific. macOS reaches
  # the network through utun and never needs the fallback.
  if [ "$(uname -s)" != "Linux" ]; then
    printf '%s\n' "tun"
    return
  fi
  if node_has_tun_device && node_has_net_admin; then
    printf '%s\n' "tun"
  else
    printf '%s\n' "userspace"
  fi
}

# Emit the classification as JSON without mutating the node.
print_network_capability() {
  local tun="false" net_admin="false" mode
  mode="$(classify_network_mode)" || return 1
  node_has_tun_device && tun="true"
  node_has_net_admin && net_admin="true"
  printf '{"schema":"mac.node_network_capability.v1","os":"%s","tun_device":%s,"net_admin":%s,"mode":"%s"}\n' \
    "$(uname -s)" "$tun" "$net_admin" "$mode"
}

userspace_proxy_address() {
  printf '%s:%s\n' "$TAILSCALE_USERSPACE_PROXY_HOST" "$TAILSCALE_USERSPACE_PROXY_PORT"
}

# Resolve the data plane mode once and derive every flag that depends on it.
select_network_mode() {
  TAILSCALE_NETWORK_MODE="$(classify_network_mode)" || exit 1
  if [ "$TAILSCALE_NETWORK_MODE" != "userspace" ]; then
    return
  fi
  local proxy
  proxy="$(userspace_proxy_address)"
  TAILSCALED_EXTRA_FLAGS="--tun=userspace-networking --socks5-server=${proxy} --outbound-http-proxy-listen=${proxy}"
  # The userspace engine never touches netfilter; saying so explicitly keeps
  # `up` from attempting the iptables setup that this node cannot perform.
  TAILSCALE_UP_EXTRA_FLAGS="--netfilter-mode=off"
  TAILSCALE_ACCEPT_DNS="false"
  echo "[tailscale] Node has no usable TUN device or CAP_NET_ADMIN; joining in userspace-networking mode"
  echo "[tailscale] Tailnet traffic from this node must go through the local proxy at ${proxy}"
}

# Record the resolved mode so that everything reading mac.env knows whether
# this node's tailnet address is bound to the host stack (tun) or reachable
# only through the local proxy (userspace).
record_network_mode_env() {
  set_env_key "$ENV_FILE" MAC_TAILSCALE_NETWORK_MODE "$TAILSCALE_NETWORK_MODE"
  if [ "$TAILSCALE_NETWORK_MODE" = "userspace" ]; then
    set_env_key "$ENV_FILE" MAC_TAILSCALE_SOCKS5_PROXY "$(userspace_proxy_address)"
    set_env_key "$ENV_FILE" MAC_TAILSCALE_HTTP_PROXY "http://$(userspace_proxy_address)"
  fi
}

# The Debian/RPM tailscaled unit reads flags from /etc/default/tailscaled, so
# that is where a systemd node's userspace flags belong.
configure_systemd_daemon_flags() {
  local file="${TAILSCALED_DEFAULTS_FILE:-/etc/default/tailscaled}" rendered
  rendered="$(
    if [ -f "$file" ]; then
      grep -v '^FLAGS=' "$file" || true
    fi
    printf 'FLAGS="%s"\n' "$TAILSCALED_EXTRA_FLAGS"
  )"
  printf '%s\n' "$rendered" | sudo -n tee "$file" >/dev/null
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

# -- Capability probe only: classify and exit before touching the node --
if [ "${MAC_TAILSCALE_PROBE_ONLY:-0}" = "1" ] || [ "${1:-}" = "--print-network-capability" ]; then
  print_network_capability || exit 1
  exit 0
fi

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

# -- Resolve the data plane mode before anything reads it --
# This runs ahead of the already-connected check so that mac.env records the
# node's real mode on every path, not only on a fresh join.
select_network_mode

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
    record_network_mode_env
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
echo "[tailscale] Starting tailscaled under ${SUPERVISOR_KIND}"
mkdir -p "$LOG_DIR"

case "$SUPERVISOR_KIND" in
  systemd)
    if [ -n "$TAILSCALED_EXTRA_FLAGS" ]; then
      configure_systemd_daemon_flags
      sudo -n systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    sudo -n systemctl enable tailscaled >/dev/null 2>&1 || true
    sudo -n systemctl restart tailscaled 2>/dev/null || sudo -n systemctl start tailscaled
    ;;
  supervisord)
    conf_dir="$(supervisord_conf_dir)"
    sudo -n install -d -m 0755 "$conf_dir"
    sudo -n tee "$conf_dir/${FLEET_NAME}-tailscaled.conf" >/dev/null <<EOF
[program:${FLEET_NAME}-tailscaled]
command=/usr/sbin/tailscaled --state=/var/lib/${FLEET_NAME}/tailscale/tailscaled.state --socket=/run/tailscale/${FLEET_NAME}.sock --port=41641 ${TAILSCALED_EXTRA_FLAGS}
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
  # shellcheck disable=SC2046,SC2086
  run_tailscale $(tailscale_socket_flag) up \
    --login-server="$HEADSCALE_URL" \
    --auth-key="$HEADSCALE_PREAUTHKEY" \
    --hostname="$TAILSCALE_HOSTNAME" \
    --accept-routes \
    --accept-dns="$TAILSCALE_ACCEPT_DNS" \
    $TAILSCALE_UP_EXTRA_FLAGS >/dev/null 2>&1 || {
      echo "[tailscale] ERROR: headscale join failed (credential-bearing output suppressed)" >&2
      exit 1
    }
else
  # shellcheck disable=SC2046,SC2086
  run_tailscale $(tailscale_socket_flag) up \
    --auth-key="$TAILSCALE_AUTH_KEY" \
    --hostname="$TAILSCALE_HOSTNAME" \
    --accept-routes \
    --accept-dns="$TAILSCALE_ACCEPT_DNS" \
    $TAILSCALE_UP_EXTRA_FLAGS >/dev/null 2>&1 || {
      echo "[tailscale] ERROR: tailscale join failed (credential-bearing output suppressed)" >&2
      exit 1
    }
fi

# -- Wait for Tailscale IP --
ts_ip="$(wait_for_tailscale_ip || true)"
if [ -z "$ts_ip" ]; then
  echo "[tailscale] ERROR: did not get a Tailscale IP after joining (mode=${TAILSCALE_NETWORK_MODE})" >&2
  # shellcheck disable=SC2046
  run_tailscale $(tailscale_socket_flag) status >&2 || true
  exit 1
fi

echo "[tailscale] Connected — hostname=${TAILSCALE_HOSTNAME} IP=${ts_ip} mode=${TAILSCALE_NETWORK_MODE}"

set_env_key "$ENV_FILE" MAC_TAILSCALE_IP "$ts_ip"
set_env_key "$ENV_FILE" MAC_TAILSCALE_HOSTNAME "$TAILSCALE_HOSTNAME"
record_network_mode_env

if [ "$TAILSCALE_NETWORK_MODE" = "userspace" ]; then
  cat >&2 <<EOF
[tailscale] This node joined in userspace-networking mode. Its tailnet address
[tailscale] is not bound to the host network stack, so:
[tailscale]   - inbound tailnet connections are forwarded to localhost by tailscaled;
[tailscale]   - outbound tailnet connections must use the proxy recorded in
[tailscale]     ${ENV_FILE} as MAC_TAILSCALE_SOCKS5_PROXY / MAC_TAILSCALE_HTTP_PROXY;
[tailscale]   - subnet routes and MagicDNS are not installed on this host.
[tailscale] Grant the pod CAP_NET_ADMIN and /dev/net/tun for full TUN networking.
EOF
fi
