"""Harness, reproducibility, and semantics must not collapse into one boolean.

Regression cover for the task_4ce995cb shape (2026-08-13).  A worker submitted
a correct one-line regression test three times.  All three reviews rejected --
not on the merits, but because the review harness itself failed: attempts 1 and
3 reported ``36 failed, 84 passed, 588 errors``, and attempt 2 reported a
sandbox ``UnicodeEncodeError`` that was fixed eleven hours later.  The task
reached 3/3, went terminal, and the post-mortem classifier labelled it
``scope`` -- whose operator remediation is "decompose", advice that was
actively wrong for a one-line change.  An equivalent task filed afterwards
succeeded unchanged.

The first fix reconstructed "was this infrastructure?" from the rejection's
free text and refunded an attempt when the guess said so.  This suite pins the
replacement: the verdict PRODUCER records which axis failed, the verdict word
itself carries it (``approved`` / ``rejected`` / ``tests_failed`` /
``infrastructure``), and attempt consumption follows from the verdict rather
than from a classifier run after the fact.
"""

from __future__ import annotations

import json

import pytest

from mac import review_finalizer
from mac.evidence_validators import review_axes_problems
from mac.models import ReviewStatus, TaskState
from mac.review_verdict import (
    HarnessOutcome,
    ReproducibilityOutcome,
    ReviewVerdict,
    SemanticOutcome,
    classify_contract_run,
    consumed_attempt_count,
    pytest_collection_error_count,
    resolve_review_verdict,
    with_infrastructure_attempt,
)
from mac.services import ControlPlane
from tests.test_control_plane import register_agent, verified_repo_metadata


# The verbatim output shapes from task_4ce995cb's three rejections.
HARNESS_588_ERRORS = (
    "ERROR tests/test_control_plane_public_contract.py::"
    "test_control_plane_public_methods_accept_or_reject_complete_requests\n"
    "============ 36 failed, 84 passed, 4 skipped, 588 errors in 29.56s "
    "=============\n"
)

HARNESS_UTF8 = (
    'File "/opt/mac-venv/lib/python3.12/site-packages/psycopg/_queries.py", '
    "line 167, in _ensure_bytes\n"
    "    return query.encode(self._tx.encoding)\n"
    "UnicodeEncodeError: 'ascii' codec can't encode character '\\xa7' in "
    "position 17789: ordinal not in range(128)\n"
)

RED_SUITE = (
    "FAILED tests/test_widget.py::test_rounding - assert 3 == 4\n"
    "==================== 1 failed, 402 passed in 91.02s ====================\n"
)


# ---------------------------------------------------------------------------
# The axes themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("harness", "reproducibility", "semantics", "expected"),
    [
        # H is evaluated first: nothing downstream of a broken harness is read.
        (
            HarnessOutcome.FAIL,
            ReproducibilityOutcome.NOT_RUN,
            SemanticOutcome.APPROVED,
            ReviewVerdict.INFRASTRUCTURE,
        ),
        (
            HarnessOutcome.FAIL,
            ReproducibilityOutcome.FAIL,
            SemanticOutcome.REJECTED,
            ReviewVerdict.INFRASTRUCTURE,
        ),
        # A red suite on a working harness is its own disposition.
        (
            HarnessOutcome.PASS,
            ReproducibilityOutcome.FAIL,
            SemanticOutcome.APPROVED,
            ReviewVerdict.TESTS_FAILED,
        ),
        # A reviewer who read the change and said no stays rejected.
        (
            HarnessOutcome.PASS,
            ReproducibilityOutcome.PASS,
            SemanticOutcome.REJECTED,
            ReviewVerdict.REJECTED,
        ),
        # A reviewer that produced nothing usable fails closed as a rejection,
        # NOT as infrastructure -- infrastructure never spends an attempt, so
        # routing "unreadable" there would retry forever.
        (
            HarnessOutcome.PASS,
            ReproducibilityOutcome.PASS,
            SemanticOutcome.INVALID,
            ReviewVerdict.REJECTED,
        ),
        (
            HarnessOutcome.PASS,
            ReproducibilityOutcome.PASS,
            SemanticOutcome.APPROVED,
            ReviewVerdict.APPROVED,
        ),
    ],
)
def test_verdict_is_derived_from_all_three_axes(
    harness, reproducibility, semantics, expected
):
    assert resolve_review_verdict(harness, reproducibility, semantics) == expected


