from types import SimpleNamespace

from mac.attempt_failure_classifier import classify_attempt_failure


def test_attempt_failure_classifier_collects_salvage_pointers():
    result = classify_attempt_failure(
        [
            {
                "event_type": "worker.repository.adopted_pushed_branch",
                "detail": {
                    "checks": {
                        "branch_pushed": True,
                        "repository_branch": "mac/agent/task",
                    }
                },
            },
            {
                "event_type": "memory.learning_recorded",
                "detail": {
                    "id": "memory-1",
                    "lessons": [{"id": "memory-2"}, 3],
                },
            },
            {
                "event_type": "plan.child_published",
                "detail": {"children": [{"id": "task_child"}]},
            },
        ]
    )

    assert result.failure_class == "work"
    assert result.salvage["pushed_branch"] == "mac/agent/task"
    assert result.salvage["recorded_lessons"] == ["memory-2", "3", "memory-1"]
    assert result.salvage["published_children"] == ["task_child"]


def test_attempt_failure_classifier_handles_branch_pushed_without_branch_name():
    result = classify_attempt_failure(
        [{"event_type": "task.evidence_added", "detail": {"branch_pushed": True}}]
    )

    assert result.failure_class == "work"
    assert result.salvage == {"branch_pushed": True}


def test_attempt_failure_classifier_classifies_scope_from_object_history():
    result = classify_attempt_failure(
        [
            SimpleNamespace(
                event_type="task.transitioned",
                detail={"error": "returncode 124", "problems": ["agent run timed out"]},
            )
        ]
    )

    assert result.failure_class == "scope"


def test_attempt_failure_classifier_superseded_preempts_environment():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "heartbeat_offline",
                    "failure_class": "superseded",
                },
            }
        ]
    )

    assert result.failure_class == "superseded"


def test_attempt_failure_classifier_classifies_executor_failed_as_environment():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "executor_failed",
                    "manual_repair_required": True,
                },
            }
        ]
    )

    assert result.failure_class == "environment"


def test_attempt_failure_classifier_executor_failed_in_mixed_history():
    result = classify_attempt_failure(
        [
            {
                "event_type": "task.updated",
                "detail": {"pushed_branch": "mac/agent/task"},
            },
            {
                "event_type": "task.transitioned",
                "detail": {
                    "reason": "executor_failed",
                    "manual_repair_required": True,
                    "returncode": 1,
                },
            },
        ]
    )

    assert result.failure_class == "environment"
    assert result.salvage.get("pushed_branch") == "mac/agent/task"
