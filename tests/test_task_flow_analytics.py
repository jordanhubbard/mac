from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.cli import build_parser
from mac.models import TaskFlowStage, TaskState, parse_time, utcnow
from mac.services import ControlPlane


def _running_task(cp: ControlPlane, *, project: str = "flow"):
    machine = cp.register_machine("flow-host", resources={"cpu": 4, "memory_gb": 8})
    agent = cp.register_agent(machine.id, "flow-worker", capabilities=[])
    task = cp.create_task("small task", project=project)
    _claimed, lease = cp.claim_task(task.id, agent.id)
    cp.start_task(task.id, agent.id, lease_id=lease.id)
    return task, agent


def test_lifecycle_hook_materializes_current_stage_without_history_scan():
    cp = ControlPlane.in_memory()
    task, _agent = _running_task(cp)

    rows = cp.store.query_all(
        "SELECT stage, ended_at, duration_seconds FROM task_flow_spans "
        "WHERE task_id = ? ORDER BY started_at, stage",
        (task.id,),
    )

    assert [row["stage"] for row in rows] == [
        TaskFlowStage.READY_QUEUE.value,
        TaskFlowStage.CLAIM_TO_START.value,
        TaskFlowStage.EXECUTION.value,
    ]
    assert rows[-1]["ended_at"] is None
    assert rows[-1]["duration_seconds"] is None
    assert all(row["ended_at"] is not None for row in rows[:-1])


def test_terminal_transition_materializes_completion_and_task_show_flow():
    cp = ControlPlane.in_memory()
    task, agent = _running_task(cp)
    when = utcnow()
    with cp.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET state = ?, completed_at = ?, updated_at = ? WHERE id = ?",
            (TaskState.COMPLETED.value, when, when, task.id),
        )
        cp._record_history(
            task.id,
            "task.transitioned",
            agent.id,
            TaskState.RUNNING.value,
            TaskState.COMPLETED.value,
            {"test_terminal": True},
            conn=conn,
        )

    completion = cp.store.query_one(
        "SELECT * FROM task_completions WHERE task_id = ?",
        (task.id,),
    )
    assert completion is not None
    assert completion["outcome"] == "completed"
    assert completion["ended_at"] is not None
    detail = cp.task_detail(task.id)
    assert detail["flow"]["schema"] == "mac.task_flow_detail.v1"
    assert detail["flow"]["completions"][0]["outcome"] == "completed"
    assert (
        detail["flow"]["spans"][-1]["stage"]
        == TaskFlowStage.FINALIZATION.value
    )


def test_report_opens_and_resolves_stranding_episode():
    cp = ControlPlane.in_memory()
    task, _agent = _running_task(cp)
    future = (parse_time(utcnow()) + timedelta(minutes=11)).isoformat(
        timespec="microseconds"
    )

    report = cp.task_flow.report(
        warning_seconds=300,
        critical_seconds=600,
        refresh_limit=0,
        idle_worker_count=0,
        observed_at=future,
    )

    assert report["schema"] == "mac.task_flow_snapshot.v1"
    assert report["stranding"]["count"] == 1
    episode = report["stranding"]["episodes"][0]
    assert episode["task_id"] == task.id
    assert episode["stage"] == TaskFlowStage.EXECUTION.value
    assert episode["severity"] == "critical"
    stored = cp.store.query_one(
        "SELECT * FROM task_stranding_episodes WHERE id = ?",
        (episode["id"],),
    )
    assert stored is not None and stored["resolved_at"] is None

    when = (parse_time(future) + timedelta(seconds=1)).isoformat(
        timespec="microseconds"
    )
    with cp.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET state = ?, completed_at = ?, updated_at = ? WHERE id = ?",
            (TaskState.COMPLETED.value, when, when, task.id),
        )
        cp._record_history(
            task.id,
            "task.transitioned",
            "test",
            TaskState.RUNNING.value,
            TaskState.COMPLETED.value,
            {},
            conn=conn,
        )
    cp.task_flow.report(
        warning_seconds=300,
        critical_seconds=600,
        refresh_limit=0,
        observed_at=when,
    )
    resolved = cp.store.query_one(
        "SELECT resolved_at FROM task_stranding_episodes WHERE id = ?",
        (episode["id"],),
    )
    assert resolved["resolved_at"] == when


def test_contention_uses_digest_and_is_included_in_snapshot():
    cp = ControlPlane.in_memory()
    task = cp.create_task("collision", project="flow")
    raw_resource = "git@example.invalid/private.git:refs/heads/main"
    event = cp.task_flow.record_contention(
        task_id=task.id,
        project="flow",
        attempt=1,
        stage=TaskFlowStage.PUBLICATION.value,
        resource_class="repository_ref",
        resource_key=raw_resource,
        reason="base_moved_merge_conflict",
        peer_task_ids=["task_peer"],
    )
    row = cp.store.query_one(
        "SELECT * FROM task_resource_contentions WHERE id = ?",
        (event["id"],),
    )

    assert raw_resource not in str(dict((key, row[key]) for key in row.keys()))
    report = cp.task_flow.report(
        project="flow",
        refresh_limit=0,
        warning_seconds=3600,
        critical_seconds=7200,
    )
    assert report["contention"]["count"] == 1
    assert report["contention"]["by_resource_class"]["repository_ref"]["count"] == 1


def test_task_throughput_api_and_remote_shape():
    cp = ControlPlane.in_memory()
    cp.create_task("queued", project="flow")
    client = TestClient(create_app(control_plane=cp))

    response = client.get(
        "/tasks/throughput",
        params={
            "project": "flow",
            "since_hours": 12,
            "warning_seconds": 300,
            "critical_seconds": 600,
            "refresh_limit": 10,
        },
    )

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["schema"] == "mac.task_flow_snapshot.v1"
    assert report["project"] == "flow"
    assert report["active"]["count"] == 1
    assert report["snapshot_id"].startswith("flowsnap_")


def test_task_throughput_cli_parser_exposes_bounded_controls():
    args = build_parser().parse_args(
        [
            "task",
            "throughput",
            "--all",
            "--since-hours",
            "6",
            "--warning-minutes",
            "4",
            "--critical-minutes",
            "9",
            "--refresh-limit",
            "25",
        ]
    )

    assert args.all is True
    assert args.since_hours == 6
    assert args.warning_minutes == 4
    assert args.critical_minutes == 9
    assert args.refresh_limit == 25
