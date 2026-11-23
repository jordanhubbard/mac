"""
tests/test_pg_memory.py — Unit tests for lib/memory_graph/pg_memory.py

All DB interactions are mocked: psycopg2 is stubbed at the module level so
tests run without a real Postgres connection.

Coverage:
  - set_agent(): stamps _AGENT_ID
  - available(): True when SELECT 1 succeeds, False on exception
  - upsert_node(): happy path, visibility:private skip, failure path (returns False)
  - upsert_edge(): happy path, failure path (returns False)
  - record_access(): happy path, failure path
  - fleet_search(): result shaping, agent_filter branch, exception path
  - fleet_top(): result shaping, agent_filter branch, exception path
  - fleet_stats(): counts dict shape, exception path
  - _conn reset on failure (module reconnects after error)
"""

import sys
import types
import importlib
from unittest.mock import MagicMock, patch, call
import pytest

# ── Stub psycopg2 before pg_memory imports it ──────────────────────────────────


def _make_psycopg2_stub():
    """Return a minimal psycopg2 stub sufficient for pg_memory's import."""
    mod = types.ModuleType("psycopg2")
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = dict  # used by nothing in pg_memory but imported
    mod.extras = extras
    mod.connect = MagicMock()

    class OperationalError(Exception):
        pass

    class InterfaceError(Exception):
        pass

    mod.OperationalError = OperationalError
    mod.InterfaceError = InterfaceError
    return mod, extras


@pytest.fixture(autouse=True)
def fresh_pg_memory():
    """
    Reload pg_memory fresh with a clean psycopg2 stub for each test.
    This ensures module-level globals (_conn, _AGENT_ID) are reset.
    """
    stub, extras_stub = _make_psycopg2_stub()
    sys.modules["psycopg2"] = stub
    sys.modules["psycopg2.extras"] = extras_stub

    # Remove any cached module so importlib re-executes top-level code
    for key in list(sys.modules.keys()):
        if "pg_memory" in key:
            del sys.modules[key]

    # Add lib/memory_graph to path
    import os

    lib_path = os.path.join(os.path.dirname(__file__), "..", "lib", "memory_graph")
    if lib_path not in sys.path:
        sys.path.insert(0, lib_path)

    import pg_memory as pg

    yield pg

    # Cleanup
    for key in list(sys.modules.keys()):
        if "pg_memory" in key:
            del sys.modules[key]


def _mock_conn(pg):
    """Create and wire a mock connection into pg_memory._conn."""
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_cursor.fetchone.return_value = None

    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.autocommit = False

    pg._conn = mock_conn
    return mock_conn, mock_cursor


# ── set_agent ─────────────────────────────────────────────────────────────────


def test_set_agent_changes_agent_id(fresh_pg_memory):
    pg = fresh_pg_memory
    pg.set_agent("bullwinkle")
    assert pg._AGENT_ID == "bullwinkle"


def test_set_agent_default_is_natasha(fresh_pg_memory):
    pg = fresh_pg_memory
    # Default comes from env or hardcoded fallback; without env var it's "natasha"
    import os

    assert pg._AGENT_ID == os.environ.get("FLEET_AGENT_ID", "natasha")


# ── available ─────────────────────────────────────────────────────────────────


def test_available_true_when_query_succeeds(fresh_pg_memory):
    pg = fresh_pg_memory
    _mock_conn(pg)
    assert pg.available() is True


def test_available_false_when_query_raises(fresh_pg_memory):
    pg = fresh_pg_memory
    # _get_conn will fail because psycopg2.connect raises
    sys.modules["psycopg2"].connect.side_effect = Exception("connection refused")
    assert pg.available() is False


# ── upsert_node ───────────────────────────────────────────────────────────────


def test_upsert_node_happy_path(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)

    result = pg.upsert_node(
        node_id="n1",
        content="hello fleet",
        kind="memory",
        tags=["fleet", "test"],
        pinned=False,
        score=1.0,
    )

    assert result is True
    mock_cursor.execute.assert_called_once()
    mock_conn.commit.assert_called_once()


def test_upsert_node_skips_private(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)

    result = pg.upsert_node(
        node_id="private1",
        content="private data",
        tags=["visibility:private"],
    )

    assert result is False
    mock_cursor.execute.assert_not_called()


