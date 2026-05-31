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
  python3 "${setup_args[@]}"
  REQUESTED_AGENTS=("$NEW_HUB_NAME")
fi

fleet_config_query() {
  local mode="$1"
  shift || true
  python3 - "$mode" "$FLEET_CONFIG" "$FLEET_REGISTRY_CONFIG" "$HUB_SELECTOR" "$@" <<'PY'
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
            "ERROR: no fleet registry found at %s. Run bash setup.sh to create one, "
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
    tokenhub = merge_dicts(defaults.get("tokenhub", {}) if isinstance(defaults.get("tokenhub"), dict) else {}, agent.get("tokenhub", {}) if isinstance(agent.get("tokenhub"), dict) else {})
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
        os.environ.get("MAC_DEPLOY_TOKENHUB_URL") or text_field(tokenhub.get("url")),
        os.environ.get("MAC_DEPLOY_TOKENHUB_INSTALL") or text_field(tokenhub.get("install") or "auto"),
        os.environ.get("MAC_DEPLOY_REQUIRE_TOKENHUB") or bool_field(tokenhub.get("required"), True),
        os.environ.get("MAC_DEPLOY_TOKENHUB_BIND_ADDR") or text_field(tokenhub.get("bind_addr")),
        os.environ.get("MAC_DEPLOY_TOKENHUB_PORT") or text_field(tokenhub.get("port") or "8090"),
        os.environ.get("MAC_DEPLOY_TOKENHUB_REPO_URL") or text_field(tokenhub.get("repo_url") or "https://github.com/jordanhubbard/tokenhub.git"),
        os.environ.get("MAC_DEPLOY_TOKENHUB_REF") or text_field(tokenhub.get("ref")),
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
  python3 - "$raw_target" "${SSH_PORT_OVERRIDE:-}" <<'PY'
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

ssh_target_args() {
  local raw_target="$1" ssh_target ssh_port
  IFS='|' read -r ssh_target ssh_port < <(parse_ssh_target_fields "$raw_target")
  if [ -n "$ssh_port" ]; then
    printf '%s\0%s\0%s\0' "-p" "$ssh_port" "$ssh_target"
  else
    printf '%s\0' "$ssh_target"
  fi
}

scp_target_args() {
  local raw_target="$1" ssh_target ssh_port
  IFS='|' read -r ssh_target ssh_port < <(parse_ssh_target_fields "$raw_target")
  if [ -n "$ssh_port" ]; then
    printf '%s\0%s\0%s\0' "-P" "$ssh_port" "$ssh_target"
  else
    printf '%s\0' "$ssh_target"
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
  python3 - "$key" "$fleet" <<'PY'
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
python3 - "$manifest" "$agent" <<'PY'
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
  local spec="$1" hub_token="${2:-}" hub_tunnel_pubkey="${3:-}" tokenhub_api_key="${4:-}" allow_degraded_services="${5:-0}" github_review_key_b64="${6:-}" agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary supervisor shared_services_manager qdrant_url qdrant_install qdrant_required qdrant_bind_addr qdrant_port qdrant_image qdrant_memory_limit fleet_name control_port qdrant_data_dir firecrawl_url firecrawl_install firecrawl_required firecrawl_bind_addr firecrawl_port tokenhub_url tokenhub_install tokenhub_required tokenhub_bind_addr tokenhub_port tokenhub_repo_url tokenhub_ref network_provider network_install network_hostname_prefix tailscale_auth_key_env headscale_manage headscale_login_server headscale_health_url headscale_fleet_url headscale_preauth_key_source headscale_preauth_key_env headscale_port headscale_public_addr headscale_dns headscale_ip_prefix remote_archive ssh_args scp_args ssh_target scp_target nvidia_api_key nvidia_api_base nvidia_base_url openai_api_key openai_base_url anthropic_api_key anthropic_base_url perplexity_api_key perplexity_base_url perplexity_api_base
  IFS='|' read -r agent target os home_channel gateway_model gateway_provider gateway_base_url hub_url bind_host worker_mode worker_capabilities worker_allowed_projects worker_required_metadata worker_require_canary supervisor shared_services_manager qdrant_url qdrant_install qdrant_required qdrant_bind_addr qdrant_port qdrant_image qdrant_memory_limit fleet_name control_port qdrant_data_dir firecrawl_url firecrawl_install firecrawl_required firecrawl_bind_addr firecrawl_port tokenhub_url tokenhub_install tokenhub_required tokenhub_bind_addr tokenhub_port tokenhub_repo_url tokenhub_ref network_provider network_install network_hostname_prefix tailscale_auth_key_env headscale_manage headscale_login_server headscale_health_url headscale_fleet_url headscale_preauth_key_source headscale_preauth_key_env headscale_port headscale_public_addr headscale_dns headscale_ip_prefix <<<"$spec"
  nvidia_api_key="$(fleet_scoped_env NVIDIA_API_KEY "$agent")"
  nvidia_api_base="$(fleet_scoped_env NVIDIA_API_BASE "$agent")"
  nvidia_base_url="$(fleet_scoped_env NVIDIA_BASE_URL "$agent")"
  openai_api_key="$(fleet_scoped_env OPENAI_API_KEY "$agent")"
  openai_base_url="$(fleet_scoped_env OPENAI_BASE_URL "$agent")"
  anthropic_api_key="$(fleet_scoped_env ANTHROPIC_API_KEY "$agent")"
  anthropic_base_url="$(fleet_scoped_env ANTHROPIC_BASE_URL "$agent")"
  perplexity_api_key="$(fleet_scoped_env PERPLEXITY_API_KEY "$agent")"
  perplexity_base_url="$(fleet_scoped_env PERPLEXITY_BASE_URL "$agent")"
  perplexity_api_base="$(fleet_scoped_env PERPLEXITY_API_BASE "$agent")"
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
  if ! ssh -A -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}" "$ssh_target" \
    "MAC_DEPLOY_AGENT=$(shell_quote "$agent") MAC_DEPLOY_OS=$(shell_quote "$os") MAC_DEPLOY_ARCHIVE=$(shell_quote "$remote_archive") MAC_DEPLOY_TS=$(shell_quote "$TS") MAC_DEPLOY_GIT_REV=$(shell_quote "$GIT_REV") MAC_DEPLOY_GIT_URL=$(shell_quote "$GIT_URL") MAC_DEPLOY_GIT_BRANCH=$(shell_quote "$GIT_BRANCH") MAC_DEPLOY_HERMES_SLACK_HOME_CHANNEL_NAME=$(shell_quote "$home_channel") MAC_DEPLOY_HERMES_GATEWAY_MODEL=$(shell_quote "$gateway_model") MAC_DEPLOY_HERMES_GATEWAY_PROVIDER=$(shell_quote "$gateway_provider") MAC_DEPLOY_HERMES_GATEWAY_BASE_URL=$(shell_quote "$gateway_base_url") MAC_DEPLOY_HUB_URL=$(shell_quote "$hub_url") MAC_DEPLOY_HUB_TOKEN=$(shell_quote "$hub_token") MAC_DEPLOY_CONTROL_BIND_HOST=$(shell_quote "$bind_host") MAC_DEPLOY_WORKER_MODE=$(shell_quote "$worker_mode") MAC_DEPLOY_WORKER_CAPABILITIES=$(shell_quote "$worker_capabilities") MAC_DEPLOY_WORKER_ALLOWED_PROJECTS=$(shell_quote "$worker_allowed_projects") MAC_DEPLOY_WORKER_REQUIRED_METADATA=$(shell_quote "$worker_required_metadata") MAC_DEPLOY_WORKER_REQUIRE_CANARY=$(shell_quote "$worker_require_canary") MAC_DEPLOY_SUPERVISOR=$(shell_quote "$supervisor") MAC_DEPLOY_SHARED_SERVICES_MANAGER_AGENT=$(shell_quote "$shared_services_manager") MAC_DEPLOY_QDRANT_URL=$(shell_quote "$qdrant_url") MAC_DEPLOY_QDRANT_INSTALL=$(shell_quote "$qdrant_install") MAC_DEPLOY_REQUIRE_QDRANT_MEMORY=$(shell_quote "$qdrant_required") MAC_DEPLOY_QDRANT_BIND_ADDR=$(shell_quote "$qdrant_bind_addr") MAC_DEPLOY_QDRANT_PORT=$(shell_quote "$qdrant_port") MAC_DEPLOY_QDRANT_IMAGE=$(shell_quote "$qdrant_image") MAC_DEPLOY_QDRANT_MEMORY_LIMIT=$(shell_quote "$qdrant_memory_limit") MAC_DEPLOY_TARGET=$(shell_quote "$target") MAC_DEPLOY_FLEET_NAME=$(shell_quote "$fleet_name") MAC_DEPLOY_CONTROL_PORT=$(shell_quote "$control_port") MAC_DEPLOY_QDRANT_DATA_DIR=$(shell_quote "$qdrant_data_dir") MAC_DEPLOY_FIRECRAWL_URL=$(shell_quote "$firecrawl_url") MAC_DEPLOY_FIRECRAWL_INSTALL=$(shell_quote "$firecrawl_install") MAC_DEPLOY_REQUIRE_FIRECRAWL=$(shell_quote "$firecrawl_required") MAC_DEPLOY_FIRECRAWL_BIND_ADDR=$(shell_quote "$firecrawl_bind_addr") MAC_DEPLOY_FIRECRAWL_PORT=$(shell_quote "$firecrawl_port") MAC_DEPLOY_TOKENHUB_URL=$(shell_quote "$tokenhub_url") MAC_DEPLOY_TOKENHUB_INSTALL=$(shell_quote "$tokenhub_install") MAC_DEPLOY_REQUIRE_TOKENHUB=$(shell_quote "$tokenhub_required") MAC_DEPLOY_TOKENHUB_BIND_ADDR=$(shell_quote "$tokenhub_bind_addr") MAC_DEPLOY_TOKENHUB_PORT=$(shell_quote "$tokenhub_port") MAC_DEPLOY_TOKENHUB_REPO_URL=$(shell_quote "$tokenhub_repo_url") MAC_DEPLOY_TOKENHUB_REF=$(shell_quote "$tokenhub_ref") MAC_DEPLOY_TOKENHUB_API_KEY=$(shell_quote "$tokenhub_api_key") MAC_DEPLOY_NETWORK_PROVIDER=$(shell_quote "$network_provider") MAC_DEPLOY_NETWORK_INSTALL=$(shell_quote "$network_install") MAC_DEPLOY_NETWORK_HOSTNAME_PREFIX=$(shell_quote "$network_hostname_prefix") MAC_DEPLOY_TAILSCALE_AUTH_KEY_ENV=$(shell_quote "$tailscale_auth_key_env") MAC_DEPLOY_HEADSCALE_MANAGE=$(shell_quote "$headscale_manage") MAC_DEPLOY_HEADSCALE_LOGIN_SERVER=$(shell_quote "$headscale_login_server") MAC_DEPLOY_HEADSCALE_HEALTH_URL=$(shell_quote "$headscale_health_url") MAC_DEPLOY_HEADSCALE_FLEET_URL=$(shell_quote "$headscale_fleet_url") MAC_DEPLOY_HEADSCALE_PREAUTH_KEY_SOURCE=$(shell_quote "$headscale_preauth_key_source") MAC_DEPLOY_HEADSCALE_PREAUTH_KEY_ENV=$(shell_quote "$headscale_preauth_key_env") MAC_DEPLOY_HEADSCALE_PORT=$(shell_quote "$headscale_port") MAC_DEPLOY_HEADSCALE_PUBLIC_ADDR=$(shell_quote "$headscale_public_addr") MAC_DEPLOY_HEADSCALE_DNS=$(shell_quote "$headscale_dns") MAC_DEPLOY_HEADSCALE_IP_PREFIX=$(shell_quote "$headscale_ip_prefix") MAC_DEPLOY_DRAIN_MODE=$(shell_quote "${MAC_DEPLOY_DRAIN_MODE:-}") MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS=$(shell_quote "${MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS:-}") MAC_DEPLOY_DRAIN_POLL_SECONDS=$(shell_quote "${MAC_DEPLOY_DRAIN_POLL_SECONDS:-}") MAC_DEPLOY_HUB_TUNNEL_PUBKEY=$(shell_quote "$hub_tunnel_pubkey") MAC_DEPLOY_ALLOW_DEGRADED_SERVICES=$(shell_quote "${allow_degraded_services:-0}") MAC_DEPLOY_GITHUB_REVIEW_KEY_B64=$(shell_quote "$github_review_key_b64") NVIDIA_API_KEY=$(shell_quote "$nvidia_api_key") NVIDIA_API_BASE=$(shell_quote "$nvidia_api_base") NVIDIA_BASE_URL=$(shell_quote "$nvidia_base_url") OPENAI_API_KEY=$(shell_quote "$openai_api_key") OPENAI_BASE_URL=$(shell_quote "$openai_base_url") ANTHROPIC_API_KEY=$(shell_quote "$anthropic_api_key") ANTHROPIC_BASE_URL=$(shell_quote "$anthropic_base_url") PERPLEXITY_API_KEY=$(shell_quote "$perplexity_api_key") PERPLEXITY_BASE_URL=$(shell_quote "$perplexity_base_url") PERPLEXITY_API_BASE=$(shell_quote "$perplexity_api_base") bash -s" <<'REMOTE'
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
TOKENHUB_URL_CONFIGURED="${MAC_DEPLOY_TOKENHUB_URL:-}"
TOKENHUB_INSTALL="${MAC_DEPLOY_TOKENHUB_INSTALL:-auto}"
TOKENHUB_REQUIRE="${MAC_DEPLOY_REQUIRE_TOKENHUB:-1}"
TOKENHUB_BIND_ADDR_CONFIGURED="${MAC_DEPLOY_TOKENHUB_BIND_ADDR:-}"
TOKENHUB_PORT_CONFIGURED="${MAC_DEPLOY_TOKENHUB_PORT:-8090}"
TOKENHUB_REPO_URL_CONFIGURED="${MAC_DEPLOY_TOKENHUB_REPO_URL:-https://github.com/jordanhubbard/tokenhub.git}"
TOKENHUB_REF_CONFIGURED="${MAC_DEPLOY_TOKENHUB_REF:-}"
TOKENHUB_API_KEY_CONFIGURED="${MAC_DEPLOY_TOKENHUB_API_KEY:-}"
NETWORK_PROVIDER="${MAC_DEPLOY_NETWORK_PROVIDER:-tailscale}"
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
# runtime via PYTHONPATH; HERMES_DIR is no longer created.
HERMES_VENDORED="$SRC_DIR/src/mac/_hermes"
export PYTHONPATH="$HERMES_VENDORED:${PYTHONPATH:-}"
BEADS_DIR="$MAC_HOME/vendor/beads"
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
BEADS_REPO_URL="${MAC_DEPLOY_BEADS_REPO_URL:-https://github.com/gastownhall/beads.git}"
BEADS_REF="${MAC_DEPLOY_BEADS_REF:-main}"

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
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
    then
      printf '%s\n' "$candidate"
      return
    fi
  done
  log "ERROR: no Python >= 3.10 found"
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
HERMES_PY="$(hermes_python_bin "$PY")"
SUPERVISOR_KIND=""
export AGENT FLEET_NAME OS_KIND DEPLOY_TS DEPLOY_REV DEPLOY_GIT_URL DEPLOY_GIT_BRANCH DEPLOY_STARTED_ISO HERMES_SLACK_HOME_CHANNEL_NAME HERMES_GATEWAY_MODEL HERMES_GATEWAY_PROVIDER HERMES_GATEWAY_BASE_URL HUB_URL HUB_TUNNEL_PUBKEY CONTROL_BIND_HOST WORKER_MODE WORKER_CAPABILITIES WORKER_ALLOWED_PROJECTS WORKER_REQUIRED_METADATA WORKER_REQUIRE_CANARY SUPERVISOR_REQUESTED SUPERVISOR_KIND SHARED_SERVICES_MANAGER_AGENT QDRANT_URL_CONFIGURED QDRANT_INSTALL QDRANT_REQUIRE QDRANT_BIND_ADDR_CONFIGURED QDRANT_PORT_CONFIGURED QDRANT_IMAGE_CONFIGURED QDRANT_MEMORY_LIMIT_CONFIGURED QDRANT_DATA_DIR_CONFIGURED DRAIN_MODE DRAIN_TIMEOUT_SECONDS DRAIN_POLL_SECONDS MAC_HOME MAC_PORT MAC_SERVICE_NAME HERMES_SERVICE_NAME MAC_AGENT_SERVICE_NAME MAC_LAUNCHD_LABEL HERMES_LAUNCHD_LABEL MAC_AGENT_LAUNCHD_LABEL MAC_SUPERVISORD_PROG HERMES_SUPERVISORD_PROG AGENT_SUPERVISORD_PROG MAC_SUPERVISORD_CONF_NAME SRC_DIR VENV HERMES_DIR BEADS_DIR BEADS_REPO_URL BEADS_REF ENV_FILE LOG_DIR DEPLOY_LOG PY HERMES_PY

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

tokenhub_install_enabled() {
  case "${TOKENHUB_INSTALL:-auto}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    0|false|FALSE|no|NO|off|OFF|none|disabled) return 1 ;;
    auto|"") [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]; return ;;
    *)
      log "ERROR: unsupported MAC_DEPLOY_TOKENHUB_INSTALL value: $TOKENHUB_INSTALL"
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
  python3 -c "import base64, sys; open(sys.argv[1],'wb').write(base64.b64decode(sys.argv[2]))" "$key_file" "$GITHUB_REVIEW_KEY_B64"
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

