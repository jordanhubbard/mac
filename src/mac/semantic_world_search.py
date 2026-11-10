"""Semantic and full-text search over the shared fleet soul graph.

Two search paths, one function:

1. Full-text (fast, no inference cost) — always works.
   Uses the ``search_vec`` tsvector index on ``soul_nodes``.

2. Semantic (vector cosine similarity) — requires the hub inference endpoint.
   Embeds the query via ``POST /v1/embeddings``, then does an
   ``embedding <=> query_vec`` nearest-neighbour scan.

``hybrid_search`` runs both and merges by rank.

CLI::

    python -m mac.semantic_world_search "consequence engine" --limit 5

or from Python::

    from mac.semantic_world_search import hybrid_search
    results = hybrid_search("consequence engine", db_url=..., hub_url=..., token=...)

Environment variables (all optional when db_url / hub_url are passed explicitly):

    MAC_WORKER_TOKEN   — bearer token for the hub inference endpoint
    MAC_HUB_URL        — hub base URL (default http://100.72.16.110:8789)
    AGENT_BRAIN_URL    — PostgreSQL DSN (default postgresql://fleet_agent@100.72.16.110:5432/agent_brain)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import psycopg2  # type: ignore[import-untyped]
    import psycopg2.extras  # type: ignore[import-untyped]

    _HAS_PSYCOPG2 = True
except ImportError:
    psycopg2 = None  # type: ignore[assignment]
    _HAS_PSYCOPG2 = False

# ── defaults ────────────────────────────────────────────────────────────────

DEFAULT_DB_URL = "postgresql://fleet_agent@100.72.16.110:5432/agent_brain"
DEFAULT_HUB_URL = "http://100.72.16.110:8789"
# 1536-dim to match the soul_nodes.embedding column
EMBEDDING_MODEL = "azure/openai/text-embedding-3-small"
EMBEDDING_DIMS = 1536

# ── result type ──────────────────────────────────────────────────────────────


@dataclass
class SearchResult:
    id: str
    agent: str
    content: str
    tags: List[str]
    score: float
    method: str  # "fts" | "semantic" | "hybrid"
    rank: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    def short(self) -> str:
        snippet = self.content[:120].replace("\n", " ")
        return f"[{self.rank}] {self.id} ({self.agent}) [{','.join(self.tags)}]\n    {snippet}"


# ── embedding ────────────────────────────────────────────────────────────────


def _embed(query: str, hub_url: str, token: str) -> List[float]:
    """Call the hub /v1/embeddings endpoint and return the embedding vector."""
    url = hub_url.rstrip("/") + "/v1/embeddings"
    payload = json.dumps(
        {"model": EMBEDDING_MODEL, "input": query, "dimensions": EMBEDDING_DIMS}
    ).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            return body["data"][0]["embedding"]
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Embedding request failed {e.code}: {body}") from e


# ── search ───────────────────────────────────────────────────────────────────


def fts_search(
    query: str,
    limit: int = 10,
    agent: Optional[str] = None,
    db_url: Optional[str] = None,
) -> List[SearchResult]:
    """Full-text search using the soul_nodes tsvector index."""
    if not _HAS_PSYCOPG2:
        raise RuntimeError("psycopg2 not available; install it to use fts_search")

    db_url = db_url or os.environ.get("AGENT_BRAIN_URL", DEFAULT_DB_URL)
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = """
                SELECT id, agent, content, tags,
                       ts_rank_cd(search_vec, plainto_tsquery('english', %s)) AS rank_score
                FROM soul_nodes
                WHERE search_vec @@ plainto_tsquery('english', %s)
                {agent_filter}
                ORDER BY rank_score DESC
                LIMIT %s
            """.format(agent_filter="AND agent = %s" if agent else "")

            params = [query, query] + ([agent] if agent else []) + [limit]
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        SearchResult(
            id=r["id"],
            agent=r["agent"],
            content=r["content"],
            tags=r["tags"] or [],
            score=float(r["rank_score"]),
            method="fts",
            rank=i + 1,
        )
        for i, r in enumerate(rows)
    ]


def semantic_search(
    query: str,
    limit: int = 10,
    agent: Optional[str] = None,
    db_url: Optional[str] = None,
    hub_url: Optional[str] = None,
    token: Optional[str] = None,
) -> List[SearchResult]:
    """Vector cosine-similarity search against soul_nodes.embedding.

    Nodes without embeddings are excluded (embedding IS NOT NULL).
    """
    if not _HAS_PSYCOPG2:
        raise RuntimeError("psycopg2 not available; install it to use semantic_search")

    db_url = db_url or os.environ.get("AGENT_BRAIN_URL", DEFAULT_DB_URL)
    hub_url = hub_url or os.environ.get("MAC_HUB_URL", DEFAULT_HUB_URL)
    token = token or os.environ.get("MAC_WORKER_TOKEN", "")
    if not token:
        raise RuntimeError("No token: set MAC_WORKER_TOKEN or pass token=")

    vec = _embed(query, hub_url, token)
    vec_literal = "[" + ",".join(str(v) for v in vec) + "]"

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = """
                SELECT id, agent, content, tags,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM soul_nodes
                WHERE embedding IS NOT NULL
                {agent_filter}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """.format(agent_filter="AND agent = %s" if agent else "")

            params = [vec_literal, vec_literal] + ([agent] if agent else []) + [limit]
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        SearchResult(
            id=r["id"],
            agent=r["agent"],
            content=r["content"],
            tags=r["tags"] or [],
            score=float(r["similarity"]),
            method="semantic",
            rank=i + 1,
        )
        for i, r in enumerate(rows)
    ]


def hybrid_search(
    query: str,
    limit: int = 10,
    agent: Optional[str] = None,
    db_url: Optional[str] = None,
    hub_url: Optional[str] = None,
    token: Optional[str] = None,
    semantic_weight: float = 0.6,
) -> List[SearchResult]:
    """Merge FTS and semantic results with reciprocal rank fusion.

    Falls back to FTS-only if the embedding call fails (no token, model error).
    """
    fts_results = fts_search(query, limit=limit * 2, agent=agent, db_url=db_url)

    try:
        sem_results = semantic_search(
            query, limit=limit * 2, agent=agent, db_url=db_url, hub_url=hub_url, token=token
        )
    except Exception as exc:
        # Graceful degradation: semantic unavailable, return FTS only
        sys.stderr.write(f"[semantic_world_search] semantic path unavailable: {exc}\n")
        for i, r in enumerate(fts_results[:limit]):
            r.rank = i + 1
            r.method = "fts"
        return fts_results[:limit]

    # Reciprocal rank fusion: score = Σ weight / (k + rank)
    k = 60
    scores: Dict[str, float] = {}
    by_id: Dict[str, SearchResult] = {}

    for r in fts_results:
        scores[r.id] = scores.get(r.id, 0.0) + (1 - semantic_weight) / (k + r.rank)
        by_id[r.id] = r

    for r in sem_results:
        scores[r.id] = scores.get(r.id, 0.0) + semantic_weight / (k + r.rank)
        if r.id not in by_id:
            by_id[r.id] = r

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    results = []
    for i, (node_id, fused_score) in enumerate(ranked):
        r = by_id[node_id]
        r.score = fused_score
        r.rank = i + 1
        r.method = "hybrid"
        results.append(r)

    return results


# ── CLI ──────────────────────────────────────────────────────────────────────


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Search the fleet soul graph (FTS + semantic).")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--agent", default=None, help="Restrict to one agent")
    parser.add_argument(
        "--mode",
        choices=["hybrid", "fts", "semantic"],
        default="hybrid",
        help="Search mode (default: hybrid)",
    )
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--hub-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    kwargs = dict(
        query=args.query,
        limit=args.limit,
        agent=args.agent,
        db_url=args.db_url,
    )

    if args.mode == "fts":
        results = fts_search(**kwargs)
    elif args.mode == "semantic":
        results = semantic_search(**kwargs, hub_url=args.hub_url, token=args.token)
    else:
        results = hybrid_search(**kwargs, hub_url=args.hub_url, token=args.token)

    if args.json:
        out = [
            {
                "rank": r.rank,
                "id": r.id,
                "agent": r.agent,
                "tags": r.tags,
                "score": r.score,
                "method": r.method,
                "content": r.content[:300],
            }
            for r in results
        ]
        print(json.dumps(out, indent=2))
    else:
        print(f"Query: {args.query!r}  mode={args.mode}  limit={args.limit}")
        print(f"{'─' * 72}")
        for r in results:
            print(r.short())
        print(f"{'─' * 72}")
        print(f"{len(results)} results")


if __name__ == "__main__":
    _cli()
