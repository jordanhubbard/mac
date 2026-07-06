#!/usr/bin/env python3
"""Measure per-test execution contribution without declaring tests redundant.

The report combines exact pytest node-id timings with coverage.py contexts and
branch arcs. A zero-unique-contribution test is only a review candidate; fault
or assertion semantics can still justify keeping it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "mac.test_portfolio_report.v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".test-portfolio"


def _run(command: list[str], *, env: dict[str, str]) -> int:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode


def _hermetic_environment(output_dir: Path) -> dict[str, str]:
    """Match the canonical contract gate's secret-free, host-free environment."""

    env = dict(os.environ)
    prefixes = ("ACC_", "FIRECRAWL_", "HERMES_", "MAC_", "QDRANT_", "SLACK_", "TOKENHUB_")
    for key in list(env):
        if key.startswith(prefixes):
            env.pop(key, None)
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GITEA_TOKEN", "GIT_TOKEN"):
        env.pop(key, None)
    home = output_dir / "home"
    if home.exists():
        shutil.rmtree(home)
    (home / ".config").mkdir(parents=True)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    for key, value in (
        ("user.email", "mac-contract-tests@example.invalid"),
        ("user.name", "mac contract tests"),
        ("init.defaultBranch", "main"),
    ):
        subprocess.run(
            ["git", "config", "--global", key, value],
            env=env,
            capture_output=True,
            check=False,
        )
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", "*"],
        env=env,
        capture_output=True,
        check=False,
    )
    return env


