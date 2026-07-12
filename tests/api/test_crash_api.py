from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def test_crash_api_binds_agent_identity_and_exposes_normalized_read_model():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("crashed-host")
    crashed = cp.register_agent(
        machine.id, "crashed", capabilities=["python", "ops"]
    )
    peer_machine = cp.register_machine("peer-host")
    cp.register_agent(peer_machine.id, "peer", capabilities=["python", "ops"])
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "crashed-token": {"scopes": ["agent"], "agent_id": crashed.id},
                "other-token": {"scopes": ["agent"], "agent_id": "agent_other"},
                "reader": ["read"],
                "secret-reader": ["read", "secret"],
                "admin": ["admin"],
            },
        )
    )
    payload = {
        "event_id": "evt-api-1",
        "supervisor": "launchd",
        "process_name": "mac-agent-service",
        "exit_code": 1,
        "revision": "deadbeef",
        "stack_trace": "Traceback (most recent call last):\nRuntimeError: crash",
    }

    denied = client.post(
        "/agents/%s/crash-reports" % crashed.id,
        headers={"Authorization": "Bearer other-token"},
        json=payload,
    )
    assert denied.status_code == 403

    created = client.post(
        "/agents/%s/crash-reports" % crashed.id,
        headers={"Authorization": "Bearer crashed-token"},
        json=payload,
    )
    assert created.status_code == 200
    report = created.json()
    assert report["schema"] == "mac.agent_crash_report.v1"
    assert report["repair_task_id"]

    listed = client.get(
        "/crash-reports?agent_id=%s" % crashed.id,
        headers={"Authorization": "Bearer reader"},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [report["id"]]
    assert client.get(
        "/crash-reports/%s" % report["id"],
        headers={"Authorization": "Bearer reader"},
    ).status_code == 403
    detail = client.get(
        "/crash-reports/%s" % report["id"],
        headers={"Authorization": "Bearer secret-reader"},
    )
    assert detail.status_code == 200
    assert detail.json()["occurrences"][0]["event_id"] == "evt-api-1"

    assert client.post(
        "/crash-reports/%s/resolve" % report["id"],
        headers={"Authorization": "Bearer reader"},
        json={"reason": "fixed"},
    ).status_code == 403
    resolved = client.post(
        "/crash-reports/%s/resolve" % report["id"],
        headers={"Authorization": "Bearer admin"},
        json={"reason": "fixed", "actor": "operator"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
