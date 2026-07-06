#!/usr/bin/env python3
"""Enforce statement and branch coverage safety floors without KPI gaming."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any


SCHEMA = "mac.coverage_policy_result.v1"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-json", default="coverage.json")
    parser.add_argument("--policy", default="test-policy.toml")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    try:
        coverage_doc = json.loads(Path(args.coverage_json).read_text(encoding="utf-8"))
        with Path(args.policy).open("rb") as stream:
            policy_doc = tomllib.load(stream)
        result = evaluate(coverage_doc, policy_doc)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"coverage-policy: invalid input: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        statement = result["statements"]
        branch = result["branches"]
        print(
            "coverage safety: statements "
            f"{statement['covered']}/{statement['total']} ({statement['percent']:.2f}%, floor {statement['safety_floor']:.2f}%); "
            f"branches {branch['covered']}/{branch['total']} ({branch['percent']:.2f}%, floor {branch['safety_floor']:.2f}%)"
        )
        for failure in result["failures"]:
            print(f"coverage-policy: {failure}", file=sys.stderr)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
