"""Tests for the dreaming pipeline.

Several of these encode specific defects measured in the live ledger under the
previous implementation, so a regression is caught here rather than after
another forty days of production:

* ``test_duplicates_are_merged_not_appended`` — 154,273 rows / 4,414 statements
* ``test_wins_survive_extraction`` — 1,443 successful outcomes, 0 win artifacts
* ``test_confidence_needs_independent_sources`` — one row re-read scored "high"
* ``test_growing_the_store_is_quarantined`` — the append-forever bug
* ``test_promotion_shrinks_the_store`` — promotion must retire what it replaces
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, List

import pytest

from mac import dreaming
from mac.dreaming.models import (
    DreamPolicy,
    InputRecord,
    InputSession,
    MemoryCandidate,
    MemoryKind,
    SessionOutcome,
    SourceRef,
    StoreState,
)
from mac.models import NotFoundError, ValidationError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def learning_record(
    record_id: str,
    outcome: str,
    *,
    signature: str = "",
    evidence_type: str = "repo_change",
    project: str = "mac",
) -> InputRecord:
    return InputRecord(
        id=record_id,
        record_type="deployment_learning:%s" % project,
        content=json.dumps(
            {
                "schema": "mac.deployment_learning.v1",
                "outcome": outcome,
                "evidence_type": evidence_type,
                "error_signature": signature,
            }
        ),
        project=project,
        created_at="2026-07-01T00:00:00Z",
    )


def candidate(
    kind: MemoryKind,
    statement: str,
    *,
    sources: int = 1,
    source_prefix: str = "sesn",
) -> MemoryCandidate:
    return MemoryCandidate(
        kind=kind,
        statement=statement,
        sources=[
            SourceRef(kind="session", id="%s_%d" % (source_prefix, i))
            for i in range(sources)
        ],
    )


class FakeMemoryService:
    """Minimal stand-in for the hub memory service used by promote_run."""

    def __init__(self) -> None:
        self.added: List[dict] = []

    def add_memory(self, **kwargs: Any):
        record_id = "mem_%d" % len(self.added)
        self.added.append({"id": record_id, **kwargs})
        return type("Memory", (), {"id": record_id})()


class SqliteStore:
    """Thin Store-protocol adapter over an in-memory SQLite database."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE memory_records (id TEXT PRIMARY KEY, task_id TEXT,"
            " subject_type TEXT, subject_id TEXT, record_type TEXT,"
            " content TEXT, evidence_id TEXT, created_by TEXT, created_at TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE vector_refs (id TEXT PRIMARY KEY, memory_id TEXT)"
        )

    def execute(self, sql: str, params: Any = ()) -> Any:
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur

    def query_one(self, sql: str, params: Any = ()):
        return self.conn.execute(sql, tuple(params)).fetchone()

    def query_all(self, sql: str, params: Any = ()) -> list:
        return list(self.conn.execute(sql, tuple(params)).fetchall())


@pytest.fixture()
def store() -> SqliteStore:
    return SqliteStore()


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def test_wins_survive_extraction() -> None:
    """A successful outcome must produce a PRACTICE, not be discarded.

    The old ``_dream_kind`` had no success kind at all, so every one of the
    1,443 wins in the live ledger vanished.
    """

    records = [
        learning_record("mem_a", "success"),
        learning_record("mem_b", "approved_published"),
        learning_record("mem_c", "failure", signature="tests failed"),
    ]
    result = dreaming.dream(records, policy=DreamPolicy(max_output_ratio=1.0))
    kinds = {candidate.kind for candidate in result.candidates}
    assert MemoryKind.PRACTICE in kinds, "wins were dropped"
    assert MemoryKind.PITFALL in kinds
    assert result.win_count >= 1


def test_heuristic_does_not_keyword_scan_free_text() -> None:
    """Prose containing "error" must not become a pitfall on its own.

    This is the exact mechanism that turned successful tasks into failure
    findings: the group text contained the substring "error", so the whole
    group was labelled a failure pattern.
    """

    records = [
        InputRecord(
            id="mem_prose",
            record_type="note:mac",
            content="We discussed the error handling design and everything went well.",
        )
    ]
    result = dreaming.dream(records)
    assert result.candidates == []


