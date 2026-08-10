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
import shutil
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
    stale = json.loads(os.environ.get("FAKE_STALE_SANDBOXES", "[]"))
    reaped = set(state.get("deleted_stale", []))
    # A live, lease-owned sandbox whose name contains ``-drain-`` models an
    # executor that finishes its task mid-drain: after ``FAKE_LIVE_DRAIN_LISTS``
    # inventory passes the sandbox is torn down and vanishes, letting the gate
    # observe the lease reach a terminal state without any delete call.
    drain_lists = int(os.environ.get("FAKE_LIVE_DRAIN_LISTS", "0"))
    if drain_lists > 0:
        state["live_drain_lists"] = state.get("live_drain_lists", 0) + 1
        if state["live_drain_lists"] >= drain_lists:
            state.setdefault("drained_live", [])
            for row in stale:
                name = row.get("name") or ""
                if "-drain-" in name and name not in state["drained_live"]:
                    state["drained_live"].append(name)
    drained_live = set(state.get("drained_live", []))
    listing = [{{"name": sandbox}}] if state.get("present") else []
    listing.extend(
        row
        for row in stale
        if row.get("name") not in reaped and row.get("name") not in drained_live
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps(listing))
    raise SystemExit(0)

if len(args) >= 5 and args[0:2] == ["sandbox", "download"]:
    stale_names = {{row.get("name") for row in json.loads(os.environ.get("FAKE_STALE_SANDBOXES", "[]"))}}
    if args[2] not in stale_names or args[3] != "/sandbox":
        raise SystemExit(65)
    if mode == "stale-download-nonzero":
        raise SystemExit(67)
    if mode == "stale-download-slow":
        time.sleep(1.25)
    destination = Path(args[4])
    destination.mkdir(parents=True)
    (destination / "task.json").write_text(
        json.dumps({{"sandbox": args[2]}}), encoding="utf-8"
    )
    state["downloaded_stale"] = sorted(
        set(state.get("downloaded_stale", [])) | {{args[2]}}
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    raise SystemExit(0)

if len(args) >= 3 and args[0:2] == ["sandbox", "delete"]:
    stale_names = {{row.get("name") for row in json.loads(os.environ.get("FAKE_STALE_SANDBOXES", "[]"))}}
    if args[2] in stale_names:
        # Additive stale-delete fault injection keyed on the already-allowlisted
        # ``FAKE_OPENSHELL_MODE`` (no new production env passthrough required).
        # The default modes are unchanged: the stale sandbox is reaped and
        # disappears from subsequent listings.  ``stale-delete-nonzero`` makes
        # the delete itself fail (nonzero exit) without reaping, exercising the
        # deletion-failure path.  ``stale-persist`` returns success yet never
        # reaps, so the sandbox keeps reappearing and the reconcile deadline
        # path must fail closed.  ``stale-linger`` returns success but only
        # reaps a name after two delete attempts, so a reap-eligible sandbox is
        # re-observed on a later pass — exercising ``seen`` name de-duplication.
        state["stale_delete_attempts"] = state.get("stale_delete_attempts", 0) + 1
        if mode == "stale-delete-nonzero":
            state_path.write_text(json.dumps(state), encoding="utf-8")
            raise SystemExit(66)
        if mode == "stale-persist":
            state_path.write_text(json.dumps(state), encoding="utf-8")
            raise SystemExit(0)
        if mode == "stale-linger":
            per_name = state.get("stale_delete_per_name", {{}})
            per_name[args[2]] = per_name.get(args[2], 0) + 1
            state["stale_delete_per_name"] = per_name
            if per_name[args[2]] < 2:
                # First observation: report success but leave it present so it
                # is re-listed and re-classified as reap-eligible next pass.
                state_path.write_text(json.dumps(state), encoding="utf-8")
                raise SystemExit(0)
        reaped = set(state.get("deleted_stale", []))
        reaped.add(args[2])
        state["deleted_stale"] = sorted(reaped)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        raise SystemExit(0)
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
    if (
        mode == "inspect-vanish"
        and "label=openshell.ai/managed-by=openshell" in args
    ):
        state["vanish_on_inspect"] = True
    containers = list(state.get("containers", []))
    if mode == "openshell-retire" and state["ps_count"] >= 3:
        containers = []
        state["containers"] = containers
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
    if mode == "inspect-vanish" and state.pop("vanish_on_inspect", False):
        state["containers"] = [
            item for item in state.get("containers", []) if item["Id"] not in wanted
        ]
        state_path.write_text(json.dumps(state), encoding="utf-8")
        raise SystemExit(1)
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
        # Bound the live lease-drain wait tightly so drain behavior is exercised
        # within the harness subprocess timeout. Individual tests override this.
        "MAC_DEPLOY_DAEMON_LEASE_DRAIN_TIMEOUT_SECONDS": "1",
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
    assert "download" in block
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

    gate_main = block.split('elif mode == "quiesce":', 1)[1].split(
        "\n    else:", 1
    )[0]
    assert gate_main.index("reconcile_managed_task_sandboxes()") < gate_main.index(
        "prove_managed_openshell_inactive(runtimes)"
    )

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
        "MAC_DEPLOY_DAEMON_PRESERVATION_TIMEOUT_SECONDS",
        "MAC_DEPLOY_DAEMON_QUIESCENCE_TIMEOUT_SECONDS",
        "MAC_DEPLOY_DAEMON_LEASE_DRAIN_TIMEOUT_SECONDS",
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
    if shutil.which("ps") is None:
        pytest.skip("ps(1) is required to observe descendant termination state")
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


def test_retiring_openshell_task_container_is_awaited_before_receipt(
    tmp_path: Path,
) -> None:
    container_id = "r" * 64
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        docker=[_openshell_container(container_id, running=True)],
        docker_mode="openshell-retire",
    )
    receipt = _assert_success_marker(run)
    assert receipt["openshell_managed"]["final_state"] == "inactive"
    state = json.loads(run.docker_state.read_text(encoding="utf-8"))
    assert state["containers"] == []
    assert state["ps_count"] >= 4
    assert not any(" rm " in f" {line} " for line in _call_lines(run))


