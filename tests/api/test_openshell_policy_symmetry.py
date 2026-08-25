"""Reads and writes of an OpenShell guardrail policy meet at the same scope.

PR #298 made the policy BODY admin-only because it names the fleet's hub and
gateway hosts, their ports, and the binaries permitted to reach them. It left
the mutations at `write`, which produced an indefensible split: a token could
CREATE, UPDATE, DELETE and ASSIGN the guardrail every ``--yolo`` agent runs
under, and then be refused when it tried to read back what it had just written.

Authoring a confinement is at least as privileged as reading one, so the two
meet at admin rather than the body being lowered to `write`.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from mac.api import _required_scope, create_app
from mac.services import ControlPlane

SECRET_HOST = "hub-internal.example.invalid"
POLICY_TEXT = (
    """version: 1

network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: %s
        port: 8789
"""
    % SECRET_HOST
)

#: Everything that mutates the guardrail resource.
WRITES = [
    ("POST", "/openshell/policies"),
    ("PUT", "/openshell/policies/%(policy)s"),
    ("DELETE", "/openshell/policies/%(policy)s"),
    ("POST", "/openshell/policies/%(policy)s/assignments"),
]
#: Everything that discloses the guardrail SOURCE.
BODY_READS = [
    ("GET", "/openshell/policies/%(policy)s"),
    ("GET", "/openshell/policies/%(policy)s/versions"),
    ("POST", "/openshell/policies/%(policy)s/render"),
]
#: Identity-only views that stay readable so drift detection keeps working.
IDENTITY_READS = [
    ("GET", "/openshell/policies"),
    ("GET", "/openshell/policies/%(policy)s/assignments"),
]


def _headers(token: str) -> dict:
    return {"Authorization": "Bearer %s" % token}


@pytest.fixture
def fleet():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    agent = cp.register_agent(machine.id, "worker-1")
    policy = cp.openshell.create_policy("fleet", POLICY_TEXT)
    cp.openshell.assign_policy(policy.id, target_type="agent", target_id=agent.id)
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "reader": {"scopes": ["read"], "client_id": "dashboard"},
                "writer": {"scopes": ["write"], "client_id": "ci"},
                "admin": {"scopes": ["admin"], "client_id": "operator"},
            },
        )
    )
    return client, cp, policy


def test_every_write_requires_the_same_scope_as_reading_the_body():
    """The property this change exists to establish."""
    body_scope = _required_scope("GET", "/openshell/policies/p1")
    assert body_scope == "admin"
    for method, template in WRITES:
        assert _required_scope(method, template % {"policy": "p1"}) == body_scope, (
            method,
            template,
        )


@pytest.mark.parametrize("method,template", WRITES + BODY_READS)
def test_guardrail_source_and_mutations_require_admin(method, template):
    assert _required_scope(method, template % {"policy": "p1"}) == "admin"


@pytest.mark.parametrize("method,template", IDENTITY_READS)
def test_identity_views_stay_readable(method, template):
    assert _required_scope(method, template % {"policy": "p1"}) == "read"


@pytest.mark.parametrize("method,template", WRITES)
def test_a_write_token_can_no_longer_author_a_guardrail(fleet, method, template):
    """`POST /openshell/policies` previously accepted a `write` token, so a
    task-writing credential could author the confinement every agent runs under."""
    client, _cp, policy = fleet
    response = client.request(
        method,
        template % {"policy": policy.id},
        headers=_headers("writer"),
        json={"name": "pwn", "policy_text": POLICY_TEXT},
    )
    assert response.status_code == 403


def test_an_operator_can_still_do_the_full_lifecycle(fleet):
    """Raising the writes must not break `mac openshell policy ...`."""
    client, _cp, _policy = fleet
    created = client.post(
        "/openshell/policies",
        headers=_headers("admin"),
        json={"name": "second", "policy_text": POLICY_TEXT},
    )
    assert created.status_code == 200
    new_id = created.json()["id"]

    assert (
        client.put(
            "/openshell/policies/%s" % new_id,
            headers=_headers("admin"),
            json={"policy_text": POLICY_TEXT.replace("8789", "9999")},
        ).status_code
        == 200
    )
    # ...and read back exactly what was written.
    fetched = client.get("/openshell/policies/%s" % new_id, headers=_headers("admin")).json()
    assert "9999" in fetched["policy_text"]
    assert (
        client.delete("/openshell/policies/%s" % new_id, headers=_headers("admin")).status_code
        == 200
    )


def test_write_and_read_back_is_coherent_for_the_one_scope_that_has_it(fleet):
    """The bug in one sentence: whatever principal can write must be able to
    read back. Assert it as a round trip rather than as two scope constants."""
    client, _cp, _policy = fleet
    created = client.post(
        "/openshell/policies",
        headers=_headers("admin"),
        json={"name": "roundtrip", "policy_text": POLICY_TEXT},
    )
    assert created.status_code == 200
    readback = client.get(
        "/openshell/policies/%s" % created.json()["id"], headers=_headers("admin")
    )
    assert readback.status_code == 200
    assert readback.json()["policy_text"] == POLICY_TEXT.strip()


def test_read_token_still_sees_no_guardrail_text_anywhere(fleet):
    """The #298 guarantee must survive this change."""
    client, _cp, policy = fleet
    for path in (
        "/openshell/policies",
        "/openshell/policies/%s/assignments" % policy.id,
        "/dashboard/state",
    ):
        response = client.get(path, headers=_headers("reader"))
        assert response.status_code == 200, path
        assert SECRET_HOST not in response.text, path