validate_qdrant_endpoint() {
  local qdrant_url
  qdrant_url="${QDRANT_URL:-${QDRANT_ADDRESS:-${QDRANT_FLEET_URL:-}}}"
  if [ -z "$qdrant_url" ]; then
    log "ERROR: Qdrant shared memory is required but no endpoint is configured"
    exit 1
  fi
  if curl -fsS --connect-timeout 2 --max-time 5 "${qdrant_url%/}/collections" >/dev/null; then
    log "Qdrant shared memory reachable at configured collections endpoint"
    return
  fi
  log "ERROR: Qdrant shared memory is unreachable at ${qdrant_url%/}/collections"
  exit 1
}

validate_firecrawl_endpoint() {
  local firecrawl_url
  firecrawl_url="${FIRECRAWL_API_URL:-${FIRECRAWL_GATEWAY_URL:-${FIRECRAWL_URL_CONFIGURED:-}}}"
  if [ -z "$firecrawl_url" ]; then
    log "ERROR: Firecrawl web search is required but no endpoint is configured"
    exit 1
  fi
  if curl -fsS --connect-timeout 2 --max-time 5 "${firecrawl_url%/}/health" >/dev/null; then
    log "Firecrawl web search reachable at configured health endpoint"
    return
  fi
  log "ERROR: Firecrawl web search is unreachable at ${firecrawl_url%/}/health"
  exit 1
}

validate_tokenhub_endpoint() {
  local tokenhub_url tokenhub_api_key required allow_degraded
  tokenhub_url="${TOKENHUB_URL:-${TOKENHUB_URL_CONFIGURED:-}}"
  tokenhub_api_key="${TOKENHUB_API_KEY:-${TOKENHUB_AGENT_KEY:-${OPENAI_API_KEY:-}}}"
  required="${MAC_REQUIRE_TOKENHUB:-${TOKENHUB_REQUIRE:-1}}"
  allow_degraded="${MAC_TOKENHUB_ALLOW_DEGRADED:-${MAC_DEPLOY_ALLOW_DEGRADED_SERVICES:-0}}"
  if ! truthy "$required"; then
    if [ -z "$tokenhub_url" ]; then
      log "TokenHub is optional and no endpoint is configured"
      return
    fi
    if curl -fsS --connect-timeout 2 --max-time 5 "${tokenhub_url%/}/healthz" >/dev/null; then
      log "Optional TokenHub reachable at configured health endpoint"
    else
      log "WARNING: optional TokenHub is unreachable at ${tokenhub_url%/}/healthz"
    fi
    return
  fi
  if [ -z "$tokenhub_url" ]; then
    if truthy "$allow_degraded"; then
      log "WARNING: TokenHub is required but no endpoint is configured; degraded override is active"
      return
    fi
    log "ERROR: TokenHub is required but no endpoint is configured"
    exit 1
  fi
  if ! curl -fsS --connect-timeout 2 --max-time 5 "${tokenhub_url%/}/healthz" >/dev/null; then
    # The configured URL may be an external DNS name not reachable from inside this host
    # (e.g. a K8s service FQDN when the service doesn't expose the tokenhub port).
    # Fall back to loopback only when tokenhub is installed locally on this node.
    local loopback_port="${MAC_TOKENHUB_PORT:-${TOKENHUB_PORT_CONFIGURED:-8090}}"
    if tokenhub_install_enabled && curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:${loopback_port}/healthz" >/dev/null 2>&1; then
      log "TokenHub health confirmed on loopback; configured URL ${tokenhub_url%/} is not reachable from this host"
      tokenhub_url="http://127.0.0.1:${loopback_port}"
    elif truthy "$allow_degraded"; then
      log "WARNING: TokenHub health endpoint is unreachable; degraded override is active"
      return
    else
      log "ERROR: TokenHub is unreachable at ${tokenhub_url%/}/healthz"
      exit 1
    fi
  fi
  if [ -z "$tokenhub_api_key" ]; then
    if truthy "$allow_degraded"; then
      log "WARNING: TokenHub is reachable but no client key is configured; degraded override is active"
      return
    fi
    log "ERROR: TokenHub client key is required before Hermes/AI paths start"
    exit 1
  fi
  if curl -fsS --connect-timeout 2 --max-time 8 -H "Authorization: Bearer $tokenhub_api_key" "${tokenhub_url%/}/v1/models" >/dev/null; then
    log "TokenHub model routing reachable at configured OpenAI-compatible endpoint"
    return
  fi
  if truthy "$allow_degraded"; then
    log "WARNING: TokenHub /v1/models is unreachable with configured client key; degraded override is active"
    return
  fi
  log "ERROR: TokenHub /v1/models is unreachable with configured client key"
  exit 1
}

reload_mac_env() {
  unset MAC_HERMES_GATEWAY_MODEL ACC_HERMES_GATEWAY_MODEL HERMES_INFERENCE_MODEL ACC_LLM_MODEL
  set -a
  . "$ENV_FILE"
  set +a
}

