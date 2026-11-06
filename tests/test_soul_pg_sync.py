"""
tests/test_soul_pg_sync.py — Tests for soul_pg_sync bidirectional sync.

Uses a lightweight in-memory SQLite shim (via unittest.mock) for the
"happy path" logic tests (upsert, conflict resolution, edge derivation,
idempotency), so the test suite runs without a live Postgres instance.

Integration tests (marked with @pytest.mark.integration) require a live
fleet_brain Postgres and are skipped by default; run with:
  pytest -m integration tests/test_soul_pg_sync.py
"""

from __future__ import annotations

import json
import math
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call
import pytest

# ---------------------------------------------------------------------------
# Helpers — fake connection/cursor
# ---------------------------------------------------------------------------


class FakeCursor:
    """Minimal cursor that records SQL calls and returns canned results."""

    def __init__(self):
        self.executed: list[tuple] = []
        self._results: list[list] = []
        self.rowcount = 0

    def queue_results(self, rows: list):
        self._results.append(rows)

    def execute(self, sql, params=()):
        self.executed.append((sql.strip(), params))
        self.rowcount = 1  # default: one row affected

    def fetchall(self):
        if self._results:
            return self._results.pop(0)
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeConn:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_soul_json(tmp_path) -> Path:
    """A minimal soul.json with 3 nodes and a parent chain."""
    nodes = {
        "aaa": {
            "id": "aaa",
            "content": "Root axiom",
            "tags": ["identity"],
            "parents": [],
            "children": ["bbb"],
            "pinned": True,
            "created_at": 1000.0,
            "last_accessed": 1000.0,
            "access_count": 5,
            "metadata": {},
        },
        "bbb": {
            "id": "bbb",
            "content": "Child of aaa",
            "tags": ["lesson"],
            "parents": ["aaa"],
            "children": ["ccc"],
            "pinned": False,
            "created_at": 2000.0,
            "last_accessed": 2000.0,
            "access_count": 2,
            "metadata": {},
        },
        "ccc": {
            "id": "ccc",
            "content": "Grandchild of aaa, child of bbb",
            "tags": ["pattern", "fleet"],
            "parents": ["bbb"],
            "children": [],
            "pinned": False,
            "created_at": 3000.0,
            "last_accessed": 3000.0,
            "access_count": 0,
            "metadata": {},
        },
    }
    p = tmp_path / "soul.json"
    p.write_text(json.dumps({"name": "soul", "exploration": False, "nodes": nodes}))
    return p


# ---------------------------------------------------------------------------
# json_to_pg — insertion path
# ---------------------------------------------------------------------------


class TestJsonToPgInsert:
    def test_new_nodes_inserted(self, sample_soul_json):
        """All nodes absent in PG → all inserted, edges derived."""
        from mac.soul_pg_sync import json_to_pg, _upsert_nodes, _upsert_edges

        cur = FakeCursor()
        cur.queue_results([])  # SELECT existing → empty

        with patch("mac.soul_pg_sync._get_conn") as mock_conn:
            mock_conn.return_value = FakeConn(cur)
            report = json_to_pg("bullwinkle", sample_soul_json)

        assert report["nodes_inserted"] == 3
        assert report["nodes_updated"] == 0
        assert report["nodes_skipped"] == 0
        # Edges: aaa→bbb, bbb→ccc
        assert report["edges_inserted"] == 2
        assert not report["errors"]

    def test_all_nodes_already_exist_identical(self, sample_soul_json):
        """All nodes present with identical content → all skipped."""
        from mac.soul_pg_sync import json_to_pg

        # Return same content / tags for each node
        existing_rows = [
            ("aaa", "Root axiom", ["identity"], True, math.inf),
            ("bbb", "Child of aaa", ["lesson"], False, 1.0),
            ("ccc", "Grandchild of aaa, child of bbb", ["pattern", "fleet"], False, 0.0),
        ]
        cur = FakeCursor()
        cur.queue_results(existing_rows)

        with patch("mac.soul_pg_sync._get_conn") as mock_conn:
            mock_conn.return_value = FakeConn(cur)
            report = json_to_pg("bullwinkle", sample_soul_json)

        assert report["nodes_skipped"] == 3
        assert report["nodes_inserted"] == 0
        assert report["nodes_updated"] == 0


