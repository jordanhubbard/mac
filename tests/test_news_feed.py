from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.models import utcnow
from mac.services import ControlPlane


def _plane() -> ControlPlane:
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    return cp


def test_news_feed_curates_task_and_agent_lifecycle_with_attribution():
    cp = _plane()
    machine = cp.register_machine("host", machine_id="machine_host")
    agent = cp.register_agent(machine.id, "worker", agent_id="agent_worker", actor="operator")
    task = cp.create_task("Ship the board", project="mac", actor="jkh")
    cp.claim_task(task.id, agent.id)

    feed = cp.list_news(limit=20)
    items = feed["items"]

    assert feed["schema"] == "mac.news.v1"
    assert [item["sequence"] for item in items] == sorted(
        [item["sequence"] for item in items], reverse=True
    )
    claimed = next(item for item in items if item["to_state"] == "claimed")
    assert claimed["task_id"] == task.id
    assert claimed["task_title"] == "Ship the board"
    assert claimed["project"] == "mac"
    assert claimed["actor"] == agent.id
    assert agent.id in claimed["summary"]
    joined = next(item for item in items if item["event_type"] == "agent.registered")
    assert joined["agent_id"] == agent.id
    assert joined["agent_name"] == "worker"


def test_news_cursor_is_oldest_first_and_project_filter_excludes_agents():
    cp = _plane()
    other = cp.create_project("other", dispatch_paused=False)
    cp.create_task("Mac task", project="mac")
    first = cp.list_news(limit=20)
    cursor = first["cursor"]
    cp.create_task("Other task", project=other.name)
    cp.create_task("Later mac task", project="mac")

    page = cp.list_news(after_sequence=cursor, project="mac", limit=20)

    assert [item["task_title"] for item in page["items"]] == ["Later mac task"]
    assert [item["sequence"] for item in page["items"]] == sorted(
        item["sequence"] for item in page["items"]
    )


def test_news_endpoint_and_stream_share_the_safe_shape():
    cp = _plane()
    task = cp.create_task("HTTP news", project="mac")
    # Raw transition detail may carry arbitrary internals. The news shape must not.
    cp._record_history(
        task.id,
        "task.transitioned",
        "agent_worker",
        "open",
        "claimed",
        {
            "secretish_internal": "do not publish",
            "review_failure_class": "hub_verification_error",
            "attempt_refunded": True,
        },
    )
    client = TestClient(create_app(control_plane=cp))

    response = client.get("/news", params={"limit": 10})
    assert response.status_code == 200
    item = next(row for row in response.json()["items"] if row["to_state"] == "claimed")
    assert "secretish_internal" not in item
    assert item["failure_class"] == "hub_verification_error"
    assert item["attempt_refunded"] is True
    assert "attempt refunded" in item["summary"]

    streamed = client.get(
        "/news/stream",
        params={"after_sequence": 0, "timeout_seconds": 0, "poll_interval_seconds": 0.1},
    )
    records = [json.loads(line) for line in streamed.text.splitlines() if line]
    assert streamed.status_code == 200
    assert records
    assert [row["sequence"] for row in records] == sorted(row["sequence"] for row in records)
    assert all("detail" not in row for row in records)


def test_news_reports_meaningful_agent_status_changes():
    cp = _plane()
    machine = cp.register_machine("host", machine_id="machine_host")
    agent = cp.register_agent(machine.id, "worker", agent_id="agent_worker")
    cp.heartbeat_agent(agent.id, status="offline", health_status="degraded")

    item = next(
        row
        for row in cp.list_news(limit=20)["items"]
        if row["event_type"] == "agent.heartbeat_updated"
    )
    assert item["previous_status"] == "idle"
    assert item["status"] == "offline"
    assert "status" in item["changed_fields"]
    assert "idle to offline" in item["summary"]
