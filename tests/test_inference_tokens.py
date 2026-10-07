"""Per-task inference tokens: mint, scope, expiry and the router front door.

A sandboxed coding CLI reaches the hub's model router with a token that may
call POST /v1/chat/completions and /v1/embeddings and nothing else. The
worker's own token, which can claim and write the ledger, never enters the
sandbox (tests/test_opencode_router.py covers that side).
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from mac import router_app
from mac.api import TokenPrincipal, _required_scope, create_app
from mac.inference_tokens import (
    InferenceTokenError,
    InferenceTokenLifecycle,
    InferenceTokenPrincipalProvider,
    request_inference_token,
    revoke_inference_token,
)
from mac.services import ControlPlane
from mac.test_support import ephemeral_dsn, store_on
from mac.worker_credentials import WorkerCredentialLifecycle, _token_hash


class _StubProxy:
    """Stands in for the provider router; records what it was asked."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def complete(self, path: str, body: Dict[str, Any], *, route_context=None):
        self.calls.append({"path": path, "body": body, "route_context": dict(route_context or {})})
        if path == "/embeddings":
            return 200, {"object": "list", "data": [{"embedding": [0.0]}]}
        return 200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    def stream_complete(self, path: str, body: Dict[str, Any], *, route_context=None):
        return self.complete(path, body, route_context=route_context)


def _plane() -> ControlPlane:
    cp = ControlPlane(
        store_on(ephemeral_dsn(), initialize=True),
        secret_key="inference-token-test-key-with-32-bytes",
    )
    machine = cp.register_machine("worker-host", machine_id="machine_worker", labels={})
    for name in ("alpha", "beta"):
        cp.register_agent(machine.id, name, ["python"], resources={}, agent_id="agent_" + name)
    return cp


def _worker_token(cp: ControlPlane, agent_id: str = "agent_alpha") -> str:
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issued = lifecycle.issue(agent_id)
    lifecycle.activate(agent_id, issued.record["id"])
    return issued.token


def _app(cp: ControlPlane):
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "static-admin": {"scopes": ["admin"]},
            "static-writer": {"scopes": ["read", "write"]},
        },
    )
    proxy = _StubProxy()
    assert router_app.mount_router(app, env={"MAC_ROUTER_BACKEND": "inproc"}, proxy=proxy)
    return app, proxy


def _bearer(token: str) -> Dict[str, str]:
    return {"Authorization": "Bearer " + token}


def _chat(client: TestClient, token: str):
    return client.post(
        "/v1/chat/completions",
        headers=_bearer(token),
        json={"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": "hi"}]},
    )


def test_inference_scope_opens_exactly_two_post_routes() -> None:
    assert _required_scope("POST", "/v1/chat/completions") == "inference"
    assert _required_scope("POST", "/v1/embeddings") == "inference"
    assert _required_scope("GET", "/v1/chat/completions") == "agent"
    assert _required_scope("POST", "/v1/responses") == "agent"
    assert _required_scope("POST", "/agents/agent_alpha/inference-tokens") == "agent"
    assert _required_scope("DELETE", "/agents/agent_alpha/inference-tokens/x") == "agent"

    inference_only = TokenPrincipal(scopes=frozenset({"inference"}))
    assert inference_only.has_scope("inference")
    for other in ("agent", "read", "write", "dispatch", "secret", "admin"):
        assert not inference_only.has_scope(other)
    # Every agent credential already reached /v1; it keeps doing so.
    assert TokenPrincipal(scopes=frozenset({"agent"})).has_scope("inference")
    assert not TokenPrincipal(scopes=frozenset({"write"})).has_scope("inference")


def test_inference_token_reaches_the_router_and_nothing_else() -> None:
    cp = _plane()
    worker = _worker_token(cp)
    task = cp.create_task("inference token cannot claim")
    app, proxy = _app(cp)
    with TestClient(app) as client:
        minted = client.post(
            "/agents/agent_alpha/inference-tokens",
            headers=_bearer(worker),
            json={"task_id": task.id, "ttl_seconds": 3600},
        )
        assert minted.status_code == 200, minted.text
        body = minted.json()
        assert body["agent_id"] == "agent_alpha"
        assert body["scopes"] == ["inference"]
        token = body["token"]
        assert token.startswith("mac_inference_")

        chat = _chat(client, token)
        assert chat.status_code == 200, chat.text
        # Attributed to the agent the token is bound to.
        assert proxy.calls[-1]["route_context"]["agent_id"] == "agent_alpha"
        embeddings = client.post(
            "/v1/embeddings", headers=_bearer(token), json={"model": "e", "input": "x"}
        )
        assert embeddings.status_code == 200

        refused = [
            client.get("/tasks", headers=_bearer(token)),
            client.get("/agents", headers=_bearer(token)),
            client.get("/tasks/%s" % task.id, headers=_bearer(token)),
            client.post(
                "/tasks/%s/claim" % task.id,
                headers=_bearer(token),
                params={"agent_id": "agent_alpha"},
            ),
            client.post(
                "/tasks/%s/evidence" % task.id,
                headers=_bearer(token),
                json={"agent_id": "agent_alpha", "kind": "test", "summary": "x"},
            ),
            client.post(
                "/agents/agent_alpha/heartbeat", headers=_bearer(token), json={"status": "idle"}
            ),
            client.post(
                "/v1/responses", headers=_bearer(token), json={"model": "m", "input": "hi"}
            ),
            client.post("/agents/agent_alpha/inference-tokens", headers=_bearer(token), json={}),
        ]
        for response in refused:
            assert response.status_code == 403, (response.request.url, response.text)
            assert "lacks required scope" in response.json()["detail"]
        assert cp.get_task(task.id).owner_agent_id in (None, "")


