"""Tests for GET /projects/{project}/registration endpoint.

Verifies the bounded canonical project-registration observation endpoint
(schema: mac.project_registration.v1) added to support WebMux issues
#673/#674.

Coverage:
- Registered project returns correct schema + field allowlist.
- 404 for an unknown / unregistered project.
- Allowlist enforces no raw metadata / credentials / task data leakage.
- repository_registration field populated when set, null when absent.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _make_client(cp: ControlPlane) -> TestClient:
    return TestClient(create_app(control_plane=cp))


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_registered_project_returns_v1_schema():
    cp = ControlPlane.in_memory()
    cp.create_project("myproject")
    client = _make_client(cp)

    resp = client.get("/projects/myproject/registration")

    assert resp.status_code == 200
    body = resp.json()
    assert body["schema"] == "mac.project_registration.v1"


def test_registered_project_returns_required_fields():
    cp = ControlPlane.in_memory()
    cp.create_project("myproject")
    client = _make_client(cp)

    body = client.get("/projects/myproject/registration").json()

    assert "id" in body
    assert body["name"] == "myproject"
    assert "description" in body
    assert "status" in body
    assert "created_at" in body
    assert "updated_at" in body
    assert "repository_registration" in body


def test_repository_registration_is_null_when_not_set():
    cp = ControlPlane.in_memory()
    cp.create_project("norepo")
    client = _make_client(cp)

    body = client.get("/projects/norepo/registration").json()

    assert body["repository_registration"] is None


def test_repository_registration_populated_when_set():
    """When the project was registered with a repository URL, that value
    appears in the response under ``repository_registration``."""
    cp = ControlPlane.in_memory()
    cp.create_project(
        "repoproj",
        metadata={"repository_registration": "https://github.com/jordanhubbard/mac.git#main"},
    )
    client = _make_client(cp)

    body = client.get("/projects/repoproj/registration").json()

    assert body["repository_registration"] == "https://github.com/jordanhubbard/mac.git#main"


def test_response_does_not_leak_raw_metadata():
    """The endpoint must expose a strict allowlist — no raw metadata dump."""
    cp = ControlPlane.in_memory()
    cp.create_project("clean", metadata={"some_secret": "hunter2", "worker_id": "w1"})
    client = _make_client(cp)

    body = client.get("/projects/clean/registration").json()

    # These keys must NOT appear; they would indicate raw metadata/task leakage.
    for banned in (
        "tasks",
        "metadata",
        "worker",
        "credentials",
        "bridge_items",
        "repositories",
        "members",
        "agents",
        "some_secret",
        "worker_id",
    ):
        assert banned not in body, f"Leaked field: {banned!r}"


def test_exactly_eight_fields_in_response():
    """Exact allowlist: schema, id, name, description, status, created_at,
    updated_at, repository_registration — 8 fields total."""
    cp = ControlPlane.in_memory()
    cp.create_project("exact")
    client = _make_client(cp)

    body = client.get("/projects/exact/registration").json()

    expected = {
        "schema",
        "id",
        "name",
        "description",
        "status",
        "created_at",
        "updated_at",
        "repository_registration",
    }
    assert set(body.keys()) == expected


def test_project_description_is_returned():
    cp = ControlPlane.in_memory()
    cp.create_project("withdesc", description="Fleet control plane")
    client = _make_client(cp)

    body = client.get("/projects/withdesc/registration").json()

    assert body["description"] == "Fleet control plane"


def test_project_status_reflects_active():
    cp = ControlPlane.in_memory()
    cp.create_project("actproj", status="active")
    client = _make_client(cp)

    body = client.get("/projects/actproj/registration").json()

    assert body["status"] == "active"


# ---------------------------------------------------------------------------
# 404 path
# ---------------------------------------------------------------------------


def test_unregistered_project_returns_404():
    cp = ControlPlane.in_memory()
    client = _make_client(cp)

    resp = client.get("/projects/no-such-project/registration")

    assert resp.status_code == 404


def test_404_message_is_descriptive():
    cp = ControlPlane.in_memory()
    client = _make_client(cp)

    body = client.get("/projects/nonexistent/registration").json()

    err_text = str(body).lower()
    assert "nonexistent" in err_text or "not registered" in err_text or "not found" in err_text
