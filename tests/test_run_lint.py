"""make lint and make lint-fix are a diagnose/apply pair, including format.

A gate that does not check format cannot warn before lint-fix rewrites the
tree. That failure already happened: lint was green, lint-fix reformatted
hundreds of files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run-lint.sh"


def _case_branch(text: str, label: str, next_label: str) -> str:
    start = text.index(label)
    end = text.index(next_label, start + 1)
    return text[start:end]


def test_lint_script_exists():
    assert SCRIPT.is_file()


def test_lint_reports_format_drift():
    """The no-arg path is make lint. It must run the same format checker as --format-check."""
    branch = _case_branch(SCRIPT.read_text(encoding="utf-8"), '    "")', "    *)")
    assert "ruff check ." in branch
    assert "ruff format --check" in branch


def test_lint_fix_applies_the_same_tools():
    branch = _case_branch(
        SCRIPT.read_text(encoding="utf-8"),
        "    --fix)",
        "    --format-check)",
    )
    assert "ruff check --fix" in branch
    assert "ruff format ." in branch


@pytest.mark.parametrize("mode", [[], ["--fix"], ["--format-check"]])
@pytest.mark.parametrize(
    "mac_venv,uv_environment,expected",
    [
        (None, None, None),
        ("", "configured env", "configured env"),
        ("relative env", "ignored env", "relative env"),
        ("/tmp/custom env", None, "/tmp/custom env"),
        ("~/custom env", "ignored env", "HOME/custom env"),
        ("literal-$(do-not-execute)", None, "literal-$(do-not-execute)"),
    ],
)
def test_lint_uses_the_prepared_environment(tmp_path, mode, mac_venv, uv_environment, expected):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    entrypoint = scripts / "run-lint.sh"
    entrypoint.write_text(SCRIPT.read_text())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    uv = binaries / "uv"
    uv.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "print(json.dumps({'environment':os.environ.get('UV_PROJECT_ENVIRONMENT'),"
        "'arguments':sys.argv[1:]}))\n"
    )
    uv.chmod(0o755)
    env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"], HOME=str(tmp_path))
    for key, value in [("MAC_VENV", mac_venv), ("UV_PROJECT_ENVIRONMENT", uv_environment)]:
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    result = subprocess.run(
        ["bash", str(entrypoint), *mode], env=env, capture_output=True, text=True, check=True
    )
    calls = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert len(calls) == (1 if mode == ["--format-check"] else 2)
    selected = expected.replace("HOME/", str(tmp_path) + "/") if expected else expected
    assert all(call["environment"] == selected for call in calls)
    assert all(call["arguments"][:3] == ["run", "--no-sync", "ruff"] for call in calls)
