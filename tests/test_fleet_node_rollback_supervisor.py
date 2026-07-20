from __future__ import annotations

import contextlib
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, MutableMapping, Optional, Tuple

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "fleet-node-rollback-supervisor.py"

SYSTEMD_NAMES = {
    "control": "mac.service",
    "hermes": "mac-hermes-gateway.service",
    "openclaw": "mac-openclaw-gateway.service",
    "nemoclaw": "mac-nemoclaw-gateway.service",
    "agent": "mac-agent.service",
}
SUPERVISORD_NAMES = {
    key: value.removesuffix(".service") for key, value in SYSTEMD_NAMES.items()
}
LAUNCHD_NAMES = {
    "control": "com.mac.control-plane",
    "hermes": "com.mac.hermes-gateway",
    "openclaw": "com.mac.openclaw-gateway",
    "nemoclaw": "com.mac.nemoclaw-gateway",
    "agent": "com.mac.agent",
}
SYSTEM_SUPERVISOR = "com.mac.supervisor"


FAKE_MANAGER = r"""#!__PYTHON__
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root = Path(__file__).resolve().parent
state_path = root / "manager-state.json"

def load():
    return json.loads(state_path.read_text(encoding="utf-8"))

def save(state):
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, state_path)

state = load()
for selector in (
    "DBUS_SESSION_BUS_ADDRESS",
    "LAUNCHD_SOCKET",
    "SUPERVISOR_SERVER_URL",
    "SYSTEMD_UNIT_PATH",
    "XDG_RUNTIME_DIR",
):
    if selector in os.environ:
        print("SECRET-SELECTOR-LEAK", file=sys.stderr)
        raise SystemExit(97)

command = sys.argv[1] if len(sys.argv) > 1 else ""
if state.get("timeout_command") == command:
    marker = root / "descendant-terminated"
    child_code = (
        "import pathlib,signal,sys,time;"
        "p=pathlib.Path(sys.argv[1]);"
        "signal.signal(signal.SIGTERM,lambda *_:(p.write_text('yes'),sys.exit(0)));"
        "time.sleep(30)"
    )
    child = subprocess.Popen([sys.executable, "-c", child_code, str(marker)])
    (root / "descendant-pid").write_text(str(child.pid), encoding="utf-8")
    time.sleep(30)

manager = Path(sys.argv[0]).name
args = sys.argv[1:]

def next_pid():
    state["pid_counter"] = int(state.get("pid_counter", 4000)) + 1
    return state["pid_counter"]

if manager == "systemctl":
    services = state.setdefault("services", {})
    if command == "show":
        identity = args[1]
        item = services.get(identity, {
            "load": "not-found", "active": "inactive", "sub": "dead",
            "pid": 0, "restarts": 0, "enabled": "not-found",
        })
        print("LoadState=" + item.get("load", "loaded"))
        if state.get("ambiguous_show") == identity:
            print("LoadState=loaded")
        print("ActiveState=" + item.get("active", "inactive"))
        print("SubState=" + item.get("sub", "dead"))
        print("MainPID=" + str(item.get("pid", 0)))
        print("NRestarts=" + str(item.get("restarts", 0)))
        raise SystemExit(0 if item.get("load") != "not-found" else 4)
    if command == "stop":
        identity = args[-1]
        if identity not in state.get("fail_open_stop", []):
            item = services.setdefault(identity, {})
            item.update(active="inactive", sub="dead", pid=0)
            save(state)
        if state.get("secret_output_on_stop"):
            print("RAW-TOKEN-SHOULD-NOT-ESCAPE", file=sys.stderr)
        raise SystemExit(0)
    if command == "daemon-reload":
        raise SystemExit(0 if not state.get("fail_reload") else 1)
    if command == "disable":
        identity = args[-1]
        item = services.get(identity)
        if item is None:
            raise SystemExit(1)
        if identity not in state.get("fail_open_disable", []):
            item.update(active="inactive", sub="dead", pid=0)
            if identity not in state.get("preserve_indirect_disable", []):
                item["enabled"] = "disabled"
            save(state)
        raise SystemExit(0)
    if command == "enable":
        identity = args[-1]
        if identity not in services:
            raise SystemExit(1)
        services[identity]["enabled"] = "enabled"
        save(state)
        raise SystemExit(0)
    if command == "restart":
        identity = args[-1]
        if identity not in state.get("fail_open_start", []):
            item = services.setdefault(identity, {})
            item.update(load="loaded", active="active", sub="running", pid=next_pid())
            item.setdefault("restarts", 0)
            item.setdefault("enabled", "enabled")
            save(state)
        raise SystemExit(0)
    if command == "is-enabled":
        identity = args[-1]
        value = services.get(identity, {}).get("enabled", "not-found")
        print(value)
        raise SystemExit(0 if value == "enabled" else 1)
    raise SystemExit(2)

if manager == "supervisorctl":
    programs = state.setdefault("programs", {})
    if command == "status":
        identity = args[1]
        item = programs.get(identity)
        if item is None:
            print(identity + ": ERROR (no such process)", file=sys.stderr)
            raise SystemExit(3)
        program_state = item.get("state", "STOPPED")
        if program_state == "RUNNING":
            print("%s RUNNING pid %s, uptime 0:00:01" % (identity, item.get("pid", 0)))
        else:
            print("%s %s Not started" % (identity, program_state))
        if state.get("ambiguous_status") == identity:
            print("unrelated RUNNING pid 9999, uptime 0:00:01")
        raise SystemExit(0)
    if command == "stop":
        identity = args[1]
        if identity not in state.get("fail_open_stop", []) and identity in programs:
            programs[identity].update(state="STOPPED", pid=0)
            save(state)
        raise SystemExit(0)
    if command in {"reread", "update"}:
        raise SystemExit(1 if state.get("fail_" + command) else 0)
    if command == "start":
        identity = args[1]
        if identity not in programs:
            raise SystemExit(1)
        if identity not in state.get("fail_open_start", []):
            programs[identity].update(state="RUNNING", pid=next_pid())
            save(state)
        raise SystemExit(0)
    raise SystemExit(2)

if manager == "launchctl":
    jobs = state.setdefault("jobs", {})
    if command == "print":
        target = args[1]
        item = jobs.get(target)
        if item is None:
            if state.get("canonical_macos_absent") == target:
                print("Bad request.", file=sys.stderr)
                print('Could not find service "com.mac.agent" in domain for user gui: 501', file=sys.stderr)
            elif state.get("ambiguous_absent") == target:
                print("Could not find service; extra detail", file=sys.stderr)
                print("second line", file=sys.stderr)
            else:
                print("Could not find service " + target, file=sys.stderr)
            raise SystemExit(113)
        job_state = item.get("state", "running")
        if state.get("transient_launchd") == target:
            job_state = "waiting (throttled: 1)"
        if state.get("malformed_launchd") == target:
            job_state = "running\x7funsafe"
        print(target + " = {")
        print("    state = " + job_state)
        if state.get("duplicate_launchd_state") == target:
            print("    state = running")
        if item.get("pid") is not None:
            print("    pid = " + str(item["pid"]))
        print("}")
        raise SystemExit(0)
    if command == "bootout":
        target = args[1]
        if target not in state.get("fail_open_bootout", []):
            jobs.pop(target, None)
            save(state)
        raise SystemExit(0)
    if command == "enable":
        raise SystemExit(0)
    if command == "bootstrap":
        domain = args[1]
        label = Path(args[2]).stem
        target = domain + "/" + label
        if target in jobs:
            raise SystemExit(5)
        jobs[target] = {"state": "running", "pid": next_pid()}
        if state.get("inject_duplicate_control") and label == state.get("control_label"):
            uid = str(state.get("uid", 501))
            duplicate_domain = ("gui/" + uid) if domain == "system" else "system"
            jobs[duplicate_domain + "/" + label] = {"state": "running", "pid": next_pid()}
        save(state)
        raise SystemExit(0)
    if command == "kickstart":
        target = args[-1]
        if target not in jobs:
            raise SystemExit(6)
        jobs[target]["state"] = "running"
        jobs[target].setdefault("pid", next_pid())
        save(state)
        raise SystemExit(0)
    raise SystemExit(2)

raise SystemExit(2)
"""


