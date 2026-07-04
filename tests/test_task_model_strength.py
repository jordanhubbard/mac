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


def test_review_model_is_independent_from_executor_model():
    review = {
        "metadata": {
            "model": "author/model",
            "review_context": {"review_id": "review_1"},
        }
    }
    assert _task_model_override(review) == ""

    review["metadata"]["review_model"] = "reviewer/model"
    assert _task_model_override(review) == "reviewer/model"


def test_strength_with_no_persisted_ladder_falls_through(tmp_path, monkeypatch):
    # No selection file AND no hub client -> strength can't resolve -> empty
    # (fleet default applies).
    monkeypatch.setenv("MAC_MODEL_SELECTION_FILE", str(tmp_path / "absent.json"))
    assert _task_model_override({"metadata": {"model_strength": 8}}) == ""


class _FakeHubClient:
    """Serves /model-selection/status like the hub does, for spoke-fallback tests."""

    def __init__(self, ladder):
        self._ladder = list(ladder)
        self.calls = 0

    def get(self, path):
        self.calls += 1
        assert path == "/model-selection/status"
        return {"active": {"models": [self._ladder[-1]], "ladder": self._ladder}}


def test_strength_resolves_from_hub_when_no_local_file(tmp_path, monkeypatch):
    # A SPOKE worker has no local selection file; without the hub fallback the
    # --model-strength pin was silently dropped. It must resolve via the hub's
    # active ladder instead.
    monkeypatch.setenv("MAC_MODEL_SELECTION_FILE", str(tmp_path / "absent.json"))
    client = _FakeHubClient(["p/mini", "p/base", "p/opus"])
    assert _task_model_override({"metadata": {"model_strength": 10}}, hub_client=client) == "p/opus"
    assert _task_model_override({"metadata": {"model_strength": 1}}, hub_client=client) == "p/mini"
    assert client.calls == 2


def test_local_ladder_wins_over_hub(tmp_path, monkeypatch):
    # When a local ladder IS present (co-located hub process) it is used and the
    # hub is never queried.
    _persist_ladder(tmp_path, monkeypatch, ["l/mini", "l/opus"])
    client = _FakeHubClient(["h/mini", "h/opus"])
    assert _task_model_override({"metadata": {"model_strength": 10}}, hub_client=client) == "l/opus"
    assert client.calls == 0
