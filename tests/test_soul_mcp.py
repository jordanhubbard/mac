"""The soul graph's MCP surface, exercised against a real SoulGraph.

An earlier version of these tests stubbed the graph out entirely, which let the
tool layer call methods the engine does not have. These use the engine itself.
"""

from __future__ import annotations

import io
import json

from mac.soul_graph import SoulGraph
from mac.soul_mcp import SoulTools, default_soul_path, serve


def _graph() -> SoulGraph:
    g = SoulGraph(name="t")
    g.add("Be genuinely helpful.", tags={"axiom"}, pinned=True, node_id="a1")
    g.add("Rocky is the fleet hub.", tags={"crew"}, parents=["a1"], node_id="i1")
    g.add("Bullwinkle handles RAG retrieval.", tags={"crew", "rag"}, parents=["i1"], node_id="i2")
    g.add("curl is blocked; use urllib.", tags={"lesson"}, parents=["a1"], node_id="l1")
    return g


def _payload(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_query_touches_hits_and_merges_the_hint():
    g = _graph()
    tools = SoulTools(g)
    data = _payload(tools.soul_query("urllib", hint="RAG retrieval"))
    assert [d["id"] for d in data] == ["l1", "i2"]
    assert g.nodes["l1"].access_count == 1


def test_related_walks_from_hits_to_their_parents():
    data = _payload(SoulTools(_graph()).soul_related("RAG retrieval", top_k=3))
    assert [d["id"] for d in data] == ["i2", "i1"]


def test_fisheye_exposes_near_context_and_boundary_count():
    data = _payload(SoulTools(_graph()).soul_fisheye("RAG retrieval", top_k=1))
    assert [(item["id"], item["hop"]) for item in data["expanded"]] == [
        ("i2", 0),
        ("i1", 1),
    ]
    assert data["boundary_counts"] == {"2": 1}


def test_discover_walks_children_from_an_id_or_text():
    tools = SoulTools(_graph())
    by_id = _payload(tools.soul_discover("a1", hops=2))
    assert {d["id"] for d in by_id["discovered"]} == {"i1", "l1", "i2"}
    by_text = _payload(tools.soul_discover("fleet hub", hops=1))
    assert by_text["seed"] == "i1"
    assert [d["id"] for d in by_text["discovered"]] == ["i2"]


def test_add_link_and_pin_use_the_real_engine():
    g = _graph()
    tools = SoulTools(g)
    added = _payload(tools.soul_add("Natasha runs on the GPU host.", id="i3", parents=["i1"]))
    assert added["parents"] == ["i1"]
    assert tools.soul_add("dup", id="i3").get("isError")
    assert tools.soul_add("orphan", parents=["nope"]).get("isError")
    assert tools.soul_link("i3", "a1").get("isError")  # would be a cycle
    _payload(tools.soul_pin("i3"))
    assert g.nodes["i3"].pinned
    assert tools.soul_pin("missing").get("isError")


def test_soul_unpin_removes_axiom_status():
    import math

    g = _graph()
    tools = SoulTools(g)
    # pin a non-pinned node first
    _payload(tools.soul_add("Stale resolved belief.", id="u1"))
    _payload(tools.soul_pin("u1"))
    assert g.nodes["u1"].pinned
    # unpin it
    result = _payload(tools.soul_unpin("u1"))
    assert result["unpinned"] == "u1"
    assert not g.nodes["u1"].pinned
    # score should be finite again
    assert g.nodes["u1"].recency_score() < math.inf
    # idempotent: already-unpinned node returns a benign response
    result2 = _payload(tools.soul_unpin("u1"))
    assert result2["already_unpinned"] == "u1"
    # missing node is an error
    assert tools.soul_unpin("missing").get("isError")


def test_promote_moves_only_the_named_hypothesis():
    g = _graph()
    tools = SoulTools(g)
    _payload(tools.soul_explore("h1", "quotas cause the stalls"))
    _payload(tools.soul_explore("h2", "it is the network"))
    promoted = _payload(tools.soul_promote("h1", parent_ids=["i1"]))
    assert promoted["parents"] == ["i1"]
    assert "h1" in g.nodes and "h2" not in g.nodes
    assert tools.soul_promote("h1").get("isError")


def test_prime_seeds_from_context_and_walks_its_children():
    data = _payload(SoulTools(_graph()).soul_prime(context="fleet hub", n_hot=2))
    assert data["seed"] == "i1"
    assert [d["id"] for d in data["discovered"]] == ["i2"]
    assert data["hot"][0]["score"] == "pinned"


def test_unknown_tools_and_bad_arguments_are_tool_errors():
    tools = SoulTools(_graph())
    assert tools.dispatch("soul_nope", {}).get("isError")
    assert tools.dispatch("__init__", {}).get("isError")
    assert tools.dispatch("soul_hot", {"bogus": 1}).get("isError")


def _rpc(soul_path, *requests):
    out = io.StringIO()
    serve(soul_path, inp=io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"), out=out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_stdio_server_speaks_mcp_and_saves_only_after_writes(tmp_path):
    path = tmp_path / "soul.json"
    replies = _rpc(
        path,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "soul_hot", "arguments": {}},
        },
    )
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert {t["name"] for t in replies[1]["result"]["tools"]} >= {"soul_prime", "soul_related"}
    assert not path.exists()  # read-only calls never write

    _rpc(
        path,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "soul_add", "arguments": {"content": "first memory", "id": "m1"}},
        },
    )
    assert "m1" in SoulGraph.load(path).nodes
    reply = _rpc(path, {"jsonrpc": "2.0", "id": 5, "method": "nope"})[0]
    assert reply["error"]["code"] == -32601