def _write_manager(tmp_path: Path, name: str) -> Path:
    manager = tmp_path / name
    manager.write_text(
        FAKE_MANAGER.replace("__PYTHON__", sys.executable),
        encoding="utf-8",
    )
    manager.chmod(0o755)
    return manager


def _write_state(tmp_path: Path, payload: Mapping[str, object]) -> Path:
    path = tmp_path / "manager-state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _read_state(tmp_path: Path) -> MutableMapping[str, object]:
    return json.loads((tmp_path / "manager-state.json").read_text(encoding="utf-8"))


def _closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _HealthyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/health":
            self.send_response(204)
        else:
            self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextlib.contextmanager
def _healthy_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _loaded_systemd_state() -> Dict[str, object]:
    services: Dict[str, object] = {}
    for index, identity in enumerate(SYSTEMD_NAMES.values(), start=1):
        services[identity] = {
            "load": "loaded",
            "active": "active",
            "sub": "running",
            "pid": 1000 + index,
            "restarts": 0,
            "enabled": "enabled",
        }
    return {"services": services}


def _stopped_systemd_state() -> Dict[str, object]:
    services: Dict[str, object] = {}
    for identity in SYSTEMD_NAMES.values():
        services[identity] = {
            "load": "loaded",
            "active": "inactive",
            "sub": "dead",
            "pid": 0,
            "restarts": 0,
            "enabled": "disabled",
        }
    return {"services": services}


