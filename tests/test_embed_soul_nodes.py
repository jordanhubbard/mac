"""Tests for scripts/embed_soul_nodes.py"""

from __future__ import annotations

import json
import sys
import types
from io import BytesIO
from unittest.mock import MagicMock, patch, call
import importlib
import os

import pytest


# ---------------------------------------------------------------------------
# Helpers to load the script as a module
# ---------------------------------------------------------------------------


def _load_module():
    """Import embed_soul_nodes as a module."""
    import importlib.util, pathlib

    path = pathlib.Path(__file__).parent.parent / "scripts" / "embed_soul_nodes.py"
    spec = importlib.util.spec_from_file_location("embed_soul_nodes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


# ---------------------------------------------------------------------------
# Tests for embed_texts()
# ---------------------------------------------------------------------------


class TestEmbedTexts:
    def test_returns_vectors_on_success(self):
        vec = [0.1] * 1536
        response_body = json.dumps(
            {"data": [{"index": 0, "embedding": vec}, {"index": 1, "embedding": vec}]}
        ).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = mod.embed_texts(["hello", "world"], token="tok", hub_url="http://hub")

        assert len(result) == 2
        assert result[0] == vec
        assert result[1] == vec

    def test_returns_none_on_http_error(self):
        import urllib.error

        err = urllib.error.HTTPError(
            url="u", code=403, msg="Forbidden", hdrs=None, fp=BytesIO(b"denied")
        )
        with patch("urllib.request.urlopen", side_effect=err):
            result = mod.embed_texts(["text"], token="bad", hub_url="http://hub")
        assert result == [None]

    def test_returns_none_on_connection_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = mod.embed_texts(["text"], token="tok", hub_url="http://hub")
        assert result == [None]

    def test_preserves_index_order(self):
        vec0 = [0.0] * 1536
        vec1 = [1.0] * 1536
        # API returns in reverse order
        response_body = json.dumps(
            {"data": [{"index": 1, "embedding": vec1}, {"index": 0, "embedding": vec0}]}
        ).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = mod.embed_texts(["first", "second"], token="tok", hub_url="http://hub")

        assert result[0] == vec0
        assert result[1] == vec1


# ---------------------------------------------------------------------------
# Tests for run()
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, rows=None, fail_on_execute=False):
        self._rows = rows or []
        self._executed = []
        self._committed = False
        self.fail = fail_on_execute

    def execute(self, sql, params=None):
        self._executed.append((sql, params))
        return FakeCursor(self._rows)

    def commit(self):
        self._committed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _make_rows(n):
    return [{"id": f"node_{i}", "content": f"content {i}"} for i in range(n)]


class TestRun:
    def test_no_nodes_exits_cleanly(self, capsys):
        fake_conn = FakeConn(rows=[])

        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value = fake_conn
        mock_psycopg.rows.dict_row = None

        with patch.dict(
            "sys.modules", {"psycopg": mock_psycopg, "psycopg.rows": mock_psycopg.rows}
        ):
            with patch.dict(os.environ, {"MAC_WORKER_TOKEN": "tok123"}):
                mod.run("bullwinkle", batch_size=10, dry_run=False, limit=None)

        out = capsys.readouterr().out
        assert "nothing to do" in out

    def test_dry_run_does_not_embed(self, capsys):
        rows = _make_rows(5)
        fake_conn = FakeConn(rows=rows)

        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value = fake_conn
        mock_psycopg.rows.dict_row = None

        with patch.dict(
            "sys.modules", {"psycopg": mock_psycopg, "psycopg.rows": mock_psycopg.rows}
        ):
            with patch.dict(os.environ, {"MAC_WORKER_TOKEN": "tok123"}):
                with patch.object(mod, "embed_texts") as mock_embed:
                    mod.run("bullwinkle", batch_size=10, dry_run=True, limit=None)

        mock_embed.assert_not_called()
        out = capsys.readouterr().out
        assert "dry-run" in out

    def test_missing_token_exits(self):
        env = {k: v for k, v in os.environ.items() if k != "MAC_WORKER_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit):
                mod.run("bullwinkle", batch_size=10, dry_run=False, limit=None)

    def test_batches_nodes_correctly(self, capsys):
        rows = _make_rows(45)
        fake_conn_fetch = FakeConn(rows=rows)
        fake_conn_update = FakeConn(rows=[])
        call_count = [0]

        mock_psycopg = MagicMock()

        def mock_connect(dsn, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return fake_conn_fetch
            return fake_conn_update

        mock_psycopg.connect.side_effect = mock_connect
        mock_psycopg.rows.dict_row = None

        vec = [0.5] * 1536

        with patch.dict(
            "sys.modules", {"psycopg": mock_psycopg, "psycopg.rows": mock_psycopg.rows}
        ):
            with patch.dict(os.environ, {"MAC_WORKER_TOKEN": "tok123"}):
                with patch.object(mod, "embed_texts", return_value=[vec] * 20) as mock_embed:
                    with patch("time.sleep"):  # skip rate limit delay
                        mod.run("bullwinkle", batch_size=20, dry_run=False, limit=None)

        # 45 nodes at batch_size=20 → 3 batches (20+20+5)
        assert mock_embed.call_count == 3
        first_call_texts = mock_embed.call_args_list[0][0][0]
        assert len(first_call_texts) == 20

    def test_skips_wrong_dimension_vectors(self, capsys):
        rows = _make_rows(2)
        fake_conn = FakeConn(rows=rows)
        call_count = [0]

        mock_psycopg = MagicMock()

        def mock_connect(dsn, **kw):
            call_count[0] += 1
            return fake_conn

        mock_psycopg.connect.side_effect = mock_connect
        mock_psycopg.rows.dict_row = None

        bad_vec = [0.1] * 512  # wrong dims
        good_vec = [0.2] * 1536

        with patch.dict(
            "sys.modules", {"psycopg": mock_psycopg, "psycopg.rows": mock_psycopg.rows}
        ):
            with patch.dict(os.environ, {"MAC_WORKER_TOKEN": "tok123"}):
                with patch.object(mod, "embed_texts", return_value=[bad_vec, good_vec]):
                    mod.run("bullwinkle", batch_size=10, dry_run=False, limit=None)

        out = capsys.readouterr()
        assert "1 failed" in out.out or "failed" in out.err

    def test_clips_long_content(self):
        """Content >MAX_CONTENT_CHARS is clipped before embedding."""
        long_content = "x" * 10000
        rows = [{"id": "long_node", "content": long_content}]
        fake_conn = FakeConn(rows=rows)

        call_count = [0]
        mock_psycopg = MagicMock()

        def mock_connect(dsn, **kw):
            call_count[0] += 1
            return fake_conn

        mock_psycopg.connect.side_effect = mock_connect
        mock_psycopg.rows.dict_row = None

        captured_texts = []

        def fake_embed(texts, token, hub_url):
            captured_texts.extend(texts)
            return [[0.1] * 1536]

        with patch.dict(
            "sys.modules", {"psycopg": mock_psycopg, "psycopg.rows": mock_psycopg.rows}
        ):
            with patch.dict(os.environ, {"MAC_WORKER_TOKEN": "tok123"}):
                with patch.object(mod, "embed_texts", side_effect=fake_embed):
                    mod.run("bullwinkle", batch_size=10, dry_run=False, limit=None)

        assert len(captured_texts) == 1
        assert len(captured_texts[0]) <= mod.MAX_CONTENT_CHARS
