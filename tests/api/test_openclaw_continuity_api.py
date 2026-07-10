from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def test_bound_agent_reads_own_mood_and_scoped_memory(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("continuity-host")
    agent = cp.register_agent(machine.id, "continuity-agent")
    peer = cp.register_agent(machine.id, "peer-agent")
    cp.set_mood(agent.id, "warm", reason="continuity test")
    captured = []

    def recall(query, **kwargs):
        captured.append({"query": query, **kwargs})
        return [{"summary": "remember the test", "score": 0.9}]

    monkeypatch.setattr(cp, "recall_memory", recall)
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "agent-token": {"scopes": ["agent"], "agent_id": agent.id},
        },
    )
    headers = {"Authorization": "Bearer agent-token"}
    with TestClient(app) as client:
        own = client.get(
            f"/v1/agents/{agent.id}/continuity?q=what+matters&limit=3",
            headers=headers,
        )
        refused = client.get(f"/v1/agents/{peer.id}/continuity", headers=headers)

    assert own.status_code == 200
    payload = own.json()
    assert payload["schema"] == "mac.openclaw_continuity_context.v1"
    assert payload["mood"]["mode"] == "warm"
    assert "Current mood: **warm**" in payload["mood_prompt"]
    assert payload["memories"][0]["summary"] == "remember the test"
    assert captured == [
        {"query": "what matters", "tier": "medium", "limit": 3, "agent_id": agent.id},
        {"query": "what matters", "tier": "long", "limit": 3, "agent_id": agent.id},
    ]
    assert refused.status_code == 403


def test_bound_agent_cannot_recall_peer_memory(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("recall-host")
    agent = cp.register_agent(machine.id, "recall-agent")
    peer = cp.register_agent(machine.id, "recall-peer")
    monkeypatch.setattr(cp, "recall_memory", lambda *_a, **_k: [])
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "agent-token": {"scopes": ["agent"], "agent_id": agent.id},
        },
    )
    with TestClient(app) as client:
        response = client.get(
            f"/v1/memory/recall?q=x&agent_id={peer.id}",
            headers={"Authorization": "Bearer agent-token"},
        )
    assert response.status_code == 403


def test_bound_openclaw_agent_can_set_and_clear_only_its_own_mood() -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("mood-host")
    agent = cp.register_agent(machine.id, "mood-agent")
    peer = cp.register_agent(machine.id, "mood-peer")
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "agent-token": {"scopes": ["agent"], "agent_id": agent.id},
        },
    )
    headers = {"Authorization": "Bearer agent-token"}
    with TestClient(app) as client:
        set_own = client.post(
            f"/v1/agents/{agent.id}/mood",
            headers=headers,
            json={"mode": "cheerful", "reason": "a passing migration"},
        )
        set_peer = client.post(
            f"/v1/agents/{peer.id}/mood",
            headers=headers,
            json={"mode": "warm"},
        )
        clear_own = client.request(
            "DELETE",
            f"/v1/agents/{agent.id}/mood",
            headers=headers,
            json={"reason": "test complete"},
        )

    assert set_own.status_code == 200
    assert set_own.json()["mode"] == "cheerful"
    assert set_own.json()["set_by"] == agent.id
    assert set_peer.status_code == 403
    assert clear_own.status_code == 200
    assert clear_own.json()["cleared_by"] == agent.id


def test_bound_openclaw_agent_can_crud_only_its_own_config_flags() -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("flags-host")
    agent = cp.register_agent(machine.id, "flags-agent")
    peer = cp.register_agent(machine.id, "flags-peer")
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "agent-token": {"scopes": ["agent"], "agent_id": agent.id},
        },
    )
    headers = {"Authorization": "Bearer agent-token"}
    with TestClient(app) as client:
        set_own = client.put(
            f"/v1/agents/{agent.id}/config-flags/show_reasoning",
            headers=headers,
            json={"value": "on", "channel": "slack:C123", "reason": "asked by @jkh"},
        )
        set_peer = client.put(
            f"/v1/agents/{peer.id}/config-flags/show_reasoning",
            headers=headers,
            json={"value": True},
        )
        set_unknown = client.put(
            f"/v1/agents/{agent.id}/config-flags/sandbox_policy",
            headers=headers,
            json={"value": "off"},
        )
        listed = client.get(
            f"/v1/agents/{agent.id}/config-flags?channel=slack:C123",
            headers=headers,
        )
        listed_peer = client.get(
            f"/v1/agents/{peer.id}/config-flags", headers=headers
        )
        cleared = client.request(
            "DELETE",
            f"/v1/agents/{agent.id}/config-flags/show_reasoning",
            headers=headers,
            json={"channel": "slack:C123", "reason": "user changed their mind"},
        )

    assert set_own.status_code == 200
    assert set_own.json()["value"] is True
    assert set_own.json()["set_by"] == agent.id
    assert set_peer.status_code == 403
    assert set_unknown.status_code == 400
    assert listed.status_code == 200
    flags = {f["flag"]: f for f in listed.json()["flags"]}
    assert flags["show_reasoning"]["value"] is True
    assert flags["show_reasoning"]["source"] == "channel"
    assert listed_peer.status_code == 403
    assert cleared.status_code == 200
    assert cleared.json()["cleared"] is True


def test_bound_agent_can_store_only_its_own_learnings() -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("store-host")
    agent = cp.register_agent(machine.id, "store-agent")
    peer = cp.register_agent(machine.id, "store-peer")
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "agent-token": {"scopes": ["agent"], "agent_id": agent.id},
        },
    )
    headers = {"Authorization": "Bearer agent-token"}
    with TestClient(app) as client:
        stored = client.post(
            f"/v1/agents/{agent.id}/memory",
            headers=headers,
            json={
                "content": "jkh prefers threaded replies in #rockyandfriends",
                "record_type": "agent_learning:user_preference",
            },
        )
        stored_peer = client.post(
            f"/v1/agents/{peer.id}/memory",
            headers=headers,
            json={"content": "should not land"},
        )
        masquerade = client.post(
            f"/v1/agents/{agent.id}/memory",
            headers=headers,
            json={"content": "fake", "record_type": "fleet_learning:spoof"},
        )
        empty = client.post(
            f"/v1/agents/{agent.id}/memory",
            headers=headers,
            json={"content": "   "},
        )

    assert stored.status_code == 200
    record = stored.json()
    assert record["record_type"] == "agent_learning:user_preference"
    assert record["created_by"] == agent.id
    assert record["subject_id"] == agent.id
    assert stored_peer.status_code == 403
    assert masquerade.status_code == 400
    assert empty.status_code == 400

    # The record lands in the population nap consolidation summarizes
    # (created_by = agent), and the write is observable.
    memories = cp.search_memory(content_contains="threaded replies")
    assert any(m.created_by == agent.id for m in memories)
    events = cp.list_observability(name="memory.stored_by_agent", limit=5)
    assert events and events[0].subject_id == agent.id


def test_continuity_serve_emits_observability_event() -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("obs-host")
    agent = cp.register_agent(machine.id, "obs-agent")
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "agent-token": {"scopes": ["agent"], "agent_id": agent.id},
        },
    )
    headers = {"Authorization": "Bearer agent-token"}
    with TestClient(app) as client:
        response = client.get(
            f"/v1/agents/{agent.id}/continuity", headers=headers
        )
    assert response.status_code == 200
    events = cp.list_observability(name="continuity.context_served", limit=5)
    assert events and events[0].subject_id == agent.id
    assert events[0].detail["memory_count"] == 0
