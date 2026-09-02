"""Curated, presentation-safe fleet activity over observability sequence."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mac.models import ValidationError, ensure_json_object, json_loads, utcnow

SCHEMA = "mac.news.v1"
TASK_EVENTS = ("task.created", "task.claimed", "task.transitioned")
AGENT_EVENTS = (
    "agent.registered",
    "agent.reregistered",
    "agent.updated",
    "agent.heartbeat_updated",
)


def _summary(item: Dict[str, Any]) -> str:
    actor = item["actor"] or "unknown"
    if item["kind"] == "task":
        task = item["task_title"] or item["task_id"]
        if item["event_type"] == "task.created":
            return "%s created %s" % (actor, task)
        return "%s moved %s from %s to %s" % (
            actor,
            task,
            item["from_state"] or "unknown",
            item["to_state"] or "unknown",
        ) + (
            " (%s%s)"
            % (
                item["failure_class"],
                ", attempt refunded" if item["attempt_refunded"] else "",
            )
            if item["failure_class"]
            else ""
        )
    agent = item["agent_name"] or item["agent_id"]
    if item["event_type"] in {"agent.registered", "agent.reregistered"}:
        verb = "re-registered" if item["event_type"] == "agent.reregistered" else "registered"
        return "%s %s %s" % (actor, verb, agent)
    changes = item["changed_fields"]
    if "status" in changes and item["previous_status"] != item["status"]:
        return "%s changed %s from %s to %s" % (
            actor,
            agent,
            item["previous_status"] or "unknown",
            item["status"] or "unknown",
        )
    return "%s updated %s (%s)" % (actor, agent, ", ".join(changes) or "lifecycle")


def build_news_feed(
    control_plane: Any,
    *,
    after_sequence: Optional[int] = None,
    project: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    """Return significant task/agent facts without forwarding raw event detail."""

    try:
        cap = min(max(1, int(limit)), 500)
        cursor = None if after_sequence is None else max(0, int(after_sequence))
    except (TypeError, ValueError) as exc:
        raise ValidationError("news cursor and limit must be integers") from exc
    event_names = TASK_EVENTS + AGENT_EVENTS
    placeholders = ", ".join("?" for _ in event_names)
    clauses = ["o.kind = 'log'", "o.name IN (%s)" % placeholders]
    params: List[Any] = list(event_names)
    if cursor is not None:
        clauses.append("o.sequence > ?")
        params.append(cursor)
    if project:
        clauses.append("o.subject_type = 'task' AND t.project = ?")
        params.append(project)
    direction = "ASC" if cursor is not None else "DESC"
    params.append(cap)
    rows = control_plane.store.query_all(
        """
        SELECT o.sequence, o.name, o.subject_type, o.subject_id, o.source,
               o.detail, o.created_at,
               t.title AS task_title, t.project AS task_project,
               a.name AS agent_name
        FROM observability_events o
        LEFT JOIN tasks t ON o.subject_type = 'task' AND t.id = o.subject_id
        LEFT JOIN agents a ON o.subject_type = 'agent' AND a.id = o.subject_id
        WHERE %s
        ORDER BY o.sequence %s
        LIMIT ?
        """
        % (" AND ".join(clauses), direction),
        tuple(params),
    )
    items: List[Dict[str, Any]] = []
    for row in rows:
        detail = ensure_json_object(json_loads(row["detail"], {}))
        kind = str(row["subject_type"])
        changed = detail.get("changed_fields")
        item: Dict[str, Any] = {
            "sequence": int(row["sequence"]),
            "created_at": row["created_at"],
            "kind": kind,
            "event_type": row["name"],
            "actor": str(detail.get("actor") or row["source"] or ""),
            "task_id": row["subject_id"] if kind == "task" else None,
            "task_title": row["task_title"] if kind == "task" else None,
            "project": row["task_project"] if kind == "task" else None,
            "from_state": detail.get("from_state") if kind == "task" else None,
            "to_state": detail.get("to_state") if kind == "task" else None,
            "failure_class": (
                detail.get("review_failure_class") or detail.get("failure_class")
                if kind == "task"
                else None
            ),
            "attempt_refunded": bool(detail.get("attempt_refunded")) if kind == "task" else False,
            "agent_id": row["subject_id"] if kind == "agent" else None,
            "agent_name": (
                (detail.get("agent_name") or row["agent_name"]) if kind == "agent" else None
            ),
            "previous_status": detail.get("previous_status") if kind == "agent" else None,
            "status": detail.get("status") if kind == "agent" else None,
            "previous_health_status": (
                detail.get("previous_health_status") if kind == "agent" else None
            ),
            "health_status": detail.get("health_status") if kind == "agent" else None,
            "changed_fields": (
                [str(value) for value in changed[:20]] if isinstance(changed, list) else []
            ),
        }
        item["summary"] = _summary(item)
        items.append(item)
    cursor = max((item["sequence"] for item in items), default=cursor or 0)
    return {
        "schema": SCHEMA,
        "server_time": utcnow(),
        "cursor": cursor,
        "items": items,
    }
