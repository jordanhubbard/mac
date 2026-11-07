#!/usr/bin/env python3
"""memory_gardener.py — daily maintenance pass for fleet agent graph memory.

Backends:
  SQLite (default): pass --db PATH
  Postgres (fleet_brain): pass --db-url postgresql:///fleet_brain --agent <id>

Run as a cron or manually:
  python3 gardener.py                                     # SQLite default
  python3 gardener.py --db-url postgresql:///fleet_brain --agent rocky
"""

import sys, json, re, math
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg

DB_PATH           = Path(__file__).parent / "memory_graph.db"
ARCHIVE_THRESHOLD = 0.05   # nodes below this get archived
PROMOTION_MIN_ACCESS = 5   # accessed this many times → suggest pinning
STOPWORDS = mg._STOPWORDS


def run(db_path=None, db_url: str | None = None, agent_id: str | None = None):
    """Run all gardener passes. Returns the report dict.

    When db_url is supplied the Postgres fleet_brain backend is used;
    db_path is ignored.
    """
    kw = {"db_url": db_url, "agent_id": agent_id} if db_url else {"db_path": db_path or DB_PATH}
    use_pg = bool(db_url)

    if not use_pg:
        mg.init_db(db_path or DB_PATH)

    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "backend": "postgres" if use_pg else "sqlite", "passes": {}}

    # ── Pass 1: Decay ──────────────────────────────────────────────────────────
    if use_pg:
        result = mg.decay_all(db_url=db_url, agent_id=agent_id)
    else:
        result = mg.decay_all(db_path=db_path or DB_PATH)
    report["passes"]["decay"] = result
    print(f"[decay] {result['decayed']} nodes decayed, {result['skipped_pinned']} pinned/axiom skipped")

    # ── Pass 2: Archive / flag low-score nodes ─────────────────────────────────
    if use_pg:
        archived = _pg_archive(db_url, agent_id, ARCHIVE_THRESHOLD)
        report["passes"]["archive"] = archived
        print(f"[archive] {archived['newly_archived']} nodes tagged 'archived' in Postgres")
    else:
        archived = _sqlite_archive(db_path or DB_PATH, ARCHIVE_THRESHOLD)
        report["passes"]["archive"] = archived
        print(f"[archive] {archived['candidates']} below threshold, {archived['newly_archived']} newly archived")

    # ── Pass 3: Isolated node report ───────────────────────────────────────────
    isolated_info = _isolated(db_url, agent_id, db_path or DB_PATH, use_pg)
    report["passes"]["isolated"] = isolated_info
    if isolated_info["count"]:
        print(f"[isolated] {isolated_info['count']} nodes with no edges")
    else:
        print("[isolated] none")

    # ── Pass 4: Near-duplicate detection (SQLite only — PG has pg_trgm) ────────
    if not use_pg:
        dupes = _near_dupes(db_path or DB_PATH)
        report["passes"]["near_dupes"] = dupes
        if dupes:
            print(f"[dupes] {len(dupes)} near-duplicate pairs")
        else:
            print("[dupes] none")
    else:
        # In Postgres use pg_trgm similarity; just count — no full scan needed
        pg_dupes = _pg_near_dupes(db_url, agent_id)
        report["passes"]["near_dupes"] = pg_dupes
        print(f"[dupes] ~{pg_dupes['count']} similar pairs (pg_trgm similarity > 0.7)")

    # ── Pass 5: Axiom promotion candidates ────────────────────────────────────
    if not use_pg:
        candidates = _sqlite_promo_candidates(db_path or DB_PATH, PROMOTION_MIN_ACCESS)
        report["passes"]["promotion_candidates"] = candidates
        if candidates:
            print(f"[promote] {len(candidates)} nodes accessed {PROMOTION_MIN_ACCESS}+ times")
        else:
            print("[promote] no candidates")

    # ── Pass 6: Skill-ref health (SQLite only — PG uses tags array) ───────────
    if not use_pg:
        skill_health = _sqlite_skill_health(db_path or DB_PATH)
        report["passes"]["skill_health"] = skill_health
        print(f"[skills] {skill_health['total_skills']} skill-refs, {skill_health['unlinked']} unlinked")

    # Save report
    report_path = Path(__file__).parent / "gardener_last_run.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport saved: {report_path}")
    return report


# ── Backend-specific helpers ──────────────────────────────────────────────────

def _pg_archive(db_url, agent_id, threshold):
    import psycopg2  # noqa: PLC0415
    conn = mg._pg_connect(db_url)
    try:
        with conn.cursor() as cur:
            # Tag with 'archived' if importance dropped below threshold and not already tagged
            cur.execute("""
                UPDATE memories
                SET tags = array_append(tags, 'archived')
                WHERE agent_id=%s
                  AND importance < %s
                  AND NOT (tags @> ARRAY['kind:axiom'])
                  AND NOT (tags @> ARRAY['archived'])
            """, (agent_id, threshold))
            count = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"newly_archived": count}


