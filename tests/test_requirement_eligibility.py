"""A task's stated requirements decide which agents are eligible for it.

The allocator already refused agents that could not meet a task's
requirements. What nothing could answer was the question one level up: *can
anything in this fleet ever run this, or is it only busy right now?* Both
collapsed into ``no_eligible_agent``, so a permanently undispatchable task was
indistinguishable from a saturated fleet -- and the difference is the whole
operator decision. One resolves itself; the other never does.

``classify_requirement_eligibility`` answers it, and is built on the same
``evaluate_pair`` the dispatcher uses. That is deliberate: the previous
unsatisfiable-work check compared ``required_capabilities`` against the fleet
by hand, which covered one of the five requirement dimensions and could
disagree with the real matching rules. A verdict derived from the dispatcher's
own predicate can only ever report what dispatch would actually decide.
"""

from __future__ import annotations

from mac.allocator import (
    NO_AGENTS,
    SATISFIABLE,
    UNSATISFIABLE,
    AllocationAgent,
    AllocationTask,
    classify_requirement_eligibility,
    evaluate_pair,
    rejection_kind,
)

CREATED = "2026-01-01T00:00:00+00:00"


def _task(**kwargs) -> AllocationTask:
    return AllocationTask(id="task_1", priority=1, created_at=CREATED, **kwargs)


def _agent(agent_id: str = "agent_1", **kwargs) -> AllocationAgent:
    kwargs.setdefault("capabilities", frozenset({"python"}))
    return AllocationAgent(id=agent_id, **kwargs)


# --- busy is not the same as incapable -----------------------------------


def test_a_busy_agent_that_meets_the_requirements_is_still_capable():
    """A saturated fleet must never be reported as an incapable one.

    This is the distinction the verdict exists to draw: the task will run as
    soon as a slot frees, and telling an operator "no agent can satisfy this"
    would send them to reconfigure something that is already correct.
    """
    task = _task(required_capabilities=frozenset({"python"}))
    busy = _agent(capacity=1, active_leases=1)

    verdict = classify_requirement_eligibility(task, [busy])

    assert verdict.verdict == SATISFIABLE
    assert verdict.capable_agent_ids == ("agent_1",)
    assert verdict.unmet_requirements == {}


def test_offline_held_and_unhealthy_agents_are_still_capable():
    """Every transient state, for the same reason."""
    task = _task(required_capabilities=frozenset({"python"}))
    for label, agent in (
        ("offline", _agent(online=False)),
        ("held", _agent(dispatch_held=True)),
        ("unhealthy", _agent(healthy=False)),
    ):
        verdict = classify_requirement_eligibility(task, [agent])
        assert verdict.verdict == SATISFIABLE, label


# --- a real requirement gap is structural --------------------------------


def test_a_capability_no_agent_has_is_unsatisfiable():
    task = _task(required_capabilities=frozenset({"cuda"}))

    verdict = classify_requirement_eligibility(task, [_agent()])

    assert verdict.verdict == UNSATISFIABLE
    assert verdict.capable_agent_ids == ()
    assert verdict.unmet_requirements == {"agent_capabilities_missing": 1}


def test_an_agent_that_cannot_execute_anything_is_not_capable():
    """The dimension the old capability-only check was blind to.

    jordanh-worker6 and worker7 advertised their capabilities honestly and
    were matched for every task, while the executor refused to launch on both.
    A check that only compared capability strings reported the fleet healthy
    throughout.
    """
    task = _task(required_capabilities=frozenset({"python"}))
    no_boundary = _agent(execution_boundary_verified=False)

    verdict = classify_requirement_eligibility(task, [no_boundary])

    assert verdict.verdict == UNSATISFIABLE
    assert verdict.unmet_requirements == {"agent_no_execution_boundary": 1}


