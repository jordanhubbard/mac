#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# load_env_file_with_caller_precedence <path>
#
# Precedence contract: variables already set in the caller's environment always
# win over values in the env file.  Variables absent from the caller still pick
# up their values from the file.
#
# Mechanism: (1) snapshot the values of every variable we care about that the
# caller already has set, (2) source the file with set -a so any variable in it
# is exported as a default, then (3) restore each snapshotted caller value so
# the explicit process environment is never overwritten by a file default.
load_env_file_with_caller_precedence() {
  local env_file="$1"
  [ -f "$env_file" ] || return 0

  # Variables whose caller-supplied values must survive the source.
  local -a _PRECEDENCE_VARS=(
    MAC_DEPLOY_FLEETS_CONFIG
    MAC_DEPLOY_ENV_FILE
    GH_TOKEN
    GITHUB_TOKEN
    MAC_DEPLOY_GH_TOKEN
    MAC_SECRET_KEY
    MAC_API_TOKEN
    MAC_WORKER_TOKEN
    MAC_DEPLOY_OPENSHELL
    MAC_DEPLOY_OPENSHELL_ARGS
    MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE
    MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED
    MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED
    MAC_DEPLOY_WORK_PACKAGE_BUNDLE_DIR
    MAC_DEPLOY_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT
    MAC_DEPLOY_HUB_TICK_INTERVAL_SECONDS
    MAC_DEPLOY_EXECUTION_COHORT_REVISION
    MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT
    MAC_DEPLOY_EXECUTION_COHORT_SEED
    MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD
    MAC_DEPLOY_REMOTE_PHASE_TIMEOUT_SECONDS
    MAC_FLEET_COHORT_JOURNAL_DIR
    MAC_DEPLOY_HOLD_ADOPTIONS_FILE
    MAC_DEPLOY_REQUIRE_RELEASE_ALL_SELECTED
    MAC_DEPLOY_SUCCESSOR_HOLD_REASON
  )

  # Also snapshot any fleet-scoped token variables already present.
  local var
  for var in $(compgen -v | grep -E '^MAC_API_TOKEN__'); do
    _PRECEDENCE_VARS+=("$var")
  done

  # Phase 1: snapshot caller-supplied values (only for vars that are set).
  local -A _caller_snapshot
  for var in "${_PRECEDENCE_VARS[@]}"; do
    if [ -v "$var" ]; then
      _caller_snapshot["$var"]="${!var}"
    fi
  done

  # Phase 2: source the file, exporting every declaration as a default.
  set -a
  # shellcheck source=/dev/null
  . "$env_file"
  set +a

  # Phase 3: restore caller-supplied values so they win over file defaults.
  for var in "${!_caller_snapshot[@]}"; do
    export "$var"="${_caller_snapshot[$var]}"
  done
}

# Resolve the GitHub credential once on the operator host.  A fleet deploy
# should reuse an already-authenticated ``gh`` keychain login instead of
# requiring operators to copy the same token into MAC_DEPLOY_GH_TOKEN by hand.
# Explicit deployment input still wins, followed by the standard GitHub env
# variables, then the owner-only gh credential store.  The value is never
# printed; only the source name is safe to report.
resolve_github_deploy_token() {
  local token="" source=""
  if [ -n "${MAC_DEPLOY_GH_TOKEN:-}" ]; then
    token="$MAC_DEPLOY_GH_TOKEN"
    source="env:MAC_DEPLOY_GH_TOKEN"
  elif [ -n "${GH_TOKEN:-}" ]; then
    token="$GH_TOKEN"
    source="env:GH_TOKEN"
  elif [ -n "${GITHUB_TOKEN:-}" ]; then
    token="$GITHUB_TOKEN"
    source="env:GITHUB_TOKEN"
  elif command -v gh >/dev/null 2>&1; then
    token="$(gh auth token --hostname github.com 2>/dev/null || true)"
    if [ -n "$token" ]; then
      source="gh-keyring:github.com"
    fi
  fi
  if [ -n "$token" ]; then
    export MAC_DEPLOY_GH_TOKEN="$token"
  fi
  GITHUB_DEPLOY_CREDENTIAL_SOURCE="$source"
}

# Direct fleet deploys use the same authoritative operator configuration as
# setup.py. Load it before any deployment defaults are derived so callers do
# not have to remember a separate `source ~/.mac/.env` step.
DEPLOY_ENV_FILE="${MAC_DEPLOY_ENV_FILE:-$HOME/.mac/.env}"
load_env_file_with_caller_precedence "$DEPLOY_ENV_FILE"
resolve_github_deploy_token
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMPDIR_LOCAL="$(mktemp -d "${TMPDIR:-/tmp}/mac-fleet-deploy-${TS}.XXXXXX")"
chmod 0700 "$TMPDIR_LOCAL"
ARCHIVE="${TMPDIR_LOCAL}/mac.tar.gz"
SANITIZED_FLEET_REGISTRY="${TMPDIR_LOCAL}/fleets.yaml"
PHASE1_QUIESCE_HELPER="$ROOT/deploy/fleet-node-phase1-quiesce.sh"
COHORT_JOURNAL_HELPER="$ROOT/deploy/fleet-cohort-transaction.py"
ENDPOINT_IDENTITY_HELPER="$ROOT/deploy/fleet-endpoint-identity.py"
HUB_EPOCH_CLIENT="$ROOT/deploy/fleet-release-epoch-client.py"
HUB_EPOCH_MATERIAL_HELPER="$ROOT/deploy/fleet-release-epoch-material.py"
PREREQUISITE_RECEIPT_HELPER="$ROOT/deploy/fleet-prerequisite-receipts.py"
NODE_FINALIZER_HELPER="$ROOT/deploy/fleet-node-finalize.py"
PHASE1_DAEMON_FUNCTIONS="$TMPDIR_LOCAL/daemon-resource-quiescence-functions.sh"
CONFIGURED_AGENT_IDS=""
GIT_REV="$(git -C "$ROOT" rev-parse HEAD)"
GIT_URL="$(git -C "$ROOT" config --get remote.origin.url || true)"
case "$GIT_URL" in
  git@github.com:*)
    GIT_URL="https://github.com/${GIT_URL#git@github.com:}"
    ;;
  github.com:*)
    GIT_URL="https://github.com/${GIT_URL#github.com:}"
    ;;
esac
GIT_BRANCH="${MAC_DEPLOY_GIT_BRANCH:-main}"
FLEET_CONFIG="${MAC_DEPLOY_FLEET_CONFIG:-$ROOT/deploy/fleet/config.yaml}"
FLEET_REGISTRY_CONFIG="${MAC_DEPLOY_FLEETS_CONFIG:-${MAC_FLEETS_CONFIG:-$HOME/.mac/fleets.yaml}}"
HUB_SELECTOR="${MAC_DEPLOY_HUB_AGENT:-}"
SSH_PORT_OVERRIDE="${MAC_DEPLOY_SSH_PORT:-}"
HOLD_ADOPTIONS_SOURCE="${MAC_DEPLOY_HOLD_ADOPTIONS_FILE:-}"
HOLD_ADOPTIONS_FILE=""
REQUIRE_RELEASE_ALL_SELECTED_RAW="${MAC_DEPLOY_REQUIRE_RELEASE_ALL_SELECTED:-0}"
REQUIRE_RELEASE_ALL_SELECTED=0
if [ -n "${MAC_DEPLOY_SUCCESSOR_HOLD_REASON:-}" ]; then
  SUCCESSOR_HOLD_REASON_RAW="$MAC_DEPLOY_SUCCESSOR_HOLD_REASON"
  SUCCESSOR_HOLD_REASON_SUPPLIED=1
else
  SUCCESSOR_HOLD_REASON_RAW=""
  SUCCESSOR_HOLD_REASON_SUPPLIED=0
fi
SUCCESSOR_HOLD_REASON=""
NEW_HUB_NAME=""
NEW_HUB_TARGET=""
NEW_HUB_OS="${MAC_DEPLOY_NEW_HUB_OS:-linux}"
NEW_HUB_FLEET_NAME="${MAC_DEPLOY_NEW_HUB_FLEET_NAME:-}"
NEW_HUB_CONTROL_PORT="${MAC_DEPLOY_NEW_HUB_CONTROL_PORT:-}"
NEW_HUB_URL="${MAC_DEPLOY_NEW_HUB_URL:-}"
NEW_HUB_HOME_CHANNEL="${MAC_DEPLOY_NEW_HUB_HOME_CHANNEL:-}"
NEW_HUB_MODEL="${MAC_DEPLOY_NEW_HUB_MODEL:-}"
NEW_HUB_SUPERVISOR="${MAC_DEPLOY_NEW_HUB_SUPERVISOR:-}"
NEW_HUB_NETWORK_PROVIDER="${MAC_DEPLOY_NEW_HUB_NETWORK_PROVIDER:-}"
NEW_HUB_HEADSCALE_LOGIN_SERVER="${MAC_DEPLOY_NEW_HUB_HEADSCALE_LOGIN_SERVER:-}"
NEW_HUB_HEADSCALE_PREAUTH_KEY="${MAC_DEPLOY_NEW_HUB_HEADSCALE_PREAUTH_KEY:-}"
REQUESTED_AGENTS=()
LEGACY_HUB_BOOTSTRAP=0

if [ -n "${MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE:-}" ]; then
  [[ "$MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE" =~ ^ghcr\.io/jordanhubbard/mac-openshell-runtime@sha256:[0-9a-f]{64}$ ]] || {
    echo "ERROR: MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE must be the immutable repository-owned GHCR digest" >&2
    exit 2
  }
  _runtime_digest="${MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE##*@sha256:}"
  [ "${#_runtime_digest}" -eq 64 ] || {
    echo "ERROR: MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE digest must contain 64 lowercase hex characters" >&2
    exit 2
  }
fi

# Keep fatal errors consistent in both the local launcher and the generated
# remote deploy script.  Remote deployments execute selected functions in a
# fresh shell, so relying on a caller-defined `die` silently turns a required
# abort into `command not found` and leaves the host half-deployed.
die() {
  log "ERROR: $*"
  exit 1
}

normalize_boolean_token() {
  printf '%s' "${1:-}" \
    | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
    | tr '[:upper:]' '[:lower:]'
}

resolve_python_bin() {
  local candidate
  for candidate in "${PYTHON:-}" "${MAC_PYTHON:-}" "$ROOT/.venv/bin/python" python3.11 python3 python; do
    [ -n "$candidate" ] || continue
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

usage() {
  cat <<'USAGE'
Usage:
  deploy/deploy-mac-fleet.sh --hub <hub-node> [--ssh-port <port>]
                            [--hold-adoptions <owner-only-json>]
                            [--require-release-all-selected]
                            [--successor-hold-reason <reason>] [agent ...]
  deploy/deploy-mac-fleet.sh --hub <hub-node> --legacy-hub-bootstrap <hub-node>
  deploy/deploy-mac-fleet.sh --new-hub <hub-node> --target user@host[:port] [--ssh-port <port>]
                            [--fleet-name <name>] [--control-port <port>]
                            [--hub-url <url>] [--home-channel <channel>]
                            [--hub-model <model>] [--supervisor <kind>]
                            [--network-provider tailscale|headscale|none]

Deploy mac as the local ACC replacement to a fleet declared in
~/.mac/fleets.yaml, or in MAC_DEPLOY_FLEETS_CONFIG. Real fleet topology must
live outside this Git repository. The checked-in deploy/fleet/config.yaml is a
generic schema/defaults sample only. The registry may use a top-level
``fleets`` mapping/list or the single-fleet flat form written by setup; agents
may be keyed by name or expressed as a list. See docs/fleet-registry-schema.md.

Each host gets:
  - ~/.mac/src/mac from this repository (includes the vendored Hermes runtime
    at src/mac/_hermes — pinned + patched; no upstream clone, no separate venv)
  - ~/.mac/venv with mac + the hermes-gateway extra installed
  - preinstalled configured Hermes messaging dependencies
  - enforced Hermes secret redaction
  - a host-local mac service, with the configured hub exposed
  - a mac-agent service that registers against the configured hub
  - rollback script and structured deploy manifests under ~/.mac/logs
  - one-time ACC SQLite dry-run and import reports under ~/.mac/logs

Production OpenShell deployments require
MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE=ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:<digest>.
Only isolated development hosts may opt back into per-host builds with
MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD=1.

The hub name selects the fleet. Agent arguments may be agent names from that
fleet. With no agent arguments, all enabled agents in the selected fleet are
deployed.

An adoption file explicitly authorizes exact-reason CAS replacement of every
pre-existing hold in the selected cohort. Supplying one always enables
--require-release-all-selected: phase 3 must release the exact full cohort or
remain fail-closed. Legacy single-node CAS bootstrap rejects this authority.

A successor hold atomically replaces every deployment-owned hold in phase 3,
without exposing an unheld claim window. It also requires exact full-cohort
ownership and may be supplied by MAC_DEPLOY_SUCCESSOR_HOLD_REASON. The live hub
must advertise the distinct POST /agents/dispatch-hold/transition-batch route.

--legacy-hub-bootstrap is a one-use, single-node, pre-held upgrade path for a
hub that does not yet expose the typed epoch API. It leaves the hub held; the
next normal invocation must include that hub in a typed cohort and commit it.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --hub)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --hub requires a hub agent name" >&2
        exit 2
      fi
      HUB_SELECTOR="$2"
      shift 2
      ;;
    --hub=*)
      HUB_SELECTOR="${1#--hub=}"
      shift
      ;;
    --fleets-config)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --fleets-config requires a path" >&2
        exit 2
      fi
      FLEET_REGISTRY_CONFIG="$2"
      shift 2
      ;;
    --fleets-config=*)
      FLEET_REGISTRY_CONFIG="${1#--fleets-config=}"
      shift
      ;;
    --hold-adoptions)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --hold-adoptions requires an owner-only JSON file" >&2
        exit 2
      fi
      HOLD_ADOPTIONS_SOURCE="$2"
      shift 2
      ;;
    --hold-adoptions=*)
      HOLD_ADOPTIONS_SOURCE="${1#--hold-adoptions=}"
      shift
      ;;
    --require-release-all-selected)
      REQUIRE_RELEASE_ALL_SELECTED_RAW=1
      shift
      ;;
    --successor-hold-reason)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --successor-hold-reason requires a non-empty reason" >&2
        exit 2
      fi
      SUCCESSOR_HOLD_REASON_RAW="$2"
      SUCCESSOR_HOLD_REASON_SUPPLIED=1
      shift 2
      ;;
    --successor-hold-reason=*)
      SUCCESSOR_HOLD_REASON_RAW="${1#--successor-hold-reason=}"
      SUCCESSOR_HOLD_REASON_SUPPLIED=1
      shift
      ;;
    --legacy-hub-bootstrap)
      LEGACY_HUB_BOOTSTRAP=1
      shift
      ;;
    --ssh-port)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --ssh-port requires a port" >&2
        exit 2
      fi
      SSH_PORT_OVERRIDE="$2"
      shift 2
      ;;
    --ssh-port=*)
      SSH_PORT_OVERRIDE="${1#--ssh-port=}"
      shift
      ;;
    --new-hub)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --new-hub requires a hub agent name" >&2
        exit 2
      fi
      NEW_HUB_NAME="$2"
      HUB_SELECTOR="$2"
      shift 2
      ;;
    --new-hub=*)
      NEW_HUB_NAME="${1#--new-hub=}"
      HUB_SELECTOR="$NEW_HUB_NAME"
      shift
      ;;
    --target)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --target requires user@host[:port]" >&2
        exit 2
      fi
      NEW_HUB_TARGET="$2"
      shift 2
      ;;
    --target=*)
      NEW_HUB_TARGET="${1#--target=}"
      shift
      ;;
    --hub-os)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --hub-os requires linux or darwin" >&2
        exit 2
      fi
      NEW_HUB_OS="$2"
      shift 2
      ;;
    --hub-os=*)
      NEW_HUB_OS="${1#--hub-os=}"
      shift
      ;;
    --fleet-name)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --fleet-name requires a name" >&2
        exit 2
      fi
      NEW_HUB_FLEET_NAME="$2"
      shift 2
      ;;
    --fleet-name=*)
      NEW_HUB_FLEET_NAME="${1#--fleet-name=}"
      shift
      ;;
    --control-port)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --control-port requires a port" >&2
        exit 2
      fi
      NEW_HUB_CONTROL_PORT="$2"
      shift 2
      ;;
    --control-port=*)
      NEW_HUB_CONTROL_PORT="${1#--control-port=}"
      shift
      ;;
    --hub-url)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --hub-url requires a URL" >&2
        exit 2
      fi
      NEW_HUB_URL="$2"
      shift 2
      ;;
    --hub-url=*)
      NEW_HUB_URL="${1#--hub-url=}"
      shift
      ;;
    --home-channel)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --home-channel requires a channel" >&2
        exit 2
      fi
      NEW_HUB_HOME_CHANNEL="$2"
      shift 2
      ;;
    --home-channel=*)
      NEW_HUB_HOME_CHANNEL="${1#--home-channel=}"
      shift
      ;;
    --hub-model)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --hub-model requires a model" >&2
        exit 2
      fi
      NEW_HUB_MODEL="$2"
      shift 2
      ;;
    --hub-model=*)
      NEW_HUB_MODEL="${1#--hub-model=}"
      shift
      ;;
    --supervisor)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --supervisor requires auto, systemd, launchd, or supervisord" >&2
        exit 2
      fi
      NEW_HUB_SUPERVISOR="$2"
      shift 2
      ;;
    --supervisor=*)
      NEW_HUB_SUPERVISOR="${1#--supervisor=}"
      shift
      ;;
    --network-provider)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --network-provider requires tailscale, headscale, or none" >&2
        exit 2
      fi
      NEW_HUB_NETWORK_PROVIDER="$2"
      shift 2
      ;;
    --network-provider=*)
      NEW_HUB_NETWORK_PROVIDER="${1#--network-provider=}"
      shift
      ;;
    --headscale-login-server)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --headscale-login-server requires a URL" >&2
        exit 2
      fi
      NEW_HUB_HEADSCALE_LOGIN_SERVER="$2"
      shift 2
      ;;
    --headscale-login-server=*)
      NEW_HUB_HEADSCALE_LOGIN_SERVER="${1#--headscale-login-server=}"
      shift
      ;;
    --headscale-preauth-key)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --headscale-preauth-key requires a key" >&2
        exit 2
      fi
      NEW_HUB_HEADSCALE_PREAUTH_KEY="$2"
      shift 2
      ;;
    --headscale-preauth-key=*)
      NEW_HUB_HEADSCALE_PREAUTH_KEY="${1#--headscale-preauth-key=}"
      shift
      ;;
    --)
      shift
      REQUESTED_AGENTS+=("$@")
      break
      ;;
    -*)
      echo "ERROR: unknown option $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      REQUESTED_AGENTS+=("$1")
      shift
      ;;
  esac
done

case "$(normalize_boolean_token "$REQUIRE_RELEASE_ALL_SELECTED_RAW")" in
  1|true|yes|on) REQUIRE_RELEASE_ALL_SELECTED=1 ;;
  0|false|no|off|'') REQUIRE_RELEASE_ALL_SELECTED=0 ;;
  *)
    echo "ERROR: MAC_DEPLOY_REQUIRE_RELEASE_ALL_SELECTED must be a boolean token" >&2
    exit 2
    ;;
esac
if [ -n "$HOLD_ADOPTIONS_SOURCE" ]; then
  # Adoption is authority to clear an operator hold. It is never meaningful as
  # a partial best-effort action: the selected epoch must own and release all.
  REQUIRE_RELEASE_ALL_SELECTED=1
fi

if ! PYTHON_BIN="$(resolve_python_bin)"; then
  echo "ERROR: Python 3.11+ is required (.venv/bin/python, python3.11, python3, or python)" >&2
  exit 127
fi
if [ "$SUCCESSOR_HOLD_REASON_SUPPLIED" = 1 ]; then
  if ! SUCCESSOR_HOLD_REASON="$("$PYTHON_BIN" - "$SUCCESSOR_HOLD_REASON_RAW" <<'PY'
import sys

raw_reason = sys.argv[1]
if any(not character.isprintable() for character in raw_reason):
    raise SystemExit(2)
reason = raw_reason.strip()
if not reason:
    raise SystemExit(1)
if len(reason.encode("utf-8")) > 512:
    raise SystemExit(2)
print(reason, end="")
PY
)"; then
    echo "ERROR: successor hold reason must be nonblank, at most 512 UTF-8 bytes, and contain no control characters" >&2
    exit 2
  fi
  REQUIRE_RELEASE_ALL_SELECTED=1
fi
DEPLOY_CONTROLLER_NONCE="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(16))')"
readonly DEPLOY_CONTROLLER_NONCE
COHORT_EPOCH_ID="${GIT_REV}:${TS}:${DEPLOY_CONTROLLER_NONCE}"
COHORT_JOURNAL_DIR="${MAC_FLEET_COHORT_JOURNAL_DIR:-$HOME/.mac/fleet-cohort-transactions}"
COHORT_JOURNAL_ACTIVE=0
COHORT_JOURNAL_REVISION=0
COHORT_RECOVERY_RUNNING=0
readonly COHORT_EPOCH_ID COHORT_JOURNAL_DIR COHORT_JOURNAL_HELPER
SSH_CONTROL_DIR="/tmp/mac-fleet-ssh-${UID:-0}-${DEPLOY_CONTROLLER_NONCE:0:12}"
mkdir -p "$SSH_CONTROL_DIR"
chmod 0700 "$SSH_CONTROL_DIR"
SSH_CONTROL_REQUIRED=0

cleanup_local_deployment() {
  local status=$? pid_file pid
  trap - EXIT
  set +e
  if [ "$status" -ne 0 ] \
    && [ "$COHORT_JOURNAL_ACTIVE" = 1 ] \
    && [ "$COHORT_RECOVERY_RUNNING" != 1 ] \
    && declare -F recover_active_cohort_transaction_v2 >/dev/null 2>&1; then
    COHORT_RECOVERY_RUNNING=1
    if ! recover_active_cohort_transaction_v2 "$COHORT_EPOCH_ID"; then
      echo "ERROR: cohort recovery remains incomplete; dispatch holds and the durable journal were retained" >&2
    fi
  fi
  for pid_file in "$SSH_CONTROL_DIR"/*.pid; do
    [ -f "$pid_file" ] || continue
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    case "$pid" in
      ''|*[!0-9]*) ;;
      *) kill "$pid" >/dev/null 2>&1 || true ;;
    esac
  done
  for pid_file in "$SSH_CONTROL_DIR"/*.pid; do
    [ -f "$pid_file" ] || continue
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    case "$pid" in
      ''|*[!0-9]*) ;;
      *) wait "$pid" >/dev/null 2>&1 || true ;;
    esac
  done
  rm -rf "$SSH_CONTROL_DIR" "$TMPDIR_LOCAL"
  exit "$status"
}
trap cleanup_local_deployment EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -n "$HOLD_ADOPTIONS_SOURCE" ]; then
  HOLD_ADOPTIONS_FILE="$TMPDIR_LOCAL/hold-adoptions.authority.json"
  "$PYTHON_BIN" "$ROOT/scripts/deploy-hold-adoptions.py" snapshot \
    "$HOLD_ADOPTIONS_SOURCE" "$HOLD_ADOPTIONS_FILE" \
    --source-commit "$GIT_REV"
fi
readonly HOLD_ADOPTIONS_FILE REQUIRE_RELEASE_ALL_SELECTED SUCCESSOR_HOLD_REASON

if [ -n "$NEW_HUB_NAME" ]; then
  if [ -z "$NEW_HUB_TARGET" ]; then
    echo "ERROR: --new-hub requires --target user@host[:port]" >&2
    exit 2
  fi
  setup_args=(
    "$ROOT/scripts/setup-fleet.py"
    --force
    --new-hub "$NEW_HUB_NAME"
    --target "$NEW_HUB_TARGET"
    --hub-os "$NEW_HUB_OS"
    --fleets-config "$FLEET_REGISTRY_CONFIG"
    --env-file "${MAC_DEPLOY_ENV_FILE:-$HOME/.mac/.env}"
  )
  if [ -n "$SSH_PORT_OVERRIDE" ]; then
    setup_args+=(--ssh-port "$SSH_PORT_OVERRIDE")
  fi
  if [ -n "$NEW_HUB_FLEET_NAME" ]; then
    setup_args+=(--fleet-name "$NEW_HUB_FLEET_NAME")
  fi
  if [ -n "$NEW_HUB_CONTROL_PORT" ]; then
    setup_args+=(--control-port "$NEW_HUB_CONTROL_PORT")
  fi
  if [ -n "$NEW_HUB_URL" ]; then
    setup_args+=(--hub-url "$NEW_HUB_URL")
  fi
  if [ -n "$NEW_HUB_HOME_CHANNEL" ]; then
    setup_args+=(--home-channel "$NEW_HUB_HOME_CHANNEL")
  fi
  if [ -n "$NEW_HUB_MODEL" ]; then
    setup_args+=(--hub-model "$NEW_HUB_MODEL")
  fi
  if [ -n "$NEW_HUB_SUPERVISOR" ]; then
    setup_args+=(--supervisor "$NEW_HUB_SUPERVISOR")
  fi
  if [ -n "$NEW_HUB_NETWORK_PROVIDER" ]; then
    setup_args+=(--network-provider "$NEW_HUB_NETWORK_PROVIDER")
  fi
  if [ -n "$NEW_HUB_HEADSCALE_LOGIN_SERVER" ]; then
    setup_args+=(--headscale-login-server "$NEW_HUB_HEADSCALE_LOGIN_SERVER")
  fi
  if [ -n "$NEW_HUB_HEADSCALE_PREAUTH_KEY" ]; then
    setup_args+=(--headscale-preauth-key "$NEW_HUB_HEADSCALE_PREAUTH_KEY")
  fi
  "$PYTHON_BIN" "${setup_args[@]}"
  REQUESTED_AGENTS=("$NEW_HUB_NAME")
fi

# Freeze the complete routing input for this invocation. Every later remote
# resolution, hub lookup, and sanitized registry render must use the same
# owner-only snapshot; otherwise an operator edit or host swap in fleets.yaml
# can make one controller lock host A, mutate host B, and reconcile host C.
# --new-hub intentionally runs first so its newly written registry state is
# included in the immutable deployment view.
FLEET_REGISTRY_SOURCE="$FLEET_REGISTRY_CONFIG"
FLEET_CONFIG_SOURCE="$FLEET_CONFIG"
mkdir -p "$TMPDIR_LOCAL"
cp -f "$FLEET_REGISTRY_SOURCE" "$TMPDIR_LOCAL/fleets-source.yaml"
cp -f "$FLEET_CONFIG_SOURCE" "$TMPDIR_LOCAL/fleet-defaults-source.yaml"
chmod 0600 "$TMPDIR_LOCAL/fleets-source.yaml" "$TMPDIR_LOCAL/fleet-defaults-source.yaml"
FLEET_REGISTRY_CONFIG="$TMPDIR_LOCAL/fleets-source.yaml"
FLEET_CONFIG="$TMPDIR_LOCAL/fleet-defaults-source.yaml"
readonly FLEET_REGISTRY_CONFIG FLEET_CONFIG FLEET_REGISTRY_SOURCE FLEET_CONFIG_SOURCE

fleet_config_query() {
  local mode="$1"
  shift || true
  "$PYTHON_BIN" - "$mode" "$FLEET_CONFIG" "$FLEET_REGISTRY_CONFIG" "$HUB_SELECTOR" "$@" <<'PY'
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List

try:
    import yaml
except Exception as exc:
    print(
        "ERROR: PyYAML is required to read fleet config; run via the project "
        "environment or install PyYAML: %s" % exc,
        file=sys.stderr,
    )
    raise SystemExit(2)


def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        print("ERROR: fleet config %s must be a YAML mapping" % path, file=sys.stderr)
        raise SystemExit(2)
    return data


def merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if (
            isinstance(value, dict)
            and isinstance(result.get(key), dict)
        ):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def bool_field(value: Any, default: bool) -> str:
    if value is None:
        return "1" if default else "0"
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return "1"
    if text in {"0", "false", "no", "off"}:
        return "0"
    return str(value)


def text_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


DEFAULT_WORKER_CAPABILITIES = "ops,python,openclaw,review,api,architecture,cli,docs,security,testing,typescript,ui,web_search,web_extract,web_crawl,firecrawl,work_package_v1"
LEGACY_WORKER_CAPABILITIES = {
    "ops", "python", "hermes", "review", "web_search", "web_extract", "web_crawl", "firecrawl"
}


def worker_capabilities_field(value: Any) -> str:
    items = [item.strip() for item in text_field(value).split(",") if item.strip()]
    if not items or set(items) == LEGACY_WORKER_CAPABILITIES:
        return DEFAULT_WORKER_CAPABILITIES
    return ",".join(items)


def model_field(value: Any) -> str:
    value = text_field(value)
    return "" if value == "*" else value


def host_from_target(target: Any) -> str:
    text = text_field(target)
    if not text:
        return ""
    host = text.rsplit("@", 1)[-1]
    if host.count(":") == 1:
        maybe_host, maybe_port = host.rsplit(":", 1)
        if maybe_port.isdigit():
            host = maybe_host
    return host


def normalize_public_path(value: Any) -> str:
    path = text_field(value) or "/artifacts/"
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path += "/"
    return path


def valid_dns_name(value: str) -> bool:
    name = text_field(value).rstrip(".")
    if not name or len(name) > 253 or "." not in name:
        return False
    try:
        ipaddress.ip_address(name)
        return False
    except ValueError:
        pass
    labels = name.split(".")
    return all(
        0 < len(label) <= 63
        and re.match(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$", label) is not None
        for label in labels
    )


def webdav_url(dns_name: str, public_path: str) -> str:
    host = text_field(dns_name).rstrip(".")
    if not host:
        return ""
    return "https://%s%s" % (host, normalize_public_path(public_path))


def stable_id(prefix: str, value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).lower()).strip("_")
    return "%s_%s" % (prefix, safe or "default")


def hermes_surface_payload(hermes: Dict[str, Any]) -> str:
    payload = {
        "schema": "mac.hermes_fleet_config_payload.v1",
        "runtime": {
            key: hermes.get(key, "")
            for key in (
                "slack_home_channel_name",
                "gateway_model",
                "gateway_provider",
                "gateway_base_url",
                "gateway_impl",
                "public_identity",
                "represented_by",
                "representation_mode",
                "slack_account_id",
                "telegram_account_id",
            )
            if key in hermes
        },
        "config": hermes.get("config") if isinstance(hermes.get("config"), dict) else {},
        "env": hermes.get("env") if isinstance(hermes.get("env"), dict) else {},
        "plugins": hermes.get("plugins") if isinstance(hermes.get("plugins"), dict) else {},
        "skills": hermes.get("skills") if isinstance(hermes.get("skills"), dict) else {},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def scrub_registry(value: Any) -> Any:
    if isinstance(value, list):
        return [scrub_registry(item) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result: Dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        key_lc = key_text.lower()
        if key_lc.endswith("_env") or key_lc.endswith("_env_var"):
            result[key] = scrub_registry(item)
            continue
        if any(fragment in key_lc for fragment in ("password", "secret", "token", "credential", "api_key")):
            result[key] = "<redacted>"
            continue
        result[key] = scrub_registry(item)
    return result


def require_no_pipe(fields: Iterable[str]) -> None:
    for field in fields:
        if "|" in field:
            print("ERROR: fleet config values may not contain '|'", file=sys.stderr)
            raise SystemExit(2)


def agent_map(items: Any) -> Dict[str, Dict[str, Any]]:
    if not items:
        return {}
    normalized: List[Dict[str, Any]] = []
    if isinstance(items, dict):
        # ~/.mac/fleets.yaml is also the CLI/SSH source of truth, whose compact
        # form keys agents by name.  Deploy must accept that canonical mapping
        # instead of requiring operators to maintain a second list-shaped
        # topology file.  An explicit name may be repeated, but it must agree
        # with the mapping key so selection cannot silently target another host.
        for key, value in items.items():
            if not isinstance(value, dict):
                print("ERROR: each fleet agent must be a mapping", file=sys.stderr)
                raise SystemExit(2)
            item = deepcopy(value)
            key_name = text_field(key)
            explicit_name = text_field(item.get("name"))
            if explicit_name and explicit_name != key_name:
                print(
                    "ERROR: fleet agent name %s does not match mapping key %s"
                    % (explicit_name, key_name),
                    file=sys.stderr,
                )
                raise SystemExit(2)
            item["name"] = key_name
            normalized.append(item)
    elif isinstance(items, list):
        normalized = [deepcopy(item) for item in items]
    else:
        print("ERROR: fleet config agents must be a mapping or list", file=sys.stderr)
        raise SystemExit(2)
    result: Dict[str, Dict[str, Any]] = {}
    for item in normalized:
        if not isinstance(item, dict):
            print("ERROR: each fleet agent must be a mapping", file=sys.stderr)
            raise SystemExit(2)
        name = text_field(item.get("name"))
        if not name:
            print("ERROR: each fleet agent needs a name", file=sys.stderr)
            raise SystemExit(2)
        result[name] = deepcopy(item)
    return result


mode = sys.argv[1]
base_path = Path(sys.argv[2])
registry_path = Path(sys.argv[3]).expanduser()
hub_selector = sys.argv[4].strip()
requested = sys.argv[5:]

base = load_yaml(base_path)


def normalize_fleets(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    fleets = data.get("fleets")
    result: Dict[str, Dict[str, Any]] = {}
    # Match scripts/setup-fleet.py's read contract: a single-fleet registry may
    # omit the outer ``fleets`` wrapper.  This is still a registry shape, not a
    # request to merge in the checked-in sample topology; the route-only guard
    # below continues to require ``sample: false`` before deployment.
    if fleets is None and data.get("hub_agent") and data.get("agents"):
        hub = text_field(data.get("hub_agent"))
        if not hub:
            print("ERROR: every fleet entry in %s needs a hub_agent" % registry_path, file=sys.stderr)
            raise SystemExit(2)
        fleet = deepcopy(data)
        fleet["hub_agent"] = hub
        result[hub] = fleet
        return result
    if isinstance(fleets, dict):
        for key, value in fleets.items():
            if not isinstance(value, dict):
                print("ERROR: fleet %s in %s must be a mapping" % (key, registry_path), file=sys.stderr)
                raise SystemExit(2)
            hub = text_field(value.get("hub_agent") or key)
            if not hub:
                print("ERROR: every fleet entry in %s needs a hub_agent" % registry_path, file=sys.stderr)
                raise SystemExit(2)
            fleet = deepcopy(value)
            fleet["hub_agent"] = hub
            result[hub] = fleet
        return result
    if isinstance(fleets, list):
        for value in fleets:
            if not isinstance(value, dict):
                print("ERROR: every fleet entry in %s must be a mapping" % registry_path, file=sys.stderr)
                raise SystemExit(2)
            hub = text_field(value.get("hub_agent"))
            if not hub:
                print("ERROR: every fleet entry in %s needs a hub_agent" % registry_path, file=sys.stderr)
                raise SystemExit(2)
            result[hub] = deepcopy(value)
        return result
    if fleets is None:
        return {}
    print("ERROR: %s fleets must be a mapping or list" % registry_path, file=sys.stderr)
    raise SystemExit(2)


registry_present = registry_path.exists()
if registry_present:
    registry = load_yaml(registry_path)
    fleets = normalize_fleets(registry)
    if not fleets:
        print("ERROR: %s does not contain any fleets" % registry_path, file=sys.stderr)
        raise SystemExit(2)
    if hub_selector:
        if hub_selector not in fleets:
            print(
                "ERROR: hub %s not found in %s. Known hubs: %s"
                % (hub_selector, registry_path, ", ".join(sorted(fleets))),
                file=sys.stderr,
            )
            raise SystemExit(2)
        fleet = fleets[hub_selector]
    elif len(fleets) == 1:
        fleet = next(iter(fleets.values()))
    else:
        print(
            "ERROR: multiple fleets are configured in %s; pass --hub <hub-node>. Known hubs: %s"
            % (registry_path, ", ".join(sorted(fleets))),
            file=sys.stderr,
        )
        raise SystemExit(2)
    # ~/.mac/fleets.yaml is also the canonical SSH target registry.  Its
    # compact route-only form intentionally contains just targets and must not
    # be merged with the checked-in sample defaults: doing so fabricates a
    # deploy topology (fleet identity, service labels, shared-service owner)
    # that can mutate a healthy host under the wrong identity.  setup-fleet
    # writes ``sample: false`` as the explicit full-topology marker.
    if fleet.get("sample") is not False:
        print(
            "ERROR: fleet %s in %s is a route-only target registry, not a full "
            "deployment topology. Run make setup (or scripts/setup-fleet.py) "
            "to write an explicit fleet entry with sample: false before deploying."
            % (text_field(fleet.get("fleet_name") or fleet.get("hub_agent")), registry_path),
            file=sys.stderr,
        )
        raise SystemExit(2)
    cfg = merge_dicts(base, {k: v for k, v in fleet.items() if k != "agents"})
    cfg["agents"] = list(agent_map(fleet.get("agents") if "agents" in fleet else base.get("agents")).values())
else:
    if base.get("sample") and os.environ.get("MAC_DEPLOY_ALLOW_SAMPLE_CONFIG") != "1":
        print(
            "ERROR: no fleet registry found at %s. Run make setup to create one, "
            "or pass --fleets-config /path/to/fleets.yaml. The checked-in %s is "
            "a sample only." % (registry_path, base_path),
            file=sys.stderr,
        )
        raise SystemExit(2)
    cfg = base

agents = [agent for agent in cfg.get("agents") or [] if agent.get("enabled", True)]
if not agents:
    print("ERROR: no enabled agents in fleet config", file=sys.stderr)
    raise SystemExit(2)

hub_agent = (
    hub_selector
    or os.environ.get("MAC_DEPLOY_HUB_AGENT")
    or text_field(cfg.get("hub_agent"))
    or text_field(agents[0].get("name"))
)
hub_url = os.environ.get("MAC_DEPLOY_HUB_URL") or text_field(cfg.get("hub_url"))
fleet_name = os.environ.get("MAC_DEPLOY_FLEET_NAME") or text_field(cfg.get("fleet_name")) or "mac"
control_port = os.environ.get("MAC_DEPLOY_CONTROL_PORT") or text_field(cfg.get("control_port")) or "8789"
shared_services_manager = (
    text_field(cfg.get("shared_services_manager_agent"))
    or os.environ.get("MAC_DEPLOY_SHARED_SERVICES_MANAGER_AGENT")
    or hub_agent
)
defaults = cfg.get("defaults") if isinstance(cfg.get("defaults"), dict) else {}

if mode == "sanitized-registry":
    if registry_present:
        out = scrub_registry(registry)
    else:
        out = scrub_registry({"version": 1, "fleets": {hub_agent: cfg}})
    print(yaml.safe_dump(out, sort_keys=False), end="")
    raise SystemExit(0)

if mode == "configured-agent-ids":
    for agent in agents:
        print(stable_id("agent", text_field(agent.get("name"))))
    raise SystemExit(0)

if mode == "hub-agent":
    print(hub_agent)
    raise SystemExit(0)

if mode == "hub-target":
    for agent in agents:
        if text_field(agent.get("name")) == hub_agent:
            print(text_field(agent.get("target")))
            raise SystemExit(0)
    print("ERROR: hub_agent %s is not an enabled agent" % hub_agent, file=sys.stderr)
    raise SystemExit(2)

if mode == "ssh-jump":
    # Operator->node bastion ProxyJump + host-key strictness, fleet-wide.
    # Emits "<jump>|<strict01>"; both empty/1 by default (no jump, strict on).
    jump = text_field(defaults.get("ssh_jump"))
    strict = "0" if defaults.get("ssh_strict_host_key_checking", True) is False else "1"
    print("%s|%s" % (jump, strict))
    raise SystemExit(0)

by_name = {text_field(agent.get("name")): agent for agent in agents}
selected = requested or list(by_name)
unknown = [name for name in selected if name not in by_name]
if unknown:
    print("unknown agent(s): %s" % ", ".join(unknown), file=sys.stderr)
    raise SystemExit(2)

if mode != "specs":
    print("ERROR: unknown fleet config query mode %s" % mode, file=sys.stderr)
    raise SystemExit(2)

for name in selected:
    agent = by_name[name]
    hermes = merge_dicts(defaults.get("hermes", {}) if isinstance(defaults.get("hermes"), dict) else {}, agent.get("hermes", {}) if isinstance(agent.get("hermes"), dict) else {})
    worker = merge_dicts(defaults.get("worker", {}) if isinstance(defaults.get("worker"), dict) else {}, agent.get("worker", {}) if isinstance(agent.get("worker"), dict) else {})
    qdrant = merge_dicts(defaults.get("qdrant", {}) if isinstance(defaults.get("qdrant"), dict) else {}, agent.get("qdrant", {}) if isinstance(agent.get("qdrant"), dict) else {})
    firecrawl = merge_dicts(defaults.get("firecrawl", {}) if isinstance(defaults.get("firecrawl"), dict) else {}, agent.get("firecrawl", {}) if isinstance(agent.get("firecrawl"), dict) else {})
    webdav = merge_dicts(defaults.get("webdav", {}) if isinstance(defaults.get("webdav"), dict) else {}, agent.get("webdav", {}) if isinstance(agent.get("webdav"), dict) else {})
    network = merge_dicts(defaults.get("network", {}) if isinstance(defaults.get("network"), dict) else {}, agent.get("network", {}) if isinstance(agent.get("network"), dict) else {})
    network_provider = text_field(network.get("provider"))
    if not network_provider:
        network_provider = "none"
    if network_provider not in {"tailscale", "headscale", "none"}:
        print("ERROR: network.provider must be tailscale, headscale, or none", file=sys.stderr)
        raise SystemExit(2)
    tailscale = network.get("tailscale", {}) if isinstance(network.get("tailscale"), dict) else {}
    headscale = network.get("headscale", {}) if isinstance(network.get("headscale"), dict) else {}
    headscale_login_server = text_field(headscale.get("login_server"))
    if network_provider == "headscale" and not headscale_login_server:
        print("ERROR: Headscale provider requires network.headscale.login_server", file=sys.stderr)
        raise SystemExit(2)
    qdrant_data_dir = text_field(qdrant.get("data_dir"))
    webdav_enabled = bool_field(webdav.get("enabled"), False)
    webdav_port = text_field(webdav.get("port") or "80")
    webdav_public_path = normalize_public_path(webdav.get("public_path"))
    webdav_dns_name = (
        text_field(webdav.get("dns_name"))
        or text_field(webdav.get("public_dns_name"))
        or text_field(webdav.get("domain"))
    ).rstrip(".")
    if webdav_enabled == "1" and not valid_dns_name(webdav_dns_name):
        print("ERROR: webdav.enabled requires webdav.dns_name to be a valid DNS name", file=sys.stderr)
        raise SystemExit(2)
    webdav_public_host = (
        text_field(webdav.get("public_host"))
        or text_field(webdav.get("principal_host"))
        or host_from_target(by_name.get(hub_agent, {}).get("target"))
    )
    webdav_public_url = text_field(webdav.get("url")) or webdav_url(webdav_dns_name, webdav_public_path)
    if webdav_enabled == "1" and not webdav_public_url.lower().startswith("https://"):
        print("ERROR: webdav public url must use https:// when webdav.enabled is true", file=sys.stderr)
        raise SystemExit(2)
    target = text_field(agent.get("target"))
    os_kind = text_field(agent.get("os"))
    if not target or not os_kind:
        print("ERROR: agent %s must set target and os" % name, file=sys.stderr)
        raise SystemExit(2)
    control_bind_host = text_field(agent.get("control_bind_host"))
    if not control_bind_host:
        control_bind_host = "0.0.0.0" if name == hub_agent else "127.0.0.1"
    fields = [
        name,
        target,
        os_kind,
        text_field(hermes.get("slack_home_channel_name")),
        model_field(hermes.get("gateway_model")),
        text_field(hermes.get("gateway_provider") or "custom"),
        text_field(hermes.get("gateway_base_url")),
        hub_url,
        control_bind_host,
        text_field(worker.get("mode") or "heartbeat"),
        worker_capabilities_field(worker.get("capabilities")),
        text_field(worker.get("allowed_projects")),
        text_field(worker.get("required_metadata")),
        bool_field(worker.get("require_canary"), True),
        text_field(agent.get("supervisor") or defaults.get("supervisor") or "auto"),
        shared_services_manager,
        text_field(qdrant.get("url")),
        text_field(qdrant.get("install") or "auto"),
        "true",
        text_field(qdrant.get("bind_addr")),
        text_field(qdrant.get("port") or "6333"),
        text_field(qdrant.get("image") or "docker.io/qdrant/qdrant:latest"),
        text_field(qdrant.get("memory_limit") or "2g"),
        fleet_name,
        control_port,
        qdrant_data_dir,
        os.environ.get("MAC_DEPLOY_FIRECRAWL_URL") or text_field(firecrawl.get("url")),
        os.environ.get("MAC_DEPLOY_FIRECRAWL_INSTALL") or text_field(firecrawl.get("install") or "auto"),
        "true",
        os.environ.get("MAC_DEPLOY_FIRECRAWL_BIND_ADDR") or text_field(firecrawl.get("bind_addr")),
        os.environ.get("MAC_DEPLOY_FIRECRAWL_PORT") or text_field(firecrawl.get("port") or "3002"),
        network_provider,
        text_field(network.get("install") or "auto"),
        text_field(network.get("hostname_prefix")),
        text_field(tailscale.get("auth_key_env") or "MAC_DEPLOY_TAILSCALE_AUTH_KEY"),
        bool_field(headscale.get("manage"), False),
        headscale_login_server,
        text_field(headscale.get("health_url") or ("%s/health" % headscale_login_server.rstrip("/") if headscale_login_server else "")),
        text_field(headscale.get("fleet_url")),
        text_field(headscale.get("preauth_key_source") or "env"),
        text_field(headscale.get("preauth_key_env") or "MAC_DEPLOY_HEADSCALE_PREAUTHKEY"),
        text_field(headscale.get("port") or "8080"),
        text_field(headscale.get("public_addr")),
        text_field(headscale.get("dns") or "magicdns"),
        text_field(headscale.get("ip_prefix") or "100.64.0.0/10"),
        webdav_enabled,
        text_field(webdav.get("install") or "auto"),
        webdav_public_url,
        text_field(webdav.get("bind_addr") or "0.0.0.0"),
        webdav_port,
        text_field(webdav.get("root")),
        webdav_public_path,
        hermes_surface_payload(hermes),
        # Pure workers are code executors and therefore require the confined
        # OpenShell runtime by default.  Conversational nodes remain opt-in,
        # while an explicit worker.openshell_required value wins for either
        # role.  Carry this in the deploy spec so a fresh/ephemeral node cannot
        # inherit an enabled policy but miss the CLI and gateway binaries.
        bool_field(
            worker.get("openshell_required"),
            text_field(hermes.get("gateway_impl") or "hermes") == "none",
        ),
        # Pure workers must be able to fetch and publish repository work from a
        # fresh host.  Make that fail closed by default while preserving an
        # explicit opt-out for intentionally public/read-only executors.
        bool_field(
            worker.get("github_credentials_required"),
            text_field(hermes.get("gateway_impl") or "hermes") == "none",
        ),
    ]
    require_no_pipe(fields)
    print("|".join(fields))
PY
}

agent_spec() {
  fleet_config_query specs "$1"
}

selected_hosts() {
  fleet_config_query specs "$@"
}

fleet_hub_agent() {
  fleet_config_query hub-agent
}

fleet_hub_target() {
  fleet_config_query hub-target
}

shell_quote() {
  local value="$1"
  printf "'%s'" "$(printf '%s' "$value" | sed "s/'/'\\\\''/g")"
}

sha256_file() {
  "$PYTHON_BIN" - "$1" <<'PY'
import hashlib,sys
digest=hashlib.sha256()
with open(sys.argv[1], "rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

# Resolve every operator-side SSH call through the Python contract used by
# the CLI and desktop bridge. Output is NUL-delimited so paths containing spaces
# remain argv-safe. ``-F /dev/null`` prevents ambient ~/.ssh/config from
# silently supplying a different jump host, identity, or host-key policy.
fleet_ssh_route_args() {
  local kind="$1" agent="$2" cmd
  cmd=(
    "$PYTHON_BIN" -m mac.fleet_ssh
    --config "$FLEET_REGISTRY_CONFIG"
    --fleet "$HUB_SELECTOR"
    --agent "$agent"
    --kind "$kind"
    --nul
  )
  if [ -n "${SSH_PORT_OVERRIDE:-}" ]; then
    cmd+=(--port-override "$SSH_PORT_OVERRIDE")
  fi
  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "${cmd[@]}"
}

ssh_control_path_for_agent() {
  local digest
  digest="$("$PYTHON_BIN" -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:20])' "$1")"
  printf '%s/%s.sock\n' "$SSH_CONTROL_DIR" "$digest"
}

start_ssh_control_master() {
  local agent="$1" control_path log_path pid_path
  local route_parts=() route_args=() target item last_index attempt pid
  control_path="$(ssh_control_path_for_agent "$agent")"
  log_path="${control_path}.log"
  pid_path="${control_path}.pid"
  if [ -S "$control_path" ] && [ -f "$pid_path" ]; then
    return 0
  fi
  while IFS= read -r -d '' item; do route_parts+=("$item"); done < <(fleet_ssh_route_args ssh "$agent")
  last_index=$((${#route_parts[@]} - 1))
  target="${route_parts[$last_index]}"
  route_args=("${route_parts[@]:0:$last_index}")
  # Keep the master in this controller's process tree. Every later session
  # reuses this exact socket and carries a failing ProxyCommand fallback, so it
  # cannot open a fresh TCP connection if this socket or process disappears.
  ssh -vv -A -MN -o BatchMode=yes -o ConnectTimeout=10 \
    -o ControlMaster=yes -o ControlPersist=no -o ControlPath="$control_path" \
    "${route_args[@]}" "$target" </dev/null >"$log_path" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" > "$pid_path"
  chmod 0600 "$pid_path" "$log_path"
  for attempt in $(seq 1 100); do
    if ssh -n -S "$control_path" -O check "${route_args[@]}" "$target" >/dev/null 2>&1; then
      return 0
    fi
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
  echo "ERROR: ${agent}: could not establish pinned SSH control master" >&2
  sed -n '1,20p' "$log_path" >&2 || true
  return 1
}

pinned_fleet_route_args() {
  local agent="$1" control_path
  local route_parts=() item last_index
  while IFS= read -r -d '' item; do route_parts+=("$item"); done < <(fleet_ssh_route_args ssh "$agent")
  if [ "$SSH_CONTROL_REQUIRED" != 1 ]; then
    printf '%s\0' "${route_parts[@]}"
    return 0
  fi
  control_path="$(ssh_control_path_for_agent "$agent")"
  # Always emit the explicit pinned-multiplex route, even if the socket has already
  # disappeared.  This function is normally consumed through process
  # substitution, whose exit status is not propagated to the caller.  An
  # early return could therefore leave the caller with an unpinned route.
  if [ ! -S "$control_path" ]; then
    echo "ERROR: ${agent}: pinned SSH control socket is unavailable" >&2
  fi
  last_index=$((${#route_parts[@]} - 1))
  # ``ssh -O proxy`` is a control command: when used as the outer invocation it
  # creates a raw proxy stream and does not execute the requested remote
  # command.  Reuse the master as a normal multiplexed session instead.  The
  # deliberately failing ProxyCommand is ignored while the master is alive,
  # but makes the fallback connection fail closed if the socket disappears.
  # The master already froze the complete route (jump, identity, host keys and
  # target), so a reused session needs only the socket and original target.
  printf '%s\0' -F /dev/null
  printf '%s\0' -S "$control_path"
  printf '%s\0' -o ControlMaster=no -o ControlPersist=no
  printf '%s\0' -o ProxyCommand=/usr/bin/false
  printf '%s\0' "${route_parts[$last_index]}"
}

ssh_target_args() {
  pinned_fleet_route_args "$1"
}

remote_deployment_fenced_exec() {
  local deployment_id="$1" ready="$2" code arg
  shift 2
  code='import fcntl,json,os,sys
from pathlib import Path
root=Path.home()/".mac"
guard_path=root/"deploy-controller.guard"
fd=os.open(str(guard_path), os.O_CREAT|os.O_RDWR, 0o600)
os.fchmod(fd, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
owner=root/"deploy-controller.lock"/"owner.json"
try:
    payload=json.loads(owner.read_text(encoding="utf-8"))
except (OSError,ValueError,TypeError) as exc:
    raise SystemExit("deployment lock is unreadable: %s" % type(exc).__name__)
if payload.get("deployment_id") != sys.argv[1]:
    raise SystemExit("deployment lock fence does not match this controller")
os.set_inheritable(fd, True)
os.environ["MAC_DEPLOY_LOCK_GUARD_FD"]=str(fd)
if sys.argv[2] == "1":
    print("MAC_DEPLOY_FENCE_READY:" + sys.argv[1], flush=True)
os.execvp(sys.argv[3], sys.argv[3:])'
  printf 'python3 -c %s %s %s' "$(shell_quote "$code")" \
    "$(shell_quote "$deployment_id")" "$(shell_quote "$ready")"
  for arg in "$@"; do
    printf ' %s' "$(shell_quote "$arg")"
  done
}

stream_file_after_remote_fence() {
  local source_file="$1" expected_ready="$2"
  shift 2
  # The payload file is deliberately not opened until the exact remote
  # deployment owner has acquired the stable guard and emitted READY. One
  # monotonic deadline covers the READY handshake, payload upload, remote
  # output drain, and child reap so no live-but-stalled SSH session can hold a
  # cohort forever.
  "$PYTHON_BIN" - "$source_file" "$expected_ready" \
    "${MAC_DEPLOY_REMOTE_PHASE_TIMEOUT_SECONDS:-7200}" "$@" <<'PY'
import os
import math
import selectors
import signal
import subprocess
import sys
import time

source, expected, timeout_raw, *command = sys.argv[1:]
try:
    timeout = float(timeout_raw)
except (TypeError, ValueError) as exc:
    raise SystemExit("remote fenced stream timeout is invalid") from exc
if not math.isfinite(timeout) or timeout < 0.05 or timeout > 21600:
    raise SystemExit("remote fenced stream timeout is outside its safe bound")
deadline = time.monotonic() + timeout
process = subprocess.Popen(
    command,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    bufsize=0,
    start_new_session=True,
)
assert process.stdin is not None and process.stdout is not None
selector = selectors.DefaultSelector()
stdin_fd = process.stdin.fileno()
stdout_fd = process.stdout.fileno()
os.set_blocking(stdin_fd, False)
os.set_blocking(stdout_fd, False)
selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
source_stream = None


def remaining():
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("remote fenced stream exceeded its monotonic deadline")
    return value


def stop_child():
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


try:
    ready = bytearray()
    while b"\n" not in ready:
        events = selector.select(remaining())
        if not events:
            raise TimeoutError(
                "remote fenced stream exceeded its monotonic deadline"
            )
        chunk = os.read(stdout_fd, 65536)
        if not chunk:
            raise RuntimeError(
                "remote deployment fence did not emit the exact READY receipt"
            )
        ready.extend(chunk)
        if len(ready) > 4096 and b"\n" not in ready:
            raise RuntimeError("remote deployment fence emitted an oversized receipt")
    raw_line, trailing = bytes(ready).split(b"\n", 1)
    line = raw_line.rstrip(b"\r").decode("utf-8", "replace")
    if line != expected:
        raise RuntimeError(
            "remote deployment fence did not emit the exact READY receipt"
        )
    if trailing:
        sys.stdout.buffer.write(trailing)
        sys.stdout.buffer.flush()

    # Open only after the exact receipt. Both pipes are nonblocking and driven
    # together so a verbose remote cannot deadlock behind a full stdout pipe
    # while the controller is still writing a large archive.
    source_stream = open(source, "rb")
    selector.register(stdin_fd, selectors.EVENT_WRITE, "stdin")
    pending = b""
    input_done = False
    stdout_done = False
    while not (input_done and stdout_done):
        events = selector.select(remaining())
        if not events:
            raise TimeoutError(
                "remote fenced stream exceeded its monotonic deadline"
            )
        for key, _mask in events:
            if key.data == "stdout":
                chunk = os.read(stdout_fd, 65536)
                if chunk:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                else:
                    selector.unregister(stdout_fd)
                    stdout_done = True
            elif key.data == "stdin":
                if not pending:
                    pending = source_stream.read(65536)
                    if not pending:
                        selector.unregister(stdin_fd)
                        process.stdin.close()
                        input_done = True
                        continue
                try:
                    written = os.write(stdin_fd, pending)
                except BrokenPipeError as exc:
                    raise RuntimeError(
                        "remote fenced stream closed before payload completion"
                    ) from exc
                pending = pending[written:]

    try:
        returncode = process.wait(timeout=remaining())
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            "remote fenced stream exceeded its monotonic deadline"
        ) from exc
    if returncode != 0:
        raise SystemExit(returncode)
except (OSError, RuntimeError, TimeoutError) as exc:
    stop_child()
    raise SystemExit(str(exc)) from exc
finally:
    if source_stream is not None:
        source_stream.close()
    selector.close()
    if process.stdin and not process.stdin.closed:
        process.stdin.close()
    if process.stdout and not process.stdout.closed:
        process.stdout.close()
PY
}

pinned_remote_private_upload() {
  # Upload an owner-private bounded control-plane request over the already
  # pinned SSH channel without assuming the hub is also a deployed node.
  local agent="$1" source="$2" destination="$3"
  local ssh_parts=() ssh_args=() ssh_target item last_index upload_code
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1)); ssh_target="${ssh_parts[$last_index]}"; ssh_args=("${ssh_parts[@]:0:$last_index}")
  "$PYTHON_BIN" - "$source" <<'PY'
import os,stat,sys
value=os.lstat(sys.argv[1])
if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode) or value.st_uid!=os.getuid() or value.st_nlink!=1 or not 1<=value.st_size<=1024*1024:
    raise SystemExit("upload source is unsafe")
PY
  upload_code='import os,sys
path=sys.argv[1]
if not path.startswith("/tmp/mac-") or "/../" in path: raise SystemExit("upload destination is outside the private relay namespace")
flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0)
fd=os.open(path,flags,0o600)
complete=False
try:
    os.fchmod(fd,0o600); total=0
    while True:
        chunk=os.read(0,65536)
        if not chunk: break
        total+=len(chunk)
        if total>1024*1024: raise SystemExit("upload exceeds its size bound")
        pending=memoryview(chunk)
        while pending:
            written=os.write(fd,pending)
            if written<=0: raise SystemExit("upload write made no progress")
            pending=pending[written:]
    os.fsync(fd)
    complete=True
finally:
    os.close(fd)
    if not complete:
        try: os.unlink(path)
        except FileNotFoundError: pass'
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" \
    "python3 -c $(shell_quote "$upload_code") $(shell_quote "$destination")" \
    < "$source"
}

fenced_remote_upload() {
  local agent="$1" deployment_id="$2" source_file="$3" destination="$4"
  local ssh_parts=() ssh_args=() ssh_target item last_index remote_cmd upload_code
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  upload_code='import os,sys,tempfile
target=os.path.abspath(sys.argv[1])
directory=os.path.dirname(target)
if not directory or not os.path.isdir(directory):
    raise SystemExit("remote upload target directory is unavailable")
fd,tmp=tempfile.mkstemp(prefix="."+os.path.basename(target)+".upload.",dir=directory)
published=False
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"wb") as output:
        while True:
            chunk=sys.stdin.buffer.read(65536)
            if not chunk:
                break
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    os.replace(tmp,target)
    published=True
    directory_fd=os.open(directory,os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if not published:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass'
  remote_cmd="$(remote_deployment_fenced_exec "$deployment_id" 1 \
    python3 -c "$upload_code" "$destination")"
  stream_file_after_remote_fence "$source_file" \
    "MAC_DEPLOY_FENCE_READY:${deployment_id}" \
    ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$remote_cmd"
}

env_value_or_empty() {
  local key="$1"
  if [ -z "$key" ]; then
    return
  fi
  printf '%s' "${!key-}"
}

fleet_scoped_name() {
  local key="$1" fleet="$2"
  "$PYTHON_BIN" - "$key" "$fleet" <<'PY'
import re
import sys

key = sys.argv[1]
fleet = sys.argv[2]
suffix = re.sub(r"[^A-Za-z0-9]+", "_", fleet.strip()).strip("_").upper()
print("%s__%s" % (key, suffix) if suffix else key)
PY
}

# Resolve a secret from the MAC secrets store (encrypted-at-rest, access-audited)
# via the hub's POST /secrets/{name}/resolve endpoint. Prints the value or
# nothing. The hub token is read DIRECTLY from env (never via the vault) to avoid
# recursion when the token itself is looked up. Silent on any failure so the
# vault is a pure fallback that never breaks an env-only deploy.
resolve_secret_from_store() {
  # MUST always return 0 (it is called in `val="$(...)"` under set -euo
  # pipefail — a non-zero return would kill the whole deploy). A miss just
  # prints nothing.
  local name="$1" fleet="$2" hub token scoped_token body
  hub="${MAC_API_URL:-${MAC_URL:-${MAC_HUB_URL:-}}}"
  scoped_token="$(fleet_scoped_name MAC_API_TOKEN "$fleet")"
  token="${!scoped_token:-${MAC_API_TOKEN:-}}"
  if [ -z "$hub" ] || [ -z "$token" ] || ! command -v curl >/dev/null 2>&1; then
    return 0
  fi
  body="$(curl -fsS --max-time 15 -X POST -H "Authorization: Bearer ${token}" \
    "${hub%/}/secrets/${name}/resolve" 2>/dev/null || true)"
  [ -n "$body" ] || return 0
  printf '%s' "$body" | "$PYTHON_BIN" -c 'import sys,json
try: sys.stdout.write(str(json.load(sys.stdin).get("value","")))
except Exception: pass' 2>/dev/null || true
  return 0
}

# Deploy secret resolution, in precedence order:
#   1. fleet-scoped env var (NAME__FLEET)   2. bare env var (NAME)
#   3. MAC secrets store, fleet-scoped name  4. MAC secrets store, bare name
# Env wins for back-compat; the vault is the single encrypted source of truth
# for anything not in env, so deploy secrets no longer require plaintext ~/.mac/.env.
fleet_scoped_env() {
  local key="$1" fleet="$2" scoped val
  scoped="$(fleet_scoped_name "$key" "$fleet")"
  if [ -n "${!scoped+x}" ]; then
    printf '%s' "${!scoped}"; return
  elif [ -n "${!key+x}" ]; then
    printf '%s' "${!key}"; return
  fi
  val="$(resolve_secret_from_store "$scoped" "$fleet")"
  [ -n "$val" ] && { printf '%s' "$val"; return 0; }
  val="$(resolve_secret_from_store "$key" "$fleet")"
  [ -n "$val" ] && printf '%s' "$val"
  return 0
}

assert_frozen_deployment_source() {
  # The release archive is cut from GIT_REV, but the controller and its
  # pre-source/rollback helpers execute or upload directly from the checkout.
  # Refuse a split-brain deployment in which those bytes differ from the commit
  # named by every receipt and release epoch.  Limit the broad dirtiness check
  # to executable deployment inputs so unrelated documentation/test work does
  # not prevent an operator from deploying the already committed release.
  local untracked asset expected_hash observed_hash
  if ! git -C "$ROOT" diff --quiet --no-ext-diff "$GIT_REV" -- \
    deploy scripts src/mac; then
    echo "ERROR: deployment executable inputs differ from frozen commit $GIT_REV" >&2
    echo "       commit or restore deploy/, scripts/, and src/mac before fleet mutation" >&2
    return 1
  fi
  untracked="$(git -C "$ROOT" ls-files --others --exclude-standard -- \
    deploy scripts src/mac)"
  if [ -n "$untracked" ]; then
    echo "ERROR: untracked deployment executable inputs are present" >&2
    printf '%s\n' "$untracked" >&2
    return 1
  fi

  # Name every file the outer controller executes or uploads outside the
  # GIT_REV archive.  Object-id comparison also catches assume-unchanged and
  # skip-worktree entries that an ordinary diff can intentionally omit.
  for asset in \
    deploy/deploy-mac-fleet.sh \
    deploy/fleet-cohort-transaction.py \
    deploy/fleet-node-install.sh \
    deploy/fleet-node-phase1-quiesce.sh \
    deploy/fleet-node-rollback-supervisor.py \
    deploy/lib/launchd-lifecycle.sh \
    deploy/reviewed-tool-assets.sh \
    scripts/deploy-hold-adoptions.py \
    scripts/provision-openclaw-personality.py; do
    if [ ! -f "$ROOT/$asset" ] || [ -L "$ROOT/$asset" ]; then
      echo "ERROR: frozen deployment asset is missing or unsafe: $asset" >&2
      return 1
    fi
    if ! expected_hash="$(git -C "$ROOT" rev-parse "$GIT_REV:$asset" 2>/dev/null)"; then
      echo "ERROR: deployment asset is absent from frozen commit $GIT_REV: $asset" >&2
      return 1
    fi
    observed_hash="$(git -C "$ROOT" hash-object -- "$ROOT/$asset")"
    if [ "$observed_hash" != "$expected_hash" ]; then
      echo "ERROR: deployment asset does not match frozen commit $GIT_REV: $asset" >&2
      return 1
    fi
  done
}

make_archive() {
  mkdir -p "$TMPDIR_LOCAL"
  # Archive the same immutable revision advertised to the remote installer.
  # HEAD can advance after GIT_REV is captured (for example, when another
  # operator or agent commits in this shared checkout); packaging symbolic
  # HEAD would make the archive contents disagree with every deployment proof.
  git -C "$ROOT" archive --format=tar.gz --output="$ARCHIVE" "$GIT_REV"
  fleet_config_query sanitized-registry > "$SANITIZED_FLEET_REGISTRY"
  chmod 0644 "$SANITIZED_FLEET_REGISTRY"
}

prepare_phase1_quiescence_assets() {
  [ -x "$PHASE1_QUIESCE_HELPER" ] || {
    echo "ERROR: phase-1 quiescence helper is unavailable: $PHASE1_QUIESCE_HELPER" >&2
    return 1
  }
  "$PYTHON_BIN" - "$ROOT/deploy/fleet-node-install.sh" "$PHASE1_DAEMON_FUNCTIONS" <<'PY'
import os
import tempfile
import sys
from pathlib import Path

source = Path(sys.argv[1])
output = Path(sys.argv[2])
text = source.read_text(encoding="utf-8")
begin = "# BEGIN MAC DAEMON RESOURCE QUIESCENCE"
end = "# END MAC DAEMON RESOURCE QUIESCENCE"
if text.count(begin) != 1 or text.count(end) != 1:
    raise SystemExit("daemon quiescence source markers are ambiguous")
prefix, remainder = text.split(begin, 1)
block, suffix = remainder.split(end, 1)
if not prefix or not suffix or "daemon_resource_quiescence_gate()" not in block:
    raise SystemExit("daemon quiescence function block is incomplete")
payload = begin + block + end + "\n"
fd, raw = tempfile.mkstemp(prefix=output.name + ".", dir=str(output.parent))
temporary = Path(raw)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
finally:
    temporary.unlink(missing_ok=True)
PY
}

reconcile_remote_deploy() {
  local agent="$1" _display_target="$2" clear_repo_update_blocker="${3:-0}"
  # Routing is deliberately resolved again by agent name, but only against the
  # immutable invocation snapshot.  The raw target is retained for the public
  # function contract/log context and can no longer redirect reconciliation.
  local ssh_parts=() ssh_args=() ssh_target item last_index deployment_id fence_exec
  deployment_id="$(deployment_id_for_agent "$agent")"
  fence_exec="$(remote_deployment_fenced_exec "$deployment_id" 0 bash -s)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  local _max_retries="${MAC_DEPLOY_RECONCILE_MAX_RETRIES:-3}"
  local _attempt _sleep_interval
  for _attempt in $(seq 1 "$_max_retries"); do
    if [ "$_attempt" -gt 1 ]; then
      _sleep_interval=$((2 ** (_attempt - 1)))
      echo "==> ${agent}: reconcile_remote_deploy attempt ${_attempt}/${_max_retries} (sleeping ${_sleep_interval}s after previous failure)"
      sleep "$_sleep_interval"
    fi
    if ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" \
      "MAC_DEPLOY_AGENT=$(shell_quote "$agent") MAC_DEPLOY_TS=$(shell_quote "$TS") MAC_DEPLOY_GIT_REV=$(shell_quote "$GIT_REV") MAC_DEPLOY_GENERATION_EXPECTED=$(shell_quote "$deployment_id") MAC_DEPLOY_CLEAR_REPO_UPDATE_BLOCKER=$(shell_quote "$clear_repo_update_blocker") $fence_exec" <<'REMOTE'
set -euo pipefail
agent="${MAC_DEPLOY_AGENT:?}"
deploy_ts="${MAC_DEPLOY_TS:?}"
expected_rev="${MAC_DEPLOY_GIT_REV:?}"
expected_generation="${MAC_DEPLOY_GENERATION_EXPECTED:?}"
clear_repo_update_blocker="${MAC_DEPLOY_CLEAR_REPO_UPDATE_BLOCKER:-0}"
mac_home="${MAC_HOME:-$HOME/.mac}"
log_dir="$mac_home/logs"
manifest="$log_dir/deploy-manifest-${deploy_ts}-post.json"
latest="$log_dir/deploy-manifest-latest.json"
deploy_log="$log_dir/deploy-${deploy_ts}.log"
source_revision_file="$mac_home/deployed-source-revision"
python_bin="${PYTHON_BIN:-}"
if [ -z "$python_bin" ] || ! command -v "$python_bin" >/dev/null 2>&1; then
  python_bin=""
  for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      python_bin="$(command -v "$candidate")"
      break
    fi
  done
fi
if [ -z "$python_bin" ]; then
  echo "remote reconciliation failed: no Python interpreter found" >&2
  exit 1
fi
if [ ! -s "$manifest" ]; then
  echo "remote reconciliation failed: missing post manifest $manifest" >&2
  exit 1
fi
if [ ! -s "$latest" ]; then
  echo "remote reconciliation failed: missing latest manifest $latest" >&2
  exit 1
fi
if ! grep -q "deploy complete" "$deploy_log"; then
  echo "remote reconciliation failed: deploy log lacks completion marker" >&2
  exit 1
fi
if ! [[ "$expected_rev" =~ ^[0-9a-f]{40}$ ]]; then
  echo "remote reconciliation failed: expected source revision is invalid" >&2
  exit 1
fi
if [ ! -s "$source_revision_file" ]; then
  echo "remote reconciliation failed: missing deployed source revision $source_revision_file" >&2
  exit 1
fi
deployed_rev="$(cat "$source_revision_file")"
if [ "$deployed_rev" != "$expected_rev" ]; then
  echo "remote reconciliation failed: deployed source revision does not match requested revision" >&2
  exit 1
fi
if [ -e "$mac_home/src/mac/.git" ]; then
  checkout_rev="$(git -C "$mac_home/src/mac" rev-parse HEAD 2>/dev/null || true)"
  if [ "$checkout_rev" != "$expected_rev" ]; then
    echo "remote reconciliation failed: deployed Git checkout does not match requested revision" >&2
    exit 1
  fi
fi
"$python_bin" - "$manifest" "$latest" "$agent" "$deploy_ts" "$expected_rev" "$expected_generation" <<'PY'
import json
import sys
(
    manifest_path,
    latest_path,
    expected_agent,
    expected_ts,
    expected_rev,
    expected_generation,
) = sys.argv[1:]
quiescence_summaries = []
gateway_summaries = []
phase1_summaries = []
for label, path in (("post manifest", manifest_path), ("latest manifest", latest_path)):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit("remote reconciliation failed: %s is not a JSON object" % label)
    if data.get("stage") != "post":
        raise SystemExit("remote reconciliation failed: %s stage is %r" % (label, data.get("stage")))
    if data.get("agent") != expected_agent:
        raise SystemExit("remote reconciliation failed: %s agent is %r" % (label, data.get("agent")))
    deploy = data.get("deploy") or {}
    if deploy.get("timestamp") != expected_ts:
        raise SystemExit(
            "remote reconciliation failed: %s deploy timestamp is %r"
            % (label, deploy.get("timestamp"))
        )
    if deploy.get("mac_git_rev") != expected_rev:
        raise SystemExit(
            "remote reconciliation failed: %s source revision is %r"
            % (label, deploy.get("mac_git_rev"))
        )
    quiescence = data.get("daemon_resource_quiescence")
    if not isinstance(quiescence, dict):
        raise SystemExit("remote reconciliation failed: %s lacks quiescence" % label)
    required = quiescence.get("required_phases")
    proved = quiescence.get("proved_phases")
    if (
        quiescence.get("schema")
        != "mac.daemon_resource_quiescence_manifest.v1"
        or quiescence.get("status") != "proved"
        or quiescence.get("generation") != expected_generation
        or quiescence.get("revision") != expected_rev
        or not isinstance(quiescence.get("sha256"), str)
        or len(quiescence["sha256"]) != 64
        or not isinstance(required, list)
        or not required
        or not isinstance(proved, list)
        or not set(required).issubset(set(proved))
        or not isinstance(quiescence.get("container_runtimes"), list)
    ):
        raise SystemExit(
            "remote reconciliation failed: %s has invalid quiescence evidence" % label
        )
    quiescence_summaries.append(quiescence)
    phase1 = data.get("phase1_cohort_quiescence")
    phase1_daemon = (
        phase1.get("daemon_resource_receipt")
        if isinstance(phase1, dict)
        else None
    )
    if (
        not isinstance(phase1, dict)
        or phase1.get("schema")
        != "mac.phase1_cohort_quiescence_manifest.v1"
        or phase1.get("status") != "proved"
        or phase1.get("generation") != expected_generation
        or phase1.get("revision") != expected_rev
        or not isinstance(phase1.get("sha256"), str)
        or len(phase1["sha256"]) != 64
        or not isinstance(phase1.get("supervisor"), dict)
        or not isinstance(phase1_daemon, dict)
        or phase1_daemon.get("schema")
        != "mac.daemon_resource_quiescence.v1"
        or phase1_daemon.get("proof_phase") != "pre_source"
        or not isinstance(phase1_daemon.get("sha256"), str)
        or len(phase1_daemon["sha256"]) != 64
        or not isinstance(phase1_daemon.get("function_block_sha256"), str)
        or len(phase1_daemon["function_block_sha256"]) != 64
    ):
        raise SystemExit(
            "remote reconciliation failed: %s has invalid phase-1 evidence" % label
        )
    phase1_summaries.append(phase1)
    gateway = data.get("gateway_readiness")
    if (
        not isinstance(gateway, dict)
        or gateway.get("schema") != "mac.gateway_readiness_manifest.v1"
        or gateway.get("status") != "proved"
        or gateway.get("generation") != expected_generation
        or gateway.get("revision") != expected_rev
        or gateway.get("stable_observations") != 2
        or gateway.get("implementation")
        not in {"hermes", "openclaw", "nemoclaw", "none"}
        or gateway.get("supervisor")
        not in {"systemd", "launchd", "supervisord"}
        or not isinstance(gateway.get("sha256"), str)
        or len(gateway["sha256"]) != 64
        or not isinstance(gateway.get("identities"), dict)
        or not isinstance(gateway.get("state"), dict)
    ):
        raise SystemExit(
            "remote reconciliation failed: %s has invalid gateway readiness" % label
        )
    gateway_summaries.append(gateway)
if quiescence_summaries[0] != quiescence_summaries[1]:
    raise SystemExit("remote reconciliation failed: manifest quiescence evidence diverged")
if phase1_summaries[0] != phase1_summaries[1]:
    raise SystemExit("remote reconciliation failed: manifest phase-1 evidence diverged")
if gateway_summaries[0] != gateway_summaries[1]:
    raise SystemExit("remote reconciliation failed: manifest gateway readiness diverged")
PY
# An explicit optional OpenShell disable scrubs the stale runtime enforcement
# settings during the remote transaction.  Clear a repository-update hold only
# here, after the outer controller has independently proved exact source and
# matching durable post manifests.  A partial or stale deployment must remain
# fail-closed.
if [ "$clear_repo_update_blocker" = 1 ]; then
  configured_blocker="${MAC_REPO_UPDATE_DISPATCH_BLOCKER_FILE:-}"
  env_file="$mac_home/mac.env"
  if [ -z "$configured_blocker" ] && [ -f "$env_file" ]; then
    if ! configured_blocker="$(
      set +u
      # shellcheck disable=SC1090 -- this is the managed worker environment.
      if ! . "$env_file" >/dev/null 2>&1; then
        exit 1
      fi
      printf '%s' "${MAC_REPO_UPDATE_DISPATCH_BLOCKER_FILE:-}"
    )"; then
      echo "remote reconciliation failed: could not resolve repository-update blocker path" >&2
      exit 1
    fi
  fi
  "$python_bin" - "$mac_home" "$configured_blocker" <<'PY'
import os
import sys
from pathlib import Path

mac_home = Path(sys.argv[1]).expanduser()
configured = sys.argv[2].strip()
raw_paths = [str(mac_home / "repo-update-dispatch-blocked.json")]
if configured:
    raw_paths.append(configured)

seen = set()
for raw in raw_paths:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = mac_home / path
    key = os.path.abspath(os.fspath(path))
    if key in seen:
        continue
    seen.add(key)
    try:
        Path(key).unlink()
    except FileNotFoundError:
        pass
PY
  echo "repository-update dispatch hold cleared after exact deployment reconciliation"
fi
# The remote transaction already performed the role-aware health check before
# writing the post manifest.  Do not unconditionally probe loopback here: a
# spoke intentionally has no local control plane, so 127.0.0.1:8789 is not a
# valid reconciliation target for it.
echo "remote reconciliation succeeded for $agent"
REMOTE
    then
      return 0
    fi
    echo "==> ${agent}: reconcile_remote_deploy attempt ${_attempt}/${_max_retries} failed" >&2
  done
  echo "==> ${agent}: reconcile_remote_deploy failed after ${_max_retries} attempt(s)" >&2
  return 1
}

remote_daemon_quiescence_attestation() {
  local agent="$1" deployment_id="$2"
  local ssh_parts=() ssh_args=() ssh_target item last_index fence_exec
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  fence_exec="$(remote_deployment_fenced_exec "$deployment_id" 0 python3 -)"
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
    "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_ATTEST_AGENT=$(shell_quote "$agent") MAC_DEPLOY_ATTEST_TS=$(shell_quote "$TS") MAC_DEPLOY_ATTEST_REV=$(shell_quote "$GIT_REV") MAC_DEPLOY_ATTEST_GENERATION=$(shell_quote "$deployment_id") $fence_exec" <<'PY'
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

agent = os.environ["MAC_DEPLOY_ATTEST_AGENT"]
deploy_ts = os.environ["MAC_DEPLOY_ATTEST_TS"]
revision = os.environ["MAC_DEPLOY_ATTEST_REV"]
generation = os.environ["MAC_DEPLOY_ATTEST_GENERATION"]
mac_home = Path.home() / ".mac"
manifest_path = mac_home / "logs" / ("deploy-manifest-%s-post.json" % deploy_ts)
latest_path = mac_home / "logs" / "deploy-manifest-latest.json"
deadline = time.monotonic() + 120.0


def fail(message):
    raise SystemExit("daemon quiescence attestation failed: " + message)


def remaining():
    value = deadline - time.monotonic()
    if value <= 0:
        fail("total deadline expired")
    return value


def run_bounded(argv, env):
    if not argv or not os.path.isabs(str(argv[0])):
        fail("runtime executable is not absolute")
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                [str(item) for item in argv],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                env=env,
                start_new_session=True,
            )
        except OSError:
            fail("runtime command could not start")
        try:
            process.wait(timeout=min(20.0, remaining()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            fail("runtime command timed out")
        if output.tell() > 4 * 1024 * 1024 or errors.tell() > 4 * 1024 * 1024:
            fail("runtime command output exceeded its bound")
        output.seek(0)
        errors.seek(0)
        raw = output.read(4 * 1024 * 1024 + 1) + errors.read(4 * 1024 * 1024 + 1)
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            fail("runtime command output is not UTF-8")
        return process.returncode, text


def read_manifest(path, label):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        fail(label + " is unreadable")
    if (
        not isinstance(data, dict)
        or data.get("stage") != "post"
        or data.get("agent") != agent
        or (data.get("deploy") or {}).get("timestamp") != deploy_ts
        or (data.get("deploy") or {}).get("mac_git_rev") != revision
    ):
        fail(label + " belongs to another release")
    summary = data.get("daemon_resource_quiescence")
    phase1 = data.get("phase1_cohort_quiescence")
    gateway = data.get("gateway_readiness")
    if not isinstance(summary, dict):
        fail(label + " lacks daemon evidence")
    if not isinstance(phase1, dict):
        fail(label + " lacks phase-1 evidence")
    if not isinstance(gateway, dict):
        fail(label + " lacks gateway evidence")
    return summary, phase1, gateway


summary, phase1_summary, gateway_summary = read_manifest(
    manifest_path, "post manifest"
)
if read_manifest(latest_path, "latest manifest") != (
    summary,
    phase1_summary,
    gateway_summary,
):
    fail("post and latest manifest evidence diverged")
required_phases = summary.get("required_phases")
proved_phases = summary.get("proved_phases")
if (
    summary.get("schema") != "mac.daemon_resource_quiescence_manifest.v1"
    or summary.get("status") != "proved"
    or summary.get("generation") != generation
    or summary.get("revision") != revision
    or not isinstance(required_phases, list)
    or not required_phases
    or not isinstance(proved_phases, list)
    or not set(required_phases).issubset(set(proved_phases))
):
    fail("manifest daemon evidence is invalid")
receipt_path = mac_home / ("daemon-resource-quiescence-%s.json" % generation)
if summary.get("path") != str(receipt_path):
    fail("manifest names a noncanonical daemon receipt")
try:
    metadata = receipt_path.lstat()
    raw_receipt = receipt_path.read_bytes()
except OSError:
    fail("daemon receipt is unreadable")
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != os.getuid()
    or stat.S_IMODE(metadata.st_mode) != 0o600
    or len(raw_receipt) > 4 * 1024 * 1024
):
    fail("daemon receipt is not owner-private and bounded")
digest = hashlib.sha256(raw_receipt).hexdigest()
if digest != summary.get("sha256"):
    fail("daemon receipt digest changed")
try:
    receipt = json.loads(raw_receipt)
except (TypeError, ValueError):
    fail("daemon receipt is malformed")
if not isinstance(receipt, dict):
    fail("daemon receipt is not an object")
runtimes = receipt.get("container_runtimes")
proofs = receipt.get("proofs")
if (
    receipt.get("schema") != "mac.daemon_resource_quiescence.v1"
    or receipt.get("generation") != generation
    or receipt.get("revision") != revision
    or runtimes != summary.get("container_runtimes")
    or not isinstance(proofs, dict)
):
    fail("daemon receipt identity is invalid")
for phase in required_phases:
    proof = proofs.get(phase)
    if (
        not isinstance(proof, dict)
        or proof.get("stable_absence_observations") != 2
        or proof.get("container_runtimes") != runtimes
    ):
        fail("daemon receipt lacks a required phase")

phase1_path = mac_home / ("phase1-cohort-quiescence-%s.json" % generation)
phase1_daemon_summary = phase1_summary.get("daemon_resource_receipt")
if (
    phase1_summary.get("schema")
    != "mac.phase1_cohort_quiescence_manifest.v1"
    or phase1_summary.get("status") != "proved"
    or phase1_summary.get("path") != str(phase1_path)
    or phase1_summary.get("generation") != generation
    or phase1_summary.get("revision") != revision
    or not isinstance(phase1_summary.get("supervisor"), dict)
    or not isinstance(phase1_daemon_summary, dict)
    or phase1_daemon_summary.get("schema")
    != "mac.daemon_resource_quiescence.v1"
    or phase1_daemon_summary.get("proof_phase") != "pre_source"
):
    fail("manifest phase-1 evidence is invalid")
try:
    phase1_metadata = phase1_path.lstat()
    raw_phase1 = phase1_path.read_bytes()
except OSError:
    fail("phase-1 cohort receipt is unreadable")
phase1_digest = hashlib.sha256(raw_phase1).hexdigest()
if (
    not stat.S_ISREG(phase1_metadata.st_mode)
    or phase1_metadata.st_uid != os.getuid()
    or stat.S_IMODE(phase1_metadata.st_mode) != 0o600
    or len(raw_phase1) > 4 * 1024 * 1024
    or phase1_digest != phase1_summary.get("sha256")
):
    fail("phase-1 cohort receipt changed or is unsafe")
try:
    phase1_receipt = json.loads(raw_phase1)
except (TypeError, ValueError):
    fail("phase-1 cohort receipt is malformed")
phase1_daemon = (
    phase1_receipt.get("daemon_resource_receipt")
    if isinstance(phase1_receipt, dict)
    else None
)
if (
    not isinstance(phase1_receipt, dict)
    or phase1_receipt.get("schema") != "mac.phase1_cohort_quiescence.v1"
    or phase1_receipt.get("agent") != agent
    or phase1_receipt.get("revision") != revision
    or phase1_receipt.get("generation") != generation
    or phase1_receipt.get("supervisor") != phase1_summary.get("supervisor")
    or phase1_daemon != phase1_daemon_summary
):
    fail("phase-1 cohort receipt identity is invalid")
phase1_daemon_digest = phase1_daemon.get("sha256")
phase1_function_digest = phase1_daemon.get("function_block_sha256")
if (
    not isinstance(phase1_daemon_digest, str)
    or len(phase1_daemon_digest) != 64
    or not isinstance(phase1_function_digest, str)
    or len(phase1_function_digest) != 64
):
    fail("phase-1 daemon binding is invalid")

gateway_path = mac_home / "logs" / "gateway-readiness.json"
if (
    gateway_summary.get("schema") != "mac.gateway_readiness_manifest.v1"
    or gateway_summary.get("status") != "proved"
    or gateway_summary.get("path") != str(gateway_path)
    or gateway_summary.get("generation") != generation
    or gateway_summary.get("revision") != revision
    or gateway_summary.get("implementation")
    not in {"hermes", "openclaw", "nemoclaw", "none"}
    or gateway_summary.get("supervisor")
    not in {"systemd", "launchd", "supervisord"}
    or gateway_summary.get("stable_observations") != 2
    or not isinstance(gateway_summary.get("identities"), dict)
    or gateway_summary.get("implementation")
    != summary.get("gateway_implementation")
):
    fail("manifest gateway evidence is invalid")
try:
    gateway_metadata = gateway_path.lstat()
    raw_gateway = gateway_path.read_bytes()
except OSError:
    fail("gateway readiness receipt is unreadable")
if (
    not stat.S_ISREG(gateway_metadata.st_mode)
    or gateway_metadata.st_uid != os.getuid()
    or stat.S_IMODE(gateway_metadata.st_mode) != 0o600
    or len(raw_gateway) > 1024 * 1024
    or hashlib.sha256(raw_gateway).hexdigest() != gateway_summary.get("sha256")
):
    fail("gateway readiness receipt changed or is unsafe")
try:
    gateway_receipt = json.loads(raw_gateway)
except (TypeError, ValueError):
    fail("gateway readiness receipt is malformed")
if (
    not isinstance(gateway_receipt, dict)
    or gateway_receipt.get("schema") != "mac.gateway_readiness.v1"
    or gateway_receipt.get("agent") != agent
    or gateway_receipt.get("generation") != generation
    or gateway_receipt.get("revision") != revision
    or gateway_receipt.get("supervisor") != gateway_summary.get("supervisor")
    or gateway_receipt.get("implementation")
    != gateway_summary.get("implementation")
    or gateway_receipt.get("identities") != gateway_summary.get("identities")
    or gateway_receipt.get("state") != gateway_summary.get("state")
    or gateway_receipt.get("stable_observations") != 2
    or not isinstance(gateway_receipt.get("observed_at"), str)
    or not gateway_receipt["observed_at"]
):
    fail("gateway readiness receipt identity is invalid")

clean_env = os.environ.copy()
for key in (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "CONTAINER_HOST",
    "CONTAINER_CONNECTION",
    "PODMAN_HOST",
    "PODMAN_CONNECTION",
):
    clean_env.pop(key, None)


def podman_machine_prefix(path, runtime):
    selector = runtime.get("selector")
    endpoint = runtime.get("endpoint")
    if (
        not isinstance(selector, list)
        or len(selector) != 2
        or selector[0] != "--connection"
        or not isinstance(selector[1], str)
    ):
        fail("Podman machine selector is invalid")
    match = re.fullmatch(
        r"podman-machine://([^@]+)@127[.]0[.]0[.]1:([0-9]+)(/.*)", endpoint
    )
    if match is None:
        fail("Podman machine endpoint is invalid")
    machine_name, expected_port_raw, expected_path = match.groups()
    rc, listed = run_bounded(
        [path, "system", "connection", "list", "--format", "json"], clean_env
    )
    if rc != 0:
        fail("Podman connection inventory failed")
    try:
        connections = json.loads(listed)
    except (TypeError, ValueError):
        fail("Podman connection inventory is malformed")
    matches = [
        item for item in connections
        if isinstance(item, dict)
        and (item.get("Name") or item.get("name")) == selector[1]
    ] if isinstance(connections, list) else []
    if len(matches) != 1:
        fail("Podman machine connection identity changed")
    connection = matches[0]
    uri = connection.get("URI") or connection.get("Uri") or connection.get("uri")
    try:
        parsed = urlsplit(uri)
        observed_port = parsed.port
    except (TypeError, ValueError):
        fail("Podman machine URI is malformed")
    if (
        parsed.scheme != "ssh"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or observed_port != int(expected_port_raw)
        or parsed.path != expected_path
    ):
        fail("Podman machine connection endpoint changed")
    is_machine = connection.get("IsMachine")
    if is_machine is None:
        is_machine = connection.get("isMachine")
    if is_machine is not None and not isinstance(is_machine, bool):
        fail("Podman machine ownership is malformed")
    if is_machine is False:
        fail("Podman machine ownership was withdrawn")
    if is_machine is None:
        rc, inspected = run_bounded([path, "machine", "inspect", machine_name], clean_env)
        if rc != 0:
            fail("Podman machine ownership cannot be inspected")
        try:
            machines = json.loads(inspected)
        except (TypeError, ValueError):
            fail("Podman machine inspection is malformed")
        if not isinstance(machines, list) or len(machines) != 1:
            fail("Podman machine inspection is ambiguous")
        machine = machines[0]
        if not isinstance(machine, dict):
            fail("Podman machine inspection has an invalid entry")
        ssh_config = machine.get("SSHConfig")
        port = ssh_config.get("Port") if isinstance(ssh_config, dict) else None
        if port is None:
            port = machine.get("Port")
        try:
            port = int(port)
        except (TypeError, ValueError):
            fail("Podman machine inspection lacks its port")
        if (machine.get("Name") or machine.get("name")) != machine_name or port != observed_port:
            fail("Podman machine inspection does not match its connection")
    return [path] + selector


def runtime_prefix(runtime):
    if not isinstance(runtime, dict):
        fail("runtime identity is invalid")
    kind = runtime.get("kind")
    path = runtime.get("path")
    endpoint = runtime.get("endpoint")
    selector = runtime.get("selector")
    if (
        kind not in {"docker", "podman"}
        or not isinstance(path, str)
        or not os.path.isabs(path)
        or not isinstance(endpoint, str)
        or not isinstance(selector, list)
    ):
        fail("runtime identity is incomplete")
    try:
        executable = Path(path).stat()
    except OSError:
        fail("runtime executable disappeared")
    if (
        not stat.S_ISREG(executable.st_mode)
        or executable.st_uid not in {0, os.getuid()}
        or executable.st_mode & 0o022
        or not os.access(path, os.X_OK)
    ):
        fail("runtime executable ownership is unsafe")
    if kind == "docker":
        if not endpoint.startswith("unix://"):
            fail("Docker endpoint is not directly node-local")
        return [path, "--host", endpoint]
    if endpoint.startswith("unix://"):
        return [path, "--url", endpoint]
    if endpoint == "podman-local://uid-%d" % os.getuid():
        if selector:
            fail("native Podman endpoint unexpectedly has a selector")
        return [path]
    if endpoint.startswith("podman-machine://"):
        return podman_machine_prefix(path, runtime)
    fail("Podman endpoint is not node-local")


def live_gateway_sample():
    supervisor = gateway_summary["supervisor"]
    identities = gateway_summary["identities"]
    if set(identities) != {"hermes", "openclaw", "nemoclaw"} or not all(
        isinstance(value, str) and value for value in identities.values()
    ):
        fail("gateway supervisor identities are invalid")
    result = {}
    if supervisor == "systemd":
        systemctl = shutil.which("systemctl")
        if not systemctl:
            fail("systemctl is unavailable")
        prefix = []
        if os.geteuid() != 0:
            sudo = shutil.which("sudo")
            if not sudo:
                fail("systemd inspection requires noninteractive sudo")
            prefix = [sudo, "-n"]
        for owner, name in identities.items():
            rc, text = run_bounded(
                prefix
                + [
                    systemctl,
                    "show",
                    name,
                    "--no-pager",
                    "--property=LoadState",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=MainPID",
                    "--property=NRestarts",
                ],
                clean_env,
            )
            fields = {}
            for line in text.splitlines():
                if not line or "=" not in line:
                    fail("systemd gateway state is malformed")
                key, value = line.split("=", 1)
                if key in fields:
                    fail("systemd gateway state is ambiguous")
                fields[key] = value
            if set(fields) != {
                "LoadState",
                "ActiveState",
                "SubState",
                "MainPID",
                "NRestarts",
            }:
                fail("systemd gateway state is incomplete")
            if fields.get("LoadState") == "not-found":
                if fields.get("ActiveState") != "inactive" or fields.get("MainPID") != "0":
                    fail("systemd gateway absent state is contradictory")
                result[owner] = {
                    "state": "absent",
                    "pid": 0,
                    "restarts": 0,
                    "enabled": "not-found",
                }
                continue
            if rc != 0:
                fail("systemd gateway state is unreadable")
            if fields.get("LoadState") != "loaded":
                fail("systemd gateway load state is unknown")
            if fields.get("ActiveState") == "active" and fields.get("SubState") == "running":
                try:
                    pid = int(fields.get("MainPID") or "0")
                    restarts = int(fields.get("NRestarts") or "0")
                except ValueError:
                    fail("systemd gateway counters are malformed")
                if pid <= 0 or restarts < 0:
                    fail("systemd gateway has no valid process")
                state = "running"
            elif (
                fields.get("ActiveState") in {"inactive", "failed"}
                and fields.get("MainPID") == "0"
            ):
                state = fields["ActiveState"]
                pid = 0
                restarts = 0
            else:
                fail("systemd gateway is transitional")
            enabled_rc, enabled_text = run_bounded(
                prefix + [systemctl, "is-enabled", name], clean_env
            )
            enabled_lines = [
                line.strip() for line in enabled_text.splitlines() if line.strip()
            ]
            if len(enabled_lines) != 1 or enabled_lines[0] not in {
                "enabled",
                "disabled",
                "masked",
                "static",
                "indirect",
            }:
                fail("systemd gateway enablement is ambiguous")
            enabled = enabled_lines[0]
            if enabled == "enabled" and enabled_rc != 0:
                fail("systemd gateway enablement is contradictory")
            result[owner] = {
                "state": state,
                "pid": pid,
                "restarts": restarts,
                "enabled": enabled,
            }
    elif supervisor == "launchd":
        launchctl = shutil.which("launchctl")
        if not launchctl:
            fail("launchctl is unavailable")
        domain = "gui/%d" % os.getuid()
        for owner, label in identities.items():
            rc, text = run_bounded(
                [launchctl, "print", domain + "/" + label], clean_env
            )
            absent_lines = [line.strip() for line in text.splitlines() if line.strip()]
            if rc == 113 and len(absent_lines) == 1 and "Could not find service" in absent_lines[0]:
                result[owner] = {"state": "absent", "pid": 0, "restarts": 0}
                continue
            if rc != 0:
                fail("launchd gateway state is unreadable")
            state = re.search(r"(?m)^\s*state\s*=\s*([^\s]+)", text)
            pid_match = re.search(r"(?m)^\s*pid\s*=\s*([0-9]+)", text)
            if not state or state.group(1) != "running" or not pid_match:
                fail("launchd gateway is loaded but not running")
            pid = int(pid_match.group(1))
            if pid <= 0:
                fail("launchd gateway has no valid process")
            result[owner] = {
                "state": "running",
                "pid": pid,
                "restarts": 0,
            }
    else:
        supervisorctl = shutil.which("supervisorctl")
        if not supervisorctl:
            fail("supervisorctl is unavailable")
        commands = [[supervisorctl]]
        sudo = shutil.which("sudo")
        if sudo and os.geteuid() != 0:
            commands.insert(0, [sudo, "-n", supervisorctl])
        manager_samples = []
        for command in commands:
            sample = {}
            unavailable = False
            for owner, name in identities.items():
                rc, text = run_bounded(
                    command[:-1] + [command[-1], "status", name], clean_env
                )
                lowered = text.lower()
                if rc != 0 and (
                    "no such file" in lowered or "connection refused" in lowered
                ):
                    unavailable = True
                    break
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if rc != 0 and lines == [name + ": ERROR (no such process)"]:
                    sample[owner] = {"state": "absent", "pid": 0, "restarts": 0}
                    continue
                line = lines[0] if len(lines) == 1 else ""
                fields = line.split()
                if len(fields) < 2 or fields[0] != name:
                    fail("supervisord gateway identity is ambiguous")
                if fields[1] == "RUNNING" and rc == 0:
                    pid_match = re.search(r"\bpid\s+([0-9]+)\b", line)
                    if pid_match is None:
                        fail("supervisord gateway lacks a process")
                    pid = int(pid_match.group(1))
                    if pid <= 0:
                        fail("supervisord gateway has no valid process")
                    sample[owner] = {
                        "state": "running",
                        "pid": pid,
                        "restarts": 0,
                    }
                elif fields[1] in {"STOPPED", "EXITED", "FATAL"} and rc in {0, 3}:
                    sample[owner] = {
                        "state": fields[1].lower(),
                        "pid": 0,
                        "restarts": 0,
                    }
                else:
                    fail("supervisord gateway is transitional")
            if not unavailable and sample not in manager_samples:
                manager_samples.append(sample)
        if not manager_samples:
            fail("no supervisord manager can prove gateway state")
        for owner in identities:
            running = [
                sample[owner]
                for sample in manager_samples
                if sample[owner]["state"] == "running"
            ]
            if len({item["pid"] for item in running}) > 1:
                fail("multiple supervisord managers own one gateway")
            configured = [
                sample[owner]
                for sample in manager_samples
                if sample[owner]["state"] != "absent"
            ]
            if running and any(item["state"] != "running" for item in configured):
                fail("multiple supervisord managers configure one gateway")
            if not running and len({item["state"] for item in configured}) > 1:
                fail("supervisord managers disagree on gateway state")
            result[owner] = (
                running[0]
                if running
                else (configured[0] if configured else manager_samples[0][owner])
            )
    implementation = gateway_summary["implementation"]
    if implementation != "none":
        selected = result[implementation]
        if selected["state"] != "running" or selected["pid"] <= 0:
            fail("selected gateway is not running")
        if supervisor == "systemd" and selected.get("enabled") != "enabled":
            fail("selected systemd gateway is not enabled")
    for owner, item in result.items():
        if owner == implementation:
            continue
        if supervisor == "systemd":
            if (
                item["state"] not in {"absent", "inactive"}
                or item["pid"] != 0
                or item.get("enabled")
                not in {"not-found", "disabled", "masked"}
            ):
                fail("non-selected systemd gateway is unsafe")
        elif supervisor == "launchd":
            if item["state"] != "absent":
                fail("non-selected launchd gateway is loaded")
        elif implementation == "openclaw" and owner == "hermes":
            if item["state"] not in {"absent", "stopped"}:
                fail("Hermes rollback gateway is not stopped")
        elif item["state"] != "absent":
            fail("non-selected supervisord gateway is configured")
    return result


prefixes = [runtime_prefix(runtime) for runtime in runtimes]
gateway_samples = []
for observation in range(2):
    gateway_samples.append(live_gateway_sample())
    for prefix in prefixes:
        rc, _ignored = run_bounded(prefix + ["info"], clean_env)
        if rc != 0:
            fail("container runtime became unreadable")
        rc, listed = run_bounded(
            prefix
            + [
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.service=nemoclaw-gateway",
                "--format",
                "{{.ID}}",
            ],
            clean_env,
        )
        if rc != 0:
            fail("container runtime inventory failed")
        if any(line.strip() for line in listed.splitlines()):
            fail("legacy Nemo container is present")
    if observation == 0:
        time.sleep(min(1.0, remaining()))
implementation = gateway_summary["implementation"]
if implementation != "none":
    first_gateway = gateway_samples[0][implementation]
    second_gateway = gateway_samples[1][implementation]
    if (
        first_gateway["pid"] != second_gateway["pid"]
        or first_gateway["restarts"] != second_gateway["restarts"]
    ):
        fail("selected gateway restarted during release attestation")

print(
    json.dumps(
        {
            "schema": "mac.daemon_resource_quiescence_attestation.v1",
            "agent": agent,
            "receipt_sha256": digest,
            "phase1_receipt_sha256": phase1_digest,
            "phase1_daemon_receipt_sha256": phase1_daemon_digest,
            "phase1_function_block_sha256": phase1_function_digest,
            "phase1_supervisor": phase1_summary.get("supervisor"),
            "generation": generation,
            "revision": revision,
            "gateway_implementation": gateway_summary.get("implementation"),
            "gateway_readiness_sha256": gateway_summary.get("sha256"),
            "gateway_supervisor": gateway_summary.get("supervisor"),
            "gateway_identities": gateway_summary.get("identities"),
            "required_phases": required_phases,
            "container_runtimes": runtimes,
            "stable_absence_observations": 2,
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
        sort_keys=True,
    )
)
PY
}

assert_phase1_attestation_matches_controller() {
  local agent_id="$1" attestation="$2"
  local phase1_ready="$TMPDIR_LOCAL/phase1-ready-${agent_id}.json"
  "$PYTHON_BIN" - "$phase1_ready" "$attestation" <<'PY'
import json
import sys

ready = json.load(open(sys.argv[1], encoding="utf-8"))
attestation = json.loads(sys.argv[2])
receipt = ready.get("receipt") if isinstance(ready, dict) else None
if (
    not isinstance(ready, dict)
    or ready.get("schema") != "mac.phase1_cohort_ready.v1"
    or not isinstance(receipt, dict)
    or receipt.get("schema") != "mac.phase1_cohort_quiescence.v1"
    or not isinstance(attestation, dict)
    or attestation.get("schema")
    != "mac.daemon_resource_quiescence_attestation.v1"
    or attestation.get("agent") != ready.get("agent")
    or attestation.get("generation") != ready.get("generation")
    or attestation.get("revision") != ready.get("revision")
    or attestation.get("phase1_receipt_sha256")
    != ready.get("receipt_sha256")
    or attestation.get("phase1_daemon_receipt_sha256")
    != (receipt.get("daemon_resource_receipt") or {}).get("sha256")
    or attestation.get("phase1_function_block_sha256")
    != (receipt.get("daemon_resource_receipt") or {}).get(
        "function_block_sha256"
    )
    or attestation.get("phase1_supervisor") != receipt.get("supervisor")
):
    raise SystemExit(
        "release attestation does not match the controller's phase-1 receipt"
    )
PY
}

restore_remote_agent_release_barrier() {
  local agent="$1" deployment_id="$2" generation="$3" agent_id="$4"
  local hold_reason="$5" prior_owned="$6"
  local ssh_parts=() ssh_args=() ssh_target item last_index restore_fence
  local rehold_output="" barrier_output="" rehold_rc=0 barrier_rc=0
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  restore_fence="$(remote_deployment_fenced_exec "$deployment_id" 0 bash -s)"

  # Re-establish the durable hub hold first.  Even when that fails, still
  # restore the node-local process barrier as an independent fail-closed layer.
  # Both operations are bounded: hub API calls have explicit timeouts and the
  # SSH transports have connect and keepalive failure bounds.
  if rehold_output="$(
    hub_agent_restart_gate rehold "$agent_id" "$generation" "" "$hold_reason" \
      "$prior_owned" 0 0
  )"; then
    rehold_rc=0
  else
    rehold_rc=$?
  fi
  if barrier_output="$(
    ssh -o BatchMode=yes -o ConnectTimeout=10 \
      -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
      "${ssh_args[@]}" "$ssh_target" \
      "MAC_DEPLOY_RELEASE_GENERATION=$(shell_quote "$generation") $restore_fence" <<'REMOTE_RESTORE'
set -euo pipefail
generation="${MAC_DEPLOY_RELEASE_GENERATION:?}"
set -a
. "$HOME/.mac/mac.env"
set +a
barrier_path="${MAC_WORKER_DEPLOY_BARRIER_FILE:?}"
tmp="${barrier_path}.tmp.$$"
printf '%s\n' "$generation" > "$tmp"
chmod 0600 "$tmp"
mv -f "$tmp" "$barrier_path"
REMOTE_RESTORE
  )"; then
    barrier_rc=0
  else
    barrier_rc=$?
  fi

  if [ "$rehold_rc" -eq 0 ] && [ "$barrier_rc" -eq 0 ]; then
    echo "hub_hold=reestablished process_barrier=restored agent_id=${agent_id}"
    return 0
  fi
  echo "release compensation failed: hub_hold_rc=${rehold_rc} process_barrier_rc=${barrier_rc}" >&2
  if [ -n "$rehold_output" ]; then
    printf 'hub hold compensation output: %s\n' "$rehold_output" >&2
  fi
  if [ -n "$barrier_output" ]; then
    printf 'process barrier compensation output: %s\n' "$barrier_output" >&2
  fi
  return 1
}

fail_release_with_compensation() {
  local primary_error="$1"
  shift
  local compensation_output="" compensation_rc=0
  if compensation_output="$(restore_remote_agent_release_barrier "$@" 2>&1)"; then
    echo "ERROR: ${primary_error}; compensation result: ${compensation_output}; cohort release aborted" >&2
  else
    compensation_rc=$?
    echo "ERROR: ${primary_error}; REQUIRED compensation failed (rc=${compensation_rc}); cohort release aborted" >&2
    if [ -n "$compensation_output" ]; then
      printf '%s\n' "$compensation_output" >&2
    fi
  fi
  # The primary operation failed.  Successful compensation restores safety;
  # it never converts the failed release attempt into success.
  return 1
}

hub_dispatch_hold_cas_available() {
  local hub_agent ssh_parts=() ssh_args=() ssh_target last_index item
  hub_agent="$(fleet_hub_agent)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    'set -euo pipefail; set -a; . "$HOME/.mac/mac.env"; set +a; export MAC_DEPLOY_GATE_ADMIN_TOKEN="${MAC_API_TOKEN:?}"; "$HOME/.mac/venv/bin/python" - <<'"'"'PY'"'"'
import json
import os
import urllib.request

hub_url = str(os.environ.get("MAC_HUB_URL") or "").rstrip("/")
request = urllib.request.Request(
    hub_url + "/openapi.json",
    headers={
        "Authorization": "Bearer " + os.environ["MAC_DEPLOY_GATE_ADMIN_TOKEN"],
        "Accept": "application/json",
    },
)
with urllib.request.urlopen(request, timeout=15) as response:
    payload = json.load(response)
paths = payload.get("paths") if isinstance(payload, dict) else None
required = {
    "/agents/{agent_id}/dispatch-hold/acquire",
    "/agents/{agent_id}/dispatch-hold/release",
    "/agents/dispatch-hold/release-batch",
}
if not isinstance(paths, dict) or not required.issubset(paths):
    raise SystemExit(1)
PY'
}

hub_dispatch_hold_transition_available() {
  # Successor-hold cutover depends on a distinct atomic operation. A hub that
  # only exposes release-batch would recreate the release/refreeze claim race,
  # so prove the POST operation itself before phase 1 mutates any worker.
  local hub_agent ssh_parts=() ssh_args=() ssh_target last_index item
  hub_agent="$(fleet_hub_agent)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    'set -euo pipefail; set -a; . "$HOME/.mac/mac.env"; set +a; export MAC_DEPLOY_GATE_ADMIN_TOKEN="${MAC_API_TOKEN:?}"; "$HOME/.mac/venv/bin/python" - <<'"'"'PY'"'"'
import json
import os
import urllib.request

hub_url = str(os.environ.get("MAC_HUB_URL") or "").rstrip("/")
request = urllib.request.Request(
    hub_url + "/openapi.json",
    headers={
        "Authorization": "Bearer " + os.environ["MAC_DEPLOY_GATE_ADMIN_TOKEN"],
        "Accept": "application/json",
    },
)
with urllib.request.urlopen(request, timeout=15) as response:
    payload = json.load(response)
paths = payload.get("paths") if isinstance(payload, dict) else None
operation = (
    paths.get("/agents/dispatch-hold/transition-batch")
    if isinstance(paths, dict)
    else None
)
if not isinstance(operation, dict) or not isinstance(operation.get("post"), dict):
    raise SystemExit(1)
PY'
}

preflight_cohort_hold_adoptions() {
  # Freeze the selected names/ids, read every live hub row once, then validate
  # all exact-reason authority locally.  No hold CAS, drain, stop, or other
  # mutation may precede this whole-cohort decision.
  local selected_specs_file="$1" plan_output="$2" hub_agent="$3"
  local cohort_file="$TMPDIR_LOCAL/hold-adoption-cohort.jsonl"
  local rows_file="$TMPDIR_LOCAL/hold-adoption-live-rows.json"
  local spec agent agent_id fleet_name item request_b64 rows_json
  local hub_ssh_parts=() hub_ssh_args=() hub_ssh_target last_index
  : > "$cohort_file"
  chmod 0600 "$cohort_file"
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a spec_fields <<<"$spec"
    agent="${spec_fields[0]}"
    agent_id="$(stable_worker_agent_id "$agent")"
    fleet_name="${spec_fields[23]:-mac}"
    "$PYTHON_BIN" - "$cohort_file" "$agent" "$agent_id" "$fleet_name" <<'PY'
import json
import sys

with open(sys.argv[1], "a", encoding="utf-8") as stream:
    stream.write(
        json.dumps(
            {"agent": sys.argv[2], "agent_id": sys.argv[3], "fleet": sys.argv[4]},
            sort_keys=True,
        )
        + "\n"
    )
PY
  done < "$selected_specs_file"

  local cohort_values selected_fleet selected_ids=()
  cohort_values="$($PYTHON_BIN - "$cohort_file" <<'PY'
import json
import sys

entries = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
if not entries:
    raise SystemExit("hold-adoption cohort is empty")
names = [entry["agent"] for entry in entries]
ids = [entry["agent_id"] for entry in entries]
fleets = {entry["fleet"] for entry in entries}
if len(set(names)) != len(names) or len(set(ids)) != len(ids):
    raise SystemExit("hold-adoption cohort contains duplicate names or stable ids")
if len(fleets) != 1:
    raise SystemExit("hold-adoption cohort spans multiple fleet identities")
print(next(iter(fleets)))
for agent_id in ids:
    print(agent_id)
PY
)"
  selected_fleet="${cohort_values%%$'\n'*}"
  while IFS= read -r agent_id; do
    [ -n "$agent_id" ] && selected_ids+=("$agent_id")
  done <<<"${cohort_values#*$'\n'}"

  if [ -n "$HOLD_ADOPTIONS_FILE" ]; then
    local validation_args=(
      "$PYTHON_BIN" "$ROOT/scripts/deploy-hold-adoptions.py" validate-selected
      "$HOLD_ADOPTIONS_FILE" --fleet "$selected_fleet" --hub-agent "$hub_agent"
    )
    for agent_id in "${selected_ids[@]}"; do
      validation_args+=(--agent "$agent_id")
    done
    "${validation_args[@]}"
  fi

  request_b64="$($PYTHON_BIN - "$cohort_file" <<'PY'
import base64
import json
import sys

agent_ids = [
    json.loads(line)["agent_id"]
    for line in open(sys.argv[1], encoding="utf-8")
    if line.strip()
]
print(base64.b64encode(json.dumps(agent_ids).encode("utf-8")).decode("ascii"))
PY
)"
  while IFS= read -r -d '' item; do hub_ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#hub_ssh_parts[@]} - 1))
  hub_ssh_target="${hub_ssh_parts[$last_index]}"
  hub_ssh_args=("${hub_ssh_parts[@]:0:$last_index}")
  # This remote bash consumes the local heredoc.  ``ssh -n`` would replace
  # stdin with /dev/null and silently turn the live cohort snapshot into an
  # empty response.
  rows_json="$(ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" \
    "MAC_DEPLOY_PREFLIGHT_AGENT_IDS_B64=$(shell_quote "$request_b64") bash -s" <<'REMOTE_HOLD_PREFLIGHT'
set -euo pipefail
set -a
. "$HOME/.mac/mac.env"
set +a
export MAC_DEPLOY_GATE_ADMIN_TOKEN="${MAC_API_TOKEN:?}"
"$HOME/.mac/venv/bin/python" - <<'PY'
import base64
import json
import os
import urllib.error
import urllib.request

agent_ids = json.loads(
    base64.b64decode(os.environ["MAC_DEPLOY_PREFLIGHT_AGENT_IDS_B64"])
)
if not isinstance(agent_ids, list) or any(not isinstance(value, str) for value in agent_ids):
    raise SystemExit("invalid hold-adoption preflight agent set")
hub_url = str(os.environ.get("MAC_HUB_URL") or "").rstrip("/")
token = os.environ["MAC_DEPLOY_GATE_ADMIN_TOKEN"]
rows = []
for agent_id in agent_ids:
    request = urllib.request.Request(
        hub_url + "/agents/%s" % agent_id,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            row = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            row = None
        else:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                "GET agent failed with HTTP %s: %s" % (exc.code, detail)
            ) from exc
    rows.append({"agent_id": agent_id, "row": row})
print(json.dumps(rows, sort_keys=True))
PY
REMOTE_HOLD_PREFLIGHT
)"
  printf '%s\n' "$rows_json" > "$rows_file"
  chmod 0600 "$rows_file"

  "$PYTHON_BIN" - "$cohort_file" "$rows_file" "$HOLD_ADOPTIONS_FILE" \
    "$REQUIRE_RELEASE_ALL_SELECTED" "$plan_output" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

cohort_path, rows_path, authority_path, require_all_raw, output_path = sys.argv[1:]
cohort = [
    json.loads(line)
    for line in open(cohort_path, encoding="utf-8")
    if line.strip()
]
live = json.load(open(rows_path, encoding="utf-8"))
if not isinstance(live, list) or len(live) != len(cohort):
    raise SystemExit("hub returned an incomplete hold-adoption preflight snapshot")
authority = {}
if authority_path:
    payload = json.load(open(authority_path, encoding="utf-8"))
    authority = {item["agent"]: item["reason"] for item in payload["adoptions"]}
require_all = require_all_raw == "1"
plan = []
for expected, observed in zip(cohort, live):
    agent_id = expected["agent_id"]
    if not isinstance(observed, dict) or observed.get("agent_id") != agent_id:
        raise SystemExit("hub returned a reordered or incorrect agent preflight snapshot")
    row = observed.get("row")
    if row is not None and (not isinstance(row, dict) or row.get("id") != agent_id):
        raise SystemExit("hub returned the wrong live agent during hold preflight")
    exists = row is not None and not row.get("deleted_at")
    held = bool(exists and row.get("dispatch_hold"))
    current_reason = str(row.get("dispatch_hold_reason") or "") if exists else ""
    authorized = authority.get(agent_id, "")
    if authorized and (not exists or not held):
        raise SystemExit("hold adoption became stale for agent %s" % agent_id)
    if authorized and current_reason != authorized:
        raise SystemExit("hold adoption reason drifted for agent %s" % agent_id)
    if require_all and held and not authorized:
        raise SystemExit("selected held agent lacks exact adoption authority: %s" % agent_id)
    plan.append(
        {
            "agent": expected["agent"],
            "agent_id": agent_id,
            "exists": exists,
            "held": held,
            "authorized_prior_reason": authorized,
            "require_owned_after_prepare": require_all,
        }
    )

output = Path(output_path)
descriptor, raw = tempfile.mkstemp(prefix=output.name + ".", dir=str(output.parent))
temporary = Path(raw)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(
            {
                "schema": "mac.dispatch_hold_adoption_plan.v1",
                "require_release_all_selected": require_all,
                "agents": plan,
            },
            stream,
            sort_keys=True,
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, output)
finally:
    temporary.unlink(missing_ok=True)
PY
  echo "==> fleet: exact hold-adoption preflight passed for ${#selected_ids[@]} selected agent(s)"
}

hold_adoption_reason_for_agent() {
  local plan="$1" agent_id="$2"
  "$PYTHON_BIN" - "$plan" "$agent_id" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
matches = [
    item for item in payload.get("agents", []) if item.get("agent_id") == sys.argv[2]
]
if len(matches) != 1:
    raise SystemExit("hold-adoption plan lacks the exact selected agent")
print(matches[0].get("authorized_prior_reason") or "")
PY
}

hub_agent_restart_gate() {
  # Run the dispatch barrier from the hub itself so the administrative bearer
  # never crosses onto a worker.  The hub's clock is also the only clock used
  # for the strict post-restart heartbeat comparison.
  local phase="$1" agent_id="$2" generation="${3:-}" baseline_seen="${4:-}"
  local hold_reason="${5:-}" prior_owned="${6:-0}" allow_missing="${7:-0}"
  local require_authenticated="${8:-0}"
  local prior_hold_reason="${9:-$hold_reason}"
  local expected_principal_id="${10:-}"
  local authorized_prior_reason="${11:-}"
  local require_owned_after_prepare="${12:-0}"
  local require_report_executor="${13:-0}"
  local hub_agent ssh_parts=() ssh_args=() ssh_target last_index item
  hub_agent="$(fleet_hub_agent)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
    "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_GATE_PHASE=$(shell_quote "$phase") MAC_DEPLOY_GATE_AGENT_ID=$(shell_quote "$agent_id") MAC_DEPLOY_GATE_GENERATION=$(shell_quote "$generation") MAC_DEPLOY_GATE_BASELINE=$(shell_quote "$baseline_seen") MAC_DEPLOY_GATE_HOLD_REASON=$(shell_quote "$hold_reason") MAC_DEPLOY_GATE_PRIOR_HOLD_REASON=$(shell_quote "$prior_hold_reason") MAC_DEPLOY_GATE_PRIOR_OWNED=$(shell_quote "$prior_owned") MAC_DEPLOY_GATE_ALLOW_MISSING=$(shell_quote "$allow_missing") MAC_DEPLOY_GATE_REQUIRE_AUTHENTICATED=$(shell_quote "$require_authenticated") MAC_DEPLOY_GATE_EXPECTED_PRINCIPAL_ID=$(shell_quote "$expected_principal_id") MAC_DEPLOY_GATE_ADOPT_REASON=$(shell_quote "$authorized_prior_reason") MAC_DEPLOY_GATE_REQUIRE_OWNED=$(shell_quote "$require_owned_after_prepare") MAC_DEPLOY_GATE_REQUIRE_REPORT_EXECUTOR=$(shell_quote "$require_report_executor") MAC_DEPLOY_GATE_TIMEOUT=$(shell_quote "${MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS:-1800}") bash -s" <<'REMOTE_HUB_GATE'
set -euo pipefail
set -a
# shellcheck source=/dev/null -- owner-only hub deployment environment.
. "$HOME/.mac/mac.env"
set +a
export MAC_DEPLOY_GATE_ADMIN_TOKEN="${MAC_API_TOKEN:?}"
"$HOME/.mac/venv/bin/python" - <<'PY'
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

phase = os.environ["MAC_DEPLOY_GATE_PHASE"]
agent_id = os.environ["MAC_DEPLOY_GATE_AGENT_ID"]
generation = os.environ.get("MAC_DEPLOY_GATE_GENERATION", "")
baseline_text = os.environ.get("MAC_DEPLOY_GATE_BASELINE", "")
hold_reason = os.environ.get("MAC_DEPLOY_GATE_HOLD_REASON", "")
prior_hold_reason = os.environ.get("MAC_DEPLOY_GATE_PRIOR_HOLD_REASON", "")
prior_owned = os.environ.get("MAC_DEPLOY_GATE_PRIOR_OWNED", "0") == "1"
allow_missing = os.environ.get("MAC_DEPLOY_GATE_ALLOW_MISSING", "0") == "1"
require_authenticated = (
    os.environ.get("MAC_DEPLOY_GATE_REQUIRE_AUTHENTICATED", "0") == "1"
)
expected_principal_id = os.environ.get("MAC_DEPLOY_GATE_EXPECTED_PRINCIPAL_ID", "")
authorized_prior_reason = os.environ.get("MAC_DEPLOY_GATE_ADOPT_REASON", "")
require_owned_after_prepare = os.environ.get("MAC_DEPLOY_GATE_REQUIRE_OWNED", "0") == "1"
require_report_executor = (
    os.environ.get("MAC_DEPLOY_GATE_REQUIRE_REPORT_EXECUTOR", "0") == "1"
)
timeout = max(1.0, float(os.environ.get("MAC_DEPLOY_GATE_TIMEOUT") or "1800"))
hub_url = str(os.environ.get("MAC_HUB_URL") or "").rstrip("/")
token = os.environ.get("MAC_DEPLOY_GATE_ADMIN_TOKEN") or ""
if not hub_url or not token or not agent_id:
    raise SystemExit("hub restart gate lacks URL, administrative authority, or agent id")
if require_authenticated and not expected_principal_id:
    raise SystemExit("authenticated deployment release lacks the newly issued principal id")


def api(method, path, body=None, *, missing_ok=False):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        hub_url + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if missing_ok and exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            "%s %s failed with HTTP %s: %s" % (method, path, exc.code, detail)
        ) from exc
    return json.loads(raw) if raw else None


def agent_row(*, missing_ok=False):
    row = api("GET", "/agents/%s" % agent_id, missing_ok=missing_ok)
    # A tombstone is historical identity, not a runnable worker.  During the
    # initial gate treat it exactly like an absent agent so the restarted,
    # administrator-authorized registration follows the prepare-new path and
    # immediately reacquires this deployment's hold before strict proof.
    if row is not None and row.get("deleted_at") and missing_ok:
        return None
    if row is None and not missing_ok:
        raise RuntimeError("agent %s is absent from the hub" % agent_id)
    if row is not None and (not isinstance(row, dict) or row.get("id") != agent_id):
        raise RuntimeError("GET /agents/%s returned the wrong agent" % agent_id)
    return row


def parse_seen(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def report_executor_ready(resources):
    if not require_report_executor:
        return True
    # The one-time held-hub bootstrap executes this embedded gate with the
    # previously deployed MAC package, which predates report-executor support.
    # Import the new validator only on post-upgrade paths that require it.
    from mac.models import agent_has_read_only_report_repository_executor

    startup = resources.get("startup_self_test")
    checks = startup.get("checks") if isinstance(startup, dict) else None
    attestation = resources.get("report_repository_executor_attestation")
    return bool(
        agent_has_read_only_report_repository_executor(resources)
        and isinstance(startup, dict)
        and startup.get("schema") == "mac.agent_startup_self_test.v1"
        and startup.get("agent_id") == agent_id
        and startup.get("status") in {"passed", "degraded"}
        and startup.get("blocking_problems") == []
        and isinstance(checks, dict)
        and checks.get("openshell_executor_config") is True
        and checks.get("report_repository_executor_attestation") is True
        and startup.get("report_repository_executor_attestation") == attestation
    )


def post_drain():
    # This is an explicit operator/deployment transition, not a worker claim of
    # identity. Use the admin-only agent update route so it remains valid after
    # exact worker identity enforcement.
    row = api(
        "PUT",
        "/agents/%s" % agent_id,
        {"status": "draining", "health_status": "degraded"},
    )
    if not isinstance(row, dict) or row.get("id") != agent_id:
        raise RuntimeError("drain heartbeat returned the wrong agent")
    if row.get("status") != "draining":
        raise RuntimeError("hub did not persist the draining state")
    return row


def active_work():
    active = []
    for state in ("claimed", "running"):
        payload = api("GET", "/tasks?state=%s" % state)
        if not isinstance(payload, list):
            raise RuntimeError("GET /tasks?state=%s did not return a list" % state)
        active.extend(
            task
            for task in payload
            if isinstance(task, dict)
            and task.get("owner_agent_id") == agent_id
            and task.get("lease_id")
            and task.get("state") == state
        )
    row = agent_row()
    current_task_id = row.get("current_task_id")
    if current_task_id and not any(task.get("id") == current_task_id for task in active):
        active.append(
            {
                "id": current_task_id,
                "state": "agent_current_task",
                "owner_agent_id": agent_id,
            }
        )
    return active


def cas_hold(reason, *, expected_hold, expected_reason=None):
    body = {"reason": reason, "expected_dispatch_hold": bool(expected_hold)}
    if expected_hold:
        body["expected_reason"] = expected_reason
    payload = api(
        "POST",
        "/agents/%s/dispatch-hold/acquire" % agent_id,
        body,
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("agent"), dict):
        raise RuntimeError("dispatch hold CAS returned an invalid response")
    return bool(payload.get("changed")), payload["agent"]


if phase == "legacy-bootstrap":
    if authorized_prior_reason:
        raise RuntimeError("legacy CAS bootstrap rejects dispatch-hold adoption")
    row = agent_row()
    if not row.get("dispatch_hold"):
        raise RuntimeError(
            "legacy CAS bootstrap requires the live hub agent to be pre-held"
        )
    post_drain()
    deadline = time.monotonic() + timeout
    while True:
        active = active_work()
        if not active:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "active work did not drain before legacy hub bootstrap: %s"
                % ",".join(str(task.get("id")) for task in active)
            )
        time.sleep(5)
    row = post_drain()
    if not row.get("dispatch_hold"):
        raise RuntimeError("pre-existing hub hold disappeared during CAS bootstrap")
    print(
        json.dumps(
            {
                "exists": True,
                "baseline_seen": str(row.get("last_seen_at") or ""),
                "owns_hold": False,
            },
            sort_keys=True,
        )
    )
    raise SystemExit(0)

if phase == "prepare-new":
    deadline = time.monotonic() + min(timeout, 300.0)
    while agent_row(missing_ok=True) is None:
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "new worker never atomically registered under its local deployment barrier"
            )
        time.sleep(2)
    phase = "prepare"

if phase == "prepare":
    row = agent_row(missing_ok=allow_missing)
    if row is None:
        if authorized_prior_reason:
            raise RuntimeError("hold adoption became stale because the agent is absent")
        print(json.dumps({"exists": False, "baseline_seen": "", "owns_hold": False}))
        raise SystemExit(0)
    if not hold_reason:
        raise RuntimeError("deployment hold reason is required")
    owns_hold = False
    if authorized_prior_reason:
        if not bool(row.get("dispatch_hold")):
            raise RuntimeError("hold adoption became stale because the hold was cleared")
        if row.get("dispatch_hold_reason") != authorized_prior_reason:
            raise RuntimeError("hold adoption reason changed before exact CAS")
        changed, row = cas_hold(
            hold_reason,
            expected_hold=True,
            expected_reason=authorized_prior_reason,
        )
        if not changed:
            raise RuntimeError("exact dispatch-hold adoption CAS did not change the hold")
        if not row.get("dispatch_hold") or row.get("dispatch_hold_reason") != hold_reason:
            raise RuntimeError("exact dispatch-hold adoption returned the wrong hold")
        owns_hold = True
    elif bool(row.get("dispatch_hold")):
        if require_owned_after_prepare:
            # Exact full-cohort mode never upgrades a stale controller's
            # remembered reason into current ownership. Without explicit
            # adoption authority, only this invocation's unique reason counts.
            owns_hold = prior_owned and row.get("dispatch_hold_reason") == hold_reason
        else:
            owns_hold = prior_owned and row.get("dispatch_hold_reason") in {
                hold_reason,
                prior_hold_reason,
            }
        if owns_hold and row.get("dispatch_hold_reason") != hold_reason:
            changed, row = cas_hold(
                hold_reason,
                expected_hold=True,
                expected_reason=row.get("dispatch_hold_reason"),
            )
            owns_hold = changed
    else:
        changed, row = cas_hold(
            hold_reason,
            expected_hold=False,
        )
        owns_hold = changed
    if not row.get("dispatch_hold"):
        raise RuntimeError("deployment dispatch-hold CAS lost without a replacement hold")
    if owns_hold and row.get("dispatch_hold_reason") != hold_reason:
        raise RuntimeError("hub persisted an unexpected deployment hold reason")
    if require_owned_after_prepare and not owns_hold:
        raise RuntimeError(
            "exact full-cohort release lost deployment hold ownership before drain"
        )
    post_drain()
    deadline = time.monotonic() + timeout
    while True:
        active = active_work()
        if not active:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "active work did not drain before worker restart: %s"
                % ",".join(str(task.get("id")) for task in active)
            )
        time.sleep(5)
    # A lease renewal can project BUSY while we wait. Reassert the drain only
    # after the attachment set is empty, then take the hub-side baseline.
    row = post_drain()
    if not row.get("dispatch_hold"):
        raise RuntimeError("dispatch hold disappeared while active leases drained")
    if owns_hold and row.get("dispatch_hold_reason") != hold_reason:
        raise RuntimeError("deployment-owned dispatch hold was replaced")
    print(
        json.dumps(
            {
                "exists": True,
                "baseline_seen": str(row.get("last_seen_at") or ""),
                "owns_hold": owns_hold,
            },
            sort_keys=True,
        )
    )
elif phase == "verify":
    if not generation or not baseline_text:
        raise RuntimeError("restart verification lacks generation or hub baseline")
    baseline = parse_seen(baseline_text)
    deadline = time.monotonic() + min(timeout, 300.0)
    last_error = "heartbeat not observed"
    while time.monotonic() < deadline:
        row = agent_row()
        resources = row.get("resources") if isinstance(row.get("resources"), dict) else {}
        seen = parse_seen(row.get("last_seen_at"))
        if (
            seen is not None
            and baseline is not None
            and seen > baseline
            and row.get("status") == "draining"
            and row.get("health_status") == "degraded"
            and row.get("current_task_id") is None
            and bool(row.get("dispatch_hold"))
            and resources.get("deployment_generation") == generation
        ):
            print(json.dumps({"agent_id": agent_id, "last_seen_at": row["last_seen_at"]}))
            break
        last_error = "agent lacks strict generation, drain, hold, or clock proof"
        time.sleep(2)
    else:
        raise RuntimeError("restarted worker failed strict heartbeat proof: " + last_error)
elif phase in {"arm", "release"}:
    if not generation or not baseline_text:
        raise RuntimeError("deployment release lacks generation or hub baseline")
    baseline = parse_seen(baseline_text)
    deadline = time.monotonic() + min(timeout, 300.0)
    last_error = "worker-generated idle heartbeat not observed"
    while time.monotonic() < deadline:
        row = agent_row()
        resources = row.get("resources") if isinstance(row.get("resources"), dict) else {}
        authenticated = resources.get("worker_credential_authenticated")
        seen = parse_seen(row.get("last_seen_at"))
        auth_ok = not require_authenticated or (
            isinstance(authenticated, dict)
            and authenticated.get("agent_id") == agent_id
            and authenticated.get("principal_id") == expected_principal_id
        )
        if (
            seen is not None
            and baseline is not None
            and seen > baseline
            and row.get("status") == "idle"
            and row.get("health_status") == "healthy"
            and row.get("current_task_id") is None
            and bool(row.get("dispatch_hold"))
            and resources.get("deployment_generation") == generation
            and auth_ok
            and report_executor_ready(resources)
            and not active_work()
        ):
            break
        last_error = "agent lacks strict idle, healthy, generation, hold, lease, credential, or report-executor proof"
        time.sleep(2)
    else:
        raise RuntimeError("deployment release proof failed: " + last_error)
    if phase == "arm":
        print(
            json.dumps(
                {
                    "agent_id": agent_id,
                    "release_ready": True,
                    "hold_reason": row.get("dispatch_hold_reason"),
                },
                sort_keys=True,
            )
        )
        raise SystemExit(0)
    cleared = False
    if prior_owned:
        if row.get("dispatch_hold_reason") != hold_reason:
            raise RuntimeError("refusing to clear a hold no longer owned by this deployment")
        payload = api(
            "POST",
            "/agents/%s/dispatch-hold/release" % agent_id,
            {"reason": hold_reason},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("agent"), dict):
            raise RuntimeError("dispatch hold release CAS returned an invalid response")
        if not payload.get("released"):
            raise RuntimeError("deployment dispatch hold ownership changed before release")
        row = payload["agent"]
        cleared = True
    print(json.dumps({"agent_id": agent_id, "hold_cleared": cleared}, sort_keys=True))
elif phase == "rehold":
    row = agent_row()
    if not row.get("dispatch_hold"):
        _changed, row = cas_hold(hold_reason, expected_hold=False)
    if not row.get("dispatch_hold"):
        raise RuntimeError("could not restore a durable dispatch hold")
    if prior_owned and row.get("dispatch_hold_reason") != hold_reason:
        raise RuntimeError(
            "could not restore the deployment-owned dispatch hold exactly"
        )
    row = post_drain()
    if not row.get("dispatch_hold"):
        raise RuntimeError("restored dispatch hold disappeared during compensation")
    if prior_owned and row.get("dispatch_hold_reason") != hold_reason:
        raise RuntimeError(
            "deployment-owned dispatch hold changed during compensation"
        )
    if active_work():
        raise RuntimeError("active work attached while release compensation ran")
    row = agent_row()
    if not row.get("dispatch_hold"):
        raise RuntimeError("restored dispatch hold disappeared after compensation")
    if prior_owned and row.get("dispatch_hold_reason") != hold_reason:
        raise RuntimeError(
            "deployment-owned dispatch hold changed after compensation"
        )
    print(
        json.dumps(
            {
                "agent_id": agent_id,
                "dispatch_hold": True,
                "dispatch_hold_reason": row.get("dispatch_hold_reason"),
            },
            sort_keys=True,
        )
    )
else:
    raise RuntimeError("unsupported hub restart gate phase: %s" % phase)
PY
REMOTE_HUB_GATE
}

remote_deployment_hold_state() {
  local agent="$1" ssh_parts=() ssh_args=() ssh_target last_index item
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    'python3 - <<'"'"'PY'"'"'
import json
from pathlib import Path

path = Path.home() / ".mac" / "deploy-dispatch-hold.json"
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (FileNotFoundError, OSError, ValueError, TypeError):
    payload = {}
print(json.dumps(payload if isinstance(payload, dict) else {}, sort_keys=True))
PY'
}

deployment_id_for_agent() {
  local agent="$1"
  printf '%s:%s:%s:%s' "$GIT_REV" "$agent" "$TS" "$DEPLOY_CONTROLLER_NONCE"
}

worker_generation_for_agent() {
  # The runtime and rollback generation are the same fenced deployment
  # identity. Keeping one value prevents the hub readiness proof from being
  # satisfied by an artifact generation different from the rollback owner.
  deployment_id_for_agent "$1"
}

cohort_journal() {
  "$PYTHON_BIN" "$COHORT_JOURNAL_HELPER" \
    --directory "$COHORT_JOURNAL_DIR" "$@"
}

cohort_journal_revision() {
  "$PYTHON_BIN" -c \
    'import json,sys; payload=json.load(sys.stdin); print(payload["journal"]["revision"])'
}

cohort_journal_mutate() {
  local command="$1" epoch_id="$2" revision="$3" operation_id="$4" owner_nonce="$5"
  shift 5
  local result
  result="$(cohort_journal "$command" \
    --epoch "$epoch_id" \
    --expected-revision "$revision" \
    --operation-id "$operation_id" \
    --owner-nonce "$owner_nonce" "$@")"
  COHORT_JOURNAL_REVISION="$(printf '%s' "$result" | cohort_journal_revision)"
  printf '%s\n' "$result"
}

write_cohort_transaction_input() {
  local selected_specs_file="$1" output="$2"
  local identities="$TMPDIR_LOCAL/fleet-cohort-identities.txt"
  local spec fields=() name stable_id generation deployment_id os_kind supervisor report_required
  : > "$identities"
  chmod 0600 "$identities"
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a fields <<<"$spec"
    name="${fields[0]}"
    stable_id="$(stable_worker_agent_id "$name")"
    generation="$(worker_generation_for_agent "$name")"
    deployment_id="$(deployment_id_for_agent "$name")"
    os_kind="${fields[2]:-unknown}"
    supervisor="${fields[14]:-auto}"
    report_required=0
    if spec_requires_report_repository_executor "$spec"; then report_required=1; fi
    printf '%s|%s|%s|%s|%s|%s|%s\n' \
      "$name" "$stable_id" "$generation" "$deployment_id" \
      "$os_kind" "$supervisor" "$report_required" >> "$identities"
  done < "$selected_specs_file"
  "$PYTHON_BIN" - "$identities" "$output" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

source, output_raw = sys.argv[1:]
nodes = []
for line in Path(source).read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    fields = line.split("|")
    if len(fields) != 7:
        raise SystemExit("cohort identity input is malformed")
    name, stable_id, generation, deployment_id, os_kind, supervisor, report_required = fields
    nodes.append(
        {
            "name": name,
            "stable_id": stable_id,
            "generation": generation,
            "deployment_id": deployment_id,
            "os": os_kind,
            "supervisor": supervisor,
            "report_executor_required": report_required == "1",
        }
    )
if not nodes:
    raise SystemExit("cohort transaction input is empty")
output = Path(output_raw)
descriptor, temporary_raw = tempfile.mkstemp(prefix=output.name + ".", dir=str(output.parent))
temporary = Path(temporary_raw)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(nodes, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
finally:
    temporary.unlink(missing_ok=True)
PY
}

initialize_cohort_transaction() {
  local selected_specs_file="$1" hub_agent="$2" fleet_name="$3"
  local cohort_file="$TMPDIR_LOCAL/fleet-cohort.json" result
  write_cohort_transaction_input "$selected_specs_file" "$cohort_file"
  local init_args=(
    init
    --epoch "$COHORT_EPOCH_ID"
    --source-commit "$GIT_REV"
    --deploy-ts "$TS"
    --fleet "$fleet_name"
    --hub-agent "$hub_agent"
    --cohort-file "$cohort_file"
    --owner-nonce "$DEPLOY_CONTROLLER_NONCE"
    --owner-pid "$$"
  )
  if [ -n "$SUCCESSOR_HOLD_REASON" ]; then
    init_args+=(--successor-hold "$SUCCESSOR_HOLD_REASON")
  fi
  if [ "$REQUIRE_RELEASE_ALL_SELECTED" = 1 ]; then
    init_args+=(--require-release-all-selected)
  fi
  result="$(cohort_journal "${init_args[@]}")"
  COHORT_JOURNAL_REVISION="$(printf '%s' "$result" | cohort_journal_revision)"
  COHORT_JOURNAL_ACTIVE=1
}

legacy_hub_bootstrap() {
  local selected_specs_file="$1" hub_agent="$2" selected_count="$3"
  local spec fields=() agent supervisor fleet_name os_kind authority_file
  [ "$selected_count" -eq 1 ] || {
    echo "ERROR: legacy hub bootstrap requires exactly one selected node" >&2
    return 1
  }
  spec="$(awk 'NF { print; exit }' "$selected_specs_file")"
  IFS='|' read -r -a fields <<<"$spec"
  agent="${fields[0]}"
  [ "$agent" = "$hub_agent" ] || {
    echo "ERROR: legacy hub bootstrap may deploy only the configured hub" >&2
    return 1
  }
  [ "$REQUIRE_RELEASE_ALL_SELECTED" = 0 ] \
    && [ -z "$HOLD_ADOPTIONS_FILE" ] \
    && [ -z "$SUCCESSOR_HOLD_REASON" ] || {
      echo "ERROR: legacy hub bootstrap rejects adoption, release-all, and successor-hold authority" >&2
      return 1
    }
  supervisor="${fields[14]:-auto}"
  fleet_name="${fields[23]:-mac}"
  os_kind="${fields[2]}"
  echo "==> ${agent}: executing explicit pre-held hub API bootstrap"
  prepare_remote_phase1_restore_contract \
    "$agent" "$(deployment_id_for_agent "$agent")" \
    "$supervisor" "$fleet_name" "$os_kind"
  MAC_DEPLOY_ALLOW_LEGACY_CAS_BOOTSTRAP=1 \
    prepare_remote_mac_agent_deployment \
      "$agent" "$(deployment_id_for_agent "$agent")" \
      "$supervisor" "$fleet_name" "" 0 "$os_kind"
  deploy_host "$spec" "$(read_hub_token)" \
    "$(read_hub_tunnel_pubkey 2>/dev/null || true)" 0 \
    "$(ensure_local_github_review_key)" 0 1
  authority_file="$TMPDIR_LOCAL/bootstrap-hub-authority.json"
  hub_epoch_client_read "$hub_agent" "$authority_file" authority
  "$PYTHON_BIN" - "$authority_file" <<'PY'
import json,sys,uuid
value=json.load(open(sys.argv[1], encoding="utf-8"))
if value.get("schema") != "mac.fleet_release_hub_authority.v1":
    raise SystemExit("typed hub authority endpoint was not installed")
uuid.UUID(str(value.get("hub_authority_id")))
PY
  finalize_remote_deployment_release "$agent" "$(deployment_id_for_agent "$agent")"
  echo "==> ${agent}: typed hub epoch API installed; dispatch hold intentionally retained"
}

hub_epoch_client_read() {
  # Execute the reviewed client from stdin on the pinned hub route. The bearer
  # is copied from mac.env into an unnamed owner-private temporary file and is
  # never placed in argv, the environment, stdout, or the controller journal.
  local hub_agent="$1" output="$2"
  shift 2
  local ssh_parts=() ssh_args=() ssh_target item last_index remote_args="" arg output_tmp
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  for arg in "$@"; do
    remote_args+=" $(shell_quote "$arg")"
  done
  output_tmp="$(mktemp "$TMPDIR_LOCAL/hub-read.XXXXXX")"; chmod 0600 "$output_tmp"
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" \
    "set -e; umask 077; _mac_token=\$(mktemp); _mac_output=\$(mktemp); trap 'rm -f \"\$_mac_token\" \"\$_mac_output\"' EXIT HUP INT TERM; set -a; . \"\$HOME/.mac/mac.env\"; set +a; [ -n \"\${MAC_API_TOKEN:-}\" ]; printf '%s' \"\$MAC_API_TOKEN\" > \"\$_mac_token\"; python3 - --token-file \"\$_mac_token\"${remote_args} --output \"\$_mac_output\" >/dev/null; cat \"\$_mac_output\"" \
    < "$HUB_EPOCH_CLIENT" > "$output_tmp"
  mv -f "$output_tmp" "$output"
  chmod 0600 "$output"
}

hub_epoch_client_request() {
  local hub_agent="$1" request_file="$2" output="$3"
  shift 3
  local request_nonce remote_client remote_request remote_output output_tmp
  local ssh_parts=() ssh_args=() ssh_target item last_index remote_args="" arg command
  request_nonce="$($PYTHON_BIN -c 'import secrets; print(secrets.token_hex(16))')"
  remote_client="/tmp/mac-fleet-epoch-client-${request_nonce}.py"
  remote_request="/tmp/mac-fleet-epoch-request-${request_nonce}.json"
  remote_output="/tmp/mac-fleet-epoch-receipt-${request_nonce}.json"
  pinned_remote_private_upload "$hub_agent" "$HUB_EPOCH_CLIENT" "$remote_client"
  pinned_remote_private_upload "$hub_agent" "$request_file" "$remote_request"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  for arg in "$@"; do
    remote_args+=" $(shell_quote "$arg")"
  done
  command="set -e; umask 077; _mac_token=\$(mktemp); trap 'rm -f \"\$_mac_token\" $(shell_quote "$remote_client") $(shell_quote "$remote_request") $(shell_quote "$remote_output")' EXIT HUP INT TERM; set -a; . \"\$HOME/.mac/mac.env\"; set +a; [ -n \"\${MAC_API_TOKEN:-}\" ]; printf '%s' \"\$MAC_API_TOKEN\" > \"\$_mac_token\"; python3 $(shell_quote "$remote_client") --token-file \"\$_mac_token\"${remote_args} --request-file $(shell_quote "$remote_request") --output $(shell_quote "$remote_output") >/dev/null; cat $(shell_quote "$remote_output")"
  output_tmp="$(mktemp "$TMPDIR_LOCAL/hub-request.XXXXXX")"; chmod 0600 "$output_tmp"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command" > "$output_tmp"
  mv -f "$output_tmp" "$output"
  chmod 0600 "$output"
}

hub_epoch_recovery_request_name() {
  local epoch_id="$1" phase="$2"
  "$PYTHON_BIN" - "$epoch_id" "$phase" <<'PY'
import hashlib,re,sys
phase=sys.argv[2]
if phase not in {"open","prove"}: raise SystemExit("invalid recovery request phase")
print("%s-%s.json"%(hashlib.sha256(sys.argv[1].encode()).hexdigest(),phase))
PY
}

persist_hub_epoch_recovery_request() {
  local hub_agent="$1" request_file="$2" phase="$3"
  local name remote_stage command persist_code
  local ssh_parts=() ssh_args=() ssh_target item last_index
  name="$(hub_epoch_recovery_request_name "$COHORT_EPOCH_ID" "$phase")"
  remote_stage="/tmp/mac-fleet-epoch-recovery-${DEPLOY_CONTROLLER_NONCE}-${phase}.json"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1)); ssh_target="${ssh_parts[$last_index]}"; ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "rm -f $(shell_quote "$remote_stage")"
  pinned_remote_private_upload "$hub_agent" "$request_file" "$remote_stage"
  persist_code='import os,stat,sys
from pathlib import Path
stage=Path(sys.argv[1]); name=sys.argv[2]; root=Path.home()/".mac"/"fleet-epoch-recovery"
try: root.mkdir(mode=0o700)
except FileExistsError: pass
parent=os.lstat(root)
if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode) or parent.st_uid!=os.getuid() or stat.S_IMODE(parent.st_mode)!=0o700: raise SystemExit("recovery relay directory is unsafe")
fd=os.open(stage,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
try:
 s=os.fstat(fd); raw=os.read(fd,s.st_size+1)
 if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_nlink!=1 or stat.S_IMODE(s.st_mode)!=0o600 or not 1<=s.st_size<=1024*1024 or len(raw)!=s.st_size: raise SystemExit("recovery relay request is unsafe")
finally: os.close(fd)
target=root/name
if target.exists() or target.is_symlink():
 current=os.open(target,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
 try:
  metadata=os.fstat(current); existing=os.read(current,metadata.st_size+1)
  if metadata.st_nlink!=1 or metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode)!=0o600 or existing!=raw: raise SystemExit("recovery relay request conflicts with durable bytes")
 finally: os.close(current)
 stage.unlink()
else: os.replace(stage,target)
directory=os.open(root,os.O_RDONLY)
try: os.fsync(directory)
finally: os.close(directory)'
  command="python3 -c $(shell_quote "$persist_code") $(shell_quote "$remote_stage") $(shell_quote "$name")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command"
}

remove_hub_epoch_recovery_request() {
  local hub_agent="$1" epoch_id="$2" phase="$3" name
  local ssh_parts=() ssh_args=() ssh_target item last_index
  name="$(hub_epoch_recovery_request_name "$epoch_id" "$phase")"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1)); ssh_target="${ssh_parts[$last_index]}"; ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" \
    "python3 -c $(shell_quote 'import os,stat,sys
from pathlib import Path
root=Path.home()/".mac"/"fleet-epoch-recovery"; name=sys.argv[1]
try: metadata=os.lstat(root)
except FileNotFoundError: raise SystemExit(0)
if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode)!=0o700: raise SystemExit("recovery relay directory is unsafe")
directory=os.open(root,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
try:
 try: os.unlink(name,dir_fd=directory)
 except FileNotFoundError: pass
 os.fsync(directory)
finally: os.close(directory)') $(shell_quote "$name")" >/dev/null
}

replay_hub_epoch_recovery_request() {
  local hub_agent="$1" epoch_id="$2" phase="$3" output="$4"
  local name remote_request local_request="$TMPDIR_LOCAL/recovered-${phase}-request.json"
  local ssh_parts=() ssh_args=() ssh_target item last_index
  name="$(hub_epoch_recovery_request_name "$epoch_id" "$phase")"
  remote_request="\$HOME/.mac/fleet-epoch-recovery/$name"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1)); ssh_target="${ssh_parts[$last_index]}"; ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" \
    "python3 -c $(shell_quote 'import os,stat,sys
from pathlib import Path
path=Path.home()/".mac"/"fleet-epoch-recovery"/sys.argv[1]
fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
try:
 before=os.fstat(fd)
 if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1 or stat.S_IMODE(before.st_mode)!=0o600 or not 1<=before.st_size<=1024*1024: raise SystemExit("recovery relay request is unsafe")
 raw=os.read(fd,before.st_size+1); after=os.fstat(fd)
 if len(raw)!=before.st_size or (before.st_dev,before.st_ino,before.st_nlink,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_nlink,after.st_size,after.st_mtime_ns,after.st_ctime_ns): raise SystemExit("recovery relay request changed while reading")
 os.write(1,raw)
finally: os.close(fd)') $(shell_quote "$name")" > "$local_request"
  chmod 0600 "$local_request"
  hub_epoch_client_request "$hub_agent" "$local_request" "$output" \
    "$phase" --epoch "$epoch_id"
}

write_hub_identity_request() {
  local output="$1" identity="$2" kind="$3" reason="${4:-}"
  "$PYTHON_BIN" - "$output" "$identity" "$kind" "$reason" <<'PY'
import json,os,re,sys,tempfile
from pathlib import Path
output=Path(sys.argv[1]); identity=sys.argv[2]; kind=sys.argv[3]; reason=sys.argv[4]
if re.fullmatch(r"sha256:[0-9a-f]{64}",identity) is None: raise SystemExit("hub identity digest is invalid")
payload={"identity_sha256":identity}
if kind=="abort":
    if not reason.strip(): raise SystemExit("hub abort reason is required")
    payload["reason"]=reason.strip()
elif kind!="commit": raise SystemExit("hub request kind is invalid")
fd,raw=tempfile.mkstemp(prefix=output.name+".",dir=output.parent); tmp=Path(raw)
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        json.dump(payload,stream,sort_keys=True,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp,output)
finally: tmp.unlink(missing_ok=True)
PY
}

read_hub_epoch_status_exact() {
  local hub_agent="$1" epoch_id="$2" identity="$3" output="$4"
  hub_epoch_client_read "$hub_agent" "$output" status \
    --epoch "$epoch_id" --identity-sha256 "$identity"
}

abort_hub_epoch_exact() {
  local hub_agent="$1" epoch_id="$2" identity="$3" output="$4"
  local request="$TMPDIR_LOCAL/hub-abort-request.json"
  write_hub_identity_request "$request" "$identity" abort \
    "controller recovery rolled back an incomplete synchronized cohort"
  hub_epoch_client_request "$hub_agent" "$request" "$output" \
    abort --epoch "$epoch_id"
}

commit_hub_epoch_exact() {
  local hub_agent="$1" epoch_id="$2" identity="$3" output="$4"
  local request="$TMPDIR_LOCAL/hub-recovered-commit-request.json"
  write_hub_identity_request "$request" "$identity" commit
  hub_epoch_client_request "$hub_agent" "$request" "$output" \
    commit --epoch "$epoch_id"
}

live_ssh_host_key_fingerprint() {
  local agent="$1" log_path line
  log_path="$(ssh_control_path_for_agent "$agent").log"
  [ -f "$log_path" ] || {
    echo "ERROR: ${agent}: SSH control-master transcript is missing" >&2
    return 1
  }
  line="$(sed -n 's/^debug1: Server host key: [^ ]* \(SHA256:[A-Za-z0-9+\/=]*\)$/\1/p' "$log_path" | tail -1)"
  [ -n "$line" ] || {
    echo "ERROR: ${agent}: actual negotiated SSH host-key fingerprint is unavailable" >&2
    return 1
  }
  printf '%s\n' "$line"
}

live_machine_instance_id() {
  local agent="$1" ssh_parts=() ssh_args=() ssh_target item last_index
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" 'set -e; if [ -r /etc/machine-id ]; then printf "machine-id|"; tr -d "\r\n" < /etc/machine-id; elif command -v ioreg >/dev/null 2>&1; then printf "ioplatformuuid|"; ioreg -rd1 -c IOPlatformExpertDevice | awk -F\" '\''/IOPlatformUUID/{print tolower($(NF-1)); exit}'\''; else exit 3; fi'
}

write_live_endpoint_identity() {
  local agent="$1" output="$2" durable_store_uuid="${3:-}"
  local fingerprint instance_record instance_kind instance_id args=()
  fingerprint="$(live_ssh_host_key_fingerprint "$agent")"
  instance_record="$(live_machine_instance_id "$agent")"
  instance_kind="${instance_record%%|*}"
  instance_id="${instance_record#*|}"
  [ -n "$instance_id" ] && [ "$instance_id" != "$instance_record" ] || {
    echo "ERROR: ${agent}: durable machine instance identity is unavailable" >&2
    return 1
  }
  args=(build-ssh --host-key-fingerprint "$fingerprint" \
    --instance-id-kind "$instance_kind" --instance-id "$instance_id")
  if [ -n "$durable_store_uuid" ]; then
    args+=(--durable-store-uuid "$durable_store_uuid")
  fi
  "$PYTHON_BIN" "$ENDPOINT_IDENTITY_HELPER" "${args[@]}" --output "$output" >/dev/null
  chmod 0600 "$output"
}

bind_live_cohort_routes() {
  local selected_specs_file="$1" hub_agent="$2"
  local authority_file="$TMPDIR_LOCAL/hub-authority.json"
  local hub_identity="$TMPDIR_LOCAL/hub-route-identity.json"
  local authority_id spec fields=() agent agent_id generation node_identity
  hub_epoch_client_read "$hub_agent" "$authority_file" authority
  authority_id="$("$PYTHON_BIN" - "$authority_file" <<'PY'
import json,sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
if value.get("schema") != "mac.fleet_release_hub_authority.v1":
    raise SystemExit("hub authority response is invalid")
print(value["hub_authority_id"])
PY
)"
  write_live_endpoint_identity "$hub_agent" "$hub_identity" "$authority_id"
  cohort_journal_mutate hub-route-bound "$COHORT_EPOCH_ID" \
    "$COHORT_JOURNAL_REVISION" hub-route "$DEPLOY_CONTROLLER_NONCE" \
    --identity-file "$hub_identity" >/dev/null
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a fields <<<"$spec"
    agent="${fields[0]}"
    agent_id="$(stable_worker_agent_id "$agent")"
    generation="$(worker_generation_for_agent "$agent")"
    node_identity="$TMPDIR_LOCAL/node-route-identity-${agent_id}.json"
    write_live_endpoint_identity "$agent" "$node_identity"
    cohort_journal_mutate route-bound "$COHORT_EPOCH_ID" \
      "$COHORT_JOURNAL_REVISION" "route-${agent_id}" "$DEPLOY_CONTROLLER_NONCE" \
      --agent-name "$agent" --stable-id "$agent_id" \
      --generation "$generation" --identity-file "$node_identity" >/dev/null
  done < "$selected_specs_file"
}

node_route_identity_file() {
  printf '%s/node-route-identity-%s.json\n' \
    "$TMPDIR_LOCAL" "$(stable_worker_agent_id "$1")"
}

node_route_identity_sha256() {
  sha256_file "$(node_route_identity_file "$1")"
}

node_prerequisite_bundle_file() {
  printf '%s/prerequisite-bundle-%s.json\n' \
    "$TMPDIR_LOCAL" "$(stable_worker_agent_id "$1")"
}

node_prerequisite_expectations_file() {
  printf '%s/prerequisite-expectations-%s.json\n' \
    "$TMPDIR_LOCAL" "$(stable_worker_agent_id "$1")"
}

node_finalizer_capability_name_for_generation() {
  "$PYTHON_BIN" -c \
    'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()+".py")' \
    "$1"
}

node_finalizer_capability_name() {
  node_finalizer_capability_name_for_generation "$(deployment_id_for_agent "$1")"
}

install_remote_node_finalizer() {
  local agent="$1" deployment_id stage name expected command
  local ssh_parts=() ssh_args=() ssh_target item last_index
  deployment_id="$(deployment_id_for_agent "$agent")"
  name="$(node_finalizer_capability_name "$agent")"
  expected="$(sha256_file "$NODE_FINALIZER_HELPER")"
  stage="/tmp/mac-fleet-node-finalizer-${DEPLOY_CONTROLLER_NONCE}-${name}"
  fenced_remote_upload "$agent" "$deployment_id" "$NODE_FINALIZER_HELPER" "$stage"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1)); ssh_target="${ssh_parts[$last_index]}"; ssh_args=("${ssh_parts[@]:0:$last_index}")
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c \
    "set -e; umask 077; mkdir -p \"\$HOME/.mac/fleet-finalizers\"; chmod 0700 \"\$HOME/.mac/fleet-finalizers\"; python3 - $(shell_quote "$stage") \"\$HOME/.mac/fleet-finalizers/$name\" $(shell_quote "$expected")")"
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

source, target, expected = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1 or not 1 <= metadata.st_size <= 1024 * 1024:
        raise SystemExit("staged finalizer is unsafe")
    raw = bytearray()
    while len(raw) < metadata.st_size:
        chunk = os.read(descriptor, min(65536, metadata.st_size - len(raw)))
        if not chunk: raise SystemExit("staged finalizer was truncated")
        raw.extend(chunk)
    after = os.fstat(descriptor)
    if (metadata.st_dev, metadata.st_ino, metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise SystemExit("staged finalizer changed while reading")
    if hashlib.sha256(raw).hexdigest() != expected:
        raise SystemExit("staged finalizer digest differs")
finally:
    os.close(descriptor)
os.chmod(source, 0o700)
os.replace(source, target)
directory = os.open(target.parent, os.O_RDONLY)
try: os.fsync(directory)
finally: os.close(directory)
PY
  echo "==> ${agent}: immutable node finalizer capability installed"
}

run_remote_node_finalizer() {
  local agent="$1" fleet_name="$2" generation="$3" revision="$4" deploy_ts="$5" expected_sha="$6" output="$7"
  local deployment_id="${8:-$(deployment_id_for_agent "$agent")}" name code
  name="$(node_finalizer_capability_name_for_generation "$deployment_id")"
  code="$(command cat <<'PY'
import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

name, expected, agent, fleet, generation, revision, deploy_ts = sys.argv[1:]
path = Path.home() / ".mac" / "fleet-finalizers" / name
descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or not 1 <= metadata.st_size <= 1024 * 1024
    ):
        raise SystemExit("installed finalizer capability is unsafe")
    raw = bytearray()
    while len(raw) < metadata.st_size:
        chunk = os.read(descriptor, min(65536, metadata.st_size - len(raw)))
        if not chunk: raise SystemExit("installed finalizer was truncated")
        raw.extend(chunk)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise SystemExit("installed finalizer differs from the journal")
    os.lseek(descriptor, 0, os.SEEK_SET)
    result = subprocess.run(
        [sys.executable, "/dev/fd/%d" % descriptor,
         "--mac-home", str(Path.home() / ".mac"),
         "--agent", agent, "--fleet", fleet,
         "--generation", generation, "--revision", revision,
         "--deploy-ts", deploy_ts],
        pass_fds=(descriptor,), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=None, check=False,
    )
    if result.returncode != 0: raise SystemExit(result.returncode)
    os.write(1, result.stdout)
finally:
    os.close(descriptor)
PY
)"
  run_fenced_remote_python "$agent" "$deployment_id" "$code" \
    "$name" "$expected_sha" "$agent" "$fleet_name" "$generation" \
    "$revision" "$deploy_ts" > "$output"
  chmod 0600 "$output"
}

prepare_remote_prerequisite_bundle() {
  local spec="$1" fields=() agent supervisor os_kind qdrant_url qdrant_required
  local firecrawl_url firecrawl_required webdav_url webdav_enabled openshell_required
  local deployment_id agent_id identity_sha remote_helper remote_root command
  local bundle expectations ssh_parts=() ssh_args=() ssh_target item last_index
  IFS='|' read -r -a fields <<<"$spec"
  agent="${fields[0]}"
  os_kind="${fields[2]}"
  qdrant_url="${fields[16]:-}"
  qdrant_required="${fields[18]:-0}"
  firecrawl_url="${fields[26]:-}"
  firecrawl_required="${fields[28]:-0}"
  webdav_enabled="${fields[46]:-0}"
  webdav_url="${fields[48]:-}"
  supervisor="${fields[14]:-auto}"
  openshell_required="${fields[53]:-0}"
  deployment_id="$(deployment_id_for_agent "$agent")"
  agent_id="$(stable_worker_agent_id "$agent")"
  identity_sha="$(node_route_identity_sha256 "$agent")"
  remote_helper="/tmp/mac-prerequisite-builder-${agent}-${DEPLOY_CONTROLLER_NONCE}.py"
  remote_root="/tmp/mac-prerequisites-${agent}-${DEPLOY_CONTROLLER_NONCE}"
  bundle="$(node_prerequisite_bundle_file "$agent")"
  expectations="$(node_prerequisite_expectations_file "$agent")"
  fenced_remote_upload "$agent" "$deployment_id" \
    "$PREREQUISITE_RECEIPT_HELPER" "$remote_helper"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c \
    "set -e; umask 077; rm -rf $(shell_quote "$remote_root"); mkdir -m 0700 $(shell_quote "$remote_root"); MAC_PREREQ_AGENT=$(shell_quote "$agent") MAC_PREREQ_AGENT_ID=$(shell_quote "$agent_id") MAC_PREREQ_IDENTITY=$(shell_quote "$identity_sha") MAC_PREREQ_HELPER=$(shell_quote "$remote_helper") MAC_PREREQ_ROOT=$(shell_quote "$remote_root") MAC_PREREQ_QDRANT_URL=$(shell_quote "$qdrant_url") MAC_PREREQ_QDRANT_REQUIRED=$(shell_quote "$qdrant_required") MAC_PREREQ_FIRECRAWL_URL=$(shell_quote "$firecrawl_url") MAC_PREREQ_FIRECRAWL_REQUIRED=$(shell_quote "$firecrawl_required") MAC_PREREQ_WEBDAV_URL=$(shell_quote "$webdav_url") MAC_PREREQ_WEBDAV_ENABLED=$(shell_quote "$webdav_enabled") MAC_PREREQ_OPENSHELL_REQUIRED=$(shell_quote "$openshell_required") MAC_PREREQ_SUPERVISOR=$(shell_quote "$supervisor") MAC_PREREQ_OS=$(shell_quote "$os_kind") python3 -")"
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command" <<'PY'
import hashlib
import json
import os
import stat
import subprocess
import sys
import urllib.parse
from pathlib import Path

root = Path(os.environ["MAC_PREREQ_ROOT"])
helper = Path(os.environ["MAC_PREREQ_HELPER"])
agent = os.environ["MAC_PREREQ_AGENT"]
agent_id = os.environ["MAC_PREREQ_AGENT_ID"]
identity = os.environ["MAC_PREREQ_IDENTITY"]


def truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def path_check(name, raw, *, executable=False):
    path = Path(raw).expanduser().resolve(strict=True)
    metadata = path.stat()
    if path.is_dir():
        return {
            "name": name,
            "kind": "path",
            "path": str(path),
            "file_type": "directory",
            "expected_mode": stat.S_IMODE(metadata.st_mode),
            "sha256": None,
        }
    return {
        "name": name,
        "kind": "path",
        "path": str(path),
        "file_type": "executable" if executable else "file",
        "expected_mode": stat.S_IMODE(metadata.st_mode),
        "sha256": digest(path),
    }


def service_check(name, url, required, fallback):
    parsed = urllib.parse.urlsplit(url or "")
    if truthy(required):
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise SystemExit("required %s prerequisite is not an unauthenticated loopback service" % name)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return {
            "name": name,
            "kind": "tcp",
            "host": parsed.hostname,
            "port": port,
            "timeout_seconds": 3,
        }
    return path_check(name + "-config", fallback)


home = Path.home()
mac_home = home / ".mac"
mac_env = mac_home / "mac.env"
mac_bin = (home / ".local" / "bin" / "mac").resolve(strict=True)
python_bin = (mac_home / "venv" / "bin" / "python").resolve(strict=True)
hermes_root = (mac_home / "src" / "mac" / "src" / "mac" / "_hermes").resolve(strict=True)
requested_supervisor = os.environ["MAC_PREREQ_SUPERVISOR"]
os_kind = os.environ["MAC_PREREQ_OS"]
systemctl = next((path for path in (Path("/bin/systemctl"), Path("/usr/bin/systemctl")) if path.exists() and os.access(path, os.X_OK)), None)
launchctl = Path("/bin/launchctl") if Path("/bin/launchctl").exists() and os.access("/bin/launchctl", os.X_OK) else None
supervisorctl = next((path for path in (mac_home / "supervisord" / "venv" / "bin" / "supervisorctl", Path("/usr/bin/supervisorctl"), Path("/usr/local/bin/supervisorctl")) if path.exists() and os.access(path, os.X_OK)), None)
if requested_supervisor in {"", "auto"}:
    if os_kind == "darwin" and launchctl is not None:
        supervisor, supervisor_bin = "launchd", launchctl
    elif os_kind == "linux" and systemctl is not None and Path("/run/systemd/system").is_dir():
        supervisor, supervisor_bin = "systemd", systemctl
    elif supervisorctl is not None:
        supervisor, supervisor_bin = "supervisord", supervisorctl
    else:
        raise SystemExit("no supported active supervisor is available")
elif requested_supervisor == "systemd" and os_kind == "linux" and systemctl is not None and Path("/run/systemd/system").is_dir():
    supervisor, supervisor_bin = "systemd", systemctl
elif requested_supervisor == "launchd" and os_kind == "darwin" and launchctl is not None:
    supervisor, supervisor_bin = "launchd", launchctl
elif requested_supervisor == "supervisord" and supervisorctl is not None:
    supervisor, supervisor_bin = "supervisord", supervisorctl
else:
    raise SystemExit("requested supervisor is unavailable on the declared node OS")

openshell_path = mac_env
if truthy(os.environ["MAC_PREREQ_OPENSHELL_REQUIRED"]):
    openshell_path = mac_home / "openshell" / "runtime-image-attestation.json"

checks = {
    "machine-onboarding": [path_check("mac-cli", mac_bin, executable=True)],
    "route-tunnel": [path_check("route-config", mac_env)],
    "openshell": [path_check("openshell-contract", openshell_path)],
    "qdrant": [service_check("qdrant", os.environ["MAC_PREREQ_QDRANT_URL"], os.environ["MAC_PREREQ_QDRANT_REQUIRED"], mac_env)],
    "firecrawl": [service_check("firecrawl", os.environ["MAC_PREREQ_FIRECRAWL_URL"], os.environ["MAC_PREREQ_FIRECRAWL_REQUIRED"], mac_env)],
    "webdav": [service_check("webdav", os.environ["MAC_PREREQ_WEBDAV_URL"], os.environ["MAC_PREREQ_WEBDAV_ENABLED"], mac_env)],
    "hermes": [path_check("hermes-runtime", hermes_root), path_check("python-runtime", python_bin, executable=True)],
    "service-topology": [path_check("supervisor", supervisor_bin, executable=True)],
}

receipts = []
contracts = {}
for participant in (
    "machine-onboarding",
    "route-tunnel",
    "openshell",
    "qdrant",
    "firecrawl",
    "webdav",
    "hermes",
    "service-topology",
):
    contract = {
        "schema": "mac.fleet_prerequisite_contract.v1",
        "participant": participant,
        "agent_id": agent,
        "node_identity_sha256": identity,
        "checks": checks[participant],
    }
    contract_path = root / (participant + "-contract.json")
    receipt_path = root / (participant + "-receipt.json")
    contract_path.write_text(json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    contract_path.chmod(0o600)
    subprocess.run([sys.executable, str(helper), "verify", "--contract", str(contract_path), "--output", str(receipt_path)], check=True, stdout=subprocess.DEVNULL)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipts.append(receipt_path)
    contracts[participant] = receipt["contract_sha256"]

expectations = {
    "schema": "mac.fleet_prerequisite_expectations.v1",
    "agent_id": agent,
    "node_identity_sha256": identity,
    "contracts": contracts,
}
expectations_path = root / "expectations.json"
expectations_path.write_text(json.dumps(expectations, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
expectations_path.chmod(0o600)
command = [sys.executable, str(helper), "bundle"]
for receipt in receipts:
    command.extend(["--receipt", str(receipt)])
command.extend(["--agent-id", agent, "--node-identity-sha256", identity, "--output", str(root / "bundle.json")])
subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
PY
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c \
    "cat $(shell_quote "$remote_root/bundle.json")")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command" > "$bundle"
  chmod 0600 "$bundle"
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c \
    "cat $(shell_quote "$remote_root/expectations.json"); rm -rf $(shell_quote "$remote_root") $(shell_quote "$remote_helper")")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command" > "$expectations"
  chmod 0600 "$expectations"
  "$PYTHON_BIN" "$PREREQUISITE_RECEIPT_HELPER" validate-bundle \
    --bundle "$bundle" --expectations "$expectations" \
    --agent-id "$agent" --node-identity-sha256 "$identity_sha" \
    --max-age-seconds 3600 >/dev/null
  echo "==> ${agent}: eight exact prerequisite participants proved"
}

collect_phase2_arm_evidence() {
  local agent="$1" deployment_id agent_id output pre intent finalizer_sha finalizer_name command
  local ssh_parts=() ssh_args=() ssh_target item last_index
  deployment_id="$(deployment_id_for_agent "$agent")"
  agent_id="$(stable_worker_agent_id "$agent")"
  output="$TMPDIR_LOCAL/phase2-arm-${agent_id}.json"
  pre="$TMPDIR_LOCAL/phase2-arm-${agent_id}-pre.json"
  intent="$TMPDIR_LOCAL/phase2-arm-${agent_id}-intent.json"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c \
    "cat \"\$HOME/.mac/logs/deploy-manifest-${TS}-pre.json\"")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command" > "$pre"
  chmod 0600 "$pre"
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c \
    "cat \"\$HOME/.mac/logs/rollback-${TS}-intent.json\"")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command" > "$intent"
  chmod 0600 "$intent"
  finalizer_name="$(node_finalizer_capability_name "$agent")"
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 python3 -c \
    'import hashlib,os,stat,sys; p=os.path.expanduser(sys.argv[1]); fd=os.open(p,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); s=os.fstat(fd); raw=os.read(fd,s.st_size+1); os.close(fd); (stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and s.st_nlink==1 and stat.S_IMODE(s.st_mode)==0o700 and len(raw)==s.st_size and 1<=s.st_size<=1024*1024) or sys.exit("unsafe finalizer capability"); print(hashlib.sha256(raw).hexdigest())' \
    "~/.mac/fleet-finalizers/$finalizer_name")"
  finalizer_sha="$(ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command")"
  [ "$finalizer_sha" = "$(sha256_file "$NODE_FINALIZER_HELPER")" ] || {
    echo "ERROR: ${agent}: installed finalizer differs from reviewed helper" >&2
    return 1
  }
  "$PYTHON_BIN" - "$pre" "$intent" "$output" "$agent" "$deployment_id" "$finalizer_sha" <<'PY'
import hashlib,json,os,sys,tempfile
from pathlib import Path
pre_path,intent_path,output_path,agent,generation,finalizer_sha=sys.argv[1:]
pre_raw=Path(pre_path).read_bytes()
intent_raw=Path(intent_path).read_bytes()
pre=json.loads(pre_raw)
intent=json.loads(intent_raw)
deploy=pre.get("deploy") if isinstance(pre,dict) else None
if pre.get("stage") != "pre" or pre.get("agent") != agent or not isinstance(deploy,dict) or deploy.get("generation") != generation:
    raise SystemExit("phase-2 pre manifest differs from the selected generation")
if intent.get("schema") != "mac.fleet_node_rollback_intent.v1" or intent.get("agent") != agent or intent.get("generation") != generation:
    raise SystemExit("phase-2 rollback intent differs from the selected generation")
payload={
    "schema":"mac.fleet_phase2_arm_ready.v1",
    "agent":agent,
    "generation":generation,
    "pre_manifest_sha256":hashlib.sha256(pre_raw).hexdigest(),
    "rollback_intent_sha256":hashlib.sha256(intent_raw).hexdigest(),
    "finalizer_sha256":finalizer_sha,
}
output=Path(output_path)
fd,raw=tempfile.mkstemp(prefix=output.name+".",dir=output.parent)
tmp=Path(raw)
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        json.dump(payload,stream,sort_keys=True,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp,output)
finally:
    tmp.unlink(missing_ok=True)
print(payload["rollback_intent_sha256"])
print(payload["finalizer_sha256"])
PY
}

acquire_remote_deployment_lock() {
  local agent="$1" deployment_id="$2" ssh_parts=() ssh_args=() ssh_target last_index item
  local takeover="${3:-}" takeover_request
  if [ -z "$takeover" ]; then
    takeover_request="${MAC_DEPLOY_TAKEOVER_STALE_LOCK:-0}"
    case "$(printf '%s' "$takeover_request" | tr '[:upper:]' '[:lower:]')" in
      1|true|yes|on) takeover=1 ;;
      *) takeover=0 ;;
    esac
  fi
  case "$takeover" in
    0|1) ;;
    *) echo "invalid deployment-lock takeover policy" >&2; return 2 ;;
  esac
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_LOCK_ID=$(shell_quote "$deployment_id") MAC_DEPLOY_LOCK_TAKEOVER=$(shell_quote "$takeover") MAC_DEPLOY_LOCK_STALE_SECONDS=$(shell_quote "${MAC_DEPLOY_STALE_LOCK_SECONDS:-3600}") python3 - <<'PY'
import json
import os
import shutil
import tempfile
import time
import fcntl
from pathlib import Path

deployment_id = os.environ['MAC_DEPLOY_LOCK_ID']
allow_takeover = os.environ['MAC_DEPLOY_LOCK_TAKEOVER'] == '1'
stale_seconds = max(60.0, float(os.environ['MAC_DEPLOY_LOCK_STALE_SECONDS']))
root = Path.home() / '.mac'
root.mkdir(parents=True, exist_ok=True)
lock = root / 'deploy-controller.lock'
owner_path = lock / 'owner.json'
guard_path = root / 'deploy-controller.guard'

guard_fd = os.open(str(guard_path), os.O_CREAT | os.O_RDWR, 0o600)
os.fchmod(guard_fd, 0o600)
fcntl.flock(guard_fd, fcntl.LOCK_EX)

def owner():
    try:
        value = json.loads(owner_path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        value = {}
    return value if isinstance(value, dict) else {}

try:
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        current = owner()
        if current.get('deployment_id') == deployment_id:
            raise SystemExit(0)
        renewed = current.get('renewed_at_epoch') or current.get('created_at_epoch')
        try:
            renewed_epoch = float(renewed)
        except (TypeError, ValueError):
            renewed_epoch = lock.stat().st_mtime
        age = max(0.0, time.time() - renewed_epoch)
        if not allow_takeover or age < stale_seconds:
            raise SystemExit(
                'another deployment owns this node: %s (age %.0fs); explicit stale takeover required'
                % (current.get('deployment_id') or 'unknown', age)
            )
        # The stable fcntl guard covers owner read, stale decision, rename, and
        # replacement. A concurrent renewer cannot refresh between stat/read
        # and rename, and cannot resolve a replacement lock directory mid-write.
        stale = root / (
            'deploy-controller.lock.stale.%d.%d'
            % (time.time_ns(), os.getpid())
        )
        os.replace(lock, stale)
        lock.mkdir(mode=0o700)
        shutil.rmtree(stale)

    now = time.time()
    payload = {
        'schema': 'mac.deploy_controller_lock.v1',
        'deployment_id': deployment_id,
        'created_at_epoch': now,
        'renewed_at_epoch': now,
    }
    fd, raw = tempfile.mkstemp(prefix='owner.json.', dir=str(lock))
    tmp = Path(raw)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(payload, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, owner_path)
        directory_fd = os.open(lock, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        tmp.unlink(missing_ok=True)
finally:
    fcntl.flock(guard_fd, fcntl.LOCK_UN)
    os.close(guard_fd)
PY"
}

assert_remote_deployment_lock() {
  local agent="$1" deployment_id="$2" ssh_parts=() ssh_args=() ssh_target last_index item
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_LOCK_ID=$(shell_quote "$deployment_id") python3 - <<'PY'
import json
import os
import tempfile
import time
import fcntl
from pathlib import Path

root = Path.home() / '.mac'
path = root / 'deploy-controller.lock' / 'owner.json'
guard_path = root / 'deploy-controller.guard'
guard_fd = os.open(str(guard_path), os.O_CREAT | os.O_RDWR, 0o600)
os.fchmod(guard_fd, 0o600)
fcntl.flock(guard_fd, fcntl.LOCK_EX)
try:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit('deployment lock is unreadable: %s' % type(exc).__name__)
    if payload.get('deployment_id') != os.environ['MAC_DEPLOY_LOCK_ID']:
        raise SystemExit('deployment lock fence does not match this controller')
    payload['renewed_at_epoch'] = time.time()
    fd, raw = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    tmp = Path(raw)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(payload, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        tmp.unlink(missing_ok=True)
finally:
    fcntl.flock(guard_fd, fcntl.LOCK_UN)
    os.close(guard_fd)
PY"
}

release_remote_deployment_lock() {
  local agent="$1" deployment_id="$2" ssh_parts=() ssh_args=() ssh_target last_index item
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_LOCK_ID=$(shell_quote "$deployment_id") python3 - <<'PY'
import json
import os
import fcntl
from pathlib import Path

root = Path.home() / '.mac'
lock = root / 'deploy-controller.lock'
owner = lock / 'owner.json'
guard_path = root / 'deploy-controller.guard'
guard_fd = os.open(str(guard_path), os.O_CREAT | os.O_RDWR, 0o600)
os.fchmod(guard_fd, 0o600)
fcntl.flock(guard_fd, fcntl.LOCK_EX)
try:
    payload = json.loads(owner.read_text(encoding='utf-8'))
    if payload.get('deployment_id') != os.environ['MAC_DEPLOY_LOCK_ID']:
        raise SystemExit('refusing to release another deployment controller lock')
    owner.unlink()
    lock.rmdir()
finally:
    fcntl.flock(guard_fd, fcntl.LOCK_UN)
    os.close(guard_fd)
PY"
}

write_remote_deployment_hold_state() {
  local agent="$1" deployment_id="$2" hold_reason="$3" owns_hold="$4"
  local agent_existed="${5:-1}"
  local adopted_from_reason="${6:-}"
  local require_owned_after_prepare="${7:-0}"
  local ssh_parts=() ssh_args=() ssh_target last_index item fence_exec
  fence_exec="$(remote_deployment_fenced_exec "$deployment_id" 0 python3 -)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_STATE_ID=$(shell_quote "$deployment_id") MAC_DEPLOY_STATE_REASON=$(shell_quote "$hold_reason") MAC_DEPLOY_STATE_OWNS=$(shell_quote "$owns_hold") MAC_DEPLOY_STATE_AGENT_EXISTED=$(shell_quote "$agent_existed") MAC_DEPLOY_STATE_ADOPTED_FROM=$(shell_quote "$adopted_from_reason") MAC_DEPLOY_STATE_REQUIRE_OWNED=$(shell_quote "$require_owned_after_prepare") $fence_exec <<'PY'
import json
import os
import tempfile
from pathlib import Path

directory = Path.home() / '.mac'
directory.mkdir(parents=True, exist_ok=True)
path = directory / 'deploy-dispatch-hold.json'
fd, raw = tempfile.mkstemp(prefix=path.name + '.', dir=str(directory))
tmp = Path(raw)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        payload = {
            'schema': 'mac.deploy_dispatch_hold.v1',
            'deployment_id': os.environ['MAC_DEPLOY_STATE_ID'],
            'hold_reason': os.environ['MAC_DEPLOY_STATE_REASON'],
            'owns_hold': os.environ['MAC_DEPLOY_STATE_OWNS'] == '1',
            'agent_existed': os.environ['MAC_DEPLOY_STATE_AGENT_EXISTED'] == '1',
            'require_owned_after_prepare': os.environ['MAC_DEPLOY_STATE_REQUIRE_OWNED'] == '1',
        }
        if os.environ.get('MAC_DEPLOY_STATE_ADOPTED_FROM'):
            payload['adopted_from_reason'] = os.environ['MAC_DEPLOY_STATE_ADOPTED_FROM']
        json.dump(payload, stream, sort_keys=True)
        stream.write('\\n')
    tmp.chmod(0o600)
    os.replace(tmp, path)
finally:
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
PY"
}

set_remote_mac_startup_hold_policy() {
  local agent="$1" value="$2" ssh_parts=() ssh_args=() ssh_target last_index item
  local deployment_id fence_exec
  deployment_id="$(deployment_id_for_agent "$agent")"
  fence_exec="$(remote_deployment_fenced_exec "$deployment_id" 0 python3 -)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_STARTUP_CLEAR_HOLD=$(shell_quote "$value") $fence_exec <<'PY'
import os
import tempfile
from pathlib import Path

path = Path.home() / '.mac' / 'mac.env'
if path.exists():
    # Avoid importing the not-yet-deployed mac package: preserve all existing
    # assignments and replace/append only this simple numeric policy value.
    lines = path.read_text(encoding='utf-8').splitlines()
    key = 'MAC_STARTUP_CLEAR_HOLD'
    replacement = key + '=' + os.environ['MAC_DEPLOY_STARTUP_CLEAR_HOLD']
    updated = []
    replaced = False
    for line in lines:
        if line.startswith(key + '=') or line.startswith('export ' + key + '='):
            if not replaced:
                updated.append(replacement)
                replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(replacement)
    fd, raw = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    tmp = Path(raw)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write('\\n'.join(updated) + '\\n')
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
PY"
}

phase1_restore_contract_file_for_agent() {
  printf '%s/phase1-restore-contract-%s.json' \
    "$TMPDIR_LOCAL" "$(stable_worker_agent_id "$1")"
}

phase1_restore_contract_digest_for_agent() {
  "$PYTHON_BIN" - "$(phase1_restore_contract_file_for_agent "$1")" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
value = payload.get("contract_sha256")
if not isinstance(value, str) or len(value) != 64:
    raise SystemExit("phase-1 restore contract digest is unavailable")
print(value)
PY
}

prepare_remote_phase1_restore_contract() {
  local agent="$1" deployment_id="$2" supervisor="$3" fleet_name="$4" os_kind="$5"
  local remote_helper="/tmp/mac-phase1-prepare-${agent}-${DEPLOY_CONTROLLER_NONCE}.sh"
  local remote_functions="/tmp/mac-phase1-prepare-functions-${agent}-${DEPLOY_CONTROLLER_NONCE}.sh"
  local ssh_parts=() ssh_args=() ssh_target item last_index fence_exec
  local agent_id local_contract contract_raw
  agent_id="$(stable_worker_agent_id "$agent")"
  local_contract="$(phase1_restore_contract_file_for_agent "$agent")"
  contract_raw="$TMPDIR_LOCAL/phase1-restore-contract-raw-${agent_id}.json"
  acquire_remote_deployment_lock "$agent" "$deployment_id"
  fenced_remote_upload "$agent" "$deployment_id" "$PHASE1_QUIESCE_HELPER" "$remote_helper"
  fenced_remote_upload "$agent" "$deployment_id" "$PHASE1_DAEMON_FUNCTIONS" "$remote_functions"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  fence_exec="$(remote_deployment_fenced_exec "$deployment_id" 0 bash -s)"
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
    "${ssh_args[@]}" "$ssh_target" \
    "MAC_PHASE1_AGENT=$(shell_quote "$agent") MAC_PHASE1_FLEET=$(shell_quote "$fleet_name") MAC_PHASE1_OS=$(shell_quote "$os_kind") MAC_PHASE1_REV=$(shell_quote "$GIT_REV") MAC_PHASE1_GENERATION=$(shell_quote "$deployment_id") MAC_PHASE1_SUPERVISOR=$(shell_quote "$supervisor") MAC_PHASE1_HELPER=$(shell_quote "$remote_helper") MAC_PHASE1_FUNCTIONS=$(shell_quote "$remote_functions") $fence_exec" > "$contract_raw" <<'REMOTE_PHASE1_PREPARE'
set -euo pipefail
helper="${MAC_PHASE1_HELPER:?}"
functions="${MAC_PHASE1_FUNCTIONS:?}"
cleanup() { rm -f "$helper" "$functions"; }
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
phase1_python=""
for candidate in \
  "$HOME/.mac/venv/bin/python" \
  python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if [ -x "$candidate" ]; then
    phase1_python="$candidate"
    break
  fi
  if command -v "$candidate" >/dev/null 2>&1; then
    phase1_python="$(command -v "$candidate")"
    break
  fi
done
[ -n "$phase1_python" ] || {
  echo "phase-1 prepare requires Python 3.9 or newer" >&2
  exit 1
}
AGENT="${MAC_PHASE1_AGENT:?}" \
FLEET_NAME="${MAC_PHASE1_FLEET:?}" \
OS_KIND="${MAC_PHASE1_OS:?}" \
DEPLOY_REV="${MAC_PHASE1_REV:?}" \
DEPLOY_GENERATION="${MAC_PHASE1_GENERATION:?}" \
SUPERVISOR_KIND="${MAC_PHASE1_SUPERVISOR:?}" \
MAC_HOME="$HOME/.mac" \
PY="$phase1_python" \
MAC_PHASE1_HELPER_SOURCE="$helper" \
MAC_PHASE1_DAEMON_FUNCTIONS_FILE="$functions" \
  bash "$helper" prepare >/dev/null
"$phase1_python" - "$HOME/.mac/phase1-cohort-restore-contract-${MAC_PHASE1_GENERATION:?}.json" <<'PY'
import os
import stat
import sys

descriptor = os.open(sys.argv[1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
        or metadata.st_size > 4 * 1024 * 1024
    ):
        raise SystemExit("phase-1 restore contract is unsafe")
    raw = os.read(descriptor, metadata.st_size + 1)
    if len(raw) != metadata.st_size:
        raise SystemExit("phase-1 restore contract changed while reading")
finally:
    os.close(descriptor)
sys.stdout.buffer.write(raw)
PY
REMOTE_PHASE1_PREPARE
  chmod 0600 "$contract_raw"
  "$PYTHON_BIN" - "$local_contract" "$contract_raw" "$agent" "$deployment_id" \
    "$GIT_REV" <<'PY'
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

output = Path(sys.argv[1])
raw = Path(sys.argv[2]).read_bytes()
agent, generation, revision = sys.argv[3:]
contract = json.loads(raw)
restore = contract.get("restore_executable")
functions = contract.get("daemon_function_block")
supervisor = contract.get("supervisor")
host_automation = contract.get("host_automation")
if (
    contract.get("schema") != "mac.phase1_cohort_restore_contract.v1"
    or contract.get("status") != "prepared"
    or contract.get("agent") != agent
    or contract.get("generation") != generation
    or contract.get("revision") != revision
    or contract.get("rollback_capable") is not True
    or not isinstance(restore, dict)
    or restore.get("mode") != "0700"
    or restore.get("argv") != [restore.get("path"), "restore"]
    or not isinstance(restore.get("sha256"), str)
    or len(restore["sha256"]) != 64
    or not isinstance(functions, dict)
    or functions.get("mode") != "0600"
    or not isinstance(functions.get("sha256"), str)
    or len(functions["sha256"]) != 64
    or not isinstance(supervisor, dict)
    or not isinstance(host_automation, dict)
    or host_automation.get("schema") != "mac.phase1_host_automation.v1"
    or host_automation.get("manager") != supervisor.get("manager")
    or not isinstance(host_automation.get("definitions"), list)
):
    reason = contract.get("rollback_ineligible_reason")
    raise SystemExit("selected node lacks an exact phase-1 restore contract: %s" % (reason or agent))
payload = {
    "schema": "mac.phase1_restore_contract_ready.v1",
    "agent": agent,
    "generation": generation,
    "revision": revision,
    "contract_sha256": hashlib.sha256(raw).hexdigest(),
    "contract": contract,
}
descriptor, temporary_raw = tempfile.mkstemp(prefix=output.name + ".", dir=str(output.parent))
temporary = Path(temporary_raw)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
finally:
    temporary.unlink(missing_ok=True)
PY
  echo "==> ${agent}: exact phase-1 restore contract prepared before cohort mutation"
}

quiesce_remote_agent_for_cohort() {
  local agent="$1" deployment_id="$2" supervisor="$3" fleet_name="$4" os_kind="$5"
  local remote_helper="/tmp/mac-phase1-quiesce-${agent}-${DEPLOY_CONTROLLER_NONCE}.sh"
  local remote_functions="/tmp/mac-phase1-daemon-functions-${agent}-${DEPLOY_CONTROLLER_NONCE}.sh"
  local ssh_parts=() ssh_args=() ssh_target item last_index fence_exec proof
  local agent_id local_proof restore_contract_sha256
  agent_id="$(stable_worker_agent_id "$agent")"
  local_proof="$TMPDIR_LOCAL/phase1-ready-${agent_id}.json"
  restore_contract_sha256="$(phase1_restore_contract_digest_for_agent "$agent")"
  fenced_remote_upload "$agent" "$deployment_id" "$PHASE1_QUIESCE_HELPER" "$remote_helper"
  fenced_remote_upload "$agent" "$deployment_id" "$PHASE1_DAEMON_FUNCTIONS" "$remote_functions"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  fence_exec="$(remote_deployment_fenced_exec "$deployment_id" 0 bash -s)"
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
    "${ssh_args[@]}" "$ssh_target" \
    "MAC_PHASE1_AGENT=$(shell_quote "$agent") MAC_PHASE1_FLEET=$(shell_quote "$fleet_name") MAC_PHASE1_OS=$(shell_quote "$os_kind") MAC_PHASE1_REV=$(shell_quote "$GIT_REV") MAC_PHASE1_GENERATION=$(shell_quote "$deployment_id") MAC_PHASE1_SUPERVISOR=$(shell_quote "$supervisor") MAC_PHASE1_HELPER=$(shell_quote "$remote_helper") MAC_PHASE1_FUNCTIONS=$(shell_quote "$remote_functions") MAC_PHASE1_RESTORE_SHA256=$(shell_quote "$restore_contract_sha256") $fence_exec" <<'REMOTE_PHASE1'
set -euo pipefail
helper="${MAC_PHASE1_HELPER:?}"
functions="${MAC_PHASE1_FUNCTIONS:?}"
cleanup() { rm -f "$helper" "$functions"; }
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
phase1_python=""
for candidate in \
  "$HOME/.mac/venv/bin/python" \
  python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if [ -x "$candidate" ]; then
    phase1_python="$candidate"
    break
  fi
  if command -v "$candidate" >/dev/null 2>&1; then
    phase1_python="$(command -v "$candidate")"
    break
  fi
done
[ -n "$phase1_python" ] || {
  echo "phase-1 quiescence requires Python 3.9 or newer" >&2
  exit 1
}
if ! "$phase1_python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "phase-1 quiescence requires Python 3.9 or newer" >&2
  exit 1
fi
AGENT="${MAC_PHASE1_AGENT:?}" \
FLEET_NAME="${MAC_PHASE1_FLEET:?}" \
OS_KIND="${MAC_PHASE1_OS:?}" \
DEPLOY_REV="${MAC_PHASE1_REV:?}" \
DEPLOY_GENERATION="${MAC_PHASE1_GENERATION:?}" \
SUPERVISOR_KIND="${MAC_PHASE1_SUPERVISOR:?}" \
MAC_HOME="$HOME/.mac" \
PY="$phase1_python" \
MAC_PHASE1_RESTORE_CONTRACT_SHA256="${MAC_PHASE1_RESTORE_SHA256:?}" \
MAC_PHASE1_DAEMON_FUNCTIONS_FILE="$functions" \
  bash "$helper" quiesce
REMOTE_PHASE1
  fence_exec="$(remote_deployment_fenced_exec "$deployment_id" 0 python3 -)"
  proof="$(ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" \
    "MAC_PHASE1_EXPECT_AGENT=$(shell_quote "$agent") MAC_PHASE1_EXPECT_REV=$(shell_quote "$GIT_REV") MAC_PHASE1_EXPECT_GENERATION=$(shell_quote "$deployment_id") MAC_PHASE1_EXPECT_RESTORE_SHA256=$(shell_quote "$restore_contract_sha256") $fence_exec" <<'PY'
import hashlib
import json
import os
import stat
from pathlib import Path

path = Path.home() / ".mac" / (
    "phase1-cohort-quiescence-%s.json" % os.environ["MAC_PHASE1_EXPECT_GENERATION"]
)
metadata = path.lstat()
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != os.getuid()
    or stat.S_IMODE(metadata.st_mode) != 0o600
    or metadata.st_size > 4 * 1024 * 1024
):
    raise SystemExit("phase-1 cohort receipt is unsafe")
raw = path.read_bytes()
payload = json.loads(raw)
if (
    not isinstance(payload, dict)
    or payload.get("schema") != "mac.phase1_cohort_quiescence.v1"
    or payload.get("agent") != os.environ["MAC_PHASE1_EXPECT_AGENT"]
    or payload.get("revision") != os.environ["MAC_PHASE1_EXPECT_REV"]
    or payload.get("generation") != os.environ["MAC_PHASE1_EXPECT_GENERATION"]
    or payload.get("source_contract_sha256")
    != os.environ["MAC_PHASE1_EXPECT_RESTORE_SHA256"]
):
    raise SystemExit("phase-1 cohort receipt belongs to another deployment")
print(
    json.dumps(
        {
            "schema": "mac.phase1_cohort_ready.v1",
            "agent": payload["agent"],
            "generation": payload["generation"],
            "revision": payload["revision"],
            "receipt_sha256": hashlib.sha256(raw).hexdigest(),
            "receipt": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
PY
)"
  "$PYTHON_BIN" - "$local_proof" "$proof" <<'PY'
import json
import os
import tempfile
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(sys.argv[2])
fd, raw = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
tmp = Path(raw)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
finally:
    tmp.unlink(missing_ok=True)
PY
  echo "==> ${agent}: phase-1 supervisor and daemon resources proved quiescent"
}

prepare_remote_mac_agent_deployment() {
  local agent="$1" deployment_id="$2" supervisor="${3:-auto}" fleet_name="${4:-mac}"
  local adoption_reason="${5:-}" require_owned_after_prepare="${6:-0}"
  local os_kind="${7:-}"
  local agent_id state prior_owned prior_hold_reason hold_reason result owns_hold agent_existed gate_phase=prepare
  agent_id="$(stable_worker_agent_id "$agent")"
  if ! hub_dispatch_hold_cas_available; then
    if [ -n "$adoption_reason" ] || [ "$require_owned_after_prepare" = 1 ]; then
      echo "==> ${agent}: legacy CAS bootstrap rejects hold adoption and exact full-cohort release" >&2
      return 1
    fi
    case "$(printf '%s' "${MAC_DEPLOY_ALLOW_LEGACY_CAS_BOOTSTRAP:-0}" | tr '[:upper:]' '[:lower:]')" in
      1|true|yes|on) ;;
      *)
        echo "==> ${agent}: live hub lacks dispatch-hold CAS routes; deploy the pre-held hub first with explicit MAC_DEPLOY_ALLOW_LEGACY_CAS_BOOTSTRAP=1" >&2
        return 1
        ;;
    esac
    if [ "$agent" != "$(fleet_hub_agent)" ]; then
      echo "==> ${agent}: legacy CAS bootstrap is restricted to the configured hub agent" >&2
      return 1
    fi
    gate_phase=legacy-bootstrap
    echo "==> ${agent}: using one-time pre-held hub bootstrap to install CAS routes"
  fi
  acquire_remote_deployment_lock "$agent" "$deployment_id"
  state="$(remote_deployment_hold_state "$agent")"
  prior_owned="$(printf '%s' "$state" | "$PYTHON_BIN" -c 'import json,sys; print("1" if json.load(sys.stdin).get("owns_hold") else "0")')"
  prior_hold_reason="$(printf '%s' "$state" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("hold_reason") or "")')"
  hold_reason="mac fleet deployment ${deployment_id}"
  result="$(hub_agent_restart_gate "$gate_phase" "$agent_id" "" "" "$hold_reason" "$prior_owned" 1 0 "$prior_hold_reason" "" "$adoption_reason" "$require_owned_after_prepare")"
  owns_hold="$(printf '%s' "$result" | "$PYTHON_BIN" -c 'import json,sys; print("1" if json.load(sys.stdin).get("owns_hold") else "0")')"
  agent_existed="$(printf '%s' "$result" | "$PYTHON_BIN" -c 'import json,sys; print("1" if json.load(sys.stdin).get("exists") else "0")')"
  if [ "$require_owned_after_prepare" = 1 ] \
    && [ "$agent_existed" = 1 ] && [ "$owns_hold" != 1 ]; then
    echo "==> ${agent}: exact full-cohort release requires deployment ownership of the live hold" >&2
    return 1
  fi
  # This precedes every other target mutation. If the transaction rolls back
  # and restarts an older worker, that restored process cannot clear the hub
  # barrier before the outer controller performs an exact-generation restart.
  set_remote_mac_startup_hold_policy "$agent" 0
  write_remote_deployment_hold_state \
    "$agent" "$deployment_id" "$hold_reason" "$owns_hold" "$agent_existed" \
    "$adoption_reason" "$require_owned_after_prepare"
  # A pre-cutover worker may not yet honor dispatch_hold before controls,
  # service claims, or review nudges, and older review claims are not visible
  # through current_task_id. Stop every selected worker immediately after its
  # hub fence/drain proof. Phase 2 restarts only the new generation behind its
  # local barrier, so completing phase 1 creates a real fleet-wide quiescent
  # boundary rather than a best-effort database label.
  [ -n "$os_kind" ] || {
    echo "==> ${agent}: phase-1 quiescence lacks a frozen OS identity" >&2
    return 1
  }
  quiesce_remote_agent_for_cohort \
    "$agent" "$deployment_id" "$supervisor" "$fleet_name" "$os_kind"
  if [ "$gate_phase" = legacy-bootstrap ]; then
    echo "==> ${agent}: legacy hub worker stopped after drain and before source mutation"
  else
    echo "==> ${agent}: worker stopped after drain and before cohort deployment"
  fi
  echo "==> ${agent}: durable hub dispatch barrier established before remote mutation"
}

set_remote_mac_agent_service() {
  local agent="$1" supervisor="$2" fleet_name="$3" action="$4"
  local release_mode="${5:-keep}" release_policy="${6:-authenticated}"
  local expected_principal_id="${7:-}" release_commit_mode="${8:-immediate}"
  local require_report_executor="${9:-0}"
  local agent_id state deployment_id hold_reason prior_owned gate_result owns_hold release_result hold_cleared
  local agent_existed adopted_from_reason require_owned_after_prepare new_agent=0
  local generation="" baseline_seen="" release_baseline="" require_authenticated=1
  local quiescence_attestation=""
  local expected_deployment_id service_fence release_fence restore_fence cleanup_fence
  local ssh_parts=() ssh_args=() ssh_target last_index item
  agent_id="$(stable_worker_agent_id "$agent")"
  expected_deployment_id="$(deployment_id_for_agent "$agent")"
  service_fence="$(remote_deployment_fenced_exec "$expected_deployment_id" 0 bash -s)"
  assert_remote_deployment_lock "$agent" "$expected_deployment_id"
  state="$(remote_deployment_hold_state "$agent")"
  deployment_id="$(printf '%s' "$state" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("deployment_id") or "")')"
  hold_reason="$(printf '%s' "$state" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("hold_reason") or "")')"
  prior_owned="$(printf '%s' "$state" | "$PYTHON_BIN" -c 'import json,sys; print("1" if json.load(sys.stdin).get("owns_hold") else "0")')"
  agent_existed="$(printf '%s' "$state" | "$PYTHON_BIN" -c 'import json,sys; print("1" if json.load(sys.stdin).get("agent_existed", True) else "0")')"
  adopted_from_reason="$(printf '%s' "$state" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("adopted_from_reason") or "")')"
  require_owned_after_prepare="$(printf '%s' "$state" | "$PYTHON_BIN" -c 'import json,sys; print("1" if json.load(sys.stdin).get("require_owned_after_prepare") else "0")')"
  [ -n "$deployment_id" ] && [ -n "$hold_reason" ] || {
    echo "==> ${agent}: missing durable deployment hold state" >&2
    return 1
  }
  if [ "$deployment_id" != "$expected_deployment_id" ]; then
    echo "==> ${agent}: deployment state fence belongs to $deployment_id, expected $expected_deployment_id" >&2
    return 1
  fi
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")

  if [ "$action" = restart ]; then
    generation="$(worker_generation_for_agent "$agent")"
    gate_result="$(hub_agent_restart_gate prepare "$agent_id" "$generation" "" "$hold_reason" "$prior_owned" "$([ "$agent_existed" = 0 ] && printf 1 || printf 0)" 0 "$hold_reason" "" "" "$require_owned_after_prepare")"
    baseline_seen="$(printf '%s' "$gate_result" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("baseline_seen") or "")')"
    owns_hold="$(printf '%s' "$gate_result" | "$PYTHON_BIN" -c 'import json,sys; print("1" if json.load(sys.stdin).get("owns_hold") else "0")')"
    new_agent="$(printf '%s' "$gate_result" | "$PYTHON_BIN" -c 'import json,sys; print("0" if json.load(sys.stdin).get("exists") else "1")')"
    write_remote_deployment_hold_state "$agent" "$deployment_id" "$hold_reason" "$owns_hold" "$agent_existed" "$adopted_from_reason" "$require_owned_after_prepare"
    prior_owned="$owns_hold"
  elif [ "$action" = release ]; then
    generation="$(ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
      "$(remote_deployment_fenced_exec "$expected_deployment_id" 0 sh -c 'set -euo pipefail; set -a; . "$HOME/.mac/mac.env"; set +a; printf "%s" "${MAC_WORKER_DEPLOY_GENERATION:?}"')")"
    gate_result="$(hub_agent_restart_gate prepare "$agent_id" "$generation" "" "$hold_reason" "$prior_owned" 0 0 "$hold_reason" "" "" "$require_owned_after_prepare")"
    release_baseline="$(printf '%s' "$gate_result" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("baseline_seen") or "")')"
    owns_hold="$(printf '%s' "$gate_result" | "$PYTHON_BIN" -c 'import json,sys; print("1" if json.load(sys.stdin).get("owns_hold") else "0")')"
    write_remote_deployment_hold_state "$agent" "$deployment_id" "$hold_reason" "$owns_hold" 1 "$adopted_from_reason" "$require_owned_after_prepare"
    prior_owned="$owns_hold"
  fi

  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_SERVICE_ACTION=$(shell_quote "$action") MAC_DEPLOY_SUPERVISOR=$(shell_quote "$supervisor") MAC_DEPLOY_FLEET_NAME=$(shell_quote "$fleet_name") MAC_DEPLOY_RESTART_GENERATION=$(shell_quote "$generation") MAC_DEPLOY_SERVICE_TS=$(shell_quote "$TS") $service_fence" <<'REMOTE'
set -euo pipefail
action="${MAC_DEPLOY_SERVICE_ACTION:?}"
supervisor="${MAC_DEPLOY_SUPERVISOR:?}"
if [ "$supervisor" = "auto" ]; then
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    supervisor=systemd
  elif [ "$(uname -s)" = "Darwin" ]; then
    supervisor=launchd
  elif command -v supervisorctl >/dev/null 2>&1; then
    supervisor=supervisord
  else
    echo "could not resolve supervisor=auto on remote host" >&2
    exit 1
  fi
fi
if [ "$action" = restart ]; then
  generation="${MAC_DEPLOY_RESTART_GENERATION:?}"
  env_file="$HOME/.mac/mac.env"
  barrier_path="$HOME/.mac/deploy-start-barrier"
  MAC_DEPLOY_ENV_FILE="$env_file" MAC_DEPLOY_BARRIER_FILE="$barrier_path" \
    MAC_DEPLOY_RESTART_GENERATION="$generation" \
    "$HOME/.mac/venv/bin/python" - <<'PY'
import os
import tempfile
from pathlib import Path

from mac.deploy_env import read_env_file, write_env_file

env_path = Path(os.environ["MAC_DEPLOY_ENV_FILE"])
barrier_path = Path(os.environ["MAC_DEPLOY_BARRIER_FILE"])
generation = os.environ["MAC_DEPLOY_RESTART_GENERATION"]
values = read_env_file(env_path)
values["MAC_WORKER_DEPLOY_GENERATION"] = generation
values["MAC_WORKER_DEPLOY_BARRIER_FILE"] = str(barrier_path)
values["MAC_STARTUP_CLEAR_HOLD"] = "0"
fd, raw = tempfile.mkstemp(prefix=env_path.name + ".", dir=str(env_path.parent))
os.close(fd)
tmp = Path(raw)
try:
    write_env_file(tmp, values)
    os.replace(tmp, env_path)
finally:
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
barrier_tmp = barrier_path.with_name(barrier_path.name + ".tmp.%s" % os.getpid())
barrier_tmp.write_text(generation + "\n", encoding="utf-8")
barrier_tmp.chmod(0o600)
os.replace(barrier_tmp, barrier_path)
if barrier_path.read_text(encoding="utf-8").strip() != generation:
    raise SystemExit("fresh deployment barrier verification failed")
PY
fi
lifecycle="$HOME/.mac/logs/launchd-lifecycle-${MAC_DEPLOY_SERVICE_TS:?}.sh"
if [ ! -f "$lifecycle" ] || [ -L "$lifecycle" ]; then
  echo "bounded supervisor lifecycle contract is unavailable: $lifecycle" >&2
  exit 1
fi
# shellcheck disable=SC1090 -- owner-private snapshot of this deployment's
# reviewed bounded-command contract, created before source mutation.
. "$lifecycle"

run_fleet_supervisorctl() {
  if [ "$(id -u)" -eq 0 ]; then
    mac_run_bounded \
      "${MAC_SUPERVISOR_COMMAND_TIMEOUT_SECONDS:-30}" \
      supervisorctl "$@"
  else
    command -v sudo >/dev/null 2>&1 || {
      echo "system supervisord scope requires non-interactive sudo" >&2
      return 1
    }
    mac_run_bounded \
      "${MAC_SUPERVISOR_COMMAND_TIMEOUT_SECONDS:-30}" \
      sudo -n supervisorctl "$@"
  fi
}

run_fleet_systemctl() {
  if [ "$(id -u)" -eq 0 ]; then
    mac_run_bounded \
      "${MAC_SYSTEMD_COMMAND_TIMEOUT_SECONDS:-30}" \
      systemctl "$@"
  else
    command -v sudo >/dev/null 2>&1 || {
      echo "systemd system scope requires non-interactive sudo" >&2
      return 1
    }
    mac_run_bounded \
      "${MAC_SYSTEMD_COMMAND_TIMEOUT_SECONDS:-30}" \
      sudo -n systemctl "$@"
  fi
}

SUPERVISORD_WORKER_STATE=""
SUPERVISORD_WORKER_DETAIL=""
read_exact_supervisord_worker_state() {
  local program="$1" status="" status_rc=0 observed_program="" observed_state="" detail=""
  if status="$(run_fleet_supervisorctl status "$program" 2>&1)"; then
    status_rc=0
  else
    status_rc=$?
  fi
  if [ "$status_rc" -ne 0 ]; then
    echo "could not inspect exact supervisord worker $program (rc=$status_rc): $status" >&2
    return 1
  fi
  case "$status" in
    *$'\n'*)
      echo "supervisord returned ambiguous multi-line state for $program" >&2
      return 1
      ;;
  esac
  IFS=' ' read -r observed_program observed_state detail <<<"$status"
  if [ "$observed_program" != "$program" ]; then
    echo "supervisord returned a different worker identity for $program" >&2
    return 1
  fi
  case "$observed_state" in
    BACKOFF|EXITED|FATAL|RUNNING|STARTING|STOPPED|STOPPING) ;;
    *)
      echo "supervisord returned an unrecognized state for $program" >&2
      return 1
      ;;
  esac
  SUPERVISORD_WORKER_STATE="$observed_state"
  SUPERVISORD_WORKER_DETAIL="$detail"
}

systemd_worker_property() {
  local unit="$1" property="$2" value="" value_rc=0
  if value="$(run_fleet_systemctl show "$unit" "--property=$property" --value 2>&1)"; then
    value_rc=0
  else
    value_rc=$?
  fi
  if [ "$value_rc" -ne 0 ]; then
    echo "could not inspect systemd $property for exact worker $unit (rc=$value_rc): $value" >&2
    return 1
  fi
  case "$value" in
    *$'\n'*)
      echo "systemd returned ambiguous $property for exact worker $unit" >&2
      return 1
      ;;
  esac
  printf '%s\n' "$value"
}

case "$supervisor" in
  supervisord)
    program="${MAC_DEPLOY_FLEET_NAME:?}-agent"
    if [ "$action" = restart ] || [ "$action" = stop ]; then
      read_exact_supervisord_worker_state "$program"
      case "$SUPERVISORD_WORKER_STATE" in
        RUNNING|STARTING|BACKOFF|STOPPING)
          run_fleet_supervisorctl stop "$program" >/dev/null
          read_exact_supervisord_worker_state "$program"
          ;;
        STOPPED|EXITED|FATAL) ;;
      esac
      case "$SUPERVISORD_WORKER_STATE" in
        STOPPED|EXITED|FATAL) ;;
        *) echo "exact supervisord worker did not become inactive: $program" >&2; exit 1 ;;
      esac
    fi
    if [ "$action" = restart ]; then
      run_fleet_supervisorctl start "$program" >/dev/null
      read_exact_supervisord_worker_state "$program"
      if [ "$SUPERVISORD_WORKER_STATE" != RUNNING ]; then
        echo "exact supervisord worker did not become running: $program" >&2
        exit 1
      fi
      case "$SUPERVISORD_WORKER_DETAIL" in
        pid\ *,*)
          worker_pid="${SUPERVISORD_WORKER_DETAIL#pid }"
          worker_pid="${worker_pid%%,*}"
          case "$worker_pid" in
            ''|0|*[!0-9]*)
              echo "running supervisord worker lacks a positive pid: $program" >&2
              exit 1
              ;;
          esac
          ;;
        *)
          echo "running supervisord worker lacks a positive pid: $program" >&2
          exit 1
          ;;
      esac
    fi
    ;;
  systemd)
    unit="${MAC_DEPLOY_FLEET_NAME:?}-agent.service"
    if [ "$action" = restart ] || [ "$action" = stop ]; then
      if [ "$(systemd_worker_property "$unit" LoadState)" != loaded ]; then
        echo "exact systemd worker unit is not loaded: $unit" >&2
        exit 1
      fi
      run_fleet_systemctl stop "$unit" >/dev/null
      active_state="$(systemd_worker_property "$unit" ActiveState)"
      sub_state="$(systemd_worker_property "$unit" SubState)"
      main_pid="$(systemd_worker_property "$unit" MainPID)"
      if [ "$active_state" != inactive ] || [ "$sub_state" != dead ] || [ "$main_pid" != 0 ]; then
        echo "exact systemd worker did not become inactive/dead with pid 0: $unit" >&2
        exit 1
      fi
    fi
    if [ "$action" = restart ]; then
      run_fleet_systemctl start "$unit" >/dev/null
      active_state="$(systemd_worker_property "$unit" ActiveState)"
      sub_state="$(systemd_worker_property "$unit" SubState)"
      main_pid="$(systemd_worker_property "$unit" MainPID)"
      if [ "$active_state" != active ] || [ "$sub_state" != running ]; then
        echo "exact systemd worker did not become active/running: $unit" >&2
        exit 1
      fi
      case "$main_pid" in
        ''|0|*[!0-9]*)
          echo "running systemd worker lacks a positive pid: $unit" >&2
          exit 1
          ;;
      esac
    fi
    ;;
  launchd)
    label="com.${MAC_DEPLOY_FLEET_NAME:?}.agent"
    domain="gui/$(id -u)"
    MAC_LAUNCHD_LOG_PREFIX="[mac-agent:${label}]"
    if [ "$action" = restart ] || [ "$action" = stop ]; then
      mac_launchd_stop_job_if_present "$domain/$label" "$label" user
    fi
    if [ "$action" = restart ]; then
      # A deferred restart intentionally leaves the freshly written plist
      # unregistered until the post manifest has reconciled. The shared helper
      # proves the pre-quiesced generation absent under one monotonic deadline
      # before it bootstraps the reviewed replacement.
      plist="$HOME/Library/LaunchAgents/${label}.plist"
      [ -f "$plist" ] && [ ! -L "$plist" ] || {
        echo "launchd agent plist missing or unsafe: $plist" >&2
        exit 1
      }
      mac_launchd_bootstrap_job \
        "$domain" "$plist" "$domain/$label" "$label" user
    fi
    ;;
  *) echo "unsupported supervisor: $supervisor" >&2; exit 1 ;;
esac
REMOTE

  if [ "$action" = restart ]; then
    if [ "$new_agent" = 1 ]; then
      gate_result="$(hub_agent_restart_gate prepare-new "$agent_id" "$generation" "" "$hold_reason" "$prior_owned" 0 0 "$hold_reason" "" "" "$require_owned_after_prepare")"
      baseline_seen="$(printf '%s' "$gate_result" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("baseline_seen") or "")')"
      owns_hold="$(printf '%s' "$gate_result" | "$PYTHON_BIN" -c 'import json,sys; print("1" if json.load(sys.stdin).get("owns_hold") else "0")')"
      prior_owned="$owns_hold"
      write_remote_deployment_hold_state "$agent" "$deployment_id" "$hold_reason" "$owns_hold" 1 "$adopted_from_reason" "$require_owned_after_prepare"
    fi
    hub_agent_restart_gate verify "$agent_id" "$generation" "$baseline_seen" \
      "$hold_reason" "$prior_owned" 0 0 >/dev/null
    if [ "$release_mode" = keep ]; then
      echo "==> ${agent}: exact generation restart kept under deployment barrier"
      return 0
    fi
    echo "==> ${agent}: restart release mode is unsupported; activate credentials before explicit release" >&2
    return 1
  fi
  if [ "$action" = release ]; then
    # Unlinking the process barrier is not authorization: the durable dispatch
    # hold remains until a worker-generated idle heartbeat advances the hub
    # clock and the hub clears only this deployment's own hold.
    if ! quiescence_attestation="$(
      remote_daemon_quiescence_attestation "$agent" "$expected_deployment_id"
    )"; then
      echo "==> ${agent}: current daemon quiescence could not be proved before release arm" >&2
      return 1
    fi
    if ! assert_phase1_attestation_matches_controller \
      "$agent_id" "$quiescence_attestation"; then
      echo "==> ${agent}: release attestation is not bound to this cohort's phase-1 proof" >&2
      return 1
    fi
    release_fence="$(remote_deployment_fenced_exec "$expected_deployment_id" 0 bash -s)"
    ssh -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
      "MAC_DEPLOY_RELEASE_GENERATION=$(shell_quote "$generation") $release_fence" <<'REMOTE_RELEASE'
set -euo pipefail
generation="${MAC_DEPLOY_RELEASE_GENERATION:?}"
set -a
. "$HOME/.mac/mac.env"
set +a
barrier_path="${MAC_WORKER_DEPLOY_BARRIER_FILE:?}"
[ "${MAC_WORKER_DEPLOY_GENERATION:?}" = "$generation" ]
[ "$(cat "$barrier_path")" = "$generation" ]
rm -f "$barrier_path"
REMOTE_RELEASE
    case "$release_policy" in
      authenticated) require_authenticated=1 ;;
      legacy) require_authenticated=0 ;;
      *) echo "unsupported release policy: $release_policy" >&2; return 1 ;;
    esac
    local release_gate_phase=release
    if [ "$release_commit_mode" = deferred ]; then
      release_gate_phase=arm
    elif [ "$release_commit_mode" != immediate ]; then
      echo "unsupported deployment release commit mode: $release_commit_mode" >&2
      return 1
    fi
    if ! release_result="$(hub_agent_restart_gate "$release_gate_phase" "$agent_id" "$generation" "$release_baseline" \
      "$hold_reason" "$prior_owned" 0 "$require_authenticated" "$hold_reason" "$expected_principal_id" "" "$require_owned_after_prepare" "$require_report_executor")"; then
      if ! fail_release_with_compensation \
        "${agent}: hub release-readiness gate failed" \
        "$agent" "$expected_deployment_id" "$generation" "$agent_id" \
        "$hold_reason" "$prior_owned"; then
        return 1
      fi
    fi
    if ! quiescence_attestation="$(
      remote_daemon_quiescence_attestation "$agent" "$expected_deployment_id"
    )"; then
      if ! fail_release_with_compensation \
        "${agent}: daemon quiescence changed while release readiness was armed" \
        "$agent" "$expected_deployment_id" "$generation" "$agent_id" \
        "$hold_reason" "$prior_owned"; then
        return 1
      fi
    fi
    if ! assert_phase1_attestation_matches_controller \
      "$agent_id" "$quiescence_attestation"; then
      if ! fail_release_with_compensation \
        "${agent}: phase-1 proof changed while release readiness was armed" \
        "$agent" "$expected_deployment_id" "$generation" "$agent_id" \
        "$hold_reason" "$prior_owned"; then
        return 1
      fi
    fi
    if [ "$release_commit_mode" = deferred ]; then
      local ready_file="$TMPDIR_LOCAL/release-ready-${agent_id}.json"
      "$PYTHON_BIN" - "$ready_file" "$agent" "$agent_id" "$supervisor" "$fleet_name" \
      "$generation" "$release_baseline" "$hold_reason" "$prior_owned" \
        "$expected_principal_id" "$require_authenticated" "$deployment_id" \
        "$require_report_executor" "$quiescence_attestation" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

(
    output,
    agent,
    agent_id,
    supervisor,
    fleet_name,
    generation,
    baseline,
    hold_reason,
    owns_hold,
    principal_id,
    require_authenticated,
    deployment_id,
    require_report_executor,
    quiescence_raw,
) = sys.argv[1:]
quiescence = json.loads(quiescence_raw)
if quiescence.get("schema") != "mac.daemon_resource_quiescence_attestation.v1":
    raise SystemExit("release readiness lacks daemon quiescence attestation")
path = Path(output)
fd, raw = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
tmp = Path(raw)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(
            {
                "schema": "mac.deploy_release_ready.v1",
                "agent": agent,
                "agent_id": agent_id,
                "supervisor": supervisor,
                "fleet_name": fleet_name,
                "generation": generation,
                "baseline_seen": baseline,
                "hold_reason": hold_reason,
                "owns_hold": owns_hold == "1",
                "principal_id": principal_id,
                "require_authenticated": require_authenticated == "1",
                "deployment_id": deployment_id,
                "require_report_executor": require_report_executor == "1",
                "quiescence": quiescence,
            },
            stream,
            sort_keys=True,
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, path)
finally:
    tmp.unlink(missing_ok=True)
PY
      echo "==> ${agent}: release armed under durable hold for fleet epoch commit"
      return 0
    fi
    hold_cleared="$(printf '%s' "$release_result" | "$PYTHON_BIN" -c 'import json,sys; print("1" if json.load(sys.stdin).get("hold_cleared") else "0")')"
    # CAS release is the authorization linearization point. Never attempt to
    # re-hold after it succeeds: work may already have been claimed. This file
    # is retry bookkeeping only; managed nodes permanently retain
    # MAC_STARTUP_CLEAR_HOLD=0 in mac.env.
    cleanup_fence="$(remote_deployment_fenced_exec "$expected_deployment_id" 0 sh -c 'rm -f "$HOME/.mac/deploy-dispatch-hold.json"')"
    if ! ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
      "$cleanup_fence"; then
      echo "==> ${agent}: WARNING: released successfully but could not remove deployment hold bookkeeping" >&2
    fi
    if ! release_remote_deployment_lock "$agent" "$expected_deployment_id"; then
      echo "==> ${agent}: WARNING: released successfully but could not remove deployment controller lock" >&2
    fi
    if [ "$hold_cleared" = 1 ]; then
      echo "==> ${agent}: deployment-owned dispatch barrier released after exact idle credential proof"
    else
      echo "==> ${agent}: process barrier released after exact idle credential proof; pre-existing operator dispatch hold preserved"
    fi
  fi
}

validate_router_topology_spec() {
  # Local, read-only preflight. Run before tunnels, archive copies, or remote
  # service/source mutation so an incomplete inproc topology cannot replace a
  # working deployment with a process-healthy but model-dead configuration.
  local spec="$1" hub_token="${2:-}" agent hub_url shared_services_manager
  local router_backend router_backend_lc router_providers local_token
  IFS='|' read -r agent _ _ _ _ _ _ hub_url _ _ _ _ _ _ _ shared_services_manager _ <<<"$spec"
  router_backend="$(fleet_scoped_env MAC_ROUTER_BACKEND "$agent")"
  router_backend_lc="$(printf '%s' "$router_backend" | tr 'A-Z' 'a-z')"
  [ "$router_backend_lc" = "inproc" ] || return 0
  if [ "$agent" = "$shared_services_manager" ]; then
    router_providers="$(fleet_scoped_env MAC_ROUTER_PROVIDERS "$agent")"
    if [ -z "$router_providers" ]; then
      echo "ERROR: ${agent}: inproc router hub requires MAC_ROUTER_PROVIDERS (exported remotely as MAC_DEPLOY_ROUTER_PROVIDERS)" >&2
      return 1
    fi
    local provider_spec
    while IFS= read -r provider_spec; do
      [ -n "$provider_spec" ] || continue
      case ",$provider_spec," in
        *,key=secret:*) ;;
        *)
          echo "ERROR: ${agent}: every inproc router provider must use key=secret:<name>" >&2
          return 1
          ;;
      esac
    done < <(printf '%s' "$router_providers" | tr ';' '\n')
    return 0
  fi
  if [ -z "$hub_url" ]; then
    echo "ERROR: ${agent}: inproc router spoke requires a hub URL" >&2
    return 1
  fi
  if [ -z "$hub_token" ]; then
    echo "ERROR: ${agent}: inproc router spoke requires a hub-facing token" >&2
    return 1
  fi
  local_token="$(fleet_scoped_env MAC_API_TOKEN "$agent")"
  if [ -n "$local_token" ] && [ "$hub_token" = "$local_token" ]; then
    echo "ERROR: ${agent}: inproc router spoke hub token must differ from its local MAC_API_TOKEN" >&2
    return 1
  fi
}

validate_openshell_runtime_image_spec() {
  # Pure workers carry openshell_required in the frozen selected-host spec.
  # Reject an incomplete production cohort locally, before the controller
  # opens a remote connection or acquires any worker hold/lock.
  local spec="$1" agent openshell_required openshell_enabled=0 request required
  local fields=()
  IFS='|' read -r -a fields <<<"$spec"
  agent="${fields[0]:-unknown}"
  openshell_required="${fields[53]:-0}"
  request="$(normalize_boolean_token "${MAC_DEPLOY_OPENSHELL:-}")"
  required="$(normalize_boolean_token "$openshell_required")"
  case "$request" in
    1|true|yes|on) openshell_enabled=1 ;;
  esac
  case "$required" in
    1|true|yes|on) openshell_enabled=1 ;;
  esac
  [ "$openshell_enabled" = 1 ] || return 0
  case "$(printf '%s' "${MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD:-}" | tr 'A-Z' 'a-z')" in
    1|true|yes|on) return 0 ;;
  esac
  if [ -z "${MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE:-}" ]; then
    echo "ERROR: ${agent}: production OpenShell deployment requires MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE before fleet phase 1" >&2
    return 1
  fi
  case " ${MAC_DEPLOY_OPENSHELL_ARGS:-} " in
    *" --skip-image "*)
      echo "ERROR: ${agent}: --skip-image is incompatible with a digest-managed OpenShell deployment" >&2
      return 1
      ;;
  esac
}

deploy_host() {
  local spec="$1" hub_token="${2:-}" hub_tunnel_pubkey="${3:-}" allow_degraded_services="${4:-0}" github_review_key_b64="${5:-}" direct_mesh_hub_flag="${6:-0}" already_prepared="${7:-0}" node_action="${8:-legacy-one-shot}" prerequisite_bundle="${9:-}" prerequisite_expectations="${10:-}" node_identity_sha256="${11:-}" agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary supervisor shared_services_manager qdrant_url qdrant_install qdrant_required qdrant_bind_addr qdrant_port qdrant_image qdrant_memory_limit fleet_name control_port qdrant_data_dir firecrawl_url firecrawl_install firecrawl_required firecrawl_bind_addr firecrawl_port network_provider network_install network_hostname_prefix tailscale_auth_key_env headscale_manage headscale_login_server headscale_health_url headscale_fleet_url headscale_preauth_key_source headscale_preauth_key_env headscale_port headscale_public_addr headscale_dns headscale_ip_prefix webdav_enabled webdav_install webdav_url webdav_bind_addr webdav_port webdav_root webdav_public_path hermes_surface_b64 openshell_required github_credentials_required remote_archive remote_registry deploy_generation ssh_args ssh_target nvidia_api_key nvidia_api_base nvidia_base_url openai_api_key openai_base_url anthropic_api_key anthropic_base_url perplexity_api_key perplexity_base_url perplexity_api_base
  case "$node_action" in
    legacy-one-shot|arm-phase2|apply-phase2|finalize) ;;
    *) echo "ERROR: ${node_action}: unsupported deploy-host node action" >&2; return 2 ;;
  esac
  IFS='|' read -r agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary supervisor shared_services_manager qdrant_url qdrant_install qdrant_required qdrant_bind_addr qdrant_port qdrant_image qdrant_memory_limit fleet_name control_port qdrant_data_dir firecrawl_url firecrawl_install firecrawl_required firecrawl_bind_addr firecrawl_port network_provider network_install network_hostname_prefix tailscale_auth_key_env headscale_manage headscale_login_server headscale_health_url headscale_fleet_url headscale_preauth_key_source headscale_preauth_key_env headscale_port headscale_public_addr headscale_dns headscale_ip_prefix webdav_enabled webdav_install webdav_url webdav_bind_addr webdav_port webdav_root webdav_public_path hermes_surface_b64 openshell_required github_credentials_required <<<"$spec"
  deploy_generation="$(deployment_id_for_agent "$agent")"
  nvidia_api_key="$(fleet_scoped_env NVIDIA_API_KEY "$agent")"
  nvidia_api_base="$(fleet_scoped_env NVIDIA_API_BASE "$agent")"
  nvidia_base_url="$(fleet_scoped_env NVIDIA_BASE_URL "$agent")"
  openai_api_key="$(fleet_scoped_env OPENAI_API_KEY "$agent")"
  openai_base_url="$(fleet_scoped_env OPENAI_BASE_URL "$agent")"
  mem_embed_model="$(fleet_scoped_env MAC_MEMORY_EMBED_MODEL "$agent")"
  router_backend="$(fleet_scoped_env MAC_ROUTER_BACKEND "$agent")"
  router_providers="$(fleet_scoped_env MAC_ROUTER_PROVIDERS "$agent")"
  router_default_model="$(fleet_scoped_env MAC_ROUTER_DEFAULT_MODEL "$agent")"
  router_wildcard_models="$(fleet_scoped_env MAC_ROUTER_WILDCARD_MODELS "$agent")"
  anthropic_api_key="$(fleet_scoped_env ANTHROPIC_API_KEY "$agent")"
  anthropic_base_url="$(fleet_scoped_env ANTHROPIC_BASE_URL "$agent")"
  perplexity_api_key="$(fleet_scoped_env PERPLEXITY_API_KEY "$agent")"
  perplexity_base_url="$(fleet_scoped_env PERPLEXITY_BASE_URL "$agent")"
  perplexity_api_base="$(fleet_scoped_env PERPLEXITY_API_BASE "$agent")"
  local router_backend_lc
  router_backend_lc="$(printf '%s' "$router_backend" | tr 'A-Z' 'a-z')"
  # Stream B: upstream provider keys are CENTRALIZED on the hub. Only the hub runs
  # the router (and escrows these into its vault); a spoke routes chat through the
  # hub's /v1 and must never receive an upstream key in its deploy process env.
  # Blank them for inproc spokes so they are not embedded in the remote SSH command below.
  # ($shared_services_manager is the hub agent, parsed from the spec above.) These
  # four mirror the per-provider reads above + the registry (mac.providers
  # ROUTER_PROVIDERS); the spoke gateway env is scrubbed of the full set by
  # scrub_spoke_provider_secrets (also registry-derived).
  if [ "$agent" != "$shared_services_manager" ] && [ "$router_backend_lc" = "inproc" ]; then
    nvidia_api_key="" ; openai_api_key="" ; anthropic_api_key="" ; perplexity_api_key=""
    router_providers="" ; router_default_model="" ; router_wildcard_models=""
  fi
  remote_archive="/tmp/mac-${agent}-${TS}.tar.gz"
  remote_registry="/tmp/mac-fleets-${agent}-${TS}.yaml"
  local ssh_parts=() last_index
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")

  # Establish the durable hub-side dispatch barrier before any target-side
  # personality, source, runtime, or service mutation. The node-local drain is
  # defense in depth; this outer gate also works when the worker cannot reach
  # the hub itself.
  if [ "$already_prepared" != 1 ]; then
    prepare_remote_mac_agent_deployment \
      "$agent" "$deploy_generation" "$supervisor" "$fleet_name" "" "0" "$os"
  fi
  assert_remote_deployment_lock "$agent" "$deploy_generation"

  # A non-interactive OpenClaw node must not inherit the stock blank wizard
  # identity.  The provisioner is a no-op when Hermes or a configured
  # OpenClaw workspace already exists; otherwise it asks an established fleet
  # agent for a distinct, roster-aware personality and installs the validated
  # proposal before the remote transaction starts.
  # OpenClaw persona provisioning is only meaningful for conversational
  # (gateway) nodes; a pure worker needs no persona. It must never abort the
  # worker deploy — a mentor being unavailable or the persona step failing
  # (e.g. openclaw-agent rejecting a multi-line --message) leaves the worker
  # perfectly able to claim and execute tasks. So it is best-effort/non-fatal.
  local personality_result personality_status personality_file personality_remote
  local personality_install_cmd
  if [ "$node_action" = legacy-one-shot ]; then
    if personality_result="$(MAC_OPENCLAW_BOOTSTRAP_TOKEN="$hub_token" \
    PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
      "$PYTHON_BIN" "$ROOT/scripts/provision-openclaw-personality.py" \
        --config "$FLEET_REGISTRY_CONFIG" \
        --fleet "$HUB_SELECTOR" \
        --agent "$agent" \
        --hub-url "$hub_url" \
        --dry-run)"; then
    personality_file="$TMPDIR_LOCAL/openclaw-personality-${agent}.json"
    personality_status="$("$PYTHON_BIN" - "$personality_result" "$personality_file" <<'PY'
import json
import os
import sys

payload = json.loads(sys.argv[1])
status = str(payload.get("status") or "")
if status == "would_install":
    proposal = payload.get("proposal")
    if not isinstance(proposal, dict):
        raise SystemExit("personality dry-run omitted its validated proposal")
    with open(sys.argv[2], "w", encoding="utf-8") as stream:
        json.dump(proposal, stream, indent=2, sort_keys=True)
        stream.write("\n")
    os.chmod(sys.argv[2], 0o600)
print(status)
PY
)"
    if [ "$personality_status" = would_install ]; then
      personality_remote="/tmp/mac-openclaw-personality-${agent}-${TS}.json"
      fenced_remote_upload "$agent" "$deploy_generation" \
        "$personality_file" "$personality_remote"
      personality_install_cmd="$(remote_deployment_fenced_exec "$deploy_generation" 0 sh -c \
        "set -e; umask 077; mkdir -p \"\$HOME/.mac/openclaw/migration\"; chmod 0700 \"\$HOME/.mac/openclaw\" \"\$HOME/.mac/openclaw/migration\"; mv -f $(shell_quote "$personality_remote") \"\$HOME/.mac/openclaw/migration/personality-proposal.json\"; chmod 0600 \"\$HOME/.mac/openclaw/migration/personality-proposal.json\"")"
      ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
        "${ssh_args[@]}" "$ssh_target" "$personality_install_cmd"
      rm -f "$personality_file"
      echo "==> ${agent}: installed validated OpenClaw personality under deployment fence"
    fi
    else
      echo "==> ${agent}: OpenClaw persona provisioning skipped (non-fatal) — a worker does not require a persona"
    fi
  fi

  assert_remote_deployment_lock "$agent" "$deploy_generation"
  echo "==> ${agent}: copying mac release archive"
  # Stream every node-target file through a same-session exact fence. A
  # separate file-transfer multiplex client may silently reconnect when its
  # ControlMaster disappears;
  # this path instead uses fail-closed `ssh -O proxy` and holds the remote
  # fcntl guard while the atomic upload is materialized.
  fenced_remote_upload "$agent" "$deploy_generation" "$ARCHIVE" "$remote_archive"
  echo "==> ${agent}: copying fleet registry"
  fenced_remote_upload "$agent" "$deploy_generation" "$SANITIZED_FLEET_REGISTRY" "$remote_registry"

  echo "==> ${agent}: running one-time deploy"
  local remote_env=() remote_secret_env=() remote_cmd openshell_enabled=0
  local openshell_disable_requested=0 openshell_request openshell_required_normalized
  local effective_openshell_args="${MAC_DEPLOY_OPENSHELL_ARGS:-}"
  openshell_request="$(normalize_boolean_token "${MAC_DEPLOY_OPENSHELL:-}")"
  case "$openshell_request" in
    1|true|yes|on) openshell_enabled=1 ;;
    0|false|no|off) openshell_disable_requested=1 ;;
  esac
  openshell_required_normalized="$(normalize_boolean_token "$openshell_required")"
  case "$openshell_required_normalized" in
    1|true|yes|on)
      openshell_enabled=1
      openshell_disable_requested=0
      # Required workers may not accidentally turn a bootstrap into setup-only
      # mode.  Add both enforcement flags unless the caller already supplied
      # them; other caller flags (for example --skip-image) are preserved.
      case " $effective_openshell_args " in
        *" --enable "*) ;;
        *) effective_openshell_args="${effective_openshell_args:+$effective_openshell_args }--enable" ;;
      esac
      case " $effective_openshell_args " in
        *" --fail-closed "*) ;;
        *) effective_openshell_args="${effective_openshell_args:+$effective_openshell_args }--fail-closed" ;;
      esac
      ;;
  esac
  if [ "$openshell_enabled" = "1" ]; then
    case "$(printf '%s' "${MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD:-}" | tr 'A-Z' 'a-z')" in
      1|true|yes|on) ;;
      *)
        [ -n "${MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE:-}" ] || {
          echo "==> ${agent}: ERROR: production OpenShell deployment requires MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE" >&2
          return 1
        }
        case " $effective_openshell_args " in
          *" --skip-image "*)
            echo "==> ${agent}: ERROR: --skip-image is incompatible with a digest-managed OpenShell deployment" >&2
            return 1
            ;;
        esac
        ;;
    esac
  fi
  add_remote_env() { remote_env+=("$1=$(shell_quote "$2")"); }
  # Secret values are streamed over the same fenced SSH stdin and sourced
  # directly from /dev/stdin; they never gain a remote pathname and must never
  # appear in the ssh remote command or process argv.
  add_remote_secret_env() {
    [ -n "$2" ] || return 0
    remote_secret_env+=("$1=$(shell_quote "$2")")
  }
  add_remote_env MAC_DEPLOY_AGENT "$agent"
  add_remote_env MAC_DEPLOY_OS "$os"
  add_remote_env MAC_DEPLOY_ARCHIVE "$remote_archive"
  add_remote_env MAC_DEPLOY_FLEET_REGISTRY_FILE "$remote_registry"
  add_remote_env MAC_DEPLOY_CONFIGURED_AGENT_IDS "$CONFIGURED_AGENT_IDS"
  add_remote_env MAC_DEPLOY_TS "$TS"
  add_remote_env MAC_DEPLOY_GIT_REV "$GIT_REV"
  add_remote_env MAC_DEPLOY_GENERATION "$deploy_generation"
  add_remote_env MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE 1
  add_remote_env MAC_DEPLOY_GIT_URL "$GIT_URL"
  add_remote_env MAC_DEPLOY_GIT_BRANCH "$GIT_BRANCH"
  add_remote_env MAC_DEPLOY_HERMES_SLACK_HOME_CHANNEL_NAME "$home_channel"
  add_remote_env MAC_DEPLOY_HERMES_GATEWAY_MODEL "$gateway_model"
  add_remote_env MAC_DEPLOY_HERMES_GATEWAY_PROVIDER "$gateway_provider"
  add_remote_env MAC_DEPLOY_HERMES_GATEWAY_BASE_URL "$gateway_base_url"
  add_remote_env MAC_DEPLOY_HERMES_SURFACE_B64 "$hermes_surface_b64"
  add_remote_env MAC_DEPLOY_OPENCLAW_LIVE_CANARY "${MAC_DEPLOY_OPENCLAW_LIVE_CANARY:-0}"
  add_remote_env MAC_DEPLOY_HUB_URL "$hub_url"
  # The shared token exists only for the compatibility bootstrap. Keep it out
  # of the remote command/argv just like every other deploy credential; the
  # per-agent credential phase below replaces it before deployment succeeds.
  add_remote_secret_env MAC_DEPLOY_HUB_TOKEN "$hub_token"
  add_remote_env MAC_DEPLOY_CONTROL_BIND_HOST "$bind_host"
  add_remote_env MAC_DEPLOY_WORKER_MODE "$worker_mode"
  add_remote_env MAC_DEPLOY_WORKER_CAPABILITIES "$worker_capabilities"
  add_remote_env MAC_DEPLOY_WORKER_ALLOWED_PROJECTS "$worker_allowed_projects"
  add_remote_env MAC_DEPLOY_WORKER_REQUIRED_METADATA "$worker_required_metadata"
  add_remote_env MAC_DEPLOY_WORKER_REQUIRE_CANARY "$worker_require_canary"
  # Preserve the caller's three-state intent: blank means keep an optional
  # node's existing OpenShell state, true bootstraps it, and explicit false
  # tears down stale deploy-managed state.  The independently derived required
  # flag below still wins for pure workers.
  add_remote_env MAC_DEPLOY_OPENSHELL "${MAC_DEPLOY_OPENSHELL:-}"
  add_remote_env MAC_DEPLOY_OPENSHELL_REQUIRED "$openshell_required"
  # Bootstrap the final gateway inside the one-use node transaction, after the
  # exact source/runtime contract is installed but before OpenClaw creates a
  # long-lived sandbox.  Replacing the gateway after OpenClaw verification can
  # strand an older protobuf SandboxSpec that the new client cannot decode.
  add_remote_env MAC_DEPLOY_OPENSHELL_ENABLED "$openshell_enabled"
  add_remote_env MAC_DEPLOY_OPENSHELL_EFFECTIVE_ARGS "$effective_openshell_args"
  add_remote_env MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE "${MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE:-}"
  add_remote_env MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD "${MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD:-0}"
  add_remote_env MAC_DEPLOY_GITHUB_CREDENTIALS_REQUIRED "$github_credentials_required"
  add_remote_env MAC_DEPLOY_SUPERVISOR "$supervisor"
  add_remote_env MAC_DEPLOY_SHARED_SERVICES_MANAGER_AGENT "$shared_services_manager"
  add_remote_env MAC_DEPLOY_QDRANT_URL "$qdrant_url"
  add_remote_env MAC_DEPLOY_QDRANT_INSTALL "$qdrant_install"
  add_remote_env MAC_DEPLOY_REQUIRE_QDRANT_MEMORY "$qdrant_required"
  add_remote_env MAC_DEPLOY_QDRANT_BIND_ADDR "$qdrant_bind_addr"
  add_remote_env MAC_DEPLOY_QDRANT_PORT "$qdrant_port"
  add_remote_env MAC_DEPLOY_QDRANT_IMAGE "$qdrant_image"
  add_remote_env MAC_DEPLOY_QDRANT_MEMORY_LIMIT "$qdrant_memory_limit"
  add_remote_env MAC_DEPLOY_TARGET "$target"
  add_remote_env MAC_DEPLOY_FLEET_NAME "$fleet_name"
  add_remote_env MAC_DEPLOY_CONTROL_PORT "$control_port"
  add_remote_env MAC_DEPLOY_QDRANT_DATA_DIR "$qdrant_data_dir"
  add_remote_env MAC_DEPLOY_FIRECRAWL_URL "$firecrawl_url"
  add_remote_env MAC_DEPLOY_FIRECRAWL_INSTALL "$firecrawl_install"
  add_remote_env MAC_DEPLOY_REQUIRE_FIRECRAWL "$firecrawl_required"
  add_remote_env MAC_DEPLOY_FIRECRAWL_BIND_ADDR "$firecrawl_bind_addr"
  add_remote_env MAC_DEPLOY_FIRECRAWL_PORT "$firecrawl_port"
  add_remote_env MAC_DEPLOY_WEBDAV_ENABLED "$webdav_enabled"
  add_remote_env MAC_DEPLOY_WEBDAV_INSTALL "$webdav_install"
  add_remote_env MAC_DEPLOY_WEBDAV_URL "$webdav_url"
  add_remote_env MAC_DEPLOY_WEBDAV_BIND_ADDR "$webdav_bind_addr"
  add_remote_env MAC_DEPLOY_WEBDAV_PORT "$webdav_port"
  add_remote_env MAC_DEPLOY_WEBDAV_ROOT "$webdav_root"
  add_remote_env MAC_DEPLOY_WEBDAV_PUBLIC_PATH "$webdav_public_path"
  add_remote_env MAC_DEPLOY_WEBDAV_MAX_UPLOAD_BYTES "${MAC_DEPLOY_WEBDAV_MAX_UPLOAD_BYTES:-}"
  add_remote_env MAC_DEPLOY_NETWORK_PROVIDER "$network_provider"
  add_remote_env MAC_DEPLOY_NETWORK_INSTALL "$network_install"
  add_remote_env MAC_DEPLOY_NETWORK_HOSTNAME_PREFIX "$network_hostname_prefix"
  add_remote_env MAC_DEPLOY_TAILSCALE_AUTH_KEY_ENV "$tailscale_auth_key_env"
  add_remote_env MAC_DEPLOY_HEADSCALE_MANAGE "$headscale_manage"
  add_remote_env MAC_DEPLOY_HEADSCALE_LOGIN_SERVER "$headscale_login_server"
  add_remote_env MAC_DEPLOY_HEADSCALE_HEALTH_URL "$headscale_health_url"
  add_remote_env MAC_DEPLOY_HEADSCALE_FLEET_URL "$headscale_fleet_url"
  add_remote_env MAC_DEPLOY_HEADSCALE_PREAUTH_KEY_SOURCE "$headscale_preauth_key_source"
  add_remote_env MAC_DEPLOY_HEADSCALE_PREAUTH_KEY_ENV "$headscale_preauth_key_env"
  add_remote_env MAC_DEPLOY_HEADSCALE_PORT "$headscale_port"
  add_remote_env MAC_DEPLOY_HEADSCALE_PUBLIC_ADDR "$headscale_public_addr"
  add_remote_env MAC_DEPLOY_HEADSCALE_DNS "$headscale_dns"
  add_remote_env MAC_DEPLOY_HEADSCALE_IP_PREFIX "$headscale_ip_prefix"
  add_remote_env MAC_DEPLOY_DRAIN_MODE "${MAC_DEPLOY_DRAIN_MODE:-}"
  add_remote_env MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS "${MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS:-}"
  add_remote_env MAC_DEPLOY_DRAIN_POLL_SECONDS "${MAC_DEPLOY_DRAIN_POLL_SECONDS:-}"
  # Agent restart is deliberately deferred until the post manifest reconciles,
  # so drain release must be deferred too. The outer restart helper proves a
  # fresh heartbeat from the new generation before deployment may succeed.
  add_remote_env MAC_DEPLOY_DEFER_CLEAR_DRAIN 1
  # Starting/restarting the worker from inside the remote install transaction
  # can terminate that transaction before its post manifest is durable (most
  # visibly under supervisord).  The outer controller owns the restart after
  # it has reconciled the post manifest.
  add_remote_env MAC_DEPLOY_DEFER_AGENT_RESTART 1
  add_remote_env MAC_DEPLOY_HUB_TUNNEL_PUBKEY "$hub_tunnel_pubkey"
  add_remote_env MAC_DEPLOY_ALLOW_DEGRADED_SERVICES "${allow_degraded_services:-0}"
  add_remote_env MAC_DEPLOY_DIRECT_HUB "${direct_mesh_hub_flag:-0}"
  add_remote_secret_env MAC_DEPLOY_GITHUB_REVIEW_KEY_B64 "$github_review_key_b64"
  add_remote_env MAC_DEPLOY_MEMORY_EMBED_MODEL "$mem_embed_model"
  add_remote_env MAC_DEPLOY_ROUTER_BACKEND "$router_backend"
  add_remote_env MAC_DEPLOY_ROUTER_PROVIDERS "$router_providers"
  add_remote_env MAC_DEPLOY_ROUTER_DEFAULT_MODEL "$router_default_model"
  add_remote_env MAC_DEPLOY_ROUTER_WILDCARD_MODELS "$router_wildcard_models"
  # Modality reverse-proxy upstreams + keys (image/audio/video), supplied at
  # cluster init and DISTINCT from the chat key. The image upstream defaults in
  # deploy_env; audio/video are wired only when an upstream is configured. Keys
  # are escrowed on the HUB only — blank for inproc spokes (they route via the hub).
  add_remote_env MAC_DEPLOY_ROUTER_IMAGE_UPSTREAM "${MAC_DEPLOY_ROUTER_IMAGE_UPSTREAM:-}"
  add_remote_env MAC_DEPLOY_ROUTER_AUDIO_UPSTREAM "${MAC_DEPLOY_ROUTER_AUDIO_UPSTREAM:-}"
  add_remote_env MAC_DEPLOY_ROUTER_VIDEO_UPSTREAM "${MAC_DEPLOY_ROUTER_VIDEO_UPSTREAM:-}"
  # media-01 durable local-gen advertisement (GPU-gated in the agent on register).
  add_remote_env MAC_DEPLOY_AGENT_GEN_MODEL "${MAC_DEPLOY_AGENT_GEN_MODEL:-}"
  add_remote_env MAC_DEPLOY_AGENT_GEN_PORT "${MAC_DEPLOY_AGENT_GEN_PORT:-}"
  add_remote_env MAC_DEPLOY_AGENT_GEN_HOST "${MAC_DEPLOY_AGENT_GEN_HOST:-}"
  add_remote_env MAC_DEPLOY_AGENT_GEN_BASE_URL "${MAC_DEPLOY_AGENT_GEN_BASE_URL:-}"
  # CUDA wheel index for the gen-server venv torch install (per-GPU, e.g.
  # https://download.pytorch.org/whl/cu130 for the GB10/aarch64 box). Deploy-time
  # only; consumed by install_gpu_gen_server.
  add_remote_env MAC_DEPLOY_AGENT_GEN_TORCH_INDEX_URL "${MAC_DEPLOY_AGENT_GEN_TORCH_INDEX_URL:-}"
  # Optional HF cache/home for the gen server — point at pre-staged weights to
  # avoid a fresh multi-GB download (e.g. /home/jkh/gen/hf on the GB10 box).
  add_remote_env MAC_DEPLOY_AGENT_GEN_HF_HOME "${MAC_DEPLOY_AGENT_GEN_HF_HOME:-}"
  # B1b: audio/video local servers (CSV catalog-id model lists + ports).
  add_remote_env MAC_DEPLOY_AGENT_GEN_AUDIO_MODELS "${MAC_DEPLOY_AGENT_GEN_AUDIO_MODELS:-}"
  add_remote_env MAC_DEPLOY_AGENT_GEN_AUDIO_PORT "${MAC_DEPLOY_AGENT_GEN_AUDIO_PORT:-}"
  add_remote_env MAC_DEPLOY_AGENT_GEN_VIDEO_MODELS "${MAC_DEPLOY_AGENT_GEN_VIDEO_MODELS:-}"
  add_remote_env MAC_DEPLOY_AGENT_GEN_VIDEO_PORT "${MAC_DEPLOY_AGENT_GEN_VIDEO_PORT:-}"
  add_remote_env MAC_DEPLOY_AGENT_MEDIA_ROUTES "${MAC_DEPLOY_AGENT_MEDIA_ROUTES:-}"
  # media-01 service-role election: ops the fleet wants held (hub seeds + reconciles).
  add_remote_env MAC_DEPLOY_SERVICE_ROLE_OPS "${MAC_DEPLOY_SERVICE_ROLE_OPS:-}"
  # Git credential for cloning/pushing private repos (-> GH_TOKEN in mac.env).
  # This is deliberately separate from remote_env: the latter becomes argv.
  add_remote_secret_env MAC_DEPLOY_GH_TOKEN "${MAC_DEPLOY_GH_TOKEN:-}"
  # mac-selfdrive: hub self-drives its tick loop (review->merge->dispatch) on
  # this cadence so the autonomous loop needs no external clock. 30s; 0 disables.
  add_remote_env MAC_DEPLOY_HUB_TICK_INTERVAL_SECONDS "${MAC_DEPLOY_HUB_TICK_INTERVAL_SECONDS:-30}"
  # The hub-owned repository-ref reconciler is configured by the caller but
  # materialized on the remote host by mac.deploy_env. Forward every setting
  # explicitly so audit-first rollouts and non-default schedules survive SSH.
  add_remote_env MAC_DEPLOY_REPOSITORY_REF_RECONCILER_MODE "${MAC_DEPLOY_REPOSITORY_REF_RECONCILER_MODE:-}"
  add_remote_env MAC_DEPLOY_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS "${MAC_DEPLOY_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS:-}"
  add_remote_env MAC_DEPLOY_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS "${MAC_DEPLOY_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS:-}"
  add_remote_env MAC_DEPLOY_REPOSITORY_REF_RECONCILER_GRACE_DAYS "${MAC_DEPLOY_REPOSITORY_REF_RECONCILER_GRACE_DAYS:-}"
  # Assembly-line activation is an explicit hub opt-in. Empty values preserve
  # the remote fail-closed defaults; the deployment guide requires a complete
  # repository certifier contract and durable storage before setting these.
  add_remote_env MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED "${MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED:-}"
  add_remote_env MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED "${MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED:-}"
  add_remote_env MAC_DEPLOY_WORK_PACKAGE_BUNDLE_DIR "${MAC_DEPLOY_WORK_PACKAGE_BUNDLE_DIR:-}"
  add_remote_env MAC_DEPLOY_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT "${MAC_DEPLOY_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT:-}"
  # The randomized execution pilot belongs to the control-plane hub only.
  # Revision/percentage are ordinary configuration. The HMAC seed uses the
  # one-use secret stdin channel so it never appears in SSH argv or on spokes.
  if [ "$agent" = "$shared_services_manager" ]; then
    add_remote_env MAC_DEPLOY_EXECUTION_COHORT_REVISION "${MAC_DEPLOY_EXECUTION_COHORT_REVISION:-1}"
    add_remote_env MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT "${MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT:-50}"
    add_remote_secret_env MAC_DEPLOY_EXECUTION_COHORT_SEED "${MAC_DEPLOY_EXECUTION_COHORT_SEED:-}"
  fi
  local img_key="${NVIDIA_IMAGE_API_KEY:-}" aud_key="${NVIDIA_AUDIO_API_KEY:-}" vid_key="${NVIDIA_VIDEO_API_KEY:-}"
  if [ "$agent" != "$shared_services_manager" ] && [ "$router_backend_lc" = "inproc" ]; then
    img_key="" ; aud_key="" ; vid_key=""
  fi
  add_remote_secret_env NVIDIA_IMAGE_API_KEY "$img_key"
  add_remote_secret_env NVIDIA_AUDIO_API_KEY "$aud_key"
  add_remote_secret_env NVIDIA_VIDEO_API_KEY "$vid_key"
  add_remote_secret_env NVIDIA_API_KEY "$nvidia_api_key"
  add_remote_env NVIDIA_API_BASE "$nvidia_api_base"
  add_remote_env NVIDIA_BASE_URL "$nvidia_base_url"
  add_remote_secret_env OPENAI_API_KEY "$openai_api_key"
  add_remote_env OPENAI_BASE_URL "$openai_base_url"
  add_remote_secret_env ANTHROPIC_API_KEY "$anthropic_api_key"
  add_remote_env ANTHROPIC_BASE_URL "$anthropic_base_url"
  add_remote_secret_env PERPLEXITY_API_KEY "$perplexity_api_key"
  add_remote_env PERPLEXITY_BASE_URL "$perplexity_base_url"
  add_remote_env PERPLEXITY_API_BASE "$perplexity_api_base"
  # The deploy body is in the standalone fleet-node-install.sh script which is
  # copied to the remote node and executed there, eliminating the large stdin
  # heredoc and its interaction with child processes that read from stdin.
  unset -f add_remote_env add_remote_secret_env
  local remote_node_script="/tmp/mac-node-install-${agent}-${TS}.sh"
  local remote_tool_assets="/tmp/mac-reviewed-tool-assets-${agent}-${TS}.sh"
  local remote_launchd_lifecycle="/tmp/mac-launchd-lifecycle-${agent}-${DEPLOY_CONTROLLER_NONCE}.sh"
  local remote_rollback_supervisor="/tmp/mac-rollback-supervisor-${agent}-${DEPLOY_CONTROLLER_NONCE}.py"
  local remote_prerequisite_helper="/tmp/mac-prerequisite-receipts-${agent}-${DEPLOY_CONTROLLER_NONCE}.py"
  local remote_prerequisite_bundle="/tmp/mac-prerequisite-bundle-${agent}-${DEPLOY_CONTROLLER_NONCE}.json"
  local remote_prerequisite_expectations="/tmp/mac-prerequisite-expectations-${agent}-${DEPLOY_CONTROLLER_NONCE}.json"
  local deploy_script reviewed_tool_assets launchd_lifecycle rollback_supervisor
  deploy_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fleet-node-install.sh"
  reviewed_tool_assets="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reviewed-tool-assets.sh"
  launchd_lifecycle="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/launchd-lifecycle.sh"
  rollback_supervisor="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fleet-node-rollback-supervisor.py"
  echo "==> ${agent}: copying fleet-node-install.sh"
  fenced_remote_upload "$agent" "$deploy_generation" "$deploy_script" "$remote_node_script"
  echo "==> ${agent}: copying reviewed native-tool checksum contract"
  fenced_remote_upload "$agent" "$deploy_generation" "$reviewed_tool_assets" "$remote_tool_assets"
  echo "==> ${agent}: copying bounded launchd lifecycle contract"
  fenced_remote_upload "$agent" "$deploy_generation" "$launchd_lifecycle" "$remote_launchd_lifecycle"
  echo "==> ${agent}: copying bounded rollback supervisor contract"
  fenced_remote_upload "$agent" "$deploy_generation" "$rollback_supervisor" "$remote_rollback_supervisor"
  remote_env+=("MAC_DEPLOY_REVIEWED_TOOL_ASSETS=$(shell_quote "$remote_tool_assets")")
  remote_env+=("MAC_DEPLOY_LAUNCHD_LIFECYCLE=$(shell_quote "$remote_launchd_lifecycle")")
  remote_env+=("MAC_DEPLOY_ROLLBACK_SUPERVISOR_HELPER=$(shell_quote "$remote_rollback_supervisor")")
  if [ "$node_action" = arm-phase2 ] || [ "$node_action" = apply-phase2 ]; then
    [ -f "$prerequisite_bundle" ] && [ -f "$prerequisite_expectations" ] \
      && [ -n "$node_identity_sha256" ] || {
        echo "ERROR: ${agent}: typed phase 2 lacks prerequisite material" >&2
        return 1
      }
    fenced_remote_upload "$agent" "$deploy_generation" \
      "$PREREQUISITE_RECEIPT_HELPER" "$remote_prerequisite_helper"
    fenced_remote_upload "$agent" "$deploy_generation" \
      "$prerequisite_bundle" "$remote_prerequisite_bundle"
    fenced_remote_upload "$agent" "$deploy_generation" \
      "$prerequisite_expectations" "$remote_prerequisite_expectations"
    remote_env+=("MAC_DEPLOY_PREREQUISITE_HELPER=$(shell_quote "$remote_prerequisite_helper")")
    remote_env+=("MAC_DEPLOY_PREREQUISITE_HELPER_SHA256=$(shell_quote "$(sha256_file "$PREREQUISITE_RECEIPT_HELPER")")")
    remote_env+=("MAC_DEPLOY_PREREQUISITE_BUNDLE=$(shell_quote "$remote_prerequisite_bundle")")
    remote_env+=("MAC_DEPLOY_PREREQUISITE_EXPECTATIONS=$(shell_quote "$remote_prerequisite_expectations")")
    remote_env+=("MAC_DEPLOY_NODE_IDENTITY_SHA256=$(shell_quote "$node_identity_sha256")")
  fi
  local remote_cmd fenced_remote_cmd local_secret_payload
  # Consume the one-use credential stream directly from the fenced SSH stdin.
  # No predictable remote pathname exists for another process to pre-create as
  # a symlink, and the values still never enter the SSH command or process argv.
  remote_cmd="${remote_env[*]} sh -c 'umask 077; _mac_script=\$1; _mac_tool_assets=\$2; _mac_launchd_lifecycle=\$3; _mac_rollback_supervisor=\$4; _mac_action=\$5; trap \"rm -f \\\"\$_mac_script\\\" \\\"\$_mac_tool_assets\\\" \\\"\$_mac_launchd_lifecycle\\\" \\\"\$_mac_rollback_supervisor\\\"\" EXIT HUP INT TERM; set -a; . /dev/stdin; set +a; bash \"\$_mac_script\" \"\$_mac_action\"' sh $(shell_quote "$remote_node_script") $(shell_quote "$remote_tool_assets") $(shell_quote "$remote_launchd_lifecycle") $(shell_quote "$remote_rollback_supervisor") $(shell_quote "$node_action")"
  assert_remote_deployment_lock "$agent" "$deploy_generation"
  local_secret_payload="$TMPDIR_LOCAL/node-secrets-${agent}.env"
  printf '%s\n' "${remote_secret_env[@]}" > "$local_secret_payload"
  chmod 0600 "$local_secret_payload"
  fenced_remote_cmd="$(remote_deployment_fenced_exec "$deploy_generation" 1 sh -c "$remote_cmd")"
  if stream_file_after_remote_fence "$local_secret_payload" \
    "MAC_DEPLOY_FENCE_READY:${deploy_generation}" \
    ssh -A -o BatchMode=yes -o ConnectTimeout=10 \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
      "${ssh_args[@]}" "$ssh_target" "$fenced_remote_cmd"
  then
    rm -f "$local_secret_payload"
    if [ "$node_action" = arm-phase2 ] || [ "$node_action" = finalize ]; then
      echo "==> ${agent}: typed phase-2 ${node_action} completed"
      return 0
    fi
    echo "==> ${agent}: validating remote post-deploy manifest"
    if ! reconcile_remote_deploy "$agent" "$target" "$openshell_disable_requested"; then
      echo "==> ${agent}: remote deploy returned success but post manifest validation failed" >&2
      return 1
    fi
  else
    rm -f "$local_secret_payload"
    if [ "$node_action" = arm-phase2 ] || [ "$node_action" = finalize ]; then
      echo "==> ${agent}: typed phase-2 ${node_action} failed" >&2
      return 1
    fi
    echo "==> ${agent}: ssh exited non-zero; reconciling remote deploy state"
    reconcile_remote_deploy "$agent" "$target" "$openshell_disable_requested"
  fi
  if [ "$node_action" = arm-phase2 ] || [ "$node_action" = finalize ]; then
    return 0
  elif [ "$openshell_enabled" = "1" ]; then
    echo "==> ${agent}: restarting mac-agent after in-transaction OpenShell and OpenClaw validation"
  else
    echo "==> ${agent}: restarting mac-agent after post-manifest reconciliation"
  fi
  if [ "$node_action" = apply-phase2 ]; then
    restart_remote_mac_agent_under_epoch "$agent" "$supervisor" "$fleet_name"
  else
    set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" restart keep
  fi
}

restart_remote_mac_agent_under_epoch() {
  # Typed epoch ownership lives entirely at the hub. Restart only the exact
  # selected supervisor unit under the deployment lock; never call the legacy
  # per-node hold/release gate from inside an open hub transaction.
  local agent="$1" supervisor="$2" fleet_name="$3" deployment_id command
  local ssh_parts=() ssh_args=() ssh_target item last_index
  deployment_id="$(deployment_id_for_agent "$agent")"
  assert_remote_deployment_lock "$agent" "$deployment_id"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1)); ssh_target="${ssh_parts[$last_index]}"; ssh_args=("${ssh_parts[@]:0:$last_index}")
  case "$supervisor" in
    systemd)
      command="if [ \"\$(id -u)\" -eq 0 ]; then systemctl restart $(shell_quote "${fleet_name}-agent.service"); else sudo -n systemctl restart $(shell_quote "${fleet_name}-agent.service"); fi"
      ;;
    launchd)
      command="if [ \"\$(id -u)\" -eq 0 ]; then launchctl kickstart -k $(shell_quote "system/com.${fleet_name}.agent"); else sudo -n launchctl kickstart -k $(shell_quote "system/com.${fleet_name}.agent"); fi"
      ;;
    supervisord)
      command="if [ \"\$(id -u)\" -eq 0 ]; then supervisorctl restart $(shell_quote "${fleet_name}-agent"); else sudo -n supervisorctl restart $(shell_quote "${fleet_name}-agent"); fi"
      ;;
    *) echo "ERROR: ${agent}: unsupported typed supervisor ${supervisor}" >&2; return 1 ;;
  esac
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c "set -e; $command")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
    "${ssh_args[@]}" "$ssh_target" "$command"
}

hub_target() {
  fleet_hub_target
}

upsert_local_env() {
  local key="$1" value="$2"
  local env_file="${MAC_DEPLOY_ENV_FILE:-$HOME/.mac/.env}"
  [ -f "$env_file" ] || return 0
  local escaped
  escaped="$(printf '%s' "$value" | sed 's/[&/\]/\\&/g')"
  if grep -q "^${key}=" "$env_file" 2>/dev/null; then
    sed -i.bak "s|^${key}=.*|${key}=${escaped}|" "$env_file" && rm -f "${env_file}.bak"
  else
    printf '%s=%s\n' "$key" "$value" >> "$env_file"
  fi
  chmod 600 "$env_file"
}

# Handoff file is written atomically with 0o600 mode via tempfile-then-replace so
# credentials stored in the JSON are never visible to other users or in a
# partially-written state.  See also: write_owner_only_file() in fleet_deploy.py.
write_ide_handoff_file() {
  local api_url="$1" token="$2" hub_agent="$3" fleet_name="$4"
  local handoff_file="${MAC_DEPLOY_IDE_HANDOFF_FILE:-$HOME/.mac/fleet-ide-handoff.json}"
  "$PYTHON_BIN" -c '
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

path = Path(sys.argv[1]).expanduser()
api_url = sys.argv[2].strip().rstrip("/")
hub_agent = sys.argv[3].strip()
fleet_name = sys.argv[4].strip()
token = os.fdopen(3, "r", encoding="utf-8").read()
if token.endswith("\n"):
    token = token[:-1]
parsed = urlsplit(api_url)
if parsed.scheme not in {"http", "https"} or not parsed.hostname:
    raise SystemExit("invalid Fleet IDE handoff URL")
if parsed.username or parsed.password or parsed.query or parsed.fragment:
    raise SystemExit("Fleet IDE handoff URL must not contain credentials, query, or fragment")
if token != token.strip() or not token or any(ord(ch) < 32 or ch.isspace() for ch in token):
    raise SystemExit("invalid Fleet IDE handoff token")
path.parent.mkdir(parents=True, exist_ok=True)
try:
    path.parent.chmod(0o700)
except OSError:
    pass
payload = {
    "schema": "mac.ide_handoff.v1",
    "api_url": api_url,
    "token": token,
    "hub_port": 8789,
    "source": "fleet-deploy",
    "hub_agent": hub_agent,
    "fleet": fleet_name,
    "created_at": datetime.now(timezone.utc).isoformat(),
}
fd, tmp_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
tmp = Path(tmp_name)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
finally:
    if tmp.exists():
        tmp.unlink()
print(path)
' "$handoff_file" "$api_url" "$hub_agent" "$fleet_name" 3<<<"$token"
}

read_hub_token() {
  local target hub_agent ssh_parts=() ssh_args=() ssh_target item last_index
  target="$(hub_target)"
  hub_agent="$(fleet_hub_agent)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" \
    'set -euo pipefail; set -a; . "$HOME/.mac/mac.env"; set +a; printf "%s" "${MAC_API_TOKEN:?}"'
}

read_hub_tunnel_pubkey() {
  local target hub_agent ssh_parts=() ssh_args=() ssh_target item last_index
  target="$(hub_target)"
  hub_agent="$(fleet_hub_agent)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" \
    "cat \"\$HOME/.ssh/mac_tunnel_id.pub\" 2>/dev/null || true"
}

ensure_local_github_review_key() {
  local key_dir="$HOME/.mac/keys"
  local key_file="$key_dir/mac-github-review-id"
  mkdir -p "$key_dir"
  chmod 700 "$key_dir"
  if [ ! -f "$key_file" ]; then
    echo "==> generating GitHub read-only deploy key at $key_file" >&2
    ssh-keygen -t ed25519 -f "$key_file" -N "" -C "mac-github-review-deploy-key" -q
    echo "==> Add this public key as a read-only deploy key to your GitHub repos:" >&2
    cat "${key_file}.pub" >&2
  fi
  chmod 600 "$key_file"
  "$PYTHON_BIN" -c "import base64, sys; print(base64.b64encode(open(sys.argv[1],'rb').read()).decode(), end='')" "$key_file"
}

install_reverse_tunnel_on_hub() {
  local worker_agent="$1" worker_target="$2" hub_agent="$3" hub_target_str="$4" fleet_name_arg="${5:-mac}"
  local ssh_parts=() ssh_args=() ssh_target item last_index tunnel_host fleet_name_local
  local hub_deployment_id hub_lock_temporary=0 hub_script_status=0 fence_exec cleanup_fence
  local launchd_lifecycle remote_launchd_lifecycle
  fleet_name_local="${fleet_name_arg:-mac}"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  # The fleet registry target is the contract. Do not consult ambient
  # ~/.ssh/config here: the hub cannot reproduce aliases from the operator's
  # machine. Inline ports are operator-to-worker routing metadata and are not
  # part of the address used by the hub-originated reverse tunnel.
  tunnel_host="${worker_target#*@}"
  case "$tunnel_host" in
    *:[0-9]*) tunnel_host="${tunnel_host%:*}" ;;
  esac
  if [[ "$worker_target" == *@* ]]; then
    tunnel_user="${worker_target%@*}"
  else
    tunnel_user="horde"
  fi
  # A spoke-only invocation does not otherwise own the hub's deployment lock.
  # Acquire it for this deterministic manager-config transaction; if the hub is
  # part of the selected cohort, reuse the exact phase-1 owner instead.  The
  # whole script executes while remote_deployment_fenced_exec holds the hub's
  # stable fcntl guard, so concurrent fleet controllers cannot interleave or
  # overwrite tunnel manager state.
  hub_deployment_id="$(deployment_id_for_agent "$hub_agent")"
  if ! assert_remote_deployment_lock "$hub_agent" "$hub_deployment_id" \
    >/dev/null 2>&1; then
    acquire_remote_deployment_lock "$hub_agent" "$hub_deployment_id"
    hub_lock_temporary=1
  fi
  launchd_lifecycle="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/launchd-lifecycle.sh"
  remote_launchd_lifecycle="/tmp/mac-hub-launchd-lifecycle-${worker_agent}-${DEPLOY_CONTROLLER_NONCE}.sh"
  if ! fenced_remote_upload \
    "$hub_agent" "$hub_deployment_id" \
    "$launchd_lifecycle" "$remote_launchd_lifecycle"; then
    if [ "$hub_lock_temporary" = 1 ]; then
      release_remote_deployment_lock "$hub_agent" "$hub_deployment_id" || true
    fi
    return 1
  fi
  fence_exec="$(remote_deployment_fenced_exec "$hub_deployment_id" 0 bash -s)"
  # Pass values to the remote inline; quoting handled by shell_quote.
  if ssh -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    "TUNNEL_WORKER_AGENT=$(shell_quote "$worker_agent") TUNNEL_HOST=$(shell_quote "$tunnel_host") TUNNEL_USER=$(shell_quote "$tunnel_user") TUNNEL_FLEET_NAME=$(shell_quote "$fleet_name_local") TUNNEL_LAUNCHD_LIFECYCLE=$(shell_quote "$remote_launchd_lifecycle") $fence_exec" <<'HUBSCRIPT'
set -euo pipefail
worker_agent="${TUNNEL_WORKER_AGENT:?}"
tunnel_host="${TUNNEL_HOST:?}"
tunnel_user="${TUNNEL_USER:-horde}"
fleet_name="${TUNNEL_FLEET_NAME:-mac}"
managed_marker="mac.managed-reverse-tunnel.v1:${fleet_name}:${worker_agent}"
definition_tmp=""

run_root_noninteractive() {
  if [ "$(type -t linux_root_bounded 2>/dev/null || true)" = function ]; then
    linux_root_bounded "$@"
  elif [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

cleanup_definition_tmp() {
  if [ -n "$definition_tmp" ]; then
    run_root_noninteractive rm -f "$definition_tmp" >/dev/null 2>&1 || true
  fi
}
trap cleanup_definition_tmp EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

legacy_managed_definition() {
  local kind="$1" path="$2" program_source=""
  program_source="$(cat <<'PY'
import plistlib
import re
import shlex
import sys
from pathlib import Path

kind, raw_path, fleet, worker, home, user, ssh_bin = sys.argv[1:]
path = Path(raw_path)
label = f"com.{fleet}.tunnel-{worker}"
program = f"{fleet}-tunnel-{worker}"
forwards = [
    "127.0.0.1:18789:127.0.0.1:8789",
    "127.0.0.1:18090:127.0.0.1:8090",
    "127.0.0.1:16333:127.0.0.1:6333",
    "127.0.0.1:13002:127.0.0.1:3002",
]


def ssh_arguments(binary):
    result = [
        binary,
        "-N",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-i", f"{home}/.ssh/mac_tunnel_id",
    ]
    for forward in forwards:
        result.extend(["-R", forward])
    return result


def exact_legacy_ssh(args, binary):
    expected = ssh_arguments(binary)
    return (
        args[:-1] == expected
        and len(args) == len(expected) + 1
        and re.fullmatch(r"[^@\s]+@[^\s]+", args[-1] or "") is not None
    )


try:
    raw = path.read_bytes()
    if kind == "launchd":
        data = plistlib.loads(raw)
        ok = (
            data.get("Label") == label
            and data.get("UserName") == user
            and data.get("EnvironmentVariables") == {"HOME": home}
            and exact_legacy_ssh(data.get("ProgramArguments") or [], ssh_bin)
            and data.get("KeepAlive") is True
            and data.get("RunAtLoad") is True
            and data.get("ThrottleInterval") == 5
            and data.get("StandardOutPath") == f"{home}/.mac/logs/tunnel-{worker}.log"
            and data.get("StandardErrorPath") == f"{home}/.mac/logs/tunnel-{worker}.log"
        )
    elif kind == "systemd":
        lines = [
            line.strip()
            for line in raw.decode().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        exec_lines = [line for line in lines if line.startswith("ExecStart=")]
        args = shlex.split(exec_lines[0].split("=", 1)[1]) if len(exec_lines) == 1 else []
        expected_lines = {
            "[Unit]",
            f"Description=mac reverse tunnel for {worker}",
            "After=network-online.target",
            "Wants=network-online.target",
            "[Service]",
            "Type=simple",
            f"User={user}",
            f"WorkingDirectory={home}",
            "Restart=always",
            "RestartSec=5",
            "[Install]",
            "WantedBy=multi-user.target",
        }
        ok = (
            exact_legacy_ssh(args, ssh_bin)
            and set(lines) == expected_lines | set(exec_lines)
        )
    elif kind == "supervisor":
        lines = [
            line.strip()
            for line in raw.decode().splitlines()
            if line.strip() and not line.lstrip().startswith(";")
        ]
        command_lines = [line for line in lines if line.startswith("command=")]
        args = shlex.split(command_lines[0].split("=", 1)[1]) if len(command_lines) == 1 else []
        expected_lines = {
            f"[program:{program}]",
            f"directory={home}",
            f"user={user}",
            "autostart=true",
            "autorestart=true",
            "startsecs=5",
            "startretries=1000",
            "stopwaitsecs=10",
            f"stdout_logfile={home}/.mac/logs/tunnel-{worker}.log",
            f"stderr_logfile={home}/.mac/logs/tunnel-{worker}.log",
        }
        ok = exact_legacy_ssh(args, "ssh") and set(lines) == expected_lines | set(command_lines)
    else:
        ok = False
except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
    ok = False
raise SystemExit(0 if ok else 1)
PY
)"
  run_root_noninteractive python3 -c "$program_source" \
    "$kind" "$path" "$fleet_name" "$worker_agent" "$HOME" \
    "$(whoami)" "$(command -v ssh)"
}

assert_managed_definition_or_absent() {
  local path="$1" marker_line="$2" kind="$3"
  if run_root_noninteractive test -L "$path"; then
    echo "refusing symlink reverse-tunnel manager definition: $path" >&2
    exit 1
  fi
  if run_root_noninteractive test -e "$path" \
    && ! run_root_noninteractive grep -Fqx "$marker_line" "$path"; then
    if ! legacy_managed_definition "$kind" "$path"; then
      echo "refusing to replace unowned reverse-tunnel manager definition: $path" >&2
      exit 1
    fi
  fi
}

stage_managed_definition() {
  local path="$1"
  definition_tmp="$(run_root_noninteractive mktemp "${path}.mac-deploy.XXXXXX")"
}

install_staged_definition() {
  local path="$1" marker_line="$2" kind="$3"
  # Recheck immediately before the atomic replace. An operator definition that
  # appeared after preflight must remain byte-identical.
  assert_managed_definition_or_absent "$path" "$marker_line" "$kind"
  run_root_noninteractive mv -f "$definition_tmp" "$path"
  definition_tmp=""
}

if [ "$(uname -s)" = "Darwin" ]; then
  # macOS hub: run the reverse tunnel as a system LaunchDaemon (headless, no GUI
  # session required — mirrors com.mac.control-plane). macOS has neither systemd
  # nor supervisord, so without this branch the deploy died writing
  # /etc/supervisor/conf.d on the Mac hub and no GKE pod could be provisioned.
  ssh_bin="$(command -v ssh)"
  label="com.${fleet_name}.tunnel-${worker_agent}"
  plist="/Library/LaunchDaemons/${label}.plist"
  marker_line="<!-- ${managed_marker} -->"
  hub_user="$(whoami)"
  launchd_lifecycle="${TUNNEL_LAUNCHD_LIFECYCLE:?}"
  if [ ! -f "$launchd_lifecycle" ] || [ -L "$launchd_lifecycle" ]; then
    echo "bounded launchd lifecycle contract is unavailable: $launchd_lifecycle" >&2
    exit 1
  fi
  # shellcheck disable=SC1090 -- uploaded under the hub deployment fence from
  # the same reviewed controller revision as this transaction.
  . "$launchd_lifecycle"
  MAC_LAUNCHD_LOG_PREFIX="[reverse-tunnel:${label}]"
  assert_managed_definition_or_absent "$plist" "$marker_line" launchd
  launchd_loaded_legacy=0
  launchd_file_legacy=0
  if sudo -n test -e "$plist" && ! sudo -n grep -Fqx "$marker_line" "$plist"; then
    legacy_managed_definition launchd "$plist"
    launchd_file_legacy=1
  fi
  launchd_state_has_managed_identity() {
    local mode="$1" state="$2"
    python3 - "$mode" "$plist" "$label" "$managed_marker" "$ssh_bin" "$HOME" \
      3<<<"$state" <<'PY'
import re
import sys

mode, plist, label, marker, ssh_bin, home = sys.argv[1:]
text = open(3, encoding="utf-8").read()
lines = text.splitlines()


def one(prefix):
    values = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    return values[0] if len(values) == 1 else None


if not lines or lines[0] != f"system/{label} = {{":
    raise SystemExit(1)
if one("\tpath = ") != plist or one("\tprogram = ") != ssh_bin:
    raise SystemExit(1)
try:
    start = lines.index("\targuments = {")
    end = lines.index("\t}", start + 1)
except ValueError:
    raise SystemExit(1)
argument_lines = lines[start + 1:end]
if not argument_lines or any(not line.startswith("\t\t") for line in argument_lines):
    raise SystemExit(1)
args = [line[2:] for line in argument_lines]
expected = [
    ssh_bin,
    "-N",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "ExitOnForwardFailure=yes",
    "-i", f"{home}/.ssh/mac_tunnel_id",
    "-R", "127.0.0.1:18789:127.0.0.1:8789",
    "-R", "127.0.0.1:18090:127.0.0.1:8090",
    "-R", "127.0.0.1:16333:127.0.0.1:6333",
    "-R", "127.0.0.1:13002:127.0.0.1:3002",
]
if (
    args[:-1] != expected
    or len(args) != len(expected) + 1
    or re.fullmatch(r"[^@\s]+@[^\s]+", args[-1] or "") is None
):
    raise SystemExit(1)
marker_line = f"\t\tMAC_MANAGED_REVERSE_TUNNEL => {marker}"
has_marker = lines.count(marker_line) == 1
if mode == "managed" and not has_marker:
    raise SystemExit(1)
if mode == "legacy" and has_marker:
    raise SystemExit(1)
PY
  }
  load_launchd_job_state() {
    local target="$1" display_label="$2" output="" rc=0
    output="$(mac_launchd_run_control_bounded system \
      "${MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS:-10}" \
      print "$target" 2>&1)" || rc=$?
    launchd_state="$output"
    if [ "$rc" -eq 0 ]; then
      return 0
    fi
    if [ "$rc" -eq 113 ]; then
      case "$output" in
        *"Could not find service"*) return 1 ;;
      esac
    fi
    echo "could not inspect reverse-tunnel launchd job $display_label (exit $rc): $output" >&2
    return 2
  }
  launchd_state=""
  launchd_probe_rc=0
  load_launchd_job_state "system/${label}" "$label" || launchd_probe_rc=$?
  case "$launchd_probe_rc" in
    0)
      if launchd_state_has_managed_identity managed "$launchd_state"; then
        :
      elif launchd_state_has_managed_identity legacy "$launchd_state"; then
        # Either this is the first exact-template migration or the previous run
        # atomically installed the marked plist and was interrupted before the
        # one-time legacy job bootout. Both are safe, retryable adoption states.
        [ "$launchd_file_legacy" = 1 ] || sudo -n grep -Fqx "$marker_line" "$plist"
        launchd_loaded_legacy=1
      else
        echo "refusing to bootout same-name launchd job without exact MAC identity" >&2
        exit 1
      fi
      ;;
    1) ;;
    *) exit "$launchd_probe_rc" ;;
  esac
  mkdir -p "$HOME/.mac/logs"
  mac_launchd_transaction_begin \
    system "$plist" "system/${label}" "$label" system
  definition_tmp="$(mktemp "$HOME/.mac/logs/${label}.plist.mac-deploy.XXXXXX")"
  mac_launchd_transaction_track_temporary "$definition_tmp"
  cat > "$definition_tmp" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
${marker_line}
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>UserName</key><string>${hub_user}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>${HOME}</string>
    <key>MAC_MANAGED_REVERSE_TUNNEL</key><string>${managed_marker}</string>
  </dict>
  <key>ProgramArguments</key>
  <array>
    <string>${ssh_bin}</string>
    <string>-N</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>StrictHostKeyChecking=no</string>
    <string>-o</string><string>UserKnownHostsFile=/dev/null</string>
    <string>-o</string><string>ServerAliveInterval=30</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-i</string><string>${HOME}/.ssh/mac_tunnel_id</string>
    <string>-R</string><string>127.0.0.1:18789:127.0.0.1:8789</string>
    <string>-R</string><string>127.0.0.1:18090:127.0.0.1:8090</string>
    <string>-R</string><string>127.0.0.1:16333:127.0.0.1:6333</string>
    <string>-R</string><string>127.0.0.1:13002:127.0.0.1:3002</string>
    <string>${tunnel_user}@${tunnel_host}</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>${HOME}/.mac/logs/tunnel-${worker_agent}.log</string>
  <key>StandardErrorPath</key><string>${HOME}/.mac/logs/tunnel-${worker_agent}.log</string>
</dict>
</plist>
PLIST
  chmod 0600 "$definition_tmp"
  mac_run_bounded "${MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS:-10}" \
    plutil -lint "$definition_tmp" >/dev/null
  # Re-evaluate the loaded label immediately before its one permitted stop.
  # Absence is intentional on first install; every present job must prove both
  # canonical path and durable in-job marker before the transaction mutates it.
  launchd_probe_rc=0
  load_launchd_job_state "system/${label}" "$label" || launchd_probe_rc=$?
  case "$launchd_probe_rc" in
    0)
      if ! launchd_state_has_managed_identity managed "$launchd_state"; then
        [ "$launchd_loaded_legacy" = 1 ]
        launchd_state_has_managed_identity legacy "$launchd_state"
      fi
      ;;
    1) ;;
    *) exit "$launchd_probe_rc" ;;
  esac
  mac_launchd_transaction_mark_mutating
  mac_launchd_stop_job_if_present "system/${label}" "$label" system
  # Recheck ownership after the stop and before the atomic root-owned replace.
  # A failure from here onward invokes the transaction's EXIT compensation,
  # restoring both the previous plist bytes and its exact active/inactive state.
  assert_managed_definition_or_absent "$plist" "$marker_line" launchd
  mac_launchd_transaction_replace "$definition_tmp" "$plist" 0644 0 0
  mac_launchd_bootstrap_job system "$plist" "system/${label}" "$label" system
  launchd_probe_rc=0
  load_launchd_job_state "system/${label}" "$label" || launchd_probe_rc=$?
  [ "$launchd_probe_rc" -eq 0 ]
  launchd_state_has_managed_identity managed "$launchd_state"
  mac_launchd_transaction_commit
  definition_tmp=""
  exit 0
fi

# Linux reverse-tunnel managers use the same bounded, fsyncing artifact
# primitives as launchd, but their runtime state is restored by a manager-
# specific compensation hook.  Source this only after the Darwin branch so the
# launchd transaction above remains the sole owner of its trap state.
linux_lifecycle="${TUNNEL_LAUNCHD_LIFECYCLE:?}"
if [ ! -f "$linux_lifecycle" ] || [ -L "$linux_lifecycle" ]; then
  echo "bounded service lifecycle contract is unavailable: $linux_lifecycle" >&2
  exit 1
fi
# shellcheck disable=SC1090 -- uploaded under the hub deployment fence from the
# same reviewed controller revision as this transaction.
. "$linux_lifecycle"
MAC_LAUNCHD_LOG_PREFIX="[reverse-tunnel:${worker_agent}]"

linux_root_mode=user
if [ "$(id -u)" -ne 0 ]; then
  linux_root_mode=system
  mac_run_bounded "${MAC_LINUX_MANAGER_COMMAND_TIMEOUT_SECONDS:-10}" \
    sudo -n true >/dev/null
fi

linux_root_bounded() {
  local timeout="${MAC_LINUX_MANAGER_COMMAND_TIMEOUT_SECONDS:-10}"
  if [ "$linux_root_mode" = system ]; then
    mac_run_bounded "$timeout" sudo -n "$@"
  else
    mac_run_bounded "$timeout" "$@"
  fi
}

# A one-artifact transaction used by both systemd and supervisord.  Manager
# state is deliberately not generalized: the rollback hook for each manager
# proves its own exact identity and restores its captured active/inactive
# intent.  The transaction only owns durable bytes, signal-safe compensation,
# and trap chaining back to cleanup_definition_tmp.
MAC_LINUX_SERVICE_TX_ACTIVE=0
MAC_LINUX_SERVICE_TX_MUTATING=0
MAC_LINUX_SERVICE_TX_DIR=""
MAC_LINUX_SERVICE_TX_PATH=""
MAC_LINUX_SERVICE_TX_BACKUP=""
MAC_LINUX_SERVICE_TX_EXISTED=""
MAC_LINUX_SERVICE_TX_LABEL=""
MAC_LINUX_SERVICE_TX_ROLLBACK_HOOK=""
MAC_LINUX_SERVICE_TX_SAVED_EXIT_TRAP=""
MAC_LINUX_SERVICE_TX_SAVED_HUP_TRAP=""
MAC_LINUX_SERVICE_TX_SAVED_INT_TRAP=""
MAC_LINUX_SERVICE_TX_SAVED_TERM_TRAP=""

linux_service_tx_restore_trap_definition() {
  local definition="$1"
  [ -z "$definition" ] || builtin source /dev/stdin <<<"$definition"
}

linux_service_tx_run_saved_exit_trap() {
  local definition="$1" return_definition="" trap_rc=0
  [ -n "$definition" ] || return 0
  case "$definition" in
    "trap -- "*" EXIT") ;;
    *)
      echo "saved Linux service EXIT trap has an unexpected definition" >&2
      return 1
      ;;
  esac
  return_definition="${definition% EXIT} RETURN"
  trap - RETURN
  builtin source /dev/stdin <<<"$return_definition" || trap_rc=$?
  trap - RETURN
  return "$trap_rc"
}

linux_service_tx_restore_traps() {
  local exit_trap="$MAC_LINUX_SERVICE_TX_SAVED_EXIT_TRAP"
  local hup_trap="$MAC_LINUX_SERVICE_TX_SAVED_HUP_TRAP"
  local int_trap="$MAC_LINUX_SERVICE_TX_SAVED_INT_TRAP"
  local term_trap="$MAC_LINUX_SERVICE_TX_SAVED_TERM_TRAP"
  trap - EXIT HUP INT TERM
  MAC_LINUX_SERVICE_TX_SAVED_EXIT_TRAP=""
  MAC_LINUX_SERVICE_TX_SAVED_HUP_TRAP=""
  MAC_LINUX_SERVICE_TX_SAVED_INT_TRAP=""
  MAC_LINUX_SERVICE_TX_SAVED_TERM_TRAP=""
  linux_service_tx_restore_trap_definition "$exit_trap"
  linux_service_tx_restore_trap_definition "$hup_trap"
  linux_service_tx_restore_trap_definition "$int_trap"
  linux_service_tx_restore_trap_definition "$term_trap"
}

linux_service_tx_cleanup() {
  local cleanup_rc=0
  if [ -n "$MAC_LINUX_SERVICE_TX_DIR" ]; then
    mac_launchd_cleanup_transaction_artifacts \
      "$linux_root_mode" "$MAC_LINUX_SERVICE_TX_DIR" || cleanup_rc=$?
  fi
  MAC_LINUX_SERVICE_TX_DIR=""
  return "$cleanup_rc"
}

linux_service_tx_begin() {
  local path="$1" label="$2" rollback_hook="$3" begin_rc=0
  [ "$MAC_LINUX_SERVICE_TX_ACTIVE" -eq 0 ] || {
    echo "nested Linux service transactions are not supported" >&2
    return 1
  }
  [ "$(type -t "$rollback_hook" 2>/dev/null || true)" = function ] || {
    echo "Linux service rollback hook is not a function: $rollback_hook" >&2
    return 1
  }
  MAC_LINUX_SERVICE_TX_SAVED_EXIT_TRAP="$(trap -p EXIT)"
  MAC_LINUX_SERVICE_TX_SAVED_HUP_TRAP="$(trap -p HUP)"
  MAC_LINUX_SERVICE_TX_SAVED_INT_TRAP="$(trap -p INT)"
  MAC_LINUX_SERVICE_TX_SAVED_TERM_TRAP="$(trap -p TERM)"
  MAC_LINUX_SERVICE_TX_DIR="$(mac_launchd_create_transaction_directory \
    "$(dirname "$path")" "$label" "$linux_root_mode")" || begin_rc=$?
  if [ "$begin_rc" -ne 0 ]; then
    linux_service_tx_restore_traps
    return "$begin_rc"
  fi
  MAC_LINUX_SERVICE_TX_PATH="$path"
  MAC_LINUX_SERVICE_TX_BACKUP="$MAC_LINUX_SERVICE_TX_DIR/prior"
  MAC_LINUX_SERVICE_TX_EXISTED="$(mac_launchd_snapshot_file \
    "$path" "$MAC_LINUX_SERVICE_TX_BACKUP" "$linux_root_mode")" || begin_rc=$?
  case "$MAC_LINUX_SERVICE_TX_EXISTED:$begin_rc" in
    0:0|1:0) ;;
    *)
      linux_service_tx_cleanup || true
      linux_service_tx_restore_traps
      return 1
      ;;
  esac
  MAC_LINUX_SERVICE_TX_ACTIVE=1
  MAC_LINUX_SERVICE_TX_MUTATING=0
  MAC_LINUX_SERVICE_TX_LABEL="$label"
  MAC_LINUX_SERVICE_TX_ROLLBACK_HOOK="$rollback_hook"
  trap 'linux_service_tx_on_exit "$?"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

linux_service_tx_mark_mutating() {
  [ "$MAC_LINUX_SERVICE_TX_ACTIVE" -eq 1 ] || return 1
  MAC_LINUX_SERVICE_TX_MUTATING=1
}

linux_service_tx_replace() {
  local staged="$1" destination="$2"
  [ "$MAC_LINUX_SERVICE_TX_ACTIVE" -eq 1 ] || return 1
  [ "$destination" = "$MAC_LINUX_SERVICE_TX_PATH" ] || {
    echo "refusing to replace untracked Linux service artifact: $destination" >&2
    return 1
  }
  mac_launchd_atomic_replace \
    "$staged" "$destination" "$linux_root_mode" 0644 0 0
}

linux_service_tx_restore_artifact() {
  if [ "$MAC_LINUX_SERVICE_TX_EXISTED" = 1 ]; then
    mac_launchd_atomic_restore \
      "$MAC_LINUX_SERVICE_TX_BACKUP" "$MAC_LINUX_SERVICE_TX_PATH" \
      "$linux_root_mode"
  else
    mac_launchd_remove_file_and_fsync \
      "$MAC_LINUX_SERVICE_TX_PATH" "$linux_root_mode"
  fi
}

linux_service_tx_verify_snapshot_current() {
  if [ "$MAC_LINUX_SERVICE_TX_EXISTED" = 1 ]; then
    linux_root_bounded cmp -s \
      "$MAC_LINUX_SERVICE_TX_BACKUP" "$MAC_LINUX_SERVICE_TX_PATH"
  elif linux_root_bounded test -e "$MAC_LINUX_SERVICE_TX_PATH" >/dev/null 2>&1; then
    echo "Linux service artifact appeared after pre-state capture: $MAC_LINUX_SERVICE_TX_PATH" >&2
    return 1
  fi
}

linux_service_tx_rollback() {
  local rollback_rc=0 hook_rc=0 cleanup_rc=0
  [ "$MAC_LINUX_SERVICE_TX_ACTIVE" -eq 1 ] || return 0
  trap - EXIT
  trap '' HUP INT TERM
  MAC_LINUX_SERVICE_TX_ACTIVE=0
  if [ "$MAC_LINUX_SERVICE_TX_MUTATING" -eq 1 ]; then
    "$MAC_LINUX_SERVICE_TX_ROLLBACK_HOOK" || hook_rc=$?
    [ "$hook_rc" -eq 0 ] || rollback_rc=1
  fi
  linux_service_tx_cleanup || cleanup_rc=$?
  [ "$cleanup_rc" -eq 0 ] || rollback_rc=1
  linux_service_tx_restore_traps
  if [ "$rollback_rc" -eq 0 ]; then
    echo "restored prior Linux service generation: $MAC_LINUX_SERVICE_TX_LABEL" >&2
  else
    echo "could not completely restore prior Linux service generation: $MAC_LINUX_SERVICE_TX_LABEL" >&2
  fi
  return "$rollback_rc"
}

linux_service_tx_on_exit() {
  local original_rc="$1" rollback_rc=0 chained_rc=0
  local saved_exit_trap="$MAC_LINUX_SERVICE_TX_SAVED_EXIT_TRAP"
  trap - EXIT HUP INT TERM
  if [ "$MAC_LINUX_SERVICE_TX_ACTIVE" -eq 1 ]; then
    linux_service_tx_rollback || rollback_rc=$?
    if [ "$original_rc" -eq 0 ] || [ "$rollback_rc" -ne 0 ]; then
      original_rc=1
    fi
  fi
  # Bash does not re-enter a newly restored EXIT trap while it is already
  # processing one. Invoke the saved definition exactly once through a
  # source-scoped RETURN trap, preserving the caller's function/global context
  # without eval, then clear the parent copy.
  trap - EXIT HUP INT TERM
  if [ -n "$saved_exit_trap" ]; then
    linux_service_tx_run_saved_exit_trap "$saved_exit_trap" \
      || chained_rc=$?
    if [ "$original_rc" -eq 0 ] && [ "$chained_rc" -ne 0 ]; then
      original_rc="$chained_rc"
    fi
  fi
  exit "$original_rc"
}

linux_service_tx_commit() {
  local cleanup_rc=0
  [ "$MAC_LINUX_SERVICE_TX_ACTIVE" -eq 1 ] || return 1
  trap - EXIT
  trap '' HUP INT TERM
  MAC_LINUX_SERVICE_TX_ACTIVE=0
  linux_service_tx_cleanup || cleanup_rc=$?
  linux_service_tx_restore_traps
  return "$cleanup_rc"
}

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  service="${fleet_name}-tunnel-${worker_agent}.service"
  unit="/etc/systemd/system/${service}"
  marker_line="# ${managed_marker}"
  ssh_bin="$(command -v ssh)"

  systemd_control() {
    linux_root_bounded systemctl "$@"
  }

  systemd_property() {
    local property="$1" output="" rc=0
    output="$(systemd_control show --property="$property" --value -- "$service" 2>&1)" || rc=$?
    if [ "$rc" -ne 0 ]; then
      echo "could not inspect systemd $property for exact tunnel $service (rc=$rc): $output" >&2
      return 1
    fi
    case "$output" in
      *$'\n'*)
        echo "systemd returned ambiguous $property for exact tunnel $service" >&2
        return 1
        ;;
    esac
    printf '%s\n' "$output"
  }

  systemd_inspect() {
    SYSTEMD_LOAD_STATE=""
    SYSTEMD_ACTIVE_STATE=""
    SYSTEMD_SUB_STATE=""
    SYSTEMD_MAIN_PID=""
    SYSTEMD_LOAD_STATE="$(systemd_property LoadState)" || return $?
    SYSTEMD_ACTIVE_STATE="$(systemd_property ActiveState)" || return $?
    SYSTEMD_SUB_STATE="$(systemd_property SubState)" || return $?
    SYSTEMD_MAIN_PID="$(systemd_property MainPID)" || return $?
    case "$SYSTEMD_MAIN_PID" in
      ''|*[!0-9]*)
        echo "systemd returned malformed MainPID for exact tunnel $service" >&2
        return 1
        ;;
    esac
  }

  systemd_validate_identity() {
    local marker_mode="${1:-managed}"
    local fragment="" dropins="" restart="" effective_exec="" rendered="" needle=""
    systemd_inspect || return $?
    [ "$SYSTEMD_LOAD_STATE" = loaded ] || {
      echo "exact systemd tunnel is not loaded: $service" >&2
      return 1
    }
    fragment="$(systemd_property FragmentPath)" || return $?
    [ "$fragment" = "$unit" ] || {
      echo "refusing same-name systemd tunnel loaded from another definition: $fragment" >&2
      return 1
    }
    dropins="$(systemd_property DropInPaths)" || return $?
    [ -z "$dropins" ] || {
      echo "refusing managed systemd tunnel with unreviewed drop-ins" >&2
      return 1
    }
    restart="$(systemd_property Restart)" || return $?
    [ "$restart" = always ] || {
      echo "exact systemd tunnel does not carry Restart=always" >&2
      return 1
    }
    effective_exec="$(systemd_property ExecStart)" || return $?
    for needle in \
      "$ssh_bin" \
      "$HOME/.ssh/mac_tunnel_id" \
      "127.0.0.1:18789:127.0.0.1:8789" \
      "127.0.0.1:18090:127.0.0.1:8090" \
      "127.0.0.1:16333:127.0.0.1:6333" \
      "127.0.0.1:13002:127.0.0.1:3002"; do
      case "$effective_exec" in
        *"$needle"*) ;;
        *)
          echo "exact systemd tunnel ExecStart lacks reviewed identity: $needle" >&2
          return 1
          ;;
      esac
    done
    if [ "$marker_mode" = managed ]; then
      rendered="$(systemd_control cat -- "$service" 2>&1)" || return $?
      case "$rendered" in
        *"$managed_marker"*) ;;
        *)
          echo "exact systemd tunnel does not carry the durable ownership marker" >&2
          return 1
          ;;
      esac
    elif [ "$marker_mode" != legacy ]; then
      echo "invalid systemd identity mode: $marker_mode" >&2
      return 1
    fi
  }

  systemd_prove_inactive() {
    systemd_inspect || return $?
    case "$SYSTEMD_LOAD_STATE:$SYSTEMD_ACTIVE_STATE:$SYSTEMD_SUB_STATE:$SYSTEMD_MAIN_PID" in
      loaded:inactive:dead:0|not-found:inactive:dead:0) return 0 ;;
      *)
        echo "systemd tunnel did not become exactly inactive/dead with pid 0: $service" >&2
        return 1
        ;;
    esac
  }

  # The hub installs this service before the spoke authorizes its tunnel key.
  # A live ssh process and systemd's bounded auto-restart state are therefore
  # both healthy post-install outcomes; failed/inactive/unknown states are not.
  systemd_prove_running_or_retrying() {
    systemd_inspect || return $?
    case "$SYSTEMD_LOAD_STATE:$SYSTEMD_ACTIVE_STATE:$SYSTEMD_SUB_STATE:$SYSTEMD_MAIN_PID" in
      loaded:active:running:[1-9]*|loaded:activating:start:[0-9]*|loaded:activating:auto-restart:[0-9]*)
        return 0
        ;;
      *)
        echo "systemd tunnel is neither running nor awaiting key authorization: $service ($SYSTEMD_ACTIVE_STATE/$SYSTEMD_SUB_STATE pid=$SYSTEMD_MAIN_PID)" >&2
        return 1
        ;;
    esac
  }

  systemd_stop_current() {
    local stop_rc=0
    systemd_inspect || return $?
    if [ "$SYSTEMD_LOAD_STATE" = loaded ] \
      && [ "$SYSTEMD_ACTIVE_STATE:$SYSTEMD_SUB_STATE:$SYSTEMD_MAIN_PID" != inactive:dead:0 ]; then
      systemd_control stop -- "$service" >/dev/null 2>&1 || stop_rc=$?
    elif [ "$SYSTEMD_LOAD_STATE" != loaded ] \
      && [ "$SYSTEMD_LOAD_STATE" != not-found ]; then
      echo "refusing to stop systemd tunnel in unknown load state: $SYSTEMD_LOAD_STATE" >&2
      return 1
    fi
    systemd_prove_inactive || return $?
    [ "$stop_rc" -eq 0 ] || {
      echo "systemd stop failed even though the final state was inactive: $service (rc=$stop_rc)" >&2
      return "$stop_rc"
    }
  }

  systemd_restore_previous_generation() {
    local rollback_rc=0 step_rc=0 enabled_state=""
    systemd_stop_current || rollback_rc=1
    # Remove links created for the new generation while its definition still
    # exists. Continue the artifact restore even if this step fails so the
    # rollback reports aggregate failure rather than abandoning old bytes.
    systemd_control disable -- "$service" >/dev/null 2>&1 || rollback_rc=1
    linux_service_tx_restore_artifact || rollback_rc=1
    systemd_control daemon-reload >/dev/null 2>&1 || rollback_rc=1
    case "$SYSTEMD_PRIOR_ENABLEMENT" in
      enabled)
        systemd_control enable -- "$service" >/dev/null 2>&1 || rollback_rc=1
        ;;
      enabled-runtime)
        systemd_control enable --runtime -- "$service" >/dev/null 2>&1 || rollback_rc=1
        ;;
      disabled) ;;
      absent) ;;
      *) rollback_rc=1 ;;
    esac
    if [ "$SYSTEMD_PRIOR_LOAD_STATE" = loaded ]; then
      # The exact prior bytes were restored; accept the reviewed marker or the
      # one exact legacy shape that the preflight already adopted.
      assert_managed_definition_or_absent "$unit" "$marker_line" systemd || rollback_rc=1
      legacy_managed_definition systemd "$unit" || rollback_rc=1
      if [ "$SYSTEMD_PRIOR_ACTIVE_INTENT" = active ]; then
        systemd_control start -- "$service" >/dev/null 2>&1 || rollback_rc=1
        systemd_prove_running_or_retrying || rollback_rc=1
      else
        systemd_stop_current || rollback_rc=1
      fi
      enabled_state="$(systemd_property UnitFileState)" || step_rc=$?
      [ "$step_rc" -eq 0 ] || rollback_rc=1
      [ "$enabled_state" = "$SYSTEMD_PRIOR_ENABLEMENT" ] || rollback_rc=1
    else
      systemd_inspect || rollback_rc=1
      [ "$SYSTEMD_LOAD_STATE:$SYSTEMD_ACTIVE_STATE:$SYSTEMD_SUB_STATE:$SYSTEMD_MAIN_PID" = \
        not-found:inactive:dead:0 ] || rollback_rc=1
      if linux_root_bounded test -e "$unit" >/dev/null 2>&1; then
        rollback_rc=1
      fi
    fi
    return "$rollback_rc"
  }

  assert_managed_definition_or_absent "$unit" "$marker_line" systemd
  systemd_inspect
  SYSTEMD_PRIOR_LOAD_STATE="$SYSTEMD_LOAD_STATE"
  SYSTEMD_PRIOR_ACTIVE_INTENT=""
  SYSTEMD_PRIOR_ENABLEMENT=""
  case "$SYSTEMD_LOAD_STATE" in
    loaded)
      legacy_managed_definition systemd "$unit" || {
        echo "refusing systemd tunnel definition outside the exact MAC shape" >&2
        exit 1
      }
      fragment="$(systemd_property FragmentPath)"
      [ "$fragment" = "$unit" ] || {
        echo "refusing same-name systemd tunnel loaded from another definition: $fragment" >&2
        exit 1
      }
      dropins="$(systemd_property DropInPaths)"
      [ -z "$dropins" ] || {
        echo "refusing managed systemd tunnel with unreviewed drop-ins" >&2
        exit 1
      }
      if linux_root_bounded grep -Fqx "$marker_line" "$unit" >/dev/null 2>&1; then
        systemd_validate_identity managed
      else
        # assert_managed_definition_or_absent already proved the exact legacy
        # bytes; now bind those bytes to the loaded runtime before stopping it.
        systemd_validate_identity legacy
      fi
      case "$SYSTEMD_ACTIVE_STATE:$SYSTEMD_SUB_STATE:$SYSTEMD_MAIN_PID" in
        active:running:[1-9]*|activating:start:[0-9]*|activating:auto-restart:[0-9]*)
          SYSTEMD_PRIOR_ACTIVE_INTENT=active
          ;;
        inactive:dead:0)
          SYSTEMD_PRIOR_ACTIVE_INTENT=inactive
          ;;
        *)
          echo "refusing transitional or failed systemd tunnel pre-state: $SYSTEMD_ACTIVE_STATE/$SYSTEMD_SUB_STATE pid=$SYSTEMD_MAIN_PID" >&2
          exit 1
          ;;
      esac
      SYSTEMD_PRIOR_ENABLEMENT="$(systemd_property UnitFileState)"
      case "$SYSTEMD_PRIOR_ENABLEMENT" in
        enabled|enabled-runtime|disabled) ;;
        *)
          echo "refusing unrepresentable systemd enablement pre-state: $SYSTEMD_PRIOR_ENABLEMENT" >&2
          exit 1
          ;;
      esac
      ;;
    not-found)
      [ "$SYSTEMD_ACTIVE_STATE:$SYSTEMD_SUB_STATE:$SYSTEMD_MAIN_PID" = inactive:dead:0 ] || {
        echo "systemd absent tunnel state is contradictory" >&2
        exit 1
      }
      if linux_root_bounded test -e "$unit" >/dev/null 2>&1; then
        echo "refusing unreconciled systemd definition absent from manager state" >&2
        exit 1
      fi
      SYSTEMD_PRIOR_ACTIVE_INTENT=inactive
      SYSTEMD_PRIOR_ENABLEMENT=absent
      ;;
    *)
      echo "refusing same-name systemd tunnel in unexpected load state: $SYSTEMD_LOAD_STATE" >&2
      exit 1
      ;;
  esac

  linux_service_tx_begin "$unit" "$service" systemd_restore_previous_generation
  mkdir -p "$HOME/.mac/logs"
  definition_tmp="$(mktemp "$HOME/.mac/logs/${service}.mac-deploy.XXXXXX")"
  cat > "$definition_tmp" <<EOF
${marker_line}
[Unit]
Description=mac reverse tunnel for ${worker_agent}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$HOME
ExecStart=${ssh_bin} -N -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -i $HOME/.ssh/mac_tunnel_id -R 127.0.0.1:18789:127.0.0.1:8789 -R 127.0.0.1:18090:127.0.0.1:8090 -R 127.0.0.1:16333:127.0.0.1:6333 -R 127.0.0.1:13002:127.0.0.1:3002 ${tunnel_user}@${tunnel_host}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  chmod 0600 "$definition_tmp"
  linux_service_tx_mark_mutating
  if [ "$SYSTEMD_PRIOR_LOAD_STATE" = loaded ]; then
    systemd_stop_current
  else
    systemd_prove_inactive
  fi
  # Recheck durable ownership after the one permitted stop and immediately
  # before the atomic replacement.
  assert_managed_definition_or_absent "$unit" "$marker_line" systemd
  linux_service_tx_verify_snapshot_current
  linux_service_tx_replace "$definition_tmp" "$unit"
  definition_tmp=""
  systemd_control daemon-reload >/dev/null
  systemd_control enable -- "$service" >/dev/null
  [ "$(systemd_property UnitFileState)" = enabled ]
  legacy_managed_definition systemd "$unit"
  systemd_validate_identity
  systemd_control start -- "$service" >/dev/null
  systemd_validate_identity
  systemd_prove_running_or_retrying
  linux_service_tx_commit
  exit 0
fi
conf_dir="$(ls -d /etc/supervisor/conf.d 2>/dev/null || ls -d /etc/supervisord.d 2>/dev/null || echo '/etc/supervisor/conf.d')"
program="${fleet_name}-tunnel-${worker_agent}"
conf="$conf_dir/${program}.conf"
marker_line="; ${managed_marker}"
assert_no_duplicate_supervisor_program() {
  local expected="$1" include_root="${MAC_SUPERVISOR_INCLUDE_ROOT:-/}" program_source=""
  program_source="$(cat <<'PY'
import configparser
import glob
import os
import re
import sys
from pathlib import Path

program, expected, raw_root = sys.argv[1:]
root = Path(raw_root)
expected = os.path.abspath(expected)
candidates = set()
for relative in ("etc/supervisor/conf.d", "etc/supervisord.d"):
    candidates.update(Path(path) for path in glob.glob(str(root / relative / "*")))
for relative in ("etc/supervisor/supervisord.conf", "etc/supervisord.conf"):
    config_path = root / relative
    if not config_path.is_file():
        continue
    candidates.add(config_path)
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(config_path)
        patterns = parser.get("include", "files", fallback="").split()
    except (configparser.Error, OSError):
        patterns = []
    for pattern in patterns:
        if os.path.isabs(pattern):
            pattern = str(root / pattern.lstrip("/"))
        else:
            pattern = str(config_path.parent / pattern)
        candidates.update(Path(path) for path in glob.glob(pattern))
section = re.compile(r"^\s*\[program:%s\]\s*$" % re.escape(program), re.MULTILINE)
duplicates = []
for candidate in candidates:
    if not candidate.is_file():
        continue
    try:
        matched = section.search(candidate.read_text(encoding="utf-8")) is not None
    except (OSError, UnicodeError):
        matched = False
    if matched and os.path.abspath(candidate) != expected:
        duplicates.append(str(candidate))
if duplicates:
    print("duplicate supervisor program definitions: " + ", ".join(sorted(duplicates)), file=sys.stderr)
    raise SystemExit(1)
PY
)"
  if [ "$(type -t linux_root_bounded 2>/dev/null || true)" = function ]; then
    linux_root_bounded python3 -c "$program_source" "$program" "$expected" "$include_root"
  elif [ "$(id -u)" -eq 0 ]; then
    python3 -c "$program_source" "$program" "$expected" "$include_root"
  else
    sudo -n python3 -c "$program_source" "$program" "$expected" "$include_root"
  fi
}
supervisor_control() {
  # Supervisord is fleet infrastructure, so both inspection and mutation use
  # one exact system scope. A failed system-scope command is never retried as
  # the login user (or vice versa).
  linux_root_bounded supervisorctl "$@"
}

supervisor_inspect() {
  local output="" rc=0 observed="" detail="" pid_text=""
  SUPERVISOR_STATE=UNKNOWN
  SUPERVISOR_PID=0
  output="$(supervisor_control status "$program" 2>&1)" || rc=$?
  case "$output" in
    *$'\n'*)
      echo "supervisor returned ambiguous status for exact tunnel $program" >&2
      return 1
      ;;
  esac
  if [ "$output" = "$program: ERROR (no such process)" ]; then
    SUPERVISOR_STATE=ABSENT
    SUPERVISOR_PID=0
    return 0
  fi
  if [ "$rc" -ne 0 ]; then
    echo "could not inspect exact supervisor tunnel $program (rc=$rc): $output" >&2
    return 1
  fi
  observed="${output%% *}"
  [ "$observed" = "$program" ] || {
    echo "supervisor status named an unexpected program: $output" >&2
    return 1
  }
  detail="${output#"$observed" }"
  SUPERVISOR_STATE="${detail%% *}"
  detail="${detail#"$SUPERVISOR_STATE"}"
  detail="${detail# }"
  SUPERVISOR_PID=0
  case "$SUPERVISOR_STATE" in
    RUNNING)
      case "$detail" in
        pid\ [1-9]*,*)
          pid_text="${detail#pid }"
          pid_text="${pid_text%%,*}"
          case "$pid_text" in
            ''|*[!0-9]*) return 1 ;;
          esac
          SUPERVISOR_PID="$pid_text"
          ;;
        *)
          echo "running supervisor tunnel lacks one exact positive pid: $output" >&2
          return 1
          ;;
      esac
      ;;
    STARTING|BACKOFF|STOPPED|EXITED|FATAL) ;;
    *)
      echo "supervisor returned unknown state for exact tunnel $program: $output" >&2
      return 1
      ;;
  esac
}

supervisor_prove_running_or_retrying() {
  supervisor_inspect || return $?
  case "$SUPERVISOR_STATE:$SUPERVISOR_PID" in
    RUNNING:[1-9]*|STARTING:0|BACKOFF:0) return 0 ;;
    *)
      echo "supervisor tunnel is neither running nor awaiting key authorization: $program ($SUPERVISOR_STATE)" >&2
      return 1
      ;;
  esac
}

supervisor_quiesce_current() {
  local stop_rc=0
  supervisor_inspect || return $?
  case "$SUPERVISOR_STATE" in
    RUNNING|STARTING|BACKOFF)
      supervisor_control stop "$program" >/dev/null 2>&1 || stop_rc=$?
      ;;
    ABSENT|STOPPED|EXITED|FATAL) ;;
    *) return 1 ;;
  esac
  supervisor_inspect || return $?
  case "$SUPERVISOR_STATE:$SUPERVISOR_PID" in
    ABSENT:0|STOPPED:0|EXITED:0|FATAL:0) ;;
    *)
      echo "supervisor tunnel retained a live process after stop: $program ($SUPERVISOR_STATE pid=$SUPERVISOR_PID)" >&2
      return 1
      ;;
  esac
  [ "$stop_rc" -eq 0 ] || {
    echo "supervisor stop failed even though no process remains: $program (rc=$stop_rc)" >&2
    return "$stop_rc"
  }
}

supervisor_restore_previous_generation() {
  local rollback_rc=0 start_rc=0
  supervisor_quiesce_current || rollback_rc=1
  linux_service_tx_restore_artifact || rollback_rc=1
  supervisor_control reread >/dev/null 2>&1 || rollback_rc=1
  supervisor_control update >/dev/null 2>&1 || rollback_rc=1
  if [ "$SUPERVISOR_PRIOR_PRESENT" = 1 ]; then
    assert_no_duplicate_supervisor_program "$conf" || rollback_rc=1
    assert_managed_definition_or_absent "$conf" "$marker_line" supervisor || rollback_rc=1
    legacy_managed_definition supervisor "$conf" || rollback_rc=1
    if [ "$SUPERVISOR_PRIOR_ACTIVE_INTENT" = active ]; then
      supervisor_inspect || rollback_rc=1
      case "$SUPERVISOR_STATE" in
        RUNNING|STARTING|BACKOFF) ;;
        *)
          supervisor_control start "$program" >/dev/null 2>&1 || start_rc=$?
          supervisor_prove_running_or_retrying || rollback_rc=1
          # A bounded client timeout is resolved by the authoritative status
          # proof above; every other nonzero start result remains a rollback
          # failure even if the daemon happened to race into an active state.
          [ "$start_rc" -eq 0 ] || [ "$start_rc" -eq 124 ] || rollback_rc=1
          ;;
      esac
      supervisor_prove_running_or_retrying || rollback_rc=1
    else
      supervisor_quiesce_current || rollback_rc=1
      supervisor_inspect || rollback_rc=1
      [ "$SUPERVISOR_STATE:$SUPERVISOR_PID" = STOPPED:0 ] || rollback_rc=1
    fi
  else
    supervisor_inspect || rollback_rc=1
    [ "$SUPERVISOR_STATE:$SUPERVISOR_PID" = ABSENT:0 ] || rollback_rc=1
    if linux_root_bounded test -e "$conf" >/dev/null 2>&1; then
      rollback_rc=1
    fi
  fi
  return "$rollback_rc"
}

assert_no_duplicate_supervisor_program "$conf"
assert_managed_definition_or_absent "$conf" "$marker_line" supervisor
supervisor_inspect
SUPERVISOR_PRIOR_STATE="$SUPERVISOR_STATE"
SUPERVISOR_PRIOR_PRESENT=0
SUPERVISOR_PRIOR_ACTIVE_INTENT=inactive
if linux_root_bounded test -e "$conf" >/dev/null 2>&1; then
  SUPERVISOR_PRIOR_PRESENT=1
  [ "$SUPERVISOR_STATE" != ABSENT ] || {
    echo "refusing unreconciled supervisor definition absent from manager state" >&2
    exit 1
  }
  legacy_managed_definition supervisor "$conf" || {
    echo "refusing supervisor tunnel definition outside the exact MAC shape" >&2
    exit 1
  }
else
  [ "$SUPERVISOR_STATE" = ABSENT ] || {
    echo "refusing same-name supervisor program from another include" >&2
    exit 1
  }
fi
case "$SUPERVISOR_STATE:$SUPERVISOR_PID" in
  RUNNING:[1-9]*|STARTING:0|BACKOFF:0)
    SUPERVISOR_PRIOR_ACTIVE_INTENT=active
    ;;
  STOPPED:0|ABSENT:0) ;;
  *)
    echo "refusing failed or unrepresentable supervisor pre-state: $SUPERVISOR_STATE" >&2
    exit 1
    ;;
esac

linux_service_tx_begin "$conf" "$program" supervisor_restore_previous_generation
mkdir -p "$HOME/.mac/logs"
definition_tmp="$(mktemp "$HOME/.mac/logs/${program}.conf.mac-deploy.XXXXXX")"
cat > "$definition_tmp" <<EOF
${marker_line}
[program:${fleet_name}-tunnel-${worker_agent}]
command=ssh -N -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -i $HOME/.ssh/mac_tunnel_id -R 127.0.0.1:18789:127.0.0.1:8789 -R 127.0.0.1:18090:127.0.0.1:8090 -R 127.0.0.1:16333:127.0.0.1:6333 -R 127.0.0.1:13002:127.0.0.1:3002 ${tunnel_user}@${tunnel_host}
directory=$HOME
user=$(whoami)
autostart=true
autorestart=true
startsecs=5
; gketun-01: this program is (re)installed before the spoke authorizes the hub
; tunnel key (install runs before deploy_host), so its first ssh attempts exit
; immediately. Keep retrying instead of going FATAL after the default 3 tries, so
; the tunnel auto-establishes once the spoke authorizes the key mid-deploy.
startretries=1000
stopwaitsecs=10
stdout_logfile=$HOME/.mac/logs/tunnel-${worker_agent}.log
stderr_logfile=$HOME/.mac/logs/tunnel-${worker_agent}.log
EOF
chmod 0600 "$definition_tmp"
legacy_managed_definition supervisor "$definition_tmp"
linux_service_tx_mark_mutating
if [ "$SUPERVISOR_PRIOR_PRESENT" = 1 ]; then
  supervisor_quiesce_current
else
  supervisor_inspect
  [ "$SUPERVISOR_STATE:$SUPERVISOR_PID" = ABSENT:0 ]
fi
assert_no_duplicate_supervisor_program "$conf"
assert_managed_definition_or_absent "$conf" "$marker_line" supervisor
linux_service_tx_verify_snapshot_current
linux_service_tx_replace "$definition_tmp" "$conf"
definition_tmp=""
legacy_managed_definition supervisor "$conf"
run_root_noninteractive grep -Fqx "$marker_line" "$conf"
supervisor_control reread >/dev/null
assert_no_duplicate_supervisor_program "$conf"
supervisor_control update >/dev/null
# update registers and autostarts changed definitions. Do not issue a blocking
# start while the spoke has not authorized the key; RUNNING, STARTING, and
# BACKOFF are the only valid pre-key states. Missing, stopped, or FATAL is a
# failed transaction and restores the prior program plus its active intent.
supervisor_prove_running_or_retrying
linux_service_tx_commit
HUBSCRIPT
  then
    hub_script_status=0
  else
    hub_script_status=$?
  fi
  cleanup_fence="$(remote_deployment_fenced_exec \
    "$hub_deployment_id" 0 rm -f "$remote_launchd_lifecycle")"
  if ! ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$cleanup_fence"; then
    echo "ERROR: ${hub_agent}: could not remove temporary launchd lifecycle contract" >&2
    hub_script_status=1
  fi
  if [ "$hub_lock_temporary" = 1 ]; then
    if ! release_remote_deployment_lock "$hub_agent" "$hub_deployment_id"; then
      echo "ERROR: ${hub_agent}: could not release temporary tunnel configuration fence" >&2
      return 1
    fi
  fi
  return "$hub_script_status"
}

uses_direct_mesh_hub() {
  local provider hub_url
  provider="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  hub_url="${2:-}"
  case "$provider" in
    tailscale|headscale|none)
      [ -n "$hub_url" ]
      ;;
    *)
      return 1
      ;;
  esac
}

worker_has_mesh_client() {
  local agent="$1" raw_target="$2" provider="$3" ssh_parts=() ssh_args=() ssh_target item last_index
  provider="$(printf '%s' "${provider:-}" | tr '[:upper:]' '[:lower:]')"
  case "$provider" in
    tailscale|headscale) ;;
    *) return 1 ;;
  esac
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" \
    'command -v tailscale >/dev/null 2>&1 && tailscale status --self >/dev/null 2>&1' 2>/dev/null
}

worker_can_reach_hub_url() {
  local agent="$1" raw_target="$2" hub_url="$3" ssh_parts=() ssh_args=() ssh_target item last_index
  [ -n "$hub_url" ] || return 1
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" \
    "curl -fsS --connect-timeout 3 --max-time 5 '${hub_url%/}/health' >/dev/null 2>&1" 2>/dev/null
}

spec_requires_report_repository_executor() {
  # Report repository execution is meaningful only for a loop worker whose
  # frozen deployment enables OpenShell. The same calculation is used by the
  # image preflight and remote installer, so selected launchd/systemd targets
  # and SSH-managed K8s pods cannot disagree about the release requirement.
  local spec="$1" fields=() worker_mode openshell_required request required
  IFS='|' read -r -a fields <<<"$spec"
  worker_mode="${fields[9]:-heartbeat}"
  [ "$worker_mode" = loop ] || return 1
  openshell_required="${fields[53]:-0}"
  request="$(normalize_boolean_token "${MAC_DEPLOY_OPENSHELL:-}")"
  required="$(normalize_boolean_token "$openshell_required")"
  case "$request,$required" in
    1,*|true,*|yes,*|on,*|*,1|*,true|*,yes|*,on) return 0 ;;
    *) return 1 ;;
  esac
}

stable_worker_agent_id() {
  "$PYTHON_BIN" - "$1" <<'PY'
import re
import sys

safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", sys.argv[1].lower()).strip("_") or "default"
print("agent_%s" % safe)
PY
}

reconcile_bound_worker_attestation_key() (
  # A bound worker has secret-free verify authority only. Recovery is owned by
  # this fenced deployment controller: probe the target, ask the hub to rotate
  # only when the key is missing/stale, relay the one-use key through owner-only
  # files, restart, and require a second proof before any worker credential is
  # activated. No raw key is printed or placed in argv/environment variables.
  set -euo pipefail
  umask 077
  local agent="$1" hub_agent="$2" supervisor="$3" fleet_name="$4"
  local deployment_id agent_id
  deployment_id="$(deployment_id_for_agent "$agent")"
  agent_id="$(stable_worker_agent_id "$agent")"
  assert_remote_deployment_lock "$agent" "$deployment_id"

  local probe="$TMPDIR_LOCAL/attestation-probe-${agent_id}.json"
  local second_probe="$TMPDIR_LOCAL/attestation-probe-${agent_id}-second.json"
  local manifest="$TMPDIR_LOCAL/attestation-recovery-${agent_id}.json"
  # These must remain distinct even when the selected worker is the hub host:
  # installation consumes the worker copy, while the hub copy is retained
  # until the restarted worker proves the newly installed key.
  local hub_manifest="/tmp/mac-attestation-recovery-hub-${agent_id}-${TS}.json"
  local worker_manifest="/tmp/mac-attestation-recovery-worker-${agent_id}-${TS}.json"
  local worker_receipt="/tmp/mac-attestation-recovery-${agent_id}-${TS}-receipt.json"
  local hub_ssh_parts=() hub_ssh_args=() hub_ssh_target
  local worker_ssh_parts=() worker_ssh_args=() worker_ssh_target
  local item last_index
  while IFS= read -r -d '' item; do hub_ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  while IFS= read -r -d '' item; do worker_ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#hub_ssh_parts[@]} - 1))
  hub_ssh_target="${hub_ssh_parts[$last_index]}"
  hub_ssh_args=("${hub_ssh_parts[@]:0:$last_index}")
  last_index=$((${#worker_ssh_parts[@]} - 1))
  worker_ssh_target="${worker_ssh_parts[$last_index]}"
  worker_ssh_args=("${worker_ssh_parts[@]:0:$last_index}")

  cleanup_attestation_relay() {
    rm -f "$probe" "$second_probe" "$manifest"
    local cleanup_cmd
    cleanup_cmd="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c \
      "rm -f $(shell_quote "$worker_manifest") $(shell_quote "$worker_receipt")")"
    ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
      "${worker_ssh_args[@]}" "$worker_ssh_target" "$cleanup_cmd" \
      >/dev/null 2>&1 || true
    # Recovery cannot be resumed from raw key material after a partial target
    # install. Always destroy the hub-side copy; a retry performs a fresh probe
    # and conditional rotation under a new deployment transaction.
    ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
      "${hub_ssh_args[@]}" "$hub_ssh_target" \
      "rm -f $(shell_quote "$hub_manifest")" >/dev/null 2>&1 || true
  }
  trap cleanup_attestation_relay EXIT
  rm -f "$probe" "$second_probe" "$manifest"

  local probe_cmd fenced_probe_cmd probe_state probe_b64 recovery_result
  probe_cmd='"$HOME/.mac/venv/bin/python" -m mac.deployment_attestation probe'
  probe_cmd+=" --agent-id $(shell_quote "$agent_id")"
  probe_cmd+=" --deployment-id $(shell_quote "$deployment_id")"
  probe_cmd+=' --env-file "$HOME/.mac/mac.env"'
  fenced_probe_cmd="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c "$probe_cmd")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${worker_ssh_args[@]}" "$worker_ssh_target" "$fenced_probe_cmd" > "$probe"
  chmod 0600 "$probe"
  probe_state="$("$PYTHON_BIN" - "$probe" "$agent_id" "$deployment_id" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if (
    not isinstance(payload, dict)
    or payload.get("schema") != "mac.agent_attestation_key_probe.v1"
    or payload.get("agent_id") != sys.argv[2]
    or payload.get("deployment_id") != sys.argv[3]
    or payload.get("state") not in {"missing", "present"}
    or "attestation_key" in payload
):
    raise SystemExit("target returned an invalid or secret-bearing key probe")
print(payload["state"])
PY
)"
  probe_b64="$("$PYTHON_BIN" - "$probe" <<'PY'
import base64
import sys
print(base64.b64encode(open(sys.argv[1], "rb").read()).decode("ascii"))
PY
)"
  echo "==> ${agent}: attestation key probe state=${probe_state}; validating with hub"
  recovery_result="$(ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" \
    "MAC_DEPLOY_ATTESTATION_PROBE_B64=$(shell_quote "$probe_b64") MAC_DEPLOY_ATTESTATION_MANIFEST=$(shell_quote "$hub_manifest") bash -s" <<'REMOTE_ATTESTATION_RECOVERY'
set -euo pipefail
set -a
. "$HOME/.mac/mac.env"
set +a
export MAC_DEPLOY_GATE_ADMIN_TOKEN="${MAC_API_TOKEN:?}"
"$HOME/.mac/venv/bin/python" - <<'PY'
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from mac.deployment_attestation import _atomic_private_json, recovery_manifest

probe = json.loads(base64.b64decode(os.environ["MAC_DEPLOY_ATTESTATION_PROBE_B64"]))
agent_id = str(probe.get("agent_id") or "")
deployment_id = str(probe.get("deployment_id") or "")
hub_url = str(os.environ.get("MAC_HUB_URL") or "").rstrip("/")
token = os.environ["MAC_DEPLOY_GATE_ADMIN_TOKEN"]


def api(path, body):
    request = urllib.request.Request(
        hub_url + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError("POST %s failed with HTTP %s: %s" % (path, exc.code, detail)) from exc


valid = False
if probe.get("state") == "present":
    verified = api(
        "/agents/%s/attestation-key/verify" % agent_id,
        {"challenge": probe.get("challenge"), "signature": probe.get("signature")},
    )
    valid = bool(isinstance(verified, dict) and verified.get("valid") is True)
if valid:
    print(json.dumps({"status": "valid", "agent_id": agent_id}, sort_keys=True))
    raise SystemExit(0)

rotated = api(
    "/agents/%s/attestation-key/recover" % agent_id,
    {"probe": probe},
)
key = str(rotated.get("attestation_key") or "") if isinstance(rotated, dict) else ""
manifest = recovery_manifest(agent_id, deployment_id, key)
_atomic_private_json(Path(os.environ["MAC_DEPLOY_ATTESTATION_MANIFEST"]), manifest)
print(
    json.dumps(
        {
            "status": "rotated",
            "agent_id": agent_id,
            "reason": "missing" if probe.get("state") == "missing" else "stale",
            "manifest_written": True,
        },
        sort_keys=True,
    )
)
PY
REMOTE_ATTESTATION_RECOVERY
)"
  if [ "$(printf '%s' "$recovery_result" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("status") or "")')" = valid ]; then
    echo "==> ${agent}: existing attestation key proved valid; no rotation"
    return 0
  fi
  [ "$(printf '%s' "$recovery_result" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("status") or "")')" = rotated ] || {
    echo "==> ${agent}: hub returned an invalid attestation recovery result" >&2
    return 1
  }

  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" \
    "cat $(shell_quote "$hub_manifest")" > "$manifest"
  chmod 0600 "$manifest"
  fenced_remote_upload "$agent" "$deployment_id" "$manifest" "$worker_manifest"
  local install_cmd fenced_install_cmd
  install_cmd='set -e; umask 077; chmod 0600'
  install_cmd+=" $(shell_quote "$worker_manifest")"
  install_cmd+='; "$HOME/.mac/venv/bin/python" -m mac.deployment_attestation install'
  install_cmd+=" --manifest $(shell_quote "$worker_manifest")"
  install_cmd+=' --env-file "$HOME/.mac/mac.env"'
  install_cmd+=" --agent-id $(shell_quote "$agent_id")"
  install_cmd+=" --deployment-id $(shell_quote "$deployment_id")"
  install_cmd+=" --receipt-out $(shell_quote "$worker_receipt")"
  assert_remote_deployment_lock "$agent" "$deployment_id"
  fenced_install_cmd="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c "$install_cmd")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${worker_ssh_args[@]}" "$worker_ssh_target" "$fenced_install_cmd" >/dev/null

  # The process inherited the old key. Restart behind the same durable hold,
  # then build a fresh target-owned proof from the atomically installed env.
  restart_remote_mac_agent_under_epoch "$agent" "$supervisor" "$fleet_name"
  assert_remote_deployment_lock "$agent" "$deployment_id"
  fenced_probe_cmd="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c "$probe_cmd")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${worker_ssh_args[@]}" "$worker_ssh_target" "$fenced_probe_cmd" > "$second_probe"
  chmod 0600 "$second_probe"
  probe_b64="$("$PYTHON_BIN" - "$second_probe" <<'PY'
import base64
import sys
print(base64.b64encode(open(sys.argv[1], "rb").read()).decode("ascii"))
PY
)"
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" \
    "MAC_DEPLOY_ATTESTATION_PROBE_B64=$(shell_quote "$probe_b64") MAC_DEPLOY_ATTESTATION_MANIFEST=$(shell_quote "$hub_manifest") bash -s" <<'REMOTE_ATTESTATION_SECOND_PROOF'
set -euo pipefail
set -a
. "$HOME/.mac/mac.env"
set +a
export MAC_DEPLOY_GATE_ADMIN_TOKEN="${MAC_API_TOKEN:?}"
"$HOME/.mac/venv/bin/python" - <<'PY'
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

probe = json.loads(base64.b64decode(os.environ["MAC_DEPLOY_ATTESTATION_PROBE_B64"]))
agent_id = str(probe.get("agent_id") or "")
if probe.get("state") != "present":
    raise RuntimeError("post-install attestation key is still missing")
request = urllib.request.Request(
    str(os.environ.get("MAC_HUB_URL") or "").rstrip("/")
    + "/agents/%s/attestation-key/verify" % agent_id,
    data=json.dumps(
        {"challenge": probe.get("challenge"), "signature": probe.get("signature")}
    ).encode("utf-8"),
    method="POST",
    headers={
        "Authorization": "Bearer " + os.environ["MAC_DEPLOY_GATE_ADMIN_TOKEN"],
        "Accept": "application/json",
        "Content-Type": "application/json",
    },
)
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", errors="replace")[:500]
    raise RuntimeError("post-install key verification failed with HTTP %s: %s" % (exc.code, detail)) from exc
if not isinstance(result, dict) or result.get("valid") is not True:
    raise RuntimeError("post-install attestation key proof did not verify")
Path(os.environ["MAC_DEPLOY_ATTESTATION_MANIFEST"]).unlink(missing_ok=True)
print(json.dumps({"status": "proved", "agent_id": agent_id}, sort_keys=True))
PY
REMOTE_ATTESTATION_SECOND_PROOF
  echo "==> ${agent}: recovered attestation key installed and proved a second time"
)

reconcile_report_repository_executor_approval() (
  # The attestation is worker-authored; approval is controller-authored. Fetch
  # the exact current tuple and startup report under the target deployment
  # fence, then let the admin-only API compare-and-set the matching approval.
  # Any mismatch/failure explicitly revokes eligibility before returning.
  set -euo pipefail
  local agent="$1" hub_agent="$2" required="$3"
  local deployment_id agent_id hub_ssh_parts=() hub_ssh_args=() hub_ssh_target
  local item last_index
  deployment_id="$(deployment_id_for_agent "$agent")"
  agent_id="$(stable_worker_agent_id "$agent")"
  assert_remote_deployment_lock "$agent" "$deployment_id"
  while IFS= read -r -d '' item; do hub_ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#hub_ssh_parts[@]} - 1))
  hub_ssh_target="${hub_ssh_parts[$last_index]}"
  hub_ssh_args=("${hub_ssh_parts[@]:0:$last_index}")
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" \
    "MAC_DEPLOY_REPORT_AGENT_ID=$(shell_quote "$agent_id") MAC_DEPLOY_REPORT_REQUIRED=$(shell_quote "$required") bash -s" <<'REMOTE_REPORT_EXECUTOR_APPROVAL'
set -euo pipefail
set -a
. "$HOME/.mac/mac.env"
set +a
export MAC_DEPLOY_GATE_ADMIN_TOKEN="${MAC_API_TOKEN:?}"
"$HOME/.mac/venv/bin/python" - <<'PY'
import json
import os
import urllib.error
import urllib.request

from mac.models import (
    REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY,
    agent_has_read_only_report_repository_executor,
    valid_read_only_report_repository_executor_attestation,
)

agent_id = os.environ["MAC_DEPLOY_REPORT_AGENT_ID"]
required = os.environ.get("MAC_DEPLOY_REPORT_REQUIRED") == "1"
hub_url = str(os.environ.get("MAC_HUB_URL") or "").rstrip("/")
token = os.environ["MAC_DEPLOY_GATE_ADMIN_TOKEN"]


def api(method, path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        hub_url + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            "%s %s failed with HTTP %s: %s" % (method, path, exc.code, detail)
        ) from exc
    return json.loads(raw) if raw else None


def revoke(reason):
    return api(
        "POST",
        "/agents/%s/report-repository-executor/revoke" % agent_id,
        {"reason": reason, "actor": "fleet-deploy"},
    )


if not required:
    row = revoke("selected node is not an OpenShell report executor")
    resources = row.get("resources") if isinstance(row, dict) else {}
    if agent_has_read_only_report_repository_executor(resources):
        raise RuntimeError("report executor marker survived explicit revocation")
    print(json.dumps({"status": "revoked-not-required", "agent_id": agent_id}))
    raise SystemExit(0)

try:
    row = api("GET", "/agents/%s" % agent_id)
    if not isinstance(row, dict) or row.get("id") != agent_id:
        raise RuntimeError("hub returned the wrong agent row")
    resources = row.get("resources") if isinstance(row.get("resources"), dict) else {}
    attestation = resources.get(REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY)
    startup = resources.get("startup_self_test")
    checks = startup.get("checks") if isinstance(startup, dict) else None
    if not valid_read_only_report_repository_executor_attestation(attestation):
        raise RuntimeError("worker lacks a valid current report executor attestation")
    if not (
        resources.get("openshell_required") is True
        and isinstance(startup, dict)
        and startup.get("schema") == "mac.agent_startup_self_test.v1"
        and startup.get("agent_id") == agent_id
        and startup.get("status") in {"passed", "degraded"}
        and startup.get("blocking_problems") == []
        and isinstance(checks, dict)
        and checks.get("openshell_executor_config") is True
        and checks.get("report_repository_executor_attestation") is True
        and startup.get("report_repository_executor_attestation") == attestation
        and str(startup.get("timestamp") or "")
    ):
        raise RuntimeError("worker startup proof does not bind the current attestation")
    approved = api(
        "POST",
        "/agents/%s/report-repository-executor/approve" % agent_id,
        {
            "expected_attestation": attestation,
            "expected_startup_timestamp": startup["timestamp"],
            "actor": "fleet-deploy",
        },
    )
    approved_resources = (
        approved.get("resources") if isinstance(approved, dict) else {}
    )
    approved_startup = approved_resources.get("startup_self_test")
    if not (
        isinstance(approved, dict)
        and approved.get("id") == agent_id
        and agent_has_read_only_report_repository_executor(approved_resources)
        and isinstance(approved_startup, dict)
        and approved_startup.get("timestamp") == startup["timestamp"]
        and approved_startup.get("report_repository_executor_attestation")
        == attestation
    ):
        raise RuntimeError("hub did not derive the exact approved report marker")
except Exception:
    try:
        revoke("deployment report executor approval failed or drifted")
    except Exception:
        pass
    raise
print(
    json.dumps(
        {
            "status": "approved",
            "agent_id": agent_id,
            "runtime_image_ref": attestation["runtime_image_ref"],
            "policy_sha256": attestation["policy_sha256"],
            "source_bundle_sha256": attestation["source_bundle_sha256"],
            "startup_timestamp": startup["timestamp"],
        },
        sort_keys=True,
    )
)
PY
REMOTE_REPORT_EXECUTOR_APPROVAL
  if [ "$required" = 1 ]; then
    echo "==> ${agent}: controller approved exact startup-attested report executor"
  else
    echo "==> ${agent}: report executor approval revoked (not required by frozen spec)"
  fi
)

provision_bound_worker_credential() (
  # The first deploy starts under the compatibility token so a brand-new agent
  # can register. This second phase replaces it with an exact per-agent token,
  # proves the destination readback and authenticated heartbeat, then activates
  # it. Raw token material only crosses owner-only files; it is never argv/log
  # data, and the hub manifest is consumed on successful activation.
  set -euo pipefail
  umask 077
  local agent="$1" hub_agent="$2" supervisor="$3" fleet_name="$4" worker_capabilities="$5"
  local require_report_executor="${6:-0}"
  case "$(printf '%s' "${MAC_DEPLOY_ALLOW_LEGACY_WORKER_TOKEN:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on)
      echo "==> ${agent}: explicit legacy worker-token override; package work remains disabled"
      set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" release keep legacy "" deferred "$require_report_executor"
      return 0
      ;;
  esac

  local agent_id runtime_digest principal_id
  agent_id="$(stable_worker_agent_id "$agent")"
  assert_remote_deployment_lock "$agent" "$(deployment_id_for_agent "$agent")"
  local local_manifest="$TMPDIR_LOCAL/worker-credential-${agent_id}.json"
  local local_receipt="$TMPDIR_LOCAL/worker-credential-${agent_id}-receipt.json"
  # Keep relay artifacts distinct when the selected worker and hub resolve to
  # the same host. Worker EXIT cleanup must not destroy the hub retry manifest
  # before activation has committed.
  local hub_manifest="/tmp/mac-worker-credential-hub-${agent_id}-${TS}.json"
  local hub_receipt="/tmp/mac-worker-credential-hub-${agent_id}-${TS}-receipt.json"
  local worker_manifest="/tmp/mac-worker-credential-worker-${agent_id}-${TS}.json"
  local worker_receipt="/tmp/mac-worker-credential-worker-${agent_id}-${TS}-receipt.json"

  local hub_ssh_parts=() hub_ssh_args=() hub_ssh_target
  local worker_ssh_parts=() worker_ssh_args=() worker_ssh_target
  local item last_index
  while IFS= read -r -d '' item; do hub_ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  while IFS= read -r -d '' item; do worker_ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#hub_ssh_parts[@]} - 1))
  hub_ssh_target="${hub_ssh_parts[$last_index]}"
  hub_ssh_args=("${hub_ssh_parts[@]:0:$last_index}")
  last_index=$((${#worker_ssh_parts[@]} - 1))
  worker_ssh_target="${worker_ssh_parts[$last_index]}"
  worker_ssh_args=("${worker_ssh_parts[@]:0:$last_index}")

  local runtime_cmd runtime_result
  runtime_cmd='set -e; set -a; . "$HOME/.mac/mac.env"; set +a; "$HOME/.mac/venv/bin/python" -m mac.worker_credentials ensure-runtime'
  runtime_cmd+=" --source-commit $(shell_quote "$GIT_REV") --created-by fleet-deploy"
  echo "==> ${agent}: registering exact fleet source runtime"
  runtime_result="$(
    ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
      "${hub_ssh_args[@]}" "$hub_ssh_target" "$runtime_cmd"
  )"
  runtime_digest="$(
    printf '%s' "$runtime_result" | "$PYTHON_BIN" -c '
import json
import re
import sys

payload = json.load(sys.stdin)
expected_commit = sys.argv[1]
digest = str(payload.get("runtime_digest") or "")
if (
    payload.get("schema") != "mac.fleet_source_runtime.v1"
    or payload.get("status") != "ready"
    or payload.get("source_commit") != expected_commit
    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
):
    raise SystemExit("hub returned an invalid fleet source runtime receipt")
print(digest)
' "$GIT_REV"
  )"

  cleanup_worker_relay() {
    rm -f "$local_manifest" "$local_receipt"
    local cleanup_cmd
    cleanup_cmd="$(remote_deployment_fenced_exec "$(deployment_id_for_agent "$agent")" 0 sh -c \
      "rm -f $(shell_quote "$worker_manifest") $(shell_quote "$worker_receipt")")"
    ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${worker_ssh_args[@]}" \
      "$worker_ssh_target" "$cleanup_cmd" \
      >/dev/null 2>&1 || true
  }
  trap cleanup_worker_relay EXIT
  rm -f "$local_manifest" "$local_receipt"

  local issue_cmd issue_ok=0 attempt
  issue_cmd='set -e; set -a; . "$HOME/.mac/mac.env"; set +a; umask 077; "$HOME/.mac/venv/bin/python" -m mac.worker_credentials issue'
  issue_cmd+=" --agent-id $(shell_quote "$agent_id")"
  issue_cmd+=" --fleet $(shell_quote "$fleet_name") --environment vm"
  issue_cmd+=" --expected-source-commit $(shell_quote "$GIT_REV")"
  issue_cmd+=" --expected-runtime-digest $(shell_quote "$runtime_digest")"
  case ",$worker_capabilities," in
    *,work_package_v1,*)
      issue_cmd+=" --capability work_package_v1 --package-capable"
      ;;
  esac
  issue_cmd+=" --manifest-out $(shell_quote "$hub_manifest")"
  echo "==> ${agent}: issuing exact bound worker credential"
  for attempt in $(seq 1 "${MAC_DEPLOY_WORKER_CREDENTIAL_RETRIES:-24}"); do
    if ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
      "${hub_ssh_args[@]}" "$hub_ssh_target" "$issue_cmd" \
      >/dev/null 2>&1; then
      issue_ok=1
      break
    fi
    sleep "${MAC_DEPLOY_WORKER_CREDENTIAL_RETRY_SECONDS:-5}"
  done
  if [ "$issue_ok" != "1" ]; then
    echo "==> ${agent}: ERROR: hub never observed the registered worker for credential issuance" >&2
    return 1
  fi

  assert_remote_deployment_lock "$agent" "$(deployment_id_for_agent "$agent")"

  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" \
    "cat $(shell_quote "$hub_manifest")" > "$local_manifest"
  chmod 0600 "$local_manifest"
  principal_id="$("$PYTHON_BIN" - "$local_manifest" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
principal = str(payload.get("principal_id") or "")
if payload.get("schema") != "mac.worker_credential_install.v1" or not principal:
    raise SystemExit("invalid worker credential manifest")
print(principal)
PY
)"
  # The install manifest contains the raw worker bearer. It is opened locally
  # only after the worker proves this exact deployment owner in the same SSH
  # channel, then lands atomically under the held fcntl guard.
  fenced_remote_upload "$agent" "$(deployment_id_for_agent "$agent")" \
    "$local_manifest" "$worker_manifest"
  local install_cmd
  install_cmd='set -e; umask 077; chmod 0600'
  install_cmd+=" $(shell_quote "$worker_manifest")"
  install_cmd+='; "$HOME/.mac/venv/bin/python" -m mac.worker_credentials install-vm'
  install_cmd+=" --manifest $(shell_quote "$worker_manifest")"
  install_cmd+=" --agent-id $(shell_quote "$agent_id")"
  install_cmd+=' --env-file "$HOME/.mac/mac.env"'
  install_cmd+=" --receipt-out $(shell_quote "$worker_receipt")"
  assert_remote_deployment_lock "$agent" "$(deployment_id_for_agent "$agent")"
  local fenced_install_cmd
  fenced_install_cmd="$(remote_deployment_fenced_exec "$(deployment_id_for_agent "$agent")" 0 sh -c "$install_cmd")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${worker_ssh_args[@]}" "$worker_ssh_target" "$fenced_install_cmd" >/dev/null
  local fenced_receipt_cmd
  fenced_receipt_cmd="$(remote_deployment_fenced_exec "$(deployment_id_for_agent "$agent")" 0 sh -c \
    "cat $(shell_quote "$worker_receipt")")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${worker_ssh_args[@]}" "$worker_ssh_target" "$fenced_receipt_cmd" > "$local_receipt"
  chmod 0600 "$local_receipt"
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" \
    "umask 077; cat > $(shell_quote "$hub_receipt"); chmod 0600 $(shell_quote "$hub_receipt")" \
    < "$local_receipt"

  restart_remote_mac_agent_under_epoch "$agent" "$supervisor" "$fleet_name"
  local activate_cmd activate_ok=0
  activate_cmd='set -e; set -a; . "$HOME/.mac/mac.env"; set +a; "$HOME/.mac/venv/bin/python" -m mac.worker_credentials activate'
  activate_cmd+=" --agent-id $(shell_quote "$agent_id")"
  activate_cmd+=" --principal-id $(shell_quote "$principal_id")"
  activate_cmd+=" --receipt $(shell_quote "$hub_receipt")"
  activate_cmd+=" --manifest $(shell_quote "$hub_manifest")"
  echo "==> ${agent}: waiting for exact authenticated heartbeat"
  for attempt in $(seq 1 "${MAC_DEPLOY_WORKER_CREDENTIAL_RETRIES:-24}"); do
    if ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
      "${hub_ssh_args[@]}" "$hub_ssh_target" "$activate_cmd" \
      >/dev/null 2>&1; then
      activate_ok=1
      break
    fi
    sleep "${MAC_DEPLOY_WORKER_CREDENTIAL_RETRY_SECONDS:-5}"
  done
  if [ "$activate_ok" != "1" ]; then
    echo "==> ${agent}: ERROR: bound credential did not prove destination, source, runtime, capability, and authenticated heartbeat" >&2
    echo "    hub retry manifest retained at $hub_manifest" >&2
    return 1
  fi
  # Activation consumes the hub manifest itself after the database commit.
  # Remove both hub relay paths defensively only on that successful branch;
  # the failure branch above intentionally preserves the retry authority.
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" \
    "rm -f $(shell_quote "$hub_manifest") $(shell_quote "$hub_receipt")"
  # Credential activation is proved while the worker is still drained and
  # held. Arm the worker and prove its exact newly issued principal, but retain
  # the durable hub hold until every selected node is ready for one atomic
  # fleet-epoch commit.
  set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" release \
    keep authenticated "$principal_id" deferred "$require_report_executor"
  local policy_cmd
  policy_cmd='set -e; set -a; . "$HOME/.mac/mac.env"; set +a; "$HOME/.mac/venv/bin/python" -m mac.worker_credentials set-mode compatibility --review-live'
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" "$policy_cmd" >/dev/null
  echo "==> ${agent}: exact worker credential active and package membership reconciled"
)

pending_worker_manifest_file() {
  printf '%s/pending-worker-%s.json\n' \
    "$TMPDIR_LOCAL" "$(stable_worker_agent_id "$1")"
}

pending_worker_remote_manifest_path() {
  local agent="$1" epoch_id="${2:-$COHORT_EPOCH_ID}" digest
  digest="$("$PYTHON_BIN" -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$epoch_id")"
  printf '/tmp/mac-pending-worker-%s-%s.json\n' \
    "$(stable_worker_agent_id "$agent")" "$digest"
}

pending_worker_receipt_file() {
  printf '%s/pending-worker-receipt-%s.json\n' \
    "$TMPDIR_LOCAL" "$(stable_worker_agent_id "$1")"
}

attestation_candidate_file() {
  printf '%s/attestation-candidate-%s.json\n' \
    "$TMPDIR_LOCAL" "$(stable_worker_agent_id "$1")"
}

attestation_candidate_proof_file() {
  printf '%s/attestation-candidate-proof-%s.json\n' \
    "$TMPDIR_LOCAL" "$(stable_worker_agent_id "$1")"
}

issue_pending_worker_credential() (
  set -euo pipefail
  umask 077
  local agent="$1" hub_agent="$2" fleet_name="$3" worker_capabilities="$4"
  local agent_id runtime_result runtime_digest issue_result manifest principal
  local remote_manifest hub_ssh_parts=() hub_ssh_args=() hub_ssh_target item last_index
  agent_id="$(stable_worker_agent_id "$agent")"
  manifest="$(pending_worker_manifest_file "$agent")"
  remote_manifest="$(pending_worker_remote_manifest_path "$agent")"
  while IFS= read -r -d '' item; do hub_ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#hub_ssh_parts[@]} - 1))
  hub_ssh_target="${hub_ssh_parts[$last_index]}"
  hub_ssh_args=("${hub_ssh_parts[@]:0:$last_index}")
  runtime_result="$(ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" \
    "set -e; set -a; . \"\$HOME/.mac/mac.env\"; set +a; \"\$HOME/.mac/venv/bin/python\" -m mac.worker_credentials ensure-runtime --source-commit $(shell_quote "$GIT_REV") --created-by fleet-deploy")"
  runtime_digest="$(printf '%s' "$runtime_result" | "$PYTHON_BIN" -c '
import json,re,sys
value=json.load(sys.stdin); digest=str(value.get("runtime_digest") or "")
if value.get("schema") != "mac.fleet_source_runtime.v1" or value.get("status") != "ready" or re.fullmatch(r"[0-9a-f]{64}",digest) is None: raise SystemExit("invalid fleet runtime receipt")
print(digest)')"
  local issue_cmd
  issue_cmd='set -e; set -a; . "$HOME/.mac/mac.env"; set +a; umask 077; "$HOME/.mac/venv/bin/python" -m mac.worker_credentials issue'
  issue_cmd+=" --agent-id $(shell_quote "$agent_id") --fleet $(shell_quote "$fleet_name") --environment vm"
  issue_cmd+=" --expected-source-commit $(shell_quote "$GIT_REV") --expected-runtime-digest $(shell_quote "$runtime_digest")"
  issue_cmd+=" --created-by $(shell_quote "fleet-release:${COHORT_EPOCH_ID}")"
  case ",$worker_capabilities," in
    *,work_package_v1,*) issue_cmd+=" --capability work_package_v1 --package-capable" ;;
  esac
  issue_cmd+=" --manifest-out $(shell_quote "$remote_manifest")"
  issue_result="$(ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" "$issue_cmd")"
  principal="$(printf '%s' "$issue_result" | "$PYTHON_BIN" -c '
import json,sys
value=json.load(sys.stdin)
if value.get("status") != "issued" or value.get("agent_id") != sys.argv[1] or not value.get("principal_id"): raise SystemExit("invalid pending credential issuance")
print(value["principal_id"])' "$agent_id")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" \
    "cat $(shell_quote "$remote_manifest")" > "$manifest"
  chmod 0600 "$manifest"
  "$PYTHON_BIN" - "$manifest" "$agent_id" "$principal" <<'PY'
import json,sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
if value.get("schema") != "mac.worker_credential_install.v1" or value.get("agent_id") != sys.argv[2] or value.get("principal_id") != sys.argv[3]:
    raise SystemExit("pending credential manifest differs from issuance")
token=((value.get("credential") or {}).get("token"))
if not isinstance(token,str) or not token or any(char.isspace() for char in token):
    raise SystemExit("pending credential manifest lacks a safe token")
PY
  echo "==> ${agent}: pending worker principal ${principal} staged"
)

create_attestation_candidate() {
  local agent="$1" output
  output="$(attestation_candidate_file "$agent")"
  "$PYTHON_BIN" - "$output" <<'PY'
import json,os,secrets,sys,tempfile
from pathlib import Path
output=Path(sys.argv[1]); key=secrets.token_urlsafe(48)
fd,raw=tempfile.mkstemp(prefix=output.name+".",dir=output.parent); tmp=Path(raw)
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        json.dump({"schema":"mac.fleet_attestation_candidate.v1","key":key},stream,sort_keys=True,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp,output)
finally:
    tmp.unlink(missing_ok=True)
PY
}

install_pending_worker_credential() (
  set -euo pipefail
  umask 077
  local agent="$1" supervisor="$2" fleet_name="$3"
  local deployment_id agent_id manifest receipt remote_manifest remote_receipt command
  local ssh_parts=() ssh_args=() ssh_target item last_index
  deployment_id="$(deployment_id_for_agent "$agent")"
  agent_id="$(stable_worker_agent_id "$agent")"
  manifest="$(pending_worker_manifest_file "$agent")"
  receipt="$(pending_worker_receipt_file "$agent")"
  remote_manifest="$(pending_worker_remote_manifest_path "$agent")"
  remote_receipt="/tmp/mac-pending-worker-receipt-${agent_id}-${DEPLOY_CONTROLLER_NONCE}.json"
  fenced_remote_upload "$agent" "$deployment_id" "$manifest" "$remote_manifest"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c \
    "set -e; umask 077; chmod 0600 $(shell_quote "$remote_manifest"); \"\$HOME/.mac/venv/bin/python\" -m mac.worker_credentials install-vm --manifest $(shell_quote "$remote_manifest") --agent-id $(shell_quote "$agent_id") --env-file \"\$HOME/.mac/mac.env\" --receipt-out $(shell_quote "$remote_receipt") >/dev/null; cat $(shell_quote "$remote_receipt")")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command" > "$receipt"
  chmod 0600 "$receipt"
  set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" restart keep
  echo "==> ${agent}: pending worker credential installed; promotion remains deferred"
)

install_and_prove_attestation_candidate() (
  set -euo pipefail
  umask 077
  local agent="$1" supervisor="$2" fleet_name="$3"
  local deployment_id agent_id candidate manifest challenge proof principal generation
  local remote_manifest remote_receipt remote_challenge remote_proof command
  local ssh_parts=() ssh_args=() ssh_target item last_index
  deployment_id="$(deployment_id_for_agent "$agent")"
  agent_id="$(stable_worker_agent_id "$agent")"
  generation="$(worker_generation_for_agent "$agent")"
  candidate="$(attestation_candidate_file "$agent")"
  manifest="$TMPDIR_LOCAL/attestation-install-${agent_id}.json"
  challenge="$TMPDIR_LOCAL/attestation-challenge-${agent_id}.json"
  proof="$(attestation_candidate_proof_file "$agent")"
  principal="$("$PYTHON_BIN" - "$(pending_worker_manifest_file "$agent")" <<'PY'
import json,sys
print(json.load(open(sys.argv[1],encoding="utf-8"))["principal_id"])
PY
)"
  "$PYTHON_BIN" - "$candidate" "$manifest" "$challenge" "$agent_id" \
    "$deployment_id" "$COHORT_EPOCH_ID" "$generation" "$principal" <<'PY'
import hashlib,json,os,secrets,sys,tempfile
from pathlib import Path
candidate_path,manifest_path,challenge_path,agent,deployment,epoch,generation,principal=sys.argv[1:]
candidate=json.load(open(candidate_path,encoding="utf-8")); key=candidate.get("key")
if candidate.get("schema") != "mac.fleet_attestation_candidate.v1" or not isinstance(key,str) or len(key)<32:
    raise SystemExit("attestation candidate is invalid")
values=[
    (Path(manifest_path),{"schema":"mac.agent_attestation_key_recovery.v1","agent_id":agent,"deployment_id":deployment,"attestation_key":key,"issued_at":"fleet-release-epoch"}),
    (Path(challenge_path),{"schema":"mac.fleet_release_attestation_candidate_proof.v1","purpose":"synchronized-fleet-release-candidate","epoch_id":epoch,"agent_id":agent,"generation":generation,"principal_id":principal,"candidate_fingerprint":"sha256:"+hashlib.sha256(key.encode()).hexdigest(),"nonce":secrets.token_urlsafe(32)}),
]
for output,value in values:
    fd,raw=tempfile.mkstemp(prefix=output.name+".",dir=output.parent); tmp=Path(raw)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as stream:
            json.dump(value,stream,sort_keys=True,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp,output)
    finally: tmp.unlink(missing_ok=True)
PY
  remote_manifest="/tmp/mac-attestation-install-${agent_id}-${DEPLOY_CONTROLLER_NONCE}.json"
  remote_receipt="/tmp/mac-attestation-install-receipt-${agent_id}-${DEPLOY_CONTROLLER_NONCE}.json"
  remote_challenge="/tmp/mac-attestation-challenge-${agent_id}-${DEPLOY_CONTROLLER_NONCE}.json"
  remote_proof="/tmp/mac-attestation-proof-${agent_id}-${DEPLOY_CONTROLLER_NONCE}.json"
  fenced_remote_upload "$agent" "$deployment_id" "$manifest" "$remote_manifest"
  fenced_remote_upload "$agent" "$deployment_id" "$challenge" "$remote_challenge"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"; ssh_args=("${ssh_parts[@]:0:$last_index}")
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c \
    "set -e; umask 077; \"\$HOME/.mac/venv/bin/python\" -m mac.deployment_attestation install --manifest $(shell_quote "$remote_manifest") --env-file \"\$HOME/.mac/mac.env\" --agent-id $(shell_quote "$agent_id") --deployment-id $(shell_quote "$deployment_id") --receipt-out $(shell_quote "$remote_receipt") >/dev/null")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" "$command"
  set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" restart keep
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c \
    "set -e; umask 077; \"\$HOME/.mac/venv/bin/python\" -m mac.deployment_attestation prove-candidate --challenge $(shell_quote "$remote_challenge") --env-file \"\$HOME/.mac/mac.env\" --output $(shell_quote "$remote_proof") >/dev/null; cat $(shell_quote "$remote_proof"); rm -f $(shell_quote "$remote_challenge") $(shell_quote "$remote_proof") $(shell_quote "$remote_receipt")")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command" > "$proof"
  chmod 0600 "$proof"
  echo "==> ${agent}: candidate attestation key installed and node-authored proof captured"
)

hub_receipt_identity_sha256() {
  "$PYTHON_BIN" - "$1" <<'PY'
import json,re,sys
value=json.load(open(sys.argv[1],encoding="utf-8")); digest=str(value.get("identity_sha256") or "")
if re.fullmatch(r"sha256:[0-9a-f]{64}",digest) is None: raise SystemExit("hub receipt identity is invalid")
print(digest)
PY
}

build_and_open_hub_epoch() {
  local selected_specs_file="$1" hub_agent="$2"
  local material="$TMPDIR_LOCAL/hub-open-material.json"
  local plan="$TMPDIR_LOCAL/hub-open-plan.json"
  local request="$TMPDIR_LOCAL/hub-open-request.json"
  local receipt="$TMPDIR_LOCAL/hub-open-receipt.json"
  local spec fields=() agent agent_id fleet_name capabilities state candidate manifest
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a fields <<<"$spec"
    agent="${fields[0]}"; agent_id="$(stable_worker_agent_id "$agent")"
    fleet_name="${fields[23]:-mac}"; capabilities="${fields[10]:-}"
    issue_pending_worker_credential "$agent" "$hub_agent" "$fleet_name" "$capabilities"
    create_attestation_candidate "$agent"
    state="$TMPDIR_LOCAL/participant-state-${agent_id}.json"
    hub_epoch_client_read "$hub_agent" "$state" participant-state --agent-id "$agent_id"
  done < "$selected_specs_file"
  "$PYTHON_BIN" - "$selected_specs_file" "$TMPDIR_LOCAL/fleet-cohort.json" "$TMPDIR_LOCAL" "$material" \
    "$COHORT_EPOCH_ID" "$GIT_REV" "$REQUIRE_RELEASE_ALL_SELECTED" \
    "$SUCCESSOR_HOLD_REASON" <<'PY'
import json,os,sys,tempfile
from pathlib import Path
selected,cohort_raw,root_raw,output_raw,epoch,source,require_all,successor=sys.argv[1:]
root=Path(root_raw); agents=[]
cohort={item["stable_id"]:item for item in json.load(open(cohort_raw,encoding="utf-8"))}
for line in Path(selected).read_text(encoding="utf-8").splitlines():
    if not line.strip(): continue
    fields=line.split("|"); name=fields[0]; agent_id="agent_"+__import__("re").sub(r"[^A-Za-z0-9_.-]+","_",name.lower()).strip("_")
    state=json.load(open(root/("participant-state-%s.json"%agent_id),encoding="utf-8"))
    manifest=json.load(open(root/("pending-worker-%s.json"%agent_id),encoding="utf-8"))
    candidate=json.load(open(root/("attestation-candidate-%s.json"%agent_id),encoding="utf-8"))
    bound=cohort[agent_id]
    agents.append({
        "agent_id":agent_id,
        "generation":bound["generation"],
        "deployment_id":bound["deployment_id"],
        "participant_state":state,
        "principal_id":manifest["principal_id"],
        "attestation_candidate_key":candidate["key"],
        "report_executor_action":"revoke",
        "report_executor_attestation":None,
    })
payload={
    "schema":"mac.fleet_epoch_open_material.v1",
    "epoch_id":epoch,
    "source_commit":source,
    "require_release_all_selected":require_all=="1",
    "successor_hold_reason":successor or None,
    "desired_worker_credential_mode":"compatibility",
    "agents":agents,
}
output=Path(output_raw); fd,raw=tempfile.mkstemp(prefix=output.name+".",dir=output.parent); tmp=Path(raw)
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        json.dump(payload,stream,sort_keys=True,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp,output)
finally: tmp.unlink(missing_ok=True)
PY
  "$PYTHON_BIN" "$HUB_EPOCH_MATERIAL_HELPER" open \
    --material "$material" --plan-out "$plan" --request-out "$request" >/dev/null
  persist_hub_epoch_recovery_request "$hub_agent" "$request" open
  cohort_journal_mutate hub-open-start "$COHORT_EPOCH_ID" \
    "$COHORT_JOURNAL_REVISION" hub-open-start "$DEPLOY_CONTROLLER_NONCE" \
    --open-plan-file "$plan" >/dev/null
  hub_epoch_client_request "$hub_agent" "$request" "$receipt" \
    open --epoch "$COHORT_EPOCH_ID"
  cohort_journal_mutate hub-opened "$COHORT_EPOCH_ID" \
    "$COHORT_JOURNAL_REVISION" hub-opened "$DEPLOY_CONTROLLER_NONCE" \
    --evidence-file "$receipt" >/dev/null
  remove_hub_epoch_recovery_request "$hub_agent" "$COHORT_EPOCH_ID" open
  echo "==> fleet: exact pending principals, holds, and candidate keys staged atomically"
}

prove_and_commit_hub_epoch() {
  local selected_specs_file="$1" hub_agent="$2"
  local open_receipt="$TMPDIR_LOCAL/hub-open-receipt.json"
  local identity prove_material="$TMPDIR_LOCAL/hub-prove-material.json"
  local prove_plan="$TMPDIR_LOCAL/hub-prove-plan.json" prove_request="$TMPDIR_LOCAL/hub-prove-request.json"
  local proved_receipt="$TMPDIR_LOCAL/hub-proved-receipt.json"
  local release_material="$TMPDIR_LOCAL/hub-release-material.json"
  local release_plan="$TMPDIR_LOCAL/hub-release-plan.json" commit_request="$TMPDIR_LOCAL/hub-commit-request.json"
  local commit_receipt="$TMPDIR_LOCAL/hub-commit-receipt.json"
  identity="$(hub_receipt_identity_sha256 "$open_receipt")"
  "$PYTHON_BIN" - "$selected_specs_file" "$TMPDIR_LOCAL/fleet-cohort.json" "$TMPDIR_LOCAL" "$prove_material" \
    "$COHORT_EPOCH_ID" "$GIT_REV" "$identity" <<'PY'
import hashlib,json,os,re,sys,tempfile
from pathlib import Path
selected,cohort_raw,root_raw,output_raw,epoch,source,identity=sys.argv[1:]; root=Path(root_raw); agents=[]
cohort={item["stable_id"]:item for item in json.load(open(cohort_raw,encoding="utf-8"))}
for line in Path(selected).read_text(encoding="utf-8").splitlines():
    if not line.strip(): continue
    name=line.split("|",1)[0]; agent_id="agent_"+re.sub(r"[^A-Za-z0-9_.-]+","_",name.lower()).strip("_")
    prepared=root/("release-ready-%s.json"%agent_id)
    bound=cohort[agent_id]
    agents.append({
        "agent_id":agent_id,
        "generation":bound["generation"],
        "deployment_id":bound["deployment_id"],
        "prepared_evidence_sha256":hashlib.sha256(prepared.read_bytes()).hexdigest(),
        "install_receipt":json.load(open(root/("pending-worker-receipt-%s.json"%agent_id),encoding="utf-8")),
        "attestation_proof":json.load(open(root/("attestation-candidate-proof-%s.json"%agent_id),encoding="utf-8")),
        "report_executor_startup_timestamp":None,
    })
payload={"schema":"mac.fleet_epoch_prove_material.v1","epoch_id":epoch,"source_commit":source,"identity_sha256":identity,"agents":agents}
output=Path(output_raw); fd,raw=tempfile.mkstemp(prefix=output.name+".",dir=output.parent); tmp=Path(raw)
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        json.dump(payload,stream,sort_keys=True,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp,output)
finally: tmp.unlink(missing_ok=True)
PY
  "$PYTHON_BIN" "$HUB_EPOCH_MATERIAL_HELPER" prove \
    --material "$prove_material" --plan-out "$prove_plan" \
    --request-out "$prove_request" >/dev/null
  persist_hub_epoch_recovery_request "$hub_agent" "$prove_request" prove
  cohort_journal_mutate hub-prove-start "$COHORT_EPOCH_ID" \
    "$COHORT_JOURNAL_REVISION" hub-prove-start "$DEPLOY_CONTROLLER_NONCE" \
    --prove-plan-file "$prove_plan" >/dev/null
  hub_epoch_client_request "$hub_agent" "$prove_request" "$proved_receipt" \
    prove --epoch "$COHORT_EPOCH_ID"
  cohort_journal_mutate hub-proved "$COHORT_EPOCH_ID" \
    "$COHORT_JOURNAL_REVISION" hub-proved "$DEPLOY_CONTROLLER_NONCE" \
    --evidence-file "$proved_receipt" >/dev/null
  remove_hub_epoch_recovery_request "$hub_agent" "$COHORT_EPOCH_ID" prove
  "$PYTHON_BIN" - "$selected_specs_file" "$TMPDIR_LOCAL/fleet-cohort.json" "$release_material" \
    "$COHORT_EPOCH_ID" "$GIT_REV" "$identity" "$REQUIRE_RELEASE_ALL_SELECTED" \
    "$SUCCESSOR_HOLD_REASON" <<'PY'
import json,os,re,sys,tempfile
from pathlib import Path
selected,cohort_raw,output_raw,epoch,source,identity,require_all,successor=sys.argv[1:]; agents=[]
cohort={item["stable_id"]:item for item in json.load(open(cohort_raw,encoding="utf-8"))}
for line in Path(selected).read_text(encoding="utf-8").splitlines():
    if not line.strip(): continue
    name=line.split("|",1)[0]; agent_id="agent_"+re.sub(r"[^A-Za-z0-9_.-]+","_",name.lower()).strip("_")
    bound=cohort[agent_id]
    agents.append({"agent_id":agent_id,"generation":bound["generation"],"deployment_id":bound["deployment_id"]})
payload={"schema":"mac.fleet_epoch_release_material.v1","epoch_id":epoch,"source_commit":source,"identity_sha256":identity,"require_release_all_selected":require_all=="1","successor_hold_reason":successor or None,"agents":agents}
output=Path(output_raw); fd,raw=tempfile.mkstemp(prefix=output.name+".",dir=output.parent); tmp=Path(raw)
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        json.dump(payload,stream,sort_keys=True,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp,output)
finally: tmp.unlink(missing_ok=True)
PY
  "$PYTHON_BIN" "$HUB_EPOCH_MATERIAL_HELPER" release \
    --material "$release_material" --plan-out "$release_plan" \
    --request-out "$commit_request" >/dev/null
  cohort_journal_mutate commit-start "$COHORT_EPOCH_ID" \
    "$COHORT_JOURNAL_REVISION" commit-start "$DEPLOY_CONTROLLER_NONCE" \
    --release-plan-file "$release_plan" >/dev/null
  hub_epoch_client_request "$hub_agent" "$commit_request" "$commit_receipt" \
    commit --epoch "$COHORT_EPOCH_ID"
  cohort_journal_mutate commit "$COHORT_EPOCH_ID" \
    "$COHORT_JOURNAL_REVISION" commit "$DEPLOY_CONTROLLER_NONCE" \
    --evidence-file "$commit_receipt" >/dev/null
  echo "==> fleet: hub atomically promoted principals, attestation keys, holds, and policy"
}

collect_finalize_evidence() {
  local agent="$1" deployment_id agent_id output command
  local ssh_parts=() ssh_args=() ssh_target item last_index
  deployment_id="$(deployment_id_for_agent "$agent")"
  agent_id="$(stable_worker_agent_id "$agent")"
  output="$TMPDIR_LOCAL/finalize-${agent_id}.json"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1)); ssh_target="${ssh_parts[$last_index]}"; ssh_args=("${ssh_parts[@]:0:$last_index}")
  command="$(remote_deployment_fenced_exec "$deployment_id" 0 sh -c \
    "cat \"\$HOME/.mac/logs/deploy-${TS}-finalize.json\"")"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" "$command" > "$output"
  chmod 0600 "$output"
  validate_finalize_evidence "$output" "$agent" "$deployment_id"
}

validate_finalize_evidence() {
  local output="$1" agent="$2" generation="$3"
  "$PYTHON_BIN" - "$output" "$agent" "$generation" <<'PY'
import json,sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
if value.get("schema") != "mac.fleet_node_finalize.v1" or value.get("status") != "finalized" or value.get("agent") != sys.argv[2] or value.get("generation") != sys.argv[3]:
    raise SystemExit("node finalize receipt differs from selected generation")
PY
}

cleanup_committed_hub_relay() {
  local hub_agent="$1" spec fields=() agent agent_id remote_manifest
  local ssh_parts=() ssh_args=() ssh_target item last_index
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1)); ssh_target="${ssh_parts[$last_index]}"; ssh_args=("${ssh_parts[@]:0:$last_index}")
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a fields <<<"$spec"; agent="${fields[0]}"; agent_id="$(stable_worker_agent_id "$agent")"
    remote_manifest="$(pending_worker_remote_manifest_path "$agent")"
    ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
      "${ssh_args[@]}" "$ssh_target" "rm -f $(shell_quote "$remote_manifest")" >/dev/null
  done < "$2"
}

finalize_remote_deployment_release() {
  local agent="$1" deployment_id="$2" ssh_parts=() ssh_args=() ssh_target item last_index fence_exec
  assert_remote_deployment_lock "$agent" "$deployment_id"
  fence_exec="$(remote_deployment_fenced_exec "$deployment_id" 0 python3 -)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_STATE_ID=$(shell_quote "$deployment_id") $fence_exec <<'PY'
import json
import os
from pathlib import Path

path = Path.home() / '.mac' / 'deploy-dispatch-hold.json'
if not path.exists():
    raise SystemExit(0)
payload = json.loads(path.read_text(encoding='utf-8'))
if payload.get('deployment_id') != os.environ['MAC_DEPLOY_STATE_ID']:
    raise SystemExit('refusing to remove another deployment release state')
path.unlink()
PY"
  release_remote_deployment_lock "$agent" "$deployment_id"
}

refresh_release_ready_quiescence() {
  local ready_file agent deployment_id observed
  for ready_file in "$TMPDIR_LOCAL"/release-ready-agent_*.json; do
    [ -f "$ready_file" ] || continue
    agent="$($PYTHON_BIN -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("agent") or "")' "$ready_file")"
    deployment_id="$($PYTHON_BIN -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("deployment_id") or "")' "$ready_file")"
    [ -n "$agent" ] && [ -n "$deployment_id" ] || {
      echo "invalid release readiness identity: $ready_file" >&2
      return 1
    }
    observed="$(remote_daemon_quiescence_attestation "$agent" "$deployment_id")"
    "$PYTHON_BIN" - "$ready_file" "$observed" <<'PY'
import json
import os
import tempfile
import sys
from pathlib import Path

path = Path(sys.argv[1])
observed = json.loads(sys.argv[2])
payload = json.loads(path.read_text(encoding="utf-8"))
initial = payload.get("quiescence")
stable_keys = (
    "schema",
    "agent",
    "receipt_sha256",
    "phase1_receipt_sha256",
    "phase1_daemon_receipt_sha256",
    "phase1_function_block_sha256",
    "phase1_supervisor",
    "generation",
    "revision",
    "gateway_implementation",
    "gateway_readiness_sha256",
    "gateway_supervisor",
    "gateway_identities",
    "required_phases",
    "container_runtimes",
    "stable_absence_observations",
)
if not isinstance(initial, dict) or not isinstance(observed, dict):
    raise SystemExit("release readiness lacks daemon quiescence evidence")
if any(initial.get(key) != observed.get(key) for key in stable_keys):
    raise SystemExit("daemon quiescence identity changed before fleet commit")
if not isinstance(observed.get("observed_at"), str) or not observed["observed_at"]:
    raise SystemExit("fleet commit daemon proof lacks an observation time")
payload["commit_quiescence"] = observed
fd, raw = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
tmp = Path(raw)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, path)
finally:
    tmp.unlink(missing_ok=True)
PY
  done
}

query_hub_release_epoch_status() {
  local hub_agent="$1" plan="$2"
  local plan_b64 ssh_parts=() ssh_args=() ssh_target item last_index
  plan_b64="$("$PYTHON_BIN" - "$plan" <<'PY'
import base64
import sys

print(base64.b64encode(open(sys.argv[1], "rb").read()).decode("ascii"))
PY
)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_RELEASE_PLAN_B64=$(shell_quote "$plan_b64") bash -s" <<'REMOTE_RELEASE_STATUS'
set -euo pipefail
set -a
. "$HOME/.mac/mac.env"
set +a
export MAC_DEPLOY_GATE_ADMIN_TOKEN="${MAC_API_TOKEN:?}"
"$HOME/.mac/venv/bin/python" - <<'PY'
import base64
import hashlib
import json
import os
import urllib.parse
import urllib.request

plan = json.loads(base64.b64decode(os.environ["MAC_DEPLOY_RELEASE_PLAN_B64"]))
entries = plan.get("agents")
epoch_id = str(plan.get("epoch_id") or "")
if plan.get("schema") != "mac.fleet_release_epoch.v1" or not isinstance(entries, list) or not epoch_id:
    raise SystemExit("durable release plan is invalid")
successor_reason = plan.get("successor_hold_reason")
if successor_reason is not None:
    successor_reason = str(successor_reason).strip()
normalized = sorted(
    (
        str(item.get("agent_id") or ""),
        str(item.get("hold_reason") or ""),
        item,
    )
    for item in entries
    if isinstance(item, dict) and item.get("owns_hold")
)
if any(not agent_id or not reason for agent_id, reason, _item in normalized):
    raise SystemExit("durable release plan contains an invalid hold identity")
expectations = [
    {
        "agent_id": agent_id,
        "generation": item.get("generation"),
        "baseline_seen": str(item.get("baseline_seen") or "").strip(),
        "principal_id": item.get("principal_id"),
        "require_authenticated": bool(item.get("require_authenticated")),
        "require_report_executor": bool(item.get("require_report_executor")),
    }
    for agent_id, _reason, item in normalized
]
identity = {
    "epoch_id": epoch_id,
    "holds": [
        {"agent_id": agent_id, "hold_reason": reason}
        for agent_id, reason, _item in normalized
    ],
    "outcome": "successor_hold" if successor_reason is not None else "released",
    "successor_hold_reason": successor_reason,
    "expectations": expectations,
}
identity_sha256 = hashlib.sha256(
    json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
hub_url = str(os.environ.get("MAC_HUB_URL") or "").rstrip("/")
token = os.environ["MAC_DEPLOY_GATE_ADMIN_TOKEN"]
url = "%s/agents/dispatch-hold/epochs/%s?%s" % (
    hub_url,
    urllib.parse.quote(epoch_id, safe=""),
    urllib.parse.urlencode({"identity_sha256": identity_sha256}),
)
request = urllib.request.Request(
    url,
    method="GET",
    headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
)
with urllib.request.urlopen(request, timeout=20) as response:
    payload = json.loads(response.read())
if (
    not isinstance(payload, dict)
    or payload.get("status") not in {"absent", "committed", "mismatch"}
    or payload.get("epoch_id") != epoch_id
    or payload.get("identity_sha256") != identity_sha256
):
    raise SystemExit("hub returned an invalid release epoch status")
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
PY
REMOTE_RELEASE_STATUS
}

run_fenced_remote_python() {
  local agent="$1" deployment_id="$2" code="$3"
  shift 3
  local ssh_parts=() ssh_args=() ssh_target item last_index remote_cmd
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  remote_cmd="$(remote_deployment_fenced_exec \
    "$deployment_id" 1 python3 -c "$code" "$@")"
  stream_file_after_remote_fence /dev/null \
    "MAC_DEPLOY_FENCE_READY:${deployment_id}" \
    ssh -o BatchMode=yes -o ConnectTimeout=10 \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
      "${ssh_args[@]}" "$ssh_target" "$remote_cmd"
}

probe_remote_cohort_recovery_action() {
  local agent="$1" deployment_id="$2" source_commit="$3" deploy_ts="$4"
  local expected_restore_sha256="${5:-}"
  local code
  code="$(command cat <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

agent, generation, revision, deploy_ts, journal_contract_sha256 = sys.argv[1:]
mac_home = Path.home() / ".mac"


def private_bytes(path, mode, limit, *, optional=False):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError:
        if optional:
            return None
        raise SystemExit("required recovery contract is unavailable")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size <= 0
            or before.st_size > limit
        ):
            raise SystemExit("recovery contract owner, mode, or size is invalid")
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or (
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
            raise SystemExit("recovery contract changed while reading")
        return raw
    finally:
        os.close(descriptor)


manifest_path = mac_home / "logs" / ("deploy-manifest-%s-post.json" % deploy_ts)
manifest_raw = private_bytes(manifest_path, 0o600, 4 * 1024 * 1024, optional=True)
if manifest_raw is not None:
    try:
        manifest = json.loads(manifest_raw)
    except (TypeError, ValueError):
        raise SystemExit("phase-2 post manifest is malformed")
    deploy = manifest.get("deploy") if isinstance(manifest, dict) else None
    rollback = manifest.get("rollback") if isinstance(manifest, dict) else None
    if (
        manifest.get("stage") != "post"
        or not isinstance(deploy, dict)
        or deploy.get("generation") != generation
        or deploy.get("mac_git_rev") != revision
        or not isinstance(rollback, dict)
        or rollback.get("schema") != "mac.fleet_node_rollback_contract.v1"
        or rollback.get("status") != "armed"
        or rollback.get("generation") != generation
        or rollback.get("revision") != revision
        or not isinstance(rollback.get("path"), str)
        or not isinstance(rollback.get("sha256"), str)
        or not isinstance(rollback.get("completion_receipt"), str)
    ):
        raise SystemExit("phase-2 post manifest belongs to another generation")
    script_raw = private_bytes(Path(rollback["path"]), 0o700, 2 * 1024 * 1024)
    if hashlib.sha256(script_raw).hexdigest() != rollback["sha256"]:
        raise SystemExit("phase-2 rollback executable digest differs")
    print(json.dumps({"action": "rollback"}, sort_keys=True))
    raise SystemExit(0)

receipt_path = mac_home / ("phase1-cohort-quiescence-%s.json" % generation)
receipt_raw = private_bytes(receipt_path, 0o600, 4 * 1024 * 1024, optional=True)
if receipt_raw is None:
    print(json.dumps({"action": "probe"}, sort_keys=True))
    raise SystemExit(0)
try:
    receipt = json.loads(receipt_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-1 receipt is malformed")
contract_path = mac_home / ("phase1-cohort-restore-contract-%s.json" % generation)
contract_raw = private_bytes(contract_path, 0o600, 4 * 1024 * 1024)
contract_sha256 = hashlib.sha256(contract_raw).hexdigest()
try:
    contract = json.loads(contract_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-1 restore contract is malformed")
if (
    receipt.get("schema") != "mac.phase1_cohort_quiescence.v1"
    or receipt.get("agent") != agent
    or receipt.get("generation") != generation
    or receipt.get("revision") != revision
    or receipt.get("source_contract_sha256") != contract_sha256
    or contract.get("schema") != "mac.phase1_cohort_restore_contract.v1"
    or contract.get("agent") != agent
    or contract.get("generation") != generation
    or contract.get("revision") != revision
    or contract.get("rollback_capable") is not True
):
    raise SystemExit("phase-1 recovery identity differs")
if journal_contract_sha256 and contract_sha256 != journal_contract_sha256:
    raise SystemExit("phase-1 restore contract differs from the durable journal")
print(
    json.dumps(
        {
            "action": "phase1_restore",
            "restore_contract_sha256": contract_sha256,
        },
        sort_keys=True,
    )
)
PY
)"
  run_fenced_remote_python "$agent" "$deployment_id" "$code" \
    "$agent" "$deployment_id" "$source_commit" "$deploy_ts" \
    "$expected_restore_sha256"
}

restore_remote_phase1_generation() {
  local agent="$1" deployment_id="$2" source_commit="$3" fleet_name="$4"
  local os_kind="$5" supervisor="$6" restore_contract_sha256="$7"
  local code
  code="$(command cat <<'PY'
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

agent, generation, revision, fleet, os_kind, supervisor, expected_sha256 = sys.argv[1:]
mac_home = Path.home() / ".mac"


def open_private(path, mode, limit):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_size <= 0
        or before.st_size > limit
    ):
        os.close(descriptor)
        raise SystemExit("phase-1 recovery artifact is not private and bounded")
    raw = os.read(descriptor, before.st_size + 1)
    after = os.fstat(descriptor)
    if len(raw) != before.st_size or (
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
        os.close(descriptor)
        raise SystemExit("phase-1 recovery artifact changed while reading")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor, raw


contract_path = mac_home / ("phase1-cohort-restore-contract-%s.json" % generation)
contract_fd, contract_raw = open_private(contract_path, 0o600, 4 * 1024 * 1024)
os.close(contract_fd)
if hashlib.sha256(contract_raw).hexdigest() != expected_sha256:
    raise SystemExit("phase-1 restore contract differs from the durable journal")
try:
    contract = json.loads(contract_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-1 restore contract is malformed")
restore = contract.get("restore_executable") if isinstance(contract, dict) else None
if (
    contract.get("schema") != "mac.phase1_cohort_restore_contract.v1"
    or contract.get("agent") != agent
    or contract.get("generation") != generation
    or contract.get("revision") != revision
    or contract.get("rollback_capable") is not True
    or not isinstance(restore, dict)
    or restore.get("argv") != [restore.get("path"), "restore"]
):
    raise SystemExit("phase-1 restore contract belongs to another generation")
restore_fd, restore_raw = open_private(Path(restore["path"]), 0o700, 8 * 1024 * 1024)
if hashlib.sha256(restore_raw).hexdigest() != restore.get("sha256"):
    os.close(restore_fd)
    raise SystemExit("phase-1 restore executable digest differs")
environment = os.environ.copy()
environment.update(
    {
        "AGENT": agent,
        "FLEET_NAME": fleet,
        "OS_KIND": os_kind,
        "DEPLOY_REV": revision,
        "DEPLOY_GENERATION": generation,
        "SUPERVISOR_KIND": supervisor,
        "MAC_HOME": str(mac_home),
        "PY": sys.executable,
        "MAC_PHASE1_RESTORE_CONTRACT_SHA256": expected_sha256,
    }
)
result = subprocess.run(
    ["/bin/bash", "/dev/fd/%d" % restore_fd, "restore"],
    env=environment,
    pass_fds=(restore_fd,),
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    check=False,
)
os.close(restore_fd)
if result.returncode != 0:
    raise SystemExit(result.returncode)
receipt_path = Path(str(contract.get("restore_receipt") or ""))
receipt_fd, receipt_raw = open_private(receipt_path, 0o600, 4 * 1024 * 1024)
os.close(receipt_fd)
try:
    receipt = json.loads(receipt_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-1 restore receipt is malformed")
if (
    receipt.get("schema") != "mac.phase1_cohort_restore.v1"
    or receipt.get("status") != "restored"
    or receipt.get("agent") != agent
    or receipt.get("generation") != generation
    or receipt.get("revision") != revision
    or receipt.get("source_contract_sha256") != expected_sha256
):
    raise SystemExit("phase-1 restore receipt differs from the journal contract")
sys.stdout.buffer.write(receipt_raw)
PY
)"
  run_fenced_remote_python "$agent" "$deployment_id" "$code" \
    "$agent" "$deployment_id" "$source_commit" "$fleet_name" \
    "$os_kind" "$supervisor" "$restore_contract_sha256"
}

rollback_remote_phase2_generation() {
  local agent="$1" deployment_id="$2" source_commit="$3" deploy_ts="$4"
  local code
  code="$(command cat <<'PY'
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

generation, revision, deploy_ts = sys.argv[1:]
mac_home = Path.home() / ".mac"


def open_private(path, mode, limit):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_size <= 0
        or before.st_size > limit
    ):
        os.close(descriptor)
        raise SystemExit("phase-2 rollback artifact is not private and bounded")
    raw = os.read(descriptor, before.st_size + 1)
    after = os.fstat(descriptor)
    if len(raw) != before.st_size or (
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
        os.close(descriptor)
        raise SystemExit("phase-2 rollback artifact changed while reading")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor, raw


manifest_path = mac_home / "logs" / ("deploy-manifest-%s-post.json" % deploy_ts)
manifest_fd, manifest_raw = open_private(manifest_path, 0o600, 4 * 1024 * 1024)
os.close(manifest_fd)
manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
try:
    manifest = json.loads(manifest_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-2 post manifest is malformed")
deploy = manifest.get("deploy") if isinstance(manifest, dict) else None
contract = manifest.get("rollback") if isinstance(manifest, dict) else None
if (
    manifest.get("stage") != "post"
    or not isinstance(deploy, dict)
    or deploy.get("generation") != generation
    or deploy.get("mac_git_rev") != revision
    or not isinstance(contract, dict)
    or contract.get("schema") != "mac.fleet_node_rollback_contract.v1"
    or contract.get("status") != "armed"
    or contract.get("generation") != generation
    or contract.get("revision") != revision
):
    raise SystemExit("phase-2 rollback contract belongs to another generation")
script_fd, script_raw = open_private(Path(contract.get("path") or ""), 0o700, 2 * 1024 * 1024)
if hashlib.sha256(script_raw).hexdigest() != contract.get("sha256"):
    os.close(script_fd)
    raise SystemExit("phase-2 rollback executable digest differs")
result = subprocess.run(
    ["/bin/bash", "/dev/fd/%d" % script_fd],
    pass_fds=(script_fd,),
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    check=False,
)
os.close(script_fd)
if result.returncode != 0:
    raise SystemExit(result.returncode)
receipt_fd, receipt_raw = open_private(
    Path(contract.get("completion_receipt") or ""), 0o600, 4 * 1024 * 1024
)
os.close(receipt_fd)
try:
    receipt = json.loads(receipt_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-2 rollback receipt is malformed")
if (
    receipt.get("schema") != "mac.fleet_node_rollback.v1"
    or receipt.get("status") != "restored"
    or receipt.get("generation") != generation
    or receipt.get("revision") != revision
    or receipt.get("post_manifest_sha256") != manifest_sha256
    or receipt.get("rollback_sha256") != contract.get("sha256")
):
    raise SystemExit("phase-2 rollback receipt differs from the generation contract")
sys.stdout.buffer.write(receipt_raw)
PY
)"
  run_fenced_remote_python "$agent" "$deployment_id" "$code" \
    "$deployment_id" "$source_commit" "$deploy_ts"
}

write_cohort_recovery_probe_evidence() {
  local output="$1" agent="$2" generation="$3" action="$4"
  "$PYTHON_BIN" - "$output" "$agent" "$generation" "$action" <<'PY'
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

output = Path(sys.argv[1])
payload = {
    "schema": "mac.fleet_node_recovery_probe.v1",
    "status": "no_mutation_required",
    "agent": sys.argv[2],
    "generation": sys.argv[3],
    "action": sys.argv[4],
    "observed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
}
descriptor, temporary_raw = tempfile.mkstemp(prefix=output.name + ".", dir=str(output.parent))
temporary = Path(temporary_raw)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
finally:
    temporary.unlink(missing_ok=True)
PY
}

recover_cohort_node() {
  local epoch_id="$1" owner_nonce="$2" fleet_name="$3" candidate_b64="$4"
  local values agent stable_id runtime_generation deployment_id deploy_ts source_commit
  local os_kind supervisor requested_action restore_contract_sha256 probe action evidence
  local -a candidate_values=()
  values="$("$PYTHON_BIN" - "$candidate_b64" <<'PY'
import base64
import json
import sys

payload = json.loads(base64.b64decode(sys.argv[1]))
for key in (
    "agent_name",
    "stable_id",
    "generation",
    "deployment_id",
    "deploy_ts",
    "source_commit",
    "os",
    "supervisor",
    "recovery_action",
):
    print(payload.get(key) or "")
print(payload.get("restore_contract_sha256") or "")
PY
)"
  mapfile -t candidate_values <<<"$values"
  agent="${candidate_values[0]:-}"
  stable_id="${candidate_values[1]:-}"
  runtime_generation="${candidate_values[2]:-}"
  deployment_id="${candidate_values[3]:-}"
  deploy_ts="${candidate_values[4]:-}"
  source_commit="${candidate_values[5]:-}"
  os_kind="${candidate_values[6]:-}"
  supervisor="${candidate_values[7]:-}"
  requested_action="${candidate_values[8]:-}"
  restore_contract_sha256="${candidate_values[9]:-}"
  [ -n "$agent" ] && [ -n "$stable_id" ] && [ -n "$runtime_generation" ] \
    && [ -n "$deployment_id" ] && [ -n "$source_commit" ] || {
      echo "ERROR: durable cohort recovery candidate is incomplete" >&2
      return 1
    }
  # Never use ambient stale-takeover authority during recovery. A successor
  # deployment lock is proof that this controller no longer owns the node.
  acquire_remote_deployment_lock "$agent" "$deployment_id" 0
  action="$requested_action"
  case "$action" in
    cleanup_only|phase1_restore|phase2_rollback) ;;
    *) echo "ERROR: ${agent}: recovery probe returned an invalid action" >&2; return 1 ;;
  esac
  cohort_journal_mutate abort-start "$epoch_id" \
    "$COHORT_JOURNAL_REVISION" "abort-start-${stable_id}" "$owner_nonce" \
    --agent-name "$agent" --stable-id "$stable_id" \
    --generation "$runtime_generation" --recovery-action "$action" >/dev/null
  evidence="$TMPDIR_LOCAL/cohort-recovery-${stable_id}.json"
  case "$action" in
    phase2_rollback)
      rollback_remote_phase2_generation \
        "$agent" "$deployment_id" "$source_commit" "$deploy_ts" > "$evidence"
      chmod 0600 "$evidence"
      ;;
    phase1_restore)
      [ -n "$restore_contract_sha256" ] || {
        echo "ERROR: ${agent}: phase-1 recovery lacks its journal-bound contract digest" >&2
        return 1
      }
      restore_remote_phase1_generation \
        "$agent" "$deployment_id" "$source_commit" "$fleet_name" \
        "$os_kind" "$supervisor" "$restore_contract_sha256" > "$evidence"
      chmod 0600 "$evidence"
      ;;
    cleanup_only)
      write_cohort_recovery_probe_evidence \
        "$evidence" "$agent" "$runtime_generation" "$action"
      ;;
  esac
  # Make lock retirement part of the retryable node operation. If the process
  # dies after this unlink, the next pass reacquires the same old identity,
  # replays the idempotent restore receipt, and retires it again.
  release_remote_deployment_lock "$agent" "$deployment_id"
  cohort_journal_mutate aborted-node "$epoch_id" \
    "$COHORT_JOURNAL_REVISION" "aborted-${stable_id}" "$owner_nonce" \
    --agent-name "$agent" --stable-id "$stable_id" \
    --generation "$runtime_generation" --evidence-file "$evidence" >/dev/null
  echo "==> ${agent}: durable cohort recovery completed with ${action}"
}

recover_active_cohort_transaction() {
  local epoch_id="$1" owner_nonce="${2:-$DEPLOY_CONTROLLER_NONCE}"
  local status recovery state hub_agent fleet_name release_plan status_result outcome
  local absence_proof candidate_b64
  status="$(cohort_journal status --epoch "$epoch_id")"
  COHORT_JOURNAL_REVISION="$(printf '%s' "$status" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["journal"]["revision"])')"
  hub_agent="$(printf '%s' "$status" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["journal"]["hub_agent"])')"
  fleet_name="$(printf '%s' "$status" | "$PYTHON_BIN" -c \
    'import json,sys; print(json.load(sys.stdin)["journal"]["fleet"])')"
  recovery="$(cohort_journal recovery --epoch "$epoch_id")"
  state="$(printf '%s' "$recovery" | "$PYTHON_BIN" -c \
    'import json,sys; print((json.load(sys.stdin).get("epoch") or {}).get("state") or "")')"
  case "$state" in
    committed|aborted)
      COHORT_JOURNAL_ACTIVE=0
      return 0
      ;;
  esac
  if [ "$state" = committing ]; then
    release_plan="$(printf '%s' "$recovery" | "$PYTHON_BIN" -c \
      'import json,sys; print(json.load(sys.stdin).get("release_plan_path") or "")')"
    [ -n "$release_plan" ] || {
      echo "ERROR: committing cohort journal lacks its durable release plan" >&2
      return 1
    }
    status_result="$(query_hub_release_epoch_status "$hub_agent" "$release_plan")"
    outcome="$(printf '%s' "$status_result" | "$PYTHON_BIN" -c \
      'import json,sys; print(json.load(sys.stdin).get("status") or "")')"
    case "$outcome" in
      committed)
        cohort_journal_mutate commit "$epoch_id" \
          "$COHORT_JOURNAL_REVISION" commit-recovered "$owner_nonce" >/dev/null
        COHORT_JOURNAL_ACTIVE=0
        echo "==> fleet: recovered exact committed release epoch; rollback is forbidden"
        return 0
        ;;
      mismatch)
        echo "ERROR: hub release marker conflicts with the durable cohort plan; refusing both replay and rollback" >&2
        return 1
        ;;
      absent)
        absence_proof="$TMPDIR_LOCAL/fleet-release-epoch-absence.json"
        "$PYTHON_BIN" - "$absence_proof" "$status" "$status_result" <<'PY'
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

output = Path(sys.argv[1])
journal = json.loads(sys.argv[2])["journal"]
observed = json.loads(sys.argv[3])
release = journal.get("release_plan")
if observed.get("status") != "absent" or not isinstance(release, dict):
    raise SystemExit("release absence proof input is invalid")
payload = {
    "schema": "mac.fleet_release_epoch_absence.v1",
    "status": "absent",
    "epoch_id": release.get("epoch_id"),
    "release_plan_sha256": release.get("sha256"),
    "observed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
}
descriptor, temporary_raw = tempfile.mkstemp(prefix=output.name + ".", dir=str(output.parent))
temporary = Path(temporary_raw)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
finally:
    temporary.unlink(missing_ok=True)
PY
        cohort_journal_mutate commit-absent "$epoch_id" \
          "$COHORT_JOURNAL_REVISION" commit-absent "$owner_nonce" \
          --evidence-file "$absence_proof" >/dev/null
        recovery="$(cohort_journal recovery --epoch "$epoch_id")"
        ;;
      *)
        echo "ERROR: hub returned an unknown release epoch status" >&2
        return 1
        ;;
    esac
  fi

  while IFS= read -r candidate_b64; do
    [ -n "$candidate_b64" ] || continue
    recover_cohort_node \
      "$epoch_id" "$owner_nonce" "$fleet_name" "$candidate_b64"
  done < <(printf '%s' "$status" | "$PYTHON_BIN" -c '
import base64,json,sys
status=json.load(sys.stdin)
journal=status["journal"]
recovery=json.loads(sys.argv[1])
by_name={item["name"]: item for item in journal["cohort"]}
for candidate in recovery.get("candidates") or []:
    node=by_name[candidate["agent_name"]]
    payload={**candidate, "os": node["os"], "supervisor": node["supervisor"]}
    print(base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode())
' "$recovery")
  cohort_journal_mutate abort "$epoch_id" \
    "$COHORT_JOURNAL_REVISION" abort "$owner_nonce" >/dev/null
  COHORT_JOURNAL_ACTIVE=0
  echo "==> fleet: incomplete cohort transaction was durably rolled back"
}

verify_cohort_recovery_routes() {
  local status_file="$1" recovery_file="$2" records_file="$TMPDIR_LOCAL/recovery-routes.txt"
  local role agent encoded expected observed comparison authority_file authority_id
  "$PYTHON_BIN" - "$status_file" "$recovery_file" > "$records_file" <<'PY'
import base64,json,sys
status=json.load(open(sys.argv[1],encoding="utf-8")); recovery=json.load(open(sys.argv[2],encoding="utf-8"))
journal=status["journal"]; records=[]
hub=recovery.get("hub_recovery") or {}; direction=recovery.get("direction")
if hub.get("action") != "none" or direction == "resolve_commit":
    records.append(("hub",hub.get("agent_name"),hub.get("route_identity")))
for key in ("candidates","finalization_candidates"):
    for item in recovery.get(key) or []:
        records.append(("node",item.get("agent_name"),item.get("route_identity")))
seen=set()
for role,agent,identity in records:
    key=(role,agent)
    if key in seen: continue
    seen.add(key)
    if not agent or not isinstance(identity,dict):
        raise SystemExit("recovery mutation lacks a journal-bound endpoint identity")
    encoded=base64.b64encode(json.dumps(identity,sort_keys=True,separators=(",",":")).encode()).decode()
    print("%s|%s|%s"%(role,agent,encoded))
PY
  while IFS='|' read -r role agent encoded; do
    [ -n "$agent" ] || continue
    start_ssh_control_master "$agent"
  done < "$records_file"
  SSH_CONTROL_REQUIRED=1
  while IFS='|' read -r role agent encoded; do
    [ -n "$agent" ] || continue
    expected="$TMPDIR_LOCAL/recovery-expected-${role}-${agent}.json"
    observed="$TMPDIR_LOCAL/recovery-observed-${role}-${agent}.json"
    comparison="$TMPDIR_LOCAL/recovery-comparison-${role}-${agent}.json"
    "$PYTHON_BIN" - "$encoded" "$expected" <<'PY'
import base64,json,os,sys
value=json.loads(base64.b64decode(sys.argv[1])); path=sys.argv[2]
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,"w",encoding="utf-8") as stream:
    json.dump(value,stream,sort_keys=True,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
PY
    if [ "$role" = hub ]; then
      authority_file="$TMPDIR_LOCAL/recovery-hub-authority.json"
      hub_epoch_client_read "$agent" "$authority_file" authority
      authority_id="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["hub_authority_id"])' "$authority_file")"
      write_live_endpoint_identity "$agent" "$observed" "$authority_id"
    else
      write_live_endpoint_identity "$agent" "$observed"
    fi
    "$PYTHON_BIN" "$ENDPOINT_IDENTITY_HELPER" compare \
      --expected "$expected" --observed "$observed" > "$comparison"
    "$PYTHON_BIN" - "$comparison" "$agent" <<'PY'
import json,sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
if value.get("recovery_allowed") is not True or value.get("generic_route_recovery_allowed") is not True:
    raise SystemExit("%s: live endpoint differs from the journal-bound recovery authority"%sys.argv[2])
PY
  done < "$records_file"
}

recover_committed_cohort_node() {
  local epoch_id="$1" owner_nonce="$2" hub_agent="$3" candidate_b64="$4"
  local values agent stable_id generation deployment_id deploy_ts source_commit fleet finalizer_sha state report_required evidence
  local -a fields=()
  values="$("$PYTHON_BIN" - "$candidate_b64" <<'PY'
import base64,json,sys
value=json.loads(base64.b64decode(sys.argv[1]))
for key in ("agent_name","stable_id","generation","deployment_id","deploy_ts","source_commit","fleet","finalizer_sha256","state","report_executor_required"):
    print(value.get(key) or "")
PY
)"
  mapfile -t fields <<<"$values"
  agent="${fields[0]:-}"; stable_id="${fields[1]:-}"; generation="${fields[2]:-}"
  deployment_id="${fields[3]:-}"; deploy_ts="${fields[4]:-}"; source_commit="${fields[5]:-}"
  fleet="${fields[6]:-}"; finalizer_sha="${fields[7]:-}"; state="${fields[8]:-}"
  report_required="${fields[9]:-}"
  [ -n "$agent" ] && [ -n "$stable_id" ] && [ -n "$generation" ] \
    && [ -n "$deployment_id" ] && [ -n "$deploy_ts" ] && [ -n "$source_commit" ] \
    && [ -n "$fleet" ] && [ -n "$finalizer_sha" ] || {
      echo "ERROR: committed cohort finalization candidate is incomplete" >&2
      return 1
    }
  acquire_remote_deployment_lock "$agent" "$deployment_id" 0
  if [ "$report_required" = True ] || [ "$report_required" = true ] || [ "$report_required" = 1 ]; then
    reconcile_report_repository_executor_approval "$agent" "$hub_agent" 1
  fi
  cohort_journal_mutate finalize-start "$epoch_id" \
    "$COHORT_JOURNAL_REVISION" "finalize-start-${stable_id}" "$owner_nonce" \
    --agent-name "$agent" --stable-id "$stable_id" --generation "$generation" >/dev/null
  evidence="$TMPDIR_LOCAL/recovered-finalize-${stable_id}.json"
  run_remote_node_finalizer "$agent" "$fleet" "$generation" "$source_commit" \
    "$deploy_ts" "$finalizer_sha" "$evidence" "$deployment_id"
  validate_finalize_evidence "$evidence" "$agent" "$generation"
  finalize_remote_deployment_release "$agent" "$deployment_id"
  cohort_journal_mutate finalized-node "$epoch_id" \
    "$COHORT_JOURNAL_REVISION" "finalized-${stable_id}" "$owner_nonce" \
    --agent-name "$agent" --stable-id "$stable_id" --generation "$generation" \
    --evidence-file "$evidence" >/dev/null
  echo "==> ${agent}: committed generation finalization recovered"
}

discard_unopened_epoch_pending_credentials() {
  local hub_agent="$1" status_file="$2" epoch_id="$3" agent agent_id remote_manifest command
  local ssh_parts=() ssh_args=() ssh_target item last_index
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#ssh_parts[@]} - 1)); ssh_target="${ssh_parts[$last_index]}"; ssh_args=("${ssh_parts[@]:0:$last_index}")
  while IFS='|' read -r agent agent_id; do
    [ -n "$agent" ] && [ -n "$agent_id" ] || continue
    command='set -e; set -a; . "$HOME/.mac/mac.env"; set +a; "$HOME/.mac/venv/bin/python" -m mac.worker_credentials discard-unreserved-pending'
    command+=" --agent-id $(shell_quote "$agent_id")"
    command+=" --created-by $(shell_quote "fleet-release:${epoch_id}")"
    ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
      "${ssh_args[@]}" "$ssh_target" "$command" >/dev/null
    remote_manifest="$(pending_worker_remote_manifest_path "$agent" "$epoch_id")"
    ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
      "${ssh_args[@]}" "$ssh_target" "rm -f $(shell_quote "$remote_manifest")" >/dev/null
  done < <("$PYTHON_BIN" - "$status_file" <<'PY'
import json,sys
for node in json.load(open(sys.argv[1],encoding="utf-8"))["journal"]["cohort"]:
    print("%s|%s"%(node["name"],node["stable_id"]))
PY
)
}

recover_active_cohort_transaction_v2() {
  local epoch_id="$1" owner_nonce="${2:-$DEPLOY_CONTROLLER_NONCE}"
  local status_file="$TMPDIR_LOCAL/recovery-status.json" recovery_file="$TMPDIR_LOCAL/recovery-plan.json"
  local status recovery direction hub_action hub_agent identity receipt outcome candidate_b64 fleet_name
  umask 077
  status="$(cohort_journal status --epoch "$epoch_id")"
  printf '%s\n' "$status" > "$status_file"; chmod 0600 "$status_file"
  COHORT_JOURNAL_REVISION="$(printf '%s' "$status" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["journal"]["revision"])')"
  fleet_name="$(printf '%s' "$status" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["journal"]["fleet"])')"
  recovery="$(cohort_journal recovery --epoch "$epoch_id")"
  printf '%s\n' "$recovery" > "$recovery_file"; chmod 0600 "$recovery_file"
  direction="$(printf '%s' "$recovery" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("direction") or "none")')"
  if [ "$direction" = none ]; then
    COHORT_JOURNAL_ACTIVE=0
    return 0
  fi
  verify_cohort_recovery_routes "$status_file" "$recovery_file"
  hub_action="$(printf '%s' "$recovery" | "$PYTHON_BIN" -c 'import json,sys; print((json.load(sys.stdin).get("hub_recovery") or {}).get("action") or "none")')"
  hub_agent="$(printf '%s' "$recovery" | "$PYTHON_BIN" -c 'import json,sys; print((json.load(sys.stdin).get("hub_recovery") or {}).get("agent_name") or "")')"
  identity="$(printf '%s' "$recovery" | "$PYTHON_BIN" -c 'import json,sys; print((json.load(sys.stdin).get("hub_recovery") or {}).get("identity_sha256") or "")')"

  if [ "$direction" = resolve_commit ]; then
    [ -n "$identity" ] || { echo "ERROR: commit recovery lacks hub identity" >&2; return 1; }
    receipt="$TMPDIR_LOCAL/recovered-hub-commit.json"
    read_hub_epoch_status_exact "$hub_agent" "$epoch_id" "$identity" "$receipt"
    outcome="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["status"])' "$receipt")"
    case "$outcome" in
      committed) ;;
      proved) commit_hub_epoch_exact "$hub_agent" "$epoch_id" "$identity" "$receipt" ;;
      *) echo "ERROR: durable commit intent cannot be resolved from hub status ${outcome}" >&2; return 1 ;;
    esac
    cohort_journal_mutate commit "$epoch_id" "$COHORT_JOURNAL_REVISION" \
      commit-recovered "$owner_nonce" --evidence-file "$receipt" >/dev/null
    status="$(cohort_journal status --epoch "$epoch_id")"
    printf '%s\n' "$status" > "$status_file"
    recovery="$(cohort_journal recovery --epoch "$epoch_id")"
    printf '%s\n' "$recovery" > "$recovery_file"
    verify_cohort_recovery_routes "$status_file" "$recovery_file"
    direction=finalize
  fi

  if [ "$direction" = rollback ]; then
    receipt="$TMPDIR_LOCAL/recovered-hub-transition.json"
    case "$hub_action" in
      resolve_open)
        replay_hub_epoch_recovery_request "$hub_agent" "$epoch_id" open "$receipt"
        cohort_journal_mutate hub-opened "$epoch_id" "$COHORT_JOURNAL_REVISION" \
          hub-opened-recovered "$owner_nonce" --evidence-file "$receipt" >/dev/null
        remove_hub_epoch_recovery_request "$hub_agent" "$epoch_id" open
        identity="$(hub_receipt_identity_sha256 "$receipt")"
        ;;
      resolve_prove)
        replay_hub_epoch_recovery_request "$hub_agent" "$epoch_id" prove "$receipt"
        cohort_journal_mutate hub-proved "$epoch_id" "$COHORT_JOURNAL_REVISION" \
          hub-proved-recovered "$owner_nonce" --evidence-file "$receipt" >/dev/null
        remove_hub_epoch_recovery_request "$hub_agent" "$epoch_id" prove
        identity="$(hub_receipt_identity_sha256 "$receipt")"
        ;;
      abort_epoch) ;;
      none) ;;
      *) echo "ERROR: unsupported hub recovery action ${hub_action}" >&2; return 1 ;;
    esac
    if [ "$hub_action" != none ]; then
      [ -n "$identity" ] || { echo "ERROR: hub abort recovery lacks exact identity" >&2; return 1; }
      abort_hub_epoch_exact "$hub_agent" "$epoch_id" "$identity" "$receipt"
      cohort_journal_mutate hub-aborted "$epoch_id" "$COHORT_JOURNAL_REVISION" \
        hub-aborted-recovered "$owner_nonce" --evidence-file "$receipt" >/dev/null
    fi
    discard_unopened_epoch_pending_credentials \
      "$hub_agent" "$status_file" "$epoch_id"
    remove_hub_epoch_recovery_request "$hub_agent" "$epoch_id" open
    remove_hub_epoch_recovery_request "$hub_agent" "$epoch_id" prove
    while IFS= read -r candidate_b64; do
      [ -n "$candidate_b64" ] || continue
      recover_cohort_node "$epoch_id" "$owner_nonce" "$fleet_name" "$candidate_b64"
    done < <("$PYTHON_BIN" - "$status_file" "$recovery_file" <<'PY'
import base64,json,sys
status=json.load(open(sys.argv[1],encoding="utf-8")); recovery=json.load(open(sys.argv[2],encoding="utf-8"))
by_name={item["name"]:item for item in status["journal"]["cohort"]}
for candidate in recovery.get("candidates") or []:
    node=by_name[candidate["agent_name"]]
    value={**candidate,"os":node["os"],"supervisor":node["supervisor"]}
    print(base64.b64encode(json.dumps(value,sort_keys=True).encode()).decode())
PY
)
    cohort_journal_mutate abort "$epoch_id" "$COHORT_JOURNAL_REVISION" \
      abort-recovered "$owner_nonce" >/dev/null
    COHORT_JOURNAL_ACTIVE=0
    echo "==> fleet: incomplete typed cohort was durably rolled back"
    return 0
  fi

  recovery="$(cohort_journal recovery --epoch "$epoch_id")"
  while IFS= read -r candidate_b64; do
    [ -n "$candidate_b64" ] || continue
    recover_committed_cohort_node "$epoch_id" "$owner_nonce" "$hub_agent" "$candidate_b64"
  done < <(printf '%s' "$recovery" | "$PYTHON_BIN" -c '
import base64,json,sys
for value in json.load(sys.stdin).get("finalization_candidates") or []:
    print(base64.b64encode(json.dumps(value,sort_keys=True).encode()).decode())
')
  cohort_journal_mutate finalize "$epoch_id" "$COHORT_JOURNAL_REVISION" \
    finalize-recovered "$owner_nonce" >/dev/null
  COHORT_JOURNAL_ACTIVE=0
  echo "==> fleet: committed typed cohort finalization recovered"
}

recover_incomplete_cohort_transaction_before_deploy() {
  local discovered active_values epoch_id revision previous_nonce previous_pid alive adopted
  local -a active_fields=()
  discovered="$(cohort_journal discover)"
  active_values="$(printf '%s' "$discovered" | "$PYTHON_BIN" -c '
import json,sys
active=json.load(sys.stdin).get("active")
if active:
    print(active["epoch_id"])
    print(active["revision"])
    print(active["owner"]["nonce"])
    print(active["owner"]["pid"])
    print("1" if active["owner"]["alive"] else "0")
')"
  [ -n "$active_values" ] || return 0
  mapfile -t active_fields <<<"$active_values"
  epoch_id="${active_fields[0]:-}"
  revision="${active_fields[1]:-}"
  previous_nonce="${active_fields[2]:-}"
  previous_pid="${active_fields[3]:-}"
  alive="${active_fields[4]:-}"
  if [ "$alive" = 1 ]; then
    echo "ERROR: incomplete cohort epoch ${epoch_id} is still owned by live controller pid ${previous_pid}" >&2
    return 1
  fi
  adopted="$(cohort_journal adopt --epoch "$epoch_id" \
    --expected-revision "$revision" \
    --operation-id "adopt-${DEPLOY_CONTROLLER_NONCE}" \
    --previous-owner-nonce "$previous_nonce" \
    --new-owner-nonce "$DEPLOY_CONTROLLER_NONCE" \
    --new-owner-pid "$$")"
  COHORT_JOURNAL_REVISION="$(printf '%s' "$adopted" | cohort_journal_revision)"
  COHORT_JOURNAL_ACTIVE=1
  COHORT_RECOVERY_RUNNING=1
  if recover_active_cohort_transaction_v2 "$epoch_id" "$DEPLOY_CONTROLLER_NONCE"; then
    COHORT_RECOVERY_RUNNING=0
    return 0
  fi
  # Leave this set so the EXIT trap does not launch a second concurrent retry.
  return 1
}

commit_fleet_release_epoch() {
  local expected_count="$1" hub_agent="$2" selected_plan="$3"
  local require_release_all_selected="${4:-0}"
  local successor_hold_reason="${5:-}"
  local plan="$TMPDIR_LOCAL/fleet-release-plan.json"
  local quiescence_epoch_hash epoch_id
  refresh_release_ready_quiescence
  quiescence_epoch_hash="$($PYTHON_BIN - "$TMPDIR_LOCAL" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
evidence = []
for path in sorted(root.glob("release-ready-agent_*.json")):
    payload = json.loads(path.read_text(encoding="utf-8"))
    evidence.append(
        {
            "agent_id": payload.get("agent_id"),
            "quiescence": payload.get("quiescence"),
            "commit_quiescence": payload.get("commit_quiescence"),
        }
    )
print(
    hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
)
PY
)"
  epoch_id="${GIT_REV}:${TS}:${DEPLOY_CONTROLLER_NONCE}:${quiescence_epoch_hash}"
  "$PYTHON_BIN" - "$plan" "$expected_count" "$epoch_id" "$TMPDIR_LOCAL" \
    "$selected_plan" "$require_release_all_selected" "$successor_hold_reason" <<'PY'
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

output = Path(sys.argv[1])
expected_count = int(sys.argv[2])
epoch_id = sys.argv[3]
root = Path(sys.argv[4])
selected_plan = json.load(open(sys.argv[5], encoding="utf-8"))
require_release_all_selected = sys.argv[6] == "1"
successor_hold_reason = sys.argv[7]
if bool(selected_plan.get("require_release_all_selected")) != require_release_all_selected:
    raise SystemExit("selected hold plan and release policy disagree")
if successor_hold_reason and not require_release_all_selected:
    raise SystemExit("successor hold requires exact full-cohort release")
entries = []
for path in sorted(root.glob("release-ready-agent_*.json")):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "mac.deploy_release_ready.v1":
        raise SystemExit("invalid fleet release readiness record: %s" % path)
    entries.append(payload)
if len(entries) != expected_count:
    raise SystemExit(
        "fleet release epoch has %d readiness records, expected %d"
        % (len(entries), expected_count)
    )
agent_ids = [str(item.get("agent_id") or "") for item in entries]
if any(not value for value in agent_ids) or len(set(agent_ids)) != len(agent_ids):
    raise SystemExit("fleet release readiness records contain missing or duplicate agents")
selected_ids = sorted(
    str(item.get("agent_id") or "") for item in selected_plan.get("agents", [])
)
if sorted(agent_ids) != selected_ids:
    raise SystemExit("fleet release readiness does not match the exact selected agent set")
if require_release_all_selected and any(not item.get("owns_hold") for item in entries):
    raise SystemExit("exact full-cohort release includes a hold not owned by this deployment")
stable_quiescence_keys = (
    "schema",
    "agent",
    "receipt_sha256",
    "phase1_receipt_sha256",
    "phase1_daemon_receipt_sha256",
    "phase1_function_block_sha256",
    "phase1_supervisor",
    "generation",
    "revision",
    "gateway_implementation",
    "gateway_readiness_sha256",
    "gateway_supervisor",
    "gateway_identities",
    "required_phases",
    "container_runtimes",
    "stable_absence_observations",
)
now = dt.datetime.now(dt.timezone.utc)
for item in entries:
    initial = item.get("quiescence")
    committed = item.get("commit_quiescence")
    if not isinstance(initial, dict) or not isinstance(committed, dict):
        raise SystemExit("fleet release readiness lacks daemon quiescence evidence")
    if any(initial.get(key) != committed.get(key) for key in stable_quiescence_keys):
        raise SystemExit("fleet commit daemon evidence changed identity")
    if (
        committed.get("schema")
        != "mac.daemon_resource_quiescence_attestation.v1"
        or committed.get("agent") != item.get("agent")
        or committed.get("generation") != item.get("deployment_id")
        or committed.get("revision") != epoch_id.split(":", 1)[0]
        or committed.get("stable_absence_observations") != 2
        or not isinstance(committed.get("phase1_supervisor"), dict)
    ):
        raise SystemExit("fleet commit daemon evidence is invalid")
    for digest_key in (
        "receipt_sha256",
        "phase1_receipt_sha256",
        "phase1_daemon_receipt_sha256",
        "phase1_function_block_sha256",
        "gateway_readiness_sha256",
    ):
        digest = committed.get(digest_key)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise SystemExit("fleet commit contains an invalid evidence digest")
    try:
        observed = dt.datetime.fromisoformat(
            str(committed.get("observed_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise SystemExit("fleet commit daemon evidence lacks a valid clock") from exc
    age = (now - observed).total_seconds()
    if age < -30 or age > 180:
        raise SystemExit("fleet commit daemon evidence is stale")
evidence_digest = hashlib.sha256(
    json.dumps(
        [
            {
                "agent_id": item.get("agent_id"),
                "quiescence": item.get("quiescence"),
                "commit_quiescence": item.get("commit_quiescence"),
            }
            for item in entries
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
if epoch_id.rsplit(":", 1)[-1] != evidence_digest:
    raise SystemExit("fleet release epoch is not bound to daemon quiescence evidence")
payload = {
    "schema": "mac.fleet_release_epoch.v1",
    "epoch_id": epoch_id,
    "source_commit": epoch_id.split(":", 1)[0],
    "require_release_all_selected": require_release_all_selected,
    "successor_hold_reason": successor_hold_reason or None,
    "agents": entries,
}
fd, raw = tempfile.mkstemp(prefix=output.name + ".", dir=str(output.parent))
tmp = Path(raw)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, output)
finally:
    tmp.unlink(missing_ok=True)
PY

  # Persist the exact full release plan before the first POST.  From this
  # point a controller restart must query/replay this plan; rollback is not
  # permitted until the read-only hub epoch status proves it absent.
  cohort_journal_mutate commit-start "$COHORT_EPOCH_ID" \
    "$COHORT_JOURNAL_REVISION" commit-start "$DEPLOY_CONTROLLER_NONCE" \
    --release-plan-file "$plan" >/dev/null

  local plan_b64 hub_ssh_parts=() hub_ssh_args=() hub_ssh_target item last_index receipt
  local attempt commit_ok=0
  plan_b64="$($PYTHON_BIN - "$plan" <<'PY'
import base64
import sys
print(base64.b64encode(open(sys.argv[1], 'rb').read()).decode('ascii'))
PY
)"
  while IFS= read -r -d '' item; do hub_ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#hub_ssh_parts[@]} - 1))
  hub_ssh_target="${hub_ssh_parts[$last_index]}"
  hub_ssh_args=("${hub_ssh_parts[@]:0:$last_index}")
  echo "==> fleet: committing synchronized release epoch ${epoch_id}"
  for attempt in $(seq 1 "${MAC_DEPLOY_RELEASE_COMMIT_RETRIES:-3}"); do
    if receipt="$(ssh -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" \
    "MAC_DEPLOY_RELEASE_PLAN_B64=$(shell_quote "$plan_b64") MAC_DEPLOY_RELEASE_TS=$(shell_quote "$TS") bash -s" <<'REMOTE_RELEASE_EPOCH'
set -euo pipefail
set -a
. "$HOME/.mac/mac.env"
set +a
export MAC_DEPLOY_GATE_ADMIN_TOKEN="${MAC_API_TOKEN:?}"
"$HOME/.mac/venv/bin/python" - <<'PY'
import base64
import datetime as dt
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from mac.models import (
    agent_has_read_only_report_repository_executor,
    valid_read_only_report_repository_executor_attestation,
)

plan = json.loads(base64.b64decode(os.environ["MAC_DEPLOY_RELEASE_PLAN_B64"]))
if plan.get("schema") != "mac.fleet_release_epoch.v1":
    raise SystemExit("invalid fleet release epoch plan")
epoch_id = str(plan.get("epoch_id") or "")
entries = plan.get("agents")
require_release_all_selected = bool(plan.get("require_release_all_selected"))
successor_hold_reason = str(plan.get("successor_hold_reason") or "")
if not epoch_id or not isinstance(entries, list) or not entries:
    raise SystemExit("fleet release epoch plan is incomplete")
if successor_hold_reason and not require_release_all_selected:
    raise SystemExit("successor hold requires exact full-cohort release")
entry_ids = [str(item.get("agent_id") or "") for item in entries]
if any(not value for value in entry_ids) or len(set(entry_ids)) != len(entry_ids):
    raise SystemExit("fleet release epoch has missing or duplicate agent ids")
if require_release_all_selected and any(not item.get("owns_hold") for item in entries):
    raise SystemExit("exact full-cohort release contains an unowned hold")
stable_quiescence_keys = (
    "schema",
    "agent",
    "receipt_sha256",
    "phase1_receipt_sha256",
    "phase1_daemon_receipt_sha256",
    "phase1_function_block_sha256",
    "phase1_supervisor",
    "generation",
    "revision",
    "gateway_implementation",
    "gateway_readiness_sha256",
    "gateway_supervisor",
    "gateway_identities",
    "required_phases",
    "container_runtimes",
    "stable_absence_observations",
)
now = dt.datetime.now(dt.timezone.utc)
for item in entries:
    initial = item.get("quiescence")
    committed = item.get("commit_quiescence")
    if not isinstance(initial, dict) or not isinstance(committed, dict):
        raise SystemExit("fleet release epoch lacks daemon quiescence evidence")
    if any(initial.get(key) != committed.get(key) for key in stable_quiescence_keys):
        raise SystemExit("fleet release epoch daemon identity changed")
    if (
        committed.get("schema")
        != "mac.daemon_resource_quiescence_attestation.v1"
        or committed.get("agent") != item.get("agent")
        or committed.get("generation") != item.get("deployment_id")
        or committed.get("revision") != epoch_id.split(":", 1)[0]
        or committed.get("stable_absence_observations") != 2
        or not isinstance(committed.get("phase1_supervisor"), dict)
    ):
        raise SystemExit("fleet release epoch daemon evidence is invalid")
    for digest_key in (
        "receipt_sha256",
        "phase1_receipt_sha256",
        "phase1_daemon_receipt_sha256",
        "phase1_function_block_sha256",
        "gateway_readiness_sha256",
    ):
        digest = committed.get(digest_key)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise SystemExit("fleet release epoch contains an invalid evidence digest")
    try:
        observed = dt.datetime.fromisoformat(
            str(committed.get("observed_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise SystemExit("fleet release epoch daemon evidence has an invalid clock") from exc
    age = (now - observed).total_seconds()
    if age < -30 or age > 180:
        raise SystemExit("fleet release epoch daemon evidence is stale")
evidence_digest = hashlib.sha256(
    json.dumps(
        [
            {
                "agent_id": item.get("agent_id"),
                "quiescence": item.get("quiescence"),
                "commit_quiescence": item.get("commit_quiescence"),
            }
            for item in entries
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
if epoch_id.rsplit(":", 1)[-1] != evidence_digest:
    raise SystemExit("fleet release epoch is not bound to daemon evidence")
hub_url = str(os.environ.get("MAC_HUB_URL") or "").rstrip("/")
token = os.environ["MAC_DEPLOY_GATE_ADMIN_TOKEN"]


def api(method, path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        hub_url + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            "%s %s failed with HTTP %s: %s" % (method, path, exc.code, detail)
        ) from exc
    return json.loads(raw) if raw else None


def parse_seen(value):
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def report_executor_ready(agent_id, resources, required):
    if not required:
        return True
    startup = resources.get("startup_self_test")
    checks = startup.get("checks") if isinstance(startup, dict) else None
    attestation = resources.get("report_repository_executor_attestation")
    return bool(
        agent_has_read_only_report_repository_executor(resources)
        and valid_read_only_report_repository_executor_attestation(attestation)
        and isinstance(startup, dict)
        and startup.get("schema") == "mac.agent_startup_self_test.v1"
        and startup.get("agent_id") == agent_id
        and startup.get("status") in {"passed", "degraded"}
        and startup.get("blocking_problems") == []
        and isinstance(checks, dict)
        and checks.get("openshell_executor_config") is True
        and checks.get("report_repository_executor_attestation") is True
        and startup.get("report_repository_executor_attestation") == attestation
        and str(startup.get("timestamp") or "")
    )


rows = {}
for item in entries:
    agent_id = str(item.get("agent_id") or "")
    row = api("GET", "/agents/%s" % agent_id)
    if not isinstance(row, dict) or row.get("id") != agent_id:
        raise RuntimeError("release epoch received the wrong agent row")
    if not item.get("owns_hold"):
        # Operator-held workers are intentionally omitted from the atomic
        # release call. They still have to prove this epoch while retaining the
        # unrelated hold. Deployment-owned workers are validated inside the
        # hub transaction below, which also makes an epoch retry safe after a
        # committed response was lost.
        resources = row.get("resources") if isinstance(row.get("resources"), dict) else {}
        authenticated = resources.get("worker_credential_authenticated")
        auth_ok = not item.get("require_authenticated") or (
            isinstance(authenticated, dict)
            and authenticated.get("agent_id") == agent_id
            and authenticated.get("principal_id") == item.get("principal_id")
        )
        if not (
            parse_seen(row.get("last_seen_at")) > parse_seen(item.get("baseline_seen"))
            and row.get("status") == "idle"
            and row.get("health_status") == "healthy"
            and row.get("current_task_id") is None
            and bool(row.get("dispatch_hold"))
            and bool(row.get("dispatch_hold_reason"))
            and resources.get("deployment_generation") == item.get("generation")
            and auth_ok
            and report_executor_ready(
                agent_id,
                resources,
                bool(item.get("require_report_executor")),
            )
        ):
            raise RuntimeError(
                "operator-held agent %s lost release readiness before epoch commit"
                % agent_id
            )
    rows[agent_id] = row

holds = [
    {
        "agent_id": item["agent_id"],
        "reason": item["hold_reason"],
        "generation": item["generation"],
        "baseline_seen": item["baseline_seen"],
        "principal_id": item.get("principal_id") or None,
        "require_authenticated": bool(item.get("require_authenticated")),
        "require_report_executor": bool(item.get("require_report_executor")),
    }
    for item in entries
    if item.get("owns_hold")
]
if require_release_all_selected and len(holds) != len(entries):
    raise RuntimeError("exact full-cohort release does not own every selected hold")
if holds:
    if successor_hold_reason:
        response = api(
            "POST",
            "/agents/dispatch-hold/transition-batch",
            {
                "epoch_id": epoch_id,
                "successor_reason": successor_hold_reason,
                "holds": holds,
            },
        )
        if (
            not isinstance(response, dict)
            or response.get("transitioned") is not True
        ):
            raise RuntimeError("hub rejected the atomic fleet successor-hold epoch")
        if response.get("successor_reason") != successor_hold_reason:
            raise RuntimeError("hub returned the wrong successor hold reason")
    else:
        response = api(
            "POST",
            "/agents/dispatch-hold/release-batch",
            {"epoch_id": epoch_id, "holds": holds},
        )
        if not isinstance(response, dict) or response.get("released") is not True:
            raise RuntimeError("hub rejected the atomic fleet release epoch")
    if response.get("epoch_id") != epoch_id:
        raise RuntimeError("hub returned the wrong fleet release epoch id")
    committed_agents = response.get("agents")
    if not isinstance(committed_agents, list):
        raise RuntimeError("hub returned an invalid fleet release receipt")
    requested_ids = sorted(item["agent_id"] for item in holds)
    returned_ids = sorted(
        str(item.get("id") or "")
        for item in committed_agents
        if isinstance(item, dict)
    )
    if returned_ids != requested_ids or len(committed_agents) != len(holds):
        raise RuntimeError("hub returned the wrong fleet release agent set")
    if successor_hold_reason:
        if any(
            item.get("dispatch_hold") is not True
            or item.get("dispatch_hold_reason") != successor_hold_reason
            for item in committed_agents
        ):
            raise RuntimeError(
                "hub transition receipt lacks the exact successor hold"
            )
    elif any(
        item.get("dispatch_hold") is not False
        or item.get("dispatch_hold_reason") is not None
        for item in committed_agents
    ):
        raise RuntimeError("hub release receipt still reports a selected hold")

if require_release_all_selected:
    post_rows = {}
    for agent_id in sorted(entry_ids):
        row = api("GET", "/agents/%s" % agent_id)
        if not isinstance(row, dict) or row.get("id") != agent_id:
            raise RuntimeError("post-release verification received the wrong agent")
        if successor_hold_reason:
            if (
                row.get("dispatch_hold") is not True
                or row.get("dispatch_hold_reason") != successor_hold_reason
            ):
                raise RuntimeError(
                    "selected agent lacks the exact successor hold after fleet transition"
                )
        elif (
            row.get("dispatch_hold") is not False
            or row.get("dispatch_hold_reason") is not None
        ):
            raise RuntimeError("selected agent remained held after exact fleet release")
        post_rows[agent_id] = row
    rows = post_rows

receipt = {
    "schema": (
        "mac.fleet_release_receipt.v2"
        if successor_hold_reason
        else "mac.fleet_release_receipt.v1"
    ),
    "epoch_id": epoch_id,
    "source_commit": plan.get("source_commit"),
    "committed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "cohort_size": len(entries),
    "deployment_holds_released": len(holds),
    "operator_holds_preserved": len(entries) - len(holds),
    "agent_ids": sorted(rows),
}
if successor_hold_reason:
    receipt.update(
        {
            "outcome": "successor_hold",
            "successor_hold_reason": successor_hold_reason,
            "successor_holds_installed": len(holds),
        }
    )
if require_release_all_selected and not (
    receipt["deployment_holds_released"] == receipt["cohort_size"]
    and receipt["operator_holds_preserved"] == 0
    and receipt["agent_ids"] == sorted(entry_ids)
):
    raise RuntimeError("exact full-cohort release receipt failed its postconditions")
if successor_hold_reason and not (
    receipt["schema"] == "mac.fleet_release_receipt.v2"
    and receipt["outcome"] == "successor_hold"
    and receipt["successor_hold_reason"] == successor_hold_reason
    and receipt["successor_holds_installed"] == receipt["cohort_size"]
):
    raise RuntimeError("successor-hold fleet receipt failed its postconditions")
log_dir = Path.home() / ".mac" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
path = log_dir / ("fleet-release-epoch-%s.json" % os.environ["MAC_DEPLOY_RELEASE_TS"])
fd, raw = tempfile.mkstemp(prefix=path.name + ".", dir=str(log_dir))
tmp = Path(raw)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, path)
finally:
    tmp.unlink(missing_ok=True)
print(json.dumps(receipt, sort_keys=True))
PY
REMOTE_RELEASE_EPOCH
)"; then
      commit_ok=1
      break
    fi
    echo "==> fleet: release epoch response was not confirmed (attempt ${attempt}); retrying the same idempotency key" >&2
    sleep "$attempt"
  done
  if [ "$commit_ok" != 1 ]; then
    echo "==> fleet: ERROR: synchronized release epoch could not be confirmed; durable holds/receipts must be reconciled before retry" >&2
    return 1
  fi
  cohort_journal_mutate commit "$COHORT_EPOCH_ID" \
    "$COHORT_JOURNAL_REVISION" commit "$DEPLOY_CONTROLLER_NONCE" >/dev/null
  COHORT_JOURNAL_ACTIVE=0
  printf '%s\n' "$receipt" > "$TMPDIR_LOCAL/fleet-release-receipt.json"
  chmod 0600 "$TMPDIR_LOCAL/fleet-release-receipt.json"

  local ready_file values agent deployment_id
  for ready_file in "$TMPDIR_LOCAL"/release-ready-agent_*.json; do
    values="$($PYTHON_BIN - "$ready_file" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1], encoding='utf-8'))
print(payload['agent'])
print(payload['deployment_id'])
PY
)"
    agent="${values%%$'\n'*}"
    deployment_id="${values#*$'\n'}"
    if ! finalize_remote_deployment_release "$agent" "$deployment_id"; then
      echo "==> ${agent}: WARNING: epoch committed but deployment lock cleanup failed" >&2
    fi
  done
  if [ -n "$successor_hold_reason" ]; then
    echo "==> fleet: synchronized successor-hold epoch committed for ${expected_count} agent(s)"
  else
    echo "==> fleet: synchronized release epoch committed for ${expected_count} agent(s)"
  fi
}

enforce_bound_worker_credentials() {
  local hub_agent="$1" hub_ssh_parts=() hub_ssh_args=() hub_ssh_target item last_index
  case "$(printf '%s' "${MAC_DEPLOY_WORKER_IDENTITY_ENFORCE:-0}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) ;;
    *) return 0 ;;
  esac
  while IFS= read -r -d '' item; do hub_ssh_parts+=("$item"); done < <(ssh_target_args "$hub_agent")
  last_index=$((${#hub_ssh_parts[@]} - 1))
  hub_ssh_target="${hub_ssh_parts[$last_index]}"
  hub_ssh_args=("${hub_ssh_parts[@]:0:$last_index}")
  local policy_cmd
  policy_cmd='set -e; set -a; . "$HOME/.mac/mac.env"; set +a; "$HOME/.mac/venv/bin/python" -m mac.worker_credentials set-mode enforced --review-live'
  echo "==> fleet: requesting worker-identity enforcement after live 100% review"
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
    "${hub_ssh_args[@]}" "$hub_ssh_target" "$policy_cmd" >/dev/null
}

run_typed_cohort() {
  local selected_specs_file="$1" hub_agent="$2" hub_token="$3"
  local hub_tunnel_pubkey="$4" github_review_key_b64="$5" hold_adoption_plan="$6"
  local spec fields=() agent agent_id supervisor fleet_name os_kind generation
  local phase2_digest finalizer_digest direct_mesh_hub network_provider hub_url report_required evidence
  local -a phase2_arm_values=()

  preflight_cohort_hold_adoptions \
    "$selected_specs_file" "$hold_adoption_plan" "$hub_agent"

  echo "==> fleet: arming exact phase-1 restore contracts"
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a fields <<<"$spec"
    agent="${fields[0]}"; agent_id="$(stable_worker_agent_id "$agent")"
    supervisor="${fields[14]:-auto}"; fleet_name="${fields[23]:-mac}"; os_kind="${fields[2]}"
    generation="$(worker_generation_for_agent "$agent")"
    prepare_remote_phase1_restore_contract "$agent" \
      "$(deployment_id_for_agent "$agent")" "$supervisor" "$fleet_name" "$os_kind"
    cohort_journal_mutate phase1-armed "$COHORT_EPOCH_ID" \
      "$COHORT_JOURNAL_REVISION" "phase1-arm-${agent_id}" "$DEPLOY_CONTROLLER_NONCE" \
      --agent-name "$agent" --stable-id "$agent_id" --generation "$generation" \
      --restore-contract-sha256 "$(phase1_restore_contract_digest_for_agent "$agent")" \
      --evidence-file "$(phase1_restore_contract_file_for_agent "$agent")" >/dev/null
  done < "$selected_specs_file"

  echo "==> fleet: proving all read-only node prerequisites before hub or service mutation"
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    prepare_remote_prerequisite_bundle "$spec"
  done < "$selected_specs_file"

  build_and_open_hub_epoch "$selected_specs_file" "$hub_agent"

  echo "==> fleet: quiescing the exact cohort under hub epoch ownership"
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a fields <<<"$spec"
    agent="${fields[0]}"; agent_id="$(stable_worker_agent_id "$agent")"
    supervisor="${fields[14]:-auto}"; fleet_name="${fields[23]:-mac}"; os_kind="${fields[2]}"
    generation="$(worker_generation_for_agent "$agent")"
    cohort_journal_mutate quiesce-start "$COHORT_EPOCH_ID" \
      "$COHORT_JOURNAL_REVISION" "quiesce-start-${agent_id}" "$DEPLOY_CONTROLLER_NONCE" \
      --agent-name "$agent" --stable-id "$agent_id" --generation "$generation" >/dev/null
    quiesce_remote_agent_for_cohort "$agent" "$(deployment_id_for_agent "$agent")" \
      "$supervisor" "$fleet_name" "$os_kind"
    cohort_journal_mutate quiesced "$COHORT_EPOCH_ID" \
      "$COHORT_JOURNAL_REVISION" "quiesced-${agent_id}" "$DEPLOY_CONTROLLER_NONCE" \
      --agent-name "$agent" --stable-id "$agent_id" --generation "$generation" \
      --evidence-file "$TMPDIR_LOCAL/phase1-ready-${agent_id}.json" >/dev/null
  done < "$selected_specs_file"

  echo "==> fleet: installing immutable finalizers and arming phase-2 rollback"
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a fields <<<"$spec"
    agent="${fields[0]}"; agent_id="$(stable_worker_agent_id "$agent")"
    generation="$(worker_generation_for_agent "$agent")"
    install_remote_node_finalizer "$agent"
    deploy_host "$spec" "$hub_token" "$hub_tunnel_pubkey" 0 \
      "$github_review_key_b64" 0 1 arm-phase2 \
      "$(node_prerequisite_bundle_file "$agent")" \
      "$(node_prerequisite_expectations_file "$agent")" \
      "$(node_route_identity_sha256 "$agent")"
    mapfile -t phase2_arm_values < <(collect_phase2_arm_evidence "$agent")
    phase2_digest="${phase2_arm_values[0]:-}"
    finalizer_digest="${phase2_arm_values[1]:-}"
    [ -n "$phase2_digest" ] && [ -n "$finalizer_digest" ] || {
      echo "ERROR: ${agent}: phase-2 arm omitted rollback or finalizer binding" >&2
      return 1
    }
    cohort_journal_mutate phase2-armed "$COHORT_EPOCH_ID" \
      "$COHORT_JOURNAL_REVISION" "phase2-arm-${agent_id}" "$DEPLOY_CONTROLLER_NONCE" \
      --agent-name "$agent" --stable-id "$agent_id" --generation "$generation" \
      --rollback-intent-sha256 "$phase2_digest" \
      --finalizer-sha256 "$finalizer_digest" \
      --evidence-file "$TMPDIR_LOCAL/phase2-arm-${agent_id}.json" >/dev/null
  done < "$selected_specs_file"

  echo "==> fleet: applying and proving the held cohort"
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a fields <<<"$spec"
    agent="${fields[0]}"; agent_id="$(stable_worker_agent_id "$agent")"
    generation="$(worker_generation_for_agent "$agent")"
    supervisor="${fields[14]:-auto}"; fleet_name="${fields[23]:-mac}"
    network_provider="${fields[31]:-none}"; hub_url="${fields[7]:-}"
    direct_mesh_hub=0
    if [ "$agent" != "$hub_agent" ] \
      && uses_direct_mesh_hub "$network_provider" "$hub_url"; then
      direct_mesh_hub=1
    fi
    cohort_journal_mutate phase2-start "$COHORT_EPOCH_ID" \
      "$COHORT_JOURNAL_REVISION" "phase2-start-${agent_id}" "$DEPLOY_CONTROLLER_NONCE" \
      --agent-name "$agent" --stable-id "$agent_id" --generation "$generation" >/dev/null
    deploy_host "$spec" "$hub_token" "$hub_tunnel_pubkey" 0 \
      "$github_review_key_b64" "$direct_mesh_hub" 1 apply-phase2 \
      "$(node_prerequisite_bundle_file "$agent")" \
      "$(node_prerequisite_expectations_file "$agent")" \
      "$(node_route_identity_sha256 "$agent")"
    install_pending_worker_credential "$agent" "$supervisor" "$fleet_name"
    install_and_prove_attestation_candidate "$agent" "$supervisor" "$fleet_name"
    evidence="$TMPDIR_LOCAL/release-ready-${agent_id}.json"
    [ -s "$evidence" ] || {
      echo "ERROR: ${agent}: typed apply lacks prepared evidence" >&2
      return 1
    }
    cohort_journal_mutate prepared "$COHORT_EPOCH_ID" \
      "$COHORT_JOURNAL_REVISION" "prepared-${agent_id}" "$DEPLOY_CONTROLLER_NONCE" \
      --agent-name "$agent" --stable-id "$agent_id" --generation "$generation" \
      --evidence-file "$evidence" >/dev/null
  done < "$selected_specs_file"

  prove_and_commit_hub_epoch "$selected_specs_file" "$hub_agent"
  cleanup_committed_hub_relay "$hub_agent" "$selected_specs_file"

  echo "==> fleet: finalizing committed node generations"
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a fields <<<"$spec"
    agent="${fields[0]}"; agent_id="$(stable_worker_agent_id "$agent")"
    generation="$(worker_generation_for_agent "$agent")"
    supervisor="${fields[14]:-auto}"; fleet_name="${fields[23]:-mac}"
    report_required=0
    if spec_requires_report_repository_executor "$spec"; then report_required=1; fi
    if [ "$report_required" = 1 ]; then
      reconcile_report_repository_executor_approval "$agent" "$hub_agent" 1
    fi
    cohort_journal_mutate finalize-start "$COHORT_EPOCH_ID" \
      "$COHORT_JOURNAL_REVISION" "finalize-start-${agent_id}" "$DEPLOY_CONTROLLER_NONCE" \
      --agent-name "$agent" --stable-id "$agent_id" --generation "$generation" >/dev/null
    run_remote_node_finalizer "$agent" "$fleet_name" "$generation" \
      "$GIT_REV" "$TS" "$(sha256_file "$NODE_FINALIZER_HELPER")" \
      "$TMPDIR_LOCAL/finalize-${agent_id}.json"
    collect_finalize_evidence "$agent"
    finalize_remote_deployment_release "$agent" "$(deployment_id_for_agent "$agent")"
    cohort_journal_mutate finalized-node "$COHORT_EPOCH_ID" \
      "$COHORT_JOURNAL_REVISION" "finalized-${agent_id}" "$DEPLOY_CONTROLLER_NONCE" \
      --agent-name "$agent" --stable-id "$agent_id" --generation "$generation" \
      --evidence-file "$TMPDIR_LOCAL/finalize-${agent_id}.json" >/dev/null
  done < "$selected_specs_file"
  cohort_journal_mutate finalize "$COHORT_EPOCH_ID" \
    "$COHORT_JOURNAL_REVISION" finalize "$DEPLOY_CONTROLLER_NONCE" >/dev/null
  COHORT_JOURNAL_ACTIVE=0
  echo "==> fleet: synchronized typed cohort finalized"
}

main() {
  # A dead prior controller is reconciled before this invocation is allowed to
  # create a new cohort identity. Recovery uses only journal-bound remote
  # artifacts, so a newer checkout can never reinterpret an older generation.
  recover_incomplete_cohort_transaction_before_deploy
  assert_frozen_deployment_source
  make_archive
  prepare_phase1_quiescence_assets
  local spec agent agent_id adoption_reason hub_agent hub_token hub_token_key hub_target_str hub_tunnel_pubkey github_review_key_b64 local_target fleet_name_field network_provider_field hub_url_field direct_mesh_hub deployed_count ide_handoff_file supervisor_field worker_capabilities_field report_executor_required
  local cohort_fleet_name runtime_generation journal_evidence
  local selected_specs_file="$TMPDIR_LOCAL/selected-specs.txt" selected_count
  local hold_adoption_plan="$TMPDIR_LOCAL/hold-adoption-plan.json"
  hub_agent="$(fleet_hub_agent)"
  hub_target_str="$(fleet_hub_target)"
  hub_token="$(fleet_scoped_env MAC_DEPLOY_HUB_TOKEN "$hub_agent")"
  hub_token_key="$(fleet_scoped_name MAC_DEPLOY_HUB_TOKEN "$hub_agent")"
  hub_tunnel_pubkey="$(fleet_scoped_env MAC_DEPLOY_HUB_TUNNEL_PUBKEY "$hub_agent")"
  CONFIGURED_AGENT_IDS="$(fleet_config_query configured-agent-ids | paste -sd, -)"
  selected_hosts "${REQUESTED_AGENTS[@]}" > "$selected_specs_file"
  chmod 0600 "$selected_specs_file"
  selected_count="$(awk 'NF { count += 1 } END { print count + 0 }' "$selected_specs_file")"
  if [ "$selected_count" -eq 0 ]; then
    echo "ERROR: no agents were selected from the frozen fleet registry" >&2
    echo "  Fleet registry: ${FLEET_REGISTRY_SOURCE}" >&2
    exit 1
  fi
  # This pass is deliberately before the first remote read/control-master.
  # One missing immutable OpenShell image rejects the entire frozen cohort
  # with zero remote mutations instead of stranding already-held workers.
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    validate_openshell_runtime_image_spec "$spec"
  done < "$selected_specs_file"
  cohort_fleet_name="$("$PYTHON_BIN" - "$selected_specs_file" <<'PY'
from pathlib import Path
import sys

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.strip():
        fields = line.split("|")
        print(fields[23] if len(fields) > 23 and fields[23] else "mac")
        break
else:
    raise SystemExit("selected cohort is empty")
PY
)"
  # A typed run establishes its owner-private, fsynced journal before the
  # first node or hub mutation. The one-use legacy bootstrap deliberately has
  # no multi-node transaction and retains the pre-existing hub hold.
  if [ "$LEGACY_HUB_BOOTSTRAP" != 1 ]; then
    initialize_cohort_transaction \
      "$selected_specs_file" "$hub_agent" "$cohort_fleet_name"
  fi
  deployed_count=0
  echo "==> deploying fleet: hub=${hub_agent} target=${hub_target_str} agents=${REQUESTED_AGENTS[*]:-all}"
  if [ -n "${GITHUB_DEPLOY_CREDENTIAL_SOURCE:-}" ]; then
    echo "==> GitHub repository credential source: ${GITHUB_DEPLOY_CREDENTIAL_SOURCE}"
  else
    echo "==> WARNING: no GitHub repository credential found in deploy env or gh keyring"
  fi

  # Pin the hub and every selected agent before the first remote read, not just
  # before phase 1.  Hub tokens and tunnel keys must come from the same concrete
  # endpoint that receives all subsequent control-plane operations.
  start_ssh_control_master "$hub_agent"
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a spec_fields <<<"$spec"
    start_ssh_control_master "${spec_fields[0]}"
  done < "$selected_specs_file"
  SSH_CONTROL_REQUIRED=1

  if [ "$LEGACY_HUB_BOOTSTRAP" = 1 ]; then
    legacy_hub_bootstrap "$selected_specs_file" "$hub_agent" "$selected_count"
    rm -rf "$TMPDIR_LOCAL"
    return 0
  fi

  github_review_key_b64="$(ensure_local_github_review_key)"
  if [ -z "$hub_tunnel_pubkey" ]; then
    hub_tunnel_pubkey="$(read_hub_tunnel_pubkey 2>/dev/null || true)"
  fi
  if [ -z "$hub_token" ]; then
    hub_token="$(read_hub_token)"
    upsert_local_env "$hub_token_key" "$hub_token"
  fi

  # Bind the actual negotiated SSH endpoints and the hub's durable database
  # identity before any phase-1 restore arm or dispatch-hold mutation.
  bind_live_cohort_routes "$selected_specs_file" "$hub_agent"

  # Validate the whole frozen cohort before holding any worker.  A legacy hub
  # can only bootstrap itself; once that single-node upgrade lands, a second
  # invocation may establish the real all-node synchronized epoch.
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    validate_router_topology_spec "$spec" "$hub_token"
  done < "$selected_specs_file"

  run_typed_cohort "$selected_specs_file" "$hub_agent" "$hub_token" \
    "$hub_tunnel_pubkey" "$github_review_key_b64" "$hold_adoption_plan"
  ide_handoff_file="$(write_ide_handoff_file \
    "http://127.0.0.1:8789" "$hub_token" "$hub_agent" "$cohort_fleet_name")"
  echo "==> ${hub_agent}: hub UI access:"
  echo "    1. open tunnel:  ssh -L 8789:127.0.0.1:8789 ${hub_target_str}"
  echo "    2. open Fleet IDE: IDE_HANDOFF_FILE=$(shell_quote "$ide_handoff_file") IDE_OPEN=1 make run-gui"
  echo "       (bearer stored in the owner-only handoff file; not printed or placed in the URL)"
  echo "    token also stored in \${MAC_DEPLOY_ENV_FILE:-\$HOME/.mac/.env} as $hub_token_key"
  rm -rf "$TMPDIR_LOCAL"
  return 0
}

main
