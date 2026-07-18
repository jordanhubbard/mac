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
SSH_CONTROL_DIR="/tmp/mac-fleet-ssh-${UID:-0}-${DEPLOY_CONTROLLER_NONCE:0:12}"
mkdir -p "$SSH_CONTROL_DIR"
chmod 0700 "$SSH_CONTROL_DIR"
SSH_CONTROL_REQUIRED=0

cleanup_local_deployment() {
  local status=$? pid_file pid
  trap - EXIT
  set +e
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
  ssh -A -MN -o BatchMode=yes -o ConnectTimeout=10 \
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
  # deployment owner has acquired the stable guard and emitted READY.
  "$PYTHON_BIN" - "$source_file" "$expected_ready" "$@" <<'PY'
import shutil
import subprocess
import sys

source, expected, *command = sys.argv[1:]
process = subprocess.Popen(
    command,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
)
assert process.stdin is not None and process.stdout is not None
line = process.stdout.readline().decode("utf-8", "replace").rstrip("\r\n")
if line != expected:
    process.terminate()
    process.wait(timeout=10)
    raise SystemExit("remote deployment fence did not emit the exact READY receipt")
try:
    with open(source, "rb") as payload:
        shutil.copyfileobj(payload, process.stdin)
    process.stdin.close()
    for chunk in iter(lambda: process.stdout.read(65536), b""):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
finally:
    if process.stdin and not process.stdin.closed:
        process.stdin.close()
raise SystemExit(process.wait())
PY
}

