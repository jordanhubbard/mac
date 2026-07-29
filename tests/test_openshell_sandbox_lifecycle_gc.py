from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mac.openshell_sandbox_gc import (
    LIFECYCLE_TRIGGERS,
    classify_lifecycle_orphan_sandbox,
    classify_leftover_task_sandbox,
    delete_named_sandbox,
    lifecycle_orphan_task_sandbox_candidates,
    leftover_task_sandbox_candidates,
    reconcile_leftover_task_sandboxes,
    reconcile_task_sandbox_lifecycle,
    sandbox_inventory_summary,
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
    pid=None,
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
    if pid is not None:
        labels["mac.pid"] = str(pid)
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


def test_candidates_ignore_invalid_rows_and_normalize_naive_now():
    candidates = lifecycle_orphan_task_sandbox_candidates(
        [
            None,
            _sandbox(task_id=None, created_at=None),
            _sandbox(
                name="mac-task-terminal",
                created_at=None,
                task_id="task_terminal",
            ),
        ],
        lambda _tid: _task(state="completed", lease_id=""),
        trigger=" FINALIZATION ",
        now=NOW.replace(tzinfo=None),
    )
    assert [row["sandbox_name"] for row in candidates] == ["mac-task-terminal"]
    assert candidates[0]["trigger"] == "finalization"
    assert candidates[0]["age_seconds"] is None


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


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message"),
    [
        (1, "", "inventory unavailable", "sandbox list failed"),
        (0, "{not-json", "", "invalid JSON"),
        (0, "{}", "", "not an array"),
    ],
)
def test_reconcile_rejects_unusable_inventory(
    monkeypatch, returncode, stdout, stderr, message
):
    import subprocess

    class _Proc:
        pass

    proc = _Proc()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: proc)

    with pytest.raises(RuntimeError, match=message):
        reconcile_task_sandbox_lifecycle(lambda _tid: None, now=NOW)


def test_reconcile_default_delete_is_exact_and_idempotent(monkeypatch):
    import json
    import subprocess

    calls = []

    def _run(argv, **_kwargs):
        calls.append(argv)
        if argv[2] == "list":
            return type(
                "_Proc",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps([_sandbox(name="mac-task-terminal")]),
                    "stderr": "",
                },
            )()
        return type("_Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", _run)
    report = reconcile_task_sandbox_lifecycle(
        lambda _tid: _task(state="completed", lease_id=""),
        trigger="finalization",
        apply=True,
        now=NOW,
    )
    assert report["deleted"] == ["mac-task-terminal"]
    assert calls[-1] == ["openshell", "sandbox", "delete", "mac-task-terminal"]


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


# --- exact deletion and conservative dual-proof sweep ------------------------


def test_delete_named_sandbox_rejects_empty_identity():
    report = delete_named_sandbox("  ")
    assert report["deleted"] is False
    assert report["attempts"] == 0
    assert report["error"] == "sandbox name is empty"


def test_delete_named_sandbox_treats_already_gone_as_success():
    proc = type(
        "_Proc",
        (),
        {"returncode": 1, "stdout": "", "stderr": "sandbox not found"},
    )()
    report = delete_named_sandbox(
        "mac-task-gone",
        attempts=1,
        runner=lambda *_a, **_k: proc,
    )
    assert report["deleted"] is True
    assert report["attempts"] == 1
    assert report["error"] == ""


