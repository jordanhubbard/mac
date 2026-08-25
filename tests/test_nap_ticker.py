"""Tests for the in-hub autonomous nap driver (mac.nap_ticker).

The ticker mirrors BacklogGroomer: a daemon thread wakes on an interval,
asks the ledger which agents' nap windows have opened, and drives each
through one full ``run_nap_cycle``. These tests exercise config parsing,
the inactive/no-op path, and ``run_once`` against a real in-memory
ControlPlane.
"""

from __future__ import annotations

import pytest

from mac.nap_ticker import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_AGENTS_PER_TICK,
    NAP_TICKER_SCHEMA,
    NapTicker,
    NapTickerConfig,
)
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _register_agent(cp, name="rocky", capabilities=None):
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(machine.id, name, capabilities=capabilities or ["ops"])


def _due_agent(cp, name="rocky", capabilities=None):
    """Register an agent whose nap window is open right now.

    offset_minutes=0 puts window_start at the top of the current cadence
    bucket, and a fresh schedule has last_completed_at NULL, so
    list_due_nap_agents reports the agent due immediately.
    """
    agent = _register_agent(cp, name, capabilities=capabilities)
    cp.configure_nap(agent.id, offset_minutes=0)
    return agent


def _ticker(cp, **cfg):
    base = {"enabled": True}
    base.update(cfg)
    return NapTicker(cp, NapTickerConfig(**base))


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_config_defaults_disabled():
    cfg = NapTickerConfig.from_env({})
    assert cfg.enabled is False and cfg.active is False
    assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS
    assert cfg.max_agents_per_tick == DEFAULT_MAX_AGENTS_PER_TICK


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE "])
def test_config_enabled_flag_forms(value):
    cfg = NapTickerConfig.from_env({"MAC_NAP_TICK_ENABLED": value})
    assert cfg.enabled is True and cfg.active is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nope"])
def test_config_disabled_flag_forms(value):
    cfg = NapTickerConfig.from_env({"MAC_NAP_TICK_ENABLED": value})
    assert cfg.enabled is False and cfg.active is False


def test_config_reads_numeric_overrides():
    cfg = NapTickerConfig.from_env(
        {
            "MAC_NAP_TICK_ENABLED": "1",
            "MAC_NAP_TICK_INTERVAL_SECONDS": "120",
            "MAC_NAP_TICK_INITIAL_DELAY_SECONDS": "0",
            "MAC_NAP_TICK_MAX_AGENTS_PER_TICK": "3",
        }
    )
    assert cfg.active is True
    assert cfg.interval_seconds == 120.0
    assert cfg.initial_delay_seconds == 0.0
    assert cfg.max_agents_per_tick == 3
    assert cfg.configuration_error == ""


def test_config_non_numeric_sets_error_and_deactivates():
    cfg = NapTickerConfig.from_env(
        {
            "MAC_NAP_TICK_ENABLED": "1",
            "MAC_NAP_TICK_INTERVAL_SECONDS": "abc",
        }
    )
    assert "MAC_NAP_TICK_INTERVAL_SECONDS must be numeric" in cfg.configuration_error
    assert cfg.enabled is True and cfg.active is False
    # The default survives the rejected override.
    assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS


def test_config_out_of_range_sets_error_and_deactivates():
    cfg = NapTickerConfig.from_env(
        {
            "MAC_NAP_TICK_ENABLED": "1",
            "MAC_NAP_TICK_INTERVAL_SECONDS": "1",  # below the 60s floor
            "MAC_NAP_TICK_MAX_AGENTS_PER_TICK": "1000",  # above the 100 cap
        }
    )
    assert "MAC_NAP_TICK_INTERVAL_SECONDS must be between" in cfg.configuration_error
    assert "MAC_NAP_TICK_MAX_AGENTS_PER_TICK must be between" in cfg.configuration_error
    assert cfg.active is False


def test_config_to_dict_includes_active():
    cfg = NapTickerConfig(enabled=True)
    assert cfg.to_dict()["active"] is True
    assert NapTickerConfig(enabled=True, configuration_error="x").to_dict()["active"] is False


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_disabled_ticker_does_not_start(cp):
    ticker = NapTicker(cp, NapTickerConfig(enabled=False))
    assert ticker.start() is False
    assert ticker.status()["thread_alive"] is False


def test_misconfigured_ticker_does_not_start(cp):
    ticker = NapTicker(cp, NapTickerConfig(enabled=True, configuration_error="bad"))
    assert ticker.start() is False
    assert ticker.status()["thread_alive"] is False


