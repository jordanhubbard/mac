"""Ephemeral/session agent lifecycle (task_43f8d6e3, AgentBus audit 3/7).

An agent may appear, work, communicate, and disappear. Registration carries
``resources.ephemeral: true`` + ``resources.ephemeral_ttl_seconds``; every
heartbeat renews the lease (last_seen_at); the hub tick tombstones lapsed
ephemerals with history preserved; a graceful deregister can leave one final
human-facing message that delivers after the agent is gone.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mac.models import ValidationError, parse_time, utcnow
from mac.services import ControlPlane


@pytest.fixture()
def cp() -> ControlPlane:
    return ControlPlane.in_memory()


def _ephemeral(cp: ControlPlane, name: str, ttl: int = 60):
    machine = cp.register_machine("host-%s" % name)
    return cp.register_agent(
        machine.id,
        name,
        agent_id="agent_%s" % name,
        resources={"ephemeral": True, "ephemeral_ttl_seconds": ttl},
    )


def _age_last_seen(cp: ControlPlane, agent_id: str, seconds: int) -> None:
    stale = (parse_time(utcnow()) - timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    )
    cp.store.execute(
        "UPDATE agents SET last_seen_at = ? WHERE id = ?", (stale, agent_id)
    )


def test_lapsed_ephemeral_is_tombstoned_with_streams_closed(cp: ControlPlane) -> None:
    machine = cp.register_machine("host-static")
    static = cp.register_agent(machine.id, "static-agent")
    ephemeral = _ephemeral(cp, "burst", ttl=60)
    stream = cp.open_agentbus_stream(
        ephemeral.id, recipient_agent_id=static.id, topic="peer.message.v1"
    )
    _age_last_seen(cp, ephemeral.id, 3600)
    _age_last_seen(cp, static.id, 3600)  # static agents never expire

    expired = cp.expire_ephemeral_agents()
    assert [agent.id for agent in expired] == [ephemeral.id]
    record = cp.get_agent(ephemeral.id)
    assert record.deleted_at
    # The abandoned open stream is terminally closed for waiting peers,
    # and its history remains readable.
    closed = cp.get_agentbus_stream(stream.id)
    assert closed.status == "closed"
    assert closed.sender_agent_id == ephemeral.id
    assert cp.get_agent(static.id).deleted_at is None

    # Departed-grace: the fleet snapshot still shows the ephemeral, marked.
    snapshot = cp.fleet_snapshot()
    departed = {m["agent_id"]: m for m in snapshot["members"]}
    assert departed[ephemeral.id].get("departed_at")
    assert "departed_at" not in departed[static.id]


def test_heartbeat_renews_the_ephemeral_lease(cp: ControlPlane) -> None:
    ephemeral = _ephemeral(cp, "renewed", ttl=60)
    _age_last_seen(cp, ephemeral.id, 3600)
    cp.heartbeat_agent(ephemeral.id, status="idle")
    assert cp.expire_ephemeral_agents() == []
    assert cp.get_agent(ephemeral.id).deleted_at is None


def test_ephemeral_holding_active_lease_is_not_swept(cp: ControlPlane) -> None:
    ephemeral = _ephemeral(cp, "leased", ttl=60)
    task = cp.create_task("burst work")
    cp.claim_task(task.id, ephemeral.id)
    _age_last_seen(cp, ephemeral.id, 3600)
    assert cp.expire_ephemeral_agents() == []
    assert cp.get_agent(ephemeral.id).deleted_at is None


def test_deregister_leaves_a_final_message_that_outlives_the_agent(
    cp: ControlPlane,
) -> None:
    ephemeral = _ephemeral(cp, "reporter", ttl=60)
    machine = cp.register_machine("host-gateway")
    gateway = cp.register_agent(machine.id, "gateway-agent")
    hive = cp.configure_communication_identity("mac-hive", is_default=True)
    account = cp.configure_communication_account(
        hive.id, "slack", config={"default": True}
    )
    cp.acquire_gateway_identity_lease(account.id, gateway.id)

    result = cp.deregister_agent(
        ephemeral.id,
        final_message="Session done: benchmark uploaded, 3 regressions filed",
        final_target="channel:C123",
    )
    assert result["schema"] == "mac.agent_deregister.v1"
    assert result["final_delivery_id"]
    assert cp.get_agent(ephemeral.id).deleted_at

    # The proxy guarantee end-to-end: the message still delivers via a live
    # gateway AFTER the origin agent is gone, with its identity intact.
    claimed = cp.claim_human_messages(gateway.id)
    assert [item.id for item in claimed] == [result["final_delivery_id"]]
    assert claimed[0].origin_agent_id == ephemeral.id
    delivered = cp.acknowledge_human_message(
        claimed[0].id, gateway.id, provider_message_id="1700000000.0003"
    )
    assert delivered.status == "delivered"

    # Deregistering again refuses: the agent is already decommissioned.
    with pytest.raises(ValidationError, match="decommissioned"):
        cp.deregister_agent(ephemeral.id)


def test_deregister_final_message_requires_target(cp: ControlPlane) -> None:
    ephemeral = _ephemeral(cp, "no-target", ttl=60)
    with pytest.raises(ValidationError, match="final_target"):
        cp.deregister_agent(ephemeral.id, final_message="done")
    # Refused before any mutation: still live.
    assert cp.get_agent(ephemeral.id).deleted_at is None
