"""OS-agnostic autonomous nap driver.

``run_nap_cycle`` was written for an autonomous nap tick, but the only shipped
driver was a systemd timer (``deploy/systemd/mac-nap-tick.timer``) — useless on
a macOS hub, where launchd rules. The live fleet's naps (and therefore dream
artifacts and dream-repair tasks) silently stopped the day the timer's host
went away. This daemon moves the tick into the hub process itself, mirroring
``BacklogGroomer``: a thread wakes on an interval, asks the ledger which
agents' nap windows have opened, and drives each through one full cycle.

No-op unless ``MAC_NAP_TICK_ENABLED`` is set, so bringing the hub up with this
code is safe everywhere; enabling is one env line, not an OS-specific unit.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, Optional

NAP_TICKER_SCHEMA = "mac.nap_ticker.v1"

MIN_INTERVAL_SECONDS = 60.0
MAX_INTERVAL_SECONDS = 24 * 60 * 60.0
DEFAULT_INTERVAL_SECONDS = 900.0
DEFAULT_INITIAL_DELAY_SECONDS = 120.0
DEFAULT_MAX_AGENTS_PER_TICK = 10

_log = logging.getLogger("mac.nap_ticker")


@dataclass(frozen=True)
class NapTickerConfig:
    enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    max_agents_per_tick: int = DEFAULT_MAX_AGENTS_PER_TICK
    configuration_error: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and not self.configuration_error

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "active": self.active}

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "NapTickerConfig":
        env = os.environ if environ is None else environ
        errors: List[str] = []
        enabled = str(env.get("MAC_NAP_TICK_ENABLED") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }

        def _num(name: str, default: float, low: float, high: float) -> float:
            raw = str(env.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                errors.append("%s must be numeric" % name)
                return default
            if value < low or value > high:
                errors.append("%s must be between %s and %s" % (name, low, high))
                return default
            return value

        interval = _num("MAC_NAP_TICK_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS,
                        MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
        initial_delay = _num("MAC_NAP_TICK_INITIAL_DELAY_SECONDS",
                             DEFAULT_INITIAL_DELAY_SECONDS, 0.0, 60 * 60.0)
        max_agents = int(_num("MAC_NAP_TICK_MAX_AGENTS_PER_TICK",
                              DEFAULT_MAX_AGENTS_PER_TICK, 1, 100))
        return cls(
            enabled=enabled,
            interval_seconds=interval,
            initial_delay_seconds=initial_delay,
            max_agents_per_tick=max_agents,
            configuration_error="; ".join(errors),
        )


class NapTicker:
    """Drive due agents through nap cycles from inside the hub process."""

    def __init__(self, control_plane: Any, config: NapTickerConfig) -> None:
        self.control_plane = control_plane
        self.config = config
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_report: Optional[Dict[str, Any]] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        if not self.config.active:
            if self.config.configuration_error:
                self._observe("nap.tick.configuration_invalid", "warning",
                              {"error": self.config.configuration_error})
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            thread = threading.Thread(target=self._loop, name="mac-nap-ticker", daemon=True)
            self._thread = thread
            thread.start()
        self._observe("nap.tick.started", "info", {"config": self.config.to_dict()})
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._observe("nap.tick.stopped", "info", {})
        return stopped

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            last_report = copy.deepcopy(self._last_report)
        return {
            "schema": NAP_TICKER_SCHEMA,
            "config": self.config.to_dict(),
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "run_active": self._run_lock.locked(),
            "last_report": last_report,
        }

    def _loop(self) -> None:
        if self._stop_event.wait(max(0.0, self.config.initial_delay_seconds)):
            return
        while not self._stop_event.is_set():
            try:
                self.run_once(trigger="scheduled")
            except Exception:  # noqa: BLE001 - the next tick must still run.
                _log.warning("nap tick failed", exc_info=True)
            if self._stop_event.wait(max(0.01, self.config.interval_seconds)):
                return

    # -- core ---------------------------------------------------------------

    def run_once(self, *, actor: str = "nap-ticker", trigger: str = "operator") -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"schema": NAP_TICKER_SCHEMA, "status": "busy", "trigger": trigger, "agents": []}
        results: List[Dict[str, Any]] = []
        run_id = "naptick_%s" % uuid.uuid4().hex
        try:
            due = self._due_agents()
            overflow = max(0, len(due) - self.config.max_agents_per_tick)
            for entry in due[: self.config.max_agents_per_tick]:
                agent_id = str(entry.get("agent_id") or "")
                if not agent_id:
                    continue
                results.append(self._cycle_agent(agent_id, actor=actor))
        finally:
            self._run_lock.release()
        report = {
            "schema": NAP_TICKER_SCHEMA,
            "run_id": run_id,
            "status": "ok",
            "trigger": trigger,
            "due_count": len(due),
            "napped_count": sum(1 for r in results if r.get("napped")),
            "skipped_count": sum(1 for r in results if r.get("skipped")),
            # Deferred due agents are picked up next tick — list_due_nap_agents
            # is catch-up, not strict-window, so nothing is lost, but say so.
            "deferred_count": overflow,
            "agents": results,
        }
        with self._state_lock:
            self._last_report = report
        level = "warning" if any(r.get("error") for r in results) else "info"
        self._observe("nap.tick.run", level, report)
        return report

    def _due_agents(self) -> List[Dict[str, Any]]:
        try:
            return list(self.control_plane.list_due_nap_agents())
        except Exception as exc:  # noqa: BLE001
            _log.warning("nap tick could not list due agents: %s", exc)
            return []

    def _cycle_agent(self, agent_id: str, *, actor: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {"agent_id": agent_id, "napped": False, "skipped": False}
        try:
            cycle = self.control_plane.run_nap_cycle(agent_id, actor=actor)
        except Exception as exc:  # noqa: BLE001 - isolate one agent's failure.
            result["error"] = str(exc)[:500]
            return result
        if cycle.get("skipped"):
            result["skipped"] = True
            result["skip_reason"] = cycle.get("skip_reason")
            return result
        result["napped"] = True
        run = cycle.get("nap_run") or {}
        result["nap_run_id"] = run.get("id") if isinstance(run, dict) else None
        for key in ("consolidation_error", "complete_error"):
            if cycle.get(key):
                result[key] = str(cycle[key])[:500]
        return result

    # -- telemetry ----------------------------------------------------------

    def _observe(self, event_type: str, level: str, detail: Dict[str, Any]) -> None:
        try:
            self.control_plane.record_log(event_type, level=level, detail=detail)
        except Exception:  # noqa: BLE001 - telemetry must never break the tick.
            _log.debug("nap tick observability write failed", exc_info=True)
