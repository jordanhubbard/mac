"""A persona must not claim work it cannot do.

`operator` is a persona standing in for a human: `resources` carries
`operator_persona: true` and `virtual: true`, and it advertises no
capabilities at all. It is not a worker and nothing executes on its behalf.

It claimed a code task anyway, twice, and held it for six hours without
producing a single history entry -- while the same task, on its first
attempt, had been executed properly by a real worker. The fleet looked busy
and the task was simply frozen. It had been holding generated repair tasks
the same way.

The execution-boundary gate should have caught this and could not. It reads

    execution_boundary_verified = proven or not contradicted

where `proven` needs a verified confinement provider and `contradicted` means
the agent explicitly declared `openshell_required: false`. A persona
advertises no runtime whatsoever, so nothing is proven, nothing is
contradicted, and absence of evidence passes as permission.
"""

from __future__ import annotations

from dataclasses import replace

from mac.allocator import (
    AGENT_OPERATOR_PERSONA,
    REQUIREMENT_REJECTIONS,
    AllocationAgent,
    AllocationTask,
    evaluate_pair,
)


def _task() -> AllocationTask:
    return AllocationTask(id="task_probe", priority=50, created_at="2026-08-13T00:00:00+00:00")


def _agent(**resources) -> AllocationAgent:
    return AllocationAgent.from_hub_record(
        {
            "id": "agent_probe",
            "status": "idle",
            "health_status": "healthy",
            "capabilities": [],
            "resources": resources,
        },
        online=True,
        capacity=1,
        active_leases=0,
        machine_trusted=True,
    )


def test_a_persona_is_rejected_for_work_that_must_execute():
    """The live failure: `operator` claimed a code task and froze it."""
    agent = _agent(operator_persona=True, virtual=True)

    evaluation = evaluate_pair(_task(), agent)

    assert AGENT_OPERATOR_PERSONA in evaluation.rejections


def test_the_rejection_is_structural_rather_than_something_to_wait_out():
    """Classification decides what an operator is told. A persona will never
    grow a worker, so reporting this as transient capacity would send someone
    to wait for a dispatch that cannot come."""
    assert AGENT_OPERATOR_PERSONA in REQUIREMENT_REJECTIONS


def test_a_worker_that_advertises_nothing_unusual_is_untouched():
    """The gate keys on the persona flag, not on silence. Workers mid-upgrade
    advertise no confinement either, and disqualifying them would strand the
    fleet -- which is why the boundary rule is permissive in the first place."""
    agent = _agent()

    evaluation = evaluate_pair(_task(), agent)

    assert AGENT_OPERATOR_PERSONA not in evaluation.rejections


def test_the_review_verifier_is_rejected_for_undeclared_work_too(tmp_path=None):
    """The second half of the same failure. Holding `operator` did not fix it:
    `hub-reviewer` -- virtual, capabilities ['review'] -- claimed the
    replacement task within a minute and froze it the same way. A gate keyed on
    the operator persona alone would have moved the stall, not removed it."""
    agent = _agent(virtual=True, hub_review_verifier={"enabled": True})

    evaluation = evaluate_pair(_task(), agent)

    assert AGENT_OPERATOR_PERSONA in evaluation.rejections


def test_a_stand_in_may_still_take_work_that_asks_for_what_it_is():
    """A task declaring `review` is precisely what the review verifier exists
    for. Rejecting that would trade a frozen implementation task for a review
    stage that never runs."""
    agent = _agent(virtual=True, hub_review_verifier={"enabled": True})
    agent = replace(agent, capabilities=frozenset({"review"}))
    task = replace(_task(), required_capabilities=frozenset({"review"}))

    evaluation = evaluate_pair(task, agent)

    assert AGENT_OPERATOR_PERSONA not in evaluation.rejections
