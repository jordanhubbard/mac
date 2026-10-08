"""deploy/bin is the source of truth for the host wrappers.

scripts/fleet-update installs mac-agent-service, mac-agent-startup-self-test,
mac-task-executor and mac-task-executor.py from here into ~/.mac/bin on each
worker and on the hub, whose own agent runs under them; mac-service is the hub's control-plane wrapper. Nothing generates them
any more, so these checks only prove the files are present and parse.
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


def test_fleet_update_installs_every_worker_wrapper_from_deploy_bin() -> None:
    script = (ROOT / "scripts" / "fleet-update").read_text(encoding="utf-8")
    assert (
        "for f in mac-agent-service mac-agent-startup-self-test mac-task-executor; "
        'do put 0700 "deploy/bin/$f" "$bin/$f"; done'
    ) in script
    assert 'put 0600 deploy/bin/mac-task-executor.py "$bin/mac-task-executor.py"' in script


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
    body = re.search(r"^hub_put\(\) .*?^hub_install_bin\(\) \{.*?^\}$", script, re.M | re.S)
    assert log and body, "fleet-update must define log, hub_put and hub_install_bin"
    return log.group(0) + "\n" + body.group(0) + "\n"


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
        env={**os.environ, "SRC": str(ROOT), "MAC_HOME": str(home)},
    )
    assert result.returncode == 0, result.stderr
    assert "hub: installed deploy/bin and the crash observer" in result.stdout
    for name in SHELL_WRAPPERS:
        if name == "mac-service":
            continue
        installed = home / "bin" / name
        assert installed.read_bytes() == (BIN / name).read_bytes()
        assert stat.S_IMODE(installed.stat().st_mode) == 0o700
    executor = home / "bin" / "mac-task-executor.py"
    assert stat.S_IMODE(executor.stat().st_mode) == 0o600
    observer = home / "bin" / "mac-crash-observer"
    assert observer.read_bytes() == (ROOT / "deploy" / "mac-crash-observer.py").read_bytes()
    assert stat.S_IMODE(observer.stat().st_mode) == 0o755
    assert not list((home / "bin").glob("*.fleet-update.*"))


def test_a_failed_hub_wrapper_install_warns_without_failing_the_update(tmp_path: Path) -> None:
    home = tmp_path / "mac"
    home.mkdir()
    (home / "bin").write_text("not a directory\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _hub_install_bin_functions() + "hub_install_bin; echo after=$?"],
        capture_output=True,
        text=True,
        env={**os.environ, "SRC": str(ROOT), "MAC_HOME": str(home)},
    )
    assert "hub: WARNING installing deploy/bin" in result.stdout
    assert "after=0" in result.stdout
