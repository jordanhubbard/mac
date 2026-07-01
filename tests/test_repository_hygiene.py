from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac.models import ValidationError
from mac import repository_hygiene as rh
from mac.repository_hygiene import (
    DEFAULT_CLEANUP_GRACE_SECONDS,
    ManagedRepositoryRef,
    RepositoryHygieneError,
    RepositoryRefAudit,
    audit_repository_refs,
    cleanup_evidence_metadata,
    list_managed_remote_refs,
    normalize_cancellation_detail,
    parse_managed_branch,
    prune_repository_refs,
    repository_ref_lifecycle_for_transition,
)


TASK_ID = "task_" + "a" * 32
REPLACEMENT_ID = "task_" + "b" * 32
LEASE_ID = "lease_" + "c" * 18
SHA = "d" * 40
BRANCH = "mac/agent_worker/%s-%s" % (TASK_ID, LEASE_ID)
NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _cp(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _ref(**overrides):
    values = {
        "remote": "origin",
        "branch": BRANCH,
        "ref": "refs/heads/%s" % BRANCH,
        "sha": SHA,
        "task_id": TASK_ID,
        "lease_id": LEASE_ID,
    }
    values.update(overrides)
    return ManagedRepositoryRef(**values)


def _detail(
    state,
    *,
    lifecycle=None,
    lease_id=None,
    completed_at="2026-06-01T00:00:00+00:00",
    history=None,
):
    metadata = {}
    if lifecycle is not None:
        metadata["repository_ref_lifecycle"] = lifecycle
    return {
        "task": {
            "id": TASK_ID,
            "state": state,
            "lease_id": lease_id,
            "completed_at": completed_at,
            "metadata": metadata,
        },
        "history": history or [],
    }


def _audit(detail, **kwargs):
    def load(task_id):
        if task_id == REPLACEMENT_ID:
            return _detail("completed")
        return detail

    return audit_repository_refs(
        Path("."),
        [_ref()],
        load,
        now=NOW,
        open_pull_requests={},
        **kwargs,
    )[0]


def test_cancellation_detail_defaults_to_preserve():
    detail = normalize_cancellation_detail({"reason": "operator stopped it"})
    assert detail["disposition"] == "preserve"
    assert detail["cleanup_grace_seconds"] == DEFAULT_CLEANUP_GRACE_SECONDS


@pytest.mark.parametrize("disposition", ["duplicate", "superseded"])
def test_replacement_dispositions_require_replacement_task(disposition):
    with pytest.raises(ValidationError, match="replacement_task_id"):
        normalize_cancellation_detail(
            {"disposition": disposition, "reason": "replaced"}
        )


def test_auto_cleanup_disposition_requires_reason_and_valid_values():
    with pytest.raises(ValidationError, match="requires a reason"):
        normalize_cancellation_detail({"disposition": "not_applicable"})
    with pytest.raises(ValidationError, match="unsupported"):
        normalize_cancellation_detail({"disposition": "delete_everything"})
    with pytest.raises(ValidationError, match="between"):
        normalize_cancellation_detail(
            {"disposition": "preserve", "cleanup_grace_seconds": -1}
        )
    with pytest.raises(ValidationError, match="integer"):
        normalize_cancellation_detail(
            {"disposition": "preserve", "cleanup_grace_seconds": "later"}
        )
    with pytest.raises(ValidationError, match="task_<32 hex>"):
        normalize_cancellation_detail(
            {"disposition": "preserve", "replacement_task_id": "bad"}
        )


def test_cancelled_lifecycle_schedules_only_explicit_supersession():
    lifecycle = repository_ref_lifecycle_for_transition(
        "cancelled",
        {
            "disposition": "superseded",
            "replacement_task_id": REPLACEMENT_ID,
            "reason": "new implementation landed",
            "cleanup_grace_seconds": 60,
        },
        now=NOW.isoformat(),
    )
    assert lifecycle["status"] == "scheduled"
    assert lifecycle["replacement_task_id"] == REPLACEMENT_ID
    assert lifecycle["eligible_after"] == (NOW + timedelta(seconds=60)).isoformat(
        timespec="microseconds"
    )

    preserved = repository_ref_lifecycle_for_transition(
        "cancelled", {}, now=NOW.isoformat()
    )
    assert preserved["status"] == "preserved"
    assert preserved["eligible_after"] is None


def test_transition_lifecycle_covers_completed_failed_and_reopened():
    completed = repository_ref_lifecycle_for_transition(
        "completed", {"cleanup_grace_seconds": 0}, now=NOW.isoformat()
    )
    failed = repository_ref_lifecycle_for_transition(
        "failed", {"reason": "tests failed"}, now=NOW.isoformat()
    )
    reopened = repository_ref_lifecycle_for_transition(
        "open", {"reason": "retry"}, now=NOW.isoformat()
    )
    assert completed["disposition"] == "integrated"
    assert completed["eligible_after"] == NOW.isoformat(timespec="microseconds")
    assert failed["status"] == "quarantined"
    assert reopened["status"] == "active"

    cancelled_failure = repository_ref_lifecycle_for_transition(
        "cancelled",
        {"disposition": "failed_attempt"},
        now=NOW.isoformat(),
    )
    assert cancelled_failure["status"] == "quarantined"
    assert cancelled_failure["eligible_after"] is None
    assert repository_ref_lifecycle_for_transition(
        "unexpected", {}, now=NOW.isoformat()
    ) is None


def test_lifecycle_rejects_bad_timestamps_and_completed_grace():
    with pytest.raises(ValidationError, match="ISO timestamp"):
        repository_ref_lifecycle_for_transition("open", {}, now="not-a-time")
    with pytest.raises(ValidationError, match="integer"):
        repository_ref_lifecycle_for_transition(
            "completed", {"cleanup_grace_seconds": "later"}, now=NOW.isoformat()
        )
    with pytest.raises(ValidationError, match="between"):
        repository_ref_lifecycle_for_transition(
            "completed", {"cleanup_grace_seconds": 999999999}, now=NOW.isoformat()
        )


def test_managed_branch_parser_rejects_lookalikes_and_bad_shas():
    parsed = parse_managed_branch("origin", BRANCH, SHA)
    assert parsed.task_id == TASK_ID
    assert parsed.lease_id == LEASE_ID
    with pytest.raises(RepositoryHygieneError, match="outside"):
        parse_managed_branch("origin", "feature/%s" % TASK_ID, SHA)
    with pytest.raises(RepositoryHygieneError, match="invalid commit SHA"):
        parse_managed_branch("origin", BRANCH, "not-a-sha")
    with pytest.raises(RepositoryHygieneError, match="remote name"):
        parse_managed_branch("--upload-pack=bad", BRANCH, SHA)


def test_list_managed_remote_refs_filters_unsafe_namespaces(tmp_path):
    output = "\n".join(
        [
            "%s\trefs/heads/%s" % (SHA, BRANCH),
            "malformed",
            "%s\trefs/tags/not-a-branch" % ("f" * 40),
            "%s\trefs/heads/mac/not-an-agent/%s-%s" % (
                "e" * 40,
                TASK_ID,
                LEASE_ID,
            ),
        ]
    )

    def runner(_repo, argv, timeout):
        assert argv[:3] == ["git", "ls-remote", "--heads"]
        assert timeout == 60
        return _cp(argv, stdout=output)

    refs = list_managed_remote_refs(tmp_path, runner=runner)
    assert refs == [_ref()]

    with pytest.raises(RepositoryHygieneError, match="remote name"):
        list_managed_remote_refs(tmp_path, remote="bad remote", runner=runner)


def test_list_managed_remote_refs_redacts_authenticated_url(tmp_path):
    def runner(_repo, argv, timeout):
        return _cp(
            argv,
            returncode=1,
            stderr="fatal: https://user:top-secret@example.invalid/repo",
        )

    with pytest.raises(RepositoryHygieneError) as exc_info:
        list_managed_remote_refs(tmp_path, runner=runner)
    message = str(exc_info.value)
    assert "top-secret" not in message
    assert "<redacted>@example.invalid" in message


def test_default_runner_normalizes_tool_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    with pytest.raises(RepositoryHygieneError, match="unavailable"):
        rh._run(tmp_path, ["git", "status"])

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("git", 1)
        ),
    )
    with pytest.raises(RepositoryHygieneError, match="timed out"):
        rh._run(tmp_path, ["git", "status"])

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(RepositoryHygieneError, match="failed"):
        rh._run(tmp_path, ["git", "status"])


