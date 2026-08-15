"""Indexes that are never scanned are pure cost, and they came back for free.

Measured on the fleet hub 2026-08-15 (PostgreSQL 17.10, stats never reset, so
idx_scan=0 means never used since the database was created):

    unused indexes                        407
    their total size                     2682 MB
    database size                        7652 MB

A third of the database was index that only ever cost writes, concentrated on
the two highest-write-rate tables:

    action_events          8 indexes  2775 MB  (69 MB heap, 148k rows)
    observability_events   6 indexes  2480 MB  (201 MB heap, 500k rows)

5.25 GB of index on 270 MB of heap, maintained on every firehose insert. This
is the same failure mode as the action_events incident that wedged the hub at
16 GB: an index per column, added speculatively, never queried.

Twelve were dropped, reclaiming 2296 MB (7652 MB -> 5356 MB). This test exists
because dropping them from the DATABASE is not enough: initialize() re-applies
the packaged schema on every hub start, so leaving the CREATE INDEX statements
in schema.sql would have silently rebuilt all of it on the next restart, and
the next measurement would have looked like the drops simply never worked.

Constraint-backing indexes are deliberately NOT in this list even when unused.
observability_events_id_key has never been scanned and stays: a unique index is
enforced on write, and its scan count says nothing about whether it is needed.
"""

from __future__ import annotations

import pathlib

import pytest

SCHEMA = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "mac" / "data" / "postgres" / "schema.sql"
)

# name -> size it occupied on the fleet hub when it was dropped
DROPPED_INDEXES = {
    "idx_observability_events_kind_layer": "496 MB",
    "idx_action_events_type_outcome": "371 MB",
    "idx_action_events_agent_timestamp": "326 MB",
    "idx_action_events_policy_timestamp": "324 MB",
    "idx_action_events_sandbox_timestamp": "324 MB",
    "idx_action_events_session_timestamp": "324 MB",
    "idx_secret_audit_secret_created": "56 MB",
    "idx_memory_task_created": "38 MB",
    "idx_operator_notifications_subject": "16 MB",
    "idx_evidence_artifacts_evidence": "8.6 MB",
    "idx_task_flow_spans_project": "6.6 MB",
    "idx_task_flow_spans_stage_time": "6.0 MB",
}


@pytest.mark.parametrize("name", sorted(DROPPED_INDEXES), ids=sorted(DROPPED_INDEXES))
def test_a_dropped_index_is_not_recreated_by_the_schema(name):
    """Re-adding one of these to schema.sql rebuilds it on the next hub start."""
    schema = SCHEMA.read_text(encoding="utf-8")

    assert name not in schema, (
        "%s was dropped from the fleet hub (%s reclaimed) because it had never "
        "been scanned. Putting it back in schema.sql recreates it on every "
        "initialize(). If it is genuinely needed now, say so here and delete "
        "this entry -- do not add it back silently."
        % (name, DROPPED_INDEXES[name])
    )


def test_the_indexes_that_earn_their_keep_are_still_there():
    """The counterweight: this must not become an argument for having no
    indexes. Each of these was measured in active use on the hub."""
    schema = SCHEMA.read_text(encoding="utf-8")

    for name in (
        "idx_observability_events_subject_sequence",  # 33,820 scans
        "idx_observability_events_name_created",      # 2,043 scans
        "idx_observability_events_created",           # 2,937 scans
        "idx_action_events_timestamp",                # 353 scans
        "idx_action_events_task_timestamp",           # 993 scans
        "idx_tasks_state_priority",                   # 13,692 scans
        "idx_tasks_owner",                            # 575,667 scans
    ):
        assert name in schema, "%s is in active use and must not be dropped" % name
