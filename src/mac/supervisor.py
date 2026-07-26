"""Always-on node supervisor: an external watchdog + a lightweight ops channel.

Why this exists
---------------
Two supervision mechanisms already ran on every node and BOTH miss the failure
that took the hub down:

* launchd/systemd ``KeepAlive`` restarts a process that *dies*, but is blind to
  a process that is alive yet *wedged* — the control-plane pinned on a 16GB DB,
  answering nothing. A live-but-hung daemon is never restarted.
* ``SelfHealingSentinel`` runs INSIDE the control-plane process, so when the
  control-plane hangs or dies the sentinel dies with it — it cannot restart its
  own host.

The supervisor runs as its own tiny process (launchd ``com.mac.supervisor`` /
systemd), OUTSIDE the control-plane, and does two things:

1. **Watchdog** — actively probes the supervised process each interval. A single
   HTTP health probe catches BOTH failure modes: a crash surfaces as a refused
   connection, a hang as a timeout. After ``failure_threshold`` consecutive bad
   probes it restarts the process (``kickstart -k`` kills a hung one and starts
   a dead one alike), with post-restart grace, and a flap ceiling so a
   crash-loop escalates to a human instead of thrashing forever.

2. **Ops channel** — a token-authed HTTP surface (``/health``, ``/restart``,
   ``/maintenance``) so operators drive the node over the same lightweight HTTP
   the fleet already speaks, instead of heavyweight, relay-fragile SSH. It stays
   reachable even while the control-plane it supervises is down.

Dependency-light on purpose (stdlib only): the thing that restarts everything
else must itself be trivially robust. The turtle terminates at launchd, which
only has to handle the supervisor *crashing* — a simple loop it can keep alive.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

_log = logging.getLogger("mac.supervisor")

SUPERVISOR_SCHEMA = "mac.supervisor.v1"

DEFAULT_LABEL = "com.mac.control-plane"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8789/health"
DEFAULT_OPS_PORT = 8790


def _now() -> float:
    return time.monotonic()


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _float_env(env: Dict[str, str], name: str, default: float, *, low: float, high: float) -> float:
    raw = str(env.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return max(low, min(high, value))


def _int_env(env: Dict[str, str], name: str, default: int, *, low: int, high: int) -> int:
    return int(_float_env(env, name, float(default), low=float(low), high=float(high)))


def _default_restart_command(label: str) -> Tuple[str, ...]:
    """Platform-appropriate 'kill if hung, then (re)start' for ``label``."""
    system = platform.system()
    if system == "Darwin":
        uid = os.getuid()
        return ("launchctl", "kickstart", "-k", "gui/%d/%s" % (uid, label))
    # systemd --user on Linux workers; falls back cleanly if unit is a service.
    return ("systemctl", "--user", "restart", label)


@dataclass(frozen=True)
class SupervisorConfig:
    label: str = DEFAULT_LABEL
    health_url: str = DEFAULT_HEALTH_URL
    ops_host: str = "127.0.0.1"
    ops_port: int = DEFAULT_OPS_PORT
    probe_interval_seconds: float = 15.0
    probe_timeout_seconds: float = 8.0
    # Consecutive failed probes required before a restart. >1 so a single
    # transient blip (GC pause, a slow request) never triggers a restart.
    failure_threshold: int = 3
    # After a restart, ignore probe results for this long to let the process
    # bind its port and warm up before we judge it again.
    restart_grace_seconds: float = 45.0
    # Flap protection: at most ``max_restarts_per_window`` restarts within
    # ``flap_window_seconds``; beyond that the supervisor stops restarting and
    # escalates (a restart that never sticks is a human problem).
    flap_window_seconds: float = 900.0
    max_restarts_per_window: int = 5
    restart_command: Tuple[str, ...] = ()
    auth_token: str = ""
    # Escalation: a generic HTTP endpoint (e.g. a Slack incoming webhook or an
    # ops alert ingest) that a restart/flap event is POSTed to. Deliberately
    # INDEPENDENT of the hub — the supervisor's whole job is to survive the hub
    # being down, so it must not depend on the hub to report that the hub is down.
    alert_webhook: str = ""
    enabled: bool = True
    configuration_error: str = ""

    def resolved_restart_command(self) -> Tuple[str, ...]:
        return self.restart_command or _default_restart_command(self.label)

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "SupervisorConfig":
        env = dict(os.environ if environ is None else environ)
        errors: List[str] = []
        raw_cmd = str(env.get("MAC_SUPERVISOR_RESTART_COMMAND") or "").strip()
        restart_command: Tuple[str, ...] = tuple(raw_cmd.split()) if raw_cmd else ()
        token = str(
            env.get("MAC_SUPERVISOR_TOKEN") or env.get("MAC_API_TOKEN") or ""
        ).strip()
        if not token:
            errors.append("no MAC_SUPERVISOR_TOKEN/MAC_API_TOKEN set; ops channel disabled")
        threshold = _int_env(env, "MAC_SUPERVISOR_FAILURE_THRESHOLD", 3, low=1, high=20)
        return cls(
            label=str(env.get("MAC_SUPERVISOR_LABEL") or DEFAULT_LABEL).strip(),
            health_url=str(env.get("MAC_SUPERVISOR_HEALTH_URL") or DEFAULT_HEALTH_URL).strip(),
            ops_host=str(env.get("MAC_SUPERVISOR_OPS_HOST") or "127.0.0.1").strip(),
            ops_port=_int_env(env, "MAC_SUPERVISOR_OPS_PORT", DEFAULT_OPS_PORT, low=1, high=65535),
            probe_interval_seconds=_float_env(
                env, "MAC_SUPERVISOR_PROBE_INTERVAL_SECONDS", 15.0, low=2.0, high=600.0),
            probe_timeout_seconds=_float_env(
                env, "MAC_SUPERVISOR_PROBE_TIMEOUT_SECONDS", 8.0, low=1.0, high=120.0),
            failure_threshold=threshold,
            restart_grace_seconds=_float_env(
                env, "MAC_SUPERVISOR_RESTART_GRACE_SECONDS", 45.0, low=0.0, high=600.0),
            flap_window_seconds=_float_env(
                env, "MAC_SUPERVISOR_FLAP_WINDOW_SECONDS", 900.0, low=60.0, high=24 * 3600.0),
            max_restarts_per_window=_int_env(
                env, "MAC_SUPERVISOR_MAX_RESTARTS_PER_WINDOW", 5, low=1, high=100),
            restart_command=restart_command,
            auth_token=token,
            alert_webhook=str(env.get("MAC_SUPERVISOR_ALERT_WEBHOOK") or "").strip(),
            enabled=not _truthy(env.get("MAC_SUPERVISOR_DISABLED")),
            configuration_error="; ".join(errors),
        )


def http_health_probe(url: str, timeout: float) -> bool:
    """Return True iff ``url`` answers 2xx within ``timeout``.

    A refused connection (crash) and a read timeout (hang) both raise and are
    reported as unhealthy — the single probe that catches both failure modes.
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return False


