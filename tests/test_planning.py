"""Tests for mac.planning – topology ordering primitive.

Uses a small synthetic graph (4 files in a diamond shape) to verify:
  - leaf-first ordering (default)
  - core-first ordering
  - blast_radius calculation
  - files not in the index land in .unknown
  - no .codegraph → single unknown layer

The CLI round-trip for ``mac plan order`` is covered by the dedicated
``tests/cli/test_cli_plan.py``; this module tests the ordering primitive.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mac.planning import Layer, OrderResult, PLANNING_SCHEMA, blast_radius, order_layers


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_codegraph_db(tmp_path: Path) -> Path:
    """Create a minimal codegraph DB with a synthetic import graph.

    Graph (file-level imports, edges point "depends on"):
        leaf_a.py  ─┐
        leaf_b.py  ─┤─> middle.py ─> core.py
                    └─────────────>

    leaf_a and leaf_b are pure leaves.
    middle imports leaf_a and leaf_b.
    core imports middle.

    Blast radius of leaf_a: {middle.py, core.py}
    Blast radius of middle: {core.py}

    Leaf-first layers:
        layer 0: [leaf_a.py, leaf_b.py]
        layer 1: [middle.py]
        layer 2: [core.py]

    Core-first layers (reversed):
        layer 0: [core.py]
        layer 1: [middle.py]
        layer 2: [leaf_a.py, leaf_b.py]
    """
    cg_dir = tmp_path / ".codegraph"
    cg_dir.mkdir()
    db_path = cg_dir / "codegraph.db"

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    # Minimal schema matching codegraph's real schema
    cur.executescript(
        """
        CREATE TABLE files (
            path TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            language TEXT NOT NULL,
            size INTEGER NOT NULL,
            modified_at INTEGER NOT NULL,
            indexed_at INTEGER NOT NULL,
            node_count INTEGER DEFAULT 0
        );
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            language TEXT NOT NULL,
            start_line INTEGER NOT NULL DEFAULT 1,
            end_line INTEGER NOT NULL DEFAULT 1,
            start_column INTEGER NOT NULL DEFAULT 0,
            end_column INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            kind TEXT NOT NULL,
            line INTEGER
        );
        """
    )

    # Files
    for fname in ("leaf_a.py", "leaf_b.py", "middle.py", "core.py"):
        cur.execute(
            "INSERT INTO files VALUES (?, 'abc', 'python', 100, 0, 0, 1)",
            (fname,),
        )

    # File nodes
    for fname in ("leaf_a.py", "leaf_b.py", "middle.py", "core.py"):
        node_id = "file:" + fname
        cur.execute(
            "INSERT INTO nodes VALUES (?, 'file', ?, ?, ?, 'python', 1, 1, 0, 0)",
            (node_id, fname, fname, fname),
        )

    # Function nodes (needed so 'imports' edges can reference them)
    funcs = {
        "leaf_a.py": "function:leaf_a_func",
        "leaf_b.py": "function:leaf_b_func",
        "middle.py": "function:middle_func",
        "core.py": "function:core_func",
    }
    for fpath, node_id in funcs.items():
        cur.execute(
            "INSERT INTO nodes VALUES (?, 'function', ?, ?, ?, 'python', 2, 5, 0, 0)",
            (node_id, node_id.split(":")[1], node_id.split(":")[1], fpath),
        )

    # Import edges: file:X --imports--> function:Y (target lives in another file)
    # middle imports leaf_a and leaf_b
    cur.execute(
        "INSERT INTO edges(source, target, kind, line) VALUES (?, ?, 'imports', 1)",
        ("file:middle.py", "function:leaf_a_func"),
    )
    cur.execute(
        "INSERT INTO edges(source, target, kind, line) VALUES (?, ?, 'imports', 2)",
        ("file:middle.py", "function:leaf_b_func"),
    )
    # core imports middle
    cur.execute(
        "INSERT INTO edges(source, target, kind, line) VALUES (?, ?, 'imports', 1)",
        ("file:core.py", "function:middle_func"),
    )

    con.commit()
    con.close()
    return db_path


# ---------------------------------------------------------------------------
# Tests: order_layers – leaf-first (default)
# ---------------------------------------------------------------------------


def test_order_layers_leaf_first_basic(tmp_path):
    _make_codegraph_db(tmp_path)
    paths = ["leaf_a.py", "leaf_b.py", "middle.py", "core.py"]
    result = order_layers(paths, repo_root=tmp_path)

    assert isinstance(result, OrderResult)
    assert result.schema == PLANNING_SCHEMA
    assert result.mode == "leaf-first"
    assert result.unknown == []

    # Three layers expected
    assert len(result.layers) == 3

    layer_map = {layer.layer: sorted(layer.files) for layer in result.layers}
    # Layer 0: pure leaves
    assert layer_map[0] == ["leaf_a.py", "leaf_b.py"]
    # Layer 1: middle (depends on leaves)
    assert layer_map[1] == ["middle.py"]
    # Layer 2: core (depends on middle)
    assert layer_map[2] == ["core.py"]


def test_order_layers_leaf_first_subset(tmp_path):
    _make_codegraph_db(tmp_path)
    # Only ask about middle + core; leaves are not in the request
    result = order_layers(["middle.py", "core.py"], repo_root=tmp_path)

    assert result.unknown == []
    layer_files = [sorted(layer.files) for layer in result.layers]
    # middle is a leaf in this sub-graph
    assert layer_files[0] == ["middle.py"]
    assert layer_files[1] == ["core.py"]


def test_order_layers_independent_files_same_layer(tmp_path):
    _make_codegraph_db(tmp_path)
    # leaf_a and leaf_b don't import each other → same layer
    result = order_layers(["leaf_a.py", "leaf_b.py"], repo_root=tmp_path)

    assert len(result.layers) == 1
    assert sorted(result.layers[0].files) == ["leaf_a.py", "leaf_b.py"]


# ---------------------------------------------------------------------------
# Tests: order_layers – core-first
# ---------------------------------------------------------------------------


def test_order_layers_core_first_reversal(tmp_path):
    _make_codegraph_db(tmp_path)
    paths = ["leaf_a.py", "leaf_b.py", "middle.py", "core.py"]
    result = order_layers(paths, repo_root=tmp_path, mode="core-first")

    assert result.mode == "core-first"
    assert len(result.layers) == 3

    layer_map = {layer.layer: sorted(layer.files) for layer in result.layers}
    # Core-first: layer 0 = core
    assert layer_map[0] == ["core.py"]
    # Layer 1 = middle
    assert layer_map[1] == ["middle.py"]
    # Layer 2 = leaves
    assert layer_map[2] == ["leaf_a.py", "leaf_b.py"]


# ---------------------------------------------------------------------------
# Tests: unknown files
# ---------------------------------------------------------------------------


def test_order_layers_unknown_files_not_in_index(tmp_path):
    _make_codegraph_db(tmp_path)
    paths = ["leaf_a.py", "not_indexed.py"]
    result = order_layers(paths, repo_root=tmp_path)

    assert "not_indexed.py" in result.unknown
    # leaf_a should still appear in layers (it's in the index)
    indexed_files = [f for layer in result.layers for f in layer.files]
    assert "leaf_a.py" in indexed_files
    assert "not_indexed.py" not in indexed_files


def test_order_layers_no_codegraph_db(tmp_path):
    # No .codegraph directory at all → single unknown layer
    result = order_layers(["a.py", "b.py"], repo_root=tmp_path)

    assert sorted(result.unknown) == ["a.py", "b.py"]
    assert len(result.layers) == 1
    assert sorted(result.layers[0].files) == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# Tests: blast_radius
# ---------------------------------------------------------------------------


def test_blast_radius_leaf_a(tmp_path):
    _make_codegraph_db(tmp_path)
    affected = blast_radius("leaf_a.py", repo_root=tmp_path)

    # middle imports leaf_a, core imports middle → both are affected
    assert "middle.py" in affected
    assert "core.py" in affected
    # leaf_a itself should NOT be in its own blast radius
    assert "leaf_a.py" not in affected


def test_blast_radius_middle(tmp_path):
    _make_codegraph_db(tmp_path)
    affected = blast_radius("middle.py", repo_root=tmp_path)

    assert "core.py" in affected
    assert "middle.py" not in affected
    assert "leaf_a.py" not in affected


def test_blast_radius_core(tmp_path):
    _make_codegraph_db(tmp_path)
    # core is the top; nothing imports it
    affected = blast_radius("core.py", repo_root=tmp_path)
    assert affected == []


def test_blast_radius_no_db(tmp_path):
    affected = blast_radius("anything.py", repo_root=tmp_path)
    assert affected == []


# ---------------------------------------------------------------------------
# Tests: to_dict serialization
# ---------------------------------------------------------------------------


def test_order_result_to_dict_schema(tmp_path):
    _make_codegraph_db(tmp_path)
    result = order_layers(["leaf_a.py", "core.py"], repo_root=tmp_path)
    d = result.to_dict()

    assert d["schema"] == PLANNING_SCHEMA
    assert d["mode"] == "leaf-first"
    assert isinstance(d["layers"], list)
    assert isinstance(d["unknown"], list)
    for layer in d["layers"]:
        assert "layer" in layer
        assert "files" in layer
        assert isinstance(layer["files"], list)


def test_layer_to_dict_sorted(tmp_path):
    layer = Layer(layer=1, files=["z.py", "a.py", "m.py"])
    d = layer.to_dict()
    assert d["layer"] == 1
    assert d["files"] == ["a.py", "m.py", "z.py"]


# ---------------------------------------------------------------------------
# Tests: invalid mode
# ---------------------------------------------------------------------------


def test_order_layers_invalid_mode(tmp_path):
    with pytest.raises(ValueError, match="mode must be"):
        order_layers(["a.py"], repo_root=tmp_path, mode="random")


# ---------------------------------------------------------------------------
# Tests: empty input
# ---------------------------------------------------------------------------


def test_order_layers_empty_paths(tmp_path):
    result = order_layers([], repo_root=tmp_path)
    assert result.layers == []
    assert result.unknown == []


# ---------------------------------------------------------------------------
# Tests: deduplication
# ---------------------------------------------------------------------------


def test_order_layers_deduplicates_paths(tmp_path):
    _make_codegraph_db(tmp_path)
    result = order_layers(["leaf_a.py", "leaf_a.py", "leaf_a.py"], repo_root=tmp_path)
    all_files = [f for layer in result.layers for f in layer.files]
    assert all_files.count("leaf_a.py") == 1


