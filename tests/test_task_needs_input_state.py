"""A task waiting on a human question is parked, not failed.

Before NEEDS_INPUT existed, a task that could not proceed without an answer
had nowhere correct to sit. It went to BLOCKED or FAILED, where the retry
machinery burned its attempt budget re-running work whose blocker no code
path could clear, and the reaper eventually retired it as bad work. Desired
work got classified as failed work purely because there was no state meaning
"a person needs to answer something".

NEEDS_INPUT is that state. The properties that make it safe:

* entering it REQUIRES stating the question (no unstated blockers),
* it leaves only to OPEN (answered) or CANCELLED (abandoned) -- never FAILED,
* no sweeper, reaper, or dispatcher touches it, so it waits indefinitely.
"""
from __future__ import annotations

import pytest

from mac.models import NotFoundError, TaskState, TransitionError, ValidationError
from mac.services import ControlPlane


@pytest.fixture
def cp():
    return ControlPlane.in_memory()


def _park(cp, task, questions=("which database?",), actor="human"):
    return cp.request_task_input(
        task.id, [{"question": q} for q in questions], actor
    )


def test_parking_requires_a_stated_question(cp):
    task = cp.create_task("ambiguous work")
    with pytest.raises(ValidationError):
        cp.request_task_input(task.id, [], "human")
    with pytest.raises(ValidationError):
        cp.request_task_input(task.id, [{"question": "  "}], "human")
    # The rejected attempts must not have moved the task.
    assert cp.get_task(task.id).state == TaskState.OPEN.value


def test_parking_records_the_question_and_who_asked(cp):
    task = cp.create_task("ambiguous work")
    parked = _park(cp, task, questions=("which database?", "which region?"))

    assert parked.state == TaskState.NEEDS_INPUT.value
    payload = parked.metadata["needs_input"]
    assert [q["question"] for q in payload["questions"]] == [
        "which database?",
        "which region?",
    ]
    assert payload["asked_by"] == "human"
    assert payload["asked_at"]
    assert payload["from_state"] == TaskState.OPEN.value


def test_answering_returns_the_task_to_the_dispatch_pool(cp):
    task = cp.create_task("ambiguous work")
    _park(cp, task)
    answered = cp.answer_task_input(
        task.id, "postgres, us-west", "jordan", disposition="resume"
    )

    assert answered.state == TaskState.OPEN.value
    # The outstanding question is cleared, but preserved next to its answer.
    assert "needs_input" not in answered.metadata
    record = answered.metadata["needs_input_history"][-1]
    assert record["answer"] == "postgres, us-west"
    assert record["answered_by"] == "jordan"
    assert record["resolved_to"] == TaskState.OPEN.value
    assert [q["question"] for q in record["questions"]] == ["which database?"]


def test_a_parked_task_cannot_be_failed(cp):
    """The whole point: waiting on an answer is not a failed attempt.

    Asserted against the *trusted* internal path, because that is the one
    every sweeper and reaper uses. An untrusted caller is already refused
    earlier for lack of a lease, which proves less.
    """
    task = cp.create_task("ambiguous work")
    _park(cp, task)
    for target in (
        TaskState.FAILED.value,
        TaskState.BLOCKED.value,
        TaskState.RUNNING.value,
        TaskState.COMPLETED.value,
        TaskState.WAITING.value,
    ):
        with pytest.raises(TransitionError):
            cp._transition_task_internal(task.id, target, "sweeper", {"reason": "x"})
    assert cp.get_task(task.id).state == TaskState.NEEDS_INPUT.value


def test_a_parked_task_may_be_abandoned_by_a_human(cp):
    task = cp.create_task("ambiguous work")
    _park(cp, task)
    cancelled = cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "human",
        {"reason": "requirement withdrawn", "disposition": "not_applicable"},
    )
    assert cancelled.state == TaskState.CANCELLED.value


