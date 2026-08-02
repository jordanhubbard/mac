from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mac.landing_service import (
    LandingService,
    LandingServiceConfig,
    RepositoryEndpoint,
)
from mac.models import TransitionError, json_dumps, utcnow
from mac.openshell_certifier import (
    CERTIFICATION_ISOLATION_SCHEMA,
    CertificationCheckResult,
    OpenShellCertificationResult,
)
from mac.services import ControlPlane
from mac.store import Store
from mac.test_support import ephemeral_store
from mac.work_package_acceptance_service import WorkPackageAcceptanceService
from mac.work_package_candidate_service import WorkPackageCandidateService
from mac.work_package_certification_service import (
    CERTIFICATION_CONTRACT_SCHEMA,
    WorkPackageCertificationService,
)
from mac.work_package_integration_service import WorkPackageIntegrationService
from mac.work_package_models import WORK_PACKAGE_PLAN_SCHEMA
from mac.work_package_output_service import WorkPackageOutputService
from mac.work_package_publication_finalizer import WorkPackagePublicationFinalizer
from tests.certifier_phase_profile_fixtures import mac_phase_profile


TARGET_REF = "refs/heads/main"
POLICY_ID = "trusted-work-package-e2e"
POLICY_TEXT = """\
version: 1
filesystem_policy:
  include_workdir: true
  read_only:
    - /usr
    - /bin
    - /etc
  read_write:
    - /tmp
    - /dev
landlock:
  compatibility: hard_requirement
process:
  run_as_user: sandbox
  run_as_group: sandbox
network_policies: {}
"""


def _sha256(value: bytes) -> str:
    return "sha256:%s" % hashlib.sha256(value).hexdigest()


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "canonical.git"
    work = tmp_path / "worker"
    remote.mkdir()
    work.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    _git(work, "init", "--initial-branch=main")
    _git(work, "config", "user.name", "Work Package E2E")
    _git(work, "config", "user.email", "work-package-e2e@example.invalid")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    contract_dir = work / ".mac"
    contract_dir.mkdir()
    (contract_dir / "project.yaml").write_text(
        json.dumps(
            {
                "schema": "mac.repository_contract.v1",
                "project": "mac",
                "platforms": ["linux"],
                "toolchain": {"required_commands": ["git"]},
                "bootstrap": {"command": "true"},
                "test": {"command": "true"},
                "evidence": {"required": ["tests"]},
                "canonical_remote_url": str(remote),
            }
        ),
        encoding="utf-8",
    )
    _git(work, "add", "README.md")
    _git(work, "add", ".mac/project.yaml")
    _git(work, "commit", "-m", "base")
    base_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "HEAD:%s" % TARGET_REF)
    return remote, work, base_sha


def _repository_contract(remote: Path) -> dict[str, Any]:
    return {
        "repository_contract": {
            "canonical_remote_url": str(remote),
            "landing_certification_policy_id": POLICY_ID,
            "work_package_certification": {
                "schema": CERTIFICATION_CONTRACT_SCHEMA,
                "policy": {
                    "policy_id": POLICY_ID,
                    "version": 1,
                    "checksum": _sha256(POLICY_TEXT.encode("utf-8")),
                },
                "policy_text": POLICY_TEXT,
                "phase_profile": mac_phase_profile(),
                "image_ref": ("registry.invalid/mac-certifier@sha256:" + "d" * 64),
                "controller_commands": [
                    {
                        "command_id": "contract-tests",
                        "argv": ["/opt/mac-certifier/bin/run-contract-tests"],
                        "timeout_seconds": 300,
                    }
                ],
            },
        }
    }


