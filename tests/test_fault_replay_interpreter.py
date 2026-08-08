"""The fault replay must run under an interpreter that can reach the ledger.

main went red on 2026-08-08 with a nested ImportError inside a captured
subprocess::

    ModuleNotFoundError: No module named 'psycopg'
      ... ControlPlane.in_memory() -> ephemeral_store -> PostgresStore

``scripts/fault-replay.py`` spawns each probe with ``sys.executable``, and CI
invoked the script BARE -- ``run: scripts/fault-replay.py`` -- so the shebang
picked the system ``python3`` rather than the project environment every other
step uses. That was harmless while ``ControlPlane.in_memory()`` was SQLite-
backed. The Postgres migration made every probe need the driver, and the replay
has been broken since.

It surfaced late for a familiar reason: the ``nightly`` job runs only on a
schedule, so this was invisible on every pull request and appeared as a red
main days after the change that broke it.

The fix is in two places on purpose. CI now invokes the script through
``uv run`` like everything else, and the script resolves a usable interpreter
itself so it is correct however it is invoked.

A first attempt at that resolution preferred any ``ROOT/.venv`` it found and
picked one that existed WITHOUT the driver -- the same "looks configured, is
not" failure the replay exists to catch. So candidates are now asked whether
they can import the driver rather than assumed to.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fault-replay.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def replay():
    spec = importlib.util.spec_from_file_location("fault_replay", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_resolved_interpreter_can_import_the_driver(replay):
    """The property that was actually missing.

    Every probe builds a ControlPlane, which needs psycopg since the Postgres
    migration. An interpreter that cannot import it produces a nested
    ImportError inside a captured subprocess -- the least legible way possible
    to learn that the environment is wrong.
    """
    interpreter = replay._probe_interpreter()

    completed = subprocess.run(
        [interpreter, "-c", "import psycopg"], capture_output=True, check=False
    )
    assert completed.returncode == 0, (
        "fault-replay resolved %r, which cannot import psycopg" % interpreter
    )


def test_resolution_verifies_rather_than_assumes(replay, tmp_path, monkeypatch):
    """A venv that exists but lacks the driver must not be chosen.

    This is the bug the first attempt at the fix had, and it is the same shape
    as the defect being fixed: something that looks configured and is not.
    """
    fake_root = tmp_path / "repo"
    (fake_root / ".venv" / "bin").mkdir(parents=True)
    broken = fake_root / ".venv" / "bin" / "python"
    broken.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    broken.chmod(0o755)
    monkeypatch.setattr(replay, "ROOT", fake_root)

    interpreter = replay._probe_interpreter()

    assert interpreter != str(broken)
    assert (
        subprocess.run([interpreter, "-c", "import psycopg"], capture_output=True).returncode
        == 0
    )


def test_ci_invokes_the_script_through_the_project_environment():
    """Bare invocation is what selected the system python in the first place."""
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "run: scripts/fault-replay.py" not in text, (
        "CI runs fault-replay bare again; the shebang picks the system python3, "
        "which has no psycopg"
    )
    assert "uv run" in text and "scripts/fault-replay.py" in text


def test_the_script_says_what_to_do_when_no_interpreter_works(replay, tmp_path, monkeypatch):
    """A refusal an operator can act on beats a nested ImportError."""
    fake_root = tmp_path / "empty"
    fake_root.mkdir()
    monkeypatch.setattr(replay, "ROOT", fake_root)
    monkeypatch.setattr(replay.shutil, "which", lambda _name: None)
    monkeypatch.setattr(replay.subprocess, "run", lambda *a, **k: _Fail())

    with pytest.raises(SystemExit) as excinfo:
        replay._probe_interpreter()

    message = str(excinfo.value)
    assert "psycopg" in message
    assert "uv run" in message


class _Fail:
    returncode = 1
    stdout = ""
    stderr = ""


def test_the_probe_runner_actually_uses_the_resolved_interpreter(replay, monkeypatch, tmp_path):
    """The wiring, which is the part that was actually broken.

    The tests above exercise _probe_interpreter() in isolation, and they pass
    happily against a build where _run_probe still spawns sys.executable --
    which is exactly the bug. Assert the resolved interpreter reaches the
    subprocess.
    """
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Ok()

    monkeypatch.setattr(replay, "_probe_interpreter", lambda: "/sentinel/python")
    monkeypatch.setattr(replay.subprocess, "run", fake_run)

    replay._run_probe(tmp_path / "probe.py", tmp_path)

    assert captured["argv"][0] == "/sentinel/python", (
        "_run_probe ignored the resolved interpreter and spawned %r"
        % captured["argv"][0]
    )


class _Ok:
    returncode = 0
    stdout = ""
    stderr = ""
