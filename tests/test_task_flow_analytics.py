from __future__ import annotations

import json
from datetime import timedelta

from fastapi.testclient import TestClient

from mac.allocator import (
    AllocationAgent,
    AllocationTask,
    AuthoritativeAllocator,
    ClaimCommit,
)
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


def _dispatch_round(
    *,
    round_id: str,
    when: str,
    assignments=None,
    claim_failures=None,
):
    return {
        "schema": "mac.allocation_round_result.v1",
        "round_id": round_id,
        "allocator_version": "authoritative-v2",
        "source": "test-allocator",
        "started_at": (
            parse_time(when) - timedelta(milliseconds=20)
        ).isoformat(timespec="microseconds"),
        "completed_at": when,
        "ready_task_ids": ["task_ready"],
        "candidate_task_ids": ["task_ready"],
        "available_agent_ids": ["agent_free"],
        "assignments": assignments or [],
        "stranded_task_ids": [] if assignments else ["task_ready"],
        "claim_failures": claim_failures or [],
    }


def test_dispatch_round_is_durable_observable_and_in_throughput_report():
    cp = ControlPlane.in_memory()
    when = utcnow()
    result = cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_success",
            when=when,
            assignments=[
                {
                    "task_id": "task_ready",
                    "agent_id": "agent_free",
                    "lease_id": "lease_exact",
                }
            ],
        )
    )

    assert result["schema"] == "mac.dispatch_round.v2"
    assert result["assignment_count"] == 1
    assert result["mismatch"] is None
    stored = cp.store.query_one(
        "SELECT * FROM dispatch_rounds WHERE id = ?",
        ("round_success",),
    )
    assert stored is not None
    assert stored["ready_count"] == 1
    assert stored["free_capacity"] == 1
    assert stored["assignment_count"] == 1
    assert not cp.list_observability(
        name="dispatcher.v2.round",
        subject_id="round_success",
    )

    report = cp.task_flow.report(refresh_limit=0)
    assert report["dispatch"]["round_count"] == 1
    assert report["dispatch"]["assignment_count"] == 1
    assert report["dispatch"]["claim_failure_count"] == 0
    assert report["dispatch"]["ready_free_capacity_mismatch"]["active_count"] == 0
    assert "dispatch_rounds" in report["data_sources"]


def test_empty_dispatch_round_is_write_free():
    cp = ControlPlane.in_memory()
    result = cp.task_flow.record_dispatch_round(
        {
            "round_id": "round_empty",
            "started_at": utcnow(),
            "completed_at": utcnow(),
            "ready_task_ids": [],
            "available_agent_ids": ["agent_idle"],
            "assignments": [],
            "unmatched_task_ids": [],
            "stranded_task_ids": [],
            "claim_failures": [],
        }
    )

    assert result["retained"] is False
    assert cp.store.query_one(
        "SELECT 1 FROM dispatch_rounds WHERE id = ?",
        ("round_empty",),
    ) is None
    assert not cp.list_observability(name="dispatcher.v2.round")


def test_selected_claim_rejection_is_error_level_and_rate_limited():
    cp = ControlPlane.in_memory()
    start = parse_time(utcnow())
    failure = {
        "task_id": "task_ready",
        "agent_id": "agent_free",
        "reason": "transactional_dependency_rejection",
    }

    first = cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_rejected_1",
            when=start.isoformat(timespec="microseconds"),
            claim_failures=[failure],
        )
    )
    cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_rejected_2",
            when=(start + timedelta(minutes=1)).isoformat(
                timespec="microseconds"
            ),
            claim_failures=[failure],
        )
    )

    assert first["claim_failure_count"] == 1
    assert first["false_ready_count"] == 1
    assert not cp.list_observability(
        name="dispatcher.v2.round",
        subject_id="round_rejected_1",
    )
    rejection_events = cp.list_observability(
        name="dispatcher.v2.selected_claim_rejected",
        subject_id="task_ready",
    )
    assert len(rejection_events) == 1
    assert rejection_events[0].level == "error"
    assert rejection_events[0].detail["reason"] == (
        "transactional_dependency_rejection"
    )


def test_ready_free_capacity_mismatch_escalates_at_five_and_ten_minutes():
    cp = ControlPlane.in_memory()
    start = parse_time(utcnow())

    opened = cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_mismatch_open",
            when=start.isoformat(timespec="microseconds"),
        )
    )["mismatch"]
    assert opened["severity"] == "pending"

    cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_mismatch_repeat",
            when=(start + timedelta(minutes=1)).isoformat(
                timespec="microseconds"
            ),
        )
    )
    names = [
        event.name
        for event in cp.list_observability(
            subject_type="dispatch_mismatch",
            subject_id=opened["episode_id"],
        )
    ]
    assert names == ["dispatcher.v2.ready_capacity_mismatch_opened"]

    warning = cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_mismatch_warning",
            when=(start + timedelta(minutes=6)).isoformat(
                timespec="microseconds"
            ),
        )
    )["mismatch"]
    assert warning["severity"] == "warning"

    critical = cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_mismatch_critical",
            when=(start + timedelta(minutes=11)).isoformat(
                timespec="microseconds"
            ),
        )
    )["mismatch"]
    assert critical["severity"] == "critical"
    state = cp.store.query_one(
        "SELECT * FROM dispatch_mismatch_state WHERE scope = ?",
        ("*",),
    )
    assert state["resolved_at"] is None
    assert state["severity"] == "critical"

    resolved = cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_mismatch_resolved",
            when=(start + timedelta(minutes=12)).isoformat(
                timespec="microseconds"
            ),
            assignments=[
                {
                    "task_id": "task_ready",
                    "agent_id": "agent_free",
                    "lease_id": "lease_recovery",
                }
            ],
        )
    )["mismatch"]
    assert resolved["active"] is False
    state = cp.store.query_one(
        "SELECT * FROM dispatch_mismatch_state WHERE scope = ?",
        ("*",),
    )
    assert state["resolved_at"] is not None
    assert cp.task_flow.report(refresh_limit=0)["dispatch"][
        "ready_free_capacity_mismatch"
    ]["active_count"] == 0


