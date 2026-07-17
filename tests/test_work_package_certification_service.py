from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from mac.models import TransitionError, ValidationError, json_dumps
from mac.openshell_certifier import (
    CERTIFICATION_ISOLATION_SCHEMA,
    CertificationCheckResult,
    OpenShellCertificationResult,
)
from mac.store import SQLiteStore
from mac.work_package_certification_service import (
    CERTIFICATION_CONTRACT_SCHEMA,
    CertificationJobBusyError,
    CertificationJobLeaseLostError,
    WorkPackageCertificationService,
)


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
NOW_TEXT = NOW.isoformat(timespec="microseconds")
PACKAGE_ID = "wp_certification"
REPOSITORY_ID = "repo_certification"
BATCH_ID = "wpbatch_certification"
MUTATION_TASK_ID = "task_build"
INTEGRATION_TASK_ID = "task_assemble"
CERTIFICATION_TASK_ID = "task_certify"
INTEGRATION_NODE_KEY = "assemble"
CERTIFICATION_NODE_KEY = "certify"
CANDIDATE_SHA = "a" * 40
CANDIDATE_TREE_DIGEST = "git-tree:" + "b" * 40
BASE_SHA = "c" * 40
TARGET_REF = "refs/heads/main"
CANDIDATE_REF = "refs/mac/integration/wp-certification"

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


def _digest_bytes(value: bytes) -> str:
    return "sha256:%s" % hashlib.sha256(value).hexdigest()


def _digest_json(value: Mapping[str, Any]) -> str:
    return _digest_bytes(json_dumps(dict(value)).encode("utf-8"))


def _controller_metadata(node_key: str, node_type: str) -> str:
    return json_dumps(
        {
            "no_dispatch": True,
            "work_package": {
                "schema": "mac.work_package.task.v1",
                "package_id": PACKAGE_ID,
                "plan_version": 1,
                "epoch": 1,
                "node_key": node_key,
                "node_generation": 1,
                "node_type": node_type,
            },
        }
    )


def _task(
    store: SQLiteStore,
    task_id: str,
    *,
    state: str,
    dependencies: list[str],
    metadata: str = "{}",
) -> None:
    store.execute(
        "INSERT INTO tasks ("
        "id, title, description, priority, state, required_capabilities, "
        "dependencies, metadata, attempt_count, max_attempts, created_at, updated_at, "
        "completed_at"
        ") VALUES (?, ?, '', 0, ?, '[]', ?, ?, 0, 3, ?, ?, ?)",
        (
            task_id,
            task_id,
            state,
            json_dumps(dependencies),
            metadata,
            NOW_TEXT,
            NOW_TEXT,
            NOW_TEXT if state == "completed" else None,
        ),
    )


