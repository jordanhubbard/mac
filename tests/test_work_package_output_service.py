from __future__ import annotations

import hashlib
import json
from typing import Callable, Optional

import pytest

from mac.models import TransitionError, ValidationError
from mac.services import ControlPlane
from mac.store import SQLiteStore
from mac.work_package_candidate_service import WorkPackageCandidateService
from mac.work_package_certification_service import CERTIFICATION_CONTRACT_SCHEMA
from mac.work_package_models import WORK_PACKAGE_PLAN_SCHEMA
from mac.work_package_output import (
    AttemptOutputObservation,
    AttemptPathChange,
    WORK_PACKAGE_OUTPUT_VERIFIER_VERSION,
)
from mac.work_package_output_service import WorkPackageOutputService
from mac.work_package_service import RepositoryBaseAttestation, WorkPackageService


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
REPOSITORY_SOURCE = "ssh://git@example.invalid/mac.git"
LEGACY_REPOSITORY_SOURCE = "ssh://git@example.invalid/obsolete.git"
POLICY_ID = "output-service-certification-policy"
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


def _repository_contract() -> dict:
    return {
        "canonical_remote_url": REPOSITORY_SOURCE,
        "landing_certification_policy_id": POLICY_ID,
        "work_package_certification": {
            "schema": CERTIFICATION_CONTRACT_SCHEMA,
            "policy": {
                "policy_id": POLICY_ID,
                "version": 1,
                "checksum": "sha256:"
                + hashlib.sha256(POLICY_TEXT.encode("utf-8")).hexdigest(),
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


class _RepositoryVerifier:
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


class _Observer:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        head_sha: str = HEAD_SHA,
        mutate: Optional[Callable[[], None]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.store = store
        self.head_sha = head_sha
        self.mutate = mutate
        self.error = error
        self.calls: list[tuple[dict, dict]] = []

    def observe(self, repository, **kwargs):
        # Network/Git observation must never pin the controller transaction.
        assert self.store._conn.in_transaction is False
        self.calls.append((dict(repository), dict(kwargs)))
        if self.mutate is not None:
            self.mutate()
        if self.error is not None:
            raise self.error
        return AttemptOutputObservation(
            repository_id=repository["id"],
            attempt_ref=kwargs["attempt_ref"],
            base_sha=kwargs["base_sha"],
            head_sha=self.head_sha,
            tree_digest="sha256:" + "c" * 64,
            observed_effects_digest="sha256:" + "d" * 64,
            changes=(AttemptPathChange(status="A", path="src/feature.py"),),
            changed_paths=("src/feature.py",),
            verifier=WORK_PACKAGE_OUTPUT_VERIFIER_VERSION,
            verified_at="2026-07-17T02:00:00+00:00",
        )


def _plan() -> dict:
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_output_service",
        "goal": "Verify one exact candidate output",
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
                "node_type": "mutation",
                "effects": {"writes": ["src"]},
                "expected_outputs": ["candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            },
            {
                "node_key": "assemble",
                "title": "Assemble the exact candidate",
                "node_type": "integration",
                "depends_on": ["change"],
                "inputs": ["candidate"],
                "expected_outputs": ["candidate-tree"],
                "verification": {"profile": "integration-default"},
                "estimates": {"confidence": "high"},
            },
        ],
    }


def _setup(
    monkeypatch,
    *,
    attempt_head_sha: Optional[str] = HEAD_SHA,
    protected_ref: bool = True,
) -> tuple[SQLiteStore, str, str, str]:
    monkeypatch.setenv("MAC_WORK_PACKAGE_PIPELINE_ENABLED", "true")
    monkeypatch.setenv("MAC_WORK_PACKAGE_LANDING_ENABLED", "true")
    monkeypatch.setenv(
        "MAC_WORK_PACKAGE_BUNDLE_DIR",
        "/tmp/mac-work-package-output-service-bundles",
    )
    store = SQLiteStore(":memory:")
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "repo_mac",
            "mac",
            "/controller/registered/path",
            LEGACY_REPOSITORY_SOURCE,
            "mac",
            "[]",
            1,
            60,
            json.dumps({"repository_contract": _repository_contract()}),
            "created",
            "updated",
        ),
    )
    package_service = WorkPackageService(
        store, repository_verifier=_RepositoryVerifier()
    )
    admitted = package_service.admit(_plan(), actor="controller", reason="test")
    package_service.activate(
        admitted.package.id,
        expected_plan_version=1,
        expected_epoch=1,
        actor="operator",
    )
    task_id = store.query_one(
        "SELECT task_id FROM work_package_task_links WHERE node_key = ?", ("change",)
    )["task_id"]

    control = ControlPlane(store, secret_key="output-service-test-secret-key-0001")
    # This fixture exercises output receipts, not the host OpenShell install;
    # explicitly satisfy the independent production-runtime pull gate.
    monkeypatch.setattr(
        control.work_package_certifications,
        "validate_runtime_binding",
        lambda: None,
    )
    machine = control.register_machine("candidate-host")
    agent = control.register_agent(
        machine.id,
        "candidate-worker",
        capabilities=["work_package_v1"],
    )
    monkeypatch.setattr(
        "mac.worker_credentials.assert_package_worker_ready",
        lambda conn, agent_id: {"ready": True, "agent_id": agent_id},
    )
    claimed, lease = control.claim_task(task_id, agent.id, sync_beads=False)
    control.start_task(task_id, agent.id, lease_id=lease.id, drain_outbox=False)

    evidence_id = "ev_output_service"
    store.execute(
        "INSERT INTO evidence ("
        "id, task_id, kind, uri, summary, checksum, metadata, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id,
            task_id,
            "artifact",
            "artifact://candidate",
            "candidate output",
            None,
            json.dumps(
                {
                    "verification": {"evidence_type": "repository_change"},
                    # Must never be treated as repository authority.
                    "repository_source": "https://worker:secret@example.invalid/repo",
                }
            ),
            agent.id,
            "now",
        ),
    )
    assignment = store.query_one(
        "SELECT * FROM work_package_assignment_audit WHERE lease_id = ?", (lease.id,)
    )
    store.execute(
        "INSERT INTO evidence_attempt_links ("
        "evidence_id, task_id, lease_id, agent_id, attempt_number, attempt_ref, "
        "attempt_base_sha, attempt_head_sha, declared_effects_digest, protected_ref, "
        "created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id,
            task_id,
            lease.id,
            agent.id,
            claimed.attempt_count,
            assignment["attempt_ref"],
            assignment["attempt_base_sha"],
            attempt_head_sha,
            assignment["declared_effects_digest"],
            int(protected_ref),
            "now",
        ),
    )
    candidate = WorkPackageCandidateService(store).submit(
        evidence_id, actor="candidate-controller"
    )
    return store, evidence_id, candidate.candidate.id, assignment["attempt_ref"]


