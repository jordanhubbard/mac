"""Tests for the per-project dispatch toggle and the task release un-stage (T1).

A project can be dispatch-PAUSED (``mac project create`` defaults to paused;
``mac project pause`` / ``mac project activate`` toggle it): its open tickets are
hidden from ``ready_tasks`` and rejected by the worker claim policy with reason
``project_dispatch_paused`` until the project is activated. This is the
project-level onboarding gate that complements the per-task ``no_dispatch`` hold.

IMPLICIT projects (no ``projects`` row — the live fleet's default) are never
paused, so existing autonomous behavior is unchanged.

``release_task`` (``mac task release``) clears a per-task ``no_dispatch`` hold.
"""

from __future__ import annotations

import pytest

from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


# -- per-project pause ------------------------------------------------------


def test_implicit_project_never_paused(cp):
    # A task whose project has no record (the default-fleet case) is not paused.
    task = cp.create_task("work", project="ghost-project")
    assert cp._project_dispatch_paused("ghost-project") is False
    ok, reason = cp._task_matches_worker_claim_policy(task, {})
    assert (ok, reason) == (True, "matched")


def test_no_project_never_paused(cp):
    assert cp._project_dispatch_paused(None) is False
    assert cp._project_dispatch_paused("") is False


def test_create_project_defaults_active_at_service_layer(cp):
    # The SERVICE default stays active (non-breaking for API/Hermes-adapter
    # callers); only the CLI opts new projects into paused.
    rec = cp.create_project("svc-default")
    assert cp._project_dispatch_paused(rec.name) is False


def test_create_project_paused(cp):
    rec = cp.create_project("staged-proj", dispatch_paused=True)
    assert cp._project_dispatch_paused(rec.name) is True


def test_paused_project_hides_tasks_from_ready(cp):
    cp.create_project("paused-proj", dispatch_paused=True)
    cp.create_project("live-proj", dispatch_paused=False)
    held = cp.create_task("staged ticket", project="paused-proj")
    live = cp.create_task("live ticket", project="live-proj")
    ready_ids = {t.id for t in cp.ready_tasks()}
    assert live.id in ready_ids
    assert held.id not in ready_ids


def test_paused_project_rejected_by_claim_policy(cp):
    cp.create_project("paused-proj", dispatch_paused=True)
    task = cp.create_task("ticket", project="paused-proj")
    ok, reason = cp._task_matches_worker_claim_policy(task, {})
    assert (ok, reason) == (False, "project_dispatch_paused")


def test_dispatch_once_does_not_claim_paused_project(cp):
    # Regression: the server-push dispatcher (dispatch_once) bypassed the
    # project-pause gate and auto-claimed a paused project's tickets. It must
    # claim the live project's ticket and leave the paused one staged.
    machine = cp.register_machine("worker-host", resources={"cpu": 4, "memory_gb": 8})
    cp.register_agent(machine.id, "worker", capabilities=[])
    cp.create_project("paused-proj", dispatch_paused=True)
    cp.create_project("live-proj", dispatch_paused=False)
    staged = cp.create_task("staged ticket", project="paused-proj")  # considered first
    live = cp.create_task("live ticket", project="live-proj")
    assignment = cp.dispatch_once()
    assert assignment is not None
    assert assignment["task"]["id"] == live.id
    assert cp.get_task(staged.id).state == "open"


def test_set_project_dispatch_round_trip(cp):
    cp.create_project("p")
    task = cp.create_task("ticket", project="p")
    # active -> claimable
    assert cp._task_matches_worker_claim_policy(task, {})[0] is True
    # pause -> rejected
    cp.set_project_dispatch("p", paused=True)
    assert cp._task_matches_worker_claim_policy(task, {}) == (False, "project_dispatch_paused")
    # activate -> claimable again
    cp.set_project_dispatch("p", paused=False)
    assert cp._task_matches_worker_claim_policy(task, {})[0] is True


def test_set_project_dispatch_preserves_other_metadata(cp):
    cp.create_project("p", metadata={"keep": "me"})
    cp.set_project_dispatch("p", paused=True)
    rec = cp.get_project_record("p")
    assert rec.metadata.get("keep") == "me"
    assert rec.metadata.get("dispatch_paused") is True


def test_task_hold_precedes_project_pause(cp):
    # A held task in a paused project reports the task-level hold first.
    cp.create_project("paused-proj", dispatch_paused=True)
    task = cp.create_task("ticket", project="paused-proj", metadata={"no_dispatch": True})
    ok, reason = cp._task_matches_worker_claim_policy(task, {})
    assert (ok, reason) == (False, "dispatch_held")


# -- task release (un-stage) ------------------------------------------------


def test_release_clears_no_dispatch(cp):
    task = cp.create_task("staged", metadata={"no_dispatch": True})
    assert cp._task_dispatch_held(task) is True
    released = cp.release_task(task.id)
    assert cp._task_dispatch_held(released) is False
    ready_ids = {t.id for t in cp.ready_tasks()}
    assert released.id in ready_ids


def test_release_is_noop_when_not_held(cp):
    task = cp.create_task("normal")
    released = cp.release_task(task.id)
    assert released.id == task.id
    assert cp._task_dispatch_held(released) is False