def test_the_default_soul_file_lives_in_the_agent_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert default_soul_path() == tmp_path / "soul.json"


# ---------------------------------------------------------------------------
# Cross-agent dedup: same concept from two agents → one node, no duplicate
# ---------------------------------------------------------------------------


def _cross_agent_graph() -> SoulGraph:
    """Simulate a graph loaded with --cross-agent: nodes from two agents."""
    g = SoulGraph(name="fleet")
    # Agent Rocky already wrote this axiom
    g.add(
        "AXIOM — hyperlinked memory: every addressable thing gets its canonical ID cited inline on every mention.",
        tags={"axiom", "visibility:fleet-canonical"},
        pinned=True,
        node_id="rocky_ax1",
    )
    # Some unrelated node so search has background noise
    g.add("Rocky is the fleet hub agent running on puck.", tags={"crew"}, node_id="rocky_i1")
    return g


def test_soul_add_dedupes_on_similar_content():
    """When agent Natasha calls soul_add with near-identical axiom content,
    dedup fires and returns the existing node instead of minting a new one."""
    g = _cross_agent_graph()
    tools = SoulTools(g)
    # Natasha tries to add the same axiom in different words
    result = _payload(
        tools.soul_add(
            "AXIOM — hyperlinked memory: every addressable thing gets its canonical ID on every mention.",
            pinned=True,
        )
    )
    assert result.get("deduped") is True, "Expected dedup to fire"
    assert result["existing_node"] == "rocky_ax1"
    assert result["similarity"] >= SoulTools._DEDUP_THRESHOLD
    # Node count must not have grown
    assert len(g.nodes) == 2


def test_soul_add_mints_when_no_match():
    """Distinct content below threshold → normal mint."""
    g = _cross_agent_graph()
    tools = SoulTools(g)
    result = _payload(
        tools.soul_add("Bullwinkle handles Qdrant RAG embeddings for the fleet.", id="b1")
    )
    assert "deduped" not in result
    assert result["id"] == "b1"
    assert len(g.nodes) == 3


