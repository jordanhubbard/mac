"""Dispatch gates that stop work being matched to an agent that cannot run it."""

from __future__ import annotations

from mac.allocator import (
    AGENT_NO_EXECUTION_BOUNDARY,
    AllocationAgent,
    AllocationTask,
    evaluate_pair,
)


def test_agent_without_a_verified_execution_boundary_is_not_offered_work():
    """Capability matching never asked whether the agent can execute at all.

    Observed live 2026-08-02: five container-provisioned workers advertised
    python/testing honestly, were matched, claimed tasks, and then the executor
    refused to launch because no OpenShell sandbox existed. Every one of those
    tasks died at attempt 1 of 3 on a worker that was never able to run it.
    """
    task = AllocationTask(
        id="task_exec",
        priority=5,
        created_at="2026-08-02T00:00:00+00:00",
        required_capabilities=frozenset({"python"}),
    )
    capable = AllocationAgent(
        id="rocky", capabilities=frozenset({"python"}), execution_boundary_verified=True
    )
    sandboxless = AllocationAgent(
        id="pod", capabilities=frozenset({"python"}), execution_boundary_verified=False
    )

    assert evaluate_pair(task, capable).allowed is True
    rejected = evaluate_pair(task, sandboxless)
    assert rejected.allowed is False
    assert AGENT_NO_EXECUTION_BOUNDARY in rejected.agent_rejections


def test_a_task_that_never_invokes_an_executor_still_matches_a_sandboxless_agent():
    """The gate is about launching a coding agent, not about all work."""
    bookkeeping = AllocationTask(
        id="task_book",
        priority=5,
        created_at="2026-08-02T00:00:00+00:00",
        required_capabilities=frozenset({"python"}),
        requires_execution=False,
    )
    sandboxless = AllocationAgent(
        id="pod", capabilities=frozenset({"python"}), execution_boundary_verified=False
    )
    assert evaluate_pair(bookkeeping, sandboxless).allowed is True


def test_execution_boundary_reads_three_states_not_two():
    """proven allows, contradicted blocks, silence allows.

    The middle state is the whole point. worker6/7 reported
    ``openshell_required: false`` while the executor still refused to run
    unsandboxed -- configured to skip the sandbox AND forbidden to go without
    one. Every task routed to them died.

    Silence stays permissive deliberately: this gate is a claim about agents
    that told us something, not a new registration requirement. Making silence
    disqualifying would strand every worker mid-upgrade, and it failed 113
    existing tests when tried.
    """

    def agent(resources):
        return AllocationAgent.from_hub_record(
            {
                "id": "a",
                "capabilities": ["python"],
                "health_status": "healthy",
                "resources": resources,
            },
            online=True,
            capacity=1,
            active_leases=0,
            machine_trusted=True,
        )

    proven = {
        "openclaw_runtime": {"confinement": {"provider": "openshell"}, "verified": True}
    }
    contradicted = {"openshell_required": False}
    # Proof outranks the contradiction: a worker that verified a sandbox is
    # usable whatever a stale requirement flag says.
    proven_but_flagged = dict(proven, openshell_required=False)

    assert agent(proven).execution_boundary_verified is True
    assert agent(contradicted).execution_boundary_verified is False
    assert agent(proven_but_flagged).execution_boundary_verified is True
    assert agent({}).execution_boundary_verified is True
    assert agent({"openshell_required": True}).execution_boundary_verified is True
