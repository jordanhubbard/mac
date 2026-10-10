"""Dispatch is gated on the running release identity (task_822aada4).

A worker's checkout is not its running version, and "healthy" used to mean
"no deploy barrier". Workers now report the commit their process loaded; while
an operator-selected release is in force, a worker running anything else, a
worker whose checkout moved without a restart, or one reporting no identity is
excluded from dispatch, and the drift is visible with a repair plan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mac import release_identity, worker
from mac.api import create_app
from mac.models import AgentStatus, ValidationError
from mac.services import ControlPlane

A = "a" * 40
B = "b" * 40


def _state(loaded: str = A, disk: str = A, *, dirty: bool = False, **extra):
    return {
        "source_state": {
            "schema": release_identity.SOURCE_SCHEMA,
            "commit_sha": disk,
            "loaded_commit": loaded,
            "restart_pending": loaded != disk,
            "dirty": dirty,
            "boot_id": "boot1",
            **extra,
        }
    }


@pytest.mark.parametrize(
    ("resources", "verdict"),
    [
        (_state(), None),
        (_state(loaded=B, disk=B), "release_mismatch"),
        (_state(loaded=A, disk=B), "release_restart_pending"),
        (_state(dirty=True), "release_dirty"),
        (_state(loaded=""), "release_unknown"),
        (_state(error="repository_missing"), "release_unknown"),
        ({}, None),  # no MAC source state: not a runtime executor, not gated
    ],
)
def test_evaluate(resources, verdict):
    assert release_identity.evaluate(resources, A) == verdict


def _agent(cp, name, resources, status=AgentStatus.IDLE.value):
    machine = cp.register_machine("host-%s" % name, machine_id="machine_%s" % name)
    agent = cp.register_agent(machine.id, name, agent_id="agent_%s" % name)
    cp.heartbeat_agent(agent.id, status=status, health_status="healthy", resources=resources)
    return cp.get_agent(agent.id)


def test_a_selected_release_gates_dispatch_on_the_loaded_commit():
    cp = ControlPlane.in_memory()
    current = _agent(cp, "current", _state())
    stale = _agent(cp, "stale", _state(loaded=B, disk=B))
    unrestarted = _agent(cp, "unrestarted", _state(loaded=B, disk=A))
    session = _agent(cp, "session", {})
    task = cp.create_task("work")

    # Nothing selected: no release gate at all.
    assert cp._agent_availability_for_task(stale, task)[1] != "release_mismatch"

    cp.select_release(A, actor="operator", reason="ship it")

    assert cp._agent_availability_for_task(stale, task) == (False, "release_mismatch")
    assert cp._agent_availability_for_task(unrestarted, task) == (
        False,
        "release_restart_pending",
    )
    assert not str(cp._agent_availability_for_task(current, task)[1]).startswith("release_")
    assert not str(cp._agent_availability_for_task(session, task)[1]).startswith("release_")

    cp.clear_release_selection(actor="operator")
    cp._release_selection_cache = None
    assert cp._agent_availability_for_task(stale, task)[1] != "release_mismatch"


def test_the_transactional_claim_refuses_a_mismatched_worker():
    cp = ControlPlane.in_memory()
    stale = _agent(cp, "stale", _state(loaded=B, disk=B))
    task = cp.create_task("work")
    cp.select_release(A, actor="operator")
    with pytest.raises(Exception, match="release_mismatch"):
        cp.claim_task(task.id, stale.id)


def test_selection_is_validated():
    cp = ControlPlane.in_memory()
    with pytest.raises(ValidationError, match="40-hex"):
        cp.select_release("abc123", actor="operator")
    with pytest.raises(ValidationError, match="actor"):
        cp.select_release(A, actor="")
    with pytest.raises(ValidationError, match="positive"):
        cp.select_release(A, actor="operator", converge_minutes=0)


def test_drift_reports_each_fact_separately_with_a_repair_plan():
    cp = ControlPlane.in_memory()
    refreshed = (datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat()
    _agent(cp, "current", {**_state(), "fleet_context": {"refreshed_at": refreshed}})
    _agent(cp, "stale", _state(loaded=B, disk=B))
    _agent(cp, "unrestarted", _state(loaded=B, disk=A))
    _agent(cp, "gone", _state(loaded=B, disk=B), status=AgentStatus.OFFLINE.value)
    cp.select_release(A, actor="operator", converge_minutes=30)

    report = cp.release_drift()

    rows = {row["name"]: row for row in report["agents"]}
    assert rows["current"]["release"] == "match"
    assert 400 <= rows["current"]["fleet_context_age_seconds"] <= 440
    assert rows["stale"]["release"] == "release_mismatch"
    assert rows["stale"]["repair"] == "scripts/fleet-update stale %s" % A[:12]
    assert rows["unrestarted"]["release"] == "release_restart_pending"
    assert "restart it" in rows["unrestarted"]["repair"]
    assert rows["gone"]["live"] is False and "offline" in rows["gone"]["repair"]
    assert sorted(report["drifted"]) == ["gone", "stale", "unrestarted"]
    assert report["converged"] is False and report["overdue"] is False

    later = datetime.now(timezone.utc) + timedelta(hours=1)
    overdue = release_identity.drift(
        report["selection"], [a.to_dict() for a in cp.list_agents()], now=later
    )
    assert overdue["overdue"] is True


def test_the_dispatcher_tick_records_drift_only_when_it_changes():
    cp = ControlPlane.in_memory()
    _agent(cp, "stale", _state(loaded=B, disk=B))
    cp.select_release(A, actor="operator")

    cp.observe_release_drift()
    cp.observe_release_drift()

    events = [e for e in cp.list_observability(limit=200) if e.name == "fleet.release.drift"]
    assert len(events) == 1
    assert events[0].detail["drifted"] == ["stale"]
    assert events[0].detail["repairs"] == ["scripts/fleet-update stale %s" % A[:12]]


def test_release_api_select_show_clear():
    cp = ControlPlane.in_memory()
    _agent(cp, "stale", _state(loaded=B, disk=B))
    client = TestClient(create_app(control_plane=cp))

    selected = client.put("/fleet/release", json={"commit_sha": A, "reason": "r", "actor": "op"})
    assert selected.status_code == 200, selected.text
    assert selected.json()["commit_sha"] == A

    shown = client.get("/fleet/release").json()
    assert shown["drifted"] == ["stale"]

    cleared = client.delete("/fleet/release").json()
    assert cleared["cleared"] is True
    assert client.get("/fleet/release").json()["selection"] is None


def test_worker_reports_what_its_process_loaded(tmp_path: Path, monkeypatch):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    (repo / "f").write_text("1\n")
    run("add", "f")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
    monkeypatch.setattr(worker, "_BOOT_LOADED", {})

    first = worker._worker_source_state(repo)
    assert first["loaded_commit"] == first["commit_sha"]
    assert first["restart_pending"] is False
    assert first["boot_id"] == worker._BOOT_ID

    (repo / "f").write_text("2\n")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "two")
    second = worker._worker_source_state(repo)
    # The checkout moved; the process did not.
    assert second["commit_sha"] != first["commit_sha"]
    assert second["loaded_commit"] == first["commit_sha"]
    assert second["restart_pending"] is True
    # Selecting the new commit does not admit it until the process restarts.
    assert release_identity.evaluate({"source_state": second}, second["commit_sha"]) == (
        "release_restart_pending"
    )


def test_worker_reports_its_fleet_context_age(tmp_path: Path, monkeypatch):
    markdown = tmp_path / "ctx.md"
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", str(markdown))
    assert worker._fleet_context_state()["error"] == "missing"
    markdown.write_text("x")
    assert worker._fleet_context_state()["refreshed_at"]