def _seed(tmp_path: Path) -> tuple[SQLiteStore, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(":memory:")
    policy_checksum = _digest_bytes(POLICY_TEXT.encode("utf-8"))
    repository_metadata = {
        "repository_contract": {
            "landing_certification_policy_id": "trusted-repository-default",
            "work_package_certification": {
                "schema": CERTIFICATION_CONTRACT_SCHEMA,
                "policy": {
                    "policy_id": "trusted-repository-default",
                    "version": 7,
                    "checksum": policy_checksum,
                },
                "policy_text": POLICY_TEXT,
                "image_ref": "registry.invalid/mac-certifier@sha256:" + "d" * 64,
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
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, 'certification', '/tmp/certification', "
        "'git@example.invalid:certification.git', 'mac', '[]', 1, 60, ?, ?, ?)",
        (REPOSITORY_ID, json_dumps(repository_metadata), NOW_TEXT, NOW_TEXT),
    )
    store.execute(
        "INSERT INTO work_packages ("
        "id, project, repository_id, goal, state, current_plan_version, current_epoch, "
        "metadata, created_by, created_at, updated_at"
        ") VALUES (?, 'mac', ?, 'certify', 'draft', 0, 0, '{}', 'planner', ?, ?)",
        (PACKAGE_ID, REPOSITORY_ID, NOW_TEXT, NOW_TEXT),
    )
    definition = {
        "schema": "mac.work_package.plan.v1",
        "package_id": PACKAGE_ID,
        "repository_id": REPOSITORY_ID,
        "planning_base_ref": TARGET_REF,
        "planning_base_sha": BASE_SHA,
        "integration": {"target_ref": TARGET_REF},
        "nodes": [
            {"node_key": "build", "kind": "mutation", "depends_on": []},
            {
                "node_key": INTEGRATION_NODE_KEY,
                "kind": "integration",
                "depends_on": ["build"],
            },
            {
                "node_key": CERTIFICATION_NODE_KEY,
                "kind": "certification",
                "depends_on": [INTEGRATION_NODE_KEY],
                "external_dependencies": [],
            },
        ],
        "derived": {
            "integration_groups": [
                {
                    "integration_node_key": INTEGRATION_NODE_KEY,
                    "member_node_keys": ["build"],
                }
            ]
        },
    }
    store.execute(
        "INSERT INTO work_package_plan_versions ("
        "package_id, version, definition, plan_digest, reason, created_by, created_at"
        ") VALUES (?, 1, ?, ?, 'test', 'planner', ?)",
        (PACKAGE_ID, json_dumps(definition), "sha256:" + "1" * 64, NOW_TEXT),
    )
    store.execute(
        "INSERT INTO work_package_epochs ("
        "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, "
        "status, reason, created_by, created_at"
        ") VALUES (?, 1, 1, ?, ?, 'active', 'test', 'planner', ?)",
        (PACKAGE_ID, TARGET_REF, BASE_SHA, NOW_TEXT),
    )
    store.execute(
        "UPDATE work_packages SET state = 'admitted', current_plan_version = 1, "
        "current_epoch = 1 WHERE id = ?",
        (PACKAGE_ID,),
    )
    store.execute(
        "UPDATE work_packages SET state = 'active' WHERE id = ?", (PACKAGE_ID,)
    )

    _task(store, MUTATION_TASK_ID, state="completed", dependencies=[])
    _task(
        store,
        INTEGRATION_TASK_ID,
        state="waiting",
        dependencies=[MUTATION_TASK_ID],
        metadata=_controller_metadata(INTEGRATION_NODE_KEY, "integration"),
    )
    _task(
        store,
        CERTIFICATION_TASK_ID,
        state="waiting",
        dependencies=[INTEGRATION_TASK_ID],
        metadata=_controller_metadata(CERTIFICATION_NODE_KEY, "certification"),
    )
    for task_id, node_key in (
        (MUTATION_TASK_ID, "build"),
        (INTEGRATION_TASK_ID, INTEGRATION_NODE_KEY),
        (CERTIFICATION_TASK_ID, CERTIFICATION_NODE_KEY),
    ):
        store.execute(
            "INSERT INTO work_package_task_links ("
            "task_id, package_id, plan_version, epoch, node_key, node_generation, "
            "declared_effects_digest, contract_digest, input_digest, node_state, created_at"
            ") VALUES (?, ?, 1, 1, ?, 1, ?, ?, ?, 'planned', ?)",
            (
                task_id,
                PACKAGE_ID,
                node_key,
                "sha256:" + ("2" if node_key == "build" else "3") * 64,
                "sha256:" + "4" * 64,
                "sha256:" + "5" * 64,
                NOW_TEXT,
            ),
        )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'ready' WHERE task_id = ?",
        (INTEGRATION_TASK_ID,),
    )
    store.execute(
        "INSERT INTO work_package_integration_batches ("
        "id, package_id, plan_version, epoch, repository_id, target_ref, "
        "assembly_base_sha, landing_base_sha, input_digest, state, "
        "integration_task_id, lease_fence, metadata, created_at, updated_at"
        ") VALUES (?, ?, 1, 1, ?, ?, ?, ?, ?, 'queued', ?, 0, ?, ?, ?)",
        (
            BATCH_ID,
            PACKAGE_ID,
            REPOSITORY_ID,
            TARGET_REF,
            BASE_SHA,
            BASE_SHA,
            "sha256:" + "6" * 64,
            INTEGRATION_TASK_ID,
            json_dumps({"integration_node_key": INTEGRATION_NODE_KEY}),
            NOW_TEXT,
            NOW_TEXT,
        ),
    )
    expires = (NOW + timedelta(hours=1)).isoformat(timespec="microseconds")
    store.execute(
        "UPDATE work_package_integration_batches SET state = 'assembling', "
        "lease_owner = 'integrator', lease_expires_at = ?, lease_fence = 1 "
        "WHERE id = ?",
        (expires, BATCH_ID),
    )
    store.execute(
        "UPDATE work_package_integration_batches SET candidate_sha = ?, "
        "candidate_tree_digest = ?, candidate_ref = ?, candidate_fence = 1 "
        "WHERE id = ?",
        (CANDIDATE_SHA, CANDIDATE_TREE_DIGEST, CANDIDATE_REF, BATCH_ID),
    )
    store.execute(
        "UPDATE work_package_integration_batches SET state = 'verifying', "
        "lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
        (BATCH_ID,),
    )

    integration_detail = {
        "schema": "mac.work_package.controller_station_receipt.v1",
        "station_kind": "integration",
        "batch_id": BATCH_ID,
        "integration_task_id": INTEGRATION_TASK_ID,
        "integration_node_key": INTEGRATION_NODE_KEY,
        "certification_task_id": CERTIFICATION_TASK_ID,
        "certification_node_key": CERTIFICATION_NODE_KEY,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree_digest": CANDIDATE_TREE_DIGEST,
        "candidate_ref": CANDIDATE_REF,
        "candidate_fence": 1,
        "input_digest": "sha256:" + "6" * 64,
    }
    integration_identity = {
        "station_kind": "integration",
        "task_id": INTEGRATION_TASK_ID,
        "package_id": PACKAGE_ID,
        "plan_version": 1,
        "epoch": 1,
        "node_key": INTEGRATION_NODE_KEY,
        "batch_id": BATCH_ID,
        "outcome": "integrated",
        "detail": integration_detail,
    }
    receipt_digest = _digest_json(integration_identity)
    receipt_id = "wpstation_%s" % receipt_digest.split(":", 1)[1][:32]
    store.execute(
        "INSERT INTO work_package_controller_station_receipts ("
        "id, station_kind, task_id, package_id, plan_version, epoch, node_key, "
        "batch_id, outcome, provenance_digest, actor, detail, created_at"
        ") VALUES (?, 'integration', ?, ?, 1, 1, ?, ?, 'integrated', ?, "
        "'integrator', ?, ?)",
        (
            receipt_id,
            INTEGRATION_TASK_ID,
            PACKAGE_ID,
            INTEGRATION_NODE_KEY,
            BATCH_ID,
            receipt_digest,
            json_dumps(integration_detail),
            NOW_TEXT,
        ),
    )
    store.execute(
        "UPDATE tasks SET state = 'completed', completed_at = ? WHERE id = ?",
        (NOW_TEXT, INTEGRATION_TASK_ID),
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'integrated' WHERE task_id = ?",
        (INTEGRATION_TASK_ID,),
    )
    transition_detail = json_dumps(
        {"controller_station_receipt_id": receipt_id, "node_state": "integrated"}
    )
    store.execute(
        "INSERT INTO task_history ("
        "id, task_id, event_type, actor, from_state, to_state, detail, created_at"
        ") VALUES ('history_integration', ?, 'task.transitioned', 'integrator', "
        "'waiting', 'completed', ?, ?)",
        (INTEGRATION_TASK_ID, transition_detail, NOW_TEXT),
    )
    store.execute(
        "INSERT INTO task_transition_outbox ("
        "id, task_id, event_type, actor, from_state, to_state, detail, status, "
        "attempts, created_at, processed_at"
        ") VALUES ('outbox_integration', ?, 'task.lifecycle', 'integrator', "
        "'waiting', 'completed', ?, 'pending', 0, ?, NULL)",
        (INTEGRATION_TASK_ID, transition_detail, NOW_TEXT),
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'ready' WHERE task_id = ?",
        (CERTIFICATION_TASK_ID,),
    )

    # One held integration-stage token proves failed certification drains all
    # product WIP.  The immutable assignment is only attribution for the token.
    store.execute(
        "INSERT INTO leases (id, task_id, agent_id, expires_at, status, created_at, "
        "updated_at) VALUES ('lease_build', ?, 'agent-build', ?, 'released', ?, ?)",
        (MUTATION_TASK_ID, expires, NOW_TEXT, NOW_TEXT),
    )
    store.execute(
        "INSERT INTO work_package_assignment_audit ("
        "lease_id, package_id, plan_version, epoch, node_key, task_id, agent_id, "
        "attempt_number, attempt_ref, attempt_base_ref, attempt_base_sha, "
        "declared_effects_digest, allocator, allocator_version, rationale, decision, "
        "created_at"
        ") VALUES ('lease_build', ?, 1, 1, 'build', ?, 'agent-build', 1, "
        "'refs/mac/attempts/build', ?, ?, ?, 'test', 'v1', 'test', '{}', ?)",
        (
            PACKAGE_ID,
            MUTATION_TASK_ID,
            TARGET_REF,
            BASE_SHA,
            "sha256:" + "2" * 64,
            NOW_TEXT,
        ),
    )
    store.execute(
        "INSERT INTO work_package_wip_tokens ("
        "id, package_id, plan_version, epoch, node_key, task_id, resource_key, "
        "token_kind, stage, state, generation, capacity_units, reservation_key, "
        "acquired_by_assignment_lease_id, acquired_at"
        ") VALUES ('wip_integration', ?, 1, 1, 'build', ?, 'repo:slot:build', "
        "'mutation', 'integration', 'held', 1, 1, ?, 'lease_build', ?)",
        (PACKAGE_ID, MUTATION_TASK_ID, BATCH_ID, NOW_TEXT),
    )
    bundle = tmp_path / "candidate.bundle"
    bundle.write_bytes(b"# v2 git bundle\nfixture-object-data\n")
    assert store.query_all("PRAGMA foreign_key_check") == []
    return store, bundle


def test_repository_contract_validator_accepts_valid_and_rejects_invalid(
    tmp_path: Path,
) -> None:
    store, _bundle = _seed(tmp_path)
    service = WorkPackageCertificationService(store)

    assert service.validate_repository_contract(REPOSITORY_ID) is None

    store.execute(
        "UPDATE project_repositories SET metadata = ? WHERE id = ?",
        ("{not-json", REPOSITORY_ID),
    )
    with pytest.raises(ValidationError, match="metadata is malformed"):
        service.validate_repository_contract(REPOSITORY_ID)


def test_repository_contract_validator_rejects_unpinned_image_before_prepare(
    tmp_path: Path,
) -> None:
    store, _bundle = _seed(tmp_path)
    service = WorkPackageCertificationService(store)
    row = store.query_one(
        "SELECT metadata FROM project_repositories WHERE id = ?", (REPOSITORY_ID,)
    )
    metadata = json.loads(row["metadata"])
    metadata["repository_contract"]["work_package_certification"][
        "image_ref"
    ] = "registry.invalid/mac-certifier:mutable"
    store.execute(
        "UPDATE project_repositories SET metadata = ? WHERE id = ?",
        (json_dumps(metadata), REPOSITORY_ID),
    )

    with pytest.raises(ValidationError, match="certification contract is invalid"):
        service.validate_repository_contract(REPOSITORY_ID)


def _service(
    store: SQLiteStore,
    *,
    now: datetime = NOW,
    runner: Any = None,
) -> WorkPackageCertificationService:
    return WorkPackageCertificationService(
        store,
        owner="certifier-1",
        runner=runner,
        now=lambda: now,
    )


def _result_from_job(job: Any, *, passed: bool) -> OpenShellCertificationResult:
    command = job.controller_commands[0]
    check = CertificationCheckResult(
        command.command_id,
        command.argv,
        0 if passed else 1,
        "pass" if passed else "fail",
        "ok\n" if passed else "",
        "" if passed else "failed\n",
        False,
    )
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
        status="passed" if passed else "failed",
        policy=job.policy.identity(),
        image_ref=job.image_ref,
        image_digest=job.image_digest,
        bundle_digest=job.bundle_digest,
        commands_digest=job.commands_digest,
        sandbox_name="mac-cert-focused",
        checks=(check,),
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
        },
        started_at=NOW_TEXT,
        completed_at=(NOW + timedelta(seconds=1)).isoformat(timespec="microseconds"),
        cleanup_status="deleted",
        failure_class="" if passed else "controller_command_failed",
        failure_reason="" if passed else "contract-tests: failed",
    ).with_digest()


