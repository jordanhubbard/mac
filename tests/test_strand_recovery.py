"""Recovering tasks the unsupervision bug left stranded.

The bug (fixed separately) reverted supervised BLOCKED tasks to WAITING on
every hub restart. ``_dependency_state_satisfies_join`` needs BLOCKED *and*
the ``dependency_resolution`` marker, so a reverted task counted for nothing:
it waited forever on a dependency that will never complete, and its
integration parent waited forever on it.

Fixing the migration stops new stranding. It does not re-supervise the 240
tasks already reverted on the live hub, which is what this sweep is for.
"""
from __future__ import annotations

from mac.models import TaskState
from mac.services import ControlPlane


def _decomposed(cp):
    """Parent -> [implement, test(depends_on implement)], as the planner emits."""
    parent = cp.create_task("integration parent", project="mac")
    result = cp.add_child_tasks(
        parent.id,
        [
            {"title": "implement", "node_id": "implement"},
            {"title": "test", "depends_on": ["implement"]},
        ],
    )
    implement, test = [child["id"] for child in result["children"]]
    return parent.id, implement, test


def _strand(cp, task_id):
    """Reproduce the bug's end state: WAITING, marker intact, state reverted."""
    task = cp.get_task(task_id)
    metadata = dict(task.metadata or {})
    metadata["dependency_resolution"] = {
        "schema": "mac.dependency_resolution.v1",
        "status": "unsatisfied",
        "policy": "supervise",
    }
    cp.update_task(task_id, metadata=metadata, actor="test")
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.WAITING.value, task_id),
    )


def test_dry_run_reports_without_changing_anything():
    cp = ControlPlane.in_memory()
    _parent, implement, test = _decomposed(cp)
    cp._transition_task_internal(
        implement, TaskState.FAILED.value, "test", {"reason": "executor_failed"}
    )
    _strand(cp, test)

    report = cp.recover_stranded_dependents(dry_run=True)

    assert report["dry_run"] is True
    assert report["terminal_dependencies"] >= 1
    assert report["dependents_examined"] >= 1
    assert report["supervised"] == 0
    assert cp.get_task(test).state == TaskState.WAITING.value


def test_recovery_re_supervises_a_stranded_dependent():
    cp = ControlPlane.in_memory()
    _parent, implement, test = _decomposed(cp)
    cp._transition_task_internal(
        implement, TaskState.FAILED.value, "test", {"reason": "executor_failed"}
    )
    _strand(cp, test)

    report = cp.recover_stranded_dependents(dry_run=False)

    assert report["supervised"] >= 1
    recovered = cp.get_task(test)
    assert recovered.state == TaskState.BLOCKED.value
    assert recovered.metadata["dependency_resolution"]["status"] == "unsatisfied"


def test_an_integration_parent_stays_waiting_and_then_settles():
    """The property a naive sweep gets wrong.

    A cooperative-integration parent must NOT be blocked alongside its
    children -- it stays WAITING so its all_settled join can settle on them.
    Blocking it instead would leave it just as stuck, in a different state.
    """
    cp = ControlPlane.in_memory()
    parent_id, implement, test = _decomposed(cp)
    cp._transition_task_internal(
        implement, TaskState.FAILED.value, "test", {"reason": "executor_failed"}
    )
    _strand(cp, test)

    cp.recover_stranded_dependents(dry_run=False)

    parent = cp.get_task(parent_id)
    assert parent.state == TaskState.WAITING.value, "the parent must not be blocked"
    assert cp._dependency_join_policy(parent) == "all_settled"
    assert cp._dependencies_satisfied(parent)

    cp._unblock_ready_tasks()
    assert cp.get_task(parent_id).state == TaskState.OPEN.value


def test_recovery_is_idempotent():
    cp = ControlPlane.in_memory()
    _parent, implement, test = _decomposed(cp)
    cp._transition_task_internal(
        implement, TaskState.FAILED.value, "test", {"reason": "executor_failed"}
    )
    _strand(cp, test)

    first = cp.recover_stranded_dependents(dry_run=False)
    second = cp.recover_stranded_dependents(dry_run=False)

    assert first["supervised"] >= 1
    # Nothing is WAITING on a terminal dependency any more, so the second pass
    # selects no work at all.
    assert second["supervised"] == 0
    assert cp.get_task(test).state == TaskState.BLOCKED.value


def test_a_task_waiting_on_a_live_dependency_is_untouched():
    """The sweep must only touch work whose dependency is actually terminal."""
    cp = ControlPlane.in_memory()
    prerequisite = cp.create_task("still running", project="mac")
    dependent = cp.create_task(
        "waiting legitimately", dependencies=[prerequisite.id], project="mac"
    )
    assert cp.get_task(dependent.id).state == TaskState.WAITING.value

    report = cp.recover_stranded_dependents(dry_run=False)

    assert report["supervised"] == 0
    assert cp.get_task(dependent.id).state == TaskState.WAITING.value


def test_a_chain_unwinds_and_frees_the_parent():
    """implement(failed) <- test <- verify: the shape that made this necessary.

    Only `test` has a TERMINAL dependency. `verify` waits on `test`, which is
    supervised and will never run, but the live reconciler propagates only on
    terminal transitions -- so nothing marks `verify` and the all_settled
    parent waits on it forever. Measured on the live ledger, without chain
    propagation 168 tasks are re-supervised and ZERO parents become
    dispatchable.
    """
    cp = ControlPlane.in_memory()
    parent = cp.create_task("integration parent", project="mac")
    result = cp.add_child_tasks(
        parent.id,
        [
            {"title": "implement", "node_id": "implement"},
            {"title": "test", "node_id": "test", "depends_on": ["implement"]},
            {"title": "verify", "depends_on": ["test"]},
        ],
    )
    implement, test, verify = [child["id"] for child in result["children"]]

    cp._transition_task_internal(
        implement, TaskState.FAILED.value, "test", {"reason": "executor_failed"}
    )
    # First hop supervises automatically; the second is left stranded.
    assert cp.get_task(test).state == TaskState.BLOCKED.value
    assert cp.get_task(verify).state == TaskState.WAITING.value

    report = cp.recover_stranded_dependents(dry_run=False)

    assert report["rounds"] >= 1
    assert cp.get_task(verify).state == TaskState.BLOCKED.value, (
        "the second hop must be supervised, or the parent never settles"
    )

    parent_task = cp.get_task(parent.id)
    assert parent_task.state == TaskState.WAITING.value
    assert cp._dependencies_satisfied(parent_task)

    cp._unblock_ready_tasks()
    assert cp.get_task(parent.id).state == TaskState.OPEN.value


def test_the_sweep_is_reachable_over_the_hub():
    """The stranded tasks live on the hub, so hub mode is the only mode that
    matters. A CLI command that only works with --db would be unusable where
    it is needed."""
    from mac.dispatch import RemoteDispatch

    assert hasattr(RemoteDispatch, "recover_stranded_dependents"), (
        "recover_stranded_dependents must be wrapped in RemoteDispatch or "
        "`mac task recover-stranded` fails in hub mode"
    )
