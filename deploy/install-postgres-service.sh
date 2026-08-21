#!/usr/bin/env bash
# install-postgres-service.sh - install/start the mac control-plane PostgreSQL
# database for a hub node.
#
# PostgreSQL is the hub's ledger authority (src/mac/store.py accepts only
# postgres:// / postgresql:// DSNs). Bind to loopback only -- this database is
# never meant to be reachable off-box; remote agents talk to the hub over its
# HTTP API, not directly to the database.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -P -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
launchd_lib="$SCRIPT_DIR/lib/launchd-lifecycle.sh"
if [ ! -r "$launchd_lib" ]; then
  echo "[postgres] ERROR: launchd lifecycle helper is missing: $launchd_lib" >&2
  exit 1
fi
MAC_LAUNCHD_LOG_PREFIX="[postgres]"
# shellcheck source=lib/launchd-lifecycle.sh
. "$launchd_lib"
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
WORKSPACE="${WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
FLEET_NAME="${FLEET_NAME:-mac}"
UNIT_TEMPLATE="${WORKSPACE}/deploy/systemd/mac-postgres.service"
UNIT_DEST="/etc/systemd/system/${FLEET_NAME}-postgres.service"
SUPERVISOR_KIND="${POSTGRES_SUPERVISOR:-${MAC_SUPERVISOR_KIND:-auto}}"

# Platform handling mirrors install-qdrant-service.sh: macOS has no /etc
# service-config tree and no passwordless sudo over a non-interactive deploy,
# so service env files live under $MAC_HOME; Docker Desktop is not on the
# launchd/ssh PATH by default, so add it explicitly. Linux is unchanged.
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
ENV_DEST="$ENV_CONF_DIR/postgres.env"

maybe_sudo() {
  if [ "$OS_NAME" = "Darwin" ]; then
    "$@"
  else
    sudo "$@"
  fi
}

POSTGRES_IMAGE="${POSTGRES_IMAGE:-docker.io/library/postgres:17}"
POSTGRES_BIND_ADDR="${POSTGRES_BIND_ADDR:-127.0.0.1}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
if [ "$OS_NAME" = "Darwin" ]; then
  POSTGRES_DATA_DIR="${POSTGRES_DATA_DIR:-$MAC_HOME/postgres}"
else
  POSTGRES_DATA_DIR="${POSTGRES_DATA_DIR:-/var/lib/${FLEET_NAME}/postgres}"
fi
POSTGRES_DB="${POSTGRES_DB:-mac}"
POSTGRES_USER="${POSTGRES_USER:-mac}"
POSTGRES_CONTAINER_NAME="${POSTGRES_CONTAINER_NAME:-${FLEET_NAME}-postgres}"
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
      echo "[postgres] ERROR: unsupported supervisor: $SUPERVISOR_KIND" >&2
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
  echo "[postgres] ERROR: could not detect systemd, launchd, or supervisord" >&2
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
  echo "[postgres] ERROR: cannot locate $UNIT_TEMPLATE" >&2
  exit 1
fi

case "$POSTGRES_BIND_ADDR" in
  0.0.0.0|::|\[::\])
    echo "[postgres] ERROR: refusing unsafe all-interface bind address: $POSTGRES_BIND_ADDR" >&2
    exit 1
    ;;
esac

CONTAINER_CMD=""
CONTAINER_RUNTIME_PATHS=()
CONTAINER_RUNTIME_PATH_COUNT=0

_container_works() {
  local info=""
  command -v "$1" >/dev/null 2>&1 || return 1
  info="$(mac_run_bounded \
    "${MAC_POSTGRES_RUNTIME_COMMAND_TIMEOUT_SECONDS:-10}" \
    "$1" info 2>/dev/null)" || return 1
  # podman rootless without cgroup delegation can't start containers (empty
  # cgroupControllers). Docker (Engine or Desktop) manages cgroups inside its
  # own daemon/VM and never prints that podman-specific field, so a successful
  # `docker info` is sufficient.
  if [ "$1" = "podman" ]; then
    printf '%s\n' "$info" | grep -qE 'cgroupControllers:.*\S'
  fi
}