fenced_remote_upload() {
  local agent="$1" deployment_id="$2" source_file="$3" destination="$4"
  local ssh_parts=() ssh_args=() ssh_target item last_index remote_body remote_cmd
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  remote_body="set -e; umask 077; _mac_target=$(shell_quote "$destination"); _mac_tmp=\"\${_mac_target}.upload.$$\"; trap 'rm -f \"\$_mac_tmp\"' EXIT HUP INT TERM; cat > \"\$_mac_tmp\"; chmod 0600 \"\$_mac_tmp\"; mv -f \"\$_mac_tmp\" \"\$_mac_target\"; trap - EXIT HUP INT TERM"
  remote_cmd="$(remote_deployment_fenced_exec "$deployment_id" 1 sh -c "$remote_body")"
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
      "MAC_DEPLOY_AGENT=$(shell_quote "$agent") MAC_DEPLOY_TS=$(shell_quote "$TS") MAC_DEPLOY_GIT_REV=$(shell_quote "$GIT_REV") MAC_DEPLOY_CLEAR_REPO_UPDATE_BLOCKER=$(shell_quote "$clear_repo_update_blocker") $fence_exec" <<'REMOTE'
set -euo pipefail
agent="${MAC_DEPLOY_AGENT:?}"
deploy_ts="${MAC_DEPLOY_TS:?}"
expected_rev="${MAC_DEPLOY_GIT_REV:?}"
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
"$python_bin" - "$manifest" "$latest" "$agent" "$deploy_ts" "$expected_rev" <<'PY'
import json
import sys
manifest_path, latest_path, expected_agent, expected_ts, expected_rev = sys.argv[1:]
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
    post_drain()
    print(json.dumps({"agent_id": agent_id, "dispatch_hold": True}, sort_keys=True))
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

acquire_remote_deployment_lock() {
  local agent="$1" deployment_id="$2" ssh_parts=() ssh_args=() ssh_target last_index item
  local takeover=0
  case "$(printf '%s' "${MAC_DEPLOY_TAKEOVER_STALE_LOCK:-0}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) takeover=1 ;;
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

prepare_remote_mac_agent_deployment() {
  local agent="$1" deployment_id="$2" supervisor="${3:-auto}" fleet_name="${4:-mac}"
  local adoption_reason="${5:-}" require_owned_after_prepare="${6:-0}"
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
  set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" stop
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
    generation="$($PYTHON_BIN - "$GIT_REV" "$agent" <<'PY'
import secrets
import sys

print("%s-%s-%s" % (sys.argv[1], sys.argv[2], secrets.token_hex(16)))
PY
)"
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
    "MAC_DEPLOY_SERVICE_ACTION=$(shell_quote "$action") MAC_DEPLOY_SUPERVISOR=$(shell_quote "$supervisor") MAC_DEPLOY_FLEET_NAME=$(shell_quote "$fleet_name") MAC_DEPLOY_RESTART_GENERATION=$(shell_quote "$generation") $service_fence" <<'REMOTE'
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
case "$supervisor" in
  supervisord)
    program="${MAC_DEPLOY_FLEET_NAME:?}-agent"
    if [ "$action" = restart ] || [ "$action" = stop ]; then
      sudo supervisorctl stop "$program" >/dev/null 2>&1 || true
      status="$(sudo supervisorctl status "$program" 2>&1 || true)"
      case "$status" in
        *STOPPED*|*EXITED*|*FATAL*|*"no such process"*) ;;
        *) echo "supervisord worker did not become inactive: $status" >&2; exit 1 ;;
      esac
    fi
    if [ "$action" = restart ]; then
      sudo supervisorctl start "$program" >/dev/null
    fi
    ;;
  systemd)
    unit="${MAC_DEPLOY_FLEET_NAME:?}-agent.service"
    if [ "$action" = restart ] || [ "$action" = stop ]; then
      sudo systemctl stop "$unit" >/dev/null 2>&1 || true
      if sudo systemctl is-active --quiet "$unit"; then
        echo "systemd worker remained active after stop: $unit" >&2
        exit 1
      fi
    fi
    if [ "$action" = restart ]; then
      sudo systemctl start "$unit" >/dev/null
    fi
    ;;
  launchd)
    label="com.${MAC_DEPLOY_FLEET_NAME:?}.agent"
    domain="gui/$(id -u)"
    if [ "$action" = restart ] || [ "$action" = stop ]; then
      launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
      if launchctl print "$domain/$label" >/dev/null 2>&1; then
        echo "launchd worker remained loaded after bootout: $label" >&2
        exit 1
      fi
    fi
    if [ "$action" = restart ]; then
      # A deferred restart intentionally leaves the freshly written plist
      # unregistered until the post manifest has reconciled.  ``kickstart``
      # cannot load an absent job, so bootstrap it first when needed.
      plist="$HOME/Library/LaunchAgents/${label}.plist"
      if ! launchctl print "$domain/$label" >/dev/null 2>&1; then
        [ -f "$plist" ] || { echo "launchd agent plist missing: $plist" >&2; exit 1; }
        launchctl bootstrap "$domain" "$plist"
      fi
      launchctl kickstart "$domain/$label"
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
      restore_fence="$(remote_deployment_fenced_exec "$expected_deployment_id" 0 bash -s)"
      ssh -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
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
      hub_agent_restart_gate rehold "$agent_id" "$generation" "" "$hold_reason" \
        "$prior_owned" 0 0 >/dev/null || true
      return 1
    fi
    if [ "$release_commit_mode" = deferred ]; then
      local ready_file="$TMPDIR_LOCAL/release-ready-${agent_id}.json"
      "$PYTHON_BIN" - "$ready_file" "$agent" "$agent_id" "$supervisor" "$fleet_name" \
      "$generation" "$release_baseline" "$hold_reason" "$prior_owned" \
        "$expected_principal_id" "$require_authenticated" "$deployment_id" \
        "$require_report_executor" <<'PY'
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
) = sys.argv[1:]
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
  local spec="$1" hub_token="${2:-}" hub_tunnel_pubkey="${3:-}" allow_degraded_services="${4:-0}" github_review_key_b64="${5:-}" direct_mesh_hub_flag="${6:-0}" already_prepared="${7:-0}" agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary supervisor shared_services_manager qdrant_url qdrant_install qdrant_required qdrant_bind_addr qdrant_port qdrant_image qdrant_memory_limit fleet_name control_port qdrant_data_dir firecrawl_url firecrawl_install firecrawl_required firecrawl_bind_addr firecrawl_port network_provider network_install network_hostname_prefix tailscale_auth_key_env headscale_manage headscale_login_server headscale_health_url headscale_fleet_url headscale_preauth_key_source headscale_preauth_key_env headscale_port headscale_public_addr headscale_dns headscale_ip_prefix webdav_enabled webdav_install webdav_url webdav_bind_addr webdav_port webdav_root webdav_public_path hermes_surface_b64 openshell_required github_credentials_required remote_archive remote_registry deploy_generation ssh_args ssh_target nvidia_api_key nvidia_api_base nvidia_base_url openai_api_key openai_base_url anthropic_api_key anthropic_base_url perplexity_api_key perplexity_base_url perplexity_api_base
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
      "$agent" "$deploy_generation" "$supervisor" "$fleet_name"
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
  # Secret values are streamed over SSH stdin into a mode-0600, one-use file;
  # they must never appear in the ssh remote command or process argv.
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
  local remote_secret_file="/tmp/mac-node-install-${agent}-${TS}.env"
  local deploy_script reviewed_tool_assets
  deploy_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fleet-node-install.sh"
  reviewed_tool_assets="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reviewed-tool-assets.sh"
  echo "==> ${agent}: copying fleet-node-install.sh"
  fenced_remote_upload "$agent" "$deploy_generation" "$deploy_script" "$remote_node_script"
  echo "==> ${agent}: copying reviewed native-tool checksum contract"
  fenced_remote_upload "$agent" "$deploy_generation" "$reviewed_tool_assets" "$remote_tool_assets"
  remote_env+=("MAC_DEPLOY_REVIEWED_TOOL_ASSETS=$(shell_quote "$remote_tool_assets")")
  local remote_cmd fenced_remote_cmd local_secret_payload
  remote_cmd="${remote_env[*]} sh -c 'umask 077; _mac_secret_file=\$1; _mac_script=\$2; _mac_tool_assets=\$3; trap \"rm -f \\\"\$_mac_secret_file\\\" \\\"\$_mac_script\\\" \\\"\$_mac_tool_assets\\\"\" EXIT HUP INT TERM; cat > \"\$_mac_secret_file\"; set -a; . \"\$_mac_secret_file\"; set +a; rm -f \"\$_mac_secret_file\"; bash \"\$_mac_script\"' sh $(shell_quote "$remote_secret_file") $(shell_quote "$remote_node_script") $(shell_quote "$remote_tool_assets")"
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
    echo "==> ${agent}: validating remote post-deploy manifest"
    if ! reconcile_remote_deploy "$agent" "$target" "$openshell_disable_requested"; then
      echo "==> ${agent}: remote deploy returned success but post manifest validation failed" >&2
      return 1
    fi
  else
    rm -f "$local_secret_payload"
    echo "==> ${agent}: ssh exited non-zero; reconciling remote deploy state"
    reconcile_remote_deploy "$agent" "$target" "$openshell_disable_requested"
  fi
  if [ "$openshell_enabled" = "1" ]; then
    echo "==> ${agent}: restarting mac-agent after in-transaction OpenShell and OpenClaw validation"
  else
    echo "==> ${agent}: restarting mac-agent after post-manifest reconciliation"
  fi
  set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" restart keep
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
  local hub_deployment_id hub_lock_temporary=0 hub_script_status=0 fence_exec
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
  fence_exec="$(remote_deployment_fenced_exec "$hub_deployment_id" 0 bash -s)"
  # Pass values to the remote inline; quoting handled by shell_quote.
  if ssh -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    "TUNNEL_WORKER_AGENT=$(shell_quote "$worker_agent") TUNNEL_HOST=$(shell_quote "$tunnel_host") TUNNEL_USER=$(shell_quote "$tunnel_user") TUNNEL_FLEET_NAME=$(shell_quote "$fleet_name_local") $fence_exec" <<'HUBSCRIPT'
