"""Declarative fleet setup planning and validation.

The interactive setup wizard is useful for humans, but LLM agents need a
stable file contract they can fill, validate, and hand to deploy without
surviving a long prompt sequence. This module converts a compact
``mac.fleet_setup.v1`` spec into the existing ``~/.mac/fleets.yaml`` fleet
registry shape plus the caller-side deploy env values.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from mac.fleet_deploy import normalize_ssh_target
from mac.providers import ROUTER_PROVIDERS, router_secret_name

SETUP_SPEC_SCHEMA = "mac.fleet_setup.v1"
DEFAULT_CONTROL_PORT = 8789
DEFAULT_QDRANT_PORT = 6333
DEFAULT_FIRECRAWL_PORT = 3002


def default_worker_capabilities() -> List[str]:
    return [
        "ops",
        "python",
        "hermes",
        "review",
        "web_search",
        "web_extract",
        "web_crawl",
        "firecrawl",
    ]


def load_setup_spec(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        loaded = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("PyYAML is required for YAML setup specs") from exc
        loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError("fleet setup spec must be a mapping")
    return loaded


def build_setup_plan(
    spec: Mapping[str, Any],
    *,
    root: Path,
    fleets_config: Path,
    env_file: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Build a machine-readable setup plan from a declarative spec.

    The returned object intentionally contains no side effects. Callers decide
    whether to write files, dry-run, validate only, or deploy.
    """
    env_map = dict(os.environ if env is None else env)
    errors: List[str] = []
    warnings: List[str] = []
    schema = str(spec.get("schema") or "").strip()
    if schema != SETUP_SPEC_SCHEMA:
        errors.append("schema must be %s" % SETUP_SPEC_SCHEMA)

    fleet_block = _mapping(spec.get("fleet"))
    hub_block = _mapping(spec.get("hub"))
    defaults_block = _mapping(spec.get("defaults"))

    hub_name = _str(
        spec.get("hub_agent")
        or spec.get("hub")
        or fleet_block.get("hub_agent")
        or fleet_block.get("hub")
        or hub_block.get("name")
    )
    fleet_name = _str(spec.get("fleet_name") or fleet_block.get("name")) or hub_name
    if not hub_name:
        errors.append("hub agent name is required at hub.name or fleet.hub")

    control_port = _int(
        spec.get("control_port")
        or fleet_block.get("control_port")
        or hub_block.get("control_port"),
        DEFAULT_CONTROL_PORT,
    )
    hub_url = _str(spec.get("hub_url") or fleet_block.get("hub_url") or hub_block.get("url"))
    if not hub_url and hub_name:
        hub_target = _str(hub_block.get("target"))
        host = _host_from_target(hub_target) if hub_target else hub_name
        hub_url = "http://%s:%d" % (host, control_port)

    supervisor = _str(
        spec.get("supervisor")
        or defaults_block.get("supervisor")
        or fleet_block.get("supervisor")
    ) or "auto"
    if supervisor not in {"auto", "systemd", "launchd", "supervisord"}:
        errors.append("supervisor must be one of auto/systemd/launchd/supervisord")

    network = _network_config(spec, defaults_block, errors)
    qdrant = _qdrant_config(spec, defaults_block, hub_url)
    firecrawl = _firecrawl_config(spec, defaults_block, hub_url)
    hermes_defaults = _mapping(defaults_block.get("hermes"))
    worker_defaults = _mapping(defaults_block.get("worker"))

    agents = _agent_configs(
        spec,
        hub_name=hub_name,
        supervisor=supervisor,
        errors=errors,
        hermes_defaults=hermes_defaults,
        worker_defaults=worker_defaults,
    )
    if hub_name and not any(agent.get("name") == hub_name for agent in agents):
        errors.append("agents must include the hub agent %r" % hub_name)

    router, router_env, required_env, router_warnings = _router_env(spec, env_map)
    warnings.extend(router_warnings)
    if not router.get("providers"):
        errors.append("router.providers must include at least one upstream provider")

    env_values: Dict[str, str] = {
        "MAC_DEPLOY_FLEET_CONFIG": str(root / "deploy" / "fleet" / "config.yaml"),
        "MAC_DEPLOY_FLEETS_CONFIG": str(fleets_config),
        "MAC_DEPLOY_HUB_AGENT": hub_name,
        "MAC_DEPLOY_SHARED_SERVICES_MANAGER_AGENT": hub_name,
        "MAC_ROUTER_BACKEND": str(router.get("backend") or "inproc"),
        "MAC_ROUTER_PROVIDERS": str(router.get("providers") or ""),
    }
    env_values.update(router_env)

    secrets_block = _mapping(spec.get("secrets"))
    generate = _mapping(secrets_block.get("generate"))
    if generate.get("mac_secret_key", True) is not False:
        env_values["MAC_SECRET_KEY"] = _str(secrets_block.get("mac_secret_key")) or secrets.token_urlsafe(48)
    if generate.get("mac_api_token", True) is not False:
        env_values["MAC_API_TOKEN"] = _str(secrets_block.get("mac_api_token")) or secrets.token_urlsafe(32)
    hub_token = _str(secrets_block.get("hub_token") or spec.get("hub_token"))
    if hub_token:
        env_values["MAC_DEPLOY_HUB_TOKEN"] = hub_token

    if network["provider"] == "tailscale":
        auth_key_env = str(network["tailscale"].get("auth_key_env") or "MAC_DEPLOY_TAILSCALE_AUTH_KEY")
        auth_key = _str(secrets_block.get(auth_key_env) or secrets_block.get("tailscale_auth_key"))
        if auth_key:
            env_values[auth_key_env] = auth_key
        elif auth_key_env not in required_env and spec.get("require_mesh_auth") is True:
            required_env.append(auth_key_env)
    elif network["provider"] == "headscale":
        key_env = str(network["headscale"].get("preauth_key_env") or "MAC_DEPLOY_HEADSCALE_PREAUTHKEY")
        key_value = _str(secrets_block.get(key_env) or secrets_block.get("headscale_preauth_key"))
        if key_value:
            env_values[key_env] = key_value

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
                "slack_home_channel_name": _str(
                    hermes_defaults.get("slack_home_channel_name")
                    or hermes_defaults.get("home_channel")
                ),
                "gateway_provider": _str(hermes_defaults.get("gateway_provider")) or "custom",
                "gateway_base_url": _str(hermes_defaults.get("gateway_base_url")),
            },
            "worker": {
                "mode": _str(worker_defaults.get("mode")) or "heartbeat",
                "capabilities": _list(worker_defaults.get("capabilities")) or default_worker_capabilities(),
                "allowed_projects": _str(worker_defaults.get("allowed_projects")),
                "required_metadata": _str(worker_defaults.get("required_metadata")),
                "require_canary": worker_defaults.get("require_canary", True),
            },
            "qdrant": qdrant,
            "firecrawl": firecrawl,
            "network": network,
        },
        "agents": agents,
    }

    deploy_agents = [
        _str(agent)
        for agent in _list(spec.get("deploy_agents"))
        if _str(agent)
    ] or [hub_name]
    deploy_command = "make deploy HUB=%s" % hub_name
    if deploy_agents:
        deploy_command += ' ARGS="%s"' % " ".join(deploy_agents)
    next_steps = [
        deploy_command,
        "mac --fleet %s fleet snapshot" % hub_name,
        "mac --fleet %s memory health" % hub_name,
    ]

    checks = doctor_checks(
        {
            "errors": errors,
            "warnings": warnings,
            "required_env": required_env,
            "env_values": env_values,
            "fleet_config": fleet_config,
        },
        env=env_map,
    )
    return {
        "schema": "mac.fleet_setup_plan.v1",
        "hub": hub_name,
        "fleet_name": fleet_name,
        "fleet_config": fleet_config,
        "env_values": env_values,
        "deploy_agents": deploy_agents,
        "required_env": required_env,
        "warnings": warnings,
        "errors": errors,
        "checks": checks,
        "status": _status(errors, checks),
        "next_steps": next_steps,
    }


