"""Deployment environment modeling.

This module is intentionally dependency-free. ``deploy/deploy-mac-fleet.sh``
imports it immediately after unpacking the source tree, before the mac package
or deploy venv exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import argparse
import os
import re
import secrets
import shlex
import sys
import urllib.parse
from typing import Dict, Mapping, MutableMapping, Optional, Sequence

from mac.providers import ROUTER_PROVIDERS, router_secret_name, upstream_provider_env_vars


DEFAULT_WORKER_CAPABILITIES = (
    "ops,python,hermes,review,web_search,web_extract,web_crawl,firecrawl"
)


PROVIDERS = tuple(ROUTER_PROVIDERS)


ROUTER_KEYS = (
    "MAC_ROUTER_BACKEND",
    "MAC_ROUTER_PROVIDERS",
    "MAC_ROUTER_DEFAULT_MODEL",
    "MAC_ROUTER_WILDCARD_MODELS",
    "MAC_ROUTER_IMAGE_UPSTREAM",
    "MAC_ROUTER_IMAGE_KEY",
    "MAC_ROUTER_IMAGE_TIMEOUT",
    "MAC_ROUTER_AUDIO_UPSTREAM",
    "MAC_ROUTER_AUDIO_KEY",
    "MAC_ROUTER_VIDEO_UPSTREAM",
    "MAC_ROUTER_VIDEO_KEY",
)

GATEWAY_ROUTING_KEYS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "CUSTOM_BASE_URL",
    "MAC_HERMES_GATEWAY_BASE_URL",
    "MAC_HERMES_GATEWAY_API_KEY",
    "ACC_HERMES_GATEWAY_BASE_URL",
    "ACC_HERMES_GATEWAY_API_KEY",
)

UPSTREAM_PROVIDER_KEYS = tuple(upstream_provider_env_vars())

INPROC_MANAGED_KEYS = tuple(dict.fromkeys([*ROUTER_KEYS, *GATEWAY_ROUTING_KEYS, *UPSTREAM_PROVIDER_KEYS]))


def build_router_provider_spec(provider_env_values: Mapping[str, str]) -> str:
    """Build ``MAC_ROUTER_PROVIDERS`` from provider keys collected by setup."""
    specs = []
    for priority, provider in enumerate(PROVIDERS):
        if provider.key_env not in provider_env_values:
            continue
        base_url = provider_env_values.get(provider.base_env) or provider.default_base_url
        specs.append(
            "%s=%s,%d,key=secret:%s"
            % (provider.id, base_url, priority, router_secret_name(provider.id))
        )
    return ";".join(specs)


@dataclass(frozen=True)
class DeployPaths:
    env_file: Path
    mac_home: Path
    home: Path


@dataclass(frozen=True)
class ControlConfig:
    port: str
    hub_url: str
    hub_token: str
    bind_host: str
    supervisor_kind: str
    network_provider: str


@dataclass(frozen=True)
class GatewayConfig:
    home_channel: str
    model: str
    provider: str
    base_url: str


@dataclass(frozen=True)
class WorkerConfig:
    mode: str
    capabilities: str
    allowed_projects: str
    required_metadata: str
    require_canary: str


@dataclass(frozen=True)
class SharedServicesConfig:
    qdrant_url: str
    qdrant_port: str
    firecrawl_url: str
    firecrawl_port: str
    webdav_enabled: str = ""
    webdav_url: str = ""
    webdav_port: str = "80"
    webdav_root: str = ""
    webdav_public_path: str = "/artifacts/"


@dataclass(frozen=True)
class DeployIdentity:
    agent: str
    shared_services_manager: str
    fleet_name: str

    @property
    def is_hub(self) -> bool:
        return self.agent == self.shared_services_manager


@dataclass(frozen=True)
class DeployEnvConfig:
    paths: DeployPaths
    control: ControlConfig
    gateway: GatewayConfig
    worker: WorkerConfig
    services: SharedServicesConfig
    identity: DeployIdentity
    # No passthrough shims: callers reach through the groups directly
    # (cfg.paths.env_file, cfg.control.port, cfg.identity.is_hub).


def _raw_env_assignment(line: str) -> Optional[tuple[str, str]]:
    """Plain ``KEY=VALUE`` split for quote-free lines (no shell interpretation)."""
    if line.startswith("export "):
        line = line[len("export "):]
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    return key.strip(), value.strip()


def _parse_env_assignment(line: str) -> Optional[tuple[str, str]]:
    """Parse one ``KEY=VALUE`` (optionally ``export``-prefixed) env line.

    Well-formed lines go through ``shlex`` so quoted/escaped values round-trip
    exactly with ``render_env``'s ``shlex.quote``. Documented edge semantics:

    - **Malformed shell quoting** (e.g. an unbalanced quote): the line is treated
      as corrupt and skipped (``None``) rather than silently storing a
      half-parsed value — *unless* it is quote-free, in which case the plain
      ``KEY=VALUE`` split is unambiguous and is used.
    - **Trailing unquoted tokens** (``KEY=val extra``): the leading assignment
      wins (``val``); trailing tokens are ignored. ``render_env`` never emits
      this (unsafe values are quoted), so it only arises from hand-edited files.
    """
    try:
        tokens = shlex.split(line, comments=False, posix=True)
    except ValueError:
        if '"' in line or "'" in line:
            return None
        return _raw_env_assignment(line)
    if tokens:
        if tokens[0] == "export":
            tokens = tokens[1:]
        if tokens and "=" in tokens[0]:
            key, value = tokens[0].split("=", 1)
            return key.strip(), value
    return _raw_env_assignment(line)


def parse_env_text(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _parse_env_assignment(line)
        if parsed is None:
            continue
        key, value = parsed
        values[key] = value
    return values


def read_env_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    return parse_env_text(path.read_text(encoding="utf-8"))


_ENV_SAFE = re.compile(r"^[A-Za-z0-9_./:@=,+%-]*$")


def render_env(values: Mapping[str, str]) -> str:
    lines = [
        "# Generated by mac deploy/deploy-mac-fleet.sh.",
        "# Contains bearer tokens; keep mode 0600.",
    ]
    for key in sorted(values):
        rendered = str(values[key])
        if _ENV_SAFE.match(rendered):
            lines.append("%s=%s" % (key, rendered))
        else:
            lines.append("%s=%s" % (key, shlex.quote(rendered)))
    return "\n".join(lines) + "\n"


def write_env_file(path: Path, values: Mapping[str, str]) -> None:
    path.write_text(render_env(values), encoding="utf-8")
    path.chmod(0o600)


def stable_id(prefix: str, value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.lower()).strip("_")
    return "%s_%s" % (prefix, safe or "default")


def _clear(values: MutableMapping[str, str], keys: Sequence[str]) -> None:
    for key in keys:
        values.pop(key, None)


def _service_url(
    *,
    configured_url: str,
    hub_url: str,
    configured_port: str,
    native_port: str,
) -> str:
    if configured_url:
        return configured_url.rstrip("/")
    parsed = urllib.parse.urlsplit(hub_url or "http://127.0.0.1:8789")
    if not parsed.scheme or not parsed.hostname:
        return ""
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    service_port = int(configured_port)
    hub_port = parsed.port or int(native_port or "8789")
    if hub_port == int(native_port or "8789") + 10000:
        service_port += 10000
    return urllib.parse.urlunsplit((parsed.scheme, "%s:%s" % (host, service_port), "", "", ""))


def _mac_hub_url(cfg: DeployEnvConfig) -> str:
    if cfg.worker.mode == "loop" and cfg.identity.is_hub:
        return "http://127.0.0.1:%s" % cfg.control.port
    if cfg.control.network_provider in {"tailscale", "headscale"} and cfg.control.hub_url:
        return cfg.control.hub_url.rstrip("/")
    return "http://127.0.0.1:18789"


def _ensure_secret_values(values: MutableMapping[str, str]) -> None:
    values.setdefault("MAC_SECRET_KEY", secrets.token_urlsafe(48))
    values.setdefault("MAC_API_TOKEN", secrets.token_urlsafe(32))


def _path_values(cfg: DeployEnvConfig) -> Dict[str, str]:
    paths = cfg.paths
    hub_url = _mac_hub_url(cfg)
    return {
        "MAC_DB": str(paths.mac_home / "mac.db"),
        "MAC_PORT": cfg.control.port,
        "MAC_BIND_HOST": cfg.control.bind_host,
        "MAC_HUB_URL": hub_url,
        "MAC_URL": hub_url,
        "MAC_SUPERVISOR_KIND": cfg.control.supervisor_kind,
        "HERMES_HOME": str(paths.home / ".hermes"),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_REDACT_SECRETS": "true",
        "ACC_DIR": str(paths.home / ".acc"),
        "MAC_HERMES_AGENT_DIR": str(paths.mac_home / "src" / "mac" / "src" / "mac" / "_hermes"),
        "MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM": "1",
        "MAC_HERMES_APPLY_GATEWAY_RUNTIME_SHIM": "1",
        "MAC_HERMES_STARTUP_CHECK": "1",
        "MAC_HERMES_RUNTIME_CONTEXT_FILE": str(paths.home / ".hermes" / "mac-runtime-context.json"),
        "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN": str(paths.home / ".hermes" / "mac-runtime-context.md"),
        "MAC_HERMES_RUNTIME_CONTEXT_REQUIRED": "1",
        "MAC_HERMES_WORKSPACE": str(paths.mac_home / "src" / "mac"),
        "MAC_PROJECT_CONTRACT_FILE": str(paths.mac_home / "src" / "mac" / ".mac" / "project.yaml"),
        "MAC_SELF_UPDATE_REPO": str(paths.mac_home / "src" / "mac"),
        "MAC_MEMORY_TOPOLOGY_FILE": str(paths.home / ".hermes" / "mac-memory-topology.json"),
    }


def _identity_values(cfg: DeployEnvConfig) -> Dict[str, str]:
    identity = cfg.identity
    hermes_id = stable_id("hermes", identity.agent)
    return {
        "MAC_FLEET_NAME": identity.fleet_name,
        "MAC_FLEET_TENANT_ID": stable_id("tenant", identity.fleet_name),
        "MAC_AGENT_ID": stable_id("agent", identity.agent),
        "MAC_HERMES_PERSONA_ID": stable_id("persona", identity.agent),
        "MAC_HERMES_INSTANCE_ID": hermes_id,
        "MAC_WORKER_HERMES_INSTANCE_ID": hermes_id,
        "MAC_SHARED_SERVICES_MANAGER_AGENT": identity.shared_services_manager,
    }


def _gateway_values(cfg: DeployEnvConfig) -> Dict[str, str]:
    gateway = cfg.gateway
    values: Dict[str, str] = {}
    if gateway.model:
        values.update(
            {
                "MAC_HERMES_GATEWAY_MODEL": gateway.model,
                "ACC_HERMES_GATEWAY_MODEL": gateway.model,
                "HERMES_INFERENCE_MODEL": gateway.model,
                "ACC_LLM_MODEL": gateway.model,
            }
        )
    if gateway.provider:
        values.update(
            {
                "MAC_HERMES_GATEWAY_PROVIDER": gateway.provider,
                "ACC_HERMES_GATEWAY_PROVIDER": gateway.provider,
                "HERMES_INFERENCE_PROVIDER": gateway.provider,
            }
        )
    if gateway.base_url:
        values.update(
            {
                "MAC_HERMES_GATEWAY_BASE_URL": gateway.base_url,
                "ACC_HERMES_GATEWAY_BASE_URL": gateway.base_url,
                "CUSTOM_BASE_URL": gateway.base_url,
                "OPENAI_BASE_URL": gateway.base_url,
            }
        )
    return values


def _worker_token(cfg: DeployEnvConfig, values: Mapping[str, str]) -> str:
    if cfg.worker.mode == "loop" and cfg.identity.is_hub:
        return values["MAC_API_TOKEN"]
    if cfg.control.hub_token:
        return cfg.control.hub_token
    return values.get("MAC_WORKER_TOKEN") or values["MAC_API_TOKEN"]


def _worker_values(cfg: DeployEnvConfig, values: Mapping[str, str]) -> Dict[str, str]:
    worker = cfg.worker
    identity = cfg.identity
    return {
        "MAC_WORKER_TOKEN": _worker_token(cfg, values),
        "MAC_WORKER_AGENT_NAME": identity.agent,
        "MAC_WORKER_HOSTNAME": identity.agent,
        "MAC_WORKER_MODE": worker.mode,
        "MAC_WORKER_CAPABILITIES": worker.capabilities,
        "MAC_WORKER_REQUIRE_CANARY": worker.require_canary,
        "MAC_WORKER_ALLOWED_PROJECTS": worker.allowed_projects,
        "MAC_WORKER_REQUIRED_METADATA": worker.required_metadata,
    }


def _shared_service_requirement_values() -> Dict[str, str]:
    return {
        "MAC_REQUIRE_QDRANT_MEMORY": "1",
        "MAC_QDRANT_MEMORY_ROLE": "shared_level2",
        "MAC_REQUIRE_FIRECRAWL": "1",
    }


def _shared_service_url_values(values: Mapping[str, str], cfg: DeployEnvConfig) -> Dict[str, str]:
    hub_url = values.get("MAC_HUB_URL") or cfg.control.hub_url or "http://127.0.0.1:8789"
    services = cfg.services
    out: Dict[str, str] = {}
    qdrant_url = _service_url(
        configured_url=services.qdrant_url,
        hub_url=hub_url,
        configured_port=services.qdrant_port or "6333",
        native_port=cfg.control.port or "8789",
    )
    if qdrant_url:
        out.update(
            {
                "QDRANT_URL": qdrant_url,
                "QDRANT_ADDRESS": qdrant_url,
                "QDRANT_FLEET_URL": qdrant_url,
            }
        )
    firecrawl_url = _service_url(
        configured_url=services.firecrawl_url,
        hub_url=hub_url,
        configured_port=services.firecrawl_port or "3002",
        native_port=cfg.control.port or "8789",
    )
    if firecrawl_url:
        out.update(
            {
                "FIRECRAWL_API_URL": firecrawl_url,
                "FIRECRAWL_GATEWAY_URL": firecrawl_url,
                "MAC_WEB_SEARCH_PROVIDER": "firecrawl",
                "MAC_WEB_SEARCH_URL": firecrawl_url,
                "HERMES_WEB_SEARCH_BACKEND": "firecrawl",
                "HERMES_WEB_EXTRACT_BACKEND": "firecrawl",
            }
        )
        if "FIRECRAWL_API_KEY" not in values:
            out["FIRECRAWL_API_KEY"] = "none"
    return out


_WEBDAV_MANAGED_KEYS = (
    "MAC_PUBLISH_DIR",
    "MAC_PUBLISH_METHOD",
    "MAC_PUBLISH_PUBLIC_URL",
    "MAC_PUBLISH_WEBDAV_ENABLED",
    "MAC_PUBLISH_WEBDAV_URL",
    "MAC_WEBDAV_PUBLIC_URL",
    "MAC_WEBDAV_PUBLIC_PATH",
    "MAC_WEBDAV_ROOT",
    "MAC_WEBDAV_WRITE_TOKEN",
    "MAC_WEBDAV_MAX_UPLOAD_BYTES",
)


def _enabled(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_public_path(path: str) -> str:
    out = (path or "/artifacts/").strip()
    if not out.startswith("/"):
        out = "/" + out
    if not out.endswith("/"):
        out += "/"
    return out


def _webdav_values(values: MutableMapping[str, str], cfg: DeployEnvConfig, env: Mapping[str, str]) -> Dict[str, str]:
    _clear(values, _WEBDAV_MANAGED_KEYS)
    services = cfg.services
    enabled = _enabled(services.webdav_enabled)
    deploy_enabled = (env.get("MAC_DEPLOY_WEBDAV_ENABLED") or "").strip()
    if deploy_enabled:
        enabled = _enabled(deploy_enabled)
    if not enabled:
        return {"MAC_PUBLISH_WEBDAV_ENABLED": "0"}
    public_path = _normalize_public_path(services.webdav_public_path)
    public_url = (
        services.webdav_url
        or (env.get("MAC_DEPLOY_WEBDAV_URL") or "").strip()
        or (env.get("MAC_DEPLOY_WEBDAV_PUBLIC_URL") or "").strip()
    ).rstrip("/")
    root = services.webdav_root or str(cfg.paths.mac_home / "public-artifacts")
    out = {
        "MAC_PUBLISH_DIR": root,
        "MAC_PUBLISH_METHOD": "hub_directory_http",
        "MAC_PUBLISH_WEBDAV_ENABLED": "1",
        "MAC_WEBDAV_PUBLIC_PATH": public_path,
        "MAC_WEBDAV_ROOT": root,
        "MAC_WEBDAV_MAX_UPLOAD_BYTES": (env.get("MAC_DEPLOY_WEBDAV_MAX_UPLOAD_BYTES") or "536870912").strip(),
    }
    if public_url:
        out["MAC_PUBLISH_PUBLIC_URL"] = public_url
        out["MAC_PUBLISH_WEBDAV_URL"] = public_url
        out["MAC_WEBDAV_PUBLIC_URL"] = public_url
    return out


def _deploy_router_config(env: Mapping[str, str]) -> Dict[str, str]:
    return {
        "backend": (env.get("MAC_DEPLOY_ROUTER_BACKEND") or "").strip(),
        "providers": (env.get("MAC_DEPLOY_ROUTER_PROVIDERS") or "").strip(),
        "default_model": (env.get("MAC_DEPLOY_ROUTER_DEFAULT_MODEL") or "").strip(),
        "wildcard_models": (env.get("MAC_DEPLOY_ROUTER_WILDCARD_MODELS") or "").strip(),
    }


def _apply_inproc_router_hub(
    values: MutableMapping[str, str], cfg: DeployEnvConfig, env: Mapping[str, str]
) -> None:
    """Hub: run the router locally and point the hub's own gateway at its local
    /v1, so the hub's upstream keys (held only here) serve every agent."""
    router = _deploy_router_config(env)
    values["MAC_ROUTER_BACKEND"] = router["backend"]
    if router["default_model"]:
        values["MAC_ROUTER_DEFAULT_MODEL"] = router["default_model"]
    if router["wildcard_models"]:
        values["MAC_ROUTER_WILDCARD_MODELS"] = router["wildcard_models"]
    values["MAC_ROUTER_PROVIDERS"] = router["providers"]
    local_router_v1 = "http://127.0.0.1:%s/v1" % cfg.control.port
    values["OPENAI_BASE_URL"] = local_router_v1
    values["CUSTOM_BASE_URL"] = local_router_v1
    values["MAC_HERMES_GATEWAY_BASE_URL"] = local_router_v1
    values["ACC_HERMES_GATEWAY_BASE_URL"] = local_router_v1
    local_token = values["MAC_API_TOKEN"]
    values["OPENAI_API_KEY"] = local_token
    values["MAC_HERMES_GATEWAY_API_KEY"] = local_token
    values["ACC_HERMES_GATEWAY_API_KEY"] = local_token
    # Modality reverse-proxies (image/audio/video): set the upstream + vault key
    # ref when a key for that modality is available. The image key is DISTINCT
    # from the chat key — prefer NVIDIA_IMAGE_API_KEY (set at cluster init), fall
    # back to NVIDIA_API_KEY for back-compat. Audio/video have no default upstream
    # (hosted paths vary) — wired only when configured at init via
    # MAC_DEPLOY_ROUTER_<M>_UPSTREAM. The key VALUE is escrowed separately
    # (deploy escrow → secret:nvidia-<m>).
    _modalities = (
        ("image", "https://ai.api.nvidia.com/v1/genai",
         (env.get("NVIDIA_IMAGE_API_KEY") or env.get("NVIDIA_API_KEY") or "").strip()),
        ("audio", "", (env.get("NVIDIA_AUDIO_API_KEY") or "").strip()),
        ("video", "", (env.get("NVIDIA_VIDEO_API_KEY") or "").strip()),
    )
    for _m, _default_upstream, _key_present in _modalities:
        _up = (env.get("MAC_DEPLOY_ROUTER_%s_UPSTREAM" % _m.upper()) or _default_upstream).strip()
        if _up and _key_present:
            values["MAC_ROUTER_%s_UPSTREAM" % _m.upper()] = _up
            values["MAC_ROUTER_%s_KEY" % _m.upper()] = "secret:nvidia-%s" % _m


