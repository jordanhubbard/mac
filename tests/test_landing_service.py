from __future__ import annotations

import subprocess
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mac.landing_service import (
    AssemblyInput,
    CERTIFICATION_ISOLATION_SCHEMA,
    LandingBusyError,
    LandingDisabledError,
    LandingError,
    LandingLeaseLostError,
    LandingService,
    LandingServiceConfig,
    RepositoryEndpoint,
    SubprocessGitRunner,
    compute_landing_input_digest,
)
from mac.store import SQLiteStore
from mac.models import ValidationError, json_dumps, json_loads


TARGET_REF = "refs/heads/main"
CANDIDATE_REF = "refs/mac/candidates/batch-1/1"
ATTEMPT_REF = "refs/mac/attempts/wp-1/e1/worker-input/a1-lease-worker-input"
CREATED_AT = "2026-07-17T12:00:00.000000+00:00"


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


def _repository(tmp_path: Path) -> tuple[Path, Path, str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    remote = tmp_path / "canonical.git"
    work = tmp_path / "work"
    remote.mkdir()
    work.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    _git(work, "init", "--initial-branch=main")
    _git(work, "config", "user.name", "Landing Test")
    _git(work, "config", "user.email", "landing-test@example.invalid")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "base")
    base_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "HEAD:%s" % TARGET_REF)

    _git(work, "checkout", "-b", "candidate")
    (work / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _git(work, "add", "candidate.txt")
    _git(work, "commit", "-m", "candidate")
    candidate_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "HEAD:%s" % CANDIDATE_REF)
    _git(work, "push", "origin", "HEAD:%s" % ATTEMPT_REF)
    return remote, work, base_sha, candidate_sha


def _task(store: SQLiteStore, task_id: str) -> None:
    store.execute(
        "INSERT INTO tasks ("
        "id, title, description, priority, state, required_capabilities, dependencies, "
        "metadata, attempt_count, max_attempts, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            task_id,
            "",
            0,
            "completed",
            "[]",
            "[]",
            "{}",
            1,
            3,
            CREATED_AT,
            CREATED_AT,
        ),
    )


def _evidence(store: SQLiteStore, evidence_id: str, task_id: str) -> None:
    store.execute(
        "INSERT INTO evidence ("
        "id, task_id, kind, uri, summary, metadata, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id,
            task_id,
            "test",
            "urn:test:%s" % evidence_id,
            "passed",
            "{}",
            "certifier",
            CREATED_AT,
        ),
    )


def _seed_certified_batch(
    store: SQLiteStore,
    *,
    remote: Path,
    base_sha: str,
    candidate_sha: str,
    certification_verification: str | None = None,
) -> None:
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "repo_1",
            "landing-test",
            str(remote),
            "git@example.invalid:obsolete/repository.git",
            "mac",
            "[]",
            1,
            60,
            json_dumps(
                {
                    "repository_contract": {
                        "canonical_remote_url": str(remote),
                        "landing_certification_policy_id": (
                            "trusted-repository-default"
                        ),
                    }
                }
            ),
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
            "land exact candidate",
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
            json_dumps(
                {
                    "nodes": [
                        {
                            "node_key": "worker_input",
                            "kind": "mutation",
                            "depends_on": [],
                        },
                        {
                            "node_key": "assemble",
                            "kind": "integration",
                            "depends_on": ["worker_input"],
                        },
                        {
                            "node_key": "certify",
                            "kind": "certification",
                            "depends_on": ["assemble"],
                        },
                    ]
                }
            ),
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

    _task(store, "task_integration")
    _task(store, "task_review")
    _evidence(store, "evidence_tests", "task_integration")
    _evidence(store, "evidence_review", "task_review")
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
        "lease_owner = 'seed', lease_expires_at = '2099-01-01T00:00:00+00:00', "
        "lease_fence = 1 WHERE id = 'batch_1'"
    )
    store.execute(
        "UPDATE work_package_integration_batches SET candidate_sha = ?, "
        "candidate_tree_digest = ?, candidate_ref = ?, candidate_fence = 1 "
        "WHERE id = 'batch_1'",
        (candidate_sha, "git-tree:" + "3" * 40, CANDIDATE_REF),
    )
    store.execute(
        "UPDATE work_package_integration_batches SET state = 'verifying' "
        "WHERE id = 'batch_1'"
    )
    store.execute(
        "INSERT INTO work_package_certifications ("
        "id, batch_id, package_id, plan_version, epoch, candidate_sha, "
        "assembly_base_sha, landing_base_sha, target_ref, status, "
        "verification_digest, verification, certification_task_id, "
        "tests_evidence_id, review_task_id, review_evidence_id, certified_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "cert_1",
            "batch_1",
            "wp_1",
            1,
            1,
            candidate_sha,
            base_sha,
            base_sha,
            TARGET_REF,
            "passed",
            "sha256:" + "4" * 64,
            certification_verification
            or json_dumps(
                {
                    "isolation": {
                        "schema": CERTIFICATION_ISOLATION_SCHEMA,
                        "network": "disabled",
                        "landing_credentials": "absent",
                        "planner_commands": "rejected",
                        "policy_source": "trusted_controller",
                        "policy_id": "trusted-repository-default",
                    }
                }
            ),
            "task_integration",
            "evidence_tests",
            "task_review",
            "evidence_review",
            "isolated-certifier",
            CREATED_AT,
        ),
    )
    store.execute(
        "UPDATE work_package_integration_batches SET state = 'certified' "
        "WHERE id = 'batch_1'"
    )
    store.execute(
        "UPDATE work_package_integration_batches SET lease_owner = NULL, "
        "lease_expires_at = NULL WHERE id = 'batch_1'"
    )