def test_time_and_detail_normalization_edges():
    assert rh._parse_time("") is None
    assert rh._parse_time("invalid") is None
    assert rh._parse_time("2026-07-01T00:00:00").tzinfo is not None
    wrapped = SimpleNamespace(to_dict=lambda: {"task": {"state": "open"}})
    task, history = rh._task_parts(wrapped)
    assert task["state"] == "open"
    assert history == []
    assert rh._task_parts("invalid") == ({}, [])
    assert rh._last_cancellation_disposition(
        [
            {"to_state": "open", "detail": {}},
            {"to_state": "cancelled", "detail": {"disposition": "deferred"}},
        ]
    ) == "deferred"
    assert rh._last_cancellation_disposition(
        [{"to_state": "cancelled", "detail": "bad"}]
    ) == ""
    assert rh._is_ancestor(Path("."), SHA, "", runner=lambda *_a, **_k: _cp([])) is False


@pytest.mark.parametrize(
    ("detail", "classification", "eligible"),
    [
        (_detail("open"), "active", False),
        (_detail("blocked"), "blocked", False),
        (_detail("failed"), "quarantined", False),
        (_detail("cancelled"), "deferred", False),
        (_detail("running", lease_id="lease_live"), "active", False),
    ],
)
def test_audit_preserves_non_terminal_and_ambiguous_refs(
    detail, classification, eligible
):
    result = _audit(detail)
    assert result.classification == classification
    assert result.eligible is eligible


