"""Self-only delivery of the assigned OpenShell policy over HTTP.

A guardrail policy names the fleet's hub and gateway hosts and the binary paths
permitted to reach them. This route exists so a worker can converge onto its
assignment; it must not become a way to read the fleet's confinement map with a
generic read token, or to read another agent's policy with an agent token.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane

POLICY_TEXT = """version: 1

network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: hub.example.com
        port: 8789
"""


def _headers(token: str) -> dict:
    return {"Authorization": "Bearer %s" % token}


def _app():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("worker-host")
    mine = cp.register_agent(machine.id, "worker-self")
    other = cp.register_agent(machine.id, "worker-other")
    policy = cp.openshell.create_policy("fleet", POLICY_TEXT)
    cp.openshell.assign_policy(policy.id, target_type="agent", target_id=mine.id)
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "admin": {"scopes": ["admin"], "client_id": "operator"},
                "reader": {"scopes": ["read"], "client_id": "dashboard"},
                "mine": {"scopes": ["agent"], "agent_id": mine.id},
                "other": {"scopes": ["agent"], "agent_id": other.id},
            },
        )
    )
    return client, cp, mine, other, policy


def test_agent_can_fetch_its_own_assigned_policy() -> None:
    client, _cp, mine, _other, policy = _app()
    response = client.get("/agents/%s/openshell/policy" % mine.id, headers=_headers("mine"))
    assert response.status_code == 200
    body = response.json()
    assert body["policy_text"] == POLICY_TEXT.strip()
    assert body["policy_id"] == policy.id
    assert body["checksum"] == policy.checksum


def test_an_agent_cannot_read_another_agents_policy() -> None:
    """The whole point of binding the path agent to the token principal."""
    client, _cp, mine, _other, _policy = _app()
    response = client.get("/agents/%s/openshell/policy" % mine.id, headers=_headers("other"))
    assert response.status_code == 403


def test_a_read_token_cannot_reach_the_policy() -> None:
    """A GET would otherwise fall through to the generic "read" scope, handing
    the fleet's confinement map to any dashboard credential."""
    client, _cp, mine, _other, _policy = _app()
    response = client.get("/agents/%s/openshell/policy" % mine.id, headers=_headers("reader"))
    assert response.status_code == 403


def test_unauthenticated_access_is_rejected() -> None:
    client, _cp, mine, _other, _policy = _app()
    assert client.get("/agents/%s/openshell/policy" % mine.id).status_code in {401, 403}


def test_agent_without_an_assignment_gets_404_not_an_empty_policy() -> None:
    """An empty policy would read as "no confinement required" at the far end."""
    client, _cp, _mine, other, _policy = _app()
    response = client.get("/agents/%s/openshell/policy" % other.id, headers=_headers("other"))
    assert response.status_code == 404