def test_worker_token_still_reaches_v1() -> None:
    cp = _plane()
    worker = _worker_token(cp)
    app, proxy = _app(cp)
    with TestClient(app) as client:
        assert _chat(client, worker).status_code == 200
    assert proxy.calls[0]["route_context"]["agent_id"] == "agent_alpha"


def test_expired_and_revoked_inference_tokens_are_refused() -> None:
    cp = _plane()
    worker = _worker_token(cp)
    lifecycle = InferenceTokenLifecycle(cp.store)
    expired = lifecycle.mint("agent_alpha", ttl_seconds=600)
    cp.store.execute(
        "UPDATE inference_tokens SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00.000000Z", expired.id),
    )
    app, _proxy = _app(cp)
    with TestClient(app) as client:
        response = _chat(client, expired.token)
        assert response.status_code == 403
        assert response.json()["detail"] == "unknown bearer token"

        live = client.post(
            "/agents/agent_alpha/inference-tokens", headers=_bearer(worker), json={}
        ).json()
        assert _chat(client, live["token"]).status_code == 200
        revoked = client.delete(
            "/agents/agent_alpha/inference-tokens/%s" % live["id"], headers=_bearer(worker)
        )
        assert revoked.status_code == 200
        assert revoked.json()["revoked"] is True
        assert _chat(client, live["token"]).status_code == 403
        again = client.delete(
            "/agents/agent_alpha/inference-tokens/%s" % live["id"], headers=_bearer(worker)
        )
        assert again.status_code == 404


def test_mint_is_allowed_only_for_the_agents_own_id() -> None:
    cp = _plane()
    alpha = _worker_token(cp, "agent_alpha")
    beta_token = InferenceTokenLifecycle(cp.store).mint("agent_beta")
    app, _proxy = _app(cp)
    with TestClient(app) as client:
        peer = client.post("/agents/agent_beta/inference-tokens", headers=_bearer(alpha), json={})
        assert peer.status_code == 403
        assert "bound to agent agent_alpha" in peer.json()["detail"]
        peer_revoke = client.delete(
            "/agents/agent_beta/inference-tokens/%s" % beta_token.id, headers=_bearer(alpha)
        )
        assert peer_revoke.status_code == 403
        writer = client.post(
            "/agents/agent_alpha/inference-tokens", headers=_bearer("static-writer"), json={}
        )
        assert writer.status_code == 403
        admin = client.post(
            "/agents/agent_beta/inference-tokens", headers=_bearer("static-admin"), json={}
        )
        assert admin.status_code == 200
        assert admin.json()["agent_id"] == "agent_beta"
        bad_ttl = client.post(
            "/agents/agent_alpha/inference-tokens",
            headers=_bearer(alpha),
            json={"ttl_seconds": 10},
        )
        assert bad_ttl.status_code == 400


def test_lifecycle_stores_only_the_hash_and_refuses_unknown_agents() -> None:
    cp = _plane()
    lifecycle = InferenceTokenLifecycle(cp.store)
    issued = lifecycle.mint("agent_alpha", task_id="task-x", actor="tester")
    row = cp.store.query_one("SELECT * FROM inference_tokens WHERE id = ?", (issued.id,))
    assert row["token_hash"] == _token_hash(issued.token)
    assert issued.token not in json.dumps(dict(row), default=str)
    assert row["task_id"] == "task-x" and row["created_by"] == "tester"
    projected = InferenceTokenPrincipalProvider(cp.store).tokens()[row["token_hash"]]
    assert projected == {
        "scopes": ["inference"],
        "client_id": issued.id,
        "agent_id": "agent_alpha",
        "principal_kind": "inference",
        "credential_fingerprint": row["token_fingerprint"],
        "task_id": "task-x",
    }
    with pytest.raises(InferenceTokenError, match="registered agent"):
        lifecycle.mint("agent_missing")
    with pytest.raises(InferenceTokenError, match="ttl_seconds"):
        lifecycle.mint("agent_alpha", ttl_seconds=10 * 24 * 60 * 60)
    assert lifecycle.revoke("agent_beta", issued.id) is False


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_worker_client_posts_with_its_own_token(monkeypatch) -> None:
    seen: List[Any] = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        if request.get_method() == "DELETE":
            return _Response(b"{}")
        return _Response(json.dumps({"id": "inference-1", "token": "mac_inference_x"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    payload = request_inference_token(
        "http://hub:8789/", "mac_worker_secret", "agent alpha", task_id="t1", ttl_seconds=600
    )
    assert payload["token"] == "mac_inference_x"
    assert seen[0].full_url == "http://hub:8789/agents/agent%20alpha/inference-tokens"
    assert seen[0].get_header("Authorization") == "Bearer mac_worker_secret"
    assert json.loads(seen[0].data) == {"task_id": "t1", "ttl_seconds": 600}
    revoke_inference_token("http://hub:8789", "mac_worker_secret", "agent_alpha", "inference-1")
    assert seen[1].get_method() == "DELETE"
    assert seen[1].full_url.endswith("/agents/agent_alpha/inference-tokens/inference-1")

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _Response(b'{"token": "nope"}')
    )
    with pytest.raises(InferenceTokenError):
        request_inference_token("http://hub:8789", "w", "agent_alpha")


def test_an_inference_token_never_switches_auth_on_for_an_open_hub() -> None:
    cp = _plane()
    app = create_app(control_plane=cp)
    with TestClient(app) as client:
        minted = client.post("/agents/agent_alpha/inference-tokens", json={})
        assert minted.status_code == 200
        assert client.get("/tasks").status_code == 200
