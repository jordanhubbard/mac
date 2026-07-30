from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mac.models import AmbiguousIdError, NotFoundError, ValidationError
from mac.services import ControlPlane
from mac.store import SQLiteStore
from mac.task_dependencies import MIGRATION_VERSION


def _plane() -> ControlPlane:
    return ControlPlane.in_memory()


def _reset_dependency_migration(database: Path, updates: list[tuple[str, str]]) -> None:
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "DELETE FROM task_dependency_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        )
        conn.execute("DELETE FROM task_edges")
        conn.execute("DELETE FROM task_dependency_quarantine")
        for task_id, dependencies_json in updates:
            conn.execute(
                "UPDATE tasks SET dependencies = ? WHERE id = ?",
                (dependencies_json, task_id),
            )
        conn.commit()
    finally:
        conn.close()


def test_create_resolves_prefix_and_persists_full_edge() -> None:
    control = _plane()
    dependency = control.create_task("dependency")
    task = control.create_task("consumer", dependencies=[dependency.id[:13]])

    assert task.dependencies == [dependency.id]
    edge = control.store.query_one(
        "SELECT dependency_task_id, edge_position FROM task_edges WHERE task_id = ?",
        (task.id,),
    )
    assert edge is not None
    assert edge["dependency_task_id"] == dependency.id
    assert edge["edge_position"] == 0


def test_create_rejects_missing_and_ambiguous_dependency_prefixes() -> None:
    control = _plane()
    first = "task_abcdef" + ("1" * 26)
    second = "task_abcdef" + ("2" * 26)
    control.create_task("first", _task_id=first)
    control.create_task("second", _task_id=second)

    with pytest.raises(AmbiguousIdError):
        control.create_task("ambiguous", dependencies=["task_abcdef"])
    with pytest.raises(NotFoundError):
        control.create_task("missing", dependencies=["task_deadbeef"])


def test_create_rejects_supplied_id_self_prefix() -> None:
    control = _plane()
    task_id = "task_123456" + ("7" * 26)

    with pytest.raises(ValidationError, match="cannot depend on itself"):
        control.create_task(
            "self",
            _task_id=task_id,
            dependencies=[task_id[:13]],
        )


def test_update_rejects_self_dependency_and_cycle_atomically() -> None:
    control = _plane()
    first = control.create_task("first")
    second = control.create_task("second", dependencies=[first.id])

    with pytest.raises(ValidationError, match="cannot depend on itself"):
        control.update_task(first.id, dependencies=[first.id[:13]])
    with pytest.raises(ValidationError, match="dependency cycle"):
        control.update_task(first.id, dependencies=[second.id])

    assert control.get_task(first.id).dependencies == []
    assert (
        control.store.query_one("SELECT 1 FROM task_edges WHERE task_id = ?", (first.id,)) is None
    )


def test_update_by_task_prefix_replaces_edge_authority() -> None:
    control = _plane()
    first = control.create_task("first")
    second = control.create_task("second")
    consumer = control.create_task("consumer", dependencies=[first.id])

    updated = control.update_task(consumer.id[:13], dependencies=[second.id[:13]])

    assert updated.dependencies == [second.id]
    edges = control.store.query_all(
        "SELECT dependency_task_id FROM task_edges WHERE task_id = ?",
        (consumer.id,),
    )
    assert [row["dependency_task_id"] for row in edges] == [second.id]


def test_migration_rewrites_unique_short_reference(tmp_path: Path) -> None:
    database = tmp_path / "mac.db"
    store = SQLiteStore(str(database))
    control = ControlPlane(store, secret_key="test-key-with-enough-entropy-32+chars")
    dependency = control.create_task("dependency")
    consumer = control.create_task("consumer")
    store.close()

    _reset_dependency_migration(database, [(consumer.id, json.dumps([dependency.id[:13]]))])
    migrated = SQLiteStore(str(database))
    try:
        row = migrated.query_one("SELECT dependencies FROM tasks WHERE id = ?", (consumer.id,))
        assert row is not None
        assert json.loads(row["dependencies"]) == [dependency.id]
        edge = migrated.query_one(
            "SELECT dependency_task_id FROM task_edges WHERE task_id = ?",
            (consumer.id,),
        )
        assert edge is not None
        assert edge["dependency_task_id"] == dependency.id
    finally:
        migrated.close()


