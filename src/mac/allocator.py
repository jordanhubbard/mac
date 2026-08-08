"""Authoritative hub-owned task allocation.

This module deliberately contains no task-ledger queries.  The hub supplies a
snapshot of tasks and agents, and one callback that performs the authoritative
transactional claim.  Keeping the policy pure makes the same evaluation useful
for allocation, ``task ready``, and ``why-unclaimed`` without rebuilding three
different definitions of eligibility.

The important boundary is ``claim_pair``: it must create the task lease and
transition the task in one database transaction.  ``ControlPlane.claim_task``
already provides that primitive and can be adapted with
``adapt_claim_primitive`` during the allocator cutover.  A PostgreSQL-native
implementation can later replace the callback with an allocator transaction
using ``FOR UPDATE SKIP LOCKED`` without changing the matching contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, Mapping, Optional, Tuple
from uuid import uuid4

from mac.roles_service import machine_hardware_satisfies


JsonDict = Dict[str, Any]


TASK_NOT_OPEN = "task_not_open"
TASK_HELD = "task_held"
TASK_ALREADY_LEASED = "task_already_leased"
TASK_DEPENDENCIES_INCOMPLETE = "task_dependencies_incomplete"
TASK_PROJECT_UNREGISTERED = "task_project_unregistered"
TASK_PROJECT_INACTIVE = "task_project_inactive"
TASK_ATTEMPTS_EXHAUSTED = "task_attempts_exhausted"
TASK_PACKAGE_NOT_READY = "task_package_not_ready"
TASK_REQUIRED_ROLE_UNKNOWN = "task_required_role_unknown"

AGENT_OFFLINE = "agent_offline"
AGENT_UNHEALTHY = "agent_unhealthy"
AGENT_HELD = "agent_held"
AGENT_CAPACITY_FULL = "agent_capacity_full"
AGENT_MACHINE_UNTRUSTED = "agent_machine_untrusted"
AGENT_TENANT_UNAUTHORIZED = "agent_tenant_unauthorized"
AGENT_TARGET_MISMATCH = "agent_target_mismatch"
AGENT_CAPABILITIES_MISSING = "agent_capabilities_missing"
AGENT_RESOURCES_INSUFFICIENT = "agent_resources_insufficient"
AGENT_HARDWARE_INSUFFICIENT = "agent_hardware_insufficient"
AGENT_ROLE_INELIGIBLE = "agent_role_ineligible"
AGENT_ROLE_MISMATCH = "agent_role_mismatch"
AGENT_NO_EXECUTION_BOUNDARY = "agent_no_execution_boundary"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class AllocationTask:
    """The complete dispatch authority needed for one work item.

    ``dependencies_satisfied`` is expected to come from normalized dependency
    edges in the same database snapshot as the task.  The allocator checks it
    once before considering any task/agent pairs, so a dependency-blocked task
    cannot consume a worker-sized scan.
    """

    id: str
    priority: int
    created_at: str
    state: str = "open"
    released: bool = True
    lease_id: Optional[str] = None
    dependencies_satisfied: bool = True
    project: Optional[str] = None
    project_registered: bool = True
    project_active: bool = True
    attempt_count: int = 0
    max_attempts: int = 3
    # Advisory-only critical-path ordering.  Priority remains authoritative;
    # this bounded signal only orders peers within the same priority lane.
    order_signal: float = 0.0
    tenant_id: Optional[str] = None
    target_agent_id: Optional[str] = None
    # A live, control-plane-owned break-glass authorization binds the task to
    # exactly one agent.  The authorized pair bypasses normal placement
    # constraints, while the allocator still enforces host safety below.
    break_glass_agent_id: Optional[str] = None
    avoid_agent_ids: FrozenSet[str] = field(default_factory=frozenset)
    # Whether running this task means launching a coding agent inside a
    # sandbox, and so whether the agent must have a verified execution
    # boundary.
    #
    # Every task currently asserts it: the snapshot builder does not set this,
    # so it always takes the default. That is the fail-closed direction -- a
    # task that does not need an executor is merely held to a stricter bar
    # than necessary, whereas the reverse would route real work to a worker
    # that cannot run it. Deriving it per task kind would loosen the gate, so
    # it wants evidence about which kinds are genuinely executor-free rather
    # than a guess.
    requires_execution: bool = True
    excluded_agent_ids: FrozenSet[str] = field(default_factory=frozenset)
    required_capabilities: FrozenSet[str] = field(default_factory=frozenset)
    required_resources: Mapping[str, Any] = field(default_factory=dict)
    required_hardware: Mapping[str, Any] = field(default_factory=dict)
    required_role: Optional[str] = None
    required_role_known: bool = True
    required_role_capabilities: FrozenSet[str] = field(default_factory=frozenset)
    package_ready: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AllocationAgent:
    """Hub-owned capacity and hard authorization for one agent."""

    id: str
    online: bool = True
    healthy: bool = True
    dispatch_held: bool = False
    machine_trusted: bool = True
    capacity: int = 1
    active_leases: int = 0
    capabilities: FrozenSet[str] = field(default_factory=frozenset)
    resources: Mapping[str, float] = field(default_factory=dict)
    authorized_tenants: Optional[FrozenSet[str]] = None
    denied_tenants: FrozenSet[str] = field(default_factory=frozenset)
    # Project lists advertised by workers are placement affinities, not an
    # authorization boundary.  Any fleet agent may execute any registered
    # project if the hard tenant/capability/trust gates pass.
    preferred_projects: FrozenSet[str] = field(default_factory=frozenset)
    dispatch_policy: Mapping[str, Any] = field(default_factory=dict)
    hardware: Mapping[str, Any] = field(default_factory=dict)
    bound_role_slug: Optional[str] = None
    bound_role_eligible: bool = True
    bound_role_required_capabilities: FrozenSet[str] = field(default_factory=frozenset)
    # Whether this agent has a VERIFIED sandbox to execute inside. Capability
    # matching answers "can this agent do Python?"; it never asked "can this
    # agent execute anything at all?". A worker provisioned without OpenShell
    # advertises its capabilities honestly, is matched, claims the task, and
    # then the executor refuses to launch -- so the work dies on a worker that
    # was never able to run it. Provisioning cannot be trusted to guarantee
    # this: the SSH installer installs OpenShell and the container image does
    # not, and a future AWS/Azure worker brings its own runtime or none.
    execution_boundary_verified: bool = True

    @property
    def free_slots(self) -> int:
        return max(0, int(self.capacity) - int(self.active_leases))

    @classmethod
    def from_hub_record(
        cls,
        record: Any,
        *,
        online: bool,
        capacity: int,
        active_leases: int,
        machine_trusted: bool,
        authorized_tenants: Optional[Iterable[str]] = None,
        denied_tenants: Optional[Iterable[str]] = None,
    ) -> "AllocationAgent":
        """Build hub allocation capacity from an Agent/model/API record.

        ``resources.dispatch_policy`` is the only worker-advertised policy
        surface consumed by allocator v2.  Its ``preferred_projects`` value is
        a soft affinity; worker-local metadata and canary filters do not exist
        in the v2 contract.
        """

        def value(name: str, default: Any = None) -> Any:
            if isinstance(record, Mapping):
                return record.get(name, default)
            return getattr(record, name, default)

        raw_resources = value("resources", {})
        resources = dict(raw_resources) if isinstance(raw_resources, Mapping) else {}
        raw_policy = resources.get("dispatch_policy")
        policy = dict(raw_policy) if isinstance(raw_policy, Mapping) else {}
        allowed = policy.get("preferred_projects")
        preferred_projects = frozenset(
            str(project)
            for project in (allowed if isinstance(allowed, (list, tuple, set)) else ())
            if str(project)
        )
        health = str(value("health_status", "") or "").lower()
        # An advisory startup self-test can report ``degraded`` while proving it
        # has no blocking problems. deploy-mac-fleet.sh (release_health_ready)
        # and ControlPlane._advisory_health_dispatch_ready both treat that as
        # dispatch-ready; disagreeing here would let the hub offer an agent that
        # the policy layer then refuses, once per round, forever.
        startup = resources.get("startup_self_test")
        advisory_ready = bool(
            health == "degraded"
            and isinstance(startup, Mapping)
            and startup.get("schema") == "mac.agent_startup_self_test.v1"
            and startup.get("agent_id") == str(value("id"))
            and startup.get("status") == "degraded"
            and startup.get("blocking_problems") == []
        )
        # Can this agent execute anything at all? Capability matching never
        # asked. A worker provisioned without a sandbox advertises python and
        # testing honestly, is matched, claims the task, and only then does the
        # executor refuse to launch -- so the work dies on a worker that was
        # never able to run it.
        #
        # Three states, and the middle one is the whole point:
        #   proven      - a confinement provider the worker verified  -> allow
        #   contradicted- openshell_required is false, yet the executor still
        #                 refuses to run unsandboxed                  -> block
        #   unknown     - reports neither                             -> allow
        #
        # "Unknown" stays permissive deliberately. This gate is a claim about
        # agents that have told us something, not a new registration
        # requirement; making silence disqualifying would strand every worker
        # mid-upgrade. The unsatisfiable-requirements diagnostic reports the
        # silent ones separately.
        runtime = resources.get("openclaw_runtime")
        confinement = (
            runtime.get("confinement") if isinstance(runtime, Mapping) else None
        )
        proven = bool(
            isinstance(confinement, Mapping)
            and str(confinement.get("provider") or "").strip()
            and isinstance(runtime, Mapping)
            and runtime.get("verified") is True
        )
        contradicted = resources.get("openshell_required") is False
        execution_boundary_verified = proven or not contradicted
        return cls(
            id=str(value("id")),
            online=bool(online),
            execution_boundary_verified=execution_boundary_verified,
            healthy=health == "healthy" or advisory_ready,
            dispatch_held=bool(value("dispatch_hold", False)),
            machine_trusted=bool(machine_trusted),
            capacity=int(capacity),
            active_leases=int(active_leases),
            capabilities=frozenset(str(item) for item in (value("capabilities", []) or [])),
            resources=resources,
            authorized_tenants=(
                None
                if authorized_tenants is None
                else frozenset(str(item) for item in authorized_tenants)
            ),
            denied_tenants=frozenset(
                str(item) for item in (denied_tenants or ())
            ),
            preferred_projects=preferred_projects,
            dispatch_policy=policy,
        )


@dataclass(frozen=True)
class PairEvaluation:
    """One reusable evaluation for both task gates and agent hard constraints."""

    task_id: str
    agent_id: Optional[str]
    task_rejections: Tuple[str, ...] = ()
    agent_rejections: Tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return not self.task_rejections and not self.agent_rejections

    @property
    def rejections(self) -> Tuple[str, ...]:
        return self.task_rejections + self.agent_rejections

    def to_dict(self) -> JsonDict:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "allowed": self.allowed,
            "task_rejections": list(self.task_rejections),
            "agent_rejections": list(self.agent_rejections),
        }


@dataclass(frozen=True)
class AssignmentProposal:
    round_id: str
    task_id: str
    agent_id: str
    task_rank: int
    agent_rank: int

    def to_dict(self) -> JsonDict:
        return {
            "round_id": self.round_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "task_rank": self.task_rank,
            "agent_rank": self.agent_rank,
        }


@dataclass(frozen=True)
class ClaimCommit:
    """Result of the authoritative database claim transaction."""

    committed: bool
    assignment: Optional[Mapping[str, Any]] = None
    rejection_reason: Optional[str] = None
    # Set only when the transaction proves that a different agent may still
    # claim this exact task (for example, the proposed agent filled its slot).
    retry_with_other_agent: bool = False

    @classmethod
    def success(cls, assignment: Mapping[str, Any]) -> "ClaimCommit":
        return cls(committed=True, assignment=dict(assignment))

    @classmethod
    def rejected(cls, reason: str, *, retry_with_other_agent: bool = False) -> "ClaimCommit":
        return cls(
            committed=False,
            rejection_reason=str(reason),
            retry_with_other_agent=retry_with_other_agent,
        )


@dataclass(frozen=True)
class AllocationAssignment:
    proposal: AssignmentProposal
    assignment: Mapping[str, Any]

    def to_dict(self) -> JsonDict:
        lease_id = self.assignment.get("lease_id")
        lease = self.assignment.get("lease")
        if not lease_id and isinstance(lease, Mapping):
            lease_id = lease.get("id")
        return {
            "task_id": self.proposal.task_id,
            "agent_id": self.proposal.agent_id,
            "lease_id": lease_id,
            "proposal": self.proposal.to_dict(),
            "assignment": dict(self.assignment),
        }


@dataclass(frozen=True)
class ClaimFailure:
    proposal: AssignmentProposal
    reason: str
    retry_with_other_agent: bool = False

    def to_dict(self) -> JsonDict:
        return {
            "task_id": self.proposal.task_id,
            "agent_id": self.proposal.agent_id,
            "reason": self.reason,
            "retry_with_other_agent": self.retry_with_other_agent,
            "proposal": self.proposal.to_dict(),
        }


@dataclass(frozen=True)
class TaskAllocationDecision:
    task_id: str
    status: str
    task_evaluation: PairEvaluation
    project: Optional[str] = None
    pair_evaluations: Tuple[PairEvaluation, ...] = ()
    proposal: Optional[AssignmentProposal] = None
    claim_failures: Tuple[ClaimFailure, ...] = ()

    def to_dict(self) -> JsonDict:
        return {
            "task_id": self.task_id,
            "project": self.project,
            "status": self.status,
            "task_evaluation": self.task_evaluation.to_dict(),
            "pair_evaluations": [evaluation.to_dict() for evaluation in self.pair_evaluations],
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "claim_failures": [failure.to_dict() for failure in self.claim_failures],
        }


@dataclass(frozen=True)
class AllocationRoundResult:
    round_id: str
    started_at: str
    completed_at: str
    task_count: int
    agent_count: int
    free_capacity: int
    ready_task_ids: Tuple[str, ...]
    available_agent_ids: Tuple[str, ...]
    assignments: Tuple[AllocationAssignment, ...]
    decisions: Tuple[TaskAllocationDecision, ...]
    completion_hook_error: Optional[str] = None

    @property
    def assigned_count(self) -> int:
        return len(self.assignments)

    @property
    def runnable_count(self) -> int:
        return sum(1 for decision in self.decisions if not decision.task_evaluation.task_rejections)

    @property
    def stranded_task_ids(self) -> Tuple[str, ...]:
        """Tasks that had a compatible pair but lost the transactional claim.

        A task deferred only because this caller requested a bounded round is
        not stranded, and a task with no compatible worker is provisioning
        demand rather than allocator failure.
        """
        return tuple(
            decision.task_id
            for decision in self.decisions
            if decision.status == "claim_rejected"
            and any(evaluation.allowed for evaluation in decision.pair_evaluations)
        )

    @property
    def unmatched_task_ids(self) -> Tuple[str, ...]:
        """Runnable tasks for which no hard-compatible worker exists."""

        return tuple(
            decision.task_id
            for decision in self.decisions
            if decision.status == "unmatched"
            and not any(evaluation.allowed for evaluation in decision.pair_evaluations)
        )

    @property
    def candidate_task_ids(self) -> Tuple[str, ...]:
        return tuple(
            decision.task_id
            for decision in self.decisions
            if any(evaluation.allowed for evaluation in decision.pair_evaluations)
        )

    @property
    def claim_failures(self) -> Tuple[ClaimFailure, ...]:
        return tuple(failure for decision in self.decisions for failure in decision.claim_failures)

    def to_dict(self) -> JsonDict:
        return {
            "round_id": self.round_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "task_count": self.task_count,
            "agent_count": self.agent_count,
            "free_capacity": self.free_capacity,
            "runnable_count": self.runnable_count,
            "assigned_count": self.assigned_count,
            "ready_task_ids": list(self.ready_task_ids),
            "candidate_task_ids": list(self.candidate_task_ids),
            "available_agent_ids": list(self.available_agent_ids),
            "stranded_task_ids": list(self.stranded_task_ids),
            "unmatched_task_ids": list(self.unmatched_task_ids),
            "assignments": [assignment.to_dict() for assignment in self.assignments],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "claim_failures": [failure.to_dict() for failure in self.claim_failures],
            "completion_hook_error": self.completion_hook_error,
        }

    def dispatch_telemetry_dict(self) -> JsonDict:
        """Return the bounded allocator facts needed by flow telemetry.

        ``to_dict`` is the operator/debug surface and intentionally contains
        every pair evaluation.  Calling it from the claim hot path amplified a
        round into a tasks-by-agents document only for analytics to discard
        nearly all of it.  This projection keeps allocation itself observable
        without serializing task payloads or the full compatibility matrix.
        """

        projects_by_task = {
            decision.task_id: decision.project for decision in self.decisions
        }
        projects = sorted(
            {
                project
                for project in projects_by_task.values()
                if project is not None
            }
        )
        assignments = []
        for item in self.assignments:
            lease_id = item.assignment.get("lease_id")
            lease = item.assignment.get("lease")
            if not lease_id and isinstance(lease, Mapping):
                lease_id = lease.get("id")
            assignments.append(
                {
                    "task_id": item.proposal.task_id,
                    "agent_id": item.proposal.agent_id,
                    "lease_id": lease_id,
                    "project": projects_by_task.get(item.proposal.task_id),
                }
            )
        return {
            "round_id": self.round_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "allocator_version": "mac.dispatch.allocator.v2",
            "source": "authoritative-allocator",
            "project": projects[0] if len(projects) == 1 else None,
            "projects": projects,
            "ready_count": len(self.ready_task_ids),
            "free_capacity": self.free_capacity,
            "ready_tasks": [
                {
                    "task_id": task_id,
                    "project": projects_by_task.get(task_id),
                }
                for task_id in self.ready_task_ids
            ],
            "free_agents": [
                {"agent_id": agent_id} for agent_id in self.available_agent_ids
            ],
            "assignments": assignments,
            "unmatched_tasks": [
                {
                    "task_id": task_id,
                    "project": projects_by_task.get(task_id),
                }
                for task_id in self.unmatched_task_ids
            ],
            "stranded_tasks": [
                {
                    "task_id": task_id,
                    "project": projects_by_task.get(task_id),
                }
                for task_id in self.stranded_task_ids
            ],
            "claim_failures": [
                {
                    "task_id": failure.proposal.task_id,
                    "agent_id": failure.proposal.agent_id,
                    "reason": failure.reason,
                    "retry_with_other_agent": failure.retry_with_other_agent,
                    "project": projects_by_task.get(failure.proposal.task_id),
                }
                for failure in self.claim_failures
            ],
        }


ClaimPair = Callable[[AssignmentProposal], ClaimCommit]
RoundCompleteHook = Callable[[AllocationRoundResult], None]


# --- requirement authority -------------------------------------------------
#
# A task states what it needs; an agent either meets that or does not. Sorting
# the rejection codes by WHY they fired is what lets the system answer a
# question it previously could not: "can anything in this fleet ever run this,
# or is it only busy right now?" Both used to collapse into no_eligible_agent.

#: The agent fails a requirement the task states. Structural: waiting does not
#: fix it. Someone must change the task, or teach/build an agent that fits.
REQUIREMENT_REJECTIONS: FrozenSet[str] = frozenset(
    {
        AGENT_CAPABILITIES_MISSING,
        AGENT_RESOURCES_INSUFFICIENT,
        AGENT_HARDWARE_INSUFFICIENT,
        AGENT_ROLE_INELIGIBLE,
        AGENT_ROLE_MISMATCH,
        AGENT_NO_EXECUTION_BOUNDARY,
    }
)

#: Also structural, but the remedy is an authorization change rather than a
#: capability one, so it is reported separately instead of being blurred in.
AUTHORIZATION_REJECTIONS: FrozenSet[str] = frozenset(
    {AGENT_TENANT_UNAUTHORIZED, AGENT_MACHINE_UNTRUSTED}
)

#: The same pair passes later with nothing reconfigured.
TRANSIENT_REJECTIONS: FrozenSet[str] = frozenset(
    {AGENT_OFFLINE, AGENT_UNHEALTHY, AGENT_HELD, AGENT_CAPACITY_FULL}
)

SATISFIABLE = "satisfiable"
UNSATISFIABLE = "unsatisfiable"
NO_AGENTS = "no_agents"


def rejection_kind(code: str) -> str:
    """Classify one rejection code as requirement / authorization / transient.

    ``agent_resources_insufficient`` carries the offending resource after a
    colon, so codes are matched on their stem.
    """
    stem = code.split(":", 1)[0]
    if stem in REQUIREMENT_REJECTIONS:
        return "requirement"
    if stem in AUTHORIZATION_REJECTIONS:
        return "authorization"
    if stem in TRANSIENT_REJECTIONS:
        return "transient"
    return "other"


@dataclass(frozen=True)
class RequirementEligibility:
    """Whether this fleet can ever satisfy one task's stated requirements."""

    task_id: str
    verdict: str
    considered_agent_ids: Tuple[str, ...] = ()
    capable_agent_ids: Tuple[str, ...] = ()
    unmet_requirements: Mapping[str, int] = field(default_factory=dict)

    @property
    def satisfiable(self) -> bool:
        return self.verdict == SATISFIABLE

    def to_dict(self) -> JsonDict:
        return {
            "task_id": self.task_id,
            "verdict": self.verdict,
            "satisfiable": self.satisfiable,
            "considered_agent_count": len(self.considered_agent_ids),
            "capable_agent_ids": list(self.capable_agent_ids),
            "capable_agent_count": len(self.capable_agent_ids),
            "unmet_requirements": dict(self.unmet_requirements),
        }


