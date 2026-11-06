"""Tests for DeliberationService.

Runs against an in-memory SQLite store created from the migration SQL.
No external services required.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from mac.deliberation_service import DeliberationService
from mac.models import AuthorizationError, JsonDict, NotFoundError, ValidationError

# ---------------------------------------------------------------------------
# Minimal in-memory store adapter for testing
# ---------------------------------------------------------------------------


class _SqliteStore:
    """Thin sqlite3 wrapper that speaks the store interface DeliberationService needs."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._conn.execute(sql, params or {})
        self._conn.commit()

    def fetchone(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[sqlite3.Row]:
        return self._conn.execute(sql, params or {}).fetchone()

    def fetchall(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[sqlite3.Row]:
        return self._conn.execute(sql, params or {}).fetchall()


@pytest.fixture()
def store() -> _SqliteStore:
    migration_path = Path(__file__).parent.parent / "migrations" / "0014_deliberations.sql"
    conn = sqlite3.connect(":memory:")
    conn.executescript(migration_path.read_text())
    return _SqliteStore(conn)


@pytest.fixture()
def svc(store: _SqliteStore) -> DeliberationService:
    return DeliberationService(store)


# ---------------------------------------------------------------------------
# Tests: create
# ---------------------------------------------------------------------------


def test_create_basic(svc: DeliberationService) -> None:
    d = svc.create_deliberation(
        topic="Which migration pattern?",
        created_by="agent_natasha",
    )
    assert d["id"].startswith("deliberation_")
    assert d["topic"] == "Which migration pattern?"
    assert d["status"] == "open"
    assert d["scope"] == "design"
    assert d["authorized_resolvers"] == []
    assert d["participants"] == []
    assert d["arguments"] == []


def test_create_with_authorized_resolvers(svc: DeliberationService) -> None:
    d = svc.create_deliberation(
        topic="Architecture conflict",
        created_by="agent_natasha",
        authorized_resolvers=["agent_rocky"],
        task_id="task_abc",
    )
    assert d["authorized_resolvers"] == ["agent_rocky"]
    assert d["task_id"] == "task_abc"


def test_create_invalid_scope(svc: DeliberationService) -> None:
    with pytest.raises(ValidationError, match="scope"):
        svc.create_deliberation(topic="T", created_by="agent_natasha", scope="bad")


def test_create_missing_topic(svc: DeliberationService) -> None:
    with pytest.raises(ValidationError, match="topic"):
        svc.create_deliberation(topic="", created_by="agent_natasha")


# ---------------------------------------------------------------------------
# Tests: participants
# ---------------------------------------------------------------------------


def test_add_participant(svc: DeliberationService) -> None:
    d = svc.create_deliberation(topic="T", created_by="agent_natasha")
    d2 = svc.add_participant(d["id"], agent_id="agent_rocky", initial_position="support")
    assert len(d2["participants"]) == 1
    p = d2["participants"][0]
    assert p["agent_id"] == "agent_rocky"
    assert p["initial_position"] == "support"
    assert p["final_position"] == "support"
    assert p["moved_by"] is None


def test_add_duplicate_participant(svc: DeliberationService) -> None:
    d = svc.create_deliberation(topic="T", created_by="agent_natasha")
    svc.add_participant(d["id"], agent_id="agent_rocky")
    with pytest.raises(ValidationError, match="already a participant"):
        svc.add_participant(d["id"], agent_id="agent_rocky")


def test_add_participant_invalid_position(svc: DeliberationService) -> None:
    d = svc.create_deliberation(topic="T", created_by="agent_natasha")
    with pytest.raises(ValidationError, match="position"):
        svc.add_participant(d["id"], agent_id="agent_rocky", initial_position="maybe")


# ---------------------------------------------------------------------------
# Tests: arguments
# ---------------------------------------------------------------------------


def test_add_argument(svc: DeliberationService) -> None:
    d = svc.create_deliberation(topic="T", created_by="agent_natasha")
    svc.add_participant(d["id"], agent_id="agent_natasha")
    d2 = svc.add_argument(
        d["id"],
        agent_id="agent_natasha",
        claim="JSONB is better than flat JSON",
        argument_type="empirical",
    )
    assert len(d2["arguments"]) == 1
    a = d2["arguments"][0]
    assert a["id"].startswith("arg_")
    assert a["claim"] == "JSONB is better than flat JSON"
    assert a["argument_type"] == "empirical"


def test_add_argument_invalid_type(svc: DeliberationService) -> None:
    d = svc.create_deliberation(topic="T", created_by="agent_natasha")
    with pytest.raises(ValidationError, match="argument_type"):
        svc.add_argument(d["id"], agent_id="agent_natasha", claim="X", argument_type="opinion")


# ---------------------------------------------------------------------------
# Tests: position update with moved_by
# ---------------------------------------------------------------------------


def test_update_position_with_moved_by(svc: DeliberationService) -> None:
    d = svc.create_deliberation(topic="T", created_by="agent_natasha")
    svc.add_participant(d["id"], agent_id="agent_rocky", initial_position="oppose")
    d2 = svc.add_argument(
        d["id"], agent_id="agent_natasha", claim="Prior precedent X", argument_type="precedent"
    )
    arg_id = d2["arguments"][0]["id"]
    d3 = svc.update_position(
        d["id"], agent_id="agent_rocky", new_position="support", moved_by=arg_id
    )
    p = d3["participants"][0]
    assert p["final_position"] == "support"
    assert p["moved_by"] == arg_id
    assert p["initial_position"] == "oppose"  # preserved


def test_update_position_invalid_argument(svc: DeliberationService) -> None:
    d = svc.create_deliberation(topic="T", created_by="agent_natasha")
    svc.add_participant(d["id"], agent_id="agent_natasha")
    with pytest.raises(ValidationError, match="not found"):
        svc.update_position(
            d["id"], agent_id="agent_natasha", new_position="support", moved_by="arg_nonexistent"
        )


# ---------------------------------------------------------------------------
# Tests: resolution — authority path
# ---------------------------------------------------------------------------


def test_resolve_authority_path(svc: DeliberationService) -> None:
    d = svc.create_deliberation(
        topic="Architecture conflict",
        created_by="agent_natasha",
        authorized_resolvers=["agent_rocky"],
    )
    resolved = svc.resolve_deliberation(d["id"], conclusion="Use approach A", actor="agent_rocky")
    assert resolved["status"] == "resolved"
    assert resolved["resolution_path"] == "authority"
    assert resolved["conclusion"] == "Use approach A"
    assert resolved["resolved_by"] == "agent_rocky"


def test_resolve_authority_denied(svc: DeliberationService) -> None:
    d = svc.create_deliberation(
        topic="T",
        created_by="agent_natasha",
        authorized_resolvers=["agent_rocky"],
    )
    # natasha is NOT in authorized_resolvers, and there are no participants
    with pytest.raises(AuthorizationError, match="not in authorized_resolvers"):
        svc.resolve_deliberation(d["id"], conclusion="X", actor="agent_natasha")


# ---------------------------------------------------------------------------
# Tests: resolution — unanimity path
# ---------------------------------------------------------------------------


def test_resolve_unanimity_path(svc: DeliberationService) -> None:
    d = svc.create_deliberation(topic="T", created_by="agent_natasha")
    svc.add_participant(d["id"], agent_id="agent_natasha", initial_position="support")
    svc.add_participant(d["id"], agent_id="agent_bullwinkle", initial_position="oppose")
    svc.update_position(d["id"], agent_id="agent_bullwinkle", new_position="support")
    resolved = svc.resolve_deliberation(d["id"], conclusion="Use approach B", actor="agent_natasha")
    assert resolved["status"] == "resolved"
    assert resolved["resolution_path"] == "unanimity"


def test_resolve_unanimity_fails_when_diverged(svc: DeliberationService) -> None:
    d = svc.create_deliberation(topic="T", created_by="agent_natasha")
    svc.add_participant(d["id"], agent_id="agent_natasha", initial_position="support")
    svc.add_participant(d["id"], agent_id="agent_bullwinkle", initial_position="oppose")
    with pytest.raises(AuthorizationError):
        svc.resolve_deliberation(d["id"], conclusion="X", actor="agent_natasha")


# ---------------------------------------------------------------------------
# Tests: withdraw + re-resolve guard
# ---------------------------------------------------------------------------


def test_withdraw(svc: DeliberationService) -> None:
    d = svc.create_deliberation(topic="T", created_by="agent_natasha")
    w = svc.withdraw_deliberation(d["id"], actor="agent_natasha")
    assert w["status"] == "withdrawn"


def test_cannot_resolve_withdrawn(svc: DeliberationService) -> None:
    d = svc.create_deliberation(
        topic="T", created_by="agent_natasha", authorized_resolvers=["agent_rocky"]
    )
    svc.withdraw_deliberation(d["id"], actor="agent_rocky")
    with pytest.raises(ValidationError, match="already withdrawn"):
        svc.resolve_deliberation(d["id"], conclusion="X", actor="agent_rocky")


# ---------------------------------------------------------------------------
# Tests: list / filter
# ---------------------------------------------------------------------------


def test_list_by_status(svc: DeliberationService) -> None:
    d1 = svc.create_deliberation(topic="T1", created_by="agent_natasha")
    d2 = svc.create_deliberation(
        topic="T2", created_by="agent_natasha", authorized_resolvers=["agent_natasha"]
    )
    svc.resolve_deliberation(d2["id"], conclusion="Done", actor="agent_natasha")
    open_items = svc.list_deliberations(status="open")
    resolved_items = svc.list_deliberations(status="resolved")
    assert len(open_items) == 1
    assert open_items[0]["id"] == d1["id"]
    assert len(resolved_items) == 1
    assert resolved_items[0]["id"] == d2["id"]


def test_list_by_task_id(svc: DeliberationService) -> None:
    svc.create_deliberation(topic="T1", created_by="agent_natasha", task_id="task_A")
    svc.create_deliberation(topic="T2", created_by="agent_natasha", task_id="task_B")
    result = svc.list_deliberations(task_id="task_A")
    assert len(result) == 1
    assert result[0]["task_id"] == "task_A"


# ---------------------------------------------------------------------------
# Tests: not found
# ---------------------------------------------------------------------------


def test_get_not_found(svc: DeliberationService) -> None:
    with pytest.raises(NotFoundError):
        svc.get_deliberation("deliberation_nonexistent")


# ---------------------------------------------------------------------------
# Tests: on_resolve hook
# ---------------------------------------------------------------------------


def test_resolve_calls_on_resolve_hook(store: "_SqliteStore") -> None:
    """on_resolve callback receives the resolved deliberation dict."""
    received: list[JsonDict] = []
    svc = DeliberationService(store, on_resolve=received.append)
    d = svc.create_deliberation(
        topic="Hook test",
        created_by="agent_natasha",
        authorized_resolvers=["agent_natasha"],
    )
    result = svc.resolve_deliberation(d["id"], conclusion="Done", actor="agent_natasha")
    assert len(received) == 1
    assert received[0]["id"] == result["id"]
    assert received[0]["status"] == "resolved"


def test_resolve_hook_failure_does_not_abort_resolution(store: "_SqliteStore") -> None:
    """A crashing on_resolve hook must not propagate; resolution must succeed."""

    def boom(_d: JsonDict) -> None:
        raise RuntimeError("simulated embedding failure")

    svc = DeliberationService(store, on_resolve=boom)
    d = svc.create_deliberation(
        topic="Hook error test",
        created_by="agent_natasha",
        authorized_resolvers=["agent_natasha"],
    )
    # Must not raise:
    result = svc.resolve_deliberation(d["id"], conclusion="Done", actor="agent_natasha")
    assert result["status"] == "resolved"


def test_resolve_no_hook_is_default(svc: DeliberationService) -> None:
    """Default DeliberationService has no hook; resolve works normally."""
    d = svc.create_deliberation(
        topic="No hook",
        created_by="agent_natasha",
        authorized_resolvers=["agent_natasha"],
    )
    result = svc.resolve_deliberation(d["id"], conclusion="Fine", actor="agent_natasha")
    assert result["status"] == "resolved"
