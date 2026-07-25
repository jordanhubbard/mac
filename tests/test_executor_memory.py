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


def test_curate_lessons_uses_fleet_scoped_api_token(monkeypatch) -> None:
    """curate_lessons_from_outcome must build the router caller with the
    fleet-scoped MAC_API_TOKEN__<FLEET> (derived from MAC_FLEET) ahead of the
    legacy flat form (mac-g55y)."""
    from mac import eval_runner

    for name in (
        "MAC_FLEET",
        "MAC_API_TOKEN",
        "MAC_API_TOKEN__ROCKY",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("MAC_LESSON_CURATION_ENABLED", "1")
    monkeypatch.setenv("MAC_ROUTER_URL", "http://router")
    monkeypatch.setenv("MAC_LESSON_CURATION_MODEL", "test-model")
    monkeypatch.setenv("MAC_FLEET", "rocky")
    monkeypatch.setenv("MAC_API_TOKEN", "api-flat")
    monkeypatch.setenv("MAC_API_TOKEN__ROCKY", "api-rocky")

    # Keep the recall step hermetic (no hub).
    monkeypatch.setattr(memory, "recall_deployment_lessons", lambda *a, **k: [])

    captured: dict[str, str] = {}

    def _fake_router_model_caller(url, token=""):
        captured["url"] = url
        captured["token"] = token

        def _call(model, prompt, ctx):
            return "a novel lesson about fleet tokens", [], 0.0

        return _call

    monkeypatch.setattr(eval_runner, "router_model_caller", _fake_router_model_caller)

    lessons = memory.curate_lessons_from_outcome(
        {"id": "task_test", "title": "Test", "project": "mac"},
        {"outcome": "success", "evidence_type": "repo_change", "signals": {}},
    )
    assert captured["token"] == "api-rocky"
    assert lessons == ["a novel lesson about fleet tokens"]


def test_curate_lessons_falls_back_to_legacy_flat_token(monkeypatch) -> None:
    """Without a scoped form, the router caller uses the legacy flat
    MAC_API_TOKEN (mac-g55y)."""
    from mac import eval_runner

    for name in (
        "MAC_FLEET",
        "MAC_API_TOKEN",
        "MAC_API_TOKEN__ROCKY",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("MAC_LESSON_CURATION_ENABLED", "1")
    monkeypatch.setenv("MAC_ROUTER_URL", "http://router")
    monkeypatch.setenv("MAC_LESSON_CURATION_MODEL", "test-model")
    monkeypatch.setenv("MAC_FLEET", "rocky")
    monkeypatch.setenv("MAC_API_TOKEN", "api-flat")

    monkeypatch.setattr(memory, "recall_deployment_lessons", lambda *a, **k: [])

    captured: dict[str, str] = {}

    def _fake_router_model_caller(url, token=""):
        captured["token"] = token
        return lambda model, prompt, ctx: ("NOTHING", [], 0.0)

    monkeypatch.setattr(eval_runner, "router_model_caller", _fake_router_model_caller)

    memory.curate_lessons_from_outcome(
        {"id": "task_test", "title": "Test", "project": "mac"},
        {"outcome": "success", "evidence_type": "repo_change", "signals": {}},
    )
    assert captured["token"] == "api-flat"
