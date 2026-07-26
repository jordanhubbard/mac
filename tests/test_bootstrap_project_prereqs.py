"""Prerequisite-check behaviour for scripts/bootstrap-project.py.

These tests cover the environment-prerequisite surface (required-command
detection, actionable diagnostics, and the non-mutating ``--check`` mode) without
ever creating or mutating a virtualenv, so they are safe to run on any host.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "bootstrap-project.py"


def _load_bootstrap() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_mac_bootstrap_project", BOOTSTRAP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def bootstrap() -> types.ModuleType:
    return _load_bootstrap()


def test_missing_commands_reports_only_absent(bootstrap, monkeypatch):
    present = {"git"}
    monkeypatch.setattr(
        bootstrap.shutil,
        "which",
        lambda name: name if name in present else None,
    )
    assert bootstrap.missing_commands(("python3", "git", "gh")) == ["python3", "gh"]


def test_report_missing_commands_includes_install_hint(bootstrap, capsys):
    bootstrap.report_missing_commands(["gh"])
    err = capsys.readouterr().err
    assert "missing required command(s): gh" in err
    assert bootstrap.COMMAND_INSTALL_HINTS["gh"] in err


def test_check_mode_passes_without_mutating_venv(bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _fail(*args, **kwargs):  # pragma: no cover - must never run under --check
        raise AssertionError("--check must not create or mutate the venv")

    monkeypatch.setattr(bootstrap, "run", _fail)
    monkeypatch.setattr(sys, "argv", ["bootstrap-project.py", "--check"])
    assert bootstrap.main() == 0


def test_check_mode_fails_with_actionable_hint_when_missing(bootstrap, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    monkeypatch.setattr(sys, "argv", ["bootstrap-project.py", "--check"])
    assert bootstrap.main() == 2
    err = capsys.readouterr().err
    assert "missing required command(s)" in err
    assert bootstrap.COMMAND_INSTALL_HINTS["python3"] in err


def test_venv_only_check_ignores_dev_tools(bootstrap, monkeypatch):
    present = {"python3"}
    monkeypatch.setattr(
        bootstrap.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in present else None,
    )
    monkeypatch.setattr(sys, "argv", ["bootstrap-project.py", "--check", "--venv-only"])
    assert bootstrap.main() == 0
