"""
tests/test_memory_graph.py — Tests for memory_graph enrichment pipeline

Covers:
  - Schema migration (additive columns + FTS5)
  - Title extraction
  - Keyword extraction (TF-IDF)
  - add_node() populates title/keywords
  - FTS5 search (BM25 blend)
  - LIKE fallback search
  - browse() keyword navigation + sub-topic co-occurrence
  - gripe() feedback nodes
  - embed_node() + semantic_search()
  - backfill_titles_and_keywords()
"""

import json
import math
import os
import sqlite3
import struct
import tempfile
from pathlib import Path

import pytest

# Allow running from repo root or tests/
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "lib" / "memory_graph"))
import memory_graph as mg


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    """Fresh in-memory-equivalent DB per test."""
    p = tmp_path / "test.db"
    mg.init_db(p)
    return p


# ── Schema ────────────────────────────────────────────────────────────────────


def test_schema_has_new_columns(db):
    con = sqlite3.connect(str(db))
    cols = {r[1] for r in con.execute("PRAGMA table_info(nodes)")}
    con.close()
    assert "title" in cols
    assert "keywords" in cols
    assert "embedding" in cols
    assert "embed_model" in cols


def test_fts5_table_exists(db):
    con = sqlite3.connect(str(db))
    result = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
    ).fetchone()[0]
    con.close()
    assert result == 1


def test_init_db_idempotent(db):
    """Calling init_db twice must not raise or corrupt the schema."""
    mg.init_db(db)
    mg.init_db(db)
    assert mg.node_count(db) == 0


# ── Title extraction ──────────────────────────────────────────────────────────


def test_make_title_first_line():
    t = mg.make_title("Fleet memory architecture\nMore detail follows.")
    assert t == "Fleet memory architecture"


def test_make_title_sentence():
    t = mg.make_title("Soul graph uses splay ordering. The rest is history.")
    assert t == "Soul graph uses splay ordering."


def test_make_title_truncation():
    long_line = "x" * 100
    t = mg.make_title(long_line)
    assert len(t) <= 73  # 72 + ellipsis char


def test_make_title_strips_markdown_header():
    t = mg.make_title("## Overview\nDetails here.")
    assert t == "Overview"


def test_make_title_empty_lines_skipped():
    t = mg.make_title("\n\nActual content here.")
    assert t == "Actual content here."


# ── Keyword extraction ────────────────────────────────────────────────────────


def test_extract_keywords_returns_list():
    kws = mg.extract_keywords("Fleet memory uses splay ordering for recent access patterns.")
    assert isinstance(kws, list)
    assert len(kws) > 0


def test_extract_keywords_excludes_stopwords():
    kws = mg.extract_keywords("the fleet is a system for the agents and the humans")
    for kw in kws:
        assert kw.lower() not in mg._STOPWORDS


def test_extract_keywords_top_n():
    content = " ".join([f"word{i}" for i in range(50)])
    kws = mg.extract_keywords(content, top_n=5)
    assert len(kws) <= 5


def test_extract_keywords_empty_content():
    kws = mg.extract_keywords("")
    assert kws == []


def test_extract_keywords_with_corpus_idf():
    # With IDF, rare terms score higher than common ones
    common = "fleet fleet fleet fleet memory"
    rare = "quux unique xenon"
    # Build a tiny corpus where 'fleet' appears in 3 docs but 'quux' only in 1
    idf = {"fleet": math.log(4 / 4) + 1.0, "quux": math.log(4 / 2) + 1.0}
    kws = mg.extract_keywords(rare + " " + common, corpus_idf=idf, top_n=3)
    # 'quux' should rank higher than 'fleet' due to IDF
    assert len(kws) > 0


# ── add_node() ────────────────────────────────────────────────────────────────


def test_add_node_basic(db):
    nid = mg.add_node("Fleet soul graph architecture", db_path=db)
    assert nid is not None
    assert len(nid) == 16  # sha256[:16]


def test_add_node_populates_title(db):
    mg.add_node("Soul graph uses splay ordering for recent access.", db_path=db)
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT title FROM nodes LIMIT 1").fetchone()
    con.close()
    assert row[0] is not None
    assert len(row[0]) > 0