def _base_command(
    tmp_path: Path,
    action: str,
    supervisor: str,
    port: int,
    names: Mapping[str, str],
    control_plane_mode: Optional[str] = None,
) -> Tuple[List[str], Path]:
    if control_plane_mode is None:
        control_plane_mode = "gui" if supervisor == "launchd" else "active"
    receipt = tmp_path / (supervisor + "-" + action + "-receipt.json")
    command = [
        sys.executable,
        str(HELPER),
        action,
        "--supervisor",
        supervisor,
        "--control-plane-mode",
        control_plane_mode,
        "--control-plane",
        names["control"],
        "--hermes-gateway",
        names["hermes"],
        "--openclaw-gateway",
        names["openclaw"],
        "--nemoclaw-gateway",
        names["nemoclaw"],
        "--agent",
        names["agent"],
        "--control-plane-port",
        str(port),
        "--receipt",
        str(receipt),
        # Happy-path budgets. Kept generous so ambient CPU contention (parallel
        # xdist workers, or a co-scheduled suite) can't expire a deadline on a
        # run that is actually succeeding — the earlier flakiness. Tests that
        # deliberately prove deadline/timeout EXPIRY override these with their
        # own short values downstream (argparse honors the last occurrence), so
        # their timing fidelity is unaffected.
        "--deadline-seconds",
        "8",
        "--compensation-deadline-seconds",
        "12",
        "--command-timeout-seconds",
        "3",
        "--poll-seconds",
        "0.02",
        "--stable-observations",
        "2",
        "--sudo-mode",
        "never",
    ]
    if action == "restore":
        command.extend(
            [
                "--active-gateway",
                "hermes",
                "--agent-prior-state",
                "active",
            ]
        )
    return command, receipt


def _run(
    command: List[str], *, timeout: float = 60.0
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # coverage.py's ``patch = ["subprocess"]`` propagates measurement into every
    # Python child via these vars + a site .pth hook. The helper and the fake
    # manager live outside ``source = ["src/mac"]``, so tracing them yields ZERO
    # coverage while making each interpreter start ~5.6x slower (~+230ms/spawn).
    # The helper spawns the manager repeatedly under a 1s command timeout / 2s
    # deadline, so that overhead — amplified by parallel xdist contention — is
    # what expires the deadline and flakes these tests. Strip it so the throwaway
    # subprocesses run at native speed and the tight deadlines stay meaningful.
    env.pop("COVERAGE_PROCESS_START", None)
    env.pop("COVERAGE_PROCESS_CONFIG", None)
    # These selectors must not reach a manager even if the rollback caller's
    # environment was contaminated.
    env.update(
        SYSTEMD_UNIT_PATH="/secret/systemd",
        SUPERVISOR_SERVER_URL="unix:///secret/supervisor.sock",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/secret/dbus",
        XDG_RUNTIME_DIR="/secret/runtime",
    )
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=timeout,
        check=False,
    )


def _replace_option(command: List[str], option: str, value: str) -> None:
    command[command.index(option) + 1] = value


def _assert_passed_receipt(
    receipt: Path, action: str, supervisor: str
) -> Mapping[str, object]:
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["schema"] == "mac.fleet_node_rollback_supervisor.v1"
    assert payload["status"] == "passed"
    assert payload["action"] == action
    assert payload["supervisor"] == supervisor
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert "secret" not in receipt.read_text(encoding="utf-8").lower()
    return payload


def test_systemd_quiesce_proves_all_services_inactive_and_scrubs_selectors(
    tmp_path: Path,
) -> None:
    systemctl = _write_manager(tmp_path, "systemctl")
    _write_state(tmp_path, _loaded_systemd_state())
    command, receipt = _base_command(
        tmp_path, "quiesce", "systemd", _closed_port(), SYSTEMD_NAMES
    )
    command.extend(["--systemctl", str(systemctl)])

    result = _run(command)

    assert result.returncode == 0, result.stderr
    payload = _assert_passed_receipt(receipt, "quiesce", "systemd")
    assert payload["control_plane"]["observed"] == "closed"
    state = _read_state(tmp_path)
    for item in state["services"].values():
        assert item["active"] == "inactive"
        assert item["pid"] == 0


def test_systemd_quiesce_stops_explicit_auxiliary_media_before_restore(
    tmp_path: Path,
) -> None:
    systemctl = _write_manager(tmp_path, "systemctl")
    state = _loaded_systemd_state()
    media = "mac-gen-server.service"
    state["services"][media] = {
        "load": "loaded",
        "active": "active",
        "sub": "running",
        "pid": 8189,
        "restarts": 0,
        "enabled": "enabled",
    }
    _write_state(tmp_path, state)
    command, receipt = _base_command(
        tmp_path, "quiesce", "systemd", _closed_port(), SYSTEMD_NAMES
    )
    command.extend(
        ["--systemctl", str(systemctl), "--auxiliary-service", media]
    )

    result = _run(command)

    assert result.returncode == 0, result.stderr
    payload = _assert_passed_receipt(receipt, "quiesce", "systemd")
    assert payload["services"]["auxiliary_0"] == {
        "identity": media,
        "expected": "inactive",
        "observed": "inactive",
    }
    assert _read_state(tmp_path)["services"][media]["active"] == "inactive"


