"""Outcome-grounded learning loop (B from the Hermes evaluation):
review verdicts and finalizer refusals become recallable lessons, and memory
content is searchable."""

from __future__ import annotations

import json

import mac.task_executor as te
from mac import executor_memory as memory
from mac.services import ControlPlane


def _cp() -> ControlPlane:
    return ControlPlane.in_memory()


def test_review_outcome_lesson_lands_in_deployment_learning(monkeypatch):
    cp = _cp()
    task = cp.create_task(
        "Fix the thing",
        required_capabilities=["python"],
        metadata={"publication_target": "test://x"},
    )
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
    cp.add_memory(
        task.id,
        "project",
        "mac",
        "deployment_learning:mac",
        json.dumps({"error_signature": "git merge-tree needs 2.38"}),
        None,
        "t",
    )
    cp.add_memory(
        task.id,
        "project",
        "mac",
        "deployment_learning:mac",
        json.dumps({"error_signature": "unrelated"}),
        None,
        "t",
    )
    hits = cp.search_memory(content_contains="MERGE-TREE")
    assert len(hits) == 1 and "merge-tree" in hits[0].content
    assert len(cp.search_memory(content_contains="nomatchxyz")) == 0


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
    out = cp.publish_agent_reflection(target.id, recipient_agent_id=requester.id, reflect_timeout=0)
    # Two streams exist: inventory to requester, deep request to target.
    assert out["deep_request_stream"]
    assert out["count"] == 1 and out["payload"]["schema"] == "mac.agentbus.agent_reflection.v2"


# ---------------------------------------------------------------------------
# Refusal-to-lesson pipeline: new-file finalizer refusals become lessons
# ---------------------------------------------------------------------------


def test_refusal_to_lesson_end_to_end(monkeypatch, tmp_path):
    """End-to-end: classify_outcome on a finalizer-refusal evidence file produces
    an outcome that record_deployment_learning persists as a recallable
    deployment-learning record carrying the refusal kind."""
    posted = []
    monkeypatch.setattr(
        memory, "_hub_post", lambda path, payload: posted.append((path, payload)) or True
    )

    (tmp_path / "mac-evidence.json").write_text(
        json.dumps(
            {
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
            }
        )
    )

    task = {"id": "t_refusal", "title": "Add codegen output", "project": "mac", "metadata": {}}
    outcome = te.classify_outcome(tmp_path, task, 0)

    # The classified outcome must carry the refusal kind.
    assert outcome["error_signature"] == "untracked_new_files_at_finalize"
    assert outcome["signals"]["finalizer_refusal_kind"] == "untracked_new_files"

    assert te.record_deployment_learning(task, outcome) is True
    assert len(posted) == 1
    assert posted[0][0] == "/memory"
    content = json.loads(posted[0][1]["content"])
    assert content["schema"] == "mac.deployment_learning.v1"
    assert content["error_signature"] == "untracked_new_files_at_finalize"