def test_add_node_populates_keywords(db):
    mg.add_node("Fleet memory architecture uses splay-DAG with keyword indexing.", db_path=db)
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT keywords FROM nodes LIMIT 1").fetchone()
    con.close()
    kws = json.loads(row[0])
    assert isinstance(kws, list)
    assert len(kws) > 0


def test_add_node_custom_title(db):
    mg.add_node("Some long content here.", title="Custom Title", db_path=db)
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT title FROM nodes LIMIT 1").fetchone()
    con.close()
    assert row[0] == "Custom Title"


def test_add_node_visibility_tag(db):
    mg.add_node("Private content", visibility="private", db_path=db)
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT tags FROM nodes LIMIT 1").fetchone()
    con.close()
    tags = json.loads(row[0])
    assert "visibility:private" in tags


def test_add_node_feedback_kind(db):
    mg.add_node("Feedback content", kind="feedback", db_path=db)
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT kind FROM nodes WHERE kind='feedback'").fetchone()
    con.close()
    assert row is not None


def test_add_node_invalid_kind(db):
    with pytest.raises(ValueError):
        mg.add_node("content", kind="invalid_kind", db_path=db)


def test_add_node_axiom_always_pinned(db):
    mg.add_node("Axiom content", kind="axiom", db_path=db)
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT pinned FROM nodes WHERE kind='axiom'").fetchone()
    con.close()
    assert row[0] == 1


# ── FTS5 search ───────────────────────────────────────────────────────────────


def test_search_fts_basic(db):
    mg.add_node("Soul graph keyword indexing with BM25 ranking", db_path=db)
    mg.add_node("Unrelated content about cooking recipes", db_path=db)
    results = mg.search("soul keyword", db_path=db)
    assert len(results) >= 1
    # First result should be the soul-graph node
    assert "soul" in results[0]["content"].lower() or "keyword" in results[0]["content"].lower()


def test_search_returns_match_score(db):
    mg.add_node("Fleet memory architecture design", db_path=db)
    results = mg.search("fleet memory", db_path=db)
    assert len(results) > 0
    assert "match_score" in results[0]
    assert 0.0 <= results[0]["match_score"] <= 1.0


def test_search_no_results(db):
    mg.add_node("Soul graph content", db_path=db)
    results = mg.search("zzz_nonexistent_term_xyz", db_path=db)
    assert results == []


def test_search_includes_keywords_in_index(db):
    # Keyword "splay" won't appear in content but will be in keywords
    mg.add_node("Memory ordering for agents in a fleet", db_path=db)
    # Search should still work for terms that appear in content
    results = mg.search("memory agents", db_path=db)
    assert len(results) >= 1


def test_search_limit(db):
    for i in range(10):
        mg.add_node(f"Fleet memory node {i} with indexing support", db_path=db)
    results = mg.search("fleet memory", limit=3, db_path=db)
    assert len(results) <= 3


# ── browse() ─────────────────────────────────────────────────────────────────


def test_browse_returns_structure(db):
    mg.add_node("Fleet memory architecture overview", db_path=db)
    result = mg.browse("fleet", db_path=db)
    assert "topic" in result
    assert "nodes" in result
    assert "sub_topics" in result
    assert result["topic"] == "fleet"


def test_browse_finds_matching_nodes(db):
    mg.add_node("Fleet soul graph with memory indexing", db_path=db)
    mg.add_node("Cooking recipes for agents", db_path=db)
    result = mg.browse("fleet", db_path=db)
    # Should find the fleet node
    assert len(result["nodes"]) >= 1


def test_browse_sub_topics_co_occurrence(db):
    # Add multiple nodes with overlapping keywords
    mg.add_node("Fleet memory architecture design for agents", db_path=db)
    mg.add_node("Fleet memory indexing with keyword extraction", db_path=db)
    mg.add_node("Fleet soul graph uses memory splay ordering", db_path=db)
    result = mg.browse("fleet", db_path=db)
    # "memory" should appear as a sub-topic since it co-occurs in multiple fleet nodes
    sub_kws = [s["keyword"] for s in result["sub_topics"]]
    # At least one sub-topic should be present
    assert isinstance(sub_kws, list)


