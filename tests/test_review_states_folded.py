"""The default workflow lands from NEEDS_REVIEW; REVIEWING is legacy/human-only.

The hub-reviewer decides from validated worker evidence in the same tick it is
assigned, so nothing waits on a reviewer and the task never needs to enter
REVIEWING. The state is kept for human-requested reviews and for rows written
before this change, both of which still complete through the land loop.
"""

from __future__ import annotations

from mac.executor_prompt import build_task_prompt
from mac.models import TASK_TRANSITIONS, ReviewStatus, TaskState
from tests.test_control_plane import (  # noqa: F401 - pytest fixture
    cp,
    register_agent,
    verified_repo_metadata,
)


def _submitted_task(cp, *, target="test://publish"):
    worker = register_agent(cp, "worker", ["python"])
    task = cp.create_task(
        "Implement thing",
        required_capabilities=["python"],
        metadata={"publication_target": target},
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://worker-result",
        "tests passed",
        worker.id,
        metadata=verified_repo_metadata(cp, worker.id),
    )
    cp.submit_for_review(task.id, worker.id)
    return task, worker, evidence


def _transitions(cp, task_id):
    return [
        (event.from_state, event.to_state)
        for event in cp.task_history(task_id, limit=100)
        if event.to_state and event.from_state != event.to_state
    ]


def test_worker_evidence_goes_from_needs_review_to_completed_without_reviewing(cp):
    task, _worker, evidence = _submitted_task(cp)
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    transitions = _transitions(cp, task.id)
    assert (TaskState.NEEDS_REVIEW.value, TaskState.COMPLETED.value) in transitions
    assert all(TaskState.REVIEWING.value not in pair for pair in transitions)
    [review] = cp.list_reviews(task.id)
    assert review.status == ReviewStatus.APPROVED.value
    assert cp.get_publication(result["publication_id"]).evidence_id == evidence.id


def test_an_approved_task_waiting_to_land_stays_in_needs_review(cp, monkeypatch):
    """Approval and landing are separate ticks when landing has to wait; the
    approved task waits in NEEDS_REVIEW and the next tick reuses the approval."""
    from mac.models import ValidationError

    task, _worker, _evidence = _submitted_task(cp)

    def pending(*_args, **_kwargs):
        exc = ValidationError("git publication is waiting on the pull request's own checks")
        exc.publication_retry_after_seconds = 600
        exc.publication_failure_kind = "pull_request_checks_pending"
        raise exc

    real_publish = cp.publish_task
    monkeypatch.setattr(cp, "publish_task", pending)
    waiting = cp.advance_default_review_workflow(task.id)
    assert waiting["status"] == "publish_failed"
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value

    monkeypatch.setattr(cp, "publish_task", real_publish)
    metadata = dict(cp.get_task(task.id).metadata)
    metadata["landing"] = {k: v for k, v in metadata["landing"].items() if k != "not_before"}
    metadata.pop("publication_retry", None)
    cp._persist_task_metadata_narrow(task.id, metadata, actor="test")

    assert cp.advance_default_review_workflow(task.id)["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    # One approval, reused: no second review was opened for the same evidence.
    assert len(cp.list_reviews(task.id)) == 1


def test_a_legacy_reviewing_task_still_completes(cp):
    """A row written before the fold: REVIEWING with a pending review assigned
    to a (retired) agent reviewer. The next tick supersedes that review,
    approves from the worker evidence and lands it."""
    task, _worker, _evidence = _submitted_task(cp)
    legacy_reviewer = register_agent(cp, "legacy-reviewer", ["review"])
    legacy = cp.request_review(task.id, legacy_reviewer.id)
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "published"
    assert cp.get_task(task.id).state == TaskState.COMPLETED.value
    assert (TaskState.REVIEWING.value, TaskState.COMPLETED.value) in _transitions(cp, task.id)
    reviews = {review.id: review for review in cp.list_reviews(task.id)}
    assert reviews[legacy.id].status != ReviewStatus.PENDING.value
    assert any(review.status == ReviewStatus.APPROVED.value for review in reviews.values())


def test_a_human_requested_review_still_enters_reviewing(cp):
    task, _worker, _evidence = _submitted_task(cp)
    human = register_agent(cp, "human-reviewer", ["review"])

    review = cp.request_review(task.id, human.id)

    assert review.status == ReviewStatus.PENDING.value
    assert cp.get_task(task.id).state == TaskState.REVIEWING.value


def test_needs_review_can_reopen_and_complete():
    allowed = TASK_TRANSITIONS[TaskState.NEEDS_REVIEW.value]
    assert TaskState.OPEN.value in allowed
    assert TaskState.COMPLETED.value in allowed
    # REVIEWING stays reachable for human-requested reviews.
    assert TaskState.REVIEWING.value in allowed


# ---------------------------------------------------------------------------
# The rebase_onto_tip directive reaches the agent's prompt.
# ---------------------------------------------------------------------------


def _task_with_directive(**directive):
    return {
        "id": "task_0123456789abcdef",
        "title": "Implement thing",
        "metadata": {
            "rebase_onto_tip": {
                "schema": "mac.rebase_onto_tip.v1",
                "canonical_tip": "c" * 40,
                "reviewed_head_sha": "a" * 40,
                "previous_remote_ref": "refs/heads/mac/task_0123456789abcdef",
                "conflicted_files": [],
                "reason": "canonical_moved",
                **directive,
            }
        },
    }


def test_the_prompt_tells_a_sent_back_task_to_rebase_and_keep_its_work():
    prompt = build_task_prompt(_task_with_directive())

    assert "Sent back to rebase:" in prompt
    assert "the default branch moved after your verifier ran" in prompt
    assert "Rebase onto %s." % ("c" * 40) in prompt
    assert (
        "Keep the previous work from refs/heads/mac/task_0123456789abcdef (%s)" % ("a" * 40)
    ) in prompt
    assert "Resolve the conflicts" not in prompt


def test_the_prompt_names_the_conflicted_files():
    prompt = build_task_prompt(
        _task_with_directive(reason="conflict", conflicted_files=["src/a.py", "docs/b.md"])
    )

    assert "your change now conflicts with it" in prompt
    assert "Resolve the conflicts in: src/a.py, docs/b.md." in prompt


def test_the_prompt_has_no_rebase_section_without_a_directive():
    assert "Sent back to rebase" not in build_task_prompt({"id": "t", "metadata": {}})