# --------------------------------------------------------------------------- #
# run_once against a real in-memory ControlPlane
# --------------------------------------------------------------------------- #


def test_run_once_naps_due_agent(cp):
    agent = _due_agent(cp)
    report = _ticker(cp).run_once()

    assert report["schema"] == NAP_TICKER_SCHEMA
    assert report["status"] == "ok"
    assert report["due_count"] == 1
    assert report["napped_count"] == 1
    assert report["skipped_count"] == 0
    assert report["deferred_count"] == 0
    entry = report["agents"][0]
    assert entry["agent_id"] == agent.id
    assert entry["napped"] is True
    assert entry["nap_run_id"]

    # The cycle actually ran: the schedule remembers the completion and the
    # nap run is recorded against the agent.
    schedule = cp.get_nap_schedule(agent.id)
    assert schedule.last_completed_at is not None
    runs = cp.list_nap_runs(agent_id=agent.id)
    assert any(r.id == entry["nap_run_id"] for r in runs)

    # Having completed this window, the agent is no longer due.
    followup = _ticker(cp).run_once()
    assert followup["due_count"] == 0
    assert followup["napped_count"] == 0


def test_run_once_reports_busy_agent_as_skipped(cp):
    agent = _due_agent(cp, "rocky", capabilities=["python"])
    task = cp.create_task("hold", required_capabilities=["python"])
    cp.claim_task(task.id, agent.id)  # active lease -> begin_nap refuses

    report = _ticker(cp).run_once()

    assert report["due_count"] == 1
    assert report["napped_count"] == 0
    assert report["skipped_count"] == 1
    entry = report["agents"][0]
    assert entry["agent_id"] == agent.id
    assert entry["skipped"] is True
    assert entry["skip_reason"]


def test_run_once_caps_agents_per_tick_and_defers_the_rest(cp):
    for name in ("rocky", "natasha", "bullwinkle"):
        _due_agent(cp, name)

    report = _ticker(cp, max_agents_per_tick=1).run_once()

    assert report["due_count"] == 3
    assert len(report["agents"]) == 1
    assert report["napped_count"] == 1
    assert report["deferred_count"] == 2

    # Deferred agents are picked up by later ticks (catch-up semantics).
    second = _ticker(cp, max_agents_per_tick=1).run_once()
    assert second["due_count"] == 2
    assert second["napped_count"] == 1
    assert second["deferred_count"] == 1


def test_run_once_isolates_one_agents_cycle_failure(cp, monkeypatch):
    bad = _due_agent(cp, "rocky")
    good = _due_agent(cp, "natasha")
    real_cycle = cp.run_nap_cycle

    def flaky(agent_id, **kwargs):
        if agent_id == bad.id:
            raise RuntimeError("consolidator exploded")
        return real_cycle(agent_id, **kwargs)

    monkeypatch.setattr(cp, "run_nap_cycle", flaky)

    report = _ticker(cp).run_once()

    by_id = {r["agent_id"]: r for r in report["agents"]}
    assert by_id[bad.id]["napped"] is False
    assert by_id[bad.id]["error"] == "consolidator exploded"
    assert by_id[good.id]["napped"] is True
    assert "error" not in by_id[good.id]
    # The run itself still reports ok; the failure lives in the agent entry.
    assert report["status"] == "ok"
    assert report["napped_count"] == 1


def test_run_once_skips_entries_without_agent_id(cp, monkeypatch):
    monkeypatch.setattr(
        cp, "list_due_nap_agents", lambda **_: [{"agent_id": ""}, {"agent_id": None}]
    )
    report = _ticker(cp).run_once()
    assert report["due_count"] == 2
    assert report["agents"] == []
    assert report["napped_count"] == 0


def test_run_once_survives_due_listing_failure(cp, monkeypatch):
    def boom(**_):
        raise RuntimeError("ledger unreachable")

    monkeypatch.setattr(cp, "list_due_nap_agents", boom)
    report = _ticker(cp).run_once()
    assert report["status"] == "ok"
    assert report["due_count"] == 0
    assert report["agents"] == []


def test_run_once_returns_busy_when_already_running(cp):
    ticker = _ticker(cp)
    assert ticker._run_lock.acquire(blocking=False)
    try:
        report = ticker.run_once()
    finally:
        ticker._run_lock.release()
    assert report["status"] == "busy"
    assert report["agents"] == []


