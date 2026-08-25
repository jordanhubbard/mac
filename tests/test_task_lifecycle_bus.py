"""Task lifecycle reaches the bus, and a CLI agent can take it without blocking.

Two defects, one session, opposite directions (task_7faf8e56).

**Nothing published.** A topic census on 2026-08-21 found eleven live topics
fleet-wide -- reflect requests, peer messages, repo updates, human directives --
and no ``task.*`` among them. The fleet's entire subject matter was absent from
its own bus, so nothing could subscribe to it and every observer polled. An
operator session discovered eleven tasks being filed and claimed within the hour
only by re-running ``mac task stats``.

**Nothing consumed.** Two ``mac.repo.update.result.v1`` replies WERE addressed
to a registered CLI session (03:15Z and 03:21Z), opened and closed within ~20ms,
and were never read -- the only consumer the bus shipped was ``agentbus wait``,
which blocks, and an interactive session has no background slot to block in.

So the assertions here are the two halves of one delivery: a committed
transition produces a bus record addressed to the task's owner, and that owner
can retrieve it with a call that returns immediately.
"""

from __future__ import annotations

import pytest

from mac.agentbus_service import AgentBusService
from mac.models import TaskState
from mac.services import ControlPlane
from mac.task_lifecycle_bus import (
    TASK_LIFECYCLE_SCHEMA,
    TASK_LIFECYCLE_TOPIC_PREFIX,
    TASK_LIFECYCLE_TOPICS,
    TASK_WATCHERS_METADATA_KEY,
    lifecycle_recipients,
    lifecycle_stream_id,
    lifecycle_topic,
    task_watchers,
)


@pytest.fixture
def fleet():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    owner = cp.register_agent(machine.id, "worker-1")
    watcher = cp.register_agent(machine.id, "operator-session")
    bystander = cp.register_agent(machine.id, "worker-2")
    return cp, owner, watcher, bystander


def _owned_task(cp, owner, *, watchers=None, title="ship the thing"):
    """A claimed task plus the lease fence every public transition requires."""
    metadata = {TASK_WATCHERS_METADATA_KEY: list(watchers)} if watchers else None
    task = cp.create_task(title, project="mac", metadata=metadata)
    _claimed, lease = cp.claim_task(task.id, owner.id)
    return cp.get_task(task.id), lease.id


def _move(cp, task_id, state, *, actor, lease_id=None, detail=None):
    """Transition a task the way the fleet does.

    Worker-authored transitions go through the public, lease-fenced path (the
    actor must hold the current lease, which is why these pass ``owner.id``
    rather than a prose actor name); hub-authored ones use the trusted internal
    path, because ``human``/``allocator`` hold no lease and are exactly the
    transitions an operator has no other way to hear about.
    """
    if lease_id:
        return cp.transition_task(task_id, state, actor, detail, lease_id=lease_id)
    return cp._transition_task_internal(task_id, state, actor, detail)


def _lifecycle(cp, agent_id):
    """Lifecycle payloads sitting in ``agent_id``'s inbox, oldest first."""
    return [
        chunk.payload
        for chunk in cp.read_agentbus_inbox(agent_id, "", limit=100)
        if isinstance(chunk.payload, dict) and chunk.payload.get("schema") == TASK_LIFECYCLE_SCHEMA
    ]


# --- the vocabulary -------------------------------------------------------


def test_every_task_state_has_a_topic_and_no_state_is_missed():
    """Derived, not hand-mapped: a new state cannot ship without a topic.

    A map maintained by hand drifts the first time a state is added, and the
    failure is silent -- consumers subscribe to a topic that is never published.
    """
    assert set(TASK_LIFECYCLE_TOPICS) == {state.value for state in TaskState}
    assert all(
        topic.startswith(TASK_LIFECYCLE_TOPIC_PREFIX) for topic in TASK_LIFECYCLE_TOPICS.values()
    )
    assert lifecycle_topic(TaskState.COMPLETED) == "task.completed.v1"
    assert lifecycle_topic("needs_review") == "task.needs_review.v1"


def test_an_unknown_state_is_refused_rather_than_minting_a_topic():
    from mac.models import ValidationError

    with pytest.raises(ValidationError):
        lifecycle_topic("definitely_not_a_state")


