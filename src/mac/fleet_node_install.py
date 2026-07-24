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

When a phase fails the executor captures a secret-safe
:class:`PhaseFailureEvidence` record (command + redacted output) via
:func:`capture_phase_failure_evidence`, so a failed install can be
diagnosed later without leaking credentials. Callers persist those
records under the deploy phase-failure evidence directory, which
``fleet_deploy`` explicitly preserves through cleanup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# Exported plan schema identifier. Mirrors the ``mac.<module>.vN``
# convention used by adjacent fleet modules (e.g. ``ROLLOUT_PLAN_SCHEMA``
# in openclaw_fleet_rollout.py). Consumers pin plan compatibility against
# this value.
NODE_INSTALL_PLAN_SCHEMA = "mac.fleet_node_install.v1"

# Allowed lifecycle states for a single install phase.
PHASE_STATUSES = ("planned", "active", "succeeded", "failed", "skipped")

# Exported schema identifier for a single secret-safe phase-failure evidence
# record. Follows the same ``mac.<module>.vN`` convention as the plan schema so
# consumers can pin evidence compatibility independently of the plan version.
PHASE_FAILURE_EVIDENCE_SCHEMA = "mac.fleet_node_install.phase_failure.v1"

# Placeholder substituted for any redacted secret value. Kept identical to the
# fleet_setup ``<set>`` convention family so redacted evidence reads uniformly
# across the fleet tooling.
REDACTED_PLACEHOLDER = "<redacted>"

# Substrings that mark an assignment key (or URL credential field) as
# secret-bearing. Mirrors ``fleet_setup._looks_secret`` so redaction stays
# consistent with the rest of the deploy tooling.
_SECRET_KEY_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

# ``NAME=value`` / ``NAME: value`` assignments whose key looks secret. The value
# (everything up to whitespace) is replaced with the placeholder.
_ASSIGNMENT_RE = re.compile(
    r"(?P<key>[A-Za-z0-9_.-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z0-9_.-]*)"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?P<val>\S+)",
    re.IGNORECASE,
)

# ``Authorization: Bearer <token>`` / ``Bearer <token>`` / ``token <token>``
# style header credentials.
_BEARER_RE = re.compile(
    r"(?P<scheme>\b(?:bearer|token)\s+)(?P<val>[A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)

# Credentials embedded in an authenticated URL (``https://user:pass@host`` and
# the common ``https://x-access-token:<token>@host`` GitHub form).
_URL_CRED_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@",
)


def redact_secret_text(text: str) -> str:
    """Return *text* with secret-bearing values replaced by a placeholder.

    Redacts three families of secrets that routinely leak into command output:
    ``NAME=secret``/``NAME: secret`` assignments whose key looks secret,
    ``Bearer``/``token`` header credentials, and credentials embedded in an
    authenticated URL (``scheme://userinfo@host``). The surrounding structure
    (keys, schemes, hosts) is preserved so the evidence stays diagnostic while
    the secret material itself never survives.
    """

    if not text:
        return text

    def _assign(match: "re.Match[str]") -> str:
        return "%s%s%s" % (match.group("key"), match.group("sep"), REDACTED_PLACEHOLDER)

    def _bearer(match: "re.Match[str]") -> str:
        return "%s%s" % (match.group("scheme"), REDACTED_PLACEHOLDER)

    def _url(match: "re.Match[str]") -> str:
        return "%s%s@" % (match.group("scheme"), REDACTED_PLACEHOLDER)

    # URL credentials first: a ``scheme://user:token@host`` userinfo field can
    # otherwise be mis-consumed by the greedy assignment rule (whose value runs
    # to the next whitespace), which would swallow the host too. Redacting the
    # userinfo first keeps the diagnostic host/scheme visible.
    redacted = _URL_CRED_RE.sub(_url, text)
    redacted = _ASSIGNMENT_RE.sub(_assign, redacted)
    redacted = _BEARER_RE.sub(_bearer, redacted)
    return redacted


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


@dataclass(frozen=True)
class PhaseFailureEvidence:
    """Secret-safe record of why a single install phase failed.

    Every text field is redacted through :func:`redact_secret_text` at
    construction time via :func:`capture_phase_failure_evidence`, so an
    instance can be persisted or logged without leaking credentials. The
    record is intentionally minimal and diagnostic: which phase failed, its
    order, the (redacted) command metadata, and the (redacted) captured
    output lines.
    """

    phase: str
    order: int
    command: Optional[str] = None
    detail: List[str] = field(default_factory=list)
    schema: str = PHASE_FAILURE_EVIDENCE_SCHEMA

    def to_dict(self) -> Dict[str, object]:
        """Return a JSON-serialisable mapping of the redacted evidence."""
        return {
            "schema": self.schema,
            "phase": self.phase,
            "order": self.order,
            "command": self.command,
            "detail": list(self.detail),
        }


@dataclass
class NodeInstallResult:
    """Summary of a completed (or halted) node installation run."""

    version: str
    succeeded: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failure_evidence: List[PhaseFailureEvidence] = field(default_factory=list)

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


def capture_phase_failure_evidence(
    phase: "InstallPhase",
    output: Optional[str] = None,
) -> PhaseFailureEvidence:
    """Build a secret-safe :class:`PhaseFailureEvidence` for a failed *phase*.

    The phase command and each line of *output* are passed through
    :func:`redact_secret_text` so no credential material survives into the
    returned record. Blank output lines are dropped; the record is safe to
    persist next to (and preserve through) deploy cleanup.
    """

    command = redact_secret_text(phase.command) if phase.command else None
    detail: List[str] = []
    if output:
        for line in output.splitlines():
            stripped = line.rstrip()
            if not stripped.strip():
                continue
            detail.append(redact_secret_text(stripped))
    return PhaseFailureEvidence(
        phase=phase.name,
        order=phase.order,
        command=command,
        detail=detail,
    )


def execute_node_install(
    plan: NodeInstallPlan,
    *,
    run_fn: Optional[Callable[[InstallPhase], bool]] = None,
    output_fn: Optional[Callable[[InstallPhase], Optional[str]]] = None,
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
        output_fn: Optional callable invoked with a failed
            :class:`InstallPhase` to retrieve its raw captured output. The
            output is redacted into secret-safe
            :class:`PhaseFailureEvidence` and appended to
            ``result.failure_evidence``. When omitted, evidence is still
            recorded (command only, no output detail).
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
            output = output_fn(phase) if output_fn is not None else None
            result.failure_evidence.append(
                capture_phase_failure_evidence(phase, output)
            )
            halted = True

    return result