def test_extraction_uses_model_when_available() -> None:
    def caller(model: str, prompt: str, context: str):
        assert "practice" in prompt
        payload = {
            "memories": [
                {
                    "kind": "practice",
                    "statement": "Run the contract tests before publishing.",
                    "sources": [{"kind": "session", "id": "sesn_1"}],
                }
            ],
            "reflections": [
                {
                    "session_id": "sesn_1",
                    "objective": "Publish the change",
                    "outcome": "objective_met",
                    "reason": "Review approved and landed.",
                }
            ],
        }
        return json.dumps(payload), None, 12

    sessions = [InputSession(id="sesn_1", turns=[{"role": "user", "text": "publish it"}])]
    result = dreaming.dream(
        [learning_record("mem_a", "success")],
        sessions,
        model="test-model",
        model_caller=caller,
    )
    assert result.extractor == "model:test-model"
    assert result.reflections[0].outcome is SessionOutcome.OBJECTIVE_MET
    assert any(c.kind is MemoryKind.PRACTICE for c in result.candidates)


def test_model_reply_wrapped_in_fence_is_parsed() -> None:
    def caller(model: str, prompt: str, context: str):
        return (
            "Here you go:\n```json\n"
            '{"memories": [], "reflections": [{"session_id": "s1",'
            ' "objective": "x", "outcome": "abandoned"}]}\n```',
            None,
            1,
        )

    result = dreaming.dream(
        [], [InputSession(id="s1")], model="m", model_caller=caller
    )
    assert result.reflections[0].outcome is SessionOutcome.ABANDONED


def test_model_failure_falls_back_without_losing_the_run() -> None:
    def caller(model: str, prompt: str, context: str):
        raise RuntimeError("router down")

    result = dreaming.dream(
        [learning_record("mem_a", "success")], model="m", model_caller=caller
    )
    assert result.extractor.startswith("heuristic(after-model-error")
    assert result.candidates


def test_fallback_can_be_forbidden() -> None:
    def caller(model: str, prompt: str, context: str):
        raise RuntimeError("router down")

    result = dreaming.dream(
        [learning_record("mem_a", "success")],
        policy=DreamPolicy(allow_heuristic_fallback=False),
        model="m",
        model_caller=caller,
    )
    assert result.state is StoreState.QUARANTINED
    assert result.errors


# ---------------------------------------------------------------------------
# resolve / compress
# ---------------------------------------------------------------------------


def test_duplicates_are_merged_not_appended() -> None:
    """The 35:1 duplication bug. Same insight twice -> one memory, two sources."""

    records = [
        learning_record("mem_a", "failure", signature="tests failed on push"),
        learning_record("mem_b", "failure", signature="tests failed on push"),
        learning_record("mem_c", "failure", signature="tests failed on push"),
    ]
    result = dreaming.dream(records, policy=DreamPolicy(max_output_ratio=1.0))
    assert len(result.candidates) == 1
    assert result.candidates[0].source_count == 3


def test_reworded_duplicates_are_merged() -> None:
    policy = DreamPolicy(max_output_ratio=1.0, max_pairwise_similarity=0.6)
    raw = [
        candidate(MemoryKind.PITFALL, "the publish step fails when files are untracked"),
        candidate(
            MemoryKind.PITFALL,
            "publish step fails when untracked files exist",
            source_prefix="other",
        ),
    ]
    from mac.dreaming.pipeline import resolve_duplicates_and_conflicts

    snapshot = dreaming.Snapshot()
    merged = resolve_duplicates_and_conflicts(raw, snapshot, policy)
    assert len(merged) == 1
    assert merged[0].source_count == 2


def test_confidence_needs_independent_sources() -> None:
    """Three refs to the SAME session is one source, not corroboration."""

    same = MemoryCandidate(
        kind=MemoryKind.FACT,
        statement="x",
        sources=[SourceRef(kind="session", id="s1") for _ in range(3)],
    )
    assert same.source_count == 1
    assert same.confidence == "low"

    distinct = MemoryCandidate(
        kind=MemoryKind.FACT,
        statement="x",
        sources=[SourceRef(kind="session", id="s%d" % i) for i in range(3)],
    )
    assert distinct.source_count == 3
    assert distinct.confidence == "high"


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def test_growing_the_store_is_quarantined() -> None:
    """The append-forever bug, as a hard gate."""

    from mac.dreaming.gates import compression_gate

    snapshot = dreaming.Snapshot(records=[learning_record("m%d" % i, "success") for i in range(4)])
    too_many = [candidate(MemoryKind.FACT, "fact %d" % i) for i in range(4)]
    gate = compression_gate(too_many, snapshot, DreamPolicy(max_output_ratio=0.75))
    assert not gate.passed
    assert "not curation" in gate.detail


def test_ungrounded_candidate_fails_provenance() -> None:
    from mac.dreaming.gates import provenance_gate

    snapshot = dreaming.Snapshot(records=[learning_record("mem_real", "success")])
    invented = MemoryCandidate(
        kind=MemoryKind.FACT,
        statement="something the model made up",
        sources=[SourceRef(kind="memory", id="mem_does_not_exist")],
    )
    gate = provenance_gate([invented], snapshot, DreamPolicy())
    assert not gate.passed


