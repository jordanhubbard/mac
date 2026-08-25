from mac.allocator import (
    AGENT_CAPABILITIES_MISSING,
    AGENT_CAPACITY_FULL,
    TASK_DEPENDENCIES_INCOMPLETE,
    TASK_HELD,
    AllocationAgent,
    AllocationTask,
    AuthoritativeAllocator,
    ClaimCommit,
    adapt_v2_claim_primitive,
    evaluate_pair,
)


def task(task_id, *, priority=0, created_at="2026-01-01T00:00:00+00:00", **kwargs):
    return AllocationTask(
        id=task_id,
        priority=priority,
        created_at=created_at,
        **kwargs,
    )


def agent(agent_id, **kwargs):
    return AllocationAgent(id=agent_id, **kwargs)


def test_task_gate_and_agent_constraints_share_one_evaluation_shape():
    blocked = task(
        "blocked",
        released=False,
        dependencies_satisfied=False,
        required_capabilities=frozenset({"python"}),
    )
    worker = agent("worker", capabilities=frozenset())

    evaluation = evaluate_pair(blocked, worker)

    assert evaluation.allowed is False
    assert evaluation.task_rejections == (
        TASK_HELD,
        TASK_DEPENDENCIES_INCOMPLETE,
    )
    # Agent constraints are deliberately not scanned when the task is blocked.
    assert evaluation.agent_rejections == ()
    assert evaluation.to_dict()["allowed"] is False


def test_batch_matcher_never_scans_or_claims_dependency_blocked_tasks():
    claims = []

    def claim(proposal):
        claims.append(proposal)
        return ClaimCommit.success({"lease_id": "lease-" + proposal.task_id})

    result = AuthoritativeAllocator().allocate_round(
        [
            task("blocked", priority=100, dependencies_satisfied=False),
            task("runnable", priority=1),
        ],
        [agent("worker")],
        claim,
        round_id="round",
    )

    assert [proposal.task_id for proposal in claims] == ["runnable"]
    blocked_decision = next(
        decision for decision in result.decisions if decision.task_id == "blocked"
    )
    assert blocked_decision.status == "not_runnable"
    assert blocked_decision.pair_evaluations == ()
    assert blocked_decision.task_evaluation.task_rejections == (TASK_DEPENDENCIES_INCOMPLETE,)


def test_deterministic_batch_matching_is_priority_then_age_and_least_loaded():
    seen = []

    def claim(proposal):
        seen.append((proposal.task_id, proposal.agent_id))
        return ClaimCommit.success({"task_id": proposal.task_id, "agent_id": proposal.agent_id})

    allocator = AuthoritativeAllocator()
    result = allocator.allocate_round(
        [
            task("low", priority=1),
            task("new-high", priority=10, created_at="2026-01-02T00:00:00+00:00"),
            task("old-high", priority=10, created_at="2026-01-01T00:00:00+00:00"),
        ],
        [
            agent("busy", capacity=2, active_leases=1),
            agent("empty", capacity=2),
        ],
        claim,
        round_id="round",
    )

    assert seen == [
        ("old-high", "busy"),
        ("new-high", "empty"),
        ("low", "empty"),
    ]
    assert result.assigned_count == 3
    assert result.stranded_task_ids == ()


def test_critical_path_signal_orders_only_within_priority_lane():
    seen = []
    result = AuthoritativeAllocator().allocate_round(
        [
            task(
                "older",
                priority=10,
                created_at="2026-01-01T00:00:00+00:00",
            ),
            task(
                "critical",
                priority=10,
                order_signal=0.9,
                created_at="2026-01-02T00:00:00+00:00",
            ),
            task(
                "higher-priority",
                priority=11,
                created_at="2026-01-03T00:00:00+00:00",
            ),
        ],
        [agent("a"), agent("b"), agent("c")],
        lambda proposal: (
            seen.append(proposal.task_id)
            or ClaimCommit.success({"lease_id": "lease-" + proposal.task_id})
        ),
        round_id="round",
    )

    assert result.assigned_count == 3
    assert seen == ["higher-priority", "critical", "older"]


def test_project_affinity_is_soft():
    constrained = task(
        "task",
        project="mac",
        required_capabilities=frozenset({"python"}),
    )
    worker = AllocationAgent.from_hub_record(
        {
            "id": "worker",
            "health_status": "healthy",
            "capabilities": ["python"],
            "resources": {
                "dispatch_policy": {
                    "schema": "mac.dispatch_policy.v2",
                    "preferred_projects": ["other"],
                }
            },
        },
        online=True,
        capacity=1,
        active_leases=0,
        machine_trusted=True,
    )

    result = AuthoritativeAllocator().allocate_round(
        [constrained],
        [worker],
        lambda proposal: ClaimCommit.success({}),
        round_id="round",
    )

    assert worker.preferred_projects == frozenset({"other"})
    assert result.assignments[0].proposal.agent_id == "worker"
    assert result.decisions[0].pair_evaluations[0].agent_rejections == ()


