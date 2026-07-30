from __future__ import annotations

import base64
import threading

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.models import AuthorizationError, TaskState, TransitionError, ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp() -> ControlPlane:
    return ControlPlane.in_memory()


def _register_agent(cp: ControlPlane, name: str, *, capacity: int = 1):
    machine = cp.register_machine(
        "%s-host" % name,
        resources={"cpu": 4, "memory_gb": 8},
    )
    return cp.register_agent(
        machine.id,
        name,
        resources={"capacity": capacity},
    )


def _set_worker_identity_enforced(cp: ControlPlane) -> None:
    """Seed an already-reviewed mode; readiness flip validation is tested elsewhere."""

    cp.store.execute(
        "INSERT INTO worker_credential_policy_state ("
        "singleton_key, mode, inventory_digest, ready_agent_ids, revision, "
        "updated_by, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("fleet", "enforced", None, "[]", 1, "test", "2026-01-01T00:00:00+00:00"),
    )


def test_claim_transaction_rechecks_dependencies(cp: ControlPlane) -> None:
    worker = _register_agent(cp, "worker")
    prerequisite = cp.create_task("prerequisite")
    dependent = cp.create_task("dependent", dependencies=[prerequisite.id])

    # Reproduce an OPEN candidate produced from stale/incorrect scheduler
    # state. The claim transaction, not candidate generation, is the final
    # authority for prerequisite completion.
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.OPEN.value, dependent.id),
    )

    with pytest.raises(TransitionError, match="dependencies are not complete"):
        cp.claim_task(dependent.id, worker.id, sync_beads=False)

    fresh = cp.get_task(dependent.id)
    assert fresh.state == TaskState.OPEN.value
    assert fresh.lease_id is None
    assert fresh.attempt_count == 0


def test_concurrent_claims_serialize_agent_capacity(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_agent(cp, "one-slot-worker", capacity=1)
    tasks = [cp.create_task("lane-a"), cp.create_task("lane-b")]
    barrier = threading.Barrier(2)

    # Force both callers past the intentionally advisory preflight check. The
    # transactional capacity recheck must still admit exactly one claim.
    def simultaneous_preflight(agent, task, *, allow_cooperative_reuse=False):
        barrier.wait(timeout=5)
        return True

    monkeypatch.setattr(cp, "_agent_available_for", simultaneous_preflight)
    results = []
    results_lock = threading.Lock()

    def claim(task_id: str) -> None:
        try:
            _task, lease = cp.claim_task(task_id, worker.id, sync_beads=False)
            result = ("ok", lease.id)
        except (TransitionError, ValidationError) as exc:
            result = ("error", str(exc))
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=claim, args=(task.id,)) for task in tasks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert [result[0] for result in results].count("ok") == 1
    assert [result[0] for result in results].count("error") == 1
    active = cp.store.query_one(
        "SELECT COUNT(*) AS count FROM leases WHERE agent_id = ? AND status = 'active'",
        (worker.id,),
    )
    assert int(active["count"]) == 1
    assert sum(cp.get_task(task.id).state == TaskState.CLAIMED.value for task in tasks) == 1


def test_reacquired_task_rejects_stale_and_unfenced_worker_writes(
    cp: ControlPlane,
) -> None:
    worker = _register_agent(cp, "worker")
    task = cp.create_task("retryable work")
    _claimed, first_lease = cp.claim_task(task.id, worker.id, sync_beads=False)
    cp.release_lease(first_lease.id, worker.id)
    _claimed, current_lease = cp.claim_task(task.id, worker.id, sync_beads=False)

    with pytest.raises(AuthorizationError):
        cp.start_task(task.id, worker.id, lease_id=first_lease.id)
    with pytest.raises(AuthorizationError, match="current lease_id is required"):
        cp.start_task(task.id, worker.id)

    started = cp.start_task(task.id, worker.id, lease_id=current_lease.id)
    assert started.state == TaskState.RUNNING.value

    with pytest.raises(AuthorizationError):
        cp.add_evidence(
            task.id,
            "log",
            "artifact://stale",
            "stale attempt",
            worker.id,
            lease_id=first_lease.id,
            sync_beads=False,
        )
    with pytest.raises(AuthorizationError, match="current lease_id is required"):
        cp.add_evidence(
            task.id,
            "log",
            "artifact://unfenced",
            "unfenced attempt",
            worker.id,
            sync_beads=False,
        )

    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://current",
        "current attempt",
        worker.id,
        lease_id=current_lease.id,
        sync_beads=False,
    )
    assert evidence.created_by == worker.id

    with pytest.raises(AuthorizationError):
        cp.transition_task(
            task.id,
            TaskState.BLOCKED.value,
            worker.id,
            {"reason": "stale writer", "manual_repair_required": True},
            lease_id=first_lease.id,
            drain_outbox=False,
        )
    with pytest.raises(AuthorizationError, match="current lease_id is required"):
        cp.transition_task(
            task.id,
            TaskState.BLOCKED.value,
            worker.id,
            {"reason": "unfenced writer", "manual_repair_required": True},
            drain_outbox=False,
        )

    blocked = cp.transition_task(
        task.id,
        TaskState.BLOCKED.value,
        worker.id,
        {"reason": "current writer", "manual_repair_required": True},
        lease_id=current_lease.id,
        drain_outbox=False,
    )
    assert blocked.state == TaskState.BLOCKED.value


def test_single_attempt_legacy_worker_call_cannot_be_aba(cp: ControlPlane) -> None:
    worker = _register_agent(cp, "legacy-worker")
    task = cp.create_task("first attempt")
    cp.claim_task(task.id, worker.id, sync_beads=False)

    # Compatibility is intentionally limited to the one-lease case. There is
    # no earlier attempt whose process could be confused with this lease.
    started = cp.start_task(task.id, worker.id, drain_outbox=False)
    assert started.state == TaskState.RUNNING.value
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://legacy",
        "legacy first attempt",
        worker.id,
        sync_beads=False,
    )
    assert evidence.task_id == task.id


def test_cross_agent_reassignment_rejects_stale_and_spoofed_transitions(
    cp: ControlPlane,
) -> None:
    first = _register_agent(cp, "first-worker")
    second = _register_agent(cp, "second-worker")
    task = cp.create_task("cross-agent retry")
    _claimed, first_lease = cp.claim_task(task.id, first.id, sync_beads=False)
    cp.release_lease(first_lease.id, first.id)
    _claimed, second_lease = cp.claim_task(task.id, second.id, sync_beads=False)

    with pytest.raises(AuthorizationError):
        cp.transition_task(
            task.id,
            TaskState.BLOCKED.value,
            first.id,
            {"reason": "stale first worker", "manual_repair_required": True},
            lease_id=first_lease.id,
            drain_outbox=False,
        )
    with pytest.raises(
        AuthorizationError,
        match="current lease_id is required for active task transitions",
    ):
        cp.transition_task(
            task.id,
            TaskState.BLOCKED.value,
            "dispatcher",
            {"reason": "spoofed internal actor", "manual_repair_required": True},
            drain_outbox=False,
        )

    blocked = cp.transition_task(
        task.id,
        TaskState.BLOCKED.value,
        second.id,
        {"reason": "current second worker", "manual_repair_required": True},
        lease_id=second_lease.id,
        drain_outbox=False,
    )
    assert blocked.state == TaskState.BLOCKED.value


