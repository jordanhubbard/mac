"""Tests for the curiosity adjudication reviewer (mac.curiosity_reviewer).

The reviewer files ONE pinned adjudication task per OpenClaw-gateway agent
on a slow cadence: dedupe while a task is active, cooldown after it closes,
and per-agent error isolation. These tests exercise config parsing, agent
selection, task shape, and the dedupe/cooldown state machine against a real
in-memory ControlPlane.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mac.curiosity_reviewer import (
    ADJUDICATION_ORIGIN_TYPE,
    CURIOSITY_REVIEWER_SCHEMA,
    CuriosityReviewer,
    CuriosityReviewerConfig,
    DEFAULT_COOLDOWN_SECONDS,
    build_adjudication_description,
    _utcnow,
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


def _register_openclaw_agent(cp, name="rocky"):
    return _register_agent(
        cp, name, resources={"chat_gateway": {"implementation": "openclaw"}}
    )


def _reviewer(cp, **cfg):
    base = {"enabled": True}
    base.update(cfg)
    return CuriosityReviewer(cp, CuriosityReviewerConfig(**base))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_config_defaults_disabled():
    cfg = CuriosityReviewerConfig.from_env({})
    assert cfg.enabled is False and cfg.active is False
    assert cfg.cooldown_seconds == DEFAULT_COOLDOWN_SECONDS


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " On "])
def test_config_enabled_flag_forms(value):
    cfg = CuriosityReviewerConfig.from_env({"MAC_CURIOSITY_REVIEW_ENABLED": value})
    assert cfg.enabled is True and cfg.active is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "disabled"])
def test_config_disabled_flag_forms(value):
    cfg = CuriosityReviewerConfig.from_env({"MAC_CURIOSITY_REVIEW_ENABLED": value})
    assert cfg.enabled is False and cfg.active is False


def test_config_reads_numeric_overrides():
    cfg = CuriosityReviewerConfig.from_env({
        "MAC_CURIOSITY_REVIEW_ENABLED": "1",
        "MAC_CURIOSITY_REVIEW_INTERVAL_SECONDS": "3600",
        "MAC_CURIOSITY_REVIEW_INITIAL_DELAY_SECONDS": "0",
        "MAC_CURIOSITY_REVIEW_COOLDOWN_SECONDS": "7200",
    })
    assert cfg.active is True
    assert cfg.interval_seconds == 3600.0
    assert cfg.initial_delay_seconds == 0.0
    assert cfg.cooldown_seconds == 7200.0


def test_config_non_numeric_sets_error_and_deactivates():
    cfg = CuriosityReviewerConfig.from_env({
        "MAC_CURIOSITY_REVIEW_ENABLED": "1",
        "MAC_CURIOSITY_REVIEW_COOLDOWN_SECONDS": "later",
    })
    assert "MAC_CURIOSITY_REVIEW_COOLDOWN_SECONDS must be numeric" in cfg.configuration_error
    assert cfg.enabled is True and cfg.active is False
    assert cfg.cooldown_seconds == DEFAULT_COOLDOWN_SECONDS


def test_config_out_of_range_sets_error_and_deactivates():
    cfg = CuriosityReviewerConfig.from_env({
        "MAC_CURIOSITY_REVIEW_ENABLED": "1",
        "MAC_CURIOSITY_REVIEW_INTERVAL_SECONDS": "1",  # below the 300s floor
    })
    assert "MAC_CURIOSITY_REVIEW_INTERVAL_SECONDS must be between" in cfg.configuration_error
    assert cfg.active is False


def test_description_contains_audit_trail_flags():
    d = build_adjudication_description("rocky", "curiosity-adjudication-abc123")
    assert "rocky" in d
    assert "--approval-id curiosity-adjudication-abc123" in d
    assert "--actor fleet_reviewer" in d
    assert "curiosity list --status quarantined" in d


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_disabled_reviewer_does_not_start(cp):
    reviewer = CuriosityReviewer(cp, CuriosityReviewerConfig(enabled=False))
    assert reviewer.start() is False
    assert reviewer.status()["thread_alive"] is False


def test_misconfigured_reviewer_does_not_start(cp):
    reviewer = CuriosityReviewer(cp, CuriosityReviewerConfig(enabled=True, configuration_error="bad"))
    assert reviewer.start() is False
    assert reviewer.status()["thread_alive"] is False


# --------------------------------------------------------------------------- #
# Agent selection
# --------------------------------------------------------------------------- #


def test_only_openclaw_gateway_agents_get_tasks(cp):
    openclaw = _register_openclaw_agent(cp, "rocky")
    _register_agent(cp, "natasha")  # no chat_gateway at all
    _register_agent(  # different gateway implementation
        cp, "boris", resources={"chat_gateway": {"implementation": "hermes"}}
    )
    _register_agent(cp, "bullwinkle", resources={"chat_gateway": "openclaw"})  # not a mapping

    report = _reviewer(cp).run_once()

    assert report["schema"] == CURIOSITY_REVIEWER_SCHEMA
    assert report["status"] == "ok"
    assert report["filed_count"] == 1
    assert [r["agent_id"] for r in report["agents"]] == [openclaw.id]
    assert report["agents"][0]["filed"] is True


def test_no_agents_files_nothing(cp):
    report = _reviewer(cp).run_once()
    assert report["filed_count"] == 0
    assert report["agents"] == []


# --------------------------------------------------------------------------- #
# Task shape
# --------------------------------------------------------------------------- #


def test_filed_task_is_pinned_with_adjudication_origin(cp):
    agent = _register_openclaw_agent(cp, "rocky")
    report = _reviewer(cp).run_once(actor="curiosity-reviewer")

    task_id = report["agents"][0]["task_id"]
    assert task_id
    task = cp.get_task(task_id)
    assert "rocky" in task.title
    assert task.state == "open"
    assert task.metadata["target_agent_id"] == agent.id
    assert task.metadata["origin"]["type"] == ADJUDICATION_ORIGIN_TYPE
    assert task.metadata["origin"]["agent_id"] == agent.id
    assert task.metadata["evidence_type"] == "investigation"
    ref = task.metadata["curiosity_approval_ref"]
    assert ref.startswith("curiosity-adjudication-")
    # The prompt tells the executor to cite this task ref as the approval id.
    assert ref in task.description


# --------------------------------------------------------------------------- #
# Dedupe / cooldown
# --------------------------------------------------------------------------- #


def test_second_run_dedupes_while_task_open(cp):
    agent = _register_openclaw_agent(cp, "rocky")
    reviewer = _reviewer(cp)
    first = reviewer.run_once()
    assert first["filed_count"] == 1

    second = reviewer.run_once()
    assert second["filed_count"] == 0
    entry = second["agents"][0]
    assert entry["agent_id"] == agent.id
    assert entry["filed"] is False
    assert entry["skipped_reason"] == "adjudication task already open"

    # Still exactly one adjudication task in the ledger.
    adjudications = [
        t for t in cp.list_tasks()
        if (t.metadata.get("origin") or {}).get("type") == ADJUDICATION_ORIGIN_TYPE
    ]
    assert len(adjudications) == 1


def test_cooldown_blocks_refile_after_task_closes(cp):
    _register_openclaw_agent(cp, "rocky")
    reviewer = _reviewer(cp)
    first = reviewer.run_once()
    task_id = first["agents"][0]["task_id"]
    cp.transition_task(task_id, "cancelled", "operator", detail={"reason": "test cleanup"})

    report = reviewer.run_once()
    assert report["filed_count"] == 0
    reason = report["agents"][0]["skipped_reason"]
    assert "ago" in reason
    assert "%.0f" % DEFAULT_COOLDOWN_SECONDS in reason


def test_refiles_after_cooldown_elapses(cp, monkeypatch):
    _register_openclaw_agent(cp, "rocky")
    reviewer = _reviewer(cp)
    first = reviewer.run_once()
    cp.transition_task(first["agents"][0]["task_id"], "cancelled", "operator",
                       detail={"reason": "test cleanup"})

    # Jump the reviewer's clock past the cooldown window.
    future = _utcnow() + timedelta(seconds=DEFAULT_COOLDOWN_SECONDS + 60)
    monkeypatch.setattr("mac.curiosity_reviewer._utcnow", lambda: future)

    report = reviewer.run_once()
    assert report["filed_count"] == 1
    new_task_id = report["agents"][0]["task_id"]
    assert new_task_id != first["agents"][0]["task_id"]


def test_cooldown_ignores_active_task_of_other_agent(cp):
    rocky = _register_openclaw_agent(cp, "rocky")
    natasha = _register_openclaw_agent(cp, "natasha")
    reviewer = _reviewer(cp)
    first = reviewer.run_once()
    assert first["filed_count"] == 2

    by_id = {r["agent_id"]: r for r in first["agents"]}
    cp.transition_task(by_id[rocky.id]["task_id"], "cancelled", "operator",
                       detail={"reason": "test cleanup"})

    second = reviewer.run_once()
    by_id = {r["agent_id"]: r for r in second["agents"]}
    # rocky is in cooldown; natasha's task is still open.
    assert "ago" in by_id[rocky.id]["skipped_reason"]
    assert by_id[natasha.id]["skipped_reason"] == "adjudication task already open"
    assert second["filed_count"] == 0


# --------------------------------------------------------------------------- #
# Error isolation
# --------------------------------------------------------------------------- #


def test_one_agents_create_failure_is_isolated(cp, monkeypatch):
    bad = _register_openclaw_agent(cp, "boris")
    good = _register_openclaw_agent(cp, "rocky")
    real_create = cp.create_task

    def flaky(title, **kwargs):
        if "boris" in title:
            raise RuntimeError("ledger write refused")
        return real_create(title, **kwargs)

    monkeypatch.setattr(cp, "create_task", flaky)

    report = _reviewer(cp).run_once()

    by_id = {r["agent_id"]: r for r in report["agents"]}
    assert by_id[bad.id]["filed"] is False
    assert by_id[bad.id]["error"] == "ledger write refused"
    assert by_id[good.id]["filed"] is True
    assert report["filed_count"] == 1
    assert report["status"] == "ok"


def test_run_once_survives_task_listing_failure(cp, monkeypatch):
    _register_openclaw_agent(cp, "rocky")

    def boom(**_):
        raise RuntimeError("ledger unreachable")

    monkeypatch.setattr(cp, "list_tasks", boom)
    # Snapshot failure degrades to "nothing known" -> the task still files.
    report = _reviewer(cp).run_once()
    assert report["status"] == "ok"
    assert report["filed_count"] == 1


def test_run_once_returns_busy_when_already_running(cp):
    reviewer = _reviewer(cp)
    assert reviewer._run_lock.acquire(blocking=False)
    try:
        report = reviewer.run_once()
    finally:
        reviewer._run_lock.release()
    assert report["status"] == "busy"
    assert report["agents"] == []


def test_status_reflects_last_report(cp):
    _register_openclaw_agent(cp, "rocky")
    reviewer = _reviewer(cp)
    assert reviewer.status()["last_report"] is None

    report = reviewer.run_once(trigger="test")
    status = reviewer.status()
    assert status["schema"] == CURIOSITY_REVIEWER_SCHEMA
    assert status["last_report"]["run_id"] == report["run_id"]
    assert status["last_report"]["trigger"] == "test"
    assert status["last_report"]["filed_count"] == 1
    assert status["thread_alive"] is False
    assert status["run_active"] is False