def test_resource_and_hardware_gaps_are_reported_with_the_offending_key():
    """Two more dimensions the previous check never looked at."""
    agent = _agent(hardware={"os": "linux"}, resources={"vram_mb": 8000})

    resources = classify_requirement_eligibility(
        _task(required_resources={"vram_mb": 48000}), [agent]
    )
    assert resources.verdict == UNSATISFIABLE
    # The resource is named, so the report says which one is short.
    assert resources.unmet_requirements == {"agent_resources_insufficient:vram_mb": 1}

    hardware = classify_requirement_eligibility(
        _task(required_hardware={"os": ["darwin"]}), [agent]
    )
    assert hardware.verdict == UNSATISFIABLE
    assert hardware.unmet_requirements == {"agent_hardware_insufficient": 1}


def test_one_capable_agent_makes_the_task_satisfiable():
    """The verdict is about the fleet, not about every agent in it."""
    task = _task(required_capabilities=frozenset({"cuda"}))
    verdict = classify_requirement_eligibility(
        task,
        [
            _agent("cpu_only"),
            _agent("gpu_busy", capabilities=frozenset({"cuda"}), capacity=1, active_leases=1),
        ],
    )

    assert verdict.verdict == SATISFIABLE
    assert verdict.capable_agent_ids == ("gpu_busy",)
    # The incapable agent is still counted, so the report shows the shortfall.
    assert verdict.unmet_requirements == {"agent_capabilities_missing": 1}


def test_an_empty_fleet_is_reported_separately_from_an_incapable_one():
    """No agents is an operator state, not a broken task."""
    verdict = classify_requirement_eligibility(_task(), [])

    assert verdict.verdict == NO_AGENTS
    assert not verdict.satisfiable


def test_task_level_gates_do_not_mask_the_requirement_verdict():
    """Whether the fleet CAN run it is separate from whether it is ready to.

    evaluate_pair runs the task gates first and returns early when one fires,
    leaving agent_rejections empty. Reusing it naively meant a task that was
    held, waiting on a dependency, or out of attempts reported every agent as
    capable -- announcing that a fleet which cannot do the work can.

    Readiness is reported separately; this verdict must answer only the
    requirement question.
    """
    cpu_only = [_agent(capabilities=frozenset({"python"}))]
    for label, gate in (
        ("held", {"released": False}),
        ("waiting on a dependency", {"dependencies_satisfied": False}),
        ("attempts exhausted", {"attempt_count": 3, "max_attempts": 3}),
        ("project inactive", {"project_active": False}),
        ("already leased", {"lease_id": "lease_1"}),
        ("package not ready", {"package_ready": False}),
        ("ready", {}),
    ):
        verdict = classify_requirement_eligibility(
            _task(required_capabilities=frozenset({"cuda"}), **gate), cpu_only
        )
        assert verdict.verdict == UNSATISFIABLE, label
        assert verdict.unmet_requirements == {"agent_capabilities_missing": 1}, label


def test_a_task_naming_an_unknown_role_can_never_run():
    """A requirement that names something that does not exist is unsatisfiable.

    This is the task's own statement being wrong rather than the fleet being
    short of something, but the operator consequence is identical: it will
    never dispatch until someone changes it.
    """
    verdict = classify_requirement_eligibility(
        _task(required_role="media:nonexistent", required_role_known=False), [_agent()]
    )

    assert verdict.verdict == UNSATISFIABLE
    assert verdict.unmet_requirements == {"task_required_role_unknown": 1}


# --- targeting is the task's own routing, not fleet evidence -------------


def test_a_targeted_task_is_judged_only_against_its_target():
    """Pinning a task makes every other agent mismatch by construction.

    Counting those as "cannot meet the requirements" would report a healthy
    fleet as incapable every time an operator targeted a single worker.
    """
    task = _task(target_agent_id="chosen", required_capabilities=frozenset({"python"}))
    verdict = classify_requirement_eligibility(
        task, [_agent("chosen", capacity=1, active_leases=1), _agent("other")]
    )

    assert verdict.considered_agent_ids == ("chosen",)
    assert verdict.verdict == SATISFIABLE
    assert verdict.unmet_requirements == {}


def test_a_targeted_task_whose_target_cannot_run_it_is_unsatisfiable():
    task = _task(target_agent_id="chosen", required_capabilities=frozenset({"cuda"}))
    verdict = classify_requirement_eligibility(
        task, [_agent("chosen"), _agent("other", capabilities=frozenset({"cuda"}))]
    )

    assert verdict.considered_agent_ids == ("chosen",)
    assert verdict.verdict == UNSATISFIABLE