set -euo pipefail
worker_agent="${TUNNEL_WORKER_AGENT:?}"
tunnel_host="${TUNNEL_HOST:?}"
tunnel_user="${TUNNEL_USER:-horde}"
fleet_name="${TUNNEL_FLEET_NAME:-mac}"
managed_marker="mac.managed-reverse-tunnel.v1:${fleet_name}:${worker_agent}"
definition_tmp=""

cleanup_definition_tmp() {
  if [ -n "$definition_tmp" ]; then
    sudo rm -f "$definition_tmp" >/dev/null 2>&1 || true
  fi
}
trap cleanup_definition_tmp EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

legacy_managed_definition() {
  local kind="$1" path="$2"
  sudo python3 - "$kind" "$path" "$fleet_name" "$worker_agent" "$HOME" \
    "$(whoami)" "$(command -v ssh)" <<'PY'
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
        lines = [line.strip() for line in raw.decode().splitlines() if line.strip()]
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
}

assert_managed_definition_or_absent() {
  local path="$1" marker_line="$2" kind="$3"
  if sudo test -L "$path"; then
    echo "refusing symlink reverse-tunnel manager definition: $path" >&2
    exit 1
  fi
  if sudo test -e "$path" && ! sudo grep -Fqx "$marker_line" "$path"; then
    if ! legacy_managed_definition "$kind" "$path"; then
      echo "refusing to replace unowned reverse-tunnel manager definition: $path" >&2
      exit 1
    fi
  fi
}

