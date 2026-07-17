from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mac.models import ValidationError, json_loads
from mac.store import SQLiteStore
from mac.work_package_models import compile_work_package_plan
from mac.work_package_service import RepositoryBaseAttestation, WorkPackageService
from mac.work_plan_admission import (
    GitCanonicalBaseResolver,
    ManagedWorkPlanBridge,
    CanonicalRepositoryBase,
    managed_plan_from_dashboard_accept,
)


SHA = "a" * 40


class _BaseResolver:
    def __init__(self, sha: str = SHA, *, resource_namespace=None) -> None:
        self.sha = sha
        self.resource_namespace = dict(resource_namespace or {})
        self.calls = []

    def resolve(self, repository, *, requested_ref=None):
        self.calls.append((dict(repository), requested_ref))
        return CanonicalRepositoryBase(
            repository_id=repository["id"],
            planning_base_ref=requested_ref or "refs/heads/main",
            planning_base_sha=self.sha,
            resource_namespace=self.resource_namespace,
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


def _register_repository(
    store: SQLiteStore,
    *,
    repository_id: str = "projectrepo_mac",
    name: str = "mac",
    project: str = "mac",
    source: str = "git@example.invalid:org/mac.git",
) -> None:
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            repository_id,
            name,
            "/missing/controller/checkout",
            source,
            project,
            "[]",
            1,
            60,
            "{}",
            "created",
            "updated",
        ),
    )


def _proposal() -> dict:
    return {
        "nodes": [
            {
                "node_id": "backend",
                "title": "Implement backend",
                "description": "Change the backend slice.",
                "kind": "mutation",
                "effects": {"writes": ["src/backend"]},
                "expected_outputs": ["backend-candidate"],
                "estimates": {"confidence": "high"},
                "verification": {
                    "profile": "repository-default",
                    "command": "pytest tests/backend",
                },
            },
            {
                "node_id": "frontend",
                "title": "Implement frontend",
                "description": "Change the frontend slice.",
                "kind": "mutation",
                "effects": {"writes": ["src/frontend"]},
                "expected_outputs": ["frontend-candidate"],
                "estimates": {"confidence": "high"},
                "verification": {
                    "profile": "repository-default",
                    "command": "npm test",
                },
            },
            {
                "node_id": "assemble",
                "title": "Assemble exact candidate",
                "kind": "integration",
                "depends_on": ["backend", "frontend"],
                "effects": {"reads": ["src"]},
                "expected_outputs": ["assembled-tree"],
                "verification": {"profile": "integration-default"},
            },
            {
                "node_id": "certify",
                "title": "Certify assembled candidate",
                "kind": "certification",
                "depends_on": ["assemble"],
                "effects": {"reads": ["src"]},
                "expected_outputs": ["certificate"],
                "verification": {"profile": "certification-default"},
            },
        ],
        "max_in_flight": 2,
        "mutation_wip": {"max_tokens": 2},
    }


@pytest.fixture
def managed_bridge():
    store = SQLiteStore(":memory:")
    _register_repository(store)
    resolver = _BaseResolver()
    packages = WorkPackageService(store, repository_verifier=_AdmissionAttestor())
    bridge = ManagedWorkPlanBridge(
        store,
        packages,
        base_resolver=resolver,
    )
    try:
        yield store, resolver, packages, bridge
    finally:
        store.close()


def test_preview_locks_repository_and_base_without_creating_work(managed_bridge) -> None:
    store, resolver, _packages, bridge = managed_bridge

    result = bridge.preview(
        _proposal(),
        request={
            "goal": "Ship coordinated UI and API changes",
            "project": "mac",
            "package_id": "wp_dashboard",
        },
        source="model",
    ).to_dict()

    assert result["schema"] == "mac.dashboard.managed_work_plan.v1"
    assert result["mode"] == "managed"
    assert result["package_id"] == "wp_dashboard"
    assert result["repository"] == {
        "id": "projectrepo_mac",
        "name": "mac",
        "project": "mac",
    }
    assert result["planning_base_ref"] == "refs/heads/main"
    assert result["planning_base_sha"] == SHA
    assert result["plan_digest"].startswith("sha256:")
    assert result["topological_order"] == [
        "backend",
        "frontend",
        "assemble",
        "certify",
    ]
    assert result["activation"]["automatic"] is False
    assert resolver.calls[0][1] is None
    assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 0
    assert store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0
    encoded = json.dumps(result, sort_keys=True)
    assert "git@example.invalid" not in encoded
    assert "/missing/controller/checkout" not in encoded


