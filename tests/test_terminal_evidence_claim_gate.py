"""Terminal evidence makes a row non-claimable, and reopening it needs lineage.

The control-plane half of ADR-0121 finding 4. Reproduces the live incident:
``task_f33a2da7`` merged as PR #498, was reopened on a fleet restart without
anyone checking that its work had landed, and was immediately claimed and
re-implemented.
"""

from __future__ import annotations

import pytest

from mac.models import TaskState, TransitionError
from mac.services import ControlPlane


MERGED_PR = "https://github.com/example/mac/pull/498"


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _agent(cp, name="worker"):
    machine = cp.register_machine("%s-host" % name, resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, name, capabilities=[])


def _merged_pull_request_evidence(cp, task_id, actor="human"):
    return cp.add_evidence(
        task_id,
        "repo_change",
        "https://example.invalid/evidence",
        "work landed as a merged pull request",
        actor,
        metadata={
            "verification": {
                "repo": {
                    "pull_request": {
                        "merged": True,
                        "number": 498,
                        "url": MERGED_PR,
                    }
                }
            }
        },
        _trusted_internal=True,
    )


def test_an_open_row_whose_pull_request_merged_is_not_claimable(cp):
    task = cp.create_task("implement the module")
    agent = _agent(cp)
    _merged_pull_request_evidence(cp, task.id)

    assert cp.get_task(task.id).state == TaskState.OPEN.value
    with pytest.raises(TransitionError) as excinfo:
        cp.claim_task(task.id, agent.id)

    message = str(excinfo.value)
    assert "not claimable" in message
    assert "/pull/498" in message
    # The refusal has to be actionable, not just a denial.
    assert "replacement" in message


def test_terminal_evidence_is_reportable_without_attempting_a_claim(cp):
    task = cp.create_task("implement the module")
    _merged_pull_request_evidence(cp, task.id)

    verdict = cp.terminal_evidence_for_task(task.id)
    assert verdict["present"] is True
    assert verdict["kind"] == "merged_pull_request"
    assert verdict["task_id"] == task.id
    assert "/pull/498" in verdict["summary"]


def test_a_row_without_terminal_evidence_still_claims_normally(cp):
    task = cp.create_task("implement the module")
    agent = _agent(cp)
    claimed, lease = cp.claim_task(task.id, agent.id)
    assert claimed.state == TaskState.CLAIMED.value
    assert lease.agent_id == agent.id


def test_reopening_a_task_whose_work_landed_refuses_and_says_why(cp):
    task = cp.create_task("implement the module")
    _merged_pull_request_evidence(cp, task.id)
    cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "human",
        {"reason": "stopping the run", "disposition": "preserve"},
    )

    with pytest.raises(TransitionError) as excinfo:
        cp.reopen_task(task.id, "human", "fleet restart")

    message = str(excinfo.value)
    assert "refusing to reopen" in message
    assert "already exists" in message
    assert "replace=true" in message
    assert cp.get_task(task.id).state == TaskState.CANCELLED.value


def test_reopen_with_replace_creates_a_linked_replacement_row(cp):
    task = cp.create_task("implement the module", project="mac", priority=5)
    _merged_pull_request_evidence(cp, task.id)
    cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "human",
        {"reason": "stopping the run", "disposition": "preserve"},
    )

    replacement = cp.reopen_task(
        task.id, "human", "fleet restart", replace=True
    )

    assert replacement.id != task.id
    assert replacement.state == TaskState.OPEN.value
    assert replacement.project == "mac"
    assert replacement.priority == 5
    lineage = replacement.metadata["lineage"]
    assert lineage["retry_of"] == {"kind": "task", "ref": task.id}
    assert lineage["terminal_evidence_acknowledged"]["task_id"] == task.id
    # The original stays terminal: a replacement row is created INSTEAD of
    # re-dispatching this one, which is the whole point.
    assert cp.get_task(task.id).state == TaskState.CANCELLED.value


def test_the_replacement_row_is_claimable_and_the_original_stays_blocked(cp):
    task = cp.create_task("implement the module")
    _merged_pull_request_evidence(cp, task.id)
    cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "human",
        {"reason": "stopping the run", "disposition": "preserve"},
    )
    replacement = cp.reopen_task(task.id, "human", "fleet restart", replace=True)

    agent = _agent(cp)
    claimed, _lease = cp.claim_task(replacement.id, agent.id)
    assert claimed.state == TaskState.CLAIMED.value


def test_lineage_is_queryable_in_both_directions_from_the_control_plane(cp):
    task = cp.create_task("implement the module")
    _merged_pull_request_evidence(cp, task.id)
    cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "human",
        {"reason": "stopping the run", "disposition": "preserve"},
    )
    replacement = cp.reopen_task(task.id, "human", "fleet restart", replace=True)

    prior_view = cp.task_lineage(task.id)
    assert [entry["source"]["ref"] for entry in prior_view["replaced_by"]] == [
        replacement.id
    ]
    assert prior_view["terminal_evidence"]["present"] is True

    successor_view = cp.task_lineage(replacement.id)
    assert [entry["target"]["ref"] for entry in successor_view["replaces"]] == [task.id]
    assert successor_view["replaced_by"] == []


def test_reopening_an_ordinary_failed_task_is_unchanged(cp):
    task = cp.create_task("implement the module")
    cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "human",
        {"reason": "stopping the run", "disposition": "preserve"},
    )
    reopened = cp.reopen_task(task.id, "human", "try again")
    assert reopened.id == task.id
    assert reopened.state == TaskState.OPEN.value


def test_reopening_only_to_cancel_is_not_blocked_by_terminal_evidence(cp):
    # `mac task cancel` on a FAILED task passes through OPEN because
    # failed -> cancelled is not a legal transition. That reopen never
    # dispatches, and cancelling IS the reconciliation the gate wants -- so
    # refusing it would block the correct response to terminal evidence.
    task = cp.create_task("implement the module")
    agent = _agent(cp)
    cp.claim_task(task.id, agent.id)
    _merged_pull_request_evidence(cp, task.id)
    cp._transition_task_internal(
        task.id, TaskState.FAILED.value, "dispatcher", {"reason": "attempt failed"}
    )
    assert cp.get_task(task.id).state == TaskState.FAILED.value

    reopened = cp.reopen_task(
        task.id, "human", "reopened only to cancel: superseded", for_cancellation=True
    )
    assert reopened.state == TaskState.OPEN.value
    cancelled = cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "human",
        {
            "disposition": "superseded",
            "replacement_pull_request": MERGED_PR,
            "reason": "the work landed as PR 498",
        },
    )
    assert cancelled.state == TaskState.CANCELLED.value


def test_cancelling_as_superseded_can_name_a_merged_pull_request(cp):
    task = cp.create_task("implement the module")
    cancelled = cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "human",
        {
            "disposition": "superseded",
            "replacement_pull_request": MERGED_PR,
            "reason": "the work landed as PR 498",
        },
    )
    lifecycle = cancelled.metadata["repository_ref_lifecycle"]
    assert lifecycle["disposition"] == "superseded"
    assert lifecycle["replacement_pull_request"] == MERGED_PR
    assert lifecycle["replacement_task_id"] is None
