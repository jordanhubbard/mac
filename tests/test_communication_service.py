from __future__ import annotations

import pytest

from mac.models import TransitionError
from mac.services import ControlPlane


@pytest.fixture()
def cp() -> ControlPlane:
    return ControlPlane.in_memory()


def _agent(cp: ControlPlane, name: str):
    machine = cp.register_machine("host-%s" % name)
    return cp.register_agent(machine.id, name, agent_id="agent_%s" % name)


def test_default_hive_represents_many_agents_without_per_agent_accounts(cp: ControlPlane) -> None:
    first = _agent(cp, "first")
    second = _agent(cp, "second")
    hive = cp.configure_communication_identity(
        "mac-hive", display_name="MAC Hive", is_default=True
    )
    account = cp.configure_communication_account(
        hive.id,
        "slack",
        "operations",
        credential_refs={"bot": "secret://channel-identity.mac-hive.slack.operations.bot"},
        config={"default": True},
    )

    for agent in (first, second):
        resolved = cp.resolve_agent_representation(agent.id)
        assert resolved["mode"] == "delegated"
        assert resolved["identity"]["id"] == hive.id
        assert resolved["reason"] == "default_identity"

    assert cp.list_communication_accounts(identity_id=hive.id) == [account]


def test_direct_and_internal_only_bindings_override_default(cp: ControlPlane) -> None:
    direct_agent = _agent(cp, "direct")
    silent_agent = _agent(cp, "silent")
    hive = cp.configure_communication_identity("mac-hive", is_default=True)
    specialist = cp.configure_communication_identity("release-bot")
    cp.configure_representation_binding(
        "agent", direct_agent.id, identity_id=specialist.id, mode="direct"
    )
    cp.configure_representation_binding("agent", silent_agent.id, mode="internal_only")

    direct = cp.resolve_agent_representation(direct_agent.id)
    assert direct["mode"] == "direct"
    assert direct["identity"]["id"] == specialist.id
    silent = cp.resolve_agent_representation(silent_agent.id)
    assert silent["mode"] == "internal_only"
    assert silent["identity"] is None
    assert hive.is_default is True


def test_gateway_account_lease_is_singleton_and_fenced(cp: ControlPlane) -> None:
    first = _agent(cp, "first")
    second = _agent(cp, "second")
    hive = cp.configure_communication_identity("mac-hive", is_default=True)
    account = cp.configure_communication_account(hive.id, "telegram")

    lease = cp.acquire_gateway_identity_lease(account.id, first.id, lease_seconds=60)
    renewed = cp.renew_gateway_identity_lease(
        lease.id, first.id, lease.fencing_token, lease_seconds=120
    )
    assert renewed.id == lease.id
    assert renewed.fencing_token == lease.fencing_token
    with pytest.raises(TransitionError, match="held by"):
        cp.acquire_gateway_identity_lease(account.id, second.id)
    with pytest.raises(TransitionError, match="fencing token"):
        cp.release_gateway_identity_lease(lease.id, first.id, "wrong")

    cp.release_gateway_identity_lease(lease.id, first.id, lease.fencing_token)
    takeover = cp.acquire_gateway_identity_lease(account.id, second.id)
    assert takeover.agent_id == second.id
    assert takeover.fencing_token != lease.fencing_token


def test_outbox_claim_ack_retry_and_idempotency(cp: ControlPlane) -> None:
    origin = _agent(cp, "origin")
    gateway = _agent(cp, "gateway")
    hive = cp.configure_communication_identity("mac-hive", is_default=True)
    account = cp.configure_communication_account(
        hive.id, "slack", config={"default": True}
    )
    cp.acquire_gateway_identity_lease(account.id, gateway.id)

    delivery = cp.enqueue_human_message(
        "channel:C123",
        "Build completed",
        origin_agent_id=origin.id,
        channel="slack",
        idempotency_key="task-1-completed",
    )
    duplicate = cp.enqueue_human_message(
        "channel:C123",
        "Build completed",
        origin_agent_id=origin.id,
        channel="slack",
        idempotency_key="task-1-completed",
    )
    assert duplicate.id == delivery.id
    assert cp.claim_human_messages(origin.id) == []

    claimed = cp.claim_human_messages(gateway.id)
    assert [item.id for item in claimed] == [delivery.id]
    retry = cp.fail_human_message(delivery.id, gateway.id, "temporary outage")
    assert retry.status == "pending"
    assert retry.attempt_count == 1

    claimed_again = cp.claim_human_messages(gateway.id)
    assert [item.id for item in claimed_again] == [delivery.id]
    completed = cp.acknowledge_human_message(
        delivery.id,
        gateway.id,
        provider_message_id="1700000000.0001",
        detail={"ok": True},
    )
    assert completed.status == "delivered"
    assert completed.provider_message_id == "1700000000.0001"
    assert completed.metadata["provider_receipt"] == {"ok": True}


