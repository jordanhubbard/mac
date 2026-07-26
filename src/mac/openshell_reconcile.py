"""OpenShell fleet deployment reconciliation helpers."""

from __future__ import annotations

from pathlib import Path

from mac import mac_paths
from typing import Any, Dict, Iterable, List, Optional, Sequence

import yaml

from mac.models import utcnow
from mac.openshell_service import policy_checksum


DEFAULT_POLICY_NAME = "mac-docker-engine-moby"
DEFAULT_RUNTIME = "docker-engine-moby"
DEFAULT_GATEWAY_DRIVER = "docker"
DEFAULT_IMAGE = "localhost/mac-hermes:net"
DEFAULT_OPENSHELL_VERSION = "0.0.72"
VALID_STATUSES = {"active", "starting", "inactive", "degraded", "failed", "unknown"}


def default_policy_path() -> Path:
    """Return the default path to the OpenShell policy file."""
    return Path(__file__).resolve().parents[2] / "deploy" / "openshell" / "mac-hermes-policy.yaml"


def default_fleets_path() -> Path:
    """Return the default path to the fleets configuration file."""
    return mac_paths.fleets_config()


def _to_dict(value: Any) -> Dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return value
    raise TypeError("expected dict-like value, got %s" % type(value).__name__)


def _list_dicts(values: Iterable[Any]) -> List[Dict[str, Any]]:
    return [_to_dict(value) for value in values]


def _policy_metadata() -> Dict[str, Any]:
    return {
        "runtime": DEFAULT_RUNTIME,
        "source": "deploy/openshell/mac-hermes-policy.yaml",
    }


