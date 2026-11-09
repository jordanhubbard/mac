"""Tests for world_presence.py — agent presence in the shared world."""

from __future__ import annotations

import pytest
from mac.world_presence import (
    presence_checkin,
    presence_update,
    presence_all,
    presence_get,
    presence_checkout,
    _presence_id,
)
from mac.world import world_despawn, _connect

AGENT = "bullwinkle-test-presence"


@pytest.fixture(autouse=True)
def cleanup(world_database):
    yield
    handle = _presence_id(AGENT)
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM world_events WHERE object_id=%s", (handle,))
                cur.execute("DELETE FROM world_objects WHERE id=%s", (handle,))
    finally:
        conn.close()


def test_checkin_creates_object():
    obj = presence_checkin(AGENT, status="idle", note="test run")
    assert obj.id == _presence_id(AGENT)
    assert obj.fields["status"] == "idle"
    assert obj.fields["note"] == "test run"
    assert "heartbeat_utc" in obj.fields


def test_checkin_idempotent():
    presence_checkin(AGENT, status="idle")
    obj2 = presence_checkin(AGENT, status="working", current_task="task_abc")
    assert obj2.fields["status"] == "working"
    assert obj2.fields["current_task"] == "task_abc"
    # Only one presence object should exist
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM world_objects WHERE id=%s", (_presence_id(AGENT),))
            row = cur.fetchone()
            count = list(row.values())[0] if hasattr(row, "values") else row[0]
    finally:
        conn.close()
    assert count == 1


def test_presence_update_partial():
    presence_checkin(AGENT, status="idle", note="initial")
    obj = presence_update(AGENT, status="reviewing")
    assert obj.fields["status"] == "reviewing"
    assert obj.fields["note"] == "initial"  # preserved


def test_presence_get():
    presence_checkin(AGENT, status="working")
    obj = presence_get(AGENT)
    assert obj is not None
    assert obj.fields["status"] == "working"


def test_presence_get_missing_returns_none():
    result = presence_get("agent_that_does_not_exist_xyz")
    assert result is None


def test_presence_all_includes_checked_in_agent():
    presence_checkin(AGENT, status="idle")
    all_agents = presence_all()
    ids = [o.id for o in all_agents]
    assert _presence_id(AGENT) in ids


def test_presence_checkout_marks_offline():
    presence_checkin(AGENT, status="working")
    result = presence_checkout(AGENT)
    assert result["fields"]["status"] == "offline"
    assert result["fields"]["current_task"] == ""