def test_delete_named_sandbox_bounds_retries_and_backoff():
    calls = 0
    sleeps = []

    def _runner(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary transport failure")
        return type(
            "_Proc",
            (),
            {"returncode": 1, "stdout": "still busy", "stderr": ""},
        )()

    report = delete_named_sandbox(
        "mac-task-busy",
        attempts=2,
        backoff_seconds=0.25,
        sleep=sleeps.append,
        runner=_runner,
    )
    assert report["deleted"] is False
    assert report["attempts"] == 2
    assert report["error"] == "still busy"
    assert sleeps == [0.25]


def test_leftover_requires_both_dead_pid_and_expired_lease_proofs():
    sandbox = _sandbox(pid=4321)
    terminal = _task(state="completed", lease_id="")

    record = classify_leftover_task_sandbox(
        sandbox, terminal, now=NOW, pid_is_alive=lambda _pid: False
    )
    assert record["reap"] is True

    live_pid = classify_leftover_task_sandbox(
        sandbox, terminal, now=NOW, pid_is_alive=lambda _pid: True
    )
    assert live_pid["reap"] is False
    assert "dead-PID proof withheld" in live_pid["reason"]

    live_lease = classify_leftover_task_sandbox(
        sandbox, _task(), now=NOW, pid_is_alive=lambda _pid: False
    )
    assert live_lease["reap"] is False
    assert "lease-authority proof withheld" in live_lease["reason"]


def test_leftover_candidates_fail_closed_sort_and_deduplicate():
    rows = [
        None,
        {"name": "mac-task-unlabelled", "phase": "Ready", "labels": None},
        _sandbox(name="mac-task-z", task_id="task_z", pid=10),
        _sandbox(name="mac-task-a", task_id="task_a", pid=11),
        _sandbox(name="mac-task-a", task_id="task_a", pid=11),
        _sandbox(name="mac-task-lookup-error", task_id="task_error", pid=12),
    ]

    def _lookup(task_id):
        if task_id == "task_error":
            raise RuntimeError("hub unavailable")
        return _task(state="completed", lease_id="")

    candidates = leftover_task_sandbox_candidates(
        rows,
        _lookup,
        now=NOW.replace(tzinfo=None),
        pid_is_alive=lambda _pid: False,
    )
    assert [row["name"] for row in candidates] == ["mac-task-a", "mac-task-z"]


def test_leftover_reconcile_applies_successes_and_records_failures(monkeypatch):
    import json
    import subprocess

    rows = [
        _sandbox(name="mac-task-ok", task_id="task_ok", pid=10),
        _sandbox(name="mac-task-busy", task_id="task_busy", pid=11),
    ]
    proc = type(
        "_Proc",
        (),
        {"returncode": 0, "stdout": json.dumps(rows), "stderr": ""},
    )()
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: proc)
    monkeypatch.setattr(
        "mac.openshell_sandbox_gc.delete_named_sandbox",
        lambda name, **_kwargs: {
            "name": name,
            "deleted": name == "mac-task-ok",
            "error": "" if name == "mac-task-ok" else "busy",
        },
    )

    report = reconcile_leftover_task_sandboxes(
        lambda _tid: _task(state="completed", lease_id=""),
        apply=True,
        now=NOW,
        pid_is_alive=lambda _pid: False,
    )
    assert report["deleted"] == ["mac-task-ok"]
    assert report["failures"] == [{"name": "mac-task-busy", "error": "busy"}]


@pytest.mark.parametrize(
    ("returncode", "stdout", "message"),
    [
        (1, "", "sandbox list failed"),
        (0, "{bad-json", "invalid JSON"),
        (0, "{}", "not an array"),
    ],
)
def test_leftover_reconcile_rejects_unusable_inventory(
    monkeypatch, returncode, stdout, message
):
    import subprocess

    proc = type(
        "_Proc",
        (),
        {"returncode": returncode, "stdout": stdout, "stderr": "list error"},
    )()
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: proc)
    with pytest.raises(RuntimeError, match=message):
        reconcile_leftover_task_sandboxes(lambda _tid: None, now=NOW)


def test_inventory_summary_accounts_for_protected_and_reapable_rows():
    rows = [
        None,
        {"name": "foreign", "labels": {"mac.owner": "mac"}},
        _sandbox(name="mac-task-old", pid=10, created_at="2026-07-20 12:00:00"),
        _sandbox(name="mac-task-young", pid=11, created_at="2026-07-27 12:00:00"),
        _sandbox(name="mac-task-live", pid=12, created_at=None),
    ]
    report = sandbox_inventory_summary(
        rows,
        now=NOW.replace(tzinfo=None),
        pid_is_alive=lambda pid: pid == 12,
    )
    assert report == {
        "schema": "mac.openshell.sandbox_inventory.v1",
        "scanned": 4,
        "managed": 3,
        "reap_eligible": 2,
        "protected": 1,
        "oldest_managed_age_seconds": 8 * 24 * 60 * 60,
    }
