#!/usr/bin/env python3
"""Build the committed test-impact map from a per-test coverage run.

The map is the dynamic (per-test coverage) layer of impact-based test selection
(see scripts/resolve-impacted-tests.py). It records, for each source file (and
line), the tests whose execution actually touched it, so a change can select
only the tests that exercise the changed code.

Input is coverage.py's SQLite data file produced by the portfolio run
(scripts/test-portfolio.py), whose root conftest.py tags every test's arcs with
a ``test|<nodeid>`` context. Tests whose coverage is NOT attributed (subprocess
work that stripped the coverage env, or session-level arcs) are recorded in
``always_run`` so the selector can never silently drop them.

The artifact is intentionally interned (a nodeid table plus integer indices) to
stay compact for a suite with thousands of tests and files.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "mac.test_impact_map.v1"
DEFAULT_COVERAGE = ROOT / ".test-portfolio" / ".coverage"
DEFAULT_TIMINGS = ROOT / ".test-portfolio" / "timings.json"
DEFAULT_OUTPUT = ROOT / "src" / "mac" / "data" / "test_impact_map.json"
SOURCE_PREFIX = "src/"
# A source line executed by more than this many tests carries almost no
# selective signal — a change there would select a large fraction of the suite,
# which is barely different from a full run — while dominating the committed
# artifact's size (the highest-fanout lines are the bulk of the bytes). Such
# lines are dropped from the LINE index; the resolver transparently falls back
# to the FILE index for any line it cannot find, so a change to a pruned line
# still selects every test that touched the file (a safe superset), never fewer.
DEFAULT_MAX_LINE_FANOUT = 200


def _load_portfolio_module():
    """Import scripts/test-portfolio.py (hyphenated) to reuse its documented
    coverage-context reader — one source of truth for the SQLite schema."""
    path = Path(__file__).resolve().parent / "test-portfolio.py"
    spec = importlib.util.spec_from_file_location("mac_test_portfolio", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load test-portfolio.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else "unknown"


def _file_of(nodeid: str) -> str:
    """The test file path portion of a pytest node id (before ``::``)."""
    return nodeid.split("::", 1)[0]


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return "sha256:" + digest


def _file_scopes(path: Path) -> dict[str, tuple[int, int]]:
    """Qualified scope name -> inclusive line span for one source file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return {}
    scopes: dict[str, tuple[int, int]] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                end = getattr(child, "end_lineno", None)
                if end is not None:
                    scopes[name] = (child.lineno, end)
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return scopes


def _innermost_scope(scopes: dict[str, tuple[int, int]], line: int) -> str | None:
    best: str | None = None
    best_span: int | None = None
    for name, (start, end) in scopes.items():
        if start <= line <= end:
            span = end - start
            if best_span is None or span < best_span:
                best, best_span = name, span
    return best


def _timing_nodeids(timings_file: Path) -> set[str]:
    if not timings_file.is_file():
        return set()
    document = json.loads(timings_file.read_text(encoding="utf-8"))
    return {str(item["nodeid"]) for item in document.get("tests", [])}


