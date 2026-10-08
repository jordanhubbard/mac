"""MCP tool surface for the SoulGraph.

Exposes one agent's soul graph as MCP tools over stdio (JSON-RPC 2.0, the same
shape as mac.mcp_server, no extra dependencies):

    python -m mac.soul_mcp [--soul-file PATH]

The soul file defaults to ``soul.json`` in the agent's home
(``mac_paths.gateway_home()``, i.e. ``$HERMES_HOME``) and is created empty on
first use. Tools that change the graph save it atomically after each call;
read-only tools do not write.

Tools
-----
soul_prime      startup: the splay root plus one context-guided discovery hop
soul_related    search hits plus their DAG parents and children (best recall
                of older experience on the task-ledger replay, see
                scripts/soul-graph-eval.py)
soul_query      keyword search, optionally merged with a second ``hint`` query
soul_hot        top-N splay nodes: who this agent is right now
soul_discover   walk the DAG from a seed node
soul_by_tag     cross-cut the graph on a tag
soul_splay      explicitly access (promote) a node
soul_pin        pin a node as an axiom (never decays)
soul_explore    add a hypothesis to the exploration branch
soul_promote    graduate a hypothesis into the core graph
soul_add        add a node with optional DAG parents and tags
soul_link       add a DAG edge between two existing nodes
soul_summary    graph stats
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from mac.soul_graph import SoulGraph, SoulNode, default_soul_path

JsonDict = Dict[str, Any]

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mac-soul"

# Tools that only read the graph; everything else is saved after the call.
READ_ONLY_TOOLS = frozenset({"soul_hot", "soul_by_tag", "soul_summary"})


def _text(payload: Any) -> JsonDict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]}


def _error(msg: str) -> JsonDict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _score(node: SoulNode) -> Any:
    score = node.recency_score()
    return "pinned" if score == math.inf else round(score, 4)


def _brief(node: SoulNode) -> JsonDict:
    return {"id": node.id, "content": node.content, "tags": sorted(node.tags)}


class SoulTools:
    """MCP tool surface bound to one SoulGraph."""

    def __init__(self, graph: SoulGraph) -> None:
        self._g = graph
        self._branch: Optional[SoulGraph] = None

    def soul_query(self, query: str, hint: str = "", top_k: int = 5) -> JsonDict:
        """Keyword search. ``hint`` (the current task) adds a second search
        whose new hits are appended, biasing toward that neighbourhood."""
        g = self._g
        results = [n for n, _ in g.semantic_search(query, top_k=top_k)]
        if hint and hint != query:
            seen = {n.id for n in results}
            for n, _ in g.semantic_search(hint, top_k=top_k):
                if n.id not in seen:
                    results.append(n)
                    seen.add(n.id)
        for n in results:
            g.touch(n.id)
        return _text([dict(_brief(n), score=_score(n), parents=n.parents) for n in results])

    def soul_related(self, query: str, top_k: int = 10, recency_weight: float = 1.0) -> JsonDict:
        """Search hits, each followed by its DAG parents and children."""
        nodes = self._g.related(query, top_k=top_k, recency_weight=recency_weight, touch=True)
        return _text([dict(_brief(n), parents=n.parents) for n in nodes])

    def soul_hot(self, n: int = 7) -> JsonDict:
        return _text([dict(_brief(nd), score=_score(nd)) for nd in self._g.hot(n)])

    def soul_discover(self, seed_id: str, hops: int = 2) -> JsonDict:
        """Walk the DAG's children from ``seed_id`` (an id, or text to search
        for one) for ``hops`` levels, surfacing context nobody asked for."""
        g = self._g
        seed = g.get(seed_id)
        if seed is None:
            hits = g.semantic_search(seed_id, top_k=1)
            if not hits:
                return _text({"discovered": [], "note": "No node matching %r found." % seed_id})
            seed = hits[0][0]
        visited = {seed.id}
        frontier = list(seed.children)
        discovered: List[SoulNode] = []
        for _ in range(max(0, int(hops))):
            next_frontier: List[str] = []
            for nid in frontier:
                if nid in visited or nid not in g.nodes:
                    continue
                visited.add(nid)
                node = g.nodes[nid]
                discovered.append(node)
                g.touch(nid)
                next_frontier.extend(node.children)
            frontier = next_frontier
        return _text(
            {"seed": seed.id, "hops": hops, "discovered": [_brief(n) for n in discovered]}
        )

    def soul_by_tag(self, tag: str) -> JsonDict:
        nodes = self._g.by_tag(tag)
        return _text({"tag": tag, "count": len(nodes), "nodes": [_brief(n) for n in nodes]})

    def soul_splay(self, node_id: str) -> JsonDict:
        node = self._g.touch(node_id)
        if node is None:
            return _error("Node %r not found." % node_id)
        return _text({"id": node.id, "access_count": node.access_count, "score": _score(node)})

    def soul_pin(self, node_id: str) -> JsonDict:
        node = self._g.get(node_id)
        if node is None:
            return _error("Node %r not found." % node_id)
        self._g.pin(node_id)
        return _text({"pinned": node_id, "content": node.content})

    def soul_explore(self, id: str, content: str, tags: Optional[List[str]] = None) -> JsonDict:
        """Add a hypothesis to the exploration branch, isolated from the core
        graph until soul_promote. The branch lives as long as this server."""
        if self._branch is None:
            self._branch = self._g.branch()
        if id in self._branch.nodes:
            return _error("Exploration node %r already exists." % id)
        node = self._branch.add(content, tags=set(tags or []), node_id=id)
        return _text({"exploring": node.id, "content": node.content})

    def soul_promote(self, exp_id: str, parent_ids: Optional[List[str]] = None) -> JsonDict:
        """Move one hypothesis into the core graph. Only ``exp_id`` moves: the
        rest of the branch stays unpromoted (merge_from would copy it all)."""
        branch = self._branch
        if branch is None or exp_id not in branch.nodes or exp_id in self._g.nodes:
            return _error("No unpromoted exploration node %r (call soul_explore first)." % exp_id)
        node = branch.nodes[exp_id]
        promoted = self._g.add(
            node.content,
            tags=set(node.tags),
            parents=[p for p in node.parents if p in self._g.nodes],
            metadata=dict(node.metadata),
            node_id=exp_id,
        )
        refused = [pid for pid in (parent_ids or []) if not self._g.link(pid, exp_id)]
        return _text(
            {"promoted": exp_id, "parents": promoted.parents, "refused_parents": refused}
        )

    def soul_add(
        self,
        content: str,
        id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        parents: Optional[List[str]] = None,
        pinned: bool = False,
    ) -> JsonDict:
        if id is not None and id in self._g.nodes:
            return _error("Node %r already exists." % id)
        missing = [p for p in (parents or []) if p not in self._g.nodes]
        if missing:
            return _error("Unknown parent node(s): %s" % ", ".join(missing))
        node = self._g.add(
            content, tags=set(tags or []), parents=list(parents or []), pinned=pinned, node_id=id
        )
        return _text(dict(_brief(node), parents=node.parents, pinned=node.pinned))

    def soul_link(self, parent_id: str, child_id: str) -> JsonDict:
        if not self._g.link(parent_id, child_id):
            return _error(
                "Could not link %r -> %r: a node is missing or the edge would make a cycle."
                % (parent_id, child_id)
            )
        return _text({"linked": "%s -> %s" % (parent_id, child_id)})

    def soul_summary(self) -> JsonDict:
        return _text(self._g.summary())

    def soul_prime(self, context: str = "", n_hot: int = 5, discovery_hops: int = 1) -> JsonDict:
        """Startup: the splay root (who you are) plus a short discovery walk
        from a seed chosen by ``context`` (what to notice this session).

        The seed is the best search hit for ``context``, else the hottest
        non-axiom node. Each hop takes at most five children, so the cost is
        bounded by n_hot plus a few edges, not the size of the graph.
        """
        g = self._g
        hot = g.hot(n_hot)
        seed = next((nd for nd in hot if not nd.pinned), hot[0] if hot else None)
        if context:
            hits = g.semantic_search(context, top_k=1)
            if hits:
                seed = hits[0][0]
                g.touch(seed.id)
        discovered: List[SoulNode] = []
        if seed is not None:
            seen = {seed.id}
            frontier = [seed]
            for _ in range(max(0, int(discovery_hops))):
                nxt = []
                for node in frontier:
                    for cid in node.children[:5]:
                        if cid in seen or cid not in g.nodes:
                            continue
                        seen.add(cid)
                        nxt.append(g.nodes[cid])
                discovered.extend(nxt)
                frontier = nxt
        return _text(
            {
                "hot": [dict(_brief(nd), score=_score(nd)) for nd in hot],
                "seed": seed.id if seed else None,
                "context_hint": context or None,
                "discovered": [_brief(n) for n in discovered],
            }
        )

    TOOL_SPECS: List[JsonDict] = [
        {
            "name": "soul_prime",
            "description": (
                "Call once at session start: the splay root (who you are) plus a "
                "discovery walk from the node best matching `context` (channel, "
                "topic, last task)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "context": {"type": "string"},
                    "n_hot": {"type": "integer", "default": 5},
                    "discovery_hops": {"type": "integer", "default": 1},
                },
            },
        },
        {
            "name": "soul_related",
            "description": (
                "Find earlier experience related to `query`: search hits plus their "
                "DAG parents and children. Best for 'what is this built on?'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 10},
                    "recency_weight": {"type": "number", "default": 1.0},
                },
                "required": ["query"],
            },
        },
        {
            "name": "soul_query",
            "description": "Keyword search. Pass `hint` (the current task) to bias toward it.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "hint": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
        {
            "name": "soul_hot",
            "description": "Top-N splay nodes: who this agent is right now.",
            "inputSchema": {"type": "object", "properties": {"n": {"type": "integer", "default": 7}}},
        },
        {
            "name": "soul_discover",
            "description": "Walk the DAG's children from a seed node id (or text matching one).",
            "inputSchema": {
                "type": "object",
                "properties": {"seed_id": {"type": "string"}, "hops": {"type": "integer", "default": 2}},
                "required": ["seed_id"],
            },
        },
        {
            "name": "soul_by_tag",
            "description": "Every node with a tag, most recent first.",
            "inputSchema": {
                "type": "object",
                "properties": {"tag": {"type": "string"}},
                "required": ["tag"],
            },
        },
        {
            "name": "soul_splay",
            "description": "Access a node, promoting it in the splay order.",
            "inputSchema": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
        },
        {
            "name": "soul_pin",
            "description": "Pin a node as an axiom: it never decays.",
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
            "description": "Graduate an exploration hypothesis into the core graph.",
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
            "description": "Add a node to the core graph.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "id": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "parents": {"type": "array", "items": {"type": "string"}},
                    "pinned": {"type": "boolean", "default": False},
                },
                "required": ["content"],
            },
        },
        {
            "name": "soul_link",
            "description": "Add a DAG edge between two existing nodes.",
            "inputSchema": {
                "type": "object",
                "properties": {"parent_id": {"type": "string"}, "child_id": {"type": "string"}},
                "required": ["parent_id", "child_id"],
            },
        },
        {
            "name": "soul_summary",
            "description": "Node count, axioms, tags and the hottest nodes.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]

    def dispatch(self, name: str, args: JsonDict) -> JsonDict:
        if name not in {spec["name"] for spec in self.TOOL_SPECS}:
            return _error("Unknown soul tool: %s" % name)
        try:
            return getattr(self, name)(**args)
        except TypeError as exc:
            return _error("Bad arguments for %s: %s" % (name, exc))
        except Exception as exc:  # noqa: BLE001 - a tool failure is a tool result
            return _error("%s failed: %s" % (name, exc))


def serve(soul_path: Path, inp: Any = None, out: Any = None) -> None:
    """Run the soul MCP server over stdio (or the given streams)."""
    inp = inp or sys.stdin
    out = out or sys.stdout
    graph = SoulGraph.load(soul_path) if soul_path.exists() else SoulGraph(name=soul_path.stem)
    tools = SoulTools(graph)

    def send(obj: JsonDict) -> None:
        out.write(json.dumps(obj) + "\n")
        out.flush()

    for raw in inp:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": "0.2.0"},
                "capabilities": {"tools": {}},
            }})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": SoulTools.TOOL_SPECS}})
        elif method == "tools/call":
            name = params.get("name", "")
            result = tools.dispatch(name, params.get("arguments") or {})
            if name not in READ_ONLY_TOOLS and not result.get("isError"):
                soul_path.parent.mkdir(parents=True, exist_ok=True)
                graph.save(soul_path)
            send({"jsonrpc": "2.0", "id": rid, "result": result})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": "Method not found: %s" % method}})


def main(argv: Optional[List[str]] = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Soul graph MCP server (JSON-RPC 2.0 over stdio)")
    parser.add_argument(
        "--soul-file", type=Path, default=None,
        help="soul JSON file (default: soul.json in the agent home); created on first write",
    )
    args = parser.parse_args(argv)
    serve(args.soul_file or default_soul_path())


if __name__ == "__main__":
    main()