def _plan(package_id: str, base_sha: str) -> dict[str, Any]:
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": package_id,
        "goal": "assemble, certify, and land one exact local Git candidate",
        "project": "mac",
        "repository_id": "repo_e2e",
        "planning_base_ref": TARGET_REF,
        "planning_base_sha": base_sha,
        "plan_generation": 1,
        "max_in_flight": 1,
        "mutation_wip": {"max_tokens": 1},
        "integration": {"target_ref": TARGET_REF},
        "nodes": [
            {
                "node_key": "change",
                "title": "Create the product change",
                "node_type": "mutation",
                "effects": {"writes": ["product.txt"]},
                "expected_outputs": ["component-candidate"],
                "verification": {"profile": "repository-default"},
                "rework": {"max_cycles": 1},
                "estimates": {"confidence": "high"},
            },
            {
                "node_key": "assemble",
                "title": "Assemble the exact product",
                "node_type": "integration",
                "depends_on": ["change"],
                "inputs": ["component-candidate"],
                "expected_outputs": ["candidate-tree"],
                "verification": {"profile": "integration-default"},
                "estimates": {"confidence": "high"},
            },
            {
                "node_key": "certify",
                "title": "Certify the assembled product",
                "node_type": "certification",
                "depends_on": ["assemble"],
                "inputs": ["candidate-tree"],
                "expected_outputs": ["certificate"],
                "verification": {"profile": "certification-default"},
                "estimates": {"confidence": "high"},
            },
        ],
    }


class _ExternalCertificationRunner:
    def __init__(self, *, passed: bool) -> None:
        self.passed = passed
        self.calls: list[Any] = []

    def run(self, job: Any, *, result_path: Path) -> OpenShellCertificationResult:
        self.calls.append(job)
        checks = tuple(
            CertificationCheckResult(
                command.command_id,
                command.argv,
                0 if self.passed else 1,
                "pass" if self.passed else "fail",
                "ok\n" if self.passed else "",
                "" if self.passed else "contract failed\n",
                False,
            )
            for command in job.controller_commands
        )
        changed_files = ["docs/canaries/e2e.md"]

        def digest_json(value: Any) -> str:
            return (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                    ).encode("utf-8")
                ).hexdigest()
            )

        phase_manifest = {
            "schema": "mac.certifier_phase_manifest.v1",
            "trusted_source_revision": "f" * 40,
            "assembly_base_sha": job.assembly_base_sha,
            "candidate_sha": job.candidate_sha,
            "changed_files": changed_files,
            "changed_file_count": 1,
            "changed_files_digest": digest_json(changed_files),
            "selection_mode": "documentation_fast_lane",
            "authoritative": {
                "mode": "focused",
                "reason": "documentation_only_invariants",
                "tests": [
                    "tests/test_openshell_certifier.py",
                    "tests/test_publication_lane.py",
                    "tests/test_repository_contract_certification.py",
                ],
            },
            "supplemental": {
                "mode": "skipped",
                "reason": "documentation_only",
                "tests": [],
            },
            "full_suite_count": 0,
        }
        phase_manifest["manifest_digest"] = digest_json(phase_manifest)
        return OpenShellCertificationResult(
            job_id=job.job_id,
            job_digest=job.job_digest,
            batch_id=job.batch_id,
            package_id=job.package_id,
            plan_version=job.plan_version,
            epoch=job.epoch,
            candidate_sha=job.candidate_sha,
            candidate_tree_digest=job.candidate_tree_digest,
            assembly_base_sha=job.assembly_base_sha,
            landing_base_sha=job.landing_base_sha,
            target_ref=job.target_ref,
            status="passed" if self.passed else "failed",
            policy=job.policy.identity(),
            image_ref=job.image_ref,
            image_digest=job.image_digest,
            bundle_digest=job.bundle_digest,
            commands_digest=job.commands_digest,
            sandbox_name="mac-cert-e2e",
            checks=checks,
            phase_manifest=phase_manifest,
            isolation={
                "schema": CERTIFICATION_ISOLATION_SCHEMA,
                "network": "disabled",
                "landing_credentials": "absent",
                "planner_commands": "rejected",
                "policy_source": "trusted_controller",
                "policy_id": job.policy.policy_id,
                "policy_version": job.policy.version,
                "policy_checksum": job.policy.checksum,
                "landlock": "hard_requirement",
                "run_as_user": "non_root",
                "launcher_environment": ["PATH"],
                "input_format": "credential_free_git_bundle",
                "assembly_base_transport": "controller_bound_argv",
            },
            started_at="2026-07-17T12:00:00.000000+00:00",
            completed_at="2026-07-17T12:00:01.000000+00:00",
            cleanup_status="deleted",
            failure_class="" if self.passed else "controller_command_failed",
            failure_reason="" if self.passed else "contract-tests: failed",
        ).with_digest()


