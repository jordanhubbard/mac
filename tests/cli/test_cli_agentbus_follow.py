"""`mac admin agentbus follow` — the tail -f the bus never had.

`wait` answers "has anyone spoken TO ME" and exits on the first message,
because a watcher exists to wake its caller. That leaves the other question
unanswerable from the CLI: "what is the fleet saying". Most of the bus is
traffic an agent was not addressed in, and it is the part that stops an agent
touching a branch a peer already holds.

The hub has had `/agents/{id}/agentbus/traffic` for that all along, with no
caller on any front end. This verb and the console's Bus view are the two that
now exist (ADR 0025).

Three behaviours are asserted, because each one is what makes it a *follow*
rather than a second `wait`:

* it keeps printing rather than exiting on the first message;
* it carries traffic the agent was NOT addressed in — that is the point; and
* every line carries a resumable `cursor`, so interrupting it loses nothing.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time

import pytest

from mac.cli import main
from mac.test_support import control_plane_on, dsn_for


def _run(db, *args):
    """Run the CLI and return every JSON document it printed, in order.

    `follow` streams: it emits one record per line as each message arrives,
    rather than one document at the end, because a follow that cannot be piped
    into `grep` while it runs is not a follow. Decoding the buffer document by
    document reads both that and the ordinary pretty-printed single result, so
    one helper covers every verb here.

    The name is the convention the other CLI tests use, and is what
    `test_cli_coverage_gate.py` scans for.
    """
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        main(["--db", dsn_for(db), "--json", *args])
    finally:
        sys.stdout = old

    decoder = json.JSONDecoder()
    buffer = out.getvalue()
    records = []
    index = 0
    while index < len(buffer):
        if buffer[index].isspace():
            index += 1
            continue
        record, index = decoder.raw_decode(buffer, index)
        records.append(record)
    return records


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_SECRET_KEY", "test-key-with-at-least-32-characters-x")
    cp = control_plane_on(dsn_for(tmp_path))
    machine = cp.register_machine("host")
    return (
        tmp_path,
        cp,
        cp.register_agent(machine.id, "worker-1"),
        cp.register_agent(machine.id, "worker-2"),
        cp.register_agent(machine.id, "observer"),
    )


def _say(cp, sender, recipient, text, stream_id):
    stream = cp.agentbus.open_stream(
        sender.id, recipient.id if recipient is not None else None, stream_id=stream_id
    )
    cp.agentbus.append_chunk(stream.id, sender.id, {"text": text})


def test_it_hears_a_conversation_it_was_not_part_of(fleet):
    """The whole reason this is not `wait`.

    `wait` would report nothing here: the observer was never addressed. An
    agent about to edit `src/mac/api.py` needs to hear that a peer already
    claimed it, and nobody thinks to address that to everyone.
    """
    tmp, cp, worker, peer, observer = fleet
    _say(cp, worker, peer, "taking src/mac/api.py", "s1")

    lines = _run(
        tmp, "admin", "agentbus", "follow", observer.id,
        "--timeout-seconds", "1", "--poll-interval-seconds", "0.1",
    )
    messages = [line for line in lines if "chunk" in line]

    assert [line["chunk"]["payload"]["text"] for line in messages] == [
        "taking src/mac/api.py"
    ]
    assert messages[0]["from_agent_id"] == worker.id
    # Overheard, not addressed: the observer is not expected to answer, and the
    # convention that it stays quiet is what makes the bus overhearable safely.
    assert messages[0]["addressed_to"] == [peer.id]
    assert messages[0]["addressed_to_me"] is False


def test_it_keeps_printing_rather_than_exiting_on_the_first_message(fleet):
    tmp, cp, worker, peer, observer = fleet
    _say(cp, worker, peer, "first", "s1")
    _say(cp, worker, peer, "second", "s2")
    _say(cp, worker, peer, "third", "s3")

    lines = _run(
        tmp, "admin", "agentbus", "follow", observer.id,
        "--timeout-seconds", "1", "--poll-interval-seconds", "0.1",
    )
    messages = [line for line in lines if "chunk" in line]

    assert [line["chunk"]["payload"]["text"] for line in messages] == [
        "first",
        "second",
        "third",
    ]


def test_every_line_carries_a_cursor_and_resuming_does_not_replay(fleet):
    tmp, cp, worker, peer, observer = fleet
    _say(cp, worker, peer, "first", "s1")

    first = [
        line
        for line in _run(
            tmp, "admin", "agentbus", "follow", observer.id,
            "--timeout-seconds", "1", "--poll-interval-seconds", "0.1",
        )
        if "chunk" in line
    ]
    assert first[0]["cursor"]

    again = _run(
        tmp, "admin", "agentbus", "follow", observer.id,
        "--after-cursor", first[-1]["cursor"],
        "--timeout-seconds", "1", "--poll-interval-seconds", "0.1",
    )
    assert [line for line in again if "chunk" in line] == []


def test_a_bounded_follow_says_where_it_stopped(fleet):
    """A quiet follow still ends with a resumable position.

    Without it, the next follow either replays the backlog or skips whatever
    landed between the two runs — the same failure `wait --after-cursor` exists
    to avoid.
    """
    tmp, _cp, _worker, _peer, observer = fleet

    lines = _run(
        tmp, "admin", "agentbus", "follow", observer.id,
        "--timeout-seconds", "1", "--poll-interval-seconds", "0.1",
    )

    assert lines[-1]["status"] == "timeout"
    assert "next_cursor" in lines[-1]


def test_max_messages_stops_a_forever_follow(fleet):
    tmp, cp, worker, peer, observer = fleet
    for index in range(4):
        _say(cp, worker, peer, "line %d" % index, "s%d" % index)

    lines = _run(
        tmp, "admin", "agentbus", "follow", observer.id,
        "--timeout-seconds", "0", "--max-messages", "2",
        "--poll-interval-seconds", "0.1",
    )

    assert [line["chunk"]["payload"]["text"] for line in lines] == ["line 0", "line 1"]


def test_only_unaddressed_drops_what_wait_already_handles(fleet):
    """The two verbs compose instead of overlapping.

    An agent running `wait` in the background is already being woken for
    anything addressed to it; a follow beside it can skip those and show only
    the ambient traffic.
    """
    tmp, cp, worker, peer, observer = fleet
    _say(cp, worker, observer, "this one is for you", "s-direct")
    _say(cp, worker, peer, "this one is not", "s-other")

    lines = _run(
        tmp, "admin", "agentbus", "follow", observer.id, "--only-unaddressed",
        "--timeout-seconds", "1", "--poll-interval-seconds", "0.1",
    )
    messages = [line for line in lines if "chunk" in line]

    assert [line["chunk"]["payload"]["text"] for line in messages] == ["this one is not"]


def test_a_follow_is_not_woken_by_its_own_messages(fleet):
    tmp, cp, _worker, peer, observer = fleet
    _say(cp, observer, peer, "observer talking", "s-own")

    lines = _run(
        tmp, "admin", "agentbus", "follow", observer.id,
        "--timeout-seconds", "1", "--poll-interval-seconds", "0.1",
    )

    assert [line for line in lines if "chunk" in line] == []


def test_a_message_landing_mid_follow_is_printed_while_it_runs(fleet):
    """The behaviour the verb exists for: it is watched while work happens."""
    tmp, cp, worker, peer, observer = fleet
    captured = {}

    def watcher():
        captured["lines"] = _run(
            tmp, "admin", "agentbus", "follow", observer.id,
            "--timeout-seconds", "20", "--poll-interval-seconds", "0.2",
            "--max-messages", "1",
        )

    thread = threading.Thread(target=watcher)
    thread.start()
    time.sleep(0.8)  # the operator is doing something else
    _say(cp, worker, peer, "canonical branch moved", "s-live")
    thread.join(timeout=25)

    assert captured["lines"][-1]["chunk"]["payload"]["text"] == "canonical branch moved"


def test_roll_call_lists_who_is_present_and_what_they_can_do(fleet):
    tmp, _cp, worker, peer, observer = fleet

    [body] = _run(tmp, "admin", "agentbus", "roll-call")

    assert body["agent_count"] == len(body["agents"])
    assert {agent["id"] for agent in body["agents"]} >= {worker.id, peer.id, observer.id}
    assert all("capabilities" in agent for agent in body["agents"])
