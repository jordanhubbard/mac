"""list_agents must stay fast as the agents table fills with tombstones.

Ephemeral/decommissioned agents are tombstoned (deleted_at set), never purged,
so the table grows without bound. The default list filters `deleted_at IS NULL`
and sorts by name,id — without a partial index that is a full scan + filesort
over every tombstone (the ~1s /agents latency for 8 live agents). The partial
index idx_agents_live_name makes it an index-only scan of just the live rows.
"""
from __future__ import annotations

from mac.services import ControlPlane


def _backend(cp) -> str:
    return str(cp.store.backend_identity().get("backend") or "").lower()


def test_list_agents_query_uses_partial_live_index():
    cp = ControlPlane.in_memory()
    query = "SELECT * FROM agents WHERE deleted_at IS NULL ORDER BY name, id"
    if _backend(cp) == "postgres":
        plan = " ".join(
            str(dict(row).get("QUERY PLAN", ""))
            for row in cp.store.query_all("EXPLAIN " + query)
        )
        assert "idx_agents_live_name" in plan, plan
        # A Sort node means the index is not supplying the ordering.
        assert "Sort" not in plan, plan
        return
    plan = cp.store.query_all("EXPLAIN QUERY PLAN " + query)
    details = " ".join(str(dict(r).get("detail", "")) for r in plan)
    # index-only scan, and crucially NO 'USE TEMP B-TREE' (i.e. no filesort)
    assert "idx_agents_live_name" in details, details
    assert "SCAN agents" not in details or "USING INDEX" in details, details
    assert "TEMP B-TREE" not in details.upper(), details


def test_partial_index_exists_and_is_predicate_scoped():
    cp = ControlPlane.in_memory()
    if _backend(cp) == "postgres":
        # Scoped to this test's own schema: pg_indexes spans every schema in
        # the database, and a parallel worker dropping its schema mid-query
        # makes an unscoped lookup flaky.
        row = cp.store.query_one(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'idx_agents_live_name' "
            "AND schemaname = current_schema()"
        )
        assert row is not None, "partial index missing on postgres"
        assert "deleted_at IS NULL" in row["indexdef"]
        return
    row = cp.store.query_one(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_agents_live_name'"
    )
    assert row is not None
    assert "WHERE deleted_at IS NULL" in row["sql"]
    # functional tombstone exclusion is covered by tests/test_agent_tombstone.py