stage_managed_definition() {
  local path="$1"
  definition_tmp="$(sudo mktemp "${path}.mac-deploy.XXXXXX")"
}

install_staged_definition() {
  local path="$1" marker_line="$2" kind="$3"
  # Recheck immediately before the atomic replace. An operator definition that
  # appeared after preflight must remain byte-identical.
  assert_managed_definition_or_absent "$path" "$marker_line" "$kind"
  sudo mv -f "$definition_tmp" "$path"
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
  assert_managed_definition_or_absent "$plist" "$marker_line" launchd
  launchd_loaded_legacy=0
  launchd_file_legacy=0
  if sudo test -e "$plist" && ! sudo grep -Fqx "$marker_line" "$plist"; then
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
  launchd_state=""
  if launchd_state="$(sudo launchctl print "system/${label}" 2>/dev/null)"; then
    if launchd_state_has_managed_identity managed "$launchd_state"; then
      :
    elif launchd_state_has_managed_identity legacy "$launchd_state"; then
      # Either this is the first exact-template migration or the previous run
      # atomically installed the marked plist and was interrupted before the
      # one-time legacy job bootout. Both are safe, retryable adoption states.
      [ "$launchd_file_legacy" = 1 ] || sudo grep -Fqx "$marker_line" "$plist"
      launchd_loaded_legacy=1
    else
      echo "refusing to bootout same-name launchd job without exact MAC identity" >&2
      exit 1
    fi
  fi
  mkdir -p "$HOME/.mac/logs"
  stage_managed_definition "$plist"
  sudo tee "$definition_tmp" > /dev/null <<PLIST
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
  sudo chown root:wheel "$definition_tmp"
  sudo chmod 0644 "$definition_tmp"
  install_staged_definition "$plist" "$marker_line" launchd
  # Re-evaluate the loaded label immediately before its one permitted stop.
  # Absence is intentional on first install; every present job must prove both
  # canonical path and durable in-job marker before path-form bootout.
  if launchd_state="$(sudo launchctl print "system/${label}" 2>/dev/null)"; then
    if ! launchd_state_has_managed_identity managed "$launchd_state"; then
      [ "$launchd_loaded_legacy" = 1 ]
      launchd_state_has_managed_identity legacy "$launchd_state"
    fi
    sudo launchctl bootout system "$plist"
  fi
  sudo launchctl bootstrap system "$plist"
  sudo launchctl enable "system/${label}"
  sudo launchctl kickstart -k "system/${label}"
  launchd_state="$(sudo launchctl print "system/${label}")"
  launchd_state_has_managed_identity managed "$launchd_state"
  exit 0
fi
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  service="${fleet_name}-tunnel-${worker_agent}.service"
  unit="/etc/systemd/system/${service}"
  marker_line="# ${managed_marker}"
  ssh_bin="$(command -v ssh)"
  load_state="$(sudo systemctl show -p LoadState --value "$service")"
  if [ "$load_state" = loaded ]; then
    fragment="$(sudo systemctl show -p FragmentPath --value "$service")"
    [ -n "$fragment" ] \
      && [ "$(sudo realpath "$fragment")" = "$(sudo realpath "$unit")" ] || {
        echo "refusing same-name systemd tunnel loaded from another definition" >&2
        exit 1
      }
    dropins="$(sudo systemctl show -p DropInPaths --value "$service")"
    [ -z "$dropins" ] || {
      echo "refusing managed systemd tunnel with unreviewed drop-ins" >&2
      exit 1
    }
  elif [ "$load_state" != not-found ]; then
    echo "refusing same-name systemd tunnel in unexpected load state: $load_state" >&2
    exit 1
  fi
  assert_managed_definition_or_absent "$unit" "$marker_line" systemd
  stage_managed_definition "$unit"
  sudo tee "$definition_tmp" > /dev/null <<EOF
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
  sudo chown root:root "$definition_tmp"
  sudo chmod 0644 "$definition_tmp"
  install_staged_definition "$unit" "$marker_line" systemd
  sudo systemctl daemon-reload
  fragment="$(sudo systemctl show -p FragmentPath --value "$service")"
  [ "$(sudo realpath "$fragment")" = "$(sudo realpath "$unit")" ]
  [ -z "$(sudo systemctl show -p DropInPaths --value "$service")" ]
  effective_exec="$(sudo systemctl show -p ExecStart --value "$service")"
  for needle in \
    "$ssh_bin" \
    "$HOME/.ssh/mac_tunnel_id" \
    "127.0.0.1:18789:127.0.0.1:8789" \
    "127.0.0.1:18090:127.0.0.1:8090" \
    "127.0.0.1:16333:127.0.0.1:6333" \
    "127.0.0.1:13002:127.0.0.1:3002"; do
    printf '%s\n' "$effective_exec" | grep -Fq "$needle"
  done
  [ "$(sudo systemctl show -p Restart --value "$service")" = always ]
  sudo systemctl enable "$service" >/dev/null
  # Enabling creates manager-owned links; recheck the effective source and
  # policy at the last possible point before restarting a same-name unit.
  [ "$(sudo systemctl show -p LoadState --value "$service")" = loaded ]
  fragment="$(sudo systemctl show -p FragmentPath --value "$service")"
  [ "$(sudo realpath "$fragment")" = "$(sudo realpath "$unit")" ]
  [ -z "$(sudo systemctl show -p DropInPaths --value "$service")" ]
  sudo systemctl cat "$service" | grep -Fq "$managed_marker"
  [ "$(sudo systemctl show -p Restart --value "$service")" = always ]
  sudo systemctl restart "$service" >/dev/null
  sudo systemctl is-enabled "$service" >/dev/null
  sudo systemctl cat "$service" | grep -Fq "$managed_marker"
  exit 0
fi
conf_dir="$(ls -d /etc/supervisor/conf.d 2>/dev/null || ls -d /etc/supervisord.d 2>/dev/null || echo '/etc/supervisor/conf.d')"
program="${fleet_name}-tunnel-${worker_agent}"
conf="$conf_dir/${program}.conf"
marker_line="; ${managed_marker}"
assert_no_duplicate_supervisor_program() {
  local expected="$1" include_root="${MAC_SUPERVISOR_INCLUDE_ROOT:-/}"
  sudo python3 - "$program" "$expected" "$include_root" <<'PY'
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
}
supervisor_status_output() {
  local output rc
  set +e
  output="$(supervisorctl status "$program" 2>&1)"
  rc=$?
  set -e
  case "$output" in
    *RUNNING*|*STARTING*|*BACKOFF*|*STOPPED*|*EXITED*|*FATAL*|*"no such process"*)
      printf '%s\n' "$output"
      return 0
      ;;
  esac
  set +e
  output="$(sudo supervisorctl status "$program" 2>&1)"
  rc=$?
  set -e
  case "$output" in
    *RUNNING*|*STARTING*|*BACKOFF*|*STOPPED*|*EXITED*|*FATAL*|*"no such process"*)
      printf '%s\n' "$output"
      return 0
      ;;
  esac
  echo "could not establish supervisor identity for $program (status $rc): $output" >&2
  return 1
}
assert_no_duplicate_supervisor_program "$conf"
preexisting_status="$(supervisor_status_output)"
if ! sudo test -e "$conf"; then
  case "$preexisting_status" in
    *"no such process"*) ;;
    *) echo "refusing same-name supervisor program from another include" >&2; exit 1 ;;
  esac
