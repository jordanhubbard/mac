#!/usr/bin/env python3
"""
soul_pg_migrate.py — Migrate agent soul graph from soul.json + legacy SQLite to
the shared fleet Postgres (agent_brain database).

Usage:
    python scripts/soul_pg_migrate.py \
        --agent natasha \
        --soul-file ~/.hermes/soul.json \
        --sqlite ~/AgentFS/natasha-memory-graph/memory_graph.db \
        --pg-url "postgresql://fleet_agent:<pass>@100.72.16.110:5432/agent_brain" \
        [--dry-run]

What it does:
1. ALTER TABLE soul_nodes ADD COLUMN IF NOT EXISTS agent TEXT (idempotent)
2. Loads soul.json nodes + SQLite nodes; deduplicates by ID and exact content
3. Upserts into soul_nodes — merges tags, OR-s pinned, preserves existing content
4. Upserts edges; drops dangling edges
5. Backfills agent column on any existing rows with NULL agent

Merge semantics: existing PG content is NOT overwritten. Only tags/pinned are
merged upward. Safe to re-run (idempotent).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_soul_json(path: Path) -> list[dict]:
    """Load nodes from soul.json. Returns list of node dicts."""
    if not path.exists():
        print(f"  [skip] soul.json not found: {path}")
        return []
    data = json.loads(path.read_text())
    nodes = data.get("nodes", {})
    if isinstance(nodes, dict):
        return list(nodes.values())
    return nodes  # already a list


def _load_sqlite(path: Path) -> list[dict]:
    """Load nodes from legacy memory_graph SQLite. Returns list of node dicts."""
    if not path.exists():
        print(f"  [skip] SQLite not found: {path}")
        return []
    import sqlite3

    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM nodes").fetchall()
    con.close()
    result = []
    for row in rows:
        d = dict(row)
        # Normalize tags: JSON string → list
        tags_raw = d.get("tags", "[]")
        if isinstance(tags_raw, str):
            try:
                d["tags"] = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
        # Normalize kind → tag
        kind = d.pop("kind", None)
        if kind and kind not in d.get("tags", []):
            d.setdefault("tags", []).append(kind)
        # Normalize timestamps: may be ISO or epoch float
        for field in ("created_at", "last_accessed"):
            val = d.get(field)
            if isinstance(val, str):
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(val)
                    d[field] = dt.timestamp()
                except ValueError:
                    d[field] = time.time()
            elif val is None:
                d[field] = time.time()
        d.setdefault("parents", [])
        d.setdefault("children", [])
        d.setdefault("pinned", False)
        d.setdefault("access_count", 0)
        d.setdefault("metadata", {})
        d.setdefault("content", d.get("summary", ""))
        result.append(d)
    return result


def _load_sqlite_edges(path: Path) -> list[tuple[str, str]]:
    """Load edges from legacy SQLite. Returns list of (parent_id, child_id)."""
    if not path.exists():
        return []
    import sqlite3

    con = sqlite3.connect(str(path))
    rows = con.execute("SELECT from_id, to_id FROM edges").fetchall()
    con.close()
    return [(r[0], r[1]) for r in rows]


def _merge_nodes(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Deduplicate incoming nodes against existing. Returns merged list."""
    seen_ids: dict[str, dict] = {}
    for n in existing:
        seen_ids[n["id"]] = n

    for n in incoming:
        nid = n["id"]
        if nid not in seen_ids:
            seen_ids[nid] = n
        else:
            # Merge: union tags, OR pinned. Do NOT overwrite content.
            ex = seen_ids[nid]
            ex["tags"] = list(set(ex.get("tags", [])) | set(n.get("tags", [])))
            ex["pinned"] = ex.get("pinned", False) or n.get("pinned", False)

    return list(seen_ids.values())


