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
REQUIRED_COMMANDS = ("python3", "git", "gh")
# Actionable install hints keyed by required command. Surfaced when a
# prerequisite is missing so environment-repair does not require guessing
# which package provides the tool on the failing host.
COMMAND_INSTALL_HINTS = {
    "python3": "install Python 3.11+ from your OS package manager or python.org",
    "git": "install git (e.g. `apt-get install git`, `brew install git`)",
    "gh": "install the GitHub CLI from https://cli.github.com (e.g. `brew install gh`)",
}


def run(command: list[str]) -> None:
    print("+ %s" % " ".join(command), flush=True)
    subprocess.run(command, cwd=str(ROOT), check=True)


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


def has_pip() -> bool:
    return (
        subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "--version"],
            cwd=str(ROOT),
            capture_output=True,
        ).returncode
        == 0
    )


def ensure_pip() -> None:
    """Make sure the venv has pip, even when something else created it.

    An existing venv is REUSED -- the check above only builds one when the
    interpreter is missing -- and a venv built by `uv venv` has no pip at all.
    That is the likely state in this repository, which runs `uv run --extra dev`
    everywhere, and it produced a bare failure on puck.local:

        /Users/jkh/Src/mac/.venv/bin/python: No module named pip
        make: *** [.venv/bin/mac] Error 1

    `ensurepip` is tried first because it preserves the existing environment.
    Some distributions ship Python without it, so recreating the venv is the
    fallback -- noisy, but it leaves a working install rather than a message
    about a module the user never chose to omit.
    """
    if has_pip():
        return
    print("venv has no pip (created by uv?); bootstrapping it", flush=True)
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "ensurepip", "--upgrade"],
        cwd=str(ROOT),
        capture_output=True,
    )
    if result.returncode == 0 and has_pip():
        return
    print("ensurepip unavailable; recreating the venv with python -m venv", flush=True)
    shutil.rmtree(VENV)
    run([sys.executable, "-m", "venv", str(VENV)])
    if not has_pip():
        raise SystemExit(
            "could not provision pip in %s; create it with `python3 -m venv` "
            "and re-run `make install`" % VENV
        )


def main() -> int:
    # --venv-only: build just the .venv (pip install -e .[dev]) without the
    # dev-workflow tool checks. git/gh serve the human dev loop; verification
    # hosts (worker venvs, sandboxes) need only the venv, and requiring gh
    # there blocked contract-test bootstrap on the GKE pods.
    # --check: verify prerequisites (required commands + Python version)
    # without creating or mutating the venv, so a verification host can
    # pre-flight the environment prerequisite before running the suite.
    args = sys.argv[1:]
    venv_only = "--venv-only" in args
    check_only = "--check" in args
    required = ("python3",) if venv_only else REQUIRED_COMMANDS
    if not (ROOT / "pyproject.toml").exists():
        print("bootstrap-project.py must be run from a mac checkout", file=sys.stderr)
        return 2
    if sys.version_info < (3, 11):
        print(
            "Python 3.11+ is required to bootstrap mac; current interpreter is %s"
            % sys.version.split()[0],
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
    if not VENV_PYTHON.exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    ensure_pip()
    run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(VENV_PYTHON), "-m", "pip", "install", "-e", ".[dev]"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