def test_landing_rejects_legacy_composed_mutation_topology(tmp_path: Path) -> None:
    remote, _work, base_sha, candidate_sha = _repository(tmp_path)
    store = SQLiteStore(":memory:")
    try:
        _seed_certified_batch(
            store,
            remote=remote,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
        )
        row = store.query_one(
            "SELECT definition FROM work_package_plan_versions "
            "WHERE package_id = 'wp_1' AND version = 1"
        )
        definition = json_loads(row["definition"], {})
        definition["nodes"].insert(
            1,
            {
                "node_key": "followup",
                "kind": "mutation",
                "depends_on": ["worker_input"],
            },
        )
        assemble = next(
            node for node in definition["nodes"] if node["node_key"] == "assemble"
        )
        assemble["depends_on"] = ["followup"]
        store.execute("DROP TRIGGER trg_work_package_plan_versions_immutable")
        store.execute(
            "UPDATE work_package_plan_versions SET definition = ? "
            "WHERE package_id = 'wp_1' AND version = 1",
            (json_dumps(definition),),
        )

        with pytest.raises(ValidationError, match="flat mutation wave"):
            _service(store).land(
                "batch_1", RepositoryEndpoint("repo_1", str(remote))
            )

        assert _git(remote, "rev-parse", TARGET_REF) == base_sha
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_intents"
        )["n"] == 0
    finally:
        store.close()


