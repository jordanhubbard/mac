"""soul_pg_sync.py — Bidirectional sync between soul.json and fleet_brain Postgres.

CLI:
    python -m mac.soul_pg_sync import --agent bullwinkle --soul ~/.hermes/soul.json
    python -m mac.soul_pg_sync export --agent bullwinkle --soul ~/.hermes/soul.json
    python -m mac.soul_pg_sync status --agent bullwinkle

Programmatic:
    from mac.soul_pg_sync import json_to_pg, pg_to_json, PgSoulBackend
    # PgSoulBackend wraps a live psycopg2 connection and provides
    # save(graph) / load(name, agent_id) in the same shape as SoulGraph.save/load.

Requirements: psycopg2-binary (already in mac venv).
Connections: uses DATABASE_URL env or --db-url arg (default: fleet_brain local socket).

Design:
- soul_nodes: (node_id, agent_id, content, tags, pinned, score, synced_at, created_at)
- soul_edges: derived from SoulNode.parents — (from_id, to_id, agent_id, relation_type, weight)
- soul.json encodes edges inside each node's `parents` list; we store them in soul_edges.
- import = json → PG (upsert); export = PG → json (overwrite local file).
- PgSoulBackend.save() / .load() are drop-in replacements for SoulGraph.save() / SoulGraph.load().
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mac.soul_graph import SoulGraph

_DEFAULT_DB_URL = os.environ.get("DATABASE_URL", "postgresql:///fleet_brain")


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def _connect(db_url: str | None = None):
    """Return a psycopg2 connection. Never use autocommit — callers commit."""
    import psycopg2  # noqa: PLC0415
    import psycopg2.extras  # noqa: PLC0415

    url = db_url or _DEFAULT_DB_URL
    conn = psycopg2.connect(url)
    conn.autocommit = False
    return conn


# ---------------------------------------------------------------------------
# Low-level upsert helpers
# ---------------------------------------------------------------------------


def _upsert_nodes(cur, agent_id: str, nodes: list[dict]) -> int:
    """Upsert a list of node dicts into soul_nodes. Returns row count."""
    sql = """
        INSERT INTO soul_nodes (node_id, agent_id, content, tags, pinned, score, synced_at, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
        ON CONFLICT (node_id, agent_id) DO UPDATE SET
            content   = EXCLUDED.content,
            tags      = EXCLUDED.tags,
            pinned    = EXCLUDED.pinned,
            score     = EXCLUDED.score,
            synced_at = now()
    """
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for n in nodes:
        created = n.get("created_at") or now
        cur.execute(
            sql,
            (
                n["id"],
                agent_id,
                n["content"],
                n.get("tags", []),
                bool(n.get("pinned", False)),
                float(n.get("key", n.get("score", 0.0))),
                created,
            ),
        )
        count += 1
    return count


def _upsert_edges(cur, agent_id: str, edges: list[dict]) -> int:
    """Upsert a list of edge dicts into soul_edges. Returns row count."""
    sql = """
        INSERT INTO soul_edges (from_id, to_id, agent_id, relation_type, weight)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (from_id, to_id, agent_id, relation_type) DO NOTHING
    """
    count = 0
    for e in edges:
        cur.execute(
            sql,
            (
                e["from_id"],
                e["to_id"],
                agent_id,
                e.get("relation_type", "parent"),
                float(e.get("weight", 1.0)),
            ),
        )
        count += 1
    return count


def _edges_from_nodes(node_dicts: list[dict]) -> list[dict]:
    """Extract parent→child edge rows from soul.json node list.

    soul.json encodes edges as node.parents = [parent_id, ...].
    We store: from=parent, to=child (parent points to child).
    """
    edges = []
    for n in node_dicts:
        child_id = n["id"]
        for parent_id in n.get("parents", []):
            edges.append(
                {
                    "from_id": parent_id,
                    "to_id": child_id,
                    "relation_type": "parent",
                    "weight": 1.0,
                }
            )
    return edges


# ---------------------------------------------------------------------------
# Public API: json_to_pg / pg_to_json
# ---------------------------------------------------------------------------


def json_to_pg(soul_path: str | Path, agent_id: str, db_url: str | None = None) -> dict:
    """Import soul.json → fleet_brain. Returns {nodes, edges} counts."""
    data = json.loads(Path(soul_path).read_text())
    raw_nodes = list(data.get("nodes", {}).values())

    # Normalise: soul_graph stores node dicts with 'id' at top level
    node_dicts = []
    for nid, nd in data.get("nodes", {}).items():
        d = dict(nd)
        d["id"] = nid
        node_dicts.append(d)

    edges = _edges_from_nodes(node_dicts)

    conn = _connect(db_url)
    try:
        with conn.cursor() as cur:
            n_count = _upsert_nodes(cur, agent_id, node_dicts)
            e_count = _upsert_edges(cur, agent_id, edges)
        conn.commit()
    finally:
        conn.close()

    return {"nodes": n_count, "edges": e_count}


def pg_to_json(soul_path: str | Path, agent_id: str, db_url: str | None = None) -> dict:
    """Export fleet_brain → soul.json. Overwrites the local file. Returns counts."""
    import psycopg2.extras  # noqa: PLC0415

    conn = _connect(db_url)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM soul_nodes WHERE agent_id = %s ORDER BY created_at",
                (agent_id,),
            )
            pg_nodes = cur.fetchall()

            cur.execute(
                "SELECT from_id, to_id, relation_type, weight FROM soul_edges WHERE agent_id = %s",
                (agent_id,),
            )
            pg_edges = cur.fetchall()
    finally:
        conn.close()

    # Build parent lists: child_id → [parent_id, ...]
    child_parents: dict[str, list[str]] = {}
    for e in pg_edges:
        child_parents.setdefault(e["to_id"], []).append(e["from_id"])

    nodes_dict = {}
    for n in pg_nodes:
        nid = n["node_id"]
        nodes_dict[nid] = {
            "id": nid,
            "content": n["content"],
            "tags": list(n["tags"] or []),
            "pinned": bool(n["pinned"]),
            "key": float(n["score"] or 0.0),
            "parents": child_parents.get(nid, []),
            "children": [],  # will be filled below
            "created_at": n["created_at"].isoformat() if n["created_at"] else None,
            "access_count": 0,
            "metadata": {},
        }

    # Fill children from parent lists
    for child_id, parents in child_parents.items():
        for parent_id in parents:
            if parent_id in nodes_dict:
                nodes_dict[parent_id].setdefault("children", [])
                if child_id not in nodes_dict[parent_id]["children"]:
                    nodes_dict[parent_id]["children"].append(child_id)

    out = {"name": agent_id, "exploration": False, "nodes": nodes_dict}
    path = Path(soul_path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=2, default=str))
    tmp.replace(path)

    return {"nodes": len(nodes_dict), "edges": len(pg_edges)}


# ---------------------------------------------------------------------------
# PgSoulBackend — drop-in save/load replacement for soul_mcp
# ---------------------------------------------------------------------------


class PgSoulBackend:
    """Wraps fleet_brain so soul_mcp can use Postgres as primary storage.

    Usage in soul_mcp::

        backend = PgSoulBackend(agent_id="bullwinkle", db_url="postgresql:///fleet_brain")
        graph = backend.load("soul")
        # ... operate on graph ...
        backend.save(graph)
    """

    def __init__(self, agent_id: str, db_url: str | None = None):
        self.agent_id = agent_id
        self.db_url = db_url or _DEFAULT_DB_URL

    def load(self, name: str = "soul") -> "SoulGraph":
        """Load from Postgres into a SoulGraph in memory."""
        import psycopg2.extras  # noqa: PLC0415
        from mac.soul_graph import SoulGraph, SoulNode  # noqa: PLC0415

        conn = _connect(self.db_url)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM soul_nodes WHERE agent_id = %s ORDER BY created_at",
                    (self.agent_id,),
                )
                pg_nodes = cur.fetchall()

                cur.execute(
                    "SELECT from_id, to_id FROM soul_edges WHERE agent_id = %s",
                    (self.agent_id,),
                )
                pg_edges = cur.fetchall()
        finally:
            conn.close()

        # Build parent map: child_id → [parent_id]
        child_parents: dict[str, list[str]] = {}
        for e in pg_edges:
            child_parents.setdefault(e["to_id"], []).append(e["from_id"])

        # Build a node dict list that SoulGraph.load can consume via json
        nodes_dict = {}
        for n in pg_nodes:
            nid = n["node_id"]
            nodes_dict[nid] = {
                "id": nid,
                "content": n["content"],
                "tags": list(n["tags"] or []),
                "pinned": bool(n["pinned"]),
                "key": float(n["score"] or 0.0),
                "parents": child_parents.get(nid, []),
                "children": [],
                "created_at": n["created_at"].isoformat() if n["created_at"] else None,
                "access_count": 0,
                "metadata": {},
            }

        # Fill children
        for child_id, parents in child_parents.items():
            for parent_id in parents:
                if parent_id in nodes_dict and child_id not in nodes_dict[parent_id]["children"]:
                    nodes_dict[parent_id]["children"].append(child_id)

        data = {"name": name, "exploration": False, "nodes": nodes_dict}
        import tempfile  # noqa: PLC0415

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f, default=str)
            tmppath = f.name
        try:
            g = SoulGraph.load(tmppath)
        finally:
            Path(tmppath).unlink(missing_ok=True)
        return g

    def save(self, graph: "SoulGraph") -> None:
        """Persist a SoulGraph back to Postgres (upsert, non-destructive)."""
        node_dicts = []
        edges = []
        for nid, node in graph.nodes.items():
            d = node.to_dict()
            d["id"] = nid
            node_dicts.append(d)
            for parent_id in node.parents:
                edges.append(
                    {
                        "from_id": parent_id,
                        "to_id": nid,
                        "relation_type": "parent",
                        "weight": 1.0,
                    }
                )

        conn = _connect(self.db_url)
        try:
            with conn.cursor() as cur:
                _upsert_nodes(cur, self.agent_id, node_dicts)
                _upsert_edges(cur, self.agent_id, edges)
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli():
    import argparse  # noqa: PLC0415

    from mac import mac_paths

    parser = argparse.ArgumentParser(
        description="soul_pg_sync: sync soul.json ↔ fleet_brain Postgres"
    )
    parser.add_argument(
        "command",
        choices=["import", "export", "status"],
        help="import=json→pg, export=pg→json, status=show counts",
    )
    parser.add_argument("--agent", required=True, help="agent_id (e.g. bullwinkle)")
    parser.add_argument(
        "--soul", type=Path, default=None, help="Path to soul.json (default: ~/.hermes/soul.json)"
    )
    parser.add_argument(
        "--db-url", default=None, help="Postgres URL (default: postgresql:///fleet_brain)"
    )
    args = parser.parse_args()

    soul_path = args.soul or (mac_paths.hermes_home() / "soul.json")
    db_url = args.db_url

    if args.command == "import":
        result = json_to_pg(soul_path, args.agent, db_url)
        print(f"import ok — {result['nodes']} nodes, {result['edges']} edges → Postgres")

    elif args.command == "export":
        result = pg_to_json(soul_path, args.agent, db_url)
        print(f"export ok — {result['nodes']} nodes, {result['edges']} edges → {soul_path}")

    elif args.command == "status":
        import psycopg2  # noqa: PLC0415

        conn = _connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM soul_nodes WHERE agent_id=%s", (args.agent,))
                n_row = cur.fetchone()
                n = n_row[0] if n_row else 0
                cur.execute("SELECT COUNT(*) FROM soul_edges WHERE agent_id=%s", (args.agent,))
                e_row = cur.fetchone()
                e = e_row[0] if e_row else 0
        finally:
            conn.close()
        print(f"fleet_brain  agent={args.agent}  nodes={n}  edges={e}")
        if soul_path.exists():
            data = json.loads(soul_path.read_text())
            local_n = len(data.get("nodes", {}))
            print(f"soul.json    path={soul_path}  nodes={local_n}")
        else:
            print(f"soul.json    path={soul_path}  NOT FOUND")


if __name__ == "__main__":
    _cli()
