from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane
from mac.test_support import ephemeral_store
from mac.work_package_service import RepositoryBaseAttestation
from mac.work_plan_admission import CanonicalRepositoryBase


SHA = "c" * 40


class _BaseResolver:
    def resolve(self, repository, *, requested_ref=None):
        return CanonicalRepositoryBase(
            repository_id=repository["id"],
            planning_base_ref=requested_ref or "refs/heads/main",
            planning_base_sha=SHA,
        )


class _AdmissionAttestor:
    def verify(self, repository, *, planning_base_ref, planning_base_sha):
        return RepositoryBaseAttestation(
            repository_id=repository["id"],
            planning_base_ref=planning_base_ref,
            planning_base_sha=planning_base_sha,
            canonical_ref_sha=planning_base_sha,
            source_kind="test",
            verified_at="attested",
            resource_namespace={"status": "unresolved"},
        )


def _proposal() -> dict:
    return {
        "nodes": [
            {
                "node_id": "change",
                "title": "Implement the change",
                "kind": "mutation",
                "effects": {"writes": ["src/mac/change.py"]},
                "expected_outputs": ["candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            },
            {
                "node_id": "assemble",
                "title": "Assemble exact candidate",
                "kind": "integration",
                "depends_on": ["change"],
                "effects": {"reads": ["src/mac/change.py"]},
                "expected_outputs": ["assembled-tree"],
                "verification": {"profile": "integration-default"},
            },
            {
                "node_id": "certify",
                "title": "Certify exact candidate",
                "kind": "certification",
                "depends_on": ["assemble"],
                "effects": {"reads": ["src/mac/change.py"]},
                "expected_outputs": ["certificate"],
                "verification": {"profile": "certification-default"},
            },
        ]
    }


def test_managed_dashboard_preview_and_admin_accept_are_held_and_secret_free() -> None:
    store = ephemeral_store()
    try:
        store.execute(
            "INSERT INTO project_repositories ("
            "id, name, path, source, project, required_capabilities, enabled, "
            "poll_interval_seconds, metadata, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "projectrepo_mac",
                "mac",
                "/controller/private/checkout",
                "git@example.invalid:org/private-mac.git",
                "mac",
                "[]",
                1,
                60,
                "{}",
                "created",
                "updated",
            ),
        )
        cp = ControlPlane(store, secret_key="managed-work-plan-api-test-key-0000001")
        cp.work_packages.repository_verifier = _AdmissionAttestor()
        app = create_app(
            control_plane=cp,
            auth_tokens={
                "admin-token": {"scopes": ["admin"]},
                "write-token": {"scopes": ["write"]},
            },
        )
        app.state.managed_work_plan_bridge.base_resolver = _BaseResolver()
        app.state.workflow_plan_model = lambda _request: _proposal()
        client = TestClient(app)

        preview_response = client.post(
            "/dashboard/workflow-plan/preview",
            headers={"Authorization": "Bearer write-token"},
            json={
                "mode": "managed",
                "goal": "Ship the managed change",
                "project": "mac",
                "package_id": "wp_managed_api",
            },
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["schema"] == "mac.dashboard.managed_work_plan.v1"
        assert preview["activation"]["automatic"] is False
        assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 0
        encoded = json.dumps(preview, sort_keys=True)
        assert "git@example.invalid" not in encoded
        assert "/controller/private/checkout" not in encoded

        preview["plan"]["nodes"][0]["title"] = "Implement operator-edited change"
        acceptance_body = {
            "mode": "managed",
            "goal": preview["goal"],
            "project": preview["project"],
            "plan": preview["plan"],
            "actor": "operator",
            "reason": "operator accepted the edited DAG",
        }
        denied = client.post(
            "/dashboard/workflow-plan/accept",
            headers={"Authorization": "Bearer write-token"},
            json=acceptance_body,
        )
        assert denied.status_code == 403

        accepted_response = client.post(
            "/dashboard/workflow-plan/accept",
            headers={"Authorization": "Bearer admin-token"},
            json=acceptance_body,
        )
        assert accepted_response.status_code == 200, accepted_response.text
        accepted = accepted_response.json()
        assert accepted["schema"] == "mac.dashboard.managed_work_plan_accept.v1"
        assert accepted["package"]["state"] == "admitted"
        assert accepted["held"] is True
        assert accepted["activation"]["automatic"] is False
        tasks = store.query_all("SELECT title, metadata FROM tasks ORDER BY title")
        assert any(
            task["title"] == "Implement operator-edited change" for task in tasks
        )
        assert all(json.loads(task["metadata"])["no_dispatch"] is True for task in tasks)
    finally:
        store.close()