# ---------------------------------------------------------------------------
# json_to_pg — conflict resolution
# ---------------------------------------------------------------------------


class TestJsonToPgConflictResolution:
    def test_richer_content_wins(self, sample_soul_json):
        """Content conflict resolution: richer (longer) content wins, no unnecessary writes.

        When PG already has the richer content, the skip condition fires (PG is already
        correct — no write needed).  When JSON has richer content, PG is updated.
        """
        from mac.soul_pg_sync import json_to_pg

        # PG has longer content for "aaa" (PG already wins, skip)
        # PG has shorter content for "bbb" (JSON wins → update PG)
        # PG has identical content for "ccc" (skip)
        existing_rows = [
            (
                "aaa",
                "Root axiom — extended PG version with more detail",
                ["identity"],
                True,
                math.inf,
            ),
            ("bbb", "Short", ["lesson"], False, 1.0),
            ("ccc", "Grandchild of aaa, child of bbb", ["pattern", "fleet"], False, 0.0),
        ]
        cur = FakeCursor()
        cur.queue_results(existing_rows)

        with patch("mac.soul_pg_sync._get_conn") as mock_conn:
            mock_conn.return_value = FakeConn(cur)
            report = json_to_pg("bullwinkle", sample_soul_json)

        # aaa: PG content already richer → winner == PG content → skip (no write needed)
        # bbb: JSON content richer → update PG with JSON content
        # ccc: identical → skip
        assert report["nodes_updated"] == 1, (
            f"Expected 1 update (bbb only), got {report['nodes_updated']}"
        )
        assert report["nodes_skipped"] == 2, (
            f"Expected 2 skips (aaa + ccc), got {report['nodes_skipped']}"
        )

        # Confirm the UPDATE for "bbb" used the longer JSON string
        update_calls = [
            (sql, p) for sql, p in cur.executed if sql.startswith("UPDATE") and p and "bbb" in p
        ]
        assert update_calls, "Expected an UPDATE for bbb (JSON content is richer)"
        content_used = update_calls[0][1][0]
        assert content_used == "Child of aaa", (
            f"Expected JSON content to win for bbb, got: {content_used!r}"
        )

    def test_tags_are_merged(self, sample_soul_json, tmp_path):
        """Tags from JSON and PG are unioned — when JSON has a tag PG lacks, PG is updated.

        Note: if PG already has a superset of JSON tags (and content is identical),
        no update fires — the skip-condition catches it. For the update to fire,
        JSON must contribute at least one tag PG doesn't have.
        """
        import json as _json
        from mac.soul_pg_sync import json_to_pg

        # Modify bbb in soul to have a new tag that PG doesn't know about
        soul_data = _json.loads(sample_soul_json.read_text())
        soul_data["nodes"]["bbb"]["tags"].append("json_new_tag")
        modified = tmp_path / "soul_modified.json"
        modified.write_text(_json.dumps(soul_data))

        # bbb in PG has only ["lesson"]; JSON now has ["lesson", "json_new_tag"]
        # → winner_tags = {"lesson", "json_new_tag"} ≠ PG's {"lesson"} → update fires
        existing_rows = [
            ("aaa", "Root axiom", ["identity"], True, math.inf),
            ("bbb", "Child of aaa", ["lesson"], False, 1.0),
            ("ccc", "Grandchild of aaa, child of bbb", ["pattern", "fleet"], False, 0.0),
        ]
        cur = FakeCursor()
        cur.queue_results(existing_rows)

        with patch("mac.soul_pg_sync._get_conn") as mock_conn:
            mock_conn.return_value = FakeConn(cur)
            report = json_to_pg("bullwinkle", modified)

        update_calls = [
            (sql, p) for sql, p in cur.executed if sql.startswith("UPDATE") and p and "bbb" in p
        ]
        assert update_calls, "Expected an UPDATE for bbb (json_new_tag is new)"
        merged_tags = set(update_calls[0][1][1])
        assert "lesson" in merged_tags, "lesson should be preserved"
        assert "json_new_tag" in merged_tags, "json_new_tag should be added"


