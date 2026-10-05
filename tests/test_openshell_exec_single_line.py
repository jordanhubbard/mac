"""Every ``openshell sandbox exec`` argv MAC builds must be newline-free.

OpenShell's exec RPC rejects any command argument containing a newline or
carriage return. After work moved from ``sandbox create ... -- <cmd>`` to
create-then-exec (#935), every task on the fleet failed at the coding-agent
preflight on OpenShell 0.0.72 with::

    command argument 2 contains newline or carriage return characters

because the probe passed ``bash -lc "<multi-line script>"``. The unit tests
mocked OpenShell, so nothing noticed. These tests drive every exec-building
path, for both keep-alive generations, and assert the arguments are single
lines that still carry the original script.
"""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

import pytest

from mac import executor_sandbox as te
from mac import gitops, openshell_runtime, services
from mac.openshell_runtime import (
    OpenShellExecArgvError,
    assert_exec_argv_single_line,
    single_line_shell_script,
    split_sandbox_create_command,
)

_ENCODED = re.compile(r'^__mac_script="\$\(printf %s ([A-Za-z0-9+/=]+) \| /usr/bin/base64 -d\)"')


def decode_shell_argument(argument: str) -> str:
    """The script a ``bash -c`` argument runs (decoded when it was encoded)."""
    match = _ENCODED.match(argument)
    if not match:
        return argument
    return base64.b64decode(match.group(1)).decode("utf-8")


def _assert_single_line(argv) -> None:
    for index, token in enumerate(argv):
        assert "\n" not in token and "\r" not in token, (index, token)


@pytest.fixture(params=[(0, 0, 72), (0, 1, 0)], ids=["openshell-0.0.x", "openshell-0.1.x"])
def openshell_version(request, monkeypatch):
    monkeypatch.setattr(openshell_runtime, "openshell_cli_version", lambda _bin: request.param)
    return request.param


# --- the guard ---------------------------------------------------------------


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "trailing\n"])
def test_guard_rejects_newline_and_carriage_return(bad):
    with pytest.raises(OpenShellExecArgvError, match="argument 3"):
        assert_exec_argv_single_line(["openshell", "sandbox", "exec", bad])


def test_split_refuses_a_multi_line_command(openshell_version):
    argv = ["openshell", "sandbox", "create", "--name", "n", "--", "/bin/sh", "-c", "a\nb"]
    with pytest.raises(OpenShellExecArgvError):
        split_sandbox_create_command(argv)


def test_sandbox_step_refuses_a_multi_line_exec(monkeypatch):
    ran = []
    monkeypatch.setattr(te, "_run_captured", lambda *args: ran.append(args))
    with pytest.raises(OpenShellExecArgvError):
        te._sandbox_step(["exec", "--name", "n", "--", "/bin/sh", "-c", "a\nb"], timeout=1.0)
    assert ran == []


def test_single_line_script_preserves_output_status_and_exec(tmp_path):
    script = "\n".join(
        [
            "set -a",
            "x='two words'",
            'if [ -n "$x" ]; then printf "%s|%%s\\n" "$x"; fi',
            "printf 'err\\r\\n' >&2",
            "exec /bin/sh -c 'exit 7'",
        ]
    )
    encoded = single_line_shell_script(script)
    _assert_single_line([encoded])
    assert decode_shell_argument(encoded) == script
    direct = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)
    wrapped = subprocess.run(["/bin/bash", "-c", encoded], capture_output=True, text=True)
    assert (wrapped.returncode, wrapped.stdout, wrapped.stderr) == (
        direct.returncode,
        direct.stdout,
        direct.stderr,
    )
    assert wrapped.returncode == 7
    assert single_line_shell_script("echo one line") == "echo one line"


# --- every exec-building path ------------------------------------------------


def test_task_agent_launch_exec_is_single_line(monkeypatch, tmp_path, openshell_version):
    monkeypatch.setattr(te, "_resolve_openshell_policy", lambda: "/policy.yaml")
    monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", "bounded-tmpfs")
    create_argv = te._build_sandbox_create_argv(
        "mac-task-x",
        tmp_path,
        "task-x",
        ["python", "-m", "mac.agent_command"],
        extra_create_argv=["--from", "approved-image"],
    )
    create, exec_argv = te._sandbox_launch_argvs(create_argv)
    _assert_single_line(exec_argv)
    inner = decode_shell_argument(exec_argv[-1])
    assert "mac_sandbox_toolchain_setup" in inner and "\nexec " in inner
    if openshell_version < (0, 1, 0):
        assert create[-3:] == ["--no-tty", "--", "/bin/true"]
    else:
        assert create[-1] == "--detach"