@dataclass
class _AssemblyLine:
    store: Store
    control: ControlPlane
    remote: Path
    work: Path
    base_sha: str
    package_id: str
    task_ids: dict[str, str]
    candidate_id: str
    evidence_id: str
    batch_id: str
    candidate_sha: str
    candidate_ref: str
    bundle: Path
    certification: Any
    certification_runner: _ExternalCertificationRunner
    expired_lease_id: str | None = None
    worker_lease_id: str | None = None
    fast_lane_task_id: str | None = None

    def close(self) -> None:
        self.store.close()


def _seed_review(
    store: Store,
    *,
    task_id: str,
    evidence_id: str,
    base_sha: str,
    head_sha: str,
) -> None:
    review_created_at = utcnow()
    verdict_created_at = utcnow()
    verdict_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "approved",
        "reviewed_evidence_id": evidence_id,
        "signed_by": "reviewer-e2e",
        "signature": "signed-e2e-review",
        "worktree_digest": _sha256(head_sha.encode("ascii")),
        "checks": [{"name": "independent-review", "returncode": 0}],
        "repo": {"base_sha": base_sha, "head_sha": head_sha},
    }
    store.execute(
        "INSERT INTO evidence ("
        "id, task_id, kind, uri, summary, checksum, metadata, created_by, created_at"
        ") VALUES (?, ?, 'review', 'artifact://e2e-review', 'approved', NULL, ?, ?, ?)",
        (
            "evidence_review_e2e",
            task_id,
            json_dumps({"verification": verdict_manifest}),
            "reviewer-e2e",
            verdict_created_at,
        ),
    )
    store.execute(
        "INSERT INTO reviews ("
        "id, task_id, reviewer_agent_id, status, reason, evidence_id, "
        "created_at, completed_at"
        ") VALUES ('review_e2e', ?, 'reviewer-e2e', 'approved', "
        "'exact candidate approved', 'evidence_review_e2e', ?, ?)",
        (task_id, review_created_at, verdict_created_at),
    )
    store.execute(
        "UPDATE tasks SET state = 'reviewing', updated_at = ? WHERE id = ?",
        (verdict_created_at, task_id),
    )