def test_soul_add_pinned_gets_fleet_canonical_tag_when_no_visibility():
    """Pinned node without a visibility: tag → auto-tagged visibility:fleet-canonical."""
    g = SoulGraph(name="t")
    tools = SoulTools(g)
    result = _payload(
        tools.soul_add("Agents must file all work as MAC tasks.", pinned=True, id="p1")
    )
    assert "deduped" not in result
    assert "visibility:fleet-canonical" in result["tags"]


def test_soul_add_pinned_respects_explicit_visibility_tag():
    """Pinned node with explicit visibility:private → not overridden."""
    g = SoulGraph(name="t")
    tools = SoulTools(g)
    result = _payload(
        tools.soul_add(
            "Private axiom for natasha only.",
            pinned=True,
            tags=["visibility:private"],
            id="p2",
        )
    )
    assert "visibility:fleet-canonical" not in result["tags"]
    assert "visibility:private" in result["tags"]


# ---------------------------------------------------------------------------
# Bug fixes: soul_link relation parameter and typed-edge round-trip
# ---------------------------------------------------------------------------


def test_soul_link_relation_in_tool_spec():
    """Bug 1 fix: soul_link TOOL_SPEC must expose the `relation` property."""
    spec = next(s for s in SoulTools.TOOL_SPECS if s["name"] == "soul_link")
    props = spec["inputSchema"]["properties"]
    assert "relation" in props, (
        "soul_link TOOL_SPEC missing 'relation' property — MCP clients cannot pass typed edges"
    )
    assert props["relation"]["type"] == "string"
    assert "contradicts" in props["relation"]["enum"]
    assert "depends_on" in props["relation"]["enum"]


def test_soul_link_relation_stored_in_child_metadata():
    """soul_link with a relation type must store it in child.metadata['parent_rels']."""
    g = _graph()
    tools = SoulTools(g)
    result = _payload(tools.soul_link("a1", "i1", relation="depends_on"))
    assert result.get("relation") == "depends_on"
    # The engine must have stored the rel on the child node
    assert g.nodes["i1"].metadata.get("parent_rels", {}).get("a1") == "depends_on"


def test_soul_link_contradicts_relation():
    """contradicts relation (repulsion type) round-trips through soul_link."""
    g = _graph()
    tools = SoulTools(g)
    # Add a node that contradicts an existing one
    tools.soul_add("curl works fine actually.", id="contra1")
    result = _payload(tools.soul_link("a1", "contra1", relation="contradicts"))
    assert result.get("relation") == "contradicts"
    assert g.nodes["contra1"].metadata.get("parent_rels", {}).get("a1") == "contradicts"


# ---------------------------------------------------------------------------
# soul_search: hybrid FTS + vector search with graceful fallback
# ---------------------------------------------------------------------------


def test_soul_search_fallback_when_hybrid_unavailable(monkeypatch):
    """When semantic_world_search is not importable, soul_search falls back to
    soul_query FTS and signals the reason in _hybrid_unavailable."""
    import sys

    # Remove semantic_world_search from sys.modules so the import fails
    monkeypatch.setitem(sys.modules, "mac.semantic_world_search", None)

    g = _graph()
    tools = SoulTools(g)
    result = tools.soul_search("RAG retrieval")
    # Fallback must NOT be an isError response
    assert not result.get("isError"), result
    # Must carry the fallback key
    assert "_hybrid_unavailable" in result, (
        "soul_search fallback must include '_hybrid_unavailable' explanation key"
    )
    # Must still have content (FTS results via soul_query)
    payload = json.loads(result["content"][0]["text"])
    assert isinstance(payload, list)


