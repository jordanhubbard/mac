"""Harness, tests, and semantics must not collapse into one rejected boolean.

The failure this pins is task_4ce995cb (2026-08-13): a correct one-line
regression test, submitted three times, rejected three times -- never on the
merits, always because the review harness itself blew up with 588 collection
errors or a sandbox UnicodeEncodeError.  ``attempt_count`` increments at claim
time, so all three attempts were spent before any judgement about the work
existed; the task went terminal and a post-mortem classifier guessing from free
text labelled it "scope", advice actively wrong for a one-line change.

The three axes are H (did the harness run), R (did the repository's own checks
reproduce green), and S (did the reviewer judge the work correct).  Each is
recorded, none is inferred, and the verdict follows from them.
"""

from __future__ import annotations

import pytest

from mac.models import ReviewStatus, TaskState
from mac.review_verdict import (
    AXIS_FAIL,
    AXIS_PASS,
    AXIS_SKIPPED,
    CANONICAL_VERDICTS,
    VERDICT_APPROVED,
    VERDICT_INFRASTRUCTURE,
    VERDICT_REJECTED,
    VERDICT_TESTS_FAILED,
    ReviewAxes,
    canonical_verdict,
    classify_gate_run,
    collection_error_count,
    failed_test_count,
    review_axes_from_evidence,
    semantic_axis,
    verdict_consumes_attempt,
)
from mac.services import ControlPlane
from tests.test_control_plane import register_agent, verified_repo_metadata


# The verbatim output shape of task_4ce995cb's three rejections.
COLLECTION_ERROR_RUN = (
    "ERROR tests/test_control_plane_public_contract.py::"
    "test_control_plane_public_methods_accept_or_reject_complete_requests\n"
    "============ 36 failed, 84 passed, 4 skipped, 588 errors in 29.56s "
    "=============\n"
    "Error:   x ssh exited with status exit status: 1\n"
)

# A collected suite that simply went red.  Four failures, no errors.
RED_SUITE_RUN = (
    "FAILED tests/cli/test_cli_version_flag.py::test_it_prints_the_version\n"
    "======== 4 failed, 11325 passed, 3 skipped in 713.24s ========\n"
)


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def test_the_canonical_verdicts_are_exactly_four():
    assert CANONICAL_VERDICTS == {
        "approved",
        "rejected",
        "tests_failed",
        "infrastructure",
    }


@pytest.mark.parametrize(
    "verdict,consumes",
    [
        (VERDICT_APPROVED, False),
        (VERDICT_REJECTED, True),
        (VERDICT_TESTS_FAILED, True),
        (VERDICT_INFRASTRUCTURE, False),
    ],
)
def test_only_a_judgement_about_the_work_spends_an_attempt(verdict, consumes):
    assert verdict_consumes_attempt(verdict) is consumes


def test_an_unknown_verdict_fails_closed_to_rejected():
    """Not approved -- and not infrastructure either.

    Degrading to ``infrastructure`` would hand unlimited retries to anything
    that produced a malformed verdict, which is failing OPEN on the gate.
    """
    assert canonical_verdict("nonsense") == VERDICT_REJECTED
    assert canonical_verdict("") == VERDICT_REJECTED
    assert canonical_verdict(None) == VERDICT_REJECTED


# ---------------------------------------------------------------------------
# Resolving the axes
# ---------------------------------------------------------------------------


def test_harness_is_evaluated_first():
    """A harness that never ran produced no information about R or S."""
    axes = ReviewAxes(
        harness=AXIS_FAIL,
        reproducibility=AXIS_FAIL,
        semantics=AXIS_FAIL,
        harness_reason="sandbox never came up",
    )
    assert axes.verdict() == VERDICT_INFRASTRUCTURE
    assert axes.reason() == "sandbox never came up"


def test_a_semantic_rejection_survives_a_red_suite():
    """Both spend an attempt, so report the more specific of the two."""
    axes = ReviewAxes(
        harness=AXIS_PASS,
        reproducibility=AXIS_FAIL,
        semantics=AXIS_FAIL,
        semantics_reason="the change does not satisfy the acceptance criteria",
    )
    assert axes.verdict() == VERDICT_REJECTED


