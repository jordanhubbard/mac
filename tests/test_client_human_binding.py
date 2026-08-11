"""A credential remembers which person it speaks for.

`mac client enroll` runs on the hub, reached over SSH. Whoever sshd
authenticated is the account the process runs as, so the local unix account is
the evidence of who is enrolling -- nothing the remote caller sends is trusted
for it. That evidence is resolved ONCE, here, to a durable id.

Resolving it per-request instead would be a trap: unix accounts get renamed and
recycled, so a recreated account would silently inherit the previous holder's
agents and work.
"""

from __future__ import annotations

import pytest

from mac.client_principals import ClientPrincipalStore


@pytest.fixture()
def store(tmp_path):
    return ClientPrincipalStore(tmp_path / "clients.json")


def test_an_enrolled_credential_records_its_human(store):
    issued = store.enroll("laptop", human_id="human_abc", actor="test")

    assert issued.record["human_id"] == "human_abc"


def test_a_credential_without_a_human_is_still_valid(store):
    """Worker and automation credentials speak for no person. They must keep
    working -- their tasks simply belong to nobody."""
    issued = store.enroll("ci-runner", actor="test")

    assert "human_id" not in issued.record


def test_the_binding_survives_a_rotation(store):
    """Rotating a credential replaces the secret, not the person holding it."""
    store.enroll("laptop", human_id="human_abc", actor="test")

    rotated = store.enroll("laptop", human_id="human_abc", rotate=True, actor="test")

    assert rotated.record["human_id"] == "human_abc"
    assert rotated.record["credential_version"] == 2


def test_the_token_itself_is_never_the_identity(store):
    """The record binds a person to a credential; the credential is a secret
    that can be rotated without the person changing."""
    first = store.enroll("laptop", human_id="human_abc", actor="test")
    second = store.enroll("laptop", human_id="human_abc", rotate=True, actor="test")

    assert first.token != second.token
    assert first.record["human_id"] == second.record["human_id"]


def test_the_hub_sees_the_human_on_the_authenticated_principal(tmp_path):
    """The link that makes the rest of it real.

    Enrolment can record a human perfectly and it changes nothing unless the
    mapping the hub AUTHENTICATES AGAINST carries it -- that mapping is built
    separately, and it previously kept only scopes and the client id.
    """
    from mac.client_principals import ClientPrincipalProvider

    store = ClientPrincipalStore(tmp_path / "clients.json")
    issued = store.enroll("laptop", human_id="human_abc", actor="test")

    mapping = ClientPrincipalProvider(tmp_path / "clients.json").tokens()

    principal = mapping[issued.record["token_hash"]]
    assert principal["human_id"] == "human_abc"


def test_the_principal_the_api_builds_carries_the_human(tmp_path):
    """One step further: through the API's own coercion, because that is what
    the request handler receives."""
    from mac.api import _coerce_principal

    principal = _coerce_principal(
        {"scopes": ["write"], "client_id": "laptop", "human_id": "human_abc"}
    )

    assert principal.human_id == "human_abc"