remember_container_runtime() {
  local runtime="$1" existing="" index=0
  while [ "$index" -lt "$CONTAINER_RUNTIME_PATH_COUNT" ]; do
    existing="${CONTAINER_RUNTIME_PATHS[$index]}"
    [ "$existing" = "$runtime" ] && return 0
    index=$(( index + 1 ))
  done
  CONTAINER_RUNTIME_PATHS[$CONTAINER_RUNTIME_PATH_COUNT]="$runtime"
  CONTAINER_RUNTIME_PATH_COUNT=$(( CONTAINER_RUNTIME_PATH_COUNT + 1 ))
}

for _candidate in podman docker; do
  if command -v "$_candidate" >/dev/null 2>&1 && _container_works "$_candidate"; then
    remember_container_runtime "$(command -v "$_candidate")"
    [ -n "$CONTAINER_CMD" ] || CONTAINER_CMD="$_candidate"
  fi
done
if [ -z "$CONTAINER_CMD" ] && command -v apt-get >/dev/null 2>&1; then
  echo "[postgres] no working container runtime; trying apt-get install podman"
  sudo apt-get install -y podman >/dev/null 2>&1 || true
  if command -v podman >/dev/null 2>&1 && _container_works podman; then
    CONTAINER_CMD="podman"
    remember_container_runtime "$(command -v podman)"
  fi
fi
if [ -z "$CONTAINER_CMD" ] && [ "$OS_NAME" = "Darwin" ]; then
  echo "[postgres] ERROR: Docker is required on macOS but no working 'docker' was found." >&2
  echo "[postgres] Start Docker Desktop so 'docker info' succeeds, then re-run." >&2
  exit 1
fi

# Some sandboxed hosts (e.g. GKE pods with no /dev/net/tun and no NET_ADMIN)
# have a container runtime binary that reports success on `info` but cannot
# actually start a container's network namespace -- podman's rootless
# slirp4netns needs /dev/net/tun, and even --network=host fails there
# ("mount `proc`... OCI permission denied") because the pod itself has no
# CAP_SYS_ADMIN for a nested mount namespace. Unlike Qdrant (a single
# downloadable binary), Postgres has no simple prebuilt tarball, but Debian/
# Ubuntu ship it as a first-class apt package with its own systemd unit and
# supervision -- use that as the fallback instead of failing the whole
# control-plane deploy over a container runtime the sandbox cannot use.
if [ -z "$CONTAINER_CMD" ] && [ "$OS_NAME" = "Linux" ] && command -v apt-get >/dev/null 2>&1; then
  echo "[postgres] no working container runtime; falling back to the native apt postgresql package"
  # DEBIAN_FRONTEND=noninteractive is required, not cosmetic: postgresql pulls
  # in tzdata, whose postinst prompts for a timezone via debconf. Without a
  # TTY and without this, apt-get hangs indefinitely on that prompt instead
  # of failing -- it doesn't even return a non-zero exit code to notice.
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql >/dev/null 2>&1 || true
  if command -v pg_lsclusters >/dev/null 2>&1; then
    USE_NATIVE_PACKAGE=1
  fi
fi

if [ "${USE_NATIVE_PACKAGE:-0}" != 1 ] && [ -z "$CONTAINER_CMD" ]; then
  echo "[postgres] ERROR: no container runtime (podman/docker) and no native postgresql package available" >&2
  exit 1
fi

# Absolute path to the container runtime so the launchd/systemd wrapper finds
# it under a minimal PATH (Docker Desktop lives outside the default PATH).
CONTAINER_CMD_ABS="$(command -v "$CONTAINER_CMD" 2>/dev/null || printf '%s' "$CONTAINER_CMD")"

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

get_env_key() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 0
  # grep exits 1 on no match (the normal, expected case on a first-ever run,
  # before any password has been written yet) -- under set -o pipefail that
  # failure propagates through the pipeline and this function's return
  # status, and `var="$(get_env_key ...)"` at the call site is a bare
  # command-substitution assignment, which set -e treats as fatal. Swallow
  # the no-match case explicitly so "not found yet" behaves as "empty", not
  # as a silent script abort.
  grep "^${key}=" "$file" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