def test_a_parked_task_is_never_dispatched(cp):
    """It must not be handed to a worker while the question is outstanding."""
    task = cp.create_task("ambiguous work", required_capabilities=["python"])
    machine = cp.register_machine("host", resources={"cpu": 4, "memory_gb": 8})
    worker = cp.register_agent(machine.id, "worker", capabilities=["python"])
    _park(cp, task)

    assert task.id not in {t.id for t in cp.ready_tasks()}
    with pytest.raises((ValidationError, Exception)):
        cp.claim_task(task.id, worker.id)
    assert cp.get_task(task.id).state == TaskState.NEEDS_INPUT.value


def test_the_ledger_audit_does_not_call_it_a_contradiction(cp):
    """A parked task is a legitimate resting place, not state corruption.

    It previously fell through to `unknown_task_state`/`repair_task_state`,
    which is precisely how an automated repair pass would have collected it.
    """
    from mac.task_ledger_audit import _assessment

    verdict = _assessment(
        {"task": {"id": "t1", "state": TaskState.NEEDS_INPUT.value, "metadata": {}}},
        [],
        {},
    )
    assert verdict["verdict"] == "active_valid"
    assert "task_awaiting_human_input" in verdict["findings"]
    assert "unknown_task_state" not in verdict["findings"]


def test_it_is_reachable_from_every_state_where_a_question_can_arise(cp):
    """An agent discovers it needs an answer mid-flight, not just at pickup."""
    from mac.models import TASK_TRANSITIONS

    for origin in (
        TaskState.OPEN,
        TaskState.WAITING,
        TaskState.BLOCKED,
        TaskState.CLAIMED,
        TaskState.RUNNING,
        TaskState.NEEDS_REVIEW,
        TaskState.REVIEWING,
    ):
        assert TaskState.NEEDS_INPUT.value in TASK_TRANSITIONS[origin.value], origin


# --- answering is a judgement, not automatically a release ------------------
#
# This used to transition unconditionally to OPEN. During the 2026-08-03
# triage, 12 of 27 parked questions were answered "no longer necessary" or
# "superseded" -- answering them put 12 unwanted tasks back in front of the
# fleet, and every one had to be cancelled immediately afterwards.


def test_an_answer_that_means_stop_closes_the_task(cp):
    task = cp.create_task("work whose premise expired")
    _park(cp, task)

    answered = cp.answer_task_input(
        task.id,
        "No longer necessary: the vendored tree this targets is being retired.",
        "jordan",
        disposition="not_applicable",
    )

    assert answered.state == TaskState.CANCELLED.value
    record = answered.metadata["needs_input_history"][-1]
    assert record["resolved_to"] == TaskState.CANCELLED.value
    assert "No longer necessary" in record["answer"]


def test_a_superseding_task_is_recorded_and_implies_cancel(cp):
    replacement = cp.create_task("the task that supersedes it")
    task = cp.create_task("superseded work")
    _park(cp, task)

    answered = cp.answer_task_input(
        task.id,
        "Superseded.",
        "jordan",
        disposition="superseded",
        replaced_by=replacement.id,
    )

    assert answered.state == TaskState.CANCELLED.value
    event = [
        e for e in cp.task_history(task.id)
        if e.to_state == TaskState.CANCELLED.value
    ][-1]
    assert event.detail["replacement_task_id"] == replacement.id


def test_a_dangling_replacement_is_refused(cp):
    task = cp.create_task("superseded work")
    _park(cp, task)

    with pytest.raises(NotFoundError):
        cp.answer_task_input(
            task.id, "Superseded.", "jordan",
            disposition="superseded", replaced_by="task_does_not_exist",
        )
    # The task is left parked rather than half-disposed.
    assert cp.get_task(task.id).state == TaskState.NEEDS_INPUT.value


def test_disposition_is_required_and_validated(cp):
    task = cp.create_task("ambiguous work")
    _park(cp, task)

    with pytest.raises(ValidationError):
        cp.answer_task_input(task.id, "an answer", "jordan", disposition="")
    with pytest.raises(ValidationError):
        cp.answer_task_input(task.id, "an answer", "jordan", disposition="maybe")
    # replaced_by is meaningless when the task is being released.
    with pytest.raises(ValidationError):
        cp.answer_task_input(
            task.id, "an answer", "jordan",
            disposition="resume", replaced_by="task_whatever",
        )
    assert cp.get_task(task.id).state == TaskState.NEEDS_INPUT.value