def test_openshell_container_vanishing_between_list_and_inspect_is_reproved(
    tmp_path: Path,
) -> None:
    container_id = "v" * 64
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        docker=[_openshell_container(container_id, running=True)],
        docker_mode="inspect-vanish",
    )
    receipt = _assert_success_marker(run)
    assert receipt["openshell_managed"]["final_state"] == "inactive"
    state = json.loads(run.docker_state.read_text(encoding="utf-8"))
    assert state["containers"] == []
    assert state["ps_count"] >= 4
    assert not any(" rm " in f" {line} " for line in _call_lines(run))


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


def _dead_pid() -> int:
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child exits immediately.
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def _managed_task_sandbox(
    name: str,
    *,
    owner: str = "mac",
    kind: str = "task",
    keep: str = "false",
    pid: int,
) -> dict[str, Any]:
    return {
        "name": name,
        "phase": "Ready",
        "labels": {
            "mac.owner": owner,
            "mac.kind": kind,
            "mac.keep": keep,
            "mac.pid": str(pid),
        },
    }


def test_stale_orphaned_task_sandbox_is_reconciled_before_receipt(
    tmp_path: Path,
) -> None:
    stale = _managed_task_sandbox("mac-task-orphaned-fixture", pid=_dead_pid())
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps([stale])},
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    assert proof["stable_inactive_observations"] == 2
    assert proof["reconciled"] == ["mac-task-orphaned-fixture"]
    assert proof["reconciled_count"] == 1
    assert (
        "openshell:sandbox delete mac-task-orphaned-fixture" in _call_lines(run)
    )
    _assert_no_secret(run)


