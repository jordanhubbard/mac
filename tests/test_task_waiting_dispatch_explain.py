from __future__ import annotations

import pytest

from mac.models import (
    AgentStatus,
    TaskState,
    ValidationError,
    json_dumps,
    new_id,
    utcnow,
)
from mac.services import ControlPlane


def test_dependency_tasks_use_waiting_not_actionable_blocked():
    cp = ControlPlane.in_memory()
    prerequisite = cp.create_task("prerequisite", metadata={"deliverable": "report"})
    dependent = cp.create_task(
        "dependent",
        dependencies=[prerequisite.id],
        metadata={"deliverable": "report"},
    )

    assert dependent.state == TaskState.WAITING.value
    assert cp.task_stats() == {"open": 1, "waiting": 1}
    created = cp.task_history(dependent.id)[0]
    assert created.to_state == TaskState.WAITING.value
    assert created.detail["dependencies"] == [prerequisite.id]


def test_waiting_sweep_opens_task_when_dependencies_complete():
    cp = ControlPlane.in_memory()
    prerequisite = cp.create_task("prerequisite", metadata={"deliverable": "report"})
    dependent = cp.create_task(
        "dependent",
        dependencies=[prerequisite.id],
        metadata={"deliverable": "report"},
    )
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.COMPLETED.value, prerequisite.id),
    )

    result = cp._unblock_ready_tasks()

    assert [task.id for task in result["tasks"]] == [dependent.id]
    assert cp.get_task(dependent.id).state == TaskState.OPEN.value


