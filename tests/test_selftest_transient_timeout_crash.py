"""Regression for the transient-timeout startup self-test crash.

Crash fingerprint (parent P0: repair MAC crash mac-agent-service at 62021be0):
a worker whose mandatory shared-service probe (Qdrant/Firecrawl) — or the hub
heartbeat — hit a *transient* ``TimeoutError`` previously recorded a blocking
problem and made ``mac-agent-startup-self-test`` exit non-zero, which stops
``mac-agent-service`` even though the timeout was a passing hub blip.  After the
repair, a shared-service probe that only ever times out (after bounded retries)
degrades the node (non-blocking) so the self-test exits 0 and the service keeps
starting, while a genuine misconfiguration (a refused connection / bad endpoint)
still blocks startup.

This extracts the embedded ``mac-agent-startup-self-test`` Python body from
``install_mac_agent_wrapper`` in ``deploy/fleet-node-install.sh`` and execs it in
a temporary HOME with the mandatory shared services configured and ``urllib``
stubbed to raise the relevant error, following the extract-and-run pattern in
tests/test_gatewayless_worker_selftest_crash.py and tests/test_fleet_node_install.py.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _startup_self_test_source() -> str:
    """Extract the embedded mac-agent-startup-self-test Python (the inner PY heredoc)."""
    script = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    match = re.search(
        r"exec \"\$selftest_python\" - <<'PY'\n(?P<source>.*?)\nPY\n",
        script,
        re.DOTALL,
    )
    assert match, "self-test PY heredoc not found in fleet-node-install.sh"
    return match.group("source")


def _run_self_test_with_probe_error(tmp_path, monkeypatch, probe_error):
    """Exec the startup self-test with the mandatory Qdrant/Firecrawl probes failing.

    ``probe_error`` is raised from every ``urllib.request.urlopen`` call (the
    shared-service probes and the hub heartbeat), letting a caller reproduce a
    transient timeout or a genuine connection error.  Returns ``(exit_code, report)``.
    """
    import urllib.request

    home = tmp_path / "home"
    mac_home = home / ".mac"
    (mac_home / "openclaw" / "managed").mkdir(parents=True, exist_ok=True)
    (mac_home / "bin").mkdir(parents=True, exist_ok=True)
    (mac_home / "logs").mkdir(parents=True, exist_ok=True)
    report_path = mac_home / "logs" / "mac-agent-startup-self-test.json"

    # A fully-configured pure worker: identity present, gateway impl is not
    # openclaw (so OpenClaw checks pass and never interfere), and both mandatory
    # shared services are required and have URLs — the only failure comes from the
    # probe error injected below.
    env = {
        "MAC_CHAT_GATEWAY_IMPL": "none",
        "MAC_WORKER_AGENT_NAME": "worker1",
        "MAC_AGENT_ID": "agent_worker1",
        "MAC_HERMES_INSTANCE_ID": "hermes-1",
        "MAC_HERMES_PERSONA_ID": "persona-1",
        "MAC_FLEET_TENANT_ID": "tenant-1",
        "MAC_REQUIRE_QDRANT_MEMORY": "1",
        "QDRANT_URL": "http://qdrant.local:6333",
        "MAC_REQUIRE_FIRECRAWL": "1",
        "FIRECRAWL_API_URL": "http://firecrawl.local:3002",
        # Exercise the hub heartbeat path too; it must never change the exit code.
        "MAC_HUB_URL": "http://hub.local:8789",
        "MAC_WORKER_TOKEN": "token-1",
        "MAC_AGENT_STARTUP_SELF_TEST_REPORT": str(report_path),
    }

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # Avoid the retry backoff sleeps slowing the test down.
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)

    def _raise(*args, **kwargs):
        raise probe_error

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    # A pure worker (impl != openclaw) never invokes openclaw-agent, but guard it.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("openclaw-agent must not run for a pure worker")
        ),
    )

    namespace = {"__name__": "__mac_selftest__", "os": os}
    saved_environ = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    exit_code = 0
    try:
        exec(compile(_startup_self_test_source(), "<selftest>", "exec"), namespace)
    except SystemExit as exc:
        exit_code = exc.code or 0
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    return exit_code, report


def test_transient_shared_service_timeout_does_not_block_startup(tmp_path, monkeypatch):
    # A socket read/connect timeout surfaces as TimeoutError (an OSError subclass).
    exit_code, report = _run_self_test_with_probe_error(
        tmp_path, monkeypatch, TimeoutError("timed out")
    )

    # The self-test must NOT exit non-zero and must NOT record blocking problems
    # for a transient timeout, so mac-agent-service keeps starting.
    assert exit_code == 0, report["blocking_problems"]
    assert report["status"] == "degraded"
    assert report["blocking_problems"] == []

    # The timed-out shared-service probes are recorded as transient/non-blocking.
    assert report["transient_problems"], report
    assert set(report["transient_problems"]).issubset(set(report["non_blocking_problems"]))
    assert any("Qdrant" in p for p in report["transient_problems"])
    assert any("Firecrawl" in p for p in report["transient_problems"])


def test_transient_timeout_wrapped_in_urlerror_does_not_block_startup(tmp_path, monkeypatch):
    # A timeout can also surface wrapped inside urllib.error.URLError.reason.
    exit_code, report = _run_self_test_with_probe_error(
        tmp_path, monkeypatch, urllib.error.URLError(TimeoutError("timed out"))
    )

    assert exit_code == 0, report["blocking_problems"]
    assert report["status"] == "degraded"
    assert report["blocking_problems"] == []
    assert report["transient_problems"], report


def test_genuine_misconfiguration_still_blocks_startup(tmp_path, monkeypatch):
    # Control: a refused connection is a deterministic misconfiguration, not a
    # transient blip, so it must remain blocking and fail the self-test.
    exit_code, report = _run_self_test_with_probe_error(
        tmp_path, monkeypatch, ConnectionRefusedError("[Errno 111] Connection refused")
    )

    assert exit_code == 1, report
    assert report["status"] == "failed"
    assert report["blocking_problems"], report
    assert report["transient_problems"] == []
    assert any("Qdrant" in p for p in report["blocking_problems"])
