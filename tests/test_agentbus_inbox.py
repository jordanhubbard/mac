"""An agent's inbox: "has anyone said anything to me", across every stream.

AgentBus already answers "what is new in this conversation" (`read_chunks`),
which requires knowing the stream id. An agent that is *mid-task* does not know
which stream a correction will arrive on — that is the whole point of a
correction — so it had no way to be reached while working. Messages surfaced
between tasks, not between work steps.

This is the other question, and it is what a working agent can watch.

Membership is the rule the bus already enforces: direct recipient, or a member
of a group stream. Two exclusions matter and are asserted, because getting
either wrong makes the watcher useless rather than merely wrong:

* an agent's own messages — a watcher woken by its own writes would spin; and
* other agents' conversations — an inbox that leaked them would turn a
  coordination primitive into a surveillance one.
"""

from __future__ import annotations

import pytest

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


def _texts(chunks):
    return [chunk.payload["text"] for chunk in chunks]


def test_a_direct_message_reaches_the_recipients_inbox(fleet):
    cp, a, b, _c = fleet
    _say(cp, b, a, "stop, the schema changed", stream_id="s1")
    assert _texts(cp.read_agentbus_inbox(a.id)) == ["stop, the schema changed"]


def test_a_group_message_reaches_every_member(fleet):
    cp, a, b, c = fleet
    _say(cp, c, None, "team: rebase first", stream_id="s2",
         participants=[a.id, b.id])
    assert _texts(cp.read_agentbus_inbox(a.id)) == ["team: rebase first"]
    assert _texts(cp.read_agentbus_inbox(b.id)) == ["team: rebase first"]


def test_an_agent_is_not_woken_by_its_own_messages(fleet):
    """A watcher that woke on its own writes would spin instead of working."""
    cp, a, b, _c = fleet
    _say(cp, a, b, "a talking", stream_id="s-own")
    assert cp.read_agentbus_inbox(a.id) == []


def test_another_pairs_conversation_is_invisible(fleet):
    """An inbox that leaked these would be surveillance, not coordination."""
    cp, a, b, c = fleet
    _say(cp, b, c, "private to c", stream_id="s3")
    assert cp.read_agentbus_inbox(a.id) == []
    assert _texts(cp.read_agentbus_inbox(c.id)) == ["private to c"]


def test_a_group_stream_does_not_leak_to_non_members(fleet):
    cp, a, b, c = fleet
    _say(cp, b, None, "b and c only", stream_id="s4", participants=[c.id])
    assert cp.read_agentbus_inbox(a.id) == []
    assert _texts(cp.read_agentbus_inbox(c.id)) == ["b and c only"]


def test_a_substring_agent_id_does_not_match_by_accident(fleet):
    """Membership is stored as JSON and prefiltered with LIKE for speed. The
    exact check afterwards is what stops `agent_1` matching `agent_12`."""
    cp, a, b, _c = fleet
    machine = cp.register_machine("host-2")
    impostor = cp.register_agent(machine.id, "worker-1-extra")
    _say(cp, b, None, "members only", stream_id="s5", participants=[impostor.id])
    assert cp.read_agentbus_inbox(a.id) == []
    assert _texts(cp.read_agentbus_inbox(impostor.id)) == ["members only"]


# --- the cursor is what makes a restarted watcher safe --------------------


def test_the_cursor_resumes_exactly_where_it_stopped(fleet):
    cp, a, b, _c = fleet
    _say(cp, b, a, "first", stream_id="s1")
    inbox = cp.read_agentbus_inbox(a.id)
    cursor = cp.agentbus_inbox_cursor(inbox[-1])

    assert cp.read_agentbus_inbox(a.id, cursor) == []

    stream = cp.agentbus.get_stream("s1")
    cp.agentbus.append_chunk(stream.id, b.id, {"text": "second"})
    assert _texts(cp.read_agentbus_inbox(a.id, cursor)) == ["second"]


def test_messages_from_several_streams_interleave_by_time(fleet):
    """`sequence` is per-stream, so ordering by it would interleave two
    conversations incorrectly. The inbox orders by (created_at, id)."""
    cp, a, b, c = fleet
    _say(cp, b, a, "from b", stream_id="s1")
    _say(cp, c, a, "from c", stream_id="s2")
    assert _texts(cp.read_agentbus_inbox(a.id)) == ["from b", "from c"]


def test_an_empty_cursor_returns_the_whole_backlog(fleet):
    cp, a, b, _c = fleet
    _say(cp, b, a, "one", stream_id="s1")
    _say(cp, b, a, "two", stream_id="s2")
    assert len(cp.read_agentbus_inbox(a.id, "")) == 2


def test_a_malformed_cursor_does_not_hide_messages(fleet):
    """A watcher restarted with a corrupted cursor must over-deliver, never
    under-deliver: a missed correction is the failure that matters."""
    cp, a, b, _c = fleet
    _say(cp, b, a, "important", stream_id="s1")
    assert _texts(cp.read_agentbus_inbox(a.id, "not-a-cursor")) == ["important"]


def test_the_limit_is_bounded(fleet):
    cp, a, b, _c = fleet
    stream = cp.agentbus.open_stream(b.id, a.id, stream_id="s1")
    for index in range(5):
        cp.agentbus.append_chunk(stream.id, b.id, {"text": str(index)})
    assert len(cp.read_agentbus_inbox(a.id, "", limit=2)) == 2
    # Absurd limits are clamped rather than honoured.
    assert len(cp.read_agentbus_inbox(a.id, "", limit=10_000)) == 5


# --- self-only over HTTP --------------------------------------------------


def test_the_inbox_endpoint_is_self_only():
    from mac.api import _required_scope

    assert _required_scope("GET", "/agents/a1/agentbus/inbox") == "agent"


def test_an_agent_cannot_watch_another_agents_inbox(fleet):
    from fastapi.testclient import TestClient

    from mac.api import create_app

    cp, a, b, _c = fleet
    _say(cp, b, a, "for a only", stream_id="s1")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "mine": {"scopes": ["agent"], "agent_id": a.id},
                "other": {"scopes": ["agent"], "agent_id": b.id},
                "reader": {"scopes": ["read"], "client_id": "dash"},
            },
        )
    )
    url = "/agents/%s/agentbus/inbox?timeout_seconds=1" % a.id
    assert client.get(url, headers={"Authorization": "Bearer other"}).status_code == 403
    assert client.get(url, headers={"Authorization": "Bearer reader"}).status_code == 403

    ok = client.get(url, headers={"Authorization": "Bearer mine"})
    assert ok.status_code == 200
    assert "for a only" in ok.text
