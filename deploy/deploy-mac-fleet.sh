#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMPDIR_LOCAL="${TMPDIR:-/tmp}/mac-fleet-deploy-${TS}.$$"
ARCHIVE="${TMPDIR_LOCAL}/mac.tar.gz"
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

resolve_python_bin() {
  local candidate
  for candidate in "${PYTHON:-}" "${MAC_PYTHON:-}" python3 python; do
    [ -n "$candidate" ] || continue
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
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
generic schema/defaults sample only.

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
  echo "ERROR: Python 3.9+ is required (python3 or python)" >&2
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

import os
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


def model_field(value: Any) -> str:
    value = text_field(value)
    return "" if value == "*" else value


def require_no_pipe(fields: Iterable[str]) -> None:
    for field in fields:
        if "|" in field:
            print("ERROR: fleet config values may not contain '|'", file=sys.stderr)
            raise SystemExit(2)


def agent_map(items: Any) -> Dict[str, Dict[str, Any]]:
    if not items:
        return {}
    if not isinstance(items, list):
        print("ERROR: fleet config agents must be a list", file=sys.stderr)
        raise SystemExit(2)
    result: Dict[str, Dict[str, Any]] = {}
    for item in items:
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
        text_field(worker.get("capabilities") or "ops,python,hermes,review,web_search,web_extract,web_crawl,firecrawl"),
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

parse_ssh_target_fields() {
  local raw_target="$1"
  "$PYTHON_BIN" - "$raw_target" "${SSH_PORT_OVERRIDE:-}" <<'PY'
import sys

text = (sys.argv[1] or "").strip()
port_override = int(sys.argv[2]) if sys.argv[2] else None
user_host = text
parsed_port = port_override
if text.count(":") == 1 and not text.endswith(":"):
    candidate_host, candidate_port = text.rsplit(":", 1)
    if candidate_port.isdigit():
        user_host = candidate_host
        parsed_port = parsed_port if parsed_port is not None else int(candidate_port)
print("%s|%s" % (user_host, parsed_port or ""))
PY
}

# Operator->node connection options injected from the fleet config (SSH_JUMP /
# SSH_STRICT are set once in main() from defaults.ssh_jump /
# defaults.ssh_strict_host_key_checking in fleets.yaml). This is the single
# chokepoint every operator->node ssh/scp flows through, so a bastion fleet (e.g.
# GKE pods reachable only via a jump host) works without any ~/.ssh/config edits.
# NOTE: these are operator-side only — they are never interpolated into the
# hub->spoke reverse-tunnel heredocs, which run in-cluster without the bastion.
ssh_conn_opts() {
  if [ "${SSH_STRICT:-1}" = "0" ]; then
    printf '%s\0%s\0%s\0%s\0' "-o" "StrictHostKeyChecking=no" "-o" "UserKnownHostsFile=/dev/null"
  fi
  if [ -n "${SSH_JUMP:-}" ]; then
    printf '%s\0%s\0' "-o" "ProxyJump=${SSH_JUMP}"
  fi
}

ssh_target_args() {
  local raw_target="$1" ssh_target ssh_port
  IFS='|' read -r ssh_target ssh_port < <(parse_ssh_target_fields "$raw_target")
  ssh_conn_opts
  if [ -n "$ssh_port" ]; then
    printf '%s\0%s\0%s\0' "-p" "$ssh_port" "$ssh_target"
  else
    printf '%s\0' "$ssh_target"
  fi
}

scp_target_args() {
  local raw_target="$1" ssh_target ssh_port
  IFS='|' read -r ssh_target ssh_port < <(parse_ssh_target_fields "$raw_target")
  ssh_conn_opts
  if [ -n "$ssh_port" ]; then
    printf '%s\0%s\0%s\0' "-P" "$ssh_port" "$ssh_target"
  else
    printf '%s\0' "$ssh_target"
  fi
}

# Populate SSH_JUMP / SSH_STRICT globals from the fleet's
# defaults.ssh_jump / defaults.ssh_strict_host_key_checking (fleets.yaml).
# Called once in main() before any operator->node ssh/scp.
SSH_JUMP="${SSH_JUMP:-}"
SSH_STRICT="${SSH_STRICT:-1}"
# Space-joined ssh opts (no spaces within any token, so it's safe to expand
# unquoted) for the few bare-target ssh calls that don't flow through
# ssh_target_args (health/tunnel probes, post-deploy restarts).
SSH_CONN_OPTS=""
load_ssh_jump_config() {
  local out
  out="$(fleet_config_query ssh-jump 2>/dev/null || true)"
  [ -n "$out" ] || return 0
  SSH_JUMP="${out%%|*}"
  if [ "${out##*|}" = "0" ]; then SSH_STRICT="0"; else SSH_STRICT="1"; fi
  SSH_CONN_OPTS=""
  [ "$SSH_STRICT" = "0" ] && SSH_CONN_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
  [ -n "$SSH_JUMP" ] && SSH_CONN_OPTS="${SSH_CONN_OPTS:+$SSH_CONN_OPTS }-o ProxyJump=$SSH_JUMP"
  if [ -n "$SSH_JUMP" ]; then
    # Operator-side message: log() is defined only inside the remote node
    # payload (the <<'REMOTE' heredoc), so on the operator it resolves to the
    # macOS /usr/bin/log binary and would abort under set -e. Use the same
    # echo "==> ..." convention as the rest of main()'s operator-side output.
    echo "==> ssh: operator->node via -o ProxyJump=${SSH_JUMP} (strict host-key checking: $([ "$SSH_STRICT" = "0" ] && echo off || echo on))"
  fi
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

fleet_scoped_env() {
  local key="$1" fleet="$2" scoped
  scoped="$(fleet_scoped_name "$key" "$fleet")"
  if [ -n "${!scoped+x}" ]; then
    printf '%s' "${!scoped}"
  elif [ -n "${!key+x}" ]; then
    printf '%s' "${!key}"
  fi
}

make_archive() {
  mkdir -p "$TMPDIR_LOCAL"
  git -C "$ROOT" archive --format=tar.gz --output="$ARCHIVE" HEAD
}

reconcile_remote_deploy() {
  local agent="$1" target="$2" ssh_parts=() ssh_args=() ssh_target item last_index
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$target")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_AGENT=$(shell_quote "$agent") MAC_DEPLOY_TS=$(shell_quote "$TS") bash -s" <<'REMOTE'
set -euo pipefail
agent="${MAC_DEPLOY_AGENT:?}"
deploy_ts="${MAC_DEPLOY_TS:?}"
mac_home="${MAC_HOME:-$HOME/.mac}"
log_dir="$mac_home/logs"
manifest="$log_dir/deploy-manifest-${deploy_ts}-post.json"
latest="$log_dir/deploy-manifest-latest.json"
deploy_log="$log_dir/deploy-${deploy_ts}.log"
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
"$PYTHON_BIN" - "$manifest" "$agent" <<'PY'
import json
import sys
manifest_path, expected_agent = sys.argv[1], sys.argv[2]
data = json.load(open(manifest_path, encoding="utf-8"))
if data.get("stage") != "post":
    raise SystemExit("remote reconciliation failed: manifest stage is %r" % data.get("stage"))
if data.get("agent") != expected_agent:
    raise SystemExit("remote reconciliation failed: manifest agent is %r" % data.get("agent"))
PY
if [ -f "$mac_home/mac.env" ]; then
  set -a
  . "$mac_home/mac.env"
  set +a
  curl -fsS --max-time 10 "http://127.0.0.1:${MAC_PORT:-8789}/health" >/dev/null
fi
echo "remote reconciliation succeeded for $agent"
REMOTE
}

deploy_host() {
  local spec="$1" hub_token="${2:-}" hub_tunnel_pubkey="${3:-}" allow_degraded_services="${4:-0}" github_review_key_b64="${5:-}" agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary supervisor shared_services_manager qdrant_url qdrant_install qdrant_required qdrant_bind_addr qdrant_port qdrant_image qdrant_memory_limit fleet_name control_port qdrant_data_dir firecrawl_url firecrawl_install firecrawl_required firecrawl_bind_addr firecrawl_port network_provider network_install network_hostname_prefix tailscale_auth_key_env headscale_manage headscale_login_server headscale_health_url headscale_fleet_url headscale_preauth_key_source headscale_preauth_key_env headscale_port headscale_public_addr headscale_dns headscale_ip_prefix remote_archive ssh_args scp_args ssh_target scp_target nvidia_api_key nvidia_api_base nvidia_base_url openai_api_key openai_base_url anthropic_api_key anthropic_base_url perplexity_api_key perplexity_base_url perplexity_api_base
  IFS='|' read -r agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary supervisor shared_services_manager qdrant_url qdrant_install qdrant_required qdrant_bind_addr qdrant_port qdrant_image qdrant_memory_limit fleet_name control_port qdrant_data_dir firecrawl_url firecrawl_install firecrawl_required firecrawl_bind_addr firecrawl_port network_provider network_install network_hostname_prefix tailscale_auth_key_env headscale_manage headscale_login_server headscale_health_url headscale_fleet_url headscale_preauth_key_source headscale_preauth_key_env headscale_port headscale_public_addr headscale_dns headscale_ip_prefix <<<"$spec"
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
  local ssh_parts=() scp_parts=() last_index
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$target")
  while IFS= read -r -d '' item; do scp_parts+=("$item"); done < <(scp_target_args "$target")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  last_index=$((${#scp_parts[@]} - 1))
  scp_target="${scp_parts[$last_index]}"
  scp_args=("${scp_parts[@]:0:$last_index}")

  echo "==> ${agent}: copying mac release archive"
  scp -q -o BatchMode=yes -o ConnectTimeout=10 "${scp_args[@]}" "$ARCHIVE" "${scp_target}:${remote_archive}"

  echo "==> ${agent}: running one-time deploy"
  local remote_env=() remote_cmd
  add_remote_env() { remote_env+=("$1=$(shell_quote "$2")"); }
  add_remote_env MAC_DEPLOY_AGENT "$agent"
  add_remote_env MAC_DEPLOY_OS "$os"
  add_remote_env MAC_DEPLOY_ARCHIVE "$remote_archive"
  add_remote_env MAC_DEPLOY_TS "$TS"
  add_remote_env MAC_DEPLOY_GIT_REV "$GIT_REV"
  add_remote_env MAC_DEPLOY_GIT_URL "$GIT_URL"
  add_remote_env MAC_DEPLOY_GIT_BRANCH "$GIT_BRANCH"
  add_remote_env MAC_DEPLOY_HERMES_SLACK_HOME_CHANNEL_NAME "$home_channel"
  add_remote_env MAC_DEPLOY_HERMES_GATEWAY_MODEL "$gateway_model"
  add_remote_env MAC_DEPLOY_HERMES_GATEWAY_PROVIDER "$gateway_provider"
  add_remote_env MAC_DEPLOY_HERMES_GATEWAY_BASE_URL "$gateway_base_url"
  add_remote_env MAC_DEPLOY_HUB_URL "$hub_url"
  add_remote_env MAC_DEPLOY_HUB_TOKEN "$hub_token"
  add_remote_env MAC_DEPLOY_CONTROL_BIND_HOST "$bind_host"
  add_remote_env MAC_DEPLOY_WORKER_MODE "$worker_mode"
  add_remote_env MAC_DEPLOY_WORKER_CAPABILITIES "$worker_capabilities"
  add_remote_env MAC_DEPLOY_WORKER_ALLOWED_PROJECTS "$worker_allowed_projects"
  add_remote_env MAC_DEPLOY_WORKER_REQUIRED_METADATA "$worker_required_metadata"
  add_remote_env MAC_DEPLOY_WORKER_REQUIRE_CANARY "$worker_require_canary"
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
  add_remote_env MAC_DEPLOY_HUB_TUNNEL_PUBKEY "$hub_tunnel_pubkey"
  add_remote_env MAC_DEPLOY_ALLOW_DEGRADED_SERVICES "${allow_degraded_services:-0}"
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
  add_remote_env MAC_DEPLOY_AGENT_MEDIA_ROUTES "${MAC_DEPLOY_AGENT_MEDIA_ROUTES:-}"
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
  remote_cmd="${remote_env[*]} bash -s"
  unset -f add_remote_env
  if ! ssh -A -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" \
    "$remote_cmd" <<'REMOTE'
set -euo pipefail

AGENT="${MAC_DEPLOY_AGENT:?}"
FLEET_NAME="${MAC_DEPLOY_FLEET_NAME:-mac}"
OS_KIND="${MAC_DEPLOY_OS:?}"
ARCHIVE="${MAC_DEPLOY_ARCHIVE:?}"
DEPLOY_TS="${MAC_DEPLOY_TS:?}"
DEPLOY_REV="${MAC_DEPLOY_GIT_REV:?}"
DEPLOY_GIT_URL="${MAC_DEPLOY_GIT_URL:-}"
DEPLOY_GIT_BRANCH="${MAC_DEPLOY_GIT_BRANCH:-main}"
HERMES_SLACK_HOME_CHANNEL_NAME="${MAC_DEPLOY_HERMES_SLACK_HOME_CHANNEL_NAME:-}"
HERMES_GATEWAY_MODEL="${MAC_DEPLOY_HERMES_GATEWAY_MODEL:-}"
if [ "$HERMES_GATEWAY_MODEL" = "*" ]; then
  HERMES_GATEWAY_MODEL=""
fi
HERMES_GATEWAY_PROVIDER="${MAC_DEPLOY_HERMES_GATEWAY_PROVIDER:-custom}"
HERMES_GATEWAY_BASE_URL="${MAC_DEPLOY_HERMES_GATEWAY_BASE_URL:-}"
HUB_URL="${MAC_DEPLOY_HUB_URL:-http://127.0.0.1:8789}"
HUB_TOKEN="${MAC_DEPLOY_HUB_TOKEN:-}"
CONTROL_BIND_HOST="${MAC_DEPLOY_CONTROL_BIND_HOST:-127.0.0.1}"
WORKER_MODE="${MAC_DEPLOY_WORKER_MODE:-heartbeat}"
WORKER_CAPABILITIES="${MAC_DEPLOY_WORKER_CAPABILITIES:-ops,python,hermes,review,web_search,web_extract,web_crawl,firecrawl}"
WORKER_ALLOWED_PROJECTS="${MAC_DEPLOY_WORKER_ALLOWED_PROJECTS:-}"
WORKER_REQUIRED_METADATA="${MAC_DEPLOY_WORKER_REQUIRED_METADATA:-}"
WORKER_REQUIRE_CANARY="${MAC_DEPLOY_WORKER_REQUIRE_CANARY:-1}"
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
NETWORK_PROVIDER="${MAC_DEPLOY_NETWORK_PROVIDER:-tailscale}"
# gketun-02: network=none spokes reach hub-managed shared services through the
# reverse tunnel's localhost forwards (install_reverse_tunnel_on_hub:
# -R 127.0.0.1:16333:hub:6333, -R 127.0.0.1:13002:hub:3002), NOT the hub FQDN —
# cross-pod service ports are typically blocked (only port 22 is), which is the
# whole reason the SSH tunnel exists. Mirror MAC_HUB_URL's 127.0.0.1:18789
# convention so the agent self-test + runtime hit the tunnel-forwarded ports.
if [ "$NETWORK_PROVIDER" = "none" ] && [ "$AGENT" != "$SHARED_SERVICES_MANAGER_AGENT" ]; then
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
MAC_HOME="${MAC_HOME:-$HOME/.mac}"
MAC_PORT="${MAC_DEPLOY_CONTROL_PORT:-${MAC_PORT:-8789}}"
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
MAC_GEN_SERVICE_NAME="${FLEET_NAME}-gen-server.service"
MAC_LAUNCHD_LABEL="com.${FLEET_NAME}.control-plane"
HERMES_LAUNCHD_LABEL="com.${FLEET_NAME}.hermes-gateway"
MAC_AGENT_LAUNCHD_LABEL="com.${FLEET_NAME}.agent"
MAC_SUPERVISORD_PROG="${FLEET_NAME}-control-plane"
HERMES_SUPERVISORD_PROG="${FLEET_NAME}-hermes-gateway"
AGENT_SUPERVISORD_PROG="${FLEET_NAME}-agent"
MAC_SUPERVISORD_CONF_NAME="${FLEET_NAME}-fleet.conf"
SRC_BACKUP=""
VENV_BACKUP=""
HERMES_BACKUP=""
MAC_UNIT_BACKUP=""
HERMES_UNIT_BACKUP=""
MAC_AGENT_UNIT_BACKUP=""
MAC_PLIST_BACKUP=""
HERMES_PLIST_BACKUP=""
MAC_AGENT_PLIST_BACKUP=""

mkdir -p "$LOG_DIR" "$MAC_HOME/backups"
exec > >(tee -a "$DEPLOY_LOG") 2>&1

log() {
  printf '[%s] [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$AGENT" "$*"
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
  log "ERROR: no Python >= 3.11 found (mac requires-python >=3.11)"
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
export AGENT FLEET_NAME OS_KIND DEPLOY_TS DEPLOY_REV DEPLOY_GIT_URL DEPLOY_GIT_BRANCH DEPLOY_STARTED_ISO HERMES_SLACK_HOME_CHANNEL_NAME HERMES_GATEWAY_MODEL HERMES_GATEWAY_PROVIDER HERMES_GATEWAY_BASE_URL HUB_URL HUB_TUNNEL_PUBKEY CONTROL_BIND_HOST WORKER_MODE WORKER_CAPABILITIES WORKER_ALLOWED_PROJECTS WORKER_REQUIRED_METADATA WORKER_REQUIRE_CANARY SUPERVISOR_REQUESTED SUPERVISOR_KIND SHARED_SERVICES_MANAGER_AGENT QDRANT_URL_CONFIGURED QDRANT_INSTALL QDRANT_REQUIRE QDRANT_BIND_ADDR_CONFIGURED QDRANT_PORT_CONFIGURED QDRANT_IMAGE_CONFIGURED QDRANT_MEMORY_LIMIT_CONFIGURED QDRANT_DATA_DIR_CONFIGURED DRAIN_MODE DRAIN_TIMEOUT_SECONDS DRAIN_POLL_SECONDS MAC_HOME MAC_PORT MAC_SERVICE_NAME HERMES_SERVICE_NAME MAC_AGENT_SERVICE_NAME MAC_LAUNCHD_LABEL HERMES_LAUNCHD_LABEL MAC_AGENT_LAUNCHD_LABEL MAC_SUPERVISORD_PROG HERMES_SUPERVISORD_PROG AGENT_SUPERVISORD_PROG MAC_SUPERVISORD_CONF_NAME SRC_DIR VENV HERMES_DIR ENV_FILE LOG_DIR DEPLOY_LOG PY HERMES_PY PYTHON_BIN

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

install_github_review_key() {
  [ -n "$GITHUB_REVIEW_KEY_B64" ] || return 0
  local ssh_dir="$HOME/.ssh"
  local key_file="$ssh_dir/mac_github_review_id"
  local config_file="$ssh_dir/config"
  mkdir -p "$ssh_dir"
  chmod 700 "$ssh_dir"
  "$PYTHON_BIN" -c "import base64, sys; open(sys.argv[1],'wb').write(base64.b64decode(sys.argv[2]))" "$key_file" "$GITHUB_REVIEW_KEY_B64"
  chmod 600 "$key_file"
  log "installed GitHub review deploy key at $key_file"
  ssh-keyscan -H github.com 2>/dev/null >> "$ssh_dir/known_hosts"
  chmod 644 "$ssh_dir/known_hosts"
  log "added github.com to SSH known_hosts"
  touch "$config_file"
  chmod 600 "$config_file"
  if ! grep -q "mac_github_review_id" "$config_file" 2>/dev/null; then
    printf '\n# mac GitHub review deploy key\nHost github.com\n  IdentityFile ~/.ssh/mac_github_review_id\n  IdentitiesOnly yes\n' >> "$config_file"
    log "added GitHub review SSH config block"
  fi
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

reload_mac_env() {
  unset MAC_HERMES_GATEWAY_MODEL ACC_HERMES_GATEWAY_MODEL HERMES_INFERENCE_MODEL ACC_LLM_MODEL
  set -a
  . "$ENV_FILE"
  set +a
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
    "mac-hermes-task-executor",
    "mac-hermes work-context",
    "mac-hermes tasks",
    "mac-hermes add-child-task",
    "mac-hermes projects",
    "mac-hermes project-items",
    "mac-hermes agents",
    "shell_execution",
    "workspace_file_access",
    "hgmac agents list",
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
from mac.services import ControlPlane

agent = os.environ["AGENT"]
fleet = os.environ.get("FLEET_NAME") or "mac"
home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
tenant_id = os.environ.get("MAC_FLEET_TENANT_ID") or stable_id("tenant", fleet)
agent_id = os.environ.get("MAC_AGENT_ID") or stable_id("agent", agent)
persona_id = os.environ.get("MAC_HERMES_PERSONA_ID") or stable_id("persona", agent)
instance_id = os.environ.get("MAC_HERMES_INSTANCE_ID") or stable_id("hermes", agent)
shared_services_manager = os.environ.get("SHARED_SERVICES_MANAGER_AGENT") or agent
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
            fleet_id=fleet_fid,
            actor="mac-deploy",
        )
    else:
        cp.update_fleet(
            existing_fleet.id,
            status="active",
            tenant_id=tenant_id,
            metadata={**existing_fleet.metadata, **fleet_metadata},
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
  MAC_PLIST_BACKUP="$MAC_PLIST_BACKUP" HERMES_PLIST_BACKUP="$HERMES_PLIST_BACKUP" \
  MAC_AGENT_PLIST_BACKUP="$MAC_AGENT_PLIST_BACKUP" \
  FLEET_NAME="$FLEET_NAME" \
  MAC_SERVICE_NAME="$MAC_SERVICE_NAME" HERMES_SERVICE_NAME="$HERMES_SERVICE_NAME" MAC_AGENT_SERVICE_NAME="$MAC_AGENT_SERVICE_NAME" \
  MAC_LAUNCHD_LABEL="$MAC_LAUNCHD_LABEL" HERMES_LAUNCHD_LABEL="$HERMES_LAUNCHD_LABEL" MAC_AGENT_LAUNCHD_LABEL="$MAC_AGENT_LAUNCHD_LABEL" \
  MAC_SUPERVISORD_PROG="$MAC_SUPERVISORD_PROG" HERMES_SUPERVISORD_PROG="$HERMES_SUPERVISORD_PROG" AGENT_SUPERVISORD_PROG="$AGENT_SUPERVISORD_PROG" \
  "$PY" - "$stage" "$path" <<'PY'
import json
import os
import subprocess
import sys
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
    agent_svc = os.environ.get("MAC_AGENT_SERVICE_NAME", fleet + "-agent.service")
    mac_label = os.environ.get("MAC_LAUNCHD_LABEL", "com." + fleet + ".control-plane")
    hermes_label = os.environ.get("HERMES_LAUNCHD_LABEL", "com." + fleet + ".hermes-gateway")
    agent_label = os.environ.get("MAC_AGENT_LAUNCHD_LABEL", "com." + fleet + ".agent")
    qdrant_label = "com." + fleet + ".qdrant"
    mac_prog = os.environ.get("MAC_SUPERVISORD_PROG", fleet + "-control-plane")
    hermes_prog = os.environ.get("HERMES_SUPERVISORD_PROG", fleet + "-hermes-gateway")
    agent_prog = os.environ.get("AGENT_SUPERVISORD_PROG", fleet + "-agent")
    qdrant_prog = fleet + "-qdrant"
    if supervisor == "systemd":
        result = run(
            [
                "systemctl",
                "show",
                mac_svc,
                hermes_svc,
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
            "hermes_gateway": run(["launchctl", "list", hermes_label]),
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
        "hermes_plist": os.environ.get("HERMES_PLIST_BACKUP") or None,
        "mac_agent_plist": os.environ.get("MAC_AGENT_PLIST_BACKUP") or None,
    },
    "rollback": str(Path(os.environ["LOG_DIR"]) / "rollback-latest.sh"),
}
output_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

write_rollback_script() {
  cat > "$ROLLBACK_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail

MAC_HOME='$MAC_HOME'
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
HERMES_PLIST_BACKUP='$HERMES_PLIST_BACKUP'
MAC_AGENT_PLIST_BACKUP='$MAC_AGENT_PLIST_BACKUP'
MAC_SERVICE_NAME='$MAC_SERVICE_NAME'
HERMES_SERVICE_NAME='$HERMES_SERVICE_NAME'
MAC_AGENT_SERVICE_NAME='$MAC_AGENT_SERVICE_NAME'
MAC_LAUNCHD_LABEL='$MAC_LAUNCHD_LABEL'
HERMES_LAUNCHD_LABEL='$HERMES_LAUNCHD_LABEL'
MAC_AGENT_LAUNCHD_LABEL='$MAC_AGENT_LAUNCHD_LABEL'
MAC_SUPERVISORD_PROG='$MAC_SUPERVISORD_PROG'
HERMES_SUPERVISORD_PROG='$HERMES_SUPERVISORD_PROG'
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

case "\${SUPERVISOR_KIND:-\$OS_KIND}" in
  systemd|linux)
    sudo systemctl stop "\$MAC_AGENT_SERVICE_NAME" "\$HERMES_SERVICE_NAME" "\$MAC_SERVICE_NAME" >/dev/null 2>&1 || true
    ;;
  supervisord)
    supervisorctl stop "\$AGENT_SUPERVISORD_PROG" "\$HERMES_SUPERVISORD_PROG" "\$MAC_SUPERVISORD_PROG" >/dev/null 2>&1 || true
    sudo supervisorctl stop "\$AGENT_SUPERVISORD_PROG" "\$HERMES_SUPERVISORD_PROG" "\$MAC_SUPERVISORD_PROG" >/dev/null 2>&1 || true
    ;;
  launchd|darwin)
    uid="\$(id -u)"
    launchctl bootout "gui/\$uid/\$MAC_AGENT_LAUNCHD_LABEL" >/dev/null 2>&1 || true
    launchctl bootout "gui/\$uid/\$HERMES_LAUNCHD_LABEL" >/dev/null 2>&1 || true
    launchctl bootout "gui/\$uid/\$MAC_LAUNCHD_LABEL" >/dev/null 2>&1 || true
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
    [ -n "\$MAC_PLIST_BACKUP" ] && [ -f "\$MAC_PLIST_BACKUP" ] && cp -f "\$MAC_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/\$MAC_LAUNCHD_LABEL.plist"
    [ -n "\$HERMES_PLIST_BACKUP" ] && [ -f "\$HERMES_PLIST_BACKUP" ] && cp -f "\$HERMES_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/\$HERMES_LAUNCHD_LABEL.plist"
    [ -n "\$MAC_AGENT_PLIST_BACKUP" ] && [ -f "\$MAC_AGENT_PLIST_BACKUP" ] && cp -f "\$MAC_AGENT_PLIST_BACKUP" "\$HOME/Library/LaunchAgents/\$MAC_AGENT_LAUNCHD_LABEL.plist"
    uid="\$(id -u)"
    launchctl bootstrap "gui/\$uid" "\$HOME/Library/LaunchAgents/\$MAC_LAUNCHD_LABEL.plist" >/dev/null 2>&1 || launchctl kickstart -k "gui/\$uid/\$MAC_LAUNCHD_LABEL"
    launchctl bootstrap "gui/\$uid" "\$HOME/Library/LaunchAgents/\$HERMES_LAUNCHD_LABEL.plist" >/dev/null 2>&1 || launchctl kickstart -k "gui/\$uid/\$HERMES_LAUNCHD_LABEL"
    launchctl bootstrap "gui/\$uid" "\$HOME/Library/LaunchAgents/\$MAC_AGENT_LAUNCHD_LABEL.plist" >/dev/null 2>&1 || launchctl kickstart -k "gui/\$uid/\$MAC_AGENT_LAUNCHD_LABEL"
    ;;
esac

echo "rollback complete from $DEPLOY_TS"
EOF
  chmod 700 "$ROLLBACK_SCRIPT"
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

stop_existing_services_for_deploy() {
  log "stopping existing mac services for artifact replacement"
  case "$SUPERVISOR_KIND" in
    systemd)
      sudo systemctl stop "$MAC_AGENT_SERVICE_NAME" "$HERMES_SERVICE_NAME" "$MAC_SERVICE_NAME" >/dev/null 2>&1 || true
      ;;
    supervisord)
      run_supervisorctl stop "$AGENT_SUPERVISORD_PROG" "$HERMES_SUPERVISORD_PROG" "$MAC_SUPERVISORD_PROG" >/dev/null 2>&1 || true
      ;;
    launchd)
      local uid
      uid="$(id -u)"
      launchctl bootout "gui/$uid/$MAC_AGENT_LAUNCHD_LABEL" >/dev/null 2>&1 || true
      launchctl bootout "gui/$uid/$HERMES_LAUNCHD_LABEL" >/dev/null 2>&1 || true
      launchctl bootout "gui/$uid/$MAC_LAUNCHD_LABEL" >/dev/null 2>&1 || true
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
  local response
  response="$(mac_api_json GET "/agents")" || return 1
  "$PY" - "$AGENT" "$response" <<'PY'
import json
import sys

expected = sys.argv[1]
agents = json.loads(sys.argv[2])
for agent in agents:
    if agent.get("name") == expected or agent.get("id") == expected:
        print(agent.get("id"))
        raise SystemExit(0)
raise SystemExit(1)
PY
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
  # Pull this agent's Slack tokens from mac's OWN vault (the hub's
  # SecretsService, via /secrets + /secrets/<name>/resolve) — the
  # centralized secret store that replaces per-host .env scattering.
  # Idempotent; writes ~/.hermes/slack_accounts.json and updates
  # SLACK_BOT_TOKEN / SLACK_APP_TOKEN in ~/.hermes/config.yaml's env block.
  # (Secrets were migrated off the retired TokenHub by
  # scripts/migrate-tokenhub-vault.sh.)
  local fetcher="$SRC_DIR/scripts/mac-fetch-slack-secrets.py"
  if [ ! -f "$fetcher" ]; then
    log "skipping Slack vault fetch: $fetcher not present (older mac source?)"
    return 0
  fi
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
    log "fetching Slack secrets for ${AGENT} from mac vault ($mac_vault_url)"
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

install_gpu_gen_server() {
  # media-01: durable local media-gen server for a GPU agent. Provisions a
  # dedicated venv (torch/diffusers), installs the OpenAI-images-compatible
  # server as a systemd unit on the port the agent advertises (#1), and starts
  # it. GPU-gated (like Omniverse skills) + systemd-only + requires a configured
  # gen model. Entirely non-fatal: a GPU-dep hiccup must never block the deploy.
  if [ "$SUPERVISOR_KIND" != "systemd" ]; then
    log "gen server: supervisor is $SUPERVISOR_KIND (systemd-only for now); skipping"
    return 0
  fi
  if ! { command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; }; then
    log "gen server: no NVIDIA GPU on $AGENT; skipping (GPU-only)"
    return 0
  fi
  local gen_model
  gen_model="$(grep -E '^MAC_AGENT_GEN_MODEL=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"'" || true)"
  if [ -z "$gen_model" ]; then
    log "gen server: MAC_AGENT_GEN_MODEL unset; skipping (set MAC_DEPLOY_AGENT_GEN_MODEL to enable)"
    return 0
  fi
  local server_script="$SRC_DIR/deploy/local-gen/openai_image_server.py"
  if [ ! -f "$server_script" ]; then
    log "gen server: server script missing ($server_script); skipping"
    return 0
  fi

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
        "$gen_venv/bin/python" -m pip install torch --index-url "$torch_index" >/dev/null 2>&1 \
          || log "WARNING: torch install failed (check MAC_DEPLOY_AGENT_GEN_TORCH_INDEX_URL for this GPU's CUDA)"
      else
        "$gen_venv/bin/python" -m pip install torch >/dev/null 2>&1 || log "WARNING: torch install failed"
      fi
    fi
    if ! "$gen_venv/bin/python" -c "import diffusers" >/dev/null 2>&1; then
      log "gen server: installing diffusers stack"
      "$gen_venv/bin/python" -m pip install diffusers transformers accelerate safetensors pillow huggingface_hub >/dev/null 2>&1 \
        || log "WARNING: diffusers stack install failed"
    fi
  fi
  "$gen_venv/bin/python" -m pip list --format=json > "$LOG_DIR/gen-server-deps.json" 2>/dev/null || true
  if ! "$gen_venv/bin/python" -c "import torch, diffusers" >/dev/null 2>&1; then
    log "WARNING: gen venv lacks torch/diffusers; installing the unit anyway (it retries the warm-load on start). Set MAC_DEPLOY_AGENT_GEN_TORCH_INDEX_URL for this GPU."
  fi

  # Wrapper: source mac.env, map MAC_AGENT_GEN_* -> LOCAL_GEN_*, exec in the gen
  # venv. $gen_venv/$server_script expand at write time; the \$ vars stay runtime.
  local wrapper="$MAC_HOME/bin/mac-gen-server"
  mkdir -p "$MAC_HOME/bin"
  cat > "$wrapper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
set -a
. "\$HOME/.mac/mac.env"
set +a
export PATH="$gen_venv/bin:\$PATH"
export LOCAL_GEN_MODEL="\${MAC_AGENT_GEN_MODEL:-stabilityai/sdxl-turbo}"
export LOCAL_GEN_PORT="\${MAC_AGENT_GEN_PORT:-8189}"
export LOCAL_GEN_HOST="\${MAC_AGENT_GEN_HOST:-0.0.0.0}"
exec "$gen_venv/bin/python" "$server_script"
EOF
  chmod 700 "$wrapper"

  local unit="/etc/systemd/system/${MAC_GEN_SERVICE_NAME}"
  log "installing systemd service $unit (model=$gen_model, venv=$gen_venv)"
  if sudo test -f "$unit"; then
    sudo cp -f "$unit" "$MAC_HOME/backups/${MAC_GEN_SERVICE_NAME}.${AGENT}.${DEPLOY_TS}" 2>/dev/null || true
  fi
  sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=mac local media-gen server (media-01 GPU agent)
After=network-online.target ${MAC_AGENT_SERVICE_NAME}
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$MAC_HOME
EnvironmentFile=$ENV_FILE
ExecStart=$MAC_HOME/bin/mac-gen-server
Restart=always
RestartSec=10
TimeoutStartSec=900
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable "$MAC_GEN_SERVICE_NAME" >/dev/null 2>&1 || true
  sudo systemctl restart "$MAC_GEN_SERVICE_NAME" \
    || log "WARNING: gen server failed to start (journalctl -u $MAC_GEN_SERVICE_NAME)"
  sleep 2
  sudo systemctl show "$MAC_GEN_SERVICE_NAME" -p ActiveState -p SubState -p MainPID 2>/dev/null || true
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
write_deploy_manifest "pre" "$MANIFEST_PRE"
drain_mac_agent_before_deploy
stop_existing_services_for_deploy
backup_existing_artifacts
log "installing mac source"
rm -rf "$SRC_DIR.new"
if [ -n "$DEPLOY_GIT_URL" ] && git clone --quiet --branch "$DEPLOY_GIT_BRANCH" "$DEPLOY_GIT_URL" "$SRC_DIR.new"; then
  actual_rev="$(git -C "$SRC_DIR.new" rev-parse HEAD)"
  if [ "$actual_rev" != "$DEPLOY_REV" ]; then
    # Pin the worktree to the operator's exact deploy revision. fetch + reset,
    # NOT `merge --ff-only`: ff aborts with "Not possible to fast-forward"
    # whenever $DEPLOY_REV isn't a descendant of the freshly-cloned branch HEAD
    # — e.g. origin/$DEPLOY_GIT_BRANCH advanced past the operator's local HEAD
    # (someone else merged mid-session), which leaves the spoke half-deployed.
    # We want exactly $DEPLOY_REV regardless of how it relates to the branch tip.
    if git -C "$SRC_DIR.new" fetch --quiet origin "$DEPLOY_REV"; then
      git -C "$SRC_DIR.new" reset --hard --quiet "$DEPLOY_REV"
    else
      log "WARNING: could not fetch deploy rev $DEPLOY_REV from origin; using clone HEAD $actual_rev"
    fi
  fi
else
  log "WARNING: git clone failed or was not configured; installing archive without self-update worktree"
  mkdir -p "$SRC_DIR.new"
  tar -xzf "$ARCHIVE" -C "$SRC_DIR.new"
fi
mv "$SRC_DIR.new" "$SRC_DIR"
rm -f "$ARCHIVE"

install_github_cli || true

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
  "$FIRECRAWL_URL_CONFIGURED" "$FIRECRAWL_REQUIRE" "$FIRECRAWL_PORT_CONFIGURED"

normalize_hermes_redaction_env

reload_mac_env
if [ "$WORKER_MODE" = "loop" ]; then
  ensure_hub_tunnel_key
else
  install_hub_tunnel_pubkey
fi
install_github_review_key
install_or_validate_shared_services
write_hermes_memory_topology

log "installing mac Python package (with vendored Hermes runtime + gateway extra)"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel >/dev/null
# ADR 0001 hu-04: install the hermes-gateway extra so the vendored Hermes
# runtime (src/mac/_hermes) runs in-process from this one venv — no separate
# hermes-agent venv needed. The gateway service execs mac-hermes-gateway.
"$VENV/bin/python" -m pip install -e "${SRC_DIR}[hermes-gateway]" >/dev/null
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/mac" "$HOME/.local/bin/mac"
install_or_validate_web_search_service
write_hermes_web_search_config

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
install_fleet_skills
install_omniverse_gpu_skills
install_hermes_web_deps
install_hermes_messaging_deps
repair_hermes_kanban_schema
log "installed Hermes agent from upstream plus mac-managed patches"

log "initializing mac database"
"$VENV/bin/mac" --db "$MAC_DB" init >/dev/null
register_hermes_runtime_identity
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
  if [ -f "$LOG_DIR/acc-migration-import.json" ] && [ "${MAC_FORCE_ACC_MIGRATION:-0}" != "1" ]; then
    log "existing ACC migration import report found; skipping one-time import"
    summarize_report "migration import existing" "$LOG_DIR/acc-migration-import.json"
    write_migration_status "already_imported" "$ACC_DB"
  else
    log "running ACC migration dry-run from $ACC_DB"
    "$VENV/bin/mac" --db "$MAC_DB" migrate acc "$ACC_DB" \
      --mode dry-run \
      --agent-home "$HOME" \
      --report "$LOG_DIR/acc-migration-dry-run.json" \
      > "$LOG_DIR/acc-migration-dry-run.stdout.json"
    summarize_report "migration dry-run" "$LOG_DIR/acc-migration-dry-run.json"

    log "running ACC migration import with active tasks requeued"
    "$VENV/bin/mac" --db "$MAC_DB" migrate acc "$ACC_DB" \
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
  fetch_slack_secrets_from_vault
  reload_mac_env
  sync_hermes_slack_identity_env
  sync_hermes_home_channels
}

install_linux_service() {
  local unit="/etc/systemd/system/${MAC_SERVICE_NAME}" restart_since
  log "installing systemd service $unit"
  install_mac_control_wrapper
  install_hermes_gateway_wrapper
  install_mac_agent_wrapper
  if sudo test -f "$unit"; then
    MAC_UNIT_BACKUP="$MAC_HOME/backups/${MAC_SERVICE_NAME}.${AGENT}.${DEPLOY_TS}"
    sudo cp -f "$unit" "$MAC_UNIT_BACKUP"
    sudo chown "$USER" "$MAC_UNIT_BACKUP" || true
    write_rollback_script
  fi
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
  scrub_spoke_provider_secrets
  sync_messaging_config
  install_linux_hermes_service
}

install_hermes_gateway_wrapper() {
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
  local executor="$MAC_HOME/bin/mac-hermes-task-executor"
  local executor_py="$MAC_HOME/bin/mac-hermes-task-executor.py"
  mkdir -p "$MAC_HOME/bin"
  cat > "$wrapper" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ulimit -n "${MAC_SERVICE_NOFILE_LIMIT:-4096}" 2>/dev/null || true
set -a
. "$HOME/.mac/mac.env"
set +a
export PATH="$HOME/.mac/bin:$HOME/.mac/venv/bin:$PATH"

: "${MAC_HUB_URL:?MAC_HUB_URL is required}"
: "${MAC_WORKER_TOKEN:?MAC_WORKER_TOKEN is required}"

agent_name="${MAC_WORKER_AGENT_NAME:-$(hostname -s 2>/dev/null || hostname)}"
host_name="${MAC_WORKER_HOSTNAME:-$agent_name}"
workspace="${MAC_WORKER_WORKSPACE:-$HOME/.mac/agent-workspaces}"
mode="${MAC_WORKER_MODE:-heartbeat}"
capabilities="${MAC_WORKER_CAPABILITIES:-ops,python,hermes,review,web_search,web_extract,web_crawl,firecrawl}"
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
    -H "Authorization: Bearer $MAC_WORKER_TOKEN" \
    -H "Content-Type: application/json" \
    --data '{"status":"offline","health_status":"degraded"}' \
    "$MAC_HUB_URL/agents/$agent_id/heartbeat" >/dev/null || true
}
trap mark_worker_offline TERM INT

if [ "${MAC_AGENT_STARTUP_SELF_TEST:-1}" != "0" ]; then
  "$HOME/.mac/bin/mac-agent-startup-self-test"
fi

common=(
  "$HOME/.mac/venv/bin/mac-agent"
  --url "$MAC_HUB_URL"
  --token "$MAC_WORKER_TOKEN"
  --register
  --agent-id "${MAC_AGENT_ID:-$(stable_agent_id)}"
  --agent-name "$agent_name"
  --hostname "$host_name"
  --capabilities "$capabilities"
  --workspace "$workspace"
  --lease-seconds "${MAC_WORKER_LEASE_SECONDS:-900}"
  --poll-interval "${MAC_WORKER_POLL_INTERVAL:-2}"
  --attestation-key-env "$HOME/.mac/mac.env"
  --rotate-missing-attestation-key
  --rotate-invalid-attestation-key
)
if [ -n "${MAC_WORKER_RESOURCES:-}" ]; then
  common+=(--resources "$MAC_WORKER_RESOURCES")
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
    executor="${MAC_WORKER_EXECUTOR:-$HOME/.mac/bin/mac-hermes-task-executor}"
    if [ "$executor" = "$HOME/.mac/bin/mac-hermes-task-executor" ]; then
      test -x "$HOME/.mac/venv/bin/python"
      test -f "$HOME/.mac/bin/mac-hermes-task-executor.py"
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
set +u
[ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"
. "$HOME/.mac/mac.env"
set -u
set +a
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_DISABLE_LAZY_INSTALLS=1
export HERMES_REDACT_SECRETS=true
if [ -z "${OPENAI_BASE_URL:-}" ] && [ -n "${CUSTOM_BASE_URL:-}" ]; then
  export OPENAI_BASE_URL="$CUSTOM_BASE_URL"
fi
if [ -z "${ACC_HERMES_GATEWAY_API_KEY:-}" ] && [ -n "${MAC_HERMES_GATEWAY_API_KEY:-}" ]; then
  export ACC_HERMES_GATEWAY_API_KEY="$MAC_HERMES_GATEWAY_API_KEY"
fi
# ADR 0001 hu-04: the self-test runs from the single mac venv (vendored runtime).
selftest_python="$HOME/.mac/venv/bin/python"
exec "$selftest_python" - <<'PY'
from __future__ import annotations

import json
import os
import subprocess
import sys
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


def probe_http(path_base: str, suffix: str, headers: dict[str, str] | None = None) -> tuple[bool, str]:
    if not path_base:
        return False, "endpoint is not configured"
    url = path_base.rstrip("/") + suffix
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read(1_048_576)
        return True, ""
    except (OSError, urllib.error.URLError) as exc:
        return False, safe_error(exc)


def first_context_value(context: dict[str, object], paths: list[tuple[str, ...]]) -> str:
    for path in paths:
        current: object = context
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current is not None:
            return str(current)
    return ""


def output_contains_identity(output: str, field: str, value: str) -> bool:
    if not value:
        return False
    normalized = output.lower().replace("_", " ")
    field_text = field.lower().replace("_", " ")
    value_text = value.lower().replace("_", " ")
    candidates = (
        f"{field.lower()}={value.lower()}",
        f"{field_text}={value_text}",
        f"{field_text}: {value_text}",
        f"{field_text} = {value_text}",
    )
    return any(candidate in normalized for candidate in candidates)


def classify_hermes_chat_failure(output: str) -> str:
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
hermes_home = Path(os.environ.get("HERMES_HOME") or home / ".hermes")
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
context_path = Path(
    os.environ.get("MAC_HERMES_RUNTIME_CONTEXT_FILE")
    or hermes_home / "mac-runtime-context.json"
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
# ADR 0001 hu-04: run the vendored Hermes runtime from the mac venv in-process.
python_bin = str(mac_home / "venv" / "bin" / "python")
hermes_vendored = str(mac_home / "src" / "mac" / "src" / "mac" / "_hermes")
hermes_env = {**os.environ, "PYTHONPATH": hermes_vendored + os.pathsep + os.environ.get("PYTHONPATH", "")}

problems: list[str] = []
checks: dict[str, object] = {
    "identity_env": False,
    "runtime_context": False,
    "qdrant_shared_memory": False,
    "firecrawl_web_search": False,
    "hermes_chat": False,
}
runtime_provider: dict[str, object] = {}
chat_output = ""
chat_returncode: int | None = None
hermes_failure_class = ""

for key, value in {
    "MAC_WORKER_AGENT_NAME": agent_name,
    "MAC_AGENT_ID": agent_id,
    "MAC_HERMES_INSTANCE_ID": hermes_instance,
    "MAC_HERMES_PERSONA_ID": persona_id,
    "MAC_FLEET_TENANT_ID": tenant_id,
    "HERMES_HOME": str(hermes_home),
}.items():
    if not value:
        problems.append(f"missing required identity env {key}")
checks["identity_env"] = not any(problem.startswith("missing required identity env") for problem in problems)

try:
    context = json.loads(context_path.read_text(encoding="utf-8"))
except Exception as exc:
    context = {}
    problems.append(f"runtime context unreadable at {context_path}: {safe_error(exc)}")

if context:
    expected_context = {
        "agent_id": (
            agent_id,
            [("agent_id",), ("agent", "agent_id"), ("environment", "MAC_AGENT_ID")],
        ),
        "agent_name": (
            agent_name,
            [("agent_name",), ("agent", "name"), ("environment", "MAC_WORKER_AGENT_NAME")],
        ),
        "hermes_instance_id": (
            hermes_instance,
            [
                ("hermes_instance_id",),
                ("agent", "hermes_instance_id"),
                ("identity", "hermes_instance_id"),
                ("environment", "MAC_HERMES_INSTANCE_ID"),
            ],
        ),
        "persona_id": (
            persona_id,
            [("persona_id",), ("identity", "persona_id"), ("environment", "MAC_HERMES_PERSONA_ID")],
        ),
        "tenant_id": (
            tenant_id,
            [("tenant_id",), ("identity", "tenant_id"), ("environment", "MAC_FLEET_TENANT_ID")],
        ),
    }
    for key, (expected, paths) in expected_context.items():
        actual = first_context_value(context, paths)
        if expected and actual != expected:
            problems.append(f"runtime context mismatch {key}: expected {expected!r}, got {actual!r}")
    checks["runtime_context"] = not any("runtime context" in problem for problem in problems)


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
    ok, error = probe_http(qdrant_url, "/collections", qdrant_headers)
    if not ok:
        problems.append(f"Qdrant shared memory endpoint is unreachable: {error}")
    checks["qdrant_shared_memory"] = ok

if not truthy(firecrawl_required_flag):
    problems.append("MAC_REQUIRE_FIRECRAWL must be true")
firecrawl_headers = {"Accept": "application/json"}
if firecrawl_key and firecrawl_key.lower() != "none":
    firecrawl_headers["Authorization"] = f"Bearer {firecrawl_key}"
if firecrawl_required and not firecrawl_url:
    problems.append("FIRECRAWL_API_URL is required but not configured")
elif firecrawl_url:
    ok, error = probe_http(firecrawl_url, "/health", firecrawl_headers)
    if not ok:
        problems.append(f"Firecrawl web search endpoint is unreachable: {error}")
    checks["firecrawl_web_search"] = ok

try:
    sys.path.insert(0, hermes_vendored)
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime_provider = resolve_runtime_provider(
        target_model=os.environ.get("HERMES_INFERENCE_MODEL") or None
    )
    runtime_provider = {
        "provider": runtime_provider.get("provider"),
        "source": runtime_provider.get("source"),
        "model": runtime_provider.get("model"),
    }
except Exception as exc:
    runtime_provider = {"error": safe_error(exc)}
    problems.append(f"Hermes runtime provider resolution failed: {safe_error(exc)}")

prompt = (
    "From your MAC runtime context only, answer exactly: "
    f"name={agent_name}; agent_id={agent_id}; hermes_instance={hermes_instance}. "
    "Do not infer or proxy."
)
try:
    completed = subprocess.run(
        [python_bin, "-m", "hermes_cli.main", "chat", "--query", prompt, "--quiet"],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=hermes_env,
    )
    chat_returncode = completed.returncode
    chat_output = tail((completed.stdout or "") + "\n" + (completed.stderr or ""))
    normalized = chat_output.lower()
    if completed.returncode != 0:
        hermes_failure_class = classify_hermes_chat_failure(chat_output)
        problems.append(f"Hermes chat self-test exited {completed.returncode}")
    expected_fragments = {
        "name": agent_name,
        "agent_id": agent_id,
        "hermes_instance": hermes_instance,
    }
    for field, expected in expected_fragments.items():
        if expected and not output_contains_identity(normalized, field, expected):
            problems.append(f"Hermes chat self-test did not report {field}={expected}")
    checks["hermes_chat"] = not any("Hermes chat self-test" in problem for problem in problems)
except subprocess.TimeoutExpired as exc:
    chat_returncode = None
    chat_output = tail(output_text(exc.stdout) + "\n" + output_text(exc.stderr))
    hermes_failure_class = classify_hermes_chat_failure(chat_output)
    problems.append(f"Hermes chat self-test timed out after {timeout}s")
except Exception as exc:
    chat_returncode = None
    problems.append(f"Hermes chat self-test failed to execute: {safe_error(exc)}")

blocking_problems = list(problems)
if hermes_failure_class == "budget_exceeded":
    blocking_problems = [
        problem
        for problem in blocking_problems
        if not problem.startswith("Hermes chat self-test")
    ]
status = "passed"
if problems:
    status = "failed" if blocking_problems else "degraded"

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
    "chat_returncode": chat_returncode,
    "chat_output_tail": chat_output,
    "hermes_failure_class": hermes_failure_class,
    "problems": problems,
    "blocking_problems": blocking_problems,
}
report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

hub_url = str(os.environ.get("MAC_HUB_URL") or "").rstrip("/")
token = os.environ.get("MAC_WORKER_TOKEN") or ""
if hub_url and token and agent_id:
    payload = {
        "status": "offline" if blocking_problems else "idle",
        "health_status": "degraded" if problems else "healthy",
        "resources": {"startup_self_test": report},
    }
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
#!/usr/bin/env bash
set -euo pipefail
set -a
set +u
[ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"
. "$HOME/.mac/mac.env"
set -u
set +a
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_DISABLE_LAZY_INSTALLS=1
export HERMES_REDACT_SECRETS=true
if [ -z "${OPENAI_BASE_URL:-}" ] && [ -n "${CUSTOM_BASE_URL:-}" ]; then
  export OPENAI_BASE_URL="$CUSTOM_BASE_URL"
fi
if [ -z "${ACC_HERMES_GATEWAY_API_KEY:-}" ] && [ -n "${MAC_HERMES_GATEWAY_API_KEY:-}" ]; then
  export ACC_HERMES_GATEWAY_API_KEY="$MAC_HERMES_GATEWAY_API_KEY"
fi
exec "$HOME/.mac/venv/bin/python" "$HOME/.mac/bin/mac-hermes-task-executor.py"
EOF
  chmod 700 "$executor"

cat > "$executor_py" <<'PY'
# Autonomous task executor shim. The real, unit-tested logic lives in the
# mac.task_executor module (extracted from this heredoc per loop-01): it
# builds the prompt, runs the vendored Hermes agent, writes deterministic
# evidence, emits executor telemetry, and feeds deployment lessons into
# memory so the fleet gets smarter over time.
from mac.task_executor import main

raise SystemExit(main())
PY
  chmod 600 "$executor_py"
}

install_linux_hermes_service() {
  local unit="/etc/systemd/system/${HERMES_SERVICE_NAME}" restart_since
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
After=network-online.target $MAC_SERVICE_NAME
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
TimeoutStopSec=120
LimitNOFILE=65536
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
  local unit="/etc/systemd/system/${MAC_AGENT_SERVICE_NAME}" restart_since
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
After=network-online.target $MAC_SERVICE_NAME
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$USER
WorkingDirectory=$MAC_HOME
EnvironmentFile=$ENV_FILE
ExecStart=$MAC_HOME/bin/mac-agent-service
Restart=always
RestartSec=5
TimeoutStopSec=30
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable "$MAC_AGENT_SERVICE_NAME"
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
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
}


install_supervisord_service() {
  local conf_dir conf restart_since
  conf_dir="$(supervisord_conf_dir)"
  conf="$conf_dir/$MAC_SUPERVISORD_CONF_NAME"
  log "installing supervisord programs in $conf"
  install_mac_control_wrapper
  install_hermes_gateway_wrapper
  install_mac_agent_wrapper
  if sudo test -f "$conf"; then
    MAC_UNIT_BACKUP="$MAC_HOME/backups/${MAC_SUPERVISORD_CONF_NAME}.${AGENT}.${DEPLOY_TS}"
    sudo cp -f "$conf" "$MAC_UNIT_BACKUP"
    sudo chown "$USER" "$MAC_UNIT_BACKUP" || true
    write_rollback_script
  fi
  sudo install -d -m 0755 "$conf_dir"
  sudo tee "$conf" >/dev/null <<EOF
[program:$MAC_SUPERVISORD_PROG]
command=$MAC_HOME/bin/mac-service
directory=$MAC_HOME
user=$USER
autostart=true
autorestart=true
startsecs=3
stopwaitsecs=20
stdout_logfile=$LOG_DIR/mac-service.log
stderr_logfile=$LOG_DIR/mac-service.log
environment=HOME="$HOME"

[program:$HERMES_SUPERVISORD_PROG]
command=$MAC_HOME/bin/hermes-gateway
directory=$MAC_HOME
user=$USER
autostart=true
autorestart=true
startsecs=5
stopwaitsecs=120
stdout_logfile=$LOG_DIR/hermes-gateway.log
stderr_logfile=$LOG_DIR/hermes-gateway.log
environment=HOME="$HOME"

[program:$AGENT_SUPERVISORD_PROG]
command=$MAC_HOME/bin/mac-agent-service
directory=$MAC_HOME
user=$USER
autostart=true
autorestart=true
startsecs=3
stopwaitsecs=30
stdout_logfile=$LOG_DIR/mac-agent.log
stderr_logfile=$LOG_DIR/mac-agent.log
environment=HOME="$HOME"
EOF
  # Remove stale worker-side hub tunnel conf from previous deploy approach
  sudo rm -f "$conf_dir/${FLEET_NAME}-hub-tunnel.conf" 2>/dev/null || true
  # Truncate gateway log so classify_gateway_logs only sees output from this deploy
  sudo truncate -s 0 "$LOG_DIR/hermes-gateway.log" 2>/dev/null || : > "$LOG_DIR/hermes-gateway.log" 2>/dev/null || true
  restart_since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  run_supervisorctl reread >/dev/null
  run_supervisorctl update >/dev/null
  run_supervisorctl restart "$MAC_SUPERVISORD_PROG" >/dev/null 2>&1 || run_supervisorctl start "$MAC_SUPERVISORD_PROG" >/dev/null
  sleep 3
  # Escrow the router upstream key + scrub spoke secrets + sync messaging BEFORE
  # the gateway/agent start, mirroring the systemd (install_linux_hermes_service)
  # and launchd paths. Without this the supervisord path left the hub vault empty,
  # so the router forwarded keyless (upstream 401) and the agent self-test failed.
  # Needs the control plane reachable, so wait briefly for it.
  for _i in $(seq 1 30); do
    curl -fsS -o /dev/null "http://127.0.0.1:${MAC_PORT:-8789}/ui" 2>/dev/null && break
    sleep 1
  done
  escrow_router_provider_keys
  scrub_spoke_provider_secrets
  sync_messaging_config
  run_supervisorctl restart "$HERMES_SUPERVISORD_PROG" >/dev/null 2>&1 || run_supervisorctl start "$HERMES_SUPERVISORD_PROG" >/dev/null
  sleep 5
  run_supervisorctl restart "$AGENT_SUPERVISORD_PROG" >/dev/null 2>&1 || run_supervisorctl start "$AGENT_SUPERVISORD_PROG" >/dev/null
  sleep 3
  run_supervisorctl status "$MAC_SUPERVISORD_PROG" "$HERMES_SUPERVISORD_PROG" "$AGENT_SUPERVISORD_PROG" > "$LOG_DIR/supervisord-services.txt" || true
  printf 'supervisord restarted at %s\n' "$restart_since" >> "$LOG_DIR/supervisord-services.txt"
}

install_darwin_service() {
  local uid plist wrapper
  uid="$(id -u)"
  plist="$HOME/Library/LaunchAgents/${MAC_LAUNCHD_LABEL}.plist"
  wrapper="$MAC_HOME/bin/mac-service"
  install_hermes_gateway_wrapper
  install_mac_agent_wrapper
  mkdir -p "$MAC_HOME/bin" "$HOME/Library/LaunchAgents"
  if [ -f "$plist" ]; then
    MAC_PLIST_BACKUP="$MAC_HOME/backups/${MAC_LAUNCHD_LABEL}.${AGENT}.${DEPLOY_TS}.plist"
    cp -f "$plist" "$MAC_PLIST_BACKUP"
    write_rollback_script
  fi
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
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$MAC_LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$wrapper</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$MAC_HOME</string>
  <key>StandardOutPath</key><string>$LOG_DIR/mac-service.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/mac-service.log</string>
</dict>
</plist>
EOF
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$plist"
  fi
  launchctl bootout "gui/$uid" "$plist" >/dev/null 2>&1 || true
  launchctl bootout "gui/$uid/$MAC_LAUNCHD_LABEL" >/dev/null 2>&1 || true
  : > "$LOG_DIR/mac-service.log"
  launchctl enable "gui/$uid/$MAC_LAUNCHD_LABEL"
  if ! launchctl bootstrap "gui/$uid" "$plist"; then
    launchctl kickstart -k "gui/$uid/$MAC_LAUNCHD_LABEL"
  fi
  launchctl kickstart -k "gui/$uid/$MAC_LAUNCHD_LABEL"
  sleep 3
  launchctl list "$MAC_LAUNCHD_LABEL" || true
  escrow_router_provider_keys
  scrub_spoke_provider_secrets
  sync_messaging_config
  install_darwin_hermes_service "$uid"
  install_darwin_agent_service "$uid"
}

install_darwin_hermes_service() {
  local uid="$1" plist="$HOME/Library/LaunchAgents/${HERMES_LAUNCHD_LABEL}.plist"
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
  <array><string>$MAC_HOME/bin/mac-agent-service</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$MAC_HOME</string>
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
  if ! launchctl bootstrap "gui/$uid" "$plist"; then
    launchctl kickstart -k "gui/$uid/$MAC_AGENT_LAUNCHD_LABEL"
  fi
  launchctl kickstart -k "gui/$uid/$MAC_AGENT_LAUNCHD_LABEL"
  sleep 3
  launchctl list "$MAC_AGENT_LAUNCHD_LABEL" || true
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
    "secret_redaction_disabled": {
        "severity": "critical",
        "regex": r"Secret redaction: DISABLED|HERMES_REDACT_SECRETS=false",
    },
    "traceback": {
        "severity": "error",
        "regex": r"Traceback \(most recent call last\)|\bERROR\b|Exception",
    },
}
classes = []
actionable_text = text
for name, spec in patterns.items():
    if spec["severity"] != "info":
        continue
    matches = re.findall(spec["regex"], text, flags=re.IGNORECASE)
    if matches:
        classes.append({"name": name, "severity": spec["severity"], "count": len(matches)})
        actionable_text = "\n".join(
            line
            for line in actionable_text.splitlines()
            if not re.search(spec["regex"], line, flags=re.IGNORECASE)
        )

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
    if curl -fsS --max-time 10 -H "Authorization: Bearer $token" \
      "${check_url}/agents" > "$LOG_DIR/hub-agents.json"; then
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

# media-01: durable local media-gen server on GPU agents (non-fatal, self-gated).
install_gpu_gen_server || true

if [ "$SUPERVISOR_KIND" = "systemd" ]; then
  classify_gateway_logs "$LOG_DIR/hermes-gateway-journal.txt"
else
  classify_gateway_logs "$LOG_DIR/hermes-gateway.log"
fi

log "verifying mac health and Hermes startup report"
curl -fsS "http://127.0.0.1:$MAC_PORT/health" > "$LOG_DIR/health.json"
curl -fsS -H "Authorization: Bearer $MAC_API_TOKEN" \
  "http://127.0.0.1:$MAC_PORT/startup/hermes" \
  > "$LOG_DIR/startup-hermes.json"
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
clear_mac_agent_drain_after_deploy

write_deploy_manifest "post" "$MANIFEST_POST"
cp -f "$MANIFEST_POST" "$LOG_DIR/deploy-manifest-latest.json"
log "deploy complete"
REMOTE
  then
    echo "==> ${agent}: ssh exited non-zero; reconciling remote deploy state"
    reconcile_remote_deploy "$agent" "$target"
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

read_hub_token() {
  local target ssh_parts=() ssh_args=() ssh_target item last_index
  target="$(hub_target)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$target")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    'set -euo pipefail; set -a; . "$HOME/.mac/mac.env"; set +a; printf "%s" "${MAC_API_TOKEN:?}"'
}

read_hub_tunnel_pubkey() {
  local target ssh_parts=() ssh_args=() ssh_target item last_index
  target="$(hub_target)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$target")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
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
  local worker_agent="$1" worker_target="$2" hub_target_str="$3" fleet_name_arg="${4:-mac}"
  local ssh_parts=() ssh_args=() ssh_target item last_index tunnel_host fleet_name_local
  fleet_name_local="${fleet_name_arg:-mac}"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$hub_target_str")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  # Resolve the worker's actual SSH hostname from local ~/.ssh/config so the hub
  # (which has no local SSH aliases) can reach it by FQDN within the cluster.
  tunnel_host="$(ssh -G "$worker_target" 2>/dev/null | awk '/^hostname / {print $2; exit}')"
  if [ -z "$tunnel_host" ]; then
    tunnel_host="$worker_target"
  fi
  # Derive the worker's SSH user from the agent's target (user@host, an
  # ~/.ssh/config alias, or a bare host) the same way we derive the
  # host. Falls back to "horde" only when ssh can't resolve a user, so
  # fleets whose worker user isn't horde (e.g. jkh@...) get the right
  # account instead of a hardcoded guess.
  tunnel_user="$(ssh -G "$worker_target" 2>/dev/null | awk '/^user / {print $2; exit}')"
  if [ -z "$tunnel_user" ]; then
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
    tailscale|headscale)
      [ -n "$hub_url" ]
      ;;
    *)
      return 1
      ;;
  esac
}

worker_has_mesh_client() {
  local raw_target="$1" provider="$2" ssh_parts=() ssh_args=() ssh_target item last_index
  provider="$(printf '%s' "${provider:-}" | tr '[:upper:]' '[:lower:]')"
  case "$provider" in
    tailscale|headscale) ;;
    *) return 1 ;;
  esac
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$raw_target")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=5 "${ssh_args[@]}" "$ssh_target" \
    'command -v tailscale >/dev/null 2>&1 && tailscale status --self >/dev/null 2>&1' 2>/dev/null
}

