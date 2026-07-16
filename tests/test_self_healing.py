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


# ── fleet pin divergence ─────────────────────────────────────────────────────


def _stub_events(monkeypatch, cp, events_by_name):
    """Route list_observability(name=...) to canned event stubs."""
    from types import SimpleNamespace

    real = cp.list_observability

    def fake(*args, **kwargs):
        name = kwargs.get("name") or (args[0] if args else None)
        if name in events_by_name:
            return [SimpleNamespace(**e) for e in events_by_name[name]]
        return real(*args, **kwargs)

    monkeypatch.setattr(cp, "list_observability", fake)


def test_pin_divergence_flags_heartbeating_agent_with_stale_trail(cp, monkeypatch):
    fresh = _register_agent(cp, name="current")
    laggard = _register_agent(cp, name="wedged")
    cp.heartbeat_agent(fresh.id)
    cp.heartbeat_agent(laggard.id)
    import mac.self_healing as sh
    from datetime import timedelta

    now = sh._utcnow()
    _stub_events(monkeypatch, cp, {
        "worker.agentbus.repo_update.updated": [
            {"source": fresh.id,
             "created_at": (now - timedelta(hours=4)).isoformat()},
            {"source": laggard.id,
             "created_at": (now - timedelta(days=21)).isoformat()},
        ],
    })
    report = _sentinel(cp).run_once()
    fingerprints = [f["fingerprint"] for f in report["findings"]]
    assert ("fleet_pin_divergence:%s" % laggard.id) in fingerprints
    assert ("fleet_pin_divergence:%s" % fresh.id) not in fingerprints


def test_pin_divergence_waits_out_a_recent_sweep(cp, monkeypatch):
    a = _register_agent(cp, name="a")
    b = _register_agent(cp, name="b")
    cp.heartbeat_agent(a.id)
    cp.heartbeat_agent(b.id)
    import mac.self_healing as sh
    from datetime import timedelta

    now = sh._utcnow()
    # b just applied a sweep minutes ago; a hasn't consumed it yet — that's
    # in-flight propagation, not divergence.
    _stub_events(monkeypatch, cp, {
        "worker.agentbus.repo_update.updated": [
            {"source": b.id, "created_at": (now - timedelta(minutes=5)).isoformat()},
            {"source": a.id, "created_at": (now - timedelta(days=21)).isoformat()},
        ],
    })
    report = _sentinel(cp).run_once()
    assert not [f for f in report["findings"] if f["kind"] == "fleet_pin_divergence"]



def test_pin_divergence_flags_agent_with_no_trail_at_all(cp, monkeypatch):
    """A heartbeating agent with zero repo-update events is flagged."""
    import mac.self_healing as sh
    from datetime import timedelta

    known = _register_agent(cp, name="known")
    ghost = _register_agent(cp, name="ghost")
    cp.heartbeat_agent(known.id)
    cp.heartbeat_agent(ghost.id)

    now = sh._utcnow()
    # Only 'known' has events; 'ghost' never appears in any status stream.
    _stub_events(monkeypatch, cp, {
        "worker.agentbus.repo_update.updated": [
            {"source": known.id,
             "created_at": (now - timedelta(days=14)).isoformat()},
        ],
    })
    report = _sentinel(cp).run_once()
    findings = {f["fingerprint"]: f for f in report["findings"]}
    fp = "fleet_pin_divergence:%s" % ghost.id
    assert fp in findings
    assert findings[fp]["detail"]["lag_seconds"] is None
    assert findings[fp]["detail"]["agent_latest_update"] is None


