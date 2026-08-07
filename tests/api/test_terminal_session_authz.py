"""A debug terminal is an interactive shell on a fleet host.

`require_global_fleet()` was guarding these routes and cannot do this job: it
refuses only TENANT-BOUND non-admin tokens
(``if self.is_admin or self.tenant_id is None: return``). An untenanted client
token — the ordinary ``write`` scope, the same one that creates a task — has no
tenant, is not admin, and carries no ``agent_id``, so it fell through every
check and could open a session on any agent and type into its shell.

These tests pin the boundary in both directions: the general client token is
out, and the two legitimate principals (an operator, and a worker acting on
itself) are still in.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _headers(token: str) -> dict:
    return {"Authorization": "Bearer %s" % token}


@pytest.fixture
def fleet():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    mine = cp.register_agent(machine.id, "worker-1")
    other = cp.register_agent(machine.id, "worker-2")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "reader": {"scopes": ["read"], "client_id": "dashboard"},
                # Untenanted general client token: the principal class that
                # should never have reached a shell.
                "writer": {"scopes": ["write"], "client_id": "ci"},
                "tenant_w": {"scopes": ["write"], "tenant_id": "tenant-a"},
                "admin": {"scopes": ["admin"], "client_id": "operator"},
                # A worker: `agent` alone cannot satisfy the route's `write`
                # scope, so the real-world worker token carries both.
                "mine": {"scopes": ["agent", "write"], "agent_id": mine.id},
                "other": {"scopes": ["agent", "write"], "agent_id": other.id},
            },
        )
    )
    return client, cp, mine, other


def _open(client, agent_id, token):
    return client.post(
        "/dashboard/agents/%s/terminal-sessions" % agent_id,
        headers=_headers(token),
        json={"shell": "/bin/sh"},
    )


# --- the hole -------------------------------------------------------------


def test_general_write_token_cannot_open_a_shell(fleet):
    """The regression that motivated this: `write` opened a session and could
    then type into it — arbitrary command execution on a fleet host with an
    ordinary task-writing credential."""
    client, _cp, mine, _other = fleet
    response = _open(client, mine.id, "writer")
    assert response.status_code == 403
    assert "admin token" in response.json()["detail"]


def test_general_write_token_cannot_drive_an_existing_session(fleet):
    """Opening is not the only entry point: input/resize/close each took the
    same insufficient guard, so a session opened by anyone was drivable."""
    client, _cp, mine, _other = fleet
    opened = _open(client, mine.id, "admin").json()
    session, stream = opened["session_id"], opened["input_stream_id"]

    for path, body in [
        ("input", {"input_stream_id": stream, "data": "whoami\n"}),
        ("resize", {"input_stream_id": stream, "rows": 40, "cols": 100}),
        ("close", {"input_stream_id": stream}),
    ]:
        response = client.post(
            "/dashboard/terminal-sessions/%s/%s" % (session, path),
            headers=_headers("writer"),
            json=body,
        )
        assert response.status_code == 403, path


def test_read_token_cannot_enumerate_sessions(fleet):
    """Session listing names agents with live shells — reconnaissance, and the
    session id is the handle the other routes take."""
    client, _cp, _mine, _other = fleet
    assert client.get(
        "/dashboard/terminal-sessions", headers=_headers("reader")
    ).status_code == 403


def test_tenant_bound_token_is_still_refused(fleet):
    """The one case require_global_fleet() did cover must keep working."""
    client, _cp, mine, _other = fleet
    response = _open(client, mine.id, "tenant_w")
    assert response.status_code == 403
    assert "bound to a tenant" in response.json()["detail"]


# --- the principals that must keep working --------------------------------


def test_admin_can_open_and_drive_a_session(fleet):
    client, _cp, mine, _other = fleet
    opened = _open(client, mine.id, "admin")
    assert opened.status_code == 200
    body = opened.json()
    assert client.post(
        "/dashboard/terminal-sessions/%s/input" % body["session_id"],
        headers=_headers("admin"),
        json={"input_stream_id": body["input_stream_id"], "data": "whoami\n"},
    ).status_code == 200
    assert client.get(
        "/dashboard/terminal-sessions", headers=_headers("admin")
    ).status_code == 200


def test_a_worker_can_still_open_its_own_terminal(fleet):
    """Deliberately not collapsed to admin-only: a worker legitimately opens its
    own debug terminal, and that path predates this change."""
    client, _cp, mine, _other = fleet
    assert _open(client, mine.id, "mine").status_code == 200


def test_a_worker_cannot_open_another_agents_terminal(fleet):
    """The handler's own assert_actor narrowing still applies on top."""
    client, _cp, mine, _other = fleet
    assert _open(client, mine.id, "other").status_code == 403