def migrate(
    agent: str,
    soul_file: Path,
    sqlite_file: Path,
    pg_url: str,
    dry_run: bool = False,
) -> None:
    import psycopg  # type: ignore[import]
    import datetime

    print(f"\n=== Soul graph migration: {agent} → Postgres ===")

    # 1. Load source data
    print("\n[1] Loading source data...")
    soul_nodes = _load_soul_json(soul_file)
    print(f"  soul.json: {len(soul_nodes)} nodes")
    sqlite_nodes = _load_sqlite(sqlite_file)
    print(f"  SQLite:    {len(sqlite_nodes)} nodes")

    all_nodes = _merge_nodes(soul_nodes, sqlite_nodes)
    print(f"  Merged:    {len(all_nodes)} nodes (after dedup)")

    # Gather all edges
    soul_edges: list[tuple[str, str]] = []
    all_ids = {n["id"] for n in all_nodes}
    for n in all_nodes:
        for pid in n.get("parents", []):
            if pid in all_ids:
                soul_edges.append((pid, n["id"]))
        for cid in n.get("children", []):
            if cid in all_ids:
                soul_edges.append((n["id"], cid))
    sqlite_edges = _load_sqlite_edges(sqlite_file)
    all_edges_raw = set(soul_edges) | set(sqlite_edges)
    # Drop dangling edges
    all_edges = [(p, c) for p, c in all_edges_raw if p in all_ids and c in all_ids]
    print(f"  Edges:     {len(all_edges)} (after dropping dangling)")

    if dry_run:
        print("\n[DRY RUN] Would upsert:")
        for n in all_nodes[:5]:
            print(f"  [{n['id']}] {n.get('content','')[:60]!r}")
        if len(all_nodes) > 5:
            print(f"  ... and {len(all_nodes) - 5} more")
        print(f"  Edges: {len(all_edges)}")
        return

    # 2. Connect and verify schema
    print("\n[2] Connecting to Postgres...")
    conn = psycopg.connect(pg_url, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='soul_nodes' AND column_name='agent'"
        )
        if not cur.fetchone():
            raise RuntimeError(
                "soul_nodes.agent column missing — run scripts/soul_pg_migrate.py as "
                "a superuser first to create the schema (or hub admin already did it)"
            )
    print("  agent column: present")

    # 3. Upsert nodes
    print(f"\n[3] Upserting {len(all_nodes)} nodes...")
    upserted = 0
    with conn.cursor() as cur:
        for n in all_nodes:
            created = n.get("created_at", time.time())
            accessed = n.get("last_accessed", time.time())
            if isinstance(created, (int, float)):
                created_dt = datetime.datetime.fromtimestamp(float(created), tz=datetime.timezone.utc)
            else:
                created_dt = datetime.datetime.now(datetime.timezone.utc)
            if isinstance(accessed, (int, float)):
                accessed_dt = datetime.datetime.fromtimestamp(float(accessed), tz=datetime.timezone.utc)
            else:
                accessed_dt = datetime.datetime.now(datetime.timezone.utc)
            cur.execute(
                """
                INSERT INTO soul_nodes
                  (id, content, tags, parents, pinned, score,
                   created_at, updated_at, last_accessed, access_count,
                   metadata, agent)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                  tags         = soul_nodes.tags || EXCLUDED.tags,
                  pinned       = soul_nodes.pinned OR EXCLUDED.pinned,
                  updated_at   = now()
                """,
                (
                    n["id"],
                    n.get("content") or n.get("summary", ""),
                    list(set(n.get("tags", []))),
                    n.get("parents", []),
                    n.get("pinned", False) is True or n.get("pinned", 0) == 1,
                    1.0,
                    created_dt,
                    accessed_dt,
                    int(n.get("access_count", 0)),
                    json.dumps(n.get("metadata", {})),
                    agent,
                ),
            )
            upserted += 1
    conn.commit()
    print(f"  Upserted: {upserted} nodes")

    # 4. Upsert edges
    print(f"\n[4] Upserting {len(all_edges)} edges...")
    with conn.cursor() as cur:
        for parent_id, child_id in all_edges:
            cur.execute(
                """
                INSERT INTO soul_edges (parent_id, child_id, relation)
                VALUES (%s, %s, 'child')
                ON CONFLICT (parent_id, child_id, relation) DO NOTHING
                """,
                (parent_id, child_id),
            )
    conn.commit()
    print(f"  Edges: done")

    # 5. Backfill agent column on any NULL rows
    print("\n[5] Backfilling NULL agent values...")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE soul_nodes SET agent = %s WHERE agent = 'unknown'",
            (agent,),
        )
        backfilled = cur.rowcount
    conn.commit()
    print(f"  Backfilled: {backfilled} rows")

    conn.close()
    print(f"\n✓ Migration complete: {upserted} nodes, {len(all_edges)} edges → agent={agent}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate soul graph to shared Postgres")
    parser.add_argument("--agent", required=True, help="Agent name (e.g. natasha)")
    parser.add_argument(
        "--soul-file", type=Path, default=None,
        help="Path to soul.json (default: ~/.hermes/soul.json)",
    )
    parser.add_argument(
        "--sqlite", type=Path, default=None,
        help="Path to legacy SQLite memory_graph.db (optional)",
    )
    parser.add_argument("--pg-url", required=True, help="Postgres connection URL")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, don't write")
    args = parser.parse_args()

    soul_file = args.soul_file or Path.home() / ".hermes" / "soul.json"
    sqlite_file = args.sqlite or Path.home() / "AgentFS" / f"{args.agent}-memory-graph" / "memory_graph.db"

    migrate(
        agent=args.agent,
        soul_file=soul_file,
        sqlite_file=sqlite_file,
        pg_url=args.pg_url,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