# The database volume bakes in whatever password created it -- restarting
# with a different password locks the deploy out of its own database. Reuse
# a password already on disk (env file, then mac.env) before generating one.
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
if [ -z "$POSTGRES_PASSWORD" ]; then
  POSTGRES_PASSWORD="$(get_env_key "$ENV_DEST" POSTGRES_PASSWORD)"
fi
if [ -z "$POSTGRES_PASSWORD" ]; then
  POSTGRES_PASSWORD="$(get_env_key "${MAC_HOME}/mac.env" MAC_CONTROL_PLANE_DB_PASSWORD)"
fi
if [ -z "$POSTGRES_PASSWORD" ]; then
  if command -v openssl >/dev/null 2>&1; then
    POSTGRES_PASSWORD="$(openssl rand -hex 24)"
  else
    POSTGRES_PASSWORD="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
  echo "[postgres] generated a new control-plane database password"
fi

dsn="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_BIND_ADDR}:${POSTGRES_PORT}/${POSTGRES_DB}"

if [ "${USE_NATIVE_PACKAGE:-0}" = 1 ]; then
  # apt's postgresql package already owns supervision (its own systemd unit,
  # started and enabled on install) -- there is nothing for this script to
  # wrap. Configure the role/database/network binding on the already-running
  # cluster instead of installing a competing service definition.
  cluster="$(pg_lsclusters -h | awk '{print $1"/"$2; exit}')"
  [ -n "$cluster" ] || { echo "[postgres] ERROR: apt installed postgresql but no cluster was created" >&2; exit 1; }
  version="${cluster%%/*}"
  cluster_name="${cluster#*/}"
  hba_file="/etc/postgresql/${version}/${cluster_name}/pg_hba.conf"
  conf_file="/etc/postgresql/${version}/${cluster_name}/postgresql.conf"

  sudo pg_ctlcluster "$version" "$cluster_name" start 2>/dev/null || true

  # Bind loopback only -- same posture as the container path, and this is
  # apt's own default (listen_addresses='localhost'); pin it explicitly so a
  # prior local override can't leave it exposed.
  sudo sed -i "s/^#*listen_addresses.*/listen_addresses = '${POSTGRES_BIND_ADDR}'/" "$conf_file"
  if ! sudo grep -q "^host\s\+${POSTGRES_DB}\s\+${POSTGRES_USER}\s\+${POSTGRES_BIND_ADDR}/32\s\+scram-sha-256" "$hba_file" 2>/dev/null; then
    printf 'host %s %s %s/32 scram-sha-256\n' "$POSTGRES_DB" "$POSTGRES_USER" "$POSTGRES_BIND_ADDR" \
      | sudo tee -a "$hba_file" >/dev/null
  fi
  sudo pg_ctlcluster "$version" "$cluster_name" restart

  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sudo -u postgres pg_isready -q && break
    sleep 2
  done

  role_exists="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${POSTGRES_USER}'")"
  if [ "$role_exists" = "1" ]; then
    sudo -u postgres psql -c "ALTER ROLE ${POSTGRES_USER} WITH LOGIN PASSWORD '${POSTGRES_PASSWORD}'" >/dev/null
  else
    sudo -u postgres psql -c "CREATE ROLE ${POSTGRES_USER} WITH LOGIN PASSWORD '${POSTGRES_PASSWORD}'" >/dev/null
  fi
  db_exists="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${POSTGRES_DB}'")"
  if [ "$db_exists" != "1" ]; then
    sudo -u postgres psql -c "CREATE DATABASE ${POSTGRES_DB} OWNER ${POSTGRES_USER}" >/dev/null
  fi

  if ! sudo -u postgres pg_isready -q; then
    echo "[postgres] ERROR: native postgresql cluster did not become ready" >&2
    sudo pg_ctlcluster "$version" "$cluster_name" status >&2 || true
    exit 1
  fi
  echo "[postgres] PostgreSQL ready (native apt package) at ${POSTGRES_BIND_ADDR}:${POSTGRES_PORT}"

  maybe_sudo install -d -m 0755 "$ENV_CONF_DIR"
  tmp_env="$(mktemp)"
  cat > "$tmp_env" <<EOF
