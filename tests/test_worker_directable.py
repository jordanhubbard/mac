"""Tests for MacWorker's directable peer/directive seam (task_c6f02f06).

Phase 0 contract: gated behind MAC_WORKER_DIRECTABLE (default OFF). When the
flag is unset the worker's control-poll loop must not touch peer/directive
streams at all. When ON, a 1:1 peer.message produces a peer.reply.v1, a
verified human directive acts and replies, an unverified directive is declined
without acting, and a GROUP peer stream (participants set) is skipped.
"""

from __future__ import annotations

import time
from pathlib import Path

from mac.agentbus_control import (
    PEER_MESSAGE_CONTENT_TYPE,
    PEER_MESSAGE_SCHEMA,
    PEER_MESSAGE_TOPIC,
    PEER_REPLY_TOPIC,
)
from mac.agentbus_service import HUMAN_DIRECTIVE_TOPIC
from mac.worker import MacWorker, WorkerExecution


class _Client:
    """Scripted fake hub client for the control-poll loop.

    - GET /agentbus/streams? -> the configured stream list
    - GET .../chunks?        -> the configured chunk list for that stream
    - GET .../directive-verification -> the configured verification dict
    - POST /agentbus         -> recorded
    """

    def __init__(self, streams, chunks_by_stream, verification=None) -> None:
        self.streams = streams
        self.chunks_by_stream = chunks_by_stream
        self.verification = verification
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, path: str):
        self.gets.append(path)
        if path.startswith("/agentbus/streams?"):
            return self.streams
        if "/directive-verification" in path:
            return self.verification
        if "/chunks" in path:
            # /agentbus/streams/{id}/chunks?...
            sid = path.split("/agentbus/streams/", 1)[1].split("/chunks", 1)[0]
            return self.chunks_by_stream.get(sid, [])
        return []

    def post(self, path: str, body):
        self.posts.append((path, body))
        return {}


def _worker(tmp_path: Path, client: _Client) -> MacWorker:
    return MacWorker(
        client,
        "agent_worker",
        tmp_path,
        lambda _task, _task_dir: WorkerExecution(0, "unused"),
    )


def _peer_replies(client: _Client) -> list[dict]:
    return [
        body
        for (path, body) in client.posts
        if path == "/agentbus" and body.get("topic") == PEER_REPLY_TOPIC
    ]


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _peer_stream(participants=None) -> dict:
    stream = {
        "id": "bus_peer1",
        "recipient_agent_id": "agent_worker",
        "sender_agent_id": "agent_sender",
        "topic": PEER_MESSAGE_TOPIC,
        "content_type": PEER_MESSAGE_CONTENT_TYPE,
    }
    if participants is not None:
        stream["participants"] = participants
    return stream


def _peer_chunks() -> list[dict]:
    return [
        {
            "payload": {
                "schema": PEER_MESSAGE_SCHEMA,
                "message": "please run the build",
                "correlation_id": "corr_peer",
                "from_agent_id": "agent_sender",
                "to_agent_id": "agent_worker",
            }
        }
    ]


def _directive_stream() -> dict:
    return {
        "id": "bus_dir1",
        "recipient_agent_id": "agent_worker",
        "sender_agent_id": "agent_operator",
        "topic": HUMAN_DIRECTIVE_TOPIC,
        "content_type": "application/vnd.mac.human-directive+json",
        "headers": {"issued_by": "jkh"},
    }


def _directive_chunks() -> list[dict]:
    return [
        {
            "payload": {
                "schema": "mac.human.directive.v1",
                "message": "restart the service",
                "issued_by": "jkh",
            }
        }
    ]


