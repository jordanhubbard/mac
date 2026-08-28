"""Regression: a gateway-serving node must degrade (not crash) on a soft
openclaw-agent runtime probe failure.

Crash fingerprint sha256:7e47597d...: on a node that DOES serve the OpenClaw
gateway (verified ``service-advertisement.json`` AND the ``openclaw-agent``
binary both installed => ``openclaw_serves_gateway=True``), an ``openclaw-agent``
self-test invocation that returns a NON-ZERO exit for a soft/runtime reason
previously kept the ``OpenClaw agent self-test exited N`` problem in
``blocking_problems`` (because the gateway-serving branch made ``non_blocking``
empty), so the self-test ``sys.exit(1)`` killed ``mac-agent-service`` under
``set -euo pipefail``.

The root-cause fix classifies the openclaw-agent runtime probe outcome problems
as DEGRADED (soft, non-blocking) even on a gateway-serving node, while HARD
misconfiguration problems (unreadable/missing/unverified advertisement, model
config, mandatory-service misconfig, etc.) stay blocking.  This test extracts
the embedded ``mac-agent-startup-self-test`` Python from
``deploy/fleet-node-install.sh`` and runs it as a fully-provisioned gateway
node whose openclaw-agent exits 1 for a runtime reason, asserting exit 0,
status ``degraded``, empty ``blocking_problems``, and the agent-probe problem
present in ``non_blocking_problems``.
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


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_gateway_serving_self_test(tmp_path, monkeypatch, *, agent_rc, agent_stdout, agent_stderr):
    import urllib.request

    home = tmp_path / "home"
    mac_home = home / ".mac"
    (mac_home / "openclaw" / "managed").mkdir(parents=True, exist_ok=True)
    (mac_home / "bin").mkdir(parents=True, exist_ok=True)
    (mac_home / "logs").mkdir(parents=True, exist_ok=True)
    report_path = mac_home / "logs" / "mac-agent-startup-self-test.json"

    # A fully-provisioned gateway-serving node: the openclaw-agent binary exists
    # AND a verified service-advertisement.json proving exclusive gateway
    # ownership is on disk => openclaw_serves_gateway is True.
    agent_bin = mac_home / "bin" / "openclaw-agent"
    agent_bin.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    agent_bin.chmod(0o755)

    advertisement = mac_home / "openclaw" / "service-advertisement.json"
    advertisement.write_text(
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

    # A readable, valid managed OpenClaw model configuration so the only problem
    # is the runtime agent-probe failure.
    openclaw_config = mac_home / "openclaw" / "managed" / "openclaw.json"
    openclaw_config.write_text(
        json.dumps(
            {
                "models": {"providers": {"mac-router": {"api": "openai"}}},
                "agents": {"defaults": {"model": {"primary": "mac-router/gpt"}}},
            }
        ),
        encoding="utf-8",
    )

    env = {
        "MAC_CHAT_GATEWAY_IMPL": "openclaw",
        "MAC_OPENCLAW_AGENT_BIN": str(agent_bin),
        "MAC_WORKER_RESOURCES_FILE": str(advertisement),
        "MAC_WORKER_AGENT_NAME": "rocky",
        "MAC_AGENT_ID": "agent_rocky",
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

    # Stub reachable shared services + heartbeat so they are not a failure cause.
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())

    outcomes = (
        list(zip(agent_rc, agent_stdout, agent_stderr)) if isinstance(agent_rc, list) else None
    )

    def _fake_run(*args, **kwargs):
        if outcomes is not None:
            rc, stdout, stderr = outcomes.pop(0)
            return _FakeCompleted(rc, stdout, stderr)
        return _FakeCompleted(agent_rc, agent_stdout, agent_stderr)

    monkeypatch.setattr(subprocess, "run", _fake_run)

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


def test_gateway_serving_worker_agent_exit_nonzero_is_degraded(tmp_path, monkeypatch):
    exit_code, report = _run_gateway_serving_self_test(
        tmp_path,
        monkeypatch,
        agent_rc=1,
        agent_stdout="",
        agent_stderr="runtime probe failed",
    )

    # The node actually serves the gateway.
    assert report["openclaw_gateway"]["impl_advertised"] is True
    assert report["openclaw_gateway"]["installed"] is True
    assert report["openclaw_gateway"]["serves_gateway"] is True

    # A soft runtime probe failure must NOT block startup.
    assert exit_code == 0, report["blocking_problems"]
    assert report["status"] == "degraded"
    assert report["blocking_problems"] == []

    # The agent-probe problem is present and classified non-blocking.
    probe = "OpenClaw agent self-test exited 1"
    assert probe in report["problems"]
    assert probe in report["non_blocking_problems"]


def test_gateway_serving_worker_agent_no_sentinel_is_degraded(tmp_path, monkeypatch):
    # A zero exit that never produced the sentinel is also a soft runtime probe
    # failure (the agent ran but did not confirm the execution contract).
    exit_code, report = _run_gateway_serving_self_test(
        tmp_path,
        monkeypatch,
        agent_rc=0,
        agent_stdout="some other output",
        agent_stderr="",
    )

    assert report["openclaw_gateway"]["serves_gateway"] is True
    assert exit_code == 0, report["blocking_problems"]
    assert report["status"] == "degraded"
    assert report["blocking_problems"] == []
    probe = "OpenClaw agent self-test did not return its sentinel"
    assert probe in report["non_blocking_problems"]


def test_gateway_serving_worker_retries_transient_router_503(tmp_path, monkeypatch):
    exit_code, report = _run_gateway_serving_self_test(
        tmp_path,
        monkeypatch,
        agent_rc=[1, 0],
        agent_stdout=["", "MAC_OPENCLAW_STARTUP_OK"],
        agent_stderr=[
            "FailoverError: 503 no provider could serve model=azure/anthropic/claude-sonnet-4-6",
            "",
        ],
    )

    assert exit_code == 0
    assert report["status"] == "passed"
    assert report["checks"]["openclaw_agent"] is True
    assert report["openclaw_failure_class"] == ""
    assert report["problems"] == []


def test_gateway_serving_worker_hard_misconfig_still_blocks(tmp_path, monkeypatch):
    # Guardrail: a HARD misconfiguration (unverified advertisement) on a node
    # that installs the gateway artifacts must still block startup, proving the
    # soft classification is scoped to the runtime agent probe only.
    import urllib.request

    home = tmp_path / "home"
    mac_home = home / ".mac"
    (mac_home / "openclaw" / "managed").mkdir(parents=True, exist_ok=True)
    (mac_home / "bin").mkdir(parents=True, exist_ok=True)
    (mac_home / "logs").mkdir(parents=True, exist_ok=True)
    report_path = mac_home / "logs" / "mac-agent-startup-self-test.json"

    agent_bin = mac_home / "bin" / "openclaw-agent"
    agent_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agent_bin.chmod(0o755)

    advertisement = mac_home / "openclaw" / "service-advertisement.json"
    # verified is False => hard misconfiguration problem.
    advertisement.write_text(
        json.dumps(
            {
                "openclaw_runtime": {
                    "implementation": "openclaw",
                    "verified": False,
                    "exclusive_service_owner": True,
                    "confinement": {"provider": "openshell"},
                },
                "gateway_ownership": {"exclusive": True},
            }
        ),
        encoding="utf-8",
    )

    env = {
        "MAC_CHAT_GATEWAY_IMPL": "openclaw",
        "MAC_OPENCLAW_AGENT_BIN": str(agent_bin),
        "MAC_WORKER_RESOURCES_FILE": str(advertisement),
        "MAC_WORKER_AGENT_NAME": "rocky",
        "MAC_AGENT_ID": "agent_rocky",
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

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(0, "MAC_OPENCLAW_STARTUP_OK", ""),
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

    assert report["openclaw_gateway"]["serves_gateway"] is True
    assert exit_code == 1
    assert report["status"] == "failed"
    assert "OpenClaw runtime advertisement is not verified" in report["blocking_problems"]