def build_map(
    coverage_file: Path,
    timings_file: Path,
    *,
    repo_root: Path = ROOT,
    max_line_fanout: int = DEFAULT_MAX_LINE_FANOUT,
) -> dict[str, Any]:
    """Return the interned impact-map document for one coverage data file.

    ``max_line_fanout`` drops line-index entries touched by more than that many
    tests (see DEFAULT_MAX_LINE_FANOUT); such lines are the least selective and
    dominate the artifact size, and the resolver's file-level fallback keeps a
    change to a pruned line safe. Pass a value <= 0 to keep every line."""
    portfolio = _load_portfolio_module()
    context_lines, context_arcs, unattributed_arcs = portfolio._coverage_contributions(
        coverage_file
    )

    attributed = set(context_lines) | set(context_arcs)
    timing_nodeids = _timing_nodeids(timings_file)
    # Tests that ran but left no attributed coverage (subprocess work that
    # stripped the coverage env, pure-assertion tests, session teardown) cannot
    # be mapped to a file, so they must always run. Record at file granularity.
    unattributed_tests = sorted(timing_nodeids - attributed)
    always_run = sorted({_file_of(nodeid) for nodeid in unattributed_tests})

    # Intern nodeids so the file/line indices reference small integers.
    nodeid_index: dict[str, int] = {}

    def _intern(nodeid: str) -> int:
        if nodeid not in nodeid_index:
            nodeid_index[nodeid] = len(nodeid_index)
        return nodeid_index[nodeid]

    file_tests: dict[str, set[int]] = {}
    file_line_tests: dict[str, dict[str, set[int]]] = {}
    for nodeid, entries in context_lines.items():
        idx = _intern(nodeid)
        for filename, line in entries:
            if not str(filename).startswith(SOURCE_PREFIX):
                continue
            file_tests.setdefault(filename, set()).add(idx)
            file_line_tests.setdefault(filename, {}).setdefault(str(line), set()).add(idx)

    # Aggregate per SCOPE (qualified function/class name) before anything is
    # pruned. Two problems this solves, both of which sent whole-suite runs to
    # CI for a one-line change:
    #
    #   drift   - the line index is only usable for files byte-identical to
    #             this revision. src/mac/cli.py changes most weeks, so its line
    #             data was almost never usable, and an unusable file resolves
    #             to the full suite. Names survive edits that renumber lines.
    #
    #   pruning - lines executed by more than the fanout cap are dropped from
    #             the line index. Those are precisely the widely-executed ones,
    #             so the changes most likely to break something had the least
    #             line data. Aggregating here, BEFORE the prune, keeps the
    #             answer for exactly those.
    scope_tests: dict[str, dict[str, set[int]]] = {}
    for filename, lines in file_line_tests.items():
        scopes = _file_scopes(repo_root / filename)
        if not scopes:
            continue
        per_file = scope_tests.setdefault(filename, {})
        for line, indices in lines.items():
            name = _innermost_scope(scopes, int(line))
            if name is not None:
                per_file.setdefault(name, set()).update(indices)

    nodeids = [nodeid for nodeid, _ in sorted(nodeid_index.items(), key=lambda kv: kv[1])]
    file_hashes: dict[str, str] = {}
    for filename in file_tests:
        digest = _sha256_file(repo_root / filename)
        if digest is not None:
            file_hashes[filename] = digest

    # Prune the least-selective / heaviest line entries. file_tests (file-level)
    # is deliberately left intact so a change to a pruned line still resolves to
    # every test that touched the file — a safe superset of the line answer.
    pruned_lines = 0
    line_index: dict[str, dict[str, list[int]]] = {}
    for filename, lines in sorted(file_line_tests.items()):
        kept: dict[str, list[int]] = {}
        for line, indices in sorted(lines.items()):
            if 0 < max_line_fanout < len(indices):
                pruned_lines += 1
                continue
            kept[line] = sorted(indices)
        if kept:
            line_index[filename] = kept

    return {
        "schema": SCHEMA,
        "generated_by": "scripts/build-test-impact-map.py",
        "base_sha": _git_head(repo_root),
        "source_prefix": SOURCE_PREFIX,
        "nodeids": nodeids,
        "file_tests": {
            filename: sorted(indices) for filename, indices in sorted(file_tests.items())
        },
        "file_line_tests": line_index,
        "file_scope_tests": {
            filename: {name: sorted(indices) for name, indices in sorted(scopes.items())}
            for filename, scopes in sorted(scope_tests.items())
            if scopes
        },
        "file_hashes": file_hashes,
        "always_run": always_run,
        "stats": {
            "attributed_tests": len(attributed),
            "unattributed_tests": len(unattributed_tests),
            "mapped_files": len(file_tests),
            "unattributed_arcs": unattributed_arcs,
            "interned_nodeids": len(nodeids),
            "line_fanout_cap": max_line_fanout if max_line_fanout > 0 else 0,
            "pruned_high_fanout_lines": pruned_lines,
            "mapped_scopes": sum(len(scopes) for scopes in scope_tests.values()),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-file", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--timings", type=Path, default=DEFAULT_TIMINGS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument(
        "--max-line-fanout",
        type=int,
        default=DEFAULT_MAX_LINE_FANOUT,
        help="drop line-index entries touched by more than N tests (<=0 keeps all)",
    )
    args = parser.parse_args(argv)

    if not args.coverage_file.is_file():
        print(
            f"build-test-impact-map: coverage data not found: {args.coverage_file}\n"
            "Run `MAC_TEST_PORTFOLIO=1 scripts/run-contract-tests.sh` or "
            "`scripts/test-portfolio.py` first.",
            file=sys.stderr,
        )
        return 2
    try:
        document = build_map(
            args.coverage_file,
            args.timings,
            repo_root=args.repo_root.resolve(),
            max_line_fanout=args.max_line_fanout,
        )
    except (OSError, ValueError) as exc:
        print(f"build-test-impact-map: {exc}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Compact separators: this artifact is machine-read, committed, and large;
    # pretty-printing roughly tripled its on-disk (and git-history) size.
    args.output.write_text(
        json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    stats = document["stats"]
    print(
        "test-impact map: %d files, %d tests (%d unattributed -> always_run), "
        "%d high-fanout lines pruned (cap %d), base %s -> %s (%.1f MB)"
        % (
            stats["mapped_files"],
            stats["interned_nodeids"],
            stats["unattributed_tests"],
            stats["pruned_high_fanout_lines"],
            stats["line_fanout_cap"],
            document["base_sha"][:12],
            args.output,
            args.output.stat().st_size / 1e6,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