def test_near_duplicate_output_fails_retrieval_quality() -> None:
    from mac.dreaming.gates import retrieval_quality_gate

    pair = [
        candidate(MemoryKind.FACT, "the hub ledger runs on postgres now"),
        candidate(MemoryKind.FACT, "the hub ledger runs on postgres now", source_prefix="b"),
    ]
    gate = retrieval_quality_gate(pair, DreamPolicy())
    assert not gate.passed


def test_contradictory_output_fails() -> None:
    from mac.dreaming.gates import contradiction_gate

    pair = [
        candidate(MemoryKind.PRACTICE, "always rebase before publishing the branch"),
        candidate(MemoryKind.PITFALL, "always rebase before publishing the branch", source_prefix="b"),
    ]
    gate = contradiction_gate(pair, DreamPolicy())
    assert not gate.passed


def test_secrets_never_reach_a_memory() -> None:
    from mac.dreaming.gates import privacy_gate

    leaky = MemoryCandidate(
        kind=MemoryKind.FACT,
        statement="use token ghp_abcdefghijklmnopqrstuvwxyz012345 for the repo",
        sources=[SourceRef(kind="session", id="s1")],
    )
    assert not privacy_gate([leaky], []).passed

    clean = MemoryCandidate(
        kind=MemoryKind.FACT,
        statement=dreaming.redact(leaky.statement),
        sources=[SourceRef(kind="session", id="s1")],
    )
    assert privacy_gate([clean], []).passed


def test_balance_gate_flags_failure_only_output() -> None:
    from mac.dreaming.gates import balance_gate

    gate = balance_gate([candidate(MemoryKind.PITFALL, "a thing broke")])
    assert gate.passed  # advisory
    assert "failure-only" in gate.detail


# ---------------------------------------------------------------------------
# persistence and promotion
# ---------------------------------------------------------------------------


def test_dream_never_writes_to_live_memory(store: SqliteStore) -> None:
    """Copy-on-write: running and saving a dream leaves memory_records alone."""

    store.execute(
        "INSERT INTO memory_records (id, record_type, content, created_at)"
        " VALUES (?, ?, ?, ?)",
        ("mem_a", "deployment_learning:mac", learning_record("mem_a", "success").content, "2026-07-01"),
    )
    before = store.query_all("SELECT id FROM memory_records")
    records = dreaming.load_records(store)
    result = dreaming.dream(records, policy=DreamPolicy(max_output_ratio=1.0))
    dreaming.save_run(store, result, DreamPolicy())
    after = store.query_all("SELECT id FROM memory_records")
    assert [row["id"] for row in before] == [row["id"] for row in after]


def test_promotion_shrinks_the_store(store: SqliteStore) -> None:
    """Promoting must retire the rows it supersedes — that is the compaction."""

    for i in range(3):
        store.execute(
            "INSERT INTO memory_records (id, record_type, content, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                "mem_%d" % i,
                "deployment_learning:mac",
                learning_record("mem_%d" % i, "failure", signature="tests failed on push").content,
                "2026-07-0%d" % (i + 1),
            ),
        )
    records = dreaming.load_records(store)
    result = dreaming.dream(records, policy=DreamPolicy(max_output_ratio=1.0))
    assert result.state is StoreState.READY_FOR_REVIEW
    dreaming.save_run(store, result, DreamPolicy())

    memory_service = FakeMemoryService()
    report = dreaming.promote_run(store, memory_service, result.run_id)
    assert report["promoted_count"] == 1
    assert report["retired_count"] == 3
    assert report["net_change"] == -2, "promotion must reduce the store"
    remaining = store.query_all("SELECT id FROM memory_records")
    assert remaining == []


def test_quarantined_run_cannot_be_promoted(store: SqliteStore) -> None:
    snapshot_records = [learning_record("m%d" % i, "success") for i in range(2)]
    result = dreaming.dream(snapshot_records, policy=DreamPolicy(max_output_ratio=0.1))
    assert result.state is StoreState.QUARANTINED
    dreaming.save_run(store, result, DreamPolicy())
    with pytest.raises(ValidationError, match="ready_for_review"):
        dreaming.promote_run(store, FakeMemoryService(), result.run_id)


def test_unknown_run_raises_a_domain_error(store: SqliteStore) -> None:
    """Control-plane methods must not leak implementation exceptions."""

    dreaming.ensure_schema(store)
    with pytest.raises(NotFoundError):
        dreaming.promote_run(store, FakeMemoryService(), "dreamrun_nope")