def test_audit_marks_explicit_supersession_eligible_after_grace():
    lifecycle = {
        "disposition": "superseded",
        "eligible_after": "2026-06-30T00:00:00+00:00",
        "replacement_task_id": REPLACEMENT_ID,
        "reason": "replacement merged",
    }
    result = _audit(_detail("cancelled", lifecycle=lifecycle))
    assert result.classification == "superseded"
    assert result.eligible is True
    assert result.replacement_task_id == REPLACEMENT_ID


def test_audit_requires_completed_replacement_for_superseded_work():
    lifecycle = {
        "disposition": "superseded",
        "eligible_after": "2026-06-30T00:00:00+00:00",
        "replacement_task_id": REPLACEMENT_ID,
    }

    def load(task_id):
        return _detail("open") if task_id == REPLACEMENT_ID else _detail(
            "cancelled", lifecycle=lifecycle
        )

    result = audit_repository_refs(
        Path("."),
        [_ref()],
        load,
        now=NOW,
        open_pull_requests={},
    )[0]
    assert result.classification == "superseded"
    assert result.eligible is False
    assert "replacement task" in result.reason


def test_audit_uses_terminal_time_when_lifecycle_has_no_due_date():
    history = [
        {
            "to_state": "cancelled",
            "detail": {"disposition": "not_applicable"},
        }
    ]
    result = _audit(
        _detail(
            "cancelled",
            completed_at="2026-06-01T00:00:00+00:00",
            history=history,
        )
    )
    assert result.classification == "superseded"
    assert result.eligible is True
    assert result.eligible_after.startswith("2026-06-08")


def test_audit_holds_supersession_during_grace_or_open_pr():
    lifecycle = {
        "disposition": "not_applicable",
        "eligible_after": "2026-07-02T00:00:00+00:00",
    }
    assert _audit(_detail("cancelled", lifecycle=lifecycle)).eligible is False

    lifecycle["eligible_after"] = "2026-06-01T00:00:00+00:00"
    result = audit_repository_refs(
        Path("."),
        [_ref()],
        lambda _task_id: _detail("cancelled", lifecycle=lifecycle),
        now=NOW,
        open_pull_requests={BRANCH: "https://example.invalid/pr/1"},
    )[0]
    assert result.eligible is False
    assert result.open_pull_request.endswith("/1")


