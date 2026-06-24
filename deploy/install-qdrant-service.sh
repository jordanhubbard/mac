#!/usr/bin/env bash
# install-qdrant-service.sh - install/start Qdrant for the mac hub memory layer.
#
# Qdrant is a hub-managed shared service. Worker agents use the hub endpoint as
# shared level-2 memory for Hermes recall and semantic history. Bind to the
# hub's Tailscale IPv4 when available; never bind to all interfaces.
set -euo pipefail

MAC_HOME="${MAC_HOME:-$HOME/.mac}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
FLEET_NAME="${FLEET_NAME:-mac}"
UNIT_TEMPLATE="${WORKSPACE}/deploy/systemd/mac-qdrant.service"
UNIT_DEST="/etc/systemd/system/${FLEET_NAME}-qdrant.service"
SUPERVISOR_KIND="${QDRANT_SUPERVISOR:-${MAC_SUPERVISOR_KIND:-auto}}"

# Platform handling. macOS has no /etc service-config tree and no
# passwordless sudo over a non-interactive deploy, so service env files live
# under $MAC_HOME; Docker Desktop is not on the launchd/ssh PATH by default,
# so add it explicitly. Linux is unchanged (/etc + sudo).
OS_NAME="$(uname -s)"
if [ "$OS_NAME" = "Darwin" ]; then
  ENV_CONF_DIR="$MAC_HOME/service-env"
  for _docker_dir in /Applications/Docker.app/Contents/Resources/bin /opt/homebrew/bin /usr/local/bin; do
    if [ -x "$_docker_dir/docker" ]; then
      case ":$PATH:" in *":$_docker_dir:"*) ;; *) PATH="$_docker_dir:$PATH" ;; esac
    fi
  done
  export PATH
else
  ENV_CONF_DIR="/etc/${FLEET_NAME}"
fi
ENV_DEST="$ENV_CONF_DIR/qdrant.env"

maybe_sudo() {
  if [ "$OS_NAME" = "Darwin" ]; then
    "$@"
  else
    sudo "$@"
  fi
}

QDRANT_IMAGE="${QDRANT_IMAGE:-docker.io/qdrant/qdrant:latest}"

detect_tailscale_ip() {
  if command -v tailscale >/dev/null 2>&1; then
    tailscale ip -4 2>/dev/null | head -1
  fi
}

default_qdrant_bind_addr() {
  local ts_ip
  ts_ip="$(detect_tailscale_ip || true)"
  if [ -n "$ts_ip" ]; then
    printf '%s\n' "$ts_ip"
  else
    printf '%s\n' "127.0.0.1"
  fi
}

QDRANT_BIND_ADDR="${QDRANT_BIND_ADDR:-$(default_qdrant_bind_addr)}"
QDRANT_PORT="${QDRANT_PORT:-6333}"
if [ "$OS_NAME" = "Darwin" ]; then
  QDRANT_DATA_DIR="${QDRANT_DATA_DIR:-$MAC_HOME/qdrant}"
else
  QDRANT_DATA_DIR="${QDRANT_DATA_DIR:-/var/lib/${FLEET_NAME}/qdrant}"
fi
QDRANT_MEMORY_LIMIT="${QDRANT_MEMORY_LIMIT:-2g}"
QDRANT_CONTAINER_NAME="${QDRANT_CONTAINER_NAME:-${FLEET_NAME}-qdrant}"
LOG_DIR="${LOG_DIR:-$MAC_HOME/logs}"

detect_supervisor() {
  case "$SUPERVISOR_KIND" in
    systemd|launchd|supervisord)
      printf '%s\n' "$SUPERVISOR_KIND"
      return
      ;;
    auto|"")
      ;;
    *)
      echo "[qdrant] ERROR: unsupported supervisor: $SUPERVISOR_KIND" >&2
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
  echo "[qdrant] ERROR: could not detect systemd, launchd, or supervisord" >&2
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