def classify_requirement_eligibility(
    task: AllocationTask,
    agents: Iterable[AllocationAgent],
) -> RequirementEligibility:
    """Can any agent here meet this task's requirements, ignoring how busy it is?

    Derived from :func:`evaluate_pair` rather than re-deriving the matching
    rules, so this answer cannot drift from the decision the dispatcher
    actually makes. The previous check compared ``required_capabilities``
    against the fleet by hand -- one of the five requirement dimensions -- so a
    task demanding hardware, resources, a role, or an execution boundary that
    nothing could provide looked perfectly dispatchable right up until it
    never dispatched.

    An agent counts as *capable* when its only rejections are transient. Being
    offline, held, unhealthy, or full says nothing about whether it meets the
    requirements, and a fleet that is merely busy must never be reported as
    one that cannot do the work.
    """
    # evaluate_pair runs the task-level gates first and returns early when any
    # of them fires, leaving agent_rejections empty. Asked about a task that is
    # held, waiting on a dependency, or out of attempts, this would then see no
    # rejections at all and call every agent capable -- reporting a fleet that
    # cannot do the work as one that can.
    #
    # Those gates answer "is this task ready?", which is a different question
    # and separately reported. Neutralise them so what remains is only the
    # requirement match.
    ready = replace(
        task,
        state="open",
        released=True,
        lease_id=None,
        dependencies_satisfied=True,
        project_registered=True,
        project_active=True,
        attempt_count=0,
        package_ready=True,
    )
    considered: list = []
    capable: list = []
    unmet: dict = {}
    for agent in agents:
        pair = evaluate_pair(ready, agent)
        # A task pinned to one agent (target/break-glass) makes every other
        # agent mismatch. That is the task's own routing, not evidence about
        # what the fleet can do, so those agents are not considered at all.
        if any(
            code.split(":", 1)[0] == AGENT_TARGET_MISMATCH
            for code in pair.agent_rejections
        ):
            continue
        considered.append(agent.id)
        blocking = [
            code
            for code in pair.agent_rejections
            if rejection_kind(code) in {"requirement", "authorization", "other"}
        ]
        if blocking:
            for code in blocking:
                unmet[code] = unmet.get(code, 0) + 1
        else:
            capable.append(agent.id)

    if not considered:
        verdict = NO_AGENTS
    elif capable:
        verdict = SATISFIABLE
    else:
        verdict = UNSATISFIABLE
    return RequirementEligibility(
        task_id=task.id,
        verdict=verdict,
        considered_agent_ids=tuple(considered),
        capable_agent_ids=tuple(capable),
        unmet_requirements=dict(sorted(unmet.items())),
    )


