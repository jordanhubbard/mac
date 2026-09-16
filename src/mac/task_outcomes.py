"""Task result and acceptance records, without owning dispatch or lifecycle.

This module takes a store, never the ControlPlane. Acceptance annotates one
immutable executor evidence record and attempt; it cannot complete a task,
approve a review, or attest a deployment.
"""

from __future__ import annotations

from datetime import timedelta
import math
import shlex
from statistics import median
from typing import Any

from mac.models import (
    NotFoundError,
    ValidationError,
    json_loads,
    json_dumps,
    new_id,
    parse_time,
    utcnow,
)

ACCEPTANCE_EVENT = "task.acceptance_recorded"


def _object(value: Any) -> dict:
    parsed = json_loads(value, {}) if isinstance(value, str) else value
    return parsed if isinstance(parsed, dict) else {}


def _target(task: dict) -> str | None:
    return _object(_object(task.get("metadata")).get("review_target")).get("executor_evidence_id")


def task_outcome(store: Any, task_id: str) -> dict:
    row = store.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise NotFoundError("task not found: %s" % task_id)
    task = dict(row)
    target = _target(task)
    evidence = [
        dict(r)
        for r in store.query_all(
            "SELECT * FROM evidence WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT 501",
            (task_id,),
        )
    ]
    publications = [
        dict(r)
        for r in store.query_all(
            "SELECT * FROM publications WHERE task_id = ? ORDER BY created_at DESC LIMIT 40",
            (task_id,),
        )
    ]
    acceptance_rows = store.query_all(
        "SELECT actor, detail, created_at FROM task_history WHERE task_id = ? AND event_type = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id, ACCEPTANCE_EVENT),
    )
    acceptance = {
        "status": "unknown",
        "reason": "No acceptance recorded for the current evidence and attempt.",
    }
    if acceptance_rows:
        record = dict(acceptance_rows[0])
        detail = _object(record["detail"])
        if (
            target
            and detail.get("evidence_id") == target
            and detail.get("attempt") == task["attempt_count"]
        ):
            acceptance = {
                "status": "accepted" if detail.get("accepted") is True else "rejected",
                "reason": detail.get("reason"),
                "actor": record["actor"],
                "evidence_id": target,
                "recorded_at": record["created_at"],
            }
    tests = {"status": "unknown", "reason": "No test result recorded for the current evidence."}
    for item in evidence:
        manifest = _object(_object(item["metadata"]).get("verification"))
        if not target or (item["id"] != target and manifest.get("reviewed_evidence_id") != target):
            continue
        checks = manifest.get("tests")
        if not isinstance(checks, list) or not checks:
            continue
        results = [check.get("returncode") for check in checks if isinstance(check, dict)]
        if len(results) != len(checks) or any(type(value) is not int for value in results):
            continue
        tests = {
            "status": "reported_pass" if all(value == 0 for value in results) else "reported_fail",
            "reason": "Recorded test results; request acceptance is separate.",
            "evidence_id": item["id"],
            "recorded_by": item["created_by"],
        }
        break
    publication = next(
        (
            p
            for p in publications
            if p["status"] == "published"
            and task["state"] == "completed"
            and target
            and p["evidence_id"] == target
        ),
        None,
    )
    # Publication closes the code task's evidence write boundary. A later
    # rollout records the exact deployed result under its own dependent task's
    # lease. Read the newest explicit record across those authorized contexts.
    deployment_row = (
        store.query_one(
            "SELECT e.id, e.task_id FROM evidence e "
            "WHERE e.kind = 'deployment' "
            "AND json_extract(e.metadata, '$.executor_evidence_id') = ? "
            "AND (e.task_id = ? OR e.task_id IN "
            "(SELECT task_id FROM task_edges WHERE dependency_task_id = ?)) "
            "ORDER BY e.created_at DESC, e.id DESC LIMIT 1",
            (target, task_id, task_id),
        )
        if target
        else None
    )
    deployment = dict(deployment_row) if deployment_row is not None else None
    tid = shlex.quote(task_id)
    actions = [
        {"label": "Inspect current state", "command": f"mac task show {tid}", "requires": "read"}
    ]
    if deployment:
        actions.append(
            {
                "label": "Inspect recorded deployment evidence",
                "command": f"mac task show {shlex.quote(deployment['task_id'])}",
                "requires": "read",
            }
        )
    state = task["state"]
    if state == "needs_input":
        actions.append(
            {
                "label": "Answer the pending question",
                "command": f"mac task edit {tid}",
                "requires": "admin",
            }
        )
    if state not in {"completed", "cancelled", "failed", "stopped"}:
        actions.append(
            {
                "label": "Stop work",
                "command": f"mac task stop {tid} --reason 'Operator requested stop'",
                "requires": "admin",
            }
        )
    if state in {"open", "waiting", "blocked", "failed", "stopped"}:
        actions.append(
            {
                "label": "Diagnose before recovery",
                "command": f"mac task why-unclaimed {tid}",
                "requires": "read",
            }
        )
    if state in {"reviewing", "completed"} and target:
        actions.append(
            {
                "label": "Record acceptance after checking the result",
                "command": f"mac task accept {tid} --evidence {shlex.quote(target)} --reason-file -",
                "requires": "operator write",
            }
        )
    return {
        "schema": "mac.task_outcome.v1",
        "task_id": task_id,
        "state": state,
        "executor_evidence_id": target,
        "tests": tests,
        "acceptance": acceptance,
        "publication": {
            "status": "published" if publication else "unknown",
            "target": publication["target"] if publication else None,
            "content_hash": publication["content_hash"] if publication else None,
        },
        "deployment": {
            "status": "recorded" if deployment else "unknown",
            "evidence_id": deployment["id"] if deployment else None,
            "task_id": deployment["task_id"] if deployment else None,
        },
        "evidence_truncated": len(evidence) > 500,
        "actions": actions,
    }


