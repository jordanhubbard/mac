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
) -> dict[str, Any]:
    """Return the interned impact-map document for one coverage data file."""
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

    nodeids = [nodeid for nodeid, _ in sorted(nodeid_index.items(), key=lambda kv: kv[1])]
    file_hashes: dict[str, str] = {}
    for filename in file_tests:
        digest = _sha256_file(repo_root / filename)
        if digest is not None:
            file_hashes[filename] = digest

    return {
        "schema": SCHEMA,
        "generated_by": "scripts/build-test-impact-map.py",
        "base_sha": _git_head(repo_root),
        "source_prefix": SOURCE_PREFIX,
        "nodeids": nodeids,
        "file_tests": {
            filename: sorted(indices) for filename, indices in sorted(file_tests.items())
        },
        "file_line_tests": {
            filename: {line: sorted(indices) for line, indices in sorted(lines.items())}
            for filename, lines in sorted(file_line_tests.items())
        },
        "file_hashes": file_hashes,
        "always_run": always_run,
        "stats": {
            "attributed_tests": len(attributed),
            "unattributed_tests": len(unattributed_tests),
            "mapped_files": len(file_tests),
            "unattributed_arcs": unattributed_arcs,
            "interned_nodeids": len(nodeids),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-file", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--timings", type=Path, default=DEFAULT_TIMINGS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
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
            args.coverage_file, args.timings, repo_root=args.repo_root.resolve()
        )
    except (OSError, ValueError) as exc:
        print(f"build-test-impact-map: {exc}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    stats = document["stats"]
    print(
        "test-impact map: %d files, %d tests (%d unattributed -> always_run), "
        "base %s -> %s"
        % (
            stats["mapped_files"],
            stats["interned_nodeids"],
            stats["unattributed_tests"],
            document["base_sha"][:12],
            args.output,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