def test_migration_quarantines_missing_dependency(tmp_path: Path) -> None:
    database = tmp_path / "mac.db"
    store = SQLiteStore(str(database))
    control = ControlPlane(store, secret_key="test-key-with-enough-entropy-32+chars")
    consumer = control.create_task("consumer")
    store.close()

    _reset_dependency_migration(database, [(consumer.id, json.dumps(["task_deadbeef"]))])
    migrated = SQLiteStore(str(database))
    try:
        finding = migrated.query_one(
            "SELECT reason FROM task_dependency_quarantine WHERE task_id = ?",
            (consumer.id,),
        )
        assert finding is not None
        assert finding["reason"] == "missing_dependency"
        row = migrated.query_one("SELECT metadata FROM tasks WHERE id = ?", (consumer.id,))
        assert row is not None
        metadata = json.loads(row["metadata"])
        assert metadata["no_dispatch"] is True
        assert metadata["dependency_quarantine"]["schema"] == "mac.task_dependency_quarantine.v1"
        receipt = migrated.query_one(
            "SELECT quarantine_count FROM task_dependency_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        )
        assert receipt is not None
        assert receipt["quarantine_count"] == 1
    finally:
        migrated.close()


def test_migration_deduplicates_repeated_invalid_dependency(tmp_path: Path) -> None:
    database = tmp_path / "mac.db"
    store = SQLiteStore(str(database))
    control = ControlPlane(store, secret_key="test-key-with-enough-entropy-32+chars")
    consumer = control.create_task("consumer")
    store.close()

    _reset_dependency_migration(
        database,
        [(consumer.id, json.dumps(["task_deadbeef", "task_deadbeef"]))],
    )
    migrated = SQLiteStore(str(database))
    try:
        findings = migrated.query_all(
            "SELECT reason FROM task_dependency_quarantine WHERE task_id = ?",
            (consumer.id,),
        )
        assert [row["reason"] for row in findings] == ["missing_dependency"]
    finally:
        migrated.close()


@pytest.mark.parametrize(
    ("prior_migration_owned", "hold_after_repair"),
    [(True, False), (False, True)],
)
def test_migration_preserves_prior_hold_provenance(
    tmp_path: Path,
    prior_migration_owned: bool,
    hold_after_repair: bool,
) -> None:
    database = tmp_path / "mac.db"
    store = SQLiteStore(str(database))
    control = ControlPlane(store, secret_key="test-key-with-enough-entropy-32+chars")
    dependency = control.create_task("dependency")
    consumer = control.create_task("consumer")
    store.close()

    prior_quarantine = {
        "schema": "mac.task_dependency_quarantine.v1",
        "issues": [
            {
                "raw_dependency_id": "task_deadbeef",
                "reason": "missing_dependency",
                "candidates": [],
            }
        ],
        "detected_at": "prior-migration",
        "applied_no_dispatch": prior_migration_owned,
    }
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "DELETE FROM task_dependency_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        )
        conn.execute("DELETE FROM task_edges")
        conn.execute("DELETE FROM task_dependency_quarantine")
        conn.execute(
            "UPDATE tasks SET dependencies = ?, metadata = ? WHERE id = ?",
            (
                json.dumps(["task_deadbeef"]),
                json.dumps(
                    {
                        "no_dispatch": True,
                        "dependency_quarantine": prior_quarantine,
                    }
                ),
                consumer.id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    migrated = SQLiteStore(str(database))
    try:
        repaired_control = ControlPlane(
            migrated,
            secret_key="test-key-with-enough-entropy-32+chars",
        )
        quarantined = repaired_control.get_task(consumer.id)
        assert (
            quarantined.metadata["dependency_quarantine"][
                "applied_no_dispatch"
            ]
            is prior_migration_owned
        )

        repaired = repaired_control.update_task(
            consumer.id,
            dependencies=[dependency.id],
        )
        assert "dependency_quarantine" not in repaired.metadata
        assert (repaired.metadata.get("no_dispatch") is True) is hold_after_repair
    finally:
        migrated.close()


def test_migration_normalizes_uppercase_hex_prefix(tmp_path: Path) -> None:
    database = tmp_path / "mac.db"
    store = SQLiteStore(str(database))
    control = ControlPlane(store, secret_key="test-key-with-enough-entropy-32+chars")
    dependency_id = "task_abcdef" + ("1" * 26)
    dependency = control.create_task(
        "dependency",
        _task_id=dependency_id,
    )
    consumer = control.create_task("consumer")
    store.close()

    uppercase_prefix = "task_" + dependency.id[5:13].upper()
    _reset_dependency_migration(
        database,
        [(consumer.id, json.dumps([uppercase_prefix]))],
    )
    migrated = SQLiteStore(str(database))
    try:
        row = migrated.query_one(
            "SELECT dependencies FROM tasks WHERE id = ?",
            (consumer.id,),
        )
        assert row is not None
        assert json.loads(row["dependencies"]) == [dependency.id]
        edge = migrated.query_one(
            "SELECT dependency_task_id FROM task_edges WHERE task_id = ?",
            (consumer.id,),
        )
        assert edge is not None
        assert edge["dependency_task_id"] == dependency.id
        assert (
            migrated.query_one(
                "SELECT 1 FROM task_dependency_quarantine WHERE task_id = ?",
                (consumer.id,),
            )
            is None
        )
    finally:
        migrated.close()


def test_child_external_dependency_prefix_is_canonicalized() -> None:
    control = _plane()
    dependency = control.create_task("dependency")
    parent = control.create_task("parent")

    result = control.add_child_tasks(
        parent.id,
        [{"title": "child", "dependencies": [dependency.id[:13]]}],
    )

    child = control.get_task(result["children"][0]["id"])
    assert child.dependencies == [dependency.id]
    edge = control.store.query_one(
        "SELECT dependency_task_id FROM task_edges WHERE task_id = ?",
        (child.id,),
    )
    assert edge["dependency_task_id"] == dependency.id


def test_task_prefix_lookup_treats_underscore_literally() -> None:
    control = _plane()
    synthetic = "taskXabcdef" + ("1" * 26)
    control.create_task("synthetic", _task_id=synthetic)

    with pytest.raises(NotFoundError):
        control.get_task("task_abcdef")


def test_migration_quarantines_all_cycle_members(tmp_path: Path) -> None:
    database = tmp_path / "mac.db"
    store = SQLiteStore(str(database))
    control = ControlPlane(store, secret_key="test-key-with-enough-entropy-32+chars")
    first = control.create_task("first")
    second = control.create_task("second")
    store.close()

    _reset_dependency_migration(
        database,
        [
            (first.id, json.dumps([second.id])),
            (second.id, json.dumps([first.id])),
        ],
    )
    migrated = SQLiteStore(str(database))
    try:
        findings = migrated.query_all(
            "SELECT task_id, reason FROM task_dependency_quarantine ORDER BY task_id"
        )
        assert [(row["task_id"], row["reason"]) for row in findings] == [
            (task_id, "dependency_cycle") for task_id in sorted((first.id, second.id))
        ]
        assert migrated.query_one("SELECT 1 FROM task_edges LIMIT 1") is None
    finally:
        migrated.close()


def test_postgres_schema_declares_dependency_authority() -> None:
    schema = (
        Path(__file__).resolve().parents[1] / "src" / "mac" / "data" / "postgres" / "schema.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS task_edges" in schema
    assert "CREATE TABLE IF NOT EXISTS task_dependency_quarantine" in schema
    assert "CREATE TABLE IF NOT EXISTS task_dependency_migrations" in schema
