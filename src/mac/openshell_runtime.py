"""Shared OpenShell runtime identity helpers."""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping, Optional


DEFAULT_REQUIRED_AGENT_NAMES = frozenset()  # de-personalized snapshot: set required agents via MAC_OPENSHELL_REQUIRED or per-agent resources, not hardcoded names
TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUTHY_VALUES


def base_agent_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("agent_"):
        text = text[len("agent_") :]
    return text.split(".", 1)[0]


def _boolish(value: Any) -> bool:
    if isinstance(value, str):
        return truthy(value)
    return bool(value)


def openshell_required_for_identity(
    *,
    agent_id: Any = None,
    agent_name: Any = None,
    host: Any = None,
    resources: Optional[Mapping[str, Any]] = None,
    explicit: Any = None,
    required_agent_names: Iterable[str] = DEFAULT_REQUIRED_AGENT_NAMES,
) -> bool:
    if explicit is not None:
        return truthy(explicit)
    data = resources or {}
    raw = data.get("openshell_required")
    if raw is not None:
        return _boolish(raw)
    names = {
        agent_id,
        agent_name,
        host,
        data.get("hostname"),
        data.get("host"),
    }
    required = {base_agent_name(name) for name in required_agent_names}
    return any(base_agent_name(name) in required for name in names if name)


def openshell_required_for_local_agent(
    environ: Optional[Mapping[str, str]] = None,
    *,
    fallback_name: Optional[str] = None,
) -> bool:
    env = os.environ if environ is None else environ
    if "MAC_OPENSHELL_REQUIRED" in env:
        return truthy(env.get("MAC_OPENSHELL_REQUIRED"))
    name = (
        env.get("MAC_AGENT_ID")
        or env.get("MAC_WORKER_AGENT_ID")
        or env.get("MAC_WORKER_AGENT_NAME")
        or fallback_name
        or os.uname().nodename
    )
    return openshell_required_for_identity(agent_id=name)
