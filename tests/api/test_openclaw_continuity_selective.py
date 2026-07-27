"""Regression coverage for selective, AgentBus-aware continuity recall.

These tests reproduce the 2026-07-23 field incident: a 'query token
inefficiency findings' recall returned only low-relevance task-import records
(scores ~0.037-0.054), so a prior AgentBus conversation about Reto looked
forgotten. The fix must (1) surface the real bus exchange ahead of the filler
band and (2) omit low-scoring filler entirely when nothing genuinely matches.
"""

from fastapi.testclient import TestClient

from mac.agentbus_control import PEER_MESSAGE_SCHEMA
from mac.api import create_app
from mac.openclaw_continuity import ContinuityConfig, recall_continuity
from mac.services import ControlPlane

PEER_CONTENT_TYPE = "application/vnd.mac.agent-peer+json"
PEER_TOPIC = "peer.message.v1"

# The exact low-relevance band observed in the field.
FILLER_HITS = [
    {"memory_id": "m1", "summary": "task-import confirmation for task_aaa", "score": 0.037},
    {"memory_id": "m2", "summary": "imported task task_bbb into the queue", "score": 0.041},
    {"memory_id": "m3", "summary": "task queued: task_ccc nap summary", "score": 0.054},
]


def _seed_reto_conversation(cp, agent_id, peer_id):
    stream = cp.agentbus.open_stream(
        sender_agent_id=agent_id,
        recipient_agent_id=peer_id,
        content_type=PEER_CONTENT_TYPE,
        topic=PEER_TOPIC,
        headers={"correlation_id": "corr-reto-1"},
    )
    cp.agentbus.append_chunk(
        stream.id,
        agent_id,
        payload={
            "schema": PEER_MESSAGE_SCHEMA,
            "from_agent_id": agent_id,
            "to_agent_id": peer_id,
            "correlation_id": "corr-reto-1",
            "message": (
                "Reto findings: the token inefficiency came from injecting "
                "low-score filler memories to fill the recall limit."
            ),
        },
        content_type=PEER_CONTENT_TYPE,
    )
    return stream


def test_prior_reto_bus_exchange_outranks_task_import_filler(monkeypatch):
    # Even with verbose poll telemetry explicitly enabled, historical
    # continuity search is analytical recall rather than a new-message read.
    monkeypatch.setenv("MAC_OBSERVABILITY_VERBOSE_POLL", "1")
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("reto-host")
    agent = cp.register_agent(machine.id, "reto-agent")
    peer = cp.register_agent(machine.id, "reto-peer")
    _seed_reto_conversation(cp, agent.id, peer.id)

    # Simulate a backend that ignores the score floor: fusion must still drop the
    # 0.037-0.054 filler band so it never crowds out the real bus exchange.
    def recall(query, **kwargs):
        return list(FILLER_HITS)

    monkeypatch.setattr(cp, "recall_memory", recall)
    app = create_app(
        control_plane=cp,
        auth_tokens={"agent-token": {"scopes": ["agent"], "agent_id": agent.id}},
    )
    headers = {"Authorization": "Bearer agent-token"}
    with TestClient(app) as client:
        resp = client.get(
            f"/v1/agents/{agent.id}/continuity?q=token+inefficiency+findings&limit=5",
            headers=headers,
        )

    assert resp.status_code == 200
    memories = resp.json()["memories"]
    assert memories, "expected the Reto bus exchange to be recalled"
    top = memories[0]
    assert top["source"] == "bus"
    assert "Reto" in top["text"]
    # Provenance preserved for browsing/observability.
    assert top["sender_agent_id"] == agent.id
    assert top["recipient_agent_id"] == peer.id
    assert top["correlation_id"] == "corr-reto-1"
    assert top["topic"] == PEER_TOPIC
    assert top["timestamp"]
    assert top["score"] > 0.0
    # The 0.037-0.054 filler band must not have crowded it out.
    assert not any(m["source"] == "memory" for m in memories)
    metrics = resp.json()["recall_metrics"]
    assert metrics["source_bus"] == 1
    assert metrics["threshold_drops"] >= 3
    assert cp.list_observability(name="agentbus.chunks.read", limit=10) == []


