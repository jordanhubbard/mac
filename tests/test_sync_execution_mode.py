"""A sync task owns its agent; an async task never waits for one.

Every task today is async: take any eligible work, up to capacity, in any
order. That is right for ordinary work and wrong for work that mutates the
WORKER -- rolling out a new sandbox image cannot run beside the tasks whose
sandbox it is replacing.

The barrier is only useful if it holds in the awkward cases, so that is what
these cover: a barrier that starves behind incoming work, a barrier that
reorders under priority, a barrier that leaks onto an unrelated agent, and a
barrier bypassed by break-glass.
"""

from __future__ import annotations

import pytest

from mac.allocator import (
    AGENT_SYNC_BARRIER,
    EXECUTION_MODE_ASYNC,
    EXECUTION_MODE_SYNC,
    TASK_SYNC_UNTARGETED,
    AllocationAgent,
    AllocationTask,
    evaluate_pair,
    evaluate_task,
    normalize_execution_mode,
    rejection_kind,
)


def _task(task_id="task_1", *, mode=EXECUTION_MODE_ASYNC, target=None, priority=1,
          created_at="2026-08-08T00:00:00+00:00"):
    return AllocationTask(
        id=task_id,
        priority=priority,
        created_at=created_at,
        execution_mode=mode,
        target_agent_id=target,
    )


def _agent(agent_id="agent_1", *, capacity=4, active=0, head=None, running=False):
    return AllocationAgent(
        id=agent_id,
        capacity=capacity,
        active_leases=active,
        sync_queue_head_task_id=head,
        sync_task_running=running,
    )


def _barrier_reasons(evaluation):
    return [
        reason
        for reason in evaluation.agent_rejections
        if reason.split(":", 1)[0] == AGENT_SYNC_BARRIER
    ]


# --------------------------------------------------------------------------
# The default is unchanged
# --------------------------------------------------------------------------


def test_ordinary_work_is_async_and_unaffected():
    """Every existing task takes the default. If this needs a mode set, the
    change was not backwards compatible."""
    assert evaluate_pair(_task(), _agent(active=3)).allowed


@pytest.mark.parametrize("value", [None, "", "ASYNC", "nonsense", "syncish"])
def test_an_unrecognized_mode_reads_as_async(value):
    """Fail-open in this one direction: an unknown mode read as sync would
    quiesce a worker over a typo."""
    assert normalize_execution_mode(value) == EXECUTION_MODE_ASYNC


def test_sync_is_recognized_case_insensitively():
    assert normalize_execution_mode("SYNC") == EXECUTION_MODE_SYNC


# --------------------------------------------------------------------------
# A barrier waits for the agent to drain
# --------------------------------------------------------------------------


def test_a_sync_task_does_not_start_until_the_agent_has_drained():
    evaluation = evaluate_pair(
        _task(mode=EXECUTION_MODE_SYNC, target="agent_1"),
        _agent(active=1, head="task_1"),
    )

    assert not evaluation.allowed
    assert "%s:not_drained" % AGENT_SYNC_BARRIER in evaluation.agent_rejections


def test_a_sync_task_starts_once_the_agent_is_empty():
    evaluation = evaluate_pair(
        _task(mode=EXECUTION_MODE_SYNC, target="agent_1"),
        _agent(active=0, head="task_1"),
    )

    assert evaluation.allowed


def test_capacity_alone_would_not_have_been_enough():
    """A worker with capacity 4 and one task running has free slots. Only the
    drain rule stops the barrier landing beside that task."""
    agent = _agent(capacity=4, active=1, head="task_1")
    assert agent.free_slots > 0

    assert not evaluate_pair(_task(mode=EXECUTION_MODE_SYNC, target="agent_1"), agent).allowed


# --------------------------------------------------------------------------
# The barrier quiesces the agent, or it starves
# --------------------------------------------------------------------------


def test_a_pending_barrier_stops_the_agent_taking_new_async_work():
    """The rule the whole thing turns on.

    If the agent kept accepting async work while a barrier waited,
    active_leases would never reach zero and the barrier would starve -- and
    the busiest workers, the ones most needing an image update, would be the
    ones that never got one.
    """
    evaluation = evaluate_pair(_task("task_2"), _agent(active=1, head="task_1"))

    assert not evaluation.allowed
    assert "%s:draining" % AGENT_SYNC_BARRIER in evaluation.agent_rejections


