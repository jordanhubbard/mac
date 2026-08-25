"""The two gates that failed silently on 2026-08-19/20.

Neither bug was a wrong decision. Both were correct decisions that said
nothing, which is worse: a fleet with 84 open tasks and idle capable workers
looked, from every command an operator would run, like a fleet with nothing
wrong.
"""

from __future__ import annotations

import io
import json
import sys

from mac.cli import _render_why_unclaimed
from mac.services import startup_self_test_clears_dispatch

SCHEMA = "mac.agent_startup_self_test.v1"


def _self_test(status, *, agent_id="agent_natasha", blocking=None):
    return {
        "schema": SCHEMA,
        "agent_id": agent_id,
        "status": status,
        "blocking_problems": [] if blocking is None else blocking,
    }


# --- the self-test gate -----------------------------------------------------


def test_a_passed_self_test_clears_dispatch():
    """The regression that benched natasha.

    Its self-test reported "passed" with no blocking problems -- the best
    possible result -- and the gate written to RELEASE degraded agents
    required the status to be literally "degraded", so the healthiest outcome
    was the one it rejected.
    """
    assert startup_self_test_clears_dispatch(_self_test("passed"), agent_id="agent_natasha")


def test_a_degraded_but_unblocked_self_test_still_clears_dispatch():
    """The case the gate was originally written for must keep working."""
    assert startup_self_test_clears_dispatch(_self_test("degraded"), agent_id="agent_natasha")


def test_blocking_problems_are_still_disqualifying():
    for status in ("passed", "degraded"):
        assert not startup_self_test_clears_dispatch(
            _self_test(status, blocking=["openshell missing"]),
            agent_id="agent_natasha",
        )


def test_a_self_test_for_a_different_agent_does_not_clear_this_one():
    assert not startup_self_test_clears_dispatch(
        _self_test("passed", agent_id="agent_someone_else"),
        agent_id="agent_natasha",
    )


def test_an_unrecognised_schema_does_not_clear_dispatch():
    stale = _self_test("passed")
    stale["schema"] = "mac.agent_startup_self_test.v0"
    assert not startup_self_test_clears_dispatch(stale, agent_id="agent_natasha")


def test_failed_is_not_a_clearing_status():
    assert not startup_self_test_clears_dispatch(_self_test("failed"), agent_id="agent_natasha")


# --- why-unclaimed ----------------------------------------------------------

PAYLOAD = {
    "task": {"id": "task_x", "state": "open", "priority": 100, "title": "a task"},
    "task_reasons": [],
    "candidate_count": 3,
    "candidates": [
        {"agent_name": "natasha", "eligible": False, "reasons": [{"code": "agent_unhealthy"}]},
        {"agent_name": "rocky", "eligible": False, "reasons": [{"code": "agent_capacity_full"}]},
        {
            "agent_name": "bullwinkle",
            "eligible": False,
            "reasons": [{"code": "agent_capacity_full"}],
        },
    ],
}


def test_why_unclaimed_names_the_gate_that_benched_each_agent():
    """The whole point: the payload always held this, the renderer dropped it.

    `mac task why-unclaimed` printed a title and two attempt counters, which
    reads as "nothing is wrong" -- while 15 top-priority tasks sat unclaimed.
    """
    out = _render_why_unclaimed(PAYLOAD)

    assert "agent_unhealthy" in out
    assert "natasha" in out
    assert "NO agent can take this task" in out


def test_why_unclaimed_groups_agents_sharing_a_reason():
    """Ten agents rejected for one cause is one fact, not ten lines."""
    out = _render_why_unclaimed(PAYLOAD)

    assert out.count("agent_capacity_full") == 1
    line = next(l for l in out.splitlines() if "agent_capacity_full" in l)
    assert "bullwinkle" in line and "rocky" in line


def test_why_unclaimed_says_so_when_no_gate_is_closed():
    """An eligible agent and an unclaimed task means DISPATCH is at fault.

    That conclusion has to be stated. Printing nothing is what let this go
    unnoticed the first time.
    """
    payload = dict(PAYLOAD)
    payload["candidates"] = [{"agent_name": "natasha", "eligible": True, "reasons": []}]
    payload["task_reasons"] = []

    out = _render_why_unclaimed(payload)

    assert "ELIGIBLE" in out
    assert "dispatch itself" in out


def test_why_unclaimed_reports_task_level_gates_separately():
    payload = dict(PAYLOAD)
    payload["task_reasons"] = ["task_dispatch_held"]

    out = _render_why_unclaimed(payload)

    assert "TASK-LEVEL GATES" in out
    assert "no_dispatch" in out  # the hint names the fix


def test_why_unclaimed_states_truncation_rather_than_implying_completeness():
    payload = dict(PAYLOAD)
    payload["candidate_truncated"] = True
    payload["candidate_limit"] = 60

    out = _render_why_unclaimed(payload)

    assert "truncated" in out


def test_reasons_render_as_codes_whether_they_are_strings_or_objects():
    """Both shapes occur. A raw dict repr in the output is not greppable."""
    payload = dict(PAYLOAD)
    payload["task_reasons"] = [{"code": "no_eligible_agent", "message": "no eligible agent"}]

    out = _render_why_unclaimed(payload)

    assert "no_eligible_agent" in out
    assert "{'code'" not in out
