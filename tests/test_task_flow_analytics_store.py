"""Tests for task-flow analytics models and durable storage.

Covers the models + storage + schema layer added for transition-derived
task-flow KPIs (mac.task_flow_span.v1 / mac.task_completion.v1):

 * Model validation: canonical stage vocabulary, attempt/duration bounds,
   per-stage-duration key validation.
 * Table/index creation on a fresh database.
 * Postgres schema file: the new tables/indexes appear in the bundled DDL.
 * Postgres translation shim: new table names round-trip unmodified.

It no longer claims to cover "Store helper UPSERT idempotency" or "SQLite
upgrade": see the REMOVED note in the middle of the file for why neither claim
was true.
"""

from __future__ import annotations

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
        tables = {r["name"] for r in [{"name": n} for n in table_names(db)]}
        assert "task_flow_spans" in tables
        assert "task_completions" in tables
        db.close()

    def test_span_columns(self) -> None:
        db = ephemeral_store()
        cols = {r["name"] for r in [{"name": c} for c in column_names(db, "task_flow_spans")]}
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
        cols = {r["name"] for r in [{"name": c} for c in column_names(db, "task_completions")]}
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
# REMOVED: TestSQLiteUpgrade and TestStoreHelpers.
#
# TestSQLiteUpgrade (the "tables appear on a pre-existing database via _migrate"
# suite) targeted the SQLite backend, which is gone. It also never tested an
# upgrade: it wrote a legacy .sqlite file and then ignored it, calling
# `ephemeral_store()` and asserting a FRESH database has the tables. Its sibling
# built a `db_path` it never passed to anything. Neither could fail for the
# reason its name gave.
#
# TestStoreHelpers certified UPSERT idempotency, project filtering and stage
# aggregation for six Store helpers with no production caller:
# upsert_task_flow_span, upsert_task_completion, list_task_flow_spans_by_task,
# list_task_flow_spans_by_project, get_task_completion and
# query_task_flow_stage_aggregates. Production writes and reads
# task_flow_spans/task_completions with its own inline SQL in
# task_flow_analytics.py and never called them, so the suite was green over code
# no request reaches -- and it read as coverage of the analytics write path,
# which it was not. The helpers are deleted with it (store_helpers.py, plus
# their Store Protocol stubs in store.py). Consolidating the inline SQL onto one
# shared definition is filed separately.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Postgres schema file / translation shim
# ---------------------------------------------------------------------------


class TestPostgresSchema:
    def _schema_text(self) -> str:
        schema_path = (
            Path(__file__).resolve().parents[1] / "src" / "mac" / "data" / "postgres" / "schema.sql"
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
