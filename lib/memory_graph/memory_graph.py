"""
memory_graph.py — SQLite-backed graph memory for Rocky
Stdlib only: sqlite3, json, datetime, hashlib, re, math, os, pathlib
"""

import sqlite3
import json
import hashlib
import re
import math
import os
from datetime import datetime, timezone
from pathlib import Path

# ── Default DB path ────────────────────────────────────────────────────────────
_DEFAULT_DB = Path(__file__).parent / "memory_graph.db"

VALID_RELATIONS = {"related-to", "cites", "refines", "corroborates", "derived-from", "contradicts"}
VALID_KINDS = {"memory", "axiom", "skill-ref", "task-ref"}
VALID_VISIBILITY = {
    "private",
    "fleet",
    "personal",
}  # private=Rocky-only, fleet=shareable, personal=jkh-scoped
DECAY_FACTOR = 0.95  # multiplied per decay_all() call

# Stopwords excluded from auto-link token overlap
_STOPWORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "to",
    "of",
    "and",
    "or",
    "in",
    "on",
    "at",
    "for",
    "with",
    "by",
    "from",
    "use",
    "not",
    "do",
    "it",
    "this",
    "that",
    "as",
    "if",
    "so",
    "but",
    "all",
    "any",
    "no",
    "via",
    "per",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path=None) -> sqlite3.Connection:
    path = db_path or _DEFAULT_DB
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


