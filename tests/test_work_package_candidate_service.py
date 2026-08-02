from __future__ import annotations

import json

import pytest

from mac.models import TransitionError, ValidationError
from mac.services import ControlPlane
from mac.store import Store
from mac.test_support import ephemeral_store
from mac.work_package_candidate_service import WorkPackageCandidateService
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


def _plan() -> dict:
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_candidate",
        "goal": "Produce one reviewed candidate",
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
            }
        ],
    }


def _setup(monkeypatch) -> tuple[Store, ControlPlane, str, str, str]:
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
    admitted = WorkPackageService(store, repository_verifier=_Verifier()).admit(
        _plan(), actor="controller", reason="test"
    )
    WorkPackageService(store, repository_verifier=_Verifier()).activate(
        admitted.package.id,
        expected_plan_version=1,
        expected_epoch=1,
        actor="operator",
    )
    task_id = store.query_one(
        "SELECT task_id FROM work_package_task_links WHERE node_key = ?", ("change",)
    )["task_id"]
    cp = ControlPlane(store, secret_key="candidate-test-secret-key-value-0001")
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
    machine = cp.register_machine("candidate-host")
    agent = cp.register_agent(
        machine.id,
        "candidate-worker",
        capabilities=["work_package_v1"],
    )
    monkeypatch.setattr(
        "mac.worker_credentials.assert_package_worker_ready",
        lambda conn, agent_id: {"ready": True, "agent_id": agent_id},
    )
    claimed, lease = cp.claim_task(task_id, agent.id, sync_beads=False)
    cp.start_task(task_id, agent.id, lease_id=lease.id, drain_outbox=False)
    evidence_id = "ev_candidate"
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
            json.dumps({"verification": {"evidence_type": "repository_change"}}),
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
        "attempt_base_sha, declared_effects_digest, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id,
            task_id,
            lease.id,
            agent.id,
            claimed.attempt_count,
            assignment["attempt_ref"],
            assignment["attempt_base_sha"],
            assignment["declared_effects_digest"],
            "now",
        ),
    )
    return store, cp, task_id, lease.id, evidence_id


def test_candidate_submission_transfers_product_wip_atomically(monkeypatch) -> None:
    store, _cp, task_id, lease_id, evidence_id = _setup(monkeypatch)
    try:
        result = WorkPackageCandidateService(store).submit(
            evidence_id, actor="candidate-controller"
        )

        assert result.created is True
        assert result.candidate.assignment_lease_id == lease_id
        assert result.candidate.evidence_id == evidence_id
        assert store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (task_id,),
        )["node_state"] == "candidate_submitted"
        old = store.query_all(
            "SELECT * FROM work_package_wip_tokens WHERE task_id = ? AND stage = ?",
            (task_id, "mutation"),
        )
        buffered = store.query_all(
            "SELECT * FROM work_package_wip_tokens WHERE task_id = ? AND stage = ? "
            "ORDER BY id",
            (task_id, "candidate_buffer"),
        )
        assert old and all(row["state"] == "released" for row in old)
        assert buffered and all(row["state"] == "held" for row in buffered)
        assert {row["predecessor_token_id"] for row in buffered} == {
            row["id"] for row in old
        }
        assert tuple(row["id"] for row in buffered) == result.transferred_wip_token_ids
    finally:
        store.close()


def test_candidate_submission_is_idempotent_without_second_transfer(monkeypatch) -> None:
    store, _cp, task_id, _lease_id, evidence_id = _setup(monkeypatch)
    try:
        service = WorkPackageCandidateService(store)
        first = service.submit(evidence_id, actor="candidate-controller")
        second = service.submit(evidence_id, actor="candidate-controller")

        assert first.created is True
        assert second.created is False
        assert second.candidate.id == first.candidate.id
        assert second.transferred_wip_token_ids == first.transferred_wip_token_ids
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_node_candidates"
        )["n"] == 1
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_wip_tokens WHERE task_id = ?",
            (task_id,),
        )["n"] == 4
    finally:
        store.close()


def test_candidate_submission_rejects_unattributed_evidence(monkeypatch) -> None:
    store, _cp, task_id, _lease_id, _evidence_id = _setup(monkeypatch)
    try:
        store.execute(
            "INSERT INTO evidence ("
            "id, task_id, kind, uri, summary, checksum, metadata, created_by, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ev_unattributed",
                task_id,
                "artifact",
                "artifact://unattributed",
                "unattributed",
                None,
                "{}",
                "someone",
                "now",
            ),
        )
        with pytest.raises(ValidationError, match="exact work-package assignment"):
            WorkPackageCandidateService(store).submit(
                "ev_unattributed", actor="candidate-controller"
            )
    finally:
        store.close()


