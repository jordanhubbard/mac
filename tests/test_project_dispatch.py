"""Tests for allocator-v2 project dispatch and task release behavior.

A project can be dispatch-PAUSED (``mac project create`` defaults to paused;
``mac project pause`` / ``mac project activate`` toggle it): its open tickets are
hidden from ``ready_tasks`` and reported as ``task_project_inactive`` by the
public dispatch explanation until the project is activated. This is the
project-level onboarding gate that complements the per-task ``no_dispatch`` hold.

Project-scoped work must refer to a registered project. Unscoped tasks remain
valid without a project record.

``release_task`` (``mac task release``) clears a per-task ``no_dispatch`` hold.
"""

from __future__ import annotations

import pytest

from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


# -- per-project pause ------------------------------------------------------


def test_unregistered_project_is_not_dispatchable(cp):
    task = cp.create_task("work", project="ghost-project")
    explanation = cp.explain_task_dispatch(task.id)
    assert explanation["task_ready"] is False
    assert [reason["code"] for reason in explanation["task_reasons"]] == [
        "task_project_unregistered",
        "task_project_inactive",
    ]
    assert task.id not in {item.id for item in cp.ready_tasks()}


def test_unscoped_task_is_ready_without_project_record(cp):
    task = cp.create_task("unscoped work")
    assert cp.explain_task_dispatch(task.id)["task_ready"] is True
    assert task.id in {item.id for item in cp.ready_tasks()}


def test_create_project_defaults_active_at_service_layer(cp):
    rec = cp.create_project("svc-default")
    task = cp.create_task("work", project=rec.name)
    assert cp.explain_task_dispatch(task.id)["task_ready"] is True


def test_create_project_paused(cp):
    rec = cp.create_project("staged-proj", dispatch_paused=True)
    task = cp.create_task("work", project=rec.name)
    explanation = cp.explain_task_dispatch(task.id)
    assert [reason["code"] for reason in explanation["task_reasons"]] == ["task_project_inactive"]


def test_paused_project_hides_tasks_from_ready(cp):
    cp.create_project("paused-proj", dispatch_paused=True)
    cp.create_project("live-proj", dispatch_paused=False)
    held = cp.create_task("staged ticket", project="paused-proj")
    live = cp.create_task("live ticket", project="live-proj")
    ready_ids = {t.id for t in cp.ready_tasks()}
    assert live.id in ready_ids
    assert held.id not in ready_ids


def test_paused_project_explains_allocator_v2_rejection(cp):
    cp.create_project("paused-proj", dispatch_paused=True)
    task = cp.create_task("ticket", project="paused-proj")
    explanation = cp.explain_task_dispatch(task.id)
    assert explanation["task_ready"] is False
    assert explanation["task_reasons"][0]["code"] == "task_project_inactive"


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
    assert cp.explain_task_dispatch(task.id)["task_ready"] is True
    # pause -> rejected
    cp.set_project_dispatch("p", paused=True)
    paused = cp.explain_task_dispatch(task.id)
    assert [reason["code"] for reason in paused["task_reasons"]] == ["task_project_inactive"]
    # activate -> claimable again
    cp.set_project_dispatch("p", paused=False)
    assert cp.explain_task_dispatch(task.id)["task_ready"] is True


def test_set_project_dispatch_preserves_other_metadata(cp):
    cp.create_project("p", metadata={"keep": "me"})
    cp.set_project_dispatch("p", paused=True)
    rec = cp.get_project_record("p")
    assert rec.metadata.get("keep") == "me"
    assert rec.metadata.get("dispatch_paused") is True


def test_task_hold_and_project_pause_are_both_reported(cp):
    cp.create_project("paused-proj", dispatch_paused=True)
    task = cp.create_task("ticket", project="paused-proj", metadata={"no_dispatch": True})
    explanation = cp.explain_task_dispatch(task.id)
    assert [reason["code"] for reason in explanation["task_reasons"]] == [
        "task_held",
        "task_project_inactive",
    ]


# -- task release (un-stage) ------------------------------------------------


def test_release_clears_no_dispatch(cp):
    task = cp.create_task("staged", metadata={"no_dispatch": True})
    assert cp.explain_task_dispatch(task.id)["task_ready"] is False
    released = cp.release_task(task.id)
    assert cp.explain_task_dispatch(released.id)["task_ready"] is True
    ready_ids = {t.id for t in cp.ready_tasks()}
    assert released.id in ready_ids


def test_release_is_noop_when_not_held(cp):
    task = cp.create_task("normal")
    released = cp.release_task(task.id)
    assert released.id == task.id
    assert cp.explain_task_dispatch(released.id)["task_ready"] is True
