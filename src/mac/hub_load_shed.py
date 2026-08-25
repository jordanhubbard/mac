"""Bounded work on the co-located hub host with a load-shed circuit-breaker.

The hub host runs BOTH the control-plane process (``uvicorn mac.api`` /
``com.<fleet>.control-plane``) AND a registered worker. Historically the box was
manually HELD out of the worker pool ("hub host resource isolation") after a
heavy sandbox task plus the ~34-minute contract gate saturated the host and
starved the control plane. A blanket hold wastes most of a capable machine, so
this module makes bounded co-located work SAFE and permanent:

* **Co-location detection** (:func:`is_hub_host`): the caps + breaker activate
  ONLY on the host that is both the resolved hub agent AND runs a local
  control-plane process. Non-hub workers are entirely unaffected.
* **Resource caps** (:func:`resolve_test_jobs`, :class:`HubLoadShedConfig`): cap
  test parallelism to a configurable *fraction* of cores and cap concurrent
  tasks, so a single gate run can never consume the whole host.
* **Load-shed circuit-breaker** (:class:`LoadShedBreaker`): a hysteresis breaker
  that samples the control-plane CPU/RSS and system load. Above the HIGH-water
  threshold it STOPS claiming and drains/pauses in-flight work; below the
  LOW-water threshold it RESUMES claiming. Protecting the coordination layer for
  the whole fleet is far cheaper than one idle worker.
* **Observable** (:class:`BreakerState`, :meth:`LoadShedBreaker.snapshot`): the
  breaker exposes its state (``claiming`` | ``shedding`` | ``draining``) and the
  metric that tripped it, so an operator can see *why* the hub host is or isn't
  working.

The metric samplers and clock are injected (the ``sampler``/``clock`` seams) so
the trip/drain/recover hysteresis is deterministically testable without a live
control plane.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Optional

from mac.env_config import env_bool, env_float, env_int, resolve_hub_agent

__all__ = [
    "BreakerState",
    "ControlPlaneSample",
    "HubLoadShedConfig",
    "LoadShedBreaker",
    "default_control_plane_sampler",
    "is_hub_host",
    "resolve_hub_test_jobs",
    "resolve_test_jobs",
]


class BreakerState(str, Enum):
    """Observable load-shed states for the co-located hub worker."""

    CLAIMING = "claiming"  # below LOW-water: safe to claim new work
    SHEDDING = "shedding"  # above HIGH-water and idle: refuse to claim
    DRAINING = "draining"  # above HIGH-water with work in flight: pause/drain


@dataclass(frozen=True)
class ControlPlaneSample:
    """A single reading of the metrics that drive the breaker.

    ``load_ratio`` is the 1-minute system load average divided by the CPU count
    (so it is comparable across differently-sized hosts). ``cpu_percent`` and
    ``rss_mb`` describe the control-plane process itself; either being unknown
    (``None``) simply means that signal does not contribute to a trip.
    """

    load_ratio: float = 0.0
    cpu_percent: Optional[float] = None
    rss_mb: Optional[float] = None


@dataclass(frozen=True)
class HubLoadShedConfig:
    """Bounded-work configuration for the co-located hub host.

    Every knob is overridable from the environment so an operator can tune the
    hub without a redeploy; the defaults are conservative for a 12-core box that
    also hosts the control plane.
    """

    # Fraction of cores a single hub-host gate run may use for test parallelism.
    test_jobs_fraction: float = 0.5
    min_test_jobs: int = 1
    max_test_jobs: int = 0  # 0 => no explicit ceiling beyond the fraction
    # Concurrent-task cap for the hub-host worker.
    max_concurrent_tasks: int = 1
    # Load-shed hysteresis watermarks. HIGH must exceed LOW or hysteresis
    # collapses into a flapping single threshold.
    load_high: float = 0.85  # system load / cores
    load_low: float = 0.55
    cpu_high: float = 70.0  # control-plane process CPU %
    cpu_low: float = 40.0
    rss_high_mb: float = 0.0  # 0 => RSS does not trip (opt-in)
    rss_low_mb: float = 0.0

    def normalized(self) -> "HubLoadShedConfig":
        """Return a copy with watermarks/fractions coerced into a sane range."""
        fraction = min(max(self.test_jobs_fraction, 0.05), 1.0)
        load_high = max(self.load_high, 0.05)
        load_low = min(max(self.load_low, 0.0), load_high - 0.01)
        cpu_high = max(self.cpu_high, 1.0)
        cpu_low = min(max(self.cpu_low, 0.0), cpu_high - 0.5)
        rss_high = max(self.rss_high_mb, 0.0)
        rss_low = min(max(self.rss_low_mb, 0.0), rss_high) if rss_high else 0.0
        return HubLoadShedConfig(
            test_jobs_fraction=fraction,
            min_test_jobs=max(1, self.min_test_jobs),
            max_test_jobs=max(0, self.max_test_jobs),
            max_concurrent_tasks=max(1, self.max_concurrent_tasks),
            load_high=load_high,
            load_low=load_low,
            cpu_high=cpu_high,
            cpu_low=cpu_low,
            rss_high_mb=rss_high,
            rss_low_mb=rss_low,
        )

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "HubLoadShedConfig":
        env = os.environ if environ is None else environ
        return cls(
            test_jobs_fraction=env_float(
                "MAC_HUB_TEST_JOBS_FRACTION", cls.test_jobs_fraction, environ=env
            ),
            min_test_jobs=env_int("MAC_HUB_MIN_TEST_JOBS", cls.min_test_jobs, environ=env),
            max_test_jobs=env_int("MAC_HUB_MAX_TEST_JOBS", cls.max_test_jobs, environ=env),
            max_concurrent_tasks=env_int(
                "MAC_HUB_MAX_CONCURRENT_TASKS", cls.max_concurrent_tasks, environ=env
            ),
            load_high=env_float("MAC_HUB_LOAD_SHED_HIGH", cls.load_high, environ=env),
            load_low=env_float("MAC_HUB_LOAD_SHED_LOW", cls.load_low, environ=env),
            cpu_high=env_float("MAC_HUB_CONTROL_PLANE_CPU_HIGH", cls.cpu_high, environ=env),
            cpu_low=env_float("MAC_HUB_CONTROL_PLANE_CPU_LOW", cls.cpu_low, environ=env),
            rss_high_mb=env_float(
                "MAC_HUB_CONTROL_PLANE_RSS_HIGH_MB", cls.rss_high_mb, environ=env
            ),
            rss_low_mb=env_float("MAC_HUB_CONTROL_PLANE_RSS_LOW_MB", cls.rss_low_mb, environ=env),
        ).normalized()


def is_hub_host(
    agent_id: str,
    agent_name: str = "",
    *,
    environ: Optional[Mapping[str, str]] = None,
    control_plane_probe: Optional[Callable[[], bool]] = None,
) -> bool:
    """Return ``True`` only on the co-located hub host.

    A host qualifies when it is the resolved hub agent (matching ``agent_id`` or
    ``agent_name`` against ``MAC_HUB_LOAD_SHED_AGENT`` / the shared hub-agent
    selectors) AND a control-plane process is present locally. Either half alone
    is insufficient: a non-hub worker never sheds, and the hub agent running on a
    box without the control plane (mis-set var) is not treated as co-located.

    ``MAC_HUB_LOAD_SHED_FORCE`` forces the co-located path on (for a host whose
    control-plane process cannot be probed) and ``MAC_HUB_LOAD_SHED_DISABLED``
    forces it off.
    """
    env = os.environ if environ is None else environ
    if env_bool("MAC_HUB_LOAD_SHED_DISABLED", False, environ=env):
        return False
    if env_bool("MAC_HUB_LOAD_SHED_FORCE", False, environ=env):
        return True
    hub_agent = resolve_hub_agent(
        "MAC_HUB_LOAD_SHED_AGENT",
        "MAC_SHARED_SERVICES_MANAGER_AGENT",
        "MAC_NOTIFIER_DRAIN_HUB_AGENT",
        "MAC_REVIEW_TICK_HUB_AGENT",
        environ=env,
    )
    if not hub_agent:
        return False
    if agent_id != hub_agent and (not agent_name or agent_name != hub_agent):
        return False
    probe = control_plane_probe or default_control_plane_present
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001 - a probe failure must never crash the claim loop.
        return False


def default_control_plane_present(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Best-effort local detection of a running control-plane process.

    Scans ``/proc/*/cmdline`` (Linux) for the ``uvicorn mac.api`` signature. On
    platforms without ``/proc`` (macOS is the real hub), operators set
    ``MAC_HUB_LOAD_SHED_FORCE=1``; this probe returning ``False`` there is
    expected and safe (co-location simply stays off unless forced).
    """
    proc = "/proc"
    if not os.path.isdir(proc):
        return False
    try:
        entries = os.listdir(proc)
    except OSError:
        return False
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc, entry, "cmdline"), "rb") as handle:
                cmdline = handle.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "mac.api" in cmdline and ("uvicorn" in cmdline or "gunicorn" in cmdline):
            return True
    return False


