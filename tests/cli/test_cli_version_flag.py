"""`mac --version` prints the version and exits cleanly.

It used to print a usage error instead:

    usage: mac [-h] [--db DB] [--local-authority] [--hub-url HUB_URL] ...
    mac: error: the following arguments are required: SUBCOMMAND

The flag did not exist at all, so argparse fell through to enforcing the
required positional. That is the wrong answer twice over -- it says nothing
about the version, and the first thing anyone does when a CLI misbehaves is ask
it what version it is.

The number comes from `mac.__version__`, the same attribute `pyproject.toml`
reads for its dynamic version and `mac.api` gives FastAPI. A CLI that printed
its own copy could disagree with the wheel it shipped in, which is exactly the
four-way hand-copied drift that attribute was created to end.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import mac
from mac import __version__, cli

# The child must import the mac under test, not an installed one. pytest's
# `pythonpath = ["src"]` (pyproject) only shadows an installed package for
# IN-PROCESS imports; a subprocess started with `sys.executable -m mac.cli`
# gets that interpreter's site-packages instead. On a host whose resolved
# interpreter is the pre-baked runtime venv (/opt/mac-venv, which
# run-contract-tests.sh falls back to when the worktree has no .venv) that copy
# can predate `--version` entirely, and these tests then fail with the very
# usage error quoted above -- reporting the baked wheel's age as a defect in
# the worktree. Deriving the path from `mac.__file__` rather than from a
# relative guess is the point: the child imports exactly the package this
# module imported.
_SRC = str(Path(mac.__file__).resolve().parent.parent)


def _mac(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    )
    return subprocess.run(
        [sys.executable, "-m", "mac.cli", *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def test_it_prints_the_version_and_exits_zero():
    result = _mac("--version")

    assert result.returncode == 0, (
        "`mac --version` exited %d; it used to fail with 'the following "
        "arguments are required: SUBCOMMAND'" % result.returncode
    )
    assert result.stdout.strip() == "mac %s" % __version__


def test_it_does_not_demand_a_subcommand():
    """The whole defect: a required positional swallowed the question."""
    result = _mac("--version")

    assert "SUBCOMMAND" not in (result.stdout + result.stderr)
    assert "usage:" not in (result.stdout + result.stderr)


def test_the_version_is_the_package_version():
    """Not a second copy. `pyproject.toml` has dynamic = ["version"] pointing at
    src/mac/__init__.py, and mac.api hands the same attribute to FastAPI."""
    import mac

    assert _mac("--version").stdout.strip() == "mac %s" % mac.__version__


def test_it_works_alongside_the_other_global_flags():
    """--json is accepted in any position, so it can precede --version."""
    assert _mac("--json", "--version").returncode == 0


def test_the_flag_is_on_the_root_parser_not_a_subcommand():
    parser = cli.build_parser()

    assert "--version" in {
        option for action in parser._actions for option in action.option_strings
    }


def test_a_real_subcommand_still_requires_its_arguments():
    """Adding a short-circuiting flag must not relax the parser elsewhere."""
    result = _mac("task", "show")

    assert result.returncode != 0
    assert "usage:" in result.stderr


def test_no_subcommand_at_all_is_still_an_error():
    """`mac` alone must keep reporting the missing SUBCOMMAND."""
    result = _mac()

    assert result.returncode != 0
    assert "SUBCOMMAND" in result.stderr