def test_systemd_quiesce_rejects_fail_open_stop_without_leaking_output(
    tmp_path: Path,
) -> None:
    systemctl = _write_manager(tmp_path, "systemctl")
    state = _loaded_systemd_state()
    state["fail_open_stop"] = [SYSTEMD_NAMES["agent"]]
    state["secret_output_on_stop"] = True
    _write_state(tmp_path, state)
    command, receipt = _base_command(
        tmp_path, "quiesce", "systemd", _closed_port(), SYSTEMD_NAMES
    )
    command.extend(
        [
            "--systemctl",
            str(systemctl),
            "--deadline-seconds",
            "0.35",
            "--command-timeout-seconds",
            "0.5",
        ]
    )

    result = _run(command)

    assert result.returncode == 1
    assert not receipt.exists()
    assert "RAW-TOKEN" not in result.stderr
    assert "SECRET-SELECTOR" not in result.stderr


def test_systemd_restore_proves_exact_healthy_topology(tmp_path: Path) -> None:
    systemctl = _write_manager(tmp_path, "systemctl")
    _write_state(tmp_path, _stopped_systemd_state())
    with _healthy_server() as port:
        command, receipt = _base_command(
            tmp_path, "restore", "systemd", port, SYSTEMD_NAMES
        )
        command.extend(["--systemctl", str(systemctl)])
        result = _run(command)

    assert result.returncode == 0, result.stderr
    payload = _assert_passed_receipt(receipt, "restore", "systemd")
    assert payload["control_plane"]["observed"] == "healthy"
    state = _read_state(tmp_path)["services"]
    for key in ("control", "hermes", "agent"):
        assert state[SYSTEMD_NAMES[key]]["active"] == "active"
        assert state[SYSTEMD_NAMES[key]]["enabled"] == "enabled"
    for key in ("openclaw", "nemoclaw"):
        assert state[SYSTEMD_NAMES[key]]["active"] == "inactive"
        assert state[SYSTEMD_NAMES[key]]["enabled"] == "disabled"


def test_systemd_restore_keeps_indirect_auxiliary_media_safely_inactive(
    tmp_path: Path,
) -> None:
    systemctl = _write_manager(tmp_path, "systemctl")
    state = _stopped_systemd_state()
    media = "mac-gen-server.service"
    state["services"][media] = {
        "load": "loaded",
        "active": "inactive",
        "sub": "dead",
        "pid": 0,
        "restarts": 0,
        "enabled": "indirect",
    }
    state["preserve_indirect_disable"] = [media]
    _write_state(tmp_path, state)
    with _healthy_server() as port:
        command, receipt = _base_command(
            tmp_path, "restore", "systemd", port, SYSTEMD_NAMES
        )
        command.extend(
            ["--systemctl", str(systemctl), "--auxiliary-service", media]
        )
        result = _run(command)

    assert result.returncode == 0, result.stderr
    payload = _assert_passed_receipt(receipt, "restore", "systemd")
    assert payload["services"]["auxiliary_0"]["observed"] == "inactive"
    restored = _read_state(tmp_path)["services"][media]
    assert restored["active"] == "inactive"
    assert restored["enabled"] == "indirect"


def test_systemd_restore_requiesces_a_partial_start_failure(tmp_path: Path) -> None:
    systemctl = _write_manager(tmp_path, "systemctl")
    state = _stopped_systemd_state()
    # Control and Hermes start before the agent fails open. Compensation must
    # stop both already-started identities, not merely withhold the receipt.
    state["fail_open_start"] = [SYSTEMD_NAMES["agent"]]
    _write_state(tmp_path, state)
    command, receipt = _base_command(
        tmp_path, "restore", "systemd", _closed_port(), SYSTEMD_NAMES
    )
    command.extend(
        [
            "--systemctl",
            str(systemctl),
            "--deadline-seconds",
            "0.4",
        ]
    )
    result = _run(command)

    assert result.returncode == 1
    assert "compensation re-quiesced every exact service identity" in result.stderr
    assert not receipt.exists()
    for item in _read_state(tmp_path)["services"].values():
        assert item["active"] == "inactive"
        assert item["pid"] == 0


def test_systemd_restore_reports_sanitized_compensation_failure(
    tmp_path: Path,
) -> None:
    systemctl = _write_manager(tmp_path, "systemctl")
    state = _stopped_systemd_state()
    state["fail_open_start"] = [SYSTEMD_NAMES["agent"]]
    state["fail_open_stop"] = [SYSTEMD_NAMES["control"]]
    state["secret_output_on_stop"] = True
    _write_state(tmp_path, state)
    command, receipt = _base_command(
        tmp_path, "restore", "systemd", _closed_port(), SYSTEMD_NAMES
    )
    command.extend(
        [
            "--systemctl",
            str(systemctl),
            "--deadline-seconds",
            "2",
            "--command-timeout-seconds",
            "0.5",
        ]
    )

    result = _run(command)

    assert result.returncode == 1
    assert "restore failed (" in result.stderr
    assert "); compensation failed (" in result.stderr
    assert "RAW-TOKEN-SHOULD-NOT-ESCAPE" not in result.stderr
    assert not receipt.exists()