def test_partially_labeled_task_sandboxes_are_never_reaped(
    tmp_path: Path,
) -> None:
    # Dead-PID sandboxes that fail any ownership signal (blank mac.keep, foreign
    # owner, unmanaged kind) are protective, so the gate certifies quiescence
    # without deleting or waiting on any of them.
    protected = [
        _managed_task_sandbox(
            "mac-task-missing-keep-fixture", keep="", pid=_dead_pid()
        ),
        _managed_task_sandbox(
            "mac-task-foreign-owner-fixture", owner="other", pid=_dead_pid()
        ),
        _managed_task_sandbox(
            "mac-task-unmanaged-kind-fixture", kind="gadget", pid=_dead_pid()
        ),
    ]
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps(protected)},
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    assert proof["stable_inactive_observations"] == 2
    assert proof["reconciled"] == []
    assert proof["reconciled_count"] == 0
    assert proof["lease_drain"]["classification"] == "no_active_lease"
    assert proof["lease_drain"]["leases"] == {}
    assert not any("sandbox delete" in line for line in _call_lines(run))
    _assert_no_secret(run)


def test_live_lease_owned_sandbox_still_active_at_deadline_fails_without_delete(
    tmp_path: Path,
) -> None:
    # A live, lease-owned task sandbox whose creator PID stays alive is active
    # work to drain. The gate waits within the bounded lease-drain window and,
    # when the lease is still renewing at the deadline, fails closed WITHOUT
    # ever deleting or interrupting the live sandbox and WITHOUT a receipt.
    live = _managed_task_sandbox(
        "mac-task-live-fixture",
        pid=os.getpid(),
    )
    live["labels"]["mac.lease.id"] = "lease_live_fixture"
    live["labels"]["mac.task.id"] = "task_live_fixture"
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        extra_env={
            "FAKE_STALE_SANDBOXES": json.dumps([live]),
            "MAC_DEPLOY_DAEMON_LEASE_DRAIN_TIMEOUT_SECONDS": "0.3",
        },
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert "active" in run.result.stderr and "lease" in run.result.stderr
    # The live sandbox is never deleted or archived: draining is a wait, never
    # a teardown.
    assert not any("sandbox delete" in line for line in _call_lines(run))
    assert not any("sandbox download" in line for line in _call_lines(run))
    _assert_no_secret(run)


def test_live_lease_owned_sandbox_that_drains_is_reconciled_and_certified(
    tmp_path: Path,
) -> None:
    # A live, lease-owned task sandbox whose executor finishes mid-drain reaches
    # a terminal lease (the sandbox is torn down and vanishes from inventory).
    # The gate must observe the lease drain, resume fix-forward, and certify
    # quiescence with lease-drain telemetry -- never deleting the live sandbox.
    live = _managed_task_sandbox(
        "mac-task-drain-fixture",
        pid=os.getpid(),
    )
    live["labels"]["mac.lease.id"] = "lease_drain_fixture"
    live["labels"]["mac.task.id"] = "task_drain_fixture"
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        extra_env={
            "FAKE_STALE_SANDBOXES": json.dumps([live]),
            "FAKE_LIVE_DRAIN_LISTS": "2",
            "MAC_DEPLOY_DAEMON_LEASE_DRAIN_TIMEOUT_SECONDS": "5",
        },
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    assert proof["stable_inactive_observations"] == 2
    assert proof["reconciled"] == []
    assert proof["reconciled_count"] == 0
    lease_drain = proof["lease_drain"]
    assert lease_drain["classification"] == "active_lease_drained"
    assert lease_drain["drain_passes"] >= 1
    assert lease_drain["wait_seconds"] >= 0.0
    lease = lease_drain["leases"]["mac-task-drain-fixture"]
    assert lease["lease_id"] == "lease_drain_fixture"
    assert lease["task_id"] == "task_drain_fixture"
    assert lease["lease_state"] == "terminal"
    # Draining is a wait for the lease to end, never a delete of live work.
    assert not any("sandbox delete" in line for line in _call_lines(run))
    _assert_no_secret(run)


