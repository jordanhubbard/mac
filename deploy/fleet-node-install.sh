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
trap stop_deployment_lock_renewer EXIT

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
DEPLOY_LOG="$LOG_DIR/deploy-${DEPLOY_TS}.log"
DEPLOY_STARTED_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ROLLBACK_SCRIPT="$LOG_DIR/rollback-${DEPLOY_TS}.sh"
ROLLBACK_LATEST="$LOG_DIR/rollback-latest.sh"
MANIFEST_PRE="$LOG_DIR/deploy-manifest-${DEPLOY_TS}-pre.json"
MANIFEST_POST="$LOG_DIR/deploy-manifest-${DEPLOY_TS}-post.json"
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
MAC_AGENT_LAUNCHD_LABEL="com.${FLEET_NAME}.agent"
MAC_SUPERVISORD_PROG="${FLEET_NAME}-control-plane"
HERMES_SUPERVISORD_PROG="${FLEET_NAME}-hermes-gateway"
OPENCLAW_SUPERVISORD_PROG="${FLEET_NAME}-openclaw-gateway"
AGENT_SUPERVISORD_PROG="${FLEET_NAME}-agent"
MAC_SUPERVISORD_CONF_NAME="${FLEET_NAME}-fleet.conf"
SRC_BACKUP=""
VENV_BACKUP=""
HERMES_BACKUP=""
MAC_UNIT_BACKUP=""
HERMES_UNIT_BACKUP=""
MAC_AGENT_UNIT_BACKUP=""
MAC_PLIST_BACKUP=""
DARWIN_SYSTEM_PLIST_BACKUP=""
DARWIN_SYSTEM_LAUNCHD_ACTIVE=0
DARWIN_GUI_LAUNCHD_ACTIVE=0
DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP=""
DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE=0
HERMES_PLIST_BACKUP=""
MAC_AGENT_PLIST_BACKUP=""

# Apply a restrictive umask before creating LOG_DIR so it gets 0700 (owner only).
umask 0077
mkdir -p "$LOG_DIR" "$MAC_HOME/backups"
umask 0022
exec > >(tee -a "$DEPLOY_LOG") 2>&1
# Tighten deploy log to owner-read/write only (the tee process already has the fd open).
chmod 0600 "$DEPLOY_LOG" 2>/dev/null || true

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

if [ -n "$FLEET_REGISTRY_FILE" ] && [ -f "$FLEET_REGISTRY_FILE" ]; then
  mkdir -p "$MAC_HOME"
  cp -f "$FLEET_REGISTRY_FILE" "$MAC_HOME/fleets.yaml"
  chmod 0644 "$MAC_HOME/fleets.yaml"
  rm -f "$FLEET_REGISTRY_FILE"
  log "installed fleet registry at $MAC_HOME/fleets.yaml"
fi

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
  # No host Python >= 3.11. Rather than depend on whatever the host/base image
  # happens to ship (host Python bleeding through — a generic Ubuntu pod is 3.10),
  # provision an exact interpreter with a SHA256-reviewed native uv release.
  # No remote installer script is executed in this credential-bearing process.
  local uv_bin managed
  uv_bin="$MAC_HOME/bin/uv-bootstrap"
  if ! mac_install_reviewed_uv "$uv_bin" "$MAC_HOME/cache/reviewed-assets"; then
    log "ERROR: checksum-verified uv $MAC_REVIEWED_UV_VERSION installation failed"
    exit 1
  fi
  log "no host Python >= 3.11; provisioning exact Python $MAC_REVIEWED_PYTHON_VERSION with reviewed uv $MAC_REVIEWED_UV_VERSION"
  run_without_deploy_credentials \
    "$uv_bin" python install "$MAC_REVIEWED_PYTHON_VERSION" >/dev/null 2>&1
  managed="$(run_without_deploy_credentials \
    "$uv_bin" python find "$MAC_REVIEWED_PYTHON_VERSION" 2>/dev/null || true)"
  if [ -n "$managed" ] && run_without_deploy_credentials \
      "$managed" - "$MAC_REVIEWED_PYTHON_VERSION" <<'PY' >/dev/null 2>&1
import sys
expected = tuple(int(part) for part in sys.argv[1].split("."))
raise SystemExit(0 if sys.version_info[:3] == expected else 1)
PY
  then
    printf '%s\n' "$managed"
    return
  fi
  log "ERROR: reviewed uv did not provision exact Python $MAC_REVIEWED_PYTHON_VERSION"
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
export AGENT FLEET_NAME OS_KIND DEPLOY_TS DEPLOY_REV DEPLOY_GENERATION DEPLOY_GIT_URL DEPLOY_GIT_BRANCH DEPLOY_STARTED_ISO HERMES_SLACK_HOME_CHANNEL_NAME HERMES_GATEWAY_MODEL HERMES_GATEWAY_PROVIDER HERMES_GATEWAY_BASE_URL HERMES_GATEWAY_IMPL HERMES_SURFACE_B64 OPENCLAW_PUBLIC_IDENTITY OPENCLAW_REPRESENTED_BY OPENCLAW_REPRESENTATION_MODE OPENCLAW_SLACK_ACCOUNT_ID OPENCLAW_TELEGRAM_ACCOUNT_ID HUB_URL HUB_TUNNEL_PUBKEY CONTROL_BIND_HOST WORKER_MODE WORKER_CAPABILITIES WORKER_ALLOWED_PROJECTS WORKER_REQUIRED_METADATA WORKER_REQUIRE_CANARY SUPERVISOR_REQUESTED SUPERVISOR_KIND SHARED_SERVICES_MANAGER_AGENT QDRANT_URL_CONFIGURED QDRANT_INSTALL QDRANT_REQUIRE QDRANT_BIND_ADDR_CONFIGURED QDRANT_PORT_CONFIGURED QDRANT_IMAGE_CONFIGURED QDRANT_MEMORY_LIMIT_CONFIGURED QDRANT_DATA_DIR_CONFIGURED FIRECRAWL_URL_CONFIGURED FIRECRAWL_INSTALL FIRECRAWL_REQUIRE FIRECRAWL_BIND_ADDR_CONFIGURED FIRECRAWL_PORT_CONFIGURED WEBDAV_ENABLED WEBDAV_URL_CONFIGURED WEBDAV_INSTALL WEBDAV_BIND_ADDR_CONFIGURED WEBDAV_PORT_CONFIGURED WEBDAV_ROOT_CONFIGURED WEBDAV_PUBLIC_PATH_CONFIGURED WEBDAV_MAX_UPLOAD_BYTES_CONFIGURED DRAIN_MODE DRAIN_TIMEOUT_SECONDS DRAIN_POLL_SECONDS CONFIGURED_AGENT_IDS OPENSHELL_DEPLOY_ENABLED OPENSHELL_EFFECTIVE_ARGS OPENSHELL_RUNTIME_IMAGE OPENSHELL_LOCAL_IMAGE_BUILD MAC_HOME MAC_PORT MAC_SERVICE_NAME HERMES_SERVICE_NAME OPENCLAW_SERVICE_NAME NEMOCLAW_SERVICE_NAME MAC_AGENT_SERVICE_NAME MAC_LAUNCHD_LABEL HERMES_LAUNCHD_LABEL OPENCLAW_LAUNCHD_LABEL MAC_AGENT_LAUNCHD_LABEL MAC_SUPERVISORD_PROG HERMES_SUPERVISORD_PROG OPENCLAW_SUPERVISORD_PROG AGENT_SUPERVISORD_PROG MAC_SUPERVISORD_CONF_NAME SRC_DIR VENV HERMES_DIR ENV_FILE LOG_DIR DEPLOY_LOG PY HERMES_PY PYTHON_BIN

disk_hygiene_report() {
  local stage="$1" path="$2"
  "$PY" - "$stage" "$path" <<'PY'
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
  if dns_lookup; then
    return
  fi
  if [ "$OS_KIND" = "linux" ] && [ -f /run/systemd/resolve/resolv.conf ]; then
    log "repairing DNS resolver path for package installation"
    sudo ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
  fi
  if ! dns_lookup; then
    log "ERROR: DNS resolution still fails after resolver repair"
    exit 1
  fi
}

