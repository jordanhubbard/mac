"""Tests for source_releases and fleet_desired_source_states persistence.

Covers:
 * SQLite table creation on a fresh database (via SQLiteStore).
 * SQLite upgrade: tables are created on an existing database that lacks them.
 * Model validation: commit_sha immutability, branch-ref rejection, generation
   monotonicity, scope-exclusivity.
 * Idempotency record uniqueness.
 * Postgres schema file: tables appear in the DDL.
 * Postgres translation shim: new table names round-trip through placeholder
   translation unmodified.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from mac.models import (
    DesiredSourceIdempotencyRecord,
    DesiredSourcePolicy,
    DesiredSourceTransition,
    FleetDesiredSourceState,
    SourceRelease,
    ValidationError,
    new_id,
    utcnow,
)
from mac.store import SQLiteStore, StoreError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GOOD_SHA = "a" * 40


def _good_release(**overrides) -> SourceRelease:
    kw = dict(
        id=new_id("release"),
        repository_id="projectrepo_abc",
        repository_name="mac",
        canonical_remote_url="git@github.com:org/mac.git",
        commit_sha=GOOD_SHA,
        canonical_ref="refs/tags/v1.0.0",
        tree_digest="sha256:" + "b" * 64,
        artifact_digest=None,
        image_digest=None,
        created_by="agent_test",
        created_by_task_id=None,
        review_evidence_id=None,
        publication_evidence_id=None,
        status="draft",
        metadata={},
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    kw.update(overrides)
    return SourceRelease(**kw)


def _good_desired_state(release_id: str, **overrides) -> FleetDesiredSourceState:
    kw = dict(
        id=new_id("dss"),
        fleet_id="fleet_abc",
        environment_id=None,
        generation=1,
        release_id=release_id,
        rollout_policy="immediate",
        actor="agent_test",
        reason="initial",
        prior_generation=None,
        paused=False,
        request_id="req_abc",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    kw.update(overrides)
    return FleetDesiredSourceState(**kw)


def _store_release(db: SQLiteStore, rel: SourceRelease) -> None:
    db.execute(
        """
        INSERT INTO source_releases (
            id, repository_id, repository_name, canonical_remote_url,
            commit_sha, canonical_ref, tree_digest, artifact_digest,
            image_digest, created_by, created_by_task_id,
            review_evidence_id, publication_evidence_id,
            status, metadata, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            rel.id, rel.repository_id, rel.repository_name,
            rel.canonical_remote_url, rel.commit_sha, rel.canonical_ref,
            rel.tree_digest, rel.artifact_digest, rel.image_digest,
            rel.created_by, rel.created_by_task_id,
            rel.review_evidence_id, rel.publication_evidence_id,
            rel.status, "{}", rel.created_at, rel.updated_at,
        ),
    )