def test_systemd_spoke_restore_keeps_control_plane_and_port_inactive(
    tmp_path: Path,
) -> None:
    systemctl = _write_manager(tmp_path, "systemctl")
    _write_state(tmp_path, _stopped_systemd_state())
    command, receipt = _base_command(
        tmp_path,
        "restore",
        "systemd",
        _closed_port(),
        SYSTEMD_NAMES,
        "inactive",
    )
    command.extend(["--systemctl", str(systemctl)])

    result = _run(command)

    assert result.returncode == 0, result.stderr
    payload = _assert_passed_receipt(receipt, "restore", "systemd")
    assert payload["control_plane"]["mode"] == "inactive"
    assert payload["control_plane"]["observed"] == "closed"
    state = _read_state(tmp_path)["services"]
    assert state[SYSTEMD_NAMES["control"]]["active"] == "inactive"
    assert state[SYSTEMD_NAMES["control"]]["enabled"] == "disabled"
    assert state[SYSTEMD_NAMES["hermes"]]["active"] == "active"
    assert state[SYSTEMD_NAMES["agent"]]["active"] == "active"


@pytest.mark.parametrize("gateway", ["openclaw", "nemoclaw", "none"])
def test_systemd_restore_recreates_the_explicit_prior_gateway_owner(
    tmp_path: Path,
    gateway: str,
) -> None:
    systemctl = _write_manager(tmp_path, "systemctl")
    _write_state(tmp_path, _stopped_systemd_state())
    command, receipt = _base_command(
        tmp_path,
        "restore",
        "systemd",
        _closed_port(),
        SYSTEMD_NAMES,
        "inactive",
    )
    _replace_option(command, "--active-gateway", gateway)
    _replace_option(command, "--agent-prior-state", "inactive")
    command.extend(["--systemctl", str(systemctl)])

    result = _run(command)

    assert result.returncode == 0, result.stderr
    payload = _assert_passed_receipt(receipt, "restore", "systemd")
    assert payload["prior_topology"] == {
        "active_gateway": gateway,
        "agent_state": "inactive",
    }
    state = _read_state(tmp_path)["services"]
    for name in ("hermes", "openclaw", "nemoclaw"):
        expected_active = gateway == name
        assert (state[SYSTEMD_NAMES[name]]["active"] == "active") is expected_active
        assert (state[SYSTEMD_NAMES[name]]["enabled"] == "enabled") is expected_active
    assert state[SYSTEMD_NAMES["control"]]["active"] == "inactive"
    assert state[SYSTEMD_NAMES["agent"]]["active"] == "inactive"


def test_supervisord_rejects_ambiguous_status(tmp_path: Path) -> None:
    supervisorctl = _write_manager(tmp_path, "supervisorctl")
    programs = {
        identity: {"state": "RUNNING", "pid": 2000 + index}
        for index, identity in enumerate(SUPERVISORD_NAMES.values())
    }
    _write_state(
        tmp_path,
        {
            "programs": programs,
            "ambiguous_status": SUPERVISORD_NAMES["agent"],
        },
    )
    command, receipt = _base_command(
        tmp_path, "quiesce", "supervisord", _closed_port(), SUPERVISORD_NAMES
    )
    command.extend(["--supervisorctl", str(supervisorctl)])

    result = _run(command)

    assert result.returncode == 1
    assert "ambiguous" in result.stderr
    assert not receipt.exists()


@pytest.mark.parametrize("inactive_state", ["EXITED", "FATAL"])
def test_supervisord_quiesce_accepts_stable_process_free_states(
    tmp_path: Path,
    inactive_state: str,
) -> None:
    supervisorctl = _write_manager(tmp_path, "supervisorctl")
    programs = {
        identity: {"state": "STOPPED", "pid": 0}
        for identity in SUPERVISORD_NAMES.values()
    }
    programs[SUPERVISORD_NAMES["hermes"]] = {
        "state": inactive_state,
        "pid": 0,
    }
    _write_state(tmp_path, {"programs": programs})
    command, receipt = _base_command(
        tmp_path,
        "quiesce",
        "supervisord",
        _closed_port(),
        SUPERVISORD_NAMES,
    )
    command.extend(["--supervisorctl", str(supervisorctl)])

    result = _run(command)

    assert result.returncode == 0, result.stderr
    _assert_passed_receipt(receipt, "quiesce", "supervisord")


def test_supervisord_restore_proves_successors_stopped_and_active_pids_stable(
    tmp_path: Path,
) -> None:
    supervisorctl = _write_manager(tmp_path, "supervisorctl")
    programs = {
        identity: {"state": "STOPPED", "pid": 0}
        for identity in SUPERVISORD_NAMES.values()
    }
    _write_state(tmp_path, {"programs": programs})
    with _healthy_server() as port:
        command, receipt = _base_command(
            tmp_path, "restore", "supervisord", port, SUPERVISORD_NAMES
        )
        command.extend(["--supervisorctl", str(supervisorctl)])
        result = _run(command)

    assert result.returncode == 0, result.stderr
    _assert_passed_receipt(receipt, "restore", "supervisord")
    state = _read_state(tmp_path)["programs"]
    for key in ("control", "hermes", "agent"):
        assert state[SUPERVISORD_NAMES[key]]["state"] == "RUNNING"
        assert state[SUPERVISORD_NAMES[key]]["pid"] > 0
    for key in ("openclaw", "nemoclaw"):
        assert state[SUPERVISORD_NAMES[key]]["state"] == "STOPPED"


