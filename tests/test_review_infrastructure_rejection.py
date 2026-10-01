"""A review rejected by its own harness parks for an operator; it never reopens.

Background: task_4ce995cb (2026-08-13). A worker submitted a correct one-line
regression test three times, and all three reviews were rejected by the review
harness itself (588 collection errors; a sandbox UnicodeEncodeError), not on
the merits. The hub-verify era answered harness rejections by refunding the
attempt and reopening the task, which re-ran the whole task for a fault the
task never had -- 275 times.

Re-executing the task cannot fix its reviewer's infrastructure, so a rejection
that ``classify_review_failure`` recognises as infrastructure now moves the
task to BLOCKED with manual repair required. A rejection on the merits still
spends the attempt and reopens the task.
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
    which is where verdict feedback is recorded.
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


@pytest.mark.parametrize("feedback", [HARNESS_588_ERRORS, HARNESS_UTF8])
def test_harness_failure_blocks_instead_of_reopening(cp, feedback):
    """The task_4ce995cb rejections park the task; they do not re-run it."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "harness-block")
    assert cp.get_task(task.id).attempt_count == 1

    _reject_with(cp, review, reviewer, task, feedback, ev)

    after = cp.get_task(task.id)
    assert after.state == TaskState.BLOCKED.value
    assert after.attempt_count == 1, "no attempt is refunded any more"
    assert "review_infrastructure_failure_count" not in after.metadata


def test_semantic_rejection_still_spends_the_attempt_and_reopens(cp):
    """A real judgement about the work consumes the budget and reopens the task."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "semantic-spend")
    assert cp.get_task(task.id).attempt_count == 1

    _reject_with(cp, review, reviewer, task, SEMANTIC_REJECTION, ev)

    after = cp.get_task(task.id)
    assert after.attempt_count == 1
    assert after.state == TaskState.OPEN.value


def test_the_block_says_the_harness_failed(cp):
    """An operator reading the ledger must see why the task stopped."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "block-narrative")
    _reject_with(cp, review, reviewer, task, HARNESS_588_ERRORS, ev)

    blocked = [
        event.detail
        for event in cp.task_history(task.id, limit=50)
        if event.to_state == TaskState.BLOCKED.value and isinstance(event.detail, dict)
    ]
    assert blocked, "no transition recorded the block"
    detail = blocked[-1]
    assert detail["manual_repair_required"] is True
    assert detail["review_failure_is_infrastructure"] is True
    assert detail["reason"].startswith("review harness failed (")
    assert "attempt_refunded" not in detail
