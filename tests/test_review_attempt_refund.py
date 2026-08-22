"""Attempt consumption is a verdict, not a guess made afterwards.

This file used to pin the opposite arrangement: every review failure arrived as
``rejected``, and ``submit_review`` ran ``classify_review_failure`` over the
free text to work out whether the rejection had really been a harness fault and
refund the attempt if so.

That was a repair for the right problem -- task_4ce995cb (2026-08-13), where a
correct one-line regression test was rejected three times by a harness carrying
588 collection errors and a sandbox UnicodeEncodeError, exhausted its budget,
and was labelled "scope" by a post-mortem classifier -- built on the wrong
mechanism.  Reconstructing which axis failed from prose is guesswork, and
guesswork in the refund path fails both ways: a semantic rejection quoting a
stack trace gets a free retry, and a harness failure that words itself
unusually still costs the work an attempt.

Whoever observed the failure knows which axis it was, so it says so.  The
verdict vocabulary is ``approved | rejected | tests_failed | infrastructure``
(mac.review_verdict), ``infrastructure`` never consumes an attempt, and no
classifier is consulted to decide it.  The task_4ce995cb regression itself now
lives in tests/test_review_verdict_three_axis.py, driven by the verdict rather
than by the text.

What is left here is the guarantee that the removed machinery stays removed.
"""

from __future__ import annotations

import inspect

import pytest

from mac.models import ReviewStatus, TaskState
from mac.review_service import (
    ATTEMPT_CONSUMING_REVIEW_STATUSES,
    NOT_APPROVED_REVIEW_STATUSES,
    ReviewService,
)
from mac.services import ControlPlane
from tests.test_control_plane import register_agent, verified_repo_metadata


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


# The verbatim feedback recorded on task_4ce995cb's three rejections.  Kept
# because the point is that this text no longer changes anything: it is a
# rejection if and only if the reviewer signed one.
HARNESS_588_ERRORS = (
    "hub contract verification failed: ]\n"
    "ERROR tests/test_control_plane_public_contract.py::"
    "test_control_plane_public_methods_accept_or_reject_complete_requests\n"
    "============ 36 failed, 84 passed, 4 skipped, 588 errors in 29.56s "
    "=============\n"
    "  Uploading files to /sandbox...\n"
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
    """Reject `review`, carrying `feedback` on the verdict evidence."""
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


def test_a_signed_rejection_spends_an_attempt_whatever_it_quotes(cp):
    """Prose no longer buys a retry.

    A reviewer that signs ``rejected`` has made a judgement about the work.
    If the real cause was the harness, the fix is for the producer to sign
    ``infrastructure`` -- not for the consumer to second-guess the signature.
    """
    _, reviewer, task, review, ev = _drive_to_review(cp, "prose-no-refund")
    assert cp.get_task(task.id).attempt_count == 1

    _reject_with(cp, review, reviewer, task, HARNESS_588_ERRORS, ev)

    after = cp.get_task(task.id)
    assert after.attempt_count == 1
    assert after.state == TaskState.OPEN.value


def test_a_semantic_rejection_spends_an_attempt(cp):
    """Unchanged, and now for a reason that does not depend on wording."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "semantic-spend")

    _reject_with(cp, review, reviewer, task, SEMANTIC_REJECTION, ev)

    after = cp.get_task(task.id)
    assert after.attempt_count == 1
    assert after.state == TaskState.OPEN.value


def test_no_rejection_path_records_an_attempt_refund(cp):
    """The ledger key that named the guess is gone from this path."""
    _, reviewer, task, review, ev = _drive_to_review(cp, "no-refund-key")

    _reject_with(cp, review, reviewer, task, HARNESS_588_ERRORS, ev)

    details = [
        event.detail
        for event in cp.task_history(task.id, limit=50)
        if isinstance(getattr(event, "detail", None), dict)
    ]
    assert not any("attempt_refunded" in detail for detail in details)


def test_attempt_consumption_is_decided_by_the_verdict_not_a_classifier():
    """The structural guarantee: no classifier in the consumption decision."""
    source = inspect.getsource(ReviewService.submit_review)

    assert "classify_review_failure" not in source, (
        "submit_review must not reconstruct which axis failed from free text; "
        "the signed verdict already says"
    )
    assert ATTEMPT_CONSUMING_REVIEW_STATUSES == {
        ReviewStatus.REJECTED.value,
        ReviewStatus.CHANGES_REQUESTED.value,
        ReviewStatus.TESTS_FAILED.value,
    }
    assert ReviewStatus.INFRASTRUCTURE.value in NOT_APPROVED_REVIEW_STATUSES
    assert ReviewStatus.INFRASTRUCTURE.value not in ATTEMPT_CONSUMING_REVIEW_STATUSES
