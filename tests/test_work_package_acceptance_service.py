from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from mac.models import ValidationError, utcnow
from mac.services import ControlPlane
from mac.store import Store
from mac.test_support import ephemeral_store
from mac.work_package_acceptance_service import WorkPackageAcceptanceService
from mac.work_package_models import WORK_PACKAGE_PLAN_SCHEMA
from mac.work_package_service import RepositoryBaseAttestation, WorkPackageService


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


@dataclass
class _CandidateFixture:
    store: Store
    cp: ControlPlane
    task_id: str
    downstream_task_id: str
    candidate_id: str
    evidence_id: str
    lease_id: str
    agent_id: str
    attempt_ref: str
    base_sha: str
    head_sha: str
    effects_digest: str


def _plan(*, max_cycles: int = 1, second_root: bool = False) -> dict:
    nodes = [
        {
            "node_key": "change",
            "title": "Change source",
            "node_type": "mutation",
            "effects": {"writes": ["src/change.py"]},
            "expected_outputs": ["component-candidate"],
            "verification": {"profile": "repository-default"},
            "rework": {"max_cycles": max_cycles},
            "estimates": {"confidence": "high"},
        }
    ]
    if second_root:
        nodes.append(
            {
                "node_key": "other",
                "title": "Change another component",
                "node_type": "mutation",
                "effects": {"writes": ["src/other.py"]},
                "expected_outputs": ["other-component-candidate"],
                "verification": {"profile": "repository-default"},
                "rework": {"max_cycles": max_cycles},
                "estimates": {"confidence": "high"},
            }
        )
    nodes.append(
        {
            "node_key": "assemble",
            "title": "Assemble exact candidate",
            "node_type": "integration",
            "depends_on": ["change", "other"] if second_root else ["change"],
            "inputs": ["component-candidate"],
            "expected_outputs": ["candidate-tree"],
            "verification": {"profile": "integration-default"},
            "estimates": {"confidence": "high"},
        }
    )
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_acceptance",
        "goal": "Accept an exact reviewed component before assembly",
        "project": "mac",
        "repository_id": "repo_mac",
        "resource_namespace": {
            "case_sensitive": True,
            "unicode_normalization": "NFC",
            "symlink_resolution": "resolved",
        },
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": "a" * 40,
        "plan_generation": 1,
        "mutation_wip": {
            "max_tokens": 2 if second_root else 1,
            "fan_in_reservation": True,
        },
        "nodes": nodes,
    }