ensure_venv_support() {
  local probe="$MAC_HOME/.venv-probe"
  rm -rf "$probe"
  if "$PY" -m venv "$probe" >/dev/null 2>&1; then
    rm -rf "$probe"
    return
  fi
  rm -rf "$probe"
  if [ "$OS_KIND" = "linux" ] && command -v apt-get >/dev/null 2>&1; then
    log "installing python3-venv prerequisite"
    sudo apt-get update >/dev/null
    sudo apt-get install -y python3-venv >/dev/null
    "$PY" -m venv "$probe" >/dev/null
    rm -rf "$probe"
    return
  fi
  log "ERROR: python venv support is unavailable and could not be installed automatically"
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
    systemd|launchd|supervisord)
      printf '%s\n' "$SUPERVISOR_REQUESTED"
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
  if command -v sudo >/dev/null 2>&1; then
    sudo supervisorctl "$@" || supervisorctl "$@"
  else
    supervisorctl "$@"
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
  local ssh_dir="$HOME/.ssh"
  local key_file="$ssh_dir/mac_github_review_id"
  local config_file="$ssh_dir/config"
  local candidate_file="$ssh_dir/.mac_github_review_id.candidate"
  mkdir -p "$ssh_dir"
  chmod 700 "$ssh_dir"
  ssh-keyscan -H github.com 2>/dev/null >> "$ssh_dir/known_hosts"
  chmod 644 "$ssh_dir/known_hosts"
  log "added github.com to SSH known_hosts"
  touch "$config_file"
  chmod 600 "$config_file"
  remove_managed_github_review_key_config "$config_file"
  rm -f "$candidate_file"

  if [ -n "$GITHUB_REVIEW_KEY_B64" ]; then
    "$PYTHON_BIN" -c "import base64, sys; open(sys.argv[1],'wb').write(base64.b64decode(sys.stdin.buffer.read()))" \
      "$candidate_file" <<<"$GITHUB_REVIEW_KEY_B64"
    chmod 600 "$candidate_file"
    if ssh-keygen -y -f "$candidate_file" >/dev/null 2>&1 \
      && github_ssh_auth_succeeds "$candidate_file"; then
      mv -f "$candidate_file" "$key_file"
      printf '\n# mac GitHub review deploy key\nHost github.com\n  IdentityFile ~/.ssh/mac_github_review_id\n  IdentitiesOnly yes\n' >> "$config_file"
      log "installed and verified GitHub review deploy key at $key_file"
      return 0
    fi
    log "WARNING: generated GitHub review key is not authorized by github.com; refusing to make it the exclusive identity"
  fi

  rm -f "$candidate_file" "$key_file"
  if github_ssh_auth_succeeds; then
    log "using the host's verified ambient GitHub SSH identity"
    return 0
  fi
  if [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]; then
    echo "ERROR: the hub cannot authenticate to github.com for review publication" >&2
    echo "Authorize the deploy operator's ~/.mac/keys/mac-github-review-id.pub as an account SSH key, or configure a working ambient GitHub SSH identity, then redeploy." >&2
    exit 1
  fi
  log "WARNING: no GitHub SSH identity is authorized on this spoke; repository work that requires SSH will be rejected or routed elsewhere"
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
  if ! "$gh_bin" auth setup-git --hostname github.com >/dev/null 2>&1; then
    if [ "$GITHUB_CREDENTIALS_REQUIRED" = "1" ]; then
      log "ERROR: gh auth setup-git failed on a node that requires repository credentials"
      return 1
    fi
    log "WARNING: gh auth setup-git failed; HTTPS repository publication may fail"
    return 0
  fi
  log "GitHub HTTPS credential helper configured and credential verified (source: env:GH_TOKEN)"
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
        systemctl --user disable --now openshell-gateway.service >/dev/null 2>&1 \
          || die "could not stop deploy-managed OpenShell systemd gateway"
        if systemctl --user is-active --quiet openshell-gateway.service; then
          die "deploy-managed OpenShell systemd gateway is still active"
        fi
      fi
      rm -f "$systemd_gateway"
      if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        systemctl --user daemon-reload >/dev/null 2>&1 \
          || die "could not reload the user systemd manager after OpenShell removal"
      fi
    fi
    if [ "$supervisor_gateway_owned" = 1 ]; then
      command -v supervisorctl >/dev/null 2>&1 \
        || die "cannot stop deploy-managed OpenShell supervisor gateway: supervisorctl is unavailable"
      sudo -n supervisorctl stop openshell-gateway >/dev/null 2>&1 || true
      sudo -n rm -f "$supervisor_gateway"
      sudo -n supervisorctl reread >/dev/null
      sudo -n supervisorctl update >/dev/null
      if sudo -n supervisorctl status openshell-gateway 2>/dev/null \
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
          sudo -n systemctl disable --now mac-openshell-firewall.service >/dev/null 2>&1 \
            || die "could not stop deploy-managed OpenShell firewall service"
          if sudo -n systemctl is-active --quiet mac-openshell-firewall.service; then
            die "deploy-managed OpenShell firewall service is still active"
          fi
        fi
        sudo -n rm -f "$systemd_firewall"
        if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
          sudo -n systemctl daemon-reload >/dev/null
        fi
      fi
      if [ "$supervisor_firewall_owned" = 1 ]; then
        command -v supervisorctl >/dev/null 2>&1 \
          || die "cannot remove deploy-managed OpenShell supervisor firewall: supervisorctl is unavailable"
        sudo -n rm -f "$supervisor_firewall"
        sudo -n supervisorctl reread >/dev/null
        sudo -n supervisorctl update >/dev/null
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
  FLEET_NAME="$FLEET_NAME" \
  MAC_SERVICE_NAME="$MAC_SERVICE_NAME" HERMES_SERVICE_NAME="$HERMES_SERVICE_NAME" OPENCLAW_SERVICE_NAME="$OPENCLAW_SERVICE_NAME" MAC_AGENT_SERVICE_NAME="$MAC_AGENT_SERVICE_NAME" \
  MAC_LAUNCHD_LABEL="$MAC_LAUNCHD_LABEL" DARWIN_SYSTEM_SUPERVISOR_LABEL="$DARWIN_SYSTEM_SUPERVISOR_LABEL" HERMES_LAUNCHD_LABEL="$HERMES_LAUNCHD_LABEL" OPENCLAW_LAUNCHD_LABEL="$OPENCLAW_LAUNCHD_LABEL" MAC_AGENT_LAUNCHD_LABEL="$MAC_AGENT_LAUNCHD_LABEL" \
  MAC_SUPERVISORD_PROG="$MAC_SUPERVISORD_PROG" HERMES_SUPERVISORD_PROG="$HERMES_SUPERVISORD_PROG" OPENCLAW_SUPERVISORD_PROG="$OPENCLAW_SUPERVISORD_PROG" AGENT_SUPERVISORD_PROG="$AGENT_SUPERVISORD_PROG" \
  "$PY" - "$stage" "$path" <<'PY'
import json
import os
import shlex
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


def service_summary():
    supervisor = os.environ.get("SUPERVISOR_KIND") or (
        "launchd" if os.environ["OS_KIND"] == "darwin" else "systemd"
    )
    fleet = os.environ.get("FLEET_NAME", "mac")
    mac_svc = os.environ.get("MAC_SERVICE_NAME", fleet + ".service")
    hermes_svc = os.environ.get("HERMES_SERVICE_NAME", fleet + "-hermes-gateway.service")
    openclaw_svc = os.environ.get("OPENCLAW_SERVICE_NAME", fleet + "-openclaw-gateway.service")
    agent_svc = os.environ.get("MAC_AGENT_SERVICE_NAME", fleet + "-agent.service")
    mac_label = os.environ.get("MAC_LAUNCHD_LABEL", "com." + fleet + ".control-plane")
    system_supervisor_label = os.environ.get(
        "DARWIN_SYSTEM_SUPERVISOR_LABEL", "com." + fleet + ".supervisor"
    )
    hermes_label = os.environ.get("HERMES_LAUNCHD_LABEL", "com." + fleet + ".hermes-gateway")
    openclaw_label = os.environ.get("OPENCLAW_LAUNCHD_LABEL", "com." + fleet + ".openclaw-gateway")
    agent_label = os.environ.get("MAC_AGENT_LAUNCHD_LABEL", "com." + fleet + ".agent")
    qdrant_label = "com." + fleet + ".qdrant"
    mac_prog = os.environ.get("MAC_SUPERVISORD_PROG", fleet + "-control-plane")
    hermes_prog = os.environ.get("HERMES_SUPERVISORD_PROG", fleet + "-hermes-gateway")
    openclaw_prog = os.environ.get("OPENCLAW_SUPERVISORD_PROG", fleet + "-openclaw-gateway")
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

