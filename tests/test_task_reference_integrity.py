"""Every stored reference to a task must resolve to a task.

Written before the bigint-key migration, deliberately. Converting task ids
from TEXT to BIGINT touches 57 columns across the schema, and only 31 of them
have a foreign key -- the other 26 hold task ids with no referential integrity
at all, and would silently keep stale text values through any FK-driven
migration. Worse, task ids also live *inside* JSON: the `dependencies` array,
and `replacement_task_id` / `root_task_id` / `repair_task_id` /
`parent_task_id` in metadata. No column type, foreign key, or type checker
sees those.

So this is the invariant the migration must preserve, expressed once and
checked everywhere: a task reference either resolves or does not exist. It is
worth having regardless of the migration -- it catches referential rot in the
26 unguarded columns today -- and it is what makes the migration verifiable
rather than hopeful.
"""

from __future__ import annotations

import json
from typing import Dict, List, Set, Tuple

import pytest

from mac.services import ControlPlane
from mac.test_support import ephemeral_store

#: Columns whose value is a single task id, discovered by naming convention.
#: Convention rather than an explicit list so a new column is covered the day
#: it is added, which is precisely when it would otherwise be missed.
_TASK_ID_SUFFIXES = ("task_id",)

#: Columns holding a JSON array of task ids.
_TASK_ID_LIST_COLUMNS = {
    ("tasks", "dependencies"),
    ("task_resource_contentions", "peer_task_ids"),
}

#: Metadata paths inside `tasks.metadata` that name another task.
_METADATA_TASK_PATHS = (
    ("repository_ref_lifecycle", "replacement_task_id"),
    ("replacement_task_id",),
    ("root_task_id",),
    ("repair_task_id",),
    ("parent_task_id",),
)


def _task_id_columns(store) -> List[Tuple[str, str]]:
    rows = store.query_all(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND (column_name LIKE '%task_id' OR column_name LIKE '%task_ids')
        ORDER BY table_name, column_name
        """
    )
    return [(row["table_name"], row["column_name"]) for row in rows]


def _live_task_ids(store) -> Set[str]:
    return {str(row["id"]) for row in store.query_all("SELECT id FROM tasks")}


def _dangling(store) -> Dict[str, List[str]]:
    """Every stored task reference that does not resolve, by location."""
    live = _live_task_ids(store)
    problems: Dict[str, List[str]] = {}

    for table, column in _task_id_columns(store):
        if (table, column) in _TASK_ID_LIST_COLUMNS:
            continue
        rows = store.query_all(
            'SELECT DISTINCT "%s" AS value FROM "%s" WHERE "%s" IS NOT NULL'
            % (column, table, column)
        )
        missing = [str(row["value"]) for row in rows if str(row["value"]) not in live]
        if missing:
            problems["%s.%s" % (table, column)] = sorted(missing)[:10]

    for table, column in sorted(_TASK_ID_LIST_COLUMNS):
        rows = store.query_all(
            'SELECT "%s" AS value FROM "%s" WHERE "%s" IS NOT NULL'
            % (column, table, column)
        )
        missing = []
        for row in rows:
            try:
                ids = json.loads(row["value"]) if row["value"] else []
            except (TypeError, ValueError):
                continue
            missing.extend(str(i) for i in ids if str(i) not in live)
        if missing:
            problems["%s.%s[]" % (table, column)] = sorted(set(missing))[:10]

    for row in store.query_all("SELECT id, metadata FROM tasks"):
        try:
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        except (TypeError, ValueError):
            continue
        for path in _METADATA_TASK_PATHS:
            cursor = metadata
            for part in path:
                cursor = cursor.get(part) if isinstance(cursor, dict) else None
            if cursor and str(cursor) not in live:
                key = "tasks.metadata.%s" % ".".join(path)
                problems.setdefault(key, []).append(str(cursor))

    return problems


def test_every_task_reference_resolves_on_a_working_ledger():
    """The invariant, exercised over a ledger that used the real code paths."""
    store = ephemeral_store()
    cp = ControlPlane(store, secret_key="k" * 40)

    machine = cp.register_machine("host", resources={"cpu": 4, "memory_gb": 8})
    agent = cp.register_agent(machine.id, "worker", capabilities=["python"])

    parent = cp.create_task("parent", project="mac", required_capabilities=["python"])
    child = cp.create_task("child", project="mac", dependencies=[parent.id])
    parked = cp.create_task("parked", project="mac")
    cp.request_task_input(parked.id, [{"question": "which database?"}], "worker-1")
    cp.answer_task_input(
        parked.id, "postgres", "jordan", disposition=cp.ANSWER_RESUME
    )

    lease = cp.claim_task(parent.id, agent.id)
    assert lease is not None
    cp.add_evidence(
        parent.id,
        kind="log",
        uri="file:///tmp/log",
        summary="ran",
        created_by=agent.id,
    )

    replacement = cp.create_task("replacement", project="mac")
    cp.close_task(
        child.id,
        "cancelled",
        "ops",
        {
            "reason": "superseded",
            "disposition": "superseded",
            "replacement_task_id": replacement.id,
        },
    )

    assert _dangling(store) == {}


def test_the_check_actually_detects_a_dangling_reference():
    """A guard that cannot fail is not a guard.

    The 26 task-id columns without a foreign key are exactly where a stale id
    can survive, so the detector is proven against one before it is trusted.
    """
    store = ephemeral_store()
    cp = ControlPlane(store, secret_key="k" * 40)
    task = cp.create_task("real", project="mac")

    # command_audit.task_id has no foreign key, so nothing stops this.
    store.execute(
        "INSERT INTO command_audit "
        "(id, command_id, agent_id, phase, argv, cwd, task_id, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "audit_1",
            "cmd_1",
            "agent_1",
            "completed",
            '["mac","task","show"]',
            "/tmp",
            "task_does_not_exist",
            "{}",
            "2026-08-02T00:00:00+00:00",
        ),
    )

    problems = _dangling(store)

    assert "command_audit.task_id" in problems
    assert problems["command_audit.task_id"] == ["task_does_not_exist"]
    assert str(task.id) not in problems.get("command_audit.task_id", [])


def test_a_dangling_id_inside_json_metadata_is_detected():
    """The class no schema change can find.

    A replacement_task_id living in metadata is a task reference that no
    column type, foreign key, or type checker sees. Converting ids to bigint
    would leave these strings behind, pointing at nothing, and only a runtime
    lookup would ever notice.
    """
    store = ephemeral_store()
    cp = ControlPlane(store, secret_key="k" * 40)
    task = cp.create_task("real", project="mac")

    store.execute(
        "UPDATE tasks SET metadata = ? WHERE id = ?",
        (json.dumps({"replacement_task_id": "task_vanished"}), task.id),
    )

    problems = _dangling(store)

    assert "tasks.metadata.replacement_task_id" in problems
    assert problems["tasks.metadata.replacement_task_id"] == ["task_vanished"]


def test_the_columns_under_guard_are_enumerated_by_convention():
    """New task-id columns must be covered the day they are added.

    An explicit allowlist would drift; the check discovers columns from the
    catalog, so the only way to escape it is to name a column something other
    than *task_id.
    """
    store = ephemeral_store()
    columns = _task_id_columns(store)

    tables = {table for table, _ in columns}
    # Spot-check both halves: a column with a foreign key, and one without.
    assert ("task_history", "task_id") in columns
    assert ("command_audit", "task_id") in columns
    assert len(columns) >= 50, "expected the full task-id surface, found %d" % len(columns)
    assert "tasks" not in tables or ("tasks", "task_id") not in columns
