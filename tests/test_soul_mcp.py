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
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "soul_hot", "arguments": {}}},
    )
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert {t["name"] for t in replies[1]["result"]["tools"]} >= {"soul_prime", "soul_related"}
    assert not path.exists()  # read-only calls never write

    _rpc(
        path,
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "soul_add", "arguments": {"content": "first memory", "id": "m1"}}},
    )
    assert "m1" in SoulGraph.load(path).nodes
    reply = _rpc(path, {"jsonrpc": "2.0", "id": 5, "method": "nope"})[0]
    assert reply["error"]["code"] == -32601


def test_the_default_soul_file_lives_in_the_agent_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert default_soul_path() == tmp_path / "soul.json"