def test_coding_agent_probe_exec_is_single_line(monkeypatch, tmp_path, openshell_version):
    monkeypatch.setattr(te, "_resolve_openshell_policy", lambda: "/policy.yaml")
    private_dir = tmp_path / "probe"
    bundle = te._write_agent_command_bundle(
        private_dir, "PRIVATE-PROMPT\nline two\r\n", ["opencode", "run", te.PROMPT_SENTINEL]
    )
    probe_argv = te._build_sandbox_probe_argv(
        "mac-probe-x", bundle.argv(sandbox_workspace="/sandbox/probe"), private_dir
    )
    steps = []

    def run(argv, **kwargs):
        steps.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(te.subprocess, "run", run)
    assert te._openshell_probe(probe_argv, timeout=30.0)[0] == 0
    assert [step[2] for step in steps] == ["create", "exec"]
    _assert_single_line(steps[1])
    inner = decode_shell_argument(steps[1][-1])
    assert "mac.agent_command" in inner and "PRIVATE-PROMPT" not in inner


def test_lifecycle_execs_are_single_line(monkeypatch, tmp_path):
    """Progress snapshot, download cleanup, report detail, read-only checks."""
    calls = []

    def captured(argv, cwd, timeout):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(te, "_run_captured", captured)
    monkeypatch.setenv("MAC_TASK_REPO_BASE_SHA", "a" * 40)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(tmp_path / "repo"))
    te._sandbox_progress_snapshot("n", "task-x", tmp_path)
    te._sandbox_download("n", "task-x", tmp_path)
    execs = [argv for argv in calls if argv[2] == "exec"]
    assert len(execs) == 2
    for argv in execs:
        _assert_single_line(argv)
    assert "changed_digest=" in decode_shell_argument(execs[0][-1])

    seen = []

    def run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(te.subprocess, "run", run)
    te._sandbox_verification_report_detail("n", "/sandbox/task-x")
    assert seen and seen[0][2] == "exec"
    _assert_single_line(seen[0])


def test_repository_verifier_execs_are_single_line(monkeypatch, tmp_path):
    launched = []

    def popen(argv, **kwargs):
        launched.append(list(argv))
        raise OSError("not launched in tests")

    monkeypatch.setattr(te.subprocess, "Popen", popen)
    result = te._sandbox_run_repository_verification_exec(
        "n", "/sandbox/task-x", "/sandbox/task-x/.verify.sh", "/tmp/marker", timeout=5.0
    )
    assert result.failure_class == "verifier_infrastructure"
    assert launched and launched[0][2] == "exec"
    _assert_single_line(launched[0])


def test_read_only_verifier_exec_is_single_line(monkeypatch, tmp_path):
    from tests.test_openshell_sandbox import _exact_read_only_report_workspace

    workspace, _, task = _exact_read_only_report_workspace(tmp_path)
    monkeypatch.setattr(
        te, "_read_only_verifier_extra_create_argv", lambda: ["--from", "approved-image"]
    )
    calls = []

    def captured(argv, cwd, timeout):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(te, "_run_captured", captured)
    te._sandbox_run_read_only_repository_verification("agent-sandbox", workspace, task)
    execs = [argv for argv in calls if argv[2] == "exec"]
    assert execs
    for argv in execs:
        _assert_single_line(argv)


def test_hub_verifier_execs_are_single_line_with_multi_line_commands(monkeypatch):
    head = "a" * 40
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == "git" and "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, head + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "passed", "")

    monkeypatch.setattr(services.subprocess, "run", run)
    monkeypatch.setattr(gitops, "askpass_remote_auth", lambda url: (url, {}))
    monkeypatch.setenv(
        "MAC_HUB_VERIFY_IMAGE",
        "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64,
    )
    monkeypatch.delenv("MAC_OPENSHELL_GC", raising=False)
    services.run_repository_contract_test_in_openshell(
        "https://example.invalid/repo.git",
        "branch",
        head,
        "set -e\nrun-repository-tests\r\n",
        "bootstrap-step-1\nbootstrap-step-2",
    )
    execs = [argv for argv in calls if "exec" in argv[:3]]
    assert len(execs) == 2
    for argv in execs:
        _assert_single_line(argv)
    scripts = [decode_shell_argument(argv[-1]) for argv in execs]
    assert scripts[0].endswith("bootstrap-step-1\nbootstrap-step-2")
    assert scripts[1].endswith("set -e\nrun-repository-tests\r\n")


def test_bootstrap_confinement_probe_execs_an_uploaded_file():
    script = (
        Path(__file__).resolve().parents[1] / "deploy" / "openshell" / "bootstrap-openshell.sh"
    ).read_text(encoding="utf-8")
    assert "-- /bin/bash /sandbox/live-confinement-probe.sh" in script
