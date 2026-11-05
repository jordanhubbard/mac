"""
tests/test_memory_graph.py — tests for lib/memory_graph/memory_graph.py
Runs against a temp in-memory SQLite DB (no disk I/O).
"""
import sys
import os
import tempfile
import pytest
from pathlib import Path

# Add lib to path
sys.path.insert(0, str(Path(__file__).parent.parent / "lib" / "memory_graph"))
import memory_graph as mg


@pytest.fixture
def db(tmp_path):
    """Provide a fresh DB path for each test."""
    p = tmp_path / "test_memory.db"
    mg.init_db(p)
    return p


def test_add_node_returns_id(db):
    nid = mg.add_node("hello world test content", db_path=db)
    assert len(nid) == 16


def test_add_node_dedup(db):
    content = "unique content for dedup test"
    id1 = mg.add_node(content, db_path=db)
    id2 = mg.add_node(content, db_path=db)
    assert id1 == id2


def test_axiom_always_pinned(db):
    nid = mg.add_node("some axiom rule", kind="axiom", pinned=False, db_path=db)
    import sqlite3
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT pinned, kind FROM nodes WHERE id=?", (nid,)).fetchone()
    con.close()
    assert row["pinned"] == 1
    assert row["kind"] == "axiom"


def test_visibility_tag_added(db):
    nid = mg.add_node("some content", visibility="fleet", db_path=db)
    import sqlite3, json
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    tags = json.loads(con.execute("SELECT tags FROM nodes WHERE id=?", (nid,)).fetchone()["tags"])
    con.close()
    assert "visibility:fleet" in tags


def test_auto_link_creates_edges(db):
    # Two nodes with 2+ shared significant tokens should get a related-to edge
    id1 = mg.add_node("memory graph sqlite database storage", db_path=db)
    id2 = mg.add_node("memory graph qdrant vector database", db_path=db)
    import sqlite3
    con = sqlite3.connect(str(db))
    count = con.execute(
        "SELECT COUNT(*) FROM edges WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=?)",
        (id1, id2, id2, id1)).fetchone()[0]
    con.close()
    assert count >= 1


def test_no_self_link(db):
    nid = mg.add_node("memory graph sqlite storage unique", db_path=db)
    import sqlite3
    con = sqlite3.connect(str(db))
    count = con.execute(
        "SELECT COUNT(*) FROM edges WHERE from_id=? AND to_id=?",
        (nid, nid)).fetchone()[0]
    con.close()
    assert count == 0


def test_access_reinforces_score(db):
    nid = mg.add_node("some content to access reinforce", db_path=db)
    import sqlite3
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    before = con.execute("SELECT score FROM nodes WHERE id=?", (nid,)).fetchone()["score"]
    con.close()
    mg.access(nid, db_path=db)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    after = con.execute("SELECT score FROM nodes WHERE id=?", (nid,)).fetchone()["score"]
    con.close()
    assert after > before


def test_decay_skips_pinned(db):
    pinned_id = mg.add_node("pinned axiom content", kind="axiom", db_path=db)
    unpinned_id = mg.add_node("unpinned memory content", kind="memory", db_path=db)
    import sqlite3
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    before_pinned = con.execute("SELECT score FROM nodes WHERE id=?", (pinned_id,)).fetchone()["score"]
    before_unpinned = con.execute("SELECT score FROM nodes WHERE id=?", (unpinned_id,)).fetchone()["score"]
    con.close()
    mg.decay_all(db_path=db)
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    after_pinned = con.execute("SELECT score FROM nodes WHERE id=?", (pinned_id,)).fetchone()["score"]
    after_unpinned = con.execute("SELECT score FROM nodes WHERE id=?", (unpinned_id,)).fetchone()["score"]
    con.close()
    assert abs(after_pinned - before_pinned) < 0.001  # unchanged
    assert after_unpinned < before_unpinned  # decayed


def test_search_finds_matching_nodes(db):
    mg.add_node("slack home channel rockyandfriends", db_path=db)
    mg.add_node("completely unrelated content here", db_path=db)
    results = mg.search("slack channel", db_path=db)
    assert len(results) >= 1
    assert any("slack" in r["content"].lower() for r in results)


def test_top_returns_highest_score(db):
    mg.add_node("low score content alpha", db_path=db)
    high_id = mg.add_node("high score content beta", db_path=db)
    # Reinforce high node several times
    for _ in range(5):
        mg.access(high_id, db_path=db)
    top = mg.top(1, db_path=db)
    assert top[0]["id"] == high_id


def test_fisheye_hop1(db):
    id1 = mg.add_node("focal node alpha content", db_path=db)
    id2 = mg.add_node("neighbor node beta content", db_path=db)
    mg.add_edge(id1, id2, relation="cites", db_path=db)
    view = mg.fisheye(id1, db_path=db)
    assert view["focal"]["id"] == id1
    assert any(n["id"] == id2 for n in view["hop1"])


def test_fisheye_hop2_counts(db):
    id1 = mg.add_node("hub alpha primary focal", auto_link=False, db_path=db)
    id2 = mg.add_node("bridge beta secondary middle", auto_link=False, db_path=db)
    id3 = mg.add_node("peripheral gamma tertiary distal", auto_link=False, db_path=db)
    mg.add_edge(id1, id2, relation="cites", db_path=db)
    mg.add_edge(id2, id3, relation="refines", db_path=db)
    view = mg.fisheye(id1, db_path=db)
    # id3 should appear in hop2 counts, not hop1
    hop1_ids = {n["id"] for n in view["hop1"]}
    assert id3 not in hop1_ids
    assert sum(view["hop2"].values()) >= 1


def test_add_edge_invalid_relation(db):
    id1 = mg.add_node("first node content test", db_path=db)
    id2 = mg.add_node("second node content test", db_path=db)
    with pytest.raises(ValueError):
        mg.add_edge(id1, id2, relation="invalid-relation", db_path=db)


def test_node_count(db):
    mg.add_node("counting node one", db_path=db)
    mg.add_node("counting node two", db_path=db)
    assert mg.node_count(db_path=db) >= 2


def test_pinned_count(db):
    mg.add_node("axiom one content", kind="axiom", db_path=db)
    mg.add_node("memory one content", kind="memory", db_path=db)
    assert mg.pinned_count(db_path=db) >= 1
