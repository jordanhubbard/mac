"""Behavioral contract for the synchronized gateway-readiness proof.

The node installer carries its readiness probe as an embedded Python program.
These tests execute that exact program (with only its production wait constants
shortened) against fake systemd, launchd, and supervisord managers.  They also
pin the durable receipt -> manifest -> live attestation -> fleet epoch evidence
chain so a future deployment change cannot silently drop one of the bindings.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE_INSTALL = ROOT / "deploy" / "fleet-node-install.sh"
FLEET_DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"
REVISION = "a" * 40
GENERATION = "generation-rocky-001"
AGENT = "rocky"
FLEET = "mac"
IDENTITIES = {
    "systemd": {
        "hermes": "mac-hermes-gateway.service",
        "openclaw": "mac-openclaw-gateway.service",
        "nemoclaw": "mac-nemoclaw-gateway.service",
    },
    "launchd": {
        "hermes": "com.mac.hermes-gateway",
        "openclaw": "com.mac.openclaw-gateway",
        "nemoclaw": "com.mac.nemoclaw-gateway",
    },
    "supervisord": {
        "hermes": "mac-hermes-gateway",
        "openclaw": "mac-openclaw-gateway",
        "nemoclaw": "mac-nemoclaw-gateway",
    },
}


def _node_text() -> str:
    return NODE_INSTALL.read_text(encoding="utf-8")


def _fleet_text() -> str:
    return FLEET_DEPLOY.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"expected one {start!r} anchor"
    start_at = text.index(start)
    end_at = text.index(end, start_at)
    return text[start_at:end_at]


def _gateway_probe_python() -> str:
    function = _between(
        _node_text(),
        "verify_selected_gateway_supervisor_health() {",
        "\n# Re-prove immediately before any new gateway supervisor",
    )
    marker = "<<'PY'\n"
    assert function.count(marker) == 1
    body = function.split(marker, 1)[1]
    assert body.endswith("\nPY\n}\n")
    return body[: -len("\nPY\n}\n")]


def _fast_gateway_probe_python() -> str:
    """Keep production logic exact while avoiding ten-second failure tests."""

    source = _gateway_probe_python()
    command_wait = "process.wait(timeout=min(8.0, remaining()))"
    observation_wait = "time.sleep(min(2.0, remaining()))"
    assert source.count(command_wait) == 1
    assert source.count(observation_wait) == 1
    fast_command_wait = (
        'process.wait(timeout=min(float(os.environ.get('
        '"MAC_TEST_GATEWAY_COMMAND_TIMEOUT", "8")), remaining()))'
    )
    source = source.replace(
        command_wait,
        fast_command_wait,
    )
    return source.replace(observation_wait, "time.sleep(0.01)")


def _gateway_summary_python() -> str:
    source = _node_text()
    return _between(
        source,
        "def gateway_readiness_summary(stage):",
        "\n\ndef service_summary():",
    )


def _outer_run_bounded_python() -> str:
    attestation = _between(
        _fleet_text(),
        "remote_daemon_quiescence_attestation() {",
        "\nassert_phase1_attestation_matches_controller() {",
    )
    source = _between(attestation, "def run_bounded(argv, env):", "\n\ndef read_manifest")
    command_wait = "process.wait(timeout=min(20.0, remaining()))"
    assert source.count(command_wait) == 1
    return source.replace(command_wait, "process.wait(timeout=1.0)")


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def _fake_supervisor_source() -> str:
    return f"""#!{sys.executable}
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

config_path = Path(os.environ["FAKE_GATEWAY_CONFIG"])
config = json.loads(config_path.read_text(encoding="utf-8"))
manager = Path(sys.argv[0]).name
if manager == "sudo":
    args = sys.argv[1:]
    if args and args[0] == "-n":
        args = args[1:]
    os.environ["FAKE_SUPERVISOR_MANAGER"] = "privileged"
    os.execvpe(args[0], args, os.environ)

args = sys.argv[1:]
if config.get("mode") == "timeout":
    time.sleep(30)
if config.get("mode") == "child-timeout":
    child_source = '''
import os
from pathlib import Path
import signal
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ["FAKE_GATEWAY_CHILD_PID"]).write_text(
    str(os.getpid()), encoding="utf-8"
)
time.sleep(30)
'''
    subprocess.Popen([sys.executable, "-c", child_source])
    time.sleep(30)
