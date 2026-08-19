"""Slash-containing project names must resolve on every project-name route.

Project names are generated as ``repo@branch`` (services.register_project) and
Git branches legitimately contain ``/`` (e.g. ``isaacsim7-poc@feat/ros-sim``).
Clients percent-encode the name with ``quote(safe="")``, but the ASGI server
decodes ``%2F`` back to a real ``/`` before Starlette routes the request, so a
single-segment ``{project}`` converter can never match such a name and the hub
answers 404. ``mac project list`` shows the project while ``mac project show``
404s -- the client is correct; the server routing was not.

These tests pin the fix: a ``{project:path}`` twin on every project-name route
so the whole decoded tail is captured as the name, without letting the catch-all
shadow ``/projects`` (list/create) or ``/projects/register``.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


SLASH_PROJECT = "isaacsim7-poc@feat/ros-sim"


def _client() -> TestClient:
    return TestClient(create_app(control_plane=ControlPlane.in_memory()))


def _create_project(client: TestClient, name: str) -> str:
    response = client.post("/projects", json={"name": name})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == name
    return name


def _encoded(name: str) -> str:
    # Exactly how the client quotes the name on the wire (dispatch.py uses
    # quote(..., safe="")): every slash becomes %2F.
    return quote(name, safe="")


def test_get_project_resolves_slash_name():
    client = _client()
    _create_project(client, SLASH_PROJECT)

    response = client.get("/projects/%s" % _encoded(SLASH_PROJECT))

    assert response.status_code == 200, response.text
    assert response.json()["project"] == SLASH_PROJECT


def test_put_project_resolves_slash_name():
    client = _client()
    _create_project(client, SLASH_PROJECT)

    response = client.put(
        "/projects/%s" % _encoded(SLASH_PROJECT),
        json={"description": "updated via slash route", "actor": "operator"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == SLASH_PROJECT
    assert response.json()["description"] == "updated via slash route"


def test_dispatch_post_resolves_slash_name():
    client = _client()
    _create_project(client, SLASH_PROJECT)

    response = client.post(
        "/projects/%s/dispatch" % _encoded(SLASH_PROJECT),
        json={"paused": True, "actor": "operator"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == SLASH_PROJECT


def test_delete_project_resolves_slash_name():
    client = _client()
    _create_project(client, SLASH_PROJECT)

    response = client.delete("/projects/%s" % _encoded(SLASH_PROJECT))

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": SLASH_PROJECT}

    # The row is really gone: a follow-up read 404s.
    follow_up = client.get("/projects/%s" % _encoded(SLASH_PROJECT))
    assert follow_up.status_code == 404, follow_up.text


def test_optimizer_rollback_resolves_slash_name():
    client = _client()
    _create_project(client, SLASH_PROJECT)
    policy = client.post(
        "/optimizer/policies",
        json={
            "name": "slash-rollback-policy",
            "project": SLASH_PROJECT,
            "parameters": {"plan_first": True},
            "created_by": "operator",
        },
    )
    assert policy.status_code == 200, policy.text
    policy_id = policy.json()["id"]

    response = client.post(
        "/optimizer/projects/%s/rollback/%s" % (_encoded(SLASH_PROJECT), policy_id),
        json={"actor": "operator", "reason": "slash rollback route coverage"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["project"] == SLASH_PROJECT


def test_slash_route_does_not_shadow_projects_collection():
    client = _client()
    _create_project(client, SLASH_PROJECT)

    # GET /projects still lists (not captured by GET /projects/{project:path}).
    listing = client.get("/projects")
    assert listing.status_code == 200, listing.text
    assert any(item.get("project") == SLASH_PROJECT for item in listing.json())

    # POST /projects still creates (not captured by any dispatch/path route).
    created = client.post("/projects", json={"name": "plain-collection-project"})
    assert created.status_code == 200, created.text
    assert created.json()["name"] == "plain-collection-project"


def test_slash_route_does_not_shadow_projects_register():
    client = _client()

    # POST /projects/register keeps its own handler: an empty body is rejected
    # by that handler's schema (422), proving the request was NOT swallowed by
    # POST /projects/{project:path}/dispatch or any catch-all.
    response = client.post("/projects/register", json={})

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert any(err.get("loc", [])[:2] == ["body", "repository_url"] for err in detail)


def test_single_segment_project_names_still_resolve():
    client = _client()
    _create_project(client, "plain-project")

    response = client.get("/projects/plain-project")

    assert response.status_code == 200, response.text
    assert response.json()["project"] == "plain-project"
