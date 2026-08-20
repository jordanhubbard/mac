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
set -euo pipefail

AGENT_NAME="${AGENT:-$(hostname)}"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
LOG_DIR="${LOG_DIR:-$MAC_HOME/logs}"
ENV_FILE="${ENV_FILE:-$MAC_HOME/mac.env}"
SUPERVISOR_KIND="${TAILSCALE_SUPERVISOR:-${MAC_SUPERVISOR_KIND:-auto}}"
FLEET_NAME="${FLEET_NAME:-mac}"

# Datapath engine selection.
#
# tailscaled's default engine needs a kernel TUN device: /dev/net/tun must
# exist AND the process must hold CAP_NET_ADMIN so it can create tailscale0 and
# program netfilter.  An hgx-provisioned container node (e.g. a gke-newhouse
# `standard` pod) is granted neither, and tailscaled hard-fails:
#
#   is CONFIG_TUN enabled in your kernel? `modprobe tun` failed
#   tstun.New("tailscale0"): CreateTUN("tailscale0") failed;
#     /dev/net/tun does not exist
#
# The same missing capability makes every iptables/ip6tables call fail with
# "Permission denied (you must be root)" even under user=root, because netlink
# rejects the unprivileged netns.  No auth key and no supervisor change can fix
# that: it is the pod spec, not the deploy.  Tailscale ships a userspace
# networking engine for exactly this topology, so detect the missing capability
# and select it instead of failing the node.
#
#   auto      probe /dev/net/tun + CAP_NET_ADMIN and pick kernel or userspace
#   kernel    force the default TUN engine (fail loudly if it is unavailable)
#   userspace force userspace networking even where TUN would work
TAILSCALE_TUN_MODE="${MAC_DEPLOY_TAILSCALE_TUN_MODE:-auto}"
# Local proxy endpoints published by the userspace engine.  In userspace mode
# the node has no tailscale0 interface, so host traffic reaches the mesh only
# through these listeners.
TAILSCALE_USERSPACE_SOCKS5_PORT="${MAC_DEPLOY_TAILSCALE_SOCKS5_PORT:-1055}"
TAILSCALE_USERSPACE_HTTP_PROXY_PORT="${MAC_DEPLOY_TAILSCALE_HTTP_PROXY_PORT:-1055}"
TAILSCALE_TUN_MODE_RESOLVED=""   # filled by resolve_tun_mode()
TAILSCALE_TUN_MODE_REASON=""     # why resolve_tun_mode() chose what it chose

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

# -- Datapath capability probes --------------------------------------------
# Both probes are read-only and never modprobe: on a container node the module
# load is exactly what is denied, so attempting it only produces noise.

have_tun_device() {
  [ -c /dev/net/tun ]
}

# CAP_NET_ADMIN (capability bit 12) in this process's bounding set.  The
# bounding set is the right question: a pod that was never granted NET_ADMIN
# cannot regain it by becoming root, which is precisely why `user=root` in the
# supervisord stanza did not help.  Reading /proc/self/status works without
# capsh(1), which container images routinely omit.
have_net_admin() {
  local bounding
  bounding="$(awk '/^CapBnd:/ { print $2; exit }' /proc/self/status 2>/dev/null || true)"
  # No /proc/self/status (macOS, or a hardened procfs) means "unknown", not
  # "missing".  Fail open so a Mac node keeps its stock kernel datapath; the
  # /dev/net/tun probe still guards the container case.
  [ -n "$bounding" ] || return 0
  python3 -c 'import sys
try:
    mask = int(sys.argv[1], 16)
except ValueError:
    sys.exit(0)
sys.exit(0 if mask & (1 << 12) else 1)' "$bounding"
}

# Resolve kernel vs userspace once; echoes "<mode> <reason>".
resolve_tun_mode() {
  case "$TAILSCALE_TUN_MODE" in
    kernel)    printf '%s %s\n' "kernel" "forced_by_operator"; return ;;
    userspace) printf '%s %s\n' "userspace" "forced_by_operator"; return ;;
    auto|"") ;;
    *)
      echo "[tailscale] ERROR: unsupported MAC_DEPLOY_TAILSCALE_TUN_MODE: $TAILSCALE_TUN_MODE (expected auto, kernel, or userspace)" >&2
      exit 1
      ;;
  esac
  # The probe is Linux-specific.  macOS has no /dev/net/tun (tailscaled uses
  # utun there) and no CapBnd, so probing would misread a healthy Mac node as
  # capability-starved and quietly downgrade it to userspace networking.
  if [ "$(uname -s 2>/dev/null || echo unknown)" != "Linux" ]; then
    printf '%s %s\n' "kernel" "non_linux_host"
    return
  fi
  if ! have_tun_device; then
    printf '%s %s\n' "userspace" "no_dev_net_tun"
    return
  fi
  if ! have_net_admin; then
    printf '%s %s\n' "userspace" "no_cap_net_admin"
    return
  fi
  printf '%s %s\n' "kernel" "tun_and_net_admin_present"
}

