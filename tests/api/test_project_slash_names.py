from __future__ import annotations

from typing import Optional

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Match

from mac.api import create_app
from mac.services import ControlPlane


SLASH_PROJECT = "isaacsim7-poc@feat/ros-sim"


def _matching_route(app, method: str, path: str) -> Optional[APIRoute]:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        match, _child = route.matches(scope)
        if match == Match.FULL:
            return route
    return None


def test_slash_project_name_routes_resolve_and_static_paths_keep_winning():
    app = create_app(control_plane=ControlPlane.in_memory())
    client = TestClient(app)

    created = client.post(
        "/projects",
        json={"name": SLASH_PROJECT, "description": "slash-bearing branch name"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["name"] == SLASH_PROJECT

    list_route = _matching_route(app, "GET", "/projects")
    assert list_route is not None
    assert list_route.endpoint.__name__ == "list_projects"

    create_route = _matching_route(app, "POST", "/projects")
    assert create_route is not None
    assert create_route.endpoint.__name__ == "create_project"

    register_route = _matching_route(app, "POST", "/projects/register")
    assert register_route is not None
    assert register_route.endpoint.__name__ == "register_project"

    listed = client.get("/projects")
    assert listed.status_code == 200, listed.text
    assert isinstance(listed.json(), list)
    assert any(item.get("project") == SLASH_PROJECT for item in listed.json())

    registered = client.post(
        "/projects/register",
        json={"repository_url": "https://github.com/example/slash-register.git"},
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["project"]

    looked_up = client.get("/projects", params={"name": SLASH_PROJECT})
    assert looked_up.status_code == 200, looked_up.text
    assert looked_up.json()["project"] == SLASH_PROJECT

    shown = client.get("/projects/%s" % SLASH_PROJECT)
    assert shown.status_code == 200, shown.text
    assert shown.json()["project"] == SLASH_PROJECT
    get_route = _matching_route(app, "GET", "/projects/%s" % SLASH_PROJECT)
    assert get_route is not None
    assert get_route.path == "/projects/{project:path}"

    updated = client.put(
        "/projects/%s" % SLASH_PROJECT,
        json={"description": "updated slash project"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == "updated slash project"

    dispatched = client.post(
        "/projects/%s/dispatch" % SLASH_PROJECT,
        json={"paused": True, "actor": "operator"},
    )
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["name"] == SLASH_PROJECT
    assert dispatched.json()["metadata"]["dispatch_paused"] is True

    policy = client.post(
        "/optimizer/policies",
        json={
            "name": "slash-policy",
            "project": SLASH_PROJECT,
            "parameters": {"plan_first": True},
            "created_by": "operator",
        },
    )
    assert policy.status_code == 200, policy.text
    policy_id = policy.json()["id"]
    rolled_back = client.post(
        "/optimizer/projects/%s/rollback/%s" % (SLASH_PROJECT, policy_id),
        json={"actor": "operator", "reason": "slash-name routing"},
    )
    assert rolled_back.status_code == 200, rolled_back.text

    deleted = client.delete("/projects/%s" % SLASH_PROJECT, params={"force": True})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == SLASH_PROJECT
    missing = client.get("/projects/%s" % SLASH_PROJECT)
    assert missing.status_code == 404
