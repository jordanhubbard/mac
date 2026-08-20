"""How a front end joins the bus, and the widening it must not cause.

Every bus read is self-only: ``/agents/{id}/agentbus/traffic`` and
``/roll-call`` both call ``assert_actor``, because an agent connects to the bus
AS ITSELF. That is why both endpoints shipped with no caller — a browser has to
know its own agent name before it can even form the URL, and nothing told it.

There were two ways to close that gap.

The first was to widen ``assert_actor`` so a read token could name any agent in
the path. That is the one this design refuses, and these tests pin the refusal:
it would hand every read token the fleet's whole conversation under a borrowed
name, and it would make ``who said that`` unanswerable the moment phase 2 lets
the console speak.

The second is ``GET /agentbus/identity``: the hub reports the binding on the
credential the caller already presented, and nothing else. It grants no
authority — an unbound token is told it is not a participant, not handed a
persona — so the console can ask who it is without anyone being able to ask who
someone else is. See ADR 0025.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mac.api import _required_scope, create_app
from mac.services import ControlPlane


@pytest.fixture
def fleet():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    worker = cp.register_agent(machine.id, "worker-1")
    console = cp.register_agent(machine.id, "console-persona")
    return cp, worker, console


def _client(cp: ControlPlane, console_id: str, worker_id: str) -> TestClient:
    return TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "console": {"scopes": ["agent"], "agent_id": console_id},
                "worker": {"scopes": ["agent"], "agent_id": worker_id},
                "reader": {"scopes": ["read"], "client_id": "observability"},
                "root": {"scopes": ["admin"], "client_id": "operator"},
            },
        )
    )


def _bearer(token: str) -> dict:
    return {"Authorization": "Bearer %s" % token}


# ---------------------------------------------------------------------------
# The identity endpoint reports a binding. It does not confer one.
# ---------------------------------------------------------------------------


def test_a_bound_credential_learns_the_name_it_speaks_under(fleet):
    cp, worker, console = fleet
    client = _client(cp, console.id, worker.id)

    body = client.get("/agentbus/identity", headers=_bearer("console")).json()

    assert body["schema"] == "mac.agentbus.identity.v1"
    assert body["agent_id"] == console.id
    assert body["bus_participant"] is True
    assert body["reason"] == ""


def test_a_read_token_is_refused_the_question_entirely(fleet):
    """A credential that cannot join the bus is not told, by name, who is on it.

    The console renders this refusal as "this session is not on the bus" —
    which is a different fact from "the bus is quiet" and must stay
    distinguishable — using the hub's own detail as the reason.
    """
    cp, worker, console = fleet
    client = _client(cp, console.id, worker.id)

    refused = client.get("/agentbus/identity", headers=_bearer("reader"))

    assert refused.status_code == 403
    assert refused.json()["detail"], (
        "a refusal with no detail renders as a broken console rather than an "
        "explained one"
    )


def test_admin_authority_is_not_a_seat_on_the_bus(fleet):
    """Deliberate: authority OVER the fleet is not membership OF it.

    Handing an admin token a bus identity would be the quickest way to make
    "who said that" unanswerable, because every operator would speak as the
    same unattributable principal.
    """
    cp, worker, console = fleet
    client = _client(cp, console.id, worker.id)

    body = client.get("/agentbus/identity", headers=_bearer("root")).json()

    assert body["bus_participant"] is False
    assert "not a seat" in body["reason"]


def test_identity_never_reports_another_principal(fleet):
    """It answers "who am I", and there is no way to ask it "who is X"."""
    cp, worker, console = fleet
    client = _client(cp, console.id, worker.id)

    as_console = client.get("/agentbus/identity", headers=_bearer("console")).json()
    as_worker = client.get("/agentbus/identity", headers=_bearer("worker")).json()

    assert as_console["agent_id"] == console.id
    assert as_worker["agent_id"] == worker.id


# ---------------------------------------------------------------------------
# assert_actor stays un-widened. This is the invariant the view is built on.
# ---------------------------------------------------------------------------


def test_a_read_token_still_cannot_read_the_bus_as_an_agent(fleet):
    """The widening that would have made a browser work without a persona.

    If this ever returns 200, the console's identity design has been bypassed
    and every holder of a read token can listen to the fleet under any name.
    """
    cp, worker, console = fleet
    client = _client(cp, console.id, worker.id)

    for path in (
        "/agents/%s/agentbus/traffic" % worker.id,
        "/agents/%s/agentbus/roll-call" % worker.id,
    ):
        assert client.get(path, headers=_bearer("reader")).status_code == 403, path


def test_one_agent_cannot_read_the_bus_as_another(fleet):
    cp, worker, console = fleet
    client = _client(cp, console.id, worker.id)

    denied = client.get(
        "/agents/%s/agentbus/traffic" % worker.id, headers=_bearer("console")
    )
    assert denied.status_code == 403

    allowed = client.get(
        "/agents/%s/agentbus/traffic" % console.id, headers=_bearer("console")
    )
    assert allowed.status_code == 200


def test_the_identity_read_sits_at_the_scope_of_the_reads_it_enables(fleet):
    """Scope check, kept beside the behavioural ones.

    The ``agent`` scope is right because the answer is only useful to a
    participant. The GET default would otherwise have handed this ``read``,
    since the path does not match the ``/agents/{id}/...`` shape the rest of
    the self-only bus reads use — an accident that would have let any read
    token be told, by name, who is on the bus.
    """
    assert _required_scope("GET", "/agentbus/identity") == "agent"
    # And the reads it unlocks stay self-only.
    assert _required_scope("GET", "/agents/a1/agentbus/traffic") == "agent"
    assert _required_scope("GET", "/agents/a1/agentbus/roll-call") == "agent"


# ---------------------------------------------------------------------------
# What the view then reads.
# ---------------------------------------------------------------------------


def test_traffic_is_overhearable_and_says_who_must_answer(fleet):
    """Addressing, not access — the convention the whole view renders.

    A point-to-point message is visible to the console. What makes that safe is
    that ``addressed_to`` names who is expected to answer, and by convention
    nobody else does. The view shows the addressing for exactly that reason.
    """
    cp, worker, console = fleet
    other = cp.register_agent(cp.list_machines()[0].id, "worker-2")
    stream = cp.agentbus.open_stream(worker.id, other.id, stream_id="s1")
    cp.agentbus.append_chunk(stream.id, worker.id, {"text": "taking src/mac/api.py"})

    client = _client(cp, console.id, worker.id)
    traffic = client.get(
        "/agents/%s/agentbus/traffic" % console.id, headers=_bearer("console")
    ).json()

    assert [entry["chunk"]["payload"]["text"] for entry in traffic] == [
        "taking src/mac/api.py"
    ]
    assert traffic[0]["from_agent_id"] == worker.id
    assert traffic[0]["addressed_to"] == [other.id]
    # The console overheard it; it is not the one expected to reply.
    assert traffic[0]["addressed_to_me"] is False
    assert traffic[0]["reply_expected"] is False
    assert traffic[0]["cursor"], "a row with no cursor cannot be resumed from"


def test_roll_call_answers_who_is_present_and_what_they_can_do(fleet):
    cp, worker, console = fleet
    client = _client(cp, console.id, worker.id)

    body = client.get(
        "/agents/%s/agentbus/roll-call" % console.id, headers=_bearer("console")
    ).json()

    assert body["agent_count"] == len(body["agents"])
    assert {agent["id"] for agent in body["agents"]} >= {worker.id, console.id}
    assert all("capabilities" in agent for agent in body["agents"])