def _seed_assembly_batch(
    store: SQLiteStore,
    *,
    base_sha: str,
    reviewed_sha: str,
    reviewed_ref: str,
) -> None:
    task_id = "task_worker_input"
    lease_id = "lease_worker_input"
    evidence_id = "evidence_worker_input"
    _task(store, task_id)
    store.execute(
        "INSERT INTO work_package_task_links ("
        "task_id, package_id, plan_version, epoch, node_key, node_generation, "
        "declared_effects_digest, contract_digest, input_digest, node_state, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            "wp_1",
            1,
            1,
            "worker_input",
            1,
            "sha256:effects",
            "sha256:contract",
            "sha256:input",
            "planned",
            CREATED_AT,
        ),
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'ready' WHERE task_id = ?",
        (task_id,),
    )
    store.execute(
        "INSERT INTO leases ("
        "id, task_id, agent_id, expires_at, status, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            lease_id,
            task_id,
            "agent_worker",
            "2099-01-01T00:00:00+00:00",
            "active",
            CREATED_AT,
            CREATED_AT,
        ),
    )
    store.execute(
        "INSERT INTO work_package_assignment_audit ("
        "lease_id, package_id, plan_version, epoch, node_key, task_id, agent_id, "
        "attempt_number, attempt_ref, attempt_base_ref, attempt_base_sha, "
        "declared_effects_digest, allocator, allocator_version, score, rationale, "
        "decision, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lease_id,
            "wp_1",
            1,
            1,
            "worker_input",
            task_id,
            "agent_worker",
            1,
            reviewed_ref,
            TARGET_REF,
            base_sha,
            "sha256:effects",
            "allocator",
            "v1",
            1.0,
            "test input",
            "{}",
            CREATED_AT,
        ),
    )
    _evidence(store, evidence_id, task_id)
    store.execute(
        "INSERT INTO evidence_attempt_links ("
        "evidence_id, task_id, lease_id, agent_id, attempt_number, attempt_ref, "
        "attempt_base_sha, attempt_head_sha, artifact_digest, "
        "declared_effects_digest, observed_effects_digest, protected_ref, "
        "controller_verified, controller_verifier, controller_verified_at, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id,
            task_id,
            lease_id,
            "agent_worker",
            1,
            reviewed_ref,
            base_sha,
            reviewed_sha,
            "sha256:artifact",
            "sha256:effects",
            "sha256:observed",
            1,
            1,
            "controller",
            CREATED_AT,
            CREATED_AT,
        ),
    )
    store.execute(
        "INSERT INTO evidence_attempt_verifications ("
        "id, evidence_id, task_id, lease_id, agent_id, attempt_number, "
        "repository_id, attempt_ref, attempt_base_sha, attempt_head_sha, "
        "tree_digest, declared_effects_digest, observed_effects_digest, "
        "changed_paths, changes, verifier, verifier_version, verified_at, "
        "receipt_digest"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "verification_worker_input",
            evidence_id,
            task_id,
            lease_id,
            "agent_worker",
            1,
            "repo_1",
            reviewed_ref,
            base_sha,
            reviewed_sha,
            "sha256:" + "5" * 64,
            "sha256:effects",
            "sha256:" + "6" * 64,
            '["candidate.txt"]',
            '[{"path":"candidate.txt","status":"A"}]',
            "git-attempt-output",
            "work-package-output-verifier-v1",
            CREATED_AT,
            "sha256:" + "7" * 64,
        ),
    )
    store.execute(
        "INSERT INTO work_package_node_candidates ("
        "id, task_id, package_id, plan_version, epoch, node_key, node_generation, "
        "assignment_lease_id, attempt_number, evidence_id, status, submitted_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "candidate_worker_input",
            task_id,
            "wp_1",
            1,
            1,
            "worker_input",
            1,
            lease_id,
            1,
            evidence_id,
            "submitted",
            CREATED_AT,
        ),
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'executing' WHERE task_id = ?",
        (task_id,),
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'candidate_submitted' "
        "WHERE task_id = ?",
        (task_id,),
    )
    store.execute(
        "UPDATE work_package_node_candidates SET status = 'accepted', "
        "accepted_at = ?, accepted_by = 'controller' WHERE id = 'candidate_worker_input'",
        (CREATED_AT,),
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'candidate_accepted' "
        "WHERE task_id = ?",
        (task_id,),
    )
    input_digest = compute_landing_input_digest(
        [
            AssemblyInput(
                ordinal=0,
                task_id=task_id,
                evidence_id=evidence_id,
                protected_ref=reviewed_ref,
                reviewed_sha=reviewed_sha,
            )
        ]
    )
    store.execute(
        "INSERT INTO work_package_integration_batches ("
        "id, package_id, plan_version, epoch, repository_id, target_ref, "
        "assembly_base_sha, landing_base_sha, input_digest, state, "
        "integration_task_id, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "batch_assemble",
            "wp_1",
            1,
            1,
            "repo_1",
            TARGET_REF,
            base_sha,
            base_sha,
            input_digest,
            "queued",
            "task_integration",
            "{}",
            CREATED_AT,
            CREATED_AT,
        ),
    )
    store.execute(
        "INSERT INTO work_package_batch_inputs ("
        "id, batch_id, package_id, plan_version, epoch, ordinal, node_key, "
        "node_generation, task_id, candidate_id, candidate_status, "
        "assignment_lease_id, attempt_number, evidence_id, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "batch_input_1",
            "batch_assemble",
            "wp_1",
            1,
            1,
            0,
            "worker_input",
            1,
            task_id,
            "candidate_worker_input",
            "accepted",
            lease_id,
            1,
            evidence_id,
            CREATED_AT,
        ),
    )


def _remote_ref(remote: Path, ref: str) -> str:
    output = _git(remote, "ls-remote", str(remote), ref)
    return output.split()[0] if output else ""


def _service(
    store: SQLiteStore,
    *,
    owner: str = "landing-a",
    git_runner=None,
    fault_hook=None,
    lease_seconds: int = 30,
    now=None,
) -> LandingService:
    return LandingService(
        store,
        owner=owner,
        config=LandingServiceConfig(enabled=True, lease_seconds=lease_seconds),
        git_runner=git_runner,
        fault_hook=fault_hook,
        now=now,
    )


def test_landing_is_disabled_by_default() -> None:
    store = SQLiteStore(":memory:")
    try:
        service = LandingService(store, owner="disabled-test")
        endpoint = RepositoryEndpoint("repo_1", "/tmp/does-not-matter.git")
        with pytest.raises(LandingDisabledError):
            service.land("batch_1", endpoint)
    finally:
        store.close()