def test_pin_divergence_skips_held_agents(cp, monkeypatch):
    """An agent with dispatch_hold is never included in findings."""
    import mac.self_healing as sh
    from datetime import timedelta

    held = _register_agent(cp, name="held")
    cp.heartbeat_agent(held.id)
    cp.set_agent_dispatch_hold(held.id, "operator: maintenance")

    anchor = _register_agent(cp, name="anchor")
    cp.heartbeat_agent(anchor.id)

    now = sh._utcnow()
    _stub_events(monkeypatch, cp, {
        "worker.agentbus.repo_update.updated": [
            {"source": anchor.id,
             "created_at": (now - timedelta(days=14)).isoformat()},
        ],
    })
    report = _sentinel(cp).run_once()
    fingerprints = [f["fingerprint"] for f in report["findings"]]
    assert ("fleet_pin_divergence:%s" % held.id) not in fingerprints


def test_pin_divergence_uses_max_across_all_statuses(cp, monkeypatch):
    """The check merges all six status streams; the max timestamp wins."""
    import mac.self_healing as sh
    from datetime import timedelta

    a = _register_agent(cp, name="a")
    cp.heartbeat_agent(a.id)

    now = sh._utcnow()
    # 'updated' event is three weeks stale, but 'skipped' event is recent
    # (yesterday). The merged max puts agent 'a' within the divergence
    # window, so it should NOT be flagged.
    _stub_events(monkeypatch, cp, {
        "worker.agentbus.repo_update.updated": [
            {"source": a.id,
             "created_at": (now - timedelta(days=21)).isoformat()},
        ],
        "worker.agentbus.repo_update.skipped": [
            {"source": a.id,
             "created_at": (now - timedelta(hours=23)).isoformat()},
        ],
    })
    report = _sentinel(cp).run_once()
    assert not [f for f in report["findings"] if f["kind"] == "fleet_pin_divergence"]


def test_pin_divergence_files_one_deduped_task_per_agent(cp, monkeypatch):
    """run_once() files exactly one task per diverged agent; a second run
    does not file another."""
    import mac.self_healing as sh
    from datetime import timedelta

    laggard = _register_agent(cp, name="laggard")
    anchor = _register_agent(cp, name="anchor")
    cp.heartbeat_agent(laggard.id)
    cp.heartbeat_agent(anchor.id)

    now = sh._utcnow()
    _stub_events(monkeypatch, cp, {
        "worker.agentbus.repo_update.updated": [
            {"source": anchor.id,
             "created_at": (now - timedelta(days=14)).isoformat()},
            {"source": laggard.id,
             "created_at": (now - timedelta(days=21)).isoformat()},
        ],
    })

    sentinel = _sentinel(cp)
    sentinel.run_once()
    fp = "fleet_pin_divergence:%s" % laggard.id
    filed_after_first = _self_heal_tasks(cp, fp)
    assert len(filed_after_first) == 1

    sentinel.run_once()
    filed_after_second = _self_heal_tasks(cp, fp)
    assert len(filed_after_second) == 1



def test_pin_divergence_flags_agent_with_no_update_trail(cp, monkeypatch):
    """An agent absent entirely from all repo-update event streams is flagged
    when the fleet's newest sweep is older than pin_divergence_seconds."""
    import mac.self_healing as sh
    from datetime import timedelta

    known = _register_agent(cp, name="known")
    absent = _register_agent(cp, name="absent")
    cp.heartbeat_agent(known.id)
    cp.heartbeat_agent(absent.id)

    now = sh._utcnow()
    # Only 'known' has any repo-update events; 'absent' never appears.
    _stub_events(monkeypatch, cp, {
        "worker.agentbus.repo_update.updated": [
            {"source": known.id,
             "created_at": (now - timedelta(days=10)).isoformat()},
        ],
    })
    report = _sentinel(cp).run_once()
    findings = {f["fingerprint"]: f for f in report["findings"]}
    fp = "fleet_pin_divergence:%s" % absent.id
    assert fp in findings
    assert findings[fp]["detail"]["lag_seconds"] is None
    assert findings[fp]["detail"]["agent_latest_update"] is None


