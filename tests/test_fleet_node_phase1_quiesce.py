from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "fleet-node-phase1-quiesce.sh"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _install_sudo(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "sudo",
        """#!/bin/sh
set -eu
if [ "${1:-}" = -n ]; then shift; fi
case "${1:-}" in
  */supervisorctl|supervisorctl)
    export FAKE_SUPERVISOR_MANAGER=privileged
    ;;
esac
exec "$@"
""",
    )


def _install_daemon_block(tmp_path: Path) -> tuple[Path, Path]:
    writer = tmp_path / "daemon_writer.py"
    writer.write_text(
        """from __future__ import annotations
import json
import os
from pathlib import Path

generation = os.environ["DEPLOY_GENERATION"]
revision = os.environ["DEPLOY_REV"]
mode = os.environ.get("FAKE_DAEMON_MODE", "valid")
action = os.environ.get("FAKE_DAEMON_ACTION", "quiesce")
if action == "prepare":
    receipt = Path(os.environ["MAC_HOME"]) / (
        "daemon-resource-restore-contract-%s.json" % generation
    )
    receipt.write_text(json.dumps({
        "schema": "mac.daemon_resource_restore_contract.v1",
        "generation": generation,
        "revision": revision,
        "openclaw": {"sandbox": None, "prior_state": "not_managed"},
        "container_runtimes": [],
        "legacy_nemoclaw": {"retained_stopped": [], "prior_state": "inactive"},
    }) + "\\n", encoding="utf-8")
    receipt.chmod(0o600)
    with open(os.environ["FAKE_PHASE1_EVENTS"], "a", encoding="utf-8") as stream:
        stream.write("daemon-prepare\\n")
    raise SystemExit(0)
if action == "restore":
    receipt = Path(os.environ["MAC_HOME"]) / (
        "daemon-resource-restore-%s.json" % generation
    )
    contract = Path(os.environ["MAC_HOME"]) / (
        "daemon-resource-restore-contract-%s.json" % generation
    )
    receipt.write_text(json.dumps({
        "schema": "mac.daemon_resource_restore.v1",
        "status": "restored",
        "generation": generation,
        "revision": revision,
        "source_contract_sha256": __import__("hashlib").sha256(contract.read_bytes()).hexdigest(),
        "openclaw": {"sandbox": None, "prior_state": "not_managed"},
        "container_runtimes": [],
        "legacy_nemoclaw": {"final_state": "inactive"},
    }) + "\\n", encoding="utf-8")
    receipt.chmod(0o600)
    with open(os.environ["FAKE_PHASE1_EVENTS"], "a", encoding="utf-8") as stream:
        stream.write("daemon-restore\\n")
    raise SystemExit(0)
if mode == "wrong-generation":
    generation += "-stale"
supervisor_tamper = os.environ.get("FAKE_SUPERVISOR_TAMPER", "")
if supervisor_tamper:
    supervisor_path = Path(os.environ["MAC_PHASE1_SUPERVISOR_PROOF_PATH"])
    supervisor = json.loads(supervisor_path.read_text(encoding="utf-8"))
    manager = supervisor["supervisor"]["manager"]
    if manager == "supervisord":
        resources = supervisor["supervisor"]["managers"][0]["resources"]
    else:
        resources = supervisor["supervisor"]["resources"]
    if supervisor_tamper == "missing-prior-state":
        resources[0].pop("prior_state")
    elif supervisor_tamper == "malformed-prior-state":
        resources[0]["prior_state"] = {"state": "active"}
    elif supervisor_tamper == "nonquiescent-final-state":
        resources[0]["state"] = "active"
    elif supervisor_tamper == "wrong-generation":
        supervisor["generation"] += "-stale"
    elif supervisor_tamper == "wrong-identity":
        resources[0]["name"] += "-impostor"
    else:
        raise RuntimeError("unknown supervisor tamper mode")
    supervisor_path.write_text(
        json.dumps(supervisor) + "\\n", encoding="utf-8"
    )
receipt = Path(os.environ["MAC_HOME"]) / (
    "daemon-resource-quiescence-%s.json" % os.environ["DEPLOY_GENERATION"]
)
identities = []
payload = {
    "schema": "mac.daemon_resource_quiescence.v1",
    "generation": generation,
    "revision": revision,
    "recorded_at": "2026-07-19T00:00:00Z",
    "openclaw": {
        "sandbox": None,
        "initial_state": "not_managed",
        "final_state": "absent",
        "stop_wrapper_invoked": False,
        "delete_invoked": False,
    },
    "container_runtimes": identities,
    "legacy_nemoclaw": {"retained_stopped": [], "final_state": "inactive"},
    "proofs": {
        "pre_source": {
            "recorded_at": "2026-07-19T00:00:00Z",
            "container_runtimes": identities,
            "stable_inactive_observations": 2,
        }
    },
}
if mode == "raw-output":
    payload["stdout"] = "forbidden raw output"
receipt.write_text(json.dumps(payload) + "\\n", encoding="utf-8")
receipt.chmod(0o600)
with open(os.environ["FAKE_PHASE1_EVENTS"], "a", encoding="utf-8") as stream:
    stream.write("daemon\\n")
""",
        encoding="utf-8",
    )
    writer.chmod(0o700)
    block = tmp_path / "daemon-functions.sh"
    block.write_text(
        """quiesce_daemon_resources_before_source_replacement() {
  "$PY" "$FAKE_DAEMON_WRITER"
}
prepare_daemon_resources_for_phase1_restore() {
  FAKE_DAEMON_ACTION=prepare "$PY" "$FAKE_DAEMON_WRITER"
}
verify_daemon_resources_after_phase1_restore() {
  FAKE_DAEMON_ACTION=restore "$PY" "$FAKE_DAEMON_WRITER"
}
""",
        encoding="utf-8",
    )
    block.chmod(0o600)
    return block, writer


def _install_production_interface_daemon_block(
    tmp_path: Path, writer: Path
) -> Path:
    source = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    start_marker = "# BEGIN MAC DAEMON RESOURCE QUIESCENCE"
    end_marker = "# END MAC DAEMON RESOURCE QUIESCENCE"
    start = source.index(start_marker)
    end = source.index(end_marker, start) + len(end_marker)
    extracted = source[start:end]
    # Keep the exact production entrypoint and its log() call, while replacing
    # only the lower-level runtime probe so this interface regression cannot
    # inspect or mutate daemon resources on the test host.
    extracted += """
daemon_resource_quiescence_gate() {
  case "$1" in
    prepare-restore) FAKE_DAEMON_ACTION=prepare "$PY" "$FAKE_DAEMON_WRITER" ;;
    restore-check) FAKE_DAEMON_ACTION=restore "$PY" "$FAKE_DAEMON_WRITER" ;;
    *) "$PY" "$FAKE_DAEMON_WRITER" ;;
  esac
}
"""
    block = tmp_path / "production-daemon-functions.sh"
    block.write_text(extracted + "\n", encoding="utf-8")
    block.chmod(0o600)
    assert writer.is_file()
    return block