def test_candidate_submission_rejects_paused_package(monkeypatch) -> None:
    store, _cp, _task_id, _lease_id, evidence_id = _setup(monkeypatch)
    try:
        store.execute(
            "UPDATE work_packages SET state = ?, updated_at = ? WHERE id = ?",
            ("paused", "paused", "wp_candidate"),
        )
        with pytest.raises(TransitionError, match="not current and reviewable"):
            WorkPackageCandidateService(store).submit(
                evidence_id, actor="candidate-controller"
            )
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_node_candidates"
        )["n"] == 0
    finally:
        store.close()


def test_submit_for_review_routes_package_output_through_candidate_buffer(
    monkeypatch,
) -> None:
    store, cp, task_id, lease_id, evidence_id = _setup(monkeypatch)
    try:
        evidence = cp.get_evidence(evidence_id)
        monkeypatch.setattr(cp, "_require_review_ready", lambda task: evidence)

        reviewed = cp.submit_for_review(
            task_id,
            cp.get_task(task_id).owner_agent_id,
            lease_id=lease_id,
            drain_outbox=False,
        )

        assert reviewed.state == "needs_review"
        assert reviewed.lease_id is None
        assert store.query_one("SELECT status FROM leases WHERE id = ?", (lease_id,))[
            "status"
        ] == "released"
        assert store.query_one(
            "SELECT status FROM work_package_node_candidates WHERE evidence_id = ?",
            (evidence_id,),
        )["status"] == "submitted"
        assert store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (task_id,),
        )["node_state"] == "candidate_submitted"
    finally:
        store.close()


def test_generic_publication_cannot_bypass_package_landing(monkeypatch) -> None:
    store, cp, task_id, _lease_id, evidence_id = _setup(monkeypatch)
    try:
        with pytest.raises(ValidationError, match="exact-candidate"):
            cp.publish_task(
                task_id,
                "git://main",
                "operator",
                evidence_id=evidence_id,
            )
        assert cp.get_task(task_id).state == "running"
        assert store.query_one("SELECT COUNT(*) AS n FROM publications")["n"] == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda cp, task_id, _agent, _lease: cp.update_task(task_id, title="split"),
         "generic task update"),
        (lambda cp, task_id, _agent, _lease: cp.release_task(task_id),
         "generic task release"),
        (lambda cp, task_id, _agent, _lease: cp.add_child_tasks(
            task_id, [{"title": "unplanned child"}]
        ), "ad-hoc child creation"),
        (lambda cp, task_id, _agent, _lease: cp.reopen_task(task_id, "operator"),
         "operator reopen"),
        (lambda cp, task_id, _agent, _lease: cp.force_complete_task(
            task_id, "operator"
        ), "operator force-complete"),
        (lambda cp, task_id, _agent, _lease: cp.close_task(
            task_id,
            "cancelled",
            "operator",
            {"reason": "generic operator close"},
        ), "generic transition to cancelled"),
        (lambda cp, task_id, agent, lease: cp.transition_task(
            task_id, "failed", agent, lease_id=lease
        ), "generic transition to failed"),
        (lambda cp, task_id, agent, lease: cp.transition_task(
            task_id, "completed", agent, lease_id=lease
        ), "generic transition to completed"),
    ],
)
def test_legacy_lifecycle_writers_cannot_split_package_state(
    monkeypatch, operation, message
) -> None:
    store, cp, task_id, lease_id, _evidence_id = _setup(monkeypatch)
    try:
        task_before = cp.get_task(task_id).to_dict()
        link_before = dict(
            store.query_one(
                "SELECT * FROM work_package_task_links WHERE task_id = ?",
                (task_id,),
            )
        )
        wip_before = [
            dict(row)
            for row in store.query_all(
                "SELECT * FROM work_package_wip_tokens WHERE task_id = ? ORDER BY id",
                (task_id,),
            )
        ]

        with pytest.raises(ValidationError, match=message):
            operation(cp, task_id, task_before["owner_agent_id"], lease_id)

        assert cp.get_task(task_id).to_dict() == task_before
        assert dict(
            store.query_one(
                "SELECT * FROM work_package_task_links WHERE task_id = ?",
                (task_id,),
            )
        ) == link_before
        assert [
            dict(row)
            for row in store.query_all(
                "SELECT * FROM work_package_wip_tokens WHERE task_id = ? ORDER BY id",
                (task_id,),
            )
        ] == wip_before
    finally:
        store.close()


