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

#: Name of the check that reports the authoritative backend identity. It always
#: runs (even under a subset selection) and feeds the report's ``data_source``.
DATA_SOURCE_CHECK = "data-source-identity"

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


def _recent_rows(
    control_plane: Any,
    sql: str,
    params: Sequence[Any] = (),
    *,
    limit: int = 10,
) -> tuple[int, List[Dict[str, Any]]]:
    """Run ``sql`` and return ``(total_count, first ``limit`` rows as dicts)``.

    The full result is counted so callers can compare against a threshold, but
    only the first ``limit`` rows are materialized as plain dicts (sqlite3.Row
    converted) for inclusion in a Finding's detail. ``params`` is forwarded to
    the query, so callers keep ordering / filtering in their SQL.
    """
    rows = control_plane.store.query_all(sql, tuple(params))
    recent = [dict(row) for row in rows[:limit]]
    return len(rows), recent


def _threshold_finding(
    check: str,
    count: int,
    threshold: int,
    recent: List[Dict[str, Any]],
    noun: str,
) -> Finding:
    """Build the uniform ok/warn Finding for a "count vs. threshold" check.

    ``ok`` when ``count <= threshold`` (summary ``"%d <noun> (threshold %d)"``,
    detail ``{count, threshold}``); otherwise ``warn`` (summary
    ``"%d <noun> exceed threshold %d"``, detail ``{count, threshold, recent}``).
    """
    if count <= threshold:
        return Finding(
            check,
            "ok",
            "%d %s (threshold %d)" % (count, noun, threshold),
            {"count": count, "threshold": threshold},
        )
    return Finding(
        check,
        "warn",
        "%d %s exceed threshold %d" % (count, noun, threshold),
        {"count": count, "threshold": threshold, "recent": recent},
    )


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
    report = {
        "schema": "mac.diagnostics.report.v1",
        "checks": sorted(diag.name for diag in CHECKS),
        "counts": counts,
        "ok": counts["error"] == 0,
        "findings": [finding.as_dict() for finding in findings],
    }
    for finding in findings:
        if finding.check == DATA_SOURCE_CHECK:
            # Surface the authoritative backend identity as a top-level,
            # machine-readable block so consumers (and the hub client, which
            # augments it with the hub URL) do not have to scan findings.
            report["data_source"] = dict(finding.detail)
            break
    return report


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
    count, recent = _recent_rows(
        control_plane,
        "SELECT id, title, project FROM tasks WHERE state = 'failed' ORDER BY created_at DESC",
    )
    return [_threshold_finding("failed-tasks", count, threshold, recent, "failed task(s)")]


#: How many stranded replacement chains are tolerated before "stranded-replacements" warns.
#: Default 0 so any stranded chain surfaces a warning.
STRANDED_REPLACEMENTS_THRESHOLD = 0


@register(
    "stranded-replacements",
    "terminal tasks whose replacement_task_id chain ends without a live or completed successor",
)
def _stranded_replacements(
    control_plane: Any, threshold: int = STRANDED_REPLACEMENTS_THRESHOLD
) -> List[Finding]:
    """Find terminal tasks whose replacement chain is stranded.

    A replacement chain is stranded when a cancelled or failed task's
    ``repository_ref_lifecycle.replacement_task_id`` pointer ultimately leads to
    another terminal task with no live or completed successor.  Stranded chains
    block ref-lifecycle progress indefinitely and should be repaired by creating
    a new successor task.
    """
    from mac.repository_hygiene import walk_replacement_chain

    rows = control_plane.store.query_all(
        "SELECT id, title, project, state "
        "FROM tasks "
        "WHERE state IN ('failed', 'cancelled') "
        "  AND json_extract(metadata, '$.repository_ref_lifecycle.replacement_task_id') IS NOT NULL "
        "ORDER BY updated_at DESC"
    )

    def _get_task(task_id: str) -> Optional[Dict[str, Any]]:
        row = control_plane.store.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            return None
        import json as _json

        metadata = row["metadata"]
        if isinstance(metadata, str):
            try:
                metadata = _json.loads(metadata)
            except Exception:
                metadata = {}
        return {"state": row["state"], "metadata": metadata}

    stranded: List[Dict[str, Any]] = []
    for row in rows:
        result = walk_replacement_chain(row["id"], _get_task)
        if result.status == "stranded":
            stranded.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "project": row["project"],
                    "state": row["state"],
                    "chain": result.chain,
                    "remediation": result.remediation,
                }
            )

    count = len(stranded)
    recent = stranded[:10]
    return [
        _threshold_finding(
            "stranded-replacements", count, threshold, recent, "stranded replacement chain(s)"
        )
    ]


