"""Learning-loop hardening: review rejections become durable project lessons."""

from __future__ import annotations

import json

from mac.services import ControlPlane

from mac.task_executor import _format_learning_content


def _cp(tmp_path=None) -> ControlPlane:
    return ControlPlane.in_memory()


def _lessons(cp, project):
    """Deployment-learning records for a project (what recall reads)."""
    out = []
    for rec in cp.search_memory(subject_type="project", subject_id=project):
        if str(rec.record_type or "").startswith("deployment_learning"):
            out.append(rec)
    return out


def test_records_review_rejection_lesson(tmp_path):
    cp = _cp(tmp_path)
    cp.create_project("mac")
    task = cp.create_task("Do the thing", project="mac")

    cp._record_project_failure_lesson(
        task.id,
        evidence_type="review_verdict",
        error_signature="review_rejected",
        signals={"review_rejected": True, "problems": ["missing tests", "lint fail"]},
    )

    lessons = _lessons(cp, "mac")
    assert len(lessons) == 1
    content = json.loads(lessons[0].content)
    assert content["schema"] == "mac.deployment_learning.v1"
    assert content["outcome"] == "failure"
    assert content["error_signature"] == "review_rejected"
    assert content["project"] == "mac"
    assert content["task_id"] == task.id
    # The record must be renderable by the recall path into a one-line lesson.
    rendered = _format_learning_content(lessons[0].content)
    assert rendered and "review_rejected" in rendered


def test_record_type_matches_recall_contract(tmp_path):
    # recall_deployment_lessons filters by record_type prefix + project; the
    # hub-written record must use the same "deployment_learning:<project>" shape.
    cp = _cp(tmp_path)
    cp.create_project("proj-x")
    task = cp.create_task("t", project="proj-x")
    cp._record_project_failure_lesson(
        task.id, evidence_type="review_verdict", error_signature="review_rejected"
    )
    lessons = _lessons(cp, "proj-x")
    assert lessons and lessons[0].record_type == "deployment_learning:proj-x"


def test_lesson_recording_is_best_effort_on_unknown_task(tmp_path):
    cp = _cp(tmp_path)
    # Must not raise for a nonexistent task (telemetry-only, never breaks review).
    cp._record_project_failure_lesson(
        "task_does_not_exist", evidence_type="review_verdict", error_signature="review_rejected"
    )
