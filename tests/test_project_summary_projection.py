"""Project polling preserves counts/joins without loading full task bodies."""

import pytest

from mac.services import ControlPlane
from mac.models import json_dumps


@pytest.mark.parametrize(
    "dependency_state,dependency_metadata",
    [
        ("cancelled", {"repository_ref_lifecycle": {"disposition": "duplicate"}}),
        ("cancelled", {}),
        ("completed", {}),
        ("failed", {}),
        ("blocked", {"dependency_resolution": {"status": "unsatisfied"}}),
    ],
)
def test_project_poll_preserves_summary_without_full_task_reads(
    monkeypatch, dependency_state, dependency_metadata
):
    cp = ControlPlane.in_memory()
    cp.create_project("empty", metadata={"dispatch_paused": True})
    machine = cp.register_machine("summary-worker")
    agent = cp.register_agent(machine.id, "summary-worker")
    resolved = cp.create_task("resolved", project="upstream")
    held = cp.create_task(
        "held",
        project="downstream",
        priority=20,
        metadata={"no_dispatch": True, "executor_evidence": "x" * 100_000},
    )
    cp.create_task(
        "joined",
        project="downstream",
        dependencies=[resolved.id],
        metadata={
            "dependency_policy": {"join": "all_success"},
            "origin": {"hermes_instance_id": "test"},
            "memory_boundary": {"scope": "project"},
        },
    )
    cp.create_task("waiting", project="other", dependencies=[held.id])
    cp.create_task(
        "settled",
        project="other",
        dependencies=[resolved.id],
        metadata={
            "coordination": {"mode": "cooperative_integration"},
        },
    )
    cp.create_task(
        "retry",
        project="other",
        metadata={
            "semantic_retry": {
                "status": "scheduled",
                "not_before": "2999-01-01T00:00:00+00:00",
            }
        },
    )
    cp.create_task("unassigned", description="large body" * 10000)
    cp.store.execute(
        "UPDATE tasks SET state = ?, metadata = ?, owner_agent_id = ? WHERE id = ?",
        (
            dependency_state,
            json_dumps(dependency_metadata),
            agent.id,
            resolved.id,
        ),
    )
    expected = cp._hermes_project_contexts(
        cp.list_tasks(),
        cp.list_agents(),
        [x.to_dict() for x in cp.list_project_items()],
        [x.to_dict() for x in cp.list_project_repositories()],
        [x.to_dict() for x in cp.list_project_records()],
    )
    projected = cp._project_summary_tasks()
    assert all(task.description == "" for task in projected)
    assert all("executor_evidence" not in task.metadata for task in projected)
    monkeypatch.setattr(
        cp,
        "list_tasks",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("project polling must not load the full task ledger")
        ),
    )
    assert cp.list_projects() == expected