def load_fleet_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load and validate the fleet configuration from the given or default path."""
    cfg_path = path or default_fleets_path()
    if not cfg_path.is_file():
        raise FileNotFoundError("fleet config not found: %s" % cfg_path)
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("fleet config must be a YAML object: %s" % cfg_path)
    return data


def default_fleet_name(config: Dict[str, Any]) -> str:
    """Return the name of the default fleet from the configuration."""
    fleets = config.get("fleets") or {}
    if not isinstance(fleets, dict) or not fleets:
        raise ValueError("fleet config does not define any fleets")
    marked = [
        name
        for name, entry in fleets.items()
        if isinstance(entry, dict) and entry.get("default") is True
    ]
    if len(marked) == 1:
        return str(marked[0])
    if not marked and len(fleets) == 1:
        return str(next(iter(fleets)))
    raise ValueError("multiple fleets are configured; pass --target-fleet")


def fleet_agent_names(config: Dict[str, Any], fleet: Optional[str] = None) -> List[str]:
    """Return the enabled Linux agent names for the given or default fleet."""
    fleet_name = fleet or default_fleet_name(config)
    fleets = config.get("fleets") or {}
    entry = fleets.get(fleet_name)
    if not isinstance(entry, dict):
        raise ValueError("fleet not found in config: %s" % fleet_name)
    names: List[str] = []
    for agent in entry.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        if agent.get("enabled", True) is False:
            continue
        if str(agent.get("os") or "linux").lower() != "linux":
            continue
        name = str(agent.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _select_agents(
    agents: Sequence[Dict[str, Any]],
    selectors: Sequence[str],
    *,
    allow_missing: bool = False,
) -> tuple[List[Dict[str, Any]], List[str]]:
    by_id = {str(agent.get("id")): agent for agent in agents if agent.get("id")}
    by_name: Dict[str, Dict[str, Any]] = {}
    for agent in agents:
        name = str(agent.get("name") or "")
        if name:
            by_name.setdefault(name, agent)
    selected: List[Dict[str, Any]] = []
    missing: List[str] = []
    seen: set[str] = set()
    for selector in selectors:
        key = str(selector or "").strip()
        if not key:
            continue
        agent = by_id.get(key) or by_name.get(key)
        if agent is None:
            missing.append(key)
            continue
        agent_id = str(agent.get("id"))
        if agent_id not in seen:
            selected.append(agent)
            seen.add(agent_id)
    if missing and not allow_missing:
        raise ValueError("agents not found in hub registry: %s" % ", ".join(missing))
    return selected, missing


def _policy_action(
    existing: Optional[Dict[str, Any]],
    *,
    target_checksum: str,
) -> str:
    if existing is None:
        return "create"
    if existing.get("checksum") != target_checksum:
        return "update"
    return "reuse"


def _ensure_policy(
    plane: Any,
    *,
    policy_name: str,
    policy_text: str,
    actor: str,
    apply: bool,
) -> Dict[str, Any]:
    text_value = str(policy_text or "").strip()
    target_checksum = policy_checksum(text_value)
    policies = _list_dicts(plane.list_openshell_policies(include_deleted=False))
    existing = next((policy for policy in policies if policy.get("name") == policy_name), None)
    action = _policy_action(existing, target_checksum=target_checksum)
    if apply and action == "create":
        existing = _to_dict(
            plane.create_openshell_policy(
                policy_name,
                text_value,
                description="MAC standard OpenShell policy for Docker Engine/Moby fleet runtime",
                metadata=_policy_metadata(),
                created_by=actor,
            )
        )
    elif apply and action == "update" and existing is not None:
        existing = _to_dict(
            plane.update_openshell_policy(
                existing["id"],
                policy_text=text_value,
                metadata=_policy_metadata(),
                updated_by=actor,
            )
        )
    elif action == "update" and existing is not None:
        existing = dict(existing)
        existing["version"] = int(existing.get("version") or 0) + 1
        existing["checksum"] = target_checksum
    policy = existing or {
        "id": None,
        "name": policy_name,
        "version": None,
        "checksum": target_checksum,
    }
    return {
        "action": action,
        "id": policy.get("id"),
        "name": policy.get("name"),
        "version": policy.get("version"),
        "checksum": policy.get("checksum") or target_checksum,
    }


def _validation_detail(
    *,
    detail: Optional[Dict[str, Any]],
    runtime: str,
    openshell_version: str,
    gateway_driver: str,
    image: str,
    validation_summary: str,
    validated: bool,
) -> Dict[str, Any]:
    out = dict(detail or {})
    out.setdefault("runtime", runtime)
    out.setdefault("openshell_version", openshell_version)
    out.setdefault("gateway_driver", gateway_driver)
    out.setdefault("image", image)
    out.setdefault(
        "validation",
        validation_summary if validation_summary else ("validated" if validated else "not validated"),
    )
    out.setdefault("reported_at_by_admin", utcnow())
    return out


def reconcile_openshell_agents(
    plane: Any,
    *,
    agent_selectors: Sequence[str],
    policy_name: str = DEFAULT_POLICY_NAME,
    policy_text: Optional[str] = None,
    apply: bool = False,
    actor: str = "human",
    status: str = "active",
    validated: bool = False,
    sandbox_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    runtime: str = DEFAULT_RUNTIME,
    openshell_version: str = DEFAULT_OPENSHELL_VERSION,
    gateway_driver: str = DEFAULT_GATEWAY_DRIVER,
    image: str = DEFAULT_IMAGE,
    validation_summary: str = "",
    report_status: bool = True,
    allow_missing_agents: bool = False,
) -> Dict[str, Any]:
    """Reconcile OpenShell required/policy/status state for registered agents.

    ``plane`` is intentionally dispatch-shaped: either ``ControlPlane`` or the
    CLI's HTTP ``RemoteDispatch`` works. Mutations are disabled unless
    ``apply=True`` so operators can inspect the proposed changes first.
    """

    status_value = str(status or "").strip().lower()
    if status_value not in VALID_STATUSES:
        raise ValueError("unsupported OpenShell status: %s" % status)
    if apply and report_status and status_value == "active" and not validated:
        raise ValueError("--validated is required before reporting active OpenShell deployment")
    if not agent_selectors:
        raise ValueError("no agents selected for OpenShell reconciliation")

    text = policy_text if policy_text is not None else default_policy_path().read_text(encoding="utf-8")
    agents, missing_agents = _select_agents(
        _list_dicts(plane.list_agents()),
        agent_selectors,
        allow_missing=allow_missing_agents,
    )
    policy = _ensure_policy(
        plane,
        policy_name=policy_name,
        policy_text=text,
        actor=actor,
        apply=apply,
    )
    policy_id = policy.get("id")
    policy_version = policy.get("version")
    policy_checksum_value = policy.get("checksum")
    status_detail = _validation_detail(
        detail=detail,
        runtime=runtime,
        openshell_version=openshell_version,
        gateway_driver=gateway_driver,
        image=image,
        validation_summary=validation_summary,
        validated=validated,
    )

    rows: List[Dict[str, Any]] = []
    for agent in agents:
        agent_id = str(agent["id"])
        resources = dict(agent.get("resources") or {})
        before_required = resources.get("openshell_required")
        resources["openshell_required"] = True
        actions: List[str] = []
        if before_required is not True:
            actions.append("set_resources.openshell_required")
            if apply:
                updated = _to_dict(
                    plane.update_agent(
                        agent_id,
                        resources=resources,
                        actor=actor,
                    )
                )
                agent = updated
        current_status = _to_dict(plane.get_openshell_status(agent_id))
        current_assignment = current_status.get("assignment") or {}
        assignment_current = (
            policy_id is not None
            and current_assignment.get("policy_id") == policy_id
            and current_assignment.get("policy_version") == policy_version
            and bool(current_assignment.get("active", True))
        )
        if not assignment_current:
            actions.append("assign_policy")
            if apply and policy_id:
                plane.assign_openshell_policy(
                    policy_id,
                    target_type="agent",
                    target_id=agent_id,
                    created_by=actor,
                )
        if report_status:
            actions.append("report_status")
            if apply and policy_id:
                plane.report_openshell_status(
                    agent_id,
                    status=status_value,
                    required=True,
                    active=status_value in {"active", "starting", "degraded"},
                    sandbox_id=sandbox_id,
                    policy_id=policy_id,
                    policy_version=policy_version,
                    checksum=policy_checksum_value,
                    detail=status_detail,
                )
        after_status = _to_dict(plane.get_openshell_status(agent_id)) if apply else None
        rows.append(
            {
                "agent_id": agent_id,
                "agent_name": agent.get("name"),
                "before": {
                    "openshell_required": before_required,
                    "effective": current_status.get("effective"),
                },
                "after": {
                    "openshell_required": True,
                    "effective": after_status.get("effective") if after_status else None,
                },
                "actions": actions,
            }
        )

    counts = {
        "agents": len(rows),
        "resource_updates": sum("set_resources.openshell_required" in row["actions"] for row in rows),
        "policy_assignments": sum("assign_policy" in row["actions"] for row in rows),
        "status_reports": sum("report_status" in row["actions"] for row in rows),
    }
    return {
        "schema": "mac.openshell.reconcile.v1",
        "dry_run": not apply,
        "validated": validated,
        "status": status_value,
        "policy": policy,
        "agents": rows,
        "missing_agents": missing_agents,
        "counts": counts,
        "detail": status_detail,
    }