manifest = {
    "schema_version": 1,
    "stage": stage,
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "agent": os.environ["AGENT"],
    "os_kind": os.environ["OS_KIND"],
    "deploy": {
        "timestamp": os.environ["DEPLOY_TS"],
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
    },
    "rollback": str(Path(os.environ["LOG_DIR"]) / "rollback-latest.sh"),
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

write_rollback_script() {
  cat > "$ROLLBACK_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail

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
MAC_UNIT_BACKUP='$MAC_UNIT_BACKUP'
HERMES_UNIT_BACKUP='$HERMES_UNIT_BACKUP'
MAC_AGENT_UNIT_BACKUP='$MAC_AGENT_UNIT_BACKUP'
MAC_PLIST_BACKUP='$MAC_PLIST_BACKUP'
DARWIN_SYSTEM_PLIST_BACKUP='$DARWIN_SYSTEM_PLIST_BACKUP'
DARWIN_SYSTEM_LAUNCHD_ACTIVE='$DARWIN_SYSTEM_LAUNCHD_ACTIVE'
DARWIN_GUI_LAUNCHD_ACTIVE='$DARWIN_GUI_LAUNCHD_ACTIVE'
DARWIN_SYSTEM_SUPERVISOR_LABEL='$DARWIN_SYSTEM_SUPERVISOR_LABEL'
DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP='$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP'
DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE='$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE'
HERMES_PLIST_BACKUP='$HERMES_PLIST_BACKUP'
MAC_AGENT_PLIST_BACKUP='$MAC_AGENT_PLIST_BACKUP'
MAC_SERVICE_NAME='$MAC_SERVICE_NAME'
HERMES_SERVICE_NAME='$HERMES_SERVICE_NAME'
OPENCLAW_SERVICE_NAME='$OPENCLAW_SERVICE_NAME'
MAC_AGENT_SERVICE_NAME='$MAC_AGENT_SERVICE_NAME'
MAC_LAUNCHD_LABEL='$MAC_LAUNCHD_LABEL'
HERMES_LAUNCHD_LABEL='$HERMES_LAUNCHD_LABEL'
OPENCLAW_LAUNCHD_LABEL='$OPENCLAW_LAUNCHD_LABEL'
MAC_AGENT_LAUNCHD_LABEL='$MAC_AGENT_LAUNCHD_LABEL'
MAC_SUPERVISORD_PROG='$MAC_SUPERVISORD_PROG'
HERMES_SUPERVISORD_PROG='$HERMES_SUPERVISORD_PROG'
OPENCLAW_SUPERVISORD_PROG='$OPENCLAW_SUPERVISORD_PROG'
AGENT_SUPERVISORD_PROG='$AGENT_SUPERVISORD_PROG'
ROLLBACK_TS="\$(date -u +%Y%m%dT%H%M%SZ)"

restore_dir() {
  local backup="\$1" dest="\$2" current_backup
  [ -n "\$backup" ] || return 0
  [ -d "\$backup" ] || return 0
  current_backup="\$MAC_HOME/backups/rollback-current.\$(basename "\$dest").\$ROLLBACK_TS"
  if [ -e "\$dest" ]; then
    mv -f "\$dest" "\$current_backup"
  fi
  command cp -a "\$backup" "\$dest"
}

wait_control_plane_health() {
  local deadline=\$(( \$(date +%s) + 60 ))
  while :; do
    if curl -fsS --max-time 2 "http://127.0.0.1:\$MAC_PORT/health" >/dev/null 2>&1; then
      return 0
    fi
    if [ "\$(date +%s)" -ge "\$deadline" ]; then
      echo "rollback failed: restored control plane did not become healthy" >&2
      return 1
    fi
    sleep 1
  done
}

wait_system_job_unloaded() {
  local label="\$1" deadline=\$(( \$(date +%s) + 30 ))
  while sudo -n launchctl print "system/\$label" >/dev/null 2>&1; do
    if [ "\$(date +%s)" -ge "\$deadline" ]; then
      echo "rollback failed: system launchd job remained loaded: \$label" >&2
      return 1
    fi
    sleep 1
  done
}

wait_gui_job_unloaded() {
  local uid="\$1" label="\$2" deadline=\$(( \$(date +%s) + 30 ))
  while launchctl print "gui/\$uid/\$label" >/dev/null 2>&1; do
    if [ "\$(date +%s)" -ge "\$deadline" ]; then
      echo "rollback failed: GUI launchd job remained loaded: \$label" >&2
      return 1
    fi
    sleep 1
  done
}

wait_control_plane_stopped() {
  local deadline=\$(( \$(date +%s) + 30 ))
  while /usr/bin/nc -z -w 1 127.0.0.1 "\$MAC_PORT" >/dev/null 2>&1; do
    if [ "\$(date +%s)" -ge "\$deadline" ]; then
      echo "rollback failed: control-plane port remained open" >&2
      return 1
    fi
    sleep 1
  done
}

case "\${SUPERVISOR_KIND:-\$OS_KIND}" in
  systemd|linux)
    sudo systemctl stop "\$MAC_AGENT_SERVICE_NAME" "\$HERMES_SERVICE_NAME" "\$OPENCLAW_SERVICE_NAME" "\$MAC_SERVICE_NAME" >/dev/null 2>&1 || true
    ;;
  supervisord)
    supervisorctl stop "\$AGENT_SUPERVISORD_PROG" "\$HERMES_SUPERVISORD_PROG" "\$OPENCLAW_SUPERVISORD_PROG" "\$MAC_SUPERVISORD_PROG" >/dev/null 2>&1 || true
    sudo supervisorctl stop "\$AGENT_SUPERVISORD_PROG" "\$HERMES_SUPERVISORD_PROG" "\$OPENCLAW_SUPERVISORD_PROG" "\$MAC_SUPERVISORD_PROG" >/dev/null 2>&1 || true
    ;;
  launchd|darwin)
    uid="\$(id -u)"
    sudo -n true
    if sudo -n launchctl print "system/\$DARWIN_SYSTEM_SUPERVISOR_LABEL" >/dev/null 2>&1; then
      sudo -n launchctl bootout "system/\$DARWIN_SYSTEM_SUPERVISOR_LABEL"
      wait_system_job_unloaded "\$DARWIN_SYSTEM_SUPERVISOR_LABEL"
    fi
    for label in "\$MAC_AGENT_LAUNCHD_LABEL" "\$HERMES_LAUNCHD_LABEL" "\$OPENCLAW_LAUNCHD_LABEL" "\$MAC_LAUNCHD_LABEL"; do
      if launchctl print "gui/\$uid/\$label" >/dev/null 2>&1; then
        launchctl bootout "gui/\$uid/\$label"
        wait_gui_job_unloaded "\$uid" "\$label"
      fi
    done
    if sudo -n launchctl print "system/\$MAC_LAUNCHD_LABEL" >/dev/null 2>&1; then
      sudo -n launchctl bootout "system/\$MAC_LAUNCHD_LABEL"
      wait_system_job_unloaded "\$MAC_LAUNCHD_LABEL"
    fi
    wait_control_plane_stopped
    ;;
esac

restore_dir "\$SRC_BACKUP" "\$SRC_DIR"
restore_dir "\$VENV_BACKUP" "\$VENV"
restore_dir "\$HERMES_BACKUP" "\$HERMES_DIR"

