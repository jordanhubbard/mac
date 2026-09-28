"""
Tests for mac.soul_graph — the splay-DAG-tag soul engine.

Covers: node creation, pinning, splay promotion, DAG edges, tag index,
semantic search, discovery traversal, exploration branch, merge, persistence.
"""
from __future__ import annotations
import json, math, time, tempfile
from pathlib import Path

import pytest
from mac.soul_graph import SoulGraph, SoulNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_soul() -> SoulGraph:
    g = SoulGraph(name="test")
    g.add("Be genuinely helpful.", tags={"axiom"}, pinned=True, node_id="a1")
    g.add("Have opinions.", tags={"axiom"}, pinned=True, node_id="a2")
    g.add("Rocky is the fleet hub.", tags={"identity", "crew"}, parents=["a1"], node_id="i1")
    g.add("Bullwinkle handles RAG.", tags={"crew", "rag"}, parents=["i1"], node_id="i2")
    g.add("Natasha runs on the GPU host.", tags={"crew", "gpu"}, parents=["i1"], node_id="i3")
    g.add("Lessons: curl is blocked, use urllib.", tags={"lesson", "tooling"}, parents=["a1"], node_id="l1")
    g.add("git is the filesystem.", tags={"lesson", "git"}, parents=["a1"], node_id="l2")
    return g


# ---------------------------------------------------------------------------
# Basic creation
# ---------------------------------------------------------------------------

def test_add_node():
    g = SoulGraph(name="t")
    n = g.add("hello world", tags={"a", "b"})
    assert n.id in g.nodes
    assert g.nodes[n.id].content == "hello world"
    assert "a" in g.nodes[n.id].tags


def test_pinned_node_infinite_score():
    g = make_soul()
    assert g.nodes["a1"].recency_score() == math.inf
    assert g.nodes["a2"].recency_score() == math.inf


def test_unpinned_node_finite_score():
    g = make_soul()
    assert g.nodes["i1"].recency_score() < math.inf


# ---------------------------------------------------------------------------
# Splay / access promotion
# ---------------------------------------------------------------------------

def test_touch_promotes():
    g = make_soul()
    # Touch i2 several times — should rise above i3
    for _ in range(5):
        g.touch("i2")
    hot = [n.id for n in g.hot(10) if not n.pinned]
    assert hot.index("i2") < hot.index("i3") if "i3" in hot else True


def test_hot_returns_pinned_first():
    g = make_soul()
    hot = g.hot(3)
    # All top-3 should be pinned (we only have 2 pinned, so at least those 2)
    pinned_ids = {n.id for n in hot if n.pinned}
    assert "a1" in pinned_ids
    assert "a2" in pinned_ids


# ---------------------------------------------------------------------------
# DAG edges
# ---------------------------------------------------------------------------

def test_parent_child_wired():
    g = make_soul()
    assert "i1" in g.nodes["a1"].children
    assert "a1" in g.nodes["i1"].parents


def test_link_adds_edge():
    g = make_soul()
    g.link("l1", "l2")
    assert "l2" in g.nodes["l1"].children
    assert "l1" in g.nodes["l2"].parents


def test_path_finds_route():
    g = make_soul()
    p = g.path("a1", "i2")
    assert p[0] == "a1"
    assert p[-1] == "i2"
    assert "i1" in p


def test_path_no_route():
    g = make_soul()
    # l1 and i2 are not connected
    p = g.path("i2", "l1")
    assert p == []


def test_ancestors():
    g = make_soul()
    anc = g.ancestors("i2")
    assert "i1" in anc
    assert "a1" in anc


# ---------------------------------------------------------------------------
# Tag index
# ---------------------------------------------------------------------------

def test_by_tag_returns_nodes():
    g = make_soul()
    crew = g.by_tag("crew")
    ids = {n.id for n in crew}
    assert "i1" in ids
    assert "i2" in ids
    assert "i3" in ids


def test_by_tag_multi():
    g = make_soul()
    results = g.by_tag("rag", "gpu")
    ids = {n.id for n in results}
    assert "i2" in ids
    assert "i3" in ids


def test_tag_method():
    g = make_soul()
    g.tag("l2", "important")
    assert "important" in g.nodes["l2"].tags
    assert "l2" in g._tag_index.get("important", set())


# ---------------------------------------------------------------------------
# Semantic search (keyword stub)
# ---------------------------------------------------------------------------

def test_semantic_search_finds_match():
    g = make_soul()
    results = g.semantic_search("curl blocked urllib", top_k=3)
    assert results
    top_node, top_score = results[0]
    assert top_node.id == "l1"
    assert top_score > 0


def test_semantic_search_empty_query():
    g = make_soul()
    results = g.semantic_search("zzznonexistent", top_k=3)
    assert results == []


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discover_returns_path():
    g = make_soul()
    results = g.discover("rag bullwinkle", top_k=2)
    assert results
    node, path = results[0]
    assert node.id == "i2"
    assert len(path) >= 1