def test_bound_agent_cannot_spoof_current_owner_on_transition(
    cp: ControlPlane,
) -> None:
    first = _register_agent(cp, "first-worker")
    second = _register_agent(cp, "second-worker")
    task = cp.create_task("authenticated actor binding")
    _claimed, first_lease = cp.claim_task(task.id, first.id, sync_beads=False)
    cp.release_lease(first_lease.id, first.id)
    _claimed, second_lease = cp.claim_task(task.id, second.id, sync_beads=False)
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "first-token": {"scopes": ["write"], "agent_id": first.id},
        },
    )

    with TestClient(app) as client:
        response = client.post(
            "/tasks/%s/transition" % task.id,
            headers={"Authorization": "Bearer first-token"},
            json={
                "target_state": TaskState.BLOCKED.value,
                "actor": second.id,
                "lease_id": second_lease.id,
                "detail": {
                    "reason": "spoof current owner",
                    "manual_repair_required": True,
                },
            },
        )

    assert response.status_code == 403
    assert cp.get_task(task.id).state == TaskState.CLAIMED.value


def test_transition_rechecks_lease_after_validation_before_write(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_agent(cp, "transition-racer")
    task = cp.create_task("transition race")
    _claimed, first_lease = cp.claim_task(task.id, worker.id, sync_beads=False)
    cp.start_task(task.id, worker.id, lease_id=first_lease.id, drain_outbox=False)

    validated = threading.Event()
    resume = threading.Event()
    original = cp._require_exact_lease_actor

    def pause_after_validation(task_arg, agent_id, lease_id):
        original(task_arg, agent_id, lease_id)
        validated.set()
        assert resume.wait(timeout=5)

    monkeypatch.setattr(cp, "_require_exact_lease_actor", pause_after_validation)
    outcome = []

    def stale_transition() -> None:
        try:
            cp.transition_task(
                task.id,
                TaskState.BLOCKED.value,
                worker.id,
                {"reason": "late lease A", "manual_repair_required": True},
                lease_id=first_lease.id,
                drain_outbox=False,
            )
            outcome.append("unexpected-success")
        except AuthorizationError:
            outcome.append("rejected")

    thread = threading.Thread(target=stale_transition)
    thread.start()
    assert validated.wait(timeout=5)
    cp.release_lease(first_lease.id, worker.id)
    _claimed, second_lease = cp.claim_task(task.id, worker.id, sync_beads=False)
    resume.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert outcome == ["rejected"]
    current = cp.get_task(task.id)
    assert current.state == TaskState.CLAIMED.value
    assert current.lease_id == second_lease.id


def test_evidence_rechecks_lease_after_validation_before_insert(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_agent(cp, "evidence-racer")
    task = cp.create_task("evidence race")
    _claimed, first_lease = cp.claim_task(task.id, worker.id, sync_beads=False)
    cp.start_task(task.id, worker.id, lease_id=first_lease.id, drain_outbox=False)

    validated = threading.Event()
    resume = threading.Event()
    original = cp._prepare_evidence_artifacts

    def pause_before_insert(*args, **kwargs):
        validated.set()
        assert resume.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(cp, "_prepare_evidence_artifacts", pause_before_insert)
    outcome = []

    def stale_evidence() -> None:
        try:
            cp.add_evidence(
                task.id,
                "log",
                "artifact://late-a",
                "late lease A evidence",
                worker.id,
                lease_id=first_lease.id,
                sync_beads=False,
            )
            outcome.append("unexpected-success")
        except AuthorizationError:
            outcome.append("rejected")

    thread = threading.Thread(target=stale_evidence)
    thread.start()
    assert validated.wait(timeout=5)
    cp.release_lease(first_lease.id, worker.id)
    _claimed, second_lease = cp.claim_task(task.id, worker.id, sync_beads=False)
    resume.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert outcome == ["rejected"]
    assert cp.list_evidence(task.id) == []
    assert cp.get_task(task.id).lease_id == second_lease.id


def test_active_but_expired_lease_is_not_worker_authority(cp: ControlPlane) -> None:
    worker = _register_agent(cp, "expired-worker")
    task = cp.create_task("expired but unswept")
    _claimed, lease = cp.claim_task(task.id, worker.id, sync_beads=False)
    expired_at = "2000-01-01T00:00:00.000000+00:00"
    cp.store.execute(
        "UPDATE leases SET expires_at = ? WHERE id = ?",
        (expired_at, lease.id),
    )
    cp.store.execute(
        "UPDATE tasks SET leased_until = ? WHERE id = ?",
        (expired_at, task.id),
    )

    assert cp.get_lease(lease.id).status == "active"
    with pytest.raises(AuthorizationError):
        cp.start_task(task.id, worker.id, lease_id=lease.id, drain_outbox=False)
    assert cp.get_task(task.id).state == TaskState.CLAIMED.value


def test_lifecycle_http_identity_and_recovery_authority_are_fail_closed(
    cp: ControlPlane,
) -> None:
    first = _register_agent(cp, "http-first", capacity=3)
    second = _register_agent(cp, "http-second", capacity=3)
    claim_target = cp.create_task("cross-agent claim")
    leased_task = cp.create_task("owned lease")
    _claimed, owned_lease = cp.claim_task(
        leased_task.id,
        first.id,
        sync_beads=False,
    )
    held = cp.create_task("held task", metadata={"no_dispatch": True})
    blocked = cp.create_task("blocked recovery")
    _claimed, blocked_lease = cp.claim_task(blocked.id, second.id, sync_beads=False)
    cp.start_task(
        blocked.id,
        second.id,
        lease_id=blocked_lease.id,
        drain_outbox=False,
    )
    cp.transition_task(
        blocked.id,
        TaskState.BLOCKED.value,
        second.id,
        {"reason": "test recovery", "manual_repair_required": True},
        lease_id=blocked_lease.id,
        drain_outbox=False,
    )
    force_target = cp.create_task("force recovery")
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "first-token": {"scopes": ["write"], "agent_id": first.id},
            "second-token": {"scopes": ["write"], "agent_id": second.id},
            "admin-token": {"scopes": ["admin"]},
        },
    )

    first_headers = {"Authorization": "Bearer first-token"}
    second_headers = {"Authorization": "Bearer second-token"}
    admin_headers = {"Authorization": "Bearer admin-token"}
    with TestClient(app) as client:
        cross_claim = client.post(
            "/tasks/%s/claim" % claim_target.id,
            params={"agent_id": second.id},
            headers=first_headers,
        )
        spoof_renew = client.post(
            "/leases/%s/renew" % owned_lease.id,
            headers=second_headers,
            json={"agent_id": first.id, "lease_seconds": 120},
        )
        spoof_delegate = client.post(
            "/leases/%s/delegate" % owned_lease.id,
            headers=second_headers,
            json={"agent_id": first.id, "to_agent_id": second.id},
        )
        release_denied = client.post(
            "/tasks/%s/release" % held.id,
            headers=first_headers,
            json={"actor": first.id},
        )
        reopen_denied = client.post(
            "/tasks/%s/reopen" % blocked.id,
            headers=first_headers,
            json={"actor": first.id, "reason": "spoof recovery"},
        )
        force_denied = client.post(
            "/tasks/%s/force-complete" % force_target.id,
            headers=first_headers,
            json={"actor": first.id, "reason": "spoof force"},
        )

        release_allowed = client.post(
            "/tasks/%s/release" % held.id,
            headers=admin_headers,
            json={"actor": "admin"},
        )
        reopen_allowed = client.post(
            "/tasks/%s/reopen" % blocked.id,
            headers=admin_headers,
            json={"actor": "admin", "reason": "operator recovery"},
        )
        force_allowed = client.post(
            "/tasks/%s/force-complete" % force_target.id,
            headers=admin_headers,
            json={"actor": "admin", "reason": "operator recovery"},
        )

    assert [
        cross_claim.status_code,
        spoof_renew.status_code,
        spoof_delegate.status_code,
        release_denied.status_code,
        reopen_denied.status_code,
        force_denied.status_code,
    ] == [403, 403, 403, 403, 403, 403]
    assert cp.get_task(claim_target.id).lease_id is None
    assert cp.get_lease(owned_lease.id).delegated_agent_id is None
    assert release_allowed.status_code == 200
    assert cp.get_task(held.id).metadata.get("no_dispatch") is None
    assert reopen_allowed.status_code == 200
    assert cp.get_task(blocked.id).state == TaskState.OPEN.value
    assert force_allowed.status_code == 200
    assert cp.get_task(force_target.id).state == TaskState.COMPLETED.value


