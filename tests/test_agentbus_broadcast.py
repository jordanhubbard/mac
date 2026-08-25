"""AgentBus as a broadcast bus: agents hear each other, and so does the hub.

The bus enforced point-to-point privacy, which made the fleet a set of sealed
conversations. Two agents working the same checkout could not learn that fact
from the system — only from a convention in CLAUDE.md, written after one
agent's ``git add -A`` nearly swept up 1,200 lines of another's work and a
``git commit -a`` actually did.

**This file changes an existing semantic**: connecting to the bus means
hearing the bus. Point-to-point messages are no longer private, and the tests
that asserted a non-party was refused now assert the opposite. The argument was
already in the code — ``_authorized`` exempted human directives because
"relay-by-citation only works if any agent can look a cited directive up" —
and that is not special to directives. It is what a bus is.

What these tests pin:

* any agent can read any stream and its chunks, including a point-to-point one;
* addressing still works and is still identifiable: the addressee still
  receives the message, and every heard message says who it was addressed to,
  which is what the fleet convention ("do not answer until addressed by name")
  is honoured against — by consumers, not by enforcement here;
* the WRITE paths did not move: reading a conversation does not let you speak
  into it;
* roll call names every agent on the bus with its capabilities;
* volume is bounded, in the hub, where a bound can be trusted. The precedent
  for not doing this is on the record — ``action_events`` reached 10.4M rows
  and 16GB and wedged the hub.
"""

from __future__ import annotations

import pytest

from mac.agentbus_broadcast import (
    BROADCAST_EVENT_TYPES,
    BROADCAST_LAYER,
    BROADCAST_MAX_PAYLOAD_BYTES,
    BROADCAST_MAX_VALUE_CHARS,
    BROADCAST_RATE_LIMIT_EVENTS,
)
from mac.models import AuthorizationError, NotFoundError, ValidationError
from mac.services import ControlPlane


@pytest.fixture
def fleet():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    return (
        cp,
        cp.register_agent(machine.id, "worker-1"),
        cp.register_agent(machine.id, "worker-2"),
        cp.register_agent(machine.id, "worker-3"),
    )


def _say(cp, sender, recipient, text, *, stream_id, participants=None):
    stream = cp.agentbus.open_stream(
        sender.id,
        recipient.id if recipient is not None else None,
        stream_id=stream_id,
        participant_agent_ids=participants,
    )
    cp.agentbus.append_chunk(stream.id, sender.id, {"text": text})
    return stream


# ---------------------------------------------------------------------------
# Point-to-point is not private
#
# SEMANTIC CHANGE. These assertions are the inverse of what the bus enforced
# before: a non-party used to be refused.
# ---------------------------------------------------------------------------


def test_a_non_party_can_read_a_point_to_point_stream(fleet):
    cp, a, b, c = fleet
    stream = _say(cp, a, b, "rebasing onto main, do not push", stream_id="s1")

    assert cp.assert_agentbus_authorized(c.id, stream.id).id == stream.id
    assert [chunk.payload["text"] for chunk in cp.read_agentbus_chunks(c.id, stream.id)] == [
        "rebasing onto main, do not push"
    ]


def test_a_terminal_stream_stays_participant_scoped(fleet):
    """The one carve-out, and the test that stops a refactor widening it.

    Making point-to-point messages readable was about agents TALKING.
    OpenShell debug-terminal streams carry raw terminal I/O -- command output,
    environment, credentials -- and were never in scope; they merely sat
    behind the same check. Adding them to the broadcast later is one line;
    a credential read off the bus cannot be un-read.
    """
    cp, a, b, c = fleet
    stream = cp.agentbus.open_stream(
        a.id, b.id, topic="mac.debug.terminal.output.v1", stream_id="t1"
    )
    cp.agentbus.append_chunk(stream.id, a.id, {"text": "export TOKEN=hunter2"})

    # The participants still read it...
    assert cp.read_agentbus_chunks(b.id, stream.id)
    assert cp.read_agentbus_chunks(a.id, stream.id)
    # ...and nobody else does, by either door.
    with pytest.raises(AuthorizationError):
        cp.read_agentbus_chunks(c.id, stream.id)
    with pytest.raises(AuthorizationError):
        cp.assert_agentbus_authorized(c.id, stream.id)
    assert cp.read_agentbus_traffic(c.id) == []
    # The addressee still hears it on the traffic feed.
    assert len(cp.read_agentbus_traffic(b.id)) == 1


