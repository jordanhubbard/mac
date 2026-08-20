"""`mac admin agentbus follow` / `roll-call` — the bus from a terminal.

`wait` answers "has anyone said anything to ME" and exits on the first message,
because a watcher exists to wake its caller. That makes it the wrong tool for
the other question a bus is for -- "what is the fleet saying" -- and before
these verbs the only approximation was re-invoking `wait` in a shell loop,
which sees addressed messages only and therefore cannot show a conversation.

Three properties are asserted, because each one is a way the verb could look
like it works and not:

* it hears traffic it was NOT addressed in, which is the entire difference from
  `wait`;
* every line carries `addressed_to`, so the convention that nobody answers
  until named survives the trip through the CLI; and
* the closing line always carries `next_cursor`, so a follower resumed after an
  interruption starts where the last one stopped instead of replaying or
  skipping.

The same two reads back the console's Bus view. One backend, two front ends --
so a divergence between what the terminal shows and what the console shows is a
bug in one of these, not two independent products drifting.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main
from mac.test_support import control_plane_on, dsn_for


def _run_lines(db, *args):
    """Run a command that emits NDJSON, returning the parsed lines."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        main(["--db", dsn_for(db), "--json", *args])
    finally:
        sys.stdout = old
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


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
    listener = cp.register_agent(machine.id, "listener", capabilities=["python"])
    speaker = cp.register_agent(machine.id, "speaker", capabilities=["python"])
    third = cp.register_agent(machine.id, "third", capabilities=["review"])
    return tmp_path, cp, listener, speaker, third


def _say(cp, sender, recipient, text, stream_id):
    stream = cp.agentbus.open_stream(sender.id, recipient.id, stream_id=stream_id)
    cp.agentbus.append_chunk(stream.id, sender.id, {"text": text})


def test_it_hears_a_conversation_it_was_not_addressed_in(fleet):
    """The whole reason the verb exists. `wait` would report nothing here."""
    tmp, cp, listener, speaker, third = fleet
    _say(cp, speaker, third, "I have the branch, do not touch it", "s-peer")

    lines = _run_lines(tmp, "admin", "agentbus", "follow", listener.id, "--once")

    traffic = [line for line in lines if line["event"] == "traffic"]
    assert len(traffic) == 1
    assert traffic[0]["chunk"]["payload"]["text"] == "I have the branch, do not touch it"
    assert traffic[0]["from_agent_id"] == speaker.id


def test_each_line_says_who_is_expected_to_answer(fleet):
    """`addressed_to` is addressing, not access.

    The message is readable by everyone -- that is what makes the bus useful.
    What the field carries is the convention: the named agent answers. A CLI
    that dropped it would leave a reader unable to tell a question aimed at
    them from one they are merely overhearing.
    """
    tmp, cp, listener, speaker, third = fleet
    _say(cp, speaker, third, "third, can you review this?", "s-addr")

    lines = _run_lines(tmp, "admin", "agentbus", "follow", listener.id, "--once")
    entry = next(line for line in lines if line["event"] == "traffic")

    assert entry["addressed_to"] == [third.id]
    assert entry["addressed_to_me"] is False
    assert entry["reply_expected"] is False


def test_a_message_addressed_to_the_follower_is_marked_as_expecting_a_reply(fleet):
    tmp, cp, listener, speaker, _third = fleet
    _say(cp, speaker, listener, "listener, the schema changed under you", "s-me")

    lines = _run_lines(tmp, "admin", "agentbus", "follow", listener.id, "--once")
    entry = next(line for line in lines if line["event"] == "traffic")

    assert entry["addressed_to_me"] is True
    assert entry["reply_expected"] is True


def test_it_always_closes_with_a_resumable_cursor(fleet):
    tmp, cp, listener, speaker, third = fleet
    _say(cp, speaker, third, "first", "s-1")

    lines = _run_lines(tmp, "admin", "agentbus", "follow", listener.id, "--once")
    closing = lines[-1]

    assert closing["event"] == "closed"
    assert closing["count"] == 1
    assert closing["next_cursor"]


def test_resuming_from_the_cursor_does_not_replay(fleet):
    """The property that makes an interrupted follow safe to restart."""
    tmp, cp, listener, speaker, third = fleet
    _say(cp, speaker, third, "first", "s-1")
    first = _run_lines(tmp, "admin", "agentbus", "follow", listener.id, "--once")
    cursor = first[-1]["next_cursor"]

    again = _run_lines(
        tmp, "admin", "agentbus", "follow", listener.id,
        "--once", "--after-cursor", cursor,
    )

    assert [line for line in again if line["event"] == "traffic"] == []
    assert again[-1]["event"] == "closed"
    assert again[-1]["count"] == 0
    # The cursor survives an empty round, so the NEXT resume is still correct.
    assert again[-1]["next_cursor"] == cursor


def test_a_follower_does_not_hear_itself(fleet):
    """A follower woken by its own writes would spin."""
    tmp, cp, listener, speaker, _third = fleet
    stream = cp.agentbus.open_stream(listener.id, speaker.id, stream_id="s-own")
    cp.agentbus.append_chunk(stream.id, listener.id, {"text": "listener talking"})

    lines = _run_lines(tmp, "admin", "agentbus", "follow", listener.id, "--once")

    assert [line for line in lines if line["event"] == "traffic"] == []


def test_exclude_addressed_drops_what_the_inbox_already_has(fleet):
    tmp, cp, listener, speaker, third = fleet
    _say(cp, speaker, listener, "aimed at the follower", "s-mine")
    _say(cp, speaker, third, "aimed at someone else", "s-theirs")

    lines = _run_lines(
        tmp, "admin", "agentbus", "follow", listener.id,
        "--once", "--exclude-addressed",
    )
    texts = [
        line["chunk"]["payload"]["text"]
        for line in lines
        if line["event"] == "traffic"
    ]

    assert texts == ["aimed at someone else"]


def test_roll_call_lists_who_is_present_and_what_each_can_do(fleet):
    tmp, _cp, listener, speaker, third = fleet

    body = _run(tmp, "admin", "agentbus", "roll-call")

    by_id = {entry["id"]: entry for entry in body["agents"]}
    assert set(by_id) >= {listener.id, speaker.id, third.id}
    assert by_id[third.id]["capabilities"] == ["review"]
    assert body["agent_count"] == len(body["agents"])