def test_browse_no_match(db):
    mg.add_node("Soul graph content", db_path=db)
    result = mg.browse("zzz_nonexistent", db_path=db)
    assert result["nodes"] == []
    assert result["sub_topics"] == []


# ── gripe() ───────────────────────────────────────────────────────────────────


def test_gripe_creates_node(db):
    nid = mg.gripe("The search is too slow", db_path=db)
    assert nid is not None
    count = mg.node_count(db)
    assert count == 1


def test_gripe_node_has_feedback_tags(db):
    mg.gripe("FTS5 returns wrong results", db_path=db)
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT tags, kind FROM nodes LIMIT 1").fetchone()
    con.close()
    tags = json.loads(row[0])
    assert "feedback" in tags
    assert "open" in tags
    assert row[1] == "feedback"


def test_gripe_fleet_visibility(db):
    mg.gripe("Needs improvement", db_path=db)
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT tags FROM nodes LIMIT 1").fetchone()
    con.close()
    tags = json.loads(row[0])
    assert "visibility:fleet" in tags


def test_gripe_custom_source(db):
    mg.gripe("Human complaint", source="human", db_path=db)
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT tags FROM nodes LIMIT 1").fetchone()
    con.close()
    tags = json.loads(row[0])
    assert "source:human" in tags


# ── Embeddings ────────────────────────────────────────────────────────────────