def test_verifies_exact_candidate_and_appends_controller_receipt(monkeypatch) -> None:
    store, evidence_id, candidate_id, attempt_ref = _setup(monkeypatch)
    try:
        observer = _Observer(store)
        result = WorkPackageOutputService(store, verifier=observer).verify(evidence_id)

        assert result.created is True
        assert result.candidate_id == candidate_id
        assert result.verification.evidence_id == evidence_id
        assert result.verification.attempt_head_sha == HEAD_SHA
        assert result.verification.changed_paths == ["src/feature.py"]
        assert result.verification.receipt_digest.startswith("sha256:")
        assert observer.calls == [
            (
                {"id": "repo_mac", "source": REPOSITORY_SOURCE},
                {
                    "attempt_ref": attempt_ref,
                    "base_sha": BASE_SHA,
                    "attempt_base_ref": "refs/heads/main",
                    "declared_effects": {
                        "reads": [],
                        "writes": ["src"],
                        "exclusive": [],
                        "external": [],
                        "external_contract": {},
                    },
                    "resource_namespace": {
                        "case_sensitive": True,
                        "conflict_policy": "exact",
                        "status": "resolved",
                        "symlink_resolution": "resolved",
                        "unicode_normalization": "NFC",
                    },
                },
            )
        ]
        attribution = store.query_one(
            "SELECT controller_verified FROM evidence_attempt_links "
            "WHERE evidence_id = ?",
            (evidence_id,),
        )
        assert attribution["controller_verified"] == 0
        assert (
            store.query_one("SELECT COUNT(*) AS n FROM evidence_attempt_verifications")[
                "n"
            ]
            == 1
        )
        assert store.query_all("PRAGMA foreign_key_check") == []
    finally:
        store.close()


def test_retry_is_idempotent_without_second_repository_observation(monkeypatch) -> None:
    store, evidence_id, _candidate_id, _attempt_ref = _setup(monkeypatch)
    try:
        observer = _Observer(store)
        service = WorkPackageOutputService(store, verifier=observer)
        first = service.verify(evidence_id)
        second = service.verify(evidence_id)

        assert first.created is True
        assert second.created is False
        assert second.verification.id == first.verification.id
        assert len(observer.calls) == 1
        assert (
            store.query_one("SELECT COUNT(*) AS n FROM evidence_attempt_verifications")[
                "n"
            ]
            == 1
        )
    finally:
        store.close()