def test_assembly_fetches_reviewed_sha_and_stages_exact_disposable_candidate(
    tmp_path: Path,
) -> None:
    remote, _work, base_sha, reviewed_sha = _repository(tmp_path)
    store = SQLiteStore(str(tmp_path / "mac.db"))
    try:
        _seed_certified_batch(
            store, remote=remote, base_sha=base_sha, candidate_sha=reviewed_sha
        )
        _seed_assembly_batch(
            store,
            base_sha=base_sha,
            reviewed_sha=reviewed_sha,
            reviewed_ref=ATTEMPT_REF,
        )

        outcome = _service(store).assemble(
            "batch_assemble", RepositoryEndpoint("repo_1", str(remote))
        )

        assert outcome.status == "assembled"
        batch = store.query_one(
            "SELECT * FROM work_package_integration_batches "
            "WHERE id = 'batch_assemble'"
        )
        assert batch["state"] == "verifying"
        assert batch["candidate_sha"] == outcome.candidate_sha
        assert batch["candidate_sha"] != reviewed_sha  # --no-ff assembly commit
        assert batch["candidate_ref"].startswith("refs/mac/candidates/batch_assemble/")
        assert _remote_ref(remote, batch["candidate_ref"]) == batch["candidate_sha"]
        assert _remote_ref(remote, CANDIDATE_REF) == reviewed_sha
    finally:
        store.close()


@pytest.mark.parametrize(
    ("crash_stage", "staged_before_recovery"),
    [
        ("after_candidate_assignment", False),
        ("after_candidate_stage", True),
    ],
)
def test_assembly_resumes_original_candidate_after_crash(
    tmp_path: Path,
    crash_stage: str,
    staged_before_recovery: bool,
) -> None:
    remote, _work, base_sha, reviewed_sha = _repository(tmp_path)
    store = SQLiteStore(str(tmp_path / "mac.db"))
    _seed_certified_batch(
        store, remote=remote, base_sha=base_sha, candidate_sha=reviewed_sha
    )
    _seed_assembly_batch(
        store,
        base_sha=base_sha,
        reviewed_sha=reviewed_sha,
        reviewed_ref=ATTEMPT_REF,
    )

    class SimulatedCrash(RuntimeError):
        pass

    def crash(stage, _detail):
        if stage == crash_stage:
            raise SimulatedCrash("lost assembly process")

    endpoint = RepositoryEndpoint("repo_1", str(remote))
    try:
        with pytest.raises(SimulatedCrash):
            _service(store, fault_hook=crash).assemble("batch_assemble", endpoint)
        assigned = store.query_one(
            "SELECT * FROM work_package_integration_batches "
            "WHERE id = 'batch_assemble'"
        )
        assert assigned["state"] == "assembling"
        assert assigned["candidate_sha"]
        assert bool(_remote_ref(remote, assigned["candidate_ref"])) is (
            staged_before_recovery
        )

        outcome = _service(store).assemble("batch_assemble", endpoint)
        resumed = store.query_one(
            "SELECT * FROM work_package_integration_batches "
            "WHERE id = 'batch_assemble'"
        )
        assert outcome.status == "assembled"
        assert outcome.detail["recovered"] is True
        assert resumed["state"] == "verifying"
        assert resumed["candidate_sha"] == assigned["candidate_sha"]
        assert resumed["candidate_ref"] == assigned["candidate_ref"]
        assert resumed["candidate_fence"] == assigned["candidate_fence"]
        assert _remote_ref(remote, assigned["candidate_ref"]) == assigned["candidate_sha"]
    finally:
        store.close()


