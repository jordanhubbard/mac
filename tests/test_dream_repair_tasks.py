from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mac.dream_repair_tasks import (
    DREAM_REPAIR_ORIGIN_TYPE,
    file_low_confidence_repair_tasks,
)
from mac.services import ControlPlane


@dataclass
class FakeTask:
    id: str
    project: str | None = None
    state: str = "open"
    metadata: dict[str, Any] = field(default_factory=dict)


class FakeControlPlane:
    def __init__(
        self,
        tasks: list[FakeTask] | None = None,
        *,
        fail_create: bool = False,
        fail_list: bool = False,
    ):
        self.tasks = list(tasks or [])
        self.fail_create = fail_create
        self.fail_list = fail_list
        self.created: list[dict[str, Any]] = []

    def list_tasks(self):
        if self.fail_list:
            raise RuntimeError("hub list_tasks unavailable")
        return list(self.tasks)

    def create_task(self, title, *, description="", project=None, metadata=None, actor="human", **_):
        if self.fail_create:
            raise RuntimeError("hub create_task unavailable")
        task = FakeTask(
            id="task_%d" % (len(self.tasks) + 1),
            project=project,
            metadata=metadata or {},
        )
        self.tasks.append(task)
        self.created.append(
            {
                "title": title,
                "description": description,
                "project": project,
                "metadata": metadata or {},
                "actor": actor,
            }
        )
        return task


def _candidate(**overrides: Any) -> dict[str, Any]:
    base = {
        "schema": "mac.dream.v1",
        "kind": "failure_pattern",
        "scope": "project",
        "confidence": "low",
        "confidence_score": 0.35,
        "summary": (
            "skill_bundle failed when terminal_tool ran on "
            "/Users/jkh/.mac with token=sk-secret-token-one for agent_jordanh-worker1"
        ),
        "project": "mac",
        "task_id": "task-source",
        "nap_run_id": "nap-cycle",
        "evidence": [
            {
                "memory_id": "mem-1",
                "record_type": "note",
                "task_id": "task-source",
                "excerpt": (
                    "terminal_tool traceback in /home/horde/.mac; "
                    "Authorization: Bearer secret-token-value"
                ),
            }
        ],
        "dimensions": {
            "skills": [{"name": "codex"}],
            "tools": [{"name": "terminal_tool"}],
        },
    }
    base.update(overrides)
    return base


def test_files_low_confidence_task_with_evidence_and_labels() -> None:
    cp = FakeControlPlane()

    report = file_low_confidence_repair_tasks(cp, [_candidate()], actor="nap-cycle")

    assert report["status"] == "ok"
    assert report["created_count"] == 1
    created = cp.created[0]
    assert created["project"] == "mac"
    assert created["actor"] == "nap-cycle"
    assert created["metadata"]["origin"]["type"] == DREAM_REPAIR_ORIGIN_TYPE
    assert created["metadata"]["dream_repair"]["affected"]["skills"][-1] == "codex"
    assert "terminal_tool" in created["metadata"]["dream_repair"]["affected"]["tools"]
    assert "mem-1" in created["description"]
    assert "codex" in created["description"]
    assert "terminal_tool" in created["description"]
    assert "sk-secret" not in created["description"]
    assert "secret-token-value" not in created["description"]
    assert "jkh" not in created["description"]
    assert "horde" not in created["description"]
    assert "agent_jordanh" not in created["description"]


def test_dedupes_repeated_cycle_reports_by_fingerprint() -> None:
    cp = FakeControlPlane()

    first = file_low_confidence_repair_tasks(cp, [_candidate()])
    second = file_low_confidence_repair_tasks(cp, [_candidate(nap_run_id="nap-next")])

    assert first["created_count"] == 1
    assert second["created_count"] == 0
    assert second["deduped_count"] == 1
    assert len(cp.created) == 1
    assert second["tasks"][0]["task_id"] == cp.tasks[0].id


def test_api_failure_is_reported_without_raising() -> None:
    cp = FakeControlPlane(fail_create=True)

    report = file_low_confidence_repair_tasks(cp, [_candidate()])

    assert report["status"] == "error"
    assert report["created_count"] == 0
    assert report["errors"][0]["phase"] == "create_task"
    assert "unavailable" in report["errors"][0]["error"]


def test_existing_task_list_failure_stops_before_create() -> None:
    cp = FakeControlPlane(fail_list=True)

    report = file_low_confidence_repair_tasks(cp, [_candidate()])

    assert report["status"] == "error"
    assert report["created_count"] == 0
    assert report["errors"][0]["phase"] == "list_existing_tasks"
    assert cp.created == []


def test_skips_non_low_confidence_candidate() -> None:
    candidate = _candidate(
        confidence="high",
        confidence_score=0.9,
        evidence=[
            {"memory_id": "mem-1", "record_type": "note"},
            {"memory_id": "mem-2", "record_type": "note"},
            {"memory_id": "mem-3", "record_type": "note"},
        ],
    )

    report = file_low_confidence_repair_tasks(FakeControlPlane(), [candidate])

    assert report["created_count"] == 0
    assert report["skipped_count"] == 1
    assert report["tasks"][0]["reason"] == "confidence_not_low"


def test_skips_low_confidence_candidate_without_affected_area() -> None:
    candidate = _candidate(
        summary="generic observation with no repair target",
        dimensions={},
    )

    report = file_low_confidence_repair_tasks(FakeControlPlane(), [candidate])

    assert report["created_count"] == 0
    assert report["skipped_count"] == 1
    assert report["tasks"][0]["reason"] == "no_affected_area"


def test_signature_only_candidate_without_evidence_still_files_generic_task() -> None:
    cp = FakeControlPlane()
    candidate = _candidate(
        summary="skill failed during validation",
        signature="skill:codex",
        evidence=None,
        dimensions={},
    )

    report = file_low_confidence_repair_tasks(cp, [candidate])

    assert report["created_count"] == 1
    assert "codex" in cp.created[0]["description"]
    assert "No structured evidence" in cp.created[0]["description"]


def test_nap_cycle_files_low_confidence_repair_task() -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("h1")
    agent = cp.register_agent(machine.id, "agent-cycle-repair", capabilities=[])
    cp.add_memory(
        task_id=None,
        subject_type="topic",
        subject_id="terminal",
        record_type="note",
        content="terminal_tool failed during skill_bundle validation",
        evidence_id=None,
        created_by=agent.id,
    )

    out = cp.run_nap_cycle(agent.id, embed_into_medium=False)

    assert out["repair_task_error"] is None
    assert out["repair_tasks"]["created_count"] == 1
    repair_tasks = [
        task
        for task in cp.list_tasks()
        if task.metadata.get("origin", {}).get("type") == DREAM_REPAIR_ORIGIN_TYPE
    ]
    assert len(repair_tasks) == 1
    assert repair_tasks[0].metadata["dream_repair"]["affected"]["tools"]
