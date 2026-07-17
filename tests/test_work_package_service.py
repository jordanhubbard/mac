from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mac.models import ValidationError, json_dumps, json_loads
from mac.services import ControlPlane
from mac.store import SQLiteStore
from mac.work_package_models import WORK_PACKAGE_PLAN_SCHEMA
from mac.work_package_service import (
    GitRepositoryBaseVerifier,
    RepositoryBaseAttestation,
    WorkPackageService,
)


class _Verifier:
    def __init__(self, *, sha: str = "a" * 40) -> None:
        self.sha = sha
        self.calls = []

    def verify(self, repository, *, planning_base_ref, planning_base_sha):
        self.calls.append((dict(repository), planning_base_ref, planning_base_sha))
        return RepositoryBaseAttestation(
            repository_id=repository["id"],
            planning_base_ref=planning_base_ref,
            planning_base_sha=planning_base_sha,
            canonical_ref_sha=self.sha,
            source_kind="test",
            verified_at="attested",
            resource_namespace={"status": "unresolved"},
        )


def _register_repository(store: SQLiteStore) -> None:
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "projectrepo_mac",
            "mac",
            "/tmp/mac",
            "git@example.invalid:mac.git",
            "mac",
            "[]",
            1,
            60,
            "{}",
            "created",
            "updated",
        ),
    )


def _plan(*, package_id: str = "wp_service", goal: str = "Ship safely") -> dict:
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": package_id,
        "goal": goal,
        "project": "mac",
        "repository_id": "projectrepo_mac",
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": "a" * 40,
        "plan_generation": 1,
        "max_in_flight": 2,
        "nodes": [
            {
                "node_key": "build",
                "title": "Build the change",
                "node_type": "mutation",
                "effects": {"writes": ["src/mac"]},
                "expected_outputs": ["candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            },
            {
                "node_key": "assemble",
                "title": "Assemble exact candidate",
                "node_type": "integration",
                "depends_on": ["build"],
                "expected_outputs": ["assembled-tree"],
                "verification": {"profile": "integration-default"},
            },
            {
                "node_key": "certify",
                "title": "Certify exact candidate",
                "node_type": "certification",
                "depends_on": ["assemble"],
                "expected_outputs": ["certificate"],
                "verification": {"profile": "certification-default"},
            },
        ],
    }


def test_admission_atomically_materializes_a_held_dag() -> None:
    store = SQLiteStore(":memory:")
    try:
        _register_repository(store)
        verifier = _Verifier()
        service = WorkPackageService(store, repository_verifier=verifier)
        result = service.admit(
            _plan(), actor="planner-controller", reason="approved proposal"
        )

        assert result.created is True
        assert result.held is True
        assert result.package.state == "admitted"
        assert result.package.current_plan_version == 1
        assert result.package.current_epoch == 1
        assert len(result.task_ids) == 3
        assert len(verifier.calls) == 1

        tasks = store.query_all("SELECT * FROM tasks ORDER BY id")
        assert len(tasks) == 3
        assert {task["state"] for task in tasks} == {"open", "waiting"}
        assert all(json_loads(task["metadata"], {})["no_dispatch"] for task in tasks)
        assert all(
            "work_package_v1" in json_loads(task["required_capabilities"], [])
            for task in tasks
        )

        links = store.query_all(
            "SELECT * FROM work_package_task_links ORDER BY node_key"
        )
        assert [row["node_key"] for row in links] == ["assemble", "build", "certify"]
        assert {row["node_state"] for row in links} == {"planned"}
        task_by_node = {row["node_key"]: row["task_id"] for row in links}
        build = store.query_one("SELECT * FROM tasks WHERE id = ?", (task_by_node["build"],))
        assemble = store.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task_by_node["assemble"],)
        )
        certify = store.query_one(
            "SELECT * FROM tasks WHERE id = ?", (task_by_node["certify"],)
        )
        assert json_loads(build["dependencies"], []) == []
        assert json_loads(assemble["dependencies"], []) == [task_by_node["build"]]
        assert json_loads(certify["dependencies"], []) == [task_by_node["assemble"]]
        described = service.describe(result.package.id)
        assert described["package"]["id"] == result.package.id
        assert described["plan"]["plan_digest"] == result.plan_digest
        assert described["epoch"]["epoch"] == 1
        assert [node["node_key"] for node in described["nodes"]] == [
            "assemble",
            "build",
            "certify",
        ]
        assert service.get(result.package.id).id == result.package.id
        assert [package.id for package in service.list(project="mac")] == [
            result.package.id
        ]
        assert store.query_all("PRAGMA foreign_key_check") == []
    finally:
        store.close()