def test_a_running_barrier_stops_new_async_work_and_says_so():
    """Distinguished from draining on purpose: "waiting for this worker to
    finish" and "this worker is being updated right now" are different answers
    to an operator asking why nothing is dispatching."""
    evaluation = evaluate_pair(
        _task("task_2"), _agent(active=1, head="task_1", running=True)
    )

    assert "%s:running" % AGENT_SYNC_BARRIER in evaluation.agent_rejections


def test_an_agent_with_no_barrier_takes_async_work_normally():
    assert evaluate_pair(_task("task_2"), _agent(active=1)).allowed


# --------------------------------------------------------------------------
# Barriers run serially, oldest first
# --------------------------------------------------------------------------


def test_two_barriers_run_oldest_first_even_when_the_younger_outranks_it():
    """FIFO, not priority. A barrier that reorders under priority is not a
    barrier: the update that was supposed to run last would run first."""
    younger_but_urgent = _task(
        "task_2", mode=EXECUTION_MODE_SYNC, target="agent_1",
        priority=99, created_at="2026-08-08T12:00:00+00:00",
    )
    agent = _agent(active=0, head="task_1")  # task_1 is older

    evaluation = evaluate_pair(younger_but_urgent, agent)

    assert not evaluation.allowed
    assert "%s:fifo" % AGENT_SYNC_BARRIER in evaluation.agent_rejections


def test_the_oldest_barrier_is_the_one_that_runs():
    agent = _agent(active=0, head="task_1")

    assert evaluate_pair(
        _task("task_1", mode=EXECUTION_MODE_SYNC, target="agent_1"), agent
    ).allowed


# --------------------------------------------------------------------------
# A barrier is one agent's business
# --------------------------------------------------------------------------


def test_a_barrier_on_one_agent_does_not_quiesce_another():
    """Otherwise a rolling image update stops the entire fleet at once, which
    is the opposite of rolling."""
    other = _agent("agent_2", active=1)

    assert evaluate_pair(_task("task_2"), other).allowed


def test_a_barrier_targeted_elsewhere_is_not_placeable_here():
    evaluation = evaluate_pair(
        _task(mode=EXECUTION_MODE_SYNC, target="agent_2"), _agent("agent_1")
    )

    assert not evaluation.allowed


# --------------------------------------------------------------------------
# A barrier must name its agent
# --------------------------------------------------------------------------


def test_an_untargeted_barrier_is_refused():
    """"Wait for all tasks to complete" is ambiguous between one worker and the
    fleet, and the fleet reading is a global stop-the-world. Refuse rather than
    let whichever code path runs first decide."""
    evaluation = evaluate_task(_task(mode=EXECUTION_MODE_SYNC))

    assert TASK_SYNC_UNTARGETED in evaluation.task_rejections


def test_a_targeted_barrier_is_not_refused_for_that_reason():
    evaluation = evaluate_task(_task(mode=EXECUTION_MODE_SYNC, target="agent_1"))

    assert TASK_SYNC_UNTARGETED not in evaluation.task_rejections


# --------------------------------------------------------------------------
# Break-glass does not open the barrier
# --------------------------------------------------------------------------


def test_break_glass_does_not_run_work_inside_a_sandbox_being_replaced():
    """Break-glass forces a task past ROUTING bars. Running it beside a sync
    task would put it in a sandbox being swapped underneath it, which is host
    safety -- the same reason capacity and health are checked unconditionally.
    """
    task = AllocationTask(
        id="task_2",
        priority=1,
        created_at="2026-08-08T00:00:00+00:00",
        break_glass_agent_id="agent_1",
    )

    evaluation = evaluate_pair(task, _agent(head="task_1", running=True))

    assert not evaluation.allowed
    assert _barrier_reasons(evaluation)


def test_a_barrier_is_transient_not_a_fleet_requirement_gap():
    """It clears when the agent drains. Classified as anything else, the
    eligibility diagnostic reports a fleet that cannot meet the task's
    requirements when the real answer is "this worker is being updated" --
    the same misdirection the :excluded / :pinned split was added to fix.
    """
    assert rejection_kind("%s:draining" % AGENT_SYNC_BARRIER) == "transient"
    assert rejection_kind("%s:not_drained" % AGENT_SYNC_BARRIER) == "transient"