def test_supervisord_restore_requiesces_a_partial_start_failure(
    tmp_path: Path,
) -> None:
    supervisorctl = _write_manager(tmp_path, "supervisorctl")
    programs = {
        identity: {"state": "STOPPED", "pid": 0}
        for identity in SUPERVISORD_NAMES.values()
    }
    _write_state(
        tmp_path,
        {
            "programs": programs,
            "fail_open_start": [SUPERVISORD_NAMES["hermes"]],
        },
    )
    command, receipt = _base_command(
        tmp_path,
        "restore",
        "supervisord",
        _closed_port(),
        SUPERVISORD_NAMES,
    )
    command.extend(
        [
            "--supervisorctl",
            str(supervisorctl),
            "--deadline-seconds",
            "0.4",
        ]
    )
    result = _run(command)

    assert result.returncode == 1
    assert "compensation re-quiesced every exact service identity" in result.stderr
    assert not receipt.exists()
    for item in _read_state(tmp_path)["programs"].values():
        assert item["state"] == "STOPPED"
        assert item["pid"] == 0


def test_supervisord_spoke_restore_keeps_control_plane_and_port_inactive(
    tmp_path: Path,
) -> None:
    supervisorctl = _write_manager(tmp_path, "supervisorctl")
    programs = {
        identity: {"state": "STOPPED", "pid": 0}
        for identity in SUPERVISORD_NAMES.values()
    }
    _write_state(tmp_path, {"programs": programs})
    command, receipt = _base_command(
        tmp_path,
        "restore",
        "supervisord",
        _closed_port(),
        SUPERVISORD_NAMES,
        "inactive",
    )
    command.extend(["--supervisorctl", str(supervisorctl)])

    result = _run(command)

    assert result.returncode == 0, result.stderr
    payload = _assert_passed_receipt(receipt, "restore", "supervisord")
    assert payload["control_plane"]["mode"] == "inactive"
    assert payload["control_plane"]["observed"] == "closed"
    state = _read_state(tmp_path)["programs"]
    assert state[SUPERVISORD_NAMES["control"]]["state"] == "STOPPED"
    assert state[SUPERVISORD_NAMES["hermes"]]["state"] == "RUNNING"
    assert state[SUPERVISORD_NAMES["agent"]]["state"] == "RUNNING"


def test_supervisord_restore_recreates_openclaw_without_an_agent(
    tmp_path: Path,
) -> None:
    supervisorctl = _write_manager(tmp_path, "supervisorctl")
    programs = {
        identity: {"state": "STOPPED", "pid": 0}
        for identity in SUPERVISORD_NAMES.values()
    }
    _write_state(tmp_path, {"programs": programs})
    command, receipt = _base_command(
        tmp_path,
        "restore",
        "supervisord",
        _closed_port(),
        SUPERVISORD_NAMES,
        "inactive",
    )
    _replace_option(command, "--active-gateway", "openclaw")
    _replace_option(command, "--agent-prior-state", "absent")
    command.extend(["--supervisorctl", str(supervisorctl)])

    result = _run(command)

    assert result.returncode == 0, result.stderr
    _assert_passed_receipt(receipt, "restore", "supervisord")
    state = _read_state(tmp_path)["programs"]
    assert state[SUPERVISORD_NAMES["openclaw"]]["state"] == "RUNNING"
    for name in ("control", "hermes", "nemoclaw", "agent"):
        assert state[SUPERVISORD_NAMES[name]]["state"] == "STOPPED"


def _launchd_args(
    tmp_path: Path,
    action: str,
    port: int,
    launchctl: Path,
    control_plane_mode: str = "gui",
) -> Tuple[List[str], Path]:
    command, receipt = _base_command(
        tmp_path,
        action,
        "launchd",
        port,
        LAUNCHD_NAMES,
        control_plane_mode,
    )
    command.extend(
        [
            "--launchctl",
            str(launchctl),
            "--launchd-uid",
            "501",
            "--launchd-system-supervisor",
            SYSTEM_SUPERVISOR,
        ]
    )
    _replace_option(command, "--deadline-seconds", "6")
    return command, receipt


def _plist(tmp_path: Path, label: str) -> Path:
    path = tmp_path / (label + ".plist")
    path.write_text("plist fixture", encoding="utf-8")
    return path


def _restore_launchd_plist_args(tmp_path: Path, *, system: bool) -> List[str]:
    args = [
        "--launchd-hermes-plist",
        str(_plist(tmp_path, LAUNCHD_NAMES["hermes"])),
        "--launchd-agent-plist",
        str(_plist(tmp_path, LAUNCHD_NAMES["agent"])),
    ]
    if system:
        args.extend(
            [
                "--launchd-control-system-plist",
                str(_plist(tmp_path, LAUNCHD_NAMES["control"])),
                "--launchd-system-supervisor-was-active",
                "--launchd-system-supervisor-plist",
                str(_plist(tmp_path, SYSTEM_SUPERVISOR)),
            ]
        )
    else:
        args.extend(
            [
                "--launchd-control-gui-plist",
                str(_plist(tmp_path, LAUNCHD_NAMES["control"])),
            ]
        )
    return args


