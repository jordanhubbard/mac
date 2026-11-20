#!/usr/bin/env python3
"""embed_soul_nodes.py — generate embeddings for un-embedded soul_nodes.

Fetches soul nodes with no embedding from agent_brain, calls the hub
embedding endpoint (azure/openai/text-embedding-3-small, 1536-dim),
and upserts the embedding back.

Usage:
    python scripts/embed_soul_nodes.py [--agent bullwinkle] [--batch 20] [--dry-run]

Environment:
    MAC_WORKER_TOKEN   — bearer token for hub embeddings
    MAC_HUB_URL        — hub base URL (default: http://100.72.16.110:8789)
    AGENT_BRAIN_URL    — postgres DSN (default: postgresql://fleet_agent:fleetlink2026@100.72.16.110:5432/agent_brain)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import List, Optional

PG_DSN_DEFAULT = "postgresql://fleet_agent:fleetlink2026@100.72.16.110:5432/agent_brain"
HUB_URL_DEFAULT = "http://100.72.16.110:8789"
EMBED_MODEL = "azure/openai/text-embedding-3-small"
EMBED_DIMS = 1536
MAX_CONTENT_CHARS = 8000  # clip very long nodes to avoid token limits


def embed_texts(texts: List[str], token: str, hub_url: str) -> List[Optional[List[float]]]:
    """Call hub /v1/embeddings for a batch of texts. Returns list of vectors (None on failure)."""
    url = f"{hub_url.rstrip('/')}/v1/embeddings"
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        # data["data"] is a list of {"embedding": [...], "index": N}
        embeddings_by_index = {item["index"]: item["embedding"] for item in data.get("data", [])}
        return [embeddings_by_index.get(i) for i in range(len(texts))]
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"[embed] HTTP {e.code}: {body[:200]}", file=sys.stderr)
        return [None] * len(texts)
    except Exception as exc:
        print(f"[embed] Error: {exc}", file=sys.stderr)
        return [None] * len(texts)


def run(agent: str, batch_size: int, dry_run: bool, limit: Optional[int]) -> None:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        sys.exit("psycopg (v3) required: pip install psycopg[binary]")

    token = os.environ.get("MAC_WORKER_TOKEN", "")
    hub_url = os.environ.get("MAC_HUB_URL", HUB_URL_DEFAULT)
    dsn = os.environ.get("AGENT_BRAIN_URL", PG_DSN_DEFAULT)

    if not token:
        sys.exit("MAC_WORKER_TOKEN not set")

    print(f"[embed_soul_nodes] agent={agent} batch={batch_size} dry_run={dry_run}")

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        # Fetch un-embedded nodes
        limit_clause = f"LIMIT {limit}" if limit else ""
        rows = conn.execute(
            f"""SELECT id, content
                FROM soul_nodes
                WHERE agent = %s AND embedding IS NULL
                ORDER BY created_at
                {limit_clause}""",
            (agent,),
        ).fetchall()

    print(f"[embed_soul_nodes] found {len(rows)} un-embedded nodes")
    if not rows:
        print("[embed_soul_nodes] nothing to do")
        return

    total_embedded = 0
    total_failed = 0

    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        ids = [r["id"] for r in batch]
        texts = [r["content"][:MAX_CONTENT_CHARS] for r in batch]

        print(
            f"[embed_soul_nodes] batch {i // batch_size + 1}: {len(batch)} nodes",
            end="",
            flush=True,
        )

        if dry_run:
            print(" [dry-run, skipping]")
            continue

        vectors = embed_texts(texts, token, hub_url)

        # Upsert embeddings
        with psycopg.connect(dsn) as conn:
            for node_id, vec in zip(ids, vectors):
                if vec is None:
                    total_failed += 1
                    print(f"\n  [FAIL] {node_id}", file=sys.stderr)
                    continue
                if len(vec) != EMBED_DIMS:
                    total_failed += 1
                    print(f"\n  [WRONG DIMS {len(vec)}] {node_id}", file=sys.stderr)
                    continue
                # Format as Postgres vector literal
                vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"
                conn.execute(
                    "UPDATE soul_nodes SET embedding = %s::vector, updated_at = now() WHERE id = %s",
                    (vec_str, node_id),
                )
            conn.commit()
            total_embedded += sum(1 for v in vectors if v is not None and len(v) == EMBED_DIMS)

        print(f" done (+{sum(1 for v in vectors if v is not None)})")

        # Rate limit: 100ms between batches
        if i + batch_size < len(rows):
            time.sleep(0.1)

    print(f"[embed_soul_nodes] complete: {total_embedded} embedded, {total_failed} failed")


def main() -> None:
    ap = argparse.ArgumentParser(description="Embed un-embedded soul_nodes")
    ap.add_argument("--agent", default="bullwinkle", help="Agent name (default: bullwinkle)")
    ap.add_argument("--batch", type=int, default=20, help="Batch size for embedding calls")
    ap.add_argument("--dry-run", action="store_true", help="List nodes without embedding")
    ap.add_argument("--limit", type=int, default=None, help="Max nodes to process")
    args = ap.parse_args()
    run(args.agent, args.batch, args.dry_run, args.limit)


if __name__ == "__main__":
    main()
