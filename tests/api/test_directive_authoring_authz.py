"""Authoring a directive is operator speech, so it requires an operator.

This codebase already states the principle: `/agentbus/human-directive` is admin
because human directives are "never mintable via the agent scope (authority =
attested provenance)". The directive *authoring* lifecycle carried only `write`,
which is the same provenance problem — a token that writes tasks could propose,
approve and activate a directive that changes fleet-wide behaviour, claiming an
authority nobody granted it.

The two boundaries that must NOT move are pinned here too: the read models stay
readable, and the agent-side distribution paths keep the `agent` scope so a
worker still receives and acknowledges directives normally.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from mac.api import _required_scope, create_app
from mac.services import ControlPlane

AUTHORING = [
    ("POST", "/directives"),
    ("POST", "/directives/d1/check"),
    ("POST", "/directives/d1/approve"),
    ("POST", "/directives/d1/activate"),
    ("POST", "/directives/d1/deactivate"),
    ("POST", "/directives/d1/waivers"),
    ("POST", "/directive-bindings"),
    ("POST", "/directive-waivers/w1/revoke"),
]

READ_MODELS = [
    "/directives",
    "/directives/effective",
    "/directives/d1",
    "/directives/d1/versions",
    "/directives/d1/impact",
    "/directive-bindings",
    "/directive-waivers",
]

AGENT_DISTRIBUTION = [
    ("GET", "/agents/a1/directives/effective"),
    ("POST", "/agents/a1/directive-activations/x1/ack"),
]


def _headers(token: str) -> dict:
    return {"Authorization": "Bearer %s" % token}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MAC_DIRECTIVES_ENABLED", "1")
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    agent = cp.register_agent(machine.id, "worker-1")
    return TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "reader": {"scopes": ["read"], "client_id": "dashboard"},
                "writer": {"scopes": ["write"], "client_id": "ci"},
                "admin": {"scopes": ["admin"], "client_id": "operator"},
                "worker": {"scopes": ["agent"], "agent_id": agent.id},
            },
        )
    ), agent


@pytest.mark.parametrize("method,path", AUTHORING)
def test_authoring_requires_admin(method, path):
    assert _required_scope(method, path) == "admin"


@pytest.mark.parametrize("path", READ_MODELS)
def test_read_models_stay_readable(path):
    """Directive documents, versions and impact are a read model, not a secret;
    operators and dashboards depend on them."""
    assert _required_scope("GET", path) == "read"


@pytest.mark.parametrize("method,path", AGENT_DISTRIBUTION)
def test_agent_distribution_keeps_the_agent_scope(method, path):
    """A worker must still receive and acknowledge directives. These are matched
    earlier than the authoring rule; if that ordering ever breaks, directive
    delivery silently stops fleet-wide."""
    assert _required_scope(method, path) == "agent"


@pytest.mark.parametrize("method,path", AUTHORING)
def test_a_write_token_can_no_longer_author_a_directive(client, method, path):
    api, _agent = client
    response = api.request(
        method,
        path,
        headers=_headers("writer"),
        json={"document": {}, "actor": "ci"},
    )
    assert response.status_code == 403


def test_a_write_token_previously_reached_the_service_layer(client):
    """POST /directives/{id}/check used to get past the guard and raise a
    TypeError from inside DirectiveService — proof it was executing business
    logic, not merely failing validation."""
    api, _agent = client
    response = api.post(
        "/directives/d1/check", headers=_headers("writer"), json={}
    )
    assert response.status_code == 403


def test_an_operator_can_still_propose(client):
    """Raising the scope must not break `mac directive propose`."""
    api, _agent = client
    response = api.post(
        "/directives",
        headers=_headers("admin"),
        json={
            "document": {
                "schema": "mac.directive.v1",
                "name": "build.example",
                "description": "Example.",
                "scope": "fleet",
                "when": {"eq": [{"literal": 1}, {"literal": 1}]},
                "set": {"example.enabled": True},
            },
            "actor": "operator",
        },
    )
    assert response.status_code == 200


def test_a_reader_can_still_list_directives(client):
    api, _agent = client
    assert api.get("/directives", headers=_headers("reader")).status_code == 200