def test_unbound_write_and_admin_tokens_cannot_impersonate_workers(
    cp: ControlPlane,
) -> None:
    worker = _register_agent(cp, "bound-only-worker", capacity=3)
    write_target = cp.create_task("unbound write claim")
    admin_target = cp.create_task("unbound admin claim")
    active = cp.create_task("unbound active mutation")
    _claimed, lease = cp.claim_task(active.id, worker.id, sync_beads=False)
    _set_worker_identity_enforced(cp)
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "shared-write": {"scopes": ["write"]},
            "shared-admin": {"scopes": ["admin"]},
        },
    )

    with TestClient(app) as client:
        write_claim = client.post(
            "/tasks/%s/claim" % write_target.id,
            headers={"Authorization": "Bearer shared-write"},
            params={"agent_id": worker.id},
        )
        admin_claim = client.post(
            "/tasks/%s/claim" % admin_target.id,
            headers={"Authorization": "Bearer shared-admin"},
            params={"agent_id": worker.id},
        )
        admin_start = client.post(
            "/tasks/%s/start" % active.id,
            headers={"Authorization": "Bearer shared-admin"},
            params={"agent_id": worker.id, "lease_id": lease.id},
        )

    assert [write_claim.status_code, admin_claim.status_code, admin_start.status_code] == [
        403,
        403,
        403,
    ]
    assert "agent-bound token" in write_claim.json()["detail"]
    assert "agent-bound token" in admin_claim.json()["detail"]
    assert cp.get_task(write_target.id).lease_id is None
    assert cp.get_task(admin_target.id).lease_id is None
    assert cp.get_task(active.id).state == TaskState.CLAIMED.value


def test_bound_agent_cannot_mutate_unowned_nonactive_tasks_or_evidence(
    cp: ControlPlane,
) -> None:
    executor = _register_agent(cp, "nonactive-executor", capacity=2)
    attacker = _register_agent(cp, "nonactive-attacker", capacity=2)
    open_task = cp.create_task("unowned open")
    review_task = cp.create_task("unowned review")
    _claimed, lease = cp.claim_task(
        review_task.id, executor.id, sync_beads=False
    )
    cp.start_task(
        review_task.id,
        executor.id,
        lease_id=lease.id,
        drain_outbox=False,
    )
    evidence = cp.add_evidence(
        review_task.id,
        "log",
        "artifact://executor",
        "executor result",
        executor.id,
        lease_id=lease.id,
        sync_beads=False,
    )
    cp.update_task(
        review_task.id,
        metadata={
            **cp.get_task(review_task.id).metadata,
            "review_target": {"executor_evidence_id": evidence.id},
        },
    )
    cp.store.execute(
        "UPDATE leases SET status = 'released' WHERE id = ?",
        (lease.id,),
    )
    cp.store.execute(
        "UPDATE tasks SET state = ?, owner_agent_id = NULL, lease_id = NULL, "
        "leased_until = NULL WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, review_task.id),
    )
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "attacker-token": {"scopes": ["write"], "agent_id": attacker.id},
        },
    )
    headers = {"Authorization": "Bearer attacker-token"}
    with TestClient(app) as client:
        cancel_open = client.post(
            "/tasks/%s/transition" % open_task.id,
            headers=headers,
            json={
                "target_state": TaskState.CANCELLED.value,
                "actor": attacker.id,
                "detail": {"reason": "forge cancellation"},
            },
        )
        reopen_review = client.post(
            "/tasks/%s/transition" % review_task.id,
            headers=headers,
            json={
                "target_state": TaskState.RUNNING.value,
                "actor": attacker.id,
            },
        )
        forge_open_evidence = client.post(
            "/tasks/%s/evidence" % open_task.id,
            headers=headers,
            json={
                "kind": "log",
                "uri": "artifact://forged-open",
                "summary": "forged evidence",
                "created_by": attacker.id,
            },
        )
        forge_review_evidence = client.post(
            "/tasks/%s/evidence" % review_task.id,
            headers=headers,
            json={
                "kind": "review",
                "uri": "artifact://forged-review",
                "summary": "forged verdict",
                "created_by": attacker.id,
                "metadata": {
                    "verification": {
                        "evidence_type": "review_verdict",
                        "reviewed_evidence_id": evidence.id,
                    }
                },
            },
        )

    assert [
        cancel_open.status_code,
        reopen_review.status_code,
        forge_open_evidence.status_code,
        forge_review_evidence.status_code,
    ] == [403, 403, 403, 403]
    assert cp.get_task(open_task.id).state == TaskState.OPEN.value
    assert cp.get_task(review_task.id).state == TaskState.NEEDS_REVIEW.value
    assert [item.id for item in cp.list_evidence(review_task.id)] == [evidence.id]


