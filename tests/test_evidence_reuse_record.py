"""Tests for the EvidenceReuseRecord model, its store table, and the
ControlPlane record/get/list service methods.

The record persists prior-executor-evidence reuse decisions (see
``mac.evidence_reuse_verifier``) so the control plane keeps a durable,
queryable audit trail across both SQLite and Postgres backends.
"""
from __future__ import annotations

import pytest

from mac.models import EvidenceReuseRecord, NotFoundError, ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _task(cp, title="reuse-target"):
    return cp.create_task(title)


def test_record_evidence_reuse_persists_decision(cp):
    task = _task(cp)
    record = cp.record_evidence_reuse(
        task.id,
        "evd_prior_1",
        True,
        verification={"schema": "mac.evidence_reuse_verification.v1", "ok": True, "problems": []},
        remote_url="git@github.com:org/repo.git",
        expected_head_sha="a" * 40,
        decided_by="agent_recovery",
    )

    assert isinstance(record, EvidenceReuseRecord)
    assert record.id.startswith("evreuse")
    assert record.task_id == task.id
    assert record.source_evidence_id == "evd_prior_1"
    assert record.reused is True
    assert record.remote_url == "git@github.com:org/repo.git"
    assert record.expected_head_sha == "a" * 40
    assert record.decided_by == "agent_recovery"
    assert record.problems == []
    assert record.verification["ok"] is True
    assert record.created_at

    fetched = cp.get_evidence_reuse(record.id)
    assert fetched.to_dict() == record.to_dict()


def test_record_evidence_reuse_defaults_problems_from_verification(cp):
    task = _task(cp)
    record = cp.record_evidence_reuse(
        task.id,
        "evd_prior_2",
        False,
        verification={"ok": False, "problems": ["remote sha mismatch", "dirty branch"]},
    )

    assert record.reused is False
    assert record.problems == ["remote sha mismatch", "dirty branch"]
    assert record.decided_by == "control-plane"
    assert record.remote_url is None
    assert record.expected_head_sha is None


def test_record_evidence_reuse_explicit_problems_override(cp):
    task = _task(cp)
    record = cp.record_evidence_reuse(
        task.id,
        "evd_prior_3",
        False,
        verification={"ok": False, "problems": ["ignored"]},
        problems=["explicit reason"],
    )
    assert record.problems == ["explicit reason"]


def test_record_evidence_reuse_requires_known_task(cp):
    with pytest.raises(NotFoundError):
        cp.record_evidence_reuse("task_missing", "evd_x", True)


def test_record_evidence_reuse_requires_source_id(cp):
    task = _task(cp)
    with pytest.raises(ValidationError):
        cp.record_evidence_reuse(task.id, "   ", True)


def test_get_evidence_reuse_unknown_raises(cp):
    with pytest.raises(NotFoundError):
        cp.get_evidence_reuse("evreuse_nope")


def test_list_evidence_reuse_orders_and_filters(cp):
    task_a = _task(cp, "task-a")
    task_b = _task(cp, "task-b")

    r1 = cp.record_evidence_reuse(task_a.id, "evd_a1", True)
    r2 = cp.record_evidence_reuse(task_a.id, "evd_a2", False)
    r3 = cp.record_evidence_reuse(task_b.id, "evd_b1", True)

    all_records = cp.list_evidence_reuse()
    ids = {r.id for r in all_records}
    assert {r1.id, r2.id, r3.id} <= ids

    by_task = cp.list_evidence_reuse(task_id=task_a.id)
    assert {r.id for r in by_task} == {r1.id, r2.id}

    by_source = cp.list_evidence_reuse(source_evidence_id="evd_b1")
    assert [r.id for r in by_source] == [r3.id]

    reused_only = cp.list_evidence_reuse(task_id=task_a.id, reused=True)
    assert [r.id for r in reused_only] == [r1.id]

    refused_only = cp.list_evidence_reuse(reused=False)
    assert r2.id in {r.id for r in refused_only}
    assert r1.id not in {r.id for r in refused_only}


def test_list_evidence_reuse_limit_is_clamped(cp):
    task = _task(cp)
    for i in range(3):
        cp.record_evidence_reuse(task.id, "evd_%d" % i, True)
    assert len(cp.list_evidence_reuse(limit=0)) == 1
    assert len(cp.list_evidence_reuse(limit=2)) == 2