case "\${SUPERVISOR_KIND:-\$OS_KIND}" in
  systemd|linux)
    [ -n "\$MAC_UNIT_BACKUP" ] && [ -f "\$MAC_UNIT_BACKUP" ] && sudo cp -f "\$MAC_UNIT_BACKUP" /etc/systemd/system/\$MAC_SERVICE_NAME
    [ -n "\$HERMES_UNIT_BACKUP" ] && [ -f "\$HERMES_UNIT_BACKUP" ] && sudo cp -f "\$HERMES_UNIT_BACKUP" /etc/systemd/system/\$HERMES_SERVICE_NAME
    [ -n "\$MAC_AGENT_UNIT_BACKUP" ] && [ -f "\$MAC_AGENT_UNIT_BACKUP" ] && sudo cp -f "\$MAC_AGENT_UNIT_BACKUP" /etc/systemd/system/\$MAC_AGENT_SERVICE_NAME
    sudo systemctl daemon-reload
    sudo systemctl disable "\$OPENCLAW_SERVICE_NAME" >/dev/null 2>&1 || true
    sudo systemctl restart "\$MAC_SERVICE_NAME" "\$HERMES_SERVICE_NAME" "\$MAC_AGENT_SERVICE_NAME"
    ;;
  supervisord)
    supervisorctl reread >/dev/null 2>&1 || sudo supervisorctl reread >/dev/null 2>&1 || true
    supervisorctl update >/dev/null 2>&1 || sudo supervisorctl update >/dev/null 2>&1 || true
    supervisorctl restart "\$MAC_SUPERVISORD_PROG" "\$HERMES_SUPERVISORD_PROG" "\$AGENT_SUPERVISORD_PROG" >/dev/null 2>&1 || \
      sudo supervisorctl restart "\$MAC_SUPERVISORD_PROG" "\$HERMES_SUPERVISORD_PROG" "\$AGENT_SUPERVISORD_PROG" >/dev/null 2>&1 || true
    ;;
  launchd|darwin)
    mkdir -p "\$HOME/Library/LaunchAgents"
    if [ -n "\$MAC_PLIST_BACKUP" ] && [ -f "\$MAC_PLIST_BACKUP" ]; then
      cp -f "\$MAC_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/\$MAC_LAUNCHD_LABEL.plist"
    else
      rm -f "\$HOME/Library/LaunchAgents/\$MAC_LAUNCHD_LABEL.plist"
    fi
    if [ -n "\$DARWIN_SYSTEM_PLIST_BACKUP" ] && [ -f "\$DARWIN_SYSTEM_PLIST_BACKUP" ]; then
      sudo -n cp -f "\$DARWIN_SYSTEM_PLIST_BACKUP" "/Library/LaunchDaemons/\$MAC_LAUNCHD_LABEL.plist"
      sudo -n chown root:wheel "/Library/LaunchDaemons/\$MAC_LAUNCHD_LABEL.plist"
      sudo -n chmod 0644 "/Library/LaunchDaemons/\$MAC_LAUNCHD_LABEL.plist"
    else
      sudo -n rm -f "/Library/LaunchDaemons/\$MAC_LAUNCHD_LABEL.plist"
    fi
    if [ -n "\$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP" ] && [ -f "\$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP" ]; then
      sudo -n cp -f "\$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP" "/Library/LaunchDaemons/\$DARWIN_SYSTEM_SUPERVISOR_LABEL.plist"
      sudo -n chown root:wheel "/Library/LaunchDaemons/\$DARWIN_SYSTEM_SUPERVISOR_LABEL.plist"
      sudo -n chmod 0644 "/Library/LaunchDaemons/\$DARWIN_SYSTEM_SUPERVISOR_LABEL.plist"
    fi
    [ -n "\$HERMES_PLIST_BACKUP" ] && [ -f "\$HERMES_PLIST_BACKUP" ] && cp -f "\$HERMES_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/\$HERMES_LAUNCHD_LABEL.plist"
    [ -n "\$MAC_AGENT_PLIST_BACKUP" ] && [ -f "\$MAC_AGENT_PLIST_BACKUP" ] && cp -f "\$MAC_AGENT_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/\$MAC_AGENT_LAUNCHD_LABEL.plist"
    uid="\$(id -u)"
    launchctl disable "gui/\$uid/\$OPENCLAW_LAUNCHD_LABEL" >/dev/null 2>&1 || true
    if [ "\$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ]; then
      sudo -n launchctl bootstrap system "/Library/LaunchDaemons/\$MAC_LAUNCHD_LABEL.plist"
      sudo -n launchctl kickstart -k "system/\$MAC_LAUNCHD_LABEL"
    elif [ "\$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ]; then
      launchctl bootstrap "gui/\$uid" "\$HOME/Library/LaunchAgents/\$MAC_LAUNCHD_LABEL.plist"
      launchctl kickstart -k "gui/\$uid/\$MAC_LAUNCHD_LABEL"
    fi
    if [ "\$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ] || [ "\$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ]; then
      wait_control_plane_health
    fi
    if [ "\$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" = 1 ]; then
      sudo -n launchctl bootstrap system "/Library/LaunchDaemons/\$DARWIN_SYSTEM_SUPERVISOR_LABEL.plist"
      sudo -n launchctl kickstart -k "system/\$DARWIN_SYSTEM_SUPERVISOR_LABEL"
      sudo -n launchctl print "system/\$DARWIN_SYSTEM_SUPERVISOR_LABEL" >/dev/null
      if [ "\$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ]; then
        sudo -n launchctl print "system/\$MAC_LAUNCHD_LABEL" >/dev/null
      fi
      wait_control_plane_health
    fi
    launchctl bootstrap "gui/\$uid" "\$HOME/Library/LaunchAgents/\$HERMES_LAUNCHD_LABEL.plist" >/dev/null 2>&1 || launchctl kickstart -k "gui/\$uid/\$HERMES_LAUNCHD_LABEL"
    launchctl bootstrap "gui/\$uid" "\$HOME/Library/LaunchAgents/\$MAC_AGENT_LAUNCHD_LABEL.plist" >/dev/null 2>&1 || launchctl kickstart -k "gui/\$uid/\$MAC_AGENT_LAUNCHD_LABEL"
    ;;
esac

echo "rollback complete from $DEPLOY_TS"
EOF
  # Rollback script contains sensitive paths; restrict to owner-only execution (0700).
  chmod 700 "$ROLLBACK_SCRIPT"
  # cp preserves the 0700 mode, so ROLLBACK_LATEST also gets owner-only permissions.
  cp -f "$ROLLBACK_SCRIPT" "$ROLLBACK_LATEST"
}

backup_existing_artifacts() {
  if [ -d "$SRC_DIR" ]; then
    SRC_BACKUP="$MAC_HOME/backups/mac-src.${AGENT}.${DEPLOY_TS}"
    log "backing up existing mac source to $SRC_BACKUP"
    mv -f "$SRC_DIR" "$SRC_BACKUP"
  fi
  if [ -d "$VENV" ]; then
    VENV_BACKUP="$MAC_HOME/backups/venv.${AGENT}.${DEPLOY_TS}"
    log "backing up existing mac venv to $VENV_BACKUP"
    mv -f "$VENV" "$VENV_BACKUP"
  fi
  if [ -d "$HERMES_DIR" ]; then
    HERMES_BACKUP="$MAC_HOME/backups/hermes-agent.${AGENT}.${DEPLOY_TS}"
    log "backing up existing Hermes checkout to $HERMES_BACKUP"
    mv -f "$HERMES_DIR" "$HERMES_BACKUP"
  fi
  write_rollback_script
}

capture_darwin_launchd_prestate() {
  [ "$SUPERVISOR_KIND" = "launchd" ] || return 0
  local uid system_plist system_supervisor_plist gui_plist
  if ! sudo -n true; then
    log "ERROR: passwordless sudo is required to inspect system launchd state"
    return 1
  fi
  uid="$(id -u)"
  system_plist="/Library/LaunchDaemons/${MAC_LAUNCHD_LABEL}.plist"
  system_supervisor_plist="/Library/LaunchDaemons/${DARWIN_SYSTEM_SUPERVISOR_LABEL}.plist"
  gui_plist="$HOME/Library/LaunchAgents/${MAC_LAUNCHD_LABEL}.plist"

  if sudo -n launchctl print "system/$MAC_LAUNCHD_LABEL" >/dev/null 2>&1; then
    DARWIN_SYSTEM_LAUNCHD_ACTIVE=1
  fi
  if launchctl print "gui/$uid/$MAC_LAUNCHD_LABEL" >/dev/null 2>&1; then
    DARWIN_GUI_LAUNCHD_ACTIVE=1
  fi
  if sudo -n launchctl print "system/$DARWIN_SYSTEM_SUPERVISOR_LABEL" >/dev/null 2>&1; then
    DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE=1
  fi

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

  if sudo -n test -f "$system_plist"; then
    DARWIN_SYSTEM_PLIST_BACKUP="$MAC_HOME/backups/${MAC_LAUNCHD_LABEL}.${AGENT}.${DEPLOY_TS}.system.plist"
    log "backing up system control-plane service to $DARWIN_SYSTEM_PLIST_BACKUP"
    sudo -n cp -f "$system_plist" "$DARWIN_SYSTEM_PLIST_BACKUP"
    sudo -n chown "$(id -u):$(id -g)" "$DARWIN_SYSTEM_PLIST_BACKUP"
    chmod 0600 "$DARWIN_SYSTEM_PLIST_BACKUP"
  fi
  if [ -f "$gui_plist" ]; then
    MAC_PLIST_BACKUP="$MAC_HOME/backups/${MAC_LAUNCHD_LABEL}.${AGENT}.${DEPLOY_TS}.plist"
    log "backing up GUI control-plane service to $MAC_PLIST_BACKUP"
    cp -f "$gui_plist" "$MAC_PLIST_BACKUP"
    chmod 0600 "$MAC_PLIST_BACKUP"
  fi
  if sudo -n test -f "$system_supervisor_plist"; then
    DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP="$MAC_HOME/backups/${DARWIN_SYSTEM_SUPERVISOR_LABEL}.${AGENT}.${DEPLOY_TS}.system.plist"
    log "backing up system control-plane supervisor to $DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP"
    sudo -n cp -f "$system_supervisor_plist" "$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP"
    sudo -n chown "$(id -u):$(id -g)" "$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP"
    chmod 0600 "$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP"
  fi

  # This first rollback version restores the original service topology.  It is
  # rewritten with source/venv backup paths before artifact replacement.
  write_rollback_script
}

wait_for_system_launchd_job_unloaded() {
  local label="$1" deadline=$(( $(date +%s) + 30 ))
  while sudo -n launchctl print "system/$label" >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "ERROR: system launchd job remained loaded after bootout: $label"
      return 1
    fi
    sleep 1
  done
}

wait_for_local_control_plane_stop() {
  local deadline=$(( $(date +%s) + 30 ))
  while :; do
    if "$PY" - "$MAC_PORT" <<'PY'
import socket
import sys

port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.25)
    listening = sock.connect_ex(("127.0.0.1", port)) == 0
raise SystemExit(1 if listening else 0)
PY
    then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "ERROR: local control-plane port $MAC_PORT remained open after service stop"
      return 1
    fi
    sleep 1
  done
}

wait_for_local_control_plane_health() {
  local deadline=$(( $(date +%s) + 60 ))
  while :; do
    if curl -fsS --max-time 2 "http://127.0.0.1:${MAC_PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "ERROR: local control plane did not become healthy on port $MAC_PORT"
      return 1
    fi
    sleep 1
  done
}

stop_systemd_service_if_present() {
  local unit="$1" load_state active_state
  if ! load_state="$(sudo systemctl show "$unit" -p LoadState --value 2>/dev/null)"; then
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
  if ! sudo systemctl stop "$unit" >/dev/null 2>&1; then
    log "ERROR: failed to stop systemd unit $unit"
    return 1
  fi
  if ! active_state="$(sudo systemctl show "$unit" -p ActiveState --value 2>/dev/null)"; then
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

stop_supervisord_program_if_present() {
  local program="$1" status status_rc=0
  status="$(run_supervisorctl status "$program" 2>&1)" \
    && status_rc=0 || status_rc=$?
  case "$status" in
    *RUNNING*|*STARTING*|*BACKOFF*)
      if ! run_supervisorctl stop "$program" >/dev/null 2>&1; then
        log "ERROR: failed to stop supervisord program $program"
        return 1
      fi
      ;;
    *STOPPED*|*EXITED*|*FATAL*|*"no such process"*) return 0 ;;
    *)
      log "ERROR: could not inspect supervisord program $program (status=$status_rc)"
      return 1
      ;;
  esac
  status="$(run_supervisorctl status "$program" 2>&1)" \
    && status_rc=0 || status_rc=$?
  case "$status" in
    *STOPPED*|*EXITED*|*FATAL*|*"no such process"*) return 0 ;;
    *)
      log "ERROR: supervisord program $program did not become inactive (status=$status_rc)"
      return 1
      ;;
  esac
}

