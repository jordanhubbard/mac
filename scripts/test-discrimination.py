#!/usr/bin/env python3
"""Prove whether a test detects anything no other test detects.

THE GATE THIS IMPLEMENTS

A suite that only ever grows is a suite nobody can reason about, and this one
grew for over a year with no way to retire anything. But "these tests execute
the same lines" is not grounds for deleting one: two tests can run identical
code and check entirely different properties. Coverage says what RAN. It says
nothing about what was CHECKED.

What distinguishes them is whether a test NOTICES when the code is wrong. So
this breaks the code on purpose -- one small, targeted mutation at a time, on
the lines the tests share -- and records which tests fail. A test that fails
for no mutation any other test also fails for is carrying no unique weight, and
retiring it costs nothing.

The inverse is the important half: a test that catches a mutant nothing else
catches must be KEPT, however redundant its coverage looked. That is the
protection a coverage-only heuristic cannot offer, and the reason retirement
should be gated on this rather than on the report.

WHAT IT DOES NOT PROVE

Surviving mutants are a lower bound. These operators are deliberately small and
syntactic, so "no test caught any mutation" can mean the tests are weak OR that
the operators never expressed a fault that matters here. Read a nothing-killed
result as "no evidence either way", never as "safe to delete everything".
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "mac.test_discrimination.v1"

#: Small, syntactic, and reversible: each flips ONE decision so a test that
#: checks the outcome of that decision fails and one that merely executes the
#: line does not. Ordered so the cheapest and least ambiguous run first.
MUTATIONS: tuple[tuple[str, str], ...] = (
    ("==", "!="),
    ("!=", "=="),
    (" and ", " or "),
    (" or ", " and "),
    (">=", ">"),
    ("<=", "<"),
    ("True", "False"),
    ("False", "True"),
    (" not ", " "),
)


def mutants(path: Path, lines: set[int], *, limit: int) -> list[tuple[int, str, str]]:
    """(line number, original text, mutated text) for each candidate mutation."""
    source = path.read_text(encoding="utf-8").splitlines()
    found: list[tuple[int, str, str]] = []
    for number in sorted(lines):
        if not (1 <= number <= len(source)):
            continue
        text = source[number - 1]
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for needle, replacement in MUTATIONS:
            if needle in text:
                found.append((number, text, text.replace(needle, replacement, 1)))
                break  # one mutation per line keeps the attribution unambiguous
        if len(found) >= limit:
            break
    return found


def run_tests(nodeids: list[str], *, timeout: float) -> set[str]:
    """The nodeids that FAILED. A crashed or timed-out run returns nothing,
    which counts as 'killed nothing' rather than silently as 'all killed'."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.xml"
        try:
            subprocess.run(
                [
                    sys.executable, "-m", "pytest", *nodeids,
                    "-q", "--no-header", "--tb=no",
                    "-p", "no:randomly", "-p", "no:cacheprovider",
                    "--junit-xml", str(report),
                ],
                cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return set()
        if not report.exists():
            return set()
        import xml.etree.ElementTree as ET

        try:
            tree = ET.parse(report)
        except ET.ParseError:
            return set()
        failed = set()
        for case in tree.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                classname = (case.get("classname") or "").replace(".", "/")
                name = case.get("name") or ""
                failed.add("%s::%s" % (classname, name))
        return failed


def _matches(failed_keys: set[str], nodeid: str) -> bool:
    """Junit reports classname::name; nodeids carry paths and parametrisation."""
    leaf = nodeid.split("::")[-1].split("[")[0]
    return any(key.endswith("::" + leaf) or leaf in key for key in failed_keys)


def analyse(
    nodeids: list[str], target: Path, lines: set[int], *, limit: int, timeout: float
) -> dict:
    candidates = mutants(target, lines, limit=limit)
    if not candidates:
        return {
            "schema": SCHEMA,
            "verdict": "no_mutations_available",
            "detail": "no mutable expression on the covered lines",
            "tests": nodeids,
        }
    backup = target.read_text(encoding="utf-8")
    kills: dict[str, list[int]] = {nodeid: [] for nodeid in nodeids}
    survived: list[int] = []
    try:
        for number, original, mutated in candidates:
            source = backup.splitlines()
            source[number - 1] = mutated
            target.write_text("\n".join(source) + "\n", encoding="utf-8")
            failed = run_tests(nodeids, timeout=timeout)
            killers = [n for n in nodeids if _matches(failed, n)]
            if not killers:
                survived.append(number)
            for nodeid in killers:
                kills[nodeid].append(number)
    finally:
        # Restore unconditionally. A tool that breaks the tree on Ctrl-C is a
        # tool nobody runs twice.
        target.write_text(backup, encoding="utf-8")

    # A MINIMUM COVERING SET, not "which tests killed nothing".
    #
    # The naive rule -- retire any test with no unique kill -- is wrong in the
    # common case. If two tests both catch only mutant 7 and nothing else,
    # neither has a unique kill, so both look retirable and deleting both loses
    # mutant 7 entirely. What has to be preserved is the UNION of what the
    # cluster detects, using as few tests as possible.
    #
    # Greedy set cover: repeatedly take the test that catches the most
    # not-yet-covered mutants. Ties break on nodeid so the choice is stable and
    # a rerun proposes the same survivors.
    remaining = {n for killed in kills.values() for n in killed}
    keep: list[str] = []
    while remaining:
        best = max(
            sorted(nodeids),
            key=lambda nodeid: len(set(kills[nodeid]) & remaining),
        )
        gained = set(kills[best]) & remaining
        if not gained:
            break
        keep.append(best)
        remaining -= gained
    keep = sorted(keep)
    # Everything else: every mutant it catches is still caught by the kept set,
    # so retiring it removes no detection this evidence can see.
    retirable = sorted(set(nodeids) - set(keep))
    discriminating = len({n for killed in kills.values() for n in killed})
    unique = {
        nodeid: sorted(
            set(kills[nodeid])
            - {n for other, ks in kills.items() if other != nodeid for n in ks}
        )
        for nodeid in nodeids
        if set(kills[nodeid])
        - {n for other, ks in kills.items() if other != nodeid for n in ks}
    }
    return {
        "schema": SCHEMA,
        "file": str(target.relative_to(ROOT)),
        "mutations_applied": len(candidates),
        "mutations_no_test_caught": sorted(survived),
        "tests": nodeids,
        "kills": {k: v for k, v in kills.items() if v},
        # The smallest set that still catches everything the cluster catches.
        "must_keep": keep,
        # Tests that catch something NOTHING else catches. A subset of
        # must_keep, surfaced separately because these are the ones where the
        # cover had no choice -- deleting one provably loses detection.
        "irreplaceable": unique,
        # Every mutant these catch is still caught by must_keep, so retiring
        # them removes no detection this evidence can see.
        "retirable": retirable,
        # Withheld deliberately when nothing was caught: that says the
        # mutations were too weak OR the tests are, and those need different
        # answers. It must never be read as "delete the whole cluster".
        "verdict": "no_evidence" if not any(kills.values()) else "ok",
        "discriminating_mutations": discriminating,
        # HOW MUCH THIS IS WORTH. A cover of 1 test drawn from 3 discriminating
        # mutations is not proof that the other 26 are worthless -- it is proof
        # that these operators cannot tell them apart. Table-driven and
        # parametrised tests are the clearest case: each case asserts a
        # different input/output pair, and flipping one `==` breaks all of them
        # at once, so they all die together and look interchangeable.
        #
        # Retire on "strong" only. "weak" means go and write a mutation that
        # actually expresses the property in question, usually by altering a
        # value in the table rather than an operator in the code.
        "evidence": "strong" if discriminating >= 5 else "weak",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True, help="source file to mutate")
    parser.add_argument("--lines", required=True, help="comma-separated line numbers")
    parser.add_argument("--test", action="append", required=True, help="nodeid (repeatable)")
    parser.add_argument("--max-mutations", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(argv)

    target = args.file if args.file.is_absolute() else ROOT / args.file
    lines = {int(x) for x in args.lines.split(",") if x.strip()}
    result = analyse(
        args.test, target, lines, limit=args.max_mutations, timeout=args.timeout
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
