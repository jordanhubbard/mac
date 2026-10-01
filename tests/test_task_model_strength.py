"""Per-task model selection: by-name pins route; ``model_strength`` is advisory.

The strength ladder that once mapped ``--model-strength`` to a concrete model
was removed. Existing callers and tasks may still carry the value, so the CLI
keeps recording it and the worker must ignore it (fleet default model) rather
than crash or route on it.
"""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.test_support import dsn_for
from mac.worker import _task_model_override


def test_by_name_override_preserved():
    # An explicit name wins; a strength alongside it changes nothing.
    task = {"metadata": {"model": "p/exact-choice", "model_strength": 10}}
    assert _task_model_override(task) == "p/exact-choice"


def test_strength_alone_falls_back_to_fleet_default():
    # Empty string means "no pin": the agent's fleet default model applies.
    assert _task_model_override({"metadata": {"model_strength": 9}}) == ""
    assert _task_model_override({"metadata": {"model_strength": 1}}) == ""
    assert _task_model_override({"metadata": {"model_strength": "not-a-number"}}) == ""
    assert _task_model_override({"metadata": {"runtime": {"model_strength": 10}}}) == ""


def test_runtime_model_still_honored_with_strength():
    task = {"metadata": {"model_strength": 9, "runtime": {"model": "p/runtime"}}}
    assert _task_model_override(task) == "p/runtime"


def test_no_pin_returns_empty():
    assert _task_model_override({"metadata": {}}) == ""
    assert _task_model_override({}) == ""


def test_review_model_is_independent_from_executor_model():
    review = {
        "metadata": {
            "model": "author/model",
            "review_model_strength": 9,
            "review_context": {"review_id": "review_1"},
        }
    }
    assert _task_model_override(review) == ""

    review["metadata"]["review_model"] = "reviewer/model"
    assert _task_model_override(review) == "reviewer/model"


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def test_cli_still_accepts_and_records_model_strength(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_SECRET_KEY", "cli-test-key-with-at-least-32-characters")
    rc, task = _run(tmp_path, "task", "create", "strength task", "--model-strength", "9")
    assert rc == 0, task
    assert task["metadata"]["model_strength"] == 9
    assert "model" not in task["metadata"]
    assert _task_model_override(task) == ""
