"""There is ONE definition of agent dispatch-readiness.

Three implementations drifted. Two were fixed to accept a `passed` self-test
and the third was not, so a worker whose self-test passed with no blocking
problems stayed benched beside 80 open tasks — and fixing two of three changed
nothing observable, which is the worst outcome available, because the work
looked done.
"""

from __future__ import annotations

import inspect
from pathlib import Path

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


@pytest.mark.parametrize("health", ["failed", "unknown", "", None, "DEGRADED_LOOKING_BUT_NOT"])
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
    assert not startup_self_test_clears_dispatch(_self_test("failed"), agent_id="agent_x")


# --- there is only one of it -------------------------------------------------


def test_neither_module_reimplements_the_rule():
    """The drift guard.

    Each copy was a correct-looking inline expression over the same fields.
    Asserting on the SHAPE of the rule rather than on behaviour is what catches
    a fourth copy being added, because a fourth copy passes every behavioural
    test above on the day it is written -- and then rots.
    """
    # Every module under src/mac, not a hand-listed pair. The pair was how a
    # copy in fleet_release_epoch_service.py -- carrying the same defect --
    # stayed invisible while two modules were "consolidated".
    root = Path(__file__).resolve().parents[1] / "src" / "mac"
    for path in sorted(root.glob("*.py")):
        if path.name in {"agent_health.py"}:
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        module = type("M", (), {"__name__": path.name})
        assert 'startup.get("status") ==' not in src, (
            "%s compares a self-test status inline; call "
            "mac.agent_health.advisory_health_dispatch_ready instead" % module.__name__
        )
        assert '"mac.agent_startup_self_test.v1"' not in src, (
            "%s hardcodes the self-test schema; import SELF_TEST_SCHEMA" % module.__name__
        )


def test_no_shell_or_deploy_script_reimplements_the_rule():
    """The copy the module-based guard could not see.

    deploy/deploy-mac-fleet.sh embedded a fourth implementation, in Python
    inside a heredoc. `inspect.getsource` reaches importable modules, so a copy
    living in a shell script is invisible to the guard above -- and that copy
    had the identical `status == "degraded"` defect, meaning the deploy path
    would keep refusing a worker whose self-test PASSED even after both Python
    copies were fixed.

    It never needed to be a copy: the block runs under
    $HOME/.mac/venv/bin/python and the helper beside it already imports from
    mac.models.
    """
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in list(root.glob("deploy/**/*.sh")) + list(root.glob("scripts/**/*.sh")):
        text = path.read_text(encoding="utf-8", errors="replace")
        # READERS only. deploy/fleet-node-install.sh WRITES the report and
        # legitimately names the schema as a dict key -- flagging the producer
        # would make this guard unpassable and teach people to weaken it.
        if 'startup.get("status") ==' in text or 'startup.get("status") in' in text:
            offenders.append("%s compares a self-test status inline" % path.name)
        if '"schema") == "mac.agent_startup_self_test.v1"' in text:
            offenders.append("%s compares the self-test schema inline" % path.name)
    assert not offenders, (
        "call mac.agent_health.advisory_health_dispatch_ready instead: %s" % "; ".join(offenders)
    )


def test_the_deploy_script_delegates_rather_than_reimplementing():
    deploy = Path(__file__).resolve().parents[1] / "deploy" / "deploy-mac-fleet.sh"
    text = deploy.read_text(encoding="utf-8", errors="replace")
    assert "advisory_health_dispatch_ready" in text, (
        "release_health_ready must delegate to the shared predicate"
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
    assert advisory_health_dispatch_ready("degraded", resources, agent_id="agent_n") is True
