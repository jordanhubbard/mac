"""Standalone router service (mac.router_service): auth, mounting, isolation."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mac import router_service


PROVIDERS = "local=http://127.0.0.1:9999/v1,0,key=none"


def _env(**extra: str) -> dict:
    env = {
        "MAC_ROUTER_BACKEND": "standalone",
        "MAC_ROUTER_PROVIDERS": PROVIDERS,
        "MAC_API_TOKEN": "hub-local-token",
        "MAC_WORKER_TOKEN": "hub-facing-worker-token",
    }
    env.update(extra)
    return env


def test_requires_at_least_one_bearer_token():
    with pytest.raises(RuntimeError, match="bearer token"):
        router_service.build_router_app(
            {"MAC_ROUTER_BACKEND": "standalone", "MAC_ROUTER_PROVIDERS": PROVIDERS}
        )


def test_healthz_is_open_and_v1_requires_bearer():
    app = router_service.build_router_app(_env())
    client = TestClient(app)
    assert client.get("/healthz").json()["status"] == "ok"

    denied = client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert denied.status_code == 401

    wrong = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers={"Authorization": "Bearer nope"},
    )
    assert wrong.status_code == 401


def test_accepts_any_configured_token():
    app = router_service.build_router_app(
        _env(MAC_ROUTER_TOKENS="replica-token-a, replica-token-b")
    )
    client = TestClient(app)
    for token in ("hub-local-token", "hub-facing-worker-token", "replica-token-a", "replica-token-b"):
        response = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer %s" % token},
        )
        # Auth passes; the fake upstream is unreachable, so the router
        # answers with a provider error rather than a 401.
        assert response.status_code != 401


def test_hub_api_does_not_mount_v1_when_backend_standalone():
    """The ledger API and the router must not both serve /v1."""
    from fastapi import FastAPI

    from mac.router_app import mount_router

    app = FastAPI()
    mounted = mount_router(
        app,
        env={"MAC_ROUTER_BACKEND": "standalone", "MAC_ROUTER_PROVIDERS": PROVIDERS},
    )
    assert mounted is False
    assert all(getattr(r, "path", "") != "/v1/chat/completions" for r in app.routes)


def test_no_providers_fails_closed():
    with pytest.raises(RuntimeError, match="no router routes mounted"):
        router_service.build_router_app(
            {"MAC_ROUTER_BACKEND": "standalone", "MAC_API_TOKEN": "t"}
        )