def test_preview_uses_controller_attested_paths_for_real_parallel_mutations() -> None:
    store = SQLiteStore(":memory:")
    try:
        _register_repository(store)
        namespace = {
            "status": "resolved",
            "case_sensitive": False,
            "unicode_normalization": "NFC",
            "symlink_resolution": "resolved",
        }
        bridge = ManagedWorkPlanBridge(
            store,
            WorkPackageService(store, repository_verifier=_AdmissionAttestor()),
            base_resolver=_BaseResolver(resource_namespace=namespace),
        )
        preview = bridge.preview(
            _proposal(),
            request={
                "goal": "Ship coordinated UI and API changes",
                "project": "mac",
                "package_id": "wp_parallel",
            },
        ).to_dict()

        assert preview["resource_namespace"] == {
            "case_sensitive": False,
            "unicode_normalization": "NFC",
            "symlink_resolution": "resolved",
        }
        compiled = compile_work_package_plan(preview["plan"])
        mutations = [
            node for node in compiled.task_specs if node.node_type == "mutation"
        ]
        assert mutations
        assert all("repo:*" not in node.effects.exclusive for node in mutations)
    finally:
        store.close()


def test_accept_recompiles_and_atomically_admits_held_then_activation_is_explicit(
    managed_bridge,
) -> None:
    store, resolver, packages, bridge = managed_bridge
    preview = bridge.preview(
        _proposal(),
        request={
            "goal": "Ship coordinated UI and API changes",
            "project": "mac",
            "package_id": "wp_dashboard",
        },
    )
    plan = preview.to_dict()["plan"]
    plan["nodes"][0]["title"] = "Implement edited backend"

    accepted = bridge.accept(
        plan,
        actor="operator",
        reason="operator accepted edited managed plan",
    ).to_dict()

    assert accepted["schema"] == "mac.dashboard.managed_work_plan_accept.v1"
    assert accepted["package"]["state"] == "admitted"
    assert accepted["held"] is True
    assert accepted["activation"] == {
        "required": True,
        "automatic": False,
        "expected_plan_version": 1,
        "expected_epoch": 1,
        "endpoint": "/work-packages/wp_dashboard/activate",
    }
    assert [call[1] for call in resolver.calls] == [None, "refs/heads/main"]
    rows = store.query_all("SELECT title, metadata FROM tasks ORDER BY title")
    assert any(row["title"] == "Implement edited backend" for row in rows)
    assert all(json_loads(row["metadata"], {})["no_dispatch"] is True for row in rows)

    active = packages.activate(
        "wp_dashboard",
        expected_plan_version=1,
        expected_epoch=1,
        actor="operator",
    )
    assert active.state == "active"
    backend = store.query_one("SELECT metadata FROM tasks WHERE title = ?", ("Implement edited backend",))
    assert "no_dispatch" not in json_loads(backend["metadata"], {})


def test_flat_dashboard_preview_projects_back_to_closed_compiler_plan(
    managed_bridge,
) -> None:
    _store, _resolver, _packages, bridge = managed_bridge
    preview = bridge.preview(
        _proposal(),
        request={
            "goal": "Ship through the flat dashboard form",
            "project": "mac",
            "package_id": "wp_flat_form",
        },
    ).to_dict()
    preview.pop("plan")

    plan = managed_plan_from_dashboard_accept(preview)

    assert plan["schema"] == "mac.work_package.plan.v1"
    assert plan["package_id"] == "wp_flat_form"
    assert plan["repository_id"] == "projectrepo_mac"
    assert "activation" not in plan
    assert "repository" not in plan
    accepted = bridge.accept(
        plan,
        actor="operator",
        reason="accepted flat dashboard form",
    )
    assert accepted.admission.package.state == "admitted"


def test_accept_fails_closed_when_canonical_base_moved(managed_bridge) -> None:
    store, resolver, _packages, bridge = managed_bridge
    plan = bridge.preview(
        _proposal(),
        request={
            "goal": "Ship safely",
            "project": "mac",
            "package_id": "wp_stale",
        },
    ).plan
    resolver.sha = "b" * 40

    with pytest.raises(ValidationError, match="canonical planning ref moved"):
        bridge.accept(plan, actor="operator", reason="stale acceptance")

    assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 0
    assert store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda proposal: proposal["nodes"].pop(),
            "exactly one integration station and one certification station",
        ),
        (
            lambda proposal: proposal["nodes"][3].update({"depends_on": ["backend"]}),
            "certification station must depend directly",
        ),
        (
            lambda proposal: proposal["nodes"][2].update({"depends_on": ["backend"]}),
            "integration fan-in node",
        ),
        (
            lambda proposal: proposal["nodes"][0].pop("effects"),
            "must explicitly declare effects",
        ),
    ],
)
def test_managed_topology_and_contracts_fail_closed(
    managed_bridge,
    mutate,
    message: str,
) -> None:
    _store, _resolver, _packages, bridge = managed_bridge
    proposal = _proposal()
    mutate(proposal)
    with pytest.raises(ValidationError, match=message):
        bridge.preview(
            proposal,
            request={"goal": "Ship safely", "project": "mac"},
        )


