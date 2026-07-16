"""Regression for the gateway-SERVING OpenClaw agent-probe crash.

Crash fingerprint sha256:7e47597d...: on a node that DOES serve the OpenClaw
gateway (``MAC_CHAT_GATEWAY_IMPL=openclaw`` with the gateway artifacts installed
on disk -- a verified ``service-advertisement.json`` and a real
``openclaw-agent`` binary), an ``openclaw-agent`` startup probe that exits
non-zero for a *runtime* reason (e.g. ``OpenClaw agent self-test exited 1``)
previously stayed *blocking*.  That made ``mac-agent-startup-self-test`` exit
non-zero, which stops ``mac-agent-service``.

The worker/gateway decoupling contract says a runtime/service-reachability
failure of the ``openclaw-agent`` probe is a soft, DEGRADED condition even on a
gateway-serving node: hard OpenClaw *misconfiguration* problems (unreadable /
unverified advertisement, bad model config, etc.) stay blocking, but the agent
probe's runtime exit must be non-blocking so the self-test exits 0 and the
service continues degraded.

This extracts the embedded ``mac-agent-startup-self-test`` Python body from
``install_mac_agent_wrapper`` in ``deploy/fleet-node-install.sh`` and runs it in
a temporary HOME with a *gateway-serving* environment: valid advertisement +
model config + a real agent binary on disk (so ``openclaw_serves_gateway`` is
True), with ``urllib``/``subprocess`` stubbed so the ``openclaw-agent`` probe
exits 1 without the ``MAC_OPENCLAW_STARTUP_OK`` sentinel while Qdrant, Firecrawl
and the heartbeat are reachable.  It follows the extract-and-run pattern used by
tests/test_gatewayless_worker_selftest_crash.py.

The assertions fail against the pre-fix code (where a gateway-serving node kept
the ``OpenClaw agent self-test exited 1`` problem blocking and the self-test
exited 1) and pass after the sibling implementation change.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
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


def _run_gateway_serving_self_test(tmp_path, monkeypatch):
    """Exec the startup self-test as a gateway-SERVING OpenClaw node.

    Constructs the crash environment: MAC_CHAT_GATEWAY_IMPL=openclaw with the
    gateway artifacts installed on disk (a verified ``service-advertisement.json``
    proving exclusive OpenShell-confined ownership, a readable managed model
    config, and a real ``openclaw-agent`` binary), so ``openclaw_serves_gateway``
    is True.  The ``openclaw-agent`` probe is stubbed to exit 1 without the
    ``MAC_OPENCLAW_STARTUP_OK`` sentinel, simulating a runtime failure.  Qdrant,
    Firecrawl and the heartbeat HTTP are stubbed reachable so they are not the
    failure source.  Returns ``(exit_code, report)``.
    """
    import urllib.request

    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_dir = mac_home / "openclaw"
    managed_dir = openclaw_dir / "managed"
    managed_dir.mkdir(parents=True, exist_ok=True)
    (mac_home / "bin").mkdir(parents=True, exist_ok=True)
    (mac_home / "logs").mkdir(parents=True, exist_ok=True)
    report_path = mac_home / "logs" / "mac-agent-startup-self-test.json"

    # A valid, verified service advertisement proving this node exclusively owns
    # and serves the OpenClaw gateway inside OpenShell.
    advertisement_path = openclaw_dir / "service-advertisement.json"
    advertisement_path.write_text(
        json.dumps(
            {
                "openclaw_runtime": {
                    "implementation": "openclaw",
                    "verified": True,
                    "exclusive_service_owner": True,
                    "confinement": {"provider": "openshell"},
                },
                "gateway_ownership": {"exclusive": True},
            }
        ),
        encoding="utf-8",
    )

    # A readable managed OpenClaw model config so model configuration is not a
    # source of problems.
    (managed_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "models": {"providers": {"mac-router": {"api": "openai"}}},
                "agents": {"defaults": {"model": {"primary": "mac-router/gpt-x"}}},
            }
        ),
        encoding="utf-8",
    )

    # A REAL agent binary on disk so openclaw_gateway_installed / serves_gateway
    # are True (the crash path is specific to a gateway-serving node).
    agent_bin = mac_home / "bin" / "openclaw-agent"
    agent_bin.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    agent_bin.chmod(0o755)
    assert agent_bin.is_file()

    env = {
        "MAC_CHAT_GATEWAY_IMPL": "openclaw",
        "MAC_OPENCLAW_AGENT_BIN": str(agent_bin),
        "MAC_WORKER_AGENT_NAME": "gwnode1",
        "MAC_AGENT_ID": "agent_gwnode1",
        "MAC_HERMES_INSTANCE_ID": "hermes-1",
        "MAC_HERMES_PERSONA_ID": "persona-1",
        "MAC_FLEET_TENANT_ID": "tenant-1",
        "MAC_REQUIRE_QDRANT_MEMORY": "1",
        "QDRANT_URL": "http://qdrant.local:6333",
        "MAC_REQUIRE_FIRECRAWL": "1",
        "FIRECRAWL_API_URL": "http://firecrawl.local:3002",
        "MAC_AGENT_STARTUP_SELF_TEST_REPORT": str(report_path),
    }

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args, **kwargs):
            return b"{}"

    # Qdrant / Firecrawl / heartbeat are all reachable -- they must not be the
    # reason for any failure.
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())

    class _Completed:
        returncode = 1
        stdout = "OpenClaw agent self-test exited 1"
        stderr = ""

    # The openclaw-agent probe exits non-zero WITHOUT the sentinel, simulating a
    # runtime failure of the agent process.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Completed())

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


def test_gateway_serving_openclaw_agent_probe_failure_is_degraded(tmp_path, monkeypatch):
    exit_code, report = _run_gateway_serving_self_test(tmp_path, monkeypatch)

    # The self-test must NOT exit non-zero: an openclaw-agent runtime failure on a
    # gateway-serving node degrades the node rather than crashing mac-agent-service.
    assert exit_code == 0, report["blocking_problems"]
    assert report["status"] == "degraded"
    assert report["blocking_problems"] == []

    # This node actually serves the gateway (artifacts installed on disk).
    assert report["openclaw_gateway"]["impl_advertised"] is True
    assert report["openclaw_gateway"]["installed"] is True
    assert report["openclaw_gateway"]["serves_gateway"] is True

    # The runtime probe failure is recorded but soft (non-blocking).
    probe_problem = "OpenClaw agent self-test exited 1"
    assert probe_problem in report["problems"]
    assert probe_problem in report["non_blocking_problems"]
    assert probe_problem not in report["blocking_problems"]