def test_in_process_certification_is_blocked_and_external_record_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, _work, base_sha, reviewed_sha = _repository(tmp_path)
    store = SQLiteStore(str(tmp_path / "mac.db"))
    _seed_certified_batch(
        store, remote=remote, base_sha=base_sha, candidate_sha=reviewed_sha
    )
    _seed_assembly_batch(
        store,
        base_sha=base_sha,
        reviewed_sha=reviewed_sha,
        reviewed_ref=ATTEMPT_REF,
    )

    class RecordingRunner(SubprocessGitRunner):
        def __init__(self) -> None:
            super().__init__(timeout_seconds=30)
            self.environments = []

        def run(self, args, *, cwd, env):
            self.environments.append(dict(env))
            return super().run(args, cwd=cwd, env=env)

    monkeypatch.setenv("LANDING_UNRELATED_SECRET", "must-not-enter-git")
    runner = RecordingRunner()
    endpoint = RepositoryEndpoint("repo_1", str(remote))
    service = LandingService(
        store,
        owner="landing-a",
        config=LandingServiceConfig(enabled=True, lease_seconds=30),
        git_runner=runner,
        credential_environment=lambda _operation, _endpoint: {
            "LANDING_PUSH_SECRET": "secret-value"
        },
    )
    try:
        service.assemble("batch_assemble", endpoint)
        with pytest.raises(LandingError, match="release-blocked"):
            service.certify("batch_assemble", endpoint)
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_certifications "
            "WHERE batch_id = 'batch_assemble'"
        )["n"] == 0

        # Stand in for the separate OpenShell/network-disabled station.  The
        # landing process accepts only this durable, exact, policy-bound row.
        batch = store.query_one(
            "SELECT * FROM work_package_integration_batches "
            "WHERE id = 'batch_assemble'"
        )
        store.execute(
            "INSERT INTO work_package_certifications ("
            "id, batch_id, package_id, plan_version, epoch, candidate_sha, "
            "assembly_base_sha, landing_base_sha, target_ref, status, "
            "verification_digest, verification, certification_task_id, "
            "tests_evidence_id, review_task_id, review_evidence_id, "
            "certified_by, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "cert_assemble",
                "batch_assemble",
                "wp_1",
                1,
                1,
                batch["candidate_sha"],
                batch["assembly_base_sha"],
                batch["landing_base_sha"],
                batch["target_ref"],
                "passed",
                "sha256:" + "5" * 64,
                json_dumps(
                    {
                        "isolation": {
                            "schema": CERTIFICATION_ISOLATION_SCHEMA,
                            "network": "disabled",
                            "landing_credentials": "absent",
                            "planner_commands": "rejected",
                            "policy_source": "trusted_controller",
                            "policy_id": "trusted-repository-default",
                        }
                    }
                ),
                "task_integration",
                "evidence_tests",
                "task_review",
                "evidence_review",
                "openshell-certifier",
                CREATED_AT,
            ),
        )
        outcome = service.accept_certification(
            "batch_assemble", endpoint, certification_id="cert_assemble"
        )
        assert outcome.status == "certified"
        assert runner.environments
        assert all(
            env.get("LANDING_PUSH_SECRET") == "secret-value"
            for env in runner.environments
        )
        assert all(
            "LANDING_UNRELATED_SECRET" not in env for env in runner.environments
        )
        assert all(env.get("GIT_CONFIG_NOSYSTEM") == "1" for env in runner.environments)
    finally:
        store.close()


def test_exact_candidate_lands_with_append_only_receipt_and_ref_retirement(
    tmp_path: Path,
) -> None:
    remote, _work, base_sha, candidate_sha = _repository(tmp_path)
    store = SQLiteStore(str(tmp_path / "mac.db"))
    try:
        _seed_certified_batch(
            store, remote=remote, base_sha=base_sha, candidate_sha=candidate_sha
        )
        outcome = _service(store).land(
            "batch_1", RepositoryEndpoint("repo_1", str(remote))
        )

        assert outcome.status == "landed"
        assert outcome.remote_sha == candidate_sha
        assert _remote_ref(remote, TARGET_REF) == candidate_sha
        assert _remote_ref(remote, CANDIDATE_REF) == ""
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_intents"
        )["n"] == 1
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_attempts"
        )["n"] == 1
        receipt = store.query_one("SELECT * FROM work_package_landing_receipts")
        assert receipt["candidate_sha"] == candidate_sha
        assert receipt["observed_sha"] == candidate_sha
        assert receipt["recovered"] == 0
        assert store.query_one(
            "SELECT state FROM work_package_integration_batches WHERE id = 'batch_1'"
        )["state"] == "published"
    finally:
        store.close()


def test_caller_cannot_redirect_canonical_remote_or_landing_credentials(
    tmp_path: Path,
) -> None:
    remote, _work, base_sha, candidate_sha = _repository(tmp_path / "canonical")
    attacker, _attacker_work, _attacker_base, _attacker_candidate = _repository(
        tmp_path / "attacker"
    )
    store = SQLiteStore(str(tmp_path / "mac.db"))
    calls = []
    try:
        _seed_certified_batch(
            store, remote=remote, base_sha=base_sha, candidate_sha=candidate_sha
        )
        service = LandingService(
            store,
            owner="landing-a",
            config=LandingServiceConfig(enabled=True, lease_seconds=30),
            credential_environment=lambda operation, endpoint: calls.append(
                (operation, endpoint.remote_url)
            )
            or {"LANDING_PUSH_SECRET": "must-not-leak"},
        )
        with pytest.raises(LandingError, match="locked canonical"):
            service.land("batch_1", RepositoryEndpoint("repo_1", str(attacker)))
        assert calls == []
        assert _remote_ref(remote, TARGET_REF) == base_sha
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_intents"
        )["n"] == 0
    finally:
        store.close()