worker_can_reach_hub_url() {
  local raw_target="$1" hub_url="$2" ssh_parts=() ssh_args=() ssh_target item last_index
  [ -n "$hub_url" ] || return 1
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$raw_target")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=5 "${ssh_args[@]}" "$ssh_target" \
    "curl -fsS --connect-timeout 3 --max-time 5 '${hub_url%/}/health' >/dev/null 2>&1" 2>/dev/null
}

main() {
  make_archive
  # Operator->node ssh/scp options (bastion ProxyJump etc.) from fleets.yaml.
  load_ssh_jump_config
  local spec agent hub_agent hub_token hub_token_key hub_target_str hub_tunnel_pubkey github_review_key_b64 local_target fleet_name_field network_provider_field hub_url_field direct_mesh_hub deployed_count
  hub_agent="$(fleet_hub_agent)"
  hub_target_str="$(fleet_hub_target)"
  hub_token="$(fleet_scoped_env MAC_DEPLOY_HUB_TOKEN "$hub_agent")"
  hub_token_key="$(fleet_scoped_name MAC_DEPLOY_HUB_TOKEN "$hub_agent")"
  hub_tunnel_pubkey="$(fleet_scoped_env MAC_DEPLOY_HUB_TUNNEL_PUBKEY "$hub_agent")"
  deployed_count=0
  echo "==> deploying fleet: hub=${hub_agent} target=${hub_target_str} agents=${REQUESTED_AGENTS[*]:-all}"
  github_review_key_b64="$(ensure_local_github_review_key)"
  if [ -z "$hub_tunnel_pubkey" ]; then
    hub_tunnel_pubkey="$(read_hub_tunnel_pubkey 2>/dev/null || true)"
  fi
  while IFS= read -r spec; do
    IFS='|' read -r -a spec_fields <<<"$spec"
    agent="${spec_fields[0]}"
    local_target="${spec_fields[1]}"
    hub_url_field="${spec_fields[7]:-}"
    fleet_name_field="${spec_fields[23]:-mac}"
    network_provider_field="${spec_fields[31]:-none}"
    direct_mesh_hub=0
    if [ "$agent" != "$hub_agent" ] \
      && uses_direct_mesh_hub "$network_provider_field" "$hub_url_field" \
      && worker_can_reach_hub_url "$local_target" "$hub_url_field"; then
      direct_mesh_hub=1
    fi
    if [ "$agent" != "$hub_agent" ] && [ -z "$hub_token" ]; then
      hub_token="$(read_hub_token)"
      upsert_local_env "$hub_token_key" "$hub_token"
    fi
    allow_degraded_services=0
    if [ "$agent" != "$hub_agent" ] && [ "$direct_mesh_hub" != "1" ]; then
      # Detect brand-new nodes: if the remote mac API is unreachable before deploy,
      # the hub tunnel key has not been authorized yet. Allow Qdrant and Firecrawl
      # to be degraded for this first deploy; they are re-checked after the tunnel
      # is established below.
      if ! ssh -n -o BatchMode=yes -o ConnectTimeout=5 $SSH_CONN_OPTS "$local_target" \
        "curl -fsS --max-time 3 http://127.0.0.1:8789/health >/dev/null 2>&1" 2>/dev/null; then
        allow_degraded_services=1
        echo "==> ${agent}: first deploy (no existing mac API); shared-services degraded override active"
      fi
      # Update the hub's reverse-tunnel conf and wait for it BEFORE deploying the
      # worker. The worker validates hub-managed Qdrant and Firecrawl
      # through tunnel-forwarded ports during deploy; the tunnel must be
      # established first.
      install_reverse_tunnel_on_hub "$agent" "$local_target" "$hub_target_str" "$fleet_name_field"
      echo "==> ${agent}: waiting for hub reverse tunnel to establish before worker deploy"
      local attempt tunnel_ok=0
      for attempt in $(seq 1 6); do
        sleep 5
        if ssh -n -o BatchMode=yes -o ConnectTimeout=5 $SSH_CONN_OPTS "$local_target" \
          "curl -fsS --max-time 3 http://127.0.0.1:18789/health >/dev/null 2>&1" 2>/dev/null; then
          echo "==> ${agent}: hub tunnel reachable after $((attempt * 5))s"
          tunnel_ok=1
          break
        fi
      done
    fi
    deploy_host "$spec" "$hub_token" "$hub_tunnel_pubkey" "$allow_degraded_services" "$github_review_key_b64"
    deployed_count=$((deployed_count + 1))
    if [ "$agent" = "$hub_agent" ]; then
      hub_token="$(read_hub_token)"
      upsert_local_env "$hub_token_key" "$hub_token"
      echo "==> ${agent}: hub UI access:"
      echo "    1. open tunnel:  ssh -L 8789:127.0.0.1:8789 ${hub_target_str}"
      echo "    2. open browser: http://localhost:8789/ui?t=${hub_token}"
      echo "       (token auto-populates from ?t= param and is stripped from the URL)"
      echo "    token also stored in \${MAC_DEPLOY_ENV_FILE:-\$HOME/.mac/.env} as $hub_token_key"
      hub_tunnel_pubkey="$(read_hub_tunnel_pubkey)"
    else
      if [ "$direct_mesh_hub" = "1" ]; then
        echo "==> ${agent}: using ${network_provider_field} hub URL ${hub_url_field}; skipping reverse tunnel"
      elif [ "${tunnel_ok:-0}" = "1" ]; then
        local agent_prog="${fleet_name_field}-agent"
        ssh -n -o BatchMode=yes -o ConnectTimeout=10 $SSH_CONN_OPTS "$local_target" \
          "sudo supervisorctl restart '$agent_prog' >/dev/null 2>&1 || sudo supervisorctl start '$agent_prog' >/dev/null 2>&1 || true" 2>/dev/null || true
        echo "==> ${agent}: restarted mac-agent with tunnel now available"
      elif [ "${allow_degraded_services:-0}" = "1" ]; then
        # First deploy: hub tunnel key now installed. Wait for the hub supervisord
        # tunnel process to reconnect (autorestart=true) then restart mac-agent.
        echo "==> ${agent}: first deploy complete; waiting for hub tunnel to auto-establish"
        local attempt first_tunnel_ok=0
        for attempt in $(seq 1 12); do
          sleep 5
          if ssh -n -o BatchMode=yes -o ConnectTimeout=5 $SSH_CONN_OPTS "$local_target" \
            "curl -fsS --max-time 3 http://127.0.0.1:18789/health >/dev/null 2>&1" 2>/dev/null; then
            echo "==> ${agent}: hub tunnel reachable after $((attempt * 5))s"
            first_tunnel_ok=1
            break
          fi
        done
        local agent_prog="${fleet_name_field}-agent"
        if [ "$first_tunnel_ok" = "1" ]; then
          ssh -n -o BatchMode=yes -o ConnectTimeout=10 $SSH_CONN_OPTS "$local_target" \
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
