"""
Tests for consequence_engine.py

Tests are organized around:
1. GitHub helpers (mocked network)
2. Node synthesis (_categorize_files, _infer_kind, synthesize_consequence_node)
3. Postgres watermark helpers (mocked connection)
4. ConsequenceEngine.run() integration (mocked DB + mocked GitHub)
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Minimal stub for psycopg (avoid hard dep in test env)
# ---------------------------------------------------------------------------


def _make_psycopg_stub():
    psycopg = types.ModuleType("psycopg")
    psycopg.connect = MagicMock()  # patched per test
    sys.modules.setdefault("psycopg", psycopg)
    return psycopg


_make_psycopg_stub()

from mac.consequence_engine import (  # noqa: E402
    ConsequenceEngine,
    _categorize_files,
    _infer_kind,
    _impact_summary,
    _extract_body_signal,
    _get_watermark,
    _upsert_watermark,
    _insert_node,
    synthesize_consequence_node,
    fetch_merged_prs_since,
    WATERMARK_NODE_ID,
    SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# _extract_body_signal
# ---------------------------------------------------------------------------


class TestExtractBodySignal:
    def test_empty(self):
        assert _extract_body_signal("") == ""

    def test_none_equivalent(self):
        assert _extract_body_signal(None) == ""  # type: ignore[arg-type]

    def test_skips_headings(self):
        body = "## Summary\nActual content here."
        assert "Summary" not in _extract_body_signal(body)
        assert "Actual content here" in _extract_body_signal(body)

    def test_skips_boilerplate_closes(self):
        body = "Fixes the bug.\nCloses #42"
        result = _extract_body_signal(body)
        assert "Fixes the bug" in result
        assert "#42" not in result

    def test_strips_bullet_markers(self):
        body = "- First point\n- Second point"
        result = _extract_body_signal(body)
        assert "First point" in result
        assert "- " not in result

    def test_joins_lines_with_pipe(self):
        body = "Line one.\nLine two."
        result = _extract_body_signal(body)
        assert " | " in result

    def test_truncates_at_400(self):
        body = "word " * 200  # ~1000 chars
        result = _extract_body_signal(body)
        assert len(result) <= 400

    def test_impact_summary_uses_context_prefix(self):
        result = _impact_summary("feat: add thing", [], "This adds the thing.")
        assert "Context:" in result
        assert "Author note:" not in result


# ---------------------------------------------------------------------------
# _categorize_files
# ---------------------------------------------------------------------------


class TestCategorizeFiles:
    def test_empty(self):
        assert _categorize_files([]) == {}

    def test_src_and_tests(self):
        files = [
            {"filename": "src/mac/foo.py"},
            {"filename": "tests/test_foo.py"},
            {"filename": "tests/test_bar.py"},
        ]
        cats = _categorize_files(files)
        assert cats["src"] == 1
        assert cats["tests"] == 2

    def test_docs_and_scripts(self):
        files = [
            {"filename": "docs/design.md"},
            {"filename": "README.md"},
            {"filename": "scripts/deploy.sh"},
        ]
        cats = _categorize_files(files)
        assert cats["docs"] == 2
        assert cats["scripts"] == 1

    def test_config_fallback(self):
        files = [{"filename": "pyproject.toml"}]
        cats = _categorize_files(files)
        assert cats["config"] == 1


# ---------------------------------------------------------------------------
# _infer_kind
# ---------------------------------------------------------------------------


class TestInferKind:
    def test_fix(self):
        assert _infer_kind("fix: null pointer in soul_mcp", []) == "fix"

    def test_docs(self):
        assert _infer_kind("docs: update AGENTS.md", []) == "docs"

    def test_refactor(self):
        assert _infer_kind("refactor soul graph traversal", []) == "refactor"

    def test_chore(self):
        assert _infer_kind("chore: bump deps", []) == "chore"

    def test_feature_default(self):
        assert _infer_kind("add consequence engine", []) == "feature"

    def test_test(self):
        assert _infer_kind("test: add coverage for soul_add", []) == "test"


# ---------------------------------------------------------------------------
# synthesize_consequence_node
# ---------------------------------------------------------------------------


class TestSynthesizeConsequenceNode:
    def _make_pr(
        self, number=42, title="Add foo", merged_at="2026-11-07T10:00:00Z", files=None, body=""
    ):
        return {
            "number": number,
            "title": title,
            "merged_at": merged_at,
            "html_url": f"https://github.com/jordanhubbard/mac/pull/{number}",
            "body": body,
            "_files": files or [],
        }

    def test_node_id_format(self):
        pr = self._make_pr(number=1030)
        node = synthesize_consequence_node(pr, "jordanhubbard", "mac", "bullwinkle")
        assert node["id"] == "pr_merge_jordanhubbard_mac_1030"

    def test_content_contains_title(self):
        pr = self._make_pr(number=1030, title="soul_link typed edges")
        node = synthesize_consequence_node(pr, "jordanhubbard", "mac", "bullwinkle")
        assert "soul_link typed edges" in node["content"]

    def test_tags(self):
        pr = self._make_pr()
        node = synthesize_consequence_node(pr, "jordanhubbard", "mac", "bullwinkle")
        assert "fleet" in node["tags"]
        assert "consequence" in node["tags"]
        assert "pr_merge" in node["tags"]

    def test_metadata_visibility(self):
        pr = self._make_pr()
        node = synthesize_consequence_node(pr, "jordanhubbard", "mac", "bullwinkle")
        assert node["metadata"]["visibility"] == "fleet"
        assert node["metadata"]["consequence"] is True

    def test_agent_field(self):
        pr = self._make_pr()
        node = synthesize_consequence_node(pr, "jordanhubbard", "mac", "natasha")
        assert node["agent"] == "natasha"

    def test_schema_version_in_content(self):
        pr = self._make_pr()
        node = synthesize_consequence_node(pr, "jordanhubbard", "mac", "bullwinkle")
        assert SCHEMA_VERSION in node["content"]


# ---------------------------------------------------------------------------
# Watermark helpers (mocked DB)
# ---------------------------------------------------------------------------


def _make_conn(fetchone_result=None, rowcount=1):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetchone_result
    cur.rowcount = rowcount
    conn.execute.return_value = cur
    return conn


class TestGetWatermark:
    def test_no_row(self):
        conn = _make_conn(fetchone_result=None)
        assert _get_watermark(conn, "bullwinkle") == 0

    def test_row_with_number(self):
        conn = _make_conn(fetchone_result=("1025",))
        assert _get_watermark(conn, "bullwinkle") == 1025

    def test_row_with_bad_value(self):
        conn = _make_conn(fetchone_result=("bad",))
        assert _get_watermark(conn, "bullwinkle") == 0


class TestUpsertWatermark:
    def test_update_path(self):
        conn = _make_conn(rowcount=1)
        _upsert_watermark(conn, "bullwinkle", 1030)
        # First call is UPDATE
        first_call_sql = conn.execute.call_args_list[0][0][0]
        assert "UPDATE soul_nodes" in first_call_sql

    def test_insert_path_when_no_row(self):
        conn = _make_conn(rowcount=0)
        _upsert_watermark(conn, "bullwinkle", 1030)
        calls = conn.execute.call_args_list
        assert len(calls) == 2  # UPDATE then INSERT
        assert "INSERT INTO soul_nodes" in calls[1][0][0]


class TestInsertNode:
    def test_inserts_when_new(self):
        conn = _make_conn(fetchone_result=None)
        node = {
            "id": "pr_merge_test_1",
            "content": "test content",
            "tags": ["fleet"],
            "parents": [],
            "pinned": False,
            "score": 1.0,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "last_accessed": "2026-01-01T00:00:00Z",
            "access_count": 0,
            "metadata": {},
            "agent": "bullwinkle",
        }
        result = _insert_node(conn, node)
        assert result is True

    def test_skips_existing(self):
        conn = _make_conn(fetchone_result=(1,))  # row exists
        node = {"id": "pr_merge_test_existing"}
        result = _insert_node(conn, node)
        assert result is False
        # Only the SELECT was called, no INSERT
        assert conn.execute.call_count == 1


# ---------------------------------------------------------------------------
# ConsequenceEngine.run() — integration (all I/O mocked)
# ---------------------------------------------------------------------------


class TestConsequenceEngineRun:
    def _engine(self, dry_run=False):
        return ConsequenceEngine(
            db_url="postgresql://fleet_agent@localhost:5432/agent_brain",
            agent="bullwinkle",
            repo="jordanhubbard/mac",
            dry_run=dry_run,
            max_prs=10,
        )

    def _mock_pr(self, number, title="Add feature"):
        return {
            "number": number,
            "title": title,
            "merged_at": "2026-11-07T10:00:00Z",
            "html_url": f"https://github.com/jordanhubbard/mac/pull/{number}",
            "body": "",
            "_files": [{"filename": "src/mac/foo.py"}],
        }

    def test_no_new_prs(self):
        engine = self._engine()
        mock_conn = MagicMock()
        # Watermark returns 1030, search returns nothing newer
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("1030",)
        mock_conn.execute.return_value = mock_cur

        with (
            patch.object(engine, "_connect", return_value=mock_conn),
            patch("mac.consequence_engine._gh_token", return_value="fake_token"),
            patch("mac.consequence_engine.fetch_merged_prs_since", return_value=[]),
        ):
            result = engine.run()

        assert result["written"] == 0
        assert result["watermark_before"] == 1030
        assert result["watermark_after"] == 1030

    def test_writes_new_pr(self):
        engine = self._engine()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [
            ("1025",),  # watermark read
            None,  # check if node exists (no) → insert
        ]
        mock_cur.rowcount = 1  # watermark UPDATE succeeds
        mock_conn.execute.return_value = mock_cur

        pr = self._mock_pr(1030, "typed edges")

        with (
            patch.object(engine, "_connect", return_value=mock_conn),
            patch("mac.consequence_engine._gh_token", return_value="fake_token"),
            patch("mac.consequence_engine.fetch_merged_prs_since", return_value=[pr]),
            patch("mac.consequence_engine.fetch_pr_detail", return_value=pr),
        ):
            result = engine.run()

        assert result["written"] == 1
        assert result["watermark_after"] == 1030

    def test_dry_run_does_not_write(self):
        engine = self._engine(dry_run=True)
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("1025",)
        mock_conn.execute.return_value = mock_cur

        pr = self._mock_pr(1030)

        with (
            patch.object(engine, "_connect", return_value=mock_conn),
            patch("mac.consequence_engine._gh_token", return_value="fake_token"),
            patch("mac.consequence_engine.fetch_merged_prs_since", return_value=[pr]),
            patch("mac.consequence_engine.fetch_pr_detail", return_value=pr),
        ):
            result = engine.run()

        assert result["written"] == 1
        assert result["nodes"][0]["dry_run"] is True
        # In dry_run, watermark_after reflects the computed highest number seen
        # but nothing was persisted. The engine still tracks the high-water mark
        # for the summary (watermark_after > watermark_before when new PRs processed).
        assert result["watermark_after"] == 1030
        assert result["watermark_before"] == 1025

    def test_no_github_token_returns_error(self):
        engine = self._engine()
        mock_conn = MagicMock()

        with (
            patch.object(engine, "_connect", return_value=mock_conn),
            patch("mac.consequence_engine._gh_token", return_value=""),
        ):
            result = engine.run()

        assert "error" in result
        assert result["written"] == 0

    def test_invalid_repo_format(self):
        with pytest.raises(ValueError, match="owner/repo"):
            ConsequenceEngine(
                db_url="postgresql://localhost/agent_brain",
                agent="bullwinkle",
                repo="bad-repo-no-slash",
            )