def evaluate_task(task: AllocationTask) -> PairEvaluation:
    """Evaluate task-level gates once, before looking at any agent."""

    reasons = []
    if task.state != "open":
        reasons.append(TASK_NOT_OPEN)
    if not task.released:
        reasons.append(TASK_HELD)
    if task.lease_id is not None:
        reasons.append(TASK_ALREADY_LEASED)
    if not task.dependencies_satisfied:
        reasons.append(TASK_DEPENDENCIES_INCOMPLETE)
    if not task.project_registered:
        reasons.append(TASK_PROJECT_UNREGISTERED)
    if not task.project_active:
        reasons.append(TASK_PROJECT_INACTIVE)
    if task.attempt_count >= task.max_attempts:
        reasons.append(TASK_ATTEMPTS_EXHAUSTED)
    if not task.package_ready:
        reasons.append(TASK_PACKAGE_NOT_READY)
    return PairEvaluation(task_id=task.id, agent_id=None, task_rejections=tuple(reasons))


def evaluate_pair(
    task: AllocationTask,
    agent: AllocationAgent,
    *,
    reserved_slots: int = 0,
) -> PairEvaluation:
    """Return every hard rejection for an otherwise runnable task/agent pair."""

    task_evaluation = evaluate_task(task)
    if task_evaluation.task_rejections:
        return PairEvaluation(
            task_id=task.id,
            agent_id=agent.id,
            task_rejections=task_evaluation.task_rejections,
        )

    reasons = []
    break_glass_active = task.break_glass_agent_id == agent.id
    if not task.required_role_known:
        reasons.append(TASK_REQUIRED_ROLE_UNKNOWN)
    if not agent.online:
        reasons.append(AGENT_OFFLINE)
    if not agent.healthy:
        reasons.append(AGENT_UNHEALTHY)
    if agent.free_slots <= reserved_slots:
        reasons.append(AGENT_CAPACITY_FULL)
    if not agent.machine_trusted:
        reasons.append(AGENT_MACHINE_UNTRUSTED)
    if agent.authorized_tenants is not None and task.tenant_id not in agent.authorized_tenants:
        reasons.append(AGENT_TENANT_UNAUTHORIZED)
    if task.tenant_id in agent.denied_tenants:
        reasons.append(AGENT_TENANT_UNAUTHORIZED)
    if not break_glass_active:
        if agent.dispatch_held:
            reasons.append(AGENT_HELD)
        if agent.id in task.excluded_agent_ids:
            reasons.append(AGENT_TARGET_MISMATCH)
        if task.target_agent_id is not None and task.target_agent_id != agent.id:
            reasons.append(AGENT_TARGET_MISMATCH)
        if not task.required_capabilities.issubset(agent.capabilities):
            reasons.append(AGENT_CAPABILITIES_MISSING)
        if not task.required_role_capabilities.issubset(agent.capabilities):
            reasons.append(AGENT_CAPABILITIES_MISSING)
        if task.requires_execution and not agent.execution_boundary_verified:
            reasons.append(AGENT_NO_EXECUTION_BOUNDARY)
        if not agent.bound_role_eligible:
            reasons.append(AGENT_ROLE_INELIGIBLE)
        if not agent.bound_role_required_capabilities.issubset(agent.capabilities):
            reasons.append(AGENT_ROLE_INELIGIBLE)
        if (
            task.required_role is not None
            and agent.bound_role_slug is not None
            and agent.bound_role_slug != task.required_role
        ):
            reasons.append(AGENT_ROLE_MISMATCH)
        hardware_ok, _hardware_reasons = machine_hardware_satisfies(
            dict(task.required_hardware),
            dict(agent.hardware),
        )
        if not hardware_ok:
            reasons.append(AGENT_HARDWARE_INSUFFICIENT)
        for resource, required in sorted(task.required_resources.items()):
            available = agent.resources.get(resource)
            if isinstance(required, (int, float)):
                try:
                    enough = available is not None and float(available) >= float(required)
                except (TypeError, ValueError):
                    enough = False
            elif isinstance(required, (list, tuple, set, frozenset)):
                enough = set(required).issubset(set(available or []))
            else:
                enough = required is None or available == required
            if not enough:
                reasons.append("%s:%s" % (AGENT_RESOURCES_INSUFFICIENT, resource))
    return PairEvaluation(
        task_id=task.id,
        agent_id=agent.id,
        agent_rejections=tuple(reasons),
    )


