"""Decomposition is the submitter's declaration, not the framework's default.

The executor prompt used to carry five numbered steps explaining how to fan a
task out -- on EVERY task -- followed by one hedged sentence permitting the
agent not to. The sizing heuristic that could have counterweighted it only
spoke when it DETECTED a plan; when it decided a task was atomic it said
nothing, so the agent read a fan-out recipe with no opposing evidence.

Measured: a task whose description was "run `command -v` on these sixteen
commands and report what you find", which the detector scored as NOT a plan
with zero signals, was split into five children. Machine-originated work in
this ledger completes at 0-9.6% against 20% for human-filed, and
over-decomposition is one of the things manufacturing it.
"""

from __future__ import annotations

import pytest

from mac.executor_hub_io import _plan_detection_section, detect_plan_signals
from mac.models import ValidationError
from mac.services import ControlPlane
from mac.task_decomposition import (
    MAX_AUTHORISED_CHILDREN,
    check_children_allowed,
    decomposition_budget,
)


def _task(**metadata):
    return {
        "id": "task_1",
        "project": "mac",
        "title": "Report the sandbox toolchain",
        "description": "run command -v for each and report",
        "metadata": metadata,
    }


# --------------------------------------------------------------------------
# The default inverts
# --------------------------------------------------------------------------


def test_a_task_is_atomic_unless_the_submitter_says_otherwise():
    assert not decomposition_budget(_task()).authorised


def test_the_prompt_tells_an_unauthorised_task_not_to_split():
    section = _plan_detection_section(_task())

    assert "ATOMIC" in section
    assert "Do NOT create child tasks" in section


def test_the_five_step_recipe_is_absent_when_unauthorised():
    """The actual defect: the recipe printed on every task, always."""
    section = _plan_detection_section(_task())

    assert "Break the work into" not in section
    assert "children endpoint" not in section or "do NOT post" in section


def test_an_authorised_task_gets_the_recipe_with_its_budget():
    section = _plan_detection_section(
        _task(decomposition={"max_children": 3, "kind": "one per subsystem"})
    )

    assert "at most 3 child task(s)" in section
    assert "one per subsystem" in section


def test_the_budget_is_a_ceiling_not_a_quota():
    """Authorising 5 children must not read as 'produce 5 children'."""
    section = _plan_detection_section(_task(decomposition={"max_children": 5}))

    assert "ceiling, not a quota" in section


# --------------------------------------------------------------------------
# The heuristic becomes an observation
# --------------------------------------------------------------------------


def test_a_plan_verdict_without_authorisation_asks_rather_than_splits():
    """The detector disagreeing is a question for the submitter, not a licence."""
    section = _plan_detection_section(_task(), )
    # Force the plan branch directly, since the fixture text is deliberately atomic.
    from mac.task_decomposition import prompt_section

    noted = prompt_section(_task(), is_plan=True, signals=["numbered_steps:4"])

    assert "Do not act on that by splitting it" in noted
    assert "submitter can decide" in noted
    assert "ATOMIC" in section


def test_the_canary_that_caused_this_is_scored_atomic():
    """Regression on the real case: the detector was right, and was ignored."""
    is_plan, signals = detect_plan_signals(
        "Canary: verify the sandbox toolchain matches the derived BOM",
        "Report which of the contract-derived toolchain commands are actually "
        "present inside your OpenShell sandbox. Run command -v for each.",
    )

    assert is_plan is False and signals == []


# --------------------------------------------------------------------------
# Enforcement, not just advice
# --------------------------------------------------------------------------


def test_unauthorised_children_are_refused():
    allowed, why = check_children_allowed(_task(), 3)

    assert not allowed
    assert "did not authorise decomposition" in why


def test_the_refusal_says_how_to_authorise_it():
    """A refusal that does not say the remedy just gets worked around."""
    _allowed, why = check_children_allowed(_task(), 3)

    assert "max_children" in why


def test_a_budget_is_a_hard_ceiling():
    allowed, why = check_children_allowed(_task(decomposition={"max_children": 2}), 5)

    assert not allowed and "at most 2" in why


def test_within_budget_is_allowed():
    allowed, _ = check_children_allowed(_task(decomposition={"max_children": 5}), 5)

    assert allowed


def test_no_decompose_still_wins_over_a_budget():
    """A task carrying both is contradictory; refusing is the safe reading."""
    allowed, _ = check_children_allowed(
        _task(no_decompose=True, decomposition={"max_children": 5}), 1
    )

    assert not allowed


def test_a_bare_true_is_refused_rather_than_given_an_invented_budget():
    """Inventing a number is how the framework got here in the first place."""
    budget = decomposition_budget(_task(decomposition=True))

    assert not budget.authorised
    assert "max_children" in budget.reason


def test_an_absurd_budget_is_capped():
    """A typo must not enqueue a thousand tasks."""
    budget = decomposition_budget(_task(decomposition={"max_children": 10_000}))

    assert budget.max_children == MAX_AUTHORISED_CHILDREN


# --------------------------------------------------------------------------
# The control plane holds the line
# --------------------------------------------------------------------------


@pytest.fixture()
def cp():
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


def _agent_adds_children(cp, parent, children):
    """What an executing agent does: untrusted, under its own lease.

    The guard is scoped to this path deliberately. An operator adding children
    IS the submitter declaring, and work-package assembly builds its own graph;
    the defect was an AGENT splitting a task nobody asked to split.
    """
    machine = cp.register_machine("decomp-host")
    agent = cp.register_agent(machine.id, "decomp-worker")
    _task, lease = cp.claim_task(parent.id, agent.id)   # returns (Task, Lease)
    lease_id = lease.id
    return cp.add_child_tasks(
        parent.id,
        children,
        actor=agent.id,
        lease_id=lease_id,
        trusted_internal=False,
    )


def test_an_agent_cannot_split_an_unauthorised_task(cp):
    parent = cp.create_task("atomic work", project="mac")

    with pytest.raises(ValidationError) as excinfo:
        _agent_adds_children(cp, parent, [{"title": "child", "description": "x"}])

    assert "did not authorise decomposition" in str(excinfo.value)


def test_an_agent_may_split_an_authorised_task(cp):
    parent = cp.create_task(
        "real plan", project="mac", metadata={"decomposition": {"max_children": 2}}
    )

    assert _agent_adds_children(
        cp, parent, [{"title": "a", "description": "x"}, {"title": "b", "description": "y"}]
    )


def test_an_agent_cannot_exceed_the_ceiling(cp):
    parent = cp.create_task(
        "real plan", project="mac", metadata={"decomposition": {"max_children": 1}}
    )

    with pytest.raises(ValidationError) as excinfo:
        _agent_adds_children(
            cp, parent,
            [{"title": "a", "description": "x"}, {"title": "b", "description": "y"}],
        )

    assert "at most 1" in str(excinfo.value)


def test_an_operator_adding_children_is_not_blocked(cp):
    """The operator IS the submitter. Blocking them would break work-package
    assembly and every deliberate decomposition."""
    parent = cp.create_task("operator plan", project="mac")

    assert cp.add_child_tasks(parent.id, [{"title": "a", "description": "x"}])
