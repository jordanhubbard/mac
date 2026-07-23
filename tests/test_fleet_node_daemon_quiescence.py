"""Behavioral contract for node-local daemon resource quiescence.

The fleet installer is intentionally a single transportable shell script.  The
daemon-resource block has stable markers so these tests can execute the exact
production functions without running an entire node deployment.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import time
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE_INSTALL_SCRIPT = ROOT / "deploy" / "fleet-node-install.sh"
BLOCK_BEGIN = "# BEGIN MAC DAEMON RESOURCE QUIESCENCE"
BLOCK_END = "# END MAC DAEMON RESOURCE QUIESCENCE"
FUNCTION = "quiesce_daemon_resources_before_source_replacement"
SCHEMA = "mac.daemon_resource_quiescence.v1"
GENERATION = "42"
REVISION = "a" * 40
SANDBOX = "mac-openclaw-agent-test"
SECRET = "router-secret-must-never-be-replayed"


def _installer_text() -> str:
    return NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")


def _quiescence_block() -> str:
    text = _installer_text()
    assert text.count(BLOCK_BEGIN) == 1, (
        f"{NODE_INSTALL_SCRIPT} must contain exactly one {BLOCK_BEGIN!r} marker"
    )
    assert text.count(BLOCK_END) == 1, (
        f"{NODE_INSTALL_SCRIPT} must contain exactly one {BLOCK_END!r} marker"
    )
    before, remainder = text.split(BLOCK_BEGIN, 1)
    block, after = remainder.split(BLOCK_END, 1)
    assert before and after
    assert f"{FUNCTION}()" in block
    return BLOCK_BEGIN + block + BLOCK_END


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def _fake_openshell_source() -> str:
    return f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys
import time

for forbidden in ("MAC_API_TOKEN", "GH_TOKEN", "OPENAI_API_KEY", "SLACK_BOT_TOKEN"):
    if forbidden in os.environ:
        raise SystemExit("deploy credential reached OpenShell subprocess: " + forbidden)

calls = Path(os.environ["FAKE_DAEMON_CALLS"])
state_path = Path(os.environ["FAKE_OPENSHELL_STATE"])
mode = os.environ.get("FAKE_OPENSHELL_MODE", "active")
sandbox = os.environ["FAKE_SANDBOX_NAME"]
with calls.open("a", encoding="utf-8") as stream:
    stream.write("openshell:" + " ".join(sys.argv[1:]) + "\\n")

state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
if len(args) >= 2 and args[0:2] == ["sandbox", "list"]:
    offset = int(args[args.index("--offset") + 1]) if "--offset" in args else 0
    if mode == "timeout":
        time.sleep(5)
    if mode == "nonzero":
        print(os.environ["FAKE_SECRET"], file=sys.stderr)
        raise SystemExit(70)
    if mode == "malformed":
        print("not-json " + os.environ["FAKE_SECRET"])
        raise SystemExit(0)
    if mode == "duplicate":
        print(json.dumps([{{"name": sandbox}}, {{"name": sandbox}}]))
        raise SystemExit(0)
    if mode == "second-page":
        if offset == 0:
            print(json.dumps([{{"name": "unrelated-%04d" % index}} for index in range(1000)]))
        else:
            print(json.dumps([{{"name": sandbox}}] if state.get("present") else []))
        raise SystemExit(0)
    if state.get("delete_seen"):
        state["lists_after_delete"] = state.get("lists_after_delete", 0) + 1
        if mode == "delayed-delete" and state["lists_after_delete"] >= 3:
            state["present"] = False
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps([{{"name": sandbox}}] if state.get("present") else []))
    raise SystemExit(0)

if len(args) >= 3 and args[0:2] == ["sandbox", "delete"]:
    if args[2] != sandbox:
        raise SystemExit(65)
    state["delete_seen"] = True
    if mode == "delete-error-absent":
        state["present"] = False
        state_path.write_text(json.dumps(state), encoding="utf-8")
        raise SystemExit(9)
    if mode not in {{"persistent-delete", "delayed-delete"}}:
        state["present"] = False
    state_path.write_text(json.dumps(state), encoding="utf-8")
    raise SystemExit(0)

print("unexpected openshell invocation", file=sys.stderr)
raise SystemExit(64)
"""


def _fake_stop_wrapper_source() -> str:
    return f"""#!{sys.executable}
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

for forbidden in ("MAC_API_TOKEN", "GH_TOKEN", "OPENAI_API_KEY", "SLACK_BOT_TOKEN"):
    if forbidden in os.environ:
        raise SystemExit("deploy credential reached stop-wrapper subprocess: " + forbidden)

with Path(os.environ["FAKE_DAEMON_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write("openclaw-stop:" + " ".join(sys.argv[1:]) + "\\n")
mode = os.environ.get("FAKE_STOP_WRAPPER_MODE", "success")
if mode == "timeout":
    time.sleep(5)
if mode == "child-timeout":
    child_source = '''
import os
from pathlib import Path
import signal
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ["FAKE_CHILD_PID"]).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
'''
    subprocess.Popen([sys.executable, "-c", child_source])
    time.sleep(5)
if mode == "failure":
    print(os.environ["FAKE_SECRET"], file=sys.stderr)
    raise SystemExit(23)
raise SystemExit(0)
"""


def _fake_runtime_source(runtime: str) -> str:
    return f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

runtime = {runtime!r}
for forbidden in ("MAC_API_TOKEN", "GH_TOKEN", "OPENAI_API_KEY", "SLACK_BOT_TOKEN"):
    if forbidden in os.environ:
        raise SystemExit("deploy credential reached container runtime: " + forbidden)