def doctor_checks(plan: Mapping[str, Any], *, env: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    env_map = dict(os.environ if env is None else env)
    checks: List[Dict[str, Any]] = []
    errors = list(plan.get("errors") or [])
    _check(checks, "spec.valid", "fail" if errors else "pass", "; ".join(errors) if errors else "spec parsed")

    fleet = _mapping(plan.get("fleet_config"))
    _check(checks, "fleet.not_sample", "pass" if fleet.get("sample") is False else "fail", "sample must be false")
    _check(checks, "hub.url", "pass" if _str(fleet.get("hub_url")) else "fail", _str(fleet.get("hub_url")) or "missing")

    agents = list(fleet.get("agents") or [])
    hub = _str(fleet.get("hub_agent"))
    hub_present = any(isinstance(agent, dict) and agent.get("name") == hub for agent in agents)
    _check(checks, "hub.agent_present", "pass" if hub and hub_present else "fail", hub or "missing")
    missing_targets = [
        str(agent.get("name") or "?")
        for agent in agents
        if isinstance(agent, dict) and not _str(agent.get("target"))
    ]
    _check(
        checks,
        "agents.targets",
        "fail" if missing_targets else "pass",
        "missing targets: %s" % ", ".join(missing_targets) if missing_targets else "%d target(s)" % len(agents),
    )

    defaults = _mapping(fleet.get("defaults"))
    network = _mapping(defaults.get("network"))
    provider = _str(network.get("provider")) or "none"
    _check(checks, "network.provider", "pass" if provider in {"tailscale", "headscale", "none"} else "fail", provider)
    qdrant = _mapping(defaults.get("qdrant"))
    _check(checks, "qdrant.url", "pass" if _str(qdrant.get("url")) else "warn", _str(qdrant.get("url")) or "missing")
    firecrawl = _mapping(defaults.get("firecrawl"))
    _check(
        checks,
        "firecrawl.url",
        "pass" if _str(firecrawl.get("url")) else "warn",
        _str(firecrawl.get("url")) or "missing",
    )

    env_values = _mapping(plan.get("env_values"))
    router_providers = _str(env_values.get("MAC_ROUTER_PROVIDERS"))
    _check(checks, "router.providers", "pass" if router_providers else "fail", router_providers or "missing")
    required = list(plan.get("required_env") or [])
    missing = [
        name
        for name in required
        if not _str(env_values.get(str(name))) and not _str(env_map.get(str(name)))
    ]
    _check(
        checks,
        "env.required",
        "fail" if missing else "pass",
        "missing: %s" % ", ".join(missing) if missing else "all required env available",
    )
    return checks


def public_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Redact secret values before printing a plan/report."""
    out = dict(plan)
    env_values = dict(out.get("env_values") or {})
    for key in list(env_values):
        if _looks_secret(key):
            env_values[key] = "<set>"
    out["env_values"] = env_values
    return out


def _agent_configs(
    spec: Mapping[str, Any],
    *,
    hub_name: str,
    supervisor: str,
    errors: List[str],
    hermes_defaults: Mapping[str, Any],
    worker_defaults: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    raw_agents = list(spec.get("agents") or [])
    hub_block = _mapping(spec.get("hub"))
    if hub_block and not any(_mapping(agent).get("name") == hub_name for agent in raw_agents):
        raw_agents.insert(0, {"role": "hub", **hub_block, "name": hub_name})
    if not raw_agents:
        errors.append("agents list is required")
        return []
    agents: List[Dict[str, Any]] = []
    seen = set()
    for raw in raw_agents:
        item = _mapping(raw)
        name = _str(item.get("name"))
        if not name:
            errors.append("every agent needs a name")
            continue
        if name in seen:
            errors.append("duplicate agent name: %s" % name)
            continue
        seen.add(name)
        target = _str(item.get("target"))
        if not target:
            errors.append("agent %s requires target" % name)
        else:
            try:
                target = normalize_ssh_target(target, port=_optional_int(item.get("ssh_port")))
            except ValueError as exc:
                errors.append("agent %s target invalid: %s" % (name, exc))
        os_kind = _str(item.get("os") or item.get("os_kind")) or "linux"
        if os_kind not in {"linux", "darwin"}:
            errors.append("agent %s os must be linux or darwin" % name)
        agent_supervisor = _str(item.get("supervisor")) or supervisor
        if agent_supervisor not in {"auto", "systemd", "launchd", "supervisord"}:
            errors.append("agent %s supervisor invalid" % name)
        hermes = _mapping(item.get("hermes"))
        worker = _mapping(item.get("worker"))
        config: Dict[str, Any] = {
            "name": name,
            "enabled": item.get("enabled", True) is not False,
            "target": target,
            "os": os_kind,
            "supervisor": agent_supervisor,
            "hermes": {
                "gateway_model": _str(
                    item.get("model")
                    or hermes.get("gateway_model")
                    or hermes_defaults.get("gateway_model")
                ),
            },
            "worker": {
                "mode": _str(worker.get("mode") or worker_defaults.get("mode"))
                or ("loop" if name == hub_name else "heartbeat"),
                "require_canary": worker.get(
                    "require_canary",
                    False if name == hub_name else worker_defaults.get("require_canary", False),
                ),
            },
        }
        if name == hub_name:
            config["control_bind_host"] = _str(item.get("control_bind_host")) or "0.0.0.0"
        agents.append(config)
    return agents


def _router_env(
    spec: Mapping[str, Any],
    env: Mapping[str, str],
) -> Tuple[Dict[str, str], Dict[str, str], List[str], List[str]]:
    router = _mapping(spec.get("router"))
    providers = list(router.get("providers") or [])
    if not providers:
        providers = list(spec.get("providers") or [])
    registry = {provider.id: provider for provider in ROUTER_PROVIDERS}
    provider_env_values: Dict[str, str] = {}
    required_env: List[str] = []
    warnings: List[str] = []
    router_specs: List[str] = []
    for index, raw in enumerate(providers):
        item = _mapping(raw)
        provider_id = _str(item.get("id") or item.get("provider"))
        if provider_id not in registry:
            warnings.append("unknown router provider skipped: %s" % (provider_id or "<missing>"))
            continue
        provider = registry[provider_id]
        key_env = _str(item.get("key_env")) or provider.key_env
        base_env = _str(item.get("base_env")) or provider.base_env
        base_url = _str(item.get("base_url")) or provider.default_base_url
        priority = _optional_int(item.get("priority"))
        priority = index if priority is None else priority
        secret = _str(item.get("secret") or item.get("secret_name")) or router_secret_name(provider_id)
        router_specs.append("%s=%s,%d,key=secret:%s" % (provider_id, base_url, priority, secret))
        if _str(item.get("key")):
            provider_env_values[key_env] = _str(item["key"])
        elif _str(env.get(key_env)):
            provider_env_values[key_env] = _str(env[key_env])
        else:
            required_env.append(key_env)
        if base_url != provider.default_base_url:
            provider_env_values[base_env] = base_url
    if router_specs:
        provider_env_values["MAC_ROUTER_PROVIDERS"] = ";".join(router_specs)
    # Upstream keys are often scoped to a fixed model set, so the bare wildcard
    # `*` a runtime emits must be substituted to a concrete allowed model. Carry
    # the ladder + default from the spec; without it a fresh fleet's chat dies on
    # "key can only access models=[...]. Tried to access *".
    wildcard_models = _str(router.get("wildcard_models"))
    if wildcard_models:
        provider_env_values["MAC_ROUTER_WILDCARD_MODELS"] = wildcard_models
    default_model = _str(router.get("default_model"))
    if default_model:
        provider_env_values["MAC_ROUTER_DEFAULT_MODEL"] = default_model
    # Modality reverse-proxies (image/audio/video): each takes an optional upstream
    # URL + a key DISTINCT from the chat/OpenAI key (hosted image/speech/video NIMs
    # use separate endpoints + keys). The url -> MAC_DEPLOY_ROUTER_<M>_UPSTREAM; the
    # key -> NVIDIA_<M>_API_KEY (inline, else read from the environment). The hub
    # escrows it as secret:nvidia-<m>; spokes route through the hub.
    for modality in ("image", "audio", "video"):
        block = _mapping(router.get(modality))
        if not block:
            continue
        url = _str(block.get("url") or block.get("upstream"))
        if url:
            provider_env_values["MAC_DEPLOY_ROUTER_%s_UPSTREAM" % modality.upper()] = url
        key_env = _str(block.get("key_env")) or "NVIDIA_%s_API_KEY" % modality.upper()
        if _str(block.get("key")):
            provider_env_values[key_env] = _str(block["key"])
        elif _str(env.get(key_env)):
            provider_env_values[key_env] = _str(env[key_env])
    backend = _str(router.get("backend")) or "inproc"
    return {"backend": backend, "providers": ";".join(router_specs)}, provider_env_values, required_env, warnings


def _network_config(spec: Mapping[str, Any], defaults: Mapping[str, Any], errors: List[str]) -> Dict[str, Any]:
    network = {**_mapping(defaults.get("network")), **_mapping(spec.get("network"))}
    provider = _str(network.get("provider")) or "tailscale"
    if provider not in {"tailscale", "headscale", "none"}:
        errors.append("network.provider must be tailscale, headscale, or none")
    headscale = _mapping(network.get("headscale"))
    if provider == "headscale" and not _str(headscale.get("login_server")):
        errors.append("network.provider=headscale requires headscale.login_server")
    return {
        "provider": provider,
        "install": _str(network.get("install")) or "auto",
        "hostname_prefix": _str(network.get("hostname_prefix")),
        "tailscale": {
            "auth_key_env": _str(_mapping(network.get("tailscale")).get("auth_key_env"))
            or "MAC_DEPLOY_TAILSCALE_AUTH_KEY",
        },
        "headscale": {
            "manage": headscale.get("manage", False) is True,
            "login_server": _str(headscale.get("login_server")),
            "health_url": _str(headscale.get("health_url"))
            or (
                "%s/health" % _str(headscale.get("login_server")).rstrip("/")
                if _str(headscale.get("login_server"))
                else ""
            ),
            "preauth_key_source": _str(headscale.get("preauth_key_source")) or "env",
            "preauth_key_env": _str(headscale.get("preauth_key_env")) or "MAC_DEPLOY_HEADSCALE_PREAUTHKEY",
            "port": _int(headscale.get("port"), 8080),
            "public_addr": _str(headscale.get("public_addr")),
            "dns": _str(headscale.get("dns")) or "magicdns",
            "ip_prefix": _str(headscale.get("ip_prefix")) or "100.64.0.0/10",
        },
    }


def _qdrant_config(spec: Mapping[str, Any], defaults: Mapping[str, Any], hub_url: str) -> Dict[str, Any]:
    qdrant = {**_mapping(defaults.get("qdrant")), **_mapping(spec.get("qdrant"))}
    port = _int(qdrant.get("port"), DEFAULT_QDRANT_PORT)
    return {
        "install": _str(qdrant.get("install")) or "auto",
        "required": qdrant.get("required", True) is not False,
        "url": _str(qdrant.get("url")) or _service_url_from_hub(hub_url, port),
        "bind_addr": _str(qdrant.get("bind_addr")),
        "port": port,
        "data_dir": _str(qdrant.get("data_dir")),
        "image": _str(qdrant.get("image")) or "docker.io/qdrant/qdrant:latest",
        "memory_limit": _str(qdrant.get("memory_limit")) or "2g",
    }


def _firecrawl_config(spec: Mapping[str, Any], defaults: Mapping[str, Any], hub_url: str) -> Dict[str, Any]:
    firecrawl = {**_mapping(defaults.get("firecrawl")), **_mapping(spec.get("firecrawl"))}
    port = _int(firecrawl.get("port"), DEFAULT_FIRECRAWL_PORT)
    return {
        "install": _str(firecrawl.get("install")) or "auto",
        "required": firecrawl.get("required", True) is not False,
        "url": _str(firecrawl.get("url")) or _service_url_from_hub(hub_url, port),
        "bind_addr": _str(firecrawl.get("bind_addr")),
        "port": port,
    }


def _service_url_from_hub(hub_url: str, port: int) -> str:
    if not hub_url or "://" not in hub_url:
        return ""
    scheme, rest = hub_url.split("://", 1)
    host = rest.split("/", 1)[0].rsplit(":", 1)[0]
    return "%s://%s:%d" % (scheme, host, port)


def _host_from_target(target: str) -> str:
    if not target:
        return ""
    parsed = normalize_ssh_target(target)
    host = parsed.rsplit("@", 1)[-1]
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host


def _check(checks: List[Dict[str, Any]], name: str, status: str, detail: str) -> None:
    checks.append({"name": name, "status": status, "detail": detail})


def _status(errors: List[str], checks: List[Mapping[str, Any]]) -> str:
    if errors or any(check.get("status") == "fail" for check in checks):
        return "fail"
    if any(check.get("status") == "warn" for check in checks):
        return "warn"
    return "pass"


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _looks_secret(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
