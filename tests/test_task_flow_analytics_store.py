"""Tests for task-flow analytics models and durable storage.

Covers the models + storage + schema layer added for transition-derived
task-flow KPIs (mac.task_flow_span.v1 / mac.task_completion.v1):

 * Model validation: canonical stage vocabulary, attempt/duration bounds,
   per-stage-duration key validation.
 * SQLite table/index creation on a fresh database and on an existing database
   that predates these tables (via _migrate).
 * Store helper UPSERT idempotency: a recompute with the same key updates in
   place rather than appending.
 * Aggregate window query for KPI reporting.
 * Postgres schema file: the new tables/indexes appear in the bundled DDL.
 * Postgres translation shim: new table names round-trip unmodified.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mac.models import (
    TASK_FLOW_STAGES,
    TASK_FLOW_STAGE_NAMES,
    TaskCompletion,
    TaskFlowOutcome,
    TaskFlowSpan,
    TaskFlowStage,
    ValidationError,
    new_id,
    utcnow,
)
from mac.store import Store
from mac.test_support import all_index_names, column_names, ephemeral_store, table_names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _good_span(**overrides) -> TaskFlowSpan:
    kw = dict(
        id=new_id("span"),
        task_id="task_abc",
        project="mac",
        attempt=1,
        stage=TaskFlowStage.EXECUTION.value,
        started_at=utcnow(),
        ended_at=utcnow(),
        duration_seconds=12.5,
        outcome=TaskFlowOutcome.COMPLETED.value,
        metadata={},
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    kw.update(overrides)
    return TaskFlowSpan(**kw)


def _good_completion(**overrides) -> TaskCompletion:
    kw = dict(
        id=new_id("taskcompletion"),
        task_id="task_abc",
        project="mac",
        attempt=1,
        started_at=utcnow(),
        ended_at=utcnow(),
        duration_seconds=120.0,
        outcome=TaskFlowOutcome.COMPLETED.value,
        publication_sha="a" * 40,
        main_sha="b" * 40,
        route_count=2,
        token_count=1000,
        cost_count=0.42,
        review_count=1,
        rebase_count=0,
        test_count=3,
        per_stage_durations={TaskFlowStage.EXECUTION.value: 12.5},
        metadata={},
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    kw.update(overrides)
    return TaskCompletion(**kw)


# ---------------------------------------------------------------------------
# Stage vocabulary
# ---------------------------------------------------------------------------


class TestStageVocabulary:
    def test_all_expected_stages_enumerated(self) -> None:
        expected = {
            "intake",
            "ready_queue",
            "claim_to_start",
            "execution",
            "review_queue",
            "review",
            "integration_queue",
            "integration_test",
            "publication",
            "ci_follow_up",
            "finalization",
        }
        assert TASK_FLOW_STAGE_NAMES == expected

    def test_stage_order_matches_names_set(self) -> None:
        assert len(TASK_FLOW_STAGES) == len(TASK_FLOW_STAGE_NAMES)
        assert {s.value for s in TASK_FLOW_STAGES} == TASK_FLOW_STAGE_NAMES

    def test_stage_ordering_is_intake_first_finalization_last(self) -> None:
        assert TASK_FLOW_STAGES[0] is TaskFlowStage.INTAKE
        assert TASK_FLOW_STAGES[-1] is TaskFlowStage.FINALIZATION


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestTaskFlowSpanModel:
    def test_valid_span_constructs(self) -> None:
        span = _good_span()
        assert span.stage == "execution"
        assert span.to_dict()["duration_seconds"] == 12.5

    def test_unknown_stage_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _good_span(stage="not_a_stage")

    def test_attempt_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _good_span(attempt=0)

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _good_span(duration_seconds=-1.0)

    def test_open_span_allows_null_end(self) -> None:
        span = _good_span(
            ended_at=None,
            duration_seconds=None,
            outcome=TaskFlowOutcome.PENDING.value,
        )
        assert span.ended_at is None
        assert span.duration_seconds is None


class TestTaskCompletionModel:
    def test_valid_completion_constructs(self) -> None:
        summary = _good_completion()
        assert summary.per_stage_durations["execution"] == 12.5
        assert summary.token_count == 1000

    def test_attempt_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _good_completion(attempt=0)

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _good_completion(duration_seconds=-5.0)

    def test_bad_per_stage_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _good_completion(per_stage_durations={"bogus_stage": 1.0})


# ---------------------------------------------------------------------------
# Fresh-database table / index creation
# ---------------------------------------------------------------------------


class TestFreshDatabaseTables:
    def test_tables_exist(self) -> None:
        db = ephemeral_store()
        tables = {
            r["name"]
            for r in [{"name": n} for n in table_names(db)]
        }
        assert "task_flow_spans" in tables
        assert "task_completions" in tables
        db.close()

    def test_span_columns(self) -> None:
        db = ephemeral_store()
        cols = {
            r["name"] for r in [{"name": c} for c in column_names(db, "task_flow_spans")]
        }
        expected = {
            "id",
            "task_id",
            "project",
            "attempt",
            "stage",
            "started_at",
            "ended_at",
            "duration_seconds",
            "outcome",
            "metadata",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols)
        db.close()

    def test_completion_columns(self) -> None:
        db = ephemeral_store()
        cols = {
            r["name"] for r in [{"name": c} for c in column_names(db, "task_completions")]
        }
        expected = {
            "id",
            "task_id",
            "project",
            "attempt",
            "started_at",
            "ended_at",
            "duration_seconds",
            "outcome",
            "publication_sha",
            "main_sha",
            "route_count",
            "token_count",
            "cost_count",
            "review_count",
            "rebase_count",
            "test_count",
            "per_stage_durations",
            "metadata",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols)
        db.close()

    def test_indexes_created(self) -> None:
        db = ephemeral_store()
        indexes = all_index_names(db)
        for name in (
            "idx_task_flow_spans_task",
            "idx_task_completions_task",
            "idx_task_completions_project",
            "idx_task_completions_outcome_time",
        ):
            assert name in indexes
        db.close()


# ---------------------------------------------------------------------------
# SQLite upgrade: tables appear on a pre-existing database via _migrate
# ---------------------------------------------------------------------------


class TestSQLiteUpgrade:
    def test_upgrade_adds_tables_to_existing_db(self, tmp_path) -> None:
        legacy = tmp_path / "legacy.sqlite"
        conn = sqlite3.connect(legacy)
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
            """
        )
        conn.commit()
        conn.close()

        upgraded = ephemeral_store()
        tables = {
            r["name"]
            for r in [{"name": n} for n in table_names(upgraded)]
        }
        assert "task_flow_spans" in tables
        assert "task_completions" in tables
        upgraded.close()

    def test_upgrade_is_idempotent(self, tmp_path) -> None:
        db_path = str(tmp_path / "idem.sqlite")
        db1 = ephemeral_store()
        db1.close()
        db2 = ephemeral_store()
        tables = {
            r["name"]
            for r in [{"name": n} for n in table_names(db2)]
        }
        assert "task_flow_spans" in tables
        assert "task_completions" in tables
        db2.close()


