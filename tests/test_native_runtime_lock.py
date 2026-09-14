"""Native self-installs must not replace the service's locked dependencies."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from mac.worker_runtime_deps import RuntimeDepsMixin


def wheel(directory, name, version, requires=()):
    stem = name.replace("-", "_")
    info = f"{stem}-{version}.dist-info"
    entries = {
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {item}\n" for item in requires)
        ),
        f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    entries[f"{info}/RECORD"] = "".join(f"{key},,\n" for key in entries) + f"{info}/RECORD,,\n"
    path = directory / f"{stem}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


@pytest.fixture
def native_worker(tmp_path, monkeypatch):
    home = tmp_path / "mac-home"
    venv = home / "venv"
    source = home / "src" / "mac"
    source.mkdir(parents=True)
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    wheel(wheelhouse, "mac-test-core", "1.0")
    wheel(wheelhouse, "mac-test-core", "2.0")
    wheel(wheelhouse, "mac-test-tool", "1.0", ["mac-test-core==2.0"])
    monkeypatch.setenv("MAC_HOME", str(home))
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    python = venv / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "mac-test-core==1.0"],
        check=True,
        capture_output=True,
    )
    for name in ["uv.lock", "pyproject.toml"]:
        (source / name).write_text("native fixture baseline\n")
    constraints = venv / "mac-runtime-constraints.txt"
    constraints.write_text("mac-test-core==1.0\n")
    manifest = {
        "schema": "mac.native_runtime.v1",
        "python_version": sys.version.split()[0],
        "source_hashes": {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ["uv.lock", "pyproject.toml"]
        },
        "constraints_sha256": hashlib.sha256(constraints.read_bytes()).hexdigest(),
        "core_packages": {"mac-test-core": "1.0"},
    }
    (venv / "mac-runtime-lock.json").write_text(json.dumps(manifest))
    worker = object.__new__(RuntimeDepsMixin)
    worker._record_command_audit = lambda value: None
    worker._report_footprint = lambda value: None
    return worker, home, wheelhouse


@pytest.mark.parametrize("spec", ["mac-test-core==2.0", "mac-test-tool==1.0"])
def test_native_install_rejects_core_and_transitive_conflicts(native_worker, spec):
    worker, home, _ = native_worker
    result = worker.ensure_pip([spec])
    assert result["ok"] is False
    assert worker._pip_installed(worker._agent_venv_python())["mac-test-core"] == "1.0"
    assert not worker._load_footprint().get("pip")


def test_native_install_rejects_missing_baseline_before_mutation(native_worker):
    worker, home, _ = native_worker
    (home / "venv" / "mac-runtime-lock.json").unlink()
    with pytest.raises(RuntimeError, match="native runtime"):
        worker.ensure_pip(["mac-test-core==2.0"])
    assert worker._pip_installed(worker._agent_venv_python())["mac-test-core"] == "1.0"
