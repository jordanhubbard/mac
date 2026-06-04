from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

import mac.k8s.orchestrator as orchestrator

class _StubMac:
    def __init__(self, url: str, token: str = "") -> None:
        self.url = url
        self.token = token

class _StubJobs:
    pass


class _StubDeploys:
    pass


def _empty_yaml_doc() -> Dict[str, Any]:
    return {
        "mac_url": "http://mac-api.mac.svc:80",
        "dispatcher": {
            "machine": {
                "machine_id": "mac-runner",
                "hostname": "mac-runner.svc",
                "labels": {"kind": "k8s-deployment"},
            },
            "agent": {
                "agent_id": "mac-runner",
                "name": "mac-runner",
                "capabilities": ["ops"],
            },
        },
        "role_machines": [],
        "roles": {},
        "capability_role_aliases": {},
    }

def _write_config(tmp_path: Path) -> Path:
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump(_empty_yaml_doc()))
    return f

@pytest.fixture()
def baseline_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env that makes ``RunnerConfig.from_env`` succeed."""
    monkeypatch.setenv("MAC_URL", "http://mac-api.mac.svc:80")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "test-token")
    monkeypatch.setenv("MAC_AGENT_ID", "mac-runner-test")
    monkeypatch.setenv("MAC_RUNNER_NAMESPACE", "mac")
    monkeypatch.setenv("MAC_CONFIG_FILE", str(_write_config(tmp_path)))

@pytest.fixture()
def patched_runtime(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "load_in_cluster_config_calls": 0,
        "drift_calls": [],
        "runner_calls": [],
        "controller_iterations": 0,
        "drift_called": False,
        "runner_called_during_drift": False,
    }
    rec["drift_calls_before_runner"] = False

    import mac.hermes_adapter as hermes_mod
    import mac.k8s.k8s_client as k8s_client_mod
    import mac.k8s.runner as runner_mod
    import mac.k8s.controller as controller_mod

    monkeypatch.setattr(hermes_mod, "MacApiClient", _StubMac, raising=True)
    monkeypatch.setattr(
        k8s_client_mod, "K8sJobsClient", lambda: _StubJobs(), raising=True
    )
    monkeypatch.setattr(
        k8s_client_mod,
        "K8sDeploymentsClient",
        lambda: _StubDeploys(),
        raising=True,
    )

    def _fake_load_in_cluster() -> None:
        rec["load_in_cluster_config_calls"] += 1

    monkeypatch.setattr(
        k8s_client_mod, "load_in_cluster_config", _fake_load_in_cluster, raising=True
    )

    def _fake_check(cfg: Any, mac: Any) -> List[str]:
        rec["drift_calls"].append((cfg, mac))
        rec["drift_called"] = True
        if rec["runner_calls"]:
            rec["runner_called_during_drift"] = True
        return []

    monkeypatch.setattr(
        runner_mod, "check_dispatcher_capabilities", _fake_check, raising=True
    )

    def _fake_runner_loop(mac: Any, jobs: Any, cfg: Any, **kwargs: Any) -> int:
        rec["runner_calls"].append((mac, jobs, cfg))
        # Drift probe MUST have fired before this point.
        rec["drift_calls_before_runner"] = bool(rec["drift_called"])
        return 0

    monkeypatch.setattr(runner_mod, "runner_loop", _fake_runner_loop, raising=True)

    def _fake_reconcile_stuck(mac: Any, jobs: Any, cfg: Any) -> List[Dict[str, Any]]:
        rec["controller_iterations"] += 1
        return []

    monkeypatch.setattr(
        controller_mod, "reconcile_stuck_jobs", _fake_reconcile_stuck, raising=True
    )

    return rec

def test_main_runs_drift_probe_then_runner(
    baseline_env: None, patched_runtime: Dict[str, Any]
) -> None:
    rc = orchestrator.main([])
    assert rc == 0
    assert len(patched_runtime["drift_calls"]) == 1
    assert patched_runtime["drift_calls_before_runner"] is True
    assert len(patched_runtime["runner_calls"]) == 1
    # load_in_cluster_config wired exactly once.
    assert patched_runtime["load_in_cluster_config_calls"] == 1

def test_main_returns_two_when_mac_url_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    patched_runtime: Dict[str, Any],
) -> None:
    import mac.k8s.runner as runner_mod

    def _empty_url_cfg() -> Any:
        cfg = runner_mod.RunnerConfig(
            mac_url="",
            agent_id="mac-runner-test",
            namespace="mac",
        )
        return cfg

    monkeypatch.setattr(
        runner_mod.RunnerConfig, "from_env", staticmethod(_empty_url_cfg)
    )
    monkeypatch.setenv("MAC_WORKER_TOKEN", "tok")
    assert orchestrator.main([]) == 2

def test_main_returns_two_when_token_missing(
    baseline_env: None,
    monkeypatch: pytest.MonkeyPatch,
    patched_runtime: Dict[str, Any],
) -> None:
    monkeypatch.delenv("MAC_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)
    assert orchestrator.main([]) == 2

def test_controller_thread_is_daemon(
    baseline_env: None,
    patched_runtime: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture the controller thread at start time and assert daemon=True."""
    captured: Dict[str, threading.Thread] = {}
    real_thread_init = threading.Thread.__init__

    def _spy_init(self: threading.Thread, *args: Any, **kwargs: Any) -> None:
        real_thread_init(self, *args, **kwargs)
        if kwargs.get("name") == "mac-orchestrator-controller":
            captured["t"] = self

    monkeypatch.setattr(threading.Thread, "__init__", _spy_init)

    rc = orchestrator.main([])
    assert rc == 0
    t = captured.get("t")
    assert t is not None, "controller thread must be created with the expected name"
    assert t.daemon is True, "controller thread must be daemon so it doesn't block exit"

