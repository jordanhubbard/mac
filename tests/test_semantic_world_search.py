"""Tests for semantic_world_search — no live DB or hub required."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ── stub out psycopg2 so the module loads in a venv without it ───────────────

def _make_psycopg2_stub():
    mod = types.ModuleType("psycopg2")
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = dict
    mod.extras = extras
    mod.connect = MagicMock()
    sys.modules.setdefault("psycopg2", mod)
    sys.modules.setdefault("psycopg2.extras", extras)
    return mod


_make_psycopg2_stub()

from mac.semantic_world_search import (  # noqa: E402
    SearchResult,
    fts_search,
    hybrid_search,
    semantic_search,
    _embed,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _mock_conn(rows):
    """Return a psycopg2-shaped mock connection that yields *rows* from fetchall."""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ── unit: SearchResult ────────────────────────────────────────────────────────


def test_search_result_short():
    r = SearchResult(
        id="test_node",
        agent="bullwinkle",
        content="A" * 200,
        tags=["fleet", "tool"],
        score=0.9,
        method="fts",
        rank=1,
    )
    short = r.short()
    assert "test_node" in short
    assert "bullwinkle" in short
    assert "fleet" in short
    # snippet capped at 120 chars
    assert len(short.splitlines()[1].strip()) <= 120


# ── unit: fts_search ─────────────────────────────────────────────────────────


def test_fts_search_returns_results():
    fake_rows = [
        {
            "id": "node_abc",
            "agent": "bullwinkle",
            "content": "consequence engine design",
            "tags": ["fleet", "tool"],
            "rank_score": 0.8,
        }
    ]
    import psycopg2

    with patch.object(psycopg2, "connect", return_value=_mock_conn(fake_rows)):
        results = fts_search("consequence engine", limit=5, db_url="postgresql://fake/db")

    assert len(results) == 1
    r = results[0]
    assert r.id == "node_abc"
    assert r.agent == "bullwinkle"
    assert r.score == pytest.approx(0.8)
    assert r.method == "fts"
    assert r.rank == 1


def test_fts_search_agent_filter():
    """Agent filter should be in the SQL — test it doesn't crash."""
    import psycopg2

    with patch.object(psycopg2, "connect", return_value=_mock_conn([])):
        results = fts_search(
            "anything", limit=3, agent="natasha", db_url="postgresql://fake/db"
        )
    assert results == []


# ── unit: _embed ──────────────────────────────────────────────────────────────


def test_embed_returns_vector():
    fake_vec = [0.1] * 1536
    fake_body = json.dumps({"data": [{"embedding": fake_vec}]}).encode()

    class _FakeResp:
        def read(self):
            return fake_body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    with patch("urllib.request.urlopen", return_value=_FakeResp()):
        vec = _embed("test query", hub_url="http://hub", token="tok")

    assert len(vec) == 1536
    assert vec[0] == pytest.approx(0.1)


def test_embed_raises_on_http_error():
    import urllib.error

    err = urllib.error.HTTPError(
        url="http://hub/v1/embeddings",
        code=403,
        msg="Forbidden",
        hdrs=None,  # type: ignore[arg-type]
        fp=MagicMock(read=lambda: b"denied"),
    )
    err.read = lambda: b"denied"
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="403"):
            _embed("test", hub_url="http://hub", token="bad")


# ── unit: semantic_search ─────────────────────────────────────────────────────


def test_semantic_search_returns_results():
    fake_rows = [
        {
            "id": "sem_node",
            "agent": "rocky",
            "content": "fleet world semantic design",
            "tags": ["fleet"],
            "similarity": 0.95,
        }
    ]
    fake_vec = [0.0] * 1536

    import psycopg2

    with (
        patch("mac.semantic_world_search._embed", return_value=fake_vec),
        patch.object(psycopg2, "connect", return_value=_mock_conn(fake_rows)),
    ):
        results = semantic_search(
            "fleet world",
            limit=5,
            db_url="postgresql://fake/db",
            hub_url="http://hub",
            token="tok",
        )

    assert len(results) == 1
    assert results[0].id == "sem_node"
    assert results[0].method == "semantic"
    assert results[0].score == pytest.approx(0.95)


# ── unit: hybrid_search ───────────────────────────────────────────────────────


def test_hybrid_merges_and_ranks():
    fts_rows = [
        {"id": "n1", "agent": "bullwinkle", "content": "alpha", "tags": [], "rank_score": 1.0},
        {"id": "n2", "agent": "bullwinkle", "content": "beta", "tags": [], "rank_score": 0.5},
    ]
    sem_rows = [
        {"id": "n2", "agent": "bullwinkle", "content": "beta", "tags": [], "similarity": 0.9},
        {"id": "n3", "agent": "natasha", "content": "gamma", "tags": [], "similarity": 0.8},
    ]

    import psycopg2

    call_count = {"n": 0}

    def _connect_side_effect(dsn):
        call_count["n"] += 1
        return _mock_conn(fts_rows if call_count["n"] == 1 else sem_rows)

    with (
        patch("mac.semantic_world_search._embed", return_value=[0.0] * 1536),
        patch.object(psycopg2, "connect", side_effect=_connect_side_effect),
    ):
        results = hybrid_search(
            "query",
            limit=5,
            db_url="postgresql://fake/db",
            hub_url="http://hub",
            token="tok",
        )

    ids = [r.id for r in results]
    # n2 appears in both → should rank highest via RRF
    assert ids[0] == "n2"
    assert set(ids) == {"n1", "n2", "n3"}
    for i, r in enumerate(results):
        assert r.rank == i + 1
        assert r.method == "hybrid"


def test_hybrid_falls_back_to_fts_on_embed_error():
    fts_rows = [
        {"id": "n1", "agent": "bullwinkle", "content": "alpha", "tags": [], "rank_score": 1.0},
    ]

    import psycopg2

    with (
        patch("mac.semantic_world_search._embed", side_effect=RuntimeError("no token")),
        patch.object(psycopg2, "connect", return_value=_mock_conn(fts_rows)),
    ):
        results = hybrid_search(
            "query",
            limit=5,
            db_url="postgresql://fake/db",
            hub_url="http://hub",
            token="",
        )

    assert len(results) == 1
    assert results[0].method == "fts"