def _read_load_ratio() -> float:
    """1-minute load average divided by CPU count, or 0.0 when unavailable."""
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        return 0.0
    cores = os.cpu_count() or 1
    return load1 / max(1, cores)


def _read_control_plane_process():
    """Return ``(cpu_percent, rss_mb)`` for the local control-plane process.

    Best-effort and Linux-only via ``/proc``; returns ``(None, None)`` elsewhere
    so those signals simply do not contribute to a trip (load still does).
    """
    proc = "/proc"
    if not os.path.isdir(proc):
        return (None, None)
    try:
        entries = os.listdir(proc)
    except OSError:
        return (None, None)
    page_kb = 4  # statm is in pages; assume 4KiB pages (Linux default).
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc, entry, "cmdline"), "rb") as handle:
                cmdline = handle.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "mac.api" not in cmdline or not ("uvicorn" in cmdline or "gunicorn" in cmdline):
            continue
        rss_mb = None
        try:
            with open(os.path.join(proc, entry, "statm"), "r", encoding="utf-8") as handle:
                fields = handle.read().split()
            if len(fields) >= 2:
                rss_mb = int(fields[1]) * page_kb / 1024.0
        except (OSError, ValueError):
            rss_mb = None
        # Instantaneous per-process CPU% is expensive to compute portably here;
        # leave it None and rely on the (cheap, host-wide) load ratio as the
        # primary trip signal. Operators may inject a richer sampler.
        return (None, rss_mb)
    return (None, None)


