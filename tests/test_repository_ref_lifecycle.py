from __future__ import annotations

import pytest

from mac.models import TaskState, ValidationError
from mac.services import ControlPlane


REPLACEMENT_ID = "task_" + "b" * 32


def test_cancelled_task_defaults_to_preserved_repository_refs():
    cp = ControlPlane.in_memory()
    task = cp.create_task("cancel conservatively")

    cancelled = cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "operator",
        {"reason": "stopped without a disposition"},
    )

    lifecycle = cancelled.metadata["repository_ref_lifecycle"]
    assert lifecycle["schema"] == "mac.repository_ref_lifecycle.v1"
    assert lifecycle["disposition"] == "preserve"
    assert lifecycle["status"] == "preserved"
    assert lifecycle["eligible_after"] is None
    detail = cp.task_detail(task.id)
    data = detail.to_dict() if hasattr(detail, "to_dict") else detail
    transition = next(item for item in data["history"] if item["event_type"] == "task.transitioned")
    assert transition["detail"]["disposition"] == "preserve"


def test_superseded_task_records_replacement_and_cleanup_schedule():
    cp = ControlPlane.in_memory()
    task = cp.create_task("old implementation")

    cancelled = cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "operator",
        {
            "reason": "replacement was accepted",
            "disposition": "superseded",
            "replacement_task_id": REPLACEMENT_ID,
            "cleanup_grace_seconds": 0,
        },
    )

    lifecycle = cancelled.metadata["repository_ref_lifecycle"]
    assert lifecycle["status"] == "scheduled"
    assert lifecycle["replacement_task_id"] == REPLACEMENT_ID
    assert lifecycle["eligible_after"] == lifecycle["terminal_at"]
    assert cp.list_task_transition_outbox(task_id=task.id) == []


@pytest.mark.parametrize(
    "detail",
    [
        {"disposition": "superseded", "reason": "missing replacement"},
        {"disposition": "not-real", "reason": "invalid"},
        {"disposition": "not_applicable"},
    ],
)
def test_invalid_cancellation_contract_is_rejected(detail):
    cp = ControlPlane.in_memory()
    task = cp.create_task("invalid cancellation")

    with pytest.raises(ValidationError):
        cp.close_task(
            task.id,
            TaskState.CANCELLED.value,
            "operator",
            detail,
        )
    assert cp.get_task(task.id).state == TaskState.OPEN.value


def test_preserved_cancellation_requires_reason():
    cp = ControlPlane.in_memory()
    task = cp.create_task("missing cancellation reason")

    with pytest.raises(ValidationError, match="requires a reason"):
        cp.close_task(
            task.id,
            TaskState.CANCELLED.value,
            "operator",
            {"disposition": "preserve"},
        )

    assert cp.get_task(task.id).state == TaskState.OPEN.value


def test_reopening_cancelled_task_invalidates_cleanup_schedule():
    cp = ControlPlane.in_memory()
    task = cp.create_task("retry later")
    cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "operator",
        {
            "reason": "new task took over",
            "disposition": "duplicate",
            "replacement_task_id": REPLACEMENT_ID,
            "cleanup_grace_seconds": 0,
        },
    )

    reopened = cp.reopen_task(task.id, "operator", "replacement failed")

    lifecycle = reopened.metadata["repository_ref_lifecycle"]
    assert reopened.state == TaskState.OPEN.value
    assert lifecycle["status"] == "active"
    assert lifecycle["eligible_after"] is None


def test_cancelled_task_disposition_can_be_backfilled_without_reopening():
    cp = ControlPlane.in_memory()
    task = cp.create_task("legacy cancellation")
    original = cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "operator",
        {"reason": "legacy cancellation"},
    )
    terminal_at = original.metadata["repository_ref_lifecycle"]["terminal_at"]

    updated = cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        "operator",
        {
            "reason": "replacement was merged",
            "disposition": "superseded",
            "replacement_task_id": REPLACEMENT_ID,
            "cleanup_grace_seconds": 0,
        },
    )

    lifecycle = updated.metadata["repository_ref_lifecycle"]
    assert updated.state == TaskState.CANCELLED.value
    assert lifecycle["disposition"] == "superseded"
    assert lifecycle["terminal_at"] == terminal_at
    raw_detail = cp.task_detail(task.id)
    detail = raw_detail.to_dict() if hasattr(raw_detail, "to_dict") else raw_detail
    assert any(
        item["event_type"] == "repository_ref.lifecycle_updated" for item in detail["history"]
    )


def test_failed_task_is_quarantined_not_scheduled():
    cp = ControlPlane.in_memory()
    task = cp.create_task("failed attempt")

    failed = cp._transition_task_internal(
        task.id,
        TaskState.FAILED.value,
        "worker",
        {"reason": "tests failed"},
    )

    lifecycle = failed.metadata["repository_ref_lifecycle"]
    assert lifecycle["disposition"] == "failed_attempt"
    assert lifecycle["status"] == "quarantined"
    assert lifecycle["eligible_after"] is None


def test_force_complete_records_integrated_cleanup_lifecycle():
    cp = ControlPlane.in_memory()
    task = cp.create_task("completed out of band")

    completed = cp.force_complete_task(task.id, "operator", "merged manually")

    lifecycle = completed.metadata["repository_ref_lifecycle"]
    assert completed.state == TaskState.COMPLETED.value
    assert lifecycle["disposition"] == "integrated"
    assert lifecycle["status"] == "scheduled"
    assert lifecycle["eligible_after"] is not None
    assert cp.list_task_transition_outbox(task_id=task.id) == []
