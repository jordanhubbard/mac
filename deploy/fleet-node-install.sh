#!/bin/bash
set -euo pipefail

AGENT="${MAC_DEPLOY_AGENT:?}"
FLEET_NAME="${MAC_DEPLOY_FLEET_NAME:-mac}"
OS_KIND="${MAC_DEPLOY_OS:?}"
ARCHIVE="${MAC_DEPLOY_ARCHIVE:?}"
FLEET_REGISTRY_FILE="${MAC_DEPLOY_FLEET_REGISTRY_FILE:-}"
CONFIGURED_AGENT_IDS="${MAC_DEPLOY_CONFIGURED_AGENT_IDS:-}"
DEPLOY_TS="${MAC_DEPLOY_TS:?}"
DEPLOY_REV="${MAC_DEPLOY_GIT_REV:?}"
DEPLOY_GENERATION="${MAC_DEPLOY_GENERATION:-${DEPLOY_REV}:${AGENT}:${DEPLOY_TS}}"
export MAC_DEPLOY_GENERATION="$DEPLOY_GENERATION"
NODE_ACTION="${1:-${MAC_DEPLOY_NODE_ACTION:-legacy-one-shot}}"
case "$NODE_ACTION" in
  arm-phase2|apply-phase2|rollback-phase2|finalize|legacy-one-shot) ;;
  *)
    printf '%s\n' \
      "usage: $0 [arm-phase2|apply-phase2|rollback-phase2|finalize]" >&2
    exit 64
    ;;
esac

# The outer controller acquires this fence before it copies or mutates any
# managed state.  Re-check it in-band before this transaction's first write,
# then renew it throughout long package/image installs.  A controller whose
# lock is replaced must not continue writing merely because its SSH session is
# still alive.
DEPLOY_LOCK_DIR="${MAC_HOME:-$HOME/.mac}/deploy-controller.lock"
deployment_lock_assert_and_renew() {
  MAC_DEPLOY_LOCK_DIR="$DEPLOY_LOCK_DIR" \
    MAC_DEPLOY_LOCK_ID="$DEPLOY_GENERATION" python3 - <<'PY'
import json
import os
import tempfile
import time
import fcntl
from pathlib import Path

lock = Path(os.environ["MAC_DEPLOY_LOCK_DIR"])
owner = lock / "owner.json"
guard_path = lock.parent / "deploy-controller.guard"
inherited_guard = os.environ.get("MAC_DEPLOY_LOCK_GUARD_FD", "").strip()
opened_guard = not inherited_guard
if inherited_guard:
    try:
        guard_fd = int(inherited_guard)
        os.fstat(guard_fd)
    except (OSError, ValueError) as exc:
        raise SystemExit("inherited deployment guard is invalid") from exc
else:
    guard_fd = os.open(str(guard_path), os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(guard_fd, 0o600)
    fcntl.flock(guard_fd, fcntl.LOCK_EX)
try:
    try:
        payload = json.loads(owner.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit("deployment lock is unreadable: %s" % type(exc).__name__)
    if payload.get("deployment_id") != os.environ["MAC_DEPLOY_LOCK_ID"]:
        raise SystemExit("deployment lock fence no longer belongs to this transaction")
    payload["renewed_at_epoch"] = time.time()
    fd, raw = tempfile.mkstemp(prefix="owner.json.", dir=str(lock))
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, owner)
        directory_fd = os.open(lock, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        tmp.unlink(missing_ok=True)
finally:
    if opened_guard:
        fcntl.flock(guard_fd, fcntl.LOCK_UN)
        os.close(guard_fd)
PY
}

deployment_lock_assert_and_renew
DEPLOY_LOCK_RENEW_PID=""
DEPLOY_ROLLBACK_ARMED=0
DEPLOY_COMPLETED=0
DEPLOY_ROLLBACK_IN_PROGRESS=0
deployment_lock_renewer() {
  local controller_pid="$1"
  while sleep "${MAC_DEPLOY_LOCK_RENEW_SECONDS:-20}"; do
    if ! deployment_lock_assert_and_renew; then
      echo "deployment lock ownership was lost; terminating fenced install" >&2
      kill -TERM "$controller_pid" 2>/dev/null || true
      return 1
    fi
  done
}
deployment_lock_renewer "$$" &
DEPLOY_LOCK_RENEW_PID="$!"
stop_deployment_lock_renewer() {
  if [ -n "${DEPLOY_LOCK_RENEW_PID:-}" ]; then
    kill "$DEPLOY_LOCK_RENEW_PID" 2>/dev/null || true
    wait "$DEPLOY_LOCK_RENEW_PID" 2>/dev/null || true
    DEPLOY_LOCK_RENEW_PID=""
  fi
}

deployment_exit_handler() {
  local original_rc="${1:-1}" rollback_rc=0
  trap - EXIT HUP INT TERM
  if [ "$original_rc" -ne 0 ] \
    && [ "${DEPLOY_ROLLBACK_ARMED:-0}" = 1 ] \
    && [ "${DEPLOY_COMPLETED:-0}" != 1 ] \
    && [ "${DEPLOY_ROLLBACK_IN_PROGRESS:-0}" != 1 ]; then
    DEPLOY_ROLLBACK_IN_PROGRESS=1
    if deployment_lock_assert_and_renew; then
      echo "deployment failed after phase-2 mutation; restoring the prior generation" >&2
      if [ -x "${ROLLBACK_SCRIPT:-}" ] && [ ! -L "${ROLLBACK_SCRIPT:-}" ]; then
        "${ROLLBACK_SCRIPT}" || rollback_rc=$?
      else
        echo "automatic rollback failed: rollback program is unavailable" >&2
        rollback_rc=1
      fi
    else
      echo "automatic rollback refused: deployment fence is no longer owned" >&2
      rollback_rc=1
    fi
    if [ "$rollback_rc" -ne 0 ]; then
      echo "automatic rollback did not restore the prior generation" >&2
    fi
  fi
  stop_deployment_lock_renewer
  exit "$original_rc"
}
trap 'deployment_exit_handler "$?"' EXIT

DEPLOY_GIT_URL="${MAC_DEPLOY_GIT_URL:-}"
DEPLOY_GIT_BRANCH="${MAC_DEPLOY_GIT_BRANCH:-main}"
HERMES_SLACK_HOME_CHANNEL_NAME="${MAC_DEPLOY_HERMES_SLACK_HOME_CHANNEL_NAME:-}"
HERMES_GATEWAY_MODEL="${MAC_DEPLOY_HERMES_GATEWAY_MODEL:-}"
if [ "$HERMES_GATEWAY_MODEL" = "*" ]; then
  HERMES_GATEWAY_MODEL=""
fi
HERMES_GATEWAY_PROVIDER="${MAC_DEPLOY_HERMES_GATEWAY_PROVIDER:-custom}"
HERMES_GATEWAY_BASE_URL="${MAC_DEPLOY_HERMES_GATEWAY_BASE_URL:-}"
HERMES_SURFACE_B64="${MAC_DEPLOY_HERMES_SURFACE_B64:-}"
# gateway_impl: which chat-gateway service to install.
#   openclaw — stock OpenClaw inside a MAC-authored OpenShell policy
#   hermes   — vendored Hermes gateway (rollback path)
#   nemoclaw — retained reference/compatibility path
# Decoded from the hermes_surface_b64 payload; also injectable via env.
HERMES_GATEWAY_IMPL="${MAC_DEPLOY_HERMES_GATEWAY_IMPL:-$(
  if [ -n "${MAC_DEPLOY_HERMES_SURFACE_B64:-}" ]; then
    python3 -c "
import base64, json, sys
try:
    p = json.loads(base64.b64decode(sys.argv[1]))
    print(p.get('runtime', {}).get('gateway_impl', 'hermes'))
except Exception:
    print('hermes')
" "${MAC_DEPLOY_HERMES_SURFACE_B64}" 2>/dev/null || echo "hermes"
  else
    echo "hermes"
  fi
)}"
openclaw_runtime_value() {
  local key="$1" fallback="${2:-}"
  if [ -z "${HERMES_SURFACE_B64:-}" ]; then
    printf '%s\n' "$fallback"
    return
  fi
  python3 -c '
import base64, json, sys
try:
    payload = json.loads(base64.b64decode(sys.argv[1]))
    print(payload.get("runtime", {}).get(sys.argv[2], sys.argv[3]))
except Exception:
    print(sys.argv[3])
' "$HERMES_SURFACE_B64" "$key" "$fallback" 2>/dev/null || printf '%s\n' "$fallback"
}
OPENCLAW_PUBLIC_IDENTITY="${MAC_DEPLOY_OPENCLAW_PUBLIC_IDENTITY:-$(openclaw_runtime_value public_identity)}"
OPENCLAW_REPRESENTED_BY="${MAC_DEPLOY_OPENCLAW_REPRESENTED_BY:-$(openclaw_runtime_value represented_by)}"
OPENCLAW_REPRESENTATION_MODE="${MAC_DEPLOY_OPENCLAW_REPRESENTATION_MODE:-$(openclaw_runtime_value representation_mode delegated)}"
OPENCLAW_SLACK_ACCOUNT_ID="${MAC_DEPLOY_OPENCLAW_SLACK_ACCOUNT_ID:-$(openclaw_runtime_value slack_account_id default)}"
OPENCLAW_TELEGRAM_ACCOUNT_ID="${MAC_DEPLOY_OPENCLAW_TELEGRAM_ACCOUNT_ID:-$(openclaw_runtime_value telegram_account_id default)}"
HUB_URL="${MAC_DEPLOY_HUB_URL:-http://127.0.0.1:8789}"
HUB_TOKEN="${MAC_DEPLOY_HUB_TOKEN:-}"
CONTROL_BIND_HOST="${MAC_DEPLOY_CONTROL_BIND_HOST:-127.0.0.1}"
WORKER_MODE="${MAC_DEPLOY_WORKER_MODE:-heartbeat}"
WORKER_CAPABILITIES="${MAC_DEPLOY_WORKER_CAPABILITIES:-ops,python,openclaw,review,api,architecture,cli,docs,security,testing,typescript,ui,web_search,web_extract,web_crawl,firecrawl,work_package_v1}"
WORKER_ALLOWED_PROJECTS="${MAC_DEPLOY_WORKER_ALLOWED_PROJECTS:-}"
WORKER_REQUIRED_METADATA="${MAC_DEPLOY_WORKER_REQUIRED_METADATA:-}"
WORKER_REQUIRE_CANARY="${MAC_DEPLOY_WORKER_REQUIRE_CANARY:-1}"
GITHUB_CREDENTIALS_REQUIRED="${MAC_DEPLOY_GITHUB_CREDENTIALS_REQUIRED:-0}"
SUPERVISOR_REQUESTED="${MAC_DEPLOY_SUPERVISOR:-auto}"
SHARED_SERVICES_MANAGER_AGENT="${MAC_DEPLOY_SHARED_SERVICES_MANAGER_AGENT:-$AGENT}"
QDRANT_URL_CONFIGURED="${MAC_DEPLOY_QDRANT_URL:-}"
QDRANT_INSTALL="${MAC_DEPLOY_QDRANT_INSTALL:-auto}"
QDRANT_REQUIRE="1"
QDRANT_BIND_ADDR_CONFIGURED="${MAC_DEPLOY_QDRANT_BIND_ADDR:-}"
QDRANT_PORT_CONFIGURED="${MAC_DEPLOY_QDRANT_PORT:-6333}"
QDRANT_IMAGE_CONFIGURED="${MAC_DEPLOY_QDRANT_IMAGE:-docker.io/qdrant/qdrant:latest}"
QDRANT_MEMORY_LIMIT_CONFIGURED="${MAC_DEPLOY_QDRANT_MEMORY_LIMIT:-2g}"
FIRECRAWL_URL_CONFIGURED="${MAC_DEPLOY_FIRECRAWL_URL:-}"
FIRECRAWL_INSTALL="${MAC_DEPLOY_FIRECRAWL_INSTALL:-auto}"
FIRECRAWL_REQUIRE="1"
FIRECRAWL_BIND_ADDR_CONFIGURED="${MAC_DEPLOY_FIRECRAWL_BIND_ADDR:-}"
FIRECRAWL_PORT_CONFIGURED="${MAC_DEPLOY_FIRECRAWL_PORT:-3002}"
WEBDAV_ENABLED="${MAC_DEPLOY_WEBDAV_ENABLED:-0}"
WEBDAV_URL_CONFIGURED="${MAC_DEPLOY_WEBDAV_URL:-${MAC_DEPLOY_WEBDAV_PUBLIC_URL:-}}"
WEBDAV_INSTALL="${MAC_DEPLOY_WEBDAV_INSTALL:-auto}"
WEBDAV_BIND_ADDR_CONFIGURED="${MAC_DEPLOY_WEBDAV_BIND_ADDR:-0.0.0.0}"
WEBDAV_PORT_CONFIGURED="${MAC_DEPLOY_WEBDAV_PORT:-80}"
WEBDAV_ROOT_CONFIGURED="${MAC_DEPLOY_WEBDAV_ROOT:-}"
WEBDAV_PUBLIC_PATH_CONFIGURED="${MAC_DEPLOY_WEBDAV_PUBLIC_PATH:-/artifacts/}"
WEBDAV_MAX_UPLOAD_BYTES_CONFIGURED="${MAC_DEPLOY_WEBDAV_MAX_UPLOAD_BYTES:-536870912}"
NETWORK_PROVIDER="${MAC_DEPLOY_NETWORK_PROVIDER:-tailscale}"
# The outer orchestrator may have proved that this spoke can reach the hub URL
# directly. There is no localhost reverse-tunnel control-plane forward on that
# path, regardless of whether the route is a mesh client or another reachable
# private network.
DEPLOY_DIRECT_HUB="${MAC_DEPLOY_DIRECT_HUB:-0}"
case "$NETWORK_PROVIDER" in
  tailscale|headscale)
    case "${HUB_URL:-}" in
      http://127.0.0.1*) ;;
      *) DEPLOY_DIRECT_HUB=1 ;;
    esac
    ;;
esac
# gketun-02: network=none spokes reach hub-managed shared services through the
# reverse tunnel's localhost forwards (install_reverse_tunnel_on_hub:
# -R 127.0.0.1:16333:hub:6333, -R 127.0.0.1:13002:hub:3002), NOT the hub FQDN —
# cross-pod service ports are typically blocked (only port 22 is), which is the
# whole reason the SSH tunnel exists. Mirror MAC_HUB_URL's 127.0.0.1:18789
# convention so the agent self-test + runtime hit the tunnel-forwarded ports.
if [ "$NETWORK_PROVIDER" = "none" ] \
  && [ "$DEPLOY_DIRECT_HUB" != "1" ] \
  && [ "$AGENT" != "$SHARED_SERVICES_MANAGER_AGENT" ]; then
  QDRANT_URL_CONFIGURED="http://127.0.0.1:16333"
  FIRECRAWL_URL_CONFIGURED="http://127.0.0.1:13002"
fi
NETWORK_INSTALL="${MAC_DEPLOY_NETWORK_INSTALL:-auto}"
NETWORK_HOSTNAME_PREFIX="${MAC_DEPLOY_NETWORK_HOSTNAME_PREFIX:-}"
TAILSCALE_AUTH_KEY_ENV="${MAC_DEPLOY_TAILSCALE_AUTH_KEY_ENV:-MAC_DEPLOY_TAILSCALE_AUTH_KEY}"
TAILSCALE_AUTH_KEY="${!TAILSCALE_AUTH_KEY_ENV:-}"
HEADSCALE_MANAGE="${MAC_DEPLOY_HEADSCALE_MANAGE:-0}"
HEADSCALE_LOGIN_SERVER="${MAC_DEPLOY_HEADSCALE_LOGIN_SERVER:-}"
HEADSCALE_HEALTH_URL="${MAC_DEPLOY_HEADSCALE_HEALTH_URL:-}"
HEADSCALE_FLEET_URL="${MAC_DEPLOY_HEADSCALE_FLEET_URL:-}"
HEADSCALE_PREAUTH_KEY_SOURCE="${MAC_DEPLOY_HEADSCALE_PREAUTH_KEY_SOURCE:-env}"
HEADSCALE_PREAUTH_KEY_ENV="${MAC_DEPLOY_HEADSCALE_PREAUTH_KEY_ENV:-MAC_DEPLOY_HEADSCALE_PREAUTHKEY}"
HEADSCALE_PREAUTHKEY="${!HEADSCALE_PREAUTH_KEY_ENV:-}"
HEADSCALE_PORT="${MAC_DEPLOY_HEADSCALE_PORT:-8080}"
HEADSCALE_PUBLIC_ADDR="${MAC_DEPLOY_HEADSCALE_PUBLIC_ADDR:-}"
HEADSCALE_DNS="${MAC_DEPLOY_HEADSCALE_DNS:-magicdns}"
HEADSCALE_IP_PREFIX="${MAC_DEPLOY_HEADSCALE_IP_PREFIX:-100.64.0.0/10}"
QDRANT_DATA_DIR_CONFIGURED="${MAC_DEPLOY_QDRANT_DATA_DIR:-}"
HUB_TUNNEL_PUBKEY="${MAC_DEPLOY_HUB_TUNNEL_PUBKEY:-}"
GITHUB_REVIEW_KEY_B64="${MAC_DEPLOY_GITHUB_REVIEW_KEY_B64:-}"
MAC_DEPLOY_TARGET="${MAC_DEPLOY_TARGET:-}"
DRAIN_MODE="${MAC_DEPLOY_DRAIN_MODE:-wait}"
DRAIN_TIMEOUT_SECONDS="${MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS:-1800}"
DRAIN_POLL_SECONDS="${MAC_DEPLOY_DRAIN_POLL_SECONDS:-10}"
DEFER_CLEAR_DRAIN="${MAC_DEPLOY_DEFER_CLEAR_DRAIN:-0}"
DEFER_AGENT_RESTART="${MAC_DEPLOY_DEFER_AGENT_RESTART:-0}"
OPENSHELL_DEPLOY_ENABLED="${MAC_DEPLOY_OPENSHELL_ENABLED:-0}"
OPENSHELL_EFFECTIVE_ARGS="${MAC_DEPLOY_OPENSHELL_EFFECTIVE_ARGS:-}"
OPENSHELL_RUNTIME_IMAGE="${MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE:-}"
OPENSHELL_LOCAL_IMAGE_BUILD="${MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD:-0}"
OPENSHELL_BOOTSTRAPPED=0
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
MAC_PORT="${MAC_DEPLOY_CONTROL_PORT:-${MAC_PORT:-8789}}"
REVIEWED_TOOL_ASSETS="${MAC_DEPLOY_REVIEWED_TOOL_ASSETS:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reviewed-tool-assets.sh}"
[ -r "$REVIEWED_TOOL_ASSETS" ] || {
  echo "ERROR: reviewed tool asset contract is unavailable: $REVIEWED_TOOL_ASSETS" >&2
  exit 1
}
# shellcheck disable=SC1090 -- copied from the same reviewed deploy revision.
. "$REVIEWED_TOOL_ASSETS"
LAUNCHD_LIFECYCLE_SOURCE="${MAC_DEPLOY_LAUNCHD_LIFECYCLE:-}"
ROLLBACK_SUPERVISOR_SOURCE="${MAC_DEPLOY_ROLLBACK_SUPERVISOR_HELPER:-}"
SRC_DIR="$MAC_HOME/src/mac"
VENV="$MAC_HOME/venv"
HERMES_DIR="$MAC_HOME/hermes-agent"
# ADR 0001 hu-04: the Hermes runtime is vendored in-tree (no upstream clone).
# Deploy-time python runs from the mac venv ($VENV) and imports the vendored
# runtime via PYTHONPATH; HERMES_DIR is no longer created. Include $SRC_DIR/src
# too so deploy-time Python helpers can import mac.* before the venv is built.
HERMES_VENDORED="$SRC_DIR/src/mac/_hermes"
export PYTHONPATH="$SRC_DIR/src:$HERMES_VENDORED:${PYTHONPATH:-}"
ENV_FILE="$MAC_HOME/mac.env"
LOG_DIR="$MAC_HOME/logs"
ROLLBACK_LAUNCHD_LIFECYCLE="$LOG_DIR/launchd-lifecycle-${DEPLOY_TS}.sh"
ROLLBACK_SUPERVISOR_HELPER="$LOG_DIR/rollback-supervisor-${DEPLOY_TS}.py"
ROLLBACK_LAUNCHD_LIFECYCLE_SHA256=""
ROLLBACK_SUPERVISOR_HELPER_SHA256=""
DEPLOY_LOG="$LOG_DIR/deploy-${DEPLOY_TS}.log"
DEPLOY_STARTED_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ROLLBACK_SCRIPT="$LOG_DIR/rollback-${DEPLOY_TS}.sh"
ROLLBACK_LATEST="$LOG_DIR/rollback-latest.sh"
ROLLBACK_INTENT="$LOG_DIR/rollback-${DEPLOY_TS}-intent.json"
ROLLBACK_INTENT_SHA256=""
ROLLBACK_COMPLETION_RECEIPT="$LOG_DIR/rollback-${DEPLOY_TS}-completion.json"
MANIFEST_PRE="$LOG_DIR/deploy-manifest-${DEPLOY_TS}-pre.json"
MANIFEST_POST="$LOG_DIR/deploy-manifest-${DEPLOY_TS}-post.json"
FINALIZE_RECEIPT="$LOG_DIR/deploy-${DEPLOY_TS}-finalize.json"
PREREQUISITE_HELPER="${MAC_DEPLOY_PREREQUISITE_HELPER:-}"
PREREQUISITE_HELPER_SHA256="${MAC_DEPLOY_PREREQUISITE_HELPER_SHA256:-}"
PREREQUISITE_BUNDLE="${MAC_DEPLOY_PREREQUISITE_BUNDLE:-}"
PREREQUISITE_EXPECTATIONS="${MAC_DEPLOY_PREREQUISITE_EXPECTATIONS:-}"
NODE_IDENTITY_SHA256="${MAC_DEPLOY_NODE_IDENTITY_SHA256:-}"
PREREQUISITE_SUMMARY="$LOG_DIR/prerequisite-bundle-${DEPLOY_TS}.json"
PREREQUISITE_BUNDLE_SHA256=""
PREREQUISITE_EXPECTATIONS_SHA256=""
MAC_SERVICE_NAME="${FLEET_NAME}.service"
HERMES_SERVICE_NAME="${FLEET_NAME}-hermes-gateway.service"
MAC_AGENT_SERVICE_NAME="${FLEET_NAME}-agent.service"
OPENCLAW_SERVICE_NAME="${FLEET_NAME}-openclaw-gateway.service"
# NemoClaw gateway service name (chat-gateway YOLO migration target).
NEMOCLAW_SERVICE_NAME="${FLEET_NAME}-nemoclaw-gateway.service"
MAC_GEN_SERVICE_NAME="${FLEET_NAME}-gen-server.service"
MAC_GEN_AUDIO_SERVICE_NAME="${FLEET_NAME}-gen-audio-server.service"
MAC_GEN_VIDEO_SERVICE_NAME="${FLEET_NAME}-gen-video-server.service"
MAC_LAUNCHD_LABEL="com.${FLEET_NAME}.control-plane"
DARWIN_SYSTEM_SUPERVISOR_LABEL="com.${FLEET_NAME}.supervisor"
HERMES_LAUNCHD_LABEL="com.${FLEET_NAME}.hermes-gateway"
OPENCLAW_LAUNCHD_LABEL="com.${FLEET_NAME}.openclaw-gateway"
NEMOCLAW_LAUNCHD_LABEL="com.${FLEET_NAME}.nemoclaw-gateway"
MAC_AGENT_LAUNCHD_LABEL="com.${FLEET_NAME}.agent"
MAC_SUPERVISORD_PROG="${FLEET_NAME}-control-plane"
HERMES_SUPERVISORD_PROG="${FLEET_NAME}-hermes-gateway"
OPENCLAW_SUPERVISORD_PROG="${FLEET_NAME}-openclaw-gateway"
NEMOCLAW_SUPERVISORD_PROG="${FLEET_NAME}-nemoclaw-gateway"
AGENT_SUPERVISORD_PROG="${FLEET_NAME}-agent"
MAC_SUPERVISORD_CONF_NAME="${FLEET_NAME}-fleet.conf"
SRC_BACKUP=""
VENV_BACKUP=""
HERMES_BACKUP=""
BIN_BACKUP=""
OPENCLAW_HOME_BACKUP=""
OPENCLAW_HOME_EXISTED=0
MAC_UNIT_BACKUP=""
HERMES_UNIT_BACKUP=""
MAC_AGENT_UNIT_BACKUP=""
MAC_UNIT_MUTATED=0
HERMES_UNIT_MUTATED=0
MAC_AGENT_UNIT_MUTATED=0
MAC_PLIST_BACKUP=""
MAC_PLIST_MUTATED=0
DARWIN_SYSTEM_PLIST_BACKUP=""
DARWIN_SYSTEM_PLIST_MUTATED=0
DARWIN_SYSTEM_LAUNCHD_ACTIVE=0
DARWIN_GUI_LAUNCHD_ACTIVE=0
DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP=""
DARWIN_SYSTEM_SUPERVISOR_PLIST_MUTATED=0
DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE=0
HERMES_PLIST_BACKUP=""
HERMES_PLIST_MUTATED=0
MAC_AGENT_PLIST_BACKUP=""
MAC_AGENT_PLIST_MUTATED=0
DARWIN_HERMES_LAUNCHD_ACTIVE=0
DARWIN_OPENCLAW_LAUNCHD_ACTIVE=0
DARWIN_NEMOCLAW_LAUNCHD_ACTIVE=0
DARWIN_AGENT_LAUNCHD_ACTIVE=0
ROLLBACK_AUX_ARTIFACT_COUNT=0
ROLLBACK_AUX_ARTIFACT_PATHS=()
ROLLBACK_AUX_ARTIFACT_BACKUPS=()
ROLLBACK_AUX_ARTIFACT_EXISTED=()
ROLLBACK_AUX_ARTIFACT_MODES=()
ROLLBACK_ACTIVE_GATEWAY=""
ROLLBACK_AGENT_PRIOR_STATE=""
ROLLBACK_PRIOR_GENERATION=""
ROLLBACK_PRIOR_REVISION=""

# Apply a restrictive umask before creating LOG_DIR so it gets 0700 (owner only).
umask 0077
mkdir -p "$LOG_DIR" "$MAC_HOME/backups"
umask 0022
exec > >(tee -a "$DEPLOY_LOG") 2>&1
# Tighten deploy log to owner-read/write only (the tee process already has the fd open).
chmod 0600 "$DEPLOY_LOG" 2>/dev/null || true

snapshot_deploy_contract() {
  local source="$1" destination="$2" marker="$3" description="$4"
  [ -n "$source" ] || {
    echo "ERROR: $description was not supplied" >&2
    return 1
  }
  MAC_CONTRACT_SOURCE="$source" MAC_CONTRACT_SNAPSHOT="$destination" \
    MAC_CONTRACT_MARKER="$marker" MAC_CONTRACT_DESCRIPTION="$description" \
    python3 - <<'PY'
import hashlib
import os
import stat
import tempfile
from pathlib import Path

source = Path(os.environ["MAC_CONTRACT_SOURCE"])
destination = Path(os.environ["MAC_CONTRACT_SNAPSHOT"])
marker = os.environ["MAC_CONTRACT_MARKER"].encode("utf-8")
description = os.environ["MAC_CONTRACT_DESCRIPTION"]
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(str(source), flags)
except OSError:
    raise SystemExit(description + " could not be opened safely")
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or metadata.st_size <= 0
        or metadata.st_size > 1024 * 1024
    ):
        raise SystemExit(description + " is not owner-private and bounded")
    chunks = []
    remaining = metadata.st_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            raise SystemExit(description + " was truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise SystemExit(description + " changed while reading")
    after = os.fstat(descriptor)
finally:
    os.close(descriptor)
raw = b"".join(chunks)
if (
    after.st_dev != metadata.st_dev
    or after.st_ino != metadata.st_ino
    or after.st_size != metadata.st_size
    or after.st_mtime_ns != metadata.st_mtime_ns
    or after.st_ctime_ns != metadata.st_ctime_ns
    or marker not in raw
):
    raise SystemExit(description + " is incomplete or changed while reading")
fd, temporary_raw = tempfile.mkstemp(
    prefix="." + destination.name + ".", dir=str(destination.parent)
)
temporary = Path(temporary_raw)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    directory = os.open(str(destination.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    temporary.unlink(missing_ok=True)
print(hashlib.sha256(raw).hexdigest())
PY
}

ROLLBACK_SUPERVISOR_HELPER_SHA256="$(snapshot_deploy_contract \
  "$ROLLBACK_SUPERVISOR_SOURCE" "$ROLLBACK_SUPERVISOR_HELPER" \
  'mac.fleet_node_rollback_supervisor.v1' 'rollback supervisor contract')"

ROLLBACK_LAUNCHD_LIFECYCLE_SHA256="$(snapshot_deploy_contract \
  "$LAUNCHD_LIFECYCLE_SOURCE" "$ROLLBACK_LAUNCHD_LIFECYCLE" \
  'mac_launchd_transaction_begin()' 'bounded launchd lifecycle contract')"
if ! [[ "$ROLLBACK_SUPERVISOR_HELPER_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || ! [[ "$ROLLBACK_LAUNCHD_LIFECYCLE_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "ERROR: rollback contract hash capture failed" >&2
  exit 1
fi
# shellcheck source=/dev/null -- owner-private snapshot validated above.
. "$ROLLBACK_LAUNCHD_LIFECYCLE"

log() {
  printf '[%s] [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$AGENT" "$*"
}

# The remote deploy shell is assembled from this heredoc and does not inherit
# the launcher functions.  Define the fatal-error helper in that shell too.
die() {
  log "ERROR: $*"
  exit 1
}

run_without_deploy_credentials() {
  # Reviewed bootstrap tools need ordinary filesystem/network context only.
  # Never project hub, GitHub, provider, or worker secrets into child tools.
  env -i HOME="$HOME" PATH="$MAC_HOME/bin:${PATH:-/usr/bin:/bin}" \
    TMPDIR="${TMPDIR:-/tmp}" \
    HTTPS_PROXY="${HTTPS_PROXY:-}" HTTP_PROXY="${HTTP_PROXY:-}" \
    NO_PROXY="${NO_PROXY:-}" \
    "$@"
}

install_fleet_registry() {
  if [ -n "$FLEET_REGISTRY_FILE" ] && [ -f "$FLEET_REGISTRY_FILE" ]; then
    mkdir -p "$MAC_HOME"
    cp -f "$FLEET_REGISTRY_FILE" "$MAC_HOME/fleets.yaml"
    chmod 0644 "$MAC_HOME/fleets.yaml"
    rm -f "$FLEET_REGISTRY_FILE"
    log "installed fleet registry at $MAC_HOME/fleets.yaml"
  fi
}

python_bin() {
  local candidate
  for candidate in "${MAC_PYTHON:-}" /opt/homebrew/bin/python3 /usr/local/bin/python3 python3.13 python3.12 python3.11 python3.10 python3 python; do
    [ -n "$candidate" ] || continue
    if ! command -v "$candidate" >/dev/null 2>&1; then
      continue
    fi
    candidate="$(command -v "$candidate")"
    if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
# Must match pyproject.toml requires-python (>=3.11); a 3.10 interpreter would
# fail `pip install -e .` partway through the remote deploy. Skip it so we pick
# a real 3.11+ (e.g. python3.12) instead of dying mid-install.
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      printf '%s\n' "$candidate"
      return
    fi
  done
  # Interpreter provisioning is monotonic onboarding, not a reversible cohort
  # generation mutation.  Synchronized phase 2 only verifies that onboarding
  # completed and fails closed before any node state is quiesced.
  log "ERROR: Python >= 3.11 is missing; complete node onboarding before phase 2"
  exit 1
}

hermes_python_bin() {
  local candidate
  for candidate in "${MAC_HERMES_PYTHON:-}" python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 python; do
    [ -n "$candidate" ] || continue
    if ! command -v "$candidate" >/dev/null 2>&1; then
      continue
    fi
    candidate="$(command -v "$candidate")"
    if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      printf '%s\n' "$candidate"
      return
    fi
  done
  log "WARNING: Python >= 3.11 not found; Hermes agent venv will use $1 with --ignore-requires-python" >&2
  printf '%s\n' "$1"
}

PY="$(python_bin)"
# PYTHON_BIN is referenced by remote-payload helpers (e.g. install_github_review_key);
# resolve_python_bin only runs in the local driver, so assign it here in the payload
# (mirrors PY) or the remote aborts under `set -u` with "PYTHON_BIN: unbound variable".
PYTHON_BIN="$PY"
HERMES_PY="$(hermes_python_bin "$PY")"
SUPERVISOR_KIND=""
export AGENT FLEET_NAME OS_KIND DEPLOY_TS DEPLOY_REV DEPLOY_GENERATION DEPLOY_GIT_URL DEPLOY_GIT_BRANCH DEPLOY_STARTED_ISO HERMES_SLACK_HOME_CHANNEL_NAME HERMES_GATEWAY_MODEL HERMES_GATEWAY_PROVIDER HERMES_GATEWAY_BASE_URL HERMES_GATEWAY_IMPL HERMES_SURFACE_B64 OPENCLAW_PUBLIC_IDENTITY OPENCLAW_REPRESENTED_BY OPENCLAW_REPRESENTATION_MODE OPENCLAW_SLACK_ACCOUNT_ID OPENCLAW_TELEGRAM_ACCOUNT_ID HUB_URL HUB_TUNNEL_PUBKEY CONTROL_BIND_HOST WORKER_MODE WORKER_CAPABILITIES WORKER_ALLOWED_PROJECTS WORKER_REQUIRED_METADATA WORKER_REQUIRE_CANARY SUPERVISOR_REQUESTED SUPERVISOR_KIND SHARED_SERVICES_MANAGER_AGENT QDRANT_URL_CONFIGURED QDRANT_INSTALL QDRANT_REQUIRE QDRANT_BIND_ADDR_CONFIGURED QDRANT_PORT_CONFIGURED QDRANT_IMAGE_CONFIGURED QDRANT_MEMORY_LIMIT_CONFIGURED QDRANT_DATA_DIR_CONFIGURED FIRECRAWL_URL_CONFIGURED FIRECRAWL_INSTALL FIRECRAWL_REQUIRE FIRECRAWL_BIND_ADDR_CONFIGURED FIRECRAWL_PORT_CONFIGURED WEBDAV_ENABLED WEBDAV_URL_CONFIGURED WEBDAV_INSTALL WEBDAV_BIND_ADDR_CONFIGURED WEBDAV_PORT_CONFIGURED WEBDAV_ROOT_CONFIGURED WEBDAV_PUBLIC_PATH_CONFIGURED WEBDAV_MAX_UPLOAD_BYTES_CONFIGURED DRAIN_MODE DRAIN_TIMEOUT_SECONDS DRAIN_POLL_SECONDS CONFIGURED_AGENT_IDS OPENSHELL_DEPLOY_ENABLED OPENSHELL_EFFECTIVE_ARGS OPENSHELL_RUNTIME_IMAGE OPENSHELL_LOCAL_IMAGE_BUILD MAC_HOME MAC_PORT MAC_SERVICE_NAME HERMES_SERVICE_NAME OPENCLAW_SERVICE_NAME NEMOCLAW_SERVICE_NAME MAC_AGENT_SERVICE_NAME MAC_LAUNCHD_LABEL HERMES_LAUNCHD_LABEL OPENCLAW_LAUNCHD_LABEL NEMOCLAW_LAUNCHD_LABEL MAC_AGENT_LAUNCHD_LABEL MAC_SUPERVISORD_PROG HERMES_SUPERVISORD_PROG OPENCLAW_SUPERVISORD_PROG NEMOCLAW_SUPERVISORD_PROG AGENT_SUPERVISORD_PROG MAC_SUPERVISORD_CONF_NAME SRC_DIR VENV HERMES_DIR ENV_FILE LOG_DIR DEPLOY_LOG PY HERMES_PY PYTHON_BIN NODE_ACTION NODE_IDENTITY_SHA256 PREREQUISITE_SUMMARY PREREQUISITE_BUNDLE_SHA256 PREREQUISITE_EXPECTATIONS_SHA256

disk_hygiene_report() {
  local stage="$1" path="$2"
  "$PY" - "$stage" "$path" <<'PY'
import calendar
import json
import os
import shutil
import sys
import time
from pathlib import Path

stage, output = sys.argv[1], Path(sys.argv[2])
home = Path.home()
mac_home = Path(os.environ["MAC_HOME"])

def du(path: Path) -> dict:
    try:
        exists = path.exists()
    except OSError:
        exists = False
    result = {"path": str(path), "exists": exists, "bytes": 0}
    if not exists:
        return result
    try:
        if path.is_file() or path.is_symlink():
            result["bytes"] = path.stat().st_size
            return result
        total = 0
        for root, dirs, files in os.walk(path):
            dirs[:] = [name for name in dirs if name not in {".git", ".venv", "node_modules"}]
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
        result["bytes"] = total
    except OSError as exc:
        result["error"] = str(exc)
    return result

usage = shutil.disk_usage(home)
paths = [
    mac_home / "backups",
    mac_home / "logs",
    mac_home / "agent-workspaces",
    home / ".acc" / "build",
    home / ".acc" / "dist",
    home / ".acc" / "deploy",
    home / ".acc" / "logs",
    home / ".acc" / "hermes-agent",
    home / ".agentfs" / "reviews",
    home / "AgentFS" / "reviews",
]
report = {
    "schema": "mac.deploy.disk_hygiene.v1",
    "stage": stage,
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "filesystem": {"total": usage.total, "used": usage.used, "free": usage.free},
    "paths": [du(path) for path in paths],
}
output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(
    "disk hygiene %s: free_gb=%.2f report=%s"
    % (stage, usage.free / (1024 ** 3), output)
)
PY
}

cleanup_obsolete_deploy_artifacts() {
  "$PY" - "$LOG_DIR/disk-cleanup-${DEPLOY_TS}.json" <<'PY'
import json
import os
import shutil
import sys
import time
from pathlib import Path

output = Path(sys.argv[1])
home = Path.home()
mac_home = Path(os.environ["MAC_HOME"])
now = time.time()

obsolete_acc_paths = [
    home / ".acc" / "build",
    home / ".acc" / "dist",
    home / ".acc" / "deploy",
    home / ".acc" / "logs",
    home / ".acc" / ".pytest_cache",
    home / ".acc" / "hermes-agent",
    home / ".agentfs" / "reviews",
    home / "AgentFS" / "reviews",
]
retained_roots = [
    (mac_home / "backups", 14, "generated MAC deploy backups"),
    (mac_home / "logs", 30, "generated MAC deploy logs"),
]
tmp_patterns = [Path("/tmp").glob("mac-*.tar.gz"), Path("/tmp").glob("mac-fleet-deploy-*")]

def size(path: Path) -> int:
    try:
        if path.is_file() or path.is_symlink():
            return path.stat().st_size
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0

def remove(path: Path, reason: str) -> dict:
    entry = {"path": str(path), "reason": reason, "bytes": size(path)}
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        entry["removed"] = True
    except FileNotFoundError:
        entry["removed"] = False
    except OSError as exc:
        entry["removed"] = False
        entry["error"] = str(exc)
    return entry

removed = []
for path in obsolete_acc_paths:
    if path.exists():
        removed.append(remove(path, "obsolete ACC-derived artifact"))

for root, retain_days, reason in retained_roots:
    if not root.exists():
        continue
    cutoff = now - retain_days * 86400
    for child in root.iterdir():
        try:
            if child.stat().st_mtime < cutoff:
                removed.append(remove(child, "%s older than %d days" % (reason, retain_days)))
        except OSError as exc:
            removed.append({"path": str(child), "reason": reason, "removed": False, "error": str(exc)})

tmp_cutoff = now - 2 * 86400
for pattern in tmp_patterns:
    for path in pattern:
        try:
            if path.stat().st_mtime < tmp_cutoff:
                removed.append(remove(path, "stale MAC deploy temp artifact older than 2 days"))
        except OSError:
            pass

report = {
    "schema": "mac.deploy.disk_cleanup.v1",
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "removed": removed,
    "removed_count": sum(1 for item in removed if item.get("removed")),
    "removed_bytes": sum(int(item.get("bytes") or 0) for item in removed if item.get("removed")),
}
output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("disk cleanup: removed=%d bytes=%d report=%s" % (report["removed_count"], report["removed_bytes"], output))
PY
}

dns_lookup() {
  if command -v getent >/dev/null 2>&1; then
    getent hosts pypi.org >/dev/null 2>&1
    return
  fi
  "$PY" - <<'PY' >/dev/null 2>&1
import socket
socket.getaddrinfo("pypi.org", 443)
PY
}

ensure_dns_resolution() {
  dns_lookup && return 0
  log "ERROR: DNS prerequisite is unavailable; repair it during node onboarding"
  exit 1
}

ensure_venv_support() {
  if "$PY" -c 'import ensurepip, venv' >/dev/null 2>&1; then
    return 0
  fi
  log "ERROR: Python venv support is missing; complete node onboarding before phase 2"
  exit 1
}

truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

detect_supervisor() {
  case "${SUPERVISOR_REQUESTED:-auto}" in
    systemd)
      [ "$OS_KIND" = linux ] && command -v systemctl >/dev/null 2>&1 \
        && [ -d /run/systemd/system ] || {
          log "ERROR: requested systemd supervisor is not active on this Linux node"
          exit 1
        }
      printf '%s\n' systemd
      return
      ;;
    launchd)
      [ "$OS_KIND" = darwin ] && command -v launchctl >/dev/null 2>&1 || {
        log "ERROR: requested launchd supervisor is unavailable on this Darwin node"
        exit 1
      }
      printf '%s\n' launchd
      return
      ;;
    supervisord)
      command -v supervisorctl >/dev/null 2>&1 || {
        log "ERROR: requested supervisord supervisor lacks supervisorctl"
        exit 1
      }
      printf '%s\n' supervisord
      return
      ;;
    auto|"")
      ;;
    *)
      log "ERROR: unsupported MAC_DEPLOY_SUPERVISOR value: $SUPERVISOR_REQUESTED"
      exit 1
      ;;
  esac
  if [ "$OS_KIND" = "darwin" ] && command -v launchctl >/dev/null 2>&1; then
    printf '%s\n' "launchd"
    return
  fi
  if [ "$OS_KIND" = "linux" ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    printf '%s\n' "systemd"
    return
  fi
  if command -v supervisorctl >/dev/null 2>&1; then
    printf '%s\n' "supervisord"
    return
  fi
  log "ERROR: could not detect a supported supervisor; set MAC_DEPLOY_SUPERVISOR=systemd, launchd, or supervisord"
  exit 1
}

run_supervisorctl() {
  if [ "$(id -u)" -eq 0 ]; then
    mac_run_bounded \
      "${MAC_SUPERVISOR_COMMAND_TIMEOUT_SECONDS:-30}" \
      supervisorctl "$@"
  else
    command -v sudo >/dev/null 2>&1 \
      || die "system supervisord scope requires non-interactive sudo"
    mac_run_bounded \
      "${MAC_SUPERVISOR_COMMAND_TIMEOUT_SECONDS:-30}" \
      sudo -n supervisorctl "$@"
  fi
}

run_privileged_bounded() {
  local timeout="$1"
  shift
  if [ "$(id -u)" -eq 0 ]; then
    mac_run_bounded "$timeout" "$@"
  else
    mac_run_bounded "$timeout" sudo -n "$@"
  fi
}

run_systemctl() {
  if [ "$(id -u)" -eq 0 ]; then
    mac_run_bounded \
      "${MAC_SYSTEMD_COMMAND_TIMEOUT_SECONDS:-30}" \
      systemctl "$@"
  else
    mac_run_bounded \
      "${MAC_SYSTEMD_COMMAND_TIMEOUT_SECONDS:-30}" \
      sudo -n systemctl "$@"
  fi
}

run_user_systemctl() {
  mac_run_bounded \
    "${MAC_SYSTEMD_COMMAND_TIMEOUT_SECONDS:-30}" \
    systemctl --user "$@"
}

run_journalctl() {
  if [ "$(id -u)" -eq 0 ]; then
    mac_run_bounded \
      "${MAC_SYSTEMD_COMMAND_TIMEOUT_SECONDS:-30}" \
      journalctl "$@"
  else
    mac_run_bounded \
      "${MAC_SYSTEMD_COMMAND_TIMEOUT_SECONDS:-30}" \
      sudo -n journalctl "$@"
  fi
}

control_plane_enabled() {
  [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]
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

qdrant_install_enabled() {
  case "${QDRANT_INSTALL:-auto}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    0|false|FALSE|no|NO|off|OFF|none|disabled) return 1 ;;
    auto|"") [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]; return ;;
    *)
      log "ERROR: unsupported MAC_DEPLOY_QDRANT_INSTALL value: $QDRANT_INSTALL"
      exit 1
      ;;
  esac
}

firecrawl_install_enabled() {
  case "${FIRECRAWL_INSTALL:-auto}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    0|false|FALSE|no|NO|off|OFF|none|disabled) return 1 ;;
    auto|"") [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]; return ;;
    *)
      log "ERROR: unsupported MAC_DEPLOY_FIRECRAWL_INSTALL value: $FIRECRAWL_INSTALL"
      exit 1
      ;;
  esac
}

webdav_enabled() {
  case "${WEBDAV_ENABLED:-0}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

webdav_install_enabled() {
  webdav_enabled || return 1
  case "${WEBDAV_INSTALL:-auto}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    0|false|FALSE|no|NO|off|OFF|none|disabled) return 1 ;;
    auto|"") [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]; return ;;
    *)
      log "ERROR: unsupported MAC_DEPLOY_WEBDAV_INSTALL value: $WEBDAV_INSTALL"
      exit 1
      ;;
  esac
}

ensure_hub_tunnel_key() {
  local key_file="$HOME/.ssh/mac_tunnel_id"
  if [ ! -f "$key_file" ]; then
    log "generating hub SSH tunnel keypair at $key_file"
    ssh-keygen -t ed25519 -f "$key_file" -N "" -C "mac-hub-tunnel@${AGENT}" -q
  fi
  chmod 600 "$key_file"
  log "hub tunnel public key: $(cat "${key_file}.pub")"
}

install_hub_tunnel_pubkey() {
  [ -n "$HUB_TUNNEL_PUBKEY" ] || return 0
  local auth_keys="$HOME/.ssh/authorized_keys"
  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"
  touch "$auth_keys"
  chmod 600 "$auth_keys"
  if ! grep -qF "$HUB_TUNNEL_PUBKEY" "$auth_keys" 2>/dev/null; then
    log "adding hub tunnel public key to authorized_keys"
    printf '%s\n' "$HUB_TUNNEL_PUBKEY" >> "$auth_keys"
  fi
}

# gketun-02: a spoke reaches the hub control plane (127.0.0.1:18789) and the
# hub-managed shared services (Qdrant 127.0.0.1:16333, Firecrawl 127.0.0.1:13002)
# ONLY through the hub's reverse SSH tunnel (hub runs `ssh -R 18789:...:8789 ...`).
# The hub's tunnel program (startretries=1000) keeps retrying and connects within
# seconds once this spoke authorizes the hub key (install_hub_tunnel_pubkey above).
# Wait for the tunnel here so the strict shared-services validation that follows
# does not race tunnel establishment (which otherwise fails the whole deploy on a
# redeploy where allow-degraded is off).
wait_for_hub_reverse_tunnel() {
  [ -n "$HUB_TUNNEL_PUBKEY" ] || return 0
  if [ "${DEPLOY_DIRECT_HUB:-0}" = "1" ]; then
    log "direct hub path (${NETWORK_PROVIDER:-mesh}); skipping reverse-tunnel wait"
    return 0
  fi
  local i
  for i in $(seq 1 24); do
    if curl -fsS --max-time 3 "http://127.0.0.1:18789/health" >/dev/null 2>&1; then
      log "hub reverse tunnel established (127.0.0.1:18789 reachable after $(( (i - 1) * 5 ))s)"
      return 0
    fi
    sleep 5
  done
  log "WARNING: hub reverse tunnel still unreachable after 120s (127.0.0.1:18789); shared-services validation may fail"
  return 0
}

remove_managed_github_review_key_config() {
  local config_file="$1"
  [ -f "$config_file" ] || return 0
  sed -i.bak '/^# mac GitHub review deploy key$/,/^  IdentitiesOnly yes$/d' "$config_file"
  rm -f "${config_file}.bak"
}

github_ssh_auth_succeeds() {
  local key_file="${1:-}" output rc ssh_args
  ssh_args=(
    ssh -n -F /dev/null -o BatchMode=yes -o ConnectTimeout=10
    -o StrictHostKeyChecking=yes
  )
  if [ -n "$key_file" ]; then
    ssh_args+=(-o IdentitiesOnly=yes -i "$key_file")
  fi
  set +e
  output="$("${ssh_args[@]}" -T git@github.com 2>&1)"
  rc=$?
  set -e
  [ "$rc" -eq 1 ] && printf '%s' "$output" | grep -q 'successfully authenticated'
}

install_github_review_key() {
  local key_file="$HOME/.ssh/mac_github_review_id"
  # SSH identities, known_hosts, and per-user Git configuration are onboarding
  # state.  Phase 2 proves the already-installed identity; it never creates or
  # rewrites that durable state under the generation rollback umbrella.
  if [ -f "$key_file" ] && [ ! -L "$key_file" ] \
      && github_ssh_auth_succeeds "$key_file"; then
    log "verified onboarded GitHub review identity"
    return 0
  fi
  if github_ssh_auth_succeeds; then
    log "verified onboarded ambient GitHub SSH identity"
    return 0
  fi
  if [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]; then
    echo "ERROR: the hub cannot authenticate to github.com for review publication" >&2
    echo "Install and authorize the GitHub review identity during onboarding, then retry phase 2." >&2
    exit 1
  fi
  log "WARNING: no onboarded GitHub SSH identity is authorized on this spoke"
}

configure_github_https_credentials() {
  local gh_bin
  # The operator streams the deployment credential as MAC_DEPLOY_GH_TOKEN so
  # it never appears in the SSH command argv.  Promote it only inside this
  # one-use installer process; build_mac_env() later persists the same value as
  # GH_TOKEN for the worker service and private OpenShell environment.
  if [ -z "${GH_TOKEN:-}" ] && [ -n "${MAC_DEPLOY_GH_TOKEN:-}" ]; then
    export GH_TOKEN="$MAC_DEPLOY_GH_TOKEN"
  fi
  if [ -z "${GH_TOKEN:-}" ]; then
    if [ "$GITHUB_CREDENTIALS_REQUIRED" = "1" ]; then
      log "ERROR: GH_TOKEN absent on a node that requires GitHub repository credentials"
      return 1
    fi
    log "GH_TOKEN absent; skipping optional GitHub HTTPS credential setup"
    return 0
  fi
  gh_bin="$(command -v gh 2>/dev/null || true)"
  if [ -z "$gh_bin" ]; then
    if [ "$GITHUB_CREDENTIALS_REQUIRED" = "1" ]; then
      log "ERROR: gh CLI not found on a node that requires GitHub repository credentials"
      return 1
    fi
    log "WARNING: gh CLI not found; optional GitHub HTTPS credential setup skipped"
    return 0
  fi
  if ! "$gh_bin" auth status --hostname github.com >/dev/null 2>&1; then
    if [ "$GITHUB_CREDENTIALS_REQUIRED" = "1" ]; then
      log "ERROR: GH_TOKEN was projected but GitHub rejected it"
      return 1
    fi
    log "WARNING: GH_TOKEN was projected but GitHub rejected it"
    return 0
  fi
  log "GitHub HTTPS credential verified without changing host Git configuration"
}

# On a brand-new spoke the hub's Qdrant/Firecrawl are reached through a reverse
# tunnel that is not fully established until this first deploy authorizes the
# tunnel key. MAC_DEPLOY_ALLOW_DEGRADED_SERVICES=1 (set by main() for that first
# deploy) lets the deploy proceed degraded so the tunnel gets set up; the
# post-deploy path in main() then waits for the tunnel and restarts the agent,
# and a subsequent deploy (flag unset) validates strictly.
validate_qdrant_endpoint() {
  local qdrant_url degraded="${MAC_DEPLOY_ALLOW_DEGRADED_SERVICES:-0}"
  qdrant_url="${QDRANT_URL:-${QDRANT_ADDRESS:-${QDRANT_FLEET_URL:-}}}"
  if [ -z "$qdrant_url" ]; then
    if [ "$degraded" = "1" ]; then
      log "WARNING: Qdrant endpoint not configured; proceeding degraded (first deploy)"
      return
    fi
    log "ERROR: Qdrant shared memory is required but no endpoint is configured"
    exit 1
  fi
  if curl -fsS --connect-timeout 2 --max-time 5 "${qdrant_url%/}/collections" >/dev/null; then
    log "Qdrant shared memory reachable at configured collections endpoint"
    return
  fi
  if [ "$degraded" = "1" ]; then
    log "WARNING: Qdrant unreachable at ${qdrant_url%/}/collections; proceeding degraded (first deploy — hub tunnel not yet established). Redeploy after it comes up to validate strictly."
    return
  fi
  log "ERROR: Qdrant shared memory is unreachable at ${qdrant_url%/}/collections"
  exit 1
}

validate_firecrawl_endpoint() {
  local firecrawl_url degraded="${MAC_DEPLOY_ALLOW_DEGRADED_SERVICES:-0}"
  firecrawl_url="${FIRECRAWL_API_URL:-${FIRECRAWL_GATEWAY_URL:-${FIRECRAWL_URL_CONFIGURED:-}}}"
  if [ -z "$firecrawl_url" ]; then
    if [ "$degraded" = "1" ]; then
      log "WARNING: Firecrawl endpoint not configured; proceeding degraded (first deploy)"
      return
    fi
    log "ERROR: Firecrawl web search is required but no endpoint is configured"
    exit 1
  fi
  if curl -fsS --connect-timeout 2 --max-time 5 "${firecrawl_url%/}/health" >/dev/null; then
    log "Firecrawl web search reachable at configured health endpoint"
    return
  fi
  if [ "$degraded" = "1" ]; then
    log "WARNING: Firecrawl unreachable at ${firecrawl_url%/}/health; proceeding degraded (first deploy — hub tunnel not yet established). Redeploy after it comes up to validate strictly."
    return
  fi
  log "ERROR: Firecrawl web search is unreachable at ${firecrawl_url%/}/health"
  exit 1
}

validate_webdav_endpoint() {
  webdav_enabled || return 0
  local webdav_url health_url
  webdav_url="${MAC_PUBLISH_WEBDAV_URL:-${MAC_WEBDAV_PUBLIC_URL:-${WEBDAV_URL_CONFIGURED:-}}}"
  if [ -z "$webdav_url" ]; then
    log "ERROR: WebDAV publishing is enabled but no public URL is configured"
    exit 1
  fi
  health_url="$("$PYTHON_BIN" - "$webdav_url" <<'PY'
from urllib.parse import urlsplit, urlunsplit
import sys

url = sys.argv[1].strip()
parts = urlsplit(url)
if parts.scheme and parts.netloc:
    print(urlunsplit((parts.scheme, parts.netloc, "/health", "", "")))
else:
    print(url.rstrip("/") + "/health")
PY
)"
  if curl -fsS --connect-timeout 2 --max-time 5 "$health_url" >/dev/null; then
    log "WebDAV public artifact server reachable at $health_url"
    return
  fi
  log "WARNING: WebDAV public artifact server is not reachable at $health_url from this node; continuing because public reachability may differ from node-local routing"
}

reload_mac_env() {
  unset MAC_HERMES_GATEWAY_MODEL ACC_HERMES_GATEWAY_MODEL HERMES_INFERENCE_MODEL ACC_LLM_MODEL
  set -a
  . "$ENV_FILE"
  set +a
}

openshell_disable_requested() {
  local requested required
  requested="$(
    printf '%s' "${MAC_DEPLOY_OPENSHELL:-}" \
      | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
      | tr '[:upper:]' '[:lower:]'
  )"
  case "$requested" in
    0|false|no|off) ;;
    *) return 1 ;;
  esac
  required="$(
    printf '%s' "${MAC_DEPLOY_OPENSHELL_REQUIRED:-${MAC_OPENSHELL_REQUIRED:-}}" \
      | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
      | tr '[:upper:]' '[:lower:]'
  )"
  case "$required" in
    1|true|yes|on) return 1 ;;
  esac
  return 0
}

bootstrap_enabled_openshell() {
  local bootstrap="$SRC_DIR/deploy/openshell/bootstrap-openshell.sh"
  local parsed_args arg enabled=0 required=0 value found expected_openclaw suffix
  local -a bootstrap_args=()
  if truthy "$OPENSHELL_DEPLOY_ENABLED" || truthy "${MAC_DEPLOY_OPENSHELL:-}"; then
    enabled=1
  fi
  if truthy "${MAC_DEPLOY_OPENSHELL_REQUIRED:-}"; then
    enabled=1
    required=1
  fi
  [ "$enabled" = 1 ] || return 0
  [ -x "$bootstrap" ] || die "OpenShell bootstrap is missing or not executable: $bootstrap"

  if [ -n "$OPENSHELL_RUNTIME_IMAGE" ]; then
    [[ "$OPENSHELL_RUNTIME_IMAGE" =~ ^ghcr\.io/jordanhubbard/mac-openshell-runtime@sha256:[0-9a-f]{64}$ ]] \
      || die "OpenShell runtime image is not an immutable repository-owned digest"
  elif ! truthy "$OPENSHELL_LOCAL_IMAGE_BUILD"; then
    die "enabled OpenShell deployment requires an immutable runtime image"
  fi

  # The outer controller has already normalized role-required flags. Parse its
  # exact result without eval so quoted whitespace cannot become shell syntax.
  # Only bootstrap's reviewed flag surface is accepted here.
  parsed_args="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-args.XXXXXX")"
  if ! "$PY" - "$OPENSHELL_EFFECTIVE_ARGS" > "$parsed_args" <<'PY'
import shlex
import sys

allowed = {"--enable", "--fail-closed", "--skip-image"}
try:
    values = shlex.split(sys.argv[1], posix=True)
except ValueError as exc:
    raise SystemExit("invalid OpenShell bootstrap arguments: %s" % exc)
unknown = sorted(set(values) - allowed)
if unknown:
    raise SystemExit("unreviewed OpenShell bootstrap arguments: %s" % ", ".join(unknown))
for value in values:
    sys.stdout.buffer.write(value.encode("utf-8") + b"\0")
PY
  then
    rm -f "$parsed_args"
    die "could not parse reviewed OpenShell bootstrap arguments"
  fi
  while IFS= read -r -d '' arg; do
    bootstrap_args+=("$arg")
  done < "$parsed_args"
  rm -f "$parsed_args"

  if [ "$required" = 1 ]; then
    for value in --enable --fail-closed; do
      found=0
      if [ "${#bootstrap_args[@]}" -gt 0 ]; then
        for arg in "${bootstrap_args[@]}"; do
          [ "$arg" != "$value" ] || found=1
        done
      fi
      if [ "$found" = 0 ]; then
        bootstrap_args+=("$value")
      fi
    done
  fi
  if [ -n "$OPENSHELL_RUNTIME_IMAGE" ] && [ "${#bootstrap_args[@]}" -gt 0 ]; then
    for arg in "${bootstrap_args[@]}"; do
      [ "$arg" != --skip-image ] \
        || die "--skip-image is incompatible with a digest-managed OpenShell deployment"
    done
  fi

  expected_openclaw=""
  if [ "${HERMES_GATEWAY_IMPL:-hermes}" = openclaw ]; then
    suffix="$(printf '%s' "${MAC_AGENT_ID:-agent_$AGENT}" \
      | sed -E 's/^agent_//; s/[^A-Za-z0-9]+/-/g; s/^-+//; s/-+$//' \
      | tr '[:upper:]' '[:lower:]')"
    expected_openclaw="mac-openclaw-${suffix:-gateway}"
  fi

  log "bootstrapping final OpenShell gateway before any chat or worker sandbox is created"
  if [ "${#bootstrap_args[@]}" -gt 0 ]; then
    MAC_OPENSH_EXPECTED_OPENCLAW_SANDBOX="$expected_openclaw" \
      OSH_RUNTIME_IMAGE_REF="$OPENSHELL_RUNTIME_IMAGE" \
      "$bootstrap" "${bootstrap_args[@]}"
  else
    # Bash 3.2 with set -u rejects an expansion of an empty declared array.
    MAC_OPENSH_EXPECTED_OPENCLAW_SANDBOX="$expected_openclaw" \
      OSH_RUNTIME_IMAGE_REF="$OPENSHELL_RUNTIME_IMAGE" \
      "$bootstrap"
  fi
  OPENSHELL_BOOTSTRAPPED=1
}

verify_managed_openshell_runtime() {
  [ "$OPENSHELL_BOOTSTRAPPED" = 1 ] || return 0

  local cli="$MAC_HOME/bin/openshell" docker_bin inventory containers report
  local cli_version_text cli_version expected_supervisor container_id sandbox_name
  local supervisor_path supervisor_version supervisor_digest expected_openclaw="" managed_count=0
  [ -x "$cli" ] || die "reviewed OpenShell CLI is absent after bootstrap"
  docker_bin="$(command -v docker || true)"
  if [ -z "$docker_bin" ] && [ "$OS_KIND" = darwin ]; then
    for docker_bin in /Applications/Docker.app/Contents/Resources/bin/docker /opt/homebrew/bin/docker /usr/local/bin/docker; do
      [ -x "$docker_bin" ] && break
    done
  fi
  [ -x "$docker_bin" ] || die "Docker CLI is absent after OpenShell bootstrap"

  inventory="$LOG_DIR/openshell-sandbox-inventory-${DEPLOY_TS}.json"
  OPENSHELL_GATEWAY_ENDPOINT=http://127.0.0.1:17670 \
    "$cli" sandbox list --limit 1000 --output json > "$inventory" \
    || die "OpenShell sandbox inventory is unreadable after bootstrap"
  "$PY" - "$inventory" <<'PY'
import json
import sys
from pathlib import Path

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(value, list):
    raise SystemExit("OpenShell sandbox inventory is not a list")
PY

  cli_version_text="$("$cli" --version 2>&1)"
  if [[ "$cli_version_text" =~ ([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    cli_version="${BASH_REMATCH[1]}"
  else
    die "could not derive the reviewed OpenShell CLI version"
  fi
  expected_supervisor="openshell-sandbox $cli_version"
  supervisor_digest="$($PY - "$MAC_HOME/openshell/gateway.toml" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'^supervisor_image\s*=\s*"[^"\n]+@sha256:([0-9a-f]{64})"$', text, re.MULTILINE)
if not match:
    raise SystemExit("OpenShell gateway config lacks an immutable supervisor image")
print(match.group(1))
PY
)" || die "could not verify the configured OpenShell supervisor digest"
  containers="$(mktemp "${TMPDIR:-/tmp}/mac-openshell-conformance.XXXXXX")"
  while IFS= read -r container_id; do
    [ -n "$container_id" ] || continue
    managed_count=$((managed_count + 1))
    sandbox_name="$("$docker_bin" inspect --format \
      '{{ index .Config.Labels "openshell.ai/sandbox-name" }}' "$container_id")"
    supervisor_path="$("$docker_bin" inspect --format \
      '{{range .Mounts}}{{if eq .Destination "/opt/openshell/bin/openshell-sandbox"}}{{.Source}}{{end}}{{end}}' \
      "$container_id")"
    [ -n "$sandbox_name" ] || {
      rm -f "$containers"
      die "managed OpenShell container $container_id has no sandbox name"
    }
    [ -r "$supervisor_path" ] || {
      rm -f "$containers"
      die "managed sandbox $sandbox_name lacks a readable supervisor bind"
    }
    case "$supervisor_path" in
      */docker-supervisor/sha256-"$supervisor_digest"/openshell-sandbox) ;;
      *)
        rm -f "$containers"
        die "managed sandbox $sandbox_name is bound to an unreviewed supervisor digest"
        ;;
    esac
    if [ "$OS_KIND" = darwin ]; then
      # The bind is a Linux ELF inside Docker Desktop and cannot execute on the
      # Darwin host. verify_supervisor_image already ran this exact digest in
      # Docker; the immutable cache path proves each sandbox uses that binary.
      supervisor_version="$expected_supervisor (digest-bound)"
    else
      supervisor_version="$("$supervisor_path" --version 2>&1 || true)"
      [ "$supervisor_version" = "$expected_supervisor" ] || {
        rm -f "$containers"
        die "managed sandbox $sandbox_name uses '$supervisor_version', expected '$expected_supervisor'"
      }
    fi
    printf '%s\t%s\t%s\t%s\n' \
      "$container_id" "$sandbox_name" "$supervisor_version" "$supervisor_digest" \
      >> "$containers"
  done < <("$docker_bin" ps -a \
    --filter label=openshell.ai/managed-by=openshell --format '{{.ID}}')

  if [ "${HERMES_GATEWAY_IMPL:-hermes}" = openclaw ]; then
    expected_openclaw="$($PY - "$MAC_HOME/openclaw/service-advertisement.json" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
sandbox = (((value.get("openclaw_runtime") or {}).get("confinement") or {}).get("sandbox"))
if not isinstance(sandbox, str) or not re.fullmatch(r"mac-openclaw-[a-z0-9][a-z0-9._-]*", sandbox):
    raise SystemExit("verified OpenClaw advertisement has no safe sandbox identity")
print(sandbox)
PY
)" || {
      rm -f "$containers"
      die "could not verify the advertised OpenClaw sandbox identity"
    }
    OPENSHELL_GATEWAY_ENDPOINT=http://127.0.0.1:17670 \
      "$cli" sandbox get "$expected_openclaw" >/dev/null \
      || {
        rm -f "$containers"
        die "advertised OpenClaw sandbox is not decodable after OpenShell bootstrap"
      }
    [ "$managed_count" -gt 0 ] || {
      rm -f "$containers"
      die "OpenClaw is advertised but no managed supervisor container exists"
    }
    awk -F '\t' -v expected="$expected_openclaw" \
      '$2 == expected { found = 1 } END { exit(found ? 0 : 1) }' \
      "$containers" || {
      rm -f "$containers"
      die "advertised OpenClaw sandbox has no exact managed container"
    }
  fi

  report="$LOG_DIR/openshell-runtime-conformance-${DEPLOY_TS}.json"
  "$PY" - "$containers" "$report" "$cli_version" "$expected_openclaw" <<'PY'
import json
import sys
from pathlib import Path

rows_path, report_path, cli_version, openclaw = sys.argv[1:]
rows = []
for line in Path(rows_path).read_text(encoding="utf-8").splitlines():
    if not line:
        continue
    container_id, sandbox, supervisor, supervisor_digest = line.split("\t")
    rows.append({
        "container_id": container_id,
        "sandbox": sandbox,
        "supervisor_image_digest": "sha256:" + supervisor_digest,
        "supervisor_version": supervisor,
    })
Path(report_path).write_text(json.dumps({
    "schema": "mac.openshell_runtime_conformance.v1",
    "cli_version": cli_version,
    "expected_openclaw_sandbox": openclaw or None,
    "managed_containers": rows,
    "status": "passed",
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  rm -f "$containers"
  chmod 0600 "$inventory" "$report"
  log "OpenShell runtime conformance passed for $managed_count managed container(s)"
}

remove_openshell_firewall_chain() {
  local ipt rule rules refreshed_rules inspected=0 parts=()
  for ipt in iptables ip6tables; do
    command -v "$ipt" >/dev/null 2>&1 || continue
    inspected=1
    if ! rules="$(sudo -n "$ipt" -S 2>/dev/null)"; then
      die "could not inspect deploy-managed OpenShell firewall state in $ipt"
    fi
    while sudo -n "$ipt" -C INPUT -p tcp --dport 17670 -j MAC_OPENSH_GW >/dev/null 2>&1; do
      sudo -n "$ipt" -D INPUT -p tcp --dport 17670 -j MAC_OPENSH_GW
    done
    if printf '%s\n' "$rules" | grep -Fqx -- '-N MAC_OPENSH_GW'; then
      sudo -n "$ipt" -F MAC_OPENSH_GW
      sudo -n "$ipt" -X MAC_OPENSH_GW
    fi

    # Historical MAC bootstraps installed one direct INPUT rule on the then
    # current default NIC instead of the named chain. Remove only that exact
    # legacy shape (interface-scoped TCP/17670 DROP), and only after the caller
    # has proved this host contains MAC-owned OpenShell state.
    while IFS= read -r rule; do
      [ -n "$rule" ] || continue
      read -r -a parts <<<"$rule"
      parts[0]="-D"
      sudo -n "$ipt" "${parts[@]}"
    done < <(printf '%s\n' "$rules" | awk '
        $1 == "-A" && $2 == "INPUT" && $3 == "-i" &&
        $5 == "-p" && $6 == "tcp" &&
        $(NF-3) == "--dport" && $(NF-2) == "17670" &&
        $(NF-1) == "-j" && $NF == "DROP" { print }
      ')
    if ! refreshed_rules="$(sudo -n "$ipt" -S 2>/dev/null)"; then
      die "could not verify deploy-managed OpenShell firewall removal in $ipt"
    fi
    if printf '%s\n' "$refreshed_rules" | grep -Eq -- \
        '^-N MAC_OPENSH_GW$|^-A INPUT .*--dport 17670 .* -j MAC_OPENSH_GW$'; then
      die "could not remove deploy-managed OpenShell firewall state from $ipt"
    fi
    if printf '%s\n' "$refreshed_rules" | awk '
        $1 == "-A" && $2 == "INPUT" && $3 == "-i" &&
        $5 == "-p" && $6 == "tcp" &&
        $(NF-3) == "--dport" && $(NF-2) == "17670" &&
        $(NF-1) == "-j" && $NF == "DROP" { found=1 }
        END { exit found ? 0 : 1 }
      '; then
      die "could not remove legacy deploy-managed OpenShell firewall state from $ipt"
    fi
  done
  [ "$inspected" = 1 ] \
    || die "could not inspect deploy-managed OpenShell firewall state: iptables is unavailable"
}

openshell_firewall_state_present() {
  local ipt rules
  for ipt in iptables ip6tables; do
    command -v "$ipt" >/dev/null 2>&1 || continue
    if ! rules="$(sudo -n "$ipt" -S 2>/dev/null)"; then
      return 2
    fi
    if printf '%s\n' "$rules" | grep -Eq -- \
        '^-N MAC_OPENSH_GW$|^-A INPUT .*--dport 17670 .* -j MAC_OPENSH_GW$'; then
      return 0
    fi
    if printf '%s\n' "$rules" | awk '
        $1 == "-A" && $2 == "INPUT" && $3 == "-i" &&
        $5 == "-p" && $6 == "tcp" &&
        $(NF-3) == "--dport" && $(NF-2) == "17670" &&
        $(NF-1) == "-j" && $NF == "DROP" { found=1 }
        END { exit found ? 0 : 1 }
      '; then
      return 0
    fi
  done
  return 1
}

assert_no_openshell_gateway_listener() {
  local listeners
  if command -v ss >/dev/null 2>&1; then
    if ! listeners="$(ss -H -ltn 2>/dev/null)"; then
      die "could not inspect TCP listeners before removing the OpenShell firewall"
    fi
    if printf '%s\n' "$listeners" | awk '
        $1 == "LISTEN" && $4 ~ /:17670$/ { found=1 }
        END { exit found ? 0 : 1 }
      '; then
      die "refusing to remove the OpenShell firewall while TCP/17670 is listening"
    fi
    return 0
  fi

  # iproute2 supplies ss on supported Linux fleet nodes. Keep a socket-bind
  # fallback for minimal images, and fail closed on any ambiguous result.
  "$PY" - <<'PY'
import errno
import socket

checks = (
    (socket.AF_INET, ("0.0.0.0", 17670)),
    (socket.AF_INET6, ("::", 17670)),
)
for family, address in checks:
    try:
        probe = socket.socket(family, socket.SOCK_STREAM)
    except OSError as exc:
        if exc.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT}:
            continue
        raise SystemExit("could not create listener probe: %s" % exc)
    try:
        if family == socket.AF_INET6:
            probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        probe.bind(address)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise SystemExit("TCP/17670 is still in use")
        if family == socket.AF_INET6 and exc.errno in {
            errno.EADDRNOTAVAIL,
            errno.EAFNOSUPPORT,
        }:
            continue
        raise SystemExit("could not prove TCP/17670 is unused: %s" % exc)
    finally:
        probe.close()
PY
}

reconcile_disabled_optional_openshell() {
  openshell_disable_requested || return 0
  log "explicitly disabling optional OpenShell runtime and removing deploy-managed service state"

  local openshell_dir="$MAC_HOME/openshell" cli="" docker_bin="" registration_json=""
  local systemd_gateway="" systemd_firewall="" supervisor_gateway=""
  local supervisor_firewall="" firewall_script="" expected_gateway_command=""
  local gateway_process_pattern=""
  local managed_state=0 gateway_owned=0 firewall_owned=0 firewall_probe_status=0
  local systemd_gateway_owned=0 supervisor_gateway_owned=0
  local systemd_firewall_owned=0 supervisor_firewall_owned=0
  if [ -e "$openshell_dir/gateway.toml" ] \
      || [ -e "$openshell_dir/run-gateway.sh" ] \
      || [ -e "$openshell_dir/runtime-image-ref" ] \
      || [ -e "$openshell_dir/image-source-sha" ]; then
    managed_state=1
  fi
  if [ "$OS_KIND" = "darwin" ]; then
    for docker_bin in \
      "$(command -v docker 2>/dev/null || true)" \
      /Applications/Docker.app/Contents/Resources/bin/docker \
      /opt/homebrew/bin/docker \
      /usr/local/bin/docker; do
      [ -n "$docker_bin" ] && [ -x "$docker_bin" ] && break
      docker_bin=""
    done
    if [ -n "$docker_bin" ]; then
      if "$docker_bin" info >/dev/null 2>&1; then
        if "$docker_bin" inspect openshell-gw >/dev/null 2>&1; then
          # Current deployments carry explicit ownership labels. Accept the
          # old unlabeled container only when its immutable shape proves the
          # legacy MAC gateway: /osh is this MAC_HOME, port 17670 is loopback
          # only, and the command uses the MAC-owned gateway config.
          if [ "$("$docker_bin" inspect --format '{{ index .Config.Labels "mac.owner" }}:{{ index .Config.Labels "mac.kind" }}' openshell-gw 2>/dev/null || true)" = "mac:openshell-gateway" ] \
              || { [ "$managed_state" = 1 ] \
                && "$docker_bin" inspect openshell-gw | "$PY" -c '
import json
import os
import sys

container = json.load(sys.stdin)[0]
expected = os.path.abspath(sys.argv[1])
mount_ok = any(
    os.path.abspath(str(item.get("Source") or "")) == expected
    and item.get("Destination") == "/osh"
    for item in container.get("Mounts") or []
)
ports = ((container.get("NetworkSettings") or {}).get("Ports") or {}).get("17670/tcp") or []
port_ok = len(ports) == 1 and ports[0].get("HostIp") == "127.0.0.1" and ports[0].get("HostPort") == "17670"
command = (container.get("Config") or {}).get("Cmd") or []
command_ok = command == ["/osh/openshell-gateway", "--config", "/osh/gateway.toml"]
raise SystemExit(0 if mount_ok and port_ok and command_ok else 1)
' "$openshell_dir"; }; then
            managed_state=1
            gateway_owned=1
            "$docker_bin" rm -f openshell-gw >/dev/null
          else
            log "leaving non-MAC Docker container named openshell-gw untouched"
          fi
        fi
        if [ "$gateway_owned" = 1 ] \
            && "$docker_bin" inspect openshell-gw >/dev/null 2>&1; then
          die "deploy-managed Darwin OpenShell gateway container is still present"
        fi
      elif [ "$managed_state" = 1 ]; then
        die "cannot verify deploy-managed Darwin OpenShell gateway removal because Docker is unavailable"
      fi
    elif [ "$managed_state" = 1 ]; then
      die "cannot remove deploy-managed Darwin OpenShell gateway because Docker is unavailable"
    fi
  else
    # Linux bootstrap creates root-owned firewall persistence and may use
    # either a user systemd gateway or supervisord. Fleet provisioning already
    # requires non-interactive sudo; fail instead of leaving a plaintext stale
    # gateway reachable when that authority is unavailable.
    sudo -n true >/dev/null

    systemd_gateway="$HOME/.config/systemd/user/openshell-gateway.service"
    systemd_firewall="/etc/systemd/system/mac-openshell-firewall.service"
    supervisor_gateway="/etc/supervisor/conf.d/openshell-gateway.conf"
    supervisor_firewall="/etc/supervisor/conf.d/mac-openshell-firewall.conf"
    firewall_script="/usr/local/sbin/mac-openshell-firewall.sh"
    expected_gateway_command="$HOME/.local/bin/openshell-gateway --config $openshell_dir/gateway.toml"
    gateway_process_pattern="$(
      "$PY" -c 'import re, sys; print("^" + re.escape(sys.argv[1]) + "$")' \
        "$expected_gateway_command"
    )"

    if [ -f "$systemd_gateway" ] \
        && { grep -Fqx 'ExecStart=%h/.mac/openshell/run-gateway.sh' "$systemd_gateway" \
          || grep -Fqx "ExecStart=$openshell_dir/run-gateway.sh" "$systemd_gateway" \
          || grep -Fqx 'ExecStart=%h/.local/bin/openshell-gateway --config %h/.mac/openshell/gateway.toml' "$systemd_gateway" \
          || grep -Fqx "ExecStart=$expected_gateway_command" "$systemd_gateway"; }; then
      gateway_owned=1
      systemd_gateway_owned=1
      managed_state=1
    fi
    if sudo -n test -f "$supervisor_gateway" \
        && { sudo -n grep -Fqx "command=$openshell_dir/run-gateway.sh" "$supervisor_gateway" \
          || sudo -n grep -Fqx "command=$expected_gateway_command" "$supervisor_gateway"; }; then
      gateway_owned=1
      supervisor_gateway_owned=1
      managed_state=1
    fi
    if sudo -n test -f "$systemd_firewall" \
        && sudo -n grep -Fqx 'ExecStart=/usr/local/sbin/mac-openshell-firewall.sh' "$systemd_firewall"; then
      firewall_owned=1
      systemd_firewall_owned=1
      managed_state=1
    fi
    if sudo -n test -f "$supervisor_firewall" \
        && sudo -n grep -Fqx 'command=/usr/local/sbin/mac-openshell-firewall.sh' "$supervisor_firewall"; then
      firewall_owned=1
      supervisor_firewall_owned=1
      managed_state=1
    fi
    if sudo -n test -f "$firewall_script" \
        && sudo -n grep -Eq 'MAC_OPENSH_GW|--dport 17670' "$firewall_script"; then
      firewall_owned=1
      managed_state=1
    fi

    # A pre-service-manager bootstrap may have left only the durable MAC
    # markers plus the exact gateway process.  Treat that immutable command
    # shape as owned when (and only when) the marker has already established
    # MAC ownership, so removing an old unit file cannot strand the listener.
    if [ "$managed_state" = 1 ] \
        && pgrep -f "$gateway_process_pattern" >/dev/null 2>&1; then
      gateway_owned=1
    fi
    if [ "$managed_state" = 1 ]; then
      if openshell_firewall_state_present; then
        firewall_owned=1
      else
        firewall_probe_status=$?
        [ "$firewall_probe_status" = 1 ] \
          || die "could not inspect existing OpenShell firewall state"
      fi
    fi

    if [ "$systemd_gateway_owned" = 1 ]; then
      if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        run_user_systemctl disable --now openshell-gateway.service >/dev/null 2>&1 \
          || die "could not stop deploy-managed OpenShell systemd gateway"
        if run_user_systemctl is-active --quiet openshell-gateway.service; then
          die "deploy-managed OpenShell systemd gateway is still active"
        fi
      fi
      rm -f "$systemd_gateway"
      if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        run_user_systemctl daemon-reload >/dev/null 2>&1 \
          || die "could not reload the user systemd manager after OpenShell removal"
      fi
    fi
    if [ "$supervisor_gateway_owned" = 1 ]; then
      command -v supervisorctl >/dev/null 2>&1 \
        || die "cannot stop deploy-managed OpenShell supervisor gateway: supervisorctl is unavailable"
      stop_supervisord_program_if_present openshell-gateway
      mac_launchd_remove_file_and_fsync "$supervisor_gateway" system
      run_supervisorctl reread >/dev/null
      run_supervisorctl update >/dev/null
      if run_supervisorctl status openshell-gateway 2>/dev/null \
          | grep -q '[[:space:]]RUNNING[[:space:]]'; then
        die "deploy-managed OpenShell supervisord gateway is still running"
      fi
    fi

    if [ "$gateway_owned" = 1 ]; then
      sudo -n pkill -f "$gateway_process_pattern" >/dev/null 2>&1 || true
      if pgrep -f "$gateway_process_pattern" >/dev/null 2>&1; then
        die "deploy-managed OpenShell gateway process is still running"
      fi
    fi
    if [ "$managed_state" = 1 ]; then
      assert_no_openshell_gateway_listener
    fi
    if [ "$firewall_owned" = 1 ]; then
      if [ "$systemd_firewall_owned" = 1 ]; then
        if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
          run_systemctl disable --now mac-openshell-firewall.service >/dev/null 2>&1 \
            || die "could not stop deploy-managed OpenShell firewall service"
          if run_systemctl is-active --quiet mac-openshell-firewall.service; then
            die "deploy-managed OpenShell firewall service is still active"
          fi
        fi
        sudo -n rm -f "$systemd_firewall"
        if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
          run_systemctl daemon-reload >/dev/null
        fi
      fi
      if [ "$supervisor_firewall_owned" = 1 ]; then
        command -v supervisorctl >/dev/null 2>&1 \
          || die "cannot remove deploy-managed OpenShell supervisor firewall: supervisorctl is unavailable"
        stop_supervisord_program_if_present mac-openshell-firewall
        mac_launchd_remove_file_and_fsync "$supervisor_firewall" system
        run_supervisorctl reread >/dev/null
        run_supervisorctl update >/dev/null
      fi
      # Persistence is now disabled while the proven listener-free rules are
      # still in place. Remove the live rules last so no manager can race this
      # transaction by reinstalling them after verification.
      remove_openshell_firewall_chain
      sudo -n rm -f "$firewall_script"
    fi
  fi

  if [ "$managed_state" = 1 ]; then
    for cli in "$MAC_HOME/bin/openshell" "$HOME/.local/bin/openshell"; do
      [ -x "$cli" ] || continue
      registration_json="$(
        env -u OPENSHELL_GATEWAY_ENDPOINT -u OPENSHELL_GATEWAY \
          "$cli" gateway list --output json
      )" || die "could not inspect OpenShell gateway registrations during disable"
      if printf '%s\n' "$registration_json" | "$PY" -c '
import json
import sys

items = json.load(sys.stdin)
raise SystemExit(0 if any(
    item.get("name") == "openshell"
    and item.get("endpoint") == "http://127.0.0.1:17670"
    for item in items
) else 1)
'; then
        env -u OPENSHELL_GATEWAY_ENDPOINT -u OPENSHELL_GATEWAY \
          "$cli" gateway remove openshell >/dev/null
      fi
      break
    done
  fi

  rm -f \
    "$openshell_dir/runtime-image-ref" \
    "$openshell_dir/image-source-sha" \
    "$openshell_dir/gateway.toml" \
    "$openshell_dir/run-gateway.sh"
  log "optional OpenShell runtime disabled"
}

prepare_work_package_pipeline_storage() {
  case "$(printf '%s' "${MAC_WORK_PACKAGE_PIPELINE_ENABLED:-0}" | tr 'A-Z' 'a-z')" in
    1|true|yes|on) ;;
    *) return 0 ;;
  esac
  if [ "$AGENT" != "$SHARED_SERVICES_MANAGER_AGENT" ]; then
    log "ERROR: work-package pipeline may run only on the control-plane hub"
    return 1
  fi
  case "$(printf '%s' "${MAC_WORK_PACKAGE_LANDING_ENABLED:-0}" | tr 'A-Z' 'a-z')" in
    1|true|yes|on) ;;
    *)
      log "ERROR: work-package pipeline requires explicit landing enablement"
      return 1
      ;;
  esac
  local bundle_dir="${MAC_WORK_PACKAGE_BUNDLE_DIR:-}"
  if [ -z "$bundle_dir" ]; then
    log "ERROR: work-package pipeline requires MAC_WORK_PACKAGE_BUNDLE_DIR"
    return 1
  fi
  log "preparing durable work-package bundle storage"
  "$PY" - "$bundle_dir" <<'PY'
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
if not path.is_absolute():
    raise SystemExit("work-package bundle directory must be absolute")

# Refuse a symlink at the leaf or at any already-existing parent. The bundle
# cache holds untrusted-repository inputs and is later chmod'd by the runtime;
# following an operator-unreviewed link here would apply that authority to a
# different filesystem location.
probe = path
while True:
    try:
        mode = probe.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(mode):
            raise SystemExit("work-package bundle path contains a symlink: %s" % probe)
    if probe.parent == probe:
        break
    probe = probe.parent

path.mkdir(parents=True, exist_ok=True)
metadata = path.lstat()
if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
    raise SystemExit("work-package bundle path is not a regular directory")
if metadata.st_uid != os.getuid():
    raise SystemExit("work-package bundle directory is not owned by the mac service user")
path.chmod(0o700)
if stat.S_IMODE(path.stat().st_mode) != 0o700:
    raise SystemExit("work-package bundle directory mode is not 0700")
PY
}

install_or_validate_shared_services() {
  if qdrant_install_enabled; then
    log "installing hub-managed Qdrant shared memory service"
    if [ -n "$QDRANT_BIND_ADDR_CONFIGURED" ]; then
      export QDRANT_BIND_ADDR="$QDRANT_BIND_ADDR_CONFIGURED"
    else
      unset QDRANT_BIND_ADDR
    fi
    export QDRANT_PORT="$QDRANT_PORT_CONFIGURED"
    export QDRANT_IMAGE="$QDRANT_IMAGE_CONFIGURED"
    export QDRANT_MEMORY_LIMIT="$QDRANT_MEMORY_LIMIT_CONFIGURED"
    export QDRANT_CONTAINER_NAME="${FLEET_NAME}-qdrant"
    if [ -n "$QDRANT_DATA_DIR_CONFIGURED" ]; then
      export QDRANT_DATA_DIR="$QDRANT_DATA_DIR_CONFIGURED"
    fi
    export FLEET_NAME="$FLEET_NAME"
    export QDRANT_SUPERVISOR="$SUPERVISOR_KIND"
    MAC_HOME="$MAC_HOME" HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" WORKSPACE="$SRC_DIR" \
      bash "$SRC_DIR/deploy/install-qdrant-service.sh"
    reload_mac_env
  else
    log "using hub-managed shared services from $SHARED_SERVICES_MANAGER_AGENT"
  fi
  validate_qdrant_endpoint
}

install_or_validate_web_search_service() {
  if firecrawl_install_enabled; then
    log "installing hub-managed Firecrawl-compatible web search service"
    if [ -n "$FIRECRAWL_BIND_ADDR_CONFIGURED" ]; then
      export FIRECRAWL_BIND_ADDR="$FIRECRAWL_BIND_ADDR_CONFIGURED"
    else
      unset FIRECRAWL_BIND_ADDR
    fi
    export FIRECRAWL_PORT="$FIRECRAWL_PORT_CONFIGURED"
    export FLEET_NAME="$FLEET_NAME"
    export FIRECRAWL_SUPERVISOR="$SUPERVISOR_KIND"
    MAC_HOME="$MAC_HOME" HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" WORKSPACE="$SRC_DIR" \
      bash "$SRC_DIR/deploy/install-firecrawl-gateway.sh"
    reload_mac_env
  else
    log "using hub-managed Firecrawl web search from $SHARED_SERVICES_MANAGER_AGENT"
  fi
  validate_firecrawl_endpoint
}

install_or_validate_publish_service() {
  webdav_enabled || return 0
  if webdav_install_enabled; then
    log "installing hub-managed public artifact WebDAV service"
    export WEBDAV_BIND_ADDR="$WEBDAV_BIND_ADDR_CONFIGURED"
    export WEBDAV_PORT="$WEBDAV_PORT_CONFIGURED"
    if [ -n "$WEBDAV_ROOT_CONFIGURED" ]; then
      export WEBDAV_ROOT="$WEBDAV_ROOT_CONFIGURED"
    else
      unset WEBDAV_ROOT
    fi
    export WEBDAV_PUBLIC_PATH="$WEBDAV_PUBLIC_PATH_CONFIGURED"
    export WEBDAV_PUBLIC_URL="$WEBDAV_URL_CONFIGURED"
    export WEBDAV_MAX_UPLOAD_BYTES="$WEBDAV_MAX_UPLOAD_BYTES_CONFIGURED"
    export FLEET_NAME="$FLEET_NAME"
    export WEBDAV_SUPERVISOR="$SUPERVISOR_KIND"
    MAC_HOME="$MAC_HOME" HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" WORKSPACE="$SRC_DIR" \
      bash "$SRC_DIR/deploy/install-webdav-server.sh"
    reload_mac_env
  else
    log "using hub-managed WebDAV publishing from $SHARED_SERVICES_MANAGER_AGENT"
  fi
  validate_webdav_endpoint
}

write_hermes_memory_topology() {
  log "writing Hermes memory topology"
  "$PY" - "$HOME/.hermes/mac-memory-topology.json" "$HOME/.hermes/.env" <<'PY'
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

topology_path = Path(sys.argv[1])
hermes_env_path = Path(sys.argv[2])
topology_path.parent.mkdir(parents=True, exist_ok=True)


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def connection_url(raw: str) -> str:
    parsed = urllib.parse.urlsplit(raw.strip())
    if not parsed.scheme or not parsed.netloc:
        return raw.strip()
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def set_env(path: Path, updates: dict[str, str | None]) -> None:
    lines: list[str] = []
    seen: set[str] = set()
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            if updates[key] is not None:
                output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key in sorted(updates):
        if key not in seen and updates[key] is not None:
            output.append(f"{key}={updates[key]}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    path.chmod(0o600)


agent = os.environ["AGENT"]
hub_url = os.environ.get("MAC_HUB_URL") or os.environ.get("HUB_URL") or ""
hub_agent = os.environ.get("MAC_SHARED_SERVICES_MANAGER_AGENT") or os.environ.get("SHARED_SERVICES_MANAGER_AGENT") or agent
qdrant_url = (
    os.environ.get("QDRANT_URL")
    or os.environ.get("QDRANT_ADDRESS")
    or os.environ.get("QDRANT_FLEET_URL")
    or ""
)
safe_qdrant_url = connection_url(qdrant_url) if qdrant_url else ""
firecrawl_url = (
    os.environ.get("FIRECRAWL_API_URL")
    or os.environ.get("FIRECRAWL_GATEWAY_URL")
    or ""
)
safe_firecrawl_url = connection_url(firecrawl_url) if firecrawl_url else ""
required = True
degraded_allowed = False
firecrawl_required = True
firecrawl_degraded_allowed = False

topology = {
    "schema": "mac.hermes.memory_topology.v1",
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "agent": agent,
    "hub": {
        "agent": hub_agent,
        "url": connection_url(hub_url) if hub_url else "",
        "manages_shared_services": True,
    },
    "local_memory": {
        "owner": "hermes",
        "home": os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"),
        "soul": "SOUL.md",
        "user_profile": "USER.md",
        "memory_user_profile": "memories/USER.md",
        "long_term_memory": "memories/MEMORY.md",
        "legacy_user_profile": "USER.md",
        "legacy_long_term_memory": "MEMORY.md",
        "user_profile_candidates": ["memories/USER.md", "USER.md"],
        "long_term_memory_candidates": ["memories/MEMORY.md", "MEMORY.md"],
        "conversation_state": "state.db",
    },
    "mac_memory": {
        "owner": "mac",
        "purpose": "operational provenance, task ledger, vector_refs pointers",
        "database": os.environ.get("MAC_DB", ""),
    },
    "shared_services": {
        "qdrant": {
            "owner": "hub",
            "manager_agent": hub_agent,
            "role": "shared_level2_memory",
            "url": safe_qdrant_url,
            "required": required,
            "mandatory": True,
            "degraded_allowed": degraded_allowed,
            "api_key_env": "QDRANT_API_KEY" if os.environ.get("QDRANT_API_KEY") else "",
        },
        "firecrawl": {
            "owner": "hub",
            "manager_agent": hub_agent,
            "role": "shared_web_search",
            "url": safe_firecrawl_url,
            "required": firecrawl_required,
            "mandatory": True,
            "degraded_allowed": firecrawl_degraded_allowed,
            "api_key_env": "FIRECRAWL_API_KEY" if os.environ.get("FIRECRAWL_API_KEY") else "",
        }
    },
}
topology_path.write_text(json.dumps(topology, indent=2, sort_keys=True) + "\n", encoding="utf-8")
topology_path.chmod(0o600)

updates = {
    "MAC_MEMORY_TOPOLOGY_FILE": str(topology_path),
    "MAC_SHARED_SERVICES_MANAGER_AGENT": hub_agent,
    "MAC_REQUIRE_QDRANT_MEMORY": "1",
    "MAC_QDRANT_MEMORY_ROLE": "shared_level2",
    "MAC_REQUIRE_FIRECRAWL": "1",
    "MAC_WEB_SEARCH_PROVIDER": "firecrawl" if safe_firecrawl_url else "",
}
if safe_qdrant_url:
    updates["QDRANT_URL"] = safe_qdrant_url
    updates["QDRANT_ADDRESS"] = safe_qdrant_url
    updates["QDRANT_FLEET_URL"] = safe_qdrant_url
if safe_firecrawl_url:
    updates["FIRECRAWL_API_URL"] = safe_firecrawl_url
    updates["FIRECRAWL_GATEWAY_URL"] = safe_firecrawl_url
    updates["FIRECRAWL_API_KEY"] = os.environ.get("FIRECRAWL_API_KEY") or "none"
    updates["HERMES_WEB_SEARCH_BACKEND"] = "firecrawl"
    updates["HERMES_WEB_EXTRACT_BACKEND"] = "firecrawl"
set_env(hermes_env_path, updates)
print(
    "memory topology: agent=%s hub=%s qdrant=%s required=%s firecrawl=%s firecrawl_required=%s"
    % (agent, hub_agent, safe_qdrant_url or "disabled", required, safe_firecrawl_url or "disabled", firecrawl_required)
)
PY
}

write_hermes_runtime_context() {
  log "writing Hermes task/project runtime context"
  "$VENV/bin/python" -m mac.hermes_runtime \
    "$HOME/.hermes/mac-runtime-context.json" \
    "$HOME/.hermes/mac-runtime-context.md" \
    "$HOME/.hermes/.env" \
    --agent-name "$AGENT" \
    --fleet-name "$FLEET_NAME" \
    --mac-url "${MAC_HUB_URL:-${HUB_URL:-}}" \
    --hermes-home "${HERMES_HOME:-$HOME/.hermes}" \
    --mac-home "$MAC_HOME" \
    --workspace "$SRC_DIR" \
    --tenant-id "${MAC_FLEET_TENANT_ID:-}" \
    --persona-id "${MAC_HERMES_PERSONA_ID:-}" \
    --hermes-instance-id "${MAC_HERMES_INSTANCE_ID:-}" \
    --agent-id "${MAC_AGENT_ID:-}"
  set -a
  set +u
  . "$HOME/.hermes/.env"
  set -u
  set +a
}

verify_hermes_prompt_bridge() {
  log "verifying Hermes prompt bridge sees MAC runtime context"
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
  MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN="${MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN:-$HOME/.hermes/mac-runtime-context.md}" \
  PYTHONPATH="$HERMES_DIR:${PYTHONPATH:-}" \
  "$VENV/bin/python" - "$SRC_DIR" <<'PY'
from __future__ import annotations

import sys

from agent import prompt_builder

workspace = sys.argv[1]
required = [
    "MAC Task and Project Runtime",
    "First-Class Objects",
    "MAC Vocabulary",
    "`tasks`: authority `mac`",
    "`projects`: authority `mac`",
    "`agents`: authority `mac`",
    "Project Bridge",
    "Agent View",
    "Dashboard Views",
    "/ui?view=work",
    "Direct Session Parity",
    "mac-task-executor",
    "mac-hermes work-context",
    "mac-hermes tasks",
    "mac-hermes add-child-task",
    "mac-hermes projects",
    "mac-hermes project-items",
    "mac-hermes agents",
    "shell_execution",
    "workspace_file_access",
    "mac task ready",
    "git push",
]
runtime_context = prompt_builder._load_mac_runtime_context()
missing = [item for item in required if item not in runtime_context]
if missing:
    raise SystemExit("Hermes MAC runtime prompt bridge did not load: %s" % ", ".join(missing))
prompt = prompt_builder.build_context_files_prompt(cwd=workspace, skip_soul=True)
missing = [item for item in required if item not in prompt]
if missing:
    raise SystemExit("Hermes MAC runtime prompt is missing: %s" % ", ".join(missing))
print("Hermes prompt bridge verified for %s" % workspace)
PY
}

register_hermes_runtime_identity() {
  log "registering Hermes runtime identity in mac"
  "$VENV/bin/python" - <<'PY'
from __future__ import annotations

import os

from mac.hermes_runtime import stable_id
from mac.models import NotFoundError
from mac.services import (
    ControlPlane,
    DEFAULT_HUB_REVIEWER_AGENT_ID,
    DEFAULT_HUB_REVIEWER_AGENT_NAME,
    DEFAULT_HUB_REVIEWER_MACHINE_ID,
    HUB_REVIEW_VERIFIER_RESOURCE_SCHEMA,
)


def truthy(raw, default=""):
    return str(raw if raw is not None else default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

agent = os.environ["AGENT"]
fleet = os.environ.get("FLEET_NAME") or "mac"
home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
tenant_id = os.environ.get("MAC_FLEET_TENANT_ID") or stable_id("tenant", fleet)
agent_id = os.environ.get("MAC_AGENT_ID") or stable_id("agent", agent)
persona_id = os.environ.get("MAC_HERMES_PERSONA_ID") or stable_id("persona", agent)
instance_id = os.environ.get("MAC_HERMES_INSTANCE_ID") or stable_id("hermes", agent)
shared_services_manager = os.environ.get("SHARED_SERVICES_MANAGER_AGENT") or agent
configured_agent_ids = [
    item.strip()
    for item in os.environ.get("CONFIGURED_AGENT_IDS", "").split(",")
    if item.strip()
]
cp = ControlPlane()
cp.register_tenant(
    fleet,
    tenant_id=tenant_id,
    metadata={"source": "mac-deploy", "fleet": fleet},
)
cp.register_persona(
    tenant_id,
    agent,
    os.path.join(home, "SOUL.md"),
    home,
    persona_id=persona_id,
    metadata={"source": "mac-deploy", "agent_id": agent_id},
)
cp.register_hermes_instance(
    tenant_id,
    agent,
    persona_id=persona_id,
    home_ref=home,
    instance_id=instance_id,
    metadata={"source": "mac-deploy", "agent_id": agent_id, "fleet": fleet},
)
if agent == shared_services_manager:
    fleet_metadata = {"source": "mac-deploy", "fleet": fleet, "hub_agent": agent}
    registered_configured_agent_ids = []
    if truthy(os.environ.get("MAC_REVIEW_HUB_VERIFY")) and truthy(
        os.environ.get("MAC_HUB_REVIEWER_AUTO_REGISTER"),
        "1",
    ):
        reviewer_name = (
            os.environ.get("MAC_HUB_REVIEWER_AGENT_NAME")
            or DEFAULT_HUB_REVIEWER_AGENT_NAME
        )
        reviewer_agent_id = (
            os.environ.get("MAC_HUB_REVIEWER_AGENT_ID")
            or DEFAULT_HUB_REVIEWER_AGENT_ID
        )
        reviewer_machine_id = (
            os.environ.get("MAC_HUB_REVIEWER_MACHINE_ID")
            or DEFAULT_HUB_REVIEWER_MACHINE_ID
        )
        reviewer_machine = cp.register_machine(
            "operator-review",
            labels={
                "source": "mac-deploy",
                "fleet": fleet,
                "role": "hub-reviewer",
                "virtual": True,
            },
            resources={"virtual": True, "review": {"mode": "hub_verify"}},
            trusted=True,
            machine_id=reviewer_machine_id,
        )
        reviewer = cp.register_agent(
            reviewer_machine.id,
            reviewer_name,
            capabilities=["review"],
            resources={
                "virtual": True,
                "hub_review_verifier": {
                    "schema": HUB_REVIEW_VERIFIER_RESOURCE_SCHEMA,
                    "enabled": True,
                    "mode": "hub_verify",
                },
            },
            agent_id=reviewer_agent_id,
            actor="mac-deploy",
        )
        registered_configured_agent_ids.append(reviewer.id)
    for configured_agent_id in configured_agent_ids:
        try:
            cp.get_agent(configured_agent_id)
        except NotFoundError:
            continue
        if configured_agent_id not in registered_configured_agent_ids:
            registered_configured_agent_ids.append(configured_agent_id)
    # Idempotent get-or-create. create_fleet derives the id via
    # stable_id("fleet", fleet), which lowercases the name — so a prior deploy of
    # the same fleet under different case (e.g. "jordanh-GKE" vs "jordanh-gke")
    # shares the id but NOT the case-sensitive name. Look up by the stable id as
    # well as the name, or a re-deploy hits a fleets.id UNIQUE collision instead
    # of reconciling the fleet that is already there.
    fleet_fid = stable_id("fleet", fleet)
    existing_fleet = None
    for _key in (fleet, fleet_fid):
        try:
            existing_fleet = cp.get_fleet(_key)
            break
        except NotFoundError:
            continue
    if existing_fleet is None:
        cp.create_fleet(
            fleet,
            description="Auto-registered deployment fleet",
            metadata=fleet_metadata,
            tenant_id=tenant_id,
            agent_ids=registered_configured_agent_ids,
            fleet_id=fleet_fid,
            actor="mac-deploy",
        )
    else:
        cp.update_fleet(
            existing_fleet.id,
            status="active",
            tenant_id=tenant_id,
            metadata={**existing_fleet.metadata, **fleet_metadata},
            agent_ids=registered_configured_agent_ids,
            actor="mac-deploy",
        )
print("Hermes runtime identity: tenant=%s persona=%s instance=%s agent=%s" % (tenant_id, persona_id, instance_id, agent_id))
PY
}

write_deploy_manifest() {
  local stage="$1" path="$2"
  SRC_BACKUP="$SRC_BACKUP" VENV_BACKUP="$VENV_BACKUP" HERMES_BACKUP="$HERMES_BACKUP" \
  MAC_UNIT_BACKUP="$MAC_UNIT_BACKUP" HERMES_UNIT_BACKUP="$HERMES_UNIT_BACKUP" \
  MAC_AGENT_UNIT_BACKUP="$MAC_AGENT_UNIT_BACKUP" \
  MAC_PLIST_BACKUP="$MAC_PLIST_BACKUP" DARWIN_SYSTEM_PLIST_BACKUP="$DARWIN_SYSTEM_PLIST_BACKUP" \
  DARWIN_SYSTEM_LAUNCHD_ACTIVE="$DARWIN_SYSTEM_LAUNCHD_ACTIVE" \
  DARWIN_GUI_LAUNCHD_ACTIVE="$DARWIN_GUI_LAUNCHD_ACTIVE" \
  DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP="$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP" \
  DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE="$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" \
  HERMES_PLIST_BACKUP="$HERMES_PLIST_BACKUP" \
  MAC_AGENT_PLIST_BACKUP="$MAC_AGENT_PLIST_BACKUP" \
  DARWIN_HERMES_LAUNCHD_ACTIVE="$DARWIN_HERMES_LAUNCHD_ACTIVE" \
  DARWIN_OPENCLAW_LAUNCHD_ACTIVE="$DARWIN_OPENCLAW_LAUNCHD_ACTIVE" \
  DARWIN_NEMOCLAW_LAUNCHD_ACTIVE="$DARWIN_NEMOCLAW_LAUNCHD_ACTIVE" \
  DARWIN_AGENT_LAUNCHD_ACTIVE="$DARWIN_AGENT_LAUNCHD_ACTIVE" \
  ROLLBACK_SCRIPT="$ROLLBACK_SCRIPT" \
  ROLLBACK_INTENT="$ROLLBACK_INTENT" \
  ROLLBACK_INTENT_SHA256="$ROLLBACK_INTENT_SHA256" \
  ROLLBACK_COMPLETION_RECEIPT="$ROLLBACK_COMPLETION_RECEIPT" \
  FLEET_NAME="$FLEET_NAME" \
  MAC_SERVICE_NAME="$MAC_SERVICE_NAME" HERMES_SERVICE_NAME="$HERMES_SERVICE_NAME" OPENCLAW_SERVICE_NAME="$OPENCLAW_SERVICE_NAME" NEMOCLAW_SERVICE_NAME="$NEMOCLAW_SERVICE_NAME" MAC_AGENT_SERVICE_NAME="$MAC_AGENT_SERVICE_NAME" \
  MAC_LAUNCHD_LABEL="$MAC_LAUNCHD_LABEL" DARWIN_SYSTEM_SUPERVISOR_LABEL="$DARWIN_SYSTEM_SUPERVISOR_LABEL" HERMES_LAUNCHD_LABEL="$HERMES_LAUNCHD_LABEL" OPENCLAW_LAUNCHD_LABEL="$OPENCLAW_LAUNCHD_LABEL" NEMOCLAW_LAUNCHD_LABEL="$NEMOCLAW_LAUNCHD_LABEL" MAC_AGENT_LAUNCHD_LABEL="$MAC_AGENT_LAUNCHD_LABEL" \
  MAC_SUPERVISORD_PROG="$MAC_SUPERVISORD_PROG" HERMES_SUPERVISORD_PROG="$HERMES_SUPERVISORD_PROG" OPENCLAW_SUPERVISORD_PROG="$OPENCLAW_SUPERVISORD_PROG" NEMOCLAW_SUPERVISORD_PROG="$NEMOCLAW_SUPERVISORD_PROG" AGENT_SUPERVISORD_PROG="$AGENT_SUPERVISORD_PROG" \
  "$PY" - "$stage" "$path" <<'PY'
import json
import hashlib
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(cmd):
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=8)
    except Exception as exc:
        return {"ok": False, "output": str(exc)}
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def probe(cmd):
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": result.returncode == 0, "returncode": result.returncode}


def py_version(path):
    candidate = Path(path)
    if not candidate.exists():
        return None
    result = run([str(candidate), "--version"])
    text = result.get("stdout") or result.get("stderr")
    return text or None


def file_ref(path):
    candidate = Path(path)
    try:
        exists = candidate.exists()
    except OSError:
        exists = False
    ref = {"path": str(candidate), "exists": exists}
    if exists:
        try:
            stat = candidate.stat()
            ref.update(
                {
                    "kind": "dir" if candidate.is_dir() else "file",
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        except OSError:
            ref["exists"] = False
    return ref


def rollback_contract_summary(stage):
    script = Path(os.environ["ROLLBACK_SCRIPT"])
    intent_path = Path(os.environ["ROLLBACK_INTENT"])
    completion = Path(os.environ["ROLLBACK_COMPLETION_RECEIPT"])

    def private_bytes(path, mode, limit, description):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(path), flags)
        except OSError as exc:
            raise SystemExit(
                "%s is unavailable: %s" % (description, type(exc).__name__)
            )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != mode
                or before.st_size <= 0
                or before.st_size > limit
            ):
                raise SystemExit(description + " is not owner-private and bounded")
            raw = bytearray()
            while len(raw) < before.st_size:
                chunk = os.read(
                    descriptor, min(64 * 1024, before.st_size - len(raw))
                )
                if not chunk:
                    raise SystemExit(description + " was truncated")
                raw.extend(chunk)
            if os.read(descriptor, 1):
                raise SystemExit(description + " grew while reading")
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise SystemExit(description + " changed while reading")
            return bytes(raw)
        finally:
            os.close(descriptor)

    script_raw = private_bytes(
        script, 0o700, 2 * 1024 * 1024, "rollback program"
    )
    intent_raw = private_bytes(
        intent_path, 0o600, 4 * 1024 * 1024, "pre-mutation rollback intent"
    )
    try:
        intent = json.loads(intent_raw)
    except (TypeError, ValueError) as exc:
        raise SystemExit("pre-mutation rollback intent is malformed") from exc
    generation = os.environ["DEPLOY_GENERATION"]
    revision = os.environ["DEPLOY_REV"]
    script_sha256 = hashlib.sha256(script_raw).hexdigest()
    intent_sha256 = hashlib.sha256(intent_raw).hexdigest()
    rollback = intent.get("rollback") if isinstance(intent, dict) else None
    if (
        not isinstance(intent, dict)
        or intent.get("schema") != "mac.fleet_node_rollback_intent.v1"
        or intent.get("status") != "armed"
        or intent.get("generation") != generation
        or intent.get("revision") != revision
        or intent.get("rollback_capable") is not True
        or not isinstance(rollback, dict)
        or rollback.get("path") != str(script)
        or rollback.get("sha256") != script_sha256
        or rollback.get("completion_receipt") != str(completion)
        or (
            os.environ.get("ROLLBACK_INTENT_SHA256")
            and os.environ["ROLLBACK_INTENT_SHA256"] != intent_sha256
        )
    ):
        raise SystemExit("rollback intent differs from the armed generation")
    return {
        "schema": "mac.fleet_node_rollback_contract.v1",
        "status": "armed",
        "authority": "pre_mutation_intent",
        "evidence_stage": stage,
        "generation": generation,
        "revision": revision,
        "path": str(script),
        "sha256": script_sha256,
        "completion_receipt": str(completion),
        "intent": {
            "schema": "mac.fleet_node_rollback_intent.v1",
            "path": str(intent_path),
            "sha256": intent_sha256,
        },
    }


def daemon_quiescence_summary(stage, mac_home):
    gateway_implementation = os.environ.get("HERMES_GATEWAY_IMPL") or "hermes"
    required_phases = ["pre_source", "pre_install", "post_install"]
    if gateway_implementation == "openclaw":
        required_phases = [
            "pre_source",
            "pre_install",
            "pre_verify",
            "pre_finalize",
            "post_install",
        ]
    if stage != "post":
        return {
            "schema": "mac.daemon_resource_quiescence_manifest.v1",
            "status": "pending",
            "gateway_implementation": gateway_implementation,
            "required_phases": required_phases,
        }
    generation = os.environ["MAC_DEPLOY_GENERATION"]
    revision = os.environ["DEPLOY_REV"]
    receipt_path = mac_home / ("daemon-resource-quiescence-%s.json" % generation)
    try:
        metadata = receipt_path.lstat()
        raw = receipt_path.read_bytes()
    except OSError as exc:
        raise SystemExit(
            "post manifest lacks daemon quiescence receipt: %s" % type(exc).__name__
        )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or len(raw) > 4 * 1024 * 1024
    ):
        raise SystemExit("daemon quiescence receipt is not an owner-private bounded file")
    try:
        receipt = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SystemExit("daemon quiescence receipt is malformed") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "mac.daemon_resource_quiescence.v1"
        or receipt.get("generation") != generation
        or receipt.get("revision") != revision
    ):
        raise SystemExit("daemon quiescence receipt belongs to another release")
    runtimes = receipt.get("container_runtimes")
    proofs = receipt.get("proofs")
    if not isinstance(runtimes, list) or not isinstance(proofs, dict):
        raise SystemExit("daemon quiescence receipt lacks runtime or phase evidence")
    for runtime in runtimes:
        if (
            not isinstance(runtime, dict)
            or runtime.get("kind") not in {"docker", "podman"}
            or not isinstance(runtime.get("path"), str)
            or not os.path.isabs(runtime["path"])
            or not isinstance(runtime.get("endpoint"), str)
            or not isinstance(runtime.get("selector"), list)
            or not all(isinstance(item, str) for item in runtime["selector"])
        ):
            raise SystemExit("daemon quiescence receipt has an invalid runtime identity")
    for phase in required_phases:
        proof = proofs.get(phase)
        if (
            not isinstance(proof, dict)
            or proof.get("stable_inactive_observations") != 2
            or proof.get("container_runtimes") != runtimes
            or not isinstance(proof.get("recorded_at"), str)
            or not proof["recorded_at"]
        ):
            raise SystemExit("daemon quiescence receipt lacks phase %s" % phase)
    if receipt.get("post_install") != proofs["post_install"]:
        raise SystemExit("daemon quiescence post-install proof is inconsistent")
    return {
        "schema": "mac.daemon_resource_quiescence_manifest.v1",
        "status": "proved",
        "gateway_implementation": gateway_implementation,
        "path": str(receipt_path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "generation": generation,
        "revision": revision,
        "required_phases": required_phases,
        "proved_phases": sorted(proofs),
        "container_runtimes": runtimes,
    }


def phase1_cohort_quiescence_summary(mac_home):
    required = os.environ.get("MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE") == "1"
    if not required:
        return {
            "schema": "mac.phase1_cohort_quiescence_manifest.v1",
            "status": "not_required",
        }
    generation = os.environ["MAC_DEPLOY_GENERATION"]
    path = mac_home / ("phase1-cohort-quiescence-%s.json" % generation)
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise SystemExit("deployment lacks phase-1 cohort receipt: %s" % type(exc).__name__)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or len(raw) > 4 * 1024 * 1024
    ):
        raise SystemExit("phase-1 cohort receipt is not owner-private and bounded")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SystemExit("phase-1 cohort receipt is malformed") from exc
    daemon = payload.get("daemon_resource_receipt") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "mac.phase1_cohort_quiescence.v1"
        or payload.get("agent") != os.environ["AGENT"]
        or payload.get("fleet") != os.environ["FLEET_NAME"]
        or payload.get("os_kind") != os.environ["OS_KIND"]
        or payload.get("revision") != os.environ["DEPLOY_REV"]
        or payload.get("generation") != generation
        or not isinstance(payload.get("supervisor"), dict)
        or not isinstance(daemon, dict)
        or daemon.get("schema") != "mac.daemon_resource_quiescence.v1"
        or daemon.get("proof_phase") != "pre_source"
        or not isinstance(daemon.get("sha256"), str)
        or len(daemon["sha256"]) != 64
        or not isinstance(daemon.get("function_block_sha256"), str)
        or len(daemon["function_block_sha256"]) != 64
    ):
        raise SystemExit("phase-1 cohort receipt belongs to another release")
    return {
        "schema": "mac.phase1_cohort_quiescence_manifest.v1",
        "status": "proved",
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "generation": generation,
        "revision": os.environ["DEPLOY_REV"],
        "supervisor": payload["supervisor"],
        "daemon_resource_receipt": daemon,
    }


def gateway_readiness_summary(stage):
    implementation = os.environ.get("HERMES_GATEWAY_IMPL") or "hermes"
    supervisor = os.environ.get("SUPERVISOR_KIND") or (
        "launchd" if os.environ["OS_KIND"] == "darwin" else "systemd"
    )
    if stage != "post":
        return {
            "schema": "mac.gateway_readiness_manifest.v1",
            "status": "pending",
            "implementation": implementation,
            "supervisor": supervisor,
        }
    path = Path(os.environ["LOG_DIR"]) / "gateway-readiness.json"
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise SystemExit("post manifest lacks gateway readiness: %s" % type(exc).__name__)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or len(raw) > 1024 * 1024
    ):
        raise SystemExit("gateway readiness is not an owner-private bounded file")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SystemExit("gateway readiness is malformed") from exc
    expected_identities = {
        "systemd": {
            "hermes": os.environ.get(
                "HERMES_SERVICE_NAME", os.environ["FLEET_NAME"] + "-hermes-gateway.service"
            ),
            "openclaw": os.environ.get(
                "OPENCLAW_SERVICE_NAME", os.environ["FLEET_NAME"] + "-openclaw-gateway.service"
            ),
            "nemoclaw": os.environ.get(
                "NEMOCLAW_SERVICE_NAME", os.environ["FLEET_NAME"] + "-nemoclaw-gateway.service"
            ),
        },
        "launchd": {
            "hermes": os.environ.get(
                "HERMES_LAUNCHD_LABEL", "com." + os.environ["FLEET_NAME"] + ".hermes-gateway"
            ),
            "openclaw": os.environ.get(
                "OPENCLAW_LAUNCHD_LABEL", "com." + os.environ["FLEET_NAME"] + ".openclaw-gateway"
            ),
            "nemoclaw": os.environ.get(
                "NEMOCLAW_LAUNCHD_LABEL", "com." + os.environ["FLEET_NAME"] + ".nemoclaw-gateway"
            ),
        },
        "supervisord": {
            "hermes": os.environ.get(
                "HERMES_SUPERVISORD_PROG", os.environ["FLEET_NAME"] + "-hermes-gateway"
            ),
            "openclaw": os.environ.get(
                "OPENCLAW_SUPERVISORD_PROG", os.environ["FLEET_NAME"] + "-openclaw-gateway"
            ),
            "nemoclaw": os.environ.get(
                "NEMOCLAW_SUPERVISORD_PROG", os.environ["FLEET_NAME"] + "-nemoclaw-gateway"
            ),
        },
    }[supervisor]
    state = payload.get("state") if isinstance(payload, dict) else None
    observed_at = payload.get("observed_at") if isinstance(payload, dict) else None
    try:
        observed_epoch = calendar.timegm(
            time.strptime(str(observed_at), "%Y-%m-%dT%H:%M:%SZ")
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise SystemExit("gateway readiness has an invalid observation clock") from exc
    age = time.time() - observed_epoch
    if age < -30 or age > 300:
        raise SystemExit("gateway readiness observation is stale")
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "mac.gateway_readiness.v1"
        or payload.get("agent") != os.environ["AGENT"]
        or payload.get("fleet") != os.environ["FLEET_NAME"]
        or payload.get("generation") != os.environ["MAC_DEPLOY_GENERATION"]
        or payload.get("revision") != os.environ["DEPLOY_REV"]
        or payload.get("supervisor") != supervisor
        or payload.get("implementation") != implementation
        or payload.get("stable_observations") != 2
        or payload.get("identities") != expected_identities
        or not isinstance(state, dict)
        or set(state) != {"hermes", "openclaw", "nemoclaw"}
    ):
        raise SystemExit("gateway readiness belongs to another release")
    for owner, item in state.items():
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("state"), str)
            or not isinstance(item.get("pid"), int)
            or isinstance(item.get("pid"), bool)
            or item["pid"] < 0
            or not isinstance(item.get("restarts"), int)
            or isinstance(item.get("restarts"), bool)
            or item["restarts"] < 0
        ):
            raise SystemExit("gateway readiness has malformed process state")
        selected = owner == implementation
        if selected:
            if item["state"] != "running" or item["pid"] <= 0:
                raise SystemExit("gateway readiness selected process is not running")
            if supervisor == "systemd" and item.get("enabled") != "enabled":
                raise SystemExit("gateway readiness selected unit is not enabled")
            continue
        if supervisor == "systemd":
            if (
                item["state"] not in {"absent", "inactive"}
                or item["pid"] != 0
                or item.get("enabled")
                not in {"not-found", "disabled", "masked"}
            ):
                raise SystemExit("gateway readiness non-selected unit is unsafe")
        elif supervisor == "launchd":
            if item["state"] != "absent" or item["pid"] != 0:
                raise SystemExit("gateway readiness non-selected launchd job is loaded")
        elif implementation == "openclaw" and owner == "hermes":
            if item["state"] not in {"absent", "stopped"} or item["pid"] != 0:
                raise SystemExit("gateway readiness Hermes rollback job is active")
        elif item["state"] != "absent" or item["pid"] != 0:
            raise SystemExit("gateway readiness non-selected supervisord job exists")
    return {
        "schema": "mac.gateway_readiness_manifest.v1",
        "status": "proved",
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "generation": payload["generation"],
        "revision": payload["revision"],
        "implementation": implementation,
        "supervisor": supervisor,
        "stable_observations": 2,
        "identities": payload["identities"],
        "state": payload["state"],
    }


def service_summary():
    supervisor = os.environ.get("SUPERVISOR_KIND") or (
        "launchd" if os.environ["OS_KIND"] == "darwin" else "systemd"
    )
    fleet = os.environ.get("FLEET_NAME", "mac")
    mac_svc = os.environ.get("MAC_SERVICE_NAME", fleet + ".service")
    hermes_svc = os.environ.get("HERMES_SERVICE_NAME", fleet + "-hermes-gateway.service")
    openclaw_svc = os.environ.get("OPENCLAW_SERVICE_NAME", fleet + "-openclaw-gateway.service")
    nemoclaw_svc = os.environ.get("NEMOCLAW_SERVICE_NAME", fleet + "-nemoclaw-gateway.service")
    agent_svc = os.environ.get("MAC_AGENT_SERVICE_NAME", fleet + "-agent.service")
    mac_label = os.environ.get("MAC_LAUNCHD_LABEL", "com." + fleet + ".control-plane")
    system_supervisor_label = os.environ.get(
        "DARWIN_SYSTEM_SUPERVISOR_LABEL", "com." + fleet + ".supervisor"
    )
    hermes_label = os.environ.get("HERMES_LAUNCHD_LABEL", "com." + fleet + ".hermes-gateway")
    openclaw_label = os.environ.get("OPENCLAW_LAUNCHD_LABEL", "com." + fleet + ".openclaw-gateway")
    nemoclaw_label = os.environ.get("NEMOCLAW_LAUNCHD_LABEL", "com." + fleet + ".nemoclaw-gateway")
    agent_label = os.environ.get("MAC_AGENT_LAUNCHD_LABEL", "com." + fleet + ".agent")
    qdrant_label = "com." + fleet + ".qdrant"
    mac_prog = os.environ.get("MAC_SUPERVISORD_PROG", fleet + "-control-plane")
    hermes_prog = os.environ.get("HERMES_SUPERVISORD_PROG", fleet + "-hermes-gateway")
    openclaw_prog = os.environ.get("OPENCLAW_SUPERVISORD_PROG", fleet + "-openclaw-gateway")
    nemoclaw_prog = os.environ.get("NEMOCLAW_SUPERVISORD_PROG", fleet + "-nemoclaw-gateway")
    agent_prog = os.environ.get("AGENT_SUPERVISORD_PROG", fleet + "-agent")
    qdrant_prog = fleet + "-qdrant"
    if supervisor == "systemd":
        result = run(
            [
                "systemctl",
                "show",
                mac_svc,
                hermes_svc,
                openclaw_svc,
                nemoclaw_svc,
                agent_svc,
                fleet + "-qdrant.service",
                "-p",
                "Id",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "MainPID",
                "-p",
                "ExecMainStatus",
                "-p",
                "NRestarts",
                "-p",
                "TimeoutStopUSec",
            ]
        )
        return {"manager": "systemd", "raw": result}
    if supervisor == "launchd":
        return {
            "manager": "launchd",
            "control_plane": run(["launchctl", "list", mac_label]),
            "control_plane_system": probe(
                ["launchctl", "print", "system/" + mac_label]
            ),
            "control_plane_system_plist": file_ref(
                "/Library/LaunchDaemons/" + mac_label + ".plist"
            ),
            "control_plane_system_supervisor": probe(
                ["launchctl", "print", "system/" + system_supervisor_label]
            ),
            "control_plane_system_supervisor_plist": file_ref(
                "/Library/LaunchDaemons/" + system_supervisor_label + ".plist"
            ),
            "hermes_gateway": run(["launchctl", "list", hermes_label]),
            "openclaw_gateway": run(["launchctl", "list", openclaw_label]),
            "nemoclaw_gateway": run(["launchctl", "list", nemoclaw_label]),
            "mac_agent": run(["launchctl", "list", agent_label]),
            "qdrant": run(["launchctl", "list", qdrant_label]),
        }
    if supervisor == "supervisord":
        return {
            "manager": "supervisord",
            "status": run(
                [
                    "supervisorctl",
                    "status",
                    mac_prog,
                    hermes_prog,
                    openclaw_prog,
                    nemoclaw_prog,
                    agent_prog,
                    qdrant_prog,
                ]
            ),
        }
    return {
        "manager": supervisor,
        "error": "unsupported supervisor in manifest",
    }


stage, output_path = sys.argv[1], Path(sys.argv[2])
mac_home = Path(os.environ["MAC_HOME"])
hermes_dir = Path(os.environ["HERMES_DIR"])
acc_candidates = [
    Path.home() / ".acc" / "data" / "fleet.db",
    Path.home() / ".acc" / "data" / "acc.db",
]
hermes_config = hermes_dir / "gateway" / "config.py"
hermes_config_text = ""
try:
    hermes_config_text = hermes_config.read_text(encoding="utf-8", errors="ignore")
except OSError:
    pass
hermes_run = hermes_dir / "gateway" / "run.py"
hermes_run_text = ""
try:
    hermes_run_text = hermes_run.read_text(encoding="utf-8", errors="ignore")
except OSError:
    pass
hermes_prompt_builder = hermes_dir / "agent" / "prompt_builder.py"
hermes_prompt_builder_text = ""
try:
    hermes_prompt_builder_text = hermes_prompt_builder.read_text(encoding="utf-8", errors="ignore")
except OSError:
    pass


def prerequisite_summary(stage):
    if os.environ.get("NODE_ACTION") not in {"arm-phase2", "apply-phase2"}:
        return {
            "schema": "mac.fleet_prerequisite_bundle_manifest.v1",
            "status": "legacy_not_required",
        }
    path = Path(os.environ["PREREQUISITE_SUMMARY"])
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > 1024 * 1024
        ):
            raise SystemExit("typed prerequisite summary is not owner-private")
        raw = os.read(descriptor, metadata.st_size + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SystemExit("typed prerequisite summary is malformed") from exc
    participants = value.get("participants") if isinstance(value, dict) else None
    if (
        value.get("schema") != "mac.fleet_prerequisite_bundle_summary.v1"
        or value.get("agent_id") != os.environ["AGENT"]
        or value.get("node_identity_sha256")
        != os.environ["NODE_IDENTITY_SHA256"]
        or value.get("bundle_sha256")
        != os.environ["PREREQUISITE_BUNDLE_SHA256"]
        or value.get("expectations_sha256")
        != os.environ["PREREQUISITE_EXPECTATIONS_SHA256"]
        or not isinstance(participants, list)
        or len(participants) != 8
    ):
        raise SystemExit("typed prerequisite summary binding differs")
    return {
        "schema": "mac.fleet_prerequisite_bundle_manifest.v1",
        "status": "proved",
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bundle_sha256": value["bundle_sha256"],
        "expectations_sha256": value["expectations_sha256"],
        "node_identity_sha256": value["node_identity_sha256"],
        "participants": participants,
    }

manifest = {
    "schema_version": 1,
    "stage": stage,
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "agent": os.environ["AGENT"],
    "os_kind": os.environ["OS_KIND"],
    "deploy": {
        "timestamp": os.environ["DEPLOY_TS"],
        "generation": os.environ["DEPLOY_GENERATION"],
        "mac_git_rev": os.environ["DEPLOY_REV"],
        "mac_git_url": os.environ.get("DEPLOY_GIT_URL") or None,
        "mac_git_branch": os.environ.get("DEPLOY_GIT_BRANCH") or None,
        "log": os.environ["DEPLOY_LOG"],
        "hermes_slack_home_channel_name": os.environ.get("HERMES_SLACK_HOME_CHANNEL_NAME") or None,
        "hermes_gateway_model": os.environ.get("HERMES_GATEWAY_MODEL") or None,
        "hermes_gateway_provider": os.environ.get("HERMES_GATEWAY_PROVIDER") or None,
        "hermes_gateway_base_url_configured": bool(os.environ.get("HERMES_GATEWAY_BASE_URL")),
        "hub_url": os.environ.get("HUB_URL") or None,
        "control_bind_host": os.environ.get("CONTROL_BIND_HOST") or None,
        "worker_mode": os.environ.get("WORKER_MODE") or None,
        "worker_capabilities": [
            item.strip()
            for item in (os.environ.get("WORKER_CAPABILITIES") or "").split(",")
            if item.strip()
        ],
        "worker_allowed_projects": [
            item.strip()
            for item in (os.environ.get("WORKER_ALLOWED_PROJECTS") or "").split(",")
            if item.strip()
        ],
        "worker_required_metadata_configured": bool(os.environ.get("WORKER_REQUIRED_METADATA")),
        "worker_require_canary": os.environ.get("WORKER_REQUIRE_CANARY") or None,
        "supervisor_requested": os.environ.get("SUPERVISOR_REQUESTED") or None,
        "supervisor_selected": os.environ.get("SUPERVISOR_KIND") or None,
        "shared_services_manager_agent": os.environ.get("SHARED_SERVICES_MANAGER_AGENT") or None,
        "qdrant": {
            "install": os.environ.get("QDRANT_INSTALL") or None,
            "required": os.environ.get("QDRANT_REQUIRE") or None,
            "url_configured": bool(os.environ.get("QDRANT_URL_CONFIGURED")),
            "port": os.environ.get("QDRANT_PORT_CONFIGURED") or None,
            "image": os.environ.get("QDRANT_IMAGE_CONFIGURED") or None,
            "memory_limit": os.environ.get("QDRANT_MEMORY_LIMIT_CONFIGURED") or None,
        },
        "firecrawl": {
            "install": os.environ.get("FIRECRAWL_INSTALL") or None,
            "required": os.environ.get("FIRECRAWL_REQUIRE") or None,
            "url_configured": bool(os.environ.get("FIRECRAWL_URL_CONFIGURED")),
            "port": os.environ.get("FIRECRAWL_PORT_CONFIGURED") or None,
        },
        "webdav": {
            "enabled": os.environ.get("WEBDAV_ENABLED") or None,
            "install": os.environ.get("WEBDAV_INSTALL") or None,
            "url": os.environ.get("WEBDAV_URL_CONFIGURED") or None,
            "bind_addr": os.environ.get("WEBDAV_BIND_ADDR_CONFIGURED") or None,
            "port": os.environ.get("WEBDAV_PORT_CONFIGURED") or None,
            "root_configured": bool(os.environ.get("WEBDAV_ROOT_CONFIGURED")),
            "public_path": os.environ.get("WEBDAV_PUBLIC_PATH_CONFIGURED") or None,
        },
        "network": {
            "provider": os.environ.get("NETWORK_PROVIDER") or None,
            "install": os.environ.get("NETWORK_INSTALL") or None,
            "hostname_prefix": os.environ.get("NETWORK_HOSTNAME_PREFIX") or None,
            "mesh_ip": os.environ.get("MAC_TAILSCALE_IP") or None,
            "mesh_hostname": os.environ.get("MAC_TAILSCALE_HOSTNAME") or None,
            "tailscale": {
                "auth_key_env": os.environ.get("TAILSCALE_AUTH_KEY_ENV") or None,
                "auth_key_configured": bool(os.environ.get("TAILSCALE_AUTH_KEY")),
            },
            "headscale": {
                "manage": os.environ.get("HEADSCALE_MANAGE") or None,
                "login_server": os.environ.get("HEADSCALE_LOGIN_SERVER") or None,
                "health_url": os.environ.get("HEADSCALE_HEALTH_URL") or None,
                "fleet_url": os.environ.get("HEADSCALE_FLEET_URL") or None,
                "preauth_key_env": os.environ.get("HEADSCALE_PREAUTH_KEY_ENV") or None,
                "preauth_key_source": os.environ.get("HEADSCALE_PREAUTH_KEY_SOURCE") or None,
                "preauth_key_configured": bool(os.environ.get("HEADSCALE_PREAUTHKEY")),
                "port": os.environ.get("HEADSCALE_PORT") or None,
                "dns": os.environ.get("HEADSCALE_DNS") or None,
            },
        },
        "drain": {
            "mode": os.environ.get("DRAIN_MODE") or None,
            "timeout_seconds": int(os.environ.get("DRAIN_TIMEOUT_SECONDS") or 0),
            "poll_seconds": int(os.environ.get("DRAIN_POLL_SECONDS") or 0),
        },
    },
    "paths": {
        "mac_home": str(mac_home),
        "source": str(Path(os.environ["SRC_DIR"])),
        "mac_venv": str(Path(os.environ["VENV"])),
        "hermes_agent": str(hermes_dir),
        "env_file": str(Path(os.environ["ENV_FILE"])),
        "hermes_runtime_context": os.environ.get("MAC_HERMES_RUNTIME_CONTEXT_FILE") or str(Path.home() / ".hermes" / "mac-runtime-context.json"),
        "hermes_runtime_markdown": os.environ.get("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN") or str(Path.home() / ".hermes" / "mac-runtime-context.md"),
    },
    "python": {
        "selected": os.environ["PY"],
        "selected_version": py_version(os.environ["PY"]),
        "mac_venv_version": py_version(Path(os.environ["VENV"]) / "bin" / "python"),
        "hermes_venv_version": py_version(hermes_dir / ".venv" / "bin" / "python"),
    },
    "artifacts": {
        "mac_source": file_ref(os.environ["SRC_DIR"]),
        "mac_database": file_ref(mac_home / "mac.db"),
        "hermes_agent": file_ref(hermes_dir),
        "hermes_state": file_ref(Path.home() / ".hermes"),
        "hermes_runtime_context": file_ref(os.environ.get("MAC_HERMES_RUNTIME_CONTEXT_FILE") or (Path.home() / ".hermes" / "mac-runtime-context.json")),
        "hermes_runtime_markdown": file_ref(os.environ.get("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN") or (Path.home() / ".hermes" / "mac-runtime-context.md")),
        "acc_state": file_ref(Path.home() / ".acc"),
        "disk_before_cleanup": file_ref(Path(os.environ["LOG_DIR"]) / ("disk-before-cleanup-%s.json" % os.environ["DEPLOY_TS"])),
        "disk_after_cleanup": file_ref(Path(os.environ["LOG_DIR"]) / ("disk-after-cleanup-%s.json" % os.environ["DEPLOY_TS"])),
        "disk_cleanup_report": file_ref(Path(os.environ["LOG_DIR"]) / ("disk-cleanup-%s.json" % os.environ["DEPLOY_TS"])),
        "openshell_sandbox_inventory": file_ref(Path(os.environ["LOG_DIR"]) / ("openshell-sandbox-inventory-%s.json" % os.environ["DEPLOY_TS"])),
        "openshell_runtime_conformance": file_ref(Path(os.environ["LOG_DIR"]) / ("openshell-runtime-conformance-%s.json" % os.environ["DEPLOY_TS"])),
        "openshell_upgrade_recovery": file_ref(Path(os.environ["MAC_HOME"]) / "openshell" / "upgrade-recovery.log"),
    },
    "acc": {
        "candidate_databases": [file_ref(path) for path in acc_candidates],
        "selected_database": next((str(path) for path in acc_candidates if path.exists()), None),
        "migration_status_report": file_ref(Path(os.environ["LOG_DIR"]) / "acc-migration-status.json"),
        "migration_import_report": file_ref(Path(os.environ["LOG_DIR"]) / "acc-migration-import.json"),
    },
    "hermes": {
        "origin": run(["git", "-C", str(hermes_dir), "remote", "get-url", "origin"]),
        "rev": run(["git", "-C", str(hermes_dir), "rev-parse", "HEAD"]),
        "slack_account_file_shim_present": (
            "_slack_accounts_file_configured" in hermes_config_text
            and "slack_accounts.json" in hermes_config_text
        ),
        "gateway_runtime_shim_present": (
            "MAC_HERMES_GATEWAY_MODEL" in hermes_run_text
            and "MAC_HERMES_GATEWAY_PROVIDER" in hermes_run_text
            and "resolve_runtime_provider" in hermes_run_text
        ),
        "task_project_runtime_context": file_ref(os.environ.get("MAC_HERMES_RUNTIME_CONTEXT_FILE") or (Path.home() / ".hermes" / "mac-runtime-context.json")),
        "task_project_runtime_prompt_bridge_present": (
            "_load_mac_runtime_context" in hermes_prompt_builder_text
            and "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN" in hermes_prompt_builder_text
        ),
        "messaging_deps_report": file_ref(Path(os.environ["LOG_DIR"]) / "hermes-messaging-deps.json"),
        "web_deps_report": file_ref(Path(os.environ["LOG_DIR"]) / "hermes-web-deps.json"),
        "log_summary": file_ref(Path(os.environ["LOG_DIR"]) / "hermes-log-summary.json"),
    },
    "services": service_summary(),
    "phase1_cohort_quiescence": phase1_cohort_quiescence_summary(mac_home),
    "daemon_resource_quiescence": daemon_quiescence_summary(stage, mac_home),
    "gateway_readiness": gateway_readiness_summary(stage),
    "prerequisites": prerequisite_summary(stage),
    "backups": {
        "source": os.environ.get("SRC_BACKUP") or None,
        "mac_venv": os.environ.get("VENV_BACKUP") or None,
        "hermes_agent": os.environ.get("HERMES_BACKUP") or None,
        "mac_unit": os.environ.get("MAC_UNIT_BACKUP") or None,
        "hermes_unit": os.environ.get("HERMES_UNIT_BACKUP") or None,
        "mac_agent_unit": os.environ.get("MAC_AGENT_UNIT_BACKUP") or None,
        "mac_plist": os.environ.get("MAC_PLIST_BACKUP") or None,
        "mac_system_plist": os.environ.get("DARWIN_SYSTEM_PLIST_BACKUP") or None,
        "mac_system_launchd_was_active": (
            os.environ.get("DARWIN_SYSTEM_LAUNCHD_ACTIVE") == "1"
        ),
        "mac_gui_launchd_was_active": (
            os.environ.get("DARWIN_GUI_LAUNCHD_ACTIVE") == "1"
        ),
        "mac_system_supervisor_plist": (
            os.environ.get("DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP") or None
        ),
        "mac_system_supervisor_launchd_was_active": (
            os.environ.get("DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE") == "1"
        ),
        "hermes_plist": os.environ.get("HERMES_PLIST_BACKUP") or None,
        "mac_agent_plist": os.environ.get("MAC_AGENT_PLIST_BACKUP") or None,
        "hermes_launchd_was_active": (
            os.environ.get("DARWIN_HERMES_LAUNCHD_ACTIVE") == "1"
        ),
        "openclaw_launchd_was_active": (
            os.environ.get("DARWIN_OPENCLAW_LAUNCHD_ACTIVE") == "1"
        ),
        "nemoclaw_launchd_was_active": (
            os.environ.get("DARWIN_NEMOCLAW_LAUNCHD_ACTIVE") == "1"
        ),
        "mac_agent_launchd_was_active": (
            os.environ.get("DARWIN_AGENT_LAUNCHD_ACTIVE") == "1"
        ),
    },
    "rollback": rollback_contract_summary(stage),
}
import os, tempfile as _tempfile
fd, tmp_name = _tempfile.mkstemp(prefix="." + output_path.name + ".", dir=str(output_path.parent))
_tmp = __import__("pathlib").Path(tmp_name)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as _fh:
        _fh.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        _fh.flush()
        os.fsync(_fh.fileno())
    os.replace(_tmp, output_path)
    try:
        output_path.chmod(0o600)
    except OSError:
        pass
finally:
    if _tmp.exists():
        _tmp.unlink()
PY
}

write_phase2_finalize_receipt() {
  MAC_FINALIZE_RECEIPT="$FINALIZE_RECEIPT" \
  MAC_FINALIZE_POST_MANIFEST="$MANIFEST_POST" \
  MAC_FINALIZE_ROLLBACK_INTENT="$ROLLBACK_INTENT" \
  MAC_FINALIZE_AGENT="$AGENT" MAC_FINALIZE_FLEET="$FLEET_NAME" \
  MAC_FINALIZE_GENERATION="$DEPLOY_GENERATION" \
  MAC_FINALIZE_REVISION="$DEPLOY_REV" \
    "$PY" - <<'PY'
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path


def private_bytes(path: Path, mode: int, limit: int, description: str) -> bytes:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size <= 0
            or before.st_size > limit
        ):
            raise SystemExit(description + " is not owner-private and bounded")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise SystemExit(description + " was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise SystemExit(description + " grew while reading")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise SystemExit(description + " changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


output = Path(os.environ["MAC_FINALIZE_RECEIPT"])
post_path = Path(os.environ["MAC_FINALIZE_POST_MANIFEST"])
intent_path = Path(os.environ["MAC_FINALIZE_ROLLBACK_INTENT"])
generation = os.environ["MAC_FINALIZE_GENERATION"]
revision = os.environ["MAC_FINALIZE_REVISION"]
post_raw = private_bytes(post_path, 0o600, 8 * 1024 * 1024, "post manifest")
intent_raw = private_bytes(
    intent_path, 0o600, 4 * 1024 * 1024, "pre-mutation rollback intent"
)
try:
    post = json.loads(post_raw)
    intent = json.loads(intent_raw)
except (TypeError, ValueError) as exc:
    raise SystemExit("finalize input is malformed") from exc
post_rollback = post.get("rollback") if isinstance(post, dict) else None
intent_rollback = intent.get("rollback") if isinstance(intent, dict) else None
post_intent = post_rollback.get("intent") if isinstance(post_rollback, dict) else None
if (
    not isinstance(post, dict)
    or post.get("stage") != "post"
    or (post.get("deploy") or {}).get("generation") != generation
    or (post.get("deploy") or {}).get("mac_git_rev") != revision
    or not isinstance(intent, dict)
    or intent.get("schema") != "mac.fleet_node_rollback_intent.v1"
    or intent.get("status") != "armed"
    or intent.get("generation") != generation
    or intent.get("revision") != revision
    or not isinstance(intent_rollback, dict)
    or not isinstance(post_rollback, dict)
    or post_rollback.get("status") != "armed"
    or post_rollback.get("authority") != "pre_mutation_intent"
    or post_rollback.get("path") != intent_rollback.get("path")
    or post_rollback.get("sha256") != intent_rollback.get("sha256")
    or not isinstance(post_intent, dict)
    or post_intent.get("path") != str(intent_path)
    or post_intent.get("sha256") != hashlib.sha256(intent_raw).hexdigest()
):
    raise SystemExit("post manifest does not finalize the armed generation")
post_sha256 = hashlib.sha256(post_raw).hexdigest()
intent_sha256 = hashlib.sha256(intent_raw).hexdigest()
expected = {
    "schema": "mac.fleet_node_finalize.v1",
    "status": "finalized",
    "agent": os.environ["MAC_FINALIZE_AGENT"],
    "fleet": os.environ["MAC_FINALIZE_FLEET"],
    "generation": generation,
    "revision": revision,
    "post_manifest": {"path": str(post_path), "sha256": post_sha256},
    "rollback_intent": {"path": str(intent_path), "sha256": intent_sha256},
}
if output.exists() or output.is_symlink():
    raw = private_bytes(output, 0o600, 1024 * 1024, "finalize receipt")
    try:
        receipt = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SystemExit("finalize receipt is malformed") from exc
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise SystemExit("finalize receipt belongs to another generation")
    os.write(1, raw)
    raise SystemExit(0)
payload = {
    **expected,
    "finalized_at": dt.datetime.now(dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    ),
}
descriptor, temporary_raw = tempfile.mkstemp(
    prefix="." + output.name + ".", dir=str(output.parent)
)
temporary = Path(temporary_raw)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, output)
    except FileExistsError as exc:
        raise SystemExit("finalize receipt appeared concurrently") from exc
    temporary.unlink()
    directory = os.open(str(output.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    temporary.unlink(missing_ok=True)
os.write(1, private_bytes(output, 0o600, 1024 * 1024, "finalize receipt"))
PY
}

snapshot_rollback_file() {
  local source="$1" backup="$2" mode="${3:-user}" existed="" snapshot_rc=0
  existed="$(mac_launchd_snapshot_file "$source" "$backup" "$mode")" \
    || snapshot_rc=$?
  [ "$snapshot_rc" -eq 0 ] \
    || die "could not durably snapshot rollback artifact: $source"
  [ "$existed" = 1 ] \
    || die "rollback artifact disappeared before snapshot: $source"
  if [ "$mode" = system ]; then
    run_privileged_bounded \
      "${MAC_LAUNCHD_ARTIFACT_TIMEOUT_SECONDS:-10}" \
      chown "$(id -u):$(id -g)" "$backup"
  fi
  chmod 0600 "$backup"
}

track_auxiliary_rollback_artifact() {
  local path="$1" mode="${2:-user}" index=0 backup existed="" snapshot_rc=0
  case "$mode" in
    user|system) ;;
    *) die "invalid auxiliary rollback artifact mode: $mode" ;;
  esac
  while [ "$index" -lt "$ROLLBACK_AUX_ARTIFACT_COUNT" ]; do
    [ "${ROLLBACK_AUX_ARTIFACT_PATHS[$index]}" != "$path" ] || return 0
    index=$(( index + 1 ))
  done
  backup="$MAC_HOME/backups/aux-artifact.${AGENT}.${DEPLOY_TS}.${ROLLBACK_AUX_ARTIFACT_COUNT}"
  existed="$(mac_launchd_snapshot_file "$path" "$backup" "$mode")" \
    || snapshot_rc=$?
  [ "$snapshot_rc" -eq 0 ] \
    || die "could not snapshot auxiliary rollback artifact: $path"
  case "$existed" in
    0|1) ;;
    *) die "invalid auxiliary rollback snapshot result for $path" ;;
  esac
  ROLLBACK_AUX_ARTIFACT_PATHS[$ROLLBACK_AUX_ARTIFACT_COUNT]="$path"
  ROLLBACK_AUX_ARTIFACT_BACKUPS[$ROLLBACK_AUX_ARTIFACT_COUNT]="$backup"
  ROLLBACK_AUX_ARTIFACT_EXISTED[$ROLLBACK_AUX_ARTIFACT_COUNT]="$existed"
  ROLLBACK_AUX_ARTIFACT_MODES[$ROLLBACK_AUX_ARTIFACT_COUNT]="$mode"
  ROLLBACK_AUX_ARTIFACT_COUNT=$(( ROLLBACK_AUX_ARTIFACT_COUNT + 1 ))
}

snapshot_rollback_directory() {
  local source="$1" destination="$2" description="$3"
  [ -d "$source" ] && [ ! -L "$source" ] \
    || die "$description is not a regular directory"
  mac_launchd_run_python_bounded \
    user "${MAC_ROLLBACK_DIRECTORY_SNAPSHOT_TIMEOUT_SECONDS:-300}" '
import os
import shutil
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
metadata = source.lstat()
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
    raise SystemExit("rollback bin source is not a regular directory")
if destination.exists() or destination.is_symlink():
    raise SystemExit("rollback bin snapshot already exists")
temporary = destination.with_name(".%s.stage.%d" % (destination.name, os.getpid()))
if temporary.exists() or temporary.is_symlink():
    raise SystemExit("rollback bin staging path already exists")
try:
    shutil.copytree(source, temporary, symlinks=True)
    for root, directories, files in os.walk(temporary, topdown=False):
        root_path = Path(root)
        for name in files:
            path = root_path / name
            item = path.lstat()
            if not stat.S_ISREG(item.st_mode):
                continue
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(str(path), flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        descriptor = os.open(str(root_path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    os.replace(temporary, destination)
    descriptor = os.open(str(destination.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
finally:
    if temporary.exists():
        shutil.rmtree(temporary)
' "$source" "$destination" \
    || die "could not durably snapshot $description for rollback"
}

snapshot_bin_directory_for_rollback() {
  BIN_BACKUP="$MAC_HOME/backups/bin.${AGENT}.${DEPLOY_TS}"
  snapshot_rollback_directory \
    "$MAC_HOME/bin" "$BIN_BACKUP" "MAC_HOME/bin"
}

capture_prior_deployment_identity() {
  local identity
  identity="$(MAC_PRIOR_ENV_FILE="$ENV_FILE" \
    MAC_PRIOR_REVISION_FILE="$MAC_HOME/deployed-source-revision" \
    "$PY" - <<'PY'
from __future__ import annotations

import os
import re
import shlex
import stat
from pathlib import Path
from typing import Optional


GENERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")


def private_bytes(path: Path, limit: int) -> Optional[bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SystemExit("prior deployment identity could not be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > limit
        ):
            raise SystemExit("prior deployment identity is not owner-private and bounded")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise SystemExit("prior deployment identity was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise SystemExit("prior deployment identity grew while reading")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise SystemExit("prior deployment identity changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


generation = ""
env_raw = private_bytes(Path(os.environ["MAC_PRIOR_ENV_FILE"]), 1024 * 1024)
if env_raw is not None:
    try:
        env_text = env_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("prior deployment environment is not UTF-8") from exc
    matches: list[str] = []
    for line in env_text.splitlines():
        try:
            fields = shlex.split(line, posix=True)
        except ValueError as exc:
            raise SystemExit("prior deployment environment is malformed") from exc
        if len(fields) == 1 and fields[0].startswith("MAC_WORKER_DEPLOY_GENERATION="):
            matches.append(fields[0].split("=", 1)[1])
    if len(matches) > 1:
        raise SystemExit("prior deployment environment has duplicate generation identity")
    if matches:
        generation = matches[0]
        if not GENERATION.fullmatch(generation):
            raise SystemExit("prior deployment generation identity is invalid")

revision = ""
revision_raw = private_bytes(
    Path(os.environ["MAC_PRIOR_REVISION_FILE"]), 256
)
if revision_raw is not None:
    try:
        revision = revision_raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise SystemExit("prior deployment revision is not ASCII") from exc
    if revision and not REVISION.fullmatch(revision):
        raise SystemExit("prior deployment revision identity is invalid")

print(generation + "|" + revision)
PY
)" || die "could not capture the prior deployment identity"
  IFS='|' read -r ROLLBACK_PRIOR_GENERATION ROLLBACK_PRIOR_REVISION <<< "$identity"
}

capture_mutable_runtime_state_for_rollback() {
  local openclaw_home="$MAC_HOME/openclaw"
  [ -d "$SRC_DIR" ] && [ ! -L "$SRC_DIR" ] \
    && [ -d "$VENV" ] && [ ! -L "$VENV" ] || return 0
  if [ "${MAC_DEPLOY_TEST_INJECT_OPENCLAW_SNAPSHOT_FAILURE:-0}" = 1 ]; then
    die "injected OpenClaw rollback snapshot failure"
  fi
  if [ -e "$openclaw_home" ] || [ -L "$openclaw_home" ]; then
    OPENCLAW_HOME_EXISTED=1
    OPENCLAW_HOME_BACKUP="$MAC_HOME/backups/openclaw.${AGENT}.${DEPLOY_TS}"
    snapshot_rollback_directory \
      "$openclaw_home" "$OPENCLAW_HOME_BACKUP" "MAC_HOME/openclaw"
  else
    OPENCLAW_HOME_EXISTED=0
    OPENCLAW_HOME_BACKUP=""
    if [ "$ROLLBACK_ACTIVE_GATEWAY" = openclaw ]; then
      die "prior OpenClaw topology has no MAC_HOME/openclaw runtime tree"
    fi
  fi
}

capture_auxiliary_rollback_artifacts() {
  # Capture before bootstrap tools, source metadata, environment, wrappers, or
  # supervisor definitions can change.  The bin tree is one generation unit,
  # including symlink topology; individual wrapper snapshots would miss newly
  # introduced helpers and leave a mixed executable surface after rollback.
  [ -d "$SRC_DIR" ] && [ -d "$VENV" ] || return 0
  snapshot_bin_directory_for_rollback
  track_auxiliary_rollback_artifact "$ENV_FILE" user
  track_auxiliary_rollback_artifact "$MAC_HOME/fleets.yaml" user
  track_auxiliary_rollback_artifact \
    "$MAC_HOME/deployed-source-revision" user
  track_auxiliary_rollback_artifact "$MAC_HOME/deploy-start-barrier" user
  case "$SUPERVISOR_KIND" in
    systemd)
      local system_unit
      for system_unit in \
        "$MAC_SERVICE_NAME" \
        "$HERMES_SERVICE_NAME" \
        "$OPENCLAW_SERVICE_NAME" \
        "$NEMOCLAW_SERVICE_NAME" \
        "$MAC_AGENT_SERVICE_NAME" \
        "$MAC_GEN_SERVICE_NAME" \
        "$MAC_GEN_AUDIO_SERVICE_NAME" \
        "$MAC_GEN_VIDEO_SERVICE_NAME" \
        "${FLEET_NAME}-qdrant.service" \
        "${FLEET_NAME}-firecrawl-gateway.service" \
        "${FLEET_NAME}-webdav.service" \
        "mac-openshell-firewall.service"; do
        track_auxiliary_rollback_artifact \
          "/etc/systemd/system/$system_unit" system
      done
      track_auxiliary_rollback_artifact \
        "$HOME/.config/systemd/user/openshell-gateway.service" user
      track_auxiliary_rollback_artifact \
        "/usr/local/sbin/mac-openshell-firewall.sh" system
      ;;
    launchd)
      local user_plist
      for user_plist in \
        "$MAC_LAUNCHD_LABEL" \
        "$HERMES_LAUNCHD_LABEL" \
        "$OPENCLAW_LAUNCHD_LABEL" \
        "$NEMOCLAW_LAUNCHD_LABEL" \
        "$MAC_AGENT_LAUNCHD_LABEL" \
        "com.${FLEET_NAME}.qdrant" \
        "com.${FLEET_NAME}.firecrawl-gateway" \
        "com.${FLEET_NAME}.webdav"; do
        track_auxiliary_rollback_artifact \
          "$HOME/Library/LaunchAgents/${user_plist}.plist" user
      done
      track_auxiliary_rollback_artifact \
        "/Library/LaunchDaemons/${MAC_LAUNCHD_LABEL}.plist" system
      track_auxiliary_rollback_artifact \
        "/Library/LaunchDaemons/${DARWIN_SYSTEM_SUPERVISOR_LABEL}.plist" system
      ;;
    supervisord)
      track_auxiliary_rollback_artifact \
        "$(supervisord_conf_dir)/$MAC_SUPERVISORD_CONF_NAME" system
      track_auxiliary_rollback_artifact \
        "/etc/supervisor/conf.d/mac-openshell-firewall.conf" system
      track_auxiliary_rollback_artifact \
        "/usr/local/sbin/mac-openshell-firewall.sh" system
      ;;
    *) die "cannot capture auxiliary artifacts for unsupported supervisor: $SUPERVISOR_KIND" ;;
  esac
  write_rollback_script
}

write_rollback_script() {
  local rollback_control_plane_mode=inactive
  local rollback_supervisord_conf
  local rollback_aux_declarations="" rollback_aux_index=0
  local rollback_aux_path_quoted rollback_aux_backup_quoted
  local rollback_aux_existed_quoted rollback_aux_mode_quoted
  local rollback_stage="$ROLLBACK_SCRIPT.stage.$$"
  if [ -e "$ROLLBACK_INTENT" ] || [ -L "$ROLLBACK_INTENT" ]; then
    ROLLBACK_INTENT_SHA256="$(verify_phase2_rollback_intent)" \
      || die "existing phase-2 rollback intent is invalid"
    return 0
  fi
  if control_plane_enabled; then
    rollback_control_plane_mode=active
  fi
  rollback_supervisord_conf="$(supervisord_conf_dir)/$MAC_SUPERVISORD_CONF_NAME"
  while [ "$rollback_aux_index" -lt "$ROLLBACK_AUX_ARTIFACT_COUNT" ]; do
    printf -v rollback_aux_path_quoted '%q' \
      "${ROLLBACK_AUX_ARTIFACT_PATHS[$rollback_aux_index]}"
    printf -v rollback_aux_backup_quoted '%q' \
      "${ROLLBACK_AUX_ARTIFACT_BACKUPS[$rollback_aux_index]}"
    printf -v rollback_aux_existed_quoted '%q' \
      "${ROLLBACK_AUX_ARTIFACT_EXISTED[$rollback_aux_index]}"
    printf -v rollback_aux_mode_quoted '%q' \
      "${ROLLBACK_AUX_ARTIFACT_MODES[$rollback_aux_index]}"
    rollback_aux_declarations+="ROLLBACK_AUX_ARTIFACT_PATHS[$rollback_aux_index]=$rollback_aux_path_quoted"$'\n'
    rollback_aux_declarations+="ROLLBACK_AUX_ARTIFACT_BACKUPS[$rollback_aux_index]=$rollback_aux_backup_quoted"$'\n'
    rollback_aux_declarations+="ROLLBACK_AUX_ARTIFACT_EXISTED[$rollback_aux_index]=$rollback_aux_existed_quoted"$'\n'
    rollback_aux_declarations+="ROLLBACK_AUX_ARTIFACT_MODES[$rollback_aux_index]=$rollback_aux_mode_quoted"$'\n'
    rollback_aux_index=$(( rollback_aux_index + 1 ))
  done
  [ ! -e "$rollback_stage" ] && [ ! -L "$rollback_stage" ] \
    || die "rollback staging path already exists"
  cat > "$rollback_stage" <<EOF
#!/usr/bin/env bash
set -eEuo pipefail

MAC_HOME='$MAC_HOME'
MAC_PORT='$MAC_PORT'
SRC_DIR='$SRC_DIR'
VENV='$VENV'
HERMES_DIR='$HERMES_DIR'
OS_KIND='$OS_KIND'
SUPERVISOR_KIND='${SUPERVISOR_KIND:-}'
SRC_BACKUP='$SRC_BACKUP'
VENV_BACKUP='$VENV_BACKUP'
HERMES_BACKUP='$HERMES_BACKUP'
BIN_BACKUP='$BIN_BACKUP'
OPENCLAW_HOME_BACKUP='$OPENCLAW_HOME_BACKUP'
OPENCLAW_HOME_EXISTED='$OPENCLAW_HOME_EXISTED'
MAC_UNIT_BACKUP='$MAC_UNIT_BACKUP'
HERMES_UNIT_BACKUP='$HERMES_UNIT_BACKUP'
MAC_AGENT_UNIT_BACKUP='$MAC_AGENT_UNIT_BACKUP'
MAC_UNIT_MUTATED='$MAC_UNIT_MUTATED'
HERMES_UNIT_MUTATED='$HERMES_UNIT_MUTATED'
MAC_AGENT_UNIT_MUTATED='$MAC_AGENT_UNIT_MUTATED'
MAC_PLIST_BACKUP='$MAC_PLIST_BACKUP'
MAC_PLIST_MUTATED='$MAC_PLIST_MUTATED'
DARWIN_SYSTEM_PLIST_BACKUP='$DARWIN_SYSTEM_PLIST_BACKUP'
DARWIN_SYSTEM_PLIST_MUTATED='$DARWIN_SYSTEM_PLIST_MUTATED'
DARWIN_SYSTEM_LAUNCHD_ACTIVE='$DARWIN_SYSTEM_LAUNCHD_ACTIVE'
DARWIN_GUI_LAUNCHD_ACTIVE='$DARWIN_GUI_LAUNCHD_ACTIVE'
DARWIN_SYSTEM_SUPERVISOR_LABEL='$DARWIN_SYSTEM_SUPERVISOR_LABEL'
DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP='$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP'
DARWIN_SYSTEM_SUPERVISOR_PLIST_MUTATED='$DARWIN_SYSTEM_SUPERVISOR_PLIST_MUTATED'
DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE='$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE'
HERMES_PLIST_BACKUP='$HERMES_PLIST_BACKUP'
MAC_AGENT_PLIST_BACKUP='$MAC_AGENT_PLIST_BACKUP'
HERMES_PLIST_MUTATED='$HERMES_PLIST_MUTATED'
MAC_AGENT_PLIST_MUTATED='$MAC_AGENT_PLIST_MUTATED'
ROLLBACK_SUPERVISOR_HELPER='$ROLLBACK_SUPERVISOR_HELPER'
ROLLBACK_LAUNCHD_LIFECYCLE='$ROLLBACK_LAUNCHD_LIFECYCLE'
ROLLBACK_SUPERVISOR_HELPER_SHA256='$ROLLBACK_SUPERVISOR_HELPER_SHA256'
ROLLBACK_LAUNCHD_LIFECYCLE_SHA256='$ROLLBACK_LAUNCHD_LIFECYCLE_SHA256'
ROLLBACK_CONTROL_PLANE_MODE='$rollback_control_plane_mode'
ROLLBACK_SUPERVISORD_CONF='$rollback_supervisord_conf'
ROLLBACK_ACTIVE_GATEWAY='$ROLLBACK_ACTIVE_GATEWAY'
ROLLBACK_AGENT_PRIOR_STATE='$ROLLBACK_AGENT_PRIOR_STATE'
ROLLBACK_EXPECTED_GENERATION='$DEPLOY_GENERATION'
ROLLBACK_EXPECTED_REVISION='$DEPLOY_REV'
ROLLBACK_PRIOR_GENERATION='$ROLLBACK_PRIOR_GENERATION'
ROLLBACK_PRIOR_REVISION='$ROLLBACK_PRIOR_REVISION'
ROLLBACK_INTENT='$ROLLBACK_INTENT'
ROLLBACK_SCRIPT_PATH='$ROLLBACK_SCRIPT'
ROLLBACK_COMPLETION_RECEIPT='$ROLLBACK_COMPLETION_RECEIPT'
MAC_SERVICE_NAME='$MAC_SERVICE_NAME'
HERMES_SERVICE_NAME='$HERMES_SERVICE_NAME'
OPENCLAW_SERVICE_NAME='$OPENCLAW_SERVICE_NAME'
NEMOCLAW_SERVICE_NAME='$NEMOCLAW_SERVICE_NAME'
MAC_AGENT_SERVICE_NAME='$MAC_AGENT_SERVICE_NAME'
MAC_LAUNCHD_LABEL='$MAC_LAUNCHD_LABEL'
HERMES_LAUNCHD_LABEL='$HERMES_LAUNCHD_LABEL'
OPENCLAW_LAUNCHD_LABEL='$OPENCLAW_LAUNCHD_LABEL'
NEMOCLAW_LAUNCHD_LABEL='$NEMOCLAW_LAUNCHD_LABEL'
MAC_AGENT_LAUNCHD_LABEL='$MAC_AGENT_LAUNCHD_LABEL'
MAC_SUPERVISORD_PROG='$MAC_SUPERVISORD_PROG'
HERMES_SUPERVISORD_PROG='$HERMES_SUPERVISORD_PROG'
OPENCLAW_SUPERVISORD_PROG='$OPENCLAW_SUPERVISORD_PROG'
NEMOCLAW_SUPERVISORD_PROG='$NEMOCLAW_SUPERVISORD_PROG'
AGENT_SUPERVISORD_PROG='$AGENT_SUPERVISORD_PROG'
ROLLBACK_TS="\$(date -u +%Y%m%dT%H%M%SZ)"
ROLLBACK_LOG_DIR='$LOG_DIR'
ROLLBACK_DIR_COUNT=0
ROLLBACK_FILE_COUNT=0
ROLLBACK_DIR_DESTINATIONS=()
ROLLBACK_DIR_CURRENT_BACKUPS=()
ROLLBACK_DIR_CURRENT_EXISTED=()
ROLLBACK_FILE_DESTINATIONS=()
ROLLBACK_FILE_CURRENT_BACKUPS=()
ROLLBACK_FILE_CURRENT_EXISTED=()
ROLLBACK_FILE_MODES=()
ROLLBACK_RESTORE_RECEIPT="\$ROLLBACK_LOG_DIR/rollback-\$ROLLBACK_TS-restore.json"
ROLLBACK_AUX_ARTIFACT_COUNT='$ROLLBACK_AUX_ARTIFACT_COUNT'
ROLLBACK_AUX_ARTIFACT_PATHS=()
ROLLBACK_AUX_ARTIFACT_BACKUPS=()
ROLLBACK_AUX_ARTIFACT_EXISTED=()
ROLLBACK_AUX_ARTIFACT_MODES=()
$rollback_aux_declarations

restore_dir() {
  local backup="\$1" dest="\$2" required="\${3:-0}" current_backup stage
  local current_existed=0
  if [ -z "\$backup" ] || [ ! -d "\$backup" ]; then
    if [ "\$required" = 1 ]; then
      echo "rollback failed: required directory backup is unavailable" >&2
      return 1
    fi
    return 0
  fi
  current_backup="\$MAC_HOME/backups/rollback-current.\$(basename "\$dest").\$ROLLBACK_TS.\$\$"
  stage="\${dest}.rollback-stage.\$ROLLBACK_TS.\$\$"
  [ ! -e "\$stage" ] || {
    echo "rollback failed: directory staging path already exists" >&2
    return 1
  }
  command cp -a "\$backup" "\$stage"
  if [ -e "\$dest" ]; then
    current_existed=1
    mv -f "\$dest" "\$current_backup"
  fi
  if ! mv -f "\$stage" "\$dest"; then
    rm -rf "\$stage"
    if [ -e "\$current_backup" ] && [ ! -e "\$dest" ]; then
      mv -f "\$current_backup" "\$dest"
    fi
    return 1
  fi
  ROLLBACK_DIR_DESTINATIONS[\$ROLLBACK_DIR_COUNT]="\$dest"
  ROLLBACK_DIR_CURRENT_BACKUPS[\$ROLLBACK_DIR_COUNT]="\$current_backup"
  ROLLBACK_DIR_CURRENT_EXISTED[\$ROLLBACK_DIR_COUNT]="\$current_existed"
  ROLLBACK_DIR_COUNT=\$(( ROLLBACK_DIR_COUNT + 1 ))
}

restore_absent_dir() {
  local dest="\$1" current_backup current_existed=0
  current_backup="\$MAC_HOME/backups/rollback-current.\$(basename "\$dest").\$ROLLBACK_TS.\$\$"
  if [ -e "\$dest" ] || [ -L "\$dest" ]; then
    [ -d "\$dest" ] && [ ! -L "\$dest" ] || {
      echo "rollback failed: current optional directory is not a regular directory" >&2
      return 1
    }
    current_existed=1
    mv -f "\$dest" "\$current_backup"
  fi
  ROLLBACK_DIR_DESTINATIONS[\$ROLLBACK_DIR_COUNT]="\$dest"
  ROLLBACK_DIR_CURRENT_BACKUPS[\$ROLLBACK_DIR_COUNT]="\$current_backup"
  ROLLBACK_DIR_CURRENT_EXISTED[\$ROLLBACK_DIR_COUNT]="\$current_existed"
  ROLLBACK_DIR_COUNT=\$(( ROLLBACK_DIR_COUNT + 1 ))
  mac_launchd_fsync_directory "\$MAC_HOME" user
}

rollback_python() {
  local candidate
  for candidate in "\$VENV/bin/python" python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if [ -x "\$candidate" ]; then
      printf '%s\n' "\$candidate"
      return 0
    fi
    if command -v "\$candidate" >/dev/null 2>&1; then
      command -v "\$candidate"
      return 0
    fi
  done
  echo "rollback failed: Python 3.9 or newer is unavailable" >&2
  return 1
}

ROLLBACK_PY="\$(rollback_python)"
"\$ROLLBACK_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
  || { echo "rollback failed: Python 3.9 or newer is required" >&2; exit 1; }
if ! [[ "\$ROLLBACK_SUPERVISOR_HELPER_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || ! [[ "\$ROLLBACK_LAUNCHD_LIFECYCLE_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "rollback failed: retained contract hash is malformed" >&2
  exit 1
fi

# Bind this executable to the exact pre-mutation rollback intent.  A successful
# post manifest may never exist after SIGKILL, so it is evidence of apply
# success only and is deliberately absent from the rollback authority chain.
ROLLBACK_INTENT_SHA256="\$("\$ROLLBACK_PY" - \
  "\$ROLLBACK_INTENT" "\$ROLLBACK_SCRIPT_PATH" \
  "\$ROLLBACK_EXPECTED_GENERATION" "\$ROLLBACK_EXPECTED_REVISION" \
  "\$ROLLBACK_PRIOR_GENERATION" "\$ROLLBACK_PRIOR_REVISION" \
  "\$ROLLBACK_COMPLETION_RECEIPT" "\$SUPERVISOR_KIND" \
  "\$ROLLBACK_ACTIVE_GATEWAY" "\$ROLLBACK_AGENT_PRIOR_STATE" <<'PY'
import hashlib
import json
import os
import stat
import sys


def private_regular_bytes(path, mode, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise SystemExit("rollback intent could not be opened safely")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size <= 0
            or before.st_size > limit
        ):
            raise SystemExit("rollback intent owner, mode, or size is invalid")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise SystemExit("rollback intent was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise SystemExit("rollback intent grew while reading")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise SystemExit("rollback intent changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


(
    intent_path,
    script_path,
    generation,
    revision,
    prior_generation,
    prior_revision,
    completion_path,
    supervisor,
    active_gateway,
    agent_prior_state,
) = sys.argv[1:]
intent_raw = private_regular_bytes(intent_path, 0o600, 4 * 1024 * 1024)
script_raw = private_regular_bytes(script_path, 0o700, 2 * 1024 * 1024)
try:
    intent = json.loads(intent_raw)
except (TypeError, ValueError):
    raise SystemExit("rollback intent is malformed")
contract = intent.get("rollback") if isinstance(intent, dict) else None
topology = intent.get("prior_topology") if isinstance(intent, dict) else None
script_sha256 = hashlib.sha256(script_raw).hexdigest()
if (
    not isinstance(intent, dict)
    or intent.get("schema") != "mac.fleet_node_rollback_intent.v1"
    or intent.get("status") != "armed"
    or intent.get("generation") != generation
    or intent.get("revision") != revision
    or intent.get("prior_generation") != (prior_generation or None)
    or intent.get("prior_revision") != (prior_revision or None)
    or intent.get("rollback_capable") is not True
    or not isinstance(contract, dict)
    or contract.get("path") != script_path
    or contract.get("sha256") != script_sha256
    or contract.get("completion_receipt") != completion_path
    or not isinstance(topology, dict)
    or topology.get("supervisor") != supervisor
    or topology.get("active_gateway") != active_gateway
    or topology.get("agent_prior_state") != agent_prior_state
):
    raise SystemExit("rollback intent does not match this generation contract")
print(hashlib.sha256(intent_raw).hexdigest())
PY
)" || { echo "rollback failed: pre-mutation intent contract is invalid" >&2; exit 1; }

ROLLBACK_CURRENT_IDENTITY="\$("\$ROLLBACK_PY" - \
  "\$MAC_HOME/mac.env" "\$MAC_HOME/deployed-source-revision" <<'PY'
import os
import re
import shlex
import stat
import sys


generation_re = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}\Z")
revision_re = re.compile(r"[0-9a-f]{40}\Z")


def optional_private_bytes(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise SystemExit("rollback generation witness could not be opened safely")
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > limit
        ):
            raise SystemExit("rollback generation witness is not owner-private and bounded")
        raw = os.read(descriptor, metadata.st_size + 1)
        if len(raw) != metadata.st_size:
            raise SystemExit("rollback generation witness changed while reading")
        return raw
    finally:
        os.close(descriptor)


generation = ""
env_raw = optional_private_bytes(sys.argv[1], 1024 * 1024)
if env_raw is not None:
    try:
        lines = env_raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise SystemExit("rollback environment witness is malformed")
    matches = []
    for line in lines:
        try:
            fields = shlex.split(line, posix=True)
        except ValueError:
            raise SystemExit("rollback environment witness is malformed")
        if len(fields) == 1 and fields[0].startswith("MAC_WORKER_DEPLOY_GENERATION="):
            matches.append(fields[0].split("=", 1)[1])
    if len(matches) > 1:
        raise SystemExit("rollback environment has duplicate generation witnesses")
    if matches:
        generation = matches[0]
        if not generation_re.fullmatch(generation):
            raise SystemExit("rollback environment generation witness is invalid")

revision = ""
revision_raw = optional_private_bytes(sys.argv[2], 256)
if revision_raw is not None:
    try:
        revision = revision_raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise SystemExit("rollback revision witness is malformed")
    if revision and not revision_re.fullmatch(revision):
        raise SystemExit("rollback revision witness is invalid")
print(generation + "|" + revision)
PY
)" || { echo "rollback failed: current generation could not be proven" >&2; exit 1; }
IFS='|' read -r rollback_current_generation rollback_current_revision \
  <<< "\$ROLLBACK_CURRENT_IDENTITY"
rollback_generation_state=""
if [ "\$rollback_current_generation" = "\$ROLLBACK_EXPECTED_GENERATION" ] \
    && [ "\$rollback_current_revision" = "\$ROLLBACK_EXPECTED_REVISION" ]; then
  rollback_generation_state=successor
elif [ -n "\$ROLLBACK_PRIOR_GENERATION" ] \
    && [ "\$rollback_current_generation" = "\$ROLLBACK_PRIOR_GENERATION" ] \
    && [ -n "\$ROLLBACK_PRIOR_REVISION" ] \
    && [ "\$rollback_current_revision" = "\$ROLLBACK_PRIOR_REVISION" ]; then
  rollback_generation_state=prior
elif [ -z "\$ROLLBACK_PRIOR_GENERATION" ] \
    && [ -z "\$rollback_current_generation" ] \
    && [ -n "\$ROLLBACK_PRIOR_REVISION" ] \
    && [ "\$rollback_current_revision" = "\$ROLLBACK_PRIOR_REVISION" ]; then
  rollback_generation_state=prior
elif { [ "\$rollback_current_generation" = "\$ROLLBACK_PRIOR_GENERATION" ] \
        || { [ -z "\$ROLLBACK_PRIOR_GENERATION" ] \
             && [ -z "\$rollback_current_generation" ]; }; } \
    && [ "\$rollback_current_revision" = "\$ROLLBACK_EXPECTED_REVISION" ]; then
  # Source publication precedes the atomic environment rewrite.  A kill in
  # that deliberately narrow interval therefore has a successor revision and
  # the prior generation marker; it is an in-contract partial apply, not an
  # alien generation.
  rollback_generation_state=applying
else
  echo "rollback failed: current node generation is outside this rollback contract" >&2
  exit 1
fi

if [ -e "\$ROLLBACK_COMPLETION_RECEIPT" ] || [ -L "\$ROLLBACK_COMPLETION_RECEIPT" ]; then
  [ "\$rollback_generation_state" = prior ] || {
    echo "rollback failed: completion receipt contradicts the current generation" >&2
    exit 1
  }
  "\$ROLLBACK_PY" - \
    "\$ROLLBACK_COMPLETION_RECEIPT" "\$ROLLBACK_EXPECTED_GENERATION" \
    "\$ROLLBACK_EXPECTED_REVISION" "\$ROLLBACK_INTENT_SHA256" \
    "\$ROLLBACK_PRIOR_GENERATION" "\$ROLLBACK_PRIOR_REVISION" <<'PY'
import hashlib
import json
import os
import stat
import sys


def re_full_sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def private_bytes(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > limit
        ):
            raise SystemExit("rollback completion evidence is not owner-private and bounded")
        raw = os.read(descriptor, metadata.st_size + 1)
        if len(raw) != metadata.st_size:
            raise SystemExit("rollback completion evidence changed while reading")
        return raw
    finally:
        os.close(descriptor)


path, generation, revision, intent_sha256, prior_generation, prior_revision = sys.argv[1:]
raw = private_bytes(path, 1024 * 1024)
try:
    receipt = json.loads(raw)
except (TypeError, ValueError):
    raise SystemExit("rollback completion receipt is malformed")
proof = receipt.get("prior_topology_proof") if isinstance(receipt, dict) else None
if (
    receipt.get("schema") != "mac.fleet_node_rollback.v1"
    or receipt.get("status") != "restored"
    or receipt.get("generation") != generation
    or receipt.get("revision") != revision
    or receipt.get("intent_sha256") != intent_sha256
    or receipt.get("prior_generation") != (prior_generation or None)
    or receipt.get("prior_revision") != (prior_revision or None)
    or not isinstance(proof, dict)
    or not isinstance(proof.get("path"), str)
    or not re_full_sha(proof.get("sha256"))
):
    raise SystemExit("rollback completion receipt does not match this contract")
proof_raw = private_bytes(proof["path"], 4 * 1024 * 1024)
if hashlib.sha256(proof_raw).hexdigest() != proof["sha256"]:
    raise SystemExit("rollback topology proof digest changed")
sys.stdout.buffer.write(raw)
PY
  exit \$?
fi

# Once mutation starts, controller-channel death must not strand a half-restored
# generation.  Every external operation below is independently bounded.
trap '' HUP INT TERM

# Open with O_NOFOLLOW, bind owner/mode/size/hash to the generation, rewind the
# same descriptor, then exec the interpreter against /dev/fd/N.  For the shell
# lifecycle contract the exec'd Bash sources that exact descriptor and invokes
# one named function; there is no check/path-open race.
verified_contract_call() {
  local kind="\$1" path="\$2" expected_sha256="\$3"
  shift 3
  "\$ROLLBACK_PY" - "\$kind" "\$path" "\$expected_sha256" "\$@" <<'PY'
import hashlib
import os
import re
import stat
import sys

kind, path, expected, *arguments = sys.argv[1:]
if kind not in {"python", "bash"} or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
    raise SystemExit("rollback contract verification arguments are invalid")
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(path, flags)
except OSError:
    raise SystemExit("rollback contract could not be opened safely")
try:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size <= 0
        or before.st_size > 1024 * 1024
    ):
        raise SystemExit("rollback contract owner, mode, or size is invalid")
    digest = hashlib.sha256()
    remaining = before.st_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            raise SystemExit("rollback contract was truncated")
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise SystemExit("rollback contract grew during verification")
    after = os.fstat(descriptor)
    if (
        digest.hexdigest() != expected
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    ):
        raise SystemExit("rollback contract hash or identity changed")
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.set_inheritable(descriptor, True)
    descriptor_path = "/dev/fd/%d" % descriptor
    if kind == "python":
        argv = [sys.executable, descriptor_path, *arguments]
        os.execve(sys.executable, argv, os.environ.copy())
    command = (
        'contract="\$1"; function="\$2"; shift 2; '
        '. "\$contract"; "\$function" "\$@"'
    )
    argv = [
        "/bin/bash",
        "-c",
        command,
        "verified-contract",
        descriptor_path,
        *arguments,
    ]
    os.execve("/bin/bash", argv, os.environ.copy())
finally:
    os.close(descriptor)
PY
}

verified_lifecycle_call() {
  local function="\$1"
  shift
  verified_contract_call \
    bash "\$ROLLBACK_LAUNCHD_LIFECYCLE" \
    "\$ROLLBACK_LAUNCHD_LIFECYCLE_SHA256" "\$function" "\$@"
}
mac_launchd_artifact_timeout() { verified_lifecycle_call mac_launchd_artifact_timeout "\$@"; }
mac_launchd_run_python_bounded() { verified_lifecycle_call mac_launchd_run_python_bounded "\$@"; }
mac_launchd_snapshot_file() { verified_lifecycle_call mac_launchd_snapshot_file "\$@"; }
mac_launchd_atomic_restore() { verified_lifecycle_call mac_launchd_atomic_restore "\$@"; }
mac_launchd_remove_file_and_fsync() { verified_lifecycle_call mac_launchd_remove_file_and_fsync "\$@"; }
mac_launchd_fsync_directory() { verified_lifecycle_call mac_launchd_fsync_directory "\$@"; }
mac_run_bounded() { verified_lifecycle_call mac_run_bounded "\$@"; }

compensate_restored_artifacts() {
  local compensation_rc=0 index destination current_backup existed mode
  set +e
  index=\$(( ROLLBACK_FILE_COUNT - 1 ))
  while [ "\$index" -ge 0 ]; do
    destination="\${ROLLBACK_FILE_DESTINATIONS[\$index]}"
    current_backup="\${ROLLBACK_FILE_CURRENT_BACKUPS[\$index]}"
    existed="\${ROLLBACK_FILE_CURRENT_EXISTED[\$index]}"
    mode="\${ROLLBACK_FILE_MODES[\$index]}"
    if [ "\$existed" = 1 ]; then
      mac_launchd_atomic_restore "\$current_backup" "\$destination" "\$mode" \
        || compensation_rc=1
    else
      mac_launchd_remove_file_and_fsync "\$destination" "\$mode" \
        || compensation_rc=1
    fi
    index=\$(( index - 1 ))
  done
  index=\$(( ROLLBACK_DIR_COUNT - 1 ))
  while [ "\$index" -ge 0 ]; do
    destination="\${ROLLBACK_DIR_DESTINATIONS[\$index]}"
    current_backup="\${ROLLBACK_DIR_CURRENT_BACKUPS[\$index]}"
    existed="\${ROLLBACK_DIR_CURRENT_EXISTED[\$index]}"
    rm -rf "\$destination" || compensation_rc=1
    if [ "\$existed" = 1 ]; then
      mv -f "\$current_backup" "\$destination" || compensation_rc=1
    fi
    index=\$(( index - 1 ))
  done
  return "\$compensation_rc"
}

rollback_error_handler() {
  local original_rc="\${1:-1}" compensation_rc=0
  trap - ERR
  compensate_restored_artifacts || compensation_rc=\$?
  if [ "\$compensation_rc" -ne 0 ]; then
    echo "rollback failed and artifact compensation was incomplete" >&2
  else
    echo "rollback failed; restored artifacts were compensated to the pre-rollback generation" >&2
  fi
  exit "\$original_rc"
}
trap 'rollback_error_handler "\$?"' ERR

rollback_supervisor="\${SUPERVISOR_KIND:-\$OS_KIND}"
rollback_control_mode="\$ROLLBACK_CONTROL_PLANE_MODE"
rollback_args=()
rollback_restore_args=(
  --active-gateway "\$ROLLBACK_ACTIVE_GATEWAY"
  --agent-prior-state "\$ROLLBACK_AGENT_PRIOR_STATE"
)
case "\$rollback_supervisor" in
  systemd|linux)
    rollback_supervisor=systemd
    rollback_args=(
      --control-plane "\$MAC_SERVICE_NAME"
      --hermes-gateway "\$HERMES_SERVICE_NAME"
      --openclaw-gateway "\$OPENCLAW_SERVICE_NAME"
      --nemoclaw-gateway "\$NEMOCLAW_SERVICE_NAME"
      --agent "\$MAC_AGENT_SERVICE_NAME"
    )
    ;;
  supervisord)
    rollback_args=(
      --control-plane "\$MAC_SUPERVISORD_PROG"
      --hermes-gateway "\$HERMES_SUPERVISORD_PROG"
      --openclaw-gateway "\$OPENCLAW_SUPERVISORD_PROG"
      --nemoclaw-gateway "\$NEMOCLAW_SUPERVISORD_PROG"
      --agent "\$AGENT_SUPERVISORD_PROG"
      --supervisord-scope system
    )
    ;;
  launchd|darwin)
    rollback_supervisor=launchd
    if [ "\$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ]; then
      rollback_control_mode=system
    elif [ "\$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ]; then
      rollback_control_mode=gui
    else
      rollback_control_mode=inactive
    fi
    rollback_args=(
      --control-plane "\$MAC_LAUNCHD_LABEL"
      --hermes-gateway "\$HERMES_LAUNCHD_LABEL"
      --openclaw-gateway "\$OPENCLAW_LAUNCHD_LABEL"
      --nemoclaw-gateway "\$NEMOCLAW_LAUNCHD_LABEL"
      --agent "\$MAC_AGENT_LAUNCHD_LABEL"
      --launchd-uid "\$(id -u)"
      --launchd-system-supervisor "\$DARWIN_SYSTEM_SUPERVISOR_LABEL"
      --launchd-control-system-plist "/Library/LaunchDaemons/\$MAC_LAUNCHD_LABEL.plist"
      --launchd-control-gui-plist "\$HOME/Library/LaunchAgents/\$MAC_LAUNCHD_LABEL.plist"
      --launchd-system-supervisor-plist "/Library/LaunchDaemons/\$DARWIN_SYSTEM_SUPERVISOR_LABEL.plist"
      --launchd-hermes-plist "\$HOME/Library/LaunchAgents/\$HERMES_LAUNCHD_LABEL.plist"
      --launchd-openclaw-plist "\$HOME/Library/LaunchAgents/\$OPENCLAW_LAUNCHD_LABEL.plist"
      --launchd-nemoclaw-plist "\$HOME/Library/LaunchAgents/\$NEMOCLAW_LAUNCHD_LABEL.plist"
      --launchd-agent-plist "\$HOME/Library/LaunchAgents/\$MAC_AGENT_LAUNCHD_LABEL.plist"
    )
    [ "\$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" = 1 ] \
      && rollback_args+=(--launchd-system-supervisor-was-active)
    ;;
  *) echo "rollback failed: unsupported supervisor" >&2; exit 1 ;;
esac

require_rollback_directory() {
  [ -n "\$1" ] && [ -d "\$1" ] && [ ! -L "\$1" ] || {
    echo "rollback failed: required directory backup is unavailable" >&2
    return 1
  }
}

require_rollback_file() {
  [ -n "\$1" ] && [ -f "\$1" ] && [ ! -L "\$1" ] || {
    echo "rollback failed: required service backup is unavailable" >&2
    return 1
  }
}

rollback_directory_state() {
  local backup="\$1" destination="\$2"
  if [ -d "\$backup" ] && [ ! -L "\$backup" ]; then
    printf '%s\n' backup
    return 0
  fi
  if [ -e "\$backup" ] || [ -L "\$backup" ]; then
    echo "rollback failed: planned directory backup is unsafe" >&2
    return 1
  fi
  if [ "\$rollback_generation_state" = prior ] \
      && [ -d "\$destination" ] && [ ! -L "\$destination" ]; then
    printf '%s\n' canonical-prior
    return 0
  fi
  echo "rollback failed: neither a durable backup nor the untouched prior directory is available" >&2
  return 1
}

SRC_ROLLBACK_STATE="\$(rollback_directory_state "\$SRC_BACKUP" "\$SRC_DIR")"
VENV_ROLLBACK_STATE="\$(rollback_directory_state "\$VENV_BACKUP" "\$VENV")"
if [ -n "\$HERMES_BACKUP" ]; then
  HERMES_ROLLBACK_STATE="\$(rollback_directory_state "\$HERMES_BACKUP" "\$HERMES_DIR")"
else
  HERMES_ROLLBACK_STATE=prior-absent
fi
require_rollback_directory "\$BIN_BACKUP"
case "\$OPENCLAW_HOME_EXISTED" in
  0)
    [ -z "\$OPENCLAW_HOME_BACKUP" ] \
      || { echo "rollback failed: contradictory OpenClaw runtime snapshot" >&2; exit 1; }
    ;;
  1) require_rollback_directory "\$OPENCLAW_HOME_BACKUP" ;;
  *) echo "rollback failed: invalid OpenClaw runtime snapshot state" >&2; exit 1 ;;
esac
[ "\$ROLLBACK_ACTIVE_GATEWAY" != openclaw ] \
  || [ "\$OPENCLAW_HOME_EXISTED" = 1 ] \
  || { echo "rollback failed: active OpenClaw topology lacks runtime state" >&2; exit 1; }
case "\$ROLLBACK_ACTIVE_GATEWAY" in
  hermes|openclaw|nemoclaw|none) ;;
  *) echo "rollback failed: prior gateway topology is unavailable" >&2; exit 1 ;;
esac
case "\$ROLLBACK_AGENT_PRIOR_STATE" in
  active|inactive|absent) ;;
  *) echo "rollback failed: prior worker topology is unavailable" >&2; exit 1 ;;
esac
rollback_aux_index=0
while [ "\$rollback_aux_index" -lt "\$ROLLBACK_AUX_ARTIFACT_COUNT" ]; do
  case "\${ROLLBACK_AUX_ARTIFACT_MODES[\$rollback_aux_index]}" in
    user|system) ;;
    *) echo "rollback failed: invalid auxiliary artifact mode" >&2; exit 1 ;;
  esac
  case "\${ROLLBACK_AUX_ARTIFACT_EXISTED[\$rollback_aux_index]}" in
    0) ;;
    1) require_rollback_file \
         "\${ROLLBACK_AUX_ARTIFACT_BACKUPS[\$rollback_aux_index]}" ;;
    *) echo "rollback failed: invalid auxiliary artifact snapshot" >&2; exit 1 ;;
  esac
  rollback_aux_index=\$(( rollback_aux_index + 1 ))
done
case "\$rollback_supervisor" in
  systemd)
    [ "\$HERMES_UNIT_MUTATED" != 1 ] || require_rollback_file "\$HERMES_UNIT_BACKUP"
    [ "\$MAC_AGENT_UNIT_MUTATED" != 1 ] || require_rollback_file "\$MAC_AGENT_UNIT_BACKUP"
    [ "\$MAC_UNIT_MUTATED" != 1 ] || require_rollback_file "\$MAC_UNIT_BACKUP"
    ;;
  supervisord)
    [ "\$MAC_UNIT_MUTATED" != 1 ] || require_rollback_file "\$MAC_UNIT_BACKUP"
    ;;
  launchd)
    [ "\$HERMES_PLIST_MUTATED" != 1 ] || require_rollback_file "\$HERMES_PLIST_BACKUP"
    [ "\$MAC_AGENT_PLIST_MUTATED" != 1 ] || require_rollback_file "\$MAC_AGENT_PLIST_BACKUP"
    [ "\$DARWIN_SYSTEM_PLIST_MUTATED" != 1 ] \
      || [ "\$DARWIN_SYSTEM_LAUNCHD_ACTIVE" != 1 ] \
      || require_rollback_file "\$DARWIN_SYSTEM_PLIST_BACKUP"
    [ "\$MAC_PLIST_MUTATED" != 1 ] \
      || [ "\$DARWIN_GUI_LAUNCHD_ACTIVE" != 1 ] \
      || require_rollback_file "\$MAC_PLIST_BACKUP"
    [ "\$DARWIN_SYSTEM_SUPERVISOR_PLIST_MUTATED" != 1 ] \
      || [ "\$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" != 1 ] \
      || require_rollback_file "\$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP"
    ;;
esac

verified_contract_call \
  python "\$ROLLBACK_SUPERVISOR_HELPER" "\$ROLLBACK_SUPERVISOR_HELPER_SHA256" quiesce \
  --supervisor "\$rollback_supervisor" \
  --control-plane-mode "\$rollback_control_mode" \
  --control-plane-port "\$MAC_PORT" \
  --receipt "\$ROLLBACK_LOG_DIR/rollback-\$ROLLBACK_TS-quiesce.json" \
  "\${rollback_args[@]}"

# Supervisor quiescence cannot prove OpenShell stopped a sandbox after its
# launcher crashed.  Use the current generation's reviewed withdrawal path,
# under an outer process-group deadline, before replacing its identity/state
# tree.  The withdrawal independently deletes and proves the exact managed
# sandbox absent.
if [ "\$rollback_generation_state" = successor ] \
    && { [ -e "\$MAC_HOME/openclaw" ] || [ -L "\$MAC_HOME/openclaw" ]; }; then
  [ -d "\$MAC_HOME/openclaw" ] && [ ! -L "\$MAC_HOME/openclaw" ] \
    || { echo "rollback failed: current OpenClaw runtime tree is unsafe" >&2; exit 1; }
  current_openclaw_installer="\$SRC_DIR/deploy/openclaw/install-openclaw-gateway.sh"
  if ! { [ -f "\$current_openclaw_installer" ] \
      && [ ! -L "\$current_openclaw_installer" ] \
      && [ -x "\$current_openclaw_installer" ]; }; then
    current_openclaw_installer="\$SRC_BACKUP/deploy/openclaw/install-openclaw-gateway.sh"
  fi
  [ -f "\$current_openclaw_installer" ] \
    && [ ! -L "\$current_openclaw_installer" ] \
    && [ -x "\$current_openclaw_installer" ] \
    || { echo "rollback failed: retained OpenClaw withdrawal path is unavailable" >&2; exit 1; }
  current_openclaw_source="\$(cd "\$(dirname "\$current_openclaw_installer")/../.." && pwd -P)"
  MAC_HOME="\$MAC_HOME" \
  MAC_SRC="\$current_openclaw_source" \
  MAC_OPENCLAW_FLEET_NAME='$FLEET_NAME' \
  MAC_OPENCLAW_SUPERVISOR="\$rollback_supervisor" \
    mac_run_bounded 360 "\$current_openclaw_installer" withdraw
fi

restore_dir_or_keep_prior() {
  local backup="\$1" destination="\$2" state="\$3"
  case "\$state" in
    backup) restore_dir "\$backup" "\$destination" 1 ;;
    canonical-prior)
      [ ! -e "\$backup" ] && [ ! -L "\$backup" ] \
        && [ -d "\$destination" ] && [ ! -L "\$destination" ] \
        || { echo "rollback failed: untouched prior directory changed after preflight" >&2; return 1; }
      ;;
    *) echo "rollback failed: invalid directory restoration state" >&2; return 1 ;;
  esac
}

restore_dir_or_keep_prior "\$SRC_BACKUP" "\$SRC_DIR" "\$SRC_ROLLBACK_STATE"
restore_dir_or_keep_prior "\$VENV_BACKUP" "\$VENV" "\$VENV_ROLLBACK_STATE"
if [ "\$HERMES_ROLLBACK_STATE" = prior-absent ]; then
  restore_absent_dir "\$HERMES_DIR"
else
  restore_dir_or_keep_prior \
    "\$HERMES_BACKUP" "\$HERMES_DIR" "\$HERMES_ROLLBACK_STATE"
fi
if [ "\$OPENCLAW_HOME_EXISTED" = 1 ]; then
  restore_dir "\$OPENCLAW_HOME_BACKUP" "\$MAC_HOME/openclaw" 1
else
  restore_absent_dir "\$MAC_HOME/openclaw"
fi
restore_dir "\$BIN_BACKUP" "\$MAC_HOME/bin" 1

restore_file_or_remove() {
  local backup="\$1" destination="\$2" mode="\${3:-user}"
  local current_backup existed
  current_backup="\$MAC_HOME/backups/rollback-current-file.\$ROLLBACK_TS.\$\$.\$ROLLBACK_FILE_COUNT"
  existed="\$(mac_launchd_snapshot_file \
    "\$destination" "\$current_backup" "\$mode")"
  case "\$existed" in
    0|1) ;;
    *) echo "rollback failed: invalid current-file snapshot" >&2; return 1 ;;
  esac
  ROLLBACK_FILE_DESTINATIONS[\$ROLLBACK_FILE_COUNT]="\$destination"
  ROLLBACK_FILE_CURRENT_BACKUPS[\$ROLLBACK_FILE_COUNT]="\$current_backup"
  ROLLBACK_FILE_CURRENT_EXISTED[\$ROLLBACK_FILE_COUNT]="\$existed"
  ROLLBACK_FILE_MODES[\$ROLLBACK_FILE_COUNT]="\$mode"
  ROLLBACK_FILE_COUNT=\$(( ROLLBACK_FILE_COUNT + 1 ))
  if [ -n "\$backup" ] && [ -f "\$backup" ] && [ ! -L "\$backup" ]; then
    mac_launchd_atomic_restore "\$backup" "\$destination" "\$mode"
  else
    mac_launchd_remove_file_and_fsync "\$destination" "\$mode"
  fi
}

# Wrappers and successor gateway definitions live outside SRC_DIR/VENV.  They
# therefore participate in the same compensation journal as service files;
# otherwise a later rollback could run restored source through a successor
# generation wrapper or leave a successor unit/plist runnable after reboot.
rollback_aux_index=0
while [ "\$rollback_aux_index" -lt "\$ROLLBACK_AUX_ARTIFACT_COUNT" ]; do
  rollback_aux_backup=""
  if [ "\${ROLLBACK_AUX_ARTIFACT_EXISTED[\$rollback_aux_index]}" = 1 ]; then
    rollback_aux_backup="\${ROLLBACK_AUX_ARTIFACT_BACKUPS[\$rollback_aux_index]}"
  fi
  restore_file_or_remove \
    "\$rollback_aux_backup" \
    "\${ROLLBACK_AUX_ARTIFACT_PATHS[\$rollback_aux_index]}" \
    "\${ROLLBACK_AUX_ARTIFACT_MODES[\$rollback_aux_index]}"
  rollback_aux_index=\$(( rollback_aux_index + 1 ))
done

case "\$rollback_supervisor" in
  systemd)
    [ "\$MAC_UNIT_MUTATED" != 1 ] \
      || restore_file_or_remove "\$MAC_UNIT_BACKUP" "/etc/systemd/system/\$MAC_SERVICE_NAME" system
    [ "\$HERMES_UNIT_MUTATED" != 1 ] \
      || restore_file_or_remove "\$HERMES_UNIT_BACKUP" "/etc/systemd/system/\$HERMES_SERVICE_NAME" system
    [ "\$MAC_AGENT_UNIT_MUTATED" != 1 ] \
      || restore_file_or_remove "\$MAC_AGENT_UNIT_BACKUP" "/etc/systemd/system/\$MAC_AGENT_SERVICE_NAME" system
    ;;
  supervisord)
    [ "\$MAC_UNIT_MUTATED" != 1 ] \
      || restore_file_or_remove "\$MAC_UNIT_BACKUP" "\$ROLLBACK_SUPERVISORD_CONF" system
    ;;
  launchd)
    mkdir -p "\$HOME/Library/LaunchAgents"
    [ "\$MAC_PLIST_MUTATED" != 1 ] \
      || restore_file_or_remove "\$MAC_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/\$MAC_LAUNCHD_LABEL.plist"
    [ "\$DARWIN_SYSTEM_PLIST_MUTATED" != 1 ] \
      || restore_file_or_remove "\$DARWIN_SYSTEM_PLIST_BACKUP" "/Library/LaunchDaemons/\$MAC_LAUNCHD_LABEL.plist" system
    [ "\$DARWIN_SYSTEM_SUPERVISOR_PLIST_MUTATED" != 1 ] \
      || restore_file_or_remove "\$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP" "/Library/LaunchDaemons/\$DARWIN_SYSTEM_SUPERVISOR_LABEL.plist" system
    [ "\$HERMES_PLIST_MUTATED" != 1 ] \
      || restore_file_or_remove "\$HERMES_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/\$HERMES_LAUNCHD_LABEL.plist"
    [ "\$MAC_AGENT_PLIST_MUTATED" != 1 ] \
      || restore_file_or_remove "\$MAC_AGENT_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/\$MAC_AGENT_LAUNCHD_LABEL.plist"
    ;;
esac

# The venv path may now name the restored generation; resolve it again before
# asking the exact supervisor helper to start and prove the fallback topology.
ROLLBACK_PY="\$(rollback_python)"
verified_contract_call \
  python "\$ROLLBACK_SUPERVISOR_HELPER" "\$ROLLBACK_SUPERVISOR_HELPER_SHA256" restore \
  --supervisor "\$rollback_supervisor" \
  --control-plane-mode "\$rollback_control_mode" \
  --control-plane-port "\$MAC_PORT" \
  --receipt "\$ROLLBACK_RESTORE_RECEIPT" \
  "\${rollback_args[@]}" \
  "\${rollback_restore_args[@]}"

# Publish one generation-level completion receipt only after the exact prior
# topology proof is durable.  A replay validates and returns these same bytes
# before attempting any further mutation.
"\$ROLLBACK_PY" - \
  "\$ROLLBACK_COMPLETION_RECEIPT" "\$ROLLBACK_RESTORE_RECEIPT" \
  "\$ROLLBACK_INTENT" "\$ROLLBACK_INTENT_SHA256" \
  "\$ROLLBACK_EXPECTED_GENERATION" "\$ROLLBACK_EXPECTED_REVISION" \
  "\$ROLLBACK_PRIOR_GENERATION" "\$ROLLBACK_PRIOR_REVISION" \
  "\$ROLLBACK_ACTIVE_GATEWAY" "\$ROLLBACK_AGENT_PRIOR_STATE" <<'PY'
import datetime as dt
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


def private_bytes(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > limit
        ):
            raise SystemExit("rollback completion input is not owner-private and bounded")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise SystemExit("rollback completion input was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise SystemExit("rollback completion input grew while reading")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise SystemExit("rollback completion input changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


(
    output_raw,
    topology_raw_path,
    intent_raw_path,
    expected_intent_sha256,
    generation,
    revision,
    prior_generation,
    prior_revision,
    active_gateway,
    agent_prior_state,
) = sys.argv[1:]
topology_raw = private_bytes(topology_raw_path, 4 * 1024 * 1024)
intent_raw = private_bytes(intent_raw_path, 4 * 1024 * 1024)
if hashlib.sha256(intent_raw).hexdigest() != expected_intent_sha256:
    raise SystemExit("rollback intent changed before completion")
try:
    topology = json.loads(topology_raw)
    intent = json.loads(intent_raw)
except (TypeError, ValueError):
    raise SystemExit("rollback completion input is malformed")
prior_topology = topology.get("prior_topology") if isinstance(topology, dict) else None
contract = intent.get("rollback") if isinstance(intent, dict) else None
if (
    topology.get("schema") != "mac.fleet_node_rollback_supervisor.v1"
    or topology.get("status") != "passed"
    or topology.get("action") != "restore"
    or not isinstance(prior_topology, dict)
    or prior_topology.get("active_gateway") != active_gateway
    or prior_topology.get("agent_state") != agent_prior_state
    or not isinstance(contract, dict)
    or intent.get("schema") != "mac.fleet_node_rollback_intent.v1"
    or intent.get("generation") != generation
    or intent.get("revision") != revision
):
    raise SystemExit("rollback topology proof does not match the generation contract")
payload = {
    "schema": "mac.fleet_node_rollback.v1",
    "status": "restored",
    "generation": generation,
    "revision": revision,
    "prior_generation": prior_generation or None,
    "prior_revision": prior_revision or None,
    "intent_sha256": expected_intent_sha256,
    "rollback_sha256": contract.get("sha256"),
    "prior_topology_proof": {
        "path": topology_raw_path,
        "sha256": hashlib.sha256(topology_raw).hexdigest(),
    },
    "restored_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
}
output = Path(output_raw)
output.parent.mkdir(parents=True, exist_ok=True)
if output.exists() or output.is_symlink():
    raise SystemExit("rollback completion receipt appeared concurrently")
descriptor, temporary_raw = tempfile.mkstemp(
    prefix="." + output.name + ".", dir=str(output.parent)
)
temporary = Path(temporary_raw)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
    directory = os.open(str(output.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    temporary.unlink(missing_ok=True)
PY

trap - ERR
cleanup_rollback_directory() {
  local path="\$1"
  mac_launchd_run_python_bounded \
    user "\$(mac_launchd_artifact_timeout)" '
import os
import shutil
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
backup_root = Path(sys.argv[2]).resolve()
if path.parent.resolve() != backup_root:
    raise SystemExit("refusing rollback cleanup outside the backup root")
try:
    metadata = path.lstat()
except FileNotFoundError:
    raise SystemExit(0)
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
    raise SystemExit("refusing non-directory rollback cleanup artifact")
shutil.rmtree(path)
directory = os.open(str(path.parent), os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
' "\$path" "\$MAC_HOME/backups"
}

rollback_cleanup_warning=0
rollback_cleanup_index=0
while [ "\$rollback_cleanup_index" -lt "\$ROLLBACK_DIR_COUNT" ]; do
  if ! cleanup_rollback_directory \
      "\${ROLLBACK_DIR_CURRENT_BACKUPS[\$rollback_cleanup_index]}"; then
    echo "rollback warning: retained a current-generation directory snapshot" >&2
    rollback_cleanup_warning=1
  fi
  rollback_cleanup_index=\$(( rollback_cleanup_index + 1 ))
done
rollback_cleanup_index=0
while [ "\$rollback_cleanup_index" -lt "\$ROLLBACK_FILE_COUNT" ]; do
  if ! mac_launchd_remove_file_and_fsync \
      "\${ROLLBACK_FILE_CURRENT_BACKUPS[\$rollback_cleanup_index]}" user; then
    echo "rollback warning: retained a current-generation file snapshot" >&2
    rollback_cleanup_warning=1
  fi
  rollback_cleanup_index=\$(( rollback_cleanup_index + 1 ))
done
[ "\$rollback_cleanup_warning" -eq 0 ] \
  || echo "rollback restored the prior generation; stale cleanup snapshots remain" >&2
echo "rollback complete from $DEPLOY_TS" >&2
command cat "\$ROLLBACK_COMPLETION_RECEIPT"
EOF
  # Rollback programs contain sensitive paths. Publish both aliases through
  # the bounded lifecycle helper so file bytes and parent-directory entries
  # are fsynced before DEPLOY_ROLLBACK_ARMED can become true.
  chmod 0700 "$rollback_stage"
  mac_launchd_atomic_replace \
    "$rollback_stage" "$ROLLBACK_SCRIPT" user 0700 "$(id -u)" "$(id -g)"
  mac_launchd_atomic_restore "$ROLLBACK_SCRIPT" "$ROLLBACK_LATEST" user
}

verify_phase2_rollback_intent() {
  MAC_ROLLBACK_INTENT="$ROLLBACK_INTENT" \
  MAC_ROLLBACK_SCRIPT="$ROLLBACK_SCRIPT" \
  MAC_ROLLBACK_COMPLETION="$ROLLBACK_COMPLETION_RECEIPT" \
  MAC_ROLLBACK_AGENT="$AGENT" MAC_ROLLBACK_FLEET="$FLEET_NAME" \
  MAC_ROLLBACK_OS="$OS_KIND" MAC_ROLLBACK_GENERATION="$DEPLOY_GENERATION" \
  MAC_ROLLBACK_REVISION="$DEPLOY_REV" \
  MAC_ROLLBACK_PRIOR_GENERATION="$ROLLBACK_PRIOR_GENERATION" \
  MAC_ROLLBACK_PRIOR_REVISION="$ROLLBACK_PRIOR_REVISION" \
  MAC_ROLLBACK_SUPERVISOR="$SUPERVISOR_KIND" \
  MAC_ROLLBACK_ACTIVE_GATEWAY="$ROLLBACK_ACTIVE_GATEWAY" \
  MAC_ROLLBACK_AGENT_PRIOR_STATE="$ROLLBACK_AGENT_PRIOR_STATE" \
  MAC_ROLLBACK_NODE_IDENTITY_SHA256="$NODE_IDENTITY_SHA256" \
  MAC_ROLLBACK_PREREQUISITE_BUNDLE_SHA256="$PREREQUISITE_BUNDLE_SHA256" \
  MAC_ROLLBACK_PREREQUISITE_EXPECTATIONS_SHA256="$PREREQUISITE_EXPECTATIONS_SHA256" \
  MAC_ROLLBACK_SRC="$SRC_DIR" MAC_ROLLBACK_SRC_BACKUP="$SRC_BACKUP" \
  MAC_ROLLBACK_VENV="$VENV" MAC_ROLLBACK_VENV_BACKUP="$VENV_BACKUP" \
  MAC_ROLLBACK_HERMES="$HERMES_DIR" MAC_ROLLBACK_HERMES_BACKUP="$HERMES_BACKUP" \
  MAC_ROLLBACK_BIN_BACKUP="$BIN_BACKUP" \
  MAC_ROLLBACK_OPENCLAW_BACKUP="$OPENCLAW_HOME_BACKUP" \
  MAC_ROLLBACK_OPENCLAW_EXISTED="$OPENCLAW_HOME_EXISTED" \
  MAC_ROLLBACK_SUPERVISOR_HELPER="$ROLLBACK_SUPERVISOR_HELPER" \
  MAC_ROLLBACK_SUPERVISOR_HELPER_SHA256="$ROLLBACK_SUPERVISOR_HELPER_SHA256" \
  MAC_ROLLBACK_LIFECYCLE="$ROLLBACK_LAUNCHD_LIFECYCLE" \
  MAC_ROLLBACK_LIFECYCLE_SHA256="$ROLLBACK_LAUNCHD_LIFECYCLE_SHA256" \
    "$PY" - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import stat


def private_bytes(path: str, mode: int, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size <= 0
            or before.st_size > limit
        ):
            raise SystemExit("phase-2 rollback intent artifact is unsafe")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise SystemExit("phase-2 rollback intent artifact was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise SystemExit("phase-2 rollback intent artifact grew while reading")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise SystemExit("phase-2 rollback intent artifact changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


intent_raw = private_bytes(os.environ["MAC_ROLLBACK_INTENT"], 0o600, 4 * 1024 * 1024)
script_raw = private_bytes(os.environ["MAC_ROLLBACK_SCRIPT"], 0o700, 2 * 1024 * 1024)
try:
    intent = json.loads(intent_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-2 rollback intent is malformed")
expected_artifacts = {
    "source": {"path": os.environ["MAC_ROLLBACK_SRC"], "backup": os.environ["MAC_ROLLBACK_SRC_BACKUP"]},
    "venv": {"path": os.environ["MAC_ROLLBACK_VENV"], "backup": os.environ["MAC_ROLLBACK_VENV_BACKUP"]},
    "hermes": {"path": os.environ["MAC_ROLLBACK_HERMES"], "backup": os.environ["MAC_ROLLBACK_HERMES_BACKUP"] or None},
    "bin_backup": os.environ["MAC_ROLLBACK_BIN_BACKUP"],
    "openclaw_backup": os.environ["MAC_ROLLBACK_OPENCLAW_BACKUP"] or None,
    "openclaw_existed": os.environ["MAC_ROLLBACK_OPENCLAW_EXISTED"] == "1",
}
expected_contracts = {
    "supervisor_helper": {
        "path": os.environ["MAC_ROLLBACK_SUPERVISOR_HELPER"],
        "sha256": os.environ["MAC_ROLLBACK_SUPERVISOR_HELPER_SHA256"],
    },
    "lifecycle_helper": {
        "path": os.environ["MAC_ROLLBACK_LIFECYCLE"],
        "sha256": os.environ["MAC_ROLLBACK_LIFECYCLE_SHA256"],
    },
}
rollback = intent.get("rollback") if isinstance(intent, dict) else None
if (
    not isinstance(intent, dict)
    or intent.get("schema") != "mac.fleet_node_rollback_intent.v1"
    or intent.get("status") != "armed"
    or intent.get("agent") != os.environ["MAC_ROLLBACK_AGENT"]
    or intent.get("fleet") != os.environ["MAC_ROLLBACK_FLEET"]
    or intent.get("os_kind") != os.environ["MAC_ROLLBACK_OS"]
    or intent.get("generation") != os.environ["MAC_ROLLBACK_GENERATION"]
    or intent.get("revision") != os.environ["MAC_ROLLBACK_REVISION"]
    or intent.get("prior_generation") != (os.environ["MAC_ROLLBACK_PRIOR_GENERATION"] or None)
    or intent.get("prior_revision") != (os.environ["MAC_ROLLBACK_PRIOR_REVISION"] or None)
    or intent.get("rollback_capable") is not True
    or intent.get("prior_topology") != {
        "supervisor": os.environ["MAC_ROLLBACK_SUPERVISOR"],
        "active_gateway": os.environ["MAC_ROLLBACK_ACTIVE_GATEWAY"],
        "agent_prior_state": os.environ["MAC_ROLLBACK_AGENT_PRIOR_STATE"],
    }
    or intent.get("prerequisites") != {
        "schema": "mac.fleet_prerequisite_rollback_binding.v1",
        "node_identity_sha256": os.environ["MAC_ROLLBACK_NODE_IDENTITY_SHA256"],
        "bundle_sha256": os.environ["MAC_ROLLBACK_PREREQUISITE_BUNDLE_SHA256"],
        "expectations_sha256": os.environ[
            "MAC_ROLLBACK_PREREQUISITE_EXPECTATIONS_SHA256"
        ],
    }
    or intent.get("artifacts") != expected_artifacts
    or intent.get("contracts") != expected_contracts
    or not isinstance(rollback, dict)
    or rollback.get("path") != os.environ["MAC_ROLLBACK_SCRIPT"]
    or rollback.get("sha256") != hashlib.sha256(script_raw).hexdigest()
    or rollback.get("completion_receipt") != os.environ["MAC_ROLLBACK_COMPLETION"]
):
    raise SystemExit("phase-2 rollback intent belongs to another node generation")
print(hashlib.sha256(intent_raw).hexdigest())
PY
}

write_phase2_rollback_intent() {
  [ ! -e "$ROLLBACK_INTENT" ] && [ ! -L "$ROLLBACK_INTENT" ] \
    || die "phase-2 rollback intent appeared concurrently"
  MAC_ROLLBACK_INTENT="$ROLLBACK_INTENT" \
  MAC_ROLLBACK_SCRIPT="$ROLLBACK_SCRIPT" \
  MAC_ROLLBACK_COMPLETION="$ROLLBACK_COMPLETION_RECEIPT" \
  MAC_ROLLBACK_AGENT="$AGENT" MAC_ROLLBACK_FLEET="$FLEET_NAME" \
  MAC_ROLLBACK_OS="$OS_KIND" MAC_ROLLBACK_GENERATION="$DEPLOY_GENERATION" \
  MAC_ROLLBACK_REVISION="$DEPLOY_REV" \
  MAC_ROLLBACK_PRIOR_GENERATION="$ROLLBACK_PRIOR_GENERATION" \
  MAC_ROLLBACK_PRIOR_REVISION="$ROLLBACK_PRIOR_REVISION" \
  MAC_ROLLBACK_SUPERVISOR="$SUPERVISOR_KIND" \
  MAC_ROLLBACK_ACTIVE_GATEWAY="$ROLLBACK_ACTIVE_GATEWAY" \
  MAC_ROLLBACK_AGENT_PRIOR_STATE="$ROLLBACK_AGENT_PRIOR_STATE" \
  MAC_ROLLBACK_NODE_IDENTITY_SHA256="$NODE_IDENTITY_SHA256" \
  MAC_ROLLBACK_PREREQUISITE_BUNDLE_SHA256="$PREREQUISITE_BUNDLE_SHA256" \
  MAC_ROLLBACK_PREREQUISITE_EXPECTATIONS_SHA256="$PREREQUISITE_EXPECTATIONS_SHA256" \
  MAC_ROLLBACK_SRC="$SRC_DIR" MAC_ROLLBACK_SRC_BACKUP="$SRC_BACKUP" \
  MAC_ROLLBACK_VENV="$VENV" MAC_ROLLBACK_VENV_BACKUP="$VENV_BACKUP" \
  MAC_ROLLBACK_HERMES="$HERMES_DIR" MAC_ROLLBACK_HERMES_BACKUP="$HERMES_BACKUP" \
  MAC_ROLLBACK_BIN_BACKUP="$BIN_BACKUP" \
  MAC_ROLLBACK_OPENCLAW_BACKUP="$OPENCLAW_HOME_BACKUP" \
  MAC_ROLLBACK_OPENCLAW_EXISTED="$OPENCLAW_HOME_EXISTED" \
  MAC_ROLLBACK_SUPERVISOR_HELPER="$ROLLBACK_SUPERVISOR_HELPER" \
  MAC_ROLLBACK_SUPERVISOR_HELPER_SHA256="$ROLLBACK_SUPERVISOR_HELPER_SHA256" \
  MAC_ROLLBACK_LIFECYCLE="$ROLLBACK_LAUNCHD_LIFECYCLE" \
  MAC_ROLLBACK_LIFECYCLE_SHA256="$ROLLBACK_LAUNCHD_LIFECYCLE_SHA256" \
    "$PY" - <<'PY'
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path


def private_script(path: Path) -> bytes:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o700
            or before.st_size <= 0
            or before.st_size > 2 * 1024 * 1024
        ):
            raise SystemExit("phase-2 rollback executable is unsafe")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise SystemExit("phase-2 rollback executable was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise SystemExit("phase-2 rollback executable grew while reading")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise SystemExit("phase-2 rollback executable changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


script = Path(os.environ["MAC_ROLLBACK_SCRIPT"])
output = Path(os.environ["MAC_ROLLBACK_INTENT"])
script_raw = private_script(script)
payload = {
    "schema": "mac.fleet_node_rollback_intent.v1",
    "status": "armed",
    "agent": os.environ["MAC_ROLLBACK_AGENT"],
    "fleet": os.environ["MAC_ROLLBACK_FLEET"],
    "os_kind": os.environ["MAC_ROLLBACK_OS"],
    "generation": os.environ["MAC_ROLLBACK_GENERATION"],
    "revision": os.environ["MAC_ROLLBACK_REVISION"],
    "prior_generation": os.environ["MAC_ROLLBACK_PRIOR_GENERATION"] or None,
    "prior_revision": os.environ["MAC_ROLLBACK_PRIOR_REVISION"] or None,
    "rollback_capable": True,
    "prior_topology": {
        "supervisor": os.environ["MAC_ROLLBACK_SUPERVISOR"],
        "active_gateway": os.environ["MAC_ROLLBACK_ACTIVE_GATEWAY"],
        "agent_prior_state": os.environ["MAC_ROLLBACK_AGENT_PRIOR_STATE"],
    },
    "prerequisites": {
        "schema": "mac.fleet_prerequisite_rollback_binding.v1",
        "node_identity_sha256": os.environ["MAC_ROLLBACK_NODE_IDENTITY_SHA256"],
        "bundle_sha256": os.environ["MAC_ROLLBACK_PREREQUISITE_BUNDLE_SHA256"],
        "expectations_sha256": os.environ[
            "MAC_ROLLBACK_PREREQUISITE_EXPECTATIONS_SHA256"
        ],
    },
    "artifacts": {
        "source": {"path": os.environ["MAC_ROLLBACK_SRC"], "backup": os.environ["MAC_ROLLBACK_SRC_BACKUP"]},
        "venv": {"path": os.environ["MAC_ROLLBACK_VENV"], "backup": os.environ["MAC_ROLLBACK_VENV_BACKUP"]},
        "hermes": {"path": os.environ["MAC_ROLLBACK_HERMES"], "backup": os.environ["MAC_ROLLBACK_HERMES_BACKUP"] or None},
        "bin_backup": os.environ["MAC_ROLLBACK_BIN_BACKUP"],
        "openclaw_backup": os.environ["MAC_ROLLBACK_OPENCLAW_BACKUP"] or None,
        "openclaw_existed": os.environ["MAC_ROLLBACK_OPENCLAW_EXISTED"] == "1",
    },
    "contracts": {
        "supervisor_helper": {"path": os.environ["MAC_ROLLBACK_SUPERVISOR_HELPER"], "sha256": os.environ["MAC_ROLLBACK_SUPERVISOR_HELPER_SHA256"]},
        "lifecycle_helper": {"path": os.environ["MAC_ROLLBACK_LIFECYCLE"], "sha256": os.environ["MAC_ROLLBACK_LIFECYCLE_SHA256"]},
    },
    "rollback": {
        "path": str(script),
        "sha256": hashlib.sha256(script_raw).hexdigest(),
        "completion_receipt": os.environ["MAC_ROLLBACK_COMPLETION"],
    },
    "armed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
}
descriptor, temporary_raw = tempfile.mkstemp(prefix="." + output.name + ".", dir=str(output.parent))
temporary = Path(temporary_raw)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, output)
    except FileExistsError as exc:
        raise SystemExit("phase-2 rollback intent appeared concurrently") from exc
    temporary.unlink()
    directory = os.open(str(output.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    temporary.unlink(missing_ok=True)
PY
  ROLLBACK_INTENT_SHA256="$(verify_phase2_rollback_intent)" \
    || die "published phase-2 rollback intent failed exact readback"
}

arm_phase2_rollback() {
  [ -d "$SRC_DIR" ] && [ ! -L "$SRC_DIR" ] \
    && [ -d "$VENV" ] && [ ! -L "$VENV" ] \
    || die "phase-2 apply requires a complete rollback-capable prior generation"
  SRC_BACKUP="$MAC_HOME/backups/mac-src.${AGENT}.${DEPLOY_TS}"
  VENV_BACKUP="$MAC_HOME/backups/venv.${AGENT}.${DEPLOY_TS}"
  HERMES_BACKUP=""
  [ ! -d "$HERMES_DIR" ] || HERMES_BACKUP="$MAC_HOME/backups/hermes-agent.${AGENT}.${DEPLOY_TS}"
  if [ -e "$ROLLBACK_INTENT" ] || [ -L "$ROLLBACK_INTENT" ]; then
    BIN_BACKUP="$MAC_HOME/backups/bin.${AGENT}.${DEPLOY_TS}"
    if [ -e "$MAC_HOME/openclaw" ] || [ -L "$MAC_HOME/openclaw" ]; then
      OPENCLAW_HOME_EXISTED=1
      OPENCLAW_HOME_BACKUP="$MAC_HOME/backups/openclaw.${AGENT}.${DEPLOY_TS}"
    fi
    ROLLBACK_INTENT_SHA256="$(verify_phase2_rollback_intent)" \
      || die "existing phase-2 rollback intent is invalid"
    DEPLOY_ROLLBACK_ARMED=1
    return 0
  fi
  local destination
  for destination in "$SRC_BACKUP" "$VENV_BACKUP" "$HERMES_BACKUP"; do
    [ -z "$destination" ] || [ ! -e "$destination" ] \
      || die "phase-2 rollback backup destination already exists: $destination"
  done
  capture_mutable_runtime_state_for_rollback
  capture_auxiliary_rollback_artifacts
  write_rollback_script
  write_phase2_rollback_intent
  DEPLOY_ROLLBACK_ARMED=1
  log "phase-2 rollback intent armed: $ROLLBACK_INTENT"
}

backup_existing_artifacts() {
  local moved_src=0 moved_venv=0 moved_hermes=0 backup_rc=0
  SRC_BACKUP=""
  VENV_BACKUP=""
  HERMES_BACKUP=""
  if [ -d "$SRC_DIR" ]; then
    SRC_BACKUP="$MAC_HOME/backups/mac-src.${AGENT}.${DEPLOY_TS}"
  fi
  if [ -d "$VENV" ]; then
    VENV_BACKUP="$MAC_HOME/backups/venv.${AGENT}.${DEPLOY_TS}"
  fi
  if [ -d "$HERMES_DIR" ]; then
    HERMES_BACKUP="$MAC_HOME/backups/hermes-agent.${AGENT}.${DEPLOY_TS}"
  fi
  local destination
  for destination in "$SRC_BACKUP" "$VENV_BACKUP" "$HERMES_BACKUP"; do
    [ -z "$destination" ] || [ ! -e "$destination" ] \
      || die "artifact backup destination already exists: $destination"
  done
  # These are same-filesystem renames under MAC_HOME.  Compensate in reverse
  # order if any member fails so the backup boundary itself is one generation
  # transition rather than three independent moves.
  if [ -n "$SRC_BACKUP" ]; then
    log "backing up existing mac source to $SRC_BACKUP"
    mv -f "$SRC_DIR" "$SRC_BACKUP" || backup_rc=$?
    [ "$backup_rc" -ne 0 ] || moved_src=1
  fi
  if [ "$backup_rc" -eq 0 ] && [ -n "$VENV_BACKUP" ]; then
    log "backing up existing mac venv to $VENV_BACKUP"
    mv -f "$VENV" "$VENV_BACKUP" || backup_rc=$?
    [ "$backup_rc" -ne 0 ] || moved_venv=1
  fi
  if [ "$backup_rc" -eq 0 ] && [ -n "$HERMES_BACKUP" ]; then
    log "backing up existing Hermes checkout to $HERMES_BACKUP"
    mv -f "$HERMES_DIR" "$HERMES_BACKUP" || backup_rc=$?
    [ "$backup_rc" -ne 0 ] || moved_hermes=1
  fi
  if [ "$backup_rc" -ne 0 ]; then
    [ "$moved_hermes" -ne 1 ] || mv -f "$HERMES_BACKUP" "$HERMES_DIR" || true
    [ "$moved_venv" -ne 1 ] || mv -f "$VENV_BACKUP" "$VENV" || true
    [ "$moved_src" -ne 1 ] || mv -f "$SRC_BACKUP" "$SRC_DIR" || true
    log "ERROR: artifact backup transaction failed and was compensated"
    return "$backup_rc"
  fi
  mac_launchd_fsync_directory "$MAC_HOME/backups" user
  mac_launchd_fsync_directory "$MAC_HOME" user
  write_rollback_script
}

capture_darwin_launchd_prestate() {
  [ "$SUPERVISOR_KIND" = "launchd" ] || return 0
  local uid system_plist system_supervisor_plist gui_plist probe_rc=0 phase1_states=""
  if ! sudo -n true; then
    log "ERROR: passwordless sudo is required to inspect system launchd state"
    return 1
  fi
  uid="$(id -u)"
  system_plist="/Library/LaunchDaemons/${MAC_LAUNCHD_LABEL}.plist"
  system_supervisor_plist="/Library/LaunchDaemons/${DARWIN_SYSTEM_SUPERVISOR_LABEL}.plist"
  gui_plist="$HOME/Library/LaunchAgents/${MAC_LAUNCHD_LABEL}.plist"

  probe_rc=0
  system_launchd_job_is_loaded "$MAC_LAUNCHD_LABEL" || probe_rc=$?
  case "$probe_rc" in
    0) DARWIN_SYSTEM_LAUNCHD_ACTIVE=1 ;;
    1) ;;
    *) return "$probe_rc" ;;
  esac
  probe_rc=0
  gui_launchd_job_is_loaded "$uid" "$MAC_LAUNCHD_LABEL" || probe_rc=$?
  case "$probe_rc" in
    0) DARWIN_GUI_LAUNCHD_ACTIVE=1 ;;
    1) ;;
    *) return "$probe_rc" ;;
  esac
  probe_rc=0
  system_launchd_job_is_loaded "$DARWIN_SYSTEM_SUPERVISOR_LABEL" || probe_rc=$?
  case "$probe_rc" in
    0) DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE=1 ;;
    1) ;;
    *) return "$probe_rc" ;;
  esac

  if [ "$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ] && ! sudo -n test -r "$system_plist"; then
    log "ERROR: loaded system control-plane service has no readable plist at $system_plist"
    return 1
  fi
  if [ "$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ] && [ ! -r "$gui_plist" ]; then
    log "ERROR: loaded GUI control-plane service has no readable plist at $gui_plist"
    return 1
  fi
  if [ "$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" = 1 ] \
    && ! sudo -n test -r "$system_supervisor_plist"; then
    log "ERROR: loaded system supervisor has no readable plist at $system_supervisor_plist"
    return 1
  fi
  if [ "$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" = 1 ] \
    && [ "$DARWIN_SYSTEM_LAUNCHD_ACTIVE" != 1 ]; then
    log "ERROR: system supervisor is active without its system control-plane target"
    return 1
  fi
  if [ "$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ] && [ "$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ]; then
    log "ERROR: control plane is loaded in both system and GUI launchd domains"
    return 1
  fi

  # Phase 1 deliberately stopped the worker and every gateway before this
  # installer began, so their live state is no longer their rollback state.
  # Recover that fact only from the generation-bound phase-1 receipt.  A
  # system-domain gateway/worker would be a topology this installer cannot
  # faithfully restore (the canonical definitions are LaunchAgents), so fail
  # closed instead of silently moving it to another domain.
  phase1_states="$("$PY" - \
    "$MAC_HOME/phase1-cohort-quiescence-${DEPLOY_GENERATION}.json" \
    "$AGENT" "$FLEET_NAME" "$DEPLOY_REV" "$DEPLOY_GENERATION" "$uid" \
    "$HERMES_LAUNCHD_LABEL" "$OPENCLAW_LAUNCHD_LABEL" \
    "$NEMOCLAW_LAUNCHD_LABEL" "$MAC_AGENT_LAUNCHD_LABEL" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

(
    raw_path,
    agent,
    fleet,
    revision,
    generation,
    raw_uid,
    *labels,
) = sys.argv[1:]
path = Path(raw_path)
metadata = path.lstat()
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != os.getuid()
    or metadata.st_mode & 0o077
    or metadata.st_size <= 0
    or metadata.st_size > 1024 * 1024
):
    raise SystemExit("phase-1 launchd prestate receipt is not owner-private and bounded")
with path.open("r", encoding="utf-8") as stream:
    payload = json.load(stream)
expected = {
    "schema": "mac.phase1_cohort_quiescence.v1",
    "agent": agent,
    "fleet": fleet,
    "revision": revision,
    "generation": generation,
}
if not isinstance(payload, dict) or any(payload.get(k) != v for k, v in expected.items()):
    raise SystemExit("phase-1 launchd prestate receipt belongs to another release")
supervisor = payload.get("supervisor")
if not isinstance(supervisor, dict) or supervisor.get("manager") != "launchd":
    raise SystemExit("phase-1 receipt does not contain launchd prestate")
resources = supervisor.get("resources")
if not isinstance(resources, list):
    raise SystemExit("phase-1 launchd prestate resources are malformed")
uid = int(raw_uid)
states = {}
for item in resources:
    if not isinstance(item, dict):
        raise SystemExit("phase-1 launchd prestate resource is malformed")
    label = item.get("name")
    target = item.get("target")
    state = item.get("state")
    prior = item.get("prior_state")
    if not all(isinstance(value, str) for value in (label, target, state, prior)):
        raise SystemExit("phase-1 launchd prestate contains an invalid transition")
    if label not in labels or target not in {
        "gui/%d/%s" % (uid, label),
        "system/%s" % label,
    }:
        raise SystemExit("phase-1 launchd prestate contains an unexpected identity")
    if state != "absent" or prior not in {"absent", "active"}:
        raise SystemExit("phase-1 launchd prestate contains an invalid transition")
    key = (label, target)
    if key in states:
        raise SystemExit("phase-1 launchd prestate contains a duplicate identity")
    states[key] = prior
expected_keys = {
    (label, target)
    for label in labels
    for target in ("gui/%d/%s" % (uid, label), "system/%s" % label)
}
if set(states) != expected_keys:
    raise SystemExit("phase-1 launchd prestate is incomplete")
for label in labels:
    if states[(label, "system/%s" % label)] == "active":
        raise SystemExit("phase-1 found a system-domain gateway or worker")
print(
    " ".join(
        "1" if states[(label, "gui/%d/%s" % (uid, label))] == "active" else "0"
        for label in labels
    )
)
PY
)" || return $?
  read -r \
    DARWIN_HERMES_LAUNCHD_ACTIVE \
    DARWIN_OPENCLAW_LAUNCHD_ACTIVE \
    DARWIN_NEMOCLAW_LAUNCHD_ACTIVE \
    DARWIN_AGENT_LAUNCHD_ACTIVE <<<"$phase1_states"
  case "${DARWIN_HERMES_LAUNCHD_ACTIVE}${DARWIN_OPENCLAW_LAUNCHD_ACTIVE}${DARWIN_NEMOCLAW_LAUNCHD_ACTIVE}${DARWIN_AGENT_LAUNCHD_ACTIVE}" in
    ""|*[!01]*|?????*|???|??|?)
      log "ERROR: phase-1 launchd prestate result is malformed"
      return 1
      ;;
  esac
  if [ $((
    DARWIN_HERMES_LAUNCHD_ACTIVE
    + DARWIN_OPENCLAW_LAUNCHD_ACTIVE
    + DARWIN_NEMOCLAW_LAUNCHD_ACTIVE
  )) -gt 1 ]; then
    log "ERROR: phase-1 found multiple active launchd gateway owners"
    return 1
  fi

  if sudo -n test -f "$system_plist"; then
    DARWIN_SYSTEM_PLIST_BACKUP="$MAC_HOME/backups/${MAC_LAUNCHD_LABEL}.${AGENT}.${DEPLOY_TS}.system.plist"
    log "backing up system control-plane service to $DARWIN_SYSTEM_PLIST_BACKUP"
    snapshot_rollback_file \
      "$system_plist" "$DARWIN_SYSTEM_PLIST_BACKUP" system
  fi
  if [ -f "$gui_plist" ]; then
    MAC_PLIST_BACKUP="$MAC_HOME/backups/${MAC_LAUNCHD_LABEL}.${AGENT}.${DEPLOY_TS}.plist"
    log "backing up GUI control-plane service to $MAC_PLIST_BACKUP"
    snapshot_rollback_file "$gui_plist" "$MAC_PLIST_BACKUP" user
  fi
  if sudo -n test -f "$system_supervisor_plist"; then
    DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP="$MAC_HOME/backups/${DARWIN_SYSTEM_SUPERVISOR_LABEL}.${AGENT}.${DEPLOY_TS}.system.plist"
    log "backing up system control-plane supervisor to $DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP"
    snapshot_rollback_file \
      "$system_supervisor_plist" \
      "$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP" system
  fi

  # This first rollback version restores the original service topology.  It is
  # rewritten with source/venv backup paths before artifact replacement.
  write_rollback_script
}

capture_phase1_prior_worker_topology() {
  local topology=""
  topology="$("$PY" - \
    "$MAC_HOME/phase1-cohort-quiescence-${DEPLOY_GENERATION}.json" \
    "$AGENT" "$FLEET_NAME" "$DEPLOY_REV" "$DEPLOY_GENERATION" \
    "$SUPERVISOR_KIND" "$(id -u)" \
    "$HERMES_SERVICE_NAME" "$OPENCLAW_SERVICE_NAME" \
    "$NEMOCLAW_SERVICE_NAME" "$MAC_AGENT_SERVICE_NAME" \
    "$HERMES_LAUNCHD_LABEL" "$OPENCLAW_LAUNCHD_LABEL" \
    "$NEMOCLAW_LAUNCHD_LABEL" "$MAC_AGENT_LAUNCHD_LABEL" \
    "$HERMES_SUPERVISORD_PROG" "$OPENCLAW_SUPERVISORD_PROG" \
    "$NEMOCLAW_SUPERVISORD_PROG" "$AGENT_SUPERVISORD_PROG" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

(
    raw_path,
    agent,
    fleet,
    revision,
    generation,
    manager,
    raw_uid,
    *identities,
) = sys.argv[1:]
path = Path(raw_path)
metadata = path.lstat()
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != os.getuid()
    or metadata.st_mode & 0o077
    or metadata.st_size <= 0
    or metadata.st_size > 1024 * 1024
):
    raise SystemExit("phase-1 prior-topology receipt is not owner-private and bounded")
with path.open("r", encoding="utf-8") as stream:
    payload = json.load(stream)
expected = {
    "schema": "mac.phase1_cohort_quiescence.v1",
    "agent": agent,
    "fleet": fleet,
    "revision": revision,
    "generation": generation,
}
if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in expected.items()):
    raise SystemExit("phase-1 prior-topology receipt belongs to another release")
supervisor = payload.get("supervisor")
if not isinstance(supervisor, dict) or supervisor.get("manager") != manager:
    raise SystemExit("phase-1 prior-topology manager diverged from the installer")

systemd_names = identities[0:4]
launchd_names = identities[4:8]
supervisord_names = identities[8:12]
active_prior = {
    "systemd": {"active"},
    "launchd": {"active"},
    "supervisord": {"RUNNING", "STARTING", "BACKOFF", "STOPPING"},
}
inactive_prior = {
    "systemd": {"inactive"},
    "launchd": set(),
    "supervisord": {"STOPPED", "EXITED", "FATAL", "UNKNOWN"},
}


def exact_resources(resources, expected_names, allowed_prior, allowed_final):
    if not isinstance(resources, list) or len(resources) != len(expected_names):
        raise SystemExit("phase-1 prior topology has the wrong resource count")
    result = {}
    for item in resources:
        if not isinstance(item, dict):
            raise SystemExit("phase-1 prior topology has an invalid resource")
        name = item.get("name")
        prior = item.get("prior_state")
        final = item.get("state")
        if (
            name not in expected_names
            or prior not in allowed_prior
            or final not in allowed_final
            or name in result
        ):
            raise SystemExit("phase-1 prior topology has an invalid transition")
        result[name] = prior
    if set(result) != set(expected_names):
        raise SystemExit("phase-1 prior topology is incomplete")
    return result


if manager == "systemd":
    names = systemd_names
    states = exact_resources(
        supervisor.get("resources"),
        names,
        {"active", "inactive", "absent"},
        {"inactive", "absent"},
    )
elif manager == "launchd":
    names = launchd_names
    uid = int(raw_uid)
    resources = supervisor.get("resources")
    if not isinstance(resources, list) or len(resources) != 2 * len(names):
        raise SystemExit("phase-1 launchd prior topology is incomplete")
    transitions = {}
    for item in resources:
        if not isinstance(item, dict):
            raise SystemExit("phase-1 launchd prior topology has an invalid resource")
        name = item.get("name")
        target = item.get("target")
        prior = item.get("prior_state")
        if (
            name not in names
            or target not in {"gui/%d/%s" % (uid, name), "system/%s" % name}
            or prior not in {"active", "absent"}
            or item.get("state") != "absent"
            or (name, target) in transitions
        ):
            raise SystemExit("phase-1 launchd prior topology has an invalid transition")
        transitions[(name, target)] = prior
    expected_targets = {
        (name, target)
        for name in names
        for target in ("gui/%d/%s" % (uid, name), "system/%s" % name)
    }
    if set(transitions) != expected_targets:
        raise SystemExit("phase-1 launchd prior topology is incomplete")
    for name in names:
        if transitions[(name, "system/%s" % name)] == "active":
            raise SystemExit("phase-1 found an unsupported system launchd worker topology")
    states = {
        name: transitions[(name, "gui/%d/%s" % (uid, name))]
        for name in names
    }
elif manager == "supervisord":
    names = supervisord_names
    managers = supervisor.get("managers")
    if not isinstance(managers, list) or not managers:
        raise SystemExit("phase-1 supervisord prior topology lacks a manager")
    system_managers = [item for item in managers if isinstance(item, dict) and item.get("scope") == "system"]
    if len(system_managers) != 1:
        raise SystemExit("phase-1 supervisord prior topology lacks one system manager")
    for item in managers:
        if not isinstance(item, dict) or item.get("scope") not in {"system", "user"}:
            raise SystemExit("phase-1 supervisord prior topology has an invalid scope")
        candidate = exact_resources(
            item.get("resources"),
            names,
            active_prior[manager] | inactive_prior[manager] | {"absent"},
            {"STOPPED", "EXITED", "FATAL", "absent"},
        )
        if item.get("scope") == "user" and any(
            value in active_prior[manager] for value in candidate.values()
        ):
            raise SystemExit("phase-1 found an active unsupported user supervisord topology")
    states = exact_resources(
        system_managers[0].get("resources"),
        names,
        active_prior[manager] | inactive_prior[manager] | {"absent"},
        {"STOPPED", "EXITED", "FATAL", "absent"},
    )
else:
    raise SystemExit("phase-1 prior topology names an unsupported manager")

gateway_names = dict(zip(("hermes", "openclaw", "nemoclaw"), names[:3]))
active_gateways = [
    owner
    for owner, name in gateway_names.items()
    if states[name] in active_prior[manager]
]
if len(active_gateways) > 1:
    raise SystemExit("phase-1 prior topology has multiple active gateways")
active_gateway = active_gateways[0] if active_gateways else "none"
if active_gateway == "nemoclaw":
    raise SystemExit(
        "phase-1 prior Nemo gateway lacks a durable runtime checkpoint"
    )
agent_prior = states[names[3]]
if agent_prior in active_prior[manager]:
    agent_state = "active"
elif agent_prior == "absent":
    agent_state = "absent"
elif agent_prior in inactive_prior[manager]:
    agent_state = "inactive"
else:
    raise SystemExit("phase-1 prior topology has an unknown agent state")
print(active_gateway, agent_state)
PY
)" || return $?
  read -r ROLLBACK_ACTIVE_GATEWAY ROLLBACK_AGENT_PRIOR_STATE <<<"$topology"
  case "$ROLLBACK_ACTIVE_GATEWAY" in
    hermes|openclaw|nemoclaw|none) ;;
    *) die "phase-1 prior gateway topology result is malformed" ;;
  esac
  case "$ROLLBACK_AGENT_PRIOR_STATE" in
    active|inactive|absent) ;;
    *) die "phase-1 prior worker topology result is malformed" ;;
  esac
  write_rollback_script
}

system_launchd_job_is_loaded() {
  local label="$1" state=""
  state="$(mac_launchd_job_state "system/$label" "$label" system)" || return $?
  [ "$state" = active ]
}

gui_launchd_job_is_loaded() {
  local uid="$1" label="$2" state=""
  state="$(mac_launchd_job_state "gui/$uid/$label" "$label" user)" || return $?
  [ "$state" = active ]
}

wait_for_system_launchd_job_unloaded() {
  local label="$1"
  mac_launchd_wait_unloaded "system/$label" "$label" system
}

wait_for_gui_launchd_job_unloaded() {
  local uid="$1" label="$2"
  mac_launchd_wait_unloaded "gui/$uid/$label" "$label" user
}

stop_system_launchd_job_if_present() {
  local label="$1"
  mac_launchd_stop_job_if_present "system/$label" "$label" system
}

stop_gui_launchd_job_if_present() {
  local uid="$1" label="$2"
  mac_launchd_stop_job_if_present "gui/$uid/$label" "$label" user
}

wait_for_local_control_plane_stop() {
  "$PY" - "$MAC_PORT" <<'PY'
import socket
import sys
import time

port = int(sys.argv[1])
deadline = time.monotonic() + 30.0
while True:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        listening = sock.connect_ex(("127.0.0.1", port)) == 0
    if not listening:
        raise SystemExit(0)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SystemExit("local control-plane port remained open after service stop")
    time.sleep(min(0.5, remaining))
PY
}

wait_for_local_control_plane_health() {
  "$PY" - "$MAC_PORT" <<'PY'
import http.client
import sys
import time

port = int(sys.argv[1])
deadline = time.monotonic() + 60.0
while True:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        response.read(1024)
        if 200 <= response.status < 300:
            raise SystemExit(0)
    except (OSError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SystemExit("local control plane did not become healthy")
    time.sleep(min(0.5, remaining))
PY
}

stop_systemd_service_if_present() {
  local unit="$1" load_state active_state
  if ! load_state="$(run_systemctl show "$unit" -p LoadState --value 2>/dev/null)"; then
    log "ERROR: could not inspect systemd unit $unit before artifact replacement"
    return 1
  fi
  case "$load_state" in
    not-found) return 0 ;;
    loaded|masked) ;;
    *)
      log "ERROR: unsupported systemd load state for $unit: ${load_state:-empty}"
      return 1
      ;;
  esac
  if ! run_systemctl stop "$unit" >/dev/null 2>&1; then
    log "ERROR: failed to stop systemd unit $unit"
    return 1
  fi
  if ! active_state="$(run_systemctl show "$unit" -p ActiveState --value 2>/dev/null)"; then
    log "ERROR: could not verify stopped systemd unit $unit"
    return 1
  fi
  case "$active_state" in
    inactive|failed) return 0 ;;
    *)
      log "ERROR: systemd unit $unit remained $active_state after stop"
      return 1
      ;;
  esac
}

disable_systemd_service_if_present() {
  local unit="$1" load_state enabled_state
  stop_systemd_service_if_present "$unit"
  load_state="$(run_systemctl show "$unit" -p LoadState --value 2>/dev/null)" \
    || return $?
  case "$load_state" in
    not-found) return 0 ;;
    loaded|masked) ;;
    *)
      log "ERROR: unsupported systemd load state while disabling $unit: $load_state"
      return 1
      ;;
  esac
  if [ "$load_state" = loaded ]; then
    run_systemctl disable "$unit" >/dev/null
  fi
  enabled_state="$(run_systemctl is-enabled "$unit" 2>/dev/null || true)"
  case "$enabled_state" in
    disabled|masked|static|indirect|not-found) return 0 ;;
    *)
      log "ERROR: systemd unit remained enabled after disable: $unit"
      return 1
      ;;
  esac
}

supervisord_program_state() {
  local program="$1" output="" rc=0 observed="" state="" details=""
  output="$(run_supervisorctl status "$program" 2>&1)" && rc=0 || rc=$?
  case "$output" in
    *$'\n'*)
      log "ERROR: supervisord returned multiple records for $program"
      return 1
      ;;
  esac
  if [ "$output" = "$program: ERROR (no such process)" ] && [ "$rc" -ne 0 ]; then
    printf '%s\n' absent
    return 0
  fi
  read -r observed state details <<<"$output"
  if [ "$observed" != "$program" ]; then
    log "ERROR: supervisord returned the wrong program identity for $program (status=$rc)"
    return 1
  fi
  case "$state" in
    RUNNING)
      if [ "$rc" -ne 0 ] \
          || ! [[ "$details" =~ (^|[[:space:]])pid[[:space:]]+([0-9]+)(,|[[:space:]]|$) ]] \
          || [ "${BASH_REMATCH[2]}" -le 0 ]; then
        log "ERROR: supervisord RUNNING state lacks an exact positive pid for $program"
        return 1
      fi
      printf '%s\n' running
      ;;
    STOPPED|EXITED|FATAL)
      case "$rc" in
        0|3) printf '%s\n' inactive ;;
        *)
          log "ERROR: supervisord contradicted inactive state for $program (status=$rc)"
          return 1
          ;;
      esac
      ;;
    STARTING|BACKOFF|STOPPING)
      printf '%s\n' transitional
      ;;
    *)
      log "ERROR: supervisord returned an unknown state for $program (status=$rc)"
      return 1
      ;;
  esac
}

stop_supervisord_program_if_present() {
  local program="$1" state=""
  state="$(supervisord_program_state "$program")" || return $?
  case "$state" in
    absent|inactive) return 0 ;;
    running|transitional)
      if ! run_supervisorctl stop "$program" >/dev/null 2>&1; then
        log "ERROR: failed to stop supervisord program $program"
        return 1
      fi
      ;;
    *) return 1 ;;
  esac
  state="$(supervisord_program_state "$program")" || return $?
  case "$state" in
    absent|inactive) return 0 ;;
    *)
      log "ERROR: supervisord program $program did not become inactive"
      return 1
      ;;
  esac
}

start_supervisord_program() {
  local program="$1" state=""
  state="$(supervisord_program_state "$program")" || return $?
  case "$state" in
    absent)
      log "ERROR: cannot start absent supervisord program: $program"
      return 1
      ;;
    running|transitional)
      stop_supervisord_program_if_present "$program" || return $?
      ;;
    inactive) ;;
    *) return 1 ;;
  esac
  run_supervisorctl start "$program" >/dev/null
  state="$(supervisord_program_state "$program")" || return $?
  if [ "$state" != running ]; then
    log "ERROR: supervisord program did not reach RUNNING: $program"
    return 1
  fi
}

stop_existing_services_for_deploy() {
  local control_mode=inactive
  local args=()
  log "proving exact supervisor quiescence before artifact replacement"
  if control_plane_enabled; then
    control_mode=active
  fi
  case "$SUPERVISOR_KIND" in
    systemd)
      args=(
        --control-plane "$MAC_SERVICE_NAME"
        --hermes-gateway "$HERMES_SERVICE_NAME"
        --openclaw-gateway "$OPENCLAW_SERVICE_NAME"
        --nemoclaw-gateway "$NEMOCLAW_SERVICE_NAME"
        --agent "$MAC_AGENT_SERVICE_NAME"
      )
      ;;
    supervisord)
      args=(
        --control-plane "$MAC_SUPERVISORD_PROG"
        --hermes-gateway "$HERMES_SUPERVISORD_PROG"
        --openclaw-gateway "$OPENCLAW_SUPERVISORD_PROG"
        --nemoclaw-gateway "$NEMOCLAW_SUPERVISORD_PROG"
        --agent "$AGENT_SUPERVISORD_PROG"
        --supervisord-scope system
      )
      ;;
    launchd)
      if [ "$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ]; then
        control_mode=system
      elif [ "$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ]; then
        control_mode=gui
      else
        control_mode=inactive
      fi
      args=(
        --control-plane "$MAC_LAUNCHD_LABEL"
        --hermes-gateway "$HERMES_LAUNCHD_LABEL"
        --openclaw-gateway "$OPENCLAW_LAUNCHD_LABEL"
        --nemoclaw-gateway "$NEMOCLAW_LAUNCHD_LABEL"
        --agent "$MAC_AGENT_LAUNCHD_LABEL"
        --launchd-uid "$(id -u)"
        --launchd-system-supervisor "$DARWIN_SYSTEM_SUPERVISOR_LABEL"
      )
      [ "$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" != 1 ] \
        || args+=(--launchd-system-supervisor-was-active)
      ;;
    *) die "unsupported supervisor before artifact replacement: $SUPERVISOR_KIND" ;;
  esac
  "$PY" "$ROLLBACK_SUPERVISOR_HELPER" quiesce \
    --supervisor "$SUPERVISOR_KIND" \
    --control-plane-mode "$control_mode" \
    --control-plane-port "$MAC_PORT" \
    --receipt "$LOG_DIR/pre-artifact-supervisor-quiescence.json" \
    "${args[@]}"
}

load_drain_api_env() {
  DRAIN_API_URL="${MAC_HUB_URL:-${HUB_URL:-http://127.0.0.1:$MAC_PORT}}"
  DRAIN_API_TOKEN="${MAC_WORKER_TOKEN:-${MAC_API_TOKEN:-}}"
  if [ -f "$ENV_FILE" ]; then
    set -a
    set +u
    # shellcheck source=/dev/null
    . "$ENV_FILE"
    set -u
    set +a
    DRAIN_API_URL="${MAC_HUB_URL:-${HUB_URL:-$DRAIN_API_URL}}"
    DRAIN_API_TOKEN="${MAC_WORKER_TOKEN:-${MAC_API_TOKEN:-$DRAIN_API_TOKEN}}"
  fi
  DRAIN_API_URL="${DRAIN_API_URL%/}"
}

# BEGIN MAC DAEMON RESOURCE QUIESCENCE
# Supervisor state is not a complete quiescence proof.  OpenShell sandboxes
# and legacy compose containers are daemon-owned resources: they can survive
# the launchd/systemd/supervisord process which originally created them.  This
# gate inventories those resources through every reachable owner API, removes
# only identities whose ownership is proven, and writes a generation-bound
# certificate only after two stable absence observations.
daemon_resource_quiescence_gate() {
  local mode="$1" phase="${2:-pre_source}" runtime_path runtime_paths="" runtime_paths_configured=0 runtime_paths_declaration=""
  local env_name env_value
  local -a gate_env=()
  runtime_paths_declaration="$(declare -p CONTAINER_RUNTIME_PATHS 2>/dev/null || true)"
  case "$runtime_paths_declaration" in
  "declare -a "*)
    runtime_paths_configured=1
    for runtime_path in "${CONTAINER_RUNTIME_PATHS[@]}"; do
      [ -n "$runtime_path" ] || continue
      runtime_paths="${runtime_paths}${runtime_path}
"
    done
    ;;
  esac
  # The deploy process carries repository, hub, model-provider, and messaging
  # credentials.  This proof only needs local daemon identity and timing
  # inputs, so do not make the Python gate (or anything it launches) a new
  # credential-bearing process.  A fixed PATH also prevents the stop wrapper
  # from resolving deployment-controlled helper names through the caller's
  # ambient PATH.  Isolated/no-site Python prevents PYTHONPATH, user-site, and
  # sitecustomize startup hooks from executing inside this trust boundary.
  gate_env=(
    "HOME=${HOME:?HOME is required}"
    "PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/Docker.app/Contents/Resources/bin"
    "LC_ALL=C"
    "LANG=C"
    "OS_KIND=${OS_KIND:-}"
    "MAC_DEPLOY_DAEMON_RUNTIME_PATHS=$runtime_paths"
    "MAC_DEPLOY_DAEMON_RUNTIME_PATHS_CONFIGURED=$runtime_paths_configured"
  )
  for env_name in \
    USER LOGNAME TMPDIR SHELL \
    XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_RUNTIME_DIR \
    DBUS_SESSION_BUS_ADDRESS \
    DOCKER_CONFIG DOCKER_HOST DOCKER_CONTEXT \
    CONTAINERS_CONF CONTAINERS_STORAGE_CONF CONTAINERS_REGISTRIES_CONF \
    CONTAINER_HOST CONTAINER_CONNECTION PODMAN_HOST PODMAN_CONNECTION \
    MAC_DEPLOY_DAEMON_COMMAND_TIMEOUT_SECONDS \
    MAC_DEPLOY_DAEMON_QUIESCENCE_TIMEOUT_SECONDS \
    MAC_DEPLOY_DAEMON_QUIESCENCE_POLL_SECONDS \
    MAC_DEPLOY_DAEMON_TOTAL_TIMEOUT_SECONDS \
    MAC_DEPLOY_DAEMON_INJECT_RECEIPT_POST_REPLACE_FAILURE \
    MAC_OPENCLAW_SUBPROCESS_TIMEOUT_SECONDS \
    MAC_OPENCLAW_SANDBOX_DELETE_TIMEOUT_SECONDS; do
    env_value="${!env_name-}"
    [ -z "$env_value" ] || gate_env+=("$env_name=$env_value")
  done
  # Behavioral fakes need their state handles, but this namespace is admitted
  # only under the explicit test switch and cannot carry production deploy
  # credentials into a child process accidentally.
  if [ "${MAC_DEPLOY_DAEMON_TEST_MODE:-0}" = 1 ]; then
    gate_env+=("MAC_DEPLOY_DAEMON_TEST_MODE=1")
    for env_name in \
      FAKE_DAEMON_CALLS FAKE_OPENSHELL_STATE FAKE_DOCKER_STATE \
      FAKE_PODMAN_STATE FAKE_CHILD_PID FAKE_OPENSHELL_MODE \
      FAKE_STOP_WRAPPER_MODE FAKE_DOCKER_MODE FAKE_PODMAN_MODE \
      FAKE_SANDBOX_NAME FAKE_SECRET FAKE_GATE_CAPTURE; do
      env_value="${!env_name-}"
      [ -z "$env_value" ] || gate_env+=("$env_name=$env_value")
    done
  fi
  /usr/bin/env -i "${gate_env[@]}" \
    "$PY" -I -S - "$mode" "$phase" "$MAC_HOME" "$DEPLOY_GENERATION" "$DEPLOY_REV" <<'PY'
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit


class QuiescenceFailure(Exception):
    pass


class CommandResult:
    def __init__(self, returncode, stdout, timed_out=False):
        self.returncode = returncode
        self.stdout = stdout
        self.timed_out = timed_out


mode, proof_phase, mac_home_raw, generation, revision = sys.argv[1:6]
mac_home = Path(mac_home_raw).resolve()
managed = mac_home / "openclaw" / "managed"
openshell = mac_home / "bin" / "openshell"
stop_wrapper = mac_home / "bin" / "openclaw-gateway-stop"
endpoint = "http://127.0.0.1:17670"
max_output_bytes = 4 * 1024 * 1024


def bounded_number(name, default, minimum, maximum):
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        raise QuiescenceFailure("invalid %s" % name)
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise QuiescenceFailure("out-of-range %s" % name)
    return value


def remaining_time():
    remaining = gate_deadline - time.monotonic()
    if remaining <= 0:
        raise QuiescenceFailure("daemon-resource quiescence deadline expired")
    return remaining


def sleep_for_poll():
    if poll_seconds:
        time.sleep(min(poll_seconds, remaining_time()))
    else:
        remaining_time()


def run_bounded(argv, env=None):
    if not argv or not os.path.isabs(str(argv[0])):
        raise QuiescenceFailure("daemon command is not an absolute executable")
    effective_timeout = min(command_timeout, remaining_time())
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                [str(item) for item in argv],
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                env=env,
                start_new_session=True,
            )
        except OSError:
            raise QuiescenceFailure("daemon command could not be started")
        timed_out = False
        try:
            process.wait(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                term_grace = min(
                    2.0, max(0.0, gate_deadline - time.monotonic())
                )
                if term_grace:
                    process.wait(timeout=term_grace)
            except subprocess.TimeoutExpired:
                pass
            # The session leader can exit on TERM while a grandchild ignores
            # it.  Always kill the process group after the grace period; a
            # leader-only wait is not a process-tree quiescence proof.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                reap_budget = min(
                    0.25, max(0.0, gate_deadline - time.monotonic())
                )
                if reap_budget:
                    try:
                        process.wait(timeout=reap_budget)
                    except subprocess.TimeoutExpired:
                        pass
        stdout_size = stdout_file.tell()
        stderr_size = stderr_file.tell()
        if stdout_size > max_output_bytes or stderr_size > max_output_bytes:
            raise QuiescenceFailure("daemon command output exceeded its bound")
        stdout_file.seek(0)
        raw = stdout_file.read(max_output_bytes + 1)
    return CommandResult(
        process.returncode,
        raw.decode("utf-8", errors="strict"),
        timed_out=timed_out,
    )


def require_private_regular(path, executable=False):
    try:
        metadata = path.lstat()
    except OSError:
        raise QuiescenceFailure("managed identity artifact is unreadable")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise QuiescenceFailure("managed identity artifact is not a regular file")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise QuiescenceFailure("managed identity artifact is not owner-private")
    if metadata.st_size > 1024 * 1024:
        raise QuiescenceFailure("managed identity artifact exceeds its size bound")
    if executable and not os.access(str(path), os.X_OK):
        raise QuiescenceFailure("managed stop executable is not executable")
    return metadata


def resolve_owned_executable(path):
    try:
        parent = path.parent.stat()
        target = path.resolve(strict=True)
        metadata = target.stat()
    except OSError:
        raise QuiescenceFailure("managed executable is unreadable")
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
        raise QuiescenceFailure("managed executable directory has an unexpected owner")
    if parent.st_mode & 0o022:
        raise QuiescenceFailure("managed executable directory is group/world writable")
    if not stat.S_ISREG(metadata.st_mode):
        raise QuiescenceFailure("managed executable target is not a regular file")
    if metadata.st_uid not in {0, os.getuid()} or metadata.st_mode & 0o022:
        raise QuiescenceFailure("managed executable target is not trusted")
    if not os.access(str(target), os.X_OK):
        raise QuiescenceFailure("managed executable target is not executable")
    return target


sandbox_pattern = re.compile(r"mac-openclaw-[a-z0-9][a-z0-9._-]{0,127}\Z")


def validate_sandbox_name(value):
    if not sandbox_pattern.fullmatch(value):
        raise QuiescenceFailure("managed OpenClaw sandbox identity is invalid")
    return value


def read_private_text(path):
    require_private_regular(path)
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        raise QuiescenceFailure("managed identity artifact is undecodable")


def resolve_sandbox_name():
    identity_path = managed / "sandbox-name"
    runtime_path = managed / "runtime.env"
    if identity_path.exists() or identity_path.is_symlink():
        text = read_private_text(identity_path)
        if "\n" in text.rstrip("\n") or text.count("\n") > 1:
            raise QuiescenceFailure("managed sandbox identity is not one line")
        value = text.rstrip("\n")
        if value != value.strip():
            raise QuiescenceFailure("managed sandbox identity has surrounding whitespace")
        return validate_sandbox_name(value)

    if runtime_path.exists() or runtime_path.is_symlink():
        text = read_private_text(runtime_path)
        matches = []
        for line in text.splitlines():
            match = re.match(
                r"^[ \t]*(?:export[ \t]+)?MAC_OPENCLAW_SANDBOX[ \t]*=(.*)$",
                line,
            )
            if not match:
                continue
            try:
                values = shlex.split(match.group(1), comments=True, posix=True)
            except ValueError:
                raise QuiescenceFailure("managed sandbox identity assignment is invalid")
            if len(values) != 1:
                raise QuiescenceFailure("managed sandbox identity assignment is ambiguous")
            matches.append(values[0])
        if len(matches) != 1:
            raise QuiescenceFailure("managed runtime lacks one sandbox identity")
        return validate_sandbox_name(matches[0])

    try:
        has_artifacts = managed.is_dir() and any(managed.iterdir())
    except OSError:
        raise QuiescenceFailure("managed OpenClaw artifact directory is unreadable")
    if has_artifacts:
        raise QuiescenceFailure("managed OpenClaw artifacts lack a sandbox identity")
    return None


common_child_environment = (
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "SHELL",
    "PATH",
    "LC_ALL",
    "LANG",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)
test_child_environment = (
    "FAKE_DAEMON_CALLS",
    "FAKE_OPENSHELL_STATE",
    "FAKE_DOCKER_STATE",
    "FAKE_PODMAN_STATE",
    "FAKE_CHILD_PID",
    "FAKE_OPENSHELL_MODE",
    "FAKE_STOP_WRAPPER_MODE",
    "FAKE_DOCKER_MODE",
    "FAKE_PODMAN_MODE",
    "FAKE_SANDBOX_NAME",
    "FAKE_SECRET",
    "FAKE_GATE_CAPTURE",
)


def allowlisted_child_environment(extra=()):
    keys = common_child_environment + tuple(extra)
    if os.environ.get("MAC_DEPLOY_DAEMON_TEST_MODE") == "1":
        keys += test_child_environment
    return {key: os.environ[key] for key in keys if key in os.environ}


def openshell_env():
    value = allowlisted_child_environment(
        (
            "MAC_OPENCLAW_SUBPROCESS_TIMEOUT_SECONDS",
            "MAC_OPENCLAW_SANDBOX_DELETE_TIMEOUT_SECONDS",
        )
    )
    value["OPENSHELL_GATEWAY_ENDPOINT"] = endpoint
    return value


def sandbox_inventory(expected):
    openshell_target = resolve_owned_executable(openshell)
    offset = 0
    names = set()
    found = False
    while True:
        result = run_bounded(
            [
                str(openshell_target),
                "sandbox",
                "list",
                "--limit",
                "1000",
                "--offset",
                str(offset),
                "--output",
                "json",
            ],
            env=openshell_env(),
        )
        if result.timed_out:
            raise QuiescenceFailure("OpenShell sandbox inventory timed out")
        if result.returncode != 0:
            raise QuiescenceFailure("OpenShell sandbox inventory failed")
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError):
            raise QuiescenceFailure("OpenShell sandbox inventory is malformed")
        if not isinstance(payload, list):
            raise QuiescenceFailure("OpenShell sandbox inventory is not a list")
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise QuiescenceFailure("OpenShell sandbox inventory has an invalid entry")
            name = item["name"]
            if not name or "\x00" in name or "\n" in name:
                raise QuiescenceFailure("OpenShell sandbox inventory has an invalid name")
            if name in names:
                raise QuiescenceFailure(
                    "OpenShell sandbox inventory contains duplicate identities"
                )
            names.add(name)
            found = found or name == expected
        if len(payload) < 1000:
            return found
        offset += len(payload)


def quiesce_openclaw_sandbox():
    name = resolve_sandbox_name()
    outcome = {
        "sandbox": name,
        "initial_state": "not_managed" if name is None else "unknown",
        "final_state": "absent",
        "stop_wrapper_invoked": False,
        "delete_invoked": False,
    }
    if name is None:
        return outcome
    present = sandbox_inventory(name)
    outcome["initial_state"] = "present" if present else "absent"
    if not present:
        deadline = min(gate_deadline, time.monotonic() + quiescence_timeout)
        if stable_sandbox_absence(name, deadline, return_on_presence=True):
            return outcome

    require_private_regular(stop_wrapper, executable=True)
    outcome["stop_wrapper_invoked"] = True
    stopped = run_bounded([str(stop_wrapper)], env=openshell_env())
    if stopped.timed_out:
        raise QuiescenceFailure("managed OpenClaw stop wrapper timed out")
    if stopped.returncode != 0:
        raise QuiescenceFailure("managed OpenClaw stop wrapper failed")
    deadline = min(gate_deadline, time.monotonic() + quiescence_timeout)
    if stable_sandbox_absence(name, deadline, return_on_presence=True):
        return outcome

    outcome["delete_invoked"] = True
    openshell_target = resolve_owned_executable(openshell)
    deleted = run_bounded(
        [str(openshell_target), "sandbox", "delete", name], env=openshell_env()
    )
    deadline = min(gate_deadline, time.monotonic() + quiescence_timeout)
    if stable_sandbox_absence(name, deadline, return_on_presence=False):
        return outcome
    if deleted.timed_out:
        raise QuiescenceFailure("OpenShell sandbox deletion timed out")
    if deleted.returncode != 0:
        raise QuiescenceFailure("OpenShell sandbox deletion failed")
    raise QuiescenceFailure("OpenShell sandbox remained present after deletion")


def stable_sandbox_absence(expected, deadline, return_on_presence):
    empty_proofs = 0
    while empty_proofs < 2:
        if sandbox_inventory(expected):
            if return_on_presence:
                return False
            empty_proofs = 0
        else:
            empty_proofs += 1
        if empty_proofs >= 2:
            return True
        if time.monotonic() >= deadline:
            if not return_on_presence:
                return False
            raise QuiescenceFailure("OpenShell sandbox did not remain absent")
        sleep_for_poll()
    return True


runtime_candidates = {
    "docker": [
        "/usr/bin/docker",
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ],
    "podman": [
        "/usr/bin/podman",
        "/usr/local/bin/podman",
        "/opt/homebrew/bin/podman",
    ],
}


docker_config_snapshots = []


def sanitized_docker_config():
    # Docker context inventory normally receives DOCKER_CONFIG (or HOME/.docker),
    # whose config.json can contain registry auth and credential-helper policy.
    # Copy only bounded, regular context metadata into an ephemeral config.  TLS
    # material is deliberately excluded; a context which cannot be described
    # without it is unknown state and the caller will fail closed.
    temporary = tempfile.TemporaryDirectory(prefix="mac-daemon-docker-config-")
    docker_config_snapshots.append(temporary)

    def identity(value):
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    destination = Path(temporary.name)
    configured = os.environ.get("DOCKER_CONFIG", "").strip()
    source = Path(configured) if configured else Path(os.environ["HOME"]) / ".docker"
    metadata_root = source / "contexts" / "meta"
    try:
        root_metadata = metadata_root.lstat()
    except FileNotFoundError:
        return destination
    except OSError:
        raise QuiescenceFailure("Docker context metadata is unreadable")
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise QuiescenceFailure("Docker context metadata root is not a regular directory")

    file_count = 0
    total_bytes = 0
    destination_root = destination / "contexts" / "meta"
    destination_root.mkdir(parents=True, mode=0o700)
    for raw_root, directories, files in os.walk(
        metadata_root, topdown=True, followlinks=False
    ):
        root = Path(raw_root)
        try:
            current_root = root.lstat()
        except OSError:
            raise QuiescenceFailure("Docker context metadata changed during snapshot")
        if stat.S_ISLNK(current_root.st_mode) or not stat.S_ISDIR(current_root.st_mode):
            raise QuiescenceFailure("Docker context metadata contains a non-directory")
        for name in directories:
            try:
                item = (root / name).lstat()
            except OSError:
                raise QuiescenceFailure("Docker context metadata changed during snapshot")
            if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
                raise QuiescenceFailure("Docker context metadata contains a linked directory")
        relative_root = root.relative_to(metadata_root)
        output_root = destination_root / relative_root
        output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in files:
            path = root / name
            try:
                initial = path.lstat()
            except OSError:
                raise QuiescenceFailure("Docker context metadata changed during snapshot")
            if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
                raise QuiescenceFailure("Docker context metadata contains a non-regular file")
            file_count += 1
            total_bytes += initial.st_size
            if (
                initial.st_size <= 0
                or initial.st_size > 1024 * 1024
                or file_count > 2048
                or total_bytes > 16 * 1024 * 1024
            ):
                raise QuiescenceFailure("Docker context metadata exceeds its bound")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(str(path), flags)
            except OSError:
                raise QuiescenceFailure("Docker context metadata could not be opened safely")
            try:
                opened = os.fstat(descriptor)
                chunks = []
                remaining = opened.st_size
                while remaining:
                    chunk = os.read(descriptor, min(remaining, 64 * 1024))
                    if not chunk:
                        raise QuiescenceFailure("Docker context metadata was truncated")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(descriptor, 1):
                    raise QuiescenceFailure("Docker context metadata grew during snapshot")
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or identity(initial) != identity(opened)
                or identity(opened) != identity(after)
            ):
                raise QuiescenceFailure("Docker context metadata changed during snapshot")
            output = output_root / name
            output_descriptor = os.open(
                str(output), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(output_descriptor, "wb") as stream:
                for chunk in chunks:
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
    return destination


def clean_container_environment():
    ambient = {
        key: os.environ.get(key, "").strip()
        for key in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "CONTAINER_HOST",
            "CONTAINER_CONNECTION",
            "PODMAN_HOST",
            "PODMAN_CONNECTION",
        )
    }
    value = allowlisted_child_environment(
        (
            "CONTAINERS_CONF",
            "CONTAINERS_STORAGE_CONF",
            "CONTAINERS_REGISTRIES_CONF",
        )
    )
    value["DOCKER_CONFIG"] = str(sanitized_docker_config())
    for key in ("DOCKER_HOST", "CONTAINER_HOST", "PODMAN_HOST"):
        raw = ambient[key]
        if raw and canonical_unix_endpoint(raw) is None:
            raise QuiescenceFailure(
                "ambient remote container endpoint is not allowed"
            )
    return value, ambient


def canonical_unix_endpoint(raw):
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme != "unix" or parsed.netloc not in {"", "localhost"}:
        return None
    path = unquote(parsed.path)
    if not path or not os.path.isabs(path) or "\x00" in path:
        return None
    return "unix://" + os.path.realpath(path)


def canonical_loopback_tcp_endpoint(raw):
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "tcp"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return None
    host = parsed.hostname.lower()
    if host == "::1":
        host = "[::1]"
    return "tcp://%s:%d" % (host, port)


def is_loopback_ssh_endpoint(raw):
    try:
        parsed = urlsplit(raw)
        parsed.port
    except ValueError:
        return False
    return parsed.scheme == "ssh" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def parse_json_lines(raw, description):
    values = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            raise QuiescenceFailure("%s is malformed" % description)
        if not isinstance(value, dict):
            raise QuiescenceFailure("%s has an invalid entry" % description)
        values.append(value)
    return values


def runtime_result(runtime, *args):
    return run_bounded(
        [str(runtime["path"])] + runtime["selector"] + list(args),
        env=runtime["env"],
    )


def runtime_identity(runtime):
    return {
        "kind": runtime["kind"],
        "path": str(runtime["path"]),
        "endpoint": runtime["endpoint"],
        "selector": list(runtime["selector"]),
    }


def docker_endpoints(path, clean_env, ambient):
    listed = run_bounded(
        [str(path), "context", "ls", "--format", "{{json .}}"], env=clean_env
    )
    if listed.timed_out:
        raise QuiescenceFailure("Docker context inventory timed out")
    if listed.returncode != 0:
        raise QuiescenceFailure("Docker context inventory failed")
    contexts = parse_json_lines(listed.stdout, "Docker context inventory")
    if not contexts:
        raise QuiescenceFailure("Docker context inventory is empty")
    endpoints = []
    local_context_names = set()
    for summary in contexts:
        name = summary.get("Name") or summary.get("name")
        if not isinstance(name, str) or not name or "\n" in name:
            raise QuiescenceFailure("Docker context has an invalid name")
        inspected = run_bounded(
            [str(path), "context", "inspect", name], env=clean_env
        )
        if inspected.timed_out:
            raise QuiescenceFailure("Docker context inspection timed out")
        if inspected.returncode != 0:
            raise QuiescenceFailure("Docker context inspection failed")
        try:
            payload = json.loads(inspected.stdout)
        except (TypeError, ValueError):
            raise QuiescenceFailure("Docker context inspection is malformed")
        if not isinstance(payload, list) or len(payload) != 1:
            raise QuiescenceFailure("Docker context inspection is ambiguous")
        context = payload[0]
        if not isinstance(context, dict):
            raise QuiescenceFailure("Docker context inspection has an invalid entry")
        context_endpoints = context.get("Endpoints")
        if not isinstance(context_endpoints, dict):
            raise QuiescenceFailure("Docker context endpoints are malformed")
        docker = context_endpoints.get("docker")
        host = docker.get("Host") if isinstance(docker, dict) else None
        if not isinstance(host, str) or not host:
            raise QuiescenceFailure("Docker context lacks an endpoint")
        endpoint_identity = canonical_unix_endpoint(host)
        if endpoint_identity is None and canonical_loopback_tcp_endpoint(host) is not None:
            raise QuiescenceFailure(
                "Docker loopback TCP context lacks node-local ownership proof"
            )
        if endpoint_identity is None and is_loopback_ssh_endpoint(host):
            raise QuiescenceFailure(
                "Docker loopback SSH context lacks node-local ownership proof"
            )
        if endpoint_identity is None:
            if ambient["DOCKER_CONTEXT"] == name:
                raise QuiescenceFailure("ambient Docker context is remote")
            continue
        local_context_names.add(name)
        endpoints.append(
            {
                "kind": "docker",
                "path": path,
                "endpoint": endpoint_identity,
                "selector": ["--context", name],
                "env": clean_env,
            }
        )
    selected_context = ambient["DOCKER_CONTEXT"]
    if selected_context and selected_context not in local_context_names:
        raise QuiescenceFailure("ambient Docker context is not a local endpoint")
    ambient_host = ambient["DOCKER_HOST"]
    if ambient_host:
        endpoints.append(
            {
                "kind": "docker",
                "path": path,
                "endpoint": canonical_unix_endpoint(ambient_host),
                "selector": ["--host", ambient_host],
                "env": clean_env,
            }
        )
    return endpoints


def podman_endpoints(path, clean_env, ambient):
    listed = run_bounded(
        [str(path), "system", "connection", "list", "--format", "json"],
        env=clean_env,
    )
    if listed.timed_out:
        raise QuiescenceFailure("Podman connection inventory timed out")
    if listed.returncode != 0:
        raise QuiescenceFailure("Podman connection inventory failed")
    try:
        connections = json.loads(listed.stdout)
    except (TypeError, ValueError):
        raise QuiescenceFailure("Podman connection inventory is malformed")
    if not isinstance(connections, list):
        raise QuiescenceFailure("Podman connection inventory is not a list")
    endpoints = []
    local_names = set()
    loopback_ssh = []
    for connection in connections:
        if not isinstance(connection, dict):
            raise QuiescenceFailure("Podman connection inventory has an invalid entry")
        name = connection.get("Name") or connection.get("name")
        uri = connection.get("URI") or connection.get("Uri") or connection.get("uri")
        if not isinstance(name, str) or not name or not isinstance(uri, str):
            raise QuiescenceFailure("Podman connection has an invalid identity")
        endpoint_identity = canonical_unix_endpoint(uri)
        if endpoint_identity is None and canonical_loopback_tcp_endpoint(uri) is not None:
            raise QuiescenceFailure(
                "Podman loopback TCP connection lacks node-local ownership proof"
            )
        if endpoint_identity is None:
            try:
                parsed = urlsplit(uri)
                parsed_port = parsed.port
            except ValueError:
                parsed = None
                parsed_port = None
            if (
                parsed is not None
                and parsed.scheme == "ssh"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed_port is not None
            ):
                is_machine = connection.get("IsMachine")
                if is_machine is None:
                    is_machine = connection.get("isMachine")
                if is_machine is not None and not isinstance(is_machine, bool):
                    raise QuiescenceFailure(
                        "Podman loopback SSH connection has malformed machine ownership"
                    )
                if is_machine is False:
                    raise QuiescenceFailure(
                        "Podman loopback SSH connection lacks local-machine ownership"
                    )
                loopback_ssh.append(
                    (name, uri, parsed_port, parsed.path, is_machine)
                )
                continue
            if name in {ambient["CONTAINER_CONNECTION"], ambient["PODMAN_CONNECTION"]}:
                raise QuiescenceFailure("ambient Podman connection is remote")
            continue
        local_names.add(name)
        endpoints.append(
            {
                "kind": "podman",
                "path": path,
                "endpoint": endpoint_identity,
                "selector": ["--connection", name],
                "env": clean_env,
            }
        )
    if loopback_ssh:
        for name, uri, port, socket_path, is_machine in loopback_ssh:
            machine_name = name[:-5] if name.endswith("-root") else name
            if is_machine is None:
                inspected = run_bounded(
                    [str(path), "machine", "inspect", machine_name], env=clean_env
                )
                if inspected.timed_out:
                    raise QuiescenceFailure("Podman machine inspection timed out")
                if inspected.returncode != 0:
                    raise QuiescenceFailure(
                        "Podman loopback connection lacks machine proof"
                    )
                try:
                    machine_payload = json.loads(inspected.stdout)
                except (TypeError, ValueError):
                    raise QuiescenceFailure("Podman machine inspection is malformed")
                if not isinstance(machine_payload, list) or len(machine_payload) != 1:
                    raise QuiescenceFailure("Podman machine inspection is ambiguous")
                machine = machine_payload[0]
                if not isinstance(machine, dict):
                    raise QuiescenceFailure(
                        "Podman machine inspection has an invalid entry"
                    )
                ssh_config = machine.get("SSHConfig")
                inspected_name = machine.get("Name") or machine.get("name")
                inspected_port = (
                    ssh_config.get("Port") if isinstance(ssh_config, dict) else None
                )
                if inspected_port is None:
                    inspected_port = machine.get("Port")
                try:
                    inspected_port = int(inspected_port)
                except (TypeError, ValueError):
                    raise QuiescenceFailure("Podman machine has an invalid SSH port")
                if inspected_name != machine_name or inspected_port != port:
                    raise QuiescenceFailure(
                        "Podman loopback connection does not match its local machine"
                    )
            local_names.add(name)
            endpoints.append(
                {
                    "kind": "podman",
                    "path": path,
                    "endpoint": "podman-machine://%s@127.0.0.1:%d%s"
                    % (machine_name, port, socket_path),
                    "selector": ["--connection", name],
                    "env": clean_env,
                }
            )
    for key in ("CONTAINER_CONNECTION", "PODMAN_CONNECTION"):
        if ambient[key] and ambient[key] not in local_names:
            raise QuiescenceFailure("ambient Podman connection is not local")
    ambient_host = ambient["CONTAINER_HOST"] or ambient["PODMAN_HOST"]
    if ambient_host:
        endpoints.append(
            {
                "kind": "podman",
                "path": path,
                "endpoint": canonical_unix_endpoint(ambient_host),
                "selector": ["--url", ambient_host],
                "env": clean_env,
            }
        )
    host_os = str(os.environ.get("OS_KIND") or sys.platform).lower()
    if host_os in {"linux", "linux2"} or not connections:
        endpoints.append(
            {
                "kind": "podman",
                "path": path,
                "endpoint": "podman-local://uid-%d" % os.getuid(),
                "selector": [],
                "env": clean_env,
            }
        )
    return endpoints


def discover_working_runtimes():
    candidates = []
    configured = [
        value.strip()
        for value in os.environ.get("MAC_DEPLOY_DAEMON_RUNTIME_PATHS", "").splitlines()
        if value.strip()
    ]
    sources = []
    for raw in configured:
        kind = Path(raw).name.lower()
        if "podman" in kind:
            kind = "podman"
        elif "docker" in kind:
            kind = "docker"
        else:
            raise QuiescenceFailure("configured container runtime kind is unknown")
        sources.append((kind, [raw]))
    configured_only = (
        os.environ.get("MAC_DEPLOY_DAEMON_RUNTIME_PATHS_CONFIGURED") == "1"
    )
    if not configured_only:
        for kind, known_paths in runtime_candidates.items():
            from_path = shutil.which(kind)
            sources.append((kind, ([from_path] if from_path else []) + known_paths))
    for kind, paths in sources:
        for raw in paths:
            path = Path(raw)
            try:
                if not path.is_file() or not os.access(str(path), os.X_OK):
                    continue
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            resolved_kind = kind
            if resolved_kind == "docker" and "podman" in resolved.name.lower():
                # podman-docker commonly installs /usr/bin/docker as a symlink
                # to the Podman CLI.  Classify the resolved executable once so
                # we never send Docker context commands to Podman or certify
                # the same native store through two nominal frontends.
                resolved_kind = "podman"
            identity = (resolved_kind, str(resolved))
            if identity not in candidates:
                candidates.append(identity)
    clean_env, ambient = clean_container_environment()
    discovered = []
    for kind, raw in candidates:
        path = Path(raw)
        if kind == "docker":
            discovered.extend(docker_endpoints(path, clean_env, ambient))
        else:
            discovered.extend(podman_endpoints(path, clean_env, ambient))
    working = []
    endpoint_identities = set()
    for runtime in discovered:
        identity = (runtime["kind"], runtime["endpoint"])
        if identity in endpoint_identities:
            continue
        endpoint_identities.add(identity)
        result = runtime_result(runtime, "info")
        if result.timed_out:
            raise QuiescenceFailure("container runtime availability probe timed out")
        if result.returncode == 0:
            working.append(runtime)
        else:
            # A stopped or inaccessible daemon can still own restart-managed
            # containers which reappear later.  Installed-but-uninspectable is
            # unknown state, never an absence proof.
            raise QuiescenceFailure("container runtime is unreadable")
    return working


safe_container_id = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def compose_path_corroborates(labels):
    config_files = labels.get("com.docker.compose.project.config_files")
    if isinstance(config_files, str):
        for value in config_files.split(","):
            normalized = value.strip().replace("\\", "/")
            if normalized.endswith("/deploy/nemoclaw/docker-compose.yaml"):
                return True
    working_dir = labels.get("com.docker.compose.project.working_dir")
    if isinstance(working_dir, str):
        normalized = working_dir.rstrip("/\\").replace("\\", "/")
        if (normalized + "/docker-compose.yaml").endswith(
            "/deploy/nemoclaw/docker-compose.yaml"
        ):
            return True
    return False


def legacy_tuple_corroborates(container):
    config = container.get("Config")
    if not isinstance(config, dict):
        return False
    image = config.get("Image")
    command = config.get("Cmd")
    name = container.get("Name")
    if not isinstance(name, str):
        return False
    normalized_name = name.lstrip("/")
    return (
        image == "localhost/mac-hermes:net"
        and command == ["python3", "-m", "mac.hermes_gateway"]
        and re.fullmatch(
            r"nemoclaw[-_]nemoclaw-gateway[-_]1", normalized_name
        )
        is not None
    )


def inspect_runtime(runtime):
    listed = runtime_result(
        runtime,
        "ps",
        "-a",
        "--filter",
        "label=com.docker.compose.service=nemoclaw-gateway",
        "--format",
        "{{.ID}}",
    )
    if listed.timed_out:
        raise QuiescenceFailure("container runtime inventory timed out")
    if listed.returncode != 0:
        raise QuiescenceFailure("container runtime inventory failed")
    identifiers = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if len(identifiers) != len(set(identifiers)):
        raise QuiescenceFailure("container runtime returned duplicate identities")
    owned = []
    ambiguous = []
    for identifier in identifiers:
        if not safe_container_id.fullmatch(identifier):
            raise QuiescenceFailure("container runtime returned an invalid identity")
        inspected = runtime_result(runtime, "inspect", identifier)
        if inspected.timed_out:
            raise QuiescenceFailure("container inspection timed out")
        if inspected.returncode != 0:
            raise QuiescenceFailure("container inspection failed")
        try:
            payload = json.loads(inspected.stdout)
        except (TypeError, ValueError):
            raise QuiescenceFailure("container inspection is malformed")
        if not isinstance(payload, list) or len(payload) != 1:
            raise QuiescenceFailure("container inspection is ambiguous")
        container = payload[0]
        if not isinstance(container, dict):
            raise QuiescenceFailure("container inspection has an invalid entry")
        if "Config" not in container or not isinstance(container["Config"], dict):
            raise QuiescenceFailure("container configuration is malformed")
        config = container["Config"]
        if "Labels" not in config:
            raise QuiescenceFailure("container labels are missing")
        labels_raw = config["Labels"]
        if labels_raw is None:
            labels = {}
        elif isinstance(labels_raw, dict):
            labels = labels_raw
        else:
            raise QuiescenceFailure("container labels are malformed")
        if labels.get("com.docker.compose.service") != "nemoclaw-gateway":
            continue
        corroborated = (
            labels.get("com.docker.compose.project") == "nemoclaw"
            or compose_path_corroborates(labels)
            or legacy_tuple_corroborates(container)
        )
        if corroborated:
            state = container.get("State")
            if not isinstance(state, dict) or not isinstance(
                state.get("Running"), bool
            ):
                raise QuiescenceFailure("legacy Nemo container state is malformed")
            owned.append((identifier, state["Running"]))
        else:
            ambiguous.append(identifier)
    return owned, ambiguous


def inventory_legacy_nemoclaw(runtimes):
    result = []
    for runtime in runtimes:
        owned, ambiguous = inspect_runtime(runtime)
        if ambiguous:
            raise QuiescenceFailure(
                "ambiguous legacy Nemo container ownership; refusing deletion"
            )
        result.append((runtime, owned))
    return result


def prove_legacy_nemoclaw_inactive(runtimes):
    retained = []
    stable_proofs = 0
    deadline = min(gate_deadline, time.monotonic() + quiescence_timeout)
    previous = None
    while stable_proofs < 2:
        inventory = inventory_legacy_nemoclaw(runtimes)
        running = [
            identifier
            for _runtime, owned in inventory
            for identifier, is_running in owned
            if is_running
        ]
        if running:
            raise QuiescenceFailure(
                "active legacy Nemo container cannot be restored without a durable runtime checkpoint"
            )
        current = sorted(
            [
                {
                    "runtime": runtime["kind"],
                    "endpoint": runtime["endpoint"],
                    "container_id": identifier,
                }
                for runtime, owned in inventory
                for identifier, _is_running in owned
            ],
            key=lambda item: (
                item["runtime"], item["endpoint"], item["container_id"]
            ),
        )
        if current == previous:
            stable_proofs += 1
        else:
            previous = current
            retained = current
            stable_proofs = 1
        if stable_proofs >= 2:
            return retained
        if time.monotonic() >= deadline:
            raise QuiescenceFailure("legacy Nemo inactive inventory did not stabilize")
        sleep_for_poll()
    return retained


def assert_legacy_nemoclaw_inactive(runtimes):
    previous = None
    for proof in range(2):
        inventory = inventory_legacy_nemoclaw(runtimes)
        current = sorted(
            (runtime["kind"], runtime["endpoint"], identifier)
            for runtime, owned in inventory
            for identifier, is_running in owned
            if not is_running
        )
        if any(
            is_running
            for _runtime, owned in inventory
            for _identifier, is_running in owned
        ):
            raise QuiescenceFailure("legacy Nemo container became active")
        if previous is not None and current != previous:
            raise QuiescenceFailure("legacy Nemo inactive inventory changed")
        previous = current
        if proof == 0:
            sleep_for_poll()


def recorded_at():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def runtime_identities(runtimes):
    return sorted(
        [runtime_identity(runtime) for runtime in runtimes],
        key=lambda item: (item["kind"], item["endpoint"], item["path"]),
    )


def atomic_write_certificate(path, payload):
    remaining_time()
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent)
    )
    temporary = Path(temporary_raw)
    directory = None
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(str(path.parent), os.O_RDONLY)
        os.replace(str(temporary), str(path))
        published = True
        if os.environ.get(
            "MAC_DEPLOY_DAEMON_INJECT_RECEIPT_POST_REPLACE_FAILURE"
        ) == "1":
            raise OSError("injected receipt durability failure")
        os.fsync(directory)
    except Exception:
        if published:
            try:
                path.unlink()
                os.fsync(directory)
            except FileNotFoundError:
                pass
        raise
    finally:
        if directory is not None:
            os.close(directory)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_pre_source_certificate(path, sandbox, runtimes, retained):
    identities = runtime_identities(runtimes)
    timestamp = recorded_at()
    payload = {
        "schema": "mac.daemon_resource_quiescence.v1",
        "generation": generation,
        "revision": revision,
        "recorded_at": timestamp,
        "openclaw": sandbox,
        "container_runtimes": identities,
        "legacy_nemoclaw": {
            "retained_stopped": retained,
            "final_state": "inactive",
        },
        "proofs": {
            "pre_source": {
                "recorded_at": timestamp,
                "container_runtimes": identities,
                "stable_inactive_observations": 2,
            }
        },
    }
    atomic_write_certificate(path, payload)


def load_certificate(path):
    text = read_private_text(path)
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        raise QuiescenceFailure("daemon quiescence receipt is malformed")
    if not isinstance(payload, dict):
        raise QuiescenceFailure("daemon quiescence receipt is not an object")
    if payload.get("schema") != "mac.daemon_resource_quiescence.v1":
        raise QuiescenceFailure("daemon quiescence receipt has the wrong schema")
    if payload.get("generation") != generation or payload.get("revision") != revision:
        raise QuiescenceFailure("daemon quiescence receipt belongs to another generation")
    if not isinstance(payload.get("container_runtimes"), list):
        raise QuiescenceFailure("daemon quiescence receipt lacks runtime identities")
    openclaw = payload.get("openclaw")
    legacy = payload.get("legacy_nemoclaw")
    proofs = payload.get("proofs")
    if not isinstance(openclaw, dict) or openclaw.get("final_state") != "absent":
        raise QuiescenceFailure("daemon quiescence receipt lacks OpenClaw absence")
    if not isinstance(legacy, dict) or legacy.get("final_state") != "inactive":
        raise QuiescenceFailure("daemon quiescence receipt lacks Nemo inactivity")
    if not isinstance(proofs, dict) or not isinstance(proofs.get("pre_source"), dict):
        raise QuiescenceFailure("daemon quiescence receipt lacks its pre-source proof")
    return payload


def write_daemon_restore_contract(path, sandbox, runtimes, retained):
    payload = {
        "schema": "mac.daemon_resource_restore_contract.v1",
        "generation": generation,
        "revision": revision,
        "recorded_at": recorded_at(),
        "openclaw": sandbox,
        "container_runtimes": runtime_identities(runtimes),
        "legacy_nemoclaw": {
            "retained_stopped": retained,
            "prior_state": "inactive",
        },
    }
    atomic_write_certificate(path, payload)


def load_daemon_restore_contract(path):
    text = read_private_text(path)
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        raise QuiescenceFailure("daemon restore contract is malformed")
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "mac.daemon_resource_restore_contract.v1"
        or payload.get("generation") != generation
        or payload.get("revision") != revision
        or not isinstance(payload.get("openclaw"), dict)
        or not isinstance(payload.get("container_runtimes"), list)
    ):
        raise QuiescenceFailure("daemon restore contract belongs to another generation")
    return payload, text.encode("utf-8")


def prove_sandbox_present(name):
    stable = 0
    deadline = min(gate_deadline, time.monotonic() + quiescence_timeout)
    while stable < 2:
        if sandbox_inventory(name):
            stable += 1
        else:
            stable = 0
        if stable >= 2:
            return
        if time.monotonic() >= deadline:
            raise QuiescenceFailure("OpenClaw sandbox was not restored stably")
        sleep_for_poll()


def clear_phase_proof(path, payload, phase):
    proofs = dict(payload.get("proofs") or {})
    changed = proofs.pop(phase, None) is not None
    payload["proofs"] = proofs
    if phase == "post_install" and "post_install" in payload:
        payload.pop("post_install", None)
        changed = True
    if changed:
        atomic_write_certificate(path, payload)


def record_phase_proof(path, payload, phase, runtimes):
    proof = {
        "recorded_at": recorded_at(),
        "container_runtimes": runtime_identities(runtimes),
        "stable_inactive_observations": 2,
    }
    proofs = dict(payload.get("proofs") or {})
    proofs[phase] = proof
    payload["proofs"] = proofs
    if phase == "post_install":
        payload["post_install"] = proof
    atomic_write_certificate(path, payload)


try:
    if mode not in {"quiesce", "assert-nemoclaw", "prepare-restore", "restore-check"}:
        raise QuiescenceFailure("invalid daemon quiescence mode")
    allowed_phases = {
        "pre_source",
        "pre_install",
        "pre_verify",
        "pre_finalize",
        "post_install",
        "phase1_prepare",
        "phase1_restore",
    }
    if proof_phase not in allowed_phases:
        raise QuiescenceFailure("invalid daemon quiescence proof phase")
    if mode == "quiesce" and proof_phase != "pre_source":
        raise QuiescenceFailure("quiescence must publish the pre-source phase")
    if mode == "assert-nemoclaw" and proof_phase == "pre_source":
        raise QuiescenceFailure("assertion phase cannot replace the pre-source proof")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,180}", generation):
        raise QuiescenceFailure("deployment generation is not path-safe")
    if not mac_home.is_dir():
        raise QuiescenceFailure("MAC home is unavailable")
    certificate = mac_home / (
        "daemon-resource-quiescence-%s.json" % generation
    )
    restore_contract = mac_home / (
        "daemon-resource-restore-contract-%s.json" % generation
    )
    restore_receipt = mac_home / (
        "daemon-resource-restore-%s.json" % generation
    )
    if mode == "quiesce":
        try:
            certificate.unlink()
        except FileNotFoundError:
            pass
    command_timeout = bounded_number(
        "MAC_DEPLOY_DAEMON_COMMAND_TIMEOUT_SECONDS", 20, 0.01, 300
    )
    quiescence_timeout = bounded_number(
        "MAC_DEPLOY_DAEMON_QUIESCENCE_TIMEOUT_SECONDS", 45, 0.01, 600
    )
    poll_seconds = bounded_number(
        "MAC_DEPLOY_DAEMON_QUIESCENCE_POLL_SECONDS", 1, 0.1, 30
    )
    total_timeout = bounded_number(
        "MAC_DEPLOY_DAEMON_TOTAL_TIMEOUT_SECONDS", 120, 0.01, 900
    )
    gate_deadline = time.monotonic() + total_timeout
    if mode == "prepare-restore":
        runtimes = discover_working_runtimes()
        retained = prove_legacy_nemoclaw_inactive(runtimes)
        name = resolve_sandbox_name()
        if name is None:
            sandbox = {"sandbox": None, "prior_state": "not_managed"}
        else:
            sandbox = {
                "sandbox": name,
                "prior_state": "present" if sandbox_inventory(name) else "absent",
            }
        write_daemon_restore_contract(restore_contract, sandbox, runtimes, retained)
    elif mode == "restore-check":
        contract, contract_raw = load_daemon_restore_contract(restore_contract)
        runtimes = discover_working_runtimes()
        if runtime_identities(runtimes) != contract["container_runtimes"]:
            raise QuiescenceFailure("container runtime identities changed before restore")
        assert_legacy_nemoclaw_inactive(runtimes)
        expected = contract["openclaw"]
        name = expected.get("sandbox")
        prior_state = expected.get("prior_state")
        current_name = resolve_sandbox_name()
        if prior_state == "not_managed":
            if current_name is not None:
                raise QuiescenceFailure("unexpected OpenClaw managed identity appeared")
        elif prior_state == "absent":
            if current_name != name or not stable_sandbox_absence(
                name, min(gate_deadline, time.monotonic() + quiescence_timeout), False
            ):
                raise QuiescenceFailure("OpenClaw sandbox absence was not restored")
        elif prior_state == "present":
            if current_name != name:
                raise QuiescenceFailure("OpenClaw sandbox identity changed during restore")
            prove_sandbox_present(name)
        else:
            raise QuiescenceFailure("daemon restore contract has an invalid sandbox state")
        atomic_write_certificate(
            restore_receipt,
            {
                "schema": "mac.daemon_resource_restore.v1",
                "status": "restored",
                "generation": generation,
                "revision": revision,
                "recorded_at": recorded_at(),
                "source_contract_sha256": __import__("hashlib").sha256(contract_raw).hexdigest(),
                "openclaw": expected,
                "container_runtimes": contract["container_runtimes"],
                "legacy_nemoclaw": {"final_state": "inactive"},
            },
        )
    elif mode == "quiesce":
        runtimes = discover_working_runtimes()
        retained = prove_legacy_nemoclaw_inactive(runtimes)
        sandbox = quiesce_openclaw_sandbox()
        write_pre_source_certificate(certificate, sandbox, runtimes, retained)
    else:
        receipt = load_certificate(certificate)
        clear_phase_proof(certificate, receipt, proof_phase)
        runtimes = discover_working_runtimes()
        if runtime_identities(runtimes) != receipt["container_runtimes"]:
            raise QuiescenceFailure(
                "container runtime identities changed after the pre-source proof"
            )
        assert_legacy_nemoclaw_inactive(runtimes)
        record_phase_proof(certificate, receipt, proof_phase, runtimes)
except QuiescenceFailure as exc:
    print("daemon-resource quiescence failed: %s" % exc, file=sys.stderr)
    raise SystemExit(1)
except Exception as exc:
    print(
        "daemon-resource quiescence failed unexpectedly: %s"
        % type(exc).__name__,
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
}

quiesce_daemon_resources_before_source_replacement() {
  log "proving daemon-owned resources quiescent before artifact replacement"
  daemon_resource_quiescence_gate quiesce pre_source
}

prepare_daemon_resources_for_phase1_restore() {
  log "capturing daemon-owned resources for phase-1 restore"
  daemon_resource_quiescence_gate prepare-restore phase1_prepare
}

verify_daemon_resources_after_phase1_restore() {
  log "proving daemon-owned resources restored after phase-1 abort"
  daemon_resource_quiescence_gate restore-check phase1_restore
}

assert_legacy_nemoclaw_containers_inactive() {
  local phase="${1:?daemon quiescence assertion phase is required}"
  log "re-proving legacy Nemo containers inactive for $phase"
  daemon_resource_quiescence_gate assert-nemoclaw "$phase"
}
# END MAC DAEMON RESOURCE QUIESCENCE

mac_api_json() {
  local method="$1" path="$2" body="${3:-}"
  [ -n "${DRAIN_API_TOKEN:-}" ] || return 1
  "$PY" - "$method" "$DRAIN_API_URL$path" "$DRAIN_API_TOKEN" "$body" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

method, url, token, body = sys.argv[1:5]
data = body.encode("utf-8") if body else None
request = urllib.request.Request(url, data=data, method=method)
request.add_header("Authorization", "Bearer " + token)
if data is not None:
    request.add_header("Content-Type", "application/json")
try:
    timeout = float(os.environ.get("MAC_DEPLOY_API_TIMEOUT_SECONDS") or "30")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        sys.stdout.write(response.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    sys.stderr.write(exc.read().decode("utf-8", errors="replace"))
    raise SystemExit(1)
PY
}

agent_id_for_drain() {
  local response_file status
  response_file="$(mktemp "${TMPDIR:-/tmp}/mac-deploy-agents.XXXXXX")"
  if ! mac_api_json GET "/agents" > "$response_file"; then
    rm -f "$response_file"
    return 1
  fi
  if "$PY" - "$AGENT" "$response_file" <<'PY'
import json
import sys

expected = sys.argv[1]
with open(sys.argv[2], encoding="utf-8") as handle:
    agents = json.load(handle)
for agent in agents:
    if agent.get("name") == expected or agent.get("id") == expected:
        print(agent.get("id"))
        raise SystemExit(0)
raise SystemExit(1)
PY
  then
    status=0
  else
    status=$?
  fi
  rm -f "$response_file"
  return "$status"
}

wait_for_agent_active_leases() {
  local agent_id="$1" summary_path="$LOG_DIR/mac-agent-drain.json"
  "$PY" - \
    "$DRAIN_API_URL/tasks" "$DRAIN_API_TOKEN" "$agent_id" "$summary_path" \
    "${DRAIN_TIMEOUT_SECONDS:-1800}" "${DRAIN_POLL_SECONDS:-10}" \
    "${MAC_DEPLOY_API_TIMEOUT_SECONDS:-30}" <<'PY'
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

url, token, agent_id, raw_summary, raw_total, raw_poll, raw_request = sys.argv[1:]
summary_path = Path(raw_summary)
try:
    total = float(raw_total)
    poll = float(raw_poll)
    request_cap = float(raw_request)
except ValueError:
    raise SystemExit("invalid drain timeout configuration")
if (
    not all(math.isfinite(value) for value in (total, poll, request_cap))
    or total < 0
    or poll <= 0
    or request_cap <= 0
):
    raise SystemExit("invalid drain timeout configuration")
fail_fast = total == 0
deadline = time.monotonic() + total


def publish(active):
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "agent_id": agent_id,
        "active_lease_count": len(active),
        "active_tasks": [
            {
                "id": task.get("id"),
                "state": task.get("state"),
                "lease_id": task.get("lease_id"),
                "leased_until": task.get("leased_until"),
                "title": task.get("title"),
            }
            for task in active
        ],
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix="." + summary_path.name + ".", dir=str(summary_path.parent))
    temporary = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(summary, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, summary_path)
        directory = os.open(summary_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


while True:
    remaining = max(0.0, deadline - time.monotonic())
    if not fail_fast and remaining <= 0:
        print("ERROR: drain timed out with active leases for %s" % agent_id, file=sys.stderr)
        raise SystemExit(1)
    timeout = request_cap if fail_fast else min(request_cap, remaining)
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            tasks = json.load(response)
        if not isinstance(tasks, list):
            raise ValueError("tasks response is not a list")
        active = [
            task
            for task in tasks
            if isinstance(task, dict)
            and task.get("owner_agent_id") == agent_id
            and task.get("lease_id")
            and task.get("state") in {"claimed", "running"}
        ]
        publish(active)
        if not active:
            print("mac-agent drain complete: no active leases for %s" % agent_id)
            raise SystemExit(0)
        print(
            "mac-agent drain waiting: %d active lease(s) for %s"
            % (len(active), agent_id),
            flush=True,
        )
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        print("WARNING: could not query active leases during drain", flush=True)
    if fail_fast or time.monotonic() >= deadline:
        print("ERROR: drain timed out with active leases for %s" % agent_id, file=sys.stderr)
        raise SystemExit(1)
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(poll, remaining))
PY
}

drain_mac_agent_before_deploy() {
  case "${DRAIN_MODE:-wait}" in
    skip|off|disabled)
      log "skipping mac-agent drain because MAC_DEPLOY_DRAIN_MODE=$DRAIN_MODE"
      return 0
      ;;
    wait|fail-fast)
      ;;
    *)
      log "ERROR: unsupported MAC_DEPLOY_DRAIN_MODE=$DRAIN_MODE"
      return 1
      ;;
  esac
  load_drain_api_env
  if ! mac_api_json GET "/health" >/dev/null 2>&1; then
    log "existing mac API is not reachable; skipping drain"
    return 0
  fi
  local agent_id
  if ! agent_id="$(agent_id_for_drain)" || [ -z "$agent_id" ]; then
    log "existing mac-agent registration for $AGENT not found; skipping drain"
    return 0
  fi
  log "pausing new claims for $agent_id before artifact replacement"
  mac_api_json POST "/agents/$agent_id/heartbeat" '{"status":"draining","health_status":"degraded"}' >/dev/null
  if [ "${DRAIN_MODE:-wait}" = "fail-fast" ]; then
    DRAIN_TIMEOUT_SECONDS=0 wait_for_agent_active_leases "$agent_id"
  else
    wait_for_agent_active_leases "$agent_id"
  fi
}

clear_mac_agent_drain_after_deploy() {
  load_drain_api_env
  if ! mac_api_json GET "/health" >/dev/null 2>&1; then
    log "WARNING: mac API is not reachable after deploy; cannot clear drain state"
    return 0
  fi
  local agent_id
  if ! agent_id="$(agent_id_for_drain)" || [ -z "$agent_id" ]; then
    return 0
  fi
  log "clearing drain state for $agent_id"
  mac_api_json POST "/agents/$agent_id/heartbeat" '{"status":"idle","health_status":"healthy"}' >/dev/null || true
}


install_github_cli() {
  local existing=""
  existing="$(command -v gh 2>/dev/null || true)"
  [ -n "$existing" ] && [ -x "$existing" ] \
    || die "GitHub CLI is missing; complete node onboarding before phase 2"
  "$existing" --version >/dev/null 2>&1 \
    || die "onboarded GitHub CLI is not executable"
  log "verified onboarded GitHub CLI at $existing"
}

install_codegraph_cli() {
  local target="$MAC_HOME/bin/codegraph"
  [ -x "$target" ] && [ ! -L "$target" ] \
    || die "reviewed CodeGraph bundle is missing; complete node onboarding before phase 2"
  run_without_deploy_credentials "$target" --version 2>/dev/null \
    | grep -qx "${MAC_REVIEWED_CODEGRAPH_VERSION#v}" \
    || die "onboarded CodeGraph version differs from $MAC_REVIEWED_CODEGRAPH_VERSION"
  log "verified onboarded CodeGraph $MAC_REVIEWED_CODEGRAPH_VERSION"
}

ensure_codegraph_git_exclude() {
  local repo_dir="$1" exclude_file=""
  if ! git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return 0
  fi
  exclude_file="$(git -C "$repo_dir" rev-parse --git-path info/exclude 2>/dev/null || true)"
  [ -n "$exclude_file" ] || return 0
  case "$exclude_file" in
    /*) ;;
    *) exclude_file="$repo_dir/$exclude_file" ;;
  esac
  mkdir -p "$(dirname "$exclude_file")"
  touch "$exclude_file"
  if ! grep -qxF ".codegraph/" "$exclude_file"; then
    printf '\n.codegraph/\n' >> "$exclude_file"
  fi
}

initialize_codegraph_repository() {
  local repo_dir="$1" log_file status_file pid_file
  [ -d "$repo_dir" ] || return 0
  if ! PATH="$MAC_HOME/bin:$PATH" command -v codegraph >/dev/null 2>&1; then
    log "CodeGraph CLI unavailable; skipping CodeGraph init for $repo_dir"
    return 0
  fi
  if ! git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "CodeGraph init skipped; $repo_dir is not a git worktree"
    return 0
  fi
  ensure_codegraph_git_exclude "$repo_dir"
  log_file="$LOG_DIR/codegraph-init-source.txt"
  status_file="$LOG_DIR/codegraph-init-source.json"
  pid_file="$LOG_DIR/codegraph-init-source.pid"
  log "queuing asynchronous CodeGraph index initialization for $repo_dir"
  CODEGRAPH_STATUS_FILE="$status_file" nohup "$PY" - "$repo_dir" "$MAC_HOME/bin:$PATH" \
    "${MAC_DEPLOY_CODEGRAPH_INIT_TIMEOUT_SECONDS:-300}" > "$log_file" 2>&1 <<'PY' &
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

repo_dir, path, raw_timeout = sys.argv[1:4]
timeout = max(1, int(raw_timeout))
status_path = Path(os.environ["CODEGRAPH_STATUS_FILE"])

def write_status(state, **extra):
    payload = {
        "schema": "mac.codegraph_background_init.v1",
        "state": state,
        "repository": repo_dir,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **extra,
    }
    status_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + status_path.name + ".", dir=str(status_path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, status_path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

env = dict(os.environ)
env["PATH"] = path
write_status("running", timeout_seconds=timeout)
process = subprocess.Popen(
    ["codegraph", "init"],
    cwd=repo_dir,
    env=env,
    start_new_session=True,
)
try:
    returncode = process.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    print("codegraph init timed out after %d seconds" % timeout, file=sys.stderr)
    write_status("timed_out", timeout_seconds=timeout, returncode=124)
    raise SystemExit(124)
if returncode == 0:
    write_status("completed", timeout_seconds=timeout, returncode=0)
else:
    write_status("failed", timeout_seconds=timeout, returncode=returncode)
raise SystemExit(returncode)
PY
  local init_pid=$!
  printf '%s\n' "$init_pid" > "$pid_file"
  log "CodeGraph index initialization queued for $repo_dir (pid=$init_pid status=$status_file)"
}



normalize_hermes_redaction_env() {
  "$PY" - "$LOG_DIR/hermes-redaction-normalization.json" "$HOME/.hermes/config.yaml" "$HOME/.hermes/.env" <<'PY'
import json
import re
import sys
import time
from pathlib import Path

report_path = Path(sys.argv[1])
config_path = Path(sys.argv[2])
targets = [Path(item) for item in sys.argv[3:]]
report = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "policy": "Hermes secret redaction must not be false in env or config",
    "config": {"path": str(config_path), "exists": config_path.exists(), "changed": False, "had_false": False},
    "files": [],
}
if config_path.exists() and config_path.is_file():
    try:
        config_lines = config_path.read_text(encoding="utf-8").splitlines()
        output = []
        changed = False
        for line in config_lines:
            if re.match(r"^(\s*redact_secrets\s*:\s*)(false|no|off|0)\s*$", line, flags=re.IGNORECASE):
                prefix = re.match(r"^(\s*redact_secrets\s*:\s*)", line, flags=re.IGNORECASE).group(1)
                output.append(prefix + "true")
                changed = True
                report["config"]["had_false"] = True
            else:
                output.append(line)
        if changed:
            backup = config_path.with_name(config_path.name + ".mac-redaction-backup-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
            backup.write_text("\n".join(config_lines) + "\n", encoding="utf-8")
            backup.chmod(0o600)
            config_path.write_text("\n".join(output) + "\n", encoding="utf-8")
            report["config"]["changed"] = True
            report["config"]["backup"] = str(backup)
    except OSError as exc:
        report["config"]["error"] = str(exc)
for path in targets:
    entry = {"path": str(path), "exists": path.exists(), "changed": False, "had_false": False}
    if not path.exists() or not path.is_file():
        report["files"].append(entry)
        continue
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        entry["error"] = str(exc)
        report["files"].append(entry)
        continue
    changed = False
    output = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("HERMES_REDACT_SECRETS="):
            value = stripped.split("=", 1)[1].strip().strip("\"'").lower()
            if value in {"0", "false", "no", "off"}:
                entry["had_false"] = True
                output.append("HERMES_REDACT_SECRETS=true")
                changed = True
                continue
        output.append(line)
    if changed:
        backup = path.with_name(path.name + ".mac-redaction-backup-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        backup.write_text("\n".join(lines) + "\n", encoding="utf-8")
        backup.chmod(0o600)
        path.write_text("\n".join(output) + "\n", encoding="utf-8")
        entry["changed"] = True
        entry["backup"] = str(backup)
    report["files"].append(entry)
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
if report["config"].get("changed") or any(item.get("changed") for item in report["files"]):
    print("redaction: corrected inherited secret-redaction=false drift")
else:
    print("redaction: no inherited secret-redaction=false drift found")
PY
}

fetch_slack_secrets_from_vault() {
  # Pull this agent's channel credentials from MAC's own vault.  OpenClaw and
  # Hermes have deliberately disjoint destinations: an OpenClaw deployment
  # must never refresh the retained rollback gateway's credential files.
  local fetcher="$SRC_DIR/scripts/mac-fetch-slack-secrets.py"
  if [ "$(printf '%s' "${MAC_DEPLOY_ROUTER_BACKEND:-}" | tr 'A-Z' 'a-z')" = inproc ]; then
    local mac_vault_url="${MAC_HUB_URL:-http://127.0.0.1:${MAC_PORT:-8789}}"
    local mac_vault_token="${MAC_WORKER_TOKEN:-${MAC_API_TOKEN:-}}"
    if [ -z "$mac_vault_token" ]; then
      log "skipping mac-vault Slack fetch: MAC_WORKER_TOKEN/MAC_API_TOKEN unavailable"
      return 0
    fi
    # th-merge-07: wait for the mac vault API to be serving before fetching. The
    # hub reads its OWN API here; during the hub's deploy that API may be briefly
    # mid-restart, which previously made the hub transiently fail its own slack
    # fetch (warning + preserve). /health is unauthenticated. Bounded (~30s).
    local _i
    for _i in $(seq 1 15); do
      curl -fsS -m3 "${mac_vault_url%/}/health" >/dev/null 2>&1 && break
      sleep 2
    done
    if [ "${HERMES_GATEWAY_IMPL:-hermes}" = "openclaw" ]; then
      local openclaw_fetcher="$SRC_DIR/scripts/mac-fetch-openclaw-secrets.py"
      [ -f "$openclaw_fetcher" ] \
        || die "OpenClaw credential fetcher is missing: $openclaw_fetcher"
      log "fetching OpenClaw channel credentials for ${AGENT} from mac vault ($mac_vault_url)"
      MAC_AGENT_NAME="$AGENT" \
        MAC_OPENCLAW_PUBLIC_IDENTITY="$OPENCLAW_PUBLIC_IDENTITY" \
        MAC_OPENCLAW_SLACK_ACCOUNT_ID="$OPENCLAW_SLACK_ACCOUNT_ID" \
        MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID="$OPENCLAW_TELEGRAM_ACCOUNT_ID" \
        MAC_SECRET_VAULT_URL="$mac_vault_url" \
        MAC_SECRET_VAULT_TOKEN="$mac_vault_token" \
        MAC_OPENCLAW_CREDENTIALS_FILE="$MAC_HOME/openclaw/credentials.env" \
        "$PY" "$openclaw_fetcher" >> "$DEPLOY_LOG" 2>&1 \
        || die "mac-vault OpenClaw credential fetch failed for ${AGENT}"
      return 0
    fi
    if [ ! -f "$fetcher" ]; then
      log "skipping Hermes Slack vault fetch: $fetcher not present (older mac source?)"
      return 0
    fi
    log "fetching Hermes rollback Slack secrets for ${AGENT} from mac vault ($mac_vault_url)"
    MAC_AGENT_NAME="$AGENT" \
      MAC_SECRET_VAULT_URL="$mac_vault_url" \
      MAC_SECRET_VAULT_TOKEN="$mac_vault_token" \
      HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
      "$PY" "$fetcher" >> "$DEPLOY_LOG" 2>&1 || \
        log "WARNING: mac-vault Slack fetch failed for ${AGENT}; existing slack config preserved"
    return 0
  fi
}

sync_hermes_slack_identity_env() {
  log "syncing Hermes Slack identity/routing environment"
  "$PY" - "$ENV_FILE" "$HOME/.hermes/.env" <<'PY'
from pathlib import Path
import sys

source_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_env(path: Path, updates: dict[str, str | None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            seen.add(key)
            if updates[key] is not None:
                output.append(f"{key}={updates[key]}")
        else:
            output.append(line)
    for key in sorted(updates):
        if key not in seen and updates[key] is not None:
            output.append(f"{key}={updates[key]}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    path.chmod(0o600)


def read_config_env_slack_tokens(config_path: Path) -> dict[str, str]:
    """Extract SLACK_BOT_TOKEN/SLACK_APP_TOKEN from the top-level env: block of
    config.yaml. This covers spokes whose Slack *vault fetch* is skipped but
    whose config.yaml already carries the tokens: the gateway wrapper sources
    ~/.hermes/.env, so the tokens must live there too or a restarted gateway
    comes up "No messaging platforms enabled"."""
    out: dict[str, str] = {}
    if not config_path.exists():
        return out
    in_env = False
    for line in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not in_env:
            if line.rstrip() == "env:":
                in_env = True
            continue
        if line.strip() and line[:1] not in {" ", "\t", "#"}:
            break  # left the env: block
        stripped = line.strip()
        for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
            if stripped.startswith(k + ":"):
                v = stripped.split(":", 1)[1].strip().strip("'\"")
                if v:
                    out[k] = v
    return out


source = read_env(source_path)
keys = (
    "MAC_HERMES_SLACK_HOME_CHANNEL_NAME",
    "ACC_SLACK_HOME_CHANNEL_NAME",
    "SLACK_HOME_CHANNEL_NAME",
    "MAC_HERMES_SYNC_SLACK_HOME_CHANNELS",
    "SLACK_ALLOWED_USERS",
    "SLACK_REQUIRE_MENTION",
    "SLACK_STRICT_MENTION",
)
updates: dict[str, str | None] = {}
for key in keys:
    value = source.get(key, "").strip()
    if value:
        updates[key] = value
# Ensure the gateway-sourced .env has the Slack tokens even when the vault fetch
# was skipped (spokes without the admin token) — read them from config.yaml.
for tk, tv in read_config_env_slack_tokens(target_path.parent / "config.yaml").items():
    updates.setdefault(tk, tv)
if updates:
    write_env(target_path, updates)
print("Slack identity env: synced %d value(s) to %s" % (len(updates), target_path))
PY
}

apply_hermes_gateway_runtime_shim() {
  log "applying Hermes gateway runtime/model shim"
  MAC_HERMES_AGENT_DIR="$HERMES_DIR" "$VENV/bin/python" - <<'PY'
from mac.hermes_startup import apply_hermes_gateway_runtime_shim_report

report = apply_hermes_gateway_runtime_shim_report()
patch = report.get("gateway_runtime_shim_patch") or {}
configured = bool(
    report.get("configured_model")
    or report.get("provider_override_configured")
    or report.get("base_url_override_configured")
)
print(
    "gateway runtime shim: present=%s applied=%s model=%s provider_override=%s base_url_override=%s error=%s"
    % (
        report.get("gateway_runtime_shim_present"),
        patch.get("applied"),
        report.get("configured_model") or "",
        report.get("provider_override_configured"),
        report.get("base_url_override_configured"),
        patch.get("error") or "",
    )
)
if configured and (patch.get("error") or not report.get("gateway_runtime_shim_present")):
    raise SystemExit(1)
PY
}

sync_hermes_chat_config() {
  # The Hermes runtime reads ~/.hermes/.env + config.yaml (+ auth.json pool) for
  # its chat provider, NOT mac.env — and those retained stale TokenHub state
  # (:8090 / provider: tokenhub / custom:* pool) across the retirement, so the
  # agent self-test + task execution dialed the dead endpoint or sent a rejected
  # bearer (403). Mirror mac.env's router endpoint + token into the runtime
  # config. Non-fatal: a failure leaves chat degraded but doesn't abort deploy.
  log "syncing Hermes chat config (in-mac-router endpoint + provider) from mac.env"
  "$VENV/bin/python" -m mac.hermes_chat_config --hermes-home "$HOME/.hermes" --mac-env "$ENV_FILE" \
    || log "WARNING: hermes chat config sync failed; agent chat self-test may stay degraded"
}

apply_hermes_fleet_surface() {
  if [ -z "${HERMES_SURFACE_B64:-}" ]; then
    return 0
  fi
  log "applying fleet Hermes config surface"
  "$VENV/bin/python" -m mac.hermes_config_surface apply \
    --payload-b64 "$HERMES_SURFACE_B64" \
    --hermes-home "$HOME/.hermes" \
    || log "WARNING: fleet Hermes config surface apply failed; preserving existing Hermes config"
}

install_fleet_skills() {
  # Fleet-wide (no GPU gate): these skills drive the hub's hosted models through
  # the in-mac router (vision + image generation), so every agent benefits — no
  # local GPU needed. Re-copied each deploy → durable + repeatable. Non-fatal.
  local src="$SRC_DIR/deploy/skills/fleet"
  local skills_dir="$HOME/.hermes/skills"
  [ -d "$src" ] || return 0
  mkdir -p "$skills_dir"
  local n=0 d
  for d in "$src"/*/; do
    [ -f "$d/SKILL.md" ] || continue
    cp -r "$d" "$skills_dir/" && n=$((n + 1))
  done
  log "fleet skills installed on $AGENT: $n skill(s) under $skills_dir"
}

install_omniverse_gpu_skills() {
  # GPU-only: the vendored NVIDIA Omniverse + physical-AI agent skills (and our
  # omniverse-kit-app build skill) are only useful where the Kit SDK / CUDA can
  # run, so gate on an actual NVIDIA GPU. This auto-scopes to GPU nodes without a
  # per-agent fleet-config flag, and re-extracting every deploy keeps it durable
  # + repeatable. Non-fatal. Asset is re-vendored via deploy/skills/README.md.
  if ! { command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; }; then
    log "Omniverse 3D skills: no NVIDIA GPU on $AGENT; skipping (GPU-only)"
    return 0
  fi
  local asset="$SRC_DIR/deploy/skills/omniverse-skills.tar.gz"
  local skills_dir="$HOME/.hermes/skills"
  if [ ! -f "$asset" ]; then
    log "Omniverse 3D skills: vendored asset missing ($asset); skipping"
    return 0
  fi
  mkdir -p "$skills_dir"
  if tar xzf "$asset" -C "$skills_dir" 2>/dev/null; then
    log "Omniverse 3D skills installed (GPU node $AGENT): $(tar tzf "$asset" 2>/dev/null | grep -c 'SKILL.md$') skills under $skills_dir"
  else
    log "WARNING: Omniverse 3D skills extraction failed on $AGENT"
  fi
}

_install_gen_unit() {
  # $1=service name  $2=wrapper basename ($MAC_HOME/bin/<name>)  $3=description.
  # Writes + enables + restarts a systemd unit for an already-written wrapper.
  local svc="$1" wrapper_name="$2" desc="$3"
  local unit="/etc/systemd/system/${svc}"
  log "installing systemd service $unit ($desc)"
  if sudo test -f "$unit"; then
    sudo cp -f "$unit" "$MAC_HOME/backups/${svc}.${AGENT}.${DEPLOY_TS}" 2>/dev/null || true
  fi
  sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=mac local media-gen server ($desc)
After=network-online.target ${MAC_AGENT_SERVICE_NAME}
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$MAC_HOME
EnvironmentFile=$ENV_FILE
ExecStart=$MAC_HOME/bin/${wrapper_name}
Restart=always
RestartSec=10
TimeoutStartSec=900
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  run_systemctl daemon-reload
  if ! run_systemctl enable "$svc" >/dev/null 2>&1; then
    log "WARNING: could not enable optional GPU service $svc"
    return 0
  fi
  run_systemctl restart "$svc" \
    || log "WARNING: $svc failed to start (journalctl -u $svc)"
}

install_gpu_gen_server() {
  # media-01: durable local media-gen servers for a GPU agent — image (:8189),
  # audio (:8190), video (:8191) — each as a GPU-gated systemd unit serving the
  # routes the agent advertises (#1/B1b). Provisions one shared venv
  # (torch/diffusers/transformers). GPU-gated (like Omniverse skills) +
  # systemd-only. Entirely non-fatal: a GPU-dep hiccup must never block the deploy.
  if truthy "${MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE:-0}"; then
    log "gen server: synchronized cutover excludes media-gen until its lifecycle joins the rollback journal"
    return 0
  fi
  if [ "$SUPERVISOR_KIND" != "systemd" ]; then
    log "gen server: supervisor is $SUPERVISOR_KIND (systemd-only for now); skipping"
    return 0
  fi
  if ! { command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; }; then
    log "gen server: no NVIDIA GPU on $AGENT; skipping (GPU-only)"
    return 0
  fi
  local gen_model audio_models video_models
  _genenv() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"'" || true; }
  gen_model="$(_genenv MAC_AGENT_GEN_MODEL)"
  audio_models="$(_genenv MAC_AGENT_GEN_AUDIO_MODELS)"
  video_models="$(_genenv MAC_AGENT_GEN_VIDEO_MODELS)"
  if [ -z "$gen_model" ] && [ -z "$audio_models" ] && [ -z "$video_models" ]; then
    log "gen server: no MAC_AGENT_GEN_MODEL/AUDIO_MODELS/VIDEO_MODELS set; skipping"
    return 0
  fi
  # Resolve the image catalog id (e.g. sdxl-turbo) to its HF repo using the MAC venv. The
  # gen venv has torch/diffusers but NOT the mac package, so the server can't
  # resolve the catalog id itself — bake the resolved repo into the wrapper, else
  # diffusers gets a bare "sdxl-turbo" and 500s (the hub then fails over to cloud).
  local gen_repo
  gen_repo="$("$VENV/bin/python" -c 'import sys
from mac.local_gen_catalog import get_model
m = get_model(sys.argv[1]); print(m.repo if m else sys.argv[1])' "$gen_model" 2>/dev/null || true)"
  [ -n "$gen_repo" ] || gen_repo="$gen_model"

  # Resolve the gen venv: reuse an existing ~/gen/venv that already has the stack
  # (e.g. a hand-provisioned GPU box), else build a dedicated $MAC_HOME/gen-venv.
  local gen_venv
  if [ -x "$HOME/gen/venv/bin/python" ] && "$HOME/gen/venv/bin/python" -c "import torch, diffusers" >/dev/null 2>&1; then
    gen_venv="$HOME/gen/venv"
    log "gen server: reusing existing gen venv $gen_venv"
  else
    gen_venv="$MAC_HOME/gen-venv"
    if [ ! -x "$gen_venv/bin/python" ]; then
      log "gen server: creating gen venv $gen_venv"
      "$PY" -m venv "$gen_venv" || { log "WARNING: gen venv creation failed; skipping gen server"; return 0; }
    fi
    "$gen_venv/bin/python" -m pip install --upgrade pip wheel >/dev/null 2>&1 || true
    # torch first (CUDA-specific index if configured), then the diffusers stack.
    local torch_index="${MAC_DEPLOY_AGENT_GEN_TORCH_INDEX_URL:-}"
    if ! "$gen_venv/bin/python" -c "import torch" >/dev/null 2>&1; then
      log "gen server: installing torch${torch_index:+ (index $torch_index)}"
      if [ -n "$torch_index" ]; then
        "$gen_venv/bin/python" -m pip install torch torchvision --index-url "$torch_index" >/dev/null 2>&1 \
          || log "WARNING: torch install failed (check MAC_DEPLOY_AGENT_GEN_TORCH_INDEX_URL for this GPU's CUDA)"
      else
        "$gen_venv/bin/python" -m pip install torch torchvision >/dev/null 2>&1 || log "WARNING: torch install failed"
      fi
    fi
    if ! "$gen_venv/bin/python" -c "import diffusers" >/dev/null 2>&1; then
      log "gen server: installing diffusers/transformers stack (+ audio codecs)"
      "$gen_venv/bin/python" -m pip install diffusers transformers accelerate safetensors pillow huggingface_hub soundfile scipy >/dev/null 2>&1 \
        || log "WARNING: diffusers stack install failed"
    fi
  fi
  "$gen_venv/bin/python" -m pip list --format=json > "$LOG_DIR/gen-server-deps.json" 2>/dev/null || true
  if ! "$gen_venv/bin/python" -c "import torch, diffusers" >/dev/null 2>&1; then
    log "WARNING: gen venv lacks torch/diffusers; installing the unit anyway (it retries the warm-load on start). Set MAC_DEPLOY_AGENT_GEN_TORCH_INDEX_URL for this GPU."
  fi

  # One wrapper + unit per configured modality, all sharing the gen venv. Each
  # wrapper sources mac.env, points HF at pre-staged weights, sets the modality
  # port, and execs the server. \$ vars stay runtime; $gen_venv/$SRC_DIR expand now.
  mkdir -p "$MAC_HOME/bin"

  # Image (:8189) — bake the resolved HF repo (the gen venv can't resolve catalog ids).
  if [ -n "$gen_model" ] && [ -f "$SRC_DIR/deploy/local-gen/openai_image_server.py" ]; then
    cat > "$MAC_HOME/bin/mac-gen-server" <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a
. "\$HOME/.mac/mac.env"
set +a
export PATH="$gen_venv/bin:\$PATH"
[ -n "\${MAC_AGENT_GEN_HF_HOME:-}" ] && export HF_HOME="\$MAC_AGENT_GEN_HF_HOME"
export LOCAL_GEN_MODEL="$gen_repo"
export LOCAL_GEN_PORT="\${MAC_AGENT_GEN_PORT:-8189}"
export LOCAL_GEN_HOST="\${MAC_AGENT_GEN_HOST:-0.0.0.0}"
exec "$gen_venv/bin/python" "$SRC_DIR/deploy/local-gen/openai_image_server.py"
EOF
    chmod 700 "$MAC_HOME/bin/mac-gen-server"
    _install_gen_unit "$MAC_GEN_SERVICE_NAME" "mac-gen-server" "image=$gen_model venv=$gen_venv"
  fi

  # Audio (:8190) — TTS/music/ASR; server defaults are valid HF repos (override
  # via LOCAL_AUDIO_* in mac.env). MAC_AGENT_GEN_AUDIO_MODELS declares served ops.
  if [ -n "$audio_models" ] && [ -f "$SRC_DIR/deploy/local-gen/audio_server.py" ]; then
    cat > "$MAC_HOME/bin/mac-gen-audio-server" <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a
. "\$HOME/.mac/mac.env"
set +a
export PATH="$gen_venv/bin:\$PATH"
[ -n "\${MAC_AGENT_GEN_HF_HOME:-}" ] && export HF_HOME="\$MAC_AGENT_GEN_HF_HOME"
export LOCAL_GEN_PORT="\${MAC_AGENT_GEN_AUDIO_PORT:-8190}"
export LOCAL_GEN_HOST="\${MAC_AGENT_GEN_HOST:-0.0.0.0}"
exec "$gen_venv/bin/python" "$SRC_DIR/deploy/local-gen/audio_server.py"
EOF
    chmod 700 "$MAC_HOME/bin/mac-gen-audio-server"
    _install_gen_unit "$MAC_GEN_AUDIO_SERVICE_NAME" "mac-gen-audio-server" "audio=$audio_models venv=$gen_venv"
  fi

  # Video (:8191) — async AnimateDiff/SVD; server defaults are valid HF repos.
  if [ -n "$video_models" ] && [ -f "$SRC_DIR/deploy/local-gen/video_server.py" ]; then
    cat > "$MAC_HOME/bin/mac-gen-video-server" <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a
. "\$HOME/.mac/mac.env"
set +a
export PATH="$gen_venv/bin:\$PATH"
[ -n "\${MAC_AGENT_GEN_HF_HOME:-}" ] && export HF_HOME="\$MAC_AGENT_GEN_HF_HOME"
export LOCAL_GEN_PORT="\${MAC_AGENT_GEN_VIDEO_PORT:-8191}"
export LOCAL_GEN_HOST="\${MAC_AGENT_GEN_HOST:-0.0.0.0}"
exec "$gen_venv/bin/python" "$SRC_DIR/deploy/local-gen/video_server.py"
EOF
    chmod 700 "$MAC_HOME/bin/mac-gen-video-server"
    _install_gen_unit "$MAC_GEN_VIDEO_SERVICE_NAME" "mac-gen-video-server" "video=$video_models venv=$gen_venv"
  fi
}

install_agent_footprint() {
  # media-01 Part C3: re-hydrate the agent's self-installed footprint from the
  # hub so a rebuilt agent keeps the pip/npm tools it self-provisioned. Pulls
  # GET /agents/<stable_id>.installed_packages and re-installs into the agent
  # venv (pip) + local npm prefix. Idempotent + non-fatal (a bad pin must never
  # block the deploy). Disable with MAC_AGENT_FOOTPRINT_REINSTALL=0.
  [ -x "$VENV/bin/python" ] || return 0
  if [ "${MAC_AGENT_FOOTPRINT_REINSTALL:-1}" = "0" ]; then
    log "agent footprint: re-hydrate disabled (MAC_AGENT_FOOTPRINT_REINSTALL=0)"
    return 0
  fi
  "$VENV/bin/python" - "$ENV_FILE" "$VENV" "$MAC_HOME" "$LOG_DIR" <<'PY' 2>&1 \
    | while IFS= read -r _ln; do log "agent footprint: $_ln"; done || true
import json, os, re, shutil, subprocess, sys, urllib.request
from collections import defaultdict

env_file, venv, mac_home, log_dir = sys.argv[1:5]
env = {}
try:
    for ln in open(env_file, encoding="utf-8"):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            env[k] = v.strip().strip('"').strip("'")
except Exception as exc:  # noqa: BLE001
    print("env read failed: %s; skipping" % exc); sys.exit(0)
hub = (env.get("MAC_HUB_URL") or "").rstrip("/")
token = env.get("MAC_WORKER_TOKEN") or env.get("MAC_API_TOKEN") or ""
name = env.get("MAC_WORKER_AGENT_NAME") or env.get("MAC_AGENT_NAME") or ""
if not (hub and token and name):
    print("hub/token/name unavailable; skipping"); sys.exit(0)
agent_id = "agent_%s" % (re.sub(r"[^A-Za-z0-9_.-]+", "_", name.lower()).strip("_") or "default")
req = urllib.request.Request(
    "%s/agents/%s" % (hub, agent_id), headers={"Authorization": "Bearer %s" % token}
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        fp = json.loads(resp.read().decode("utf-8")).get("installed_packages") or {}
except Exception as exc:  # noqa: BLE001
    print("no footprint to re-hydrate (%s)" % exc); sys.exit(0)
report = {"pip": [], "npm": []}
groups = defaultdict(list)
for e in (fp.get("pip") or []):
    spec = isinstance(e, dict) and (e.get("spec") or e.get("name"))
    if spec:
        groups[e.get("index_url") or ""].append(spec)
for index_url, specs in groups.items():
    cmd = [os.path.join(venv, "bin", "python"), "-m", "pip", "install", *specs]
    if index_url:
        cmd += ["--index-url", index_url]
    rc = subprocess.run(cmd, capture_output=True, text=True).returncode
    report["pip"].append({"specs": specs, "index_url": index_url, "returncode": rc})
    print("pip install %s -> rc=%d" % (" ".join(specs), rc))
npm_pkgs = [
    e.get("spec") or e.get("name")
    for e in (fp.get("npm") or [])
    if isinstance(e, dict) and (e.get("spec") or e.get("name"))
]
if npm_pkgs:
    if shutil.which("npm"):
        rc = subprocess.run(
            ["npm", "install", "--prefix", mac_home, *npm_pkgs], capture_output=True, text=True
        ).returncode
        report["npm"].append({"packages": npm_pkgs, "returncode": rc})
        print("npm install %s -> rc=%d" % (" ".join(npm_pkgs), rc))
    else:
        print("npm not present; skipping %d npm package(s)" % len(npm_pkgs))
try:
    with open(os.path.join(log_dir, "agent-footprint-reinstall.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh)
except Exception:  # noqa: BLE001
    pass
print("re-hydrate done (pip groups=%d, npm=%d)" % (len(report["pip"]), len(report["npm"])))
PY
}

initialize_hermes_home() {
  log "initializing Hermes home with upstream Hermes defaults"
  "$VENV/bin/python" - <<'PY'
from hermes_cli.config import ensure_hermes_home

ensure_hermes_home()
print("Hermes home initialized")
PY
}

ensure_hermes_identity_memory_continuity() {
  log "verifying Hermes identity and memory continuity"
  "$PY" - "$HOME/.hermes" <<'PY'
from pathlib import Path
import shutil
import sys

home = Path(sys.argv[1])
memories = home / "memories"
memories.mkdir(parents=True, exist_ok=True)


def link_or_copy(link_path: Path, target: Path) -> str:
    try:
        relative = target.relative_to(link_path.parent)
    except ValueError:
        relative = target
    try:
        link_path.symlink_to(relative)
        return "symlinked"
    except OSError:
        shutil.copy2(target, link_path)
        return "copied"


actions = []
for filename in ("MEMORY.md", "USER.md"):
    legacy = home / filename
    current = memories / filename
    if current.exists() and not legacy.exists():
        actions.append("%s:%s-legacy" % (filename, link_or_copy(legacy, current)))
    elif legacy.exists() and not current.exists():
        actions.append("%s:%s-current" % (filename, link_or_copy(current, legacy)))

soul = home / "SOUL.md"
if not soul.exists():
    actions.append("SOUL.md:missing")

if actions:
    print("Hermes identity/memory continuity: " + ", ".join(actions))
else:
    print("Hermes identity/memory continuity: ok")
PY
}

install_hermes_messaging_deps() {
  log "preinstalling configured Hermes messaging dependencies"
  "$VENV/bin/python" - "$HERMES_VENDORED" "$HOME/.hermes" "$LOG_DIR/hermes-messaging-deps.json" <<'PY'
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

repo = Path(sys.argv[1])
hermes_home = Path(sys.argv[2])
report_path = Path(sys.argv[3])
sys.path.insert(0, str(repo))

from tools.lazy_deps import LAZY_DEPS  # type: ignore


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


config = read(hermes_home / "config.yaml")
env_text = read(hermes_home / ".env")
features = set()
if (
    (hermes_home / "slack_accounts.json").exists()
    or os.environ.get("SLACK_BOT_TOKEN")
    or re.search(r"(?mi)^\s*SLACK_BOT_TOKEN\s*=", env_text)
    or re.search(r"(?mi)^\s*slack\s*:", config)
):
    features.add("platform.slack")
if (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or re.search(r"(?mi)^\s*TELEGRAM_BOT_TOKEN\s*=", env_text)
    or re.search(r"(?mi)^\s*telegram\s*:", config)
):
    features.add("platform.telegram")
if (
    os.environ.get("DISCORD_TOKEN")
    or re.search(r"(?mi)^\s*DISCORD_TOKEN\s*=", env_text)
    or re.search(r"(?mi)^\s*discord\s*:", config)
):
    features.add("platform.discord")

report = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "features": [],
}
failed = False
for feature in sorted(features):
    specs = list(LAZY_DEPS.get(feature, ()))
    entry = {"feature": feature, "specs": specs, "installed": False, "error": ""}
    if not specs:
        entry["error"] = "feature is not in Hermes LAZY_DEPS"
        failed = True
        report["features"].append(entry)
        continue
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *specs],
        text=True,
        capture_output=True,
    )
    entry["installed"] = result.returncode == 0
    if result.returncode != 0:
        entry["error"] = (result.stderr or result.stdout)[-4000:]
        failed = True
    report["features"].append(entry)

imports = {
    "platform.slack": ["slack_bolt", "slack_sdk", "aiohttp"],
    "platform.telegram": ["telegram"],
    "platform.discord": ["discord", "aiohttp", "brotlicffi"],
}
for entry in report["features"]:
    modules = imports.get(entry["feature"], [])
    entry["imports_ok"] = all(importlib.util.find_spec(module) is not None for module in modules)
    if not entry["imports_ok"]:
        failed = True

report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("messaging deps: %d configured feature(s), failures=%d" % (len(report["features"]), int(failed)))
raise SystemExit(1 if failed else 0)
PY
}

write_hermes_web_search_config() {
  log "writing Hermes web search configuration"
  "$VENV/bin/python" - "$HOME/.hermes/config.yaml" "$HOME/.hermes/.env" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1])
env_path = Path(sys.argv[2])
firecrawl_url = (
    os.environ.get("FIRECRAWL_API_URL")
    or os.environ.get("FIRECRAWL_GATEWAY_URL")
    or ""
).strip().rstrip("/")
required = True


def set_env(path: Path, updates: dict[str, str | None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            if updates[key] is not None:
                output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key in sorted(updates):
        if key not in seen and updates[key] is not None:
            output.append(f"{key}={updates[key]}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    path.chmod(0o600)


updates: dict[str, str | None] = {
    "MAC_REQUIRE_FIRECRAWL": "1",
}
if firecrawl_url:
    updates.update(
        {
            "FIRECRAWL_API_URL": firecrawl_url,
            "FIRECRAWL_GATEWAY_URL": firecrawl_url,
            "FIRECRAWL_API_KEY": os.environ.get("FIRECRAWL_API_KEY") or "none",
            "HERMES_WEB_SEARCH_BACKEND": "firecrawl",
            "HERMES_WEB_EXTRACT_BACKEND": "firecrawl",
            "MAC_WEB_SEARCH_PROVIDER": "firecrawl",
            "MAC_WEB_SEARCH_URL": firecrawl_url,
        }
    )
else:
    updates.update({"FIRECRAWL_API_URL": None, "FIRECRAWL_GATEWAY_URL": None})
set_env(env_path, updates)

config_path.parent.mkdir(parents=True, exist_ok=True)
try:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
except yaml.YAMLError:
    config = {}
if not isinstance(config, dict):
    config = {}
if firecrawl_url:
    web = config.setdefault("web", {})
    if not isinstance(web, dict):
        web = {}
        config["web"] = web
    web["backend"] = "firecrawl"
    web["search_backend"] = "firecrawl"
    web["extract_backend"] = "firecrawl"
config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
config_path.chmod(0o600)
print("web search config: firecrawl=%s required=%s" % (firecrawl_url or "disabled", required))
PY
}

install_hermes_web_deps() {
  log "preinstalling configured Hermes web dependencies"
  "$VENV/bin/python" - "$HOME/.hermes" "$LOG_DIR/hermes-web-deps.json" <<'PY'
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

hermes_home = Path(sys.argv[1])
report_path = Path(sys.argv[2])
env_text = ""
try:
    env_text = (hermes_home / ".env").read_text(encoding="utf-8", errors="ignore")
except OSError:
    pass
configured = bool(os.environ.get("FIRECRAWL_API_URL") or "FIRECRAWL_API_URL=" in env_text)
report = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "configured": configured,
    "specs": ["firecrawl-py==4.17.0"] if configured else [],
    "installed": False,
    "imports_ok": False,
    "error": "",
}
if configured:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "firecrawl-py==4.17.0"],
        text=True,
        capture_output=True,
    )
    report["installed"] = result.returncode == 0
    if result.returncode != 0:
        report["error"] = (result.stderr or result.stdout)[-4000:]
    report["imports_ok"] = importlib.util.find_spec("firecrawl") is not None
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("web deps: configured=%s installed=%s imports_ok=%s" % (configured, report["installed"], report["imports_ok"]))
raise SystemExit(1 if configured and (not report["installed"] or not report["imports_ok"]) else 0)
PY
}

sync_hermes_home_channels() {
  log "syncing Hermes Slack home-channel data"
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
  "$PY" "$SRC_DIR/deploy/sync-hermes-home-channels.py" \
    "${HERMES_SLACK_ACCOUNTS_FILE:-${HERMES_HOME:-$HOME/.hermes}/slack_accounts.json}" \
    "${HERMES_SLACK_HOME_CHANNELS_FILE:-${HERMES_HOME:-$HOME/.hermes}/slack_home_channels.json}" \
    "${HERMES_SLACK_CHANNEL_TEAMS_FILE:-${HERMES_HOME:-$HOME/.hermes}/slack_channel_teams.json}" \
    "$LOG_DIR/hermes-home-channel-sync.json" || \
    log "WARNING: Hermes Slack home-channel sync failed; preserving existing home-channel data"
}

repair_hermes_kanban_schema() {
  local report="$LOG_DIR/hermes-kanban-schema-repair.json"
  log "checking Hermes kanban SQLite schema compatibility"
  HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
  "$PY" - "$report" "$LOG_DIR" "$DEPLOY_TS" <<'PY'
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

report_path = Path(sys.argv[1])
log_dir = Path(sys.argv[2])
deploy_ts = sys.argv[3]
hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def add_column(
    conn: sqlite3.Connection,
    table: str,
    columns: set[str],
    column: str,
    ddl: str,
) -> bool:
    if column in columns:
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise
    columns.add(column)
    return True


def maybe_copy_column(
    conn: sqlite3.Connection,
    table: str,
    columns: set[str],
    dest: str,
    source: str,
    expression: str,
) -> None:
    if dest in columns and source in columns:
        conn.execute(f"UPDATE {table} SET {dest} = {expression}")


def candidate_dbs() -> list[Path]:
    paths: list[Path] = []
    legacy = hermes_home / "kanban.db"
    if legacy.exists():
        paths.append(legacy)
    boards = hermes_home / "kanban" / "boards"
    if boards.exists():
        paths.extend(sorted(boards.glob("*/kanban.db")))
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(path)
    return deduped


def repair_db(path: Path) -> dict[str, object]:
    entry: dict[str, object] = {
        "path": str(path),
        "exists": path.exists(),
        "changed": False,
        "backup": None,
        "added_columns": [],
        "created_indexes": [],
        "error": None,
    }
    if not path.exists():
        return entry
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        if not table_exists(conn, "tasks"):
            return entry

        task_cols = table_columns(conn, "tasks")
        planned = []
        optional_task_columns = [
            ("tenant", "tenant TEXT"),
            ("result", "result TEXT"),
            ("branch_name", "branch_name TEXT"),
            ("idempotency_key", "idempotency_key TEXT"),
            ("consecutive_failures", "consecutive_failures INTEGER NOT NULL DEFAULT 0"),
            ("worker_pid", "worker_pid INTEGER"),
            ("last_failure_error", "last_failure_error TEXT"),
            ("max_runtime_seconds", "max_runtime_seconds INTEGER"),
            ("last_heartbeat_at", "last_heartbeat_at INTEGER"),
            ("current_run_id", "current_run_id INTEGER"),
            ("workflow_template_id", "workflow_template_id TEXT"),
            ("current_step_key", "current_step_key TEXT"),
            ("skills", "skills TEXT"),
            ("model_override", "model_override TEXT"),
            ("max_retries", "max_retries INTEGER"),
            ("session_id", "session_id TEXT"),
        ]
        for column, ddl in optional_task_columns:
            if column not in task_cols:
                planned.append(("tasks", column, ddl))

        event_cols = table_columns(conn, "task_events") if table_exists(conn, "task_events") else set()
        if event_cols and "run_id" not in event_cols:
            planned.append(("task_events", "run_id", "run_id INTEGER"))

        notify_cols = (
            table_columns(conn, "kanban_notify_subs")
            if table_exists(conn, "kanban_notify_subs")
            else set()
        )
        if notify_cols and "notifier_profile" not in notify_cols:
            planned.append(
                ("kanban_notify_subs", "notifier_profile", "notifier_profile TEXT")
            )

        if planned:
            backup = log_dir / f"{path.name}.{deploy_ts}.bak"
            shutil.copy2(path, backup)
            entry["backup"] = str(backup)

        for table, column, ddl in planned:
            cols = table_columns(conn, table)
            if add_column(conn, table, cols, column, ddl):
                entry["added_columns"].append({"table": table, "column": column})
                entry["changed"] = True
                if table == "tasks" and column == "consecutive_failures":
                    maybe_copy_column(
                        conn,
                        "tasks",
                        table_columns(conn, "tasks"),
                        "consecutive_failures",
                        "spawn_failures",
                        "COALESCE(spawn_failures, 0)",
                    )
                if table == "tasks" and column == "last_failure_error":
                    maybe_copy_column(
                        conn,
                        "tasks",
                        table_columns(conn, "tasks"),
                        "last_failure_error",
                        "last_spawn_error",
                        "last_spawn_error",
                    )

        index_specs = [
            (
                "tasks",
                "session_id",
                "idx_tasks_session_id",
                "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(session_id)",
            ),
            (
                "tasks",
                "idempotency_key",
                "idx_tasks_idempotency",
                "CREATE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key)",
            ),
            (
                "task_events",
                "run_id",
                "idx_events_run",
                "CREATE INDEX IF NOT EXISTS idx_events_run ON task_events(run_id, id)",
            ),
        ]
        for table, column, name, sql in index_specs:
            if table_exists(conn, table) and column in table_columns(conn, table):
                conn.execute(sql)
                entry["created_indexes"].append(name)
        return entry
    except Exception as exc:  # pragma: no cover - remote deploy diagnostic.
        entry["error"] = str(exc)
        return entry
    finally:
        conn.close()


report = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "hermes_home": str(hermes_home),
    "databases": [repair_db(path) for path in candidate_dbs()],
}
report["changed_count"] = sum(1 for db in report["databases"] if db.get("changed"))
report["error_count"] = sum(1 for db in report["databases"] if db.get("error"))
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(
    "kanban schema repair: dbs=%d changed=%d errors=%d"
    % (len(report["databases"]), report["changed_count"], report["error_count"])
)
raise SystemExit(1 if report["error_count"] else 0)
PY
}

validate_typed_prerequisite_bundle() {
  case "$NODE_ACTION" in
    arm-phase2|apply-phase2) ;;
    *) die "typed prerequisite validation is restricted to phase 2" ;;
  esac
  [ -n "$PREREQUISITE_HELPER" ] \
    || die "synchronized apply-phase2 lacks the prerequisite verifier"
  [ -n "$PREREQUISITE_HELPER_SHA256" ] \
    || die "synchronized apply-phase2 lacks the prerequisite verifier digest"
  [ -n "$PREREQUISITE_BUNDLE" ] \
    || die "synchronized apply-phase2 lacks the prerequisite receipt bundle"
  [ -n "$PREREQUISITE_EXPECTATIONS" ] \
    || die "synchronized apply-phase2 lacks prerequisite expectations"
  case "$NODE_IDENTITY_SHA256" in
    [0-9a-f][0-9a-f]*) ;;
    *) die "synchronized apply-phase2 lacks a node identity digest" ;;
  esac
  [ "${#NODE_IDENTITY_SHA256}" -eq 64 ] \
    || die "synchronized apply-phase2 node identity digest is malformed"

  local helper_actual summary_tmp values
  local -a summary_values=()
  helper_actual="$("$PY" - "$PREREQUISITE_HELPER" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_absolute():
    raise SystemExit("prerequisite verifier path is not absolute")
descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > 1024 * 1024
    ):
        raise SystemExit("prerequisite verifier is not an owner-controlled bounded file")
    digest = hashlib.sha256()
    remaining = metadata.st_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            raise SystemExit("prerequisite verifier was truncated")
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise SystemExit("prerequisite verifier grew while reading")
    print(digest.hexdigest())
finally:
    os.close(descriptor)
PY
)" || die "synchronized apply-phase2 could not authenticate its prerequisite verifier"
  [ "$helper_actual" = "$PREREQUISITE_HELPER_SHA256" ] \
    || die "synchronized apply-phase2 prerequisite verifier digest differs"

  summary_tmp="${PREREQUISITE_SUMMARY}.tmp.$$"
  rm -f "$summary_tmp"
  umask 077
  if ! "$PY" "$PREREQUISITE_HELPER" validate-bundle \
    --bundle "$PREREQUISITE_BUNDLE" \
    --expectations "$PREREQUISITE_EXPECTATIONS" \
    --agent-id "$AGENT" \
    --node-identity-sha256 "$NODE_IDENTITY_SHA256" \
    --max-age-seconds "${MAC_DEPLOY_PREREQUISITE_MAX_AGE_SECONDS:-3600}" \
    > "$summary_tmp"; then
    rm -f "$summary_tmp"
    die "synchronized apply-phase2 prerequisite bundle is invalid"
  fi
  chmod 0600 "$summary_tmp"
  values="$("$PY" - "$summary_tmp" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
metadata = path.lstat()
raw = path.read_bytes()
try:
    value = json.loads(raw)
except (TypeError, ValueError) as exc:
    raise SystemExit("prerequisite summary is malformed") from exc
required = {
    "machine-onboarding",
    "route-tunnel",
    "openshell",
    "qdrant",
    "firecrawl",
    "webdav",
    "hermes",
    "service-topology",
}
participants = value.get("participants") if isinstance(value, dict) else None
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != os.getuid()
    or stat.S_IMODE(metadata.st_mode) != 0o600
    or metadata.st_nlink != 1
    or len(raw) > 1024 * 1024
    or value.get("schema") != "mac.fleet_prerequisite_bundle_summary.v1"
    or value.get("agent_id") != os.environ["AGENT"]
    or value.get("node_identity_sha256") != os.environ["NODE_IDENTITY_SHA256"]
    or not isinstance(participants, list)
    or len(participants) != len(required)
    or {item.get("participant") for item in participants if isinstance(item, dict)}
    != required
):
    raise SystemExit("prerequisite summary differs from the exact node contract")
for key in ("bundle_sha256", "expectations_sha256"):
    digest = value.get(key)
    if not isinstance(digest, str) or len(digest) != 64 or any(
        char not in "0123456789abcdef" for char in digest
    ):
        raise SystemExit("prerequisite summary digest is malformed")
print(value["bundle_sha256"])
print(value["expectations_sha256"])
PY
)" || {
    rm -f "$summary_tmp"
    die "synchronized apply-phase2 prerequisite summary failed validation"
  }
  mapfile -t summary_values <<<"$values"
  PREREQUISITE_BUNDLE_SHA256="${summary_values[0]:-}"
  PREREQUISITE_EXPECTATIONS_SHA256="${summary_values[1]:-}"
  [ -n "$PREREQUISITE_BUNDLE_SHA256" ] \
    && [ -n "$PREREQUISITE_EXPECTATIONS_SHA256" ] \
    || die "synchronized apply-phase2 prerequisite summary lacks its binding"
  mv -f "$summary_tmp" "$PREREQUISITE_SUMMARY"
  log "typed prerequisite bundle proved for the exact node identity"
}

log "deploy log: $DEPLOY_LOG"
SUPERVISOR_KIND="$(detect_supervisor)"
export SUPERVISOR_KIND
log "selected supervisor: $SUPERVISOR_KIND (requested: ${SUPERVISOR_REQUESTED:-auto})"

case "$NODE_ACTION" in
  rollback-phase2)
    DEPLOY_ROLLBACK_IN_PROGRESS=1
    [ -x "$ROLLBACK_SCRIPT" ] && [ ! -L "$ROLLBACK_SCRIPT" ] \
      || die "exact phase-2 rollback program is unavailable"
    "$ROLLBACK_SCRIPT"
    DEPLOY_COMPLETED=1
    exit 0
    ;;
  finalize)
    write_phase2_finalize_receipt
    DEPLOY_COMPLETED=1
    exit 0
    ;;
esac

if [ "$NODE_ACTION" = arm-phase2 ] || [ "$NODE_ACTION" = apply-phase2 ]; then
  validate_typed_prerequisite_bundle
else
  # Monotonic machine preparation is an onboarding responsibility.  These
  # legacy gates are verification-only; typed phase 2 consumes exact participant
  # receipts instead of rediscovering an open-ended set of host assumptions.
  ensure_dns_resolution
  ensure_venv_support
  install_github_cli
  configure_github_https_credentials
  install_github_review_key
  install_codegraph_cli
fi

capture_darwin_launchd_prestate
capture_phase1_prior_worker_topology
capture_prior_deployment_identity
arm_phase2_rollback
write_deploy_manifest "pre" "$MANIFEST_PRE"
if [ "$NODE_ACTION" = arm-phase2 ]; then
  DEPLOY_COMPLETED=1
  log "phase-2 arm complete; apply remains explicitly pending"
  exit 0
fi
if [ "${MAC_DEPLOY_TEST_INTERRUPT_AFTER_PHASE2_INTENT:-0}" = 1 ]; then
  stop_deployment_lock_renewer
  kill -KILL "$$"
fi

# Everything below this boundary may mutate the active generation.  The exact
# rollback executable and owner-private intent have already been fsynced and
# read back, so controller or process death cannot strand an unauthorised
# half-generation.
disk_hygiene_report "before-cleanup" "$LOG_DIR/disk-before-cleanup-${DEPLOY_TS}.json"
install_fleet_registry
if [ "$NODE_ACTION" = legacy-one-shot ]; then
  drain_mac_agent_before_deploy
  stop_existing_services_for_deploy
  quiesce_daemon_resources_before_source_replacement
else
  log "typed phase 2 is consuming the journal-bound phase-1 quiescence proof"
fi
backup_existing_artifacts
[ "$DEPLOY_ROLLBACK_ARMED" = 1 ] \
  || die "phase-2 apply reached source replacement without durable rollback intent"
log "installing mac source"
rm -rf "$SRC_DIR.new"
install_archive_source=0
if [ -n "$DEPLOY_GIT_URL" ] && git clone --quiet --branch "$DEPLOY_GIT_BRANCH" "$DEPLOY_GIT_URL" "$SRC_DIR.new"; then
  actual_rev="$(git -C "$SRC_DIR.new" rev-parse HEAD)"
  if [ "$actual_rev" != "$DEPLOY_REV" ]; then
    # Pin the worktree to the operator's exact deploy revision. fetch + reset,
    # NOT `merge --ff-only`: ff aborts with "Not possible to fast-forward"
    # whenever $DEPLOY_REV isn't a descendant of the freshly-cloned branch HEAD
    # — e.g. origin/$DEPLOY_GIT_BRANCH advanced past the operator's local HEAD
    # (someone else merged mid-session), which leaves the spoke half-deployed.
    # We want exactly $DEPLOY_REV regardless of how it relates to the branch tip.
    if git -C "$SRC_DIR.new" fetch --quiet origin "$DEPLOY_REV" \
        && git -C "$SRC_DIR.new" reset --hard --quiet "$DEPLOY_REV"; then
      actual_rev="$(git -C "$SRC_DIR.new" rev-parse HEAD)"
    else
      log "WARNING: could not install exact deploy rev $DEPLOY_REV from origin; using the exact deployment archive"
      install_archive_source=1
    fi
  fi
else
  log "WARNING: git clone failed or was not configured; installing archive without self-update worktree"
  install_archive_source=1
fi
if [ "$install_archive_source" = 1 ]; then
  rm -rf "$SRC_DIR.new"
  mkdir -p "$SRC_DIR.new"
  tar -xzf "$ARCHIVE" -C "$SRC_DIR.new"
fi
if [ "$install_archive_source" = 0 ]; then
  actual_rev="$(git -C "$SRC_DIR.new" rev-parse HEAD)"
  [ "$actual_rev" = "$DEPLOY_REV" ] || die \
    "installed Git source revision $actual_rev does not match deploy revision $DEPLOY_REV"
fi
mv "$SRC_DIR.new" "$SRC_DIR"

# Archive installs intentionally have no .git directory. Preserve the
# operator's exact git-archive revision outside the replaced source tree so
# runtime-image bootstrap can enforce the same source/image identity contract
# on every node and every subsequent bootstrap invocation.
deployed_source_revision_file="$MAC_HOME/deployed-source-revision"
deployed_source_revision_tmp="${deployed_source_revision_file}.tmp.$$"
printf '%s\n' "$DEPLOY_REV" > "$deployed_source_revision_tmp"
chmod 0600 "$deployed_source_revision_tmp"
mv -f "$deployed_source_revision_tmp" "$deployed_source_revision_file"
rm -f "$ARCHIVE"

if [ "$NODE_ACTION" = legacy-one-shot ]; then
  initialize_codegraph_repository "$SRC_DIR"
else
  log "typed phase 2 defers CodeGraph indexing to post-commit maintenance"
fi

log "creating/updating mac environment file"
PYTHONPATH="$SRC_DIR/src:${PYTHONPATH:-}" "$PY" -m mac.deploy_env write-mac-env \
  "$ENV_FILE" "$MAC_HOME" "$HOME" "$MAC_PORT" \
  "$HERMES_SLACK_HOME_CHANNEL_NAME" "$HERMES_GATEWAY_MODEL" \
  "$HERMES_GATEWAY_PROVIDER" "$HERMES_GATEWAY_BASE_URL" \
  "$HUB_URL" "$HUB_TOKEN" "$CONTROL_BIND_HOST" "$WORKER_MODE" \
  "$WORKER_CAPABILITIES" "$WORKER_ALLOWED_PROJECTS" \
  "$WORKER_REQUIRED_METADATA" "$WORKER_REQUIRE_CANARY" \
  "$AGENT" "$SUPERVISOR_KIND" "$SHARED_SERVICES_MANAGER_AGENT" \
  "$QDRANT_URL_CONFIGURED" "$QDRANT_REQUIRE" "$QDRANT_PORT_CONFIGURED" \
  "$FIRECRAWL_URL_CONFIGURED" "$FIRECRAWL_REQUIRE" "$FIRECRAWL_PORT_CONFIGURED" \
  "$WEBDAV_ENABLED" "$WEBDAV_URL_CONFIGURED" "$WEBDAV_PORT_CONFIGURED" \
  "$WEBDAV_ROOT_CONFIGURED" "$WEBDAV_PUBLIC_PATH_CONFIGURED"

normalize_hermes_redaction_env

deploy_barrier_file="$MAC_HOME/deploy-start-barrier"
if truthy "$DEFER_AGENT_RESTART"; then
  deploy_barrier_tmp="${deploy_barrier_file}.tmp.$$"
  printf '%s\n' "$DEPLOY_GENERATION" > "$deploy_barrier_tmp"
  chmod 0600 "$deploy_barrier_tmp"
  mv -f "$deploy_barrier_tmp" "$deploy_barrier_file"
else
  rm -f "$deploy_barrier_file"
fi

reload_mac_env
if [ "$NODE_ACTION" = legacy-one-shot ]; then
  reconcile_disabled_optional_openshell
  prepare_work_package_pipeline_storage
  # gketun-02: the hub (shared-services manager) owns the reverse-tunnel keypair it
  # uses to dial spokes, so it generates that key (ensure_hub_tunnel_key). Every
  # spoke must AUTHORIZE the hub's pubkey (install_hub_tunnel_pubkey) so the hub's
  # `ssh -R` reverse tunnel can connect. Decide by role, not worker mode.
  if [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]; then
    ensure_hub_tunnel_key
  else
    if [ "$WORKER_MODE" = "loop" ]; then
      ensure_hub_tunnel_key
    fi
    install_hub_tunnel_pubkey
    wait_for_hub_reverse_tunnel
  fi
  install_or_validate_shared_services
  write_hermes_memory_topology
else
  log "typed phase 2 consumed infrastructure receipts; tunnel, OpenShell, shared-service, and storage mutation is forbidden"
fi

log "installing mac Python package (with vendored Hermes runtime + gateway + relay extras)"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel >/dev/null
# ADR 0001 hu-04: install the hermes-gateway extra so the vendored Hermes
# runtime (src/mac/_hermes) runs in-process from this one venv — no separate
# hermes-agent venv needed. The gateway service execs mac-hermes-gateway.
# The relay extra ships nemo-relay so the gateway has the observability seam at
# deploy time (the worker also reconciles REQUIRED_RUNTIME_PIP at lifecycle
# start, so a stale node self-upgrades on demand — see mac/worker.py).
"$VENV/bin/python" -m pip install -e "${SRC_DIR}[hermes-gateway,relay]" >/dev/null
if [ "$NODE_ACTION" = legacy-one-shot ]; then
  mkdir -p "$HOME/.local/bin"
  ln -sf "$VENV/bin/mac" "$HOME/.local/bin/mac"
else
  [ -d "$HOME/.local/bin" ] && [ ! -L "$HOME/.local/bin" ] \
    || die "typed phase 2 requires the onboarded local bin directory"
  [ -L "$HOME/.local/bin/mac" ] \
    && [ "$(readlink "$HOME/.local/bin/mac")" = "$VENV/bin/mac" ] \
    || die "typed phase 2 requires the onboarding-owned mac CLI link"
fi

# OpenShell owns the schema used by every later sandbox.  Upgrade and prove the
# final gateway now, while the node is drained and all prior services remain
# stopped.  OpenClaw is installed only after this returns successfully.
if [ "$NODE_ACTION" = legacy-one-shot ]; then
  bootstrap_enabled_openshell
  install_or_validate_web_search_service
  write_hermes_web_search_config
  install_or_validate_publish_service
else
  log "typed phase 2 retained the receipt-proved OpenShell and shared-service authorities"
fi

log "using vendored in-tree Hermes runtime (ADR 0001 hu-04; no upstream clone)"
# The Hermes runtime ships pinned + patched in the mac package at
# $HERMES_VENDORED and runs in-process from the single mac venv ($VENV) — there
# is no upstream clone and no separate hermes venv. HERMES_DIR stays a path
# symbol for the (guarded) backup/restore logic but is intentionally NOT created.
git -C "$SRC_DIR" rev-parse HEAD > "$LOG_DIR/hermes-vendored-rev.txt" 2>/dev/null || true
cat "$HERMES_VENDORED/SNAPSHOT_PIN" > "$LOG_DIR/hermes-vendored-pin.txt" 2>/dev/null || true
if [ "$NODE_ACTION" = legacy-one-shot ]; then
  initialize_hermes_home
  ensure_hermes_identity_memory_continuity
  apply_hermes_gateway_runtime_shim
  sync_hermes_chat_config
  apply_hermes_fleet_surface
  install_fleet_skills
  install_omniverse_gpu_skills
  install_hermes_web_deps
  install_hermes_messaging_deps
  repair_hermes_kanban_schema
  log "installed Hermes agent from upstream plus mac-managed patches"
else
  log "typed phase 2 retained the receipt-proved Hermes durable state and configuration"
fi

mac_authority() {
  if control_plane_enabled; then
    "$VENV/bin/mac" --local-authority --db "$MAC_DB" "$@"
  else
    "$VENV/bin/mac" --hub-url "$MAC_HUB_URL" "$@"
  fi
}

retire_spoke_local_control_plane_database() {
  if control_plane_enabled; then
    return 0
  fi
  local source="$MAC_HOME/mac.db" plan="$LOG_DIR/spoke-local-ledger-plan.json"
  local active retirement archive
  [ -f "$source" ] || return 0

  log "spoke has a legacy local control-plane database; inspecting before retirement"
  "$VENV/bin/mac" --json migrate local-ledger --source-db "$source" > "$plan"
  active="$("$PY" - "$plan" <<'PY'
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    print(int(json.load(handle).get("active_task_count") or 0))
PY
)"
  if [ "$active" -gt 0 ]; then
    log "ERROR: legacy spoke database contains $active active task(s); refusing to strand them"
    log "Migrate them explicitly to $MAC_HUB_URL with: mac --hub-url <hub> migrate local-ledger --execute"
    return 1
  fi

  retirement="$LOG_DIR/spoke-local-ledger-retirement.json"
  "$VENV/bin/mac" --json migrate local-ledger \
    --source-db "$source" \
    --archive-dir "$MAC_HOME/archive" \
    --retire-inactive > "$retirement"
  archive="$("$PY" - "$retirement" <<'PY'
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    print(json.load(handle)["archive_path"])
PY
)"
  log "archived inactive legacy spoke database at $archive"
}

if [ "$NODE_ACTION" = legacy-one-shot ]; then
  if control_plane_enabled; then
    log "initializing hub control-plane database"
    mac_authority init >/dev/null
    register_hermes_runtime_identity
  else
    log "configuring spoke as a database-free hub client"
    retire_spoke_local_control_plane_database
  fi
  write_hermes_runtime_context
  verify_hermes_prompt_bridge
else
  log "typed phase 2 retained hub database, runtime identity, and Hermes context authorities"
fi

ACC_DB=""
for candidate in "$HOME/.acc/data/fleet.db" "$HOME/.acc/data/acc.db"; do
  if [ -f "$candidate" ]; then
    ACC_DB="$candidate"
    break
  fi
done

summarize_report() {
  local label="$1" path="$2"
  "$PY" - "$label" "$path" <<'PY'
import json
import sys
label, path = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as handle:
    data = json.load(handle)
counts = data.get("counts", {})
imp = data.get("import") or {}
print(
    f"{label}: tasks={counts.get('tasks', 0)} planned={counts.get('tasks_planned_for_import', 0)} "
    f"active_blockers={counts.get('active_tasks_blocking', 0)} terminal_skipped={counts.get('terminal_tasks_skipped', 0)} "
    f"private_tables={len(data.get('skipped_private_tables') or [])} "
    f"errors={len(imp.get('errors') or []) if imp else 0}"
)
warnings = data.get("warnings") or []
if warnings:
    print(f"{label}: warnings={len(warnings)}")
PY
}

write_migration_status() {
  local status="$1" db_path="${2:-}"
  "$PY" - "$LOG_DIR/acc-migration-status.json" "$status" "$db_path" <<'PY'
import json
import sys
import time
from pathlib import Path

report_path = Path(sys.argv[1])
status = sys.argv[2]
db_path = sys.argv[3] or None
hermes_home = Path.home() / ".hermes"
state_refs = {
    "hermes_home": hermes_home.exists(),
    "hermes_state_db": (hermes_home / "state.db").exists(),
    "hermes_soul": (hermes_home / "SOUL.md").exists(),
    "hermes_memory": (hermes_home / "MEMORY.md").exists() or (hermes_home / "memories" / "MEMORY.md").exists(),
}
host_class = "acc_migrated" if status in {"imported", "already_imported", "dry_run"} else "missing_migration_source"
if status == "no_acc_sqlite_db" and (state_refs["hermes_state_db"] or state_refs["hermes_soul"] or state_refs["hermes_memory"]):
    host_class = "hermes_state_only"
report = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "status": status,
    "host_class": host_class,
    "database": db_path,
    "hermes_state_refs": state_refs,
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("migration status: status=%s host_class=%s" % (status, host_class))
PY
}

if [ "$NODE_ACTION" = legacy-one-shot ] && [ -n "$ACC_DB" ]; then
  if control_plane_enabled && [ -f "$LOG_DIR/acc-migration-import.json" ] && [ "${MAC_FORCE_ACC_MIGRATION:-0}" != "1" ]; then
    log "existing ACC migration import report found; skipping one-time import"
    summarize_report "migration import existing" "$LOG_DIR/acc-migration-import.json"
    write_migration_status "already_imported" "$ACC_DB"
  else
    log "running ACC migration dry-run from $ACC_DB"
    mac_authority migrate acc "$ACC_DB" \
      --mode dry-run \
      --agent-home "$HOME" \
      --report "$LOG_DIR/acc-migration-dry-run.json" \
      > "$LOG_DIR/acc-migration-dry-run.stdout.json"
    summarize_report "migration dry-run" "$LOG_DIR/acc-migration-dry-run.json"

    log "running ACC migration import with active tasks requeued"
    mac_authority migrate acc "$ACC_DB" \
      --mode import \
      --allow-active \
      --agent-home "$HOME" \
      --report "$LOG_DIR/acc-migration-import.json" \
      > "$LOG_DIR/acc-migration-import.stdout.json"
    summarize_report "migration import" "$LOG_DIR/acc-migration-import.json"
    write_migration_status "imported" "$ACC_DB"
  fi
elif [ "$NODE_ACTION" = legacy-one-shot ]; then
  log "no ACC SQLite database found under ~/.acc/data; classifying host"
  write_migration_status "no_acc_sqlite_db" ""
else
  log "typed phase 2 forbids ACC or spoke-ledger migration"
fi

install_mac_control_wrapper() {
  local wrapper="${1:-$MAC_HOME/bin/mac-service}"
  mkdir -p "$(dirname "$wrapper")"
  cat > "$wrapper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
. "$HOME/.mac/mac.env"
set +a
export PATH="$HOME/.mac/bin:$HOME/.mac/venv/bin:$PATH"
export HERMES_REDACT_SECRETS=true
exec "$HOME/.mac/venv/bin/uvicorn" mac.api:create_app --factory --host "${MAC_BIND_HOST:-127.0.0.1}" --port "${MAC_PORT:-8789}" --workers 1 --log-level info
EOF
  chmod 700 "$wrapper"
}

escrow_router_provider_keys() {
  # Stream B (B2): on the HUB, escrow each router provider's upstream key into the
  # local encrypted vault under the secret:<name> the provider spec references, so
  # the in-mac router resolves it from secure storage rather than a plaintext env
  # var. Keys stay centralized on the hub; spokes hold none and never run a router.
  # HUB-only, idempotent (skips names already in the vault), best-effort with a
  # loud warning on failure (chat will not route until the key is in the vault).
  # The provider key values + MAC_DEPLOY_ROUTER_PROVIDERS arrive in the deploy env;
  # the per-provider source var is <PROVIDER>_API_KEY (e.g. nvidia -> NVIDIA_API_KEY).
  [ "$WORKER_MODE" = "loop" ] && [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ] || return 0
  case "${MAC_DEPLOY_ROUTER_PROVIDERS:-}" in *key=secret:*) ;; *) return 0 ;; esac
  reload_mac_env
  log "escrowing router provider keys into the hub vault"
  if "$PY" - >> "$DEPLOY_LOG" 2>&1 <<'PY'
import json, os, re, urllib.request, urllib.error
from mac.providers import provider_key_env
providers = os.environ.get("MAC_DEPLOY_ROUTER_PROVIDERS", "")
port = os.environ.get("MAC_PORT", "8789")
tok = os.environ.get("MAC_API_TOKEN", "")

def existing_names():
    req = urllib.request.Request(
        "http://127.0.0.1:%s/secrets" % port,
        headers={"Authorization": "Bearer " + tok},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return {s.get("name") for s in json.load(resp)}

def post_secret(name, value, capabilities):
    body = json.dumps({
        "name": name,
        "value": value,
        "scopes": {"capabilities": capabilities},
        "created_by": "deploy",
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%s/secrets" % port, data=body,
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=15)
    print("escrowed %r (HTTP %s, %d-char value)" % (name, resp.status, len(value)))

# provider id -> source-env-var, from the single registry (mac.providers). An
# unmapped provider (in MAC_ROUTER_PROVIDERS but not the registry) warns loudly.
PROVIDER_KEY_ENV = provider_key_env()
have = existing_names()
escrowed = skipped = 0
for chunk in providers.split(";"):
    chunk = chunk.strip()
    if not chunk:
        continue
    pid = chunk.split("=", 1)[0].strip()
    m = re.search(r"key=secret:([^,;]+)", chunk)
    if not m:
        continue
    name = m.group(1).strip()
    if name in have:
        print("escrow: %r already in vault; skip" % name)
        skipped += 1
        continue
    env_var = PROVIDER_KEY_ENV.get(pid)
    if not env_var:
        print("escrow: WARNING no source env var mapped for provider %r (secret %r); "
              "skip — add %r to PROVIDER_KEY_ENV" % (pid, name, pid))
        skipped += 1
        continue
    value = (os.environ.get(env_var) or "").strip()
    if not value:
        print("escrow: %s empty/unset for secret %r; skip" % (env_var, name))
        skipped += 1
        continue
    post_secret(name, value, ["router-upstream"])
    escrowed += 1
# Modality upstream keys: the hub's /v1/{genai,audio,video} proxies resolve
# MAC_ROUTER_<M>_KEY (secret:<name>) from the vault. Each modality is a SEPARATE
# entitlement from the chat key — build.nvidia.com's genai/image API rejects the
# chat key (401) — so each escrows ONLY its own source var and NEVER falls back
# to NVIDIA_API_KEY. Falling back escrows a key the upstream refuses, turning a
# clean deploy-time "disabled" into a runtime 401 (media-02). MAC_ROUTER_<M>_KEY
# is read from the freshly written mac.env (reload_mac_env ran before this step).
media_status = []
for _modality, _spec_env, _src_env in (
    ("image", "MAC_ROUTER_IMAGE_KEY", "NVIDIA_IMAGE_API_KEY"),
    ("audio", "MAC_ROUTER_AUDIO_KEY", "NVIDIA_AUDIO_API_KEY"),
    ("video", "MAC_ROUTER_VIDEO_KEY", "NVIDIA_VIDEO_API_KEY"),
):
    _spec = (os.environ.get(_spec_env) or "").strip()
    if not _spec.startswith("secret:"):
        continue
    _name = _spec[len("secret:"):]
    _value = (os.environ.get(_src_env) or "").strip()
    if _name in have:
        print("escrow: %r already in vault; skip" % _name); skipped += 1
        media_status.append("%s=enabled(vault)" % _modality)
    elif not _value:
        print("media %s DISABLED: set %s to enable (the chat key is NOT used — "
              "distinct entitlement); %r not escrowed" % (_modality, _src_env, _name)); skipped += 1
        media_status.append("%s=disabled(no %s)" % (_modality, _src_env))
    else:
        post_secret(_name, _value, ["router-upstream", _modality]); escrowed += 1
        media_status.append("%s=enabled(escrowed)" % _modality)
if media_status:
    print("media ops: %s" % ", ".join(media_status))
print("router key escrow: %d escrowed, %d skipped" % (escrowed, skipped))
PY
  then
    :
  else
    log "WARNING: router provider key escrow failed; chat will not route until the upstream key(s) are escrowed into the hub vault (mac secret create)"
  fi
}

scrub_spoke_provider_secrets() {
  # Clean invariant (NOT mutate-in-place): a spoke routes every provider call
  # through the hub, so its gateway env (~/.hermes/.env) must hold NO upstream
  # provider/API keys — only messaging connection tokens (SLACK_*/MATTERMOST_*,
  # which the gateway needs to connect directly) and the hub-token gateway creds
  # mac.env supplies. The Slack/identity sync upserts specific keys and PRESERVES
  # the rest, so a pre-centralization deploy's provider secrets would otherwise
  # survive re-deploy forever. Strip the known upstream-provider secrets every
  # deploy so re-deploy converges to the clean invariant. HUB keeps its keys (it
  # runs the router + escrows them). Spoke-only, idempotent, backs up first.
  [ "$AGENT" != "$SHARED_SERVICES_MANAGER_AGENT" ] || return 0
  local henv="$HOME/.hermes/.env"
  [ -f "$henv" ] || return 0
  # Upstream provider/API keys + base-url companions, from the single registry
  # (mac.providers). Messaging tokens (SLACK_*, MATTERMOST_*) and MAC_*/gateway
  # creds are intentionally NOT in that set — the gateway needs them locally. If
  # the registry can't be loaded, skip loudly rather than silently scrub nothing.
  local strip
  strip="$("$PY" -m mac.providers scrub-regex 2>/dev/null || true)"
  if [ -z "$strip" ]; then
    log "WARNING: could not load provider registry (mac.providers); spoke gateway env NOT scrubbed"
    return 0
  fi
  local hits
  hits="$(grep -cE "^(export )?($strip)=" "$henv" 2>/dev/null || true)"
  if [ "${hits:-0}" -eq 0 ]; then
    log "spoke gateway env already clean of upstream provider keys"
    return 0
  fi
  log "scrubbing ${hits} stale upstream provider key(s) from spoke gateway env (clean invariant)"
  grep -nE "^(export )?($strip)=" "$henv" | sed -E 's/=.*/=<redacted>/' | sed 's/^/      /' >> "$DEPLOY_LOG" 2>&1 || true
  cp -pf "$henv" "$henv.bak-scrub-${DEPLOY_TS}"
  awk -v p="^(export )?($strip)=" '$0 !~ p' "$henv" > "$henv.scrub.$$"
  chmod 600 "$henv.scrub.$$"
  mv -f "$henv.scrub.$$" "$henv"
}

sync_messaging_config() {
  # th-merge-07: fetch Slack secrets + resolve the home channel AFTER the mac API
  # is (re)started, so the hub reads its OWN freshly-up /v1 vault instead of
  # racing its own service cycle (the early pre-restart attempt could hit
  # connection-refused). Runs before the gateway (re)start so the gateway picks
  # up the resolved home channel. Idempotent for spokes (they read the hub API).
  reload_mac_env
  # A pure worker (gateway_impl=none) has no chat gateway, so it needs no Slack
  # secrets, identity, or home-channel data at all. Skip the whole Hermes/Slack
  # block — otherwise the home-channel sync fails on a gateway-less node and
  # aborts the worker deploy (the != "openclaw" guard wrongly included "none").
  if [ "${HERMES_GATEWAY_IMPL:-hermes}" = "none" ]; then
    log "gateway_impl=none: pure worker; skipping Hermes/Slack gateway setup"
  else
    fetch_slack_secrets_from_vault
    reload_mac_env
    if [ "${HERMES_GATEWAY_IMPL:-hermes}" != "openclaw" ]; then
      sync_hermes_slack_identity_env
      sync_hermes_home_channels
    fi
  fi
}

prepare_openclaw_gateway() {
  local installer="$SRC_DIR/deploy/openclaw/install-openclaw-gateway.sh"
  [ -x "$installer" ] || die "stock OpenClaw installer not found/executable: $installer"
  MAC_SRC="$SRC_DIR" \
  MAC_OPENCLAW_AGENT_ID="${MAC_AGENT_ID:-agent_$AGENT}" \
  MAC_OPENCLAW_FLEET_NAME="$FLEET_NAME" \
  MAC_OPENCLAW_MODEL="${HERMES_GATEWAY_MODEL:-${MAC_HERMES_GATEWAY_MODEL:-}}" \
  MAC_OPENCLAW_ROUTER_URL="${HERMES_GATEWAY_BASE_URL:-${MAC_HERMES_GATEWAY_BASE_URL:-}}" \
  MAC_OPENCLAW_PUBLIC_IDENTITY="$OPENCLAW_PUBLIC_IDENTITY" \
  MAC_OPENCLAW_REPRESENTED_BY="$OPENCLAW_REPRESENTED_BY" \
  MAC_OPENCLAW_REPRESENTATION_MODE="$OPENCLAW_REPRESENTATION_MODE" \
  MAC_OPENCLAW_SLACK_ACCOUNT_ID="$OPENCLAW_SLACK_ACCOUNT_ID" \
  MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID="$OPENCLAW_TELEGRAM_ACCOUNT_ID" \
  MAC_OPENCLAW_REQUIRE_NO_HOST_SCRIPT_AUTOMATION=1 \
    "$installer" prepare
}

verify_openclaw_gateway() {
  local installer="$SRC_DIR/deploy/openclaw/install-openclaw-gateway.sh"
  assert_legacy_nemoclaw_containers_inactive pre_verify || return $?
  MAC_SRC="$SRC_DIR" \
  MAC_OPENCLAW_AGENT_ID="${MAC_AGENT_ID:-agent_$AGENT}" \
  MAC_OPENCLAW_FLEET_NAME="$FLEET_NAME" \
  MAC_OPENCLAW_MODEL="${HERMES_GATEWAY_MODEL:-${MAC_HERMES_GATEWAY_MODEL:-}}" \
  MAC_OPENCLAW_ROUTER_URL="${HERMES_GATEWAY_BASE_URL:-${MAC_HERMES_GATEWAY_BASE_URL:-}}" \
  MAC_OPENCLAW_PUBLIC_IDENTITY="$OPENCLAW_PUBLIC_IDENTITY" \
  MAC_OPENCLAW_REPRESENTED_BY="$OPENCLAW_REPRESENTED_BY" \
  MAC_OPENCLAW_REPRESENTATION_MODE="$OPENCLAW_REPRESENTATION_MODE" \
  MAC_OPENCLAW_SLACK_ACCOUNT_ID="$OPENCLAW_SLACK_ACCOUNT_ID" \
  MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID="$OPENCLAW_TELEGRAM_ACCOUNT_ID" \
  MAC_OPENCLAW_LIVE_CANARY="${MAC_DEPLOY_OPENCLAW_LIVE_CANARY:-0}" \
    "$installer" verify
}

finalize_openclaw_gateway() {
  local installer="$SRC_DIR/deploy/openclaw/install-openclaw-gateway.sh"
  assert_legacy_nemoclaw_containers_inactive pre_finalize || return $?
  MAC_SRC="$SRC_DIR" \
  MAC_OPENCLAW_AGENT_ID="${MAC_AGENT_ID:-agent_$AGENT}" \
  MAC_OPENCLAW_FLEET_NAME="$FLEET_NAME" \
  MAC_OPENCLAW_MODEL="${HERMES_GATEWAY_MODEL:-${MAC_HERMES_GATEWAY_MODEL:-}}" \
  MAC_OPENCLAW_ROUTER_URL="${HERMES_GATEWAY_BASE_URL:-${MAC_HERMES_GATEWAY_BASE_URL:-}}" \
  MAC_OPENCLAW_PUBLIC_IDENTITY="$OPENCLAW_PUBLIC_IDENTITY" \
  MAC_OPENCLAW_REPRESENTED_BY="$OPENCLAW_REPRESENTED_BY" \
  MAC_OPENCLAW_REPRESENTATION_MODE="$OPENCLAW_REPRESENTATION_MODE" \
  MAC_OPENCLAW_SLACK_ACCOUNT_ID="$OPENCLAW_SLACK_ACCOUNT_ID" \
  MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID="$OPENCLAW_TELEGRAM_ACCOUNT_ID" \
  MAC_OPENCLAW_SUPERVISOR="$SUPERVISOR_KIND" \
    "$installer" finalize
}

withdraw_openclaw_gateway() {
  local installer="$SRC_DIR/deploy/openclaw/install-openclaw-gateway.sh"
  MAC_OPENCLAW_FLEET_NAME="$FLEET_NAME" \
  MAC_OPENCLAW_SUPERVISOR="$SUPERVISOR_KIND" \
    "$installer" withdraw
}

install_linux_service() {
  local unit="/etc/systemd/system/${MAC_SERVICE_NAME}" restart_since
  local unit_staging="$LOG_DIR/${MAC_SERVICE_NAME}.${DEPLOY_TS}.$$.stage"
  install_hermes_gateway_wrapper
  install_mac_agent_wrapper
  if sudo test -f "$unit"; then
    MAC_UNIT_BACKUP="$MAC_HOME/backups/${MAC_SERVICE_NAME}.${AGENT}.${DEPLOY_TS}"
    snapshot_rollback_file "$unit" "$MAC_UNIT_BACKUP" system
    write_rollback_script
  fi
  if control_plane_enabled; then
    if [ "$DEPLOY_ROLLBACK_ARMED" = 1 ] && [ -z "$MAC_UNIT_BACKUP" ]; then
      die "cannot mutate the control-plane unit without a prior-generation backup"
    fi
    MAC_UNIT_MUTATED=1
    write_rollback_script
    log "installing hub control-plane systemd service $unit"
    install_mac_control_wrapper
    cat > "$unit_staging" <<EOF
[Unit]
Description=mac control plane replacement for ACC
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$MAC_HOME
EnvironmentFile=$ENV_FILE
ExecStart=$MAC_HOME/bin/mac-service
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
    mac_launchd_atomic_replace "$unit_staging" "$unit" system 0644 0 0
    run_systemctl daemon-reload
    run_systemctl enable "$MAC_SERVICE_NAME"
    restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    run_systemctl restart "$MAC_SERVICE_NAME"
    wait_for_local_control_plane_health
    run_systemctl --no-pager -l status "$MAC_SERVICE_NAME" \
      > "$LOG_DIR/mac-service-status.txt" || true
    run_journalctl -u "$MAC_SERVICE_NAME" --since "$restart_since" --no-pager \
      > "$LOG_DIR/mac-service-journal.txt" || true
    if [ "$NODE_ACTION" = legacy-one-shot ]; then
      escrow_router_provider_keys
    fi
  else
    log "spoke role: removing stale local control-plane systemd service"
    disable_systemd_service_if_present "$MAC_SERVICE_NAME"
    if sudo test -f "$unit"; then
      MAC_UNIT_MUTATED=1
      write_rollback_script
      mac_launchd_remove_file_and_fsync "$unit" system
    fi
    run_systemctl daemon-reload
    : > "$LOG_DIR/mac-service-not-installed.txt"
  fi
  if [ "$NODE_ACTION" = legacy-one-shot ]; then
    scrub_spoke_provider_secrets
    sync_messaging_config
  else
    log "typed phase 2 retained the hub vault and Hermes credential projection"
  fi
  # Route to the configured gateway implementation. Stock OpenClaw is the
  # primary; Hermes remains the explicit rollback path; NemoClaw is reference
  # compatibility only.
  case "${HERMES_GATEWAY_IMPL:-hermes}" in
    openclaw)
      install_linux_openclaw_service ;;
    nemoclaw)
      install_linux_nemoclaw_service ;;
    hermes|"")
      install_linux_hermes_service ;;
    none)
      install_linux_no_gateway_service ;;
    *)
      die "unsupported Linux gateway implementation: ${HERMES_GATEWAY_IMPL}" ;;
  esac
}

install_linux_no_gateway_service() {
  local unit
  log "gateway_impl=none: proving every systemd gateway inactive and disabled"
  for unit in "$OPENCLAW_SERVICE_NAME" "$HERMES_SERVICE_NAME" "$NEMOCLAW_SERVICE_NAME"; do
    disable_systemd_service_if_present "$unit"
  done
  rm -f "$MAC_HOME/bin/openclaw-gateway" "$MAC_HOME/bin/hermes-gateway"
  install_linux_agent_service
}

install_linux_openclaw_service() {
  local unit_src="$SRC_DIR/deploy/systemd/mac-openclaw-gateway.service"
  local rendered="$MAC_HOME/openclaw/${OPENCLAW_SERVICE_NAME}.rendered"
  local unit="/etc/systemd/system/${OPENCLAW_SERVICE_NAME}" restart_since
  prepare_openclaw_gateway
  [ -f "$unit_src" ] || die "stock OpenClaw systemd template not found: $unit_src"
  python3 - "$unit_src" "$rendered" "$USER" "$MAC_HOME" <<'PY'
import sys

source, dest, user, home = sys.argv[1:]
text = open(source, encoding="utf-8").read()
text = text.replace("__MAC_USER__", user).replace("__MAC_HOME__", home)
if "__MAC_" in text:
    raise SystemExit("unresolved OpenClaw systemd placeholder")
with open(dest, "w", encoding="utf-8") as handle:
    handle.write(text)
PY
  chmod 0600 "$rendered"
  mac_launchd_atomic_replace "$rendered" "$unit" system 0644 0 0
  run_systemctl daemon-reload
  run_systemctl enable "$OPENCLAW_SERVICE_NAME"
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  run_systemctl restart "$OPENCLAW_SERVICE_NAME"
  run_journalctl -u "$OPENCLAW_SERVICE_NAME" --since "$restart_since" --no-pager \
    > "$LOG_DIR/openclaw-gateway-journal.txt" || true
  if ! verify_openclaw_gateway; then
    log "ERROR: stock OpenClaw verification failed; withdrawing the successor before generation rollback"
    if ! withdraw_openclaw_gateway; then
      log "ERROR: OpenClaw withdrawal failed; generation rollback remains armed and deployment stays blocked"
    fi
    return 1
  fi
  disable_systemd_service_if_present "$HERMES_SERVICE_NAME"
  run_systemctl reset-failed "$HERMES_SERVICE_NAME" 2>/dev/null || true
  disable_systemd_service_if_present "$NEMOCLAW_SERVICE_NAME"
  if ! finalize_openclaw_gateway; then
    log "ERROR: OpenClaw exclusivity proof failed; withdrawing the successor before generation rollback"
    if ! withdraw_openclaw_gateway; then
      log "ERROR: OpenClaw withdrawal failed; generation rollback remains armed and deployment stays blocked"
    fi
    return 1
  fi
  log "stock OpenClaw verified as exclusive gateway; Hermes retained only for rollback"
  install_linux_agent_service
}

install_linux_nemoclaw_service() {
  # Install the NemoClaw gateway service (YOLO migration: replaces hermes gateway).
  # Requires install-nemoclaw-gateway.sh to have already been run (or will run it).
  local unit_src="$SRC_DIR/deploy/systemd/mac-nemoclaw-gateway.service"
  local unit="/etc/systemd/system/${NEMOCLAW_SERVICE_NAME}" restart_since control_after=""
  local unit_staging="$LOG_DIR/${NEMOCLAW_SERVICE_NAME}.${DEPLOY_TS}.$$.stage"
  if control_plane_enabled; then
    control_after="$MAC_SERVICE_NAME"
  fi
  log "installing NemoClaw gateway systemd service $unit"
  [ -f "$unit_src" ] || die "NemoClaw service template not found: $unit_src"
  cp -f "$unit_src" "$unit_staging"
  mac_launchd_atomic_replace "$unit_staging" "$unit" system 0644 0 0
  run_systemctl daemon-reload
  run_systemctl enable "$NEMOCLAW_SERVICE_NAME"
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  run_systemctl restart "$NEMOCLAW_SERVICE_NAME"
  run_systemctl --no-pager -l status "$NEMOCLAW_SERVICE_NAME" \
    > "$LOG_DIR/nemoclaw-gateway-status.txt" || true
  run_journalctl -u "$NEMOCLAW_SERVICE_NAME" --since "$restart_since" --no-pager \
    > "$LOG_DIR/nemoclaw-gateway-journal.txt" || true

  # Stop and disable the hermes gateway service (YOLO: it must not run alongside NemoClaw).
  log "disabling hermes gateway service (YOLO: NemoClaw replaces it)"
  disable_systemd_service_if_present "$HERMES_SERVICE_NAME"
  disable_systemd_service_if_present "$OPENCLAW_SERVICE_NAME"
  run_systemctl reset-failed "$HERMES_SERVICE_NAME" 2>/dev/null || true
  log "hermes gateway stopped and disabled; NemoClaw gateway active"
  install_linux_agent_service
}

install_hermes_gateway_wrapper() {
  # Worker/gateway decoupling: a pure worker (gateway_impl=none) runs no chat
  # gateway at all — only the mac-agent worker. Skip installing the Hermes
  # gateway wrapper so the node is a clean executor, not a conversational agent.
  if [ "${HERMES_GATEWAY_IMPL:-hermes}" = "none" ]; then
    log "gateway_impl=none: pure worker; skipping Hermes gateway wrapper install"
    return 0
  fi
  local wrapper="${1:-$MAC_HOME/bin/hermes-gateway}"
  mkdir -p "$(dirname "$wrapper")"
  cat > "$wrapper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ulimit -n "${MAC_SERVICE_NOFILE_LIMIT:-4096}" 2>/dev/null || true
set -a
set +u
[ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"
[ -f "$HOME/.mac/mac.env" ] && . "$HOME/.mac/mac.env"
set -u
set +a
export PATH="$HOME/.mac/bin:$HOME/.mac/venv/bin:$PATH"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_DISABLE_LAZY_INSTALLS=1
export HERMES_REDACT_SECRETS=true
if [ -z "${OPENAI_BASE_URL:-}" ] && [ -n "${CUSTOM_BASE_URL:-}" ]; then
  export OPENAI_BASE_URL="$CUSTOM_BASE_URL"
fi
if [ -z "${ACC_HERMES_GATEWAY_API_KEY:-}" ] && [ -n "${MAC_HERMES_GATEWAY_API_KEY:-}" ]; then
  export ACC_HERMES_GATEWAY_API_KEY="$MAC_HERMES_GATEWAY_API_KEY"
fi
# ADR 0001 hu-04: run the vendored Hermes gateway in-process from the mac venv
# (mac-hermes-gateway -> hermes_cli.main "gateway run --replace"), instead of a
# separate hermes-agent venv. Validated in fleet rollout 2026-05-31.
exec "$HOME/.mac/venv/bin/python" -m mac.hermes_gateway
EOF
  chmod 700 "$wrapper"
}

install_mac_agent_wrapper() {
  local wrapper="${1:-$MAC_HOME/bin/mac-agent-service}"
  local selftest="${2:-$MAC_HOME/bin/mac-agent-startup-self-test}"
  local executor="${3:-$MAC_HOME/bin/mac-task-executor}"
  local executor_py="${4:-$MAC_HOME/bin/mac-task-executor.py}"
  local crash_observer="${5:-$MAC_HOME/bin/mac-crash-observer}"
  local install_aliases="${6:-1}"
  local report_python="$MAC_HOME/venv/bin/mac-report-python"
  mkdir -p \
    "$(dirname "$wrapper")" \
    "$(dirname "$selftest")" \
    "$(dirname "$executor")" \
    "$(dirname "$executor_py")" \
    "$(dirname "$crash_observer")"
  # venv/bin/python is normally a symlink. A retarget between attestation and
  # exec would select an unapproved interpreter, so install a real immutable-by-
  # identity launcher within the venv and attest/invoke this exact file.
  local resolved_python
  resolved_python="$("$VENV/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
  [ -f "$resolved_python" ] || die "resolved worker Python is missing: $resolved_python"
  install -m 0755 "$resolved_python" "${report_python}.new"
  mv -f "${report_python}.new" "$report_python"
  # Deliberately run outside the MAC virtualenv: it must remain usable when a
  # broken MAC import graph is the reason the worker cannot start.
  install -m 0755 "$SRC_DIR/deploy/mac-crash-observer.py" "$crash_observer"
  cat > "$wrapper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ulimit -n "${MAC_SERVICE_NOFILE_LIMIT:-4096}" 2>/dev/null || true
ulimit -c unlimited 2>/dev/null || true
set -a
. "$HOME/.mac/mac.env"
set +a
export PATH="$HOME/.mac/bin:$HOME/.mac/venv/bin:$HOME/.mac/node_modules/.bin:$HOME/.cargo/bin:$PATH"
export PYTHONFAULTHANDLER=1
export PYTHONUNBUFFERED=1

: "${MAC_HUB_URL:?MAC_HUB_URL is required}"
: "${MAC_WORKER_TOKEN:?MAC_WORKER_TOKEN is required}"

agent_name="${MAC_WORKER_AGENT_NAME:-$(hostname -s 2>/dev/null || hostname)}"
host_name="${MAC_WORKER_HOSTNAME:-$agent_name}"
workspace="${MAC_WORKER_WORKSPACE:-$HOME/.mac/agent-workspaces}"
mode="${MAC_WORKER_MODE:-heartbeat}"
capabilities="${MAC_WORKER_CAPABILITIES:-ops,python,openclaw,review,api,architecture,cli,docs,security,testing,typescript,ui,web_search,web_extract,web_crawl,firecrawl,work_package_v1}"
# Hardware capability probes: append gpu/cuda if nvidia-smi sees GPUs; always append cpu.
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L 2>/dev/null | grep -q "^GPU"; then
  capabilities="$capabilities,gpu,cuda"
fi
capabilities="$capabilities,cpu"
mkdir -p "$workspace"

stable_agent_id() {
  "$HOME/.mac/venv/bin/python" - "$agent_name" <<'PY'
import re
import sys
safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", sys.argv[1].lower()).strip("_") or "default"
print("agent_%s" % safe)
PY
}

mark_worker_offline() {
  local agent_id
  agent_id="${MAC_WORKER_AGENT_ID:-$(stable_agent_id)}"
  curl -fsS --max-time 10 -X POST \
    -H "Content-Type: application/json" \
    --config - \
    --data '{"status":"offline","health_status":"degraded"}' \
    "$MAC_HUB_URL/agents/$agent_id/heartbeat" >/dev/null <<CURL || true
header = "Authorization: Bearer $MAC_WORKER_TOKEN"
CURL
}
trap mark_worker_offline TERM INT

if [ "${MAC_AGENT_STARTUP_SELF_TEST:-1}" != "0" ]; then
  # The self-test declares its verdict through its exit code: 0 = passed or
  # degraded (non-blocking), 1 = a genuine blocking misconfiguration.  Guard the
  # unguarded invocation so 'set -e' only kills mac-agent-service on a real
  # blocking verdict; any other non-zero exit (a transient probe blip that the
  # self-test itself already downgrades, or an internal self-test fault) leaves
  # the worker degraded but running instead of crash-looping the service.
  selftest_rc=0
  "$HOME/.mac/bin/mac-agent-startup-self-test" || selftest_rc=$?
  if [ "$selftest_rc" -eq 1 ]; then
    exit 1
  elif [ "$selftest_rc" -ne 0 ]; then
    echo "mac-agent-service: startup self-test exited $selftest_rc; continuing degraded" >&2
  fi
fi

common=(
  "$HOME/.mac/venv/bin/mac-agent"
  --url "$MAC_HUB_URL"
  --register
  --agent-id "${MAC_AGENT_ID:-$(stable_agent_id)}"
  --agent-name "$agent_name"
  --hostname "$host_name"
  --capabilities "$capabilities"
  --workspace "$workspace"
  --lease-seconds "${MAC_WORKER_LEASE_SECONDS:-900}"
  --poll-interval "${MAC_WORKER_POLL_INTERVAL:-2}"
  --attestation-key-env "$HOME/.mac/mac.env"
)
worker_resources="${MAC_WORKER_RESOURCES:-}"
if [ -n "${MAC_WORKER_RESOURCES_FILE:-}" ] && [ -f "$MAC_WORKER_RESOURCES_FILE" ]; then
  worker_resources="$(< "$MAC_WORKER_RESOURCES_FILE")"
fi
if [ -n "$worker_resources" ]; then
  common+=(--resources "$worker_resources")
fi
if [ -n "${MAC_WORKER_HERMES_INSTANCE_ID:-${MAC_HERMES_INSTANCE_ID:-}}" ]; then
  common+=(--hermes-instance-id "${MAC_WORKER_HERMES_INSTANCE_ID:-${MAC_HERMES_INSTANCE_ID:-}}")
fi
if [ -n "${MAC_WORKER_ALLOWED_PROJECTS:-}" ]; then
  common+=(--allowed-projects "$MAC_WORKER_ALLOWED_PROJECTS")
fi
if [ -n "${MAC_WORKER_REQUIRED_METADATA:-}" ]; then
  common+=(--required-metadata "$MAC_WORKER_REQUIRED_METADATA")
fi
case "${MAC_WORKER_REQUIRE_CANARY:-}" in
  1|true|TRUE|yes|YES|on|ON)
    common+=(--require-canary)
    ;;
esac

case "$mode" in
  heartbeat)
    interval="${MAC_WORKER_HEARTBEAT_INTERVAL:-30}"
    while :; do
      "${common[@]}" --heartbeat-only || true
      sleep "$interval"
    done
    ;;
  dry-run)
    interval="${MAC_WORKER_HEARTBEAT_INTERVAL:-30}"
    while :; do
      "${common[@]}" --dry-run-claim || true
      sleep "$interval"
    done
    ;;
  loop)
    executor="${MAC_WORKER_EXECUTOR:-$HOME/.mac/bin/mac-task-executor}"
    if [ "$executor" = "$HOME/.mac/bin/mac-task-executor" ]; then
      test -x "$HOME/.mac/venv/bin/python"
      test -f "$HOME/.mac/bin/mac-task-executor.py"
    fi
    exec "${common[@]}" --loop --executor "$executor"
    ;;
  *)
    echo "unsupported MAC_WORKER_MODE=$mode" >&2
    exit 2
    ;;
esac
EOF
  chmod 700 "$wrapper"

  cat > "$selftest" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
. "$HOME/.mac/mac.env"
set +a
selftest_python="$HOME/.mac/venv/bin/python"
exec "$selftest_python" - <<'PY'
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def falsey(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"0", "false", "no", "off"}


def stable_agent_id(name: str) -> str:
    import re

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.lower()).strip("_") or "default"
    return f"agent_{safe}"


def tail(text: str, limit: int = 1200) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def output_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def is_transient_timeout(exc: BaseException) -> bool:
    # A read/connect timeout during a shared-service probe is a transient hub
    # blip, not a misconfiguration: socket timeouts surface as ``TimeoutError``
    # (an ``OSError`` subclass) directly or wrapped inside ``URLError.reason``.
    candidate: BaseException | None = exc
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        candidate = exc.reason
    if isinstance(candidate, TimeoutError):
        return True
    text = str(candidate).lower()
    return "timed out" in text or "timeout" in text


def probe_http(
    path_base: str,
    suffix: str,
    headers: dict[str, str] | None = None,
    *,
    attempts: int = 3,
    backoff: float = 1.5,
) -> tuple[bool, str, bool]:
    # Returns (ok, error, timed_out). ``timed_out`` is True only when every
    # bounded retry exhausted on a transient timeout, letting the caller class a
    # persistent shared-service timeout as degraded rather than a hard block.
    if not path_base:
        return False, "endpoint is not configured", False
    url = path_base.rstrip("/") + suffix
    request = urllib.request.Request(url, headers=headers or {})
    last_error = ""
    last_timed_out = False
    for attempt in range(1, max(1, attempts) + 1):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read(1_048_576)
            return True, "", False
        except (OSError, urllib.error.URLError) as exc:
            last_error = safe_error(exc)
            last_timed_out = is_transient_timeout(exc)
            # Only retry transient timeouts; a refused connection or bad status
            # is deterministic and retrying just delays the verdict.
            if not last_timed_out or attempt >= max(1, attempts):
                break
            time.sleep(backoff * attempt)
    return False, last_error, last_timed_out


def classify_openclaw_agent_failure(output: str) -> str:
    normalized = output.lower()
    if (
        "budget_exceeded" in normalized
        or "insufficient_quota" in normalized
        or "exceeded your current quota" in normalized
        or ("http 429" in normalized and "quota" in normalized)
    ):
        return "budget_exceeded"
    if "no eligible models registered" in normalized:
        return "no_eligible_models"
    return ""


home = Path.home()
mac_home = home / ".mac"
report_path = Path(
    os.environ.get("MAC_AGENT_STARTUP_SELF_TEST_REPORT")
    or mac_home / "logs" / "mac-agent-startup-self-test.json"
)
report_path.parent.mkdir(parents=True, exist_ok=True)

agent_name = os.environ.get("MAC_WORKER_AGENT_NAME") or os.environ.get("MAC_WORKER_HOSTNAME") or ""
agent_id = os.environ.get("MAC_AGENT_ID") or os.environ.get("MAC_WORKER_AGENT_ID") or ""
if not agent_id and agent_name:
    agent_id = stable_agent_id(agent_name)
hermes_instance = (
    os.environ.get("MAC_WORKER_HERMES_INSTANCE_ID")
    or os.environ.get("MAC_HERMES_INSTANCE_ID")
    or ""
)
persona_id = os.environ.get("MAC_HERMES_PERSONA_ID") or ""
tenant_id = os.environ.get("MAC_FLEET_TENANT_ID") or ""
resources_path = Path(
    os.environ.get("MAC_WORKER_RESOURCES_FILE")
    or mac_home / "openclaw" / "service-advertisement.json"
)
openclaw_config_path = mac_home / "openclaw" / "managed" / "openclaw.json"
openclaw_agent_bin = Path(
    os.environ.get("MAC_OPENCLAW_AGENT_BIN")
    or mac_home / "bin" / "openclaw-agent"
)
qdrant_url = str(
    os.environ.get("QDRANT_URL")
    or os.environ.get("QDRANT_ADDRESS")
    or os.environ.get("QDRANT_FLEET_URL")
    or ""
).rstrip("/")
qdrant_key = os.environ.get("QDRANT_API_KEY") or os.environ.get("QDRANT_FLEET_KEY") or ""
qdrant_required = True
qdrant_required_flag = os.environ.get("MAC_REQUIRE_QDRANT_MEMORY")
qdrant_disable_flag = os.environ.get("MAC_QDRANT_MEMORY") or os.environ.get("ACC_QDRANT_MEMORY")
firecrawl_url = str(
    os.environ.get("FIRECRAWL_API_URL")
    or os.environ.get("FIRECRAWL_GATEWAY_URL")
    or os.environ.get("MAC_WEB_SEARCH_URL")
    or ""
).rstrip("/")
firecrawl_key = os.environ.get("FIRECRAWL_API_KEY") or ""
firecrawl_required = True
firecrawl_required_flag = os.environ.get("MAC_REQUIRE_FIRECRAWL")
timeout = int(os.environ.get("MAC_AGENT_STARTUP_SELF_TEST_TIMEOUT") or "120")
problems: list[str] = []
checks: dict[str, object] = {
    "identity_env": False,
    "openclaw_runtime": False,
    "openshell_executor_config": False,
    "report_repository_executor_attestation": False,
    "qdrant_shared_memory": False,
    "firecrawl_web_search": False,
    "openclaw_agent": False,
}
runtime_provider: dict[str, object] = {}
agent_output = ""
agent_returncode: int | None = None
openclaw_failure_class = ""

openshell_create_args = str(os.environ.get("MAC_OPENSHELL_CREATE_ARGS") or "").strip()
openshell_enabled = truthy(os.environ.get("MAC_OPENSHELL_SANDBOX"))
if openshell_enabled and openshell_create_args:
    try:
        openshell_create_argv = shlex.split(openshell_create_args)
    except ValueError as exc:
        problems.append(f"MAC_OPENSHELL_CREATE_ARGS is invalid: {safe_error(exc)}")
    else:
        forbidden = sorted({arg for arg in openshell_create_argv if arg in {"--env", "--"}})
        if forbidden:
            problems.append(
                "MAC_OPENSHELL_CREATE_ARGS contains forbidden executor arguments: "
                + ", ".join(forbidden)
            )
openshell_bin = str(os.environ.get("MAC_OPENSHELL_BIN") or "openshell").strip() or "openshell"
if openshell_enabled and shutil.which(openshell_bin) is None:
    problems.append(
        f"OpenShell sandbox is enabled but MAC_OPENSHELL_BIN is not executable: {openshell_bin}"
    )
checks["openshell_executor_config"] = not any(
    problem.startswith(("MAC_OPENSHELL_CREATE_ARGS", "OpenShell sandbox is enabled"))
    for problem in problems
)
report_executor_attestation: dict[str, object] = {}
if openshell_enabled and str(os.environ.get("MAC_WORKER_MODE") or "").strip() == "loop":
    try:
        from mac.worker import _read_only_report_executor_attestation

        observed_report_attestation = _read_only_report_executor_attestation(
            [str(mac_home / "bin" / "mac-task-executor")]
        )
    except Exception as exc:
        observed_report_attestation = None
        problems.append(
            "report repository executor attestation probe failed: "
            + safe_error(exc)
        )
    if not isinstance(observed_report_attestation, dict):
        problems.append(
            "report repository executor lacks the exact hardened OpenShell attestation"
        )
    else:
        report_executor_attestation = observed_report_attestation
        checks["report_repository_executor_attestation"] = True
else:
    checks["report_repository_executor_attestation"] = not openshell_enabled

for key, value in {
    "MAC_WORKER_AGENT_NAME": agent_name,
    "MAC_AGENT_ID": agent_id,
    "MAC_HERMES_INSTANCE_ID": hermes_instance,
    "MAC_HERMES_PERSONA_ID": persona_id,
    "MAC_FLEET_TENANT_ID": tenant_id,
}.items():
    if not value:
        problems.append(f"missing required identity env {key}")
checks["identity_env"] = not any(problem.startswith("missing required identity env") for problem in problems)

# Worker/gateway decoupling: the OpenClaw runtime/ownership advertisement and the
# openclaw-agent self-test only apply when this agent actually runs an OpenClaw
# chat gateway. A pure worker (MAC_CHAT_GATEWAY_IMPL != "openclaw") has no gateway,
# so these checks are skipped — otherwise a gateway-less worker could never pass
# its startup self-test and would refuse to start (the whole point of a worker is
# to claim and execute tasks, which needs no gateway).
#
# MAC_CHAT_GATEWAY_IMPL is set fleet-wide from the deploy-time gateway
# implementation, so it is also "openclaw" on pure workers that never install or
# serve the gateway.  A node only actually serves the gateway when its gateway
# artifacts are installed on disk: the verified service-advertisement.json AND the
# openclaw-agent binary.  When the impl advertises openclaw but those artifacts are
# absent, this node is a gateway-less worker and its OpenClaw readiness deficiency
# must be reported as degraded (non-blocking) instead of hard-crashing the worker.
# A node that HAS the gateway installed but broken still fails hard.
openclaw_required = os.environ.get("MAC_CHAT_GATEWAY_IMPL", "").strip().lower() == "openclaw"
openclaw_gateway_installed = resources_path.is_file() and openclaw_agent_bin.is_file()
openclaw_serves_gateway = openclaw_required and openclaw_gateway_installed
openclaw_problems: list[str] = []
# Persistent-but-transient shared-service timeouts (Qdrant/Firecrawl/hub) are
# recorded here so they degrade the node instead of blocking startup, mirroring
# the OpenClaw gateway-decoupling degraded pattern below.
transient_problems: list[str] = []


def add_openclaw_problem(message: str) -> None:
    problems.append(message)
    openclaw_problems.append(message)


# A non-zero / timed-out / sentinel-less openclaw-agent runtime probe is a soft,
# DEGRADED condition (runtime/service reachability), not a hard startup
# misconfiguration -- even on a node that actually serves the gateway.  These
# agent-probe problems are tracked here so they stay non-blocking everywhere,
# while hard misconfiguration problems (unreadable/missing/unverified
# advertisement, model config, mandatory-service misconfig, etc.) remain
# blocking on a gateway-serving node.
openclaw_agent_probe_problems: list[str] = []


def add_openclaw_agent_probe_problem(message: str) -> None:
    add_openclaw_problem(message)
    openclaw_agent_probe_problems.append(message)


if not openclaw_required:
    checks["openclaw_runtime"] = True
    checks["openclaw_agent"] = True
else:
    try:
        resources = json.loads(resources_path.read_text(encoding="utf-8"))
    except Exception as exc:
        resources = {}
        add_openclaw_problem(
            f"OpenClaw service advertisement unreadable at {resources_path}: {safe_error(exc)}"
        )

    runtime = resources.get("openclaw_runtime") if isinstance(resources, dict) else None
    ownership = resources.get("gateway_ownership") if isinstance(resources, dict) else None
    if not isinstance(runtime, dict) or runtime.get("implementation") != "openclaw":
        add_openclaw_problem("OpenClaw runtime advertisement is missing or has the wrong implementation")
    elif runtime.get("verified") is not True:
        add_openclaw_problem("OpenClaw runtime advertisement is not verified")
    elif runtime.get("exclusive_service_owner") is not True:
        add_openclaw_problem("OpenClaw runtime lacks exclusive service-ownership proof")
    elif not isinstance(runtime.get("confinement"), dict) or runtime["confinement"].get("provider") != "openshell":
        add_openclaw_problem("OpenClaw runtime is not advertised inside OpenShell")
    if not isinstance(ownership, dict) or ownership.get("exclusive") is not True:
        add_openclaw_problem("OpenClaw gateway ownership proof is missing")
    checks["openclaw_runtime"] = not any(
        problem.startswith("OpenClaw") for problem in problems
    )


if not truthy(qdrant_required_flag):
    problems.append("MAC_REQUIRE_QDRANT_MEMORY must be true")
if falsey(qdrant_disable_flag):
    problems.append("Qdrant shared memory is mandatory and cannot be disabled")
qdrant_headers = {"Accept": "application/json"}
if qdrant_key:
    qdrant_headers["api-key"] = qdrant_key
if qdrant_required and not qdrant_url:
    problems.append("QDRANT_URL is required but not configured")
elif qdrant_url:
    ok, error, timed_out = probe_http(qdrant_url, "/collections", qdrant_headers)
    if not ok:
        message = f"Qdrant shared memory endpoint is unreachable: {error}"
        problems.append(message)
        if timed_out:
            transient_problems.append(message)
    checks["qdrant_shared_memory"] = ok

if not truthy(firecrawl_required_flag):
    problems.append("MAC_REQUIRE_FIRECRAWL must be true")
firecrawl_headers = {"Accept": "application/json"}
if firecrawl_key and firecrawl_key.lower() != "none":
    firecrawl_headers["Authorization"] = f"Bearer {firecrawl_key}"
if firecrawl_required and not firecrawl_url:
    problems.append("FIRECRAWL_API_URL is required but not configured")
elif firecrawl_url:
    ok, error, timed_out = probe_http(firecrawl_url, "/health", firecrawl_headers)
    if not ok:
        message = f"Firecrawl web search endpoint is unreachable: {error}"
        problems.append(message)
        if timed_out:
            transient_problems.append(message)
    checks["firecrawl_web_search"] = ok

if openclaw_required:
    try:
        openclaw_config = json.loads(openclaw_config_path.read_text(encoding="utf-8"))
        provider = openclaw_config["models"]["providers"]["mac-router"]
        primary_model = openclaw_config["agents"]["defaults"]["model"]["primary"]
        runtime_provider = {
            "provider": "mac-router",
            "source": "openclaw_config",
            "model": str(primary_model).removeprefix("mac-router/"),
            "protocol": provider.get("api"),
        }
    except Exception as exc:
        runtime_provider = {"error": safe_error(exc)}
        add_openclaw_problem(f"OpenClaw model configuration is unreadable: {safe_error(exc)}")

    prompt = "Respond exactly MAC_OPENCLAW_STARTUP_OK"
    try:
        completed = subprocess.run(
            [
                str(openclaw_agent_bin),
                "--agent",
                "main",
                "--message",
                prompt,
                "--session-id",
                f"mac-openclaw-startup-self-test-{agent_id}-{int(time.time())}",
                "--json",
            ],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "MAC_AGENT_ID": agent_id},
        )
        agent_returncode = completed.returncode
        raw_agent_output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        agent_output = tail(raw_agent_output)
        if "MAC_OPENCLAW_STARTUP_OK" in raw_agent_output:
            # OpenClaw may report a non-zero CLI status after a gateway scope
            # upgrade request while successfully completing the model turn via
            # its embedded fallback runner.  The sentinel proves the execution
            # contract; only a missing sentinel is a hard self-test failure.
            checks["openclaw_agent"] = True
        elif completed.returncode != 0:
            openclaw_failure_class = classify_openclaw_agent_failure(agent_output)
            add_openclaw_agent_probe_problem(f"OpenClaw agent self-test exited {completed.returncode}")
        else:
            add_openclaw_agent_probe_problem("OpenClaw agent self-test did not return its sentinel")
            checks["openclaw_agent"] = False
    except subprocess.TimeoutExpired as exc:
        agent_returncode = None
        agent_output = tail(output_text(exc.stdout) + "\n" + output_text(exc.stderr))
        openclaw_failure_class = classify_openclaw_agent_failure(agent_output)
        add_openclaw_agent_probe_problem(f"OpenClaw agent self-test timed out after {timeout}s")
    except Exception as exc:
        agent_returncode = None
        add_openclaw_agent_probe_problem(f"OpenClaw agent self-test failed to execute: {safe_error(exc)}")

# A gateway-less worker (impl advertises openclaw but the gateway artifacts are
# not installed on this node) must not hard-crash on OpenClaw readiness gaps: the
# worker/gateway decoupling contract says such a node can still claim and execute
# tasks.  Its OpenClaw problems are therefore non-blocking (degraded) while every
# other problem — and any OpenClaw failure on a node that actually serves the
# gateway — stays blocking.
if openclaw_serves_gateway:
    # A gateway-serving node keeps hard OpenClaw misconfiguration problems
    # blocking, but a runtime/service-reachability failure of the openclaw-agent
    # probe is degraded (soft), so those agent-probe problems stay non-blocking.
    non_blocking_problems: list[str] = list(openclaw_agent_probe_problems)
else:
    non_blocking_problems = list(openclaw_problems)
# A shared-service (or hub) probe that only ever timed out is a transient hub
# blip after bounded retries, so it degrades the node instead of blocking start.
for problem in transient_problems:
    if problem not in non_blocking_problems:
        non_blocking_problems.append(problem)
blocking_problems = [problem for problem in problems if problem not in non_blocking_problems]
status = "passed"
if blocking_problems:
    status = "failed"
elif problems:
    status = "degraded"

report = {
    "schema": "mac.agent_startup_self_test.v1",
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "status": status,
    "agent_name": agent_name,
    "agent_id": agent_id,
    "hermes_instance_id": hermes_instance,
    "persona_id": persona_id,
    "tenant_id": tenant_id,
    "checks": checks,
    "report_repository_executor_attestation": report_executor_attestation,
    "mandatory_services": {
        "qdrant_shared_memory": {
            "required": qdrant_required,
            "required_flag": "MAC_REQUIRE_QDRANT_MEMORY",
            "required_flag_true": truthy(qdrant_required_flag),
            "disabled_by_env": falsey(qdrant_disable_flag),
            "url_configured": bool(qdrant_url),
        },
        "firecrawl_web_search": {
            "required": firecrawl_required,
            "required_flag": "MAC_REQUIRE_FIRECRAWL",
            "required_flag_true": truthy(firecrawl_required_flag),
            "url_configured": bool(firecrawl_url),
        },
    },
    "runtime_provider": runtime_provider,
    "agent_returncode": agent_returncode,
    "agent_output_tail": agent_output,
    "openclaw_failure_class": openclaw_failure_class,
    "openclaw_gateway": {
        "impl_advertised": openclaw_required,
        "installed": openclaw_gateway_installed,
        "serves_gateway": openclaw_serves_gateway,
    },
    "problems": problems,
    "blocking_problems": blocking_problems,
    "non_blocking_problems": non_blocking_problems,
    "transient_problems": transient_problems,
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

# Registration follows this self-test in mac-agent-service. Persist the report
# into the exact resources document that registration consumes so a brand-new
# agent cannot lose a degraded verdict to the expected pre-registration 404.
# Existing OpenClaw runtime/ownership advertisements are preserved.
resources_path.parent.mkdir(parents=True, exist_ok=True)
try:
    registration_resources = (
        json.loads(resources_path.read_text(encoding="utf-8"))
        if resources_path.exists()
        else {}
    )
    if not isinstance(registration_resources, dict):
        raise ValueError("worker resources document is not an object")
    registration_resources["startup_self_test"] = report
    fd, raw = tempfile.mkstemp(prefix=resources_path.name + ".", dir=str(resources_path.parent))
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(registration_resources, stream, indent=2, sort_keys=True)
            stream.write("\n")
        tmp.chmod(0o600)
        os.replace(tmp, resources_path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
except Exception as exc:
    print(
        f"agent startup self-test: cannot persist registration health: {safe_error(exc)}",
        file=sys.stderr,
    )
    raise SystemExit(1)

hub_url = str(os.environ.get("MAC_HUB_URL") or "").rstrip("/")
token = os.environ.get("MAC_WORKER_TOKEN") or ""
if hub_url and token and agent_id:
    payload = {"resources": {"startup_self_test": report}}
    if blocking_problems:
        payload.update({"status": "offline", "health_status": "degraded"})
    req = urllib.request.Request(
        f"{hub_url}/agents/{agent_id}/heartbeat",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except (OSError, urllib.error.URLError) as exc:
        # HTTPError (e.g. HTTP 400) is a URLError subclass, so a rejected heartbeat
        # is logged but never propagates or changes the self-test exit code: only
        # blocking_problems decide whether the worker starts.
        print(f"agent startup self-test: failed to report heartbeat: {safe_error(exc)}", file=sys.stderr)

if problems:
    print(f"agent startup self-test: {status}; report={report_path}", file=sys.stderr)
    for problem in problems:
        print(f"agent startup self-test: {problem}", file=sys.stderr)
    sys.exit(1 if blocking_problems else 0)

print(f"agent startup self-test: passed; report={report_path}")
PY
EOF
  chmod 700 "$selftest"

  cat > "$executor" <<'EOF'
#!/bin/bash
set -euo pipefail
: "${MAC_TASK_EXECUTOR_PYTHON:?hub-approved executor Python is required}"
: "${MAC_TASK_EXECUTOR_SCRIPT:?hub-approved executor script is required}"
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE
export PYTHONNOUSERSITE=1
exec "$MAC_TASK_EXECUTOR_PYTHON" "$MAC_TASK_EXECUTOR_SCRIPT"
EOF
  chmod 700 "$executor"

cat > "$executor_py" <<'PY'
# Autonomous task executor shim. The real, unit-tested logic lives in the
# mac.task_executor module (extracted from this heredoc per loop-01): it
# builds the prompt, runs a verified coding agent inside OpenShell, writes deterministic
# evidence, emits executor telemetry, and feeds deployment lessons into
# memory so the fleet gets smarter over time.
from mac.task_executor import main

raise SystemExit(main())
PY
  chmod 600 "$executor_py"
  # Compatibility only for explicit pre-migration MAC_WORKER_EXECUTOR values.
  # The target contains no Hermes runtime path and may be removed after config
  # migration has converged fleet-wide.
  if truthy "$install_aliases"; then
    ln -sf "$executor" "$MAC_HOME/bin/mac-hermes-task-executor"
    ln -sf "$executor_py" "$MAC_HOME/bin/mac-hermes-task-executor.py"
  fi
}

install_linux_hermes_service() {
  local unit="/etc/systemd/system/${HERMES_SERVICE_NAME}" restart_since control_after=""
  local unit_staging="$LOG_DIR/${HERMES_SERVICE_NAME}.${DEPLOY_TS}.$$.stage"
  disable_systemd_service_if_present "$OPENCLAW_SERVICE_NAME"
  disable_systemd_service_if_present "$NEMOCLAW_SERVICE_NAME"
  if control_plane_enabled; then
    control_after="$MAC_SERVICE_NAME"
  fi
  log "installing systemd service $unit"
  if sudo test -f "$unit"; then
    HERMES_UNIT_BACKUP="$MAC_HOME/backups/${HERMES_SERVICE_NAME}.${AGENT}.${DEPLOY_TS}"
    snapshot_rollback_file "$unit" "$HERMES_UNIT_BACKUP" system
    write_rollback_script
  fi
  if [ "$DEPLOY_ROLLBACK_ARMED" = 1 ] && [ -z "$HERMES_UNIT_BACKUP" ]; then
    die "cannot mutate the Hermes unit without a prior-generation backup"
  fi
  HERMES_UNIT_MUTATED=1
  write_rollback_script
  cat > "$unit_staging" <<EOF
[Unit]
Description=mac-managed Hermes gateway
After=network-online.target $control_after
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$USER
WorkingDirectory=$MAC_HOME
EnvironmentFile=$ENV_FILE
ExecStart=$MAC_HOME/bin/hermes-gateway
Restart=always
RestartSec=5
RestartForceExitStatus=75
SuccessExitStatus=75
KillMode=mixed
KillSignal=SIGTERM
ExecReload=/bin/kill -USR1 \$MAINPID
# Must exceed the gateway's restart_drain_timeout so systemd doesn't SIGKILL it
# mid-drain. Mirrors hermes_cli/gateway.py: max(60, restart_drain_timeout) + 30
# (=210 for the default drain of 180). A too-low value triggers the gateway's
# "Stale systemd unit detected" startup warning. Bump if restart_drain_timeout
# is raised above 180.
TimeoutStopSec=210
LimitNOFILE=65536
LimitCORE=infinity
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  mac_launchd_atomic_replace "$unit_staging" "$unit" system 0644 0 0
  run_systemctl daemon-reload
  run_systemctl enable "$HERMES_SERVICE_NAME"
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  run_systemctl restart "$HERMES_SERVICE_NAME"
  run_systemctl --no-pager -l status "$HERMES_SERVICE_NAME" \
    > "$LOG_DIR/hermes-gateway-status.txt" || true
  run_journalctl -u "$HERMES_SERVICE_NAME" --since "$restart_since" --no-pager \
    > "$LOG_DIR/hermes-gateway-journal.txt" || true
  install_linux_agent_service
}

install_linux_agent_service() {
  local unit="/etc/systemd/system/${MAC_AGENT_SERVICE_NAME}" restart_since control_after=""
  local unit_staging="$LOG_DIR/${MAC_AGENT_SERVICE_NAME}.${DEPLOY_TS}.$$.stage"
  if control_plane_enabled; then
    control_after="$MAC_SERVICE_NAME"
  fi
  log "installing systemd service $unit"
  if sudo test -f "$unit"; then
    MAC_AGENT_UNIT_BACKUP="$MAC_HOME/backups/${MAC_AGENT_SERVICE_NAME}.${AGENT}.${DEPLOY_TS}"
    snapshot_rollback_file "$unit" "$MAC_AGENT_UNIT_BACKUP" system
    write_rollback_script
  fi
  if [ "$DEPLOY_ROLLBACK_ARMED" = 1 ] && [ -z "$MAC_AGENT_UNIT_BACKUP" ]; then
    die "cannot mutate the agent unit without a prior-generation backup"
  fi
  MAC_AGENT_UNIT_MUTATED=1
  write_rollback_script
  cat > "$unit_staging" <<EOF
[Unit]
Description=mac worker agent registration loop
After=network-online.target $control_after
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$USER
WorkingDirectory=$MAC_HOME
EnvironmentFile=$ENV_FILE
ExecStart=$MAC_HOME/bin/mac-crash-observer --supervisor systemd -- $MAC_HOME/bin/mac-agent-service
Restart=always
RestartSec=5
TimeoutStopSec=30
LimitNOFILE=65536
LimitCORE=infinity
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  mac_launchd_atomic_replace "$unit_staging" "$unit" system 0644 0 0
  run_systemctl daemon-reload
  run_systemctl enable "$MAC_AGENT_SERVICE_NAME"
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if truthy "$DEFER_AGENT_RESTART"; then
    log "deferring mac-agent restart until post-manifest reconciliation"
  else
    run_systemctl restart "$MAC_AGENT_SERVICE_NAME"
    run_systemctl show "$MAC_AGENT_SERVICE_NAME" \
      -p LoadState \
      -p ActiveState \
      -p SubState \
      -p UnitFileState \
      -p MainPID \
      -p NRestarts > "$LOG_DIR/mac-agent-systemd-state.txt"
    run_journalctl -u "$MAC_AGENT_SERVICE_NAME" --since "$restart_since" --no-pager \
      > "$LOG_DIR/mac-agent-journal.txt" || true
  fi
}


install_supervisord_service() {
  local conf_dir conf restart_since control_program="" gateway_program active_gateway_program
  local conf_staging
  local agent_autostart=true
  if truthy "$DEFER_AGENT_RESTART"; then
    agent_autostart=false
  fi
  conf_dir="$(supervisord_conf_dir)"
  conf="$conf_dir/$MAC_SUPERVISORD_CONF_NAME"
  conf_staging="$LOG_DIR/${MAC_SUPERVISORD_CONF_NAME}.${DEPLOY_TS}.$$.stage"
  log "installing supervisord programs in $conf"
  install_hermes_gateway_wrapper
  install_mac_agent_wrapper
  install -m 0755 "$SRC_DIR/deploy/agent-resource-health.sh" "$MAC_HOME/bin/agent-resource-health"
  case "${HERMES_GATEWAY_IMPL:-hermes}" in
  openclaw)
    active_gateway_program="$OPENCLAW_SUPERVISORD_PROG"
    gateway_program="[program:$HERMES_SUPERVISORD_PROG]
command=$MAC_HOME/bin/hermes-gateway
directory=$MAC_HOME
user=$USER
autostart=false
autorestart=false
startsecs=5
stopwaitsecs=120
stdout_logfile=$LOG_DIR/hermes-gateway.log
stderr_logfile=$LOG_DIR/hermes-gateway.log
environment=HOME=\"$HOME\"

[program:$OPENCLAW_SUPERVISORD_PROG]
command=$MAC_HOME/bin/openclaw-gateway
directory=$MAC_HOME
user=$USER
autostart=true
autorestart=true
startsecs=8
stopwaitsecs=90
stdout_logfile=$LOG_DIR/openclaw-gateway.log
stderr_logfile=$LOG_DIR/openclaw-gateway.log
environment=HOME=\"$HOME\""
    ;;
  none)
    # A pure worker must not retain or start either chat-gateway program.  An
    # empty block also lets ``supervisorctl update`` remove stale gateway
    # programs from a node that was converted from a conversational role.
    active_gateway_program=""
    gateway_program=""
    ;;
  hermes|"")
    active_gateway_program="$HERMES_SUPERVISORD_PROG"
    gateway_program="[program:$HERMES_SUPERVISORD_PROG]
command=$MAC_HOME/bin/hermes-gateway
directory=$MAC_HOME
user=$USER
autostart=true
autorestart=true
startsecs=5
stopwaitsecs=120
stdout_logfile=$LOG_DIR/hermes-gateway.log
stderr_logfile=$LOG_DIR/hermes-gateway.log
environment=HOME=\"$HOME\""
    ;;
  *) die "unsupported supervisord gateway implementation: ${HERMES_GATEWAY_IMPL}" ;;
  esac
  if control_plane_enabled; then
    install_mac_control_wrapper
    control_program="[program:$MAC_SUPERVISORD_PROG]
command=$MAC_HOME/bin/mac-service
directory=$MAC_HOME
user=$USER
autostart=true
autorestart=true
startsecs=3
stopwaitsecs=20
stdout_logfile=$LOG_DIR/mac-service.log
stderr_logfile=$LOG_DIR/mac-service.log
environment=HOME=\"$HOME\""
  fi
  if sudo test -f "$conf"; then
    MAC_UNIT_BACKUP="$MAC_HOME/backups/${MAC_SUPERVISORD_CONF_NAME}.${AGENT}.${DEPLOY_TS}"
    snapshot_rollback_file "$conf" "$MAC_UNIT_BACKUP" system
    write_rollback_script
  fi
  if [ "$DEPLOY_ROLLBACK_ARMED" = 1 ] && [ -z "$MAC_UNIT_BACKUP" ]; then
    die "cannot mutate the supervisord configuration without a prior-generation backup"
  fi
  MAC_UNIT_MUTATED=1
  write_rollback_script
  run_privileged_bounded \
    "${MAC_SUPERVISOR_COMMAND_TIMEOUT_SECONDS:-30}" \
    install -d -m 0755 "$conf_dir"
  cat > "$conf_staging" <<EOF
$control_program

$gateway_program

[program:${AGENT_SUPERVISORD_PROG}-resource-health]
command=$MAC_HOME/bin/agent-resource-health --loop
directory=$MAC_HOME
user=$USER
autostart=true
autorestart=true
startsecs=2
stopwaitsecs=10
stdout_logfile=$LOG_DIR/resource-health.log
stderr_logfile=$LOG_DIR/resource-health.log
environment=HOME="$HOME",MAC_HOME="$MAC_HOME",MAC_RESOURCE_HEALTH_INTERVAL_SECONDS="300"

[program:$AGENT_SUPERVISORD_PROG]
command=$MAC_HOME/bin/mac-crash-observer --supervisor supervisord -- $MAC_HOME/bin/mac-agent-service
directory=$MAC_HOME
user=$USER
autostart=$agent_autostart
autorestart=true
startsecs=3
stopwaitsecs=30
stdout_logfile=$LOG_DIR/mac-agent.log
stderr_logfile=$LOG_DIR/mac-agent.log
environment=HOME="$HOME"
EOF
  mac_launchd_atomic_replace "$conf_staging" "$conf" system 0644 0 0
  # Remove stale worker-side hub tunnel conf from previous deploy approach
  mac_launchd_remove_file_and_fsync \
    "$conf_dir/${FLEET_NAME}-hub-tunnel.conf" system
  # Truncate both possible gateway logs so classify_gateway_logs only sees
  # output from this deploy.  OpenClaw and Hermes share the same classifier;
  # leaving the inactive implementation's selected log cumulative can make a
  # historical traceback fail every otherwise healthy future deployment.
  local gateway_log
  for gateway_log in "$LOG_DIR/hermes-gateway.log" "$LOG_DIR/openclaw-gateway.log"; do
    sudo truncate -s 0 "$gateway_log" 2>/dev/null || : > "$gateway_log" 2>/dev/null || true
  done
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  run_supervisorctl reread >/dev/null
  run_supervisorctl update >/dev/null
  if truthy "$DEFER_AGENT_RESTART"; then
    # ``update`` starts newly added autostart programs.  Keep the agent down
    # until the outer deploy controller has reconciled the post manifest.
    stop_supervisord_program_if_present "$AGENT_SUPERVISORD_PROG"
  fi
  if control_plane_enabled; then
    start_supervisord_program "$MAC_SUPERVISORD_PROG"
    wait_for_local_control_plane_health
  else
    log "spoke role: supervisord control-plane program is absent"
    : > "$LOG_DIR/mac-service-not-installed.txt"
  fi
  # Escrow the router upstream key + scrub spoke secrets + sync messaging BEFORE
  # the gateway/agent start, mirroring the systemd (install_linux_hermes_service)
  # and launchd paths. Without this the supervisord path left the hub vault empty,
  # so the router forwarded keyless (upstream 401) and the agent self-test failed.
  # Needs the control plane reachable, so wait briefly for it.
  if [ "$NODE_ACTION" = legacy-one-shot ] && control_plane_enabled; then
    escrow_router_provider_keys
  fi
  if [ "$NODE_ACTION" = legacy-one-shot ]; then
    scrub_spoke_provider_secrets
    sync_messaging_config
  else
    log "typed phase 2 retained the hub vault and Hermes credential projection"
  fi
  if [ -n "$active_gateway_program" ]; then
    if [ "${HERMES_GATEWAY_IMPL:-hermes}" = "openclaw" ]; then
      prepare_openclaw_gateway
    fi
    start_supervisord_program "$active_gateway_program"
  else
    log "gateway_impl=none: pure worker; skipping gateway program install/restart"
  fi
  if [ "${HERMES_GATEWAY_IMPL:-hermes}" = "openclaw" ]; then
    if ! verify_openclaw_gateway; then
      log "ERROR: stock OpenClaw verification failed under supervisord; withdrawing the successor before generation rollback"
      if ! withdraw_openclaw_gateway; then
        log "ERROR: OpenClaw withdrawal failed under supervisord; generation rollback stays armed"
      fi
      return 1
    fi
    stop_supervisord_program_if_present "$HERMES_SUPERVISORD_PROG"
    if ! finalize_openclaw_gateway; then
      log "ERROR: OpenClaw exclusivity proof failed under supervisord; withdrawing the successor before generation rollback"
      if ! withdraw_openclaw_gateway; then
        log "ERROR: OpenClaw withdrawal failed under supervisord; generation rollback stays armed"
      fi
      return 1
    fi
  fi
  if truthy "$DEFER_AGENT_RESTART"; then
    log "deferring mac-agent restart until post-manifest reconciliation"
    # An intentionally STOPPED supervisord program makes ``status`` return 3.
    # Do not feed that expected state through the privilege fallback below.
    # Keep executing the remote deployment, however: the post manifest must be
    # durable before the outer controller owns the subsequent restart/readback.
    printf '%s STOPPED (restart deferred until post-manifest reconciliation)\n' \
      "$AGENT_SUPERVISORD_PROG" > "$LOG_DIR/supervisord-services.txt"
  else
    start_supervisord_program "$AGENT_SUPERVISORD_PROG"
    if control_plane_enabled; then
      if [ -n "$active_gateway_program" ]; then
        run_supervisorctl status "$MAC_SUPERVISORD_PROG" "$active_gateway_program" "$AGENT_SUPERVISORD_PROG" > "$LOG_DIR/supervisord-services.txt" || true
      else
        run_supervisorctl status "$MAC_SUPERVISORD_PROG" "$AGENT_SUPERVISORD_PROG" > "$LOG_DIR/supervisord-services.txt" || true
      fi
    else
      if [ -n "$active_gateway_program" ]; then
        run_supervisorctl status "$active_gateway_program" "$AGENT_SUPERVISORD_PROG" > "$LOG_DIR/supervisord-services.txt" || true
      else
        run_supervisorctl status "$AGENT_SUPERVISORD_PROG" > "$LOG_DIR/supervisord-services.txt" || true
      fi
    fi
  fi
  printf 'supervisord restarted at %s\n' "$restart_since" >> "$LOG_DIR/supervisord-services.txt"
}

DARWIN_AUX_RESTORE_DOMAIN=""
DARWIN_AUX_RESTORE_PLIST=""
DARWIN_AUX_RESTORE_TARGET=""
DARWIN_AUX_RESTORE_LABEL=""
DARWIN_AUX_RESTORE_MODE="user"

darwin_expected_prior_state() {
  case "${1:-0}" in
    1) printf '%s\n' active ;;
    0) printf '%s\n' inactive ;;
    *) die "invalid captured launchd prior-state flag" ;;
  esac
}

darwin_disable_job() {
  local target="$1" label="$2" mode="${3:-user}" output="" rc=0
  output="$(mac_launchd_run_control_bounded \
    "$mode" "${MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS:-10}" \
    disable "$target" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "ERROR: could not disable launchd job $label (exit $rc)"
    return 1
  fi
}

darwin_restore_auxiliary_job() {
  [ -n "$DARWIN_AUX_RESTORE_TARGET" ] || return 0
  mac_launchd_bootstrap_job \
    "$DARWIN_AUX_RESTORE_DOMAIN" \
    "$DARWIN_AUX_RESTORE_PLIST" \
    "$DARWIN_AUX_RESTORE_TARGET" \
    "$DARWIN_AUX_RESTORE_LABEL" \
    "$DARWIN_AUX_RESTORE_MODE"
}

darwin_clear_auxiliary_restore() {
  DARWIN_AUX_RESTORE_DOMAIN=""
  DARWIN_AUX_RESTORE_PLIST=""
  DARWIN_AUX_RESTORE_TARGET=""
  DARWIN_AUX_RESTORE_LABEL=""
  DARWIN_AUX_RESTORE_MODE="user"
}

darwin_set_auxiliary_restore() {
  DARWIN_AUX_RESTORE_DOMAIN="$1"
  DARWIN_AUX_RESTORE_PLIST="$2"
  DARWIN_AUX_RESTORE_TARGET="$3"
  DARWIN_AUX_RESTORE_LABEL="$4"
  DARWIN_AUX_RESTORE_MODE="${5:-user}"
  mac_launchd_transaction_set_after_restore_hook darwin_restore_auxiliary_job
}

darwin_track_and_remove_plist() {
  local plist="$1" target="$2" label="$3" mode="${4:-user}"
  mac_launchd_transaction_track_file "$plist"
  mac_launchd_stop_job_if_present "$target" "$label" "$mode"
  darwin_disable_job "$target" "$label" "$mode"
  mac_launchd_remove_file_and_fsync "$plist" "$MAC_LAUNCHD_TX_MODE"
}

install_darwin_service() {
  local uid plist wrapper wrapper_staging system_plist system_plist_staging
  local system_supervisor_plist expected_state
  uid="$(id -u)"
  plist="$HOME/Library/LaunchAgents/${MAC_LAUNCHD_LABEL}.plist"
  wrapper="$MAC_HOME/bin/mac-service"
  wrapper_staging="$MAC_HOME/bin/.mac-service.${DEPLOY_TS}.$$.stage"
  system_plist="/Library/LaunchDaemons/${MAC_LAUNCHD_LABEL}.plist"
  system_plist_staging="$LOG_DIR/${MAC_LAUNCHD_LABEL}.${DEPLOY_TS}.system.plist"
  system_supervisor_plist="/Library/LaunchDaemons/${DARWIN_SYSTEM_SUPERVISOR_LABEL}.plist"
  mkdir -p "$MAC_HOME/bin" "$HOME/Library/LaunchAgents"
  if control_plane_enabled; then
    log "installing headless system launchd control plane $MAC_LAUNCHD_LABEL"
    DARWIN_SYSTEM_PLIST_MUTATED=1
    if [ -f "$plist" ]; then
      MAC_PLIST_MUTATED=1
    fi
    write_rollback_script
    darwin_clear_auxiliary_restore
    mac_launchd_transaction_begin \
      system "$system_plist" "system/$MAC_LAUNCHD_LABEL" \
      "$MAC_LAUNCHD_LABEL" system
    expected_state="$(darwin_expected_prior_state "$DARWIN_SYSTEM_LAUNCHD_ACTIVE")"
    mac_launchd_transaction_set_expected_prior_state "$expected_state"
    mac_launchd_transaction_track_file "$wrapper"
    mac_launchd_transaction_track_file "$plist"
    mac_launchd_transaction_track_temporary "$wrapper_staging"
    mac_launchd_transaction_track_temporary "$system_plist_staging"
    if [ "$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ]; then
      darwin_set_auxiliary_restore \
        "gui/$uid" "$plist" "gui/$uid/$MAC_LAUNCHD_LABEL" \
        "$MAC_LAUNCHD_LABEL" user
    elif [ "$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" = 1 ]; then
      darwin_set_auxiliary_restore \
        system "$system_supervisor_plist" \
        "system/$DARWIN_SYSTEM_SUPERVISOR_LABEL" \
        "$DARWIN_SYSTEM_SUPERVISOR_LABEL" system
    fi
    install_mac_control_wrapper "$wrapper_staging"
    cat > "$system_plist_staging" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$MAC_LAUNCHD_LABEL</string>
  <key>UserName</key><string>$(id -un)</string>
  <key>GroupName</key><string>$(id -gn)</string>
  <key>EnvironmentVariables</key>
  <dict><key>HOME</key><string>$HOME</string></dict>
  <key>ProgramArguments</key>
  <array><string>$wrapper</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>WorkingDirectory</key><string>$MAC_HOME</string>
  <key>StandardOutPath</key><string>$LOG_DIR/mac-service.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/mac-service.log</string>
</dict>
</plist>
EOF
    /bin/bash -n "$wrapper_staging"
    if command -v plutil >/dev/null 2>&1; then
      plutil -lint "$system_plist_staging"
    fi
    mac_launchd_transaction_mark_mutating
    mac_launchd_stop_job_if_present \
      "system/$MAC_LAUNCHD_LABEL" "$MAC_LAUNCHD_LABEL" system
    mac_launchd_stop_job_if_present \
      "gui/$uid/$MAC_LAUNCHD_LABEL" "$MAC_LAUNCHD_LABEL" user
    darwin_disable_job "gui/$uid/$MAC_LAUNCHD_LABEL" "$MAC_LAUNCHD_LABEL" user
    mac_launchd_remove_file_and_fsync "$plist" system
    mac_launchd_transaction_replace \
      "$wrapper_staging" "$wrapper" 0700 "$(id -u)" "$(id -g)"
    mac_launchd_transaction_replace \
      "$system_plist_staging" "$system_plist" 0644 0 0
    : > "$LOG_DIR/mac-service.log"
    mac_launchd_bootstrap_job \
      system "$system_plist" "system/$MAC_LAUNCHD_LABEL" \
      "$MAC_LAUNCHD_LABEL" system
    wait_for_local_control_plane_health
    if [ "$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" = 1 ]; then
      if ! mac_run_bounded "${MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS:-10}" \
        sudo -n cmp -s \
        "$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP" "$system_supervisor_plist"; then
        log "ERROR: system supervisor plist changed during deployment"
        die "system supervisor plist changed during deployment"
      fi
      mac_launchd_bootstrap_job \
        system "$system_supervisor_plist" \
        "system/$DARWIN_SYSTEM_SUPERVISOR_LABEL" \
        "$DARWIN_SYSTEM_SUPERVISOR_LABEL" system
      wait_for_local_control_plane_health
    fi
    mac_launchd_transaction_commit
    darwin_clear_auxiliary_restore
    if [ "$NODE_ACTION" = legacy-one-shot ]; then
      escrow_router_provider_keys
    fi
  else
    log "spoke role: removing stale local control-plane launchd service"
    stop_gui_launchd_job_if_present "$uid" "$MAC_LAUNCHD_LABEL"
    stop_system_launchd_job_if_present "$DARWIN_SYSTEM_SUPERVISOR_LABEL"
    stop_system_launchd_job_if_present "$MAC_LAUNCHD_LABEL"
    darwin_disable_job \
      "system/$DARWIN_SYSTEM_SUPERVISOR_LABEL" \
      "$DARWIN_SYSTEM_SUPERVISOR_LABEL" system
    darwin_disable_job "system/$MAC_LAUNCHD_LABEL" "$MAC_LAUNCHD_LABEL" system
    darwin_disable_job "gui/$uid/$MAC_LAUNCHD_LABEL" "$MAC_LAUNCHD_LABEL" user
    if sudo -n test -f "$system_supervisor_plist"; then
      DARWIN_SYSTEM_SUPERVISOR_PLIST_MUTATED=1
    fi
    if sudo -n test -f "$system_plist"; then
      DARWIN_SYSTEM_PLIST_MUTATED=1
    fi
    if [ -f "$plist" ]; then
      MAC_PLIST_MUTATED=1
    fi
    write_rollback_script
    [ "$DARWIN_SYSTEM_SUPERVISOR_PLIST_MUTATED" != 1 ] \
      || mac_launchd_remove_file_and_fsync "$system_supervisor_plist" system
    [ "$DARWIN_SYSTEM_PLIST_MUTATED" != 1 ] \
      || mac_launchd_remove_file_and_fsync "$system_plist" system
    [ "$MAC_PLIST_MUTATED" != 1 ] \
      || mac_launchd_remove_file_and_fsync "$plist" user
    mac_launchd_remove_file_and_fsync "$wrapper" user
    : > "$LOG_DIR/mac-service-not-installed.txt"
  fi
  if [ "$NODE_ACTION" = legacy-one-shot ]; then
    scrub_spoke_provider_secrets
    sync_messaging_config
  else
    log "typed phase 2 retained the hub vault and Hermes credential projection"
  fi
  case "${HERMES_GATEWAY_IMPL:-hermes}" in
  openclaw)
    install_darwin_openclaw_service "$uid"
    ;;
  hermes|"")
    install_darwin_hermes_service "$uid"
    ;;
  none)
    install_darwin_no_gateway_service "$uid"
    ;;
  *) die "unsupported launchd gateway implementation: ${HERMES_GATEWAY_IMPL}" ;;
  esac
  install_darwin_agent_service "$uid"
}

install_darwin_no_gateway_service() {
  local uid="$1" label plist
  local primary_label="$HERMES_LAUNCHD_LABEL"
  local primary_active="$DARWIN_HERMES_LAUNCHD_ACTIVE"
  if [ "$DARWIN_OPENCLAW_LAUNCHD_ACTIVE" = 1 ]; then
    primary_label="$OPENCLAW_LAUNCHD_LABEL"
    primary_active=1
  elif [ "$DARWIN_NEMOCLAW_LAUNCHD_ACTIVE" = 1 ]; then
    primary_label="$NEMOCLAW_LAUNCHD_LABEL"
    primary_active=1
  fi
  plist="$HOME/Library/LaunchAgents/${primary_label}.plist"
  log "gateway_impl=none: proving every launchd gateway absent and disabled"
  if [ -f "$HOME/Library/LaunchAgents/${HERMES_LAUNCHD_LABEL}.plist" ] \
    && [ -z "$HERMES_PLIST_BACKUP" ]; then
    HERMES_PLIST_BACKUP="$MAC_HOME/backups/${HERMES_LAUNCHD_LABEL}.${AGENT}.${DEPLOY_TS}.plist"
    snapshot_rollback_file \
      "$HOME/Library/LaunchAgents/${HERMES_LAUNCHD_LABEL}.plist" \
      "$HERMES_PLIST_BACKUP" user
  fi
  if [ "$DEPLOY_ROLLBACK_ARMED" = 1 ] && [ -z "$HERMES_PLIST_BACKUP" ]; then
    die "cannot remove launchd gateways without the Hermes rollback plist"
  fi
  HERMES_PLIST_MUTATED=1
  write_rollback_script
  darwin_clear_auxiliary_restore
  mac_launchd_transaction_begin \
    "gui/$uid" "$plist" "gui/$uid/$primary_label" \
    "$primary_label" user
  mac_launchd_transaction_set_expected_prior_state \
    "$(darwin_expected_prior_state "$primary_active")"
  for label in \
    "$OPENCLAW_LAUNCHD_LABEL" \
    "$HERMES_LAUNCHD_LABEL" \
    "$NEMOCLAW_LAUNCHD_LABEL"; do
    mac_launchd_transaction_track_file \
      "$HOME/Library/LaunchAgents/${label}.plist"
  done
  mac_launchd_transaction_track_file "$MAC_HOME/bin/openclaw-gateway"
  mac_launchd_transaction_track_file "$MAC_HOME/bin/hermes-gateway"
  mac_launchd_transaction_mark_mutating
  for label in "$OPENCLAW_LAUNCHD_LABEL" "$HERMES_LAUNCHD_LABEL" "$NEMOCLAW_LAUNCHD_LABEL"; do
    stop_gui_launchd_job_if_present "$uid" "$label"
    darwin_disable_job "gui/$uid/$label" "$label" user
    mac_launchd_remove_file_and_fsync \
      "$HOME/Library/LaunchAgents/${label}.plist" user
  done
  mac_launchd_remove_file_and_fsync "$MAC_HOME/bin/openclaw-gateway" user
  mac_launchd_remove_file_and_fsync "$MAC_HOME/bin/hermes-gateway" user
  verify_selected_gateway_supervisor_health
  mac_launchd_transaction_commit
}

install_darwin_openclaw_service() {
  local uid="$1" plist="$HOME/Library/LaunchAgents/${OPENCLAW_LAUNCHD_LABEL}.plist"
  local plist_staging="$HOME/Library/LaunchAgents/.${OPENCLAW_LAUNCHD_LABEL}.${DEPLOY_TS}.$$.stage"
  local hermes_plist="$HOME/Library/LaunchAgents/${HERMES_LAUNCHD_LABEL}.plist"
  local nemoclaw_plist="$HOME/Library/LaunchAgents/${NEMOCLAW_LAUNCHD_LABEL}.plist"
  local openclaw_wrapper="$MAC_HOME/bin/openclaw-gateway"
  local hermes_wrapper="$MAC_HOME/bin/hermes-gateway"
  local hermes_wrapper_staging="$MAC_HOME/bin/.hermes-gateway.${DEPLOY_TS}.$$.stage"
  darwin_clear_auxiliary_restore
  mac_launchd_transaction_begin \
    "gui/$uid" "$plist" "gui/$uid/$OPENCLAW_LAUNCHD_LABEL" \
    "$OPENCLAW_LAUNCHD_LABEL" user
  mac_launchd_transaction_set_expected_prior_state \
    "$(darwin_expected_prior_state "$DARWIN_OPENCLAW_LAUNCHD_ACTIVE")"
  mac_launchd_transaction_track_file "$openclaw_wrapper"
  mac_launchd_transaction_track_file "$hermes_wrapper"
  mac_launchd_transaction_track_file "$hermes_plist"
  mac_launchd_transaction_track_file "$nemoclaw_plist"
  mac_launchd_transaction_track_temporary "$plist_staging"
  mac_launchd_transaction_track_temporary "$hermes_wrapper_staging"
  if [ "$DARWIN_HERMES_LAUNCHD_ACTIVE" = 1 ]; then
    darwin_set_auxiliary_restore \
      "gui/$uid" "$hermes_plist" "gui/$uid/$HERMES_LAUNCHD_LABEL" \
      "$HERMES_LAUNCHD_LABEL" user
  elif [ "$DARWIN_NEMOCLAW_LAUNCHD_ACTIVE" = 1 ]; then
    darwin_set_auxiliary_restore \
      "gui/$uid" "$nemoclaw_plist" "gui/$uid/$NEMOCLAW_LAUNCHD_LABEL" \
      "$NEMOCLAW_LAUNCHD_LABEL" user
  fi
  mac_launchd_transaction_set_rollback_hook withdraw_openclaw_gateway
  install_hermes_gateway_wrapper "$hermes_wrapper_staging"
  cat > "$plist_staging" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$OPENCLAW_LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$MAC_HOME/bin/openclaw-gateway</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$MAC_HOME</string>
  <key>StandardOutPath</key><string>$LOG_DIR/openclaw-gateway.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/openclaw-gateway.log</string>
</dict>
</plist>
EOF
  plutil -lint "$plist_staging"
  /bin/bash -n "$hermes_wrapper_staging"
  mac_launchd_transaction_mark_mutating
  prepare_openclaw_gateway
  stop_gui_launchd_job_if_present "$uid" "$OPENCLAW_LAUNCHD_LABEL"
  mac_launchd_transaction_replace \
    "$hermes_wrapper_staging" "$hermes_wrapper" 0700
  mac_launchd_transaction_replace "$plist_staging" "$plist" 0644
  : > "$LOG_DIR/openclaw-gateway.log"
  mac_launchd_bootstrap_job \
    "gui/$uid" "$plist" "gui/$uid/$OPENCLAW_LAUNCHD_LABEL" \
    "$OPENCLAW_LAUNCHD_LABEL" user
  if ! verify_openclaw_gateway; then
    log "ERROR: stock OpenClaw verification failed under launchd"
    mac_launchd_transaction_rollback \
      || die "OpenClaw transaction compensation failed under launchd"
    return 1
  fi
  stop_gui_launchd_job_if_present "$uid" "$HERMES_LAUNCHD_LABEL"
  darwin_disable_job "gui/$uid/$HERMES_LAUNCHD_LABEL" "$HERMES_LAUNCHD_LABEL" user
  stop_gui_launchd_job_if_present "$uid" "$NEMOCLAW_LAUNCHD_LABEL"
  darwin_disable_job "gui/$uid/$NEMOCLAW_LAUNCHD_LABEL" "$NEMOCLAW_LAUNCHD_LABEL" user
  if ! finalize_openclaw_gateway; then
    log "ERROR: OpenClaw exclusivity proof failed under launchd"
    mac_launchd_transaction_rollback \
      || die "OpenClaw transaction compensation failed under launchd"
    return 1
  fi
  verify_selected_gateway_supervisor_health
  mac_launchd_transaction_commit
  darwin_clear_auxiliary_restore
  log "stock OpenClaw verified as exclusive launchd gateway; Hermes retained only for rollback"
}

install_darwin_hermes_service() {
  local uid="$1" plist="$HOME/Library/LaunchAgents/${HERMES_LAUNCHD_LABEL}.plist"
  local plist_staging="$HOME/Library/LaunchAgents/.${HERMES_LAUNCHD_LABEL}.${DEPLOY_TS}.$$.stage"
  local wrapper="$MAC_HOME/bin/hermes-gateway"
  local wrapper_staging="$MAC_HOME/bin/.hermes-gateway.${DEPLOY_TS}.$$.stage"
  local openclaw_plist="$HOME/Library/LaunchAgents/${OPENCLAW_LAUNCHD_LABEL}.plist"
  local nemoclaw_plist="$HOME/Library/LaunchAgents/${NEMOCLAW_LAUNCHD_LABEL}.plist"
  if [ -f "$plist" ]; then
    HERMES_PLIST_BACKUP="$MAC_HOME/backups/${HERMES_LAUNCHD_LABEL}.${AGENT}.${DEPLOY_TS}.plist"
    snapshot_rollback_file "$plist" "$HERMES_PLIST_BACKUP" user
    write_rollback_script
  fi
  if [ "$DEPLOY_ROLLBACK_ARMED" = 1 ] && [ -z "$HERMES_PLIST_BACKUP" ]; then
    die "cannot mutate the Hermes launchd job without a prior plist backup"
  fi
  HERMES_PLIST_MUTATED=1
  write_rollback_script
  darwin_clear_auxiliary_restore
  mac_launchd_transaction_begin \
    "gui/$uid" "$plist" "gui/$uid/$HERMES_LAUNCHD_LABEL" \
    "$HERMES_LAUNCHD_LABEL" user
  mac_launchd_transaction_set_expected_prior_state \
    "$(darwin_expected_prior_state "$DARWIN_HERMES_LAUNCHD_ACTIVE")"
  mac_launchd_transaction_track_file "$wrapper"
  mac_launchd_transaction_track_file "$openclaw_plist"
  mac_launchd_transaction_track_file "$nemoclaw_plist"
  mac_launchd_transaction_track_temporary "$wrapper_staging"
  mac_launchd_transaction_track_temporary "$plist_staging"
  if [ "$DARWIN_OPENCLAW_LAUNCHD_ACTIVE" = 1 ]; then
    darwin_set_auxiliary_restore \
      "gui/$uid" "$openclaw_plist" "gui/$uid/$OPENCLAW_LAUNCHD_LABEL" \
      "$OPENCLAW_LAUNCHD_LABEL" user
  elif [ "$DARWIN_NEMOCLAW_LAUNCHD_ACTIVE" = 1 ]; then
    darwin_set_auxiliary_restore \
      "gui/$uid" "$nemoclaw_plist" "gui/$uid/$NEMOCLAW_LAUNCHD_LABEL" \
      "$NEMOCLAW_LAUNCHD_LABEL" user
  fi
  install_hermes_gateway_wrapper "$wrapper_staging"
  cat > "$plist_staging" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$HERMES_LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$MAC_HOME/bin/hermes-gateway</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$MAC_HOME</string>
  <key>StandardOutPath</key><string>$LOG_DIR/hermes-gateway.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/hermes-gateway.log</string>
</dict>
</plist>
EOF
  /bin/bash -n "$wrapper_staging"
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$plist_staging"
  fi
  mac_launchd_transaction_mark_mutating
  stop_gui_launchd_job_if_present "$uid" "$OPENCLAW_LAUNCHD_LABEL"
  darwin_disable_job "gui/$uid/$OPENCLAW_LAUNCHD_LABEL" "$OPENCLAW_LAUNCHD_LABEL" user
  stop_gui_launchd_job_if_present "$uid" "$NEMOCLAW_LAUNCHD_LABEL"
  darwin_disable_job "gui/$uid/$NEMOCLAW_LAUNCHD_LABEL" "$NEMOCLAW_LAUNCHD_LABEL" user
  stop_gui_launchd_job_if_present "$uid" "$HERMES_LAUNCHD_LABEL"
  mac_launchd_transaction_replace "$wrapper_staging" "$wrapper" 0700
  mac_launchd_transaction_replace "$plist_staging" "$plist" 0644
  : > "$LOG_DIR/hermes-gateway.log"
  mac_launchd_bootstrap_job \
    "gui/$uid" "$plist" "gui/$uid/$HERMES_LAUNCHD_LABEL" \
    "$HERMES_LAUNCHD_LABEL" user
  verify_selected_gateway_supervisor_health
  mac_launchd_transaction_commit
  darwin_clear_auxiliary_restore
}

install_darwin_agent_service() {
  local uid="$1" plist="$HOME/Library/LaunchAgents/${MAC_AGENT_LAUNCHD_LABEL}.plist"
  local plist_staging="$HOME/Library/LaunchAgents/.${MAC_AGENT_LAUNCHD_LABEL}.${DEPLOY_TS}.$$.stage"
  local wrapper="$MAC_HOME/bin/mac-agent-service"
  local selftest="$MAC_HOME/bin/mac-agent-startup-self-test"
  local executor="$MAC_HOME/bin/mac-task-executor"
  local executor_py="$MAC_HOME/bin/mac-task-executor.py"
  local crash_observer="$MAC_HOME/bin/mac-crash-observer"
  local wrapper_staging="$MAC_HOME/bin/.mac-agent-service.${DEPLOY_TS}.$$.stage"
  local selftest_staging="$MAC_HOME/bin/.mac-agent-startup-self-test.${DEPLOY_TS}.$$.stage"
  local executor_staging="$MAC_HOME/bin/.mac-task-executor.${DEPLOY_TS}.$$.stage"
  local executor_py_staging="$MAC_HOME/bin/.mac-task-executor.py.${DEPLOY_TS}.$$.stage"
  local crash_observer_staging="$MAC_HOME/bin/.mac-crash-observer.${DEPLOY_TS}.$$.stage"
  log "installing launchd agent $plist"
  if [ -f "$plist" ]; then
    MAC_AGENT_PLIST_BACKUP="$MAC_HOME/backups/${MAC_AGENT_LAUNCHD_LABEL}.${AGENT}.${DEPLOY_TS}.plist"
    snapshot_rollback_file "$plist" "$MAC_AGENT_PLIST_BACKUP" user
    write_rollback_script
  fi
  if [ "$DEPLOY_ROLLBACK_ARMED" = 1 ] && [ -z "$MAC_AGENT_PLIST_BACKUP" ]; then
    die "cannot mutate the agent launchd job without a prior plist backup"
  fi
  MAC_AGENT_PLIST_MUTATED=1
  write_rollback_script
  darwin_clear_auxiliary_restore
  mac_launchd_transaction_begin \
    "gui/$uid" "$plist" "gui/$uid/$MAC_AGENT_LAUNCHD_LABEL" \
    "$MAC_AGENT_LAUNCHD_LABEL" user
  mac_launchd_transaction_set_expected_prior_state \
    "$(darwin_expected_prior_state "$DARWIN_AGENT_LAUNCHD_ACTIVE")"
  local canonical temporary
  for canonical in \
    "$wrapper" "$selftest" "$executor" "$executor_py" "$crash_observer"; do
    mac_launchd_transaction_track_file "$canonical"
  done
  for temporary in \
    "$plist_staging" "$wrapper_staging" "$selftest_staging" \
    "$executor_staging" "$executor_py_staging" "$crash_observer_staging"; do
    mac_launchd_transaction_track_temporary "$temporary"
  done
  install_mac_agent_wrapper \
    "$wrapper_staging" "$selftest_staging" \
    "$executor_staging" "$executor_py_staging" \
    "$crash_observer_staging" 0
  cat > "$plist_staging" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$MAC_AGENT_LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$MAC_HOME/bin/mac-crash-observer</string>
    <string>--supervisor</string><string>launchd</string>
    <string>--</string><string>$MAC_HOME/bin/mac-agent-service</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ExitTimeOut</key><integer>30</integer>
  <key>AbandonProcessGroup</key><false/>
  <key>WorkingDirectory</key><string>$MAC_HOME</string>
  <key>SoftResourceLimits</key>
  <dict><key>Core</key><integer>9223372036854775807</integer></dict>
  <key>HardResourceLimits</key>
  <dict><key>Core</key><integer>9223372036854775807</integer></dict>
  <key>StandardOutPath</key><string>$LOG_DIR/mac-agent.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/mac-agent.log</string>
</dict>
</plist>
EOF
  /bin/bash -n "$wrapper_staging"
  /bin/bash -n "$selftest_staging"
  /bin/bash -n "$executor_staging"
  "$PY" - "$executor_py_staging" "$crash_observer_staging" <<'PY'
import ast
import sys
from pathlib import Path

for raw in sys.argv[1:]:
    path = Path(raw)
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
PY
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$plist_staging"
  fi
  mac_launchd_transaction_mark_mutating
  stop_gui_launchd_job_if_present "$uid" "$MAC_AGENT_LAUNCHD_LABEL"
  mac_launchd_transaction_replace "$wrapper_staging" "$wrapper" 0700
  mac_launchd_transaction_replace "$selftest_staging" "$selftest" 0700
  mac_launchd_transaction_replace "$executor_staging" "$executor" 0700
  mac_launchd_transaction_replace "$executor_py_staging" "$executor_py" 0600
  mac_launchd_transaction_replace \
    "$crash_observer_staging" "$crash_observer" 0755
  mac_launchd_transaction_replace "$plist_staging" "$plist" 0644
  ln -sf "$executor" "$MAC_HOME/bin/mac-hermes-task-executor"
  ln -sf "$executor_py" "$MAC_HOME/bin/mac-hermes-task-executor.py"
  : > "$LOG_DIR/mac-agent.log"
  if truthy "$DEFER_AGENT_RESTART"; then
    log "deferring mac-agent restart until post-manifest reconciliation"
    [ "$(mac_launchd_job_state \
      "gui/$uid/$MAC_AGENT_LAUNCHD_LABEL" \
      "$MAC_AGENT_LAUNCHD_LABEL" user)" = inactive ] \
      || die "deferred launchd agent became active before controller handoff"
  else
    mac_launchd_bootstrap_job \
      "gui/$uid" "$plist" "gui/$uid/$MAC_AGENT_LAUNCHD_LABEL" \
      "$MAC_AGENT_LAUNCHD_LABEL" user
  fi
  mac_launchd_transaction_commit
}

classify_gateway_logs() {
  local input="$1"
  "$PY" - "$input" "$LOG_DIR/hermes-log-summary.json" <<'PY'
import json
import re
import sys
import time
from pathlib import Path

input_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
try:
    text = input_path.read_text(encoding="utf-8", errors="ignore")
except OSError:
    text = ""

# OpenShell uses terminal styling even when its output is redirected to the
# launchd log.  Classify the visible message, not the ANSI control bytes.
ansi_escape = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])")
classification_text = ansi_escape.sub("", text)

patterns = {
    "controlled_restart": {
        "severity": "info",
        "regex": r"Shutdown context: signal=SIGTERM|Failed with result 'exit-code'",
    },
    "slack_file_public_unhandled": {
        "severity": "info",
        "regex": r"Unhandled request .*'file_public'",
    },
    "discord_missing_token_unconfigured": {
        "severity": "info",
        "regex": r"\[Discord\] No bot token configured|discord failed to connect",
    },
    "openclaw_sandbox_create_recovered": {
        "severity": "info",
        "regex": (
            r"(?<![a-z0-9_])Error:[ \t]+×[ \t]+sandbox "
            r"'(?P<sandbox>mac-openclaw-[a-z0-9][a-z0-9._-]*)' "
            r"already exists[ \t]*$"
        ),
        "requires_regex": (
            r"Created sandbox:[ \t]*"
            r"(?P<sandbox>mac-openclaw-[a-z0-9][a-z0-9._-]*)[ \t]*$"
        ),
        "requires_then_regex": r"\[gateway\] ready\b",
        "match_key": "sandbox",
    },
    "openclaw_cron_device_approval_deferred": {
        "severity": "info",
        "regex": (
            r"(?:^[ \t]*openclaw cron deferred until device approval: "
            r"(?:scope_upgrade_pending_approval|pairing_required)[ \t]*$|"
            r"^[ \t]*openclaw cron deferred until device approval: gateway connect failed: "
            r"GatewayClientRequestError: scope upgrade pending approval \(requestId: "
            r"(?P<request_prefix>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-)(?P<request_tail>[0-9a-f]{9})[0-9a-f]{3}\)[ \t]*\r?\n"
            r"GatewayTransportError: gateway closed \(1008\): pairing required: "
            r"device is asking for more scopes than currently approved \(requestId: "
            r"(?P=request_prefix)(?P=request_tail)[ \t]*$)"
        ),
    },
    "secret_redaction_disabled": {
        "severity": "critical",
        "regex": r"Secret redaction: DISABLED|HERMES_REDACT_SECRETS=false",
    },
    "traceback": {
        "severity": "error",
        "regex": (
            r"Traceback \(most recent call last\)|\bERROR\b|Exception|"
            r"Gateway(?:ClientRequest|Transport)Error"
        ),
    },
}
classes = []
actionable_text = classification_text
info_flags = re.IGNORECASE | re.MULTILINE


def blank_matches(source, matches):
    chars = list(source)
    for match in matches:
        for index in range(match.start(), match.end()):
            if chars[index] not in {"\r", "\n"}:
                chars[index] = " "
    return "".join(chars)


for name, spec in patterns.items():
    if spec["severity"] != "info":
        continue
    matches = list(re.finditer(spec["regex"], actionable_text, flags=info_flags))
    match_key = spec.get("match_key")
    requires_regex = spec.get("requires_regex")
    requires_then_regex = spec.get("requires_then_regex")
    if match_key and requires_regex:
        required_matches = list(
            re.finditer(requires_regex, actionable_text, flags=info_flags)
        )
        then_matches = (
            list(re.finditer(requires_then_regex, actionable_text, flags=info_flags))
            if requires_then_regex
            else []
        )

        def has_ordered_recovery(candidate):
            candidate_key = candidate.group(match_key).lower()
            for required in required_matches:
                if required.start() <= candidate.end():
                    continue
                if required.group(match_key).lower() != candidate_key:
                    continue
                if requires_then_regex and not any(
                    then.start() > required.end() for then in then_matches
                ):
                    continue
                return True
            return False

        matches = [
            match for match in matches if has_ordered_recovery(match)
        ]
    if matches:
        classes.append({"name": name, "severity": spec["severity"], "count": len(matches)})
        actionable_text = blank_matches(actionable_text, matches)

for name, spec in patterns.items():
    if spec["severity"] == "info":
        continue
    matches = re.findall(spec["regex"], actionable_text, flags=re.IGNORECASE)
    if matches:
        classes.append({"name": name, "severity": spec["severity"], "count": len(matches)})

summary = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "source": str(input_path),
    "classes": classes,
    "actionable_count": sum(1 for item in classes if item["severity"] in {"critical", "error"}),
    "benign_count": sum(1 for item in classes if item["severity"] == "info"),
}
output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(
    "gateway log summary: actionable=%d benign=%d classes=%s"
    % (
        summary["actionable_count"],
        summary["benign_count"],
        ",".join(item["name"] for item in classes) or "none",
    )
)
if summary["actionable_count"]:
    raise SystemExit(1)
PY
}

verify_hub_registration() {
  # Hub nodes register with their own local API; the external service DNS may
  # not expose the control-plane port (e.g. K8s Service without port 8789).
  local check_url token
  if [ "$WORKER_MODE" = "loop" ] && [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]; then
    check_url="http://127.0.0.1:${MAC_PORT}"
    token="${MAC_API_TOKEN}"
  else
    check_url="${MAC_HUB_URL:-$HUB_URL}"
    token="${MAC_WORKER_TOKEN}"
  fi
  log "verifying mac-agent registration with hub ${check_url}"
  local attempt
  for attempt in $(seq 1 10); do
    if curl -fsS --max-time 10 --config - \
      "${check_url}/agents" > "$LOG_DIR/hub-agents.json" <<CURL; then
header = "Authorization: Bearer $token"
CURL
      if "$PY" - "$LOG_DIR/hub-agents.json" "${MAC_WORKER_AGENT_NAME:-$AGENT}" <<'PY'; then
import json
import sys

agents_path, expected_name = sys.argv[1], sys.argv[2]
with open(agents_path, "r", encoding="utf-8") as handle:
    agents = json.load(handle)
for agent in agents:
    if agent.get("name") == expected_name:
        print(
            "hub registration: agent=%s id=%s status=%s health=%s last_seen=%s"
            % (
                agent.get("name"),
                agent.get("id"),
                agent.get("status"),
                agent.get("health_status"),
                agent.get("last_seen_at"),
            )
        )
        raise SystemExit(0)
print("hub registration: agent %s not present yet among %d agents" % (expected_name, len(agents)))
raise SystemExit(1)
PY
        return 0
      fi
    fi
    sleep 2
  done
  if [ "$WORKER_MODE" = "loop" ]; then
    log "ERROR: mac-agent did not register with hub ${check_url}"
    return 1
  fi
  log "WARNING: mac-agent not yet registered with hub ${check_url} (tunnel may not be established yet)"
}

verify_selected_gateway_supervisor_health() {
  local output="$LOG_DIR/gateway-readiness.json"
  "$PY" - "$SUPERVISOR_KIND" "${HERMES_GATEWAY_IMPL:-hermes}" "$FLEET_NAME" \
    "$HERMES_SERVICE_NAME" "$OPENCLAW_SERVICE_NAME" "$NEMOCLAW_SERVICE_NAME" \
    "$HERMES_LAUNCHD_LABEL" "$OPENCLAW_LAUNCHD_LABEL" "$NEMOCLAW_LAUNCHD_LABEL" \
    "$HERMES_SUPERVISORD_PROG" "$OPENCLAW_SUPERVISORD_PROG" "$NEMOCLAW_SUPERVISORD_PROG" \
    "$DEPLOY_GENERATION" "$DEPLOY_REV" "$output" <<'PY'
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

(
    supervisor,
    implementation,
    fleet,
    hermes_unit,
    openclaw_unit,
    nemoclaw_unit,
    hermes_label,
    openclaw_label,
    nemoclaw_label,
    hermes_program,
    openclaw_program,
    nemoclaw_program,
    generation,
    revision,
    output_raw,
) = sys.argv[1:]
output = Path(output_raw)
deadline = time.monotonic() + 45.0
names = {
    "hermes": {
        "systemd": hermes_unit,
        "launchd": hermes_label,
        "supervisord": hermes_program,
    },
    "openclaw": {
        "systemd": openclaw_unit,
        "launchd": openclaw_label,
        "supervisord": openclaw_program,
    },
    "nemoclaw": {
        "systemd": nemoclaw_unit,
        "launchd": nemoclaw_label,
        "supervisord": nemoclaw_program,
    },
}
if implementation not in {"hermes", "openclaw", "nemoclaw", "none"}:
    raise SystemExit("gateway readiness received an unsupported implementation")


def fail(message):
    raise SystemExit("gateway readiness failed: " + message)


def remaining():
    value = deadline - time.monotonic()
    if value <= 0:
        fail("total deadline expired")
    return value


def run(argv):
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError:
            fail("supervisor command could not start")
        try:
            process.wait(timeout=min(8.0, remaining()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            # The group leader may exit on TERM while a descendant ignores it.
            # Always issue the terminal group kill before reporting timeout.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            fail("supervisor command timed out")
        if stdout.tell() > 1024 * 1024 or stderr.tell() > 1024 * 1024:
            fail("supervisor output exceeded its bound")
        stdout.seek(0)
        stderr.seek(0)
        try:
            stdout_text = stdout.read().decode("utf-8", errors="strict")
            stderr_text = stderr.read().decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            fail("supervisor output is not UTF-8")
        return process.returncode, stdout_text, stderr_text


def systemd_sample():
    systemctl = shutil.which("systemctl")
    if not systemctl:
        fail("systemctl is unavailable")
    prefix = []
    sudo = shutil.which("sudo")
    if os.geteuid() != 0:
        if not sudo:
            fail("systemd inspection requires noninteractive sudo")
        prefix = [sudo, "-n"]
    result = {}
    for owner, mapping in names.items():
        rc, text, _errors = run(
            prefix
            + [
                systemctl,
                "show",
                mapping["systemd"],
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
                "--property=NRestarts",
            ]
        )
        fields = {}
        for line in text.splitlines():
            if not line or "=" not in line:
                fail("systemd state is malformed")
            key, value = line.split("=", 1)
            if key in fields:
                fail("systemd state is ambiguous")
            fields[key] = value
        if set(fields) != {"LoadState", "ActiveState", "SubState", "MainPID", "NRestarts"}:
            fail("systemd state is incomplete")
        if fields.get("LoadState") == "not-found":
            if fields.get("ActiveState") != "inactive" or fields.get("MainPID") != "0":
                fail("systemd absent state is contradictory")
            result[owner] = {
                "state": "absent",
                "pid": 0,
                "restarts": 0,
                "enabled": "not-found",
            }
            continue
        if rc != 0:
            fail("systemd state is unreadable")
        if fields.get("LoadState") != "loaded":
            fail("systemd unit load state is unknown")
        active = fields.get("ActiveState")
        sub = fields.get("SubState")
        if active == "active" and sub == "running":
            try:
                pid = int(fields.get("MainPID") or "0")
                restarts = int(fields.get("NRestarts") or "0")
            except ValueError:
                fail("systemd runtime counters are malformed")
            if pid <= 0 or restarts < 0:
                fail("systemd running state lacks a valid process")
            state = "running"
        elif active in {"inactive", "failed"} and fields.get("MainPID") == "0":
            state = active
            pid = 0
            restarts = 0
        else:
            fail("systemd unit is transitional or unknown")
        enabled_rc, enabled_out, _enabled_errors = run(
            prefix + [systemctl, "is-enabled", mapping["systemd"]]
        )
        enabled_lines = [line.strip() for line in enabled_out.splitlines() if line.strip()]
        if len(enabled_lines) != 1 or enabled_lines[0] not in {
            "enabled",
            "disabled",
            "masked",
            "static",
            "indirect",
        }:
            fail("systemd enablement state is ambiguous")
        enabled = enabled_lines[0]
        if enabled == "enabled" and enabled_rc != 0:
            fail("systemd enablement state is contradictory")
        result[owner] = {
            "state": state,
            "pid": pid,
            "restarts": restarts,
            "enabled": enabled,
        }
    return result


def launchd_sample():
    launchctl = shutil.which("launchctl")
    if not launchctl:
        fail("launchctl is unavailable")
    domain = "gui/%d" % os.getuid()
    result = {}
    for owner, mapping in names.items():
        label = mapping["launchd"]
        rc, stdout, stderr = run([launchctl, "print", domain + "/" + label])
        text = stdout + stderr
        if rc == 113 and len([line for line in text.splitlines() if line.strip()]) == 1 and "Could not find service" in text:
            result[owner] = {"state": "absent", "pid": 0, "restarts": 0}
            continue
        if rc != 0:
            fail("launchd state is unreadable")
        state_match = re.search(r"(?m)^\s*state\s*=\s*([^\s]+)", text)
        pid_match = re.search(r"(?m)^\s*pid\s*=\s*([0-9]+)", text)
        if not state_match or state_match.group(1) != "running" or not pid_match:
            fail("launchd job is loaded but not stably running")
        pid = int(pid_match.group(1))
        if pid <= 0:
            fail("launchd running state lacks a valid process")
        result[owner] = {"state": "running", "pid": pid, "restarts": 0}
    return result


def supervisor_scope_status(prefix, supervisorctl):
    result = {}
    for owner, mapping in names.items():
        rc, stdout, stderr = run(prefix + [supervisorctl, "status", mapping["supervisord"]])
        text = stdout + stderr
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        expected_absent = mapping["supervisord"] + ": ERROR (no such process)"
        if rc != 0 and lines == [expected_absent]:
            result[owner] = {"state": "absent", "pid": 0, "restarts": 0}
            continue
        first = lines[0] if len(lines) == 1 else ""
        fields = first.split()
        if len(fields) < 2 or fields[0] != mapping["supervisord"]:
            fail("supervisord returned an ambiguous program identity")
        state = fields[1]
        if state == "RUNNING" and rc == 0:
            match = re.search(r"\bpid\s+([0-9]+)\b", first)
            if match is None or int(match.group(1)) <= 0:
                fail("supervisord running state lacks a valid process")
            result[owner] = {
                "state": "running",
                "pid": int(match.group(1)),
                "restarts": 0,
            }
        elif state in {"STOPPED", "EXITED", "FATAL"} and rc in {0, 3}:
            result[owner] = {"state": state.lower(), "pid": 0, "restarts": 0}
        else:
            fail("supervisord program is transitional or unreadable")
    return result


def supervisord_sample():
    supervisorctl = shutil.which("supervisorctl")
    if not supervisorctl:
        fail("supervisorctl is unavailable")
    prefix = []
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            fail("system supervisord inspection requires noninteractive sudo")
        prefix = [sudo, "-n"]
    # Installation, quiescence, rollback, and readiness must all address the
    # same system supervisord manager.  Never retry a failed system-manager
    # probe against the invoking user's unrelated manager: that would publish
    # evidence for a topology MAC did not mutate.
    return supervisor_scope_status(prefix, supervisorctl)


sampler = {
    "systemd": systemd_sample,
    "launchd": launchd_sample,
    "supervisord": supervisord_sample,
}.get(supervisor)
if sampler is None:
    fail("unsupported supervisor")
samples = []
for observation in range(2):
    sample = sampler()
    selected_ready = (
        implementation != "none" and sample[implementation]["state"] == "running"
    )
    if supervisor == "systemd" and implementation != "none":
        selected_ready = selected_ready and sample[implementation].get("enabled") == "enabled"
    if not selected_ready and implementation != "none":
        fail("selected gateway implementation is not in its required state")
    for owner, item in sample.items():
        if owner == implementation:
            continue
        if supervisor == "systemd":
            if item["state"] not in {"absent", "inactive"} or item.get("enabled") not in {
                "not-found",
                "disabled",
                "masked",
            }:
                fail("non-selected systemd gateway is not safely disabled")
        elif supervisor == "launchd":
            if item["state"] != "absent":
                fail("non-selected launchd gateway is still loaded")
        elif implementation == "openclaw" and owner == "hermes":
            if item["state"] not in {"absent", "stopped"}:
                fail("Hermes rollback gateway is not stopped")
        elif item["state"] != "absent":
            fail("non-selected supervisord gateway is still configured")
    samples.append(sample)
    if observation == 0:
        time.sleep(min(2.0, remaining()))
if implementation != "none":
    first = samples[0][implementation]
    second = samples[1][implementation]
    if first["pid"] != second["pid"] or first["restarts"] != second["restarts"]:
        fail("selected gateway restarted during the readiness proof")

payload = {
    "schema": "mac.gateway_readiness.v1",
    "agent": os.environ.get("AGENT"),
    "fleet": fleet,
    "generation": generation,
    "revision": revision,
    "supervisor": supervisor,
    "implementation": implementation,
    "identities": {owner: mapping[supervisor] for owner, mapping in names.items()},
    "stable_observations": 2,
    "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "state": samples[-1],
}
output.parent.mkdir(parents=True, exist_ok=True)
fd, raw = tempfile.mkstemp(prefix=output.name + ".", dir=str(output.parent))
tmp = Path(raw)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, output)
    directory = os.open(output.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    tmp.unlink(missing_ok=True)
PY
}

# Re-prove immediately before any new gateway supervisor is installed or
# started.  The post-install proof below catches a daemon restart during the
# service transition; OpenClaw verify/finalize also assert immediately before
# health and publication.
assert_legacy_nemoclaw_containers_inactive pre_install
case "$SUPERVISOR_KIND" in
  systemd) install_linux_service ;;
  launchd) install_darwin_service ;;
  supervisord) install_supervisord_service ;;
  *) log "ERROR: unsupported supervisor $SUPERVISOR_KIND"; exit 1 ;;
esac

verify_selected_gateway_supervisor_health

# A compose daemon can recreate a restart-managed legacy container after the
# pre-replacement proof.  Do not let that old gateway satisfy downstream
# health checks or coexist with the selected post-deploy implementation.
if ! assert_legacy_nemoclaw_containers_inactive post_install; then
  if [ "${HERMES_GATEWAY_IMPL:-hermes}" = openclaw ]; then
    log "ERROR: post-install daemon-resource proof failed; withdrawing OpenClaw ownership"
    if ! withdraw_openclaw_gateway; then
      log "ERROR: OpenClaw withdrawal failed after daemon-resource proof failure; generation rollback stays armed"
    fi
    rm -f \
      "$MAC_HOME/openclaw/service-advertisement.json" \
      "$MAC_HOME/openclaw/verification-pending.json"
  fi
  exit 1
fi

# The service installer may now have created the long-lived OpenClaw sandbox.
# Prove the API can decode it and every surviving managed container binds the
# exact supervisor version reviewed with this CLI before any post manifest can
# declare the node deployable.
verify_managed_openshell_runtime

if [ "$NODE_ACTION" = legacy-one-shot ]; then
  # Optional capabilities and footprint reconciliation are independent
  # onboarding/post-commit jobs, never part of synchronized phase 2.
  install_gpu_gen_server || true
  install_agent_footprint || true
else
  log "typed phase 2 deferred optional capabilities and package-footprint reconciliation"
fi

if [ "${HERMES_GATEWAY_IMPL:-hermes}" = "openclaw" ]; then
  if [ "$SUPERVISOR_KIND" = "systemd" ]; then
    classify_gateway_logs "$LOG_DIR/openclaw-gateway-journal.txt"
  else
    classify_gateway_logs "$LOG_DIR/openclaw-gateway.log"
  fi
elif [ "$SUPERVISOR_KIND" = "systemd" ]; then
  classify_gateway_logs "$LOG_DIR/hermes-gateway-journal.txt"
else
  classify_gateway_logs "$LOG_DIR/hermes-gateway.log"
fi

log "verifying hub health and local executor startup report"
if control_plane_enabled; then
  curl -fsS "http://127.0.0.1:$MAC_PORT/health" > "$LOG_DIR/health.json"
  curl -fsS --config - \
    "http://127.0.0.1:$MAC_PORT/startup/hermes" \
    > "$LOG_DIR/startup-hermes.json" <<CURL
header = "Authorization: Bearer $MAC_API_TOKEN"
CURL
else
  curl -fsS "$MAC_HUB_URL/health" > "$LOG_DIR/health.json"
  "$VENV/bin/python" - "$LOG_DIR/startup-hermes.json" <<'PY'
import json
import sys
from pathlib import Path
from mac.hermes_startup import build_hermes_startup_report

Path(sys.argv[1]).write_text(
    json.dumps(build_hermes_startup_report(), indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
fi
"$PY" - "$LOG_DIR/startup-hermes.json" <<'PY'
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
slack = data.get("slack") or {}
qdrant = data.get("qdrant_level2") or {}
firecrawl = data.get("firecrawl_web_search") or {}
runtime = data.get("task_project_runtime") or {}
prompt_bridge = runtime.get("prompt_bridge") or {}
refs = data.get("state_refs") or []
existing = sum(1 for ref in refs if ref.get("exists"))
patch = slack.get("account_file_activation_shim_patch") or {}
print(
    "startup: ready=%s warnings=%d state_refs_existing=%d "
    "slack_activation=%s shim_present=%s redaction=%s operator_status=%s "
    "qdrant_status=%s qdrant_ready=%s topology=%s firecrawl_status=%s firecrawl_ready=%s "
    "runtime_status=%s runtime_ready=%s runtime_context=%s prompt_bridge=%s hermes_instance=%s "
    "patch_attempted=%s patch_applied=%s patch_error=%s"
    % (
        data.get("ready"),
        len(data.get("warnings") or []),
        existing,
        slack.get("activation_source"),
        slack.get("account_file_activation_shim_present"),
        (data.get("security") or {}).get("secret_redaction", {}).get("effective"),
        (data.get("operator_health") or {}).get("status"),
        qdrant.get("status"),
        qdrant.get("ready"),
        ((qdrant.get("topology") or {}).get("file") or {}).get("exists"),
        firecrawl.get("status"),
        firecrawl.get("ready"),
        runtime.get("status"),
        runtime.get("ready"),
        (runtime.get("context_file") or {}).get("exists"),
        prompt_bridge.get("present"),
        runtime.get("hermes_instance_id"),
        patch.get("attempted"),
        patch.get("applied"),
        bool(patch.get("error")),
    )
)
if data.get("warnings"):
    for warning in data["warnings"]:
        print("startup warning: %s" % warning)
PY

verify_hub_registration
case "$(printf '%s' "$DEFER_CLEAR_DRAIN" | tr 'A-Z' 'a-z')" in
  1|true|yes|on) log "keeping drain state until post-deploy OpenShell validation completes" ;;
  *) clear_mac_agent_drain_after_deploy ;;
esac

write_deploy_manifest "post" "$MANIFEST_POST"
cp -f "$MANIFEST_POST" "$LOG_DIR/deploy-manifest-latest.json"
chmod 0600 "$LOG_DIR/deploy-manifest-latest.json" 2>/dev/null || true
if [ "$NODE_ACTION" = legacy-one-shot ]; then
  write_phase2_finalize_receipt
fi
DEPLOY_COMPLETED=1
log "deploy complete"