# ---------------------------------------------------------------------------
# Edge derivation
# ---------------------------------------------------------------------------


class TestEdgeDerivation:
    def test_parent_edges_derived(self, sample_soul_json):
        """Parent links from JSON become soul_edges rows."""
        from mac.soul_pg_sync import _upsert_edges

        cur = FakeCursor()
        raw_nodes = json.loads(sample_soul_json.read_text())["nodes"]
        report = {"edges_inserted": 0, "edges_skipped": 0}

        _upsert_edges(cur, "bullwinkle", raw_nodes, report, dry_run=False)

        insert_sqls = [sql for sql, _ in cur.executed if "INSERT INTO soul_edges" in sql]
        # Expect aaa→bbb and bbb→ccc
        assert len(insert_sqls) == 2
        assert report["edges_inserted"] == 2

    def test_no_self_loop_edges(self, tmp_path):
        """Nodes that list themselves as a parent don't produce self-loop edges."""
        from mac.soul_pg_sync import _upsert_edges

        raw_nodes = {
            "x": {
                "id": "x",
                "content": "self-loopy",
                "tags": [],
                "parents": ["x"],
                "children": [],
                "pinned": False,
                "created_at": 0.0,
                "last_accessed": 0.0,
                "access_count": 0,
                "metadata": {},
            }
        }
        cur = FakeCursor()
        report = {"edges_inserted": 0, "edges_skipped": 0}
        _upsert_edges(cur, "bullwinkle", raw_nodes, report, dry_run=False)

        insert_sqls = [sql for sql, _ in cur.executed if "INSERT INTO soul_edges" in sql]
        assert len(insert_sqls) == 0

    def test_duplicate_parent_declarations_deduplicated(self, tmp_path):
        """If parent appears in both parents[] of child and children[] of parent, only one edge."""
        from mac.soul_pg_sync import _upsert_edges

        raw_nodes = {
            "p": {
                "id": "p",
                "content": "parent",
                "tags": [],
                "parents": [],
                "children": ["q"],
                "pinned": False,
                "created_at": 0.0,
                "last_accessed": 0.0,
                "access_count": 0,
                "metadata": {},
            },
            "q": {
                "id": "q",
                "content": "child",
                "tags": [],
                "parents": ["p"],
                "children": [],
                "pinned": False,
                "created_at": 0.0,
                "last_accessed": 0.0,
                "access_count": 0,
                "metadata": {},
            },
        }
        cur = FakeCursor()
        report = {"edges_inserted": 0, "edges_skipped": 0}
        _upsert_edges(cur, "bullwinkle", raw_nodes, report, dry_run=False)

        insert_sqls = [sql for sql, _ in cur.executed if "INSERT INTO soul_edges" in sql]
        # Only one INSERT despite p.children=[q] and q.parents=[p] both pointing at the same edge
        assert len(insert_sqls) == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_second_run_skips_all(self, sample_soul_json):
        """Running json_to_pg twice leaves no additional writes on the second run."""
        from mac.soul_pg_sync import json_to_pg

        raw = json.loads(sample_soul_json.read_text())["nodes"]

        # Simulate PG reflecting exactly what JSON says after first run
        existing_rows = [
            ("aaa", "Root axiom", ["identity"], True, math.inf),
            ("bbb", "Child of aaa", ["lesson"], False, 1.1),
            ("ccc", "Grandchild of aaa, child of bbb", ["pattern", "fleet"], False, 0.0),
        ]
        cur = FakeCursor()
        cur.queue_results(existing_rows)

        with patch("mac.soul_pg_sync._get_conn") as mock_conn:
            mock_conn.return_value = FakeConn(cur)
            report = json_to_pg("bullwinkle", sample_soul_json)

        # On the second run everything matches → all skipped, no UPDATEs
        update_calls = [sql for sql, _ in cur.executed if sql.startswith("UPDATE")]
        assert report["nodes_skipped"] == 3
        assert not update_calls


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_no_writes(self, sample_soul_json):
        """dry_run=True: report counts are populated but no SQL writes executed."""
        from mac.soul_pg_sync import json_to_pg

        cur = FakeCursor()
        cur.queue_results([])  # no existing nodes

        with patch("mac.soul_pg_sync._get_conn") as mock_conn:
            mock_conn.return_value = FakeConn(cur)
            report = json_to_pg("bullwinkle", sample_soul_json, dry_run=True)

        write_calls = [
            sql for sql, _ in cur.executed if sql.startswith("INSERT") or sql.startswith("UPDATE")
        ]
        assert not write_calls, f"Expected no writes in dry_run, got: {write_calls}"
        assert report["nodes_inserted"] == 3
        assert report["dry_run"] is True


