"""WHO is calling, and why the hub must decide it rather than the caller.

`created_by_human` gates dispatch: a private agent runs only tasks filed by
its owner. If the filer comes from the request body, that gate is decorative --
anyone can name anyone, so a stranger's task can be aimed at your private
worker just by asserting your id.

So the filer is taken from the authenticated principal, and the principal
carries a human because the credential was issued on the hub over SSH, where
the unix account that sshd authenticated is the evidence of who is enrolling.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import TokenPrincipal, create_app
from mac.services import ControlPlane


def _fixture(principal_human=None, admin=False):
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    alice = cp.register_human(username="alice")
    bob = cp.register_human(username="bob")
    scopes = frozenset({"admin"} if admin else {"write"})
    token = "t0ken"
    app = create_app(
        control_plane=cp,
        auth_tokens={
            token: TokenPrincipal(
                scopes=scopes,
                human_id=(alice.id if principal_human == "alice" else None),
            )
        },
    )
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer %s" % token})
    return cp, client, alice, bob


def test_the_filer_is_the_authenticated_caller():
    """Not supplied, yet recorded: the hub knows who called."""
    _cp, client, alice, _bob = _fixture(principal_human="alice")

    response = client.post("/tasks", json={"title": "mine", "project": "mac"})

    assert response.status_code in (200, 201), response.text
    assert response.json()["created_by_human"] == alice.id


def test_a_caller_cannot_file_a_task_as_someone_else():
    """The security property. Without this, marking an agent private protects
    nothing: aim a task at its owner by claiming to be them."""
    _cp, client, _alice, bob = _fixture(principal_human="alice")

    response = client.post(
        "/tasks",
        json={"title": "not mine to file", "project": "mac", "created_by_human": bob.id},
    )

    assert response.status_code == 403, response.text


def test_naming_yourself_explicitly_is_allowed():
    """It agrees with the principal, so it asserts nothing new."""
    _cp, client, alice, _bob = _fixture(principal_human="alice")

    response = client.post(
        "/tasks",
        json={"title": "mine", "project": "mac", "created_by_human": alice.id},
    )

    assert response.status_code in (200, 201), response.text
    assert response.json()["created_by_human"] == alice.id


def test_an_admin_may_file_on_behalf_of_someone_else():
    """Impersonation is an operator act -- needed to backfill the ledger and to
    repair a mis-filed task -- so it is admin-scoped rather than forbidden."""
    _cp, client, _alice, bob = _fixture(principal_human="alice", admin=True)

    response = client.post(
        "/tasks",
        json={"title": "on behalf", "project": "mac", "created_by_human": bob.id},
    )

    assert response.status_code in (200, 201), response.text
    assert response.json()["created_by_human"] == bob.id


def test_an_unbound_token_files_an_unowned_task():
    """Automation and pre-existing credentials have no human. They keep
    working; their tasks simply belong to nobody, which is what they were
    before this existed."""
    _cp, client, _alice, _bob = _fixture(principal_human=None)

    response = client.post("/tasks", json={"title": "automation", "project": "mac"})

    assert response.status_code in (200, 201), response.text
    assert response.json()["created_by_human"] is None


def test_an_admin_can_refile_an_existing_task():
    """The operator path the backfill needs: a ledger of 7,984 tasks predates
    recorded filers, and without this none of them can ever run on a private
    agent -- the gate would refuse work that is unambiguously its owner's."""
    _cp, client, _alice, bob = _fixture(principal_human="alice", admin=True)
    task_id = client.post(
        "/tasks", json={"title": "historic", "project": "mac"}
    ).json()["id"]

    response = client.put(
        "/tasks/%s" % task_id, json={"created_by_human": bob.id}
    )

    assert response.status_code in (200, 201), response.text
    assert response.json()["created_by_human"] == bob.id


def test_a_non_admin_cannot_refile_a_task():
    _cp, client, _alice, bob = _fixture(principal_human="alice")
    cp2, admin_client, _a, _b = _fixture(principal_human="alice", admin=True)
    task_id = admin_client.post(
        "/tasks", json={"title": "historic", "project": "mac"}
    ).json()["id"]

    response = client.put("/tasks/%s" % task_id, json={"created_by_human": bob.id})

    assert response.status_code == 403
