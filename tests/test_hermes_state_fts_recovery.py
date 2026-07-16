"""Regression tests: FTS5 orphan-schema recovery in vendored hermes_state.

Background: ``SessionDB._init_schema()`` probes each FTS5 virtual table with a
``SELECT ... LIMIT 0`` and, on ``no such table``, creates it fresh. But an FTS5
virtual table can survive in ``sqlite_master`` while its backing shadow tables
(``messages_fts_data`` / ``_idx`` / ``_docsize`` / ``_config``) are lost or
corrupted — an "orphan schema". The probe then raises ``vtable constructor
failed`` (a bare ``sqlite3.DatabaseError``, not ``no such table``), which used
to abort Hermes startup on a DB that was otherwise fully usable.

These tests corrupt a healthy DB into that orphan state and assert that
re-opening it self-heals: the orphaned index is dropped, recreated, and
backfilled from ``messages`` instead of raising.
"""

from __future__ import annotations

import sqlite3

import pytest


def _ensure_hermes_on_path() -> None:
    from mac import hermes_vendor

    if not hermes_vendor.is_vendored():
        pytest.skip("vendored hermes tree not present")
    hermes_vendor.ensure_on_path()


@pytest.fixture(autouse=True)
def _hermes_on_path():
    _ensure_hermes_on_path()


def _session_db_cls():
    from hermes_state import SessionDB

    return SessionDB


def _orphan_fts_shadow_tables(db_path, base: str) -> None:
    """Delete an FTS index's shadow tables from sqlite_master, leaving the
    virtual-table declaration behind — reproduces the orphan-schema failure."""
    raw = sqlite3.connect(str(db_path))
    try:
        raw.execute("PRAGMA writable_schema=ON")
        for suffix in ("_data", "_idx", "_docsize", "_config"):
            raw.execute("DELETE FROM sqlite_master WHERE name = ?", (base + suffix,))
        raw.execute("PRAGMA writable_schema=OFF")
        raw.commit()
    finally:
        raw.close()


def _assert_orphaned(db_path, table: str) -> None:
    chk = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.DatabaseError):
            chk.execute("SELECT * FROM %s LIMIT 0" % table).fetchall()
    finally:
        chk.close()


def test_classifier_distinguishes_fresh_create_from_orphan():
    SessionDB = _session_db_cls()
    f = SessionDB._is_fts_orphan_error
    # Missing top-level vtable is the fresh-create case, not an orphan.
    assert f("messages_fts", "no such table: messages_fts") is False
    # Missing/lost shadow tables and corruption are recoverable orphans.
    assert f("messages_fts", "no such table: main.messages_fts_data") is True
    assert f("messages_fts", "no such table: messages_fts_idx") is True
    assert f("messages_fts", "no such table: messages_fts_docsize") is True
    assert f("messages_fts", "no such table: messages_fts_content") is True
    assert f("messages_fts", "no such table: messages_fts_config") is True
    assert f("messages_fts", "vtable constructor failed: messages_fts") is True
    assert f("messages_fts", "malformed database schema (messages_fts)") is True
    assert f("messages_fts", "database disk image is malformed") is True
    # The trigram index shares the same recovery classifier.
    assert (
        f("messages_fts_trigram", "no such table: messages_fts_trigram_data")
        is True
    )
    # Non-orphan operational failures must not be treated as recoverable.
    assert f("messages_fts", "database or disk is full") is False
    assert f("messages_fts", "some unrelated error") is False
    assert f("messages_fts", "disk I/O error") is False
    assert f("messages_fts", "no such table: messages") is False
    # Case-insensitive: the bare-vtable fresh-create case is never an orphan.
    assert f("messages_fts", "no such table: MESSAGES_FTS") is False


def test_orphaned_messages_fts_is_recovered(tmp_path):
    SessionDB = _session_db_cls()
    db_path = tmp_path / "state.db"

    db = SessionDB(db_path)
    assert db._fts_enabled
    db._conn.close()

    _orphan_fts_shadow_tables(db_path, "messages_fts")
    _assert_orphaned(db_path, "messages_fts")

    recovered = SessionDB(db_path)
    try:
        assert recovered._fts_enabled
        # Index is usable again after recovery.
        recovered._conn.execute("SELECT * FROM messages_fts LIMIT 0").fetchall()
    finally:
        recovered._conn.close()