def _prepared_job(
    service: WorkPackageCertificationService,
    bundle: Path,
) -> tuple[dict[str, Any], Any]:
    public = service.prepare(BATCH_ID, bundle, actor="pipeline-controller")
    row = service.store.query_one(
        "SELECT * FROM work_package_certification_jobs WHERE id = ?", (public["id"],)
    )
    return public, service._job_from_row(row, bundle)


def _redigest(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    value.pop("result_digest", None)
    value["result_digest"] = _digest_json(value)
    return value


def test_prepare_binds_exact_certification_successor_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store, bundle = _seed(tmp_path)
    try:
        service = _service(store)
        first, _job = _prepared_job(service, bundle)
        second = service.prepare(BATCH_ID, bundle, actor="retrying-controller")

        assert first["created"] is True
        assert second["created"] is False
        assert second["id"] == first["id"]
        assert first["integration_task_id"] == INTEGRATION_TASK_ID
        assert first["integration_node_key"] == INTEGRATION_NODE_KEY
        assert first["certification_task_id"] == CERTIFICATION_TASK_ID
        assert first["certification_node_key"] == CERTIFICATION_NODE_KEY
        definition = json.loads(
            store.query_one(
                "SELECT definition FROM work_package_certification_jobs WHERE id = ?",
                (first["id"],),
            )["definition"]
        )
        assert definition["prepared_by"] == "pipeline-controller"
        assert definition["certification_task_id"] == CERTIFICATION_TASK_ID

        changed = tmp_path / "changed.bundle"
        changed.write_bytes(bundle.read_bytes() + b"changed")
        with pytest.raises(TransitionError, match="identity differs"):
            service.prepare(BATCH_ID, changed, actor="pipeline-controller")
    finally:
        store.close()


def test_claim_is_idempotent_per_owner_and_stale_fence_cannot_ingest(
    tmp_path: Path,
) -> None:
    store, bundle = _seed(tmp_path)
    try:
        first_service = _service(store)
        public, job = _prepared_job(first_service, bundle)
        first = first_service.claim(public["id"], owner="controller-one")
        retry = first_service.claim(public["id"], owner="controller-one")
        assert retry == first
        with pytest.raises(CertificationJobBusyError, match="live owner"):
            first_service.claim(public["id"], owner="controller-two")

        recovered_service = _service(store, now=NOW + timedelta(hours=2))
        recovered = recovered_service.claim(public["id"], owner="controller-two")
        assert recovered.fence == first.fence + 1
        payload = _result_from_job(job, passed=True)
        with pytest.raises(CertificationJobLeaseLostError, match="current job fence"):
            recovered_service.ingest(
                public["id"], payload, owner=first.owner, fence=first.fence
            )
        accepted = recovered_service.ingest(
            public["id"],
            payload,
            owner=recovered.owner,
            fence=recovered.fence,
        )
        assert accepted.status == "passed"
        assert accepted.certification_task_id == CERTIFICATION_TASK_ID
    finally:
        store.close()


def test_result_integrity_station_projection_and_idempotent_ingestion(
    tmp_path: Path,
) -> None:
    store, bundle = _seed(tmp_path)
    try:
        service = _service(store)
        public, job = _prepared_job(service, bundle)
        claim = service.claim(public["id"], owner="controller")
        payload = _result_from_job(job, passed=True).to_dict()
        malformed = dict(payload)
        malformed["checks"] = [dict(malformed["checks"][0])]
        malformed["checks"][0]["returncode"] = "0"
        malformed = _redigest(malformed)
        with pytest.raises(ValidationError, match="returncode"):
            service.ingest(
                public["id"], malformed, owner=claim.owner, fence=claim.fence
            )

        first = service.ingest(
            public["id"], payload, owner=claim.owner, fence=claim.fence
        )
        second = service.ingest(
            public["id"], payload, owner=claim.owner, fence=claim.fence
        )
        assert first.created is True
        assert second.created is False
        assert second.certification_id == first.certification_id
        assert first.batch_state == "verifying"
        assert first.package_state == "active"
        task = store.query_one(
            "SELECT state FROM tasks WHERE id = ?", (CERTIFICATION_TASK_ID,)
        )
        link = store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (CERTIFICATION_TASK_ID,),
        )
        evidence = store.query_all(
            "SELECT task_id, kind FROM evidence WHERE task_id = ? ORDER BY kind",
            (CERTIFICATION_TASK_ID,),
        )
        assert task["state"] == "completed"
        assert link["node_state"] == "certified"
        assert [(row["task_id"], row["kind"]) for row in evidence] == [
            (CERTIFICATION_TASK_ID, "review"),
            (CERTIFICATION_TASK_ID, "test"),
        ]
        assert store.query_all("PRAGMA foreign_key_check") == []

        different = replace(
            _result_from_job(job, passed=True),
            completed_at=(NOW + timedelta(seconds=2)).isoformat(timespec="microseconds"),
            result_digest="",
        ).with_digest()
        with pytest.raises(TransitionError, match="immutable"):
            service.ingest(
                public["id"], different, owner=claim.owner, fence=claim.fence
            )
    finally:
        store.close()


