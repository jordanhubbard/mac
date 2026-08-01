"""Old-schema -> new-schema migration tests for the PersonaInstance rename.

``hermes_instances`` -> ``persona_instances`` and the
``platform_bindings.hermes_instance_id`` FK -> ``persona_instance_id`` are
renamed in place by ``Store._migrate_persona_instance_identity``. These
tests assert the one-time migration preserves every row and relationship from a
legacy database, records an immutable receipt, and is idempotent.
"""

from __future__ import annotations

import sqlite3

import pytest

from mac.identity_service import IdentityService
from mac.models import HermesInstance, PersonaInstance
from mac.store import StoreError
from mac.test_support import ephemeral_store

PERSONA_MIGRATION_VERSION = "mac.persona_instance_identity.v1"
PERSONA_REPAIR_VERSION = "mac.persona_instance_identity_fk_repair.v1"


def _build_old_schema_db(path: str) -> None:
    """Create a legacy database that still uses the pre-persona names."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tenants (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE personas (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
            soul_ref TEXT NOT NULL, memory_scope TEXT NOT NULL, metadata TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE hermes_instances (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            persona_id TEXT REFERENCES personas(id) ON DELETE SET NULL,
            home_ref TEXT NOT NULL, status TEXT NOT NULL, metadata TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            UNIQUE(tenant_id, name)
        );
        CREATE INDEX idx_hermes_instances_tenant ON hermes_instances (tenant_id);
        CREATE TABLE platform_bindings (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            hermes_instance_id TEXT NOT NULL
                REFERENCES hermes_instances(id) ON DELETE CASCADE,
            platform TEXT NOT NULL, external_id TEXT NOT NULL, display_name TEXT NOT NULL,
            scopes TEXT NOT NULL, metadata TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(tenant_id, platform, external_id)
        );
        CREATE INDEX idx_platform_bindings_instance
            ON platform_bindings (hermes_instance_id);
        """
    )
    conn.execute(
        "INSERT INTO tenants VALUES ('t1', 'acme', '{}', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO personas VALUES "
        "('p1', 't1', 'concierge', 'soul://p1', 'scope://p1', '{}', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO hermes_instances VALUES "
        "('h1', 't1', 'bot', 'p1', 'ref://home', 'active', "
        "'{\"k\":\"v\"}', 'now', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO hermes_instances VALUES "
        "('h2', 't1', 'bot2', NULL, 'ref://home2', 'paused', "
        "'{}', 'now', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO platform_bindings VALUES "
        "('b1', 't1', 'h1', 'slack', 'U1', 'User One', '{}', '{}', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO platform_bindings VALUES "
        "('b2', 't1', 'h2', 'telegram', 'T2', 'User Two', '{}', '{}', 'now', 'now')"
    )
    conn.commit()
    conn.close()