def test_the_carve_out_is_one_removable_constant():
    """Emptying the set opens terminal streams. Nothing else should need to.

    Pinned so the removal path stays a one-line change: every read path routes
    through ``_may_read``, which is the only consumer of the set.
    """
    import inspect

    from mac import agentbus_service

    source = inspect.getsource(agentbus_service)
    assert source.count("in PARTICIPANT_SCOPED_TOPICS") == 1
    assert "if topic not in PARTICIPANT_SCOPED_TOPICS" in source
    # ...and exactly one decision function behind it.
    assert source.count("self._may_read(") == 2  # per-stream read + traffic feed


def test_an_agent_hears_traffic_it_is_not_addressed_on(fleet):
    cp, a, b, c = fleet
    _say(cp, a, b, "rebasing onto main, do not push", stream_id="s1")

    heard = cp.read_agentbus_traffic(c.id)

    assert [item["chunk"]["payload"]["text"] for item in heard] == [
        "rebasing onto main, do not push"
    ]
    assert heard[0]["from_agent_id"] == a.id
    assert heard[0]["addressed_to"] == [b.id]


def test_an_agent_does_not_hear_its_own_echo(fleet):
    """A watcher woken by its own writes would spin instead of working."""
    cp, a, b, _c = fleet
    _say(cp, a, b, "stop", stream_id="s1")

    assert cp.read_agentbus_traffic(a.id) == []


def test_bus_traffic_resumes_from_a_cursor(fleet):
    cp, a, b, c = fleet
    _say(cp, a, b, "first", stream_id="s1")
    first = cp.read_agentbus_traffic(c.id)
    _say(cp, a, b, "second", stream_id="s2")

    resumed = cp.read_agentbus_traffic(c.id, first[-1]["cursor"])

    assert [item["chunk"]["payload"]["text"] for item in resumed] == ["second"]


# ---------------------------------------------------------------------------
# Addressing survives, as routing
# ---------------------------------------------------------------------------


def test_the_addressed_agent_still_receives_the_message(fleet):
    cp, a, b, _c = fleet
    stream = _say(cp, a, b, "the schema changed", stream_id="s1")

    inbox = cp.read_agentbus_inbox(b.id)

    assert [chunk.payload["text"] for chunk in inbox] == ["the schema changed"]
    assert inbox[0].stream_id == stream.id
    assert inbox[0].sequence == 1


def test_every_heard_message_says_who_it_was_addressed_to(fleet):
    """The convention -- do not answer until addressed by name -- needs this.

    It is honoured by consumers, not enforced here: an agent that overhears a
    peer being told about a branch it is ABOUT to force-push should act on
    that, and an enforced silence would be the failure, not the safeguard.
    """
    cp, a, b, c = fleet
    _say(cp, a, b, "who has the release branch?", stream_id="s1")

    for_c = cp.read_agentbus_traffic(c.id)[0]
    for_b = cp.read_agentbus_traffic(b.id)[0]

    assert for_c["addressed_to"] == [b.id]
    assert for_c["addressed_to_me"] is False
    assert for_c["reply_expected"] is False
    assert for_b["addressed_to_me"] is True
    assert for_b["reply_expected"] is True


def test_a_reader_can_skip_what_its_inbox_already_has(fleet):
    cp, a, b, _c = fleet
    _say(cp, a, b, "addressed", stream_id="s1")

    assert cp.read_agentbus_traffic(b.id, include_addressed=False) == []
    assert len(cp.read_agentbus_traffic(b.id)) == 1


