"""Tests for GET /tasks?view=summary projection.

The list-view projection returns only lightweight fields so that
``mac task list`` downloads a few KB instead of the full task ledger.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane

_LIST_VIEW_FIELDS = frozenset(
    {
        "id",
        "title",
        "project",
        "priority",
        "state",
        "owner_agent_id",
        "dependencies",
        "created_at",
        "updated_at",
        "last_updated_at",
    }
)
_OMITTED_FIELDS = frozenset(
    {
        "metadata",
        "description",
        "required_capabilities",
    }
)


def _client() -> TestClient:
    cp = ControlPlane.in_memory()
    app = create_app(control_plane=cp)
    return TestClient(app)


def _make(
    client: TestClient,
    title: str,
    *,
    project: str = "p",
    dependencies: list[str] | None = None,
) -> dict:
    resp = client.post(
        "/tasks",
        json={
            "title": title,
            "project": project,
            "required_capabilities": ["python"],
            "dependencies": dependencies or [],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_summary_projection_omits_heavy_fields() -> None:
    """view=summary must exclude metadata and description."""
    client = _client()
    _make(client, "task alpha")
    _make(client, "task beta")

    resp = client.get("/tasks", params={"view": "summary"})
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) >= 2
    for task in tasks:
        for field in _OMITTED_FIELDS:
            assert field not in task, "field %r should be absent in summary view" % field


def test_summary_projection_includes_list_view_fields() -> None:
    """view=summary must include the list-view fields."""
    client = _client()
    dependency = _make(client, "task gamma dependency")
    task = _make(client, "task gamma", dependencies=[dependency["id"]])

    resp = client.get("/tasks", params={"view": "summary"})
    assert resp.status_code == 200
    tasks = resp.json()
    assert tasks
    by_id = {item["id"]: item for item in tasks}
    assert by_id[dependency["id"]]["dependencies"] == []
    assert by_id[task["id"]]["dependencies"] == [dependency["id"]]
    for item in tasks:
        for field in _LIST_VIEW_FIELDS:
            assert field in item, "field %r must be present in summary view" % field


def test_no_view_param_returns_full_task() -> None:
    """Without view=summary the full task body is returned."""
    client = _client()
    _make(client, "task delta")

    resp = client.get("/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert tasks
    # full body has metadata
    for task in tasks:
        assert "metadata" in task
        assert "description" in task


def test_full_detail_endpoint_unchanged() -> None:
    """GET /tasks/{id} always returns the full task body."""
    client = _client()
    task = _make(client, "task epsilon")
    task_id = task["id"]

    resp = client.get("/tasks/%s" % task_id)
    assert resp.status_code == 200
    detail = resp.json()
    # task_detail wraps the task under a "task" key and includes history/reviews
    inner = detail.get("task", detail)
    assert "metadata" in inner
    assert "description" in inner


def test_summary_projection_filter_by_state() -> None:
    """view=summary is composable with the state filter."""
    client = _client()
    _make(client, "task zeta")

    resp = client.get("/tasks", params={"view": "summary", "state": "open"})
    assert resp.status_code == 200
    tasks = resp.json()
    assert tasks
    for task in tasks:
        assert "metadata" not in task
        assert task.get("state") == "open"