def test_live_lease_owned_sandbox_is_never_interrupted_during_drain(
    tmp_path: Path,
) -> None:
    # Even while it fails closed on an undrained lease, the gate must never send
    # a delete or download for the live sandbox: a live task sandbox is never
    # interrupted.
    live = _managed_task_sandbox("mac-task-protected-live-fixture", pid=os.getpid())
    live["labels"]["mac.lease.id"] = "lease_protected"
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        extra_env={
            "FAKE_STALE_SANDBOXES": json.dumps([live]),
            "MAC_DEPLOY_DAEMON_LEASE_DRAIN_TIMEOUT_SECONDS": "0.3",
        },
    )
    assert run.result.returncode != 0
    assert not any(
        "mac-task-protected-live-fixture" in line
        and ("delete" in line or "download" in line)
        for line in _call_lines(run)
    )
    _assert_no_secret(run)


def test_multiple_stale_task_sandboxes_are_reaped_in_one_gate_pass(
    tmp_path: Path,
) -> None:
    stale = [
        _managed_task_sandbox("mac-task-alpha-fixture", pid=_dead_pid()),
        _managed_task_sandbox(
            "mac-hubverify-beta-fixture", kind="hubverify", pid=_dead_pid()
        ),
        _managed_task_sandbox(
            "mac-security-probe-gamma-fixture",
            kind="security-probe",
            pid=_dead_pid(),
        ),
    ]
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps(stale)},
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    assert proof["stable_inactive_observations"] == 2
    assert proof["reconciled"] == [
        "mac-hubverify-beta-fixture",
        "mac-security-probe-gamma-fixture",
        "mac-task-alpha-fixture",
    ]
    assert proof["reconciled_count"] == 3
    # Every listed sandbox was managed and each managed sandbox was reaped, so
    # the final quiescent observation sees an empty inventory.
    assert proof["scanned"] == 0
    assert proof["managed"] == 0
    delete_calls = [
        line
        for line in _call_lines(run)
        if line.startswith("openshell:sandbox delete ")
    ]
    assert sorted(delete_calls) == [
        "openshell:sandbox delete mac-hubverify-beta-fixture",
        "openshell:sandbox delete mac-security-probe-gamma-fixture",
        "openshell:sandbox delete mac-task-alpha-fixture",
    ]
    _assert_no_secret(run)


def test_each_managed_kind_is_reaped_when_its_recorded_pid_is_dead(
    tmp_path: Path,
) -> None:
    kinds = ("task", "hubverify", "codingcap", "runtime-smoke", "security-probe")
    stale = [
        _managed_task_sandbox(
            f"mac-{kind}-deadpid-fixture", kind=kind, pid=_dead_pid()
        )
        for kind in kinds
    ]
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps(stale)},
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    assert proof["stable_inactive_observations"] == 2
    assert proof["reconciled"] == sorted(
        f"mac-{kind}-deadpid-fixture" for kind in kinds
    )
    assert proof["reconciled_count"] == len(kinds)
    assert proof["scanned"] == 0
    assert proof["managed"] == 0
    delete_calls = {
        line
        for line in _call_lines(run)
        if line.startswith("openshell:sandbox delete ")
    }
    assert delete_calls == {
        f"openshell:sandbox delete mac-{kind}-deadpid-fixture" for kind in kinds
    }
    _assert_no_secret(run)


