"""The hub must self-trim high-volume telemetry so mac.db can never again grow
unbounded (the 16GB action_events firehose that wedged the control-plane).

RetentionService is preserve-by-default; these tests pin the wiring that turns
it on for the two disposable telemetry classes and drains them from the tick,
while leaving the task ledger untouched.
"""
from __future__ import annotations

import os

import pytest

from mac.retention_service import RECORD_CLASS_CONFIG
from mac.services import ControlPlane


@pytest.fixture
def cp():
    return ControlPlane.in_memory()


def _enabled(cp):
    return {p["record_class"]: p for p in cp.retention_list_policies() if p["enabled"]}


def test_default_policies_enabled_for_telemetry_only(cp):
    enabled = _enabled(cp)
    assert set(enabled) == {"action_events", "observability_events"}
    # ledger classes must stay preserve-by-default
    for ledger in ("evidence_artifacts", "command_audit", "operator_notifications"):
        assert ledger in RECORD_CLASS_CONFIG
        assert ledger not in enabled


def test_window_and_batch_are_env_tunable(monkeypatch):
    monkeypatch.setenv("MAC_RETENTION_TELEMETRY_DAYS", "3")
    monkeypatch.setenv("MAC_RETENTION_BATCH_SIZE", "111")
    cp = ControlPlane.in_memory()
    pol = _enabled(cp)["action_events"]
    assert pol["max_age_seconds"] == 3 * 86400
    assert pol["batch_size"] == 111


def test_disable_flag_leaves_everything_preserve(monkeypatch):
    monkeypatch.setenv("MAC_RETENTION_TICK_ENABLED", "0")
    cp = ControlPlane.in_memory()
    assert _enabled(cp) == {}


def test_prune_tick_deletes_aged_observability_rows(cp):
    # Insert observability rows well outside the 7-day window, then drain.
    cfg = RECORD_CLASS_CONFIG["observability_events"]
    table, ts = cfg["table"], cfg["ts"]
    old = "2000-01-01T00:00:00.000000"
    before = cp.store.query_one("SELECT COUNT(*) AS n FROM %s" % table)["n"]
    for i in range(5):
        cp.record_log(
            "retention.test.old_event",
            layer="test",
            source="test",
            level="info",
            detail={"i": i},
        )
    # backdate the rows we just wrote so they are eligible
    cp.store.execute(
        "UPDATE %s SET %s = ? WHERE name = 'retention.test.old_event'" % (table, ts),
        (old,),
    )
    aged = cp.store.query_one(
        "SELECT COUNT(*) AS n FROM %s WHERE %s = ?" % (table, ts), (old,)
    )["n"]
    assert aged == 5

    summary = cp.retention_prune_tick()
    assert summary["deleted"].get("observability_events", 0) >= 5
    remaining = cp.store.query_one(
        "SELECT COUNT(*) AS n FROM %s WHERE %s = ?" % (table, ts), (old,)
    )["n"]
    assert remaining == 0


def test_prune_tick_preserves_recent_rows(cp):
    # A freshly written observability row is inside the window and must survive.
    cp.record_log("retention.test.recent", layer="test", source="test", level="info")
    cp.retention_prune_tick()
    n = cp.store.query_one(
        "SELECT COUNT(*) AS n FROM observability_events WHERE name = 'retention.test.recent'"
    )["n"]
    assert n == 1


def test_prune_tick_bounded_by_max_batches(cp, monkeypatch):
    # With batch_size=1 and max_batches=2, a single tick deletes at most 2 rows
    # per class even if more are eligible (bounded work per tick).
    monkeypatch.setenv("MAC_RETENTION_BATCH_SIZE", "1")
    cp2 = ControlPlane.in_memory()
    cfg = RECORD_CLASS_CONFIG["observability_events"]
    table, ts = cfg["table"], cfg["ts"]
    for _ in range(6):
        cp2.record_log("retention.test.bulk", layer="test", source="test", level="info")
    cp2.store.execute(
        "UPDATE %s SET %s = '2000-01-01T00:00:00.000000' WHERE name = 'retention.test.bulk'"
        % (table, ts)
    )
    summary = cp2.retention_prune_tick(max_batches=2)
    assert summary["deleted"]["observability_events"] == 2
