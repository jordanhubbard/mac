"""A parent released over cancelled children must be able to tell.

Observed end to end on 2026-08-05, in about five minutes:

    17:17:14  four children cancelled (task_a308126c, a12723af, b2ef42ea,
              b4906951) -- approved but undeliverable, see task_ce6c8ea3
    17:17:45  task_c5407d23 "Adjudicate quarantined curiosity candidates"
              went waiting -> open -> claimed -> running, having waited
              since 2026-07-31
    17:19:59  attempt 2 blocked: verification_contract_failed
    17:22:41  attempt 3 blocked: same
    17:22:53  failed, 3/3

The executing agent's own diagnosis named the cause: "missing tool/store +
MISSING CHILD OUTPUTS". The parent adjudicates candidates its children were
supposed to enumerate. They had just been cancelled, so those outputs will
never exist.

Its policy is {"join": "all_settled", "on_unsatisfied": "supervise"}, and
``all_settled`` counts CANCELLED as settled. That is CORRECT and stays: a
cancelled dependency must not block a parent forever. The defect is that
"settled" collapsed two materially different facts --

    the dependency RAN and produced output      -> the parent can proceed
    the dependency was CANCELLED, producing none -> it cannot

-- and nothing recorded which had happened, so neither the executing agent nor
the operator could tell the two apart at dispatch time.

It landed safely only because that contract happens to be fail-closed and the
agent was careful enough to say "missing child outputs" rather than adjudicate
an empty set. A task without that clause would have produced a confident result
over nothing, which is worse than failing.

These tests cover the two halves that were missing (items 1 and 2 of the
filing): the settlement provenance the parent carries, and the warning at
cancel time. Item 3 -- declining to dispatch when everything settled by
cancellation -- is deliberately NOT implemented, and one test pins that, since
doing it would reintroduce the permanent stall ``all_settled`` exists to
prevent.
"""

from __future__ import annotations

import pytest

from mac.models import TaskState
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


def _integration_parent(cp, children):
    """A cooperative-integration parent, as the curiosity adjudicator was."""
    return cp.create_task(
        "adjudicate what the children enumerated",
        project="mac",
        dependencies=[child.id for child in children],
        metadata={
            "coordination": {"mode": "cooperative_integration"},
            "dependency_policy": {"join": "all_settled", "on_unsatisfied": "supervise"},
        },
    )


def _child(cp, title="enumerate candidates"):
    return cp.create_task(title, project="mac")


def _finish(cp, task, state):
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?", (state, task.id)
    )


# --------------------------------------------------------------------------
# The settlement record
# --------------------------------------------------------------------------


def test_a_completed_dependency_reports_output(cp):
    child = _child(cp)
    parent = _integration_parent(cp, [child])
    _finish(cp, child, TaskState.COMPLETED.value)

    settlement = cp._dependency_settlement(cp.get_task(parent.id))

    assert settlement["all_inputs_present"] is True
    assert settlement["settled_without_output"] == []
    assert settlement["dependencies"][child.id]["produced_output"] is True


def test_a_cancelled_dependency_reports_no_output(cp):
    """The regression. Settled, yes -- but it produced nothing."""
    child = _child(cp)
    parent = _integration_parent(cp, [child])
    _finish(cp, child, TaskState.CANCELLED.value)

    settlement = cp._dependency_settlement(cp.get_task(parent.id))

    assert settlement["all_inputs_present"] is False
    assert settlement["settled_without_output"] == [child.id]
    assert settlement["dependencies"][child.id]["produced_output"] is False
    # Still SATISFIED: all_settled is working correctly and must keep doing so.
    assert settlement["dependencies"][child.id]["satisfied"] is True


def test_a_failed_dependency_also_reports_no_output(cp):
    child = _child(cp)
    parent = _integration_parent(cp, [child])
    _finish(cp, child, TaskState.FAILED.value)

    settlement = cp._dependency_settlement(cp.get_task(parent.id))

    assert settlement["settled_without_output"] == [child.id]


def test_a_partly_cancelled_family_names_only_the_empty_ones(cp):
    """The operator needs to know WHICH inputs are missing, not just that some are."""
    ran = _child(cp, "enumerated fine")
    cancelled = _child(cp, "never ran")
    parent = _integration_parent(cp, [ran, cancelled])
    _finish(cp, ran, TaskState.COMPLETED.value)
    _finish(cp, cancelled, TaskState.CANCELLED.value)

    settlement = cp._dependency_settlement(cp.get_task(parent.id))

    assert settlement["settled_without_output"] == [cancelled.id]
    assert settlement["all_inputs_present"] is False


