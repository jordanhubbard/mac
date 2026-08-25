"""`mac admin agentbus broadcast` — the observation feed, readable by a human.

The broadcast channel carried the fleet's git facts and had no CLI reader,
which is most of why nobody read it: an agent told to "check the bus before
starting" had no command to type. This verb is that command, and the skill
(`skills/agentbus-context/SKILL.md`) names it.

What is asserted here is what a reader needs to trust it: the events come back,
the type filter selects, and an event this agent emitted itself is marked as
such so a consumer can drop its own echo.
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
    worker = cp.register_agent(machine.id, "worker-1")
    peer = cp.register_agent(machine.id, "worker-2")
    return tmp_path, cp, worker, peer


def test_it_reads_the_feed_and_marks_the_readers_own_echo(fleet):
    tmp, cp, worker, peer = fleet
    cp.publish_agentbus_broadcast(
        peer.id, "git.merged", payload={"tree_sha": "tree-abc", "pr_number": 461}
    )
    cp.publish_agentbus_broadcast(worker.id, "git.pushed", payload={"sha": "mine"})

    heard = _run(tmp, "admin", "agentbus", "broadcast", worker.id, "--limit", "50")

    by_type = {item["event_type"]: item for item in heard}
    assert by_type["git.merged"]["payload"]["tree_sha"] == "tree-abc"
    assert by_type["git.merged"]["self_emitted"] is False
    assert by_type["git.pushed"]["self_emitted"] is True


def test_the_type_filter_selects_the_terminal_events(fleet):
    tmp, cp, worker, peer = fleet
    cp.publish_agentbus_broadcast(peer.id, "task.progress", payload={"note": "noise"})
    cp.publish_agentbus_broadcast(peer.id, "git.merged", payload={"tree_sha": "t1"})
    cp.publish_agentbus_broadcast(peer.id, "git.canonical_advanced", payload={"to_sha": "tip1"})

    heard = _run(
        tmp,
        "admin",
        "agentbus",
        "broadcast",
        worker.id,
        "--event-types",
        "git.merged,git.canonical_advanced",
    )

    assert sorted(item["event_type"] for item in heard) == [
        "git.canonical_advanced",
        "git.merged",
    ]
