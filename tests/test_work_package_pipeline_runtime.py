from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from mac.landing_service import LandingServiceConfig, RepositoryEndpoint
from mac.models import ValidationError, json_dumps
from mac.services import ControlPlane
from mac.store import Store
from mac.test_support import ephemeral_store
from mac.work_package_certification_service import CERTIFICATION_CONTRACT_SCHEMA
from mac.work_package_pipeline import PipelineSnapshot
from mac.work_package_pipeline_runtime import (
    ExactCandidateBundleProvider,
    RepositoryPipelineReleaseGateResolver,
    WorkPackagePipelineRuntimeConfig,
    build_work_package_pipeline_runtime,
    controller_git_credential_environment,
)


CREATED_AT = "2026-07-17T12:00:00.000000+00:00"
TARGET_REF = "refs/heads/main"
CANDIDATE_REF = "refs/mac/candidates/batch/1"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str, str]:
    remote = tmp_path / "canonical.git"
    work = tmp_path / "work"
    remote.mkdir()
    work.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    _git(work, "init", "--initial-branch=main")
    _git(work, "config", "user.name", "Pipeline Runtime Test")
    _git(work, "config", "user.email", "pipeline@example.invalid")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "base")
    base_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "HEAD:%s" % TARGET_REF)
    (work / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _git(work, "add", "candidate.txt")
    _git(work, "commit", "-m", "candidate")
    candidate_sha = _git(work, "rev-parse", "HEAD")
    tree_sha = _git(work, "rev-parse", "HEAD^{tree}")
    _git(work, "push", "origin", "HEAD:%s" % CANDIDATE_REF)
    return remote, base_sha, candidate_sha, tree_sha


def _seed(
    store: Store,
    *,
    remote: Path,
    base_sha: str,
    candidate_sha: str,
    tree_sha: str,
    certification_contract: bool = True,
    repository_source: str | None = None,
) -> None:
    repository_contract: dict = {
        "canonical_remote_url": str(remote),
        "landing_certification_policy_id": "trusted-policy",
    }
    if certification_contract:
        repository_contract["work_package_certification"] = {
            "schema": CERTIFICATION_CONTRACT_SCHEMA
        }
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "repo_1",
            "runtime-test",
            str(remote),
            repository_source if repository_source is not None else str(remote),
            "mac",
            "[]",
            1,
            60,
            json_dumps({"repository_contract": repository_contract}),
            CREATED_AT,
            CREATED_AT,
        ),
    )
    store.execute(
        "INSERT INTO work_packages ("
        "id, project, repository_id, goal, state, current_plan_version, "
        "current_epoch, metadata, created_by, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "wp_1",
            "mac",
            "repo_1",
            "certify exact candidate",
            "draft",
            0,
            0,
            "{}",
            "planner",
            CREATED_AT,
            CREATED_AT,
        ),
    )
    store.execute(
        "INSERT INTO work_package_plan_versions ("
        "package_id, version, definition, plan_digest, reason, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "wp_1",
            1,
            "{}",
            "sha256:" + "1" * 64,
            "test",
            "planner",
            CREATED_AT,
        ),
    )
    store.execute(
        "INSERT INTO work_package_epochs ("
        "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, "
        "status, reason, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "wp_1",
            1,
            1,
            TARGET_REF,
            base_sha,
            "active",
            "test",
            "planner",
            CREATED_AT,
        ),
    )
    store.execute(
        "UPDATE work_packages SET state = 'admitted', current_plan_version = 1, "
        "current_epoch = 1 WHERE id = 'wp_1'"
    )
    store.execute("UPDATE work_packages SET state = 'active' WHERE id = 'wp_1'")
    store.execute(
        "INSERT INTO tasks ("
        "id, title, description, priority, state, required_capabilities, dependencies, "
        "metadata, attempt_count, max_attempts, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "task_integration",
            "integration",
            "",
            0,
            "waiting",
            "[]",
            "[]",
            json_dumps({"no_dispatch": True}),
            0,
            1,
            CREATED_AT,
            CREATED_AT,
        ),
    )
    store.execute(
        "INSERT INTO work_package_integration_batches ("
        "id, package_id, plan_version, epoch, repository_id, target_ref, "
        "assembly_base_sha, landing_base_sha, input_digest, state, "
        "integration_task_id, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "batch_1",
            "wp_1",
            1,
            1,
            "repo_1",
            TARGET_REF,
            base_sha,
            base_sha,
            "sha256:" + "2" * 64,
            "queued",
            "task_integration",
            "{}",
            CREATED_AT,
            CREATED_AT,
        ),
    )
    store.execute(
        "UPDATE work_package_integration_batches SET state = 'assembling', "
        "lease_owner = 'controller', lease_expires_at = '2099-01-01T00:00:00+00:00', "
        "lease_fence = 1 WHERE id = 'batch_1'"
    )
    store.execute(
        "UPDATE work_package_integration_batches SET candidate_sha = ?, "
        "candidate_tree_digest = ?, candidate_ref = ?, candidate_fence = 1 "
        "WHERE id = 'batch_1'",
        (candidate_sha, "git-tree:%s" % tree_sha, CANDIDATE_REF),
    )
    store.execute(
        "UPDATE work_package_integration_batches SET state = 'verifying', "
        "lease_owner = NULL, lease_expires_at = NULL WHERE id = 'batch_1'"
    )