def test_watchers_are_read_leniently_and_recipients_are_owner_first():
    assert task_watchers({TASK_WATCHERS_METADATA_KEY: ["a", "a", " b ", ""]}) == ["a", "b"]
    assert task_watchers({TASK_WATCHERS_METADATA_KEY: "solo"}) == ["solo"]
    # Malformed metadata costs a missed notification, never an exception --
    # this runs inside a transition that has already committed.
    assert task_watchers({TASK_WATCHERS_METADATA_KEY: 17}) == []
    assert task_watchers(None) == []
    assert lifecycle_recipients("owner", ["w1", "owner"]) == ["owner", "w1"]
    assert lifecycle_recipients("owner", ["w1"], exclude=["owner"]) == ["w1"]


# --- publication ----------------------------------------------------------


def test_a_transition_puts_a_record_on_the_bus_addressed_to_the_owner(fleet):
    """The headline assertion: work moved, and the owner was told."""
    cp, owner, _watcher, _bystander = fleet
    task, lease = _owned_task(cp, owner)

    _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)

    payloads = _lifecycle(cp, owner.id)
    assert [item["to_state"] for item in payloads] == ["running"]
    record = payloads[0]
    assert record["task_id"] == task.id
    assert record["topic"] == "task.running.v1"
    assert record["owner_agent_id"] == owner.id
    assert record["actor"] == owner.id
    assert record["project"] == "mac"
    assert record["title"] == "ship the thing"


def test_the_record_is_a_real_stream_on_a_task_topic(fleet):
    """Addressed traffic, not a log line: topic, recipient and task binding."""
    cp, owner, _watcher, _bystander = fleet
    task, lease = _owned_task(cp, owner)

    _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)
    _move(
        cp,
        task.id,
        TaskState.FAILED.value,
        actor=owner.id,
        lease_id=lease,
        detail={"error": "the build fell over"},
    )

    streams = [
        stream
        for stream in cp.list_agentbus_streams(agent_id=owner.id, limit=100)
        if str(stream.topic).startswith(TASK_LIFECYCLE_TOPIC_PREFIX)
    ]
    # ``list_agentbus_streams`` is newest-first; the set is what matters here.
    assert {stream.topic for stream in streams} == {"task.running.v1", "task.failed.v1"}
    stream = next(item for item in streams if item.topic == "task.failed.v1")
    assert stream.recipient_agent_id == owner.id
    assert stream.task_id == task.id
    # One-shot: nothing is expected to reply to a lifecycle notification, so
    # the sender finalizes it rather than leaving an open channel per
    # transition on the busiest table the bus has.
    assert stream.status == "closed"


def test_watchers_are_addressed_alongside_the_owner(fleet):
    """The operator's half: file a task, name yourself, hear about it."""
    cp, owner, watcher, bystander = fleet
    task, lease = _owned_task(cp, owner, watchers=[watcher.id])

    _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)

    assert [item["to_state"] for item in _lifecycle(cp, watcher.id)] == ["running"]
    assert [item["to_state"] for item in _lifecycle(cp, owner.id)] == ["running"]
    # An inbox is "who spoke to me". A fleet where every agent is woken by
    # every transition is worse than one that polls.
    assert _lifecycle(cp, bystander.id) == []


def test_an_unknown_watcher_costs_one_recipient_and_not_the_notification(fleet):
    cp, owner, watcher, _bystander = fleet
    task, lease = _owned_task(cp, owner, watchers=["agent_does_not_exist", watcher.id])

    _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)
    _move(
        cp,
        task.id,
        TaskState.FAILED.value,
        actor=owner.id,
        lease_id=lease,
        detail={"error": "the build fell over"},
    )

    assert [item["to_state"] for item in _lifecycle(cp, owner.id)] == ["running", "failed"]
    assert [item["to_state"] for item in _lifecycle(cp, watcher.id)] == ["running", "failed"]


def test_an_unowned_unwatched_task_still_publishes_a_fleet_fact(fleet):
    """No addressee still means a durable, retained fleet operation."""
    cp, owner, _watcher, _bystander = fleet
    task = cp.create_task("nobody's task", project="mac")

    _move(cp, task.id, TaskState.BLOCKED.value, actor="human", detail={"reason": "no capacity"})

    assert _lifecycle(cp, owner.id) == []
    assert [
        stream
        for stream in cp.list_agentbus_streams(limit=100)
        if str(stream.topic).startswith(TASK_LIFECYCLE_TOPIC_PREFIX)
    ] == []
    events = cp.read_agentbus_broadcasts(owner.id, event_types=["task.transitioned.v1"])
    assert events[-1]["task_id"] == task.id
    assert events[-1]["payload"]["to_state"] == "blocked"