def test_discover_touches_nodes():
    g = make_soul()
    before = g.nodes["i2"].access_count
    g.discover("rag bullwinkle", top_k=1)
    assert g.nodes["i2"].access_count > before


# ---------------------------------------------------------------------------
# Exploration branch
# ---------------------------------------------------------------------------

def test_branch_is_independent():
    g = make_soul()
    b = g.branch()
    b.add("Rocky tries on GPU expert role.", tags={"exploration", "gpu"}, node_id="exp1")
    assert "exp1" in b.nodes
    assert "exp1" not in g.nodes


def test_merge_new_only():
    g = make_soul()
    b = g.branch()
    b.add("Experimental node.", tags={"exploration"}, node_id="exp2")
    original_count = len(g.nodes)
    g.merge_from(b, new_only=True)
    assert "exp2" in g.nodes
    assert len(g.nodes) == original_count + 1


def test_merge_does_not_overwrite_existing():
    g = make_soul()
    b = g.branch()
    b.nodes["a1"].content = "MUTATED"
    g.merge_from(b, new_only=True)
    assert g.nodes["a1"].content == "Be genuinely helpful."


# ---------------------------------------------------------------------------
# Persistence (save / load round-trip)
# ---------------------------------------------------------------------------

def test_save_load_roundtrip():
    g = make_soul()
    g.touch("i2")
    g.touch("i2")

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)

    try:
        g.save(path)
        g2 = SoulGraph.load(path)

        assert set(g2.nodes.keys()) == set(g.nodes.keys())
        assert g2.nodes["a1"].pinned is True
        assert g2.nodes["i2"].access_count == 2
        assert g2.nodes["a1"].recency_score() == math.inf
        assert "crew" in g2.nodes["i1"].tags
        assert "i1" in g2.nodes["a1"].children
    finally:
        path.unlink(missing_ok=True)


def test_save_load_tag_index():
    g = make_soul()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    try:
        g.save(path)
        g2 = SoulGraph.load(path)
        crew = g2.by_tag("crew")
        ids = {n.id for n in crew}
        assert "i1" in ids
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_summary_runs():
    g = make_soul()
    s = g.summary()
    assert "test" in s
    assert "7" in s  # 7 nodes


# ---------------------------------------------------------------------------
# Regression tests for bugs observed via live agent session (2026-09-12)
# ---------------------------------------------------------------------------

def test_bug4_new_node_scores_higher_than_old():
    """BUG 4 fixed: exponential decay — new zero-access node >> week-old zero-access."""
    g = SoulGraph(name="t")
    old = g.add("old experience", tags={"old"})
    g.nodes[old.id].created_at -= 7 * 86400
    g.nodes[old.id].last_accessed -= 7 * 86400
    g.splay.access(old.id, g.nodes[old.id].recency_score())

    new = g.add("fresh experience", tags={"new"})
    # With exp decay, new node at age~0 should score >> 5x an unaccessed week-old node
    assert g.nodes[new.id].recency_score() > g.nodes[old.id].recency_score() * 5


def test_bug3_touch_partially_promotes_parents():
    """BUG 3 fixed: touching a child splay-promotes its parents."""
    g = make_soul()
    # Touch i2 repeatedly; i1 (its parent) should appear before unrelated nodes
    for _ in range(3):
        g.touch("i2")
    hot_ids = [n.id for n in g.hot(10) if not n.pinned]
    assert "i2" in hot_ids
    # i1 is parent of i2 — should rank above never-touched l1, l2
    if "i1" in hot_ids and "l1" in hot_ids:
        assert hot_ids.index("i1") < hot_ids.index("l1")


def test_bug1_axiom_resplayed_when_child_added():
    """BUG 1 fixed: adding a child under a pinned axiom re-splays axiom to root."""
    g = SoulGraph(name="t")
    ax = g.add("Core axiom", tags={"axiom"}, pinned=True, node_id="ax1")
    g.add("Unrelated A", tags={"other"}, node_id="u1")
    g.add("Unrelated B", tags={"other"}, node_id="u2")
    # Adding a child of the axiom should splay it back to root
    g.add("Derived from axiom", tags={"derived"}, parents=["ax1"], node_id="d1")
    assert g.hot(1)[0].id == "ax1"


def test_bug2_new_node_outranks_old_unaccessed():
    """BUG 2 fixed: newly added node appears above stale zero-access nodes in hot()."""
    g = SoulGraph(name="t")
    for i in range(5):
        n = g.add(f"stale node {i}", tags={"old"})
        g.nodes[n.id].created_at -= 3 * 86400
        g.nodes[n.id].last_accessed -= 3 * 86400
        g.splay.access(n.id, g.nodes[n.id].recency_score())
    new = g.add("fresh experience just now", tags={"new"})
    hot_ids = [n.id for n in g.hot(10) if not n.pinned]
    assert hot_ids[0] == new.id
