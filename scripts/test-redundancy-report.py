#!/usr/bin/env python3
"""Rank tests that are candidates for retirement, with the cost of keeping them.

WHY THIS EXISTS

The suite grew for over a year by adding tests alongside every code change, and
nothing was ever removed: there has never been a gate for retiring a test. The
result is 11,020 tests over 239,696 lines of test code for 223,016 lines of
source -- more test than code.

WHAT THE COST ACTUALLY IS

Not assertions. Test bodies total 34.2 minutes and 47.6% of tests finish in
under 10ms, which cannot explain a 63-minute job. Measured marginally, each
test costs about 0.6s no matter what it asserts, because each one builds its
own Postgres schema. Wall time therefore scales with the NUMBER of tests, so
removing a redundant test is worth as much as removing a slow one.

WHAT THIS REPORTS, AND WHAT IT DOES NOT CLAIM

Tests are grouped by coverage signature: the exact set of (file, line) pairs
they execute. Tests sharing a signature are CANDIDATES, not proven duplicates
-- two tests can execute identical lines and assert entirely different things,
which is the difference between "same code ran" and "same property checked".

Deciding that is what scripts/test-discrimination.py does, by mutating the
covered lines and seeing which tests actually notice. This report only says
where to look, ordered by what it costs to keep looking away.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = ROOT / "src" / "mac" / "data" / "test_impact_map.json"
DEFAULT_TIMINGS = ROOT / ".test-portfolio" / "timings.json"
SCHEMA = "mac.test_redundancy.v1"

#: Marginal wall-clock cost of one test, measured rather than assumed: 15 tests
#: took 5.5s and 36 took 18.5s in the same process, so each additional test
#: costs ~0.6s of fixture work (predominantly its own Postgres schema) before
#: it asserts anything at all.
FIXTURE_COST_SECONDS = 0.6


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def coverage_signatures(impact_map: dict) -> dict[str, frozenset]:
    """nodeid -> the exact set of (file, line) pairs it executed."""
    nodeids = impact_map.get("nodeids") or []
    per_test: dict[int, set] = collections.defaultdict(set)
    for filename, lines in (impact_map.get("file_line_tests") or {}).items():
        for line, indices in lines.items():
            for index in indices:
                per_test[index].add((filename, line))
    return {
        nodeids[index]: frozenset(pairs)
        for index, pairs in per_test.items()
        if 0 <= index < len(nodeids)
    }


def durations(timings: dict) -> dict[str, float]:
    return {
        str(entry.get("nodeid")): float(entry.get("duration_seconds") or 0.0)
        for entry in (timings.get("tests") or [])
        if entry.get("nodeid")
    }


def clusters(signatures: dict[str, frozenset]) -> list[list[str]]:
    grouped: dict[frozenset, list[str]] = collections.defaultdict(list)
    for nodeid, signature in signatures.items():
        if signature:
            grouped[signature].append(nodeid)
    return [sorted(members) for members in grouped.values() if len(members) > 1]


#: A signature this small is INCIDENTAL, not evidence. The largest raw cluster
#: was 49 tests sharing 3 lines across 6 unrelated test files -- tests that
#: barely touch mapped source at all (they exercise vendored code or assert on
#: pure data), so their coverage matches by accident. Ranking those first sends
#: the reviewer to the one place there is nothing to find.
MIN_SIGNIFICANT_LINES = 10


def cluster_report(members: list[str], timing: dict[str, float], signature: frozenset) -> dict:
    """One cluster, with what retiring all but one of its tests would save.

    Deliberately keeps ONE member: the code it covers must still be exercised.
    The saving is therefore over the redundant members only.
    """
    redundant = members[1:]
    saved = sum(timing.get(nodeid, 0.0) + FIXTURE_COST_SECONDS for nodeid in redundant)
    files = sorted({filename for filename, _line in signature})
    test_files = sorted({nodeid.split("::", 1)[0] for nodeid in members})
    return {
        "members": members,
        "size": len(members),
        "covered_lines": len(signature),
        "covered_files": files[:5],
        # Tests of the same subject living in the same file are far likelier to
        # be genuine duplicates than a coincidental match across unrelated
        # suites, so this is surfaced for the reviewer rather than hidden.
        "test_files": test_files,
        "same_test_file": len(test_files) == 1,
        "keep": members[0],
        "candidates": redundant,
        "seconds_saved_if_retired": round(saved, 2),
    }


def build_report(
    impact_map: dict,
    timings: dict,
    *,
    limit: int = 0,
    min_lines: int = MIN_SIGNIFICANT_LINES,
) -> dict:
    signatures = coverage_signatures(impact_map)
    timing = durations(timings)
    every = [cluster_report(members, timing, sig) for sig, members in _grouped(signatures).items()]
    found = [c for c in every if c["covered_lines"] >= min_lines]
    # Rank same-file clusters first at equal value: those are the ones a
    # reviewer can actually adjudicate quickly.
    found.sort(key=lambda item: (-item["seconds_saved_if_retired"], not item["same_test_file"]))
    total_candidates = sum(len(item["candidates"]) for item in found)
    total_saved = sum(item["seconds_saved_if_retired"] for item in found)
    return {
        "schema": SCHEMA,
        "tests_with_coverage": len(signatures),
        "distinct_signatures": len(set(signatures.values())),
        "min_significant_lines": min_lines,
        "clusters_all": len(every),
        "clusters": len(found),
        "candidates_all": sum(len(c["candidates"]) for c in every),
        "retirement_candidates": total_candidates,
        "seconds_saved_if_all_retired": round(total_saved, 1),
        "minutes_saved_if_all_retired": round(total_saved / 60.0, 1),
        "fixture_cost_seconds_per_test": FIXTURE_COST_SECONDS,
        # Ordered so the first entries are worth the most; verification is
        # per-cluster, so a partial pass still banks the largest savings.
        "top_clusters": found[:limit] if limit else found,
    }


def _grouped(signatures: dict[str, frozenset]) -> dict[frozenset, list[str]]:
    grouped: dict[frozenset, list[str]] = collections.defaultdict(list)
    for nodeid, signature in signatures.items():
        if signature:
            grouped[signature].append(nodeid)
    return {sig: sorted(members) for sig, members in grouped.items() if len(members) > 1}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--timings", type=Path, default=DEFAULT_TIMINGS)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument(
        "--min-lines",
        type=int,
        default=MIN_SIGNIFICANT_LINES,
        help="ignore clusters whose shared coverage is smaller than this",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    impact_map = load_json(args.map)
    timings = load_json(args.timings) if args.timings.exists() else {"tests": []}
    report = build_report(impact_map, timings, limit=args.limit, min_lines=args.min_lines)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(
            "%d clusters, %d retirement candidates, %.1f minutes -> %s"
            % (
                report["clusters"],
                report["retirement_candidates"],
                report["minutes_saved_if_all_retired"],
                args.output,
            )
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
