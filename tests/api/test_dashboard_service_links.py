"""Tests for /dashboard/service-links and /dashboard/service-links/{id}/navigate."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _client() -> TestClient:
    return TestClient(create_app(control_plane=ControlPlane.in_memory()))


@pytest.fixture(autouse=True)
def _isolate_service_env(monkeypatch):
    """`_lookup_config_value` falls back to ~/.mac/mac.env, ~/.hermes/.env,
    ~/.tokenhub/*.env, /etc/<fleet>/*.env when an env var is unset. On a
    deployed host those files actually contain the service URLs, which
    makes ``monkeypatch.delenv(TOKENHUB_URL)`` ineffective and breaks
    the "absent when not configured" assertions. Force the lookup to
    return an empty file list so the tests really do see "unconfigured".
    """
    monkeypatch.setattr("mac.api._service_env_files", lambda: [])


# ---------------------------------------------------------------------------
# /dashboard/state — service_links field
# ---------------------------------------------------------------------------


def test_service_links_absent_when_no_services_configured(monkeypatch):
    for key in ("TOKENHUB_URL", "QDRANT_URL", "QDRANT_ADDRESS", "FIRECRAWL_API_URL", "FIRECRAWL_GATEWAY_URL"):
        monkeypatch.delenv(key, raising=False)

    client = _client()
    state = client.get("/dashboard/state").json()
    links = {item["id"]: item for item in state.get("service_links", [])}
    assert "tokenhub" not in links or not links["tokenhub"].get("ui_url")
    assert "qdrant" not in links or not links["qdrant"].get("ui_url")
    assert "firecrawl" not in links or not links["firecrawl"].get("ui_url")


def test_service_links_present_when_services_configured(monkeypatch):
    monkeypatch.setenv("TOKENHUB_URL", "http://tokenhub.internal:8090")
    monkeypatch.setenv("TOKENHUB_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.internal:6333")
    monkeypatch.setenv("QDRANT_API_KEY", "qdrant-key")
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://firecrawl.internal:3002")

    client = _client()
    state = client.get("/dashboard/state").json()
    links = {item["id"]: item for item in state["service_links"]}

    assert "tokenhub" in links
    assert "qdrant" in links
    assert "firecrawl" in links


def test_kanban_link_present_when_hermes_dashboard_configured(monkeypatch):
    monkeypatch.setenv("MAC_HERMES_DASHBOARD_URL", "http://hermes.internal:8765")
    client = _client()
    links = {item["id"]: item for item in client.get("/dashboard/state").json()["service_links"]}
    assert "kanban" in links
    k = links["kanban"]
    assert k["status"] == "configured"
    assert "hermes.internal:8765/kanban" in k["ui_url"]
    # Links out to the Hermes dashboard (separate login) — no credential pass-through.
    assert k["auth"]["credential_pass_through"] is False


def test_kanban_link_not_configured_without_hermes_dashboard_url(monkeypatch):
    monkeypatch.delenv("MAC_HERMES_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_URL", raising=False)
    client = _client()
    links = {item["id"]: item for item in client.get("/dashboard/state").json()["service_links"]}
    # Present in the list but unconfigured -> no ui_url, so the sidebar hides it.
    assert links["kanban"]["status"] == "not_configured"
    assert not links["kanban"]["ui_url"]


def test_service_links_redact_credentials(monkeypatch):
    monkeypatch.setenv("TOKENHUB_URL", "http://tokenhub.internal:8090")
    monkeypatch.setenv("TOKENHUB_ADMIN_TOKEN", "super-secret-admin-key")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.internal:6333")
    monkeypatch.setenv("QDRANT_API_KEY", "super-secret-qdrant-key")
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://firecrawl.internal:3002")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "super-secret-firecrawl-key")

    client = _client()
    rendered = str(client.get("/dashboard/state").json())

    assert "super-secret-admin-key" not in rendered
    assert "super-secret-qdrant-key" not in rendered
    assert "super-secret-firecrawl-key" not in rendered


# ---------------------------------------------------------------------------
# /dashboard/service-links/{id}/navigate
# ---------------------------------------------------------------------------


def test_navigate_unknown_service_returns_404():
    client = _client()
    resp = client.get("/dashboard/service-links/unknown-service/navigate")
    assert resp.status_code == 404


def test_navigate_tokenhub_redirects_to_sso(monkeypatch):
    monkeypatch.setenv("TOKENHUB_URL", "http://tokenhub.internal:8090")
    monkeypatch.setenv("TOKENHUB_ADMIN_TOKEN", "admin-token-at-least-32-chars!!")

    client = _client()
    resp = client.get("/dashboard/service-links/tokenhub/navigate")
    assert resp.status_code == 200
    url = resp.json()["url"]
    assert "tokenhub.internal:8090" in url


def test_navigate_tokenhub_missing_config_returns_error(monkeypatch):
    monkeypatch.delenv("TOKENHUB_URL", raising=False)

    client = _client()
    resp = client.get("/dashboard/service-links/tokenhub/navigate")
    assert resp.status_code in (400, 404, 422, 500)


def test_navigate_qdrant_returns_dashboard_url_with_api_key(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.internal:6333")
    monkeypatch.setenv("QDRANT_API_KEY", "qdrant-secret")

    client = _client()
    resp = client.get("/dashboard/service-links/qdrant/navigate")
    assert resp.status_code == 200
    url = resp.json()["url"]
    assert "qdrant.internal:6333/dashboard" in url
    assert "api_key=qdrant-secret" in url


def test_navigate_qdrant_without_api_key_returns_plain_dashboard_url(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.internal:6333")
    monkeypatch.delenv("QDRANT_API_KEY", raising=False)
    monkeypatch.delenv("QDRANT_FLEET_KEY", raising=False)

    client = _client()
    resp = client.get("/dashboard/service-links/qdrant/navigate")
    assert resp.status_code == 200
    url = resp.json()["url"]
    assert "qdrant.internal:6333/dashboard" in url
    assert "api_key" not in url


def test_navigate_qdrant_missing_config_returns_error(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    monkeypatch.delenv("QDRANT_ADDRESS", raising=False)
    monkeypatch.delenv("QDRANT_FLEET_URL", raising=False)

    client = _client()
    resp = client.get("/dashboard/service-links/qdrant/navigate")
    assert resp.status_code in (400, 404, 422, 500)


def test_navigate_firecrawl_returns_base_url(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://firecrawl.internal:3002")

    client = _client()
    resp = client.get("/dashboard/service-links/firecrawl/navigate")
    assert resp.status_code == 200
    url = resp.json()["url"]
    assert "firecrawl.internal:3002" in url


def test_navigate_firecrawl_missing_config_returns_error(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.delenv("FIRECRAWL_GATEWAY_URL", raising=False)

    client = _client()
    resp = client.get("/dashboard/service-links/firecrawl/navigate")
    assert resp.status_code in (400, 404, 422, 500)


# ---------------------------------------------------------------------------
# Pass-through flags on service_links items
# ---------------------------------------------------------------------------


def test_tokenhub_service_link_has_pass_through_flag(monkeypatch):
    monkeypatch.setenv("TOKENHUB_URL", "http://tokenhub.internal:8090")
    monkeypatch.setenv("TOKENHUB_ADMIN_TOKEN", "admin-token")

    client = _client()
    state = client.get("/dashboard/state").json()
    links = {item["id"]: item for item in state["service_links"]}
    th = links["tokenhub"]
    assert th["auth"]["credential_pass_through"] is True
    assert "/dashboard/service-links/tokenhub/" in th["auth"]["pass_through_url"]


def test_qdrant_service_link_has_pass_through_flag_when_configured(monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.internal:6333")
    monkeypatch.setenv("QDRANT_API_KEY", "qdrant-key")

    client = _client()
    state = client.get("/dashboard/state").json()
    links = {item["id"]: item for item in state["service_links"]}
    q = links["qdrant"]
    assert q["auth"]["credential_pass_through"] is True
    assert "/dashboard/service-links/qdrant/" in q["auth"]["pass_through_url"]
