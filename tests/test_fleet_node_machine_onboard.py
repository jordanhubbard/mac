from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "fleet-node-machine-onboard.py"
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"


def _load_helper():
    spec = importlib.util.spec_from_file_location("fleet_node_machine_onboard", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _archive(path: Path) -> Path:
    with tarfile.open(path, "w:gz") as bundle:
        for name, body, mode in (
            ("pyproject.toml", b"[project]\nname='mac'\nversion='0'\n", 0o644),
            ("src/mac/__init__.py", b"", 0o644),
            ("scripts/tool", b"#!/bin/sh\n", 0o755),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = mode
            bundle.addfile(info, io.BytesIO(body))
    return path


def _private_json(module, path: Path, value: dict) -> Path:
    module._atomic_private_json(path, value)
    return path


def _route(module, path: Path) -> Path:
    return _private_json(
        module,
        path,
        {
            "schema": module.ROUTE_SCHEMA,
            "adapter": "ssh-machine",
            "authority": {
                "ssh_host_key_sha256": "a" * 64,
                "instance_id_kind": "machine-id",
                "instance_id_sha256": "b" * 64,
            },
            "observation": {},
        },
    )


def _fake_toolchain(module, stage: Path):
    uv = stage / "tools" / "uv"
    python = stage / "python" / "cpython-3.14.7-test" / "bin" / "python3.14"
    for executable in (uv, python):
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    return uv, python


@pytest.fixture()
def module(monkeypatch):
    value = _load_helper()
    monkeypatch.setattr(value, "_service_configuration_paths", lambda _layout: [])
    monkeypatch.setattr(value, "_service_processes", lambda _supervisor: [])
    return value


def test_pristine_gate_rejects_every_deployed_artifact(module, tmp_path):
    layout = module.Layout.for_home(tmp_path)
    assert all(module.validate_pristine(layout, "supervisord").values())

    blockers = (
        layout.source,
        layout.venv,
        layout.mac_home / "deployed-source-revision",
    )
    for path in blockers:
        if path.name in {"mac", "venv"}:
            path.mkdir(parents=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("0" * 40, encoding="ascii")
        with pytest.raises(module.OnboardingError, match="failed-prephase"):
            module.validate_pristine(layout, "supervisord")
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()

    env = layout.mac_home / "mac.env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("export MAC_WORKER_DEPLOY_GENERATION='old'\n", encoding="utf-8")
    with pytest.raises(module.OnboardingError, match="worker_generation"):
        module.validate_pristine(layout, "supervisord")


def test_pristine_gate_rejects_service_configuration_and_process(module, tmp_path, monkeypatch):
    layout = module.Layout.for_home(tmp_path)
    monkeypatch.setattr(
        module,
        "_service_configuration_paths",
        lambda _layout: [tmp_path / "mac-worker.service"],
    )
    monkeypatch.setattr(module, "_service_processes", lambda _supervisor: ["mac-worker"])
    with pytest.raises(module.OnboardingError) as error:
        module.validate_pristine(layout, "systemd")
    assert "service_configuration" in str(error.value)
    assert "service_process" in str(error.value)


def test_service_configuration_allows_network_prerequisite(monkeypatch):
    module = _load_helper()
    paths = {
        Path("/etc/supervisor/conf.d/mac-tailscaled.conf"),
        Path("/etc/supervisor/conf.d/mac-agent.conf"),
    }

    monkeypatch.setattr(
        module.Path,
        "glob",
        lambda self, pattern: (
            list(paths) if self == Path("/etc/supervisor/conf.d") and pattern == "mac*.conf" else []
        ),
    )
    monkeypatch.setattr(module, "_path_exists", lambda path: path in paths)

    assert module._service_configuration_paths(module.Layout.for_home(Path("/home/test"))) == [
        Path("/etc/supervisor/conf.d/mac-agent.conf")
    ]


def test_prepare_is_generation_scoped_and_does_not_publish(module, tmp_path, monkeypatch):
    layout = module.Layout.for_home(tmp_path / "home")
    archive = _archive(tmp_path / "mac.tar.gz")
    assets = tmp_path / "reviewed-tool-assets.sh"
    assets.write_text("# reviewed\n", encoding="utf-8")
    route = _route(module, tmp_path / "route.json")
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setattr(
        module,
        "install_reviewed_toolchain",
        lambda stage, _assets, _cache: _fake_toolchain(module, stage),
    )
    monkeypatch.setattr(module, "_trusted_gh", lambda: gh)

    receipt = module.prepare(
        layout,
        generation="onboard:test",
        agent="worker4",
        source_revision="1" * 40,
        supervisor="supervisord",
        archive=archive,
        reviewed_assets=assets,
        route_identity=route,
    )

    stage = layout.stage("onboard:test")
    assert receipt["status"] == "prepared"
    assert receipt["versions"] == {
        "uv": "0.12.12",
        "python": "3.14.7",
    }
    assert (stage / "source" / "pyproject.toml").is_file()
    assert stat.S_IMODE((stage / "stage.json").stat().st_mode) == 0o600
    assert not layout.source.exists()
    assert not layout.venv.exists()
    assert not layout.receipt.exists()


def _prepared(module, tmp_path: Path, monkeypatch):
    layout = module.Layout.for_home(tmp_path / "home")
    archive = _archive(tmp_path / "mac.tar.gz")
    assets = tmp_path / "reviewed-tool-assets.sh"
    assets.write_text("# reviewed\n", encoding="utf-8")
    route = _route(module, tmp_path / "route.json")
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setattr(
        module,
        "install_reviewed_toolchain",
        lambda stage, _assets, _cache: _fake_toolchain(module, stage),
    )
    monkeypatch.setattr(module, "_trusted_gh", lambda: gh)
    stage = module.prepare(
        layout,
        generation="onboard:test",
        agent="worker4",
        source_revision="1" * 40,
        supervisor="supervisord",
        archive=archive,
        reviewed_assets=assets,
        route_identity=route,
    )
    placeholder = _private_json(
        module,
        tmp_path / "placeholder.json",
        {
            "schema": module.PLACEHOLDER_SCHEMA,
            "agent": "worker4",
            "agent_id": "agent_worker4",
            "generation": "onboard:test",
            "source_revision": "1" * 40,
            "route_identity_sha256": stage["route_identity_sha256"],
            "instance_kind": "fungible",
            "status": "draining",
            "health_status": "degraded",
        },
    )
    return layout, placeholder


@pytest.fixture
def real_onboarding(module, tmp_path, monkeypatch):
    """Use real uv, managed Python, wheels and console scripts without a registry."""
    uv = shutil.which("uv")
    assert uv is not None
    assert sys.version.split()[0] == module.PYTHON_VERSION
    source = tmp_path / "fixture-source"
    source.mkdir()
    wheels = tmp_path / "wheels"
    wheels.mkdir()

    def dependency(name, version):
        stem = name.replace("-", "_")
        info = f"{stem}-{version}.dist-info"
        entries = {
            f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        }
        entries[f"{info}/RECORD"] = "".join(f"{key},,\n" for key in entries)
        with zipfile.ZipFile(wheels / f"{stem}-{version}-py3-none-any.whl", "w") as archive:
            for name, body in entries.items():
                archive.writestr(name, body)

    dependency("mac-onboard-core", "1.0")
    dependency("mac-onboard-postgres", "1.0")
    (source / "pyproject.toml").write_text(
        '[project]\nname="mac"\nversion="0.0.0"\nrequires-python=">=3.14,<3.15"\n'
        '[project.scripts]\nmac="onboard_fixture:main"\n'
        '[project.optional-dependencies]\nrelay=["mac-onboard-core>=1"]\n'
        'postgres=["mac-onboard-postgres==1.0"]\n'
        '[build-system]\nrequires=[]\nbuild-backend="fixture_build"\nbackend-path=["."]\n'
    )
    (source / ".python-version").write_text(module.PYTHON_VERSION + "\n")
    (source / "src/mac").mkdir(parents=True)
    (source / "src/mac/__init__.py").write_text("")
    (source / "fixture_build.py").write_text('''from pathlib import Path
import tomllib
import zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    project = tomllib.loads((Path(__file__).parent / "pyproject.toml").read_text())["project"]
    info = "mac-0.0.0.dist-info"
    metadata = "Metadata-Version: 2.1\\nName: mac\\nVersion: 0.0.0\\nRequires-Python: >=3.14,<3.15\\n"
    for extra, requirements in project["optional-dependencies"].items():
        metadata += f"Provides-Extra: {extra}\\n"
        for requirement in requirements:
            metadata += f"Requires-Dist: {requirement}; extra == '{extra}'\\n"
    entries = {
        info + "/METADATA": metadata,
        info + "/WHEEL": "Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
        info + "/entry_points.txt": "[console_scripts]\\nmac = onboard_fixture:main\\n",
        "onboard_fixture.py": "import json,sys\\nfrom importlib.metadata import version\\ndef main():\\n print(json.dumps({'core':version('mac-onboard-core'),'postgres':version('mac-onboard-postgres'),'prefix':sys.prefix}))\\n",
    }
    entries[info + "/RECORD"] = "".join(f"{key},,\\n" for key in entries)
    name = "mac-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(Path(wheel_directory) / name, "w") as wheel:
        for path, body in entries.items():
            wheel.writestr(path, body)
    return name
''')
    offline = {
        "UV_NO_INDEX": "1",
        "UV_FIND_LINKS": str(wheels),
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "UV_PYTHON_DOWNLOADS": "never",
    }
    subprocess.run(
        [uv, "lock", "--python", sys.executable, "--project", str(source)],
        env={**os.environ, **offline}, check=True, capture_output=True, text=True,
    )
    # A range-based reinstall can now choose 2.0, while the accepted lock names 1.0.
    dependency("mac-onboard-core", "2.0")
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for entry in source.iterdir():
            bundle.add(entry, arcname=entry.name)

    def real_toolchain(stage, _assets, _cache):
        staged_uv = stage / "tools/uv"
        staged_uv.parent.mkdir(parents=True)
        shutil.copy2(uv, staged_uv)
        runtime = Path(sys.base_prefix)
        staged_runtime = stage / "python" / runtime.name
        shutil.copytree(runtime, staged_runtime, symlinks=True)
        return staged_uv, staged_runtime / "bin/python3.14"

    run = module._run
    monkeypatch.setattr(module, "_run", lambda argv, *, env=None, timeout=900: run(
        argv, env={**(env if env is not None else os.environ), **offline}, timeout=timeout
    ))
    monkeypatch.setattr(module, "install_reviewed_toolchain", real_toolchain)
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    monkeypatch.setattr(module, "_trusted_gh", lambda: gh)
    assets = tmp_path / "reviewed-assets.sh"
    assets.write_text("# toolchain supplied by the real managed-runtime fixture\n")
    layout = module.Layout.for_home(tmp_path / "home")
    generation = "onboard:real"
    stage = module.prepare(
        layout, generation=generation, agent="worker4", source_revision="1" * 40,
        supervisor="supervisord", archive=archive, reviewed_assets=assets,
        route_identity=_route(module, tmp_path / "route.json"),
    )
    placeholder = _private_json(module, tmp_path / "placeholder.json", {
        "schema": module.PLACEHOLDER_SCHEMA, "agent": "worker4", "agent_id": "agent_worker4",
        "generation": generation, "source_revision": "1" * 40,
        "route_identity_sha256": stage["route_identity_sha256"], "instance_kind": "fungible",
        "status": "draining", "health_status": "degraded",
    })
    return layout, generation, placeholder


def test_pristine_install_uses_lock_and_relocated_cli(module, real_onboarding):
    layout, generation, placeholder = real_onboarding
    lock = (layout.stage(generation) / "source/uv.lock").read_bytes()
    receipt = module.commit(
        layout, generation=generation, agent="worker4", source_revision="1" * 40,
        supervisor="supervisord", placeholder=placeholder,
    )
    assert receipt["services_started"] is False
    assert not layout.stage(generation).exists()
    result = subprocess.run([str(layout.mac_bin), "--help"], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {
        "core": "1.0", "postgres": "1.0", "prefix": str(layout.venv),
    }
    assert (layout.source / "uv.lock").read_bytes() == lock


@pytest.mark.parametrize("damage", ["missing_lock", "stale_lock"])
def test_pristine_install_refuses_invalid_lock_and_compensates(module, real_onboarding, damage):
    layout, generation, placeholder = real_onboarding
    source = layout.stage(generation) / "source"
    if damage == "missing_lock":
        (source / "uv.lock").unlink()
    else:
        project = source / "pyproject.toml"
        project.write_text(project.read_text().replace("mac-onboard-core>=1", "mac-onboard-core==2.0"))
    with pytest.raises(module.OnboardingError, match="command failed"):
        module.commit(
            layout, generation=generation, agent="worker4", source_revision="1" * 40,
            supervisor="supervisord", placeholder=placeholder,
        )
    assert not layout.source.exists()
    assert not layout.venv.exists()
    assert not (layout.mac_home / "lib/python").exists()
    assert not layout.receipt.exists()


def test_commit_publishes_complete_baseline_and_owner_private_receipt(
    module, tmp_path, monkeypatch
):
    layout, placeholder = _prepared(module, tmp_path, monkeypatch)
    commands: list[list[str]] = []

    def fake_run(argv, *, env=None, timeout=900):
        del env, timeout
        args = [str(item) for item in argv]
        commands.append(args)
        if "venv" in args:
            target = Path(args[-1])
            (target / "bin").mkdir(parents=True)
            for name in ("python", "mac"):
                executable = target / "bin" / name
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
        if args[0].endswith("/python") and "-c" in args:
            return subprocess.CompletedProcess(args, 0, "3.14.7\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(module, "_run", fake_run)
    receipt = module.commit(
        layout,
        generation="onboard:test",
        agent="worker4",
        source_revision="1" * 40,
        supervisor="supervisord",
        placeholder=placeholder,
    )

    assert receipt["status"] == "published"
    assert receipt["services_started"] is False
    assert receipt["barrier"] == {"status": "draining", "health_status": "degraded"}
    assert layout.source.is_dir() and not layout.source.is_symlink()
    assert layout.venv.is_dir() and not layout.venv.is_symlink()
    assert layout.mac_bin.readlink() == layout.venv / "bin" / "mac"
    assert layout.gh_bin.is_symlink()
    assert stat.S_IMODE(layout.receipt.stat().st_mode) == 0o600
    assert all("start" not in command and "restart" not in command for command in commands)


def test_failed_commit_compensates_to_source_and_venv_absent(module, tmp_path, monkeypatch):
    layout, placeholder = _prepared(module, tmp_path, monkeypatch)

    def fail_package_install(argv, *, env=None, timeout=900):
        del env, timeout
        args = [str(item) for item in argv]
        if "venv" in args:
            target = Path(args[-1])
            (target / "bin").mkdir(parents=True)
            (target / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        if "pip" in args:
            raise module.OnboardingError("simulated package failure")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(module, "_run", fail_package_install)
    with pytest.raises(module.OnboardingError, match="simulated package failure"):
        module.commit(
            layout,
            generation="onboard:test",
            agent="worker4",
            source_revision="1" * 40,
            supervisor="supervisord",
            placeholder=placeholder,
        )

    assert not layout.source.exists()
    assert not layout.venv.exists()
    assert not (layout.mac_home / "lib" / "python").exists()
    assert not layout.receipt.exists()


def test_aborted_cohort_journal_is_preserved_while_precohort_receipt_commits(
    module, tmp_path, monkeypatch
):
    transaction = (
        tmp_path
        / "home"
        / ".mac"
        / "fleet-cohort-transactions"
        / "transaction-052a-diagnostic.json"
    )
    transaction.parent.mkdir(parents=True)
    sentinel = b'{"schema":"mac.fleet_cohort_transaction.v1","status":"aborted"}\n'
    transaction.write_bytes(sentinel)
    transaction.chmod(0o600)
    layout, placeholder = _prepared(module, tmp_path, monkeypatch)

    def fake_run(argv, *, env=None, timeout=900):
        del env, timeout
        args = [str(item) for item in argv]
        if "venv" in args:
            target = Path(args[-1])
            (target / "bin").mkdir(parents=True)
            for name in ("python", "mac"):
                executable = target / "bin" / name
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
        if args[0].endswith("/python") and "-c" in args:
            return subprocess.CompletedProcess(args, 0, "3.14.7\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(module, "_run", fake_run)
    module.commit(
        layout,
        generation="onboard:test",
        agent="worker4",
        source_revision="1" * 40,
        supervisor="supervisord",
        placeholder=placeholder,
    )

    assert transaction.read_bytes() == sentinel
    assert stat.S_IMODE(transaction.stat().st_mode) == 0o600
    assert layout.receipt.is_file()
    assert module._private_json(layout.receipt, module.RECEIPT_SCHEMA)["status"] == ("published")


def test_controller_exposes_precohort_mode_without_weakening_typed_deploy():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "--prepare-fungible-onboarding" in text
    assert 'text_field(agent.get("instance_kind") or "static")' in text
    assert 'instance_kind="${fields[55]:-static}"' in text
    assert "bind_precohort_routes" in text
    assert '"instance_kind":"fungible"' in text
    assert '"status":"draining"' in text
    assert '"health_status":"degraded"' in text
    assert "prepare_fungible_machine_onboarding" in text
    assert "run_typed_cohort" in text
    assert "no services started and no cohort transaction was opened" in text
    assert (
        '&& [ "$PREPARE_FUNGIBLE_ONBOARDING" != 1 ]; then\n'
        "    recover_incomplete_cohort_transaction_before_deploy"
    ) in text


def test_controller_counts_zero_preparation_modes_without_pipefail_exit():
    text = DEPLOY.read_text(encoding="utf-8")
    counter = text.split("preparation_mode_count=$((", 1)[1].split("))", 1)[0]
    assert "PREPARE_REVIEWED_OPENSHELL_CLI" in counter
    assert "PREPARE_NETWORK_PREREQUISITES" in counter
    assert "PREPARE_FUNGIBLE_ONBOARDING" in counter
    assert "wc -l" not in counter

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -euo pipefail\n"
                "PREPARE_REVIEWED_OPENSHELL_CLI=0\n"
                "PREPARE_NETWORK_PREREQUISITES=0\n"
                "PREPARE_FUNGIBLE_ONBOARDING=0\n"
                f"preparation_mode_count=$(({counter}))\n"
                'printf "%s\\n" "$preparation_mode_count"\n'
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0\n"


def test_node_installer_reports_only_structural_error_context():
    text = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    prefix = text.split('RECOVERY_POLICY="${MAC_DEPLOY_RECOVERY_POLICY', 1)[0]
    assert "set -euo pipefail" in prefix
    assert "set -E" in prefix
    assert "trap deploy_structural_error ERR" in prefix
    reporter = prefix.split("deploy_structural_error() {", 1)[1]
    assert "NODE_ACTION" in reporter
    assert "FUNCNAME[1]" in reporter
    assert "BASH_LINENO[0]" in reporter
    assert '"$BASH_COMMAND"' not in reporter
    assert "env" not in reporter.lower()


def test_deployment_runtime_includes_postgres_extra():
    onboard = HELPER.read_text(encoding="utf-8")
    # Pristine onboarding has its own bootstrap. The native deployment helper's
    # actual relay/postgres installation is covered by test_native_runtime_lock.
    assert "[relay,postgres]" in onboard


def test_node_installer_prefers_phase_zero_managed_python(tmp_path):
    text = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    function = (
        "python_bin() {"
        + text.split("python_bin() {", 1)[1].split("\n}\n\nhermes_python_bin()", 1)[0]
        + "\n}"
    )
    mac_home = tmp_path / ".mac"
    managed = mac_home / "lib" / "python" / "cpython-3.14.7-test" / "bin" / "python3.14"
    managed.parent.mkdir(parents=True)
    managed.symlink_to(Path(sys.executable))
    system_bin = tmp_path / "system-bin"
    system_bin.mkdir()
    system_python = system_bin / "python3"
    system_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    system_python.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -euo pipefail\n"
                'MAC_HOME="$1"\n'
                'VENV="$MAC_HOME/venv"\n'
                'PATH="$2:/usr/bin:/bin"\n'
                "MAC_PYTHON=\n"
                "MAC_REVIEWED_PYTHON_VERSION=3.14.7\n"
                "log() { :; }\n"
                f"{function}\n"
                "python_bin\n"
            ),
            "managed-python",
            str(mac_home),
            str(system_bin),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(Path(sys.executable).resolve())