def test_non_descendant_certified_candidate_cannot_replace_canonical_history(
    tmp_path: Path,
) -> None:
    remote, work, base_sha, _candidate_sha = _repository(tmp_path)
    _git(work, "checkout", "--orphan", "unrelated-candidate")
    _git(work, "rm", "-rf", ".")
    (work / "unrelated.txt").write_text("unrelated history\n", encoding="utf-8")
    _git(work, "add", "unrelated.txt")
    _git(work, "commit", "-m", "unrelated candidate")
    unrelated_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "--force", "origin", "HEAD:%s" % CANDIDATE_REF)

    store = SQLiteStore(str(tmp_path / "mac.db"))
    try:
        _seed_certified_batch(
            store,
            remote=remote,
            base_sha=base_sha,
            candidate_sha=unrelated_sha,
        )
        with pytest.raises(LandingError, match="does not preserve landing base"):
            _service(store).land(
                "batch_1", RepositoryEndpoint("repo_1", str(remote))
            )
        assert _remote_ref(remote, TARGET_REF) == base_sha
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_intents"
        )["n"] == 0
    finally:
        store.close()


def test_landing_rejects_certification_from_non_contract_policy(tmp_path: Path) -> None:
    remote, _work, base_sha, candidate_sha = _repository(tmp_path)
    wrong_policy = json_dumps(
        {
            "isolation": {
                "schema": CERTIFICATION_ISOLATION_SCHEMA,
                "network": "disabled",
                "landing_credentials": "absent",
                "planner_commands": "rejected",
                "policy_source": "trusted_controller",
                "policy_id": "planner-selected-policy",
            }
        }
    )
    store = SQLiteStore(str(tmp_path / "mac.db"))
    try:
        _seed_certified_batch(
            store,
            remote=remote,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            certification_verification=wrong_policy,
        )
        with pytest.raises(LandingError, match="does not match repository contract"):
            _service(store).land(
                "batch_1", RepositoryEndpoint("repo_1", str(remote))
            )
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_intents"
        )["n"] == 0
    finally:
        store.close()


def test_crash_after_push_recovers_by_remote_readback_without_second_push(
    tmp_path: Path,
) -> None:
    remote, _work, base_sha, candidate_sha = _repository(tmp_path)
    store = SQLiteStore(str(tmp_path / "mac.db"))
    _seed_certified_batch(
        store, remote=remote, base_sha=base_sha, candidate_sha=candidate_sha
    )

    class SimulatedCrash(RuntimeError):
        pass

    def crash(stage, _detail):
        if stage == "after_push":
            raise SimulatedCrash("power loss")

    endpoint = RepositoryEndpoint("repo_1", str(remote))
    try:
        with pytest.raises(SimulatedCrash):
            _service(store, fault_hook=crash).land("batch_1", endpoint)

        assert _remote_ref(remote, TARGET_REF) == candidate_sha
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_attempts"
        )["n"] == 1
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_receipts"
        )["n"] == 0

        outcome = _service(store).land("batch_1", endpoint)
        assert outcome.status == "recovered"
        assert outcome.detail["recovery"] == "exact_remote_readback"
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_attempts"
        )["n"] == 1
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_receipts"
        )["n"] == 1
    finally:
        store.close()


class _BlockingPushRunner(SubprocessGitRunner):
    def __init__(self) -> None:
        super().__init__(timeout_seconds=30)
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self, args, *, cwd, env):
        if (
            args
            and args[0] == "push"
            and any(str(item).endswith(":" + TARGET_REF) for item in args)
        ):
            self.entered.set()
            assert self.release.wait(timeout=10)
        return super().run(args, cwd=cwd, env=env)


def test_repository_stream_lease_excludes_a_concurrent_lander(tmp_path: Path) -> None:
    remote, _work, base_sha, candidate_sha = _repository(tmp_path)
    db_path = tmp_path / "mac.db"
    first_store = SQLiteStore(str(db_path))
    _seed_certified_batch(
        first_store, remote=remote, base_sha=base_sha, candidate_sha=candidate_sha
    )
    second_store = SQLiteStore(str(db_path), initialize_schema=False)
    runner = _BlockingPushRunner()
    endpoint = RepositoryEndpoint("repo_1", str(remote))
    result: list[object] = []

    def run_first() -> None:
        try:
            result.append(
                _service(first_store, owner="landing-a", git_runner=runner).land(
                    "batch_1", endpoint
                )
            )
        except BaseException as exc:  # preserve thread failures for the assertion
            result.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert runner.entered.wait(timeout=10)
        with pytest.raises(LandingBusyError):
            _service(second_store, owner="landing-b").land("batch_1", endpoint)
    finally:
        runner.release.set()
        thread.join(timeout=15)
        second_store.close()
        first_store.close()

    assert len(result) == 1
    assert getattr(result[0], "status", None) == "landed"


