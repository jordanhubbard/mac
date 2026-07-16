"""Tests for the reuse *provenance* surface of :class:`EvidenceReuseRecord`.

The sibling implementation task added provenance fields to the record
(``reused_by_agent_id``, ``reuse_context``, ``metadata`` and the
``prior_evidence_id`` alias for ``source_evidence_id``) plus the
``ControlPlane.record_evidence_reuse`` / ``get_evidence_reuse_records``
service methods. These tests focus on the provenance behaviour: that the
record captures *which* agent reused *which* prior evidence and *why*, that
the decision emits a durable ``evidence.reuse_recorded`` task-history event,
and that the per-task query returns records in a stable order.

They complement (rather than duplicate) ``test_evidence_reuse_record.py``,
which covers the base record/list plumbing.
"""
from __future__ import annotations

import pytest

from mac.models import EvidenceReuseRecord, NotFoundError, ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _task(cp, title="reuse-provenance-target"):
    return cp.create_task(title)


# ---------------------------------------------------------------------------
# 1. Model construction + to_dict() serialisation, incl. prior_evidence_id alias
# ---------------------------------------------------------------------------
def test_record_dataclass_construction_and_to_dict_alias():
    record = EvidenceReuseRecord(
        id="evreuse_1",
        task_id="task_1",
        source_evidence_id="evd_prior_1",
        remote_url="git@github.com:org/repo.git",
        expected_head_sha="a" * 40,
        reused=True,
        verification={"ok": True, "problems": []},
        problems=[],
        decided_by="control-plane",
        created_at="2026-01-01T00:00:00Z",
        reused_by_agent_id="agent_recovery",
        reuse_context="review_bypass",
        metadata={"source_task_id": "task_prior"},
    )

    # prior_evidence_id is a read-only provenance alias for source_evidence_id.
    assert record.prior_evidence_id == "evd_prior_1"

    data = record.to_dict()
    assert data["source_evidence_id"] == "evd_prior_1"
    assert data["prior_evidence_id"] == "evd_prior_1"
    assert data["reused_by_agent_id"] == "agent_recovery"
    assert data["reuse_context"] == "review_bypass"
    assert data["metadata"] == {"source_task_id": "task_prior"}
    assert data["reused"] is True


def test_record_provenance_fields_default_to_empty():
    """Historical rows / positional constructors keep working: the additive
    provenance fields are optional with empty defaults."""
    record = EvidenceReuseRecord(
        "evreuse_2",
        "task_2",
        "evd_prior_2",
        None,
        None,
        False,
        {},
        [],
        "control-plane",
        "2026-01-01T00:00:00Z",
    )
    assert record.reused_by_agent_id == ""
    assert record.reuse_context == ""
    assert record.metadata == {}
    assert record.prior_evidence_id == "evd_prior_2"
    assert record.to_dict()["prior_evidence_id"] == "evd_prior_2"


# ---------------------------------------------------------------------------
# 2. Store round-trip: record_evidence_reuse persists, get_evidence_reuse_records reads
# ---------------------------------------------------------------------------
def test_record_persists_provenance_and_roundtrips(cp):
    task = _task(cp)
    record = cp.record_evidence_reuse(
        task.id,
        "evd_prior_3",
        True,
        reused_by_agent_id="agent_recovery",
        reuse_context="recovery_shortcut",
        metadata={"reviewer_failure": "infra_timeout"},
        decided_by="control-plane",
    )

    assert record.reused_by_agent_id == "agent_recovery"
    assert record.reuse_context == "recovery_shortcut"
    assert record.metadata == {"reviewer_failure": "infra_timeout"}
    assert record.prior_evidence_id == "evd_prior_3"

    fetched = cp.get_evidence_reuse_records(task.id)
    assert [r.id for r in fetched] == [record.id]
    assert fetched[0].to_dict() == record.to_dict()


def test_reused_by_agent_defaults_to_decider(cp):
    """When no acting agent is named, provenance falls back to decided_by."""
    task = _task(cp)
    record = cp.record_evidence_reuse(
        task.id,
        "evd_prior_4",
        True,
        decided_by="agent_decider",
    )
    assert record.reused_by_agent_id == "agent_decider"
    assert record.reuse_context == ""