def record_acceptance(
    store: Any, task_id: str, *, evidence_id: str, reason: str, actor: str, accepted: bool = True
) -> dict:
    if not reason.strip() or len(reason) > 8000:
        raise ValidationError("acceptance reason must contain 1..8000 characters")
    with store.transaction() as conn:
        # Serialize with lifecycle changes: a result cannot change between
        # validating its attempt/evidence and recording the operator's decision.
        locked = conn.execute("UPDATE tasks SET updated_at = updated_at WHERE id = ?", (task_id,))
        if locked.rowcount != 1:
            raise NotFoundError("task not found: %s" % task_id)
        task = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        if task["state"] not in {"reviewing", "completed"}:
            raise ValidationError("acceptance requires a task in review or completed")
        if not evidence_id or _target(task) != evidence_id:
            raise ValidationError(
                "acceptance must name the current executor evidence; reread task outcome"
            )
        evidence = conn.execute(
            "SELECT id FROM evidence WHERE id = ? AND task_id = ?", (evidence_id, task_id)
        ).fetchone()
        if evidence is None:
            raise ValidationError("executor evidence does not belong to this task")
        detail = {
            "schema": "mac.task_acceptance.v1",
            "evidence_id": evidence_id,
            "attempt": task["attempt_count"],
            "accepted": accepted,
            "reason": reason.strip(),
        }
        conn.execute(
            "INSERT INTO task_history (id, task_id, event_type, actor, from_state, to_state, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("history"),
                task_id,
                ACCEPTANCE_EVENT,
                actor,
                task["state"],
                task["state"],
                json_dumps(detail),
                utcnow(),
            ),
        )
    return task_outcome(store, task_id)