def test_legacy_dependency_block_is_migrated_to_waiting_once():
    cp = ControlPlane.in_memory()
    prerequisite = cp.create_task("legacy prerequisite", metadata={"deliverable": "report"})
    dependent = cp.create_task(
        "legacy dependent",
        dependencies=[prerequisite.id],
        metadata={"deliverable": "report"},
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
    cp._reconcile_legacy_task_state_semantics()

    assert cp.get_task(dependent.id).state == TaskState.WAITING.value
    migrations = [
        event
        for event in cp.task_history(dependent.id)
        if event.event_type == "task.state_semantics_migrated"
    ]
    assert len(migrations) == 1


def test_legacy_reasonless_block_receives_diagnosis_and_activity():
    cp = ControlPlane.in_memory()
    task = cp.create_task("legacy reasonless", metadata={"deliverable": "report"})
    now = utcnow()
    cp.store.execute(
        "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
        (TaskState.BLOCKED.value, now, task.id),
    )
    cp.store.execute(
        """
        INSERT INTO task_history (
            id, task_id, event_type, actor, from_state, to_state, detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("hist"),
            task.id,
            "task.legacy_fixture",
            "legacy-worker",
            TaskState.OPEN.value,
            TaskState.BLOCKED.value,
            json_dumps({}),
            now,
        ),
    )

    cp._reconcile_legacy_task_state_semantics()

    repaired = cp.get_task(task.id)
    assert repaired.state == TaskState.BLOCKED.value
    event = cp.task_history(task.id)[-1]
    assert event.event_type == "task.diagnosis_backfilled"
    assert event.detail["reason"] == "legacy_block_reason_unavailable"
    assert event.detail["diagnosis"]["actor"] == "legacy-worker"
    assert repaired.metadata["activity"][-1]["detail"] == event.detail["diagnosis"]


def test_block_transition_records_structured_diagnosis_and_redacted_tail():
    cp = ControlPlane.in_memory()
    task = cp.create_task("diagnose me", metadata={"deliverable": "report"})

    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "agent_test",
        {
            "reason": "executor_failed",
            "error": "command failed",
            "stdout": "first line\nAuthorization: Bearer super-secret-token\nlast line",
        },
    )

    event = cp.task_history(task.id)[-1]
    diagnosis = event.detail["diagnosis"]
    assert diagnosis["actor"] == "agent_test"
    assert diagnosis["attempt"] == 0
    assert diagnosis["failure"] == "executor_failed"
    assert diagnosis["problem"]
    assert diagnosis["remediation"]
    assert "super-secret-token" not in diagnosis["output_tail"]
    assert "<redacted>" in diagnosis["output_tail"]
    assert diagnosis["output_tail_unavailable_reason"] == ""
    activity = cp.get_task(task.id).metadata["activity"][-1]
    assert activity["phase"] == "diagnosis"
    assert activity["detail"] == diagnosis


def test_block_transition_records_explicit_missing_output_reason():
    cp = ControlPlane.in_memory()
    task = cp.create_task("diagnose no output", metadata={"deliverable": "report"})
    cp._transition_task_internal(
        task.id,
        TaskState.BLOCKED.value,
        "agent_test",
        {"reason": "operator_direction_required", "question": "Which target?"},
    )
    diagnosis = cp.task_history(task.id)[-1].detail["diagnosis"]
    assert diagnosis["output_tail"] == ""
    assert "supplied no" in diagnosis["output_tail_unavailable_reason"]


def test_dispatch_explanation_uses_exact_task_and_agent_gates():
    cp = ControlPlane.in_memory()
    task = cp.create_task(
        "python task",
        required_capabilities=["python"],
        metadata={"deliverable": "report", "no_dispatch": True},
    )
    machine = cp.register_machine("worker-host", resources={"cpu": 4, "memory_gb": 8})
    agent = cp.register_agent(machine.id, "worker", capabilities=[])
    cp.update_agent(agent.id, status=AgentStatus.IDLE.value)

    held = cp.explain_task_dispatch(task.id)
    assert held["dispatchable"] is False
    assert [item["code"] for item in held["task_reasons"]] == ["task_dispatch_held"]
    assert held["candidates"][0]["reasons"][0]["code"] == "capabilities_missing"

    cp.release_task(task.id)
    missing_capability = cp.explain_task_dispatch(task.id)
    assert missing_capability["task_ready"] is True
    assert missing_capability["dispatchable"] is False
    assert missing_capability["unclaimed_reasons"][0]["code"] == "no_eligible_agent"
    assert "capabilities_missing" in missing_capability["unclaimed_reasons"][0]["detail"]["rejected_by"]

    cp.update_agent(agent.id, capabilities=["python"])
    ready = cp.explain_task_dispatch(task.id)
    assert ready["dispatchable"] is True
    assert ready["eligible_agent_count"] == 1
    assert ready["unclaimed_reasons"][0]["code"] == "awaiting_dispatch"


def test_cooperative_all_settled_dependencies_share_dispatch_gate_predicate():
    cp = ControlPlane.in_memory()
    completed = cp.create_task("completed child", metadata={"deliverable": "report"})
    failed = cp.create_task("failed child", metadata={"deliverable": "report"})
    cancelled = cp.create_task("cancelled child", metadata={"deliverable": "report"})
    parent = cp.create_task(
        "integration parent",
        dependencies=[completed.id, failed.id, cancelled.id],
        metadata={
            "deliverable": "report",
            "coordination": {
                "mode": "cooperative_integration",
                "phase": "awaiting_children",
            },
        },
    )
    for child, state in (
        (completed, TaskState.COMPLETED.value),
        (failed, TaskState.FAILED.value),
        (cancelled, TaskState.CANCELLED.value),
    ):
        cp.store.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (state, child.id),
        )
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.OPEN.value, parent.id),
    )
    machine = cp.register_machine("all-settled-worker")
    agent = cp.register_agent(machine.id, "all-settled-agent")
    cp.update_agent(agent.id, status=AgentStatus.IDLE.value)

    explanation = cp.explain_task_dispatch(parent.id)

    assert explanation["task_ready"] is True
    assert explanation["dispatchable"] is True
    assert explanation["task_reasons"] == []
    assignment = cp.dispatch_once()
    assert assignment is not None
    assert assignment["task"]["id"] == parent.id


def test_dirty_managed_source_blocks_repo_change_dispatch_and_claim():
    cp = ControlPlane.in_memory()
    task = cp.create_task(
        "change repository source",
        metadata={
            "origin": {
                "type": "direct_task",
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "project": "example",
                },
            }
        },
    )
    machine = cp.register_machine("dirty-source-host")
    agent = cp.register_agent(
        machine.id,
        "dirty-source-worker",
        resources={
            "source_state": {
                "schema": "mac.worker_source_state.v1",
                "repo_path": "/srv/example",
                "repository_name": "example",
                "commit_sha": "a" * 40,
                "dirty": True,
            }
        },
    )
    cp.update_agent(agent.id, status=AgentStatus.IDLE.value)

    explanation = cp.explain_task_dispatch(task.id)

    assert explanation["dispatchable"] is False
    assert explanation["eligible_agent_count"] == 0
    assert explanation["candidates"][0]["reasons"] == [
        {
            "code": "repository_source_dirty",
            "message": "agent's managed repository source checkout is dirty",
        }
    ]
    assert explanation["unclaimed_reasons"][0]["code"] == "no_eligible_agent"
    assert explanation["unclaimed_reasons"][0]["detail"]["rejected_by"] == [
        "repository_source_dirty"
    ]
    with pytest.raises(ValidationError, match="repository_source_dirty"):
        cp.claim_task(task.id, agent.id)

    cp.update_agent(
        agent.id,
        resources={
            "source_state": {
                "schema": "mac.worker_source_state.v1",
                "repo_path": "/srv/example",
                "repository_name": "example",
                "commit_sha": "a" * 40,
                "dirty": False,
            }
        },
    )
    clean = cp.explain_task_dispatch(task.id)
    assert clean["dispatchable"] is True
    assert clean["eligible_agent_count"] == 1


def test_dirty_managed_source_does_not_block_a_different_repository():
    cp = ControlPlane.in_memory()
    task = cp.create_task(
        "change another repository",
        metadata={
            "origin": {
                "type": "direct_task",
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "project": "example",
                },
            }
        },
    )
    machine = cp.register_machine("dirty-other-source-host")
    agent = cp.register_agent(
        machine.id,
        "dirty-other-source-worker",
        resources={
            "source_state": {
                "schema": "mac.worker_source_state.v1",
                "repo_path": "/srv/mac",
                "repository_name": "mac",
                "commit_sha": "b" * 40,
                "dirty": True,
            }
        },
    )
    cp.update_agent(agent.id, status=AgentStatus.IDLE.value)

    explanation = cp.explain_task_dispatch(task.id)

    assert explanation["dispatchable"] is True
    assert explanation["eligible_agent_count"] == 1
    assert explanation["candidates"][0]["eligible"] is True


def test_dirty_managed_source_does_not_block_report_task():
    cp = ControlPlane.in_memory()
    task = cp.create_task(
        "inspect repository source",
        metadata={"deliverable": "report"},
    )
    machine = cp.register_machine("dirty-report-host")
    agent = cp.register_agent(
        machine.id,
        "dirty-report-worker",
        resources={
            "source_state": {
                "schema": "mac.worker_source_state.v1",
                "commit_sha": "b" * 40,
                "dirty": True,
            }
        },
    )
    cp.update_agent(agent.id, status=AgentStatus.IDLE.value)

    explanation = cp.explain_task_dispatch(task.id)

    assert explanation["dispatchable"] is True
    assert explanation["eligible_agent_count"] == 1
    assert explanation["candidates"][0]["eligible"] is True
    assert explanation["candidates"][0]["reasons"] == []
