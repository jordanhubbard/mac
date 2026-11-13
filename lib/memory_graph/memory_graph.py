"""
memory_graph.py — SQLite-backed graph memory for fleet agents
Stdlib only: sqlite3, json, datetime, hashlib, re, math, os, pathlib, struct, collections

Layers:
  1. Schema: nodes + edges + FTS5 virtual table (title/keywords/content, BM25)
  2. Title extraction: first non-empty line, truncated
  3. Keyword extraction: TF-IDF scored against corpus, normalized
  4. Upgraded search(): BM25 blended with splay score, falls back to LIKE
  5. Embedding storage + cosine semantic_search() (pure Python, model-stamped cache)
  6. browse(topic): keyword-grouped navigation + co-occurrence sub-topics
  7. gripe(text): one-call feedback node
"""

import sqlite3
import json
import hashlib
import re
import math
import os
import struct
import collections
from datetime import datetime, timezone
from pathlib import Path

# ── Default DB path ────────────────────────────────────────────────────────────
_DEFAULT_DB = Path(__file__).parent / "memory_graph.db"

VALID_RELATIONS = {
    "related-to",
    "cites",
    "refines",
    "corroborates",
    "derived-from",
    "contradicts",
    # Extended to match soul-graph edge vocabulary
    "depends-on",
    "explains",
    "instance-of",
    "learned-from",
    "applies-to",
    "implements",
    "blocks",
    "supersedes",
}
# NOTE: the soul graph MCP layer uses a separate vocabulary (depends_on, explains, instance_of,
# learned_from, applies_to, implements) that does NOT overlap with the original VALID_RELATIONS.
# Those types are now included above (hyphenated form). Bridge code should use the hyphenated
# versions when calling memory_graph; the soul MCP uses underscore variants.
# A future release may unify the two vocabularies — see lib/memory_graph/README.md for the full gap note.
VALID_KINDS = {"memory", "axiom", "skill-ref", "task-ref", "feedback"}
VALID_VISIBILITY = {
    "private",
    "fleet",
    "personal",
}  # private=Rocky-only, fleet=shareable, personal=jkh-scoped
DECAY_FACTOR = 0.95  # multiplied per decay_all() call

