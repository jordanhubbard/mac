#!/usr/bin/env python3
"""soul_pg_migrate.py — Migrate agent soul graph into shared Postgres.

Reads soul.json (mac.soul_graph JSON format) and/or a legacy memory_graph
SQLite database, then upserts the nodes + edges into the shared Postgres
soul_nodes / soul_edges tables.

Usage
-----
  python scripts/soul_pg_migrate.py \\
      --agent natasha \\
      --soul-file ~/.hermes/soul.json \\
      --sqlite ~/AgentFS/natasha-memory-graph/memory_graph.db \\
      --pg-url "postgresql://natasha:natasha_local@127.0.0.1:5433/natasha"

For Rocky (from their host, pointing at their Postgres):
  python scripts/soul_pg_migrate.py \\
      --agent rocky \\
      --soul-file ~/.hermes/soul.json \\
      --pg-url "postgresql://rocky:rocky_local@127.0.0.1:5433/rocky"

Schema changes
--------------
The script adds an `agent` column to soul_nodes if it is absent (idempotent).
Existing rows without an agent value get the value supplied via --agent
(defaulting to the agent whose data is being loaded).

Merge semantics
---------------
- Node IDs are authoritative: if a node with the same `id` already exists,
  tags and pinned state are merged (union of tags, OR of pinned), content
  is kept as-is (don't overwrite a richer live node with a stale seed).
- Edges are upserted by (parent_id, child_id, relation); no duplicate edges.
- SQLite memory_graph nodes that share content with an existing soul.json node
  are matched by exact content to avoid duplicates; a new ID is minted only
  when neither ID nor content is present.
- The `agent` column value is set for all nodes written in this run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Postgres connection (psycopg v3, bundled in the mac venv)
# ---------------------------------------------------------------------------


def _pg(url: str):
    try:
        import psycopg  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        sys.exit("psycopg not found — run inside ~/.mac/venv: ~/.mac/venv/bin/python scripts/soul_pg_migrate.py ...")
    return psycopg.connect(url, autocommit=False)


# ---------------------------------------------------------------------------
# Schema migration (idempotent)
# ---------------------------------------------------------------------------

ADD_AGENT_COL = """
ALTER TABLE soul_nodes ADD COLUMN IF NOT EXISTS agent TEXT;
"""

BACKFILL_AGENT = """
UPDATE soul_nodes SET agent = %(agent)s WHERE agent IS NULL;
"""


def ensure_schema(cur, agent: str) -> None:
    """Add `agent` column if missing; backfill existing rows."""
    cur.execute(ADD_AGENT_COL)
    cur.execute(BACKFILL_AGENT, {"agent": agent})


# ---------------------------------------------------------------------------
# Load soul.json
# ---------------------------------------------------------------------------


def load_soul_json(path: Path) -> dict[str, dict]:
    """Return {node_id: node_dict} from a soul.json file."""
    if not path.exists():
        print(f"  [soul.json] not found at {path}, skipping", file=sys.stderr)
        return {}
    data = json.loads(path.read_text())
    nodes = data.get("nodes", {})
    print(f"  [soul.json] loaded {len(nodes)} nodes from {path}")
    return nodes  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Load legacy SQLite memory_graph
# ---------------------------------------------------------------------------

_SQLITE_SCHEMA_QUERY = """
    SELECT name FROM sqlite_master
    WHERE type='table' AND name IN ('nodes','edges','node_tags')
    ORDER BY name
"""

_SQLITE_NODES_QUERY = "SELECT id, content, kind, tags, pinned, score, access_count, last_accessed, created_at FROM nodes"
_SQLITE_EDGES_QUERY = "SELECT from_id, to_id, relation_type FROM edges"


def load_sqlite(path: Path) -> tuple[list[dict], list[dict]]:
    """Return (nodes, edges) from a legacy memory_graph SQLite file."""
    if not path.exists():
        print(f"  [sqlite] not found at {path}, skipping", file=sys.stderr)
        return [], []

    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    tables = {r[0] for r in con.execute(_SQLITE_SCHEMA_QUERY)}

    nodes: list[dict] = []

    if "nodes" in tables:
        for row in con.execute(_SQLITE_NODES_QUERY):
            nid = row["id"]
            raw_tags: list[str] = []
            try:
                raw_tags = json.loads(row["tags"] or "[]")
            except (ValueError, TypeError):
                pass
            kind = row["kind"] or ""
            if kind and kind not in raw_tags:
                raw_tags.append(kind)
            nodes.append({
                "id": nid,
                "content": row["content"],
                "tags": raw_tags,
                "pinned": bool(row["pinned"]),
                "created_at": row["created_at"] or time.time(),
                "last_accessed": row["last_accessed"] or time.time(),
                "access_count": row["access_count"] or 0,
                "parents": [],
                "children": [],
                "metadata": {},
            })

    edges: list[dict] = []
    if "edges" in tables:
        for row in con.execute(_SQLITE_EDGES_QUERY):
            edges.append({
                "parent_id": row["from_id"],
                "child_id": row["to_id"],
                "relation": row["relation_type"] or "child",
            })

    con.close()
    print(f"  [sqlite] loaded {len(nodes)} nodes, {len(edges)} edges from {path}")
    return nodes, edges


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

UPSERT_NODE = """
INSERT INTO soul_nodes (id, content, tags, parents, pinned, score, created_at, updated_at, agent)
VALUES (%(id)s, %(content)s, %(tags)s, %(parents)s, %(pinned)s, %(score)s,
        to_timestamp(%(created_at)s), now(), %(agent)s)