def test_pending_delivery_prevents_identity_and_account_deletion(cp: ControlPlane) -> None:
    origin = _agent(cp, "origin")
    hive = cp.configure_communication_identity("mac-hive", is_default=True)
    account = cp.configure_communication_account(hive.id, "slack")
    cp.enqueue_human_message(
        "channel:C123", "Queued", origin_agent_id=origin.id, channel="slack"
    )
    with pytest.raises(TransitionError, match="delivery history"):
        cp.delete_communication_account(account.id)
    with pytest.raises(TransitionError, match="delivery history"):
        cp.delete_communication_identity(hive.id)


def test_delivered_history_and_active_leases_block_destructive_deletion(
    cp: ControlPlane,
) -> None:
    origin = _agent(cp, "origin")
    gateway = _agent(cp, "gateway")
    hive = cp.configure_communication_identity("mac-hive", is_default=True)
    account = cp.configure_communication_account(hive.id, "slack")
    lease = cp.acquire_gateway_identity_lease(account.id, gateway.id)
    delivery = cp.enqueue_human_message(
        "channel:C123", "Queued", origin_agent_id=origin.id, channel="slack"
    )
    cp.claim_human_messages(gateway.id)
    cp.acknowledge_human_message(delivery.id, gateway.id)

    with pytest.raises(TransitionError, match="delivery history"):
        cp.delete_communication_account(account.id)
    with pytest.raises(TransitionError, match="delivery history"):
        cp.delete_communication_identity(hive.id)

    unused = cp.configure_communication_account(hive.id, "telegram")
    unused_lease = cp.acquire_gateway_identity_lease(unused.id, gateway.id)
    with pytest.raises(TransitionError, match="active gateway lease"):
        cp.delete_communication_account(unused.id)
    cp.release_gateway_identity_lease(
        unused_lease.id, gateway.id, unused_lease.fencing_token
    )
    cp.delete_communication_account(unused.id)

    cp.release_gateway_identity_lease(lease.id, gateway.id, lease.fencing_token)


def test_notifier_uses_representative_openclaw_outbox_instead_of_agent_message(
    cp: ControlPlane,
) -> None:
    worker = _agent(cp, "worker")
    hive = cp.configure_communication_identity("mac-hive", is_default=True)
    cp.configure_communication_account(
        hive.id, "slack", config={"default": True}
    )
    cp.configure_notifier_channel(
        "hive-status",
        "slack",
        event_types=["task.*"],
        target={
            "agent_id": worker.id,
            "channel_type": "slack",
            "external_id": "T123/C456",
        },
    )
    notification = cp.record_notification(
        "task.completed",
        "Task complete",
        "Build passed",
        subject_type="task",
        channels=["slack"],
    )

    result = cp.deliver_pending_notifications(notification_id=notification.id)
    deliveries = cp.list_human_messages()
    assert result["delivered"] == 1
    assert len(deliveries) == 1
    assert deliveries[0].identity_id == hive.id
    assert deliveries[0].origin_agent_id == worker.id
    assert deliveries[0].target == "channel:C456"
    assert deliveries[0].body == "[task.completed] Build passed"
    assert cp.list_messages(worker.id) == []


def test_identity_registry_does_not_suppress_internal_agent_notifications(
    cp: ControlPlane,
) -> None:
    worker = _agent(cp, "worker")
    cp.configure_communication_identity("mac-hive", is_default=True)
    cp.configure_notifier_channel(
        "internal-status",
        "hermes",
        event_types=["task.*"],
        target={"agent_id": worker.id},
    )
    notification = cp.record_notification(
        "task.completed",
        "Task complete",
        "Build passed",
        subject_type="task",
        channels=["hermes"],
    )

    result = cp.deliver_pending_notifications(notification_id=notification.id)

    assert result["delivered"] == 1
    assert cp.list_human_messages() == []
    assert len(cp.list_messages(worker.id)) == 1
