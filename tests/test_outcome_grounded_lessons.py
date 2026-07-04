"""Outcome-grounded learning loop (B from the Hermes evaluation):
review verdicts become recallable lessons, curation is LLM-forked but
outcome-grounded, and memory content is searchable."""

from __future__ import annotations

import json

import mac.task_executor as te
from mac.services import ControlPlane


def _cp() -> ControlPlane:
    return ControlPlane.in_memory()


def test_review_outcome_lesson_lands_in_deployment_learning(monkeypatch):
    cp = _cp()
    task = cp.create_task("Fix the thing", required_capabilities=["python"],
                          metadata={"publication_target": "test://x"})
    cp._record_review_outcome_lesson(
        task.id, outcome="review_rejected", detail="hub contract verification failed: 1 failed"
    )
    records = cp.search_memory(record_type_prefix="deployment_learning")
    assert len(records) == 1
    content = json.loads(records[0].content)
    # Emitted in the executor's recall schema so recall_deployment_lessons
    # surfaces it to the next task with zero reader changes.
    assert content["schema"] == "mac.deployment_learning.v1"
    assert content["outcome"] == "review_rejected"
    assert "verification failed" in content["error_signature"]
    assert records[0].created_by == "hub-review-workflow"


def test_review_outcome_lesson_never_breaks_workflow(monkeypatch):
    cp = _cp()
    # Nonexistent task -> get_task raises inside -> swallowed, no exception out.
    cp._record_review_outcome_lesson("task_missing", outcome="approved_published", detail="x")


def test_memory_search_content_contains():
    cp = _cp()
    task = cp.create_task("T", required_capabilities=["python"])
    cp.add_memory(task.id, "project", "mac", "deployment_learning:mac",
                  json.dumps({"error_signature": "git merge-tree needs 2.38"}), None, "t")
    cp.add_memory(task.id, "project", "mac", "deployment_learning:mac",
                  json.dumps({"error_signature": "unrelated"}), None, "t")
    hits = cp.search_memory(content_contains="MERGE-TREE")
    assert len(hits) == 1 and "merge-tree" in hits[0].content
    assert len(cp.search_memory(content_contains="nomatchxyz")) == 0


def test_curation_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MAC_LESSON_CURATION_ENABLED", raising=False)
    assert te.curate_lessons_from_outcome({"title": "t"}, {"outcome": "success"}) == []


def test_curation_parses_lessons_and_caps(monkeypatch):
    monkeypatch.setenv("MAC_LESSON_CURATION_ENABLED", "1")
    monkeypatch.setenv("MAC_ROUTER_URL", "http://router.test/v1")
    monkeypatch.setenv("MAC_LESSON_CURATION_MODEL", "test-model")

    def fake_caller_factory(url, token=""):
        def call(model, question, context):
            assert model == "test-model"
            assert "Outcome: failure" in question
            return ("- lesson one about the venv\nNOTHING\nlesson two\nlesson three\nlesson four", [], 5.0)
        return call

    import mac.eval_runner as er
    monkeypatch.setattr(er, "router_model_caller", fake_caller_factory)
    lessons = te.curate_lessons_from_outcome(
        {"title": "t", "metadata": {}},
        {"outcome": "failure", "evidence_type": "repo_change",
         "signals": {"tests": "fail"}, "error_signature": "boom"},
    )
    assert lessons == ["lesson one about the venv", "lesson two", "lesson three"]


def test_curation_nothing_and_errors_yield_empty(monkeypatch):
    monkeypatch.setenv("MAC_LESSON_CURATION_ENABLED", "1")
    monkeypatch.setenv("MAC_ROUTER_URL", "http://router.test/v1")
    monkeypatch.setenv("MAC_LESSON_CURATION_MODEL", "m")
    import mac.eval_runner as er
    monkeypatch.setattr(er, "router_model_caller",
                        lambda url, token="": lambda m, q, c: ("NOTHING", [], 1.0))
    assert te.curate_lessons_from_outcome({"title": "t"}, {"outcome": "success"}) == []
    def raising_factory(url, token=""):
        def call(m, q, c):
            raise ConnectionError("router down")
        return call
    monkeypatch.setattr(er, "router_model_caller", raising_factory)
    assert te.curate_lessons_from_outcome({"title": "t"}, {"outcome": "success"}) == []


def test_record_curated_lessons_posts_learning_records(monkeypatch):
    monkeypatch.setattr(te, "curate_lessons_from_outcome",
                        lambda task, outcome: ["use the ppa git", "bootstrap needs --venv-only"])
    posted = []
    monkeypatch.setattr(te, "_hub_post", lambda path, payload: posted.append((path, payload)) or True)
    n = te.record_curated_lessons(
        {"id": "task_x", "title": "T", "metadata": {}},
        {"outcome": "failure", "evidence_type": "repo_change", "signals": {"tests": "fail"}},
    )
    assert n == 2
    assert all(p[0] == "/memory" for p in posted)
    contents = [json.loads(p[1]["content"]) for p in posted]
    assert contents[0]["error_signature"] == "use the ppa git"
    assert all(c["signals"]["curated"] is True for c in contents)
    assert all(c["schema"] == "mac.deployment_learning.v1" for c in contents)
