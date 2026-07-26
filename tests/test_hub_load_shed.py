"""Bounded work on the co-located hub host + load-shed circuit-breaker.

Covers the four acceptance requirements (task_1bd5db4b):

* co-location detection (hub agent + local control plane; nothing else),
* resource caps (MAC_TEST_JOBS as a configurable fraction of cores; task cap),
* the trip / drain / recover hysteresis under synthetic control-plane load,
* the observable breaker state + triggering metric.
"""

from __future__ import annotations

import pytest

from mac.hub_load_shed import (
    BreakerState,
    ControlPlaneSample,
    HubLoadShedConfig,
    LoadShedBreaker,
    is_hub_host,
    resolve_hub_test_jobs,
    resolve_test_jobs,
)


# -- co-location detection -------------------------------------------------


def _yes():
    return True


def _no():
    return False


def test_hub_host_requires_both_hub_agent_and_control_plane():
    env = {"MAC_HUB_LOAD_SHED_AGENT": "rocky"}
    # Hub agent AND control plane present => hub host.
    assert is_hub_host("rocky", environ=env, control_plane_probe=_yes) is True
    # Hub agent but NO control plane => not co-located.
    assert is_hub_host("rocky", environ=env, control_plane_probe=_no) is False


def test_non_hub_worker_is_never_a_hub_host():
    env = {"MAC_HUB_LOAD_SHED_AGENT": "rocky"}
    # A different agent, even with a control plane locally, is not the hub.
    assert is_hub_host("otterbot", environ=env, control_plane_probe=_yes) is False


def test_hub_host_matches_on_agent_name_too():
    env = {"MAC_HUB_LOAD_SHED_AGENT": "rocky"}
    assert is_hub_host("agent_123", "rocky", environ=env, control_plane_probe=_yes) is True


def test_hub_host_falls_back_to_shared_hub_selectors():
    env = {"MAC_REVIEW_TICK_HUB_AGENT": "rocky"}
    assert is_hub_host("rocky", environ=env, control_plane_probe=_yes) is True


def test_hub_host_disabled_and_force_flags():
    env = {"MAC_HUB_LOAD_SHED_AGENT": "rocky", "MAC_HUB_LOAD_SHED_DISABLED": "1"}
    assert is_hub_host("rocky", environ=env, control_plane_probe=_yes) is False
    env2 = {"MAC_HUB_LOAD_SHED_FORCE": "1"}
    assert is_hub_host("anyone", environ=env2, control_plane_probe=_no) is True


def test_no_hub_agent_configured_means_no_shedding():
    assert is_hub_host("rocky", environ={}, control_plane_probe=_yes) is False


def test_probe_failure_is_safe():
    env = {"MAC_HUB_LOAD_SHED_AGENT": "rocky"}

    def _boom():
        raise RuntimeError("no /proc")

    assert is_hub_host("rocky", environ=env, control_plane_probe=_boom) is False


# -- resource caps ---------------------------------------------------------


def test_test_jobs_capped_to_fraction_of_cores():
    cfg = HubLoadShedConfig(test_jobs_fraction=0.5)
    # 12 cores * 0.5 => 6; a single gate run cannot use all 12.
    assert resolve_test_jobs(12, cfg) == 6
    assert resolve_test_jobs(12, cfg) < 12


def test_test_jobs_fraction_is_configurable():
    assert resolve_test_jobs(12, HubLoadShedConfig(test_jobs_fraction=0.25)) == 3
    assert resolve_test_jobs(12, HubLoadShedConfig(test_jobs_fraction=0.75)) == 9


def test_test_jobs_respects_min_and_max():
    cfg = HubLoadShedConfig(test_jobs_fraction=0.5, min_test_jobs=2, max_test_jobs=4)
    assert resolve_test_jobs(2, cfg) == 2      # min floor
    assert resolve_test_jobs(20, cfg) == 4     # max ceiling
    assert resolve_test_jobs(1, cfg) == 2      # never below 1/min