def test_all_falsey_keep_spellings_are_reaped_and_dead_truthy_task_is_archived(
    tmp_path: Path,
) -> None:
    falsey = ["0", "false", "no", "off", "FALSE", "Off", "No"]
    reapable = [
        _managed_task_sandbox(
            f"mac-task-falsey-{index}-fixture", keep=value, pid=_dead_pid()
        )
        for index, value in enumerate(falsey)
    ]
    preserved = _managed_task_sandbox(
        "mac-task-truthy-keep-fixture", keep="true", pid=_dead_pid()
    )
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps([*reapable, preserved])},
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    assert proof["stable_inactive_observations"] == 2
    expected_reconciled = {
        f"mac-task-falsey-{index}-fixture" for index in range(len(falsey))
    } | {"mac-task-truthy-keep-fixture"}
    assert proof["reconciled"] == sorted(expected_reconciled)
    assert proof["reconciled_count"] == len(expected_reconciled)
    assert proof["preserved"] == ["mac-task-truthy-keep-fixture"]
    assert proof["preserved_count"] == 1
    assert proof["scanned"] == 0
    assert proof["managed"] == 0
    delete_calls = {
        line
        for line in _call_lines(run)
        if line.startswith("openshell:sandbox delete ")
    }
    assert delete_calls == {
        f"openshell:sandbox delete mac-task-falsey-{index}-fixture"
        for index in range(len(falsey))
    } | {"openshell:sandbox delete mac-task-truthy-keep-fixture"}
    assert (
        "openshell:sandbox download mac-task-truthy-keep-fixture "
        "/sandbox"
    ) in "\n".join(_call_lines(run))
    recovery_dirs = list((run.mac_home / "openshell-recovery").iterdir())
    assert len(recovery_dirs) == 1
    manifest = json.loads(
        (recovery_dirs[0] / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schema"] == "mac.openshell.task_preservation.v1"
    assert manifest["sandbox"] == "mac-task-truthy-keep-fixture"
    assert (recovery_dirs[0] / "workspace" / "task.json").is_file()
    _assert_no_secret(run)


def test_protected_task_archive_has_a_separate_bounded_transfer_window(
    tmp_path: Path,
) -> None:
    protected = _managed_task_sandbox(
        "mac-task-slow-preserve-fixture", keep="true", pid=_dead_pid()
    )
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        openshell_mode="stale-download-slow",
        extra_env={
            "FAKE_STALE_SANDBOXES": json.dumps([protected]),
            # The ordinary command budget in the harness is one second. A
            # workspace transfer gets its own finite budget because archive
            # duration scales with workspace size.
            "MAC_DEPLOY_DAEMON_PRESERVATION_TIMEOUT_SECONDS": "2",
        },
    )
    receipt = _assert_success_marker(run)
    assert receipt["openshell_task_sandboxes"]["preserved"] == [
        "mac-task-slow-preserve-fixture"
    ]
    _assert_no_secret(run)


def test_dead_protected_task_download_failure_fails_closed_without_delete(
    tmp_path: Path,
) -> None:
    protected = _managed_task_sandbox(
        "mac-task-preserve-failure-fixture", keep="true", pid=_dead_pid()
    )
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        seed_marker=True,
        openshell_mode="stale-download-nonzero",
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps([protected])},
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    calls = _call_lines(run)
    assert any(
        line.startswith(
            "openshell:sandbox download mac-task-preserve-failure-fixture "
            "/sandbox"
        )
        for line in calls
    )
    assert not any(
        line == "openshell:sandbox delete mac-task-preserve-failure-fixture"
        for line in calls
    )
    _assert_no_secret(run)


def test_task_sandbox_reconciliation_runs_when_primary_sandbox_is_absent(
    tmp_path: Path,
) -> None:
    stale = _managed_task_sandbox("mac-task-no-openclaw-fixture", pid=_dead_pid())
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps([stale])},
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    assert proof["stable_inactive_observations"] == 2
    assert proof["reconciled"] == ["mac-task-no-openclaw-fixture"]
    assert proof["reconciled_count"] == 1
    assert proof["scanned"] == 0
    assert proof["managed"] == 0
    # The primary OpenClaw sandbox is absent (no authoritative artifact), so its
    # quiescence proof is a no-op while task-sandbox reconciliation still runs
    # and executes a real delete.
    openclaw = receipt["openclaw"]
    assert openclaw["sandbox"] is None
    assert openclaw["initial_state"] == "not_managed"
    assert openclaw["final_state"] == "absent"
    assert openclaw["stop_wrapper_invoked"] is False
    assert openclaw["delete_invoked"] is False
    delete_calls = [
        line
        for line in _call_lines(run)
        if line.startswith("openshell:sandbox delete ")
    ]
    assert delete_calls == ["openshell:sandbox delete mac-task-no-openclaw-fixture"]
    _assert_no_secret(run)