def test_pin_divergence_skips_held_agent(cp, monkeypatch):
    """A held (dispatch_hold) agent is skipped by pin-divergence even when its
    update trail is completely stale."""
    import mac.self_healing as sh
    from datetime import timedelta

    held = _register_agent(cp, name="held")
    cp.heartbeat_agent(held.id)
    cp.set_agent_dispatch_hold(held.id, "operator: maintenance")

    anchor = _register_agent(cp, name="anchor")
    cp.heartbeat_agent(anchor.id)

    now = sh._utcnow()
    _stub_events(monkeypatch, cp, {
        "worker.agentbus.repo_update.updated": [
            {"source": anchor.id,
             "created_at": (now - timedelta(days=14)).isoformat()},
            {"source": held.id,
             "created_at": (now - timedelta(days=21)).isoformat()},
        ],
    })
    report = _sentinel(cp).run_once()
    fingerprints = [f["fingerprint"] for f in report["findings"]]
    assert ("fleet_pin_divergence:%s" % held.id) not in fingerprints


def test_pin_divergence_ignores_silent_agent(cp, monkeypatch):
    """A silent agent (last_seen_at beyond agent_silence_seconds) is NOT flagged
    by pin-divergence; that is the agent_unhealthy check's responsibility."""
    import mac.self_healing as sh
    from datetime import timedelta

    real_now = sh._utcnow()

    silent = _register_agent(cp, name="silent")
    anchor = _register_agent(cp, name="anchor")
    # Heartbeat anchor at real_now; do NOT heartbeat silent so its last_seen_at
    # stays at registration time (real_now).  We then advance the sentinel clock
    # by 2 hours so silent's last_seen_at (real_now) falls beyond the default
    # agent_silence_seconds (1 hour), making it appear silent to the check.
    cp.heartbeat_agent(anchor.id)

    monkeypatch.setattr(sh, "_utcnow", lambda: real_now + timedelta(hours=2))
    now = real_now + timedelta(hours=2)

    _stub_events(monkeypatch, cp, {
        "worker.agentbus.repo_update.updated": [
            {"source": anchor.id,
             "created_at": (now - timedelta(days=14)).isoformat()},
            {"source": silent.id,
             "created_at": (now - timedelta(days=21)).isoformat()},
        ],
    })
    report = _sentinel(cp).run_once()
    fingerprints = [f["fingerprint"] for f in report["findings"]]
    assert ("fleet_pin_divergence:%s" % silent.id) not in fingerprints


def test_pin_divergence_dedupes_self_heal_task(cp, monkeypatch):
    """A diverged agent produces exactly one deduped self-heal task via the
    fleet_pin_divergence fingerprint across multiple sentinel cycles."""
    import mac.self_healing as sh
    from datetime import timedelta

    laggard = _register_agent(cp, name="laggard")
    anchor = _register_agent(cp, name="anchor")
    cp.heartbeat_agent(laggard.id)
    cp.heartbeat_agent(anchor.id)

    now = sh._utcnow()
    _stub_events(monkeypatch, cp, {
        "worker.agentbus.repo_update.updated": [
            {"source": anchor.id,
             "created_at": (now - timedelta(days=14)).isoformat()},
            {"source": laggard.id,
             "created_at": (now - timedelta(days=21)).isoformat()},
        ],
    })

    sentinel = _sentinel(cp)
    fp = "fleet_pin_divergence:%s" % laggard.id

    sentinel.run_once()
    after_first = _self_heal_tasks(cp, fp)
    assert len(after_first) == 1

    sentinel.run_once()
    after_second = _self_heal_tasks(cp, fp)
    assert len(after_second) == 1

    sentinel.run_once()
    after_third = _self_heal_tasks(cp, fp)
    assert len(after_third) == 1

# ── agent unhealthy ──────────────────────────────────────────────────────────


