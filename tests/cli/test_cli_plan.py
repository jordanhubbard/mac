"""CLI coverage tests for ``mac plan`` subcommands.

Exercises ``mac plan order`` via the standard ``_run(tmp_path, ...)``
helper so the CLI subcommand coverage gate registers the command as tested.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from mac.cli import main


def _run(tmp_path: Path, *args):
    """Run ``mac --db <tmp>/mac.db <args>`` and return (rc, parsed_output)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", str(tmp_path / "mac.db"), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def _make_synthetic_codegraph_db(repo_dir: Path) -> None:
    """Create a minimal .codegraph/codegraph.db with four files.

    Import topology:
        leaf_a.py ─┐
        leaf_b.py ─┤─> middle.py ─> core.py
    """
    cg_dir = repo_dir / ".codegraph"
    cg_dir.mkdir(parents=True, exist_ok=True)
    db_path = cg_dir / "codegraph.db"

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
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
    for fname in ("leaf_a.py", "leaf_b.py", "middle.py", "core.py"):
        cur.execute(
            "INSERT INTO files VALUES (?, 'abc', 'python', 100, 0, 0, 1)", (fname,)
        )
        cur.execute(
            "INSERT INTO nodes VALUES (?, 'file', ?, ?, ?, 'python', 1, 1, 0, 0)",
            ("file:" + fname, fname, fname, fname),
        )
    # Function nodes for import targets
    for fname, nid in [
        ("leaf_a.py", "function:leaf_a_func"),
        ("leaf_b.py", "function:leaf_b_func"),
        ("middle.py", "function:middle_func"),
        ("core.py", "function:core_func"),
    ]:
        cur.execute(
            "INSERT INTO nodes VALUES (?, 'function', ?, ?, ?, 'python', 2, 5, 0, 0)",
            (nid, nid.split(":")[1], nid.split(":")[1], fname),
        )
    # Import edges
    cur.execute(
        "INSERT INTO edges(source, target, kind, line) VALUES (?, ?, 'imports', 1)",
        ("file:middle.py", "function:leaf_a_func"),
    )
    cur.execute(
        "INSERT INTO edges(source, target, kind, line) VALUES (?, ?, 'imports', 2)",
        ("file:middle.py", "function:leaf_b_func"),
    )
    cur.execute(
        "INSERT INTO edges(source, target, kind, line) VALUES (?, ?, 'imports', 1)",
        ("file:core.py", "function:middle_func"),
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_plan_order_leaf_first(tmp_path):
    """mac plan order returns leaf-first layers by default."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_synthetic_codegraph_db(repo)

    rc, result = _run(
        tmp_path,
        "plan",
        "order",
        "leaf_a.py",
        "leaf_b.py",
        "middle.py",
        "core.py",
        "--repo",
        str(repo),
    )
    assert rc == 0
    assert result is not None
    assert result["schema"] == "mac.planning.v1"
    assert result["mode"] == "leaf-first"
    layer_map = {layer["layer"]: sorted(layer["files"]) for layer in result["layers"]}
    assert layer_map[0] == ["leaf_a.py", "leaf_b.py"]
    assert layer_map[1] == ["middle.py"]
    assert layer_map[2] == ["core.py"]


def test_cli_plan_order_core_first(tmp_path):
    """mac plan order --core-first reverses to core-first layers."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_synthetic_codegraph_db(repo)

    rc, result = _run(
        tmp_path,
        "plan",
        "order",
        "leaf_a.py",
        "leaf_b.py",
        "middle.py",
        "core.py",
        "--repo",
        str(repo),
        "--core-first",
    )
    assert rc == 0
    assert result is not None
    assert result["mode"] == "core-first"
    layer_map = {layer["layer"]: sorted(layer["files"]) for layer in result["layers"]}
    assert layer_map[0] == ["core.py"]
    assert layer_map[2] == ["leaf_a.py", "leaf_b.py"]


def test_cli_plan_order_no_db_returns_unknown(tmp_path):
    """mac plan order without a .codegraph DB puts all files in unknown."""
    repo = tmp_path / "repo"
    repo.mkdir()

    rc, result = _run(
        tmp_path,
        "plan",
        "order",
        "a.py",
        "b.py",
        "--repo",
        str(repo),
    )
    assert rc == 0
    assert result is not None
    assert sorted(result["unknown"]) == ["a.py", "b.py"]