if "SSH_AUTH_SOCK" in os.environ:
    raise SystemExit("SSH agent capability reached container runtime")
if runtime == "docker":
    docker_config = Path(os.environ["DOCKER_CONFIG"])
    docker_auth = docker_config / "config.json"
    if docker_auth.exists() and os.environ["FAKE_SECRET"] in docker_auth.read_text(encoding="utf-8"):
        raise SystemExit("Docker registry credentials reached container runtime")
calls = Path(os.environ["FAKE_DAEMON_CALLS"])
state_path = Path(os.environ["FAKE_{{}}_STATE".format(runtime.upper())])
mode = os.environ.get("FAKE_{{}}_MODE".format(runtime.upper()), "normal")
with calls.open("a", encoding="utf-8") as stream:
    stream.write(runtime + ":" + " ".join(sys.argv[1:]) + "\\n")
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]

if runtime == "docker" and args[0:2] == ["context", "ls"]:
    names = ["default", "secondary"] if mode == "two-contexts" else ["default"]
    for name in names:
        print(json.dumps({{"Name": name}}))
    raise SystemExit(0)
if runtime == "docker" and args[0:2] == ["context", "inspect"]:
    suffix = "-secondary" if args[2] == "secondary" else ""
    host = "unix://" + str(state_path.parent / ("docker" + suffix + ".sock"))
    if mode == "loopback-tcp":
        host = "tcp://127.0.0.1:2375"
    elif mode == "loopback-ssh":
        host = "ssh://operator@localhost:22"
    print(json.dumps([{{
        "Name": args[2],
        "Endpoints": {{"docker": {{"Host": host}}}},
    }}]))
    raise SystemExit(0)
if runtime == "docker" and len(args) >= 2 and args[0] == "--context":
    args = args[2:]
if runtime == "podman" and args[0:3] == ["system", "connection", "list"]:
    if mode == "machine-ssh":
        print(json.dumps([{{
            "Name": "podman-machine-default",
            "URI": "ssh://core@127.0.0.1:51234/run/user/501/podman/podman.sock",
            "IsMachine": True,
        }}]))
    elif mode in {{"machine-malformed-string", "machine-malformed-zero"}}:
        print(json.dumps([{{
            "Name": "podman-machine-default",
            "URI": "ssh://core@127.0.0.1:51234/run/user/501/podman/podman.sock",
            "IsMachine": "true" if mode.endswith("string") else 0,
        }}]))
    elif mode == "loopback-tcp":
        print(json.dumps([{{
            "Name": "podman-loopback",
            "URI": "tcp://localhost:9988",
        }}]))
    elif mode == "remote-plus-local":
        print(json.dumps([{{
            "Name": "stored-remote",
            "URI": "ssh://operator@remote.example.invalid/run/user/1000/podman/podman.sock",
            "IsMachine": False,
        }}]))
    else:
        print("[]")
    raise SystemExit(0)
if runtime == "podman" and args[0:3] == ["machine", "list", "--format"]:
    print(json.dumps([{{"Name": "podman-machine-default", "SSHPort": 51234}}]))
    raise SystemExit(0)
if runtime == "podman" and len(args) >= 2 and args[0] == "--connection":
    args = args[2:]

if args and args[0] in {{"info", "version"}}:
    if mode == "runtime-error":
        print(os.environ["FAKE_SECRET"], file=sys.stderr)
        raise SystemExit(71)
    print("fake " + runtime)
    raise SystemExit(0)

if args and args[0] == "ps":
    if mode == "inspection-error":
        print(os.environ["FAKE_SECRET"], file=sys.stderr)
        raise SystemExit(72)
    state["ps_count"] = state.get("ps_count", 0) + 1
    containers = list(state.get("containers", []))
    if mode == "post-reappear" and state["ps_count"] >= 5:
        containers = list(state.get("reappear_template", []))
        state["containers"] = containers
    if mode == "reappear" and state.get("delete_seen"):
        state["ps_after_delete"] = state.get("ps_after_delete", 0) + 1
        if state["ps_after_delete"] == 1:
            containers = []
        else:
            containers = list(state.get("removed", []))
            state["containers"] = containers
    state_path.write_text(json.dumps(state), encoding="utf-8")
    for container in containers:
        print(container["Id"])
    raise SystemExit(0)

if args and args[0] == "inspect":
    if mode == "inspection-error":
        print(os.environ["FAKE_SECRET"], file=sys.stderr)
        raise SystemExit(73)
    wanted = {{arg for arg in args[1:] if not arg.startswith("-")}}
    containers = [
        item for item in state.get("containers", []) if item["Id"] in wanted
    ]
    if not containers and wanted:
        raise SystemExit(1)
    print(json.dumps(containers))
    raise SystemExit(0)

if args and args[0] == "rm":
    wanted = {{arg for arg in args[1:] if not arg.startswith("-")}}
    removed = [
        item for item in state.get("containers", []) if item["Id"] in wanted
    ]
    state["delete_seen"] = True
    state["removed"] = removed
    if mode not in {{"persistent", "reappear"}}:
        state["containers"] = [
            item for item in state.get("containers", []) if item["Id"] not in wanted
        ]
    elif mode == "reappear":
        state["containers"] = []
    state_path.write_text(json.dumps(state), encoding="utf-8")
    raise SystemExit(0)

