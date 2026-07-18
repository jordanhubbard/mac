from __future__ import annotations

import copy
import hashlib
import json

import pytest

from mac.models import TransitionError, ValidationError, json_loads
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
from mac.work_package_pipeline_runtime import WorkPackagePipelineRuntimeConfig
from mac.work_package_replan_service import WorkPackageReplanService
from mac.work_package_service import RepositoryBaseAttestation, WorkPackageService
from tests.certifier_phase_profile_fixtures import mac_phase_profile


BASE_SHA = "a" * 40
NEXT_SHA = "b" * 40

_CERTIFIER_POLICY_TEXT = """\
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


class _RepositoryVerifier:
    def __init__(self) -> None:
        self.calls = []

    def verify(self, repository, *, planning_base_ref, planning_base_sha):
        self.calls.append((dict(repository), planning_base_ref, planning_base_sha))
        return RepositoryBaseAttestation(
            repository_id=repository["id"],
            planning_base_ref=planning_base_ref,
            planning_base_sha=planning_base_sha,
            canonical_ref_sha=planning_base_sha,
            source_kind="test",
            verified_at="attested",
            resource_namespace={"status": "unresolved"},
        )


class _OutputObserver:
    def observe(self, repository, **kwargs):
        return AttemptOutputObservation(
            repository_id=repository["id"],
            attempt_ref=kwargs["attempt_ref"],
            base_sha=kwargs["base_sha"],
            head_sha="c" * 40,
            tree_digest="sha256:" + "d" * 64,
            observed_effects_digest="sha256:" + "e" * 64,
            changes=(AttemptPathChange(status="A", path="src/feature.py"),),
            changed_paths=("src/feature.py",),
            verifier=WORK_PACKAGE_OUTPUT_VERIFIER_VERSION,
            verified_at="2026-07-17T12:00:00+00:00",
        )


def _register_repository(store: SQLiteStore) -> None:
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "repo_replan",
            "mac",
            "/controller/mac",
            "ssh://git@example.invalid/mac.git",
            "mac",
            "[]",
            1,
            60,
            "{}",
            "created",
            "registry-v1",
        ),
    )


def _plan(
    *,
    package_id: str = "wp_replan",
    generation: int = 1,
    base_sha: str = BASE_SHA,
) -> dict:
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": package_id,
        "goal": "Build, assemble, and certify one exact product",
        "project": "mac",
        "repository_id": "repo_replan",
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": base_sha,
        "plan_generation": generation,
        "max_in_flight": 2,
        "nodes": [
            {
                "node_key": "foundation",
                "title": "Build foundation",
                "description": "Implement the foundation",
                "node_type": "mutation",
                "effects": {"writes": ["src/foundation"]},
                "expected_outputs": ["foundation-candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            },
            {
                "node_key": "parallel",
                "title": "Build independent component",
                "description": "Implement an independent component",
                "node_type": "mutation",
                "effects": {"writes": ["src/parallel"]},
                "expected_outputs": ["parallel-candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            },
            {
                "node_key": "assemble",
                "title": "Assemble exact candidate",
                "node_type": "integration",
                "depends_on": ["foundation", "parallel"],
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


def _carry_plan(*, generation: int = 1, base_sha: str = BASE_SHA) -> dict:
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_replan_carry",
        "goal": "Produce one exact mutation candidate",
        "project": "mac",
        "repository_id": "repo_replan",
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": base_sha,
        "plan_generation": generation,
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
                "title": "Assemble exact candidate",
                "node_type": "integration",
                "depends_on": ["change"],
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


def _new_store() -> SQLiteStore:
    store = SQLiteStore(":memory:")
    _register_repository(store)
    return store


def _configure_ready_downstream(
    store: SQLiteStore,
    control: ControlPlane,
    *,
    bundle_dir,
) -> None:
    """Install the same real metadata/config gate required by live claims."""

    policy_id = "replan-test-policy"
    repository_contract = {
        "canonical_remote_url": "ssh://git@example.invalid/mac.git",
        "landing_certification_policy_id": policy_id,
        "work_package_certification": {
            "schema": CERTIFICATION_CONTRACT_SCHEMA,
            "policy": {
                "policy_id": policy_id,
                "version": 1,
                "checksum": "sha256:"
                + hashlib.sha256(_CERTIFIER_POLICY_TEXT.encode("utf-8")).hexdigest(),
            },
            "policy_text": _CERTIFIER_POLICY_TEXT,
            "phase_profile": mac_phase_profile(),
            "image_ref": "registry.invalid/mac-certifier@sha256:" + "c" * 64,
            "controller_commands": [
                {
                    "command_id": "contract-tests",
                    "argv": ["/opt/mac-certifier/bin/run-contract-tests"],
                    "timeout_seconds": 900,
                }
            ],
        },
    }
    store.execute(
        "UPDATE project_repositories SET metadata = ? WHERE id = ?",
        (json.dumps({"repository_contract": repository_contract}), "repo_replan"),
    )
    control.work_package_pipeline_runtime_config = (
        WorkPackagePipelineRuntimeConfig.from_env(
            {
                "MAC_WORK_PACKAGE_PIPELINE_ENABLED": "true",
                "MAC_WORK_PACKAGE_LANDING_ENABLED": "true",
                "MAC_WORK_PACKAGE_BUNDLE_DIR": str(bundle_dir),
            }
        )
    )
    control.work_package_certifications.validate_runtime_binding = lambda: None
    assert control.work_package_pipeline_runtime_config.enabled is True


def test_replan_cannot_introduce_unfenced_external_effect() -> None:
    store = _new_store()
    try:
        _admit_active(store, _plan())
        replacement = _plan(generation=2, base_sha=NEXT_SHA)
        replacement["nodes"][0]["effects"] = {
            "external": ["github:release"],
            "external_contract": {
                "idempotency_key": "release-wp-replan",
                "exclusive": True,
            },
        }
        with pytest.raises(
            ValidationError,
            match="controller-owned fenced effector",
        ):
            WorkPackageReplanService(
                store,
                repository_verifier=_RepositoryVerifier(),
            ).propose(
                replacement,
                package_id="wp_replan",
                expected_plan_version=1,
                expected_epoch=1,
                actor="planner",
                reason="unsafe external action",
            )
        assert (
            store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_plan_versions "
                "WHERE package_id = ?",
                ("wp_replan",),
            )["n"]
            == 1
        )
    finally:
        store.close()


def _admit_active(store: SQLiteStore, plan: dict) -> None:
    service = WorkPackageService(store, repository_verifier=_RepositoryVerifier())
    admitted = service.admit(plan, actor="planner", reason="initial plan")
    service.activate(
        admitted.package.id,
        expected_plan_version=1,
        expected_epoch=1,
        actor="operator",
    )


def _proposal(
    service: WorkPackageReplanService,
    plan: dict,
    *,
    package_id: str = "wp_replan",
):
    return service.propose(
        plan,
        package_id=package_id,
        expected_plan_version=1,
        expected_epoch=1,
        actor="replan-controller",
        reason="Andon correction",
    )


def _pause(service: WorkPackageReplanService, package_id: str = "wp_replan") -> None:
    service.pause(
        package_id,
        expected_plan_version=1,
        expected_epoch=1,
        actor="operator",
        reason="inspect revised DAG",
    )


def test_preview_computes_changed_node_descendant_cone() -> None:
    store = _new_store()
    try:
        _admit_active(store, _plan())
        replacement = _plan(generation=2)
        replacement["nodes"][0]["description"] = "Use the corrected foundation contract"
        service = WorkPackageReplanService(
            store, repository_verifier=_RepositoryVerifier()
        )
        proposal = _proposal(service, replacement)

        before_pause = service.preview(proposal)
        assert before_pause.can_apply is False
        assert "package must be paused" in " ".join(before_pause.blockers)

        _pause(service)
        preview = service.preview(proposal)
        assert preview.can_apply is True
        assert preview.affected_node_keys == ("assemble", "certify", "foundation")
        assert preview.invalidated_node_keys == (
            "assemble",
            "certify",
            "foundation",
        )
        assert "parallel" not in preview.affected_node_keys
        assert {item.node_key for item in preview.carry_decisions} == {
            "foundation",
            "parallel",
            "assemble",
            "certify",
        }
    finally:
        store.close()


def test_apply_materializes_new_epoch_and_preserves_immutable_old_rows() -> None:
    store = _new_store()
    try:
        original = _plan()
        _admit_active(store, original)
        old_plan = store.query_one(
            "SELECT * FROM work_package_plan_versions WHERE package_id = ? "
            "AND version = ?",
            ("wp_replan", 1),
        )
        old_links = [
            dict(row)
            for row in store.query_all(
                "SELECT * FROM work_package_task_links WHERE package_id = ? "
                "AND epoch = ? ORDER BY node_key",
                ("wp_replan", 1),
            )
        ]
        replacement = _plan(generation=2)
        replacement["nodes"][0]["description"] = "Corrected foundation contract"
        service = WorkPackageReplanService(
            store, repository_verifier=_RepositoryVerifier()
        )
        proposal = _proposal(service, replacement)
        _pause(service)

        result = service.apply(proposal, expected_plan_version=1, expected_epoch=1)

        assert result.created is True
        assert result.plan_version == 2
        assert result.epoch == 2
        assert result.state == "paused"
        assert len(result.task_ids) == 4
        package = store.query_one(
            "SELECT * FROM work_packages WHERE id = ?", ("wp_replan",)
        )
        assert (
            package["state"],
            package["current_plan_version"],
            package["current_epoch"],
        ) == (
            "paused",
            2,
            2,
        )
        epochs = store.query_all(
            "SELECT epoch, status FROM work_package_epochs WHERE package_id = ? "
            "ORDER BY epoch",
            ("wp_replan",),
        )
        assert [(row["epoch"], row["status"]) for row in epochs] == [
            (1, "superseded"),
            (2, "active"),
        ]
        current_old_plan = store.query_one(
            "SELECT * FROM work_package_plan_versions WHERE package_id = ? "
            "AND version = ?",
            ("wp_replan", 1),
        )
        assert dict(current_old_plan) == dict(old_plan)
        current_old_links = store.query_all(
            "SELECT * FROM work_package_task_links WHERE package_id = ? "
            "AND epoch = ? ORDER BY node_key",
            ("wp_replan", 1),
        )
        assert all(row["node_state"] == "superseded" for row in current_old_links)
        for before, after in zip(old_links, current_old_links):
            assert {key: after[key] for key in after.keys() if key != "node_state"} == {
                key: before[key] for key in before.keys() if key != "node_state"
            }
        new_links = store.query_all(
            "SELECT link.*, task.metadata FROM work_package_task_links AS link "
            "JOIN tasks AS task ON task.id = link.task_id "
            "WHERE link.package_id = ? AND link.epoch = ? ORDER BY link.node_key",
            ("wp_replan", 2),
        )
        assert all(row["node_state"] == "planned" for row in new_links)
        assert all(json_loads(row["metadata"], {})["no_dispatch"] for row in new_links)
        lineage = store.query_all(
            "SELECT * FROM work_package_node_lineage WHERE package_id = ? "
            "ORDER BY from_node_key",
            ("wp_replan",),
        )
        assert len(lineage) == 4
        relations = {row["from_node_key"]: row["relation"] for row in lineage}
        assert relations == {
            "assemble": "invalidated",
            "certify": "invalidated",
            "foundation": "invalidated",
            "parallel": "replaced",
        }
        assert store.query_all("PRAGMA foreign_key_check") == []
    finally:
        store.close()


def test_apply_uses_epoch_cas_and_rejects_a_competing_plan() -> None:
    store = _new_store()
    try:
        _admit_active(store, _plan())
        service = WorkPackageReplanService(
            store, repository_verifier=_RepositoryVerifier()
        )
        first_plan = _plan(generation=2)
        first_plan["nodes"][0]["description"] = "First correction"
        second_plan = _plan(generation=2)
        second_plan["nodes"][1]["description"] = "Competing correction"
        first = _proposal(service, first_plan)
        second = _proposal(service, second_plan)
        _pause(service)
        service.apply(first, expected_plan_version=1, expected_epoch=1)

        with pytest.raises(TransitionError, match="different replan|incoherent"):
            service.apply(second, expected_plan_version=1, expected_epoch=1)
        assert (
            store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_plan_versions "
                "WHERE package_id = ?",
                ("wp_replan",),
            )["n"]
            == 2
        )
    finally:
        store.close()


def test_apply_rechecks_the_exact_registered_repository_identity() -> None:
    store = _new_store()
    try:
        _admit_active(store, _plan())
        verifier = _RepositoryVerifier()
        service = WorkPackageReplanService(store, repository_verifier=verifier)
        proposal = _proposal(service, _plan(generation=2))
        repository = dict(
            store.query_one(
                "SELECT * FROM project_repositories WHERE id = ?", ("repo_replan",)
            )
        )
        assert verifier.calls == [
            (
                repository,
                "refs/heads/main",
                BASE_SHA,
            )
        ]
        _pause(service)
        store.execute(
            "UPDATE project_repositories SET updated_at = ? WHERE id = ?",
            ("registry-v2", "repo_replan"),
        )

        with pytest.raises(TransitionError, match="registered repository changed"):
            service.apply(proposal, expected_plan_version=1, expected_epoch=1)
        assert len(verifier.calls) == 1
        assert (
            store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_plan_versions "
                "WHERE package_id = ?",
                ("wp_replan",),
            )["n"]
            == 1
        )
    finally:
        store.close()


def test_carry_forward_is_refused_without_base_delta_authority(
    monkeypatch, tmp_path
) -> None:
    store = _new_store()
    try:
        _admit_active(store, _carry_plan())
        control = ControlPlane(store, secret_key="replan-carry-secret-key-value-0001")
        _configure_ready_downstream(store, control, bundle_dir=tmp_path / "bundles")
        machine = control.register_machine("replan-worker-host")
        agent = control.register_agent(
            machine.id, "replan-worker", capabilities=["work_package_v1"]
        )
        monkeypatch.setattr(
            "mac.worker_credentials.assert_package_worker_ready",
            lambda conn, agent_id: {"ready": True, "agent_id": agent_id},
        )
        task_id = store.query_one(
            "SELECT task_id FROM work_package_task_links "
            "WHERE package_id = ? AND node_key = ?",
            ("wp_replan_carry", "change"),
        )["task_id"]
        claimed, lease = control.claim_task(task_id, agent.id, sync_beads=False)
        control.start_task(task_id, agent.id, lease_id=lease.id, drain_outbox=False)
        assignment = store.query_one(
            "SELECT * FROM work_package_assignment_audit WHERE lease_id = ?",
            (lease.id,),
        )
        store.execute(
            "INSERT INTO evidence ("
            "id, task_id, kind, uri, summary, checksum, metadata, created_by, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ev_replan_carry",
                task_id,
                "artifact",
                "artifact://candidate",
                "candidate",
                None,
                json.dumps({"verification": {"evidence_type": "repository_change"}}),
                agent.id,
                "now",
            ),
        )
        store.execute(
            "INSERT INTO evidence_attempt_links ("
            "evidence_id, task_id, lease_id, agent_id, attempt_number, attempt_ref, "
            "attempt_base_sha, attempt_head_sha, declared_effects_digest, protected_ref, "
            "created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ev_replan_carry",
                task_id,
                lease.id,
                agent.id,
                claimed.attempt_count,
                assignment["attempt_ref"],
                assignment["attempt_base_sha"],
                "c" * 40,
                assignment["declared_effects_digest"],
                1,
                "now",
            ),
        )
        candidate = (
            WorkPackageCandidateService(store)
            .submit("ev_replan_carry", actor="candidate-controller")
            .candidate
        )
        WorkPackageOutputService(store, verifier=_OutputObserver()).verify(
            "ev_replan_carry"
        )
        store.execute(
            "UPDATE work_package_node_candidates SET status = ?, accepted_at = ?, "
            "accepted_by = ? WHERE id = ? AND status = ?",
            ("accepted", "accepted", "review-controller", candidate.id, "submitted"),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ? "
            "AND node_state = ?",
            ("candidate_accepted", task_id, "candidate_submitted"),
        )
        store.execute(
            "UPDATE leases SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            ("released", "released", lease.id, "active"),
        )
        store.execute(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
            ("needs_review", "reviewed", task_id, "running"),
        )

        service = WorkPackageReplanService(
            store, repository_verifier=_RepositoryVerifier()
        )
        proposal = _proposal(
            service,
            _carry_plan(generation=2, base_sha=NEXT_SHA),
            package_id="wp_replan_carry",
        )
        _pause(service, package_id="wp_replan_carry")
        preview = service.preview(proposal)

        assert preview.can_apply is True
        decision = next(
            item for item in preview.carry_decisions if item.node_key == "change"
        )
        assert decision.status == "blocked"
        assert decision.reason == "authoritative base-delta verifier is unavailable"
        assert decision.checks["accepted_candidate"] is True
        assert decision.checks["output_receipt"] is True
        assert decision.checks["schema_can_materialize_exact_candidate"] is False

        result = service.apply(proposal, expected_plan_version=1, expected_epoch=1)
        assert result.created is True
        lineage = store.query_one(
            "SELECT * FROM work_package_node_lineage "
            "WHERE package_id = ? AND from_node_key = ?",
            ("wp_replan_carry", "change"),
        )
        assert lineage["relation"] == "invalidated"
        assert lineage["source_evidence_id"] is None
        assert (
            store.query_one(
                "SELECT status FROM work_package_node_candidates WHERE id = ?",
                (candidate.id,),
            )["status"]
            == "accepted"
        )
    finally:
        store.close()


@pytest.mark.parametrize("failure_stage", ["tasks_materialized", "lineage_written"])
def test_replan_transaction_rolls_back_and_retry_is_idempotent(
    failure_stage: str,
) -> None:
    store = _new_store()
    try:
        _admit_active(store, _plan())
        replacement = copy.deepcopy(_plan(generation=2))
        replacement["nodes"][0]["description"] = "Atomic correction"

        def fail(stage: str) -> None:
            if stage == failure_stage:
                raise RuntimeError("simulated controller crash")

        crashing = WorkPackageReplanService(
            store,
            repository_verifier=_RepositoryVerifier(),
            failure_injector=fail,
        )
        proposal = _proposal(crashing, replacement)
        _pause(crashing)
        with pytest.raises(RuntimeError, match="simulated controller crash"):
            crashing.apply(proposal, expected_plan_version=1, expected_epoch=1)

        package = store.query_one(
            "SELECT * FROM work_packages WHERE id = ?", ("wp_replan",)
        )
        assert (
            package["state"],
            package["current_plan_version"],
            package["current_epoch"],
        ) == (
            "paused",
            1,
            1,
        )
        assert (
            store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_plan_versions "
                "WHERE package_id = ?",
                ("wp_replan",),
            )["n"]
            == 1
        )
        assert (
            store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_task_links "
                "WHERE package_id = ?",
                ("wp_replan",),
            )["n"]
            == 4
        )
        assert (
            store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_node_lineage "
                "WHERE package_id = ?",
                ("wp_replan",),
            )["n"]
            == 0
        )

        healthy = WorkPackageReplanService(
            store, repository_verifier=_RepositoryVerifier()
        )
        first = healthy.apply(proposal, expected_plan_version=1, expected_epoch=1)
        second = healthy.apply(proposal, expected_plan_version=1, expected_epoch=1)
        assert first.created is True
        assert second.created is False
        assert first.task_ids == second.task_ids
        assert (
            store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_plan_versions "
                "WHERE package_id = ?",
                ("wp_replan",),
            )["n"]
            == 2
        )
        assert (
            store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_task_links "
                "WHERE package_id = ?",
                ("wp_replan",),
            )["n"]
            == 8
        )
        assert (
            store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_node_lineage "
                "WHERE package_id = ?",
                ("wp_replan",),
            )["n"]
            == 4
        )
    finally:
        store.close()


def test_active_package_lease_blocks_replan_without_partial_fencing(
    monkeypatch, tmp_path
) -> None:
    store = _new_store()
    try:
        _admit_active(store, _carry_plan())
        control = ControlPlane(store, secret_key="replan-lease-secret-key-value-0001")
        _configure_ready_downstream(store, control, bundle_dir=tmp_path / "bundles")
        machine = control.register_machine("lease-host")
        agent = control.register_agent(
            machine.id, "lease-worker", capabilities=["work_package_v1"]
        )
        monkeypatch.setattr(
            "mac.worker_credentials.assert_package_worker_ready",
            lambda conn, agent_id: {"ready": True, "agent_id": agent_id},
        )
        task_id = store.query_one(
            "SELECT task_id FROM work_package_task_links "
            "WHERE package_id = ? AND node_key = ?",
            ("wp_replan_carry", "change"),
        )["task_id"]
        control.claim_task(task_id, agent.id, sync_beads=False)

        service = WorkPackageReplanService(
            store, repository_verifier=_RepositoryVerifier()
        )
        proposal = _proposal(
            service,
            _carry_plan(generation=2),
            package_id="wp_replan_carry",
        )
        _pause(service, package_id="wp_replan_carry")
        preview = service.preview(proposal)
        assert preview.can_apply is False
        assert "package-aware expiry finalizer" in " ".join(preview.blockers)

        with pytest.raises(TransitionError, match="expiry finalizer"):
            service.apply(proposal, expected_plan_version=1, expected_epoch=1)
        assert (
            store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_plan_versions "
                "WHERE package_id = ?",
                ("wp_replan_carry",),
            )["n"]
            == 1
        )
        assert (
            store.query_one(
                "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
                (task_id,),
            )["node_state"]
            == "executing"
        )
    finally:
        store.close()