ON CONFLICT (id) DO UPDATE SET
    tags    = (
        SELECT array_agg(DISTINCT t) FROM unnest(soul_nodes.tags || EXCLUDED.tags) t
    ),
    pinned  = soul_nodes.pinned OR EXCLUDED.pinned,
    agent   = COALESCE(soul_nodes.agent, EXCLUDED.agent),
    updated_at = now()
"""

UPSERT_EDGE = """
INSERT INTO soul_edges (parent_id, child_id, relation)
VALUES (%(parent_id)s, %(child_id)s, %(relation)s)
ON CONFLICT (parent_id, child_id, relation) DO NOTHING
"""

CONTENT_ID_LOOKUP = "SELECT id FROM soul_nodes WHERE content = %(content)s LIMIT 1"
EXISTS_BY_ID = "SELECT 1 FROM soul_nodes WHERE id = %(id)s"


def _content_hash(content: str) -> str:
    return hashlib.sha1(content.encode()).hexdigest()[:8]


def upsert_nodes(cur, nodes: list[dict], agent: str, existing_ids: set[str], existing_content: dict[str, str]) -> dict[str, str]:
    """Upsert nodes. Returns {original_id: canonical_id} for ID remapping."""
    id_map: dict[str, str] = {}
    for node in nodes:
        orig_id: str = node["id"]
        content: str = node.get("content", "")

        # Dedup: if content already in PG under a different ID, use that ID
        if orig_id not in existing_ids and content in existing_content:
            canonical_id = existing_content[content]
            id_map[orig_id] = canonical_id
            # still merge tags/pinned onto the existing node
            cur.execute("""
                UPDATE soul_nodes SET
                    tags   = (SELECT array_agg(DISTINCT t) FROM unnest(soul_nodes.tags || %(tags)s::text[]) t),
                    pinned = soul_nodes.pinned OR %(pinned)s,
                    agent  = COALESCE(soul_nodes.agent, %(agent)s)
                WHERE id = %(id)s
            """, {
                "id": canonical_id,
                "tags": node.get("tags", []),
                "pinned": node.get("pinned", False),
                "agent": agent,
            })
            continue

        canonical_id = orig_id
        id_map[orig_id] = canonical_id

        created_at = node.get("created_at")
        if isinstance(created_at, str):
            # ISO string — convert to epoch for to_timestamp()
            from datetime import datetime, timezone
            try:
                created_at = datetime.fromisoformat(created_at).timestamp()
            except ValueError:
                created_at = time.time()
        elif created_at is None:
            created_at = time.time()

        cur.execute(UPSERT_NODE, {
            "id": canonical_id,
            "content": content,
            "tags": node.get("tags", []),
            "parents": node.get("parents", []),
            "pinned": node.get("pinned", False),
            "score": node.get("recency_score", node.get("score", 1.0)) or 1.0,
            "created_at": float(created_at),
            "agent": agent,
        })
        existing_ids.add(canonical_id)
        existing_content[content] = canonical_id

    return id_map


def upsert_edges(cur, edges: list[dict], id_map: dict[str, str], valid_ids: set[str]) -> int:
    """Upsert edges. Remaps IDs, skips edges where either end is missing."""
    inserted = 0
    for edge in edges:
        parent = id_map.get(edge["parent_id"], edge["parent_id"])
        child = id_map.get(edge["child_id"], edge["child_id"])
        if parent not in valid_ids or child not in valid_ids:
            continue
        if parent == child:
            continue
        cur.execute(UPSERT_EDGE, {
            "parent_id": parent,
            "child_id": child,
            "relation": edge.get("relation", "child"),
        })
        inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--agent", required=True,
                        help="Agent name (e.g. natasha, rocky, bullwinkle)")
    parser.add_argument("--soul-file", default="~/.hermes/soul.json",
                        help="Path to soul.json (default: ~/.hermes/soul.json)")
    parser.add_argument("--sqlite", default="",
                        help="Path to legacy memory_graph SQLite (optional)")
    parser.add_argument("--pg-url", required=True,
                        help="Postgres DSN, e.g. postgresql://user:pass@host:port/db")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing")
    args = parser.parse_args(argv)

    agent = args.agent
    soul_path = Path(args.soul_file).expanduser()
    sqlite_path = Path(args.sqlite).expanduser() if args.sqlite else None

    print(f"\nMigrating soul graph → Postgres for agent '{agent}'")
    print(f"  soul.json : {soul_path}")
    print(f"  sqlite    : {sqlite_path or '(none)'}")
    print(f"  pg-url    : {args.pg_url}")
    print(f"  dry-run   : {args.dry_run}\n")

    # --- collect nodes ---
    all_nodes: list[dict] = []
    all_edges: list[dict] = []

    # soul.json (primary — higher priority, richer nodes)
    soul_nodes_dict = load_soul_json(soul_path)
    for nid, nd in soul_nodes_dict.items():
        nd = dict(nd)
        nd["id"] = nid
        # Normalise tags to list
        raw_tags = nd.get("tags", [])
        if isinstance(raw_tags, (set, list)):
            nd["tags"] = list(raw_tags)
        else:
            nd["tags"] = []
        all_nodes.append(nd)
        # DAG edges from parents list
        for parent_id in nd.get("parents", []):
            all_edges.append({"parent_id": parent_id, "child_id": nid, "relation": "child"})
        for child_id in nd.get("children", []):
            all_edges.append({"parent_id": nid, "child_id": child_id, "relation": "child"})

    # SQLite (legacy — lower priority, may duplicate)
    if sqlite_path:
        sq_nodes, sq_edges = load_sqlite(sqlite_path)
        all_nodes.extend(sq_nodes)
        all_edges.extend(sq_edges)

    # Deduplicate edges list (same triple can appear from both sources)
    seen_edges: set[tuple] = set()
    unique_edges: list[dict] = []
    for e in all_edges:
        key = (e["parent_id"], e["child_id"], e.get("relation", "child"))
        if key not in seen_edges:
            seen_edges.add(key)
            unique_edges.append({**e, "relation": e.get("relation", "child")})
    all_edges = unique_edges

    print(f"Total to migrate: {len(all_nodes)} nodes, {len(all_edges)} edges")

    if args.dry_run:
        print("\n[dry-run] No writes performed.")
        return 0

    # --- connect and migrate ---
    conn = _pg(args.pg_url)
    with conn:
        cur = conn.cursor()

        # 1. Schema: add agent column if needed
        ensure_schema(cur, agent)
        print(f"  Schema: agent column ensured, existing rows tagged as '{agent}'")

        # 2. Load existing IDs and content index from PG
        cur.execute("SELECT id, content FROM soul_nodes")
        existing_ids: set[str] = set()
        existing_content: dict[str, str] = {}  # content -> id
        for row in cur.fetchall():
            existing_ids.add(row[0])
            existing_content[row[1]] = row[0]
        print(f"  PG has {len(existing_ids)} existing nodes")

        # 3. Upsert nodes
        id_map = upsert_nodes(cur, all_nodes, agent, existing_ids, existing_content)
        all_valid_ids = set(id_map.values()) | existing_ids
        print(f"  Upserted {len(id_map)} nodes")

        # 4. Upsert edges
        n_edges = upsert_edges(cur, all_edges, id_map, all_valid_ids)
        print(f"  Upserted {n_edges} edges")

        conn.commit()

    # --- verify ---
    conn2 = _pg(args.pg_url)
    cur2 = conn2.cursor()
    cur2.execute("SELECT agent, count(*) FROM soul_nodes GROUP BY agent ORDER BY agent")
    rows = cur2.fetchall()
    cur2.execute("SELECT count(*) FROM soul_edges")
    edge_count = cur2.fetchone()[0]
    conn2.close()

    print("\nResult:")
    for row in rows:
        print(f"  agent={row[0] or '(null)'}: {row[1]} nodes")
    print(f"  total edges: {edge_count}")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