print("unexpected " + runtime + " invocation", file=sys.stderr)
raise SystemExit(64)
"""


def _container(
    container_id: str,
    *,
    owned: bool = True,
    ambiguous: bool = False,
    running: bool = False,
    omit_config: bool = False,
    omit_labels: bool = False,
) -> dict[str, Any]:
    labels: dict[str, str] = {
        "com.docker.compose.service": "nemoclaw-gateway",
    }
    if owned and not ambiguous:
        labels.update(
            {
                "com.docker.compose.project": "nemoclaw",
                "com.docker.compose.project.config_files": (
                    "/opt/mac/deploy/nemoclaw/docker-compose.yaml"
                ),
                "com.docker.compose.project.working_dir": "/opt/mac/deploy/nemoclaw",
            }
        )
    container: dict[str, Any] = {
        "Id": container_id,
        "Name": "/nemoclaw-gateway",
        "State": {"Running": running},
        "Config": {
            "Image": "ghcr.io/nvidia/nemoclaw-gateway:latest",
            "Cmd": ["nemoclaw", "gateway"],
            "Labels": labels,
        },
    }
    if omit_config:
        container.pop("Config")
    elif omit_labels:
        container["Config"].pop("Labels")
    return container


def _openshell_container(
    container_id: str,
    *,
    sandbox: str = "mac-task-stale-fixture",
    running: bool,
) -> dict[str, Any]:
    return {
        "Id": container_id,
        "Name": "/openshell-" + sandbox,
        "State": {"Running": running},
        "Config": {
            "Image": "localhost/mac-hermes:net",
            "Cmd": ["sleep", "infinity"],
            "Labels": {
                "openshell.ai/managed-by": "openshell",
                "openshell.ai/sandbox-name": sandbox,
            },
        },
    }


@dataclass
class QuiescenceRun:
    result: subprocess.CompletedProcess[str]
    mac_home: Path
    calls: Path
    marker: Path
    openshell_state: Path
    docker_state: Path
    podman_state: Path
    child_pid: Path


def _run_quiescence(
    tmp_path: Path,
    *,
    sandbox_source: str = "sandbox-name",
    sandbox_present: bool = True,
    sandbox_file_mode: int = 0o600,
    openshell_mode: str = "active",
    wrapper_mode: str = "success",
    docker: list[dict[str, Any]] | None = None,
    podman: list[dict[str, Any]] | None = None,
    docker_mode: str = "normal",
    podman_mode: str = "normal",
    docker_reappear: list[dict[str, Any]] | None = None,
    openshell_symlink: bool = False,
    podman_docker_symlink: bool = False,
    seed_marker: bool = False,
    extra_env: dict[str, str] | None = None,
    run_quiesce: bool = True,
    assert_phase: str | None = None,
) -> QuiescenceRun:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    managed = mac_home / "openclaw" / "managed"
    mac_bin = mac_home / "bin"
    fake_bin = tmp_path / "bin"
    for directory in (managed, mac_bin, fake_bin):
        directory.mkdir(parents=True, exist_ok=True)
    managed.chmod(0o700)

    if sandbox_source == "sandbox-name":
        sandbox_name = managed / "sandbox-name"
        sandbox_name.write_text(SANDBOX + "\n", encoding="utf-8")
        sandbox_name.chmod(sandbox_file_mode)
        runtime_env = managed / "runtime.env"
        runtime_env.write_text(
            "MAC_OPENCLAW_SANDBOX='mac-openclaw-wrong-fallback'\n"
            f"MAC_OPENCLAW_ROUTER_API_KEY='{SECRET}'\n",
            encoding="utf-8",
        )
        runtime_env.chmod(0o600)
    elif sandbox_source == "runtime.env":
        runtime_env = managed / "runtime.env"
        runtime_env.write_text(
            "# only MAC_OPENCLAW_SANDBOX may be parsed by the deployer\n"
            f"MAC_OPENCLAW_ROUTER_API_KEY='{SECRET}'\n"
            f"MAC_OPENCLAW_SANDBOX='{SANDBOX}'\n"
            "MAC_OPENCLAW_HOSTILE='$(touch should-not-exist)'\n",
            encoding="utf-8",
        )
        runtime_env.chmod(sandbox_file_mode)
    elif sandbox_source == "none":
        # The managed directory is created by the harness, but no authoritative
        # OpenClaw artifact exists, so sandbox quiescence is a no-op.
        pass
    else:  # pragma: no cover - harness misuse.
        raise ValueError(sandbox_source)

    calls = tmp_path / "calls.log"
    openshell_state = tmp_path / "openshell-state.json"
    docker_state = tmp_path / "docker-state.json"
    podman_state = tmp_path / "podman-state.json"
    child_pid = tmp_path / "child.pid"
    openshell_state.write_text(
        json.dumps({"present": sandbox_present}), encoding="utf-8"
    )
    docker_state.write_text(
        json.dumps(
            {
                "containers": docker or [],
                "reappear_template": docker_reappear or [],
            }
        ),
        encoding="utf-8",
    )
    podman_state.write_text(
        json.dumps({"containers": podman or []}), encoding="utf-8"
    )

    openshell_source = _fake_openshell_source()
    _write_executable(fake_bin / "openshell", openshell_source)
    if openshell_symlink:
        (fake_bin / "openshell").chmod(0o755)
        (mac_bin / "openshell").symlink_to(fake_bin / "openshell")
    else:
        _write_executable(mac_bin / "openshell", openshell_source)
    canonical_openshell = mac_bin / "openshell"
    cli_sha256 = hashlib.sha256(canonical_openshell.read_bytes()).hexdigest()
    reviewed_dir = mac_home / "openshell"
    reviewed_dir.mkdir(mode=0o700)
    reviewed_dir.chmod(0o700)
    host_arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    receipt = reviewed_dir / "reviewed-cli.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": "mac.reviewed_openshell_cli.v1",
                "status": "published",
                "version": "0.0.72",
                "os": platform.system().lower(),
                "arch": host_arch,
                "asset": "openshell-test-fixture",
                # The behavioral fixture's reviewed asset is the exact fake CLI
                # installed above, so both evidence digests are truthful.
                "asset_sha256": cli_sha256,
                "cli_path": str(canonical_openshell),
                "cli_sha256": cli_sha256,
                "recorded_at": "2026-07-20T00:00:00Z",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    receipt.chmod(0o600)
    receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
    _write_executable(mac_bin / "openclaw-gateway-stop", _fake_stop_wrapper_source())
    _write_executable(fake_bin / "podman", _fake_runtime_source("podman"))
    if podman_docker_symlink:
        (fake_bin / "docker").symlink_to(fake_bin / "podman")
    else:
        _write_executable(fake_bin / "docker", _fake_runtime_source("docker"))

    marker = mac_home / f"daemon-resource-quiescence-{GENERATION}.json"
    if seed_marker:
        marker.write_text('{"schema":"stale"}\n', encoding="utf-8")
        marker.chmod(0o600)

    harness = tmp_path / "harness.sh"
    invocation = ""
    if run_quiesce:
        invocation += f"\n{FUNCTION}\n"
    if assert_phase is not None:
        invocation += (
            "\nassert_legacy_nemoclaw_containers_inactive "
            + _shell_quote(assert_phase)
            + "\n"
        )
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "log() { printf '%s\\n' \"$*\" >&2; }\n"
        f"MAC_HOME={_shell_quote(str(mac_home))}\n"
        f"DEPLOY_GENERATION={_shell_quote(GENERATION)}\n"
        f"DEPLOY_REV={_shell_quote(REVISION)}\n"
        "AGENT=agent_test\n"
        "FLEET_NAME=mac\n"
        f"PY={_shell_quote(sys.executable)}\n"
        f"PYTHON_BIN={_shell_quote(sys.executable)}\n"
        "export MAC_DEPLOY_REVIEWED_OPENSHELL_VERSION=0.0.72\n"
        f"export MAC_DEPLOY_REVIEWED_OPENSHELL_ASSET_SHA256={cli_sha256}\n"
        f"export MAC_DEPLOY_REVIEWED_OPENSHELL_CLI_SHA256={cli_sha256}\n"
        f"export MAC_DEPLOY_REVIEWED_OPENSHELL_RECEIPT_SHA256={receipt_sha256}\n"
        f"DOCKER_BIN={_shell_quote(str(fake_bin / 'docker'))}\n"
        f"PODMAN_BIN={_shell_quote(str(fake_bin / 'podman'))}\n"
        "CONTAINER_RUNTIME_PATHS=(\"$DOCKER_BIN\" \"$PODMAN_BIN\")\n"
        + _quiescence_block()
        + invocation,
        encoding="utf-8",
    )
    harness.chmod(0o700)
    env = {
        **os.environ,
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "FAKE_DAEMON_CALLS": str(calls),
        "FAKE_OPENSHELL_STATE": str(openshell_state),
        "FAKE_DOCKER_STATE": str(docker_state),
        "FAKE_PODMAN_STATE": str(podman_state),
        "FAKE_CHILD_PID": str(child_pid),
        "FAKE_OPENSHELL_MODE": openshell_mode,
        "FAKE_STOP_WRAPPER_MODE": wrapper_mode,
        "FAKE_DOCKER_MODE": docker_mode,
        "FAKE_PODMAN_MODE": podman_mode,
        "FAKE_SANDBOX_NAME": SANDBOX,
        "FAKE_SECRET": SECRET,
        "MAC_DEPLOY_DAEMON_TEST_MODE": "1",
        # Production defaults remain conservative. Keep this guard much smaller
        # than production while allowing a cold Python fake CLI to start under
        # xdist/coverage scheduler load. The three-second aggregate deadline
        # remains the behavioral bound.
        "MAC_DEPLOY_DAEMON_COMMAND_TIMEOUT_SECONDS": "1",
        # Inventorying two independent runtimes launches several short-lived
        # Python fake CLIs.  Three seconds leaves ample room on a loaded CI
        # host while still making persistent-resource failures fast.
        "MAC_DEPLOY_DAEMON_QUIESCENCE_TIMEOUT_SECONDS": "3",
        "MAC_DEPLOY_DAEMON_QUIESCENCE_POLL_SECONDS": "0.1",
    }
    env.update(extra_env or {})
    # The quiescence orchestrator runs under ``$PY -I -S`` (isolated, no site) so
    # it never loads coverage's site .pth, but the short-lived fake runtime CLIs
    # (docker/podman/openshell inventory) run via PATH and ARE traced by
    # coverage.py's ``patch = ["subprocess"]`` (COVERAGE_PROCESS_{START,CONFIG}).
    # They import only stdlib — never ``mac`` — so tracing adds ~5.6x start
    # overhead for ZERO src/mac coverage. Strip it so the fakes start natively;
    # the finite command and aggregate deadlines still exercise timeout behavior,
    # and coverage totals are unchanged.
    env.pop("COVERAGE_PROCESS_START", None)
    env.pop("COVERAGE_PROCESS_CONFIG", None)
    result = subprocess.run(
        ["bash", str(harness)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=8,
    )
    return QuiescenceRun(
        result=result,
        mac_home=mac_home,
        calls=calls,
        marker=marker,
        openshell_state=openshell_state,
        docker_state=docker_state,
        podman_state=podman_state,
        child_pid=child_pid,
    )


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _call_lines(run: QuiescenceRun) -> list[str]:
    if not run.calls.exists():
        return []
    return run.calls.read_text(encoding="utf-8").splitlines()


def _assert_no_secret(run: QuiescenceRun) -> None:
    material = run.result.stdout + run.result.stderr
    if run.marker.exists():
        material += run.marker.read_text(encoding="utf-8")
    assert SECRET not in material


def _assert_success_marker(run: QuiescenceRun) -> dict[str, Any]:
    assert run.result.returncode == 0, run.result.stderr
    assert run.marker.is_file()
    assert stat.S_IMODE(run.marker.stat().st_mode) == 0o600
    receipt = json.loads(run.marker.read_text(encoding="utf-8"))
    assert receipt["schema"] == SCHEMA
    assert str(receipt["generation"]) == GENERATION
    assert receipt["revision"] == REVISION
    _assert_no_secret(run)
    return receipt


def test_block_interface_and_main_call_order_are_stable() -> None:
    block = _quiescence_block()
    assert "OPENSHELL_GATEWAY_ENDPOINT" in block
    assert "http://127.0.0.1:17670" in block
    assert "sandbox" in block and "list" in block and "delete" in block
    assert SCHEMA in block
    assert "/usr/bin/env -i" in block
    assert '"$PY" -I -S -' in block
    assert "os.environ.copy()" not in block
    assert "SSH_AUTH_SOCK" not in block

    main = _installer_text().split(
        'write_deploy_manifest "pre" "$MANIFEST_PRE"', 1
    )[1]
    stop = main.index("stop_existing_services_for_deploy\n")
    quiesce = main.index(FUNCTION + "\n", stop)
    backup = main.index("backup_existing_artifacts\n", quiesce)
    install = main.index('log "installing mac source"', backup)
    assert stop < quiesce < backup < install

    verify_function = _installer_text().split("verify_openclaw_gateway() {", 1)[1]
    verify_function = verify_function.split("\n}\n", 1)[0]
    assert verify_function.index("assert_legacy_nemoclaw_containers_inactive") < (
        verify_function.index('"$installer" verify')
    )
    finalize_function = _installer_text().split("finalize_openclaw_gateway() {", 1)[1]
    finalize_function = finalize_function.split("\n}\n", 1)[0]
    assert finalize_function.index("assert_legacy_nemoclaw_containers_inactive") < (
        finalize_function.index('"$installer" finalize')
    )


def test_deploy_credentials_are_absent_from_gate_and_daemon_children(
    tmp_path: Path,
) -> None:
    run = _run_quiescence(
        tmp_path,
        extra_env={
            "MAC_API_TOKEN": SECRET,
            "GH_TOKEN": SECRET,
            "OPENAI_API_KEY": SECRET,
            "SLACK_BOT_TOKEN": SECRET,
        },
    )
    _assert_success_marker(run)


def test_container_probe_gets_metadata_only_docker_config(tmp_path: Path) -> None:
    docker_config = tmp_path / "docker-config-with-auth"
    docker_config.mkdir()
    (docker_config / "config.json").write_text(
        json.dumps({"auths": {"registry.invalid": {"auth": SECRET}}}),
        encoding="utf-8",
    )
    run = _run_quiescence(
        tmp_path / "run",
        extra_env={
            "DOCKER_CONFIG": str(docker_config),
            "SSH_AUTH_SOCK": str(tmp_path / "live-agent.sock"),
        },
    )
    _assert_success_marker(run)


def test_exported_scalar_cannot_enable_configured_only_runtime_discovery(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "capture.json"
    fake_python = tmp_path / "capture-runtime-env"
    _write_executable(
        fake_python,
        f"""#!{sys.executable}