@pytest.mark.parametrize(
    "pid_label",
    ["not-a-pid", "-1", "0", "  ", "3.14", "0x10", ""],
)
def test_task_sandbox_with_non_positive_or_nonintegral_pid_is_never_reaped(
    tmp_path: Path, pid_label: str
) -> None:
    """A recorded ``mac.pid`` that is not a positive integer fails closed.

    Non-integer, negative, zero, blank, or (via a fully missing label) absent
    process identities can never prove the recorded owner is dead, so the
    sandbox must be preserved and never deleted, yet the gate still certifies.
    """

    sandbox = {
        "name": "mac-task-badpid-fixture",
        "phase": "Ready",
        "labels": {
            "mac.owner": "mac",
            "mac.kind": "task",
            "mac.keep": "false",
            "mac.pid": pid_label,
        },
    }
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps([sandbox])},
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    assert proof["reconciled"] == []
    assert proof["reconciled_count"] == 0
    # The sandbox is managed (name + owner match) but not reap-eligible.
    assert proof["managed"] == 1
    assert not any("sandbox delete" in line for line in _call_lines(run))
    _assert_no_secret(run)


def test_task_sandbox_with_missing_pid_label_is_never_reaped(
    tmp_path: Path,
) -> None:
    """A managed sandbox whose ``mac.pid`` label is entirely absent fails closed."""

    sandbox = {
        "name": "mac-task-nopid-fixture",
        "phase": "Ready",
        "labels": {
            "mac.owner": "mac",
            "mac.kind": "task",
            "mac.keep": "false",
        },
    }
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps([sandbox])},
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    assert proof["reconciled"] == []
    assert proof["reconciled_count"] == 0
    assert proof["managed"] == 1
    assert not any("sandbox delete" in line for line in _call_lines(run))
    _assert_no_secret(run)


@pytest.mark.parametrize("owner", ["other", "MAC-imposter", "", "hub"])
def test_task_sandbox_with_foreign_or_missing_owner_is_never_reaped(
    tmp_path: Path, owner: str
) -> None:
    """A foreign or blank ``mac.owner`` is never reaped (never reap on partial)."""

    sandbox = _managed_task_sandbox(
        "mac-task-foreign-fixture", owner=owner, pid=_dead_pid()
    )
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps([sandbox])},
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    assert proof["reconciled"] == []
    assert proof["reconciled_count"] == 0
    # A foreign or blank owner is not counted as MAC-managed.
    assert proof["managed"] == 0
    assert not any("sandbox delete" in line for line in _call_lines(run))
    _assert_no_secret(run)


def test_task_sandbox_with_missing_owner_label_is_never_reaped(
    tmp_path: Path,
) -> None:
    """A managed-name sandbox with no ``mac.owner`` label at all fails closed."""

    sandbox = {
        "name": "mac-task-ownerless-fixture",
        "phase": "Ready",
        "labels": {
            "mac.kind": "task",
            "mac.keep": "false",
            "mac.pid": str(_dead_pid()),
        },
    }
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps([sandbox])},
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    assert proof["reconciled"] == []
    assert proof["reconciled_count"] == 0
    assert proof["managed"] == 0
    assert not any("sandbox delete" in line for line in _call_lines(run))
    _assert_no_secret(run)


def test_stale_task_sandbox_delete_failure_fails_closed_without_receipt(
    tmp_path: Path,
) -> None:
    """A reap-eligible sandbox whose delete returns nonzero blocks the receipt.

    The deletion command failing (nonzero exit) must raise ``QuiescenceFailure``
    inside the gate, producing no receipt marker and disclosing no secret.
    """

    stale = _managed_task_sandbox("mac-task-delfail-fixture", pid=_dead_pid())
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        seed_marker=True,
        openshell_mode="stale-delete-nonzero",
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps([stale])},
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    # The delete was attempted (proving the classifier reaped it) but failed.
    assert (
        "openshell:sandbox delete mac-task-delfail-fixture" in _call_lines(run)
    )
    _assert_no_secret(run)


