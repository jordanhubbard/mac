from __future__ import annotations

import subprocess
import threading
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
    RepositoryRefAuditResult,
    audit_repository_refs,
    audit_repository_refs_result,
    cleanup_evidence_metadata,
    list_managed_remote_refs,
    normalize_cancellation_detail,
    parse_managed_branch,
    prune_repository_refs,
    query_open_pull_requests,
    refresh_remote_base_ref,
    repository_ref_lifecycle_for_transition,
    retire_protected_remote_ref_exact,
    resolve_remote_base_ref,
    verify_repository_remote,
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


def test_protected_ref_cleanup_rejects_sha_mismatch_before_push(tmp_path):
    ref = "refs/mac/integration/batch-1"
    calls = []

    def runner(_repo, argv, timeout):
        calls.append(list(argv))
        return _cp(argv, stdout="%s\t%s\n" % ("e" * 40, ref))

    with pytest.raises(RepositoryHygieneError, match="changed identity"):
        retire_protected_remote_ref_exact(
            tmp_path,
            "origin",
            ref,
            "d" * 40,
            execute=True,
            runner=runner,
        )

    assert len(calls) == 1
    assert calls[0][1] == "ls-remote"


def test_protected_ref_cleanup_uses_exact_sha_lease_and_readback(tmp_path):
    ref = "refs/mac/attempts/package/e1/change/a1-lease"
    sha = "d" * 40
    calls = []
    observations = iter(["%s\t%s\n" % (sha, ref), ""])

    def runner(_repo, argv, timeout):
        calls.append(list(argv))
        if argv[1] == "ls-remote":
            return _cp(argv, stdout=next(observations))
        return _cp(argv, stdout="ok")

    outcome = retire_protected_remote_ref_exact(
        tmp_path,
        "origin",
        ref,
        sha,
        execute=True,
        runner=runner,
    )

    assert outcome == "deleted"
    push = calls[1]
    assert "--force-with-lease=%s:%s" % (ref, sha) in push
    assert ":%s" % ref in push


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
            {
                "disposition": "preserve",
                "reason": "invalid grace fixture",
                "cleanup_grace_seconds": -1,
            }
        )
    with pytest.raises(ValidationError, match="integer"):
        normalize_cancellation_detail(
            {
                "disposition": "preserve",
                "reason": "invalid grace fixture",
                "cleanup_grace_seconds": "later",
            }
        )
    with pytest.raises(ValidationError, match="task_<32 hex>"):
        normalize_cancellation_detail(
            {
                "disposition": "preserve",
                "reason": "invalid replacement fixture",
                "replacement_task_id": "bad",
            }
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
        "cancelled",
        {"reason": "operator stopped the task"},
        now=NOW.isoformat(),
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
        {"disposition": "failed_attempt", "reason": "execution failed"},
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


def test_query_open_pull_requests_uses_distinct_failure_state(tmp_path):
    payload = (
        '[{"headRefName":"branch","number":7,'
        '"url":"https://example.invalid/pull/7"}]'
    )

    def success(argv, **kwargs):
        assert argv[:4] == ["gh", "pr", "list", "--state"]
        assert kwargs["cwd"] == str(tmp_path.resolve())
        return _cp(argv, stdout=payload)

    heads, warning = query_open_pull_requests(tmp_path, runner=success)
    assert warning == ""
    assert heads == {"branch": "https://example.invalid/pull/7"}

    heads, warning = query_open_pull_requests(
        tmp_path,
        runner=lambda *args, **kwargs: _cp([], returncode=1),
    )
    assert heads is None
    assert "could not be verified" in warning


def test_verify_repository_remote_accepts_transport_equivalence(tmp_path):
    def runner(_repo, argv, timeout):
        assert argv == ["git", "remote", "get-url", "origin"]
        assert timeout == 30
        return _cp(argv, stdout="https://github.com/example/project.git\n")

    verify_repository_remote(
        tmp_path,
        "origin",
        "git@github.com:example/project.git",
        runner=runner,
    )

    with pytest.raises(RepositoryHygieneError, match="does not match"):
        verify_repository_remote(
            tmp_path,
            "origin",
            "git@github.com:example/other.git",
            runner=runner,
        )


def test_verify_repository_remote_rejects_invalid_inputs(tmp_path):
    with pytest.raises(RepositoryHygieneError, match="remote name"):
        verify_repository_remote(tmp_path, "bad remote", "git@github.com:a/b.git")
    with pytest.raises(RepositoryHygieneError, match="canonical"):
        verify_repository_remote(tmp_path, "origin", "not-a-url")

    def missing(_repo, argv, timeout):
        return _cp(argv, returncode=1)

    with pytest.raises(RepositoryHygieneError, match="could not resolve"):
        verify_repository_remote(
            tmp_path,
            "origin",
            "git@github.com:a/b.git",
            runner=missing,
        )


def test_resolve_remote_base_ref_uses_local_or_advertised_head(tmp_path):
    def local(_repo, argv, timeout):
        if argv[1] == "symbolic-ref":
            return _cp(argv, stdout="origin/trunk\n")
        if argv[1] == "check-ref-format":
            return _cp(argv)
        raise AssertionError(argv)

    assert resolve_remote_base_ref(tmp_path, runner=local) == "origin/trunk"

    def advertised(_repo, argv, timeout):
        if argv[1] == "symbolic-ref":
            return _cp(argv, returncode=1)
        if argv[1] == "ls-remote":
            return _cp(argv, stdout="ref: refs/heads/release\tHEAD\n%s\tHEAD\n" % SHA)
        if argv[1] == "check-ref-format":
            return _cp(argv)
        raise AssertionError(argv)

    assert resolve_remote_base_ref(tmp_path, runner=advertised) == "origin/release"


def test_resolve_and_refresh_base_ref_fail_closed(tmp_path):
    def checked(_repo, argv, timeout):
        if argv[1] == "check-ref-format":
            return _cp(argv)
        if argv[1] == "fetch":
            assert argv[-1] == "+refs/heads/main:refs/remotes/origin/main"
            return _cp(argv)
        raise AssertionError(argv)

    assert resolve_remote_base_ref(
        tmp_path,
        configured="origin/main",
        runner=checked,
    ) == "origin/main"
    assert refresh_remote_base_ref(
        tmp_path,
        "origin",
        "origin/main",
        runner=checked,
    ) == "origin/main"

    with pytest.raises(RepositoryHygieneError, match="belong to remote"):
        resolve_remote_base_ref(
            tmp_path,
            configured="upstream/main",
            runner=checked,
        )

    def failed_fetch(_repo, argv, timeout):
        if argv[1] == "check-ref-format":
            return _cp(argv)
        return _cp(
            argv,
            returncode=1,
            stderr="https://user:secret@example.invalid unavailable",
        )

    with pytest.raises(RepositoryHygieneError) as exc_info:
        refresh_remote_base_ref(
            tmp_path,
            "origin",
            "origin/main",
            runner=failed_fetch,
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


# ---------------------------------------------------------------------------
# Bounded / batched audit regression coverage.
#
# These exercise the bounded, observable audit exposed by
# ``audit_repository_refs_result``: a slow or stalled hub task_loader must never
# exceed the overall deadline or hang, every ref is still audited, timed-out or
# unavailable tasks fail closed (never ``eligible=True``), task-detail reads are
# deduped/cached (at most one loader call per task_id), and a KeyboardInterrupt
# yields a clean partial report. Fake loaders use ``threading.Event``/sleep and
# call counters instead of real HTTP, and a deterministic monotonic clock hook
# keeps the deadline logic hermetic.
# ---------------------------------------------------------------------------


def _fake_clock(start=0.0):
    """Deterministic ``monotonic`` hook whose value only advances on demand."""

    state = {"t": float(start)}

    def clock():
        return state["t"]

    def advance(delta):
        state["t"] += float(delta)

    return clock, advance


def _multi_ref(task_id, *, branch_suffix, sha=None):
    suffix = str(branch_suffix)
    branch = "mac/agent_worker/%s-%s" % (task_id, suffix)
    return ManagedRepositoryRef(
        remote="origin",
        branch=branch,
        ref="refs/heads/%s" % branch,
        sha=sha or SHA,
        task_id=task_id,
        lease_id=LEASE_ID,
    )


def _stall_event():
    """A threading.Event that a fake loader can block on until released."""

    return threading.Event()


def test_slow_hub_stays_within_deadline_and_reports_timeout():
    # A task_loader that sleeps longer than the per-task timeout must not push
    # the audit past its overall deadline; the audit still returns audits for
    # every ref plus a warning and the timed-out task IDs.
    release = _stall_event()

    def slow_loader(task_id):
        # Block far longer than the per-task timeout; the bounded loader must
        # cancel/abandon this lookup instead of waiting the full sleep.
        release.wait(timeout=5.0)
        return _detail("completed")

    clock, _advance = _fake_clock()
    try:
        result = audit_repository_refs_result(
            Path("."),
            [_ref()],
            slow_loader,
            now=NOW,
            open_pull_requests={},
            per_task_timeout_seconds=0.05,
            audit_deadline_seconds=1.0,
            monotonic=clock,
        )
    finally:
        release.set()

    assert isinstance(result, RepositoryRefAuditResult)
    assert len(result.audits) == 1
    audit = result.audits[0]
    assert audit.eligible is False
    assert TASK_ID in result.timed_out_task_ids
    assert result.warning
    assert "timed out" in audit.reason


def test_one_stalled_task_does_not_block_other_refs():
    # With multiple refs where exactly one task_loader call stalls, the other
    # refs audit normally and the stalled ref is reported ineligible with a
    # clear unavailable/timed-out reason.
    stalled_task = "task_" + "e" * 32
    healthy_task = "task_" + "f" * 32
    release = _stall_event()

    def loader(task_id):
        if task_id == stalled_task:
            release.wait(timeout=5.0)
            return _detail("completed")
        return _detail("open")

    def merged(_repo, argv, timeout):
        return _cp(argv)

    refs = [
        _multi_ref(healthy_task, branch_suffix="h"),
        _multi_ref(stalled_task, branch_suffix="s"),
    ]
    clock, _advance = _fake_clock()
    try:
        result = audit_repository_refs_result(
            Path("."),
            refs,
            loader,
            now=NOW,
            open_pull_requests={},
            runner=merged,
            per_task_timeout_seconds=0.05,
            audit_deadline_seconds=1.0,
            monotonic=clock,
        )
    finally:
        release.set()

    by_task = {audit.task_id: audit for audit in result.audits}
    assert set(by_task) == {healthy_task, stalled_task}

    healthy = by_task[healthy_task]
    assert healthy.task_state == "open"
    assert healthy.classification != "unknown"

    stalled = by_task[stalled_task]
    assert stalled.eligible is False
    assert stalled_task in result.timed_out_task_ids
    assert "timed out" in stalled.reason


def test_partial_result_structure_is_complete_and_does_not_hang():
    # The returned structure includes audits for every ref plus the timed-out
    # task IDs and a warning, instead of raising or hanging.
    stalled_task = "task_" + "e" * 32
    ok_task = "task_" + "f" * 32
    release = _stall_event()

    def loader(task_id):
        if task_id == stalled_task:
            release.wait(timeout=5.0)
        return _detail("open")

    refs = [
        _multi_ref(ok_task, branch_suffix="ok"),
        _multi_ref(stalled_task, branch_suffix="stall"),
    ]
    clock, _advance = _fake_clock()
    try:
        result = audit_repository_refs_result(
            Path("."),
            refs,
            loader,
            now=NOW,
            open_pull_requests={},
            per_task_timeout_seconds=0.05,
            audit_deadline_seconds=1.0,
            monotonic=clock,
        )
    finally:
        release.set()

    assert isinstance(result, RepositoryRefAuditResult)
    assert len(result.audits) == len(refs)
    assert {audit.task_id for audit in result.audits} == {ok_task, stalled_task}
    assert stalled_task in result.timed_out_task_ids
    assert ok_task not in result.timed_out_task_ids
    assert result.warning
    # Serializable partial report (never raised / hung).
    payload = result.to_dict()
    assert len(payload["audits"]) == len(refs)
    assert payload["timed_out_task_ids"] == list(result.timed_out_task_ids)
    assert payload["warning"] == result.warning


def test_task_detail_lookups_are_deduped_and_cached():
    # A task_id referenced by multiple refs (and via replacement task_ids)
    # triggers at most one task_loader call, and cached unavailability is
    # reused rather than re-fetched.
    calls = {}
    stalled_task = "task_" + "e" * 32
    replacement = REPLACEMENT_ID
    lifecycle = {
        "disposition": "superseded",
        "eligible_after": "2026-06-01T00:00:00+00:00",
        "replacement_task_id": replacement,
    }
    release = _stall_event()

    def loader(task_id):
        calls[task_id] = calls.get(task_id, 0) + 1
        if task_id == stalled_task:
            release.wait(timeout=5.0)
            return None
        if task_id == replacement:
            return _detail("completed")
        return _detail("cancelled", lifecycle=lifecycle)

    def merged(_repo, argv, timeout):
        return _cp(argv)

    # TASK_ID appears on three refs; stalled_task on two refs; both replacement
    # chains point at the same replacement task id.
    refs = [
        _multi_ref(TASK_ID, branch_suffix="1"),
        _multi_ref(TASK_ID, branch_suffix="2"),
        _multi_ref(TASK_ID, branch_suffix="3"),
        _multi_ref(stalled_task, branch_suffix="s1"),
        _multi_ref(stalled_task, branch_suffix="s2"),
    ]
    clock, _advance = _fake_clock()
    try:
        result = audit_repository_refs_result(
            Path("."),
            refs,
            loader,
            now=NOW,
            open_pull_requests={},
            runner=merged,
            per_task_timeout_seconds=0.05,
            audit_deadline_seconds=1.0,
            monotonic=clock,
        )
    finally:
        release.set()

    # Every task id fetched at most once despite repeated references.
    assert calls.get(TASK_ID) == 1
    assert calls.get(stalled_task) == 1
    assert calls.get(replacement) == 1

    by_branch = {audit.branch: audit for audit in result.audits}
    assert len(by_branch) == len(refs)
    # Cached unavailability reused: both stalled refs report timed-out.
    stalled_refs = [a for a in result.audits if a.task_id == stalled_task]
    assert len(stalled_refs) == 2
    assert all(a.eligible is False for a in stalled_refs)
    assert stalled_task in result.timed_out_task_ids


def test_fail_closed_when_primary_or_replacement_detail_unavailable():
    # A ref whose primary OR replacement task detail is unavailable/timed-out
    # must never be eligible=True.
    lifecycle = {
        "disposition": "superseded",
        "eligible_after": "2026-06-01T00:00:00+00:00",
        "replacement_task_id": REPLACEMENT_ID,
    }

    def merged(_repo, argv, timeout):
        return _cp(argv)

    # 1) Primary detail unavailable (loader returns None sentinel).
    def missing_primary(task_id):
        return None

    primary_result = audit_repository_refs_result(
        Path("."),
        [_ref()],
        missing_primary,
        now=NOW,
        open_pull_requests={},
        runner=merged,
    )
    assert primary_result.audits[0].eligible is False

    # 2) Primary present + eligible-looking, but replacement unavailable.
    def missing_replacement(task_id):
        if task_id == REPLACEMENT_ID:
            return None
        return _detail("cancelled", lifecycle=lifecycle)

    replacement_result = audit_repository_refs_result(
        Path("."),
        [_ref()],
        missing_replacement,
        now=NOW,
        open_pull_requests={},
        runner=merged,
    )
    audit = replacement_result.audits[0]
    assert audit.eligible is False
    assert "replacement" in audit.reason

    # 3) Sanity: with a completed replacement it becomes eligible, proving the
    # fail-closed cases above are driven by unavailability, not a blanket deny.
    def complete_replacement(task_id):
        if task_id == REPLACEMENT_ID:
            return _detail("completed")
        return _detail("cancelled", lifecycle=lifecycle)

    ok_result = audit_repository_refs_result(
        Path("."),
        [_ref()],
        complete_replacement,
        now=NOW,
        open_pull_requests={},
        runner=merged,
    )
    assert ok_result.audits[0].eligible is True


def test_keyboard_interrupt_yields_clean_partial_report():
    # A task_loader raising KeyboardInterrupt must produce a clean partial
    # report path: no hang, the interrupt is not swallowed into eligible=True,
    # and a partial warning is surfaced.
    def interrupting_loader(task_id):
        raise KeyboardInterrupt()

    result = audit_repository_refs_result(
        Path("."),
        [_ref()],
        interrupting_loader,
        now=NOW,
        open_pull_requests={},
        per_task_timeout_seconds=0.05,
        audit_deadline_seconds=1.0,
    )

    assert isinstance(result, RepositoryRefAuditResult)
    assert len(result.audits) == 1
    assert result.audits[0].eligible is False
    assert result.warning
    assert "interrupted" in result.warning
    assert TASK_ID in result.timed_out_task_ids
