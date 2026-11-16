"""Unit tests for mac.semantic_world_search.

All database and HTTP calls are mocked — these tests run without
a Postgres connection or a live hub endpoint.
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to build a fake psycopg2 environment
# ---------------------------------------------------------------------------


def _make_psycopg2_mock(rows: list[dict]) -> tuple[MagicMock, MagicMock]:
    """Return a (psycopg2_mod, conn_mock) pair that returns *rows* from fetchall."""
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows

    conn = MagicMock()
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    extras_mod = types.SimpleNamespace(RealDictCursor=dict)

    psycopg2_mod = MagicMock()
    psycopg2_mod.connect.return_value = conn
    psycopg2_mod.extras = extras_mod

    return psycopg2_mod, conn


def _sws():
    """Return a freshly-imported (or already imported) semantic_world_search module."""
    from mac import semantic_world_search  # noqa: PLC0415

    return semantic_world_search


# ---------------------------------------------------------------------------
# SearchResult
# ---------------------------------------------------------------------------


class TestSearchResult:
    def test_short_includes_rank_and_id(self):
        from mac.semantic_world_search import SearchResult

        r = SearchResult(
            id="abc123",
            agent="bullwinkle",
            content="Hello world",
            tags=["fleet"],
            score=0.9,
            method="fts",
            rank=1,
        )
        s = r.short()
        assert "[1]" in s
        assert "abc123" in s
        assert "bullwinkle" in s
        assert "fleet" in s

    def test_short_truncates_long_content(self):
        from mac.semantic_world_search import SearchResult

        long_content = "x" * 200
        r = SearchResult(
            id="x",
            agent="a",
            content=long_content,
            tags=[],
            score=1.0,
            method="fts",
            rank=1,
        )
        assert len(r.short()) < 300  # much shorter than the content

    def test_method_field_recorded(self):
        from mac.semantic_world_search import SearchResult

        r = SearchResult(id="n", agent="a", content="c", tags=[], score=0.5, method="hybrid")
        assert r.method == "hybrid"


# ---------------------------------------------------------------------------
# fts_search
# ---------------------------------------------------------------------------


class TestFtsSearch:
    def test_returns_search_results(self):
        rows = [
            {
                "id": "node1",
                "agent": "bullwinkle",
                "content": "soul graph node",
                "tags": ["fleet"],
                "rank_score": 0.8,
            },
            {
                "id": "node2",
                "agent": "rocky",
                "content": "consequence engine",
                "tags": ["tool"],
                "rank_score": 0.4,
            },
        ]
        psycopg2_mock, _ = _make_psycopg2_mock(rows)

        with patch.dict(
            sys.modules, {"psycopg2": psycopg2_mock, "psycopg2.extras": psycopg2_mock.extras}
        ):
            sws = _sws()
            # Patch the module-level psycopg2 reference
            with (
                patch.object(sws, "psycopg2", psycopg2_mock),
                patch.object(sws, "_HAS_PSYCOPG2", True),
            ):
                results = sws.fts_search("soul graph", db_url="postgresql://test/db")

        assert len(results) == 2
        assert results[0].id == "node1"
        assert results[0].method == "fts"
        assert results[0].rank == 1
        assert results[1].rank == 2
        assert results[0].score == pytest.approx(0.8)

    def test_empty_tags_normalized(self):
        rows = [{"id": "n", "agent": "a", "content": "c", "tags": None, "rank_score": 0.1}]
        psycopg2_mock, _ = _make_psycopg2_mock(rows)

        with (
            patch.object(_sws(), "psycopg2", psycopg2_mock),
            patch.object(_sws(), "_HAS_PSYCOPG2", True),
        ):
            results = _sws().fts_search("anything", db_url="postgresql://test/db")

        assert results[0].tags == []

    def test_raises_when_psycopg2_unavailable(self):
        sws = _sws()
        with patch.object(sws, "_HAS_PSYCOPG2", False):
            with pytest.raises(RuntimeError, match="psycopg2 not available"):
                sws.fts_search("query", db_url="postgresql://test/db")

    def test_agent_filter_applied(self):
        """When agent= is passed, the query should include the agent."""
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        conn = MagicMock()
        conn.cursor.return_value = cur
        psycopg2_mock = MagicMock()
        psycopg2_mock.connect.return_value = conn
        psycopg2_mock.extras = types.SimpleNamespace(RealDictCursor=dict)

        sws = _sws()
        with patch.object(sws, "psycopg2", psycopg2_mock), patch.object(sws, "_HAS_PSYCOPG2", True):
            sws.fts_search("query", db_url="postgresql://test/db", agent="bullwinkle")

        # Should have been called with agent in params
        call_args = cur.execute.call_args
        assert "bullwinkle" in call_args[0][1]


# ---------------------------------------------------------------------------
# _embed
# ---------------------------------------------------------------------------


class TestEmbed:
    def test_returns_vector_on_success(self):
        fake_vec = [0.1] * 1536
        fake_response = json.dumps({"data": [{"embedding": fake_vec}]}).encode()

        import urllib.request

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_response

        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            vec = _sws()._embed("test query", "http://hub:8789", "tok_abc")

        assert len(vec) == 1536
        assert vec[0] == pytest.approx(0.1)

    def test_raises_on_http_error(self):
        import urllib.error
        import urllib.request

        with patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.HTTPError(
                "http://hub/v1/embeddings", 403, "Forbidden", {}, None
            ),
        ):
            with pytest.raises(RuntimeError, match="403"):
                _sws()._embed("query", "http://hub:8789", "bad_tok")


# ---------------------------------------------------------------------------
# hybrid_search — fallback path
# ---------------------------------------------------------------------------


class TestHybridSearchFallback:
    def test_falls_back_to_fts_when_semantic_raises(self):
        """hybrid_search should return FTS results when semantic_search raises."""
        fts_rows = [
            {
                "id": "n1",
                "agent": "bw",
                "content": "fleet soul graph",
                "tags": ["fleet"],
                "rank_score": 0.9,
            },
        ]
        psycopg2_mock, _ = _make_psycopg2_mock(fts_rows)

        sws = _sws()
        with (
            patch.object(sws, "psycopg2", psycopg2_mock),
            patch.object(sws, "_HAS_PSYCOPG2", True),
            patch.object(sws, "semantic_search", side_effect=RuntimeError("No token")),
        ):
            results = sws.hybrid_search("soul graph", db_url="postgresql://test/db", limit=5)

        assert len(results) >= 1
        assert all(r.method == "fts" for r in results)

    def test_hybrid_merges_fts_and_semantic(self):
        """When both paths succeed, hybrid_search should merge and re-rank."""
        from mac.semantic_world_search import SearchResult

        fts_result = SearchResult(
            id="n1", agent="bw", content="soul", tags=[], score=0.8, method="fts", rank=1
        )
        sem_result = SearchResult(
            id="n2", agent="bw", content="graph", tags=[], score=0.9, method="semantic", rank=1
        )
        overlap_sem = SearchResult(
            id="n1", agent="bw", content="soul", tags=[], score=0.7, method="semantic", rank=2
        )

        sws = _sws()
        with (
            patch.object(sws, "fts_search", return_value=[fts_result]),
            patch.object(sws, "semantic_search", return_value=[sem_result, overlap_sem]),
        ):
            results = sws.hybrid_search("soul graph", db_url="postgresql://test/db", limit=5)

        ids = [r.id for r in results]
        # Both n1 and n2 should appear
        assert "n1" in ids
        assert "n2" in ids
        # All should be tagged hybrid
        assert all(r.method == "hybrid" for r in results)
        # Ranks should be 1-indexed sequential
        assert [r.rank for r in results] == list(range(1, len(results) + 1))

    def test_reciprocal_rank_fusion_prefers_overlap(self):
        """A node that appears in both FTS and semantic results should rank higher."""
        from mac.semantic_world_search import SearchResult

        overlap_id = "shared"
        fts_results = [
            SearchResult(
                id=overlap_id,
                agent="bw",
                content="overlap",
                tags=[],
                score=0.5,
                method="fts",
                rank=1,
            ),
            SearchResult(
                id="fts_only",
                agent="bw",
                content="fts only",
                tags=[],
                score=0.4,
                method="fts",
                rank=2,
            ),
        ]
        sem_results = [
            SearchResult(
                id=overlap_id,
                agent="bw",
                content="overlap",
                tags=[],
                score=0.6,
                method="semantic",
                rank=1,
            ),
            SearchResult(
                id="sem_only",
                agent="bw",
                content="sem only",
                tags=[],
                score=0.5,
                method="semantic",
                rank=2,
            ),
        ]

        sws = _sws()
        with (
            patch.object(sws, "fts_search", return_value=fts_results),
            patch.object(sws, "semantic_search", return_value=sem_results),
        ):
            results = sws.hybrid_search("test", db_url="postgresql://test/db", limit=5)

        # Overlap should be rank 1
        assert results[0].id == overlap_id


# ---------------------------------------------------------------------------
# Constants / module structure
# ---------------------------------------------------------------------------


class TestModuleStructure:
    def test_default_constants_set(self):
        sws = _sws()
        assert sws.DEFAULT_DB_URL.startswith("postgresql://")
        assert sws.DEFAULT_HUB_URL.startswith("http://")
        assert sws.EMBEDDING_MODEL == "azure/openai/text-embedding-3-small"
        assert sws.EMBEDDING_DIMS == 1536

    def test_all_public_functions_present(self):
        sws = _sws()
        for fn in ("fts_search", "semantic_search", "hybrid_search"):
            assert callable(getattr(sws, fn)), f"{fn} not callable"

    def test_search_result_dataclass_fields(self):
        from mac.semantic_world_search import SearchResult
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(SearchResult)}
        assert {"id", "agent", "content", "tags", "score", "method", "rank", "extra"} == field_names