def _snapshot() -> PipelineSnapshot:
    return PipelineSnapshot(
        key="wp_1:1:1:integrate",
        package_id="wp_1",
        plan_version=1,
        epoch=1,
        integration_node_key="integrate",
        integration_task_id="task_integration",
        integration_node_state="integrated",
        certification_node_key="certify",
        certification_task_id="task_certification",
        certification_node_state="ready",
        batch_id="batch_1",
        batch_state="verifying",
    )


def test_release_gate_requires_contract_landing_and_secret_free_endpoint(
    tmp_path: Path,
) -> None:
    remote, base_sha, candidate_sha, tree_sha = _repository(tmp_path)
    store = ephemeral_store()
    try:
        _seed(
            store,
            remote=remote,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            tree_sha=tree_sha,
            repository_source="git@example.invalid:obsolete/repository.git",
        )
        validated: list[str] = []
        resolver = RepositoryPipelineReleaseGateResolver(
            store,
            validate_certification_contract=lambda repository_id: validated.append(
                repository_id
            ),
            landing_config=LandingServiceConfig(enabled=True),
        )
        gate = resolver.resolve(_snapshot())
        assert gate.ready is True
        assert validated == ["repo_1"]
        assert isinstance(gate.endpoint, RepositoryEndpoint)
        assert gate.endpoint.repository_id == "repo_1"
        assert gate.endpoint.remote_url == str(remote)
        assert "token" not in repr(gate.endpoint).lower()

        disabled = RepositoryPipelineReleaseGateResolver(
            store,
            validate_certification_contract=lambda _repository_id: None,
            landing_config=LandingServiceConfig(enabled=False),
        ).resolve(_snapshot())
        assert disabled.ready is False
        assert disabled.code == "landing_disabled"

        invalid = RepositoryPipelineReleaseGateResolver(
            store,
            validate_certification_contract=lambda _repository_id: (
                _ for _ in ()
            ).throw(ValidationError("bad token=must-not-leak")),
            landing_config=LandingServiceConfig(enabled=True),
        ).resolve(_snapshot())
        assert invalid.ready is False
        assert invalid.code == "certification_contract_unavailable"
        assert "must-not-leak" not in invalid.reason
    finally:
        store.close()


def test_exact_bundle_is_rebuildable_and_contains_only_exact_candidate(
    tmp_path: Path,
) -> None:
    remote, base_sha, candidate_sha, tree_sha = _repository(tmp_path)
    store = ephemeral_store()
    try:
        _seed(
            store,
            remote=remote,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            tree_sha=tree_sha,
        )
        cache = tmp_path / "bundles"
        provider = ExactCandidateBundleProvider(store, cache_dir=cache)
        first = provider.ensure_bundle(_snapshot())
        first_digest = hashlib.sha256(first.read_bytes()).hexdigest()
        assert first.read_bytes().startswith(
            (b"# v2 git bundle\n", b"# v3 git bundle\n")
        )
        assert candidate_sha in _git(tmp_path, "bundle", "list-heads", str(first))
        assert stat_mode(first) == 0o400

        second = provider.ensure_bundle(_snapshot())
        assert second == first
        assert hashlib.sha256(second.read_bytes()).hexdigest() == first_digest

        first.unlink()
        rebuilt = provider.ensure_bundle(_snapshot())
        assert rebuilt == first
        assert hashlib.sha256(rebuilt.read_bytes()).hexdigest() == first_digest
    finally:
        store.close()