install_or_validate_tokenhub_service() {
  if tokenhub_install_enabled; then
    log "installing hub-managed TokenHub secret/model routing service"
    if [ -n "$TOKENHUB_BIND_ADDR_CONFIGURED" ]; then
      export TOKENHUB_BIND_ADDR="$TOKENHUB_BIND_ADDR_CONFIGURED"
    else
      unset TOKENHUB_BIND_ADDR
    fi
    if [ -n "$TOKENHUB_URL_CONFIGURED" ]; then
      export TOKENHUB_URL="$TOKENHUB_URL_CONFIGURED"
    else
      unset TOKENHUB_URL
    fi
    export TOKENHUB_PORT="$TOKENHUB_PORT_CONFIGURED"
    export TOKENHUB_REPO_URL="$TOKENHUB_REPO_URL_CONFIGURED"
    export TOKENHUB_REF="$TOKENHUB_REF_CONFIGURED"
    export FLEET_NAME="$FLEET_NAME"
    export TOKENHUB_SUPERVISOR="${TOKENHUB_SUPERVISOR:-auto}"
    MAC_HOME="$MAC_HOME" HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" WORKSPACE="$SRC_DIR" \
      bash "$SRC_DIR/deploy/install-tokenhub-service.sh"
    reload_mac_env
  else
    log "using hub-managed TokenHub from $SHARED_SERVICES_MANAGER_AGENT"
  fi
  validate_tokenhub_endpoint
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
    try:
        existing_fleet = cp.get_fleet(fleet)
    except NotFoundError:
        cp.create_fleet(
            fleet,
            description="Auto-registered deployment fleet",
            metadata=fleet_metadata,
            tenant_id=tenant_id,
            fleet_id=stable_id("fleet", fleet),
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
        "beads_repo_url": os.environ.get("BEADS_REPO_URL") or None,
        "beads_ref": os.environ.get("BEADS_REF") or None,
    },
    "paths": {
        "mac_home": str(mac_home),
        "source": str(Path(os.environ["SRC_DIR"])),
        "mac_venv": str(Path(os.environ["VENV"])),
        "hermes_agent": str(hermes_dir),
        "beads_source": str(Path(os.environ["BEADS_DIR"])),
        "beads_cli": str(mac_home / "bin" / "bd"),
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
        "beads_cli": file_ref(mac_home / "bin" / "bd"),
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

install_beads_cli() {
  local target="$MAC_HOME/bin/bd" existing upgrade source_first missing_required required
  mkdir -p "$MAC_HOME/bin" "$(dirname "$BEADS_DIR")"
  upgrade="${MAC_DEPLOY_BEADS_UPGRADE:-1}"
  source_first="${MAC_DEPLOY_BEADS_SOURCE_FIRST:-1}"
  if [ -x "$target" ]; then
    log "bd CLI already installed at $target"
    "$target" version > "$LOG_DIR/beads-version.txt" 2>&1 || true
    if ! truthy "$upgrade"; then
      return 0
    fi
    log "upgrading bd CLI from $BEADS_REPO_URL@$BEADS_REF"
    existing="$target"
  fi

  if truthy "$source_first"; then
    missing_required=""
    for required in git make go; do
      if ! command -v "$required" >/dev/null 2>&1; then
        missing_required="${missing_required}${missing_required:+, }$required"
      fi
    done
    if [ -z "$missing_required" ]; then
      log "building bd CLI from $BEADS_REPO_URL@$BEADS_REF"
      if [ -d "$BEADS_DIR/.git" ]; then
        git -C "$BEADS_DIR" fetch --quiet origin "$BEADS_REF"
      else
        git clone --quiet "$BEADS_REPO_URL" "$BEADS_DIR"
        git -C "$BEADS_DIR" fetch --quiet origin "$BEADS_REF"
      fi
      git -C "$BEADS_DIR" checkout --quiet FETCH_HEAD
      make -C "$BEADS_DIR" build
      install -m 0755 "$BEADS_DIR/bd" "$target"
      "$target" version > "$LOG_DIR/beads-version.txt" 2>&1 || true
      return 0
    fi
    log "WARNING: cannot build bd CLI from source (missing: $missing_required); falling back to an existing binary or release download"
  fi

  if [ -z "${existing:-}" ]; then
    existing="$(command -v bd 2>/dev/null || true)"
  fi
  if [ -z "$existing" ]; then
    for candidate in "$HOME/.local/bin/bd" "$HOME/bin/bd" /opt/homebrew/bin/bd /usr/local/bin/bd; do
      if [ -x "$candidate" ]; then
        existing="$candidate"
        break
      fi
    done
  fi
  if [ -n "$existing" ] && [ -x "$existing" ]; then
    log "copying existing bd CLI from $existing to managed mac bin"
    if [ "$existing" != "$target" ]; then
      cp -f "$existing" "$target"
      chmod 0755 "$target"
    fi
    "$target" version > "$LOG_DIR/beads-version.txt" 2>&1 || true
    return 0
  fi
  local os_name arch_name dl_url tmp_dir bd_version
  case "$OS_KIND" in
    linux)  os_name="linux" ;;
    darwin) os_name="darwin" ;;
    *)      os_name="" ;;
  esac
  case "$(uname -m 2>/dev/null || true)" in
    x86_64)        arch_name="amd64" ;;
    aarch64|arm64) arch_name="arm64" ;;
    *)             arch_name="" ;;
  esac
  if [ -n "$os_name" ] && [ -n "$arch_name" ] && command -v curl >/dev/null 2>&1; then
    bd_version="$(curl -fsSL "https://api.github.com/repos/gastownhall/beads/releases/latest" 2>/dev/null \
      | grep '"tag_name"' | sed 's/.*"tag_name": *"v\([^"]*\)".*/\1/' | tr -d '\r\n')"
    if [ -n "$bd_version" ]; then
      dl_url="https://github.com/gastownhall/beads/releases/download/v${bd_version}/beads_${bd_version}_${os_name}_${arch_name}.tar.gz"
      log "downloading bd CLI v${bd_version} from GitHub releases"
      tmp_dir="$(mktemp -d)"
      if curl -fsSL "$dl_url" | tar -xz -C "$tmp_dir" 2>/dev/null && [ -x "$tmp_dir/bd" ]; then
        install -m 0755 "$tmp_dir/bd" "$target"
        rm -rf "$tmp_dir"
        "$target" version > "$LOG_DIR/beads-version.txt" 2>&1 || true
        return 0
      fi
      rm -rf "$tmp_dir"
      log "WARNING: bd CLI release download failed; falling back to source build"
    fi
  fi
  for required in git make go; do
    if ! command -v "$required" >/dev/null 2>&1; then
      log "WARNING: bd CLI could not be installed (build prereq missing: $required); Beads lifecycle sync disabled"
      return 1
    fi
  done
  log "building bd CLI from $BEADS_REPO_URL@$BEADS_REF"
  if [ -d "$BEADS_DIR/.git" ]; then
    git -C "$BEADS_DIR" fetch --quiet origin "$BEADS_REF"
  else
    git clone --quiet "$BEADS_REPO_URL" "$BEADS_DIR"
    git -C "$BEADS_DIR" fetch --quiet origin "$BEADS_REF"
  fi
  git -C "$BEADS_DIR" checkout --quiet FETCH_HEAD
  make -C "$BEADS_DIR" build
  install -m 0755 "$BEADS_DIR/bd" "$target"
  "$target" version > "$LOG_DIR/beads-version.txt" 2>&1 || true
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

bootstrap_beads_repositories() {
  local raw="${MAC_BEADS_REPOSITORIES:-}" entry rest repo_path index log_path
  [ -n "$raw" ] || return 0
  index=0
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    if [ "$entry" = "${entry#*=}" ]; then
      log "WARNING: skipping malformed MAC_BEADS_REPOSITORIES entry: $entry"
      continue
    fi
    rest="${entry#*=}"
    repo_path="${rest%%|*}"
    repo_path="${repo_path%%:*}"
    [ -n "$repo_path" ] || continue
    if [ ! -d "$repo_path/.beads" ]; then
      log "WARNING: skipping Beads bootstrap for $repo_path because .beads is absent"
      continue
    fi
    chmod 700 "$repo_path/.beads" 2>/dev/null || true
    git -C "$repo_path" config beads.role maintainer 2>/dev/null || true
    index=$((index + 1))
    log_path="$LOG_DIR/beads-bootstrap-${index}.log"
    log "bootstrapping Beads repository at $repo_path"
    if ! (cd "$repo_path" && "$MAC_BEADS_CLI" bootstrap --yes) > "$log_path" 2>&1; then
      log "ERROR: Beads bootstrap failed for $repo_path; see $log_path"
      cat "$log_path"
      exit 1
    fi
    # Dolt pull is intentionally skipped: embedded dolt's migration
    # rejects the issues table once any worker has touched it, and the
    # JSONL files under .beads/ are already the source of truth (and
    # travel with the repo via git). Re-enable here only if
    # MAC_BEADS_DOLT_SYNC_ENABLED is set on the running fleet.
    if [ "${MAC_BEADS_DOLT_SYNC_ENABLED:-}" = "1" ]; then
      if ! (cd "$repo_path" && "$MAC_BEADS_CLI" dolt pull) >> "$log_path" 2>&1; then
        log "WARNING: Beads Dolt pull failed for $repo_path; bridge polling will report authority drift if the embedded DB is stale"
      fi
    fi
  done <<EOF
${raw//;/$'\n'}
EOF
}

restore_beads_tracked_exports() {
  local raw="${MAC_BEADS_REPOSITORIES:-}" entry rest repo_path index status_path
  case "${MAC_BEADS_RESTORE_TRACKED_EXPORTS:-}" in
    1|true|TRUE|yes|YES|on|ON)
      ;;
    *)
      return 0
      ;;
  esac
  [ -n "$raw" ] || return 0
  index=0
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    [ "$entry" != "${entry#*=}" ] || continue
    rest="${entry#*=}"
    repo_path="${rest%%|*}"
    repo_path="${repo_path%%:*}"
    [ -n "$repo_path" ] || continue
    if ! git -C "$repo_path" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      continue
    fi
    if [ -z "$(git -C "$repo_path" status --porcelain -- .beads/config.yaml .beads/issues.jsonl)" ]; then
      continue
    fi
    index=$((index + 1))
    status_path="$LOG_DIR/beads-tracked-export-restore-${index}.txt"
    git -C "$repo_path" status --porcelain -- .beads/config.yaml .beads/issues.jsonl > "$status_path" || true
    git -C "$repo_path" restore --staged --worktree -- .beads/config.yaml .beads/issues.jsonl
    log "restored tracked Beads export noise in $repo_path; status saved to $status_path"
  done <<EOF