# --- the verdict cannot drift from the dispatcher ------------------------


def test_a_capable_agent_is_exactly_one_the_dispatcher_would_accept_when_free():
    """The property that makes this trustworthy.

    A hand-rolled copy of the matching rules can disagree with the real ones,
    which is how the old check could call a task dispatchable that never
    dispatched. Capability here must mean precisely "evaluate_pair allows this
    pair once the transient blockers clear".
    """
    import dataclasses

    task = _task(required_capabilities=frozenset({"python"}))
    agents = [
        _agent("meets_it"),
        _agent("busy", capacity=1, active_leases=1),
        _agent("offline", online=False),
        _agent("wrong_caps", capabilities=frozenset({"rust"})),
        _agent("no_boundary", execution_boundary_verified=False),
        _agent("untrusted", machine_trusted=False),
    ]

    verdict = classify_requirement_eligibility(task, agents)
    capable = set(verdict.capable_agent_ids)

    for agent in agents:
        # Clear every transient blocker and re-ask the dispatcher directly.
        freed = dataclasses.replace(
            agent, online=True, healthy=True, dispatch_held=False, active_leases=0, capacity=1
        )
        assert evaluate_pair(task, freed).allowed is (agent.id in capable), agent.id


def test_every_agent_rejection_code_is_classified():
    """An unclassified code would silently count as a requirement failure.

    Falling back to "requirement" is the safe direction -- it over-reports
    rather than hiding a stall -- but a new code should be a deliberate
    decision, so this fails loudly when one is added without one.
    """
    from mac import allocator

    codes = {
        value
        for name, value in vars(allocator).items()
        if name.startswith("AGENT_") and isinstance(value, str)
    }
    # agent_target_mismatch is handled by exclusion rather than classification.
    codes.discard(allocator.AGENT_TARGET_MISMATCH)

    unclassified = sorted(code for code in codes if rejection_kind(code) == "other")

    assert unclassified == [], (
        "unclassified rejection codes %s -- decide whether each is a "
        "requirement, an authorization, or transient" % unclassified
    )


# --- what an operator is actually told -----------------------------------


def test_explain_distinguishes_an_incapable_fleet_from_a_busy_one():
    """The operator-facing half of the same distinction.

    Both cases used to report ``no_eligible_agent``, which is true and
    useless: it does not say whether to wait or to go change something.
    """
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host", resources={"cpu": 4, "memory_gb": 8})
    cp.register_agent(machine.id, "worker", capabilities=["python"])

    stuck = cp.create_task("needs a gpu", required_capabilities=["cuda"])
    explained = cp.explain_task_dispatch(stuck.id)

    assert explained["dispatchable"] is False
    assert [item["code"] for item in explained["unclaimed_reasons"]] == ["no_capable_agent"]
    assert explained["requirement_eligibility"]["verdict"] == UNSATISFIABLE
    assert explained["requirement_eligibility"]["unmet_requirements"] == {
        "agent_capabilities_missing": 1
    }
    # The message has to say what to do about it, not restate the verdict.
    message = explained["unclaimed_reasons"][0]["message"]
    assert "waiting will not change that" in message


def test_explain_still_says_busy_when_the_fleet_is_merely_busy():
    """The control: a capable-but-saturated fleet keeps the old verdict."""
    from mac.models import AgentStatus
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host", resources={"cpu": 4, "memory_gb": 8})
    agent = cp.register_agent(machine.id, "worker", capabilities=["python"])
    cp.update_agent(agent.id, status=AgentStatus.OFFLINE.value, actor="ops")

    task = cp.create_task("ordinary work", required_capabilities=["python"])
    explained = cp.explain_task_dispatch(task.id)

    assert explained["dispatchable"] is False
    assert [item["code"] for item in explained["unclaimed_reasons"]] == ["no_eligible_agent"]
    assert explained["requirement_eligibility"]["verdict"] == SATISFIABLE
