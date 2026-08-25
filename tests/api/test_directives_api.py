from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _headers(token: str) -> dict:
    return {"Authorization": "Bearer %s" % token}


def test_directive_api_enforces_admin_lifecycle_and_agent_bound_ack(monkeypatch) -> None:
    monkeypatch.setenv("MAC_DIRECTIVES_ENABLED", "1")
    cp = ControlPlane.in_memory()
    cp.store.execute(
        "INSERT INTO projects (id, name, description, metadata, status, created_at, updated_at) "
        "VALUES ('project_demo', 'demo', '', '{}', 'active', 'created', 'updated')"
    )
    cp.store.execute(
        "INSERT INTO project_repositories (id, name, path, source, project, "
        "required_capabilities, enabled, poll_interval_seconds, metadata, created_at, updated_at) "
        "VALUES ('repo_demo', 'demo', '/tmp/demo', 'git@example.invalid:demo/repo.git', "
        "'demo', '[]', 1, 60, '{\"build_system\":\"make\"}', 'created', 'updated')"
    )
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "admin": {"scopes": ["admin"], "client_id": "operator"},
                "writer": {"scopes": ["write"], "tenant_id": "tenant-a"},
                "worker": {"scopes": ["agent"], "agent_id": agent.id},
            },
        )
    )
    document = {
        "schema": "mac.directive.v1",
        "name": "build.bazel-first",
        "description": "Require Bazel for repositories currently using Make.",
        "scope": "fleet",
        "when": {
            "eq": [
                {"fact": "repository.metadata.build_system"},
                {"literal": "make"},
            ]
        },
        "set": {"build.bazel.required": True},
    }

    denied = client.post(
        "/directives",
        headers=_headers("writer"),
        json={"document": document, "actor": "operator"},
    )
    assert denied.status_code == 403
    proposed = client.post(
        "/directives",
        headers=_headers("admin"),
        json={"document": document, "actor": "operator"},
    )
    assert proposed.status_code == 200, proposed.text
    directive = proposed.json()
    version = directive["versions"][0]

    checked = client.post(
        "/directives/%s/check" % directive["id"],
        headers=_headers("admin"),
        json={"version": 1, "actor": "operator"},
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["status"] == "pass"
    approved = client.post(
        "/directives/%s/approve" % directive["id"],
        headers=_headers("admin"),
        json={
            "version": 1,
            "directive_digest": version["digest"],
            "check_id": checked.json()["id"],
            "actor": "operator",
        },
    )
    assert approved.status_code == 200, approved.text
    activated = client.post(
        "/directives/%s/activate" % directive["id"],
        headers=_headers("admin"),
        json={
            "version": 1,
            "directive_digest": version["digest"],
            "actor": "operator",
        },
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["state"] == "distributing"

    effective = client.get(
        "/agents/%s/directives/effective" % agent.id,
        headers=_headers("worker"),
    )
    assert effective.status_code == 200, effective.text
    pending = effective.json()["pending_activations"]
    assert pending[0]["activation_id"] == activated.json()["id"]

    wrong_agent = client.post(
        "/agents/agent_other/directive-activations/%s/ack" % activated.json()["id"],
        headers=_headers("worker"),
        json={"digest": version["digest"]},
    )
    assert wrong_agent.status_code == 403
    ack = client.post(
        "/agents/%s/directive-activations/%s/ack" % (agent.id, activated.json()["id"]),
        headers=_headers("worker"),
        json={"digest": version["digest"]},
    )
    assert ack.status_code == 200, ack.text
    assert ack.json()["state"] == "active"

    repository_policy = client.get(
        "/directives/effective?repository_id=repo_demo",
        headers=_headers("admin"),
    )
    assert repository_policy.status_code == 200, repository_policy.text
    assert repository_policy.json()["set"]["build.bazel.required"] is True
