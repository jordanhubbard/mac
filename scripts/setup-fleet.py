#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except Exception:  # noqa: BLE001 - deploy will surface PyYAML requirement too.
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mac.fleet_deploy import (  # noqa: E402
    canonicalize_mesh_ssh_target,
    parse_ssh_target,
)
from mac.deploy_env import DEFAULT_WORKER_CAPABILITIES, build_router_provider_spec  # noqa: E402
from mac.fleet_setup import (  # noqa: E402
    DEFAULT_GATEWAY_MODEL,
    build_setup_plan,
    load_setup_spec,
    public_plan,
    resolve_gateway_model,
)
from mac.providers import ROUTER_PROVIDERS, router_secret_name  # noqa: E402


def prompt(
    label: str,
    *,
    default: str = "",
    required: bool = False,
    choices: List[str] | None = None,
) -> str:
    suffix = ""
    if choices:
        suffix += " [%s]" % "/".join(choices)
    if default:
        suffix += " [%s]" % default
    while True:
        value = input("%s%s: " % (label, suffix)).strip()
        if not value and default:
            value = default
        if not value and required:
            print("Required.")
            continue
        if choices and value and value not in choices:
            print("Choose one of: %s" % ", ".join(choices))
            continue
        return value


def prompt_bool(label: str, *, default: bool) -> bool:
    value = prompt(label, default="y" if default else "n", choices=["y", "n"])
    return value == "y"


def host_from_target(target: str) -> str:
    parsed = parse_ssh_target(target)
    host = parsed.user_host.rsplit("@", 1)[-1].strip()
    return host or "127.0.0.1"


def qdrant_url_from_hub(hub_url: str, qdrant_port: int = 6333) -> str:
    if hub_url.startswith("http://") or hub_url.startswith("https://"):
        scheme, rest = hub_url.split("://", 1)
        host = rest.split("/", 1)[0].rsplit(":", 1)[0]
        return "%s://%s:%d" % (scheme, host, qdrant_port)
    return ""


def webdav_url_from_dns(dns_name: str, public_path: str = "/artifacts/") -> str:
    host = dns_name.strip().rstrip(".")
    if not public_path.startswith("/"):
        public_path = "/" + public_path
    if not public_path.endswith("/"):
        public_path += "/"
    return "https://%s%s" % (host, public_path)