def test_orphaned_trigram_index_is_recovered(tmp_path):
    SessionDB = _session_db_cls()
    db_path = tmp_path / "state.db"

    SessionDB(db_path)._conn.close()

    _orphan_fts_shadow_tables(db_path, "messages_fts_trigram")
    _assert_orphaned(db_path, "messages_fts_trigram")

    recovered = SessionDB(db_path)
    try:
        # Main index stays enabled; trigram index is rebuilt and usable.
        assert recovered._fts_enabled
        recovered._conn.execute(
            "SELECT * FROM messages_fts_trigram LIMIT 0"
        ).fetchall()
    finally:
        recovered._conn.close()


def test_recovery_backfills_existing_messages(tmp_path):
    SessionDB = _session_db_cls()
    db_path = tmp_path / "state.db"

    db = SessionDB(db_path)
    db._conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('s', 't', 0)"
    )
    db._conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES ('s', 'user', 'uniquetokenxyz', 1)"
    )
    db._conn.commit()
    db._conn.close()

    _orphan_fts_shadow_tables(db_path, "messages_fts")

    recovered = SessionDB(db_path)
    try:
        rows = recovered._conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'uniquetokenxyz'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        recovered._conn.close()


def _drop_single_shadow_table(db_path, base: str, suffix: str) -> None:
    """Delete just one shadow table's declaration, leaving the rest and the
    virtual-table declaration behind. Dropping ``_data`` (or ``_config``) is
    enough for the vtable constructor to fail on load — a partial orphan that
    is distinct from losing every shadow table at once."""
    raw = sqlite3.connect(str(db_path))
    try:
        raw.execute("PRAGMA writable_schema=ON")
        raw.execute(
            "DELETE FROM sqlite_master WHERE name = ?", (base + suffix,)
        )
        raw.execute("PRAGMA writable_schema=OFF")
        raw.commit()
    finally:
        raw.close()


def test_partial_orphan_missing_data_shadow_is_recovered(tmp_path):
    SessionDB = _session_db_cls()
    db_path = tmp_path / "state.db"

    db = SessionDB(db_path)
    db._conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('s', 't', 0)"
    )
    db._conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES ('s', 'user', 'partialtoken', 1)"
    )
    db._conn.commit()
    db._conn.close()

    # Lose only the _data shadow table; the vtable + other shadows survive.
    _drop_single_shadow_table(db_path, "messages_fts", "_data")
    _assert_orphaned(db_path, "messages_fts")

    recovered = SessionDB(db_path)
    try:
        assert recovered._fts_enabled
        rows = recovered._conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'partialtoken'"
        ).fetchall()
        assert len(rows) == 1
    finally:
        recovered._conn.close()


def test_both_indexes_orphaned_recover_together(tmp_path):
    SessionDB = _session_db_cls()
    db_path = tmp_path / "state.db"

    SessionDB(db_path)._conn.close()

    _orphan_fts_shadow_tables(db_path, "messages_fts")
    _orphan_fts_shadow_tables(db_path, "messages_fts_trigram")
    _assert_orphaned(db_path, "messages_fts")
    _assert_orphaned(db_path, "messages_fts_trigram")

    recovered = SessionDB(db_path)
    try:
        assert recovered._fts_enabled
        recovered._conn.execute("SELECT * FROM messages_fts LIMIT 0").fetchall()
        recovered._conn.execute(
            "SELECT * FROM messages_fts_trigram LIMIT 0"
        ).fetchall()
    finally:
        recovered._conn.close()


def test_healthy_reopen_does_not_trigger_recovery(tmp_path):
    SessionDB = _session_db_cls()
    db_path = tmp_path / "state.db"

    db = SessionDB(db_path)
    db.create_session("s1", "test")
    db._conn.close()

    reopened = SessionDB(db_path)
    try:
        assert reopened._fts_enabled
        reopened._conn.execute("SELECT * FROM messages_fts LIMIT 0").fetchall()
        reopened._conn.execute(
            "SELECT * FROM messages_fts_trigram LIMIT 0"
        ).fetchall()
    finally:
        reopened._conn.close()