# ---------------------------------------------------------------------------
# pg_to_json (export)
# ---------------------------------------------------------------------------


class TestPgToJson:
    def test_export_roundtrip(self, tmp_path):
        """pg_to_json writes a valid soul.json from PG rows."""
        from mac.soul_pg_sync import pg_to_json

        import datetime

        now_dt = datetime.datetime.fromtimestamp(time.time())

        node_rows = [
            ("aaa", "Root axiom", ["identity"], True, math.inf, now_dt),
            ("bbb", "Child of aaa", ["lesson"], False, 1.0, now_dt),
        ]
        edge_rows = [
            ("aaa", "bbb", "parent_of"),
        ]

        cur = FakeCursor()
        cur.queue_results(node_rows)
        cur.queue_results(edge_rows)

        out_path = tmp_path / "soul_export.json"

        with patch("mac.soul_pg_sync._get_conn") as mock_conn:
            mock_conn.return_value = FakeConn(cur)
            report = pg_to_json("bullwinkle", out_path)

        assert out_path.exists()
        exported = json.loads(out_path.read_text())
        assert "nodes" in exported
        assert "aaa" in exported["nodes"]
        assert "bbb" in exported["nodes"]
        assert exported["nodes"]["bbb"]["parents"] == ["aaa"]
        assert report["nodes_exported"] == 2
        assert report["edges_exported"] == 1

    def test_export_dry_run_does_not_write(self, tmp_path):
        """dry_run=True on export: no file written."""
        from mac.soul_pg_sync import pg_to_json

        import datetime

        now_dt = datetime.datetime.fromtimestamp(time.time())

        cur = FakeCursor()
        cur.queue_results([("aaa", "Root", ["identity"], True, math.inf, now_dt)])
        cur.queue_results([])

        out_path = tmp_path / "should_not_exist.json"

        with patch("mac.soul_pg_sync._get_conn") as mock_conn:
            mock_conn.return_value = FakeConn(cur)
            pg_to_json("bullwinkle", out_path, dry_run=True)

        assert not out_path.exists()


# ---------------------------------------------------------------------------
# Integration marker (skipped unless -m integration)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_roundtrip(tmp_path):
    """Live test: import bullwinkle soul.json to fleet_brain, verify counts, export back."""
    from mac.soul_pg_sync import json_to_pg, pg_to_json

    soul_path = Path.home() / ".hermes" / "soul.json"
    if not soul_path.exists():
        pytest.skip("No soul.json found")

    dsn = "dbname=fleet_brain host=100.72.16.110 user=postgres"

    report_in = json_to_pg("bullwinkle", soul_path, dsn)
    assert not report_in["errors"]
    assert report_in["nodes_total"] > 0

    out_path = tmp_path / "soul_export.json"
    report_out = pg_to_json("bullwinkle", out_path, dsn)
    assert report_out["nodes_exported"] > 0
    assert out_path.exists()