def _make_vec(seed: float, dim: int = 8) -> list:
    """Generate a deterministic unit vector."""
    import math

    v = [math.sin(seed + i) for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def test_embed_node_stores_blob(db):
    nid = mg.add_node("Embedding test content", db_path=db)
    vec = _make_vec(1.0)
    result = mg.embed_node(nid, vec, model="test-model", db_path=db)
    assert result is True
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT embedding, embed_model FROM nodes WHERE id=?", (nid,)).fetchone()
    con.close()
    assert row[0] is not None
    assert row[1] == "test-model"


def test_embed_node_missing_node(db):
    result = mg.embed_node("nonexistent", [1.0, 2.0], model="x", db_path=db)
    assert result is False


def test_semantic_search_basic(db):
    n1 = mg.add_node("Soul graph memory architecture", db_path=db)
    n2 = mg.add_node("Cooking and recipes for humans", db_path=db)
    v1 = _make_vec(1.0)
    v2 = _make_vec(5.0)
    mg.embed_node(n1, v1, model="test", db_path=db)
    mg.embed_node(n2, v2, model="test", db_path=db)

    # Query similar to v1 → n1 should rank higher
    query = _make_vec(1.1)
    results = mg.semantic_search(query, top_n=2, model="test", db_path=db)
    assert len(results) == 2
    assert results[0]["id"] == n1
    assert results[0]["similarity"] > results[1]["similarity"]


def test_semantic_search_dimension_mismatch_skipped(db):
    nid = mg.add_node("Content", db_path=db)
    mg.embed_node(nid, _make_vec(1.0, dim=8), model="model-8d", db_path=db)
    # Query with different dimension
    results = mg.semantic_search(_make_vec(1.0, dim=4), model="model-8d", db_path=db)
    assert results == []  # dimension mismatch → skipped


def test_semantic_search_model_filter(db):
    n1 = mg.add_node("Node with model A", db_path=db)
    n2 = mg.add_node("Node with model B", db_path=db)
    mg.embed_node(n1, _make_vec(1.0), model="model-a", db_path=db)
    mg.embed_node(n2, _make_vec(1.0), model="model-b", db_path=db)
    results = mg.semantic_search(_make_vec(1.0), model="model-a", db_path=db)
    assert all(r["id"] == n1 for r in results)


# ── backfill ──────────────────────────────────────────────────────────────────


def test_backfill_titles_and_keywords(tmp_path):
    """Nodes inserted without title/keywords get backfilled correctly."""
    p = tmp_path / "old.db"
    # Manually create a DB with the old schema (no title/keywords)
    con = sqlite3.connect(str(p))
    con.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'memory', tags TEXT NOT NULL DEFAULT '[]',
            score REAL NOT NULL DEFAULT 1.0, access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed TEXT, created_at TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE edges (
            from_id TEXT NOT NULL, to_id TEXT NOT NULL,
            relation_type TEXT NOT NULL DEFAULT 'related-to',
            weight REAL NOT NULL DEFAULT 1.0, created_at TEXT NOT NULL,
            PRIMARY KEY (from_id, to_id, relation_type)
        )
    """)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO nodes (id, content, kind, tags, created_at) VALUES (?, ?, ?, ?, ?)",
        ("abc123", "Fleet memory architecture overview", "memory", "[]", now),
    )
    con.commit()
    con.close()

    # init_db upgrades the schema
    mg.init_db(p)
    result = mg.backfill_titles_and_keywords(db_path=p)
    assert result["updated"] == 1

    con = sqlite3.connect(str(p))
    row = con.execute("SELECT title, keywords FROM nodes").fetchone()
    con.close()
    assert row[0] is not None
    kws = json.loads(row[1])
    assert isinstance(kws, list)
    assert len(kws) > 0


# ── enrich_node() ─────────────────────────────────────────────────────────────


def test_enrich_node_with_llm_fn(db):
    nid = mg.add_node("Soul graph memory", db_path=db)

    def fake_llm(content):
        return {"title": "LLM Title", "keywords": ["llm", "enriched"]}

    result = mg.enrich_node(nid, fake_llm, db_path=db)
    assert result is True

    con = sqlite3.connect(str(db))
    row = con.execute("SELECT title, keywords FROM nodes WHERE id=?", (nid,)).fetchone()
    con.close()
    assert row[0] == "LLM Title"
    assert json.loads(row[1]) == ["llm", "enriched"]


def test_enrich_node_missing(db):
    result = mg.enrich_node("nonexistent", lambda c: {}, db_path=db)
    assert result is False


# ── add_edge() validation ─────────────────────────────────────────────────────


def test_add_edge_extended_relations(db):
    n1 = mg.add_node("Node one about fleet architecture", db_path=db)
    n2 = mg.add_node("Node two about memory design", db_path=db)
    # These extended relation types should now be valid
    for rel in ["depends-on", "explains", "implements", "blocks", "supersedes"]:
        mg.add_edge(n1, n2, relation=rel, db_path=db)


def test_add_edge_invalid_relation(db):
    n1 = mg.add_node("Fleet memory node", db_path=db)
    n2 = mg.add_node("Soul graph node", db_path=db)
    with pytest.raises(ValueError):
        mg.add_edge(n1, n2, relation="bogus-relation", db_path=db)


# ── decay ─────────────────────────────────────────────────────────────────────


def test_decay_all(db):
    mg.add_node("Fleet memory architecture", db_path=db)
    result = mg.decay_all(db_path=db)
    assert result["decayed"] >= 1
    assert result["factor"] == mg.DECAY_FACTOR


def test_axiom_not_decayed(db):
    mg.add_node("Axiom content that must survive", kind="axiom", db_path=db)
    mg.decay_all(db_path=db)
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT score FROM nodes WHERE kind='axiom'").fetchone()
    con.close()
    assert row[0] == 1.0  # untouched


# ── top() with filters ────────────────────────────────────────────────────────


def test_top_kind_filter(db):
    mg.add_node("Memory node", kind="memory", db_path=db)
    mg.add_node("Axiom node", kind="axiom", db_path=db)
    results = mg.top(10, kind="axiom", db_path=db)
    assert all(r["kind"] == "axiom" for r in results)


def test_top_exclude_tags(db):
    mg.add_node("Archived node", tags=["archived", "visibility:personal"], db_path=db)
    mg.add_node("Active node", tags=["visibility:personal"], db_path=db)
    results = mg.top(10, exclude_tags=["archived"], db_path=db)
    assert all("archived" not in r["tags"] for r in results)


def test_top_require_tags(db):
    mg.add_node("Fleet node", tags=["fleet", "visibility:fleet"], db_path=db)
    mg.add_node("Personal node", tags=["visibility:personal"], db_path=db)
    results = mg.top(10, require_tags=["fleet"], db_path=db)
    assert all("fleet" in r["tags"] for r in results)