POSTGRES_BIND_ADDR=${POSTGRES_BIND_ADDR}
POSTGRES_PORT=${POSTGRES_PORT}
POSTGRES_DB=${POSTGRES_DB}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
EOF
  maybe_sudo install -m 0600 "$tmp_env" "$ENV_DEST"
  rm -f "$tmp_env"

  set_env_key "${MAC_HOME}/mac.env" MAC_CONTROL_PLANE_DB_PASSWORD "$POSTGRES_PASSWORD"
  set_env_key "${MAC_HOME}/mac.env" MAC_DATABASE_URL "$dsn"
  if [ -n "${POSTGRES_DSN_OUT_FILE:-}" ]; then
    printf '%s' "$dsn" > "$POSTGRES_DSN_OUT_FILE"
  fi
  exit 0
fi

echo "[postgres] Installing PostgreSQL under ${SUPERVISOR_KIND}"
echo "[postgres] Binding PostgreSQL to ${POSTGRES_BIND_ADDR}:${POSTGRES_PORT}"
maybe_sudo install -d -m 0755 "$ENV_CONF_DIR"
maybe_sudo install -d -m 0750 "$POSTGRES_DATA_DIR"
maybe_sudo chown "$USER" "$POSTGRES_DATA_DIR" || true
mkdir -p "$MAC_HOME/bin" "$LOG_DIR"

tmp_env="$(mktemp)"
cat > "$tmp_env" <<EOF
POSTGRES_IMAGE=${POSTGRES_IMAGE}
POSTGRES_CONTAINER_NAME=${POSTGRES_CONTAINER_NAME}
POSTGRES_BIND_ADDR=${POSTGRES_BIND_ADDR}
POSTGRES_PORT=${POSTGRES_PORT}
POSTGRES_DATA_DIR=${POSTGRES_DATA_DIR}
POSTGRES_DB=${POSTGRES_DB}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
EOF
maybe_sudo install -m 0600 "$tmp_env" "$ENV_DEST"
rm -f "$tmp_env"

# Persisted twice: MAC_CONTROL_PLANE_DB_PASSWORD is this script's own source
# of truth for "reuse the existing password" on the next run (mac.env is
# rewritten wholesale by `mac.deploy_env write-mac-env`, which does not know
# about this password and would otherwise drop it); MAC_DATABASE_URL is what
# the mac CLI/store actually read to reach the database.
set_env_key "${MAC_HOME}/mac.env" MAC_CONTROL_PLANE_DB_PASSWORD "$POSTGRES_PASSWORD"
set_env_key "${MAC_HOME}/mac.env" MAC_DATABASE_URL "$dsn"

# The caller may be about to (re)write mac.env itself (e.g. the deploy's
# "creating/updating mac environment file" step, which does not know about
# this password) -- hand the DSN back directly rather than making the caller
# re-derive our password-lookup precedence.
if [ -n "${POSTGRES_DSN_OUT_FILE:-}" ]; then
  printf '%s' "$dsn" > "$POSTGRES_DSN_OUT_FILE"
fi

write_postgres_wrapper() {
  local wrapper="${1:-$MAC_HOME/bin/mac-postgres-run}"
  cat > "$wrapper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
# Keep the container runtime's own dir on PATH so its credential helper
# (e.g. docker-credential-desktop) is found under a minimal launchd PATH.
export PATH="$(dirname "$CONTAINER_CMD_ABS"):\$PATH"
set -a
[ -f ${ENV_DEST} ] && . ${ENV_DEST}
set +a
: "\${POSTGRES_IMAGE:=docker.io/library/postgres:17}"
: "\${POSTGRES_CONTAINER_NAME:=${FLEET_NAME}-postgres}"
: "\${POSTGRES_BIND_ADDR:=127.0.0.1}"
: "\${POSTGRES_PORT:=5432}"
: "\${POSTGRES_DATA_DIR:=${POSTGRES_DATA_DIR}}"
: "\${POSTGRES_DB:=mac}"
: "\${POSTGRES_USER:=mac}"
exec ${CONTAINER_CMD_ABS} run --rm --name "\$POSTGRES_CONTAINER_NAME" --pull=missing \
  --security-opt=no-new-privileges \
  -e POSTGRES_DB="\$POSTGRES_DB" -e POSTGRES_USER="\$POSTGRES_USER" \
  -e POSTGRES_PASSWORD="\$POSTGRES_PASSWORD" \
  -p "\$POSTGRES_BIND_ADDR:\$POSTGRES_PORT:5432" \
  -v "\$POSTGRES_DATA_DIR:/var/lib/postgresql/data" "\$POSTGRES_IMAGE"
EOF
  chmod 700 "$wrapper"
}