if config.get("mode") == "invalid-utf8":
    os.write(1, b"\\xff")
    raise SystemExit(0)

if manager == "systemctl":
    if len(args) < 2 or args[0] not in {{"show", "is-enabled"}}:
        raise SystemExit(64)
    identity = args[1]
elif manager == "launchctl":
    if len(args) != 2 or args[0] != "print":
        raise SystemExit(64)
    identity = args[1].rsplit("/", 1)[-1]
elif manager == "supervisorctl":
    if len(args) != 2 or args[0] != "status":
        raise SystemExit(64)
    identity = args[1]
else:
    raise SystemExit(64)

if config.get("mode") == "malformed":
    print("manager returned an unknown state")
    raise SystemExit(0)
if manager == "supervisorctl" and config.get("mode") == "ambiguous-absent":
    print("untrusted prefix: no such process")
    raise SystemExit(0)

counts = config.setdefault("counts", {{}})
counter_key = "%s:%s:%s" % (
    manager,
    os.environ.get("FAKE_SUPERVISOR_MANAGER", "user"),
    identity,
)
index = int(counts.get(counter_key, 0))
if manager != "systemctl" or args[0] == "show":
    counts[counter_key] = index + 1
config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
sequence = config.get("states", {{}}).get(identity)
if not isinstance(sequence, list) or not sequence:
    print("fake manager has no state for " + identity, file=sys.stderr)
    raise SystemExit(65)
entry = sequence[min(index, len(sequence) - 1)]
state = entry.get("state")
pid = int(entry.get("pid", 0))
restarts = int(entry.get("restarts", 0))

if manager == "systemctl":
    if args[0] == "is-enabled":
        enabled = entry.get("enabled") or (
            "enabled" if state == "running" else "disabled"
        )
        print(enabled)
        raise SystemExit(0 if enabled == "enabled" else 1)
    if state == "absent":
        print("LoadState=not-found")
        print("ActiveState=inactive")
        print("SubState=dead")
        print("MainPID=0")
        print("NRestarts=0")
        raise SystemExit(0)
    substate = "running" if state == "running" else "dead"
    active = "active" if state == "running" else state
    print("LoadState=loaded")
    print("ActiveState=" + active)
    print("SubState=" + substate)
    print("MainPID=" + str(pid))
    print("NRestarts=" + str(restarts))
    raise SystemExit(0)

if manager == "launchctl":
    if state == "absent":
        print("Could not find service", file=sys.stderr)
        raise SystemExit(113)
    print("{{")
    print("    state = " + state)
    print("    pid = " + str(pid))
    print("}}")
    raise SystemExit(0)

if state == "absent":
    print(identity + ": ERROR (no such process)", file=sys.stderr)
    raise SystemExit(3)
if state == "running":
    print("%s RUNNING pid %d, uptime 0:00:30" % (identity, pid))
    if config.get("mode") == "extra-line":
        print("unexpected second manager record")
    raise SystemExit(0)