def test_admission_rejects_composed_mutation_bases_without_side_effects() -> None:
    store = SQLiteStore(":memory:")
    try:
        _register_repository(store)
        verifier = _Verifier()
        service = WorkPackageService(store, repository_verifier=verifier)
        unsafe = _plan(package_id="wp_unsafe_composed_base")
        unsafe["nodes"].insert(
            1,
            {
                "node_key": "followup",
                "title": "Build on the first mutation",
                "node_type": "mutation",
                "depends_on": ["build"],
                "effects": {"writes": ["src/followup"]},
                "expected_outputs": ["followup-candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            },
        )
        assemble = next(
            node for node in unsafe["nodes"] if node["node_key"] == "assemble"
        )
        assemble["depends_on"] = ["followup"]

        with pytest.raises(ValidationError, match="flat mutation wave"):
            service.admit(
                unsafe,
                actor="planner-controller",
                reason="must not silently omit build",
            )

        assert verifier.calls == []
        assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 0
        assert store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0
    finally:
        store.close()


def test_activation_fences_unsafe_plan_persisted_by_an_older_controller() -> None:
    store = SQLiteStore(":memory:")
    try:
        _register_repository(store)
        service = WorkPackageService(store, repository_verifier=_Verifier())
        plan = _plan(package_id="wp_old_unsafe_topology")
        plan["nodes"].insert(
            1,
            {
                "node_key": "second",
                "title": "Second parallel mutation",
                "node_type": "mutation",
                "effects": {"writes": ["src/second"]},
                "expected_outputs": ["second-candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            },
        )
        assemble = next(node for node in plan["nodes"] if node["node_key"] == "assemble")
        assemble["depends_on"] = ["build", "second"]
        admitted = service.admit(plan, actor="old-controller", reason="old policy")

        row = store.query_one(
            "SELECT definition FROM work_package_plan_versions "
            "WHERE package_id = ? AND version = 1",
            (admitted.package.id,),
        )
        definition = json_loads(row["definition"], {})
        second = next(
            node for node in definition["nodes"] if node["node_key"] == "second"
        )
        second["depends_on"] = ["build"]
        # Simulate an immutable definition written before the topology policy
        # existed. The production trigger remains part of the normal boundary.
        store.execute("DROP TRIGGER trg_work_package_plan_versions_immutable")
        store.execute(
            "UPDATE work_package_plan_versions SET definition = ? "
            "WHERE package_id = ? AND version = 1",
            (json_dumps(definition), admitted.package.id),
        )

        with pytest.raises(ValidationError, match="flat mutation wave"):
            service.activate(
                admitted.package.id,
                expected_plan_version=1,
                expected_epoch=1,
                actor="operator",
            )

        assert service.get(admitted.package.id).state == "admitted"
        tasks = store.query_all("SELECT state, metadata FROM tasks")
        assert {row["state"] for row in tasks} == {"open", "waiting"}
        assert all(json_loads(row["metadata"], {})["no_dispatch"] for row in tasks)
    finally:
        store.close()


def test_exact_admission_retry_is_idempotent_but_package_id_reuse_is_rejected() -> None:
    store = SQLiteStore(":memory:")
    try:
        _register_repository(store)
        verifier = _Verifier()
        service = WorkPackageService(store, repository_verifier=verifier)
        first = service.admit(_plan(), actor="controller", reason="initial")
        verifier.sha = "b" * 40  # the canonical branch may move after admission
        second = service.admit(_plan(), actor="controller", reason="retry")

        assert first.created is True
        assert second.created is False
        assert set(first.task_ids) == set(second.task_ids)
        assert len(verifier.calls) == 1
        assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 1
        assert store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 3
        assert store.query_one("SELECT COUNT(*) AS n FROM work_package_history")["n"] == 1

        with pytest.raises(ValidationError, match="different plan"):
            service.admit(
                _plan(goal="Different goal"), actor="controller", reason="collision"
            )
    finally:
        store.close()


def test_failed_attestation_leaves_no_partial_package_or_tasks() -> None:
    store = SQLiteStore(":memory:")
    try:
        _register_repository(store)
        service = WorkPackageService(store, repository_verifier=_Verifier(sha="b" * 40))
        with pytest.raises(ValidationError, match="attestation"):
            service.admit(_plan(), actor="controller", reason="bad base")
        assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 0
        assert store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0
    finally:
        store.close()


def test_external_effect_declaration_cannot_create_a_lease_expiry_hazard() -> None:
    store = SQLiteStore(":memory:")
    try:
        _register_repository(store)
        plan = _plan()
        plan["nodes"][0]["effects"] = {
            "external": ["github:release"],
            "external_contract": {
                "idempotency_key": "release-wp-service",
                "exclusive": True,
            },
        }
        service = WorkPackageService(store, repository_verifier=_Verifier())
        with pytest.raises(
            ValidationError,
            match="controller-owned fenced effector",
        ):
            service.admit(plan, actor="controller", reason="unsafe external action")

        # A declared key is not target-system authority.  Fail before task or
        # lease creation so there is no partitioned worker to outlive expiry
        # and no retry that can duplicate the external action.
        assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 0
        assert store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0
        assert store.query_one("SELECT COUNT(*) AS n FROM leases")["n"] == 0
    finally:
        store.close()


def test_resolved_resource_namespace_requires_independent_attestation() -> None:
    store = SQLiteStore(":memory:")
    try:
        _register_repository(store)
        plan = _plan()
        plan["resource_namespace"] = {
            "case_sensitive": True,
            "unicode_normalization": "NFC",
            "symlink_resolution": "resolved",
        }
        with pytest.raises(ValidationError, match="resource namespace"):
            WorkPackageService(store, repository_verifier=_Verifier()).admit(
                plan, actor="controller", reason="unattested namespace"
            )
        assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 0
    finally:
        store.close()


def test_activation_releases_only_dependency_free_roots() -> None:
    store = SQLiteStore(":memory:")
    try:
        _register_repository(store)
        service = WorkPackageService(store, repository_verifier=_Verifier())
        admitted = service.admit(_plan(), actor="controller", reason="initial")

        active = service.activate(
            admitted.package.id,
            expected_plan_version=1,
            expected_epoch=1,
            actor="operator",
        )
        assert active.state == "active"
        rows = store.query_all(
            "SELECT link.node_key, link.node_state, task.metadata "
            "FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id ORDER BY link.node_key"
        )
        by_node = {row["node_key"]: row for row in rows}
        assert by_node["build"]["node_state"] == "ready"
        assert "no_dispatch" not in json_loads(by_node["build"]["metadata"], {})
        for node_key in ("assemble", "certify"):
            assert by_node[node_key]["node_state"] == "planned"
            assert json_loads(by_node[node_key]["metadata"], {})["no_dispatch"] is True

        # Exact retries are safe and do not append duplicate activation history.
        assert service.activate(
            admitted.package.id,
            expected_plan_version=1,
            expected_epoch=1,
            actor="operator",
        ).state == "active"
        assert store.query_one("SELECT COUNT(*) AS n FROM work_package_history")["n"] == 2
    finally:
        store.close()


def test_control_plane_activation_requires_a_bound_ready_worker(
    monkeypatch,
) -> None:
    store = SQLiteStore(":memory:")
    try:
        _register_repository(store)
        admitted = WorkPackageService(
            store, repository_verifier=_Verifier()
        ).admit(_plan(), actor="planner-controller", reason="approved proposal")
        cp = ControlPlane(store, secret_key="work-package-activation-test-key-0001")
        machine = cp.register_machine("activation-host")
        agent = cp.register_agent(
            machine.id,
            "activation-worker",
            capabilities=["work_package_v1"],
        )

        monkeypatch.setattr(
            "mac.worker_credentials.package_worker_readiness",
            lambda store_arg, agent_id: {
                "ready": agent_id == agent.id,
                "reason": "test-ready",
            },
        )
        downstream_blocked = cp.work_package_activation_readiness(
            admitted.package.id
        )
        assert downstream_blocked["ready"] is False
        assert downstream_blocked["downstream"]["code"] == (
            "work_package_pipeline_disabled"
        )

        monkeypatch.setattr(
            cp,
            "_work_package_downstream_activation_readiness",
            lambda _described: {"ready": True, "code": "ready", "reason": ""},
        )
        readiness = cp.work_package_activation_readiness(admitted.package.id)
        assert readiness["ready"] is True
        assert [
            item["route"] for item in readiness["requirements"]
        ].count("worker") == 1

        activated = cp.activate_work_package(
            admitted.package.id,
            expected_plan_version=1,
            expected_epoch=1,
            actor="operator",
        )
        assert activated["package"]["state"] == "active"

        monkeypatch.setattr(
            "mac.worker_credentials.package_worker_readiness",
            lambda _store, _agent_id: {
                "ready": False,
                "reason": "credential-missing",
            },
        )
        # Idempotent activation still remains fail-closed if the worker route
        # is no longer ready; activation is not a bypass around revocation.
        with pytest.raises(ValidationError, match="activation readiness failed"):
            cp.activate_work_package(
                admitted.package.id,
                expected_plan_version=1,
                expected_epoch=1,
                actor="operator",
            )
    finally:
        store.close()


def test_resolved_external_lineage_requires_a_controller_verifier() -> None:
    store = SQLiteStore(":memory:")
    try:
        _register_repository(store)
        store.execute(
            "INSERT INTO tasks ("
            "id, title, description, priority, state, required_capabilities, dependencies, "
            "metadata, attempt_count, max_attempts, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task_external",
                "external",
                "",
                0,
                "completed",
                "[]",
                "[]",
                "{}",
                1,
                3,
                "created",
                "updated",
            ),
        )
        plan = _plan()
        plan["nodes"][0]["external_dependencies"] = [
            {
                "task_id": "task_external",
                "accepted_evidence_digest": "sha256:" + "1" * 64,
                "output_digest": "sha256:" + "2" * 64,
                "contract_digest": "sha256:" + "3" * 64,
            }
        ]
        with pytest.raises(ValidationError, match="controller verifier"):
            WorkPackageService(store, repository_verifier=_Verifier()).admit(
                plan, actor="controller", reason="untrusted lineage"
            )
        assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 0
    finally:
        store.close()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_git_repository_verifier_uses_the_advertised_canonical_ref(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    remote.mkdir()
    work.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    _git(work, "init", "--initial-branch=main")
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "base")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "HEAD:refs/heads/main")

    verifier = GitRepositoryBaseVerifier(timeout_seconds=5)
    repository = {
        "id": "repo",
        "source": str(remote),
        "path": str(work),
    }
    attested = verifier.verify(
        repository,
        planning_base_ref="refs/heads/main",
        planning_base_sha=sha,
    )
    assert attested.canonical_ref_sha == sha
    assert attested.resource_namespace == {
        "status": "resolved",
        "case_sensitive": False,
        "unicode_normalization": "NFC",
        "symlink_resolution": "resolved",
        "conflict_policy": "exact",
        "attestor": "git-tree-namespace-v1",
        "planning_base_sha": sha,
    }

    contract_attested = verifier.verify(
        {
            "id": "repo_contract",
            "source": "git@example.invalid:obsolete/repository.git",
            "path": str(work),
            "metadata": json.dumps(
                {
                    "repository_contract": {
                        "canonical_remote_url": str(remote),
                    }
                }
            ),
        },
        planning_base_ref="refs/heads/main",
        planning_base_sha=sha,
    )
    assert contract_attested.canonical_ref_sha == sha

    with pytest.raises(ValidationError, match="stale"):
        verifier.verify(
            repository,
            planning_base_ref="refs/heads/main",
            planning_base_sha="f" * 40,
        )
