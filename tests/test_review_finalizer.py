"""Contract tests for the shared deterministic review finalizer bridge."""

from __future__ import annotations

import json

import pytest

from mac import review_finalizer


def _write_task(tmp_path, payload) -> None:
    (tmp_path / "task.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(("status", "expected"), [("complete", 0), ("failed", 1)])
def test_main_runs_finalizer_and_returns_manifest_status(
    tmp_path, monkeypatch, status, expected
) -> None:
    review_context = {"executor_evidence_id": "ev-executor"}
    task = {"id": "task", "metadata": {"review_context": review_context}}
    _write_task(tmp_path, {"task": task})
    monkeypatch.setenv("MAC_TASK_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("MAC_TASK_FILE", raising=False)
    captured = {}

    def finalize(workspace, received_task, received_context):
        captured.update(
            workspace=workspace,
            task=received_task,
            review_context=received_context,
        )
        (workspace / "mac-evidence.json").write_text(
            json.dumps({"status": status}), encoding="utf-8"
        )

    monkeypatch.setattr(review_finalizer, "run_deterministic_review_verdict", finalize)

    assert review_finalizer.main() == expected
    assert captured == {
        "workspace": tmp_path,
        "task": task,
        "review_context": review_context,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"task": "not-a-task"},
        {"task": {"id": "task", "metadata": {}}},
    ],
)
def test_main_rejects_missing_review_context(tmp_path, monkeypatch, payload) -> None:
    _write_task(tmp_path, payload)
    monkeypatch.setenv("MAC_TASK_WORKSPACE", str(tmp_path))
    with pytest.raises(SystemExit, match="requires a review task"):
        review_finalizer.main()


def test_main_requires_finalizer_manifest(tmp_path, monkeypatch) -> None:
    task_file = tmp_path / "custom-task.json"
    task_file.write_text(
        json.dumps({"metadata": {"review_context": {"id": "review"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAC_TASK_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MAC_TASK_FILE", str(task_file))
    monkeypatch.setattr(
        review_finalizer, "run_deterministic_review_verdict", lambda *_a: None
    )

    with pytest.raises(SystemExit, match="did not produce"):
        review_finalizer.main()