def test_config_from_env_overrides_fraction_and_watermarks():
    env = {
        "MAC_HUB_TEST_JOBS_FRACTION": "0.25",
        "MAC_HUB_MAX_CONCURRENT_TASKS": "2",
        "MAC_HUB_LOAD_SHED_HIGH": "0.9",
        "MAC_HUB_LOAD_SHED_LOW": "0.4",
    }
    cfg = HubLoadShedConfig.from_env(env)
    assert cfg.test_jobs_fraction == 0.25
    assert cfg.max_concurrent_tasks == 2
    assert cfg.load_high == 0.9
    assert cfg.load_low == 0.4


def test_resolve_hub_test_jobs_none_for_non_hub():
    assert resolve_hub_test_jobs("otter", environ={}, control_plane_probe=_yes) is None


def test_resolve_hub_test_jobs_caps_on_hub():
    env = {"MAC_HUB_LOAD_SHED_AGENT": "rocky", "MAC_HUB_TEST_JOBS_FRACTION": "0.5"}
    jobs = resolve_hub_test_jobs("rocky", cores=12, environ=env, control_plane_probe=_yes)
    assert jobs == 6


# -- load-shed hysteresis (trip / drain / recover) -------------------------


class _Sampler:
    """Mutable control-plane metric source for deterministic breaker tests."""

    def __init__(self, load_ratio=0.0, cpu_percent=None, rss_mb=None):
        self.sample = ControlPlaneSample(load_ratio, cpu_percent, rss_mb)

    def set_load(self, load_ratio):
        self.sample = ControlPlaneSample(load_ratio, self.sample.cpu_percent, self.sample.rss_mb)

    def __call__(self):
        return self.sample


def _breaker(**overrides):
    cfg = HubLoadShedConfig(load_high=0.85, load_low=0.55, **overrides)
    sampler = _Sampler(load_ratio=0.1)
    return LoadShedBreaker(cfg, sampler), sampler


def test_breaker_claims_below_low_watermark():
    b, s = _breaker()
    s.set_load(0.2)
    assert b.state() is BreakerState.CLAIMING
    assert b.should_claim() is True


def test_breaker_trips_and_sheds_above_high_watermark():
    b, s = _breaker()
    s.set_load(0.9)  # >= high 0.85, no work in flight
    assert b.state() is BreakerState.SHEDDING
    assert b.should_claim() is False


def test_breaker_drains_when_task_in_flight_under_load():
    b, s = _breaker()
    b.task_started()
    s.set_load(0.9)
    assert b.state() is BreakerState.DRAINING
    assert b.should_drain() is True
    assert b.should_claim() is False


def test_hysteresis_stays_tripped_between_low_and_high():
    b, s = _breaker()
    s.set_load(0.9)          # trip
    assert b.state() is BreakerState.SHEDDING
    s.set_load(0.7)          # between low(0.55) and high(0.85): still shedding
    assert b.state() is BreakerState.SHEDDING
    assert b.should_claim() is False


def test_breaker_recovers_below_low_watermark():
    b, s = _breaker()
    s.set_load(0.9)          # trip
    assert b.state() is BreakerState.SHEDDING
    s.set_load(0.5)          # <= low 0.55: recover
    assert b.state() is BreakerState.CLAIMING
    assert b.should_claim() is True


def test_trip_and_recovery_counters():
    b, s = _breaker()
    s.set_load(0.9)
    b.state()
    s.set_load(0.4)
    b.state()
    s.set_load(0.95)
    b.state()
    snap = b.snapshot()
    assert snap.trips == 2
    assert snap.recoveries == 1


def test_concurrent_task_cap_blocks_claims():
    b, s = _breaker(max_concurrent_tasks=1)
    s.set_load(0.1)  # load is fine
    assert b.should_claim() is True
    b.task_started()
    # At capacity even though load is low: cannot claim a second task.
    assert b.at_task_capacity() is True
    assert b.should_claim() is False
    b.task_finished()
    assert b.should_claim() is True


