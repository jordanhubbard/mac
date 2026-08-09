"""`mac agentbus wait` — the surface a working agent watches.

This verb exists to be run as a BACKGROUND task of a coding agent's own harness.
The agent keeps working in the foreground; the harness surfaces the completion
between steps. That is what lets a correction reach an agent mid-task instead of
waiting for it to finish.

Two behaviours make it usable that way and are asserted here:

* it **exits on the first message** rather than holding the channel open — a
  watcher exists to wake its caller, not to be a transport; and
* it **always prints `next_cursor`, including on timeout**, so the restarted
  watcher resumes exactly where this one stopped. Without that, a message
  landing between rounds is lost, which is the one failure that would make the
  whole mechanism untrustworthy.
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
    worker = cp.register_agent(machine.id, "worker-1")
    peer = cp.register_agent(machine.id, "reviewer")
    return tmp_path, cp, worker, peer


def _say(cp, sender, recipient, text, stream_id):
    stream = cp.agentbus.open_stream(sender.id, recipient.id, stream_id=stream_id)
    cp.agentbus.append_chunk(stream.id, sender.id, {"text": text})


def test_it_returns_a_waiting_message_immediately(fleet):
    tmp, cp, worker, peer = fleet
    _say(cp, peer, worker, "stop, wrong file", "s1")

    result = _run(tmp, "admin", "agentbus", "wait", worker.id, "--timeout-seconds", "5")
    assert result["status"] == "message"
    assert result["count"] == 1
    assert result["messages"][0]["payload"]["text"] == "stop, wrong file"
    assert result["next_cursor"]


def test_it_times_out_with_a_cursor_rather_than_hanging(fleet):
    """The timeout still carries next_cursor, so the restarted watcher does not
    re-read the backlog or skip past an unread message."""
    tmp, _cp, worker, _peer = fleet
    result = _run(
        tmp, "admin", "agentbus", "wait", worker.id,
        "--timeout-seconds", "1", "--poll-interval-seconds", "0.1",
    )
    assert result["status"] == "timeout"
    assert result["count"] == 0
    assert "next_cursor" in result


def test_a_correction_reaches_an_agent_that_is_already_waiting(fleet):
    """The behaviour the feature exists for: the watcher is running while the
    agent works, and a peer's message wakes it."""
    tmp, cp, worker, peer = fleet
    captured = {}

    def watcher():
        captured["result"] = _run(
            tmp, "admin", "agentbus", "wait", worker.id,
            "--timeout-seconds", "20", "--poll-interval-seconds", "0.2",
        )

    thread = threading.Thread(target=watcher)
    thread.start()
    time.sleep(0.8)  # the agent is working in the foreground
    _say(cp, peer, worker, "the schema changed under you", "s-live")
    thread.join(timeout=25)

    assert captured["result"]["status"] == "message"
    assert (
        captured["result"]["messages"][0]["payload"]["text"]
        == "the schema changed under you"
    )


def test_resuming_from_the_cursor_does_not_replay(fleet):
    tmp, cp, worker, peer = fleet
    _say(cp, peer, worker, "first", "s1")
    first = _run(tmp, "admin", "agentbus", "wait", worker.id, "--timeout-seconds", "5")

    again = _run(
        tmp, "admin", "agentbus", "wait", worker.id,
        "--after-cursor", first["next_cursor"],
        "--timeout-seconds", "1", "--poll-interval-seconds", "0.1",
    )
    assert again["status"] == "timeout"
    assert again["count"] == 0


def test_an_agent_is_not_woken_by_its_own_message(fleet):
    tmp, cp, worker, peer = fleet
    stream = cp.agentbus.open_stream(worker.id, peer.id, stream_id="s-own")
    cp.agentbus.append_chunk(stream.id, worker.id, {"text": "worker talking"})

    result = _run(
        tmp, "admin", "agentbus", "wait", worker.id,
        "--timeout-seconds", "1", "--poll-interval-seconds", "0.1",
    )
    assert result["status"] == "timeout"
