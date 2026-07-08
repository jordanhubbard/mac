from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from mac import worker
from mac.jlens.advisory import advisory_audit_from_environment
from mac.jlens.calibration import (
    accuracy_at_threshold,
    auroc,
    calibration_report,
    expected_calibration_error,
    load_calibration_records,
)
from mac.jlens.classifier import JLensClassifier
from mac.jlens.runtime import ForwardHookActivationExtractor


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "jlens"


class _Handle:
    def __init__(self, hooks, hook):
        self.hooks, self.hook = hooks, hook

    def remove(self):
        self.hooks.remove(self.hook)


class _Layer:
    def __init__(self):
        self.hooks = []

    def register_forward_hook(self, hook):
        self.hooks.append(hook)
        return _Handle(self.hooks, hook)

    def emit(self, value):
        for hook in list(self.hooks):
            hook(self, (), value)


class _Model:
    def __init__(self, layer):
        self.layer = layer

    def __call__(self, value):
        self.layer.emit(value)


def test_runtime_captures_per_token_activations_and_removes_hook():
    layer = _Layer()
    extractor = ForwardHookActivationExtractor(_Model(layer), {"resid.0": layer})
    result = extractor.capture(np.array([[[1.0, 2.0], [3.0, 4.0]]]))
    assert result.layer("resid.0").shape == (2, 2)
    assert result.mean_pool("resid.0").tolist() == [2.0, 3.0]
    assert layer.hooks == []


def test_classifier_accepts_sequence_or_summary_and_disabled_checkpoint():
    classifier = JLensClassifier.load(FIXTURES / "checkpoint.json")
    sequence = classifier.predict([[2.0, -2.0], [1.0, -1.0]])
    summary = classifier.predict([1.5, -1.5])
    assert 0.0 <= sequence.score <= 1.0
    assert sequence.label == "flagged"
    assert sequence.score == pytest.approx(summary.score)
    assert JLensClassifier.load(None).predict([0.0]).label == "disabled"


def test_classifier_rejects_dimension_mismatch():
    classifier = JLensClassifier.load(FIXTURES / "checkpoint.json")
    with pytest.raises(ValueError, match="dimension mismatch"):
        classifier.predict([1.0, 2.0, 3.0])


def test_calibration_split_and_metrics():
    records = load_calibration_records(FIXTURES / "calibration.jsonl")
    report = calibration_report(
        JLensClassifier.load(FIXTURES / "checkpoint.json"), records, bins=5
    )
    assert report["training_data_used"] is False
    assert report["metrics"]["accuracy"] == 1.0
    assert report["metrics"]["auroc"] == 1.0
    assert 0.0 <= report["metrics"]["ece"] <= 1.0
    assert auroc([0.1, 0.9], [0, 1]) == 1.0
    assert accuracy_at_threshold([0.1, 0.9], [0, 1], threshold=0.5) == 1.0
    assert expected_calibration_error([0.1, 0.9], [0, 1], bins=2) == pytest.approx(0.1)


def test_calibration_loader_rejects_training_records(tmp_path):
    path = tmp_path / "mixed.jsonl"
    path.write_text(json.dumps({"split": "train", "activations": [0], "label": 0}))
    with pytest.raises(ValueError, match="non-calibration"):
        load_calibration_records(path)


def test_calibration_cli_writes_report(tmp_path):
    output = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "jlens_calibrate.py"),
            "--checkpoint",
            str(FIXTURES / "checkpoint.json"),
            "--dataset",
            str(FIXTURES / "calibration.jsonl"),
            "--output",
            str(output),
            "--bins",
            "5",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text())["schema"] == "mac.jlens.calibration.v1"


def test_advisory_audit_is_disabled_by_default_and_attaches_when_enabled(tmp_path):
    metadata = {"jlens_activations": [[2.0, -2.0], [1.0, -1.0]]}
    assert advisory_audit_from_environment(tmp_path, metadata, env={}) is None
    result = advisory_audit_from_environment(
        tmp_path,
        metadata,
        env={
            "MAC_JLENS_ENABLED": "1",
            "MAC_JLENS_CHECKPOINT": str(FIXTURES / "checkpoint.json"),
        },
    )
    assert result["label"] == "flagged"
    assert result["advisory_only"] is True
    assert result["runtime_ms"] >= 0.0
    disabled = advisory_audit_from_environment(
        tmp_path, {}, env={"MAC_JLENS_ENABLED": "1"}
    )
    assert disabled["label"] == "disabled"


def test_advisory_error_never_propagates_to_worker_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_JLENS_ENABLED", "1")
    monkeypatch.setenv("MAC_JLENS_CHECKPOINT", str(tmp_path / "missing.json"))
    instance = object.__new__(worker.MacWorker)
    instance.attestation_key = ""
    (tmp_path / "stdout.txt").write_text("")
    (tmp_path / "stderr.txt").write_text("")
    execution = worker.WorkerExecution(returncode=0, summary="ok")
    metadata = instance._execution_metadata(tmp_path, execution)
    assert metadata["verification"]["status"] == "missing"
    assert "jlens_audit" not in metadata


def test_worker_evidence_attaches_only_bounded_advisory_result(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_JLENS_ENABLED", "1")
    monkeypatch.setenv("MAC_JLENS_CHECKPOINT", str(FIXTURES / "checkpoint.json"))
    instance = object.__new__(worker.MacWorker)
    instance.attestation_key = ""
    (tmp_path / "stdout.txt").write_text("")
    (tmp_path / "stderr.txt").write_text("")
    execution = worker.WorkerExecution(
        returncode=0,
        summary="ok",
        metadata={"jlens_activations": [[2.0, -2.0], [1.0, -1.0]]},
    )
    metadata = instance._execution_metadata(tmp_path, execution)
    assert metadata["jlens_audit"]["advisory_only"] is True
    assert metadata["jlens_audit"]["label"] == "flagged"
    assert "jlens_activations" not in metadata
