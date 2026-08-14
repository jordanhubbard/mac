from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mac.openshell_sandbox_gc import (
    reconcile_stale_sandboxes,
    stale_sandbox_candidates,
)


NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def _sandbox(name: str, *, age_hours: int = 48, labels=None, phase="Ready"):
    created = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    if age_hours != 48:
        created = NOW - timedelta(hours=age_hours)
    return {
        "name": name,
        "phase": phase,
        "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
        "labels": labels or {},
    }


def test_candidates_accept_old_managed_and_legacy_names_only():
    rows = [
        _sandbox(
            "mac-task-deadbeef",
            labels={"mac.owner": "mac", "mac.kind": "task", "mac.pid": "991"},
        ),
        _sandbox("mac-hubverify-cafebabe"),
        _sandbox("someone-elses-sandbox"),
        _sandbox("mac-task-too-young", age_hours=2),
        _sandbox("mac-task-running", phase="Running"),
    ]

    candidates = stale_sandbox_candidates(
        rows,
        now=NOW,
        stale_after_seconds=24 * 3600,
        pid_is_alive=lambda _pid: False,
    )

    assert [row["name"] for row in candidates] == [
        "mac-task-deadbeef",
        "mac-hubverify-cafebabe",
    ]
    assert candidates[0]["legacy"] is False
    assert candidates[1]["legacy"] is True


def test_candidates_protect_live_creator_kept_and_foreign_labels():
    rows = [
        _sandbox(
            "mac-task-live",
            labels={"mac.owner": "mac", "mac.pid": "42", "mac.keep": "false"},
        ),
        _sandbox(
            "mac-task-kept",
            labels={"mac.owner": "mac", "mac.pid": "99", "mac.keep": "true"},
        ),
        _sandbox(
            "mac-task-foreign",
            labels={"mac.owner": "another-system", "mac.pid": "99"},
        ),
    ]

    assert stale_sandbox_candidates(
        rows,
        now=NOW,
        stale_after_seconds=0,
        pid_is_alive=lambda pid: pid == 42,
    ) == []


def test_candidates_can_exclude_unlabeled_legacy_sandboxes():
    assert stale_sandbox_candidates(
        [_sandbox("mac-task-legacy")],
        now=NOW,
        stale_after_seconds=0,
        include_legacy=False,
    ) == []


def test_reconcile_dry_run_does_not_delete(monkeypatch):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([_sandbox("mac-task-old")]),
            stderr="",
        )

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    report = reconcile_stale_sandboxes(
        stale_after_seconds=0,
        apply=False,
        now=NOW,
    )

    assert report["dry_run"] is True
    assert [row["name"] for row in report["candidates"]] == ["mac-task-old"]
    assert report["deleted"] == []
    assert len(calls) == 1


def test_reconcile_apply_reports_delete_failures(monkeypatch):
    def fake_run(argv, **_kwargs):
        if argv[2] == "list":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [_sandbox("mac-task-ok"), _sandbox("mac-task-fail")]
                ),
                stderr="",
            )
        if argv[-1] == "mac-task-ok":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="busy")

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    report = reconcile_stale_sandboxes(
        stale_after_seconds=0,
        apply=True,
        now=NOW,
    )

    assert report["deleted"] == ["mac-task-ok"]
    assert report["failures"] == [{"name": "mac-task-fail", "error": "busy"}]


# --- Fail-closed dead-PID orphan reaper --------------------------------------

from mac.openshell_sandbox_gc import (  # noqa: E402
    classify_orphan_task_sandbox,
    orphan_task_sandbox_candidates,
    reap_orphaned_task_sandboxes,
)


def _orphan(name="mac-task-deadbeef", *, owner="mac", kind="task", keep="false",
            pid="991", phase="Ready"):
    labels = {}
    if owner is not None:
        labels["mac.owner"] = owner
    if kind is not None:
        labels["mac.kind"] = kind
    if keep is not None:
        labels["mac.keep"] = keep
    if pid is not None:
        labels["mac.pid"] = pid
    return {"name": name, "phase": phase, "labels": labels}


def test_reaper_reaps_exact_dead_pid_task_sandbox():
    record = classify_orphan_task_sandbox(_orphan(), pid_is_alive=lambda _p: False)
    assert record["reap"] is True
    assert "dead recorded PID" in record["reason"]
    # Secret-free evidence: only ownership signals, no arbitrary label values.
    assert set(record) == {"name", "phase", "owner", "kind", "keep", "pid", "reap", "reason"}


