"""
soul_pg_sync.py — Smart bidirectional sync between soul.json and fleet_brain Postgres.

Directions:
  json_to_pg(agent_id, soul_json_path, conn_or_dsn)  — import JSON graph → PG (upsert, smart merge)
  pg_to_json(agent_id, soul_json_path, conn_or_dsn)  — export PG → JSON (backup/restore)

Conflict resolution (JSON→PG):
  Same node_id + agent_id already exists:
    - Keep the richer content (len(content) wins; ties keep PG version)
    - Keep higher access_count (splay score proxy)
    - Merge tags (union)
    - Keep lower created_at (older origin wins)
    - Update synced_at to now()

Edge derivation:
  soul.json encodes edges via node.parents / node.children lists.
  These become soul_edges rows with relation_type='parent_of'
  (from_id=parent, to_id=child).  Deduplication is handled by the
  UNIQUE constraint on (from_id, to_id, agent_id, relation_type).

Usage (CLI):
  python -m mac.soul_pg_sync import --agent bullwinkle --soul ~/.hermes/soul.json
  python -m mac.soul_pg_sync export --agent bullwinkle --soul ~/.hermes/soul.json

Library:
  import mac.soul_pg_sync as sps
  report = sps.json_to_pg("bullwinkle", "~/.hermes/soul.json")
  print(report)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# DB connection helper
# ---------------------------------------------------------------------------


def _get_conn(conn_or_dsn=None):
    """Return a DB-API 2 connection (psycopg2 or psycopg v3).

    Priority:
      1. conn_or_dsn argument (connection object or DSN string)
      2. FLEET_BRAIN_DSN environment variable
      3. PGDATABASE=fleet_brain on hub (100.72.16.110)

    Driver preference: psycopg2 first (wider compatibility), psycopg v3 as
    fallback.  Both expose identical DB-API 2 cursor semantics used here.
    """
    # Resolve argument / env
    if conn_or_dsn is None:
        conn_or_dsn = os.environ.get(
            "FLEET_BRAIN_DSN", "dbname=fleet_brain user=postgres"
        )  # default: local Unix socket (pg_hba peer auth); set FLEET_BRAIN_DSN for remote

    # Already a connection object — pass through
    if not isinstance(conn_or_dsn, str):
        return conn_or_dsn

    # Try psycopg2, then psycopg v3
    try:
        import psycopg2

        return psycopg2.connect(conn_or_dsn)
    except (ImportError, Exception) as first_err:
        if isinstance(first_err, ImportError):
            pass
        else:
            # Driver loaded but connection failed — try psycopg v3 before giving up
            try:
                import psycopg

                return psycopg.connect(conn_or_dsn)
            except Exception:
                raise first_err  # surface the original error

    try:
        import psycopg

        return psycopg.connect(conn_or_dsn)
    except ImportError:
        raise RuntimeError(
            "A postgres driver is required: pip install psycopg2-binary  OR  pip install psycopg"
        )


# ---------------------------------------------------------------------------
# JSON → PG (import / upsert)
# ---------------------------------------------------------------------------


def json_to_pg(
    agent_id: str,
    soul_json_path: str | Path,
    conn_or_dsn=None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import soul.json into fleet_brain.soul_nodes / soul_edges.

    Returns a report dict:
      nodes_inserted, nodes_updated, nodes_skipped,
      edges_inserted, edges_skipped, errors
    """
    path = Path(soul_json_path).expanduser()
    data = json.loads(path.read_text())
    raw_nodes: dict = data.get("nodes", {})

    report = {
        "agent_id": agent_id,
        "source": str(path),
        "nodes_total": len(raw_nodes),
        "nodes_inserted": 0,
        "nodes_updated": 0,
        "nodes_skipped": 0,
        "edges_inserted": 0,
        "edges_skipped": 0,
        "errors": [],
        "dry_run": dry_run,
    }

    if not raw_nodes:
        return report

    conn = _get_conn(conn_or_dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                _upsert_nodes(cur, agent_id, raw_nodes, report, dry_run)
                _upsert_edges(cur, agent_id, raw_nodes, report, dry_run)
    finally:
        conn.close()

    return report


def _upsert_nodes(cur, agent_id, raw_nodes, report, dry_run):
    # Fetch existing rows for this agent in one shot
    cur.execute(
        "SELECT node_id, content, tags, pinned, score FROM soul_nodes WHERE agent_id = %s",
        (agent_id,),
    )
    existing = {row[0]: row for row in cur.fetchall()}

    for nid, nd in raw_nodes.items():
        content = nd.get("content", "")
        tags = list(nd.get("tags", []))
        pinned = bool(nd.get("pinned", False))
        access_count = int(nd.get("access_count", 0))
        created_at_ts = nd.get("created_at", time.time())
        # score proxy: same formula soul_graph uses for splay key
        import math

        last_accessed = nd.get("last_accessed", time.time())
        score = math.inf if pinned else (math.log(access_count + 1) + last_accessed / 86400)

        if nid in existing:
            _, ex_content, ex_tags, ex_pinned, ex_score = existing[nid]
            # Conflict resolution: keep richer version
            winner_content = content if len(content) >= len(ex_content or "") else ex_content
            winner_tags = list(set(tags) | set(ex_tags or []))
            winner_pinned = pinned or ex_pinned
            winner_score = max(score, ex_score or 0)

            if (
                winner_content == ex_content
                and set(winner_tags) == set(ex_tags or [])
                and winner_pinned == ex_pinned
            ):
                report["nodes_skipped"] += 1
                continue

            if not dry_run:
                cur.execute(
                    """
                    UPDATE soul_nodes
                    SET content = %s,
                        tags = %s,
                        pinned = %s,
                        score = %s,
                        synced_at = now()
                    WHERE node_id = %s AND agent_id = %s
                    """,
                    (winner_content, winner_tags, winner_pinned, winner_score, nid, agent_id),
                )
            report["nodes_updated"] += 1
        else:
            if not dry_run:
                cur.execute(
                    """
                    INSERT INTO soul_nodes
                        (node_id, agent_id, content, tags, pinned, score, created_at, synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s,
                            to_timestamp(%s),
                            now())
                    ON CONFLICT (node_id) DO NOTHING
                    """,
                    (nid, agent_id, content, tags, pinned, score, created_at_ts),
                )
            report["nodes_inserted"] += 1


def _upsert_edges(cur, agent_id, raw_nodes, report, dry_run):
    """Derive edges from parent/children lists and upsert to soul_edges."""
    seen: set[tuple[str, str]] = set()  # (from_id, to_id) dedup within this batch

    for nid, nd in raw_nodes.items():
        # Parent edges: parent → this node
        for parent_id in nd.get("parents", []):
            if parent_id == nid:
                continue  # self-loop guard
            pair = (parent_id, nid)
            if pair in seen:
                continue
            seen.add(pair)
            if not dry_run:
                cur.execute(
                    """
                    INSERT INTO soul_edges (from_id, to_id, agent_id, relation_type)
                    VALUES (%s, %s, %s, 'parent_of')
                    ON CONFLICT (from_id, to_id, agent_id, relation_type) DO NOTHING
                    """,
                    (parent_id, nid, agent_id),
                )
                affected = cur.rowcount
            else:
                affected = 1  # pretend for dry-run

            if affected:
                report["edges_inserted"] += 1
            else:
                report["edges_skipped"] += 1


# ---------------------------------------------------------------------------
# PG → JSON (export / backup)
# ---------------------------------------------------------------------------


def pg_to_json(
    agent_id: str,
    soul_json_path: str | Path,
    conn_or_dsn=None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Export soul_nodes + soul_edges from PG back to soul.json format.

    Useful for:
      - backup
      - bootstrapping a new agent node with the shared graph state
      - restoring after soul.json corruption

    Returns a report dict: nodes_exported, edges_exported
    """
    path = Path(soul_json_path).expanduser()
    report = {
        "agent_id": agent_id,
        "destination": str(path),
        "nodes_exported": 0,
        "edges_exported": 0,
        "dry_run": dry_run,
    }

    conn = _get_conn(conn_or_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT node_id, content, tags, pinned, score, created_at
                FROM soul_nodes
                WHERE agent_id = %s
                ORDER BY score DESC NULLS LAST
                """,
                (agent_id,),
            )
            pg_nodes = cur.fetchall()

            cur.execute(
                """
                SELECT from_id, to_id, relation_type
                FROM soul_edges
                WHERE agent_id = %s
                """,
                (agent_id,),
            )
            pg_edges = cur.fetchall()
    finally:
        conn.close()

    # Build parent/children maps from edges
    import time as _time
    import math

    parents_of: dict[str, list[str]] = {}  # child → [parents]
    children_of: dict[str, list[str]] = {}  # parent → [children]

    for from_id, to_id, rel in pg_edges:
        if rel == "parent_of":
            parents_of.setdefault(to_id, []).append(from_id)
            children_of.setdefault(from_id, []).append(to_id)
        report["edges_exported"] += 1

    nodes_dict = {}
    now = _time.time()
    for nid, content, tags, pinned, score, created_at in pg_nodes:
        # Reconstruct access_count from score (inverse of splay key formula)
        created_ts = created_at.timestamp() if created_at else now
        if pinned or score is None or score == float("inf"):
            access_count = 0
        else:
            # score ≈ log(access_count+1) + last_accessed/86400
            # We can't recover last_accessed perfectly; approximate
            access_count = max(0, round(math.exp(max(0, score - now / 86400)) - 1))

        nodes_dict[nid] = {
            "id": nid,
            "content": content,
            "tags": list(tags or []),
            "parents": parents_of.get(nid, []),
            "children": children_of.get(nid, []),
            "pinned": bool(pinned),
            "created_at": created_ts,
            "last_accessed": now,
            "access_count": access_count,
            "metadata": {},
        }
        report["nodes_exported"] += 1

    out = {
        "name": "soul",
        "exploration": False,
        "nodes": nodes_dict,
    }

    if not dry_run:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(out, indent=2))
        tmp.replace(path)

    return report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _cli():
    import argparse

    parser = argparse.ArgumentParser(description="soul_pg_sync — sync soul graph ↔ Postgres")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for cmd in ("import", "export"):
        p = sub.add_parser(cmd)
        p.add_argument("--agent", required=True, help="agent_id (e.g. bullwinkle)")
        p.add_argument("--soul", required=True, help="Path to soul.json")
        p.add_argument("--dsn", default=None, help="Postgres DSN (overrides FLEET_BRAIN_DSN env)")
        p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    dsn = args.dsn or os.environ.get("FLEET_BRAIN_DSN")

    if args.cmd == "import":
        report = json_to_pg(args.agent, args.soul, dsn, dry_run=args.dry_run)
    else:
        report = pg_to_json(args.agent, args.soul, dsn, dry_run=args.dry_run)

    import pprint

    pprint.pprint(report)
    sys.exit(0 if not report.get("errors") else 1)


if __name__ == "__main__":
    _cli()