def yaml_scalar(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    return json.dumps(text)


def write_yaml_lines(value: Any, indent: int = 0) -> List[str]:
    prefix = " " * indent
    lines: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append("%s%s:" % (prefix, key))
                lines.extend(write_yaml_lines(item, indent + 2))
            else:
                lines.append("%s%s: %s" % (prefix, key, yaml_scalar(item)))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                lines.append("%s-" % prefix)
                lines.extend(write_yaml_lines(item, indent + 2))
            elif isinstance(item, list):
                lines.append("%s-" % prefix)
                lines.extend(write_yaml_lines(item, indent + 2))
            else:
                lines.append("%s- %s" % (prefix, yaml_scalar(item)))
    else:
        lines.append("%s%s" % (prefix, yaml_scalar(value)))
    return lines


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(content, encoding="utf-8")
    tmp.chmod(mode)
    tmp.replace(path)


def backup_existing(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name("%s.backup-%s" % (path.name, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())))
    shutil.copy2(path, backup)
    return backup


def load_fleet_registry(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "fleets": {}}
    if yaml is None:
        raise RuntimeError("PyYAML is required to update an existing fleet registry")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {"version": 1, "fleets": {}}
    if not isinstance(data, dict):
        raise RuntimeError("%s must contain a YAML mapping" % path)
    if isinstance(data.get("fleets"), dict):
        data.setdefault("version", 1)
        return data
    if isinstance(data.get("fleets"), list):
        fleets: Dict[str, Any] = {}
        for item in data["fleets"]:
            if not isinstance(item, dict) or not str(item.get("hub_agent") or "").strip():
                raise RuntimeError("each fleet in %s must have hub_agent" % path)
            fleets[str(item["hub_agent"]).strip()] = item
        data["fleets"] = fleets
        data.setdefault("version", 1)
        return data
    if data.get("hub_agent") and data.get("agents"):
        hub = str(data["hub_agent"]).strip()
        return {"version": 1, "fleets": {hub: data}}
    data.setdefault("version", 1)
    data["fleets"] = {}
    return data


def build_agent(
    *,
    name: str,
    target: str,
    os_kind: str,
    model: str,
    supervisor: str,
    mode: str,
    require_canary: bool,
    control_bind_host: str = "",
) -> Dict[str, Any]:
    agent: Dict[str, Any] = {
        "name": name,
        "enabled": True,
        "target": target,
        "os": os_kind,
        "supervisor": supervisor,
    }
    if control_bind_host:
        agent["control_bind_host"] = control_bind_host
    agent["hermes"] = {"gateway_model": resolve_gateway_model(model)}
    agent["worker"] = {"mode": mode, "require_canary": require_canary}
    return agent


def env_line(key: str, value: str) -> str:
    return "%s=%s" % (key, shlex.quote(value))


def write_generated_files(
    *,
    args: argparse.Namespace,
    fleets_config: Path,
    env_file: Path,
    hub_name: str,
    fleet_config: Dict[str, Any],
    env_values: Dict[str, str],
    next_steps: Optional[List[str]] = None,
    deploy_agents: Optional[List[str]] = None,
) -> int:
    registry = load_fleet_registry(fleets_config)
    fleets = registry.get("fleets")
    if not isinstance(fleets, dict):
        fleets = {}
        registry["fleets"] = fleets
    fleets[hub_name] = fleet_config
    registry["version"] = registry.get("version") or 1
    config_content = "\n".join(write_yaml_lines(registry)) + "\n"
    env_content = "\n".join(
        [
            "# Generated by scripts/setup-fleet.py.",
            "# Contains local deploy secrets; keep mode 0600.",
            *[env_line(key, value) for key, value in env_values.items()],
            "",
        ]
    )

    if args.dry_run:
        print("--- %s" % fleets_config)
        print(config_content, end="")
        print("--- %s" % env_file)
        print(env_content, end="")
        return 0

    fleets_backup = backup_existing(fleets_config)
    env_backup = backup_existing(env_file)
    atomic_write(fleets_config, config_content, 0o600)
    atomic_write(env_file, env_content, 0o600)
    if args.deploy_plan_file:
        plan = {
            "hub": hub_name,
            "agents": deploy_agents or [hub_name],
            "env_file": str(env_file),
            "fleets_config": str(fleets_config),
        }
        atomic_write(
            Path(args.deploy_plan_file).expanduser(),
            json.dumps(plan, indent=2) + "\n",
            0o600,
        )

    if fleets_backup:
        print("Backed up previous fleet registry to %s" % fleets_backup)
    if env_backup:
        print("Backed up previous env file to %s" % env_backup)
    print("Wrote %s" % fleets_config)
    print("Wrote %s" % env_file)
    print("")
    print("Next:")
    if next_steps:
        for step in next_steps:
            print("  %s" % step)
    else:
        print("  make deploy HUB=%s" % hub_name)
    return 0


SAMPLES_DIR = ROOT / "deploy" / "fleet" / "samples"


def _sample_description(path: Path) -> str:
    """First non-shebang header-comment line of a sample, as a short blurb."""
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("#"):
                break
            text = line.lstrip("#").strip()
            if text:
                return text
    except OSError:
        pass
    return ""


def _available_samples() -> List[Path]:
    if not SAMPLES_DIR.is_dir():
        return []
    return sorted(SAMPLES_DIR.glob("*.fleet.yaml"))


def list_samples() -> int:
    samples = _available_samples()
    if not samples:
        print("No fleet samples found under %s" % SAMPLES_DIR, file=sys.stderr)
        return 1
    print("Available fleet samples (deploy/fleet/samples):")
    width = max(len(p.name[: -len(".fleet.yaml")]) for p in samples)
    for path in samples:
        name = path.name[: -len(".fleet.yaml")]
        desc = _sample_description(path)
        if desc:
            print("  %-*s  %s" % (width, name, desc))
        else:
            print("  %s" % name)
    print("")
    print("Copy one with: scripts/setup-fleet.py --init-from <name> [--name <fleet>]")
    return 0


def init_from_sample(name: str, fleet: str, *, force: bool, specs_dir: Path) -> int:
    src = SAMPLES_DIR / ("%s.fleet.yaml" % name)
    if not src.is_file():
        available = ", ".join(p.name[: -len(".fleet.yaml")] for p in _available_samples()) or "(none)"
        print("No sample named %r under %s. Available: %s" % (name, SAMPLES_DIR, available), file=sys.stderr)
        return 2
    fleet_name = (fleet or name).strip()
    dest = specs_dir.expanduser() / ("%s.fleet.yaml" % fleet_name)
    if dest.exists() and not force:
        print("Refusing to overwrite %s without --force." % dest, file=sys.stderr)
        return 2
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    print("Copied %s -> %s" % (src, dest))
    print("")
    print("Next:")
    print("  1. Edit %s and fill in the <placeholders>." % dest)
    print("  2. Re-run with: scripts/setup-fleet.py --spec %s --force" % dest)
    return 0


def _default_worker_capabilities() -> List[str]:
    return DEFAULT_WORKER_CAPABILITIES.split(",")


# Known OpenAI-compatible upstreams the wizard can wire into the in-mac router,
# derived from the single source of truth (mac.providers). provider id ->
# (key env var, base-url env var, default base url).
_KNOWN_PROVIDERS: Dict[str, tuple] = {
    p.id: (p.key_env, p.base_env, p.default_base_url) for p in ROUTER_PROVIDERS
}


def _setup_hub(args: argparse.Namespace, fleets_config: Path, env_file: Path, running_locally: bool) -> int:
    fleet_name = prompt("Fleet name", default="my-fleet")
    hub_name = prompt("Hub node name", required=True)

    if running_locally:
        hub_target = "127.0.0.1"
        print("  Hub SSH target: 127.0.0.1 (running locally)")
    else:
        hub_target = prompt("Hub SSH target (user@host or host)", required=True)

    hub_os = prompt("Hub OS", default="linux", choices=["linux", "darwin"])
    control_port_str = prompt("Hub control plane port", default="8789")
    control_port = int(control_port_str) if control_port_str.isdigit() else 8789

    if running_locally:
        default_hub_url = "http://127.0.0.1:%d" % control_port
    else:
        default_hub_url = "http://%s:%d" % (host_from_target(hub_target), control_port)

    hub_url = prompt(
        "Hub URL agents should use to reach this hub",
        default=default_hub_url,
        required=True,
    )
    supervisor = prompt("Default supervisor", default="auto", choices=["auto", "systemd", "launchd", "supervisord"])
    home_channel = prompt("Slack home channel name without # (blank to skip)", default="")
    hub_model = prompt("Hub Hermes model selector", default=DEFAULT_GATEWAY_MODEL)
    hub_worker_mode = prompt("Hub worker mode", default="loop", choices=["heartbeat", "dry-run", "loop"])
    hub_require_canary = prompt_bool("Require canary metadata on hub tasks?", default=False)
    qdrant_required = True
    print("Shared Qdrant memory readiness is mandatory.")
    qdrant_port_str = prompt("Qdrant port", default="6333")
    qdrant_port = int(qdrant_port_str) if qdrant_port_str.isdigit() else 6333
    qdrant_url = prompt(
        "Shared Qdrant URL",
        default=qdrant_url_from_hub(hub_url, qdrant_port),
        required=True,
    )
    qdrant_bind_addr = prompt("Hub Qdrant bind address override (blank for Tailscale/loopback auto)", default="")
    qdrant_data_dir = prompt("Qdrant data directory override (blank for default /var/lib/<fleet>/qdrant)", default="")
    firecrawl_port = 3002
    firecrawl_url = qdrant_url_from_hub(hub_url, firecrawl_port)
    webdav_enabled = prompt_bool("Enable hub public artifact WebDAV server?", default=False)
    webdav_port = 80
    webdav_public_path = "/artifacts/"
    webdav_url = ""
    webdav_dns_name = ""
    webdav_bind_addr = "0.0.0.0"
    webdav_root = ""
    if webdav_enabled:
        webdav_dns_name = prompt("WebDAV public DNS name", default="", required=True).rstrip(".")
        webdav_port_str = prompt("WebDAV backend artifact port", default="80")
        webdav_port = int(webdav_port_str) if webdav_port_str.isdigit() else 80
        webdav_public_path = prompt("WebDAV public path prefix", default="/artifacts/")
        if not webdav_public_path.startswith("/"):
            webdav_public_path = "/" + webdav_public_path
        if not webdav_public_path.endswith("/"):
            webdav_public_path += "/"
        webdav_url = prompt(
            "WebDAV public HTTPS read URL",
            default=webdav_url_from_dns(webdav_dns_name, webdav_public_path),
            required=True,
        )
        webdav_bind_addr = prompt("WebDAV bind address", default="0.0.0.0")
        webdav_root = prompt(
            "Hub publish directory (blank for each node's ~/.mac/public-artifacts)",
            default="",
        )

    print("")
    print("Fleet mesh networking connects agents across networks without manual VPN config.")
    print("Tailscale is the default. Headscale is advanced and must be configured explicitly.")
    network_provider = prompt("Fleet network provider", default="tailscale", choices=["tailscale", "headscale", "none"])
    network_install = "auto"
    network_hostname_prefix = ""
    tailscale_auth_key = ""
    headscale_manage = False
    headscale_login_server = ""
    headscale_health_url = ""
    headscale_preauth_key_source = "env"
    headscale_preauth_key_env = "MAC_DEPLOY_HEADSCALE_PREAUTHKEY"
    headscale_preauth_key = ""
    headscale_port = "8080"
    headscale_public_addr = ""
    headscale_dns = "magicdns"
    headscale_ip_prefix = "100.64.0.0/10"

    if network_provider == "tailscale":
        tailscale_auth_key = prompt("Tailscale auth key (blank to skip automatic mesh join)", default="")
        if tailscale_auth_key:
            network_hostname_prefix = prompt(
                "Tailscale hostname prefix for fleet agents (blank for none)",
                default="",
            )
            ts_hub_name = "%s%s" % (network_hostname_prefix, hub_name)
            if prompt_bool(
                "Set hub URL to Tailscale MagicDNS name http://%s:%d?" % (ts_hub_name, control_port),
                default=True,
            ):
                hub_url = "http://%s:%d" % (ts_hub_name, control_port)
                firecrawl_url = "http://%s:%d" % (ts_hub_name, firecrawl_port)
                if prompt_bool(
                    "Set Qdrant URL to http://%s:%d?" % (ts_hub_name, qdrant_port),
                    default=True,
                ):
                    qdrant_url = "http://%s:%d" % (ts_hub_name, qdrant_port)
    elif network_provider == "headscale":
        print("Headscale requires a reachable login server and an enrollment key source.")
        headscale_mode = prompt("Headscale mode", default="external", choices=["external", "managed-hub"])
        headscale_manage = headscale_mode == "managed-hub"
        headscale_login_server = prompt("Headscale login server URL", required=True)
        headscale_health_url = prompt(
            "Headscale health check URL",
            default="%s/health" % headscale_login_server.rstrip("/"),
            required=True,
        )
        if headscale_manage:
            headscale_preauth_key_source = "hub-managed"
            headscale_port = prompt("Managed Headscale listen port", default="8080")
            headscale_public_addr = prompt(
                "Managed Headscale public address override (blank to use login server URL)",
                default="",
            )
        else:
            headscale_preauth_key_source = prompt(
                "Headscale preauth key source",
                default="env",
                choices=["env", "hub-managed"],
            )
            headscale_preauth_key_env = prompt(
                "Headscale preauth key env var",
                default="MAC_DEPLOY_HEADSCALE_PREAUTHKEY",
                required=True,
            )
            if headscale_preauth_key_source == "env":
                headscale_preauth_key = prompt(
                    "Headscale preauth key value for ~/.mac/.env (blank to provide at deploy time)",
                    default="",
                )
        headscale_dns = prompt("Headscale DNS assumption", default="magicdns", choices=["magicdns", "none"])
        headscale_ip_prefix = prompt("Headscale IP prefix (CGNAT range for fleet mesh)", default="100.64.0.0/10")
        network_hostname_prefix = prompt(
            "Tailscale hostname prefix for fleet agents (blank for none)",
            default="",
        )
        hs_host = "%s%s" % (network_hostname_prefix, hub_name)
        if headscale_dns == "magicdns" and prompt_bool(
            "Set hub URL to Headscale MagicDNS name http://%s.mac.internal:%d?" % (hs_host, control_port),
            default=False,
        ):
            hub_url = "http://%s.mac.internal:%d" % (hs_host, control_port)
            firecrawl_url = "http://%s.mac.internal:%d" % (hs_host, firecrawl_port)
            if prompt_bool(
                "Set Qdrant URL to http://%s.mac.internal:%d?" % (hs_host, qdrant_port),
                default=False,
            ):
                qdrant_url = "http://%s.mac.internal:%d" % (hs_host, qdrant_port)

    try:
        original_hub_target = hub_target
        hub_target = canonicalize_mesh_ssh_target(
            hub_target,
            provider=network_provider,
        )
    except ValueError as exc:
        print("Invalid hub SSH target: %s" % exc, file=sys.stderr)
        return 2
    if not running_locally and hub_url == default_hub_url and hub_target != original_hub_target:
        old_hub_url = hub_url
        hub_url = "http://%s:%d" % (host_from_target(hub_target), control_port)
        firecrawl_url = qdrant_url_from_hub(hub_url, firecrawl_port)
        if qdrant_url == qdrant_url_from_hub(old_hub_url, qdrant_port):
            qdrant_url = qdrant_url_from_hub(hub_url, qdrant_port)

    agents = [
        build_agent(
            name=hub_name,
            target=hub_target,
            os_kind=hub_os,
            model=hub_model,
            supervisor=supervisor,
            mode=hub_worker_mode,
            require_canary=hub_require_canary,
            control_bind_host="0.0.0.0",
        )
    ]
    while prompt_bool("Add another agent?", default=False):
        name = prompt("Agent name", required=True)
        target = prompt("Agent SSH target", required=True)
        try:
            target = canonicalize_mesh_ssh_target(target, provider=network_provider)
        except ValueError as exc:
            print("Invalid agent SSH target: %s" % exc, file=sys.stderr)
            return 2
        os_kind = prompt("Agent OS", default="linux", choices=["linux", "darwin"])
        model = prompt("Agent Hermes model selector", default=DEFAULT_GATEWAY_MODEL)
        agent_supervisor = prompt("Agent supervisor", default=supervisor, choices=["auto", "systemd", "launchd", "supervisord"])
        mode = prompt("Agent worker mode", default="loop", choices=["heartbeat", "dry-run", "loop"])
        require_canary = prompt_bool("Require canary metadata on this agent?", default=False)
        agents.append(
            build_agent(
                name=name,
                target=target,
                os_kind=os_kind,
                model=model,
                supervisor=agent_supervisor,
                mode=mode,
                require_canary=require_canary,
            )
        )

    fleet_config: Dict[str, Any] = {
        "sample": False,
        "fleet_name": fleet_name,
        "hub_agent": hub_name,
        "hub_url": hub_url,
        "control_port": control_port,
        "shared_services_manager_agent": hub_name,
        "defaults": {
            "supervisor": supervisor,
            "hermes": {
                "slack_home_channel_name": home_channel,
                "gateway_provider": "custom",
                "gateway_base_url": "",
            },
            "worker": {
                "mode": "heartbeat",
                "capabilities": _default_worker_capabilities(),
                "allowed_projects": "",
                "required_metadata": "",
                "require_canary": True,
            },
            "qdrant": {
                "install": "auto",
                "required": qdrant_required,
                "url": qdrant_url,
                "bind_addr": qdrant_bind_addr,
                "port": qdrant_port,
                "data_dir": qdrant_data_dir,
                "image": "docker.io/qdrant/qdrant:latest",
                "memory_limit": "2g",
            },
            "firecrawl": {
                "install": "auto",
                "required": True,
                "url": firecrawl_url,
                "bind_addr": "",
                "port": firecrawl_port,
            },
            "webdav": {
                "enabled": webdav_enabled,
                "install": "auto",
                "url": webdav_url,
                "dns_name": webdav_dns_name,
                "public_host": host_from_target(hub_target),
                "bind_addr": webdav_bind_addr,
                "port": webdav_port,
                "root": webdav_root,
                "public_path": webdav_public_path,
                "max_upload_bytes": 536870912,
            },
            "network": {
                "provider": network_provider,
                "install": network_install,
                "hostname_prefix": network_hostname_prefix,
                "tailscale": {
                    "auth_key_env": "MAC_DEPLOY_TAILSCALE_AUTH_KEY",
                },
                "headscale": {
                    "manage": headscale_manage,
                    "login_server": headscale_login_server,
                    "health_url": headscale_health_url,
                    "preauth_key_source": headscale_preauth_key_source,
                    "preauth_key_env": headscale_preauth_key_env,
                    "port": int(headscale_port) if str(headscale_port).isdigit() else 8080,
                    "public_addr": headscale_public_addr,
                    "dns": headscale_dns,
                    "ip_prefix": headscale_ip_prefix,
                },
            },
        },
        "agents": agents,
    }

    # Provider credentials — at least one required for the in-mac router to route requests.
    provider_env_values: Dict[str, str] = {}
    print("")
    print("The in-mac router requires at least one upstream LLM provider.")
    print("Keys are written to %s (mode 0600, never committed to git)." % env_file)
    print("Known providers: %s" % ", ".join(_KNOWN_PROVIDERS))
    print("")
    while True:
        pid = input("Provider to add (blank when done): ").strip().lower()
        if not pid:
            if not provider_env_values:
                print("At least one provider is required.")
                continue
            break
        if pid not in _KNOWN_PROVIDERS:
            print("Unknown provider '%s'. Known: %s" % (pid, ", ".join(_KNOWN_PROVIDERS)))
            continue
        key_var, base_var, default_base = _KNOWN_PROVIDERS[pid]
        api_key = ""
        while not api_key:
            api_key = input("  %s API key: " % pid).strip()
            if not api_key:
                print("  Required.")
        base_url = input("  %s base URL [%s]: " % (pid, default_base)).strip()
        provider_env_values[key_var] = api_key
        if base_url and base_url != default_base:
            provider_env_values[base_var] = base_url
        print("  Added %s." % pid)

    env_values = {
        "MAC_DEPLOY_FLEET_CONFIG": str(ROOT / "deploy" / "fleet" / "config.yaml"),
        "MAC_DEPLOY_FLEETS_CONFIG": str(fleets_config),
        "MAC_DEPLOY_HUB_AGENT": hub_name,
        "MAC_DEPLOY_SHARED_SERVICES_MANAGER_AGENT": hub_name,
    }
    env_values.update(provider_env_values)
    # Wire the in-mac router — the replacement for the retired TokenHub. Keys stay
    # centralized: only the HUB runs the router (MAC_ROUTER_BACKEND=inproc), and it
    # references each provider's upstream key as secret:<name>, which the deploy
    # escrows into the hub's encrypted vault. SPOKES route through the hub's /v1
    # and carry no upstream key (see deploy-mac-fleet.sh router block). Without this
    # a freshly-generated fleet would have no chat routing.
    # NOTE: these are the RUNTIME names the deploy reads via fleet_scoped_env
    # (deploy-mac-fleet.sh: `fleet_scoped_env MAC_ROUTER_BACKEND`), exactly like the
    # provider keys (NVIDIA_API_KEY, ...). They are NOT MAC_DEPLOY_*-prefixed —
    # deploy_host re-exports them as MAC_DEPLOY_ROUTER_* into the remote env itself.
    # Writing MAC_DEPLOY_ROUTER_* here would be inert (the deploy never reads it).
    env_values["MAC_ROUTER_BACKEND"] = "inproc"
    env_values["MAC_ROUTER_PROVIDERS"] = build_router_provider_spec(provider_env_values)
    print("")
    print("Wired in-mac router (hub-only; keys escrowed to the hub vault on deploy):")
    print("  MAC_ROUTER_BACKEND=inproc")
    print("  providers: %s" % (env_values["MAC_ROUTER_PROVIDERS"] or "(none)"))
    print("  (set MAC_ROUTER_DEFAULT_MODEL in %s if your gateway model is '*')" % env_file)
    # Optional modality reverse-proxies: image generation (+ speech ASR/TTS, video).
    # These use an endpoint + key DISTINCT from the chat/OpenAI key (hosted image/
    # speech/video NIMs have separate endpoints + keys). All optional — blank to
    # skip. The hub escrows the key as secret:nvidia-<m>; spokes route via the hub.
    # Interactive-only (a tty); automation should set router.{image,audio,video} in
    # the --spec so input alignment can't drift.
    if sys.stdin.isatty():
        print("")
        print("Optional generative modalities (separate URL + key from the chat key):")
        for modality, default_url in (
            ("image", "https://ai.api.nvidia.com/v1/genai"),
            ("audio", ""),
            ("video", ""),
        ):
            try:
                key = input("  %s-gen API key (build.nvidia.com nvapi-… ; blank to skip): " % modality).strip()
            except EOFError:
                break
            if not key:
                continue
            url = input("  %s-gen upstream URL [%s]: " % (modality, default_url or "(required)")).strip() or default_url
            if not url:
                print("  Skipped %s — no URL." % modality)
                continue
            env_values["NVIDIA_%s_API_KEY" % modality.upper()] = key
            env_values["MAC_DEPLOY_ROUTER_%s_UPSTREAM" % modality.upper()] = url
            print("  Wired %s-gen → %s (key escrowed as secret:nvidia-%s on the hub)." % (modality, url, modality))
    if prompt_bool("Generate MAC_SECRET_KEY in %s?" % env_file, default=True):
        env_values["MAC_SECRET_KEY"] = secrets.token_urlsafe(48)
    if prompt_bool("Generate MAC_API_TOKEN in %s?" % env_file, default=True):
        env_values["MAC_API_TOKEN"] = secrets.token_urlsafe(32)
    hub_token = prompt("Existing hub token for spoke deploys (blank to read it from hub during deploy)", default="")
    if hub_token:
        env_values["MAC_DEPLOY_HUB_TOKEN"] = hub_token
    if tailscale_auth_key:
        env_values["MAC_DEPLOY_TAILSCALE_AUTH_KEY"] = tailscale_auth_key
    if headscale_preauth_key:
        env_values[headscale_preauth_key_env] = headscale_preauth_key

    if running_locally:
        next_steps = [
            "make deploy HUB=%s" % hub_name,
        ]
    else:
        next_steps = [
            "make deploy HUB=%s" % hub_name,
        ]

    return write_generated_files(
        args=args,
        fleets_config=fleets_config,
        env_file=env_file,
        hub_name=hub_name,
        fleet_config=fleet_config,
        env_values=env_values,
        next_steps=next_steps,
        deploy_agents=[hub_name],
    )


def _setup_worker(args: argparse.Namespace, fleets_config: Path, env_file: Path, running_locally: bool) -> int:
    registry = load_fleet_registry(fleets_config)
    fleets = registry.get("fleets") or {}

    if fleets:
        print("Known fleets: %s" % ", ".join(sorted(fleets.keys())))

    hub_name = prompt("Hub agent name (which fleet to join)", required=True)

    existing_fleet = fleets.get(hub_name) if isinstance(fleets, dict) else None
    if existing_fleet:
        fleet_name = existing_fleet.get("fleet_name", "my-fleet")
        hub_url = existing_fleet.get("hub_url", "")
        control_port = existing_fleet.get("control_port", 8789)
        defaults = existing_fleet.get("defaults", {})
        supervisor = defaults.get("supervisor", "auto")
        print("  Found fleet '%s' (hub_url: %s)" % (fleet_name, hub_url))
    else:
        print("  Fleet '%s' not found in %s — enter hub details." % (hub_name, fleets_config))
        fleet_name = prompt("Fleet name", default="my-fleet")
        control_port_str = prompt("Hub control plane port", default="8789")
        control_port = int(control_port_str) if control_port_str.isdigit() else 8789
        hub_url = prompt("Hub URL (how to reach the control plane)", required=True)
        supervisor = prompt("Default supervisor", default="auto", choices=["auto", "systemd", "launchd", "supervisord"])
        defaults = {}

    print("")
    agent_name = prompt("Worker agent name", required=True)

    if running_locally:
        agent_target = "127.0.0.1"
        print("  Worker SSH target: 127.0.0.1 (running locally)")
    else:
        agent_target = prompt("Worker SSH target (user@host or host)", required=True)

    network_provider = str(
        ((defaults.get("network") or {}) if isinstance(defaults, dict) else {}).get("provider")
        or "none"
    )
    try:
        agent_target = canonicalize_mesh_ssh_target(
            agent_target,
            provider=network_provider,
        )
    except ValueError as exc:
        print("Invalid worker SSH target: %s" % exc, file=sys.stderr)
        return 2

    agent_os = prompt("Worker OS", default="linux", choices=["linux", "darwin"])
    agent_supervisor = prompt(
        "Worker supervisor",
        default=supervisor,
        choices=["auto", "systemd", "launchd", "supervisord"],
    )
    agent_model = prompt("Worker Hermes model selector", default=DEFAULT_GATEWAY_MODEL)
    agent_mode = prompt("Worker mode", default="loop", choices=["heartbeat", "dry-run", "loop"])
    require_canary = prompt_bool("Require canary metadata on this worker?", default=False)

    new_agent = build_agent(
        name=agent_name,
        target=agent_target,
        os_kind=agent_os,
        model=agent_model,
        supervisor=agent_supervisor,
        mode=agent_mode,
        require_canary=require_canary,
    )

    if existing_fleet:
        agents = list(existing_fleet.get("agents") or [])
        existing_names = [a.get("name") for a in agents if isinstance(a, dict)]
        if agent_name in existing_names:
            if not prompt_bool("Agent '%s' already exists in fleet. Overwrite?" % agent_name, default=False):
                print("Aborted.")
                return 2
            agents = [a for a in agents if not (isinstance(a, dict) and a.get("name") == agent_name)]
        agents.append(new_agent)
        fleet_config = dict(existing_fleet)
        fleet_config["agents"] = agents
    else:
        qdrant_port = int(defaults.get("qdrant", {}).get("port", 6333))
        firecrawl_port = int(defaults.get("firecrawl", {}).get("port", 3002))
        fleet_config = {
            "sample": False,
            "fleet_name": fleet_name,
            "hub_agent": hub_name,
            "hub_url": hub_url,
            "control_port": control_port,
            "shared_services_manager_agent": hub_name,
            "defaults": {
                "supervisor": supervisor,
                "hermes": {"slack_home_channel_name": "", "gateway_provider": "custom", "gateway_base_url": ""},
                "worker": {
                    "mode": "heartbeat",
                    "capabilities": _default_worker_capabilities(),
                    "allowed_projects": "",
                    "required_metadata": "",
                    "require_canary": True,
                },
                "qdrant": {
                    "install": "none",
                    "required": True,
                    "url": qdrant_url_from_hub(hub_url, qdrant_port),
                    "bind_addr": "",
                    "port": qdrant_port,
                    "data_dir": "",
                    "image": "docker.io/qdrant/qdrant:latest",
                    "memory_limit": "2g",
                },
                "firecrawl": {
                    "install": "none",
                    "required": True,
                    "url": qdrant_url_from_hub(hub_url, firecrawl_port),
                    "bind_addr": "",
                    "port": firecrawl_port,
                },
            },
            "agents": [new_agent],
        }

    env_values: Dict[str, str] = {
        "MAC_DEPLOY_FLEET_CONFIG": str(ROOT / "deploy" / "fleet" / "config.yaml"),
        "MAC_DEPLOY_FLEETS_CONFIG": str(fleets_config),
        "MAC_DEPLOY_HUB_AGENT": hub_name,
        "MAC_DEPLOY_SHARED_SERVICES_MANAGER_AGENT": hub_name,
    }
    hub_token = prompt(
        "Hub API token (MAC_API_TOKEN from the hub's ~/.mac/.env, blank to read at deploy time)",
        default="",
    )
    if hub_token:
        env_values["MAC_DEPLOY_HUB_TOKEN"] = hub_token

    if running_locally:
        next_steps = [
            'make deploy HUB=%s ARGS="%s"' % (hub_name, agent_name),
        ]
    else:
        next_steps = [
            'make deploy HUB=%s ARGS="%s"' % (hub_name, agent_name),
        ]

    return write_generated_files(
        args=args,
        fleets_config=fleets_config,
        env_file=env_file,
        hub_name=hub_name,
        fleet_config=fleet_config,
        env_values=env_values,
        next_steps=next_steps,
        deploy_agents=[agent_name],
    )


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Interactive first-run mac fleet setup wizard.")
    parser.add_argument(
        "--fleets-config",
        default=str(Path.home() / ".mac" / "fleets.yaml"),
        help="Path to the home-scoped multi-fleet registry.",
    )
    parser.add_argument(
        "--env-file",
        default=str(Path.home() / ".mac" / ".env"),
        help="Path to write caller-machine deploy env/secrets.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing files after backing them up.")
    parser.add_argument("--dry-run", action="store_true", help="Print generated files without writing them.")
    parser.add_argument("--deploy-plan-file", default="", help="Write a setup deployment plan JSON file.")
    parser.add_argument("--spec", help="Declarative mac.fleet_setup.v1 YAML/JSON spec for non-interactive setup.")
    parser.add_argument(
        "--list-samples",
        action="store_true",
        help="List the available per-CSP fleet samples (deploy/fleet/samples) and exit.",
    )
    parser.add_argument(
        "--init-from",
        metavar="SAMPLE",
        help="Copy deploy/fleet/samples/<SAMPLE>.fleet.yaml to ~/.mac/specs/<fleet>.fleet.yaml, then exit.",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Fleet/spec name for --init-from (defaults to the sample name).",
    )
    parser.add_argument(
        "--specs-dir",
        default=str(Path.home() / ".mac" / "specs"),
        help="Directory --init-from copies samples into.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate --spec and print the redacted setup plan.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run setup doctor checks for --spec and print the report.",
    )
    parser.add_argument("--new-hub", help="Create a one-node first-hub fleet non-interactively.")
    parser.add_argument("--target", help="Hub SSH target for --new-hub, optionally user@host:port.")
    parser.add_argument("--ssh-port", type=int, help="SSH port for --new-hub target.")
    parser.add_argument("--fleet-name", default="my-fleet", help="Fleet name for --new-hub.")
    parser.add_argument("--hub-os", default="linux", choices=["linux", "darwin"], help="Hub OS for --new-hub.")
    parser.add_argument("--control-port", type=int, default=8789, help="Hub control plane port for --new-hub.")
    parser.add_argument("--hub-url", default="", help="Hub URL agents should use for --new-hub.")
    parser.add_argument("--home-channel", default="", help="Slack home channel for --new-hub.")
    parser.add_argument("--hub-model", default="", help="Hub Hermes model selector for --new-hub.")
    parser.add_argument(
        "--supervisor",
        default="auto",
        choices=["auto", "systemd", "launchd", "supervisord"],
        help="Default supervisor for --new-hub.",
    )
    parser.add_argument(
        "--network-provider",
        default="tailscale",
        choices=["tailscale", "headscale", "none"],
        help="Fleet mesh provider for --new-hub.",
    )
    parser.add_argument("--headscale-login-server", default="", help="Headscale login server for --new-hub.")
    parser.add_argument("--headscale-preauth-key", default="", help="Headscale preauth key to place in env file.")
    parser.add_argument("--webdav", action="store_true", help="Enable hub public artifact WebDAV server for --new-hub.")
    parser.add_argument("--webdav-port", type=int, default=80, help="WebDAV backend artifact port for --new-hub.")
    parser.add_argument("--webdav-dns-name", default="", help="Public DNS name for WebDAV/HTTPS artifacts for --new-hub.")
    parser.add_argument("--webdav-url", default="", help="Public WebDAV read URL for --new-hub.")
    parser.add_argument("--webdav-bind-addr", default="0.0.0.0", help="WebDAV bind address for --new-hub.")
    parser.add_argument("--webdav-root", default="", help="Hub/shared artifact publish directory for --new-hub.")
    parser.add_argument("--webdav-public-path", default="/artifacts/", help="WebDAV public path prefix for --new-hub.")
    args = parser.parse_args(argv)

    if args.list_samples:
        return list_samples()
    if args.init_from:
        return init_from_sample(
            args.init_from,
            args.name,
            force=args.force,
            specs_dir=Path(args.specs_dir),
        )

    fleets_config = Path(args.fleets_config).expanduser()
    env_file = Path(args.env_file).expanduser()

    if args.spec:
        try:
            spec = load_setup_spec(Path(args.spec).expanduser())
            plan = build_setup_plan(
                spec,
                root=ROOT,
                fleets_config=fleets_config,
                env_file=env_file,
            )
        except Exception as exc:  # noqa: BLE001
            print("setup spec failed to load: %s" % exc, file=sys.stderr)
            return 2
        redacted = public_plan(plan)
        if args.validate_only or args.doctor:
            print(json.dumps(redacted, indent=2, sort_keys=True))
            return 0 if plan.get("status") != "fail" else 1
        if plan.get("status") == "fail":
            print(json.dumps(redacted, indent=2, sort_keys=True), file=sys.stderr)
            print("setup spec is not deployable; fix failed checks before writing config", file=sys.stderr)
            return 2
        if (
            not args.force
            and not args.dry_run
            and any(path.exists() for path in (fleets_config, env_file))
        ):
            print("Refusing to overwrite existing setup files without --force.", file=sys.stderr)
            return 2
        return write_generated_files(
            args=args,
            fleets_config=fleets_config,
            env_file=env_file,
            hub_name=str(plan["hub"]),
            fleet_config=plan["fleet_config"],
            env_values=plan["env_values"],
            next_steps=plan["next_steps"],
            deploy_agents=plan["deploy_agents"],
        )

    noninteractive = bool(args.new_hub)
    if (
        not args.force
        and noninteractive
        and not args.dry_run
        and any(path.exists() for path in (fleets_config, env_file))
    ):
        print("Refusing to overwrite existing setup files without --force.", file=sys.stderr)
        return 2
    if not args.force and not noninteractive:
        for path in (fleets_config, env_file):
            if path.exists() and not prompt_bool("Overwrite %s after making a backup?" % path, default=False):
                print("Aborted before writing %s" % path)
                return 2

    if args.new_hub:
        if not args.target:
            print("--new-hub requires --target", file=sys.stderr)
            return 2
        hub_name = args.new_hub.strip()
        if not hub_name:
            print("--new-hub requires a non-empty hub name", file=sys.stderr)
            return 2
        try:
            hub_target = canonicalize_mesh_ssh_target(
                args.target,
                provider=args.network_provider,
                port=args.ssh_port,
            )
        except ValueError as exc:
            print("Invalid hub SSH target: %s" % exc, file=sys.stderr)
            return 2
        host = host_from_target(hub_target)
        hub_url = args.hub_url.strip() or "http://%s:%d" % (host, args.control_port)
        qdrant_port = 6333
        firecrawl_port = 3002
        webdav_public_path = args.webdav_public_path.strip() or "/artifacts/"
        if not webdav_public_path.startswith("/"):
            webdav_public_path = "/" + webdav_public_path
        if not webdav_public_path.endswith("/"):
            webdav_public_path += "/"
        webdav_dns_name = args.webdav_dns_name.strip().rstrip(".")
        if args.webdav and not webdav_dns_name:
            print("--webdav requires --webdav-dns-name", file=sys.stderr)
            return 2
        webdav_url = args.webdav_url.strip() or (
            webdav_url_from_dns(webdav_dns_name, webdav_public_path)
            if args.webdav
            else ""
        )
        headscale_login_server = args.headscale_login_server.strip()
        if args.network_provider == "headscale" and not headscale_login_server:
            print("--network-provider headscale requires --headscale-login-server", file=sys.stderr)
            return 2
        headscale_preauth_key_env = "MAC_DEPLOY_HEADSCALE_PREAUTHKEY"
        fleet_config = {
            "sample": False,
            "fleet_name": args.fleet_name,
            "hub_agent": hub_name,
            "hub_url": hub_url,
            "control_port": args.control_port,
            "shared_services_manager_agent": hub_name,
            "defaults": {
                "supervisor": args.supervisor,
                "hermes": {
                    "slack_home_channel_name": args.home_channel.strip().lstrip("#"),
                    "gateway_provider": "custom",
                    "gateway_base_url": "",
                },
                "worker": {
                    "mode": "heartbeat",
                    "capabilities": _default_worker_capabilities(),
                    "allowed_projects": "",
                    "required_metadata": "",
                    "require_canary": True,
                },
                "qdrant": {
                    "install": "auto",
                    "required": True,
                    "url": qdrant_url_from_hub(hub_url, qdrant_port),
                    "bind_addr": "",
                    "port": qdrant_port,
                    "data_dir": "",
                    "image": "docker.io/qdrant/qdrant:latest",
                    "memory_limit": "2g",
                },
                "firecrawl": {
                    "install": "auto",
                    "required": True,
                    "url": qdrant_url_from_hub(hub_url, firecrawl_port),
                    "bind_addr": "",
                    "port": firecrawl_port,
                },
                "webdav": {
                    "enabled": bool(args.webdav),
                    "install": "auto",
                    "url": webdav_url,
                    "dns_name": webdav_dns_name,
                    "public_host": host,
                    "bind_addr": args.webdav_bind_addr,
                    "port": args.webdav_port,
                    "root": args.webdav_root,
                    "public_path": webdav_public_path,
                    "max_upload_bytes": 536870912,
                },
                "network": {
                    "provider": args.network_provider,
                    "install": "auto",
                    "hostname_prefix": "",
                    "tailscale": {"auth_key_env": "MAC_DEPLOY_TAILSCALE_AUTH_KEY"},
                    "headscale": {
                        "manage": False,
                        "login_server": headscale_login_server,
                        "health_url": "%s/health" % headscale_login_server.rstrip("/") if headscale_login_server else "",
                        "preauth_key_source": "env",
                        "preauth_key_env": headscale_preauth_key_env,
                        "port": 8080,
                        "public_addr": "",
                        "dns": "magicdns",
                        "ip_prefix": "100.64.0.0/10",
                    },
                },
            },
            "agents": [
                build_agent(
                    name=hub_name,
                    target=hub_target,
                    os_kind=args.hub_os,
                    model=args.hub_model,
                    supervisor=args.supervisor,
                    mode="loop",
                    require_canary=False,
                    control_bind_host="0.0.0.0",
                )
            ],
        }
        env_values = {
            "MAC_DEPLOY_FLEET_CONFIG": str(ROOT / "deploy" / "fleet" / "config.yaml"),
            "MAC_DEPLOY_FLEETS_CONFIG": str(fleets_config),
            "MAC_DEPLOY_HUB_AGENT": hub_name,
            "MAC_DEPLOY_SHARED_SERVICES_MANAGER_AGENT": hub_name,
            "MAC_SECRET_KEY": secrets.token_urlsafe(48),
            "MAC_API_TOKEN": secrets.token_urlsafe(32),
            # Make the in-mac router the routing backend (mounts as a no-op until
            # providers are added). --new-hub collects no provider keys, so set
            # MAC_ROUTER_PROVIDERS + the provider key(s) in the env file before
            # deploy or chat will have no upstream. Runtime name (fleet_scoped_env),
            # NOT MAC_DEPLOY_*-prefixed — see _setup_hub.
            "MAC_ROUTER_BACKEND": "inproc",
        }
        if args.headscale_preauth_key:
            env_values[headscale_preauth_key_env] = args.headscale_preauth_key
        return write_generated_files(
            args=args,
            fleets_config=fleets_config,
            env_file=env_file,
            hub_name=hub_name,
            fleet_config=fleet_config,
            env_values=env_values,
            deploy_agents=[hub_name],
        )

    print("mac fleet setup wizard")
    print("Do not put provider API keys in the fleet config YAML. The wizard collects")
    print("them into ~/.mac/.env (mode 0600) and wires the in-mac router to read them.")
    print("")

    running_locally = prompt_bool(
        "Are you running this script on the machine being configured (hub or worker)?",
        default=False,
    )
    print("")
    print("  hub    = the control plane node (first node in a new fleet)")
    print("  worker = an additional agent that joins an existing fleet")
    role = prompt("Setting up a hub or a worker?", required=True, choices=["hub", "worker"])
    print("")

    if role == "worker":
        return _setup_worker(args, fleets_config, env_file, running_locally)
    else:
        return _setup_hub(args, fleets_config, env_file, running_locally)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