def test_runner_loop_exception_returns_non_zero(
    baseline_env: None,
    patched_runtime: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mac.k8s.runner as runner_mod

    def _boom(*_a: Any, **_kw: Any) -> int:
        raise RuntimeError("runner blew up")

    monkeypatch.setattr(runner_mod, "runner_loop", _boom, raising=True)
    rc = orchestrator.main([])
    assert rc == 1

def test_runner_loop_keyboard_interrupt_returns_zero(
    baseline_env: None,
    patched_runtime: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mac.k8s.runner as runner_mod

    def _interrupt(*_a: Any, **_kw: Any) -> int:
        raise KeyboardInterrupt()

    monkeypatch.setattr(runner_mod, "runner_loop", _interrupt, raising=True)
    rc = orchestrator.main([])
    assert rc == 0

def test_controller_daemon_failure_does_not_kill_runner(
    baseline_env: None,
    patched_runtime: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import mac.k8s.controller as controller_mod

    crash_event = threading.Event()
    crash_count = {"n": 0}

    def _crashy_reconcile(mac: Any, jobs: Any, cfg: Any) -> List[Dict[str, Any]]:
        crash_count["n"] += 1
        crash_event.set()
        raise RuntimeError("kube-apiserver gone walkabout")

    monkeypatch.setattr(
        controller_mod, "reconcile_stuck_jobs", _crashy_reconcile, raising=True
    )

    orchestrator.controller_loop_failures = 0

    import mac.k8s.runner as runner_mod

    sleep_calls = {"n": 0}
    real_sleep = time.sleep

    def _slow_runner(*_a: Any, **_kw: Any) -> int:
        # Wait until the controller has crashed at least once.
        crash_event.wait(timeout=2.0)
        # Use the REAL sleep, not the bounded one: the bounded-sleep counter
        # must be driven solely by the controller daemon loop. If the runner
        # consumed it, its time.sleep could be the call that trips the bound,
        # raising inside runner_loop and making main() return 1 under CI
        # thread-scheduling pressure (flaky).
        real_sleep(0.05)
        return 0

    monkeypatch.setattr(runner_mod, "runner_loop", _slow_runner, raising=True)

    def _bounded_sleep(seconds: float) -> None:
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            # Kill the inner loop via outer-except path.
            raise RuntimeError("test bound: stop daemon")
        real_sleep(0)

    monkeypatch.setattr(orchestrator.time, "sleep", _bounded_sleep)

    # Short interval so the daemon loops fast.
    monkeypatch.setenv("MAC_CONTROLLER_INTERVAL_SECONDS", "0.01")
    # Isolate the controller daemon: the review-tick daemon also calls
    # time.sleep and would race the bounded-sleep counter.
    monkeypatch.setenv("MAC_REVIEW_TICK_LOOP_ENABLED", "0")

    with caplog.at_level(logging.ERROR, logger="mac-k8s-orchestrator.controller"):
        rc = orchestrator.main([])

    real_sleep(0.05)

    assert rc == 0, "runner completed normally; orchestrator should exit 0"
    assert (
        orchestrator.controller_loop_failures >= 1
    ), "controller failure counter should have incremented"
    # An ERROR log was emitted with a stack trace.
    assert any(
        "controller reconcile iteration failed" in rec.getMessage()
        for rec in caplog.records
    ), "expected an ERROR log naming the failed reconcile iteration"

def test_drift_probe_fires_before_dispatch_loop(
    baseline_env: None, patched_runtime: Dict[str, Any]
) -> None:
    orchestrator.main([])
    assert patched_runtime["drift_called"] is True
    assert patched_runtime["drift_calls_before_runner"] is True
    assert patched_runtime["runner_called_during_drift"] is False

def test_run_controller_loop_forever_continues_after_iteration_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import mac.k8s.controller as controller_mod

    call_count: Dict[str, int] = {"n": 0}

    def _reconcile(mac: Any, jobs: Any, cfg: Any) -> List[Dict[str, Any]]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first call crashes")
        return []

    monkeypatch.setattr(
        controller_mod, "reconcile_stuck_jobs", _reconcile, raising=True
    )

    stop_event = threading.Event()
    sleep_calls = {"n": 0}

    def _fake_sleep(seconds: float) -> None:
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            stop_event.set()
            raise RuntimeError("stopping test loop")
        # Otherwise yield instantly.

    monkeypatch.setattr(orchestrator.time, "sleep", _fake_sleep)

    orchestrator.controller_loop_failures = 0
    with caplog.at_level(logging.ERROR):
        orchestrator._run_controller_loop_forever(
            mac=object(),
            jobs=object(),
            cfg=object(),
            interval=0.001,
            log=logging.getLogger("mac-k8s-orchestrator.controller"),
        )

    assert call_count["n"] >= 2
    assert orchestrator.controller_loop_failures >= 1


class _TickMac:
    """Mac client stub that records POSTs to /reviews/default/tick."""

    def __init__(self) -> None:
        self.tick_calls: List[str] = []

    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if "/reviews/default/tick" in path:
            self.tick_calls.append(path)
        return {"processed": 0, "results": []}


def test_review_tick_loop_posts_default_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator must periodically OPEN reviews for needs_review
    tasks by POSTing /reviews/default/tick — not merely claim existing
    nudges. Without this, a task reaching needs_review never gets a review
    opened and stalls forever."""
    mac = _TickMac()
    sleep_calls = {"n": 0}

    def _fake_sleep(seconds: float) -> None:
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise RuntimeError("stop test loop")

    monkeypatch.setattr(orchestrator.time, "sleep", _fake_sleep)
    orchestrator.review_tick_loop_failures = 0
    orchestrator._run_review_tick_loop_forever(
        mac=mac,
        interval=0.001,
        limit=25,
        actor="mac-runner",
        log=logging.getLogger("mac-k8s-orchestrator.review-tick"),
    )
    assert mac.tick_calls, "review-tick loop must POST /reviews/default/tick"
    assert any("limit=25" in c for c in mac.tick_calls)
    assert any("actor=mac-runner" in c for c in mac.tick_calls)


def test_review_tick_loop_survives_iteration_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _BoomMac:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("hub unreachable")
            return {"processed": 0, "results": []}

    mac = _BoomMac()
    sleep_calls = {"n": 0}

    def _fake_sleep(seconds: float) -> None:
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise RuntimeError("stop test loop")

    monkeypatch.setattr(orchestrator.time, "sleep", _fake_sleep)
    orchestrator.review_tick_loop_failures = 0
    with caplog.at_level(logging.WARNING):
        orchestrator._run_review_tick_loop_forever(
            mac=mac,
            interval=0.001,
            limit=10,
            actor="mac-runner",
            log=logging.getLogger("mac-k8s-orchestrator.review-tick"),
        )
    # First iteration failed but loop continued to a second POST.
    assert mac.calls >= 2
    assert orchestrator.review_tick_loop_failures >= 1


def test_main_starts_review_tick_daemon_thread(
    baseline_env: None,
    patched_runtime: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() must start the review-tick loop as a daemon thread so
    needs_review tasks auto-advance (open reviews + emit reviewer nudges)."""
    captured: Dict[str, threading.Thread] = {}
    real_thread_init = threading.Thread.__init__

    def _spy_init(self: threading.Thread, *args: Any, **kwargs: Any) -> None:
        real_thread_init(self, *args, **kwargs)
        if kwargs.get("name") == "mac-orchestrator-review-tick":
            captured["t"] = self

    monkeypatch.setattr(threading.Thread, "__init__", _spy_init)
    rc = orchestrator.main([])
    assert rc == 0
    t = captured.get("t")
    assert t is not None, "review-tick thread must be created"
    assert t.daemon is True, "review-tick thread must be a daemon"