def test_canonical_move_marks_batch_stale_and_invalidates_certification(
    tmp_path: Path,
) -> None:
    remote, work, base_sha, candidate_sha = _repository(tmp_path)
    store = SQLiteStore(str(tmp_path / "mac.db"))
    try:
        _seed_certified_batch(
            store, remote=remote, base_sha=base_sha, candidate_sha=candidate_sha
        )
        _git(work, "checkout", "main")
        (work / "other.txt").write_text("unrelated\n", encoding="utf-8")
        _git(work, "add", "other.txt")
        _git(work, "commit", "-m", "canonical moved")
        moved_sha = _git(work, "rev-parse", "HEAD")
        _git(work, "push", "origin", "HEAD:%s" % TARGET_REF)

        outcome = _service(store).land(
            "batch_1", RepositoryEndpoint("repo_1", str(remote))
        )
        assert outcome.status == "stale"
        assert outcome.remote_sha == moved_sha
        assert store.query_one(
            "SELECT state FROM work_package_integration_batches WHERE id = 'batch_1'"
        )["state"] == "stale"
        assert store.query_one(
            "SELECT status FROM work_package_certifications WHERE id = 'cert_1'"
        )["status"] == "invalidated"
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_intents"
        )["n"] == 1
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_attempts"
        )["n"] == 0
        assert _remote_ref(remote, CANDIDATE_REF) == candidate_sha
    finally:
        store.close()