# --------------------------------------------------------------------------- #
# 1. Flag OFF: peer stream is not processed by the directable branches.
# --------------------------------------------------------------------------- #
def test_flag_off_peer_message_is_not_processed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MAC_WORKER_DIRECTABLE", raising=False)
    client = _Client([_peer_stream()], {"bus_peer1": _peer_chunks()})
    worker = _worker(tmp_path, client)

    called = {"turn": False}
    monkeypatch.setattr(
        MacWorker,
        "_run_directable_turn",
        lambda self, prompt, **kw: called.__setitem__("turn", True) or "x",
    )

    result = worker._process_agentbus_control()
    # Give any (erroneously spawned) thread a moment to publish.
    time.sleep(0.2)

    assert result is None
    assert _peer_replies(client) == []
    assert called["turn"] is False
    # The loop never fetched chunks or ran a turn for the peer stream.
    assert not any("/directive-verification" in g for g in client.gets)


# --------------------------------------------------------------------------- #
# 2a. Flag ON: a verified human directive produces a peer.reply.v1.
# --------------------------------------------------------------------------- #
def test_flag_on_verified_directive_replies(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAC_WORKER_DIRECTABLE", "1")
    client = _Client(
        [_directive_stream()],
        {"bus_dir1": _directive_chunks()},
        verification={
            "verified": True,
            "message": "restart the service",
            "issued_by": "jkh",
        },
    )
    worker = _worker(tmp_path, client)
    monkeypatch.setattr(
        MacWorker, "_run_directable_turn", lambda self, prompt, **kw: "service restarted"
    )

    worker._process_agentbus_control()
    assert _wait_for(lambda: len(_peer_replies(client)) == 1)

    reply = _peer_replies(client)[0]
    assert reply["recipient_agent_id"] == "agent_operator"
    assert reply["payload"]["status"] == "ok"
    assert reply["payload"]["reply"] == "service restarted"


# --------------------------------------------------------------------------- #
# 2b. Flag ON: an UNVERIFIED directive is declined and never acted on.
# --------------------------------------------------------------------------- #
def test_flag_on_unverified_directive_is_declined(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAC_WORKER_DIRECTABLE", "1")
    client = _Client(
        [_directive_stream()],
        {"bus_dir1": _directive_chunks()},
        verification={"verified": False, "reason": "not an operator-minted directive"},
    )
    worker = _worker(tmp_path, client)

    turns: list[str] = []
    monkeypatch.setattr(
        MacWorker,
        "_run_directable_turn",
        lambda self, prompt, **kw: turns.append(prompt) or "should not run",
    )

    worker._process_agentbus_control()
    assert _wait_for(lambda: len(_peer_replies(client)) == 1)

    reply = _peer_replies(client)[0]
    assert reply["payload"]["status"] == "refused"
    assert turns == []  # the directive was never acted on


# --------------------------------------------------------------------------- #
# 2c. Flag ON: a 1:1 peer.message produces a peer.reply.v1.
# --------------------------------------------------------------------------- #
def test_flag_on_peer_message_replies(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAC_WORKER_DIRECTABLE", "1")
    client = _Client([_peer_stream()], {"bus_peer1": _peer_chunks()})
    worker = _worker(tmp_path, client)
    monkeypatch.setattr(MacWorker, "_run_directable_turn", lambda self, prompt, **kw: "build done")

    worker._process_agentbus_control()
    assert _wait_for(lambda: len(_peer_replies(client)) == 1)

    reply = _peer_replies(client)[0]
    assert reply["recipient_agent_id"] == "agent_sender"
    assert reply["payload"]["status"] == "ok"
    assert reply["payload"]["reply"] == "build done"
    assert reply["payload"]["correlation_id"] == "corr_peer"


# --------------------------------------------------------------------------- #
# 3. Flag ON: a GROUP peer stream (participants set) is skipped in Phase 0.
# --------------------------------------------------------------------------- #
def test_flag_on_group_peer_stream_is_skipped(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAC_WORKER_DIRECTABLE", "1")
    client = _Client(
        [_peer_stream(participants=["agent_worker", "agent_sender"])],
        {"bus_peer1": _peer_chunks()},
    )
    worker = _worker(tmp_path, client)

    turns: list[str] = []
    monkeypatch.setattr(
        MacWorker,
        "_run_directable_turn",
        lambda self, prompt, **kw: turns.append(prompt) or "x",
    )

    worker._process_agentbus_control()
    time.sleep(0.2)

    assert _peer_replies(client) == []
    assert turns == []
