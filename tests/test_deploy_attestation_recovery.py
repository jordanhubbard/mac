"""Run the recovery shell against real key installation and a local hub boundary."""

import base64
import hmac
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


def _run_recovery(tmp_path, *, ordinal=0, retained=True, key_valid=False, failure=""):
    source = (ROOT / "deploy/deploy-mac-fleet.sh").read_text()
    names = (
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
                "source_commit": "d" * 40,
                "os": "linux",
                "supervisor": "systemd",
                "recovery_action": "retain_forward",
            }
        ).encode()
    ).decode()
    # Only transport, supervisor and journal boundaries are replaced. The
    # reviewed shell, private-file checks, key install/consumption and signed
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
hub_agent_restart_gate() { test "$(cat "$HOLD")" = 'existing operator hold'; }
set_remote_mac_startup_hold_policy() { test "$2" = 0; }
staged_bundle_remote_root_for_deployment() { printf '%s' "$STAGED"; }
run_fenced_remote_python() {
  assert_remote_deployment_lock "$1" "$2" || return 1
  local code="$3"; shift 3
  "$PYTHON_BIN" -c "$code" "$@"
}
restart_remote_mac_agent_under_epoch() {
  if [ "${4:-activate}" = stop ]; then
    record 'service stop'
    test "$FAILURE" != stop || return 73
    printf stopped > "$WORKER"
  else
    test "${4:-activate}" = restart || return 74
    record restart; printf running > "$WORKER"
  fi
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
        + functions
        + stubs
        + f"\nif {command}; then exit 0; else exit 1; fi\n"
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "PYTHONPATH": str(ROOT / "src"),
        "CALLS": str(calls),
        "LOCK": str(tmp_path / "lock"),
        "HOLD": str(hold),
        "STAGED": str(staged),
        "WORKER": str(worker),
        "FAILURE": failure,
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
    }


@pytest.mark.parametrize("ordinal", range(3))
@pytest.mark.parametrize("key_valid", [False, True])
def test_retained_recovery_keeps_each_cohort_worker_stopped(tmp_path, ordinal, key_valid):
    observed = _run_recovery(tmp_path, ordinal=ordinal, key_valid=key_valid)
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


def test_normal_attestation_recovery_still_restarts_after_key_install(tmp_path):
    observed = _run_recovery(tmp_path, retained=False)
    assert observed["result"].returncode == 0, observed["result"].stderr
    assert observed["worker"] == "running"
    assert observed["calls"] == ["install", "restart"]
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


@pytest.mark.parametrize("supervisor", ["systemd", "launchd", "supervisord"])
@pytest.mark.parametrize("action", ["activate", "restart", "stop"])
def test_epoch_worker_lifecycle_uses_only_the_selected_supervisor(tmp_path, supervisor, action):
    source = (ROOT / "deploy/deploy-mac-fleet.sh").read_text()
    function = _shell_function(source, "restart_remote_mac_agent_under_epoch", "hub_target")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for name in ["systemctl", "supervisorctl"]:
        command = bin_dir / name
        command.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
        command.chmod(0o700)
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\n[ "$1" != -n ] || shift\nexec "$@"\n')
    sudo.chmod(0o700)
    lifecycle = tmp_path / ".mac/logs/launchd-lifecycle-fixture.sh"
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
            + f"\nrestart_remote_mac_agent_under_epoch node {supervisor} fleet {action}",
        ],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "CALLS": str(calls),
            "SUPERVISOR": supervisor,
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    if supervisor == "launchd":
        expected = ["stop"] if action == "stop" else ["stop", "bootstrap"]
    else:
        verb = "start" if action == "activate" else action
        unit = "fleet-agent.service" if supervisor == "systemd" else "fleet-agent"
        expected = [f"{verb} {unit}"]
    assert calls.read_text().splitlines() == expected