${raw//;/$'\n'}
EOF
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

sync_hermes_tokenhub_client_env() {
  log "syncing Hermes TokenHub client environment"
  "$PY" - "$ENV_FILE" "$HOME/.hermes/.env" <<'PY'
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

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


def sync_tokenhub_credential_pool(hermes_home: Path, tokenhub_url: str, tokenhub_key: str) -> bool:
    if not tokenhub_url or not tokenhub_key:
        return False
    auth_path = hermes_home / "auth.json"
    try:
        auth_store = json.loads(auth_path.read_text(encoding="utf-8")) if auth_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        auth_store = {}
    if not isinstance(auth_store, dict):
        auth_store = {}
    pool = auth_store.get("credential_pool")
    if not isinstance(pool, dict):
        pool = {}
        auth_store["credential_pool"] = pool
    entries = pool.get("custom:tokenhub")
    if not isinstance(entries, list) or not entries:
        entries = [
            {
                "id": "mac-tokenhub",
                "label": "tokenhub",
                "auth_type": "api_key",
                "priority": 0,
                "source": "mac-tokenhub-sync",
            }
        ]
    now = datetime.now(timezone.utc).isoformat()
    entry = entries[0] if isinstance(entries[0], dict) else {}
    entry.update(
        {
            "label": entry.get("label") or "tokenhub",
            "auth_type": "api_key",
            "priority": int(entry.get("priority") or 0),
            "source": "mac-tokenhub-sync",
            "access_token": tokenhub_key,
            "base_url": tokenhub_url.rstrip("/") + "/v1",
            "last_status": "ok",
            "last_status_at": now,
            "last_error_code": None,
            "last_error_reason": None,
            "last_error_message": None,
            "last_error_reset_at": None,
        }
    )
    if not str(entry.get("id") or "").strip():
        entry["id"] = "mac-tokenhub"
    entries[0] = entry
    pool["custom:tokenhub"] = entries
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text(json.dumps(auth_store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    auth_path.chmod(0o600)
    return True


source = read_env(source_path)
tokenhub_url = source.get("TOKENHUB_URL", "").strip().rstrip("/")
tokenhub_key = (source.get("TOKENHUB_API_KEY") or source.get("TOKENHUB_AGENT_KEY") or "").strip()
if not tokenhub_url and not tokenhub_key:
    print("TokenHub client env: skipped; TOKENHUB_URL/TOKENHUB_API_KEY absent")
    raise SystemExit(0)

updates: dict[str, str | None] = {
    # Preserve a provided NVIDIA_API_KEY (the NIM image-gen backend reads it).
    # The NVIDIA *base URLs* are still stripped below so chat never routes to
    # NVIDIA directly — the gateway provider stays pinned to "custom"/TokenHub.
    "NVIDIA_API_KEY": (source.get("NVIDIA_API_KEY") or "").strip() or None,
    "NVIDIA_API_BASE": None,
    "NVIDIA_BASE_URL": None,
    "ANTHROPIC_API_KEY": None,
    "ANTHROPIC_BASE_URL": None,
    "PERPLEXITY_API_KEY": None,
    "PERPLEXITY_BASE_URL": None,
    "PERPLEXITY_API_BASE": None,
    "LLM_KEY": None,
    "LLM_URL": None,
}
if tokenhub_url:
    tokenhub_v1 = tokenhub_url + "/v1"
    updates.update(
        {
            "TOKENHUB_URL": tokenhub_url,
            "OPENAI_BASE_URL": tokenhub_v1,
            "CUSTOM_BASE_URL": tokenhub_v1,
            "MAC_HERMES_GATEWAY_BASE_URL": tokenhub_v1,
            "ACC_HERMES_GATEWAY_BASE_URL": tokenhub_v1,
            "MAC_HERMES_GATEWAY_PROVIDER": "custom",
            "ACC_HERMES_GATEWAY_PROVIDER": "custom",
            "HERMES_INFERENCE_PROVIDER": "custom",
            "MAC_REQUIRE_TOKENHUB": source.get("MAC_REQUIRE_TOKENHUB") or "1",
            "MAC_TOKENHUB_PORT": source.get("MAC_TOKENHUB_PORT") or "8090",
        }
    )
if tokenhub_key:
    updates["TOKENHUB_API_KEY"] = tokenhub_key
    updates["OPENAI_API_KEY"] = tokenhub_key
    updates["MAC_HERMES_GATEWAY_API_KEY"] = tokenhub_key
    updates["ACC_HERMES_GATEWAY_API_KEY"] = tokenhub_key
else:
    updates["TOKENHUB_API_KEY"] = None
    updates["OPENAI_API_KEY"] = None
    updates["MAC_HERMES_GATEWAY_API_KEY"] = None
    updates["ACC_HERMES_GATEWAY_API_KEY"] = None

model = (
    source.get("MAC_HERMES_GATEWAY_MODEL")
    or source.get("HERMES_INFERENCE_MODEL")
    or source.get("ACC_HERMES_GATEWAY_MODEL")
    or source.get("ACC_LLM_MODEL")
    or ""
).strip()
if model == "*":
    model = ""
updates["MAC_HERMES_GATEWAY_MODEL"] = model or None
updates["ACC_HERMES_GATEWAY_MODEL"] = model or None
updates["HERMES_INFERENCE_MODEL"] = model or None
updates["ACC_LLM_MODEL"] = model or None

write_env(target_path, updates)
pool_synced = sync_tokenhub_credential_pool(target_path.parent, tokenhub_url, tokenhub_key)
print("TokenHub client env: synced to %s credential_pool=%s" % (target_path, "synced" if pool_synced else "skipped"))
PY
}

sync_hermes_tokenhub_runtime_config() {
  log "syncing Hermes TokenHub runtime config"
  "$VENV/bin/python" - "$HOME/.hermes/config.yaml" "$HOME/.hermes/.env" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1])
env_path = Path(sys.argv[2])


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


env = read_env(env_path)
tokenhub_url = str(env.get("TOKENHUB_URL") or "").strip().rstrip("/")
tokenhub_key = str(env.get("TOKENHUB_API_KEY") or env.get("TOKENHUB_AGENT_KEY") or "").strip()
if not tokenhub_url:
    print("TokenHub runtime config: skipped; TOKENHUB_URL absent")
    raise SystemExit(0)

config_path.parent.mkdir(parents=True, exist_ok=True)
if config_path.exists():
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8", errors="ignore")) or {}
else:
    loaded = {}
config = loaded if isinstance(loaded, dict) else {}
tokenhub_v1 = tokenhub_url.rstrip("/") + "/v1/"
model = (
    str(env.get("HERMES_INFERENCE_MODEL") or "").strip()
    or str(env.get("MAC_HERMES_GATEWAY_MODEL") or "").strip()
)
if model == "*":
    model = ""

model_config = config.get("model") if isinstance(config.get("model"), dict) else {}
model_config = dict(model_config)
if model:
    model_config["default"] = model
else:
    model_config.pop("default", None)
model_config["provider"] = "tokenhub"
model_config["base_url"] = tokenhub_v1
# Keep the client key in .env and point Hermes at it. Inline keys in config.yaml
# can outlive a TokenHub redeploy and silently break every oneshot worker.
model_config.pop("api_key", None)
config["model"] = model_config

providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
providers = dict(providers)
tokenhub_provider = providers.get("tokenhub") if isinstance(providers.get("tokenhub"), dict) else {}
tokenhub_provider = dict(tokenhub_provider)
tokenhub_provider.update(
    {
        "name": "tokenhub",
        "api": tokenhub_v1,
        "key_env": "TOKENHUB_API_KEY",
        "transport": "chat_completions",
    }
)
if model:
    tokenhub_provider["default_model"] = model
else:
    tokenhub_provider.pop("default_model", None)
tokenhub_provider.pop("api_key", None)
providers["tokenhub"] = tokenhub_provider
config["providers"] = providers

if tokenhub_key:
    # Ensure the env file has the key name referenced by config.yaml even when
    # the deploy input used TOKENHUB_AGENT_KEY.
    lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines() if env_path.exists() else []
    seen = False
    output: list[str] = []
    for line in lines:
        if line.strip().startswith("TOKENHUB_API_KEY="):
            output.append("TOKENHUB_API_KEY=%s" % tokenhub_key)
            seen = True
        else:
            output.append(line)
    if not seen:
        output.append("TOKENHUB_API_KEY=%s" % tokenhub_key)
    env_path.write_text("\n".join(output) + "\n", encoding="utf-8")
    env_path.chmod(0o600)

# Image generation backend: when this agent carries an NVIDIA_API_KEY (a NIM
# image key the operator opted into via the fleet config), make NVIDIA the
# active ``image_gen`` provider so the core ``image_generate`` tool surfaces.
# Chat is unaffected — it stays pinned to the "tokenhub" model/provider above.
nvidia_image_key = str(env.get("NVIDIA_API_KEY") or "").strip()
if nvidia_image_key:
    image_gen_cfg = config.get("image_gen") if isinstance(config.get("image_gen"), dict) else {}
    image_gen_cfg = dict(image_gen_cfg)
    image_gen_cfg["provider"] = "nvidia"
    image_gen_cfg.setdefault("model", "flux.1-dev")
    config["image_gen"] = image_gen_cfg
    print("Image generation: NVIDIA NIM backend enabled (model=%s)" % image_gen_cfg["model"])

config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
config_path.chmod(0o600)
print("TokenHub runtime config: synced to %s" % config_path)
PY
}

fetch_slack_secrets_from_vault() {
  # Pull this agent's Slack tokens from TokenHub vault (centralized
  # secret store). Replaces per-host .env scattering. Idempotent;
  # writes ~/.hermes/slack_accounts.json and updates SLACK_BOT_TOKEN /
  # SLACK_APP_TOKEN in ~/.hermes/config.yaml's env block.
  local fetcher="$SRC_DIR/scripts/mac-fetch-slack-secrets.py"
  if [ ! -f "$fetcher" ]; then
    log "skipping Slack vault fetch: $fetcher not present (older mac source?)"
    return 0
  fi
  local th_env="$HOME/.tokenhub/env"
  local th_admin="${TOKENHUB_ADMIN_TOKEN:-}"
  if [ -z "$th_admin" ] && [ -f "$th_env" ]; then
    th_admin="$(awk -F= '/^TOKENHUB_ADMIN_TOKEN=/{print $2}' "$th_env" | head -1)"
  fi
  local th_url="${TOKENHUB_URL:-${MAC_DEPLOY_TOKENHUB_URL:-http://127.0.0.1:${MAC_DEPLOY_TOKENHUB_PORT:-8090}}}"
  if [ -z "$th_admin" ]; then
    log "skipping Slack vault fetch: TOKENHUB_ADMIN_TOKEN unavailable on $HOME"
    return 0
  fi
  log "fetching Slack secrets for ${AGENT} from TokenHub vault ($th_url)"
  MAC_AGENT_NAME="$AGENT" \
    TOKENHUB_URL="$th_url" \
    TOKENHUB_ADMIN_TOKEN="$th_admin" \
    HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
    "$PY" "$fetcher" >> "$DEPLOY_LOG" 2>&1 || \
      log "WARNING: Slack vault fetch failed for ${AGENT}; will fall back to existing files"
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
    git -C "$SRC_DIR.new" fetch --quiet origin "$DEPLOY_REV"
    git -C "$SRC_DIR.new" merge --ff-only "$DEPLOY_REV"
  fi
else
  log "WARNING: git clone failed or was not configured; installing archive without self-update worktree"
  mkdir -p "$SRC_DIR.new"
  tar -xzf "$ARCHIVE" -C "$SRC_DIR.new"
fi
mv "$SRC_DIR.new" "$SRC_DIR"
rm -f "$ARCHIVE"

install_beads_cli || true
install_github_cli || true

log "creating/updating mac environment file"
"$PY" - "$ENV_FILE" "$MAC_HOME" "$HOME" "$MAC_PORT" "$HERMES_SLACK_HOME_CHANNEL_NAME" "$HERMES_GATEWAY_MODEL" "$HERMES_GATEWAY_PROVIDER" "$HERMES_GATEWAY_BASE_URL" "$HUB_URL" "$HUB_TOKEN" "$CONTROL_BIND_HOST" "$WORKER_MODE" "$WORKER_CAPABILITIES" "$WORKER_ALLOWED_PROJECTS" "$WORKER_REQUIRED_METADATA" "$WORKER_REQUIRE_CANARY" "$AGENT" "$SUPERVISOR_KIND" "$SHARED_SERVICES_MANAGER_AGENT" "$QDRANT_URL_CONFIGURED" "$QDRANT_REQUIRE" "$QDRANT_PORT_CONFIGURED" "$FIRECRAWL_URL_CONFIGURED" "$FIRECRAWL_REQUIRE" "$FIRECRAWL_PORT_CONFIGURED" "$TOKENHUB_URL_CONFIGURED" "$TOKENHUB_REQUIRE" "$TOKENHUB_PORT_CONFIGURED" "$TOKENHUB_API_KEY_CONFIGURED" <<'PY'
from pathlib import Path
import os
import re
import secrets
import sys
import urllib.parse

env_path = Path(sys.argv[1])
mac_home = Path(sys.argv[2])
home = Path(sys.argv[3])
port = sys.argv[4]
configured_home_channel = sys.argv[5].strip().lstrip("#")
configured_gateway_model = sys.argv[6].strip()
if configured_gateway_model == "*":
    configured_gateway_model = ""
configured_gateway_provider = sys.argv[7].strip()
configured_gateway_base_url = sys.argv[8].strip()
configured_hub_url = sys.argv[9].strip()
configured_hub_token = sys.argv[10].strip()
configured_bind_host = sys.argv[11].strip() or "127.0.0.1"
configured_worker_mode = sys.argv[12].strip() or "heartbeat"
configured_worker_capabilities = sys.argv[13].strip() or "ops,python,hermes,review,web_search,web_extract,web_crawl,firecrawl"
configured_worker_allowed_projects = sys.argv[14].strip()
configured_worker_required_metadata = sys.argv[15].strip()
configured_worker_require_canary = sys.argv[16].strip() or "1"
agent_name = sys.argv[17].strip()
supervisor_kind = sys.argv[18].strip()
shared_services_manager = sys.argv[19].strip() or agent_name
configured_qdrant_url = sys.argv[20].strip()
configured_qdrant_required = "1"
configured_qdrant_port = sys.argv[22].strip() or "6333"
configured_firecrawl_url = sys.argv[23].strip()
configured_firecrawl_required = "1"
configured_firecrawl_port = sys.argv[25].strip() or "3002"
configured_tokenhub_url = sys.argv[26].strip()
configured_tokenhub_required = sys.argv[27].strip() or "1"
configured_tokenhub_port = sys.argv[28].strip() or "8090"
configured_tokenhub_api_key = sys.argv[29].strip()
network_provider = (
    os.environ.get("MAC_DEPLOY_NETWORK_PROVIDER")
    or os.environ.get("NETWORK_PROVIDER")
    or "tailscale"
).strip().lower()


def stable_id(prefix, value):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.lower()).strip("_")
    return "%s_%s" % (prefix, safe or "default")


values = {}
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

values.setdefault("MAC_SECRET_KEY", secrets.token_urlsafe(48))
values.setdefault("MAC_API_TOKEN", secrets.token_urlsafe(32))
values["MAC_DB"] = str(mac_home / "mac.db")
values["MAC_PORT"] = port
values["MAC_BIND_HOST"] = configured_bind_host
if configured_worker_mode == "loop" and agent_name == shared_services_manager:
    # Hub nodes connect to their own local control plane; the external service
    # DNS may not expose the API port (e.g. K8s Service without port mapping).
    values["MAC_HUB_URL"] = f"http://127.0.0.1:{port}"
elif network_provider in {"tailscale", "headscale"} and configured_hub_url:
    values["MAC_HUB_URL"] = configured_hub_url.rstrip("/")
else:
    # Legacy/fallback topology: workers reach the hub through a local reverse SSH tunnel.
    values["MAC_HUB_URL"] = "http://127.0.0.1:18789"
values["MAC_SUPERVISOR_KIND"] = supervisor_kind
values["HERMES_HOME"] = str(home / ".hermes")
values["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
values["HERMES_REDACT_SECRETS"] = "true"
values["ACC_DIR"] = str(home / ".acc")
values["MAC_HERMES_AGENT_DIR"] = str(mac_home / "src" / "mac" / "src" / "mac" / "_hermes")
values["MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM"] = "1"
values["MAC_HERMES_APPLY_GATEWAY_RUNTIME_SHIM"] = "1"
values["MAC_HERMES_STARTUP_CHECK"] = "1"
values.setdefault("MAC_REQUIRE_HERMES_STARTUP_READY", "0")
values["MAC_URL"] = values["MAC_HUB_URL"]
values["MAC_FLEET_NAME"] = os.environ.get("FLEET_NAME") or "mac"
values["MAC_FLEET_TENANT_ID"] = stable_id("tenant", os.environ.get("FLEET_NAME") or "mac")
values["MAC_AGENT_ID"] = stable_id("agent", agent_name)
values["MAC_HERMES_PERSONA_ID"] = stable_id("persona", agent_name)
values["MAC_HERMES_INSTANCE_ID"] = stable_id("hermes", agent_name)
values["MAC_WORKER_HERMES_INSTANCE_ID"] = values["MAC_HERMES_INSTANCE_ID"]
values["MAC_HERMES_RUNTIME_CONTEXT_FILE"] = str(home / ".hermes" / "mac-runtime-context.json")
values["MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN"] = str(home / ".hermes" / "mac-runtime-context.md")
values["MAC_HERMES_RUNTIME_CONTEXT_REQUIRED"] = "1"
values["MAC_HERMES_WORKSPACE"] = str(mac_home / "src" / "mac")
values["MAC_PROJECT_CONTRACT_FILE"] = str(mac_home / "src" / "mac" / ".mac" / "project.yaml")
values["MAC_SELF_UPDATE_REPO"] = str(mac_home / "src" / "mac")
values["MAC_BEADS_CLI"] = str(mac_home / "bin" / "bd")
values.setdefault("MAC_BEADS_BRIDGE_ROOT", str(mac_home / "beads-checkouts"))
if configured_gateway_model:
    values["MAC_HERMES_GATEWAY_MODEL"] = configured_gateway_model
    values["ACC_HERMES_GATEWAY_MODEL"] = configured_gateway_model
    values["HERMES_INFERENCE_MODEL"] = configured_gateway_model
    values["ACC_LLM_MODEL"] = configured_gateway_model
if configured_gateway_provider:
    values["MAC_HERMES_GATEWAY_PROVIDER"] = configured_gateway_provider
    values["ACC_HERMES_GATEWAY_PROVIDER"] = configured_gateway_provider
    values["HERMES_INFERENCE_PROVIDER"] = configured_gateway_provider
if configured_gateway_base_url:
    values["MAC_HERMES_GATEWAY_BASE_URL"] = configured_gateway_base_url
    values["ACC_HERMES_GATEWAY_BASE_URL"] = configured_gateway_base_url
    values["CUSTOM_BASE_URL"] = configured_gateway_base_url
    values["OPENAI_BASE_URL"] = configured_gateway_base_url
if configured_worker_mode == "loop" and agent_name == shared_services_manager:
    # Hub node: agent must always use the local API token.
    values["MAC_WORKER_TOKEN"] = values["MAC_API_TOKEN"]
elif configured_hub_token:
    values["MAC_WORKER_TOKEN"] = configured_hub_token
else:
    values.setdefault("MAC_WORKER_TOKEN", values["MAC_API_TOKEN"])
values["MAC_WORKER_AGENT_NAME"] = agent_name
values["MAC_WORKER_HOSTNAME"] = agent_name
values["MAC_WORKER_MODE"] = configured_worker_mode
values["MAC_WORKER_CAPABILITIES"] = configured_worker_capabilities
values["MAC_WORKER_REQUIRE_CANARY"] = configured_worker_require_canary
values["MAC_WORKER_ALLOWED_PROJECTS"] = configured_worker_allowed_projects
values["MAC_WORKER_REQUIRED_METADATA"] = configured_worker_required_metadata
values["MAC_SHARED_SERVICES_MANAGER_AGENT"] = shared_services_manager
values["MAC_REQUIRE_QDRANT_MEMORY"] = "1"
values["MAC_QDRANT_MEMORY_ROLE"] = "shared_level2"
values["MAC_MEMORY_TOPOLOGY_FILE"] = str(home / ".hermes" / "mac-memory-topology.json")
values["MAC_REQUIRE_FIRECRAWL"] = "1"

def truthy(raw):
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}

def qdrant_url():
    if configured_qdrant_url:
        return configured_qdrant_url.rstrip("/")
    raw_hub = values.get("MAC_HUB_URL") or configured_hub_url or "http://127.0.0.1:8789"
    parsed = urllib.parse.urlsplit(raw_hub)
    if not parsed.scheme or not parsed.hostname:
        return ""
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    qd_port = int(configured_qdrant_port or "6333")
    native_hub_port = int(port or "8789")
    hub_port = parsed.port or native_hub_port
    if hub_port == native_hub_port + 10000:
        qd_port += 10000
    return urllib.parse.urlunsplit((parsed.scheme, "%s:%s" % (host, qd_port), "", "", ""))

derived_qdrant_url = qdrant_url()
if derived_qdrant_url:
    values["QDRANT_URL"] = derived_qdrant_url
    values["QDRANT_ADDRESS"] = derived_qdrant_url
    values["QDRANT_FLEET_URL"] = derived_qdrant_url

def firecrawl_url():
    if configured_firecrawl_url:
        return configured_firecrawl_url.rstrip("/")
    # Use MAC_HUB_URL (not raw configured_hub_url) to detect tunnel: workers
    # reach the hub via a reverse SSH tunnel where the local port is
    # native_port + 10000. Apply the same offset to the Firecrawl port so
    # workers use the tunnel-forwarded port (e.g. 3002 -> 13002).
    mac_hub_url = values.get("MAC_HUB_URL") or configured_hub_url or "http://127.0.0.1:8789"
    parsed = urllib.parse.urlsplit(mac_hub_url)
    if not parsed.scheme or not parsed.hostname:
        return ""
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    fc_port = int(configured_firecrawl_port or "3002")
    native_hub_port = int(port or "8789")
    hub_port = parsed.port or native_hub_port
    if hub_port == native_hub_port + 10000:
        fc_port += 10000
    return urllib.parse.urlunsplit((parsed.scheme, "%s:%s" % (host, fc_port), "", "", ""))

derived_firecrawl_url = firecrawl_url()
if derived_firecrawl_url:
    values["FIRECRAWL_API_URL"] = derived_firecrawl_url
    values["FIRECRAWL_GATEWAY_URL"] = derived_firecrawl_url
    values.setdefault("FIRECRAWL_API_KEY", "none")
    values["MAC_WEB_SEARCH_PROVIDER"] = "firecrawl"
    values["MAC_WEB_SEARCH_URL"] = derived_firecrawl_url
    values["HERMES_WEB_SEARCH_BACKEND"] = "firecrawl"
    values["HERMES_WEB_EXTRACT_BACKEND"] = "firecrawl"

provider_secret_keys = (
    # NVIDIA_API_KEY is intentionally NOT stripped: it's the NVIDIA NIM
    # image-generation backend's key. Chat routing is unaffected because the
    # NVIDIA base URLs (below) are still removed and the gateway provider is
    # pinned to "custom"/TokenHub.
    "NVIDIA_API_BASE",
    "NVIDIA_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "PERPLEXITY_API_KEY",
    "PERPLEXITY_BASE_URL",
    "PERPLEXITY_API_BASE",
    "LLM_KEY",
    "LLM_URL",
)
for key in provider_secret_keys:
    values.pop(key, None)

def tokenhub_url():
    if configured_tokenhub_url:
        return configured_tokenhub_url.rstrip("/")
    # Use the derived MAC_HUB_URL (not the raw spec hub URL) to detect whether
    # this agent accesses the hub via a reverse SSH tunnel. MAC_HUB_URL is set
    # to 127.0.0.1:18789 for tunnel-connected workers and 127.0.0.1:<port> for
    # the hub itself — the raw configured_hub_url is always the native port.
    mac_hub_url = values.get("MAC_HUB_URL") or configured_hub_url or "http://127.0.0.1:8789"
    parsed = urllib.parse.urlsplit(mac_hub_url)
    if not parsed.scheme or not parsed.hostname:
        return ""
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    th_port = int(configured_tokenhub_port or "8090")
    native_hub_port = int(port or "8789")
    hub_port = parsed.port or native_hub_port
    # Workers access the hub via a reverse SSH tunnel where the local port is
    # native_port + 10000 (e.g. 8789 -> 18789). Apply the same offset to tokenhub
    # so workers use the tunnel-forwarded tokenhub port (e.g. 8090 -> 18090).
    if hub_port == native_hub_port + 10000:
        th_port += 10000
    return urllib.parse.urlunsplit((parsed.scheme, "%s:%s" % (host, th_port), "", "", ""))

derived_tokenhub_url = tokenhub_url()
if derived_tokenhub_url:
    tokenhub_v1 = derived_tokenhub_url.rstrip("/") + "/v1"
    values["TOKENHUB_URL"] = derived_tokenhub_url
    values["MAC_REQUIRE_TOKENHUB"] = configured_tokenhub_required
    values["MAC_TOKENHUB_PORT"] = configured_tokenhub_port
    values["OPENAI_BASE_URL"] = tokenhub_v1
    values["CUSTOM_BASE_URL"] = tokenhub_v1
    values["MAC_HERMES_GATEWAY_BASE_URL"] = tokenhub_v1
    values["ACC_HERMES_GATEWAY_BASE_URL"] = tokenhub_v1
    values["MAC_HERMES_GATEWAY_PROVIDER"] = "custom"
    values["ACC_HERMES_GATEWAY_PROVIDER"] = "custom"
    values["HERMES_INFERENCE_PROVIDER"] = "custom"
    if configured_gateway_model:
        values["MAC_HERMES_GATEWAY_MODEL"] = configured_gateway_model
        values["ACC_HERMES_GATEWAY_MODEL"] = configured_gateway_model
        values["HERMES_INFERENCE_MODEL"] = configured_gateway_model
        values["ACC_LLM_MODEL"] = configured_gateway_model
    else:
        values.pop("MAC_HERMES_GATEWAY_MODEL", None)
        values.pop("ACC_HERMES_GATEWAY_MODEL", None)
        values.pop("HERMES_INFERENCE_MODEL", None)
        values.pop("ACC_LLM_MODEL", None)
    if configured_tokenhub_api_key:
        values["TOKENHUB_API_KEY"] = configured_tokenhub_api_key
        values["OPENAI_API_KEY"] = configured_tokenhub_api_key
        values["MAC_HERMES_GATEWAY_API_KEY"] = configured_tokenhub_api_key
        values["ACC_HERMES_GATEWAY_API_KEY"] = configured_tokenhub_api_key
    else:
        values.pop("OPENAI_API_KEY", None)
        values.pop("MAC_HERMES_GATEWAY_API_KEY", None)
        values.pop("ACC_HERMES_GATEWAY_API_KEY", None)
elif not truthy(configured_tokenhub_required):
    for key in (
        "TOKENHUB_URL",
        "TOKENHUB_API_KEY",
        "TOKENHUB_AGENT_KEY",
        "OPENAI_API_KEY",
        "MAC_HERMES_GATEWAY_API_KEY",
        "ACC_HERMES_GATEWAY_API_KEY",
        "OPENAI_BASE_URL",
        "CUSTOM_BASE_URL",
        "MAC_HERMES_GATEWAY_BASE_URL",
        "ACC_HERMES_GATEWAY_BASE_URL",
        "MAC_REQUIRE_TOKENHUB",
    ):
        values.pop(key, None)
values.setdefault("MAC_WORKER_WORKSPACE", str(mac_home / "agent-workspaces"))
values.setdefault("MAC_WORKER_HEARTBEAT_INTERVAL", "30")
values.setdefault("MAC_WORKER_POLL_INTERVAL", "2")
values.setdefault("MAC_WORKER_LEASE_SECONDS", "900")
values.setdefault("MAC_WORKER_EXECUTOR", str(mac_home / "bin" / "mac-hermes-task-executor"))
values.setdefault("MAC_AGENT_STARTUP_SELF_TEST", "1")
values.setdefault("MAC_AGENT_STARTUP_SELF_TEST_TIMEOUT", "120")
values["MAC_BEADS_BRIDGE_HUB_AGENT"] = shared_services_manager
values.setdefault("MAC_REVIEW_TICK_HUB_AGENT", shared_services_manager)
values.setdefault("MAC_BEADS_RESTORE_TRACKED_EXPORTS", "1")
if agent_name == shared_services_manager:
    values["MAC_BEADS_BRIDGE_ON_HEARTBEAT"] = "1"
    values["MAC_BEADS_AUTO_PULL"] = "1"
    values.setdefault(
        "MAC_BEADS_REPOSITORIES",
        "mac=%s:repo-beads-mac:repo-beads-mac::30" % (mac_home / "src" / "mac"),
    )
else:
    values.setdefault("MAC_BEADS_BRIDGE_ON_HEARTBEAT", "0")
if "MAC_BEADS_REPOSITORIES" in values:
    # Older generated env files used "|" as an internal separator. That is
    # readable by Python but invalid in a shell-sourced env file because it is
    # parsed as a pipeline. Normalize before the file is sourced below.
    values["MAC_BEADS_REPOSITORIES"] = values["MAC_BEADS_REPOSITORIES"].replace("|", ":")
home_channel = (
    configured_home_channel
    or values.get("MAC_HERMES_SLACK_HOME_CHANNEL_NAME", "").strip().lstrip("#")
    or values.get("ACC_SLACK_HOME_CHANNEL_NAME", "").strip().lstrip("#")
    or values.get("SLACK_HOME_CHANNEL_NAME", "").strip().lstrip("#")
    or ""
)
values["MAC_HERMES_SLACK_HOME_CHANNEL_NAME"] = home_channel
values["ACC_SLACK_HOME_CHANNEL_NAME"] = home_channel
values["SLACK_HOME_CHANNEL_NAME"] = home_channel
values.setdefault("MAC_HERMES_SYNC_SLACK_HOME_CHANNELS", "1")
values.setdefault("SLACK_ALLOWED_USERS", "*")
values.setdefault("SLACK_REQUIRE_MENTION", "true")
values.setdefault("SLACK_STRICT_MENTION", "true")

lines = [
    "# Generated by mac deploy/deploy-mac-fleet.sh.",
    "# Contains bearer tokens; keep mode 0600.",
]
for key in sorted(values):
    lines.append(f"{key}={values[key]}")
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
env_path.chmod(0o600)
PY

normalize_hermes_redaction_env

reload_mac_env
install_or_validate_tokenhub_service
reload_mac_env
sync_hermes_tokenhub_client_env
reload_mac_env
fetch_slack_secrets_from_vault
reload_mac_env
sync_hermes_slack_identity_env
[ -x "$MAC_HOME/bin/bd" ] && bootstrap_beads_repositories || true
[ -x "$MAC_HOME/bin/bd" ] && restore_beads_tracked_exports || true
if [ "$WORKER_MODE" = "loop" ]; then
  ensure_hub_tunnel_key
else
  install_hub_tunnel_pubkey
fi
install_github_review_key
install_or_validate_shared_services
write_hermes_memory_topology
sync_hermes_home_channels

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
sync_hermes_tokenhub_runtime_config
ensure_hermes_identity_memory_continuity
apply_hermes_gateway_runtime_shim
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
if [ -z "${CUSTOM_BASE_URL:-}" ] && [ -n "${TOKENHUB_URL:-}" ]; then
  export CUSTOM_BASE_URL="${TOKENHUB_URL%/}/v1"
fi
if [ -z "${OPENAI_BASE_URL:-}" ] && [ -n "${CUSTOM_BASE_URL:-}" ]; then
  export OPENAI_BASE_URL="$CUSTOM_BASE_URL"
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
  if [ -n "${TOKENHUB_API_KEY:-}" ]; then
    export OPENAI_API_KEY="$TOKENHUB_API_KEY"
  elif [ -n "${TOKENHUB_AGENT_KEY:-}" ]; then
    export OPENAI_API_KEY="$TOKENHUB_AGENT_KEY"
  fi
fi
if [ -z "${MAC_HERMES_GATEWAY_API_KEY:-}" ]; then
  if [ -n "${TOKENHUB_API_KEY:-}" ]; then
    export MAC_HERMES_GATEWAY_API_KEY="$TOKENHUB_API_KEY"
  elif [ -n "${TOKENHUB_AGENT_KEY:-}" ]; then
    export MAC_HERMES_GATEWAY_API_KEY="$TOKENHUB_AGENT_KEY"
  fi
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
if [ -z "${CUSTOM_BASE_URL:-}" ] && [ -n "${TOKENHUB_URL:-}" ]; then
  export CUSTOM_BASE_URL="${TOKENHUB_URL%/}/v1"
fi
if [ -z "${OPENAI_BASE_URL:-}" ] && [ -n "${CUSTOM_BASE_URL:-}" ]; then
  export OPENAI_BASE_URL="$CUSTOM_BASE_URL"
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
  if [ -n "${TOKENHUB_API_KEY:-}" ]; then
    export OPENAI_API_KEY="$TOKENHUB_API_KEY"
  elif [ -n "${TOKENHUB_AGENT_KEY:-}" ]; then
    export OPENAI_API_KEY="$TOKENHUB_AGENT_KEY"
  fi
fi
if [ -z "${MAC_HERMES_GATEWAY_API_KEY:-}" ]; then
  if [ -n "${TOKENHUB_API_KEY:-}" ]; then
    export MAC_HERMES_GATEWAY_API_KEY="$TOKENHUB_API_KEY"
  elif [ -n "${TOKENHUB_AGENT_KEY:-}" ]; then
    export MAC_HERMES_GATEWAY_API_KEY="$TOKENHUB_AGENT_KEY"
  fi
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
tokenhub_url = str(os.environ.get("TOKENHUB_URL") or "").rstrip("/")
tokenhub_key = os.environ.get("TOKENHUB_API_KEY") or os.environ.get("TOKENHUB_AGENT_KEY") or ""
tokenhub_required = truthy(os.environ.get("MAC_REQUIRE_TOKENHUB") or "1")
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
    "tokenhub_runtime": False,
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

if tokenhub_required and not tokenhub_url:
    problems.append("TOKENHUB_URL is required but not configured")
if tokenhub_required and not tokenhub_key:
    problems.append("TOKENHUB_API_KEY is required but not configured")

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
    runtime_key = str(runtime_provider.get("api_key") or "")
    runtime_base_url = str(runtime_provider.get("base_url") or "").rstrip("/")
    expected_base_url = f"{tokenhub_url}/v1" if tokenhub_url else ""
    key_matches_env = bool(tokenhub_key and runtime_key == tokenhub_key)
    base_url_matches_env = bool(
        not expected_base_url or runtime_base_url == expected_base_url.rstrip("/")
    )
    runtime_provider = {
        "provider": runtime_provider.get("provider"),
        "source": runtime_provider.get("source"),
        "model": runtime_provider.get("model"),
        "base_url_matches_env": base_url_matches_env,
        "key_matches_env": key_matches_env,
    }
    if tokenhub_key and not key_matches_env:
        problems.append("Hermes runtime provider key does not match TOKENHUB_API_KEY")
    if expected_base_url and not base_url_matches_env:
        problems.append("Hermes runtime provider base_url does not match TOKENHUB_URL")
    checks["tokenhub_runtime"] = not any(
        problem.startswith("Hermes runtime provider") or problem.startswith("TOKENHUB_")
        for problem in problems
    )
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
if [ -z "${CUSTOM_BASE_URL:-}" ] && [ -n "${TOKENHUB_URL:-}" ]; then
  export CUSTOM_BASE_URL="${TOKENHUB_URL%/}/v1"
fi
if [ -z "${OPENAI_BASE_URL:-}" ] && [ -n "${CUSTOM_BASE_URL:-}" ]; then
  export OPENAI_BASE_URL="$CUSTOM_BASE_URL"
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
  if [ -n "${TOKENHUB_API_KEY:-}" ]; then
    export OPENAI_API_KEY="$TOKENHUB_API_KEY"
  elif [ -n "${TOKENHUB_AGENT_KEY:-}" ]; then
    export OPENAI_API_KEY="$TOKENHUB_AGENT_KEY"
  fi
fi
if [ -z "${MAC_HERMES_GATEWAY_API_KEY:-}" ]; then
  if [ -n "${TOKENHUB_API_KEY:-}" ]; then
    export MAC_HERMES_GATEWAY_API_KEY="$TOKENHUB_API_KEY"
  elif [ -n "${TOKENHUB_AGENT_KEY:-}" ]; then
    export MAC_HERMES_GATEWAY_API_KEY="$TOKENHUB_AGENT_KEY"
  fi
fi
if [ -z "${ACC_HERMES_GATEWAY_API_KEY:-}" ] && [ -n "${MAC_HERMES_GATEWAY_API_KEY:-}" ]; then
  export ACC_HERMES_GATEWAY_API_KEY="$MAC_HERMES_GATEWAY_API_KEY"
fi
exec "$HOME/.mac/venv/bin/python" "$HOME/.mac/bin/mac-hermes-task-executor.py"
EOF
  chmod 700 "$executor"

  cat > "$executor_py" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def sha256_text(value: str) -> str:
    return "sha256:%s" % hashlib.sha256(value.encode("utf-8")).hexdigest()


def command_audit_id() -> str:
    seed = "%s:%s" % (time.time_ns(), os.getpid())
    return "cmd_%s" % hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def audit_safe_argv(argv: list[str]) -> list[str]:
    safe: list[str] = []
    redact_next = False
    for raw in argv:
        arg = str(raw)
        lowered = arg.lower()
        if redact_next:
            safe.append(redacted_arg(arg))
            redact_next = False
            continue
        if lowered in {"--token", "--api-key", "--key", "--secret", "--password"}:
            safe.append(arg)
            redact_next = True
            continue
        if any(marker in lowered for marker in ("bearer ", "token=", "api_key=", "apikey=", "password=", "secret=")):
            safe.append(redacted_arg(arg))
            continue
        if len(arg) > 512:
            safe.append("<truncated:%s:chars=%d>" % (sha256_text(arg), len(arg)))
            continue
        safe.append(arg)
    return safe


def redacted_arg(value: str) -> str:
    return "<redacted:%s:chars=%d>" % (sha256_text(value), len(value))


def safe_path_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)[:180]


