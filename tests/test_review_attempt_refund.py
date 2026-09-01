"""A review rejected by its own harness must not spend the task's retry budget.

Regression cover for task_b1f81fde, whose evidence is task_4ce995cb
(2026-08-13).  A worker submitted a correct one-line regression test three
times.  All three reviews rejected -- not on the merits, but because the review
harness itself failed: attempts 1 and 3 reported ``36 failed, 84 passed, 588
errors``, and attempt 2 reported the sandbox ``UnicodeEncodeError: 'ascii'
codec can't encode character '\\xa7'`` that PR #352 fixed eleven hours later.

``attempt_count`` increments at CLAIM time, so each of those harness failures
had already spent an attempt before any judgement about the work existed.  The
task reached 3/3, went terminal, and the post-mortem classifier labelled it
``scope`` -- whose operator remediation is "decompose", advice that was
actively wrong for a one-line change.  An equivalent task filed afterwards
succeeded unchanged and merged as PR #353.

``classify_review_failure`` already separated these cases correctly; it was
simply never consulted where the budget is spent.
"""

from __future__ import annotations

import pytest

from mac.models import ReviewStatus, TaskState
from mac.services import ControlPlane
from tests.test_control_plane import register_agent, verified_repo_metadata


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


# The verbatim feedback recorded on task_4ce995cb's three rejections.
HARNESS_588_ERRORS = (
    "hub contract verification failed: ]\n"
    "ERROR tests/test_control_plane_public_contract.py::"
    "test_control_plane_public_methods_accept_or_reject_complete_requests"
    "[workflow_run_decisions]\n"
    "============ 36 failed, 84 passed, 4 skipped, 588 errors in 29.56s "
    "=============\n"
    "  Uploading files to /sandbox...\n"
    "Error:   x ssh exited with status exit status: 1\n"
)

HARNESS_UTF8 = (
    "hub contract verification failed: "
    'File "/opt/mac-venv/lib/python3.12/site-packages/psycopg/_queries.py", '
    "line 167, in _ensure_bytes\n"
    "    return query.encode(self._tx.encoding)\n"
    "UnicodeEncodeError: 'ascii' codec can't encode character '\\xa7' in "
    "position 17789: ordinal not in range(128)\n"
    "Error:   x ssh exited with status exit status: 1\n"
)

SEMANTIC_REJECTION = (
    "The change does not satisfy the acceptance criteria: the new test still "
    "passes when the empty-owner half of the gate is deleted, so it does not "
    "pin the behaviour the task asked for."
)


def _drive_to_review(cp, name):
    """Create a task, take it through one attempt, and open a review on it."""
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


def _reject_with(cp, review, reviewer, task, feedback, reviewed_evidence):
    """Reject `review`, carrying `feedback` on the verdict evidence.

    The feedback lives in evidence metadata under ``verification.feedback``,
    which is where the hub review verifier actually writes it.
    """
    verdict = cp.add_evidence(
        task.id,
        "review",
        "artifact://verdict-%s" % review.id,
        "verdict",
        reviewer.id,
        metadata={
            "verification": {
                "evidence_type": "review_verdict",
                "verdict": "rejected",
                "feedback": feedback,
                "reviewed_evidence_id": reviewed_evidence.id,
            }
        },
    )
    return cp.submit_review(
        review.id,
        ReviewStatus.REJECTED.value,
        reviewer.id,
        reason="reviewer rejected via signed verdict evidence",
        evidence_id=verdict.id,
    )


def test_harness_failure_refunds_the_attempt_it_spent(cp):
    """The 588-collection-error rejection from task_4ce995cb costs nothing."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "harness-refund")
    assert cp.get_task(task.id).attempt_count == 1

    _reject_with(cp, review, reviewer, task, HARNESS_588_ERRORS, ev)

    after = cp.get_task(task.id)
    assert after.attempt_count == 0, (
        "a rejection caused by the review harness must not spend the work's retry budget"
    )
    assert after.state == TaskState.OPEN.value


def test_sandbox_encoding_failure_refunds_the_attempt(cp):
    """Attempt 2's UnicodeEncodeError is infrastructure, not a judgement."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "utf8-refund")
    assert cp.get_task(task.id).attempt_count == 1

    _reject_with(cp, review, reviewer, task, HARNESS_UTF8, ev)

    after = cp.get_task(task.id)
    assert after.attempt_count == 0
    assert after.state == TaskState.OPEN.value


def test_semantic_rejection_still_spends_the_attempt(cp):
    """A real judgement about the work must still consume the budget."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "semantic-spend")
    assert cp.get_task(task.id).attempt_count == 1

    _reject_with(cp, review, reviewer, task, SEMANTIC_REJECTION, ev)

    after = cp.get_task(task.id)
    assert after.attempt_count == 1, (
        "a rejection on the merits is evidence about the work and must still cost an attempt"
    )
    assert after.state == TaskState.OPEN.value


def test_repeated_harness_failures_stop_after_three_refunded_attempts(cp):
    """Harness failures refund work attempts, but cannot retry forever."""
    executor, reviewer, task, review, ev = _drive_to_review(cp, "three-strikes")

    for round_index, feedback in enumerate((HARNESS_588_ERRORS, HARNESS_UTF8, HARNESS_588_ERRORS)):
        _reject_with(cp, review, reviewer, task, feedback, ev)
        current = cp.get_task(task.id)
        expected = TaskState.BLOCKED.value if round_index == 2 else TaskState.OPEN.value
        assert current.state == expected, "round %d left the task in %s" % (
            round_index,
            current.state,
        )
        assert current.attempt_count == 0
        assert current.attempt_count < current.max_attempts
        assert current.metadata["review_infrastructure_failure_count"] == round_index + 1

        if current.state == TaskState.BLOCKED.value:
            break

        # Next attempt: re-claim (which spends an attempt again) and re-review.
        # After a reacquire the control plane requires the current lease_id, so
        # carry it through the rest of the attempt.
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
    assert final.state == TaskState.BLOCKED.value


def test_the_transition_says_why_the_attempt_was_refunded(cp):
    """An operator reading the ledger must see the refund and its cause."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "refund-narrative")
    _reject_with(cp, review, reviewer, task, HARNESS_588_ERRORS, ev)

    details = [
        event.detail
        for event in cp.task_history(task.id, limit=50)
        if isinstance(getattr(event, "detail", None), dict)
    ]
    refunded = [d for d in details if d.get("attempt_refunded") is True]
    assert refunded, "no history row recorded the refund"
    assert any(str(d.get("review_failure_class") or "") for d in refunded), (
        "the refund did not name the harness failure class"
    )