def test_soul_search_fallback_touches_nodes(monkeypatch):
    """Fallback path (FTS) must still touch matched nodes for splay ordering."""
    import sys

    monkeypatch.setitem(sys.modules, "mac.semantic_world_search", None)

    g = _graph()
    tools = SoulTools(g)
    # Reset access counts
    for n in g.nodes.values():
        n.access_count = 0
    tools.soul_search("urllib")
    # FTS on "urllib" should hit l1; that node's access count must have risen
    assert g.nodes["l1"].access_count >= 1, (
        "soul_search fallback must touch matched nodes via soul_query"
    )


def test_soul_search_with_hybrid_path(monkeypatch):
    """When hybrid_search is importable and returns results, soul_search
    uses them and returns the right schema (id, agent, content, tags, score,
    method, rank)."""
    from dataclasses import dataclass, field
    from typing import List, Dict, Any

    @dataclass
    class _FakeResult:
        id: str
        agent: str
        content: str
        tags: List[str]
        score: float
        method: str
        rank: int = 0
        extra: Dict[str, Any] = field(default_factory=dict)

    fake_results = [
        _FakeResult(
            "i2",
            "bullwinkle",
            "Bullwinkle handles RAG retrieval.",
            ["crew", "rag"],
            0.9,
            "hybrid",
            0,
        ),
        _FakeResult(
            "l1", "bullwinkle", "curl is blocked; use urllib.", ["lesson"], 0.7, "hybrid", 1
        ),
    ]

    import types

    fake_module = types.ModuleType("mac.semantic_world_search")

    def _hybrid_search(query, **kwargs):
        return fake_results

    fake_module.hybrid_search = _hybrid_search

    import sys

    monkeypatch.setitem(sys.modules, "mac.semantic_world_search", fake_module)

    g = _graph()
    tools = SoulTools(g)
    result = tools.soul_search("RAG retrieval")
    assert not result.get("isError"), result
    assert "_hybrid_unavailable" not in result, "Should not fall back when hybrid works"
    payload = json.loads(result["content"][0]["text"])
    assert isinstance(payload, list)
    assert len(payload) == 2
    # Check required schema fields
    for item in payload:
        for field_name in ("id", "agent", "content", "tags", "score", "method", "rank"):
            assert field_name in item, f"Missing field '{field_name}' in soul_search result"
    assert payload[0]["id"] == "i2"
    assert payload[0]["method"] == "hybrid"


def test_soul_search_in_tool_spec():
    """soul_search must be in the TOOL_SPECS with the right schema."""
    spec = next((s for s in SoulTools.TOOL_SPECS if s["name"] == "soul_search"), None)
    assert spec is not None, "soul_search missing from TOOL_SPECS"
    props = spec["inputSchema"]["properties"]
    assert "query" in props
    assert "limit" in props
    assert "token" in props
    assert spec["inputSchema"].get("required") == ["query"]


def test_soul_search_dispatches_via_mcp():
    """soul_search must be callable through the MCP dispatch layer."""
    import sys

    # Force fallback path so we don't need a real DB
    sys.modules.pop("mac.semantic_world_search", None)
    # If it was None (monkeypatched), remove to allow normal import attempt
    # In this test we just need dispatch to route correctly without isError on bad-method
    g = _graph()
    tools = SoulTools(g)
    result = tools.dispatch("soul_search", {"query": "fleet hub"})
    # Even in fallback, must not be a tool-not-found error
    assert not (result.get("isError") and "unknown tool" in str(result).lower()), (
        "soul_search must be a registered dispatch target"
    )


# ---------------------------------------------------------------------------
# soul_path MCP tool tests
# ---------------------------------------------------------------------------


def test_soul_path_in_tool_spec():
    """soul_path must be declared in TOOL_SPECS with correct schema."""
    spec = next((s for s in SoulTools.TOOL_SPECS if s["name"] == "soul_path"), None)
    assert spec is not None, "soul_path missing from TOOL_SPECS"
    props = spec["inputSchema"]["properties"]
    assert "from_id" in props, "soul_path inputSchema must have from_id"
    assert "to_id" in props, "soul_path inputSchema must have to_id"
    assert set(spec["inputSchema"].get("required", [])) == {"from_id", "to_id"}


