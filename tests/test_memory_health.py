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
    critical alert: the hourly nap stopped running."""
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
    # Default threshold is 2*1h = 2h; 100h > 2h so the alert fires.
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


def test_health_uses_qdrant_url_fallback(cp, monkeypatch):
    """Deployment may expose QDRANT_URL without MAC_QDRANT_URL."""
    monkeypatch.delenv("MAC_QDRANT_URL", raising=False)
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.internal:6333")
    h = cp.memory_health()
    assert h["qdrant"]["url"] == "http://qdrant.internal:6333"


def test_health_omits_qdrant_collections_when_url_unset(cp, monkeypatch):
    """No configured Qdrant URL → qdrant.url is None, collections dict empty."""
    for name in ("MAC_QDRANT_URL", "QDRANT_URL", "QDRANT_ADDRESS", "QDRANT_FLEET_URL"):
        monkeypatch.delenv(name, raising=False)
    h = cp.memory_health()
    assert h["qdrant"]["url"] is None
    assert h["qdrant"]["collections"] == {}


# --------------------------------------------------------------------------
# The 2026-08-21 audit's findings, surfaced through memory_health itself.
#
# Ingestion stopped on 2026-07-25 and nothing said so for 27 days, because
# points_count — the only question the snapshot used to ask Qdrant — cannot
# see a stopped writer. These wire the Qdrant probe through the real
# memory_health entry point with a fake transport.
# --------------------------------------------------------------------------


def _audit_transport(medium_points, long_points=()):
    """A transport shaped like Qdrant's REST replies for the two tiers."""

    def _call(method, url, body=None):
        name = url.split("/collections/")[1].split("/")[0]
        points = {
            "mac_memory_medium": list(medium_points),
            "mac_memory_long": list(long_points),
        }[name]
        if not url.endswith("/scroll"):
            return {"result": {"points_count": len(points)}}
        return {"result": {"points": points, "next_page_offset": None}}

    return _call


def _pt(embedded_at, model):
    return {"id": 1, "payload": {"embedded_at": embedded_at, "embedding_model": model}}


def test_health_alerts_on_ingestion_that_stopped_a_month_ago(cp, monkeypatch):
    """667 points is not health when the newest is 27 days old."""
    monkeypatch.setenv("MAC_QDRANT_URL", "http://qdrant.internal:6333")
    h = cp.memory_health(
        qdrant_transport=_audit_transport(
            [_pt("2026-07-25T20:16:47Z", "model-a")] * 3
        )
    )
    entry = h["qdrant"]["collections"]["mac_memory_medium"]
    assert entry["newest_embedded_at"] == "2026-07-25T20:16:47Z"
    assert entry["ingestion_age_hours"] > 24.0
    codes = [a["code"] for a in h["alerts"]]
    assert "stalled_vector_ingestion" in codes
    assert h["qdrant"]["ingestion_max_age_hours"] == 24.0


def test_health_alerts_on_the_never_written_long_tier(cp, monkeypatch):
    monkeypatch.setenv("MAC_QDRANT_URL", "http://qdrant.internal:6333")
    h = cp.memory_health(
        qdrant_transport=_audit_transport(
            [_pt(_utcnow_iso(), "model-a")], long_points=()
        )
    )
    unwritten = next(
        a for a in h["alerts"] if a["code"] == "unwritten_memory_tier"
    )
    assert unwritten["tier"] == "long"
    assert unwritten["severity"] == "critical"


def test_health_alerts_on_two_embedding_models_in_one_collection(cp, monkeypatch):
    monkeypatch.setenv("MAC_QDRANT_URL", "http://qdrant.internal:6333")
    fresh = _utcnow_iso()
    h = cp.memory_health(
        qdrant_transport=_audit_transport(
            [_pt(fresh, "nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2")] * 2
            + [_pt(fresh, "azure/openai/text-embedding-3-large")],
            long_points=[_pt(fresh, "model-a")],
        )
    )
    mixed = next(a for a in h["alerts"] if a["code"] == "mixed_embedding_spaces")
    assert mixed["collection"] == "mac_memory_medium"
    assert mixed["embedding_models"] == {
        "nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2": 2,
        "azure/openai/text-embedding-3-large": 1,
    }
    # Both tiers written and both fresh, so nothing else fires.
    assert [a["code"] for a in h["alerts"] if a["code"].startswith("stalled_v")] == []


def test_health_ingestion_threshold_is_caller_tunable(cp, monkeypatch):
    monkeypatch.setenv("MAC_QDRANT_URL", "http://qdrant.internal:6333")
    two_hours_ago = (
        datetime.now(tz=timezone.utc) - timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    points = [_pt(two_hours_ago, "model-a")]
    quiet = cp.memory_health(qdrant_transport=_audit_transport(points, points))
    assert "stalled_vector_ingestion" not in [a["code"] for a in quiet["alerts"]]
    strict = cp.memory_health(
        qdrant_transport=_audit_transport(points, points),
        vector_ingestion_max_age_hours=1.0,
    )
    assert "stalled_vector_ingestion" in [a["code"] for a in strict["alerts"]]
    assert strict["qdrant"]["ingestion_max_age_hours"] == 1.0


def test_health_qdrant_failure_never_costs_the_database_numbers(cp, monkeypatch):
    """An exploding transport degrades the Qdrant block, nothing else."""
    monkeypatch.setenv("MAC_QDRANT_URL", "http://qdrant.internal:6333")

    def _boom(method, url, body=None):
        raise RuntimeError("connection reset")

    _add_memory(cp)
    h = cp.memory_health(qdrant_transport=_boom)
    assert h["memory_records_count"] == 1
    for entry in h["qdrant"]["collections"].values():
        assert entry["error"] == "connection reset"
    # Unknown collections produce no Qdrant alerts, only the database-side ones.
    assert [a["code"] for a in h["alerts"]] == ["no_nap_history"]


def _utcnow_iso():
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