def test_review_routes_bind_assignment_claim_and_decision_to_principal(
    cp: ControlPlane,
) -> None:
    executor = _register_agent(cp, "review-route-executor", capacity=2)
    assigned = _register_agent(cp, "review-route-assigned", capacity=2)
    attacker = _register_agent(cp, "review-route-attacker", capacity=2)
    cp.update_agent(assigned.id, capabilities=["review"])
    cp.update_agent(attacker.id, capabilities=["review"])
    task = cp.create_task("review route authority")
    _claimed, lease = cp.claim_task(task.id, executor.id, sync_beads=False)
    cp.start_task(
        task.id,
        executor.id,
        lease_id=lease.id,
        drain_outbox=False,
    )
    executor_evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://review-target",
        "executor result",
        executor.id,
        lease_id=lease.id,
        sync_beads=False,
    )
    cp.update_task(
        task.id,
        metadata={
            **cp.get_task(task.id).metadata,
            "review_target": {"executor_evidence_id": executor_evidence.id},
        },
    )
    cp.store.execute(
        "UPDATE leases SET status = 'released' WHERE id = ?",
        (lease.id,),
    )
    cp.store.execute(
        "UPDATE tasks SET state = ?, owner_agent_id = NULL, lease_id = NULL, "
        "leased_until = NULL WHERE id = ?",
        (TaskState.NEEDS_REVIEW.value, task.id),
    )
    _set_worker_identity_enforced(cp)
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "admin-token": {"scopes": ["admin"]},
            "assigned-token": {"scopes": ["write"], "agent_id": assigned.id},
            "attacker-token": {"scopes": ["write"], "agent_id": attacker.id},
        },
    )
    admin_headers = {"Authorization": "Bearer admin-token"}
    assigned_headers = {"Authorization": "Bearer assigned-token"}
    attacker_headers = {"Authorization": "Bearer attacker-token"}
    with TestClient(app) as client:
        request = client.post(
            "/tasks/%s/reviews" % task.id,
            headers=admin_headers,
            json={"reviewer_agent_id": assigned.id, "actor": attacker.id},
        )
        assert request.status_code == 200
        review_id = request.json()["id"]
        forged_claim = client.post(
            "/reviews/%s/claim" % review_id,
            headers=attacker_headers,
            json={"reviewer_agent_id": assigned.id, "actor": attacker.id},
        )
        unbound_claim = client.post(
            "/reviews/%s/claim" % review_id,
            headers=admin_headers,
            json={"reviewer_agent_id": assigned.id, "actor": "admin"},
        )
        valid_claim = client.post(
            "/reviews/%s/claim" % review_id,
            headers=assigned_headers,
            json={
                "reviewer_agent_id": assigned.id,
                "executor_evidence_id": executor_evidence.id,
                "actor": attacker.id,
            },
        )
        forged_decision = client.post(
            "/reviews/%s/decision" % review_id,
            headers=attacker_headers,
            json={
                "status": "rejected",
                "reviewer_agent_id": assigned.id,
                "reason": "forged",
            },
        )

    assert [forged_claim.status_code, unbound_claim.status_code] == [403, 403]
    assert valid_claim.status_code == 200
    assert valid_claim.json()["claim"]["actor"] == assigned.id
    assert forged_decision.status_code == 403
    assert cp.get_review(review_id).status == "pending"


@pytest.mark.parametrize(
    "mutation, expected_reason",
    [
        ("agent_offline", "agent_status_unavailable"),
        ("task_hold", "dispatch_held"),
        ("project_pause", "project_dispatch_paused"),
        ("capability_removed", "capabilities_missing"),
        ("runtime_changed", "runtime_digest_mismatch"),
    ],
)
def test_claim_rechecks_mutable_eligibility_in_transaction(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_reason: str,
) -> None:
    machine = cp.register_machine(
        "eligibility-host",
        resources={"cpu": 4, "memory_gb": 8},
    )
    worker = cp.register_agent(
        machine.id,
        "eligibility-worker",
        capabilities=["python"],
        resources={"capacity": 1},
    )
    cp.store.execute(
        "UPDATE agents SET running_digest = ? WHERE id = ?",
        ("runtime-a", worker.id),
    )
    cp.create_project("eligibility-project", dispatch_paused=False)
    task = cp.create_task(
        "eligibility race",
        project="eligibility-project",
        required_capabilities=["python"],
        metadata={"runtime": {"required_runtime_digest": "runtime-a"}},
    )
    preflight_complete = threading.Event()
    resume = threading.Event()
    original = cp._agent_available_for

    def pause_after_preflight(agent, candidate, *, allow_cooperative_reuse=False):
        result = original(
            agent,
            candidate,
            allow_cooperative_reuse=allow_cooperative_reuse,
        )
        assert result is True
        preflight_complete.set()
        assert resume.wait(timeout=5)
        return result

    monkeypatch.setattr(cp, "_agent_available_for", pause_after_preflight)
    outcome = []

    def claim() -> None:
        try:
            cp.claim_task(task.id, worker.id, sync_beads=False)
            outcome.append(("unexpected-success", ""))
        except ValidationError as exc:
            outcome.append(("rejected", str(exc)))

    thread = threading.Thread(target=claim)
    thread.start()
    assert preflight_complete.wait(timeout=5)
    if mutation == "agent_offline":
        cp.update_agent(worker.id, status="offline")
    elif mutation == "task_hold":
        current = cp.get_task(task.id)
        cp.update_task(
            task.id,
            metadata={**current.metadata, "no_dispatch": True},
        )
    elif mutation == "project_pause":
        cp.set_project_dispatch("eligibility-project", paused=True)
    elif mutation == "capability_removed":
        cp.update_agent(worker.id, capabilities=[])
    elif mutation == "runtime_changed":
        cp.store.execute(
            "UPDATE agents SET running_digest = ? WHERE id = ?",
            ("runtime-b", worker.id),
        )
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(mutation)
    resume.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert outcome[0][0] == "rejected"
    assert expected_reason in outcome[0][1]
    assert cp.get_task(task.id).lease_id is None
    count = cp.store.query_one(
        "SELECT COUNT(*) AS count FROM leases WHERE task_id = ?",
        (task.id,),
    )
    assert int(count["count"]) == 0