def test_launchd_quiesce_removes_both_control_domains_and_system_supervisor(
    tmp_path: Path,
) -> None:
    launchctl = _write_manager(tmp_path, "launchctl")
    jobs: Dict[str, object] = {}
    for domain in ("system", "gui/501"):
        for index, identity in enumerate(LAUNCHD_NAMES.values(), start=1):
            jobs[domain + "/" + identity] = {"state": "running", "pid": 3000 + index}
    jobs["system/" + SYSTEM_SUPERVISOR] = {"state": "running", "pid": 3999}
    _write_state(tmp_path, {"jobs": jobs})
    command, receipt = _launchd_args(tmp_path, "quiesce", _closed_port(), launchctl)

    result = _run(command)

    assert result.returncode == 0, result.stderr
    _assert_passed_receipt(receipt, "quiesce", "launchd")
    assert _read_state(tmp_path)["jobs"] == {}


@pytest.mark.parametrize("system", [False, True])
def test_launchd_restore_supports_explicit_gui_and_system_topologies(
    tmp_path: Path,
    system: bool,
) -> None:
    launchctl = _write_manager(tmp_path, "launchctl")
    _write_state(tmp_path, {"jobs": {}, "uid": 501})
    with _healthy_server() as port:
        mode = "system" if system else "gui"
        command, receipt = _launchd_args(tmp_path, "restore", port, launchctl, mode)
        command.extend(_restore_launchd_plist_args(tmp_path, system=system))
        result = _run(command)

    assert result.returncode == 0, result.stderr
    _assert_passed_receipt(receipt, "restore", "launchd")
    jobs = _read_state(tmp_path)["jobs"]
    selected_domain = "system" if system else "gui/501"
    other_domain = "gui/501" if system else "system"
    assert selected_domain + "/" + LAUNCHD_NAMES["control"] in jobs
    assert other_domain + "/" + LAUNCHD_NAMES["control"] not in jobs
    assert "gui/501/" + LAUNCHD_NAMES["hermes"] in jobs
    assert "gui/501/" + LAUNCHD_NAMES["agent"] in jobs
    assert ("system/" + SYSTEM_SUPERVISOR in jobs) is system


def test_launchd_restore_rejects_duplicate_control_plane(tmp_path: Path) -> None:
    launchctl = _write_manager(tmp_path, "launchctl")
    _write_state(
        tmp_path,
        {
            "jobs": {},
            "uid": 501,
            "inject_duplicate_control": True,
            "control_label": LAUNCHD_NAMES["control"],
        },
    )
    command, receipt = _launchd_args(tmp_path, "restore", _closed_port(), launchctl)
    command.extend(_restore_launchd_plist_args(tmp_path, system=False))
    command.extend(["--deadline-seconds", "1.2"])
    result = _run(command)

    assert result.returncode == 1
    assert "compensation re-quiesced every exact service identity" in result.stderr
    assert not receipt.exists()
    jobs = _read_state(tmp_path)["jobs"]
    assert jobs == {}


def test_launchd_spoke_restore_keeps_both_control_domains_and_port_inactive(
    tmp_path: Path,
) -> None:
    launchctl = _write_manager(tmp_path, "launchctl")
    _write_state(tmp_path, {"jobs": {}, "uid": 501})
    command, receipt = _launchd_args(
        tmp_path,
        "restore",
        _closed_port(),
        launchctl,
        "inactive",
    )
    command.extend(
        [
            "--launchd-hermes-plist",
            str(_plist(tmp_path, LAUNCHD_NAMES["hermes"])),
            "--launchd-agent-plist",
            str(_plist(tmp_path, LAUNCHD_NAMES["agent"])),
        ]
    )

    result = _run(command)

    assert result.returncode == 0, result.stderr
    payload = _assert_passed_receipt(receipt, "restore", "launchd")
    assert payload["control_plane"]["mode"] == "inactive"
    assert payload["control_plane"]["observed"] == "closed"
    jobs = _read_state(tmp_path)["jobs"]
    assert "system/" + LAUNCHD_NAMES["control"] not in jobs
    assert "gui/501/" + LAUNCHD_NAMES["control"] not in jobs
    assert "gui/501/" + LAUNCHD_NAMES["hermes"] in jobs
    assert "gui/501/" + LAUNCHD_NAMES["agent"] in jobs
    assert "system/" + SYSTEM_SUPERVISOR not in jobs


def test_launchd_restore_recreates_openclaw_without_an_agent(tmp_path: Path) -> None:
    launchctl = _write_manager(tmp_path, "launchctl")
    _write_state(tmp_path, {"jobs": {}, "uid": 501})
    command, receipt = _launchd_args(
        tmp_path,
        "restore",
        _closed_port(),
        launchctl,
        "inactive",
    )
    _replace_option(command, "--active-gateway", "openclaw")
    _replace_option(command, "--agent-prior-state", "inactive")
    command.extend(
        [
            "--launchd-openclaw-plist",
            str(_plist(tmp_path, LAUNCHD_NAMES["openclaw"])),
        ]
    )

    result = _run(command)

    assert result.returncode == 0, result.stderr
    payload = _assert_passed_receipt(receipt, "restore", "launchd")
    assert payload["prior_topology"] == {
        "active_gateway": "openclaw",
        "agent_state": "inactive",
    }
    jobs = _read_state(tmp_path)["jobs"]
    assert "gui/501/" + LAUNCHD_NAMES["openclaw"] in jobs
    assert "gui/501/" + LAUNCHD_NAMES["hermes"] not in jobs
    assert "gui/501/" + LAUNCHD_NAMES["nemoclaw"] not in jobs
    assert "gui/501/" + LAUNCHD_NAMES["agent"] not in jobs


