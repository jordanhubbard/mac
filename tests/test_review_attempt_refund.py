"""Work-quality review verdicts consume attempts; harness faults do not.

Regression cover for task_4ce995cb (2026-08-13): a correct one-line change was
rejected three times because the review harness collapsed (588 collection
errors / sandbox UnicodeEncodeError). The classifier-driven refund path is
gone. Structured verdicts decide whether the coding attempt counted.
"""

from __future__ import annotations

from mac.models import ReviewStatus, TaskState
from mac.services import ControlPlane
from tests.test_control_plane import register_agent, verified_repo_metadata


def cp():
    return ControlPlane.in_memory()


HARNESS_588_ERRORS = (
    "hub contract verification failed: ]\n"
    "ERROR tests/test_control_plane_public_contract.py::"
    "test_control_plane_public_methods_accept_or_reject_complete_requests\n"
    "============ 36 failed, 84 passed, 4 skipped, 588 errors in 29.56s "
    "=============\n"
)

SEMANTIC_REJECTION = (
    "The change does not satisfy the acceptance criteria: the new test still "
    "passes when the empty-owner half of the gate is deleted."
)

TESTS_FAILED = (
    "FAILED tests/test_batch.py::test_preview_and_apply_agree\n1 failed, 40 passed in 12.00s\n"
)


def _drive_to_review(plane, name):
    executor = register_agent(plane, "%s-executor" % name, ["python"])
    reviewer = register_agent(plane, "%s-reviewer" % name, ["review"])
    task = plane.create_task(name, required_capabilities=["python"])
    plane.claim_task(task.id, executor.id)
    plane.start_task(task.id, executor.id)
    evidence = plane.add_evidence(
        task.id,
        "log",
        "artifact://%s" % name,
        "ready",
        executor.id,
        metadata=verified_repo_metadata(plane, executor.id),
    )
    plane.submit_for_review(task.id, executor.id)
    review = plane.request_review(task.id, reviewer.id, actor="manual")
    return executor, reviewer, task, review, evidence


def _submit(plane, review, reviewer, task, *, verdict, feedback, reviewed_evidence):
    evidence = plane.add_evidence(
        task.id,
        "review",
        "artifact://verdict-%s" % review.id,
        "verdict",
        reviewer.id,
        metadata={
            "verification": {
                "evidence_type": "review_verdict",
                "verdict": verdict,
                "feedback": feedback,
                "reviewed_evidence_id": reviewed_evidence.id,
            }
        },
    )
    status = {
        "rejected": ReviewStatus.REJECTED.value,
        "tests_failed": ReviewStatus.TESTS_FAILED.value,
        "infrastructure": ReviewStatus.INFRASTRUCTURE.value,
    }[verdict]
    return plane.submit_review(
        review.id,
        status,
        reviewer.id,
        reason="reviewer %s via signed verdict evidence" % verdict,
        evidence_id=evidence.id,
    )


def test_infrastructure_does_not_consume_the_coding_attempt():
    plane = cp()
    _, reviewer, task, review, ev = _drive_to_review(plane, "harness-infra")
    assert plane.get_task(task.id).attempt_count == 1

    _submit(
        plane,
        review,
        reviewer,
        task,
        verdict="infrastructure",
        feedback=HARNESS_588_ERRORS,
        reviewed_evidence=ev,
    )

    after = plane.get_task(task.id)
    assert after.attempt_count == 0
    assert after.state == TaskState.OPEN.value
    details = [
        event.detail
        for event in plane.task_history(task.id, limit=50)
        if isinstance(getattr(event, "detail", None), dict)
    ]
    assert not any(d.get("attempt_refunded") for d in details)
    assert any(d.get("work_attempt_consumed") is False for d in details)


def test_semantic_rejection_consumes_the_attempt():
    plane = cp()
    _, reviewer, task, review, ev = _drive_to_review(plane, "semantic-spend")
    _submit(
        plane,
        review,
        reviewer,
        task,
        verdict="rejected",
        feedback=SEMANTIC_REJECTION,
        reviewed_evidence=ev,
    )
    after = plane.get_task(task.id)
    assert after.attempt_count == 1
    assert after.state == TaskState.OPEN.value


def test_tests_failed_is_first_class_and_consumes():
    plane = cp()
    _, reviewer, task, review, ev = _drive_to_review(plane, "tests-failed")
    submitted = _submit(
        plane,
        review,
        reviewer,
        task,
        verdict="tests_failed",
        feedback=TESTS_FAILED,
        reviewed_evidence=ev,
    )
    assert submitted.status == ReviewStatus.TESTS_FAILED.value
    after = plane.get_task(task.id)
    assert after.attempt_count == 1
    assert after.state == TaskState.OPEN.value


def test_repeated_infrastructure_never_exhausts_the_budget():
    plane = cp()
    executor, reviewer, task, review, ev = _drive_to_review(plane, "three-strikes")

    for round_index in range(3):
        _submit(
            plane,
            review,
            reviewer,
            task,
            verdict="infrastructure",
            feedback=HARNESS_588_ERRORS,
            reviewed_evidence=ev,
        )
        current = plane.get_task(task.id)
        assert current.state == TaskState.OPEN.value
        assert current.attempt_count == 0
        plane.claim_task(task.id, executor.id)
        lease_id = plane.get_task(task.id).lease_id
        plane.start_task(task.id, executor.id, lease_id=lease_id)
        ev = plane.add_evidence(
            task.id,
            "log",
            "artifact://three-strikes-%d" % round_index,
            "ready",
            executor.id,
            metadata=verified_repo_metadata(plane, executor.id),
            lease_id=lease_id,
        )
        plane.submit_for_review(task.id, executor.id, lease_id=lease_id)
        review = plane.request_review(task.id, reviewer.id, actor="manual")

    final = plane.get_task(task.id)
    assert final.state not in {TaskState.FAILED.value, TaskState.BLOCKED.value}