def test_run_roundtrips(store: SqliteStore) -> None:
    result = dreaming.dream(
        [learning_record("mem_a", "success")], policy=DreamPolicy(max_output_ratio=1.0)
    )
    dreaming.save_run(store, result, DreamPolicy(), project="mac")
    loaded = dreaming.get_run(store, result.run_id)
    assert loaded is not None
    assert loaded["state"] == result.state.value
    assert len(loaded["candidates"]) == len(result.candidates)
    assert dreaming.list_runs(store)[0]["id"] == result.run_id


def test_dream_output_is_excluded_from_its_own_input(store: SqliteStore) -> None:
    """No self-referential loop — the defect four investigations identified."""

    for record_type in ("dream:failure_pattern", "nap_summary", "dream_memory:practice"):
        store.execute(
            "INSERT INTO memory_records (id, record_type, content, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("mem_" + record_type.replace(":", "_"), record_type, "{}", "2026-07-01"),
        )
    store.execute(
        "INSERT INTO memory_records (id, record_type, content, created_at)"
        " VALUES (?, ?, ?, ?)",
        ("mem_real", "deployment_learning:mac", "{}", "2026-07-01"),
    )
    records = dreaming.load_records(store)
    assert [record.id for record in records] == ["mem_real"]


def test_prior_memories_can_be_superseded(store: SqliteStore) -> None:
    """A dream must be able to retire its own stale earlier conclusions.

    Without this the curated store accumulates forever — the same disease as
    the old cycle, just slower.
    """

    store.execute(
        "INSERT INTO memory_records (id, subject_id, record_type, content, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            "mem_old_dream",
            "mac",
            "dream_memory:pitfall",
            json.dumps(
                {
                    "schema": "mac.dream_memory.v2",
                    "kind": "pitfall",
                    "statement": "publishing fails when files are untracked",
                }
            ),
            "2026-07-01",
        ),
    )
    existing = dreaming.load_existing_memories(store, project="mac")
    assert [record.id for record in existing] == ["mem_old_dream"]

    def caller(model: str, prompt: str, context: str):
        # The prior memory must be visible to the extractor so it can supersede it.
        assert "mem_old_dream" in prompt
        return (
            json.dumps(
                {
                    "memories": [
                        {
                            "kind": "pitfall",
                            "statement": "publishing now succeeds with untracked files after the finalizer fix",
                            "sources": [{"kind": "memory", "id": "mem_old_dream"}],
                        }
                    ],
                    "reflections": [],
                }
            ),
            None,
            1,
        )

    result = dreaming.dream(
        [], existing=existing, model="m", model_caller=caller,
        policy=DreamPolicy(max_output_ratio=1.0),
    )
    assert result.candidates[0].supersedes == ["mem_old_dream"]


def test_prior_memories_count_toward_store_size() -> None:
    """The compression gate measures the whole store, prior memories included."""

    snapshot = dreaming.Snapshot(
        records=[learning_record("m1", "success")],
        existing=[learning_record("d1", "success")],
    )
    assert snapshot.input_size == 2
    assert "d1" in snapshot.supersedable_ids


def test_run_history_is_bounded(store: SqliteStore) -> None:
    """The audit trail must not become the next runaway table."""

    from mac.dreaming.store import prune_runs

    for _ in range(8):
        result = dreaming.dream(
            [learning_record("mem_a", "success")], policy=DreamPolicy(max_output_ratio=1.0)
        )
        dreaming.save_run(store, result, DreamPolicy())
    assert len(dreaming.list_runs(store, limit=100)) == 8

    report = prune_runs(store, retention={StoreState.READY_FOR_REVIEW.value: 3})
    assert report["deleted_runs"] == 5
    assert len(dreaming.list_runs(store, limit=100)) == 3
    # Candidate entries for pruned runs go too, rather than dangling.
    remaining_ids = {run["id"] for run in dreaming.list_runs(store, limit=100)}
    orphans = [
        row
        for row in store.query_all("SELECT run_id FROM dream_candidate_entries")
        if row["run_id"] not in remaining_ids
    ]
    assert orphans == []


def test_project_is_read_from_data_not_a_hardcoded_table() -> None:
    """Repo-agnostic: any project name works, not just ``mac``."""

    from mac.dreaming.engine import _project_from_record_type

    assert _project_from_record_type("deployment_learning:freebsd-src-bazel") == "freebsd-src-bazel"
    assert _project_from_record_type("deployment_learning:Aviation") == "Aviation"
    assert _project_from_record_type("note") is None
