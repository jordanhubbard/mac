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


# ---------------------------------------------------------------------------
# Additional edge-case tests for uncovered branches
# ---------------------------------------------------------------------------


def test_non_mapping_metadata_task_is_skipped_in_fingerprints():
    """Tasks whose metadata is not a Mapping are skipped (line 160)."""
    from mac.dream_repair_tasks import _existing_repair_fingerprints  # noqa: PLC0415

    class FakeTaskNonMapping:
        id = "task-x"
        metadata = "not-a-mapping"

    class CPWithNonMappingTask:
        def list_tasks(self):
            return [FakeTaskNonMapping()]

    result = _existing_repair_fingerprints(CPWithNonMappingTask())
    assert result == {}


def test_dream_repair_origin_type_mismatch_skipped_in_fingerprints():
    """Tasks with wrong origin type are not counted as existing repairs (line 168)."""
    from mac.dream_repair_tasks import _existing_repair_fingerprints  # noqa: PLC0415

    class FakeTask:
        id = "task-y"
        metadata = {"origin": {"type": "some_other_type", "fingerprint": "fp123"}}

    class CP:
        def list_tasks(self):
            return [FakeTask()]

    result = _existing_repair_fingerprints(CP())
    assert result == {}


def test_empty_fingerprint_is_not_stored():
    """Tasks matching the origin type but with empty fingerprint are ignored (line 170->157)."""
    from mac.dream_repair_tasks import _existing_repair_fingerprints, DREAM_REPAIR_ORIGIN_TYPE  # noqa: PLC0415

    class FakeTask:
        id = "task-z"
        metadata = {"origin": {"type": DREAM_REPAIR_ORIGIN_TYPE, "fingerprint": ""}}

    class CP:
        def list_tasks(self):
            return [FakeTask()]

    result = _existing_repair_fingerprints(CP())
    assert result == {}


def test_non_mapping_area_is_skipped_in_affected_labels():
    """Non-Mapping area entries are skipped (line 188)."""
    from mac.dream_repair_tasks import _affected_labels  # noqa: PLC0415

    classification = {"areas": ["not-a-mapping", None, 42]}
    candidate = {}
    result = _affected_labels(candidate, classification)
    assert result["skills"] == []
    assert result["tools"] == []


def test_provider_and_repo_area_are_collected_in_affected_labels():
    """Provider and repo_area area types are captured (lines 197-200)."""
    from mac.dream_repair_tasks import _affected_labels  # noqa: PLC0415

    classification = {
        "areas": [
            {"area_type": "provider", "area_name": "openai"},
            {"area_type": "repo_area", "area_name": "mac.task_executor"},
        ]
    }
    candidate = {}
    result = _affected_labels(candidate, classification)
    assert "openai" in result["providers"]
    assert "mac.task_executor" in result["repo_areas"]


def test_empty_area_name_is_not_added_to_affected_labels():
    """Area entries with empty/None area_name are skipped (line 192)."""
    from mac.dream_repair_tasks import _affected_labels  # noqa: PLC0415

    classification = {
        "areas": [
            {"area_type": "skill", "area_name": ""},
            {"area_type": "tool", "area_name": None},
        ]
    }
    candidate = {}
    result = _affected_labels(candidate, classification)
    assert result["skills"] == []
    assert result["tools"] == []


def test_non_mapping_evidence_items_are_skipped():
    """Non-Mapping evidence items in _candidate_evidence are skipped (line 355)."""
    from mac.dream_repair_tasks import _candidate_evidence  # noqa: PLC0415

    candidate = {
        "evidence": [
            "not-a-mapping",
            None,
            42,
            {"memory_id": "mem-1", "record_type": "note", "excerpt": "some detail"},
            {"memory_id": "mem-2", "record_type": "note"},
        ]
    }
    result = _candidate_evidence(candidate, limit=10)
    # Only Mapping items survive; non-Mapping items are silently skipped
    assert len(result) == 2
    assert result[0]["memory_id"] == "mem-1"