postgres_container_is_present() {
  local runtime="$1" output="" rc=0 container_name="" matches=0
  output="$(mac_run_bounded \
    "${MAC_POSTGRES_RUNTIME_COMMAND_TIMEOUT_SECONDS:-10}" \
    "$runtime" ps -a --format '{{.Names}}' 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "[postgres] ERROR: could not inspect the managed container (exit $rc): $output" >&2
    return 2
  fi
  while IFS= read -r container_name; do
    [ "$container_name" = "$POSTGRES_CONTAINER_NAME" ] \
      && matches=$(( matches + 1 ))
  done <<EOF
$output
EOF
  case "$matches" in
    0) return 1 ;;
    1) return 0 ;;
    *)
      echo "[postgres] ERROR: container lookup returned duplicate exact names" >&2
      return 2
      ;;
  esac
}

stop_postgres_container_for_runtime() {
  local runtime="$1" probe_rc=0 remove_output="" remove_rc=0
  postgres_container_is_present "$runtime" || probe_rc=$?
  case "$probe_rc" in
    0) ;;
    1) return 0 ;;
    *) return "$probe_rc" ;;
  esac
  remove_output="$(mac_run_bounded \
    "${MAC_POSTGRES_RUNTIME_COMMAND_TIMEOUT_SECONDS:-10}" \
    "$runtime" rm -f "$POSTGRES_CONTAINER_NAME" 2>&1)" \
    || remove_rc=$?
  if [ "$remove_rc" -ne 0 ]; then
    echo "[postgres] ERROR: could not retire the managed container (exit $remove_rc): $remove_output" >&2
    return 1
  fi
  probe_rc=0
  postgres_container_is_present "$runtime" || probe_rc=$?
  case "$probe_rc" in
    1) return 0 ;;
    0)
      echo "[postgres] ERROR: managed container remained after removal: $POSTGRES_CONTAINER_NAME" >&2
      return 1
      ;;
    *) return "$probe_rc" ;;
  esac
}

stop_postgres_container_if_present() {
  local runtime="" index=0
  while [ "$index" -lt "$CONTAINER_RUNTIME_PATH_COUNT" ]; do
    runtime="${CONTAINER_RUNTIME_PATHS[$index]}"
    stop_postgres_container_for_runtime "$runtime"
    index=$(( index + 1 ))
  done
}

case "$SUPERVISOR_KIND" in
  systemd)
    echo "[postgres] Installing systemd unit"
    unit_tmp="$(mktemp)"
    env_dest_sed="$(printf '%s' "$ENV_DEST" | sed 's/[&|]/\\&/g')"
    sed "s|/etc/mac/postgres.env|${env_dest_sed}|g" "$UNIT_TEMPLATE" > "$unit_tmp"
    sudo install -m 0644 "$unit_tmp" "$UNIT_DEST"
    rm -f "$unit_tmp"
    sudo systemctl daemon-reload
    sudo systemctl enable "${FLEET_NAME}-postgres.service" >/dev/null
    echo "[postgres] Starting ${FLEET_NAME}-postgres.service"
    sudo systemctl restart "${FLEET_NAME}-postgres.service"
    ;;
  supervisord)
    echo "[postgres] Installing supervisord program"
    write_postgres_wrapper
    conf_dir="$(supervisord_conf_dir)"
    sudo install -d -m 0755 "$conf_dir"
    sudo tee "$conf_dir/${FLEET_NAME}-postgres.conf" >/dev/null <<EOF
