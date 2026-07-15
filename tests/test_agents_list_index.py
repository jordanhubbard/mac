"""list_agents must stay fast as the agents table fills with tombstones.

Ephemeral/decommissioned agents are tombstoned (deleted_at set), never purged,
so the table grows without bound. The default list filters `deleted_at IS NULL`
and sorts by name,id — without a partial index that is a full scan + filesort
over every tombstone (the ~1s /agents latency for 8 live agents). The partial
index idx_agents_live_name makes it an index-only scan of just the live rows.
"""
from __future__ import annotations

from mac.services import ControlPlane


def test_list_agents_query_uses_partial_live_index():
    cp = ControlPlane.in_memory()
    plan = cp.store.query_all(
        "EXPLAIN QUERY PLAN "
        "SELECT * FROM agents WHERE deleted_at IS NULL ORDER BY name, id"
    )
    details = " ".join(str(dict(r).get("detail", "")) for r in plan)
    # index-only scan, and crucially NO 'USE TEMP B-TREE' (i.e. no filesort)
    assert "idx_agents_live_name" in details, details
    assert "SCAN agents" not in details or "USING INDEX" in details, details
    assert "TEMP B-TREE" not in details.upper(), details


def test_partial_index_exists_and_is_predicate_scoped():
    cp = ControlPlane.in_memory()
    row = cp.store.query_one(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_agents_live_name'"
    )
    assert row is not None
    assert "WHERE deleted_at IS NULL" in row["sql"]
    # functional tombstone exclusion is covered by tests/test_agent_tombstone.py
