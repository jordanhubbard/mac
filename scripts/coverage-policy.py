#!/usr/bin/env python3
"""Enforce statement and branch coverage safety floors without KPI gaming."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


SCHEMA = "mac.coverage_policy_result.v1"
DIFF_SCHEMA = "mac.coverage_policy_diff_result.v1"
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _percentage(covered: int, total: int) -> float:
    return 100.0 if total == 0 else covered * 100.0 / total


def evaluate(coverage_doc: dict[str, Any], policy_doc: dict[str, Any]) -> dict[str, Any]:
    """Return a stable result document for one coverage.py JSON report."""

    totals = coverage_doc.get("totals")
    policy = policy_doc.get("coverage")
    if not isinstance(totals, dict) or not isinstance(policy, dict):
        raise ValueError("coverage JSON totals and policy [coverage] are required")

    statements = int(totals.get("num_statements", 0))
    covered_statements = int(totals.get("covered_lines", 0))
    branches = int(totals.get("num_branches", 0))
    covered_branches = int(totals.get("covered_branches", 0))
    statement_floor = float(policy["statement_safety_floor"])
    branch_floor = float(policy["branch_safety_floor"])
    require_branches = bool(policy.get("require_branch_measurement", True))

    statement_percent = _percentage(covered_statements, statements)
    branch_percent = _percentage(covered_branches, branches)
    failures: list[str] = []
    if statement_percent + 1e-9 < statement_floor:
        failures.append(
            f"statement coverage {statement_percent:.2f}% is below safety floor {statement_floor:.2f}%"
        )
    if require_branches and branches == 0:
        failures.append("branch coverage was not measured")
    elif branch_percent + 1e-9 < branch_floor:
        failures.append(
            f"branch coverage {branch_percent:.2f}% is below safety floor {branch_floor:.2f}%"
        )

    return {
        "schema": SCHEMA,
        "status": "pass" if not failures else "fail",
        "policy_role": "regression_safety_floor_not_optimization_target",
        "statements": {
            "covered": covered_statements,
            "total": statements,
            "percent": round(statement_percent, 4),
            "safety_floor": statement_floor,
        },
        "branches": {
            "covered": covered_branches,
            "total": branches,
            "percent": round(branch_percent, 4),
            "safety_floor": branch_floor,
        },
        "failures": failures,
    }


def evaluate_diff(
    coverage_doc: dict[str, Any],
    policy_doc: dict[str, Any],
    changed_lines: dict[str, set[int]],
) -> dict[str, Any]:
    """Enforce the statement floor over ONLY the changed lines.

    A resolver-selected subset run cannot measure whole-repo coverage, so the
    gate instead requires the lines the change actually touched to be covered.
    An untested changed line (or a changed source file coverage never measured)
    therefore fails the gate. Branch safety over the diff is intentionally not
    enforced here — the scheduled full run re-enforces the whole-repo branch
    floor; the count of uncovered branches on changed lines is reported for
    diagnostics only."""

    coverage = policy_doc.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("policy [coverage] is required")
    diff_policy = coverage.get("diff") if isinstance(coverage.get("diff"), dict) else {}
    statement_floor = float(
        diff_policy.get("statement_safety_floor", coverage["statement_safety_floor"])
    )

    files = coverage_doc.get("files")
    if not isinstance(files, dict):
        raise ValueError("coverage JSON files map is required")

    covered = relevant = 0
    branch_missing_on_changed = 0
    per_file: dict[str, dict[str, int]] = {}
    for path, lines in sorted(changed_lines.items()):
        info = files.get(path)
        if not isinstance(info, dict):
            # A changed source file coverage did not measure: every changed line
            # is relevant and uncovered, which fails the floor (as it should).
            covered_here, relevant_here = 0, len(lines)
        else:
            executed = set(info.get("executed_lines", []))
            missing = set(info.get("missing_lines", []))
            statement_lines = executed | missing
            relevant_lines = lines & statement_lines
            covered_here = len(lines & executed)
            relevant_here = len(relevant_lines)
            for arc in info.get("missing_branches", []) or []:
                if isinstance(arc, (list, tuple)) and arc and arc[0] in lines:
                    branch_missing_on_changed += 1
        covered += covered_here
        relevant += relevant_here
        if relevant_here:
            per_file[path] = {"covered": covered_here, "relevant": relevant_here}

    percent = _percentage(covered, relevant)
    failures: list[str] = []
    if relevant and percent + 1e-9 < statement_floor:
        failures.append(
            f"diff statement coverage {percent:.2f}% is below safety floor {statement_floor:.2f}% "
            f"({covered}/{relevant} changed lines covered)"
        )
    return {
        "schema": DIFF_SCHEMA,
        "status": "pass" if not failures else "fail",
        "mode": "diff",
        "policy_role": "changed_line_safety_floor_with_scheduled_full_backstop",
        "statements": {
            "covered": covered,
            "relevant": relevant,
            "percent": round(percent, 4),
            "safety_floor": statement_floor,
        },
        "branches": {
            "uncovered_on_changed_lines": branch_missing_on_changed,
            "enforced": False,
            "note": "whole-repo branch floor is enforced by the scheduled full run",
        },
        "files": per_file,
        "failures": failures,
    }


def changed_new_lines(base: str | None, repo_root: Path) -> dict[str, set[int]]:
    """New-side (post-change) line numbers per changed source file."""
    rng = f"{base}...HEAD" if base else "HEAD"
    result = subprocess.run(
        ["git", "diff", "-U0", "--no-color", "--no-ext-diff", rng],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {}
    lines: dict[str, set[int]] = {}
    current: str | None = None
    for row in result.stdout.splitlines():
        if row.startswith("+++ "):
            target = row[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                path = target[2:] if target.startswith("b/") else target
                current = path if path.startswith("src/") and path.endswith(".py") else None
            continue
        if current is None:
            continue
        match = _HUNK_RE.match(row)
        if match:
            start = int(match.group(1))
            length = int(match.group(2)) if match.group(2) is not None else 1
            if length > 0:
                lines.setdefault(current, set()).update(range(start, start + length))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-json", default="coverage.json")
    parser.add_argument("--policy", default="test-policy.toml")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--mode",
        choices=("whole", "diff"),
        default="whole",
        help="'whole' enforces whole-repo floors; 'diff' enforces the statement floor over changed lines",
    )
    parser.add_argument("--base", help="diff mode: base ref for changed-line detection")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--changed-lines",
        help="diff mode: JSON file mapping source path -> [line numbers] (overrides --base)",
    )
    parser.add_argument(
        "--partial",
        action="store_true",
        help=(
            "label an incomplete measurement and skip floor enforcement; "
            "the caller's test failure remains authoritative"
        ),
    )
    args = parser.parse_args(argv)

    try:
        coverage_doc = json.loads(Path(args.coverage_json).read_text(encoding="utf-8"))
        with Path(args.policy).open("rb") as stream:
            policy_doc = tomllib.load(stream)
        if args.mode == "diff":
            if args.changed_lines:
                raw = json.loads(Path(args.changed_lines).read_text(encoding="utf-8"))
                changed = {str(k): {int(n) for n in v} for k, v in raw.items()}
            else:
                changed = changed_new_lines(args.base, args.repo_root)
            result = evaluate_diff(coverage_doc, policy_doc, changed)
        else:
            result = evaluate(coverage_doc, policy_doc)
        if args.partial:
            result["partial"] = True
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(f"coverage-policy: invalid input: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    elif result.get("mode") == "diff":
        statement = result["statements"]
        print(
            ("partial diff coverage (tests failed): " if args.partial else "diff coverage safety: ")
            + "statements "
            f"{statement['covered']}/{statement['relevant']} changed lines "
            f"({statement['percent']:.2f}%, floor {statement['safety_floor']:.2f}%); "
            f"uncovered branches on changed lines: {result['branches']['uncovered_on_changed_lines']}"
        )
        if not args.partial:
            for failure in result["failures"]:
                print(f"coverage-policy: {failure}", file=sys.stderr)
    else:
        statement = result["statements"]
        branch = result["branches"]
        print(
            ("partial coverage (tests failed): " if args.partial else "coverage safety: ")
            + "statements "
            f"{statement['covered']}/{statement['total']} ({statement['percent']:.2f}%, floor {statement['safety_floor']:.2f}%); "
            f"branches {branch['covered']}/{branch['total']} ({branch['percent']:.2f}%, floor {branch['safety_floor']:.2f}%)"
        )
        if not args.partial:
            for failure in result["failures"]:
                print(f"coverage-policy: {failure}", file=sys.stderr)
    return 0 if args.partial or result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