def _setup_candidate(
    monkeypatch,
    *,
    max_cycles: int = 1,
    add_receipt: bool = True,
    receipt_head_sha: str | None = None,
    review_executor_evidence_id: str | None = None,
    review_head_sha: str | None = None,
    second_root: bool = False,
) -> _CandidateFixture:
    store = ephemeral_store()
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "repo_mac",
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
    work_packages = WorkPackageService(store, repository_verifier=_Verifier())
    admitted = work_packages.admit(
        _plan(max_cycles=max_cycles, second_root=second_root),
        actor="planner",
        reason="test",
    )
    work_packages.activate(
        admitted.package.id,
        expected_plan_version=1,
        expected_epoch=1,
        actor="operator",
    )
    task_by_node = {
        str(row["node_key"]): str(row["task_id"])
        for row in store.query_all(
            "SELECT node_key, task_id FROM work_package_task_links "
            "WHERE package_id = ? ORDER BY node_key",
            (admitted.package.id,),
        )
    }
    task_id = task_by_node["change"]
    downstream_task_id = task_by_node["assemble"]

    cp = ControlPlane(store, secret_key="acceptance-test-secret-key-value-0001")
    monkeypatch.setattr(
        cp,
        "_work_package_downstream_activation_readiness",
        lambda _described: {"ready": True, "code": "ready", "reason": ""},
    )
    monkeypatch.setattr(
        cp,
        "_work_package_downstream_release_gate",
        lambda *_args, **_kwargs: {"ready": True, "code": "ready", "reason": ""},
    )
    machine = cp.register_machine("acceptance-host")
    agent = cp.register_agent(
        machine.id,
        "acceptance-worker",
        capabilities=["work_package_v1"],
    )
    monkeypatch.setattr(
        "mac.worker_credentials.assert_package_worker_ready",
        lambda conn, agent_id: {"ready": True, "agent_id": agent_id},
    )
    claimed, lease = cp.claim_task(task_id, agent.id, sync_beads=False)
    cp.start_task(task_id, agent.id, lease_id=lease.id, drain_outbox=False)
    assignment = store.query_one(
        "SELECT * FROM work_package_assignment_audit WHERE lease_id = ?", (lease.id,)
    )
    base_sha = str(assignment["attempt_base_sha"])
    head_sha = "b" * 40
    evidence_id = "ev_executor"
    executor_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repository_change",
        "repo": {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "files_changed": ["src/change.py"],
        },
    }
    store.execute(
        "INSERT INTO evidence ("
        "id, task_id, kind, uri, summary, checksum, metadata, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id,
            task_id,
            "artifact",
            "artifact://executor",
            "executor candidate",
            None,
            json.dumps({"verification": executor_manifest}),
            agent.id,
            utcnow(),
        ),
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
            base_sha,
            head_sha,
            assignment["declared_effects_digest"],
            1,
            utcnow(),
        ),
    )
    evidence = cp.get_evidence(evidence_id)
    monkeypatch.setattr(cp, "_require_review_ready", lambda _task: evidence)
    reviewed = cp.submit_for_review(
        task_id,
        agent.id,
        lease_id=lease.id,
        drain_outbox=False,
    )
    assert reviewed.state == "needs_review"
    candidate = store.query_one(
        "SELECT * FROM work_package_node_candidates WHERE evidence_id = ?",
        (evidence_id,),
    )
    candidate_id = str(candidate["id"])

    if add_receipt:
        observed_head = receipt_head_sha or head_sha
        store.execute(
            "INSERT INTO evidence_attempt_verifications ("
            "id, evidence_id, task_id, lease_id, agent_id, attempt_number, "
            "repository_id, attempt_ref, attempt_base_sha, attempt_head_sha, "
            "tree_digest, declared_effects_digest, observed_effects_digest, "
            "changed_paths, changes, verifier, verifier_version, verified_at, "
            "receipt_digest"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "wpverify_executor",
                evidence_id,
                task_id,
                lease.id,
                agent.id,
                claimed.attempt_count,
                "repo_mac",
                assignment["attempt_ref"],
                base_sha,
                observed_head,
                "sha256:" + "1" * 64,
                assignment["declared_effects_digest"],
                "sha256:" + "2" * 64,
                json.dumps(["src/change.py"]),
                json.dumps([{"status": "M", "path": "src/change.py"}]),
                "test-controller",
                "v1",
                utcnow(),
                "sha256:" + "3" * 64,
            ),
        )

    review_at = utcnow()
    verdict_at = utcnow()
    reviewed_evidence_id = review_executor_evidence_id or evidence_id
    verdict_head = review_head_sha or head_sha
    verdict_manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "review_verdict",
        "verdict": "approved",
        "reviewed_evidence_id": reviewed_evidence_id,
        "signed_by": "reviewer",
        "signature": "signed-test-verdict",
        "worktree_digest": "sha256:" + "4" * 64,
        "checks": [{"name": "review", "returncode": 0}],
        "repo": {"base_sha": base_sha, "head_sha": verdict_head},
    }
    store.execute(
        "INSERT INTO evidence ("
        "id, task_id, kind, uri, summary, checksum, metadata, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ev_verdict",
            task_id,
            "review",
            "artifact://verdict",
            "review verdict",
            None,
            json.dumps({"verification": verdict_manifest}),
            "reviewer",
            verdict_at,
        ),
    )
    store.execute(
        "INSERT INTO reviews ("
        "id, task_id, reviewer_agent_id, status, reason, evidence_id, "
        "created_at, completed_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "review_current",
            task_id,
            "reviewer",
            "approved",
            "exact candidate approved",
            "ev_verdict",
            review_at,
            verdict_at,
        ),
    )
    store.execute("UPDATE tasks SET state = ? WHERE id = ?", ("reviewing", task_id))
    return _CandidateFixture(
        store=store,
        cp=cp,
        task_id=task_id,
        downstream_task_id=downstream_task_id,
        candidate_id=candidate_id,
        evidence_id=evidence_id,
        lease_id=lease.id,
        agent_id=agent.id,
        attempt_ref=str(assignment["attempt_ref"]),
        base_sha=base_sha,
        head_sha=head_sha,
        effects_digest=str(assignment["declared_effects_digest"]),
    )