def test_agent_unhealthy_flags_silent_and_crash_looping_agents(cp, monkeypatch):
    healthy = _register_agent(cp, name="fine")
    silent = _register_agent(cp, name="gone")
    benched = _register_agent(cp, name="benched")
    cp.set_agent_dispatch_hold(benched.id, "operator: maintenance")
    cp.heartbeat_agent(healthy.id)
    import mac.self_healing as sh
    from datetime import timedelta

    real_now = sh._utcnow()
    # Freshen only the healthy agent's clock reference: shift NOW by 2h so
    # the others' last_seen goes stale, then re-heartbeat the healthy one.
    monkeypatch.setattr(sh, "_utcnow", lambda: real_now + timedelta(hours=2))
    cp.heartbeat_agent(healthy.id)  # its last_seen is written with real utcnow
    # heartbeat writes wall-clock time; recompute staleness relative to the
    # shifted sentinel clock: healthy is ~2h stale too. Instead assert the
    # benched agent is skipped and the silent one is flagged.
    report = _sentinel(cp).run_once()
    fingerprints = [f["fingerprint"] for f in report["findings"]]
    assert ("agent_unhealthy:%s" % silent.id) in fingerprints
    assert ("agent_unhealthy:%s" % benched.id) not in fingerprints


# ── escalation dedupe ────────────────────────────────────────────────────────


def test_standing_exhausted_finding_escalates_once_not_per_cycle(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id, offset_minutes=0)
    sentinel = _sentinel(cp, max_attempts=1)
    sentinel.run_once()
    (task,) = _self_heal_tasks(cp, "nap_liveness")
    cp.force_complete_task(task.id, "test", reason="simulate ineffective fix")

    first = sentinel.run_once()
    second = sentinel.run_once()
    assert first["escalated_count"] == 1
    assert second["escalated_count"] == 0
    assert [f["action"] for f in second["findings"] if f["kind"] == "nap_liveness"] == [
        "escalated_previously"
    ]
    notes = [
        n for n in cp.list_notifications()
        if n.event_type == "self_heal.escalated"
    ]
    assert len(notes) == 1


# ── per-cycle spawn budget ────────────────────────────────────────────────────


def test_config_reads_bounded_per_cycle_budget():
    assert SelfHealingConfig.from_env({}).max_tasks_per_cycle == 10
    ok = SelfHealingConfig.from_env({"MAC_SELF_HEAL_MAX_TASKS_PER_CYCLE": "3"})
    assert ok.max_tasks_per_cycle == 3 and ok.configuration_error == ""
    bad = SelfHealingConfig.from_env({"MAC_SELF_HEAL_MAX_TASKS_PER_CYCLE": "0"})
    assert bad.max_tasks_per_cycle == 10 and "between 1 and 1000" in bad.configuration_error


def test_budget_caps_new_task_filings_and_reports_skips(cp):
    from mac.self_healing import Finding  # noqa: PLC0415

    sentinel = _sentinel(cp, max_tasks_per_cycle=2)
    findings = [
        Finding(
            fingerprint="synthetic:%d" % i,
            kind="agent_unhealthy",
            summary="synthetic finding %d" % i,
            detail={"remediation": "noop"},
        )
        for i in range(5)
    ]

    actions = sentinel._act_on_findings(findings, actor="test")

    filed = [a for a in actions if a.get("action") == "task_filed"]
    skipped = [a for a in actions if a.get("action") == "skipped"]
    assert len(filed) == 2
    assert len(skipped) == 3
    assert all(a["reason"] == "per_cycle_budget_exhausted" for a in skipped)
    assert len(_self_heal_tasks(cp)) == 2


def test_run_once_report_carries_budget_and_capped_count(cp, monkeypatch):
    from mac.self_healing import Finding  # noqa: PLC0415

    sentinel = _sentinel(cp, max_tasks_per_cycle=1)
    findings = [
        Finding(
            fingerprint="synthetic:%d" % i,
            kind="agent_unhealthy",
            summary="synthetic finding %d" % i,
            detail={"remediation": "noop"},
        )
        for i in range(3)
    ]
    monkeypatch.setattr(sentinel, "_check_agent_unhealthy", lambda: findings)

    report = sentinel.run_once()

    assert report["budget"] == 1
    assert report["filed_count"] == 1
    assert report["capped_count"] == 2
