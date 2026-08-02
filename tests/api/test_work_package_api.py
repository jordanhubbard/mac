from __future__ import annotations

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane
from mac.test_support import ephemeral_store
from mac.work_package_models import WORK_PACKAGE_PLAN_SCHEMA
from mac.work_package_service import RepositoryBaseAttestation


BASE_SHA = "a" * 40


class _Verifier:
    def verify(self, repository, *, planning_base_ref, planning_base_sha):
        return RepositoryBaseAttestation(
            repository_id=repository["id"],
            planning_base_ref=planning_base_ref,
            planning_base_sha=planning_base_sha,
            canonical_ref_sha=planning_base_sha,
            source_kind="test",
            verified_at="attested",
            resource_namespace={
                "status": "resolved",
                "case_sensitive": True,
                "unicode_normalization": "NFC",
                "symlink_resolution": "resolved",
            },
        )


def _plan() -> dict:
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_api",
        "goal": "Prove the package control API",
        "project": "mac",
        "repository_id": "repo_mac",
        "resource_namespace": {
            "case_sensitive": True,
            "unicode_normalization": "NFC",
            "symlink_resolution": "resolved",
        },
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": BASE_SHA,
        "plan_generation": 1,
        "mutation_wip": {"max_tokens": 1},
        "nodes": [
            {
                "node_key": "change",
                "title": "Change source",
                "kind": "mutation",
                "effects": {"writes": ["src"]},
                "expected_outputs": ["candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            }
        ],
    }


def test_admin_can_admit_inspect_and_readiness_gate_activation(monkeypatch) -> None:
    store = ephemeral_store()
    try:
        store.execute(
            "INSERT INTO project_repositories ("
            "id, name, path, source, project, required_capabilities, enabled, "
            "poll_interval_seconds, metadata, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "repo_mac",
                "mac",
                "/tmp/mac",
                "ssh://git@example.invalid/mac.git",
                "mac",
                "[]",
                1,
                60,
                "{}",
                "created",
                "updated",
            ),
        )
        cp = ControlPlane(store, secret_key="work-package-api-test-key-value-0001")
        cp.work_packages.repository_verifier = _Verifier()
        app = create_app(
            control_plane=cp,
            auth_tokens={
                "admin-token": {"scopes": ["admin"]},
                "write-token": {"scopes": ["write"]},
            },
        )
        client = TestClient(app)
        admin = {"Authorization": "Bearer admin-token"}

        denied = client.post(
            "/work-packages",
            headers={"Authorization": "Bearer write-token"},
            json={"plan": _plan(), "reason": "test"},
        )
        assert denied.status_code == 403

        admitted = client.post(
            "/work-packages",
            headers=admin,
            json={"plan": _plan(), "reason": "approved test plan"},
        )
        assert admitted.status_code == 200, admitted.text
        assert admitted.json()["package"]["state"] == "admitted"

        listed = client.get("/work-packages?project=mac", headers=admin)
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()] == ["wp_api"]
        described = client.get("/work-packages/wp_api", headers=admin)
        assert described.status_code == 200
        assert described.json()["nodes"][0]["node_state"] == "planned"
        blocked = client.get(
            "/work-packages/wp_api/activation-readiness", headers=admin
        )
        assert blocked.json()["ready"] is False

        machine = cp.register_machine("api-worker-host")
        agent = cp.register_agent(
            machine.id,
            "api-worker",
            capabilities=["work_package_v1"],
        )
        monkeypatch.setattr(
            "mac.worker_credentials.package_worker_readiness",
            lambda _store, agent_id: {
                "ready": agent_id == agent.id,
                "reason": "test-ready",
            },
        )
        monkeypatch.setattr(
            cp,
            "_work_package_downstream_activation_readiness",
            lambda _described: {"ready": True, "code": "ready", "reason": ""},
        )
        activated = client.post(
            "/work-packages/wp_api/activate",
            headers=admin,
            json={"expected_plan_version": 1, "expected_epoch": 1},
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["package"]["state"] == "active"
    finally:
        store.close()