def test_failed_result_is_atomic_andon_and_drains_integration_wip(
    tmp_path: Path,
) -> None:
    store, bundle = _seed(tmp_path)
    try:
        service = _service(store)
        public, job = _prepared_job(service, bundle)
        claim = service.claim(public["id"], owner="controller")
        payload = _result_from_job(job, passed=False)
        first = service.ingest(
            public["id"], payload, owner=claim.owner, fence=claim.fence
        )
        retry = service.ingest(
            public["id"], payload, owner=claim.owner, fence=claim.fence
        )

        assert first.status == "failed"
        assert first.batch_state == "rejected"
        assert first.package_state == "paused"
        assert retry.created is False
        proof = service.reject_failed_certification(
            BATCH_ID,
            certification_id=first.certification_id,
            actor="pipeline-controller",
        )
        assert proof == {
            "status": "completed",
            "batch_id": BATCH_ID,
            "batch_state": "rejected",
            "certification_id": first.certification_id,
            "provenance_verified": True,
            "andon_recorded": True,
            "package_state": "paused",
            "wip_disposition": "quarantined",
            "held_wip_count": 0,
            "integration_task_id": INTEGRATION_TASK_ID,
            "certification_task_id": CERTIFICATION_TASK_ID,
            "integration_node_state": "integrated",
            "certification_node_state": "rejected",
            "controller_station_receipt_id": first.controller_station_receipt_id,
        }
        wip = store.query_one(
            "SELECT state, release_reason FROM work_package_wip_tokens "
            "WHERE id = 'wip_integration'"
        )
        assert wip["state"] == "cancelled"
        assert wip["release_reason"] == (
            "certification_quarantine:%s" % first.certification_id
        )
        assert store.query_one(
            "SELECT state FROM tasks WHERE id = ?", (CERTIFICATION_TASK_ID,)
        )["state"] == "failed"
        assert store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (CERTIFICATION_TASK_ID,),
        )["node_state"] == "rejected"
    finally:
        store.close()