def test_cpu_metric_can_trip_and_drive_observability():
    cfg = HubLoadShedConfig(load_high=0.85, load_low=0.55, cpu_high=70.0, cpu_low=40.0)
    sampler = _Sampler(load_ratio=0.1, cpu_percent=90.0)
    b = LoadShedBreaker(cfg, sampler)
    assert b.state() is BreakerState.SHEDDING
    snap = b.snapshot()
    assert snap.metric == "control_plane_cpu"
    assert snap.value == pytest.approx(90.0)
    assert snap.high == pytest.approx(70.0)


def test_snapshot_is_observable_dict():
    b, s = _breaker()
    s.set_load(0.9)
    d = b.snapshot().to_dict()
    assert d["schema"] == "mac.hub_load_shed.v1"
    assert d["state"] == "shedding"
    assert d["metric"] == "load_ratio"
    assert d["high"] == pytest.approx(0.85)
    assert set(["state", "metric", "value", "high", "low", "trips"]).issubset(d)


def test_rss_opt_in_does_not_trip_when_zero():
    cfg = HubLoadShedConfig(load_high=0.85, load_low=0.55, rss_high_mb=0.0)
    sampler = _Sampler(load_ratio=0.1, rss_mb=99999.0)
    b = LoadShedBreaker(cfg, sampler)
    # RSS is huge but rss_high_mb=0 means it is not a trip signal.
    assert b.state() is BreakerState.CLAIMING


def test_full_trip_drain_recover_cycle():
    b, s = _breaker(max_concurrent_tasks=1)
    # 1. Idle + low load: claiming.
    s.set_load(0.2)
    assert b.state() is BreakerState.CLAIMING
    # 2. A task starts; load spikes -> draining (keep current, claim nothing).
    b.task_started()
    s.set_load(0.95)
    assert b.state() is BreakerState.DRAINING
    # 3. Task finishes while still under load -> shedding.
    b.task_finished()
    assert b.state() is BreakerState.SHEDDING
    # 4. Load falls back below low -> claiming again (hysteresis recover).
    s.set_load(0.3)
    assert b.state() is BreakerState.CLAIMING
    assert b.should_claim() is True


# -- worker claim-loop gate integration ------------------------------------


def test_worker_gate_holds_claim_when_shedding():
    """The worker's ``_maybe_hub_load_shed`` returns a held result under load.

    Exercises the exact method wired into ``run_once`` before ``_claim_next``
    without constructing a full MacWorker: it depends only on the breaker and a
    log sink, both of which we bind onto a light stand-in.
    """
    from mac import worker as worker_mod

    b, s = _breaker()
    s.set_load(0.95)  # trip -> shedding

    class _Stub:
        agent_id = "rocky"

        def __init__(self):
            self._hub_load_shed = b
            self._last_hub_shed_state = None
            self.logs = []

        def _observe_log(self, name, level="info", subject_type=None, subject_id=None, detail=None):
            self.logs.append((name, level, detail))

    stub = _Stub()
    result = worker_mod.MacWorker._maybe_hub_load_shed(stub)
    assert result is not None
    assert result.status == "held"
    assert "hub load-shed" in (result.error or "")
    assert result.evidence["state"] == "shedding"
    assert any(name == "worker.hub_load_shed.shedding" for name, _l, _d in stub.logs)


def test_worker_gate_allows_claim_when_calm():
    from mac import worker as worker_mod

    b, s = _breaker()
    s.set_load(0.2)  # calm -> claiming

    class _Stub:
        agent_id = "rocky"

        def __init__(self):
            self._hub_load_shed = b
            self._last_hub_shed_state = None

        def _observe_log(self, *a, **k):
            pass

    result = worker_mod.MacWorker._maybe_hub_load_shed(_Stub())
    assert result is None  # None => proceed to claim


def test_worker_gate_noop_for_non_hub_worker():
    from mac import worker as worker_mod

    class _Stub:
        agent_id = "otter"

        def __init__(self):
            self._hub_load_shed = None  # non-hub worker has no breaker
            self._last_hub_shed_state = None

        def _observe_log(self, *a, **k):
            pass

    assert worker_mod.MacWorker._maybe_hub_load_shed(_Stub()) is None