# Stopwords excluded from keyword extraction and auto-link token overlap
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
    "have",
    "has",
    "had",
    "will",
    "would",
    "could",
    "should",
    "when",
    "then",
    "also",
    "than",
    "just",
    "been",
    "being",
    "their",
    "they",
    "them",
    "there",
    "here",
    "your",
    "our",
    "its",
    "can",
    "may",
    "must",
    "shall",
    "into",
    "onto",
    "about",
    "after",
    "before",
    "only",
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
    """Create tables and indexes if they don't exist. Safe to call on existing DBs."""
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

    # Additive columns — safe on existing DBs
    _add_column_if_missing(con, "nodes", "title", "TEXT")
    _add_column_if_missing(con, "nodes", "keywords", "TEXT")  # JSON array
    _add_column_if_missing(con, "nodes", "embedding", "BLOB")
    _add_column_if_missing(con, "nodes", "embed_model", "TEXT")

    # FTS5 virtual table (BM25 search over title + keywords + content)
    con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts
        USING fts5(
            title,
            keywords,
            content,
            content='nodes',
            content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2'
        )
    """)

    # Triggers to keep FTS5 in sync
    con.executescript("""
        CREATE TRIGGER IF NOT EXISTS nodes_fts_insert
        AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, title, keywords, content)
            VALUES (new.rowid,
                    COALESCE(new.title, ''),
                    COALESCE(new.keywords, ''),
                    new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS nodes_fts_delete
        AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, title, keywords, content)
            VALUES ('delete', old.rowid,
                    COALESCE(old.title, ''),
                    COALESCE(old.keywords, ''),
                    old.content);
        END;

        CREATE TRIGGER IF NOT EXISTS nodes_fts_update
        AFTER UPDATE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, title, keywords, content)
            VALUES ('delete', old.rowid,
                    COALESCE(old.title, ''),
                    COALESCE(old.keywords, ''),
                    old.content);
            INSERT INTO nodes_fts(rowid, title, keywords, content)
            VALUES (new.rowid,
                    COALESCE(new.title, ''),
                    COALESCE(new.keywords, ''),
                    new.content);
        END;
    """)

    con.commit()
    con.close()


def _add_column_if_missing(con, table, column, col_type):
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        con.commit()


# ── Title extraction ───────────────────────────────────────────────────────────


def make_title(content: str, max_len: int = 72) -> str:
    """
    Extract a short title from content.
    Rules (in order):
      1. First non-empty line that is not a markdown header marker
      2. First sentence (up to first '.', '!', or '?')
      3. First max_len characters
    """
    for line in content.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            # If it looks like a sentence, truncate at sentence boundary
            m = re.match(r"^(.+?[.!?])\s", line + " ")
            if m and len(m.group(1)) <= max_len:
                return m.group(1)
            return line[:max_len] + ("…" if len(line) > max_len else "")
    return content[:max_len]


# ── Keyword extraction (stdlib TF-IDF) ────────────────────────────────────────


def _tokenize(text: str) -> list:
    return [
        t.lower()
        for t in re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{2,}\b", text)
        if t.lower() not in _STOPWORDS
    ]


def _stem(token: str) -> str:
    """Very lightweight suffix stemming — no NLTK needed."""
    for suffix in (
        "ing",
        "tion",
        "tions",
        "ations",
        "ation",
        "ers",
        "ies",
        "ness",
        "ments",
        "ment",
        "ally",
        "ically",
        "edly",
        "edly",
    ):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: len(token) - len(suffix)]
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def extract_keywords(content: str, corpus_idf: "dict | None" = None, top_n: int = 8) -> list:
    """
    Extract top_n keywords from content using TF × IDF.

    corpus_idf: dict mapping stemmed token → idf score (pre-computed from corpus).
                If None, falls back to raw TF (useful for single-node extraction).

    Returns list of original (unstemmed) tokens, most informative first.
    """
    tokens = _tokenize(content)
    if not tokens:
        return []

    # TF: normalized term frequency
    stem_to_original: dict[str, str] = {}
    tf_counts: collections.Counter = collections.Counter()
    for tok in tokens:
        stem = _stem(tok)
        tf_counts[stem] += 1
        # Keep the most common original form
        if stem not in stem_to_original:
            stem_to_original[stem] = tok

    total = sum(tf_counts.values())
    scores = {}
    for stem, count in tf_counts.items():
        tf = count / total
        idf = corpus_idf.get(stem, 1.0) if corpus_idf else 1.0
        scores[stem] = tf * idf

    ranked = sorted(scores, key=lambda s: -scores[s])[:top_n]
    return [stem_to_original[s] for s in ranked]


def _corpus_idf(con) -> dict:
    """
    Compute IDF for all tokens in the nodes table.
    Returns dict: stemmed_token → idf_score
    """
    rows = con.execute("SELECT content FROM nodes").fetchall()
    n_docs = len(rows)
    if n_docs == 0:
        return {}

    doc_freq: collections.Counter = collections.Counter()
    for row in rows:
        stems = set(_stem(t) for t in _tokenize(row["content"]))
        doc_freq.update(stems)

    return {stem: math.log((n_docs + 1) / (freq + 1)) + 1.0 for stem, freq in doc_freq.items()}


# ── Node ID ───────────────────────────────────────────────────────────────────


def _node_id(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ── Core API ───────────────────────────────────────────────────────────────────


def add_node(
    content: str,
    kind: str = "memory",
    tags: "list | None" = None,
    pinned: bool = False,
    visibility: str = "personal",
    auto_link: bool = True,
    title: "str | None" = None,
    db_path=None,
) -> str:
    """
    Insert or update a node. Returns node_id.
    - Extracts title and keywords automatically.
    - Populates FTS5 index via trigger.
    - Auto-links to nodes with ≥2 overlapping tokens.
    """
    if tags is None:
        tags = []
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}")
    if visibility not in VALID_VISIBILITY:
        raise ValueError(f"visibility must be one of {VALID_VISIBILITY}")
    if kind == "axiom":
        pinned = True

    vis_tag = f"visibility:{visibility}"
    if vis_tag not in tags:
        tags = list(tags) + [vis_tag]

    node_id = _node_id(content)
    auto_title = title or make_title(content)

    con = _connect(db_path)
    try:
        # Compute IDF and extract keywords
        idf = _corpus_idf(con)
        kws = extract_keywords(content, corpus_idf=idf)
        kws_json = json.dumps(kws)
        kws_text = " ".join(kws)  # for FTS indexing

        con.execute(
            """
            INSERT INTO nodes (id, content, kind, tags, score, access_count, created_at,
                               pinned, title, keywords)
            VALUES (?, ?, ?, ?, 1.0, 0, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind     = excluded.kind,
                tags     = excluded.tags,
                pinned   = excluded.pinned,
                title    = excluded.title,
                keywords = excluded.keywords
        """,
            (
                node_id,
                content.strip(),
                kind,
                json.dumps(tags),
                _now_iso(),
                int(pinned),
                auto_title,
                kws_json,
            ),
        )
        con.commit()

        # Auto-link: find nodes sharing ≥2 significant tokens
        if auto_link:
            tokens = {_stem(t) for t in _tokenize(content)}
            if tokens:
                cur = con.execute("SELECT id, content FROM nodes WHERE id != ?", (node_id,))
                for row in cur.fetchall():
                    other_tokens = {_stem(t) for t in _tokenize(row["content"])}
                    if len(tokens & other_tokens) >= 2:
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
    """Add a directed edge between two existing nodes."""
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
    """Record an access: bump access_count, update last_accessed, logarithmic score boost."""
    con = _connect(db_path)
    try:
        cur = con.execute("SELECT score, access_count FROM nodes WHERE id=?", (node_id,))
        row = cur.fetchone()
        if row is None:
            return False
        new_count = row["access_count"] + 1
        boost = 0.1 * math.log1p(new_count)
        new_score = min(row["score"] + boost, 100.0)
        con.execute(
            """
            UPDATE nodes SET score=?, access_count=?, last_accessed=? WHERE id=?
        """,
            (new_score, new_count, _now_iso(), node_id),
        )
        con.commit()
        return True
    finally:
        con.close()


# ── Search: BM25 + splay score blend ──────────────────────────────────────────


def search(query: str, limit: int = 10, db_path=None) -> list:
    """
    Search nodes. Strategy (in order of quality):
      1. FTS5 BM25 search if the virtual table exists — blends rank with splay score.
      2. LIKE fallback (original behaviour) if FTS5 is unavailable.

    Blend formula: 0.6 * normalized_bm25 + 0.4 * normalized_score
    Returns list of dicts with all node fields plus a 'match_score' key.
    """
    con = _connect(db_path)
    try:
        # Check if FTS5 is available
        fts_exists = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
        ).fetchone()[0]

        if fts_exists:
            return _search_fts(con, query, limit)
        else:
            return _search_like(con, query, limit)
    finally:
        con.close()


def _search_fts(con, query: str, limit: int) -> list:
    """BM25 FTS5 search blended with splay score."""
    try:
        # FTS5 rank is negative BM25 (lower = better match)
        rows = con.execute(
            """
            SELECT n.*, f.rank AS bm25_rank
            FROM nodes_fts f
            JOIN nodes n ON n.rowid = f.rowid
            WHERE nodes_fts MATCH ?
            ORDER BY f.rank
            LIMIT ?
        """,
            (query, limit * 3),
        ).fetchall()  # oversample for blend re-ranking

        if not rows:
            return []

        # Normalize BM25 ranks (negate: higher is better) and splay scores
        bm25_vals = [-r["bm25_rank"] for r in rows]
        bm25_min, bm25_max = min(bm25_vals), max(bm25_vals)
        score_vals = [r["score"] for r in rows]
        score_min, score_max = min(score_vals), max(score_vals)

        def norm(v, lo, hi):
            return (v - lo) / (hi - lo) if hi > lo else 1.0

        results = []
        for r, bm25 in zip(rows, bm25_vals):
            nb = norm(bm25, bm25_min, bm25_max)
            ns = norm(r["score"], score_min, score_max)
            blend = 0.6 * nb + 0.4 * ns
            d = dict(r)
            d["tags"] = json.loads(d["tags"] or "[]")
            d["keywords"] = json.loads(d["keywords"] or "[]") if d.get("keywords") else []
            d["match_score"] = round(blend, 4)
            results.append(d)

        results.sort(key=lambda x: -x["match_score"])
        return results[:limit]

    except Exception:
        # FTS syntax error or other issue — fall back to LIKE
        return _search_like(con, query, limit)


def _search_like(con, query: str, limit: int) -> list:
    """Original LIKE-based search, retained as fallback."""
    tokens = re.findall(r"\w+", query.lower())
    if not tokens:
        return []
    conditions = " AND ".join(["LOWER(content) LIKE ?" for _ in tokens])
    params = [f"%{t}%" for t in tokens] + [limit]
    cur = con.execute(f"SELECT * FROM nodes WHERE {conditions} ORDER BY score DESC LIMIT ?", params)
    results = []
    for r in cur.fetchall():
        d = dict(r)
        d["tags"] = json.loads(d["tags"] or "[]")
        d["keywords"] = json.loads(d["keywords"] or "[]") if d.get("keywords") else []
        d["match_score"] = round(d["score"], 4)
        results.append(d)
    return results


# ── Embedding storage + semantic search ───────────────────────────────────────


def _vec_to_blob(vec: list) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> list:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def embed_node(node_id: str, vector: list, model: str, db_path=None) -> bool:
    """
    Store an embedding vector for a node.
    model: name of the embedding model (used as cache key).
    Returns True if node found and updated, False otherwise.
    """
    con = _connect(db_path)
    try:
        row = con.execute("SELECT id FROM nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            return False
        blob = _vec_to_blob(vector)
        con.execute(
            """
            UPDATE nodes SET embedding=?, embed_model=? WHERE id=?
        """,
            (blob, model, node_id),
        )
        con.commit()
        return True
    finally:
        con.close()


def semantic_search(query_vector: list, top_n: int = 10, model: str = None, db_path=None) -> list:
    """
    Pure-Python cosine similarity search over stored embeddings.

    query_vector: embedding of the query string (same model as stored embeddings).
    model: if set, only compares nodes embedded with this model.
           If None, uses all nodes that have an embedding.

    Returns list of {id, title, summary, score, similarity} dicts, sorted by similarity desc.
    """
    con = _connect(db_path)
    try:
        if model:
            rows = con.execute(
                "SELECT id, content, title, score, embedding FROM nodes "
                "WHERE embedding IS NOT NULL AND embed_model=?",
                (model,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id, content, title, score, embedding FROM nodes WHERE embedding IS NOT NULL"
            ).fetchall()

        results = []
        for r in rows:
            vec = _blob_to_vec(r["embedding"])
            if len(vec) != len(query_vector):
                continue  # dimension mismatch — skip stale blobs
            sim = _cosine(query_vector, vec)
            results.append(
                {
                    "id": r["id"],
                    "title": r["title"] or r["content"][:60],
                    "summary": r["content"][:100] + ("…" if len(r["content"]) > 100 else ""),
                    "score": round(r["score"], 4),
                    "similarity": round(sim, 4),
                }
            )

        results.sort(key=lambda x: -x["similarity"])
        return results[:top_n]
    finally:
        con.close()


def enrich_node(node_id: str, llm_fn, db_path=None) -> bool:
    """
    LLM enrichment hook. llm_fn(content: str) → {"title": str, "keywords": list[str]}
    Updates the node's title and keywords columns.
    Call this after embeddings are available or on-demand.
    Returns True if node was found and enriched.
    """
    con = _connect(db_path)
    try:
        row = con.execute("SELECT content FROM nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            return False
        result = llm_fn(row["content"])
        new_title = result.get("title", "")
        new_kws = json.dumps(result.get("keywords", []))
        con.execute(
            """
            UPDATE nodes SET title=?, keywords=? WHERE id=?
        """,
            (new_title, new_kws, node_id),
        )
        con.commit()
        return True
    finally:
        con.close()


# ── Browse: keyword-grouped navigation ────────────────────────────────────────


def browse(topic: str, top_n: int = 20, db_path=None) -> dict:
    """
    Keyword-based hierarchical navigation.

    Returns:
      {
        "topic":      str,
        "nodes":      [{ id, title, summary, score, keywords }, ...],   # sorted by score
        "sub_topics": [{"keyword": str, "count": int}, ...]             # co-occurring keywords
      }

    sub_topics are keywords that frequently appear alongside `topic` in
    matching nodes — the natural next level of the hierarchy.
    """
    con = _connect(db_path)
    try:
        # Find nodes where topic appears in keywords or content
        all_nodes = con.execute(
            "SELECT id, content, title, keywords, score FROM nodes ORDER BY score DESC"
        ).fetchall()

        topic_lower = topic.lower()
        matching = []
        for r in all_nodes:
            kws = json.loads(r["keywords"] or "[]")
            kws_lower = [k.lower() for k in kws]
            if topic_lower in kws_lower or topic_lower in r["content"].lower():
                matching.append(r)

        matching.sort(key=lambda r: -r["score"])
        nodes_out = []
        co_occur: collections.Counter = collections.Counter()

        for r in matching[:top_n]:
            kws = json.loads(r["keywords"] or "[]")
            nodes_out.append(
                {
                    "id": r["id"],
                    "title": r["title"] or r["content"][:60],
                    "summary": r["content"][:100] + ("…" if len(r["content"]) > 100 else ""),
                    "score": round(r["score"], 4),
                    "keywords": kws,
                }
            )
            # Count co-occurring keywords (excluding the topic itself)
            for k in kws:
                if k.lower() != topic_lower:
                    co_occur[k.lower()] += 1

        sub_topics = [
            {"keyword": kw, "count": cnt}
            for kw, cnt in co_occur.most_common(10)
            if cnt > 1  # only keywords shared by 2+ nodes
        ]

        return {
            "topic": topic,
            "nodes": nodes_out,
            "sub_topics": sub_topics,
        }
    finally:
        con.close()


# ── Gripe: low-friction feedback dump ─────────────────────────────────────────


def gripe(text: str, source: str = "agent", db_path=None) -> str:
    """
    One-call feedback node. Tags: feedback, open, source:<source>.
    Returns node_id.

    CLI: python3 memory_graph.py gripe "this is broken"
    MCP: soul_gripe(text)
    """
    tags = ["feedback", "open", f"source:{source}"]
    return add_node(
        content=text,
        kind="feedback",
        tags=tags,
        visibility="fleet",  # feedback is shared — agents + humans can see it
        db_path=db_path,
    )


# ── FTS5 backfill (run on existing DBs that predate this version) ──────────────


def rebuild_fts(db_path=None):
    """
    Backfill FTS5 index from existing nodes.
    Safe to run multiple times — clears and rebuilds.
    """
    con = _connect(db_path)
    try:
        # Rebuild the FTS index
        con.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
        con.commit()
        count = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        return {"rebuilt": count}
    finally:
        con.close()


def backfill_titles_and_keywords(db_path=None) -> dict:
    """
    Compute and store title + keywords for existing nodes that have NULL values.
    Returns {"updated": N}.

    Rebuilds the FTS5 index before updating so that the update triggers
    don't encounter a stale/empty FTS index (which causes "database disk image
    is malformed" when the DELETE phase of the update trigger can't find the
    old row).
    """
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, content FROM nodes WHERE title IS NULL OR keywords IS NULL"
        ).fetchall()
        if not rows:
            return {"updated": 0}

        # Sync FTS index with current node content BEFORE triggering any updates.
        fts_exists = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
        ).fetchone()[0]
        if fts_exists:
            con.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
            con.commit()

        idf = _corpus_idf(con)
        updated = 0
        for r in rows:
            t = make_title(r["content"])
            kws = json.dumps(extract_keywords(r["content"], corpus_idf=idf))
            con.execute("UPDATE nodes SET title=?, keywords=? WHERE id=?", (t, kws, r["id"]))
            updated += 1
        con.commit()
        return {"updated": updated}
    finally:
        con.close()


# ── Fisheye retrieval ─────────────────────────────────────────────────────────


def fisheye(node_id: str, hops: int = 1, db_path=None) -> dict:
    """
    Fisheye retrieval centred on node_id.
    Returns focal node (full), hop1 neighbours (summaries), hop2 edge-type counts.
    """
    con = _connect(db_path)
    try:
        cur = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,))
        focal_row = cur.fetchone()
        if focal_row is None:
            return {"error": f"Node {node_id!r} not found"}

        focal = dict(focal_row)
        focal["tags"] = json.loads(focal["tags"] or "[]")
        focal["keywords"] = json.loads(focal["keywords"] or "[]") if focal.get("keywords") else []

        cur = con.execute(
            """
            SELECT n.id, n.content, n.title, n.kind, n.score,
                   e.relation_type, e.weight, 'outbound' AS direction
            FROM edges e JOIN nodes n ON n.id = e.to_id
            WHERE e.from_id = ?
            UNION ALL
            SELECT n.id, n.content, n.title, n.kind, n.score,
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
                    "title": r["title"] or r["content"][:60],
                    "summary": r["content"][:80] + ("…" if len(r["content"]) > 80 else ""),
                    "kind": r["kind"],
                    "score": round(r["score"], 4),
                    "relation": r["relation_type"],
                    "direction": r["direction"],
                }
            )

        hop2_counts: dict = {}
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