if [ -z "$WORKSPACE" ] || [ ! -f "$UNIT_TEMPLATE" ]; then
  echo "[qdrant] ERROR: cannot locate $UNIT_TEMPLATE" >&2
  exit 1
fi

case "$QDRANT_BIND_ADDR" in
  0.0.0.0|::|\[::\])
    echo "[qdrant] ERROR: refusing unsafe all-interface bind address: $QDRANT_BIND_ADDR" >&2
    exit 1
    ;;
esac

if [ "${1:-}" = "--print-bind-addr" ]; then
  printf '%s\n' "$QDRANT_BIND_ADDR"
  exit 0
fi

CONTAINER_CMD=""
QDRANT_BINARY="${QDRANT_BINARY:-$MAC_HOME/bin/qdrant}"
USE_NATIVE_BINARY=0

_container_works() {
  command -v "$1" >/dev/null 2>&1 || return 1
  "$1" info >/dev/null 2>&1 || return 1
  # podman rootless without cgroup delegation can't start containers (empty
  # cgroupControllers). Docker (Engine or Desktop) manages cgroups inside its
  # own daemon/VM and never prints that podman-specific field, so a successful
  # `docker info` is sufficient.
  if [ "$1" = "podman" ]; then
    "$1" info 2>/dev/null | grep -qE 'cgroupControllers:.*\S'
  fi
}

for _candidate in podman docker; do
  if command -v "$_candidate" >/dev/null 2>&1 && _container_works "$_candidate"; then
    CONTAINER_CMD="$_candidate"
    break
  fi
done
if [ -z "$CONTAINER_CMD" ] && command -v apt-get >/dev/null 2>&1; then
  echo "[qdrant] no working container runtime; trying apt-get install podman"
  sudo apt-get install -y podman >/dev/null 2>&1 || true
  if command -v podman >/dev/null 2>&1 && _container_works podman; then
    CONTAINER_CMD="podman"
  fi
fi
if [ -z "$CONTAINER_CMD" ] && [ "$OS_NAME" = "Darwin" ]; then
  echo "[qdrant] ERROR: Docker is required on macOS but no working 'docker' was found." >&2
  echo "[qdrant] Start Docker Desktop so 'docker info' succeeds, then re-run." >&2
  echo "[qdrant] (the Linux native-binary fallback is not usable on macOS)" >&2
  exit 1
fi
if [ -z "$CONTAINER_CMD" ]; then
  if [ -x "$QDRANT_BINARY" ]; then
    USE_NATIVE_BINARY=1
    echo "[qdrant] using existing native qdrant binary at $QDRANT_BINARY"
  elif command -v curl >/dev/null 2>&1; then
    _qdrant_ver="$(curl -fsSL "https://api.github.com/repos/qdrant/qdrant/releases/latest" 2>/dev/null \
      | grep '"tag_name"' | sed 's/.*"tag_name": *"v\([^"]*\)".*/\1/' | tr -d '\r\n')"
    if [ -n "$_qdrant_ver" ]; then
      case "$(uname -m)" in
        x86_64)  _qdrant_asset="qdrant-x86_64-unknown-linux-musl.tar.gz" ;;
        aarch64) _qdrant_asset="qdrant-aarch64-unknown-linux-musl.tar.gz" ;;
        *)       _qdrant_asset="" ;;
      esac
      if [ -n "$_qdrant_asset" ]; then
        echo "[qdrant] downloading native binary v${_qdrant_ver}"
        _tmp="$(mktemp -d)"
        if curl -fsSL "https://github.com/qdrant/qdrant/releases/download/v${_qdrant_ver}/${_qdrant_asset}" \
             | tar -xz -C "$_tmp" 2>/dev/null && [ -x "$_tmp/qdrant" ]; then
          mkdir -p "$MAC_HOME/bin"
          install -m 0755 "$_tmp/qdrant" "$QDRANT_BINARY"
          rm -rf "$_tmp"
          USE_NATIVE_BINARY=1
          echo "[qdrant] native binary v${_qdrant_ver} installed at $QDRANT_BINARY"
        else
          rm -rf "$_tmp"
        fi
      fi
    fi
  fi
  if [ "$USE_NATIVE_BINARY" = 0 ]; then
    echo "[qdrant] ERROR: no container runtime (podman/docker) or native binary available" >&2
    exit 1
  fi