def test_truncate_long_title_adds_ellipsis():
    """Titles longer than 180 chars are truncated with ellipsis (line 435)."""
    from mac.dream_repair_tasks import _truncate  # noqa: PLC0415

    long_text = "a" * 200
    result = _truncate(long_text, 180)
    assert result.endswith("...")
    assert len(result) == 180


def test_truncate_short_title_unchanged():
    """Titles within the limit are returned unchanged."""
    from mac.dream_repair_tasks import _truncate  # noqa: PLC0415

    short_text = "short title"
    assert _truncate(short_text, 180) == short_text


def test_append_unique_empty_value_is_not_appended():
    """_append_unique skips empty strings after cleaning (line 418->exit)."""
    from mac.dream_repair_tasks import _append_unique  # noqa: PLC0415

    values: list[str] = []
    _append_unique(values, "")
    assert values == []
    _append_unique(values, "   ")
    assert values == []


# ---------------------------------------------------------------------------
# Per-cycle spawn budget
# ---------------------------------------------------------------------------


def _distinct_candidates(count: int) -> list[dict[str, Any]]:
    candidates = []
    for index in range(count):
        candidate = _candidate(
            summary="terminal_tool failed variant %d during skill_bundle" % index,
            signature="failure:variant:%d" % index,
            task_id="task-source-%d" % index,
        )
        candidates.append(candidate)
    return candidates


def test_default_budget_caps_distinct_new_tasks_per_cycle() -> None:
    cp = FakeControlPlane()

    report = file_low_confidence_repair_tasks(cp, _distinct_candidates(12))

    assert report["budget"] == 10
    assert report["created_count"] == 10
    assert report["capped_count"] == 2
    assert report["skipped_count"] == 2
    assert len(cp.created) == 10
    capped = [t for t in report["tasks"] if t.get("reason") == "per_cycle_budget_exhausted"]
    assert len(capped) == 2


def test_explicit_budget_argument_wins() -> None:
    cp = FakeControlPlane()

    report = file_low_confidence_repair_tasks(
        cp, _distinct_candidates(5), max_tasks_per_cycle=2
    )

    assert report["budget"] == 2
    assert report["created_count"] == 2
    assert report["capped_count"] == 3
    assert len(cp.created) == 2


def test_env_budget_is_read_via_bounded_helper() -> None:
    cp = FakeControlPlane()

    report = file_low_confidence_repair_tasks(
        cp,
        _distinct_candidates(4),
        environ={"MAC_DREAM_REPAIR_MAX_TASKS_PER_CYCLE": "1"},
    )

    assert report["budget"] == 1
    assert report["created_count"] == 1
    assert report["capped_count"] == 3


def test_invalid_env_budget_falls_back_to_default() -> None:
    cp = FakeControlPlane()

    report = file_low_confidence_repair_tasks(
        cp,
        _distinct_candidates(3),
        environ={"MAC_DREAM_REPAIR_MAX_TASKS_PER_CYCLE": "not-a-number"},
    )

    assert report["budget"] == 10
    assert report["created_count"] == 3
    assert report["capped_count"] == 0


def test_dedup_does_not_consume_budget() -> None:
    cp = FakeControlPlane()

    first = file_low_confidence_repair_tasks(cp, [_candidate()], max_tasks_per_cycle=1)
    assert first["created_count"] == 1

    # Re-run with the same finding plus a new distinct one; the deduped finding
    # must not eat the single-task budget meant for genuinely new work.
    second = file_low_confidence_repair_tasks(
        cp,
        [_candidate(nap_run_id="nap-next"), _distinct_candidates(1)[0]],
        max_tasks_per_cycle=1,
    )
    assert second["deduped_count"] == 1
    assert second["created_count"] == 1
    assert second["capped_count"] == 0
