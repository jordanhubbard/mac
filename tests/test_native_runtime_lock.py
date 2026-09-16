"""Native self-installs must not replace the service's locked dependencies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
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
    (source / ".python-version").write_text(sys.version.split()[0] + "\n")
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


def test_native_install_retains_compatible_tool_and_records_it(native_worker):
    worker, home, wheels = native_worker
    wheel(wheels, "mac-test-compatible", "1.0", ["mac-test-core==1.0"])
    result = worker.ensure_pip(["mac-test-compatible==1.0"])
    assert result["ok"], result
    assert worker._pip_installed(worker._agent_venv_python())["mac-test-core"] == "1.0"
    assert worker._load_footprint()["pip"][0]["name"] == "mac-test-compatible"
    assert worker.ensure_pip(["mac-test-compatible==1.0"])["skipped"] == "already satisfied"


@pytest.mark.parametrize(
    "damage", ["source", "constraints", "core", "python", "python_baseline", "empty_core"]
)
def test_native_fast_path_rejects_drift(native_worker, damage):
    worker, home, _ = native_worker
    manifest_path = home / "venv" / "mac-runtime-lock.json"
    manifest = json.loads(manifest_path.read_text())
    if damage == "source":
        (home / "src/mac/uv.lock").write_text("changed\n")
    elif damage == "python_baseline":
        (home / "src/mac/.python-version").write_text("0.0.0\n")
    elif damage == "constraints":
        (home / "venv/mac-runtime-constraints.txt").write_text("mac-test-core==2.0\n")
    else:
        if damage == "core":
            manifest["core_packages"]["mac-test-core"] = "2.0"
        elif damage == "python":
            manifest["python_version"] = "0.0.0"
        else:
            manifest["core_packages"] = {}
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="native runtime"):
        worker.ensure_pip(["mac-test-core==1.0"])
    assert not worker._load_footprint().get("pip")


@pytest.fixture
def locked_project(tmp_path, monkeypatch):
    import shutil

    uv = shutil.which("uv")
    if not uv:
        pytest.skip("installer integration requires the fleet's reviewed uv")
    home = tmp_path / "installed"
    source = home / "src/mac"
    source.mkdir(parents=True)
    wheels = tmp_path / "wheelhouse"
    wheels.mkdir()
    for name, version, dependencies in [
        ("mac-test-core", "1.0", []),
        ("mac-test-core", "2.0", []),
        ("mac-test-postgres", "1.0", []),
        ("mac-test-platform", "1.0", []),
        ("mac-test-platform", "2.0", []),
        ("mac-test-unrecorded", "1.0", ["mac-test-core==1.0"]),
        ("mac-test-recorded", "1.0", ["mac-test-core==1.0"]),
        ("mac-test-conflict", "1.0", ["mac-test-core==2.0"]),
    ]:
        wheel(wheels, name, version, dependencies)
    (source / "pyproject.toml").write_text(
        '[project]\nname="mac-native-fixture"\nversion="1.0"\n'
        'requires-python=">=3.14"\ndependencies=[]\n'
        '[project.optional-dependencies]\nrelay=["mac-test-core==1.0", "mac-test-platform==2.0; sys_platform == \'win32\'"]\npostgres=["mac-test-postgres==1.0"]\n'
    )
    (source / ".python-version").write_text(sys.version.split()[0] + "\n")
    for name in ("PIP_FIND_LINKS", "UV_FIND_LINKS"):
        monkeypatch.setenv(name, str(wheels))
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("UV_NO_INDEX", "1")
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    subprocess.run(
        [uv, "lock", "--python", sys.executable], cwd=source, check=True, capture_output=True
    )
    snapshot = home / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "packages": [
                    {"name": "mac-test-unrecorded", "version": "1.0"},
                    {"name": "mac-test-platform", "version": "1.0"},
                ],
                "footprint": {
                    "pip": [{"name": "mac-test-recorded", "spec": "mac-test-recorded==1.0"}]
                },
            }
        )
    )
    return home, source, snapshot, uv


def test_real_locked_install_preserves_recorded_and_unrecorded_tools_on_repeat(locked_project):
    from mac.native_runtime import install, inventory

    home, source, snapshot, uv = locked_project
    before = (source / "uv.lock").read_bytes()
    first = install(source, home / "venv", snapshot, home / "agent-footprint.json", uv)
    second = install(source, home / "venv", snapshot, home / "agent-footprint.json", uv)
    assert (
        first["core_packages"]
        == second["core_packages"]
        == {"mac-test-core": "1.0", "mac-test-postgres": "1.0"}
    )
    installed = inventory(home / "venv/bin/python")
    assert installed["mac-test-platform"] == "1.0"
    assert (
        json.loads((home / "agent-footprint.json").read_text())["pip"][0]["name"]
        == "mac-test-recorded"
    )
    assert {
        name: installed[name]
        for name in ("mac-test-core", "mac-test-unrecorded", "mac-test-recorded")
    } == {
        "mac-test-core": "1.0",
        "mac-test-unrecorded": "1.0",
        "mac-test-recorded": "1.0",
    }
    assert (source / "uv.lock").read_bytes() == before
    assert (home / "venv/mac-runtime-lock.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("recorded", [False, True])
def test_real_locked_install_refuses_incompatible_restored_tool(locked_project, recorded):
    from mac.native_runtime import install, inventory

    home, source, snapshot, uv = locked_project
    snapshot.write_text(
        json.dumps(
            {
                "packages": [] if recorded else [{"name": "mac-test-conflict", "version": "1.0"}],
                "footprint": {"pip": [{"spec": "mac-test-conflict==1.0"}] if recorded else []},
            }
        )
    )
    with pytest.raises(RuntimeError, match="native runtime .* failed"):
        install(source, home / "venv", snapshot, home / "agent-footprint.json", uv)
    assert inventory(home / "venv/bin/python")["mac-test-core"] == "1.0"


def test_deployment_capture_reads_real_old_venv_before_rename(native_worker, tmp_path):
    import shlex

    worker, home, wheels = native_worker
    wheel(wheels, "mac-test-compatible", "1.0", ["mac-test-core==1.0"])
    assert worker.ensure_pip(["mac-test-compatible==1.0"])["ok"]
    script = (Path(__file__).resolve().parents[1] / "deploy/fleet-node-install.sh").read_text()
    function = (
        "capture_native_runtime() {"
        + script.split("capture_native_runtime() {", 1)[1].split(
            "\ninstall_agent_footprint() {", 1
        )[0]
    )
    log = home / "logs"
    log.mkdir()
    env_file = home / "mac.env"
    env_file.write_text("")
    values = {
        "PY": sys.executable,
        "VENV": str(home / "venv"),
        "MAC_HOME": str(home),
        "ENV_FILE": str(env_file),
        "LOG_DIR": str(log),
        "DEPLOY_TS": "test",
    }
    shell = "set -eu\n" + "\n".join(
        name + "=" + shlex.quote(value) for name, value in values.items()
    )
    result = subprocess.run(
        ["bash", "-c", shell + "\ndie() { exit 1; }\n" + function + "\ncapture_native_runtime\n"],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    saved_path = log / "native-runtime-packages-test.json"
    saved = json.loads(saved_path.read_text())
    assert {p["name"] for p in saved["packages"]} >= {"mac-test-core", "mac-test-compatible"}
    assert saved["footprint"]["pip"][0]["name"] == "mac-test-compatible"
    assert saved_path.stat().st_mode & 0o777 == 0o600
    assert script.index("\ncapture_native_runtime\n") < script.index(
        "\nbackup_existing_artifacts\n"
    )


def test_broken_native_venv_does_not_redirect_install_to_callers_python(native_worker):
    worker, home, _ = native_worker
    (home / "venv/bin/python").unlink()
    assert worker._agent_venv_python() == str(home / "venv/bin/python")
    with pytest.raises(RuntimeError, match="native runtime"):
        worker.ensure_pip(["mac-test-core==2.0"])
