"""Hub-only stall nudge (ADR 0023): one sender, cap, cooldown, heartbeat ≠ progress."""

from __future__ import annotations

from datetime import timedelta

from mac.agentbus_broadcast import BROADCAST_LAYER
from mac.harness_plugin import peer_must_not_nudge as plugin_peer_must_not_nudge
from mac.models import parse_time, utcnow
from mac.services import ControlPlane
from mac.session_nudge import (
    DEFAULT_MAX_ATTEMPTS,
    NUDGE_SCHEMA,
    NUDGE_TOPIC,
    nudge_stalled_sessions,
    peer_must_not_nudge,
)


def _iso(stamp, **delta):
    return (parse_time(stamp) + timedelta(**delta)).isoformat()


def _broadcast(cp, name, agent_id, subject_id, when):
    cp.observability.insert_observation(
        cp.store,
        "log",
        name,
        BROADCAST_LAYER,
        agent_id,
        "info",
        None,
        "",
        "task",
        subject_id,
        {},
        when,
    )


def _live(cp: ControlPlane):
    cp._ensure_operator_persona()
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker", capabilities=["python"])
    task = cp.create_task(title="silent work")
    cp.claim_task(task.id, agent.id)
    return agent, cp.get_task(task.id)


def test_peer_obligation_is_hub_only():
    assert peer_must_not_nudge() == "hub-only"
    assert plugin_peer_must_not_nudge() == "hub-only"


def test_silence_gets_one_addressed_nudge_from_the_hub():
    cp = ControlPlane.in_memory()
    agent, task = _live(cp)
    claimed_at = utcnow()

    result = nudge_stalled_sessions(
        cp,
        now=_iso(claimed_at, minutes=20),
        silence_seconds=600,
        cooldown_seconds=60,
    )

    assert result["sender_id"] == "agent_operator"
    assert result["nudged"] == [{"agent_id": agent.id, "task_id": task.id, "attempt": 1}]
    streams = cp.agentbus.list_streams(agent_id=agent.id)
    assert any(stream.topic == NUDGE_TOPIC for stream in streams)
    assert all(
        stream.sender_agent_id == "agent_operator"
        for stream in streams
        if stream.topic == NUDGE_TOPIC
    )
    stored = cp.get_agent(agent.id).resources["stall_nudge"]
    assert stored["count"] == 1
    assert stored["task_id"] == task.id


def test_recent_progress_is_not_a_stall():
    cp = ControlPlane.in_memory()
    agent, task = _live(cp)
    claimed_at = utcnow()
    _broadcast(
        cp,
        "bcast.task.progress",
        agent.id,
        task.id,
        _iso(claimed_at, minutes=19),
    )

    result = nudge_stalled_sessions(
        cp,
        now=_iso(claimed_at, minutes=20),
        silence_seconds=600,
    )

    assert result["nudged"] == []


def test_heartbeat_is_not_progress():
    cp = ControlPlane.in_memory()
    agent, task = _live(cp)
    claimed_at = utcnow()
    _broadcast(
        cp,
        "bcast.agent.heartbeat.v1",
        agent.id,
        task.id,
        _iso(claimed_at, minutes=19),
    )

    result = nudge_stalled_sessions(
        cp,
        now=_iso(claimed_at, minutes=20),
        silence_seconds=600,
    )

    assert result["nudged"][0]["task_id"] == task.id


def test_cooldown_and_cap():
    cp = ControlPlane.in_memory()
    agent, task = _live(cp)
    claimed_at = utcnow()

    first = nudge_stalled_sessions(
        cp,
        now=_iso(claimed_at, minutes=20),
        silence_seconds=600,
        cooldown_seconds=600,
        max_attempts=2,
    )
    immediate = nudge_stalled_sessions(
        cp,
        now=_iso(claimed_at, minutes=21),
        silence_seconds=600,
        cooldown_seconds=600,
        max_attempts=2,
    )
    second = nudge_stalled_sessions(
        cp,
        now=_iso(claimed_at, minutes=40),
        silence_seconds=600,
        cooldown_seconds=600,
        max_attempts=2,
    )
    capped = nudge_stalled_sessions(
        cp,
        now=_iso(claimed_at, minutes=60),
        silence_seconds=600,
        cooldown_seconds=600,
        max_attempts=2,
    )

    assert first["nudged"][0]["attempt"] == 1
    assert immediate["nudged"] == []
    assert second["nudged"][0]["attempt"] == 2
    assert capped["nudged"] == []
    assert cp.get_agent(agent.id).resources["stall_nudge"]["count"] == 2
    assert DEFAULT_MAX_ATTEMPTS == 3


def test_waiting_work_is_not_a_stall():
    cp = ControlPlane.in_memory()
    cp._ensure_operator_persona()
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker")
    task = cp.create_task(title="needs a human")
    cp.store.execute(
        "UPDATE tasks SET state = ?, owner_agent_id = ? WHERE id = ?",
        ("waiting", agent.id, task.id),
    )

    result = nudge_stalled_sessions(
        cp,
        now=_iso(utcnow(), minutes=20),
        silence_seconds=600,
    )

    assert result["nudged"] == []


def test_tick_includes_stall_nudges_and_swallows_failures(monkeypatch):
    cp = ControlPlane.in_memory()
    healthy = cp.tick(limit=0)
    assert healthy["stall_nudges"]["schema"] == NUDGE_SCHEMA
    assert not healthy["stall_nudges"].get("errors")

    def _boom(_plane, **_kwargs):
        raise RuntimeError("nudge exploded")

    monkeypatch.setattr("mac.session_nudge.nudge_stalled_sessions", _boom)
    broken = ControlPlane.in_memory()
    tick = broken.tick(limit=0)
    assert tick["stall_nudges"]["errors"]
    assert "assignments" in tick
