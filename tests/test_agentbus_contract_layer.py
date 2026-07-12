"""AgentBus contract layer (task_0d50e190, audit 7/7).

Schema registry enforced at publish for declared registered schemas,
advisory for unknown ones; standard error taxonomy; hub-durable consumer
cursors; first-class request/reply with deadline + timeout error payload.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mac.agentbus_schemas import error_payload
from mac.api import create_app
from mac.models import ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp() -> ControlPlane:
    return ControlPlane.in_memory()


def _agents(cp: ControlPlane, *names: str):
    machine = cp.register_machine("contract-host")
    return [
        cp.register_agent(machine.id, name, agent_id="agent_%s" % name)
        for name in names
    ]


def test_registered_schemas_are_enforced_at_publish(cp: ControlPlane) -> None:
    natasha, rocky = _agents(cp, "natasha", "rocky")
    # Valid registered payload publishes.
    cp.publish_agentbus_content(
        sender_agent_id=natasha.id,
        recipient_agent_id=rocky.id,
        topic="peer.message.v1",
        payload={"schema": "mac.agent.peer_message.v1", "message": "hi"},
    )
    # Missing required field: the PRODUCER learns immediately.
    with pytest.raises(ValidationError, match="missing required field: message"):
        cp.publish_agentbus_content(
            sender_agent_id=natasha.id,
            recipient_agent_id=rocky.id,
            payload={"schema": "mac.agent.peer_message.v1"},
        )
    # Wrong type is refused too.
    with pytest.raises(ValidationError, match="wrong type"):
        cp.publish_agentbus_content(
            sender_agent_id=natasha.id,
            recipient_agent_id=rocky.id,
            payload={"schema": "mac.agent.peer_message.v1", "message": 42},
        )
    # Unknown schema names stay advisory (ad-hoc experimentation), but leave
    # an observability trace.
    cp.publish_agentbus_content(
        sender_agent_id=natasha.id,
        recipient_agent_id=rocky.id,
        payload={"schema": "mac.experimental.v0", "whatever": True},
    )
    events = cp.list_observability(name="agentbus.schema.unregistered", limit=5)
    assert events and events[0].detail["schema"] == "mac.experimental.v0"


def test_error_taxonomy_is_closed_and_shaped(cp: ControlPlane) -> None:
    payload = error_payload("timeout", "no reply", retryable=True, correlation_id="c1")
    assert payload["schema"] == "mac.agentbus.error.v1"
    assert payload["code"] == "timeout" and payload["retryable"] is True
    with pytest.raises(ValidationError, match="unknown agentbus error code"):
        error_payload("oops", "nope")
    # The error schema itself passes registry validation.
    natasha, rocky = _agents(cp, "natasha", "rocky")
    cp.publish_agentbus_content(
        sender_agent_id=natasha.id, recipient_agent_id=rocky.id, payload=payload
    )


def test_consumer_cursors_survive_hub_side(cp: ControlPlane) -> None:
    (natasha,) = _agents(cp, "natasha")
    assert cp.get_agentbus_consumer_cursor(natasha.id, "peer.message.v1") is None
    position = {"watermark": "2026-07-12T21:00:00+00:00", "groupCursors": {"bus_1": 4}}
    saved = cp.set_agentbus_consumer_cursor(natasha.id, "peer.message.v1", position)
    assert saved["position"] == position
    loaded = cp.get_agentbus_consumer_cursor(natasha.id, "peer.message.v1")
    assert loaded["position"] == position
    # Upsert, and bounded.
    cp.set_agentbus_consumer_cursor(natasha.id, "peer.message.v1", {"watermark": "x"})
    assert cp.get_agentbus_consumer_cursor(natasha.id, "peer.message.v1")["position"] == {
        "watermark": "x"
    }
    with pytest.raises(ValidationError, match="exceeds"):
        cp.set_agentbus_consumer_cursor(
            natasha.id, "peer.message.v1", {"blob": "y" * 9000}
        )


def test_request_reply_endpoint_correlates_and_times_out(cp: ControlPlane) -> None:
    natasha, rocky = _agents(cp, "natasha", "rocky")
    app = create_app(
        control_plane=cp,
        auth_tokens={"agent-token": {"scopes": ["agent"], "agent_id": natasha.id}},
    )
    headers = {"Authorization": "Bearer agent-token"}
    with TestClient(app) as client:
        # Happy path: the reply (pre-published with a matching correlation id,
        # as the peer bridge would) is found and returned.
        cp.publish_agentbus_content(
            sender_agent_id=rocky.id,
            recipient_agent_id=natasha.id,
            topic="peer.reply.v1",
            headers={"correlation_id": "corr-42"},
            payload={
                "schema": "mac.agent.peer_reply.v1",
                "correlation_id": "corr-42",
                "reply": "benchmark done: 118 TFLOPS",
            },
        )
        replied = client.post(
            "/agentbus/request",
            headers=headers,
            json={
                "sender_agent_id": natasha.id,
                "recipient_agent_id": rocky.id,
                "payload": {
                    "schema": "mac.agent.peer_message.v1",
                    "message": "rerun the benchmark",
                },
                "correlation_id": "corr-42",
                "deadline_seconds": 5,
            },
        )
        # Deadline of zero: immediate standard timeout error payload.
        timed_out = client.post(
            "/agentbus/request",
            headers=headers,
            json={
                "sender_agent_id": natasha.id,
                "recipient_agent_id": rocky.id,
                "payload": {
                    "schema": "mac.agent.peer_message.v1",
                    "message": "anyone there?",
                },
                "deadline_seconds": 0,
            },
        )
        # An agent-bound token cannot request on behalf of a peer.
        forged = client.post(
            "/agentbus/request",
            headers=headers,
            json={
                "sender_agent_id": rocky.id,
                "recipient_agent_id": natasha.id,
                "payload": {"schema": "mac.agent.peer_message.v1", "message": "x"},
                "deadline_seconds": 0,
            },
        )

    assert replied.status_code == 200
    body = replied.json()
    assert body["status"] == "replied"
    assert body["reply"]["reply"] == "benchmark done: 118 TFLOPS"
    assert body["request_stream"]["topic"] == "peer.message.v1"

    assert timed_out.status_code == 200
    timeout_body = timed_out.json()
    assert timeout_body["status"] == "timeout"
    assert timeout_body["reply"]["schema"] == "mac.agentbus.error.v1"
    assert timeout_body["reply"]["code"] == "timeout"
    assert timeout_body["reply"]["retryable"] is True

    assert forged.status_code == 403