def test_no_match_omits_low_scoring_filler(monkeypatch):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("empty-host")
    agent = cp.register_agent(machine.id, "empty-agent")

    def recall(query, **kwargs):
        return list(FILLER_HITS)

    monkeypatch.setattr(cp, "recall_memory", recall)
    app = create_app(
        control_plane=cp,
        auth_tokens={"agent-token": {"scopes": ["agent"], "agent_id": agent.id}},
    )
    headers = {"Authorization": "Bearer agent-token"}
    with TestClient(app) as client:
        resp = client.get(
            f"/v1/agents/{agent.id}/continuity?q=something+entirely+unrelated&limit=5",
            headers=headers,
        )

    assert resp.status_code == 200
    body = resp.json()
    # No genuine match: filler is dropped, not padded in to reach the limit.
    assert body["memories"] == []
    assert body["recall_metrics"]["selected"] == 0
    assert body["recall_metrics"]["threshold_drops"] >= 3


def test_secret_and_mirror_bus_payloads_are_excluded(monkeypatch):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("sec-host")
    agent = cp.register_agent(machine.id, "sec-agent")
    peer = cp.register_agent(machine.id, "sec-peer")

    # A secret-bearing peer message and an LLM Slack-mirror of the same
    # conversation both mention the query terms but must never be recalled.
    secret_stream = cp.agentbus.open_stream(
        sender_agent_id=agent.id,
        recipient_agent_id=peer.id,
        content_type=PEER_CONTENT_TYPE,
        topic=PEER_TOPIC,
    )
    cp.agentbus.append_chunk(
        secret_stream.id,
        agent.id,
        payload={
            "schema": PEER_MESSAGE_SCHEMA,
            "from_agent_id": agent.id,
            "to_agent_id": peer.id,
            "message": "deploy token findings",
            "api_key": "sk-thisisasecretkeyvalue1234567890",
        },
        content_type=PEER_CONTENT_TYPE,
    )
    mirror_stream = cp.agentbus.open_stream(
        sender_agent_id=agent.id,
        recipient_agent_id=peer.id,
        content_type="application/json",
        topic="fleet.mirror.v1",
    )
    cp.agentbus.append_chunk(
        mirror_stream.id,
        agent.id,
        payload={
            "schema": "mac.fleet_conversation_mirror.v1",
            "stream_id": secret_stream.id,
            "sender_agent_id": agent.id,
            "message": "findings mirror text",
        },
        content_type="application/json",
    )

    monkeypatch.setattr(cp, "recall_memory", lambda *a, **k: [])
    result, metrics = recall_continuity(
        agent_id=agent.id,
        query="findings",
        limit=5,
        recall=cp.recall_memory,
        agentbus=cp.agentbus,
        config=ContinuityConfig(min_score=0.0),
    )
    assert result == []
    assert metrics.secret_drops >= 1
    assert metrics.mirror_drops >= 1


def test_bus_recall_respects_bound_agent_authorization(monkeypatch):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("auth-host")
    agent = cp.register_agent(machine.id, "auth-agent")
    other_a = cp.register_agent(machine.id, "other-a")
    other_b = cp.register_agent(machine.id, "other-b")

    # A conversation the requesting agent is NOT part of must be invisible.
    stream = cp.agentbus.open_stream(
        sender_agent_id=other_a.id,
        recipient_agent_id=other_b.id,
        content_type=PEER_CONTENT_TYPE,
        topic=PEER_TOPIC,
    )
    cp.agentbus.append_chunk(
        stream.id,
        other_a.id,
        payload={
            "schema": PEER_MESSAGE_SCHEMA,
            "from_agent_id": other_a.id,
            "to_agent_id": other_b.id,
            "message": "private findings between other agents",
        },
        content_type=PEER_CONTENT_TYPE,
    )

    result, _ = recall_continuity(
        agent_id=agent.id,
        query="findings",
        limit=5,
        recall=lambda *a, **k: [],
        agentbus=cp.agentbus,
        config=ContinuityConfig(min_score=0.0),
    )
    assert result == []
