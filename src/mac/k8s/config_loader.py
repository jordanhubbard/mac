from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

JsonDict = Dict[str, Any]

DEFAULT_CONFIG_PATH = "/etc/mac/config.yaml"

@dataclass
class RoleConfig:
    slug: str
    agent_id: str
    name: str
    machine_id: str
    capabilities: List[str]
    image: str
    executor: str
    attestation_key_secret: Dict[str, str]
    description: str = ""
    system_prompt: str = ""
    level: str = "ic"
    required_capabilities: List[str] = field(default_factory=list)

@dataclass
class ProjectConfig:
    name: str
    description: str = ""
    status: str = "active"
    metadata: JsonDict = field(default_factory=dict)


@dataclass
class MacConfigFile:
    mac_url: str
    dispatcher: JsonDict
    role_machines: List[JsonDict] = field(default_factory=list)
    roles: Dict[str, RoleConfig] = field(default_factory=dict)
    capability_role_aliases: Dict[str, str] = field(default_factory=dict)
    attestation_keys: Optional[JsonDict] = None
    projects: List[ProjectConfig] = field(default_factory=list)
    fleet: Optional[JsonDict] = None

    def role_definitions(self) -> List[JsonDict]:
        """Returns one ``RoleCreate``-shaped dict per role for /roles POST.

        The result feeds ``mac-k8s-bootstrap.register_role_definitions``
        which registers each role in mac's ``agent_roles`` table so the
        dispatcher's ``_agent_available_for`` role-gate can resolve a
        task's ``metadata.required_role`` against a real row.
        """
        out: List[JsonDict] = []
        for slug, role in self.roles.items():
            required = role.required_capabilities or list(role.capabilities)
            out.append({
                "slug": slug,
                "name": role.name or slug,
                "description": role.description or "auto-registered role %s" % slug,
                "system_prompt": role.system_prompt or "You are a %s." % (role.name or slug),
                "level": role.level or "ic",
                "default_capabilities": list(role.capabilities),
                "required_capabilities": required,
            })
        return out

    def role_agents(self) -> List[JsonDict]:
        return [
            {
                "agent_id": role.agent_id,
                "name": role.name,
                "machine_id": role.machine_id,
                "capabilities": list(role.capabilities),
            }
            for role in self.roles.values()
        ]

    def reviewer_role_slugs(self) -> List[str]:
        """Roles tagged as reviewers via a ``review`` capability.

        Orchestrator polls these roles' agent mailboxes for verdict
        nudges. Control-plane writes nudges into the reviewer's mailbox
        after auto-assigning a review; without this drain in K8s the
        nudges accumulate and the review never advances.
        """
        out: List[str] = []
        for slug, role in self.roles.items():
            caps = {str(c).lower() for c in role.capabilities}
            if "review" in caps or any(c.endswith("-review") or c.endswith("-reviewer") for c in caps):
                out.append(slug)
        return out

    def reviewer_agent_ids(self) -> Dict[str, str]:
        return {slug: self.roles[slug].agent_id for slug in self.reviewer_role_slugs()}

    def attestation_keys_block(self) -> Optional[JsonDict]:
        if self.attestation_keys is None:
            return None
        if not self.roles:
            return None
        roles_map = {slug: role.agent_id for slug, role in self.roles.items()}
        return {
            "namespace": self.attestation_keys["namespace"],
            "secret_name": self.attestation_keys["secret_name"],
            "roles": roles_map,
        }

    def role_images(self) -> Dict[str, str]:
        return {slug: role.image for slug, role in self.roles.items()}

    def fleet_block(self) -> Optional[JsonDict]:
        return dict(self.fleet) if self.fleet is not None else None

    def role_agent_ids(self) -> Dict[str, str]:
        return {slug: role.agent_id for slug, role in self.roles.items()}

    def role_executors(self) -> Dict[str, str]:
        return {slug: role.executor for slug, role in self.roles.items()}

    def role_attestation_key_secrets(self) -> Dict[str, Dict[str, str]]:
        return {
            slug: dict(role.attestation_key_secret)
            for slug, role in self.roles.items()
        }

def _require_str(obj: JsonDict, key: str, *, path: str) -> str:
    """Return ``obj[key]`` as a non-empty stripped string or SystemExit."""
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        raise SystemExit(
            "%s requires a non-empty string at %s" % (path, key)
        )
    return val.strip()

def _require_dict(obj: JsonDict, key: str, *, path: str) -> JsonDict:
    val = obj.get(key)
    if not isinstance(val, dict) or not val:
        raise SystemExit(
            "%s requires a non-empty object at %s" % (path, key)
        )
    return dict(val)

def _require_list(obj: JsonDict, key: str, *, path: str) -> List[Any]:
    val = obj.get(key)
    if not isinstance(val, list):
        raise SystemExit(
            "%s requires a list at %s (got %s)"
            % (path, key, type(val).__name__)
        )
    return list(val)