fi
assert_managed_definition_or_absent "$conf" "$marker_line" supervisor
stage_managed_definition "$conf"
sudo tee "$definition_tmp" > /dev/null <<EOF
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
sudo chown root:root "$definition_tmp"
sudo chmod 0644 "$definition_tmp"
install_staged_definition "$conf" "$marker_line" supervisor
supervisor_control() {
  supervisorctl "$@" >/dev/null 2>&1 || sudo supervisorctl "$@" >/dev/null
}
supervisor_control reread
assert_no_duplicate_supervisor_program "$conf"
supervisor_control update
# update registers and autostarts changed definitions. Do not issue a blocking
# start while the spoke has not authorized the key; STARTING/BACKOFF/STOPPED
# are valid pre-key states, but a missing or FATAL program is not.
status="$(supervisor_status_output)"
case "$status" in
  *RUNNING*|*STARTING*|*BACKOFF*) ;;
  *) echo "supervisor did not register managed reverse tunnel: $status" >&2; exit 1 ;;
esac
HUBSCRIPT
  then
    hub_script_status=0
  else
    hub_script_status=$?
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
  set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" restart keep
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

  set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" restart keep
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
payload = json.loads(path.read_text(encoding='utf-8'))
if payload.get('deployment_id') != os.environ['MAC_DEPLOY_STATE_ID']:
    raise SystemExit('refusing to remove another deployment release state')