import json
import os
from pathlib import Path
Path(os.environ["FAKE_GATE_CAPTURE"]).write_text(json.dumps({{
    "configured": os.environ.get("MAC_DEPLOY_DAEMON_RUNTIME_PATHS_CONFIGURED"),
    "paths": os.environ.get("MAC_DEPLOY_DAEMON_RUNTIME_PATHS"),
}}), encoding="utf-8")
""",
    )
    harness = tmp_path / "scalar-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"PY={_shell_quote(str(fake_python))}\n"
        f"MAC_HOME={_shell_quote(str(tmp_path / 'home'))}\n"
        f"FAKE_GATE_CAPTURE={_shell_quote(str(capture))}\n"
        "MAC_DEPLOY_DAEMON_TEST_MODE=1\n"
        "DEPLOY_GENERATION=42\n"
        f"DEPLOY_REV={REVISION}\n"
        "CONTAINER_RUNTIME_PATHS=''\n"
        "export CONTAINER_RUNTIME_PATHS FAKE_GATE_CAPTURE MAC_DEPLOY_DAEMON_TEST_MODE\n"
        + _quiescence_block()
        + "\ndaemon_resource_quiescence_gate quiesce pre_source\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(harness)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text(encoding="utf-8")) == {
        "configured": "0",
        "paths": "",
    }


def test_subprocess_cleanup_has_no_unbounded_wait() -> None:
    block = _quiescence_block()
    assert "process.wait()" not in block
    assert "process.wait(timeout=reap_budget)" in block


def test_private_sandbox_name_wins_and_stop_precedes_exact_delete(tmp_path: Path) -> None:
    run = _run_quiescence(tmp_path)
    _assert_success_marker(run)
    calls = _call_lines(run)
    wrapper = calls.index("openclaw-stop:")
    delete = calls.index(f"openshell:sandbox delete {SANDBOX}")
    assert wrapper < delete
    assert any(
        line.startswith("openshell:sandbox list")
        and "--limit 1000" in line
        and "--output json" in line
        for line in calls
    )
    assert not any("mac-openclaw-wrong-fallback" in line for line in calls)
    _assert_no_secret(run)


def test_legacy_openshell_symlink_to_0755_binary_is_rejected(
    tmp_path: Path,
) -> None:
    run = _run_quiescence(tmp_path, openshell_symlink=True)
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert "reviewed OpenShell CLI canonical path is indirect" in run.result.stderr
    assert f"openshell:sandbox delete {SANDBOX}" not in _call_lines(run)


def test_runtime_env_is_parsed_not_sourced_and_does_not_disclose_secrets(
    tmp_path: Path,
) -> None:
    run = _run_quiescence(tmp_path, sandbox_source="runtime.env")
    _assert_success_marker(run)
    assert not (tmp_path / "should-not-exist").exists()
    assert f"openshell:sandbox delete {SANDBOX}" in _call_lines(run)
    _assert_no_secret(run)


@pytest.mark.parametrize("source", ["sandbox-name", "runtime.env"])
def test_non_private_authoritative_sandbox_artifact_fails_closed(
    tmp_path: Path, source: str
) -> None:
    run = _run_quiescence(
        tmp_path,
        sandbox_source=source,
        sandbox_file_mode=0o644,
        seed_marker=True,
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert not any("sandbox delete" in line for line in _call_lines(run))
    _assert_no_secret(run)


def test_already_absent_sandbox_neither_stops_nor_deletes(tmp_path: Path) -> None:
    run = _run_quiescence(tmp_path, sandbox_present=False)
    _assert_success_marker(run)
    calls = _call_lines(run)
    assert any(line.startswith("openshell:sandbox list") for line in calls)
    assert not any(line.startswith("openclaw-stop:") for line in calls)
    assert not any("sandbox delete" in line for line in calls)


def test_openshell_inventory_paginates_before_deciding_absence(
    tmp_path: Path,
) -> None:
    run = _run_quiescence(tmp_path, openshell_mode="second-page")
    _assert_success_marker(run)
    calls = _call_lines(run)
    assert any("--offset 1000" in line for line in calls)
    assert f"openshell:sandbox delete {SANDBOX}" in calls


@pytest.mark.parametrize(
    "variable",
    [
        "MAC_DEPLOY_DAEMON_COMMAND_TIMEOUT_SECONDS",
        "MAC_DEPLOY_DAEMON_QUIESCENCE_TIMEOUT_SECONDS",
        "MAC_DEPLOY_DAEMON_QUIESCENCE_POLL_SECONDS",
        "MAC_DEPLOY_DAEMON_TOTAL_TIMEOUT_SECONDS",
    ],
)
def test_non_finite_deadline_configuration_fails_closed(
    tmp_path: Path, variable: str
) -> None:
    run = _run_quiescence(
        tmp_path,
        seed_marker=True,
        extra_env={variable: "nan"},
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()


def test_end_to_end_deadline_covers_discovery_and_inventory(tmp_path: Path) -> None:
    run = _run_quiescence(
        tmp_path,
        seed_marker=True,
        extra_env={"MAC_DEPLOY_DAEMON_TOTAL_TIMEOUT_SECONDS": "0.01"},
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()


def test_zero_poll_interval_cannot_certify_back_to_back_absence(tmp_path: Path) -> None:
    run = _run_quiescence(
        tmp_path,
        seed_marker=True,
        extra_env={"MAC_DEPLOY_DAEMON_QUIESCENCE_POLL_SECONDS": "0"},
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()


@pytest.mark.parametrize("mode", ["nonzero", "malformed", "timeout", "duplicate"])
def test_openshell_unknown_states_fail_closed_without_raw_output(
    tmp_path: Path, mode: str
) -> None:
    run = _run_quiescence(tmp_path, openshell_mode=mode, seed_marker=True)
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert not any("sandbox delete" in line for line in _call_lines(run))
    _assert_no_secret(run)


def test_delayed_sandbox_deletion_waits_for_proven_absence(tmp_path: Path) -> None:
    run = _run_quiescence(tmp_path, openshell_mode="delayed-delete")
    _assert_success_marker(run)
    state = json.loads(run.openshell_state.read_text(encoding="utf-8"))
    assert state["lists_after_delete"] >= 3
    assert state["present"] is False


def test_sandbox_reappearance_after_delete_fails_without_receipt(
    tmp_path: Path,
) -> None:
    run = _run_quiescence(
        tmp_path,
        openshell_mode="persistent-delete",
        seed_marker=True,
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    state = json.loads(run.openshell_state.read_text(encoding="utf-8"))
    assert state["lists_after_delete"] >= 2


def test_persistent_sandbox_deletion_fails_without_receipt(tmp_path: Path) -> None:
    run = _run_quiescence(
        tmp_path,
        openshell_mode="persistent-delete",
        seed_marker=True,
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert f"openshell:sandbox delete {SANDBOX}" in _call_lines(run)
    _assert_no_secret(run)


@pytest.mark.parametrize("mode", ["failure", "timeout"])
def test_stop_wrapper_failure_blocks_delete_and_receipt(
    tmp_path: Path, mode: str
) -> None:
    run = _run_quiescence(tmp_path, wrapper_mode=mode, seed_marker=True)
    assert run.result.returncode != 0
    assert not run.marker.exists()
    calls = _call_lines(run)
    assert "openclaw-stop:" in calls
    assert not any("sandbox delete" in line for line in calls)
    _assert_no_secret(run)


def test_timeout_kills_descendant_that_ignores_term(tmp_path: Path) -> None:
    run = _run_quiescence(
        tmp_path,
        wrapper_mode="child-timeout",
        seed_marker=True,
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert run.child_pid.is_file()
    child = run.child_pid.read_text(encoding="utf-8").strip()
    deadline = time.monotonic() + 15
    state = ""
    while time.monotonic() < deadline:
        observed = subprocess.run(
            ["ps", "-o", "stat=", "-p", child],
            check=False,
            capture_output=True,
            text=True,
        )
        state = observed.stdout.strip()
        if not state or state.startswith("Z"):
            break
        time.sleep(0.05)
    assert not state or state.startswith("Z")


def test_stopped_docker_and_podman_nemoclaw_containers_are_retained_inactive(
    tmp_path: Path,
) -> None:
    docker_id = "d" * 64
    podman_id = "p" * 64
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        docker=[_container(docker_id)],
        podman=[_container(podman_id)],
    )
    receipt = _assert_success_marker(run)
    assert len(
        json.loads(run.docker_state.read_text(encoding="utf-8"))["containers"]
    ) == 1
    assert len(
        json.loads(run.podman_state.read_text(encoding="utf-8"))["containers"]
    ) == 1
    calls = _call_lines(run)
    assert not any("rm -f" in line for line in calls)
    serialized = json.dumps(receipt, sort_keys=True)
    assert docker_id in serialized and podman_id in serialized
    assert receipt["legacy_nemoclaw"]["final_state"] == "inactive"
    assert str(tmp_path / "bin" / "docker") in serialized
    assert str(tmp_path / "bin" / "podman") in serialized


def test_running_openshell_managed_container_fails_before_receipt(
    tmp_path: Path,
) -> None:
    container_id = "a" * 64
    run = _run_quiescence(
        tmp_path,
        sandbox_present=False,
        docker=[_openshell_container(container_id, running=True)],
        seed_marker=True,
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert "running OpenShell-managed sandbox survived" in run.result.stderr
    assert not any(" rm " in f" {line} " for line in _call_lines(run))
    _assert_no_secret(run)


def test_stopped_openshell_managed_container_is_compatible_and_proved(
    tmp_path: Path,
) -> None:
    container_id = "b" * 64
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        docker=[_openshell_container(container_id, running=False)],
    )
    receipt = _assert_success_marker(run)
    assert receipt["openshell_managed"] == {
        "final_state": "inactive",
        "stable_inactive_observations": 2,
        "container_runtimes": receipt["container_runtimes"],
    }
    state = json.loads(run.docker_state.read_text(encoding="utf-8"))
    assert [item["Id"] for item in state["containers"]] == [container_id]


def test_all_local_docker_context_endpoints_are_certified(tmp_path: Path) -> None:
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        docker_mode="two-contexts",
    )
    receipt = _assert_success_marker(run)
    docker_endpoints = {
        item["endpoint"]
        for item in receipt["container_runtimes"]
        if item["kind"] == "docker"
    }
    assert len(docker_endpoints) == 2
    calls = _call_lines(run)
    assert any("--context default info" in line for line in calls)
    assert any("--context secondary info" in line for line in calls)


def test_ambient_remote_container_endpoint_is_rejected(tmp_path: Path) -> None:
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        seed_marker=True,
        extra_env={"DOCKER_HOST": "tcp://remote.example.invalid:2376"},
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    _assert_no_secret(run)


def test_macos_podman_machine_connection_is_explicitly_certified(
    tmp_path: Path,
) -> None:
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        podman_mode="machine-ssh",
        extra_env={"OS_KIND": "darwin"},
    )
    receipt = _assert_success_marker(run)
    podman_endpoints = [
        item["endpoint"]
        for item in receipt["container_runtimes"]
        if item["kind"] == "podman"
    ]
    assert podman_endpoints == [
        "podman-machine://podman-machine-default@127.0.0.1:51234/run/user/501/podman/podman.sock"
    ]
    assert any(
        "podman:--connection podman-machine-default info" in line
        for line in _call_lines(run)
    )


@pytest.mark.parametrize(
    "podman_mode", ["machine-malformed-string", "machine-malformed-zero"]
)
def test_malformed_podman_machine_metadata_cannot_authorize_inventory_or_deletion(
    tmp_path: Path,
    podman_mode: str,
) -> None:
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        podman=[_container("m" * 64)],
        podman_mode=podman_mode,
        seed_marker=True,
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    podman_calls = [line for line in _call_lines(run) if line.startswith("podman:")]
    assert not any(" ps " in f" {line} " or " rm " in f" {line} " for line in podman_calls)
    assert not any("--connection podman-machine-default" in line for line in podman_calls)
    _assert_no_secret(run)


@pytest.mark.parametrize(
    ("runtime", "mode", "forbidden_selector"),
    [
        (
            "docker",
            "loopback-tcp",
            "--context default",
        ),
        (
            "podman",
            "loopback-tcp",
            "--connection podman-loopback",
        ),
        (
            "docker",
            "loopback-ssh",
            "--context default",
        ),
    ],
)
def test_unproven_loopback_daemon_endpoints_fail_closed_before_mutation(
    tmp_path: Path,
    runtime: str,
    mode: str,
    forbidden_selector: str,
) -> None:
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        docker_mode=mode if runtime == "docker" else "normal",
        podman_mode=mode if runtime == "podman" else "normal",
        seed_marker=True,
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    runtime_calls = [line for line in _call_lines(run) if line.startswith(runtime + ":")]
    assert not any(forbidden_selector in line for line in runtime_calls)
    assert not any(
        " ps " in f" {line} " or " rm " in f" {line} " for line in runtime_calls
    )
    _assert_no_secret(run)


def test_linux_native_podman_store_is_inventoried_with_stored_remote_connections(
    tmp_path: Path,
) -> None:
    container_id = "l" * 64
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        podman=[_container(container_id)],
        podman_mode="remote-plus-local",
        extra_env={"OS_KIND": "linux"},
    )
    receipt = _assert_success_marker(run)
    assert any(
        item["endpoint"].startswith("podman-local://")
        for item in receipt["container_runtimes"]
    )
    assert not any("rm -f" in line for line in _call_lines(run))


def test_linux_podman_docker_symlink_is_classified_as_one_native_podman_store(
    tmp_path: Path,
) -> None:
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        podman_docker_symlink=True,
        extra_env={"OS_KIND": "linux"},
    )
    receipt = _assert_success_marker(run)
    runtimes = receipt["container_runtimes"]
    assert [item["kind"] for item in runtimes] == ["podman"]
    assert runtimes[0]["endpoint"].startswith("podman-local://")
    calls = _call_lines(run)
    assert not any("context" in line for line in calls)
    assert sum(line.startswith("podman:system connection list") for line in calls) == 1
    assert sum(line.startswith("podman:ps ") for line in calls) == 4


@pytest.mark.parametrize(
    ("runtime", "mode"),
    [("docker", "runtime-error"), ("docker", "inspection-error"), ("podman", "inspection-error")],
)
def test_runtime_or_container_inspection_failure_is_not_treated_as_absence(
    tmp_path: Path, runtime: str, mode: str
) -> None:
    kwargs: dict[str, Any] = {
        "sandbox_source": "none",
        runtime: [_container(runtime[0] * 64)],
        f"{runtime}_mode": mode,
        "seed_marker": True,
    }
    run = _run_quiescence(tmp_path, **kwargs)
    assert run.result.returncode != 0
    assert not run.marker.exists()
    _assert_no_secret(run)


def test_ambiguous_compose_service_label_fails_closed_without_deletion(
    tmp_path: Path,
) -> None:
    container_id = "b" * 64
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        docker=[_container(container_id, owned=False, ambiguous=True)],
        seed_marker=True,
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert not any(
        line.startswith("docker:") and "rm -f" in line for line in _call_lines(run)
    )
    state = json.loads(run.docker_state.read_text(encoding="utf-8"))
    assert [item["Id"] for item in state["containers"]] == [container_id]


@pytest.mark.parametrize("field", ["Config", "Labels"])
def test_partial_container_inspection_cannot_prove_unrelated_state(
    tmp_path: Path, field: str
) -> None:
    container = _container(
        "e" * 64,
        omit_config=field == "Config",
        omit_labels=field == "Labels",
    )
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        docker=[container],
        seed_marker=True,
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert not any("rm -f" in line for line in _call_lines(run))


def test_running_legacy_container_fails_before_any_daemon_resource_mutation(
    tmp_path: Path,
) -> None:
    container_id = "c" * 64
    run = _run_quiescence(
        tmp_path,
        docker=[_container(container_id, running=True)],
        seed_marker=True,
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    calls = _call_lines(run)
    assert not any("rm -f" in line for line in calls)
    assert not any("openclaw-stop:" in line for line in calls)
    assert not any("sandbox delete" in line for line in calls)


def test_receipt_is_success_only_atomic_private_and_generation_bound(
    tmp_path: Path,
) -> None:
    success = _run_quiescence(tmp_path / "success", sandbox_source="none")
    receipt = _assert_success_marker(success)
    assert receipt["generation"] in {GENERATION, int(GENERATION)}
    assert not list(success.mac_home.glob(success.marker.name + ".*"))

    failure = _run_quiescence(
        tmp_path / "failure",
        sandbox_source="none",
        docker=[_container("f" * 64)],
        docker_mode="inspection-error",
        seed_marker=True,
    )
    assert failure.result.returncode != 0
    assert not failure.marker.exists()
    assert not list(failure.mac_home.glob(failure.marker.name + ".*"))


def test_receipt_is_removed_when_post_replace_durability_fails(
    tmp_path: Path,
) -> None:
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        seed_marker=True,
        extra_env={"MAC_DEPLOY_DAEMON_INJECT_RECEIPT_POST_REPLACE_FAILURE": "1"},
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert not list(run.mac_home.glob(run.marker.name + ".*"))


def test_assertion_requires_and_promotes_matching_pre_source_receipt(
    tmp_path: Path,
) -> None:
    missing = _run_quiescence(
        tmp_path / "missing",
        sandbox_source="none",
        run_quiesce=False,
        assert_phase="post_install",
    )
    assert missing.result.returncode != 0
    assert not missing.marker.exists()

    success = _run_quiescence(
        tmp_path / "success",
        sandbox_source="none",
        assert_phase="post_install",
    )
    receipt = _assert_success_marker(success)
    assert receipt["proofs"]["pre_source"]["stable_inactive_observations"] == 2
    assert receipt["proofs"]["post_install"]["stable_inactive_observations"] == 2
    assert receipt["post_install"] == receipt["proofs"]["post_install"]


def test_failed_post_assertion_cannot_leave_stale_post_proof(
    tmp_path: Path,
) -> None:
    reappearing = _container("a" * 64, running=True)
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        docker_mode="post-reappear",
        docker_reappear=[reappearing],
        assert_phase="post_install",
    )
    assert run.result.returncode != 0
    receipt = json.loads(run.marker.read_text(encoding="utf-8"))
    assert "post_install" not in receipt
    assert "post_install" not in receipt["proofs"]