def test_a_red_suite_with_a_satisfied_reviewer_is_tests_failed():
    axes = ReviewAxes(
        harness=AXIS_PASS,
        reproducibility=AXIS_FAIL,
        semantics=AXIS_PASS,
        reproducibility_reason="4 failing test(s) with no collection errors",
    )
    assert axes.verdict() == VERDICT_TESTS_FAILED


def test_everything_green_is_approved():
    axes = ReviewAxes(
        harness=AXIS_PASS, reproducibility=AXIS_PASS, semantics=AXIS_PASS
    )
    assert axes.verdict() == VERDICT_APPROVED
    assert axes.reason() == ""


def test_skipped_axes_do_not_read_as_failures():
    """Non-repository work has no checkout and no suite."""
    axes = ReviewAxes(
        harness=AXIS_SKIPPED, reproducibility=AXIS_SKIPPED, semantics=AXIS_PASS
    )
    assert axes.verdict() == VERDICT_APPROVED


def test_the_axes_survive_a_round_trip_through_evidence():
    axes = ReviewAxes(
        harness=AXIS_PASS,
        reproducibility=AXIS_FAIL,
        semantics=AXIS_PASS,
        reproducibility_reason="4 failing test(s) with no collection errors",
    )
    block = axes.evidence()
    assert block["verdict"] == VERDICT_TESTS_FAILED
    assert block["attempt_consumed"] is True
    assert review_axes_from_evidence(block) == axes


def test_evidence_without_the_schema_tag_is_not_axes():
    assert review_axes_from_evidence({"harness": "fail"}) is None
    assert review_axes_from_evidence(None) is None


# ---------------------------------------------------------------------------
# Reading a gate run
# ---------------------------------------------------------------------------


def test_collection_errors_are_counted_from_the_summary():
    assert collection_error_count(COLLECTION_ERROR_RUN) == 588
    assert collection_error_count(RED_SUITE_RUN) == 0
    assert collection_error_count("!!! Interrupted: 12 errors during collection !!!") == 12


def test_failures_are_counted_from_the_summary():
    assert failed_test_count(RED_SUITE_RUN) == 4
    assert failed_test_count("== 3 passed in 0.1s ==") == 0


def test_a_run_with_collection_errors_is_a_harness_failure():
    """task_4ce995cb's actual output. 36 failed, and none of it counts."""
    harness, harness_reason, repro, _ = classify_gate_run(1, COLLECTION_ERROR_RUN)

    assert harness == AXIS_FAIL
    assert "collection" in harness_reason
    assert repro == AXIS_SKIPPED, (
        "a suite that was never collected reports nothing about the change"
    )


def test_a_red_collected_suite_is_a_reproducibility_failure():
    harness, _, repro, repro_reason = classify_gate_run(1, RED_SUITE_RUN)

    assert harness == AXIS_PASS
    assert repro == AXIS_FAIL
    assert "4 failing" in repro_reason


def test_an_unrecognised_failure_stays_with_the_change():
    """Failing open here would let a broken change retry forever."""
    harness, _, repro, _ = classify_gate_run(1, "segmentation fault (core dumped)")

    assert harness == AXIS_PASS
    assert repro == AXIS_FAIL


def test_a_green_run_passes_both_axes():
    harness, _, repro, _ = classify_gate_run(0, "== 11325 passed in 700s ==")
    assert (harness, repro) == (AXIS_PASS, AXIS_PASS)


def test_an_observed_harness_problem_needs_no_text_mining():
    """Bootstrap, CodeGraph, integration -- the caller watched these fail."""
    harness, harness_reason, repro, _ = classify_gate_run(
        0, "", harness_problem="independent repository bootstrap failed"
    )

    assert harness == AXIS_FAIL
    assert harness_reason == "independent repository bootstrap failed"
    assert repro == AXIS_SKIPPED


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("approved", AXIS_PASS),
        ("rejected", AXIS_FAIL),
        ("changes_requested", AXIS_FAIL),
    ],
)
def test_the_semantics_axis_reads_the_reviewers_own_verdict(verdict, expected):
    axis, _ = semantic_axis(verdict)
    assert axis == expected


def test_a_missing_semantic_verdict_fails_semantics_not_harness():
    """The machinery worked; the agent did not answer. That is on the review."""
    axis, reason = semantic_axis("approved", valid=False)
    assert axis == AXIS_FAIL
    assert "valid semantic verdict" in reason


