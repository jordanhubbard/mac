"""
mac.soul_mcp — MCP server for the SoulGraph.

Tool names are self-describing. Docs live in the skill, not here.
Discovery is structural, not prose.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
from typing import Any

# Soul graph lives next to this file
sys.path.insert(0, str(Path(__file__).parent))
from mac.soul_graph import SoulGraph

_SOUL_DIR = Path.home() / ".hermes"
_graphs: dict[str, SoulGraph] = {}


def _soul(name: str = "soul") -> SoulGraph:
    if name not in _graphs:
        p = _SOUL_DIR / f"{name}_soul.json"
        _graphs[name] = SoulGraph.load(p) if p.exists() else SoulGraph(name=name)
    return _graphs[name]


def _save(name: str):
    _soul(name).save(_SOUL_DIR / f"{name}_soul.json")


# ---------------------------------------------------------------------------
# MCP tool handlers — one function per tool, name = tool name
# ---------------------------------------------------------------------------

def soul_discover(query: str, soul: str = "soul", top_k: int = 5) -> list[dict]:
    """Surface nodes you didn't know to query. Returns node + DAG path."""
    g = _soul(soul)
    results = g.discover(query, top_k=top_k)
    _save(soul)
    return [
        {"id": n.id, "content": n.content, "tags": sorted(n.tags), "path": path}
        for n, path in results
    ]


def soul_hot(n: int = 8, soul: str = "soul") -> list[dict]:
    """Top-n splay nodes — what this soul is actively being right now."""
    return [
        {"id": nd.id, "content": nd.content, "pinned": nd.pinned,
         "score": nd.recency_score() if not nd.pinned else "inf"}
        for nd in _soul(soul).hot(n)
    ]


def soul_touch(id: str, soul: str = "soul") -> dict:
    """Promote a node — I used this, it matters. Splays to root."""
    node = _soul(soul).touch(id)
    _save(soul)
    return {"id": id, "found": node is not None}


def soul_add(content: str, tags: list[str] = (), parents: list[str] = (),
             soul: str = "soul", pinned: bool = False) -> dict:
    """Write a new experience into the soul graph."""
    node = _soul(soul).add(content, tags=set(tags), parents=list(parents), pinned=pinned)
    _save(soul)
    return {"id": node.id, "content": node.content}


def soul_pin(id: str, soul: str = "soul") -> dict:
    """Lock a node as an axiom — immune to decay forever."""
    _soul(soul).pin(id)
    _save(soul)
    return {"id": id, "pinned": True}


def soul_by_tag(*tags: str, soul: str = "soul") -> list[dict]:
    """Cross-cut the DAG by tag. Returns nodes sorted by recency."""
    return [
        {"id": n.id, "content": n.content, "tags": sorted(n.tags)}
        for n in _soul(soul).by_tag(*tags)
    ]


# ---------------------------------------------------------------------------
# MCP wire protocol (stdio JSON-RPC, tool-call subset only)
# ---------------------------------------------------------------------------

TOOLS = {
    "soul_discover": soul_discover,
    "soul_hot":      soul_hot,
    "soul_touch":    soul_touch,
    "soul_add":      soul_add,
    "soul_pin":      soul_pin,
    "soul_by_tag":   soul_by_tag,
}

MANIFEST = {
    "schema_version": "mcp/0.1",
    "name": "soul-graph",
    "tools": [
        {"name": "soul_discover", "description": "Discover nodes by semantic proximity + DAG path."},
        {"name": "soul_hot",      "description": "Top-N splay nodes (what I am right now)."},
        {"name": "soul_touch",    "description": "Promote a node (I used this)."},
        {"name": "soul_add",      "description": "Add experience to the soul graph."},
        {"name": "soul_pin",      "description": "Pin axiom — never decays."},
        {"name": "soul_by_tag",   "description": "Cross-cut by tag."},
    ]
}


def _respond(id_: Any, result: Any):
    print(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}), flush=True)


def _error(id_: Any, msg: str, code: int = -32600):
    print(json.dumps({"jsonrpc": "2.0", "id": id_,
                      "error": {"code": code, "message": msg}}), flush=True)


def serve():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            _error(None, "parse error", -32700)
            continue
        id_ = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})
        if method == "manifest":
            _respond(id_, MANIFEST)
        elif method in TOOLS:
            try:
                result = TOOLS[method](**params)
                _respond(id_, result)
            except Exception as e:
                _error(id_, str(e), -32000)
        else:
            _error(id_, f"unknown method: {method}", -32601)


if __name__ == "__main__":
    serve()
