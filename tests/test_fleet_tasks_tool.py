"""The `tasks` tool gives a chat agent read/write access to the SHARED hub task
ledger (so work is visible fleet-wide), unlike the session-local `todo` tool.

These tests drive the tool's action router with a stubbed _hub_request and
assert it builds the exact requests the hub /tasks API expects."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERMES = Path(__file__).resolve().parents[1] / "src" / "mac" / "_hermes"
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

from tools import fleet_tool  # noqa: E402


@pytest.fixture
def capture(monkeypatch):
    """Capture (method, path, payload) and return a canned response per path."""
    calls = []

    def fake_hub_request(method, path, payload=None, **kw):
        calls.append((method, path, payload))
        if method == "GET" and (path == "/tasks" or path.startswith("/tasks?")):
            return [
                {"id": "task_aaa", "state": "open", "title": "embed API", "owner_agent_id": None},
                {"id": "task_bbb", "state": "running", "title": "vector math", "owner_agent_id": "rocky"},
            ]
        if "/claim" in path:
            return {"task": {"state": "claimed"}, "lease": {"id": "lease_1"}}
        if "/transition" in path:
            return {"state": (payload or {}).get("target_state")}
        if method == "POST" and path == "/tasks":
            return {"id": "task_new", "state": "open", "title": (payload or {}).get("title")}
        return {"id": "task_aaa", "state": "open", "title": "embed API"}

    monkeypatch.setattr(fleet_tool, "_hub_request", fake_hub_request)
    return calls


def test_create_posts_title_and_actor(capture, monkeypatch):
    monkeypatch.setenv("MAC_AGENT_ID", "rocky")
    out = json.loads(fleet_tool._handle_tasks(
        {"action": "create", "title": "Add embedding API", "description": "for dreaming", "priority": 2}))
    method, path, payload = capture[-1]
    assert (method, path) == ("POST", "/tasks")
    assert payload == {"title": "Add embedding API", "description": "for dreaming", "priority": 2, "actor": "rocky"}
    assert out["success"] and out["task_id"] == "task_new"


def test_claim_passes_agent_id_as_query(capture, monkeypatch):
    monkeypatch.setenv("MAC_AGENT_ID", "natasha")
    out = json.loads(fleet_tool._handle_tasks({"action": "claim", "task_id": "task_aaa"}))
    method, path, _ = capture[-1]
    assert method == "POST"
    assert path == "/tasks/task_aaa/claim?agent_id=natasha"
    assert out["owner"] == "natasha" and out["state"] == "claimed"


def test_close_completed_vs_cancelled(capture, monkeypatch):
    monkeypatch.setenv("MAC_AGENT_ID", "rocky")
    json.loads(fleet_tool._handle_tasks({"action": "close", "task_id": "task_bbb", "reason": "done"}))
    _, path, payload = capture[-1]
    assert path == "/tasks/task_bbb/transition"
    assert payload["target_state"] == "completed" and payload["detail"] == {"reason": "done"}

    json.loads(fleet_tool._handle_tasks({"action": "close", "task_id": "task_bbb", "cancelled": True}))
    _, _, payload2 = capture[-1]
    assert payload2["target_state"] == "cancelled" and payload2["detail"] == {}


def test_list_renders_shared_backlog(capture):
    out = fleet_tool._handle_tasks({"action": "list"})
    assert capture[-1][:2] == ("GET", "/tasks")
    assert "task_aaa" in out and "[open]" in out and "owner: rocky" in out


def test_list_state_filter_is_query(capture):
    fleet_tool._handle_tasks({"action": "list", "state": "open"})
    assert capture[-1][1] == "/tasks?state=open"


def test_claim_without_agent_id_errors(capture, monkeypatch):
    monkeypatch.delenv("MAC_AGENT_ID", raising=False)
    monkeypatch.delenv("MAC_WORKER_AGENT_ID", raising=False)
    out = fleet_tool._handle_tasks({"action": "claim", "task_id": "task_aaa"})
    assert "agent id" in out.lower()


def test_create_requires_title(capture):
    out = fleet_tool._handle_tasks({"action": "create", "title": ""})
    assert "title" in out.lower()


def test_check_fn_gates_on_hub_env(monkeypatch):
    monkeypatch.setenv("MAC_HUB_URL", "http://hub:8789")
    monkeypatch.setenv("MAC_API_TOKEN", "tok")
    assert fleet_tool.check_fleet_requirements() is True
    monkeypatch.delenv("MAC_HUB_URL", raising=False)
    monkeypatch.delenv("MAC_URL", raising=False)
    assert fleet_tool.check_fleet_requirements() is False


def test_tasks_tool_registered():
    from tools.registry import registry
    entry = registry._tools.get("tasks")
    assert entry is not None and entry.toolset == "fleet"