def test_soul_path_connected_nodes():
    """soul_path returns ordered path for a connected DAG chain."""
    g = _graph()
    tools = SoulTools(g)
    result = _payload(tools.soul_path("a1", "i2"))
    assert result["connected"] is True
    ids = [n["id"] for n in result["path"]]
    assert ids[0] == "a1"
    assert ids[-1] == "i2"
    assert result["length"] == len(ids)


def test_soul_path_unconnected_nodes_returns_empty():
    """soul_path returns empty path when no directed route exists."""
    g = _graph()
    tools = SoulTools(g)
    # No path from i2 back to a1 in a DAG
    result = _payload(tools.soul_path("i2", "a1"))
    assert result["connected"] is False
    assert result["path"] == []
    assert result["length"] == 0


def test_soul_path_missing_node_returns_error():
    """soul_path returns isError for a missing node."""
    g = _graph()
    tools = SoulTools(g)
    result = tools.soul_path("a1", "does_not_exist")
    assert result.get("isError"), "Expected isError for missing target node"


def test_soul_path_dispatches_via_mcp():
    """soul_path is callable through the MCP dispatch layer."""
    g = _graph()
    tools = SoulTools(g)
    result = tools.dispatch("soul_path", {"from_id": "a1", "to_id": "i1"})
    assert not (result.get("isError") and "unknown tool" in str(result).lower()), (
        "soul_path must be a registered dispatch target"
    )


# ---------------------------------------------------------------------------
# soul_ancestors MCP tool tests
# ---------------------------------------------------------------------------

def test_soul_ancestors_in_tool_spec():
    """soul_ancestors must be declared in TOOL_SPECS with correct schema."""
    spec = next((s for s in SoulTools.TOOL_SPECS if s["name"] == "soul_ancestors"), None)
    assert spec is not None, "soul_ancestors missing from TOOL_SPECS"
    props = spec["inputSchema"]["properties"]
    assert "node_id" in props, "soul_ancestors inputSchema must have node_id"
    assert "node_id" in spec["inputSchema"].get("required", [])


def test_soul_ancestors_leaf_node():
    """A leaf node (no parents) should return an empty ancestor set."""
    g = _graph()
    tools = SoulTools(g)
    data = _payload(tools.soul_ancestors("a1"))
    assert data["node_id"] == "a1"
    assert data["ancestor_count"] == 0
    assert data["ancestors"] == []


def test_soul_ancestors_child_returns_parent():
    """A node with one parent should return exactly that parent."""
    g = _graph()
    tools = SoulTools(g)
    data = _payload(tools.soul_ancestors("i1"))
    ids = [n["id"] for n in data["ancestors"]]
    assert "a1" in ids, "Direct parent a1 must appear in ancestors of i1"
    assert data["ancestor_count"] == len(data["ancestors"])


def test_soul_ancestors_deep_node_returns_full_chain():
    """A node with grandparent chain should return all transitive ancestors."""
    g = _graph()
    tools = SoulTools(g)
    # i2 -> i1 -> a1
    data = _payload(tools.soul_ancestors("i2"))
    ids = set(n["id"] for n in data["ancestors"])
    assert "i1" in ids, "i1 must appear as ancestor of i2"
    assert "a1" in ids, "a1 must appear as transitive ancestor of i2"
    # seed node itself must NOT be included
    assert "i2" not in ids, "soul_ancestors must not include the seed node"


def test_soul_ancestors_missing_node_returns_error():
    """soul_ancestors returns isError for a node that does not exist."""
    g = _graph()
    tools = SoulTools(g)
    result = tools.soul_ancestors("does_not_exist")
    assert result.get("isError"), "Expected isError for missing node_id"