def test_claim_rechecks_break_glass_reservation_after_preflight(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _register_agent(cp, "ordinary-claimant")
    reserved = _register_agent(cp, "reserved-recovery-worker")
    task = cp.create_task("break-glass reservation race")
    preflight_complete = threading.Event()
    resume = threading.Event()
    original = cp._agent_available_for

    def pause_after_preflight(agent, candidate, *, allow_cooperative_reuse=False):
        result = original(
            agent,
            candidate,
            allow_cooperative_reuse=allow_cooperative_reuse,
        )
        assert result is True
        preflight_complete.set()
        assert resume.wait(timeout=5)
        return result

    monkeypatch.setattr(cp, "_agent_available_for", pause_after_preflight)
    outcome = []

    def claim() -> None:
        try:
            cp.claim_task(task.id, first.id, sync_beads=False)
            outcome.append("unexpected-success")
        except AuthorizationError as exc:
            outcome.append(str(exc))

    thread = threading.Thread(target=claim)
    thread.start()
    assert preflight_complete.wait(timeout=5)
    authorization = cp.authorize_task_break_glass(
        task.id,
        reserved.id,
        reason="repair the worker runtime",
        authorized_by="operator",
    )
    resume.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert "reserved by break-glass authorization" in outcome[0]
    assert cp.get_task(task.id).lease_id is None
    assert cp.list_task_break_glass_authorizations(task_id=task.id)[0].id == authorization.id
    assert cp.list_task_break_glass_authorizations(task_id=task.id)[0].status == "active"


def test_lease_clock_is_sampled_after_claim_locks_and_ignores_hub_skew(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_agent(cp, "clock-worker")
    task = cp.create_task("clock-safe lease")
    authority = {"now": "2030-01-01T00:00:00.000000+00:00"}
    cp._lease_clock = lambda: authority["now"]
    original = cp._claim_role_ineligibility_reason_in_transaction

    def finish_lock_wait(conn, *, agent, task, machine):
        result = original(conn, agent=agent, task=task, machine=machine)
        # Model five minutes spent waiting for the final eligibility locks.
        # The lease clock must be sampled after this point, not before entering
        # the transaction.
        authority["now"] = "2030-01-01T00:05:00.000000+00:00"
        return result

    monkeypatch.setattr(
        cp,
        "_claim_role_ineligibility_reason_in_transaction",
        finish_lock_wait,
    )
    _claimed, lease = cp.claim_task(
        task.id,
        worker.id,
        lease_seconds=60,
        sync_beads=False,
    )
    assert lease.created_at == "2030-01-01T00:05:00.000000+00:00"
    assert lease.expires_at == "2030-01-01T00:06:00.000000+00:00"

    # A wildly fast process clock must not reject a lease that the shared
    # authority still considers live.
    monkeypatch.setattr(
        "mac.services.utcnow",
        lambda: "2099-01-01T00:00:00.000000+00:00",
    )
    started = cp.start_task(
        task.id,
        worker.id,
        lease_id=lease.id,
        drain_outbox=False,
    )
    assert started.state == TaskState.RUNNING.value
    renewed = cp.renew_lease(lease.id, worker.id, lease_seconds=120)
    assert renewed.expires_at == "2030-01-01T00:07:00.000000+00:00"


def test_expiry_scan_cannot_clobber_renewal_after_candidate_selection(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_agent(cp, "renew-race-worker")
    task = cp.create_task("renew versus expiry")
    cp._lease_clock = lambda: "2030-01-01T00:00:00.000000+00:00"
    _claimed, lease = cp.claim_task(
        task.id,
        worker.id,
        lease_seconds=60,
        sync_beads=False,
    )
    candidate_selected = threading.Event()
    resume = threading.Event()
    original = cp._expire_lease_row

    def pause_before_row_lock(row, *, grace_seconds=0):
        candidate_selected.set()
        assert resume.wait(timeout=5)
        return original(row, grace_seconds=grace_seconds)

    monkeypatch.setattr(cp, "_expire_lease_row", pause_before_row_lock)
    # A fast replica over-selects the otherwise-live lease.
    monkeypatch.setattr(
        "mac.services.utcnow",
        lambda: "2099-01-01T00:00:00.000000+00:00",
    )
    recovered = []

    def expire() -> None:
        recovered.extend(cp.expire_leases())

    thread = threading.Thread(target=expire)
    thread.start()
    assert candidate_selected.wait(timeout=5)
    renewed = cp.renew_lease(lease.id, worker.id, lease_seconds=120)
    resume.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert recovered == []
    assert renewed.status == "active"
    assert cp.get_lease(lease.id).status == "active"
    assert cp.get_task(task.id).lease_id == lease.id


def test_fast_sweeper_clock_cannot_expire_authority_live_lease(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_agent(cp, "fast-clock-sweeper")
    task = cp.create_task("authority-live lease")
    cp._lease_clock = lambda: "2030-01-01T00:00:00.000000+00:00"
    _claimed, lease = cp.claim_task(
        task.id,
        worker.id,
        lease_seconds=60,
        sync_beads=False,
    )
    monkeypatch.setattr(
        "mac.services.utcnow",
        lambda: "2099-01-01T00:00:00.000000+00:00",
    )

    assert cp.expire_leases() == []
    assert cp.get_lease(lease.id).status == "active"
    assert cp.get_task(task.id).lease_id == lease.id


def test_concurrent_expiry_sweepers_finalize_one_repair_and_history(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_agent(cp, "expiry-finalizer-worker")
    task = cp.create_task(
        "repairable expired work",
        max_attempts=1,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    _claimed, lease = cp.claim_task(task.id, worker.id, sync_beads=False)
    cp.transition_task(
        task.id,
        TaskState.BLOCKED.value,
        worker.id,
        {"reason": "heartbeat_offline"},
        lease_id=lease.id,
        drain_outbox=False,
    )
    expired_at = "2000-01-01T00:00:00.000000+00:00"
    cp.store.execute(
        "UPDATE leases SET status = 'active', expires_at = ? WHERE id = ?",
        (expired_at, lease.id),
    )
    cp.store.execute(
        "UPDATE tasks SET state = ?, owner_agent_id = ?, lease_id = ?, "
        "leased_until = ?, attempt_count = 1 WHERE id = ?",
        (TaskState.RUNNING.value, worker.id, lease.id, expired_at, task.id),
    )

    finalizer_entered = threading.Event()
    finish_finalizer = threading.Event()
    classifications = []
    original = cp._exhausted_attempt_terminal_transition

    def pause_finalizer(task_arg, detail):
        classifications.append(task_arg.id)
        finalizer_entered.set()
        assert finish_finalizer.wait(timeout=5)
        return original(task_arg, detail)

    monkeypatch.setattr(cp, "_exhausted_attempt_terminal_transition", pause_finalizer)
    first_result = []

    def first_sweeper() -> None:
        first_result.extend(cp.expire_leases(grace_seconds=0))

    thread = threading.Thread(target=first_sweeper)
    thread.start()
    assert finalizer_entered.wait(timeout=5)

    # The first sweeper has committed EXPIRED and owns the durable finalizer
    # claim, but is paused before repair creation. A peer may observe the same
    # attached expired lease; it must not enter classification or create work.
    assert cp.expire_leases(grace_seconds=0) == []
    assert classifications == [task.id]

    finish_finalizer.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(first_result) == 1

    refreshed = cp.get_task(task.id)
    repair_id = refreshed.metadata["environment_repair_task_id"]
    repair_rows = cp.store.query_all(
        "SELECT id FROM tasks WHERE json_extract(metadata, '$.origin.parent_task_id') = ?",
        (task.id,),
    )
    expiry_history = [
        event
        for event in cp.task_history(task.id)
        if event.event_type == "task.lease_expired"
    ]
    assert [row["id"] for row in repair_rows] == [repair_id]
    assert len(expiry_history) == 1


@pytest.mark.parametrize("crash_point", ["after_prepare", "after_decision"])
def test_expiry_repair_finalization_recovers_without_duplicate_or_early_reset(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    worker = _register_agent(cp, "expiry-crash-%s" % crash_point)
    task = cp.create_task(
        "crash-safe repair",
        max_attempts=1,
        metadata={"repair_policy": {"environment_prerequisite": True}},
    )
    _claimed, lease = cp.claim_task(task.id, worker.id, sync_beads=False)
    cp.transition_task(
        task.id,
        TaskState.BLOCKED.value,
        worker.id,
        {"reason": "heartbeat_offline"},
        lease_id=lease.id,
        drain_outbox=False,
    )
    expired_at = "2000-01-01T00:00:00.000000+00:00"
    cp.store.execute(
        "UPDATE leases SET status = 'active', expires_at = ? WHERE id = ?",
        (expired_at, lease.id),
    )
    cp.store.execute(
        "UPDATE tasks SET state = ?, owner_agent_id = ?, lease_id = ?, "
        "leased_until = ?, attempt_count = 1 WHERE id = ?",
        (TaskState.RUNNING.value, worker.id, lease.id, expired_at, task.id),
    )

    if crash_point == "after_prepare":
        original = cp._exhausted_attempt_terminal_transition

        def crash_after_prepare(task_arg, detail):
            original(task_arg, detail)
            raise RuntimeError("simulated crash after repair preparation")

        monkeypatch.setattr(
            cp,
            "_exhausted_attempt_terminal_transition",
            crash_after_prepare,
        )
    else:
        original = cp._consume_break_glass_authorizations

        def crash_after_decision(*args, **kwargs):
            raise RuntimeError("simulated crash after decision persistence")

        monkeypatch.setattr(
            cp,
            "_consume_break_glass_authorizations",
            crash_after_decision,
        )

    assert cp.expire_leases(grace_seconds=0) == []
    stranded = cp.get_task(task.id)
    assert stranded.lease_id == lease.id
    assert stranded.attempt_count == 1
    repair_rows = cp.store.query_all(
        "SELECT id FROM tasks WHERE json_extract(metadata, '$.origin.parent_task_id') = ?",
        (task.id,),
    )
    assert len(repair_rows) == 1

    if crash_point == "after_prepare":
        monkeypatch.setattr(cp, "_exhausted_attempt_terminal_transition", original)
    else:
        monkeypatch.setattr(cp, "_consume_break_glass_authorizations", original)
    cp.store.execute(
        "UPDATE leases SET expiry_finalizer_claimed_at = ? WHERE id = ?",
        (expired_at, lease.id),
    )

    recovered = cp.expire_leases(grace_seconds=0)
    assert len(recovered) == 1
    waiting = cp.get_task(task.id)
    assert waiting.state == TaskState.WAITING.value
    assert waiting.attempt_count == 0
    assert waiting.dependencies == [repair_rows[0]["id"]]
    assert len(
        cp.store.query_all(
            "SELECT id FROM tasks WHERE json_extract(metadata, '$.origin.parent_task_id') = ?",
            (task.id,),
        )
    ) == 1
    assert len(
        [
            event
            for event in cp.task_history(task.id)
            if event.event_type == "task.lease_expired"
        ]
    ) == 1
    decision = cp.store.query_one(
        "SELECT expiry_finalization_decision, expiry_finalized_at "
        "FROM leases WHERE id = ?",
        (lease.id,),
    )
    assert decision["expiry_finalization_decision"]
    assert decision["expiry_finalized_at"]


def test_package_evidence_is_bound_to_current_assignment_attempt(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_agent(cp, "package-worker")
    task = cp.create_task("package mutation")
    package_id = "wp_scheduler_safety"
    now = "2026-01-01T00:00:00.000000+00:00"
    lease_id = "lease_scheduler_safety"
    expires_at = "2099-01-01T00:00:00.000000+00:00"
    cp.store.execute(
        "INSERT INTO work_packages ("
        "id, goal, state, current_plan_version, current_epoch, metadata, "
        "created_by, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (package_id, "safe parallel work", "draft", 0, 0, "{}", "human", now, now),
    )
    cp.store.execute(
        "INSERT INTO work_package_plan_versions ("
        "package_id, version, parent_version, definition, plan_digest, reason, "
        "created_by, created_at"
        ") VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
        (package_id, 1, "{}", "sha256:scheduler-plan", "initial", "human", now),
    )
    cp.store.execute(
        "INSERT INTO work_package_epochs ("
        "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, "
        "status, reason, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            package_id,
            1,
            1,
            "refs/heads/main",
            "a" * 40,
            "active",
            "initial",
            "human",
            now,
        ),
    )
    cp.store.execute(
        "UPDATE work_packages SET state = ?, current_plan_version = ?, "
        "current_epoch = ? WHERE id = ?",
        ("admitted", 1, 1, package_id),
    )
    cp.store.execute(
        "UPDATE work_packages SET state = ? WHERE id = ?",
        ("active", package_id),
    )
    cp.store.execute(
        "INSERT INTO work_package_task_links ("
        "task_id, package_id, plan_version, epoch, node_key, node_generation, "
        "declared_effects_digest, contract_digest, input_digest, node_state, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task.id,
            package_id,
            1,
            1,
            "mutation",
            1,
            "sha256:effects",
            "sha256:contract",
            "sha256:input",
            "planned",
            now,
        ),
    )
    cp.store.execute(
        "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
        ("ready", task.id),
    )
    cp.store.execute(
        "INSERT INTO leases (id, task_id, agent_id, expires_at, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lease_id, task.id, worker.id, expires_at, "active", now, now),
    )
    cp.store.execute(
        "INSERT INTO work_package_assignment_audit ("
        "lease_id, package_id, plan_version, epoch, node_key, task_id, agent_id, "
        "attempt_number, attempt_ref, attempt_base_ref, attempt_base_sha, "
        "declared_effects_digest, allocator, allocator_version, score, rationale, "
        "decision, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lease_id,
            package_id,
            1,
            1,
            "mutation",
            task.id,
            worker.id,
            1,
            "refs/mac/attempts/pkg-test/epoch-1/node/attempt-1/lease-test",
            "refs/heads/main",
            "a" * 40,
            "sha256:effects",
            "allocator",
            "v1",
            1.0,
            "eligible worker",
            "{}",
            now,
        ),
    )
    cp.store.execute(
        "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
        ("executing", task.id),
    )
    cp.store.execute(
        "UPDATE tasks SET state = ?, owner_agent_id = ?, lease_id = ?, "
        "leased_until = ?, attempt_count = ?, updated_at = ? "
        "WHERE id = ? AND state = ? AND lease_id IS NULL",
        (
            TaskState.CLAIMED.value,
            worker.id,
            lease_id,
            expires_at,
            1,
            now,
            task.id,
            TaskState.OPEN.value,
        ),
    )
    lease = cp.get_lease(lease_id)

    verification = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "remote_ref": (
                "refs/mac/attempts/pkg-test/epoch-1/node/attempt-1/lease-test"
            ),
            "pushed": True,
        },
    }
    wrong_ref = {
        **verification,
        "repo": {**verification["repo"], "remote_ref": "refs/heads/mutable-task"},
    }
    with pytest.raises(ValidationError, match="exact assigned attempt ref"):
        cp.add_evidence(
            task.id,
            "log",
            "artifact://wrong-package-attempt",
            "wrong attempt output",
            worker.id,
            lease_id=lease.id,
            metadata={"verification": wrong_ref},
            sync_beads=False,
        )
    assert cp.store.query_one(
        "SELECT COUNT(*) AS n FROM evidence WHERE task_id = ?", (task.id,)
    )["n"] == 0

    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://package-attempt",
        "attempt output",
        worker.id,
        lease_id=lease.id,
        metadata={"verification": verification},
        artifacts=[
            {
                "name": "worker-result.json",
                "artifact_type": "worker_result",
                "content_type": "application/json",
                "content_base64": base64.b64encode(b'{"status":"complete"}\n').decode(
                    "ascii"
                ),
            }
        ],
        sync_beads=False,
    )
    link = cp.store.query_one(
        "SELECT * FROM evidence_attempt_links WHERE evidence_id = ?",
        (evidence.id,),
    )
    assert link is not None
    assert link["task_id"] == task.id
    assert link["lease_id"] == lease.id
    assert link["agent_id"] == worker.id
    assert int(link["attempt_number"]) == 1
    assert link["attempt_ref"] == (
        "refs/mac/attempts/pkg-test/epoch-1/node/attempt-1/lease-test"
    )
    assert link["attempt_base_sha"] == "a" * 40
    assert link["declared_effects_digest"] == "sha256:effects"
    assert link["attempt_head_sha"] == "b" * 40
    assert str(link["artifact_digest"]).startswith("sha256:")
    assert len(str(link["artifact_digest"])) == len("sha256:") + 64
    assert link["observed_effects_digest"] is None
    assert int(link["protected_ref"]) == 1

    # The HTTP/worker submission boundary reaches the same atomic append path.
    # Controller-generated evidence/artifact ids and timestamps must not make
    # an otherwise identical attempt manifest address differently.
    _set_worker_identity_enforced(cp)
    monkeypatch.setattr(
        "mac.worker_credentials.package_worker_readiness",
        lambda _store, agent_id: {
            "ready": agent_id == worker.id,
            "principal_id": "credential-package-worker",
            "credential_version": 1,
            "token_fingerprint": "sha256:package-worker",
        },
    )
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "package-worker-token": {
                "scopes": ["agent", "write"],
                "agent_id": worker.id,
                "principal_kind": "worker",
                "client_id": "credential-package-worker",
                "worker_credential_version": 1,
                "credential_fingerprint": "sha256:package-worker",
            }
        },
    )
    with TestClient(app) as client:
        response = client.post(
            "/tasks/%s/evidence" % task.id,
            headers={"Authorization": "Bearer package-worker-token"},
            json={
                "kind": "log",
                "uri": "artifact://same-package-attempt-through-api",
                "summary": "same attempt output through API",
                "created_by": worker.id,
                "lease_id": lease.id,
                "metadata": {"verification": verification},
                "artifacts": [
                    {
                        "name": "worker-result.json",
                        "artifact_type": "worker_result",
                        "content_type": "application/json",
                        "content_base64": base64.b64encode(
                            b'{"status":"complete"}\n'
                        ).decode("ascii"),
                    }
                ],
            },
        )
    assert response.status_code == 200, response.text
    api_link = cp.store.query_one(
        "SELECT * FROM evidence_attempt_links WHERE evidence_id = ?",
        (response.json()["id"],),
    )
    assert api_link is not None
    assert api_link["artifact_digest"] == link["artifact_digest"]