# ── Schema bootstrap ───────────────────────────────────────────────────────────
def init_db(db_path=None):
    """Create tables if they don't exist."""
    con = _connect(db_path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            id            TEXT PRIMARY KEY,
            content       TEXT    NOT NULL,
            kind          TEXT    NOT NULL DEFAULT 'memory',
            tags          TEXT    NOT NULL DEFAULT '[]',
            score         REAL    NOT NULL DEFAULT 1.0,
            access_count  INTEGER NOT NULL DEFAULT 0,
            last_accessed TEXT,
            created_at    TEXT    NOT NULL,
            pinned        INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS edges (
            from_id       TEXT NOT NULL,
            to_id         TEXT NOT NULL,
            relation_type TEXT NOT NULL DEFAULT 'related-to',
            weight        REAL NOT NULL DEFAULT 1.0,
            created_at    TEXT NOT NULL,
            PRIMARY KEY (from_id, to_id, relation_type),
            FOREIGN KEY (from_id) REFERENCES nodes(id) ON DELETE CASCADE,
            FOREIGN KEY (to_id)   REFERENCES nodes(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_nodes_score  ON nodes(score DESC);
        CREATE INDEX IF NOT EXISTS idx_nodes_kind   ON nodes(kind);
        CREATE INDEX IF NOT EXISTS idx_edges_from   ON edges(from_id);
        CREATE INDEX IF NOT EXISTS idx_edges_to     ON edges(to_id);
    """)
    con.commit()
    con.close()


def _node_id(content: str) -> str:
    """Deterministic SHA-256 short ID from content."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ── Core API ───────────────────────────────────────────────────────────────────


def add_node(
    content: str,
    kind: str = "memory",
    tags: list = None,
    pinned: bool = False,
    visibility: str = "personal",
    auto_link: bool = True,
    db_path=None,
) -> str:
    """
    Insert or update a node. Returns node_id.
    Axiom kind is always pinned.
    visibility: 'private' (Rocky-only soul/secrets), 'fleet' (shareable), 'personal' (default)
    auto_link: if True, automatically add 'related-to' edges to nodes with token overlap >= 2
    """
    if tags is None:
        tags = []
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}")
    if visibility not in VALID_VISIBILITY:
        raise ValueError(f"visibility must be one of {VALID_VISIBILITY}")
    if kind == "axiom":
        pinned = True

    # Merge visibility into tags
    vis_tag = f"visibility:{visibility}"
    if vis_tag not in tags:
        tags = list(tags) + [vis_tag]

    node_id = _node_id(content)
    con = _connect(db_path)
    try:
        con.execute(
            """
            INSERT INTO nodes (id, content, kind, tags, score, access_count, created_at, pinned)
            VALUES (?, ?, ?, ?, 1.0, 0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind    = excluded.kind,
                tags    = excluded.tags,
                pinned  = excluded.pinned
        """,
            (node_id, content.strip(), kind, json.dumps(tags), _now_iso(), int(pinned)),
        )
        con.commit()

        # Auto-link: find nodes sharing >=2 significant tokens
        if auto_link:
            tokens = {
                t
                for t in re.findall(r"\w+", content.lower())
                if len(t) >= 4 and t not in _STOPWORDS
            }
            if tokens:
                cur = con.execute("SELECT id, content FROM nodes WHERE id != ?", (node_id,))
                for row in cur.fetchall():
                    other_tokens = {
                        t
                        for t in re.findall(r"\w+", row["content"].lower())
                        if len(t) >= 4 and t not in _STOPWORDS
                    }
                    overlap = tokens & other_tokens
                    if len(overlap) >= 2:
                        con.execute(
                            """
                            INSERT OR IGNORE INTO edges
                            (from_id, to_id, relation_type, weight, created_at)
                            VALUES (?, ?, 'related-to', 0.5, ?)
                        """,
                            (node_id, row["id"], _now_iso()),
                        )
                con.commit()
    finally:
        con.close()
    return node_id


def add_edge(
    from_id: str, to_id: str, relation: str = "related-to", weight: float = 1.0, db_path=None
):
    """
    Add a directed edge between two existing nodes.
    Silently skips if either node doesn't exist.
    """
    if relation not in VALID_RELATIONS:
        raise ValueError(f"relation must be one of {VALID_RELATIONS}")
    con = _connect(db_path)
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO edges (from_id, to_id, relation_type, weight, created_at)
            VALUES (?, ?, ?, ?, ?)
        """,
            (from_id, to_id, relation, weight, _now_iso()),
        )
        con.commit()
    finally:
        con.close()


def access(node_id: str, db_path=None) -> bool:
    """
    Record an access: bump access_count, update last_accessed,
    and reinforce score (logarithmic boost).
    Returns True if node was found, False otherwise.
    """
    con = _connect(db_path)
    try:
        cur = con.execute("SELECT score, access_count FROM nodes WHERE id=?", (node_id,))
        row = cur.fetchone()
        if row is None:
            return False
        new_count = row["access_count"] + 1
        # Logarithmic reinforcement: score += 0.1 * log(1 + access_count)
        boost = 0.1 * math.log1p(new_count)
        new_score = min(row["score"] + boost, 100.0)
        con.execute(
            """
            UPDATE nodes
            SET score=?, access_count=?, last_accessed=?
            WHERE id=?
        """,
            (new_score, new_count, _now_iso(), node_id),
        )
        con.commit()
        return True
    finally:
        con.close()


def fisheye(node_id: str, hops: int = 1, db_path=None) -> dict:
    """
    Fisheye retrieval centred on node_id.

    Returns:
        {
          "focal":   { full node dict },
          "hop1":    [ { id, summary (first 80 chars), kind, score, relation }, ... ],
          "hop2":    { relation_type: count, ... }   -- counts only
        }
    """
    con = _connect(db_path)
    try:
        # Focal node
        cur = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,))
        focal_row = cur.fetchone()
        if focal_row is None:
            return {"error": f"Node {node_id!r} not found"}

        focal = dict(focal_row)
        focal["tags"] = json.loads(focal["tags"])

        # 1-hop: direct neighbours (both directions)
        cur = con.execute(
            """
            SELECT n.id, n.content, n.kind, n.score,
                   e.relation_type, e.weight, 'outbound' AS direction
            FROM edges e JOIN nodes n ON n.id = e.to_id
            WHERE e.from_id = ?
            UNION ALL
            SELECT n.id, n.content, n.kind, n.score,
                   e.relation_type, e.weight, 'inbound' AS direction
            FROM edges e JOIN nodes n ON n.id = e.from_id
            WHERE e.to_id = ?
        """,
            (node_id, node_id),
        )
        hop1_rows = cur.fetchall()
        hop1_ids = set()
        hop1 = []
        for r in hop1_rows:
            hop1_ids.add(r["id"])
            hop1.append(
                {
                    "id": r["id"],
                    "summary": r["content"][:80] + ("…" if len(r["content"]) > 80 else ""),
                    "kind": r["kind"],
                    "score": round(r["score"], 4),
                    "relation": r["relation_type"],
                    "direction": r["direction"],
                }
            )

        # 2-hop: nodes reachable in 2 steps from hop1, excluding focal + hop1
        hop2_counts: dict[str, int] = {}
        if hop1_ids:
            seen = hop1_ids | {node_id}
            for h1_id in hop1_ids:
                cur2 = con.execute(
                    """
                    SELECT e.relation_type, COUNT(*) as cnt
                    FROM edges e
                    WHERE (e.from_id = ? AND e.to_id NOT IN ({excl}))
                       OR (e.to_id   = ? AND e.from_id NOT IN ({excl}))
                    GROUP BY e.relation_type
                """.format(excl=",".join("?" * len(seen))),
                    [h1_id] + list(seen) + [h1_id] + list(seen),
                )
                for r in cur2.fetchall():
                    hop2_counts[r["relation_type"]] = (
                        hop2_counts.get(r["relation_type"], 0) + r["cnt"]
                    )

        return {"focal": focal, "hop1": hop1, "hop2": hop2_counts}
    finally:
        con.close()


def search(query: str, limit: int = 10, db_path=None) -> list:
    """
    Case-insensitive keyword search over node content.
    Returns list of matching nodes sorted by score desc.
    """
    # Tokenise query into words, build LIKE clauses
    tokens = re.findall(r"\w+", query.lower())
    if not tokens:
        return []

    con = _connect(db_path)
    try:
        # Build WHERE with AND between tokens (all must appear)
        conditions = " AND ".join(["LOWER(content) LIKE ?" for _ in tokens])
        params = [f"%{t}%" for t in tokens] + [limit]
        cur = con.execute(
            f"SELECT * FROM nodes WHERE {conditions} ORDER BY score DESC LIMIT ?", params
        )
        rows = cur.fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"])
            results.append(d)
        return results
    finally:
        con.close()


def top(n: int = 10, db_path=None) -> list:
    """
    Return top-n nodes by score (preamble view).
    Each entry: { id, summary, kind, score, pinned, tags }
    """
    con = _connect(db_path)
    try:
        cur = con.execute(
            """
            SELECT id, content, kind, score, pinned, tags
            FROM nodes
            ORDER BY score DESC
            LIMIT ?
        """,
            (n,),
        )
        results = []
        for r in cur.fetchall():
            results.append(
                {
                    "id": r["id"],
                    "summary": r["content"][:100] + ("…" if len(r["content"]) > 100 else ""),
                    "kind": r["kind"],
                    "score": round(r["score"], 4),
                    "pinned": bool(r["pinned"]),
                    "tags": json.loads(r["tags"]),
                }
            )
        return results
    finally:
        con.close()


def decay_all(factor: float = DECAY_FACTOR, db_path=None) -> dict:
    """
    Apply exponential decay to all non-pinned nodes.
    score *= factor
    Pinned nodes and axiom kind are unaffected.
    Returns { 'decayed': N, 'skipped_pinned': M }
    """
    con = _connect(db_path)
    try:
        cur = con.execute(
            """
            UPDATE nodes
            SET score = MAX(0.001, score * ?)
            WHERE pinned = 0 AND kind != 'axiom'
        """,
            (factor,),
        )
        decayed = cur.rowcount
        cur2 = con.execute("SELECT COUNT(*) FROM nodes WHERE pinned=1 OR kind='axiom'")
        skipped = cur2.fetchone()[0]
        con.commit()
        return {"decayed": decayed, "skipped_pinned": skipped, "factor": factor}
    finally:
        con.close()


def node_count(db_path=None) -> int:
    con = _connect(db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    finally:
        con.close()


def pinned_count(db_path=None) -> int:
    con = _connect(db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM nodes WHERE pinned=1 OR kind='axiom'").fetchone()[
            0
        ]
    finally:
        con.close()


def axiom_count(db_path=None) -> int:
    con = _connect(db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM nodes WHERE kind='axiom'").fetchone()[0]
    finally:
        con.close()


# ── CLI convenience ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    db_path = Path(__file__).parent / "memory_graph.db"
    init_db(db_path)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "top"
    if cmd == "top":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        for node in top(n, db_path):
            print(
                f"[{node['score']:.2f}] ({node['kind']}) {'📌' if node['pinned'] else '  '} {node['summary'][:80]}"
            )
    elif cmd == "search":
        q = " ".join(sys.argv[2:])
        for node in search(q, db_path=db_path):
            print(f"[{node['score']:.2f}] {node['id']} — {node['content'][:100]}")
    elif cmd == "stats":
        print(
            f"Nodes: {node_count(db_path)}  Pinned: {pinned_count(db_path)}  Axioms: {axiom_count(db_path)}"
        )
