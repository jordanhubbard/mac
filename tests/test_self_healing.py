"""Self-healing sentinel: observe -> plan/act -> verify -> escalate.

The defects this sentinel exists for all actually happened on the live fleet
and sat unnoticed in the hub's own data: a nap scheduler dead for weeks, tasks
starved for 26 days, an enabled daemon silently no-oping, a learning read
path that was never exercised. These tests prove each invariant detects its
class of defect, that a violation becomes exactly one deduped fleet task,
and that a completed-but-ineffective fix escalates instead of looping.
"""

from __future__ import annotations

import pytest

from mac.self_healing import (
    SelfHealingConfig,
    SelfHealingSentinel,
    SELF_HEAL_ORIGIN_TYPE,
)
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _register_agent(cp, name="rocky", capabilities=None, resources=None):
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(
        machine.id, name, capabilities=capabilities or ["ops"], resources=resources
    )


def _sentinel(cp, environ=None, **overrides):
    config = SelfHealingConfig(enabled=True, **overrides)
    return SelfHealingSentinel(cp, config, environ=environ or {})


def _self_heal_tasks(cp, fingerprint=None):
    tasks = []
    for task in cp.list_tasks():
        metadata = task.metadata or {}
        origin = metadata.get("origin") or {}
        if origin.get("type") != SELF_HEAL_ORIGIN_TYPE:
            continue
        if fingerprint and metadata.get("self_heal_fingerprint") != fingerprint:
            continue
        tasks.append(task)
    return tasks


# ── config ───────────────────────────────────────────────────────────────────


def test_config_disabled_by_default_and_validates_numbers():
    assert SelfHealingConfig.from_env({}).active is False
    on = SelfHealingConfig.from_env({"MAC_SELF_HEAL_ENABLED": "1"})
    assert on.active is True
    bad = SelfHealingConfig.from_env(
        {"MAC_SELF_HEAL_ENABLED": "1", "MAC_SELF_HEAL_INTERVAL_SECONDS": "nope"}
    )
    assert bad.active is False
    assert "MAC_SELF_HEAL_INTERVAL_SECONDS" in bad.configuration_error


def test_start_refuses_when_inactive(cp):
    sentinel = SelfHealingSentinel(cp, SelfHealingConfig(enabled=False))
    assert sentinel.start() is False


# ── observe: the invariants ──────────────────────────────────────────────────


def test_nap_liveness_finding_when_schedules_enabled_but_no_runs(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id, offset_minutes=0)
    report = _sentinel(cp).run_once()
    kinds = [f["kind"] for f in report["findings"]]
    assert "nap_liveness" in kinds
    filed = _self_heal_tasks(cp, "nap_liveness")
    assert len(filed) == 1
    assert "nap" in filed[0].title.lower()


def test_no_nap_finding_when_naps_recent(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id, offset_minutes=0)
    run = cp.begin_nap(agent.id)
    cp.complete_nap(run.id)
    report = _sentinel(cp).run_once()
    assert "nap_liveness" not in [f["kind"] for f in report["findings"]]


def test_starvation_finding_for_old_open_task(cp, monkeypatch):
    _register_agent(cp)
    task = cp.create_task("ancient work", project="starved-project")
    # Age the task by moving the sentinel's clock forward instead of
    # rewriting ledger rows.
    import mac.self_healing as sh
    from datetime import timedelta

    real_now = sh._utcnow()
    monkeypatch.setattr(sh, "_utcnow", lambda: real_now + timedelta(days=8))
    report = _sentinel(cp).run_once()
    findings = {f["fingerprint"]: f for f in report["findings"]}
    key = "task_starvation:starved-project"
    assert key in findings
    assert task.id in findings[key]["detail"]["sample_task_ids"]
    # Deliberately staged (no_dispatch) tasks are not starvation.
    staged = cp.create_task(
        "staged work", project="staging", metadata={"no_dispatch": True}
    )
    report2 = _sentinel(cp).run_once()
    assert "task_starvation:staging" not in [
        f["fingerprint"] for f in report2["findings"]
    ]


def test_daemon_heartbeat_finding_only_for_enabled_silent_daemons(cp):
    env = {"MAC_NAP_TICK_ENABLED": "1"}
    report = _sentinel(cp, environ=env).run_once()
    assert "daemon_silent:nap.tick.run" in [f["fingerprint"] for f in report["findings"]]
    # A fresh heartbeat clears it.
    cp.record_log("nap.tick.run", detail={"status": "ok"})
    report2 = _sentinel(cp, environ=env).run_once()
    assert "daemon_silent:nap.tick.run" not in [
        f["fingerprint"] for f in report2["findings"]
    ]
    # Disabled daemons are never findings.
    report3 = _sentinel(cp, environ={}).run_once()
    assert not [f for f in report3["findings"] if f["kind"] == "daemon_silent"]


