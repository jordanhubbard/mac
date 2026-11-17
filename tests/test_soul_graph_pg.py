"""Tests for SoulGraphPG schema routing and agent-name normalization.

These tests do NOT require a live Postgres connection — they use
unittest.mock to intercept psycopg calls and verify that the right
table names and agent identifiers reach the SQL layer.
"""

from __future__ import annotations

import json
import io
from unittest.mock import MagicMock, patch, call
from mac.soul_graph_pg import SoulGraphPG


# ---------------------------------------------------------------------------
# Schema routing helpers
# ---------------------------------------------------------------------------

def _make_pg(agent: str = "rocky", schema: str = "public") -> SoulGraphPG:
    return SoulGraphPG(pg_url="postgresql://x/y", agent=agent, schema=schema, name=agent)


def test_public_schema_routes_to_soul_nodes():
    g = _make_pg(schema="public")
    assert g._nodes_table() == "public.soul_nodes"
    assert g._edges_table() == "public.soul_edges"


def test_fleet_memory_schema_routes_to_nodes():
    g = _make_pg(schema="fleet_memory")
    assert g._nodes_table() == "fleet_memory.nodes"
    assert g._edges_table() == "fleet_memory.edges"


def test_public_agent_col_is_agent():
    g = _make_pg(schema="public")
    assert g._agent_col() == "agent"


def test_fleet_memory_agent_col_is_owner_agent():
    g = _make_pg(schema="fleet_memory")
    assert g._agent_col() == "owner_agent"


def test_public_agent_val_passthrough():
    g = _make_pg(agent="rocky", schema="public")
    assert g._agent_val("rocky") == "rocky"
    assert g._agent_val("agent_rocky") == "agent_rocky"


def test_fleet_memory_agent_val_prefixes_bare_names():
    g = _make_pg(agent="rocky", schema="fleet_memory")
    assert g._agent_val("rocky") == "agent_rocky"
    assert g._agent_val("natasha") == "agent_natasha"
    # already-prefixed names are not double-prefixed
    assert g._agent_val("agent_rocky") == "agent_rocky"


# ---------------------------------------------------------------------------
# save_pg routes to correct table
# ---------------------------------------------------------------------------

def test_save_pg_uses_public_soul_nodes_by_default():
    import mac.soul_graph_pg as sgpg
    g = _make_pg(schema="public")
    g.add("test node", node_id="n1")

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    with patch.object(sgpg.psycopg, "connect", return_value=mock_conn):
        g.save_pg()

    calls_text = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "public.soul_nodes" in calls_text
    assert "fleet_memory" not in calls_text


def test_save_pg_uses_fleet_memory_nodes_when_schema_set():
    import mac.soul_graph_pg as sgpg
    g = _make_pg(schema="fleet_memory")
    g.add("test node", node_id="n1")

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    with patch.object(sgpg.psycopg, "connect", return_value=mock_conn):
        g.save_pg()

    calls_text = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "fleet_memory.nodes" in calls_text
    assert "soul_nodes" not in calls_text


def test_save_pg_uses_owner_agent_col_for_fleet_memory():
    import mac.soul_graph_pg as sgpg
    g = _make_pg(agent="natasha", schema="fleet_memory")
    g.add("test node", node_id="n1")

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    with patch.object(sgpg.psycopg, "connect", return_value=mock_conn):
        g.save_pg()

    calls_text = " ".join(str(c) for c in mock_cur.execute.call_args_list)
    assert "owner_agent" in calls_text
    assert "agent_natasha" in calls_text


# ---------------------------------------------------------------------------
# soul_mcp: main() routes to serve_pg when SOUL_PG_URL is set
# ---------------------------------------------------------------------------

def test_main_routes_to_serve_pg_via_env(monkeypatch):
    import mac.soul_mcp as mcp

    monkeypatch.setenv("SOUL_PG_URL", "postgresql://fleet_agent:pw@host/agent_brain")
    monkeypatch.setenv("SOUL_PG_AGENT", "rocky")

    with patch.object(mcp, "serve_pg") as mock_serve_pg, \
         patch.object(mcp, "serve") as mock_serve:
        mcp.main([])
        mock_serve_pg.assert_called_once_with(
            "postgresql://fleet_agent:pw@host/agent_brain", "rocky"
        )
        mock_serve.assert_not_called()


def test_main_routes_to_serve_via_file_when_no_pg_url(monkeypatch, tmp_path):
    import mac.soul_mcp as mcp

    monkeypatch.delenv("SOUL_PG_URL", raising=False)
    soul_file = tmp_path / "test_soul.json"

    with patch.object(mcp, "serve") as mock_serve, \
         patch.object(mcp, "serve_pg") as mock_serve_pg:
        mcp.main(["--soul-file", str(soul_file)])
        mock_serve.assert_called_once_with(soul_file)
        mock_serve_pg.assert_not_called()