def test_soul_ancestors_dispatches_via_mcp():
    """soul_ancestors is callable through the MCP dispatch layer."""
    g = _graph()
    tools = SoulTools(g)
    result = tools.dispatch("soul_ancestors", {"node_id": "i2"})
    assert not (result.get("isError") and "unknown tool" in str(result).lower()), (
        "soul_ancestors must be a registered dispatch target"
    )


def test_soul_ancestors_excludes_seed_sibling():
    """Siblings of the seed (same parent, different child) must not appear."""
    g = _graph()
    # l1 is a sibling of i1 under a1; it should not appear in i1's ancestors
    tools = SoulTools(g)
    data = _payload(tools.soul_ancestors("i1"))
    ids = [n["id"] for n in data["ancestors"]]
    assert "l1" not in ids, "Sibling l1 must not appear as ancestor of i1"
# soul_hot: top-N splay nodes
# ---------------------------------------------------------------------------


def test_soul_hot_returns_top_n_by_splay_score():
    """soul_hot returns top-n nodes ordered by splay score (pinned=inf beats touched)."""
    # Use an unpinned-only graph so recency order is unambiguous
    from mac.soul_graph import SoulGraph
    g2 = SoulGraph(name="hot_test")
    g2.add("Node A.", node_id="ha")
    g2.add("Node B.", node_id="hb")
    g2.add("Node C.", node_id="hc")
    tools2 = SoulTools(g2)
    # touch hb twice → highest recency; hc once → second; ha untouched
    g2.touch("hb")
    g2.touch("hb")
    g2.touch("hc")
    result = _payload(tools2.soul_hot(n=2))
    assert isinstance(result, list)
    assert len(result) <= 2
    ids = [item["id"] for item in result]
    # hb touched most recently/frequently → first; hc second
    assert ids[0] == "hb", f"Expected hb first (most touched), got {ids}"
    assert len(ids) == 2


def test_soul_hot_includes_score_field():
    """soul_hot result items must carry a 'score' field (pinned or numeric)."""
    g = _graph()
    tools = SoulTools(g)
    result = _payload(tools.soul_hot(n=3))
    for item in result:
        assert "score" in item, f"Item {item['id']} missing 'score' field"
        assert "id" in item


def test_soul_hot_in_tool_spec():
    """soul_hot must be declared in TOOL_SPECS."""
    spec = next((s for s in SoulTools.TOOL_SPECS if s["name"] == "soul_hot"), None)
    assert spec is not None, "soul_hot missing from TOOL_SPECS"
    props = spec["inputSchema"]["properties"]
    assert "n" in props


def test_soul_hot_dispatches_via_mcp():
    """soul_hot is callable through the MCP dispatch layer."""
    g = _graph()
    tools = SoulTools(g)
    result = tools.dispatch("soul_hot", {})
    assert not result.get("isError"), result


# ---------------------------------------------------------------------------
# soul_by_tag: cross-cut on a tag
# ---------------------------------------------------------------------------


def test_soul_by_tag_returns_nodes_with_matching_tag():
    """soul_by_tag must return all nodes tagged with the given tag."""
    g = _graph()
    tools = SoulTools(g)
    result = _payload(tools.soul_by_tag("crew"))
    assert result["tag"] == "crew"
    ids = {item["id"] for item in result["nodes"]}
    assert "i1" in ids
    assert "i2" in ids
    assert "a1" not in ids  # tagged axiom, not crew
    assert result["count"] == len(result["nodes"])


def test_soul_by_tag_empty_for_unknown_tag():
    """soul_by_tag returns an empty nodes list for a tag that has no nodes."""
    g = _graph()
    tools = SoulTools(g)
    result = _payload(tools.soul_by_tag("nonexistent_xyz_tag"))
    assert result["count"] == 0
    assert result["nodes"] == []


def test_soul_by_tag_in_tool_spec():
    """soul_by_tag must be declared in TOOL_SPECS with required 'tag'."""
    spec = next((s for s in SoulTools.TOOL_SPECS if s["name"] == "soul_by_tag"), None)
    assert spec is not None, "soul_by_tag missing from TOOL_SPECS"
    assert "tag" in spec["inputSchema"].get("required", [])