def _base_case(tmp_path: Path, manager: str, *, os_kind: str = "linux") -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    mac_home = tmp_path / "mac-home"
    home = tmp_path / "home"
    user_runtime = tmp_path / "user-runtime"
    fake_bin.mkdir()
    mac_home.mkdir()
    (mac_home / "src" / "mac").mkdir(parents=True)
    (mac_home / "venv").mkdir()
    (mac_home / "mac.env").write_text(
        "MAC_STARTUP_CLEAR_HOLD=1\n", encoding="utf-8"
    )
    (mac_home / "mac.env").chmod(0o600)
    home.mkdir()
    user_runtime.mkdir()
    _install_sudo(fake_bin)
    block, writer = _install_daemon_block(tmp_path)
    events = tmp_path / "events"
    events.write_text("", encoding="utf-8")
    return {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "AGENT": "rocky",
        "FLEET_NAME": "mac",
        "OS_KIND": os_kind,
        "DEPLOY_REV": "a" * 40,
        "DEPLOY_GENERATION": "generation-rocky-001",
        "MAC_HOME": str(mac_home),
        "PY": sys.executable,
        "SUPERVISOR_KIND": manager,
        "MAC_PHASE1_DAEMON_FUNCTIONS_FILE": str(block),
        "FAKE_DAEMON_WRITER": str(writer),
        "FAKE_PHASE1_EVENTS": str(events),
        "MAC_PHASE1_COMMAND_TIMEOUT_SECONDS": "0.5",
        "MAC_PHASE1_TOTAL_TIMEOUT_SECONDS": "3",
        "MAC_PHASE1_POLL_SECONDS": "0.01",
        "MAC_PHASE1_TEST_MODE": "1",
        "FAKE_MANAGER_BIN_DIR": str(fake_bin),
        "FAKE_USER_RUNTIME_DIR": str(user_runtime),
    }