def local_agent_id() -> str:
    configured = os.environ.get("MAC_AGENT_ID") or os.environ.get("MAC_WORKER_AGENT_ID")
    if configured:
        return configured
    name = os.environ.get("MAC_WORKER_AGENT_NAME") or os.uname().nodename.split(".")[0]
    return "agent_%s" % (safe_path_component(name.lower()).strip("_") or "default")


def post_command_audit(agent_id: str, payload: dict) -> None:
    base_url = (os.environ.get("MAC_HUB_URL") or os.environ.get("MAC_URL") or "").rstrip("/")
    token = os.environ.get("MAC_WORKER_TOKEN") or os.environ.get("MAC_TOKEN") or os.environ.get("MAC_API_TOKEN")
    if not base_url or not token or not agent_id:
        return
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        "%s/agents/%s/command-audit" % (base_url, agent_id),
        data=data,
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except Exception:
        pass


def run_audited_command(argv: list[str], cwd: Path, task_id, metadata: dict) -> subprocess.CompletedProcess[str]:
    command_id = command_audit_id()
    agent_id = local_agent_id()
    started_at = utcnow()
    started = time.monotonic()
    argv_hash = sha256_text(json.dumps(argv, separators=(",", ":")))
    base = {
        "command_id": command_id,
        "argv": audit_safe_argv(argv),
        "cwd": str(cwd),
        "task_id": task_id,
        "started_at": started_at,
        "metadata": {"component": "mac-hermes-task-executor", "argv_sha256": argv_hash, **metadata},
    }
    post_command_audit(agent_id, {**base, "phase": "started"})
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        post_command_audit(
            agent_id,
            {
                **base,
                "phase": "error",
                "completed_at": utcnow(),
                "duration_ms": (time.monotonic() - started) * 1000.0,
                "metadata": {**base["metadata"], "error": str(exc)},
            },
        )
        raise
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    post_command_audit(
        agent_id,
        {
            **base,
            "phase": "completed" if result.returncode == 0 else "failed",
            "completed_at": utcnow(),
            "duration_ms": (time.monotonic() - started) * 1000.0,
            "returncode": result.returncode,
            "stdout_sha256": sha256_text(stdout),
            "stderr_sha256": sha256_text(stderr),
            "stdout_bytes": len(stdout.encode("utf-8")),
            "stderr_bytes": len(stderr.encode("utf-8")),
        },
    )
    return result


