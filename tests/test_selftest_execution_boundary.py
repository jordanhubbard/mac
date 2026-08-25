"""A worker that cannot execute anything must fail its startup self-test.

The startup self-test's ``openshell_executor_config`` check only ever
validated the sandbox configuration when the sandbox was *enabled*. A worker
with ``MAC_OPENSHELL_SANDBOX=0`` therefore passed it vacuously -- no branch
appended a problem, so the check reported ``true``.

That is not hypothetical. A sandbox A/B experiment left jordanh-worker6 and
jordanh-worker7 with ``MAC_OPENSHELL_SANDBOX=0`` and
``MAC_OPENSHELL_REQUIRED=0`` but without ``MAC_ALLOW_UNSANDBOXED_YOLO``, so
the executor refused to launch anything while the self-test reported the
hosts healthy, the hub accepted their registration, and every task dispatched
to them died at first execution.

The fix asks the question the check never asked: with no sandbox, is
unsandboxed execution actually permitted? It mirrors the executor's own
refusal in ``executor_sandbox._unsandboxed_agent_argv`` so the two cannot
drift, and it is a property of the worker rather than of how the worker was
provisioned -- an SSH-installed host, a container, and a future AWS or Azure
node all answer it the same way.

Extract-and-run pattern follows tests/test_selftest_transient_timeout_crash.py.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _startup_self_test_source() -> str:
    script = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    match = re.search(
        r"exec \"\$selftest_python\" - <<'PY'\n(?P<source>.*?)\nPY\n",
        script,
        re.DOTALL,
    )
    assert match, "self-test PY heredoc not found in fleet-node-install.sh"
    return match.group("source")


def _run_self_test(tmp_path, monkeypatch, sandbox_env, *, openshell_on_path=False):
    """Exec the startup self-test for a pure worker with ``sandbox_env`` applied.

    Everything unrelated to the execution boundary is configured to pass: no
    mandatory shared services, no hub heartbeat, and a non-OpenClaw gateway
    impl, so the only thing that can fail is the boundary check under test.
    """
    home = tmp_path / "home"
    mac_home = home / ".mac"
    (mac_home / "openclaw" / "managed").mkdir(parents=True, exist_ok=True)
    (mac_home / "bin").mkdir(parents=True, exist_ok=True)
    (mac_home / "logs").mkdir(parents=True, exist_ok=True)
    report_path = mac_home / "logs" / "mac-agent-startup-self-test.json"

    env = {
        "MAC_CHAT_GATEWAY_IMPL": "none",
        "MAC_WORKER_AGENT_NAME": "worker6",
        "MAC_AGENT_ID": "agent_worker6",
        "MAC_HERMES_INSTANCE_ID": "instance-1",
        "MAC_HERMES_PERSONA_ID": "persona-1",
        "MAC_FLEET_TENANT_ID": "tenant-1",
        # Mandatory shared services must be required AND reachable; the probes
        # are stubbed to succeed below so the only thing that can fail is the
        # execution boundary under test.
        "MAC_REQUIRE_QDRANT_MEMORY": "1",
        "QDRANT_URL": "http://qdrant.local:6333",
        "MAC_REQUIRE_FIRECRAWL": "1",
        "FIRECRAWL_API_URL": "http://firecrawl.local:3002",
        "MAC_AGENT_STARTUP_SELF_TEST_REPORT": str(report_path),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    env.update(sandbox_env)

    if openshell_on_path:
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        fake = bindir / "openshell"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        env["PATH"] = "%s:%s" % (bindir, env["PATH"])

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    import time as _time
    import urllib.request

    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)

    class _OkResponse:
        status = 200

        def read(self, *args):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _OkResponse())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a pure worker must not invoke openclaw-agent")
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

    return exit_code, json.loads(report_path.read_text(encoding="utf-8"))


def _boundary_problems(report):
    return [
        problem for problem in report["problems"] if problem.startswith("worker cannot execute")
    ]


def test_the_worker6_configuration_fails_the_self_test(tmp_path, monkeypatch):
    """The exact configuration that ran for days while reporting healthy."""
    exit_code, report = _run_self_test(
        tmp_path,
        monkeypatch,
        {
            "MAC_OPENSHELL_SANDBOX": "0",
            "MAC_OPENSHELL_REQUIRED": "0",
            "MAC_ALLOW_UNSANDBOXED_YOLO": "0",
        },
    )

    assert report["checks"]["openshell_executor_config"] is False, report["checks"]
    assert _boundary_problems(report), report["problems"]
    # Blocking, not degraded: a worker that cannot run a task must not register.
    assert _boundary_problems(report)[0] in report["blocking_problems"]
    assert report["status"] == "failed"
    assert exit_code == 1


def test_requiring_openshell_without_the_sandbox_fails(tmp_path, monkeypatch):
    """Unset YOLO defaults to 0 exactly when OpenShell is required."""
    exit_code, report = _run_self_test(
        tmp_path,
        monkeypatch,
        {"MAC_OPENSHELL_SANDBOX": "0", "MAC_OPENSHELL_REQUIRED": "1"},
    )

    assert report["checks"]["openshell_executor_config"] is False
    assert exit_code == 1
    assert any("MAC_OPENSHELL_REQUIRED=1" in p for p in _boundary_problems(report))


def test_explicitly_allowing_unsandboxed_execution_passes(tmp_path, monkeypatch):
    """An operator who accepts the risk is not blocked -- the worker can run."""
    exit_code, report = _run_self_test(
        tmp_path,
        monkeypatch,
        {
            "MAC_OPENSHELL_SANDBOX": "0",
            "MAC_OPENSHELL_REQUIRED": "0",
            "MAC_ALLOW_UNSANDBOXED_YOLO": "1",
        },
    )

    assert report["checks"]["openshell_executor_config"] is True
    assert _boundary_problems(report) == []
    assert exit_code == 0


def test_an_enabled_sandbox_still_passes(tmp_path, monkeypatch):
    """The normal fleet configuration: sandboxed execution, boundary present."""
    exit_code, report = _run_self_test(
        tmp_path,
        monkeypatch,
        {"MAC_OPENSHELL_SANDBOX": "1", "MAC_OPENSHELL_REQUIRED": "1"},
        openshell_on_path=True,
    )

    assert report["checks"]["openshell_executor_config"] is True
    assert _boundary_problems(report) == []
    assert exit_code == 0


def test_an_undeterminable_boundary_is_not_guessed(tmp_path, monkeypatch):
    """With neither variable set, the executor consults an identity allowlist
    the self-test cannot see. Guessing would block workers that can in fact
    run, so the check stays silent rather than inventing a problem."""
    exit_code, report = _run_self_test(tmp_path, monkeypatch, {"MAC_OPENSHELL_SANDBOX": "0"})

    assert report["checks"]["openshell_executor_config"] is True
    assert _boundary_problems(report) == []
    assert exit_code == 0


def test_the_check_matches_the_executors_own_refusal(tmp_path, monkeypatch):
    """Guard against drift between the self-test and the code it predicts.

    The self-test reimplements the executor's gate (it runs standalone, with
    no `mac` package importable), so the two can silently diverge. This pins
    them together on every combination the self-test claims to decide.
    """
    from mac.openshell_runtime import truthy

    for required in ("0", "1"):
        for yolo in (None, "0", "1"):
            sandbox_env = {"MAC_OPENSHELL_SANDBOX": "0", "MAC_OPENSHELL_REQUIRED": required}
            if yolo is not None:
                sandbox_env["MAC_ALLOW_UNSANDBOXED_YOLO"] = yolo

            # What executor_sandbox._unsandboxed_agent_argv would decide.
            default_unsandboxed = "0" if truthy(required) else "1"
            executor_allows = truthy(yolo or default_unsandboxed)

            _, report = _run_self_test(tmp_path / f"{required}-{yolo}", monkeypatch, sandbox_env)
            self_test_allows = report["checks"]["openshell_executor_config"]

            assert self_test_allows is executor_allows, (
                f"MAC_OPENSHELL_REQUIRED={required} MAC_ALLOW_UNSANDBOXED_YOLO={yolo}: "
                f"self-test says {self_test_allows}, executor says {executor_allows}"
            )
