"""
pg_memory.py — Live Postgres dual-write layer for fleet memory.

Wraps soul_nodes / soul_edges on the fleet hub Postgres (agent_brain database).

Usage: imported by memory_graph.py; every write goes here too.
Failures are logged but never crash the caller — SQLite is still ground truth
for private/personal nodes; PG is the shared fleet layer.

Schema (live on hub):
    soul_nodes(id TEXT PK, content TEXT, tags TEXT[], parents TEXT[],
               pinned BOOL, score FLOAT8, created_at TIMESTAMPTZ,
               updated_at TIMESTAMPTZ, last_accessed TIMESTAMPTZ,
               access_count INT, metadata JSONB,
               embedding VECTOR(1536), search_vec TSVECTOR [generated],
               agent TEXT)
    soul_edges(parent_id TEXT, child_id TEXT, relation TEXT,
               created_at TIMESTAMPTZ, PK(parent_id,child_id,relation))

Credentials
-----------
Set FLEET_PG_URL to a full libpq connection URL, e.g.::

    export FLEET_PG_URL=postgresql://user@host:5432/dbname

Alternatively, set the standard libpq environment variables (PGHOST,
PGPORT, PGDATABASE, PGUSER, PGPASSWORD) or rely on ~/.pgpass for
password-less auth.  No connection defaults are compiled in.
"""

import os
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ── Connection config ──────────────────────────────────────────────────────────
# Configure via FLEET_PG_URL (full libpq-style connection URL) or the standard
# libpq env vars (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD / ~/.pgpass).
# No hardcoded defaults — connection config must come entirely from the environment.
_FLEET_PG_URL = os.environ.get("FLEET_PG_URL", "")

# Default agent name: override via FLEET_AGENT_ID env or call set_agent()
_AGENT_ID = os.environ.get("FLEET_AGENT_ID", "natasha")

_conn = None  # module-level cached connection


def set_agent(agent_id: str):
    """Call once at startup to stamp writes with the right agent name."""
    global _AGENT_ID
    _AGENT_ID = agent_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_conn():
    """Lazy connect; reconnect if closed/broken."""
    global _conn
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        raise RuntimeError("psycopg2 not installed; PG writes unavailable")

    if _conn is None or _conn.closed:
        common: dict = dict(
            connect_timeout=5,
            options="-c statement_timeout=4000",  # 4 s max per statement
        )
        if _FLEET_PG_URL:
            _conn = psycopg2.connect(_FLEET_PG_URL, **common)
        else:
            # Fall back to libpq env vars (PGHOST, PGPORT, PGDATABASE,
            # PGUSER, PGPASSWORD or ~/.pgpass); psycopg2 reads them automatically.
            _conn = psycopg2.connect(**common)
        _conn.autocommit = False
    return _conn


def _exec(sql: str, params: tuple = ()):
    """Execute SQL against PG; returns cursor. Caller must commit."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


def available() -> bool:
    """True if PG is reachable right now."""
    try:
        _exec("SELECT 1")
        return True
    except Exception:
        return False


# ── Write path ─────────────────────────────────────────────────────────────────


def upsert_node(
    node_id: str,
    content: str,
    kind: str = "memory",
    tags: list = None,
    pinned: bool = False,
    score: float = 1.0,
    created_at: str = None,
) -> bool:
    """
    Upsert a node into soul_nodes.
    Returns True on success, False on any failure (caller keeps going).
    visibility:private nodes are skipped — they stay local only.
    """
    tags_list: list = tags or []
    # Respect visibility: private nodes don't leave this machine
    if "visibility:private" in tags_list:
        return False

    ts = created_at or _now().isoformat()
    metadata = json.dumps({"kind": kind})

    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO soul_nodes
              (id, content, tags, pinned, score, agent,
               created_at, updated_at, last_accessed, metadata)
            VALUES (%s, %s, %s::text[], %s, %s, %s, %s, NOW(), NOW(), %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
              content      = EXCLUDED.content,
              tags         = EXCLUDED.tags,
              pinned       = EXCLUDED.pinned,
              score        = EXCLUDED.score,
              updated_at   = NOW(),
              metadata     = EXCLUDED.metadata
        """,
            (node_id, content, tags_list, pinned, score, _AGENT_ID, ts, metadata),
        )
        conn.commit()
        return True
    except Exception as e:
        log.warning("pg_memory upsert_node failed for %s: %s", node_id, e)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        global _conn
        _conn = None  # force reconnect next call
        return False