def test_audit_fails_closed_when_pull_request_check_is_unavailable():
    lifecycle = {
        "disposition": "not_applicable",
        "eligible_after": "2026-06-01T00:00:00+00:00",
    }
    result = audit_repository_refs(
        Path("."),
        [_ref()],
        lambda _task_id: _detail("cancelled", lifecycle=lifecycle),
        now=NOW,
        open_pull_requests=None,
    )[0]
    assert result.eligible is False
    assert "not verified" in result.reason


def test_audit_unknown_task_and_reopened_task_are_never_eligible():
    missing = audit_repository_refs(
        Path("."),
        [_ref()],
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("missing")),
        now=NOW,
        open_pull_requests={},
    )[0]
    assert missing.classification == "unknown"
    assert missing.eligible is False

    stale_lifecycle = {
        "disposition": "superseded",
        "eligible_after": "2026-06-01T00:00:00+00:00",
        "replacement_task_id": REPLACEMENT_ID,
    }
    reopened = _audit(_detail("open", lifecycle=stale_lifecycle))
    assert reopened.classification == "active"
    assert reopened.eligible is False


def test_completed_ref_requires_ancestry_proof():
    lifecycle = {
        "disposition": "integrated",
        "eligible_after": "2026-06-01T00:00:00+00:00",
    }

    def merged(_repo, argv, timeout):
        assert argv[:3] == ["git", "merge-base", "--is-ancestor"]
        return _cp(argv)

    result = _audit(_detail("completed", lifecycle=lifecycle), runner=merged)
    assert result.classification == "merged"
    assert result.eligible is True

    def not_merged(_repo, argv, timeout):
        return _cp(argv, returncode=1)

    result = _audit(_detail("completed", lifecycle=lifecycle), runner=not_merged)
    assert result.classification == "unknown"
    assert result.eligible is False

    no_due = _audit(
        _detail("completed", lifecycle={"disposition": "integrated"}),
        runner=merged,
    )
    assert no_due.eligible is True
    assert no_due.eligible_after.startswith("2026-06-08")


def test_audit_unknown_state_is_preserved():
    result = _audit(_detail("archived"))
    assert result.classification == "unknown"
    assert result.eligible is False


def _eligible_audit():
    return RepositoryRefAudit(
        **_ref().to_dict(),
        task_state="cancelled",
        disposition="superseded",
        classification="superseded",
        eligible=True,
        eligible_after="2026-06-01T00:00:00+00:00",
        reason="replacement merged",
        replacement_task_id=REPLACEMENT_ID,
    )


def test_prune_defaults_to_read_only_dry_run(tmp_path):
    called = False

    def runner(_repo, _argv, timeout):
        nonlocal called
        called = True
        return _cp([])

    result = prune_repository_refs(tmp_path, [_eligible_audit()], runner=runner)
    assert result["mode"] == "dry-run"
    assert result["count"] == 1
    assert result["deleted"] == []
    assert called is False


def test_execute_with_no_candidates_is_a_noop(tmp_path):
    result = prune_repository_refs(tmp_path, [], execute=True)
    assert result == {
        "schema": "mac.repository_ref_cleanup.v1",
        "mode": "execute",
        "eligible": [],
        "deleted": [],
        "count": 0,
    }


def test_prune_refuses_multiple_remotes(tmp_path):
    second = RepositoryRefAudit(
        **{**_eligible_audit().to_dict(), "remote": "upstream"}
    )
    with pytest.raises(RepositoryHygieneError, match="span git remotes"):
        prune_repository_refs(
            tmp_path, [_eligible_audit(), second], execute=True
        )