def repository_contract_section(task: dict) -> str:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    origin = metadata.get("origin") if isinstance(metadata, dict) else {}
    contract = origin.get("repository_contract") if isinstance(origin, dict) else None
    if not isinstance(contract, dict):
        return (
            "No repository runtime contract is attached. Do not guess bootstrap or "
            "test commands; report this as a task contract failure."
        )
    summary = {
        "schema": contract.get("schema"),
        "project": contract.get("project"),
        "contract_path": contract.get("contract_path"),
        "platforms": contract.get("platforms"),
        "toolchain": contract.get("toolchain"),
        "bootstrap": contract.get("bootstrap"),
        "test": contract.get("test"),
        "evidence": contract.get("evidence"),
    }
    return "\n".join(
        [
            json.dumps(summary, indent=2, sort_keys=True),
            "For normal repository tasks, MAC prepares a task-owned git worktree before the executor starts.",
            "Use $MAC_TASK_REPO_WORKTREE, or metadata.runtime.repository_worktree in task.json, as the only writable checkout.",
            "Treat origin.repository_path / $MAC_TASK_REPO_SOURCE as read-only registered source state; do not edit it for feature or bug work.",
            "The registered checkout must remain clean. Commit, test, and publish from the task worktree branch, then report the pushed ref in evidence.",
            "Only explicit source-remediation tasks may repair origin.repository_path directly.",
            "Before build or test work, run bootstrap.command from the repository root when the declared tools or bootstrap.creates outputs are missing.",
            "Use test.command as the canonical verification command unless the task explicitly narrows the check.",
        ]
    )