def test_reaper_protects_live_pid_race():
    # The recorded creator PID is still alive (task is mid-flight): never reap.
    record = classify_orphan_task_sandbox(
        _orphan(pid="42"), pid_is_alive=lambda pid: pid == 42
    )
    assert record["reap"] is False
    assert record["reason"] == "recorded creator PID is still alive"


def test_reaper_protects_against_pid_reuse_when_alive():
    # A recycled PID that now belongs to an unrelated live process still reads as
    # alive, so fail-closed protection holds and the sandbox is preserved.
    seen = {}

    def alive(pid):
        seen["pid"] = pid
        return True

    candidates = orphan_task_sandbox_candidates([_orphan(pid="1234")], pid_is_alive=alive)
    assert candidates == []
    assert seen["pid"] == 1234


def test_reaper_protects_keep_true():
    record = classify_orphan_task_sandbox(
        _orphan(keep="true"), pid_is_alive=lambda _p: False
    )
    assert record["reap"] is False
    assert "mac.keep is truthy" in record["reason"]


def test_reaper_protects_missing_keep_label():
    record = classify_orphan_task_sandbox(
        _orphan(keep=None), pid_is_alive=lambda _p: False
    )
    assert record["reap"] is False
    assert "mac.keep is missing or not explicitly falsey" in record["reason"]


def test_reaper_protects_foreign_owner():
    record = classify_orphan_task_sandbox(
        _orphan(owner="another-system"), pid_is_alive=lambda _p: False
    )
    assert record["reap"] is False
    assert record["reason"] == "mac.owner is not exactly 'mac'"


def test_reaper_protects_missing_kind():
    record = classify_orphan_task_sandbox(
        _orphan(kind=None), pid_is_alive=lambda _p: False
    )
    assert record["reap"] is False
    assert "mac.kind is missing" in record["reason"]


def test_reaper_protects_malformed_pid_label():
    for bad in ("not-a-number", "", "0", "-3"):
        record = classify_orphan_task_sandbox(
            _orphan(pid=bad), pid_is_alive=lambda _p: False
        )
        assert record["reap"] is False, bad


def test_reaper_protects_non_managed_name():
    record = classify_orphan_task_sandbox(
        _orphan(name="someone-elses-sandbox"), pid_is_alive=lambda _p: False
    )
    assert record["reap"] is False
    assert "not an exact MAC-managed sandbox" in record["reason"]


def test_reaper_does_not_require_ready_phase_but_still_needs_dead_pid():
    # The reaper is phase-agnostic: a leaked sandbox in any phase with a dead
    # owner PID is reapable. Liveness is the real gate.
    record = classify_orphan_task_sandbox(
        _orphan(phase="Stopped"), pid_is_alive=lambda _p: False
    )
    assert record["reap"] is True


def test_reaper_apply_deletes_only_dead_pid_orphans(monkeypatch):
    rows = [
        _orphan("mac-task-dead", pid="10"),
        _orphan("mac-task-live", pid="20"),
        _orphan("mac-task-kept", pid="30", keep="true"),
        _orphan("someone-else", pid="40"),
    ]
    deletes = []

    def fake_run(argv, **_kwargs):
        if argv[2] == "list":
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        deletes.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    report = reap_orphaned_task_sandboxes(apply=True, pid_is_alive=lambda pid: pid == 20)

    assert report["schema"] == "mac.openshell.sandbox_orphan_reap.v1"
    assert report["deleted"] == ["mac-task-dead"]
    assert report["protected"] == 3
    assert deletes == ["mac-task-dead"]


def test_reaper_idempotent_second_pass_finds_nothing(monkeypatch):
    state = {"rows": [_orphan("mac-task-dead", pid="10")]}

    def fake_run(argv, **_kwargs):
        if argv[2] == "list":
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(state["rows"]), stderr=""
            )
        # Simulate deletion by dropping the sandbox from the listing.
        state["rows"] = [r for r in state["rows"] if r["name"] != argv[-1]]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    first = reap_orphaned_task_sandboxes(apply=True, pid_is_alive=lambda _p: False)
    assert first["deleted"] == ["mac-task-dead"]

    second = reap_orphaned_task_sandboxes(apply=True, pid_is_alive=lambda _p: False)
    assert second["deleted"] == []
    assert second["candidates"] == []


def test_reaper_dry_run_reports_but_does_not_delete(monkeypatch):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([_orphan("mac-task-dead", pid="10")]),
            stderr="",
        )

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    report = reap_orphaned_task_sandboxes(apply=False, pid_is_alive=lambda _p: False)
    assert report["dry_run"] is True
    assert [row["name"] for row in report["candidates"]] == ["mac-task-dead"]
    assert report["deleted"] == []
    assert len(calls) == 1  # only the list call, no delete