stop_existing_services_for_deploy() {
  log "stopping existing mac services for artifact replacement"
  case "$SUPERVISOR_KIND" in
    systemd)
      stop_systemd_service_if_present "$MAC_AGENT_SERVICE_NAME"
      stop_systemd_service_if_present "$HERMES_SERVICE_NAME"
      stop_systemd_service_if_present "$OPENCLAW_SERVICE_NAME"
      stop_systemd_service_if_present "$MAC_SERVICE_NAME"
      ;;
    supervisord)
      stop_supervisord_program_if_present "$AGENT_SUPERVISORD_PROG"
      stop_supervisord_program_if_present "$HERMES_SUPERVISORD_PROG"
      stop_supervisord_program_if_present "$OPENCLAW_SUPERVISORD_PROG"
      stop_supervisord_program_if_present "$MAC_SUPERVISORD_PROG"
      ;;
    launchd)
      local uid
      uid="$(id -u)"
      if [ "$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" = 1 ]; then
        sudo -n launchctl bootout "system/$DARWIN_SYSTEM_SUPERVISOR_LABEL"
        wait_for_system_launchd_job_unloaded "$DARWIN_SYSTEM_SUPERVISOR_LABEL"
      fi
      launchctl bootout "gui/$uid/$MAC_AGENT_LAUNCHD_LABEL" >/dev/null 2>&1 || true
      launchctl bootout "gui/$uid/$HERMES_LAUNCHD_LABEL" >/dev/null 2>&1 || true
      launchctl bootout "gui/$uid/$OPENCLAW_LAUNCHD_LABEL" >/dev/null 2>&1 || true
      launchctl bootout "gui/$uid/$MAC_LAUNCHD_LABEL" >/dev/null 2>&1 || true
      local stopped_label
      for stopped_label in \
          "$MAC_AGENT_LAUNCHD_LABEL" \
          "$HERMES_LAUNCHD_LABEL" \
          "$OPENCLAW_LAUNCHD_LABEL"; do
        if launchctl print "gui/$uid/$stopped_label" >/dev/null 2>&1; then
          log "ERROR: launchd job $stopped_label remained loaded after bootout"
          return 1
        fi
      done
      if [ "$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ] \
        && launchctl print "gui/$uid/$MAC_LAUNCHD_LABEL" >/dev/null 2>&1; then
        log "ERROR: GUI control-plane job remained loaded after bootout"
        return 1
      fi
      if [ "$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ]; then
        sudo -n launchctl bootout "system/$MAC_LAUNCHD_LABEL"
        wait_for_system_launchd_job_unloaded "$MAC_LAUNCHD_LABEL"
      fi
      if control_plane_enabled \
        || [ "$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ] \
        || [ "$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ]; then
        wait_for_local_control_plane_stop
      fi
      ;;
  esac
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
  local agent_id="$1" deadline now count summary_path="$LOG_DIR/mac-agent-drain.json"
  deadline=$(( $(date +%s) + ${DRAIN_TIMEOUT_SECONDS:-1800} ))
  while :; do
    if mac_api_json GET "/tasks" > "$summary_path.tasks"; then
      count="$($PY - "$summary_path.tasks" "$agent_id" "$summary_path" <<'PY'
import json
import sys
import time
from pathlib import Path

tasks_path = Path(sys.argv[1])
agent_id = sys.argv[2]
summary_path = Path(sys.argv[3])
tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
active = [
    task
    for task in tasks
    if task.get("owner_agent_id") == agent_id
    and task.get("lease_id")
    and task.get("state") in {"claimed", "running"}
]
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
summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(len(active))
PY
)"
      if [ "$count" = "0" ]; then
        log "mac-agent drain complete: no active leases for $agent_id"
        return 0
      fi
      log "mac-agent drain waiting: $count active lease(s) for $agent_id"
    else
      log "WARNING: could not query active leases during drain"
    fi
    now="$(date +%s)"
    if [ "$now" -ge "$deadline" ]; then
      log "ERROR: drain timed out with active leases for $agent_id"
      return 1
    fi
    sleep "${DRAIN_POLL_SECONDS:-10}"
  done
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
  local target="$MAC_HOME/bin/gh" existing=""
  mkdir -p "$MAC_HOME/bin"
  if [ -x "$target" ]; then
    log "GitHub CLI already installed at $target"
    "$target" --version > "$LOG_DIR/gh-version.txt" 2>&1 || true
    return 0
  fi
  existing="$(command -v gh 2>/dev/null || true)"
  if [ -z "$existing" ]; then
    for candidate in /opt/homebrew/bin/gh /usr/local/bin/gh "$HOME/.local/bin/gh" "$HOME/bin/gh"; do
      if [ -x "$candidate" ]; then
        existing="$candidate"
        break
      fi
    done
  fi
  if [ -z "$existing" ]; then
    if [ "$OS_KIND" = "darwin" ] && command -v brew >/dev/null 2>&1; then
      log "installing GitHub CLI with Homebrew"
      HOMEBREW_NO_AUTO_UPDATE=1 brew install gh >/dev/null
      existing="$(command -v gh 2>/dev/null || true)"
    elif [ "$OS_KIND" = "linux" ] && command -v apt-get >/dev/null 2>&1; then
      log "installing GitHub CLI with apt"
      if ! (sudo apt-get update >/dev/null && sudo apt-get install -y gh >/dev/null); then
        if command -v curl >/dev/null 2>&1 && command -v gpg >/dev/null 2>&1 && command -v dpkg >/dev/null 2>&1; then
          log "configuring upstream GitHub CLI apt repository"
          sudo install -m 0755 -d /etc/apt/keyrings
          curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
            | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
          sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
          printf 'deb [arch=%s signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' "$(dpkg --print-architecture)" \
            | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
          sudo apt-get update >/dev/null
          sudo apt-get install -y gh >/dev/null
        else
          log "ERROR: gh is required but apt install failed and curl/gpg/dpkg fallback tools are unavailable"
          exit 1
        fi
      fi
      existing="$(command -v gh 2>/dev/null || true)"
    fi
  fi
  if [ -z "$existing" ] || [ ! -x "$existing" ]; then
    log "ERROR: GitHub CLI (gh) is required for worker publication but could not be installed"
    exit 1
  fi
  if ln -sf "$existing" "$target" 2>/dev/null; then
    :
  else
    cp -f "$existing" "$target"
    chmod 0755 "$target"
  fi
  "$target" --version > "$LOG_DIR/gh-version.txt"
  log "GitHub CLI ready at $target"
}

install_codegraph_cli() {
  local target="$MAC_HOME/bin/codegraph"
  local bundle="$MAC_HOME/lib/codegraph/versions/$MAC_REVIEWED_CODEGRAPH_VERSION"
  mkdir -p "$MAC_HOME/bin"
  log "installing reviewed CodeGraph $MAC_REVIEWED_CODEGRAPH_VERSION native bundle"
  if ! mac_install_reviewed_codegraph \
      "$bundle" "$target" "$MAC_HOME/cache/reviewed-assets"; then
    log "ERROR: reviewed CodeGraph asset installation failed"
    return 1
  fi
  if run_without_deploy_credentials \
      "$target" install --yes > "$LOG_DIR/codegraph-install.txt" 2>&1; then
    run_without_deploy_credentials \
      "$target" --version > "$LOG_DIR/codegraph-version.txt" 2>&1
    grep -qx "${MAC_REVIEWED_CODEGRAPH_VERSION#v}" "$LOG_DIR/codegraph-version.txt" || {
      log "ERROR: installed CodeGraph version differs from reviewed version"
      return 1
    }
    log "CodeGraph CLI ready at $target"
  else
    log "ERROR: codegraph install failed; see $LOG_DIR/codegraph-install.txt"
    return 1
  fi
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
  sudo systemctl daemon-reload
  sudo systemctl enable "$svc" >/dev/null 2>&1 || true
  sudo systemctl restart "$svc" || log "WARNING: $svc failed to start (journalctl -u $svc)"
}