fi

# Absolute path to the container runtime so the launchd/systemd wrapper finds
# it under a minimal PATH (Docker Desktop lives outside the default PATH).
if [ -n "$CONTAINER_CMD" ]; then
  CONTAINER_CMD_ABS="$(command -v "$CONTAINER_CMD" 2>/dev/null || printf '%s' "$CONTAINER_CMD")"
else
  CONTAINER_CMD_ABS="$CONTAINER_CMD"
fi

SUPERVISOR_KIND="$(detect_supervisor)"

# Portable in-place upsert of KEY=VALUE (GNU `sed -i` and BSD `sed -i ''`
# differ, so avoid sed entirely).
set_env_key() {
  local file="$1" key="$2" value="$3" tmp
  mkdir -p "$(dirname "$file")"
  if [ ! -f "$file" ]; then
    : > "$file"
    chmod 600 "$file"
  fi
  if grep -q "^${key}=" "$file"; then
    tmp="$(mktemp)"
    grep -v "^${key}=" "$file" > "$tmp"
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

service_url="http://${QDRANT_BIND_ADDR}:${QDRANT_PORT}"

echo "[qdrant] Installing Qdrant under ${SUPERVISOR_KIND}"
echo "[qdrant] Binding Qdrant to ${QDRANT_BIND_ADDR}:${QDRANT_PORT}"
maybe_sudo install -d -m 0755 "$ENV_CONF_DIR"
maybe_sudo install -d -m 0750 "$QDRANT_DATA_DIR"
maybe_sudo chown "$USER" "$QDRANT_DATA_DIR" || true
mkdir -p "$MAC_HOME/bin" "$LOG_DIR"

tmp_env="$(mktemp)"
cat > "$tmp_env" <<EOF
QDRANT_IMAGE=${QDRANT_IMAGE}
QDRANT_CONTAINER_NAME=${QDRANT_CONTAINER_NAME}
QDRANT_BIND_ADDR=${QDRANT_BIND_ADDR}
QDRANT_PORT=${QDRANT_PORT}
QDRANT_DATA_DIR=${QDRANT_DATA_DIR}
QDRANT_MEMORY_LIMIT=${QDRANT_MEMORY_LIMIT}
EOF
maybe_sudo install -m 0644 "$tmp_env" "$ENV_DEST"
rm -f "$tmp_env"

set_env_key "${MAC_HOME}/mac.env" QDRANT_URL "$service_url"
set_env_key "${MAC_HOME}/mac.env" QDRANT_ADDRESS "$service_url"
set_env_key "${MAC_HOME}/mac.env" QDRANT_FLEET_URL "$service_url"
set_env_key "${MAC_HOME}/mac.env" MAC_REQUIRE_QDRANT_MEMORY "1"
set_env_key "${MAC_HOME}/mac.env" MAC_QDRANT_MEMORY_ROLE "shared_level2"

write_qdrant_wrapper() {
  local wrapper="$MAC_HOME/bin/mac-qdrant-run"
  if [ "$USE_NATIVE_BINARY" = 1 ]; then
    cat > "$wrapper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a
[ -f ${ENV_DEST} ] && . ${ENV_DEST}
[ -f "\$HOME/.mac/qdrant.env" ] && . "\$HOME/.mac/qdrant.env"
set +a
: "\${QDRANT_BIND_ADDR:=127.0.0.1}"
: "\${QDRANT_PORT:=6333}"
: "\${QDRANT_DATA_DIR:=${QDRANT_DATA_DIR}}"
mkdir -p "\$QDRANT_DATA_DIR"
export QDRANT__SERVICE__HOST="\${QDRANT_BIND_ADDR}"
export QDRANT__SERVICE__HTTP_PORT="\${QDRANT_PORT}"
export QDRANT__STORAGE__STORAGE_PATH="\${QDRANT_DATA_DIR}"
exec ${QDRANT_BINARY}
EOF
  else
    cat > "$wrapper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
# Keep the container runtime's own dir on PATH so its credential helper
# (e.g. docker-credential-desktop) is found under a minimal launchd PATH.
export PATH="$(dirname "$CONTAINER_CMD_ABS"):\$PATH"
set -a
[ -f ${ENV_DEST} ] && . ${ENV_DEST}
[ -f "\$HOME/.mac/qdrant.env" ] && . "\$HOME/.mac/qdrant.env"
set +a
: "\${QDRANT_IMAGE:=docker.io/qdrant/qdrant:latest}"
: "\${QDRANT_CONTAINER_NAME:=${FLEET_NAME}-qdrant}"
: "\${QDRANT_BIND_ADDR:=127.0.0.1}"
: "\${QDRANT_PORT:=6333}"
: "\${QDRANT_DATA_DIR:=${QDRANT_DATA_DIR}}"
: "\${QDRANT_MEMORY_LIMIT:=2g}"
# Qdrant (actix/tokio) sizes its thread pools from /proc/cpuinfo, which on a
# cgroup-CPU-limited pod reports the FULL node core count (e.g. 192), not the
# container's CPU quota. A low pids-limit then makes thread spawn fail with
# EAGAIN ("Cannot spawn Arbiter's thread: WouldBlock") and Qdrant panics at
# startup. Keep a guardrail, but high enough for big nodes; override if needed.
: "\${QDRANT_PIDS_LIMIT:=4096}"
exec ${CONTAINER_CMD_ABS} run --rm --name "\$QDRANT_CONTAINER_NAME" --pull=missing \
  --security-opt=no-new-privileges --pids-limit="\$QDRANT_PIDS_LIMIT" \
  --memory="\$QDRANT_MEMORY_LIMIT" \
  -p "\$QDRANT_BIND_ADDR:\$QDRANT_PORT:6333" \
  -v "\$QDRANT_DATA_DIR:/qdrant/storage" "\$QDRANT_IMAGE"
EOF
  fi
  chmod 700 "$wrapper"
}

case "$SUPERVISOR_KIND" in
  systemd)
    echo "[qdrant] Installing systemd unit"
    unit_tmp="$(mktemp)"
    env_dest_sed="$(printf '%s' "$ENV_DEST" | sed 's/[&|]/\\&/g')"
    sed "s|/etc/mac/qdrant.env|${env_dest_sed}|g" "$UNIT_TEMPLATE" > "$unit_tmp"
    sudo install -m 0644 "$unit_tmp" "$UNIT_DEST"
    rm -f "$unit_tmp"
    sudo systemctl daemon-reload
    sudo systemctl enable "${FLEET_NAME}-qdrant.service" >/dev/null
    echo "[qdrant] Starting ${FLEET_NAME}-qdrant.service"
    sudo systemctl restart "${FLEET_NAME}-qdrant.service"
    ;;
  supervisord)
    echo "[qdrant] Installing supervisord program"
    write_qdrant_wrapper
    conf_dir="$(supervisord_conf_dir)"
    sudo install -d -m 0755 "$conf_dir"
    sudo tee "$conf_dir/${FLEET_NAME}-qdrant.conf" >/dev/null <<EOF
