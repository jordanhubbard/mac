"""Live-Postgres integration tests for PostgresStore (K8s Phase 3.6).

Skipped unless MAC_TEST_PG_URL points at a writable database. Codifies
the manual smoke test from the Phase 3.4 commit so the same end-to-end
behaviors are regression-protected going forward:

  - The bundled schema applies clean to a fresh schema.
  - PostgresStore satisfies the Store protocol.
  - The `?` placeholder translator handles INSERT, SELECT, WHERE, IN.
  - `ON CONFLICT(id) DO UPDATE SET ...` upserts work.
  - Row indexing supports both name and position (sqlite3.Row parity).
  - `json_extract` SQL shim filters TEXT-JSON columns.
  - The task-state PL/pgSQL trigger rejects bad INSERTs and UPDATEs.
  - `transaction()` commits on clean exit and rolls back on exception.
  - The `events` view projects the same shape across underlying tables.

Run with: MAC_TEST_PG_URL=postgresql://postgres:test@127.0.0.1:55432/mac \
          uv run --extra dev pytest -q -m postgres tests/test_postgres_live.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.postgres

from mac.store import Store, StoreError  # noqa: E402


def _insert_task(store, **overrides) -> str:
    cols = {
        "id": "task-1",
        "title": "first",
        "description": "d",
        "priority": 1,
        "state": "open",
        "required_capabilities": "[]",
        "dependencies": "[]",
        "metadata": "{}",
        "attempt_count": 0,
        "max_attempts": 3,
        "created_at": "2026-05-28T00:00:00Z",
        "updated_at": "2026-05-28T00:00:00Z",
    }
    cols.update(overrides)
    store.execute(
        "INSERT INTO tasks (id, title, description, priority, state, "
        "required_capabilities, dependencies, metadata, attempt_count, "
        "max_attempts, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cols["id"], cols["title"], cols["description"], cols["priority"],
            cols["state"], cols["required_capabilities"], cols["dependencies"],
            cols["metadata"], cols["attempt_count"], cols["max_attempts"],
            cols["created_at"], cols["updated_at"],
        ),
    )
    return cols["id"]


def test_postgres_store_satisfies_protocol(postgres_store) -> None:
    assert isinstance(postgres_store, Store)


def test_schema_applied_with_57_base_tables(postgres_store) -> None:
    row = postgres_store.query_one(
        "SELECT count(*) AS n FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = ?",
        ("BASE TABLE",),
    )
    assert row["n"] == 57


def test_placeholder_translation_handles_in_clause(postgres_store) -> None:
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("t1", "acme", "{}", "now", "now"),
    )
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("t2", "globex", "{}", "now", "now"),
    )
    rows = postgres_store.query_all(
        "SELECT id FROM tenants WHERE id IN (?, ?) ORDER BY id", ("t1", "t2")
    )
    assert [r["id"] for r in rows] == ["t1", "t2"]


def test_on_conflict_upsert(postgres_store) -> None:
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        ("t1", "first", "{}", "now", "now"),
    )
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        ("t1", "second", "{}", "now", "now"),
    )
    row = postgres_store.query_one("SELECT name FROM tenants WHERE id = ?", ("t1",))
    assert row["name"] == "second"


def test_row_supports_named_and_positional_access(postgres_store) -> None:
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("t1", "acme", "{}", "now", "now"),
    )
    row = postgres_store.query_one(
        "SELECT id, name FROM tenants WHERE id = ?", ("t1",)
    )
    assert row["id"] == "t1"
    assert row["name"] == "acme"
    assert row[0] == "t1"
    assert row[1] == "acme"


def test_json_extract_filters_text_json_column(postgres_store) -> None:
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("t1", "acme", '{"plan":"free"}', "now", "now"),
    )
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("t2", "globex", '{"plan":"pro"}', "now", "now"),
    )
    row = postgres_store.query_one(
        "SELECT name FROM tenants WHERE json_extract(metadata, '$.plan') = ?",
        ("pro",),
    )
    assert row is not None
    assert row["name"] == "globex"


def test_task_state_trigger_rejects_bad_insert(postgres_store) -> None:
    with pytest.raises(StoreError) as exc:
        _insert_task(postgres_store, id="bad", state="NOPE")
    assert "invalid task state" in str(exc.value)


def test_task_state_trigger_rejects_bad_update(postgres_store) -> None:
    _insert_task(postgres_store, id="ok")
    with pytest.raises(StoreError) as exc:
        postgres_store.execute(
            "UPDATE tasks SET state = ? WHERE id = ?", ("NOPE", "ok")
        )
    assert "invalid task state" in str(exc.value)


def test_partial_unique_index_active_lease_per_task(postgres_store) -> None:
    _insert_task(postgres_store, id="task-1")
    postgres_store.execute(
        "INSERT INTO leases (id, task_id, agent_id, expires_at, status, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("l1", "task-1", "a1", "now", "active", "now", "now"),
    )
    with pytest.raises(StoreError) as exc:
        postgres_store.execute(
            "INSERT INTO leases (id, task_id, agent_id, expires_at, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("l2", "task-1", "a2", "now", "active", "now", "now"),
        )
    # Postgres reports the unique-constraint name.
    assert "uniq_leases_active_per_task" in str(exc.value)


def test_transaction_commit_on_clean_exit(postgres_store) -> None:
    with postgres_store.transaction() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            ("t-commit", "ok", "{}", "now", "now"),
        )
    row = postgres_store.query_one(
        "SELECT id FROM tenants WHERE id = ?", ("t-commit",)
    )
    assert row is not None


def test_transaction_rollback_on_exception(postgres_store) -> None:
    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with postgres_store.transaction() as conn:
            conn.execute(
                "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                ("t-rollback", "nope", "{}", "now", "now"),
            )
            raise _Boom("rollback me")
    assert (
        postgres_store.query_one(
            "SELECT id FROM tenants WHERE id = ?", ("t-rollback",)
        )
        is None
    )


def test_events_view_projects_task_history(postgres_store) -> None:
    _insert_task(postgres_store, id="task-1")
    postgres_store.execute(
        "INSERT INTO task_history (id, task_id, event_type, actor, from_state, "
        "to_state, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("h1", "task-1", "created", "op", None, "open", "{}", "now"),
    )
    row = postgres_store.query_one(
        "SELECT id, subject_type, subject_id, event_type, actor, detail "
        "FROM events WHERE id = ?",
        ("h1",),
    )
    assert row is not None
    assert row["subject_type"] == "task"
    assert row["subject_id"] == "task-1"
    assert row["event_type"] == "created"
    # detail is text-encoded jsonb; both states present, NULL serialized as null.
    detail = row["detail"]
    assert "from_state" in detail
    assert "to_state" in detail
    assert '"open"' in detail


def test_ensure_column_adds_missing_column(postgres_store) -> None:
    postgres_store.ensure_column(
        "tenants", "extra_label", "extra_label TEXT"
    )
    # Re-run is idempotent.
    postgres_store.ensure_column(
        "tenants", "extra_label", "extra_label TEXT"
    )
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, extra_label, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("t1", "with-extra", "{}", "hello", "now", "now"),
    )
    row = postgres_store.query_one(
        "SELECT extra_label FROM tenants WHERE id = ?", ("t1",)
    )
    assert row["extra_label"] == "hello"
