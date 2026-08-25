"""A test that shells out must reach the same ``mac`` the test imported.

`pythonpath = ["src"]` in pyproject.toml prepends to the *pytest process's*
``sys.path``. It does not set ``PYTHONPATH``, so until conftest.py re-exported
it, ``subprocess.run([sys.executable, "-m", "mac.cli", ...])`` imported
whatever ``mac`` that interpreter had installed.

On a dev box nothing showed, because .venv carries an editable install and the
two copies are the same files. The verification sandbox has no .venv: the
runner resolves /opt/mac-venv/bin/python, whose image bakes a released ``mac``.
So in-process assertions tested the worktree and shelled-out assertions tested
the image, and the split surfaced as two unrelated-looking failures:

  * tests/cli/test_cli_version_flag.py -- the 4 cases that shell out AND expect
    success failed with ``mac: error: the following arguments are required:
    SUBCOMMAND``, the pre-``--version`` behaviour its own docstring quotes. The
    two that shell out expecting failure passed, and so did the one that never
    leaves the process. That split is the fingerprint: it is not about
    ``--version`` at all, it is about which ``mac`` answered.
  * tests/test_worker_shutdown_abandon.py::
    test_real_sigterm_then_sigkill_still_releases_the_lease -- its child script
    sets ``worker.shutdown_grace_seconds``, an attribute the baked worker.py
    does not have, so the child never ran the release path and the lease stayed
    held.

Neither reproduced outside the sandbox, so both read as flakes and cost a
doc-only task most of its attempts. These tests pin the invariant directly,
against any interpreter, so the next drift fails here with one clear reason
instead of scattering across the suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import mac

ROOT = Path(__file__).resolve().parent.parent


def _child(*code: str) -> subprocess.CompletedProcess:
    """Run a snippet the way an ordinary test shells out: inherited env only."""

    return subprocess.run(
        [sys.executable, "-c", "\n".join(code)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_a_subprocess_imports_the_same_mac_as_this_process():
    """The whole defect, reduced to one assertion."""

    result = _child("import mac", "print(mac.__file__)")

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == Path(mac.__file__).resolve(), (
        "a child process imported a different mac than the test did; the "
        "worktree is at %s" % Path(mac.__file__).resolve().parent
    )


def test_the_worktree_source_is_what_a_subprocess_gets():
    """Not merely 'the same as us' -- specifically this checkout."""

    result = _child("import mac", "print(mac.__file__)")

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve().is_relative_to(ROOT), (
        "a child imported mac from outside the worktree (%s); an installed "
        "copy is shadowing src/" % result.stdout.strip()
    )


def test_pythonpath_carries_the_ini_entry():
    """conftest re-exports pythonpath so children inherit it."""

    entries = [Path(p).resolve() for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]

    assert (ROOT / "src").resolve() in entries, (
        'src/ is missing from PYTHONPATH=%r; pythonpath=["src"] in '
        "pyproject.toml only edits this process's sys.path" % os.environ.get("PYTHONPATH", "")
    )


def test_the_cli_entrypoint_resolves_to_the_worktree():
    """`python -m mac.cli` is the exact form the version-flag tests use."""

    result = _child("import mac.cli", "print(mac.cli.__file__)")

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == (ROOT / "src/mac/cli.py").resolve()


def test_exporting_is_idempotent():
    """A test that rebuilds PYTHONPATH by hand must not duplicate src/.

    Several tests predate the conftest export and still prepend ROOT/src
    themselves. That has to stay harmless.
    """

    entries = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    resolved = [str(Path(p).resolve()) for p in entries]

    assert resolved.count(str((ROOT / "src").resolve())) == 1, (
        "src/ appears more than once in PYTHONPATH: %r" % entries
    )
