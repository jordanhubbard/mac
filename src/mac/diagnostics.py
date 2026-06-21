"""Read-only control-plane health diagnostics (``mac diagnostics``).

A small registry of checks an operator runs to spot fleet / database health
problems without mutating anything. Each check inspects the ControlPlane
read-only and returns :class:`Finding` records. New checks register by
decorating a function with :func:`register`, which appends to :data:`CHECKS` —
that list is the single integration point this module exposes, so independently
authored checks compose into one ``mac diagnostics`` report.

The framework is intentionally dependency-light and import-safe: registering a
check has no side effects beyond appending to the registry, and a check that
raises is isolated (reported as an ``error`` finding) rather than aborting the
rest of the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

#: Allowed finding severities, ascending.
SEVERITIES = ("ok", "warn", "error")

CheckFn = Callable[[Any], List["Finding"]]


@dataclass(frozen=True)
class Finding:
    """One result emitted by a diagnostic check."""

    check: str
    severity: str
    summary: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError("invalid severity %r (expected one of %r)" % (self.severity, SEVERITIES))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "summary": self.summary,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class Diagnostic:
    """A named, read-only health check over the ControlPlane."""

    name: str
    description: str
    run: CheckFn


#: The shared diagnostic registry. New checks append here via :func:`register`;
#: this is the integration point parallel contributions converge on.
CHECKS: List[Diagnostic] = []


def register(name: str, description: str) -> Callable[[CheckFn], CheckFn]:
    """Decorator: register ``fn`` as a diagnostic named ``name``.

    Raises on a duplicate name so two checks cannot silently shadow each other —
    a clear signal when independently authored checks collide on the registry.
    """

    def decorate(fn: CheckFn) -> CheckFn:
        if any(d.name == name for d in CHECKS):
            raise ValueError("duplicate diagnostic name: %s" % name)
        CHECKS.append(Diagnostic(name=name, description=description, run=fn))
        return fn

    return decorate


def run_diagnostics(control_plane: Any, names: Optional[Sequence[str]] = None) -> List[Finding]:
    """Run all registered checks (or just ``names``) and collect their findings.

    A check that raises is isolated into a single ``error`` finding so one
    broken check never aborts the rest of the report.
    """
    selected = set(names) if names else None
    findings: List[Finding] = []
    for diag in CHECKS:
        if selected is not None and diag.name not in selected:
            continue
        try:
            findings.extend(diag.run(control_plane))
        except Exception as exc:  # noqa: BLE001 - a broken check must not abort the rest
            findings.append(Finding(diag.name, "error", "check raised: %s" % exc))
    return findings


def summarize(findings: Sequence[Finding]) -> Dict[str, Any]:
    """A JSON-able report: severity counts, overall ok flag, and the findings."""
    counts = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return {
        "schema": "mac.diagnostics.report.v1",
        "checks": sorted(diag.name for diag in CHECKS),
        "counts": counts,
        "ok": counts["error"] == 0,
        "findings": [finding.as_dict() for finding in findings],
    }


# ---------------------------------------------------------------------------
# Registered checks. New diagnostics append a @register block below; keep each
# check in its own block so parallel additions integrate through CHECKS with
# minimal, well-scoped merge resolution.
# ---------------------------------------------------------------------------


@register("database-reachable", "the control-plane database answers a trivial query")
def _database_reachable(control_plane: Any) -> List[Finding]:
    control_plane.store.query_one("SELECT 1")
    return [Finding("database-reachable", "ok", "database answered SELECT 1")]


@register("stale-agents", "agents whose last_seen_at is older than the staleness threshold")
def _stale_agents(control_plane: Any, threshold_seconds: int = 3600) -> List[Finding]:
    from datetime import timedelta

    from mac.models import parse_time, utcnow

    # ISO-8601 UTC strings sort chronologically, so a lexicographic cutoff
    # comparison is equivalent to a chronological one. utcnow() returns the
    # canonical ISO string; parse it, subtract the threshold, re-render in the
    # same timespec so the comparison stays apples-to-apples.
    cutoff = (parse_time(utcnow()) - timedelta(seconds=threshold_seconds)).isoformat(
        timespec="microseconds"
    )
    rows = control_plane.store.query_all(
        "SELECT id, name, last_seen_at FROM agents "
        "WHERE last_seen_at IS NOT NULL AND last_seen_at < ?",
        (cutoff,),
    )
    if not rows:
        return [
            Finding(
                "stale-agents",
                "ok",
                "no stale agents",
                {"threshold_seconds": threshold_seconds, "cutoff": cutoff},
            )
        ]
    stale = [
        {"id": row["id"], "name": row["name"], "last_seen_at": row["last_seen_at"]}
        for row in rows
    ]
    return [
        Finding(
            "stale-agents",
            "warn",
            "%d stale agent(s) (last seen before %s)" % (len(stale), cutoff),
            {
                "threshold_seconds": threshold_seconds,
                "cutoff": cutoff,
                "count": len(stale),
                "agents": stale,
            },
        )
    ]


@register(
    "expired-active-leases",
    "leases still marked active whose expiry is in the past",
)
def _expired_active_leases(control_plane: Any) -> List[Finding]:
    from mac.models import utcnow

    now = utcnow()
    rows = control_plane.store.query_all(
        "SELECT id, task_id, agent_id FROM leases "
        "WHERE status = 'active' AND expires_at < ? "
        "ORDER BY expires_at",
        (now,),
    )
    if not rows:
        return [Finding("expired-active-leases", "ok", "no expired active leases")]
    offenders = [
        {"id": row["id"], "task_id": row["task_id"], "agent_id": row["agent_id"]}
        for row in rows
    ]
    return [
        Finding(
            "expired-active-leases",
            "warn",
            "%d active lease(s) past expiry" % len(offenders),
            {"lease_ids": [o["id"] for o in offenders], "leases": offenders},
        )
    ]


#: How many ``state='failed'`` tasks are tolerated before "failed-tasks" warns.
#: Default 0 so any failed task surfaces a warning; raise it to suppress a known
#: baseline of historical failures.
FAILED_TASKS_THRESHOLD = 0


@register("failed-tasks", "tasks stuck in the 'failed' state exceed the tolerated threshold")
def _failed_tasks(control_plane: Any, threshold: int = FAILED_TASKS_THRESHOLD) -> List[Finding]:
    rows = control_plane.store.query_all(
        "SELECT id, title, project FROM tasks WHERE state = 'failed' ORDER BY created_at DESC"
    )
    count = len(rows)
    if count <= threshold:
        return [
            Finding(
                "failed-tasks",
                "ok",
                "%d failed task(s) (threshold %d)" % (count, threshold),
                {"count": count, "threshold": threshold},
            )
        ]
    recent = [
        {"id": row["id"], "title": row["title"], "project": row["project"]}
        for row in rows[:10]
    ]
    return [
        Finding(
            "failed-tasks",
            "warn",
            "%d failed task(s) exceed threshold %d" % (count, threshold),
            {"count": count, "threshold": threshold, "recent": recent},
        )
    ]
