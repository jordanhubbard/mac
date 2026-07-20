"""Outcome-grounded learning loop (B from the Hermes evaluation):
review verdicts become recallable lessons, curation is LLM-forked but
outcome-grounded, and memory content is searchable."""

from __future__ import annotations

import json

import mac.task_executor as te
from mac import executor_memory as memory
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
    monkeypatch.setattr(memory, "curate_lessons_from_outcome",
                        lambda task, outcome: ["use the ppa git", "bootstrap needs --venv-only"])
    posted = []
    monkeypatch.setattr(memory, "_hub_post", lambda path, payload: posted.append((path, payload)) or True)
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


def test_curation_prompt_includes_existing_lessons_for_dedup(monkeypatch):
    # v2: the curator sees what the project already knows and is told to add
    # only novel lessons (first live batch re-derived one insight three times).
    monkeypatch.setenv("MAC_LESSON_CURATION_ENABLED", "1")
    monkeypatch.setenv("MAC_ROUTER_URL", "http://router.test/v1")
    monkeypatch.setenv("MAC_LESSON_CURATION_MODEL", "m")
    monkeypatch.setattr(memory, "recall_deployment_lessons",
                        lambda task, limit=8: ["pushed=false means the delivery step failed"])
    captured = {}

    def factory(url, token=""):
        def call(model, question, context):
            captured["q"] = question
            return ("NOTHING", [], 1.0)
        return call

    import mac.eval_runner as er
    monkeypatch.setattr(er, "router_model_caller", factory)
    te.curate_lessons_from_outcome({"title": "t", "metadata": {}}, {"outcome": "failure"})
    assert "pushed=false means the delivery step failed" in captured["q"]
    assert "genuinely novel" in captured["q"]


def test_publish_agent_reflection_forwards_deep_request():
    # The hub inventory alone is not reflection — the target agent's runtime
    # must be consulted (live test returned a template that failed the
    # ground-truth check). publish_agent_reflection now ALSO forwards a deep
    # reflect request to the target agent, whose worker answers via its own
    # runtime back to the requester.
    from mac.services import ControlPlane
    from tests.test_control_plane import register_agent

    cp = ControlPlane.in_memory()
    target = register_agent(cp, "target", ["python"])
    requester = register_agent(cp, "requester", ["review"])
    out = cp.publish_agent_reflection(
        target.id, recipient_agent_id=requester.id, reflect_timeout=0
    )
    # Two streams exist: inventory to requester, deep request to target.
    assert out["deep_request_stream"]
    assert out["count"] == 1 and out["payload"]["schema"] == "mac.agentbus.agent_reflection.v1"


# ---------------------------------------------------------------------------
# Refusal-to-lesson pipeline: new-file finalizer refusals become curated lessons
# ---------------------------------------------------------------------------


def test_curation_prompt_includes_finalizer_refusal_kind_in_signals(monkeypatch):
    """When a task fails with untracked_new_files_at_finalize, curate_lessons_from_outcome
    passes finalizer_refusal_kind through to the LLM curation prompt so the curator
    can emit a targeted lesson about committing new files before finishing."""
    monkeypatch.setenv("MAC_LESSON_CURATION_ENABLED", "1")
    monkeypatch.setenv("MAC_ROUTER_URL", "http://router.test/v1")
    monkeypatch.setenv("MAC_LESSON_CURATION_MODEL", "m")
    monkeypatch.setattr(memory, "recall_deployment_lessons", lambda task, limit=8: [])
    captured = {}

    def factory(url, token=""):
        def call(model, question, context):
            captured["q"] = question
            return ("NOTHING", [], 1.0)
        return call

    import mac.eval_runner as er
    monkeypatch.setattr(er, "router_model_caller", factory)

    outcome = {
        "outcome": "failure",
        "evidence_type": "repo_change",
        "error_signature": "untracked_new_files_at_finalize",
        "signals": {
            "finalizer_refusal_kind": "untracked_new_files",
            "untracked_files": ["generated.py"],
        },
    }
    te.curate_lessons_from_outcome({"title": "Add feature X", "metadata": {}}, outcome)

    assert "untracked_new_files_at_finalize" in captured["q"], (
        "curation prompt must include the error_signature so the curator knows why the task failed"
    )
    assert "finalizer_refusal_kind" in captured["q"], (
        "curation prompt must include finalizer_refusal_kind signal from the outcome"
    )
    assert "untracked_new_files" in captured["q"], (
        "curation prompt must include the refusal kind value"
    )


