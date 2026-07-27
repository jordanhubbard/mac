"""Fleet IDE API contract tests -- source-linked to ide/src/api/mac.ts.

Routes tested here are derived directly from the ``api`` object exported by
``ide/src/api/mac.ts``.  Every entry in that object maps one-to-one to an
assertion below.  If the client adds, removes, or renames a method you MUST
update this file to match.

Client-to-route mapping (as of current ide/src/api/mac.ts):
  dashboardState -> GET  /dashboard/state?view=ide
  listTasks   -> GET  /tasks               (optional ?state= filter)
  getTask     -> GET  /tasks/{id}?view=compact (bounded TaskDetail)
  listAgents  -> GET  /agents
  createTask  -> POST /tasks               (Fleet IDE payload shape)
  updateTask  -> PUT  /tasks/{id}          (operator guidance persisted on task)
  reopenTask  -> POST /tasks/{id}/reopen   (audited blocked-task recovery)
  claimTask   -> POST /tasks/{id}/claim    (specific agent assignment)
  summary     -> GET  /tasks/{id}          (same route as getTask; alias in client)
  requestReview -> POST /tasks/{id}/reviews
  workflowPlanPreview -> POST /dashboard/workflow-plan/preview
  workflowPlanAccept  -> POST /dashboard/workflow-plan/accept
  cancelWorkflowRun   -> POST /workflows/runs/{id}/cancel
  agentCard   -> GET  /.well-known/agent-card.json
  sendA2AMessage/getA2ATask -> POST /a2a
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

from fastapi.testclient import TestClient

from mac.api import _dashboard_stream_observation_relevant, create_app
from mac.models import TaskState
from mac.services import ControlPlane

# ---------------------------------------------------------------------------
# Test token -- explicit, not ambient MAC_API_TOKEN.
# The Fleet IDE normally receives auth through the local launcher's Vite proxy;
# its manual fallback stores a token in sessionStorage. Tests supply a fixed
# synthetic token directly so there is no dependency on the host environment.
# ---------------------------------------------------------------------------

# A fixed synthetic bearer token used only in this test module.
_TEST_TOKEN = "fleet-ide-test-tok"
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


def _seed_agent(
    cp: ControlPlane,
    name: str = "worker-1",
    capabilities: list[str] | None = None,
) -> str:
    """Register a machine + agent directly via the control plane and return the agent id."""
    machine = cp.register_machine(name + "-host", resources={"cpu": 4, "memory_gb": 8})
    agent = cp.register_agent(machine.id, name, capabilities=capabilities or ["python"])
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
    assert body["llm_usage"] == {
        "schema": "mac.task_llm_usage.v1",
        "observed_route_count": 0,
        "truncated": False,
        "resolved_models": [],
        "response_models": [],
        "requested_models": [],
        "providers": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "model_latency_ms": 0,
        "upstream_attempt_count": 0,
        "routes": [],
    }
    assert body["profile"]["schema"] == "mac.task_execution_profile.v1"
    assert body["profile"]["task_id"] == task_id


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
    resp = client.post(
        "/tasks", json={"description": "no title"}, headers=_AUTH_HEADERS
    )

    assert resp.status_code == 422


def test_workbench_blocked_task_context_and_operator_direction_round_trip():
    cp = ControlPlane.in_memory()
    task_id = _seed_task(cp, title="Waiting for operator direction")
    cp._transition_task_internal(
        task_id,
        TaskState.BLOCKED.value,
        "operator",
        {
            "reason": "missing_target_region",
            "question": "Which production region should receive this deployment?",
            "manual_repair_required": True,
        },
    )
    client = _make_client(cp)

    state_response = client.get("/dashboard/state", headers=_AUTH_HEADERS)
    assert state_response.status_code == 200
    summary = next(
        item for item in state_response.json()["tasks"] if item["task"]["id"] == task_id
    )
    assert summary["task"]["state"] == TaskState.BLOCKED.value
    assert summary["detail_available"] is True

    timeline_response = client.get(
        "/dashboard/tasks/%s/timeline" % task_id, headers=_AUTH_HEADERS
    )
    assert timeline_response.status_code == 200
    blocked_event = timeline_response.json()["history"][-1]
    assert blocked_event["to_state"] == TaskState.BLOCKED.value
    assert blocked_event["detail"]["reason"] == "missing_target_region"
    assert blocked_event["detail"]["question"].startswith("Which production region")

    direction = "Deploy to eu-west-1 and use the existing production account."
    task_detail_response = client.get("/tasks/%s" % task_id, headers=_AUTH_HEADERS)
    assert task_detail_response.status_code == 200
    task = task_detail_response.json()["task"]
    metadata = dict(task.get("metadata") or {})
    metadata["operator_guidance"] = [
        {"actor": "human", "at": "2026-07-03T22:00:00Z", "direction": direction}
    ]
    update_response = client.put(
        "/tasks/" + task_id,
        headers=_AUTH_HEADERS,
        json={
            "actor": "human",
            "description": task["description"]
            + "\n\nOperator direction:\n"
            + direction,
            "metadata": metadata,
        },
    )
    assert update_response.status_code == 200
    assert direction in update_response.json()["description"]
    assert (
        update_response.json()["metadata"]["operator_guidance"][-1]["direction"]
        == direction
    )

    reopen_response = client.post(
        "/tasks/%s/reopen" % task_id,
        headers=_AUTH_HEADERS,
        json={"actor": "human", "reason": direction},
    )
    assert reopen_response.status_code == 200
    assert reopen_response.json()["state"] == TaskState.OPEN.value
    history = cp.task_history(task_id)
    assert history[-1].detail == {"via": "operator_reopen", "reason": direction}


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


def test_workbench_dashboard_state_has_cockpit_collections():
    cp = ControlPlane.in_memory()
    _seed_task(cp, title="cockpit task")
    _seed_agent(cp, name="cockpit-agent")
    client = _make_client(cp)

    resp = client.get("/dashboard/state", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["overview"]["counts"]["active_tasks"] == 1
    assert isinstance(body["tasks"], list)
    assert isinstance(body["agents"], list)
    assert isinstance(body["workflow_runs"], dict)
    assert isinstance(body["service_links"], list)


def test_workbench_ide_state_is_bounded_and_does_not_load_secret_audits(monkeypatch):
    cp = ControlPlane.in_memory()
    dependency = cp.create_task(title="dependency", project="mac")
    for index in range(100):
        cp.create_task(
            title="bounded task %d" % index,
            description="description-" + ("x" * 5_000),
            project="mac",
            dependencies=[dependency.id] if index == 0 else [],
            required_capabilities=["python"] if index == 0 else [],
            metadata={"large": "y" * 10_000},
        )

    def fail_secret_audits():
        raise AssertionError("Fleet IDE projection must not load secret audits")

    monkeypatch.setattr(cp, "list_secret_audits", fail_secret_audits)
    pause_checks = []
    original_project_dispatch_paused = cp._project_dispatch_paused

    def counted_project_dispatch_paused(project):
        pause_checks.append(project)
        return original_project_dispatch_paused(project)

    monkeypatch.setattr(cp, "_project_dispatch_paused", counted_project_dispatch_paused)
    client = _make_client(cp)

    resp = client.get("/dashboard/state?view=ide", headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["schema"] == "mac.dashboard_ide.v1"
    assert body["secret_audits"] == []
    assert len(body["tasks"]) == 101
    projected = next(
        item for item in body["tasks"] if item["task"]["title"] == "bounded task 0"
    )
    assert projected["detail_loaded"] is False
    assert projected["task"]["dependencies"] == [dependency.id]
    assert projected["task"]["required_capabilities"] == ["python"]
    assert "description" not in projected["task"]
    assert "metadata" not in projected["task"]
    assert "history" not in projected
    assert "evidence" not in projected
    assert len(resp.content) < 250_000
    assert pause_checks == []


def test_workbench_ide_state_compresses_and_excludes_virtual_service_agents():
    cp = ControlPlane.in_memory()
    physical_machine = cp.register_machine("worker")
    physical = cp.register_agent(
        physical_machine.id,
        "worker",
        resources={
            "openclaw_runtime": {
                "implementation": "openclaw",
                "verified": True,
            },
            "representation": {
                "human_facing": False,
                "mode": "delegated",
            },
        },
    )
    virtual_machine = cp.register_machine(
        "operator-review",
        labels={"virtual": True},
        resources={"virtual": True},
        machine_id="machine_operator_review",
    )
    cp.register_agent(
        virtual_machine.id,
        "hub-reviewer",
        capabilities=["review"],
        resources={"virtual": True},
    )
    for index in range(40):
        cp.create_task("compressible task %d" % index, project="mac")
    client = _make_client(cp)

    resp = client.get(
        "/dashboard/state?view=ide",
        headers={**_AUTH_HEADERS, "Accept-Encoding": "gzip"},
    )

    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"
    body = resp.json()
    assert [item["agent"]["id"] for item in body["agents"]] == [physical.id]
    assert body["overview"]["counts"]["agents"] == 1
    resources = body["agents"][0]["agent"]["resources"]
    assert resources["openclaw_runtime"]["implementation"] == "openclaw"
    assert resources["representation"]["mode"] == "delegated"


def test_workbench_compact_task_detail_is_explicitly_limited():
    cp = ControlPlane.in_memory()
    task_id = _seed_task(cp, title="compact detail")
    client = _make_client(cp)

    resp = client.get(
        "/tasks/%s?view=compact" % task_id,
        headers=_AUTH_HEADERS,
    )

    assert resp.status_code == 200
    detail = resp.json()
    assert detail["task"]["id"] == task_id
    assert detail["history_limited_to"] == 50
    assert detail["evidence_limited_to"] == 25
    assert detail["reviews_limited_to"] == 25
    assert detail["publications_limited_to"] == 10


def test_dashboard_reads_do_not_observe_themselves_or_trigger_stream_updates():
    cp = ControlPlane.in_memory()
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens=_AUTH_TOKENS,
            record_http_observations=True,
        )
    )

    assert (
        client.get("/dashboard/state?view=ide", headers=_AUTH_HEADERS).status_code
        == 200
    )
    assert client.get("/.well-known/agent-card.json").status_code == 200
    stream_response = client.get(
        "/dashboard/stream?timeout_seconds=0",
        headers={**_AUTH_HEADERS, "Accept-Encoding": "gzip"},
    )
    assert stream_response.status_code == 200
    assert stream_response.headers.get("content-encoding") == "identity"

    api_paths = [
        item.detail.get("path")
        for item in cp.list_observability(limit=20)
        if item.layer == "api"
    ]
    assert "/dashboard/state" not in api_paths
    assert "/dashboard/stream" not in api_paths
    assert "/.well-known/agent-card.json" not in api_paths
    assert not _dashboard_stream_observation_relevant(SimpleNamespace(layer="api"))
    assert _dashboard_stream_observation_relevant(SimpleNamespace(layer="task"))


def test_workbench_agent_card_is_public_and_declares_a2a():
    client = _make_client(ControlPlane.in_memory())

    resp = client.get("/.well-known/agent-card.json")

    assert resp.status_code == 200
    body = resp.json()
    assert body["protocolVersion"]
    assert body["url"].endswith("/a2a")
    assert body["skills"]


def test_workbench_can_delegate_through_a2a():
    cp = ControlPlane.in_memory()
    client = _make_client(cp)
    request = {
        "jsonrpc": "2.0",
        "id": "ide-contract",
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": "msg-ide-contract",
                "contextId": "mac-fleet-workbench",
                "parts": [{"kind": "text", "text": "Verify the A2A workbench flow"}],
            }
        },
    }

    resp = client.post("/a2a", json=request, headers=_AUTH_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "ide-contract"
    assert body["result"]["status"]["state"] == "submitted"
    assert cp.get_task(body["result"]["id"]).title == "Verify the A2A workbench flow"


def test_workbench_can_claim_new_task_for_selected_agent():
    cp = ControlPlane.in_memory()
    task_id = _seed_task(cp, title="assign from inspector")
    agent_id = _seed_agent(cp, name="selected-agent")
    client = _make_client(cp)

    resp = client.post(
        "/tasks/%s/claim?agent_id=%s" % (task_id, agent_id),
        headers=_AUTH_HEADERS,
    )

    assert resp.status_code == 200
    assert resp.json()["task"]["owner_agent_id"] == agent_id


def test_workbench_can_request_review_from_selected_agent():
    cp = ControlPlane.in_memory()
    task_id = _seed_task(cp, title="review from inspector")
    agent_id = _seed_agent(cp, name="selected-reviewer", capabilities=["review"])
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task_id),
    )
    client = _make_client(cp)

    resp = client.post(
        "/tasks/%s/reviews" % task_id,
        json={"reviewer_agent_id": agent_id, "actor": "human"},
        headers=_AUTH_HEADERS,
    )

    assert resp.status_code == 200
    assert resp.json()["reviewer_agent_id"] == agent_id