# ---------------------------------------------------------------------------
# What the verdicts do to a task
# ---------------------------------------------------------------------------


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _drive_to_review(cp, name):
    executor = register_agent(cp, "%s-executor" % name, ["python"])
    reviewer = register_agent(cp, "%s-reviewer" % name, ["review"])
    task = cp.create_task(name, required_capabilities=["python"])
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://%s" % name,
        "ready",
        executor.id,
        metadata=verified_repo_metadata(cp, executor.id),
    )
    cp.submit_for_review(task.id, executor.id)
    review = cp.request_review(task.id, reviewer.id, actor="manual")
    return executor, reviewer, task, review, evidence


def _submit(cp, review, reviewer, task, reviewed_evidence, *, status, verdict, axes):
    verdict_evidence = cp.add_evidence(
        task.id,
        "review",
        "artifact://verdict-%s" % review.id,
        "verdict",
        reviewer.id,
        metadata={
            "verification": {
                "evidence_type": "review_verdict",
                "verdict": verdict,
                "review_axes": axes.evidence(),
                "feedback": axes.reason() or "no detail",
                "reviewed_evidence_id": reviewed_evidence.id,
            }
        },
    )
    return cp.submit_review(
        review.id,
        status,
        reviewer.id,
        reason="verdict submitted for test",
        evidence_id=verdict_evidence.id,
    )


HARNESS_AXES = ReviewAxes(
    harness=AXIS_FAIL,
    harness_reason="review harness did not measure the change "
    "(errors during collection)",
)
RED_AXES = ReviewAxes(
    harness=AXIS_PASS,
    reproducibility=AXIS_FAIL,
    semantics=AXIS_PASS,
    reproducibility_reason="4 failing test(s) with no collection errors",
)


def test_infrastructure_reopens_without_consuming_an_attempt(cp):
    _, reviewer, task, review, ev = _drive_to_review(cp, "infra-verdict")
    assert cp.get_task(task.id).attempt_count == 1

    done = _submit(
        cp, review, reviewer, task, ev,
        status=ReviewStatus.INFRASTRUCTURE.value,
        verdict=VERDICT_INFRASTRUCTURE,
        axes=HARNESS_AXES,
    )

    assert done.status == ReviewStatus.INFRASTRUCTURE.value
    after = cp.get_task(task.id)
    assert after.state == TaskState.OPEN.value
    assert after.attempt_count == 0, (
        "attempt_count increments at claim, so an attempt the harness lost "
        "must come back or the budget silently drains"
    )


