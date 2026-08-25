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
def _contract_schema():
    """One schema for the whole file, built once.

    swept=False because tests/conftest.py drops every created schema and closes
    every open store after each test; without it this schema would not survive
    its first case.
    """
    from mac.test_support import drop_store, ephemeral_store

    store = ephemeral_store(swept=False)
    try:
        yield store
    finally:
        drop_store(store)


@pytest.fixture
def plane(_contract_schema) -> ControlPlane:
    """A fresh, empty, initialized ``ControlPlane`` for a single case.

    This used to clone a module-scoped empty schema with SQLite's online backup
    API, because re-running the full DDL for each of ~500 parametrized cases
    dominated the file. PostgreSQL has no equivalent cheap clone of a schema,
    so each case paid a real CREATE SCHEMA plus DDL -- 164 tables and 219
    indexes, ~1.2s a case, 670 of this file's 745 seconds.

    That cost was setup, not testing, and it was the whole reason the
    in-sandbox gate could not finish: this file is in the selector's
    `always_run` set, so every task -- however small its diff -- paid it.

    So the schema is now built once for the file and emptied between cases (see
    `reset_store_data`, which measures ~30ms). The ControlPlane itself is still
    rebuilt per case, so no in-memory state crosses between them. If the reset
    cannot be performed safely the fixture falls back to the old fresh-schema
    behaviour: slow is acceptable, leaking one case's rows into the next is not.
    """
    from mac import test_support

    if not test_support.reset_store_data(_contract_schema):
        return test_support.ephemeral_control_plane(secret_key=_SECRET_KEY)
    return ControlPlane(_contract_schema, secret_key=_SECRET_KEY)


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
            parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
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
