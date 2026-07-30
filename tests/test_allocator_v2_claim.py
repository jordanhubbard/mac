import pytest

from mac.models import TaskState, ValidationError
from mac.services import ControlPlane


def _worker(cp: ControlPlane, name: str = "worker"):
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(
        machine.id,
        name,
        capabilities=["python"],
    )


def test_allocator_v2_claim_uses_minimal_locked_authority():
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    agent = _worker(cp)
    task = cp.create_task(
        "do useful work",
        project="mac",
        required_capabilities=["python"],
        metadata={
            # Runtime matching is repair telemetry, not a reason to keep an
            # otherwise capable worker idle.
            "required_runtime_digest": "sha256:stale-runtime-preference",
        },
    )

    assignment = cp.claim_task_v2(task.id, agent.id, lease_seconds=120)

    assert assignment["task"]["id"] == task.id
    assert assignment["agent"]["id"] == agent.id
    assert assignment["lease"]["agent_id"] == agent.id
    assert cp.get_task(task.id).state == TaskState.CLAIMED.value


def test_allocator_v2_claim_fails_closed_for_unregistered_named_project():
    cp = ControlPlane.in_memory()
    agent = _worker(cp)
    task = cp.create_task(
        "unknown project work",
        project="not-registered",
        required_capabilities=["python"],
    )

    with pytest.raises(ValidationError, match="task_project_unregistered"):
        cp.claim_task_v2(task.id, agent.id)

    assert cp.get_task(task.id).state == TaskState.OPEN.value


def test_allocator_v2_claim_ignores_malformed_advertised_capacity():
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    machine = cp.register_machine("malformed-capacity-host")
    agent = cp.register_agent(
        machine.id,
        "malformed-capacity",
        capabilities=["python"],
        resources={"capacity": "not-a-number"},
    )
    task = cp.create_task(
        "do useful work",
        project="mac",
        required_capabilities=["python"],
    )

    assignment = cp.claim_task_v2(task.id, agent.id)

    assert assignment["task"]["id"] == task.id
    assert assignment["agent"]["id"] == agent.id


def test_allocator_v2_claim_enforces_one_synchronous_slot():
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    machine = cp.register_machine("overadvertised-capacity-host")
    agent = cp.register_agent(
        machine.id,
        "overadvertised-capacity",
        capabilities=["python"],
        resources={"capacity": 8},
    )
    first = cp.create_task(
        "first",
        project="mac",
        required_capabilities=["python"],
    )
    second = cp.create_task(
        "second",
        project="mac",
        required_capabilities=["python"],
    )

    cp.claim_task_v2(first.id, agent.id)

    with pytest.raises(ValidationError, match="agent_capacity_full"):
        cp.claim_task_v2(second.id, agent.id)
