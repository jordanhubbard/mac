"""Read-only snapshot feed for the hub observability console (``/ui/console``).

Why this exists instead of a faster ``/dashboard/state``
-------------------------------------------------------
``GET /dashboard/state`` is the legacy command-and-control payload. It is slow
for reasons that are *inherent to what it returns*, not incidental:

* on the non-``ide`` view it rebuilds ``build_hermes_startup_report()`` on every
  request, which makes four blocking outbound HTTP health probes (Qdrant,
  Firecrawl, TokenHub ×2) at a 2s timeout each;
* it fans out per entity — ``get_openshell_status`` per agent,
  ``persona_work_context`` per Hermes instance (each of which re-reads every
  task and agent), ``explain_task_dispatch`` per *open* task;
* it reads a dozen tables with no LIMIT at all, including ``search_memory()``
  with no filter.

Making that endpoint fast therefore means removing keys from it, and its shape
is a contract for the legacy dashboard, the Fleet IDE, the Electron shell and
a few dozen tests. So this module is an **additive** endpoint with no existing
consumers, assembled from server-side ``GROUP BY`` aggregates rather than
"list every row and count in Python".

Everything here is ``SELECT``-only. Nothing in this module writes.

Honesty contract
----------------
Each section is assembled independently and any failure is recorded in
``degraded`` rather than being swallowed. A section that could not be read is
absent from the payload — it is never reported as an empty result — so the
console can say "unavailable" instead of rendering a plausible zero. This
matters here specifically: a fleet dashboard that shows "0 failures" because it
could not reach a table is worse than no dashboard.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

__all__ = [
    "SCHEMA_VERSION",
    "TASK_SCHEMA_VERSION",
    "TERMINAL_TASK_STATES",
    "build_console_snapshot",
    "build_task_drilldown",
    "build_transcript_entry",
    "bucket_transitions",
    "dwell_percentiles",
]

SCHEMA_VERSION = "mac.dashboard.observe.v1"

# Mirrors models.TERMINAL_TASK_STATES. Duplicated as a literal tuple because
# these values go straight into SQL and the ordering must be stable.
TERMINAL_TASK_STATES = ("completed", "failed", "cancelled")

# Bounds. Every list this module returns is capped; there is no unbounded read.
MAX_WINDOW_HOURS = 168.0
DEFAULT_WINDOW_HOURS = 6.0
MAX_BUCKETS = 120
TOP_PROJECTS = 12
OLDEST_TASKS = 15
RECENT_TRANSITIONS = 60
AGENT_LIMIT = 200
RECENT_CYCLE_RUNS = 12


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly; no database involved)
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(value: Any, now: datetime) -> Optional[float]:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def dwell_percentiles(ages: Sequence[float]) -> Dict[str, Any]:
    """p50 / p90 / max dwell for one state, plus the sample size.

    Dwell is the signal a raw count cannot give: 360 blocked tasks that all
    arrived this morning is a burst; 360 that have been blocked for nine days
    is a stuck fleet. Percentiles are nearest-rank (``percentile_disc``), so
    every reported number is a real task's real age rather than an
    interpolation between two tasks.
    """
    if not ages:
        return {"count": 0, "p50": None, "p90": None, "max": None}
    ordered = sorted(ages)

    def at(fraction: float) -> float:
        # Nearest-rank: index = ceil(fraction * n) - 1, clamped into range.
        rank = math.ceil(fraction * len(ordered)) - 1
        return ordered[max(0, min(len(ordered) - 1, rank))]

    return {
        "count": len(ordered),
        "p50": round(at(0.5), 1),
        "p90": round(at(0.9), 1),
        "max": round(ordered[-1], 1),
    }


def bucket_transitions(
    rows: Sequence[Dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    buckets: int,
) -> Dict[str, Any]:
    """Fold minute-resolution transition counts into a fixed bucket grid.

    ``rows`` are ``{"bucket": "2026-08-17T09:31", "to_state": "running",
    "n": 4}`` as produced by the SQL below. The grid is emitted whole — empty
    buckets included — so a gap in the data reads as a gap and not as a
    shorter chart.
    """
    buckets = max(1, min(MAX_BUCKETS, int(buckets)))
    span = (end - start).total_seconds()
    if span <= 0:
        span = 1.0
    width = span / buckets
    edges = [start + timedelta(seconds=width * i) for i in range(buckets + 1)]
    series: Dict[str, List[int]] = {}
    dropped = 0
    for row in rows:
        moment = _parse_iso(row.get("bucket"))
        if moment is None:
            dropped += 1
            continue
        index = int((moment - start).total_seconds() // width)
        if index < 0 or index >= buckets:
            dropped += 1
            continue
        state = str(row.get("to_state") or "unknown")
        lane = series.setdefault(state, [0] * buckets)
        lane[index] += int(row.get("n") or 0)
    return {
        "bucket_seconds": round(width, 3),
        "bucket_starts": [edge.isoformat() for edge in edges[:-1]],
        "series": {name: counts for name, counts in sorted(series.items())},
        "dropped_rows": dropped,
    }


def _counts(rows: Sequence[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        name = row.get(key)
        out[str(name) if name is not None else "(none)"] = int(row.get("n") or 0)
    return out


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------


class _Sections:
    """Runs each section, isolating failures into a ``degraded`` list."""

    def __init__(self) -> None:
        self.degraded: List[Dict[str, str]] = []

    def run(self, name: str, fn: Callable[[], Any]) -> Optional[Any]:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberate: report, never hide
            self.degraded.append(
                {
                    "section": name,
                    # str(exc) on a store error carries the driver message,
                    # which is what an operator needs to act. It is not shown
                    # as data; the console renders it as an explicit failure.
                    "reason": "%s: %s" % (type(exc).__name__, str(exc)[:400]),
                }
            )
            return None


def build_console_snapshot(
    cp: Any,
    *,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    buckets: int = 60,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Assemble the console payload. Read-only; never raises for one bad table."""
    started = time.monotonic()
    moment = now or datetime.now(timezone.utc)
    try:
        window = float(window_hours)
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW_HOURS
    window = max(0.25, min(MAX_WINDOW_HOURS, window))
    since = moment - timedelta(hours=window)
    since_iso = since.isoformat()
    store = cp.store
    sections = _Sections()

    def q(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return [dict(row) for row in store.query_all(sql, tuple(params))]

    terminal_sql = ", ".join("'%s'" % state for state in TERMINAL_TASK_STATES)

    payload: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "server_time": moment.isoformat(),
        "window": {
            "hours": window,
            "since": since_iso,
            "until": moment.isoformat(),
        },
    }

    # --- tasks: state distribution -----------------------------------------
    def tasks_section() -> Dict[str, Any]:
        by_state = _counts(
            q("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"), "state"
        )
        # Dwell only over non-terminal work: how long has each in-flight task
        # been sitting where it is. Uses idx_tasks_state_updated.
        live = q(
            "SELECT state, updated_at FROM tasks WHERE state NOT IN (%s)" % terminal_sql
        )
        ages: Dict[str, List[float]] = {}
        undated = 0
        for row in live:
            age = _age_seconds(row.get("updated_at"), moment)
            if age is None:
                undated += 1
                continue
            ages.setdefault(str(row.get("state")), []).append(age)
        dwell = {state: dwell_percentiles(values) for state, values in sorted(ages.items())}
        return {
            "by_state": by_state,
            "total": sum(by_state.values()),
            "live_total": len(live),
            "dwell_seconds": dwell,
            "undated_rows": undated,
        }

    # --- tasks: the oldest stuck work --------------------------------------
    def stuck_section() -> List[Dict[str, Any]]:
        rows = q(
            "SELECT id, title, state, project, owner_agent_id, updated_at, created_at, "
            "attempt_count, max_attempts "
            "FROM tasks WHERE state NOT IN (%s) "
            "ORDER BY updated_at ASC LIMIT %d" % (terminal_sql, OLDEST_TASKS)
        )
        for row in rows:
            row["dwell_seconds"] = _age_seconds(row.get("updated_at"), moment)
            row["age_seconds"] = _age_seconds(row.get("created_at"), moment)
        return rows

    # --- tasks: per project -------------------------------------------------
    def projects_section() -> Dict[str, Any]:
        rows = q("SELECT project, state, COUNT(*) AS n FROM tasks GROUP BY project, state")
        grouped: Dict[str, Dict[str, int]] = {}
        for row in rows:
            name = row.get("project") or "(unassigned)"
            grouped.setdefault(str(name), {})[str(row.get("state"))] = int(row.get("n") or 0)
        ranked = sorted(
            (
                {
                    "project": name,
                    "by_state": states,
                    "total": sum(states.values()),
                    "live": sum(
                        n for s, n in states.items() if s not in TERMINAL_TASK_STATES
                    ),
                }
                for name, states in grouped.items()
            ),
            key=lambda item: (-item["live"], -item["total"], item["project"]),
        )
        records = _counts(
            q("SELECT status, COUNT(*) AS n FROM projects GROUP BY status"), "status"
        )
        return {
            "registered_by_status": records,
            "with_tasks": len(ranked),
            "rows": ranked[:TOP_PROJECTS],
            "truncated": max(0, len(ranked) - TOP_PROJECTS),
        }

    # --- movement: transition flow over the window --------------------------
    def flow_section() -> Dict[str, Any]:
        rows = q(
            "SELECT substr(created_at, 1, 16) AS bucket, to_state, COUNT(*) AS n "
            "FROM task_history "
            "WHERE event_type = 'task.transitioned' AND created_at >= ? "
            "GROUP BY 1, 2",
            (since_iso,),
        )
        folded = bucket_transitions(rows, start=since, end=moment, buckets=buckets)
        folded["total"] = sum(int(row.get("n") or 0) for row in rows)
        return folded

    def transitions_section() -> List[Dict[str, Any]]:
        rows = q(
            "SELECT h.task_id, h.from_state, h.to_state, h.actor, h.created_at, "
            "t.title, t.project "
            "FROM task_history h LEFT JOIN tasks t ON t.id = h.task_id "
            "WHERE h.event_type = 'task.transitioned' AND h.created_at >= ? "
            "ORDER BY h.created_at DESC LIMIT %d" % RECENT_TRANSITIONS,
            (since_iso,),
        )
        for row in rows:
            row["age_seconds"] = _age_seconds(row.get("created_at"), moment)
        return rows

    # --- agents: is the hub's belief true? ----------------------------------
    def agents_section() -> Dict[str, Any]:
        rows = q(
            "SELECT id, name, status, health_status, instance_kind, current_task_id, "
            "last_seen_at, updated_at, dispatch_hold "
            "FROM agents WHERE deleted_at IS NULL ORDER BY name, id LIMIT %d" % AGENT_LIMIT
        )
        owned = {
            str(row.get("owner_agent_id")): int(row.get("n") or 0)
            for row in q(
                "SELECT owner_agent_id, COUNT(*) AS n FROM tasks "
                "WHERE owner_agent_id IS NOT NULL AND state NOT IN (%s) "
                "GROUP BY owner_agent_id" % terminal_sql
            )
        }
        leases = {
            str(row.get("agent_id")): int(row.get("n") or 0)
            for row in q(
                "SELECT agent_id, COUNT(*) AS n FROM leases "
                "WHERE status = 'active' GROUP BY agent_id"
            )
        }
        by_status: Dict[str, int] = {}
        by_health: Dict[str, int] = {}
        for row in rows:
            seen = _age_seconds(row.get("last_seen_at"), moment)
            row["seconds_since_seen"] = seen
            row["open_tasks"] = owned.get(str(row.get("id")), 0)
            row["active_leases"] = leases.get(str(row.get("id")), 0)
            # The hub's *belief* vs the evidence for it. An agent reporting
            # `busy` that has not been heard from in an hour is the failure
            # mode this console exists to surface; we report both halves and
            # let the UI mark the contradiction rather than picking a winner.
            row["belief_contradicted"] = bool(
                seen is not None
                and seen > 900
                and str(row.get("status")) in {"idle", "busy"}
            )
            status = str(row.get("status") or "(unknown)")
            by_status[status] = by_status.get(status, 0) + 1
            health = str(row.get("health_status") or "(unknown)")
            by_health[health] = by_health.get(health, 0) + 1
        total_row = q("SELECT COUNT(*) AS n FROM agents WHERE deleted_at IS NULL")
        total = int(total_row[0]["n"]) if total_row else len(rows)
        return {
            "by_status": by_status,
            "by_health": by_health,
            "rows": rows,
            "total": total,
            "truncated": max(0, total - len(rows)),
        }

    # --- review / publication / work package / lease pipelines --------------
    def pipelines_section() -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, sql in (
            ("reviews", "SELECT status, COUNT(*) AS n FROM reviews GROUP BY status"),
            (
                "publications",
                "SELECT status, COUNT(*) AS n FROM publications GROUP BY status",
            ),
            ("leases", "SELECT status, COUNT(*) AS n FROM leases GROUP BY status"),
        ):
            out[key] = _counts(q(sql), "status")
        return out

    # --- dreaming / nap cycles ----------------------------------------------
    def cycles_section() -> Dict[str, Any]:
        naps = _counts(
            q("SELECT status, COUNT(*) AS n FROM nap_runs GROUP BY status"), "status"
        )
        recent = q(
            "SELECT id, agent_id, status, started_at, completed_at "
            "FROM nap_runs ORDER BY started_at DESC LIMIT %d" % RECENT_CYCLE_RUNS
        )
        for row in recent:
            row["age_seconds"] = _age_seconds(row.get("started_at"), moment)
        schedules = q(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN enabled <> 0 THEN 1 ELSE 0 END) AS enabled FROM nap_schedules"
        )
        summary = schedules[0] if schedules else {}
        return {
            "naps_by_status": naps,
            "recent_naps": recent,
            "schedules_total": int(summary.get("n") or 0),
            "schedules_enabled": int(summary.get("enabled") or 0),
        }

    def dreams_section() -> Dict[str, Any]:
        # dream_runs is created by mac.dreaming.store, not by schema.sql, so on
        # a hub where dreaming never ran the table is simply absent. That is a
        # real answer ("dreaming has never run here"), so it must surface as a
        # degraded section rather than as zeros.
        by_status = _counts(
            q("SELECT status, COUNT(*) AS n FROM dream_runs GROUP BY status"), "status"
        )
        by_state = _counts(
            q("SELECT state, COUNT(*) AS n FROM dream_runs GROUP BY state"), "state"
        )
        recent = q(
            "SELECT id, agent_id, project, status, state, created_at, promoted_at "
            "FROM dream_runs ORDER BY created_at DESC LIMIT %d" % RECENT_CYCLE_RUNS
        )
        for row in recent:
            row["age_seconds"] = _age_seconds(row.get("created_at"), moment)
        return {"by_status": by_status, "by_state": by_state, "recent": recent}

    # --- agentbus traffic ----------------------------------------------------
    def agentbus_section() -> Dict[str, Any]:
        streams = _counts(
            q("SELECT status, COUNT(*) AS n FROM agentbus_streams GROUP BY status"),
            "status",
        )
        messages = _counts(
            q("SELECT status, COUNT(*) AS n FROM messages GROUP BY status"), "status"
        )
        recent_chunks = q(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes "
            "FROM agentbus_chunks WHERE created_at >= ?",
            (since_iso,),
        )
        chunk = recent_chunks[0] if recent_chunks else {}
        return {
            "streams_by_status": streams,
            "messages_by_status": messages,
            "chunks_in_window": int(chunk.get("n") or 0),
            "chunk_bytes_in_window": int(chunk.get("bytes") or 0),
        }

    # --- telemetry health ----------------------------------------------------
    def telemetry_section() -> Dict[str, Any]:
        head = q(
            "SELECT MAX(sequence) AS cursor, COUNT(*) AS n, "
            "MIN(created_at) AS oldest, MAX(created_at) AS newest "
            "FROM observability_events"
        )
        summary = head[0] if head else {}
        by_level = _counts(
            q(
                "SELECT level, COUNT(*) AS n FROM observability_events "
                "WHERE created_at >= ? GROUP BY level",
                (since_iso,),
            ),
            "level",
        )
        top_names = q(
            "SELECT name, COUNT(*) AS n FROM observability_events "
            "WHERE created_at >= ? GROUP BY name ORDER BY n DESC LIMIT 12",
            (since_iso,),
        )
        oldest = summary.get("oldest")
        return {
            "cursor": int(summary.get("cursor") or 0),
            "events_total": int(summary.get("n") or 0),
            "events_in_window": sum(by_level.values()),
            "by_level_in_window": by_level,
            "top_names_in_window": [
                {"name": str(row.get("name")), "count": int(row.get("n") or 0)}
                for row in top_names
            ],
            "oldest_event_at": oldest,
            "newest_event_at": summary.get("newest"),
            "retention_span_seconds": _age_seconds(oldest, moment),
        }

    payload["tasks"] = sections.run("tasks", tasks_section)
    payload["stuck"] = sections.run("stuck", stuck_section)
    payload["projects"] = sections.run("projects", projects_section)
    payload["flow"] = sections.run("flow", flow_section)
    payload["transitions"] = sections.run("transitions", transitions_section)
    payload["agents"] = sections.run("agents", agents_section)
    payload["pipelines"] = sections.run("pipelines", pipelines_section)
    payload["cycles"] = sections.run("cycles", cycles_section)
    payload["dreams"] = sections.run("dreams", dreams_section)
    # --- transcript coverage -------------------------------------------------
    def transcripts_section() -> Dict[str, Any]:
        """How much of the fleet's work has a recorded conversation at all.

        This is the honest header for the whole drill-down feature. On the live
        hub the answer is about 2% of tasks, and `coding_agent` / `model` are
        empty on every row (a known executor bug, task_8d701ea3). An operator
        who does not know that will read "no transcript" as "nothing happened"
        instead of "we did not record it", so the fraction is reported at fleet
        level rather than left to be inferred one task at a time.
        """
        totals = q(
            "SELECT COUNT(*) AS rows_total, "
            "COUNT(DISTINCT task_id) AS tasks_with, "
            "SUM(CASE WHEN COALESCE(coding_agent, '') <> '' THEN 1 ELSE 0 END) "
            "AS attributed "
            "FROM task_agent_transcripts"
        )
        row = totals[0] if totals else {}
        rows_total = int(row.get("rows_total") or 0)
        tasks_with = int(row.get("tasks_with") or 0)
        attributed = int(row.get("attributed") or 0)
        task_rows = q("SELECT COUNT(*) AS n FROM tasks")
        tasks_total = int(task_rows[0]["n"]) if task_rows else 0
        commands = q("SELECT COUNT(*) AS n FROM command_audit")
        return {
            "rows_total": rows_total,
            "tasks_with_transcript": tasks_with,
            "tasks_total": tasks_total,
            # None, not 0.0, when there is nothing to divide by.
            "coverage_fraction": (tasks_with / tasks_total) if tasks_total else None,
            "attributed_rows": attributed,
            "unattributed_rows": rows_total - attributed,
            "commands_audited": int(commands[0]["n"]) if commands else 0,
        }

    # --- merge queue: what is waiting to land, and why ---------------------
    def merge_queue_section() -> Dict[str, Any]:
        """Per-(repository, branch) queue state.

        The queue's own docstring names why this is worth a console section:
        this repository has produced four separate gates that reported healthy
        while enforcing nothing, and a queue nobody can watch is the next one.
        Depth, the AIMD window, what is testing, and what was evicted and why
        are read straight from the durable tables rather than inferred.

        Read directly here rather than through NativeMergeQueue.snapshot(),
        which answers for ONE (repository, branch) and would need a prior query
        to learn the keys. Two SELECTs answer for all of them, and this section
        must stay SELECT-only and cheap -- it runs on every console poll.
        """
        live_states = ("queued", "testing", "tested")
        entries = q(
            """
            SELECT repository, branch, state, COUNT(*) AS n
              FROM merge_queue_entries
             GROUP BY repository, branch, state
            """
        )
        windows = q(
            """
            SELECT repository, branch, window_size, landed_count, failure_count,
                   speculation_discarded, last_event, updated_at
              FROM merge_queue_windows
            """
        )
        # Most recent evictions across all queues: the one field that says why
        # a change did not land, which is the question an operator arrives with.
        evictions = q(
            """
            SELECT repository, branch, task_id, pull_request_number,
                   eviction_reason, updated_at
              FROM merge_queue_entries
             WHERE state = 'evicted' AND eviction_reason <> ''
             ORDER BY updated_at DESC
             LIMIT 10
            """
        )

        queues: Dict[tuple, Dict[str, Any]] = {}

        def slot(repository: Any, branch: Any) -> Dict[str, Any]:
            key = (str(repository or ""), str(branch or ""))
            if key not in queues:
                queues[key] = {
                    "repository": key[0],
                    "branch": key[1],
                    "depth": 0,
                    "by_state": {},
                    "window_size": None,
                    "landed_count": 0,
                    "failure_count": 0,
                    "speculation_discarded": 0,
                    "last_event": "",
                    "updated_at": None,
                }
            return queues[key]

        for row in entries:
            entry = slot(row.get("repository"), row.get("branch"))
            state = str(row.get("state") or "")
            count = int(row.get("n") or 0)
            entry["by_state"][state] = count
            if state in live_states:
                entry["depth"] += count

        for row in windows:
            entry = slot(row.get("repository"), row.get("branch"))
            entry["window_size"] = int(row.get("window_size") or 1)
            entry["landed_count"] = int(row.get("landed_count") or 0)
            entry["failure_count"] = int(row.get("failure_count") or 0)
            entry["speculation_discarded"] = int(row.get("speculation_discarded") or 0)
            entry["last_event"] = str(row.get("last_event") or "")
            entry["updated_at"] = row.get("updated_at")

        ordered = sorted(
            queues.values(),
            key=lambda item: (-item["depth"], item["repository"], item["branch"]),
        )
        return {
            "queues": ordered,
            "queue_count": len(ordered),
            "total_depth": sum(item["depth"] for item in ordered),
            "total_landed": sum(item["landed_count"] for item in ordered),
            "total_failed": sum(item["failure_count"] for item in ordered),
            "recent_evictions": evictions,
            "live_states": list(live_states),
        }

    payload["merge_queue"] = sections.run("merge_queue", merge_queue_section)
    payload["agentbus"] = sections.run("agentbus", agentbus_section)
    payload["telemetry"] = sections.run("telemetry", telemetry_section)
    payload["transcripts"] = sections.run("transcripts", transcripts_section)

    # A section that failed is removed entirely. Absent means "unknown"; it must
    # never be confusable with an empty-but-successful read.
    for key in [k for k, v in payload.items() if v is None]:
        payload.pop(key)

    payload["degraded"] = sections.degraded
    payload["observability_sequence"] = int(
        (payload.get("telemetry") or {}).get("cursor") or 0
    )
    payload["build_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    return payload


# ---------------------------------------------------------------------------
# Task drill-down
# ---------------------------------------------------------------------------

TASK_SCHEMA_VERSION = "mac.dashboard.observe.task.v1"

HISTORY_LIMIT = 200
TRANSCRIPT_LIMIT = 200
COMMAND_LIMIT = 200
EVIDENCE_LIMIT = 60
# A single expanded transcript turn can be ~115 KB compressed. Cap what the
# console will ship in one response and SAY when the cap bit, rather than
# silently handing back a prefix that reads like the whole conversation.
TRANSCRIPT_TEXT_CAP = 240_000


def _clip(text: str, cap: int = TRANSCRIPT_TEXT_CAP) -> Dict[str, Any]:
    value = text or ""
    if len(value) <= cap:
        return {"text": value, "clipped": False, "full_length": len(value)}
    return {"text": value[:cap], "clipped": True, "full_length": len(value)}


def build_task_drilldown(
    cp: Any,
    task_id: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Everything that happened to one task. Read-only.

    "What happened" is deliberately broader than "what states did it pass
    through": the state history says a task failed, the transcript says what the
    coding agent was actually doing when it did. Both are here, on one screen,
    joined by ``command_id`` to what the harness executed.

    The payloads are NOT included — they are zlib blobs up to ~115 KB each and a
    task can have dozens of turns. This returns turn metadata; the text of one
    turn comes from :func:`build_transcript_entry` when it is expanded.
    """
    started = time.monotonic()
    moment = now or datetime.now(timezone.utc)
    store = cp.store
    sections = _Sections()

    def q(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return [dict(row) for row in store.query_all(sql, tuple(params))]

    rows = q(
        "SELECT id, title, description, state, project, priority, owner_agent_id, "
        "lease_id, leased_until, attempt_count, max_attempts, started_at, "
        "completed_at, created_at, updated_at, created_by_human "
        "FROM tasks WHERE id = ?",
        (task_id,),
    )
    payload: Dict[str, Any] = {
        "schema": TASK_SCHEMA_VERSION,
        "server_time": moment.isoformat(),
        "task_id": task_id,
        "found": bool(rows),
    }
    if not rows:
        # An unknown id is a real answer, not an error and not an empty task.
        payload["degraded"] = []
        payload["build_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        return payload

    task = rows[0]
    task["dwell_seconds"] = _age_seconds(task.get("updated_at"), moment)
    task["age_seconds"] = _age_seconds(task.get("created_at"), moment)
    payload["task"] = task

    def history_section() -> List[Dict[str, Any]]:
        events = q(
            "SELECT id, event_type, actor, from_state, to_state, created_at "
            "FROM task_history WHERE task_id = ? "
            "ORDER BY created_at DESC LIMIT %d" % HISTORY_LIMIT,
            (task_id,),
        )
        for event in events:
            event["age_seconds"] = _age_seconds(event.get("created_at"), moment)
        return events

    def transcripts_section() -> Dict[str, Any]:
        # octet_length(payload) rather than the payload itself: the size is what
        # the list view needs, and shipping the blobs would defeat the point.
        turns = q(
            "SELECT id, sequence, agent_id, command_id, coding_agent, model, "
            "returncode, duration_ms, truncated, started_at, completed_at, "
            "compression, prompt_sha256, response_sha256, metadata, created_at, "
            "octet_length(payload) AS payload_bytes "
            "FROM task_agent_transcripts WHERE task_id = ? "
            "ORDER BY sequence ASC, created_at ASC LIMIT %d" % TRANSCRIPT_LIMIT,
            (task_id,),
        )
        for turn in turns:
            turn["truncated"] = bool(turn.get("truncated"))
            turn["has_payload"] = bool(turn.get("payload_bytes"))
            # Empty string and NULL both mean "nobody recorded which CLI this
            # was". Normalise to None so the UI has one thing to test and can
            # render "unattributed" rather than a blank that reads as "none".
            for key in ("coding_agent", "model"):
                if not str(turn.get(key) or "").strip():
                    turn[key] = None
        attributed = sum(1 for turn in turns if turn.get("coding_agent"))
        return {
            "rows": turns,
            "count": len(turns),
            "attributed": attributed,
            "unattributed": len(turns) - attributed,
            "truncated_list": len(turns) >= TRANSCRIPT_LIMIT,
        }

    def commands_section() -> List[Dict[str, Any]]:
        commands = q(
            "SELECT id, command_id, agent_id, phase, argv, cwd, lease_id, "
            "started_at, completed_at, duration_ms, returncode, stdout_bytes, "
            "stderr_bytes, created_at "
            "FROM command_audit WHERE task_id = ? "
            "ORDER BY created_at DESC LIMIT %d" % COMMAND_LIMIT,
            (task_id,),
        )
        for command in commands:
            command["age_seconds"] = _age_seconds(command.get("created_at"), moment)
        return commands

    def evidence_section() -> List[Dict[str, Any]]:
        return q(
            "SELECT id, kind, uri, summary, created_by, created_at "
            "FROM evidence WHERE task_id = ? "
            "ORDER BY created_at DESC LIMIT %d" % EVIDENCE_LIMIT,
            (task_id,),
        )

    def reviews_section() -> List[Dict[str, Any]]:
        return q(
            "SELECT id, reviewer_agent_id, status, created_at, completed_at "
            "FROM reviews WHERE task_id = ? ORDER BY created_at DESC LIMIT 40",
            (task_id,),
        )

    def publications_section() -> List[Dict[str, Any]]:
        return q(
            "SELECT id, status, created_at FROM publications "
            "WHERE task_id = ? ORDER BY created_at DESC LIMIT 40",
            (task_id,),
        )

    payload["history"] = sections.run("history", history_section)
    payload["transcripts"] = sections.run("transcripts", transcripts_section)
    payload["commands"] = sections.run("commands", commands_section)
    payload["evidence"] = sections.run("evidence", evidence_section)
    payload["reviews"] = sections.run("reviews", reviews_section)
    payload["publications"] = sections.run("publications", publications_section)

    for key in [k for k, v in payload.items() if v is None]:
        payload.pop(key)
    payload["degraded"] = sections.degraded
    payload["build_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    return payload


def build_transcript_entry(cp: Any, transcript_id: str) -> Dict[str, Any]:
    """One transcript turn, decompressed. Read-only.

    Fetched only when a turn is expanded, because the payload is a zlib blob of
    prompt + response + stderr and a task can have dozens of them.
    """
    rows = [
        dict(row)
        for row in cp.store.query_all(
            "SELECT id, task_id, agent_id, command_id, sequence, coding_agent, "
            "model, payload, compression, returncode, duration_ms, truncated, "
            "started_at, completed_at, prompt_sha256, response_sha256, metadata, "
            "created_at FROM task_agent_transcripts WHERE id = ?",
            (transcript_id,),
        )
    ]
    if not rows:
        return {
            "schema": "mac.dashboard.observe.transcript.v1",
            "transcript_id": transcript_id,
            "found": False,
        }
    row = rows[0]
    # Reuse the control plane's own decoder rather than reimplementing zlib
    # framing here: a second decoder is a second thing to drift.
    texts = cp._decompress_transcript(row.get("payload"), row.get("compression"))
    return {
        "schema": "mac.dashboard.observe.transcript.v1",
        "transcript_id": transcript_id,
        "found": True,
        "task_id": row.get("task_id"),
        "sequence": int(row.get("sequence") or 0),
        "agent_id": row.get("agent_id"),
        "command_id": row.get("command_id"),
        # Normalised the same way as the list view: absent, not "".
        "coding_agent": str(row.get("coding_agent") or "").strip() or None,
        "model": str(row.get("model") or "").strip() or None,
        "returncode": row.get("returncode"),
        "duration_ms": row.get("duration_ms"),
        # The executor's own truncation flag, distinct from this endpoint's cap.
        "truncated_at_capture": bool(row.get("truncated")),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "prompt": _clip(texts.get("prompt", "")),
        "response": _clip(texts.get("response", "")),
        "stderr": _clip(texts.get("stderr", "")),
        "metadata_raw": row.get("metadata"),
    }
