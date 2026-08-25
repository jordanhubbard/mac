"""A task's filer has to survive the HTTP boundary.

Everything else in the ownership chain was built and tested: the column, the
allocator gate, the CLI flag, the marking. The one link nobody exercised was
POST /tasks, and pydantic DROPS undeclared fields without complaint -- so the
CLI sent `created_by_human`, the hub ignored it, and every task was stored with
no filer.

That is not a cosmetic loss. A private agent runs only tasks filed by its
owner, so with no filer ever recorded, marking a worker private makes it refuse
every task in the ledger, permanently, with a rejection that reads like an
authorization decision rather than a dropped field.

These tests go over HTTP for exactly that reason: the service-layer tests
passed the whole time.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _client():
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    human = cp.register_human(username="jordanh", display_name="Jordan Hubbard")
    return cp, TestClient(create_app(control_plane=cp)), human


def test_the_filer_survives_task_creation():
    _cp, client, human = _client()

    response = client.post(
        "/tasks",
        json={"title": "owned work", "project": "mac", "created_by_human": human.id},
    )

    assert response.status_code in (200, 201), response.text
    assert response.json()["created_by_human"] == human.id


def test_a_task_filed_without_one_still_works():
    """Most callers do not name a filer, and refusing them would break every
    existing automation."""
    _cp, client, _human = _client()

    response = client.post("/tasks", json={"title": "unowned", "project": "mac"})

    assert response.status_code in (200, 201), response.text
    assert response.json()["created_by_human"] is None


def test_the_filer_is_readable_back_from_the_list():
    """The allocator reads it off the task record; if it only existed on the
    create response the gate would still compare against None."""
    _cp, client, human = _client()
    client.post(
        "/tasks",
        json={"title": "owned work", "project": "mac", "created_by_human": human.id},
    )

    rows = client.get("/tasks", params={"project": "mac"}).json()

    assert any(row.get("created_by_human") == human.id for row in rows)


def test_tasks_can_be_filtered_to_one_persons_work():
    _cp, client, human = _client()
    client.post(
        "/tasks",
        json={"title": "mine", "project": "mac", "created_by_human": human.id},
    )
    client.post("/tasks", json={"title": "not mine", "project": "mac"})

    rows = client.get("/tasks", params={"project": "mac", "created_by_human": human.id}).json()

    assert [row["title"] for row in rows] == ["mine"]


def test_an_unknown_filer_is_refused_rather_than_stored():
    """A free-text filer would never match a real owner, so the task would be
    invisible to exactly the private agent it was meant for."""
    _cp, client, _human = _client()

    response = client.post(
        "/tasks",
        json={"title": "typo", "project": "mac", "created_by_human": "nobody"},
    )

    assert response.status_code >= 400, response.text
