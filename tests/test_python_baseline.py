"""Keep the supported fleet interpreter and its consumers from drifting."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import tomllib

from packaging.specifiers import SpecifierSet
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / ".python-version").read_text().strip()


def bootstrap_module():
    spec = importlib.util.spec_from_file_location(
        "baseline_bootstrap", ROOT / "scripts/bootstrap-project.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_running_tests_use_reviewed_python():
    assert tuple(sys.version_info[:3]) == tuple(map(int, VERSION.split(".")))


def test_package_support_covers_baseline_but_not_older_or_next_minor():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    supported = SpecifierSet(project["requires-python"])
    assert VERSION in supported
    assert "3.13.9" not in supported
    assert "3.15.0" not in supported


@pytest.mark.parametrize("version", [(3, 11, 14), (3, 12, 11), (3, 14, 4), (3, 15, 0)])
def test_bootstrap_rejects_wrong_version_before_mutation(monkeypatch, capsys, version):
    module = bootstrap_module()
    monkeypatch.setattr(module.sys, "version_info", version)
    monkeypatch.setattr(module.sys, "argv", ["bootstrap-project.py", "--check"])
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: pytest.fail("must not mutate during version check"),
    )
    assert module.main() == 2
    assert VERSION in capsys.readouterr().err


@pytest.mark.parametrize(
    "version, supported", [("3.14.7", True), ("3.14.4", False), ("3.12.11", False)]
)
def test_bootstrap_checks_existing_venv_patch(monkeypatch, version, supported):
    module = bootstrap_module()
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, version + "\n")
    )
    assert module.venv_python_is_supported() is supported


def test_standalone_onboarding_receipts_agree_with_policy():
    for name in [
        "deploy/fleet-node-machine-onboard.py",
        "deploy/openshell/hgx-fungible-bootstrap.py",
    ]:
        tree = ast.parse((ROOT / name).read_text())
        values = {
            n.targets[0].id: ast.literal_eval(n.value)
            for n in tree.body
            if isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id in {"PYTHON_VERSION", "UV_VERSION"}
        }
        assert values["PYTHON_VERSION"] == VERSION
        assert values["UV_VERSION"] == "0.12.12"
    assets = (ROOT / "deploy/reviewed-tool-assets.sh").read_text()
    assert f'MAC_REVIEWED_PYTHON_VERSION="{VERSION}"' in assets


def test_ci_uses_project_pin_and_keeps_fault_replay():
    for name in ["ci.yml", "docs.yml", "release.yml"]:
        workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        for job in workflow["jobs"].values():
            assert "python-version" not in job.get("strategy", {}).get("matrix", {})
            for step in job.get("steps", []):
                command = step.get("run", "")
                assert not re.search(r"(?:uv python (?:install|find)|--python)\s+3\.", command), (
                    command
                )
                if "uv sync" in command:
                    assert "--locked" in command
                if "fault-replay.py" in command:
                    assert "if" not in step
    assert "scripts/fault-replay.py" in (ROOT / ".github/workflows/ci.yml").read_text()


def test_container_bases_match_pin_and_include_version_file():
    for name in ["Dockerfile", "Dockerfile.e2e", "deploy/openshell/mac-hermes.Containerfile"]:
        source = (ROOT / name).read_text()
        bases = re.findall(r"^FROM (\S*library/python\S*)", source, re.M)
        assert bases, name
        assert all(f":{VERSION}-slim-bookworm@sha256:" in base for base in bases)
        assert "COPY .python-version pyproject.toml" in source


def test_make_rejects_explicit_wrong_python():
    result = subprocess.run(
        ["make", "require-python", "PYTHON=/usr/bin/false"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert VERSION in result.stderr