def test_prune_revalidates_sha_and_uses_atomic_force_with_lease(tmp_path):
    exists = True
    commands = []
    recorded = []

    def runner(_repo, argv, timeout):
        nonlocal exists
        commands.append(argv)
        if argv[1:3] == ["ls-remote", "--heads"]:
            stdout = "%s\trefs/heads/%s\n" % (SHA, BRANCH) if exists else ""
            return _cp(argv, stdout=stdout)
        if argv[1] == "push":
            assert "--atomic" in argv
            assert "--force-with-lease=refs/heads/%s:%s" % (BRANCH, SHA) in argv
            assert ":refs/heads/%s" % BRANCH in argv
            exists = False
            return _cp(argv, stdout="ok")
        if argv[1] == "update-ref":
            return _cp(argv)
        raise AssertionError(argv)

    result = prune_repository_refs(
        tmp_path,
        [_eligible_audit()],
        execute=True,
        runner=runner,
        recorder=lambda item, action, error: recorded.append(
            (item.branch, action, error)
        ),
    )
    assert result["count"] == 1
    assert recorded == [(BRANCH, "requested", ""), (BRANCH, "deleted", "")]
    assert any(command[1] == "update-ref" for command in commands)


def test_prune_refuses_a_sha_race_before_recording_or_push(tmp_path):
    recorded = []

    def runner(_repo, argv, timeout):
        return _cp(argv, stdout="%s\trefs/heads/%s\n" % ("e" * 40, BRANCH))

    with pytest.raises(RepositoryHygieneError, match="changed after audit"):
        prune_repository_refs(
            tmp_path,
            [_eligible_audit()],
            execute=True,
            runner=runner,
            recorder=lambda *args: recorded.append(args),
        )
    assert recorded == []


def test_prune_redacts_push_failure_in_error_and_evidence(tmp_path):
    recorded = []

    def runner(_repo, argv, timeout):
        if argv[1:3] == ["ls-remote", "--heads"]:
            return _cp(argv, stdout="%s\trefs/heads/%s\n" % (SHA, BRANCH))
        return _cp(
            argv,
            returncode=1,
            stderr="https://operator:secret-token@example.invalid denied",
        )

    with pytest.raises(RepositoryHygieneError) as exc_info:
        prune_repository_refs(
            tmp_path,
            [_eligible_audit()],
            execute=True,
            runner=runner,
            recorder=lambda item, action, error: recorded.append((action, error)),
        )
    assert "secret-token" not in str(exc_info.value)
    assert [action for action, _error in recorded] == ["requested", "failed"]
    assert "secret-token" not in recorded[-1][1]


def test_prune_normalizes_remote_revalidation_failure(tmp_path):
    def runner(_repo, argv, timeout):
        return _cp(
            argv,
            returncode=1,
            stderr="https://user:secret@example.invalid unavailable",
        )

    with pytest.raises(RepositoryHygieneError) as exc_info:
        prune_repository_refs(
            tmp_path,
            [_eligible_audit()],
            execute=True,
            runner=runner,
        )
    assert "secret" not in str(exc_info.value)


def test_prune_detects_remote_that_still_exists_after_success(tmp_path):
    calls = 0

    def runner(_repo, argv, timeout):
        nonlocal calls
        if argv[1:3] == ["ls-remote", "--heads"]:
            calls += 1
            return _cp(argv, stdout="%s\trefs/heads/%s\n" % (SHA, BRANCH))
        return _cp(argv)

    with pytest.raises(RepositoryHygieneError, match="still exists"):
        prune_repository_refs(
            tmp_path,
            [_eligible_audit()],
            execute=True,
            runner=runner,
        )
    assert calls == 2


def test_cleanup_metadata_is_secret_safe_and_complete():
    metadata = cleanup_evidence_metadata(
        _eligible_audit(),
        "failed",
        at=NOW,
        error="https://user:password@example.invalid failed",
    )
    assert metadata["schema"] == "mac.repository_ref_cleanup.v1"
    assert metadata["sha"] == SHA
    assert metadata["replacement_task_id"] == REPLACEMENT_ID
    assert "password" not in metadata["error"]