def _run_action(
    env: dict[str, str], action: str, *, timeout: float = 10
) -> subprocess.CompletedProcess[str]:
    command = ["/bin/bash", str(SCRIPT), action]
    if action in {"restore", "restore-phase1"}:
        contract_path = Path(env["MAC_HOME"]) / (
            "phase1-cohort-restore-contract-%s.json" % env["DEPLOY_GENERATION"]
        )
        if contract_path.is_file():
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            retained = contract.get("restore_executable")
            if isinstance(retained, dict) and isinstance(retained.get("path"), str):
                command = [retained["path"], action]
    return subprocess.run(
        command,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run(env: dict[str, str], *, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    prepared = _run_action(env, "prepare", timeout=timeout)
    if prepared.returncode != 0:
        return prepared
    contract = Path(env["MAC_HOME"]) / (
        "phase1-cohort-restore-contract-%s.json" % env["DEPLOY_GENERATION"]
    )
    env["MAC_PHASE1_RESTORE_CONTRACT_SHA256"] = hashlib.sha256(
        contract.read_bytes()
    ).hexdigest()
    Path(env["FAKE_PHASE1_EVENTS"]).write_text("", encoding="utf-8")
    return _run_action(env, "quiesce", timeout=timeout)


def _receipt(env: dict[str, str]) -> dict:
    path = Path(env["MAC_HOME"]) / (
        "phase1-cohort-quiescence-%s.json" % env["DEPLOY_GENERATION"]
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _install_systemctl(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "systemctl",
        """#!/bin/sh
set -eu
command=${1:?}
shift
if [ "$command" = --user ]; then
  command=${1:?}
  shift
fi
case "$command" in
  list-units)
    if [ "${FAKE_HOST_AUTOMATION_LOADED:-0}" = 1 ]; then
      printf 'mac-openclaw-script-memory-sync.timer loaded active waiting synthetic\n'
    fi
    ;;
  show)
    unit=""
    for value in "$@"; do unit=$value; done
    if [ -n "${FAKE_MANAGER_ENV_CAPTURE:-}" ]; then
      /usr/bin/env > "$FAKE_MANAGER_ENV_CAPTURE"
    fi
    if [ "${FAKE_SYSTEMD_MODE:-normal}" = inspect-error ] \
        && [ "$unit" = mac-openclaw-gateway.service ]; then
      echo 'SUPER_SECRET_SYSTEMD_TRANSPORT_OUTPUT' >&2
      exit 70
    fi
    if [ "${FAKE_SYSTEMD_MODE:-normal}" = output-flood ]; then
      while :; do
        printf 'SUPER_SECRET_SYSTEMD_OUTPUT_FLOOD_0123456789abcdef\n'
      done
    fi
    if [ "${FAKE_SYSTEMD_MODE:-normal}" = timeout ]; then
      sleep 30 &
      child=$!
      if [ -n "${FAKE_TIMEOUT_CHILD_PID_FILE:-}" ]; then
        printf '%s\n' "$child" > "$FAKE_TIMEOUT_CHILD_PID_FILE"
      fi
      wait "$child"
    fi
    state_file="$FAKE_SYSTEMD_STATE/$unit"
    case "$unit" in
      mac-gen-server.service|mac-gen-audio-server.service|mac-gen-video-server.service)
        case "${FAKE_MEDIA_STATE:-absent}" in
          absent)
            printf 'LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\n'
            exit 0
            ;;
          inactive)
            printf 'LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\n'
            exit 0
            ;;
          active) ;;
          *) exit 65 ;;
        esac
        ;;
    esac
    if [ -f "$state_file.absent" ]; then
      printf 'LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\n'
      exit 0
    fi
    state=active
    if { [ "$unit" = mac-nemoclaw-gateway.service ] \
          && [ "${FAKE_NEMO_ACTIVE:-0}" != 1 ]; } \
        || [ "${FAKE_SYSTEMD_INITIAL:-active}" = inactive ] \
        || [ -f "$state_file" ]; then
      state=inactive
    fi
    printf 'LoadState=loaded\\nActiveState=%s\\nSubState=%s\\nMainPID=%s\\n' \
      "$state" "$( [ "$state" = active ] && echo running || echo dead )" \
      "$( [ "$state" = active ] && echo 321 || echo 0 )"
    ;;
  is-enabled)
    unit=${1:?}
    if [ -f "$FAKE_SYSTEMD_STATE/$unit.absent" ]; then
      printf 'not-found\n'
      exit 4
    fi
    if [ -f "$FAKE_SYSTEMD_STATE/$unit.enablement" ]; then
      cat "$FAKE_SYSTEMD_STATE/$unit.enablement"
    else
      printf '%s\n' "${FAKE_SYSTEMD_ENABLED_STATE:-enabled}"
    fi
    ;;
  daemon-reload)
    printf 'systemd-daemon-reload\n' >> "$FAKE_PHASE1_EVENTS"
    ;;
  unmask)
    unit=${1:?}
    if [ -f "$FAKE_SYSTEMD_STATE/$unit.enablement" ] \
        && [ "$(cat "$FAKE_SYSTEMD_STATE/$unit.enablement")" = masked ]; then
      printf 'disabled\n' > "$FAKE_SYSTEMD_STATE/$unit.enablement"
    fi
    printf 'systemd-unmask:%s\n' "$unit" >> "$FAKE_PHASE1_EVENTS"
    ;;
  enable|disable|mask)
    unit=${1:?}
    case "$command" in
      enable) value=enabled ;;
      disable) value=disabled ;;
      mask) value=masked ;;
    esac
    if [ "$value" = "${FAKE_SYSTEMD_ENABLED_STATE:-enabled}" ]; then
      rm -f "$FAKE_SYSTEMD_STATE/$unit.enablement"
    else
      printf '%s\n' "$value" > "$FAKE_SYSTEMD_STATE/$unit.enablement"
    fi
    printf 'systemd-%s:%s\n' "$command" "$unit" >> "$FAKE_PHASE1_EVENTS"
    ;;
  stop)
    unit=${1:?}
    : > "$FAKE_SYSTEMD_STATE/$unit"
    printf 'systemd:%s\\n' "$unit" >> "$FAKE_PHASE1_EVENTS"
    ;;
  start)
    unit=${1:?}
    rm -f "$FAKE_SYSTEMD_STATE/$unit"
    printf 'systemd-restore:%s\\n' "$unit" >> "$FAKE_PHASE1_EVENTS"
    ;;
  *) exit 64 ;;
esac
""",
    )


def test_systemd_quiesces_all_runtime_services_before_daemon_gate(tmp_path: Path) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env["FAKE_SYSTEMD_STATE"] = str(state)
    _install_systemctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    events = Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8").splitlines()
    expected = {
        "mac-agent.service",
        "mac-hermes-gateway.service",
        "mac-openclaw-gateway.service",
    }
    assert {line.split(":", 1)[1] for line in events[:-1]} == expected
    assert events[-1] == "daemon"
    assert all("control-plane" not in line for line in events)
    receipt = _receipt(env)
    assert receipt["schema"] == "mac.phase1_cohort_quiescence.v1"
    assert receipt["generation"] == env["DEPLOY_GENERATION"]
    assert receipt["supervisor"]["manager"] == "systemd"
    assert {item["name"] for item in receipt["supervisor"]["resources"]} == {
        *expected,
        "mac-nemoclaw-gateway.service",
    }
    assert {item["state"] for item in receipt["supervisor"]["resources"]} == {
        "inactive"
    }
    media = receipt["supervisor"]["media_resources"]
    assert {item["name"] for item in media} == {
        "mac-gen-server.service",
        "mac-gen-audio-server.service",
        "mac-gen-video-server.service",
    }
    assert {
        (item["prior_state"], item["state"]) for item in media
    } == {("absent", "absent")}
    assert len(receipt["daemon_resource_receipt"]["sha256"]) == 64


def test_identify_is_read_only_and_reports_rollback_capability(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    mac_home = Path(env["MAC_HOME"])
    (mac_home / "mac.env").write_text(
        "MAC_WORKER_DEPLOY_GENERATION=prior-generation\n", encoding="utf-8"
    )
    (mac_home / "mac.env").chmod(0o600)
    (mac_home / "deployed-source-revision").write_text(
        "b" * 40 + "\n", encoding="utf-8"
    )
    (mac_home / "deployed-source-revision").chmod(0o600)
    before = {
        path.relative_to(tmp_path): (path.stat().st_mode, path.read_bytes())
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    result = _run_action(env, "identify")

    assert result.returncode == 0, result.stderr
    identity = json.loads(result.stdout)
    assert identity["schema"] == "mac.fleet_node_identity.v1"
    assert identity["status"] == "identified"
    assert identity["agent"] == "rocky"
    assert identity["rollback_capable"] is True
    assert identity["current_generation"] == "prior-generation"
    assert identity["current_revision"] == "b" * 40
    assert identity["artifacts"]["source"]["regular_directory"] is True
    assert identity["artifacts"]["venv"]["regular_directory"] is True
    after = {
        path.relative_to(tmp_path): (path.stat().st_mode, path.read_bytes())
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_prepare_is_nonmutating_and_phase1_restore_is_digest_bound_and_idempotent(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env["FAKE_SYSTEMD_STATE"] = str(state)
    hold = Path(env["MAC_HOME"]) / "deploy-dispatch-hold.json"
    hold.write_text('{"prior":true}\n', encoding="utf-8")
    hold.chmod(0o600)
    _install_systemctl(tmp_path / "bin")

    prepared = _run_action(env, "arm-phase1")

    assert prepared.returncode == 0, prepared.stderr
    contract_path = Path(env["MAC_HOME"]) / (
        "phase1-cohort-restore-contract-%s.json" % env["DEPLOY_GENERATION"]
    )
    contract_raw = contract_path.read_bytes()
    contract = json.loads(contract_raw)
    assert contract["schema"] == "mac.phase1_cohort_restore_contract.v1"
    assert contract["status"] == "prepared"
    assert contract["rollback_capable"] is True
    assert contract["generation"] == env["DEPLOY_GENERATION"]
    assert contract["revision"] == env["DEPLOY_REV"]
    assert len(contract["daemon_restore_contract"]["sha256"]) == 64
    assert len(contract["local_artifacts"]["sha256"]) == 64
    retained_helper = Path(contract["restore_executable"]["path"])
    retained_daemon = Path(contract["daemon_function_block"]["path"])
    assert contract["restore_executable"]["argv"] == [
        str(retained_helper),
        "restore",
    ]
    assert retained_helper.stat().st_mode & 0o777 == 0o700
    assert retained_daemon.stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(retained_helper.read_bytes()).hexdigest() == (
        contract["restore_executable"]["sha256"]
    )
    assert hashlib.sha256(retained_daemon.read_bytes()).hexdigest() == (
        contract["daemon_function_block"]["sha256"]
    )
    assert all(not path.exists() for path in state.iterdir())
    assert (Path(env["MAC_HOME"]) / "mac.env").read_text() == (
        "MAC_STARTUP_CLEAR_HOLD=1\n"
    )

    digest = hashlib.sha256(contract_raw).hexdigest()
    env["MAC_PHASE1_RESTORE_CONTRACT_SHA256"] = digest
    (Path(env["MAC_HOME"]) / "mac.env").write_text(
        "MAC_STARTUP_CLEAR_HOLD=0\n", encoding="utf-8"
    )
    (Path(env["MAC_HOME"]) / "mac.env").chmod(0o600)
    hold.write_text('{"deployment":"successor"}\n', encoding="utf-8")
    hold.chmod(0o600)
    # Recovery is generation-local: neither quiesce nor restore may read a
    # newer or damaged copy of the originally uploaded daemon block.
    Path(env["MAC_PHASE1_DAEMON_FUNCTIONS_FILE"]).write_text(
        "exit 99\n", encoding="utf-8"
    )

    quiesced = _run_action(env, "quiesce")
    assert quiesced.returncode == 0, quiesced.stderr
    restored = _run_action(env, "restore-phase1")

    assert restored.returncode == 0, restored.stderr
    assert (Path(env["MAC_HOME"]) / "mac.env").read_text() == (
        "MAC_STARTUP_CLEAR_HOLD=1\n"
    )
    assert hold.read_text(encoding="utf-8") == '{"prior":true}\n'
    assert all(not path.exists() for path in state.iterdir())
    receipt_path = Path(env["MAC_HOME"]) / (
        "phase1-cohort-restore-%s.json" % env["DEPLOY_GENERATION"]
    )
    receipt_raw = receipt_path.read_bytes()
    receipt = json.loads(receipt_raw)
    assert receipt["schema"] == "mac.phase1_cohort_restore.v1"
    assert receipt["status"] == "restored"
    assert receipt["source_contract_sha256"] == digest
    assert len(receipt["final_topology_proof"]["sha256"]) == 64
    assert len(receipt["daemon_restore_proof"]["sha256"]) == 64
    events_before_replay = Path(env["FAKE_PHASE1_EVENTS"]).read_bytes()

    replay = _run_action(env, "restore-phase1")

    assert replay.returncode == 0, replay.stderr
    assert replay.stdout.encode() == receipt_raw
    assert Path(env["FAKE_PHASE1_EVENTS"]).read_bytes() == events_before_replay


def test_prepare_advertises_incomplete_new_node_and_quiesce_refuses_it(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env["FAKE_SYSTEMD_STATE"] = str(state)
    (Path(env["MAC_HOME"]) / "venv").rmdir()
    _install_systemctl(tmp_path / "bin")

    prepared = _run_action(env, "prepare")

    assert prepared.returncode == 0, prepared.stderr
    contract_path = Path(env["MAC_HOME"]) / (
        "phase1-cohort-restore-contract-%s.json" % env["DEPLOY_GENERATION"]
    )
    contract_raw = contract_path.read_bytes()
    contract = json.loads(contract_raw)
    assert contract["rollback_capable"] is False
    assert "source and virtualenv" in contract["rollback_ineligible_reason"]
    env["MAC_PHASE1_RESTORE_CONTRACT_SHA256"] = hashlib.sha256(
        contract_raw
    ).hexdigest()

    quiesced = _run_action(env, "quiesce")

    assert quiesced.returncode != 0
    assert "prepared restore contract belongs to another node generation" in quiesced.stderr
    assert all(not path.exists() for path in state.iterdir())
    assert "systemd:" not in Path(env["FAKE_PHASE1_EVENTS"]).read_text(
        encoding="utf-8"
    )


def test_quiesce_refuses_to_mutate_without_exact_prepared_contract_digest(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env["FAKE_SYSTEMD_STATE"] = str(state)
    _install_systemctl(tmp_path / "bin")
    prepared = _run_action(env, "prepare")
    assert prepared.returncode == 0, prepared.stderr

    result = _run_action(env, "quiesce")

    assert result.returncode != 0
    assert "exact prepared restore contract digest is required" in result.stderr
    assert all(not path.exists() for path in state.iterdir())


def test_restore_rejects_current_checkout_helper_before_mutation(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env["FAKE_SYSTEMD_STATE"] = str(state)
    _install_systemctl(tmp_path / "bin")
    prepared = _run_action(env, "prepare")
    assert prepared.returncode == 0, prepared.stderr
    contract_path = Path(env["MAC_HOME"]) / (
        "phase1-cohort-restore-contract-%s.json" % env["DEPLOY_GENERATION"]
    )
    env["MAC_PHASE1_RESTORE_CONTRACT_SHA256"] = hashlib.sha256(
        contract_path.read_bytes()
    ).hexdigest()
    mac_env = Path(env["MAC_HOME"]) / "mac.env"
    mac_env.write_text("MAC_STARTUP_CLEAR_HOLD=0\n", encoding="utf-8")
    mac_env.chmod(0o600)
    quiesced = _run_action(env, "quiesce")
    assert quiesced.returncode == 0, quiesced.stderr
    stopped_before = {
        path.name: path.read_bytes() for path in state.iterdir() if path.is_file()
    }

    wrong_helper = subprocess.run(
        ["/bin/bash", str(SCRIPT), "restore"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert wrong_helper.returncode != 0
    assert "exact retained restore executable is required" in wrong_helper.stderr
    assert mac_env.read_text(encoding="utf-8") == "MAC_STARTUP_CLEAR_HOLD=0\n"
    assert {
        path.name: path.read_bytes() for path in state.iterdir() if path.is_file()
    } == stopped_before
    assert not (
        Path(env["MAC_HOME"])
        / ("phase1-cohort-restore-%s.json" % env["DEPLOY_GENERATION"])
    ).exists()

    restored = _run_action(env, "restore")
    assert restored.returncode == 0, restored.stderr


def test_restore_rejects_tampered_retained_executable_before_mutation(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env["FAKE_SYSTEMD_STATE"] = str(state)
    _install_systemctl(tmp_path / "bin")
    prepared = _run_action(env, "prepare")
    assert prepared.returncode == 0, prepared.stderr
    contract_path = Path(env["MAC_HOME"]) / (
        "phase1-cohort-restore-contract-%s.json" % env["DEPLOY_GENERATION"]
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    env["MAC_PHASE1_RESTORE_CONTRACT_SHA256"] = hashlib.sha256(
        contract_path.read_bytes()
    ).hexdigest()
    quiesced = _run_action(env, "quiesce")
    assert quiesced.returncode == 0, quiesced.stderr
    stopped_before = {
        path.name: path.read_bytes() for path in state.iterdir() if path.is_file()
    }
    retained = Path(contract["restore_executable"]["path"])
    retained.write_bytes(retained.read_bytes() + b"\n# post-prepare tamper\n")
    retained.chmod(0o700)

    result = subprocess.run(
        [str(retained), "restore"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "retained restore executable digest differs" in result.stderr
    assert {
        path.name: path.read_bytes() for path in state.iterdir() if path.is_file()
    } == stopped_before
    assert not (
        Path(env["MAC_HOME"])
        / ("phase1-cohort-restore-%s.json" % env["DEPLOY_GENERATION"])
    ).exists()


def test_prepare_replay_rejects_tampered_retained_daemon_block(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env["FAKE_SYSTEMD_STATE"] = str(state)
    _install_systemctl(tmp_path / "bin")
    prepared = _run_action(env, "prepare")
    assert prepared.returncode == 0, prepared.stderr
    contract = json.loads(prepared.stdout)
    retained = Path(contract["daemon_function_block"]["path"])
    retained.write_bytes(retained.read_bytes() + b"\n# post-prepare tamper\n")
    retained.chmod(0o600)

    replay = _run_action(env, "prepare")

    assert replay.returncode != 0
    assert "existing daemon function block digest differs" in replay.stderr
    assert all(not path.exists() for path in state.iterdir())


def test_systemd_restore_reconstructs_exact_enablement_intent(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env["FAKE_SYSTEMD_STATE"] = str(state)
    agent_enablement = state / "mac-agent.service.enablement"
    hermes_enablement = state / "mac-hermes-gateway.service.enablement"
    openclaw_enablement = state / "mac-openclaw-gateway.service.enablement"
    agent_enablement.write_text("disabled\n", encoding="utf-8")
    hermes_enablement.write_text("masked\n", encoding="utf-8")
    _install_systemctl(tmp_path / "bin")
    prepared = _run_action(env, "prepare")
    assert prepared.returncode == 0, prepared.stderr
    contract_path = Path(env["MAC_HOME"]) / (
        "phase1-cohort-restore-contract-%s.json" % env["DEPLOY_GENERATION"]
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    initial = {
        item["name"]: item["enabled_state"]
        for item in contract["supervisor"]["resources"]
    }
    assert initial == {
        "mac-agent.service": "disabled",
        "mac-hermes-gateway.service": "masked",
        "mac-openclaw-gateway.service": "enabled",
        "mac-nemoclaw-gateway.service": "enabled",
    }
    env["MAC_PHASE1_RESTORE_CONTRACT_SHA256"] = hashlib.sha256(
        contract_path.read_bytes()
    ).hexdigest()
    quiesced = _run_action(env, "quiesce")
    assert quiesced.returncode == 0, quiesced.stderr

    # Model a successor changing all three mutable enablement intents.
    agent_enablement.unlink()
    hermes_enablement.write_text("disabled\n", encoding="utf-8")
    openclaw_enablement.write_text("masked\n", encoding="utf-8")
    restored = _run_action(env, "restore")

    assert restored.returncode == 0, restored.stderr
    assert agent_enablement.read_text(encoding="utf-8") == "disabled\n"
    assert hermes_enablement.read_text(encoding="utf-8") == "masked\n"
    assert not openclaw_enablement.exists()


def test_launchd_restore_reconstructs_disable_overrides_for_active_and_absent_jobs(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "launchd", os_kind="darwin")
    state = tmp_path / "launchd-state"
    state.mkdir()
    env["FAKE_LAUNCHD_STATE"] = str(state)
    uid = os.getuid()

    def override(target: str) -> Path:
        return state / (target.translate(str.maketrans("/.", "__")) + ".disabled")

    active_disabled = override(f"gui/{uid}/com.mac.agent")
    absent_disabled = override("system/com.mac.hermes-gateway")
    active_enabled = override(f"gui/{uid}/com.mac.hermes-gateway")
    active_disabled.touch()
    absent_disabled.touch()
    _install_launchctl(tmp_path / "bin")
    prepared = _run_action(env, "prepare")
    assert prepared.returncode == 0, prepared.stderr
    contract_path = Path(env["MAC_HOME"]) / (
        "phase1-cohort-restore-contract-%s.json" % env["DEPLOY_GENERATION"]
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    initial = {
        item["target"]: item["disabled_override"]
        for item in contract["supervisor"]["resources"]
    }
    assert initial[f"gui/{uid}/com.mac.agent"] is True
    assert initial["system/com.mac.hermes-gateway"] is True
    assert initial[f"gui/{uid}/com.mac.hermes-gateway"] is False
    env["MAC_PHASE1_RESTORE_CONTRACT_SHA256"] = hashlib.sha256(
        contract_path.read_bytes()
    ).hexdigest()
    quiesced = _run_action(env, "quiesce")
    assert quiesced.returncode == 0, quiesced.stderr

    active_disabled.unlink()
    absent_disabled.unlink()
    active_enabled.touch()
    restored = _run_action(env, "restore")

    assert restored.returncode == 0, restored.stderr
    assert active_disabled.exists()
    assert absent_disabled.exists()
    assert not active_enabled.exists()


def test_manager_subprocess_environment_excludes_credentials_and_shell_hooks(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    captured = tmp_path / "manager-env"
    env.update(
        {
            "FAKE_SYSTEMD_STATE": str(state),
            "FAKE_MANAGER_ENV_CAPTURE": str(captured),
            "MAC_HUB_TOKEN": "SUPER_SECRET_HUB_TOKEN",
            "GITHUB_TOKEN": "SUPER_SECRET_GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY": "SUPER_SECRET_AWS_KEY",
            "SSH_ASKPASS": "/tmp/secret-askpass",
            "GIT_ASKPASS": "/tmp/secret-git-askpass",
            "PYTHONPATH": "/tmp/injected-python",
        }
    )
    _install_systemctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    manager_env = captured.read_text(encoding="utf-8")
    assert (
        "PATH="
        + str(tmp_path / "bin")
        + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    ) in manager_env
    for forbidden in (
        "MAC_HUB_TOKEN=",
        "GITHUB_TOKEN=",
        "AWS_SECRET_ACCESS_KEY=",
        "SSH_ASKPASS=",
        "GIT_ASKPASS=",
        "PYTHONPATH=",
        "SUPER_SECRET",
    ):
        assert forbidden not in manager_env


def test_systemd_records_exact_prior_state_and_final_quiescence(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env["FAKE_SYSTEMD_STATE"] = str(state)
    (state / "mac-hermes-gateway.service").touch()
    (state / "mac-nemoclaw-gateway.service.absent").touch()
    _install_systemctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    resources = {
        item["name"]: (item["prior_state"], item["state"])
        for item in _receipt(env)["supervisor"]["resources"]
    }
    assert resources == {
        "mac-agent.service": ("active", "inactive"),
        "mac-hermes-gateway.service": ("inactive", "inactive"),
        "mac-openclaw-gateway.service": ("active", "inactive"),
        "mac-nemoclaw-gateway.service": ("absent", "absent"),
    }


def test_systemd_inspection_error_fails_closed_without_raw_output(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env.update(
        {
            "FAKE_SYSTEMD_STATE": str(state),
            "FAKE_SYSTEMD_MODE": "inspect-error",
        }
    )
    _install_systemctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode != 0
    assert "systemd service inspection failed" in result.stderr
    assert "SUPER_SECRET" not in result.stderr
    assert "daemon" not in Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8")
    assert not (Path(env["MAC_HOME"]) / "phase1-cohort-quiescence-generation-rocky-001.json").exists()


def test_supervisor_subprocess_timeout_is_bounded(tmp_path: Path) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env.update(
        {
            "FAKE_SYSTEMD_STATE": str(state),
            "FAKE_SYSTEMD_MODE": "timeout",
            "FAKE_TIMEOUT_CHILD_PID_FILE": str(tmp_path / "timeout-child-pid"),
            "MAC_PHASE1_COMMAND_TIMEOUT_SECONDS": "0.05",
            "MAC_PHASE1_TOTAL_TIMEOUT_SECONDS": "0.2",
        }
    )
    _install_systemctl(tmp_path / "bin")

    started = time.monotonic()
    result = _run(env)
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert elapsed < 1.5
    assert "command timed out" in result.stderr
    assert "daemon" not in Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8")
    child_pid = int(
        Path(env["FAKE_TIMEOUT_CHILD_PID_FILE"]).read_text(encoding="utf-8")
    )
    child_deadline = time.monotonic() + 1
    while time.monotonic() < child_deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("timed-out supervisor command left a child process running")


def test_supervisor_output_is_kernel_capped_while_command_runs(tmp_path: Path) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env.update(
        {
            "FAKE_SYSTEMD_STATE": str(state),
            "FAKE_SYSTEMD_MODE": "output-flood",
            "MAC_PHASE1_COMMAND_TIMEOUT_SECONDS": "2",
            "MAC_PHASE1_TOTAL_TIMEOUT_SECONDS": "3",
        }
    )
    _install_systemctl(tmp_path / "bin")

    started = time.monotonic()
    result = _run(env)
    elapsed = time.monotonic() - started

    assert result.returncode != 0
    assert elapsed < 1.5
    assert "supervisor output exceeded its bound" in result.stderr
    assert "SUPER_SECRET" not in result.stderr
    assert "daemon" not in Path(env["FAKE_PHASE1_EVENTS"]).read_text(
        encoding="utf-8"
    )
    assert not (
        Path(env["MAC_HOME"])
        / "phase1-cohort-quiescence-generation-rocky-001.json"
    ).exists()


def test_actual_production_daemon_block_entrypoint_uses_safe_log_interface(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env.update(
        {
            "FAKE_SYSTEMD_STATE": str(state),
            "FAKE_SYSTEMD_INITIAL": "inactive",
        }
    )
    env["MAC_PHASE1_DAEMON_FUNCTIONS_FILE"] = str(
        _install_production_interface_daemon_block(
            tmp_path, Path(env["FAKE_DAEMON_WRITER"])
        )
    )
    _install_systemctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("phase-1 daemon quiescence in progress") == 1
    assert _receipt(env)["schema"] == "mac.phase1_cohort_quiescence.v1"


def _install_launchctl(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "launchctl",
        """#!/bin/sh
set -eu
command=${1:?}
shift
case "$command" in
  print-disabled)
    domain=${1:?}
    if [ "${FAKE_LAUNCHD_MODE:-normal}" = disable-inspect-error ]; then
      echo 'SUPER_SECRET_LAUNCHD_DISABLE_OUTPUT' >&2
      exit 70
    fi
    printf 'disabled services = {\n'
    for label in \
      com.mac.agent \
      com.mac.hermes-gateway \
      com.mac.openclaw-gateway \
      com.mac.nemoclaw-gateway; do
      target="$domain/$label"
      safe=$(printf '%s' "$target" | tr '/.' '__')
      if [ -f "$FAKE_LAUNCHD_STATE/$safe.disabled" ]; then
        printf '    "%s" => disabled\n' "$label"
      elif [ -f "$FAKE_LAUNCHD_STATE/$safe.enabled" ]; then
        printf '    "%s" => enabled\n' "$label"
      fi
    done
    printf '}\n'
    ;;
  print)
    target=${1:?}
    safe=$(printf '%s' "$target" | tr '/.' '__')
    state_file="$FAKE_LAUNCHD_STATE/$safe"
    case "$target" in
      gui/*/*) ;;
      gui/*)
        if [ "${FAKE_HOST_AUTOMATION_LOADED:-0}" = 1 ]; then
          printf '    com.mac.openclaw-script-memory-sync = { active count = 1 }\n'
        fi
        exit 0
        ;;
    esac
    if [ "${FAKE_LAUNCHD_MODE:-normal}" = inspect-error ] \
        && echo "$target" | grep -q 'openclaw-gateway'; then
      echo 'SUPER_SECRET_LAUNCHD_TRANSPORT_OUTPUT' >&2
      exit 70
    fi
    case "$target" in
      system/*)
        echo 'Could not find service synthetic' >&2
        exit 113
        ;;
    esac
    if echo "$target" | grep -q 'nemoclaw-gateway' \
        && [ "${FAKE_NEMO_ACTIVE:-0}" != 1 ]; then
      echo 'Could not find service synthetic' >&2
      exit 113
    fi
    if [ -f "$state_file" ]; then
      echo 'Could not find service synthetic' >&2
      exit 113
    fi
    exit 0
    ;;
  bootout)
    target=${1:?}
    safe=$(printf '%s' "$target" | tr '/.' '__')
    state_file="$FAKE_LAUNCHD_STATE/$safe"
    : > "$state_file"
    printf 'launchd:%s\\n' "$target" >> "$FAKE_PHASE1_EVENTS"
    ;;
  bootstrap)
    domain=${1:?}
    plist=${2:?}
    label=$(basename "$plist" .plist)
    target="$domain/$label"
    safe=$(printf '%s' "$target" | tr '/.' '__')
    rm -f "$FAKE_LAUNCHD_STATE/$safe"
    printf 'launchd-restore:%s\\n' "$target" >> "$FAKE_PHASE1_EVENTS"
    ;;
  enable|disable)
    target=${1:?}
    safe=$(printf '%s' "$target" | tr '/.' '__')
    if [ "$command" = disable ]; then
      : > "$FAKE_LAUNCHD_STATE/$safe.disabled"
    else
      rm -f "$FAKE_LAUNCHD_STATE/$safe.disabled"
    fi
    printf 'launchd-%s:%s\\n' "$command" "$target" >> "$FAKE_PHASE1_EVENTS"
    ;;
  *) exit 64 ;;
esac
""",
    )


def test_launchd_quiesces_gui_jobs_and_proves_system_domain_absence(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "launchd", os_kind="darwin")
    state = tmp_path / "launchd-state"
    state.mkdir()
    env["FAKE_LAUNCHD_STATE"] = str(state)
    _install_launchctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    events = Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8").splitlines()
    launchd_events = events[:-1]
    assert len(launchd_events) == 3
    assert all(line.startswith("launchd:gui/") for line in launchd_events)
    assert all("control-plane" not in line for line in launchd_events)
    assert events[-1] == "daemon"
    receipt = _receipt(env)
    resources = receipt["supervisor"]["resources"]
    assert len(resources) == 8
    assert {item["state"] for item in resources} == {"absent"}


def test_launchd_records_exact_prior_state_and_final_quiescence(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "launchd", os_kind="darwin")
    state = tmp_path / "launchd-state"
    state.mkdir()
    env["FAKE_LAUNCHD_STATE"] = str(state)
    uid = os.getuid()
    initially_absent = f"gui/{uid}/com.mac.hermes-gateway"
    (state / initially_absent.translate(str.maketrans("/.", "__"))).touch()
    _install_launchctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    resources = {
        item["target"]: (item["prior_state"], item["state"])
        for item in _receipt(env)["supervisor"]["resources"]
    }
    expected_targets = {
        f"{domain}/com.mac.{service}"
        for domain in (f"gui/{uid}", "system")
        for service in (
            "agent",
            "hermes-gateway",
            "openclaw-gateway",
            "nemoclaw-gateway",
        )
    }
    assert set(resources) == expected_targets
    assert resources[initially_absent] == ("absent", "absent")
    for target in expected_targets - {initially_absent}:
        expected_prior = (
            "absent"
            if target.startswith("system/") or "nemoclaw-gateway" in target
            else "active"
        )
        assert resources[target] == (expected_prior, "absent")


def test_launchd_unknown_inspection_state_fails_closed(tmp_path: Path) -> None:
    env = _base_case(tmp_path, "launchd", os_kind="darwin")
    state = tmp_path / "launchd-state"
    state.mkdir()
    env.update(
        {
            "FAKE_LAUNCHD_STATE": str(state),
            "FAKE_LAUNCHD_MODE": "inspect-error",
        }
    )
    _install_launchctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode != 0
    assert "launchd job inspection failed" in result.stderr
    assert "SUPER_SECRET" not in result.stderr
    assert "daemon" not in Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8")


def test_launchd_unavailable_disable_override_inspection_is_ineligible(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "launchd", os_kind="darwin")
    state = tmp_path / "launchd-state"
    state.mkdir()
    env.update(
        {
            "FAKE_LAUNCHD_STATE": str(state),
            "FAKE_LAUNCHD_MODE": "disable-inspect-error",
        }
    )
    _install_launchctl(tmp_path / "bin")

    result = _run_action(env, "prepare")

    assert result.returncode != 0
    assert "launchd disable-override inspection failed" in result.stderr
    assert "SUPER_SECRET" not in result.stderr
    assert Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8") == ""
    assert not (
        Path(env["MAC_HOME"])
        / f"phase1-cohort-restore-contract-{env['DEPLOY_GENERATION']}.json"
    ).exists()


def _install_supervisorctl(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "supervisorctl",
        """#!/bin/sh
set -eu
manager=${FAKE_SUPERVISOR_MANAGER:-user}
command=${1:?}
shift
case "$command" in
  pid)
    if [ "$manager" = privileged ] \
        && [ "${FAKE_SUPERVISOR_SYSTEM_UNUSABLE:-0}" = 1 ]; then
      exit 70
    fi
    if [ "$manager" = privileged ] && [ "${FAKE_SUPERVISOR_PID_MODE:-different}" = dedupe ]; then
      echo 111
    elif [ "$manager" = privileged ]; then
      echo 222
    else
      echo 111
    fi
    ;;
  status)
    program=${1:?}
    if [ "${FAKE_SUPERVISOR_ERROR_MANAGER:-}" = "$manager" ] \
        && [ "${FAKE_SUPERVISOR_ERROR_PROGRAM:-}" = "$program" ]; then
      echo 'SUPER_SECRET_SUPERVISOR_TRANSPORT_OUTPUT' >&2
      exit 70
    fi
    state_file="$FAKE_SUPERVISOR_STATE/$manager.$program"
    if [ -f "$state_file.absent" ]; then
      printf '%s: ERROR (no such process)\n' "$program" >&2
      exit 3
    fi
    if [ -f "$state_file" ]; then
      printf '%s STOPPED Not started\\n' "$program"
      exit 3
    fi
    if [ "$program" = mac-nemoclaw-gateway ] \
        && [ "${FAKE_NEMO_ACTIVE:-0}" != 1 ]; then
      printf '%s STOPPED Not started\\n' "$program"
      exit 3
    fi
    printf '%s RUNNING pid 456, uptime 0:00:01\\n' "$program"
    ;;
  stop)
    program=${1:?}
    : > "$FAKE_SUPERVISOR_STATE/$manager.$program"
    printf 'supervisord:%s:%s\\n' "$manager" "$program" >> "$FAKE_PHASE1_EVENTS"
    ;;
  *) exit 64 ;;
esac
""",
    )


def test_supervisord_quiesces_every_usable_distinct_manager(tmp_path: Path) -> None:
    env = _base_case(tmp_path, "supervisord")
    state = tmp_path / "supervisor-state"
    state.mkdir()
    env["FAKE_SUPERVISOR_STATE"] = str(state)
    env["FAKE_SUPERVISOR_PID_MODE"] = "different"
    _install_supervisorctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    events = Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8").splitlines()
    stops = [line for line in events if line.startswith("supervisord:")]
    assert len(stops) == 6
    assert {line.split(":", 2)[1] for line in stops} == {"user", "privileged"}
    assert all("control-plane" not in line for line in stops)
    assert events[-1] == "daemon"
    managers = _receipt(env)["supervisor"]["managers"]
    assert len(managers) == 2
    assert all(len(item["resources"]) == 4 for item in managers)
    assert {item["scope"] for item in managers} == {"system", "user"}


def test_supervisord_records_exact_prior_state_and_final_quiescence(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "supervisord")
    state = tmp_path / "supervisor-state"
    state.mkdir()
    env["FAKE_SUPERVISOR_STATE"] = str(state)
    env["FAKE_SUPERVISOR_PID_MODE"] = "dedupe"
    (state / "privileged.mac-hermes-gateway").touch()
    (state / "privileged.mac-nemoclaw-gateway.absent").touch()
    _install_supervisorctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    system_manager = next(
        item
        for item in _receipt(env)["supervisor"]["managers"]
        if item["scope"] == "system"
    )
    resources = {
        item["name"]: (item["prior_state"], item["state"])
        for item in system_manager["resources"]
    }
    assert resources == {
        "mac-agent": ("RUNNING", "STOPPED"),
        "mac-hermes-gateway": ("STOPPED", "STOPPED"),
        "mac-openclaw-gateway": ("RUNNING", "STOPPED"),
        "mac-nemoclaw-gateway": ("absent", "absent"),
    }


def test_supervisord_deduplicates_two_clients_for_one_manager(tmp_path: Path) -> None:
    env = _base_case(tmp_path, "supervisord")
    state = tmp_path / "supervisor-state"
    state.mkdir()
    env["FAKE_SUPERVISOR_STATE"] = str(state)
    env["FAKE_SUPERVISOR_PID_MODE"] = "dedupe"
    _install_supervisorctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode == 0, result.stderr
    events = Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8").splitlines()
    assert len([line for line in events if line.startswith("supervisord:")]) == 3
    managers = _receipt(env)["supervisor"]["managers"]
    assert len(managers) == 1
    assert managers[0]["scope"] == "system"


def test_supervisord_requires_the_canonical_system_manager(tmp_path: Path) -> None:
    env = _base_case(tmp_path, "supervisord")
    state = tmp_path / "supervisor-state"
    state.mkdir()
    env.update(
        {
            "FAKE_SUPERVISOR_STATE": str(state),
            "FAKE_SUPERVISOR_SYSTEM_UNUSABLE": "1",
        }
    )
    _install_supervisorctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode != 0
    assert "system supervisord manager could not be inspected" in result.stderr
    assert "daemon" not in Path(env["FAKE_PHASE1_EVENTS"]).read_text(
        encoding="utf-8"
    )


def test_one_usable_supervisord_manager_inspection_error_blocks_all(
    tmp_path: Path,
) -> None:
    env = _base_case(tmp_path, "supervisord")
    state = tmp_path / "supervisor-state"
    state.mkdir()
    env.update(
        {
            "FAKE_SUPERVISOR_STATE": str(state),
            "FAKE_SUPERVISOR_PID_MODE": "different",
            "FAKE_SUPERVISOR_ERROR_MANAGER": "privileged",
            "FAKE_SUPERVISOR_ERROR_PROGRAM": "mac-openclaw-gateway",
        }
    )
    _install_supervisorctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode != 0
    assert "supervisord program inspection" in result.stderr
    assert "SUPER_SECRET" not in result.stderr
    assert "daemon" not in Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("manager", ["systemd", "launchd", "supervisord"])
def test_active_nemo_gateway_fails_before_any_phase1_mutation(
    tmp_path: Path, manager: str
) -> None:
    os_kind = "darwin" if manager == "launchd" else "linux"
    env = _base_case(tmp_path, manager, os_kind=os_kind)
    env["FAKE_NEMO_ACTIVE"] = "1"
    if manager == "systemd":
        state = tmp_path / "systemd-state"
        state.mkdir()
        env["FAKE_SYSTEMD_STATE"] = str(state)
        _install_systemctl(tmp_path / "bin")
    elif manager == "launchd":
        state = tmp_path / "launchd-state"
        state.mkdir()
        env["FAKE_LAUNCHD_STATE"] = str(state)
        _install_launchctl(tmp_path / "bin")
    else:
        state = tmp_path / "supervisor-state"
        state.mkdir()
        env["FAKE_SUPERVISOR_STATE"] = str(state)
        env["FAKE_SUPERVISOR_PID_MODE"] = "dedupe"
        _install_supervisorctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode != 0
    assert "active Nemo gateway cannot be restored" in result.stderr
    assert Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8") == ""
    assert not (
        Path(env["MAC_HOME"])
        / f"phase1-cohort-quiescence-{env['DEPLOY_GENERATION']}.json"
    ).exists()


@pytest.mark.parametrize("media_state", ["active", "inactive"])
def test_present_media_gen_service_is_ineligible_before_phase1_mutation(
    tmp_path: Path, media_state: str
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env.update(
        {
            "FAKE_SYSTEMD_STATE": str(state),
            "FAKE_MEDIA_STATE": media_state,
        }
    )
    _install_systemctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode != 0
    assert "media-gen service is ineligible" in result.stderr
    assert Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8") == ""


def test_synchronized_node_install_never_creates_unjournaled_media_gen_units() -> None:
    source = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    body = source.split("install_gpu_gen_server() {", 1)[1].split(
        "\n}\n\ninstall_agent_footprint() {", 1
    )[0]
    gate = body.index("MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE")
    gpu_probe = body.index("nvidia-smi")
    unit_install = body.index("_install_gen_unit")
    assert gate < gpu_probe < unit_install


def test_synchronized_openclaw_prepare_blocks_new_host_automation() -> None:
    node_source = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    prepare = node_source.split("prepare_openclaw_gateway() {", 1)[1].split(
        "\n}\n\nverify_openclaw_gateway() {", 1
    )[0]
    assert "MAC_OPENCLAW_REQUIRE_NO_HOST_SCRIPT_AUTOMATION=1" in prepare

    installer = (
        ROOT / "deploy" / "openclaw" / "install-openclaw-gateway.sh"
    ).read_text(encoding="utf-8")
    scheduling = installer.split("install_host_script_runner() {", 1)[1].split(
        "\n}\n\nprepare() {", 1
    )[0]
    gate = scheduling.index("MAC_OPENCLAW_REQUIRE_NO_HOST_SCRIPT_AUTOMATION")
    launchd = scheduling.index("schedule_launchd_script_job")
    systemd = scheduling.index("schedule_systemd_script_job")
    assert gate < launchd and gate < systemd


@pytest.mark.parametrize("manager", ["systemd", "launchd"])
def test_unjournaled_openclaw_host_automation_fails_before_phase1_mutation(
    tmp_path: Path, manager: str
) -> None:
    os_kind = "darwin" if manager == "launchd" else "linux"
    env = _base_case(tmp_path, manager, os_kind=os_kind)
    home = Path(env["HOME"])
    if manager == "systemd":
        state = tmp_path / "systemd-state"
        state.mkdir()
        env["FAKE_SYSTEMD_STATE"] = str(state)
        _install_systemctl(tmp_path / "bin")
        definition = (
            home
            / ".config"
            / "systemd"
            / "user"
            / "mac-openclaw-script-memory-sync.timer"
        )
    else:
        state = tmp_path / "launchd-state"
        state.mkdir()
        env["FAKE_LAUNCHD_STATE"] = str(state)
        _install_launchctl(tmp_path / "bin")
        definition = (
            home
            / "Library"
            / "LaunchAgents"
            / "com.mac.openclaw-script-memory-sync.plist"
        )
    definition.parent.mkdir(parents=True)
    definition.write_text("prior generation\n", encoding="utf-8")

    result = _run(env)

    assert result.returncode != 0
    assert "host automation lacks an exact restore journal" in result.stderr
    assert Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("manager", ["systemd", "launchd"])
def test_loaded_openclaw_host_automation_fails_without_definition_files(
    tmp_path: Path, manager: str
) -> None:
    os_kind = "darwin" if manager == "launchd" else "linux"
    env = _base_case(tmp_path, manager, os_kind=os_kind)
    env["FAKE_HOST_AUTOMATION_LOADED"] = "1"
    if manager == "systemd":
        state = tmp_path / "systemd-state"
        state.mkdir()
        env["FAKE_SYSTEMD_STATE"] = str(state)
        _install_systemctl(tmp_path / "bin")
    else:
        state = tmp_path / "launchd-state"
        state.mkdir()
        env["FAKE_LAUNCHD_STATE"] = str(state)
        _install_launchctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode != 0
    assert "loaded OpenClaw host automation lacks" in result.stderr
    assert Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("manager", ["systemd", "launchd", "supervisord"])
@pytest.mark.parametrize(
    "tamper",
    [
        "missing-prior-state",
        "malformed-prior-state",
        "nonquiescent-final-state",
        "wrong-generation",
        "wrong-identity",
    ],
)
def test_tampered_supervisor_transition_cannot_publish_phase1_receipt(
    tmp_path: Path, manager: str, tamper: str
) -> None:
    os_kind = "darwin" if manager == "launchd" else "linux"
    env = _base_case(tmp_path, manager, os_kind=os_kind)
    env["FAKE_SUPERVISOR_TAMPER"] = tamper
    if manager == "systemd":
        state = tmp_path / "systemd-state"
        state.mkdir()
        env["FAKE_SYSTEMD_STATE"] = str(state)
        _install_systemctl(tmp_path / "bin")
    elif manager == "launchd":
        state = tmp_path / "launchd-state"
        state.mkdir()
        env["FAKE_LAUNCHD_STATE"] = str(state)
        _install_launchctl(tmp_path / "bin")
    else:
        state = tmp_path / "supervisor-state"
        state.mkdir()
        env["FAKE_SUPERVISOR_STATE"] = str(state)
        env["FAKE_SUPERVISOR_PID_MODE"] = "dedupe"
        _install_supervisorctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode != 0
    assert "phase-1 quiescence failed" in result.stderr
    assert not (
        Path(env["MAC_HOME"])
        / ("phase1-cohort-quiescence-%s.json" % env["DEPLOY_GENERATION"])
    ).exists()


@pytest.mark.parametrize("mode", ["wrong-generation", "raw-output"])
def test_invalid_daemon_receipt_cannot_publish_phase1_receipt(
    tmp_path: Path, mode: str
) -> None:
    env = _base_case(tmp_path, "systemd")
    state = tmp_path / "systemd-state"
    state.mkdir()
    env.update(
        {
            "FAKE_SYSTEMD_STATE": str(state),
            "FAKE_SYSTEMD_INITIAL": "inactive",
            "FAKE_DAEMON_MODE": mode,
        }
    )
    _install_systemctl(tmp_path / "bin")

    result = _run(env)

    assert result.returncode != 0
    assert not (Path(env["MAC_HOME"]) / "phase1-cohort-quiescence-generation-rocky-001.json").exists()


def test_daemon_function_block_must_not_be_a_symlink(tmp_path: Path) -> None:
    env = _base_case(tmp_path, "systemd")
    real_block = Path(env["MAC_PHASE1_DAEMON_FUNCTIONS_FILE"])
    linked = tmp_path / "linked-daemon-functions.sh"
    linked.symlink_to(real_block)
    env["MAC_PHASE1_DAEMON_FUNCTIONS_FILE"] = str(linked)

    result = _run(env)

    assert result.returncode != 0
    assert "not a readable regular file" in result.stderr
    assert "daemon" not in Path(env["FAKE_PHASE1_EVENTS"]).read_text(encoding="utf-8")
