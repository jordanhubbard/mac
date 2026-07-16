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
TMPDIR_LOCAL="${TMPDIR:-/tmp}/mac-fleet-deploy-${TS}.$$"
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

# Keep fatal errors consistent in both the local launcher and the generated
# remote deploy script.  Remote deployments execute selected functions in a
# fresh shell, so relying on a caller-defined `die` silently turns a required
# abort into `command not found` and leaves the host half-deployed.
die() {
  log "ERROR: $*"
  exit 1
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
  deploy/deploy-mac-fleet.sh --hub <hub-node> [--ssh-port <port>] [agent ...]
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

The hub name selects the fleet. Agent arguments may be agent names from that
fleet. With no agent arguments, all enabled agents in the selected fleet are
deployed.
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

if ! PYTHON_BIN="$(resolve_python_bin)"; then
  echo "ERROR: Python 3.11+ is required (.venv/bin/python, python3.11, python3, or python)" >&2
  exit 127
fi

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


DEFAULT_WORKER_CAPABILITIES = "ops,python,openclaw,review,api,architecture,cli,docs,security,testing,typescript,ui,web_search,web_extract,web_crawl,firecrawl"
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

# Resolve every operator-side SSH/scp call through the Python contract used by
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

ssh_target_args() {
  fleet_ssh_route_args ssh "$1"
}

scp_target_args() {
  fleet_ssh_route_args scp "$1"
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
  git -C "$ROOT" archive --format=tar.gz --output="$ARCHIVE" HEAD
  fleet_config_query sanitized-registry > "$SANITIZED_FLEET_REGISTRY"
  chmod 0644 "$SANITIZED_FLEET_REGISTRY"
}

reconcile_remote_deploy() {
  local agent="$1" target="$2" ssh_parts=() ssh_args=() ssh_target item last_index
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
      "MAC_DEPLOY_AGENT=$(shell_quote "$agent") MAC_DEPLOY_TS=$(shell_quote "$TS") bash -s" <<'REMOTE'
set -euo pipefail
agent="${MAC_DEPLOY_AGENT:?}"
deploy_ts="${MAC_DEPLOY_TS:?}"
mac_home="${MAC_HOME:-$HOME/.mac}"
log_dir="$mac_home/logs"
manifest="$log_dir/deploy-manifest-${deploy_ts}-post.json"
latest="$log_dir/deploy-manifest-latest.json"
deploy_log="$log_dir/deploy-${deploy_ts}.log"
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
"$python_bin" - "$manifest" "$latest" "$agent" "$deploy_ts" <<'PY'
import json
import sys
manifest_path, latest_path, expected_agent, expected_ts = sys.argv[1:]
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
PY
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

# Optional or role-required OpenShell sandbox-enforcement bootstrap. Run it
# after a successful deploy when explicitly requested or the node role requires it. Pure
# ``gateway_impl=none`` workers require it by default; fleet config may also set
# ``worker.openshell_required`` explicitly. Required nodes always receive
# ``--enable --fail-closed`` in addition to any caller flags. Conversational
# nodes remain opt-in through MAC_DEPLOY_OPENSHELL=1. A bootstrap failure leaves
# the worker stopped and drained instead of exposing an unconfined executor.
run_openshell_bootstrap() {
  local agent="$1" target="$2" bootstrap_args="${3:-}" ssh_parts=() ssh_args=() ssh_target last_index
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  echo "==> ${agent}: OpenShell bootstrap (args='${bootstrap_args}')"
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_OPENSHELL_ARGS=$(shell_quote "$bootstrap_args") bash -s" <<'REMOTE'
set -euo pipefail
mac_home="${MAC_HOME:-$HOME/.mac}"
bs="$mac_home/src/mac/deploy/openshell/bootstrap-openshell.sh"
[ -x "$bs" ] || { echo "OpenShell bootstrap not found/executable at $bs" >&2; exit 1; }
exec "$bs" $MAC_DEPLOY_OPENSHELL_ARGS
REMOTE
}

set_remote_mac_agent_service() {
  local agent="$1" supervisor="$2" fleet_name="$3" action="$4"
  local ssh_parts=() ssh_args=() ssh_target last_index item
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_SERVICE_ACTION=$(shell_quote "$action") MAC_DEPLOY_SUPERVISOR=$(shell_quote "$supervisor") MAC_DEPLOY_FLEET_NAME=$(shell_quote "$fleet_name") bash -s" <<'REMOTE'
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
case "$supervisor" in
  supervisord)
    if [ "$action" = "stop" ]; then
      sudo supervisorctl stop mac-agent >/dev/null 2>&1 || true
    else
      sudo supervisorctl restart mac-agent >/dev/null
    fi
    ;;
  systemd)
    if [ "$action" = "stop" ]; then
      sudo systemctl stop mac-agent.service >/dev/null 2>&1 || true
    else
      sudo systemctl restart mac-agent.service >/dev/null
    fi
    ;;
  launchd)
    label="com.${MAC_DEPLOY_FLEET_NAME:?}.agent"
    domain="gui/$(id -u)"
    if [ "$action" = "stop" ]; then
      launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
    else
      # A deferred restart intentionally leaves the freshly written plist
      # unregistered until the post manifest has reconciled.  ``kickstart``
      # cannot load an absent job, so bootstrap it first when needed.
      plist="$HOME/Library/LaunchAgents/${label}.plist"
      if ! launchctl print "$domain/$label" >/dev/null 2>&1; then
        [ -f "$plist" ] || { echo "launchd agent plist missing: $plist" >&2; exit 1; }
        launchctl bootstrap "$domain" "$plist"
      fi
      launchctl kickstart -k "$domain/$label"
    fi
    ;;
  *) echo "unsupported supervisor: $supervisor" >&2; exit 1 ;;