install_gpu_gen_server() {
  # media-01: durable local media-gen servers for a GPU agent — image (:8189),
  # audio (:8190), video (:8191) — each as a GPU-gated systemd unit serving the
  # routes the agent advertises (#1/B1b). Provisions one shared venv
  # (torch/diffusers/transformers). GPU-gated (like Omniverse skills) +
  # systemd-only. Entirely non-fatal: a GPU-dep hiccup must never block the deploy.
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

log "deploy log: $DEPLOY_LOG"
ensure_dns_resolution
ensure_venv_support
SUPERVISOR_KIND="$(detect_supervisor)"
export SUPERVISOR_KIND
log "selected supervisor: $SUPERVISOR_KIND (requested: ${SUPERVISOR_REQUESTED:-auto})"
disk_hygiene_report "before-cleanup" "$LOG_DIR/disk-before-cleanup-${DEPLOY_TS}.json"
cleanup_obsolete_deploy_artifacts
disk_hygiene_report "after-cleanup" "$LOG_DIR/disk-after-cleanup-${DEPLOY_TS}.json"
capture_darwin_launchd_prestate
write_deploy_manifest "pre" "$MANIFEST_PRE"
# Resolve and verify repository auth before draining the worker or replacing
# source, so a missing required credential leaves the existing node untouched.
install_github_cli || true
configure_github_https_credentials
drain_mac_agent_before_deploy
stop_existing_services_for_deploy
backup_existing_artifacts
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

install_codegraph_cli
initialize_codegraph_repository "$SRC_DIR"

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
reconcile_disabled_optional_openshell
prepare_work_package_pipeline_storage
# gketun-02: the hub (shared-services manager) owns the reverse-tunnel keypair it
# uses to dial spokes, so it generates that key (ensure_hub_tunnel_key). Every
# spoke must AUTHORIZE the hub's pubkey (install_hub_tunnel_pubkey) so the hub's
# `ssh -R` reverse tunnel can connect. This was previously gated on
# WORKER_MODE=loop, which sent loop-mode spokes down the ensure_hub_tunnel_key
# branch and SKIPPED authorizing the hub key — leaving the reverse tunnel
# permanently unauthenticated (no shared services, no agent registration). Decide
# by role (hub vs spoke), not by loop-vs-heartbeat mode.
if [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]; then
  ensure_hub_tunnel_key
else
  if [ "$WORKER_MODE" = "loop" ]; then
    ensure_hub_tunnel_key
  fi
  install_hub_tunnel_pubkey
  wait_for_hub_reverse_tunnel
fi
install_github_review_key
install_or_validate_shared_services
write_hermes_memory_topology

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
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/mac" "$HOME/.local/bin/mac"

# OpenShell owns the schema used by every later sandbox.  Upgrade and prove the
# final gateway now, while the node is drained and all prior services remain
# stopped.  OpenClaw is installed only after this returns successfully.
bootstrap_enabled_openshell

install_or_validate_web_search_service
write_hermes_web_search_config
install_or_validate_publish_service

log "using vendored in-tree Hermes runtime (ADR 0001 hu-04; no upstream clone)"
# The Hermes runtime ships pinned + patched in the mac package at
# $HERMES_VENDORED and runs in-process from the single mac venv ($VENV) — there
# is no upstream clone and no separate hermes venv. HERMES_DIR stays a path
# symbol for the (guarded) backup/restore logic but is intentionally NOT created.
git -C "$SRC_DIR" rev-parse HEAD > "$LOG_DIR/hermes-vendored-rev.txt" 2>/dev/null || true
cat "$HERMES_VENDORED/SNAPSHOT_PIN" > "$LOG_DIR/hermes-vendored-pin.txt" 2>/dev/null || true
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

if [ -n "$ACC_DB" ]; then
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
else
  log "no ACC SQLite database found under ~/.acc/data; classifying host"
  write_migration_status "no_acc_sqlite_db" ""
fi

install_mac_control_wrapper() {
  local wrapper="$MAC_HOME/bin/mac-service"
  mkdir -p "$MAC_HOME/bin"
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
    "$installer" prepare
}

verify_openclaw_gateway() {
  local installer="$SRC_DIR/deploy/openclaw/install-openclaw-gateway.sh"
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

rollback_openclaw_gateway() {
  local installer="$SRC_DIR/deploy/openclaw/install-openclaw-gateway.sh"
  MAC_OPENCLAW_FLEET_NAME="$FLEET_NAME" \
  MAC_OPENCLAW_SUPERVISOR="$SUPERVISOR_KIND" \
    "$installer" rollback
}

install_linux_service() {
  local unit="/etc/systemd/system/${MAC_SERVICE_NAME}" restart_since
  install_hermes_gateway_wrapper
  install_mac_agent_wrapper
  if sudo test -f "$unit"; then
    MAC_UNIT_BACKUP="$MAC_HOME/backups/${MAC_SERVICE_NAME}.${AGENT}.${DEPLOY_TS}"
    sudo cp -f "$unit" "$MAC_UNIT_BACKUP"
    sudo chown "$USER" "$MAC_UNIT_BACKUP" || true
    write_rollback_script
  fi
  if control_plane_enabled; then
    log "installing hub control-plane systemd service $unit"
    install_mac_control_wrapper
    sudo tee "$unit" >/dev/null <<EOF
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
    sudo systemctl daemon-reload
    sudo systemctl enable "$MAC_SERVICE_NAME"
    restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    sudo systemctl restart "$MAC_SERVICE_NAME"
    sleep 3
    sudo systemctl --no-pager -l status "$MAC_SERVICE_NAME" || true
    sudo journalctl -u "$MAC_SERVICE_NAME" --since "$restart_since" --no-pager > "$LOG_DIR/mac-service-journal.txt" || true
    escrow_router_provider_keys
  else
    log "spoke role: removing stale local control-plane systemd service"
    sudo systemctl disable --now "$MAC_SERVICE_NAME" >/dev/null 2>&1 || true
    sudo rm -f "$unit"
    sudo systemctl daemon-reload
    : > "$LOG_DIR/mac-service-not-installed.txt"
  fi
  scrub_spoke_provider_secrets
  sync_messaging_config
  # Route to the configured gateway implementation. Stock OpenClaw is the
  # primary; Hermes remains the explicit rollback path; NemoClaw is reference
  # compatibility only.
  case "${HERMES_GATEWAY_IMPL:-hermes}" in
    openclaw)
      install_linux_openclaw_service ;;
    nemoclaw)
      install_linux_nemoclaw_service ;;
    *)
      install_linux_hermes_service ;;
  esac
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
  if sudo test -f "$unit"; then
    sudo cp -f "$unit" "$MAC_HOME/backups/${OPENCLAW_SERVICE_NAME}.${AGENT}.${DEPLOY_TS}"
  fi
  sudo install -m 0644 "$rendered" "$unit"
  sudo systemctl daemon-reload
  sudo systemctl enable "$OPENCLAW_SERVICE_NAME"
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sudo systemctl restart "$OPENCLAW_SERVICE_NAME"
  sleep 8
  sudo journalctl -u "$OPENCLAW_SERVICE_NAME" --since "$restart_since" --no-pager \
    > "$LOG_DIR/openclaw-gateway-journal.txt" || true
  if ! verify_openclaw_gateway; then
    log "ERROR: stock OpenClaw verification failed; restoring Hermes gateway"
    rollback_openclaw_gateway || true
    return 1
  fi
  sudo systemctl disable --now "$HERMES_SERVICE_NAME" >/dev/null 2>&1 || true
  sudo systemctl reset-failed "$HERMES_SERVICE_NAME" 2>/dev/null || true
  sudo systemctl disable --now "$NEMOCLAW_SERVICE_NAME" >/dev/null 2>&1 || true
  if ! finalize_openclaw_gateway; then
    log "ERROR: OpenClaw exclusivity proof failed; restoring Hermes gateway"
    rollback_openclaw_gateway || true
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
  if control_plane_enabled; then
    control_after="$MAC_SERVICE_NAME"
  fi
  log "installing NemoClaw gateway systemd service $unit"
  if sudo test -f "$unit"; then
    local nemoclaw_unit_backup
    nemoclaw_unit_backup="$MAC_HOME/backups/${NEMOCLAW_SERVICE_NAME}.${AGENT}.${DEPLOY_TS}"
    sudo cp -f "$unit" "$nemoclaw_unit_backup"
    sudo chown "$USER" "$nemoclaw_unit_backup" || true
    write_rollback_script
  fi
  [ -f "$unit_src" ] || die "NemoClaw service template not found: $unit_src"
  sudo cp -f "$unit_src" "$unit"
  sudo systemctl daemon-reload
  sudo systemctl enable "$NEMOCLAW_SERVICE_NAME"
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sudo systemctl restart "$NEMOCLAW_SERVICE_NAME"
  sleep 5
  sudo systemctl --no-pager -l status "$NEMOCLAW_SERVICE_NAME" || true
  sudo journalctl -u "$NEMOCLAW_SERVICE_NAME" --since "$restart_since" --no-pager \
    > "$LOG_DIR/nemoclaw-gateway-journal.txt" || true

  # Stop and disable the hermes gateway service (YOLO: it must not run alongside NemoClaw).
  log "disabling hermes gateway service (YOLO: NemoClaw replaces it)"
  if sudo systemctl is-active --quiet "$HERMES_SERVICE_NAME" 2>/dev/null; then
    sudo systemctl stop "$HERMES_SERVICE_NAME"
  fi
  if sudo systemctl is-enabled --quiet "$HERMES_SERVICE_NAME" 2>/dev/null; then
    sudo systemctl disable "$HERMES_SERVICE_NAME"
  fi
  sudo systemctl reset-failed "$HERMES_SERVICE_NAME" 2>/dev/null || true
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
  local wrapper="$MAC_HOME/bin/hermes-gateway"
  mkdir -p "$MAC_HOME/bin"
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
  local wrapper="$MAC_HOME/bin/mac-agent-service"
  local selftest="$MAC_HOME/bin/mac-agent-startup-self-test"
  local executor="$MAC_HOME/bin/mac-task-executor"
  local executor_py="$MAC_HOME/bin/mac-task-executor.py"
  local report_python="$MAC_HOME/venv/bin/mac-report-python"
  mkdir -p "$MAC_HOME/bin"
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
  install -m 0755 "$SRC_DIR/deploy/mac-crash-observer.py" "$MAC_HOME/bin/mac-crash-observer"
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
  ln -sf "$executor" "$MAC_HOME/bin/mac-hermes-task-executor"
  ln -sf "$executor_py" "$MAC_HOME/bin/mac-hermes-task-executor.py"
}