# ---------------------------------------------------------------------------
# Store helper UPSERT idempotency + reads
# ---------------------------------------------------------------------------


def _persist_span(db: Store, span: TaskFlowSpan) -> None:
    import json

    db.upsert_task_flow_span(
        span_id=span.id,
        task_id=span.task_id,
        project=span.project,
        attempt=span.attempt,
        stage=span.stage,
        started_at=span.started_at,
        ended_at=span.ended_at,
        duration_seconds=span.duration_seconds,
        outcome=span.outcome,
        metadata_json=json.dumps(span.metadata),
        created_at=span.created_at,
        updated_at=span.updated_at,
    )


def _persist_completion(db: Store, c: TaskCompletion) -> None:
    import json

    db.upsert_task_completion(
        completion_id=c.id,
        task_id=c.task_id,
        project=c.project,
        attempt=c.attempt,
        started_at=c.started_at,
        ended_at=c.ended_at,
        duration_seconds=c.duration_seconds,
        outcome=c.outcome,
        publication_sha=c.publication_sha,
        main_sha=c.main_sha,
        route_count=c.route_count,
        token_count=c.token_count,
        cost_count=c.cost_count,
        review_count=c.review_count,
        rebase_count=c.rebase_count,
        test_count=c.test_count,
        per_stage_durations_json=json.dumps(c.per_stage_durations),
        metadata_json=json.dumps(c.metadata),
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


class TestStoreHelpers:
    def test_span_upsert_is_idempotent(self) -> None:
        db = ephemeral_store()
        span = _good_span()
        _persist_span(db, span)
        # Recompute the same key with a different generated id / updated values.
        recomputed = _good_span(
            id=new_id("span"),
            duration_seconds=20.0,
            updated_at=utcnow(),
        )
        _persist_span(db, recomputed)
        rows = db.list_task_flow_spans_by_task(span.task_id)
        assert len(rows) == 1
        # Original id/created_at preserved; mutable fields refreshed.
        assert rows[0]["id"] == span.id
        assert rows[0]["duration_seconds"] == 20.0
        assert rows[0]["created_at"] == span.created_at
        db.close()

    def test_distinct_stage_and_attempt_append(self) -> None:
        db = ephemeral_store()
        _persist_span(db, _good_span(stage=TaskFlowStage.EXECUTION.value))
        _persist_span(db, _good_span(stage=TaskFlowStage.REVIEW.value))
        _persist_span(db, _good_span(attempt=2))
        rows = db.list_task_flow_spans_by_task("task_abc")
        assert len(rows) == 3
        rows_attempt1 = db.list_task_flow_spans_by_task("task_abc", attempt=1)
        assert len(rows_attempt1) == 2
        db.close()

    def test_completion_upsert_is_idempotent(self) -> None:
        db = ephemeral_store()
        c = _good_completion()
        _persist_completion(db, c)
        _persist_completion(
            db,
            _good_completion(
                id=new_id("taskcompletion"),
                token_count=2000,
                review_count=2,
            ),
        )
        row = db.get_task_completion(c.task_id, c.attempt)
        assert row["id"] == c.id
        assert row["token_count"] == 2000
        assert row["review_count"] == 2
        # Only one row exists for this (task, attempt).
        rows = db.query_all(
            "SELECT * FROM task_completions WHERE task_id = ?", (c.task_id,)
        )
        assert len(rows) == 1
        db.close()

    def test_list_by_project_filters(self) -> None:
        db = ephemeral_store()
        _persist_span(db, _good_span(task_id="t1", project="mac"))
        _persist_span(db, _good_span(task_id="t2", project="other"))
        mac_rows = db.list_task_flow_spans_by_project("mac")
        assert len(mac_rows) == 1
        assert mac_rows[0]["task_id"] == "t1"
        db.close()

    def test_stage_aggregates(self) -> None:
        db = ephemeral_store()
        _persist_span(
            db,
            _good_span(task_id="t1", stage="execution", duration_seconds=10.0),
        )
        _persist_span(
            db,
            _good_span(task_id="t2", stage="execution", duration_seconds=30.0),
        )
        _persist_span(
            db,
            _good_span(task_id="t3", stage="review", duration_seconds=5.0),
        )
        agg = {r["stage"]: r for r in db.query_task_flow_stage_aggregates()}
        assert agg["execution"]["span_count"] == 2
        assert agg["execution"]["avg_duration_seconds"] == 20.0
        assert agg["execution"]["total_duration_seconds"] == 40.0
        assert agg["review"]["span_count"] == 1
        db.close()

    def test_write_participates_in_open_transaction(self) -> None:
        db = ephemeral_store()
        import json

        span = _good_span()
        with db.transaction() as conn:
            db.upsert_task_flow_span(
                span_id=span.id,
                task_id=span.task_id,
                project=span.project,
                attempt=span.attempt,
                stage=span.stage,
                started_at=span.started_at,
                ended_at=span.ended_at,
                duration_seconds=span.duration_seconds,
                outcome=span.outcome,
                metadata_json=json.dumps(span.metadata),
                created_at=span.created_at,
                updated_at=span.updated_at,
                conn=conn,
            )
        rows = db.list_task_flow_spans_by_task(span.task_id)
        assert len(rows) == 1
        db.close()


# ---------------------------------------------------------------------------
# Postgres schema file / translation shim
# ---------------------------------------------------------------------------


class TestPostgresSchema:
    def _schema_text(self) -> str:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "mac"
            / "data"
            / "postgres"
            / "schema.sql"
        )
        return schema_path.read_text(encoding="utf-8")

    def test_tables_present_in_schema(self) -> None:
        text = self._schema_text()
        assert "CREATE TABLE IF NOT EXISTS task_flow_spans" in text
        assert "CREATE TABLE IF NOT EXISTS task_completions" in text

    def test_indexes_present_in_schema(self) -> None:
        text = self._schema_text()
        for name in (
            "idx_task_flow_spans_task",
            "idx_task_completions_task",
            "idx_task_completions_project",
            "idx_task_completions_outcome_time",
        ):
            assert name in text


class TestPostgresTranslation:
    def test_table_names_round_trip(self) -> None:
        from mac.store_postgres import _translate_placeholders

        sql = "SELECT * FROM task_flow_spans JOIN task_completions USING (task_id)"
        assert _translate_placeholders(sql) == sql