def test_prior_participation_is_a_soft_preference_not_a_blocker():
    constrained = task(
        "task",
        avoid_agent_ids=frozenset({"prior"}),
    )
    prior = agent("prior")
    fresh = agent("fresh")

    result = AuthoritativeAllocator().allocate_round(
        [constrained],
        [prior, fresh],
        lambda proposal: ClaimCommit.success({}),
        round_id="round",
    )
    assert result.assignments[0].proposal.agent_id == "fresh"

    fallback = AuthoritativeAllocator().allocate_round(
        [constrained],
        [prior],
        lambda proposal: ClaimCommit.success({}),
        round_id="fallback",
    )
    assert fallback.assignments[0].proposal.agent_id == "prior"


def test_generic_task_preserves_specialist_for_specialized_work():
    seen = []
    result = AuthoritativeAllocator().allocate_round(
        [
            task("generic", priority=100, required_capabilities=frozenset({"python"})),
            task(
                "gpu",
                priority=1,
                required_capabilities=frozenset({"python", "gpu"}),
            ),
        ],
        [
            agent("specialist", capabilities=frozenset({"python", "gpu"})),
            agent("generalist", capabilities=frozenset({"python"})),
        ],
        lambda proposal: (
            seen.append((proposal.task_id, proposal.agent_id))
            or ClaimCommit.success({"lease_id": "lease-" + proposal.task_id})
        ),
        round_id="round",
    )

    assert seen == [("generic", "generalist"), ("gpu", "specialist")]
    assert result.assigned_count == 2


def test_hard_constraints_are_structured_and_unmatched_task_is_stranded():
    constrained = task(
        "task",
        project="mac",
        required_capabilities=frozenset({"python"}),
    )
    wrong_target = agent(
        "a",
        capabilities=frozenset({"python"}),
    )
    full_and_missing_capability = agent("b", capacity=1, active_leases=1)
    constrained = AllocationTask(**{**constrained.__dict__, "target_agent_id": "somebody-else"})

    result = AuthoritativeAllocator().allocate_round(
        [constrained],
        [wrong_target, full_and_missing_capability],
        lambda proposal: ClaimCommit.success({}),
        round_id="round",
    )

    decision = result.decisions[0]
    assert decision.status == "unmatched"
    assert result.stranded_task_ids == ()
    assert result.unmatched_task_ids == ("task",)
    rejections = {
        evaluation.agent_id: evaluation.agent_rejections for evaluation in decision.pair_evaluations
    }
    # `:pinned` rather than the bare code: a task PINNED to one agent and an
    # agent EXCLUDED from a task both used to emit `agent_target_mismatch`, and
    # they mean opposite things. Everything that classifies codes matches on the
    # stem, so the suffix is additive -- but it is what lets a reader (and
    # classify_requirement_eligibility) tell "the task routes elsewhere" from
    # "this agent is barred".
    assert "agent_target_mismatch:pinned" in rejections["a"]
    assert rejections["b"] == (
        AGENT_CAPACITY_FULL,
        "agent_target_mismatch:pinned",
        AGENT_CAPABILITIES_MISSING,
    )


def test_break_glass_bypasses_placement_constraints_for_exact_agent_only():
    constrained = task(
        "task",
        target_agent_id="different-agent",
        break_glass_agent_id="recovery",
        excluded_agent_ids=frozenset({"recovery"}),
        required_capabilities=frozenset({"host-runtime-repair"}),
        required_resources={"os": "linux"},
    )
    recovery = agent(
        "recovery",
        dispatch_held=True,
        capabilities=frozenset(),
        resources={"os": "darwin"},
    )
    peer = agent(
        "peer",
        capabilities=frozenset({"host-runtime-repair"}),
        resources={"os": "linux"},
    )

    assert evaluate_pair(constrained, recovery).allowed is True
    assert evaluate_pair(constrained, peer).agent_rejections == ("agent_target_mismatch:pinned",)


def test_break_glass_retains_host_safety_constraints():
    constrained = task(
        "task",
        break_glass_agent_id="recovery",
        required_capabilities=frozenset({"host-runtime-repair"}),
    )
    unsafe = agent(
        "recovery",
        online=False,
        healthy=False,
        capacity=1,
        active_leases=1,
        machine_trusted=False,
        authorized_tenants=frozenset({"other"}),
    )

    assert evaluate_pair(constrained, unsafe).agent_rejections == (
        "agent_offline",
        "agent_unhealthy",
        AGENT_CAPACITY_FULL,
        "agent_machine_untrusted",
        "agent_tenant_unauthorized",
    )


