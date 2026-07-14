"""Repair tasks must not spawn repair-of-repair chains (2026-07-14 churn).

Every exhausted contract/environment failure spawned a repair task; when the
underlying failure was deterministic infra (e.g. a stale attestation key), the
repair task failed the same way and spawned another repair — "Repair
prerequisites: Repair prerequisites: ..." — each level burning a full attempt
budget. Observed live: 175 of 267 new tasks in 12h were repair tasks while only
5 tasks completed. A repair task that exhausts its attempts must dead-letter
(FAILED, manual_repair_required) instead of recursing.
"""
from __future__ import annotations

import pytest

from mac.models import LeaseStatus, TaskState, utcnow
from mac.services import ControlPlane


@pytest.fixture
def cp():
    return ControlPlane.in_memory()


def _register_agent(cp, name):
    machine = cp.register_machine("%s-host" % name, resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, name, capabilities=["python"])


def _drive_to_exhausted_environment_failure(cp, task, worker):
    """Mirror test_control_plane's expire-lease environment-failure path."""
    _, lease = cp.claim_task(task.id, worker.id, lease_seconds=-1)
    cp.transition_task(
        task.id, TaskState.BLOCKED.value, "worker", {"reason": "heartbeat_offline"}
    )
    cp.store.execute("UPDATE tasks SET attempt_count = 1 WHERE id = ?", (task.id,))
    cp.store.execute(
        "UPDATE leases SET status = ?, expires_at = ? WHERE id = ?",
        (LeaseStatus.ACTIVE.value, "2000-01-01T00:00:00+00:00", lease.id),
    )
    cp.store.execute(
        "UPDATE tasks SET state = ?, lease_id = ?, owner_agent_id = ?, leased_until = ? WHERE id = ?",
        (TaskState.RUNNING.value, lease.id, worker.id, "2000-01-01T00:00:00+00:00", task.id),
    )
    cp.expire_leases(now=utcnow())
    return cp.get_task(task.id)


def test_ordinary_task_still_gets_a_repair_child(cp):
    """The repair mechanism itself is preserved for first-level failures."""
    worker = _register_agent(cp, "w1")
    task = cp.create_task("real work", required_capabilities=["python"], max_attempts=1)
    refreshed = _drive_to_exhausted_environment_failure(cp, task, worker)
    assert refreshed.state == TaskState.WAITING.value
    assert refreshed.metadata.get("environment_repair_task_id")


def test_repair_task_dead_letters_instead_of_recursing(cp):
    """A repair task that exhausts its budget FAILS; no repair-of-repair."""
    worker = _register_agent(cp, "w2")
    before = {t.id for t in cp.list_tasks(limit=500)}
    repair = cp.create_task(
        "Repair environment prerequisites: real work",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"origin": {"type": "environment_prerequisite", "parent_task_id": "task_p"}},
    )
    refreshed = _drive_to_exhausted_environment_failure(cp, repair, worker)

    assert refreshed.state == TaskState.FAILED.value, (
        "an exhausted repair task must dead-letter, got %s" % refreshed.state
    )
    assert not refreshed.metadata.get("environment_repair_task_id")
    # and no new task was minted by the exhaustion
    after = {t.id for t in cp.list_tasks(limit=500)}
    assert after - before == {repair.id}, "no repair-of-repair task may be created"
    # the dead-letter reason is explicit in history
    events = [e for e in cp.task_history(repair.id) if e.to_state == TaskState.FAILED.value]
    assert any(
        "repair_task_exhausted_no_recursion" in str(e.detail) for e in events
    ), "expected the recursion-guard reason in the FAILED transition detail"


def test_contract_prerequisite_repair_also_dead_letters(cp):
    worker = _register_agent(cp, "w3")
    repair = cp.create_task(
        "Repair contract prerequisites: other work",
        required_capabilities=["python"],
        max_attempts=1,
        metadata={"origin": {"type": "contract_prerequisite", "parent_task_id": "task_q"}},
    )
    refreshed = _drive_to_exhausted_environment_failure(cp, repair, worker)
    assert refreshed.state == TaskState.FAILED.value