def _sqlite_archive(db_path, threshold):
    import sqlite3  # noqa: PLC0415
    con = mg._connect(db_path)
    try:
        low = con.execute(
            "SELECT id, content, score FROM nodes WHERE score < ? AND pinned=0 AND kind != 'axiom'",
            (threshold,)).fetchall()
        archived = 0
        for n in low:
            tags = json.loads(con.execute("SELECT tags FROM nodes WHERE id=?", (n['id'],)).fetchone()['tags'] or '[]')
            if 'archived' not in tags:
                tags.append('archived')
                con.execute("UPDATE nodes SET tags=? WHERE id=?", (json.dumps(tags), n['id']))
                archived += 1
        con.commit()
        return {"candidates": len(low), "newly_archived": archived}
    finally:
        con.close()


def _isolated(db_url, agent_id, db_path, use_pg):
    if use_pg:
        import psycopg2  # noqa: PLC0415
        conn = mg._pg_connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM memories m
                    WHERE m.agent_id=%s
                      AND NOT EXISTS (
                          SELECT 1 FROM soul_edges e
                          WHERE e.agent_id=%s
                            AND (e.from_id=m.id::text OR e.to_id=m.id::text)
                      )
                """, (agent_id, agent_id))
                row = cur.fetchone()
                count = row[0] if row else 0
        finally:
            conn.close()
        return {"count": count, "nodes": []}
    else:
        con = mg._connect(db_path)
        try:
            edge_ids = set()
            for r in con.execute("SELECT from_id, to_id FROM edges").fetchall():
                edge_ids.add(r['from_id']); edge_ids.add(r['to_id'])
            all_nodes = con.execute("SELECT id, content, kind, score, created_at FROM nodes").fetchall()
            isolated = [n for n in all_nodes if n['id'] not in edge_ids]
            return {
                "count": len(isolated),
                "nodes": [{"id": n['id'], "summary": n['content'][:60], "score": n['score']} for n in isolated]
            }
        finally:
            con.close()


def _pg_near_dupes(db_url, agent_id):
    """Count pairs with pg_trgm similarity > 0.7."""
    import psycopg2  # noqa: PLC0415
    conn = mg._pg_connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM (
                    SELECT a.id, b.id,
                           similarity(a.content, b.content) AS sim
                    FROM memories a
                    JOIN memories b ON a.agent_id = b.agent_id
                        AND a.id < b.id
                        AND similarity(a.content, b.content) > 0.7
                    WHERE a.agent_id = %s
                ) sub
            """, (agent_id,))
            row = cur.fetchone()
            count = row[0] if row else 0
    except Exception:
        count = -1  # pg_trgm may need SET pg_trgm.similarity_threshold first
    finally:
        conn.close()
    return {"count": count}


def _near_dupes(db_path):
    con = mg._connect(db_path)
    try:
        nodes_list = list(con.execute("SELECT id, content FROM nodes").fetchall())
    finally:
        con.close()
    dupes = []
    for i, a in enumerate(nodes_list):
        tok_a = {t for t in re.findall(r'\w+', a['content'].lower())
                 if len(t) >= 4 and t not in STOPWORDS}
        for b in nodes_list[i+1:]:
            tok_b = {t for t in re.findall(r'\w+', b['content'].lower())
                     if len(t) >= 4 and t not in STOPWORDS}
            if not tok_a or not tok_b:
                continue
            overlap = len(tok_a & tok_b) / max(len(tok_a), len(tok_b))
            if overlap >= 0.7 and a['id'] != b['id']:
                dupes.append({"a": a['id'], "b": b['id'], "overlap": round(overlap, 2),
                              "a_summary": a['content'][:50], "b_summary": b['content'][:50]})
    return dupes


def _sqlite_promo_candidates(db_path, min_access):
    con = mg._connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, content, access_count, score FROM nodes WHERE pinned=0 AND access_count >= ?",
            (min_access,)).fetchall()
        return [{"id": r['id'], "summary": r['content'][:60], "access_count": r['access_count']} for r in rows]
    finally:
        con.close()


def _sqlite_skill_health(db_path):
    con = mg._connect(db_path)
    try:
        skill_nodes = con.execute("SELECT id, content FROM nodes WHERE kind='skill-ref'").fetchall()
        unlinked = [
            s['id'] for s in skill_nodes
            if con.execute(
                "SELECT COUNT(*) FROM edges WHERE from_id=? OR to_id=?",
                (s['id'], s['id'])).fetchone()[0] == 0
        ]
        return {"total_skills": len(skill_nodes), "unlinked": len(unlinked)}
    finally:
        con.close()


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Memory graph gardener")
    parser.add_argument("--db", type=Path, default=None, help="SQLite path to memory_graph.db")
    parser.add_argument("--db-url", default=None,
                        help="Postgres URL for fleet_brain (e.g. postgresql:///fleet_brain)")
    parser.add_argument("--agent", default=None,
                        help="Agent ID for Postgres mode (e.g. rocky, bullwinkle)")
    args = parser.parse_args()
    run(db_path=args.db, db_url=args.db_url, agent_id=args.agent)
