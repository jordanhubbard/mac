"""Regression for the report-executor attestation-gap startup self-test crash.

Crash fingerprint (parent P0: repair MAC crash mac-agent-service at b7a6558a529f):
an OpenShell loop worker whose report-repository-executor attestation could not
be obtained -- ``mac.worker._read_only_report_executor_attestation`` returns
``None`` because the hardened OpenShell posture is not provable at self-test time
-- previously recorded a *blocking* problem and made
``mac-agent-startup-self-test`` exit non-zero, which stops ``mac-agent-service``.
The attestation is healed at runtime by
``mac.worker._resources_with_live_report_executor_attestation``, so a loop
worker's attestation gap must degrade the node (non-blocking) and let the
self-test exit 0 while the service keeps running.  A genuine blocking
misconfiguration (invalid ``MAC_OPENSHELL_CREATE_ARGS`` or a non-executable
``MAC_OPENSHELL_BIN``) must still fail closed with exit 1 / status ``failed``.

This extracts the embedded ``mac-agent-startup-self-test`` Python body from
``install_mac_agent_wrapper`` in ``deploy/fleet-node-install.sh`` and execs it in
a temporary HOME configured as an OpenShell loop worker
(``MAC_OPENSHELL_SANDBOX`` truthy, ``MAC_WORKER_MODE=loop``) with the mandatory
probes stubbed to pass, following the extract-and-run pattern used by
tests/test_gatewayless_worker_selftest_crash.py and
tests/test_selftest_transient_timeout_crash.py.
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


def _run_openshell_loop_worker_self_test(
    tmp_path,
    monkeypatch,
    *,
    attestation,
    extra_env=None,
    openshell_bin=None,
):
    """Exec the startup self-test as an OpenShell loop worker.

    The mandatory shared-service probes (Qdrant/Firecrawl) and the hub heartbeat
    are stubbed to succeed and ``mac.worker._read_only_report_executor_attestation``
    is patched to return ``attestation`` (``None`` reproduces the crash: the
    hardened OpenShell attestation is not obtainable at self-test time).
    ``extra_env`` overrides the base environment so a caller can inject a genuine
    blocking misconfiguration; ``openshell_bin`` overrides ``MAC_OPENSHELL_BIN``.
    Returns ``(exit_code, report)``.
    """
    import urllib.request
    import mac.worker

    home = tmp_path / "home"
    mac_home = home / ".mac"
    (mac_home / "openclaw" / "managed").mkdir(parents=True, exist_ok=True)
    (mac_home / "bin").mkdir(parents=True, exist_ok=True)
    (mac_home / "logs").mkdir(parents=True, exist_ok=True)
    report_path = mac_home / "logs" / "mac-agent-startup-self-test.json"

    # A real executable so the default OpenShell-config probe passes; only the
    # attestation gap (or an explicit override below) is the deficiency.
    default_bin = mac_home / "bin" / "openshell"
    default_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    default_bin.chmod(0o755)

    # A fully-configured OpenShell loop worker whose only deficiency is the
    # report-executor attestation gap: identity present, gateway impl is not
    # openclaw (so OpenClaw checks pass and never interfere), both mandatory
    # shared services required with URLs, and a valid OpenShell executor config.
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
        # The OpenShell loop-worker posture that gates the attestation probe.
        "MAC_OPENSHELL_SANDBOX": "1",
        "MAC_WORKER_MODE": "loop",
        "MAC_OPENSHELL_BIN": str(openshell_bin if openshell_bin is not None else default_bin),
        "MAC_AGENT_STARTUP_SELF_TEST_REPORT": str(report_path),
    }
    if extra_env:
        env.update(extra_env)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # The heredoc imports mac.worker._read_only_report_executor_attestation at
    # runtime; drive its return value directly so the test controls whether the
    # hardened attestation is obtainable, without depending on host posture.
    monkeypatch.setattr(
        mac.worker,
        "_read_only_report_executor_attestation",
        lambda *a, **k: attestation,
    )

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args, **kwargs):
            return b"{}"

    # Stub the mandatory shared-service probes and the hub heartbeat so they are
    # never the reason for any failure.
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
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


def test_report_executor_attestation_gap_does_not_block_startup(tmp_path, monkeypatch):
    # BEFORE the fix this exited 1 / status "failed" with a blocking problem.
    exit_code, report = _run_openshell_loop_worker_self_test(
        tmp_path, monkeypatch, attestation=None
    )

    # AFTER the fix: the self-test exits 0, degrades (non-blocking), and keeps
    # mac-agent-service running.
    assert exit_code == 0, report["blocking_problems"]
    assert report["status"] == "degraded"
    assert report["blocking_problems"] == []

    # The attestation-gap problem is present and classified non-blocking only.
    attestation_problems = [p for p in report["problems"] if "report repository executor" in p]
    assert attestation_problems, report["problems"]
    assert set(attestation_problems).issubset(set(report["non_blocking_problems"]))
    assert not set(attestation_problems) & set(report["blocking_problems"])
    # The OpenShell executor config itself is valid; only the attestation gap is soft.
    assert report["checks"]["openshell_executor_config"] is True
    assert report["checks"]["report_repository_executor_attestation"] is False


def test_obtainable_attestation_passes_cleanly(tmp_path, monkeypatch):
    # Control: when the hardened attestation IS obtainable the loop worker passes
    # with no attestation problem at all.
    exit_code, report = _run_openshell_loop_worker_self_test(
        tmp_path,
        monkeypatch,
        attestation={"schema": "mac.report_repository_executor_attestation.v1"},
    )

    assert exit_code == 0, report["blocking_problems"]
    assert report["status"] == "passed"
    assert report["problems"] == []
    assert report["checks"]["report_repository_executor_attestation"] is True
    assert report["report_repository_executor_attestation"] == {
        "schema": "mac.report_repository_executor_attestation.v1"
    }


def test_invalid_openshell_create_args_still_blocks_startup(tmp_path, monkeypatch):
    # Fail-closed control: an invalid MAC_OPENSHELL_CREATE_ARGS is a genuine
    # misconfiguration that must remain blocking even though the (patched)
    # attestation would otherwise be obtainable.
    exit_code, report = _run_openshell_loop_worker_self_test(
        tmp_path,
        monkeypatch,
        attestation={"schema": "mac.report_repository_executor_attestation.v1"},
        extra_env={"MAC_OPENSHELL_CREATE_ARGS": "'unterminated"},
    )

    assert exit_code == 1, report
    assert report["status"] == "failed"
    assert report["blocking_problems"], report
    assert report["checks"]["openshell_executor_config"] is False
    assert any(
        p.startswith("MAC_OPENSHELL_CREATE_ARGS is invalid") for p in report["blocking_problems"]
    )


def test_non_executable_openshell_bin_still_blocks_startup(tmp_path, monkeypatch):
    # Fail-closed control: a non-executable MAC_OPENSHELL_BIN is a genuine
    # misconfiguration that must remain blocking.
    missing_bin = tmp_path / "home" / ".mac" / "bin" / "does-not-exist-openshell"
    exit_code, report = _run_openshell_loop_worker_self_test(
        tmp_path,
        monkeypatch,
        attestation={"schema": "mac.report_repository_executor_attestation.v1"},
        openshell_bin=missing_bin,
    )

    assert exit_code == 1, report
    assert report["status"] == "failed"
    assert report["blocking_problems"], report
    assert report["checks"]["openshell_executor_config"] is False
    assert any(
        p.startswith("OpenShell sandbox is enabled but MAC_OPENSHELL_BIN is not executable")
        for p in report["blocking_problems"]
    )