def _apply_inproc_router_spoke(values: MutableMapping[str, str], cfg: DeployEnvConfig) -> None:
    """Spoke: route the gateway (chat + image) through the hub's /v1 with the
    validated hub-facing token and hold no upstream keys."""
    hub_base = (values.get("MAC_HUB_URL") or cfg.control.hub_url or "").rstrip("/")
    local_token = values.get("MAC_API_TOKEN") or ""
    hub_token = (cfg.control.hub_token or values.get("MAC_WORKER_TOKEN") or "").strip()
    hub_v1 = "%s/v1" % hub_base
    values["OPENAI_BASE_URL"] = hub_v1
    values["CUSTOM_BASE_URL"] = hub_v1
    values["MAC_HERMES_GATEWAY_BASE_URL"] = hub_v1
    values["ACC_HERMES_GATEWAY_BASE_URL"] = hub_v1
    values["OPENAI_API_KEY"] = hub_token
    values["MAC_HERMES_GATEWAY_API_KEY"] = hub_token
    values["ACC_HERMES_GATEWAY_API_KEY"] = hub_token
    values["NVIDIA_API_KEY"] = hub_token
    values["NVIDIA_IMAGE_BASE_URL"] = "%s/v1/genai" % hub_base


def _apply_inproc_router(
    values: MutableMapping[str, str], cfg: DeployEnvConfig, env: Mapping[str, str]
) -> None:
    """Clear stale routing/provider state, then configure the hub to run the
    router or a spoke to route through it (credentials centralize on the hub)."""
    router = _deploy_router_config(env)
    if cfg.identity.is_hub:
        if not router["providers"]:
            raise ValueError(
                "inproc router hub requires MAC_DEPLOY_ROUTER_PROVIDERS"
            )
        invalid_specs = [
            spec
            for spec in router["providers"].split(";")
            if spec.strip() and "key=secret:" not in spec
        ]
        if invalid_specs:
            raise ValueError(
                "inproc router providers must use key=secret:<name>: %s"
                % ";".join(invalid_specs)
            )
        if not (values.get("MAC_API_TOKEN") or "").strip():
            raise ValueError("inproc router hub requires a local MAC_API_TOKEN")
    else:
        hub_base = (values.get("MAC_HUB_URL") or cfg.control.hub_url or "").strip()
        local_token = (values.get("MAC_API_TOKEN") or "").strip()
        hub_token = (
            cfg.control.hub_token or values.get("MAC_WORKER_TOKEN") or ""
        ).strip()
        if not hub_base:
            raise ValueError("inproc router spoke requires MAC_HUB_URL")
        if not hub_token:
            raise ValueError("inproc router spoke requires a hub-facing token")
        if hub_token == local_token:
            raise ValueError(
                "inproc router spoke requires a hub-facing token distinct from MAC_API_TOKEN"
            )
    _clear(values, INPROC_MANAGED_KEYS)
    if cfg.identity.is_hub:
        _apply_inproc_router_hub(values, cfg, env)
    else:
        _apply_inproc_router_spoke(values, cfg)