# ── Top / decay ───────────────────────────────────────────────────────────────


def top(
    n: int = 10,
    kind: str = None,
    exclude_tags: list = None,
    require_tags: list = None,
    db_path=None,
) -> list:
    """Return top-n nodes by score. Optional kind/tag filters."""
    con = _connect(db_path)
    try:
        where = []
        params = []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        query = "SELECT id, content, title, keywords, kind, score, pinned, tags FROM nodes"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY score DESC"
        rows = con.execute(query, params).fetchall()
        results = []
        for r in rows:
            tags = json.loads(r["tags"] or "[]")
            if exclude_tags and any(t in tags for t in exclude_tags):
                continue
            if require_tags and not all(t in tags for t in require_tags):
                continue
            kws = json.loads(r["keywords"] or "[]") if r["keywords"] else []
            results.append(
                {
                    "id": r["id"],
                    "title": r["title"] or r["content"][:60],
                    "summary": r["content"][:100] + ("…" if len(r["content"]) > 100 else ""),
                    "kind": r["kind"],
                    "score": round(r["score"], 4),
                    "pinned": bool(r["pinned"]),
                    "tags": tags,
                    "keywords": kws,
                }
            )
            if len(results) >= n:
                break
        return results
    finally:
        con.close()


def decay_all(factor: float = DECAY_FACTOR, db_path=None) -> dict:
    """Apply exponential decay to all non-pinned nodes."""
    con = _connect(db_path)
    try:
        cur = con.execute(
            """
            UPDATE nodes SET score = MAX(0.001, score * ?)
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


# ── Stats helpers ─────────────────────────────────────────────────────────────


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


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    db_path = Path(__file__).parent / "memory_graph.db"
    init_db(db_path)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "top"

    if cmd == "top":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        for node in top(n, db_path=db_path):
            pin = "📌" if node["pinned"] else "  "
            kw = ", ".join(node["keywords"][:4])
            print(f"[{node['score']:.2f}] ({node['kind']}) {pin} {node['title']}")
            if kw:
                print(f"      kw: {kw}")

    elif cmd == "search":
        q = " ".join(sys.argv[2:])
        for node in search(q, db_path=db_path):
            print(f"[{node['match_score']:.3f}] {node['id']} — {node['title']}")
            print(f"         {node['summary'][:80]}")

    elif cmd == "browse":
        topic = " ".join(sys.argv[2:])
        result = browse(topic, db_path=db_path)
        print(f"Topic: {result['topic']} ({len(result['nodes'])} nodes)")
        for n in result["nodes"]:
            print(f"  [{n['score']:.2f}] {n['title']}")
        if result["sub_topics"]:
            print(
                "Sub-topics: "
                + ", ".join(f"{s['keyword']}({s['count']})" for s in result["sub_topics"])
            )

    elif cmd == "gripe":
        text = " ".join(sys.argv[2:])
        if not text:
            print("Usage: memory_graph.py gripe <complaint text>")
            sys.exit(1)
        nid = gripe(text, source="cli", db_path=db_path)
        print(f"Recorded feedback: {nid}")

    elif cmd == "backfill":
        result = backfill_titles_and_keywords(db_path=db_path)
        print(f"Backfilled {result['updated']} nodes")
        rebuild_fts(db_path=db_path)
        print("FTS5 index rebuilt")

    elif cmd == "stats":
        print(
            f"Nodes: {node_count(db_path)}  Pinned: {pinned_count(db_path)}  "
            f"Axioms: {axiom_count(db_path)}"
        )
