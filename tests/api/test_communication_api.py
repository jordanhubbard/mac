from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _agent(cp: ControlPlane, name: str):
    machine = cp.register_machine("host-%s" % name)
    return cp.register_agent(machine.id, name, agent_id="agent_%s" % name)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer %s" % token}


def test_communication_api_shared_identity_lease_and_delivery_round_trip() -> None:
    cp = ControlPlane.in_memory()
    origin = _agent(cp, "origin")
    gateway = _agent(cp, "gateway")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "admin": ["admin"],
                "reader": ["read"],
                "origin": {"scopes": ["agent"], "agent_id": origin.id},
                "gateway": {"scopes": ["agent"], "agent_id": gateway.id},
            },
        )
    )

    identity_response = client.post(
        "/communication/identities",
        headers=_headers("admin"),
        json={"name": "mac-hive", "display_name": "MAC Hive", "is_default": True},
    )
    assert identity_response.status_code == 200
    identity = identity_response.json()
    account_response = client.post(
        "/communication/accounts",
        headers=_headers("admin"),
        json={
            "identity_id": identity["id"],
            "channel": "slack",
            "account_id": "operations",
            "credential_refs": {"bot": "secret://slack-bot"},
            "config": {"default": True},
        },
    )
    assert account_response.status_code == 200
    account = account_response.json()

    resolution = client.get("/agents/%s/representation" % origin.id, headers=_headers("reader"))
    assert resolution.status_code == 200
    assert resolution.json()["identity"]["name"] == "mac-hive"

    lease_response = client.post(
        "/communication/gateway-leases/acquire",
        headers=_headers("gateway"),
        json={"account_id": account["id"], "agent_id": gateway.id},
    )
    assert lease_response.status_code == 200

    enqueue_response = client.post(
        "/communication/deliveries",
        headers=_headers("origin"),
        json={
            "target": "channel:C123",
            "body": "Task complete",
            "origin_agent_id": origin.id,
            "channel": "slack",
            "idempotency_key": "api-roundtrip",
        },
    )
    assert enqueue_response.status_code == 200
    delivery = enqueue_response.json()
    claim_response = client.post(
        "/communication/deliveries/claim",
        headers=_headers("gateway"),
        json={"agent_id": gateway.id},
    )
    assert claim_response.status_code == 200
    assert [item["id"] for item in claim_response.json()] == [delivery["id"]]

    ack_response = client.post(
        "/communication/deliveries/%s/ack" % delivery["id"],
        headers=_headers("gateway"),
        json={"agent_id": gateway.id, "provider_message_id": "123.456"},
    )
    assert ack_response.status_code == 200
    assert ack_response.json()["status"] == "delivered"


def test_communication_agent_endpoints_bind_actor_to_token() -> None:
    cp = ControlPlane.in_memory()
    first = _agent(cp, "first")
    second = _agent(cp, "second")
    hive = cp.configure_communication_identity("mac-hive", is_default=True)
    account = cp.configure_communication_account(hive.id, "telegram")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "first": {"scopes": ["agent"], "agent_id": first.id},
            },
        )
    )

    response = client.post(
        "/communication/gateway-leases/acquire",
        headers=_headers("first"),
        json={"account_id": account.id, "agent_id": second.id},
    )
    assert response.status_code == 403

    enqueue = client.post(
        "/communication/deliveries",
        headers=_headers("first"),
        json={
            "target": "42",
            "body": "No impersonation",
            "origin_agent_id": second.id,
            "channel": "telegram",
        },
    )
    assert enqueue.status_code == 403