def task_evidence_type(task: dict) -> str:
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    contract = metadata.get("execution_contract") if isinstance(metadata, dict) else {}
    evidence_type = str(contract.get("evidence_type") or "").strip().lower() if isinstance(contract, dict) else ""
    allowed = {
        "repo_change",
        "documentation",
        "investigation",
        "deployment",
        "test",
        "artifact",
        "no_change",
        "operator_result",
    }
    return evidence_type if evidence_type in allowed else "operator_result"


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def run_deterministic_git_finalizer(task_workspace: Path, task: dict) -> None:
    """mac-jfns: enforce deterministic repo_change evidence for tasks
    declaring publication_target=git://main. Runs after the LLM-driven
    Hermes step. Commits any uncommitted work, pushes the lease branch,
    runs the contract test suite, then writes an authoritative
    mac-evidence.json that reflects real git state (overriding whatever
    evidence_type / pushed / dirty flags the LLM proposed).
    """
    metadata = task.get("metadata") or {}
    publication_target = str(metadata.get("publication_target") or "").strip()
    if not publication_target.startswith("git://"):
        return
    worktree = os.environ.get("MAC_TASK_REPO_WORKTREE", "").strip()
    if not worktree:
        # Some tasks ship the worktree path inside the task json
        rt = metadata.get("runtime") if isinstance(metadata.get("runtime"), dict) else {}
        worktree = str(rt.get("repository_worktree") or "").strip()
    worktree_path = Path(worktree).expanduser() if worktree else None
    if not worktree_path or not worktree_path.is_dir() or not (worktree_path / ".git").exists():
        return
    # Auto-commit any uncommitted changes (the LLM may have edited files but skipped commit)
    status = _git(["status", "--porcelain"], worktree_path)
    if status.stdout.strip():
        _git(["add", "-A"], worktree_path)
        commit_msg = "auto-commit: %s" % task.get("id", "unknown")
        _git(
            [
                "-c", "user.email=mac-fleet@nvidia.com",
                "-c", "user.name=MAC fleet",
                "commit",
                "-m", commit_msg,
            ],
            worktree_path,
        )
    head_sha = _git(["rev-parse", "HEAD"], worktree_path).stdout.strip()
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path).stdout.strip() or "HEAD"
    # Push the current branch to origin (idempotent on re-runs)
    pushed = False
    push_target = "refs/heads/%s" % branch if branch != "HEAD" else "refs/heads/auto/%s" % (task.get("id") or "task")
    push = _git(["push", "origin", "HEAD:%s" % push_target], worktree_path)
    if push.returncode == 0:
        pushed = True
    # Run contract tests
    contract = (metadata.get("origin") or {}).get("repository_contract") or {}
    test_cmd = ((contract.get("test") or {}).get("command") or "scripts/run-contract-tests.sh").strip()
    tests = None
    if test_cmd:
        tr = subprocess.run(
            ["bash", "-lc", test_cmd],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        tail = (tr.stdout or "") + "\n" + (tr.stderr or "")
        # Extract pytest summary if present
        import re as _re
        passed = failed = total = None
        m = _re.search(r"(\d+) passed", tail)
        if m:
            passed = int(m.group(1))
        m = _re.search(r"(\d+) failed", tail)
        if m:
            failed = int(m.group(1))
        if passed is not None or failed is not None:
            total = (passed or 0) + (failed or 0)
        tests = {
            "command": test_cmd,
            "returncode": int(tr.returncode),
            "passed": passed,
            "failed": failed,
            "total": total,
            "status": "pass" if tr.returncode == 0 else "fail",
        }
    # Compute files_changed relative to origin/main
    _git(["fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"], worktree_path)
    diff = _git(["diff", "--name-only", "origin/main..HEAD"], worktree_path)
    files_changed = [f for f in (diff.stdout or "").splitlines() if f.strip()]
    final_status = _git(["status", "--porcelain"], worktree_path).stdout.strip()
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "summary": "Deterministic finalizer: commit+push+test for %s" % task.get("id"),
        "repo": {
            "head_sha": head_sha,
            "pushed": pushed,
            "remote_ref": "refs/heads/" + branch if branch != "HEAD" else push_target,
            "dirty": bool(final_status),
            "files_changed": files_changed,
        },
        "tests": tests,
        "checks": [
            {
                "name": "git_finalizer",
                "returncode": 0 if pushed and (tests is None or tests.get("returncode") == 0) else 1,
                "status": "pass" if pushed and (tests is None or tests.get("returncode") == 0) else "fail",
            }
        ],
    }
    manifest_path = task_workspace / "mac-evidence.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sign_verdict(key: str, manifest: dict) -> str:
    """HMAC-SHA256 → base64url; matches mac.services.sign_verification_manifest."""
    import hmac as _hmac
    import hashlib as _hashlib
    import base64 as _base64
    # Match services._canonicalize_for_signature: stable JSON, exclude 'signature'
    filtered = {k: v for k, v in manifest.items() if k != "signature"}
    blob = json.dumps(filtered, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = _hmac.new(key.encode("ascii"), blob, _hashlib.sha256).digest()
    return "v1:" + _base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def run_deterministic_review_verdict(task_workspace: Path, task: dict, review_context: dict) -> None:
    """mac-jfns: deterministic post-LLM review verdict. After Hermes
    runs as the reviewer, override whatever it wrote with a signed
    review_verdict manifest that:
      - reads the executor's evidence from executor-evidence.json
      - runs the contract test gate in the review checkout
      - signs an approved verdict if tests pass and the executor's
        commit is reachable on origin
    """
    reviewer_agent_id = str(task.get("owner_agent_id") or os.environ.get("MAC_WORKER_AGENT_ID") or "").strip()
    attestation_key = (os.environ.get("MAC_ATTESTATION_KEY") or "").strip()
    if not reviewer_agent_id or not attestation_key:
        return
    executor_evidence_id = str(review_context.get("executor_evidence_id") or "").strip()
    review_id = str(review_context.get("review_id") or "").strip()
    if not executor_evidence_id or not review_id:
        return
    # Load the executor's evidence the dispatcher staged in the review workspace
    exec_ev_path = task_workspace / "executor-evidence.json"
    if not exec_ev_path.exists():
        return
    try:
        exec_ev = json.loads(exec_ev_path.read_text(encoding="utf-8"))
    except Exception:
        return
    exec_verification = (exec_ev.get("metadata") or {}).get("verification") or {}
    exec_repo = exec_verification.get("repo") or {}
    exec_head = str(exec_repo.get("head_sha") or "").strip()
    if not exec_head:
        return
    # Locate a review worktree: the dispatcher prepares one and exposes
    # it as MAC_TASK_REPO_WORKTREE; if absent we can still attest based
    # on the executor's claims.
    review_worktree = os.environ.get("MAC_TASK_REPO_WORKTREE", "").strip()
    tests = None
    independent_pass = False
    if review_worktree and Path(review_worktree).is_dir():
        # Independent test run against the executor commit
        ck = _git(["cat-file", "-e", "%s^{commit}" % exec_head], Path(review_worktree))
        if ck.returncode == 0:
            test_cmd = (((task.get("metadata") or {}).get("origin") or {})
                        .get("repository_contract") or {}).get("test", {}).get("command", "scripts/run-contract-tests.sh")
            tr = subprocess.run(
                ["bash", "-lc", test_cmd],
                cwd=review_worktree,
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
            )
            independent_pass = tr.returncode == 0
            tests = {
                "command": test_cmd,
                "returncode": int(tr.returncode),
                "status": "pass" if tr.returncode == 0 else "fail",
            }
    verdict = "approved" if independent_pass else "rejected"
    # Compute a worktree_digest the reviewer can attest to
    digest_input = ("%s|%s|%s" % (exec_head, exec_repo.get("remote_ref") or "", verdict)).encode("utf-8")
    import hashlib as _hashlib
    worktree_digest = "sha256:" + _hashlib.sha256(digest_input).hexdigest()
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": verdict,
        "review_id": review_id,
        "reviewed_evidence_id": executor_evidence_id,
        "worktree_digest": worktree_digest,
        "repo": {
            "head_sha": exec_head,
            "remote_ref": exec_repo.get("remote_ref") or "",
            "pushed": True,
            "dirty": False,
        },
        "checks": [
            {
                "name": "review_verdict_finalizer",
                "returncode": 0 if independent_pass else 1,
                "status": "pass" if independent_pass else "fail",
            }
        ],
        "tests": tests,
        "signed_by": reviewer_agent_id,
    }
    manifest["signature"] = _sign_verdict(attestation_key, manifest)
    out_path = task_workspace / "mac-evidence.json"
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_fallback_evidence_manifest(
    task_workspace: Path,
    task: dict,
    result: subprocess.CompletedProcess[str],
    review_context,
) -> None:
    if result.returncode != 0 or isinstance(review_context, dict):
        return
    manifest_path = task_workspace / "mac-evidence.json"
    if manifest_path.exists():
        return
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    # autonomy-loop fix: do NOT substitute a "completed without output" placeholder
    # — leave the deliverable empty so the substance gate can fail it honestly.
    result_text = stdout or stderr or ""
    summary = next((line.strip() for line in result_text.splitlines() if line.strip()), "")
    if len(summary) > 240:
        summary = summary[:237].rstrip() + "..."
    # autonomy-loop fix: the fallback must never fabricate *verified* completion.
    # The deterministic git finalizer already writes real repo_change evidence for
    # git:// tasks (and short-circuits this via manifest_path.exists()). So the
    # only thing left to record here is the agent's textual output as an
    # UNVERIFIED operator_result — NOT a fake repo_change/test/deployment, and
    # with NO synthetic passing "check" (the chat process exiting 0 is not proof
    # of the work). A proof-requiring task with no real evidence then fails
    # validation honestly instead of auto-publishing chatter (the "hello hello
    # hello" jam). Repo-coupled tasks are additionally rejected by the
    # operator_result repo-coupled gate (mem-11).
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "summary": summary,
        "result": result_text[-20000:],
        "task": {
            "id": task.get("id"),
            "title": task.get("title"),
            "project": task.get("project"),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    task_file = Path(os.environ["MAC_TASK_FILE"])
    task_workspace = Path(os.environ["MAC_TASK_WORKSPACE"])
    task_payload = json.loads(task_file.read_text(encoding="utf-8"))
    task = task_payload.get("task", task_payload)
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    review_context = metadata.get("review_context") if isinstance(metadata, dict) else None
    if isinstance(review_context, dict):
        prompt = "\n\n".join(
            [
                "You are running as a MAC fleet reviewer. Review the executor's work independently.",
                "Use the workspace files as the source of truth. Preserve secrets and do not print bearer tokens.",
                "Decide whether the executor evidence actually proves the task was completed and verified.",
                "Approve only when the evidence is coherent, pushed/published when required, and the checks are passing. Reject unverifiable, local-only, failing, or mismatched work.",
                "If MAC_TASK_REPO_WORKTREE is set, use that local review checkout for independent build/test work; it is prepared from the executor evidence remote/ref/head and is safe for review commands.",
                "For repository changes, build the review checkout and run the repository contract test command or the task's declared tests before approving. Look for failures introduced by the change, not just manifest shape.",
                "When you finish, report concise findings and write a review verdict manifest to $MAC_TASK_WORKSPACE/mac-evidence.json.",
                "Use schema mac.worker_evidence.v1 with status=complete, evidence_type=review_verdict, verdict=approved or rejected, reviewed_evidence_id=%s, and review_id=%s."
                % (
                    review_context.get("executor_evidence_id", ""),
                    review_context.get("review_id", ""),
                ),
                "A review verdict must also include repo copied from the executor verification repo object, with the same repo.head_sha, plus at least one independent passing check as checks=[{\"name\":\"...\",\"returncode\":0}] or status=\"pass\".",
                "Include worktree_digest as sha256:<64 lowercase hex chars>. If you cannot independently verify the executor result, write verdict=rejected and explain the blocker instead of omitting repo/check fields.",
                "Read the original task from executor-task.json and the executor evidence from executor-evidence.json in your workspace (%s)." % str(task_workspace),
            ]
        )
    else:
        prompt = "\n\n".join(
            [
                "You are running as a MAC fleet worker. Complete the assigned task from first principles.",
                "Use the task JSON as the source of truth. Preserve secrets and do not print bearer tokens.",
                "When you finish, report the exact outcome, files changed, tests run, and any blockers.",
                "Also write a verifiable evidence manifest to $MAC_TASK_WORKSPACE/mac-evidence.json.",
                "Use schema mac.worker_evidence.v1 with status=complete and evidence_type set to one of repo_change, documentation, investigation, deployment, test, artifact, no_change, or operator_result.",
                "For no-repository planning or operator directive work, use evidence_type=operator_result with summary and result fields describing the completed work.",
                "For repo/code work include repo.head_sha, repo.remote_ref or repo.pr_url, repo.pushed=true, repo.dirty=false, repo.files_changed, and passing tests/checks. Passing tests/checks should use returncode=0, status=pass, result=passed, or boolean/count fields that make success unambiguous. For deployments include targets/services plus passing checks. If you cannot produce this manifest, say why; MAC will not auto-publish unverifiable work.",
                "Repository runtime contract:\n%s" % repository_contract_section(task),
                "Read the full task from: %s" % str(task_file),
            ]
        )
    # ADR 0001 hu-04: run the vendored Hermes runtime from the mac venv in-process.
    hermes_py = Path.home() / ".mac" / "venv" / "bin" / "python"
    hermes_vendored = str(Path.home() / ".mac" / "src" / "mac" / "src" / "mac" / "_hermes")
    os.environ["PYTHONPATH"] = hermes_vendored + os.pathsep + os.environ.get("PYTHONPATH", "")
    audit_task_id = review_context.get("task_id") if isinstance(review_context, dict) else task.get("id")
    result = run_audited_command(
        [str(hermes_py), "-m", "hermes_cli.main", "chat", "--query", prompt, "--quiet", "--accept-hooks", "--yolo"],
        task_workspace,
        str(audit_task_id) if audit_task_id else None,
        {"execution_kind": "review" if isinstance(review_context, dict) else "task"},
    )
    # mac-jfns: deterministic post-LLM finalizer for repo tasks. Runs
    # only for non-review tasks targeting git://main, and overrides
    # whatever evidence the LLM emitted with a manifest derived from
    # actual git state.
    if not isinstance(review_context, dict):
        try:
            run_deterministic_git_finalizer(task_workspace, task)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("git finalizer failed: %s\n" % exc)
    else:
        try:
            run_deterministic_review_verdict(task_workspace, task, review_context)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("review verdict finalizer failed: %s\n" % exc)
    write_fallback_evidence_manifest(task_workspace, task, result, review_context)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
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

read_hub_tokenhub_api_key() {
  local target ssh_parts=() ssh_args=() ssh_target item last_index
  target="$(hub_target)"
  while IFS= read -r -d '' item; do ssh_parts+=("$item"); done < <(ssh_target_args "$target")
  last_index=$((${#ssh_parts[@]} - 1))
  ssh_target="${ssh_parts[$last_index]}"
  ssh_args=("${ssh_parts[@]:0:$last_index}")
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "${ssh_args[@]}" "$ssh_target" \
    'set -euo pipefail; if [ -f "$HOME/.tokenhub/env" ]; then set -a; . "$HOME/.tokenhub/env"; set +a; fi; if [ -z "${TOKENHUB_API_KEY:-}" ] && [ -f "$HOME/.tokenhub/.host-api-key" ]; then TOKENHUB_API_KEY="$(tr -d "\r\n" < "$HOME/.tokenhub/.host-api-key")"; fi; printf "%s" "${TOKENHUB_API_KEY:?}"'
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
  python3 -c "import base64, sys; print(base64.b64encode(open(sys.argv[1],'rb').read()).decode(), end='')" "$key_file"
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
  local spec agent hub_agent hub_token hub_token_key hub_target_str hub_tunnel_pubkey tokenhub_api_key tokenhub_api_key_name github_review_key_b64 local_target fleet_name_field network_provider_field hub_url_field direct_mesh_hub deployed_count
  hub_agent="$(fleet_hub_agent)"
  hub_target_str="$(fleet_hub_target)"
  hub_token="$(fleet_scoped_env MAC_DEPLOY_HUB_TOKEN "$hub_agent")"
  hub_token_key="$(fleet_scoped_name MAC_DEPLOY_HUB_TOKEN "$hub_agent")"
  hub_tunnel_pubkey="$(fleet_scoped_env MAC_DEPLOY_HUB_TUNNEL_PUBKEY "$hub_agent")"
  tokenhub_api_key="$(fleet_scoped_env MAC_DEPLOY_TOKENHUB_API_KEY "$hub_agent")"
  tokenhub_api_key_name="$(fleet_scoped_name MAC_DEPLOY_TOKENHUB_API_KEY "$hub_agent")"
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
    network_provider_field="${spec_fields[38]:-none}"
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
    if [ "$agent" != "$hub_agent" ] && [ -z "$tokenhub_api_key" ]; then
      tokenhub_api_key="$(read_hub_tokenhub_api_key)"
      upsert_local_env "$tokenhub_api_key_name" "$tokenhub_api_key"
    fi
    allow_degraded_services=0
    if [ "$agent" != "$hub_agent" ] && [ "$direct_mesh_hub" != "1" ]; then
      # Detect brand-new nodes: if the remote mac API is unreachable before deploy,
      # the hub tunnel key has not been authorized yet. Allow TokenHub to be
      # degraded for this first deploy; Qdrant and Firecrawl remain mandatory
      # and are checked after the tunnel is established below.
      if ! ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$local_target" \
        "curl -fsS --max-time 3 http://127.0.0.1:8789/health >/dev/null 2>&1" 2>/dev/null; then
        allow_degraded_services=1
        echo "==> ${agent}: first deploy (no existing mac API); TokenHub degraded override active"
      fi
      # Update the hub's reverse-tunnel conf and wait for it BEFORE deploying the
      # worker. The worker validates hub-managed TokenHub, Qdrant, and Firecrawl
      # through tunnel-forwarded ports during deploy; the tunnel must be
      # established first.
      install_reverse_tunnel_on_hub "$agent" "$local_target" "$hub_target_str" "$fleet_name_field"
      echo "==> ${agent}: waiting for hub reverse tunnel to establish before worker deploy"
      local attempt tunnel_ok=0
      for attempt in $(seq 1 6); do
        sleep 5
        if ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$local_target" \
          "curl -fsS --max-time 3 http://127.0.0.1:18789/health >/dev/null 2>&1" 2>/dev/null; then
          echo "==> ${agent}: hub tunnel reachable after $((attempt * 5))s"
          tunnel_ok=1
          break
        fi
      done
    fi
    deploy_host "$spec" "$hub_token" "$hub_tunnel_pubkey" "$tokenhub_api_key" "$allow_degraded_services" "$github_review_key_b64"
    deployed_count=$((deployed_count + 1))
    if [ "$agent" = "$hub_agent" ]; then
      hub_token="$(read_hub_token)"
      upsert_local_env "$hub_token_key" "$hub_token"
      tokenhub_api_key="$(read_hub_tokenhub_api_key)"
      upsert_local_env "$tokenhub_api_key_name" "$tokenhub_api_key"
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
        ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$local_target" \
          "sudo supervisorctl restart '$agent_prog' >/dev/null 2>&1 || sudo supervisorctl start '$agent_prog' >/dev/null 2>&1 || true" 2>/dev/null || true
        echo "==> ${agent}: restarted mac-agent with tunnel now available"
      elif [ "${allow_degraded_services:-0}" = "1" ]; then
        # First deploy: hub tunnel key now installed. Wait for the hub supervisord
        # tunnel process to reconnect (autorestart=true) then restart mac-agent.
        echo "==> ${agent}: first deploy complete; waiting for hub tunnel to auto-establish"
        local attempt first_tunnel_ok=0
        for attempt in $(seq 1 12); do
          sleep 5
          if ssh -n -o BatchMode=yes -o ConnectTimeout=5 "$local_target" \
            "curl -fsS --max-time 3 http://127.0.0.1:18789/health >/dev/null 2>&1" 2>/dev/null; then
            echo "==> ${agent}: hub tunnel reachable after $((attempt * 5))s"
            first_tunnel_ok=1
            break
          fi
        done
        local agent_prog="${fleet_name_field}-agent"
        if [ "$first_tunnel_ok" = "1" ]; then
          ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$local_target" \
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
