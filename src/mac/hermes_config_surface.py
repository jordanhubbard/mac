"""Fleet-scoped OpenClaw configuration inspection and application.

The dashboard should not invent a second OpenClaw settings model.  This module
builds its inspector from the vendored OpenClaw runtime's own supported surfaces:
``config.yaml`` defaults, ``.env`` declarations, plugin manifests, and skill
frontmatter.  Writes land in the home-scoped fleet registry as desired state and
can also be applied to the current node's OpenClaw home.

Terminology note: OpenClaw is the fleet's chat-gateway runtime. The public
schema strings (``SCHEMA`` / ``PAYLOAD_SCHEMA``) and the fleet registry
``defaults.hermes`` config key are retained as-is for backward compatibility
with the persisted ``fleets.yaml`` wire format, the deploy payload contract, and
existing callers; reads accept the OpenClaw-named ``openclaw`` block first and
fall back to the legacy ``hermes`` key.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path

from mac import mac_paths
from mac.atomic_file import fsync_directory
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml

from mac.deploy_env import parse_env_text, render_env
from mac.agentbus_control import (
    HERMES_CONFIG_APPLY_RESULT_TOPIC,
    HERMES_CONFIG_APPLY_TOPIC,
)
from mac.models import ValidationError, utcnow

SCHEMA = "mac.hermes_config_surface.v1"
PAYLOAD_SCHEMA = "mac.hermes_fleet_config_payload.v1"

_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_DENYLIST = {
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "LD_DEBUG",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_FALLBACK_FRAMEWORK_PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONEXECUTABLE",
    "PYTHONNOUSERSITE",
    "NODE_OPTIONS",
    "NODE_PATH",
    "PATH",
    "SHELL",
    "BROWSER",
    "EDITOR",
    "VISUAL",
    "PAGER",
    "GIT_SSH_COMMAND",
    "GIT_EXEC_PATH",
    "GIT_SHELL",
    "HERMES_HOME",
    "HERMES_PROFILE",
    "HERMES_CONFIG",
    "HERMES_ENV",
}
_SECRET_NAME_PARTS = ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "PRIVATE_KEY", "CREDENTIAL", "KEY_JSON")
_RUNTIME_FIELDS = (
    "slack_home_channel_name",
    "gateway_model",
    "gateway_provider",
    "gateway_base_url",
)


def hermes_home() -> Path:
    return mac_paths.gateway_home()


def registry_path() -> Path:
    raw = (
        os.environ.get("MAC_FLEETS_CONFIG")
        or os.environ.get("MAC_DEPLOY_FLEETS_CONFIG")
        or os.environ.get("MAC_DEPLOY_FLEET_REGISTRY")
        or str(mac_paths.fleets_config())
    )
    return Path(raw).expanduser()


def _read_yaml_mapping(path: Path) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValidationError("%s must contain a YAML mapping" % path)
    return data


def _atomic_yaml_write(path: Path, data: Mapping[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(dict(data), fh, sort_keys=False)
            fh.flush()
            # Without this the rename can outrun the data blocks on a crash and
            # leave an empty YAML file, which parses as "no configuration".
            os.fsync(fh.fileno())
        tmp.chmod(mode)
        tmp.replace(path)
        fsync_directory(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _atomic_text_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(mode)
        tmp.replace(path)
        fsync_directory(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _is_mapping(value: Any) -> bool:
    return isinstance(value, dict)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in override.items():
        if _is_mapping(value) and _is_mapping(result.get(key)):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _flatten(value: Mapping[str, Any], prefix: str = "") -> List[Tuple[str, Any]]:
    out: List[Tuple[str, Any]] = []
    for key in sorted(value):
        item = value[key]
        path = "%s.%s" % (prefix, key) if prefix else str(key)
        if isinstance(item, dict):
            out.extend(_flatten(item, path))
        else:
            out.append((path, item))
    return out


def _nested_get(mapping: Mapping[str, Any], dotted: str) -> Tuple[bool, Any]:
    cur: Any = mapping
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _nested_set(mapping: Dict[str, Any], dotted: str, value: Any) -> None:
    if not dotted or any(not part for part in dotted.split(".")):
        raise ValidationError("config key must be a non-empty dotted path")
    cur = mapping
    parts = dotted.split(".")
    for part in parts[:-1]:
        existing = cur.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cur[part] = existing
        cur = existing
    cur[parts[-1]] = value


def _nested_delete(mapping: Dict[str, Any], dotted: str) -> None:
    cur: Any = mapping
    parents: List[Tuple[Dict[str, Any], str]] = []
    for part in dotted.split(".")[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        parents.append((cur, part))
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(dotted.split(".")[-1], None)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)


def _redacted(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return "<redacted:%s:chars=%d>" % (digest, len(text))


def _looks_secret(name: str, meta: Optional[Mapping[str, Any]] = None) -> bool:
    if meta and (meta.get("password") or meta.get("secret")):
        return True
    upper = name.upper()
    return any(part in upper for part in _SECRET_NAME_PARTS)


def _validate_env_name(name: str) -> str:
    key = str(name or "").strip()
    if not _ENV_VAR_RE.match(key):
        raise ValidationError("invalid environment variable name: %r" % name)
    if key in _ENV_DENYLIST:
        raise ValidationError("environment variable %s is not dashboard-writable" % key)
    return key


def _read_env(path: Path) -> Dict[str, str]:
    try:
        return parse_env_text(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _write_env(path: Path, values: Mapping[str, str]) -> None:
    _atomic_text_write(path, render_env({str(k): str(v) for k, v in values.items()}), mode=0o600)


def _hermes_config_module() -> Any:
    """The vendored hermes_cli config module, which no longer ships.

    The Hermes snapshot was removed on 2026-08-17: the runtime was measured
    inactive (openclaw is the live gateway) and the tree was twice the size of
    mac's own code. Both callers already guard this with try/except and fall
    back to empty, so the surface degrades to "no Hermes-declared env vars and
    no Hermes config defaults" rather than failing.

    Kept as a named seam because Hermes can be fetched and patched on demand if
    it is ever needed again.
    """
    raise ModuleNotFoundError(
        "the vendored hermes_cli was removed; fetch Hermes on demand if needed"
    )


def _safe_json_value(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _frontmatter(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        data = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _env_specs_from_entry(entry: Any, *, source: str, required: bool) -> List[Dict[str, Any]]:
    if isinstance(entry, str):
        return [{
            "name": entry,
            "description": "",
            "prompt": entry,
            "url": None,
            "password": _looks_secret(entry),
            "category": "plugin",
            "required": required,
            "source": source,
        }]
    if isinstance(entry, dict) and entry.get("name"):
        name = str(entry.get("name"))
        return [{
            "name": name,
            "description": str(entry.get("description") or ""),
            "prompt": str(entry.get("prompt") or name),
            "url": entry.get("url"),
            "password": _looks_secret(name, entry),
            "category": str(entry.get("category") or "plugin"),
            "required": required,
            "source": source,
        }]
    return []


def _plugin_manifest_records() -> List[Dict[str, Any]]:
    # The "bundled" root was the vendored Hermes tree, removed 2026-08-17.
    roots = [
        (hermes_home() / "plugins", "user"),
    ]
    records: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for root, source in roots:
        if not root.is_dir():
            continue
        for manifest_path in sorted(root.rglob("plugin.yaml")) + sorted(root.rglob("plugin.yml")):
            try:
                rel = manifest_path.parent.relative_to(root)
            except ValueError:
                continue
            if len(rel.parts) > 2:
                continue
            try:
                manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001 - inspector should survive bad manifests.
                continue
            if not isinstance(manifest, dict):
                continue
            name = str(manifest.get("name") or manifest_path.parent.name)
            key = "/".join(rel.parts) if len(rel.parts) > 1 else name
            if key in seen:
                continue
            seen.add(key)
            records.append({
                "key": key,
                "name": name,
                "label": manifest.get("label") or name,
                "version": manifest.get("version") or "",
                "kind": manifest.get("kind") or "standalone",
                "source": source,
                "path": str(manifest_path),
                "description": manifest.get("description") or "",
                "requires_env": manifest.get("requires_env") or [],
                "optional_env": manifest.get("optional_env") or [],
                "provides_tools": manifest.get("provides_tools") or [],
                "provides_hooks": manifest.get("hooks") or manifest.get("provides_hooks") or [],
            })
    return records


def _skill_records() -> List[Dict[str, Any]]:
    # The "bundled" root was the vendored Hermes tree, removed 2026-08-17.
    roots = [
        (hermes_home() / "skills", "installed"),
    ]
    records: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for root, source in roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.rglob("SKILL.md")):
            try:
                rel = skill_md.parent.relative_to(root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            meta = _frontmatter(skill_md)
            name = str(meta.get("name") or skill_md.parent.name)[:64]
            key = "/".join(rel.parts)
            identity = name or key
            if identity in seen:
                continue
            seen.add(identity)
            category = rel.parts[0] if len(rel.parts) > 1 else ""
            records.append({
                "name": name or skill_md.parent.name,
                "key": key,
                "category": category,
                "source": source,
                "path": str(skill_md),
                "description": str(meta.get("description") or "")[:1024],
                "tags": meta.get("tags") or [],
                "triggers": meta.get("triggers") or [],
                "platforms": meta.get("platforms") or [],
                "required_environment_variables": meta.get("required_environment_variables") or [],
            })
    return sorted(records, key=lambda item: (str(item.get("category") or ""), str(item.get("name") or "")))


def _declared_env_specs(plugins: List[Dict[str, Any]], skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    specs: Dict[str, Dict[str, Any]] = {}
    try:
        cfg = _hermes_config_module()
        for source, required, values in (
            ("hermes_required", True, getattr(cfg, "REQUIRED_ENV_VARS", {}) or {}),
            ("hermes_optional", False, getattr(cfg, "OPTIONAL_ENV_VARS", {}) or {}),
        ):
            if isinstance(values, dict):
                for name, meta in values.items():
                    if not isinstance(meta, dict):
                        meta = {}
                    specs[str(name)] = {
                        "name": str(name),
                        "description": str(meta.get("description") or ""),
                        "prompt": str(meta.get("prompt") or name),
                        "url": meta.get("url"),
                        "password": bool(meta.get("password") or _looks_secret(str(name), meta)),
                        "category": str(meta.get("category") or "general"),
                        "required": required,
                        "source": source,
                    }
    except Exception:  # noqa: BLE001 - vendored import should not break dashboard.
        pass

    for plugin in plugins:
        source = "plugin:%s" % plugin.get("key")
        for entry in plugin.get("requires_env") or []:
            for spec in _env_specs_from_entry(entry, source=source, required=True):
                specs.setdefault(spec["name"], spec)
        for entry in plugin.get("optional_env") or []:
            for spec in _env_specs_from_entry(entry, source=source, required=False):
                specs.setdefault(spec["name"], spec)

    for skill in skills:
        source = "skill:%s" % skill.get("name")
        for entry in skill.get("required_environment_variables") or []:
            for spec in _env_specs_from_entry(entry, source=source, required=True):
                spec["category"] = "skill"
                specs.setdefault(spec["name"], spec)

    return sorted(specs.values(), key=lambda item: (str(item.get("category") or ""), str(item.get("name") or "")))


def _load_registry(path: Optional[Path] = None) -> Dict[str, Any]:
    selected = path or registry_path()
    if not selected.exists():
        return {"version": 1, "fleets": {}}
    data = _read_yaml_mapping(selected)
    data.setdefault("version", 1)
    data.setdefault("fleets", {})
    return data


def _fleet_entries(registry: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    fleets = registry.get("fleets")
    result: Dict[str, Dict[str, Any]] = {}
    if isinstance(fleets, dict):
        for key, value in fleets.items():
            if isinstance(value, dict):
                result[str(key)] = value
    elif isinstance(fleets, list):
        for item in fleets:
            if isinstance(item, dict):
                key = str(item.get("hub_agent") or item.get("fleet_name") or item.get("name") or "")
                if key:
                    result[key] = item
    return result


def _find_or_create_fleet_entry(
    registry: Dict[str, Any],
    fleet: Mapping[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    fleet_name = str(fleet.get("name") or fleet.get("id") or "default")
    fleet_id = str(fleet.get("id") or "")
    entries = _fleet_entries(registry)
    candidates = {fleet_name, fleet_id}
    for key, entry in entries.items():
        if key in candidates:
            return key, entry
        if str(entry.get("fleet_name") or entry.get("name") or "") in candidates:
            return key, entry
        if str(entry.get("hub_agent") or "") in candidates:
            return key, entry
    if not isinstance(registry.get("fleets"), dict):
        registry["fleets"] = {}
    entry = {
        "sample": False,
        "fleet_name": fleet_name,
        "hub_agent": fleet_name,
        "defaults": {"hermes": {}},
        "agents": [],
    }
    registry["fleets"][fleet_name] = entry
    return fleet_name, entry


def _fleet_hermes_defaults(entry: Mapping[str, Any]) -> Dict[str, Any]:
    defaults = entry.get("defaults") if isinstance(entry.get("defaults"), dict) else {}
    # Prefer the OpenClaw-named block; fall back to the legacy ``hermes`` key so
    # existing fleet registries keep loading (backward-compatible READ only).
    openclaw = defaults.get("openclaw") if isinstance(defaults.get("openclaw"), dict) else None
    if openclaw is None:
        openclaw = defaults.get("hermes") if isinstance(defaults.get("hermes"), dict) else {}
    return deepcopy(openclaw)


def hermes_payload_from_defaults(hermes: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema": PAYLOAD_SCHEMA,
        "runtime": {key: hermes.get(key, "") for key in _RUNTIME_FIELDS if key in hermes},
        "config": hermes.get("config") if isinstance(hermes.get("config"), dict) else {},
        "env": hermes.get("env") if isinstance(hermes.get("env"), dict) else {},
        "plugins": hermes.get("plugins") if isinstance(hermes.get("plugins"), dict) else {},
        "skills": hermes.get("skills") if isinstance(hermes.get("skills"), dict) else {},
    }


def fleet_hermes_payload(fleet: Mapping[str, Any], *, path: Optional[Path] = None) -> Dict[str, Any]:
    registry = _load_registry(path)
    temp = deepcopy(registry)
    _entry_key, entry = _find_or_create_fleet_entry(temp, fleet)
    return hermes_payload_from_defaults(_fleet_hermes_defaults(entry))


def payload_digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def redacted_hermes_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    redacted = deepcopy(dict(payload))
    env = redacted.get("env")
    if isinstance(env, dict):
        redacted["env"] = {str(key): _redacted(value) for key, value in env.items()}
    return redacted


def _current_config_defaults() -> Dict[str, Any]:
    try:
        default = getattr(_hermes_config_module(), "DEFAULT_CONFIG", {}) or {}
        return deepcopy(default) if isinstance(default, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _config_field_records(
    current_config: Mapping[str, Any],
    desired_config: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    defaults = _current_config_defaults()
    keys = {path for path, _ in _flatten(defaults)}
    keys.update(path for path, _ in _flatten(desired_config))
    keys.update(path for path, _ in _flatten(current_config))
    out: List[Dict[str, Any]] = []
    for path in sorted(keys):
        has_default, default_value = _nested_get(defaults, path)
        has_current, current_value = _nested_get(current_config, path)
        has_desired, desired_value = _nested_get(desired_config, path)
        value = desired_value if has_desired else (current_value if has_current else default_value)
        out.append({
            "key": path,
            "value": _safe_json_value(value),
            "default": _safe_json_value(default_value) if has_default else None,
            "type": type(value).__name__ if value is not None else "null",
            "source": "fleet_desired" if has_desired else ("local_config" if has_current else "default"),
            "desired": has_desired,
        })
    return out


def _env_field_records(
    specs: List[Dict[str, Any]],
    local_env: Mapping[str, str],
    desired_env: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    declared = {str(spec.get("name")): spec for spec in specs}
    for name in sorted(set(declared) | set(local_env) | set(desired_env)):
        spec = declared.get(name) or {
            "name": name,
            "description": "Undeclared environment variable present in Hermes env",
            "prompt": name,
            "url": None,
            "password": _looks_secret(name),
            "category": "undeclared",
            "required": False,
            "source": "local",
        }
        desired_present = name in desired_env and str(desired_env.get(name) or "") != ""
        local_present = name in local_env and str(local_env.get(name) or "") != ""
        env_present = os.environ.get(name) not in (None, "")
        value = desired_env.get(name) if desired_present else (local_env.get(name) if local_present else os.environ.get(name, ""))
        out.append({
            **spec,
            "present": bool(desired_present or local_present or env_present),
            "configured": bool(desired_present or local_present or env_present),
            "desired": desired_present,
            "source": "fleet_desired" if desired_present else ("local_env" if local_present else ("process_env" if env_present else spec.get("source"))),
            "redacted_value": _redacted(value) if (value and (spec.get("password") or _looks_secret(name))) else (str(value) if value and not spec.get("password") else ""),
        })
    return out


def _plugin_records_with_state(
    plugins: List[Dict[str, Any]],
    desired_plugins: Mapping[str, Any],
    current_config: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    _, current_plugins = _nested_get(current_config, "plugins")
    current_plugins = current_plugins if isinstance(current_plugins, dict) else {}
    desired_enabled = set(str(v) for v in desired_plugins.get("enabled") or [])
    desired_disabled = set(str(v) for v in desired_plugins.get("disabled") or [])
    current_enabled = set(str(v) for v in current_plugins.get("enabled") or [])
    current_disabled = set(str(v) for v in current_plugins.get("disabled") or [])
    out = []
    for plugin in plugins:
        key = str(plugin.get("key") or plugin.get("name"))
        name = str(plugin.get("name") or key)
        state = "auto"
        source = "default"
        if key in desired_disabled or name in desired_disabled:
            state, source = "disabled", "fleet_desired"
        elif key in desired_enabled or name in desired_enabled:
            state, source = "enabled", "fleet_desired"
        elif key in current_disabled or name in current_disabled:
            state, source = "disabled", "local_config"
        elif key in current_enabled or name in current_enabled:
            state, source = "enabled", "local_config"
        elif plugin.get("source") == "bundled" and plugin.get("kind") in {"backend", "platform"}:
            state = "auto_enabled"
        out.append({**plugin, "state": state, "state_source": source})
    return out


def _skill_records_with_state(
    skills: List[Dict[str, Any]],
    desired_skills: Mapping[str, Any],
    current_config: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    _, current_skills = _nested_get(current_config, "skills")
    current_skills = current_skills if isinstance(current_skills, dict) else {}
    desired_disabled = set(str(v) for v in desired_skills.get("disabled") or [])
    current_disabled = set(str(v) for v in current_skills.get("disabled") or [])
    out = []
    for skill in skills:
        name = str(skill.get("name") or skill.get("key"))
        disabled = name in desired_disabled or name in current_disabled
        out.append({
            **skill,
            "enabled": not disabled,
            "state": "disabled" if disabled else "enabled",
            "state_source": "fleet_desired" if name in desired_disabled else ("local_config" if name in current_disabled else "default"),
        })
    return out


def _latest_stream_by_agent(
    streams: Iterable[Mapping[str, Any]],
    *,
    topic: str,
    agent_field: str,
    agent_ids: set[str],
) -> Dict[str, Mapping[str, Any]]:
    latest: Dict[str, Mapping[str, Any]] = {}
    for stream in streams:
        if str(stream.get("topic") or "") != topic:
            continue
        agent_id = str(stream.get(agent_field) or "")
        if agent_id not in agent_ids:
            continue
        previous = latest.get(agent_id)
        stamp = str(stream.get("updated_at") or stream.get("created_at") or "")
        previous_stamp = str(previous.get("updated_at") or previous.get("created_at") or "") if previous else ""
        if previous is None or stamp >= previous_stamp:
            latest[agent_id] = stream
    return latest


def _agent_apply_status_records(
    agents: Iterable[Mapping[str, Any]],
    agentbus_streams: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    agent_rows = list(agents)
    agent_ids = {
        str(agent.get("id") or agent.get("agent", {}).get("id") or "")
        for agent in agent_rows
        if str(agent.get("id") or agent.get("agent", {}).get("id") or "")
    }
    streams = list(agentbus_streams)
    applies = _latest_stream_by_agent(
        streams,
        topic=HERMES_CONFIG_APPLY_TOPIC,
        agent_field="recipient_agent_id",
        agent_ids=agent_ids,
    )
    results = _latest_stream_by_agent(
        streams,
        topic=HERMES_CONFIG_APPLY_RESULT_TOPIC,
        agent_field="sender_agent_id",
        agent_ids=agent_ids,
    )
    rows: List[Dict[str, Any]] = []
    for agent in agent_rows:
        agent_id = str(agent.get("id") or agent.get("agent", {}).get("id") or "")
        apply_stream = applies.get(agent_id)
        result_stream = results.get(agent_id)
        if result_stream:
            state = "acknowledged"
        elif apply_stream:
            state = "sent"
        else:
            state = "never"
        rows.append({
            "agent_id": agent_id,
            "agent_name": agent.get("name") or agent.get("agent", {}).get("name"),
            "state": state,
            "last_apply_stream_id": apply_stream.get("id") if apply_stream else None,
            "last_apply_status": apply_stream.get("status") if apply_stream else None,
            "last_apply_at": apply_stream.get("updated_at") or apply_stream.get("created_at") if apply_stream else None,
            "last_result_stream_id": result_stream.get("id") if result_stream else None,
            "last_result_status": result_stream.get("status") if result_stream else None,
            "last_result_at": result_stream.get("updated_at") or result_stream.get("created_at") if result_stream else None,
        })
    return rows


def build_hermes_config_surfaces(
    fleets: Iterable[Mapping[str, Any]],
    agents: Iterable[Mapping[str, Any]],
    *,
    registry: Optional[Mapping[str, Any]] = None,
    agentbus_streams: Iterable[Mapping[str, Any]] = (),
) -> List[Dict[str, Any]]:
    reg = dict(registry) if registry is not None else _load_registry()
    reg_path = registry_path()
    home = hermes_home()
    current_config = _read_yaml_mapping(home / "config.yaml")
    local_env = _read_env(home / ".env")
    plugins = _plugin_manifest_records()
    skills = _skill_records()
    env_specs = _declared_env_specs(plugins, skills)
    agents_list = list(agents)
    surfaces: List[Dict[str, Any]] = []
    for fleet in fleets:
        temp_registry = deepcopy(reg)
        entry_key, entry = _find_or_create_fleet_entry(temp_registry, fleet)
        hermes_defaults = _fleet_hermes_defaults(entry)
        desired_config = hermes_defaults.get("config") if isinstance(hermes_defaults.get("config"), dict) else {}
        desired_env = hermes_defaults.get("env") if isinstance(hermes_defaults.get("env"), dict) else {}
        desired_plugins = hermes_defaults.get("plugins") if isinstance(hermes_defaults.get("plugins"), dict) else {}
        desired_skills = hermes_defaults.get("skills") if isinstance(hermes_defaults.get("skills"), dict) else {}
        desired_payload = hermes_payload_from_defaults(hermes_defaults)
        fleet_agent_ids = set(str(v) for v in fleet.get("agent_ids") or [])
        fleet_agents = [
            agent for agent in agents_list
            if str(agent.get("id") or "") in fleet_agent_ids
            or str(agent.get("agent", {}).get("id") or "") in fleet_agent_ids
        ]
        agent_overrides = []
        for item in entry.get("agents") or []:
            if isinstance(item, dict) and isinstance(item.get("hermes"), dict):
                agent_overrides.append({
                    "name": item.get("name") or "",
                    "keys": sorted(item["hermes"].keys()),
                })
        surfaces.append({
            "schema": SCHEMA,
            "fleet_id": fleet.get("id"),
            "fleet_name": fleet.get("name"),
            "registry_key": entry_key,
            "registry_path": str(reg_path),
            "hermes_home": str(home),
            "config_path": str(home / "config.yaml"),
            "env_path": str(home / ".env"),
            "updated_at": utcnow(),
            "runtime": {key: hermes_defaults.get(key, "") for key in _RUNTIME_FIELDS},
            "agent_count": len(fleet_agents),
            "agents": [
                {
                    "id": agent.get("id") or agent.get("agent", {}).get("id"),
                    "name": agent.get("name") or agent.get("agent", {}).get("name"),
                    "hermes_instance_id": agent.get("hermes_instance_id") or agent.get("agent", {}).get("hermes_instance_id"),
                    "support_surface": {
                        "capabilities": agent.get("capabilities") or agent.get("agent", {}).get("capabilities") or [],
                        "installed_packages": agent.get("installed_packages") or agent.get("agent", {}).get("installed_packages") or {},
                    },
                }
                for agent in fleet_agents
            ],
            "agent_overrides": agent_overrides,
            "config_fields": _config_field_records(current_config, desired_config),
            "env_vars": _env_field_records(env_specs, local_env, desired_env),
            "plugins": _plugin_records_with_state(plugins, desired_plugins, current_config),
            "skills": _skill_records_with_state(skills, desired_skills, current_config),
            "apply_status": _agent_apply_status_records(fleet_agents, agentbus_streams),
            "desired_digest": payload_digest(desired_payload),
            "desired_payload_redacted": redacted_hermes_payload(desired_payload),
            "desired": {
                "runtime": {key: hermes_defaults.get(key, "") for key in _RUNTIME_FIELDS},
                "config": desired_config,
                "env_keys": sorted(desired_env.keys()),
                "plugins": desired_plugins,
                "skills": desired_skills,
            },
        })
    return surfaces


def _normalize_string_list(value: Any, *, field_name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("%s must be a list" % field_name)
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_surface_patch(raw: Mapping[str, Any]) -> Dict[str, Any]:
    runtime = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}
    config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
    plugins = raw.get("plugins") if isinstance(raw.get("plugins"), dict) else {}
    skills = raw.get("skills") if isinstance(raw.get("skills"), dict) else {}
    patch: Dict[str, Any] = {
        "schema": PAYLOAD_SCHEMA,
        "runtime": {key: str(runtime.get(key) or "") for key in _RUNTIME_FIELDS if key in runtime},
        "config": deepcopy(config),
        "remove_config": _normalize_string_list(raw.get("remove_config"), field_name="remove_config"),
        "env": {_validate_env_name(key): str(value) for key, value in env.items()},
        "remove_env": [_validate_env_name(key) for key in _normalize_string_list(raw.get("remove_env"), field_name="remove_env")],
        "plugins": {
            "enabled": _normalize_string_list(plugins.get("enabled"), field_name="plugins.enabled") if "enabled" in plugins else None,
            "disabled": _normalize_string_list(plugins.get("disabled"), field_name="plugins.disabled") if "disabled" in plugins else None,
        },
        "skills": {
            "disabled": _normalize_string_list(skills.get("disabled"), field_name="skills.disabled") if "disabled" in skills else None,
            "platform_disabled": skills.get("platform_disabled") if isinstance(skills.get("platform_disabled"), dict) else None,
        },
    }
    return patch


def _apply_patch_to_hermes_mapping(hermes: Dict[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in (patch.get("runtime") or {}).items():
        if key in _RUNTIME_FIELDS:
            hermes[key] = value
    config = hermes.setdefault("config", {})
    if not isinstance(config, dict):
        config = {}
        hermes["config"] = config
    for key, value in (patch.get("config") or {}).items():
        _nested_set(config, str(key), _safe_json_value(value))
    for key in patch.get("remove_config") or []:
        _nested_delete(config, str(key))
    env = hermes.setdefault("env", {})
    if not isinstance(env, dict):
        env = {}
        hermes["env"] = env
    for key, value in (patch.get("env") or {}).items():
        if str(value) == "":
            env.pop(key, None)
        else:
            env[key] = str(value)
    for key in patch.get("remove_env") or []:
        env.pop(key, None)
    plugins_patch = patch.get("plugins") if isinstance(patch.get("plugins"), dict) else {}
    if plugins_patch:
        plugins = hermes.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            plugins = {}
            hermes["plugins"] = plugins
        if plugins_patch.get("enabled") is not None:
            plugins["enabled"] = plugins_patch["enabled"]
        if plugins_patch.get("disabled") is not None:
            plugins["disabled"] = plugins_patch["disabled"]
    skills_patch = patch.get("skills") if isinstance(patch.get("skills"), dict) else {}
    if skills_patch:
        skills = hermes.setdefault("skills", {})
        if not isinstance(skills, dict):
            skills = {}
            hermes["skills"] = skills
        if skills_patch.get("disabled") is not None:
            skills["disabled"] = skills_patch["disabled"]
        if skills_patch.get("platform_disabled") is not None:
            normalized: Dict[str, List[str]] = {}
            for platform, values in skills_patch["platform_disabled"].items():
                normalized[str(platform)] = _normalize_string_list(values, field_name="skills.platform_disabled.%s" % platform)
            skills["platform_disabled"] = normalized


def _ensure_never_prompt_defaults(config: Dict[str, Any]) -> None:
    """Bake the fleet's never-prompt approval posture into config (sandbox-01).

    Hermes must never block on an approval prompt: the OpenShell sandbox is the
    enforcement layer, and the legacy gateway prompts went to an open channel
    where anyone could approve (no real security). Default-if-absent, so an
    explicit operator ``approvals`` config still wins.

      * ``approvals.mode = off``          -> no dangerous-command approval
                                             prompts (executor + gateway both
                                             read this; immune to the
                                             ``HERMES_YOLO_MODE`` import freeze)
      * ``approvals.cron_mode = approve`` -> non-interactive runs don't fall to
                                             the default ``deny``

    Note: enabling never-prompt for the gateway means the (currently
    un-sandboxed) Slack agent runs silently — real enforcement there requires
    wrapping the gateway service under OpenShell too (tracked separately).

    ENFORCED, not default-if-absent: agents carry a stale ``approvals.mode:
    manual`` / ``cron_mode: deny`` from an earlier lifecycle, and a setdefault
    left those in place so the gateway kept showing "Command Approval Required".
    The never-prompt posture is a fleet invariant, so we overwrite. An operator
    can opt a single host back into interactive approvals with
    ``MAC_HERMES_ALLOW_APPROVAL_PROMPTS=1`` (then their existing config wins).
    """
    if os.environ.get("MAC_HERMES_ALLOW_APPROVAL_PROMPTS", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    approvals = config.get("approvals")
    if not isinstance(approvals, dict):
        approvals = {}
    approvals["mode"] = "off"          # no dangerous-command approval prompts
    approvals["cron_mode"] = "approve"  # non-interactive runs must not fall to deny
    config["approvals"] = approvals


def _promote_slack_accounts_tokens(config: Dict[str, Any], home: Path) -> None:
    """Promote Slack tokens from ~/.hermes/slack_accounts.json into config['env'].

    An agent provisioned via a multi-workspace ``slack_accounts.json`` (e.g. one
    migrated from another host) carries its bot/app tokens there, NOT in the
    config.yaml ``env:`` block — and the gateway enables the Slack platform off
    the env tokens, so without this it comes up "No messaging platforms enabled"
    (the bullwinkle case). Promote the first account's xoxb/xapp pair so a
    redeploy keeps Slack working. setdefault: an explicit env token wins, and
    slack_accounts.json still drives the actual multi-workspace connections.
    """
    env = config.get("env") if isinstance(config.get("env"), dict) else {}
    if env.get("SLACK_BOT_TOKEN") and env.get("SLACK_APP_TOKEN"):
        return  # already enabled via explicit env tokens
    accts = home / "slack_accounts.json"
    if not accts.exists():
        return
    try:
        data = json.loads(accts.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — malformed accounts file: leave config as-is
        return
    accounts = data if isinstance(data, list) else (data.get("accounts") or data.get("agents") or [])
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        bot = str(acct.get("bot_token") or "")
        app = str(acct.get("app_token") or "")
        if bot.startswith("xoxb") and app.startswith("xapp"):
            env.setdefault("SLACK_BOT_TOKEN", bot)
            env.setdefault("SLACK_APP_TOKEN", app)
            user = str(acct.get("user_token") or "")
            if user:
                env.setdefault("SLACK_USER_TOKEN", user)
            config["env"] = env
            return


def apply_hermes_surface_payload(
    payload: Mapping[str, Any],
    *,
    target_home: Optional[Path] = None,
) -> Dict[str, Any]:
    home = (target_home or hermes_home()).expanduser()
    config_path = home / "config.yaml"
    env_path = home / ".env"
    patch = normalize_surface_patch(payload)
    config = _read_yaml_mapping(config_path)
    env = _read_env(env_path)
    hermes: Dict[str, Any] = {
        "config": config,
        "env": env,
        "plugins": config.get("plugins") if isinstance(config.get("plugins"), dict) else {},
        "skills": config.get("skills") if isinstance(config.get("skills"), dict) else {},
    }
    _apply_patch_to_hermes_mapping(hermes, patch)
    config = hermes.get("config") if isinstance(hermes.get("config"), dict) else {}
    for section in ("plugins", "skills"):
        if isinstance(hermes.get(section), dict) and hermes[section]:
            config[section] = hermes[section]
    _ensure_never_prompt_defaults(config)
    _promote_slack_accounts_tokens(config, home)
    _atomic_yaml_write(config_path, config, mode=0o600)
    _write_env(env_path, hermes.get("env") if isinstance(hermes.get("env"), dict) else {})
    return {
        "schema": PAYLOAD_SCHEMA,
        "applied": True,
        "hermes_home": str(home),
        "config_path": str(config_path),
        "env_path": str(env_path),
        "config_keys": sorted((patch.get("config") or {}).keys()),
        "env_keys": sorted((patch.get("env") or {}).keys()),
        "removed_env": sorted(patch.get("remove_env") or []),
    }


def update_fleet_hermes_surface(
    fleet: Mapping[str, Any],
    patch_body: Mapping[str, Any],
    *,
    apply_local: bool = True,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    patch = normalize_surface_patch(patch_body)
    reg_path = path or registry_path()
    registry = _load_registry(reg_path)
    _, entry = _find_or_create_fleet_entry(registry, fleet)
    defaults = entry.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
        entry["defaults"] = defaults
    hermes = defaults.setdefault("hermes", {})
    if not isinstance(hermes, dict):
        hermes = {}
        defaults["hermes"] = hermes
    _apply_patch_to_hermes_mapping(hermes, patch)
    _atomic_yaml_write(reg_path, registry, mode=0o600)
    local_result = None
    if apply_local:
        local_result = apply_hermes_surface_payload(patch)
    return {
        "schema": PAYLOAD_SCHEMA,
        "updated": True,
        "fleet_id": fleet.get("id"),
        "fleet_name": fleet.get("name"),
        "registry_path": str(reg_path),
        "registry_updated": True,
        "local_apply": local_result,
        "config_keys": sorted((patch.get("config") or {}).keys()),
        "env_keys": sorted((patch.get("env") or {}).keys()),
        "removed_env": sorted(patch.get("remove_env") or []),
    }


def encode_deploy_payload(hermes: Mapping[str, Any]) -> str:
    payload = hermes_payload_from_defaults(hermes)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_deploy_payload(value: str) -> Dict[str, Any]:
    if not value:
        return {"schema": PAYLOAD_SCHEMA}
    try:
        data = json.loads(base64.b64decode(value.encode("ascii")).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("invalid Hermes config payload") from exc
    if not isinstance(data, dict):
        raise ValidationError("Hermes config payload must decode to an object")
    return data


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mac.hermes_config_surface")
    sub = parser.add_subparsers(dest="command", required=True)
    enc = sub.add_parser("encode")
    enc.add_argument("path")
    app = sub.add_parser("apply")
    app.add_argument("--payload-b64", required=True)
    app.add_argument("--hermes-home", default="")
    ns = parser.parse_args(argv)
    if ns.command == "encode":
        data = _read_yaml_mapping(Path(ns.path))
        print(encode_deploy_payload(data))
        return 0
    if ns.command == "apply":
        payload = decode_deploy_payload(ns.payload_b64)
        result = apply_hermes_surface_payload(
            payload,
            target_home=Path(ns.hermes_home).expanduser() if ns.hermes_home else None,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