def test_exact_bundle_fetches_from_contract_canonical_remote(tmp_path: Path) -> None:
    remote, base_sha, candidate_sha, tree_sha = _repository(tmp_path)
    store = ephemeral_store()
    try:
        _seed(
            store,
            remote=remote,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            tree_sha=tree_sha,
            repository_source=str(tmp_path / "obsolete-source.git"),
        )
        bundle = ExactCandidateBundleProvider(
            store, cache_dir=tmp_path / "bundles"
        ).ensure_bundle(_snapshot())
        assert candidate_sha in _git(tmp_path, "bundle", "list-heads", str(bundle))
    finally:
        store.close()


def test_bundle_rejects_moved_candidate_ref(tmp_path: Path) -> None:
    remote, base_sha, candidate_sha, tree_sha = _repository(tmp_path)
    store = ephemeral_store()
    try:
        _seed(
            store,
            remote=remote,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            tree_sha=tree_sha,
        )
        work = tmp_path / "attacker"
        _git(tmp_path, "clone", str(remote), str(work))
        _git(work, "config", "user.name", "Attacker")
        _git(work, "config", "user.email", "attacker@example.invalid")
        (work / "moved.txt").write_text("moved\n", encoding="utf-8")
        _git(work, "add", "moved.txt")
        _git(work, "commit", "-m", "move protected ref")
        _git(work, "push", "--force", "origin", "HEAD:%s" % CANDIDATE_REF)

        with pytest.raises(ValidationError, match="no longer names"):
            ExactCandidateBundleProvider(
                store, cache_dir=tmp_path / "bundles"
            ).ensure_bundle(_snapshot())
    finally:
        store.close()


def test_bundle_cache_prunes_only_finalized_unreferenced_known_entry(
    tmp_path: Path,
) -> None:
    row = {
        "id": "batch-finalized",
        "repository_id": "repo",
        "candidate_sha": "a" * 40,
        "candidate_tree_digest": "git-tree:" + "b" * 40,
        "finalized_at": "2000-01-01T00:00:00+00:00",
    }

    class _Store:
        active = True

        def query_all(self, sql, params=()):
            assert "publication_finalizations" in sql
            return [row]

        def query_one(self, sql, params=()):
            assert "certification_jobs" in sql
            return {"active": 1} if self.active else None

    store = _Store()
    cache = tmp_path / "bundles"
    provider = ExactCandidateBundleProvider(
        store,
        cache_dir=cache,
        retention_seconds=0,
    )
    provider._ensure_cache_dir()
    known = provider._cache_path(
        repository_id="repo",
        batch_id="batch-finalized",
        candidate_sha="a" * 40,
        candidate_tree_digest="git-tree:" + "b" * 40,
    )
    known.write_bytes(b"known finalized bundle")
    unknown = cache / ("candidate-%s.bundle" % ("f" * 64))
    unknown.write_bytes(b"unknown bundle")
    os.utime(known, (1, 1))
    os.utime(unknown, (1, 1))

    assert provider.prune() == 0
    assert known.exists()
    store.active = False
    assert provider.prune() == 1
    assert not known.exists()
    assert unknown.exists()


def test_controller_git_environment_is_allowlisted_and_never_persisted() -> None:
    env = controller_git_credential_environment(
        "read",
        {"source": "https://github.com/jordanhubbard/mac.git"},
        environ={
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "GH_TOKEN": "secret",
            "GITHUB_TOKEN": "wrong-token-source",
            "GIT_ASKPASS": "/tmp/ambient-askpass",
            "GIT_CONFIG_COUNT": "1",
            "UNRELATED": "not-forwarded",
        },
    )
    assert env["GH_TOKEN"] == "secret"
    assert Path(env["GIT_ASKPASS"]).name == "mac-git-askpass"
    assert env["GIT_ASKPASS"] != "/tmp/ambient-askpass"
    assert "SSH_AUTH_SOCK" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GIT_CONFIG_COUNT" not in env
    assert "UNRELATED" not in env

    ssh = controller_git_credential_environment(
        "write",
        RepositoryEndpoint("repo", "git@github.com:jordanhubbard/mac.git"),
        environ={
            "GH_TOKEN": "must-not-enter-ssh",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            "GIT_ASKPASS": "/tmp/ambient-askpass",
        },
    )
    assert ssh == {"SSH_AUTH_SOCK": "/tmp/agent.sock"}