def test_acceptance_requires_append_only_exact_output_receipt(monkeypatch) -> None:
    fixture = _setup_candidate(monkeypatch, add_receipt=False)
    try:
        with pytest.raises(ValidationError, match="append-only output verification"):
            WorkPackageAcceptanceService(fixture.store).accept(
                fixture.candidate_id, actor="acceptance-controller"
            )
        assert fixture.store.query_one(
            "SELECT status FROM work_package_node_candidates WHERE id = ?",
            (fixture.candidate_id,),
        )["status"] == "submitted"
    finally:
        fixture.store.close()


def test_acceptance_rejects_receipt_or_review_for_a_different_exact_head(
    monkeypatch,
) -> None:
    fixture = _setup_candidate(monkeypatch, receipt_head_sha="c" * 40)
    try:
        with pytest.raises(ValidationError, match="controller observation"):
            WorkPackageAcceptanceService(fixture.store).accept(
                fixture.candidate_id, actor="acceptance-controller"
            )
    finally:
        fixture.store.close()


def test_acceptance_rejects_stale_approved_review(monkeypatch) -> None:
    fixture = _setup_candidate(
        monkeypatch,
        review_executor_evidence_id="ev_from_old_attempt",
    )
    try:
        with pytest.raises(ValidationError, match="exactly match candidate evidence"):
            WorkPackageAcceptanceService(fixture.store).accept(
                fixture.candidate_id, actor="acceptance-controller"
            )
    finally:
        fixture.store.close()


def test_exact_acceptance_is_atomic_idempotent_and_releases_downstream(
    monkeypatch,
) -> None:
    fixture = _setup_candidate(monkeypatch)
    try:
        service = WorkPackageAcceptanceService(fixture.store)
        first = service.accept(fixture.candidate_id, actor="acceptance-controller")

        assert first.created is True
        assert first.status == "accepted"
        assert first.verification_receipt_id == "wpverify_executor"
        assert first.review_id == "review_current"
        assert first.released_downstream_task_ids == (fixture.downstream_task_id,)
        assert fixture.store.query_one(
            "SELECT status FROM work_package_node_candidates WHERE id = ?",
            (fixture.candidate_id,),
        )["status"] == "accepted"
        assert fixture.store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (fixture.task_id,),
        )["node_state"] == "candidate_accepted"
        assert fixture.store.query_one(
            "SELECT state FROM tasks WHERE id = ?", (fixture.task_id,)
        )["state"] == "completed"

        downstream = fixture.store.query_one(
            "SELECT state, metadata FROM tasks WHERE id = ?",
            (fixture.downstream_task_id,),
        )
        assert downstream["state"] == "waiting"
        assert json.loads(downstream["metadata"])["no_dispatch"] is True
        assert fixture.store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (fixture.downstream_task_id,),
        )["node_state"] == "ready"
        ready_history = fixture.store.query_one(
            "SELECT detail FROM task_history WHERE task_id = ? "
            "AND event_type = 'work_package.controller_station_ready'",
            (fixture.downstream_task_id,),
        )
        assert json.loads(ready_history["detail"])["dispatch_mode"] == "controller_station"

        buffered = fixture.store.query_all(
            "SELECT state, released_at, release_reason FROM work_package_wip_tokens "
            "WHERE task_id = ? AND stage = ? ORDER BY id",
            (fixture.task_id, "candidate_buffer"),
        )
        reservations = fixture.store.query_all(
            "SELECT id, state, predecessor_token_id FROM work_package_wip_tokens "
            "WHERE task_id = ? AND stage = ? ORDER BY id",
            (fixture.task_id, "fan_in_reservation"),
        )
        assert buffered and all(row["state"] == "released" for row in buffered)
        assert all(row["released_at"] for row in buffered)
        assert all(json.loads(row["release_reason"])["decision"] == "accepted" for row in buffered)
        assert reservations and all(row["state"] == "held" for row in reservations)
        assert tuple(row["id"] for row in reservations) == first.transferred_wip_token_ids

        histories_before = fixture.store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_history WHERE package_id = ?",
            ("wp_acceptance",),
        )["n"]
        second = service.accept(fixture.candidate_id, actor="acceptance-controller")
        assert second.created is False
        assert second.candidate_id == first.candidate_id
        assert second.transferred_wip_token_ids == first.transferred_wip_token_ids
        assert fixture.store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_history WHERE package_id = ?",
            ("wp_acceptance",),
        )["n"] == histories_before
    finally:
        fixture.store.close()


