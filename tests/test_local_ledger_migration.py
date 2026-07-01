from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from mac.cli import _local_ledger_notice_payload, main
from mac.local_ledger_migration import (
    ARCHIVE_SCHEMA,
    PROVENANCE_SCHEMA,
    LocalLedgerMigrationResult,
    LocalLedgerRetirementResult,
    LocalLedgerMigrationError,
    _archive_database,
    _as_dict,
    _json_list,
    _json_object,
    _load_source_secret_key,
    _remote_provenance_index,
    _sanitize_metadata,
    _sha256,
    _verify_remote_task,
    _verify_archive_manifest,
    _write_json_atomic,
    default_archive_dir,
    inspect_local_ledger,
    local_ledger_notice,
    migrate_local_ledger,
    retire_inactive_local_ledger,
)
from mac.services import ControlPlane
from mac.store import SQLiteStore
import mac.local_ledger_migration as local_migration


SECRET = "local-ledger-migration-test-key-32-chars"


class FakeTarget:
    def __init__(self) -> None:
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.created = 0
        self.corrupt_details = False
        self.after_create = None

    def list_tasks(
        self, state: Optional[str] = None, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        del tenant_id
        values = list(self.tasks.values())
        return [task for task in values if state is None or task["state"] == state]

    def create_task(self, title: str, **kwargs: Any) -> Dict[str, Any]:
        self.created += 1
        task_id = "task_%032x" % self.created
        task = {
            "id": task_id,
            "title": title,
            "description": kwargs.get("description", ""),
            "project": kwargs.get("project"),
            "priority": kwargs.get("priority", 0),
            "state": "blocked" if kwargs.get("dependencies") else "open",
            "required_capabilities": kwargs.get("required_capabilities") or [],
            "dependencies": kwargs.get("dependencies") or [],
            "metadata": kwargs.get("metadata") or {},
            "max_attempts": kwargs.get("max_attempts", 3),
        }
        self.tasks[task_id] = task
        if self.after_create:
            self.after_create(self.created)
        return task

    def task_detail(self, task_id: str) -> Dict[str, Any]:
        task = json.loads(json.dumps(self.tasks[task_id]))
        if self.corrupt_details:
            task["priority"] += 1
        return {"task": task, "history": []}


def _source(path: Path, *, held: bool = False) -> tuple[str, str]:
    store = SQLiteStore(str(path))
    cp = ControlPlane(store, secret_key=SECRET)
    parent = cp.create_task(
        "parent",
        project="mac",
        priority=5,
        required_capabilities=["python"],
        metadata={
            "no_dispatch": held,
            "execution_contract": {"type": "repository", "repository_path": "/tmp/local"},
            "origin": {
                "type": "direct_task",
                "repository_id": "repo_local",
                "repository_path": "/tmp/local",
                "keep": "context",
            },
        },
    )
    child = cp.create_task(
        "child",
        description="depends on parent",
        project="mac",
        priority=4,
        dependencies=[parent.id],
        max_attempts=7,
    )
    store.close()
    return parent.id, child.id


def test_inspect_missing_empty_and_non_mac_database(tmp_path):
    missing = inspect_local_ledger(tmp_path / "missing.db")
    assert missing.exists is False
    assert missing.can_migrate is False
    assert local_ledger_notice(tmp_path / "missing.db") is None

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    plan = inspect_local_ledger(empty)
    assert plan.exists is True
    assert plan.issues == ["database has no MAC tasks table"]

    store = SQLiteStore(str(tmp_path / "mac.db"))
    store.close()
    assert inspect_local_ledger(tmp_path / "mac.db").active_task_count == 0
    assert local_ledger_notice(tmp_path / "mac.db") is None


def test_inspect_wraps_database_open_and_query_errors(tmp_path, monkeypatch):
    source = tmp_path / "mac.db"
    source.touch()
    real_connect = sqlite3.connect

    def fail_connect(*_args, **_kwargs):
        raise sqlite3.DatabaseError("unreadable")

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    with pytest.raises(LocalLedgerMigrationError, match="could not open"):
        inspect_local_ledger(source)

    monkeypatch.setattr(sqlite3, "connect", real_connect)
    monkeypatch.setattr(
        local_migration,
        "_table_exists",
        lambda *_args: (_ for _ in ()).throw(sqlite3.DatabaseError("bad schema")),
    )
    with pytest.raises(LocalLedgerMigrationError, match="could not inspect"):
        inspect_local_ledger(source)


def test_inspect_orders_dependencies_and_reports_notice(tmp_path):
    source = tmp_path / "mac.db"
    parent, child = _source(source, held=True)

    plan = inspect_local_ledger(source)

    assert plan.can_migrate is True
    assert plan.migration_order == [parent, child]
    assert plan.tasks[1].active_dependencies == [parent]
    assert plan.tasks[0].metadata["no_dispatch"] is True
    notice = local_ledger_notice(source)
    assert notice is not None
    assert notice["status"] == "migration_required"
    assert notice["active_task_count"] == 2

    conn = sqlite3.connect(source)
    try:
        conn.execute("UPDATE tasks SET state = 'completed' WHERE id = ?", (parent,))
        conn.commit()
    finally:
        conn.close()
    completed_dependency = inspect_local_ledger(source)
    assert completed_dependency.tasks[0].id == child
    assert completed_dependency.tasks[0].satisfied_dependencies == [parent]


@pytest.mark.parametrize("dependency_state", ["failed", "cancelled"])
def test_inspect_rejects_missing_terminal_and_cyclic_dependencies(
    tmp_path, dependency_state
):
    source = tmp_path / (dependency_state + ".db")
    parent, child = _source(source)
    conn = sqlite3.connect(source)
    try:
        conn.execute("UPDATE tasks SET state = ? WHERE id = ?", (dependency_state, parent))
        conn.commit()
    finally:
        conn.close()
    plan = inspect_local_ledger(source)
    assert plan.can_migrate is False
    assert "terminal local task" in plan.issues[0]
    assert local_ledger_notice(source)["status"] == "manual_review_required"

    conn = sqlite3.connect(source)
    try:
        conn.execute(
            "UPDATE tasks SET state = 'open', dependencies = ? WHERE id = ?",
            (json.dumps([child]), parent),
        )
        conn.commit()
    finally:
        conn.close()
    cycle = inspect_local_ledger(source)
    assert cycle.can_migrate is False
    assert any("dependency cycle" in issue for issue in cycle.issues)

    conn = sqlite3.connect(source)
    try:
        conn.execute(
            "UPDATE tasks SET dependencies = ? WHERE id = ?",
            (json.dumps(["task_missing"]), child),
        )
        conn.commit()
    finally:
        conn.close()
    missing = inspect_local_ledger(source)
    assert any("missing local task" in issue for issue in missing.issues)


def test_sanitized_metadata_keeps_context_and_adds_provenance(tmp_path):
    source = tmp_path / "mac.db"
    _source(source, held=True)
    candidate = inspect_local_ledger(source).tasks[0]

    metadata = _sanitize_metadata(
        candidate,
        migration_id="localmig_test",
        source_database_id="localdb_test",
    )

    assert "execution_contract" not in metadata
    assert metadata["origin"] == {"type": "direct_task", "keep": "context"}
    assert metadata["no_dispatch"] is True
    assert metadata["local_ledger_migration"]["schema"] == PROVENANCE_SCHEMA

    empty_origin = replace(
        candidate,
        metadata={"origin": {"repository_id": "repo", "repository_path": "/tmp"}},
    )
    assert "origin" not in _sanitize_metadata(
        empty_origin,
        migration_id="localmig_test",
        source_database_id="localdb_test",
    )


def test_conversion_helpers_cover_invalid_shapes_and_dictish_values():
    class Dictish:
        def __init__(self, value):
            self.value = value

        def to_dict(self):
            return self.value

    assert _as_dict(Dictish({"id": "task_x"})) == {"id": "task_x"}
    assert _as_dict(Dictish([])) == {}
    assert _as_dict(object()) == {}
    issues = []
    assert _json_object("{", task_id="task_x", field="metadata", issues=issues) == {}
    assert _json_object("[]", task_id="task_x", field="metadata", issues=issues) == {}
    assert _json_list("{", task_id="task_x", field="dependencies", issues=issues) == []
    assert _json_list("{}", task_id="task_x", field="dependencies", issues=issues) == []
    assert len(issues) == 4


def test_remote_provenance_index_rejects_duplicates():
    target = FakeTarget()
    provenance = {
        "schema": PROVENANCE_SCHEMA,
        "source_database_id": "localdb_test",
        "source_task_id": "task_source",
    }
    for index in (1, 2):
        task_id = "task_%032x" % index
        target.tasks[task_id] = {
            "id": task_id,
            "state": "open",
            "metadata": {"local_ledger_migration": provenance},
        }
    with pytest.raises(LocalLedgerMigrationError, match="multiple migrated copies"):
        _remote_provenance_index(target, "localdb_test")

    target.tasks = {
        "plain": {"id": "plain", "metadata": {}},
        "wrong-schema": {
            "id": "wrong-schema",
            "metadata": {"local_ledger_migration": {"schema": "wrong"}},
        },
        "wrong-db": {
            "id": "wrong-db",
            "metadata": {
                "local_ledger_migration": {
                    "schema": PROVENANCE_SCHEMA,
                    "source_database_id": "another",
                }
            },
        },
        "missing-source": {
            "id": "missing-source",
            "metadata": {
                "local_ledger_migration": {
                    "schema": PROVENANCE_SCHEMA,
                    "source_database_id": "localdb_test",
                }
            },
        },
    }
    assert _remote_provenance_index(target, "localdb_test") == {}


def test_remote_verification_reports_missing_and_wrong_provenance(tmp_path):
    source = tmp_path / "mac.db"
    _source(source)
    candidate = inspect_local_ledger(source).tasks[0]
    target = FakeTarget()
    created = target.create_task(
        candidate.title,
        description=candidate.description,
        project=candidate.project,
        priority=candidate.priority,
        required_capabilities=candidate.required_capabilities,
        dependencies=[],
        metadata={},
        max_attempts=candidate.max_attempts,
    )
    with pytest.raises(LocalLedgerMigrationError, match="provenance is missing"):
        _verify_remote_task(target, candidate, created["id"], [], "localdb_test")

    target.tasks[created["id"]]["metadata"] = {
        "local_ledger_migration": {
            "source_database_id": "wrong",
            "source_task_id": "task_wrong",
        }
    }
    with pytest.raises(LocalLedgerMigrationError) as excinfo:
        _verify_remote_task(target, candidate, created["id"], [], "localdb_test")
    assert "source_database_id" in str(excinfo.value)
    assert "source_task_id" in str(excinfo.value)


def test_migrate_verifies_cancels_and_archives(tmp_path):
    source = tmp_path / "mac.db"
    archive_dir = tmp_path / "archive"
    parent, child = _source(source, held=True)
    target = FakeTarget()

    result = migrate_local_ledger(
        target,
        source_db=source,
        archive_dir=archive_dir,
        source_secret_key=SECRET,
    )

    assert source.exists() is False
    assert Path(result.archive_path).is_file()
    assert Path(result.manifest_path).is_file()
    manifest = json.loads(Path(result.manifest_path).read_text())
    assert manifest["status"] == "completed"
    assert len(result.archive_sha256) == 64
    assert result.cancelled_local_task_ids == [parent, child]
    assert target.tasks[result.remote_task_ids[child]]["dependencies"] == [
        result.remote_task_ids[parent]
    ]
    assert target.tasks[result.remote_task_ids[parent]]["metadata"]["no_dispatch"] is True
    assert not list(archive_dir.glob("*.pending.json"))

    conn = sqlite3.connect(result.archive_path)
    try:
        states = dict(conn.execute("SELECT id, state FROM tasks"))
        history = conn.execute(
            "SELECT COUNT(*) FROM task_history WHERE actor = 'local-ledger-migrator'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert states[parent] == "cancelled"
    assert states[child] == "cancelled"
    assert history == 2
    assert result.to_dict()["schema"] == result.schema
    assert default_archive_dir().name == "archive"


def test_migration_retry_reuses_verified_remote_tasks(tmp_path):
    source = tmp_path / "mac.db"
    _source(source)
    target = FakeTarget()
    target.corrupt_details = True

    with pytest.raises(LocalLedgerMigrationError, match="remote verification failed"):
        migrate_local_ledger(
            target,
            source_db=source,
            archive_dir=tmp_path / "archive",
            source_secret_key=SECRET,
        )
    assert source.exists()
    assert target.created == 2

    target.corrupt_details = False
    result = migrate_local_ledger(
        target,
        source_db=source,
        archive_dir=tmp_path / "archive",
        source_secret_key=SECRET,
    )
    assert target.created == 2
    assert sorted(result.reused_remote_task_ids) == sorted(result.remote_task_ids.values())


def test_migration_detects_source_drift_before_cancellation(tmp_path):
    source = tmp_path / "mac.db"
    _source(source)
    target = FakeTarget()

    def mutate_after_first_create(created):
        if created == 1:
            conn = sqlite3.connect(source)
            try:
                conn.execute(
                    "UPDATE tasks SET updated_at = 'changed' WHERE title = 'parent'"
                )
                conn.commit()
            finally:
                conn.close()

    target.after_create = mutate_after_first_create
    with pytest.raises(LocalLedgerMigrationError, match="changed during migration"):
        migrate_local_ledger(
            target,
            source_db=source,
            archive_dir=tmp_path / "archive",
            source_secret_key=SECRET,
        )
    assert source.exists()
    conn = sqlite3.connect(source)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state = 'cancelled'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_migration_detects_changed_active_set_and_missing_remote_id(tmp_path):
    source = tmp_path / "changed-set.db"
    _parent, child = _source(source)
    target = FakeTarget()

    def finish_child(created):
        if created == 1:
            conn = sqlite3.connect(source)
            try:
                conn.execute("UPDATE tasks SET state = 'completed' WHERE id = ?", (child,))
                conn.commit()
            finally:
                conn.close()

    target.after_create = finish_child
    with pytest.raises(LocalLedgerMigrationError, match="active task set changed"):
        migrate_local_ledger(
            target,
            source_db=source,
            archive_dir=tmp_path / "archive",
            source_secret_key=SECRET,
        )

    class MissingIdTarget(FakeTarget):
        def create_task(self, title: str, **kwargs: Any) -> Dict[str, Any]:
            del title, kwargs
            return {}

    fresh = tmp_path / "missing-id.db"
    _source(fresh)
    with pytest.raises(LocalLedgerMigrationError, match="did not return an id"):
        migrate_local_ledger(MissingIdTarget(), source_db=fresh)


def test_archive_failure_restores_active_source_database(tmp_path, monkeypatch):
    source = tmp_path / "mac.db"
    _source(source)

    def fail_archive(*_args, **_kwargs):
        raise LocalLedgerMigrationError("archive unavailable")

    monkeypatch.setattr(local_migration, "_archive_database", fail_archive)
    with pytest.raises(LocalLedgerMigrationError, match="archive unavailable"):
        migrate_local_ledger(
            FakeTarget(),
            source_db=source,
            archive_dir=tmp_path / "archive",
            source_secret_key=SECRET,
        )

    assert source.exists()
    conn = sqlite3.connect(source)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state IN ('open', 'blocked')"
        ).fetchone()[0] == 2
    finally:
        conn.close()
    assert not list((tmp_path / "archive").glob("*.recovery.db"))


def test_manifest_is_verified_before_source_removal_and_failure_restores(
    tmp_path, monkeypatch
):
    source = tmp_path / "mac.db"
    _source(source)
    real_verify = local_migration._verify_archive_manifest

    def verify_while_source_exists(*args, **kwargs):
        assert source.exists()
        real_verify(*args, **kwargs)
        raise LocalLedgerMigrationError("manifest verification stopped migration")

    monkeypatch.setattr(
        local_migration,
        "_verify_archive_manifest",
        verify_while_source_exists,
    )
    with pytest.raises(LocalLedgerMigrationError, match="verification stopped"):
        migrate_local_ledger(
            FakeTarget(),
            source_db=source,
            archive_dir=tmp_path / "archive",
            source_secret_key=SECRET,
        )

    assert source.exists()
    conn = sqlite3.connect(source)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state IN ('open', 'blocked')"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_migration_refuses_missing_no_active_and_broken_plan(tmp_path):
    target = FakeTarget()
    with pytest.raises(LocalLedgerMigrationError, match="does not exist"):
        migrate_local_ledger(target, source_db=tmp_path / "missing.db")

    empty = tmp_path / "empty.db"
    store = SQLiteStore(str(empty))
    store.close()
    with pytest.raises(LocalLedgerMigrationError, match="no active tasks"):
        migrate_local_ledger(target, source_db=empty)

    broken = tmp_path / "broken.db"
    _parent, child = _source(broken)
    conn = sqlite3.connect(broken)
    try:
        conn.execute(
            "UPDATE tasks SET dependencies = ? WHERE id = ?",
            (json.dumps(["task_missing"]), child),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(LocalLedgerMigrationError, match="manual repair"):
        migrate_local_ledger(target, source_db=broken)


def test_migration_requires_source_secret_and_cli_defaults_to_plan(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "mac.db"
    _source(source)
    monkeypatch.delenv("MAC_SECRET_KEY", raising=False)
    with pytest.raises(LocalLedgerMigrationError, match="MAC_SECRET_KEY is required"):
        migrate_local_ledger(
            FakeTarget(),
            source_db=source,
            archive_dir=tmp_path / "archive",
        )

    rc = main(
        [
            "--json",
            "migrate",
            "local-ledger",
            "--source-db",
            str(source),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["active_task_count"] == 2
    assert payload["can_migrate"] is True

    rc = main(
        [
            "--db",
            str(source),
            "migrate",
            "local-ledger",
            "--source-db",
            str(source),
        ]
    )
    assert rc == 1
    assert "--source-db" in capsys.readouterr().err


def test_login_notice_detects_default_home_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / ".mac" / "mac.db"
    source.parent.mkdir(parents=True)
    _source(source)

    notice = _local_ledger_notice_payload()

    assert notice is not None
    assert notice["status"] == "migration_required"
    assert notice["active_task_count"] == 2
    assert notice["next_command"] == "mac migrate local-ledger --execute"


def test_secret_resolution_atomic_write_and_archive_validation(tmp_path, monkeypatch):
    source = tmp_path / "mac.db"
    parent, _child = _source(source)
    monkeypatch.setenv("MAC_SECRET_KEY", SECRET)
    assert _load_source_secret_key(source, None) == SECRET
    monkeypatch.delenv("MAC_SECRET_KEY")
    env_file = source.parent / ".env"
    env_file.write_text("MAC_SECRET_KEY=%s\n" % SECRET, encoding="utf-8")
    assert _load_source_secret_key(source, None) == SECRET

    output = tmp_path / "atomic" / "manifest.json"
    _write_json_atomic(output, {"ok": True})
    assert json.loads(output.read_text()) == {"ok": True}

    with pytest.raises(LocalLedgerMigrationError, match="does not contain cancelled"):
        _archive_database(source, tmp_path / "archive", "localmig_invalid", [parent])
    assert source.exists()

    manifest = tmp_path / "bad-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(LocalLedgerMigrationError, match="schema does not match"):
        _verify_archive_manifest(
            manifest,
            archive_path=source,
            archive_hash="0" * 64,
        )


def test_atomic_write_cleanup_and_archive_manifest_failure_modes(tmp_path, monkeypatch):
    output = tmp_path / "atomic" / "manifest.json"

    def fail_dump(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", fail_dump)
    with pytest.raises(OSError, match="disk full"):
        _write_json_atomic(output, {"ok": True})
    assert not output.exists()
    assert not list(output.parent.iterdir())

    monkeypatch.undo()
    archive = tmp_path / "archive.db"
    archive.write_bytes(b"archive")
    archive_hash = _sha256(archive)
    manifest = tmp_path / "archive.json"
    base = {
        "schema": ARCHIVE_SCHEMA,
        "status": "archive_verified",
        "archive_path": str(archive),
        "archive_sha256": archive_hash,
    }

    manifest.write_text("{", encoding="utf-8")
    with pytest.raises(LocalLedgerMigrationError, match="could not read"):
        _verify_archive_manifest(
            manifest, archive_path=archive, archive_hash=archive_hash
        )

    for field, value, message in (
        ("status", "pending", "not in archive_verified"),
        ("archive_path", "/wrong", "path does not match"),
        ("archive_sha256", "0" * 64, "hash does not match"),
    ):
        payload = dict(base)
        payload[field] = value
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(LocalLedgerMigrationError, match=message):
            _verify_archive_manifest(
                manifest, archive_path=archive, archive_hash=archive_hash
            )

    manifest.write_text(json.dumps(base), encoding="utf-8")
    archive.write_bytes(b"changed")
    with pytest.raises(LocalLedgerMigrationError, match="changed after verification"):
        _verify_archive_manifest(
            manifest, archive_path=archive, archive_hash=archive_hash
        )


def test_result_dataclass_serializes():
    result = LocalLedgerMigrationResult(
        migration_id="localmig_test",
        source_db="/tmp/mac.db",
        source_database_id="localdb_test",
        remote_task_ids={},
        reused_remote_task_ids=[],
        verified_task_ids=[],
        cancelled_local_task_ids=[],
        archive_path="/tmp/archive.db",
        archive_sha256="0" * 64,
        manifest_path="/tmp/archive.json",
        completed_at="now",
    )
    assert result.to_dict()["migration_id"] == "localmig_test"

    retirement = LocalLedgerRetirementResult(
        source_db="/tmp/mac.db",
        source_database_id="localdb_test",
        archive_path="/tmp/archive.db",
        archive_sha256="0" * 64,
        manifest_path="/tmp/archive.json",
        completed_at="now",
    )
    assert retirement.to_dict()["schema"] == retirement.schema


def test_retire_inactive_local_ledger_archives_and_refuses_active_work(tmp_path):
    inactive = tmp_path / "inactive.db"
    store = SQLiteStore(str(inactive))
    plane = ControlPlane(store, secret_key=SECRET)
    task = plane.create_task("historical")
    conn = sqlite3.connect(inactive)
    try:
        conn.execute("UPDATE tasks SET state = 'completed' WHERE id = ?", (task.id,))
        conn.commit()
    finally:
        conn.close()
        store.close()

    result = retire_inactive_local_ledger(
        source_db=inactive,
        archive_dir=tmp_path / "archive",
    )

    assert not inactive.exists()
    assert Path(result.archive_path).is_file()
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["retirement_schema"] == result.schema

    active = tmp_path / "active.db"
    _source(active)
    with pytest.raises(LocalLedgerMigrationError, match="migrate them to the hub"):
        retire_inactive_local_ledger(
            source_db=active,
            archive_dir=tmp_path / "archive",
        )
    assert active.exists()


def test_cli_retires_inactive_local_ledger_without_target_authority(
    tmp_path, capsys
):
    source = tmp_path / "inactive.db"
    store = SQLiteStore(str(source))
    store.close()

    rc = main(
        [
            "--json",
            "migrate",
            "local-ledger",
            "--source-db",
            str(source),
            "--archive-dir",
            str(tmp_path / "archive"),
            "--retire-inactive",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "mac.local_ledger_retirement_result.v1"
    assert not source.exists()
