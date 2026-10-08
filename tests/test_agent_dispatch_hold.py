"""Unit + integration tests for agent dispatch-hold enforcement.

Covers:
- services.set_agent_dispatch_hold persists all three hold fields
- services.clear_agent_dispatch_hold resets all three hold fields
- _agent_availability_for_task returns (False, "agent_dispatch_held") for held agents
- A non-held agent is still dispatched normally
- Hold survives a round-trip through the DB layer
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from mac.agentbus_control import REFLECT_REQUEST_TOPIC
from mac.api import create_app
from mac.models import NotFoundError, ValidationError, parse_time, utcnow
from mac.services import ControlPlane


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_cp() -> ControlPlane:
    return ControlPlane.in_memory()


def _register_agent(cp: ControlPlane, name: str = "worker-1"):
    machine = cp.register_machine(f"{name}-host", resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, name)


def _expire_claim(cp: ControlPlane, task_id: str, lease_id: str) -> None:
    expired_at = "2000-01-01T00:00:00+00:00"
    cp.store.execute(
        "UPDATE leases SET expires_at = ? WHERE id = ?",
        (expired_at, lease_id),
    )
    cp.store.execute(
        "UPDATE tasks SET leased_until = ? WHERE id = ?",
        (expired_at, task_id),
    )


def test_set_dispatch_hold_persists_fields():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-alpha")

    held = cp.set_agent_dispatch_hold(agent.id, "manual quarantine")

    assert held.dispatch_hold is True
    assert held.dispatch_hold_reason == "manual quarantine"
    assert held.dispatch_hold_at is not None


def test_set_dispatch_hold_round_trips_via_get_agent():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-beta")

    cp.set_agent_dispatch_hold(agent.id, "zombie suspected")
    fetched = cp.get_agent(agent.id)

    assert fetched.dispatch_hold is True
    assert fetched.dispatch_hold_reason == "zombie suspected"


def test_set_dispatch_hold_raises_for_unknown_agent():
    cp = _make_cp()
    with pytest.raises(NotFoundError):
        cp.set_agent_dispatch_hold("agent_nonexistent_id", "test")


def test_set_dispatch_hold_rejects_blank_reason():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-blank-reason")

    with pytest.raises(ValidationError, match="reason is required"):
        cp.set_agent_dispatch_hold(agent.id, "   ")


def test_dispatch_hold_acquire_and_replace_are_compare_and_swap():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-cas-acquire")

    changed, held = cp.acquire_agent_dispatch_hold(
        agent.id,
        "deployment-one",
        expected_dispatch_hold=False,
    )
    assert changed is True
    assert held.dispatch_hold is True
    assert held.dispatch_hold_reason == "deployment-one"
    deployment_one_hold_at = held.dispatch_hold_at
    deployment_one_updated_at = held.updated_at

    changed, held = cp.acquire_agent_dispatch_hold(
        agent.id,
        "stale-deployment",
        expected_dispatch_hold=False,
    )
    assert changed is False
    assert held.dispatch_hold_reason == "deployment-one"
    assert held.dispatch_hold_at == deployment_one_hold_at
    assert held.updated_at == deployment_one_updated_at

    changed, held = cp.acquire_agent_dispatch_hold(
        agent.id,
        "deployment-two",
        expected_dispatch_hold=True,
        expected_reason="deployment-one",
    )
    assert changed is True
    assert held.dispatch_hold_reason == "deployment-two"

    changed, held = cp.acquire_agent_dispatch_hold(
        agent.id,
        "late-deployment-one",
        expected_dispatch_hold=True,
        expected_reason="deployment-one",
    )
    assert changed is False
    assert held.dispatch_hold_reason == "deployment-two"


def test_dispatch_hold_acquire_validates_expected_state_and_agent():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-cas-validation")

    with pytest.raises(ValidationError, match="must be omitted"):
        cp.acquire_agent_dispatch_hold(
            agent.id,
            "deployment",
            expected_dispatch_hold=False,
            expected_reason="unexpected",
        )
    with pytest.raises(ValidationError, match="is required when a hold is expected"):
        cp.acquire_agent_dispatch_hold(
            agent.id,
            "deployment",
            expected_dispatch_hold=True,
        )
    with pytest.raises(NotFoundError):
        cp.acquire_agent_dispatch_hold(
            "agent_nonexistent_id",
            "deployment",
            expected_dispatch_hold=False,
        )


# ---------------------------------------------------------------------------
# clear_agent_dispatch_hold
# ---------------------------------------------------------------------------


def test_clear_dispatch_hold_resets_all_fields():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-gamma")
    cp.set_agent_dispatch_hold(agent.id, "held for testing")

    resumed = cp.clear_agent_dispatch_hold(agent.id)

    assert resumed.dispatch_hold is False
    assert resumed.dispatch_hold_reason is None
    assert resumed.dispatch_hold_at is None


def test_clear_dispatch_hold_round_trips_via_get_agent():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-delta")
    cp.set_agent_dispatch_hold(agent.id, "held")
    cp.clear_agent_dispatch_hold(agent.id)

    fetched = cp.get_agent(agent.id)
    assert fetched.dispatch_hold is False
    assert fetched.dispatch_hold_reason is None


def test_clear_dispatch_hold_is_idempotent_when_not_held():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-epsilon")
    # agent was never held; clear should not raise
    resumed = cp.clear_agent_dispatch_hold(agent.id)
    assert resumed.dispatch_hold is False


def test_clear_dispatch_hold_raises_for_unknown_agent():
    cp = _make_cp()
    with pytest.raises(NotFoundError):
        cp.clear_agent_dispatch_hold("agent_nonexistent_id")


def test_dispatch_hold_release_requires_exact_current_reason():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-cas-release")
    original = cp.set_agent_dispatch_hold(agent.id, "deployment-two")

    released, held = cp.release_agent_dispatch_hold(agent.id, "deployment-one")
    assert released is False
    assert held.dispatch_hold is True
    assert held.dispatch_hold_reason == "deployment-two"
    assert held.dispatch_hold_at == original.dispatch_hold_at
    assert held.updated_at == original.updated_at

    released, resumed = cp.release_agent_dispatch_hold(agent.id, "deployment-two")
    assert released is True
    assert resumed.dispatch_hold is False
    assert resumed.dispatch_hold_reason is None
    assert resumed.dispatch_hold_at is None

    released, resumed = cp.release_agent_dispatch_hold(agent.id, "deployment-two")
    assert released is False
    assert resumed.dispatch_hold is False


def test_dispatch_hold_release_validates_reason_and_agent():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-release-validation")

    with pytest.raises(ValidationError, match="reason is required"):
        cp.release_agent_dispatch_hold(agent.id, "  ")
    with pytest.raises(NotFoundError):
        cp.release_agent_dispatch_hold("agent_nonexistent_id", "deployment")


def test_dispatch_hold_cas_http_routes_return_result_and_full_agent():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-cas-http")
    client = TestClient(create_app(control_plane=cp))

    acquired = client.post(
        "/agents/%s/dispatch-hold/acquire" % agent.id,
        json={
            "reason": "http-deployment",
            "expected_dispatch_hold": False,
        },
    )
    assert acquired.status_code == 200
    assert acquired.json()["changed"] is True
    assert acquired.json()["agent"]["id"] == agent.id
    assert acquired.json()["agent"]["dispatch_hold_reason"] == "http-deployment"

    stale = client.post(
        "/agents/%s/dispatch-hold/acquire" % agent.id,
        json={
            "reason": "stale-http-deployment",
            "expected_dispatch_hold": False,
        },
    )
    assert stale.status_code == 200
    assert stale.json()["changed"] is False
    assert stale.json()["agent"]["dispatch_hold_reason"] == "http-deployment"

    wrong_release = client.post(
        "/agents/%s/dispatch-hold/release" % agent.id,
        json={"reason": "wrong-deployment"},
    )
    assert wrong_release.status_code == 200
    assert wrong_release.json()["released"] is False
    assert wrong_release.json()["agent"]["dispatch_hold"] is True

    released = client.post(
        "/agents/%s/dispatch-hold/release" % agent.id,
        json={"reason": "http-deployment"},
    )
    assert released.status_code == 200
    assert released.json()["released"] is True
    assert released.json()["agent"]["id"] == agent.id
    assert released.json()["agent"]["dispatch_hold"] is False


def test_dispatch_hold_cas_http_routes_reject_tenant_bound_principals():
    cp = _make_cp()
    tenant = cp.register_tenant("dispatch-hold-tenant")
    agent = _register_agent(cp, "agent-cas-http-authority")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "tenant-writer": {
                    "scopes": ["write"],
                    "tenant_id": tenant.id,
                },
                "admin": ["admin"],
            },
        )
    )
    path = "/agents/%s/dispatch-hold/acquire" % agent.id
    payload = {
        "reason": "authorized-deployment",
        "expected_dispatch_hold": False,
    }

    rejected = client.post(
        path,
        headers={"Authorization": "Bearer tenant-writer"},
        json=payload,
    )
    assert rejected.status_code == 403
    acquired = client.post(
        path,
        headers={"Authorization": "Bearer admin"},
        json=payload,
    )
    assert acquired.status_code == 200
    assert acquired.json()["changed"] is True

    release_path = "/agents/%s/dispatch-hold/release" % agent.id
    rejected = client.post(
        release_path,
        headers={"Authorization": "Bearer tenant-writer"},
        json={"reason": "authorized-deployment"},
    )
    assert rejected.status_code == 403
    released = client.post(
        release_path,
        headers={"Authorization": "Bearer admin"},
        json={"reason": "authorized-deployment"},
    )
    assert released.status_code == 200
    assert released.json()["released"] is True


def test_worker_principal_cannot_mutate_any_dispatch_hold_route():
    cp = _make_cp()
    worker = _register_agent(cp, "ordinary-worker-principal")
    target = _register_agent(cp, "operator-held-peer")
    cp.set_agent_dispatch_hold(target.id, "operator-owned-freeze")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "worker": {
                    "scopes": ["agent", "dispatch", "read", "write"],
                    "tenant_id": None,
                    "agent_id": worker.id,
                    "principal_kind": "worker",
                }
            },
        )
    )
    headers = {"Authorization": "Bearer worker"}
    base = "/agents/%s/dispatch-hold" % target.id

    attempts = (
        client.post(base, headers=headers, json={"reason": "worker-replacement"}),
        client.delete(base, headers=headers),
        client.post(
            base + "/acquire",
            headers=headers,
            json={
                "reason": "worker-cas-replacement",
                "expected_dispatch_hold": True,
                "expected_reason": "operator-owned-freeze",
            },
        ),
        client.post(
            base + "/release",
            headers=headers,
            json={"reason": "operator-owned-freeze"},
        ),
    )

    assert [response.status_code for response in attempts] == [403, 403, 403, 403]
    unchanged = cp.get_agent(target.id)
    assert unchanged.dispatch_hold is True
    assert unchanged.dispatch_hold_reason == "operator-owned-freeze"


def test_peer_worker_cannot_mutate_operator_agent_control_routes():
    cp = _make_cp()
    worker = _register_agent(cp, "ordinary-control-peer")
    target = _register_agent(cp, "new-worker-behind-registration-barrier")
    original = cp.get_agent(target.id)
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "worker": {
                    "scopes": ["agent", "dispatch", "read", "write"],
                    "tenant_id": None,
                    "agent_id": worker.id,
                    "principal_kind": "worker",
                }
            },
        )
    )
    headers = {"Authorization": "Bearer worker"}

    attempts = (
        client.post(
            "/agents/bulk",
            headers=headers,
            json={"agent_ids": [target.id], "status": "idle"},
        ),
        client.put(
            "/agents/%s" % target.id,
            headers=headers,
            json={"status": "idle", "health_status": "healthy"},
        ),
        client.post("/agents/%s/disable" % target.id, headers=headers),
        client.delete("/agents/%s" % target.id, headers=headers),
    )

    assert [response.status_code for response in attempts] == [403, 403, 403, 403]
    unchanged = cp.get_agent(target.id)
    assert unchanged.status == original.status
    assert unchanged.health_status == original.health_status
    assert unchanged.deleted_at is None


# ---------------------------------------------------------------------------
# _agent_availability_for_task — dispatch hold guard
# ---------------------------------------------------------------------------


def test_held_agent_skipped_with_agent_dispatch_held_reason():
    cp = _make_cp()
    from mac.models import AgentStatus

    machine = cp.register_machine("hold-host", resources={"cpu": 4, "memory_gb": 8})
    agent = cp.register_agent(machine.id, "hold-agent")
    # Put agent in IDLE/HEALTHY so all other availability checks pass
    cp.update_agent(agent.id, status=AgentStatus.IDLE.value)

    task = cp.create_task("dispatch test task")
    cp.set_agent_dispatch_hold(agent.id, "test hold")
    agent = cp.get_agent(agent.id)

    available, reason = cp._agent_availability_for_task(agent, task)
    assert available is False
    assert reason == "agent_dispatch_held"


def test_non_held_agent_availability_not_blocked_by_dispatch_hold():
    """An agent with dispatch_hold=False must not be refused for that reason."""
    cp = _make_cp()
    from mac.models import AgentStatus

    machine = cp.register_machine("free-host", resources={"cpu": 4, "memory_gb": 8})
    agent = cp.register_agent(machine.id, "free-agent")
    cp.update_agent(agent.id, status=AgentStatus.IDLE.value)

    task = cp.create_task("free dispatch task")
    agent = cp.get_agent(agent.id)

    available, reason = cp._agent_availability_for_task(agent, task)
    # The agent should not be refused for dispatch_hold; it may pass or fail on
    # other checks but must NOT return the dispatch_held reason.
    assert reason != "agent_dispatch_held"


def test_reregister_preserves_existing_deployment_drain():
    cp = _make_cp()
    machine = cp.register_machine("draining-reregister-host")
    agent = cp.register_agent(
        machine.id,
        "draining-reregister-agent",
        agent_id="agent_draining_reregister",
    )
    cp.heartbeat_agent(agent.id, status="draining", health_status="degraded")

    refreshed = cp.register_agent(
        machine.id,
        "draining-reregister-agent",
        agent_id=agent.id,
        capabilities=["python"],
        resources={"generation_candidate": "new"},
        actor=agent.id,
    )

    assert refreshed.status == "draining"
    assert refreshed.health_status == "degraded"
    assert refreshed.resources["generation_candidate"] == "new"


def test_registration_can_atomically_enter_deployment_drain():
    cp = _make_cp()
    machine = cp.register_machine("atomic-drain-host")
    agent = cp.register_agent(
        machine.id,
        "atomic-drain-agent",
        agent_id="agent_atomic_drain",
        status="draining",
        health_status="degraded",
        resources={"deployment_generation": "generation-1"},
    )

    assert agent.status == "draining"
    assert agent.health_status == "degraded"
    assert agent.resources["deployment_generation"] == "generation-1"


def test_registration_rejects_non_barrier_status_override():
    cp = _make_cp()
    machine = cp.register_machine("invalid-registration-status-host")

    with pytest.raises(ValidationError, match="only request the draining barrier"):
        cp.register_agent(
            machine.id,
            "invalid-registration-status-agent",
            status="idle",
        )


def test_status_only_heartbeat_cannot_inherit_prior_deployment_generation():
    cp = _make_cp()
    machine = cp.register_machine("generation-heartbeat-host")
    agent = cp.register_agent(
        machine.id,
        "generation-heartbeat-agent",
        agent_id="agent_generation_heartbeat",
        resources={"deployment_generation": "old-generation", "capacity": 2},
    )
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "worker": {
                    "scopes": ["agent", "dispatch", "read", "write"],
                    "agent_id": agent.id,
                    "principal_kind": "worker",
                }
            },
        )
    )

    response = client.post(
        f"/agents/{agent.id}/heartbeat",
        headers={"Authorization": "Bearer worker"},
        json={"status": "idle"},
    )

    assert response.status_code == 200
    refreshed = cp.get_agent(agent.id)
    assert refreshed.resources["capacity"] == 2
    assert "deployment_generation" not in refreshed.resources


# ---------------------------------------------------------------------------
# hold/resume round-trip: hold then clear, check dispatch eligibility restored
# ---------------------------------------------------------------------------


def test_hold_then_resume_restores_availability():
    cp = _make_cp()
    from mac.models import AgentStatus

    machine = cp.register_machine("roundtrip-host", resources={"cpu": 4, "memory_gb": 8})
    agent = cp.register_agent(machine.id, "roundtrip-agent")
    cp.update_agent(agent.id, status=AgentStatus.IDLE.value)
    task = cp.create_task("roundtrip task")

    # Hold — must be skipped
    cp.set_agent_dispatch_hold(agent.id, "roundtrip hold")
    agent = cp.get_agent(agent.id)
    available, reason = cp._agent_availability_for_task(agent, task)
    assert available is False and reason == "agent_dispatch_held"

    # Resume — must no longer be refused for hold
    cp.clear_agent_dispatch_hold(agent.id)
    agent = cp.get_agent(agent.id)
    _, reason_after = cp._agent_availability_for_task(agent, task)
    assert reason_after != "agent_dispatch_held"


def test_fleet_update_hold_cycle_heartbeats_and_claims_without_release_epochs():
    """The worker path fleet-update drives, end to end over HTTP.

    Release epochs used to fence claim, heartbeat and credential changes with
    an "is this agent reserved by an open epoch" check. Epochs and their tables
    are gone, so a held worker must still heartbeat, and once its hold is
    released it must claim and heartbeat exactly as if no epoch ever existed.
    """
    cp = _make_cp()
    agent = _register_agent(cp, "fleet-update-worker")
    client = TestClient(create_app(control_plane=cp))
    for table in ("fleet_release_epochs", "fleet_release_epoch_agents", "fleet_upgrades"):
        assert cp.store.query_one("SELECT to_regclass(?) AS rel", (table,))["rel"] is None
    reason = "fleet-update 0123456789ab"
    task = cp.create_task("work queued while the worker is updated")

    held = client.post("/agents/%s/dispatch-hold" % agent.id, json={"reason": reason})
    assert held.status_code == 200
    beat = client.post(
        "/agents/%s/heartbeat" % agent.id,
        json={"status": "idle", "health_status": "healthy"},
    )
    assert beat.status_code == 200
    assert beat.json()["dispatch_hold_reason"] == reason
    refused = client.post("/agents/%s/claim-next" % agent.id, json={})
    assert refused.status_code == 200
    assert cp.get_task(task.id).state == "open"

    released = client.post("/agents/%s/dispatch-hold/release" % agent.id, json={"reason": reason})
    assert released.status_code == 200
    assert released.json()["released"] is True

    claimed = client.post(
        "/tasks/%s/claim" % task.id, params={"agent_id": agent.id, "lease_seconds": 60}
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["lease"]["agent_id"] == agent.id
    beat = client.post(
        "/agents/%s/heartbeat" % agent.id,
        json={"status": "busy", "health_status": "healthy"},
    )
    assert beat.status_code == 200, beat.text
    assert beat.json()["dispatch_hold"] is False


def test_two_zero_telemetry_expiries_auto_quarantine_agent(monkeypatch):
    monkeypatch.setenv("MAC_AGENT_QUARANTINE_THRESHOLD", "2")
    cp = _make_cp()
    agent = _register_agent(cp, "auto-quarantine-agent")

    for index in range(2):
        task = cp.create_task("no telemetry %d" % index)
        _, lease = cp.claim_task(task.id, agent.id)
        _expire_claim(cp, task.id, lease.id)
        cp.expire_leases(now=utcnow())

    held = cp.get_agent(agent.id)
    assert held.dispatch_hold is True
    assert held.dispatch_hold_reason == "auto_quarantine:consecutive_expiries_no_telemetry"
    assert held.consecutive_lease_expiries_no_telemetry == 2
    streams = cp.list_agentbus_streams(agent_id=agent.id)
    assert any(stream.topic == REFLECT_REQUEST_TOPIC for stream in streams)


def test_virtual_agent_lease_expiry_never_quarantines(monkeypatch):
    """A virtual, hub-driven agent (e.g. the hub-reviewer) has no
    worker process and by design emits no executor telemetry, so its expired
    review leases must NOT be counted as zombie signals or quarantine it."""
    monkeypatch.setenv("MAC_AGENT_QUARANTINE_THRESHOLD", "2")
    cp = _make_cp()
    machine = cp.register_machine("virtual-review-host", resources={"cpu": 1, "memory_gb": 1})
    agent = cp.register_agent(
        machine.id,
        "hub-reviewer",
        capabilities=["review"],
        resources={"virtual": True, "review": {"mode": "worker_evidence"}},
    )

    # Well past the threshold: a real agent would be quarantined after 2.
    for index in range(4):
        task = cp.create_task("virtual review %d" % index)
        _, lease = cp.claim_task(task.id, agent.id)
        _expire_claim(cp, task.id, lease.id)
        cp.expire_leases(now=utcnow())

    refreshed = cp.get_agent(agent.id)
    assert refreshed.dispatch_hold is False
    assert refreshed.dispatch_hold_reason is None
    assert refreshed.consecutive_lease_expiries_no_telemetry == 0


def test_expired_lease_telemetry_resets_no_telemetry_counter(monkeypatch):
    monkeypatch.setenv("MAC_AGENT_QUARANTINE_THRESHOLD", "2")
    cp = _make_cp()
    agent = _register_agent(cp, "telemetry-agent")

    first = cp.create_task("missing telemetry")
    _, first_lease = cp.claim_task(first.id, agent.id)
    _expire_claim(cp, first.id, first_lease.id)
    cp.expire_leases(now=utcnow())
    assert cp.get_agent(agent.id).consecutive_lease_expiries_no_telemetry == 1

    second = cp.create_task("has telemetry")
    _, second_lease = cp.claim_task(second.id, agent.id)
    cp.record_log(
        "executor.started",
        layer="executor",
        source="mac-hermes-task-executor",
        subject_type="task",
        subject_id=second.id,
        detail={"agent_id": agent.id},
    )
    _expire_claim(cp, second.id, second_lease.id)
    cp.expire_leases(now=utcnow())

    refreshed = cp.get_agent(agent.id)
    assert refreshed.consecutive_lease_expiries_no_telemetry == 0
    assert refreshed.dispatch_hold is False


def test_evidence_row_resets_no_telemetry_counter(monkeypatch):
    monkeypatch.setenv("MAC_AGENT_QUARANTINE_THRESHOLD", "2")
    cp = _make_cp()
    agent = _register_agent(cp, "evidence-agent")

    first = cp.create_task("missing evidence")
    _, first_lease = cp.claim_task(first.id, agent.id)
    _expire_claim(cp, first.id, first_lease.id)
    cp.expire_leases(now=utcnow())
    assert cp.get_agent(agent.id).consecutive_lease_expiries_no_telemetry == 1

    second = cp.create_task("has evidence")
    _, second_lease = cp.claim_task(second.id, agent.id)
    cp.add_evidence(
        second.id,
        "log",
        "artifact://attempt",
        "attempt log",
        agent.id,
        lease_id=second_lease.id,
    )
    _expire_claim(cp, second.id, second_lease.id)
    cp.expire_leases(now=utcnow())

    refreshed = cp.get_agent(agent.id)
    assert refreshed.consecutive_lease_expiries_no_telemetry == 0
    assert refreshed.dispatch_hold is False


def test_attempt_telemetry_tolerates_bounded_database_clock_skew():
    cp = _make_cp()
    agent = _register_agent(cp, "skewed-telemetry-agent")
    task = cp.create_task("skewed telemetry")
    _, lease = cp.claim_task(task.id, agent.id)
    event = cp.record_log(
        "executor.started",
        layer="executor",
        source="mac-task-executor",
        subject_type="task",
        subject_id=task.id,
        detail={"agent_id": agent.id},
    )
    cp.store.execute(
        "UPDATE observability_events SET created_at = ? WHERE id = ?",
        (
            (parse_time(lease.created_at) - timedelta(seconds=1)).isoformat(
                timespec="microseconds"
            ),
            event.id,
        ),
    )

    assert cp._lease_attempt_telemetry_exists(lease) is True


def test_attempt_telemetry_rejects_old_same_task_event():
    cp = _make_cp()
    agent = _register_agent(cp, "old-telemetry-agent")
    task = cp.create_task("old telemetry")
    _, lease = cp.claim_task(task.id, agent.id)
    event = cp.record_log(
        "executor.started",
        layer="executor",
        source="mac-task-executor",
        subject_type="task",
        subject_id=task.id,
        detail={"agent_id": agent.id},
    )
    cp.store.execute(
        "UPDATE observability_events SET created_at = ? WHERE id = ?",
        (
            (parse_time(lease.created_at) - timedelta(seconds=6)).isoformat(
                timespec="microseconds"
            ),
            event.id,
        ),
    )

    assert cp._lease_attempt_telemetry_exists(lease) is False
