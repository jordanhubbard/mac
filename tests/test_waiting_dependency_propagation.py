"""Terminal dependencies must be reconciled without manufacturing failures."""
from __future__ import annotations

import pytest

from mac.models import TaskState
from mac.services import ControlPlane


@pytest.fixture
def cp():
    return ControlPlane.in_memory()


def _waiting_on(cp, dep_id, title="dependent", *, cancel_scope=False):
    metadata = (
        {"dependency_policy": {"on_unsatisfied": "cancel_scope"}}
        if cancel_scope
        else {}
    )
    t = cp.create_task(title, metadata=metadata)
    cp.update_task(t.id, dependencies=[dep_id], actor="test")
    cp._transition_task_internal(
        t.id,
        TaskState.WAITING.value,
        "test-fixture",
        {"reason": "blocked_on_dep"},
    )
    return cp.get_task(t.id)


def test_failed_dependency_supervises_waiting_dependent_with_provenance(cp):
    dep = cp.create_task("prerequisite")
    a = _waiting_on(cp, dep.id, "A")
    b = _waiting_on(cp, dep.id, "B")
    assert a.state == TaskState.WAITING.value and b.state == TaskState.WAITING.value

    cp._transition_task_internal(
        dep.id,
        TaskState.FAILED.value,
        "test-fixture",
        {"reason": "executor_failed"},
    )

    a2, b2 = cp.get_task(a.id), cp.get_task(b.id)
    assert a2.state == TaskState.BLOCKED.value
    assert b2.state == TaskState.BLOCKED.value
    ev = [e for e in cp.task_history(a.id) if e.to_state == TaskState.BLOCKED.value]
    assert any("dependencies_incomplete" in str(e.detail) for e in ev)
    assert any(str(e.detail).find(dep.id) >= 0 for e in ev)
    assert a2.metadata["dependency_resolution"]["unsatisfied"][dep.id]["state"] == (
        TaskState.FAILED.value
    )


def test_cancelled_dependency_is_supervised_without_derived_cancellation(cp):
    dep = cp.create_task("dup-prereq")
    a = _waiting_on(cp, dep.id, "A")
    cp.close_task(
        dep.id,
        TaskState.CANCELLED.value,
        "operator",
        {"reason": "superseded"},
    )
    assert cp.get_task(a.id).state == TaskState.BLOCKED.value


def test_supervision_stops_transitive_cancellation(cp):
    dep = cp.create_task("root")
    mid = _waiting_on(cp, dep.id, "mid")
    leaf = _waiting_on(cp, mid.id, "leaf")   # leaf waits on mid, mid waits on dep
    cp._transition_task_internal(
        dep.id, TaskState.FAILED.value, "test-fixture", {"reason": "x"}
    )
    assert cp.get_task(mid.id).state == TaskState.BLOCKED.value
    assert cp.get_task(leaf.id).state == TaskState.WAITING.value


def test_explicit_cancel_scope_preserves_all_for_one_semantics(cp):
    dep = cp.create_task("root")
    mid = _waiting_on(cp, dep.id, "mid", cancel_scope=True)
    leaf = _waiting_on(cp, mid.id, "leaf", cancel_scope=True)
    cp._transition_task_internal(
        dep.id, TaskState.FAILED.value, "test-fixture", {"reason": "x"}
    )
    assert cp.get_task(mid.id).state == TaskState.CANCELLED.value
    assert cp.get_task(leaf.id).state == TaskState.CANCELLED.value


def test_superseded_dependency_rewires_waiting_dependent(cp):
    dep = cp.create_task("superseded prerequisite")
    replacement = cp.create_task("replacement prerequisite")
    dependent = _waiting_on(cp, dep.id)

    cp.close_task(
        dep.id,
        TaskState.CANCELLED.value,
        "operator",
        {
            "reason": "replacement is authoritative",
            "disposition": "superseded",
            "replacement_task_id": replacement.id,
        },
    )

    rewired = cp.get_task(dependent.id)
    assert rewired.state == TaskState.WAITING.value
    assert rewired.dependencies == [replacement.id]
    assert cp.get_task(replacement.id).state == TaskState.OPEN.value


def test_completed_dependency_does_not_fail_dependent(cp):
    dep = cp.create_task("good-prereq")
    a = _waiting_on(cp, dep.id, "A")
    # drive dep to completed via force-complete; dependent must be UNBLOCKED, not failed
    cp.force_complete_task(dep.id, "operator", "done")
    a2 = cp.get_task(a.id)
    assert a2.state != TaskState.FAILED.value


def test_only_waiting_dependents_are_touched(cp):
    dep = cp.create_task("prereq")
    other = cp.create_task("unrelated")   # not a dependent
    a = _waiting_on(cp, dep.id, "A")
    cp._transition_task_internal(
        dep.id, TaskState.FAILED.value, "test-fixture", {"reason": "x"}
    )
    assert cp.get_task(other.id).state != TaskState.FAILED.value
    assert cp.get_task(a.id).state == TaskState.BLOCKED.value
