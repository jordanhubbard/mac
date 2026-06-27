"""Fleet IDE API contract tests -- source-linked to ide/src/api/mac.ts.

Routes tested here are derived directly from the ``api`` object exported by
``ide/src/api/mac.ts``.  Every entry in that object maps one-to-one to an
assertion below.  If the client adds, removes, or renames a method you MUST
update this file to match.

Client-to-route mapping (as of current ide/src/api/mac.ts):
  listTasks   -> GET  /tasks               (optional ?state= filter)
  getTask     -> GET  /tasks/{id}          (TaskDetail: task + evidence + history + reviews)
  listAgents  -> GET  /agents
  createTask  -> POST /tasks               (Fleet IDE payload shape)
  summary     -> GET  /tasks/{id}          (same route as getTask; alias in client)
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane

# ---------------------------------------------------------------------------
# Test token -- explicit, not ambient MAC_API_TOKEN.
# The Fleet IDE reads its token from localStorage or VITE_MAC_TOKEN; tests
# supply a fixed synthetic token so there is no dependency on the host
# environment.
# ---------------------------------------------------------------------------

# A fixed synthetic bearer token used only in this test module.
_TEST_TOKEN = 'fleet-ide-test-tok'
_AUTH_HEADERS: Dict[str, str] = {"Authorization": "Bearer " + _TEST_TOKEN}
# auth_tokens maps token -> scopes.  "admin" grants every scope (read + write).
_AUTH_TOKENS: Dict[str, Any] = {_TEST_TOKEN: ["admin"]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(cp: ControlPlane) -> TestClient:
    """Return a TestClient wired to the given in-memory control plane."""
    return TestClient(create_app(control_plane=cp, auth_tokens=_AUTH_TOKENS))


def _seed_task(cp: ControlPlane, title: str = "Seeded task") -> str:
    """Create a task directly via the control plane and return its id."""
    task = cp.create_task(title=title, description="seeded for contract test")
    return task.id


def _seed_agent(cp: ControlPlane, name: str = "worker-1") -> str:
    """Register a machine + agent directly via the control plane and return the agent id."""
    machine = cp.register_machine(name + "-host", resources={"cpu": 4, "memory_gb": 8})
    agent = cp.register_agent(machine.id, name, capabilities=["python"])
    return agent.id


# ---------------------------------------------------------------------------
# listTasks  ->  GET /tasks
# ---------------------------------------------------------------------------


def test_list_tasks_returns_200_and_list():
    """listTasks() calls GET /tasks and expects a JSON array."""
    cp = ControlPlane.in_memory()
    client = _make_client(cp)

    resp = client.get("/tasks", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)


def test_list_tasks_contains_seeded_task():
    """listTasks() result includes a previously created task."""
    cp = ControlPlane.in_memory()
    task_id = _seed_task(cp, title="contract-test-list-task")
    client = _make_client(cp)

    resp = client.get("/tasks", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert task_id in ids


def test_list_tasks_with_state_filter_returns_list():
    """listTasks(state) passes ?state= to GET /tasks and gets a list back."""
    cp = ControlPlane.in_memory()
    _seed_task(cp)
    client = _make_client(cp)

    resp = client.get("/tasks?state=open", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# getTask  ->  GET /tasks/{id}
# ---------------------------------------------------------------------------


def test_get_task_returns_200_with_task_detail_wrapper():
    """getTask(id) calls GET /tasks/{id} and expects a TaskDetail object."""
    cp = ControlPlane.in_memory()
    task_id = _seed_task(cp, title="contract-test-get-task")
    client = _make_client(cp)

    resp = client.get("/tasks/" + task_id, headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    # TaskDetail shape used by the Fleet IDE renderer
    assert "task" in body, "response must have a 'task' key"
    assert body["task"]["id"] == task_id


def test_get_task_returns_evidence_collection():
    """TaskDetail must carry an 'evidence' list (used by the Fleet IDE renderer)."""
    cp = ControlPlane.in_memory()
    task_id = _seed_task(cp)
    client = _make_client(cp)

    resp = client.get("/tasks/" + task_id, headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert "evidence" in body
    assert isinstance(body["evidence"], list)


def test_get_task_returns_history_collection():
    """TaskDetail must carry a 'history' list (used by the Fleet IDE renderer)."""
    cp = ControlPlane.in_memory()
    task_id = _seed_task(cp)
    client = _make_client(cp)

    resp = client.get("/tasks/" + task_id, headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert "history" in body
    assert isinstance(body["history"], list)


def test_get_task_returns_reviews_collection():
    """TaskDetail must carry a 'reviews' list (used by the Fleet IDE renderer)."""
    cp = ControlPlane.in_memory()
    task_id = _seed_task(cp)
    client = _make_client(cp)

    resp = client.get("/tasks/" + task_id, headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert "reviews" in body
    assert isinstance(body["reviews"], list)


def test_get_task_not_found_returns_404():
    """getTask() with an unknown id must get a 404 response."""
    cp = ControlPlane.in_memory()
    client = _make_client(cp)

    resp = client.get("/tasks/no-such-task-id", headers=_AUTH_HEADERS)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# listAgents  ->  GET /agents
# ---------------------------------------------------------------------------


def test_list_agents_returns_200_and_list():
    """listAgents() calls GET /agents and expects a JSON array."""
    cp = ControlPlane.in_memory()
    client = _make_client(cp)

    resp = client.get("/agents", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_agents_contains_seeded_agent():
    """listAgents() result includes a previously registered agent."""
    cp = ControlPlane.in_memory()
    agent_id = _seed_agent(cp, name="worker-2")
    client = _make_client(cp)

    resp = client.get("/agents", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()]
    assert agent_id in ids


def test_list_agents_response_has_expected_agent_fields():
    """Agent objects must carry id, name, and status -- fields used by the renderer."""
    cp = ControlPlane.in_memory()
    _seed_agent(cp, name="worker-1")
    client = _make_client(cp)

    resp = client.get("/agents", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    agents = resp.json()
    assert agents, "expected at least one agent"
    agent = agents[0]
    for field in ("id", "name", "status"):
        assert field in agent, "agent response missing field: " + field


# ---------------------------------------------------------------------------
# createTask  ->  POST /tasks
# ---------------------------------------------------------------------------


def test_create_task_returns_created_task():
    """createTask() POSTs to /tasks with a Fleet IDE payload and gets the task back."""
    cp = ControlPlane.in_memory()
    client = _make_client(cp)

    payload = {
        "title": "New task from Fleet IDE",
        "description": "Automated contract test",
        "project": "mac",
        "priority": 2,
    }
    resp = client.post("/tasks", json=payload, headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert "id" in body
    assert body["title"] == payload["title"]


def test_create_task_persists_fields():
    """Fields sent in the createTask payload must survive a round-trip via GET /tasks/{id}."""
    cp = ControlPlane.in_memory()
    client = _make_client(cp)

    payload = {
        "title": "Persisted task",
        "description": "check persistence",
        "project": "mac",
        "priority": 1,
    }
    create_resp = client.post("/tasks", json=payload, headers=_AUTH_HEADERS)
    assert create_resp.status_code == 200

    task_id = create_resp.json()["id"]
    get_resp = client.get("/tasks/" + task_id, headers=_AUTH_HEADERS)
    assert get_resp.status_code == 200

    task = get_resp.json()["task"]
    assert task["title"] == payload["title"]
    assert task["description"] == payload["description"]


def test_create_task_invalid_payload_returns_422():
    """createTask() with a missing required field (title) must return 422."""
    cp = ControlPlane.in_memory()
    client = _make_client(cp)

    # title is required by TaskCreate; omitting it must fail schema validation
    resp = client.post("/tasks", json={"description": "no title"}, headers=_AUTH_HEADERS)

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# UI shell smoke assertion (existence only -- not API-client parity)
# ---------------------------------------------------------------------------


def test_ui_shell_serves():
    """GET /ui returns 200 HTML -- existence check, not API parity."""
    cp = ControlPlane.in_memory()
    client = _make_client(cp)

    resp = client.get("/ui")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
