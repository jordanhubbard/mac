"""Exercise the macOS entrypoint and real shared installer with fake OS services."""

import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy/install-macos-services.sh"


@pytest.fixture
def service_host(tmp_path):
    home = tmp_path / "alternate-home"
    runtime = tmp_path / "selected-runtime"
    tools = tmp_path / "bin"
    for directory in (home, runtime, tools):
        directory.mkdir()
    data = runtime / "vectors"
    data.mkdir()
    (data / "collection-marker").write_text("preserve existing vectors")
    (runtime / "mac.env").write_text("MAC_DATABASE_URL=postgresql:///selected-authority\n")
    stub = tools / "service-stub"
    stub.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["SERVICE_CALLS"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
state = pathlib.Path(os.environ["SERVICE_STATE"])
if name == "uname":
    print(os.environ.get("SERVICE_OS", "Darwin"))
elif name in {"sleep", "plutil", "chown"}:
    pass
elif name == "podman":
    sys.exit(1)
elif name == "docker":
    if args[0] == "info":
        print("Docker fake service")
    elif args[0] != "ps":
        sys.exit(65)
elif name == "curl":
    if os.environ.get("SERVICE_HEALTH") == "failed":
        sys.exit(22)
    print('{"status":"ok"}')
elif name == "launchctl":
    if args[0] == "print":
        if args[1].endswith("/com.mac.dream-cycle"):
            mode = os.environ.get("SERVICE_DREAM", "absent")
            if mode == "active":
                print("state = running")
                sys.exit(0)
            if mode == "unknown":
                print("permission denied", file=sys.stderr)
                sys.exit(5)
        elif state.exists():
            print("state = running")
            sys.exit(0)
        print("Could not find service", file=sys.stderr)
        sys.exit(113)
    elif args[0] in {"bootstrap", "load"}:
        state.touch()
    elif args[0] in {"bootout", "unload"}:
        state.unlink(missing_ok=True)
    elif args[0] != "enable":
        sys.exit(66)
else:
    sys.exit(67)
"""
    )
    stub.chmod(0o755)
    for name in ("uname", "sleep", "plutil", "chown", "podman", "docker", "curl", "launchctl"):
        (tools / name).symlink_to(stub)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("MAC_", "QDRANT_", "COVERAGE_PROCESS"))
    }
    env.update(
        HOME=str(home),
        MAC_HOME=str(runtime),
        LOG_DIR=str(runtime / "selected-logs"),
        PATH=f"{tools}:{os.path.dirname(sys.executable)}:/usr/bin:/bin",
        WORKSPACE=str(ROOT),
        FLEET_NAME="selected",
        QDRANT_BIND_ADDR="127.0.0.1",
        QDRANT_PORT="16333",
        QDRANT_DATA_DIR=str(data),
        MAC_LAUNCHD_PYTHON_BIN=sys.executable,
        SERVICE_CALLS=str(tmp_path / "calls.jsonl"),
        SERVICE_STATE=str(tmp_path / "loaded"),
    )
    return home, runtime, env


def run_installer(env):
    return subprocess.run(
        ["bash", str(INSTALLER)], env=env, text=True, capture_output=True, timeout=40
    )


def test_selected_runtime_uses_shared_owner_and_preserves_database_and_vectors(service_host):
    home, runtime, env = service_host
    assert not (home / "Library/LaunchAgents").exists()
    result = run_installer(env)
    assert result.returncode == 0, result.stdout + result.stderr
    agents = home / "Library/LaunchAgents"
    with (agents / "com.selected.qdrant.plist").open("rb") as stream:
        plist = plistlib.load(stream)
    assert plist["ProgramArguments"] == [str(runtime / "bin/mac-qdrant-run")]
    assert plist["WorkingDirectory"] == str(runtime)
    assert plist["StandardOutPath"] == str(runtime / "selected-logs/mac-qdrant.log")
    assert not (agents / "com.mac.dream-cycle.plist").exists()
    assert "MAC_DATABASE_URL=postgresql:///selected-authority" in (runtime / "mac.env").read_text()
    assert (
        "QDRANT_DATA_DIR=" + str(runtime / "vectors")
        in (runtime / "service-env/qdrant.env").read_text()
    )
    assert (runtime / "vectors/collection-marker").read_text() == "preserve existing vectors"
    assert "Qdrant installation verified" in result.stdout
    calls = [json.loads(line) for line in Path(env["SERVICE_CALLS"]).read_text().splitlines()]
    assert any(call[0] == "curl" and "http://127.0.0.1:16333/collections" in call for call in calls)
    assert not any(call[0] == "launchctl" and call[1] in {"load", "unload"} for call in calls)


def test_failed_health_propagates_and_preserves_vector_data(service_host):
    home, runtime, env = service_host
    (home / "Library/LaunchAgents").mkdir(parents=True)
    env["SERVICE_HEALTH"] = "failed"
    result = run_installer(env)
    assert result.returncode != 0
    assert "did not become ready" in result.stderr
    assert "Qdrant installation verified" not in result.stdout
    assert "[mac] Done" not in result.stdout
    assert not (home / "Library/LaunchAgents/com.selected.qdrant.plist").exists()
    assert (runtime / "vectors/collection-marker").read_text() == "preserve existing vectors"


@pytest.mark.parametrize("existing", ["file", "active", "unknown"])
def test_existing_or_uninspectable_dream_owner_prevents_service_mutation(service_host, existing):
    home, runtime, env = service_host
    if existing == "file":
        agents = home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "com.mac.dream-cycle.plist").write_text("operator owned")
    else:
        env["SERVICE_DREAM"] = existing
    result = run_installer(env)
    assert result.returncode != 0
    assert not (runtime / "service-env").exists()
    assert not Path(env["SERVICE_STATE"]).exists()
    assert "Qdrant installation verified" not in result.stdout
    if existing == "file":
        assert (agents / "com.mac.dream-cycle.plist").read_text() == "operator owned"


def test_non_macos_entrypoint_refuses_before_service_changes(service_host):
    _, runtime, env = service_host
    env["SERVICE_OS"] = "Linux"
    result = run_installer(env)
    assert result.returncode != 0
    assert "requires macOS" in result.stderr
    assert not (runtime / "service-env").exists()
