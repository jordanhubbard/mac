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
        conn.execute("INSERT INTO context VALUES(1,'test|tests/test_foo.py::test_a')")
        conn.execute("INSERT INTO context VALUES(2,'test|tests/test_foo.py::test_b')")
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
    rc = BUILDER.main(["--coverage-file", str(db), "--timings", str(timings), "--output", str(out)])
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


def _stale_document() -> dict:
    return {
        "schema": "mac.test_impact_map.v1",
        "generated_by": "scripts/build-test-impact-map.py",
        "base_sha": "abc",
        "source_prefix": "src/",
        "nodeids": [
            "tests/test_foo.py::test_live",
            "tests/test_foo.py::test_gone",
            "tests/test_bar.py::test_other",
        ],
        "file_tests": {"src/mac/foo.py": [0, 1], "src/mac/bar.py": [2]},
        "file_line_tests": {
            "src/mac/foo.py": {"10": [0], "11": [0, 1]},
            "src/mac/bar.py": {"5": [2]},
        },
        "file_scope_tests": {
            "src/mac/foo.py": {"build": [0, 1]},
            "src/mac/bar.py": {"other": [2]},
        },
        "file_hashes": {"src/mac/foo.py": "sha256:aa", "src/mac/bar.py": "sha256:bb"},
        "always_run": ["tests/test_missing.py", "tests/test_foo.py"],
        "stats": {"interned_nodeids": 99, "mapped_files": 2, "mapped_scopes": 2},
    }


def test_prune_drops_uncollectable_ids_and_compacts_indices():
    """Retiring a test must not require a 45-minute coverage rebuild.

    The intern table is identity: remaining ids keep their relative order,
    and every integer index is rewritten. base_sha stays the coverage origin.
    """
    collectable = {
        "tests/test_foo.py::test_live",
        "tests/test_bar.py::test_other",
    }
    pruned = BUILDER.prune_uncollectable(_stale_document(), collectable, repo_root=None)

    assert pruned["nodeids"] == [
        "tests/test_foo.py::test_live",
        "tests/test_bar.py::test_other",
    ]
    assert pruned["file_tests"] == {
        "src/mac/foo.py": [0],
        "src/mac/bar.py": [1],
    }
    assert pruned["file_line_tests"]["src/mac/foo.py"]["10"] == [0]
    assert pruned["file_line_tests"]["src/mac/foo.py"]["11"] == [0]
    assert pruned["file_line_tests"]["src/mac/bar.py"]["5"] == [1]
    assert pruned["file_scope_tests"]["src/mac/foo.py"]["build"] == [0]
    assert pruned["file_scope_tests"]["src/mac/bar.py"]["other"] == [1]
    assert pruned["file_hashes"]["src/mac/foo.py"] == "sha256:aa"
    assert pruned["stats"]["interned_nodeids"] == 2
    assert pruned["stats"]["pruned_uncollectable_nodeids"] == 1
    assert pruned["base_sha"] == "abc"