def test_redelivering_the_outbox_row_does_not_duplicate_the_record(fleet):
    """The outbox is at-least-once; the stream id is derived so a retry collides."""
    cp, owner, _watcher, _bystander = fleet
    task, lease = _owned_task(cp, owner)
    _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)
    before = _lifecycle(cp, owner.id)
    assert len(before) == 1

    rows = cp.task_ledger.list_outbox(status="delivered", task_id=task.id, limit=10)
    lifecycle_rows = [row for row in rows if row.event_type == "task.lifecycle"]
    assert lifecycle_rows, "the transition should have enqueued a task.lifecycle row"
    result = cp.publish_task_lifecycle_event(
        outbox_id=lifecycle_rows[0].id,
        task=cp.get_task(task.id),
        actor=owner.id,
        from_state=lifecycle_rows[0].from_state,
        to_state=lifecycle_rows[0].to_state,
        detail=lifecycle_rows[0].detail,
    )

    assert result["status"] == "duplicate"
    assert _lifecycle(cp, owner.id) == before


def test_an_empty_stream_from_a_half_written_publish_is_finished_not_skipped(fleet):
    """Opening the stream and appending its chunk are two writes.

    A failure between them leaves an EMPTY stream under an id derived from the
    outbox row -- an id that can never be re-opened. Reporting that as a
    duplicate would strand the notification forever, so the retry finishes it.
    """
    cp, owner, _watcher, _bystander = fleet
    task, lease = _owned_task(cp, owner)
    _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)
    rows = cp.task_ledger.list_outbox(status="delivered", task_id=task.id, limit=10)
    row = next(item for item in rows if item.event_type == "task.lifecycle")

    # Simulate the half-written state: same derived id, no chunk.
    orphan_id = lifecycle_stream_id("tout_orphan_%s" % row.id)
    persona = cp._ensure_operator_persona()
    cp.agentbus.open_stream(persona.id, owner.id, stream_id=orphan_id, topic="task.running.v1")
    before = len(_lifecycle(cp, owner.id))

    result = cp.publish_task_lifecycle_event(
        outbox_id="tout_orphan_%s" % row.id,
        task=cp.get_task(task.id),
        actor=owner.id,
        from_state="claimed",
        to_state="running",
        detail=row.detail,
    )

    assert result["status"] == "published"
    assert len(_lifecycle(cp, owner.id)) == before + 1


def test_an_oversized_detail_bag_costs_the_bag_and_not_the_notification(fleet):
    """``publish`` swallows failures, so a payload the bus would refuse is silent.

    Detail is the least load-bearing field -- state, actor and task id carry
    the meaning -- so it is what gets trimmed.
    """
    cp, owner, _watcher, _bystander = fleet
    task, _lease = _owned_task(cp, owner)

    result = cp.publish_task_lifecycle_event(
        outbox_id="tout_big_%s" % task.id,
        task=cp.get_task(task.id),
        actor=owner.id,
        from_state="claimed",
        to_state="running",
        detail={"stdout": "x" * (256 * 1024), "error": "boom"},
    )

    assert result["status"] == "published"
    record = _lifecycle(cp, owner.id)[-1]
    assert record["to_state"] == "running"
    assert record["detail"]["omitted"]
    assert record["detail"]["keys"] == ["error", "stdout"]


def test_a_bus_failure_does_not_fail_a_committed_transition(fleet, monkeypatch):
    """A transition that committed must not be undone by a notification.

    The transition outbox also advances dependency resolution and workflow
    runs; an exception escaping the publisher would stall every row behind it.
    """
    cp, owner, _watcher, _bystander = fleet
    task, lease = _owned_task(cp, owner)

    def explode(*_args, **_kwargs):
        raise RuntimeError("bus is down")

    monkeypatch.setattr(cp.agentbus, "open_stream", explode)
    moved = _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)

    assert moved.state == "running"
    assert cp.get_task(task.id).state == "running"
    assert _lifecycle(cp, owner.id) == []


# --- non-blocking retrieval ----------------------------------------------


def test_the_owner_retrieves_the_record_without_blocking(fleet):
    """The other half of the defect: addressed IS NOT delivered until read.

    ``drain`` is the call an interactive session can afford between turns. It
    returns whether or not anything was waiting -- there is no timeout
    argument to get wrong and no channel held open.
    """
    cp, owner, _watcher, _bystander = fleet
    task, lease = _owned_task(cp, owner)
    _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)

    drained = cp.drain_agentbus_inbox(owner.id)

    assert drained["count"] == 1
    assert drained["committed"] is True
    message = drained["messages"][0]
    assert message["payload"]["task_id"] == task.id
    assert message["payload"]["to_state"] == "running"
    assert message["inbox_cursor"] == drained["next_cursor"]


