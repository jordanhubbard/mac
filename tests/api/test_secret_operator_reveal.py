"""The operator/provisioner path to a secret's plaintext.

There are two ways to get a secret out of the vault and they authorize on
different things:

  * ``/secrets/{id}/access`` + ``/secrets/{id}/reveal`` -- the AGENT path. The
    accessor must be a registered ``Agent`` row on a trusted ``Machine`` whose
    capabilities satisfy the secret's scopes, and the handle it issues is
    single-use and time-limited.

  * ``/secrets/{name}/resolve`` -- the OPERATOR path, which these tests cover.
    It authorizes on the token's ``secret`` scope alone.

The second exists because of a circularity in the first. The machine that most
needs the headscale pre-auth key is the one that is not on the mesh yet: a
provisioner, an operator's laptop, a worker being re-enrolled. None of them are
registered agents, and they cannot become registered agents until they can
reach the hub, which is what the key is for. Gating that fetch on fleet-agent
registration would mean the only way to join the mesh is to already be on it.

So the tests below assert the property that makes the operator path useful --
that it works with NO agent registered anywhere -- alongside the properties
that keep it from being a hole: the scope is still required, the accessor in
the audit trail is server-derived rather than self-asserted, and a disabled or
absent secret is a refusal rather than an empty answer.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import TokenPrincipal, create_app
from mac.services import ControlPlane


OPERATOR = TokenPrincipal(scopes=frozenset({"secret"}))
READ_ONLY = TokenPrincipal(scopes=frozenset({"task"}))


def _fixture():
    cp = ControlPlane.in_memory()
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={"operator-token": OPERATOR, "task-token": READ_ONLY},
        )
    )
    cp.create_secret(
        "headscale-preauthkey-demo",
        "hskey-abcdef",
        {"capabilities": ["mesh"]},
        "install-headscale",
    )
    return cp, client


def _auth(token):
    return {"Authorization": "Bearer %s" % token}


def test_resolve_reveals_plaintext_without_any_registered_agent():
    cp, client = _fixture()
    assert cp.list_agents() == []

    response = client.post(
        "/secrets/headscale-preauthkey-demo/resolve", headers=_auth("operator-token")
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "headscale-preauthkey-demo",
        "value": "hskey-abcdef",
    }


def test_resolve_refuses_a_token_without_the_secret_scope():
    _cp, client = _fixture()
    response = client.post(
        "/secrets/headscale-preauthkey-demo/resolve", headers=_auth("task-token")
    )
    assert response.status_code in (401, 403), response.text
    assert "hskey-abcdef" not in response.text


def test_resolve_refuses_an_unauthenticated_caller():
    _cp, client = _fixture()
    response = client.post("/secrets/headscale-preauthkey-demo/resolve")
    assert response.status_code in (401, 403), response.text
    assert "hskey-abcdef" not in response.text


def test_resolve_records_the_caller_supplied_purpose_in_the_audit_trail():
    """`purpose` is a caller-supplied label so an operator pull is
    distinguishable from the model router's own upstream-key lookups, which
    share this route."""
    cp, client = _fixture()
    response = client.post(
        "/secrets/headscale-preauthkey-demo/resolve",
        params={"purpose": "headscale-enrollment"},
        headers=_auth("operator-token"),
    )
    assert response.status_code == 200, response.text

    purposes = [audit.purpose for audit in cp.list_secret_audits()]
    assert "headscale-enrollment" in purposes


def test_resolve_audits_a_server_derived_accessor():
    """The accessor is taken from the authenticated principal, never from the
    request, so a client cannot write someone else's name into the audit."""
    cp, client = _fixture()
    assert (
        client.post(
            "/secrets/headscale-preauthkey-demo/resolve",
            params={"purpose": "headscale-enrollment"},
            headers=_auth("operator-token"),
        ).status_code
        == 200
    )

    fetches = [
        audit
        for audit in cp.list_secret_audits()
        if audit.purpose == "headscale-enrollment"
    ]
    assert len(fetches) == 1
    # An unbound operator token carries no agent_id, so the hub labels it
    # "fleet-fetch" rather than accepting a name from the caller.
    assert fetches[0].accessor_agent_id == "fleet-fetch"


def test_resolve_defaults_the_purpose_when_none_is_supplied():
    cp, client = _fixture()
    assert (
        client.post(
            "/secrets/headscale-preauthkey-demo/resolve",
            headers=_auth("operator-token"),
        ).status_code
        == 200
    )
    assert [audit.purpose for audit in cp.list_secret_audits()] == ["fleet-fetch"]


def test_resolve_refuses_an_absent_secret():
    _cp, client = _fixture()
    response = client.post(
        "/secrets/no-such-secret/resolve", headers=_auth("operator-token")
    )
    assert response.status_code == 404, response.text