def test_model_cannot_choose_locked_identity_or_emit_secret_fields(managed_bridge) -> None:
    _store, _resolver, _packages, bridge = managed_bridge
    locked = _proposal()
    locked["repository_id"] = "model-selected"
    with pytest.raises(ValidationError, match="controller-owned fields"):
        bridge.preview(locked, request={"goal": "Ship", "project": "mac"})

    secret = _proposal()
    secret["metadata"] = {"api_token": "should-never-be-persisted"}
    with pytest.raises(ValidationError, match="secret-like field"):
        bridge.preview(secret, request={"goal": "Ship", "project": "mac"})


def test_repository_selection_is_fail_closed_when_project_is_ambiguous(
    managed_bridge,
) -> None:
    store, _resolver, _packages, bridge = managed_bridge
    _register_repository(
        store,
        repository_id="projectrepo_mac_two",
        name="mac-two",
        project="mac",
        source="git@example.invalid:org/mac-two.git",
    )
    with pytest.raises(ValidationError, match="ambiguous"):
        bridge.preview(
            _proposal(),
            request={"goal": "Ship", "project": "mac"},
        )


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def test_git_base_resolver_uses_remote_head_and_exact_requested_ref(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "planner@example.invalid")
    _git(work, "config", "user.name", "Planner Test")
    (work / "README.md").write_text("managed planning\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "HEAD:refs/heads/trunk")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/trunk")
    repository = {"id": "repo_local", "source": str(remote), "path": str(work)}
    resolver = GitCanonicalBaseResolver()

    default = resolver.resolve(repository)
    exact = resolver.resolve(repository, requested_ref="refs/heads/trunk")

    assert default == CanonicalRepositoryBase(
        repository_id="repo_local",
        planning_base_ref="refs/heads/trunk",
        planning_base_sha=sha,
        resource_namespace={
            "status": "resolved",
            "case_sensitive": False,
            "unicode_normalization": "NFC",
            "symlink_resolution": "resolved",
            "conflict_policy": "exact",
            "attestor": "git-tree-namespace-v1",
            "planning_base_sha": sha,
        },
    )
    assert exact == default
    contract_selected = resolver.resolve(
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
        requested_ref="refs/heads/trunk",
    )
    assert contract_selected.planning_base_sha == sha
    with pytest.raises(ValidationError, match="resolution failed"):
        resolver.resolve(repository, requested_ref="refs/heads/missing")
    with pytest.raises(ValidationError, match="valid canonical repository"):
        resolver.resolve(
            {
                "id": "repo_credential",
                "source": "https://raw-token@example.invalid/org/repo.git",
            }
        )


def test_real_git_preview_and_admission_independently_attest_path_parallelism(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "planner@example.invalid")
    _git(work, "config", "user.name", "Planner Test")
    (work / "README.md").write_text("managed planning\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "HEAD:refs/heads/main")

    store = SQLiteStore(":memory:")
    try:
        _register_repository(store, source=str(remote))
        store.execute(
            "UPDATE project_repositories SET path = ? WHERE id = ?",
            (str(work), "projectrepo_mac"),
        )
        packages = WorkPackageService(store)
        bridge = ManagedWorkPlanBridge(store, packages)

        preview = bridge.preview(
            _proposal(),
            request={
                "goal": "Ship coordinated UI and API changes",
                "project": "mac",
                "planning_base_ref": "refs/heads/main",
                "package_id": "wp_real_namespace",
            },
        ).to_dict()
        accepted = bridge.accept(
            preview["plan"],
            actor="operator",
            reason="accept independently attested path namespace",
        ).to_dict()

        assert preview["resource_namespace"]["symlink_resolution"] == "resolved"
        assert accepted["package"]["state"] == "admitted"
        stored = store.query_one(
            "SELECT metadata FROM work_packages WHERE id = ?",
            ("wp_real_namespace",),
        )
        attestation = json_loads(stored["metadata"], {})["base_attestation"]
        assert attestation["resource_namespace"]["status"] == "resolved"
        assert attestation["resource_namespace"]["planning_base_sha"] == preview[
            "planning_base_sha"
        ]
    finally:
        store.close()