def upsert_edge(parent_id: str, child_id: str, relation: str = "related-to") -> bool:
    """
    Upsert an edge into soul_edges.
    Returns True on success, False on failure.
    """
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO soul_edges (parent_id, child_id, relation, created_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (parent_id, child_id, relation) DO NOTHING
        """,
            (parent_id, child_id, relation),
        )
        conn.commit()
        return True
    except Exception as e:
        log.warning("pg_memory upsert_edge failed %s→%s: %s", parent_id, child_id, e)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        global _conn
        _conn = None
        return False


def record_access(node_id: str, new_score: float, access_count: int) -> bool:
    """Update score and access stats on an existing node."""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE soul_nodes
            SET score        = %s,
                access_count = %s,
                last_accessed = NOW(),
                updated_at    = NOW()
            WHERE id = %s
        """,
            (new_score, access_count, node_id),
        )
        conn.commit()
        return True
    except Exception as e:
        log.warning("pg_memory record_access failed %s: %s", node_id, e)
        try:
            _get_conn().rollback()
        except Exception:
            pass
        global _conn
        _conn = None
        return False


# ── Cross-agent read path ──────────────────────────────────────────────────────


def fleet_search(query: str, limit: int = 10, agent_filter: str = None) -> list:
    """
    Full-text search across all agents in soul_nodes.
    Returns list of dicts: {id, content, agent, score, pinned, tags}.
    Skips visibility:private nodes (they were never written here).
    """
    try:
        conn = _get_conn()
        cur = conn.cursor()
        base = """
            SELECT id, content, agent, score, pinned, tags
            FROM soul_nodes
            WHERE search_vec @@ plainto_tsquery('english', %s)
        """
        params = [query]
        if agent_filter:
            base += " AND agent = %s"
            params.append(agent_filter)
        base += " ORDER BY score DESC, ts_rank(search_vec, plainto_tsquery('english', %s)) DESC LIMIT %s"
        params += [query, limit]
        cur.execute(base, params)
        rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "content": r[1],
                "agent": r[2],
                "score": r[3],
                "pinned": r[4],
                "tags": r[5] or [],
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("pg_memory fleet_search failed: %s", e)
        global _conn
        _conn = None
        return []


def fleet_top(limit: int = 20, agent_filter: str = None) -> list:
    """Top-N nodes by score across the whole fleet."""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        sql = "SELECT id, content, agent, score, pinned, tags FROM soul_nodes"
        params = []
        if agent_filter:
            sql += " WHERE agent = %s"
            params.append(agent_filter)
        sql += " ORDER BY score DESC LIMIT %s"
        params.append(limit)
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "summary": r[1][:100],
                "agent": r[2],
                "score": r[3],
                "pinned": r[4],
                "tags": r[5] or [],
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("pg_memory fleet_top failed: %s", e)
        global _conn
        _conn = None
        return []


def fleet_stats() -> dict:
    """Node and edge counts per agent."""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT agent, COUNT(*) as nodes
            FROM soul_nodes GROUP BY agent ORDER BY nodes DESC
        """)
        nodes = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) FROM soul_edges")
        edges = cur.fetchone()[0]
        return {"agents": nodes, "total_edges": edges, "total_nodes": sum(nodes.values())}
    except Exception as e:
        log.warning("pg_memory fleet_stats failed: %s", e)
        global _conn
        _conn = None
        return {}