def backend_identity(control_plane: Any) -> Dict[str, Any]:
    """Return the authoritative backend identity for ``control_plane``.

    Prefers the store's own :meth:`backend_identity` (SQLite / Postgres) and
    falls back to a best-effort description for stores that predate it. Always
    marks the result ``authoritative`` so a consumer can tell a durable hub
    authority apart from an incidental client-side database.
    """
    store = getattr(control_plane, "store", None)
    identify = getattr(store, "backend_identity", None)
    if callable(identify):
        identity = dict(identify())
    else:
        path = getattr(store, "path", None)
        identity = {
            "backend": type(store).__name__ if store is not None else "unknown",
            "location": path,
            "in_memory": path == ":memory:",
        }
    identity.setdefault("authoritative", True)
    return identity


@register(
    "data-source-identity",
    "identifies the authoritative backend the checks ran against (backend type + location)",
)
def _data_source_identity(control_plane: Any) -> List[Finding]:
    """Report which authoritative backend served this diagnostics run.

    This is the anti-"accidental local database" guard: it names the backend
    family (``sqlite`` / ``postgres``) and a redacted, non-secret locator so an
    operator can confirm a report ran against the intended hub authority rather
    than a stray ``~/.mac/mac.db``.  ``run_diagnostics`` always includes this
    check even when a subset is requested, and :func:`summarize` promotes its
    detail to the report's top-level ``data_source`` block.

    An in-memory store is reported as ``warn`` because it is ephemeral and never
    the durable fleet authority; a durable file/DSN backend is ``ok``.
    """
    identity = backend_identity(control_plane)
    backend = identity.get("backend", "unknown")
    location = identity.get("location")
    if identity.get("in_memory"):
        return [
            Finding(
                "data-source-identity",
                "warn",
                "authority is an ephemeral in-memory %s store" % backend,
                identity,
            )
        ]
    return [
        Finding(
            "data-source-identity",
            "ok",
            "authority backend %s (%s)" % (backend, location),
            identity,
        )
    ]


#: How long a task may dwell in one non-terminal lifecycle stage before the
#: "lifecycle-stage-dwell" check warns. Default 24h; tasks that legitimately
#: wait longer (e.g. blocked on a dependency) can raise this per invocation.
LIFECYCLE_STAGE_DWELL_THRESHOLD_SECONDS = 24 * 3600


@register(
    "lifecycle-stage-dwell",
    "non-terminal tasks that have dwelled in one lifecycle stage past the threshold",
)
def _lifecycle_stage_dwell(
    control_plane: Any,
    threshold_seconds: int = LIFECYCLE_STAGE_DWELL_THRESHOLD_SECONDS,
) -> List[Finding]:
    """Flag tasks stuck in a single non-terminal stage for too long.

    A task's ``state`` is its lifecycle stage and ``updated_at`` is when it last
    changed stage, so ``now - updated_at`` is the current-stage dwell.  Terminal
    stages (completed/failed/cancelled) are excluded — dwelling there is
    expected.  The status is derived on demand from live rows, so the check
    emits no periodic events; it only ever reports the present standing.
    """
    from datetime import timedelta

    from mac.models import TERMINAL_TASK_STATES, parse_time, utcnow

    now_iso = utcnow()
    cutoff = (parse_time(now_iso) - timedelta(seconds=threshold_seconds)).isoformat(
        timespec="microseconds"
    )
    placeholders = ", ".join("?" for _ in TERMINAL_TASK_STATES)
    terminal = tuple(sorted(TERMINAL_TASK_STATES))
    rows = control_plane.store.query_all(
        "SELECT id, title, project, state, updated_at FROM tasks "
        "WHERE state NOT IN (%s) AND updated_at IS NOT NULL AND updated_at < ? "
        "ORDER BY updated_at" % placeholders,
        (*terminal, cutoff),
    )
    if not rows:
        return [
            Finding(
                "lifecycle-stage-dwell",
                "ok",
                "no task dwelling in a stage past threshold",
                {"threshold_seconds": threshold_seconds, "cutoff": cutoff},
            )
        ]
    now = parse_time(now_iso)
    stuck: List[Dict[str, Any]] = []
    for row in rows:
        try:
            dwell = (now - parse_time(row["updated_at"])).total_seconds()
        except Exception:
            dwell = None
        stuck.append(
            {
                "id": row["id"],
                "title": row["title"],
                "project": row["project"],
                "stage": row["state"],
                "since": row["updated_at"],
                "dwell_seconds": dwell,
            }
        )
    return [
        Finding(
            "lifecycle-stage-dwell",
            "warn",
            "%d task(s) dwelling in one stage past %d seconds" % (len(stuck), threshold_seconds),
            {
                "threshold_seconds": threshold_seconds,
                "cutoff": cutoff,
                "count": len(stuck),
                "tasks": stuck[:10],
            },
        )
    ]