def test_append_only_landing_records_reject_mutation_and_deletion(tmp_path: Path) -> None:
    remote, _work, base_sha, candidate_sha = _repository(tmp_path)
    store = SQLiteStore(str(tmp_path / "mac.db"))
    try:
        _seed_certified_batch(
            store, remote=remote, base_sha=base_sha, candidate_sha=candidate_sha
        )
        _service(store).land("batch_1", RepositoryEndpoint("repo_1", str(remote)))
        for table in (
            "work_package_landing_intents",
            "work_package_landing_attempts",
            "work_package_landing_receipts",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                store.execute("UPDATE %s SET id = id" % table)
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                store.execute("DELETE FROM %s" % table)

        stream = store.query_one("SELECT * FROM work_package_landing_streams")
        with pytest.raises(sqlite3.IntegrityError, match="monotonic fence"):
            store.execute(
                "UPDATE work_package_landing_streams SET lease_fence = ? "
                "WHERE repository_id = ? AND target_ref = ?",
                (int(stream["lease_fence"]) - 1, "repo_1", TARGET_REF),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.execute("DELETE FROM work_package_landing_streams")

        intent = store.query_one("SELECT * FROM work_package_landing_intents")
        with pytest.raises(sqlite3.IntegrityError, match="current stream fence"):
            store.execute(
                "INSERT INTO work_package_landing_attempts ("
                "id, intent_id, attempt_number, repository_id, target_ref, "
                "candidate_sha, expected_remote_sha, stream_fence, created_by, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "landtry_unfenced",
                    intent["id"],
                    2,
                    "repo_1",
                    TARGET_REF,
                    candidate_sha,
                    base_sha,
                    int(stream["lease_fence"]),
                    "stale-writer",
                    CREATED_AT,
                ),
            )
    finally:
        store.close()


class _ManualClock:
    """Deterministic, monotonic UTC clock for fenced-lease expiry coverage."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _stream_row(store: SQLiteStore):
    return store.query_one(
        "SELECT lease_owner, lease_expires_at, lease_fence "
        "FROM work_package_landing_streams "
        "WHERE repository_id = 'repo_1' AND target_ref = ?",
        (TARGET_REF,),
    )


def _batch_lease_row(store: SQLiteStore):
    return store.query_one(
        "SELECT lease_owner, lease_expires_at, lease_fence "
        "FROM work_package_integration_batches WHERE id = 'batch_1'"
    )


def test_stream_lease_renewal_extends_deadline_without_advancing_fence(
    tmp_path: Path,
) -> None:
    remote, _work, base_sha, candidate_sha = _repository(tmp_path)
    store = SQLiteStore(str(tmp_path / "mac.db"))
    try:
        _seed_certified_batch(
            store, remote=remote, base_sha=base_sha, candidate_sha=candidate_sha
        )
        clock = _ManualClock()
        service = _service(store, owner="landing-a", lease_seconds=30, now=clock)
        batch = service._batch("batch_1")

        stream = service._acquire_stream(batch)
        assert stream is not None
        original_expiry = _stream_row(store)["lease_expires_at"]

        # Advance almost to expiry, then renew: the deadline moves forward but
        # the monotonic fence is unchanged, so possession is preserved.
        clock.advance(25)
        service._renew_stream(stream)
        renewed = _stream_row(store)
        assert renewed["lease_fence"] == stream.fence
        assert renewed["lease_expires_at"] > original_expiry

        # A competing lander must still be excluded because the renewed deadline
        # has not yet elapsed on the deterministic clock.
        contender = _service(store, owner="landing-b", lease_seconds=30, now=clock)
        assert contender._acquire_stream(batch) is None
    finally:
        store.close()


def test_stream_lease_expires_deterministically_and_transfers_with_new_fence(
    tmp_path: Path,
) -> None:
    remote, _work, base_sha, candidate_sha = _repository(tmp_path)
    store = SQLiteStore(str(tmp_path / "mac.db"))
    try:
        _seed_certified_batch(
            store, remote=remote, base_sha=base_sha, candidate_sha=candidate_sha
        )
        clock = _ManualClock()
        holder = _service(store, owner="landing-a", lease_seconds=30, now=clock)
        batch = holder._batch("batch_1")
        stream = holder._acquire_stream(batch)
        assert stream is not None

        contender = _service(store, owner="landing-b", lease_seconds=30, now=clock)

        # Before the lease elapses the contender is refused.
        clock.advance(29)
        assert contender._acquire_stream(batch) is None

        # Exactly at the deterministic expiry the contender takes over and the
        # fence advances so the stale holder can no longer act.
        clock.advance(1)
        stolen = contender._acquire_stream(batch)
        assert stolen is not None
        assert stolen.fence == stream.fence + 1

        # The evicted holder's fenced renewal fails closed.
        with pytest.raises(LandingLeaseLostError):
            holder._renew_stream(stream)
    finally:
        store.close()


def test_batch_lease_renewal_and_deterministic_expiry(tmp_path: Path) -> None:
    remote, _work, base_sha, candidate_sha = _repository(tmp_path)
    store = SQLiteStore(str(tmp_path / "mac.db"))
    try:
        _seed_certified_batch(
            store, remote=remote, base_sha=base_sha, candidate_sha=candidate_sha
        )
        clock = _ManualClock()
        holder = _service(store, owner="landing-a", lease_seconds=30, now=clock)
        lease = holder._acquire_batch("batch_1")
        assert lease is not None
        original_expiry = _batch_lease_row(store)["lease_expires_at"]

        clock.advance(25)
        holder._renew_batch(lease)
        renewed = _batch_lease_row(store)
        assert renewed["lease_fence"] == lease.fence
        assert renewed["lease_expires_at"] > original_expiry

        contender = _service(store, owner="landing-b", lease_seconds=30, now=clock)
        # Renewed deadline has not elapsed yet: contender excluded.
        assert contender._acquire_batch("batch_1") is None

        # Advance past the renewed deadline; contender takes over with a bumped
        # fence and the stale holder's renewal fails closed.
        clock.advance(30)
        stolen = contender._acquire_batch("batch_1")
        assert stolen is not None
        assert stolen.fence == lease.fence + 1
        with pytest.raises(LandingLeaseLostError):
            holder._renew_batch(lease)
    finally:
        store.close()


def test_renew_leases_refreshes_both_fences_and_reasserts(tmp_path: Path) -> None:
    remote, _work, base_sha, candidate_sha = _repository(tmp_path)
    store = SQLiteStore(str(tmp_path / "mac.db"))
    try:
        _seed_certified_batch(
            store, remote=remote, base_sha=base_sha, candidate_sha=candidate_sha
        )
        clock = _ManualClock()
        service = _service(store, owner="landing-a", lease_seconds=30, now=clock)
        batch = service._batch("batch_1")
        stream = service._acquire_stream(batch)
        assert stream is not None
        batch_lease = service._acquire_batch("batch_1")
        assert batch_lease is not None

        stream_before = _stream_row(store)["lease_expires_at"]
        batch_before = _batch_lease_row(store)["lease_expires_at"]

        clock.advance(20)
        service._renew_leases(stream, batch_lease)

        assert _stream_row(store)["lease_expires_at"] > stream_before
        assert _batch_lease_row(store)["lease_expires_at"] > batch_before

        # If the batch fence is stolen, the combined renewal fails closed.
        contender = _service(store, owner="landing-b", lease_seconds=30, now=clock)
        clock.advance(40)
        assert contender._acquire_batch("batch_1") is not None
        with pytest.raises(LandingLeaseLostError):
            service._renew_leases(stream, batch_lease)
    finally:
        store.close()