def test_collection_errors_are_a_harness_fault_not_a_red_suite():
    """588 errors and "1 failed" are the same exit code and different facts."""
    assert pytest_collection_error_count(HARNESS_588_ERRORS) == 588
    harness, repro, problem = classify_contract_run(1, HARNESS_588_ERRORS)
    assert harness == HarnessOutcome.FAIL
    assert repro == ReproducibilityOutcome.NOT_RUN
    assert "588" in problem


def test_sandbox_encoding_fault_is_a_harness_fault():
    harness, repro, _ = classify_contract_run(1, HARNESS_UTF8)
    assert harness == HarnessOutcome.FAIL
    assert repro == ReproducibilityOutcome.NOT_RUN


def test_red_suite_with_no_collection_errors_is_reproducibility():
    assert pytest_collection_error_count(RED_SUITE) == 0
    harness, repro, _ = classify_contract_run(1, RED_SUITE)
    assert harness == HarnessOutcome.PASS
    assert repro == ReproducibilityOutcome.FAIL


def test_a_pytest_summary_outranks_an_incidental_harness_signature():
    """A red test that merely MENTIONS a harness word is still a red test.

    The signature scan is a substring match over the whole run, and pytest
    prints a failing test's source and locals.  ``tests/test_hermes_vendor_
    fate.py`` and half a dozen others in this repository contain the literal
    string ``ModuleNotFoundError``, so without this precedence a genuine
    failure in one of them would be signed ``infrastructure`` -- which never
    spends an attempt, and so would retry forever on a real regression.
    """
    output = (
        "E       ModuleNotFoundError: No module named 'hermes'\n"
        "FAILED tests/test_hermes_vendor_fate.py::test_the_vendor_is_gone\n"
        "==================== 1 failed, 402 passed in 91.02s ====================\n"
    )
    harness, repro, problem = classify_contract_run(1, output)
    assert harness == HarnessOutcome.PASS
    assert repro == ReproducibilityOutcome.FAIL
    assert "1 failing test" in problem


def test_collection_errors_still_outrank_a_pytest_summary():
    """"36 failed, ..., 588 errors" is a harness fault, not a red suite.

    The failed count is real, but so are the 588 tests that never ran: a run
    that could not collect has not judged the change.
    """
    harness, _, _ = classify_contract_run(1, HARNESS_588_ERRORS)
    assert harness == HarnessOutcome.FAIL


def test_unrecognised_failure_fails_closed_as_a_red_suite():
    """Unknown must NOT become infrastructure.

    Infrastructure never spends an attempt, so classifying every unrecognised
    failure as a harness fault would let a genuinely broken change retry
    forever behind "we could not verify it" -- failing open on the gate.
    """
    harness, repro, _ = classify_contract_run(2, "something nobody has seen before")
    assert harness == HarnessOutcome.PASS
    assert repro == ReproducibilityOutcome.FAIL


@pytest.mark.parametrize(
    "verdict", [ReviewVerdict.INFRASTRUCTURE.value, ReviewVerdict.TESTS_FAILED.value]
)
def test_a_missing_semantic_verdict_does_not_invalidate_a_non_review_verdict(
    monkeypatch, verdict
):
    """`infrastructure` and `tests_failed` need no semantic reviewer.

    A signed manifest with an unusable semantic verdict used to be refused
    outright, whatever it said -- the only way to stop it consuming an attempt
    as though a reviewer had judged the patch. These two verdicts now say that
    themselves, and the hub verifier emits `tests_failed` with no semantic axis
    at all, so refusing them would leave those verdicts permanently unfindable.
    """
    from types import SimpleNamespace

    from mac import services

    manifest = {
        "schema": services.VERIFICATION_SCHEMA,
        "status": "complete",
        "evidence_type": "review_verdict",
        "reviewed_evidence_id": "executor",
        "signed_by": "reviewer",
        "signature": "sig",
        "verdict": verdict,
        "semantic_verdict": "invalid",
        "feedback": "the harness never ran the change",
        "worktree_digest": "sha256:" + "a" * 64,
        "tests": [{"status": "passed"}],
    }
    evidence = SimpleNamespace(
        id="verdict",
        created_by="reviewer",
        created_at="2026-01-02T00:00:00+00:00",
        metadata={"returncode": 0, "verification": manifest},
    )
    control = ControlPlane.in_memory()
    monkeypatch.setattr(control, "get_task", lambda *_a: SimpleNamespace(metadata={}))
    monkeypatch.setattr(control, "list_evidence", lambda *_a: [evidence])
    monkeypatch.setattr(control, "_agent_attestation_key", lambda *_a: "key")
    monkeypatch.setattr(
        services, "verify_verification_manifest_signature", lambda *_a: True
    )
    monkeypatch.setattr(
        control, "get_evidence", lambda *_a: SimpleNamespace(metadata={"verification": {}})
    )
    monkeypatch.setattr(services, "cross_llm_review_problems", lambda *_a, **_k: [])

    found, problems = control._find_review_verdict_evidence(
        "task", "reviewer", executor_evidence_id="executor"
    )
    assert found is evidence, problems