def test_curation_prompt_finalizer_refusal_staged_new_files(monkeypatch):
    """staged_new_files variant: the curation prompt includes finalizer_refusal_kind=staged_new_files."""
    monkeypatch.setenv("MAC_LESSON_CURATION_ENABLED", "1")
    monkeypatch.setenv("MAC_ROUTER_URL", "http://router.test/v1")
    monkeypatch.setenv("MAC_LESSON_CURATION_MODEL", "m")
    monkeypatch.setattr(memory, "recall_deployment_lessons", lambda task, limit=8: [])
    captured = {}

    def factory(url, token=""):
        def call(model, question, context):
            captured["q"] = question
            return ("NOTHING", [], 1.0)
        return call

    import mac.eval_runner as er
    monkeypatch.setattr(er, "router_model_caller", factory)

    outcome = {
        "outcome": "failure",
        "evidence_type": "repo_change",
        "error_signature": "untracked_new_files_at_finalize",
        "signals": {
            "finalizer_refusal_kind": "staged_new_files",
            "staged_new_files": ["tests/new_test.py"],
        },
    }
    te.curate_lessons_from_outcome({"title": "Patch tests", "metadata": {}}, outcome)

    assert "staged_new_files" in captured["q"]
    assert "untracked_new_files_at_finalize" in captured["q"]


def test_refusal_to_lesson_end_to_end(monkeypatch, tmp_path):
    """End-to-end: classify_outcome on a finalizer-refusal evidence file produces
    an outcome that, when passed to record_curated_lessons, flows the refusal kind
    into the curation pipeline as a structured signal."""
    monkeypatch.setenv("MAC_LESSON_CURATION_ENABLED", "1")
    monkeypatch.setenv("MAC_ROUTER_URL", "http://router.test/v1")
    monkeypatch.setenv("MAC_LESSON_CURATION_MODEL", "m")
    monkeypatch.setattr(memory, "recall_deployment_lessons", lambda task, limit=8: [])
    captured = {}

    def factory(url, token=""):
        def call(model, question, context):
            captured["q"] = question
            return ("Always commit new files before marking a task done.", [], 1.0)
        return call

    import mac.eval_runner as er
    monkeypatch.setattr(er, "router_model_caller", factory)

    posted = []
    monkeypatch.setattr(memory, "_hub_post", lambda path, payload: posted.append((path, payload)) or True)

    (tmp_path / "mac-evidence.json").write_text(json.dumps({
        "evidence_type": "repo_change",
        "status": "fail",
        "problems": [
            "untracked files present at finalize time — agent must commit ALL new files before declaring done: generated.txt"
        ],
        "repo": {
            "pushed": False,
            "dirty": True,
            "files_changed": [],
            "untracked_files": ["generated.txt"],
            "staged_new_files": [],
        },
        "checks": [{"name": "git_finalizer", "returncode": 1, "status": "fail"}],
    }))

    task = {"id": "t_refusal", "title": "Add codegen output", "project": "mac", "metadata": {}}
    outcome = te.classify_outcome(tmp_path, task, 0)

    # The classified outcome must carry the refusal kind.
    assert outcome["error_signature"] == "untracked_new_files_at_finalize"
    assert outcome["signals"]["finalizer_refusal_kind"] == "untracked_new_files"

    # The outcome feeds into the lesson curation pipeline.
    n = te.record_curated_lessons(task, outcome)
    assert n == 1, "one lesson should be recorded for the refusal outcome"
    assert len(posted) == 1
    content = json.loads(posted[0][1]["content"])
    assert content["schema"] == "mac.deployment_learning.v1"
    assert content["error_signature"] == "Always commit new files before marking a task done."

    # The curation prompt must have included the refusal kind so the curator
    # had enough context to produce a targeted lesson.
    assert "finalizer_refusal_kind" in captured["q"]
    assert "untracked_new_files_at_finalize" in captured["q"]
