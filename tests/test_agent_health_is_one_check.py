"""There is ONE definition of agent dispatch-readiness.

Three implementations drifted. Two were fixed to accept a `passed` self-test
and the third was not, so a worker whose self-test passed with no blocking
problems stayed benched beside 80 open tasks — and fixing two of three changed
nothing observable, which is the worst outcome available, because the work
looked done.
"""
from __future__ import annotations

import inspect

import pytest

from mac import allocator as allocator_mod
from mac import services as services_mod
from mac.agent_health import (
    SELF_TEST_SCHEMA,
    advisory_health_dispatch_ready,
    startup_self_test_clears_dispatch,
)


def _self_test(status, agent_id="agent_x", blocking=None):
    return {
        "schema": SELF_TEST_SCHEMA,
        "agent_id": agent_id,
        "status": status,
        "blocking_problems": [] if blocking is None else blocking,
    }


# --- the rule ---------------------------------------------------------------

@pytest.mark.parametrize("status", ["passed", "degraded"])
def test_a_clearing_self_test_releases_a_degraded_agent(status):
    """`passed` is the case that was rejected. It is strictly safer than
    `degraded`, so the gate written to RELEASE degraded agents was refusing
    the healthiest result it could receive."""
    assert advisory_health_dispatch_ready(
        "degraded", {"startup_self_test": _self_test(status)}, agent_id="agent_x"
    )


def test_healthy_needs_no_self_test():
    assert advisory_health_dispatch_ready("healthy", {}, agent_id="agent_x")


@pytest.mark.parametrize(
    "health", ["failed", "unknown", "", None, "DEGRADED_LOOKING_BUT_NOT"]
)
def test_any_other_health_value_is_refused(health):
    assert not advisory_health_dispatch_ready(
        health, {"startup_self_test": _self_test("passed")}, agent_id="agent_x"
    )


def test_degraded_without_a_self_test_stays_benched():
    assert not advisory_health_dispatch_ready("degraded", {}, agent_id="agent_x")


def test_blocking_problems_disqualify():
    assert not startup_self_test_clears_dispatch(
        _self_test("passed", blocking=["openshell missing"]), agent_id="agent_x"
    )


def test_another_agents_self_test_does_not_clear_this_one():
    assert not startup_self_test_clears_dispatch(
        _self_test("passed", agent_id="agent_other"), agent_id="agent_x"
    )


def test_a_stale_schema_does_not_clear():
    stale = _self_test("passed")
    stale["schema"] = "mac.agent_startup_self_test.v0"
    assert not startup_self_test_clears_dispatch(stale, agent_id="agent_x")


def test_a_failed_self_test_does_not_clear():
    assert not startup_self_test_clears_dispatch(
        _self_test("failed"), agent_id="agent_x"
    )


# --- there is only one of it -------------------------------------------------

def test_neither_module_reimplements_the_rule():
    """The drift guard.

    Each copy was a correct-looking inline expression over the same fields.
    Asserting on the SHAPE of the rule rather than on behaviour is what catches
    a fourth copy being added, because a fourth copy passes every behavioural
    test above on the day it is written -- and then rots.
    """
    for module in (allocator_mod, services_mod):
        src = inspect.getsource(module)
        assert 'startup.get("status") ==' not in src, (
            "%s compares a self-test status inline; call "
            "mac.agent_health.advisory_health_dispatch_ready instead" % module.__name__
        )
        assert '"mac.agent_startup_self_test.v1"' not in src, (
            "%s hardcodes the self-test schema; import SELF_TEST_SCHEMA"
            % module.__name__
        )


def test_both_call_sites_use_the_shared_predicate():
    assert "advisory_health_dispatch_ready" in inspect.getsource(allocator_mod)
    assert "advisory_health_dispatch_ready" in inspect.getsource(
        services_mod.ControlPlane._advisory_health_dispatch_ready
    )


def test_the_allocator_and_the_control_plane_agree_on_the_same_agent():
    """The failure mode the allocator's own comment predicted: the hub offers
    an agent the policy layer then refuses, once per round, forever."""
    resources = {"startup_self_test": _self_test("passed", agent_id="agent_n")}
    record = {
        "id": "agent_n",
        "health_status": "degraded",
        "resources": resources,
        "status": "idle",
        "capabilities": [],
    }
    snapshot = allocator_mod.AllocationAgent.from_hub_record(
        record, online=True, capacity=1, active_leases=0, machine_trusted=True
    )

    assert snapshot.healthy is True
    assert advisory_health_dispatch_ready(
        "degraded", resources, agent_id="agent_n"
    ) is True
