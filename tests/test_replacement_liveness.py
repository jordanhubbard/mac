"""Tests for replacement-chain walker and write guard (repository_hygiene.py)."""
from __future__ import annotations

import pytest

from mac.models import ValidationError
from mac.repository_hygiene import (
    ReplacementLivenessResult,
    validate_replacement_target,
    walk_replacement_chain,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TASK_A = "task_" + "a" * 32
TASK_B = "task_" + "b" * 32
TASK_C = "task_" + "c" * 32
TASK_D = "task_" + "d" * 32


def _make_task(state: str, replacement: str = "", no_dispatch: bool = False):
    metadata = {}
    if no_dispatch:
        metadata["no_dispatch"] = True
    if replacement:
        metadata["repository_ref_lifecycle"] = {"replacement_task_id": replacement}
    return {"task": {"state": state, "metadata": metadata}}


def _loader(db: dict):
    """Return a get_task_fn backed by ``db``."""
    def get_task(task_id: str):
        return db.get(task_id)
    return get_task


# ---------------------------------------------------------------------------
# walk_replacement_chain: basic status classifications
# ---------------------------------------------------------------------------


def test_walk_completed_is_satisfied():
    db = {TASK_A: _make_task("completed")}
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "satisfied"
    assert result.chain == [TASK_A]
    assert result.remediation == ""


def test_walk_open_task_is_live():
    db = {TASK_A: _make_task("open")}
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "live"
    assert result.chain == [TASK_A]


def test_walk_running_task_is_live():
    db = {TASK_A: _make_task("running")}
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "live"


def test_walk_claimed_task_is_live():
    db = {TASK_A: _make_task("claimed")}
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "live"


def test_walk_cancelled_no_replacement_is_stranded():
    db = {TASK_A: _make_task("cancelled")}
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "stranded"
    assert TASK_A in result.chain
    assert "replacement_task_id" in result.remediation


def test_walk_failed_no_replacement_is_stranded():
    db = {TASK_A: _make_task("failed")}
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "stranded"


def test_walk_held_task_is_held():
    db = {TASK_A: _make_task("open", no_dispatch=True)}
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "held"
    assert "no_dispatch" in result.remediation


def test_walk_unloadable_is_missing():
    result = walk_replacement_chain(TASK_A, _loader({}))
    assert result.status == "missing"
    assert TASK_A in result.chain


def test_walk_exception_in_loader_is_missing():
    def bad_loader(task_id):
        raise RuntimeError("db down")
    result = walk_replacement_chain(TASK_A, bad_loader)
    assert result.status == "missing"


# ---------------------------------------------------------------------------
# walk_replacement_chain: chain traversal
# ---------------------------------------------------------------------------


def test_walk_follows_replacement_chain_to_live():
    db = {
        TASK_A: _make_task("cancelled", replacement=TASK_B),
        TASK_B: _make_task("running"),
    }
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "live"
    assert result.chain == [TASK_A, TASK_B]


def test_walk_follows_chain_to_satisfied():
    db = {
        TASK_A: _make_task("cancelled", replacement=TASK_B),
        TASK_B: _make_task("cancelled", replacement=TASK_C),
        TASK_C: _make_task("completed"),
    }
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "satisfied"
    assert result.chain == [TASK_A, TASK_B, TASK_C]


def test_walk_detects_cycle():
    db = {
        TASK_A: _make_task("cancelled", replacement=TASK_B),
        TASK_B: _make_task("cancelled", replacement=TASK_A),
    }
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "cycle"
    assert TASK_A in result.chain
    assert TASK_B in result.chain
    assert "cycle" in result.remediation.lower()


def test_walk_depth_cap_returns_cycle():
    # Build a chain of 11 unique tasks (longer than the cap of 10).
    # Task IDs must use hex chars only (0-9a-f); use incrementing hex digits.
    ids = ["task_" + format(i, "032x") for i in range(11)]
    db = {}
    for i, tid in enumerate(ids[:-1]):
        db[tid] = _make_task("cancelled", replacement=ids[i + 1])
    db[ids[-1]] = _make_task("open")

    result = walk_replacement_chain(ids[0], _loader(db))
    assert result.status == "cycle"
    assert "depth limit" in result.remediation


# ---------------------------------------------------------------------------
# walk_replacement_chain: blocked_by_terminal
# ---------------------------------------------------------------------------


def test_walk_blocked_by_terminal_dep():
    def get_task(tid):
        return _make_task("open")

    def get_deps(tid):
        return [_make_task("failed")]

    result = walk_replacement_chain(TASK_A, get_task, get_deps)
    assert result.status == "blocked_by_terminal"
    assert "terminal state" in result.remediation


def test_walk_cancelled_dep_is_blocked_by_terminal():
    def get_task(tid):
        return _make_task("open")

    def get_deps(tid):
        return [_make_task("cancelled")]

    result = walk_replacement_chain(TASK_A, get_task, get_deps)
    assert result.status == "blocked_by_terminal"


def test_walk_deps_exception_is_silently_ignored():
    db = {TASK_A: _make_task("running")}

    def bad_deps(tid):
        raise RuntimeError("deps unavailable")

    result = walk_replacement_chain(TASK_A, _loader(db), bad_deps)
    assert result.status == "live"


def test_walk_non_terminal_dep_does_not_block():
    def get_task(tid):
        return _make_task("open")

    def get_deps(tid):
        return [_make_task("running"), _make_task("open")]

    result = walk_replacement_chain(TASK_A, get_task, get_deps)
    assert result.status == "live"


# ---------------------------------------------------------------------------
# walk_replacement_chain: unknown/blocked state treated as live
# ---------------------------------------------------------------------------


def test_walk_blocked_state_treated_as_live():
    db = {TASK_A: _make_task("blocked")}
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "live"


def test_walk_unknown_state_treated_as_live():
    db = {TASK_A: _make_task("some_future_state")}
    result = walk_replacement_chain(TASK_A, _loader(db))
    assert result.status == "live"


# ---------------------------------------------------------------------------
# validate_replacement_target: basic acceptance
# ---------------------------------------------------------------------------


def test_validate_accepts_live_replacement():
    db = {TASK_B: _make_task("open")}
    # Should not raise
    validate_replacement_target(TASK_B, _loader(db))


def test_validate_accepts_running_replacement():
    db = {TASK_B: _make_task("running")}
    validate_replacement_target(TASK_B, _loader(db))


def test_validate_accepts_unloadable_replacement_fail_open():
    # When the task cannot be loaded we cannot confirm it's bad — fail open.
    validate_replacement_target(TASK_B, _loader({}))


# ---------------------------------------------------------------------------
# validate_replacement_target: rejection paths
# ---------------------------------------------------------------------------


def test_validate_rejects_cancelled_target():
    db = {TASK_B: _make_task("cancelled")}
    with pytest.raises(ValidationError, match="already terminal"):
        validate_replacement_target(TASK_B, _loader(db))


def test_validate_rejects_failed_target():
    db = {TASK_B: _make_task("failed")}
    with pytest.raises(ValidationError, match="already terminal"):
        validate_replacement_target(TASK_B, _loader(db))


def test_validate_rejects_held_target():
    db = {TASK_B: _make_task("open", no_dispatch=True)}
    with pytest.raises(ValidationError, match="held"):
        validate_replacement_target(TASK_B, _loader(db))


def test_validate_rejects_invalid_task_id_format():
    with pytest.raises(ValidationError, match="task_<32 hex>"):
        validate_replacement_target("not-a-task-id", _loader({}))


def test_validate_rejects_empty_task_id():
    with pytest.raises(ValidationError, match="task_<32 hex>"):
        validate_replacement_target("", _loader({}))


# ---------------------------------------------------------------------------
# validate_replacement_target: archival_override
# ---------------------------------------------------------------------------


def test_validate_archival_override_bypasses_terminal_guard():
    db = {TASK_B: _make_task("cancelled")}
    # Should not raise when archival_override is set
    validate_replacement_target(TASK_B, _loader(db), archival_override=True)


def test_validate_archival_override_bypasses_held_guard():
    db = {TASK_B: _make_task("open", no_dispatch=True)}
    validate_replacement_target(TASK_B, _loader(db), archival_override=True)


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_replacement_liveness_result_is_frozen():
    result = ReplacementLivenessResult(status="live", chain=[TASK_A], remediation="")
    with pytest.raises((AttributeError, TypeError)):
        result.status = "other"  # type: ignore[misc]


def test_replacement_liveness_result_fields():
    result = ReplacementLivenessResult(
        status="stranded", chain=[TASK_A, TASK_B], remediation="fix it"
    )
    assert result.status == "stranded"
    assert result.chain == [TASK_A, TASK_B]
    assert result.remediation == "fix it"
