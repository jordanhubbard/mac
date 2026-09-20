"""Run the recovery shell against real key installation and a local hub boundary."""

import base64
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading

import pytest

from mac.deploy_env import read_env_file
from mac.services import sign_verification_manifest


ROOT = Path(__file__).resolve().parents[1]


def _shell_function(source, name, next_name, *, subshell=False):
    opening, closing = ("(", ")") if subshell else ("{", "}")
    start = f"{name}() {opening}"
    body = source.split(start, 1)[1].split(f"\n{closing}\n\n{next_name}", 1)[0]
    return start + body + f"\n{closing}\n"


def _run_recovery(
    tmp_path,
    *,
    ordinal=0,
    retained=True,
    key_valid=False,
    failure="",
    proof_fault="",
    commit_length=40,
    recovery_from_state="quiesced",
    unit_load_state="loaded",
):
    source = (ROOT / "deploy/deploy-mac-fleet.sh").read_text()
    names = (
        (
            "phase1_restore_contract_file_for_agent",
            "phase1_restore_contract_digest_for_agent",
            False,
        ),
        ("phase1_resolved_supervisor_for_agent", "cleanup_failed_phase1_prepare_lock", False),
        ("restart_remote_mac_agent_under_epoch", "hub_target", False),
        (
            "retain_remote_generation_for_forward_repair",
            "write_cohort_composite_rollback_evidence",
            False,
        ),
        ("recover_cohort_node", "recover_active_cohort_transaction", False),
        (
            "reconcile_bound_worker_attestation_key",
            "reconcile_report_repository_executor_approval",
            True,
        ),
    )
    functions = "\n".join(
        _shell_function(source, name, next_name, subshell=subshell)
        for name, next_name, subshell in names
    )
    home = tmp_path / "home"
    mac_home = home / ".mac"
    mac_home.mkdir(parents=True, mode=0o700)
    (mac_home / "venv").symlink_to(sys.prefix, target_is_directory=True)
    controller = tmp_path / "controller"
    controller.mkdir(mode=0o700)
    staged = tmp_path / "staged"
    staged.mkdir()
    worker = tmp_path / "worker"
    worker.write_text("running")
    calls = tmp_path / "calls"
    hold = tmp_path / "hold"
    hold.write_text("existing operator hold")
    agent = f"node-{ordinal}"
    state = {"key": "registered-key-" + "a" * 48, "verified": [], "rotations": 0}
    # The old controller directory is gone. Only the old node's durable
    # contract and the journal's digest survive; the new controller is empty.
    contract = {
        "schema": "mac.phase1_cohort_restore_contract.v1",
        "status": "prepared",
        "agent": agent,
        "generation": f"generation-{ordinal}",
        "revision": "d" * commit_length,
        "fleet": "fleet",
        "rollback_capable": True,
        "supervisor": {"manager": "systemd"},
    }
    if proof_fault in {"agent", "generation", "revision", "fleet"}:
        contract[proof_fault] = "different"
    elif proof_fault == "manager":
        contract["supervisor"]["manager"] = "unrecognized"
    raw_contract = (json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n").encode()
    contract_digest = hashlib.sha256(raw_contract).hexdigest()
    contract_path = mac_home / f"phase1-cohort-restore-contract-generation-{ordinal}.json"
    contract_path.write_bytes(raw_contract)
    contract_path.chmod(0o600)
    if recovery_from_state == "phase1_prepare_started":
        contract_path.unlink()
        contract_digest = None
    if proof_fault == "missing":
        contract_path.unlink()
    elif proof_fault == "digest":
        contract_digest = "0" * 64
    elif proof_fault == "mode":
        contract_path.chmod(0o644)
    elif proof_fault == "symlink":
        target = mac_home / "other-contract.json"
        contract_path.rename(target)
        contract_path.symlink_to(target)
    if not retained and proof_fault != "current_missing":
        current = {**contract, "generation": "recovery-deployment", "revision": "e" * 40}
        current_raw = (json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ready = {
            "schema": "mac.phase1_restore_contract_ready.v1",
            "agent": agent,
            "generation": "recovery-deployment",
            "revision": "e" * 40,
            "contract": current,
            "contract_sha256": hashlib.sha256(current_raw).hexdigest(),
        }
        ready_path = controller / f"phase1-restore-contract-agent_{agent}.json"
        ready_path.write_text(json.dumps(ready))
        ready_path.chmod(0o600)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    service = bin_dir / "systemctl"
    service.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        ' show) printf "%s\\n" "$UNIT_LOAD_STATE" ;;\n'
        ' stop) printf "service stop\\n" >> "$CALLS"; '
        '[ "$FAILURE" != stop ] || exit 73; printf stopped > "$WORKER" ;;\n'
        ' restart) printf "restart\\n" >> "$CALLS"; printf running > "$WORKER" ;;\n'
        " *) exit 74 ;;\n"
        "esac\n"
    )
    service.chmod(0o700)
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\n[ "$1" != -n ] || shift\nexec "$@"\n')
    sudo.chmod(0o700)

    class Hub(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == f"/agents/agent_{agent}/attestation-key/verify":
                valid = hmac.compare_digest(
                    sign_verification_manifest(state["key"], body["challenge"]),
                    body["signature"],
                )
                if failure == "second-proof" and state["rotations"]:
                    valid = False
                state["verified"].append(valid)
                result = {"valid": valid}
            elif self.path == f"/agents/agent_{agent}/attestation-key/recover":
                state["key"] = "recovered-key-" + "b" * 48
                state["rotations"] += 1
                result = {"attestation_key": state["key"]}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env_file = mac_home / "mac.env"
    installed_key = state["key"] if key_valid else "aborted-candidate-" + "c" * 48
    env_file.write_text(
        f"MAC_ATTESTATION_KEY={installed_key}\n"
        f"MAC_HUB_URL=http://127.0.0.1:{server.server_port}\n"
        "MAC_API_TOKEN=fixture-admin\nMAC_WORKER_TOKEN=discarded-pending-worker\n"
    )
    env_file.chmod(0o600)
    candidate = base64.b64encode(
        json.dumps(
            {
                "agent_name": agent,
                "stable_id": "agent_" + agent,
                "generation": f"generation-{ordinal}",
                "deployment_id": "aborted-deployment",
                "deploy_ts": "fixture",
                "source_commit": "d" * commit_length,
                "os": "linux",
                "supervisor": "auto",
                "recovery_action": "retain_forward",
                "recovery_from_state": recovery_from_state,
                "restore_contract_sha256": contract_digest,
            }
        ).encode()
    ).decode()
    # Only transport, supervisor commands and journal writes are replaced.
    # The composed recovery, phase-one proof resolver and service-control
    # shell run together, as do private-file checks, key installation and signed
    # HTTP proof all execute. The conditional deliberately disables implicit
    # errexit, as the real recovery caller does.
    stubs = r"""
record() { printf '%s\n' "$*" >> "$CALLS"; }
deployment_id_for_agent() { printf '%s' recovery-deployment; }
stable_worker_agent_id() { printf 'agent_%s' "$1"; }
assert_remote_deployment_lock() { test "$(cat "$LOCK")" = "$2"; }
acquire_remote_deployment_lock() { printf '%s' "$2" > "$LOCK"; }
release_remote_deployment_lock() { assert_remote_deployment_lock "$@" && rm -f "$LOCK"; }
cohort_journal_mutate() { record "journal $1"; }
hub_agent_restart_gate() {
  test "$FAILURE" != rehold || return 73
  test "$(cat "$HOLD")" = 'existing operator hold'
}
set_remote_mac_startup_hold_policy() {
  test "$FAILURE" != startup-policy || return 73
  test "$2" = 0
}
staged_bundle_remote_root_for_deployment() { printf '%s' "$STAGED"; }
run_fenced_remote_python() {
  assert_remote_deployment_lock "$1" "$2" || return 1
  local code="$3"; shift 3
  if [[ "$code" == *mac.fleet_node_forward_retention* ]]; then
    test "$FAILURE" != retention-write || return 73
  fi
  "$PYTHON_BIN" -c "$code" "$@"
}
ssh_target_args() { printf 'fixture-host\0'; }
shell_quote() { printf '%q' "$1"; }
remote_deployment_fenced_exec() {
  assert_remote_deployment_lock ignored "$1" || return 1
  shift 2; printf '%q ' "$@"
}
fenced_remote_upload() {
  test "$FAILURE" != upload || return 73
  assert_remote_deployment_lock "$1" "$2" || return 1
  cp -f "$3" "$4"; chmod 0600 "$4"
}
ssh() {
  local command="${!#}"
  if [[ "$command" == *mac.deployment_attestation*install* ]]; then
    record install
    test "$FAILURE" != install || return 73
  fi
  bash -c "$command"
}
"""
    command = (
        f"recover_cohort_node epoch owner fleet {shlex.quote(candidate)} hub"
        if retained
        else f"reconcile_bound_worker_attestation_key {agent} hub systemd fleet"
    )
    if not retained:
        (tmp_path / "lock").write_text("recovery-deployment")
    script = (
        "set -u\n"
        f"PYTHON_BIN={shlex.quote(sys.executable)}\n"
        f"TMPDIR_LOCAL={shlex.quote(str(controller))}\n"
        "TS=fixture\nCOHORT_JOURNAL_REVISION=1\n"
        f"GIT_REV={'e' * 40}\n"
        + functions
        + stubs
        + f"\nif {command}; then exit 0; else exit 1; fi\n"
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(ROOT / "src"),
        "CALLS": str(calls),
        "LOCK": str(tmp_path / "lock"),
        "HOLD": str(hold),
        "STAGED": str(staged),
        "WORKER": str(worker),
        "FAILURE": failure,
        "UNIT_LOAD_STATE": unit_load_state,
    }
    try:
        result = subprocess.run(
            ["bash", "-c", script], env=env, text=True, capture_output=True, timeout=40
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return {
        "result": result,
        "worker": worker.read_text(),
        "calls": calls.read_text().splitlines() if calls.exists() else [],
        "state": state,
        "env": read_env_file(env_file),
        "home": mac_home,
        "hold": hold.read_text(),
        "controller": controller,
    }


@pytest.mark.parametrize("ordinal", range(3))
@pytest.mark.parametrize("key_valid", [False, True])
@pytest.mark.parametrize("commit_length", [40, 64])
def test_retained_recovery_keeps_each_cohort_worker_stopped(
    tmp_path, ordinal, key_valid, commit_length
):
    observed = _run_recovery(
        tmp_path, ordinal=ordinal, key_valid=key_valid, commit_length=commit_length
    )
    assert observed["result"].returncode == 0, observed["result"].stderr
    assert observed["worker"] == "stopped"
    assert "restart" not in observed["calls"]
    assert observed["calls"][-1] == "journal aborted-node"
    assert observed["state"]["verified"][-1] is True
    assert observed["state"]["rotations"] == (0 if key_valid else 1)
    assert observed["env"]["MAC_ATTESTATION_KEY"] == observed["state"]["key"]
    assert observed["env"]["MAC_WORKER_TOKEN"] == "discarded-pending-worker"
    assert observed["hold"] == "existing operator hold"
    barrier = observed["home"] / "deploy-start-barrier"
    assert barrier.read_text().strip() == f"generation-{ordinal}"
    assert not list((observed["home"] / "attestation-recovery").glob("*.json"))
    assert not list(observed["controller"].glob("phase1-restore-contract-*.json"))


@pytest.mark.parametrize(
    "proof_fault",
    ["missing", "digest", "agent", "generation", "revision", "fleet", "manager", "mode", "symlink"],
)
def test_retained_recovery_requires_exact_durable_supervisor_proof(tmp_path, proof_fault):
    observed = _run_recovery(tmp_path, proof_fault=proof_fault)
    assert observed["result"].returncode != 0
    assert observed["worker"] == "running"
    assert "service stop" not in observed["calls"]
    assert "restart" not in observed["calls"]
    assert "journal aborted-node" not in observed["calls"]
    assert observed["state"]["rotations"] == 0
    assert observed["hold"] == "existing operator hold"


def test_activation_cannot_substitute_retained_proof_for_current_phase_one(tmp_path):
    observed = _run_recovery(tmp_path, retained=False, proof_fault="current_missing")
    assert observed["result"].returncode != 0
    assert "restart" not in observed["calls"]


def test_normal_attestation_recovery_still_restarts_after_key_install(tmp_path):
    observed = _run_recovery(tmp_path, retained=False)
    assert observed["result"].returncode == 0, observed["result"].stderr
    assert observed["worker"] == "running"
    assert observed["calls"] == ["install", "restart"]
    assert observed["state"]["verified"] == [False, True]


@pytest.mark.parametrize("phase", ["phase1_prepare_started", "phase1_armed"])
def test_pre_quiescence_retention_does_not_stop_or_rekey_running_worker(tmp_path, phase):
    observed = _run_recovery(tmp_path, recovery_from_state=phase)
    assert observed["result"].returncode == 0, observed["result"].stderr
    assert observed["worker"] == "running"
    assert observed["calls"] == ["journal abort-start", "journal aborted-node"]
    assert observed["state"]["rotations"] == 0
    assert observed["state"]["verified"] == []


@pytest.mark.parametrize("phase", ["", "aborting", "planned", "finalized"])
def test_retention_rejects_unknown_forward_phase_before_mutation(tmp_path, phase):
    observed = _run_recovery(tmp_path, recovery_from_state=phase)
    assert observed["result"].returncode != 0
    assert observed["worker"] == "running"
    assert observed["calls"] == []
    assert observed["state"]["rotations"] == 0


@pytest.mark.parametrize(
    "phase", ["quiesce_started", "quiesced", "phase2_armed", "phase2_started", "prepared"]
)
def test_post_quiescence_retention_stops_and_proves_worker_before_completion(tmp_path, phase):
    observed = _run_recovery(tmp_path, recovery_from_state=phase)
    assert observed["result"].returncode == 0, observed["result"].stderr
    assert observed["worker"] == "stopped"
    assert observed["calls"] == [
        "journal abort-start",
        "service stop",
        "install",
        "journal aborted-node",
    ]
    assert observed["state"]["verified"] == [False, True]


def test_post_quiescence_retention_accepts_exactly_absent_systemd_worker(tmp_path):
    observed = _run_recovery(
        tmp_path,
        recovery_from_state="quiesce_started",
        unit_load_state="not-found",
    )
    assert observed["result"].returncode == 0, observed["result"].stderr
    assert observed["worker"] == "running"
    assert observed["calls"] == [
        "journal abort-start",
        "install",
        "journal aborted-node",
    ]
    assert observed["state"]["verified"] == [False, True]


@pytest.mark.parametrize("failure", ["stop", "upload", "install", "second-proof"])
def test_retained_recovery_does_not_report_success_after_boundary_failure(tmp_path, failure):
    observed = _run_recovery(tmp_path, failure=failure)
    assert observed["result"].returncode != 0
    assert "journal aborted-node" not in observed["calls"]
    assert "restart" not in observed["calls"]
    if failure != "stop":
        assert observed["worker"] == "stopped"
    if failure in {"stop", "upload", "install"}:
        assert True not in observed["state"]["verified"]


@pytest.mark.parametrize("failure", ["rehold", "startup-policy", "retention-write"])
@pytest.mark.parametrize("phase", ["phase1_prepare_started", "quiesced"])
def test_failed_retention_prerequisite_cannot_advance_recovery(tmp_path, failure, phase):
    observed = _run_recovery(tmp_path, failure=failure, recovery_from_state=phase)
    assert observed["result"].returncode != 0
    assert observed["calls"] == ["journal abort-start"]
    assert observed["state"]["rotations"] == 0
    assert observed["state"]["verified"] == []
    assert observed["worker"] == "running"
    assert observed["hold"] == "existing operator hold"


@pytest.mark.parametrize("supervisor", ["systemd", "launchd", "supervisord"])
@pytest.mark.parametrize("action", ["activate", "restart", "stop"])
@pytest.mark.parametrize("upload_fails", [False, True])
@pytest.mark.parametrize("retained_proof", [False, True])
def test_epoch_worker_lifecycle_uses_only_the_selected_supervisor(
    tmp_path, supervisor, action, upload_fails, retained_proof
):
    source = (ROOT / "deploy/deploy-mac-fleet.sh").read_text()
    function = _shell_function(source, "restart_remote_mac_agent_under_epoch", "hub_target")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for name in ["systemctl", "supervisorctl"]:
        command = bin_dir / name
        command.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
            'if [ "$1" = show ]; then printf "loaded\\n"; fi\n'
        )
        command.chmod(0o700)
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\n[ "$1" != -n ] || shift\nexec "$@"\n')
    sudo.chmod(0o700)
    # No phase-two install has run: its timestamped helper does not exist.
    # The operation must transport its own reviewed helper before invoking it.
    lifecycle = tmp_path / "deploy/lib/launchd-lifecycle.sh"
    lifecycle.parent.mkdir(parents=True)
    lifecycle.write_text(
        'mac_launchd_stop_job_if_present() { printf "stop\\n" >> "$CALLS"; }\n'
        'mac_launchd_bootstrap_job() { printf "bootstrap\\n" >> "$CALLS"; }\n'
    )
    if action != "stop":
        plist = tmp_path / "Library/LaunchAgents/com.fleet.agent.plist"
        plist.parent.mkdir(parents=True)
        plist.touch()
    stubs = r"""
deployment_id_for_agent() { printf recovery-deployment; }
assert_remote_deployment_lock() { :; }
phase1_resolved_supervisor_for_agent() { printf '%s' "$SUPERVISOR"; }
ssh_target_args() { printf 'fixture-host\0'; }
shell_quote() { printf '%q' "$1"; }
fenced_remote_upload() {
  test "$2" = recovery-deployment || return 1
  test "$3" = "$ROOT/deploy/lib/launchd-lifecycle.sh" || return 1
  test "$UPLOAD_FAILS" = 0 || return 73
  cp -f "$3" "$4"
}
remote_deployment_fenced_exec() { shift 2; printf '%q ' "$@"; }
ssh() { bash -c "${!#}"; }
"""
    recovery_args = f" old-generation {'d' * 40} {'a' * 64}" if retained_proof else ""
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -eu\nTS=fixture\n"
            + function
            + stubs
            + f"\nif restart_remote_mac_agent_under_epoch node {supervisor} fleet {action}{recovery_args}; "
            + "then exit 0; else exit $?; fi",
        ],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "CALLS": str(calls),
            "SUPERVISOR": supervisor,
            "ROOT": str(tmp_path),
            "DEPLOY_CONTROLLER_NONCE": tmp_path.name,
            "UPLOAD_FAILS": "1" if upload_fails else "0",
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    if retained_proof and action != "stop":
        assert result.returncode != 0
        assert not calls.exists(), "retained proof cannot activate a successor"
        return
    if supervisor == "launchd" and upload_fails:
        assert result.returncode != 0
        assert not calls.exists(), "failed helper transport must not touch the service"
        return
    assert result.returncode == 0, result.stderr
    if supervisor == "launchd":
        expected = ["stop"] if action == "stop" else ["stop", "bootstrap"]
    else:
        verb = "start" if action == "activate" else action
        unit = "fleet-agent.service" if supervisor == "systemd" else "fleet-agent"
        expected = [f"{verb} {unit}"]
        if supervisor == "systemd":
            expected.insert(0, f"show {unit} --property=LoadState --value")
    assert calls.read_text().splitlines() == expected


@pytest.mark.parametrize("action", ["activate", "restart", "stop"])
@pytest.mark.parametrize(
    ("load_state", "show_rc"),
    [("not-found", 0), ("masked", 0), ("", 0), ("loaded", 71)],
)
def test_epoch_systemd_lifecycle_fails_closed_except_absent_stop(
    tmp_path, action, load_state, show_rc
):
    source = (ROOT / "deploy/deploy-mac-fleet.sh").read_text()
    function = _shell_function(source, "restart_remote_mac_agent_under_epoch", "hub_target")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
        'if [ "$1" = show ]; then printf "%s\\n" "$LOAD_STATE"; exit "$SHOW_RC"; fi\n'
        "exit 0\n"
    )
    systemctl.chmod(0o700)
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\n[ "$1" != -n ] || shift\nexec "$@"\n')
    sudo.chmod(0o700)
    stubs = r"""
deployment_id_for_agent() { printf recovery-deployment; }
assert_remote_deployment_lock() { :; }
phase1_resolved_supervisor_for_agent() { printf systemd; }
ssh_target_args() { printf 'fixture-host\0'; }
shell_quote() { printf '%q' "$1"; }
remote_deployment_fenced_exec() { shift 2; printf '%q ' "$@"; }
ssh() { bash -c "${!#}"; }
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -eu\nTS=fixture\n"
            + function
            + stubs
            + f"\nrestart_remote_mac_agent_under_epoch node systemd fleet {action}",
        ],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "CALLS": str(calls),
            "LOAD_STATE": load_state,
            "SHOW_RC": str(show_rc),
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    expected_success = action == "stop" and load_state == "not-found" and show_rc == 0
    assert (result.returncode == 0) is expected_success, result.stderr
    observed = calls.read_text().splitlines()
    assert observed == ["show fleet-agent.service --property=LoadState --value"]
