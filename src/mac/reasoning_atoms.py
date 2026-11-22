"""reasoning_atoms — schema v2 for fleet cross-agent reasoning coordination.

Each agent writes atoms to a shared Qdrant collection (``reasoning_atoms``)
when their insight is worth signalling to peers.  The publication criterion
prevents noise: an insight is worth publishing if it is **empirically grounded**
OR **trajectory-diverging** (the peer is heading somewhere different).

Schema v2 required fields
--------------------------
- author       : agent_id string (e.g. "agent_bullwinkle")
- thread       : slug identifying the exploration thread
- content      : the insight itself
- next_focus   : where this author is heading next — the trajectory signal
- provenance   : list of soul node IDs this insight builds on
- empirical    : bool — was this based on measurement?

Optional
--------
- tension       : list of conflicting atom IDs in the same collection
- run           : cron run number (int)
- ts            : ISO-8601 UTC timestamp (auto-generated if omitted)
- schema_version: always "2.0"

Publication criterion
---------------------
Publish when EITHER:
1. ``empirical=True``  — measurement-grounded, always worth signalling
2. ``cosine(atom.content, peer.next_focus) < threshold``  for ALL peers
   — we are heading somewhere different from every peer

Read-before-write protocol (not code-enforced; documented here):
1. Query top-3 atoms by other authors, filtered by thread
2. Compare candidate content against each peer's ``next_focus``
3. If ANY score >= threshold: same territory — extend or confirm theirs
4. If ALL scores < threshold: divergent — publish

CLI::

    python -m mac.reasoning_atoms validate atom.json
    python -m mac.reasoning_atoms check atom.json

Environment for Qdrant writes (optional — only needed for write/backfill):

    QDRANT_URL          — default http://100.72.16.110:6333
    MAC_WORKER_TOKEN    — bearer for hub embeddings
    MAC_HUB_URL         — default http://100.72.16.110:8789
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_VERSION = "2.0"
DEFAULT_THRESHOLD = 0.4
QDRANT_COLLECTION = "reasoning_atoms"
EMBEDDING_DIMS = 2048  # text-embedding-3-large in Qdrant


@dataclass
class ReasoningAtom:
    """One reasoning atom, schema v2."""

    author: str
    thread: str
    content: str
    next_focus: str
    provenance: List[str]
    empirical: bool
    tension: List[str] = field(default_factory=list)
    run: Optional[int] = None
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: str = SCHEMA_VERSION


@dataclass
class PublicationDecision:
    should_publish: bool
    reason: str  # "empirical" | "no_peers" | "divergent" | "overlap:<peer_author>"
    max_overlap_score: float = 0.0
    blocking_peer: Optional[str] = None


# ── Factories / validation ────────────────────────────────────────────────────


def make_atom(
    *,
    author: str,
    thread: str,
    content: str,
    next_focus: str,
    provenance: List[str],
    empirical: bool,
    tension: Optional[List[str]] = None,
    run: Optional[int] = None,
    ts: Optional[str] = None,
) -> ReasoningAtom:
    """Construct and return a ReasoningAtom with defaults filled in."""
    return ReasoningAtom(
        author=author,
        thread=thread,
        content=content,
        next_focus=next_focus,
        provenance=list(provenance),
        empirical=empirical,
        tension=list(tension) if tension is not None else [],
        run=run,
        ts=ts or datetime.now(timezone.utc).isoformat(),
        schema_version=SCHEMA_VERSION,
    )


def validate_atom(atom: ReasoningAtom) -> None:
    """Raise ValueError for any schema v2 violation."""
    required_fields = {
        "author": atom.author,
        "thread": atom.thread,
        "content": atom.content,
        "next_focus": atom.next_focus,
    }
    for name, val in required_fields.items():
        if not val or not val.strip():
            raise ValueError(f"reasoning_atom: required field '{name}' is empty")

    if atom.schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"reasoning_atom: schema_version must be {SCHEMA_VERSION!r}, got {atom.schema_version!r}"
        )


# ── Maths ─────────────────────────────────────────────────────────────────────


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if len(a) != len(b):
        raise ValueError(f"Vector length mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


# ── Publication criterion ─────────────────────────────────────────────────────

EmbedFn = Callable[[str], List[float]]


def publication_criterion(
    atom: ReasoningAtom,
    peers: List[ReasoningAtom],
    embed_fn: Optional[EmbedFn] = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> PublicationDecision:
    """Decide whether this atom should be published to the shared collection.

    Rules (in order):
    1. If ``atom.empirical`` is True: always publish ("empirical").
    2. If ``peers`` is empty: publish ("no_peers").
    3. For each peer, compute cosine(atom.content, peer.next_focus).
       If ANY score >= threshold: block ("overlap:<peer.author>").
       If ALL scores < threshold: publish ("divergent").

    ``embed_fn`` converts a text string to a float vector.  When omitted and
    there are peers, the function falls back to a simple bag-of-words overlap
    heuristic so callers without an inference endpoint can still use this API.
    """
    validate_atom(atom)

    # Rule 1: empirical always publishes
    if atom.empirical:
        return PublicationDecision(should_publish=True, reason="empirical")

    # Rule 2: no peers
    if not peers:
        return PublicationDecision(should_publish=True, reason="no_peers")

    # Rule 3: trajectory comparison
    _embed = embed_fn or _bow_embed

    atom_vec = _embed(atom.content)
    max_score = 0.0
    blocking_peer: Optional[str] = None

    for peer in peers:
        if not peer.next_focus:
            continue
        peer_vec = _embed(peer.next_focus)
        score = _cosine_similarity(atom_vec, peer_vec)
        if score > max_score:
            max_score = score
            blocking_peer = peer.author

    if max_score >= threshold:
        return PublicationDecision(
            should_publish=False,
            reason=f"overlap:{blocking_peer}",
            max_overlap_score=max_score,
            blocking_peer=blocking_peer,
        )

    return PublicationDecision(
        should_publish=True,
        reason="divergent",
        max_overlap_score=max_score,
    )


# ── Fallback bag-of-words embedding ──────────────────────────────────────────


def _bow_embed(text: str) -> List[float]:
    """Very simple bag-of-words embedding for offline / no-inference use.

    Returns a normalised TF vector over lowercased word tokens.
    Not suitable for production semantic search — use a real embedding endpoint.
    """
    words = text.lower().split()
    if not words:
        return [0.0]
    vocab: dict[str, int] = {}
    for w in words:
        vocab.setdefault(w, len(vocab))
    vec = [0.0] * len(vocab)
    for w in words:
        vec[vocab[w]] += 1.0
    # pad / truncate to fixed width so cosine works between two BOW vectors
    # (they already share the same vocab since built from same call sequence)
    return vec


# ── Qdrant write helpers ──────────────────────────────────────────────────────


def write_atom_to_qdrant(
    atom: ReasoningAtom,
    point_id: Optional[str] = None,
    qdrant_url: Optional[str] = None,
    embed_fn: Optional[EmbedFn] = None,
) -> str:
    """Write a validated atom to the Qdrant ``reasoning_atoms`` collection.

    Returns the point UUID used for the write.

    ``embed_fn`` must return a 2048-dim vector (text-embedding-3-large
    compatible).  If omitted, a zero vector placeholder is used and a warning
    is emitted — suitable for backfill of payload-only updates.

    Requires: ``qdrant-client`` package (pip install qdrant-client).
    """
    validate_atom(atom)

    try:
        from qdrant_client import QdrantClient  # type: ignore[import]
        from qdrant_client.models import (  # type: ignore[import]
            Distance,
            PointStruct,
            VectorParams,
        )
    except ImportError as exc:
        raise RuntimeError(
            "qdrant-client is required for write_atom_to_qdrant: pip install qdrant-client"
        ) from exc

    import uuid as _uuid

    qdrant_url = qdrant_url or os.environ.get("QDRANT_URL", "http://100.72.16.110:6333")
    client = QdrantClient(url=qdrant_url)

    # Ensure collection exists
    existing = {c.name for c in client.get_collections().collections}
    if QDRANT_COLLECTION not in existing:
        client.create_collection(
            QDRANT_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIMS, distance=Distance.COSINE),
        )

    # Embedding
    if embed_fn is not None:
        vector = embed_fn(atom.content)
    else:
        sys.stderr.write(
            "[reasoning_atoms] WARNING: no embed_fn supplied; using zero vector placeholder\n"
        )
        vector = [0.0] * EMBEDDING_DIMS

    pid = point_id or str(_uuid.uuid4())

    from dataclasses import asdict as _asdict

    payload = _asdict(atom)

    client.upsert(
        collection_name=QDRANT_COLLECTION,
        points=[PointStruct(id=pid, vector=vector, payload=payload)],
    )
    return pid


def backfill_next_focus(
    point_id: str,
    next_focus: str,
    qdrant_url: Optional[str] = None,
) -> None:
    """Add ``next_focus`` and ``schema_version`` to an existing v1 atom payload.

    Does not re-embed — vector stays unchanged.
    """
    if not next_focus.strip():
        raise ValueError("next_focus cannot be empty for backfill")

    try:
        from qdrant_client import QdrantClient  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "qdrant-client is required for backfill_next_focus: pip install qdrant-client"
        ) from exc

    qdrant_url = qdrant_url or os.environ.get("QDRANT_URL", "http://100.72.16.110:6333")
    client = QdrantClient(url=qdrant_url)
    client.set_payload(
        collection_name=QDRANT_COLLECTION,
        payload={"next_focus": next_focus, "schema_version": SCHEMA_VERSION},
        points=[point_id],
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def _cli() -> None:
    import argparse
    from dataclasses import asdict

    parser = argparse.ArgumentParser(
        description="reasoning_atoms CLI — validate and inspect schema v2 atoms"
    )
    sub = parser.add_subparsers(dest="cmd")

    val_p = sub.add_parser("validate", help="Validate a JSON atom file against schema v2")
    val_p.add_argument("file", help="Path to JSON file with atom payload")

    check_p = sub.add_parser(
        "check", help="Run publication criterion against a candidate atom (no peers)"
    )
    check_p.add_argument("file", help="Path to JSON file with atom payload")

    query_p = sub.add_parser(
        "query",
        help="Search reasoning_atoms in Qdrant by semantic proximity",
    )
    query_p.add_argument("text", nargs="+", help="Query text (space-separated words)")
    query_p.add_argument(
        "--author", dest="author_filter", default=None, help="Filter by author agent-id"
    )
    query_p.add_argument(
        "--thread", dest="thread_filter", default=None, help="Filter by thread slug"
    )
    query_p.add_argument("--limit", type=int, default=5, help="Max results (default 5)")
    query_p.add_argument(
        "--qdrant-url", dest="qdrant_url", default=None, help="Qdrant endpoint URL"
    )

    write_p = sub.add_parser(
        "write",
        help="Write a validated atom JSON file to Qdrant (use --dry-run to validate only)",
    )
    write_p.add_argument("file", help="Path to JSON file with atom payload")
    write_p.add_argument(
        "--qdrant-url", dest="qdrant_url", default=None, help="Qdrant endpoint URL"
    )
    write_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate the atom and print it without writing to Qdrant",
    )

    args = parser.parse_args()

    if args.cmd in ("validate", "check"):
        with open(args.file) as fh:
            data = json.load(fh)
        atom = ReasoningAtom(
            **{k: v for k, v in data.items() if k in ReasoningAtom.__dataclass_fields__}
        )  # type: ignore[attr-defined]
        try:
            validate_atom(atom)
            print("OK: atom passes schema v2 validation")
        except ValueError as e:
            print(f"INVALID: {e}")
            sys.exit(1)

        if args.cmd == "check":
            decision = publication_criterion(atom, peers=[])
            print(
                f"Publication: should_publish={decision.should_publish}  reason={decision.reason}"
            )
    elif args.cmd == "query":
        query_text = " ".join(args.text)
        results = query_atoms(
            query_text,
            qdrant_url=args.qdrant_url,
            author_filter=args.author_filter,
            thread_filter=args.thread_filter,
            limit=args.limit,
        )
        if not results:
            print("No atoms found.")
        else:
            for r in results:
                print(
                    f"[score={r['score']:.3f}] {r['author']} | {r['thread']} | {r['content'][:120]}"
                )
    elif args.cmd == "write":
        with open(args.file) as fh:
            data = json.load(fh)
        try:
            atom = ReasoningAtom(
                **{k: v for k, v in data.items() if k in ReasoningAtom.__dataclass_fields__}
            )  # type: ignore[attr-defined]
            validate_atom(atom)
        except (ValueError, TypeError) as e:
            print(f"INVALID: {e}", file=sys.stderr)
            sys.exit(1)
        if args.dry_run:
            import dataclasses

            print(f"[dry-run] valid atom from {args.file}:")
            print(json.dumps(dataclasses.asdict(atom), indent=2))
        else:
            pid = write_atom_to_qdrant(atom, qdrant_url=args.qdrant_url)
            print(f"Written: {pid}")
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()


def query_atoms(
    query_text: str,
    *,
    qdrant_url: Optional[str] = None,
    embed_fn: Optional[EmbedFn] = None,
    author_filter: Optional[str] = None,
    thread_filter: Optional[str] = None,
    limit: int = 5,
    min_score: float = 0.0,
) -> List[dict]:
    """Query the reasoning_atoms collection by semantic proximity.

    Returns a list of dicts with keys:
        ``id``, ``score``, ``author``, ``thread``, ``content``,
        ``next_focus``, ``empirical``, ``ts``, ``schema_version``,
        ``provenance``, ``tension``, ``run``

    Parameters
    ----------
    query_text:
        The text to embed and search against.
    qdrant_url:
        Qdrant endpoint; falls back to ``QDRANT_URL`` env var or
        ``http://100.72.16.110:6333``.
    embed_fn:
        Callable ``(text: str) -> List[float]`` producing a 2048-dim vector.
        When omitted, a zero query vector is used (rank-by-payload not score).
    author_filter:
        If supplied, restrict results to atoms with this exact author string.
    thread_filter:
        If supplied, restrict results to atoms in this thread.
    limit:
        Maximum results to return before score filtering.
    min_score:
        Drop any result whose cosine similarity is below this threshold.
        Only meaningful when ``embed_fn`` is provided; zero-vector queries
        return meaningless scores and ``min_score`` is ignored.

    Requires: ``qdrant-client`` package.
    """
    try:
        from qdrant_client import QdrantClient  # type: ignore[import]
        from qdrant_client.models import Filter, FieldCondition, MatchValue  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "qdrant-client is required for query_atoms: pip install qdrant-client"
        ) from exc

    qdrant_url = qdrant_url or os.environ.get("QDRANT_URL", "http://100.72.16.110:6333")
    client = QdrantClient(url=qdrant_url)

    # Build optional filter
    conditions = []
    if author_filter:
        conditions.append(FieldCondition(key="author", match=MatchValue(value=author_filter)))
    if thread_filter:
        conditions.append(FieldCondition(key="thread", match=MatchValue(value=thread_filter)))
    qdrant_filter = Filter(must=conditions) if conditions else None

    # Build query vector
    if embed_fn is not None:
        query_vector = embed_fn(query_text)
    else:
        sys.stderr.write(
            "[reasoning_atoms] WARNING: no embed_fn supplied for query_atoms; "
            "scores will not reflect semantic similarity\n"
        )
        query_vector = [0.0] * EMBEDDING_DIMS

    results = client.query_points(
        collection_name=QDRANT_COLLECTION,
        query=query_vector,
        query_filter=qdrant_filter,
        limit=limit,
        with_payload=True,
        score_threshold=min_score if embed_fn is not None else None,
    )

    hits = []
    for hit in results.points:
        payload = hit.payload or {}
        hits.append(
            {
                "id": hit.id,
                "score": hit.score,
                "author": payload.get("author", ""),
                "thread": payload.get("thread", ""),
                "content": payload.get("content", ""),
                "next_focus": payload.get("next_focus", ""),
                "empirical": payload.get("empirical", False),
                "ts": payload.get("ts", ""),
                "schema_version": payload.get("schema_version", ""),
                "provenance": payload.get("provenance", []),
                "tension": payload.get("tension", []),
                "run": payload.get("run"),
            }
        )
    return hits
