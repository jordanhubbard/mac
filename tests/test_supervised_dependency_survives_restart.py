"""A supervised unsatisfied-dependency record must survive a hub restart.

Regression for the stranding observed on the live fleet (task_0ee0b7ce). The
decomposition of `worker.py ignores CARGO_HOME ...` produced an
implement -> test -> verify child chain. The implement child failed, the
reconciler correctly supervised the test child (BLOCKED, with
`dependency_resolution.status = "unsatisfied"`), and the integration parent's
`all_settled` join could then have settled on it.

Twelve hours later a hub restart ran
`_reconcile_legacy_task_state_semantics`, which recognised only the history
detail -- reason "dependencies_incomplete", `manual_repair_required` False --
and could not tell a supervised record from the pre-WAITING legacy encoding.
It reverted the child to WAITING. WAITING satisfies no join, so the child
waited forever on a terminally failed dependency and its parent waited forever
on the child.

The dispatcher sweep already honoured the marker through
`_blocked_task_requires_manual_repair`; the startup migration did not.
"""
from __future__ import annotations

from mac.models import TaskState
from mac.services import ControlPlane


def _decompose_with_sibling_chain(cp):
    """Parent -> [implement, test(depends_on implement)], as the planner emits."""
    parent = cp.create_task(
        title="worker.py ignores CARGO_HOME on relocated installs",
        description="parent that the planner decomposed",
        project="mac",
    )
    result = cp.add_child_tasks(
        parent.id,
        [
            {"title": "Implement the CARGO_HOME branch", "node_id": "implement"},
            {"title": "Test the CARGO_HOME branch", "depends_on": ["implement"]},
        ],
    )
    implement_id, test_id = [child["id"] for child in result["children"]]
    return parent.id, implement_id, test_id


def test_supervised_child_is_not_reverted_to_waiting_by_the_startup_migration():
    cp = ControlPlane.in_memory()
    parent_id, implement_id, test_id = _decompose_with_sibling_chain(cp)

    cp._transition_task_internal(
        implement_id,
        TaskState.FAILED.value,
        "test",
        {"reason": "executor_failed"},
    )

    supervised = cp.get_task(test_id)
    assert supervised.state == TaskState.BLOCKED.value
    assert supervised.metadata["dependency_resolution"]["status"] == "unsatisfied"

    # Every ControlPlane construction runs this, so this is one hub restart.
    cp._reconcile_legacy_task_state_semantics()
    cp._reconcile_legacy_task_state_semantics()

    after_restart = cp.get_task(test_id)
    assert after_restart.state == TaskState.BLOCKED.value, (
        "the supervised child was reverted to WAITING, which satisfies no join "
        "and strands its parent forever"
    )
    assert after_restart.metadata["dependency_resolution"]["status"] == "unsatisfied"
    assert not [
        event
        for event in cp.task_history(test_id)
        if event.event_type == "task.state_semantics_migrated"
    ]


def test_a_failed_child_lets_the_parent_settle_across_a_restart():
    """The ticket's acceptance: a failing child must not strand the parent."""
    cp = ControlPlane.in_memory()
    parent_id, implement_id, test_id = _decompose_with_sibling_chain(cp)

    cp._transition_task_internal(
        implement_id,
        TaskState.FAILED.value,
        "test",
        {"reason": "executor_failed"},
    )
    cp._reconcile_legacy_task_state_semantics()

    parent = cp.get_task(parent_id)
    assert cp._dependency_join_policy(parent) == "all_settled"
    assert cp._dependencies_satisfied(parent), (
        "every child is terminal or supervised, so the all_settled parent must "
        "be satisfiable rather than waiting forever"
    )

    cp._unblock_ready_tasks()

    assert cp.get_task(parent_id).state == TaskState.OPEN.value


def test_supervision_ends_when_the_dependency_is_satisfied_after_all():
    """Supervision describes a dependency, so it must not outlive it.

    Keeping the marker across restarts is what lets an all_settled parent
    settle -- but a terminal dependency can still be replaced or
    force-completed later. If the marker froze the task permanently, this
    change would have swapped one permanent stall for another.
    """
    cp = ControlPlane.in_memory()
    prerequisite = cp.create_task("prerequisite", project="mac")
    dependent = cp.create_task(
        "dependent", dependencies=[prerequisite.id], project="mac"
    )

    cp._transition_task_internal(
        prerequisite.id, TaskState.CANCELLED.value, "test", {"reason": "fixture"}
    )
    supervised = cp.get_task(dependent.id)
    assert supervised.state == TaskState.BLOCKED.value
    assert supervised.metadata["dependency_resolution"]["status"] == "unsatisfied"

    # The dependency is resolved out of band, as a replacement or a
    # force-complete would do.
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.COMPLETED.value, prerequisite.id),
    )
    cp._reconcile_legacy_task_state_semantics()
    cp._unblock_ready_tasks()

    recovered = cp.get_task(dependent.id)
    assert recovered.state == TaskState.OPEN.value
    assert recovered.metadata["dependency_resolution"]["status"] == "resolved"


def test_an_actionable_block_is_not_recovered_by_a_satisfied_dependency():
    """Only dependency supervision is recoverable; a real failure still waits."""
    cp = ControlPlane.in_memory()
    prerequisite = cp.create_task("prerequisite", project="mac")
    dependent = cp.create_task(
        "dependent", dependencies=[prerequisite.id], project="mac"
    )
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.BLOCKED.value, dependent.id),
    )
    cp._record_history(
        dependent.id,
        "task.blocked",
        "worker",
        TaskState.RUNNING.value,
        TaskState.BLOCKED.value,
        {"reason": "executor_failed", "manual_repair_required": True},
    )
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.COMPLETED.value, prerequisite.id),
    )

    cp._reconcile_legacy_task_state_semantics()
    cp._unblock_ready_tasks()

    assert cp.get_task(dependent.id).state == TaskState.BLOCKED.value


def test_a_genuine_legacy_dependency_block_still_migrates():
    """The migration must keep working for what it was actually written for."""
    cp = ControlPlane.in_memory()
    prerequisite = cp.create_task("legacy prerequisite", project="mac")
    dependent = cp.create_task(
        "legacy dependent",
        dependencies=[prerequisite.id],
        project="mac",
    )
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.BLOCKED.value, dependent.id),
    )
    cp._record_history(
        dependent.id,
        "task.legacy_fixture",
        "legacy",
        TaskState.WAITING.value,
        TaskState.BLOCKED.value,
        {"reason": "waiting_on_dependencies", "dependencies": [prerequisite.id]},
    )

    cp._reconcile_legacy_task_state_semantics()

    assert cp.get_task(dependent.id).state == TaskState.WAITING.value
