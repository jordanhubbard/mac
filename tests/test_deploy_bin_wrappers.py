"""deploy/bin is the source of truth for the host wrappers.

mac.node_files (run by scripts/fleet-update) installs mac-agent-service,
mac-agent-startup-self-test, mac-task-executor and mac-task-executor.py from here
into ~/.mac/bin on each worker and on the hub, whose own agent runs under them;
mac-service is the hub's control-plane wrapper, a managed file of the hub role.
Nothing generates them any more, so these checks prove the files are present,
parse, and are what the node installer declares.
"""

from __future__ import annotations

import os
import py_compile
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import sys

from mac import node_files

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "deploy" / "bin"
SHELL_WRAPPERS = (
    "mac-agent-service",
    "mac-agent-startup-self-test",
    "mac-service",
    "mac-task-executor",
)
PYTHON_WRAPPERS = ("mac-task-executor.py",)


def test_deploy_bin_holds_exactly_the_known_wrappers() -> None:
    assert sorted(p.name for p in BIN.iterdir()) == sorted(SHELL_WRAPPERS + PYTHON_WRAPPERS)


def test_the_node_installer_declares_every_wrapper_from_deploy_bin() -> None:
    worker = {src: (dest, mode) for src, dest, mode in node_files.MANAGED["worker"]}
    for name in ("mac-agent-service", "mac-agent-startup-self-test", "mac-task-executor"):
        assert worker["deploy/bin/" + name] == ("bin/" + name, 0o700)
    assert worker["deploy/bin/mac-task-executor.py"] == ("bin/mac-task-executor.py", 0o600)
    hub = {src: (dest, mode) for src, dest, mode in node_files.MANAGED["hub"]}
    assert hub["deploy/bin/mac-service"] == ("bin/mac-service", 0o755)
    assert set(worker) < set(hub)
    script = (ROOT / "scripts" / "fleet-update").read_text(encoding="utf-8")
    assert '-m mac.node_files --source "$src" --role worker' in script
    assert '-m mac.node_files --source "$SRC" --role hub' in script


@pytest.mark.parametrize("name", SHELL_WRAPPERS)
def test_shell_wrapper_is_executable_and_parses(name: str) -> None:
    path = BIN / name
    assert path.is_file()
    assert os.access(path, os.X_OK), "%s must be executable" % name
    assert path.read_text(encoding="utf-8").split("\n", 1)[0] in (
        "#!/usr/bin/env bash",
        "#!/bin/bash",
    )
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", SHELL_WRAPPERS)
def test_shell_wrapper_passes_shellcheck(name: str) -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(
        [shellcheck, "-S", "warning", str(BIN / name)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("name", PYTHON_WRAPPERS)
def test_python_wrapper_compiles(name: str, tmp_path: Path) -> None:
    py_compile.compile(str(BIN / name), cfile=str(tmp_path / "out.pyc"), doraise=True)


def _hub_install_bin_functions() -> str:
    script = (ROOT / "scripts" / "fleet-update").read_text(encoding="utf-8")
    log = re.search(r"^log\(\) \{.*\}$", script, re.M)
    body = re.search(r"^hub_install_bin\(\) \{.*?^\}$", script, re.M | re.S)
    assert log and body, "fleet-update must define log and hub_install_bin"
    return log.group(0) + "\n" + body.group(0) + "\n"


def _venv(tmp_path: Path) -> Path:
    """A venv whose python is this interpreter running this checkout's mac."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text(
        '#!/bin/sh\nPYTHONPATH="%s" exec "%s" "$@"\n' % (ROOT / "src", sys.executable)
    )
    python.chmod(0o755)
    return venv


def test_fleet_update_installs_the_worker_wrappers_on_the_hub_too(tmp_path: Path) -> None:
    """The hub's own agent runs under deploy/bin and the crash observer too.

    Before this, a hub update left them at whatever was copied by hand, so the
    hub's agent ran without #928's attempt timeout and #960's live log tee.
    """
    home = tmp_path / "mac"
    (home / "bin").mkdir(parents=True)
    stale = home / "bin" / "mac-agent-service"
    stale.write_text("stale\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _hub_install_bin_functions() + "hub_install_bin"],
        capture_output=True,
        text=True,
        env={**os.environ, "SRC": str(ROOT), "MAC_HOME": str(home), "VENV": str(_venv(tmp_path))},
    )
    assert result.returncode == 0, result.stderr
    assert "hub: managed files:" in result.stdout
    for name in SHELL_WRAPPERS:
        installed = home / "bin" / name
        assert installed.read_bytes() == (BIN / name).read_bytes()
        assert stat.S_IMODE(installed.stat().st_mode) == (0o755 if name == "mac-service" else 0o700)
    executor = home / "bin" / "mac-task-executor.py"
    assert stat.S_IMODE(executor.stat().st_mode) == 0o600
    observer = home / "bin" / "mac-crash-observer"
    assert observer.read_bytes() == (ROOT / "deploy" / "mac-crash-observer.py").read_bytes()
    assert stat.S_IMODE(observer.stat().st_mode) == 0o755


def test_a_failed_hub_wrapper_install_warns_without_failing_the_update(tmp_path: Path) -> None:
    home = tmp_path / "mac"
    home.mkdir()
    (home / "bin").write_text("not a directory\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _hub_install_bin_functions() + "hub_install_bin; echo after=$?"],
        capture_output=True,
        text=True,
        env={**os.environ, "SRC": str(ROOT), "MAC_HOME": str(home), "VENV": str(_venv(tmp_path))},
    )
    assert "hub: WARNING managed file install failed" in result.stdout
    assert "after=0" in result.stdout