def test_persistently_stale_task_sandbox_trips_deadline_fail_closed(
    tmp_path: Path,
) -> None:
    """A stale sandbox that never becomes reap-free trips the deadline path.

    When deletes report success yet the reap-eligible sandbox keeps
    reappearing, reconciliation can never reach two consecutive stale-free
    observations and must fail closed with the stable ``survived phase-1``
    message, leaving no receipt and disclosing no secret.
    """

    stale = _managed_task_sandbox("mac-task-persist-fixture", pid=_dead_pid())
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        seed_marker=True,
        openshell_mode="stale-persist",
        extra_env={
            "FAKE_STALE_SANDBOXES": json.dumps([stale]),
            # Keep the deadline tight so the survival is proven quickly.
            "MAC_DEPLOY_DAEMON_QUIESCENCE_TIMEOUT_SECONDS": "1",
        },
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert (
        "stale OpenShell task sandboxes survived phase-1 quiescence"
        in run.result.stderr
    )
    # At least one delete was attempted before the deadline fired.
    assert any(
        line.startswith("openshell:sandbox delete mac-task-persist-fixture")
        for line in _call_lines(run)
    )
    _assert_no_secret(run)


def test_malformed_sandbox_labels_fail_inventory_closed(tmp_path: Path) -> None:
    """A non-mapping ``labels`` field aborts the inventory fail-closed."""

    stale = {
        "name": "mac-task-badlabels-fixture",
        "phase": "Ready",
        "labels": ["mac.owner=mac"],
    }
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        seed_marker=True,
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps([stale])},
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert not any("sandbox delete" in line for line in _call_lines(run))
    _assert_no_secret(run)


def test_malformed_inventory_json_fails_reconciliation_closed(
    tmp_path: Path,
) -> None:
    """Non-JSON inventory output during listing fails the gate closed."""

    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        openshell_mode="malformed",
        seed_marker=True,
    )
    assert run.result.returncode != 0
    assert not run.marker.exists()
    assert not any("sandbox delete" in line for line in _call_lines(run))
    _assert_no_secret(run)


def test_reconciled_names_are_deduplicated_across_multipass_observations(
    tmp_path: Path,
) -> None:
    """Across the multi-pass observation loop each reaped name appears once.

    The ``linger`` stale-delete mode acknowledges the first delete yet leaves
    the sandbox present, so each reap-eligible sandbox is re-listed and
    re-classified as reap-eligible on a subsequent pass before it finally
    disappears.  The reconcile proof must record each reconciled name exactly
    once (``seen`` de-duplication) even though it is deleted on more than one
    pass, so ``reconciled_count`` counts distinct names, not delete calls.
    """

    stale = [
        _managed_task_sandbox("mac-task-dedup-a-fixture", pid=_dead_pid()),
        _managed_task_sandbox("mac-task-dedup-b-fixture", pid=_dead_pid()),
    ]
    run = _run_quiescence(
        tmp_path,
        sandbox_source="none",
        sandbox_present=False,
        openshell_mode="stale-linger",
        extra_env={"FAKE_STALE_SANDBOXES": json.dumps(stale)},
    )
    receipt = _assert_success_marker(run)
    proof = receipt["openshell_task_sandboxes"]
    assert proof["final_state"] == "quiescent"
    # Each distinct reap-eligible name appears exactly once despite being
    # observed and deleted across multiple passes.
    assert proof["reconciled"] == [
        "mac-task-dedup-a-fixture",
        "mac-task-dedup-b-fixture",
    ]
    assert proof["reconciled_count"] == 2
    delete_calls = [
        line
        for line in _call_lines(run)
        if line.startswith("openshell:sandbox delete ")
    ]
    # ``linger`` forces two delete attempts per sandbox, proving multi-pass
    # observation, while the reconciled proof still de-duplicates the names.
    assert delete_calls.count("openshell:sandbox delete mac-task-dedup-a-fixture") >= 2
    assert delete_calls.count("openshell:sandbox delete mac-task-dedup-b-fixture") >= 2
    state = json.loads(run.openshell_state.read_text(encoding="utf-8"))
    assert state["stale_delete_per_name"]["mac-task-dedup-a-fixture"] >= 2
    assert state["stale_delete_per_name"]["mac-task-dedup-b-fixture"] >= 2
    _assert_no_secret(run)


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