def test_an_unreadable_dependency_counts_as_no_output(cp):
    """Fail toward warning: a dependency that cannot be read produced nothing.

    Driven by passing the id explicitly rather than by deleting the task,
    because deleting a task removes its edge too -- so a dangling edge is not
    reachable that way, and a test that deleted the row would be asserting on
    an empty dependency list while appearing to assert on a missing one.
    """
    parent = _integration_parent(cp, [_child(cp)])

    settlement = cp._dependency_settlement(
        cp.get_task(parent.id), dependency_ids=["task_does_not_exist"]
    )

    assert settlement["settled_without_output"] == ["task_does_not_exist"]
    assert settlement["dependencies"]["task_does_not_exist"]["state"] == "missing"


def test_the_settlement_is_stamped_on_the_task_the_executor_reads(cp):
    """An executor reads task.json, not the transition history.

    Recording this only in the transition detail would leave the agent in
    exactly the position that cost three attempts: running, with no way to see
    that its inputs were destroyed.
    """
    child = _child(cp)
    parent = _integration_parent(cp, [child])
    _finish(cp, child, TaskState.CANCELLED.value)
    reloaded = cp.get_task(parent.id)

    settlement = cp._dependency_settlement(reloaded)
    updated = cp._record_dependency_settlement(reloaded, settlement)

    recorded = updated.metadata["dependency_resolution"]["settlement"]
    assert recorded["settled_without_output"] == [child.id]


def test_a_clean_settlement_is_not_stamped(cp):
    """Only the surprising case is worth carrying; noise is what gets ignored."""
    child = _child(cp)
    parent = _integration_parent(cp, [child])
    _finish(cp, child, TaskState.COMPLETED.value)
    reloaded = cp.get_task(parent.id)

    settlement = cp._dependency_settlement(reloaded)
    updated = cp._record_dependency_settlement(reloaded, settlement)

    resolution = (updated.metadata or {}).get("dependency_resolution") or {}
    assert "settlement" not in resolution


# --------------------------------------------------------------------------
# all_settled must keep working
# --------------------------------------------------------------------------


def test_a_cancelled_dependency_still_satisfies_the_join(cp):
    """The half that must NOT change.

    all_settled exists so a cancelled dependency cannot block a parent for
    ever. Reporting the settlement must not become gating it -- that is item 3
    in the filing, deliberately not taken, because it would trade a wasted
    attempt for a permanent stall.
    """
    child = _child(cp)
    parent = _integration_parent(cp, [child])
    _finish(cp, child, TaskState.CANCELLED.value)

    assert cp._dependencies_satisfied(cp.get_task(parent.id)) is True


def test_all_success_is_unaffected(cp):
    """A non-integration parent must still refuse to run on a cancelled child."""
    child = _child(cp)
    parent = cp.create_task(
        "ordinary downstream work", project="mac", dependencies=[child.id]
    )
    _finish(cp, child, TaskState.CANCELLED.value)

    assert cp._dependencies_satisfied(cp.get_task(parent.id)) is False


# --------------------------------------------------------------------------
# The cancel-time warning
# --------------------------------------------------------------------------


def _warned(cp, name):
    events = cp.observability.list_observability(name=name, limit=20)
    return [event.detail for event in events]


def test_cancelling_a_child_warns_that_it_releases_the_parent(cp):
    """The cheap half: show the consequence BEFORE it happens.

    Cancelling a stale dependency is routine queue hygiene. On 2026-08-05 it
    silently converted a parked parent into a running one with no inputs, and
    nothing said so.
    """
    child = _child(cp)
    parent = _integration_parent(cp, [child])
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.WAITING.value, parent.id),
    )
    _finish(cp, child, TaskState.CANCELLED.value)

    cp._resolve_waiting_dependents_of(
        child.id, TaskState.CANCELLED.value, "operator"
    )

    warnings = _warned(cp, "task.cancellation_releases_integration_parent")
    assert warnings, "cancelling a dependency released a parent with no warning"
    assert warnings[0]["parent"] == parent.id
    assert warnings[0]["cancelled_dependency"] == child.id


def test_a_failed_child_does_not_raise_the_cancellation_warning(cp):
    """This warning is about an OPERATOR ACTION with a non-obvious consequence.

    A child that failed on its own is the system reporting its own outcome;
    firing the same warning there would dilute the one that means "something
    you just did released a task".
    """
    child = _child(cp)
    parent = _integration_parent(cp, [child])
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.WAITING.value, parent.id),
    )
    _finish(cp, child, TaskState.FAILED.value)

    cp._resolve_waiting_dependents_of(child.id, TaskState.FAILED.value, "worker")

    assert not _warned(cp, "task.cancellation_releases_integration_parent")
