"""Contract tests for task-scoped AgentBus directives (task_f321d438).

Reproduces the task_60be7f29 incident and pins the fix:

  * Two authentic operator ``human.directive.v1`` messages with ``task_id=null``
    were consumed by an agent's *persona* sandbox (no repo, no live lease),
    while the real executor held the worktree. The persona's Slack-mirrored
    reply looked like task progress but never steered the run.

The task-scoped path makes a task id a first-class field: the hub validates
current lease ownership before minting an executor-scoped directive, and the
worker routes a verified executor-scoped directive to a durable executor-owned
queue (never a persona chat turn), with a distinct acknowledgement schema so a
conversation mirror can tell executor delivery from a persona reply.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mac.agentbus_service import (
    HUMAN_DIRECTIVE_CONTENT_TYPE,
    HUMAN_DIRECTIVE_TOPIC,
)
from mac.executor_directive import (
    EXECUTOR_ACK_TOPIC,
    ExecutorDirectiveQueue,
    ExecutorDirectiveRecord,
    task_ownership_verdict,
)
from mac.models import ValidationError
from mac.services import ControlPlane
from mac.worker import MacWorker, WorkerExecution


# --------------------------------------------------------------------------- #
# 1. Pure ownership verdict (the shared hub/worker decision)
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime(2026, 7, 24, tzinfo=timezone.utc)


def test_ownership_verdict_deliverable_for_current_lease_holder() -> None:
    v = task_ownership_verdict(
        task_id="task_1",
        target_agent_id="agent_a",
        task_found=True,
        task_state="running",
        owner_agent_id="agent_a",
        lease_id="lease_1",
        leased_until=(_now() + timedelta(minutes=5)).isoformat(),
        now=_now(),
    )
    assert v.deliverable is True
    assert v.status == "deliverable"


@pytest.mark.parametrize(
    "kwargs,status",
    [
        (dict(task_found=False, task_state=None, owner_agent_id=None, lease_id=None, leased_until=None), "no_task"),
        (dict(task_found=True, task_state="open", owner_agent_id=None, lease_id=None, leased_until=None), "no_executor"),
        (dict(task_found=True, task_state="running", owner_agent_id="agent_b", lease_id="l", leased_until="2999-01-01T00:00:00+00:00"), "agent_task_mismatch"),
        (dict(task_found=True, task_state="running", owner_agent_id="agent_a", lease_id="l", leased_until="2000-01-01T00:00:00+00:00"), "lease_expired"),
        (dict(task_found=True, task_state="done", owner_agent_id="agent_a", lease_id="l", leased_until="2999-01-01T00:00:00+00:00"), "lease_expired"),
    ],
)
def test_ownership_verdict_fails_closed(kwargs, status) -> None:
    v = task_ownership_verdict(
        task_id="task_1", target_agent_id="agent_a", now=_now(), **kwargs
    )
    assert v.deliverable is False
    assert v.status == status
    assert v.reason  # actionable reason present


# --------------------------------------------------------------------------- #
# 2. Durable executor-owned queue
# --------------------------------------------------------------------------- #
def test_executor_queue_is_durable_and_idempotent(tmp_path: Path) -> None:
    queue = ExecutorDirectiveQueue(tmp_path / "q.json")
    record = ExecutorDirectiveRecord(
        stream_id="bus_1",
        task_id="task_1",
        correlation_id="corr_1",
        message="rerun the failing test",
        issued_by="jkh",
        enqueued_at="2026-07-24T00:00:00+00:00",
    )
    assert queue.enqueue(record) is True
    # Re-poll of the same stream must not double-enqueue.
    assert queue.enqueue(record) is False
    # Survives a fresh handle (durable file).
    reopened = ExecutorDirectiveQueue(tmp_path / "q.json")
    assert [r.stream_id for r in reopened.pending()] == ["bus_1"]
    updated = reopened.mark_consumed("bus_1", "2026-07-24T00:01:00+00:00")
    assert updated is not None and updated.consumed_at == "2026-07-24T00:01:00+00:00"
    assert reopened.pending() == []


# --------------------------------------------------------------------------- #
# 3. Hub: task-scoped publish validates lease ownership, fails closed
# --------------------------------------------------------------------------- #
def _cp_with_leased_task():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("exec-host")
    executor = cp.register_agent(machine.id, "rocky", agent_id="agent_rocky")
    other = cp.register_agent(machine.id, "bull", agent_id="agent_bull")
    task = cp.create_task("do the thing", project=None)
    cp.claim_task(task.id, executor.id)
    return cp, executor, other, task


def test_hub_publishes_executor_scoped_directive_for_lease_holder() -> None:
    cp, executor, _other, task = _cp_with_leased_task()
    published = cp.publish_human_directive(
        executor.id, "focus the retry on the flaky test", issued_by="jkh", task_id=task.id
    )
    assert published["executor_scoped"] is True
    assert published["task_id"] == task.id
    stream = cp.get_agentbus_stream(published["stream"]["id"])
    assert stream.task_id == task.id
    assert (stream.headers or {}).get("executor_scoped") == "true"
    assert (stream.headers or {}).get("delivery_target") == "executor"


def test_hub_rejects_directive_when_agent_does_not_own_task() -> None:
    cp, _executor, other, task = _cp_with_leased_task()
    with pytest.raises(ValidationError) as exc:
        cp.publish_human_directive(other.id, "hijack", issued_by="jkh", task_id=task.id)
    assert "agent_task_mismatch" in str(exc.value)


def test_hub_rejects_directive_for_unknown_task() -> None:
    cp, executor, _other, _task = _cp_with_leased_task()
    with pytest.raises(ValidationError) as exc:
        cp.publish_human_directive(
            executor.id, "steer", issued_by="jkh", task_id="task_deadbeef"
        )
    assert "no_task" in str(exc.value)


def test_hub_non_task_directive_stays_persona_scoped() -> None:
    cp, executor, _other, _task = _cp_with_leased_task()
    published = cp.publish_human_directive(executor.id, "hello there", issued_by="jkh")
    assert published["executor_scoped"] is False
    stream = cp.get_agentbus_stream(published["stream"]["id"])
    assert (stream.headers or {}).get("delivery_target") == "persona"


def test_verify_surfaces_task_ownership_for_executor_scoped_directive() -> None:
    cp, executor, _other, task = _cp_with_leased_task()
    published = cp.publish_human_directive(
        executor.id, "rerun", issued_by="jkh", task_id=task.id
    )
    v = cp.verify_human_directive(published["stream"]["id"])
    assert v["verified"] is True
    assert v["executor_scoped"] is True
    assert v["task_id"] == task.id
    assert v["deliverable_to_executor"] is True
    assert v["ownership"]["owner_agent_id"] == executor.id


# --------------------------------------------------------------------------- #
# 4. Worker: routing to the active executor vs a persona sandbox
# --------------------------------------------------------------------------- #
class _Client:
    """Scripted hub client for the worker control-poll loop."""

    def __init__(self, *, streams, chunks_by_stream, verification, active_task_id) -> None:
        self.streams = streams
        self.chunks_by_stream = chunks_by_stream
        self.verification = verification
        self.active_task_id = active_task_id
        self.posts: list[tuple[str, dict]] = []

    def get(self, path: str):
        if path.startswith("/agentbus/streams?"):
            return self.streams
        if "/directive-verification" in path:
            return self.verification
        if "/chunks" in path:
            sid = path.split("/agentbus/streams/", 1)[1].split("/chunks", 1)[0]
            return self.chunks_by_stream.get(sid, [])
        if path.startswith("/agents/"):
            return {"current_task_id": self.active_task_id}
        return []

    def post(self, path: str, body):
        self.posts.append((path, body))
        return {}


def _worker(tmp_path: Path, client: _Client) -> MacWorker:
    return MacWorker(
        client,
        "agent_rocky",
        tmp_path,
        lambda _task, _task_dir: WorkerExecution(0, "unused"),
    )


def _directive_stream(stream_id: str, task_id: str) -> dict:
    return {
        "id": stream_id,
        "recipient_agent_id": "agent_rocky",
        "sender_agent_id": "agent_operator",
        "topic": HUMAN_DIRECTIVE_TOPIC,
        "content_type": HUMAN_DIRECTIVE_CONTENT_TYPE,
        "task_id": task_id,
        "headers": {"correlation_id": "corr_1", "issued_by": "jkh"},
    }


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _acks(client: _Client) -> list[dict]:
    return [
        body
        for (path, body) in client.posts
        if path == "/agentbus" and body.get("topic") == EXECUTOR_ACK_TOPIC
    ]


def _peer_replies(client: _Client) -> list[dict]:
    return [
        body
        for (path, body) in client.posts
        if path == "/agentbus" and body.get("topic") == "peer.reply.v1"
    ]


def test_active_executor_receives_directive_with_durable_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MAC_WORKER_DIRECTABLE", "1")
    verification = {
        "verified": True,
        "executor_scoped": True,
        "task_id": "task_active",
        "issued_by": "jkh",
        "message": "rerun the flaky test",
        "ownership": {"deliverable": True, "status": "deliverable"},
    }
    client = _Client(
        streams=[_directive_stream("bus_dir", "task_active")],
        chunks_by_stream={"bus_dir": [{"payload": {"message": "rerun the flaky test"}}]},
        verification=verification,
        active_task_id="task_active",  # THIS worker is the active executor.
    )
    worker = _worker(tmp_path, client)
    worker.agentbus_control_enabled = True
    worker._process_agentbus_control()
    assert _wait_for(lambda: len(_acks(client)) >= 1)

    ack = _acks(client)[0]["payload"]
    assert ack["delivery_kind"] == "task_executor"
    assert ack["status"] == "delivered"
    assert ack["task_id"] == "task_active"
    # No persona chat turn was used for delivery.
    assert _peer_replies(client) == []
    # Durable provenance: the directive is queued in the executor-owned file.
    queue = worker.executor_directive_queue("task_active")
    pending = queue.pending()
    assert [r.stream_id for r in pending] == ["bus_dir"]

    # The executor drains it and acks WHEN it consumed the directive.
    consumed = worker.drain_executor_directives("task_active")
    assert len(consumed) == 1
    assert consumed[0]["consumed_at"]
    consumed_acks = [a["payload"] for a in _acks(client) if a["payload"]["status"] == "consumed"]
    assert consumed_acks and consumed_acks[0]["consumed_at"]


def test_persona_sandbox_without_lease_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """The task_60be reproduction: an agent whose persona has NO active lease
    (and no repo worktree) must NOT accept an executor-scoped directive and
    must NOT run a persona chat turn as if it were executor delivery."""
    monkeypatch.setenv("MAC_WORKER_DIRECTABLE", "1")
    verification = {
        "verified": True,
        "executor_scoped": True,
        "task_id": "task_active",
        "issued_by": "jkh",
        "message": "rerun the flaky test",
        "ownership": {"deliverable": True, "status": "deliverable"},
    }
    client = _Client(
        streams=[_directive_stream("bus_dir", "task_active")],
        chunks_by_stream={"bus_dir": [{"payload": {"message": "rerun the flaky test"}}]},
        verification=verification,
        active_task_id="",  # persona sandbox: no active task / no lease.
    )
    worker = _worker(tmp_path, client)
    worker.agentbus_control_enabled = True
    worker._process_agentbus_control()
    assert _wait_for(lambda: len(_acks(client)) >= 1)

    ack = _acks(client)[0]["payload"]
    assert ack["status"] == "no_executor"
    assert ack["delivery_kind"] == "task_executor"
    assert ack.get("reason")
    # Fails closed: nothing queued, no persona reply masquerading as progress.
    assert worker.executor_directive_queue("task_active").pending() == []
    assert _peer_replies(client) == []


def test_expired_lease_from_hub_verification_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MAC_WORKER_DIRECTABLE", "1")
    verification = {
        "verified": True,
        "executor_scoped": True,
        "task_id": "task_active",
        "issued_by": "jkh",
        "message": "rerun",
        "ownership": {
            "deliverable": False,
            "status": "lease_expired",
            "reason": "lease lease_1 for task task_active expired",
        },
    }
    client = _Client(
        streams=[_directive_stream("bus_dir", "task_active")],
        chunks_by_stream={"bus_dir": [{"payload": {"message": "rerun"}}]},
        verification=verification,
        active_task_id="task_active",
    )
    worker = _worker(tmp_path, client)
    worker.agentbus_control_enabled = True
    worker._process_agentbus_control()
    assert _wait_for(lambda: len(_acks(client)) >= 1)
    ack = _acks(client)[0]["payload"]
    assert ack["status"] == "lease_expired"
    assert worker.executor_directive_queue("task_active").pending() == []


def test_ordinary_non_task_directive_still_runs_persona_turn(tmp_path: Path, monkeypatch) -> None:
    """Criterion 6: non-task directives preserve the persona chat behavior."""
    monkeypatch.setenv("MAC_WORKER_DIRECTABLE", "1")
    verification = {
        "verified": True,
        "executor_scoped": False,
        "issued_by": "jkh",
        "message": "how are the fleet metrics?",
    }
    stream = _directive_stream("bus_dir", "")
    stream["task_id"] = None
    client = _Client(
        streams=[stream],
        chunks_by_stream={"bus_dir": [{"payload": {"message": "how are the fleet metrics?"}}]},
        verification=verification,
        active_task_id="",
    )
    worker = _worker(tmp_path, client)
    worker.agentbus_control_enabled = True
    # Stub the persona turn so we do not spawn a subprocess.
    worker._run_directable_turn = lambda *a, **k: "persona answer"
    worker._process_agentbus_control()
    assert _wait_for(lambda: len(_peer_replies(client)) >= 1)
    # A persona reply (not an executor ack) is emitted for an ordinary directive.
    assert _peer_replies(client)[0]["payload"]["reply"] == "persona answer"
    assert _acks(client) == []