def test_reaper_apply_reports_delete_failures(monkeypatch):
    rows = [_orphan("mac-task-ok", pid="10"), _orphan("mac-task-fail", pid="11")]

    def fake_run(argv, **_kwargs):
        if argv[2] == "list":
            return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
        if argv[-1] == "mac-task-ok":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="busy")

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    report = reap_orphaned_task_sandboxes(apply=True, pid_is_alive=lambda _p: False)
    assert report["deleted"] == ["mac-task-ok"]
    assert report["failures"] == [{"name": "mac-task-fail", "error": "busy"}]


def test_reaper_list_failure_raises(monkeypatch):
    def fake_run(argv, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr("mac.openshell_sandbox_gc.subprocess.run", fake_run)

    import pytest

    with pytest.raises(RuntimeError):
        reap_orphaned_task_sandboxes(apply=True)


def test_a_sandbox_whose_creator_is_dead_is_reaped_immediately():
    """The leak. A dead creator PID is PROOF the sandbox is garbage, but the
    age gate was tested first, so a provably abandoned sandbox was protected
    for a full day anyway.

    That is not an inefficiency. The hub creates one sandbox per verification,
    ticks every 30 seconds, and kills the creator when the gate exceeds its
    timeout -- so every tick left a Ready sandbox nothing would collect for 24
    hours. Observed on the hub: 10 abandoned sandboxes, then 87 within a few
    hours of the tick resuming, each holding a container.
    """
    rows = [
        _sandbox(
            "mac-hubverify-1059c4c10c254",
            age_hours=0,
            labels={"mac.owner": "mac", "mac.kind": "hubverify", "mac.pid": "424242"},
        )
    ]

    candidates = stale_sandbox_candidates(
        rows, now=NOW, pid_is_alive=lambda pid: False
    )

    assert [row["name"] for row in candidates] == ["mac-hubverify-1059c4c10c254"]


def test_a_live_creator_still_protects_a_brand_new_sandbox():
    """The reason the age gate existed. A verification in flight must not have
    its sandbox pulled out from under it."""
    rows = [
        _sandbox(
            "mac-hubverify-inflight0000000",
            age_hours=0,
            labels={"mac.owner": "mac", "mac.kind": "hubverify", "mac.pid": "1"},
        )
    ]

    candidates = stale_sandbox_candidates(rows, now=NOW, pid_is_alive=lambda pid: True)

    assert candidates == []


def test_an_unlabelled_sandbox_still_waits_out_the_age_threshold():
    """No creator to prove anything about, so the heuristic is all there is.
    Reaping these on sight would delete work belonging to something we cannot
    see."""
    rows = [_sandbox("mac-task-abcdef0123456789", age_hours=0, labels={})]

    candidates = stale_sandbox_candidates(rows, now=NOW, pid_is_alive=lambda pid: False)

    assert candidates == []


def test_an_errored_sandbox_is_collectable_at_all():
    """The dominant leak, and the simplest one: the phase filter accepted
    "ready" and nothing else, so a sandbox that died on its way up could never
    be collected at any age.

    Measured on the hub: 77 sandboxes, 60 in Error, 49 of those from hub
    verification -- none collectable, ever.
    """
    rows = [
        _sandbox(
            "mac-hubverify-b68620c32a5a4a05",
            age_hours=1,
            phase="Error",
            labels={"mac.owner": "mac", "mac.kind": "hubverify", "mac.pid": "1"},
        )
    ]

    candidates = stale_sandbox_candidates(rows, now=NOW, pid_is_alive=lambda pid: True)

    assert [row["name"] for row in candidates] == ["mac-hubverify-b68620c32a5a4a05"]


def test_an_errored_sandbox_keeps_a_grace_window_for_its_logs():
    """It will never be useful again, but whoever created it may still be
    reading why it failed."""
    rows = [
        _sandbox(
            "mac-hubverify-freshfailure00",
            age_hours=0,
            phase="Error",
            labels={"mac.owner": "mac", "mac.kind": "hubverify", "mac.pid": "1"},
        )
    ]

    candidates = stale_sandbox_candidates(rows, now=NOW, pid_is_alive=lambda pid: True)

    assert candidates == []


def test_a_working_sandbox_still_gets_the_full_stale_window():
    """The grace window is short because an errored sandbox is terminal. A
    Ready one may be doing work, and keeps the long threshold."""
    rows = [
        _sandbox(
            "mac-task-workinghard0000",
            age_hours=1,
            phase="Ready",
            labels={"mac.owner": "mac", "mac.pid": "1"},
        )
    ]

    candidates = stale_sandbox_candidates(rows, now=NOW, pid_is_alive=lambda pid: True)

    assert candidates == []