install_linux_hermes_service() {
  local unit="/etc/systemd/system/${HERMES_SERVICE_NAME}" restart_since control_after=""
  sudo systemctl disable --now "$OPENCLAW_SERVICE_NAME" >/dev/null 2>&1 || true
  sudo systemctl disable --now "$NEMOCLAW_SERVICE_NAME" >/dev/null 2>&1 || true
  if control_plane_enabled; then
    control_after="$MAC_SERVICE_NAME"
  fi
  log "installing systemd service $unit"
  if sudo test -f "$unit"; then
    HERMES_UNIT_BACKUP="$MAC_HOME/backups/${HERMES_SERVICE_NAME}.${AGENT}.${DEPLOY_TS}"
    sudo cp -f "$unit" "$HERMES_UNIT_BACKUP"
    sudo chown "$USER" "$HERMES_UNIT_BACKUP" || true
    write_rollback_script
  fi
  sudo tee "$unit" >/dev/null <<EOF
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
  sudo systemctl daemon-reload
  sudo systemctl enable "$HERMES_SERVICE_NAME"
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sudo systemctl restart "$HERMES_SERVICE_NAME"
  sleep 5
  sudo systemctl --no-pager -l status "$HERMES_SERVICE_NAME" || true
  sudo journalctl -u "$HERMES_SERVICE_NAME" --since "$restart_since" --no-pager > "$LOG_DIR/hermes-gateway-journal.txt" || true
  install_linux_agent_service
}

install_linux_agent_service() {
  local unit="/etc/systemd/system/${MAC_AGENT_SERVICE_NAME}" restart_since control_after=""
  if control_plane_enabled; then
    control_after="$MAC_SERVICE_NAME"
  fi
  log "installing systemd service $unit"
  if sudo test -f "$unit"; then
    MAC_AGENT_UNIT_BACKUP="$MAC_HOME/backups/${MAC_AGENT_SERVICE_NAME}.${AGENT}.${DEPLOY_TS}"
    sudo cp -f "$unit" "$MAC_AGENT_UNIT_BACKUP"
    sudo chown "$USER" "$MAC_AGENT_UNIT_BACKUP" || true
    write_rollback_script
  fi
  sudo tee "$unit" >/dev/null <<EOF
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
  sudo systemctl daemon-reload
  sudo systemctl enable "$MAC_AGENT_SERVICE_NAME"
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if truthy "$DEFER_AGENT_RESTART"; then
    log "deferring mac-agent restart until post-manifest reconciliation"
  else
    sudo systemctl restart "$MAC_AGENT_SERVICE_NAME"
    sleep 3
    sudo systemctl show "$MAC_AGENT_SERVICE_NAME" \
      -p LoadState \
      -p ActiveState \
      -p SubState \
      -p UnitFileState \
      -p MainPID \
      -p NRestarts || true
    sudo journalctl -u "$MAC_AGENT_SERVICE_NAME" --since "$restart_since" --no-pager > "$LOG_DIR/mac-agent-journal.txt" || true
  fi
}


