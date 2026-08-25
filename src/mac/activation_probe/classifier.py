"""Small logistic classifier for externally supplied model activations."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np


@dataclass(frozen=True)
class ActivationProbePrediction:
    score: float
    label: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "label": self.label,
            "confidence": self.confidence,
        }


class ActivationProbeClassifier:
    """Mean-pooled linear/logistic probe.

    Checkpoints are small JSON documents with ``weights``, ``bias``,
    ``threshold``, and optional label strings.  ``load(None)`` returns a
    disabled neutral probe, making the feature safe-by-default.
    """

    def __init__(
        self,
        weights: Optional[Sequence[float]] = None,
        *,
        bias: float = 0.0,
        threshold: float = 0.5,
        negative_label: str = "typical",
        positive_label: str = "flagged",
        checkpoint: str = "",
    ) -> None:
        self.weights = None if weights is None else np.asarray(weights, dtype=float)
        if self.weights is not None and self.weights.ndim != 1:
            raise ValueError("classifier weights must be a vector")
        if not 0.0 < float(threshold) < 1.0:
            raise ValueError("classifier threshold must be between 0 and 1")
        self.bias = float(bias)
        self.threshold = float(threshold)
        self.negative_label = str(negative_label)
        self.positive_label = str(positive_label)
        self.checkpoint = str(checkpoint)

    @property
    def enabled(self) -> bool:
        return self.weights is not None

    @classmethod
    def load(cls, checkpoint_path: Optional[str | Path]) -> "ActivationProbeClassifier":
        if checkpoint_path is None or not str(checkpoint_path).strip():
            return cls()
        path = Path(checkpoint_path).expanduser()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError("activation-probe checkpoint must contain a JSON object")
        return cls(
            data.get("weights"),
            bias=float(data.get("bias", 0.0)),
            threshold=float(data.get("threshold", 0.5)),
            negative_label=str(data.get("negative_label", "typical")),
            positive_label=str(data.get("positive_label", "flagged")),
            checkpoint=str(path),
        )

    def predict(self, activations: Any) -> ActivationProbePrediction:
        if not self.enabled:
            return ActivationProbePrediction(score=0.5, label="disabled", confidence=0.0)
        array = np.asarray(activations, dtype=float)
        if array.ndim == 2:
            array = array.mean(axis=0)
        if array.ndim != 1:
            raise ValueError("classifier input must be [seq_len, hidden_dim] or [hidden_dim]")
        if array.shape[0] != self.weights.shape[0]:
            raise ValueError(
                "classifier hidden dimension mismatch: expected %d, got %d"
                % (self.weights.shape[0], array.shape[0])
            )
        if not np.isfinite(array).all():
            raise ValueError("classifier input contains non-finite values")
        logit = float(np.dot(self.weights, array) + self.bias)
        score = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, logit))))
        label = self.positive_label if score >= self.threshold else self.negative_label
        denominator = self.threshold if score < self.threshold else 1.0 - self.threshold
        confidence = min(1.0, abs(score - self.threshold) / max(denominator, 1e-9))
        return ActivationProbePrediction(score=score, label=label, confidence=confidence)