# ---------------------------------------------------------------------------
# 3. Service happy path: fields correct + evidence.reuse_recorded history event
# ---------------------------------------------------------------------------
def test_record_emits_reuse_recorded_history_event(cp):
    task = _task(cp)
    record = cp.record_evidence_reuse(
        task.id,
        "evd_prior_5",
        True,
        reused_by_agent_id="agent_recovery",
        reuse_context="review_bypass",
        decided_by="control-plane",
    )

    events = cp.task_history(task.id)
    reuse_events = [e for e in events if e.event_type == "evidence.reuse_recorded"]
    assert len(reuse_events) == 1
    event = reuse_events[0]

    # Actor is the agent that performed the reuse (provenance).
    assert event.actor == "agent_recovery"
    detail = event.detail
    assert detail["record_id"] == record.id
    assert detail["source_evidence_id"] == "evd_prior_5"
    assert detail["prior_evidence_id"] == "evd_prior_5"
    assert detail["reused"] is True
    assert detail["reuse_context"] == "review_bypass"
    assert detail["decided_by"] == "control-plane"


def test_refused_reuse_still_records_provenance_event(cp):
    task = _task(cp)
    record = cp.record_evidence_reuse(
        task.id,
        "evd_prior_6",
        False,
        verification={"ok": False, "problems": ["remote sha mismatch"]},
        reused_by_agent_id="agent_recovery",
        reuse_context="review_bypass",
    )
    assert record.reused is False
    assert record.problems == ["remote sha mismatch"]

    detail = next(
        e.detail
        for e in cp.task_history(task.id)
        if e.event_type == "evidence.reuse_recorded"
    )
    assert detail["reused"] is False
    assert detail["prior_evidence_id"] == "evd_prior_6"


# ---------------------------------------------------------------------------
# 4. Multiple records per task are independent (distinct ids + own events)
# ---------------------------------------------------------------------------
def test_multiple_records_produce_independent_events(cp):
    task = _task(cp)
    r1 = cp.record_evidence_reuse(
        task.id, "evd_a", True, reused_by_agent_id="agent_a", reuse_context="review_bypass"
    )
    r2 = cp.record_evidence_reuse(
        task.id, "evd_b", False, reused_by_agent_id="agent_b", reuse_context="recovery_shortcut"
    )
    assert r1.id != r2.id

    reuse_events = [
        e for e in cp.task_history(task.id) if e.event_type == "evidence.reuse_recorded"
    ]
    assert len(reuse_events) == 2
    actors = {e.actor for e in reuse_events}
    assert actors == {"agent_a", "agent_b"}


# ---------------------------------------------------------------------------
# 5. ValidationError on missing/blank required source id
# ---------------------------------------------------------------------------
def test_blank_source_evidence_id_raises_validation_error(cp):
    task = _task(cp)
    with pytest.raises(ValidationError):
        cp.record_evidence_reuse(task.id, "   ", True, reused_by_agent_id="agent_recovery")


def test_empty_source_evidence_id_raises_validation_error(cp):
    task = _task(cp)
    with pytest.raises(ValidationError):
        cp.record_evidence_reuse(task.id, "", True)


# ---------------------------------------------------------------------------
# 6. NotFoundError on unknown task id (both write + read paths)
# ---------------------------------------------------------------------------
def test_record_unknown_task_raises_not_found(cp):
    with pytest.raises(NotFoundError):
        cp.record_evidence_reuse("task_does_not_exist", "evd_prior", True)


def test_get_records_unknown_task_raises_not_found(cp):
    with pytest.raises(NotFoundError):
        cp.get_evidence_reuse_records("task_does_not_exist")


# ---------------------------------------------------------------------------
# 7. get_evidence_reuse_records ordering + isolation between tasks
# ---------------------------------------------------------------------------
def test_get_records_empty_when_none_recorded(cp):
    task = _task(cp)
    assert cp.get_evidence_reuse_records(task.id) == []


def test_get_records_returns_multiple_in_creation_order(cp):
    task = _task(cp)
    created = [
        cp.record_evidence_reuse(
            task.id,
            "evd_%d" % i,
            i % 2 == 0,
            reused_by_agent_id="agent_%d" % i,
            reuse_context="review_bypass",
        )
        for i in range(3)
    ]

    records = cp.get_evidence_reuse_records(task.id)
    # oldest-first ordering (created_at, id) matches creation order.
    assert [r.id for r in records] == [r.id for r in created]
    assert [r.source_evidence_id for r in records] == ["evd_0", "evd_1", "evd_2"]


def test_get_records_is_scoped_to_the_task(cp):
    task_a = _task(cp, "task-a")
    task_b = _task(cp, "task-b")
    a1 = cp.record_evidence_reuse(task_a.id, "evd_a1", True)
    cp.record_evidence_reuse(task_b.id, "evd_b1", True)

    records_a = cp.get_evidence_reuse_records(task_a.id)
    assert [r.id for r in records_a] == [a1.id]
    assert all(r.task_id == task_a.id for r in records_a)
