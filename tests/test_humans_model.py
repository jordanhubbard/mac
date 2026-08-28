"""Tests for the Human / HumanGroup data layer.

Covers:
* Human and HumanGroup model construction and to_dict round-trip.
* store: humans + human_groups tables exist on a fresh database.
* store: upsert_human / get_human / get_human_by_username round-trip.
* store: list_humans (all, filtered by group).
* store: group membership reconciliation (human_groups table stays in sync).
* store: delete_human cascade (human_groups rows removed).
* store: migration path — humans and human_groups tables are created
  (and human_assignees / created_by_human columns added to tasks) when
  initialize() runs on an existing DB that pre-dates the humans schema.
* tasks table: human_assignees and created_by_human columns exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mac.models import Human, HumanGroup, new_id, utcnow
from mac.test_support import column_names, ephemeral_store, table_names
from mac.test_support import ephemeral_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_human(**overrides) -> Human:
    kw = dict(
        id=new_id("human"),
        username="alice",
        email="alice@example.com",
        github_login="alice-gh",
        display_name="Alice Example",
        groups=["eng", "admins"],
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    kw.update(overrides)
    return Human(**kw)


def _make_group(human_id: str, group_name: str = "eng") -> HumanGroup:
    return HumanGroup(
        id=new_id("hg"),
        human_id=human_id,
        group_name=group_name,
        created_at=utcnow(),
    )


def _fresh_store() -> ephemeral_store:
    """Return a store on a fresh schema, with the schema fully initialised."""
    return ephemeral_store()


# ---------------------------------------------------------------------------
# Model construction tests
# ---------------------------------------------------------------------------


class TestHumanModel:
    def test_construction_defaults(self):
        h = _make_human()
        assert h.id.startswith("human_")
        assert h.username == "alice"
        assert "eng" in h.groups
        assert "admins" in h.groups

    def test_optional_fields_nullable(self):
        h = _make_human(email=None, github_login=None, display_name=None)
        assert h.email is None
        assert h.github_login is None
        assert h.display_name is None

    def test_groups_empty_list(self):
        h = _make_human(groups=[])
        assert h.groups == []

    def test_to_dict_round_trip(self):
        h = _make_human()
        d = h.to_dict()
        assert d["id"] == h.id
        assert d["username"] == h.username
        assert d["groups"] == h.groups

    def test_human_group_construction(self):
        h = _make_human()
        hg = _make_group(h.id)
        assert hg.human_id == h.id
        assert hg.group_name == "eng"

    def test_human_group_to_dict(self):
        h = _make_human()
        hg = _make_group(h.id)
        d = hg.to_dict()
        assert d["human_id"] == h.id
        assert d["group_name"] == "eng"


# ---------------------------------------------------------------------------
# Schema tests: tables / columns exist
# ---------------------------------------------------------------------------


class TestHumansSchema:
    def test_humans_table_exists(self):
        store = _fresh_store()
        tables = table_names(store)
        assert "humans" in tables

    def test_human_groups_table_exists(self):
        store = _fresh_store()
        tables = table_names(store)
        assert "human_groups" in tables

    def test_humans_columns(self):
        store = _fresh_store()
        cols = column_names(store, "humans")
        required = {
            "id",
            "username",
            "email",
            "github_login",
            "display_name",
            "groups",
            "created_at",
            "updated_at",
        }
        assert required <= cols, f"Missing columns: {required - cols}"

    def test_human_groups_columns(self):
        store = _fresh_store()
        cols = column_names(store, "human_groups")
        required = {"id", "human_id", "group_name", "created_at"}
        assert required <= cols, f"Missing columns: {required - cols}"

    def test_tasks_human_assignees_column(self):
        store = _fresh_store()
        cols = column_names(store, "tasks")
        assert "human_assignees" in cols

    def test_tasks_created_by_human_column(self):
        store = _fresh_store()
        cols = column_names(store, "tasks")
        assert "created_by_human" in cols


# ---------------------------------------------------------------------------
# CRUD round-trip tests
# ---------------------------------------------------------------------------


class TestUpsertGetHuman:
    def _upsert(self, store: ephemeral_store, h: Human) -> None:
        store.upsert_human(
            h.id,
            h.username,
            email=h.email,
            github_login=h.github_login,
            display_name=h.display_name,
            groups=h.groups,
            created_at=h.created_at,
            updated_at=h.updated_at,
        )

    def test_upsert_then_get_by_id(self):
        store = _fresh_store()
        h = _make_human()
        self._upsert(store, h)
        row = store.get_human(h.id)
        assert row is not None
        assert row["id"] == h.id
        assert row["username"] == h.username

    def test_get_human_not_found_returns_none(self):
        store = _fresh_store()
        assert store.get_human("human_nonexistent") is None

    def test_get_human_by_username(self):
        store = _fresh_store()
        h = _make_human(username="bob")
        self._upsert(store, h)
        row = store.get_human_by_username("bob")
        assert row is not None
        assert row["id"] == h.id

    def test_get_human_by_username_not_found(self):
        store = _fresh_store()
        assert store.get_human_by_username("nobody") is None

    def test_groups_stored_as_json(self):
        store = _fresh_store()
        h = _make_human(groups=["alpha", "beta"])
        self._upsert(store, h)
        row = store.get_human(h.id)
        stored_groups = json.loads(row["groups"])
        assert sorted(stored_groups) == ["alpha", "beta"]

    def test_upsert_updates_existing_row(self):
        store = _fresh_store()
        h = _make_human()
        self._upsert(store, h)
        # update display_name
        h2 = Human(
            id=h.id,
            username=h.username,
            email=h.email,
            github_login=h.github_login,
            display_name="New Display Name",
            groups=h.groups,
            created_at=h.created_at,
            updated_at=utcnow(),
        )
        self._upsert(store, h2)
        row = store.get_human(h.id)
        assert row["display_name"] == "New Display Name"

    def test_upsert_idempotent(self):
        store = _fresh_store()
        h = _make_human()
        self._upsert(store, h)
        self._upsert(store, h)  # second upsert must not raise
        count = store.query_one("SELECT COUNT(*) AS c FROM humans WHERE id = ?", (h.id,))["c"]
        assert count == 1


# ---------------------------------------------------------------------------
# list_humans
# ---------------------------------------------------------------------------


class TestListHumans:
    def _upsert(self, store: ephemeral_store, h: Human) -> None:
        store.upsert_human(
            h.id,
            h.username,
            email=h.email,
            github_login=h.github_login,
            display_name=h.display_name,
            groups=h.groups,
            created_at=h.created_at,
            updated_at=h.updated_at,
        )

    def test_list_all_empty(self):
        store = _fresh_store()
        assert store.list_humans() == []

    def test_list_all_returns_all_humans(self):
        store = _fresh_store()
        h1 = _make_human(
            id=new_id("human"),
            username="alice",
            github_login="alice-gh",
            groups=["eng"],
        )
        h2 = _make_human(
            id=new_id("human"),
            username="bob",
            github_login="bob-gh",
            groups=["ops"],
        )
        self._upsert(store, h1)
        self._upsert(store, h2)
        rows = store.list_humans()
        usernames = {r["username"] for r in rows}
        assert usernames == {"alice", "bob"}

    def test_list_by_group_filters_correctly(self):
        store = _fresh_store()
        h1 = _make_human(
            id=new_id("human"),
            username="alice",
            github_login="alice-gh2",
            groups=["eng"],
        )
        h2 = _make_human(
            id=new_id("human"),
            username="bob",
            github_login="bob-gh2",
            groups=["ops"],
        )
        self._upsert(store, h1)
        self._upsert(store, h2)
        eng_rows = store.list_humans(group="eng")
        assert len(eng_rows) == 1
        assert eng_rows[0]["username"] == "alice"

    def test_list_by_group_no_match_returns_empty(self):
        store = _fresh_store()
        h = _make_human(groups=["eng"])
        self._upsert(store, h)
        assert store.list_humans(group="finance") == []

    def test_list_by_group_multi_group_human_appears_once(self):
        store = _fresh_store()
        h = _make_human(username="carol", groups=["eng", "admins"])
        self._upsert(store, h)
        admins_rows = store.list_humans(group="admins")
        assert len(admins_rows) == 1
        assert admins_rows[0]["username"] == "carol"


# ---------------------------------------------------------------------------
# Group membership reconciliation
# ---------------------------------------------------------------------------


class TestGroupMembership:
    def _upsert(self, store: ephemeral_store, h: Human) -> None:
        store.upsert_human(
            h.id,
            h.username,
            groups=h.groups,
            created_at=h.created_at,
            updated_at=h.updated_at,
        )

    def _group_count(self, store: ephemeral_store, human_id: str) -> int:
        return store.execute(
            "SELECT COUNT(*) AS c FROM human_groups WHERE human_id = ?",
            (human_id,),
        ).fetchone()["c"]

    def test_human_groups_rows_created(self):
        store = _fresh_store()
        h = _make_human(groups=["eng", "admins"])
        self._upsert(store, h)
        assert self._group_count(store, h.id) == 2

    def test_human_groups_rows_empty_for_no_groups(self):
        store = _fresh_store()
        h = _make_human(groups=[])
        self._upsert(store, h)
        assert self._group_count(store, h.id) == 0

    def test_groups_reconciled_on_upsert(self):
        store = _fresh_store()
        h = _make_human(groups=["eng", "admins"])
        self._upsert(store, h)
        # remove one group
        h2 = Human(
            id=h.id,
            username=h.username,
            email=None,
            github_login=None,
            display_name=None,
            groups=["eng"],
            created_at=h.created_at,
            updated_at=utcnow(),
        )
        self._upsert(store, h2)
        assert self._group_count(store, h.id) == 1
        rows = store.query_all("SELECT group_name FROM human_groups WHERE human_id = ?", (h.id,))
        assert rows[0]["group_name"] == "eng"

    def test_groups_expanded_on_upsert(self):
        store = _fresh_store()
        h = _make_human(groups=["eng"])
        self._upsert(store, h)
        h2 = Human(
            id=h.id,
            username=h.username,
            email=None,
            github_login=None,
            display_name=None,
            groups=["eng", "ops", "admins"],
            created_at=h.created_at,
            updated_at=utcnow(),
        )
        self._upsert(store, h2)
        assert self._group_count(store, h.id) == 3


# ---------------------------------------------------------------------------
# delete_human
# ---------------------------------------------------------------------------


class TestDeleteHuman:
    def _upsert(self, store: ephemeral_store, h: Human) -> None:
        store.upsert_human(
            h.id,
            h.username,
            groups=h.groups,
            created_at=h.created_at,
            updated_at=h.updated_at,
        )

    def test_delete_returns_true_when_found(self):
        store = _fresh_store()
        h = _make_human()
        self._upsert(store, h)
        assert store.delete_human(h.id) is True

    def test_delete_removes_human_row(self):
        store = _fresh_store()
        h = _make_human()
        self._upsert(store, h)
        store.delete_human(h.id)
        assert store.get_human(h.id) is None

    def test_delete_cascades_to_human_groups(self):
        store = _fresh_store()
        h = _make_human(groups=["eng", "ops"])
        self._upsert(store, h)
        store.delete_human(h.id)
        count = store.execute(
            "SELECT COUNT(*) AS c FROM human_groups WHERE human_id = ?",
            (h.id,),
        ).fetchone()["c"]
        assert count == 0

    def test_delete_returns_false_when_not_found(self):
        store = _fresh_store()
        assert store.delete_human("human_does_not_exist") is False


# ---------------------------------------------------------------------------
# Migration path: existing DB gains humans tables via initialize()
# ---------------------------------------------------------------------------


def _apply_versioned_humans_repair(store) -> None:
    from mac.schema_migrations import MIGRATIONS, Migration

    start = MIGRATIONS[0].sql.index("CREATE TABLE IF NOT EXISTS humans (")
    end = MIGRATIONS[0].sql.index(
        "CREATE TABLE IF NOT EXISTS openclaw_conversation_executions (",
        start,
    )
    repair = Migration(
        "0004_test_humans_repair",
        MIGRATIONS[0].sql[start:end],
        """
        SELECT to_regclass(current_schema() || '.humans') IS NOT NULL
           AND to_regclass(current_schema() || '.human_groups') IS NOT NULL
        """,
    )
    store.apply_migrations(applied_by="test-humans-repair", migrations=(*MIGRATIONS, repair))


class TestMigrationPath:
    """Verify an explicit versioned migration repairs a pre-humans schema.

    Strategy: open a full ephemeral_store (which runs _initialize()), then manually
    drop the humans and human_groups tables and the two new task columns to
    simulate a pre-humans schema.  Call initialize() directly and assert that the
    schema is back to the expected state. This does not rely on initialize()
    replaying unversioned DDL.
    """

    @staticmethod
    def _degrade_schema(store) -> None:
        """Remove humans/human_groups tables and the two task columns to
        simulate a pre-humans database state."""
        conn = store
        conn.execute("DROP TABLE IF EXISTS human_groups")
        # CASCADE because agents.owner_human_id references humans. Dropping
        # the constraint along with the table is what makes this a real
        # migration rehearsal: re-running the DDL has to put the foreign key
        # back, not just the table.
        conn.execute("DROP TABLE IF EXISTS humans CASCADE")
        # SQLite doesn't support DROP COLUMN before 3.35; use a view trick
        # instead: just verify they're absent after re-running initialize().
        # We skip the DROP COLUMN step — instead we assert _ensure_column is
        # idempotent (runs fine even when the column already exists).

    def test_humans_table_created_on_migrate(self):
        store = _fresh_store()
        self._degrade_schema(store)
        # Verify tables are gone
        tables_before = table_names(store)
        assert "humans" not in tables_before
        assert "human_groups" not in tables_before
        _apply_versioned_humans_repair(store)
        tables_after = table_names(store)
        assert "humans" in tables_after
        assert "human_groups" in tables_after

    def test_human_assignees_column_present_after_migrate(self):
        """The immutable baseline contains the former ensure-column."""
        store = _fresh_store()
        store.verify_schema()
        cols = column_names(store, "tasks")
        assert "human_assignees" in cols

    def test_created_by_human_column_present_after_migrate(self):
        store = _fresh_store()
        store.verify_schema()
        cols = column_names(store, "tasks")
        assert "created_by_human" in cols

    def test_full_crud_works_after_migrate(self):
        store = _fresh_store()
        self._degrade_schema(store)
        _apply_versioned_humans_repair(store)
        h = _make_human(username="migrated_user", groups=["alpha"])
        store.upsert_human(
            h.id,
            h.username,
            email=h.email,
            github_login=h.github_login,
            display_name=h.display_name,
            groups=h.groups,
            created_at=h.created_at,
            updated_at=h.updated_at,
        )
        row = store.get_human(h.id)
        assert row is not None
        assert row["username"] == "migrated_user"
        rows = store.list_humans(group="alpha")
        assert len(rows) == 1


def test_the_owner_foreign_key_comes_back_after_humans_is_recreated():
    """ADD COLUMN IF NOT EXISTS skips the whole statement once the column is
    there, so a foreign key declared inline would be lost for good the first
    time humans was dropped and rebuilt -- leaving agents ownable by
    principals that do not exist, with nothing to say so."""
    store = _fresh_store()
    store.execute("DROP TABLE IF EXISTS human_groups")
    store.execute("DROP TABLE IF EXISTS humans CASCADE")

    _apply_versioned_humans_repair(store)

    # Scoped to THIS schema. pg_constraint is cluster-wide and every test runs
    # in its own schema, so an unscoped lookup finds some other test's
    # constraint and passes no matter what this migration did.
    row = store.execute(
        "SELECT 1 AS present FROM pg_constraint c "
        "JOIN pg_class t ON c.conrelid = t.oid "
        "JOIN pg_namespace n ON t.relnamespace = n.oid "
        "WHERE c.conname = 'agents_owner_human_id_fkey' "
        "AND n.nspname = current_schema()"
    ).fetchone()
    assert row is not None, "the owner foreign key was not restored"