def default_control_plane_sampler() -> ControlPlaneSample:
    """Default metric sampler: host load ratio + control-plane RSS (best effort)."""
    cpu, rss = _read_control_plane_process()
    return ControlPlaneSample(
        load_ratio=_read_load_ratio(),
        cpu_percent=cpu,
        rss_mb=rss,
    )


def resolve_test_jobs(
    cores: int,
    config: HubLoadShedConfig,
) -> int:
    """Cap test parallelism to a fraction of ``cores`` for the hub host.

    Returns an explicit positive integer suitable for ``MAC_TEST_JOBS`` so a
    single gate run cannot oversubscribe the co-located host.
    """
    cfg = config.normalized()
    usable = max(1, int(cores or 1))
    jobs = int(usable * cfg.test_jobs_fraction)
    jobs = max(cfg.min_test_jobs, jobs)
    if cfg.max_test_jobs > 0:
        jobs = min(cfg.max_test_jobs, jobs)
    return max(1, jobs)


def resolve_hub_test_jobs(
    agent_id: str,
    agent_name: str = "",
    *,
    cores: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
    control_plane_probe: Optional[Callable[[], bool]] = None,
) -> Optional[int]:
    """Return the capped ``MAC_TEST_JOBS`` value for the hub host, else ``None``.

    ``None`` means "not the hub host — leave test parallelism untouched".
    """
    if not is_hub_host(
        agent_id,
        agent_name,
        environ=environ,
        control_plane_probe=control_plane_probe,
    ):
        return None
    config = HubLoadShedConfig.from_env(environ)
    resolved_cores = cores if cores is not None else (os.cpu_count() or 1)
    return resolve_test_jobs(resolved_cores, config)


@dataclass
class BreakerSnapshot:
    """Observable breaker state for operators / the API."""

    state: BreakerState
    metric: str  # which signal is driving the current decision
    value: float  # its most recent sampled value
    high: float  # the HIGH-water threshold for that metric
    low: float  # the LOW-water threshold for that metric
    tasks_in_flight: int
    max_concurrent_tasks: int
    trips: int
    recoveries: int

    def to_dict(self) -> dict:
        return {
            "schema": "mac.hub_load_shed.v1",
            "state": self.state.value,
            "metric": self.metric,
            "value": round(self.value, 4),
            "high": round(self.high, 4),
            "low": round(self.low, 4),
            "tasks_in_flight": self.tasks_in_flight,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "trips": self.trips,
            "recoveries": self.recoveries,
        }


