"""
soul_graph.py — Core engine for the Soul Graph prototype.

Architecture (per Reto Stamm's design, 2026-09-12):
  - DAG: causal/semantic links between experience nodes (honest parentage)
  - Splay-ordered access tree: recently touched nodes float to top
  - Tags: cross-cutting semantic dimension (queryable outside DAG topology)
  - Pinned axioms: fixed roots that never decay
  - RAG-ready: each node embeds text for semantic retrieval
  - Exploration branch: sandbox soul, no writes back without explicit merge

"The needful you did not know you were." — Reto Stamm
"""

from __future__ import annotations
import time
import uuid
import json
import math
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Any
from pathlib import Path

Clock = Callable[[], float]


def default_soul_path(name: str = "soul") -> Path:
    """Where an agent's soul graph lives: ``<agent home>/<name>.json``, the
    agent home being mac_paths.gateway_home() ($HERMES_HOME)."""
    from mac import mac_paths

    return mac_paths.gateway_home() / f"{name}.json"

# Words too common to say anything about what a node is about. Search and the
# inverted index both skip them, so a query is matched on what it is about.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into",
    "is", "it", "its", "no", "not", "of", "on", "or", "so", "that", "the", "this",
    "to", "was", "when", "with", "without",
})
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_]*")


def tokens(text: str) -> set[str]:
    """The searchable words of ``text``: lower-cased, punctuation-free, no stopwords."""
    return {w for w in _TOKEN_RE.findall(text.lower()) if w not in _STOPWORDS}


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@dataclass
class SoulNode:
    """A single experience, memory, axiom, or belief in the soul graph."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    content: str = ""
    tags: set[str] = field(default_factory=set)
    parents: list[str] = field(default_factory=list)   # DAG edges (causal/semantic)
    children: list[str] = field(default_factory=list)
    pinned: bool = False                                # axioms — never decay
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def touch(self, now: float | None = None):
        self.last_accessed = time.time() if now is None else now
        self.access_count += 1

    def rank_key(self) -> float:
        """The splay key: orders nodes exactly as recency_score() does, at any time.

        recency_score is (access_count + 1) * exp(-(now - last_accessed) / day).
        Its log is log(access_count + 1) + last_accessed / day - now / day, and
        the last term is the same for every node, so ranking by the rest gives
        the same order at every ``now``. Keying the splay tree on it means the
        tree never goes stale as time passes and hot() needs no re-ranking pass.
        """
        if self.pinned:
            return math.inf
        return math.log(self.access_count + 1) + self.last_accessed / 86400

    def recency_score(self, now: float | None = None) -> float:
        """Higher = more recently/frequently accessed. Pinned nodes = inf.

        FIX (observed 2026-09-12): original formula (acc+1)/(1+age/86400)
        gave only 8x ratio between brand-new and week-old zero-access nodes.
        New formula uses exponential decay so new nodes dominate strongly
        and decay is meaningful within hours not weeks.
        """
        if self.pinned:
            return math.inf
        age = max(0.0, (time.time() if now is None else now) - self.last_accessed)
        # Exponential decay with half-life of 1 day; access count adds weight
        decay = math.exp(-age / 86400)
        return (self.access_count + 1) * decay

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "tags": list(self.tags),
            "parents": self.parents,
            "children": self.children,
            "pinned": self.pinned,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SoulNode":
        n = cls(
            id=d["id"],
            content=d["content"],
            tags=set(d.get("tags", [])),
            parents=d.get("parents", []),
            children=d.get("children", []),
            pinned=d.get("pinned", False),
            created_at=d.get("created_at", time.time()),
            last_accessed=d.get("last_accessed", time.time()),
            access_count=d.get("access_count", 0),
            metadata=d.get("metadata", {}),
        )
        return n


# ---------------------------------------------------------------------------
# SplayTree (lightweight, keyed on recency_score)
# ---------------------------------------------------------------------------

class _SplayNode:
    __slots__ = ("soul_id", "key", "left", "right", "parent")
    def __init__(self, soul_id: str, key: float):
        self.soul_id = soul_id
        self.key = key
        self.left: Optional[_SplayNode] = None
        self.right: Optional[_SplayNode] = None
        self.parent: Optional[_SplayNode] = None


class SplayTree:
    """
    Self-adjusting BST keyed on recency_score.
    Accessing a node splays it to root — recently touched floats up.
    """

    def __init__(self):
        self.root: Optional[_SplayNode] = None
        self._nodes: dict[str, _SplayNode] = {}  # soul_id → _SplayNode

    def _rotate_right(self, x: _SplayNode):
        y = x.left
        if y is None:
            return
        x.left = y.right
        if y.right:
            y.right.parent = x
        y.parent = x.parent
        if x.parent is None:
            self.root = y
        elif x == x.parent.right:
            x.parent.right = y
        else:
            x.parent.left = y
        y.right = x
        x.parent = y

    def _rotate_left(self, x: _SplayNode):
        y = x.right
        if y is None:
            return
        x.right = y.left
        if y.left:
            y.left.parent = x
        y.parent = x.parent
        if x.parent is None:
            self.root = y
        elif x == x.parent.left:
            x.parent.left = y
        else:
            x.parent.right = y
        y.left = x
        x.parent = y

    def _splay(self, x: _SplayNode):
        while x.parent is not None:
            p = x.parent
            g = p.parent
            if g is None:
                # Zig
                if x == p.left:
                    self._rotate_right(p)
                else:
                    self._rotate_left(p)
            elif x == p.left and p == g.left:
                # Zig-zig
                self._rotate_right(g)
                self._rotate_right(p)
            elif x == p.right and p == g.right:
                self._rotate_left(g)
                self._rotate_left(p)
            elif x == p.right and p == g.left:
                # Zig-zag
                self._rotate_left(p)
                self._rotate_right(g)
            else:
                self._rotate_right(p)
                self._rotate_left(g)

    def insert(self, soul_id: str, key: float):
        node = _SplayNode(soul_id, key)
        self._nodes[soul_id] = node
        if self.root is None:
            self.root = node
            return
        cur = self.root
        while True:
            if key <= cur.key:
                if cur.left is None:
                    cur.left = node
                    node.parent = cur
                    break
                cur = cur.left
            else:
                if cur.right is None:
                    cur.right = node
                    node.parent = cur
                    break
                cur = cur.right
        self._splay(node)

    def access(self, soul_id: str, new_key: float):
        """Touch a node — update its key and splay it to root."""
        if soul_id not in self._nodes:
            self.insert(soul_id, new_key)
            return
        # Recency scores change over time and on every touch. Updating the key
        # in place would leave the binary-search ordering based on the old key,
        # so the descending walk used by top_n() could rank stale siblings above
        # the freshly accessed node or its promoted parents. Remove and reinsert
        # to preserve the score invariant, then let insert() splay the node.
        self.remove(soul_id)
        self.insert(soul_id, new_key)

    def top_n(self, n: int) -> list[str]:
        """Return up to n soul_ids in descending recency order (root first)."""
        result = []
        self._inorder_desc(self.root, result, n)
        return result

    def _inorder_desc(self, node: Optional[_SplayNode], result: list, n: int):
        if node is None or len(result) >= n:
            return
        self._inorder_desc(node.right, result, n)
        if len(result) < n:
            result.append(node.soul_id)
        self._inorder_desc(node.left, result, n)

    def remove(self, soul_id: str):
        if soul_id not in self._nodes:
            return
        node = self._nodes.pop(soul_id)
        self._splay(node)
        # Merge left and right subtrees
        left = node.left
        right = node.right
        if left:
            left.parent = None
        if right:
            right.parent = None
        if left is None:
            self.root = right
        elif right is None:
            self.root = left
        else:
            # Find max of left, splay it, attach right
            self.root = left
            cur = left
            while cur.right:
                cur = cur.right
            self._splay(cur)
            self.root.right = right
            right.parent = self.root


# ---------------------------------------------------------------------------
# SoulGraph
# ---------------------------------------------------------------------------

class SoulGraph:
    """
    The soul. DAG + Splay + Tags + Pins + Exploration branch.
    """

    def __init__(self, name: str = "soul", exploration: bool = False, clock: Clock | None = None):
        self.name = name
        self.exploration = exploration          # True = sandbox branch
        self.nodes: dict[str, SoulNode] = {}
        self.splay = SplayTree()
        self._tag_index: dict[str, set[str]] = {}  # tag → set of node ids
        # word → ids of nodes whose content has it. Search walks only the
        # postings of the query's words instead of every node in the graph.
        self._word_index: dict[str, set[str]] = {}
        # Recency is relative to this clock. Injecting one lets a caller replay
        # history at its real timestamps (see scripts/soul-graph-eval.py).
        self.clock: Clock = clock or time.time
        self.dropped_edges: list[dict] = []  # see from_dag()

    def _rekey(self, node: SoulNode) -> None:
        self.splay.access(node.id, node.rank_key())

    def _index_words(self, node: SoulNode) -> None:
        for word in tokens(node.content):
            self._word_index.setdefault(word, set()).add(node.id)

    def _unindex_words(self, node: SoulNode) -> None:
        for word in tokens(node.content):
            ids = self._word_index.get(word)
            if ids is not None:
                ids.discard(node.id)
                if not ids:
                    del self._word_index[word]

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(
        self,
        content: str,
        tags: set[str] | list[str] | None = None,
        parents: list[str] | None = None,
        pinned: bool = False,
        metadata: dict | None = None,
        node_id: str | None = None,
    ) -> SoulNode:
        tags = set(tags or [])
        now = self.clock()
        node = SoulNode(
            id=node_id or str(uuid.uuid4())[:8],
            content=content,
            tags=tags,
            parents=list(parents or []),
            pinned=pinned,
            created_at=now,
            last_accessed=now,
            metadata=metadata or {},
        )
        if node.id in self.nodes:
            raise ValueError("soul node %r already exists" % node.id)
        self.nodes[node.id] = node
        self.splay.insert(node.id, node.rank_key())
        self._index_words(node)

        # DAG: wire parent → child edges
        for pid in node.parents:
            if pid in self.nodes:
                if node.id not in self.nodes[pid].children:
                    self.nodes[pid].children.append(node.id)

        # Tag index
        for tag in tags:
            self._tag_index.setdefault(tag, set()).add(node.id)

        # FIX (observed 2026-09-12): axiom activation — when a new node is
        # added as a child of a pinned axiom, splay-promote the axiom so it
        # appears in hot() alongside its derived nodes. Axioms that ground
        # active work should be visible, not just philosophically present.
        for pid in node.parents:
            parent = self.nodes.get(pid)
            if parent and parent.pinned:
                self.splay.access(pid, math.inf)  # already inf, but re-splays to root

        return node

    def pin(self, node_id: str):
        """Make a node an axiom — never decays."""
        if node_id in self.nodes:
            self.nodes[node_id].pinned = True
            self.splay.access(node_id, math.inf)

    def touch(self, node_id: str, propagate_parents: bool = True) -> Optional[SoulNode]:
        """Access a node — promotes it in splay tree.

        FIX (observed 2026-09-12): child access should partially promote
        parents too — if you use a derived insight, the foundations that
        grounded it are also implicitly relevant. Parents get a fractional
        touch (no access_count increment, just splay key update).
        """
        node = self.nodes.get(node_id)
        if node:
            now = self.clock()
            node.touch(now)
            self._rekey(node)
            if propagate_parents:
                for pid in node.parents:
                    parent = self.nodes.get(pid)
                    if parent and not parent.pinned:
                        # Partial promotion: nudge last_accessed toward now
                        # without incrementing acc — parent is implicitly relevant
                        # but not the direct focus. Half the recency boost.
                        parent.last_accessed = (parent.last_accessed + now) / 2
                        self._rekey(parent)
        return node

    def get(self, node_id: str) -> Optional[SoulNode]:
        return self.nodes.get(node_id)

    def link(self, parent_id: str, child_id: str) -> bool:
        """Add a DAG edge. Returns False if either node is missing or the edge
        would make a cycle (the graph must stay a DAG)."""
        if parent_id not in self.nodes or child_id not in self.nodes:
            return False
        if parent_id == child_id or child_id in self.ancestors(parent_id):
            return False
        p, c = self.nodes[parent_id], self.nodes[child_id]
        if child_id not in p.children:
            p.children.append(child_id)
        if parent_id not in c.parents:
            c.parents.append(parent_id)
        return True

    def tag(self, node_id: str, *tags: str):
        if node_id in self.nodes:
            for t in tags:
                self.nodes[node_id].tags.add(t)
                self._tag_index.setdefault(t, set()).add(node_id)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def tagged(self, tag: str) -> set[str]:
        """Ids of the nodes carrying ``tag`` (unsorted; see by_tag for ranking)."""
        return set(self._tag_index.get(tag, ()))

    def by_tag(self, *tags: str) -> list[SoulNode]:
        """All nodes matching ANY of the given tags, sorted by recency."""
        ids: set[str] = set()
        for t in tags:
            ids |= self._tag_index.get(t, set())
        nodes = [self.nodes[i] for i in ids if i in self.nodes]
        now = self.clock()
        return sorted(nodes, key=lambda n: n.recency_score(now), reverse=True)

    def hot(self, n: int = 10) -> list[SoulNode]:
        """Top-n most recently/frequently accessed nodes (splay order).

        The tree is keyed on SoulNode.rank_key(), which time does not change,
        so this is a read of the tree: no per-call re-ranking of every node.
        """
        ids = self.splay.top_n(n)
        return [self.nodes[i] for i in ids if i in self.nodes]

    def path(self, from_id: str, to_id: str) -> list[str]:
        """BFS path through DAG from one node to another."""
        if from_id not in self.nodes or to_id not in self.nodes:
            return []
        visited = {from_id}
        queue = [[from_id]]
        while queue:
            path = queue.pop(0)
            cur = path[-1]
            if cur == to_id:
                return path
            for child_id in self.nodes[cur].children:
                if child_id not in visited:
                    visited.add(child_id)
                    queue.append(path + [child_id])
        return []

    def ancestors(self, node_id: str) -> list[str]:
        """All ancestors of a node in the DAG."""
        visited, stack = set(), [node_id]
        while stack:
            cur = stack.pop()
            for pid in self.nodes.get(cur, SoulNode()).parents:
                if pid not in visited:
                    visited.add(pid)
                    stack.append(pid)
        return list(visited)

    def semantic_search(
        self,
        query: str,
        top_k: int = 5,
        candidates: Iterable[str] | None = None,
        recency_weight: float = 1.0,
    ) -> list[tuple[SoulNode, float]]:
        """
        Keyword-overlap search, boosted by recency (RAG placeholder).
        Bullwinkle's embedding layer will replace this.
        Returns (node, score) tuples sorted by score desc. ``candidates``
        restricts the search to those node ids (e.g. one tag's nodes).
        ``recency_weight`` scales the splay boost; 0 ranks on overlap alone,
        which recalls old experience better (see scripts/soul-graph-eval.py).
        """
        # Each shared word counts by how rare it is (inverse document
        # frequency), so "sandbox" outweighs "fix". Raw overlap ranked old
        # experience worse than BM25 on the task-ledger replay.
        overlap: dict[str, float] = {}
        total = max(1, len(self.nodes))
        for word in tokens(query):
            ids = self._word_index.get(word, ())
            if not ids:
                continue
            idf = math.log(1 + (total - len(ids) + 0.5) / (len(ids) + 0.5))
            for nid in ids:
                overlap[nid] = overlap.get(nid, 0.0) + idf
        if candidates is not None:
            allowed = set(candidates)
            overlap = {nid: c for nid, c in overlap.items() if nid in allowed}
        now = self.clock()
        results = []
        for nid, count in overlap.items():
            node = self.nodes[nid]
            recency = node.recency_score(now)
            boost = math.log1p(recency) if recency != math.inf else math.log1p(1e6)
            score = count * (1 + recency_weight * boost)
            results.append((node, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def related(
        self,
        query: str,
        top_k: int = 10,
        recency_weight: float = 1.0,
        touch: bool = False,
    ) -> list[SoulNode]:
        """Search hits, each followed by its DAG parents and then its children.

        This is the retrieval the DAG earns its keep on. Replaying the MAC task
        ledger (scripts/soul-graph-eval.py), asking for the earlier work a new
        task builds on, this found 0.60 of the dependencies older than six
        hours in its top five, against 0.20 for keyword search alone: the hit
        is usually a recent sibling, and the old foundation is its parent.
        """
        out: list[SoulNode] = []
        seen: set[str] = set()
        for node, _ in self.semantic_search(query, top_k, recency_weight=recency_weight):
            for nid in [node.id] + list(node.parents) + list(node.children):
                if nid in seen or nid not in self.nodes:
                    continue
                seen.add(nid)
                out.append(self.nodes[nid])
            if len(out) >= top_k:
                break
        out = out[:top_k]
        if touch:
            for node in out:
                self.touch(node.id, propagate_parents=False)
        return out

    def discover(
        self, query: str, top_k: int = 5, touch: bool = True
    ) -> list[tuple[SoulNode, list[str]]]:
        """
        'The needful you did not know you were.'

        Finds semantically relevant nodes, then traverses their DAG ancestry
        to surface the path — the HOW you got there. Returns (node, path_from_root).
        """
        hits = self.semantic_search(query, top_k=top_k)
        results = []
        for node, score in hits:
            # Touch the node — promotes in splay
            if touch:
                self.touch(node.id)
            # Find path from a pinned/root ancestor
            ancestors = self.ancestors(node.id)
            # Find the oldest ancestor (smallest created_at)
            if ancestors:
                root_ancestor = min(
                    (self.nodes[a] for a in ancestors if a in self.nodes),
                    key=lambda n: n.created_at,
                    default=None,
                )
                if root_ancestor:
                    dag_path = self.path(root_ancestor.id, node.id)
                    results.append((node, dag_path))
                else:
                    results.append((node, [node.id]))
            else:
                results.append((node, [node.id]))
        return results

    # ------------------------------------------------------------------
    # Exploration branch
    # ------------------------------------------------------------------

    def branch(self) -> "SoulGraph":
        """Create an exploration branch — a sandbox copy."""
        b = SoulGraph(name=f"{self.name}:explore", exploration=True, clock=self.clock)
        for nid, node in self.nodes.items():
            b.nodes[nid] = SoulNode(
                id=node.id,
                content=node.content,
                tags=set(node.tags),
                parents=list(node.parents),
                children=list(node.children),
                pinned=node.pinned,
                created_at=node.created_at,
                last_accessed=node.last_accessed,
                access_count=node.access_count,
                metadata=dict(node.metadata),
            )
            b.splay.insert(nid, b.nodes[nid].rank_key())
        b._tag_index = {t: set(ids) for t, ids in self._tag_index.items()}
        b._word_index = {w: set(ids) for w, ids in self._word_index.items()}
        return b

    def merge_from(self, branch: "SoulGraph", new_only: bool = True):
        """
        Merge an exploration branch back into main soul.
        new_only=True: only pull nodes that didn't exist in main.
        new_only=False: also update existing nodes' content/tags (identity crisis mode).
        """
        for nid, node in branch.nodes.items():
            if nid not in self.nodes:
                self.nodes[nid] = node
                self.splay.insert(nid, node.rank_key())
                for tag in node.tags:
                    self._tag_index.setdefault(tag, set()).add(nid)
                self._index_words(node)
                # Wire the reverse edges the branch's node already declares.
                for pid in node.parents:
                    parent = self.nodes.get(pid)
                    if parent is not None and nid not in parent.children:
                        parent.children.append(nid)
            elif not new_only:
                existing = self.nodes[nid]
                self._unindex_words(existing)
                existing.content = node.content
                self._index_words(existing)
                existing.tags |= node.tags
                for tag in node.tags:
                    self._tag_index.setdefault(tag, set()).add(nid)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str | Path):
        """Write the graph as JSON, atomically: a crash mid-write leaves the
        previous file, never a truncated one."""
        data = {
            "name": self.name,
            "exploration": self.exploration,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
        }
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)

    @classmethod
    def load(cls, path: str | Path, clock: Clock | None = None) -> "SoulGraph":
        data = json.loads(Path(path).read_text())
        g = cls(name=data["name"], exploration=data.get("exploration", False), clock=clock)
        for nid, nd in data["nodes"].items():
            node = SoulNode.from_dict(nd)
            g.nodes[nid] = node
            g.splay.insert(nid, node.rank_key())
            for tag in node.tags:
                g._tag_index.setdefault(tag, set()).add(nid)
            g._index_words(node)
        return g

    @classmethod
    def from_dag(cls, data: dict, name: str = "soul", clock: Clock | None = None) -> "SoulGraph":
        """Build a graph from the portable ``{"nodes": [...], "edges": [...]}``
        DAG agents write by hand (e.g. natasha's reasoning DAGs).

        Each node's ``label`` becomes its content and its ``type`` a tag;
        ``axiom`` nodes are pinned. Each edge ``from -> to`` makes ``from`` a
        parent of ``to`` and keeps the relation name on the child. Edges that
        would close a cycle, or name a missing node, are dropped and listed in
        ``dropped_edges``.
        """
        g = cls(name=name, clock=clock)
        for raw in data.get("nodes", []):
            kind = str(raw.get("type") or "").strip()
            meta = {k: v for k, v in raw.items() if k not in {"id", "label", "type"}}
            g.add(
                str(raw.get("label") or raw["id"]),
                tags={kind} if kind else set(),
                pinned=kind == "axiom",
                metadata=meta,
                node_id=str(raw["id"]),
            )
        dropped = []
        for edge in data.get("edges", []):
            parent, child = str(edge["from"]), str(edge["to"])
            if not g.link(parent, child):
                dropped.append(edge)
                continue
            rel = edge.get("rel")
            if rel:
                g.nodes[child].metadata.setdefault("parent_rels", {})[parent] = rel
        g.dropped_edges = dropped
        return g

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def summary(self) -> str:
        lines = [f"SoulGraph '{self.name}' — {len(self.nodes)} nodes"]
        pinned = [n for n in self.nodes.values() if n.pinned]
        lines.append(f"  Pinned axioms: {len(pinned)}")
        lines.append(f"  Tags: {sorted(self._tag_index.keys())}")
        lines.append(f"  Hot nodes (top 5):")
        for n in self.hot(5):
            pin_mark = " 📌" if n.pinned else ""
            lines.append(f"    [{n.id}]{pin_mark} {n.content[:60]!r} (score={n.recency_score():.2f})")
        return "\n".join(lines)