def test_claim_rejection_does_not_block_later_tasks():
    def claim(proposal):
        if proposal.task_id == "stale":
            return ClaimCommit.rejected("task_changed")
        return ClaimCommit.success({"lease_id": "lease-good"})

    result = AuthoritativeAllocator().allocate_round(
        [task("stale", priority=10), task("good", priority=1)],
        [agent("worker")],
        claim,
        round_id="round",
    )

    assert [(item.proposal.task_id, item.proposal.agent_id) for item in result.assignments] == [
        ("good", "worker")
    ]
    assert [decision.status for decision in result.decisions] == [
        "claim_rejected",
        "assigned",
    ]


def test_retryable_agent_claim_failure_tries_next_compatible_agent():
    seen = []

    def claim(proposal):
        seen.append(proposal.agent_id)
        if proposal.agent_id == "a":
            return ClaimCommit.rejected("agent_capacity_race", retry_with_other_agent=True)
        return ClaimCommit.success({"lease_id": "lease"})

    result = AuthoritativeAllocator().allocate_round(
        [task("task")],
        [agent("a"), agent("b")],
        claim,
        round_id="round",
    )

    assert seen == ["a", "b"]
    assert result.assignments[0].proposal.agent_id == "b"
    assert tuple(failure.reason for failure in result.decisions[0].claim_failures) == (
        "agent_capacity_race",
    )


def test_existing_claim_primitive_adapter_preserves_atomic_assignment_payload():
    calls = []

    def existing(task_id, agent_id):
        calls.append((task_id, agent_id))
        return {"task": {"id": task_id}, "lease": {"agent_id": agent_id}}

    result = AuthoritativeAllocator().allocate_round(
        [task("task")],
        [agent("worker")],
        adapt_v2_claim_primitive(existing),
        round_id="round",
    )

    assert calls == [("task", "worker")]
    assert result.assignments[0].assignment["lease"]["agent_id"] == "worker"


def test_round_completion_hook_runs_once_and_cannot_invalidate_leases():
    observed = []

    def hook(result):
        observed.append(result.round_id)
        raise RuntimeError("telemetry unavailable")

    result = AuthoritativeAllocator(on_round_complete=hook).allocate_round(
        [task("task")],
        [agent("worker")],
        lambda proposal: ClaimCommit.success({"lease_id": "lease"}),
        round_id="round",
    )

    assert observed == ["round"]
    assert result.assigned_count == 1
    assert result.completion_hook_error == "RuntimeError:telemetry unavailable"


def _agent_with_startup_self_test(startup, *, health_status="degraded", agent_id="worker"):
    return AllocationAgent.from_hub_record(
        {
            "id": agent_id,
            "health_status": health_status,
            "capabilities": ["python"],
            "resources": {"startup_self_test": startup},
        },
        online=True,
        capacity=1,
        active_leases=0,
        machine_trusted=True,
    )


def _advisory_startup(agent_id="worker", blocking=()):
    return {
        "schema": "mac.agent_startup_self_test.v1",
        "agent_id": agent_id,
        "status": "degraded",
        "blocking_problems": list(blocking),
        "non_blocking_problems": ["OpenClaw agent self-test exited 1"],
    }


def test_advisory_startup_degradation_is_dispatch_ready():
    """A degraded self-test with no blocking problems must still dispatch.

    deploy-mac-fleet.sh already releases this agent (release_health_ready);
    when the allocator disagreed, one failed OpenClaw probe benched the whole
    fleet while the ledger still reported free capacity.
    """

    worker = _agent_with_startup_self_test(_advisory_startup())
    assert worker.healthy is True

    result = AuthoritativeAllocator().allocate_round(
        [task("task", required_capabilities=frozenset({"python"}))],
        [worker],
        lambda proposal: ClaimCommit.success({"lease_id": "lease-1"}),
        round_id="round",
    )
    assert result.assigned_count == 1


def test_blocking_startup_problems_are_not_dispatch_ready():
    worker = _agent_with_startup_self_test(
        _advisory_startup(blocking=["qdrant_shared_memory unreachable"])
    )
    assert worker.healthy is False


def test_advisory_startup_verdict_for_another_agent_is_ignored():
    worker = _agent_with_startup_self_test(_advisory_startup(agent_id="someone-else"))
    assert worker.healthy is False


def test_unhealthy_agent_stays_ineligible():
    worker = _agent_with_startup_self_test(_advisory_startup(), health_status="unhealthy")
    assert worker.healthy is False