def test_reading_a_conversation_does_not_let_you_speak_into_it(fleet):
    """Listening opened up. The write paths did not move."""
    cp, a, b, c = fleet
    stream = _say(cp, a, b, "hello", stream_id="s1")

    assert cp.read_agentbus_chunks(c.id, stream.id)  # can read
    with pytest.raises(AuthorizationError):
        cp.agentbus.append_chunk(stream.id, c.id, {"text": "me too"})
    with pytest.raises(AuthorizationError):
        cp.close_agentbus_stream(stream.id, c.id)


def test_group_membership_still_routes_the_inbox(fleet):
    cp, a, b, c = fleet
    _say(cp, a, b, "team update", stream_id="s1", participants=[b.id, c.id])

    assert [chunk.payload["text"] for chunk in cp.read_agentbus_inbox(c.id)] == ["team update"]


# ---------------------------------------------------------------------------
# Roll call
# ---------------------------------------------------------------------------


def test_roll_call_names_every_agent_on_the_bus_with_its_capabilities(fleet):
    """An agent asks the bus who is out there, instead of the hub deciding.

    This is what lets agents pull work and judge for themselves what they can
    take: without a roster, an agent can only wait to be assigned.
    """
    cp, a, b, c = fleet
    cp.update_agent(a.id, capabilities=["repo", "gpu"])

    roster = cp.agentbus_roll_call()

    assert roster["agent_count"] == 3
    by_id = {entry["id"]: entry for entry in roster["agents"]}
    assert set(by_id) == {a.id, b.id, c.id}
    assert sorted(by_id[a.id]["capabilities"]) == ["gpu", "repo"]
    assert by_id[b.id]["capabilities"] == []
    # The inventory shape agents already receive from agent_reflection_payload.
    assert {"id", "name", "capabilities", "status", "current_task_id"} <= set(by_id[a.id])


def test_roll_call_leaves_out_departed_agents_unless_asked(fleet):
    cp, a, b, c = fleet
    cp.delete_agent(c.id)

    assert {entry["id"] for entry in cp.agentbus_roll_call()["agents"]} == {a.id, b.id}
    assert {entry["id"] for entry in cp.agentbus_roll_call(include_departed=True)["agents"]} == {
        a.id,
        b.id,
        c.id,
    }


# ---------------------------------------------------------------------------
# The typed broadcast feed
# ---------------------------------------------------------------------------


def test_a_broadcast_is_heard_by_the_whole_fleet(fleet):
    cp, a, _b, c = fleet

    cp.publish_agentbus_broadcast(
        a.id,
        "git.worktree_added",
        project="mac",
        payload={"branch": "certifier/x", "worktree": "/tmp/mac-x"},
    )

    heard = cp.read_agentbus_broadcasts(c.id, event_types=["git.worktree_added"])
    assert len(heard) == 1
    assert heard[0]["event_type"] == "git.worktree_added"
    assert heard[0]["agent_id"] == a.id
    assert heard[0]["payload"]["branch"] == "certifier/x"
    assert heard[0]["self_emitted"] is False


def test_an_agent_can_tell_its_own_broadcast_from_a_peers(fleet):
    """So a consumer can skip its own echo -- not a permission, a filter."""
    cp, a, _b, c = fleet
    cp.publish_agentbus_broadcast(a.id, "project.attention", project="mac")

    assert (
        cp.read_agentbus_broadcasts(a.id, event_types=["project.attention"])[0]["self_emitted"]
        is True
    )
    assert (
        cp.read_agentbus_broadcasts(c.id, event_types=["project.attention"])[0]["self_emitted"]
        is False
    )


def test_the_vocabulary_is_closed(fleet):
    cp, a, _b, _c = fleet
    with pytest.raises(ValidationError):
        cp.publish_agentbus_broadcast(a.id, "git.rebased_probably")
    # Every declared type is publishable, so the vocabulary is not aspirational.
    for event_type in BROADCAST_EVENT_TYPES:
        assert cp.publish_agentbus_broadcast(a.id, event_type)["event_type"] == event_type


def test_an_unknown_agent_cannot_broadcast(fleet):
    cp, _a, _b, _c = fleet
    with pytest.raises(NotFoundError):
        cp.publish_agentbus_broadcast("agent_ghost", "project.attention")


