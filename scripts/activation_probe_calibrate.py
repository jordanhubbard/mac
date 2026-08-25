#!/usr/bin/env python3
"""Calibrate the external activation probe on a held-out JSONL split."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mac.activation_probe.calibration import (
    calibration_report,
    load_calibration_records,
)
from mac.activation_probe.classifier import ActivationProbeClassifier


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", default="docs/activation-probe/calibration-report.json")
    parser.add_argument("--bins", type=int, default=10)
    args = parser.parse_args()

    classifier = ActivationProbeClassifier.load(args.checkpoint)
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
