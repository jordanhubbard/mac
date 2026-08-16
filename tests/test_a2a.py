"""Tests for inbound A2A (Agent2Agent) federation (ACP roadmap Phase 4).

Exercises the A2A package against an in-memory control plane via the FastAPI
TestClient (no pytest-asyncio), mirroring tests/api/test_api.py. Covers the
AgentCard discovery document, the JSON-RPC surface (message/send -> a real
ledger task, tasks/get state reflection, tasks/cancel), and JSON-RPC error
handling for unknown methods.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import mac

from mac.a2a import agent_card
from mac.a2a.protocol import Method, TaskState
from mac.a2a.service import A2AService, map_task_state
from mac.api import create_app
from mac.models import TaskState as MacTaskState
from mac.services import ControlPlane


# -- AgentCard (pure data) --------------------------------------------------


def test_agent_card_shape():
    card = agent_card("https://hub.example.test")

    assert card["name"] == "mac"
    assert card["description"]
    assert card["url"] == "https://hub.example.test/a2a"
    assert card["version"] == mac.__version__
    # Phase 4 is inbound + polling only: streaming and push are off.
    assert card["capabilities"] == {"streaming": False, "pushNotifications": False}
    assert card["defaultInputModes"] == ["text/plain"]
    assert card["defaultOutputModes"] == ["text/plain"]
    assert isinstance(card["skills"], list) and card["skills"], "at least one skill"
    skill = card["skills"][0]
    assert skill["id"] == "software-task"
    assert skill["name"] and skill["description"]


def test_agent_card_strips_trailing_slash():
    assert agent_card("https://hub.example.test/")["url"] == "https://hub.example.test/a2a"


# -- well-known discovery endpoint ------------------------------------------


def test_well_known_agent_card_is_public(monkeypatch):
    # Unauthenticated even when the hub is token-protected (like /.well-known/acp).
    monkeypatch.setenv("MAC_API_TOKEN", "secret-token")
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    resp = client.get("/.well-known/agent-card.json")  # no Authorization header
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert card["name"] == "mac"
    assert card["url"].endswith("/a2a")
    assert card["capabilities"]["streaming"] is False
    assert card["skills"][0]["id"] == "software-task"


def test_well_known_legacy_agent_json_alias_is_public(monkeypatch):
    monkeypatch.setenv("MAC_API_TOKEN", "secret-token")
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    resp = client.get("/.well-known/agent.json")  # legacy path, no auth
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "mac"


def test_a2a_card_url_reflects_request_host():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    # TestClient default base is http://testserver.
    assert resp.json()["url"] == "http://testserver/a2a"


# -- message/send binds to the ledger ---------------------------------------


def test_message_send_creates_real_ledger_task():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))

    resp = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": "req-1",
            "method": Method.MESSAGE_SEND,
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "msg-abc",
                    "parts": [
                        {"kind": "text", "text": "Fix the flaky test\nin module X"}
                    ],
                }
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == "req-1"
    task = body["result"]

    # Submitted state, and id is a *real* task in the ledger.
    assert task["status"]["state"] == TaskState.SUBMITTED
    assert task["contextId"] == "a2a"
    assert task["kind"] == "task"
    ledger_task = cp.get_task(task["id"])
    assert ledger_task.state == MacTaskState.OPEN.value
    # First line becomes the title; full text the description.
    assert ledger_task.title == "Fix the flaky test"
    assert ledger_task.description == "Fix the flaky test\nin module X"
    # The inbound message is replayed as history.
    assert task["history"][0]["messageId"] == "msg-abc"


def test_message_send_empty_text_is_invalid_params():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    resp = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": Method.MESSAGE_SEND,
            "params": {"message": {"role": "user", "parts": []}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == -32602


# -- tasks/get reflects ledger state ----------------------------------------


def test_tasks_get_reflects_state():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))

    send = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": Method.MESSAGE_SEND,
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "work item"}],
                }
            },
        },
    ).json()
    task_id = send["result"]["id"]

    def get_state() -> str:
        resp = client.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": 9, "method": Method.TASKS_GET, "params": {"id": task_id}},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["result"]["status"]["state"]

    assert get_state() == TaskState.SUBMITTED

    # Drive the ledger task into a "working" state and confirm the projection.
    cp._transition_task_internal(
        task_id, MacTaskState.CLAIMED.value, actor="test-fixture"
    )
    assert get_state() == TaskState.WORKING


def test_tasks_get_unknown_task_is_task_not_found():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    resp = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": Method.TASKS_GET, "params": {"id": "task_missing"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == -32001


# -- tasks/cancel -----------------------------------------------------------


def test_tasks_cancel_cancels_the_ledger_task():
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))

    send = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": Method.MESSAGE_SEND,
            "params": {
                "message": {"role": "user", "parts": [{"kind": "text", "text": "cancel me"}]}
            },
        },
    ).json()
    task_id = send["result"]["id"]

    resp = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 2, "method": Method.TASKS_CANCEL, "params": {"id": task_id}},
    )
    assert resp.status_code == 200, resp.text
    task = resp.json()["result"]
    assert task["status"]["state"] == TaskState.CANCELED
    assert cp.get_task(task_id).state == MacTaskState.CANCELLED.value


# -- JSON-RPC dispatch errors -----------------------------------------------


def test_unknown_method_is_method_not_found():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    resp = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": "tasks/teleport", "params": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    assert body["error"]["code"] == -32601


def test_message_stream_is_method_not_found_until_implemented():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    resp = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": Method.MESSAGE_STREAM, "params": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32601


def test_non_jsonrpc_body_is_invalid_request():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    resp = client.post("/a2a", json={"hello": "world"})
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32600


# -- state mapping table (unit) ---------------------------------------------


def test_state_mapping_covers_all_mac_states():
    cp = ControlPlane.in_memory()

    class _T:
        def __init__(self, state, metadata=None):
            self.state = state
            self.metadata = metadata or {}

    expected = {
        MacTaskState.OPEN.value: TaskState.SUBMITTED,
        MacTaskState.WAITING.value: TaskState.SUBMITTED,
        MacTaskState.BLOCKED.value: TaskState.INPUT_REQUIRED,
        MacTaskState.CLAIMED.value: TaskState.WORKING,
        MacTaskState.RUNNING.value: TaskState.WORKING,
        MacTaskState.NEEDS_REVIEW.value: TaskState.WORKING,
        MacTaskState.REVIEWING.value: TaskState.WORKING,
        MacTaskState.COMPLETED.value: TaskState.COMPLETED,
        MacTaskState.FAILED.value: TaskState.FAILED,
        MacTaskState.CANCELLED.value: TaskState.CANCELED,
    }
    for mac_state, a2a_state in expected.items():
        assert map_task_state(_T(mac_state)) == a2a_state

    # needs_input metadata overrides a non-terminal state to input-required.
    assert (
        map_task_state(_T(MacTaskState.RUNNING.value, {"a2a": {"needs_input": True}}))
        == TaskState.INPUT_REQUIRED
    )
    # ... but not a terminal state.
    assert (
        map_task_state(_T(MacTaskState.COMPLETED.value, {"a2a": {"needs_input": True}}))
        == TaskState.COMPLETED
    )
    # A2AService is constructible from the control plane.
    assert A2AService(cp).cp is cp
