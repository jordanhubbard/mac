"""Held-out calibration helpers for the external activation probe."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .classifier import ActivationProbeClassifier


def expected_calibration_error(
    scores: Sequence[float], labels: Sequence[int], *, bins: int = 10
) -> float:
    """Compute the expected calibration error of scores against binary labels."""
    if len(scores) != len(labels) or not scores:
        raise ValueError("scores and labels must have equal nonzero length")
    if bins < 1:
        raise ValueError("bins must be positive")
    total = len(scores)
    error = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        members = [
            i
            for i, score in enumerate(scores)
            if low <= score < high or (index == bins - 1 and score == 1.0)
        ]
        if not members:
            continue
        confidence = sum(scores[i] for i in members) / len(members)
        accuracy = sum(labels[i] for i in members) / len(members)
        error += len(members) / total * abs(accuracy - confidence)
    return error


def auroc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """Compute the area under the ROC curve for scores and binary labels."""
    positives = [score for score, label in zip(scores, labels) if int(label) == 1]
    negatives = [score for score, label in zip(scores, labels) if int(label) == 0]
    if not positives or not negatives:
        raise ValueError("AUROC requires positive and negative examples")
    wins = 0.0
    for positive in positives:
        for negative in negatives:
            wins += 1.0 if positive > negative else 0.5 if positive == negative else 0.0
    return wins / (len(positives) * len(negatives))


def accuracy_at_threshold(
    scores: Sequence[float], labels: Sequence[int], *, threshold: float
) -> float:
    """Compute classification accuracy for scores at the given decision threshold."""
    if len(scores) != len(labels) or not scores:
        raise ValueError("scores and labels must have equal nonzero length")
    correct = sum(
        (score >= threshold) == bool(label) for score, label in zip(scores, labels)
    )
    return correct / len(scores)


def load_calibration_records(path: str | Path) -> list[Mapping[str, Any]]:
    """Load held-out calibration records from a JSONL file at the given path."""
    records = []
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("split") != "calibration":
            raise ValueError(
                "held-out calibration file contains non-calibration record on line %d"
                % line_number
            )
        records.append(record)
    if not records:
        raise ValueError("calibration dataset is empty")
    return records


def calibration_report(
    classifier: ActivationProbeClassifier,
    records: Iterable[Mapping[str, Any]],
    *,
    bins: int = 10,
) -> dict[str, Any]:
    """Build a calibration metrics report for the classifier over the given records."""
    materialized = list(records)
    predictions = [classifier.predict(record["activations"]) for record in materialized]
    scores = [prediction.score for prediction in predictions]
    labels = [int(record["label"]) for record in materialized]
    return {
        "schema": "mac.activation_probe.calibration.v1",
        "split": "calibration",
        "training_data_used": False,
        "examples": len(materialized),
        "threshold": classifier.threshold,
        "metrics": {
            "ece": expected_calibration_error(scores, labels, bins=bins),
            "auroc": auroc(scores, labels),
            "accuracy": accuracy_at_threshold(
                scores, labels, threshold=classifier.threshold
            ),
        },
    }