def test_drain_is_not_repeated_and_needs_no_caller_held_cursor(fleet):
    """The consumed position lives at the hub, which is what ``wait`` lacked.

    ``agentbus wait --after-cursor`` requires the caller to carry its own
    bookmark between invocations. A session that exits between turns has
    nowhere to keep one, so it either re-reads everything or misses messages.
    """
    cp, owner, _watcher, _bystander = fleet
    task, lease = _owned_task(cp, owner)
    _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)

    assert cp.drain_agentbus_inbox(owner.id)["count"] == 1
    assert cp.drain_agentbus_inbox(owner.id)["count"] == 0

    _move(
        cp,
        task.id,
        TaskState.FAILED.value,
        actor=owner.id,
        lease_id=lease,
        detail={"error": "the build fell over"},
    )
    second = cp.drain_agentbus_inbox(owner.id)
    assert second["count"] == 1
    assert second["messages"][0]["payload"]["to_state"] == "failed"


def test_a_peek_reads_without_consuming(fleet):
    cp, owner, _watcher, _bystander = fleet
    task, lease = _owned_task(cp, owner)
    _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)

    peeked = cp.drain_agentbus_inbox(owner.id, commit=False)
    assert peeked["count"] == 1
    assert peeked["committed"] is False
    assert cp.drain_agentbus_inbox(owner.id)["count"] == 1


def test_the_pending_count_answers_without_reading_the_messages(fleet):
    """What ordinary CLI output carries, so an operator never has to ask."""
    cp, owner, _watcher, _bystander = fleet
    task, lease = _owned_task(cp, owner)

    assert cp.pending_agentbus_inbox_count(owner.id)["count"] == 0

    _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)
    _move(
        cp,
        task.id,
        TaskState.FAILED.value,
        actor=owner.id,
        lease_id=lease,
        detail={"error": "the build fell over"},
    )

    pending = cp.pending_agentbus_inbox_count(owner.id)
    assert pending["count"] == 2
    assert pending["capped"] is False
    assert pending["oldest_at"] and pending["newest_at"]

    cp.drain_agentbus_inbox(owner.id)
    assert cp.pending_agentbus_inbox_count(owner.id)["count"] == 0


def test_draining_leaves_the_stream_intact_for_a_reply(fleet):
    """A recipient must not destroy the channel it is expected to answer on.

    ``agentbus_request`` waits on the SENDER closing the stream. Had drain
    closed what it read, the first agent to use it would have broken every
    request/reply exchange addressed to it.
    """
    cp, owner, _watcher, bystander = fleet
    stream = cp.agentbus.open_stream(bystander.id, owner.id, stream_id="ask-1")
    cp.agentbus.append_chunk(stream.id, bystander.id, {"text": "are you on this branch?"})

    assert cp.drain_agentbus_inbox(owner.id)["count"] == 1

    assert cp.get_agentbus_stream(stream.id).status == "open"
    cp.agentbus.append_chunk(stream.id, bystander.id, {"text": "still there?"})
    assert cp.drain_agentbus_inbox(owner.id)["count"] == 1


def test_the_durable_cursor_matches_the_wait_cursor_format(fleet):
    """``drain`` and ``wait`` share one cursor, so the two can be interleaved."""
    cp, owner, _watcher, bystander = fleet
    stream = cp.agentbus.open_stream(bystander.id, owner.id, stream_id="s-fmt")
    cp.agentbus.append_chunk(stream.id, bystander.id, {"text": "hello"})

    chunk = cp.read_agentbus_inbox(owner.id, "", limit=1)[0]
    drained = cp.drain_agentbus_inbox(owner.id)

    assert drained["next_cursor"] == cp.agentbus_inbox_cursor(chunk)
    assert cp.agentbus.durable_inbox_cursor(owner.id) == drained["next_cursor"]
    assert AgentBusService.INBOX_CURSOR_SEPARATOR in drained["next_cursor"]
    # A stale explicit cursor cannot replay an already committed batch.
    assert cp.drain_agentbus_inbox(owner.id, "")["conflict"] is True


def test_a_stale_drain_cannot_replay_or_move_the_durable_cursor_back(fleet):
    cp, owner, _watcher, bystander = fleet
    stream = cp.agentbus.open_stream(bystander.id, owner.id, stream_id="s-cas")
    cp.agentbus.append_chunk(stream.id, bystander.id, {"text": "hello"})

    first = cp.drain_agentbus_inbox(owner.id)
    stale = cp.drain_agentbus_inbox(owner.id, "")

    assert first["count"] == 1
    assert stale["count"] == 0
    assert stale["conflict"] is True
    assert cp.agentbus.durable_inbox_cursor(owner.id) == first["next_cursor"]


