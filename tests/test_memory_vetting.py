"""Tests for Phase 2b vector-memory export/vetting (mac/memory_vetting.py)."""

from __future__ import annotations

from mac import memory_vetting as mv


def _fake_scroll(store):
    """store = {collection: [points]} -> a scroll(collection) callable."""
    return lambda col: list(store.get(col, []))


def test_export_flattens_and_keeps_id_collection():
    store = {
        "mac_memory_medium": [
            {
                "id": 1,
                "payload": {"agent_id": "natasha", "summary": "knows Boris", "tier": "medium"},
            },
            {"id": 2, "payload": {"agent_id": "rocky", "summary": "ships connectors"}},
        ],
        "mac_memory_long": [
            {"id": 9, "payload": {"agent_id": "natasha", "summary": "DGX Spark host"}},
        ],
    }
    recs = mv.export_memory_records(_fake_scroll(store), ["mac_memory_medium", "mac_memory_long"])
    assert len(recs) == 3
    r0 = next(r for r in recs if r["id"] == 1)
    assert r0["collection"] == "mac_memory_medium"
    assert r0["agent_id"] == "natasha" and r0["summary"] == "knows Boris"


def test_export_filters_by_agent():
    store = {
        "mac_memory_medium": [
            {"id": 1, "payload": {"agent_id": "natasha", "summary": "a"}},
            {"id": 2, "payload": {"agent_id": "rocky", "summary": "b"}},
        ]
    }
    recs = mv.export_memory_records(_fake_scroll(store), ["mac_memory_medium"], agent_id="natasha")
    assert [r["id"] for r in recs] == [1]


def test_search_records_substring_ci():
    recs = [
        {"id": 1, "summary": "knows BORIS the spy"},
        {"id": 2, "summary": "ships connectors"},
    ]
    hits = mv.search_records(recs, "boris")
    assert [r["id"] for r in hits] == [1]


def test_prune_points_deletes_vetted_ids():
    calls = []
    delete = lambda col, ids: calls.append((col, list(ids)))
    res = mv.prune_points(delete, "mac_memory_medium", [1, 2, None])
    assert res["deleted"] == 2  # None filtered out
    assert calls == [("mac_memory_medium", [1, 2])]


def test_prune_noop_on_empty():
    calls = []
    res = mv.prune_points(lambda c, i: calls.append(1), "c", [])
    assert res["deleted"] == 0 and calls == []
