"""
soul_graph_pg.py — Postgres-backed persistence layer for SoulGraph.

Wraps a SoulGraph in-memory object with load/save operations against a shared
fleet Postgres database (agent_brain). One schema, one table, `agent` column
distinguishes ownership.

Design: load() fetches all nodes for the agent into an in-memory SoulGraph;
save() upserts only changed nodes. The in-memory object is the source of truth
during a session; Postgres is the durable store.

Connection string expected:
  postgresql://fleet_agent:<pass>@100.72.16.110:5432/agent_brain

Usage (from soul_mcp.py with --pg-url):
  graph = SoulGraphPG.load(pg_url, agent_name)
  ...
  graph.save()   # no path argument needed
"""

from __future__ import annotations

import json
import time
from typing import Optional, Any

from mac.soul_graph import SoulGraph, SoulNode


class SoulGraphPG(SoulGraph):
    """SoulGraph subclass that persists to Postgres instead of a JSON file.

    Call SoulGraphPG.from_pg(pg_url, agent) to load, then call .save_pg()
    after mutations. The soul_mcp serve_pg() function manages these calls.
    """

    def __init__(self, pg_url: str, agent: str, **kwargs: Any):
        super().__init__(**kwargs)
        self._pg_url = pg_url
        self._agent = agent
        self._dirty_ids: set[str] = set()  # IDs modified since last save
        self._pg_conn: Optional[Any] = None

    def _connect(self) -> Any:
        """Return a live psycopg connection (psycopg v3)."""
        import psycopg  # type: ignore[import]

        if self._pg_conn is None or self._pg_conn.closed:
            self._pg_conn = psycopg.connect(self._pg_url, autocommit=False)
        return self._pg_conn

    # ------------------------------------------------------------------
    # Override mutation methods to track dirty nodes
    # ------------------------------------------------------------------

    def add(
        self, content: str, tags=None, parents=None, pinned=False, metadata=None, node_id=None
    ) -> SoulNode:
        node = super().add(
            content, tags=tags, parents=parents, pinned=pinned, metadata=metadata, node_id=node_id
        )
        self._dirty_ids.add(node.id)
        # Also dirty any parents whose children list changed
        for pid in node.parents:
            self._dirty_ids.add(pid)
        return node

    def pin(self, node_id: str):
        super().pin(node_id)
        if node_id in self.nodes:
            self._dirty_ids.add(node_id)

    def touch(self, node_id: str, propagate_parents: bool = True) -> Optional[SoulNode]:
        node = super().touch(node_id, propagate_parents=propagate_parents)
        if node:
            self._dirty_ids.add(node_id)
            if propagate_parents:
                for pid in self.nodes[node_id].parents:
                    self._dirty_ids.add(pid)
        return node

    def link(self, parent_id: str, child_id: str) -> bool:
        result = super().link(parent_id, child_id)
        if result:
            self._dirty_ids.add(parent_id)
            self._dirty_ids.add(child_id)
        return result

    def tag(self, node_id: str, *tags: str):
        super().tag(node_id, *tags)
        if node_id in self.nodes:
            self._dirty_ids.add(node_id)

    # ------------------------------------------------------------------
    # Postgres I/O
    # ------------------------------------------------------------------

    @classmethod
    def from_pg(cls, pg_url: str, agent: str, cross_agent: bool = False) -> "SoulGraphPG":
        """Load soul nodes from Postgres into an in-memory SoulGraph.

        Args:
            pg_url:      Postgres connection URL.
            agent:       The owning agent name (writes always use this).
            cross_agent: If True, load nodes from ALL agents (read-only peers
                         included). Writes still tag new nodes with `agent`.
                         Useful for fleet-shared exploration queries.
        """
        import psycopg  # type: ignore[import]

        g = cls(pg_url=pg_url, agent=agent, name=agent)
        conn = psycopg.connect(pg_url, autocommit=False)
        with conn.cursor() as cur:
            if cross_agent:
                cur.execute(
                    """
                    SELECT id, content, tags, parents, pinned, score,
                           EXTRACT(EPOCH FROM created_at) AS created_epoch,
                           EXTRACT(EPOCH FROM last_accessed) AS accessed_epoch,
                           access_count, metadata
                    FROM soul_nodes
                    ORDER BY last_accessed DESC
                    """,
                )
            else:
                cur.execute(
                    """
                    SELECT id, content, tags, parents, pinned, score,
                           EXTRACT(EPOCH FROM created_at) AS created_epoch,
                           EXTRACT(EPOCH FROM last_accessed) AS accessed_epoch,
                           access_count, metadata
                    FROM soul_nodes
                    WHERE agent = %s
                    ORDER BY last_accessed DESC
                    """,
                    (agent,),
                )
            rows = cur.fetchall()
            id_set = {row[0] for row in rows}
            for row in rows:
                (
                    nid,
                    content,
                    tags,
                    parents,
                    pinned,
                    score,
                    created_epoch,
                    accessed_epoch,
                    access_count,
                    metadata_raw,
                ) = row
                # Filter parents to only those present in the loaded set
                parents_filtered = [p for p in (parents or []) if p in id_set]
                meta = (
                    metadata_raw
                    if isinstance(metadata_raw, dict)
                    else (json.loads(metadata_raw) if metadata_raw else {})
                )
                node = SoulNode(
                    id=nid,
                    content=content,
                    tags=set(tags or []),
                    parents=parents_filtered,
                    pinned=pinned,
                    created_at=float(created_epoch or time.time()),
                    last_accessed=float(accessed_epoch or time.time()),
                    access_count=int(access_count or 0),
                    metadata=meta,
                )
                g.nodes[nid] = node
                g.splay.insert(nid, node.rank_key())
                for t in node.tags:
                    g._tag_index.setdefault(t, set()).add(nid)
                g._index_words(node)

            # Rebuild children from edges (scope matches the node query above)
            if cross_agent:
                cur.execute(
                    """
                    SELECT e.parent_id, e.child_id
                    FROM soul_edges e
                    WHERE e.parent_id = ANY(%s) AND e.child_id = ANY(%s)
                    """,
                    (list(id_set), list(id_set)),
                )
            else:
                cur.execute(
                    """
                    SELECT e.parent_id, e.child_id
                    FROM soul_edges e
                    JOIN soul_nodes p ON p.id = e.parent_id AND p.agent = %s
                    JOIN soul_nodes c ON c.id = e.child_id AND c.agent = %s
                    """,
                    (agent, agent),
                )
            for parent_id, child_id in cur.fetchall():
                if parent_id in g.nodes and child_id in g.nodes:
                    pn = g.nodes[parent_id]
                    cn = g.nodes[child_id]
                    if child_id not in pn.children:
                        pn.children.append(child_id)
                    if parent_id not in cn.parents:
                        cn.parents.append(parent_id)

        g._pg_conn = conn
        return g

    def save_pg(self) -> int:
        """Upsert dirty nodes and edges to Postgres. Returns count of upserted nodes."""
        if not self._dirty_ids:
            return 0
        conn = self._connect()
        upserted = 0
        with conn.cursor() as cur:
            for nid in list(self._dirty_ids):
                node = self.nodes.get(nid)
                if node is None:
                    continue  # deleted during session
                import datetime

                created_dt = datetime.datetime.fromtimestamp(
                    node.created_at, tz=datetime.timezone.utc
                )
                accessed_dt = datetime.datetime.fromtimestamp(
                    node.last_accessed, tz=datetime.timezone.utc
                )
                cur.execute(
                    """
                    INSERT INTO soul_nodes
                      (id, content, tags, parents, pinned, score,
                       created_at, updated_at, last_accessed, access_count,
                       metadata, agent)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                      content     = EXCLUDED.content,
                      tags        = EXCLUDED.tags,
                      parents     = EXCLUDED.parents,
                      pinned      = EXCLUDED.pinned,
                      score       = EXCLUDED.score,
                      updated_at  = now(),
                      last_accessed = EXCLUDED.last_accessed,
                      access_count  = EXCLUDED.access_count,
                      metadata    = EXCLUDED.metadata
                    """,
                    (
                        node.id,
                        node.content,
                        list(node.tags),
                        node.parents,
                        node.pinned,
                        node.recency_score(),
                        created_dt,
                        accessed_dt,
                        node.access_count,
                        json.dumps(node.metadata),
                        self._agent,
                    ),
                )
                upserted += 1

                # Upsert edges for this node's parents
                for pid in node.parents:
                    if pid in self.nodes:
                        cur.execute(
                            """
                            INSERT INTO soul_edges (parent_id, child_id, relation)
                            VALUES (%s, %s, 'child')
                            ON CONFLICT (parent_id, child_id, relation) DO NOTHING
                            """,
                            (pid, node.id),
                        )
        conn.commit()
        self._dirty_ids.clear()
        return upserted

    def save(self, path=None) -> None:  # type: ignore[override]
        """Override save() — ignore path, write to Postgres instead.

        Also writes a JSON backup alongside path if provided (belt+suspenders).
        """
        self.save_pg()
        if path is not None:
            # Keep a JSON backup so the file-based sidecar can still read it
            try:
                super().save(path)
            except Exception:
                pass  # Postgres is authoritative; JSON is best-effort backup