def _apply_broken_v1_migration(path: str) -> None:
    """Reproduce the released migration that stranded the binding FK."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.executescript(
        """
        ALTER TABLE hermes_instances RENAME TO persona_instances;
        ALTER TABLE platform_bindings
            RENAME COLUMN hermes_instance_id TO persona_instance_id;
        DROP INDEX idx_hermes_instances_tenant;
        CREATE INDEX idx_persona_instances_tenant
            ON persona_instances (tenant_id);
        DROP INDEX idx_platform_bindings_instance;
        CREATE INDEX idx_platform_bindings_instance
            ON platform_bindings (persona_instance_id);
        CREATE TABLE schema_migration_receipts (
            version TEXT PRIMARY KEY,
            component TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '{}',
            applied_at TEXT NOT NULL
        );
        CREATE TRIGGER trg_schema_migration_receipt_immutable
        BEFORE UPDATE ON schema_migration_receipts
        BEGIN
            SELECT RAISE(ABORT, 'schema migration receipts are immutable');
        END;
        CREATE TRIGGER trg_schema_migration_receipt_no_delete
        BEFORE DELETE ON schema_migration_receipts
        BEGIN
            SELECT RAISE(ABORT, 'schema migration receipts are append-only');
        END;
        INSERT INTO schema_migration_receipts
            (version, component, detail, applied_at)
        VALUES (
            'mac.persona_instance_identity.v1',
            'persona_instances',
            '{"schema":"mac.schema_migration.v1","origin":"broken-test"}',
            'now'
        );
        """
    )
    conn.close()


def test_migration_renames_tables_and_columns(tmp_path):
    path = str(tmp_path / "legacy.db")
    _build_old_schema_db(path)

    store = ephemeral_store()
    try:
        conn = store._conn
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "persona_instances" in tables
        assert "hermes_instances" not in tables

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(platform_bindings)")
        }
        assert "persona_instance_id" in columns
        assert "hermes_instance_id" not in columns

        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_persona_instances_tenant" in indexes
        assert "idx_hermes_instances_tenant" not in indexes
        assert "idx_platform_bindings_instance" in indexes

        instance_foreign_key = next(
            row
            for row in conn.execute("PRAGMA foreign_key_list(platform_bindings)")
            if row["from"] == "persona_instance_id"
        )
        assert instance_foreign_key["table"] == "persona_instances"
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
    finally:
        store.close()


def test_migration_preserves_rows_and_relationships(tmp_path):
    path = str(tmp_path / "legacy.db")
    _build_old_schema_db(path)

    store = ephemeral_store()
    try:
        conn = store._conn
        instances = {
            row["id"]: row
            for row in conn.execute("SELECT * FROM persona_instances")
        }
        assert set(instances) == {"h1", "h2"}
        assert instances["h1"]["name"] == "bot"
        assert instances["h1"]["persona_id"] == "p1"
        assert instances["h1"]["status"] == "active"
        assert instances["h1"]["metadata"] == '{"k":"v"}'
        assert instances["h2"]["persona_id"] is None

        bindings = {
            row["id"]: row
            for row in conn.execute("SELECT * FROM platform_bindings")
        }
        assert set(bindings) == {"b1", "b2"}
        # Relationship survives the column rename.
        assert bindings["b1"]["persona_instance_id"] == "h1"
        assert bindings["b2"]["persona_instance_id"] == "h2"
        assert bindings["b1"]["platform"] == "slack"

        # Service layer reads the migrated data transparently.
        identity = IdentityService(store)
        instance = identity.get_hermes_instance("h1")
        assert isinstance(instance, PersonaInstance)
        assert instance.name == "bot"

        listed = identity.list_platform_bindings(hermes_instance_id="h1")
        assert [binding.id for binding in listed] == ["b1"]
        assert listed[0].persona_instance_id == "h1"
        # Backward-compatible accessor still resolves the FK.
        assert listed[0].hermes_instance_id == "h1"
    finally:
        store.close()


def test_migration_records_immutable_receipt(tmp_path):
    path = str(tmp_path / "legacy.db")
    _build_old_schema_db(path)

    store = ephemeral_store()
    try:
        conn = store._conn
        receipt = conn.execute(
            "SELECT * FROM schema_migration_receipts WHERE version = ?",
            (PERSONA_MIGRATION_VERSION,),
        ).fetchone()
        assert receipt is not None
        assert receipt["component"] == "persona_instances"
        assert "hermes_instances->persona_instances" in receipt["detail"]
        assert receipt["applied_at"]

        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            conn.execute(
                "UPDATE schema_migration_receipts SET component = 'x' "
                "WHERE version = ?",
                (PERSONA_MIGRATION_VERSION,),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            conn.execute(
                "DELETE FROM schema_migration_receipts WHERE version = ?",
                (PERSONA_MIGRATION_VERSION,),
            )
    finally:
        store.close()


def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "legacy.db")
    _build_old_schema_db(path)

    first = ephemeral_store()
    first.close()

    # Reopening an already-migrated database must not error or duplicate work.
    second = ephemeral_store()
    try:
        conn = second._conn
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM schema_migration_receipts WHERE version = ?",
            (PERSONA_MIGRATION_VERSION,),
        ).fetchone()["c"]
        assert count == 1

        instance_count = conn.execute(
            "SELECT COUNT(*) AS c FROM persona_instances"
        ).fetchone()["c"]
        assert instance_count == 2
    finally:
        second.close()


def test_false_v1_receipt_repairs_stranded_foreign_key(tmp_path):
    path = str(tmp_path / "false-receipt.db")
    _build_old_schema_db(path)
    _apply_broken_v1_migration(path)

    before = sqlite3.connect(path)
    try:
        broken_fk = next(
            row
            for row in before.execute("PRAGMA foreign_key_list(platform_bindings)")
            if row[3] == "persona_instance_id"
        )
        assert broken_fk[2] == "hermes_instances"
    finally:
        before.close()

    store = ephemeral_store()
    try:
        conn = store._conn
        repaired_fk = next(
            row
            for row in conn.execute("PRAGMA foreign_key_list(platform_bindings)")
            if row["from"] == "persona_instance_id"
        )
        assert repaired_fk["table"] == "persona_instances"
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM persona_instances"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM platform_bindings"
        ).fetchone()[0] == 2

        repair = conn.execute(
            "SELECT * FROM schema_migration_receipts WHERE version = ?",
            (PERSONA_REPAIR_VERSION,),
        ).fetchone()
        assert repair is not None
        assert '"repair":"legacy-rename-foreign-key"' in repair["detail"]
    finally:
        store.close()


def test_failed_foreign_key_check_does_not_record_success(tmp_path):
    path = str(tmp_path / "orphaned-binding.db")
    _build_old_schema_db(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE platform_bindings SET hermes_instance_id = 'missing' "
            "WHERE id = 'b1'"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StoreError, match="foreign_key_check failed"):
        ephemeral_store()

    conn = sqlite3.connect(path)
    try:
        receipt = conn.execute(
            "SELECT 1 FROM schema_migration_receipts WHERE version = ?",
            (PERSONA_MIGRATION_VERSION,),
        ).fetchone()
        assert receipt is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'hermes_instances'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'persona_instances'"
        ).fetchone() is None
    finally:
        conn.close()


def test_fresh_database_records_receipt_and_uses_new_names(tmp_path):
    path = str(tmp_path / "fresh.db")
    store = ephemeral_store()
    try:
        conn = store._conn
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "persona_instances" in tables
        assert "hermes_instances" not in tables

        receipt = conn.execute(
            "SELECT * FROM schema_migration_receipts WHERE version = ?",
            (PERSONA_MIGRATION_VERSION,),
        ).fetchone()
        assert receipt is not None
        assert '"origin":"fresh-schema"' in receipt["detail"]

        # Round-trip through the identity service on the new schema.
        identity = IdentityService(store)
        tenant = identity.register_tenant("acme")
        instance = identity.register_hermes_instance(tenant.id, "bot")
        binding = identity.register_platform_binding(
            tenant.id, instance.id, "slack", "U1"
        )
        assert binding.persona_instance_id == instance.id
    finally:
        store.close()


def test_hermes_instance_is_persona_instance_alias():
    assert HermesInstance is PersonaInstance
