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
    # Provenance + score are attached so OpenClaw can label the item.
    assert payload["memories"][0]["source"] == "memory"
    assert payload["memories"][0]["score"] == 0.9
    # A calibrated minimum-relevance floor is now pushed into recall so the
    # vector store never returns filler to reach the limit.
    assert len(captured) == 2
    assert captured[0]["query"] == "what matters"
    assert captured[0]["tier"] == "medium"
    assert captured[0]["limit"] == 3
    assert captured[0]["agent_id"] == agent.id
    assert captured[0]["min_score"] is not None and captured[0]["min_score"] > 0.0
    assert captured[1]["tier"] == "long"
    # Observability now carries a source mix without query contents.
    assert payload["recall_metrics"]["source_memory"] == 1
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


def test_mirror_fleet_conversation_flag_is_registered_and_agent_settable() -> None:
    """The conversation-mirroring toggle the agent flips from natural language
    ('let me know what you guys are talking about') must exist in the allowlist
    and be a bool the bound agent can set on itself."""
    from mac.config_flags import CONFIG_FLAG_REGISTRY, flag_default, validate_flag_value

    spec = CONFIG_FLAG_REGISTRY["mirror_fleet_conversation"]
    assert spec["type"] == "bool"
    # On by default: real agent conversations mirror to the home channel unless a
    # user turns it off ("I no longer want to know what you guys are talking about").
    assert flag_default("mirror_fleet_conversation") is True
    assert validate_flag_value("mirror_fleet_conversation", "on") is True
    assert validate_flag_value("mirror_fleet_conversation", "off") is False

    cp = ControlPlane.in_memory()
    machine = cp.register_machine("mirror-host")
    agent = cp.register_agent(machine.id, "mirror-agent")
    app = create_app(
        control_plane=cp,
        auth_tokens={"agent-token": {"scopes": ["agent"], "agent_id": agent.id}},
    )
    headers = {"Authorization": "Bearer agent-token"}
    with TestClient(app) as client:
        # Agent-global scope (channel='') — the home channel is the destination.
        set_on = client.put(
            f"/v1/agents/{agent.id}/config-flags/mirror_fleet_conversation",
            headers=headers,
            json={"value": "on", "reason": "let me know what you guys are talking about"},
        )
        listed = client.get(f"/v1/agents/{agent.id}/config-flags", headers=headers)
    assert set_on.status_code == 200
    assert set_on.json()["value"] is True
    effective = {f["flag"]: f["value"] for f in listed.json()["flags"]}
    assert effective["mirror_fleet_conversation"] is True


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


def test_bound_agent_reports_own_deploy_config_and_reads_effective_view() -> None:
    """The consolidated 'geek knobs' path (task_dfdf6ea9): a gateway
    self-reports its non-secret deploy document, and the effective-config
    view merges identity + runtime flags + deploy doc in one response."""
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("knobs-host")
    agent = cp.register_agent(machine.id, "knobs-agent")
    peer = cp.register_agent(machine.id, "knobs-peer")
    app = create_app(
        control_plane=cp,
        auth_tokens={"agent-token": {"scopes": ["agent"], "agent_id": agent.id}},
    )
    headers = {"Authorization": "Bearer agent-token"}
    document = {
        "gateway": {
            "host": "sparky",
            "image": "localhost/mac-openclaw:test",
            "sandbox": "mac-openclaw-knobs",
            "home_channel": "channel:C123",
        },
        "models": {"mirror_summarizer": "azure/anthropic/claude-sonnet-4-6"},
    }
    with TestClient(app) as client:
        reported = client.put(
            f"/v1/agents/{agent.id}/deploy-config",
            headers=headers,
            json={"document": document},
        )
        reported_peer = client.put(
            f"/v1/agents/{peer.id}/deploy-config",
            headers=headers,
            json={"document": document},
        )
        effective = client.get(
            f"/v1/agents/{agent.id}/effective-config", headers=headers
        )
        effective_peer = client.get(
            f"/v1/agents/{peer.id}/effective-config", headers=headers
        )

    assert reported.status_code == 200
    assert reported.json()["schema"] == "mac.agent_deploy_config.v1"
    assert reported.json()["reported_by"] == agent.id
    assert reported_peer.status_code == 403
    assert effective_peer.status_code == 403

    assert effective.status_code == 200
    view = effective.json()
    assert view["schema"] == "mac.agent_effective_config.v1"
    assert view["agent"]["id"] == agent.id
    assert view["agent"]["name"] == "knobs-agent"
    # Runtime flag registry included at agent-global scope.
    flags = {f["flag"]: f["value"] for f in view["config_flags"]}
    assert "mirror_fleet_conversation" in flags
    # The reported deploy doc round-trips.
    assert view["deploy_config"]["document"] == document
    # Audited like every other agent-state mutation.
    events = cp.list_observability(name="agent.deploy_config_reported", limit=5)
    assert events and events[0].subject_id == agent.id


def test_deploy_config_rejects_secret_like_keys_and_oversize() -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("secret-host")
    agent = cp.register_agent(machine.id, "secret-agent")
    app = create_app(
        control_plane=cp,
        auth_tokens={"agent-token": {"scopes": ["agent"], "agent_id": agent.id}},
    )
    headers = {"Authorization": "Bearer agent-token"}
    with TestClient(app) as client:
        secret_top = client.put(
            f"/v1/agents/{agent.id}/deploy-config",
            headers=headers,
            json={"document": {"router_api_key": "sk-nope"}},
        )
        secret_nested = client.put(
            f"/v1/agents/{agent.id}/deploy-config",
            headers=headers,
            json={"document": {"gateway": {"slack_bot_token": "xoxb-nope"}}},
        )
        empty = client.put(
            f"/v1/agents/{agent.id}/deploy-config",
            headers=headers,
            json={"document": {}},
        )
    assert secret_top.status_code == 400
    assert "secret-like" in secret_top.json()["detail"]
    assert secret_nested.status_code == 400
    assert empty.status_code == 400
    # Nothing stored after the rejected writes.
    assert cp.get_agent_deploy_config(agent.id) is None


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
