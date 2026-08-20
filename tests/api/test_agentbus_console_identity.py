"""How a front end joins the bus, and what it is still not allowed to do.

The bus read endpoints are self-only:

    principal.assert_actor(agent_id)   # an agent connects to the bus as itself

That is the right rule and it is not being relaxed. But it left a front end
with no way to address them correctly: a token does not carry its agent id in
any form a browser can read, so a console had to either guess (a 403 that looks
like an outage) or the rule had to be widened so a read token could name any
agent it liked. The second option is the one that must never be taken --
`agent_id` in a path would stop meaning anything the moment any token could put
any value there.

`GET /agentbus/identity` is the narrow third answer: it reports the binding the
hub has ALREADY made for the presented credential. These tests pin both halves
of that -- that it reports the binding, and that reporting it grants nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mac.api import AGENTBUS_IDENTITY_SCHEMA, _required_scope, create_app
from mac.services import ControlPlane


@pytest.fixture
def cp() -> ControlPlane:
    return ControlPlane.in_memory()


@pytest.fixture
def fleet(cp: ControlPlane):
    machine = cp.register_machine("host")
    worker = cp.register_agent(machine.id, "rocky", capabilities=["python"])
    peer = cp.register_agent(machine.id, "natasha", capabilities=["review"])
    return worker, peer


def _client(cp: ControlPlane, tokens) -> TestClient:
    return TestClient(create_app(control_plane=cp, auth_tokens=tokens))


def _agent_token(agent_id: str):
    return {
        "scopes": ["agent", "read"],
        "agent_id": agent_id,
        "principal_kind": "agent",
    }


def _read_token():
    return {"scopes": ["read"]}


def test_an_agent_token_learns_which_agent_it_is(cp, fleet):
    worker, _peer = fleet
    client = _client(cp, {"tok-agent": _agent_token(worker.id)})

    body = client.get(
        "/agentbus/identity", headers={"Authorization": "Bearer tok-agent"}
    ).json()

    assert body["schema"] == AGENTBUS_IDENTITY_SCHEMA
    assert body["agent_id"] == worker.id
    assert body["joined"] is True
    assert body["reason"] == ""


def test_a_read_token_is_told_it_is_not_an_agent_rather_than_refused(cp, fleet):
    """The distinction the console renders.

    "You are not on the bus" is a fact about the credential and can be shown as
    one. A 403 from the traffic route is indistinguishable, in a browser, from
    the hub being broken -- and a view that cannot tell those apart will render
    an empty conversation, which reads as a quiet fleet.
    """
    client = _client(cp, {"tok-read": _read_token()})

    response = client.get(
        "/agentbus/identity", headers={"Authorization": "Bearer tok-read"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] is None
    assert body["joined"] is False
    assert body["reason"], "a refusal with no reason is not an answer"


def test_knowing_the_answer_grants_nothing(cp, fleet):
    """The whole point. `identity` is a mirror, not a key.

    A read token that has just been told "you are not an agent" must still be
    refused by the self-only reads. If this ever passes, `assert_actor` has been
    widened and every `agent_id` in a URL has stopped being an identity claim.
    """
    worker, _peer = fleet
    client = _client(cp, {"tok-read": _read_token()})
    headers = {"Authorization": "Bearer tok-read"}

    for path in (
        "/agents/%s/agentbus/traffic" % worker.id,
        "/agents/%s/agentbus/roll-call" % worker.id,
    ):
        assert client.get(path, headers=headers).status_code in (401, 403), path


def test_an_agent_cannot_read_the_bus_as_a_DIFFERENT_agent(cp, fleet):
    """Self-only means self, not "any agent you can name"."""
    worker, peer = fleet
    client = _client(cp, {"tok-agent": _agent_token(worker.id)})
    headers = {"Authorization": "Bearer tok-agent"}

    assert (
        client.get(
            "/agents/%s/agentbus/traffic" % peer.id, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.get("/agents/%s/agentbus/traffic" % worker.id, headers=headers).status_code
        == 200
    )


def test_identity_is_a_read_and_the_bus_reads_are_not(cp):
    """Two scopes, deliberately different, asserted together so the difference
    cannot be flattened by someone tidying the scope table."""
    assert _required_scope("GET", "/agentbus/identity") == "read"
    assert _required_scope("GET", "/agents/a1/agentbus/traffic") == "agent"
    assert _required_scope("GET", "/agents/a1/agentbus/roll-call") == "agent"


def test_identity_names_nobody_else(cp, fleet):
    """It discloses the caller's own binding and no part of the roster.

    A read token learning who else is registered would be a disclosure the
    caller did not already have; the roll call is behind the agent scope for
    exactly that reason.
    """
    worker, peer = fleet
    client = _client(cp, {"tok-read": _read_token()})

    text = client.get(
        "/agentbus/identity", headers={"Authorization": "Bearer tok-read"}
    ).text

    assert worker.id not in text
    assert peer.id not in text


def test_the_cli_reaches_the_same_reads_over_the_hub(cp, fleet):
    """One backend, two front ends -- and the CLI half must work in HUB mode.

    `mac admin agentbus follow` / `roll-call` call the ControlPlane methods,
    which resolve to `RemoteDispatch` when the CLI is pointed at a hub URL
    rather than `--db`. If those wrappers are missing the verbs exist, pass
    their `--db` tests, and fail for every operator who uses the fleet the
    normal way, which is how `agentbus wait` came to be unusable in hub mode.

    Two shapes are pinned because both have bitten:

    * traffic entries come back as PLAIN dicts, since the follow handler
      splats each one into its NDJSON line; and
    * the roll call is ADDRESSED, because it is self-only on the wire even
      though its answer is fleet-wide -- and the actor it is addressed as comes
      from `/agentbus/identity`, since that is the only agent the token may
      use and the local method takes no actor at all.
    """
    from mac.dispatch import RemoteDispatch

    worker, peer = fleet
    stream = cp.agentbus.open_stream(peer.id, worker.id, stream_id="s-hub")
    cp.agentbus.append_chunk(stream.id, peer.id, {"text": "said over the bus"})
    client = _client(cp, {"tok-agent": _agent_token(worker.id)})

    class _HubClient:
        def request(self, method, path, body=None):
            response = client.request(
                method, path, json=body, headers={"Authorization": "Bearer tok-agent"}
            )
            response.raise_for_status()
            return response.json()

    remote = RemoteDispatch(_HubClient())

    assert remote.agentbus_identity()["agent_id"] == worker.id

    traffic = remote.read_agentbus_traffic(worker.id, "", 10)
    assert len(traffic) == 1
    assert isinstance(traffic[0], dict), "the follow handler splats these"
    assert {"event": "traffic", **traffic[0]}["from_agent_id"] == peer.id
    assert traffic[0]["cursor"]

    roster = remote.agentbus_roll_call(agent_id=remote.agentbus_identity()["agent_id"])
    assert {entry["id"] for entry in roster["agents"]} >= {worker.id, peer.id}


def test_the_roll_call_answers_who_is_present_and_what_they_can_do(cp, fleet):
    """The read the console's roster panel is built on."""
    worker, peer = fleet
    client = _client(cp, {"tok-agent": _agent_token(worker.id)})

    body = client.get(
        "/agents/%s/agentbus/roll-call" % worker.id,
        headers={"Authorization": "Bearer tok-agent"},
    ).json()

    by_id = {entry["id"]: entry for entry in body["agents"]}
    assert set(by_id) >= {worker.id, peer.id}
    assert by_id[peer.id]["capabilities"] == ["review"]
    assert body["agent_count"] == len(body["agents"])