class AuthoritativeAllocator:
    """Deterministic, work-conserving batch matcher.

    The matcher never attempts to claim a task rejected by ``evaluate_task``.
    It records a complete decision for every input task and continues after a
    stale transactional claim, so one malformed/racing task cannot stall the
    rest of the round.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], str] = _utcnow,
        on_round_complete: Optional[RoundCompleteHook] = None,
    ) -> None:
        self._clock = clock
        self._on_round_complete = on_round_complete

    @staticmethod
    def _task_sort_key(task: AllocationTask) -> Tuple[int, float, str, str]:
        return (
            -int(task.priority),
            -float(task.order_signal),
            str(task.created_at),
            task.id,
        )

    @staticmethod
    def _agent_sort_key(
        task: AllocationTask,
        agent: AllocationAgent,
        reserved_slots: Mapping[str, int],
    ) -> Tuple[int, int, int, float, int, str]:
        assigned = int(reserved_slots.get(agent.id, 0))
        capacity = max(1, int(agent.capacity))
        load = (int(agent.active_leases) + assigned) / float(capacity)
        affinity = (
            0 if not agent.preferred_projects or task.project in agent.preferred_projects else 1
        )
        prior_participation = 1 if agent.id in task.avoid_agent_ids else 0
        # Prefer the least-specialized compatible worker so scarce capabilities
        # remain available to later tasks that require them.  This is the
        # deterministic augmenting-path heuristic for the common fleet case
        # where each loop worker owns one executor slot.
        capability_surplus = len(agent.capabilities - task.required_capabilities)
        return (
            affinity,
            prior_participation,
            capability_surplus,
            load,
            int(agent.active_leases) + assigned,
            agent.id,
        )

    def allocate_round(
        self,
        tasks: Iterable[AllocationTask],
        agents: Iterable[AllocationAgent],
        claim_pair: ClaimPair,
        *,
        max_assignments: Optional[int] = None,
        round_id: Optional[str] = None,
    ) -> AllocationRoundResult:
        started_at = self._clock()
        round_value = str(round_id or "dispatch_%s" % uuid4().hex)
        task_list = sorted(list(tasks), key=self._task_sort_key)
        agent_list = sorted(list(agents), key=lambda agent: agent.id)
        limit = (
            sum(agent.free_slots for agent in agent_list)
            if max_assignments is None
            else max(0, int(max_assignments))
        )
        ready_task_ids = tuple(task.id for task in task_list if evaluate_task(task).allowed)
        available_agent_ids = tuple(
            agent.id
            for agent in agent_list
            if agent.online
            and agent.healthy
            and not agent.dispatch_held
            and agent.machine_trusted
            and agent.free_slots > 0
        )
        task_evaluations = {task.id: evaluate_task(task) for task in task_list}
        base_pairs = {
            (task.id, agent.id): evaluate_pair(task, agent)
            for task in task_list
            if task_evaluations[task.id].allowed
            for agent in agent_list
        }

        # Plan a maximum-cardinality bipartite assignment before claiming.
        # Each capacity slot is explicit; augmenting paths can move a generic
        # task from a specialist to a generalist so specialized work is not
        # stranded by an otherwise valid greedy choice.
        tasks_by_id = {task.id: task for task in task_list}
        slot_owner: Dict[tuple[str, int], str] = {}
        task_slot: Dict[str, tuple[str, int]] = {}
        round_limited: set[str] = set()

        def candidate_slots(task: AllocationTask) -> list[tuple[str, int]]:
            ranked_agents = sorted(
                (
                    agent
                    for agent in agent_list
                    if base_pairs[(task.id, agent.id)].allowed
                ),
                key=lambda agent: self._agent_sort_key(task, agent, {}),
            )
            return [
                (agent.id, slot)
                for agent in ranked_agents
                for slot in range(agent.free_slots)
            ]

        def augment(
            task_id: str,
            seen_slots: set[tuple[str, int]],
            active_tasks: set[str],
        ) -> bool:
            if task_id in active_tasks:
                return False
            active_tasks.add(task_id)
            for slot in candidate_slots(tasks_by_id[task_id]):
                if slot in seen_slots:
                    continue
                seen_slots.add(slot)
                displaced = slot_owner.get(slot)
                if displaced is None or augment(displaced, seen_slots, active_tasks):
                    slot_owner[slot] = task_id
                    task_slot[task_id] = slot
                    active_tasks.remove(task_id)
                    return True
            active_tasks.remove(task_id)
            return False

        for task in task_list:
            if not task_evaluations[task.id].allowed:
                continue
            if len(task_slot) >= limit:
                round_limited.add(task.id)
                continue
            augment(task.id, set(), set())
        capacity_deferred = {
            task.id
            for task in task_list
            if task_evaluations[task.id].allowed
            and task.id not in task_slot
            and any(base_pairs[(task.id, agent.id)].allowed for agent in agent_list)
        }

        reserved_slots: Dict[str, int] = {}
        assignments: list[AllocationAssignment] = []
        decisions: list[TaskAllocationDecision] = []
        for task_rank, task in enumerate(task_list, start=1):
            task_evaluation = task_evaluations[task.id]
            if task_evaluation.task_rejections:
                decisions.append(
                    TaskAllocationDecision(
                        task_id=task.id,
                        project=task.project,
                        status="not_runnable",
                        task_evaluation=task_evaluation,
                    )
                )
                continue
            if task.id in round_limited or task.id in capacity_deferred:
                has_recovered_slot = (
                    len(assignments) < limit
                    and any(
                        evaluate_pair(
                            task,
                            agent,
                            reserved_slots=reserved_slots.get(agent.id, 0),
                        ).allowed
                        for agent in agent_list
                    )
                )
                if not has_recovered_slot:
                    decisions.append(
                        TaskAllocationDecision(
                            task_id=task.id,
                            project=task.project,
                            status="round_limit",
                            task_evaluation=task_evaluation,
                        )
                    )
                    continue

            planned_slot = task_slot.get(task.id)
            planned_agent_id = planned_slot[0] if planned_slot is not None else None
            ranked_agents = sorted(
                agent_list,
                key=lambda agent: (
                    0 if agent.id == planned_agent_id else 1,
                    *self._agent_sort_key(task, agent, reserved_slots),
                ),
            )
            pair_evaluations: list[PairEvaluation] = []
            claim_failures: list[ClaimFailure] = []
            selected_proposal = None
            status = "unmatched"
            for agent_rank, agent in enumerate(ranked_agents, start=1):
                evaluation = evaluate_pair(
                    task,
                    agent,
                    reserved_slots=reserved_slots.get(agent.id, 0),
                )
                pair_evaluations.append(evaluation)
                if not evaluation.allowed:
                    continue
                proposal = AssignmentProposal(
                    round_id=round_value,
                    task_id=task.id,
                    agent_id=agent.id,
                    task_rank=task_rank,
                    agent_rank=agent_rank,
                )
                try:
                    committed = claim_pair(proposal)
                except Exception as exc:  # Claim boundary must not abort a round.
                    committed = ClaimCommit.rejected(
                        "claim_exception:%s:%s" % (exc.__class__.__name__, str(exc))
                    )
                if committed.committed:
                    selected_proposal = proposal
                    reserved_slots[agent.id] = reserved_slots.get(agent.id, 0) + 1
                    assignments.append(
                        AllocationAssignment(
                            proposal=proposal,
                            assignment=dict(committed.assignment or {}),
                        )
                    )
                    status = "assigned"
                    break
                claim_failures.append(
                    ClaimFailure(
                        proposal=proposal,
                        reason=committed.rejection_reason or "claim_rejected",
                        retry_with_other_agent=committed.retry_with_other_agent,
                    )
                )
                status = "claim_rejected"
                if not committed.retry_with_other_agent:
                    break

            decisions.append(
                TaskAllocationDecision(
                    task_id=task.id,
                    project=task.project,
                    status=status,
                    task_evaluation=task_evaluation,
                    pair_evaluations=tuple(pair_evaluations),
                    proposal=selected_proposal,
                    claim_failures=tuple(claim_failures),
                )
            )

        result = AllocationRoundResult(
            round_id=round_value,
            started_at=started_at,
            completed_at=self._clock(),
            task_count=len(task_list),
            agent_count=len(agent_list),
            free_capacity=sum(agent.free_slots for agent in agent_list),
            ready_task_ids=ready_task_ids,
            available_agent_ids=available_agent_ids,
            assignments=tuple(assignments),
            decisions=tuple(decisions),
        )
        if self._on_round_complete is not None:
            try:
                self._on_round_complete(result)
            except Exception as exc:
                # Leases are already authoritative.  Telemetry may report its
                # own failure but must never make callers discard assignments.
                result = replace(
                    result,
                    completion_hook_error="%s:%s" % (exc.__class__.__name__, str(exc)),
                )
        return result


def adapt_v2_claim_primitive(
    claim: Callable[[str, str], Mapping[str, Any]],
    *,
    retryable_exceptions: Tuple[type[BaseException], ...] = (),
) -> ClaimPair:
    """Adapt an authoritative v2 atomic ``claim(task_id, agent_id)`` primitive.

    Do not wrap legacy ``ControlPlane.claim_task`` here: that method re-applies
    historical source cleanliness, command presence, coding-route, and
    package-specific predicates after v2 has selected a pair.  The supplied
    primitive must re-check only locked task state/dependencies, capacity,
    trust/tenant, explicit holds/targets, and hard capabilities while creating
    the lease and task transition in the same transaction.  PostgreSQL may
    implement it with ``FOR UPDATE SKIP LOCKED``.
    """

    def claim_pair(proposal: AssignmentProposal) -> ClaimCommit:
        try:
            return ClaimCommit.success(claim(proposal.task_id, proposal.agent_id))
        except Exception as exc:
            return ClaimCommit.rejected(
                "%s:%s" % (exc.__class__.__name__, str(exc)),
                retry_with_other_agent=bool(
                    retryable_exceptions and isinstance(exc, retryable_exceptions)
                ),
            )

    return claim_pair