def test_expired_package_lease_requeues_node_and_transfers_exact_wip(monkeypatch) -> None:
    store, cp, task_id, lease_id, _evidence_id = _setup(monkeypatch)
    try:
        held_before = [
            row["id"]
            for row in store.query_all(
                "SELECT id FROM work_package_wip_tokens "
                "WHERE task_id = ? AND state = 'held' ORDER BY id",
                (task_id,),
            )
        ]
        store.execute(
            "UPDATE leases SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000000+00:00", lease_id),
        )

        recovered = cp.expire_leases(
            now="2030-01-01T00:00:00.000000+00:00",
            grace_seconds=0,
        )

        assert [task.id for task in recovered] == [task_id]
        task = cp.get_task(task_id)
        assert task.state == "open"
        assert task.lease_id is None
        assert store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (task_id,),
        )["node_state"] == "ready"
        repair = store.query_one(
            "SELECT * FROM work_package_lease_expiry_repairs WHERE lease_id = ?",
            (lease_id,),
        )
        assert repair is not None
        assert repair["target_task_state"] == "open"
        assert repair["target_node_state"] == "ready"
        assert repair["held_wip_ids"] == held_before
        assert [
            row["id"]
            for row in store.query_all(
                "SELECT id FROM work_package_wip_tokens "
                "WHERE task_id = ? AND state = 'held' ORDER BY id",
                (task_id,),
            )
        ] == held_before

        agent_id = cp.get_lease(lease_id).agent_id
        _retried, retry_lease = cp.claim_task(task_id, agent_id, sync_beads=False)
        assert retry_lease.id != lease_id
        assert store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (task_id,),
        )["node_state"] == "executing"
        all_tokens = store.query_all(
            "SELECT id, state, generation, predecessor_token_id, "
            "acquired_by_assignment_lease_id, release_reason "
            "FROM work_package_wip_tokens WHERE task_id = ? "
            "ORDER BY generation, id",
            (task_id,),
        )
        predecessors = [row for row in all_tokens if row["id"] in held_before]
        successors = [row for row in all_tokens if row["id"] not in held_before]
        assert len(predecessors) == len(held_before)
        assert len(successors) == len(held_before)
        assert {row["state"] for row in predecessors} == {"superseded"}
        assert {
            row["acquired_by_assignment_lease_id"] for row in predecessors
        } == {lease_id}
        assert all(
            str(row["release_reason"]).startswith("retry_transfer:")
            and str(row["release_reason"]).endswith(":" + retry_lease.id)
            for row in predecessors
        )
        assert {row["state"] for row in successors} == {"held"}
        assert {row["generation"] for row in successors} == {2}
        assert {
            row["acquired_by_assignment_lease_id"] for row in successors
        } == {retry_lease.id}
        assert {row["predecessor_token_id"] for row in successors} == set(held_before)
    finally:
        store.close()


def test_exhausted_package_lease_cancels_exact_wip_and_node(monkeypatch) -> None:
    store, cp, task_id, lease_id, _evidence_id = _setup(monkeypatch)
    try:
        held_before = [
            row["id"]
            for row in store.query_all(
                "SELECT id FROM work_package_wip_tokens "
                "WHERE task_id = ? AND state = 'held' ORDER BY id",
                (task_id,),
            )
        ]
        store.execute(
            "UPDATE tasks SET max_attempts = attempt_count WHERE id = ?",
            (task_id,),
        )
        store.execute(
            "UPDATE leases SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000000+00:00", lease_id),
        )

        recovered = cp.expire_leases(
            now="2030-01-01T00:00:00.000000+00:00",
            grace_seconds=0,
        )

        assert [task.id for task in recovered] == [task_id]
        assert cp.get_task(task_id).state == "failed"
        assert store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (task_id,),
        )["node_state"] == "cancelled"
        repair = store.query_one(
            "SELECT * FROM work_package_lease_expiry_repairs WHERE lease_id = ?",
            (lease_id,),
        )
        assert repair["target_task_state"] == "failed"
        assert repair["target_node_state"] == "cancelled"
        assert repair["held_wip_ids"] == held_before
        cancelled = store.query_all(
            "SELECT id, state, released_at, release_reason "
            "FROM work_package_wip_tokens WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        assert [row["id"] for row in cancelled] == held_before
        assert all(row["state"] == "cancelled" for row in cancelled)
        assert all(row["released_at"] for row in cancelled)
        assert all(row["release_reason"].startswith("lease_expiry:") for row in cancelled)
    finally:
        store.close()


def test_offline_bulk_expiry_uses_same_package_repair_finalizer(monkeypatch) -> None:
    store, cp, task_id, lease_id, _evidence_id = _setup(monkeypatch)
    try:
        agent_id = cp.get_lease(lease_id).agent_id

        cp._expire_agent_active_leases(
            agent_id,
            "2030-01-01T00:00:00.000000+00:00",
            "heartbeat_offline",
        )

        task = cp.get_task(task_id)
        assert task.state == "open"
        assert task.lease_id is None
        assert task.attempt_count == 0
        lease = cp.get_lease(lease_id)
        assert lease.status == "expired"
        repair = store.query_one(
            "SELECT * FROM work_package_lease_expiry_repairs WHERE lease_id = ?",
            (lease_id,),
        )
        assert repair is not None
        assert repair["target_node_state"] == "ready"
        assert store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (task_id,),
        )["node_state"] == "ready"
        decision = store.query_one(
            "SELECT expiry_finalization_decision FROM leases WHERE id = ?",
            (lease_id,),
        )["expiry_finalization_decision"]
        assert decision["attempt_count_after"] == 0
        assert decision["detail"]["attempt_refunded"] is True
    finally:
        store.close()