esac
REMOTE
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

deploy_host() {
  local spec="$1" hub_token="${2:-}" hub_tunnel_pubkey="${3:-}" allow_degraded_services="${4:-0}" github_review_key_b64="${5:-}" direct_mesh_hub_flag="${6:-0}" agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary supervisor shared_services_manager qdrant_url qdrant_install qdrant_required qdrant_bind_addr qdrant_port qdrant_image qdrant_memory_limit fleet_name control_port qdrant_data_dir firecrawl_url firecrawl_install firecrawl_required firecrawl_bind_addr firecrawl_port network_provider network_install network_hostname_prefix tailscale_auth_key_env headscale_manage headscale_login_server headscale_health_url headscale_fleet_url headscale_preauth_key_source headscale_preauth_key_env headscale_port headscale_public_addr headscale_dns headscale_ip_prefix webdav_enabled webdav_install webdav_url webdav_bind_addr webdav_port webdav_root webdav_public_path hermes_surface_b64 openshell_required github_credentials_required remote_archive remote_registry ssh_args scp_args ssh_target scp_target nvidia_api_key nvidia_api_base nvidia_base_url openai_api_key openai_base_url anthropic_api_key anthropic_base_url perplexity_api_key perplexity_base_url perplexity_api_base
  IFS='|' read -r agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary supervisor shared_services_manager qdrant_url qdrant_install qdrant_required qdrant_bind_addr qdrant_port qdrant_image qdrant_memory_limit fleet_name control_port qdrant_data_dir firecrawl_url firecrawl_install firecrawl_required firecrawl_bind_addr firecrawl_port network_provider network_install network_hostname_prefix tailscale_auth_key_env headscale_manage headscale_login_server headscale_health_url headscale_fleet_url headscale_preauth_key_source headscale_preauth_key_env headscale_port headscale_public_addr headscale_dns headscale_ip_prefix webdav_enabled webdav_install webdav_url webdav_bind_addr webdav_port webdav_root webdav_public_path hermes_surface_b64 openshell_required github_credentials_required <<<"$spec"
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
  local ssh_parts=() scp_parts=() last_index
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$agent")
  while IFS= read -r -d '' item; do scp_parts+=("$item"); done < <(scp_target_args "$agent")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  last_index=$((${#scp_parts[@]} - 1))
  scp_target="${scp_parts[$last_index]}"
  scp_args=("${scp_parts[@]:0:$last_index}")

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
  if ! MAC_OPENCLAW_BOOTSTRAP_TOKEN="$hub_token" \
    PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
      "$PYTHON_BIN" "$ROOT/scripts/provision-openclaw-personality.py" \
        --config "$FLEET_REGISTRY_CONFIG" \
        --fleet "$HUB_SELECTOR" \
        --agent "$agent" \
        --hub-url "$hub_url"; then
    echo "==> ${agent}: OpenClaw persona provisioning skipped (non-fatal) — a worker does not require a persona"
  fi

  echo "==> ${agent}: copying mac release archive"
  # OpenSSH 9 switched scp to SFTP by default. Minimal fleet containers often
  # expose the SCP protocol through sshd but intentionally omit an SFTP
  # subsystem; force the portable legacy SCP wire protocol for both kinds of
  # host. Authentication, ProxyJump, host-key policy, and destination paths
  # remain the same.
  scp -O -q -o BatchMode=yes -o ConnectTimeout=10 "${scp_args[@]}" "$ARCHIVE" "${scp_target}:${remote_archive}"
  echo "==> ${agent}: copying fleet registry"
  scp -O -q -o BatchMode=yes -o ConnectTimeout=10 "${scp_args[@]}" "$SANITIZED_FLEET_REGISTRY" "${scp_target}:${remote_registry}"

  echo "==> ${agent}: running one-time deploy"
  local remote_env=() remote_secret_env=() remote_cmd openshell_enabled=0 effective_openshell_args="${MAC_DEPLOY_OPENSHELL_ARGS:-}"
  case "$(printf '%s' "${MAC_DEPLOY_OPENSHELL:-}" | tr 'A-Z' 'a-z')" in
    1|true|yes|on) openshell_enabled=1 ;;
  esac
  case "$(printf '%s' "$openshell_required" | tr 'A-Z' 'a-z')" in
    1|true|yes|on)
      openshell_enabled=1
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
  add_remote_env MAC_DEPLOY_GIT_URL "$GIT_URL"
  add_remote_env MAC_DEPLOY_GIT_BRANCH "$GIT_BRANCH"
  add_remote_env MAC_DEPLOY_HERMES_SLACK_HOME_CHANNEL_NAME "$home_channel"
  add_remote_env MAC_DEPLOY_HERMES_GATEWAY_MODEL "$gateway_model"
  add_remote_env MAC_DEPLOY_HERMES_GATEWAY_PROVIDER "$gateway_provider"
  add_remote_env MAC_DEPLOY_HERMES_GATEWAY_BASE_URL "$gateway_base_url"
  add_remote_env MAC_DEPLOY_HERMES_SURFACE_B64 "$hermes_surface_b64"
  add_remote_env MAC_DEPLOY_OPENCLAW_LIVE_CANARY "${MAC_DEPLOY_OPENCLAW_LIVE_CANARY:-0}"
  add_remote_env MAC_DEPLOY_HUB_URL "$hub_url"
  add_remote_env MAC_DEPLOY_HUB_TOKEN "$hub_token"
  add_remote_env MAC_DEPLOY_CONTROL_BIND_HOST "$bind_host"
  add_remote_env MAC_DEPLOY_WORKER_MODE "$worker_mode"
  add_remote_env MAC_DEPLOY_WORKER_CAPABILITIES "$worker_capabilities"
  add_remote_env MAC_DEPLOY_WORKER_ALLOWED_PROJECTS "$worker_allowed_projects"
  add_remote_env MAC_DEPLOY_WORKER_REQUIRED_METADATA "$worker_required_metadata"
  add_remote_env MAC_DEPLOY_WORKER_REQUIRE_CANARY "$worker_require_canary"
  add_remote_env MAC_DEPLOY_OPENSHELL_REQUIRED "$openshell_required"
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
  add_remote_env MAC_DEPLOY_DEFER_CLEAR_DRAIN "$openshell_enabled"
  # Starting/restarting the worker from inside the remote install transaction
  # can terminate that transaction before its post manifest is durable (most
  # visibly under supervisord).  The outer controller owns the restart after
  # it has reconciled the post manifest.
  add_remote_env MAC_DEPLOY_DEFER_AGENT_RESTART 1
  add_remote_env MAC_DEPLOY_HUB_TUNNEL_PUBKEY "$hub_tunnel_pubkey"
  add_remote_env MAC_DEPLOY_ALLOW_DEGRADED_SERVICES "${allow_degraded_services:-0}"
  add_remote_env MAC_DEPLOY_DIRECT_HUB "${direct_mesh_hub_flag:-0}"
  add_remote_env MAC_DEPLOY_GITHUB_REVIEW_KEY_B64 "$github_review_key_b64"
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
  local img_key="${NVIDIA_IMAGE_API_KEY:-}" aud_key="${NVIDIA_AUDIO_API_KEY:-}" vid_key="${NVIDIA_VIDEO_API_KEY:-}"
  if [ "$agent" != "$shared_services_manager" ] && [ "$router_backend_lc" = "inproc" ]; then
    img_key="" ; aud_key="" ; vid_key=""
  fi
  add_remote_env NVIDIA_IMAGE_API_KEY "$img_key"
  add_remote_env NVIDIA_AUDIO_API_KEY "$aud_key"
  add_remote_env NVIDIA_VIDEO_API_KEY "$vid_key"
  add_remote_env NVIDIA_API_KEY "$nvidia_api_key"
  add_remote_env NVIDIA_API_BASE "$nvidia_api_base"
  add_remote_env NVIDIA_BASE_URL "$nvidia_base_url"
  add_remote_env OPENAI_API_KEY "$openai_api_key"
  add_remote_env OPENAI_BASE_URL "$openai_base_url"
  add_remote_env ANTHROPIC_API_KEY "$anthropic_api_key"
  add_remote_env ANTHROPIC_BASE_URL "$anthropic_base_url"
  add_remote_env PERPLEXITY_API_KEY "$perplexity_api_key"
  add_remote_env PERPLEXITY_BASE_URL "$perplexity_base_url"
  add_remote_env PERPLEXITY_API_BASE "$perplexity_api_base"
  # The deploy body is in the standalone fleet-node-install.sh script which is
  # copied to the remote node and executed there, eliminating the large stdin
  # heredoc and its interaction with child processes that read from stdin.
  unset -f add_remote_env add_remote_secret_env
  local remote_node_script="/tmp/mac-node-install-${agent}-${TS}.sh"
  local remote_secret_file="/tmp/mac-node-install-${agent}-${TS}.env"
  local deploy_script
  deploy_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fleet-node-install.sh"
  echo "==> ${agent}: copying fleet-node-install.sh"
  scp -O -q -o BatchMode=yes -o ConnectTimeout=10 "${scp_args[@]}" \
    "$deploy_script" "${scp_target}:${remote_node_script}"
  local remote_cmd
  remote_cmd="${remote_env[*]} sh -c 'umask 077; _mac_secret_file=\$1; _mac_script=\$2; trap \"rm -f \\\"\$_mac_secret_file\\\" \\\"\$_mac_script\\\"\" EXIT HUP INT TERM; cat > \"\$_mac_secret_file\"; set -a; . \"\$_mac_secret_file\"; set +a; rm -f \"\$_mac_secret_file\"; bash \"\$_mac_script\"' sh $(shell_quote "$remote_secret_file") $(shell_quote "$remote_node_script")"
  if printf '%s\n' "${remote_secret_env[@]}" | \
    ssh -A -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" "$remote_cmd"
  then
    echo "==> ${agent}: validating remote post-deploy manifest"
    if ! reconcile_remote_deploy "$agent" "$target"; then
      echo "==> ${agent}: remote deploy returned success but post manifest validation failed" >&2
      return 1
    fi
  else
    echo "==> ${agent}: ssh exited non-zero; reconciling remote deploy state"
    reconcile_remote_deploy "$agent" "$target"
  fi
  if [ "$openshell_enabled" = "1" ]; then
    echo "==> ${agent}: keeping mac-agent stopped while OpenShell validates"
    set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" stop
    if run_openshell_bootstrap "$agent" "$target" "$effective_openshell_args"; then
      set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" restart
    else
      echo "==> ${agent}: ERROR: OpenShell bootstrap failed; mac-agent remains stopped and drained" >&2
      set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" stop || true
      return 1
    fi
  else
    echo "==> ${agent}: restarting mac-agent after post-manifest reconciliation"
    set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" restart
  fi
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
  # Pass values to the remote inline; quoting handled by shell_quote
  ssh -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    "TUNNEL_WORKER_AGENT=$(shell_quote "$worker_agent") TUNNEL_HOST=$(shell_quote "$tunnel_host") TUNNEL_USER=$(shell_quote "$tunnel_user") TUNNEL_FLEET_NAME=$(shell_quote "$fleet_name_local") bash -s" <<'HUBSCRIPT'
set -euo pipefail
worker_agent="${TUNNEL_WORKER_AGENT:?}"
tunnel_host="${TUNNEL_HOST:?}"
tunnel_user="${TUNNEL_USER:-horde}"
fleet_name="${TUNNEL_FLEET_NAME:-mac}"
if [ "$(uname -s)" = "Darwin" ]; then
  # macOS hub: run the reverse tunnel as a system LaunchDaemon (headless, no GUI
  # session required — mirrors com.mac.control-plane). macOS has neither systemd
  # nor supervisord, so without this branch the deploy died writing
  # /etc/supervisor/conf.d on the Mac hub and no GKE pod could be provisioned.
  ssh_bin="$(command -v ssh)"
  label="com.${fleet_name}.tunnel-${worker_agent}"
  plist="/Library/LaunchDaemons/${label}.plist"
  hub_user="$(whoami)"
  mkdir -p "$HOME/.mac/logs"
  sudo tee "$plist" > /dev/null <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>UserName</key><string>${hub_user}</string>
  <key>EnvironmentVariables</key><dict><key>HOME</key><string>${HOME}</string></dict>
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
  sudo chown root:wheel "$plist" 2>/dev/null || true
  sudo chmod 644 "$plist" 2>/dev/null || true
  sudo launchctl bootout "system/${label}" >/dev/null 2>&1 || true
  sudo launchctl bootstrap system "$plist" >/dev/null 2>&1 || true
  sudo launchctl enable "system/${label}" >/dev/null 2>&1 || true
  sudo launchctl kickstart -k "system/${label}" >/dev/null 2>&1 || true
  exit 0
fi
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  service="${fleet_name}-tunnel-${worker_agent}.service"
  ssh_bin="$(command -v ssh)"
  sudo tee "/etc/systemd/system/${service}" > /dev/null <<EOF
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
  sudo systemctl daemon-reload
  sudo systemctl enable --now "$service" >/dev/null 2>&1 || true
  sudo systemctl restart "$service" >/dev/null 2>&1 || true
  exit 0
fi
conf_dir="$(ls -d /etc/supervisor/conf.d 2>/dev/null || ls -d /etc/supervisord.d 2>/dev/null || echo '/etc/supervisor/conf.d')"
sudo tee "$conf_dir/${fleet_name}-tunnel-${worker_agent}.conf" > /dev/null <<EOF
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
supervisorctl reread >/dev/null 2>&1 || sudo supervisorctl reread >/dev/null 2>&1 || true
supervisorctl update >/dev/null 2>&1 || sudo supervisorctl update >/dev/null 2>&1 || true
supervisorctl restart "${fleet_name}-tunnel-${worker_agent}" >/dev/null 2>&1 \
  || supervisorctl start "${fleet_name}-tunnel-${worker_agent}" >/dev/null 2>&1 \
  || sudo supervisorctl start "${fleet_name}-tunnel-${worker_agent}" >/dev/null 2>&1 \
  || true
HUBSCRIPT
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

main() {
  make_archive
  local spec agent hub_agent hub_token hub_token_key hub_target_str hub_tunnel_pubkey github_review_key_b64 local_target fleet_name_field network_provider_field hub_url_field direct_mesh_hub deployed_count ide_handoff_file
  hub_agent="$(fleet_hub_agent)"
  hub_target_str="$(fleet_hub_target)"
  hub_token="$(fleet_scoped_env MAC_DEPLOY_HUB_TOKEN "$hub_agent")"
  hub_token_key="$(fleet_scoped_name MAC_DEPLOY_HUB_TOKEN "$hub_agent")"
  hub_tunnel_pubkey="$(fleet_scoped_env MAC_DEPLOY_HUB_TUNNEL_PUBKEY "$hub_agent")"
  CONFIGURED_AGENT_IDS="$(fleet_config_query configured-agent-ids | paste -sd, -)"
  deployed_count=0
  echo "==> deploying fleet: hub=${hub_agent} target=${hub_target_str} agents=${REQUESTED_AGENTS[*]:-all}"
  if [ -n "${GITHUB_DEPLOY_CREDENTIAL_SOURCE:-}" ]; then
    echo "==> GitHub repository credential source: ${GITHUB_DEPLOY_CREDENTIAL_SOURCE}"
  else
    echo "==> WARNING: no GitHub repository credential found in deploy env or gh keyring"
  fi
  github_review_key_b64="$(ensure_local_github_review_key)"
  if [ -z "$hub_tunnel_pubkey" ]; then
    hub_tunnel_pubkey="$(read_hub_tunnel_pubkey 2>/dev/null || true)"
  fi
  while IFS= read -r spec; do
    IFS='|' read -r -a spec_fields <<<"$spec"
    agent="${spec_fields[0]}"
    local_target="${spec_fields[1]}"
    local local_ssh_parts=() local_ssh_args=() local_ssh_target local_item local_last_index
    while IFS= read -r -d '' local_item; do local_ssh_parts+=("$local_item"); done < <(ssh_target_args "$agent")
    local_last_index=$((${#local_ssh_parts[@]} - 1))
    local_ssh_target="${local_ssh_parts[$local_last_index]}"
    local_ssh_args=("${local_ssh_parts[@]:0:$local_last_index}")
    hub_url_field="${spec_fields[7]:-}"
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
    deploy_host "$spec" "$hub_token" "$hub_tunnel_pubkey" "$allow_degraded_services" "$github_review_key_b64" "$direct_mesh_hub"
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
        local agent_prog="${fleet_name_field}-agent"
        ssh -n -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${local_ssh_args[@]}" "$local_ssh_target" \
          "sudo supervisorctl restart '$agent_prog' >/dev/null 2>&1 || sudo supervisorctl start '$agent_prog' >/dev/null 2>&1 || true" 2>/dev/null || true
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
        local agent_prog="${fleet_name_field}-agent"
        if [ "$first_tunnel_ok" = "1" ]; then
          ssh -n -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${local_ssh_args[@]}" "$local_ssh_target" \
            "sudo supervisorctl restart '$agent_prog' >/dev/null 2>&1 || sudo supervisorctl start '$agent_prog' >/dev/null 2>&1 || true" 2>/dev/null || true
          echo "==> ${agent}: restarted mac-agent with tunnel now available"
        else
          echo "==> ${agent}: WARNING: hub tunnel not reachable after first deploy; redeploy to complete setup"
        fi
      fi
    fi
  done < <(selected_hosts "${REQUESTED_AGENTS[@]}")
  if [ "$deployed_count" -eq 0 ]; then
    echo "ERROR: no agents were deployed. Check that the fleet config is valid and the requested agents exist." >&2
    echo "  Fleet registry: ${FLEET_REGISTRY_CONFIG}" >&2
    echo "  Fleet config:   ${FLEET_CONFIG}" >&2
    echo "  Hub selector:   ${HUB_SELECTOR:-not set (use --hub <agent>)}" >&2
    echo "  Requested agents: ${REQUESTED_AGENTS[*]:-all}" >&2
    exit 1
  fi
  rm -rf "$TMPDIR_LOCAL"
}

main
