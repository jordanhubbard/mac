"""A repair prerequisite must carry the parent failure evidence it promises.

The generated description tells the repair worker to "use the parent failure
evidence and salvage metadata to restore the required agent, service,
credential source, or toolchain". Until the evidence was attached, the repair
task carried only ``origin.parent_task_id`` and an opaque
``failure_fingerprint`` -- so every repair worker was dispatched blind, and a
blind repair dead-letters against the recursion guard instead of repairing
anything.

Covers the failure class, the bounded parent output tail, the salvaged work,
and the parent's last environment preflight (where a missing declared command
shows up by name).
"""
from __future__ import annotations

import pytest

from mac.models import LeaseStatus, TaskState, utcnow
from mac.services import (
    REPAIR_FAILURE_EVIDENCE_SCHEMA,
    REPAIR_FAILURE_EVIDENCE_TAIL_CHARS,
    ControlPlane,
)


@pytest.fixture
def cp():
    return ControlPlane.in_memory()


def _register_agent(cp, name):
    machine = cp.register_machine("%s-host" % name, resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, name, capabilities=["python"])


def _drive_to_exhausted_environment_failure(cp, task, worker):
    """Mirror test_repair_task_recursion_guard's expire-lease failure path."""
    _, lease = cp.claim_task(task.id, worker.id)
    cp.transition_task(
        task.id,
        TaskState.BLOCKED.value,
        worker.id,
        {"reason": "heartbeat_offline"},
        lease_id=lease.id,
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


def _repair_origin(cp, parent_id, metadata=None):
    worker = _register_agent(cp, parent_id.replace("_", "-")[-8:])
    task = cp.create_task(
        "real work",
        required_capabilities=["python"],
        max_attempts=1,
        metadata=metadata or {"repair_policy": {"environment_prerequisite": True}},
    )
    refreshed = _drive_to_exhausted_environment_failure(cp, task, worker)
    repair_id = refreshed.metadata.get("environment_repair_task_id")
    assert repair_id, "the parent should have gained an environment repair prerequisite"
    repair = cp.get_task(repair_id)
    return task, repair, repair.metadata["origin"]


def test_repair_origin_carries_failure_evidence(cp):
    parent, _repair, origin = _repair_origin(cp, "task_a")

    evidence = origin["failure_evidence"]
    assert evidence["schema"] == REPAIR_FAILURE_EVIDENCE_SCHEMA
    assert evidence["failure_class"] == "environment"
    # The description promises evidence; something must actually be there.
    assert "output_tail" in evidence or "output_tail_unavailable_reason" in evidence
    assert origin["parent_task_id"] == parent.id
    assert origin["parent_task_title"] == parent.title


def test_repair_origin_names_its_parent_title(cp):
    """The repair title is derived from the parent's, but a worker that needs
    to reason about the parent should not have to parse a title prefix."""
    _parent, repair, origin = _repair_origin(cp, "task_b")
    assert repair.title == "Repair environment prerequisites: real work"
    assert origin["parent_task_title"] == "real work"


def test_repair_evidence_forwards_environment_preflight(cp):
    """The failing preflight check is the most actionable thing a repair
    worker can be handed: it names the missing command."""
    preflight = {
        "status": "fail",
        "checks": [
            {
                "name": "toolchain_commands",
                "status": "fail",
                "message": "declared toolchain.required_commands not on PATH: initdb",
                "missing": ["initdb"],
            }
        ],
    }
    _parent, _repair, origin = _repair_origin(
        cp,
        "task_c",
        metadata={
            "repair_policy": {"environment_prerequisite": True},
            "runtime": {"environment_contract": {"preflight": preflight}},
        },
    )

    assert origin["failure_evidence"]["environment_preflight"] == preflight


def test_repair_evidence_omits_preflight_when_parent_had_none(cp):
    _parent, _repair, origin = _repair_origin(cp, "task_d")
    assert "environment_preflight" not in origin["failure_evidence"]


def test_repair_evidence_output_tail_is_bounded(cp):
    """Task metadata is rendered into task.json on every dispatch; an
    unbounded executor log there would dwarf the task itself."""
    _parent, _repair, origin = _repair_origin(cp, "task_e")
    tail = origin["failure_evidence"].get("output_tail", "")
    assert len(tail) <= REPAIR_FAILURE_EVIDENCE_TAIL_CHARS


def test_repair_origin_still_satisfies_the_parent_identity_check(cp):
    """Adding evidence must not disturb the fields the hub validates."""
    parent, _repair, origin = _repair_origin(cp, "task_f")
    assert origin["type"] == "environment_prerequisite"
    assert origin["parent_task_id"] == parent.id
