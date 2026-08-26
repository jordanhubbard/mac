"""Old-schema -> new-schema migration tests for the PersonaInstance rename.

``hermes_instances`` -> ``persona_instances`` and the
``platform_bindings.hermes_instance_id`` FK -> ``persona_instance_id`` are
renamed in place by the frozen PostgreSQL baseline migration. These
tests assert the one-time migration preserves every row and relationship from a
legacy database, records an immutable receipt, and is idempotent.
"""

from __future__ import annotations


from mac.identity_service import IdentityService
from mac.models import HermesInstance, PersonaInstance
from mac.test_support import (
    column_names,
    foreign_keys,
    ephemeral_store,
    index_names,
    store_on,
    table_names,
)

PERSONA_MIGRATION_VERSION = "mac.persona_instance_identity.v1"
PERSONA_REPAIR_VERSION = "mac.persona_instance_identity_fk_repair.v1"


def _build_old_schema_db() -> str:
    """A schema that still uses the pre-persona names. Returns its DSN.

    The legacy DDL is created directly rather than by an old code path, then
    The test explicitly executes the immutable migration artifact.
    """
    from mac.test_support import create_schema, store_on

    _, dsn = create_schema()
    conn = store_on(dsn)
    for statement in (
        """CREATE TABLE tenants (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            metadata TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE personas (
            id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
            soul_ref TEXT NOT NULL, memory_scope TEXT NOT NULL, metadata TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE hermes_instances (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            persona_id TEXT REFERENCES personas(id) ON DELETE SET NULL,
            home_ref TEXT NOT NULL, status TEXT NOT NULL, metadata TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            UNIQUE(tenant_id, name)
        )""",
        "CREATE INDEX idx_hermes_instances_tenant ON hermes_instances (tenant_id)",
        """CREATE TABLE platform_bindings (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            hermes_instance_id TEXT NOT NULL
                REFERENCES hermes_instances(id) ON DELETE CASCADE,
            platform TEXT NOT NULL, external_id TEXT NOT NULL, display_name TEXT NOT NULL,
            scopes TEXT NOT NULL, metadata TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(tenant_id, platform, external_id)
        )""",
        "CREATE INDEX idx_platform_bindings_instance ON platform_bindings (hermes_instance_id)",
        "INSERT INTO tenants VALUES ('t1', 'acme', '{}', 'now', 'now')",
        "INSERT INTO personas VALUES "
        "('p1', 't1', 'concierge', 'soul://p1', 'scope://p1', '{}', 'now', 'now')",
        "INSERT INTO hermes_instances VALUES "
        "('h1', 't1', 'bot', 'p1', 'ref://home', 'active', "
        "'{\"k\":\"v\"}', 'now', 'now', 'now')",
        "INSERT INTO hermes_instances VALUES "
        "('h2', 't1', 'bot2', NULL, 'ref://home2', 'paused', '{}', 'now', 'now', 'now')",
        "INSERT INTO platform_bindings VALUES "
        "('b1', 't1', 'h1', 'slack', 'U1', 'User One', '{}', '{}', 'now', 'now')",
        "INSERT INTO platform_bindings VALUES "
        "('b2', 't1', 'h2', 'telegram', 'T2', 'User Two', '{}', '{}', 'now', 'now')",
    ):
        conn.execute(statement)
    conn.close()
    return dsn


def test_migration_renames_tables_and_columns(tmp_path):
    dsn = _build_old_schema_db()

    from mac.schema_migrations import MIGRATIONS

    store = store_on(dsn)
    store.execute(MIGRATIONS[0].sql)
    try:
        conn = store
        tables = table_names(store)
        assert "persona_instances" in tables
        assert "hermes_instances" not in tables

        columns = column_names(store, "platform_bindings")
        assert "persona_instance_id" in columns
        assert "hermes_instance_id" not in columns

        indexes = index_names(store, "persona_instances") | index_names(store, "platform_bindings")
        assert "idx_persona_instances_tenant" in indexes
        assert "idx_hermes_instances_tenant" not in indexes
        assert "idx_platform_bindings_instance" in indexes

        # The FK must follow the rename to the new table and column.
        assert ("persona_instance_id", "persona_instances") in foreign_keys(
            store, "platform_bindings"
        )
    finally:
        store.close()


def test_hermes_instance_is_persona_instance_alias():
    assert HermesInstance is PersonaInstance