def test_upsert_node_returns_false_on_db_error(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    mock_cursor.execute.side_effect = Exception("unique violation")

    result = pg.upsert_node("n2", "some content")

    assert result is False
    # _conn should be reset so next call reconnects
    assert pg._conn is None


def test_upsert_node_includes_agent_in_insert(fresh_pg_memory):
    pg = fresh_pg_memory
    pg.set_agent("rocky")
    mock_conn, mock_cursor = _mock_conn(pg)

    pg.upsert_node("n3", "content for rocky")

    # The agent value "rocky" should be in the params tuple
    args = mock_cursor.execute.call_args[0]
    params = args[1]  # second positional arg to execute
    assert "rocky" in params


# ── upsert_edge ───────────────────────────────────────────────────────────────


def test_upsert_edge_happy_path(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)

    result = pg.upsert_edge("parent1", "child1", "implements")

    assert result is True
    mock_cursor.execute.assert_called_once()
    # Verify relation and IDs are in params
    args = mock_cursor.execute.call_args[0]
    params = args[1]
    assert "parent1" in params
    assert "child1" in params
    assert "implements" in params


def test_upsert_edge_default_relation(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)

    pg.upsert_edge("p", "c")  # no relation arg

    args = mock_cursor.execute.call_args[0]
    params = args[1]
    assert "related-to" in params


def test_upsert_edge_returns_false_on_error(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    mock_cursor.execute.side_effect = Exception("deadlock")

    result = pg.upsert_edge("p", "c", "child")

    assert result is False
    assert pg._conn is None


# ── record_access ─────────────────────────────────────────────────────────────


def test_record_access_happy_path(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)

    result = pg.record_access("node1", new_score=2.5, access_count=7)

    assert result is True
    args = mock_cursor.execute.call_args[0]
    params = args[1]
    assert 2.5 in params
    assert 7 in params
    assert "node1" in params


def test_record_access_returns_false_on_error(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    mock_cursor.execute.side_effect = Exception("timeout")

    result = pg.record_access("node1", 1.0, 1)

    assert result is False
    assert pg._conn is None


# ── fleet_search ──────────────────────────────────────────────────────────────


def test_fleet_search_returns_shaped_dicts(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    mock_cursor.fetchall.return_value = [
        ("id1", "content one", "bullwinkle", 1.5, False, ["fleet"]),
        ("id2", "content two", "rocky", 2.0, True, ["fleet", "pinned"]),
    ]

    results = pg.fleet_search("test query")

    assert len(results) == 2
    assert results[0]["id"] == "id1"
    assert results[0]["content"] == "content one"
    assert results[0]["agent"] == "bullwinkle"
    assert results[0]["score"] == 1.5
    assert results[0]["pinned"] is False
    assert results[0]["tags"] == ["fleet"]
    assert results[1]["pinned"] is True


def test_fleet_search_with_agent_filter(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    mock_cursor.fetchall.return_value = []

    pg.fleet_search("rag", agent_filter="natasha")

    sql = mock_cursor.execute.call_args[0][0]
    assert "agent" in sql  # agent filter clause present


def test_fleet_search_returns_empty_on_exception(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    mock_cursor.execute.side_effect = Exception("syntax error")

    results = pg.fleet_search("broken query")

    assert results == []
    assert pg._conn is None


def test_fleet_search_null_tags_coerced(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    mock_cursor.fetchall.return_value = [
        ("id1", "content", "agent1", 1.0, False, None),  # tags=None from DB
    ]

    results = pg.fleet_search("anything")
    assert results[0]["tags"] == []


# ── fleet_top ─────────────────────────────────────────────────────────────────


def test_fleet_top_returns_summaries(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    long_content = "x" * 200
    mock_cursor.fetchall.return_value = [
        ("id1", long_content, "bullwinkle", 3.0, True, ["important"]),
    ]

    results = pg.fleet_top(limit=5)

    assert len(results) == 1
    assert results[0]["id"] == "id1"
    # summary is truncated to 100 chars
    assert len(results[0]["summary"]) == 100
    assert results[0]["pinned"] is True


def test_fleet_top_agent_filter_in_sql(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    mock_cursor.fetchall.return_value = []

    pg.fleet_top(limit=10, agent_filter="rocky")

    sql = mock_cursor.execute.call_args[0][0]
    # agent filter must appear in the WHERE clause
    assert "WHERE" in sql


def test_fleet_top_returns_empty_on_exception(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    mock_cursor.execute.side_effect = Exception("connection lost")

    results = pg.fleet_top()

    assert results == []


# ── fleet_stats ───────────────────────────────────────────────────────────────


def test_fleet_stats_shape(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)

    # Two calls: agents query, then edge count
    mock_cursor.fetchall.return_value = [("bullwinkle", 42), ("rocky", 100)]
    mock_cursor.fetchone.return_value = (200,)

    stats = pg.fleet_stats()

    assert stats["agents"]["bullwinkle"] == 42
    assert stats["agents"]["rocky"] == 100
    assert stats["total_edges"] == 200
    assert stats["total_nodes"] == 142


def test_fleet_stats_returns_empty_dict_on_exception(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    mock_cursor.execute.side_effect = Exception("pg down")

    stats = pg.fleet_stats()

    assert stats == {}


# ── connection reset semantics ────────────────────────────────────────────────


def test_conn_is_none_after_upsert_failure(fresh_pg_memory):
    pg = fresh_pg_memory
    mock_conn, mock_cursor = _mock_conn(pg)
    mock_cursor.execute.side_effect = Exception("disk full")

    pg.upsert_node("x", "y")

    assert pg._conn is None


def test_conn_reconnects_after_reset(fresh_pg_memory):
    """After _conn=None, the next call triggers psycopg2.connect again."""
    pg = fresh_pg_memory
    # Wire psycopg2.connect to return a fresh mock connection
    new_conn = MagicMock()
    new_conn.closed = False
    new_cursor = MagicMock()
    new_cursor.fetchall.return_value = []
    new_conn.cursor.return_value = new_cursor
    sys.modules["psycopg2"].connect.return_value = new_conn
    pg._FLEET_PG_URL = "postgresql://test@localhost/test"

    # Force _conn to None (simulating post-error state)
    pg._conn = None

    # Should succeed on reconnect
    result = pg.fleet_search("reconnect test")

    assert result == []
    sys.modules["psycopg2"].connect.assert_called_once()