def test_controller_git_environment_rejects_unsupported_or_missing_https_auth() -> None:
    with pytest.raises(ValidationError, match="only github.com"):
        controller_git_credential_environment(
            "write",
            {"source": "https://gitlab.example.invalid/owner/repo.git"},
            environ={"GH_TOKEN": "secret"},
        )
    with pytest.raises(ValidationError, match="credential is unavailable"):
        controller_git_credential_environment(
            "write",
            {"source": "https://github.com/jordanhubbard/mac.git"},
            environ={"GITHUB_TOKEN": "not-an-allowed-fallback"},
        )


def test_runtime_configuration_is_default_off_and_fails_closed_when_incomplete(
    tmp_path: Path,
) -> None:
    default = WorkPackagePipelineRuntimeConfig.from_env({})
    assert default.enabled is False
    assert default.configuration_error == ""

    incomplete = WorkPackagePipelineRuntimeConfig.from_env(
        {"MAC_WORK_PACKAGE_PIPELINE_ENABLED": "true"}
    )
    assert incomplete.enabled is False
    assert incomplete.pipeline.enabled is False
    assert "BUNDLE_DIR" in incomplete.configuration_error
    assert "LANDING_ENABLED" in incomplete.configuration_error

    configured = WorkPackagePipelineRuntimeConfig.from_env(
        {
            "MAC_WORK_PACKAGE_PIPELINE_ENABLED": "true",
            "MAC_WORK_PACKAGE_LANDING_ENABLED": "true",
            "MAC_WORK_PACKAGE_BUNDLE_DIR": str(tmp_path / "bundles"),
            "MAC_WORK_PACKAGE_PIPELINE_INTERVAL_SECONDS": "0.5",
        }
    )
    assert configured.enabled is True
    assert configured.configuration_error == ""
    assert configured.pipeline.interval_seconds == 0.5
    status = configured.to_dict()
    assert status["bundle_dir_configured"] is True
    assert str(tmp_path) not in str(status)


def test_runtime_configuration_rejects_malformed_values() -> None:
    config = WorkPackagePipelineRuntimeConfig.from_env(
        {
            "MAC_WORK_PACKAGE_PIPELINE_ENABLED": "maybe",
            "MAC_WORK_PACKAGE_CANDIDATE_NAMESPACE": "../../unsafe",
        }
    )
    assert config.enabled is False
    assert "must be a boolean" in config.configuration_error
    assert "landing configuration is invalid" in config.configuration_error


def test_live_runtime_reuses_control_plane_station_services(monkeypatch) -> None:
    monkeypatch.delenv("MAC_WORK_PACKAGE_PIPELINE_ENABLED", raising=False)
    monkeypatch.delenv("MAC_WORK_PACKAGE_LANDING_ENABLED", raising=False)
    monkeypatch.setenv("GH_TOKEN", "private-repository-token")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/controller-agent.sock")
    monkeypatch.setenv("UNRELATED_HUB_SECRET", "must-not-forward")
    cp = ControlPlane.in_memory()
    runtime = build_work_package_pipeline_runtime(cp)
    peer_runtime = build_work_package_pipeline_runtime(cp)

    assert runtime.certification is cp.work_package_certifications
    assert runtime.landing is cp.work_package_landing
    assert runtime.finalization is cp.work_package_publication_finalizer
    assert runtime.controller.owner != peer_runtime.controller.owner
    landing_credentials = runtime.landing.credential_environment(
        "write", RepositoryEndpoint("repo", "git@example.invalid:org/repo.git")
    )
    assert landing_credentials == {"SSH_AUTH_SOCK": "/tmp/controller-agent.sock"}
    assert "UNRELATED_HUB_SECRET" not in landing_credentials
    https_credentials = runtime.landing.credential_environment(
        "write", RepositoryEndpoint("repo", "https://github.com/org/repo.git")
    )
    assert https_credentials["GH_TOKEN"] == "private-repository-token"
    assert Path(https_credentials["GIT_ASKPASS"]).name == "mac-git-askpass"
    assert "SSH_AUTH_SOCK" not in https_credentials


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