def test_prune_drops_always_run_files_that_no_longer_exist(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    pruned = BUILDER.prune_uncollectable(
        _stale_document(),
        set(_stale_document()["nodeids"]),
        repo_root=tmp_path,
    )
    assert pruned["always_run"] == ["tests/test_foo.py"]


def _seed_collectable_repo(tmp_path: Path) -> Path:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_foo.py").write_text("def test_live():\n    assert True\n", encoding="utf-8")
    return tmp_path


def test_check_fails_on_stale_interned_ids(tmp_path):
    repo = _seed_collectable_repo(tmp_path)
    out = repo / "map.json"
    out.write_text(json.dumps(_stale_document()), encoding="utf-8")
    rc = BUILDER.main(
        [
            "--check",
            "--repo-root",
            str(repo),
            "--output",
            str(out),
            "--coverage-file",
            str(repo / "nope.coverage"),
        ]
    )
    assert rc == 1


def test_write_prunes_without_a_coverage_run(tmp_path):
    repo = _seed_collectable_repo(tmp_path)
    out = repo / "map.json"
    out.write_text(json.dumps(_stale_document()), encoding="utf-8")
    rc = BUILDER.main(
        [
            "--write",
            "--repo-root",
            str(repo),
            "--output",
            str(out),
            "--coverage-file",
            str(repo / "nope.coverage"),
        ]
    )
    assert rc == 0
    updated = json.loads(out.read_text(encoding="utf-8"))
    assert updated["nodeids"] == ["tests/test_foo.py::test_live"]
    assert updated["file_tests"]["src/mac/foo.py"] == [0]
    assert "src/mac/bar.py" not in updated["file_tests"]
    assert updated["stats"]["interned_nodeids"] == 1
    assert updated["stats"]["pruned_uncollectable_nodeids"] == 2
    assert updated["always_run"] == ["tests/test_foo.py"]


def test_committed_impact_map_has_no_stale_node_ids():
    """The map must not reference tests that no longer exist.

    The resolver tolerates stale entries now, but tolerating them silently
    lets the map rot until selection covers nothing real.
    """
    import collections
    import json
    import re

    root = Path(__file__).resolve().parents[1]
    have = collections.defaultdict(set)
    for path in (root / "tests").rglob("*.py"):
        for name in re.findall(
            r"^\s*(?:async\s+)?def (test_\w+)", path.read_text(encoding="utf-8"), re.M
        ):
            have[str(path.relative_to(root))].add(name)

    ids = set()

    def walk(node):
        if isinstance(node, str):
            if node.startswith("tests/") and "::" in node:
                ids.add(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(key)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads((root / "src" / "mac" / "data" / "test_impact_map.json").read_text()))
    stale = sorted(
        nodeid
        for nodeid in ids
        if nodeid.partition("::")[2].split("[")[0].split("::")[-1]
        not in have.get(nodeid.partition("::")[0], set())
    )
    assert not stale, "stale node ids in test_impact_map.json: %s" % stale[:10]


def test_committed_impact_map_interned_count_matches_the_table():
    """stats.interned_nodeids is defined as len(nodeids). Gate that.

    Hand-remaps kept decrementing stats in generator-space and the gap
    accumulated (task_72270e63). A prune recomputes the field; this test
    makes a wrong committed value visible without a coverage rebuild.
    """
    import json

    document = json.loads(
        (
            Path(__file__).resolve().parents[1] / "src" / "mac" / "data" / "test_impact_map.json"
        ).read_text(encoding="utf-8")
    )
    assert document["stats"]["interned_nodeids"] == len(document["nodeids"])


# ---------------------------------------------------------------------------
# The scope index: per qualified function/class name, aggregated BEFORE the
# fanout prune. It exists because the line index answered for neither of the
# two cases that actually cost CI time -- a file whose lines had drifted since
# the map was built, and a line executed by so many tests it was pruned.
# ---------------------------------------------------------------------------


def _write_source_file(tmp_path: Path) -> None:
    """A source file whose line numbers match the synthetic coverage arcs:
    lines 10-12 fall inside `build_parser`."""
    target = tmp_path / "src" / "mac" / "foo.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                "import os",  # 1
                "",  # 2
                "",  # 3
                "def other():",  # 4
                "    return 1",  # 5
                "",  # 6
                "",  # 7
                "def build_parser():",  # 8
                "    parser = None",  # 9
                "    parser = object()",  # 10
                "    value = 1",  # 11
                "    return parser, value",  # 12
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_the_scope_index_names_the_function_the_lines_belong_to(tmp_path):
    _write_source_file(tmp_path)

    document = _build(tmp_path)

    scopes = document["file_scope_tests"]["src/mac/foo.py"]
    assert "build_parser" in scopes
    nodeids = document["nodeids"]
    assert {nodeids[i] for i in scopes["build_parser"]} == {
        "tests/test_foo.py::test_a",
        "tests/test_foo.py::test_b",
    }


def test_the_scope_index_survives_the_fanout_prune(tmp_path):
    """The whole point. Lines executed by many tests are dropped from the line
    index -- and those are exactly the widely-executed ones, where selecting
    correctly matters most. Aggregating before the prune keeps their answer."""
    _write_source_file(tmp_path)
    _write_coverage_db(tmp_path / ".coverage")
    _write_timings(tmp_path / "timings.json")

    document = BUILDER.build_map(
        tmp_path / ".coverage",
        tmp_path / "timings.json",
        repo_root=tmp_path,
        max_line_fanout=1,
    )

    assert document["stats"]["pruned_high_fanout_lines"] > 0
    scopes = document["file_scope_tests"]["src/mac/foo.py"]
    nodeids = document["nodeids"]
    assert {nodeids[i] for i in scopes["build_parser"]} == {
        "tests/test_foo.py::test_a",
        "tests/test_foo.py::test_b",
    }


def test_a_file_that_will_not_parse_simply_has_no_scopes(tmp_path):
    """Never a build failure: no scope index for that file means the resolver
    uses the line and file indices exactly as it did before."""
    target = tmp_path / "src" / "mac" / "foo.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def (((\n", encoding="utf-8")

    document = _build(tmp_path)

    assert "src/mac/foo.py" not in document.get("file_scope_tests", {})
    assert "src/mac/foo.py" in document["file_tests"]


def test_committed_impact_map_ids_actually_collect():
    """Every node id in the map must be one pytest will COLLECT.

    The sibling test above checks that each id's `def test_*` still exists.
    That is not enough, and the gap is not theoretical: an id can name a live
    function and still not exist.

    `@pytest.mark.parametrize("...", SOME_LIST)` with an EMPTY list generates
    ZERO instances. The function is right there in the file, so a name-based
    check passes -- while every parametrised node id the map holds for it stops
    resolving. Selecting one is a pytest USAGE error, not a test failure:

        (no match in any of [<Module test_authority_boundary.py>])
        collected 398 items
        no tests ran           exit code 4

    That is what happened when #417 emptied TERMINAL_ROUTES, and it took out an
    unrelated PR's sanity run. A census at the time found 180 such ids -- most
    of them parametrisations removed by the work-package deletion, where the
    test function survived and only its parameters changed.

    Collection is the only authority on what exists. It costs ~3s.
    """
    import json
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=600,
    )
    collected = {
        line.strip()
        for line in result.stdout.splitlines()
        if "::" in line and line.startswith("tests/")
    }
    assert collected, (
        "collected nothing; the map cannot be checked against an empty set.\n%s"
        % result.stdout[-2000:]
    )
    document = json.loads(
        (root / "src" / "mac" / "data" / "test_impact_map.json").read_text(encoding="utf-8")
    )
    stale = sorted(
        node_id
        for node_id in document.get("nodeids", [])
        if node_id.startswith("tests/") and node_id not in collected
    )
    assert not stale, (
        "%d impact-map node ids do not collect. Selecting one fails the run "
        "with a pytest usage error rather than a test failure. Rebuild or "
        "remap the map.\nFirst 10: %s" % (len(stale), stale[:10])
    )
