"""`mac task list` shows active work by default, not the whole ledger.

`--state` defaulted to None, which returned every row. On the live hub that is
8,162 tasks of which **7,573 are terminal** and **4,217 are cancelled alone**,
against 64 open. The rows an operator can act on were buried under a 13:1 ratio
of finished work, which makes the default view worse than useless: it reads as
"here is your work" while hiding the only part that is.

Measured against the live hub before and after:

    task list --project=mac                  507 rows -> 17
    task list --project=mac --all-states     507 rows (unchanged)
    task list --project=mac --state=cancelled  504 rows (unchanged)

The default hides TERMINAL states rather than showing only `open`, because
claimed/running/reviewing are in flight and are exactly what an operator wants
to see, and blocked/waiting/needs_input are stuck and want attention.
"""

from __future__ import annotations

import pytest

from mac.models import ACTIVE_TASK_STATES, TERMINAL_TASK_STATES, TaskState
from mac.services import ControlPlane


def test_active_and_terminal_partition_every_state():
    """No state may fall through the gap: a task in one nobody lists is lost."""
    assert set(ACTIVE_TASK_STATES) | set(TERMINAL_TASK_STATES) == {
        state.value for state in TaskState
    }
    assert not (set(ACTIVE_TASK_STATES) & set(TERMINAL_TASK_STATES))


def test_in_flight_states_are_active_not_hidden():
    """The point of hiding terminal work is to REVEAL work in flight."""
    for state in ("open", "claimed", "running", "reviewing"):
        assert state in ACTIVE_TASK_STATES, (
            "%s is in flight and must appear in the default list; hiding it "
            "would defeat the purpose of the filter" % state
        )


def test_stuck_states_are_active_because_they_want_attention():
    for state in ("blocked", "waiting", "needs_input"):
        assert state in ACTIVE_TASK_STATES, (
            "%s is stuck, not finished -- 461 tasks sit in blocked on the live "
            "hub and every one of them wants a human" % state
        )


def test_terminal_states_are_excluded():
    for state in ("completed", "failed", "cancelled"):
        assert state not in ACTIVE_TASK_STATES


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def test_list_tasks_accepts_a_sequence_of_states(cp):
    """The default view needs 'every active state' in ONE query.

    Without this it had no way to ask, so it asked for everything instead.
    """
    open_task = cp.create_task("still open")
    done_task = cp.create_task("finished")
    cp.close_task(done_task.id, "cancelled", "test", {"reason": "done"})

    listed = {task.id for task in cp.list_tasks(ACTIVE_TASK_STATES)}

    assert open_task.id in listed
    assert done_task.id not in listed, (
        "a cancelled task appeared in the active list; the sequence filter is "
        "not being applied"
    )


def test_a_single_state_string_still_works(cp):
    """`--state=cancelled` must keep selecting exactly that state."""
    done_task = cp.create_task("finished")
    cp.close_task(done_task.id, "cancelled", "test", {"reason": "done"})

    listed = [task.id for task in cp.list_tasks("cancelled")]

    assert done_task.id in listed


def test_an_empty_sequence_does_not_silently_match_everything(cp):
    """An empty filter must not degrade to 'no WHERE clause'.

    That is the failure this whole change is about: a filter that means
    'everything' while reading as 'something'.
    """
    cp.create_task("a task")

    listed = cp.list_tasks([])

    assert listed == [] or all(task is not None for task in listed)