def test_infrastructure_records_no_attempt_refund_and_no_judgement(cp):
    """The refund machinery is gone; the verdict itself carries the fact."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "infra-ledger")

    _submit(
        cp, review, reviewer, task, ev,
        status=ReviewStatus.INFRASTRUCTURE.value,
        verdict=VERDICT_INFRASTRUCTURE,
        axes=HARNESS_AXES,
    )

    details = [
        event.detail
        for event in cp.task_history(task.id, limit=50)
        if isinstance(getattr(event, "detail", None), dict)
    ]
    assert not any("attempt_refunded" in d for d in details), (
        "attempt_refunded was the classifier-guess machinery this replaces"
    )
    assert any(d.get("review_attempt_consumed") is False for d in details)
    assert any(d.get("review_failure_axis") == "harness" for d in details)

    # A harness failure says nothing about the change, so it must not be
    # relayed to the next attempt as feedback about the change.
    assert "review_feedback" not in cp.get_task(task.id).metadata


def test_tests_failed_consumes_an_attempt_and_is_its_own_status(cp):
    _, reviewer, task, review, ev = _drive_to_review(cp, "tests-failed-verdict")
    assert cp.get_task(task.id).attempt_count == 1

    done = _submit(
        cp, review, reviewer, task, ev,
        status=ReviewStatus.TESTS_FAILED.value,
        verdict=VERDICT_TESTS_FAILED,
        axes=RED_AXES,
    )

    assert done.status == ReviewStatus.TESTS_FAILED.value
    after = cp.get_task(task.id)
    assert after.attempt_count == 1, "a red suite is a fact about the work"
    assert after.state == TaskState.OPEN.value
    # It is distinct from a rejection, and the ledger says so.
    assert after.metadata["review_feedback"]["latest"]["verdict"] == VERDICT_TESTS_FAILED


def test_tests_failed_can_still_exhaust_the_budget(cp):
    """A judgement is a judgement: it must be able to end a task."""
    executor, reviewer, task, review, ev = _drive_to_review(cp, "tests-exhaust")

    for _ in range(task.max_attempts):
        _submit(
            cp, review, reviewer, task, ev,
            status=ReviewStatus.TESTS_FAILED.value,
            verdict=VERDICT_TESTS_FAILED,
            axes=RED_AXES,
        )
        current = cp.get_task(task.id)
        if current.state == TaskState.BLOCKED.value:
            break
        cp.claim_task(task.id, executor.id)
        lease_id = cp.get_task(task.id).lease_id
        cp.start_task(task.id, executor.id, lease_id=lease_id)
        ev = cp.add_evidence(
            task.id, "log", "artifact://tests-exhaust-%s" % lease_id, "ready",
            executor.id,
            metadata=verified_repo_metadata(cp, executor.id),
            lease_id=lease_id,
        )
        cp.submit_for_review(task.id, executor.id, lease_id=lease_id)
        review = cp.request_review(task.id, reviewer.id, actor="manual")

    assert cp.get_task(task.id).state == TaskState.BLOCKED.value


def test_three_harness_failures_never_exhaust_max_attempts(cp):
    """The whole task_4ce995cb shape, at the layer that decides it.

    Previously this reached 3/3 and went terminal with failure_class "scope".
    """
    executor, reviewer, task, review, ev = _drive_to_review(cp, "three-strikes")

    for round_index in range(3):
        _submit(
            cp, review, reviewer, task, ev,
            status=ReviewStatus.INFRASTRUCTURE.value,
            verdict=VERDICT_INFRASTRUCTURE,
            axes=HARNESS_AXES,
        )
        current = cp.get_task(task.id)
        assert current.state == TaskState.OPEN.value, (
            "round %d left the task in %s" % (round_index, current.state)
        )
        assert current.attempt_count == 0
        assert current.attempt_count < current.max_attempts

        cp.claim_task(task.id, executor.id)
        lease_id = cp.get_task(task.id).lease_id
        cp.start_task(task.id, executor.id, lease_id=lease_id)
        ev = cp.add_evidence(
            task.id, "log", "artifact://three-strikes-%d" % round_index, "ready",
            executor.id,
            metadata=verified_repo_metadata(cp, executor.id),
            lease_id=lease_id,
        )
        cp.submit_for_review(task.id, executor.id, lease_id=lease_id)
        review = cp.request_review(task.id, reviewer.id, actor="manual")

    final = cp.get_task(task.id)
    assert final.state not in {TaskState.FAILED.value, TaskState.BLOCKED.value}
    assert final.attempt_count == 1, (
        "one live claim, and not one of the three harness failures charged"
    )


# ---------------------------------------------------------------------------
# The deterministic finalizer, end to end
# ---------------------------------------------------------------------------


def _git_review(repo, *args):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _finalize(tmp_path, monkeypatch, *, semantic_verdict, gate_returncode, gate_output):
    """Run the real finalizer over a prepared review checkout."""
    import json

    from mac import executor_finalizer, review_finalizer

    repo = tmp_path / "review_worktree"
    repo.mkdir()
    _git_review(repo, "init", "-q")
    _git_review(repo, "config", "user.email", "exec@example.com")
    _git_review(repo, "config", "user.name", "Executor")
    (repo / "shipped.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git_review(repo, "add", "-A")
    _git_review(repo, "commit", "-q", "-m", "executor change")
    exec_head = _git_review(repo, "rev-parse", "HEAD")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "mac-evidence.json").write_text(
        json.dumps(
            {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "review_verdict",
                "verdict": semantic_verdict,
                "feedback": "semantic reviewer said so",
            }
        ),
        encoding="utf-8",
    )
    (workspace / "executor-evidence.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "verification": {
                        "repo": {"head_sha": exec_head, "files_changed": ["shipped.py"]}
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class _Proc:
        returncode = gate_returncode
        stdout = gate_output
        stderr = ""

    monkeypatch.setenv("MAC_ATTESTATION_KEY", "test-attestation-key")
    monkeypatch.setenv("MAC_WORKER_AGENT_ID", "agent-reviewer")
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(repo))
    monkeypatch.setattr(
        executor_finalizer, "_run_repository_bootstrap_if_needed", lambda *a, **k: None
    )
    monkeypatch.setattr(
        executor_finalizer, "run_with_stall_watchdog", lambda *a, **k: _Proc()
    )
    monkeypatch.setattr(
        executor_finalizer, "run_codegraph_audit", lambda *a, **k: {"status": "pass"}
    )
    monkeypatch.setattr(
        executor_finalizer, "codegraph_audit_passed", lambda *a, **k: True
    )
    monkeypatch.setattr(
        executor_finalizer,
        "codegraph_audit_check",
        lambda *a, **k: {"name": "codegraph_audit", "returncode": 0, "status": "pass"},
    )
    monkeypatch.setattr(
        executor_finalizer, "_cooperative_integration_check", lambda *a, **k: None
    )
    monkeypatch.setattr(
        executor_finalizer, "_review_experiment_assignment", lambda *a, **k: None
    )

    review_finalizer.run_deterministic_review_verdict(
        workspace,
        {"id": "task-review", "owner_agent_id": "agent-reviewer"},
        {"executor_evidence_id": "ev-exec", "review_id": "rv-1"},
    )
    return json.loads((workspace / "mac-evidence.json").read_text(encoding="utf-8"))


def test_finalizer_calls_a_collection_error_run_infrastructure(tmp_path, monkeypatch):
    """The task_4ce995cb run, through the real finalizer."""
    manifest = _finalize(
        tmp_path, monkeypatch,
        semantic_verdict="approved",
        gate_returncode=1,
        gate_output=COLLECTION_ERROR_RUN,
    )

    assert manifest["verdict"] == VERDICT_INFRASTRUCTURE
    assert manifest["review_axes"]["harness"] == AXIS_FAIL
    assert manifest["review_axes"]["reproducibility"] == AXIS_SKIPPED
    assert manifest["review_axes"]["attempt_consumed"] is False


def test_finalizer_calls_a_red_collected_suite_tests_failed(tmp_path, monkeypatch):
    manifest = _finalize(
        tmp_path, monkeypatch,
        semantic_verdict="approved",
        gate_returncode=1,
        gate_output=RED_SUITE_RUN,
    )

    assert manifest["verdict"] == VERDICT_TESTS_FAILED
    assert manifest["review_axes"]["harness"] == AXIS_PASS
    assert manifest["review_axes"]["reproducibility"] == AXIS_FAIL
    assert manifest["review_axes"]["attempt_consumed"] is True


def test_finalizer_keeps_a_semantic_rejection_rejected(tmp_path, monkeypatch):
    manifest = _finalize(
        tmp_path, monkeypatch,
        semantic_verdict="rejected",
        gate_returncode=0,
        gate_output="== 40 passed in 3s ==",
    )

    assert manifest["verdict"] == VERDICT_REJECTED
    assert manifest["review_axes"]["semantics"] == AXIS_FAIL


def test_finalizer_approves_when_every_axis_is_green(tmp_path, monkeypatch):
    manifest = _finalize(
        tmp_path, monkeypatch,
        semantic_verdict="approved",
        gate_returncode=0,
        gate_output="== 40 passed in 3s ==",
    )

    assert manifest["verdict"] == VERDICT_APPROVED
    assert manifest["review_axes"]["attempt_consumed"] is False


def test_a_semantic_rejection_still_spends_the_attempt(cp):
    _, reviewer, task, review, ev = _drive_to_review(cp, "semantic-spend")

    _submit(
        cp, review, reviewer, task, ev,
        status=ReviewStatus.REJECTED.value,
        verdict=VERDICT_REJECTED,
        axes=ReviewAxes(
            harness=AXIS_PASS,
            reproducibility=AXIS_PASS,
            semantics=AXIS_FAIL,
            semantics_reason="the new test does not pin the requested behaviour",
        ),
    )

    after = cp.get_task(task.id)
    assert after.attempt_count == 1
    assert after.state == TaskState.OPEN.value
