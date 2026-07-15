"""Fleet node installation phase library for project ``mac``.

Provides a pure, side-effect-free model for planning and executing the
ordered installation phases a fleet node goes through when it is brought
online.  The design mirrors the adjacent fleet modules
(``openclaw_fleet_rollout.py`` and ``fleet_deploy.py``): frozen/regular
dataclasses, explicit typing, plan-building via pure functions, and an
executor whose side effects are supplied by an injected callable so the
whole module is unit-testable without touching the network, SSH, or
subprocesses.

Phase status values: planned, active, succeeded, failed, skipped
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# Exported plan schema identifier. Mirrors the ``mac.<module>.vN``
# convention used by adjacent fleet modules (e.g. ``ROLLOUT_PLAN_SCHEMA``
# in openclaw_fleet_rollout.py). Consumers pin plan compatibility against
# this value.
NODE_INSTALL_PLAN_SCHEMA = "mac.fleet_node_install.v1"

# Allowed lifecycle states for a single install phase.
PHASE_STATUSES = ("planned", "active", "succeeded", "failed", "skipped")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class InstallPhase:
    """A single ordered phase in a fleet node installation.

    Attributes:
        name: Unique phase identifier within a plan.
        order: 1-based execution order. Phases run in ascending order.
        description: Human-readable summary of what the phase does.
        status: One of :data:`PHASE_STATUSES`.
        command: Optional command string associated with the phase. Kept as
            metadata only; the executor never runs it directly (side effects
            are the responsibility of the injected runner callable).
        depends_on: Optional list of phase names that must succeed before
            this phase may run.
    """

    name: str
    order: int
    description: str = ""
    status: str = "planned"
    command: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "skipped")


@dataclass
class NodeInstallPlan:
    """Complete ordered install plan for a single fleet node."""

    version: str
    phases: List[InstallPhase] = field(default_factory=list)

    # Derived helpers -------------------------------------------------------

    @property
    def ordered_phases(self) -> List[InstallPhase]:
        """Phases sorted by their ``order`` field (ascending)."""
        return sorted(self.phases, key=lambda phase: phase.order)

    def phase_for_name(self, name: str) -> Optional[InstallPhase]:
        for phase in self.phases:
            if phase.name == name:
                return phase
        return None

    @property
    def pending_phases(self) -> List[InstallPhase]:
        return [p for p in self.ordered_phases if p.status in ("planned", "active")]

    @property
    def completed_phases(self) -> List[InstallPhase]:
        return [p for p in self.ordered_phases if p.status == "succeeded"]


@dataclass
class NodeInstallResult:
    """Summary of a completed (or halted) node installation run."""

    version: str
    succeeded: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------


def build_node_install_plan(
    version: str,
    phases: List[Dict[str, object]],
) -> NodeInstallPlan:
    """Validate *phases* and return an ordered :class:`NodeInstallPlan`.

    Each entry in *phases* is a mapping with at least a ``name`` key. The
    optional keys are ``order`` (int; defaults to declaration position),
    ``description`` (str), ``command`` (str), and ``depends_on`` (list of
    phase names).

    Args:
        version: Plan version string. Must be non-empty.
        phases: Ordered list of phase mapping dicts. Must be non-empty.

    Returns:
        A :class:`NodeInstallPlan` whose phases are ordered by ``order`` and
        all set to ``"planned"``.

    Raises:
        ValueError: If *version* is empty, *phases* is empty, a phase is
            missing its name, phase names are duplicated, a declared
            dependency does not exist, or two phases share the same order.
    """
    if not version or not version.strip():
        raise ValueError("node install plan version is required")
    if not phases:
        raise ValueError("at least one install phase is required")

    built: List[InstallPhase] = []
    seen_names: set[str] = set()
    seen_orders: set[int] = set()

    for index, entry in enumerate(phases):
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError("phase at index %d is missing 'name'" % index)
        if name in seen_names:
            raise ValueError("duplicate phase name: %r" % name)
        seen_names.add(name)

        raw_order = entry.get("order")
        order = int(raw_order) if raw_order is not None else index + 1
        if order <= 0:
            raise ValueError("phase %r has invalid order %d (must be >= 1)" % (name, order))
        if order in seen_orders:
            raise ValueError("duplicate phase order %d (phase %r)" % (order, name))
        seen_orders.add(order)

        raw_depends = entry.get("depends_on") or []
        if not isinstance(raw_depends, (list, tuple)):
            raise ValueError("phase %r depends_on must be a list" % name)
        depends_on = [str(dep).strip() for dep in raw_depends]

        description = str(entry.get("description") or "")
        command = entry.get("command")
        command_str = str(command) if command is not None else None

        built.append(
            InstallPhase(
                name=name,
                order=order,
                description=description,
                status="planned",
                command=command_str,
                depends_on=depends_on,
            )
        )

    # Validate dependency references now that every phase name is known.
    for phase in built:
        for dep in phase.depends_on:
            if dep not in seen_names:
                raise ValueError(
                    "phase %r depends on unknown phase %r" % (phase.name, dep)
                )
            if dep == phase.name:
                raise ValueError("phase %r cannot depend on itself" % phase.name)

    built.sort(key=lambda phase: phase.order)
    return NodeInstallPlan(version=version.strip(), phases=built)


# ---------------------------------------------------------------------------
# Executor / simulator
# ---------------------------------------------------------------------------


def execute_node_install(
    plan: NodeInstallPlan,
    *,
    run_fn: Optional[Callable[[InstallPhase], bool]] = None,
    simulate: bool = False,
) -> NodeInstallResult:
    """Execute (or simulate) *plan* phase-by-phase in order.

    Phases run in ascending ``order``. Each phase is set to ``"active"``
    while running, then ``"succeeded"`` or ``"failed"``. The first failure
    halts the run; every remaining phase is marked ``"skipped"``. A phase is
    also skipped (without running) if any of its ``depends_on`` phases did
    not succeed.

    Args:
        plan: The plan returned by :func:`build_node_install_plan`.
        run_fn: Callable invoked with each :class:`InstallPhase`, returning
            ``True`` on success. Required unless *simulate* is ``True``.
        simulate: When ``True`` every runnable phase is marked
            ``"succeeded"`` without calling *run_fn*.

    Returns:
        A :class:`NodeInstallResult` summarising the run.

    Raises:
        ValueError: If *simulate* is ``False`` and *run_fn* is not supplied.
    """
    if not simulate and run_fn is None:
        raise ValueError("run_fn is required unless simulate=True")

    result = NodeInstallResult(version=plan.version)
    halted = False
    succeeded_names: set[str] = set()

    for phase in plan.ordered_phases:
        if halted:
            phase.status = "skipped"
            result.skipped.append(phase.name)
            continue

        unmet = [dep for dep in phase.depends_on if dep not in succeeded_names]
        if unmet:
            phase.status = "skipped"
            result.skipped.append(phase.name)
            continue

        phase.status = "active"

        if simulate:
            phase.status = "succeeded"
            succeeded_names.add(phase.name)
            result.succeeded.append(phase.name)
            continue

        assert run_fn is not None  # narrowed by the guard above
        ok = run_fn(phase)
        if ok:
            phase.status = "succeeded"
            succeeded_names.add(phase.name)
            result.succeeded.append(phase.name)
        else:
            phase.status = "failed"
            result.failed.append(phase.name)
            halted = True

    return result