def test_axes_that_contradict_the_verdict_are_refused():
    """The axes are load-bearing, so a manifest may not disagree with itself."""
    assert review_axes_problems(
        {
            "verdict": "rejected",
            "review_axes": {
                "harness": {"status": "fail"},
                "reproducibility": {"status": "not_run"},
                "semantics": {"status": "invalid"},
            },
        }
    ) == [
        "review_verdict rejected contradicts its axes "
        "(H=fail R=not_run S=invalid implies infrastructure)"
    ]
    assert review_axes_problems(
        {
            "verdict": "infrastructure",
            "review_axes": {
                "harness": {"status": "fail"},
                "reproducibility": {"status": "not_run"},
                "semantics": {"status": "invalid"},
            },
        }
    ) == []


# ---------------------------------------------------------------------------
# The finalizer: which axis failed, decided where it is known
# ---------------------------------------------------------------------------


def _git_review(repo, *args) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _review_checkout(root):
    repo = root / "review_worktree"
    repo.mkdir()
    _git_review(repo, "init", "-q")
    _git_review(repo, "config", "user.email", "exec@example.com")
    _git_review(repo, "config", "user.name", "Executor")
    (repo / "shipped.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git_review(repo, "add", "-A")
    _git_review(repo, "commit", "-q", "-m", "executor change")
    return repo


def _finalize(tmp_path, monkeypatch, *, semantic_verdict, test_rc, test_output):
    """Run the real finalizer over a real checkout and return its manifest."""
    from mac import executor_finalizer

    review_repo = _review_checkout(tmp_path)
    exec_head = _git_review(review_repo, "rev-parse", "HEAD")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    semantic = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": semantic_verdict,
    }
    if semantic_verdict == "rejected":
        semantic["feedback"] = "the change does not satisfy the acceptance criteria"
    (workspace / "mac-evidence.json").write_text(json.dumps(semantic), encoding="utf-8")
    (workspace / "executor-evidence.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "verification": {
                        "repo": {
                            "head_sha": exec_head,
                            "files_changed": ["shipped.py"],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_ATTESTATION_KEY", "test-attestation-key")
    monkeypatch.setenv("MAC_WORKER_AGENT_ID", "agent-reviewer")
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(review_repo))

    class _Proc:
        returncode = test_rc
        stdout = test_output
        stderr = ""

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


def test_finalizer_reports_collection_errors_as_infrastructure(tmp_path, monkeypatch):
    """The exact task_4ce995cb run, finalized: not a judgement about the work."""
    manifest = _finalize(
        tmp_path,
        monkeypatch,
        semantic_verdict="approved",
        test_rc=1,
        test_output=HARNESS_588_ERRORS,
    )
    assert manifest["verdict"] == ReviewVerdict.INFRASTRUCTURE.value
    assert manifest["review_axes"]["harness"]["status"] == "fail"
    assert manifest["review_axes"]["reproducibility"]["status"] == "not_run"
    assert "588" in manifest["feedback"]


def test_finalizer_reports_a_red_suite_as_tests_failed(tmp_path, monkeypatch):
    manifest = _finalize(
        tmp_path,
        monkeypatch,
        semantic_verdict="approved",
        test_rc=1,
        test_output=RED_SUITE,
    )
    assert manifest["verdict"] == ReviewVerdict.TESTS_FAILED.value
    assert manifest["review_axes"]["harness"]["status"] == "pass"
    assert manifest["review_axes"]["reproducibility"]["status"] == "fail"


def test_finalizer_keeps_a_semantic_rejection_rejected(tmp_path, monkeypatch):
    manifest = _finalize(
        tmp_path,
        monkeypatch,
        semantic_verdict="rejected",
        test_rc=0,
        test_output="",
    )
    assert manifest["verdict"] == ReviewVerdict.REJECTED.value
    assert manifest["review_axes"]["semantics"]["status"] == "rejected"
    assert "acceptance criteria" in manifest["feedback"]


def test_finalizer_approves_when_every_axis_is_clean(tmp_path, monkeypatch):
    manifest = _finalize(
        tmp_path,
        monkeypatch,
        semantic_verdict="approved",
        test_rc=0,
        test_output="402 passed",
    )
    assert manifest["verdict"] == ReviewVerdict.APPROVED.value
    assert manifest["review_axes"] == {
        "schema": "mac.review_verdict_axes.v1",
        "harness": {"status": "pass", "problem": ""},
        "reproducibility": {"status": "pass", "problem": ""},
        "semantics": {"status": "approved", "problem": ""},
    }


def test_infrastructure_verdict_carries_no_judgement_about_the_work(
    tmp_path, monkeypatch
):
    """A harness failure must not hand the executor a critique of its diff."""
    from mac import executor_finalizer

    review_repo = _review_checkout(tmp_path)
    exec_head = _git_review(review_repo, "rev-parse", "HEAD")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "mac-evidence.json").write_text(
        json.dumps(
            {
                "schema": "mac.worker_evidence.v1",
                "status": "complete",
                "evidence_type": "review_verdict",
                "verdict": "rejected",
                "summary": "the abstraction is wrong",
                "findings": [{"detail": "rename this"}],
            }
        ),
        encoding="utf-8",
    )
    (workspace / "executor-evidence.json").write_text(
        json.dumps(
            {"metadata": {"verification": {"repo": {"head_sha": exec_head}}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_ATTESTATION_KEY", "test-attestation-key")
    monkeypatch.setenv("MAC_WORKER_AGENT_ID", "agent-reviewer")
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(review_repo))
    monkeypatch.setattr(
        executor_finalizer,
        "_run_repository_bootstrap_if_needed",
        lambda *a, **k: {"returncode": 1},
    )

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        executor_finalizer, "run_with_stall_watchdog", lambda *a, **k: _Proc()
    )
    monkeypatch.setattr(
        executor_finalizer, "run_codegraph_audit", lambda *a, **k: {"status": "pass"}
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
    manifest = json.loads((workspace / "mac-evidence.json").read_text(encoding="utf-8"))
    assert manifest["verdict"] == ReviewVerdict.INFRASTRUCTURE.value
    assert "bootstrap" in manifest["feedback"]
    assert "summary" not in manifest
    assert "findings" not in manifest


# ---------------------------------------------------------------------------
# Attempt accounting: consumption follows the verdict, not a classifier
# ---------------------------------------------------------------------------


def test_infrastructure_outcomes_do_not_count_against_the_budget():
    metadata = {}
    for _ in range(3):
        metadata = with_infrastructure_attempt(metadata)
    assert consumed_attempt_count(3, metadata) == 0
    assert consumed_attempt_count(4, metadata) == 1


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _drive_to_review(cp, name, *, lease_id=None):
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


def _submit(cp, review, reviewer, task, reviewed_evidence, *, verdict, status):
    """Record `verdict` on verdict evidence and submit `status` for it."""
    evidence = cp.add_evidence(
        task.id,
        "review",
        "artifact://verdict-%s" % review.id,
        "verdict",
        reviewer.id,
        metadata={
            "verification": {
                "evidence_type": "review_verdict",
                "verdict": verdict,
                "feedback": "recorded by the three-axis finalizer",
                "reviewed_evidence_id": reviewed_evidence.id,
            }
        },
    )
    return cp.submit_review(
        review.id, status, reviewer.id, reason="verdict %s" % verdict,
        evidence_id=evidence.id,
    )


def test_infrastructure_reopens_without_touching_attempt_count(cp):
    """attempt_count is the honest count of runs started; it is not rewritten."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "infra-reopen")
    assert cp.get_task(task.id).attempt_count == 1

    submitted = _submit(
        cp,
        review,
        reviewer,
        task,
        ev,
        verdict=ReviewVerdict.INFRASTRUCTURE.value,
        status=ReviewStatus.INFRASTRUCTURE.value,
    )
    assert submitted.status == ReviewStatus.INFRASTRUCTURE.value

    after = cp.get_task(task.id)
    assert after.state == TaskState.OPEN.value
    assert after.attempt_count == 1, "attempt_count must not be rewritten backwards"
    # ...but none of it was charged to the work.
    assert consumed_attempt_count(after.attempt_count, after.metadata) == 0

    details = [
        event.detail
        for event in cp.task_history(task.id, limit=50)
        if isinstance(getattr(event, "detail", None), dict)
    ]
    assert not [d for d in details if "attempt_refunded" in d], (
        "the refund machinery must be gone: consumption is a property of the "
        "verdict, not a correction applied afterwards"
    )
    assert any(d.get("attempt_consumed") is False for d in details)


def test_infrastructure_records_no_judgement_about_the_work(cp):
    """No review_feedback block: there is nothing for the executor to fix."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "infra-silent")
    _submit(
        cp,
        review,
        reviewer,
        task,
        ev,
        verdict=ReviewVerdict.INFRASTRUCTURE.value,
        status=ReviewStatus.INFRASTRUCTURE.value,
    )
    assert "review_feedback" not in cp.get_task(task.id).metadata


def test_tests_failed_consumes_an_attempt_and_is_recorded_as_itself(cp):
    _, reviewer, task, review, ev = _drive_to_review(cp, "tests-failed")
    assert cp.get_task(task.id).attempt_count == 1

    submitted = _submit(
        cp,
        review,
        reviewer,
        task,
        ev,
        verdict=ReviewVerdict.TESTS_FAILED.value,
        status=ReviewStatus.TESTS_FAILED.value,
    )
    assert submitted.status == ReviewStatus.TESTS_FAILED.value

    after = cp.get_task(task.id)
    assert after.state == TaskState.OPEN.value
    assert consumed_attempt_count(after.attempt_count, after.metadata) == 1
    # A red suite IS a fact about the work, so it comes back as feedback.
    assert "review_feedback" in after.metadata


def test_semantic_rejection_still_consumes_an_attempt(cp):
    _, reviewer, task, review, ev = _drive_to_review(cp, "semantic-spend")
    _submit(
        cp,
        review,
        reviewer,
        task,
        ev,
        verdict=ReviewVerdict.REJECTED.value,
        status=ReviewStatus.REJECTED.value,
    )
    after = cp.get_task(task.id)
    assert consumed_attempt_count(after.attempt_count, after.metadata) == 1
    assert after.state == TaskState.OPEN.value


def test_three_collection_error_runs_never_exhaust_max_attempts(cp):
    """The whole task_4ce995cb shape: three harness failures, still retryable.

    Previously this reached attempt_count 3/3 and went terminal with
    failure_class "scope".  The task must remain claimable throughout.
    """
    executor, reviewer, task, review, ev = _drive_to_review(cp, "three-strikes")

    for round_index in range(3):
        _submit(
            cp,
            review,
            reviewer,
            task,
            ev,
            verdict=ReviewVerdict.INFRASTRUCTURE.value,
            status=ReviewStatus.INFRASTRUCTURE.value,
        )
        current = cp.get_task(task.id)
        assert current.state == TaskState.OPEN.value, (
            "round %d left the task in %s" % (round_index, current.state)
        )
        assert consumed_attempt_count(current.attempt_count, current.metadata) == 0

        # Next run: re-claim (which spends an attempt again) and re-review.
        cp.claim_task(task.id, executor.id)
        lease_id = cp.get_task(task.id).lease_id
        cp.start_task(task.id, executor.id, lease_id=lease_id)
        ev = cp.add_evidence(
            task.id,
            "log",
            "artifact://three-strikes-%d" % round_index,
            "ready",
            executor.id,
            metadata=verified_repo_metadata(cp, executor.id),
            lease_id=lease_id,
        )
        cp.submit_for_review(task.id, executor.id, lease_id=lease_id)
        review = cp.request_review(task.id, reviewer.id, actor="manual")

    final = cp.get_task(task.id)
    assert final.attempt_count == 4, "every run really was started"
    assert final.state not in {TaskState.FAILED.value, TaskState.BLOCKED.value}


def test_work_quality_verdicts_still_exhaust_the_budget(cp):
    """The budget must still close on work that keeps failing on the merits."""
    executor, reviewer, task, review, ev = _drive_to_review(cp, "real-exhaustion")
    assert cp.get_task(task.id).max_attempts == 3

    for round_index in range(3):
        _submit(
            cp,
            review,
            reviewer,
            task,
            ev,
            verdict=ReviewVerdict.REJECTED.value,
            status=ReviewStatus.REJECTED.value,
        )
        current = cp.get_task(task.id)
        if current.state != TaskState.OPEN.value:
            break
        cp.claim_task(task.id, executor.id)
        lease_id = cp.get_task(task.id).lease_id
        cp.start_task(task.id, executor.id, lease_id=lease_id)
        ev = cp.add_evidence(
            task.id,
            "log",
            "artifact://real-exhaustion-%d" % round_index,
            "ready",
            executor.id,
            metadata=verified_repo_metadata(cp, executor.id),
            lease_id=lease_id,
        )
        cp.submit_for_review(task.id, executor.id, lease_id=lease_id)
        review = cp.request_review(task.id, reviewer.id, actor="manual")

    assert cp.get_task(task.id).state == TaskState.BLOCKED.value


# ---------------------------------------------------------------------------
# Hub verification: the hub has no semantic axis, so it must not sign one
# ---------------------------------------------------------------------------


def _hub_verdict(returncode: int, output: str) -> str:
    """The verdict the hub verification path derives, by its own rule.

    The hub never reads the diff, so it has no opinion to offer about the
    change -- its semantic axis is ``not_evaluated`` on every failure.  What it
    can say is whether its harness worked and whether the repository's suite
    agreed.  Signing both as ``rejected`` was the hub asserting a code-review
    judgement it never made.
    """
    harness, reproducibility, _problem = classify_contract_run(returncode, output)
    semantics = (
        SemanticOutcome.APPROVED
        if returncode == 0
        else SemanticOutcome.NOT_EVALUATED
    )
    return resolve_review_verdict(harness, reproducibility, semantics).value


@pytest.mark.parametrize(
    "returncode, output, expected",
    [
        (0, "", ReviewVerdict.APPROVED.value),
        (1, HARNESS_588_ERRORS, ReviewVerdict.INFRASTRUCTURE.value),
        (1, HARNESS_UTF8, ReviewVerdict.INFRASTRUCTURE.value),
        (
            1,
            "Error:   x ssh exited with status exit status: 1\n",
            ReviewVerdict.INFRASTRUCTURE.value,
        ),
        (
            1,
            "FAILED tests/test_thing.py::test_case - AssertionError\n"
            "=========== 3 failed, 5 passed in 1.20s ===========\n",
            ReviewVerdict.TESTS_FAILED.value,
        ),
    ],
)
def test_hub_verification_does_not_sign_every_nonzero_as_rejected(
    returncode, output, expected
):
    assert _hub_verdict(returncode, output) == expected


def test_the_hub_path_uses_the_shared_resolver_rather_than_a_literal():
    """Pins the wiring the parametrised cases above stand in for.

    The cases exercise the rule; this asserts the hub path actually applies it
    instead of carrying its own ``"approved" if rc == 0 else "rejected"``.
    """
    import inspect

    from mac import services

    source = inspect.getsource(
        services.ControlPlane._run_hub_review_verification_locked
    )
    assert "resolve_review_verdict(harness, reproducibility, semantics)" in source
    assert '"approved" if returncode == 0 else "rejected"' not in source


def test_every_verdict_has_exactly_one_review_status():
    """No verdict may be silently folded into another on the way to a status."""
    from mac.services import REVIEW_STATUS_FOR_VERDICT

    assert set(REVIEW_STATUS_FOR_VERDICT) == {
        item.value for item in ReviewVerdict
    }
    assert len(set(REVIEW_STATUS_FOR_VERDICT.values())) == len(ReviewVerdict)
    for status in REVIEW_STATUS_FOR_VERDICT.values():
        assert status in {item.value for item in ReviewStatus}