def test_authoritative_allocator_result_records_through_real_hook():
    cp = ControlPlane.in_memory()
    result = AuthoritativeAllocator(
        on_round_complete=cp.task_flow.record_dispatch_round
    ).allocate_round(
        [
            AllocationTask(
                id="task_hook",
                priority=10,
                created_at=utcnow(),
            )
        ],
        [AllocationAgent(id="agent_hook")],
        lambda proposal: ClaimCommit.success(
            {
                "task_id": proposal.task_id,
                "agent_id": proposal.agent_id,
                "lease_id": "lease_hook",
            }
        ),
        round_id="round_hook",
    )

    assert result.completion_hook_error is None
    row = cp.store.query_one(
        "SELECT * FROM dispatch_rounds WHERE id = ?",
        ("round_hook",),
    )
    assert row is not None
    assert row["ready_count"] == 1
    assert row["free_capacity"] == 1
    assert row["assignment_count"] == 1
    assert row["false_ready_count"] == 0


def test_empty_round_resolves_active_mismatch_without_retaining_round():
    cp = ControlPlane.in_memory()
    opened = cp.task_flow.record_dispatch_round(
        _dispatch_round(round_id="round_open", when=utcnow())
    )
    assert opened["mismatch"]["active"] is True

    empty = cp.task_flow.record_dispatch_round(
        {
            "round_id": "round_empty_resolution",
            "started_at": utcnow(),
            "completed_at": utcnow(),
            "ready_task_ids": [],
            "available_agent_ids": ["agent_idle"],
            "assignments": [],
            "unmatched_task_ids": [],
            "stranded_task_ids": [],
            "claim_failures": [],
        }
    )

    assert empty["retained"] is False
    assert empty["mismatch"]["active"] is False
    assert cp.store.query_one(
        "SELECT 1 FROM dispatch_rounds WHERE id = ?",
        ("round_empty_resolution",),
    ) is None
    state = cp.store.query_one(
        "SELECT * FROM dispatch_mismatch_state WHERE scope = ?",
        ("*",),
    )
    assert state["resolved_at"] is not None


def test_stale_mismatch_observation_cannot_reopen_resolved_episode():
    cp = ControlPlane.in_memory()
    start = parse_time(utcnow())
    cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_open",
            when=start.isoformat(timespec="microseconds"),
        )
    )
    resolved_at = (start + timedelta(minutes=2)).isoformat(timespec="microseconds")
    cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_resolve",
            when=resolved_at,
            assignments=[
                {
                    "task_id": "task_ready",
                    "agent_id": "agent_free",
                    "lease_id": "lease_recovery",
                }
            ],
        )
    )

    stale = cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_stale",
            when=(start + timedelta(minutes=1)).isoformat(
                timespec="microseconds"
            ),
        )
    )["mismatch"]

    assert stale["ignored"] is True
    assert stale["reason"] == "stale_observation"
    state = cp.store.query_one(
        "SELECT * FROM dispatch_mismatch_state WHERE scope = ?",
        ("*",),
    )
    assert state["resolved_at"] == resolved_at
    assert state["last_observed_at"] == resolved_at


def test_claim_failure_reason_is_bounded_in_round_and_observability():
    cp = ControlPlane.in_memory()
    long_reason = "prefix-" + ("x" * 100_000) + "-suffix"
    cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_large_failure",
            when=utcnow(),
            claim_failures=[
                {
                    "task_id": "task_ready",
                    "agent_id": "agent_free",
                    "reason": long_reason,
                }
            ],
        )
    )

    row = cp.store.query_one(
        "SELECT detail FROM dispatch_rounds WHERE id = ?",
        ("round_large_failure",),
    )
    stored_reason = json.loads(row["detail"])["claim_failures"][0]["reason"]
    assert len(stored_reason.encode("utf-8")) <= 2048
    assert "[truncated]" in stored_reason
    events = cp.list_observability(
        name="dispatcher.v2.selected_claim_rejected",
        subject_id="task_ready",
    )
    assert len(events) == 1
    assert len(events[0].detail["reason"].encode("utf-8")) <= 2048


def test_material_round_lifecycle_prunes_expired_dispatch_rounds():
    cp = ControlPlane.in_memory()
    now = parse_time(utcnow())
    assignment = [
        {
            "task_id": "task_ready",
            "agent_id": "agent_free",
            "lease_id": "lease_current",
        }
    ]
    cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_to_expire",
            when=now.isoformat(timespec="microseconds"),
            assignments=assignment,
        )
    )
    old = (now - timedelta(days=100)).isoformat(timespec="microseconds")
    cp.store.execute(
        "UPDATE dispatch_rounds SET created_at = ? WHERE id = ?",
        (old, "round_to_expire"),
    )
    cp.task_flow._dispatch_retention_last_at = 0.0

    cp.task_flow.record_dispatch_round(
        _dispatch_round(
            round_id="round_retention_trigger",
            when=utcnow(),
            assignments=assignment,
        )
    )

    assert cp.store.query_one(
        "SELECT 1 FROM dispatch_rounds WHERE id = ?",
        ("round_to_expire",),
    ) is None
    assert cp.store.query_one(
        "SELECT 1 FROM dispatch_rounds WHERE id = ?",
        ("round_retention_trigger",),
    ) is not None
