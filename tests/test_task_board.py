"""The task board and the router's /v1/messages front door.

The board is how a running agent hears from people and how people see what it
is doing. The agent's sandbox holds only a per-task inference token, so the
board must accept that token for its own task and refuse it everywhere else.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from mac import anthropic_passthrough, router_app
from mac.api import _required_scope, create_app
from mac.inference_tokens import InferenceTokenLifecycle
from mac.models import NotFoundError, ValidationError
from mac.provider_router import Provider
from mac.services import ControlPlane
from mac.task_board import TaskBoard
from mac.test_support import ephemeral_dsn, store_on
from mac.worker_credentials import WorkerCredentialLifecycle


def _plane() -> ControlPlane:
    cp = ControlPlane(
        store_on(ephemeral_dsn(), initialize=True),
        secret_key="task-board-test-key-with-32-bytes-x",
    )
    machine = cp.register_machine("worker-host", machine_id="machine_worker", labels={})
    for name in ("alpha", "beta"):
        cp.register_agent(machine.id, name, ["python"], resources={}, agent_id="agent_" + name)
    return cp


def _bearer(token: str) -> Dict[str, str]:
    return {"Authorization": "Bearer " + token}


def _app(cp: ControlPlane):
    return create_app(
        control_plane=cp,
        auth_tokens={
            "static-admin": {"scopes": ["admin"]},
            "static-writer": {"scopes": ["read", "write"]},
            "static-reader": {"scopes": ["read"]},
        },
    )


def _worker_token(cp: ControlPlane, agent_id: str) -> str:
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issued = lifecycle.issue(agent_id)
    lifecycle.activate(agent_id, issued.record["id"])
    return issued.token


# -- the store -------------------------------------------------------------


def test_board_reads_in_order_after_a_cursor():
    cp = _plane()
    task = cp.create_task("board order")
    board = TaskBoard(cp.store)
    first = board.post(task.id, author_kind="human", author="jkh", kind="directive", body="use X")
    second = board.post(task.id, author_kind="agent", author="agent_alpha", kind="status", body="on it")
    assert [m.id for m in board.list(task.id)] == [first.id, second.id]
    assert [m.id for m in board.list(task.id, after=first.id)] == [second.id]
    assert [m.kind for m in board.list(task.id, kinds=["status"])] == ["status"]


@pytest.mark.parametrize(
    "author_kind,kind",
    [("agent", "directive"), ("agent", "verdict"), ("human", "status"), ("human", "nudge")],
)
def test_authors_may_only_post_their_own_kinds(author_kind, kind):
    cp = _plane()
    task = cp.create_task("board kinds")
    with pytest.raises(ValidationError, match="may not post"):
        TaskBoard(cp.store).post(task.id, author_kind=author_kind, author="x", kind=kind, body="b")


def test_a_reply_must_stay_on_its_task_and_the_task_must_exist():
    cp = _plane()
    one, two = cp.create_task("one"), cp.create_task("two")
    board = TaskBoard(cp.store)
    question = board.post(one.id, author_kind="agent", author="a", kind="question", body="which?")
    with pytest.raises(ValidationError, match="same task"):
        board.post(two.id, author_kind="human", author="h", kind="answer", body="X", reply_to=question.id)
    with pytest.raises(NotFoundError):
        board.post("task_missing", author_kind="human", author="h", kind="message", body="hi")


# -- the API -----------------------------------------------------------------


def test_board_routes_have_their_own_scope_and_messages_is_an_inference_route():
    assert _required_scope("GET", "/tasks/task_1/messages") == "task_board"
    assert _required_scope("POST", "/tasks/task_1/messages") == "task_board"
    assert _required_scope("POST", "/v1/messages") == "inference"
    assert _required_scope("POST", "/v1/messages/count_tokens") == "inference"
    # Inference still opens nothing else under /v1.
    assert _required_scope("POST", "/v1/genai/x") == "agent"


def test_inference_token_reaches_only_its_own_task_and_posts_as_the_agent():
    cp = _plane()
    mine, other = cp.create_task("mine"), cp.create_task("other")
    token = InferenceTokenLifecycle(cp.store).mint("agent_alpha", task_id=mine.id).token
    client = TestClient(_app(cp))

    posted = client.post(
        "/tasks/%s/messages" % mine.id,
        headers=_bearer(token),
        json={"kind": "status", "body": "reading the code"},
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["author_kind"] == "agent"
    assert posted.json()["author"] == "agent_alpha"

    assert client.get("/tasks/%s/messages" % other.id, headers=_bearer(token)).status_code == 403
    assert (
        client.post(
            "/tasks/%s/messages" % other.id, headers=_bearer(token), json={"body": "hi"}
        ).status_code
        == 403
    )
    # An agent cannot give itself orders or record its own verdict.
    for kind in ("directive", "verdict"):
        refused = client.post(
            "/tasks/%s/messages" % mine.id, headers=_bearer(token), json={"kind": kind, "body": "x"}
        )
        assert refused.status_code in (400, 422), refused.text
    # The token still opens nothing else.
    assert client.get("/tasks/%s" % mine.id, headers=_bearer(token)).status_code == 403


def test_people_read_with_read_access_and_direct_with_write_access():
    cp = _plane()
    task = cp.create_task("people")
    client = TestClient(_app(cp))

    directive = client.post(
        "/tasks/%s/messages" % task.id,
        headers=_bearer("static-writer"),
        json={"kind": "directive", "body": "stop refactoring; fix the bug only", "author": "jkh"},
    )
    assert directive.status_code == 200, directive.text
    assert directive.json()["author_kind"] == "human"
    assert directive.json()["author"] == "jkh"

    read = client.get("/tasks/%s/messages" % task.id, headers=_bearer("static-reader"))
    assert read.status_code == 200
    assert [m["kind"] for m in read.json()["messages"]] == ["directive"]
    assert read.json()["cursor"] == directive.json()["id"]

    assert (
        client.post(
            "/tasks/%s/messages" % task.id, headers=_bearer("static-reader"), json={"body": "hi"}
        ).status_code
        == 403
    )
    # A person cannot claim to be the hub; only an admin token can.
    as_hub = client.post(
        "/tasks/%s/messages" % task.id,
        headers=_bearer("static-writer"),
        json={"kind": "nudge", "body": "status?", "author_kind": "hub"},
    )
    assert as_hub.status_code in (400, 422)


def test_agent_credential_needs_ownership_and_its_harness_may_post_as_hub():
    cp = _plane()
    task = cp.create_task("owned")
    cp.claim_task(task.id, "agent_alpha")
    client = TestClient(_app(cp))
    owner, stranger = _worker_token(cp, "agent_alpha"), _worker_token(cp, "agent_beta")

    verdict = client.post(
        "/tasks/%s/messages" % task.id,
        headers=_bearer(owner),
        json={"kind": "verdict", "body": "not met: no test", "author_kind": "hub"},
    )
    assert verdict.status_code == 200, verdict.text
    assert verdict.json()["author_kind"] == "hub"
    assert verdict.json()["author"] == "harness:agent_alpha"

    assert client.get("/tasks/%s/messages" % task.id, headers=_bearer(stranger)).status_code == 403


# -- /v1/messages ------------------------------------------------------------


class _Resp:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._buf = io.BytesIO(body)
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def close(self) -> None:
        pass


def _anthropic() -> Provider:
    return Provider(
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        model_aliases=(("azure/anthropic/claude-opus-4-8", "claude-opus-4-8"),),
    )


def test_provider_is_chosen_by_name_or_by_the_anthropic_host():
    other = Provider(name="openrouter", base_url="https://openrouter.ai/api/v1")
    assert anthropic_passthrough.select_anthropic_provider([other, _anthropic()], {}).name == "anthropic"
    assert anthropic_passthrough.select_anthropic_provider([other], {}) is None
    assert (
        anthropic_passthrough.select_anthropic_provider(
            [other, _anthropic()], {"MAC_ROUTER_ANTHROPIC_PROVIDER": "openrouter"}
        ).name
        == "openrouter"
    )


def test_forward_maps_the_model_adds_the_key_and_keeps_the_callers_credential_home():
    seen: List[Any] = []

    def opener(request, timeout):
        seen.append(request)
        return _Resp(json.dumps({"type": "message", "content": []}).encode())

    status, body, media = anthropic_passthrough.forward(
        _anthropic(),
        "/messages",
        {"model": "azure/anthropic/claude-opus-4-8", "max_tokens": 8, "messages": []},
        {"anthropic-beta": "tools-2024", "authorization": "Bearer task-token"},
        key="sk-hub",
        stream=False,
        timeout=5,
        opener=opener,
    )
    assert (status, media) == (200, "application/json")
    assert body["type"] == "message"
    request = seen[0]
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["x-api-key"] == "sk-hub"
    assert headers["anthropic-version"] == anthropic_passthrough.DEFAULT_ANTHROPIC_VERSION
    assert headers["anthropic-beta"] == "tools-2024"
    assert "authorization" not in headers
    assert json.loads(request.data)["model"] == "claude-opus-4-8"


def test_forward_relays_a_stream_and_reports_upstream_errors():
    def streaming(request, timeout):
        return _Resp(b"event: message_start\ndata: {}\n\n")

    status, chunks, media = anthropic_passthrough.forward(
        _anthropic(), "/messages", {"stream": True}, {}, key="k", stream=True, timeout=5, opener=streaming
    )
    assert (status, media) == (200, "text/event-stream")
    assert b"".join(chunks).startswith(b"event: message_start")

    def failing(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 429, "busy", {}, io.BytesIO(b'{"type":"error","error":{"type":"rate_limit_error"}}')
        )

    status, body, _ = anthropic_passthrough.forward(
        _anthropic(), "/messages", {}, {}, key="k", stream=False, timeout=5, opener=failing
    )
    assert status == 429 and body["error"]["type"] == "rate_limit_error"


def test_messages_route_serves_an_inference_token_through_the_hub_key(monkeypatch):
    cp = _plane()
    task = cp.create_task("router")
    token = InferenceTokenLifecycle(cp.store).mint("agent_alpha", task_id=task.id).token
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-hub")
    calls: List[Any] = []

    def opener(request, timeout):
        calls.append(request)
        return _Resp(json.dumps({"type": "message", "content": [{"type": "text", "text": "hi"}]}).encode())

    app = _app(cp)
    env = {
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_PROVIDERS": "anthropic=https://api.anthropic.com/v1,0,models=claude-opus-4-8,key=ANTHROPIC_API_KEY",
    }
    assert anthropic_passthrough.mount_anthropic_messages(app, env=env, opener=opener)
    client = TestClient(app)
    reply = client.post(
        "/v1/messages",
        headers=_bearer(token),
        json={"model": "claude-opus-4-8", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["content"][0]["text"] == "hi"
    assert {k.lower(): v for k, v in calls[0].header_items()}["x-api-key"] == "sk-hub"


def test_no_anthropic_provider_means_no_messages_route():
    app = _app(_plane())
    env = {"MAC_ROUTER_BACKEND": "inproc", "MAC_ROUTER_PROVIDERS": "nv=https://inference.example/v1,0"}
    assert not anthropic_passthrough.mount_anthropic_messages(app, env=env)
    assert router_app is not None