def test_status_reflects_last_report(cp):
    _due_agent(cp)
    ticker = _ticker(cp)

    before = ticker.status()
    assert before["schema"] == NAP_TICKER_SCHEMA
    assert before["last_report"] is None
    assert before["thread_alive"] is False
    assert before["run_active"] is False

    report = ticker.run_once(trigger="test")
    status = ticker.status()
    assert status["last_report"]["run_id"] == report["run_id"]
    assert status["last_report"]["trigger"] == "test"
    assert status["last_report"]["napped_count"] == 1
    assert status["config"]["active"] is True
    # status() returns a copy, not a live reference to internal state.
    status["last_report"]["napped_count"] = 999
    assert ticker.status()["last_report"]["napped_count"] == 1


# --------------------------------------------------------------------------- #
# MAC_NAP_TICK_INTERVAL_SECONDS=0 as a clean disable signal
# --------------------------------------------------------------------------- #


def test_interval_zero_disables_cleanly():
    """Interval=0 is a no-error opt-out; does not require MAC_NAP_TICK_ENABLED."""
    cfg = NapTickerConfig.from_env({"MAC_NAP_TICK_INTERVAL_SECONDS": "0"})
    assert cfg.enabled is False
    assert cfg.active is False
    assert cfg.configuration_error == ""


def test_interval_zero_overrides_enabled_flag():
    """MAC_NAP_TICK_INTERVAL_SECONDS=0 wins even if MAC_NAP_TICK_ENABLED=1."""
    cfg = NapTickerConfig.from_env(
        {
            "MAC_NAP_TICK_ENABLED": "1",
            "MAC_NAP_TICK_INTERVAL_SECONDS": "0",
        }
    )
    assert cfg.enabled is False
    assert cfg.active is False
    assert cfg.configuration_error == ""


def test_interval_zero_ticker_does_not_start(cp):
    """A ticker built from an interval=0 config never starts a thread."""
    cfg = NapTickerConfig.from_env({"MAC_NAP_TICK_INTERVAL_SECONDS": "0"})
    ticker = NapTicker(cp, cfg)
    assert ticker.start() is False
    assert ticker.status()["thread_alive"] is False


def test_interval_zero_preserves_defaults():
    """When interval=0, returned config has all other defaults intact."""
    cfg = NapTickerConfig.from_env({"MAC_NAP_TICK_INTERVAL_SECONDS": "0"})
    assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS
    assert cfg.max_agents_per_tick == DEFAULT_MAX_AGENTS_PER_TICK


# --------------------------------------------------------------------------- #
# Positive interval self-enables without MAC_NAP_TICK_ENABLED
# --------------------------------------------------------------------------- #


def test_positive_interval_self_enables():
    """Setting MAC_NAP_TICK_INTERVAL_SECONDS to a positive value enables the ticker."""
    cfg = NapTickerConfig.from_env({"MAC_NAP_TICK_INTERVAL_SECONDS": "300"})
    assert cfg.enabled is True
    assert cfg.active is True
    assert cfg.interval_seconds == 300.0
    assert cfg.configuration_error == ""


def test_positive_interval_without_enabled_flag_runs_once(cp):
    """run_once works when self-enabled via interval without MAC_NAP_TICK_ENABLED."""
    agent = _due_agent(cp)
    cfg = NapTickerConfig.from_env({"MAC_NAP_TICK_INTERVAL_SECONDS": "300"})
    ticker = NapTicker(cp, cfg)
    report = ticker.run_once()
    assert report["status"] == "ok"
    assert report["napped_count"] == 1
    entry = report["agents"][0]
    assert entry["agent_id"] == agent.id
    assert entry["napped"] is True


def test_positive_interval_with_invalid_value_does_not_self_enable():
    """An out-of-range positive interval produces an error and does not self-enable."""
    cfg = NapTickerConfig.from_env({"MAC_NAP_TICK_INTERVAL_SECONDS": "1"})
    assert cfg.enabled is False
    assert cfg.active is False
    assert "MAC_NAP_TICK_INTERVAL_SECONDS must be between" in cfg.configuration_error


def test_legacy_enabled_flag_still_works_without_interval():
    """MAC_NAP_TICK_ENABLED=1 alone still enables when interval is unset."""
    cfg = NapTickerConfig.from_env({"MAC_NAP_TICK_ENABLED": "1"})
    assert cfg.enabled is True
    assert cfg.active is True
    assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS
