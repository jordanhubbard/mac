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
    g.add(
        "Lessons: curl is blocked, use urllib.",
        tags={"lesson", "tooling"},
        parents=["a1"],
        node_id="l1",
    )
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
    g._rekey(g.nodes[old.id])

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
        g._rekey(g.nodes[n.id])
    new = g.add("fresh experience just now", tags={"new"})
    hot_ids = [n.id for n in g.hot(10) if not n.pinned]
    assert hot_ids[0] == new.id


# ---------------------------------------------------------------------------
# Replayable clock, search ranking, DAG integrity, real-DAG import
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "soul_graph"


class _Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_an_injected_clock_stamps_and_ages_nodes():
    clock = _Clock()
    g = SoulGraph(clock=clock)
    n = g.add("first")
    assert n.created_at == n.last_accessed == clock.now
    clock.now += 86400
    assert g.nodes[n.id].recency_score(clock.now) == pytest.approx(math.exp(-1))
    g.touch(n.id)
    assert g.nodes[n.id].last_accessed == clock.now


def test_hot_matches_recency_order_at_every_time_without_reranking():
    """The splay key is time-invariant, so hot() is a plain read of the tree and
    still agrees with recency_score() however much time has passed."""
    import random

    rnd = random.Random(7)
    clock = _Clock()
    g = SoulGraph(clock=clock)
    ids = []
    for i in range(60):
        clock.now += rnd.uniform(0, 7200)
        ids.append(g.add("node %d" % i, node_id="n%d" % i).id)
        for _ in range(rnd.randint(0, 3)):
            g.touch(rnd.choice(ids))
    g.pin("n5")
    for later in (0, 3600, 5 * 86400, 60 * 86400):
        clock.now += later
        expected = sorted(g.nodes.values(), key=lambda n: n.recency_score(clock.now), reverse=True)
        hot = g.hot(15)
        assert hot[0].id == "n5"
        scores = [n.recency_score(clock.now) for n in hot]
        assert scores == sorted(scores, reverse=True)
        assert scores == [n.recency_score(clock.now) for n in expected[:15]]


def test_search_weights_rare_words_over_common_ones():
    g = SoulGraph()
    for i in range(20):
        g.add("fix the worker test %d" % i)
    rare = g.add("fix the sandbox mount")
    hits = g.semantic_search("fix sandbox", top_k=3, recency_weight=0)
    assert hits[0][0].id == rare.id


def test_search_ignores_punctuation_and_stopwords():
    g = SoulGraph()
    n = g.add("Lessons: curl is blocked, use urllib.")
    assert g.semantic_search("urllib", top_k=1)[0][0].id == n.id
    assert g.semantic_search("is the of", top_k=1) == []


def test_search_can_be_restricted_to_candidates():
    g = SoulGraph()
    a = g.add("deploy the hub", tags={"project:a"})
    g.add("deploy the hub", tags={"project:b"})
    hits = g.semantic_search("deploy hub", top_k=5, candidates=g.tagged("project:a"))
    assert [n.id for n, _ in hits] == [a.id]


def test_related_returns_hits_then_their_parents_and_children():
    g = SoulGraph()
    g.add("foundation decision", node_id="root")
    g.add("sibling that names the sandbox", parents=["root"], node_id="sib")
    g.add("follow-up work", parents=["sib"], node_id="child")
    ids = [n.id for n in g.related("sandbox", top_k=5)]
    assert ids == ["sib", "root", "child"]


def test_fisheye_expands_one_hop_and_counts_the_two_hop_boundary():
    g = SoulGraph()
    g.add("foundation decision", node_id="root")
    g.add("sandbox implementation", parents=["root"], node_id="focus")
    g.add("follow-up", parents=["focus"], node_id="child")
    g.add("deeper detail", parents=["child"], node_id="deep")

    result = g.fisheye("sandbox", top_k=1)

    assert [(node.id, hop) for node, hop in result["expanded"]] == [
        ("focus", 0),
        ("root", 1),
        ("child", 1),
    ]
    assert result["boundary_counts"] == {2: 1}


def test_link_refuses_cycles_and_missing_nodes():
    g = SoulGraph()
    g.add("a", node_id="a")
    g.add("b", parents=["a"], node_id="b")
    assert g.link("b", "a") is False
    assert g.link("a", "a") is False
    assert g.link("a", "missing") is False
    assert g.nodes["a"].parents == []


def test_add_refuses_a_duplicate_id():
    g = SoulGraph()
    g.add("a", node_id="a")
    with pytest.raises(ValueError):
        g.add("again", node_id="a")


def test_merge_keeps_the_word_index_and_reverse_edges():
    g = SoulGraph()
    g.add("base", node_id="base")
    b = g.branch()
    b.add("hypothesis about quotas", parents=["base"], node_id="h")
    g.merge_from(b)
    assert g.semantic_search("quotas", top_k=1)[0][0].id == "h"
    assert "h" in g.nodes["base"].children
    b.nodes["base"].content = "renamed root"
    g.merge_from(b, new_only=False)
    assert g.semantic_search("renamed", top_k=1)[0][0].id == "base"
    assert g.semantic_search("base", top_k=1) == []


def test_save_is_atomic_and_load_rebuilds_the_indexes(tmp_path):
    g = make_soul()
    path = tmp_path / "soul.json"
    g.save(path)
    assert not (tmp_path / "soul.json.tmp").exists()
    loaded = SoulGraph.load(path)
    assert loaded.semantic_search("urllib", top_k=1)[0][0].id == "l1"
    assert loaded.hot(1)[0].pinned


def test_a_real_reasoning_dag_imports_and_is_traversable():
    """natasha's hand-built DAG (2026-10-05) in the portable nodes/edges form."""
    data = json.loads((FIXTURES / "laws-dag.json").read_text())
    g = SoulGraph.from_dag(data, name="laws")
    assert len(g.nodes) == len(data["nodes"])
    assert g.dropped_edges == []
    assert {n.id for n in g.nodes.values() if n.pinned} == {
        "unpredictable",
        "target-human",
        "human-control",
    }
    assert g.nodes["vivaldi"].metadata["parent_rels"] == {
        "ark1": "component",
        "autodrone": "precursor",
    }
    assert g.tagged("event") == {"vivaldi", "ark1", "autodrone"}
    related = [n.id for n in g.related("Operation Vivaldi", top_k=6)]
    assert related[0] == "vivaldi"
    assert {"ark1", "autodrone"} <= set(related)
    assert "diffusion" in related
    assert g.path("laws", "diffusion") == ["laws", "escalation", "diffusion"]


def test_from_dag_drops_edges_that_would_close_a_cycle():
    g = SoulGraph.from_dag(
        {
            "nodes": [{"id": "a", "label": "a"}, {"id": "b", "label": "b"}],
            "edges": [
                {"from": "a", "to": "b"},
                {"from": "b", "to": "a"},
                {"from": "a", "to": "zz"},
            ],
        }
    )
    assert g.nodes["b"].parents == ["a"]
    assert len(g.dropped_edges) == 2


# ── soul_graph.summary() health metrics ───────────────────────────────────────


class TestSummaryHealthMetrics:
    """summary() must report edge health: orphan count, isolated ratio, edge-type dist."""

    def _make_graph(self) -> SoulGraph:  # type: ignore[name-defined]
        """Small graph: root → child1 (depends_on), child2 (child), orphan (no edges)."""
        g = SoulGraph("test-health")  # type: ignore[name-defined]
        g.add("root node", node_id="root", tags=["identity"])
        g.add("child one", parents=["root"], node_id="c1")
        g.add("child two", parents=["root"], node_id="c2")
        g.link("root", "c1", "depends_on")
        g.add("orphan node", node_id="orph")  # no parents, no children
        return g

    def test_summary_contains_orphan_count(self):
        g = self._make_graph()
        s = g.summary()
        assert "orphan" in s.lower(), f"summary() must report orphan count; got:\n{s}"

    def test_summary_contains_isolated_ratio(self):
        g = self._make_graph()
        s = g.summary()
        # 1 orphan out of 4 nodes = 25%
        assert "25%" in s or "isolated" in s.lower(), (
            f"summary() must report isolated node ratio; got:\n{s}"
        )

    def test_summary_contains_edge_type_distribution(self):
        g = self._make_graph()
        s = g.summary()
        # depends_on relation should appear
        assert "depends_on" in s or "edge type" in s.lower(), (
            f"summary() must show edge-type distribution; got:\n{s}"
        )

    def test_summary_no_edge_type_when_no_typed_edges(self):
        g = SoulGraph("empty-typed")  # type: ignore[name-defined]
        g.add("node a", node_id="a1")
        g.add("node b", parents=["a1"], node_id="b1")
        s = g.summary()
        # Should not crash; edge type block may show 'child' default or be absent
        assert "orphan" in s.lower(), f"summary() must still report orphan info; got:\n{s}"


# ---------------------------------------------------------------------------
# soul_path: BFS shortest path through the DAG
# ---------------------------------------------------------------------------