def _ensure_fleet(db: SQLiteStore, fleet_id: str = "fleet_abc") -> None:
    """Insert a minimal machines + agents + fleets row so FK constraints pass."""
    now = utcnow()
    # machines row needed by agents FK
    db.execute(
        """
        INSERT OR IGNORE INTO machines
        (id, hostname, labels, resources, trusted, created_at, updated_at, last_seen_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        ("machine_test", "testhost", "[]", "{}", 1, now, now, now),
    )
    db.execute(
        """
        INSERT OR IGNORE INTO fleets
        (id, name, description, status, metadata, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (fleet_id, "test-fleet", "test fleet", "active", "{}", now, now),
    )


def _store_desired(db: SQLiteStore, dss: FleetDesiredSourceState) -> None:
    if dss.fleet_id is not None:
        _ensure_fleet(db, dss.fleet_id)
    db.execute(
        """
        INSERT INTO fleet_desired_source_states (
            id, fleet_id, environment_id, generation, release_id,
            rollout_policy, actor, reason, prior_generation,
            paused, request_id, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            dss.id, dss.fleet_id, dss.environment_id, dss.generation,
            dss.release_id, dss.rollout_policy, dss.actor, dss.reason,
            dss.prior_generation, int(dss.paused), dss.request_id,
            dss.created_at, dss.updated_at,
        ),
    )


# ---------------------------------------------------------------------------
# Fresh-database table creation
# ---------------------------------------------------------------------------

class TestFreshDatabaseTables:
    def test_source_releases_table_exists(self) -> None:
        db = SQLiteStore(":memory:")
        tables = {
            r["name"] for r in db.query_all(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "source_releases" in tables
        db.close()

    def test_fleet_desired_source_states_table_exists(self) -> None:
        db = SQLiteStore(":memory:")
        tables = {
            r["name"] for r in db.query_all(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "fleet_desired_source_states" in tables
        db.close()

    def test_fleet_desired_source_transitions_table_exists(self) -> None:
        db = SQLiteStore(":memory:")
        tables = {
            r["name"] for r in db.query_all(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "fleet_desired_source_transitions" in tables
        db.close()

    def test_fleet_desired_source_idempotency_table_exists(self) -> None:
        db = SQLiteStore(":memory:")
        tables = {
            r["name"] for r in db.query_all(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "fleet_desired_source_idempotency" in tables
        db.close()

    def test_source_releases_expected_columns(self) -> None:
        db = SQLiteStore(":memory:")
        cols = {
            r["name"] for r in db.query_all("PRAGMA table_info(source_releases)")
        }
        expected = {
            "id", "repository_id", "repository_name", "canonical_remote_url",
            "commit_sha", "canonical_ref", "tree_digest",
            "artifact_digest", "image_digest", "created_by",
            "created_by_task_id", "review_evidence_id",
            "publication_evidence_id", "status", "metadata",
            "created_at", "updated_at",
        }
        assert expected.issubset(cols)
        db.close()

    def test_fleet_desired_source_states_expected_columns(self) -> None:
        db = SQLiteStore(":memory:")
        cols = {
            r["name"] for r in db.query_all(
                "PRAGMA table_info(fleet_desired_source_states)"
            )
        }
        expected = {
            "id", "fleet_id", "environment_id", "generation", "release_id",
            "rollout_policy", "actor", "reason", "prior_generation",
            "paused", "request_id", "created_at", "updated_at",
        }
        assert expected.issubset(cols)
        db.close()

    def test_indexes_created(self) -> None:
        db = SQLiteStore(":memory:")
        indexes = {
            r["name"] for r in db.query_all(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_source_releases_repo_status" in indexes
        assert "idx_source_releases_status_created" in indexes
        assert "idx_fleet_desired_source_fleet" in indexes
        assert "idx_fleet_desired_source_env" in indexes
        assert "idx_fleet_desired_source_transitions_state" in indexes
        assert "idx_fleet_desired_source_idempotency_scope" in indexes
        db.close()


# ---------------------------------------------------------------------------
# SQLite upgrade: database created without the new tables gets them on open
# ---------------------------------------------------------------------------

class TestSQLiteUpgrade:
    def test_upgrade_adds_source_releases_to_existing_db(self, tmp_path) -> None:
        legacy = tmp_path / "legacy.sqlite"
        conn = sqlite3.connect(legacy)
        # Minimal schema: just tasks (needed for FK references inside migrate)
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                project TEXT,
                priority INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL,
                required_capabilities TEXT NOT NULL,
                dependencies TEXT NOT NULL,
                metadata TEXT NOT NULL,
                owner_agent_id TEXT,
                lease_id TEXT,
                leased_until TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE evidence (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                uri TEXT NOT NULL,
                summary TEXT NOT NULL,
                checksum TEXT,
                metadata TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE fleets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                metadata TEXT NOT NULL DEFAULT '{}',
                tenant_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE environments (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                tenant_id TEXT,
                channel TEXT NOT NULL DEFAULT 'fleet',
                promotes_from TEXT,
                metadata TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, name)
            );
            """
        )
        conn.commit()
        conn.close()

        # Opening with SQLiteStore should trigger _migrate and add the tables.
        upgraded = SQLiteStore(str(legacy))
        tables = {
            r["name"] for r in upgraded.query_all(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "source_releases" in tables
        assert "fleet_desired_source_states" in tables
        assert "fleet_desired_source_transitions" in tables
        assert "fleet_desired_source_idempotency" in tables
        upgraded.close()

    def test_upgrade_is_idempotent(self, tmp_path) -> None:
        """Opening the same upgraded DB twice does not raise."""
        db_path = str(tmp_path / "idem.sqlite")
        db1 = SQLiteStore(db_path)
        db1.close()
        db2 = SQLiteStore(db_path)
        tables = {
            r["name"] for r in db2.query_all(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "source_releases" in tables
        db2.close()


# ---------------------------------------------------------------------------
# Model validation (commit_sha immutability, branch-ref rejection, etc.)
# ---------------------------------------------------------------------------

class TestSourceReleaseModel:
    def test_valid_release_constructs(self) -> None:
        rel = _good_release()
        assert rel.commit_sha == GOOD_SHA

    def test_invalid_sha_raises(self) -> None:
        with pytest.raises(ValidationError, match="commit_sha"):
            _good_release(commit_sha="not-a-sha")

    def test_short_sha_raises(self) -> None:
        with pytest.raises(ValidationError, match="commit_sha"):
            _good_release(commit_sha="abc123")

    def test_sha_with_uppercase_raises(self) -> None:
        # Must be lowercase hex
        with pytest.raises(ValidationError, match="commit_sha"):
            _good_release(commit_sha="A" * 40)

    def test_branch_ref_rejected(self) -> None:
        with pytest.raises(ValidationError, match="canonical_ref"):
            _good_release(canonical_ref="refs/heads/main")

    def test_tag_ref_accepted(self) -> None:
        rel = _good_release(canonical_ref="refs/tags/v2.0.0")
        assert rel.canonical_ref == "refs/tags/v2.0.0"

    def test_bare_sha_ref_accepted(self) -> None:
        rel = _good_release(canonical_ref=GOOD_SHA)
        assert rel.canonical_ref == GOOD_SHA

    def test_to_dict_round_trips(self) -> None:
        rel = _good_release()
        d = rel.to_dict()
        assert d["commit_sha"] == GOOD_SHA
        assert d["status"] == "draft"


class TestFleetDesiredSourceStateModel:
    def test_valid_fleet_scope(self) -> None:
        dss = _good_desired_state(release_id="release_abc", fleet_id="fleet_abc")
        assert dss.generation == 1

    def test_valid_env_scope(self) -> None:
        dss = _good_desired_state(
            release_id="release_abc",
            fleet_id=None,
            environment_id="env_abc",
        )
        assert dss.environment_id == "env_abc"

    def test_no_scope_raises(self) -> None:
        with pytest.raises(ValidationError, match="fleet_id or environment_id"):
            _good_desired_state(
                release_id="release_abc",
                fleet_id=None,
                environment_id=None,
            )

    def test_generation_zero_raises(self) -> None:
        with pytest.raises(ValidationError, match="generation"):
            _good_desired_state(release_id="r", generation=0)

    def test_generation_negative_raises(self) -> None:
        with pytest.raises(ValidationError, match="generation"):
            _good_desired_state(release_id="r", generation=-5)

    def test_to_dict_contains_generation(self) -> None:
        dss = _good_desired_state(release_id="release_abc")
        assert dss.to_dict()["generation"] == 1


# ---------------------------------------------------------------------------
# Storage-layer constraints (SQLite enforcement)
# ---------------------------------------------------------------------------

class TestSQLiteConstraints:
    @pytest.fixture()
    def db(self):
        store = SQLiteStore(":memory:")
        yield store
        store.close()

    def _insert_release(self, db: SQLiteStore, sha: str = GOOD_SHA) -> str:
        rel = _good_release(commit_sha=sha)
        _store_release(db, rel)
        return rel.id

    def test_roundtrip_release(self, db: SQLiteStore) -> None:
        rel_id = self._insert_release(db)
        row = db.query_one(
            "SELECT id, commit_sha FROM source_releases WHERE id = ?", (rel_id,)
        )
        assert row is not None
        assert row["commit_sha"] == GOOD_SHA

    def test_duplicate_repo_sha_rejected(self, db: SQLiteStore) -> None:
        self._insert_release(db, GOOD_SHA)
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            # Same repository_id + commit_sha
            rel2 = _good_release()  # same defaults including repository_id
            _store_release(db, rel2)

    def test_sha_immutability_trigger(self, db: SQLiteStore) -> None:
        rel_id = self._insert_release(db)
        new_sha = "b" * 40
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="immutable"):
            db.execute(
                "UPDATE source_releases SET commit_sha = ? WHERE id = ?",
                (new_sha, rel_id),
            )

    def test_branch_ref_check_constraint(self, db: SQLiteStore) -> None:
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            db.execute(
                """
                INSERT INTO source_releases
                (id, repository_id, repository_name, canonical_remote_url,
                 commit_sha, canonical_ref, tree_digest, created_by,
                 status, metadata, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("rel"), "projectrepo_abc", "mac",
                    "git@github.com:org/mac.git", GOOD_SHA,
                    "refs/heads/main",  # << branch ref, must be rejected
                    "sha256:" + "c" * 64, "agent_test",
                    "draft", "{}", utcnow(), utcnow(),
                ),
            )

    def test_generation_monotonicity_trigger(self, db: SQLiteStore) -> None:
        rel_id = self._insert_release(db)
        dss = _good_desired_state(release_id=rel_id)
        _store_desired(db, dss)
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="monotonically"):
            db.execute(
                "UPDATE fleet_desired_source_states SET generation = 1 WHERE id = ?",
                (dss.id,),
            )

    def test_generation_monotonicity_allows_increase(self, db: SQLiteStore) -> None:
        rel_id = self._insert_release(db)
        dss = _good_desired_state(release_id=rel_id)
        _store_desired(db, dss)
        db.execute(
            "UPDATE fleet_desired_source_states SET generation = 2 WHERE id = ?",
            (dss.id,),
        )
        row = db.query_one(
            "SELECT generation FROM fleet_desired_source_states WHERE id = ?",
            (dss.id,),
        )
        assert row["generation"] == 2

    def test_scope_uniqueness_per_fleet(self, db: SQLiteStore) -> None:
        rel_id = self._insert_release(db)
        dss1 = _good_desired_state(release_id=rel_id, fleet_id="fleet_x")
        _store_desired(db, dss1)
        dss2 = _good_desired_state(
            release_id=rel_id,
            fleet_id="fleet_x",
            id=new_id("dss"),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            _store_desired(db, dss2)

    def test_idempotency_unique_constraint(self, db: SQLiteStore) -> None:
        rel_id = self._insert_release(db)
        dss = _good_desired_state(release_id=rel_id)
        _store_desired(db, dss)
        db.execute(
            """
            INSERT INTO fleet_desired_source_idempotency
            (id, scope_key, request_id, desired_source_state_id, generation, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (new_id("idem"), "fleet:fleet_abc", "req_001", dss.id, 1, utcnow()),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            db.execute(
                """
                INSERT INTO fleet_desired_source_idempotency
                (id, scope_key, request_id, desired_source_state_id, generation, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (new_id("idem"), "fleet:fleet_abc", "req_001", dss.id, 2, utcnow()),
            )

    def test_transition_append(self, db: SQLiteStore) -> None:
        rel_id = self._insert_release(db)
        dss = _good_desired_state(release_id=rel_id)
        _store_desired(db, dss)
        db.execute(
            """
            INSERT INTO fleet_desired_source_transitions
            (id, desired_source_state_id, from_generation, to_generation,
             release_id, rollout_policy, actor, reason, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (new_id("tr"), dss.id, None, 1, rel_id, "immediate", "agent_test",
             "initial", utcnow()),
        )
        rows = db.query_all(
            "SELECT to_generation FROM fleet_desired_source_transitions"
            " WHERE desired_source_state_id = ?", (dss.id,)
        )
        assert len(rows) == 1
        assert rows[0]["to_generation"] == 1


# ---------------------------------------------------------------------------
# Postgres schema file: ensure new tables are present in the DDL
# ---------------------------------------------------------------------------

class TestPostgresSchema:
    @pytest.fixture(scope="class")
    def schema_text(self) -> str:
        path = (
            Path(__file__).resolve().parent.parent
            / "src" / "mac" / "data" / "postgres" / "schema.sql"
        )
        return path.read_text()

    def test_source_releases_table_in_schema(self, schema_text: str) -> None:
        assert "CREATE TABLE IF NOT EXISTS source_releases" in schema_text

    def test_fleet_desired_source_states_in_schema(self, schema_text: str) -> None:
        assert "CREATE TABLE IF NOT EXISTS fleet_desired_source_states" in schema_text

    def test_fleet_desired_source_transitions_in_schema(self, schema_text: str) -> None:
        assert "CREATE TABLE IF NOT EXISTS fleet_desired_source_transitions" in schema_text

    def test_fleet_desired_source_idempotency_in_schema(self, schema_text: str) -> None:
        assert "CREATE TABLE IF NOT EXISTS fleet_desired_source_idempotency" in schema_text

    def test_sha_immutability_trigger_in_schema(self, schema_text: str) -> None:
        assert "trg_source_releases_sha_immutable" in schema_text

    def test_generation_monotonicity_trigger_in_schema(self, schema_text: str) -> None:
        assert "trg_fleet_desired_source_gen_monotonic" in schema_text

    def test_partial_unique_index_fleet_in_schema(self, schema_text: str) -> None:
        assert "uniq_fleet_desired_source_fleet" in schema_text

    def test_partial_unique_index_env_in_schema(self, schema_text: str) -> None:
        assert "uniq_fleet_desired_source_env" in schema_text

    def test_sha_check_constraint_in_schema(self, schema_text: str) -> None:
        # Postgres uses ~ regex operator instead of SQLite GLOB
        assert "~ '^[0-9a-f]+$'" in schema_text

    def test_branch_ref_check_constraint_in_schema(self, schema_text: str) -> None:
        assert "canonical_ref NOT LIKE 'refs/heads/%'" in schema_text


# ---------------------------------------------------------------------------
# Postgres translation shim: new table names pass through unchanged
# ---------------------------------------------------------------------------

class TestPostgresTranslation:
    @pytest.fixture(autouse=True)
    def _skip_if_no_psycopg(self) -> None:
        pytest.importorskip("psycopg")

    def test_source_releases_insert_translates(self) -> None:
        from mac.store_postgres import _translate_placeholders

        sql = (
            "INSERT INTO source_releases "
            "(id, repository_id, commit_sha) VALUES (?, ?, ?)"
        )
        translated = _translate_placeholders(sql)
        assert "source_releases" in translated
        assert "?" not in translated
        assert translated.count("%s") == 3

    def test_fleet_desired_source_states_select_translates(self) -> None:
        from mac.store_postgres import _translate_placeholders

        sql = (
            "SELECT id, generation FROM fleet_desired_source_states "
            "WHERE fleet_id = ? AND generation > ?"
        )
        translated = _translate_placeholders(sql)
        assert "fleet_desired_source_states" in translated
        assert translated.count("%s") == 2

    def test_idempotency_upsert_translates(self) -> None:
        from mac.store_postgres import _translate_placeholders

        sql = (
            "INSERT OR IGNORE INTO fleet_desired_source_idempotency "
            "(id, scope_key, request_id, desired_source_state_id, generation, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        translated = _translate_placeholders(sql)
        assert "fleet_desired_source_idempotency" in translated
        assert translated.count("%s") == 6

    def test_transition_insert_translates(self) -> None:
        from mac.store_postgres import _translate_placeholders

        sql = (
            "INSERT INTO fleet_desired_source_transitions "
            "(id, desired_source_state_id, from_generation, to_generation, "
            "release_id, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        translated = _translate_placeholders(sql)
        assert "fleet_desired_source_transitions" in translated
        assert translated.count("%s") == 7