def post_alert(webhook: str, payload: Dict[str, object], timeout: float = 8.0) -> bool:
    """Best-effort POST of a JSON alert to an operator webhook (Slack incoming
    webhook / ops ingest). Never raises — a failed alert must never disturb the
    watchdog loop. Returns True on a 2xx."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return False


def run_restart_command(command: Sequence[str], timeout: float = 30.0) -> bool:
    """Run a restart command and return whether it succeeded."""
    try:
        proc = subprocess.run(
            list(command), timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


@dataclass
class WatchdogState:
    consecutive_failures: int = 0
    total_probes: int = 0
    total_failures: int = 0
    total_restarts: int = 0
    last_probe_healthy: Optional[bool] = None
    last_probe_at: Optional[float] = None
    last_restart_at: Optional[float] = None
    grace_until: float = 0.0
    escalated: bool = False
    restart_times: Deque[float] = field(default_factory=deque)

    def snapshot(self, *, mono: float) -> Dict[str, object]:
        def _ago(mono_ts: Optional[float]) -> Optional[float]:
            return None if mono_ts is None else round(mono - mono_ts, 2)
        return {
            "consecutive_failures": self.consecutive_failures,
            "total_probes": self.total_probes,
            "total_failures": self.total_failures,
            "total_restarts": self.total_restarts,
            "last_probe_healthy": self.last_probe_healthy,
            "seconds_since_last_probe": _ago(self.last_probe_at),
            "seconds_since_last_restart": _ago(self.last_restart_at),
            "in_grace": mono < self.grace_until,
            "escalated": self.escalated,
            "restarts_in_window": len(self.restart_times),
        }


class ProcessWatchdog:
    """Probe-and-restart state machine.

    Pure of I/O for testability: ``probe`` (health check), ``restart`` (restart
    the process), ``now`` (monotonic clock), and ``observe`` (telemetry) are all
    injected. ``tick()`` performs exactly one probe and at most one restart.
    """

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        probe: Callable[[], bool],
        restart: Callable[[], bool],
        now: Callable[[], float] = _now,
        observe: Optional[Callable[[str, str, Dict[str, object]], None]] = None,
    ) -> None:
        self.config = config
        self._probe = probe
        self._restart = restart
        self._now = now
        self._observe = observe or (lambda *a, **k: None)
        self.state = WatchdogState()

    def tick(self) -> Dict[str, object]:
        now = self._now()
        self.state.total_probes += 1
        try:
            healthy = bool(self._probe())
        except Exception:  # noqa: BLE001 - a probe that raises is a failure.
            healthy = False
        self.state.last_probe_healthy = healthy
        self.state.last_probe_at = now

        if healthy:
            if self.state.consecutive_failures:
                self._observe("supervisor.recovered", "info", {
                    "after_failures": self.state.consecutive_failures,
                })
            self.state.consecutive_failures = 0
            self.state.escalated = False
            return {"action": "healthy"}

        # Unhealthy probe.
        self.state.total_failures += 1
        # During post-restart grace we count the failure for visibility but do
        # not act — the process may still be binding its port.
        if now < self.state.grace_until:
            return {"action": "grace"}
        self.state.consecutive_failures += 1
        if self.state.consecutive_failures < self.config.failure_threshold:
            return {"action": "observing",
                    "consecutive_failures": self.state.consecutive_failures}
        return self._attempt_restart(now)

    def _attempt_restart(self, now: float) -> Dict[str, object]:
        # Flap ceiling: prune the window, escalate if we've already restarted
        # too many times without it sticking.
        window_start = now - self.config.flap_window_seconds
        while self.state.restart_times and self.state.restart_times[0] < window_start:
            self.state.restart_times.popleft()
        if len(self.state.restart_times) >= self.config.max_restarts_per_window:
            if not self.state.escalated:
                self.state.escalated = True
                self._observe("supervisor.flap_ceiling", "critical", {
                    "label": self.config.label,
                    "restarts_in_window": len(self.state.restart_times),
                    "window_seconds": self.config.flap_window_seconds,
                    "detail": (
                        "%s restarted %d times in %.0fs without recovering; "
                        "supervisor is holding off — human intervention needed."
                        % (self.config.label, len(self.state.restart_times),
                           self.config.flap_window_seconds)
                    ),
                })
            return {"action": "flap_ceiling",
                    "restarts_in_window": len(self.state.restart_times)}

        self._observe("supervisor.restart", "warning", {
            "label": self.config.label,
            "consecutive_failures": self.state.consecutive_failures,
            "command": list(self.config.resolved_restart_command()),
        })
        ok = False
        try:
            ok = bool(self._restart())
        except Exception as exc:  # noqa: BLE001 - restart failure must not kill the loop.
            self._observe("supervisor.restart_failed", "error", {
                "label": self.config.label, "error": str(exc)[:300]})
        self.state.total_restarts += 1
        self.state.last_restart_at = now
        self.state.restart_times.append(now)
        self.state.consecutive_failures = 0
        self.state.grace_until = now + self.config.restart_grace_seconds
        if not ok:
            self._observe("supervisor.restart_failed", "error", {
                "label": self.config.label,
                "command": list(self.config.resolved_restart_command()),
            })
        return {"action": "restarted", "ok": ok}


class _OpsHandler(BaseHTTPRequestHandler):
    # Injected by the server factory.
    watchdog: ProcessWatchdog = None  # type: ignore[assignment]
    token: str = ""

    def log_message(self, *args: object) -> None:  # silence default stderr spam
        return

    def _authed(self) -> bool:
        if not self.token:
            return False
        header = self.headers.get("Authorization", "")
        expected = "Bearer %s" % self.token
        # constant-time-ish compare
        return len(header) == len(expected) and header == expected

    def _send(self, code: int, payload: Dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.rstrip("/") in ("/health", ""):
            # /health is intentionally UNAUTHENTICATED and cheap: it is the
            # supervisor's own liveness (it never touches the wedged DB), so a
            # peer/monitor can always tell the node is reachable even when the
            # control-plane is down.
            self._send(200, {
                "schema": SUPERVISOR_SCHEMA,
                "supervisor": "ok",
                "supervised": self.watchdog.config.label,
                "watchdog": self.watchdog.state.snapshot(
                    mono=_now()),
            })
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return
        route = self.path.rstrip("/")
        if route == "/restart":
            self.watchdog._observe("supervisor.restart.ops_requested", "warning",
                                   {"label": self.watchdog.config.label})
            ok = False
            try:
                ok = bool(self.watchdog._restart())
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": str(exc)[:300]})
                return
            self._send(200, {"restarted": ok, "label": self.watchdog.config.label})
            return
        if route == "/maintenance":
            # Bounded maintenance hook. A concrete command may be wired via
            # MAC_SUPERVISOR_MAINTENANCE_COMMAND; absent that, report unsupported
            # rather than pretend. Kept deliberately minimal so the ops surface
            # cannot be turned into an arbitrary-exec backdoor by default.
            cmd = str(os.environ.get("MAC_SUPERVISOR_MAINTENANCE_COMMAND") or "").strip()
            if not cmd:
                self._send(501, {"error": "no MAC_SUPERVISOR_MAINTENANCE_COMMAND configured"})
                return
            ok = run_restart_command(cmd.split(), timeout=600.0)
            self._send(200 if ok else 500, {"ran": cmd, "ok": ok})
            return
        self._send(404, {"error": "not found"})


def build_ops_server(config: SupervisorConfig, watchdog: ProcessWatchdog) -> ThreadingHTTPServer:
    """Build the supervisor ops HTTP server bound to the watchdog."""
    handler = type("_BoundOpsHandler", (_OpsHandler,), {
        "watchdog": watchdog,
        "token": config.auth_token,
    })
    return ThreadingHTTPServer((config.ops_host, config.ops_port), handler)


class Supervisor:
    """Wires a watchdog loop + ops server into a runnable node supervisor."""

    def __init__(self, config: Optional[SupervisorConfig] = None) -> None:
        self.config = config or SupervisorConfig.from_env()
        self._stop = threading.Event()
        self.watchdog = ProcessWatchdog(
            self.config,
            probe=lambda: http_health_probe(
                self.config.health_url, self.config.probe_timeout_seconds),
            restart=lambda: run_restart_command(self.config.resolved_restart_command()),
            observe=self._observe,
        )
        self._server: Optional[ThreadingHTTPServer] = None

    def _observe(self, event: str, level: str, detail: Dict[str, object]) -> None:
        line = json.dumps({"event": event, "level": level, **detail})
        (_log.warning if level in {"warning", "error", "critical"} else _log.info)(line)
        # Escalate crash/restart/flap events to the operator alert webhook so a
        # human is actually told — not just the log. Best-effort; never blocks.
        if self.config.alert_webhook and level in {"warning", "error", "critical"}:
            post_alert(self.config.alert_webhook, {
                "source": "mac.supervisor",
                "label": self.config.label,
                "event": event,
                "level": level,
                **detail,
            })

    def _serve_ops(self) -> None:
        if not self.config.auth_token:
            _log.warning("supervisor ops channel not started: no auth token")
            return
        try:
            self._server = build_ops_server(self.config, self.watchdog)
        except OSError as exc:
            _log.error("supervisor ops channel failed to bind %s:%d: %s",
                       self.config.ops_host, self.config.ops_port, exc)
            return
        _log.info("supervisor ops channel on %s:%d", self.config.ops_host, self.config.ops_port)
        self._server.serve_forever(poll_interval=1.0)

    def run(self) -> int:
        if not self.config.enabled:
            _log.info("supervisor disabled (MAC_SUPERVISOR_DISABLED)")
            return 0
        ops_thread = threading.Thread(target=self._serve_ops, name="mac-supervisor-ops", daemon=True)
        ops_thread.start()
        _log.info("supervisor watching %s via %s (interval=%.0fs threshold=%d)",
                  self.config.label, self.config.health_url,
                  self.config.probe_interval_seconds, self.config.failure_threshold)
        while not self._stop.is_set():
            try:
                self.watchdog.tick()
            except Exception:  # noqa: BLE001 - the loop must survive any tick failure.
                _log.warning("supervisor tick failed", exc_info=True)
            self._stop.wait(self.config.probe_interval_seconds)
        if self._server is not None:
            self._server.shutdown()
        return 0

    def stop(self) -> None:
        self._stop.set()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the supervisor entry point and return its exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s mac.supervisor %(levelname)s %(message)s",
    )
    config = SupervisorConfig.from_env()
    if config.configuration_error:
        _log.warning("supervisor configuration warning: %s", config.configuration_error)
    return Supervisor(config).run()


if __name__ == "__main__":
    sys.exit(main())
