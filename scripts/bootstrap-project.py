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


def run(command: list[str]) -> None:
    print("+ %s" % " ".join(command), flush=True)
    subprocess.run(command, cwd=str(ROOT), check=True)


def main() -> int:
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
    missing = [command for command in REQUIRED_COMMANDS if shutil.which(command) is None]
    if missing:
        print(
            "missing required command(s): %s" % ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    print(
        "Bootstrapping mac on %s/%s with %s"
        % (platform.system(), platform.machine(), sys.executable),
        flush=True,
    )
    if not VENV_PYTHON.exists() and VENV.exists():
        shutil.rmtree(VENV)
    if not VENV_PYTHON.exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(VENV_PYTHON), "-m", "pip", "install", "-e", ".[dev]"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