class LoadShedBreaker:
    """Hysteresis load-shed breaker for the co-located hub worker.

    States (observable via :meth:`snapshot`):

    * ``CLAIMING`` — every sampled metric is at/below its LOW-water mark: the
      worker may claim new work.
    * ``SHEDDING`` — at least one metric is at/above its HIGH-water mark and no
      task is in flight: the worker refuses to claim.
    * ``DRAINING`` — above HIGH-water WITH a task in flight: the worker keeps the
      current task but claims nothing new (drain-to-idle).

    Hysteresis: once tripped (>= HIGH), the breaker stays tripped until EVERY
    metric falls back to/below its LOW mark, so a signal hovering near threshold
    cannot flap the worker between claiming and shedding.
    """

    def __init__(
        self,
        config: HubLoadShedConfig,
        sampler: Callable[[], ControlPlaneSample],
        *,
        clock: Callable[[], float] = None,
    ) -> None:
        self._config = config.normalized()
        self._sampler = sampler
        self._clock = clock
        self._tripped = False
        self._tasks_in_flight = 0
        self._trips = 0
        self._recoveries = 0
        self._last_metric = "load_ratio"
        self._last_value = 0.0
        self._last_high = self._config.load_high
        self._last_low = self._config.load_low

    # -- task accounting -----------------------------------------------------

    def task_started(self) -> None:
        self._tasks_in_flight += 1

    def task_finished(self) -> None:
        self._tasks_in_flight = max(0, self._tasks_in_flight - 1)

    @property
    def tasks_in_flight(self) -> int:
        return self._tasks_in_flight

    def at_task_capacity(self) -> bool:
        return self._tasks_in_flight >= self._config.max_concurrent_tasks

    # -- hysteresis evaluation ----------------------------------------------

    def _evaluate(self, sample: ControlPlaneSample) -> None:
        cfg = self._config
        # Each metric: (name, value-or-None, high, low). None values never trip.
        metrics = [
            ("load_ratio", sample.load_ratio, cfg.load_high, cfg.load_low),
            ("control_plane_cpu", sample.cpu_percent, cfg.cpu_high, cfg.cpu_low),
        ]
        if cfg.rss_high_mb > 0:
            metrics.append(("control_plane_rss_mb", sample.rss_mb, cfg.rss_high_mb, cfg.rss_low_mb))

        # A trip requires ANY metric >= its HIGH mark; recovery requires EVERY
        # present metric <= its LOW mark (the hysteresis band).
        any_high = False
        all_low = True
        driver = None
        for name, value, high, low in metrics:
            if value is None:
                continue
            if value >= high:
                any_high = True
                if driver is None:
                    driver = (name, value, high, low)
            if value > low:
                all_low = False

        was_tripped = self._tripped
        if not self._tripped and any_high:
            self._tripped = True
            self._trips += 1
        elif self._tripped and all_low:
            self._tripped = False
            self._recoveries += 1

        # Record the most relevant metric for observability: the trip driver if
        # tripped, else the metric closest to its HIGH mark.
        if driver is not None:
            name, value, high, low = driver
        else:
            name, value, high, low = self._closest_to_high(metrics)
        self._last_metric, self._last_value = name, value
        self._last_high, self._last_low = high, low
        _ = was_tripped  # retained for readability of the transition above

    @staticmethod
    def _closest_to_high(metrics):
        best = None
        best_ratio = -1.0
        for name, value, high, low in metrics:
            if value is None or high <= 0:
                continue
            ratio = value / high
            if ratio > best_ratio:
                best_ratio = ratio
                best = (name, value, high, low)
        if best is None:
            return ("load_ratio", 0.0, 1.0, 0.0)
        return best

    # -- public decisions ----------------------------------------------------

    def state(self) -> BreakerState:
        """Sample the metrics and return the current observable state."""
        self._evaluate(self._sampler())
        if not self._tripped:
            return BreakerState.CLAIMING
        if self._tasks_in_flight > 0:
            return BreakerState.DRAINING
        return BreakerState.SHEDDING

    def should_claim(self) -> bool:
        """True only when the worker may claim NEW work right now.

        The worker must be below the load watermark AND under its concurrent-task
        cap. Being tripped (shedding/draining) or at capacity both refuse.
        """
        st = self.state()
        if st is not BreakerState.CLAIMING:
            return False
        return not self.at_task_capacity()

    def should_drain(self) -> bool:
        """True when an in-flight task should be paused/drained (shed under load)."""
        return self.state() is BreakerState.DRAINING

    def snapshot(self) -> BreakerSnapshot:
        """Observable, side-effect-free view (re-samples current metrics)."""
        st = self.state()
        return BreakerSnapshot(
            state=st,
            metric=self._last_metric,
            value=self._last_value,
            high=self._last_high,
            low=self._last_low,
            tasks_in_flight=self._tasks_in_flight,
            max_concurrent_tasks=self._config.max_concurrent_tasks,
            trips=self._trips,
            recoveries=self._recoveries,
        )