class _RecordingCertificationRunner:
    def __init__(self) -> None:
        self.result_paths: list[Path] = []

    def run(self, job: Any, *, result_path: Path) -> OpenShellCertificationResult:
        self.result_paths.append(Path(result_path))
        return _result_from_job(job, passed=True)


def test_run_uses_private_result_directory_and_checks_bundle_before_claim(
    tmp_path: Path,
) -> None:
    store, bundle = _seed(tmp_path)
    try:
        runner = _RecordingCertificationRunner()
        service = _service(store, runner=runner)
        public, _job = _prepared_job(service, bundle)
        outcome = service.run(public["id"], bundle, owner="controller")
        assert outcome.status == "passed"
        assert runner.result_paths
        assert runner.result_paths[0].parent != bundle.parent
    finally:
        store.close()

    store, bundle = _seed(tmp_path / "mismatch")
    try:
        service = _service(store, runner=_RecordingCertificationRunner())
        public, _job = _prepared_job(service, bundle)
        wrong = bundle.with_name("wrong.bundle")
        wrong.write_bytes(bundle.read_bytes() + b"wrong")
        with pytest.raises(ValidationError, match="does not match"):
            service.run(public["id"], wrong, owner="controller")
        assert service.get(public["id"])["state"] == "queued"
    finally:
        store.close()


def test_sqlite_job_schema_rejects_incoherent_lifecycle_and_mutation(
    tmp_path: Path,
) -> None:
    store, bundle = _seed(tmp_path)
    try:
        service = _service(store)
        public, _job = _prepared_job(service, bundle)
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(
                "UPDATE work_package_certification_jobs SET state = 'running' "
                "WHERE id = ?",
                (public["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(
                "UPDATE work_package_certification_jobs SET candidate_sha = ? "
                "WHERE id = ?",
                ("e" * 40, public["id"]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(
                "UPDATE work_package_certification_jobs SET state = 'running', "
                "lease_owner = 'owner', lease_expires_at = ?, lease_fence = 2 "
                "WHERE id = ?",
                ((NOW + timedelta(hours=1)).isoformat(), public["id"]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(
                "DELETE FROM work_package_certification_jobs WHERE id = ?",
                (public["id"],),
            )
    finally:
        store.close()
