#!/usr/bin/env python3
"""CLI over mac.test_checkpoint for scripts/run-contract-tests.sh.

Three verbs:

  plan   decide whether this run may resume from the stored checkpoint, print
         the human-auditable explanation on stdout, and (on a resume) write the
         carried-forward test-file list for the conftest deselection hook.
         Exit 0 => resume, exit 10 => run everything. Any unexpected error also
         exits 10, because failing OPEN is the whole safety property.

  record fold a finished run's per-test outcomes (the JSONL the conftest hook
         appended) into a new checkpoint document.

  show   print the stored checkpoint's summary, for operators and CI logs.

The rules themselves live in src/mac/test_checkpoint.py, next to the docstring
that justifies them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mac import test_checkpoint as tc  # noqa: E402

EXIT_RESUME = 0
EXIT_FULL = 10


def _directory(args) -> Path:
    return Path(args.dir) if Path(args.dir).is_absolute() else ROOT / args.dir


def cmd_plan(args) -> int:
    directory = _directory(args)
    try:
        decision = tc.plan(
            repo_root=ROOT,
            directory=directory,
            require_whole_coverage=bool(args.require_whole_coverage),
        )
    except Exception as exc:  # noqa: BLE001 - fail open, loudly
        print("test checkpoint: full (planner_error: %s)" % exc)
        return EXIT_FULL
    print(tc.render_plan(decision))
    if args.json:
        Path(args.json).write_text(
            json.dumps(decision.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if decision.mode != "resume":
        return EXIT_FULL
    if args.skip_file:
        Path(args.skip_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.skip_file).write_text("\n".join(decision.skip_files) + "\n", encoding="utf-8")
    return EXIT_RESUME


def cmd_record(args) -> int:
    directory = _directory(args)
    outcomes = tc.ingest_results(tc.results_dir(directory))
    if not outcomes:
        print("test checkpoint: no outcomes recorded; leaving any existing checkpoint alone")
        return 0
    resolver = tc.load_resolver(ROOT)
    impact_map = None
    if resolver is not None:
        try:
            impact_map = resolver.load_map(resolver.load_policy().map_path)
        except Exception:
            impact_map = None
    document = tc.build_checkpoint(
        repo_root=ROOT, outcomes=outcomes, gate=args.gate, impact_map=impact_map
    )
    if document is None:
        print("test checkpoint: tree manifest unavailable; not writing a checkpoint")
        return 0
    if args.carried_forward_file:
        carried = tc.read_carried_forward(Path(args.carried_forward_file))
        merged = tc.merge_carried_forward(
            document, tc.load_checkpoint(directory), carried, repo_root=ROOT
        )
        if merged is None:
            print(
                "test checkpoint: previous checkpoint no longer valid for this tree; "
                "recording only the tests this run actually executed"
            )
        else:
            document = merged
    tc.write_checkpoint(directory, document)
    stats = document["stats"]
    print(
        "test checkpoint: recorded %d tests across %d files (%d failed) for gate '%s'"
        % (stats["recorded_tests"], stats["recorded_files"], stats["failed_tests"], args.gate)
    )
    return 0


def cmd_show(args) -> int:
    document = tc.load_checkpoint(_directory(args))
    if document is None:
        print("test checkpoint: none stored")
        return 1
    print(
        json.dumps(
            {
                "gate": document.get("gate"),
                "head_sha": document.get("head_sha"),
                "runner_fingerprint": document.get("runner_fingerprint"),
                "stats": document.get("stats"),
                "failed_tests": document.get("failed_tests"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=tc.DEFAULT_DIR, help="checkpoint directory")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument(
        "--require-whole-coverage",
        action="store_true",
        help="this gate enforces whole-repo coverage floors, so a resume is a "
        "triage pass only and must be followed by the complete gate",
    )
    plan.add_argument("--skip-file", help="write the carried-forward test files here")
    plan.add_argument("--json", help="write the full plan document here")
    plan.set_defaults(func=cmd_plan)

    record = sub.add_parser("record")
    record.add_argument("--gate", default="unknown")
    record.add_argument(
        "--carried-forward-file",
        help="the skip list this run resumed with; those files' previous results "
        "are folded into the new checkpoint so a second resume still knows them",
    )
    record.set_defaults(func=cmd_record)

    show = sub.add_parser("show")
    show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
