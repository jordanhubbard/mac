#!/usr/bin/env python3
"""Run the J-lens probe over a strictly held-out JSONL calibration split."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mac.jlens.calibration import calibration_report, load_calibration_records
from mac.jlens.classifier import JLensClassifier


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument(
        "--output", default="docs/jlens/calibration-report.json"
    )
    parser.add_argument("--bins", type=int, default=10)
    args = parser.parse_args()

    classifier = JLensClassifier.load(args.checkpoint)
    report = calibration_report(
        classifier,
        load_calibration_records(args.dataset),
        bins=args.bins,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