[program:${FLEET_NAME}-qdrant]
command=$MAC_HOME/bin/mac-qdrant-run
directory=$MAC_HOME
user=$USER
autostart=true
autorestart=true
startsecs=5
stopwaitsecs=30
stdout_logfile=$LOG_DIR/mac-qdrant.log
stderr_logfile=$LOG_DIR/mac-qdrant.log
environment=HOME="$HOME"
EOF
    run_supervisorctl reread >/dev/null
    run_supervisorctl update >/dev/null
    run_supervisorctl restart "${FLEET_NAME}-qdrant" >/dev/null 2>&1 || run_supervisorctl start "${FLEET_NAME}-qdrant" >/dev/null
    ;;
  launchd)
    echo "[qdrant] Installing launchd agent"
    write_qdrant_wrapper
    plist="$HOME/Library/LaunchAgents/com.${FLEET_NAME}.qdrant.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.${FLEET_NAME}.qdrant</string>
  <key>ProgramArguments</key>
  <array><string>$MAC_HOME/bin/mac-qdrant-run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$MAC_HOME</string>
  <key>StandardOutPath</key><string>$LOG_DIR/mac-qdrant.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/mac-qdrant.log</string>
</dict>
</plist>
EOF
    if command -v plutil >/dev/null 2>&1; then
      plutil -lint "$plist"
    fi
    uid="$(id -u)"
    launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
    launchctl bootout "gui/$uid/com.${FLEET_NAME}.qdrant" >/dev/null 2>&1 || true
    launchctl enable "gui/$uid/com.${FLEET_NAME}.qdrant"
    if ! launchctl bootstrap "gui/$uid" "$plist"; then
      launchctl kickstart -k "gui/$uid/com.${FLEET_NAME}.qdrant"
    fi
    ;;
