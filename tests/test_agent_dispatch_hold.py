"""Unit + integration tests for agent dispatch-hold enforcement.

Covers:
- services.set_agent_dispatch_hold persists all three hold fields
- services.clear_agent_dispatch_hold resets all three hold fields
- _agent_availability_for_task returns (False, "agent_dispatch_held") for held agents
- A non-held agent is still dispatched normally
- Hold survives a round-trip through the DB layer
"""

from __future__ import annotations

import pytest

from mac.models import NotFoundError
from mac.services import ControlPlane


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_cp() -> ControlPlane:
    return ControlPlane.in_memory()


def _register_agent(cp: ControlPlane, name: str = "worker-1"):
    machine = cp.register_machine(f"{name}-host", resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, name)


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


# ---------------------------------------------------------------------------
# _agent_availability_for_task — dispatch hold guard
# ---------------------------------------------------------------------------


def test_held_agent_skipped_with_agent_dispatch_held_reason():
    cp = _make_cp()
    from mac.models import AgentStatus, HealthStatus

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