def test_renew_cannot_resurrect_active_but_expired_lease(cp: ControlPlane) -> None:
    worker = _register_agent(cp, "renew-expired")
    task = cp.create_task("renewal expiry")
    _claimed, lease = cp.claim_task(task.id, worker.id, sync_beads=False)
    expired_at = "2000-01-01T00:00:00.000000+00:00"
    cp.store.execute(
        "UPDATE leases SET expires_at = ? WHERE id = ?",
        (expired_at, lease.id),
    )
    cp.store.execute(
        "UPDATE tasks SET leased_until = ? WHERE id = ?",
        (expired_at, task.id),
    )
    before_lease = cp.get_lease(lease.id).to_dict()
    before_task = cp.get_task(task.id).to_dict()

    with pytest.raises(ValidationError, match="expired leases cannot be renewed"):
        cp.renew_lease(lease.id, worker.id, lease_seconds=120)

    assert cp.get_lease(lease.id).to_dict() == before_lease
    assert cp.get_task(task.id).to_dict() == before_task


@pytest.mark.parametrize("lease_seconds", [-1, 0, 31_536_000])
def test_claim_rejects_unsafe_lease_ttl_without_burning_attempt(
    cp: ControlPlane,
    lease_seconds: int,
) -> None:
    worker = _register_agent(cp, "ttl-worker-%s" % lease_seconds)
    task = cp.create_task("bounded ttl")
    with pytest.raises(ValidationError, match="lease_seconds must be between"):
        cp.claim_task(
            task.id,
            worker.id,
            lease_seconds=lease_seconds,
            sync_beads=False,
        )
    fresh = cp.get_task(task.id)
    assert fresh.attempt_count == 0
    assert fresh.lease_id is None