# Flags tailscaled itself needs for the resolved mode.
tailscaled_mode_flags() {
  if [ "$TAILSCALE_TUN_MODE_RESOLVED" = "userspace" ]; then
    printf '%s\n' "--tun=userspace-networking --socks5-server=localhost:${TAILSCALE_USERSPACE_SOCKS5_PORT} --outbound-http-proxy-listen=localhost:${TAILSCALE_USERSPACE_HTTP_PROXY_PORT}"
  fi
}

# Extra join flags handed to tailscale(1) up for the resolved mode.  Userspace networking has
# no tailscale0 interface: there is nothing to install routes into and no
# netfilter access, so asking for either only produces the permission-denied
# spam that made the original failure hard to read.
tailscale_up_mode_flags() {
  if [ "$TAILSCALE_TUN_MODE_RESOLVED" = "userspace" ]; then
    printf '%s\n' "--netfilter-mode=off --accept-routes=false --accept-dns=false"
  else
    printf '%s\n' "--accept-routes --accept-dns=true"
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

# Everything past this point provisions the daemon, so the datapath the node is
# capable of has to be known before the supervisor stanza is written.

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
read -r TAILSCALE_TUN_MODE_RESOLVED TAILSCALE_TUN_MODE_REASON <<EOF
$(resolve_tun_mode)
EOF
if [ "$TAILSCALE_TUN_MODE_RESOLVED" = "userspace" ]; then
  echo "[tailscale] datapath=userspace-networking (reason: ${TAILSCALE_TUN_MODE_REASON}) — this node joins the mesh in relay mode; outbound host traffic must use the local SOCKS5/HTTP proxy on port ${TAILSCALE_USERSPACE_SOCKS5_PORT}"
else
  echo "[tailscale] datapath=kernel-tun (reason: ${TAILSCALE_TUN_MODE_REASON})"
fi
echo "[tailscale] Starting tailscaled under ${SUPERVISOR_KIND}"
mkdir -p "$LOG_DIR"

case "$SUPERVISOR_KIND" in
  systemd)
    # The packaged unit runs `tailscaled ... $FLAGS` with an EnvironmentFile,
    # so the datapath choice belongs there rather than in a unit override.
    if [ -n "$(tailscaled_mode_flags)" ]; then
      sudo -n tee /etc/default/tailscaled >/dev/null <<EOF
# Written by mac install-tailscale.sh: $TAILSCALE_TUN_MODE_REASON
FLAGS="$(tailscaled_mode_flags)"
EOF
      sudo -n systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    sudo -n systemctl enable tailscaled >/dev/null 2>&1 || true
    sudo -n systemctl start tailscaled
    ;;
  supervisord)
    conf_dir="$(supervisord_conf_dir)"
    sudo -n install -d -m 0755 "$conf_dir"
    sudo -n tee "$conf_dir/${FLEET_NAME}-tailscaled.conf" >/dev/null <<EOF
[program:${FLEET_NAME}-tailscaled]
command=/usr/sbin/tailscaled --state=/var/lib/${FLEET_NAME}/tailscale/tailscaled.state --socket=/run/tailscale/${FLEET_NAME}.sock --port=41641 $(tailscaled_mode_flags)
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
    $(tailscale_up_mode_flags) >/dev/null 2>&1 || {
      echo "[tailscale] ERROR: headscale join failed (credential-bearing output suppressed)" >&2
      exit 1
    }
else
  # shellcheck disable=SC2046
  run_tailscale $(tailscale_socket_flag) up \
    --auth-key="$TAILSCALE_AUTH_KEY" \
    --hostname="$TAILSCALE_HOSTNAME" \
    $(tailscale_up_mode_flags) >/dev/null 2>&1 || {
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

echo "[tailscale] Connected — hostname=${TAILSCALE_HOSTNAME} IP=${ts_ip} datapath=${TAILSCALE_TUN_MODE_RESOLVED}"

set_env_key "$ENV_FILE" MAC_TAILSCALE_IP "$ts_ip"
set_env_key "$ENV_FILE" MAC_TAILSCALE_HOSTNAME "$TAILSCALE_HOSTNAME"
# Downstream deploy steps must be able to tell a kernel-datapath node from a
# userspace-relay one: on the latter, a bare `curl http://100.x.y.z:8789` does
# not route, and callers have to go through the proxy recorded below.
set_env_key "$ENV_FILE" MAC_TAILSCALE_TUN_MODE "$TAILSCALE_TUN_MODE_RESOLVED"
if [ "$TAILSCALE_TUN_MODE_RESOLVED" = "userspace" ]; then
  set_env_key "$ENV_FILE" MAC_TAILSCALE_SOCKS5_PROXY "socks5://localhost:${TAILSCALE_USERSPACE_SOCKS5_PORT}"
  set_env_key "$ENV_FILE" MAC_TAILSCALE_HTTP_PROXY "http://localhost:${TAILSCALE_USERSPACE_HTTP_PROXY_PORT}"
else
  set_env_key "$ENV_FILE" MAC_TAILSCALE_SOCKS5_PROXY ""
  set_env_key "$ENV_FILE" MAC_TAILSCALE_HTTP_PROXY ""
fi