class TestSoulPath:
    """Tests for SoulGraph.path() — shortest directed path between two nodes."""

    def _make_graph(self) -> SoulGraph:
        g = SoulGraph("path-test")
        g.add("root belief", node_id="root")
        g.add("child of root", parents=["root"], node_id="c1")
        g.add("grandchild of root", parents=["c1"], node_id="gc1")
        g.add("sibling of child", parents=["root"], node_id="c2")
        g.add("orphan node", node_id="orphan")
        return g

    def test_path_finds_direct_child(self):
        g = self._make_graph()
        p = g.path("root", "c1")
        assert p == ["root", "c1"], f"Expected ['root', 'c1'] but got {p}"

    def test_path_finds_two_hop_descendant(self):
        g = self._make_graph()
        p = g.path("root", "gc1")
        assert p == ["root", "c1", "gc1"], f"Expected 2-hop path but got {p}"

    def test_path_returns_empty_for_no_route(self):
        g = self._make_graph()
        # No path from child back to parent in a DAG
        p = g.path("c1", "root")
        assert p == [], f"Expected empty path (no upward route in DAG) but got {p}"

    def test_path_returns_empty_to_orphan(self):
        g = self._make_graph()
        p = g.path("root", "orphan")
        assert p == [], f"Expected empty path to unconnected orphan but got {p}"

    def test_path_missing_node_returns_empty(self):
        g = self._make_graph()
        p = g.path("root", "does_not_exist")
        assert p == [], f"Expected empty path for missing node but got {p}"

    def test_path_self_to_self(self):
        g = self._make_graph()
        # BFS: start == goal, return immediately
        p = g.path("root", "root")
        assert p == ["root"], f"Expected ['root'] for same-node path but got {p}"


class TestContradictRepulsion:
    """contradicts-typed edges push repulsed neighbours to end of related() results."""

    def test_contradicts_neighbour_pushed_to_end(self):
        """The 'contradicts' target must not appear before unrelated nodes."""
        g = SoulGraph()
        # Main node — will be the query hit
        main = g.add("machine learning gradient descent optimisation")
        # A related-but-contradicted node: semantically similar but conceptually opposed
        contradiction = g.add(
            "machine learning gradient descent harmful overfitting causes divergence"
        )
        # A neutral sibling: also semantically related
        sibling = g.add("machine learning gradient descent momentum acceleration")

        # Wire: main contradicts contradiction; main → sibling is a plain child edge
        g.link(main.id, contradiction.id, rel="contradicts")
        g.link(main.id, sibling.id)  # neutral edge

        results = g.related("gradient descent optimisation", top_k=10)
        ids = [n.id for n in results]

        # All three nodes must appear (repulsion ≠ exclusion)
        assert main.id in ids, "main hit must appear"
        assert contradiction.id in ids, "contradicts node must still appear"
        assert sibling.id in ids, "neutral sibling must appear"

        # The contradicted node must come AFTER the neutral sibling
        assert ids.index(sibling.id) < ids.index(contradiction.id), (
            f"neutral sibling at pos {ids.index(sibling.id)} should precede "
            f"contradicts node at pos {ids.index(contradiction.id)}"
        )

    def test_untyped_edge_not_repulsed(self):
        """Untyped edges must not be treated as repulsion."""
        g = SoulGraph()
        parent = g.add("neural network training process backpropagation")
        child = g.add("neural network training gradient computation")
        g.link(parent.id, child.id)  # no rel — neutral

        results = g.related("neural network training", top_k=10)
        ids = [n.id for n in results]
        # Both must appear; child must NOT be pushed to end (it's a hit or close sibling)
        assert parent.id in ids
        assert child.id in ids

    def test_contradicts_in_reverse_direction(self):
        """Repulsion fires when child.metadata['parent_rels'][parent_id] == 'contradicts',
        i.e. the edge goes parent → child where parent is the query hit."""
        g = SoulGraph()
        query_hit = g.add("reinforcement learning reward shaping policy gradient")
        repulsed = g.add("reinforcement learning reward shaping policy gradient")
        # Override id to avoid dedup; use explicit node_id to control
        g2 = SoulGraph()
        a = g2.add("reinforcement learning reward shaping policy gradient", node_id="aaa11111")
        b = g2.add(
            "reinforcement learning reward hacking divergence failure mode", node_id="bbb22222"
        )
        c = g2.add("reinforcement learning reward shaping convergence stable", node_id="ccc33333")
        g2.link(a.id, b.id, rel="contradicts")
        g2.link(a.id, c.id)  # neutral

        results = g2.related("reinforcement learning reward shaping", top_k=10)
        ids = [n.id for n in results]
        # neutral child must come before contradicted child
        if c.id in ids and b.id in ids:
            assert ids.index(c.id) < ids.index(b.id), (
                "neutral child must precede contradicts child in results"
            )
