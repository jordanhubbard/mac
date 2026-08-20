"""Reveal-by-name is the path for a caller that is not a registered agent.

`POST /secrets/{id}/access` + `/reveal` is the AGENT path: `reveal_secret`
matches ``accessor_agent_id`` against an Agent row on a trusted Machine. An
operator's laptop, or the provisioner host that is about to join the fleet's
mesh, is neither -- and requiring fleet-agent registration in order to fetch
the enrollment key that lets you JOIN the fleet is circular.

`POST /secrets/{name}/resolve` is that other path, and `mac admin secret get`
is its CLI. The credential is the ``secret`` token scope, which need not be
bound to any agent. These tests pin both halves: that an unbound operator token
works, and that the ordinary authorization boundaries (scope, tenant, rate
limit) still apply to it -- resolve was the one route in the /secrets/* family
that skipped the tenant check.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _fleet():
    cp = ControlPlane.in_memory()
    cp.create_secret(
        "headscale-preauthkey-demo",
        "hskey-abc123",
        {"capabilities": ["deploy", "mesh-join"]},
        created_by="install-headscale.sh",
    )
    return cp


def test_operator_token_reveals_by_name_without_being_an_agent():
    cp = _fleet()
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                # No agent_id, no tenant: an operator/provisioner credential.
                "operator": {"scopes": ["secret"]},
            },
        )
    )

    resolved = client.post(
        "/secrets/headscale-preauthkey-demo/resolve",
        headers={"Authorization": "Bearer operator"},
        json={"purpose": "mesh-join"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json() == {
        "name": "headscale-preauthkey-demo",
        "value": "hskey-abc123",
    }

    # The declared purpose lands on the audit row: "who fetched the mesh key,
    # and why" is the whole point of routing this through the vault.
    audits = cp.list_secret_audits()
    assert [a.purpose for a in audits] == ["mesh-join"]
    assert audits[0].result == "granted"


def test_resolve_defaults_the_purpose_for_callers_that_send_no_body():
    """The Slack/forge fetchers POST with no body at all; that must keep working."""
    cp = _fleet()
    client = TestClient(
        create_app(control_plane=cp, auth_tokens={"operator": {"scopes": ["secret"]}})
    )
    resolved = client.post(
        "/secrets/headscale-preauthkey-demo/resolve",
        headers={"Authorization": "Bearer operator"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["value"] == "hskey-abc123"
    assert [a.purpose for a in cp.list_secret_audits()] == ["fleet-fetch"]


def test_resolve_still_requires_the_secret_scope():
    cp = _fleet()
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "writer": {"scopes": ["write"]},
                "operator": {"scopes": ["secret"]},
            },
        )
    )
    refused = client.post(
        "/secrets/headscale-preauthkey-demo/resolve",
        headers={"Authorization": "Bearer writer"},
    )
    assert refused.status_code == 403, refused.text
    assert "hskey-abc123" not in refused.text


def test_resolve_is_tenant_isolated_like_every_other_secret_route():
    """mac-01g0 applies here too.

    `_assert_secret_tenant` guards access/reveal, but resolve-by-name skipped
    it -- so a tenant-bound `secret` token could name any secret in the fleet
    and read it back, which is exactly the boundary the other routes exist to
    hold. Promoting resolve to the operator-facing fetch path makes that gap
    reachable from the CLI, so it is closed here.
    """
    cp = ControlPlane.in_memory()
    cp.create_secret(
        "tenant-a-key", "a-value", {"capabilities": ["deploy"], "tenant_id": "tenant-a"}, "human"
    )
    cp.create_secret("global-key", "g-value", {"capabilities": ["deploy"]}, "human")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "tok-a": {"scopes": ["secret"], "tenant_id": "tenant-a"},
                "tok-b": {"scopes": ["secret"], "tenant_id": "tenant-b"},
                "operator": {"scopes": ["secret"]},
            },
        )
    )

    own = client.post(
        "/secrets/tenant-a-key/resolve", headers={"Authorization": "Bearer tok-a"}
    )
    assert own.status_code == 200, own.text
    assert own.json()["value"] == "a-value"

    cross = client.post(
        "/secrets/tenant-a-key/resolve", headers={"Authorization": "Bearer tok-b"}
    )
    assert cross.status_code == 403, cross.text
    assert "a-value" not in cross.text

    # An unscoped (fleet-global) secret is off limits to a tenant-bound token,
    # and reachable by the untenanted operator credential.
    globalled = client.post(
        "/secrets/global-key/resolve", headers={"Authorization": "Bearer tok-a"}
    )
    assert globalled.status_code == 403, globalled.text
    ok = client.post(
        "/secrets/global-key/resolve", headers={"Authorization": "Bearer operator"}
    )
    assert ok.status_code == 200, ok.text


def test_resolve_of_a_missing_secret_is_a_not_found_not_an_empty_value():
    cp = _fleet()
    client = TestClient(
        create_app(control_plane=cp, auth_tokens={"operator": {"scopes": ["secret"]}})
    )
    missing = client.post(
        "/secrets/no-such-secret/resolve", headers={"Authorization": "Bearer operator"}
    )
    assert missing.status_code == 404, missing.text


def test_remote_dispatch_sends_the_purpose_and_never_the_accessor():
    """The CLI's hub-mode seam.

    `resolve_secret` takes an `accessor` for signature parity with the
    ControlPlane method that `--db` mode calls, but must not put it on the
    wire: over HTTP the hub derives the accessor from the bearer token, and a
    caller-supplied identity on an audit row is a claim, not a fact.
    """
    from mac.dispatch import RemoteDispatch

    class _Recording:
        def __init__(self):
            self.calls = []

        def request(self, method, path, body=None):
            self.calls.append((method, path, body))
            return {"name": "headscale-preauthkey-demo", "value": "hskey-abc123"}

    client = _Recording()
    resolved = RemoteDispatch(client).resolve_secret(
        "headscale-preauthkey-demo", purpose="mesh-join", accessor="laptop"
    )

    assert resolved.to_dict()["value"] == "hskey-abc123"
    assert client.calls == [
        ("POST", "/secrets/headscale-preauthkey-demo/resolve", {"purpose": "mesh-join"})
    ]


def test_resolve_is_rate_limited_for_non_admin_tokens():
    """mac-xc8u: a leaked `secret` token must not enumerate the vault at line rate."""
    cp = _fleet()
    client = TestClient(
        create_app(control_plane=cp, auth_tokens={"operator": {"scopes": ["secret"]}})
    )
    statuses = [
        client.post(
            "/secrets/headscale-preauthkey-demo/resolve",
            headers={"Authorization": "Bearer operator"},
        ).status_code
        for _ in range(32)
    ]
    assert 403 in statuses
