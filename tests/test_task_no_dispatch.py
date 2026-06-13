"""Tests for the task do-not-dispatch hold (T1 — safe staging / project onboarding).

A task created with metadata ``no_dispatch: true`` (`mac task create --no-dispatch`)
is held from autonomous dispatch: hidden from ``ready_tasks`` and rejected by the
worker claim policy, so a backlog (e.g. a freshly-onboarded project's tickets) can
be staged WITHOUT the loop-mode fleet auto-claiming it. Default tasks are unaffected.
"""

from __future__ import annotations

import pytest

from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _held(cp):
    return cp.create_task("staged work", metadata={"no_dispatch": True})


def _normal(cp):
    return cp.create_task("normal work")


def test_dispatch_held_flag(cp):
    assert cp._task_dispatch_held(_held(cp)) is True
    assert cp._task_dispatch_held(_normal(cp)) is False


def test_ready_excludes_held_includes_normal(cp):
    held = _held(cp)
    normal = _normal(cp)
    ready_ids = {t.id for t in cp.ready_tasks()}
    assert normal.id in ready_ids       # default task is claimable
    assert held.id not in ready_ids     # staged task is hidden from the ready queue


def test_claim_policy_rejects_held(cp):
    ok, reason = cp._task_matches_worker_claim_policy(_held(cp), {})
    assert ok is False
    assert reason == "dispatch_held"


def test_claim_policy_allows_normal(cp):
    ok, reason = cp._task_matches_worker_claim_policy(_normal(cp), {})
    assert ok is True
    assert reason == "matched"


def test_held_check_precedes_project_and_capability_gates(cp):
    # The hold wins even when project/capability would also reject — staging is
    # absolute, not dependent on the worker policy.
    held = cp.create_task(
        "staged", project="p", required_capabilities=["x"], metadata={"no_dispatch": True}
    )
    ok, reason = cp._task_matches_worker_claim_policy(
        held, {"allowed_projects": ["other"], "capabilities": []}
    )
    assert (ok, reason) == (False, "dispatch_held")
