"""Tests for soul_mcp — uses an in-memory SoulGraph stub."""
from __future__ import annotations
import json, math, io, sys, types
from unittest.mock import MagicMock

# Stub soul_graph before import
stub_node = MagicMock()
stub_node.id = "test_node"
stub_node.content = "be genuinely helpful"
stub_node.tags = {"axiom", "identity"}
stub_node.parents = []
stub_node.pinned = True
stub_node.access_count = 3
stub_node.recency_score.return_value = math.inf

stub_graph = MagicMock()
stub_graph.semantic_search.return_value = [stub_node]
stub_graph.hot.return_value = [stub_node]
stub_graph.discover.return_value = [stub_node]
stub_graph.by_tag.return_value = [stub_node]
stub_graph.touch.return_value = stub_node
stub_graph.get.return_value = stub_node
stub_graph.link.return_value = True
stub_graph.summary.return_value = {"nodes": 5, "pinned": 1}

# Inject stub module
mac_mod = types.ModuleType("mac")
sg_mod = types.ModuleType("mac.soul_graph")
sys.modules.setdefault("mac", mac_mod)
sys.modules["mac.soul_graph"] = sg_mod

from mac.soul_mcp import SoulTools, _serve  # noqa: E402


def tools():
    return SoulTools(stub_graph)


def test_soul_query_returns_results():
    r = tools().soul_query("helpful")
    data = json.loads(r["content"][0]["text"])
    assert data[0]["id"] == "test_node"


def test_soul_query_hint_merges():
    # hint triggers second search pass; stub returns same node so dedup applies
    r = tools().soul_query("helpful", hint="architecture")
    data = json.loads(r["content"][0]["text"])
    assert len(data) == 1  # deduped


def test_soul_hot():
    r = tools().soul_hot(n=3)
    data = json.loads(r["content"][0]["text"])
    assert data[0]["id"] == "test_node"


def test_soul_discover():
    r = tools().soul_discover("test_node", hops=2)
    data = json.loads(r["content"][0]["text"])
    assert data["seed"] == "test_node"
    assert len(data["discovered"]) == 1


def test_soul_discover_empty():
    stub_graph.discover.return_value = []
    r = tools().soul_discover("missing", hops=1)
    data = json.loads(r["content"][0]["text"])
    assert data["discovered"] == []
    stub_graph.discover.return_value = [stub_node]


def test_soul_by_tag():
    r = tools().soul_by_tag("axiom")
    data = json.loads(r["content"][0]["text"])
    assert data["tag"] == "axiom"
    assert data["count"] == 1


def test_soul_splay():
    r = tools().soul_splay("test_node")
    data = json.loads(r["content"][0]["text"])
    assert data["id"] == "test_node"


def test_soul_splay_missing():
    stub_graph.touch.return_value = None
    r = tools().soul_splay("ghost")
    assert r["isError"]
    stub_graph.touch.return_value = stub_node


def test_soul_pin():
    r = tools().soul_pin("test_node")
    data = json.loads(r["content"][0]["text"])
    assert data["pinned"] == "test_node"


def test_soul_pin_missing():
    stub_graph.get.return_value = None
    r = tools().soul_pin("ghost")
    assert r["isError"]
    stub_graph.get.return_value = stub_node


def test_soul_summary():
    r = tools().soul_summary()
    data = json.loads(r["content"][0]["text"])
    assert data["nodes"] == 5


def test_serve_initialize(tmp_path):
    inp = io.StringIO(json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}) + "\n")
    out = io.StringIO()
    _serve(tmp_path / "soul.json", inp=inp, out=out)
    resp = json.loads(out.getvalue())
    assert resp["result"]["serverInfo"]["name"] == "mac-soul"


def test_serve_tools_list(tmp_path):
    inp = io.StringIO(json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}) + "\n")
    out = io.StringIO()
    _serve(tmp_path / "soul.json", inp=inp, out=out)
    resp = json.loads(out.getvalue())
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "soul_query" in names
    assert "soul_discover" in names
    assert "soul_hot" in names
