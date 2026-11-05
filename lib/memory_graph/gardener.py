#!/usr/bin/env python3
"""
memory_gardener.py — daily maintenance pass for Rocky's graph memory.
Run as a cron or manually: python3 gardener.py

Passes:
1. Decay all non-pinned nodes
2. Archive nodes with score < ARCHIVE_THRESHOLD
3. Flag isolated nodes that survived 3+ decay cycles
4. Report near-duplicate candidates (high token overlap, different IDs)
5. Surface axiom promotion candidates (high access, not pinned)
6. Report skill-ref health (skill nodes with no linked memory nodes)
"""

import sys, sqlite3, json, re, math
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent))
import memory_graph as mg

DB_PATH          = Path(__file__).parent / "memory_graph.db"
ARCHIVE_THRESHOLD = 0.05   # nodes below this get archived
PROMOTION_MIN_ACCESS = 5   # accessed this many times → suggest pinning
STOPWORDS = mg._STOPWORDS


def _connect():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def run():
    mg.init_db(DB_PATH)
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "passes": {}}

    # ── Pass 1: Decay ──────────────────────────────────────────────────────────
    result = mg.decay_all(db_path=DB_PATH)
    report["passes"]["decay"] = result
    print(f"[decay] {result['decayed']} nodes decayed, {result['skipped_pinned']} pinned/axiom skipped")

    con = _connect()

    # ── Pass 2: Archive low-score nodes ───────────────────────────────────────
    low = con.execute(
        "SELECT id, content, score FROM nodes WHERE score < ? AND pinned=0 AND kind != 'axiom'",
        (ARCHIVE_THRESHOLD,)).fetchall()
    archived = 0
    for n in low:
        # Mark as archived via tag (don't delete — non-destructive)
        tags = json.loads(con.execute("SELECT tags FROM nodes WHERE id=?", (n['id'],)).fetchone()['tags'] or '[]')
        if 'archived' not in tags:
            tags.append('archived')
            con.execute("UPDATE nodes SET tags=? WHERE id=?", (json.dumps(tags), n['id']))
            archived += 1
    con.commit()
    report["passes"]["archive"] = {"candidates": len(low), "newly_archived": archived}
    print(f"[archive] {len(low)} below threshold, {archived} newly archived")

    # ── Pass 3: Isolated node report ──────────────────────────────────────────
    edge_ids = set()
    for r in con.execute("SELECT from_id, to_id FROM edges").fetchall():
        edge_ids.add(r['from_id']); edge_ids.add(r['to_id'])
    all_nodes = con.execute("SELECT id, content, kind, score, created_at FROM nodes").fetchall()
    isolated = [n for n in all_nodes if n['id'] not in edge_ids]
    report["passes"]["isolated"] = {
        "count": len(isolated),
        "nodes": [{"id": n['id'], "summary": n['content'][:60], "score": n['score']} for n in isolated]
    }
    if isolated:
        print(f"[isolated] {len(isolated)} nodes with no edges:")
        for n in isolated:
            print(f"  [{n['score']:.3f}] {n['content'][:60]}")
    else:
        print(f"[isolated] none")

    # ── Pass 4: Near-duplicate detection ──────────────────────────────────────
    dupes = []
    nodes_list = list(all_nodes)
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
    report["passes"]["near_dupes"] = dupes
    if dupes:
        print(f"[dupes] {len(dupes)} near-duplicate pairs:")
        for d in dupes:
            print(f"  {d['overlap']:.0%} overlap: {d['a_summary']} / {d['b_summary']}")
    else:
        print(f"[dupes] none")

    # ── Pass 5: Axiom promotion candidates ────────────────────────────────────
    candidates = con.execute(
        "SELECT id, content, access_count, score FROM nodes WHERE pinned=0 AND access_count >= ?",
        (PROMOTION_MIN_ACCESS,)).fetchall()
    report["passes"]["promotion_candidates"] = [
        {"id": r['id'], "summary": r['content'][:60], "access_count": r['access_count']}
        for r in candidates]
    if candidates:
        print(f"[promote] {len(candidates)} nodes accessed {PROMOTION_MIN_ACCESS}+ times, consider pinning:")
        for c in candidates:
            print(f"  [{c['access_count']}x] {c['content'][:60]}")
    else:
        print(f"[promote] no candidates")

    # ── Pass 6: Skill-ref health ───────────────────────────────────────────────
    skill_nodes = con.execute("SELECT id, content FROM nodes WHERE kind='skill-ref'").fetchall()
    unlinked_skills = []
    for s in skill_nodes:
        edge_count = con.execute(
            "SELECT COUNT(*) FROM edges WHERE from_id=? OR to_id=?",
            (s['id'], s['id'])).fetchone()[0]
        if edge_count == 0:
            unlinked_skills.append(s['id'])
    report["passes"]["skill_health"] = {
        "total_skills": len(skill_nodes),
        "unlinked": len(unlinked_skills)
    }
    print(f"[skills] {len(skill_nodes)} skill-refs, {len(unlinked_skills)} unlinked")

    con.close()

    # Save report
    report_path = Path(__file__).parent / "gardener_last_run.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport saved: {report_path}")
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Memory graph gardener")
    parser.add_argument("--db", type=Path, default=None, help="Path to memory_graph.db")
    args = parser.parse_args()
    if args.db:
        DB_PATH = args.db.expanduser()
    run()