def test_default_review_workflow_verifies_and_accepts_package_candidate(
    monkeypatch,
) -> None:
    fixture = _setup_candidate(monkeypatch)
    try:
        executor_evidence = fixture.cp.get_evidence(fixture.evidence_id)
        monkeypatch.setattr(
            fixture.cp,
            "_bound_review_evidence",
            lambda _task: (
                executor_evidence,
                {"valid": True, "evidence_type": "repository_change"},
            ),
        )
        result = fixture.cp.advance_default_review_workflow(fixture.task_id)

        assert result["status"] == "package_candidate_accepted"
        assert result["verification"]["candidate_id"] == fixture.candidate_id
        assert result["verification"]["created"] is False
        assert result["acceptance"]["candidate_id"] == fixture.candidate_id
        assert result["acceptance"]["status"] == "accepted"
        assert fixture.store.query_one(
            "SELECT state FROM tasks WHERE id = ?", (fixture.task_id,)
        )["state"] == "completed"
        assert fixture.store.query_one(
            "SELECT status FROM work_package_node_candidates WHERE id = ?",
            (fixture.candidate_id,),
        )["status"] == "accepted"
    finally:
        fixture.store.close()


def test_default_review_workflow_waits_when_exact_output_cannot_be_verified(
    monkeypatch,
) -> None:
    fixture = _setup_candidate(monkeypatch, add_receipt=False)
    try:
        executor_evidence = fixture.cp.get_evidence(fixture.evidence_id)
        monkeypatch.setattr(
            fixture.cp,
            "_bound_review_evidence",
            lambda _task: (
                executor_evidence,
                {"valid": True, "evidence_type": "repository_change"},
            ),
        )
        def unavailable(*_args, **_kwargs):
            raise RuntimeError("untrusted repository observation detail")

        monkeypatch.setattr(
            fixture.cp.work_package_outputs.verifier,
            "observe",
            unavailable,
        )
        result = fixture.cp.advance_default_review_workflow(fixture.task_id)

        assert result["status"] == "approved_waiting_for_package_output_verification"
        assert result["candidate_id"] == fixture.candidate_id
        assert result["error_class"] == "ValidationError"
        assert result["problem"] == "controller attempt output observation failed"
        assert fixture.store.query_one(
            "SELECT state FROM tasks WHERE id = ?", (fixture.task_id,)
        )["state"] == "reviewing"
        assert fixture.store.query_one(
            "SELECT status FROM work_package_node_candidates WHERE id = ?",
            (fixture.candidate_id,),
        )["status"] == "submitted"
    finally:
        fixture.store.close()