# --- the HTTP surface a hub-mode agent actually reaches -------------------


def test_the_non_blocking_routes_are_self_only():
    """Both halves carry the agent scope, not the generic read/write scopes.

    Left to the suffix rules, ``pending`` would have been an ordinary GET
    (``read``) and ``drain`` an ordinary POST (``write``). Any read token in
    the fleet could then count another agent's messages, and any write token
    could advance another agent's consumed position -- which does not just
    disclose, it CONSUMES: the victim never sees what was taken.
    """
    from mac.api import _required_scope

    assert _required_scope("GET", "/agents/a1/agentbus/inbox/pending") == "agent"
    assert _required_scope("POST", "/agents/a1/agentbus/inbox/drain") == "agent"


def test_an_agent_drains_its_own_inbox_over_http_and_nobody_elses(fleet):
    from fastapi.testclient import TestClient

    from mac.api import create_app

    cp, owner, _watcher, bystander = fleet
    task, lease = _owned_task(cp, owner)
    _move(cp, task.id, TaskState.RUNNING.value, actor=owner.id, lease_id=lease)

    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "mine": {"scopes": ["agent"], "agent_id": owner.id},
                "other": {"scopes": ["agent"], "agent_id": bystander.id},
                "reader": {"scopes": ["read"], "client_id": "dash"},
            },
        )
    )
    pending_url = "/agents/%s/agentbus/inbox/pending" % owner.id
    drain_url = "/agents/%s/agentbus/inbox/drain" % owner.id

    for token in ("other", "reader"):
        headers = {"Authorization": "Bearer %s" % token}
        assert client.get(pending_url, headers=headers).status_code == 403
        assert client.post(drain_url, headers=headers).status_code == 403

    mine = {"Authorization": "Bearer mine"}
    pending = client.get(pending_url, headers=mine)
    assert pending.status_code == 200
    assert pending.json()["count"] == 1

    drained = client.post(drain_url, headers=mine)
    assert drained.status_code == 200
    body = drained.json()
    assert body["count"] == 1
    assert body["messages"][0]["payload"]["to_state"] == "running"
    # Consumed: the hub remembers, so the caller does not have to.
    assert client.get(pending_url, headers=mine).json()["count"] == 0


def test_hub_mode_can_read_an_inbox_at_all(fleet):
    """The defect that made every other fix unreachable.

    ``RemoteDispatch`` wrapped the whole agentbus surface except the inbox, so
    ``mac admin agentbus wait`` -- the only consumer the bus shipped -- answered
    "not yet supported in hub mode" for every agent in the fleet, because hub
    mode is the only mode a fleet agent runs in.
    """
    from mac.dispatch import RemoteDispatch

    for name in (
        "read_agentbus_inbox",
        "drain_agentbus_inbox",
        "pending_agentbus_inbox_count",
    ):
        assert callable(getattr(RemoteDispatch, name, None)), name


def test_first_class_task_and_agent_operations_reach_broadcast(fleet):
    cp, owner, watcher, _bystander = fleet

    task = cp.create_task("observable work", project="mac")
    cp.update_task(task.id, priority=7)
    cp.claim_task(task.id, owner.id)
    cp.set_agent_dispatch_hold(watcher.id, "operator session")
    cp.clear_agent_dispatch_hold(watcher.id)
    cp.heartbeat_agent(watcher.id, health_status="degraded")
    cp.delete_agent(watcher.id, actor="test")

    event_types = [
        event["event_type"] for event in cp.read_agentbus_broadcasts(owner.id, limit=100)
    ]
    assert "task.created.v1" in event_types
    assert "task.claimed.v1" in event_types
    assert "task.updated.v1" in event_types
    assert "agent.held.v1" in event_types
    assert "agent.resumed.v1" in event_types
    assert "agent.heartbeat.v1" in event_types
    assert "agent.left.v1" in event_types


def test_the_cli_reads_one_cursor_in_either_transport(fleet):
    """`wait` and `drain` must agree, or interleaving them loses messages."""
    from mac.cli import _inbox_cursor

    cp, owner, _watcher, bystander = fleet
    stream = cp.agentbus.open_stream(bystander.id, owner.id, stream_id="s-cli")
    cp.agentbus.append_chunk(stream.id, bystander.id, {"text": "hello"})

    local_chunk = cp.read_agentbus_inbox(owner.id, "", limit=1)[0]
    hub_shaped = cp.drain_agentbus_inbox(owner.id, "", commit=False)["messages"][0]

    assert _inbox_cursor(cp, local_chunk) == _inbox_cursor(cp, hub_shaped)
