"""Tests for src/mac/openclaw_checkpoint_gc.py.

Covers:
- CheckpointCandidate / RetireReceipt / CheckpointGCPlan data-class helpers and
  validation (empty path, non-positive pid, negative byte_count/age)
- classify_candidate(): active (live owner), complete_pair, incomplete
  (in-grace crash residue), stale (dead owner, out of grace)
- plan_checkpoint_gc(): only provably-unowned + out-of-grace + non-anchor
  candidates are retired; active, in-grace, unknown-liveness, and the
  most-recent validated pair are preserved
- Crash-injection at every staging phase — workspace download, state download,
  validation, pair promotion, cleanup — proving a crash cannot delete the last
  validated pair and cannot accumulate candidates without bound
- Receipts carry only path identity / age / liveness / validation / bytes /
  action and never database content or credentials
- lock is honored: plan runs under an injected context-manager lock

All liveness and grace decisions are simulated through injected fakes; no live
process table or wall clock is consulted.
"""

from __future__ import annotations

import threading

import pytest

from mac.openclaw_checkpoint_gc import (
    ACTION_PRESERVE,
    ACTION_RETIRE,
    CHECKPOINT_GC_SCHEMA,
    CLASS_ACTIVE,
    CLASS_COMPLETE_PAIR,
    CLASS_INCOMPLETE,
    CLASS_STALE,
    CheckpointCandidate,
    CheckpointGCPlan,
    RetireReceipt,
    classify_candidate,
    grace_from_seconds,
    liveness_from_set,
    plan_checkpoint_gc,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _cand(
    pid: int,
    *,
    has_workspace: bool = False,
    has_state: bool = False,
    validated: bool = False,
    byte_count: int = 1024,
    age_seconds: float = 10_000.0,
    path: str | None = None,
) -> CheckpointCandidate:
    return CheckpointCandidate(
        path=path or f"/root/.checkpoint-{pid}",
        owner_pid=pid,
        has_workspace=has_workspace,
        has_state=has_state,
        validated=validated,
        byte_count=byte_count,
        age_seconds=age_seconds,
    )


def _validated_pair(pid: int, *, age_seconds: float, byte_count: int = 4096) -> CheckpointCandidate:
    return _cand(
        pid,
        has_workspace=True,
        has_state=True,
        validated=True,
        byte_count=byte_count,
        age_seconds=age_seconds,
    )


class _CountingLock:
    """A context-manager lock that records how many times it was entered."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enters = 0

    def __enter__(self):
        self.enters += 1
        return self._lock.__enter__()

    def __exit__(self, *exc):
        return self._lock.__exit__(*exc)


def _receipt_for(plan: CheckpointGCPlan, path: str) -> RetireReceipt:
    for r in plan.receipts:
        if r.path == path:
            return r
    raise AssertionError(f"no receipt for {path}")


# ---------------------------------------------------------------------------
# Data-class validation
# ---------------------------------------------------------------------------


def test_candidate_rejects_empty_path() -> None:
    with pytest.raises(ValueError):
        CheckpointCandidate(path="  ", owner_pid=5)


def test_candidate_rejects_non_positive_pid() -> None:
    with pytest.raises(ValueError):
        CheckpointCandidate(path="/x", owner_pid=0)


def test_candidate_rejects_negative_bytes() -> None:
    with pytest.raises(ValueError):
        CheckpointCandidate(path="/x", owner_pid=5, byte_count=-1)


def test_candidate_rejects_negative_age() -> None:
    with pytest.raises(ValueError):
        CheckpointCandidate(path="/x", owner_pid=5, age_seconds=-0.1)


def test_is_complete_pair_requires_both_halves_and_validation() -> None:
    assert _validated_pair(5, age_seconds=1.0).is_complete_pair is True
    assert _cand(5, has_workspace=True, has_state=True, validated=False).is_complete_pair is False
    assert _cand(5, has_workspace=True, has_state=False, validated=True).is_complete_pair is False
    assert _cand(5, has_workspace=False, has_state=True, validated=True).is_complete_pair is False


# ---------------------------------------------------------------------------
# classify_candidate
# ---------------------------------------------------------------------------


def test_classify_active_when_owner_alive() -> None:
    c = _cand(5)
    assert classify_candidate(c, owner_alive=True, in_grace=False) == CLASS_ACTIVE
    # Live owner beats everything, even a complete pair.
    p = _validated_pair(5, age_seconds=1.0)
    assert classify_candidate(p, owner_alive=True, in_grace=False) == CLASS_ACTIVE


def test_classify_complete_pair_when_dead_owner_validated() -> None:
    p = _validated_pair(5, age_seconds=1.0)
    assert classify_candidate(p, owner_alive=False, in_grace=False) == CLASS_COMPLETE_PAIR


def test_classify_incomplete_when_in_grace() -> None:
    c = _cand(5, has_workspace=True, has_state=False)
    assert classify_candidate(c, owner_alive=False, in_grace=True) == CLASS_INCOMPLETE


def test_classify_stale_when_dead_and_out_of_grace() -> None:
    c = _cand(5, has_workspace=True, has_state=False)
    assert classify_candidate(c, owner_alive=False, in_grace=False) == CLASS_STALE


# ---------------------------------------------------------------------------
# Predicate builders
# ---------------------------------------------------------------------------


def test_liveness_from_set() -> None:
    fn = liveness_from_set({7})
    assert fn(7) is True
    assert fn(8) is False
    assert liveness_from_set()(1) is False


def test_grace_from_seconds() -> None:
    fn = grace_from_seconds(3600.0)
    assert fn(10.0) is True
    assert fn(3600.0) is False
    assert fn(7200.0) is False
    # Non-positive window disables grace entirely.
    assert grace_from_seconds(0)(0.0) is False


# ---------------------------------------------------------------------------
# plan_checkpoint_gc core behavior
# ---------------------------------------------------------------------------


def test_active_candidate_preserved() -> None:
    active = _cand(100, age_seconds=99_999.0)
    plan = plan_checkpoint_gc(
        [active],
        is_owner_alive=liveness_from_set({100}),
        is_in_grace=grace_from_seconds(1.0),
    )
    r = _receipt_for(plan, active.path)
    assert r.classification == CLASS_ACTIVE
    assert r.action == ACTION_PRESERVE
    assert plan.retire == []


def test_stale_dead_owner_out_of_grace_retired() -> None:
    stale = _cand(200, has_workspace=True, age_seconds=100_000.0)
    plan = plan_checkpoint_gc(
        [stale],
        is_owner_alive=liveness_from_set(set()),
        is_in_grace=grace_from_seconds(3600.0),
    )
    r = _receipt_for(plan, stale.path)
    assert r.classification == CLASS_STALE
    assert r.action == ACTION_RETIRE
    assert plan.reclaimable_bytes == stale.byte_count


def test_in_grace_residue_preserved() -> None:
    fresh = _cand(201, has_workspace=True, age_seconds=60.0)
    plan = plan_checkpoint_gc(
        [fresh],
        is_owner_alive=liveness_from_set(set()),
        is_in_grace=grace_from_seconds(3600.0),
    )
    r = _receipt_for(plan, fresh.path)
    assert r.classification == CLASS_INCOMPLETE
    assert r.action == ACTION_PRESERVE


def test_most_recent_validated_pair_never_retired_even_when_owner_dead() -> None:
    # Two validated pairs, both dead owners, both out of grace. Only the older
    # one is retirable; the most-recent is the rollback anchor.
    newest = _validated_pair(300, age_seconds=100.0, byte_count=10)
    older = _validated_pair(301, age_seconds=5000.0, byte_count=20)
    plan = plan_checkpoint_gc(
        [older, newest],
        is_owner_alive=liveness_from_set(set()),
        is_in_grace=grace_from_seconds(1.0),
    )
    anchor = _receipt_for(plan, newest.path)
    assert anchor.action == ACTION_PRESERVE
    assert "rollback anchor" in anchor.reason
    superseded = _receipt_for(plan, older.path)
    # The superseded validated pair is conservatively preserved, not deleted.
    assert superseded.action == ACTION_PRESERVE
    assert superseded.classification == CLASS_COMPLETE_PAIR


def test_unknown_liveness_preserved() -> None:
    # Liveness fn that conservatively reports alive => never retired.
    c = _cand(400, age_seconds=999_999.0)
    plan = plan_checkpoint_gc(
        [c],
        is_owner_alive=lambda _pid: True,
        is_in_grace=grace_from_seconds(0),
    )
    assert _receipt_for(plan, c.path).action == ACTION_PRESERVE


def test_lock_is_entered_when_provided() -> None:
    lock = _CountingLock()
    plan_checkpoint_gc(
        [_cand(1, age_seconds=1.0)],
        is_owner_alive=liveness_from_set(set()),
        is_in_grace=grace_from_seconds(3600.0),
        lock=lock,
    )
    assert lock.enters == 1


def test_receipts_carry_no_secrets() -> None:
    c = _cand(500, has_workspace=True, age_seconds=100_000.0)
    plan = plan_checkpoint_gc(
        [c],
        is_owner_alive=liveness_from_set(set()),
        is_in_grace=grace_from_seconds(1.0),
    )
    d = plan.retire[0].to_dict()
    assert set(d) == {
        "schema",
        "path",
        "classification",
        "action",
        "reason",
        "owner_pid",
        "owner_alive",
        "validated",
        "byte_count",
        "age_seconds",
    }
    assert d["schema"] == CHECKPOINT_GC_SCHEMA


def test_plan_counts_and_views() -> None:
    active = _cand(1, age_seconds=1.0)
    anchor = _validated_pair(2, age_seconds=2.0)
    stale = _cand(3, has_workspace=True, age_seconds=100_000.0)
    plan = plan_checkpoint_gc(
        [active, anchor, stale],
        is_owner_alive=liveness_from_set({1}),
        is_in_grace=grace_from_seconds(3600.0),
    )
    counts = plan.counts()
    assert counts[CLASS_ACTIVE] == 1
    assert counts[CLASS_COMPLETE_PAIR] == 1
    assert counts[CLASS_STALE] == 1
    assert len(plan.retire) == 1
    assert len(plan.preserve) == 2


# ---------------------------------------------------------------------------
# Crash-injection at each staging phase
#
# We model a checkpoint that reaches a given phase and then the owning process
# CRASHES (its PID becomes dead). The invariants under test:
#   1. The crash residue is NEVER retired while in grace, and IS retired once
#      dead + out of grace (bounded accumulation).
#   2. A previously promoted validated pair (the last-good) is ALWAYS preserved
#      through any crash of a *different* in-flight candidate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase,has_ws,has_state,validated",
    [
        ("workspace_download", False, False, False),  # crash before/at ws dl
        ("state_download", True, False, False),  # ws done, crash at state dl
        ("validation", True, True, False),  # both halves, crash at validation
        ("pair_promotion", True, True, True),  # validated, crash before promote
        ("cleanup", True, True, True),  # promoted, crash during cleanup
    ],
)
def test_crash_at_phase_never_deletes_last_validated_pair(
    phase: str, has_ws: bool, has_state: bool, validated: bool
) -> None:
    # The durable last-good validated pair, owned by a now-dead promoter.
    last_good = _validated_pair(10, age_seconds=50.0, byte_count=8192)
    # The in-flight candidate that crashed at `phase`; its owner PID is dead.
    crashed = _cand(
        11,
        has_workspace=has_ws,
        has_state=has_state,
        validated=validated,
        age_seconds=100_000.0,
        path=f"/root/.checkpoint-11-{phase}",
    )
    plan = plan_checkpoint_gc(
        [last_good, crashed],
        is_owner_alive=liveness_from_set(set()),  # both owners dead
        is_in_grace=grace_from_seconds(3600.0),  # out of grace
    )
    # Invariant: the last validated pair is preserved as the rollback anchor.
    anchor = _receipt_for(plan, last_good.path)
    assert anchor.action == ACTION_PRESERVE, phase

    crashed_receipt = _receipt_for(plan, crashed.path)
    if crashed.is_complete_pair:
        # A crashed-but-validated candidate that is newer than last_good would
        # itself become the anchor; here it is older, so it is a superseded
        # validated pair -> preserved (never silently deleted).
        assert crashed_receipt.action == ACTION_PRESERVE, phase
    else:
        # Incomplete crash residue outside grace is stale -> reclaimable.
        assert crashed_receipt.action == ACTION_RETIRE, phase
        assert crashed_receipt.classification == CLASS_STALE, phase


def test_crash_residue_within_grace_is_never_retired() -> None:
    # A candidate that crashed mid-validation seconds ago must survive one pass.
    fresh_crash = _cand(20, has_workspace=True, has_state=True, age_seconds=5.0)
    plan = plan_checkpoint_gc(
        [fresh_crash],
        is_owner_alive=liveness_from_set(set()),
        is_in_grace=grace_from_seconds(600.0),
    )
    assert plan.retire == []
    assert _receipt_for(plan, fresh_crash.path).classification == CLASS_INCOMPLETE


def test_accumulation_is_bounded_over_repeated_passes() -> None:
    # 45 abandoned dead-owner candidates (the reported real backlog), all out of
    # grace, none a validated pair -> every one is reclaimed in a single pass.
    residue = [
        _cand(1000 + i, has_workspace=bool(i % 2), age_seconds=100_000.0 + i)
        for i in range(45)
    ]
    plan = plan_checkpoint_gc(
        residue,
        is_owner_alive=liveness_from_set(set()),
        is_in_grace=grace_from_seconds(3600.0),
    )
    assert len(plan.retire) == 45
    assert all(r.classification == CLASS_STALE for r in plan.retire)


def test_cleanup_failure_is_observation_not_a_block() -> None:
    # plan_checkpoint_gc is pure: it only *reports* what to retire. Even if a
    # caller's later delete were to fail, re-planning is idempotent and still
    # yields the same retiral set — cleanup is a maintenance item, never a
    # reason to raise or block unrelated work.
    stale = _cand(30, has_workspace=True, age_seconds=100_000.0)
    args = dict(
        is_owner_alive=liveness_from_set(set()),
        is_in_grace=grace_from_seconds(3600.0),
    )
    first = plan_checkpoint_gc([stale], **args)
    # Simulate "delete failed, candidate still present" -> re-plan is stable.
    second = plan_checkpoint_gc([stale], **args)
    assert [r.path for r in first.retire] == [r.path for r in second.retire]
    assert first.reclaimable_bytes == second.reclaimable_bytes
