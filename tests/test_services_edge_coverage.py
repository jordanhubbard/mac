"""Alternate-state coverage for the control-plane service facade."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from mac.models import AgentStatus, HealthStatus, ValidationError
from mac.services import ControlPlane, _agent_resource_command_names


def _registered(cp: ControlPlane):
    machine = cp.register_machine(
        "host", resources={"cpu": 8, "tags": ["linux", "gpu"]}, hardware={"gpu": {"count": 1}}
    )
    agent = cp.register_agent(
        machine.id,
        "agent",
        capabilities=["python", "git"],
        resources={"capacity": 2, "memory": 16},
    )
    return machine, agent


def test_agent_resource_command_inventory_accepts_every_shape() -> None:
    resources = {
        "commands": {
            "available": ["git", " python "],
            "commands": ["gh", {"name": "codegraph"}, {}, 3],
            "paths": {"uv": "/bin/uv", "": "/bad"},
        },
        "command_inventory": ["node", {"name": "npm"}, {}, 1],
    }
    assert _agent_resource_command_names(resources) == {
        "git",
        "python",
        "gh",
        "codegraph",
        "uv",
        "node",
        "npm",
    }


def test_update_task_populates_all_optional_columns_and_validates() -> None:
    cp = ControlPlane.in_memory()
    project = cp.create_project("project-a")
    dependency = cp.create_task("dependency")
    task = cp.create_task("original")
    updated = cp.update_task(
        task.id,
        title="renamed",
        description="description",
        project=project.name,
        priority=7,
        required_capabilities=["python"],
        dependencies=[dependency.id],
        metadata={"owner": "team"},
        max_attempts=5,
        actor="test",
    )
    assert updated.title == "renamed"
    assert updated.description == "description"
    assert updated.priority == 7
    assert updated.dependencies == [dependency.id]
    assert updated.max_attempts == 5
    assert cp.update_task(updated.id).id == updated.id
    with pytest.raises(ValidationError, match="title is required"):
        cp.update_task(task.id, title=" ")
    with pytest.raises(ValidationError, match="depend on itself"):
        cp.update_task(task.id, dependencies=[task.id])
    with pytest.raises(ValidationError, match=">= 1"):
        cp.update_task(task.id, max_attempts=0)
    reopened = cp.update_task(task.id, dependencies=[])
    assert reopened.dependencies == []


def test_fleet_create_update_and_validation_covers_membership_deltas() -> None:
    cp = ControlPlane.in_memory()
    tenant = cp.register_tenant("tenant")
    _machine, first = _registered(cp)
    second_machine = cp.register_machine("host-2")
    second = cp.register_agent(second_machine.id, "agent-2")
    with pytest.raises(ValidationError, match="name is required"):
        cp.create_fleet("")
    with pytest.raises(ValidationError, match="unsupported fleet status"):
        cp.create_fleet("bad", status="unknown")

    fleet = cp.create_fleet(
        "fleet-a",
        "initial",
        tenant_id=tenant.id,
        agent_ids=[first.id],
        metadata={"region": "west"},
        actor="operator",
    )
    with pytest.raises(ValidationError, match="already exists"):
        cp.create_fleet("fleet-a")
    updated = cp.update_fleet(
        fleet.id,
        name="fleet-b",
        description="updated",
        status="inactive",
        metadata={"region": "east"},
        tenant_id="",
        agent_ids=[first.id, second.id],
        actor="operator",
    )
    assert updated.name == "fleet-b"
    assert updated.status == "inactive"
    assert updated.tenant_id is None
    assert sorted(updated.agent_ids) == sorted([first.id, second.id])
    assert cp.update_fleet(updated.id).id == updated.id
    with pytest.raises(ValidationError, match="name is required"):
        cp.update_fleet(updated.id, name="")
    with pytest.raises(ValidationError, match="unsupported fleet status"):
        cp.update_fleet(updated.id, status="bad")


def test_agent_update_all_fields_and_invalid_enums() -> None:
    cp = ControlPlane.in_memory()
    tenant = cp.register_tenant("tenant")
    hermes = cp.register_hermes_instance(tenant.id, "hermes")
    _machine, agent = _registered(cp)
    updated = cp.update_agent(
        agent.id,
        name="renamed",
        capabilities=["review"],
        resources={"capacity": 3},
        status=AgentStatus.OFFLINE.value,
        health_status=HealthStatus.DEGRADED.value,
        hermes_instance_id=hermes.id,
        actor="operator",
    )
    assert updated.name == "renamed"
    assert updated.capabilities == ["review"]
    assert updated.status == AgentStatus.OFFLINE.value
    assert updated.hermes_instance_id == hermes.id
    cleared = cp.update_agent(updated.id, hermes_instance_id="")
    assert cleared.hermes_instance_id is None
    assert cp.update_agent(updated.id).id == updated.id
    with pytest.raises(ValidationError, match="name is required"):
        cp.update_agent(updated.id, name="")
    with pytest.raises(ValidationError, match="unsupported agent status"):
        cp.update_agent(updated.id, status="bad")
    with pytest.raises(ValidationError, match="unsupported agent health_status"):
        cp.update_agent(updated.id, health_status="bad")


def test_integration_finding_filters_notifications_and_idempotent_resolution() -> None:
    cp = ControlPlane.in_memory()
    finding = cp.record_integration_finding(
        "github",
        "repo",
        "drift",
        "Branch drift",
        {"branch": "main"},
        severity="error",
        notify=True,
        channels=[],
        notification_body="Fix the branch",
    )
    assert cp.list_integration_findings(
        source_kind="github",
        source_id="repo",
        finding_type="drift",
        status="open",
        severity="error",
        limit=0,
    )[0].id == finding.id
    resolved = cp.resolve_integration_finding(finding.id, resolution="fixed")
    assert resolved.status == "resolved"
    assert cp.resolve_integration_finding(finding.id).id == finding.id


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ([{"status": "passed"}], True),
        ("invalid", False),
        ({"returncode": 0}, True),
        ({"returncode": "bad"}, False),
        ({"failed": 1, "status": "ok"}, False),
        ({"status": "success"}, True),
        ({"result": "ok"}, True),
        ({"outcome": "succeeded"}, True),
        ({"passed": True}, True),
        ({"success": 2, "failed": 0}, True),
        ({"ok": True, "satisfied": True}, True),
        ({"nested": {"status": "pass"}}, True),
    ],
)
def test_service_verification_item_shapes(item, expected: bool) -> None:
    cp = ControlPlane.in_memory()
    assert cp._verification_item_passed(item) is expected


def test_agent_resources_satisfy_numeric_list_exact_and_hardware(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    machine, agent = _registered(cp)
    base_task = cp.create_task("resource task", metadata={})
    assert cp._agent_resources_satisfy(agent, machine, base_task) is True
    assert cp._agent_resources_satisfy(
        agent, machine, replace(base_task, metadata={"resources": {"cpu": 99}})
    ) is False
    assert cp._agent_resources_satisfy(
        agent, machine, replace(base_task, metadata={"resources": {"tags": ["gpu"]}})
    ) is True
    assert cp._agent_resources_satisfy(
        agent, machine, replace(base_task, metadata={"resources": {"tags": ["missing"]}})
    ) is False
    assert cp._agent_resources_satisfy(
        agent, machine, replace(base_task, metadata={"resources": {"os": "linux"}})
    ) is False
    monkeypatch.setattr(
        "mac.roles_service.machine_hardware_satisfies", lambda *_a, **_k: (False, ["gpu"])
    )
    assert cp._agent_resources_satisfy(
        agent, machine, replace(base_task, metadata={"hardware": {"gpu": True}})
    ) is False


def test_agent_available_fast_failure_gates(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    machine, agent = _registered(cp)
    task = cp.create_task("work", required_capabilities=["python"])
    assert cp._agent_available_for(agent, task) is True
    assert cp._agent_available_for(replace(agent, status="offline"), task) is False
    assert cp._agent_available_for(replace(agent, health_status="degraded"), task) is False
    assert cp._agent_available_for(
        agent, replace(task, metadata={"target_agent_id": "other"})
    ) is False
    assert cp._agent_available_for(
        agent, replace(task, metadata={"target_agent_name": "other"})
    ) is False
    assert cp._agent_available_for(
        replace(agent, running_digest="old"),
        replace(task, metadata={"required_runtime_digest": "new"}),
    ) is False
    monkeypatch.setattr(cp, "_agent_active_lease_count", lambda _id: 99)
    assert cp._agent_available_for(agent, task) is False


def test_project_update_delete_validation_and_force() -> None:
    cp = ControlPlane.in_memory()
    project = cp.create_project("one")
    task = cp.create_task("work", project="one")
    updated = cp.update_project(
        project.id,
        name="two",
        description="description",
        metadata={"owner": "team"},
        status="inactive",
        actor="operator",
    )
    assert updated.name == "two"
    with pytest.raises(ValidationError):
        cp.delete_project(updated.id)
    cp.delete_project(updated.id, force=True)
    assert cp.get_task(task.id).project is None
