"""Emptying a schema between tests must be as isolating as rebuilding it.

The control-plane public contract file parametrizes ~500 cases, each of which
took a fresh schema: a real CREATE SCHEMA plus the packaged DDL, 164 tables and
219 indexes, ~1.2s. That was 670 of the file's 745 seconds -- the file is in the
selector's `always_run` set, so EVERY task paid it, and it alone was most of the
in-sandbox gate that kept timing out.

Reusing one schema and emptying it between cases took the file from 670s to
28s with all 578 tests still passing. The danger in that trade is that a reset
which quietly leaves rows behind still looks green: the cases would pass on
state they did not create. These tests exist to make that failure loud.

Why DELETE and not TRUNCATE, measured on this schema:

    TRUNCATE tasks CASCADE          6.4s  (tasks is an FK target for dozens of
                                           tables, so CASCADE takes them all)
    TRUNCATE <all 161 tables>       2.0s
    probe + DELETE of dirty tables  0.03s

TRUNCATE is the usual answer and here it is slower than the schema creation it
was meant to replace.
"""

from __future__ import annotations

import pytest

from mac import test_support


@pytest.fixture()
def store():
    created = test_support.ephemeral_store(swept=False)
    try:
        yield created
    finally:
        test_support.drop_store(created)


def _tables_with_rows(store) -> set[str]:
    with store._pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
            )
            names = sorted(row[0] for row in cur.fetchall())
            found = set()
            for name in names:
                cur.execute('SELECT 1 FROM "%s" LIMIT 1' % name)
                if cur.fetchone():
                    found.add(name)
            return found


def test_reset_removes_rows_a_test_wrote(store):
    """The whole point: one case's writes must not reach the next one."""
    baseline = _tables_with_rows(store)

    with store._pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (id, title, description, state,"
                " required_capabilities, dependencies, metadata,"
                " created_at, updated_at) VALUES"
                " ('task_reset_probe', 't', 'd', 'open', '[]', '[]', '{}',"
                " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
            )
        conn.commit()

    assert "tasks" in _tables_with_rows(store)

    assert test_support.reset_store_data(store) is True

    assert "tasks" not in _tables_with_rows(store)
    assert _tables_with_rows(store) == baseline


def test_reset_keeps_the_migration_ledgers(store):
    """ "Just initialized" includes the migration ledger rows. Wiping them would
    hand the next case a database whose migrations look like they never ran."""
    from mac.task_dependencies import migrate_dependency_edges

    migrate_dependency_edges(store)
    with store._pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO telemetry_data_migrations "
                "(version, component, detail, applied_at) VALUES "
                "('reset-probe', 'test', '{}', '2026-01-01T00:00:00+00:00')"
            )
            cur.execute(
                "INSERT INTO schema_migration_receipts "
                "(version, component, detail, applied_at) VALUES "
                "('reset-probe', 'test', '{}', '2026-01-01T00:00:00+00:00')"
            )
        conn.commit()
    before = _tables_with_rows(store)
    assert {"schema_version", "schema_migrations", "schema_migration_receipts"} <= before
    assert "telemetry_data_migrations" in before
    assert "task_dependency_migrations" in before

    assert test_support.reset_store_data(store) is True

    assert _tables_with_rows(store) == before


def test_reset_rewinds_the_sequence(store):
    """DELETE leaves sequences advanced where TRUNCATE ... RESTART IDENTITY
    would rewind them, so a reused schema would hand out
    observability_events.sequence values a fresh schema never would."""
    with store._pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT nextval('observability_events_sequence_seq')")
            cur.execute("SELECT nextval('observability_events_sequence_seq')")
            advanced = cur.fetchone()[0]
        conn.commit()
    assert advanced > 1

    assert test_support.reset_store_data(store) is True

    with store._pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT nextval('observability_events_sequence_seq')")
            assert cur.fetchone()[0] == 1


def test_an_unswept_store_survives_the_per_test_sweep():
    """tests/conftest.py closes every open store and drops every created schema
    after EACH test. A module-scoped schema registered normally is therefore
    dead by the second case -- which is exactly what happened on the first cut
    of this change: the reset failed on a closed pool, the fixture fell back to
    building a fresh schema, and the file was still slow while looking fixed."""
    kept = test_support.ephemeral_store(swept=False)
    try:
        test_support.drop_created_schemas()

        # Still usable: the sweep did not touch it.
        assert test_support.reset_store_data(kept) is True
    finally:
        test_support.drop_store(kept)


def test_a_swept_store_is_still_swept():
    """The exemption must be opt-in. A default store that outlived the sweep
    would leak schemas for the life of the database."""
    store = test_support.ephemeral_store()
    schema = store._mac_test_schema

    assert schema in test_support._CREATED_SCHEMAS
