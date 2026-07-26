"""HTTP endpoints for task ready/search/stats (parity-ready-http-01).

These let `mac task ready/search/stats` work against the hub instead of
requiring --db (direct SQLite).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _client() -> TestClient:
    return TestClient(create_app(control_plane=ControlPlane.in_memory()))


def _make(client, title, project=None, **extra):
    body = {"title": title}
    if project is not None:
        body["project"] = project
    body.update(extra)
    resp = client.post("/tasks", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_ready_endpoint_lists_open_unclaimed_and_is_not_shadowed_by_task_id():
    client = _client()
    a = _make(client, "alpha task", project="alpha")
    _make(client, "beta task", project="beta")

    ready = client.get("/tasks/ready")
    assert ready.status_code == 200
    titles = {t["title"] for t in ready.json()}
    assert {"alpha task", "beta task"} <= titles

    # /tasks/ready must not be captured by /tasks/{task_id}; the real id still works.
    assert client.get("/tasks/ready").json().__class__ is list
    assert client.get(f"/tasks/{a['id']}").json()["task"]["id"] == a["id"]


def test_ready_endpoint_filters_by_project_and_limit():
    client = _client()
    _make(client, "a1", project="alpha")
    _make(client, "b1", project="beta")
    scoped = client.get("/tasks/ready", params={"project": "alpha"}).json()
    assert {t["project"] for t in scoped} == {"alpha"}
    assert len(client.get("/tasks/ready", params={"limit": 1}).json()) == 1


def test_ready_endpoint_excludes_tasks_with_unmet_dependencies():
    client = _client()
    parent = _make(client, "parent", project="p")
    _make(client, "child", project="p", dependencies=[parent["id"]])
    ready_titles = {t["title"] for t in client.get("/tasks/ready").json()}
    assert "parent" in ready_titles
    assert "child" not in ready_titles  # waiting until parent completes


def test_ready_and_task_dispatch_explain_routes_share_authoritative_reasons():
    client = _client()
    task = _make(client, "explain me", project="p")

    ready = client.get("/tasks/ready/explain")
    assert ready.status_code == 200
    item = next(entry for entry in ready.json() if entry["task"]["id"] == task["id"])
    assert item["task_ready"] is True
    assert item["dispatchable"] is False
    assert item["unclaimed_reasons"][0]["code"] == "no_agents_registered"

    direct = client.get(f"/tasks/{task['id']}/dispatch-explain")
    assert direct.status_code == 200
    assert direct.json()["unclaimed_reasons"] == item["unclaimed_reasons"]


def test_ready_explain_reuses_one_bounded_agent_snapshot():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))
    for index in range(3):
        _make(client, "explain-%d" % index, project="p")
    calls = 0
    original = cp.list_agents

    def counted_list_agents(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    cp.list_agents = counted_list_agents
    response = client.get("/tasks/ready/explain", params={"limit": 3})

    assert response.status_code == 200
    assert len(response.json()) == 3
    assert calls == 1


def test_search_endpoint_matches_title_and_filters_project():
    client = _client()
    _make(client, "searchable alpha thing", project="alpha")
    _make(client, "searchable beta thing", project="beta")
    _make(client, "unrelated", project="alpha")
    hits = client.get("/tasks/search", params={"q": "searchable"}).json()
    assert {t["title"] for t in hits} == {"searchable alpha thing", "searchable beta thing"}
    scoped = client.get("/tasks/search", params={"q": "searchable", "project": "alpha"}).json()
    assert {t["title"] for t in scoped} == {"searchable alpha thing"}


def test_stats_endpoint_counts_by_state_and_filters_project():
    client = _client()
    _make(client, "o1", project="alpha")
    _make(client, "o2", project="alpha")
    _make(client, "o3", project="beta")
    allstats = client.get("/tasks/stats").json()
    assert allstats.get("open") == 3
    scoped = client.get("/tasks/stats", params={"project": "alpha"}).json()
    assert scoped == {"open": 2}


def test_list_tasks_filter_by_project():
    """GET /tasks?project=... returns only tasks from that project."""
    client = _client()
    _make(client, "alpha-1", project="alpha")
    _make(client, "alpha-2", project="alpha")
    _make(client, "beta-1", project="beta")
    scoped = client.get("/tasks", params={"project": "alpha"}).json()
    assert {t["project"] for t in scoped} == {"alpha"}
    assert len(scoped) == 2
    titles = {t["title"] for t in scoped}
    assert titles == {"alpha-1", "alpha-2"}


def test_list_tasks_limit():
    """GET /tasks?limit=N returns at most N tasks."""
    client = _client()
    for i in range(5):
        _make(client, f"task-{i}", project="mac")
    limited = client.get("/tasks", params={"limit": 3}).json()
    assert len(limited) <= 3


def test_list_tasks_project_and_limit_combined():
    """GET /tasks?project=mac&state=open&limit=20 honours both filters server-side."""
    client = _client()
    for i in range(5):
        _make(client, f"mac-{i}", project="mac")
    _make(client, "other-1", project="other")
    result = client.get("/tasks", params={"project": "mac", "state": "open", "limit": 20}).json()
    assert len(result) <= 20
    assert all(t["project"] == "mac" for t in result)
    assert len(result) == 5