install_supervisord_service() {
  local conf_dir conf restart_since control_program="" gateway_program active_gateway_program
  local agent_autostart=true
  if truthy "$DEFER_AGENT_RESTART"; then
    agent_autostart=false
  fi
  conf_dir="$(supervisord_conf_dir)"
  conf="$conf_dir/$MAC_SUPERVISORD_CONF_NAME"
  log "installing supervisord programs in $conf"
  install_hermes_gateway_wrapper
  install_mac_agent_wrapper
  install -m 0755 "$SRC_DIR/deploy/agent-resource-health.sh" "$MAC_HOME/bin/agent-resource-health"
  if [ "${HERMES_GATEWAY_IMPL:-hermes}" = "openclaw" ]; then
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
  elif [ "${HERMES_GATEWAY_IMPL:-hermes}" = "none" ]; then
    # A pure worker must not retain or start either chat-gateway program.  An
    # empty block also lets ``supervisorctl update`` remove stale gateway
    # programs from a node that was converted from a conversational role.
    active_gateway_program=""
    gateway_program=""
  else
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
  fi
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
    sudo cp -f "$conf" "$MAC_UNIT_BACKUP"
    sudo chown "$USER" "$MAC_UNIT_BACKUP" || true
    write_rollback_script
  fi
  sudo install -d -m 0755 "$conf_dir"
  sudo tee "$conf" >/dev/null <<EOF
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
  # Remove stale worker-side hub tunnel conf from previous deploy approach
  sudo rm -f "$conf_dir/${FLEET_NAME}-hub-tunnel.conf" 2>/dev/null || true
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
    run_supervisorctl stop "$AGENT_SUPERVISORD_PROG" >/dev/null 2>&1 || true
  fi
  if control_plane_enabled; then
    run_supervisorctl restart "$MAC_SUPERVISORD_PROG" >/dev/null 2>&1 || run_supervisorctl start "$MAC_SUPERVISORD_PROG" >/dev/null
    sleep 3
  else
    log "spoke role: supervisord control-plane program is absent"
    : > "$LOG_DIR/mac-service-not-installed.txt"
  fi
  # Escrow the router upstream key + scrub spoke secrets + sync messaging BEFORE
  # the gateway/agent start, mirroring the systemd (install_linux_hermes_service)
  # and launchd paths. Without this the supervisord path left the hub vault empty,
  # so the router forwarded keyless (upstream 401) and the agent self-test failed.
  # Needs the control plane reachable, so wait briefly for it.
  if control_plane_enabled; then
    for _i in $(seq 1 30); do
      curl -fsS -o /dev/null "http://127.0.0.1:${MAC_PORT:-8789}/ui" 2>/dev/null && break
      sleep 1
    done
    escrow_router_provider_keys
  fi
  scrub_spoke_provider_secrets
  sync_messaging_config
  if [ -n "$active_gateway_program" ]; then
    if [ "${HERMES_GATEWAY_IMPL:-hermes}" = "openclaw" ]; then
      prepare_openclaw_gateway
    fi
    run_supervisorctl restart "$active_gateway_program" >/dev/null 2>&1 || run_supervisorctl start "$active_gateway_program" >/dev/null
    sleep 5
  else
    log "gateway_impl=none: pure worker; skipping gateway program install/restart"
  fi
  if [ "${HERMES_GATEWAY_IMPL:-hermes}" = "openclaw" ]; then
    if ! verify_openclaw_gateway; then
      log "ERROR: stock OpenClaw verification failed under supervisord; restoring Hermes gateway"
      rollback_openclaw_gateway || true
      return 1
    fi
    run_supervisorctl stop "$HERMES_SUPERVISORD_PROG" >/dev/null 2>&1 || true
    if ! finalize_openclaw_gateway; then
      log "ERROR: OpenClaw exclusivity proof failed under supervisord; restoring Hermes gateway"
      rollback_openclaw_gateway || true
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
    run_supervisorctl restart "$AGENT_SUPERVISORD_PROG" >/dev/null 2>&1 || run_supervisorctl start "$AGENT_SUPERVISORD_PROG" >/dev/null
    sleep 3
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

install_darwin_service() {
  local uid plist wrapper system_plist system_plist_staging system_plist_tmp system_supervisor_plist
  uid="$(id -u)"
  plist="$HOME/Library/LaunchAgents/${MAC_LAUNCHD_LABEL}.plist"
  wrapper="$MAC_HOME/bin/mac-service"
  system_plist="/Library/LaunchDaemons/${MAC_LAUNCHD_LABEL}.plist"
  system_plist_staging="$LOG_DIR/${MAC_LAUNCHD_LABEL}.${DEPLOY_TS}.system.plist"
  system_plist_tmp="/Library/LaunchDaemons/.${MAC_LAUNCHD_LABEL}.${DEPLOY_TS}.tmp"
  system_supervisor_plist="/Library/LaunchDaemons/${DARWIN_SYSTEM_SUPERVISOR_LABEL}.plist"
  install_hermes_gateway_wrapper
  install_mac_agent_wrapper
  mkdir -p "$MAC_HOME/bin" "$HOME/Library/LaunchAgents"
  if control_plane_enabled; then
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
    log "installing headless system launchd control plane $MAC_LAUNCHD_LABEL"
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
    if command -v plutil >/dev/null 2>&1; then
      plutil -lint "$system_plist_staging"
    fi
    sudo -n rm -f "$system_plist_tmp"
    sudo -n install -o root -g wheel -m 0644 "$system_plist_staging" "$system_plist_tmp"
    sudo -n plutil -lint "$system_plist_tmp"
    sudo -n mv -f "$system_plist_tmp" "$system_plist"
    rm -f "$system_plist_staging" "$plist"
    : > "$LOG_DIR/mac-service.log"
    sudo -n launchctl enable "system/$MAC_LAUNCHD_LABEL"
    sudo -n launchctl bootstrap system "$system_plist"
    sudo -n launchctl kickstart -k "system/$MAC_LAUNCHD_LABEL"
    sudo -n launchctl print "system/$MAC_LAUNCHD_LABEL" >/dev/null
    if launchctl print "gui/$uid/$MAC_LAUNCHD_LABEL" >/dev/null 2>&1; then
      log "ERROR: duplicate GUI control-plane job is still loaded"
      return 1
    fi
    wait_for_local_control_plane_health
    if [ "$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" = 1 ]; then
      if ! sudo -n cmp -s "$DARWIN_SYSTEM_SUPERVISOR_PLIST_BACKUP" "$system_supervisor_plist"; then
        log "ERROR: system supervisor plist changed during deployment"
        return 1
      fi
      sudo -n launchctl bootstrap system "$system_supervisor_plist"
      sudo -n launchctl kickstart -k "system/$DARWIN_SYSTEM_SUPERVISOR_LABEL"
      sudo -n launchctl print "system/$DARWIN_SYSTEM_SUPERVISOR_LABEL" >/dev/null
      sudo -n launchctl print "system/$MAC_LAUNCHD_LABEL" >/dev/null
      wait_for_local_control_plane_health
    fi
    escrow_router_provider_keys
  else
    log "spoke role: removing stale local control-plane launchd service"
    launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
    launchctl bootout "gui/$uid/$MAC_LAUNCHD_LABEL" >/dev/null 2>&1 || true
    sudo -n launchctl bootout "system/$DARWIN_SYSTEM_SUPERVISOR_LABEL" >/dev/null 2>&1 || true
    sudo -n launchctl bootout "system/$MAC_LAUNCHD_LABEL" >/dev/null 2>&1 || true
    sudo -n rm -f "$system_supervisor_plist" "$system_plist"
    rm -f "$plist" "$wrapper"
    : > "$LOG_DIR/mac-service-not-installed.txt"
  fi
  scrub_spoke_provider_secrets
  sync_messaging_config
  if [ "${HERMES_GATEWAY_IMPL:-hermes}" = "openclaw" ]; then
    install_darwin_openclaw_service "$uid"
  else
    install_darwin_hermes_service "$uid"
  fi
  install_darwin_agent_service "$uid"
}

install_darwin_openclaw_service() {
  local uid="$1" plist="$HOME/Library/LaunchAgents/${OPENCLAW_LAUNCHD_LABEL}.plist"
  prepare_openclaw_gateway
  cat > "$plist" <<EOF
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
  plutil -lint "$plist"
  launchctl bootout "gui/$uid/$OPENCLAW_LAUNCHD_LABEL" >/dev/null 2>&1 || true
  : > "$LOG_DIR/openclaw-gateway.log"
  launchctl enable "gui/$uid/$OPENCLAW_LAUNCHD_LABEL"
  launchctl bootstrap "gui/$uid" "$plist"
  # RunAtLoad starts the wrapper as part of a successful bootstrap.  A second
  # unconditional kickstart races that wrapper's stop/delete/create lifecycle.
  sleep 8
  if ! verify_openclaw_gateway; then
    log "ERROR: stock OpenClaw verification failed under launchd; restoring Hermes gateway"
    rollback_openclaw_gateway || true
    return 1
  fi
  launchctl bootout "gui/$uid/$HERMES_LAUNCHD_LABEL" >/dev/null 2>&1 || true
  launchctl disable "gui/$uid/$HERMES_LAUNCHD_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "gui/$uid/com.${FLEET_NAME}.nemoclaw-gateway" >/dev/null 2>&1 || true
  launchctl disable "gui/$uid/com.${FLEET_NAME}.nemoclaw-gateway" >/dev/null 2>&1 || true
  if ! finalize_openclaw_gateway; then
    log "ERROR: OpenClaw exclusivity proof failed under launchd; restoring Hermes gateway"
    rollback_openclaw_gateway || true
    return 1
  fi
  log "stock OpenClaw verified as exclusive launchd gateway; Hermes retained only for rollback"
}

install_darwin_hermes_service() {
  local uid="$1" plist="$HOME/Library/LaunchAgents/${HERMES_LAUNCHD_LABEL}.plist"
  launchctl bootout "gui/$uid/$OPENCLAW_LAUNCHD_LABEL" >/dev/null 2>&1 || true
  launchctl disable "gui/$uid/$OPENCLAW_LAUNCHD_LABEL" >/dev/null 2>&1 || true
  if [ -f "$plist" ]; then
    HERMES_PLIST_BACKUP="$MAC_HOME/backups/${HERMES_LAUNCHD_LABEL}.${AGENT}.${DEPLOY_TS}.plist"
    cp -f "$plist" "$HERMES_PLIST_BACKUP"
    write_rollback_script
  fi
  cat > "$plist" <<EOF
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
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$plist"
  fi
  launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
  launchctl bootout "gui/$uid/$HERMES_LAUNCHD_LABEL" >/dev/null 2>&1 || true
  : > "$LOG_DIR/hermes-gateway.log"
  launchctl enable "gui/$uid/$HERMES_LAUNCHD_LABEL"
  if ! launchctl bootstrap "gui/$uid" "$plist"; then
    launchctl kickstart -k "gui/$uid/$HERMES_LAUNCHD_LABEL"
  fi
  launchctl kickstart -k "gui/$uid/$HERMES_LAUNCHD_LABEL"
  sleep 5
  launchctl list "$HERMES_LAUNCHD_LABEL" || true
}

install_darwin_agent_service() {
  local uid="$1" plist="$HOME/Library/LaunchAgents/${MAC_AGENT_LAUNCHD_LABEL}.plist"
  log "installing launchd agent $plist"
  if [ -f "$plist" ]; then
    MAC_AGENT_PLIST_BACKUP="$MAC_HOME/backups/${MAC_AGENT_LAUNCHD_LABEL}.${AGENT}.${DEPLOY_TS}.plist"
    cp -f "$plist" "$MAC_AGENT_PLIST_BACKUP"
    write_rollback_script
  fi
  cat > "$plist" <<EOF
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
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$plist"
  fi
  launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
  launchctl bootout "gui/$uid/$MAC_AGENT_LAUNCHD_LABEL" >/dev/null 2>&1 || true
  : > "$LOG_DIR/mac-agent.log"
  launchctl enable "gui/$uid/$MAC_AGENT_LAUNCHD_LABEL"
  if truthy "$DEFER_AGENT_RESTART"; then
    log "deferring mac-agent restart until post-manifest reconciliation"
  else
    if ! launchctl bootstrap "gui/$uid" "$plist"; then
      launchctl kickstart -k "gui/$uid/$MAC_AGENT_LAUNCHD_LABEL"
    fi
    launchctl kickstart -k "gui/$uid/$MAC_AGENT_LAUNCHD_LABEL"
    sleep 3
    launchctl list "$MAC_AGENT_LAUNCHD_LABEL" || true
  fi
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

case "$SUPERVISOR_KIND" in
  systemd) install_linux_service ;;
  launchd) install_darwin_service ;;
  supervisord) install_supervisord_service ;;
  *) log "ERROR: unsupported supervisor $SUPERVISOR_KIND"; exit 1 ;;
esac

# The service installer may now have created the long-lived OpenClaw sandbox.
# Prove the API can decode it and every surviving managed container binds the
# exact supervisor version reviewed with this CLI before any post manifest can
# declare the node deployable.
verify_managed_openshell_runtime

# media-01: durable local media-gen server on GPU agents (non-fatal, self-gated).
install_gpu_gen_server || true

# media-01 Part C3: re-hydrate the agent's self-installed pip/npm footprint.
install_agent_footprint || true

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
log "deploy complete"