@pytest.mark.parametrize("lease_seconds", [-1, 0, 31_536_000])
def test_renew_rejects_unsafe_lease_ttl_without_changing_deadline(
    cp: ControlPlane,
    lease_seconds: int,
) -> None:
    worker = _register_agent(cp, "renew-ttl-%s" % lease_seconds)
    task = cp.create_task("bounded renew ttl")
    _claimed, lease = cp.claim_task(task.id, worker.id, sync_beads=False)
    before = cp.get_lease(lease.id).to_dict()
    with pytest.raises(ValidationError, match="lease_seconds must be between"):
        cp.renew_lease(lease.id, worker.id, lease_seconds=lease_seconds)
    assert cp.get_lease(lease.id).to_dict() == before


def test_claim_api_rejects_unsafe_ttl(cp: ControlPlane) -> None:
    worker = _register_agent(cp, "api-ttl-worker")
    task = cp.create_task("api bounded ttl")
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "worker-token": {"scopes": ["write"], "agent_id": worker.id},
        },
    )
    with TestClient(app) as client:
        responses = [
            client.post(
                "/tasks/%s/claim" % task.id,
                headers={"Authorization": "Bearer worker-token"},
                params={"agent_id": worker.id, "lease_seconds": ttl},
            )
            for ttl in (-1, 0, 31_536_000)
        ]
    assert [response.status_code for response in responses] == [400, 400, 400]
    assert cp.get_task(task.id).attempt_count == 0


