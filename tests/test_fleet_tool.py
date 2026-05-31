"""Tests for the fleet awareness + agentbus messaging tool (fleet-01).

Loads the vendored module by file path (like the nvidia image-gen test) and
stubs the hub HTTP seam, so nothing here touches a live hub.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from mac import hermes_vendor

pytestmark = pytest.mark.skipif(
    not hermes_vendor.is_vendored(), reason="no vendored Hermes snapshot present"
)


def _load():
    hermes_vendor.ensure_on_path()
    path = Path(hermes_vendor.VENDOR_DIR) / "tools" / "fleet_tool.py"
    spec = importlib.util.spec_from_file_location("mac_test_fleet_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_format_fleet_status_shows_who_is_doing_what():
    mod = _load()
    agents = [
        {"id": "agent_rocky", "name": "rocky", "status": "busy", "health_status": "healthy"},
        {"id": "agent_natasha", "name": "natasha", "status": "idle", "health_status": "healthy"},
    ]
    tasks = [{"id": "task_abc", "owner_agent_id": "agent_rocky", "state": "running", "title": "Ship the connector"}]
    out = mod.format_fleet_status(agents, tasks)
    assert "rocky [busy/healthy]" in out
    assert "working on task_abc" in out and "Ship the connector" in out
    assert "natasha [idle/healthy]" in out  # idle agent shown, no task


def test_format_inbox_empty_and_populated():
    mod = _load()
    assert "Inbox empty" in mod.format_inbox([])
    chunks = [{"sender_agent_id": "natasha", "topic": "chat", "payload": "can you take task X?"}]
    out = mod.format_inbox(chunks)
    assert "from natasha" in out and "task X" in out


def test_check_requirements_needs_hub_env(monkeypatch):
    mod = _load()
    for k in ("MAC_HUB_URL", "MAC_URL", "MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert mod.check_fleet_requirements() is False
    monkeypatch.setenv("MAC_HUB_URL", "http://hub:8789")
    monkeypatch.setenv("MAC_TOKEN", "t")
    assert mod.check_fleet_requirements() is True


def test_status_action_queries_agents_and_tasks(monkeypatch):
    mod = _load()
    calls = []

    def fake_req(method, path, payload=None, **kw):
        calls.append((method, path))
        if path == "/agents":
            return [{"id": "agent_rocky", "name": "rocky", "status": "busy", "health_status": "healthy"}]
        if path.startswith("/tasks"):
            return [{"id": "t1", "owner_agent_id": "agent_rocky", "state": "running", "title": "X"}]
        return []

    monkeypatch.setattr(mod, "_hub_request", fake_req)
    out = mod._handle_fleet({"action": "status"})
    assert "rocky" in out and "working on t1" in out
    assert ("GET", "/agents") in calls


def test_message_action_resolves_name_and_posts(monkeypatch):
    mod = _load()
    monkeypatch.setenv("MAC_AGENT_ID", "agent_rocky")
    posted = {}

    def fake_req(method, path, payload=None, **kw):
        if path == "/agents":
            return [{"id": "agent_natasha", "name": "natasha"}]
        if method == "POST" and path == "/agentbus":
            posted.update(payload)
            return {"stream_id": "s1"}
        return []

    monkeypatch.setattr(mod, "_hub_request", fake_req)
    out = mod._handle_fleet({"action": "message", "recipient": "natasha", "message": "hi"})
    assert json.loads(out)["delivered_to"] == "agent_natasha"
    assert posted["sender_agent_id"] == "agent_rocky"
    assert posted["recipient_agent_id"] == "agent_natasha"
    assert posted["payload"] == "hi"


def test_unknown_action_errors():
    mod = _load()
    out = mod._handle_fleet({"action": "bogus"})
    assert "unknown fleet action" in out.lower() or "error" in out.lower()
