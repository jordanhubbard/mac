"""Tests for the allocator-v2 do-not-dispatch hold.

A task created with metadata ``no_dispatch: true`` (`mac task create --no-dispatch`)
is held from autonomous dispatch and hidden from ``ready_tasks``, so a backlog
can be staged without the loop-mode fleet auto-claiming it. Default tasks are
unaffected.
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


def _worker(cp, capabilities=None):
    machine = cp.register_machine("worker-host", resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, "worker", capabilities=capabilities or [])


def test_dispatch_explanation_reports_held_flag(cp):
    held = cp.explain_task_dispatch(_held(cp).id)
    normal = cp.explain_task_dispatch(_normal(cp).id)
    assert [reason["code"] for reason in held["task_reasons"]] == ["task_held"]
    assert normal["task_reasons"] == []


def test_ready_excludes_held_includes_normal(cp):
    held = _held(cp)
    normal = _normal(cp)
    ready_ids = {t.id for t in cp.ready_tasks()}
    assert normal.id in ready_ids  # default task is claimable
    assert held.id not in ready_ids  # staged task is hidden from the ready queue


def test_project_summary_does_not_report_held_task_as_ready(cp):
    held = _held(cp)
    normal = _normal(cp)
    [summary] = cp._hermes_project_contexts(cp.list_tasks(), [], [], [], [])
    assert summary["ready_count"] == 1
    assert summary["held_count"] == 1
    assert [task["id"] for task in summary["frontier_tasks"]] == [normal.id]
    assert held.id not in {task["id"] for task in summary["frontier_tasks"]}


def test_dispatch_explanation_rejects_held(cp):
    explanation = cp.explain_task_dispatch(_held(cp).id)
    assert explanation["task_ready"] is False
    assert explanation["task_reasons"][0]["code"] == "task_held"


def test_dispatch_explanation_allows_normal(cp):
    explanation = cp.explain_task_dispatch(_normal(cp).id)
    assert explanation["task_ready"] is True
    assert explanation["task_reasons"] == []


def test_held_task_suppresses_pair_gates_until_release(cp):
    cp.create_project("p", dispatch_paused=False)
    _worker(cp)
    held = cp.create_task(
        "staged", project="p", required_capabilities=["x"], metadata={"no_dispatch": True}
    )
    explanation = cp.explain_task_dispatch(held.id)
    assert [reason["code"] for reason in explanation["task_reasons"]] == ["task_held"]
    assert explanation["candidates"][0]["reasons"] == []

    cp.release_task(held.id)
    released = cp.explain_task_dispatch(held.id)
    assert released["task_ready"] is True
    assert released["candidates"][0]["reasons"][0]["code"] == ("agent_capabilities_missing")


def test_dispatch_once_does_not_claim_held_task(cp):
    # Regression: the server-push dispatcher (dispatch_once) used to claim
    # straight from the open queue without the no_dispatch gate, so a staged
    # ticket got auto-claimed anyway. With only a held task open, it must
    # claim nothing and leave the task staged.
    _worker(cp)
    held = _held(cp)
    assert cp.dispatch_once() is None
    assert cp.get_task(held.id).state == "open"


def test_dispatch_once_claims_normal_skips_held(cp):
    _worker(cp)
    held = _held(cp)  # created first, so considered before `normal`
    normal = _normal(cp)
    assignment = cp.dispatch_once()
    assert assignment is not None
    assert assignment["task"]["id"] == normal.id
    assert cp.get_task(held.id).state == "open"
