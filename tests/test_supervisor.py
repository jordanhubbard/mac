"""The external node supervisor must catch BOTH failure modes launchd misses —
a crashed process (probe connection refused) and a hung one (probe timeout) —
restart it, and refuse to thrash when a restart never sticks.

The watchdog is pure of I/O (probe/restart/clock injected), so these tests pin
the state machine deterministically without real HTTP, launchctl, or sleeps.
"""
from __future__ import annotations

import threading
import urllib.error
import urllib.request

import pytest

from mac.supervisor import (
    ProcessWatchdog,
    Supervisor,
    SupervisorConfig,
    build_ops_server,
    http_health_probe,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _wd(config, probe_results, clock=None):
    """Watchdog whose probe pops from a list (or repeats the last value) and
    whose restart is counted."""
    clock = clock or Clock()
    calls = {"restarts": 0}
    seq = list(probe_results)

    def probe():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def restart():
        calls["restarts"] += 1
        return True

    wd = ProcessWatchdog(config, probe=probe, restart=restart, now=clock)
    return wd, calls, clock


def test_healthy_never_restarts():
    cfg = SupervisorConfig(failure_threshold=3)
    wd, calls, _ = _wd(cfg, [True])
    for _ in range(10):
        assert wd.tick()["action"] == "healthy"
    assert calls["restarts"] == 0


def test_crash_restarts_after_threshold():
    # A crash = probe returns False (connection refused). Restart only after the
    # threshold of consecutive failures, not on the first blip.
    cfg = SupervisorConfig(failure_threshold=3, restart_grace_seconds=0)
    wd, calls, _ = _wd(cfg, [False])
    assert wd.tick()["action"] == "observing"
    assert wd.tick()["action"] == "observing"
    assert wd.tick()["action"] == "restarted"
    assert calls["restarts"] == 1


def test_hang_is_treated_as_failure():
    # A hang surfaces as a probe that raises (timeout). It must count as a
    # failure, not crash the watchdog.
    cfg = SupervisorConfig(failure_threshold=2, restart_grace_seconds=0)
    calls = {"restarts": 0}

    def probe():
        raise TimeoutError("read timed out")

    wd = ProcessWatchdog(
        cfg, probe=probe, restart=lambda: calls.__setitem__("restarts", calls["restarts"] + 1) or True,
        now=Clock(),
    )
    assert wd.tick()["action"] == "observing"
    assert wd.tick()["action"] == "restarted"
    assert calls["restarts"] == 1


def test_transient_failure_resets_before_threshold():
    cfg = SupervisorConfig(failure_threshold=3, restart_grace_seconds=0)
    wd, calls, _ = _wd(cfg, [False, False, True, False])
    assert wd.tick()["action"] == "observing"   # fail 1
    assert wd.tick()["action"] == "observing"   # fail 2
    assert wd.tick()["action"] == "healthy"     # recovered -> reset
    assert wd.tick()["action"] == "observing"   # fail 1 again, not 3
    assert calls["restarts"] == 0


def test_grace_period_suppresses_restart():
    # After a restart, failing probes during the grace window must not trigger
    # another restart (the process is still binding its port).
    cfg = SupervisorConfig(failure_threshold=1, restart_grace_seconds=45.0)
    clock = Clock()
    wd, calls, _ = _wd(cfg, [False], clock=clock)
    assert wd.tick()["action"] == "restarted"   # first failure -> restart, grace armed
    assert calls["restarts"] == 1
    clock.advance(10)
    assert wd.tick()["action"] == "grace"       # still in grace -> no restart
    assert calls["restarts"] == 1
    clock.advance(40)                            # grace expired (10+40 > 45)
    assert wd.tick()["action"] == "restarted"
    assert calls["restarts"] == 2


def test_flap_ceiling_escalates_and_stops_restarting():
    # A restart that never sticks must not thrash forever: after
    # max_restarts_per_window the supervisor holds off and escalates.
    cfg = SupervisorConfig(
        failure_threshold=1, restart_grace_seconds=0,
        max_restarts_per_window=3, flap_window_seconds=10_000,
    )
    events = []
    clock = Clock()

    def observe(event, level, detail):
        events.append((event, level))

    wd = ProcessWatchdog(cfg, probe=lambda: False, restart=lambda: True, now=clock, observe=observe)
    actions = [wd.tick()["action"] for _ in range(6)]
    assert actions[:3] == ["restarted", "restarted", "restarted"]
    assert actions[3:] == ["flap_ceiling", "flap_ceiling", "flap_ceiling"]
    assert wd.state.total_restarts == 3
    assert any(e == "supervisor.flap_ceiling" and lvl == "critical" for e, lvl in events)


def test_flap_window_rolls_off_and_allows_new_restart():
    cfg = SupervisorConfig(
        failure_threshold=1, restart_grace_seconds=0,
        max_restarts_per_window=2, flap_window_seconds=100,
    )
    clock = Clock()
    wd, calls, _ = _wd(cfg, [False], clock=clock)
    assert wd.tick()["action"] == "restarted"
    assert wd.tick()["action"] == "restarted"
    assert wd.tick()["action"] == "flap_ceiling"   # ceiling hit
    clock.advance(101)                              # both restarts roll out of window
    assert wd.tick()["action"] == "restarted"
    assert calls["restarts"] == 3


def test_config_from_env_requires_token():
    cfg = SupervisorConfig.from_env({"MAC_SUPERVISOR_LABEL": "com.x.cp"})
    assert cfg.label == "com.x.cp"
    assert "no MAC_SUPERVISOR_TOKEN" in cfg.configuration_error


def test_config_from_env_overrides():
    cfg = SupervisorConfig.from_env({
        "MAC_API_TOKEN": "tok",
        "MAC_SUPERVISOR_FAILURE_THRESHOLD": "5",
        "MAC_SUPERVISOR_OPS_PORT": "9999",
        "MAC_SUPERVISOR_RESTART_COMMAND": "echo hi there",
    })
    assert cfg.auth_token == "tok"
    assert cfg.failure_threshold == 5
    assert cfg.ops_port == 9999
    assert cfg.resolved_restart_command() == ("echo", "hi", "there")
    assert not cfg.configuration_error


def test_http_health_probe_true_and_false():
    # A local one-shot HTTP server answers 200 once, then we probe a dead port.
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()
    assert http_health_probe("http://127.0.0.1:%d/health" % port, 5.0) is True
    srv.server_close()
    # Nothing is listening now -> refused -> False (the "crash" signal).
    assert http_health_probe("http://127.0.0.1:%d/health" % port, 2.0) is False


def test_ops_channel_health_unauth_and_restart_requires_token():
    cfg = SupervisorConfig(auth_token="secret", ops_port=0)
    calls = {"restarts": 0}
    wd = ProcessWatchdog(
        cfg, probe=lambda: True,
        restart=lambda: calls.__setitem__("restarts", calls["restarts"] + 1) or True,
    )
    srv = build_ops_server(SupervisorConfig(auth_token="secret", ops_host="127.0.0.1", ops_port=0), wd)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    t.start()
    try:
        base = "http://127.0.0.1:%d" % port
        # /health is unauthenticated and reports supervisor liveness.
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            assert r.status == 200
        # POST /restart without a token is rejected.
        req = urllib.request.Request(base + "/restart", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 401
        assert calls["restarts"] == 0
        # With the token it triggers a restart.
        req = urllib.request.Request(
            base + "/restart", method="POST",
            headers={"Authorization": "Bearer secret"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
        assert calls["restarts"] == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_supervisor_disabled_returns_immediately():
    cfg = SupervisorConfig(enabled=False, auth_token="t")
    assert Supervisor(cfg).run() == 0
