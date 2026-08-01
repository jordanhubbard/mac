"""Repository-wide public API error-contract coverage for ``ControlPlane``.

Every explicitly-typed public method must be safe to call against an empty,
initialized control plane: it may return a value or reject the request with a
domain ``MACError``, but it must not leak storage, parsing, or implementation
exceptions.  This catches newly-added methods that forget their boundary
validation while exercising the complete public facade.
"""

from __future__ import annotations

import inspect
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from mac.models import MACError
from mac.services import ControlPlane
from mac.test_support import ephemeral_store


_SECRET_KEY = "test-key-with-enough-entropy-32+chars"


@pytest.fixture(scope="module")
def _schema_snapshot() -> sqlite3.Connection:
    """One-time empty-schema snapshot cloned to build each case's plane.

    Constructing ``ephemeral_store()`` runs the full ``CREATE TABLE`` and
    migration suite (~110ms), which dominated this file's runtime once every one
    of ~500 parametrized cases paid it.  SQLite's online backup API clones that
    freshly-migrated, empty schema in well under a millisecond, so each case
    still starts from an independent, byte-identical empty schema.
    """
    template = ephemeral_store()
    snapshot = sqlite3.connect(":memory:")
    template._conn.backup(snapshot)
    template.close()
    return snapshot


@pytest.fixture
def plane(_schema_snapshot: sqlite3.Connection) -> ControlPlane:
    """A fresh, empty, initialized ``ControlPlane`` for a single case.

    Behaviourally equivalent to ``ControlPlane.in_memory()``: a brand-new store
    and plane per case (no state shared between cases) running the identical
    constructor and default-seeding path.  The empty schema is cloned from the
    module snapshot instead of re-running every DDL/migration statement.
    """
    store = Store(":memory:", initialize_schema=False)
    _schema_snapshot.backup(store._conn)
    return ControlPlane(store, secret_key=_SECRET_KEY)


_NAMED_VALUES: dict[str, Any] = {
    "now": "2026-01-01T00:00:00+00:00",
    "as_of": "2026-01-01T00:00:00+00:00",
    "target_state": "open",
    "to_state": "open",
    "phase": "test",
    "state": "open",
    "status": "active",
    "health_status": "healthy",
    "source": "github",
    "source_kind": "github",
    "authority": "github",
    "finding_type": "test",
    "severity": "warning",
    "event_type": "test.event",
    "subject_type": "task",
    "record_class": "observability_events",
    "repository_url": "https://github.com/example/repository.git",
    "repo_path": ".",
    "path": ".",
    "url": "https://example.invalid",
    "target": "git://main",
    "channel_type": "slack",
    "kind": "log",
    "operation": "upsert",
    "package_manager": "pip",
    "action": "pause",
    "tier": "medium",
    "scope": "read",
    "signature": "invalid-test-signature",
    "challenge": {},
    "proof": {},
    "payload": {},
    "installed_packages": {},
    "argv": ["true"],
    "children": [],
    "ops": [],
    "willing_ops": [],
    "checks": {},
    "scopes": {},
    "manifest": {},
    "commands": [],
    "added_dependencies": [],
    "required_capabilities": [],
    "dependencies": [],
    "agent_ids": [],
    "channels": [],
    "recipient_agent_ids": [],
    "resources": {},
    "labels": {},
    "hardware": {},
    "metadata": {},
    "detail": {},
    "vector_writer": SimpleNamespace(recall=lambda *_args, **_kwargs: []),
    "min_score": 0.0,
}


def _sample(name: str) -> Any:
    if name in _NAMED_VALUES:
        value = _NAMED_VALUES[name]
        if isinstance(value, (dict, list)):
            return value.copy()
        return value
    if name in {
        "limit",
        "priority",
        "max_attempts",
        "lease_seconds",
        "stale_after_seconds",
        "poll_interval_seconds",
        "max_entries",
        "grace_seconds",
    }:
        return 1
    if name in {
        "paused",
        "force",
        "trusted",
        "notify",
        "write",
        "dry_run",
        "all_agents",
        "restart",
    }:
        return False
    return "sample"


def _explicit_public_methods() -> list[tuple[str, Any]]:
    result = []
    for name, method in inspect.getmembers(ControlPlane, inspect.isfunction):
        if name.startswith("_") or name == "in_memory":
            continue
        parameters = list(inspect.signature(method).parameters.values())[1:]
        if any(
            parameter.kind
            in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
            for parameter in parameters
        ):
            continue
        result.append((name, method))
    return result


_METHODS = _explicit_public_methods()


@pytest.mark.parametrize("name,method", _METHODS, ids=[name for name, _ in _METHODS])
def test_control_plane_public_methods_return_or_raise_domain_error(
    name: str,
    method: Any,
    plane: ControlPlane,
) -> None:
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for parameter in list(inspect.signature(method).parameters.values())[1:]:
        if parameter.default is not inspect.Parameter.empty:
            continue
        value = _sample(parameter.name)
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            args.append(value)
        else:
            kwargs[parameter.name] = value

    try:
        getattr(plane, name)(*args, **kwargs)
    except MACError:
        pass


@pytest.mark.parametrize("name,method", _METHODS, ids=[name for name, _ in _METHODS])
def test_control_plane_public_methods_accept_or_reject_complete_requests(
    name: str,
    method: Any,
    plane: ControlPlane,
) -> None:
    """Exercise each facade method with every optional field populated.

    The required-only contract above catches missing boundary validation.  This
    companion catches optional-field branches that otherwise silently rot as
    the facade grows.
    """

    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for parameter in list(inspect.signature(method).parameters.values())[1:]:
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        value = _sample(parameter.name)
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            args.append(value)
        else:
            kwargs[parameter.name] = value

    try:
        getattr(plane, name)(*args, **kwargs)
    except MACError:
        pass
