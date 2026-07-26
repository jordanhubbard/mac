"""Central, typed access to ``MAC_*`` configuration.

The fleet reads ~350 distinct ``MAC_*`` environment variables ad hoc across many
modules — with no single catalog, no validation, and dead-named legacy
fallbacks. The starkest example the architecture review flagged:
``MAC_BEADS_BRIDGE_HUB_AGENT`` — named after the *removed* beads subsystem —
still terminated several hub-agent resolution chains, silently steering
autonomous review-advance and notifier-drain toward a var no current deployment
sets under that name.

This module is the consolidation point: typed accessors with validation, and
documented resolvers for multi-variable fallback chains so a legacy name can be
retired in ONE place instead of N. It is introduced as a foundation and adopted
incrementally; new config reads should go through here rather than calling
``os.environ.get`` inline.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _env(environ: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def env_str(name: str, default: str = "", *, environ: Optional[Mapping[str, str]] = None) -> str:
    """Return the trimmed value of ``name``, or ``default`` if unset/blank."""
    value = _env(environ).get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def env_bool(name: str, default: bool = False, *, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Parse a boolean flag. Unset/blank → ``default``; unrecognized → ``default``."""
    raw = _env(environ).get(name)
    if raw is None or not str(raw).strip():
        return default
    token = str(raw).strip().lower()
    if token in _TRUTHY:
        return True
    if token in _FALSY:
        return False
    return default


def env_int(
    name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    """Parse an int with optional clamping. Unset/blank/invalid → ``default`` (then clamped)."""
    raw = str(_env(environ).get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def env_float(
    name: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> float:
    """Parse a float with optional clamping. Unset/blank/invalid -> ``default`` (then clamped)."""
    raw = str(_env(environ).get(name) or "").strip()
    try:
        value = float(raw) if raw else float(default)
    except ValueError:
        value = float(default)
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def resolve_env_chain(
    *names: str,
    default: str = "",
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """First non-empty value among ``names`` (in priority order), else ``default``.

    Centralizes multi-variable fallback chains so a dead-named legacy variable
    can be retired in one place.
    """
    env = _env(environ)
    for name in names:
        value = env.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def resolve_hub_agent(
    *names: str,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Resolve which agent acts as the hub for a heartbeat-driven side effect.

    Callers pass the applicable variables in priority order (e.g.
    ``MAC_NOTIFIER_DRAIN_HUB_AGENT`` then ``MAC_REVIEW_TICK_HUB_AGENT``, or
    ``MAC_SHARED_SERVICES_MANAGER_AGENT``). ``MAC_BEADS_BRIDGE_HUB_AGENT`` — the
    removed beads subsystem's name — is deliberately NOT consulted; set one of
    the current variables instead.
    """
    return resolve_env_chain(*names, environ=environ)


@dataclass(frozen=True)
class EnvVar:
    """Typed metadata and access for one catalogued environment variable.

    Catalog loading never reads secret values. Calling ``get`` performs the
    environment lookup and applies either the caller-supplied default or the
    catalog default. A ``None`` default preserves the distinction between an
    unset value and a false/zero value.
    """

    name: str
    kind: str
    default: Any
    family: str
    description: str
    retired: bool = False
    sources: tuple[str, ...] = ()

    def get(
        self,
        default: Any = None,
        *,
        environ: Optional[Mapping[str, str]] = None,
    ) -> Any:
        if self.retired:
            return default if default is not None else self.default
        env = _env(environ)
        raw = env.get(self.name)
        effective_default = self.default if default is None else default
        if raw is None or not str(raw).strip():
            return effective_default
        if self.kind == "bool":
            return env_bool(
                self.name,
                bool(effective_default) if effective_default is not None else False,
                environ=env,
            )
        if self.kind == "int":
            return env_int(
                self.name,
                int(effective_default) if effective_default is not None else 0,
                environ=env,
            )
        return env_str(
            self.name,
            str(effective_default) if effective_default is not None else "",
            environ=env,
        )

    __call__ = get


def _load_registry() -> Dict[str, EnvVar]:
    path = Path(__file__).with_name("data") / "env_config_registry.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    registry: Dict[str, EnvVar] = {}
    for record in records:
        item = EnvVar(
            name=str(record["name"]),
            kind=str(record["type"]),
            default=record.get("default"),
            family=str(record["family"]),
            description=str(record["description"]),
            retired=bool(record.get("retired")),
            sources=tuple(str(value) for value in record.get("sources") or []),
        )
        registry[item.name] = item
    return registry


ENV_VARS = _load_registry()
globals().update(ENV_VARS)


def environment_catalog(*, include_retired: bool = True) -> list[EnvVar]:
    """Return the stable, name-sorted registry for API/docs/tooling consumers."""
    return [
        item
        for item in ENV_VARS.values()
        if include_retired or not item.retired
    ]


__all__ = [
    "ENV_VARS",
    "EnvVar",
    "environment_catalog",
    "env_str",
    "env_bool",
    "env_int",
    "env_float",
    "resolve_env_chain",
    "resolve_hub_agent",
] + sorted(ENV_VARS)