def test_read_path_silence_pins_task_to_openclaw_agent(cp):
    agent = _register_agent(
        cp,
        name="gatewayed",
        resources={
            "chat_gateway": {"implementation": "openclaw", "verified": True}
        },
    )
    report = _sentinel(cp).run_once()
    assert "read_path_silence:continuity" in [
        f["fingerprint"] for f in report["findings"]
    ]
    filed = _self_heal_tasks(cp, "read_path_silence:continuity")
    assert len(filed) == 1
    assert filed[0].metadata.get("target_agent_id") == agent.id
    # A recent serve event clears the invariant.
    cp.record_log(
        "continuity.context_served",
        subject_type="agent",
        subject_id=agent.id,
        detail={"memory_count": 2},
    )
    # Complete the filed task so only the invariant (not dedupe) decides.
    report2 = _sentinel(cp).run_once()
    assert "read_path_silence:continuity" not in [
        f["fingerprint"] for f in report2["findings"]
    ]


def test_stuck_quarantine_finding_ignores_operator_holds(cp, monkeypatch):
    auto = _register_agent(cp, name="zombie")
    manual = _register_agent(cp, name="benched")
    cp.set_agent_dispatch_hold(auto.id, "auto_quarantine:consecutive_expiries_no_telemetry")
    cp.set_agent_dispatch_hold(manual.id, "operator: benched for maintenance")
    import mac.self_healing as sh
    from datetime import timedelta

    real_now = sh._utcnow()
    monkeypatch.setattr(sh, "_utcnow", lambda: real_now + timedelta(days=8))
    report = _sentinel(cp).run_once()
    fingerprints = [f["fingerprint"] for f in report["findings"]]
    assert ("stuck_quarantine:%s" % auto.id) in fingerprints
    assert ("stuck_quarantine:%s" % manual.id) not in fingerprints


# ── act: dedupe ──────────────────────────────────────────────────────────────


def test_standing_violation_yields_one_task_not_a_storm(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id, offset_minutes=0)
    sentinel = _sentinel(cp)
    first = sentinel.run_once()
    second = sentinel.run_once()
    assert first["filed_count"] == 1
    assert second["filed_count"] == 0
    assert [f["action"] for f in second["findings"]] == ["in_progress"]
    assert len(_self_heal_tasks(cp, "nap_liveness")) == 1


# ── verify + escalate ────────────────────────────────────────────────────────


def test_completed_but_ineffective_fix_refiles_with_attempt_and_prior_task(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id, offset_minutes=0)
    sentinel = _sentinel(cp)
    sentinel.run_once()
    (task,) = _self_heal_tasks(cp, "nap_liveness")
    # The fleet "fixes" it... but the invariant still fails next cycle.
    cp.force_complete_task(task.id, "test", reason="simulate ineffective fix")
    report = sentinel.run_once()
    refiled = [t for t in _self_heal_tasks(cp, "nap_liveness") if t.id != task.id]
    assert len(refiled) == 1
    assert refiled[0].metadata.get("self_heal_attempt") == 2
    assert refiled[0].metadata.get("self_heal_prior_task_id") == task.id
    assert "RECURRED" in refiled[0].description


def test_exhausted_attempts_escalate_to_operator_notification(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id, offset_minutes=0)
    sentinel = _sentinel(cp, max_attempts=1)
    sentinel.run_once()
    (task,) = _self_heal_tasks(cp, "nap_liveness")
    cp.force_complete_task(task.id, "test", reason="simulate ineffective fix")
    report = sentinel.run_once()
    assert report["escalated_count"] == 1
    # No second task was filed once autonomy was exhausted.
    assert len(_self_heal_tasks(cp, "nap_liveness")) == 1
    notes = [
        n for n in cp.list_notifications()
        if n.event_type == "self_heal.escalated"
    ]
    assert len(notes) == 1


# ── resilience ───────────────────────────────────────────────────────────────


def test_one_broken_check_does_not_blind_the_others(cp, monkeypatch):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id, offset_minutes=0)
    sentinel = _sentinel(cp)

    def explode():
        raise RuntimeError("check exploded")

    monkeypatch.setattr(sentinel, "_check_task_starvation", explode)
    report = sentinel.run_once()
    assert report["status"] == "ok"
    assert any("check exploded" in e for e in report["check_errors"])
    assert "nap_liveness" in [f["kind"] for f in report["findings"]]


def test_status_reflects_last_report(cp):
    sentinel = _sentinel(cp)
    assert sentinel.status()["last_report"] is None
    report = sentinel.run_once()
    assert sentinel.status()["last_report"]["run_id"] == report["run_id"]