esac

health_url="${service_url}/collections"
ready=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if curl -fsS --connect-timeout 2 --max-time 5 "$health_url" >/dev/null 2>&1; then
    echo "[qdrant] Qdrant ready at $health_url"
    ready="yes"
    break
  fi
  sleep 2
done

if [ -n "$ready" ]; then
  # mem-06: provision the mac memory tier collections idempotently.
  # PUT /collections/<name> creates if missing; on a conflict (already
  # present) Qdrant 4xxes which we explicitly tolerate.
  MAC_MEMORY_EMBEDDING_DIM="${MAC_MEMORY_EMBEDDING_DIM:-1536}"
  provision_collection() {
    local name="$1"
    local ef_construct="${2:-100}"
    local existing
    existing="$(curl -fsS --max-time 5 "${service_url}/collections/${name}" 2>/dev/null || true)"
    if [ -n "$existing" ] && echo "$existing" | grep -q '"status":"ok"'; then
      echo "[qdrant] collection ${name} already exists; skipping create"
      return 0
    fi
    local body
    body=$(cat <<JSON
{
  "vectors": {"size": ${MAC_MEMORY_EMBEDDING_DIM}, "distance": "Cosine"},
  "hnsw_config": {"m": 16, "ef_construct": ${ef_construct}},
  "optimizers_config": {"indexing_threshold": 20000}
}
JSON
)
    if curl -fsS --max-time 10 -X PUT -H "Content-Type: application/json" \
       -d "$body" "${service_url}/collections/${name}" >/dev/null 2>&1; then
      echo "[qdrant] collection ${name} created (dim=${MAC_MEMORY_EMBEDDING_DIM})"
    else
      echo "[qdrant] WARN: failed to create collection ${name}; see Qdrant logs" >&2
    fi
  }
  provision_collection mac_memory_medium 100
  provision_collection mac_memory_long   200
  exit 0
fi

echo "[qdrant] ERROR: Qdrant did not become ready at $health_url" >&2
case "$SUPERVISOR_KIND" in
  systemd) systemctl status "${FLEET_NAME}-qdrant.service" --no-pager -n 40 >&2 || true ;;
  supervisord) supervisorctl status "${FLEET_NAME}-qdrant" >&2 || true ;;
  launchd) launchctl list "com.${FLEET_NAME}.qdrant" >&2 || true ;;
esac
exit 1