def _run_to_certification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    passed: bool,
    expire_first_claim: bool = False,
    via_fast_lane: bool = False,
) -> _AssemblyLine:
    remote, work, base_sha = _repository(tmp_path)
    store = ephemeral_store()
    package_id = "wp_e2e_%s" % ("pass" if passed else "fail")
    now = utcnow()
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES ('repo_e2e', 'e2e', ?, ?, 'mac', '[]', 1, 60, ?, ?, ?)",
        (
            str(work),
            str(remote),
            json_dumps(_repository_contract(remote)),
            now,
            now,
        ),
    )

    control = ControlPlane(store, secret_key="work-package-e2e-secret-key-0001")
    packages = control.work_packages
    fast_lane_task_id = None
    if via_fast_lane:
        # Keep this an exogenously assigned primary-cohort treatment while
        # making the E2E route deterministic and independent of test order.
        control._execution_cohort_treatment_percentage = 100
        monkeypatch.setattr(
            control,
            "_managed_single_task_rollout",
            lambda: {
                "schema": "mac.managed_single_task.rollout.v1",
                "ready": True,
                "package_capable_agent_ids": ["agent_runtime"],
                "blockers": [],
            },
        )
        monkeypatch.setattr(
            control,
            "_managed_single_task_readiness",
            lambda **_kwargs: {
                "schema": "mac.managed_single_task.readiness.v1",
                "ready": False,
                "repository_id": "repo_e2e",
                "eligible_agent_ids": [],
                "blockers": [{"code": "worker_not_registered_yet"}],
            },
        )
        created = control.create_task(
            "Create the product change",
            description="Create one exact local Git candidate.",
            project="mac",
            metadata={
                "no_decompose": True,
                "no_dispatch": True,
                "publication_lane_policy": "auto",
            },
        )
        fast_lane_task_id = created.id
        link = store.query_one(
            "SELECT package_id FROM work_package_task_links WHERE task_id = ?",
            (created.id,),
        )
        package_id = str(link["package_id"])
        plan_version = 1
        epoch = 1
    else:
        admission = packages.admit(
            _plan(package_id, base_sha),
            actor="planner-e2e",
            reason="approved E2E plan",
        )
        plan_version = admission.plan_version
        epoch = admission.epoch
    packages.activate(
        package_id,
        expected_plan_version=plan_version,
        expected_epoch=epoch,
        actor="operator-e2e",
    )
    task_ids = {
        str(row["node_key"]): str(row["task_id"])
        for row in store.query_all(
            "SELECT node_key, task_id FROM work_package_task_links "
            "WHERE package_id = ? ORDER BY node_key",
            (package_id,),
        )
    }

    monkeypatch.setattr(
        control,
        "_work_package_downstream_activation_readiness",
        lambda _described: {"ready": True, "code": "ready", "reason": ""},
    )
    monkeypatch.setattr(
        control,
        "_work_package_downstream_release_gate",
        lambda *_args, **_kwargs: {"ready": True, "code": "ready", "reason": ""},
    )
    machine = control.register_machine("work-package-e2e-host")
    agent = control.register_agent(
        machine.id,
        "work-package-e2e-worker",
        capabilities=["work_package_v1"],
        resources={"commands": {"available": ["git"]}},
    )
    monkeypatch.setattr(
        "mac.worker_credentials.assert_package_worker_ready",
        lambda conn, agent_id: {"ready": True, "agent_id": agent_id},
    )
    task, lease = control.claim_task(task_ids["change"], agent.id, sync_beads=False)
    expired_lease_id = None
    if expire_first_claim:
        expired_lease_id = lease.id
        original_tokens = {
            str(row["id"]): int(row["generation"])
            for row in store.query_all(
                "SELECT id, generation FROM work_package_wip_tokens "
                "WHERE task_id = ? AND state = 'held' ORDER BY id",
                (task.id,),
            )
        }
        assert original_tokens
        store.execute(
            "UPDATE leases SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000000+00:00", lease.id),
        )
        recovered = control.expire_leases(
            now="2030-01-01T00:00:00.000000+00:00",
            grace_seconds=0,
        )
        assert [recovered_task.id for recovered_task in recovered] == [task.id]
        task, lease = control.claim_task(task_ids["change"], agent.id, sync_beads=False)
        assert lease.id != expired_lease_id
        retry_tokens = store.query_all(
            "SELECT id, generation, predecessor_token_id, "
            "acquired_by_assignment_lease_id FROM work_package_wip_tokens "
            "WHERE task_id = ? AND state = 'held' ORDER BY id",
            (task.id,),
        )
        assert len(retry_tokens) == len(original_tokens)
        assert {row["predecessor_token_id"] for row in retry_tokens} == set(
            original_tokens
        )
        assert {row["acquired_by_assignment_lease_id"] for row in retry_tokens} == {
            lease.id
        }
        assert all(
            int(row["generation"])
            == original_tokens[str(row["predecessor_token_id"])] + 1
            for row in retry_tokens
        )
    control.start_task(
        task.id,
        agent.id,
        lease_id=lease.id,
        drain_outbox=False,
    )
    assignment = store.query_one(
        "SELECT * FROM work_package_assignment_audit WHERE lease_id = ?",
        (lease.id,),
    )
    attempt_ref = str(assignment["attempt_ref"])

    _git(work, "checkout", "--detach", base_sha)
    (work / "product.txt").write_text("assembled product\n", encoding="utf-8")
    _git(work, "add", "product.txt")
    _git(work, "commit", "-m", "produce exact component")
    head_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "HEAD:%s" % attempt_ref)
    assert _git(remote, "rev-parse", attempt_ref) == head_sha

    # Exercise the public worker-attribution boundary, including the immutable
    # digest over the exact repo claim and durable artifact identities.
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "summary": "exact E2E attempt",
        "repo": {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "remote_ref": attempt_ref,
            "pushed": True,
            "files_changed": ["product.txt"],
        },
        "tests": [{"name": "e2e", "returncode": 0, "status": "pass"}],
        "checks": [{"name": "e2e", "returncode": 0, "status": "pass"}],
    }
    artifact_payload = json_dumps(manifest).encode("utf-8")
    executor_evidence = control.add_evidence(
        task.id,
        "artifact",
        "artifact://e2e-executor",
        "exact worker output",
        agent.id,
        metadata={"verification": manifest},
        lease_id=lease.id,
        sync_beads=False,
        artifacts=[
            {
                "name": "worker-evidence.json",
                "artifact_type": "worker_manifest",
                "source_uri": "artifact://e2e-executor/worker-evidence.json",
                "content_type": "application/json",
                "encoding": "base64",
                "size_bytes": len(artifact_payload),
                "sha256": _sha256(artifact_payload),
                "content_base64": base64.b64encode(artifact_payload).decode("ascii"),
            }
        ],
    )
    evidence_id = executor_evidence.id
    attempt_link = store.query_one(
        "SELECT artifact_digest FROM evidence_attempt_links WHERE evidence_id = ?",
        (evidence_id,),
    )
    assert str(attempt_link["artifact_digest"]).startswith("sha256:")

    candidate = WorkPackageCandidateService(store).submit(
        evidence_id, actor="candidate-controller-e2e"
    )
    receipt = WorkPackageOutputService(store).verify(evidence_id)
    assert receipt.candidate_id == candidate.candidate.id
    monkeypatch.setattr(
        control, "_require_review_ready", lambda _task: executor_evidence
    )
    reviewed = control.submit_for_review(
        task.id,
        agent.id,
        lease_id=lease.id,
        drain_outbox=False,
    )
    assert reviewed.state == "needs_review"
    _seed_review(
        store,
        task_id=task.id,
        evidence_id=evidence_id,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    accepted = WorkPackageAcceptanceService(store).accept(
        candidate.candidate.id, actor="acceptance-controller-e2e"
    )
    assert accepted.status == "accepted"
    assert accepted.released_downstream_task_ids == (task_ids["assemble"],)

    integrator = WorkPackageIntegrationService(store, owner="integrator-e2e")
    batch = integrator.create_batch(
        package_id, "assemble", actor="pipeline-controller-e2e"
    )
    assembled = integrator.assemble(batch.batch_id)
    assert assembled.status == "assembled"
    assert _git(remote, "rev-parse", assembled.candidate_ref) == assembled.candidate_sha

    bundle = tmp_path / "candidate.bundle"
    _git(remote, "bundle", "create", str(bundle), assembled.candidate_ref)
    assert bundle.is_file() and bundle.stat().st_size > 0
    runner = _ExternalCertificationRunner(passed=passed)
    certifier = WorkPackageCertificationService(
        store, owner="certifier-controller-e2e", runner=runner
    )
    job = certifier.prepare(batch.batch_id, bundle, actor="pipeline-controller-e2e")
    certification = certifier.run(
        str(job["id"]), bundle, owner="external-certifier-e2e"
    )
    assert len(runner.calls) == 1
    return _AssemblyLine(
        store=store,
        control=control,
        remote=remote,
        work=work,
        base_sha=base_sha,
        package_id=package_id,
        task_ids=task_ids,
        candidate_id=candidate.candidate.id,
        evidence_id=evidence_id,
        batch_id=batch.batch_id,
        candidate_sha=assembled.candidate_sha,
        candidate_ref=assembled.candidate_ref,
        bundle=bundle,
        certification=certification,
        certification_runner=runner,
        expired_lease_id=expired_lease_id,
        worker_lease_id=lease.id,
        fast_lane_task_id=fast_lane_task_id,
    )


@pytest.mark.parametrize(
    ("expire_first_claim", "via_fast_lane"),
    [(False, False), (True, False), (False, True)],
    ids=["ordinary-claim", "expired-claim-retry", "ordinary-task-fast-lane"],
)
def test_managed_work_package_reaches_exact_landed_completed_product(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expire_first_claim: bool,
    via_fast_lane: bool,
) -> None:
    line = _run_to_certification(
        tmp_path,
        monkeypatch,
        passed=True,
        expire_first_claim=expire_first_claim,
        via_fast_lane=via_fast_lane,
    )
    try:
        endpoint = RepositoryEndpoint("repo_e2e", str(line.remote))
        landing = LandingService(
            line.store,
            owner="landing-controller-e2e",
            config=LandingServiceConfig(enabled=True),
        )
        certified = landing.accept_certification(
            line.batch_id,
            endpoint,
            certification_id=line.certification.certification_id,
        )
        assert certified.status == "certified"
        landed = landing.land(line.batch_id, endpoint)
        assert landed.status == "landed"
        assert landed.remote_sha == line.candidate_sha
        assert _git(line.remote, "rev-parse", TARGET_REF) == line.candidate_sha

        finalizer = WorkPackagePublicationFinalizer(line.store)
        finalized = finalizer.finalize_landed_batch(
            line.batch_id,
            actor="publication-finalizer-e2e",
            receipt_id=str(landed.detail["id"]),
        )
        retry = finalizer.finalize_landed_batch(
            line.batch_id,
            actor="publication-finalizer-e2e",
            receipt_id=str(landed.detail["id"]),
        )

        assert finalized.created is True
        assert retry.created is False
        assert retry.finalization_id == finalized.finalization_id
        package = line.store.query_one(
            "SELECT state FROM work_packages WHERE id = ?", (line.package_id,)
        )
        epoch = line.store.query_one(
            "SELECT status FROM work_package_epochs WHERE package_id = ? AND epoch = 1",
            (line.package_id,),
        )
        assert package["state"] == "completed"
        assert epoch["status"] == "completed"
        assert (
            line.store.query_one(
                "SELECT state FROM work_package_integration_batches WHERE id = ?",
                (line.batch_id,),
            )["state"]
            == "published"
        )
        if line.fast_lane_task_id is not None:
            package = line.store.query_one(
                "SELECT root_task_id FROM work_packages WHERE id = ?",
                (line.package_id,),
            )
            assert package["root_task_id"] == line.fast_lane_task_id
            route = line.control.task_publication_route(line.fast_lane_task_id)
            assert route["lane"] == "managed"
            assert route["route_state"] == "managed_completed"
            assert route["landing_receipt_id"] == str(landed.detail["id"])
            assert route["finalization_id"] == finalized.finalization_id
            outcome = line.control.work_package_telemetry.comparable_atomic_outcomes(
                package_id=line.package_id
            )
            assert len(outcome) == 1
            assert outcome[0]["canonical_publication_outcome"] == "succeeded"
            assert outcome[0]["canonical_publication_success"] is True
            assert outcome[0]["canonical_publication_proof"] == {
                "type": "managed_publication_finalization",
                "id": finalized.finalization_id,
            }
        tasks = line.store.query_all(
            "SELECT link.node_key, link.node_state, task.state AS task_state "
            "FROM work_package_task_links AS link JOIN tasks AS task "
            "ON task.id = link.task_id WHERE link.package_id = ? "
            "ORDER BY link.node_key",
            (line.package_id,),
        )
        assert [dict(row) for row in tasks] == [
            {
                "node_key": "assemble",
                "node_state": "integrated",
                "task_state": "completed",
            },
            {
                "node_key": "certify",
                "node_state": "certified",
                "task_state": "completed",
            },
            {
                "node_key": "change",
                "node_state": "candidate_accepted",
                "task_state": "completed",
            },
        ]
        assert (
            line.store.query_one(
                "SELECT COUNT(*) AS count FROM work_package_wip_tokens "
                "WHERE package_id = ? AND state = 'held'",
                (line.package_id,),
            )["count"]
            == 0
        )
        if expire_first_claim:
            assert line.expired_lease_id is not None
            assert line.worker_lease_id != line.expired_lease_id
    finally:
        line.close()


def test_failed_external_certification_never_lands_and_raises_andon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    line = _run_to_certification(
        tmp_path,
        monkeypatch,
        passed=False,
        via_fast_lane=True,
    )
    try:
        assert line.certification.status == "failed"
        proof = WorkPackageCertificationService(
            line.store, owner="rejection-controller-e2e"
        ).reject_failed_certification(
            line.batch_id,
            certification_id=line.certification.certification_id,
            actor="pipeline-controller-e2e",
        )
        assert proof["batch_state"] == "rejected"
        assert proof["package_state"] == "paused"
        assert proof["wip_disposition"] == "quarantined"
        assert proof["held_wip_count"] == 0

        landing = LandingService(
            line.store,
            owner="landing-controller-e2e",
            config=LandingServiceConfig(enabled=True),
        )
        endpoint = RepositoryEndpoint("repo_e2e", str(line.remote))
        with pytest.raises(TransitionError, match="only a verifying"):
            landing.accept_certification(
                line.batch_id,
                endpoint,
                certification_id=line.certification.certification_id,
            )
        with pytest.raises(TransitionError, match="only a certified"):
            landing.land(line.batch_id, endpoint)

        assert _git(line.remote, "rev-parse", TARGET_REF) == line.base_sha
        assert (
            line.store.query_one(
                "SELECT COUNT(*) AS count FROM work_package_landing_intents"
            )["count"]
            == 0
        )
        assert (
            line.store.query_one(
                "SELECT COUNT(*) AS count FROM work_package_landing_receipts"
            )["count"]
            == 0
        )
        package = line.store.query_one(
            "SELECT state FROM work_packages WHERE id = ?", (line.package_id,)
        )
        assert package["state"] == "paused"
        rejection = line.store.query_one(
            "SELECT detail FROM work_package_history WHERE package_id = ? "
            "AND event_type = 'work_package.certification_rejected'",
            (line.package_id,),
        )
        assert rejection is not None
        assert json.loads(rejection["detail"])["andon_recorded"] is True
        tasks = {
            str(row["node_key"]): dict(row)
            for row in line.store.query_all(
                "SELECT link.node_key, link.node_state, task.state AS task_state "
                "FROM work_package_task_links AS link JOIN tasks AS task "
                "ON task.id = link.task_id WHERE link.package_id = ?",
                (line.package_id,),
            )
        }
        assert tasks["assemble"]["node_state"] == "integrated"
        assert tasks["assemble"]["task_state"] == "completed"
        assert tasks["certify"]["node_state"] == "rejected"
        assert tasks["certify"]["task_state"] == "failed"
        assert (
            line.store.query_one(
                "SELECT COUNT(*) AS count FROM work_package_wip_tokens "
                "WHERE package_id = ? AND state = 'held'",
                (line.package_id,),
            )["count"]
            == 0
        )
        outcome = line.control.work_package_telemetry.comparable_atomic_outcomes(
            package_id=line.package_id
        )
        assert len(outcome) == 1
        assert outcome[0]["canonical_publication_outcome"] == "failed"
        assert outcome[0]["canonical_publication_failure_class"] == (
            "managed_certification_rejected_final"
        )
        assert outcome[0]["canonical_publication_proof"]["type"] == (
            "managed_certification_rejection"
        )
    finally:
        line.close()