def _apply_non_inproc_router(values: MutableMapping[str, str], env: Mapping[str, str]) -> None:
    router = _deploy_router_config(env)
    if router["backend"]:
        values["MAC_ROUTER_BACKEND"] = router["backend"]
    if router["providers"]:
        values["MAC_ROUTER_PROVIDERS"] = router["providers"]
    if router["default_model"]:
        values["MAC_ROUTER_DEFAULT_MODEL"] = router["default_model"]
    if router["wildcard_models"]:
        values["MAC_ROUTER_WILDCARD_MODELS"] = router["wildcard_models"]


def _apply_router(values: MutableMapping[str, str], cfg: DeployEnvConfig, env: Mapping[str, str]) -> None:
    backend = (env.get("MAC_DEPLOY_ROUTER_BACKEND") or "").strip()
    if backend.lower() == "inproc":
        _apply_inproc_router(values, cfg, env)
    else:
        _apply_non_inproc_router(values, env)


def _apply_home_channel(values: MutableMapping[str, str], cfg: DeployEnvConfig) -> None:
    home_channel = (
        cfg.gateway.home_channel
        or values.get("MAC_HERMES_SLACK_HOME_CHANNEL_NAME", "").strip().lstrip("#")
        or values.get("ACC_SLACK_HOME_CHANNEL_NAME", "").strip().lstrip("#")
        or values.get("SLACK_HOME_CHANNEL_NAME", "").strip().lstrip("#")
        or ""
    )
    values["MAC_HERMES_SLACK_HOME_CHANNEL_NAME"] = home_channel
    values["ACC_HERMES_SLACK_HOME_CHANNEL_NAME"] = home_channel
    values["SLACK_HOME_CHANNEL_NAME"] = home_channel
    values.setdefault("MAC_HERMES_SYNC_SLACK_HOME_CHANNELS", "1")
    values.setdefault("SLACK_ALLOWED_USERS", "*")
    values.setdefault("SLACK_REQUIRE_MENTION", "true")
    values.setdefault("SLACK_STRICT_MENTION", "true")