[program:${FLEET_NAME}-postgres]
command=$MAC_HOME/bin/mac-postgres-run
directory=$MAC_HOME
user=$USER
autostart=true
autorestart=true
startsecs=5
stopwaitsecs=30
stdout_logfile=$LOG_DIR/mac-postgres.log
stderr_logfile=$LOG_DIR/mac-postgres.log
environment=HOME="$HOME"
EOF
    run_supervisorctl reread >/dev/null
    run_supervisorctl update >/dev/null
    run_supervisorctl restart "${FLEET_NAME}-postgres" >/dev/null 2>&1 || run_supervisorctl start "${FLEET_NAME}-postgres" >/dev/null
    ;;
  launchd)
    echo "[postgres] Installing launchd agent"
    uid="$(id -u)"
    launchd_domain="gui/$uid"
    launchd_label="com.${FLEET_NAME}.postgres"
    launchd_target="$launchd_domain/$launchd_label"
    plist="$HOME/Library/LaunchAgents/${launchd_label}.plist"
    wrapper="$MAC_HOME/bin/mac-postgres-run"
    mkdir -p "$HOME/Library/LaunchAgents"
    mac_launchd_transaction_begin \
      "$launchd_domain" "$plist" "$launchd_target" "$launchd_label"
    mac_launchd_transaction_track_file "$wrapper"
    mac_launchd_transaction_set_rollback_hook stop_postgres_container_if_present
    tmp_wrapper="$(mktemp "$MAC_HOME/bin/.mac-postgres-run.XXXXXX")"
    mac_launchd_transaction_track_temporary "$tmp_wrapper"
    write_postgres_wrapper "$tmp_wrapper"
    /bin/bash -n "$tmp_wrapper"
    tmp_plist="$(mktemp "$HOME/Library/LaunchAgents/.${launchd_label}.XXXXXX")"
    mac_launchd_transaction_track_temporary "$tmp_plist"
    cat > "$tmp_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.${FLEET_NAME}.postgres</string>
  <key>ProgramArguments</key>
  <array><string>$MAC_HOME/bin/mac-postgres-run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$MAC_HOME</string>
  <key>StandardOutPath</key><string>$LOG_DIR/mac-postgres.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/mac-postgres.log</string>
</dict>
</plist>
EOF
    if command -v plutil >/dev/null 2>&1; then
      plutil -lint "$tmp_plist"
    fi
    mac_launchd_transaction_mark_mutating
    mac_launchd_stop_job_if_present "$launchd_target" "$launchd_label"
    stop_postgres_container_if_present
    mac_launchd_transaction_replace "$tmp_wrapper" "$wrapper"
    mac_launchd_transaction_replace "$tmp_plist" "$plist"
    mac_launchd_bootstrap_job \
      "$launchd_domain" "$plist" "$launchd_target" "$launchd_label"
    ;;
esac

ready=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if "$CONTAINER_CMD" exec "$POSTGRES_CONTAINER_NAME" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    echo "[postgres] PostgreSQL ready at ${POSTGRES_BIND_ADDR}:${POSTGRES_PORT}"
    ready="yes"
    break
  fi
  sleep 2
done

if [ -n "$ready" ]; then
  if [ "$SUPERVISOR_KIND" = launchd ]; then
    mac_launchd_transaction_commit
  fi
  exit 0
fi

echo "[postgres] ERROR: PostgreSQL did not become ready" >&2
case "$SUPERVISOR_KIND" in
  systemd) systemctl status "${FLEET_NAME}-postgres.service" --no-pager -n 40 >&2 || true ;;
  supervisord) supervisorctl status "${FLEET_NAME}-postgres" >&2 || true ;;
  launchd)
    mac_run_bounded 5 launchctl print "$launchd_target" >&2 || true
    ;;
esac
"$CONTAINER_CMD" logs "$POSTGRES_CONTAINER_NAME" 2>&1 | tail -n 40 >&2 || true
exit 1