def test_fails_closed_when_worker_head_disagrees_with_observation(monkeypatch) -> None:
    store, evidence_id, _candidate_id, _attempt_ref = _setup(monkeypatch)
    try:
        observer = _Observer(store, head_sha="e" * 40)
        with pytest.raises(ValidationError, match="exact attributed attempt"):
            WorkPackageOutputService(store, verifier=observer).verify(evidence_id)
        assert (
            store.query_one("SELECT COUNT(*) AS n FROM evidence_attempt_verifications")[
                "n"
            ]
            == 0
        )
    finally:
        store.close()


def test_fails_closed_when_worker_did_not_declare_exact_head(monkeypatch) -> None:
    store, evidence_id, _candidate_id, _attempt_ref = _setup(
        monkeypatch, attempt_head_sha=None
    )
    try:
        observer = _Observer(store)
        with pytest.raises(ValidationError, match="exact protected-ref head"):
            WorkPackageOutputService(store, verifier=observer).verify(evidence_id)
        assert observer.calls == []
        assert (
            store.query_one("SELECT COUNT(*) AS n FROM evidence_attempt_verifications")[
                "n"
            ]
            == 0
        )
    finally:
        store.close()


def test_fails_closed_when_attempt_is_not_attributed_as_protected(monkeypatch) -> None:
    store, evidence_id, _candidate_id, _attempt_ref = _setup(
        monkeypatch, protected_ref=False
    )
    try:
        observer = _Observer(store)
        with pytest.raises(ValidationError, match="attributed protected ref"):
            WorkPackageOutputService(store, verifier=observer).verify(evidence_id)
        assert observer.calls == []
        assert (
            store.query_one("SELECT COUNT(*) AS n FROM evidence_attempt_verifications")[
                "n"
            ]
            == 0
        )
    finally:
        store.close()


def test_repository_registry_drift_during_observation_aborts_receipt(
    monkeypatch,
) -> None:
    store, evidence_id, _candidate_id, _attempt_ref = _setup(monkeypatch)
    try:

        def change_authoritative_source() -> None:
            changed_contract = _repository_contract()
            changed_contract["canonical_remote_url"] = (
                "ssh://git@example.invalid/replaced.git"
            )
            store.execute(
                "UPDATE project_repositories SET metadata = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    json.dumps({"repository_contract": changed_contract}),
                    "changed",
                    "repo_mac",
                ),
            )

        observer = _Observer(store, mutate=change_authoritative_source)
        with pytest.raises(TransitionError, match="context changed"):
            WorkPackageOutputService(store, verifier=observer).verify(evidence_id)
        assert observer.calls[0][0]["source"] == REPOSITORY_SOURCE
        assert (
            store.query_one("SELECT COUNT(*) AS n FROM evidence_attempt_verifications")[
                "n"
            ]
            == 0
        )
    finally:
        store.close()


def test_package_epoch_drift_during_observation_aborts_receipt(monkeypatch) -> None:
    store, evidence_id, _candidate_id, _attempt_ref = _setup(monkeypatch)
    try:

        def pause_package() -> None:
            store.execute(
                "UPDATE work_packages SET state = ?, updated_at = ? WHERE id = ?",
                ("paused", "paused", "wp_output_service"),
            )

        observer = _Observer(store, mutate=pause_package)
        with pytest.raises(TransitionError, match="current active epoch"):
            WorkPackageOutputService(store, verifier=observer).verify(evidence_id)
        assert (
            store.query_one("SELECT COUNT(*) AS n FROM evidence_attempt_verifications")[
                "n"
            ]
            == 0
        )
    finally:
        store.close()


def test_observer_exception_text_is_not_exposed(monkeypatch) -> None:
    store, evidence_id, _candidate_id, _attempt_ref = _setup(monkeypatch)
    try:
        observer = _Observer(
            store,
            error=RuntimeError(
                "fatal: https://worker:do-not-leak@example.invalid/private.git"
            ),
        )
        with pytest.raises(ValidationError) as raised:
            WorkPackageOutputService(store, verifier=observer).verify(evidence_id)
        assert str(raised.value) == "controller attempt output observation failed"
        assert "do-not-leak" not in str(raised.value)
        assert (
            store.query_one("SELECT COUNT(*) AS n FROM evidence_attempt_verifications")[
                "n"
            ]
            == 0
        )
    finally:
        store.close()