def build_mac_env(
    existing: Mapping[str, str],
    cfg: DeployEnvConfig,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    env = os.environ if environ is None else environ
    values: Dict[str, str] = dict(existing)
    _ensure_secret_values(values)
    values.update(_path_values(cfg))
    values.update(_identity_values(cfg))
    values.update(_gateway_values(cfg))
    values.update(_worker_values(cfg, values))
    values.update(_shared_service_requirement_values())
    values.update(_shared_service_url_values(values, cfg))
    values.update(_webdav_values(values, cfg, env))
    memory_model = (env.get("MAC_DEPLOY_MEMORY_EMBED_MODEL") or "").strip()
    if memory_model:
        values["MAC_MEMORY_EMBED_MODEL"] = memory_model
    # media-01 durable advertisement: carry the deploy-supplied local-gen config
    # into mac.env so a GPU agent self-advertises a media route on registration
    # (GPU-gated in worker.register_worker) — no hand-patched env, no per-agent
    # JSON. A global MAC_DEPLOY_AGENT_GEN_MODEL only lights up actual GPU agents.
    for _src, _dst in (
        ("MAC_DEPLOY_AGENT_GEN_MODEL", "MAC_AGENT_GEN_MODEL"),
        ("MAC_DEPLOY_AGENT_GEN_PORT", "MAC_AGENT_GEN_PORT"),
        ("MAC_DEPLOY_AGENT_GEN_HOST", "MAC_AGENT_GEN_HOST"),
        ("MAC_DEPLOY_AGENT_GEN_BASE_URL", "MAC_AGENT_GEN_BASE_URL"),
        ("MAC_DEPLOY_AGENT_GEN_HF_HOME", "MAC_AGENT_GEN_HF_HOME"),
        # B1b: audio/video local servers (CSV catalog-id model lists per modality).
        ("MAC_DEPLOY_AGENT_GEN_AUDIO_MODELS", "MAC_AGENT_GEN_AUDIO_MODELS"),
        ("MAC_DEPLOY_AGENT_GEN_AUDIO_PORT", "MAC_AGENT_GEN_AUDIO_PORT"),
        ("MAC_DEPLOY_AGENT_GEN_VIDEO_MODELS", "MAC_AGENT_GEN_VIDEO_MODELS"),
        ("MAC_DEPLOY_AGENT_GEN_VIDEO_PORT", "MAC_AGENT_GEN_VIDEO_PORT"),
        ("MAC_DEPLOY_AGENT_MEDIA_ROUTES", "MAC_AGENT_MEDIA_ROUTES"),
        # media-01 service-role election: ops the fleet wants held (hub seeds these).
        ("MAC_DEPLOY_SERVICE_ROLE_OPS", "MAC_SERVICE_ROLE_OPS"),
        # Git credential for agents to clone/push private repos (gitops reads
        # GH_TOKEN; mac.env is sourced after the pod env so this overrides a stale
        # platform-injected token). 600-perm file, same as MAC_API_TOKEN.
        ("MAC_DEPLOY_GH_TOKEN", "GH_TOKEN"),
        # mac-selfdrive: hub drives its own tick (dispatch->review->merge->reconcile)
        # on this interval (seconds) so the autonomous loop needs no external clock.
        ("MAC_DEPLOY_HUB_TICK_INTERVAL_SECONDS", "MAC_HUB_TICK_INTERVAL_SECONDS"),
        # OpenShell sandbox requirement, data-driven from the agent's resources
        # (no hardcoded agent list). The deploy orchestrator derives this from the
        # agent's DB resources["openshell_required"]; it lands in mac.env and the
        # executor reads it via openshell_required_for_local_agent. The worker
        # also refreshes it from its live agent record at registration.
        ("MAC_DEPLOY_OPENSHELL_REQUIRED", "MAC_OPENSHELL_REQUIRED"),
    ):
        _v = (env.get(_src) or "").strip()
        if _v:
            values[_dst] = _v
    values.setdefault("MAC_REQUIRE_HERMES_STARTUP_READY", "0")
    values.setdefault("MAC_WORKER_WORKSPACE", str(cfg.paths.mac_home / "agent-workspaces"))
    values.setdefault("MAC_WORKER_HEARTBEAT_INTERVAL", "30")
    values.setdefault("MAC_WORKER_POLL_INTERVAL", "2")
    values.setdefault("MAC_WORKER_LEASE_SECONDS", "900")
    values.setdefault("MAC_WORKER_EXECUTOR", str(cfg.paths.mac_home / "bin" / "mac-hermes-task-executor"))
    values.setdefault("MAC_AGENT_STARTUP_SELF_TEST", "1")
    values.setdefault("MAC_AGENT_STARTUP_SELF_TEST_TIMEOUT", "120")
    # NeMo Relay observability on by default for new nodes (nemo-relay ships via
    # the deploy's `relay` extra + the worker runtime-deps reconcile). setdefault
    # so an operator's explicit MAC_RELAY_OBSERVABILITY=0 is preserved across
    # redeploys; set to 0 in mac.env to opt a host out.
    values.setdefault("MAC_RELAY_OBSERVABILITY", "1")
    values.setdefault("MAC_REVIEW_TICK_HUB_AGENT", cfg.identity.shared_services_manager)
    _apply_router(values, cfg, env)
    _apply_home_channel(values, cfg)
    return values


def write_mac_env_file(
    cfg: DeployEnvConfig,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    values = build_mac_env(read_env_file(cfg.paths.env_file), cfg, environ=environ)
    write_env_file(cfg.paths.env_file, values)
    return values


@dataclass(frozen=True)
class LegacyDeployArgs:
    """Named view over the 30 positional arguments that deploy-mac-fleet.sh passes
    to ``write-mac-env`` (in the order it passes them). This is the single place
    the positional contract lives, so ``config_from_legacy_args`` below reads by
    name instead of by magic index. ``*_require`` mirror the deploy script's
    Qdrant/Firecrawl "required" flags but are intentionally unused here (the env
    model always sets the requirement on)."""

    env_file: str
    mac_home: str
    home: str
    port: str
    home_channel: str
    gateway_model: str
    gateway_provider: str
    gateway_base_url: str
    hub_url: str
    hub_token: str
    bind_host: str
    worker_mode: str
    worker_capabilities: str
    worker_allowed_projects: str
    worker_required_metadata: str
    worker_require_canary: str
    agent: str
    supervisor_kind: str
    shared_services_manager: str
    qdrant_url: str
    qdrant_require: str
    qdrant_port: str
    firecrawl_url: str
    firecrawl_require: str
    firecrawl_port: str
    webdav_enabled: str
    webdav_url: str
    webdav_port: str
    webdav_root: str
    webdav_public_path: str

    ARITY = 30

    @classmethod
    def from_argv(cls, args: Sequence[str]) -> "LegacyDeployArgs":
        if len(args) != cls.ARITY:
            raise SystemExit(
                "write-mac-env expects %d positional arguments; got %d" % (cls.ARITY, len(args))
            )
        return cls(*args)


def config_from_legacy_args(args: Sequence[str], env: Mapping[str, str]) -> DeployEnvConfig:
    a = LegacyDeployArgs.from_argv(args)
    gateway_model = a.gateway_model.strip()
    if gateway_model == "*":
        gateway_model = ""
    network_provider = (
        env.get("MAC_DEPLOY_NETWORK_PROVIDER")
        or env.get("NETWORK_PROVIDER")
        or "tailscale"
    ).strip().lower()
    agent = a.agent.strip()
    return DeployEnvConfig(
        paths=DeployPaths(
            env_file=Path(a.env_file),
            mac_home=Path(a.mac_home),
            home=Path(a.home),
        ),
        control=ControlConfig(
            port=a.port,
            hub_url=a.hub_url.strip(),
            hub_token=a.hub_token.strip(),
            bind_host=a.bind_host.strip() or "127.0.0.1",
            supervisor_kind=a.supervisor_kind.strip(),
            network_provider=network_provider,
        ),
        gateway=GatewayConfig(
            home_channel=a.home_channel.strip().lstrip("#"),
            model=gateway_model,
            provider=a.gateway_provider.strip(),
            base_url=a.gateway_base_url.strip(),
        ),
        worker=WorkerConfig(
            mode=a.worker_mode.strip() or "heartbeat",
            capabilities=a.worker_capabilities.strip() or DEFAULT_WORKER_CAPABILITIES,
            allowed_projects=a.worker_allowed_projects.strip(),
            required_metadata=a.worker_required_metadata.strip(),
            require_canary=a.worker_require_canary.strip() or "1",
        ),
        services=SharedServicesConfig(
            qdrant_url=a.qdrant_url.strip(),
            qdrant_port=a.qdrant_port.strip() or "6333",
            firecrawl_url=a.firecrawl_url.strip(),
            firecrawl_port=a.firecrawl_port.strip() or "3002",
            webdav_enabled=a.webdav_enabled.strip(),
            webdav_url=a.webdav_url.strip(),
            webdav_port=a.webdav_port.strip() or "80",
            webdav_root=a.webdav_root.strip(),
            webdav_public_path=a.webdav_public_path.strip() or "/artifacts/",
        ),
        identity=DeployIdentity(
            agent=agent,
            shared_services_manager=a.shared_services_manager.strip() or agent,
            fleet_name=env.get("FLEET_NAME") or "mac",
        ),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mac.deploy_env")
    subparsers = parser.add_subparsers(dest="command", required=True)
    write_parser = subparsers.add_parser("write-mac-env")
    write_parser.add_argument("legacy_args", nargs=30)
    ns = parser.parse_args(argv)
    if ns.command == "write-mac-env":
        cfg = config_from_legacy_args(ns.legacy_args, os.environ)
        write_mac_env_file(cfg)
        return 0
    raise SystemExit("unknown command: %s" % ns.command)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
