from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _client(cp=None, auth_tokens=None):
    return TestClient(
        create_app(control_plane=cp or ControlPlane.in_memory(), auth_tokens=auth_tokens)
    )


def _role_body(slug="code-reviewer"):
    return {
        "slug": slug,
        "name": "Code Reviewer",
        "description": "Reviews PRs.",
        "system_prompt": "You are the reviewer.",
        "level": "ic",
        "default_capabilities": ["review", "python"],
    }


def test_role_crud_round_trip_through_http():
    client = _client()

    create = client.post("/roles", json=_role_body()).json()
    assert create["slug"] == "code-reviewer"
    role_id = create["id"]

    assert client.get("/roles").status_code == 200
    listed = client.get("/roles").json()
    assert any(r["slug"] == "code-reviewer" for r in listed)

    fetched = client.get("/roles/code-reviewer").json()
    assert fetched["id"] == role_id
    assert set(fetched["default_capabilities"]) == {"python", "review"}

    update = client.put(
        "/roles/%s" % role_id, json={"description": "Reviews patches."}
    ).json()
    assert update["description"] == "Reviews patches."
    assert set(update["default_capabilities"]) == {"python", "review"}  # preserved

    delete = client.delete("/roles/%s" % role_id)
    assert delete.status_code == 200
    assert client.get("/roles/code-reviewer").status_code == 404


def test_role_seed_endpoint_loads_loom_catalog():
    client = _client()
    seeded = client.post("/roles/seed", json={}).json()
    assert len(seeded) == 13
    slugs = {r["slug"] for r in seeded}
    assert "ceo" not in slugs
    assert "qa-engineer" in slugs


def test_role_assignment_routes_through_agents_endpoint():
    cp = ControlPlane.in_memory()
    client = _client(cp=cp)
    machine = client.post("/machines", json={"hostname": "host-1"}).json()
    # Bind a soul through the HTTP surface so role assignment passes
    # the soul-precedence check.
    tenant = client.post("/tenants", json={"name": "team"}).json()
    persona = client.post(
        "/personas",
        json={
            "tenant_id": tenant["id"],
            "name": "QA Soul",
            "soul_ref": "h://team/qa/SOUL.md",
            "memory_scope": "h://team/qa/mem",
            "metadata": {"role_slugs": ["qa"]},
        },
    ).json()
    instance = client.post(
        "/hermes-instances",
        json={
            "tenant_id": tenant["id"],
            "name": "qa-instance",
            "persona_id": persona["id"],
        },
    ).json()
    agent = client.post(
        "/agents",
        json={
            "machine_id": machine["id"],
            "name": "rocky",
            "hermes_instance_id": instance["id"],
        },
    ).json()
    client.post("/roles", json=_role_body(slug="qa")).json()

    assigned = client.post(
        "/agents/%s/role" % agent["id"], json={"role_id_or_slug": "qa"}
    ).json()
    assert assigned["role_id"] is not None
    assert "review" in assigned["capabilities"]

    unassigned = client.delete("/agents/%s/role" % agent["id"]).json()
    assert unassigned["role_id"] is None


def test_agent_identity_endpoint_returns_layered_view():
    """GET /agents/{id}/identity returns soul + role + mood + hardware
    as separate fields. Callers compose the LLM prompt themselves."""
    cp = ControlPlane.in_memory()
    client = _client(cp=cp)
    machine = client.post(
        "/machines",
        json={
            "hostname": "host-id",
            "hardware": {"cpu_arch": "arm64", "memory_gb": 32},
        },
    ).json()
    tenant = client.post("/tenants", json={"name": "team"}).json()
    persona = client.post(
        "/personas",
        json={
            "tenant_id": tenant["id"],
            "name": "QA Soul",
            "soul_ref": "h://team/qa/SOUL.md",
            "memory_scope": "h://team/qa/mem",
            "metadata": {"role_slugs": ["qa"]},
        },
    ).json()
    instance = client.post(
        "/hermes-instances",
        json={
            "tenant_id": tenant["id"],
            "name": "qa-instance",
            "persona_id": persona["id"],
        },
    ).json()
    agent = client.post(
        "/agents",
        json={
            "machine_id": machine["id"],
            "name": "rocky",
            "hermes_instance_id": instance["id"],
        },
    ).json()
    client.post("/roles", json=_role_body(slug="qa"))
    client.post(
        "/agents/%s/role" % agent["id"], json={"role_id_or_slug": "qa"}
    )
    # Mood is self-reported by the agent.
    client.post(
        "/agents/%s/mood" % agent["id"],
        json={"mode": "cheerful", "reason": "tests passing"},
    )

    identity = client.get("/agents/%s/identity" % agent["id"])
    assert identity.status_code == 200
    body = identity.json()
    assert body["agent"]["id"] == agent["id"]
    assert body["soul"]["persona"]["name"] == "QA Soul"
    assert body["allowed_role_slugs"] == ["qa"]
    assert body["role"]["slug"] == "qa"
    assert body["mood"]["mode"] == "cheerful"
    assert body["machine_hardware"] == {"cpu_arch": "arm64", "memory_gb": 32}


def test_role_endpoints_respect_scope_admin_or_write_writes():
    cp = ControlPlane.in_memory()
    client = _client(
        cp=cp,
        auth_tokens={
            "reader": ["read"],
            "writer": ["write"],
            "rolesonly": ["roles"],
            "admin": ["admin"],
        },
    )
    body = _role_body()

    # No token → 403.
    assert client.post("/roles", json=body).status_code == 403
    # Read-only → 403 on write.
    assert (
        client.post("/roles", json=body, headers={"Authorization": "Bearer reader"}).status_code
        == 403
    )
    # write implies roles (backwards compat).
    assert (
        client.post("/roles", json=body, headers={"Authorization": "Bearer writer"}).status_code
        == 200
    )
    # Listing is read scope — admin should also pass.
    assert client.get("/roles", headers={"Authorization": "Bearer reader"}).status_code == 200
    # Narrow `roles` scope can still create.
    assert (
        client.post(
            "/roles",
            json=_role_body(slug="qa-engineer"),
            headers={"Authorization": "Bearer rolesonly"},
        ).status_code
        == 200
    )