def _load_timings(path: Path) -> dict[str, dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "mac.test_portfolio_timings.v1":
        raise ValueError(f"unexpected timings schema in {path}")
    return {str(item["nodeid"]): dict(item) for item in document.get("tests", [])}


def _coverage_contributions(data_file: Path) -> tuple[dict[str, set[tuple[Any, ...]]], dict[str, set[tuple[Any, ...]]], int]:
    """Read context-owned lines/arcs from coverage.py's documented SQLite data.

    Coverage stores branch runs as one row per (file, context, arc). Deriving
    executed lines from positive arc endpoints avoids an O(tests * files)
    public-API query loop for a suite with thousands of contexts.
    """

    context_lines: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    context_arcs: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    unattributed_arcs = 0
    with sqlite3.connect(data_file) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM meta"))
        if metadata.get("has_arcs") != "1":
            raise ValueError("portfolio coverage data does not contain branch arcs")
        rows = connection.execute(
            "SELECT context.context, file.path, arc.fromno, arc.tono "
            "FROM arc JOIN context ON context.id = arc.context_id "
            "JOIN file ON file.id = arc.file_id"
        )
        for raw_context, filename, from_line, to_line in rows:
            context = str(raw_context or "")
            arc = (str(filename), int(from_line), int(to_line))
            if not context.startswith("test|"):
                unattributed_arcs += 1
                continue
            nodeid = context.removeprefix("test|")
            context_arcs[nodeid].add(arc)
            if int(from_line) > 0:
                context_lines[nodeid].add((str(filename), int(from_line)))
            if int(to_line) > 0:
                context_lines[nodeid].add((str(filename), int(to_line)))
    return context_lines, context_arcs, unattributed_arcs


def _unique_counts(contributions: dict[str, set[tuple[Any, ...]]]) -> dict[str, int]:
    owners: dict[tuple[Any, ...], int] = defaultdict(int)
    for items in contributions.values():
        for item in items:
            owners[item] += 1
    return {
        context: sum(1 for item in items if owners[item] == 1)
        for context, items in contributions.items()
    }


def _fingerprint(items: set[tuple[Any, ...]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(items):
        digest.update(repr(item).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def build_report(data_file: Path, timings_file: Path) -> dict[str, Any]:
    timings = _load_timings(timings_file)
    lines, arcs, unattributed_arcs = _coverage_contributions(data_file)
    unique_lines = _unique_counts(lines)
    unique_arcs = _unique_counts(arcs)
    tests: list[dict[str, Any]] = []
    for nodeid, timing in timings.items():
        record = {
            "nodeid": nodeid,
            "outcome": timing.get("outcome", "unknown"),
            "duration_seconds": round(float(timing.get("duration_seconds", 0.0)), 6),
            "executed_lines": len(lines.get(nodeid, set())),
            "executed_arcs": len(arcs.get(nodeid, set())),
            "unique_lines": unique_lines.get(nodeid, 0),
            "unique_arcs": unique_arcs.get(nodeid, 0),
            "execution_fingerprint": _fingerprint(arcs.get(nodeid, set())),
        }
        record["review_candidate"] = bool(
            record["outcome"] == "passed"
            and record["unique_lines"] == 0
            and record["unique_arcs"] == 0
        )
        tests.append(record)
    candidates = [item for item in tests if item["review_candidate"]]
    candidates.sort(key=lambda item: (-float(item["duration_seconds"]), str(item["nodeid"])))
    fingerprint_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in tests:
        if int(item["executed_arcs"]) > 0:
            fingerprint_groups[str(item["execution_fingerprint"])].append(item)
    equivalent_groups = [
        {
            "execution_fingerprint": fingerprint,
            "executed_arcs": group[0]["executed_arcs"],
            "aggregate_duration_seconds": round(
                sum(float(item["duration_seconds"]) for item in group), 6
            ),
            "tests": sorted(str(item["nodeid"]) for item in group),
        }
        for fingerprint, group in fingerprint_groups.items()
        if len(group) > 1
    ]
    equivalent_groups.sort(
        key=lambda group: (-float(group["aggregate_duration_seconds"]), -len(group["tests"]))
    )
    return {
        "schema": SCHEMA,
        "interpretation": "candidates_require_semantic_and_fault_detection_review_before_deletion",
        "summary": {
            "tests": len(tests),
            "passed": sum(item["outcome"] == "passed" for item in tests),
            "failed": sum(item["outcome"] == "failed" for item in tests),
            "skipped": sum(item["outcome"] == "skipped" for item in tests),
            "total_duration_seconds": round(sum(float(item["duration_seconds"]) for item in tests), 6),
            "tests_with_unique_lines": sum(int(item["unique_lines"]) > 0 for item in tests),
            "tests_with_unique_arcs": sum(int(item["unique_arcs"]) > 0 for item in tests),
            "zero_unique_contribution_candidates": len(candidates),
            "equivalent_execution_groups": len(equivalent_groups),
            "unattributed_subprocess_or_session_arcs": unattributed_arcs,
        },
        "candidates": candidates,
        "equivalent_execution_groups": equivalent_groups,
        "tests_detail": sorted(tests, key=lambda item: str(item["nodeid"])),
    }


def _write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    summary = report["summary"]
    lines = [
        "# MAC test portfolio report",
        "",
        "Zero unique contribution is a review signal, not proof that a test is redundant.",
        "",
        f"- Tests observed: {summary['tests']}",
        f"- Passed / failed / skipped: {summary['passed']} / {summary['failed']} / {summary['skipped']}",
        f"- Aggregate pytest phase time: {summary['total_duration_seconds']:.3f}s",
        f"- Tests with unique lines: {summary['tests_with_unique_lines']}",
        f"- Tests with unique arcs: {summary['tests_with_unique_arcs']}",
        f"- Zero-unique review candidates: {summary['zero_unique_contribution_candidates']}",
        f"- Exact execution-equivalent groups: {summary['equivalent_execution_groups']}",
        f"- Unattributed session/child-process arcs: {summary['unattributed_subprocess_or_session_arcs']}",
        "",
        "## Highest-cost review candidates",
        "",
    ]
    for candidate in report["candidates"][:50]:
        lines.append(
            f"- {candidate['duration_seconds']:.3f}s `{candidate['nodeid']}` "
            f"(lines={candidate['executed_lines']}, arcs={candidate['executed_arcs']})"
        )
    lines.extend(["", "## Highest-cost execution-equivalent groups", ""])
    for group in report["equivalent_execution_groups"][:25]:
        lines.append(
            f"- {group['aggregate_duration_seconds']:.3f}s across {len(group['tests'])} tests; "
            f"arcs={group['executed_arcs']}"
        )
        for nodeid in group["tests"]:
            lines.append(f"  - `{nodeid}`")
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_portfolio(output_dir: Path, pytest_args: Iterable[str]) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    data_file = output_dir / ".coverage"
    timings_file = output_dir / "timings.json"
    env = _hermetic_environment(output_dir)
    env["COVERAGE_FILE"] = str(data_file)
    env["MAC_TEST_PORTFOLIO_OUTPUT"] = str(timings_file)
    if _run([sys.executable, "-m", "coverage", "erase"], env=env) != 0:
        return 2
    pytest_status = _run(
        [sys.executable, "-m", "coverage", "run", "-m", "pytest", *pytest_args], env=env
    )
    if _run([sys.executable, "-m", "coverage", "combine"], env=env) != 0:
        return 2
    if not timings_file.exists():
        print("test-portfolio: pytest did not produce timing evidence", file=sys.stderr)
        return 2
    report = build_report(data_file, timings_file)
    _write_report(report, output_dir)
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    print(f"portfolio report: {output_dir / 'report.md'}")
    return pytest_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    if args.report_only:
        try:
            report = build_report(output_dir / ".coverage", output_dir / "timings.json")
            _write_report(report, output_dir)
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            print(f"test-portfolio: cannot build report: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(report["summary"], indent=2, sort_keys=True))
        return 0
    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    return run_portfolio(output_dir, pytest_args)


if __name__ == "__main__":
    raise SystemExit(main())
