"""The executable half of `docs/authority-boundary.md`.

The lesson that produced that document is that a prose invariant is not an
invariant. `require_global_fleet` *read* as "require fleet-level authority" and
meant "refuse tenant-bound tokens"; the difference was documented nowhere and
believed at 61 call sites, and two holes followed. So the boundary is asserted
here rather than described.

Three things are pinned:

1. `refuse_tenant_bound` answers a tenancy question and cannot be an
   authorization gate — an untenanted token of any scope passes it.
2. `_required_scope` is therefore the gate, and the routes that hand out an
   execution grant are admin-only there.
3. Consent and execution grant are separate: holding a lease is not permission
   to escape the sandbox.
"""

from __future__ import annotations

import pytest

from mac.api import TokenPrincipal, _required_scope
from mac.models import AuthorizationError

#: Routes that hand out an execution grant — the ability to run something, or to
#: author the confinement something else runs under. Each was reachable with a
#: non-admin token before PR #300 / #303.
EXECUTION_GRANT_ROUTES = [
    # Authoring the guardrail policy every --yolo agent runs under.
    ("POST", "/openshell/policies"),
    ("GET", "/openshell/policies/p1"),
    ("PUT", "/openshell/policies/p1"),
    ("DELETE", "/openshell/policies/p1"),
    ("GET", "/openshell/policies/p1/versions"),
    ("POST", "/openshell/policies/p1/render"),
    ("POST", "/openshell/policies/p1/assignments"),
    # Directives are operator speech: they change fleet-wide behaviour.
    ("POST", "/directives"),
    ("POST", "/directives/d1/approve"),
    ("POST", "/directives/d1/activate"),
    ("POST", "/directives/d1/deactivate"),
    ("POST", "/directives/d1/check"),
    ("POST", "/directives/d1/waivers"),
    ("POST", "/directive-bindings"),
    ("POST", "/directive-waivers/w1/revoke"),
]

#: Reads that must stay reachable, or drift detection and the dashboard break.
#: Listed so that tightening the rows above cannot silently take these with it.
IDENTITY_READS = [
    ("GET", "/openshell/policies"),
    ("GET", "/openshell/policies/p1/assignments"),
    ("GET", "/agents/a1/openshell/status"),
    ("GET", "/dashboard/state"),
    ("GET", "/directives"),
    ("GET", "/directives/d1"),
]

#: Self-only worker control paths. A worker must keep receiving its own policy
#: and directives; if this ordering breaks, distribution stops fleet-wide and
#: silently.
SELF_ONLY_WORKER_PATHS = [
    ("GET", "/agents/a1/directives/effective"),
    ("POST", "/agents/a1/directive-activations/x1/ack"),
    ("GET", "/agents/a1/openshell/policy"),
]


# --- 1. the tenancy check is not an authorization gate ---------------------


@pytest.mark.parametrize("scope", ["read", "write", "deploy", "roles", "workflow"])
def test_refuse_tenant_bound_lets_any_untenanted_token_through(scope):
    """The property that makes it unusable as a route's only gate. If this ever
    starts refusing, the 22 routes that legitimately rely on it break — and if
    it is ever mistaken for authorization again, this test is the counterexample.
    """
    TokenPrincipal(scopes=(scope,), client_id="svc").refuse_tenant_bound()


def test_refuse_tenant_bound_refuses_exactly_one_thing():
    with pytest.raises(AuthorizationError, match="bound to a tenant"):
        TokenPrincipal(scopes=("write",), tenant_id="tenant-a").refuse_tenant_bound()


def test_its_docstring_still_disclaims_being_an_authorization_gate():
    """Renamed in PR #305 so the assumption cannot be made again; the
    disclaimer is part of the fix, not decoration."""
    doc = TokenPrincipal.refuse_tenant_bound.__doc__ or ""
    assert "NOT an authorization gate" in doc
    assert "_required_scope" in doc


# --- 2. _required_scope is the gate ---------------------------------------


@pytest.mark.parametrize("method,path", EXECUTION_GRANT_ROUTES)
def test_execution_grants_require_admin(method, path):
    """Each of these was reachable with an ordinary `write` token before
    PR #300 / #303. A regression here is a privilege escalation, not a style
    change."""
    assert _required_scope(method, path) == "admin", (method, path)


