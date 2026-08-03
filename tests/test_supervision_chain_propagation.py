"""Supervision must walk the chain, not stop at the first hop.

`_resolve_waiting_dependents_of` used to run only when a task went FAILED or
CANCELLED. A task waiting on a task that is BLOCKED-and-unsatisfiable will
never run either, but nothing marked it -- so with the planner's usual shape

    implement (failed)  <-  test  <-  verify

only `test` was supervised. `verify` waited forever on a task that would never
run, and the all_settled integration parent waited forever on `verify`.

Measured on the live ledger 2026-08-02: re-supervising one hop touched 168
tasks and freed ZERO; walking the chain freed 76. These tests pin the
behaviour at the reconciler, so a repair sweep is no longer required to reach
past the first hop.
"""
from __future__ import annotations

from mac.models import TaskState
from mac.services import ControlPlane


def _chain(cp, depth=3):
    """parent -> implement <- step1 <- step2 ... as the planner emits."""
    parent = cp.create_task("integration parent", project="mac")
    children = [{"title": "implement", "node_id": "n0"}]
    for index in range(1, depth):
        children.append(
            {
                "title": "step%d" % index,
                "node_id": "n%d" % index,
                "depends_on": ["n%d" % (index - 1)],
            }
        )
    result = cp.add_child_tasks(parent.id, children)
    return parent.id, [child["id"] for child in result["children"]]


def test_supervision_reaches_the_second_hop():
    cp = ControlPlane.in_memory()
    parent_id, ids = _chain(cp, depth=3)
    implement, step1, step2 = ids

    cp._transition_task_internal(
        implement, TaskState.FAILED.value, "test", {"reason": "executor_failed"}
    )

    assert cp.get_task(step1).state == TaskState.BLOCKED.value
    assert cp.get_task(step2).state == TaskState.BLOCKED.value, (
        "the second hop must be supervised by the reconciler itself"
    )
    for task_id in (step1, step2):
        resolution = cp.get_task(task_id).metadata["dependency_resolution"]
        assert resolution["status"] == "unsatisfied"


def test_the_integration_parent_settles_without_a_repair_sweep():
    cp = ControlPlane.in_memory()
    parent_id, ids = _chain(cp, depth=3)

    cp._transition_task_internal(
        ids[0], TaskState.FAILED.value, "test", {"reason": "executor_failed"}
    )

    parent = cp.get_task(parent_id)
    assert parent.state == TaskState.WAITING.value, "the parent must not be blocked"
    assert cp._dependencies_satisfied(parent), (
        "every child is terminal or supervised, so all_settled must be satisfied"
    )

    cp._unblock_ready_tasks()
    assert cp.get_task(parent_id).state == TaskState.OPEN.value


def test_a_long_chain_unwinds_completely():
    cp = ControlPlane.in_memory()
    parent_id, ids = _chain(cp, depth=6)

    cp._transition_task_internal(
        ids[0], TaskState.FAILED.value, "test", {"reason": "executor_failed"}
    )

    for task_id in ids[1:]:
        assert cp.get_task(task_id).state == TaskState.BLOCKED.value

    cp._unblock_ready_tasks()
    assert cp.get_task(parent_id).state == TaskState.OPEN.value


def test_propagation_terminates_on_a_dependency_cycle():
    """A corrupt edge must not spin the reconciler inside a transition."""
    cp = ControlPlane.in_memory()
    first = cp.create_task("first", project="mac")
    second = cp.create_task("second", dependencies=[first.id], project="mac")
    # Plant a cycle directly: the API refuses to create one.
    from mac.models import utcnow

    cp.store.execute(
        "INSERT INTO task_edges (task_id, dependency_task_id, edge_position, created_at) "
        "VALUES (?, ?, ?, ?)",
        (first.id, second.id, 0, utcnow()),
    )
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.WAITING.value, first.id),
    )

    # Must return rather than recurse forever.
    cp._resolve_waiting_dependents_of(first.id, TaskState.FAILED.value, "test")


def test_a_live_dependency_still_holds_its_dependents():
    """Propagation must not sweep up work whose prerequisite is still running."""
    cp = ControlPlane.in_memory()
    prerequisite = cp.create_task("still running", project="mac")
    dependent = cp.create_task(
        "waiting legitimately", dependencies=[prerequisite.id], project="mac"
    )

    cp._unblock_ready_tasks()

    assert cp.get_task(dependent.id).state == TaskState.WAITING.value
    assert "dependency_resolution" not in (cp.get_task(dependent.id).metadata or {})