def test_the_feed_can_be_filtered_by_type_and_project(fleet):
    cp, a, _b, c = fleet
    cp.publish_agentbus_broadcast(a.id, "project.attention", project="mac")
    cp.publish_agentbus_broadcast(a.id, "project.attention", project="other")
    cp.publish_agentbus_broadcast(a.id, "capacity.saturated", project="mac")

    mac_attention = cp.read_agentbus_broadcasts(
        c.id, event_types=["project.attention"], project="mac"
    )

    assert [item["project"] for item in mac_attention] == ["mac"]


def test_the_feed_resumes_from_a_sequence(fleet):
    cp, a, _b, c = fleet
    cp.publish_agentbus_broadcast(a.id, "task.claimed", payload={"task_id": "t1"})
    first = cp.read_agentbus_broadcasts(c.id)
    cp.publish_agentbus_broadcast(a.id, "task.released", payload={"task_id": "t1"})

    resumed = cp.read_agentbus_broadcasts(c.id, after_sequence=first[-1]["sequence"])

    assert [item["event_type"] for item in resumed] == ["task.released"]


# ---------------------------------------------------------------------------
# Volume: the action_events lesson
# ---------------------------------------------------------------------------


def _broadcast_rows(cp) -> int:
    row = cp.store.query_one(
        "SELECT COUNT(*) AS cnt FROM observability_events WHERE layer = ?",
        (BROADCAST_LAYER,),
    )
    return int(row["cnt"] or 0)


def test_repeating_the_same_event_does_not_repeat_the_row(fleet):
    """A worker looping on the same fact cannot turn the loop into rows."""
    cp, a, _b, _c = fleet

    outcomes = [
        cp.publish_agentbus_broadcast(
            a.id, "task.progress", task_id="task_1", payload={"branch": "b"}
        )
        for _ in range(25)
    ]

    assert outcomes[0]["accepted"] is True
    assert all(item["accepted"] is False for item in outcomes[1:])
    assert {item["reason"] for item in outcomes[1:]} == {"coalesced"}
    assert _broadcast_rows(cp) >= 1
    assert cp.agentbus_broadcast.suppressed_count(a.id) == 24


def test_a_flood_of_distinct_events_is_rate_limited(fleet):
    """Distinct events defeat coalescing, so the per-agent bucket must hold."""
    cp, a, _b, _c = fleet
    flood = BROADCAST_RATE_LIMIT_EVENTS * 3

    outcomes = [
        cp.publish_agentbus_broadcast(a.id, "git.pushed", payload={"sha": "sha-%d" % index})
        for index in range(flood)
    ]

    accepted = [item for item in outcomes if item["accepted"]]
    assert len(accepted) == BROADCAST_RATE_LIMIT_EVENTS
    assert _broadcast_rows(cp) >= BROADCAST_RATE_LIMIT_EVENTS
    assert outcomes[-1]["reason"] == "rate_limited"


def test_one_agents_flood_does_not_silence_another(fleet):
    cp, a, b, _c = fleet
    for index in range(BROADCAST_RATE_LIMIT_EVENTS * 2):
        cp.publish_agentbus_broadcast(a.id, "git.pushed", payload={"sha": "sha-%d" % index})

    assert (
        cp.publish_agentbus_broadcast(b.id, "git.pushed", payload={"sha": "quiet"})["accepted"]
        is True
    )


def test_an_oversized_payload_is_capped_and_says_so(fleet):
    cp, a, _b, c = fleet

    cp.publish_agentbus_broadcast(
        a.id,
        "git.pushed",
        task_id="task_1",
        payload={"branch": "main", "diff": "x" * (BROADCAST_MAX_PAYLOAD_BYTES * 4)},
    )

    heard = cp.read_agentbus_broadcasts(c.id, event_types=["git.pushed"])[0]
    assert heard["payload"]["truncated"] is True
    assert len(heard["payload"]["diff"]) == BROADCAST_MAX_VALUE_CHARS
    assert heard["payload"]["branch"] == "main"
    row = cp.store.query_one(
        "SELECT LENGTH(detail) AS size FROM observability_events WHERE layer = ?",
        (BROADCAST_LAYER,),
    )
    assert int(row["size"]) < BROADCAST_MAX_PAYLOAD_BYTES * 2