print("%s %s Not started" % (identity, state.upper()))
raise SystemExit(3)
"""


def _state(
    manager: str,
    selected: str,
    *,
    competing: str | None = None,
    selected_sequence: list[dict[str, Any]] | None = None,
    none_state: str = "absent",
    none_enabled: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    states: dict[str, list[dict[str, Any]]] = {}
    for offset, (owner, identity) in enumerate(IDENTITIES[manager].items(), 1):
        if selected == "none":
            entry = {"state": none_state, "pid": 0, "restarts": 0}
            if none_enabled is not None:
                entry["enabled"] = none_enabled
        elif owner == selected:
            entry = {"state": "running", "pid": 400 + offset, "restarts": 0}
        elif owner == competing:
            entry = {"state": "running", "pid": 800 + offset, "restarts": 0}
        else:
            entry = {"state": "absent", "pid": 0, "restarts": 0}
        states[identity] = [entry]
    if selected_sequence is not None:
        assert selected != "none"
        states[IDENTITIES[manager][selected]] = selected_sequence
    return states


def _run_probe(
    tmp_path: Path,
    manager: str,
    implementation: str,
    states: dict[str, list[dict[str, Any]]],
    *,
    mode: str = "normal",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    source = _fake_supervisor_source()
    executable_name = {
        "systemd": "systemctl",
        "launchd": "launchctl",
        "supervisord": "supervisorctl",
    }[manager]
    _write_executable(fake_bin / executable_name, source)
    if manager in {"systemd", "supervisord"}:
        _write_executable(fake_bin / "sudo", source)
    config = tmp_path / "supervisor-state.json"
    config.write_text(
        json.dumps({"mode": mode, "states": states, "counts": {}}),
        encoding="utf-8",
    )
    output = tmp_path / "logs" / "gateway-readiness.json"
    names: list[str] = []
    for identity_manager in ("systemd", "launchd", "supervisord"):
        names.extend(IDENTITIES[identity_manager].values())
    args = [
        sys.executable,
        "-c",
        _fast_gateway_probe_python(),
        manager,
        implementation,
        FLEET,
        *names,
        GENERATION,
        REVISION,
        str(output),
    ]
    env = {
        **os.environ,
        "PATH": str(fake_bin),
        "AGENT": AGENT,
        "FAKE_GATEWAY_CONFIG": str(config),
        "FAKE_GATEWAY_CHILD_PID": str(tmp_path / "child.pid"),
        # Linux runners add a non-root sudo -> Python exec hop before the fake
        # manager. Ordinary readiness cases test topology, not scheduler speed;
        # only the deliberate timeout fixtures use a short command deadline.
        "MAC_TEST_GATEWAY_COMMAND_TIMEOUT": (
            "1.0" if mode in {"timeout", "child-timeout"} else "30.0"
        ),
    }
    completed = subprocess.run(
        args,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed, output


@pytest.mark.parametrize(
    ("manager", "implementation"),
    [
        ("systemd", "hermes"),
        ("systemd", "openclaw"),
        ("systemd", "nemoclaw"),
        ("systemd", "none"),
        ("launchd", "openclaw"),
        ("supervisord", "nemoclaw"),
    ],
)
def test_exact_probe_records_exclusive_stable_gateway_readiness(
    tmp_path: Path, manager: str, implementation: str
) -> None:
    completed, output = _run_probe(
        tmp_path,
        manager,
        implementation,
        _state(manager, implementation),
    )
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schema"] == "mac.gateway_readiness.v1"
    assert receipt["agent"] == AGENT
    assert receipt["fleet"] == FLEET
    assert receipt["generation"] == GENERATION
    assert receipt["revision"] == REVISION
    assert receipt["supervisor"] == manager
    assert receipt["implementation"] == implementation
    assert receipt["identities"] == IDENTITIES[manager]
    assert receipt["stable_observations"] == 2
    assert set(receipt["state"]) == {"hermes", "openclaw", "nemoclaw"}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    if implementation == "none":
        assert all(item["state"] == "absent" for item in receipt["state"].values())
    else:
        assert receipt["state"][implementation]["state"] == "running"


@pytest.mark.parametrize("manager", ["systemd", "launchd", "supervisord"])
def test_exact_probe_rejects_a_competing_running_gateway(
    tmp_path: Path, manager: str
) -> None:
    completed, output = _run_probe(
        tmp_path,
        manager,
        "hermes",
        _state(manager, "hermes", competing="openclaw"),
    )
    assert completed.returncode != 0
    assert "gateway readiness failed" in completed.stderr
    assert not output.exists()


@pytest.mark.parametrize("manager", ["systemd", "launchd", "supervisord"])
def test_exact_probe_rejects_malformed_or_unknown_supervisor_output(
    tmp_path: Path, manager: str
) -> None:
    completed, output = _run_probe(
        tmp_path,
        manager,
        "hermes",
        _state(manager, "hermes"),
        mode="malformed",
    )
    assert completed.returncode != 0
    assert "gateway readiness failed" in completed.stderr
    assert not output.exists()


@pytest.mark.parametrize("mode", ["ambiguous-absent", "extra-line"])
def test_exact_supervisord_probe_rejects_ambiguous_multiline_output(
    tmp_path: Path, mode: str
) -> None:
    implementation = "none" if mode == "ambiguous-absent" else "hermes"
    completed, output = _run_probe(
        tmp_path,
        "supervisord",
        implementation,
        _state("supervisord", implementation),
        mode=mode,
    )
    assert completed.returncode != 0
    assert "gateway readiness failed" in completed.stderr
    assert not output.exists()


def test_exact_supervisord_probe_uses_only_the_system_manager(
    tmp_path: Path,
) -> None:
    completed, output = _run_probe(
        tmp_path,
        "supervisord",
        "hermes",
        _state("supervisord", "hermes"),
    )
    assert completed.returncode == 0, completed.stderr
    assert output.is_file()
    config = json.loads(
        (tmp_path / "supervisor-state.json").read_text(encoding="utf-8")
    )
    assert config["counts"]
    assert all(
        key.startswith("supervisorctl:privileged:")
        for key in config["counts"]
    )
    assert not any(":user:" in key for key in config["counts"])


def test_exact_probe_bounds_a_hung_supervisor_command(tmp_path: Path) -> None:
    completed, output = _run_probe(
        tmp_path,
        "systemd",
        "hermes",
        _state("systemd", "hermes"),
        mode="timeout",
    )
    assert completed.returncode != 0
    assert "supervisor command timed out" in completed.stderr
    assert not output.exists()


def test_exact_probe_timeout_reaps_term_ignoring_descendants(tmp_path: Path) -> None:
    completed, output = _run_probe(
        tmp_path,
        "systemd",
        "hermes",
        _state("systemd", "hermes"),
        mode="child-timeout",
    )
    assert completed.returncode != 0
    assert "supervisor command timed out" in completed.stderr
    assert not output.exists()
    pid_path = tmp_path / "child.pid"
    assert pid_path.is_file()
    pid = int(pid_path.read_text(encoding="utf-8"))
    alive = True
    try:
        for _attempt in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.01)
    finally:
        if alive:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                alive = False
    assert not alive, "timed-out gateway probe left a descendant running"


def test_outer_attestation_timeout_reaps_term_ignoring_descendants(
    tmp_path: Path,
) -> None:
    child_pid = tmp_path / "outer-child.pid"
    executable = tmp_path / "outer-manager"
    _write_executable(
        executable,
        f"""#!{sys.executable}
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
child = '''
import os
from pathlib import Path
import signal
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ["OUTER_CHILD_PID"]).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
'''
subprocess.Popen([sys.executable, "-c", child])
time.sleep(30)
""",
    )

    def fail(message: str) -> None:
        raise SystemExit(message)

    namespace = {
        "fail": fail,
        "os": os,
        "remaining": lambda: 5.0,
        "signal": signal,
        "subprocess": subprocess,
        "tempfile": tempfile,
    }
    exec(compile(_outer_run_bounded_python(), str(FLEET_DEPLOY), "exec"), namespace)
    env = {**os.environ, "OUTER_CHILD_PID": str(child_pid)}
    with pytest.raises(SystemExit, match="runtime command timed out"):
        namespace["run_bounded"]([str(executable)], env)
    assert child_pid.is_file()
    pid = int(child_pid.read_text(encoding="utf-8"))
    alive = True
    try:
        for _attempt in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.01)
    finally:
        if alive:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                alive = False
    assert not alive, "timed-out release attestation left a descendant running"


@pytest.mark.parametrize(
    "sequence",
    [
        [
            {"state": "running", "pid": 401, "restarts": 0},
            {"state": "running", "pid": 402, "restarts": 0},
        ],
        [
            {"state": "running", "pid": 401, "restarts": 0},
            {"state": "running", "pid": 401, "restarts": 1},
        ],
    ],
)
def test_exact_probe_rejects_restart_between_observations(
    tmp_path: Path, sequence: list[dict[str, Any]]
) -> None:
    completed, output = _run_probe(
        tmp_path,
        "systemd",
        "hermes",
        _state("systemd", "hermes", selected_sequence=sequence),
    )
    assert completed.returncode != 0
    assert "selected gateway restarted" in completed.stderr
    assert not output.exists()


@pytest.mark.parametrize("manager", ["launchd", "supervisord"])
def test_gatewayless_probe_requires_non_systemd_jobs_to_be_absent(
    tmp_path: Path, manager: str
) -> None:
    stopped = {
        "launchd": "waiting",
        "supervisord": "stopped",
    }[manager]
    completed, output = _run_probe(
        tmp_path,
        manager,
        "none",
        _state(manager, "none", none_state=stopped),
    )
    assert completed.returncode != 0
    assert "gateway readiness failed" in completed.stderr
    assert not output.exists()


def test_gatewayless_systemd_probe_accepts_only_safely_disabled_inactive_units(
    tmp_path: Path,
) -> None:
    completed, output = _run_probe(
        tmp_path,
        "systemd",
        "none",
        _state(
            "systemd",
            "none",
            none_state="inactive",
            none_enabled="disabled",
        ),
    )
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert all(item["state"] == "inactive" for item in receipt["state"].values())
    assert all(item["enabled"] == "disabled" for item in receipt["state"].values())


@pytest.mark.parametrize(
    ("state", "enabled"),
    [
        ("inactive", "enabled"),
        ("inactive", "static"),
        ("inactive", "indirect"),
        ("failed", "disabled"),
    ],
)
def test_gatewayless_systemd_probe_rejects_autonomously_runnable_or_failed_units(
    tmp_path: Path, state: str, enabled: str
) -> None:
    completed, output = _run_probe(
        tmp_path,
        "systemd",
        "none",
        _state(
            "systemd",
            "none",
            none_state=state,
            none_enabled=enabled,
        ),
    )
    assert completed.returncode != 0
    assert "non-selected systemd gateway is not safely disabled" in completed.stderr
    assert not output.exists()


def test_selected_systemd_gateway_must_be_enabled(tmp_path: Path) -> None:
    states = _state(
        "systemd",
        "hermes",
        selected_sequence=[
            {
                "state": "running",
                "pid": 401,
                "restarts": 0,
                "enabled": "disabled",
            }
        ],
    )
    completed, output = _run_probe(tmp_path, "systemd", "hermes", states)
    assert completed.returncode != 0
    assert "selected gateway implementation is not in its required state" in completed.stderr
    assert not output.exists()


def test_selected_systemd_gateway_allows_inactive_disabled_competitors(
    tmp_path: Path,
) -> None:
    states = _state("systemd", "hermes")
    for owner in ("openclaw", "nemoclaw"):
        states[IDENTITIES["systemd"][owner]] = [
            {
                "state": "inactive",
                "pid": 0,
                "restarts": 0,
                "enabled": "disabled",
            }
        ]
    completed, output = _run_probe(tmp_path, "systemd", "hermes", states)
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["state"]["hermes"]["enabled"] == "enabled"
    assert receipt["state"]["openclaw"]["enabled"] == "disabled"
    assert receipt["state"]["nemoclaw"]["enabled"] == "disabled"


def test_openclaw_supervisord_keeps_only_the_stopped_hermes_fallback(
    tmp_path: Path,
) -> None:
    states = _state("supervisord", "openclaw")
    states[IDENTITIES["supervisord"]["hermes"]] = [
        {"state": "stopped", "pid": 0, "restarts": 0}
    ]
    completed, output = _run_probe(tmp_path, "supervisord", "openclaw", states)
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["state"]["openclaw"]["state"] == "running"
    assert receipt["state"]["hermes"]["state"] == "stopped"
    assert receipt["state"]["nemoclaw"]["state"] == "absent"


def _valid_receipt(
    manager: str = "systemd", implementation: str = "hermes"
) -> dict[str, Any]:
    states: dict[str, dict[str, Any]] = {}
    for offset, owner in enumerate(("hermes", "openclaw", "nemoclaw"), 1):
        selected = implementation != "none" and owner == implementation
        item: dict[str, Any] = {
            "state": "running" if selected else "absent",
            "pid": 400 + offset if selected else 0,
            "restarts": 0,
        }
        if manager == "systemd":
            item["enabled"] = "enabled" if selected else "not-found"
        states[owner] = item
    return {
        "schema": "mac.gateway_readiness.v1",
        "agent": AGENT,
        "fleet": FLEET,
        "generation": GENERATION,
        "revision": REVISION,
        "supervisor": manager,
        "implementation": implementation,
        "identities": IDENTITIES[manager],
        "stable_observations": 2,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "state": states,
    }


def _call_gateway_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    receipt: dict[str, Any],
    *,
    mode: int = 0o600,
) -> dict[str, Any]:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    raw = (json.dumps(receipt, sort_keys=True) + "\n").encode("utf-8")
    path = log_dir / "gateway-readiness.json"
    path.write_bytes(raw)
    path.chmod(mode)
    environment = {
        "LOG_DIR": str(log_dir),
        "HERMES_GATEWAY_IMPL": str(receipt["implementation"]),
        "SUPERVISOR_KIND": str(receipt["supervisor"]),
        "OS_KIND": "darwin" if receipt["supervisor"] == "launchd" else "linux",
        "AGENT": AGENT,
        "FLEET_NAME": FLEET,
        "MAC_DEPLOY_GENERATION": GENERATION,
        "DEPLOY_REV": REVISION,
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    namespace = {
        "Path": Path,
        "calendar": calendar,
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "stat": stat,
        "time": time,
    }
    exec(compile(_gateway_summary_python(), str(NODE_INSTALL), "exec"), namespace)
    return namespace["gateway_readiness_summary"]("post")


def test_manifest_summary_binds_an_owner_private_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = _valid_receipt()
    summary = _call_gateway_summary(monkeypatch, tmp_path, receipt)
    raw = (tmp_path / "logs" / "gateway-readiness.json").read_bytes()
    assert summary == {
        "schema": "mac.gateway_readiness_manifest.v1",
        "status": "proved",
        "path": str(tmp_path / "logs" / "gateway-readiness.json"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "generation": GENERATION,
        "revision": REVISION,
        "implementation": "hermes",
        "supervisor": "systemd",
        "stable_observations": 2,
        "identities": IDENTITIES["systemd"],
        "state": receipt["state"],
    }


@pytest.mark.parametrize(
    ("mutation", "mode"),
    [
        (lambda value: value.update(schema="wrong"), 0o600),
        (lambda value: value.update(generation="stale"), 0o600),
        (lambda value: None, 0o644),
    ],
)
def test_manifest_summary_rejects_wrong_schema_generation_or_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: Any,
    mode: int,
) -> None:
    receipt = _valid_receipt()
    mutation(receipt)
    with pytest.raises(SystemExit):
        _call_gateway_summary(monkeypatch, tmp_path, receipt, mode=mode)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(fleet="other-fleet"),
        lambda value: value.pop("observed_at"),
        lambda value: value.update(identities={}),
        lambda value: value.update(state={}),
        lambda value: value["state"]["hermes"].update(pid=0),
        lambda value: value["state"]["openclaw"].update(pid=999),
        lambda value: value["state"]["openclaw"].update(
            state="running", pid=999
        ),
    ],
)
def test_manifest_summary_independently_validates_receipt_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: Any,
) -> None:
    receipt = _valid_receipt()
    mutation(receipt)
    with pytest.raises(SystemExit):
        _call_gateway_summary(monkeypatch, tmp_path, receipt)


@pytest.mark.parametrize(
    ("manager", "implementation", "special_state"),
    [
        ("systemd", "none", ("hermes", "inactive", "disabled")),
        ("launchd", "none", None),
        ("supervisord", "openclaw", ("hermes", "stopped", None)),
    ],
)
def test_manifest_summary_accepts_each_manager_specific_safe_topology(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manager: str,
    implementation: str,
    special_state: tuple[str, str, str | None] | None,
) -> None:
    receipt = _valid_receipt(manager, implementation)
    if manager == "systemd":
        for item in receipt["state"].values():
            item.update(state="inactive", enabled="disabled")
    if special_state is not None:
        owner, state, enabled = special_state
        receipt["state"][owner].update(state=state, pid=0)
        if enabled is not None:
            receipt["state"][owner]["enabled"] = enabled
    summary = _call_gateway_summary(monkeypatch, tmp_path, receipt)
    assert summary["status"] == "proved"
    assert summary["supervisor"] == manager
    assert summary["implementation"] == implementation


@pytest.mark.parametrize(
    ("manager", "implementation", "owner", "state", "enabled"),
    [
        ("systemd", "none", "hermes", "inactive", "enabled"),
        ("launchd", "none", "hermes", "waiting", None),
        ("supervisord", "none", "hermes", "stopped", None),
    ],
)
def test_manifest_summary_rejects_each_manager_specific_unsafe_topology(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    manager: str,
    implementation: str,
    owner: str,
    state: str,
    enabled: str | None,
) -> None:
    receipt = _valid_receipt(manager, implementation)
    receipt["state"][owner].update(state=state, pid=0)
    if enabled is not None:
        receipt["state"][owner]["enabled"] = enabled
    with pytest.raises(SystemExit):
        _call_gateway_summary(monkeypatch, tmp_path, receipt)


def test_embedded_python_and_transport_shells_parse() -> None:
    compile(_gateway_probe_python(), str(NODE_INSTALL), "exec")
    compile(_gateway_summary_python(), str(NODE_INSTALL), "exec")
    for script in (NODE_INSTALL, FLEET_DEPLOY):
        completed = subprocess.run(
            ["/bin/bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr


def test_outer_contract_binds_manifest_receipts_live_state_and_phase1() -> None:
    source = _fleet_text()
    reconcile = _between(
        source, "reconcile_remote_deploy() {", "\nremote_daemon_quiescence_attestation() {"
    )
    attest = _between(
        source,
        "remote_daemon_quiescence_attestation() {",
        "\nassert_phase1_attestation_matches_controller() {",
    )
    controller = _between(
        source,
        "assert_phase1_attestation_matches_controller() {",
        "\nrestore_remote_agent_release_barrier() {",
    )
    refresh = _between(
        source,
        "refresh_release_ready_quiescence() {",
        "\ncommit_fleet_release_epoch() {",
    )
    epoch = _between(
        source,
        "commit_fleet_release_epoch() {",
        "\nenforce_bound_worker_credentials() {",
    )

    assert "mac.phase1_cohort_quiescence_manifest.v1" in reconcile
    assert "mac.media_runtime_readiness_manifest.v1" in reconcile
    assert "mac.gateway_readiness_manifest.v1" in reconcile
    assert "manifest phase-1 evidence diverged" in reconcile
    assert "manifest media runtime readiness diverged" in reconcile
    assert "manifest gateway readiness diverged" in reconcile

    for evidence in (
        "phase1_receipt_sha256",
        "phase1_daemon_receipt_sha256",
        "phase1_function_block_sha256",
        "phase1_supervisor",
        "media_runtime_readiness",
        "media_runtime_readiness_sha256",
        "media_runtime_source_contract_sha256",
        "media_runtime_stable_observations",
        "gateway_readiness_sha256",
        "gateway_supervisor",
        "gateway_identities",
    ):
        assert evidence in attest
        assert evidence in controller or evidence.startswith("gateway_")
        assert evidence in refresh
        # Local planning and independent hub verification repeat the key set.
        assert epoch.count('"' + evidence + '"') >= 2

    assert "phase1_path.lstat()" in attest
    assert "gateway_path.lstat()" in attest
    assert 'os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)' in attest
    assert "media runtime readiness receipt changed while reading" in attest
    assert "stat.S_IMODE(phase1_metadata.st_mode) != 0o600" in attest
    assert "stat.S_IMODE(gateway_metadata.st_mode) != 0o600" in attest
    assert "phase1_digest != phase1_summary.get(\"sha256\")" in attest
    assert "hashlib.sha256(raw_gateway).hexdigest() != gateway_summary.get(\"sha256\")" in attest
    assert "media_supervisor.get(\"media_resources\") != media_resources" in attest
    assert "live_gateway_sample()" in attest
    assert "selected gateway restarted during release attestation" in attest
    assert "assert_phase1_attestation_matches_controller" in source
    assert "evidence_digest = hashlib.sha256(" in epoch
    assert epoch.count("epoch_id.rsplit(\":\", 1)[-1] != evidence_digest") == 2


def test_outer_live_attestation_requires_absence_and_positive_process_ids() -> None:
    attest = _between(
        _fleet_text(),
        "remote_daemon_quiescence_attestation() {",
        "\nassert_phase1_attestation_matches_controller() {",
    )
    # systemd may retain only inactive, disabled/masked units; launchd and
    # supervisord require absence (apart from OpenClaw's exactly-stopped Hermes
    # rollback program).  A running claim needs a positive PID everywhere.
    assert "non-selected systemd gateway is unsafe" in attest
    assert "non-selected launchd gateway is loaded" in attest
    assert "Hermes rollback gateway is not stopped" in attest
    assert "non-selected supervisord gateway is configured" in attest
    assert attest.count("pid <= 0") >= 3
