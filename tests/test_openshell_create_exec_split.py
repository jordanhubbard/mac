"""OpenShell 0.1 create/exec split (worker canary, 2026-10-03).

OpenShell 0.1 rejects ``sandbox create --upload X -- <command>`` and makes a
trailing create command the sandbox's main process, whose exit takes the
sandbox out of Ready so every later ``exec`` fails. MAC therefore creates
(uploading) with no command, kept alive, and runs the work through ``sandbox
exec``. These tests pin the argv shapes for both CLI generations, the
version probe, and the error and cleanup paths of every split call site.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac import openshell_runtime
from mac import task_executor as te

_REAL_OPENSHELL_CLI_VERSION = openshell_runtime.openshell_cli_version

_COMBINED = [
    "openshell",
    "sandbox",
    "create",
    "--no-auto-providers",
    "--policy",
    "/policy.yaml",
    "--name",
    "sb-split",
    "--label",
    "mac.owner=mac",
    "--no-tty",
    "--upload",
    "/host/ws:/sandbox",
    "--",
    "/bin/bash",
    "-c",
    "echo hi",
]


def _pin_version(monkeypatch, version):
    monkeypatch.setattr(openshell_runtime, "openshell_cli_version", lambda _bin: version)


def test_split_uses_detach_and_exec_on_openshell_0_1(monkeypatch):
    _pin_version(monkeypatch, (0, 1, 2))

    create, exec_argv = openshell_runtime.split_sandbox_create_command(
        _COMBINED, exec_args=["--workdir", "/sandbox/ws", "--timeout", "30"]
    )

    assert create == [
        "openshell",
        "sandbox",
        "create",
        "--no-auto-providers",
        "--policy",
        "/policy.yaml",
        "--name",
        "sb-split",
        "--label",
        "mac.owner=mac",
        "--upload",
        "/host/ws:/sandbox",
        "--detach",
    ]
    assert exec_argv == [
        "openshell",
        "sandbox",
        "exec",
        "--name",
        "sb-split",
        "--no-tty",
        "--workdir",
        "/sandbox/ws",
        "--timeout",
        "30",
        "--",
        "/bin/bash",
        "-c",
        "echo hi",
    ]


def test_split_keeps_true_initial_command_on_openshell_0_0(monkeypatch):
    # 0.0.x has no --detach and attaches an interactive shell when no command
    # is given; its bounded /bin/true runs over exec and the sandbox persists.
    _pin_version(monkeypatch, (0, 0, 72))

    create, exec_argv = openshell_runtime.split_sandbox_create_command(_COMBINED)

    assert create[-3:] == ["--no-tty", "--", "/bin/true"]
    assert create.count("--") == 1 and "--detach" not in create
    assert "--upload" in create
    assert exec_argv[:6] == ["openshell", "sandbox", "exec", "--name", "sb-split", "--no-tty"]


def test_unknown_version_gets_the_reviewed_0_1_shape(monkeypatch):
    _pin_version(monkeypatch, None)
    assert openshell_runtime.openshell_create_keepalive_args("openshell") == ["--detach"]


@pytest.mark.parametrize(
    "argv, message",
    [
        (["openshell", "sandbox", "exec", "--name", "x", "--", "true"], "sandbox create"),
        (["openshell", "sandbox", "create", "--name", "x"], "no command"),
        (["openshell", "sandbox", "create", "--name", "x", "--"], "empty command"),
        (["openshell", "sandbox", "create", "--", "true"], "name the sandbox"),
    ],
)
def test_split_rejects_argv_it_cannot_split(argv, message):
    with pytest.raises(ValueError, match=message):
        openshell_runtime.split_sandbox_create_command(argv)


def _fake_cli(tmp_path: Path, version_output: str, *, name: str = "openshell") -> Path:
    counter = tmp_path / ("%s.calls" % name)
    cli = tmp_path / name
    cli.write_text(
        "#!/bin/sh\necho x >> %s\nprintf '%%s\\n' '%s'\n" % (counter, version_output),
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return cli


def test_cli_version_probe_parses_and_caches_per_binary_identity(tmp_path):
    cli = _fake_cli(tmp_path, "openshell 0.1.2")

    assert _REAL_OPENSHELL_CLI_VERSION(str(cli)) == (0, 1, 2)
    assert _REAL_OPENSHELL_CLI_VERSION(str(cli)) == (0, 1, 2)
    calls = (tmp_path / "openshell.calls").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1, "the version is cached for an unchanged binary"

    # An in-place upgrade changes the binary identity and is re-probed.
    cli.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'openshell 0.0.72 (extra build info)'\n", encoding="utf-8"
    )
    assert _REAL_OPENSHELL_CLI_VERSION(str(cli)) == (0, 0, 72)


def test_cli_version_probe_is_unknown_for_missing_or_unparseable_cli(tmp_path):
    assert _REAL_OPENSHELL_CLI_VERSION(str(tmp_path / "absent")) is None
    assert _REAL_OPENSHELL_CLI_VERSION("mac-no-such-openshell-binary") is None
    garbled = _fake_cli(tmp_path, "something else", name="garbled")
    assert _REAL_OPENSHELL_CLI_VERSION(str(garbled)) is None


# --- coding-agent probe ------------------------------------------------------


def test_probe_runs_create_then_exec_and_returns_exec_result(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs["stdin"] is subprocess.DEVNULL
        if argv[2] == "create":
            return subprocess.CompletedProcess(argv, 0, "created\n", "")
        return subprocess.CompletedProcess(argv, 3, "SENTINEL\n", "agent err\n")

    monkeypatch.setattr(te.subprocess, "run", fake_run)

    rc, out = te._openshell_probe(_COMBINED, timeout=60.0)

    assert rc == 3
    assert out == "created\nSENTINEL\nagent err\n"
    assert [argv[2] for argv in calls] == ["create", "exec"]
    assert "--upload" in calls[0] and "--" not in calls[0]
    assert calls[1][-3:] == ["/bin/bash", "-c", "echo hi"]


def test_probe_stops_after_failed_create(monkeypatch):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 2, "", "upload rejected\n")

    monkeypatch.setattr(te.subprocess, "run", fake_run)

    rc, out = te._openshell_probe(_COMBINED, timeout=60.0)

    assert (rc, out) == (2, "upload rejected\n")
    assert [argv[2] for argv in calls] == ["create"]


def test_probe_maps_timeout_to_124(monkeypatch):
    def fake_run(argv, **kwargs):
        if argv[2] == "create":
            return subprocess.CompletedProcess(argv, 0, "created\n", "")
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(te.subprocess, "run", fake_run)

    rc, out = te._openshell_probe(_COMBINED, timeout=60.0)

    assert rc == 124
    assert out.startswith("created\n")


# --- detached create ---------------------------------------------------------


def test_detached_create_maps_timeout_and_missing_cli(monkeypatch):
    def timeout(argv, _cwd, limit):
        raise subprocess.TimeoutExpired(argv, limit, output=b"partial", stderr=b"slow")

    monkeypatch.setattr(te, "_run_captured", timeout)
    timed_out = te._sandbox_create_detached(["openshell", "sandbox", "create"], timeout=5)
    assert timed_out.returncode == 124
    assert timed_out.stdout == "partial"
    assert "sandbox create timed out after 5s" in timed_out.stderr

    def missing(*_args):
        raise FileNotFoundError("openshell")

    monkeypatch.setattr(te, "_run_captured", missing)
    absent = te._sandbox_create_detached(["openshell", "sandbox", "create"])
    assert absent.returncode == 127
    assert "could not run" in absent.stderr


# --- task lifecycle ----------------------------------------------------------


def test_failed_create_skips_agent_and_still_tears_down(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_OPENSHELL_PROGRESS_INTERVAL", "0")
    monkeypatch.setattr(te, "_resolve_openshell_policy", lambda: "/policy.yaml")
    monkeypatch.setattr(te, "_ensure_landlock_or_fail", lambda: None)
    monkeypatch.setattr(te, "_sandbox_name", lambda: "sb-create-fails")
    monkeypatch.setattr(te, "_sandbox_gc_best_effort", lambda: None)
    monkeypatch.setattr(te, "_reap_orphaned_task_sandboxes_best_effort", lambda *_: None)
    monkeypatch.setattr(
        te, "_reconcile_task_sandboxes_from_lease_authority_best_effort", lambda *_: None
    )
    monkeypatch.setattr(te, "_sandbox_step", lambda *_args, **_kwargs: (True, ""))
    downloads = []
    deleted = []
    monkeypatch.setattr(te, "_sandbox_download", lambda *args: downloads.append(args) or False)
    monkeypatch.setattr(te, "_sandbox_delete", lambda name: deleted.append(name) or True)
    events = []
    monkeypatch.setattr(
        te, "emit_telemetry", lambda event, **detail: events.append((event, detail)) or True
    )
    monkeypatch.setattr(
        te,
        "_sandbox_create_detached",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 2, "", "error: the argument '--upload <UPLOAD>' cannot be used"
        ),
    )
    agent_calls = []

    def runner(*args, **_kwargs):
        agent_calls.append(args)
        raise AssertionError("the agent must not run when create failed")

    workspace = tmp_path / "task-7"
    workspace.mkdir()
    agent_argv = [
        "/opt/mac-venv/bin/python",
        "-m",
        "mac.agent_command",
        "--command-file",
        "/sandbox/task-7/c.json",
        "--prompt-file",
        "/sandbox/task-7/p",
    ]

    result = te._run_sandboxed(runner, agent_argv, workspace, "tid", {})

    assert result.returncode == 2
    assert "--upload" in result.stderr
    assert agent_calls == []
    assert downloads and deleted == ["sb-create-fails"]
    failed = [detail for event, detail in events if event == "sandbox_create_failed"]
    assert failed and failed[0]["returncode"] == 2


def test_read_only_verifier_exec_failure_is_not_a_pass_and_deletes(monkeypatch, tmp_path):
    from tests.test_openshell_sandbox import _exact_read_only_report_workspace

    workspace, _repo, task = _exact_read_only_report_workspace(tmp_path)
    monkeypatch.setattr(
        te,
        "_read_only_verifier_extra_create_argv",
        lambda: ["--from", "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "b" * 64],
    )
    calls = []

    def step(args, *, timeout):
        calls.append(list(args))
        if args[0] == "exec":
            return False, "contract tests failed"
        return True, ""

    monkeypatch.setattr(te, "_sandbox_step", step)

    assert not te._sandbox_run_read_only_repository_verification("mac-task-agent", workspace, task)
    assert [call[0] for call in calls] == ["create", "exec", "download", "delete"]


def test_read_only_verifier_failed_create_never_execs(monkeypatch, tmp_path):
    from tests.test_openshell_sandbox import _exact_read_only_report_workspace

    workspace, _repo, task = _exact_read_only_report_workspace(tmp_path)
    monkeypatch.setattr(
        te,
        "_read_only_verifier_extra_create_argv",
        lambda: ["--from", "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "b" * 64],
    )
    calls = []

    def step(args, *, timeout):
        calls.append(list(args))
        return (args[0] != "create"), ("create rejected" if args[0] == "create" else "")

    monkeypatch.setattr(te, "_sandbox_step", step)

    assert not te._sandbox_run_read_only_repository_verification("mac-task-agent", workspace, task)
    assert [call[0] for call in calls] == ["create", "download", "delete"]