def outcome_cohort(
    store: Any,
    *,
    project: str | None = None,
    since_hours: float = 24,
    limit: int = 100,
    now: str | None = None,
) -> dict:
    """A bounded creation cohort, including unfinished work in the denominator."""
    if (
        type(since_hours) not in (int, float)
        or not math.isfinite(since_hours)
        or not 0 < since_hours <= 2160
        or type(limit) is not int
        or not 1 <= limit <= 500
    ):
        raise ValidationError("cohort requires 0 < since-hours <= 2160 and 1 <= limit <= 500")
    observed = now or utcnow()
    since = (parse_time(observed) - timedelta(hours=since_hours)).isoformat()
    params: list = [since, observed]
    where = "created_at >= ? AND created_at <= ?"
    if project is not None:
        where += " AND project = ?"
        params.append(project)
    rows = store.query_all(
        "SELECT id, state, created_at, completed_at, metadata FROM tasks WHERE "
        + where
        + " ORDER BY created_at, id LIMIT ?",
        tuple(params + [limit + 1]),
    )
    items = []
    for row in rows[:limit]:
        task = dict(row)
        outcome = task_outcome(store, task["id"])
        # Only actual recorded route cost is counted, never zero-filled task
        # completion rollups. Missing routes or unpriced routes remain visible.
        routes = store.query_all(
            "SELECT detail FROM observability_events WHERE subject_type = 'task' AND subject_id = ? "
            "AND name = 'llm.route' AND created_at <= ? ORDER BY created_at LIMIT 1001",
            (task["id"], observed),
        )
        costs = []
        for route in routes:
            detail = _object(route["detail"])
            cost = detail.get("cost_usd")
            if cost is None:
                cost = _object(detail.get("usage")).get("cost_usd")
            costs.append(cost)
        known = [float(v) for v in costs if type(v) in (int, float) and math.isfinite(v) and v >= 0]
        interventions = store.query_one(
            "SELECT COUNT(*) AS n FROM task_history WHERE task_id = ? AND "
            "(event_type = ? OR (event_type = 'task.transitioned' AND "
            "(json_extract(detail, '$.answer_disposition') IS NOT NULL OR "
            "(to_state = 'stopped' AND json_extract(detail, '$.abort_confirmed') IS NOT NULL))))",
            (task["id"], ACCEPTANCE_EVENT),
        )
        items.append(
            {
                "task_id": task["id"],
                "state": task["state"],
                "origin_type": _object(_object(task["metadata"]).get("origin")).get(
                    "type", "unknown"
                ),
                "accepted": outcome["acceptance"]["status"] == "accepted",
                "completed_seconds": max(
                    0,
                    (
                        parse_time(task["completed_at"]) - parse_time(task["created_at"])
                    ).total_seconds(),
                )
                if task["state"] == "completed" and task["completed_at"]
                else None,
                "known_cost_usd": sum(known) if known else None,
                "priced_route_count": len(known),
                "observed_route_count": len(routes),
                "cost_coverage": "partial"
                if len(known) != len(routes) or len(routes) >= 1001
                else "recorded_routes_only"
                if routes
                else "unknown",
                "recorded_operator_interventions": interventions["n"],
            }
        )
    completed = sum(item["state"] == "completed" for item in items)
    accepted = sum(item["accepted"] and item["state"] == "completed" for item in items)
    origins = []
    for origin in sorted({str(item["origin_type"]) for item in items}):
        group = [item for item in items if str(item["origin_type"]) == origin]
        durations = [
            item["completed_seconds"] for item in group if item["completed_seconds"] is not None
        ]
        costs = [item["known_cost_usd"] for item in group if item["known_cost_usd"] is not None]
        origins.append(
            {
                "origin_type": origin,
                "count": len(group),
                "completed_count": sum(item["state"] == "completed" for item in group),
                "accepted_completed_count": sum(
                    item["accepted"] and item["state"] == "completed" for item in group
                ),
                "median_completed_seconds": median(durations) if durations else None,
                "known_cost_usd": sum(costs) if costs else None,
                "tasks_without_priced_routes": len(group) - len(costs),
                "recorded_operator_interventions": sum(
                    item["recorded_operator_interventions"] for item in group
                ),
            }
        )
    return {
        "schema": "mac.outcome_cohort.v1",
        "project": project,
        "since": since,
        "observed_at": observed,
        "cohort_basis": "task creation time",
        "count": len(items),
        "truncated": len(rows) > limit,
        "summary_scope": "bounded_sample" if len(rows) > limit else "complete_creation_cohort",
        "completed_count": completed,
        "accepted_completed_count": accepted,
        "accepted_completion_fraction": accepted / len(items) if items else None,
        "measurement_limits": "Ledger events and observed model routes only; unrecorded manual work and uninstrumented costs are unknown.",
        "origins": origins,
        "tasks": items,
    }
