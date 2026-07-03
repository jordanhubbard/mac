"""Per-task model selection: by-name (preserved) + by-strength (new)."""

from __future__ import annotations

from mac.model_selection import ModelSelection, write_selection
from mac.worker import _task_model_override


def _persist_ladder(tmp_path, monkeypatch, ladder):
    monkeypatch.setenv("MAC_MODEL_SELECTION_FILE", str(tmp_path / "sel.json"))
    write_selection(ModelSelection(models=[ladder[-1]], source="dynamic", at="T", ladder=ladder))


def test_by_name_override_preserved(tmp_path, monkeypatch):
    _persist_ladder(tmp_path, monkeypatch, ["p/mini", "p/opus"])
    # An explicit name wins over everything (the faster/cheaper direct pin).
    task = {"metadata": {"model": "p/exact-choice", "model_strength": 10}}
    assert _task_model_override(task) == "p/exact-choice"


def test_by_strength_resolves_via_ladder(tmp_path, monkeypatch):
    _persist_ladder(tmp_path, monkeypatch, ["p/mini", "p/base", "p/opus"])
    assert _task_model_override({"metadata": {"model_strength": 1}}) == "p/mini"
    assert _task_model_override({"metadata": {"model_strength": 10}}) == "p/opus"


def test_strength_under_runtime_bag(tmp_path, monkeypatch):
    _persist_ladder(tmp_path, monkeypatch, ["p/mini", "p/opus"])
    assert _task_model_override({"metadata": {"runtime": {"model_strength": 10}}}) == "p/opus"


def test_no_pin_returns_empty(tmp_path, monkeypatch):
    _persist_ladder(tmp_path, monkeypatch, ["p/mini", "p/opus"])
    assert _task_model_override({"metadata": {}}) == ""


def test_strength_with_no_persisted_ladder_falls_through(tmp_path, monkeypatch):
    # No selection file -> strength can't resolve -> empty (fleet default applies).
    monkeypatch.setenv("MAC_MODEL_SELECTION_FILE", str(tmp_path / "absent.json"))
    assert _task_model_override({"metadata": {"model_strength": 8}}) == ""