def _parse_role(slug: str, raw: JsonDict) -> RoleConfig:
    """Validate + coerce one ``roles.<slug>`` block."""
    if not isinstance(raw, dict):
        raise SystemExit(
            "roles.%s must be a mapping (got %s)" % (slug, type(raw).__name__)
        )
    path = "roles.%s" % slug
    capabilities = _require_list(raw, "capabilities", path=path)
    if not capabilities:
        raise SystemExit("%s.capabilities must be non-empty" % path)
    att_path = "%s.attestation_key_secret" % path
    att_raw = _require_dict(raw, "attestation_key_secret", path=path)
    att_name = _require_str(att_raw, "name", path=att_path)
    att_key = _require_str(att_raw, "key", path=att_path)
    required_caps_raw = raw.get("required_capabilities") or []
    if not isinstance(required_caps_raw, list):
        raise SystemExit(
            "%s.required_capabilities must be a list (got %s)"
            % (path, type(required_caps_raw).__name__)
        )
    return RoleConfig(
        slug=slug,
        agent_id=_require_str(raw, "agent_id", path=path),
        name=_require_str(raw, "name", path=path),
        machine_id=_require_str(raw, "machine_id", path=path),
        capabilities=[str(c) for c in capabilities],
        image=_require_str(raw, "image", path=path),
        executor=_require_str(raw, "executor", path=path),
        attestation_key_secret={"name": att_name, "key": att_key},
        description=str(raw.get("description") or ""),
        system_prompt=str(raw.get("system_prompt") or ""),
        level=str(raw.get("level") or "ic"),
        required_capabilities=[str(c) for c in required_caps_raw],
    )

def load_config_file(path: Optional[str] = None) -> MacConfigFile:
    resolved = path or os.environ.get("MAC_CONFIG_FILE") or DEFAULT_CONFIG_PATH
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    except FileNotFoundError as exc:
        raise SystemExit(
            "MAC_CONFIG_FILE %s is missing: %s" % (resolved, exc)
        ) from exc
    except OSError as exc:
        raise SystemExit(
            "MAC_CONFIG_FILE %s could not be read: %s" % (resolved, exc)
        ) from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise SystemExit(
            "MAC_CONFIG_FILE %s is not valid YAML: %s" % (resolved, exc)
        ) from exc

    if not isinstance(data, dict):
        raise SystemExit(
            "MAC_CONFIG_FILE %s must decode to a mapping (got %s)"
            % (resolved, type(data).__name__)
        )

    mac_url = _require_str(data, "mac_url", path="mac_url").rstrip("/")

    dispatcher_raw = _require_dict(data, "dispatcher", path="dispatcher")
    machine = dispatcher_raw.get("machine")
    agent = dispatcher_raw.get("agent")
    if not isinstance(machine, dict) or not isinstance(agent, dict):
        raise SystemExit(
            "dispatcher requires {machine, agent} mappings"
        )

    role_machines_raw = data.get("role_machines") or []
    if not isinstance(role_machines_raw, list):
        raise SystemExit(
            "role_machines must be a list (got %s)"
            % type(role_machines_raw).__name__
        )
    role_machines: List[JsonDict] = []
    for i, m in enumerate(role_machines_raw):
        if not isinstance(m, dict):
            raise SystemExit(
                "role_machines[%d] must be a mapping (got %s)"
                % (i, type(m).__name__)
            )
        role_machines.append(dict(m))

    roles_raw = data.get("roles") or {}
    if not isinstance(roles_raw, dict):
        raise SystemExit(
            "roles must be a mapping (got %s)" % type(roles_raw).__name__
        )
    roles: Dict[str, RoleConfig] = {}
    for slug, role_raw in roles_raw.items():
        roles[str(slug)] = _parse_role(str(slug), role_raw)

    aliases_raw = data.get("capability_role_aliases") or {}
    if not isinstance(aliases_raw, dict):
        raise SystemExit(
            "capability_role_aliases must be a mapping (got %s)"
            % type(aliases_raw).__name__
        )
    aliases = {str(k): str(v) for k, v in aliases_raw.items()}

    projects_raw = data.get("projects") or []
    if not isinstance(projects_raw, list):
        raise SystemExit(
            "projects must be a list (got %s)" % type(projects_raw).__name__
        )
    projects: List[ProjectConfig] = []
    for i, p in enumerate(projects_raw):
        if not isinstance(p, dict):
            raise SystemExit(
                "projects[%d] must be a mapping (got %s)" % (i, type(p).__name__)
            )
        name = _require_str(p, "name", path="projects[%d]" % i)
        meta_raw = p.get("metadata") or {}
        if not isinstance(meta_raw, dict):
            raise SystemExit(
                "projects[%d].metadata must be a mapping (got %s)"
                % (i, type(meta_raw).__name__)
            )
        projects.append(ProjectConfig(
            name=name,
            description=str(p.get("description") or ""),
            status=str(p.get("status") or "active"),
            metadata=dict(meta_raw),
        ))

    attestation_raw = data.get("attestation_keys")
    if attestation_raw is not None and not isinstance(attestation_raw, dict):
        raise SystemExit(
            "attestation_keys must be a mapping or omitted (got %s)"
            % type(attestation_raw).__name__
        )
    attestation: Optional[JsonDict] = None
    if attestation_raw is not None:
        att_namespace = _require_str(
            attestation_raw, "namespace", path="attestation_keys"
        )
        att_secret = _require_str(
            attestation_raw, "secret_name", path="attestation_keys"
        )
        attestation = {
            "namespace": att_namespace,
            "secret_name": att_secret,
        }

    fleet_raw = data.get("fleet")
    fleet: Optional[JsonDict] = None
    if fleet_raw is not None:
        if not isinstance(fleet_raw, dict):
            raise SystemExit(
                "fleet must be a mapping or omitted (got %s)"
                % type(fleet_raw).__name__
            )
        fleet_name = _require_str(fleet_raw, "name", path="fleet")
        fleet = {
            "name": fleet_name,
            "description": str(fleet_raw.get("description") or ""),
            "status": str(fleet_raw.get("status") or "active"),
        }

    return MacConfigFile(
        mac_url=mac_url,
        dispatcher={"machine": dict(machine), "agent": dict(agent)},
        role_machines=role_machines,
        roles=roles,
        capability_role_aliases=aliases,
        attestation_keys=attestation,
        projects=projects,
        fleet=fleet,
    )
