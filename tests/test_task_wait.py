"""`mac task wait` must always terminate, and must not lie when it does.

A wait command is only trusted if two things hold: it returns when the work is
done, and it never returns while work it claimed to be watching is still
running. Everything here is one of those two, plus the cases that break them --
a task that stalls on a human, a task that decomposes mid-wait, an event the
feed never delivered.
"""

from __future__ import annotations

import pytest

from mac.task_wait import (
    LEFT_FINISHED,
    LEFT_STALLED,
    TaskWait,
    dedupe_events,
    departure_reason,
    is_waitable,
    waitable_tasks,
)


def _task(task_id, state, project="mac"):
    return {"id": task_id, "state": state, "project": project}


def _transition(task_id, to_state, event_id="hist_1", at="2026-08-09T00:00:00+00:00"):
    return {
        "id": event_id,
        "event_type": "task.transitioned",
        "subject_id": task_id,
        "created_at": at,
        "actor": "agent_1",
        "detail": {"from_state": "claimed", "to_state": to_state},
    }


# --------------------------------------------------------------------------
# What is waited on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state", ["open", "waiting", "claimed", "running", "needs_review", "reviewing"]
)
def test_active_or_activatable_states_are_waited_on(state):
    assert is_waitable(state)


def test_waiting_is_waited_on_despite_the_name():
    """It reads like "waiting for input" and is not -- it is a dependency wait,
    which resolves on its own. Excluding it would drop exactly the tasks a wait
    exists to watch."""
    assert is_waitable("waiting")


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
def test_finished_tasks_are_not_waited_on(state):
    assert not is_waitable(state)
    assert departure_reason(state) == LEFT_FINISHED


@pytest.mark.parametrize("state", ["blocked", "needs_input"])
def test_stalled_tasks_are_not_waited_on(state):
    """needs_input waits on a human -- which, during a wait, is the person
    running the command. blocked waits on something this wait cannot see."""
    assert not is_waitable(state)
    assert departure_reason(state) == LEFT_STALLED


def test_the_initial_set_is_scoped_to_the_project():
    pending = waitable_tasks(
        [_task("task_1", "running"), _task("task_2", "running", project="other")],
        project="mac",
    )

    assert set(pending) == {"task_1"}


# --------------------------------------------------------------------------
# Termination
# --------------------------------------------------------------------------


def test_a_wait_with_nothing_pending_is_already_done():
    assert TaskWait({}).done


def test_the_wait_ends_when_the_last_task_finishes():
    wait = TaskWait({"task_1": "running"})

    wait.apply_event(_transition("task_1", "completed"))

    assert wait.done


def test_a_task_that_needs_a_human_leaves_rather_than_stalling_the_wait():
    """Otherwise the wait blocks on the person running it -- a deadlock with a
    command prompt on one end."""
    wait = TaskWait({"task_1": "running"})

    update = wait.apply_event(_transition("task_1", "needs_input"))

    assert wait.done
    assert update["reason"] == LEFT_STALLED


def test_a_departure_is_reported_not_swallowed():
    """"We stopped waiting on this" is information the caller needs. A wait
    that returns success having quietly abandoned half the work is worse than
    one that never returns."""
    wait = TaskWait({"task_1": "running", "task_2": "running"})
    wait.apply_event(_transition("task_1", "blocked"))
    wait.apply_event(_transition("task_2", "completed"))

    summary = wait.summary()

    assert summary["stalled"] == ["task_1"]
    assert summary["finished"] == ["task_2"]


def test_a_finished_task_is_not_re_added_by_a_later_rescan():
    """It would make the wait oscillate and never return."""
    wait = TaskWait({"task_1": "running"})
    wait.apply_event(_transition("task_1", "completed"))

    wait.rescan([_task("task_1", "running")], project="mac")

    assert wait.done


# --------------------------------------------------------------------------
# Not returning too early
# --------------------------------------------------------------------------


def test_a_task_created_mid_wait_joins_the_set():
    """A task that decomposes into children would otherwise let the wait return
    while its children are still running -- the wait would report done for work
    that had barely started."""
    wait = TaskWait({"task_1": "running"})
    wait.apply_event(_transition("task_1", "completed"))
    assert wait.done

    update = wait.rescan([_task("task_child", "open")], project="mac")

    assert not wait.done
    assert update[0]["event"] == "joined"


def test_follow_new_can_be_turned_off():
    wait = TaskWait({"task_1": "running"}, follow_new=False)
    wait.apply_event(_transition("task_1", "completed"))

    wait.rescan([_task("task_child", "open")], project="mac")

    assert wait.done


def test_a_new_task_that_is_already_finished_does_not_join():
    wait = TaskWait({}, follow_new=True)

    wait.rescan([_task("task_done", "completed")], project="mac")

    assert wait.done


# --------------------------------------------------------------------------
# The feed
# --------------------------------------------------------------------------


def test_a_transition_is_reported_with_where_it_came_from():
    wait = TaskWait({"task_1": "claimed"})

    update = wait.apply_event(_transition("task_1", "running"))

    assert update["event"] == "transitioned"
    assert update["from_state"] == "claimed"
    assert update["state"] == "running"


def test_a_repeated_state_is_not_reported_twice():
    """The cursor overlaps deliberately, so the same event arrives again. A
    feed that re-emits it is noise the caller has to filter."""
    wait = TaskWait({"task_1": "claimed"})
    wait.apply_event(_transition("task_1", "running"))

    assert wait.apply_event(_transition("task_1", "running")) is None


def test_events_for_other_tasks_are_ignored():
    wait = TaskWait({"task_1": "running"})

    assert wait.apply_event(_transition("task_other", "completed")) is None
    assert not wait.done


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        "junk",
        {"event_type": "task.lease_renewed", "subject_id": "task_1"},
        {"event_type": "task.transitioned", "subject_id": "task_1"},
        {"event_type": "task.transitioned", "subject_id": "task_1", "detail": {}},
    ],
)
def test_a_malformed_or_irrelevant_event_is_ignored(event):
    """The stream carries lease renewals and much else; only transitions move
    the wait."""
    wait = TaskWait({"task_1": "running"})

    assert wait.apply_event(event) is None
    assert not wait.done


def test_events_are_deduplicated_by_id():
    """The cursor is a timestamp, so resuming from the newest event re-delivers
    everything sharing that timestamp. Advancing past it instead would drop
    events recorded in the same tick."""
    seen = set()
    first = dedupe_events([_transition("task_1", "running", event_id="hist_a")], seen)
    second = dedupe_events([_transition("task_1", "running", event_id="hist_a")], seen)

    assert len(first) == 1
    assert second == []


def test_rescan_recovers_a_transition_the_feed_never_delivered():
    """Events can be missed -- a hub restart, a dropped poll. A wait that hangs
    forever on a task that finished unobserved is what sends people back to
    polling by hand."""
    wait = TaskWait({"task_1": "running"})

    wait.rescan([_task("task_1", "completed")], project="mac")

    assert wait.done