path.unlink()
PY"
  release_remote_deployment_lock "$agent" "$deployment_id"
}

commit_fleet_release_epoch() {
  local expected_count="$1" hub_agent="$2" selected_plan="$3"
  local require_release_all_selected="${4:-0}"
  local successor_hold_reason="${5:-}"
  local plan="$TMPDIR_LOCAL/fleet-release-plan.json"
  local epoch_id="${GIT_REV}:${TS}:${DEPLOY_CONTROLLER_NONCE}"
  "$PYTHON_BIN" - "$plan" "$expected_count" "$epoch_id" "$TMPDIR_LOCAL" \
    "$selected_plan" "$require_release_all_selected" "$successor_hold_reason" <<'PY'
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

main() {
  make_archive
  local spec agent agent_id adoption_reason hub_agent hub_token hub_token_key hub_target_str hub_tunnel_pubkey github_review_key_b64 local_target fleet_name_field network_provider_field hub_url_field direct_mesh_hub deployed_count ide_handoff_file supervisor_field worker_capabilities_field report_executor_required
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

  github_review_key_b64="$(ensure_local_github_review_key)"
  if [ -z "$hub_tunnel_pubkey" ]; then
    hub_tunnel_pubkey="$(read_hub_tunnel_pubkey 2>/dev/null || true)"
  fi
  if [ -z "$hub_token" ]; then
    hub_token="$(read_hub_token)"
    upsert_local_env "$hub_token_key" "$hub_token"
  fi

  # Validate the whole frozen cohort before holding any worker.  A legacy hub
  # can only bootstrap itself; once that single-node upgrade lands, a second
  # invocation may establish the real all-node synchronized epoch.
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    validate_router_topology_spec "$spec" "$hub_token"
  done < "$selected_specs_file"

  if ! hub_dispatch_hold_cas_available; then
    if [ "$selected_count" -gt 1 ] \
      || [ "$REQUIRE_RELEASE_ALL_SELECTED" = 1 ] \
      || [ -n "$HOLD_ADOPTIONS_FILE" ]; then
      echo "ERROR: the live hub lacks dispatch-hold CAS; deploy only ${hub_agent} without adoption using the explicit legacy bootstrap, then rerun the full cohort" >&2
      exit 1
    fi
  fi
  if [ -n "$SUCCESSOR_HOLD_REASON" ] \
    && ! hub_dispatch_hold_transition_available; then
    echo "ERROR: the live hub lacks POST /agents/dispatch-hold/transition-batch required for atomic successor-hold cutover" >&2
    exit 1
  fi

  preflight_cohort_hold_adoptions \
    "$selected_specs_file" "$hold_adoption_plan" "$hub_agent"

  echo "==> fleet: phase 1/3 holding and draining all ${selected_count} selected agent(s)"
  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a spec_fields <<<"$spec"
    agent="${spec_fields[0]}"
    agent_id="$(stable_worker_agent_id "$agent")"
    adoption_reason="$(hold_adoption_reason_for_agent "$hold_adoption_plan" "$agent_id")"
    supervisor_field="${spec_fields[14]:-auto}"
    fleet_name_field="${spec_fields[23]:-mac}"
    prepare_remote_mac_agent_deployment \
      "$agent" "$(deployment_id_for_agent "$agent")" \
      "$supervisor_field" "$fleet_name_field" "$adoption_reason" \
      "$REQUIRE_RELEASE_ALL_SELECTED"
  done < "$selected_specs_file"
  echo "==> fleet: phase 2/3 deploying and proving all selected agents under hold"

  while IFS= read -r spec; do
    [ -n "$spec" ] || continue
    IFS='|' read -r -a spec_fields <<<"$spec"
    agent="${spec_fields[0]}"
    local_target="${spec_fields[1]}"
    local local_ssh_parts=() local_ssh_args=() local_ssh_target local_item local_last_index
    while IFS= read -r -d '' local_item; do local_ssh_parts+=("$local_item"); done < <(ssh_target_args "$agent")
    local_last_index=$((${#local_ssh_parts[@]} - 1))
    local_ssh_target="${local_ssh_parts[$local_last_index]}"
    local_ssh_args=("${local_ssh_parts[@]:0:$local_last_index}")
    hub_url_field="${spec_fields[7]:-}"
    worker_capabilities_field="${spec_fields[10]:-}"
    supervisor_field="${spec_fields[14]:-auto}"
    fleet_name_field="${spec_fields[23]:-mac}"
    network_provider_field="${spec_fields[31]:-none}"
    direct_mesh_hub=0
    if [ "$agent" != "$hub_agent" ] \
      && uses_direct_mesh_hub "$network_provider_field" "$hub_url_field" \
      && worker_can_reach_hub_url "$agent" "$local_target" "$hub_url_field"; then
      direct_mesh_hub=1
    fi
    if [ "$agent" != "$hub_agent" ] && [ -z "$hub_token" ]; then
      hub_token="$(read_hub_token)"
      upsert_local_env "$hub_token_key" "$hub_token"
    fi
    validate_router_topology_spec "$spec" "$hub_token"
    allow_degraded_services=0
    if [ "$agent" != "$hub_agent" ] && [ "$direct_mesh_hub" != "1" ]; then
      # Detect brand-new nodes: if the remote mac API is unreachable before deploy,
      # the hub tunnel key has not been authorized yet. Allow Qdrant and Firecrawl
      # to be degraded for this first deploy; they are re-checked after the tunnel
      # is established below.
      if ! ssh -n -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${local_ssh_args[@]}" "$local_ssh_target" \
        "curl -fsS --max-time 3 http://127.0.0.1:8789/health >/dev/null 2>&1" 2>/dev/null; then
        allow_degraded_services=1
        echo "==> ${agent}: first deploy (no existing mac API); shared-services degraded override active"
      fi
      # Update the hub's reverse-tunnel conf and wait for it BEFORE deploying the
      # worker. The worker validates hub-managed Qdrant and Firecrawl
      # through tunnel-forwarded ports during deploy; the tunnel must be
      # established first.
      install_reverse_tunnel_on_hub "$agent" "$local_target" "$hub_agent" "$hub_target_str" "$fleet_name_field"
      echo "==> ${agent}: waiting for hub reverse tunnel to establish before worker deploy"
      local attempt tunnel_ok=0
      for attempt in $(seq 1 6); do
        sleep 5
        if ssh -n -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${local_ssh_args[@]}" "$local_ssh_target" \
          "curl -fsS --max-time 3 http://127.0.0.1:18789/health >/dev/null 2>&1" 2>/dev/null; then
          echo "==> ${agent}: hub tunnel reachable after $((attempt * 5))s"
          tunnel_ok=1
          break
        fi
      done
    fi
    deploy_host "$spec" "$hub_token" "$hub_tunnel_pubkey" "$allow_degraded_services" "$github_review_key_b64" "$direct_mesh_hub" 1
    deployed_count=$((deployed_count + 1))
    if [ "$agent" = "$hub_agent" ]; then
      hub_token="$(read_hub_token)"
      upsert_local_env "$hub_token_key" "$hub_token"
      ide_handoff_file="$(write_ide_handoff_file "http://127.0.0.1:8789" "$hub_token" "$hub_agent" "$fleet_name_field")"
      echo "==> ${agent}: hub UI access:"
      echo "    1. open tunnel:  ssh -L 8789:127.0.0.1:8789 ${hub_target_str}"
      echo "    2. open Fleet IDE: IDE_HANDOFF_FILE=$(shell_quote "$ide_handoff_file") IDE_OPEN=1 make run-gui"
      echo "       (bearer stored in the owner-only handoff file; not printed or placed in the URL)"
      echo "    token also stored in \${MAC_DEPLOY_ENV_FILE:-\$HOME/.mac/.env} as $hub_token_key"
      hub_tunnel_pubkey="$(read_hub_tunnel_pubkey)"
    else
      if [ "$direct_mesh_hub" = "1" ]; then
        echo "==> ${agent}: using ${network_provider_field} hub URL ${hub_url_field}; skipping reverse tunnel"
      elif [ "${tunnel_ok:-0}" = "1" ]; then
        set_remote_mac_agent_service \
          "$agent" "$supervisor_field" "$fleet_name_field" restart keep
        echo "==> ${agent}: restarted mac-agent with tunnel now available"
      elif [ "${allow_degraded_services:-0}" = "1" ]; then
        # First deploy: hub tunnel key now installed. Wait for the hub supervisord
        # tunnel process to reconnect (autorestart=true) then restart mac-agent.
        echo "==> ${agent}: first deploy complete; waiting for hub tunnel to auto-establish"
        local attempt first_tunnel_ok=0
        for attempt in $(seq 1 12); do
          sleep 5
          if ssh -n -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${local_ssh_args[@]}" "$local_ssh_target" \
            "curl -fsS --max-time 3 http://127.0.0.1:18789/health >/dev/null 2>&1" 2>/dev/null; then
            echo "==> ${agent}: hub tunnel reachable after $((attempt * 5))s"
            first_tunnel_ok=1
            break
          fi
        done
        if [ "$first_tunnel_ok" = "1" ]; then
          set_remote_mac_agent_service \
            "$agent" "$supervisor_field" "$fleet_name_field" restart keep
          echo "==> ${agent}: restarted mac-agent with tunnel now available"
        else
          echo "==> ${agent}: WARNING: hub tunnel not reachable after first deploy; redeploy to complete setup"
        fi
      fi
    fi
    report_executor_required=0
    if spec_requires_report_repository_executor "$spec"; then
      report_executor_required=1
    fi
    reconcile_bound_worker_attestation_key \
      "$agent" "$hub_agent" "$supervisor_field" "$fleet_name_field"
    reconcile_report_repository_executor_approval \
      "$agent" "$hub_agent" "$report_executor_required"
    provision_bound_worker_credential \
      "$agent" "$hub_agent" "$supervisor_field" "$fleet_name_field" \
      "$worker_capabilities_field" "$report_executor_required"
  done < "$selected_specs_file"
  if [ "$deployed_count" -eq 0 ]; then
    echo "ERROR: no agents were deployed. Check that the fleet config is valid and the requested agents exist." >&2
    echo "  Fleet registry: ${FLEET_REGISTRY_SOURCE}" >&2
    echo "  Fleet config:   ${FLEET_CONFIG_SOURCE}" >&2
    echo "  Hub selector:   ${HUB_SELECTOR:-not set (use --hub <agent>)}" >&2
    echo "  Requested agents: ${REQUESTED_AGENTS[*]:-all}" >&2
    exit 1
  fi
  enforce_bound_worker_credentials "$hub_agent"
  if [ -n "$SUCCESSOR_HOLD_REASON" ]; then
    echo "==> fleet: phase 3/3 atomically handing the proved cohort to its successor hold"
  else
    echo "==> fleet: phase 3/3 atomically releasing the proved cohort"
  fi
  commit_fleet_release_epoch \
    "$deployed_count" "$hub_agent" "$hold_adoption_plan" \
    "$REQUIRE_RELEASE_ALL_SELECTED" "$SUCCESSOR_HOLD_REASON"
  rm -rf "$TMPDIR_LOCAL"
}

main