def test_launchd_quiesces_well_formed_transient_job_state(tmp_path: Path) -> None:
    launchctl = _write_manager(tmp_path, "launchctl")
    target = "gui/501/" + LAUNCHD_NAMES["agent"]
    _write_state(
        tmp_path,
        {
            "jobs": {target: {"state": "running", "pid": 5001}},
            "transient_launchd": target,
        },
    )
    command, receipt = _launchd_args(tmp_path, "quiesce", _closed_port(), launchctl)

    result = _run(command)

    assert result.returncode == 0, result.stderr
    _assert_passed_receipt(receipt, "quiesce", "launchd")
    assert _read_state(tmp_path)["jobs"] == {}


@pytest.mark.parametrize("mode", ["malformed_launchd", "duplicate_launchd_state"])
def test_launchd_rejects_malformed_or_duplicate_job_state(
    tmp_path: Path,
    mode: str,
) -> None:
    launchctl = _write_manager(tmp_path, "launchctl")
    target = "gui/501/" + LAUNCHD_NAMES["agent"]
    _write_state(
        tmp_path,
        {
            "jobs": {target: {"state": "running", "pid": 5001}},
            mode: target,
        },
    )
    command, receipt = _launchd_args(tmp_path, "quiesce", _closed_port(), launchctl)

    result = _run(command)

    assert result.returncode == 1
    assert not receipt.exists()


def test_launchd_accepts_canonical_macos_two_line_absent_state(
    tmp_path: Path,
) -> None:
    launchctl = _write_manager(tmp_path, "launchctl")
    target = "gui/501/" + LAUNCHD_NAMES["agent"]
    _write_state(tmp_path, {"jobs": {}, "canonical_macos_absent": target})
    command, receipt = _launchd_args(
        tmp_path, "quiesce", _closed_port(), launchctl
    )

    result = _run(command)

    assert result.returncode == 0, result.stderr
    _assert_passed_receipt(receipt, "quiesce", "launchd")


def test_launchd_rejects_noncanonical_multiline_absent_state(
    tmp_path: Path,
) -> None:
    launchctl = _write_manager(tmp_path, "launchctl")
    target = "gui/501/" + LAUNCHD_NAMES["agent"]
    _write_state(tmp_path, {"jobs": {}, "ambiguous_absent": target})
    command, receipt = _launchd_args(
        tmp_path, "quiesce", _closed_port(), launchctl
    )

    result = _run(command)

    assert result.returncode == 1
    assert "ambiguous absent state" in result.stderr
    assert not receipt.exists()


def test_manager_timeout_terminates_descendant_process_group(tmp_path: Path) -> None:
    systemctl = _write_manager(tmp_path, "systemctl")
    state = _loaded_systemd_state()
    state["timeout_command"] = "show"
    _write_state(tmp_path, state)
    command, receipt = _base_command(
        tmp_path, "quiesce", "systemd", _closed_port(), SYSTEMD_NAMES
    )
    command.extend(
        [
            "--systemctl",
            str(systemctl),
            "--deadline-seconds",
            "1.2",
            "--command-timeout-seconds",
            "0.15",
        ]
    )

    result = _run(command)

    assert result.returncode == 1
    assert "timed out" in result.stderr
    assert not receipt.exists()
    marker = tmp_path / "descendant-terminated"
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.read_text(encoding="utf-8") == "yes"


def test_restore_rejects_non_launchd_active_mode(tmp_path: Path) -> None:
    launchctl = _write_manager(tmp_path, "launchctl")
    _write_state(tmp_path, {"jobs": {}})
    with _healthy_server() as port:
        command, receipt = _base_command(
            tmp_path,
            "restore",
            "launchd",
            port,
            LAUNCHD_NAMES,
            "active",
        )
        command.extend(["--launchctl", str(launchctl), "--launchd-uid", "501"])
        result = _run(command)

    assert result.returncode == 1
    assert "launchd requires" in result.stderr
    assert not receipt.exists()


@pytest.mark.parametrize("missing", ["--active-gateway", "--agent-prior-state"])
def test_restore_requires_an_explicit_prior_topology(
    tmp_path: Path,
    missing: str,
) -> None:
    systemctl = _write_manager(tmp_path, "systemctl")
    _write_state(tmp_path, _stopped_systemd_state())
    command, receipt = _base_command(
        tmp_path,
        "restore",
        "systemd",
        _closed_port(),
        SYSTEMD_NAMES,
        "inactive",
    )
    index = command.index(missing)
    del command[index : index + 2]
    command.extend(["--systemctl", str(systemctl)])

    result = _run(command)

    assert result.returncode == 1
    assert "requires an explicit prior" in result.stderr
    assert not receipt.exists()
