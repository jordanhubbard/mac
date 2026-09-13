#!/usr/bin/env python3
"""Create the local development environment required by mac workers."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV = Path(os.environ.get("MAC_VENV") or ".venv").expanduser()
if not VENV.is_absolute():
    VENV = ROOT / VENV
BIN_DIR = "Scripts" if os.name == "nt" else "bin"
VENV_PYTHON = VENV / BIN_DIR / ("python.exe" if os.name == "nt" else "python")
REQUIRED_COMMANDS = ("python3", "git", "gh", "uv")
# Actionable install hints keyed by required command. Surfaced when a
# prerequisite is missing so environment-repair does not require guessing
# which package provides the tool on the failing host.
COMMAND_INSTALL_HINTS = {
    "python3": "install the version in .python-version with `uv python install`",
    "git": "install git (e.g. `apt-get install git`, `brew install git`)",
    "uv": "install the reviewed uv release from https://docs.astral.sh/uv/",
    "gh": "install the GitHub CLI from https://cli.github.com (e.g. `brew install gh`)",
}


def missing_commands(required: tuple[str, ...]) -> list[str]:
    """Return the subset of ``required`` commands not found on PATH."""
    return [command for command in required if shutil.which(command) is None]


def report_missing_commands(missing: list[str]) -> None:
    """Print an actionable, per-command diagnostic for missing prerequisites."""
    print(
        "missing required command(s): %s" % ", ".join(missing),
        file=sys.stderr,
    )
    for command in missing:
        hint = COMMAND_INSTALL_HINTS.get(command)
        if hint:
            print("  - %s: %s" % (command, hint), file=sys.stderr)


PYTHON_VERSION = (ROOT / ".python-version").read_text().strip()
REQUIRED_PYTHON = tuple(int(part) for part in PYTHON_VERSION.split("."))


def venv_python_is_supported() -> bool:
    """Does the interpreter INSIDE the venv match the reviewed patch?

    Asked separately from `sys.version_info` because they are different
    interpreters and only one of them was ever checked. Returns True when the
    version cannot be read: an unreadable venv is a different problem, and
    deleting someone's environment on a failed probe is worse than proceeding.
    """
    result = subprocess.run(
        [
            str(VENV_PYTHON),
            "-c",
            "import platform; print(platform.python_version())",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return True
    try:
        version = tuple(int(part) for part in result.stdout.strip().split("."))
    except ValueError:
        return True
    return version == REQUIRED_PYTHON


def main() -> int:
    # --venv-only: build just the .venv from uv.lock without the
    # dev-workflow tool checks. git/gh serve the human dev loop; verification
    # hosts (worker venvs, sandboxes) need only the venv, and requiring gh
    # there blocked contract-test bootstrap on the GKE pods.
    # --check: verify prerequisites (required commands + Python version)
    # without creating or mutating the venv, so a verification host can
    # pre-flight the environment prerequisite before running the suite.
    args = sys.argv[1:]
    venv_only = "--venv-only" in args
    check_only = "--check" in args
    required = ("python3", "uv") if venv_only else REQUIRED_COMMANDS
    if not (ROOT / "pyproject.toml").exists():
        print("bootstrap-project.py must be run from a mac checkout", file=sys.stderr)
        return 2
    if sys.version_info[:3] != REQUIRED_PYTHON:
        print(
            "Python %s is required to bootstrap mac; current interpreter is %s. "
            "Run `uv python install` then invoke this script with `uv python find`."
            % (PYTHON_VERSION, sys.version.split()[0]),
            file=sys.stderr,
        )
        return 2
    missing = missing_commands(required)
    if missing:
        report_missing_commands(missing)
        return 2
    if check_only:
        print(
            "prerequisites satisfied: %s" % ", ".join(required),
            flush=True,
        )
        return 0

    print(
        "Bootstrapping mac on %s/%s with %s"
        % (platform.system(), platform.machine(), sys.executable),
        flush=True,
    )
    if not VENV_PYTHON.exists() and VENV.exists():
        shutil.rmtree(VENV)
    if VENV_PYTHON.exists() and not venv_python_is_supported():
        # The check above is on the interpreter running THIS script, not on the
        # one inside the venv. An existing venv is reused unconditionally, so a
        # .venv built years ago on 3.9 survives every `make install` as long as
        # the interpreter you invoke it with is modern -- and then editable
        # installs land in an interpreter mac does not support. The failure
        # arrives later, somewhere unrelated, as an import or a syntax error.
        print(
            "recreating %s: its interpreter differs from Python %s" % (VENV, PYTHON_VERSION),
            flush=True,
        )
        shutil.rmtree(VENV)
    # Use the same locked dependency versions as CI and the runtime image.
    # The explicit environment path preserves MAC_VENV; Python is the one
    # checked above, never a different interpreter selected from ambient PATH.
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(VENV))
    subprocess.run(
        ["uv", "sync", "--locked", "--extra", "dev", "--python", sys.executable],
        cwd=str(ROOT),
        env=env,
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
