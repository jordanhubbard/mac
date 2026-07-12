"""Agent decommissioning preserves history (task_c394685a, AgentBus audit 2/7).

`delete_agent` used to hard-delete the agents row, cascading away the agent's
entire AgentBus stream history on both sides. For appear→work→exit ephemeral
agents that is backwards: their proxied results must outlive them. Deletion is
now a tombstone — the row stays (streams/events/deliveries keep their real
identities), operational overlays are purged, and liveness operations refuse
the tombstoned agent.
"""

from __future__ import annotations

import pytest

from mac.models import ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp() -> ControlPlane:
    return ControlPlane.in_memory()


def _agent(cp: ControlPlane, name: str):
    machine = cp.register_machine("host-%s" % name)
    return cp.register_agent(machine.id, name, agent_id="agent_%s" % name)


def test_bus_history_survives_agent_deletion(cp: ControlPlane) -> None:
    ephemeral = _agent(cp, "ephemeral")
    peer = _agent(cp, "peer")
    published = cp.publish_agentbus_content(
        sender_agent_id=ephemeral.id,
        recipient_agent_id=peer.id,
        topic="peer.message.v1",
        content_type="application/vnd.mac.agent-peer+json",
        payload={"schema": "mac.agent.peer_message.v1", "message": "final results: 42"},
    )
    stream_id = published["stream"]["id"]

    cp.delete_agent(ephemeral.id, actor="test")

    # The surviving counterpart still lists and reads the exchange, with the
    # departed agent's real identity intact.
    streams = cp.list_agentbus_streams(agent_id=peer.id)
    assert any(item.id == stream_id for item in streams)
    chunks = cp.read_agentbus_chunks(peer.id, stream_id)
    assert chunks and chunks[0].payload["message"] == "final results: 42"
    assert cp.get_agentbus_stream(stream_id).sender_agent_id == ephemeral.id


def test_tombstoned_agent_is_hidden_but_inspectable(cp: ControlPlane) -> None:
    gone = _agent(cp, "gone")
    stays = _agent(cp, "stays")
    cp.set_config_flag(gone.id, "show_reasoning", True)
    cp.delete_agent(gone.id, actor="test")
    # Idempotent second delete.
    cp.delete_agent(gone.id, actor="test")

    listed = {agent.id for agent in cp.list_agents()}
    assert listed == {stays.id}
    everyone = {agent.id for agent in cp.list_agents(include_deleted=True)}
    assert everyone == {gone.id, stays.id}

    # History reads stay permissive: the row is inspectable with the marker,
    # operational overlays are gone (flag back to registry default).
    record = cp.get_agent(gone.id)
    assert record.deleted_at
    assert record.dispatch_hold is True
    assert record.status == "offline"
    flag = cp.get_config_flag(gone.id, "show_reasoning")
    assert flag["value"] is False and flag["source"] == "default"


def test_liveness_operations_refuse_tombstoned_agents(cp: ControlPlane) -> None:
    dead = _agent(cp, "dead")
    live = _agent(cp, "live")
    task = cp.create_task("leftover work")
    hive = cp.configure_communication_identity("mac-hive", is_default=True)
    account = cp.configure_communication_account(
        hive.id, "slack", config={"default": True}
    )
    cp.delete_agent(dead.id, actor="test")

    with pytest.raises(ValidationError, match="decommissioned"):
        cp.heartbeat_agent(dead.id, status="idle")
    with pytest.raises(ValidationError, match="decommissioned"):
        cp.claim_task(task.id, dead.id)
    with pytest.raises(ValidationError, match="decommissioned"):
        cp.publish_agentbus_content(
            sender_agent_id=dead.id,
            recipient_agent_id=live.id,
            payload={"nope": True},
        )
    with pytest.raises(ValidationError, match="decommissioned"):
        cp.open_agentbus_stream(dead.id, recipient_agent_id=live.id)
    with pytest.raises(ValidationError, match="decommissioned"):
        cp.acquire_gateway_identity_lease(account.id, dead.id)
    with pytest.raises(ValidationError, match="decommissioned"):
        cp.claim_human_messages(dead.id)
    # The live agent is unaffected.
    cp.heartbeat_agent(live.id, status="idle")
    cp.publish_agentbus_content(
        sender_agent_id=live.id, recipient_agent_id=dead.id, payload={"ok": True}
    )
