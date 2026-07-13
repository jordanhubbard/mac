"""Isolation and compatibility tests for :mod:`mac.executor_memory`."""

from __future__ import annotations

from mac import executor_memory as memory
from mac import task_executor as te


def test_task_executor_reexports_memory_surface() -> None:
    names = (
        "build_telemetry_record",
        "emit_telemetry",
        "build_learning_record",
        "record_deployment_learning",
        "recall_deployment_lessons",
        "build_plan_learning_record",
        "record_plan_outcome",
        "recall_plan_lessons",
    )
    for name in names:
        assert getattr(te, name) is getattr(memory, name)


def test_build_telemetry_record_has_executor_identity(monkeypatch) -> None:
    monkeypatch.setattr(memory, "local_agent_id", lambda: "agent_test")
    record = memory.build_telemetry_record(
        "started", task_id="task_test", level="info", detail={"attempt": 1}
    )
    assert record["name"] == "executor.started"
    assert record["subject_id"] == "task_test"
    assert record["detail"]["agent_id"] == "agent_test"


def test_build_learning_record_has_project_scope() -> None:
    record = memory.build_learning_record(
        {"id": "task_test", "title": "Test", "project": "mac"},
        {"outcome": "success", "evidence_type": "repo_change", "signals": {}},
    )
    assert record["subject_type"] == "project"
    assert record["subject_id"] == "mac"
    assert record["record_type"] == "deployment_learning:mac"


def test_recall_deployment_lessons_returns_empty_without_hub(monkeypatch) -> None:
    monkeypatch.setattr(memory, "_hub_get", lambda *_args, **_kwargs: None)
    assert memory.recall_deployment_lessons({"title": "Test", "project": "mac"}) == []


def test_record_deployment_learning_returns_false_without_hub(monkeypatch) -> None:
    monkeypatch.setattr(memory, "_hub_post", lambda *_args, **_kwargs: False)
    assert memory.record_deployment_learning(
        {"id": "task_test", "project": "mac"}, {"outcome": "success"}
    ) is False


def test_append_lesson_enforces_budget(monkeypatch) -> None:
    monkeypatch.setattr(memory, "_LESSON_PROMPT_BUDGET", 10)
    lessons = []
    assert memory._append_lesson_with_budget(lessons, "123456789012345") is False
    assert sum(len(item) for item in lessons) == 10
    assert memory._append_lesson_with_budget(lessons, "more") is False


def test_string_list_rejects_non_lists() -> None:
    assert memory._string_list("not-a-list") == []
    assert memory._string_list(["a", "", 2]) == ["a", "2"]
