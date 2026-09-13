"""Worker health ignores absent retired OpenClaw gateway artifacts.

A stale deployment setting must neither block startup nor degrade the worker.
Shared-service and executor checks retain their own readiness contracts.
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


def _run_gatewayless_worker_self_test(tmp_path, monkeypatch):
    """Exec the startup self-test as a gateway-less OpenClaw worker.

    Reproduces the crash environment: MAC_CHAT_GATEWAY_IMPL=openclaw, no
    ``service-advertisement.json`` on disk, and MAC_OPENCLAW_AGENT_BIN pointing
    at a nonexistent path.  Shared services (Qdrant/Firecrawl) and heartbeat HTTP
    are stubbed so the only deficiencies are the missing OpenClaw gateway
    artifacts.  Returns ``(exit_code, report)``.
    """
    import urllib.request

    home = tmp_path / "home"
    mac_home = home / ".mac"
    (mac_home / "openclaw" / "managed").mkdir(parents=True, exist_ok=True)
    (mac_home / "bin").mkdir(parents=True, exist_ok=True)
    (mac_home / "logs").mkdir(parents=True, exist_ok=True)
    report_path = mac_home / "logs" / "mac-agent-startup-self-test.json"

    # The crash-inducing environment: OpenClaw advertised fleet-wide, but this
    # node never installed the gateway. No advertisement file exists, and the
    # configured agent binary path does not exist on disk.
    missing_agent_bin = mac_home / "bin" / "does-not-exist-openclaw-agent"
    assert not missing_agent_bin.exists()

    env = {
        "MAC_CHAT_GATEWAY_IMPL": "openclaw",
        "MAC_OPENCLAW_AGENT_BIN": str(missing_agent_bin),
        "MAC_WORKER_AGENT_NAME": "worker1",
        "MAC_AGENT_ID": "agent_worker1",
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

    # Stub reachable shared services and heartbeat so they are not the reason for
    # any failure; the gateway-less worker path must not invoke openclaw-agent.
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("openclaw-agent must not run for a gateway-less worker")
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


def test_gatewayless_worker_missing_openclaw_artifacts_does_not_block_startup(
    tmp_path, monkeypatch
):
    exit_code, report = _run_gatewayless_worker_self_test(tmp_path, monkeypatch)

    # The self-test must NOT exit non-zero and must NOT mark blocking problems
    # that would stop mac-agent-service.
    assert exit_code == 0, report["blocking_problems"]
    assert report["status"] != "failed"
    assert report["blocking_problems"] == []

    assert report["status"] == "passed"
    assert report["problems"] == []
    assert "openclaw_gateway" not in report
    assert not any("openclaw" in key for key in report["checks"])
