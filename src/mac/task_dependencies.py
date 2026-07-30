"""Canonical task-dependency persistence and one-time legacy migration.

``task_edges`` is the dependency authority.  The historical
``tasks.dependencies`` JSON column remains a read/API projection, but every
supported writer stores only full task ids there and updates the normalized
edge rows in the same transaction.

The migration is deliberately fail-closed: a legacy dependency document that
cannot be resolved uniquely, references itself, or participates in a cycle is
held via ``metadata.no_dispatch`` and recorded in
``task_dependency_quarantine``.  Valid unique prefixes are rewritten to full
ids.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from mac.models import ValidationError


MIGRATION_VERSION = "canonical-task-edges-v2"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_list(raw: Any) -> list[Any] | None:
    if isinstance(raw, list):
        return list(raw)
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return None
    return list(parsed) if isinstance(parsed, list) else None


def replace_task_edges(
    conn: Any,
    *,
    task_id: str,
    dependency_ids: Sequence[str],
    updated_at: str,
) -> None:
    """Replace one task's normalized edge set inside the caller transaction."""

    conn.execute("DELETE FROM task_edges WHERE task_id = ?", (task_id,))
    for position, dependency_id in enumerate(dependency_ids):
        conn.execute(
            """
            INSERT INTO task_edges (
                task_id, dependency_task_id, edge_position, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (task_id, dependency_id, position, updated_at),
        )
    conn.execute("DELETE FROM task_dependency_quarantine WHERE task_id = ?", (task_id,))


def lock_dependency_nodes(conn: Any, *, task_id: str, dependency_ids: Iterable[str]) -> None:
    """Serialize concurrent graph rewrites on the same dependency neighborhood."""

    for node_id in sorted({task_id, *dependency_ids}):
        result = conn.execute("UPDATE tasks SET updated_at = updated_at WHERE id = ?", (node_id,))
        if result.rowcount != 1:
            raise ValidationError("task dependency node disappeared: %s" % node_id)


def dependency_cycle_path(
    conn: Any,
    *,
    task_id: str,
    dependency_ids: Iterable[str],
) -> list[str] | None:
    """Return a concrete cycle path if replacing ``task_id`` edges would cycle."""

    rows = conn.execute("SELECT task_id, dependency_task_id FROM task_edges").fetchall()
    graph: dict[str, list[str]] = {}
    for row in rows:
        source = str(row["task_id"])
        target = str(row["dependency_task_id"])
        graph.setdefault(source, []).append(target)
    graph[task_id] = list(dependency_ids)

    path: list[str] = [task_id]
    active: dict[str, int] = {task_id: 0}
    visited: set[str] = set()
    frames: list[tuple[str, int]] = [(task_id, 0)]
    while frames:
        node, child_index = frames[-1]
        children = graph.get(node, ())
        if child_index >= len(children):
            frames.pop()
            path.pop()
            active.pop(node, None)
            visited.add(node)
            continue
        child = children[child_index]
        frames[-1] = (node, child_index + 1)
        if child in active:
            return [*path[active[child] :], child]
        if child in visited:
            continue
        active[child] = len(path)
        path.append(child)
        frames.append((child, 0))
    return None


def _cycle_nodes(graph: Mapping[str, Sequence[str]]) -> set[str]:
    """Return every node that participates in at least one directed cycle."""

    nodes = set(graph)
    reverse: dict[str, list[str]] = {node: [] for node in nodes}
    for source, targets in graph.items():
        for target in targets:
            nodes.add(target)
            reverse.setdefault(target, []).append(source)
            reverse.setdefault(source, [])

    finish_order: list[str] = []
    visited: set[str] = set()
    for root in nodes:
        if root in visited:
            continue
        visited.add(root)
        frames: list[tuple[str, int]] = [(root, 0)]
        while frames:
            node, child_index = frames[-1]
            children = graph.get(node, ())
            if child_index >= len(children):
                frames.pop()
                finish_order.append(node)
                continue
            child = children[child_index]
            frames[-1] = (node, child_index + 1)
            if child not in visited:
                visited.add(child)
                frames.append((child, 0))

    cyclic: set[str] = set()
    assigned: set[str] = set()
    for root in reversed(finish_order):
        if root in assigned:
            continue
        component: list[str] = []
        stack = [root]
        assigned.add(root)
        while stack:
            node = stack.pop()
            component.append(node)
            for parent in reverse.get(node, ()):
                if parent not in assigned:
                    assigned.add(parent)
                    stack.append(parent)
        if len(component) > 1 or (
            len(component) == 1 and component[0] in graph.get(component[0], ())
        ):
            cyclic.update(component)
    return cyclic


def _quarantine_id(task_id: str, raw_dependency_id: str, reason: str) -> str:
    digest = hashlib.sha256(
        ("%s\0%s\0%s" % (task_id, raw_dependency_id, reason)).encode("utf-8")
    ).hexdigest()[:32]
    return "taskdepq_%s" % digest


def migrate_dependency_edges(store: Any) -> dict[str, int]:
    """Migrate legacy JSON dependency documents into canonical edge rows once."""

    backend = str(store.backend_identity().get("backend") or "")
    with store.transaction() as conn:
        # SQLite's BEGIN IMMEDIATE serializes writers.  PostgreSQL needs an
        # explicit table barrier because this migration rewrites the whole
        # graph; ordinary task writers do not participate in an advisory lock.
        if backend == "postgres":
            conn.execute(
                "LOCK TABLE tasks, task_edges, task_dependency_quarantine, "
                "task_dependency_migrations IN ACCESS EXCLUSIVE MODE"
            )
        existing = conn.execute(
            "SELECT version, migrated_count, quarantine_count "
            "FROM task_dependency_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        ).fetchone()
        if existing is not None:
            return {
                "migrated_count": int(existing["migrated_count"]),
                "quarantine_count": int(existing["quarantine_count"]),
            }

        # The snapshot, analysis, graph rewrite, and receipt are one serialized
        # transaction.  No task or edge written during initialization can be
        # lost or overwritten by a stale pre-transaction snapshot.
        rows = conn.execute(
            "SELECT id, dependencies, metadata FROM tasks ORDER BY created_at, id"
        ).fetchall()
        ids = {str(row["id"]) for row in rows}
        ordered_ids = sorted(ids)
        resolved: dict[str, list[str]] = {}
        issues: dict[str, list[dict[str, Any]]] = {}

        def add_issue(task_id: str, issue: dict[str, Any]) -> None:
            bucket = issues.setdefault(task_id, [])
            key = (
                str(issue.get("raw_dependency_id") or ""),
                str(issue.get("reason") or ""),
                tuple(str(item) for item in issue.get("candidates") or []),
            )
            if any(
                (
                    str(existing_issue.get("raw_dependency_id") or ""),
                    str(existing_issue.get("reason") or ""),
                    tuple(
                        str(item)
                        for item in existing_issue.get("candidates") or []
                    ),
                )
                == key
                for existing_issue in bucket
            ):
                return
            bucket.append(issue)

        for row in rows:
            task_id = str(row["id"])
            raw_dependencies = _json_list(row["dependencies"])
            if raw_dependencies is None:
                add_issue(
                    task_id,
                    {
                        "raw_dependency_id": str(row["dependencies"] or ""),
                        "reason": "invalid_dependency_document",
                        "candidates": [],
                    },
                )
                resolved[task_id] = []
                continue
            canonical: list[str] = []
            seen: set[str] = set()
            for raw_value in raw_dependencies:
                raw_dependency_id = str(raw_value or "").strip()
                if not raw_dependency_id:
                    add_issue(
                        task_id,
                        {
                            "raw_dependency_id": "",
                            "reason": "empty_dependency_id",
                            "candidates": [],
                        },
                    )
                    continue
                if raw_dependency_id in ids:
                    matches = [raw_dependency_id]
                else:
                    suffix = (
                        raw_dependency_id[5:]
                        if raw_dependency_id.startswith("task_")
                        else ""
                    )
                    safe_prefix = (
                        6 <= len(suffix) < 32
                        and all(char in "0123456789abcdef" for char in suffix.lower())
                    )
                    if not safe_prefix:
                        add_issue(
                            task_id,
                            {
                                "raw_dependency_id": raw_dependency_id,
                                "reason": "invalid_dependency_id",
                                "candidates": [],
                            },
                        )
                        continue
                    normalized_prefix = "task_" + suffix.lower()
                    matches = [
                        candidate
                        for candidate in ordered_ids
                        if candidate.startswith(normalized_prefix)
                    ]
                if not matches:
                    add_issue(
                        task_id,
                        {
                            "raw_dependency_id": raw_dependency_id,
                            "reason": "missing_dependency",
                            "candidates": [],
                        },
                    )
                    continue
                if len(matches) != 1:
                    add_issue(
                        task_id,
                        {
                            "raw_dependency_id": raw_dependency_id,
                            "reason": "ambiguous_dependency_prefix",
                            "candidates": matches[:20],
                            "candidate_count": len(matches),
                        },
                    )
                    continue
                dependency_id = matches[0]
                if dependency_id == task_id:
                    add_issue(
                        task_id,
                        {
                            "raw_dependency_id": raw_dependency_id,
                            "reason": "self_dependency",
                            "candidates": [dependency_id],
                        },
                    )
                    continue
                if dependency_id not in seen:
                    canonical.append(dependency_id)
                    seen.add(dependency_id)
            resolved[task_id] = canonical

        graph = {
            task_id: dependency_ids
            for task_id, dependency_ids in resolved.items()
            if task_id not in issues
        }
        cyclic = _cycle_nodes(graph)
        for task_id in sorted(cyclic):
            # Limit candidates to the cyclic neighborhood reachable directly
            # from this task instead of conflating disjoint cycles.
            local_cycle = sorted(
                {
                    task_id,
                    *(
                        dependency_id
                        for dependency_id in graph.get(task_id, ())
                        if dependency_id in cyclic
                    ),
                    *(
                        source
                        for source, dependency_ids in graph.items()
                        if source in cyclic and task_id in dependency_ids
                    ),
                }
            )
            add_issue(
                task_id,
                {
                    "raw_dependency_id": "",
                    "reason": "dependency_cycle",
                    "candidates": local_cycle[:20],
                    "candidate_count": len(local_cycle),
                },
            )

        detected_at = _now()
        migrated_count = 0
        quarantine_count = 0
        conn.execute("DELETE FROM task_edges")
        conn.execute("DELETE FROM task_dependency_quarantine")
        for row in rows:
            task_id = str(row["id"])
            task_issues = issues.get(task_id, [])
            if task_issues:
                metadata = _json_object(row["metadata"])
                had_hold = bool(metadata.get("no_dispatch"))
                prior_quarantine = _json_object(
                    metadata.get("dependency_quarantine")
                )
                prior_migration_owned_hold = bool(
                    prior_quarantine.get("schema")
                    == "mac.task_dependency_quarantine.v1"
                    and prior_quarantine.get("applied_no_dispatch") is True
                )
                metadata["no_dispatch"] = True
                metadata["dependency_quarantine"] = {
                    "schema": "mac.task_dependency_quarantine.v1",
                    "issues": task_issues,
                    "detected_at": detected_at,
                    "applied_no_dispatch": (
                        prior_migration_owned_hold or not had_hold
                    ),
                }
                conn.execute(
                    "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                    (
                        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                        detected_at,
                        task_id,
                    ),
                )
                for issue in task_issues:
                    raw_dependency_id = str(issue["raw_dependency_id"])
                    reason = str(issue["reason"])
                    conn.execute(
                        """
                        INSERT INTO task_dependency_quarantine (
                            id, task_id, raw_dependency_id, reason, candidates,
                            detected_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _quarantine_id(task_id, raw_dependency_id, reason),
                            task_id,
                            raw_dependency_id,
                            reason,
                            json.dumps(
                                issue.get("candidates") or [],
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            detected_at,
                        ),
                    )
                quarantine_count += 1
                continue
            dependency_ids = resolved[task_id]
            conn.execute(
                "UPDATE tasks SET dependencies = ? WHERE id = ?",
                (
                    json.dumps(dependency_ids, separators=(",", ":")),
                    task_id,
                ),
            )
            replace_task_edges(
                conn,
                task_id=task_id,
                dependency_ids=dependency_ids,
                updated_at=detected_at,
            )
            migrated_count += 1
        conn.execute(
            """
            INSERT INTO task_dependency_migrations (
                version, completed_at, migrated_count, quarantine_count
            ) VALUES (?, ?, ?, ?)
            """,
            (
                MIGRATION_VERSION,
                detected_at,
                migrated_count,
                quarantine_count,
            ),
        )
    return {
        "migrated_count": migrated_count,
        "quarantine_count": quarantine_count,
    }