def test_soul_by_tag_dispatches_via_mcp():
    """soul_by_tag is callable through the MCP dispatch layer."""
    g = _graph()
    tools = SoulTools(g)
    result = tools.dispatch("soul_by_tag", {"tag": "lesson"})
    assert not result.get("isError"), result
    payload = json.loads(result["content"][0]["text"])
    assert payload["tag"] == "lesson"


# ---------------------------------------------------------------------------
# soul_splay: explicit node promotion
# ---------------------------------------------------------------------------


def test_soul_splay_increments_access_count():
    """soul_splay must increment the node's access_count."""
    g = _graph()
    tools = SoulTools(g)
    before = g.nodes["i1"].access_count
    result = _payload(tools.soul_splay("i1"))
    assert result["id"] == "i1"
    assert g.nodes["i1"].access_count == before + 1


def test_soul_splay_returns_score():
    """soul_splay must include 'score' in its response."""
    g = _graph()
    tools = SoulTools(g)
    result = _payload(tools.soul_splay("a1"))
    assert "score" in result
    # pinned nodes report 'pinned' as their score string
    assert result["score"] == "pinned"


def test_soul_splay_missing_node_returns_error():
    """soul_splay with a nonexistent node_id must return isError."""
    g = _graph()
    tools = SoulTools(g)
    result = tools.soul_splay("does_not_exist")
    assert result.get("isError"), "Expected isError for missing node"


def test_soul_splay_in_tool_spec():
    """soul_splay must be declared in TOOL_SPECS with required 'node_id'."""
    spec = next((s for s in SoulTools.TOOL_SPECS if s["name"] == "soul_splay"), None)
    assert spec is not None, "soul_splay missing from TOOL_SPECS"
    assert "node_id" in spec["inputSchema"].get("required", [])


def test_soul_splay_dispatches_via_mcp():
    """soul_splay is callable through the MCP dispatch layer."""
    g = _graph()
    tools = SoulTools(g)
    result = tools.dispatch("soul_splay", {"node_id": "i2"})
    assert not result.get("isError"), result


# ---------------------------------------------------------------------------
# soul_summary: graph-wide stats
# ---------------------------------------------------------------------------


def test_soul_summary_contains_expected_keys():
    """soul_summary must mention node count, axiom count, and tags in its output."""
    g = _graph()
    tools = SoulTools(g)
    result = _payload(tools.soul_summary())
    # soul_summary returns a human-readable string
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert "nodes" in result, "soul_summary must mention node count"
    assert "Pinned axioms" in result, "soul_summary must mention pinned axiom count"
    assert "Tags" in result, "soul_summary must mention tags"


def test_soul_summary_counts_match_graph():
    """soul_summary output must reflect actual graph node and axiom counts."""
    g = _graph()
    tools = SoulTools(g)
    result = _payload(tools.soul_summary())
    # e.g. "SoulGraph 't' — 4 nodes"
    assert str(len(g.nodes)) in result, f"Expected {len(g.nodes)} nodes in summary"
    # a1 is pinned → at least 1 pinned axiom
    assert "Pinned axioms: 1" in result, "Expected at least 1 pinned axiom in summary"


def test_soul_summary_in_tool_spec():
    """soul_summary must be declared in TOOL_SPECS."""
    spec = next((s for s in SoulTools.TOOL_SPECS if s["name"] == "soul_summary"), None)
    assert spec is not None, "soul_summary missing from TOOL_SPECS"


def test_soul_summary_dispatches_via_mcp():
    """soul_summary is callable through the MCP dispatch layer."""
    g = _graph()
    tools = SoulTools(g)
    result = tools.dispatch("soul_summary", {})
    assert not result.get("isError"), result
