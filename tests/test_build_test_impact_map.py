"""Unit tests for scripts/build-test-impact-map.py.

We synthesise a coverage.py SQLite data file with the documented schema so the
builder is exercised end-to-end without a real (multi-minute) portfolio run.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(script_name: str, module_name: str):
    path = ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load("build-test-impact-map.py", "mac_build_test_impact_map")


def _write_coverage_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE meta(key TEXT, value TEXT);
            CREATE TABLE file(id INTEGER PRIMARY KEY, path TEXT);
            CREATE TABLE context(id INTEGER PRIMARY KEY, context TEXT);
            CREATE TABLE arc(file_id INTEGER, context_id INTEGER, fromno INTEGER, tono INTEGER);
            """
        )
        conn.execute("INSERT INTO meta VALUES('has_arcs','1')")
        conn.execute("INSERT INTO file VALUES(1,'src/mac/foo.py')")
        conn.execute("INSERT INTO file VALUES(2,'tests/test_foo.py')")
        conn.execute(
            "INSERT INTO context VALUES(1,'test|tests/test_foo.py::test_a')"
        )
        conn.execute(
            "INSERT INTO context VALUES(2,'test|tests/test_foo.py::test_b')"
        )
        # Session/unattributed context (no test| prefix).
        conn.execute("INSERT INTO context VALUES(3,'')")
        # test_a executes src/mac/foo.py lines 10,11,12.
        conn.execute("INSERT INTO arc VALUES(1,1,10,11)")
        conn.execute("INSERT INTO arc VALUES(1,1,11,12)")
        # test_b executes src/mac/foo.py line 12 only.
        conn.execute("INSERT INTO arc VALUES(1,2,-1,12)")
        # test_a also touches a NON-source file; must be filtered out.
        conn.execute("INSERT INTO arc VALUES(2,1,3,4)")
        # Unattributed session arc.
        conn.execute("INSERT INTO arc VALUES(1,3,20,21)")
        conn.commit()
    finally:
        conn.close()


def _write_timings(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "mac.test_portfolio_timings.v1",
                "exitstatus": 0,
                "tests": [
                    {"nodeid": "tests/test_foo.py::test_a"},
                    {"nodeid": "tests/test_foo.py::test_b"},
                    # Ran but left no attributed coverage -> always_run.
                    {"nodeid": "tests/test_fleet_subprocess.py::test_spawns"},
                ],
            }
        ),
        encoding="utf-8",
    )


def _build(tmp_path: Path) -> dict:
    db = tmp_path / ".coverage"
    timings = tmp_path / "timings.json"
    _write_coverage_db(db)
    _write_timings(timings)
    return BUILDER.build_map(db, timings, repo_root=tmp_path)


def _tests_for(document: dict, key: str, filename: str) -> set[str]:
    nodeids = document["nodeids"]
    return {nodeids[i] for i in document[key].get(filename, [])}


def test_schema_and_provenance(tmp_path):
    document = _build(tmp_path)
    assert document["schema"] == "mac.test_impact_map.v1"
    assert document["source_prefix"] == "src/"
    assert "base_sha" in document


def test_file_level_index_maps_only_source_files(tmp_path):
    document = _build(tmp_path)
    assert set(document["file_tests"]) == {"src/mac/foo.py"}
    mapped = _tests_for(document, "file_tests", "src/mac/foo.py")
    assert mapped == {
        "tests/test_foo.py::test_a",
        "tests/test_foo.py::test_b",
    }


def test_line_level_index_is_precise(tmp_path):
    document = _build(tmp_path)
    lines = document["file_line_tests"]["src/mac/foo.py"]
    nodeids = document["nodeids"]

    def at(line: str) -> set[str]:
        return {nodeids[i] for i in lines.get(line, [])}

    assert at("10") == {"tests/test_foo.py::test_a"}
    assert at("11") == {"tests/test_foo.py::test_a"}
    # Line 12 is executed by both tests.
    assert at("12") == {"tests/test_foo.py::test_a", "tests/test_foo.py::test_b"}


def test_unattributed_test_becomes_always_run(tmp_path):
    document = _build(tmp_path)
    assert "tests/test_fleet_subprocess.py" in document["always_run"]
    # Attributed tests are NOT force-run.
    assert "tests/test_foo.py" not in document["always_run"]
    assert document["stats"]["unattributed_tests"] == 1


def test_high_fanout_lines_are_pruned_but_file_level_is_retained(tmp_path):
    """A cap drops the least-selective (highest-fanout) line entries to keep the
    committed artifact small, but never touches file_tests — so a change to a
    pruned line still resolves to every test that executed the file."""
    db = tmp_path / ".coverage"
    timings = tmp_path / "timings.json"
    _write_coverage_db(db)
    _write_timings(timings)

    # Cap at 1: line 12 (executed by BOTH tests) is pruned; lines 10 and 11
    # (single test) survive.
    document = BUILDER.build_map(db, timings, repo_root=tmp_path, max_line_fanout=1)
    lines = document["file_line_tests"]["src/mac/foo.py"]
    assert "12" not in lines
    assert set(lines) == {"10", "11"}
    # File-level index is unchanged: both tests remain selectable for the file.
    assert _tests_for(document, "file_tests", "src/mac/foo.py") == {
        "tests/test_foo.py::test_a",
        "tests/test_foo.py::test_b",
    }
    assert document["stats"]["line_fanout_cap"] == 1
    assert document["stats"]["pruned_high_fanout_lines"] == 1


def test_nonpositive_cap_keeps_every_line(tmp_path):
    db = tmp_path / ".coverage"
    timings = tmp_path / "timings.json"
    _write_coverage_db(db)
    _write_timings(timings)

    document = BUILDER.build_map(db, timings, repo_root=tmp_path, max_line_fanout=0)
    assert set(document["file_line_tests"]["src/mac/foo.py"]) == {"10", "11", "12"}
    assert document["stats"]["pruned_high_fanout_lines"] == 0


def test_output_is_written_compact(tmp_path):
    """The artifact must be emitted without indentation: pretty-printing roughly
    tripled the committed size for no machine-read benefit."""
    db = tmp_path / ".coverage"
    timings = tmp_path / "timings.json"
    _write_coverage_db(db)
    _write_timings(timings)
    out = tmp_path / "map.json"
    rc = BUILDER.main(
        ["--coverage-file", str(db), "--timings", str(timings), "--output", str(out)]
    )
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    # Compact separators leave no ", " or ": " spacing and no newline indentation.
    assert ", " not in text and ": " not in text
    assert "\n" not in text.rstrip("\n")


def test_missing_coverage_file_is_a_clean_error(tmp_path):
    rc = BUILDER.main(
        [
            "--coverage-file",
            str(tmp_path / "nope.coverage"),
            "--output",
            str(tmp_path / "out.json"),
        ]
    )
    assert rc == 2