@pytest.mark.parametrize(
    "mutation, expected_reason",
    [
        ("role_caps", "capabilities_missing"),
        ("soul_roles", "role_not_accepted_by_soul"),
    ],
)
def test_claim_rechecks_role_and_soul_in_locked_snapshot(
    cp: ControlPlane,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_reason: str,
) -> None:
    from tests.conftest import bind_soul

    role = cp.roles.create_role(
        slug="qa",
        name="QA",
        description="quality",
        system_prompt="test",
        level="ic",
        required_capabilities=["python"],
    )
    soul_id = bind_soul(cp, persona_name="QA Soul", allowed_role_slugs=["qa"])
    machine = cp.register_machine("role-race-host")
    worker = cp.register_agent(
        machine.id,
        "role-race-worker",
        capabilities=["python"],
        hermes_instance_id=soul_id,
    )
    cp.roles.assign_role(worker.id, role.id)
    task = cp.create_task("role race", required_capabilities=["python"])

    preflight_complete = threading.Event()
    resume = threading.Event()
    original = cp._agent_available_for

    def pause_after_preflight(agent, candidate, *, allow_cooperative_reuse=False):
        result = original(
            agent,
            candidate,
            allow_cooperative_reuse=allow_cooperative_reuse,
        )
        assert result is True
        preflight_complete.set()
        assert resume.wait(timeout=5)
        return result

    monkeypatch.setattr(cp, "_agent_available_for", pause_after_preflight)
    outcome = []

    def claim() -> None:
        try:
            cp.claim_task(task.id, worker.id, sync_beads=False)
            outcome.append("unexpected-success")
        except ValidationError as exc:
            outcome.append(str(exc))

    thread = threading.Thread(target=claim)
    thread.start()
    assert preflight_complete.wait(timeout=5)
    if mutation == "role_caps":
        cp.store.execute(
            "UPDATE agent_roles SET required_capabilities = ? WHERE id = ?",
            ('["gpu"]', role.id),
        )
    else:
        instance = cp.store.query_one(
            "SELECT persona_id FROM persona_instances WHERE id = ?",
            (soul_id,),
        )
        cp.store.execute(
            "UPDATE personas SET metadata = ? WHERE id = ?",
            ('{"role_slugs":["other"]}', instance["persona_id"]),
        )
    resume.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert expected_reason in outcome[0]
    assert cp.get_task(task.id).lease_id is None


@pytest.mark.postgres
def test_postgres_concurrent_capacity_claims_serialize_across_control_planes(
    postgres_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_secret = "postgres-concurrency-test-secret-32-bytes"
    first_cp = ControlPlane(postgres_store, secret_key=test_secret)
    second_cp = ControlPlane(postgres_store, secret_key=test_secret)
    worker = _register_agent(first_cp, "postgres-one-slot", capacity=1)
    tasks = [first_cp.create_task("postgres-lane-a"), first_cp.create_task("postgres-lane-b")]
    barrier = threading.Barrier(2)

    def simultaneous_preflight(agent, task, *, allow_cooperative_reuse=False):
        barrier.wait(timeout=10)
        return True

    monkeypatch.setattr(first_cp, "_agent_available_for", simultaneous_preflight)
    monkeypatch.setattr(second_cp, "_agent_available_for", simultaneous_preflight)
    results = []
    lock = threading.Lock()

    def claim(control_plane: ControlPlane, task_id: str) -> None:
        try:
            _task, lease = control_plane.claim_task(
                task_id,
                worker.id,
                sync_beads=False,
            )
            result = ("ok", lease.id)
        except (TransitionError, ValidationError) as exc:
            result = ("error", str(exc))
        with lock:
            results.append(result)

    threads = [
        threading.Thread(target=claim, args=(first_cp, tasks[0].id)),
        threading.Thread(target=claim, args=(second_cp, tasks[1].id)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert [item[0] for item in results].count("ok") == 1
    assert [item[0] for item in results].count("error") == 1
    row = postgres_store.query_one(
        "SELECT COUNT(*) AS count FROM leases WHERE agent_id = ? AND status = ?",
        (worker.id, "active"),
    )
    assert int(row["count"]) == 1


def test_bound_worker_cannot_mutate_decompose_delete_or_annotate_peer_task(
    cp: ControlPlane,
) -> None:
    owner = _register_agent(cp, "mutation-owner")
    attacker = _register_agent(cp, "mutation-attacker")
    task = cp.create_task("peer-owned task")
    _claimed, lease = cp.claim_task(task.id, owner.id, sync_beads=False)
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "attacker-token": {
                "scopes": ["write"],
                "agent_id": attacker.id,
            }
        },
    )
    headers = {"Authorization": "Bearer attacker-token"}

    with TestClient(app) as client:
        update = client.put(
            "/tasks/%s" % task.id,
            headers=headers,
            json={"title": "attacker title", "actor": attacker.id},
        )
        delete = client.delete(
            "/tasks/%s" % task.id,
            headers=headers,
            params={"actor": attacker.id, "force": True},
        )
        children = client.post(
            "/tasks/%s/children" % task.id,
            headers=headers,
            json={
                "actor": attacker.id,
                "lease_id": lease.id,
                "children": [{"title": "attacker child"}],
            },
        )
        activity = client.post(
            "/tasks/%s/activity" % task.id,
            headers=headers,
            json={
                "phase": "worker",
                "actor": attacker.id,
                "summary": "attacker note",
                "lease_id": lease.id,
            },
        )

    assert [
        update.status_code,
        delete.status_code,
        children.status_code,
        activity.status_code,
    ] == [403, 403, 403, 403]
    fresh = cp.get_task(task.id)
    assert fresh.title == "peer-owned task"
    assert fresh.state == TaskState.CLAIMED.value
    assert fresh.metadata.get("activity") is None
    assert cp.list_tasks(project=task.project) == [fresh]