@pytest.mark.parametrize("method,path", IDENTITY_READS)
def test_identity_reads_stay_readable(method, path):
    assert _required_scope(method, path) == "read", (method, path)


@pytest.mark.parametrize("method,path", SELF_ONLY_WORKER_PATHS)
def test_worker_self_service_paths_keep_the_agent_scope(method, path):
    assert _required_scope(method, path) == "agent", (method, path)


#: The debug-terminal routes are the one execution grant enforced in the HANDLER
#: (`_require_terminal_principal`) rather than at the scope layer -- they must
#: stay reachable by a worker acting on itself, which a blanket admin scope would
#: forbid. Two mechanisms for one idea; asserted behaviourally so the guarantee
#: is pinned wherever it happens to live.
#:
#: THE HTTP ROUTES ARE GONE, THE GUARANTEE IS NOT. The `/dashboard` debug-shell
#: facade was retired with the legacy dashboard. `worker_debug_terminal.py` and
#: the DEBUG_TERMINAL_* AgentBus schemas survive, because a shell an operator
#: asks a NAMED agent for over the bus is coherent with the co-worker model --
#: what went is the HTTP path that bypassed the bus.
#:
#: Nothing on the hub currently opens a session, so there is no route to point
#: this at today. When the bus-native opener lands (task_a649529a), this test
#: must be re-pointed at it. A debug shell reachable by a general write token
#: is the thing PR #300 was written to prevent, and the bus is not exempt.
TERMINAL_ROUTES = []


#: A sentinel so the parametrize list is never EMPTY. An empty
#: `@pytest.mark.parametrize` generates ZERO test instances -- the function
#: still exists, so it looks fine to the impact-map staleness gate, which
#: checks for `def test_*` -- but every node id the map holds for it stops
#: resolving. Selection by node id then fails with a pytest USAGE error:
#:
#:     (no match in any of [<Module test_authority_boundary.py>])
#:     collected 398 items
#:     no tests ran           exit code 4
#:
#: That took out an unrelated PR's sanity run. A skipped case is visible in the
#: report and keeps exactly one node id alive; zero cases are invisible until
#: something tries to select them.
_NO_ROUTES = [(None, None, None)]


@pytest.mark.parametrize("method,path,body", TERMINAL_ROUTES or _NO_ROUTES)
def test_a_general_write_token_cannot_reach_a_debug_shell(method, path, body):
    """PR #300. Enforced in the handler, so scope alone does not prove it."""
    if method is None:
        pytest.skip("no debug-shell route exists yet; see task_a649529a")
    from fastapi.testclient import TestClient

    from mac.api import create_app
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    agent = cp.register_agent(machine.id, "worker-1")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "writer": {"scopes": ["write"], "client_id": "ci"},
                "reader": {"scopes": ["read"], "client_id": "dash"},
                "admin": {"scopes": ["admin"], "client_id": "operator"},
            },
        )
    )
    target = path % agent.id if "%s" in path else path
    for token in ("writer", "reader"):
        response = client.request(
            method, target, headers={"Authorization": "Bearer %s" % token}, json=body
        )
        assert response.status_code == 403, (token, target)
    ok = client.request(
        method, target, headers={"Authorization": "Bearer admin"}, json=body
    )
    assert ok.status_code == 200, target


def test_health_is_the_only_kind_of_route_that_is_public():
    assert _required_scope("GET", "/health") is None
    # Nothing that names an agent should ever be public.
    assert _required_scope("GET", "/agents/a1/openshell/policy") is not None


# --- 3. consent is not an execution grant ---------------------------------


def test_holding_a_lease_is_not_permission_to_escape_the_sandbox():
    """The ledger records that work was asked for and claimed. The grant to run
    it outside confinement is a separate, lease-bound, single-use record — and a
    task cannot manufacture one by putting it in its own metadata."""
    from mac.services import ControlPlane

    from mac.models import ValidationError

    cp = ControlPlane.in_memory()
    # Refused at CREATION, not merely stripped at projection -- a stronger
    # guarantee than the assignment-time strip, and the one that actually holds.
    with pytest.raises(ValidationError, match="control-plane-owned"):
        cp.create_task(
            "work",
            project="mac",
            required_capabilities=["python"],
            metadata={
                "runtime": {
                    "break_glass_authorization": {
                        "execution_boundary": "host",
                        "authorized_by": "self",
                    }
                }
            },
        )
