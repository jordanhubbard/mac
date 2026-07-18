"""Unit + integration tests for agent dispatch-hold enforcement.

Covers:
- services.set_agent_dispatch_hold persists all three hold fields
- services.clear_agent_dispatch_hold resets all three hold fields
- _agent_availability_for_task returns (False, "agent_dispatch_held") for held agents
- A non-held agent is still dispatched normally
- Hold survives a round-trip through the DB layer
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mac.agentbus_control import REFLECT_REQUEST_TOPIC
from mac.api import create_app
from mac.models import NotFoundError, ValidationError, utcnow
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


# ---------------------------------------------------------------------------
# set_agent_dispatch_hold
# ---------------------------------------------------------------------------


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


@pytest.mark.parametrize("holds", [None, "sample", [("agent-only",)]])
def test_dispatch_hold_batch_release_rejects_malformed_hold_iterables(holds):
    cp = _make_cp()

    with pytest.raises(ValidationError, match="iterable|pairs"):
        cp.release_agent_dispatch_holds_batch(
            holds,
            epoch_id="epoch-malformed-input",
        )


def test_dispatch_hold_batch_release_is_atomic_when_any_reason_is_stale():
    cp = _make_cp()
    first = _register_agent(cp, "agent-epoch-first")
    second = _register_agent(cp, "agent-epoch-second")
    cp.set_agent_dispatch_hold(first.id, "epoch-first")
    cp.set_agent_dispatch_hold(second.id, "successor-replaced-epoch")
    before_events = cp.store.query_one(
        "SELECT COUNT(*) AS count FROM agent_lifecycle_events "
        "WHERE event_type = 'agent.dispatch_hold_epoch_released'"
    )["count"]

    # The first UPDATE succeeds before the stale second reason is detected.
    # The transaction must nevertheless roll back the first update and its
    # lifecycle/observability evidence, rather than exposing a partial cohort.
    with pytest.raises(
        ValidationError,
        match="lost dispatch-hold ownership for %s" % second.id,
    ):
        cp.release_agent_dispatch_holds_batch(
            (
                (first.id, "epoch-first"),
                (second.id, "stale-epoch-second"),
            ),
            epoch_id="epoch-atomic-rollback",
        )

    first_after = cp.get_agent(first.id)
    second_after = cp.get_agent(second.id)
    assert first_after.dispatch_hold is True
    assert first_after.dispatch_hold_reason == "epoch-first"
    assert second_after.dispatch_hold is True
    assert second_after.dispatch_hold_reason == "successor-replaced-epoch"
    assert (
        cp.store.query_one(
            "SELECT COUNT(*) AS count FROM agent_lifecycle_events "
            "WHERE event_type = 'agent.dispatch_hold_epoch_released'"
        )["count"]
        == before_events
    )

    recovered = cp.release_agent_dispatch_holds_batch(
        (
            (first.id, "epoch-first"),
            (second.id, "successor-replaced-epoch"),
        ),
        epoch_id="epoch-atomic-rollback",
    )
    assert {agent.id for agent in recovered} == {first.id, second.id}
    assert all(agent.dispatch_hold is False for agent in recovered)


def test_dispatch_hold_batch_release_exact_epoch_retry_returns_committed_receipt():
    cp = _make_cp()
    first = _register_agent(cp, "agent-epoch-retry-first")
    second = _register_agent(cp, "agent-epoch-retry-second")
    cp.set_agent_dispatch_hold(first.id, "epoch-retry-first")
    cp.set_agent_dispatch_hold(second.id, "epoch-retry-second")
    holds = (
        (first.id, "epoch-retry-first"),
        (second.id, "epoch-retry-second"),
    )

    initial = cp.release_agent_dispatch_holds_batch(
        holds,
        epoch_id="epoch-idempotent-retry",
    )
    receipt_count = cp.store.query_one(
        "SELECT COUNT(*) AS count FROM agent_lifecycle_events "
        "WHERE event_type = 'agent.dispatch_hold_epoch_released'"
    )["count"]

    # Simulate the controller losing the HTTP response after the transaction
    # committed. Reversing the input proves epoch identity is the exact set,
    # not incidental request ordering.
    replayed = cp.release_agent_dispatch_holds_batch(
        reversed(holds),
        epoch_id="epoch-idempotent-retry",
    )

    assert {agent.id for agent in initial} == {first.id, second.id}
    assert {agent.id for agent in replayed} == {first.id, second.id}
    assert all(agent.dispatch_hold is False for agent in replayed)
    assert (
        cp.store.query_one(
            "SELECT COUNT(*) AS count FROM agent_lifecycle_events "
            "WHERE event_type = 'agent.dispatch_hold_epoch_released'"
        )["count"]
        == receipt_count
    )


def test_dispatch_hold_epoch_retry_binds_readiness_expectations():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-epoch-expectations")
    cp.heartbeat_agent(
        agent.id,
        status="idle",
        health_status="healthy",
        resources={"deployment_generation": "generation-one"},
    )
    cp.set_agent_dispatch_hold(agent.id, "deployment-expectations")
    holds = ((agent.id, "deployment-expectations"),)
    expectations = {
        agent.id: {
            "generation": "generation-one",
            "baseline_seen": "2000-01-01T00:00:00+00:00",
            "principal_id": None,
            "require_authenticated": False,
            "require_report_executor": False,
        }
    }

    cp.release_agent_dispatch_holds_batch(
        holds,
        epoch_id="epoch-expectations",
        expectations=expectations,
    )
    replayed = cp.release_agent_dispatch_holds_batch(
        holds,
        epoch_id="epoch-expectations",
        expectations=expectations,
    )
    assert replayed[0].dispatch_hold is False

    stricter = {agent.id: {**expectations[agent.id], "require_authenticated": True}}
    with pytest.raises(ValidationError, match="successor outcome, or expectations"):
        cp.release_agent_dispatch_holds_batch(
            holds,
            epoch_id="epoch-expectations",
            expectations=stricter,
        )
    marker = cp.store.query_one(
        "SELECT detail FROM agent_lifecycle_events "
        "WHERE event_type = 'agent.dispatch_hold_epoch_committed'"
    )
    assert json.loads(marker["detail"])["expectations"] == [
        {"agent_id": agent.id, **expectations[agent.id]}
    ]


def test_dispatch_hold_batch_transition_is_atomic_and_idempotent():
    cp = _make_cp()
    first = _register_agent(cp, "agent-transition-first")
    second = _register_agent(cp, "agent-transition-second")
    cp.set_agent_dispatch_hold(first.id, "deployment-first")
    cp.set_agent_dispatch_hold(second.id, "deployment-second")
    holds = (
        (first.id, "deployment-first"),
        (second.id, "deployment-second"),
    )

    transitioned = cp.release_agent_dispatch_holds_batch(
        holds,
        epoch_id="epoch-successor-idempotent",
        successor_reason="synchronized successor hold",
    )

    assert {agent.id for agent in transitioned} == {first.id, second.id}
    assert all(agent.dispatch_hold is True for agent in transitioned)
    assert {
        agent.dispatch_hold_reason for agent in transitioned
    } == {"synchronized successor hold"}
    receipt_rows = cp.store.query_all(
        "SELECT detail FROM agent_lifecycle_events "
        "WHERE event_type = 'agent.dispatch_hold_epoch_transitioned'"
    )
    assert len(receipt_rows) == 2
    assert {
        json.loads(row["detail"])["successor_hold_reason"] for row in receipt_rows
    } == {"synchronized successor hold"}
    marker = cp.store.query_one(
        "SELECT detail FROM agent_lifecycle_events "
        "WHERE event_type = 'agent.dispatch_hold_epoch_committed'"
    )
    marker_detail = json.loads(marker["detail"])
    assert marker_detail["outcome"] == "successor_hold"
    assert marker_detail["successor_hold_reason"] == "synchronized successor hold"

    replayed = cp.release_agent_dispatch_holds_batch(
        reversed(holds),
        epoch_id="epoch-successor-idempotent",
        successor_reason="synchronized successor hold",
    )

    assert {agent.id for agent in replayed} == {first.id, second.id}
    assert all(agent.dispatch_hold is True for agent in replayed)
    assert all(
        agent.dispatch_hold_reason == "synchronized successor hold"
        for agent in replayed
    )
    assert (
        cp.store.query_one(
            "SELECT COUNT(*) AS count FROM agent_lifecycle_events "
            "WHERE event_type = 'agent.dispatch_hold_epoch_transitioned'"
        )["count"]
        == 2
    )


def test_dispatch_hold_batch_transition_rolls_back_when_any_reason_is_stale():
    cp = _make_cp()
    first = _register_agent(cp, "agent-transition-rollback-first")
    second = _register_agent(cp, "agent-transition-rollback-second")
    cp.set_agent_dispatch_hold(first.id, "deployment-first")
    cp.set_agent_dispatch_hold(second.id, "operator-replaced-second")

    with pytest.raises(
        ValidationError,
        match="lost dispatch-hold ownership for %s" % second.id,
    ):
        cp.release_agent_dispatch_holds_batch(
            (
                (first.id, "deployment-first"),
                (second.id, "stale-deployment-second"),
            ),
            epoch_id="epoch-successor-rollback",
            successor_reason="synchronized successor hold",
        )

    first_after = cp.get_agent(first.id)
    second_after = cp.get_agent(second.id)
    assert first_after.dispatch_hold is True
    assert first_after.dispatch_hold_reason == "deployment-first"
    assert second_after.dispatch_hold is True
    assert second_after.dispatch_hold_reason == "operator-replaced-second"
    assert (
        cp.store.query_one(
            "SELECT COUNT(*) AS count FROM agent_lifecycle_events "
            "WHERE event_type IN "
            "('agent.dispatch_hold_epoch_transitioned', "
            "'agent.dispatch_hold_epoch_committed')"
        )["count"]
        == 0
    )


def test_dispatch_hold_batch_epoch_reuse_separates_release_and_successor_outcomes():
    cp = _make_cp()
    transitioned_agent = _register_agent(cp, "agent-transition-epoch-mode")
    cp.set_agent_dispatch_hold(transitioned_agent.id, "deployment-transition")
    transitioned_holds = ((transitioned_agent.id, "deployment-transition"),)
    cp.release_agent_dispatch_holds_batch(
        transitioned_holds,
        epoch_id="epoch-transition-mode",
        successor_reason="successor-one",
    )

    with pytest.raises(ValidationError, match="successor outcome"):
        cp.release_agent_dispatch_holds_batch(
            transitioned_holds,
            epoch_id="epoch-transition-mode",
            successor_reason="successor-two",
        )
    with pytest.raises(ValidationError, match="successor outcome"):
        cp.release_agent_dispatch_holds_batch(
            transitioned_holds,
            epoch_id="epoch-transition-mode",
        )

    released_agent = _register_agent(cp, "agent-release-epoch-mode")
    cp.set_agent_dispatch_hold(released_agent.id, "deployment-release")
    released_holds = ((released_agent.id, "deployment-release"),)
    cp.release_agent_dispatch_holds_batch(
        released_holds,
        epoch_id="epoch-release-mode",
    )
    with pytest.raises(ValidationError, match="successor outcome"):
        cp.release_agent_dispatch_holds_batch(
            released_holds,
            epoch_id="epoch-release-mode",
            successor_reason="late-successor",
        )


@pytest.mark.parametrize("first_outcome", ["released", "successor_hold"])
def test_legacy_unmarked_epoch_reuse_rejects_opposite_outcome(first_outcome):
    cp = _make_cp()
    agent = _register_agent(cp, "agent-legacy-unmarked-epoch-" + first_outcome)
    original_reason = "deployment-legacy-unmarked"
    successor_reason = "successor-legacy-unmarked"
    epoch_id = "epoch-legacy-unmarked-" + first_outcome
    cp.set_agent_dispatch_hold(agent.id, original_reason)
    cp.release_agent_dispatch_holds_batch(
        ((agent.id, original_reason),),
        epoch_id=epoch_id,
        successor_reason=(
            successor_reason if first_outcome == "successor_hold" else None
        ),
    )
    cp.store.execute(
        "DELETE FROM agent_lifecycle_events "
        "WHERE event_type = 'agent.dispatch_hold_epoch_committed'"
    )
    cp.set_agent_dispatch_hold(agent.id, original_reason)

    with pytest.raises(ValidationError, match="successor outcome"):
        cp.release_agent_dispatch_holds_batch(
            ((agent.id, original_reason),),
            epoch_id=epoch_id,
            successor_reason=(
                None if first_outcome == "successor_hold" else successor_reason
            ),
        )

    held = cp.get_agent(agent.id)
    assert held.dispatch_hold is True
    assert held.dispatch_hold_reason == original_reason


def test_dispatch_hold_batch_transition_requires_distinct_nonblank_successor():
    cp = _make_cp()
    agent = _register_agent(cp, "agent-transition-reason-validation")
    cp.set_agent_dispatch_hold(agent.id, "deployment-hold")

    with pytest.raises(ValidationError, match="successor.*required"):
        cp.release_agent_dispatch_holds_batch(
            ((agent.id, "deployment-hold"),),
            epoch_id="epoch-blank-successor",
            successor_reason="  ",
        )
    with pytest.raises(ValidationError, match="must differ"):
        cp.release_agent_dispatch_holds_batch(
            ((agent.id, "deployment-hold"),),
            epoch_id="epoch-same-successor",
            successor_reason="deployment-hold",
        )
    for invalid_reason in (
        "successor\ncontrol",
        "successor\n",
        "\u0085successor",
        "successor\u0085control",
        "x" * 513,
    ):
        with pytest.raises(ValidationError, match="512 UTF-8 bytes"):
            cp.release_agent_dispatch_holds_batch(
                ((agent.id, "deployment-hold"),),
                epoch_id="epoch-invalid-successor",
                successor_reason=invalid_reason,
            )
    assert cp.get_agent(agent.id).dispatch_hold_reason == "deployment-hold"


def test_dispatch_hold_batch_release_rejects_partial_or_mismatched_epoch_reuse():
    cp = _make_cp()
    first = _register_agent(cp, "agent-epoch-reuse-first")
    second = _register_agent(cp, "agent-epoch-reuse-second")
    cp.set_agent_dispatch_hold(first.id, "epoch-reuse-first")
    cp.set_agent_dispatch_hold(second.id, "epoch-reuse-second")
    cp.release_agent_dispatch_holds_batch(
        (
            (first.id, "epoch-reuse-first"),
            (second.id, "epoch-reuse-second"),
        ),
        epoch_id="epoch-reuse-guard",
    )

    with pytest.raises(ValidationError, match="different or incomplete hold set"):
        cp.release_agent_dispatch_holds_batch(
            ((first.id, "epoch-reuse-first"),),
            epoch_id="epoch-reuse-guard",
        )
    with pytest.raises(ValidationError, match="different or incomplete hold set"):
        cp.release_agent_dispatch_holds_batch(
            (
                (first.id, "changed-reason"),
                (second.id, "epoch-reuse-second"),
            ),
            epoch_id="epoch-reuse-guard",
        )


def test_dispatch_hold_batch_release_http_route_is_admin_only():
    cp = _make_cp()
    worker = _register_agent(cp, "batch-release-worker-principal")
    first = _register_agent(cp, "batch-release-first")
    second = _register_agent(cp, "batch-release-second")
    cp.set_agent_dispatch_hold(first.id, "fleet-epoch-first")
    cp.set_agent_dispatch_hold(second.id, "fleet-epoch-second")
    cp.heartbeat_agent(
        first.id,
        status="idle",
        health_status="healthy",
        resources={"deployment_generation": "fleet-generation-first"},
    )
    cp.heartbeat_agent(
        second.id,
        status="idle",
        health_status="healthy",
        resources={"deployment_generation": "fleet-generation-second"},
    )
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "worker": {
                    "scopes": ["agent", "dispatch", "read", "write"],
                    "tenant_id": None,
                    "agent_id": worker.id,
                    "principal_kind": "worker",
                },
                "admin": ["admin"],
            },
        )
    )
    path = "/agents/dispatch-hold/release-batch"
    payload = {
        "epoch_id": "epoch-http-admin",
        "holds": [
            {
                "agent_id": first.id,
                "reason": "fleet-epoch-first",
                "generation": "fleet-generation-first",
                "baseline_seen": "2000-01-01T00:00:00+00:00",
                "principal_id": None,
                "require_authenticated": False,
            },
            {
                "agent_id": second.id,
                "reason": "fleet-epoch-second",
                "generation": "fleet-generation-second",
                "baseline_seen": "2000-01-01T00:00:00+00:00",
                "principal_id": None,
                "require_authenticated": False,
            },
        ],
    }

    rejected = client.post(
        path,
        headers={"Authorization": "Bearer worker"},
        json=payload,
    )
    assert rejected.status_code == 403
    assert cp.get_agent(first.id).dispatch_hold is True
    assert cp.get_agent(second.id).dispatch_hold is True

    released = client.post(
        path,
        headers={"Authorization": "Bearer admin"},
        json=payload,
    )
    assert released.status_code == 200
    assert released.json()["released"] is True
    assert released.json()["epoch_id"] == "epoch-http-admin"
    assert {agent["id"] for agent in released.json()["agents"]} == {
        first.id,
        second.id,
    }
    assert cp.get_agent(first.id).dispatch_hold is False
    assert cp.get_agent(second.id).dispatch_hold is False

    receipt_count = cp.store.query_one(
        "SELECT COUNT(*) AS count FROM agent_lifecycle_events "
        "WHERE event_type = 'agent.dispatch_hold_epoch_released'"
    )["count"]
    replayed = client.post(
        path,
        headers={"Authorization": "Bearer admin"},
        json=payload,
    )
    assert replayed.status_code == 200
    assert {agent["id"] for agent in replayed.json()["agents"]} == {
        first.id,
        second.id,
    }
    assert (
        cp.store.query_one(
            "SELECT COUNT(*) AS count FROM agent_lifecycle_events "
            "WHERE event_type = 'agent.dispatch_hold_epoch_released'"
        )["count"]
        == receipt_count
    )


def test_dispatch_hold_batch_transition_http_route_is_admin_only():
    cp = _make_cp()
    worker = _register_agent(cp, "batch-transition-worker-principal")
    first = _register_agent(cp, "batch-transition-first")
    second = _register_agent(cp, "batch-transition-second")
    cp.set_agent_dispatch_hold(first.id, "fleet-transition-first")
    cp.set_agent_dispatch_hold(second.id, "fleet-transition-second")
    cp.heartbeat_agent(
        first.id,
        status="idle",
        health_status="healthy",
        resources={"deployment_generation": "fleet-transition-generation-first"},
    )
    cp.heartbeat_agent(
        second.id,
        status="idle",
        health_status="healthy",
        resources={"deployment_generation": "fleet-transition-generation-second"},
    )
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "worker": {
                    "scopes": ["agent", "dispatch", "read", "write"],
                    "tenant_id": None,
                    "agent_id": worker.id,
                    "principal_kind": "worker",
                },
                "admin": ["admin"],
            },
        )
    )
    path = "/agents/dispatch-hold/transition-batch"
    payload = {
        "epoch_id": "epoch-http-transition-admin",
        "successor_reason": "synchronized successor hold",
        "holds": [
            {
                "agent_id": first.id,
                "reason": "fleet-transition-first",
                "generation": "fleet-transition-generation-first",
                "baseline_seen": "2000-01-01T00:00:00+00:00",
                "principal_id": None,
                "require_authenticated": False,
            },
            {
                "agent_id": second.id,
                "reason": "fleet-transition-second",
                "generation": "fleet-transition-generation-second",
                "baseline_seen": "2000-01-01T00:00:00+00:00",
                "principal_id": None,
                "require_authenticated": False,
            },
        ],
    }

    rejected = client.post(
        path,
        headers={"Authorization": "Bearer worker"},
        json=payload,
    )
    assert rejected.status_code == 403
    assert cp.get_agent(first.id).dispatch_hold_reason == "fleet-transition-first"
    assert cp.get_agent(second.id).dispatch_hold_reason == "fleet-transition-second"

    transitioned = client.post(
        path,
        headers={"Authorization": "Bearer admin"},
        json=payload,
    )
    assert transitioned.status_code == 200
    assert transitioned.json()["transitioned"] is True
    assert transitioned.json()["epoch_id"] == "epoch-http-transition-admin"
    assert transitioned.json()["successor_reason"] == "synchronized successor hold"
    assert {agent["id"] for agent in transitioned.json()["agents"]} == {
        first.id,
        second.id,
    }
    assert all(
        agent["dispatch_hold"] is True
        and agent["dispatch_hold_reason"] == "synchronized successor hold"
        for agent in transitioned.json()["agents"]
    )


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
    """A virtual, hub-driven agent (e.g. the hub_verify review verifier) has no
    worker process and by design emits no executor telemetry, so its expired
    review leases must NOT be counted as zombie signals or quarantine it."""
    monkeypatch.setenv("MAC_AGENT_QUARANTINE_THRESHOLD", "2")
    cp = _make_cp()
    machine = cp.register_machine("virtual-review-host", resources={"cpu": 1, "memory_gb": 1})
    agent = cp.register_agent(
        machine.id,
        "hub-reviewer",
        capabilities=["review"],
        resources={"virtual": True, "review": {"mode": "hub_verify"}},
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
