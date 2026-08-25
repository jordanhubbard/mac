#!/usr/bin/env python3
"""Print CLI subcommand coverage ratio as a percentage.

Used by ``make cli-coverage``.  Applies the same discovery logic as
``tests/cli/test_cli_coverage_gate.py`` so the numbers are always consistent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    cli_src = (_REPO_ROOT / "src" / "mac" / "cli.py").read_text(encoding="utf-8")

    # All .add_parser("<name>") calls -- parent -> [child, ...]
    anon_pattern = re.compile(r"(\w+)\.add_parser\(\s*[\"']([^\"']+)[\"']")
    parent_children: dict[str, list[str]] = {}
    for parent, name in anon_pattern.findall(cli_src):
        parent_children.setdefault(parent, []).append(name)

    # var = <parent>.add_parser("<name>") -- domain_name -> [var, ...]
    var_pattern = re.compile(r"(\w+)\s*=\s*(\w+)\.add_parser\(\s*[\"']([^\"']+)[\"']")
    domain_vars: dict[str, list[str]] = {}
    for var, parent, name in var_pattern.findall(cli_src):
        if parent == "sub":
            domain_vars.setdefault(name, []).append(var)

    top_level: list[str] = parent_children.get("sub", [])
    pairs: set[tuple[str, str]] = set()
    for domain in top_level:
        subs: list[str] = []
        for dvar in domain_vars.get(domain, []):
            subs.extend(parent_children.get(dvar, []))
        if subs:
            for sub in set(subs):
                pairs.add((domain, sub))
        else:
            pairs.add((domain, ""))

    # Scan tests/cli/test_*.py for _run(...) invocations
    run_re = re.compile(
        r'_run\s*\([^,)]+,\s*["\']([^"\']+)["\']'
        r'(?:\s*,\s*["\']([^"\']+)["\'])?'
    )
    tested: set[tuple[str, str]] = set()
    for test_file in sorted((_REPO_ROOT / "tests" / "cli").glob("test_*.py")):
        for domain, sub in run_re.findall(test_file.read_text(encoding="utf-8")):
            tested.add((domain, sub if sub else ""))

    total = len(pairs)
    covered = len(tested & pairs)
    ratio = (covered / total * 100) if total else 0.0

    uncovered = sorted(pairs - tested)
    print(f"CLI subcommand coverage: {covered}/{total} ({ratio:.1f}%)")
    print(f"  Tested:    {covered}")
    print(f"  Untested:  {len(uncovered)}")
    if uncovered:
        print("\nUntested subcommands:")
        for domain, sub in uncovered:
            cmd = f"  mac {domain} {sub}".strip()
            print(cmd)


if __name__ == "__main__":
    main()
