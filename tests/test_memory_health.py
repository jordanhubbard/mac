"""mem-10: memory-tier health snapshot tests.

Covers the alert rules that encode the failure modes the original
2026-05-28 audit found:

* inert vector tier: memory_records > 100 but vector_refs == 0
* stalled consolidator: last_nap_run_at older than 2× nap_interval
* no_nap_history: any memory_records but no completed nap_runs
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mac.models import new_id
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _add_memory(cp, content="x", agent_id="agent_one"):
    return cp.add_memory(
        task_id=None,
        subject_type="topic",
        subject_id=None,
        record_type="note",
        content=content,
        evidence_id=None,
        created_by=agent_id,
    )


def test_empty_db_yields_zero_counts_no_alerts(cp):
    """A fresh control plane reports zero counts and no alerts."""
    h = cp.memory_health()
    assert h["schema"] == "mac.memory_health.v1"
    assert h["memory_records_count"] == 0
    assert h["vector_refs_count"] == 0
    assert h["observability_events_count"] >= 0  # may have setup rows
    assert h["last_nap_run_at"] is None
    assert h["alerts"] == []


def test_inert_vector_tier_alert(cp):
    """The original audit's smoking gun: lots of memories, zero
    vector_refs. Fires as a critical alert."""
    for i in range(105):
        _add_memory(cp, content=f"memory {i}")
    h = cp.memory_health()
    assert h["memory_records_count"] == 105
    assert h["vector_refs_count"] == 0
    codes = [a["code"] for a in h["alerts"]]
    assert "inert_vector_tier" in codes
    inert = next(a for a in h["alerts"] if a["code"] == "inert_vector_tier")
    assert inert["severity"] == "critical"


def test_no_nap_history_alert_when_memories_present(cp):
    """Memories exist but no nap_runs have ever completed → warning."""
    _add_memory(cp)
    h = cp.memory_health()
    codes = [a["code"] for a in h["alerts"]]
    assert "no_nap_history" in codes


def test_stalled_consolidator_alert(cp):
    """An old completed nap_run (older than 2× nap_interval) is a
    critical alert: the daily nap stopped running."""
    # Pre-plant a completed nap_run with a `completed_at` 100h ago.
    old_at = (datetime.now(tz=timezone.utc) - timedelta(hours=100)).isoformat()
    machine = cp.register_machine("host-x")
    agent = cp.register_agent(machine.id, "agent-x", capabilities=[])
    cp.store.execute(
        """
        INSERT INTO nap_runs (id, agent_id, status, started_at, completed_at,
            summary_evidence_id, detail, created_at, updated_at)
        VALUES (?, ?, 'completed', ?, ?, NULL, '{}', ?, ?)
        """,
        (new_id("nap"), agent.id, old_at, old_at, old_at, old_at),
    )
    # Default threshold is 2*24h = 48h; 100h > 48h so the alert fires.
    h = cp.memory_health()
    codes = [a["code"] for a in h["alerts"]]
    assert "stalled_consolidator" in codes
    stalled = next(a for a in h["alerts"] if a["code"] == "stalled_consolidator")
    assert stalled["severity"] == "critical"


def test_stalled_consolidator_alert_respects_custom_interval(cp):
    """If the operator runs naps every 4h, 100h is *way* over 2×4 = 8h."""
    old_at = (datetime.now(tz=timezone.utc) - timedelta(hours=100)).isoformat()
    machine = cp.register_machine("host-x")
    agent = cp.register_agent(machine.id, "agent-x", capabilities=[])
    cp.store.execute(
        """
        INSERT INTO nap_runs (id, agent_id, status, started_at, completed_at,
            summary_evidence_id, detail, created_at, updated_at)
        VALUES (?, ?, 'completed', ?, ?, NULL, '{}', ?, ?)
        """,
        (new_id("nap"), agent.id, old_at, old_at, old_at, old_at),
    )
    h = cp.memory_health(nap_interval_hours=4.0)
    codes = [a["code"] for a in h["alerts"]]
    assert "stalled_consolidator" in codes


def test_recent_nap_does_not_fire_stalled_alert(cp):
    """A completed nap 1h ago is well within the 48h threshold."""
    recent_at = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
    machine = cp.register_machine("host-r")
    agent = cp.register_agent(machine.id, "agent-r", capabilities=[])
    cp.store.execute(
        """
        INSERT INTO nap_runs (id, agent_id, status, started_at, completed_at,
            summary_evidence_id, detail, created_at, updated_at)
        VALUES (?, ?, 'completed', ?, ?, NULL, '{}', ?, ?)
        """,
        (new_id("nap"), agent.id, recent_at, recent_at, recent_at, recent_at),
    )
    h = cp.memory_health()
    codes = [a["code"] for a in h["alerts"]]
    assert "stalled_consolidator" not in codes


def test_health_includes_qdrant_block_when_url_configured(cp, monkeypatch):
    """When MAC_QDRANT_URL is set, the health block tries to enumerate
    collections. Unreachable URL → per-collection error, not a crash."""
    monkeypatch.setenv("MAC_QDRANT_URL", "http://unreachable.invalid:6333")
    h = cp.memory_health()
    assert h["qdrant"]["url"] == "http://unreachable.invalid:6333"
    # Both default collections are listed, each with an error string
    # because the URL doesn't resolve.
    for coll in ("mac_memory_medium", "mac_memory_long"):
        assert coll in h["qdrant"]["collections"]
        # Either points_count comes back or the per-collection error is set.
        entry = h["qdrant"]["collections"][coll]
        assert "tier" in entry
        assert entry.get("points_count") is not None or entry.get("error")


def test_health_omits_qdrant_collections_when_url_unset(cp, monkeypatch):
    """No MAC_QDRANT_URL → qdrant.url is None, collections dict empty."""
    monkeypatch.delenv("MAC_QDRANT_URL", raising=False)
    h = cp.memory_health()
    assert h["qdrant"]["url"] is None
    assert h["qdrant"]["collections"] == {}