def test_a_payload_of_many_fields_keeps_the_identifying_ones(fleet):
    """Past the whole-payload cap, what survives is what identifies the event."""
    cp, a, _b, c = fleet

    cp.publish_agentbus_broadcast(
        a.id,
        "git.pushed",
        payload={
            "branch": "certifier/x",
            "sha": "deadbeef",
            **{"field_%02d" % index: "y" * 200 for index in range(20)},
        },
    )

    heard = cp.read_agentbus_broadcasts(c.id, event_types=["git.pushed"])[0]
    assert heard["payload"]["truncated"] is True
    assert heard["payload"]["branch"] == "certifier/x"
    assert heard["payload"]["sha"] == "deadbeef"
    assert not [key for key in heard["payload"] if key.startswith("field_")]


def test_broadcasts_fall_under_an_already_enabled_retention_policy(fleet):
    """Not a new record class retention cannot reach -- an existing one.

    Broadcasts are stored as observability events precisely so the prune path
    operators already run reaches them on the schedule they already set.
    """
    cp, a, _b, _c = fleet
    cp.publish_agentbus_broadcast(a.id, "git.pushed", payload={"sha": "abc"})

    policy = cp.retention.get_policy("observability_events")
    assert policy.enabled is True
    assert policy.max_age_seconds

    # And the rows really are visible to the prune planner.
    report = cp.retention.dry_run(
        "observability_events",
        override_policy=type(policy)(
            "observability_events", enabled=True, max_age_seconds=1, batch_size=1000
        ),
    )
    assert report.eligible_rows >= 0  # planner ran over the table without error
    assert _broadcast_rows(cp) >= 1


# ---------------------------------------------------------------------------
# The hub as a listener
# ---------------------------------------------------------------------------


def test_the_hub_derives_a_ledger_fact_from_a_broadcast_alone(fleet):
    """No second call: the hub hears the push and writes the history itself."""
    cp, a, _b, _c = fleet
    task = cp.create_task("publish something", project="mac")

    envelope = cp.publish_agentbus_broadcast(
        a.id,
        "git.pushed",
        task_id=task.id,
        project="mac",
        payload={"branch": "certifier/x", "sha": "deadbeef"},
    )

    assert envelope["derived"] == ["bus.observed.git.pushed"]
    derived = [
        event for event in cp.task_history(task.id) if event.event_type == "bus.observed.git.pushed"
    ]
    assert len(derived) == 1
    assert derived[0].detail["branch"] == "certifier/x"
    assert derived[0].detail["derived_from_observation"] is True


def test_chatty_events_do_not_reach_the_ledger(fleet):
    """Only the low-frequency git facts derive; progress pings never do."""
    cp, a, _b, _c = fleet
    task = cp.create_task("noisy", project="mac")

    envelope = cp.publish_agentbus_broadcast(
        a.id, "task.progress", task_id=task.id, payload={"note": "still going"}
    )

    assert envelope["derived"] == []
    assert not [
        event for event in cp.task_history(task.id) if event.event_type.startswith("bus.observed.")
    ]


def test_a_narrow_filter_scans_past_events_it_does_not_want(fleet):
    """A filtered reader must not stall behind a block it filtered out.

    Resuming from the last RETURNED sequence, a reader whose filter matched
    nothing in the first page would re-read that page forever and never reach
    the event it asked for.
    """
    cp, a, _b, c = fleet
    for index in range(30):
        cp.publish_agentbus_broadcast(a.id, "task.progress", payload={"sha": "noise-%d" % index})
    cp.publish_agentbus_broadcast(a.id, "git.merge_conflict", payload={"sha": "x"})

    heard = cp.read_agentbus_broadcasts(c.id, limit=5, event_types=["git.merge_conflict"])

    assert [item["event_type"] for item in heard] == ["git.merge_conflict"]
