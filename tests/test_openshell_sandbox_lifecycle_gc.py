from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mac.openshell_sandbox_gc import (
    LIFECYCLE_TRIGGERS,
    classify_lifecycle_orphan_sandbox,
    lifecycle_orphan_task_sandbox_candidates,
    reconcile_task_sandbox_lifecycle,
)


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def _sandbox(
    name="mac-task-worker6",
    *,
    owner="mac",
    kind="task",
    keep="false",
    task_id="task_worker6",
    lease_id="lease_worker6",
    phase="Ready",
    created_at="2026-07-27 12:00:00",
):
    labels = {}
    if owner is not None:
        labels["mac.owner"] = owner
    if kind is not None:
        labels["mac.kind"] = kind
    if keep is not None:
        labels["mac.keep"] = keep
    if task_id is not None:
        labels["mac.task.id"] = task_id
    if lease_id is not None:
        labels["mac.lease.id"] = lease_id
    row = {"name": name, "phase": phase, "labels": labels}
    if created_at is not None:
        row["created_at"] = created_at
    return row


def _task(*, state="claimed", lease_id="lease_worker6", leased_until=None):
    if leased_until is None:
        leased_until = (NOW + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    return {"state": state, "lease_id": lease_id, "leased_until": leased_until}


# --- classification records carry the full accountable tuple -----------------


def test_record_carries_full_accountable_tuple():
    record = classify_lifecycle_orphan_sandbox(
        _sandbox(),
        _task(),
        trigger="periodic",
        now=NOW,
    )
    for field in (
        "trigger",
        "task_id",
        "lease_id",
        "sandbox_name",
        "ownership",
        "age_seconds",
        "action",
        "outcome",
    ):
        assert field in record, field
    assert record["task_id"] == "task_worker6"
    assert record["lease_id"] == "lease_worker6"
    assert record["sandbox_name"] == "mac-task-worker6"
    assert record["age_seconds"] == 24 * 60 * 60


def test_live_matching_lease_is_kept():
    record = classify_lifecycle_orphan_sandbox(
        _sandbox(), _task(), trigger="finalization", now=NOW
    )
    assert record["action"] == "keep"
    assert record["outcome"] == "kept"
    assert record["ownership"] == "lease-live"


# --- worker6 / worker7: task moved to another worker (lease superseded) -------


def test_ownership_change_superseded_lease_is_reaped():
    # worker6's sandbox still names lease_worker6, but the task's active lease
    # moved to worker8 -- the hub proves worker6 no longer owns the task.
    task = _task(state="claimed", lease_id="lease_worker8")
    record = classify_lifecycle_orphan_sandbox(
        _sandbox(name="mac-task-worker6", lease_id="lease_worker6"),
        task,
        trigger="ownership_change",
        now=NOW,
    )
    assert record["action"] == "reap"
    assert record["ownership"] == "lease-not-live"
    assert "superseded" in record["reason"]


# --- worker7: task entered review then finalized (terminal) -------------------


def test_finalized_task_is_reaped():
    record = classify_lifecycle_orphan_sandbox(
        _sandbox(name="mac-task-worker7", task_id="task_worker7"),
        _task(state="completed", lease_id=""),
        trigger="finalization",
        now=NOW,
    )
    assert record["action"] == "reap"
    assert record["ownership"] == "lease-not-live"


def test_cancelled_task_is_reaped():
    record = classify_lifecycle_orphan_sandbox(
        _sandbox(),
        _task(state="cancelled", lease_id=""),
        trigger="cancellation",
        now=NOW,
    )
    assert record["action"] == "reap"


def test_expired_lease_is_reaped():
    expired = (NOW - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    record = classify_lifecycle_orphan_sandbox(
        _sandbox(),
        _task(state="claimed", leased_until=expired),
        trigger="worker_replacement",
        now=NOW,
    )
    assert record["action"] == "reap"
    assert "expired" in record["reason"]


# --- idleness / age alone are NEVER a reap justification ----------------------


def test_idle_worker_with_live_lease_is_never_reaped():
    # An old sandbox whose worker is "idle" but whose lease is still live in the
    # hub must be preserved -- age is recorded but never authorizes a delete.
    old = _sandbox(created_at="2026-06-01 12:00:00")
    record = classify_lifecycle_orphan_sandbox(
        old, _task(), trigger="periodic", now=NOW
    )
    assert record["action"] == "keep"
    assert record["age_seconds"] > 24 * 60 * 60


def test_unresolvable_task_fails_closed():
    candidates = lifecycle_orphan_task_sandbox_candidates(
        [_sandbox()],
        lambda _tid: None,
        trigger="periodic",
        now=NOW,
    )
    assert candidates == []


def test_lookup_failure_fails_closed():
    def _boom(_tid):
        raise RuntimeError("hub unreachable")

    candidates = lifecycle_orphan_task_sandbox_candidates(
        [_sandbox()], _boom, trigger="periodic", now=NOW
    )
    assert candidates == []


def test_keep_true_is_preserved():
    record = classify_lifecycle_orphan_sandbox(
        _sandbox(keep="true"),
        _task(state="completed", lease_id=""),
        trigger="finalization",
        now=NOW,
    )
    assert record["action"] == "keep"


# --- candidate set is deterministic and idempotent ---------------------------


def test_candidates_sorted_and_deduplicated():
    rows = [
        _sandbox(name="mac-task-worker7", task_id="task_worker7"),
        _sandbox(name="mac-task-worker7", task_id="task_worker7"),
        _sandbox(name="mac-task-worker6", task_id="task_worker6"),
    ]
    candidates = lifecycle_orphan_task_sandbox_candidates(
        rows,
        lambda _tid: _task(state="completed", lease_id=""),
        trigger="ownership_change",
        now=NOW,
    )
    names = [row["sandbox_name"] for row in candidates]
    assert names == ["mac-task-worker6", "mac-task-worker7"]


# --- reconciler: dry-run vs apply, outcomes, and secret-free evidence --------


def _fake_lister(rows):
    import json
    import subprocess

    class _Proc:
        returncode = 0
        stdout = json.dumps(rows)
        stderr = ""

    def _run(*_a, **_k):
        return _Proc()

    return _run


def test_reconcile_dry_run_marks_dry_run(monkeypatch):
    import subprocess

    rows = [_sandbox(name="mac-task-worker6")]
    monkeypatch.setattr(subprocess, "run", _fake_lister(rows))
    report = reconcile_task_sandbox_lifecycle(
        lambda _tid: _task(state="completed", lease_id=""),
        trigger="finalization",
        apply=False,
        now=NOW,
    )
    assert report["dry_run"] is True
    assert report["deleted"] == []
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["outcome"] == "dry-run"


def test_reconcile_apply_deletes_and_records_outcome(monkeypatch):
    import subprocess

    rows = [
        _sandbox(name="mac-task-worker6", task_id="task_worker6"),
        _sandbox(name="mac-task-worker7", task_id="task_worker7"),
    ]
    monkeypatch.setattr(subprocess, "run", _fake_lister(rows))

    deleted_names = []

    def _delete(name):
        deleted_names.append(name)
        return {"deleted": True, "error": ""}

    report = reconcile_task_sandbox_lifecycle(
        lambda _tid: _task(state="completed", lease_id=""),
        trigger="worker_replacement",
        apply=True,
        now=NOW,
        delete_sandbox=_delete,
    )
    assert sorted(deleted_names) == ["mac-task-worker6", "mac-task-worker7"]
    assert sorted(report["deleted"]) == ["mac-task-worker6", "mac-task-worker7"]
    for record in report["candidates"]:
        assert record["outcome"] == "deleted"
        assert record["action"] == "reap"


def test_reconcile_records_delete_failure(monkeypatch):
    import subprocess

    rows = [_sandbox(name="mac-task-worker6")]
    monkeypatch.setattr(subprocess, "run", _fake_lister(rows))

    report = reconcile_task_sandbox_lifecycle(
        lambda _tid: _task(state="completed", lease_id=""),
        trigger="ownership_change",
        apply=True,
        now=NOW,
        delete_sandbox=lambda _n: {"deleted": False, "error": "boom"},
    )
    assert report["deleted"] == []
    assert report["failures"] == [{"name": "mac-task-worker6", "error": "boom"}]
    assert report["candidates"][0]["outcome"] == "delete-failed"


def test_reconcile_rejects_unknown_trigger(monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "run", _fake_lister([]))
    with pytest.raises(ValueError):
        reconcile_task_sandbox_lifecycle(
            lambda _tid: None, trigger="not-a-trigger", now=NOW
        )


def test_lifecycle_triggers_cover_required_events():
    for trigger in (
        "ownership_change",
        "finalization",
        "cancellation",
        "worker_replacement",
    ):
        assert trigger in LIFECYCLE_TRIGGERS


# --- ControlPlane integration: real task/lease authority ---------------------


def _install_fake_lister(monkeypatch, rows):
    import subprocess

    monkeypatch.setattr(subprocess, "run", _fake_lister(rows))


def test_control_plane_lifecycle_gc_uses_real_task_authority(monkeypatch):
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    machine = cp.register_machine("worker6-host")
    agent = cp.register_agent(
        machine.id,
        "worker6",
        capabilities=["python"],
        resources={
            "commands": {
                "schema": "mac.command_inventory.v1",
                "available": ["python3", "git", "gh"],
            }
        },
    )
    task = cp.create_task("worker6 unit")
    claimed, lease = cp.claim_task(task.id, agent.id)

    sandbox = _sandbox(
        name="mac-task-worker6",
        task_id=claimed.id,
        lease_id=lease.id,
        created_at="2026-07-27 12:00:00",
    )

    # Live matching lease: the controller preserves the sandbox.
    _install_fake_lister(monkeypatch, [sandbox])
    report = cp.reconcile_openshell_task_sandbox_lifecycle(
        trigger="periodic", apply=False
    )
    assert report["candidates"] == []
    assert report["protected"] == 1

    # Cancel the task (terminal): the controller now proves the lease is gone.
    cp.close_task(
        task.id,
        "cancelled",
        actor="operator",
        detail={"disposition": "preserve", "reason": "worker replaced"},
    )
    _install_fake_lister(monkeypatch, [sandbox])
    report = cp.reconcile_openshell_task_sandbox_lifecycle(
        trigger="cancellation", apply=False, openshell_bin="openshell"
    )
    assert len(report["candidates"]) == 1
    record = report["candidates"][0]
    assert record["sandbox_name"] == "mac-task-worker6"
    assert record["task_id"] == claimed.id
    assert record["lease_id"] == lease.id
    assert record["ownership"] == "lease-not-live"
    assert record["action"] == "reap"
    assert record["outcome"] == "dry-run"
    assert record["age_seconds"] is not None and record["age_seconds"] >= 0


def test_control_plane_lifecycle_gc_missing_binary_is_best_effort(monkeypatch):
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()

    import subprocess

    def _boom(*_a, **_k):
        raise FileNotFoundError("openshell not installed")

    monkeypatch.setattr(subprocess, "run", _boom)
    report = cp.reconcile_openshell_task_sandbox_lifecycle(
        trigger="finalization", apply=True
    )
    assert report["schema"] == "mac.openshell.sandbox_lifecycle_gc.v1"
    assert report["candidates"] == []
    assert "error" in report
