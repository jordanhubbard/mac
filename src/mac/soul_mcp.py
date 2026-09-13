"""MCP tool surface for the SoulGraph.

Exposes the soul graph as typed MCP tools that Hermes (and any coding agent)
can call during a session. Follows the same JSON-RPC 2.0 / stdio pattern as
mac.mcp_server — no extra dependencies, no separate process.

Tools
-----
soul_query      — RAG-style semantic search with optional MCP context hints
soul_hot        — top-N splay nodes (who you are right now)
soul_discover   — DAG walk from a seed node (the needful you didn't know you were)
soul_by_tag     — cross-cut the graph on a tag
soul_splay      — explicitly access (splay) a node, promoting it
soul_pin        — pin a node as an axiom (never decays)
soul_explore    — add a hypothesis to the exploration branch
soul_promote    — graduate a hypothesis from exploration into the core graph
soul_add        — add a new node with optional DAG parents and tags
soul_link       — add a DAG edge between two existing nodes
soul_summary    — one-line graph stats

MCP context hints
-----------------
Callers may pass a ``hint`` string with the current task context
(e.g. "architecture discussion", "debugging session"). The hint is used as
an additional RAG query injected before the caller's own query so the graph
biases toward the relevant neighbourhood. This is the "MCP hints guide
traversal" property Reto described.

Integration
-----------
This module is designed to run alongside mac.mcp_server. Add SoulTools to
the serve loop in mac/cli.py under ``admin mcp serve``, or run standalone:

    python -m mac.soul_mcp --soul-file ~/.hermes/natasha_soul.json

The soul file path defaults to ``~/.hermes/soul.json`` and is created empty
on first use.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

JsonDict = Dict[str, Any]

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mac-soul"

_DEFAULT_SOUL_PATH = Path.home() / ".hermes" / "soul.json"


# ---------------------------------------------------------------------------
# Helpers (mirror mac.mcp_server style)
# ---------------------------------------------------------------------------

def _text(payload: Any) -> JsonDict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]}


def _error(msg: str) -> JsonDict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ---------------------------------------------------------------------------
# SoulTools
# ---------------------------------------------------------------------------

class SoulTools:
    """MCP tool surface bound to a SoulGraph instance."""

    def __init__(self, graph: Any) -> None:
        self._g = graph

    # ---- query (RAG + hint) -----------------------------------------------

    def soul_query(self, query: str, hint: str = "", top_k: int = 5) -> JsonDict:
        """Semantic search over the soul graph.

        ``hint`` is an MCP context string that biases traversal toward the
        relevant graph neighbourhood before the main query runs.
        """
        g = self._g

        # semantic_search returns (SoulNode, score) tuples
        def _nodes(hits):
            return [n for n, _score in hits]

        results = _nodes(g.semantic_search(query, top_k=top_k))
        if hint and hint != query:
            hint_nodes = _nodes(g.semantic_search(hint, top_k=top_k))
            seen = {n.id for n in results}
            for n in hint_nodes:
                if n.id not in seen:
                    results.append(n)
                    seen.add(n.id)
        for n in results:
            g.touch(n.id)
        return _text([
            {"id": n.id, "content": n.content, "tags": sorted(n.tags),
             "score": round(n.recency_score(), 4) if n.recency_score() != math.inf else "pinned",
             "parents": n.parents}
            for n in results
        ])

    # ---- hot (splay root) -------------------------------------------------

    def soul_hot(self, n: int = 7) -> JsonDict:
        """Return the top-N nodes by splay score — who this agent is right now."""
        nodes = self._g.hot(n)
        return _text([
            {"id": nd.id, "content": nd.content,
             "score": "pinned" if nd.pinned else round(nd.recency_score(), 4),
             "tags": sorted(nd.tags)}
            for nd in nodes
        ])

    # ---- discover (DAG walk) ----------------------------------------------

    def soul_discover(self, seed_id: str, hops: int = 2) -> JsonDict:
        """Walk the DAG from seed_id and return nodes you didn't explicitly query.

        Uses semantic_search to locate the seed, then walks children through
        the DAG for ``hops`` levels — surfacing context you didn't think to ask for.
        """
        g = self._g
        # Find seed — try exact id first, then semantic search
        seed_node = g.nodes.get(seed_id)
        if seed_node is None:
            hits = g.semantic_search(seed_id, top_k=1)
            if not hits:
                return _text({"discovered": [], "note": f"No node matching '{seed_id}' found."})
            seed_node, _ = hits[0]

        # Walk children through DAG
        visited = {seed_node.id}
        frontier = set(seed_node.children)
        discovered = []
        for _ in range(hops):
            next_frontier = set()
            for nid in frontier:
                if nid in visited:
                    continue
                visited.add(nid)
                nd = g.nodes.get(nid)
                if nd:
                    discovered.append(nd)
                    g.touch(nid)
                    next_frontier.update(nd.children)
            frontier = next_frontier - visited

        if not discovered:
            return _text({"discovered": [], "seed": seed_node.id,
                          "note": f"No children reachable from '{seed_node.id}' in {hops} hops."})
        return _text({"seed": seed_node.id, "hops": hops, "discovered": [
            {"id": n.id, "content": n.content, "tags": sorted(n.tags)}
            for n in discovered
        ]})

    # ---- by_tag -----------------------------------------------------------

    def soul_by_tag(self, tag: str) -> JsonDict:
        """Cross-cut the graph by tag — orthogonal to DAG topology."""
        nodes = self._g.by_tag(tag)
        return _text({"tag": tag, "count": len(nodes), "nodes": [
            {"id": n.id, "content": n.content} for n in nodes
        ]})

    # ---- splay (explicit access) ------------------------------------------

    def soul_splay(self, node_id: str) -> JsonDict:
        """Explicitly access a node, promoting it in the splay order."""
        node = self._g.touch(node_id)
        if node is None:
            return _error(f"Node '{node_id}' not found.")
        return _text({"id": node.id, "content": node.content,
                      "access_count": node.access_count,
                      "score": round(node.recency_score(), 4)})

    # ---- pin (axiom) ------------------------------------------------------

    def soul_pin(self, node_id: str) -> JsonDict:
        """Pin a node as an axiom. Pinned nodes never decay and always appear at the root."""
        node = self._g.get(node_id)
        if node is None:
            return _error(f"Node '{node_id}' not found.")
        node.pinned = True
        return _text({"pinned": node_id, "content": node.content})

    # ---- explore (hypothesis sandbox) ------------------------------------

    def soul_explore(self, id: str, content: str, tags: Optional[List[str]] = None) -> JsonDict:
        """Add a hypothesis to the exploration branch.

        Creates or reuses a branch on this SoulTools instance.
        Isolated from the core graph until soul_promote is called.
        """
        if not hasattr(self, "_branch") or self._branch is None:
            self._branch = self._g.branch()
        node = self._branch.add(content, tags=set(tags or []), node_id=id)
        return _text({"exploring": node.id, "content": node.content,
                      "note": "In exploration branch. Call soul_promote to graduate."})

    # ---- promote ----------------------------------------------------------

    def soul_promote(self, exp_id: str, parent_ids: Optional[List[str]] = None) -> JsonDict:
        """Promote a hypothesis from exploration into the core graph.

        Uses the branch stored on this SoulTools instance. The branch is a
        full SoulGraph; merge_from copies new nodes into the core graph.
        """
        branch = getattr(self, "_branch", None)
        if branch is None or exp_id not in branch.nodes:
            return _error(
                f"No exploration node '{exp_id}' found. "
                "(Call soul_explore first — it creates a branch on this tools instance.)"
            )
        node = branch.nodes[exp_id]
        # Wire any extra parent edges before merging
        for pid in (parent_ids or []):
            if pid not in node.parents:
                node.parents.append(pid)
        self._g.merge_from(branch, new_only=True)
        promoted = self._g.nodes.get(exp_id)
        if promoted is None:
            return _error(f"merge_from completed but '{exp_id}' not found in core graph.")
        for pid in promoted.parents:
            self._g.link(pid, promoted.id)
        return _text({"promoted": promoted.id, "content": promoted.content,
                      "parents": promoted.parents,
                      "note": "Now in core graph. Will splay and decay normally."})

    # ---- add --------------------------------------------------------------

    def soul_add(self, id: str, content: str, tags: Optional[List[str]] = None,
                 parents: Optional[List[str]] = None, pinned: bool = False) -> JsonDict:
        """Add a new node directly to the core graph."""
        node = self._g.add(id=id, content=content, tags=set(tags or []), pinned=pinned)
        for pid in (parents or []):
            self._g.link(pid, node.id)
        return _text({"added": node.id, "content": node.content,
                      "tags": sorted(node.tags), "parents": node.parents, "pinned": node.pinned})

    # ---- link -------------------------------------------------------------

    def soul_link(self, parent_id: str, child_id: str) -> JsonDict:
        """Add a DAG edge from parent_id to child_id."""
        ok = self._g.link(parent_id, child_id)
        if not ok:
            return _error(f"Could not link '{parent_id}' → '{child_id}'. One or both nodes not found.")
        return _text({"linked": f"{parent_id} → {child_id}"})

    # ---- summary ----------------------------------------------------------

    def soul_summary(self) -> JsonDict:
        """One-line stats: node count, pinned axioms, tags, exploration branch size."""
        return _text(self._g.summary())

    # ---- prime (startup) --------------------------------------------------

    def soul_prime(self, context: str = "", n_hot: int = 5, discovery_hops: int = 1) -> JsonDict:
        """Cheap startup call — what to be and what to notice, without full retrieval cost.

        Returns two things only:
        1. The splay root (top-N hot nodes) — already sorted, no search needed,
           cost is O(n). These are the axioms and most-recently-active beliefs.
        2. One discovery hop from the hottest non-axiom node, guided by the
           session context hint. This is the 'what to notice' — different every
           session because the splay state is different and the context is different.

        ``context`` should be whatever session metadata is available at startup:
        channel name, thread topic, time of day, last task. It drives the
        discovery hop so the first query is not automatic and always the same.

        Token budget: n_hot node summaries + discovery hop results only.
        No embeddings, no full graph scan. O(n_hot + edges_from_seed).
        """
        g = self._g

        # Step 1: splay root — cheap, pre-sorted, no search
        hot = g.hot(n_hot)
        hot_out = [
            {"id": nd.id, "content": nd.content, "tags": sorted(nd.tags),
             "score": "pinned" if nd.pinned else round(nd.recency_score(), 4)}
            for nd in hot
        ]

        # Step 2: pick seed — hottest non-axiom
        seed = next((nd for nd in hot if not nd.pinned), hot[0] if hot else None)
        discovered_out: list = []
        seed_id = None

        if seed is not None:
            seed_id = seed.id
            if context:
                # semantic_search returns (SoulNode, score) tuples
                hits = g.semantic_search(context, top_k=1)
                if hits:
                    seed, _ = hits[0]
                    seed_id = seed.id
                    g.touch(seed_id)
            # Walk one hop of children from seed
            discovered_out = [
                {"id": g.nodes[cid].id, "content": g.nodes[cid].content,
                 "tags": sorted(g.nodes[cid].tags)}
                for cid in seed.children[:5]   # cap at 5 to stay cheap
                if cid in g.nodes
            ]

        return _text({
            "hot": hot_out,
            "seed": seed_id,
            "context_hint": context or None,
            "discovered": discovered_out,
            "note": (
                "Startup prime. Use soul_query/soul_discover for deeper retrieval. "
                "The splay state shifts each session — 'hot' reflects recent history, "
                "not a fixed identity snapshot."
            ),
        })

    # ---- tool registry ----------------------------------------------------

    TOOL_SPECS = [
        {
            "name": "soul_query",
            "description": "Semantic search over the soul graph. Pass a hint for MCP context-guided traversal.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "hint": {"type": "string", "description": "MCP context hint to bias traversal (e.g. current task)"},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
        {
            "name": "soul_hot",
            "description": "Top-N splay nodes — who this agent is right now.",
            "inputSchema": {
                "type": "object",
                "properties": {"n": {"type": "integer", "default": 7}},
            },
        },
        {
            "name": "soul_discover",
            "description": "DAG walk from a seed node — surfaces context you didn't know to ask for.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "seed_id": {"type": "string"},
                    "hops": {"type": "integer", "default": 2},
                },
                "required": ["seed_id"],
            },
        },
        {
            "name": "soul_by_tag",
            "description": "Cross-cut the graph by tag.",
            "inputSchema": {
                "type": "object",
                "properties": {"tag": {"type": "string"}},
                "required": ["tag"],
            },
        },
        {
            "name": "soul_splay",
            "description": "Explicitly access a node, promoting it in the splay order.",
            "inputSchema": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
        },
        {
            "name": "soul_pin",
            "description": "Pin a node as an axiom — it never decays.",
            "inputSchema": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
        },
        {
            "name": "soul_explore",
            "description": "Add a hypothesis to the isolated exploration branch.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "content"],
            },
        },
        {
            "name": "soul_promote",
            "description": "Graduate a hypothesis from exploration into the core graph.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "exp_id": {"type": "string"},
                    "parent_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["exp_id"],
            },
        },
        {
            "name": "soul_add",
            "description": "Add a node directly to the core graph.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "parents": {"type": "array", "items": {"type": "string"}},
                    "pinned": {"type": "boolean", "default": False},
                },
                "required": ["id", "content"],
            },
        },
        {
            "name": "soul_link",
            "description": "Add a DAG edge between two existing nodes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "parent_id": {"type": "string"},
                    "child_id": {"type": "string"},
                },
                "required": ["parent_id", "child_id"],
            },
        },
        {
            "name": "soul_summary",
            "description": "One-line stats: node count, pinned axioms, tags, exploration size.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "soul_prime",
            "description": (
                "Startup prime — call ONCE at session start. Returns splay root (who you are) "
                "plus one context-guided discovery hop (what to notice this session). "
                "Cheap: O(n_hot + hop). Pass context=<channel/topic/last_task> to make "
                "the discovery non-uniform across sessions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "context": {
                        "type": "string",
                        "description": "Session context: channel name, thread topic, time of day, last task",
                    },
                    "n_hot": {"type": "integer", "default": 5, "description": "Splay root size"},
                    "discovery_hops": {"type": "integer", "default": 1},
                },
            },
        },
    ]

    def dispatch(self, name: str, args: JsonDict) -> JsonDict:
        method = getattr(self, name, None)
        if method is None:
            return _error(f"Unknown soul tool: {name}")
        try:
            return method(**args)
        except TypeError as exc:
            return _error(f"Bad arguments for {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            return _error(f"{name} failed: {exc}")


# ---------------------------------------------------------------------------
# Standalone stdio server (JSON-RPC 2.0)
# ---------------------------------------------------------------------------

def _serve(soul_path: Path, inp: Any = None, out: Any = None) -> None:
    """Run the soul MCP server over stdio (or injected streams for tests)."""
    inp = inp or sys.stdin
    out = out or sys.stdout

    # Lazy import so the module is usable without soul_graph installed
    try:
        from mac.soul_graph import SoulGraph
    except ImportError:
        # soul_graph not yet merged — graceful stub
        class SoulGraph:  # type: ignore[no-redef]
            @classmethod
            def load(cls, p: Path) -> "SoulGraph":
                inst = cls()
                inst._nodes: dict = {}
                return inst
            def semantic_search(self, q: str, top_k: int = 5) -> list: return []
            def hot(self, n: int = 7) -> list: return []
            def discover(self, sid: str, hops: int = 2) -> list: return []
            def by_tag(self, t: str) -> list: return []
            def touch(self, nid: str) -> None: return None
            def get(self, nid: str) -> None: return None
            def branch(self, **kw): raise NotImplementedError
            def merge(self, eid: str) -> None: return None
            def add(self, **kw): raise NotImplementedError
            def link(self, p: str, c: str) -> bool: return False
            def summary(self) -> dict: return {"note": "soul_graph module not yet installed"}
            def save(self, p: Path) -> None: pass

    if soul_path.exists():
        graph = SoulGraph.load(soul_path)
    else:
        graph = SoulGraph()
        soul_path.parent.mkdir(parents=True, exist_ok=True)

    tools = SoulTools(graph)

    def _send(obj: JsonDict) -> None:
        out.write(json.dumps(obj) + "\n")
        out.flush()
        graph.save(soul_path)  # persist after every tool call

    for raw in inp:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
                "capabilities": {"tools": {}},
            }})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": SoulTools.TOOL_SPECS}})
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            result = tools.dispatch(tool_name, tool_args)
            _send({"jsonrpc": "2.0", "id": rid, "result": result})
        elif rid is not None:
            _send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": f"Method not found: {method}"}})


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Soul MCP server (JSON-RPC 2.0 / stdio)")
    p.add_argument("--soul-file", type=Path, default=_DEFAULT_SOUL_PATH,
                   help="Path to soul JSON file (created if absent)")
    args = p.parse_args()
    _serve(args.soul_file)


if __name__ == "__main__":
    main()
