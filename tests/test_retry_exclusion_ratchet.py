"""A retry exclusion must never be the reason work can never run again.

Measured on the live fleet 2026-08-08, after two weeks in which no agent
executed anything while eight sat idle:

    task_b23269b4 required ['c', 'testing']
    exactly ONE agent advertised 'c'          (jordanh-worker5)
    that agent failed once, transiently
    metadata.retry_excluded_agent_ids = ['agent_jordanh-worker5']
    -> zero eligible agents, permanently

The exclusion is written by the "one bounded cross-worker retry after transient
failure" path, and the idea is right: retrying the same transient failure on the
same worker usually reproduces it. What was wrong is that the bar is HARD and
never expires, so in a finite pool it ratchets -- which is the failure mode the
codebase already names in ``_coordination_excluded_agent_ids`` ("accumulated
exclusions ratchet a task family into a permanent no-eligible-agent deadlock")
and already avoids for ``avoid_agent_ids``, which is soft for this reason.

So the bar is honoured whenever ANY agent can take the task, and dropped only
when the alternative is never running it at all. A retry on the same worker is
worse than a different worker and far better than a task that can never run.

Two rejection codes also stopped sharing one spelling. ``agent_target_mismatch``
meant both "only another agent may run this" (a pin, the task's own routing) and
"this agent is barred from it" (an exclusion, an accumulated bar). Everything
classifies on the code stem, so ``:pinned`` / ``:excluded`` is additive -- but it
is what lets the requirement diagnostic stop reporting an exclusion as an unmet
requirement, which sent the operator to add capabilities that were already there.
"""

from __future__ import annotations

import pytest

from mac.allocator import (
    AllocationAgent,
    AllocationTask,
    AuthoritativeAllocator,
    ClaimCommit,
    classify_requirement_eligibility,
    evaluate_pair,
)

CAPABLE = frozenset({"c", "testing"})


def _task(excluded=(), target=None, capabilities=CAPABLE):
    return AllocationTask(
        id="task_b23269b4",
        priority=1,
        created_at="2026-08-02T03:00:25Z",
        required_capabilities=frozenset(capabilities),
        excluded_agent_ids=frozenset(excluded),
        target_agent_id=target,
    )


def _fleet(*, extra_capable=False, capable_count=1):
    agents = [AllocationAgent(id="agent_worker5", capabilities=CAPABLE, capacity=4)]
    if extra_capable:
        agents.append(AllocationAgent(id="agent_worker9", capabilities=CAPABLE, capacity=4))
    # The rest of the fleet: identically configured, python/testing only.
    agents += [
        AllocationAgent(id="agent_%d" % i, capabilities=frozenset({"testing"}), capacity=4)
        for i in range(8)
    ]
    return agents


def _dispatch(task, agents):
    placed = []

    def claim_pair(proposal):
        placed.append(proposal.agent_id)
        return ClaimCommit.success(object())

    AuthoritativeAllocator().allocate_round([task], agents, claim_pair)
    return placed


# --------------------------------------------------------------------------
# The ratchet
# --------------------------------------------------------------------------


def test_the_only_capable_agent_is_used_even_when_excluded():
    """The live deadlock. Nothing else in the fleet can run this task."""
    placed = _dispatch(_task(excluded={"agent_worker5"}), _fleet())

    assert placed == ["agent_worker5"], (
        "the only agent able to run this task stayed excluded, so the task is "
        "undispatchable for ever"
    )


def test_the_exclusion_is_honoured_when_an_alternative_exists():
    """The half that must NOT change.

    The exclusion exists so a bounded cross-worker retry lands somewhere else.
    Relaxing it whenever it is inconvenient would discard that entirely.
    """
    placed = _dispatch(_task(excluded={"agent_worker5"}), _fleet(extra_capable=True))

    assert placed == ["agent_worker9"]


def test_an_unexcluded_task_is_unaffected():
    assert _dispatch(_task(), _fleet()) == ["agent_worker5"]


def test_a_pin_is_never_relaxed():
    """A pin is the task's own routing, not an accumulated bar.

    Relaxing it would send work to an agent the operator deliberately excluded
    -- break-glass and targeted repair depend on that holding.
    """
    placed = _dispatch(_task(target="agent_does_not_exist"), _fleet())

    assert placed == []


def test_relaxing_does_not_bypass_capabilities():
    """Only the exclusion is dropped; every other requirement still applies."""
    task = _task(excluded={"agent_worker5"}, capabilities={"c", "testing", "fortran"})

    assert _dispatch(task, _fleet()) == []


# --------------------------------------------------------------------------
# The two meanings that shared one code
# --------------------------------------------------------------------------


def test_an_exclusion_and_a_pin_are_distinguishable():
    agent = AllocationAgent(id="agent_worker5", capabilities=CAPABLE, capacity=4)

    excluded = evaluate_pair(_task(excluded={"agent_worker5"}), agent)
    pinned = evaluate_pair(_task(target="agent_other"), agent)

    assert excluded.agent_rejections == ("agent_target_mismatch:excluded",)
    assert pinned.agent_rejections == ("agent_target_mismatch:pinned",)


def test_both_codes_still_classify_on_their_stem():
    """Suffixing must be additive: everything downstream matches the stem."""
    from mac.allocator import rejection_kind

    assert rejection_kind("agent_target_mismatch:excluded") == rejection_kind(
        "agent_target_mismatch"
    )
    assert rejection_kind("agent_target_mismatch:pinned") == rejection_kind("agent_target_mismatch")


# --------------------------------------------------------------------------
# The diagnostic that reported the wrong cause
# --------------------------------------------------------------------------


def test_an_excluded_agent_is_counted_not_skipped():
    """It used to be dropped as "the task's own routing".

    That is true of a pin and false of an exclusion, and the consequence was a
    verdict of "no agent can meet the requirements" for a task whose one
    capable agent met them perfectly and was barred -- pointing the operator at
    agent capabilities instead of at the bar.
    """
    verdict = classify_requirement_eligibility(_task(excluded={"agent_worker5"}), _fleet())

    assert "agent_target_mismatch:excluded" in verdict.unmet_requirements
    assert "agent_worker5" in verdict.considered_agent_ids


def test_a_pinned_task_still_reports_only_the_pinned_agent():
    """A pin genuinely is routing, and must not be reported as a fleet defect."""
    verdict = classify_requirement_eligibility(_task(target="agent_worker5"), _fleet())

    assert verdict.considered_agent_ids == ("agent_worker5",)
    assert not any(code.startswith("agent_target_mismatch") for code in verdict.unmet_requirements)


def test_a_pin_survives_even_when_the_task_also_has_an_exclusion():
    """The case that actually exercises the relaxation against a pin.

    test_a_pin_is_never_relaxed above does NOT: with no exclusion the task
    never enters the relaxation path at all, so it passes against a build that
    happily clears the pin. A task carrying both is what proves only the
    exclusion is dropped -- and it is a real shape, since a pinned repair task
    can also have excluded a worker that failed it.
    """
    task = _task(excluded={"agent_worker9"}, target="agent_absent")

    placed = _dispatch(task, _fleet(extra_capable=True))

    assert placed == [], (
        "the relaxation cleared the pin as well as the exclusion; a pin is the "
        "task's own routing and break-glass depends on it holding"
    )
