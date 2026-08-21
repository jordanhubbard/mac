"""`mac agentbus drain` / `mac agentbus pending` — the inbox for a session.

``wait`` blocks, and that is right for a working agent whose harness can run a
watcher in a background slot. An interactive CLI session registered as an agent
has no such slot, so for it the bus was write-only: two replies addressed to
such a session on 2026-08-21 were opened and closed within ~20ms and nothing
ever surfaced them.

These two verbs are the read that fits between turns. Three properties make
them usable that way and are asserted here:

* they **return at once** whether or not anything was waiting;
* the consumed position is **kept at the hub**, so a session that exits between
  turns and holds no cursor still sees each message exactly once; and
* draining **does not close** the streams it read, because on this bus the
  sender closes, and a request awaiting its reply would lose the channel it is
  waiting on.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main
from mac.test_support import control_plane_on, dsn_for


def _run(db, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        main(["--db", dsn_for(db), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return json.loads(raw) if raw else None


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_SECRET_KEY", "test-key-with-at-least-32-characters-x")
    cp = control_plane_on(dsn_for(tmp_path))
    machine = cp.register_machine("host")
    session = cp.register_agent(machine.id, "operator-session")
    peer = cp.register_agent(machine.id, "worker-1")
    return tmp_path, cp, session, peer


def _say(cp, sender, recipient, text, stream_id):
    stream = cp.agentbus.open_stream(sender.id, recipient.id, stream_id=stream_id)
    cp.agentbus.append_chunk(stream.id, sender.id, {"text": text})
    return stream


def test_pending_reports_the_backlog_without_reading_it(fleet):
    tmp, cp, session, peer = fleet
    assert _run(tmp, "admin", "agentbus", "pending", session.id)["count"] == 0

    _say(cp, peer, session, "your refresh-source finished", "s1")
    _say(cp, peer, session, "and so did the second one", "s2")

    result = _run(tmp, "admin", "agentbus", "pending", session.id)
    assert result["count"] == 2
    assert result["capped"] is False
    # Counting is not consuming: the messages are still there to be taken.
    assert _run(tmp, "admin", "agentbus", "pending", session.id)["count"] == 2


def test_drain_prints_what_was_addressed_and_consumes_it(fleet):
    tmp, cp, session, peer = fleet
    _say(cp, peer, session, "your refresh-source finished", "s1")

    drained = _run(tmp, "admin", "agentbus", "drain", session.id)

    assert drained["count"] == 1
    assert drained["committed"] is True
    assert drained["messages"][0]["payload"]["text"] == "your refresh-source finished"
    # The position lives at the hub, so the next invocation -- a different
    # process, holding no cursor -- does not replay it.
    assert _run(tmp, "admin", "agentbus", "drain", session.id)["count"] == 0
    assert _run(tmp, "admin", "agentbus", "pending", session.id)["count"] == 0


def test_drain_returns_immediately_when_nothing_is_waiting(fleet):
    """The whole point: no timeout to pick, no channel held open."""
    tmp, _cp, session, _peer = fleet
    empty = _run(tmp, "admin", "agentbus", "drain", session.id)
    assert empty["count"] == 0
    assert empty["messages"] == []
    assert empty["committed"] is False


def test_peek_reads_without_consuming(fleet):
    tmp, cp, session, peer = fleet
    _say(cp, peer, session, "look but do not take", "s1")

    peeked = _run(tmp, "admin", "agentbus", "drain", session.id, "--peek")
    assert peeked["count"] == 1
    assert peeked["committed"] is False

    assert _run(tmp, "admin", "agentbus", "drain", session.id)["count"] == 1


def test_draining_leaves_the_stream_open_for_a_reply(fleet):
    """A recipient must not destroy the channel it is expected to answer on."""
    tmp, cp, session, peer = fleet
    stream = _say(cp, peer, session, "are you on this branch?", "ask-1")

    assert _run(tmp, "admin", "agentbus", "drain", session.id)["count"] == 1

    assert cp.get_agentbus_stream(stream.id).status == "open"
    cp.agentbus.append_chunk(stream.id, peer.id, {"text": "still there?"})
    assert _run(tmp, "admin", "agentbus", "drain", session.id)["count"] == 1


def test_an_explicit_cursor_overrides_the_stored_position(fleet):
    tmp, cp, session, peer = fleet
    _say(cp, peer, session, "first", "s1")
    assert _run(tmp, "admin", "agentbus", "drain", session.id)["count"] == 1

    # A caller that keeps its own bookmark -- as `wait --after-cursor` does --
    # can still drive the read itself. Both verbs spell one cursor, so the two
    # can be interleaved.
    replayed = _run(tmp, "admin", "agentbus", "drain", session.id, "--after-cursor", "", "--peek")
    assert replayed["count"] == 1
    assert replayed["messages"][0]["payload"]["text"] == "first"


def test_drain_ignores_conversations_it_was_not_part_of(fleet):
    """An inbox is "who spoke to me", not "what is being said"."""
    tmp, cp, session, peer = fleet
    machine = cp.list_machines()[0]
    third = cp.register_agent(machine.id, "worker-2")
    _say(cp, peer, third, "not for the session", "elsewhere")

    assert _run(tmp, "admin", "agentbus", "drain", session.id)["count"] == 0
