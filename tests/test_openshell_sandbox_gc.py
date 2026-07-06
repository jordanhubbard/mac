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