def test_candidate_submission_records_best_effort_verification_failure(
    monkeypatch,
) -> None:
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("must not leak this repository diagnostic")

    monkeypatch.setattr(
        "mac.work_package_output.GitAttemptOutputVerifier.observe",
        unavailable,
    )
    fixture = _setup_candidate(monkeypatch, add_receipt=False)
    try:
        event = fixture.store.query_one(
            "SELECT detail FROM observability_events WHERE name = ? "
            "AND subject_id = ? ORDER BY created_at DESC LIMIT 1",
            (
                "workflow.default_review.package_output_verification_failed",
                fixture.task_id,
            ),
        )
        assert event is not None
        detail = json.loads(event["detail"])
        assert detail["trigger"] == "candidate_submission"
        assert detail["candidate_id"] == fixture.candidate_id
        assert detail["error_class"] == "ValidationError"
        assert detail["error"] == "controller attempt output observation failed"
        assert "must not leak" not in event["detail"]
    finally:
        fixture.store.close()


def test_downstream_fan_in_waits_for_every_exact_predecessor_candidate(
    monkeypatch,
) -> None:
    fixture = _setup_candidate(monkeypatch, second_root=True)
    try:
        result = WorkPackageAcceptanceService(fixture.store).accept(
            fixture.candidate_id, actor="acceptance-controller"
        )

        assert result.status == "accepted"
        assert result.released_downstream_task_ids == ()
        downstream = fixture.store.query_one(
            "SELECT state, metadata FROM tasks WHERE id = ?",
            (fixture.downstream_task_id,),
        )
        assert downstream["state"] == "waiting"
        assert json.loads(downstream["metadata"])["no_dispatch"] is True
        assert fixture.store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (fixture.downstream_task_id,),
        )["node_state"] == "planned"
    finally:
        fixture.store.close()


@pytest.mark.parametrize(
    ("max_cycles", "expected_state", "retry_staged", "remaining"),
    [
        (1, "open", True, 1),
        (0, "blocked", False, 0),
    ],
)
def test_rejection_is_bounded_and_pauses_for_andon(
    monkeypatch,
    max_cycles: int,
    expected_state: str,
    retry_staged: bool,
    remaining: int,
) -> None:
    fixture = _setup_candidate(monkeypatch, max_cycles=max_cycles, add_receipt=False)
    try:
        service = WorkPackageAcceptanceService(fixture.store)
        first = service.reject(
            fixture.candidate_id,
            actor="acceptance-controller",
            reason="candidate failed exact review",
        )

        assert first.created is True
        assert first.retry_staged is retry_staged
        assert first.remaining_rework_cycles == remaining
        assert fixture.store.query_one(
            "SELECT state FROM work_packages WHERE id = ?", ("wp_acceptance",)
        )["state"] == "paused"
        task = fixture.store.query_one(
            "SELECT state, metadata FROM tasks WHERE id = ?", (fixture.task_id,)
        )
        assert task["state"] == expected_state
        metadata = json.loads(task["metadata"])
        assert metadata["no_dispatch"] is True
        assert metadata["work_package_rework"]["remaining_cycles"] == remaining
        assert fixture.store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (fixture.task_id,),
        )["node_state"] == "rejected"
        cancelled = fixture.store.query_all(
            "SELECT id, state, released_at, release_reason "
            "FROM work_package_wip_tokens WHERE task_id = ? AND stage = ? ORDER BY id",
            (fixture.task_id, "candidate_buffer"),
        )
        assert cancelled and all(row["state"] == "cancelled" for row in cancelled)
        assert all(row["released_at"] for row in cancelled)
        assert all(json.loads(row["release_reason"])["decision"] == "rejected" for row in cancelled)

        second = service.reject(
            fixture.candidate_id,
            actor="acceptance-controller",
            reason="candidate failed exact review",
        )
        assert second.created is False
        assert second.cancelled_wip_token_ids == first.cancelled_wip_token_ids
    finally:
        fixture.store.close()
