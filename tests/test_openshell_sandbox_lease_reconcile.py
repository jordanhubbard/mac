from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mac.openshell_sandbox_gc import (
    classify_lease_orphan_sandbox,
    lease_orphan_task_sandbox_candidates,
    reconcile_task_sandboxes_from_lease_authority,
)


NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _sandbox(
    name="mac-task-abc123",
    *,
    owner="mac",
    kind="task",
    keep="false",
    task_id="task_abc",
    lease_id="lease_abc",
    phase="Ready",
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
    return {"name": name, "phase": phase, "labels": labels}


def _task(*, state="claimed", lease_id="lease_abc", leased_until=None):
    if leased_until is None:
        leased_until = (NOW + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    return {"state": state, "lease_id": lease_id, "leased_until": leased_until}


# --- classification ----------------------------------------------------------


def test_live_matching_lease_is_kept():
    record = classify_lease_orphan_sandbox(_sandbox(), _task(), now=NOW)
    assert record["reap"] is False
    assert record["reason"] == "task has a live matching lease"


def test_terminal_task_is_reaped():
    for state in ("completed", "failed", "cancelled", "canceled"):
        record = classify_lease_orphan_sandbox(_sandbox(), _task(state=state), now=NOW)
        assert record["reap"] is True, state
        assert "terminal" in record["reason"]


def test_no_active_lease_is_reaped():
    record = classify_lease_orphan_sandbox(_sandbox(), _task(lease_id=""), now=NOW)
    assert record["reap"] is True
    assert record["reason"] == "task has no active lease"


def test_superseded_lease_is_reaped():
    record = classify_lease_orphan_sandbox(
        _sandbox(lease_id="lease_old"),
        _task(lease_id="lease_new"),
        now=NOW,
    )
    assert record["reap"] is True
    assert "superseded" in record["reason"]


def test_expired_lease_is_reaped():
    stale = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    record = classify_lease_orphan_sandbox(_sandbox(), _task(leased_until=stale), now=NOW)
    assert record["reap"] is True
    assert record["reason"] == "task lease has expired"


def test_missing_task_id_label_is_kept():
    record = classify_lease_orphan_sandbox(_sandbox(task_id=None), _task(), now=NOW)
    assert record["reap"] is False
    assert record["reason"] == "mac.task.id label is missing"


def test_unresolvable_task_is_kept_fail_closed():
    record = classify_lease_orphan_sandbox(_sandbox(), None, now=NOW)
    assert record["reap"] is False
    assert record["reason"] == "task could not be resolved from the lease store"


def test_keep_true_protects_even_when_task_terminal():
    record = classify_lease_orphan_sandbox(_sandbox(keep="true"), _task(state="completed"), now=NOW)
    assert record["reap"] is False
    assert "keep" in record["reason"]


def test_foreign_owner_and_kind_are_kept():
    assert (
        classify_lease_orphan_sandbox(_sandbox(owner="someone"), _task(state="completed"), now=NOW)[
            "reap"
        ]
        is False
    )
    assert (
        classify_lease_orphan_sandbox(
            _sandbox(kind="hubverify"), _task(state="completed"), now=NOW
        )["reap"]
        is False
    )


def test_non_managed_name_is_kept():
    record = classify_lease_orphan_sandbox(
        _sandbox(name="not-a-mac-sandbox"), _task(state="failed"), now=NOW
    )
    assert record["reap"] is False


def test_record_is_secret_free():
    record = classify_lease_orphan_sandbox(_sandbox(), _task(state="failed"), now=NOW)
    assert set(record) == {
        "name",
        "phase",
        "owner",
        "kind",
        "keep",
        "task_id",
        "lease_id",
        "reap",
        "reason",
    }


# --- candidate selection -----------------------------------------------------


def test_candidates_filter_reap_only_and_use_lookup():
    rows = [
        _sandbox("mac-task-terminal", task_id="task_done"),
        _sandbox("mac-task-live", task_id="task_live"),
        _sandbox("mac-task-unknown", task_id="task_missing"),
    ]
    tasks = {
        "task_done": _task(state="completed"),
        "task_live": _task(),
    }

    candidates = lease_orphan_task_sandbox_candidates(rows, lambda tid: tasks.get(tid), now=NOW)
    assert [c["name"] for c in candidates] == ["mac-task-terminal"]


def test_candidates_fail_closed_on_lookup_error():
    def boom(_tid):
        raise RuntimeError("hub unreachable")

    candidates = lease_orphan_task_sandbox_candidates([_sandbox()], boom, now=NOW)
    assert candidates == []


# --- reconcile driver --------------------------------------------------------


def test_reconcile_apply_deletes_only_lease_orphans(monkeypatch):
    rows = [
        _sandbox("mac-task-terminal", task_id="task_done"),
        _sandbox("mac-task-live", task_id="task_live"),
    ]
    tasks = {
        "task_done": _task(state="failed"),
        "task_live": _task(),
    }
    deletes = []

    def fake_run(argv, **_kwargs):
        if argv[2] == "list":
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        deletes.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    report = reconcile_task_sandboxes_from_lease_authority(
        lambda tid: tasks.get(tid), apply=True, now=NOW
    )
    assert report["schema"] == "mac.openshell.sandbox_lease_reconcile.v1"
    assert report["deleted"] == ["mac-task-terminal"]
    assert report["protected"] == 1
    assert deletes == ["mac-task-terminal"]


def test_reconcile_dry_run_reports_without_delete(monkeypatch):
    calls = []
    rows = [_sandbox("mac-task-terminal", task_id="task_done")]

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    report = reconcile_task_sandboxes_from_lease_authority(
        lambda _tid: _task(state="completed"), apply=False, now=NOW
    )
    assert report["dry_run"] is True
    assert [c["name"] for c in report["candidates"]] == ["mac-task-terminal"]
    assert report["deleted"] == []
    assert len(calls) == 1  # list only


def test_reconcile_apply_reports_delete_failures(monkeypatch):
    rows = [
        _sandbox("mac-task-ok", task_id="task_a"),
        _sandbox("mac-task-fail", task_id="task_b"),
    ]

    def fake_run(argv, **_kwargs):
        if argv[2] == "list":
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        if argv[-1] == "mac-task-ok":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="busy")

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    report = reconcile_task_sandboxes_from_lease_authority(
        lambda _tid: _task(state="cancelled"), apply=True, now=NOW
    )
    assert report["deleted"] == ["mac-task-ok"]
    assert report["failures"] == [{"name": "mac-task-fail", "error": "busy"}]


def test_reconcile_list_failure_raises(monkeypatch):
    def fake_run(argv, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    with pytest.raises(RuntimeError):
        reconcile_task_sandboxes_from_lease_authority(lambda _t: None, apply=True)


def test_reconcile_invalid_json_raises(monkeypatch):
    def fake_run(argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="{not json", stderr="")

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    with pytest.raises(RuntimeError):
        reconcile_task_sandboxes_from_lease_authority(lambda _t: None, apply=True)
