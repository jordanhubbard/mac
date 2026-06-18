"""Shared OpenShell runtime identity helpers."""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping, MutableMapping, Optional


# Empty by default: sandbox required-ness is DATA-DRIVEN (the agent's runtime
# ``resources["openshell_required"]``, an explicit override, or the
# ``MAC_OPENSHELL_REQUIRED`` env), never a hardcoded fleet roster baked into
# source that goes stale as the fleet changes. Callers may still pass an
# explicit ``required_agent_names`` set for a name-match fallback, but the
# default matches nothing.
DEFAULT_REQUIRED_AGENT_NAMES: frozenset[str] = frozenset()
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


def apply_openshell_requirement(
    resources: Optional[Mapping[str, Any]],
    environ: MutableMapping[str, str],
) -> Optional[bool]:
    """Stamp ``MAC_OPENSHELL_REQUIRED`` into ``environ`` from an agent's runtime
    ``resources`` so a DB-driven sandbox requirement reaches the local executor
    (which inherits the worker process environment) WITHOUT a hardcoded agent
    list. This is the data-driven channel that replaces the old name allowlist:
    the hub owns ``resources["openshell_required"]`` per agent, the worker reads
    its own record back at registration, and this function projects that fact
    into the env the executor reads via :func:`openshell_required_for_local_agent`.

    Precedence: an existing ``MAC_OPENSHELL_REQUIRED`` (operator/deploy override)
    always wins and is left untouched. Otherwise, if ``resources`` carries an
    explicit ``openshell_required`` flag it is written as ``"1"``/``"0"``. A
    missing flag is a no-op — the executor keeps its own default — so this never
    silently flips an unconfigured agent. Returns the bool applied, or ``None``
    when nothing was written.
    """
    if "MAC_OPENSHELL_REQUIRED" in environ:
        return None
    raw = (resources or {}).get("openshell_required")
    if raw is None:
        return None
    value = _boolish(raw)
    environ["MAC_OPENSHELL_REQUIRED"] = "1" if value else "0"
    return value
