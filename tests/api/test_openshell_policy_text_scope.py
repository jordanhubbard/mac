"""`policy_text` is privileged; policy identity is not.

A guardrail policy names the fleet's hub and gateway hosts, their ports, and the
exact binary paths permitted to reach them — a map of the control plane, and a
map of what an attacker would have to avoid. Creating, updating, deleting and
assigning a policy already required the global fleet principal, but every READ
of the same policy returned its full source to any dashboard-tier token,
including `/dashboard/state`, which embeds the whole policy corpus.

These tests pin the boundary in both directions: no text below admin, and the
non-text views stay readable so drift detection keeps working.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from mac.api import create_app, _required_scope
from mac.models import OpenShellPolicy, OpenShellPolicyVersion
from mac.services import ControlPlane

SECRET_HOST = "hub-internal.example.invalid"
POLICY_TEXT = """version: 1

network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: %s
        port: 8789
""" % SECRET_HOST


def _headers(token: str) -> dict:
    return {"Authorization": "Bearer %s" % token}


@pytest.fixture
def fleet():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    mine = cp.register_agent(machine.id, "worker-self")
    other = cp.register_agent(machine.id, "worker-other")
    policy = cp.openshell.create_policy("fleet", POLICY_TEXT)
    cp.openshell.assign_policy(policy.id, target_type="agent", target_id=mine.id)
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "reader": {"scopes": ["read"], "client_id": "dashboard"},
                "writer": {"scopes": ["write"], "client_id": "ci"},
                "admin": {"scopes": ["admin"], "client_id": "operator"},
                "mine": {"scopes": ["agent"], "agent_id": mine.id},
            },
        )
    )
    return client, cp, policy, mine, other


# --- default-safe serialisation -------------------------------------------
#
# Filtering per route is what failed: each route that serialised a policy leaked
# the text by accident. The default is the fix; the routes are downstream of it.


def test_policy_to_dict_omits_text_by_default():
    policy = OpenShellPolicy(
        id="p", name="n", description="", policy_text=POLICY_TEXT,
        parsed_metadata={}, version=1, checksum="sha256:x", created_by="h",
        updated_by="h", active=True, created_at="t", updated_at="t",
        deleted_at=None,
    )
    assert "policy_text" not in policy.to_dict()
    # Identity and drift detection survive.
    assert policy.to_dict()["checksum"] == "sha256:x"
    assert policy.to_dict()["version"] == 1
    assert policy.to_dict(include_text=True)["policy_text"] == POLICY_TEXT


def test_policy_version_to_dict_omits_text_by_default():
    """A version history is a history of guardrail sources — equally sensitive."""
    version = OpenShellPolicyVersion(
        id="v", policy_id="p", version=2, policy_text=POLICY_TEXT,
        parsed_metadata={}, checksum="sha256:x", created_by="h", created_at="t",
    )
    assert "policy_text" not in version.to_dict()
    assert version.to_dict(include_text=True)["policy_text"] == POLICY_TEXT


def test_parsed_metadata_is_a_safe_summary():
    """parsed_metadata is retained in the default payload, so it must not carry
    the hosts/ports/binaries the text does."""
    from mac.openshell_service import parse_policy_metadata

    assert SECRET_HOST not in repr(parse_policy_metadata(POLICY_TEXT))


# --- scope boundary --------------------------------------------------------


@pytest.mark.parametrize(
    "method,path_template",
    [
        ("GET", "/openshell/policies/%(policy)s"),
        ("GET", "/openshell/policies/%(policy)s/versions"),
        ("POST", "/openshell/policies/%(policy)s/render"),
    ],
)
def test_text_bearing_routes_require_admin(method, path_template):
    assert _required_scope(method, path_template % {"policy": "ospol_1"}) == "admin"


@pytest.mark.parametrize(
    "path_template",
    [
        "/openshell/policies",
        "/openshell/policies/%(policy)s/assignments",
        "/agents/%(agent)s/openshell/status",
        "/dashboard/state",
    ],
)
def test_identity_views_stay_readable(path_template):
    """Narrowing must not break drift detection or the dashboard."""
    assert (
        _required_scope("GET", path_template % {"policy": "ospol_1", "agent": "agent_1"})
        == "read"
    )


# --- no text reaches a read token, anywhere -------------------------------


@pytest.mark.parametrize(
    "route",
    [
        "/openshell/policies",
        "/openshell/policies/%(policy)s/assignments",
        "/agents/%(agent)s/openshell/status",
        "/dashboard/state",
    ],
)
def test_read_token_sees_no_policy_text_on_any_reachable_route(fleet, route):
    client, _cp, policy, mine, _other = fleet
    path = route % {"policy": policy.id, "agent": mine.id}
    response = client.get(path, headers=_headers("reader"))
    assert response.status_code == 200, path
    assert SECRET_HOST not in response.text, path


def test_status_still_reports_convergence_without_the_text(fleet):
    """The dashboard needs to know WHICH policy is assigned and whether the host
    converged — not the guardrail body."""
    client, _cp, policy, mine, _other = fleet
    body = client.get(
        "/agents/%s/openshell/status" % mine.id, headers=_headers("reader")
    ).json()
    assert body["policy"]["policy_id" if "policy_id" in body["policy"] else "id"] == policy.id
    assert body["policy"]["checksum"] == policy.checksum
    assert "policy_text" not in body["policy"]
    assert body["assignment"]["policy_id"] == policy.id


def test_dashboard_still_lists_policies_without_their_text(fleet):
    client, _cp, policy, _mine, _other = fleet
    body = client.get("/dashboard/state", headers=_headers("reader")).json()
    listed = [p for p in body["openshell_policies"] if p["id"] == policy.id]
    assert listed, "dashboard must still enumerate policies"
    assert "policy_text" not in listed[0]
    assert listed[0]["checksum"] == policy.checksum


def test_read_and_write_tokens_are_denied_the_text_routes(fleet):
    client, _cp, policy, _mine, _other = fleet
    for token in ("reader", "writer"):
        assert client.get(
            "/openshell/policies/%s" % policy.id, headers=_headers(token)
        ).status_code == 403
        assert client.get(
            "/openshell/policies/%s/versions" % policy.id, headers=_headers(token)
        ).status_code == 403
        assert client.post(
            "/openshell/policies/%s/render" % policy.id,
            headers=_headers(token),
            json={"agent_user": "u", "hub_host": "h.example", "hub_port": 8789},
        ).status_code == 403


def test_render_was_reachable_with_a_write_token_before_this_change(fleet):
    """Regression marker: a rendered policy is the template with the
    placeholders filled IN, so it is strictly more disclosive, not less."""
    client, _cp, policy, _mine, _other = fleet
    response = client.post(
        "/openshell/policies/%s/render" % policy.id,
        headers=_headers("writer"),
        json={"agent_user": "u", "hub_host": "h.example", "hub_port": 8789},
    )
    assert response.status_code == 403
    assert SECRET_HOST not in response.text


def test_admin_still_gets_the_text_it_needs(fleet):
    """`mac openshell policy show` must keep working for operators."""
    client, _cp, policy, _mine, _other = fleet
    body = client.get(
        "/openshell/policies/%s" % policy.id, headers=_headers("admin")
    ).json()
    assert body["policy_text"] == POLICY_TEXT.strip()

    versions = client.get(
        "/openshell/policies/%s/versions" % policy.id, headers=_headers("admin")
    ).json()
    assert versions and "policy_text" in versions[0]


def test_agent_self_service_route_is_unaffected(fleet):
    """The worker still converges: it needs the text for its OWN policy."""
    client, _cp, policy, mine, _other = fleet
    body = client.get(
        "/agents/%s/openshell/policy" % mine.id, headers=_headers("mine")
    ).json()
    assert body["policy_text"] == POLICY_TEXT.strip()
    assert body["checksum"] == policy.checksum


# --- operator CLI must not lose the body it exists to print ----------------


def _cli_json(fn, namespace, control_plane):
    """Run a CLI handler against a direct ControlPlane and parse its JSON."""
    import contextlib
    import io
    import json

    from mac import cli

    prior_plane, prior_json = cli._plane, cli._OUTPUT_JSON
    cli._plane, cli._OUTPUT_JSON = (lambda _args: control_plane), True
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(namespace)
        return json.loads(buf.getvalue())
    finally:
        cli._plane, cli._OUTPUT_JSON = prior_plane, prior_json


def test_cli_policy_show_and_versions_keep_the_text_on_the_db_path(fleet):
    """`_print` calls `to_dict()` with no arguments, so defaulting the text out
    silently emptied `mac openshell policy show` on the --db path -- the one
    thing that command exists to display."""
    from argparse import Namespace

    from mac import cli

    _client, cp, policy, _mine, _other = fleet

    shown = _cli_json(
        cli.cmd_openshell_policy_show, Namespace(policy=policy.id), cp
    )
    assert shown["policy_text"] == policy.policy_text

    versions = _cli_json(
        cli.cmd_openshell_policy_versions, Namespace(policy=policy.id), cp
    )
    assert versions and all("policy_text" in v for v in versions)


def test_cli_policy_list_stays_text_free(fleet):
    """Listing is an inventory view; it never needed the bodies."""
    from argparse import Namespace

    from mac import cli

    _client, cp, _policy, _mine, _other = fleet
    listed = _cli_json(
        cli.cmd_openshell_policy_list, Namespace(include_deleted=False), cp
    )
    assert listed and all("policy_text" not in item for item in listed)
