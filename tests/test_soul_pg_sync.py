"""Tests for soul_pg_sync: bidirectional soul.json ↔ fleet_brain Postgres sync."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_soul_json(tmp_path: Path) -> Path:
    """A minimal soul.json with two nodes and one parent edge."""
    data = {
        "name": "test_agent",
        "exploration": False,
        "nodes": {
            "aaa00001": {
                "id": "aaa00001",
                "content": "I am the test agent. Warm and earnest.",
                "tags": ["identity", "soul"],
                "pinned": True,
                "key": 999.0,
                "parents": [],
                "children": ["bbb00002"],
                "created_at": "2026-01-01T00:00:00+00:00",
                "access_count": 5,
                "metadata": {},
            },
            "bbb00002": {
                "id": "bbb00002",
                "content": "Grand quest: become the fleet's judgment.",
                "tags": ["grand_quest", "principle"],
                "pinned": True,
                "key": 50.0,
                "parents": ["aaa00001"],
                "children": [],
                "created_at": "2026-01-02T00:00:00+00:00",
                "access_count": 2,
                "metadata": {},
            },
        },
    }
    p = tmp_path / "soul.json"
    p.write_text(json.dumps(data))
    return p


# ---------------------------------------------------------------------------
# _edges_from_nodes
# ---------------------------------------------------------------------------

def test_edges_from_nodes_extracts_parent_child():
    from mac.soul_pg_sync import _edges_from_nodes

    nodes = [
        {"id": "child", "content": "c", "parents": ["parent1", "parent2"]},
        {"id": "parent1", "content": "p1", "parents": []},
        {"id": "parent2", "content": "p2", "parents": []},
    ]
    edges = _edges_from_nodes(nodes)
    assert len(edges) == 2
    froms = {e["from_id"] for e in edges}
    assert froms == {"parent1", "parent2"}
    for e in edges:
        assert e["to_id"] == "child"
        assert e["relation_type"] == "parent"


def test_edges_from_nodes_no_parents():
    from mac.soul_pg_sync import _edges_from_nodes

    nodes = [{"id": "solo", "content": "lone wolf", "parents": []}]
    assert _edges_from_nodes(nodes) == []


# ---------------------------------------------------------------------------
# PgSoulBackend.load — round-trip via mocked psycopg2
# ---------------------------------------------------------------------------

def _make_pg_rows(nodes, edges):
    """Build fake psycopg2 RealDictRow-like dicts."""
    from datetime import datetime, timezone
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

    pg_nodes = [
        {
            "node_id": n["id"],
            "content": n["content"],
            "tags": n.get("tags", []),
            "pinned": n.get("pinned", False),
            "score": n.get("key", 0.0),
            "created_at": ts,
        }
        for n in nodes
    ]
    pg_edges = [
        {"to_id": e["to_id"], "from_id": e["from_id"]}
        for e in edges
    ]
    return pg_nodes, pg_edges


def test_pg_backend_load_returns_soul_graph(sample_soul_json):
    """PgSoulBackend.load builds a SoulGraph from mocked PG rows."""
    from mac.soul_pg_sync import PgSoulBackend, _edges_from_nodes
    import sys
    from unittest.mock import MagicMock

    nodes_data = list(json.loads(sample_soul_json.read_text())["nodes"].values())
    edges_data = _edges_from_nodes(nodes_data)
    pg_nodes, pg_edges = _make_pg_rows(nodes_data, edges_data)

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    mock_cur.fetchall.side_effect = [pg_nodes, pg_edges]

    # Stub psycopg2.extras so it doesn't need the real package
    mock_extras = MagicMock()
    mock_extras.RealDictCursor = dict
    sys.modules.setdefault("psycopg2", MagicMock())
    sys.modules["psycopg2.extras"] = mock_extras

    with patch("mac.soul_pg_sync._connect", return_value=mock_conn):
        backend = PgSoulBackend(agent_id="test_agent", db_url="postgresql:///fake")
        g = backend.load("soul")

    assert len(g.nodes) == 2
    assert "aaa00001" in g.nodes
    assert "bbb00002" in g.nodes
    assert g.nodes["aaa00001"].pinned is True
    assert "aaa00001" in g.nodes["bbb00002"].parents


def test_pg_backend_save_calls_upsert(sample_soul_json):
    """PgSoulBackend.save calls upsert helpers and commits."""
    from mac.soul_pg_sync import PgSoulBackend
    from mac.soul_graph import SoulGraph

    # Build a real SoulGraph from JSON
    g = SoulGraph.load(sample_soul_json)

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur

    with patch("mac.soul_pg_sync._connect", return_value=mock_conn):
        backend = PgSoulBackend(agent_id="test_agent", db_url="postgresql:///fake")
        backend.save(g)

    mock_conn.commit.assert_called_once()
    # cursor.execute should have been called (upsert nodes + edges)
    assert mock_cur.execute.call_count >= 2


# ---------------------------------------------------------------------------
# json_to_pg / pg_to_json
# ---------------------------------------------------------------------------

def test_json_to_pg_upserts_nodes_and_edges(sample_soul_json):
    from mac.soul_pg_sync import json_to_pg

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur

    with patch("mac.soul_pg_sync._connect", return_value=mock_conn):
        result = json_to_pg(sample_soul_json, "test_agent", db_url="postgresql:///fake")

    assert result["nodes"] == 2
    assert result["edges"] == 1   # one parent edge: aaa00001 → bbb00002
    mock_conn.commit.assert_called_once()


def test_pg_to_json_writes_soul_file(tmp_path):
    from mac.soul_pg_sync import pg_to_json
    from datetime import datetime, timezone

    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pg_nodes = [
        {"node_id": "n1", "content": "hello", "tags": ["identity"], "pinned": True, "score": 1.0, "created_at": ts},
    ]
    pg_edges = [{"from_id": "n1", "to_id": "n2", "relation_type": "parent", "weight": 1.0}]

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    mock_cur.fetchall.side_effect = [pg_nodes, pg_edges]

    out_path = tmp_path / "soul.json"
    with patch("mac.soul_pg_sync._connect", return_value=mock_conn):
        result = pg_to_json(out_path, "test_agent", db_url="postgresql:///fake")

    assert result["nodes"] == 1
    data = json.loads(out_path.read_text())
    assert "n1" in data["nodes"]
    assert data["nodes"]["n1"]["pinned"] is True
    # n1 is a parent of n2 → n1.children should include n2
    assert "n2" in data["nodes"]["n1"]["children"]


# ---------------------------------------------------------------------------
# soul_mcp: serve_pg initializes and returns soul_summary
# ---------------------------------------------------------------------------

def test_serve_pg_initializes_and_summarises():
    """serve_pg should load from PgSoulBackend and respond to soul_summary."""
    import io
    from mac.soul_mcp import serve_pg
    from unittest.mock import patch, MagicMock
    from mac.soul_graph import SoulGraph

    # Build a tiny graph to return from backend.load
    g = SoulGraph(name="soul")
    g.add("I am a test node", tags={"identity"}, pinned=True)

    mock_backend_cls = MagicMock()
    mock_backend = MagicMock()
    mock_backend_cls.return_value = mock_backend
    mock_backend.load.return_value = g
    mock_backend.save = MagicMock()

    with patch("mac.soul_mcp.PgSoulBackend", mock_backend_cls, create=True):
        # Monkey-patch the import inside serve_pg
        import mac.soul_mcp as mcp_module
        orig = getattr(mcp_module, "serve_pg")

        inp = io.StringIO(
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"soul_summary","arguments":{}}}\n'
        )
        out = io.StringIO()

        # Call directly with mocked backend via patch on the import path
        with patch("mac.soul_pg_sync.PgSoulBackend", mock_backend_cls):
            serve_pg("test_agent", "postgresql:///fake", inp=inp, out=out)

    lines = [l for l in out.getvalue().strip().split("\n") if l]
    assert len(lines) == 2
    r2 = json.loads(lines[1])
    summary_text = r2["result"]["content"][0]["text"]
    assert "SoulGraph" in summary_text
    assert "1 nodes" in summary_text
