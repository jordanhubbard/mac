import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pytest
import shutil
import shlex
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"
NODE_INSTALL_SCRIPT = ROOT / "deploy" / "fleet-node-install.sh"
LAUNCHD_LIFECYCLE_SCRIPT = ROOT / "deploy" / "lib" / "launchd-lifecycle.sh"


def script_text():
    return (
        DEPLOY_SCRIPT.read_text(encoding="utf-8")
        + "\n"
        + NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    )


def _launchd_stop_function_variants():
    lifecycle = LAUNCHD_LIFECYCLE_SCRIPT.read_text(encoding="utf-8")
    return (
        (
            lifecycle,
            'mac_launchd_stop_job_if_present "gui/501/com.mac.agent" "com.mac.agent" user',
            "bootout gui/501/com.mac.agent",
        ),
        (
            lifecycle,
            'mac_launchd_stop_job_if_present "system/com.mac.hub" "com.mac.hub" system',
            "bootout system/com.mac.hub",
        ),
    )


def _run_launchd_stop_harness(tmp_path, functions, command, mode):
    case_dir = tmp_path / mode
    fake_bin = case_dir / "bin"
    fake_bin.mkdir(parents=True)
    state = case_dir / "state"
    count = case_dir / "count"
    calls = case_dir / "calls"
    state.write_text(mode + "\n", encoding="utf-8")
    count.write_text("0\n", encoding="utf-8")

    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
set -eu
IFS= read -r mode < "$FAKE_LAUNCHCTL_STATE"
case "$1" in
  print)
    IFS= read -r value < "$FAKE_LAUNCHCTL_COUNT"
    value=$((value + 1))
    printf '%s\n' "$value" > "$FAKE_LAUNCHCTL_COUNT"
    case "$mode" in
      absent) echo 'Could not find service synthetic' >&2; exit 113 ;;
      delayed)
        if [ "$value" -lt 3 ]; then
          exit 0
        fi
        echo 'Could not find service synthetic' >&2
        exit 113
        ;;
      persistent|failed) exit 0 ;;
      inspect-error) echo 'synthetic launchctl transport failure' >&2; exit 70 ;;
      post-inspect-error)
        if [ "$value" -eq 1 ]; then exit 0; fi
        echo 'synthetic launchctl transport failure' >&2
        exit 70
        ;;
      failed-then-absent)
        if [ "$value" -eq 1 ]; then exit 0; fi
        echo 'Could not find service synthetic' >&2
        exit 113
        ;;
    esac
    ;;
  bootout)
    printf '%s\n' "$*" >> "$FAKE_LAUNCHCTL_CALLS"
    if [ "$mode" = failed ] || [ "$mode" = failed-then-absent ]; then
      echo 'synthetic bootout refusal' >&2
      exit 9
    fi
    exit 0
    ;;
esac
exit 64
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    sudo = fake_bin / "sudo"
    sudo.write_text(
        """#!/bin/sh
set -eu
if [ "${1:-}" = -n ]; then
  shift
fi
exec "$@"
""",
        encoding="utf-8",
    )
    sudo.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_LAUNCHCTL_STATE": str(state),
        "FAKE_LAUNCHCTL_COUNT": str(count),
        "FAKE_LAUNCHCTL_CALLS": str(calls),
        "MAC_LAUNCHD_TRANSITION_TIMEOUT_SECONDS": "0.15",
        # The contract under test is the 150ms aggregate transition bound,
        # not whether a freshly scheduled shell plus the Python process-group
        # wrapper can start inside 50ms on a loaded xdist runner.  Keep the
        # per-command bound finite but comfortably above scheduler jitter;
        # mac_launchd_wait_unloaded still clamps each attempt to the smaller
        # remaining aggregate deadline.
        "MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS": "1",
        "MAC_LAUNCHD_POLL_INTERVAL_SECONDS": "0.01",
    }
    # mac_run_bounded wraps each poll in a stdlib-only ``python -c`` process-group
    # guard (never imports ``mac``). coverage.py's ``patch = ["subprocess"]`` would
    # trace every such child via COVERAGE_PROCESS_{START,CONFIG} + a site .pth,
    # adding ~5.6x interpreter-start overhead for ZERO src/mac coverage — enough to
    # blow the 150ms aggregate bound (which needs multiple bounded polls) once xdist
    # contention piles on, flaking the "delayed" case. Strip it so the wrapper runs
    # at native speed; the 150ms contract stays exact and coverage is unaffected.
    env.pop("COVERAGE_PROCESS_START", None)
    env.pop("COVERAGE_PROCESS_CONFIG", None)
    return subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            "log() { printf '%s\\n' \"$*\" >&2; }\n" + functions + "\n" + command,
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_node_launchd_mutation_boundary(
    tmp_path,
    *,
    supervisor_prestate,
    control_prestate,
    helper_rc=0,
):
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    stop_function = (
        "stop_existing_services_for_deploy() {"
        + node.split("stop_existing_services_for_deploy() {", 1)[1].split(
            "\n}\n\nload_drain_api_env() {", 1
        )[0]
        + "\n}"
    )
    tmp_path.mkdir(parents=True)
    arguments = tmp_path / "arguments"
    command = f"""set -euo pipefail
SUPERVISOR_KIND=launchd
DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE={supervisor_prestate}
DARWIN_SYSTEM_LAUNCHD_ACTIVE={control_prestate}
DARWIN_GUI_LAUNCHD_ACTIVE=0
DARWIN_SYSTEM_SUPERVISOR_LABEL=com.mac.supervisor
MAC_LAUNCHD_LABEL=com.mac.control-plane
MAC_AGENT_LAUNCHD_LABEL=com.mac.agent
HERMES_LAUNCHD_LABEL=com.mac.hermes-gateway
OPENCLAW_LAUNCHD_LABEL=com.mac.openclaw-gateway
NEMOCLAW_LAUNCHD_LABEL=com.mac.nemoclaw-gateway
MAC_SERVICE_NAME=mac.service
HERMES_SERVICE_NAME=mac-hermes.service
OPENCLAW_SERVICE_NAME=mac-openclaw.service
NEMOCLAW_SERVICE_NAME=mac-nemoclaw.service
MAC_AGENT_SERVICE_NAME=mac-agent.service
MAC_SUPERVISORD_PROG=mac
HERMES_SUPERVISORD_PROG=mac-hermes
OPENCLAW_SUPERVISORD_PROG=mac-openclaw
NEMOCLAW_SUPERVISORD_PROG=mac-nemoclaw
AGENT_SUPERVISORD_PROG=mac-agent
MAC_PORT=8000
LOG_DIR={shlex.quote(str(tmp_path))}
ROLLBACK_SUPERVISOR_HELPER=/tmp/fleet-node-rollback-supervisor.py
PY=rollback_helper
log() {{ printf '%s\n' "$*" >&2; }}
control_plane_enabled() {{
  [ "$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ]
}}
rollback_helper() {{
  printf '%s\n' "$@" > "$ARGUMENTS"
  return {helper_rc}
}}
{stop_function}
stop_existing_services_for_deploy
"""
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            **os.environ,
            "ARGUMENTS": str(arguments),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    return result, arguments


@pytest.mark.process_e2e
def test_launchd_quiescence_waits_for_removal_and_fails_closed(tmp_path):
    for variant, (functions, command, expected_call) in enumerate(
        _launchd_stop_function_variants()
    ):
        variant_dir = tmp_path / str(variant)

        delayed = _run_launchd_stop_harness(variant_dir, functions, command, "delayed")
        assert delayed.returncode == 0, delayed.stderr
        assert (variant_dir / "delayed" / "calls").read_text(encoding="utf-8") == (
            f"{expected_call}\n"
        )

        absent = _run_launchd_stop_harness(variant_dir, functions, command, "absent")
        assert absent.returncode == 0, absent.stderr
        assert not (variant_dir / "absent" / "calls").exists()

        persistent = _run_launchd_stop_harness(variant_dir, functions, command, "persistent")
        assert persistent.returncode != 0
        assert "remained loaded" in persistent.stderr
        assert (variant_dir / "persistent" / "calls").read_text(
            encoding="utf-8"
        ) == f"{expected_call}\n"

        failed = _run_launchd_stop_harness(variant_dir, functions, command, "failed")
        assert failed.returncode != 0
        assert "launchctl bootout failed" in failed.stderr
        assert "synthetic bootout refusal" in failed.stderr
        assert (variant_dir / "failed" / "calls").read_text(
            encoding="utf-8"
        ) == f"{expected_call}\n"

        inspect_error = _run_launchd_stop_harness(variant_dir, functions, command, "inspect-error")
        assert inspect_error.returncode != 0
        assert "could not inspect" in inspect_error.stderr
        assert "synthetic launchctl transport failure" in inspect_error.stderr

        post_inspect_error = _run_launchd_stop_harness(
            variant_dir, functions, command, "post-inspect-error"
        )
        assert post_inspect_error.returncode != 0
        assert "could not inspect" in post_inspect_error.stderr
        assert "synthetic launchctl transport failure" in post_inspect_error.stderr
        assert (variant_dir / "post-inspect-error" / "calls").read_text(
            encoding="utf-8"
        ) == f"{expected_call}\n"

        failed_then_absent = _run_launchd_stop_harness(
            variant_dir, functions, command, "failed-then-absent"
        )
        assert failed_then_absent.returncode == 0, failed_then_absent.stderr
        assert (variant_dir / "failed-then-absent" / "calls").read_text(
            encoding="utf-8"
        ) == f"{expected_call}\n"


def test_launchd_mutation_boundary_delegates_to_exact_bounded_helper_and_fails_closed(
    tmp_path,
):
    expected, arguments = _run_node_launchd_mutation_boundary(
        tmp_path / "expected",
        supervisor_prestate=1,
        control_prestate=1,
    )
    assert expected.returncode == 0, expected.stderr
    expected_arguments = arguments.read_text(encoding="utf-8").splitlines()
    assert expected_arguments[:4] == [
        "/tmp/fleet-node-rollback-supervisor.py",
        "quiesce",
        "--supervisor",
        "launchd",
    ]
    assert expected_arguments[expected_arguments.index("--control-plane-mode") + 1] == "system"
    assert "--launchd-system-supervisor-was-active" in expected_arguments
    assert expected_arguments[expected_arguments.index("--receipt") + 1].endswith(
        "/pre-artifact-supervisor-quiescence.json"
    )

    absent, arguments = _run_node_launchd_mutation_boundary(
        tmp_path / "absent",
        supervisor_prestate=0,
        control_prestate=0,
    )
    assert absent.returncode == 0, absent.stderr
    absent_arguments = arguments.read_text(encoding="utf-8").splitlines()
    assert absent_arguments[absent_arguments.index("--control-plane-mode") + 1] == "inactive"
    assert "--launchd-system-supervisor-was-active" not in absent_arguments

    rejected, arguments = _run_node_launchd_mutation_boundary(
        tmp_path / "helper-failure",
        supervisor_prestate=0,
        control_prestate=0,
        helper_rc=73,
    )
    assert rejected.returncode == 73
    assert arguments.exists()


def test_outer_agent_restart_uses_the_shared_bounded_launchd_contract():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    service = deploy.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]
    assert 'lifecycle="$HOME/.mac/logs/launchd-lifecycle-${MAC_DEPLOY_SERVICE_TS:?}.sh"' in service
    assert '. "$lifecycle"' in service
    assert 'mac_launchd_stop_job_if_present "$domain/$label" "$label" user' in service
    assert "deadline=$(( SECONDS + 45 ))" not in service
    assert "launchctl " not in service


def test_outer_linux_worker_restart_uses_exact_bounded_manager_commands():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    service = deploy.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]
    supervisor = service.split('case "$supervisor" in', 1)[1].split("  systemd)", 1)[0]
    systemd = service.split("  systemd)", 1)[1].split("  launchd)", 1)[0]

    assert service.index('. "$lifecycle"') < service.index('case "$supervisor" in')
    assert service.count("mac_run_bounded") >= 4
    assert "MAC_SUPERVISOR_COMMAND_TIMEOUT_SECONDS" in service
    assert "MAC_SYSTEMD_COMMAND_TIMEOUT_SECONDS" in service
    assert 'sudo -n supervisorctl "$@"' in service
    assert 'sudo -n systemctl "$@"' in service

    assert 'program="${MAC_DEPLOY_FLEET_NAME:?}-agent"' in supervisor
    assert 'run_fleet_supervisorctl stop "$program"' in supervisor
    assert 'run_fleet_supervisorctl start "$program"' in supervisor
    assert 'run_fleet_supervisorctl status "$program"' in service
    assert '"$observed_program" != "$program"' in service
    assert "no such process" not in supervisor
    assert "|| true" not in supervisor

    assert 'unit="${MAC_DEPLOY_FLEET_NAME:?}-agent.service"' in systemd
    assert 'run_fleet_systemctl stop "$unit"' in systemd
    assert 'run_fleet_systemctl start "$unit"' in systemd
    for field in ("LoadState", "ActiveState", "SubState", "MainPID"):
        assert field in systemd
    assert '"$main_pid" != 0' in systemd
    assert "*[!0-9]*" in systemd
    assert "|| true" not in systemd
    assert "sudo systemctl" not in systemd


def test_outer_coordinator_never_prompts_for_launchd_definition_privilege():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert 'sudo test -e "$plist"' not in deploy
    assert 'sudo grep -Fqx "$marker_line" "$plist"' not in deploy
    assert 'sudo -n test -e "$plist"' in deploy
    assert 'sudo -n grep -Fqx "$marker_line" "$plist"' in deploy


def _run_outer_linux_worker_manager(tmp_path, supervisor, *, stop_fails=False):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    service = deploy.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]
    remote = service.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
    manager = 'lifecycle="$HOME/.mac/logs/' + remote.split('lifecycle="$HOME/.mac/logs/', 1)[1]

    home = tmp_path / supervisor / "home"
    fake_bin = tmp_path / supervisor / "bin"
    logs = home / ".mac" / "logs"
    fake_bin.mkdir(parents=True)
    logs.mkdir(parents=True)
    (logs / "launchd-lifecycle-fixture.sh").write_text(
        LAUNCHD_LIFECYCLE_SCRIPT.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    state = tmp_path / supervisor / "state"
    calls = tmp_path / supervisor / "calls"
    state.write_text("active\n", encoding="utf-8")

    sudo = fake_bin / "sudo"
    sudo.write_text(
        '#!/bin/sh\nset -eu\nif [ "${1:-}" = -n ]; then shift; fi\nexec "$@"\n',
        encoding="utf-8",
    )
    sudo.chmod(0o755)
    if supervisor == "supervisord":
        manager_bin = fake_bin / "supervisorctl"
        manager_bin.write_text(
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FAKE_MANAGER_CALLS"
[ "${2:-}" = mac-agent ] || exit 61
case "$1" in
  status)
    state=$(cat "$FAKE_MANAGER_STATE")
    if [ "$state" = active ]; then
      printf 'mac-agent RUNNING pid 4321, uptime 0:00:10\n'
    else
      printf 'mac-agent STOPPED Not started\n'
    fi
    ;;
  stop)
    [ "${FAKE_STOP_FAIL:-0}" != 1 ] || exit 9
    printf 'inactive\n' > "$FAKE_MANAGER_STATE"
    ;;
  start) printf 'active\n' > "$FAKE_MANAGER_STATE" ;;
  *) exit 62 ;;
esac
""",
            encoding="utf-8",
        )
    else:
        manager_bin = fake_bin / "systemctl"
        manager_bin.write_text(
            """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FAKE_MANAGER_CALLS"
[ "${2:-}" = mac-agent.service ] || exit 61
case "$1" in
  show)
    property=${3#--property=}
    state=$(cat "$FAKE_MANAGER_STATE")
    case "$property" in
      LoadState) printf 'loaded\n' ;;
      ActiveState) [ "$state" = active ] && printf 'active\n' || printf 'inactive\n' ;;
      SubState) [ "$state" = active ] && printf 'running\n' || printf 'dead\n' ;;
      MainPID) [ "$state" = active ] && printf '4321\n' || printf '0\n' ;;
      *) exit 63 ;;
    esac
    ;;
  stop)
    [ "${FAKE_STOP_FAIL:-0}" != 1 ] || exit 9
    printf 'inactive\n' > "$FAKE_MANAGER_STATE"
    ;;
  start) printf 'active\n' > "$FAKE_MANAGER_STATE" ;;
  *) exit 62 ;;
esac
""",
            encoding="utf-8",
        )
    manager_bin.chmod(0o755)

    return subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail\naction=restart\nsupervisor="$1"\n' + manager,
            "outer-manager",
            supervisor,
        ],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "MAC_DEPLOY_SERVICE_TS": "fixture",
            "MAC_DEPLOY_FLEET_NAME": "mac",
            # The bound belongs to the production wrapper, not host scheduler
            # latency.  One second flakes when xdist saturates Darwin while
            # Python starts the process-group supervisor around this fake.
            "MAC_SUPERVISOR_COMMAND_TIMEOUT_SECONDS": "10",
            "MAC_SYSTEMD_COMMAND_TIMEOUT_SECONDS": "10",
            "FAKE_MANAGER_STATE": str(state),
            "FAKE_MANAGER_CALLS": str(calls),
            "FAKE_STOP_FAIL": "1" if stop_fails else "0",
        },
        check=False,
        capture_output=True,
        text=True,
    ), calls


def test_outer_linux_worker_manager_runtime_is_exact_and_fail_closed(tmp_path):
    for supervisor in ("supervisord", "systemd"):
        succeeded, calls_path = _run_outer_linux_worker_manager(tmp_path / "success", supervisor)
        assert succeeded.returncode == 0, succeeded.stderr
        calls = calls_path.read_text(encoding="utf-8").splitlines()
        assert any(call.startswith("stop mac-agent") for call in calls)
        assert any(call.startswith("start mac-agent") for call in calls)
        assert all("mac-agent" in call for call in calls)

        failed, failed_calls_path = _run_outer_linux_worker_manager(
            tmp_path / "failure", supervisor, stop_fails=True
        )
        assert failed.returncode != 0
        failed_calls = failed_calls_path.read_text(encoding="utf-8").splitlines()
        assert any(call.startswith("stop mac-agent") for call in failed_calls)
        assert not any(call.startswith("start mac-agent") for call in failed_calls)


def test_failed_release_compensation_is_required_bounded_and_never_success():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    restore = deploy.split("restore_remote_agent_release_barrier() {", 1)[1].split(
        "hub_dispatch_hold_cas_available() {", 1
    )[0]
    restore_only, fail_only = restore.split("fail_release_with_compensation() {", 1)
    fail_function = "fail_release_with_compensation() {" + fail_only
    service = deploy.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]
    rehold = deploy.split('elif phase == "rehold":', 1)[1].split(
        'else:\n    raise RuntimeError("unsupported hub restart gate phase', 1
    )[0]

    assert "hub_agent_restart_gate rehold" in restore_only
    assert restore_only.index("hub_agent_restart_gate rehold") < restore_only.index(
        "ssh -o BatchMode=yes"
    )
    assert "ServerAliveInterval=10" in restore_only
    assert "ServerAliveCountMax=3" in restore_only
    assert "rehold_rc" in restore_only
    assert "barrier_rc" in restore_only
    assert "|| true" not in restore_only
    assert "REQUIRED compensation failed" in fail_only
    assert "cohort release aborted" in fail_only
    assert service.count("fail_release_with_compensation") == 3
    assert "could not restore the deployment-owned dispatch hold exactly" in rehold
    assert "active work attached while release compensation ran" in rehold

    for compensation_rc, expected in (
        (0, "compensation result: restored"),
        (9, "REQUIRED compensation failed (rc=9)"),
    ):
        harness = (
            "set -euo pipefail\n"
            "restore_remote_agent_release_barrier() { "
            "printf 'restored\\n'; return \"${COMPENSATION_RC:?}\"; }\n"
            + fail_function
            + "\nif fail_release_with_compensation primary a b c d e f; then "
            "exit 90; fi\n"
        )
        result = subprocess.run(
            ["bash", "-c", harness],
            env={**os.environ, "COMPENSATION_RC": str(compensation_rc)},
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "primary" in result.stderr
        assert expected in result.stderr
        assert "cohort release aborted" in result.stderr


def test_deploy_drains_worker_before_stopping_services():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "drain_mac_agent_before_deploy()" in node
    assert "wait_for_agent_active_leases" in node
    assert "MAC_DEPLOY_DRAIN_MODE" in node
    assert "MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS" in node
    assert 'add_remote_env MAC_DEPLOY_DRAIN_MODE "${MAC_DEPLOY_DRAIN_MODE:-}"' in deploy
    assert "add_remote_env MAC_DEPLOY_DEFER_CLEAR_DRAIN 1" in deploy
    assert "add_remote_env MAC_DEPLOY_DEFER_AGENT_RESTART 1" in deploy
    assert 'timeout = float(os.environ.get("MAC_DEPLOY_API_TIMEOUT_SECONDS") or "30")' in node
    assert 'health_status":"degraded' in node

    main = node.split('write_deploy_manifest "pre" "$MANIFEST_PRE"', 1)[1]
    drain_pos = main.index("drain_mac_agent_before_deploy\n")
    stop_call_pos = main.index("stop_existing_services_for_deploy\n")
    assert drain_pos < stop_call_pos


def test_deploy_freezes_every_fleet_route_before_any_lookup_or_mutation():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    new_hub_update = deploy.index('REQUESTED_AGENTS=("$NEW_HUB_NAME")')
    snapshot_start = deploy.index("# Freeze the complete routing input", new_hub_update)
    query_start = deploy.index("fleet_config_query() {", snapshot_start)
    snapshot = deploy[snapshot_start:query_start]

    assert new_hub_update < snapshot_start < query_start
    assert 'FLEET_REGISTRY_SOURCE="$FLEET_REGISTRY_CONFIG"' in snapshot
    assert 'FLEET_CONFIG_SOURCE="$FLEET_CONFIG"' in snapshot
    assert 'cp -f "$FLEET_REGISTRY_SOURCE" "$TMPDIR_LOCAL/fleets-source.yaml"' in snapshot
    assert 'cp -f "$FLEET_CONFIG_SOURCE" "$TMPDIR_LOCAL/fleet-defaults-source.yaml"' in snapshot
    chmod = snapshot.index(
        'chmod 0600 "$TMPDIR_LOCAL/fleets-source.yaml" "$TMPDIR_LOCAL/fleet-defaults-source.yaml"'
    )
    registry_rebind = snapshot.index('FLEET_REGISTRY_CONFIG="$TMPDIR_LOCAL/fleets-source.yaml"')
    defaults_rebind = snapshot.index('FLEET_CONFIG="$TMPDIR_LOCAL/fleet-defaults-source.yaml"')
    assert chmod < registry_rebind < defaults_rebind
    assert (
        "readonly FLEET_REGISTRY_CONFIG FLEET_CONFIG FLEET_REGISTRY_SOURCE FLEET_CONFIG_SOURCE"
    ) in snapshot

    # All later selectors and SSH/SCP route builders resolve through the
    # rebound owner-only files, never by reopening the mutable operator paths.
    after_snapshot = deploy[query_start:]
    assert '"$mode" "$FLEET_CONFIG" "$FLEET_REGISTRY_CONFIG"' in after_snapshot
    assert "base_path = Path(sys.argv[2])" in after_snapshot
    assert "registry_path = Path(sys.argv[3]).expanduser()" in after_snapshot
    assert 'selected_hosts "${REQUESTED_AGENTS[@]}"' in after_snapshot
    reconcile = after_snapshot.split("reconcile_remote_deploy() {", 1)[1].split(
        "hub_dispatch_hold_cas_available() {", 1
    )[0]
    assert '_display_target="$2"' in reconcile
    assert 'ssh_target_args "$agent"' in reconcile
    assert "immutable invocation snapshot" in reconcile


def test_deploy_refuses_controller_bytes_outside_the_frozen_source_commit():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    freeze = deploy.split("assert_frozen_deployment_source() {", 1)[1].split(
        "\n}\n\nmake_archive() {", 1
    )[0]
    assert 'git -C "$ROOT" diff --quiet --no-ext-diff "$GIT_REV"' in freeze
    assert "ls-files --others --exclude-standard" in freeze
    assert 'rev-parse "$GIT_REV:$asset"' in freeze
    assert 'hash-object -- "$ROOT/$asset"' in freeze
    for asset in (
        "deploy/deploy-mac-fleet.sh",
        "deploy/fleet-node-install.sh",
        "deploy/fleet-node-phase1-quiesce.sh",
        "deploy/fleet-node-rollback-supervisor.py",
        "deploy/lib/launchd-lifecycle.sh",
        "scripts/deploy-hold-adoptions.py",
    ):
        assert asset in freeze

    main = deploy.split("main() {", 1)[1].rsplit("\n}\n\nmain", 1)[0]
    assert main.index("assert_frozen_deployment_source") < main.index("make_archive")
    assert main.index("assert_frozen_deployment_source") < main.index("start_ssh_control_master")


def test_typed_cohort_orders_receipts_before_mutation_and_commit_before_finalize():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    main = deploy.split("main() {", 1)[1].rsplit("\n}\n\nmain", 1)[0]
    typed = deploy.split("run_typed_cohort() {", 1)[1].split("\n}\n\nmain()", 1)[0]

    assert main.index("bind_live_cohort_routes") < main.index("run_typed_cohort")
    order = [
        "cohort_journal_mutate phase1-armed",
        'run_bounded_node_phase "$selected_specs_file" prerequisites',
        "build_and_open_hub_epoch",
        "cohort_journal_mutate quiesce-start",
        "cohort_journal_mutate phase2-armed",
        "cohort_journal_mutate phase2-start",
        "cohort_journal_mutate prepared",
        "prove_and_commit_hub_epoch",
        "cohort_journal_mutate finalize-start",
        "cohort_journal_mutate finalized-node",
    ]
    positions = [typed.index(item) for item in order]
    assert positions == sorted(positions)
    assert positions[-1] < typed.rindex('cohort_journal_mutate finalize "$COHORT')
    assert "phase1-proved" not in main
    assert "prepare-start" not in main


def test_bounded_node_workers_cannot_consume_the_controller_spec_stream(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    bounded = (
        "run_bounded_node_phase() {"
        + deploy.split("run_bounded_node_phase() {", 1)[1].split(
            "\n}\n\npreflight_probe_helper_source", 1
        )[0]
        + "\n}"
    )
    specs = tmp_path / "selected-specs"
    specs.write_text(
        "".join(f"worker{number}|fixture\n" for number in range(1, 6)),
        encoding="utf-8",
    )
    results = tmp_path / "results"
    results.mkdir()
    snippet = f"""set -euo pipefail
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
NODE_PARALLELISM=1
BOUNDED_NODE_PHASE_AGGREGATE_FAILURES=0
stable_worker_agent_id() {{ printf '%s\n' "$1"; }}
stdin_draining_worker() {{
  local spec="$1" stolen=""
  IFS= read -r stolen || true
  printf '%s\n' "${{spec%%|*}}" > {shlex.quote(str(results))}/"${{spec%%|*}}"
}}
{bounded}
run_bounded_node_phase {shlex.quote(str(specs))} stdin-proof stdin_draining_worker
"""
    result = subprocess.run(
        ["bash", "-c", snippet],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in results.iterdir()) == [
        "worker1",
        "worker2",
        "worker3",
        "worker4",
        "worker5",
    ]
    for number in range(1, 6):
        assert f"worker{number}: stdin-proof passed" in result.stdout


def test_bounded_node_phase_explains_a_child_that_dies_without_publishing_status(tmp_path):
    # Regression for a 2026-09-01 fleet deploy: bullwinkle's phase1-prepare
    # reported "status 125" with an otherwise-empty log, which reads as a
    # mysterious phase failure. That status is synthesized by the controller
    # when it loses track of a child (kill -0/`jobs -pr` race, or the child
    # was killed externally) -- it is not necessarily a command failure
    # inside the phase. The controller must say so explicitly.
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    bounded = (
        "run_bounded_node_phase() {"
        + deploy.split("run_bounded_node_phase() {", 1)[1].split(
            "\n}\n\npreflight_probe_helper_source", 1
        )[0]
        + "\n}"
    )
    specs = tmp_path / "selected-specs"
    specs.write_text("victim|fixture\n", encoding="utf-8")
    snippet = f"""set -euo pipefail
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
NODE_PARALLELISM=1
BOUNDED_NODE_PHASE_AGGREGATE_FAILURES=0
stable_worker_agent_id() {{ printf '%s\n' "$1"; }}
persist_bounded_phase_failure_evidence() {{ return 1; }}
self_destructing_worker() {{
  # Simulate a child that dies before it can write its own status file.
  # $$ is inherited from the parent shell inside a subshell -- BASHPID is
  # the subshell's own pid, the one that must die here.
  kill -9 $BASHPID
}}
{bounded}
run_bounded_node_phase {shlex.quote(str(specs))} die-before-status self_destructing_worker
"""
    result = subprocess.run(
        ["bash", "-c", snippet],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert (
        "victim: die-before-status child exited without publishing its status "
        "file (status=125 synthesized)" in result.stderr
    )
    assert "controller/job-control condition" in result.stderr
    assert "ERROR: victim: die-before-status failed with status 125" in result.stderr


def test_legacy_hub_bootstrap_preflights_onboarding_before_phase1():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    legacy = deploy.split("legacy_hub_bootstrap() {", 1)[1].split(
        "\n}\n\nhub_epoch_client_read", 1
    )[0]
    preflight = deploy.split("preflight_legacy_hub_prerequisites() {", 1)[1].split(
        "\n}\n\nlegacy_hub_bootstrap", 1
    )[0]

    assert legacy.index("preflight_legacy_hub_prerequisites") < legacy.index(
        "prepare_remote_phase1_restore_contract"
    )
    assert "mac.fleet_node_identity.v1" in preflight
    assert '("python", "github_cli")' in preflight
    assert "read-only legacy onboarding prerequisite receipt passed" in preflight


def test_legacy_hub_bootstrap_precedes_incompatible_journal_recovery():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    main = deploy.split("main() {", 1)[1].rsplit("\n}\n\nmain", 1)[0]
    recovery_guard = main.split("recover_incomplete_cohort_transaction_before_deploy", 1)[0].rsplit(
        "if ", 1
    )[1]

    assert '"$PREFLIGHT_ONLY" != 1' in recovery_guard
    assert '"$LEGACY_HUB_BOOTSTRAP" != 1' in recovery_guard
    assert main.index("recover_incomplete_cohort_transaction_before_deploy") < main.index(
        'legacy_hub_bootstrap "$selected_specs_file"'
    )


def test_legacy_hub_bootstrap_restores_phase1_after_deploy_failure(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    legacy = (
        "legacy_hub_bootstrap() {"
        + deploy.split("legacy_hub_bootstrap() {", 1)[1].split("\n}\n\nhub_epoch_client_read", 1)[0]
        + "\n}"
    )
    selected = tmp_path / "selected"
    fields = [""] * 24
    fields[0] = "rocky"
    fields[2] = "darwin"
    fields[14] = "launchd"
    fields[23] = "mac"
    selected.write_text("|".join(fields) + "\n", encoding="utf-8")
    events = tmp_path / "events"
    snippet = f"""set -u
REQUIRE_RELEASE_ALL_SELECTED=0
HOLD_ADOPTIONS_FILE=
SUCCESSOR_HOLD_REASON=
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
deployment_id_for_agent() {{ printf '%s\n' exact-generation; }}
classify_reviewed_openshell_cli_prerequisites() {{ :; }}
preflight_legacy_hub_prerequisites() {{ :; }}
prepare_remote_phase1_restore_contract() {{ printf '%s\n' prepare-contract >> {shlex.quote(str(events))}; }}
prepare_remote_mac_agent_deployment() {{ printf '%s\n' prepare-agent >> {shlex.quote(str(events))}; }}
read_hub_token() {{ printf '%s\n' token; }}
read_hub_tunnel_pubkey() {{ :; }}
ensure_local_github_review_key() {{ printf '%s\n' review-key; }}
deploy_host() {{ printf '%s\n' deploy >> {shlex.quote(str(events))}; return 9; }}
recover_legacy_hub_bootstrap_failure() {{ printf '%s\n' "recover:$1:$2:$3:$4:$5" >> {shlex.quote(str(events))}; }}
{legacy}
set +e
legacy_hub_bootstrap {shlex.quote(str(selected))} rocky 1
result=$?
set -e
printf '%s\n' "$result"
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "1"
    assert events.read_text(encoding="utf-8").splitlines() == [
        "prepare-contract",
        "prepare-agent",
        "deploy",
        "recover:rocky:exact-generation:mac:darwin:launchd",
    ]


def _first_hub_identity_validator():
    # The from-scratch gate is an embedded Python program. Run the real one
    # against crafted identity receipts rather than asserting on its source, so
    # a loosened comparison fails the test instead of passing it.
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    body = deploy.split("preflight_first_hub_prerequisites() {", 1)[1]
    return body.split('"$PYTHON_BIN" - "$receipt" "$agent" "$GIT_REV" <<\'PY\'\n', 1)[1].split(
        "\nPY\n", 1
    )[0]


def _run_first_hub_identity_validator(tmp_path, identity, name="identity.json"):
    program = tmp_path / "validate-first-hub.py"
    program.write_text(_first_hub_identity_validator(), encoding="utf-8")
    receipt = tmp_path / name
    receipt.write_text(json.dumps(identity), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(program), str(receipt), "rocky", "a" * 40],
        text=True,
        capture_output=True,
        check=False,
    )


def _from_scratch_identity():
    return {
        "schema": "mac.fleet_node_identity.v1",
        "status": "identified",
        "agent": "rocky",
        "requested_revision": "a" * 40,
        "install_kind": "from_scratch",
        "rollback_capable": False,
        "rollback_ineligible_reason": (
            "node has never been deployed; there is no prior generation to restore"
        ),
        "current_generation": None,
        "current_revision": None,
        "artifacts": {
            "source": {"path": "/home/x/.mac/src/mac", "regular_directory": False},
            "venv": {"path": "/home/x/.mac/venv", "regular_directory": False},
        },
        "prerequisites": {
            "python": "/usr/bin/python3",
            "github_cli": "/usr/bin/gh",
        },
    }


def test_first_hub_bootstrap_accepts_only_a_never_deployed_node(tmp_path):
    accepted = _run_first_hub_identity_validator(tmp_path, _from_scratch_identity())

    assert accepted.returncode == 0, accepted.stderr


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"install_kind": "upgrade"}, "has never been deployed"),
        ({"install_kind": None}, "has never been deployed"),
        ({"current_generation": "generation-rocky-001"}, "prior generation"),
        ({"current_revision": "b" * 40}, "prior generation"),
        ({"rollback_ineligible_reason": ""}, "why it cannot roll back"),
        ({"requested_revision": "b" * 40}, "identity is malformed"),
        ({"agent": "hazel"}, "identity is malformed"),
    ],
)
def test_first_hub_bootstrap_refuses_nodes_that_are_not_from_scratch(tmp_path, mutation, expected):
    identity = {**_from_scratch_identity(), **mutation}

    rejected = _run_first_hub_identity_validator(tmp_path, identity)

    assert rejected.returncode != 0
    assert expected in rejected.stderr


def test_first_hub_bootstrap_refuses_a_node_that_claims_rollback_capability(tmp_path):
    # The parent bug report suggested making a never-deployed node report
    # rollback_capable=true. This asserts the opposite contract: the from-scratch
    # path requires the honest false and rejects the fabricated true, so nothing
    # downstream can read a rollback promise this node cannot keep.
    identity = {**_from_scratch_identity(), "rollback_capable": True}

    rejected = _run_first_hub_identity_validator(tmp_path, identity)

    assert rejected.returncode != 0
    assert "must not advertise rollback capability" in rejected.stderr


@pytest.mark.parametrize("artifact", ["source", "venv"])
def test_first_hub_bootstrap_refuses_a_node_with_an_existing_artifact(tmp_path, artifact):
    identity = _from_scratch_identity()
    identity["artifacts"] = {
        **identity["artifacts"],
        artifact: {"path": "/home/x/.mac", "regular_directory": True},
    }

    rejected = _run_first_hub_identity_validator(tmp_path, identity)

    assert rejected.returncode != 0
    assert artifact in rejected.stderr


@pytest.mark.parametrize("prerequisite", ["python", "github_cli"])
def test_first_hub_bootstrap_requires_onboarded_prerequisites(tmp_path, prerequisite):
    identity = _from_scratch_identity()
    identity["prerequisites"] = {**identity["prerequisites"], prerequisite: None}

    rejected = _run_first_hub_identity_validator(tmp_path, identity)

    assert rejected.returncode != 0
    assert "lacks required onboarded prerequisite: %s" % prerequisite in rejected.stderr


def _first_hub_bootstrap_source():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    return (
        "first_hub_bootstrap() {"
        + deploy.split("first_hub_bootstrap() {", 1)[1].split(
            "\n}\n\npreflight_legacy_hub_prerequisites", 1
        )[0]
        + "\n}"
    )


def test_first_hub_bootstrap_takes_no_hub_dependent_arm():
    # A first hub has no live hub API, so the dispatch-hold gate and the phase-1
    # restore contract cannot be satisfied and must not be attempted. It must
    # also never reach the hub-route self-probe, whose target is the endpoint
    # this very invocation is installing.
    first = _first_hub_bootstrap_source()

    assert "prepare_remote_mac_agent_deployment" not in first
    assert "prepare_remote_phase1_restore_contract" not in first
    assert "quiesce_remote_agent_for_cohort" not in first
    assert "classify_network_prerequisites" not in first
    assert "initialize_cohort_transaction" not in first
    assert first.index("preflight_first_hub_prerequisites") < first.index(
        "acquire_remote_deployment_lock"
    )
    assert first.index("acquire_remote_deployment_lock") < first.index("deploy_host")


def test_first_hub_bootstrap_recovery_is_teardown_not_rollback():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    recover = deploy.split("recover_first_hub_bootstrap_failure() {", 1)[1].split("\n}\n", 1)[0]

    # Nothing existed before this install, so there is no phase-1 topology to
    # restore and no phase-2 generation to roll back to. Recovery removes what
    # this invocation uploaded and says so.
    assert "restore_remote_phase1_generation" not in recover
    assert "rollback_remote_phase2_generation" not in recover
    assert "probe_remote_cohort_recovery_action" not in recover
    assert "cleanup_remote_legacy_bootstrap_files" in recover
    assert "release_remote_deployment_lock" in recover
    assert "node remains uninstalled" in recover


def test_first_hub_bootstrap_tears_down_after_install_failure(tmp_path):
    first = _first_hub_bootstrap_source()
    selected = tmp_path / "selected"
    fields = [""] * 24
    fields[0] = "rocky"
    fields[2] = "darwin"
    fields[14] = "launchd"
    fields[23] = "mac"
    selected.write_text("|".join(fields) + "\n", encoding="utf-8")
    events = tmp_path / "events"
    snippet = f"""set -u
REQUIRE_RELEASE_ALL_SELECTED=0
HOLD_ADOPTIONS_FILE=
SUCCESSOR_HOLD_REASON=
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
deployment_id_for_agent() {{ printf '%s\n' exact-generation; }}
classify_reviewed_openshell_cli_prerequisites() {{ :; }}
classify_openshell_storage_prerequisites() {{ :; }}
preflight_first_hub_prerequisites() {{ printf '%s\n' preflight >> {shlex.quote(str(events))}; }}
acquire_remote_deployment_lock() {{ printf '%s\n' "lock:$1:$2" >> {shlex.quote(str(events))}; }}
read_hub_token() {{ printf '%s\n' token; }}
read_hub_tunnel_pubkey() {{ :; }}
ensure_local_github_review_key() {{ printf '%s\n' review-key; }}
deploy_host() {{ printf '%s\n' deploy >> {shlex.quote(str(events))}; return 9; }}
hub_epoch_client_read() {{ printf '%s\n' authority >> {shlex.quote(str(events))}; }}
finalize_remote_deployment_release() {{ printf '%s\n' finalize >> {shlex.quote(str(events))}; }}
recover_first_hub_bootstrap_failure() {{ printf '%s\n' "recover:$1:$2" >> {shlex.quote(str(events))}; }}
{first}
set +e
first_hub_bootstrap {shlex.quote(str(selected))} rocky 1
result=$?
set -e
printf '%s\n' "$result"
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "1"
    # The lock is taken before the install and the teardown runs after it fails.
    # Neither the hub authority read nor the release is reached.
    assert events.read_text(encoding="utf-8").splitlines() == [
        "preflight",
        "lock:rocky:exact-generation",
        "deploy",
        "recover:rocky:exact-generation",
    ]


def test_first_hub_bootstrap_rejects_multi_node_and_non_hub_selection(tmp_path):
    first = _first_hub_bootstrap_source()
    selected = tmp_path / "selected"
    fields = [""] * 24
    fields[0] = "worker1"
    fields[2] = "linux"
    selected.write_text("|".join(fields) + "\n", encoding="utf-8")
    snippet = f"""set -u
REQUIRE_RELEASE_ALL_SELECTED=0
HOLD_ADOPTIONS_FILE=
SUCCESSOR_HOLD_REASON=
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
{first}
set +e
first_hub_bootstrap {shlex.quote(str(selected))} rocky 2
printf 'count:%s\n' "$?"
first_hub_bootstrap {shlex.quote(str(selected))} rocky 1
printf 'agent:%s\n' "$?"
set -e
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)

    assert "count:1" in result.stdout
    assert "agent:1" in result.stdout
    assert "requires exactly one selected node" in result.stderr
    assert "may deploy only the configured hub" in result.stderr


def test_first_hub_bootstrap_is_an_explicit_documented_mode():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    usage = deploy.split("usage() {", 1)[1].split("\nUSAGE\n", 1)[0]
    main = deploy.split("main() {", 1)[1].rsplit("\n}\n\nmain", 1)[0]

    assert "--first-hub-bootstrap" in usage
    assert "install_kind=from_scratch" in usage
    # The legacy flag's own text claimed the from-scratch job it cannot do.
    assert "cannot install a hub that has never been deployed" in usage
    # The two single-node paths are separate modes and cannot be combined.
    assert "--first-hub-bootstrap and --legacy-hub-bootstrap are mutually exclusive" in deploy
    assert main.index('first_hub_bootstrap "$selected_specs_file"') < main.index(
        '\n  classify_network_prerequisites "$selected_specs_file"'
    )
    for guard in (
        "recover_incomplete_cohort_transaction_before_deploy",
        "initialize_cohort_transaction",
    ):
        preceding = main.split(guard, 1)[0]
        assert '[ "$FIRST_HUB_BOOTSTRAP" != 1 ]' in preceding.rsplit("if ", 1)[1]


def test_first_hub_bootstrap_does_not_require_phase1_quiescence():
    # A from-scratch first-hub install never writes the phase1-cohort-
    # quiescence receipt (it has no dispatch hold, no worker to drain, no
    # phase-1 topology to restore), so it must not ask fleet-node-install.sh
    # to require one -- doing so crashes the remote install with a
    # FileNotFoundError for a receipt that was never going to exist.
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    site = deploy.split("add_remote_env MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE", 1)
    assert len(site) == 2, "expected exactly one MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE site"
    preceding = site[0][-400:] + "add_remote_env MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE"
    following = site[1][:120]
    block = preceding + following
    assert 'if [ "$FIRST_HUB_BOOTSTRAP" = 1 ]; then' in block
    assert "MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE 0" in block
    assert "MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE 1" in block


def test_daemon_and_openclaw_timeouts_are_forwarded_only_when_set():
    # Empty remote assignments would fail-close bounded_number() / integer
    # parsers on the node. The controller must omit the assignment unless the
    # operator actually set a value.
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert 'if [ -n "${MAC_DEPLOY_DAEMON_QUIESCENCE_TIMEOUT_SECONDS:-}" ]; then' in deploy
    assert "add_remote_env MAC_DEPLOY_DAEMON_QUIESCENCE_TIMEOUT_SECONDS" in deploy
    assert 'if [ -n "${MAC_OPENCLAW_VERIFY_STARTUP_TIMEOUT:-}" ]; then' in deploy
    assert "add_remote_env MAC_OPENCLAW_VERIFY_STARTUP_TIMEOUT" in deploy
    for name in (
        "MAC_DEPLOY_DAEMON_COMMAND_TIMEOUT_SECONDS",
        "MAC_DEPLOY_DAEMON_PRESERVATION_TIMEOUT_SECONDS",
        "MAC_DEPLOY_DAEMON_LEASE_DRAIN_TIMEOUT_SECONDS",
        "MAC_DEPLOY_DAEMON_QUIESCENCE_POLL_SECONDS",
        "MAC_DEPLOY_DAEMON_TOTAL_TIMEOUT_SECONDS",
        "MAC_OPENCLAW_SUBPROCESS_TIMEOUT_SECONDS",
        "MAC_OPENCLAW_SANDBOX_DELETE_TIMEOUT_SECONDS",
        # launchd-lifecycle.sh's own bounded-command timeouts (macOS phase-2
        # rollback snapshotting, e.g. "could not durably snapshot MAC_HOME/
        # openclaw for rollback" -- live-confirmed on 2026-09-03 to time out
        # at the 10s/45s node defaults on a fleet whose accumulated OpenClaw
        # state made the real copy+fsync take longer).
        "MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS",
        "MAC_LAUNCHD_TRANSITION_TIMEOUT_SECONDS",
        "MAC_LAUNCHD_ARTIFACT_TIMEOUT_SECONDS",
        "MAC_LAUNCHD_POLL_INTERVAL_SECONDS",
    ):
        assert 'if [ -n "${!_timeout_var:-}" ]; then' in deploy
        assert name in deploy


def _arm_phase2_rollback_source():
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    return (
        "arm_phase2_rollback() {"
        + node.split("arm_phase2_rollback() {", 1)[1].split("\nbackup_existing_artifacts() {", 1)[0]
    )


def _run_arm_phase2_rollback(tmp_path, *, src_exists, venv_exists, require_phase1_quiescence):
    mac_home = tmp_path / "mac-home"
    mac_home.mkdir()
    src_dir = mac_home / "src" / "mac"
    venv = mac_home / "venv"
    hermes_dir = mac_home / "hermes-agent"
    if src_exists:
        src_dir.mkdir(parents=True)
    if venv_exists:
        venv.mkdir(parents=True)
    script = f"""
set -euo pipefail
die() {{ echo "DIE: $*" >&2; exit 1; }}
log() {{ :; }}
truthy() {{
  case "${{1:-}}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}}
capture_mutable_runtime_state_for_rollback() {{ :; }}
capture_auxiliary_rollback_artifacts() {{ :; }}
write_rollback_script() {{ :; }}
write_phase2_rollback_intent() {{ :; }}

AGENT=testnode
DEPLOY_TS=20260101T000000Z
MAC_HOME={shlex.quote(str(mac_home))}
SRC_DIR={shlex.quote(str(src_dir))}
VENV={shlex.quote(str(venv))}
HERMES_DIR={shlex.quote(str(hermes_dir))}
ROLLBACK_INTENT={shlex.quote(str(mac_home / "rollback-intent.json"))}
MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE={shlex.quote(require_phase1_quiescence)}

{_arm_phase2_rollback_source()}

arm_phase2_rollback
echo "ARMED:$DEPLOY_ROLLBACK_ARMED"
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)


def test_arm_phase2_rollback_accepts_a_complete_prior_generation(tmp_path):
    result = _run_arm_phase2_rollback(
        tmp_path, src_exists=True, venv_exists=True, require_phase1_quiescence="1"
    )
    assert result.returncode == 0, result.stderr
    assert "ARMED:1" in result.stdout


def test_arm_phase2_rollback_accepts_a_from_scratch_first_hub_install(tmp_path):
    # The exact scenario that crashed --first-hub-bootstrap: neither SRC_DIR
    # nor VENV exists yet, and the deploy correctly declared it doesn't
    # require phase-1 quiescence (there is no prior generation to restore).
    result = _run_arm_phase2_rollback(
        tmp_path, src_exists=False, venv_exists=False, require_phase1_quiescence="0"
    )
    assert result.returncode == 0, result.stderr
    assert "ARMED:1" in result.stdout


def test_arm_phase2_rollback_refuses_a_partial_prior_generation():
    # SRC_DIR exists but VENV doesn't, on a deploy that DID require phase-1
    # quiescence -- a broken/partial upgrade state, not a legitimate
    # from-scratch install. Must still be refused, not silently accepted.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = _run_arm_phase2_rollback(
            Path(tmp), src_exists=True, venv_exists=False, require_phase1_quiescence="1"
        )
    assert result.returncode != 0
    assert "phase-2 apply requires a complete rollback-capable prior generation" in result.stderr


def test_arm_phase2_rollback_refuses_a_from_scratch_state_when_quiescence_was_required():
    # Neither artifact exists, but the deploy DID require phase-1 quiescence
    # (i.e. this was supposed to be an upgrade with a prior generation) --
    # a genuinely broken prior state, not --first-hub-bootstrap. Must not be
    # silently treated as "nothing to preserve".
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = _run_arm_phase2_rollback(
            Path(tmp), src_exists=False, venv_exists=False, require_phase1_quiescence="1"
        )
    assert result.returncode != 0
    assert "phase-2 apply requires a complete rollback-capable prior generation" in result.stderr


def test_typed_machine_onboarding_receipt_pins_required_cli_paths():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    builder = deploy.split("prepare_remote_prerequisite_bundle() {", 1)[1].split(
        "\n}\n\nprerequisite_bundle_digests", 1
    )[0]

    assert 'path_check("mac-cli", mac_bin, executable=True)' in builder
    assert 'path_check("github-cli", github_cli, executable=True)' in builder
    assert "MAC_PREREQ_NETWORK_PROVIDER=" in builder
    assert 'provider in {"tailscale", "headscale"}' in builder
    assert 'ipaddress.ip_network("100.64.0.0/10")' in builder
    assert 'hostname.endswith((".ts.net", ".svc.cluster.local"))' in builder
    assert "parsed.username is not None" in builder
    assert "parsed.query" in builder


def test_typed_route_receipt_proves_hub_reachability_without_prior_mac_env():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    builder = deploy.split("prepare_remote_prerequisite_bundle() {", 1)[1].split(
        "\n}\n\nprerequisite_bundle_digests", 1
    )[0]

    assert 'hub_url="${fields[7]:-}"' in builder
    assert "MAC_PREREQ_HUB_URL=" in builder
    # The route is proved by dialling the hub, never by requiring the installer's
    # own output. Whether it is required is the controller's classified verdict
    # (see tests/test_deploy_fleet_first_deploy_hub_route.py): every node defaults
    # to a live proof, and only a fresh hub deploying itself defers it.
    route_check = builder.split('"route-tunnel": [', 1)[1].split("\n    ],", 1)[0]
    assert "service_check(" in route_check
    assert '"route-hub",' in route_check
    assert 'os.environ["MAC_PREREQ_HUB_URL"]' in route_check
    assert 'os.environ["MAC_PREREQ_ROUTE_HUB_REQUIRED"]' in route_check
    assert "route_hub_required=1" in builder
    assert '"route-tunnel": [path_check("route-config", mac_env)]' not in builder


def test_typed_optional_prerequisites_do_not_require_installed_mac_env():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    builder = deploy.split("prepare_remote_prerequisite_bundle() {", 1)[1].split(
        "\n}\n\nprerequisite_bundle_digests", 1
    )[0]
    checks = builder.split("home = Path.home()", 1)[1].split("\nreceipts = []", 1)[0]

    assert 'mac_env = mac_home / "mac.env"' not in checks
    assert 'path_check("openshell-disabled-state", mac_home)' in checks
    assert 'return path_check(name + "-disabled-state", state_root)' in builder
    for participant in ("qdrant", "firecrawl", "webdav"):
        assert f'service_check("{participant}",' in checks
    assert checks.count(", mac_home)]") >= 4


def test_network_prerequisite_preparation_is_separate_and_secret_safe():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    prepare = deploy.split("prepare_network_prerequisites() {", 1)[1].split(
        "\n}\n\nnode_prerequisite_bundle_file", 1
    )[0]
    remote = deploy.split("prepare_remote_tailscale_prerequisite() {", 1)[1].split(
        "\n}\n\nprepare_network_prerequisites", 1
    )[0]
    main = deploy.split("main() {", 1)[1]

    assert "--prepare-network-prerequisites" in deploy
    assert prepare.index('echo "==> fleet: classifying') < prepare.index('[ "$blocked" = 0 ]')
    assert prepare.index('[ "$blocked" = 0 ]') < prepare.index(
        "prepare_remote_tailscale_prerequisite"
    )
    assert "IFS= read -r MAC_DEPLOY_TAILSCALE_AUTH_KEY" in remote
    assert "printf '%s\\n' \"$credential\" | ssh" in remote
    assert 'shell_quote "$credential"' not in remote
    assert main.index('if [ "$PREPARE_NETWORK_PREREQUISITES" = 1 ]; then') < (
        main.index('classify_network_prerequisites "$selected_specs_file"')
    )
    network_mode = main.split('if [ "$PREPARE_NETWORK_PREREQUISITES" = 1 ]; then', 1)[1].split(
        'echo "==> network prerequisites prepared', 1
    )[0]
    assert 'hub_token="$(read_hub_token)"' in network_mode
    assert 'MAC_URL="$hub_url_field" MAC_API_TOKEN="$hub_token"' in network_mode
    assert 'prepare_network_prerequisites "$selected_specs_file"' in network_mode
    journal_gate = main.split("initialize_cohort_transaction", 1)[0]
    assert '[ "$PREPARE_NETWORK_PREREQUISITES" != 1 ]' in journal_gate


def test_typed_controller_owns_candidate_proof_and_report_approval_recovery():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    credential = deploy.split("install_pending_worker_credential() (", 1)[1].split(
        "\n)\n\ninstall_and_prove_attestation_candidate", 1
    )[0]
    candidate = deploy.split("install_and_prove_attestation_candidate() (", 1)[1].split(
        "\n)\n\nhub_receipt_identity_sha256", 1
    )[0]
    approval = deploy.split("reconcile_report_repository_executor_approval() (", 1)[1].split(
        "\n)\n\nprovision_bound_worker_credential", 1
    )[0]
    prove = deploy.split("prove_and_commit_hub_epoch() {", 1)[1].split(
        "\n}\n\ncollect_finalize_evidence", 1
    )[0]
    recovery = deploy.split("recover_committed_cohort_node() {", 1)[1].split(
        "\n}\n\ndiscard_unopened_epoch_pending_credentials", 1
    )[0]

    assert "--rotate-missing-attestation-key" not in node
    assert "--rotate-invalid-attestation-key" not in node
    assert "mac.deployment_attestation install" in candidate
    assert "mac.deployment_attestation prove-candidate" in candidate
    assert candidate.index(" install ") < candidate.index("restart_remote_mac_agent_under_epoch")
    assert candidate.index("restart_remote_mac_agent_under_epoch") < candidate.index(
        " prove-candidate "
    )
    assert "set_remote_mac_agent_service" not in candidate
    assert "set_remote_mac_agent_service" not in credential
    epoch_restart = (
        'restart_remote_mac_agent_under_epoch "$agent" "$supervisor" "$fleet_name" restart'
    )
    assert epoch_restart in candidate
    assert epoch_restart in credential
    assert 'remote_directory="/tmp/mac-attestation-' in candidate
    assert "mkdir -m 0700" in candidate
    assert "trap cleanup_remote_attestation_directory EXIT" in candidate
    assert 'rm -rf -- "$remote_directory"' in candidate
    assert prove.index("cohort_journal_mutate hub-prove-start") < prove.index(
        "cohort_journal_mutate hub-proved"
    )
    assert 'hub_epoch_client_read "$hub_agent" "$readiness_receipt" readiness' in prove
    assert prove.index(
        'hub_epoch_client_read "$hub_agent" "$readiness_receipt" readiness'
    ) < prove.index("persist_hub_epoch_recovery_request")
    assert prove.index(
        'hub_epoch_client_read "$hub_agent" "$readiness_receipt" readiness'
    ) < prove.index("cohort_journal_mutate hub-prove-start")
    assert prove.index("cohort_journal_mutate hub-proved") < prove.index(
        "cohort_journal_mutate commit-start"
    )
    assert prove.index("hub_epoch_client_request") < prove.index("cohort_journal_mutate commit")

    assert "/report-repository-executor/approve" in approval
    assert "/report-repository-executor/revoke" in approval
    assert "expected_startup_timestamp" in approval
    assert "agent_has_read_only_report_repository_executor" in approval
    assert "deployment report executor approval failed or drifted" in approval
    assert 'deployment_id="${4:-$(deployment_id_for_agent "$agent")}"' in approval

    assert "report_executor_required" in (
        ROOT / "deploy" / "fleet-cohort-transaction.py"
    ).read_text(encoding="utf-8")
    assert (
        "reconcile_report_repository_executor_approval \\\n"
        '      "$agent" "$hub_agent" 1 "$deployment_id"'
    ) in recovery


def test_committed_recovery_uses_journal_deployment_id_for_report_approval(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    recovery = (
        "recover_committed_cohort_node() {"
        + deploy.split("recover_committed_cohort_node() {", 1)[1].split(
            "\n}\n\ndiscard_unopened_epoch_pending_credentials", 1
        )[0]
        + "\n}"
    )
    journal_deployment_id = (
        "b865bea02e2c316e1e0e715bd0d8e273d0ad7973:"
        "rocky:20260723T005315Z:cc58437dd600b350da0f7907ff1957f7"
    )
    candidate = {
        "agent_name": "rocky",
        "stable_id": "agent_rocky",
        "generation": journal_deployment_id,
        "deployment_id": journal_deployment_id,
        "deploy_ts": "20260723T005315Z",
        "source_commit": "b865bea02e2c316e1e0e715bd0d8e273d0ad7973",
        "fleet": "mac",
        "finalizer_sha256": "a" * 64,
        "state": "finalizing",
        "report_executor_required": True,
    }
    encoded = base64.b64encode(json.dumps(candidate).encode()).decode()
    events = tmp_path / "events"
    snippet = f"""set -euo pipefail
PYTHON_BIN={shlex.quote(sys.executable)}
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
COHORT_JOURNAL_REVISION=34
acquire_remote_deployment_lock() {{ printf 'lock:%s\n' "$2" >> {shlex.quote(str(events))}; }}
reconcile_report_repository_executor_approval() {{ printf 'approval:%s\n' "$4" >> {shlex.quote(str(events))}; }}
cohort_journal_mutate() {{ return 0; }}
run_remote_node_finalizer() {{ printf '{{"status":"fixture"}}\n' > "$7"; }}
validate_finalize_evidence() {{ return 0; }}
finalize_remote_deployment_release() {{ return 0; }}
{recovery}
recover_committed_cohort_node epoch adopted-owner rocky {shlex.quote(encoded)}
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == [
        f"lock:{journal_deployment_id}",
        f"approval:{journal_deployment_id}",
    ]


def test_immutable_node_finalizer_executes_exact_verified_bytes(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    function = deploy.split("run_remote_node_finalizer() {", 1)[1].split(
        "\n}\n\nprepare_remote_prerequisite_bundle", 1
    )[0]
    wrapper = function.split("code=\"$(command cat <<'PY'\n", 1)[1].split('\nPY\n)"', 1)[0]
    capability = tmp_path / ".mac" / "fleet-finalizers" / "fixture-finalizer"
    capability.parent.mkdir(parents=True)
    capability.write_text(
        """import argparse
import json
parser = argparse.ArgumentParser()
parser.add_argument("--mac-home")
parser.add_argument("--agent")
parser.add_argument("--fleet")
parser.add_argument("--generation")
parser.add_argument("--revision")
parser.add_argument("--deploy-ts")
args = parser.parse_args()
print(json.dumps({
    "schema": "mac.fleet_node_finalize.v1",
    "status": "finalized",
    "agent": args.agent,
    "generation": args.generation,
    "file": __file__,
    "module": __name__,
}, sort_keys=True))
""",
        encoding="utf-8",
    )
    capability.chmod(0o700)
    expected = hashlib.sha256(capability.read_bytes()).hexdigest()

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            wrapper,
            capability.name,
            expected,
            "rocky",
            "mac",
            "generation-1",
            "a" * 40,
            "20260723T005315Z",
        ],
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "agent": "rocky",
        "file": str(capability),
        "generation": "generation-1",
        "module": "__main__",
        "schema": "mac.fleet_node_finalize.v1",
        "status": "finalized",
    }
    assert '[sys.executable, "/dev/fd/' not in function


def test_node_finalizer_failure_is_not_swallowed_in_conditional_context(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    function = (
        "run_remote_node_finalizer() {"
        + deploy.split("run_remote_node_finalizer() {", 1)[1].split(
            "\n}\n\nprepare_remote_prerequisite_bundle", 1
        )[0]
        + "\n}"
    )

    for mode in ("transport-failure", "empty-success"):
        case = tmp_path / mode
        case.mkdir()
        output = case / "receipt.json"
        output.write_text("preserved\n", encoding="utf-8")
        snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(case))}
RUNNER_MODE={shlex.quote(mode)}
node_finalizer_capability_name_for_generation() {{ printf '%s\n' fixture-finalizer; }}
run_fenced_remote_python() {{ [ "$RUNNER_MODE" != transport-failure ] || return 9; return 0; }}
validate_finalize_evidence() {{ [ -s "$1" ]; }}
{function}
if run_remote_node_finalizer rocky mac generation-1 {"a" * 40} 20260723T005315Z {"b" * 64} {shlex.quote(str(output))} journal-deployment; then
  printf 'rc=0\n'
else
  printf 'rc=%s\n' "$?"
fi
"""
        result = subprocess.run(
            ["bash", "-c", snippet], text=True, capture_output=True, check=False
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() != "rc=0"
        assert output.read_text(encoding="utf-8") == "preserved\n"
        assert not list(case.glob("node-finalize.*"))


def test_verified_python_rollback_contract_executes_exact_bytes(tmp_path):
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    function = node.split("verified_contract_call() {", 1)[1].split(
        "\n}\n\nverified_lifecycle_call", 1
    )[0]
    verifier = function.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    helper = tmp_path / "rollback-supervisor.py"
    helper.write_text(
        """import json
import sys
print(json.dumps({
    "argv": sys.argv,
    "file": __file__,
    "module": __name__,
}, sort_keys=True))
""",
        encoding="utf-8",
    )
    helper.chmod(0o600)
    expected = hashlib.sha256(helper.read_bytes()).hexdigest()

    result = subprocess.run(
        [sys.executable, "-", "python", str(helper), expected, "quiesce"],
        input=verifier + "\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "argv": [str(helper), "quiesce"],
        "file": str(helper),
        "module": "__main__",
    }
    assert "argv = [sys.executable, descriptor_path" not in function


def test_hub_epoch_client_writes_receipts_in_owner_private_remote_directory():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    read = deploy.split("hub_epoch_client_read() {", 1)[1].split(
        "\n}\n\nhub_epoch_client_request", 1
    )[0]
    request = deploy.split("hub_epoch_client_request() {", 1)[1].split(
        "\n}\n\nhub_epoch_recovery_request_name", 1
    )[0]

    for function in (read, request):
        assert r"mktemp -d \"\$HOME/.mac/.fleet-epoch-client.XXXXXX\"" in function
        assert r"chmod 700 \"\$_mac_tmp\"" in function
        assert r"_mac_output=\"\$_mac_tmp/output.json\"" in function
        assert r"rm -rf \"\$_mac_tmp\"" in function
        assert r"_mac_output=\$(mktemp)" not in function


def test_hub_epoch_open_retries_only_classified_publication_barrier(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    retry = (
        "hub_epoch_client_open_with_retry() {"
        + deploy.split("hub_epoch_client_open_with_retry() {", 1)[1].split(
            "\n}\n\nhub_epoch_recovery_request_name", 1
        )[0]
        + "\n}"
    )
    attempts = tmp_path / "attempts"
    output = tmp_path / "receipt.json"
    request = tmp_path / "request.json"
    request.write_text("{}\n", encoding="utf-8")
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
MAC_DEPLOY_PUBLICATION_BARRIER_WAIT_SECONDS=10
ATTEMPTS={shlex.quote(str(attempts))}
hub_epoch_client_request() {{
  count=0
  [ ! -f "$ATTEMPTS" ] || count="$(cat "$ATTEMPTS")"
  count=$((count + 1))
  printf '%s\\n' "$count" > "$ATTEMPTS"
  if [ "$count" -lt 3 ]; then
    echo "fleet release epoch client error: hub API POST request failed with HTTP 400: canonical runtime-source publication is in progress; retry fleet release epoch open" >&2
    return 2
  fi
  printf '{{"status":"open"}}\\n' > "$3"
}}
sleep() {{ :; }}
{retry}
hub_epoch_client_open_with_retry rocky {shlex.quote(str(request))} {shlex.quote(str(output))} open --epoch epoch-one
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert attempts.read_text(encoding="utf-8").strip() == "3"
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "open"
    assert result.stderr.count("retrying epoch open") == 2


def test_hub_epoch_open_does_not_retry_unclassified_failure(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    retry = (
        "hub_epoch_client_open_with_retry() {"
        + deploy.split("hub_epoch_client_open_with_retry() {", 1)[1].split(
            "\n}\n\nhub_epoch_recovery_request_name", 1
        )[0]
        + "\n}"
    )
    attempts = tmp_path / "attempts"
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
ATTEMPTS={shlex.quote(str(attempts))}
hub_epoch_client_request() {{
  printf 'called\\n' >> "$ATTEMPTS"
  echo "fleet release epoch client error: hub API POST request failed with HTTP 401" >&2
  return 2
}}
{retry}
hub_epoch_client_open_with_retry rocky request.json receipt.json open --epoch epoch-one
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)

    assert result.returncode != 0
    assert attempts.read_text(encoding="utf-8").splitlines() == ["called"]
    assert "HTTP 401" in result.stderr


def test_hub_epoch_open_retries_indeterminate_transport_timeout(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    retry = (
        "hub_epoch_client_open_with_retry() {"
        + deploy.split("hub_epoch_client_open_with_retry() {", 1)[1].split(
            "\n}\n\nhub_epoch_recovery_request_name", 1
        )[0]
        + "\n}"
    )
    attempts = tmp_path / "attempts"
    output = tmp_path / "receipt.json"
    request = tmp_path / "request.json"
    request.write_text("{}\n", encoding="utf-8")
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
MAC_DEPLOY_PUBLICATION_BARRIER_WAIT_SECONDS=10
ATTEMPTS={shlex.quote(str(attempts))}
hub_epoch_client_request() {{
  count=0
  [ ! -f "$ATTEMPTS" ] || count="$(cat "$ATTEMPTS")"
  count=$((count + 1))
  printf '%s\\n' "$count" > "$ATTEMPTS"
  if [ "$count" -lt 3 ]; then
    echo "socket.timeout: timed out" >&2
    return 2
  fi
  printf '{{"status":"open"}}\\n' > "$3"
}}
sleep() {{ :; }}
{retry}
hub_epoch_client_open_with_retry rocky {shlex.quote(str(request))} {shlex.quote(str(output))} open --epoch epoch-one
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert attempts.read_text(encoding="utf-8").strip() == "3"
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "open"
    assert result.stderr.count("retrying idempotent open") == 2


def test_hub_epoch_recovery_retries_open_but_not_other_phases():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    replay = deploy.split("replay_hub_epoch_recovery_request() {", 1)[1].split(
        "\n}\n\nwrite_hub_identity_request", 1
    )[0]

    assert 'if [ "$phase" = "open" ]; then' in replay
    assert 'hub_epoch_client_open_with_retry "$hub_agent" "$local_request" "$output"' in replay
    assert 'hub_epoch_client_request "$hub_agent" "$local_request" "$output"' in replay


def _run_hub_epoch_client_harness(tmp_path, *, helper, ssh_rc, ssh_output, existing_output=None):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    validator = (
        "validate_hub_epoch_client_output() {"
        + deploy.split("validate_hub_epoch_client_output() {", 1)[1].split(
            "\n}\n\nhub_epoch_client_read", 1
        )[0]
        + "\n}"
    )
    read = (
        "hub_epoch_client_read() {"
        + deploy.split("hub_epoch_client_read() {", 1)[1].split(
            "\n}\n\nhub_epoch_client_request", 1
        )[0]
        + "\n}"
    )
    request = (
        "hub_epoch_client_request() {"
        + deploy.split("hub_epoch_client_request() {", 1)[1].split(
            "\n}\n\nhub_epoch_recovery_request_name", 1
        )[0]
        + "\n}"
    )
    client = tmp_path / "hub-client.py"
    client.write_text("# synthetic client\n", encoding="utf-8")
    request_file = tmp_path / "request.json"
    request_file.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "receipt.json"
    if existing_output is not None:
        output.write_text(existing_output, encoding="utf-8")
        output.chmod(0o600)

    if helper == "read":
        invocation = f"hub_epoch_client_read rocky {shlex.quote(str(output))} status"
    elif helper == "request":
        invocation = (
            f"hub_epoch_client_request rocky {shlex.quote(str(request_file))} "
            f"{shlex.quote(str(output))} commit"
        )
    else:
        raise AssertionError(f"unexpected helper: {helper}")

    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
HUB_EPOCH_CLIENT={shlex.quote(str(client))}
PYTHON_BIN={shlex.quote(sys.executable)}
ssh_target_args() {{ printf 'synthetic-hub\\0'; }}
shell_quote() {{ printf "'%s'" "$1"; }}
pinned_remote_private_upload() {{ return 0; }}
ssh() {{ printf '%s' "$FAKE_SSH_OUTPUT"; return "$FAKE_SSH_RC"; }}
{validator}
{read}
{request}
status=0
if ! {invocation}; then
  status=1
fi
printf 'status=%s\\n' "$status"
"""
    return subprocess.run(
        ["bash", "-c", snippet],
        env={
            **os.environ,
            "FAKE_SSH_RC": str(ssh_rc),
            "FAKE_SSH_OUTPUT": ssh_output,
        },
        text=True,
        capture_output=True,
        check=False,
    ), output


def test_hub_epoch_clients_preserve_destination_on_transport_failure_under_if(tmp_path):
    prior = '{"prior":true}\n'
    for helper in ("read", "request"):
        case = tmp_path / helper
        case.mkdir()
        result, output = _run_hub_epoch_client_harness(
            case,
            helper=helper,
            ssh_rc=55,
            ssh_output="",
            existing_output=prior,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "status=1\n"
        assert output.read_text(encoding="utf-8") == prior


def test_hub_epoch_clients_reject_empty_or_invalid_success_output(tmp_path):
    for helper in ("read", "request"):
        for label, response in (("empty", ""), ("array", "[]\n")):
            case = tmp_path / helper / label
            case.mkdir(parents=True)
            result, output = _run_hub_epoch_client_harness(
                case,
                helper=helper,
                ssh_rc=0,
                ssh_output=response,
            )

            assert result.returncode == 0, result.stderr
            assert result.stdout == "status=1\n"
            assert not output.exists()


def test_hub_epoch_clients_atomically_promote_valid_private_json(tmp_path):
    response = '{"schema":"synthetic.receipt.v1"}\n'
    for helper in ("read", "request"):
        case = tmp_path / helper
        case.mkdir()
        result, output = _run_hub_epoch_client_harness(
            case,
            helper=helper,
            ssh_rc=0,
            ssh_output=response,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "status=0\n"
        assert output.read_text(encoding="utf-8") == response
        assert output.stat().st_mode & 0o777 == 0o600


def test_same_host_attestation_recovery_keeps_distinct_hub_and_worker_copies():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    recovery = deploy.split("reconcile_bound_worker_attestation_key() (", 1)[1].split(
        "\n)\n\nreconcile_report_repository_executor_approval", 1
    )[0]

    hub_path = next(line.strip() for line in recovery.splitlines() if "local hub_manifest=" in line)
    worker_path = next(
        line.strip() for line in recovery.splitlines() if "local worker_manifest=" in line
    )
    assert "attestation-recovery-hub-" in hub_path
    assert "attestation-recovery-worker-" in worker_path
    assert hub_path != worker_path
    assert 'Path(os.environ["MAC_DEPLOY_ATTESTATION_MANIFEST"]).unlink(' in recovery
    assert "missing_ok=True" in recovery


def test_same_host_worker_credential_failure_preserves_hub_retry_manifest():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    rollout = deploy.split("provision_bound_worker_credential() (", 1)[1].split(
        "\n)\n\nfinalize_remote_deployment_release", 1
    )[0]
    assignments = {
        name: next(line.strip() for line in rollout.splitlines() if "local %s=" % name in line)
        for name in (
            "hub_manifest",
            "hub_receipt",
            "worker_manifest",
            "worker_receipt",
        )
    }
    assert "worker-credential-hub-" in assignments["hub_manifest"]
    assert "worker-credential-worker-" in assignments["worker_manifest"]
    assert assignments["hub_manifest"] != assignments["worker_manifest"]
    assert assignments["hub_receipt"] != assignments["worker_receipt"]

    cleanup = rollout.split("cleanup_worker_relay() {", 1)[1].split(
        "trap cleanup_worker_relay EXIT", 1
    )[0]
    assert '$(shell_quote "$worker_manifest")' in cleanup
    assert '$(shell_quote "$worker_receipt")' in cleanup
    assert "$hub_manifest" not in cleanup
    assert "$hub_receipt" not in cleanup

    failure = rollout.index('if [ "$activate_ok" != "1" ]')
    preserve = rollout.index("hub retry manifest retained", failure)
    failure_return = rollout.index("return 1", preserve)
    success_cleanup = rollout.index(
        '"rm -f $(shell_quote "$hub_manifest") $(shell_quote "$hub_receipt")"',
        failure_return,
    )
    release = rollout.index("set_remote_mac_agent_service", success_cleanup)
    assert failure < preserve < failure_return < success_cleanup < release
    assert '--manifest $(shell_quote "$hub_manifest")' in rollout


def test_report_executor_release_gate_applies_to_all_selected_supervisors():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    service = deploy.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]
    gate = deploy.split("hub_agent_restart_gate() {", 1)[1].split(
        "remote_deployment_hold_state() {", 1
    )[0]
    # The common service path resolves launchd/systemd/supervisord (the current
    # selected GKE pods use supervisord over their frozen SSH routes) before the
    # shared arm gate; no supervisor-specific route can bypass the proof.
    for supervisor in ("launchd)", "systemd)", "supervisord)"):
        assert supervisor in service
    assert "MAC_DEPLOY_GATE_REQUIRE_REPORT_EXECUTOR" in gate
    assert "report_executor_ready(resources)" in gate
    assert "and report_executor_ready(resources)" in gate


def test_remote_restart_helper_keeps_then_releases_the_deployment_barrier():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    service_control = deploy.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]
    hub_gate = deploy.split("hub_agent_restart_gate() {", 1)[1].split(
        "remote_deployment_hold_state() {", 1
    )[0]

    assert 'phase == "verify"' in hub_gate
    assert 'phase in {"arm", "release"}' in hub_gate
    assert "MAC_WORKER_DEPLOY_GENERATION" in service_control
    assert 'row.get("status") == "draining"' in hub_gate
    assert "seen > baseline" in hub_gate
    assert "release_mode" in service_control
    assert '[ "$release_mode" = keep ]' in service_control
    assert "restart kept under deployment barrier" in service_control
    assert "worker-generated idle heartbeat" in hub_gate
    assert "release_health_ready(row, resources)" in hub_gate
    # The blocking-problems check moved into mac.agent_health with the rest of
    # the rule; asserting its inline spelling here pinned the duplicate.
    assert "advisory_health_dispatch_ready(" in hub_gate
    assert '{"health_status": "healthy"}' not in hub_gate
    assert 'if phase == "arm":' in hub_gate
    assert '"release_ready": True' in hub_gate
    assert '"/agents/%s/dispatch-hold/release" % agent_id' in hub_gate
    assert 'if not payload.get("released")' in hub_gate
    unlink_pos = service_control.index('rm -f "$barrier_path"')
    release_pos = service_control.index('hub_agent_restart_gate "$release_gate_phase"')
    assert unlink_pos < release_pos

    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    main = node.split('write_deploy_manifest "pre" "$MANIFEST_PRE"', 1)[1]
    verify_pos = main.index("verify_hub_registration")
    defer_pos = main.index("keeping drain state until post-deploy", verify_pos)
    post_manifest_pos = main.index('write_deploy_manifest "post"', defer_pos)
    assert verify_pos < defer_pos < post_manifest_pos


def test_typed_restarts_reuse_the_one_journal_bound_generation():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    generation = deploy.split("worker_generation_for_agent() {", 1)[1].split("\n}", 1)[0]
    restart = deploy.split("restart_remote_mac_agent_under_epoch() {", 1)[1].split(
        "\n}\n\nrun_typed_cohort", 1
    )[0]
    typed = deploy.split("run_typed_cohort() {", 1)[1].split("\n}\n\nmain()", 1)[0]

    assert 'deployment_id_for_agent "$1"' in generation
    assert "secrets.token_hex" not in restart
    assert 'phase1_resolved_supervisor_for_agent "$agent"' in restart
    assert 'supervisor="$resolved_supervisor"' in restart
    assert "configured supervisor differs from phase-1 proof" in restart
    assert 'local activation_mode="${4:-activate}"' in restart
    assert "activate) manager_action=start" in restart
    assert "restart) manager_action=restart" in restart
    assert 'systemctl $(shell_quote "$manager_action")' in restart
    assert 'supervisorctl $(shell_quote "$manager_action")' in restart
    assert 'domain=\\"gui/\\$(id -u)\\"' in restart
    assert "mac_launchd_stop_job_if_present" in restart
    assert "mac_launchd_bootstrap_job" in restart
    assert "system/com.${fleet_name}.agent" not in restart
    assert 'generation="$(worker_generation_for_agent "$agent")"' in typed
    assert '--generation "$generation"' in typed
    deploy_host = deploy.split("deploy_host() {", 1)[1].split(
        "\n}\n\nrestart_remote_mac_agent_under_epoch", 1
    )[0]
    apply_worker = deploy.split("typed_phase2_apply_worker() {", 1)[1].split(
        "\n}\n\ntyped_finalize_worker", 1
    )[0]
    phase2_start = typed.index("cohort_journal_mutate phase2-start")
    apply_handoff = typed.index('typed_phase2_apply_worker "$spec"', phase2_start)
    prepared = typed.index("cohort_journal_mutate prepared", apply_handoff)
    assert phase2_start < apply_handoff < prepared
    assert 'deploy_host "$spec"' in apply_worker
    assert 'if [ "$node_action" = apply-phase2 ]' in deploy_host
    assert "restart_remote_mac_agent_under_epoch" in deploy_host


def test_typed_deploy_proves_pending_identity_before_atomic_hub_commit():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    typed = deploy.split("run_typed_cohort() {", 1)[1].split("\n}\n\nmain()", 1)[0]
    prove = deploy.split("prove_and_commit_hub_epoch() {", 1)[1].split(
        "\n}\n\ncollect_finalize_evidence", 1
    )[0]

    apply_worker = deploy.split("typed_phase2_apply_worker() {", 1)[1].split(
        "\n}\n\ntyped_finalize_worker", 1
    )[0]
    install = apply_worker.index("install_pending_worker_credential")
    candidate = apply_worker.index("install_and_prove_attestation_candidate", install)
    readiness = apply_worker.index("collect_typed_release_ready_evidence", candidate)
    assert install < candidate < readiness
    apply_handoff = typed.index('typed_phase2_apply_worker "$spec"')
    prepared = typed.index("cohort_journal_mutate prepared", apply_handoff)
    commit = typed.index("prove_and_commit_hub_epoch", prepared)
    finalize = typed.index("cohort_journal_mutate finalize-start", commit)
    assert install < candidate < readiness < prepared < commit < finalize
    commit_start = prove.index("cohort_journal_mutate commit-start")
    commit_request = prove.index("hub_epoch_client_request", commit_start)
    commit_receipt = prove.index("cohort_journal_mutate commit", commit_request)
    assert commit_start < commit_request < commit_receipt


def test_typed_release_readiness_hands_local_barrier_to_open_epoch_hold():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    barrier = deploy.split("release_typed_worker_start_barrier() {", 1)[1].split(
        "\n}\n\ncollect_typed_release_ready_evidence", 1
    )[0]
    collector = deploy.split("collect_typed_release_ready_evidence() {", 1)[1].split(
        "\n}\n\ntyped_phase2_arm_worker", 1
    )[0]
    assert "assert_remote_deployment_lock" in barrier
    assert 'remote_deployment_fenced_exec "$deployment_id" 0 bash -s' in barrier
    assert '[ "$(cat "$barrier_path")" = "$generation" ]' in barrier
    assert 'rm -f "$barrier_path"' in barrier
    assert '"$TMPDIR_LOCAL/participant-state-${agent_id}.json"' in collector
    assert '"$TMPDIR_LOCAL/hub-open-receipt.json"' in collector
    assert 'receipt.get("status") != "open"' in collector
    assert 'participant.get("epoch_hold_reason")' in collector
    assert 'participant.get("principal_id") != principal_id' in collector
    assert "assert_phase1_attestation_matches_controller" in collector
    release = collector.index("release_typed_worker_start_barrier")
    readiness = collector.index("hub_agent_restart_gate arm", release)
    evidence = collector.index("write_release_ready_evidence", readiness)
    assert release < readiness < evidence
    assert '"$hold_reason" 1 0 1 "$hold_reason" "$principal_id" "" 1 0' in collector
    assert "write_release_ready_evidence" in collector
    assert "set_remote_mac_agent_service" not in collector


def test_release_ready_writer_requires_exact_node_generation(tmp_path: Path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    writer = deploy.split("write_release_ready_evidence() {", 1)[1].split(
        "\n}\n\nset_remote_mac_agent_service", 1
    )[0]
    embedded = writer.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    output = tmp_path / "ready.json"
    deployment_id = "a" * 40 + ":rocky:stamp:nonce"
    quiescence = json.dumps(
        {
            "schema": "mac.daemon_resource_quiescence_attestation.v1",
            "agent": "rocky",
            "generation": deployment_id,
        }
    )
    arguments = [
        str(output),
        "rocky",
        "agent_rocky",
        "launchd",
        "mac",
        deployment_id,
        "2026-07-21T00:00:00Z",
        "fleet release epoch hold",
        "1",
        "principal-rocky",
        "1",
        deployment_id,
        "0",
        quiescence,
    ]
    result = subprocess.run(
        [sys.executable, "-c", embedded, *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == "mac.deploy_release_ready.v1"
    assert payload["owns_hold"] is True
    assert payload["principal_id"] == "principal-rocky"
    assert output.stat().st_mode & 0o777 == 0o600

    arguments[-3] = deployment_id + "-wrong"
    mismatch = subprocess.run(
        [sys.executable, "-c", embedded, *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    assert mismatch.returncode != 0
    assert "exact daemon quiescence" in mismatch.stderr


def test_deferred_supervisord_worker_never_autostarts_during_transaction():
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    function = node.split("install_supervisord_service() {", 1)[1].split(
        "\n}\n\ninstall_launchd_services()", 1
    )[0]

    assert "local agent_autostart=true" in function
    assert 'if truthy "$DEFER_AGENT_RESTART"' in function
    assert "agent_autostart=false" in function
    assert "autostart=$agent_autostart" in function


def test_service_stop_is_verified_before_openshell_bootstrap():
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    main = node.split('write_deploy_manifest "pre" "$MANIFEST_PRE"', 1)[1]

    stop = main.index("stop_existing_services_for_deploy\n")
    bootstrap = main.index("bootstrap_enabled_openshell\n", stop)
    assert stop < bootstrap
    assert "stop_systemd_service_if_present" in node
    assert "stop_supervisord_program_if_present" in node
    assert "remained $active_state after stop" in node
    assert "did not become inactive" in node


def test_openshell_deploy_validates_in_node_before_manifest_and_restart():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    main = node.split('write_deploy_manifest "pre" "$MANIFEST_PRE"', 1)[1]

    drain = main.index("drain_mac_agent_before_deploy\n")
    stop = main.index("stop_existing_services_for_deploy\n", drain)
    venv = main.index('"$VENV/bin/python" -m pip install -e', stop)
    bootstrap = main.index("bootstrap_enabled_openshell\n", venv)
    service_install = main.index('case "$SUPERVISOR_KIND" in', bootstrap)
    runtime_proof = main.index("verify_managed_openshell_runtime\n", service_install)
    registration = main.index("verify_hub_registration\n", runtime_proof)
    clear_drain = main.index("clear_mac_agent_drain_after_deploy", registration)
    post_manifest = main.index('write_deploy_manifest "post"', clear_drain)

    assert drain < stop < venv < bootstrap < service_install
    assert service_install < runtime_proof < registration < clear_drain < post_manifest
    # A bootstrap failure exits under set -e before service creation, drain clear,
    # or a durable post manifest can make the node appear deployable.
    assert "bootstrap_enabled_openshell || true" not in main
    typed_apply = main.split('elif [ "$NODE_ACTION" = apply-phase2 ]; then', 1)[1].split(
        "\nelse\n", 1
    )[0]
    assert "bootstrap_enabled_openshell" in typed_apply

    deploy_host = deploy.split("deploy_host() {", 1)[1].split("\n}\n\nhub_target()", 1)[0]
    assert "run_openshell_bootstrap" not in deploy_host
    assert (
        'set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" stop'
    ) not in deploy_host
    restart = 'set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" restart keep'
    assert deploy_host.count(restart) == 1
    reconcile = deploy_host.index('reconcile_remote_deploy "$agent" "$target"')
    assert reconcile < deploy_host.index(restart, reconcile)
    failed_reconcile = (
        'if ! reconcile_remote_deploy "$agent" "$target" "$openshell_disable_requested"; then'
    )
    assert failed_reconcile in deploy_host
    failure_block = deploy_host.split(failed_reconcile, 1)[1].split("fi", 1)[0]
    assert "return 1" in failure_block


def test_typed_restart_resolves_auto_from_generation_bound_phase1_contract(
    tmp_path: Path,
):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    resolver = (
        "phase1_resolved_supervisor_for_agent() {"
        + deploy.split("phase1_resolved_supervisor_for_agent() {", 1)[1].split(
            "\n}\n\ncleanup_failed_phase1_prepare_lock", 1
        )[0]
        + "\n}"
    )
    revision = "a" * 40
    generation = f"{revision}:rocky:20260721T214448Z:nonce"
    contract = {
        "schema": "mac.phase1_cohort_restore_contract.v1",
        "status": "prepared",
        "agent": "rocky",
        "generation": generation,
        "revision": revision,
        "rollback_capable": True,
        "supervisor": {"manager": "launchd"},
    }
    contract_raw = (json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ready = {
        "schema": "mac.phase1_restore_contract_ready.v1",
        "agent": "rocky",
        "generation": generation,
        "revision": revision,
        "contract_sha256": hashlib.sha256(contract_raw).hexdigest(),
        "contract": contract,
    }
    contract_path = tmp_path / "phase1.json"
    contract_path.write_text(
        json.dumps(ready, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    contract_path.chmod(0o600)
    command = f"""set -euo pipefail
PYTHON_BIN={shlex.quote(sys.executable)}
GIT_REV={revision}
CONTRACT={shlex.quote(str(contract_path))}
phase1_restore_contract_file_for_agent() {{ printf '%s\\n' "$CONTRACT"; }}
deployment_id_for_agent() {{ printf '%s\\n' {shlex.quote(generation)}; }}
{resolver}
phase1_resolved_supervisor_for_agent rocky
"""
    result = subprocess.run(
        ["/bin/bash", "-c", command], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "launchd"


def test_deploy_restarts_agent_only_after_post_manifest_reconciliation():
    text = script_text()

    assert 'DEFER_AGENT_RESTART="${MAC_DEPLOY_DEFER_AGENT_RESTART:-0}"' in text
    assert text.count("deferring mac-agent restart until post-manifest reconciliation") == 3
    assert "An intentionally STOPPED supervisord program makes" in text
    assert "restart deferred until post-manifest reconciliation" in text
    assert "Keep executing the remote deployment" in text
    reconcile_pos = text.index('reconcile_remote_deploy "$agent" "$target"')
    external_restart_pos = text.index(
        "restarting mac-agent after post-manifest reconciliation", reconcile_pos
    )
    assert reconcile_pos < external_restart_pos

    service_control = text.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]
    assert 'plist="$HOME/Library/LaunchAgents/${label}.plist"' in service_control
    assert "MAC_DEPLOY_SERVICE_TS=" in service_control
    assert (
        'lifecycle="$HOME/.mac/logs/launchd-lifecycle-${MAC_DEPLOY_SERVICE_TS:?}.sh"'
        in service_control
    )
    assert '. "$lifecycle"' in service_control
    stop = service_control.index('mac_launchd_stop_job_if_present "$domain/$label" "$label" user')
    bootstrap = service_control.index("mac_launchd_bootstrap_job", stop)
    assert '"$domain" "$plist" "$domain/$label" "$label" user' in service_control[bootstrap:]
    assert stop < bootstrap
    assert "launchctl " not in service_control
    assert "$(( SECONDS" not in service_control


def test_deployment_preserves_operator_holds_and_clears_only_its_own():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    hub_gate = deploy.split("hub_agent_restart_gate() {", 1)[1].split(
        "remote_deployment_hold_state() {", 1
    )[0]

    assert "mac.deploy_dispatch_hold.v1" in deploy
    assert "prior_hold_reason" in hub_gate
    assert 'owns_hold = prior_owned and row.get("dispatch_hold_reason") in {' in hub_gate
    release = hub_gate.split('elif phase in {"arm", "release"}:', 1)[1].split(
        'elif phase == "rehold":', 1
    )[0]
    assert "if prior_owned:" in release
    assert "refusing to clear a hold no longer owned by this deployment" in release
    assert '"/agents/%s/dispatch-hold/release" % agent_id' in release
    assert "deployment dispatch hold ownership changed before release" in release
    assert '"/agents/%s/dispatch-hold/acquire" % agent_id' in hub_gate
    # Managed nodes never let an ordinary worker restart clear a later operator
    # hold; all deployment release is explicit, hub-side, and reason-bound.
    assert 'set_remote_mac_startup_hold_policy "$agent" 0' in deploy


def test_failed_typed_transaction_aborts_exact_epoch_before_node_retention():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    recovery = deploy.split("recover_active_cohort_transaction_v2() {", 1)[1].split(
        "\n}\n\nrecover_incomplete_cohort_transaction_before_deploy", 1
    )[0]

    abort_hub = recovery.index("abort_hub_epoch_exact")
    journal_hub_abort = recovery.index("cohort_journal_mutate hub-aborted", abort_hub)
    rollback_nodes = recovery.index("recover_cohort_node", journal_hub_abort)
    journal_abort = recovery.index("cohort_journal_mutate abort", rollback_nodes)
    assert abort_hub < journal_hub_abort < rollback_nodes < journal_abort
    assert 'if [ "$direction" = rollback ] || [ "$direction" = retain_forward ]' in recovery
    assert "incomplete typed cohort retained newest state for roll-forward repair" in recovery
    assert "commit_hub_epoch_exact" in recovery
    assert recovery.index("commit_hub_epoch_exact") < recovery.index("cohort_journal_mutate commit")
    resolve_prove = recovery.split("resolve_prove)", 1)[1].split("\n      abort_epoch)", 1)[0]
    assert "read_hub_epoch_status_exact" in resolve_prove
    assert "replay_hub_epoch_recovery_request" not in resolve_prove
    assert "open) ;;" in resolve_prove
    assert "cohort_journal_mutate hub-proved" in resolve_prove
    assert "cohort_journal_mutate hub-aborted" in resolve_prove


def test_brand_new_agent_registers_draining_before_hub_hold_acquisition():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    service_control = deploy.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]
    hub_gate = deploy.split("hub_agent_restart_gate() {", 1)[1].split(
        "remote_deployment_hold_state() {", 1
    )[0]

    assert "agent_existed" in deploy
    assert 'phase == "prepare-new"' in hub_gate
    assert "new worker never atomically registered under its local deployment barrier" in hub_gate
    barrier_write = service_control.index("barrier_tmp.write_text")
    service_start = service_control.index('run_fleet_supervisorctl start "$program"')
    prepare_new = service_control.index("hub_agent_restart_gate prepare-new", service_start)
    strict_verify = service_control.index("hub_agent_restart_gate verify", prepare_new)
    assert barrier_write < service_start < prepare_new < strict_verify


def test_tombstoned_configured_agent_reenters_through_prepare_new_gate():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    hub_gate = deploy.split("hub_agent_restart_gate() {", 1)[1].split(
        "remote_deployment_hold_state() {", 1
    )[0]
    service_control = deploy.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]

    # Historical GET visibility must not classify a tombstone as a live
    # deployment target.  Returning exists=False sets new_agent=1; after the
    # admin-authorized worker resurrection, prepare-new atomically reacquires
    # the hold before strict generation verification.
    assert 'row.get("deleted_at") and missing_ok' in hub_gate
    initial_prepare = hub_gate.split('if phase == "prepare":', 1)[1].split(
        'elif phase == "verify":', 1
    )[0]
    assert "row = agent_row(missing_ok=allow_missing)" in initial_prepare
    new_agent = service_control.index(
        'new_agent="$(printf \'%s\' "$gate_result"',
    )
    service_start = service_control.index('run_fleet_supervisorctl start "$program"')
    prepare_new = service_control.index("hub_agent_restart_gate prepare-new", service_start)
    verify = service_control.index("hub_agent_restart_gate verify", prepare_new)
    assert new_agent < service_start < prepare_new < verify


def test_deployment_controller_is_fenced_across_every_restart_and_release():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    python_resolution = deploy.index('PYTHON_BIN="$(resolve_python_bin)"')
    nonce_generation = deploy.index("secrets.token_hex(16)", python_resolution)
    deployment_id = deploy.index("deployment_id_for_agent()", nonce_generation)
    assert python_resolution < nonce_generation < deployment_id
    assert "readonly DEPLOY_CONTROLLER_NONCE" in deploy
    assert '"$DEPLOY_CONTROLLER_NONCE"' in deploy[deployment_id:]
    prepare = deploy.split("prepare_remote_mac_agent_deployment() {", 1)[1].split(
        "set_remote_mac_agent_service() {", 1
    )[0]
    service = deploy.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]

    assert "mac.deploy_controller_lock.v1" in deploy
    assert "another deployment owns this node" in deploy
    assert "MAC_DEPLOY_TAKEOVER_STALE_LOCK" in deploy
    assert "explicit stale takeover required" in deploy
    acquire = prepare.index('acquire_remote_deployment_lock "$agent" "$deployment_id"')
    state_read = prepare.index('state="$(remote_deployment_hold_state "$agent")"')
    gate = prepare.index('hub_agent_restart_gate "$gate_phase"', state_read)
    assert acquire < state_read < gate
    assert 'expected_deployment_id="$(deployment_id_for_agent "$agent")"' in service
    assert 'assert_remote_deployment_lock "$agent" "$expected_deployment_id"' in service
    assert 'if [ "$deployment_id" != "$expected_deployment_id" ]' in service
    arm = service.index('hub_agent_restart_gate "$release_gate_phase"')
    ready_record = service.index("write_release_ready_evidence", arm)
    assert arm < ready_record
    writer = deploy.split("write_release_ready_evidence() {", 1)[1].split(
        "\n}\n\nset_remote_mac_agent_service", 1
    )[0]
    assert "mac.deploy_release_ready.v1" in writer
    commit = deploy.split("commit_fleet_release_epoch() {", 1)[1].split(
        "enforce_bound_worker_credentials() {", 1
    )[0]
    batch_transition = commit.index('"/agents/dispatch-hold/transition-batch"')
    batch_release = commit.index('"/agents/dispatch-hold/release-batch"')
    lock_release = commit.index("finalize_remote_deployment_release", batch_release)
    assert batch_transition < lock_release
    assert batch_release < lock_release


def test_deployment_controller_lock_assertion_executes_and_renews(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    function = deploy.split("assert_remote_deployment_lock() {", 1)[1].split(
        "release_remote_deployment_lock() {", 1
    )[0]
    embedded_python = function.split("python3 - <<'PY'\n", 1)[1].split('\nPY"', 1)[0]
    lock_dir = tmp_path / ".mac" / "deploy-controller.lock"
    lock_dir.mkdir(parents=True)
    owner = lock_dir / "owner.json"
    owner.write_text(
        json.dumps({"deployment_id": "deployment-test", "renewed_at_epoch": 1.0}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-c", embedded_python],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "MAC_DEPLOY_LOCK_ID": "deployment-test",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    renewed = json.loads(owner.read_text(encoding="utf-8"))
    assert renewed["deployment_id"] == "deployment-test"
    assert renewed["renewed_at_epoch"] > 1.0
    assert owner.stat().st_mode & 0o777 == 0o600
    assert list(lock_dir.iterdir()) == [owner]


def test_node_install_lock_renewal_executes_and_fails_closed_on_fence_loss(tmp_path):
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    function = node.split("deployment_lock_assert_and_renew() {", 1)[1].split(
        "\n}\n\ndeployment_lock_assert_and_renew", 1
    )[0]
    embedded_python = function.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    lock_dir = tmp_path / ".mac" / "deploy-controller.lock"
    lock_dir.mkdir(parents=True)
    owner = lock_dir / "owner.json"
    owner.write_text(
        json.dumps({"deployment_id": "node-install-epoch", "renewed_at_epoch": 1.0}),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "MAC_DEPLOY_LOCK_DIR": str(lock_dir),
        "MAC_DEPLOY_LOCK_ID": "node-install-epoch",
    }

    renewed = subprocess.run(
        [sys.executable, "-c", embedded_python],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert renewed.returncode == 0, renewed.stderr
    payload = json.loads(owner.read_text(encoding="utf-8"))
    assert payload["deployment_id"] == "node-install-epoch"
    assert payload["renewed_at_epoch"] > 1.0
    assert owner.stat().st_mode & 0o777 == 0o600

    before_loss = owner.read_bytes()
    fenced = subprocess.run(
        [sys.executable, "-c", embedded_python],
        env={**env, "MAC_DEPLOY_LOCK_ID": "stale-controller"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert fenced.returncode != 0
    assert "deployment lock fence no longer belongs" in fenced.stderr
    assert owner.read_bytes() == before_loss

    renewer = node.split("deployment_lock_renewer() {", 1)[1].split(
        "\n}\ndeployment_lock_renewer", 1
    )[0]
    assert 'sleep "${MAC_DEPLOY_LOCK_RENEW_SECONDS:-20}"' in renewer
    assert 'kill -TERM "$controller_pid"' in renewer
    exit_handler = node.split("deployment_exit_handler() {", 1)[1].split("\n}\ntrap", 1)[0]
    assert "if deployment_lock_assert_and_renew; then" in exit_handler
    assert '"${ROLLBACK_SCRIPT}" || rollback_rc=$?' in exit_handler
    assert '[ -x "${ROLLBACK_SCRIPT:-}" ]' in exit_handler
    assert exit_handler.index('"${ROLLBACK_SCRIPT}" || rollback_rc=$?') < (
        exit_handler.index("stop_deployment_lock_renewer")
    )
    assert "trap 'deployment_exit_handler \"$?\"' EXIT" in node


def _wait_for_path(path: Path, process: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            _stdout, stderr = process.communicate()
            raise AssertionError(stderr)
        time.sleep(0.01)
    raise AssertionError("timed out waiting for concurrency harness")


def test_node_renewer_cannot_aba_over_a_replaced_lock_directory(tmp_path):
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    function = node.split("deployment_lock_assert_and_renew() {", 1)[1].split(
        "\n}\n\ndeployment_lock_assert_and_renew", 1
    )[0]
    renew_python = function.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    mac_home = tmp_path / ".mac"
    lock_dir = mac_home / "deploy-controller.lock"
    lock_dir.mkdir(parents=True)
    owner = lock_dir / "owner.json"
    owner.write_text(
        json.dumps({"deployment_id": "old-controller", "renewed_at_epoch": 1.0}),
        encoding="utf-8",
    )
    ready = tmp_path / "holder-ready"
    replace = tmp_path / "replace-now"
    holder_code = r"""
import fcntl
import json
import os
import sys
import time
from pathlib import Path

home, ready, replace = map(Path, sys.argv[1:])
root = home / ".mac"
guard = os.open(str(root / "deploy-controller.guard"), os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(guard, fcntl.LOCK_EX)
ready.touch()
while not replace.exists():
    time.sleep(0.01)
lock = root / "deploy-controller.lock"
stale = root / "deploy-controller.lock.replaced"
os.replace(lock, stale)
lock.mkdir(mode=0o700)
(lock / "owner.json").write_text(
    json.dumps({"deployment_id": "new-controller", "renewed_at_epoch": time.time()}),
    encoding="utf-8",
)
fcntl.flock(guard, fcntl.LOCK_UN)
os.close(guard)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(tmp_path), str(ready), str(replace)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for_path(ready, holder)
    renewer = subprocess.Popen(
        [sys.executable, "-c", renew_python],
        env={
            **os.environ,
            "MAC_DEPLOY_LOCK_DIR": str(lock_dir),
            "MAC_DEPLOY_LOCK_ID": "old-controller",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.1)
    assert renewer.poll() is None, "renewer did not wait on the stable guard"
    replace.touch()
    holder_stdout, holder_stderr = holder.communicate(timeout=10)
    assert holder.returncode == 0, holder_stdout + holder_stderr
    _stdout, stderr = renewer.communicate(timeout=10)
    assert renewer.returncode != 0
    assert "fence no longer belongs" in stderr
    current = json.loads(owner.read_text(encoding="utf-8"))
    assert current["deployment_id"] == "new-controller"


def test_stale_takeover_rechecks_freshness_under_the_stable_guard(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    function = deploy.split("acquire_remote_deployment_lock() {", 1)[1].split(
        "assert_remote_deployment_lock() {", 1
    )[0]
    acquire_python = function.split("python3 - <<'PY'\n", 1)[1].split('\nPY"', 1)[0]
    mac_home = tmp_path / ".mac"
    lock_dir = mac_home / "deploy-controller.lock"
    lock_dir.mkdir(parents=True)
    owner = lock_dir / "owner.json"
    owner.write_text(
        json.dumps({"deployment_id": "live-controller", "renewed_at_epoch": 1.0}),
        encoding="utf-8",
    )
    ready = tmp_path / "refresh-ready"
    refresh = tmp_path / "refresh-now"
    holder_code = r"""
import fcntl
import json
import os
import sys
import time
from pathlib import Path

home, ready, refresh = map(Path, sys.argv[1:])
root = home / ".mac"
guard = os.open(str(root / "deploy-controller.guard"), os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(guard, fcntl.LOCK_EX)
ready.touch()
while not refresh.exists():
    time.sleep(0.01)
owner = root / "deploy-controller.lock" / "owner.json"
payload = json.loads(owner.read_text(encoding="utf-8"))
payload["renewed_at_epoch"] = time.time()
owner.write_text(json.dumps(payload), encoding="utf-8")
fcntl.flock(guard, fcntl.LOCK_UN)
os.close(guard)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(tmp_path), str(ready), str(refresh)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for_path(ready, holder)
    takeover = subprocess.Popen(
        [sys.executable, "-c", acquire_python],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "MAC_DEPLOY_LOCK_ID": "takeover-controller",
            "MAC_DEPLOY_LOCK_TAKEOVER": "1",
            "MAC_DEPLOY_LOCK_STALE_SECONDS": "60",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.1)
    assert takeover.poll() is None, "takeover did not wait on the stable guard"
    refresh.touch()
    holder_stdout, holder_stderr = holder.communicate(timeout=10)
    assert holder.returncode == 0, holder_stdout + holder_stderr
    _stdout, stderr = takeover.communicate(timeout=10)
    assert takeover.returncode != 0
    assert "another deployment owns this node" in stderr
    assert json.loads(owner.read_text(encoding="utf-8"))["deployment_id"] == ("live-controller")
    assert not list(mac_home.glob("deploy-controller.lock.stale.*"))


def test_secret_stream_waits_for_same_session_exact_fence(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    shell_quote = (
        "shell_quote() {"
        + deploy.split("shell_quote() {", 1)[1].split("\n}\n\n# Resolve every operator-side", 1)[0]
        + "\n}"
    )
    fence_helpers = (
        "remote_deployment_fenced_exec() {"
        + deploy.split("remote_deployment_fenced_exec() {", 1)[1].split(
            "\n}\n\nfenced_remote_upload() {", 1
        )[0]
        + "\n}"
    )
    mac_home = tmp_path / ".mac"
    lock = mac_home / "deploy-controller.lock"
    lock.mkdir(parents=True)
    (lock / "owner.json").write_text(
        json.dumps({"deployment_id": "expected-controller"}), encoding="utf-8"
    )
    source = tmp_path / "secret.env"
    output = tmp_path / "received.env"
    source.write_text("TOKEN=not-a-real-secret\n", encoding="utf-8")
    snippet = f"""set -euo pipefail
PYTHON_BIN={sys.executable!s}
{shell_quote}
{fence_helpers}
remote_cmd="$(remote_deployment_fenced_exec expected-controller 1 sh -c 'cat > {output}')"
stream_file_after_remote_fence {source} MAC_DEPLOY_FENCE_READY:expected-controller sh -c "$remote_cmd"
"""
    result = subprocess.run(
        ["bash", "-c", snippet],
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == source.read_bytes()

    missing = tmp_path / "must-not-be-opened"
    wrong = f"""set -euo pipefail
PYTHON_BIN={sys.executable!s}
{shell_quote}
{fence_helpers}
remote_cmd="$(remote_deployment_fenced_exec wrong-controller 1 sh -c 'cat >/dev/null')"
stream_file_after_remote_fence {missing} MAC_DEPLOY_FENCE_READY:wrong-controller sh -c "$remote_cmd"
"""
    rejected = subprocess.run(
        ["bash", "-c", wrong],
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "did not emit the exact READY receipt" in rejected.stderr
    assert "No such file" not in rejected.stderr


def test_fenced_upload_uses_random_exclusive_atomic_materialization():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    upload = deploy.split("fenced_remote_upload() {", 1)[1].split(
        "\n}\n\nenv_value_or_empty() {", 1
    )[0]

    assert ".upload.$$" not in upload
    assert "tempfile.mkstemp" in upload
    assert "os.fchmod(fd,0o600)" in upload
    assert "os.fsync(output.fileno())" in upload
    assert "os.replace(tmp,target)" in upload
    assert "os.fsync(directory_fd)" in upload
    assert 'python3 -c "$upload_code" "$destination" "$expected_size" "$expected_sha256"' in upload
    assert "remote upload payload was truncated" in upload
    assert "remote upload payload exceeded its declared size" in upload
    assert "remote upload payload digest differs" in upload


def test_fenced_upload_rejects_a_truncated_transport_before_publication(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    shell_quote = (
        "shell_quote() {"
        + deploy.split("shell_quote() {", 1)[1].split("\n}\n\n# Resolve every operator-side", 1)[0]
        + "\n}"
    )
    helpers = (
        "remote_deployment_fenced_exec() {"
        + deploy.split("remote_deployment_fenced_exec() {", 1)[1].split(
            "\n}\n\nenv_value_or_empty() {", 1
        )[0]
        + "\n}"
    )
    mac_home = tmp_path / ".mac"
    lock = mac_home / "deploy-controller.lock"
    lock.mkdir(parents=True)
    (lock / "owner.json").write_text(
        json.dumps({"deployment_id": "expected-controller"}), encoding="utf-8"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
remote=
for value in "$@"; do remote="$value"; done
if [ "${FAKE_SSH_TRUNCATE:-0}" = 1 ]; then
  dd bs=1 count=3 2>/dev/null | /bin/bash -c "$remote"
else
  /bin/bash -c "$remote"
fi
""",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o700)
    source = tmp_path / "payload"
    source.write_bytes(b"content-bound-upload\n" * 32)
    complete = tmp_path / "complete"
    truncated = tmp_path / "truncated"
    snippet = f"""set -euo pipefail
PYTHON_BIN={sys.executable!s}
PATH={shlex.quote(str(fake_bin))}:{shlex.quote(str(Path(sys.executable).parent))}:/usr/bin:/bin
ssh_target_args() {{ printf '%s\\0' fake-target; }}
{shell_quote}
{helpers}
fenced_remote_upload fixture expected-controller {shlex.quote(str(source))} {shlex.quote(str(complete))}
FAKE_SSH_TRUNCATE=1 fenced_remote_upload fixture expected-controller {shlex.quote(str(source))} {shlex.quote(str(truncated))}
"""
    result = subprocess.run(
        ["bash", "-c", snippet],
        env={**os.environ, "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert complete.read_bytes() == source.read_bytes()
    assert not truncated.exists()
    assert not list(tmp_path.glob(".truncated.upload.*"))


def test_fenced_stream_deadline_covers_ready_upload_output_and_child_reap(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    stream = (
        "stream_file_after_remote_fence() {"
        + deploy.split("stream_file_after_remote_fence() {", 1)[1].split(
            "\n}\n\nfenced_remote_upload() {", 1
        )[0]
        + "\n}"
    )
    source = tmp_path / "payload"
    source.write_text("not-a-secret\n", encoding="utf-8")
    snippet = f"""set -euo pipefail
PYTHON_BIN={sys.executable!s}
MAC_DEPLOY_REMOTE_PHASE_TIMEOUT_SECONDS=0.2
{stream}
stream_file_after_remote_fence {source} READY sh -c 'printf "READY\\n"; sleep 10'
"""
    result = subprocess.run(
        ["bash", "-c", snippet],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert result.returncode != 0
    assert "monotonic deadline" in result.stderr

    missing = tmp_path / "must-not-open-before-complete-ready"
    partial = f"""set -euo pipefail
PYTHON_BIN={sys.executable!s}
MAC_DEPLOY_REMOTE_PHASE_TIMEOUT_SECONDS=0.2
{stream}
stream_file_after_remote_fence {missing} READY sh -c 'printf REA; sleep 10'
"""
    rejected = subprocess.run(
        ["bash", "-c", partial],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert rejected.returncode != 0
    assert "monotonic deadline" in rejected.stderr
    assert "No such file" not in rejected.stderr


def test_all_deploy_credentials_use_the_fenced_stdin_secret_channel():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    secret_names = (
        "MAC_DEPLOY_GITHUB_REVIEW_KEY_B64",
        "MAC_OPENCLAW_UPGRADE_TOKEN",
        "NVIDIA_IMAGE_API_KEY",
        "NVIDIA_AUDIO_API_KEY",
        "NVIDIA_VIDEO_API_KEY",
        "NVIDIA_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "PERPLEXITY_API_KEY",
    )
    for name in secret_names:
        assert f"add_remote_env {name} " not in deploy
        assert f"add_remote_secret_env {name} " in deploy
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "base64.b64decode(sys.argv[2])" not in node
    deploy_host = deploy.split("deploy_host() {", 1)[1].split("hub_target() {", 1)[0]
    assert "mac-node-install-${agent}-${TS}.env" not in deploy_host
    assert "_mac_secret_file" not in deploy_host
    assert ". /dev/stdin" in deploy_host
    assert 'local_secret_payload="$TMPDIR_LOCAL/node-secrets-${agent}.env"' in deploy_host
    assert "stream_file_after_remote_fence" in deploy_host
    assert (
        "remote_secret_env"
        not in deploy_host.split("remote_cmd=", 1)[1].split("local_secret_payload=", 1)[0]
    )

    direct = subprocess.run(
        [
            "sh",
            "-c",
            'set -eu; set -a; . /dev/stdin; set +a; test "$TOKEN" = expected',
        ],
        input="TOKEN=expected\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert direct.returncode == 0, direct.stderr


def test_control_master_and_fenced_streams_are_required_before_phase_one():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    main = deploy.split("main() {", 1)[1]
    masters = main.index('start_ssh_control_master "$hub_agent"')
    required = main.index("SSH_CONTROL_REQUIRED=1", masters)
    remote_secret_read = main.index('hub_token="$(read_hub_token)"', required)
    route_binding = main.index("bind_live_cohort_routes", required)
    typed = main.index("run_typed_cohort", route_binding)
    assert masters < required < remote_secret_read < route_binding < typed
    assert "-o ControlMaster=yes -o ControlPersist=no" in deploy
    pinned = deploy.split("pinned_fleet_route_args() {", 1)[1].split(
        "\n}\n\nssh_target_args() {", 1
    )[0]
    assert "pinned SSH control socket is unavailable" in pinned
    assert pinned.index("pinned SSH control socket is unavailable") < pinned.index(
        "printf '%s\\0' -S \"$control_path\""
    )
    assert "ProxyCommand=/usr/bin/false" in pinned
    assert "printf '%s\\0' -S \"$control_path\" -O proxy" not in pinned
    assert "printf '%s\\0' -F /dev/null" in pinned
    assert "return 1" not in pinned
    assert "scp -O" not in deploy
    assert "MAC_DEPLOY_FENCE_READY:" in deploy
    assert 'SSH_SESSION_MODE_RAW="${MAC_DEPLOY_SSH_SESSION_MODE:-direct}"' in deploy
    assert "frozen direct SSH route is unavailable" in pinned
    direct_probe = deploy.split("probe_direct_ssh_route() {", 1)[1].split(
        "\n}\n\nstart_ssh_control_master() {", 1
    )[0]
    assert '["ssh", "-vv", "-n", *route, "true"]' in direct_probe
    assert "process.wait(timeout=30)" in direct_probe
    direct_start = deploy.split("start_ssh_control_master() {", 1)[1].split(
        "\n}\n\npinned_fleet_route_args() {", 1
    )[0]
    assert 'probe_direct_ssh_route "$agent" "$log_path" "${route_parts[@]}"' in direct_start
    assert "-o ConnectionAttempts=3" in direct_start
    assert "-o ServerAliveInterval=15" in direct_start
    assert "-o ServerAliveCountMax=2" in direct_start
    credential = deploy.split("provision_bound_worker_credential() (", 1)[1].split(
        "\n)\n\nfinalize_remote_deployment_release()", 1
    )[0]
    assert "fenced_remote_upload" in credential
    assert "worker credential manifest" in credential


def test_pinned_route_executes_a_normal_mux_session_and_has_no_network_fallback(
    tmp_path,
):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    pinned = (
        "pinned_fleet_route_args() {"
        + deploy.split("pinned_fleet_route_args() {", 1)[1].split("\n}\n\nssh_target_args() {", 1)[
            0
        ]
        + "\n}"
    )
    route_path = tmp_path / "pinned.route"
    route_path.write_bytes(b"-F\0/dev/null\0-J\0jump@example\0-i\0/tmp/key\0user@target\0")
    snippet = (
        "set -euo pipefail\n"
        "SSH_CONTROL_REQUIRED=1\n"
        "SSH_SESSION_MODE=multiplex\n"
        "fleet_ssh_route_args() { "
        "printf '%s\\0' -F /dev/null -J jump@example -i /tmp/key user@target; }\n"
        "ssh_control_path_for_agent() { printf '%s\\n' /tmp/pinned.sock; }\n"
        f"ssh_pinned_route_file_for_agent() {{ printf '%s\\n' {shlex.quote(str(route_path))}; }}\n"
        + pinned
        + "\npinned_fleet_route_args worker\n"
    )
    result = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.split(b"\0")[:-1] == [
        b"-F",
        b"/dev/null",
        b"-S",
        b"/tmp/pinned.sock",
        b"-o",
        b"ControlMaster=no",
        b"-o",
        b"ControlPersist=no",
        b"-o",
        b"ProxyCommand=/usr/bin/false",
        b"user@target",
    ]
    assert b"pinned SSH control socket is unavailable" in result.stderr


def test_direct_ssh_mode_reuses_a_frozen_route_without_a_control_socket(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    pinned = (
        "pinned_fleet_route_args() {"
        + deploy.split("pinned_fleet_route_args() {", 1)[1].split("\n}\n\nssh_target_args() {", 1)[
            0
        ]
        + "\n}"
    )
    route_path = tmp_path / "pinned.route"
    route_path.write_bytes(b"-F\0/dev/null\0-o\0StrictHostKeyChecking=yes\0user@target\0")
    snippet = (
        "set -euo pipefail\n"
        "SSH_CONTROL_REQUIRED=1\n"
        "SSH_SESSION_MODE=direct\n"
        "fleet_ssh_route_args() { exit 99; }\n"
        "ssh_control_path_for_agent() { printf '%s\\n' /tmp/unused.sock; }\n"
        f"ssh_pinned_route_file_for_agent() {{ printf '%s\\n' {shlex.quote(str(route_path))}; }}\n"
        + pinned
        + "\npinned_fleet_route_args worker\n"
    )
    result = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.split(b"\0")[:-1] == [
        b"-F",
        b"/dev/null",
        b"-o",
        b"StrictHostKeyChecking=yes",
        b"user@target",
    ]


def _ssh_failure_report_functions():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    classify = (
        "ssh_transcript_failure_reason() {"
        + deploy.split("ssh_transcript_failure_reason() {", 1)[1].split(
            "\n}\n\n# Report a failed SSH route", 1
        )[0]
        + "\n}"
    )
    report = (
        "report_ssh_route_failure() {"
        + deploy.split("report_ssh_route_failure() {", 1)[1].split(
            "\n}\n\nprobe_direct_ssh_route() {", 1
        )[0]
        + "\n}"
    )
    return classify + "\n" + report


UNTRUSTED_HOST_KEY_TRANSCRIPT = (
    "debug1: Reading configuration data /dev/null\n"
    + "debug1: identity file /root/.ssh/id_rsa type -1\n" * 24
    + "debug1: Server host key: ssh-ed25519 SHA256:AAAA\n"
    "No ED25519 host key is known for 127.0.0.1 and you have requested "
    "strict checking.\n"
    "Host key verification failed.\n"
)


@pytest.mark.parametrize(
    "transcript,status,expected_reason",
    [
        (UNTRUSTED_HOST_KEY_TRANSCRIPT, 255, "host-key-untrusted"),
        (
            "debug1: connect\n@@@ WARNING @@@\n"
            "REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
            "Host key verification failed.\n",
            255,
            "host-key-changed",
        ),
        (
            "debug1: Server host key: ssh-ed25519 SHA256:AAAA\n"
            "horde@127.0.0.1: Permission denied (publickey).\n",
            255,
            "auth-rejected",
        ),
        (
            "debug1: Connecting to 127.0.0.1 port 22.\n"
            "ssh: connect to host 127.0.0.1 port 22: Connection refused\n",
            255,
            "unreachable",
        ),
        ("", 124, "timeout"),
        ("debug1: nothing interesting happened\n", 255, "unknown"),
        ("", 255, "no-transcript"),
    ],
    ids=[
        "host-key-untrusted",
        "host-key-changed",
        "auth-rejected",
        "unreachable",
        "timeout",
        "unknown",
        "no-transcript",
    ],
)
def test_ssh_transcript_failure_reason_names_the_actual_cause(
    tmp_path, transcript, status, expected_reason
):
    log_path = tmp_path / "probe.log"
    log_path.write_text(transcript, encoding="utf-8")
    snippet = (
        "set -euo pipefail\n"
        + _ssh_failure_report_functions()
        + f"\nssh_transcript_failure_reason {shlex.quote(str(log_path))} {status}\n"
    )
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    reason, summary, remediation = result.stdout.strip().split("|")
    assert reason == expected_reason
    assert summary and remediation
    if expected_reason == "host-key-untrusted":
        assert "ssh-keyscan" in remediation


def test_ssh_route_failure_report_prints_the_tail_that_names_the_cause(tmp_path):
    log_path = tmp_path / "probe.log"
    log_path.write_text(UNTRUSTED_HOST_KEY_TRANSCRIPT, encoding="utf-8")
    snippet = (
        "set -euo pipefail\n"
        + _ssh_failure_report_functions()
        + "\nreport_ssh_route_failure hazel "
        + shlex.quote(str(log_path))
        + " 255 'could not establish bounded direct SSH route (ssh exit 255)'"
        + " horde@127.0.0.1\n"
    )
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    # The generic headline stays, but it now carries the classified cause, the
    # route it applies to, a remediation, and the end of the transcript where
    # OpenSSH actually prints the failure.
    assert "could not establish bounded direct SSH route" in result.stderr
    assert "host-key-untrusted" in result.stderr
    assert "route target: horde@127.0.0.1" in result.stderr
    assert "ssh-keyscan" in result.stderr
    assert "Host key verification failed." in result.stderr


def _run_direct_ssh_probe(tmp_path, fake_ssh_body):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    probe = (
        "probe_direct_ssh_route() {"
        + deploy.split("probe_direct_ssh_route() {", 1)[1].split(
            "\n}\n\nstart_ssh_control_master() {", 1
        )[0]
        + "\n}"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(fake_ssh_body, encoding="utf-8")
    fake_ssh.chmod(0o700)
    log_path = tmp_path / "probe.log"
    snippet = (
        "set -euo pipefail\n"
        f"PYTHON_BIN={shlex.quote(sys.executable)}\n"
        f"PATH={shlex.quote(str(fake_bin))}:/usr/bin:/bin\n"
        + _ssh_failure_report_functions()
        + "\n"
        + probe
        + f"\nprobe_direct_ssh_route hazel {shlex.quote(str(log_path))}"
        " -F /dev/null -o StrictHostKeyChecking=yes horde@127.0.0.1\n"
    )
    result = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=False)
    return result, log_path


def test_direct_ssh_probe_failure_reports_the_untrusted_host_key(tmp_path):
    result, log_path = _run_direct_ssh_probe(
        tmp_path,
        "#!/usr/bin/env bash\n"
        "for _ in $(seq 1 24); do\n"
        "  echo 'debug1: identity file /root/.ssh/id_rsa type -1' >&2\n"
        "done\n"
        "echo 'No ED25519 host key is known for 127.0.0.1 and you have "
        "requested strict checking.' >&2\n"
        "echo 'Host key verification failed.' >&2\n"
        "exit 255\n",
    )
    assert result.returncode == 1
    assert "host-key-untrusted" in result.stderr
    assert "ssh exit 255" in result.stderr
    assert "ssh-keyscan" in result.stderr
    # Regression: the old report printed only the first 20 lines, which on a
    # -vv transcript are identity-file probes, so the real cause was invisible.
    assert "Host key verification failed." in result.stderr
    # The full transcript stays on disk; the host-key fingerprint parser reads it.
    assert "identity file" in log_path.read_text(encoding="utf-8")


def test_direct_ssh_probe_succeeds_quietly_and_locks_down_the_transcript(tmp_path):
    result, log_path = _run_direct_ssh_probe(
        tmp_path,
        "#!/usr/bin/env bash\necho 'debug1: Authenticated to fixture' >&2\nexit 0\n",
    )
    assert result.returncode == 0, result.stderr
    assert "ERROR:" not in result.stderr
    assert log_path.stat().st_mode & 0o077 == 0


def _run_live_ssh_host_key_parser(tmp_path, transcript):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    function = (
        "live_ssh_host_key_fingerprint() {"
        + deploy.split("live_ssh_host_key_fingerprint() {", 1)[1].split(
            "\n}\n\nlive_machine_instance_id() {", 1
        )[0]
        + "\n}"
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    control_path = tmp_path / "control"
    control_path.with_suffix(".log").write_bytes(transcript)
    snippet = f"""set -euo pipefail
ssh_control_path_for_agent() {{ printf '%s\n' {shlex.quote(str(control_path))}; }}
{function}
live_ssh_host_key_fingerprint fixture
"""
    return subprocess.run(
        ["bash", "-c", snippet],
        check=False,
        capture_output=True,
    )


def test_live_ssh_host_key_parser_selects_exact_crlf_target_and_fails_closed(
    tmp_path,
):
    first = b"SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    second = b"SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    valid = _run_live_ssh_host_key_parser(
        tmp_path / "valid",
        b"debug1: Connecting to fixture\r\n"
        + b"debug1: Server host key: ssh-ed25519 "
        + first
        + b"\r\n"
        + b"debug1: Executing proxy command\r\n"
        + b"debug1: Server host key: ssh-ed25519 "
        + second
        + b"\r\n"
        + b"debug1: Authentication succeeded\r\n",
    )
    assert valid.returncode == 0, valid.stderr.decode()
    assert valid.stdout == second + b"\n"

    direct_lf = _run_live_ssh_host_key_parser(
        tmp_path / "direct-lf",
        b"debug1: Server host key: ssh-ed25519 " + first + b"\n",
    )
    assert direct_lf.returncode == 0, direct_lf.stderr.decode()
    assert direct_lf.stdout == first + b"\n"

    missing = _run_live_ssh_host_key_parser(
        tmp_path / "missing",
        b"debug1: Connecting to fixture\r\n",
    )
    assert missing.returncode != 0
    assert missing.stdout == b""
    assert b"unavailable or malformed" in missing.stderr

    malformed_final = _run_live_ssh_host_key_parser(
        tmp_path / "malformed-final",
        b"debug1: Server host key: ssh-ed25519 "
        + first
        + b"\r\n"
        + b"debug1: Server host key: ssh-ed25519 not-a-sha256\r\n",
    )
    assert malformed_final.returncode != 0
    assert malformed_final.stdout == b""
    assert b"unavailable or malformed" in malformed_final.stderr

    malformed_jump = _run_live_ssh_host_key_parser(
        tmp_path / "malformed-jump",
        b"debug1: Server host key: ssh-ed25519 not-a-sha256\r\n"
        + b"debug1: Server host key: ssh-ed25519 "
        + second
        + b"\r\n",
    )
    assert malformed_jump.returncode != 0
    assert malformed_jump.stdout == b""
    assert b"unavailable or malformed" in malformed_jump.stderr

    embedded_carriage_return = _run_live_ssh_host_key_parser(
        tmp_path / "embedded-carriage-return",
        b"debug1: Server host key: ssh-ed25519 "
        + first
        + b"\r\n"
        + b"debug1: Server host key: ssh-ed25519 "
        + second
        + b"\r\r\n",
    )
    assert embedded_carriage_return.returncode != 0
    assert embedded_carriage_return.stdout == b""
    assert b"unavailable or malformed" in embedded_carriage_return.stderr


def test_required_openshell_image_is_rejected_before_any_remote_fleet_action():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    normalize = (
        "normalize_boolean_token() {"
        + deploy.split("normalize_boolean_token() {", 1)[1].split(
            "\n}\n\nresolve_python_bin() {", 1
        )[0]
        + "\n}"
    )
    preflight = (
        "validate_openshell_runtime_image_spec() {"
        + deploy.split("validate_openshell_runtime_image_spec() {", 1)[1].split(
            "\n}\n\ndeploy_host() {", 1
        )[0]
        + "\n}"
    )
    fields = [""] * 55
    fields[0] = "worker1"
    fields[53] = "true"
    spec = "|".join(fields)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"set -euo pipefail\n{normalize}\n{preflight}\n"
            'validate_openshell_runtime_image_spec "$1"',
            "preflight",
            spec,
        ],
        env={
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE",
                "MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD",
            }
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "before fleet phase 1" in result.stderr

    main = deploy.split("main() {", 1)[1]
    local_preflight = main.index('validate_openshell_runtime_image_spec "$spec"')
    first_remote = main.index('start_ssh_control_master "$hub_agent"')
    first_epoch = main.index("run_typed_cohort")
    assert local_preflight < first_remote < first_epoch


def test_reverse_tunnel_manager_mutation_holds_the_exact_hub_fence():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    tunnel = deploy.split("install_reverse_tunnel_on_hub() {", 1)[1].split(
        "\n}\n\nuses_direct_mesh_hub() {", 1
    )[0]

    deployment_id = tunnel.index('hub_deployment_id="$(deployment_id_for_agent "$hub_agent")"')
    assertion = tunnel.index("assert_remote_deployment_lock", deployment_id)
    temporary_acquire = tunnel.index("acquire_remote_deployment_lock", assertion)
    lifecycle_upload = tunnel.index("fenced_remote_upload", temporary_acquire)
    fence = tunnel.index(
        'remote_deployment_fenced_exec "$hub_deployment_id" 0 bash -s',
        lifecycle_upload,
    )
    hub_script = tunnel.index("<<'HUBSCRIPT'", fence)
    lifecycle_cleanup = tunnel.index("cleanup_fence=", hub_script)
    temporary_release = tunnel.index("release_remote_deployment_lock", hub_script)
    assert deployment_id < assertion < temporary_acquire < lifecycle_upload
    assert lifecycle_upload < fence < hub_script < lifecycle_cleanup < temporary_release
    assert 'TUNNEL_FLEET_NAME=$(shell_quote "$fleet_name_local")' in tunnel
    assert 'TUNNEL_LAUNCHD_LIFECYCLE=$(shell_quote "$remote_launchd_lifecycle")' in tunnel


def test_reverse_tunnel_definitions_adopt_only_exact_legacy_mac_shape(tmp_path):
    if shutil.which("ssh") is None:
        pytest.skip("ssh(1) is required to render the reverse-tunnel command line")
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    hub_script = deploy.split("<<'HUBSCRIPT'\n", 1)[1].split("\nHUBSCRIPT", 1)[0]
    helpers = (
        "managed_marker="
        + hub_script.split("managed_marker=", 1)[1].split(
            'if [ "$(uname -s)" = "Darwin" ]; then', 1
        )[0]
    )
    user = subprocess.check_output(["whoami"], text=True).strip()
    legacy = tmp_path / "legacy.conf"
    forwards = (
        "127.0.0.1:18789:127.0.0.1:8789",
        "127.0.0.1:18090:127.0.0.1:8090",
        "127.0.0.1:16333:127.0.0.1:6333",
        "127.0.0.1:13002:127.0.0.1:3002",
    )
    forward_args = " ".join(f"-R {value}" for value in forwards)
    legacy.write_text(
        "\n".join(
            (
                "[program:mac-tunnel-worker1]",
                "command=ssh -N -o BatchMode=yes -o StrictHostKeyChecking=no "
                "-o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 "
                "-o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes "
                f"-i {tmp_path}/.ssh/mac_tunnel_id {forward_args} old@old-host",
                f"directory={tmp_path}",
                f"user={user}",
                "autostart=true",
                "autorestart=true",
                "startsecs=5",
                "startretries=1000",
                "stopwaitsecs=10",
                f"stdout_logfile={tmp_path}/.mac/logs/tunnel-worker1.log",
                f"stderr_logfile={tmp_path}/.mac/logs/tunnel-worker1.log",
                "",
            )
        ),
        encoding="utf-8",
    )
    setup = f"""set -euo pipefail
fleet_name=mac
worker_agent=worker1
managed_marker=mac.managed-reverse-tunnel.v1:mac:worker1
definition_tmp=""
sudo() {{ [ "${{1:-}}" != -n ] || shift; "$@"; }}
{helpers}
"""
    ssh_bin = subprocess.check_output(["bash", "-c", "command -v ssh"], text=True).strip()

    def ssh_arguments(binary: str) -> list[str]:
        args = [
            binary,
            "-N",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ExitOnForwardFailure=yes",
            "-i",
            f"{tmp_path}/.ssh/mac_tunnel_id",
        ]
        for forward in forwards:
            args.extend(("-R", forward))
        return [*args, "old@old-host"]

    launchd_legacy = tmp_path / "legacy.plist"
    launchd_legacy.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.mac.tunnel-worker1",
                "UserName": user,
                "EnvironmentVariables": {"HOME": str(tmp_path)},
                "ProgramArguments": ssh_arguments(ssh_bin),
                "KeepAlive": True,
                "RunAtLoad": True,
                "ThrottleInterval": 5,
                "StandardOutPath": f"{tmp_path}/.mac/logs/tunnel-worker1.log",
                "StandardErrorPath": f"{tmp_path}/.mac/logs/tunnel-worker1.log",
            }
        )
    )
    systemd_legacy = tmp_path / "legacy.service"
    systemd_legacy.write_text(
        "\n".join(
            (
                "[Unit]",
                "Description=mac reverse tunnel for worker1",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"User={user}",
                f"WorkingDirectory={tmp_path}",
                "ExecStart=" + " ".join(ssh_arguments(ssh_bin)),
                "Restart=always",
                "RestartSec=5",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            )
        ),
        encoding="utf-8",
    )
    for path, marker, kind in (
        (
            launchd_legacy,
            "<!-- mac.managed-reverse-tunnel.v1:mac:worker1 -->",
            "launchd",
        ),
        (systemd_legacy, "# mac.managed-reverse-tunnel.v1:mac:worker1", "systemd"),
        (legacy, "; mac.managed-reverse-tunnel.v1:mac:worker1", "supervisor"),
    ):
        adopted = subprocess.run(
            [
                "bash",
                "-c",
                setup + 'assert_managed_definition_or_absent "$1" "$2" "$3"',
                "legacy-adoption",
                str(path),
                marker,
                kind,
            ],
            env={**os.environ, "HOME": str(tmp_path)},
            check=False,
            capture_output=True,
            text=True,
        )
        assert adopted.returncode == 0, f"{kind}: {adopted.stderr}"

    for kind, marker, original in (
        (
            "launchd",
            "<!-- mac.managed-reverse-tunnel.v1:mac:worker1 -->",
            b"not a plist\n",
        ),
        (
            "systemd",
            "# mac.managed-reverse-tunnel.v1:mac:worker1",
            b"[Service]\nExecStart=/operator\n",
        ),
        (
            "supervisor",
            "; mac.managed-reverse-tunnel.v1:mac:worker1",
            b"[program:mac-tunnel-worker1]\ncommand=/operator\n",
        ),
    ):
        collision = tmp_path / f"collision-{kind}"
        collision.write_bytes(original)
        rejected = subprocess.run(
            [
                "bash",
                "-c",
                setup
                + 'assert_managed_definition_or_absent "$1" "$2" "$3"\n'
                + 'printf overwritten > "$1"',
                "legacy-rejection",
                str(collision),
                marker,
                kind,
            ],
            env={**os.environ, "HOME": str(tmp_path)},
            check=False,
            capture_output=True,
            text=True,
        )
        assert rejected.returncode != 0
        assert "refusing to replace unowned" in rejected.stderr
        assert collision.read_bytes() == original


def test_reverse_tunnel_manager_identity_checks_precede_atomic_replacement():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    controller = deploy.split("install_reverse_tunnel_on_hub() {", 1)[1].split(
        "uses_direct_mesh_hub() {", 1
    )[0]
    hub_script = deploy.split("<<'HUBSCRIPT'\n", 1)[1].split("\nHUBSCRIPT", 1)[0]
    assert "mac.managed-reverse-tunnel.v1:" in hub_script
    assert 'run_root_noninteractive test -L "$path"' in hub_script
    upload = controller.index("fenced_remote_upload")
    assert '"$hub_agent" "$hub_deployment_id"' in controller[upload:]
    assert "TUNNEL_LAUNCHD_LIFECYCLE=" in controller
    assert "could not remove temporary launchd lifecycle contract" in controller

    launchd = hub_script.split('if [ "$(uname -s)" = "Darwin" ]; then', 1)[1].split(
        "\nfi\nif command -v systemctl", 1
    )[0]
    source = launchd.index('. "$launchd_lifecycle"')
    preflight = launchd.index('assert_managed_definition_or_absent "$plist"')
    begin = launchd.index("mac_launchd_transaction_begin")
    track = launchd.index("mac_launchd_transaction_track_temporary", begin)
    mark = launchd.index("mac_launchd_transaction_mark_mutating", track)
    stop = launchd.index("mac_launchd_stop_job_if_present", mark)
    recheck = launchd.index('assert_managed_definition_or_absent "$plist"', stop)
    replace = launchd.index("mac_launchd_transaction_replace", recheck)
    bootstrap = launchd.index("mac_launchd_bootstrap_job", replace)
    identity = launchd.index("launchd_state_has_managed_identity managed", bootstrap)
    commit = launchd.index("mac_launchd_transaction_commit", identity)
    assert source < preflight < begin < track < mark < stop
    assert stop < recheck < replace < bootstrap < identity < commit
    assert 'one("\\tpath = ") != plist' in launchd
    assert 'lines[0] != f"system/{label} = {{"' in launchd
    assert "args[:-1] != expected" in launchd
    assert "MAC_MANAGED_REVERSE_TUNNEL => {marker}" in launchd
    assert "launchd_loaded_legacy=1" in launchd
    assert "mac_launchd_run_control_bounded system" in launchd
    assert 'system "$plist" "system/${label}" "$label" system' in launchd
    assert '"$definition_tmp" "$plist" 0644 0 0' in launchd
    assert "launchctl " not in launchd
    assert "$SECONDS" not in launchd
    assert "deadline=$(( SECONDS" not in launchd
    assert launchd.count('*) exit "$launchd_probe_rc"') == 2

    systemd = hub_script.split("if command -v systemctl >/dev/null 2>&1", 1)[1].split(
        "\nfi\nconf_dir=", 1
    )[0]
    assert 'linux_root_bounded systemctl "$@"' in systemd
    assert "sudo systemctl" not in systemd
    assert 'systemctl "$@" ||' not in systemd
    capture = systemd.index('SYSTEMD_PRIOR_LOAD_STATE="$SYSTEMD_LOAD_STATE"')
    begin = systemd.index('linux_service_tx_begin "$unit"', capture)
    mark = systemd.index("linux_service_tx_mark_mutating", begin)
    stop = systemd.index("systemd_stop_current", mark)
    recheck = systemd.index('assert_managed_definition_or_absent "$unit"', stop)
    snapshot = systemd.index("linux_service_tx_verify_snapshot_current", recheck)
    replace = systemd.index('linux_service_tx_replace "$definition_tmp" "$unit"', snapshot)
    reload = systemd.index("systemd_control daemon-reload", replace)
    enable = systemd.index("systemd_control enable", reload)
    identity = systemd.index("systemd_validate_identity", enable)
    start = systemd.index("systemd_control start", identity)
    proof = systemd.index("systemd_prove_running_or_retrying", start)
    commit = systemd.index("linux_service_tx_commit", proof)
    assert capture < begin < mark < stop < recheck < snapshot < replace
    assert replace < reload < enable < identity < start < proof < commit
    rollback = systemd.split("systemd_restore_previous_generation() {", 1)[1].split("\n  }", 1)[0]
    assert "systemd_stop_current" in rollback
    assert "linux_service_tx_restore_artifact" in rollback
    assert 'case "$SYSTEMD_PRIOR_ENABLEMENT"' in rollback
    assert "systemd_prove_running_or_retrying" in rollback
    assert "loaded:activating:auto-restart:[0-9]*" in systemd

    supervisor = hub_script.split("\nfi\nconf_dir=", 1)[1]
    assert 'linux_root_bounded supervisorctl "$@"' in supervisor
    assert "sudo supervisorctl" not in supervisor
    assert 'supervisorctl "$@" >/dev/null 2>&1 ||' not in supervisor
    assert supervisor.count('assert_no_duplicate_supervisor_program "$conf"') >= 2
    assert "refusing same-name supervisor program from another include" in supervisor
    capture = supervisor.index('SUPERVISOR_PRIOR_STATE="$SUPERVISOR_STATE"')
    begin = supervisor.index('linux_service_tx_begin "$conf"', capture)
    mark = supervisor.index("linux_service_tx_mark_mutating", begin)
    quiesce = supervisor.index("supervisor_quiesce_current", mark)
    recheck = supervisor.index('assert_managed_definition_or_absent "$conf"', quiesce)
    snapshot = supervisor.index("linux_service_tx_verify_snapshot_current", recheck)
    replace = supervisor.index('linux_service_tx_replace "$definition_tmp" "$conf"', snapshot)
    reread = supervisor.index("supervisor_control reread", replace)
    update = supervisor.index("supervisor_control update", reread)
    proof = supervisor.index("supervisor_prove_running_or_retrying", update)
    commit = supervisor.index("linux_service_tx_commit", proof)
    assert capture < begin < mark < quiesce < recheck < snapshot < replace
    assert replace < reread < update < proof < commit
    rollback = supervisor.split("supervisor_restore_previous_generation() {", 1)[1].split("\n}", 1)[
        0
    ]
    assert "supervisor_quiesce_current" in rollback
    assert "linux_service_tx_restore_artifact" in rollback
    assert "supervisor_control reread" in rollback
    assert "supervisor_control update" in rollback
    assert "supervisor_prove_running_or_retrying" in rollback

    generic = hub_script.split("MAC_LINUX_SERVICE_TX_ACTIVE=0", 1)[1].split(
        "if command -v systemctl", 1
    )[0]
    assert "mac_launchd_snapshot_file" in generic
    assert "mac_launchd_atomic_replace" in generic
    assert "mac_launchd_atomic_restore" in generic
    assert "linux_service_tx_verify_snapshot_current" in generic
    assert "linux_service_tx_on_exit" in generic
    assert '"$MAC_LINUX_SERVICE_TX_ROLLBACK_HOOK"' in generic
    assert "$SECONDS" not in generic


def test_linux_reverse_tunnel_transaction_compensates_and_chains_exit_cleanup(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    hub_script = deploy.split("<<'HUBSCRIPT'\n", 1)[1].split("\nHUBSCRIPT", 1)[0]
    transaction = (
        "MAC_LINUX_SERVICE_TX_ACTIVE=0"
        + hub_script.split("MAC_LINUX_SERVICE_TX_ACTIVE=0", 1)[1].split(
            "\nif command -v systemctl", 1
        )[0]
    )

    artifact = tmp_path / "synthetic.service"
    artifact.write_text("prior-generation\n", encoding="utf-8")
    artifact.chmod(0o640)
    rollback_receipt = tmp_path / "rollback-receipt"
    cleanup_receipt = tmp_path / "cleanup-receipt"
    harness = f"""set -euo pipefail
. "$1"
linux_root_mode=user
linux_root_bounded() {{ mac_run_bounded 2 "$@"; }}
definition_tmp=""
rollback_receipt="$3"
cleanup_receipt="$4"
cleanup_definition_tmp() {{ printf chained > "$cleanup_receipt"; }}
trap cleanup_definition_tmp EXIT
{transaction}
restore_synthetic() {{
  linux_service_tx_restore_artifact
  printf compensated > "$rollback_receipt"
}}
linux_service_tx_begin "$2" synthetic restore_synthetic
printf new-generation > "$2.stage"
chmod 0600 "$2.stage"
linux_service_tx_mark_mutating
linux_service_tx_replace "$2.stage" "$2"
false
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            harness,
            "transaction",
            str(LAUNCHD_LIFECYCLE_SCRIPT),
            str(artifact),
            str(rollback_receipt),
            str(cleanup_receipt),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert artifact.read_text(encoding="utf-8") == "prior-generation\n"
    assert artifact.stat().st_mode & 0o777 == 0o640
    assert rollback_receipt.read_text(encoding="utf-8") == "compensated"
    assert cleanup_receipt.read_text(encoding="utf-8") == "chained"
    assert not list(tmp_path.glob(".synthetic.rollback.*"))


def test_launchd_interrupted_adoption_is_exact_and_retryable(tmp_path):
    if shutil.which("ssh") is None:
        pytest.skip("ssh(1) is required to render the launchd tunnel command line")
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    hub_script = deploy.split("<<'HUBSCRIPT'\n", 1)[1].split("\nHUBSCRIPT", 1)[0]
    state_function = (
        "launchd_state_has_managed_identity() {"
        + hub_script.split("launchd_state_has_managed_identity() {", 1)[1].split(
            "\n  }\n  load_launchd_job_state() {", 1
        )[0]
        + "\n}"
    )
    ssh_bin = subprocess.check_output(["bash", "-c", "command -v ssh"], text=True).strip()
    plist = tmp_path / "com.mac.tunnel-worker1.plist"
    marker = "mac.managed-reverse-tunnel.v1:mac:worker1"
    marker_line = f"<!-- {marker} -->"
    plist.write_text(marker_line + "\n", encoding="utf-8")
    args = [
        ssh_bin,
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ExitOnForwardFailure=yes",
        "-i",
        f"{tmp_path}/.ssh/mac_tunnel_id",
        "-R",
        "127.0.0.1:18789:127.0.0.1:8789",
        "-R",
        "127.0.0.1:18090:127.0.0.1:8090",
        "-R",
        "127.0.0.1:16333:127.0.0.1:6333",
        "-R",
        "127.0.0.1:13002:127.0.0.1:3002",
        "old@old-host",
    ]

    def launchd_state(path: Path, *, marked: bool) -> str:
        lines = [
            "system/com.mac.tunnel-worker1 = {",
            f"\tpath = {path}",
            f"\tprogram = {ssh_bin}",
            "\targuments = {",
            *(f"\t\t{arg}" for arg in args),
            "\t}",
            "\tenvironment = {",
        ]
        if marked:
            lines.append(f"\t\tMAC_MANAGED_REVERSE_TUNNEL => {marker}")
        lines.extend(("\t}", "}"))
        return "\n".join(lines)

    setup = f"""set -euo pipefail
plist={str(plist)!r}
label=com.mac.tunnel-worker1
managed_marker={marker!r}
marker_line={marker_line!r}
ssh_bin={ssh_bin!r}
HOME={str(tmp_path)!r}
launchd_file_legacy=0
launchd_loaded_legacy=0
sudo() {{ "$@"; }}
{state_function}
"""
    interrupted = subprocess.run(
        [
            "bash",
            "-c",
            setup
            + """state="$1"
if launchd_state_has_managed_identity managed "$state"; then
  :
elif launchd_state_has_managed_identity legacy "$state"; then
  [ "$launchd_file_legacy" = 1 ] || grep -Fqx "$marker_line" "$plist"
  launchd_loaded_legacy=1
else
  exit 1
fi
[ "$launchd_loaded_legacy" = 1 ]""",
            "interrupted-adoption",
            launchd_state(plist, marked=False),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert interrupted.returncode == 0, interrupted.stderr

    managed = subprocess.run(
        [
            "bash",
            "-c",
            setup + 'launchd_state_has_managed_identity managed "$1"',
            "managed-state",
            launchd_state(plist, marked=True),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert managed.returncode == 0, managed.stderr
    wrong_path = subprocess.run(
        [
            "bash",
            "-c",
            setup + 'launchd_state_has_managed_identity legacy "$1"',
            "wrong-path",
            launchd_state(Path(str(plist) + ".backup"), marked=False),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert wrong_path.returncode != 0


def test_supervisor_duplicate_program_include_is_rejected(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    hub_script = deploy.split("<<'HUBSCRIPT'\n", 1)[1].split("\nHUBSCRIPT", 1)[0]
    duplicate_function = (
        "assert_no_duplicate_supervisor_program() {"
        + hub_script.split("assert_no_duplicate_supervisor_program() {", 1)[1].split(
            "\n}\nsupervisor_control() {", 1
        )[0]
        + "\n}"
    )
    include_a = tmp_path / "etc" / "supervisor" / "conf.d"
    include_b = tmp_path / "etc" / "supervisord.d"
    include_a.mkdir(parents=True)
    include_b.mkdir(parents=True)
    expected = include_a / "mac-tunnel-worker1.conf"
    expected.write_text("[program:mac-tunnel-worker1]\ncommand=ssh\n", encoding="utf-8")
    duplicate = include_b / "operator.conf"
    duplicate.write_text("[program:mac-tunnel-worker1]\ncommand=/operator\n", encoding="utf-8")
    snippet = f"""set -euo pipefail
program=mac-tunnel-worker1
sudo() {{ [ "${{1:-}}" != -n ] || shift; "$@"; }}
{duplicate_function}
assert_no_duplicate_supervisor_program "$1"
"""
    rejected = subprocess.run(
        ["bash", "-c", snippet, "duplicate", str(expected)],
        env={**os.environ, "MAC_SUPERVISOR_INCLUDE_ROOT": str(tmp_path)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "duplicate supervisor program definitions" in rejected.stderr
    duplicate.unlink()
    accepted = subprocess.run(
        ["bash", "-c", snippet, "single", str(expected)],
        env={**os.environ, "MAC_SUPERVISOR_INCLUDE_ROOT": str(tmp_path)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stderr


def test_deferred_release_arms_each_worker_then_uses_one_batch_commit():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    hub_gate = deploy.split("hub_agent_restart_gate() {", 1)[1].split(
        "remote_deployment_hold_state() {", 1
    )[0]
    service = deploy.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]
    credential = deploy.split("provision_bound_worker_credential() (", 1)[1].split(
        "\n)\n\nfinalize_remote_deployment_release()", 1
    )[0]
    commit = deploy.split("commit_fleet_release_epoch() {", 1)[1].split(
        "enforce_bound_worker_credentials() {", 1
    )[0]

    assert 'release_commit_mode="${8:-immediate}"' in service
    deferred = service.split('if [ "$release_commit_mode" = deferred ]; then', 1)[1]
    assert "release_gate_phase=arm" in deferred
    assert "write_release_ready_evidence" in deferred
    arm = hub_gate.split('if phase == "arm":', 1)[1].split("cleared = False", 1)[0]
    assert '"release_ready": True' in arm
    assert "dispatch-hold/release" not in arm
    release_call = credential.index(
        'set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" release',
        credential.index("activate_ok=1"),
    )
    assert (
        'keep authenticated "$principal_id" deferred'
        in credential[release_call : release_call + 240]
    )
    assert '"/agents/dispatch-hold/release-batch"' in commit
    assert '"/agents/dispatch-hold/transition-batch"' in commit
    assert '"successor_reason": successor_hold_reason' in commit
    assert '"generation": item["generation"]' in commit
    assert '"baseline_seen": item["baseline_seen"]' in commit
    assert '"principal_id": item.get("principal_id") or None' in commit
    assert '"require_authenticated": bool(item.get("require_authenticated"))' in commit
    batch_release = commit.index('"/agents/dispatch-hold/release-batch"')
    batch_transition = commit.index('"/agents/dispatch-hold/transition-batch"')
    cleanup = commit.index("finalize_remote_deployment_release", batch_release)
    assert batch_transition < cleanup
    assert batch_release < cleanup


def test_release_readiness_is_bound_to_the_newly_issued_principal():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    hub_gate = deploy.split("hub_agent_restart_gate() {", 1)[1].split(
        "remote_deployment_hold_state() {", 1
    )[0]
    credential = deploy.split("provision_bound_worker_credential() (", 1)[1].split(
        "\n)\n\nfinalize_remote_deployment_release()", 1
    )[0]
    commit = deploy.split("commit_fleet_release_epoch() {", 1)[1].split(
        "enforce_bound_worker_credentials() {", 1
    )[0]

    extract = credential.index('principal_id="$("$PYTHON_BIN" - "$local_manifest"')
    activate = credential.index(
        'activate_cmd+=" --principal-id $(shell_quote "$principal_id")"', extract
    )
    arm = credential.index('keep authenticated "$principal_id" deferred', activate)
    assert extract < activate < arm
    assert "if require_authenticated and not expected_principal_id" in hub_gate
    assert 'authenticated.get("principal_id") == expected_principal_id' in hub_gate
    assert '"principal_id": principal_id' in deploy
    assert 'authenticated.get("principal_id") == item.get("principal_id")' in commit


def test_live_hub_cas_cutover_is_explicit_and_preheld_only():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    availability = deploy.split("hub_dispatch_hold_cas_available() {", 1)[1].split(
        "hub_agent_restart_gate() {", 1
    )[0]
    hub_gate = deploy.split("hub_agent_restart_gate() {", 1)[1].split(
        "remote_deployment_hold_state() {", 1
    )[0]
    prepare = deploy.split("prepare_remote_mac_agent_deployment() {", 1)[1].split(
        "set_remote_mac_agent_service() {", 1
    )[0]

    assert 'hub_url + "/openapi.json"' in availability
    assert '"/agents/{agent_id}/dispatch-hold/acquire"' in availability
    assert '"/agents/dispatch-hold/release-batch"' in availability
    assert "MAC_DEPLOY_ALLOW_LEGACY_CAS_BOOTSTRAP" in prepare
    assert 'if [ "$agent" != "$(fleet_hub_agent)" ]' in prepare
    legacy = hub_gate.split('if phase == "legacy-bootstrap":', 1)[1].split(
        'if phase == "prepare-new":', 1
    )[0]
    assert "legacy CAS bootstrap requires the live hub agent to be pre-held" in legacy
    assert '"owns_hold": False' in legacy
    assert "/dispatch-hold/acquire" not in legacy


def test_release_health_is_worker_reported_and_startup_verdict_is_registered():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    worker = (ROOT / "src" / "mac" / "worker.py").read_text(encoding="utf-8")
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    hub_gate = deploy.split("hub_agent_restart_gate() {", 1)[1].split(
        "remote_deployment_hold_state() {", 1
    )[0]
    release = hub_gate.split('elif phase in {"arm", "release"}:', 1)[1].split(
        'elif phase == "rehold":', 1
    )[0]

    assert "release_health_ready(row, resources)" in release
    health_gate = hub_gate.split("def release_health_ready(", 1)[1].split("\ndef post_drain(", 1)[0]
    # The gate now DELEGATES rather than inlining the rule. This assertion used
    # to pin the inline spelling, which is how the deploy script kept a copy
    # that drifted: it required `startup.get("status") == "degraded"`, refusing
    # a worker whose self-test reported `passed` -- the healthiest result --
    # long after the hub accepted it. Pinning the shape of a duplicated rule
    # protects the duplication.
    assert "advisory_health_dispatch_ready(" in health_gate
    assert "from mac.agent_health import" in health_gate
    assert 'startup.get("status")' not in health_gate, (
        "release_health_ready must not compare the self-test status inline; "
        "mac.agent_health owns that rule"
    )
    assert 'api("PUT", "/agents/%s" % agent_id, {"health_status": "healthy"})' not in release
    assert '"degraded" if barrier_active else "healthy"' in worker
    assert 'registration_resources["startup_self_test"] = report' in node
    wrapper_start = node.index('if [ "${MAC_AGENT_STARTUP_SELF_TEST:-1}" != "0" ]')
    resource_read = node.index('worker_resources="${MAC_WORKER_RESOURCES:-}"', wrapper_start)
    assert wrapper_start < resource_read


def test_darwin_deploy_preserves_an_active_system_control_plane_domain():
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")

    capture = node.split("capture_darwin_launchd_prestate() {", 1)[1].split(
        "system_launchd_job_is_loaded() {", 1
    )[0]
    assert "if ! sudo -n true; then" in capture
    assert "DARWIN_SYSTEM_LAUNCHD_ACTIVE=1" in capture
    assert 'sudo -n test -r "$system_plist"' in capture
    assert "DARWIN_GUI_LAUNCHD_ACTIVE=1" in capture
    assert "DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE=1" in capture
    assert "control plane is loaded in both system and GUI launchd domains" in capture
    assert "write_rollback_script" in capture
    assert 'system_launchd_job_is_loaded "$MAC_LAUNCHD_LABEL"' in capture
    assert 'gui_launchd_job_is_loaded "$uid" "$MAC_LAUNCHD_LABEL"' in capture
    assert 'system_launchd_job_is_loaded "$DARWIN_SYSTEM_SUPERVISOR_LABEL"' in capture
    assert capture.count('*) return "$probe_rc"') == 3

    stop = node.split("stop_existing_services_for_deploy() {", 1)[1].split(
        "load_drain_api_env() {", 1
    )[0]
    assert "control_mode=system" in stop
    assert '--launchd-system-supervisor "$DARWIN_SYSTEM_SUPERVISOR_LABEL"' in stop
    assert "--launchd-system-supervisor-was-active" in stop
    assert '"$PY" "$ROLLBACK_SUPERVISOR_HELPER" quiesce' in stop
    assert '--receipt "$LOG_DIR/pre-artifact-supervisor-quiescence.json"' in stop
    assert "system_launchd_job_is_loaded" not in stop
    assert "stop_system_launchd_job_if_present" not in stop

    install = node.split("install_darwin_service() {", 1)[1].split(
        "install_darwin_openclaw_service() {", 1
    )[0]
    assert (
        'mac_launchd_transaction_begin \\\n      system "$system_plist" "system/$MAC_LAUNCHD_LABEL"'
        in install
    )
    assert 'mac_launchd_transaction_set_expected_prior_state "$expected_state"' in install
    assert 'mac_launchd_transaction_track_file "$wrapper"' in install
    assert 'mac_launchd_transaction_track_file "$plist"' in install
    assert "mac_launchd_transaction_mark_mutating" in install
    assert (
        'mac_launchd_transaction_replace \\\n      "$system_plist_staging" "$system_plist" 0644 0 0'
        in install
    )
    assert (
        'mac_launchd_bootstrap_job \\\n      system "$system_plist" "system/$MAC_LAUNCHD_LABEL"'
        in install
    )
    assert "mac_launchd_transaction_commit" in install
    assert "wait_for_local_control_plane_health" in install
    assert 'mac_launchd_bootstrap_job \\\n        system "$system_supervisor_plist"' in install
    transaction = install.index("mac_launchd_transaction_begin")
    atomic_replace = install.index(
        'mac_launchd_transaction_replace \\\n      "$system_plist_staging" "$system_plist"',
        transaction,
    )
    supervisor_active = install.index(
        'if [ "$DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE" = 1 ]; then',
        transaction,
    )
    supervisor_stop = install.index("mac_launchd_stop_job_if_present", supervisor_active)
    supervisor_stop_target = install.index(
        '"system/$DARWIN_SYSTEM_SUPERVISOR_LABEL"', supervisor_stop
    )
    supervisor_disable = install.index("darwin_disable_job", supervisor_stop_target)
    supervisor_disable_target = install.index(
        '"system/$DARWIN_SYSTEM_SUPERVISOR_LABEL"', supervisor_disable
    )
    control_stop = install.index("mac_launchd_stop_job_if_present", supervisor_disable_target)
    control_stop_target = install.index('"system/$MAC_LAUNCHD_LABEL"', control_stop)
    control_bootstrap = install.index(
        'mac_launchd_bootstrap_job \\\n      system "$system_plist"', atomic_replace
    )
    healthy = install.index("wait_for_local_control_plane_health", control_bootstrap)
    supervisor_bootstrap = install.index(
        'mac_launchd_bootstrap_job \\\n        system "$system_supervisor_plist"',
        healthy,
    )
    health_recheck = install.index("wait_for_local_control_plane_health", supervisor_bootstrap)
    committed = install.index("mac_launchd_transaction_commit", health_recheck)
    assert supervisor_active < supervisor_stop < supervisor_stop_target
    assert supervisor_stop_target < supervisor_disable < supervisor_disable_target
    assert supervisor_disable_target < control_stop < control_stop_target
    assert control_stop_target < atomic_replace
    assert atomic_replace < control_bootstrap < healthy < supervisor_bootstrap
    assert supervisor_bootstrap < health_recheck < committed
    assert "com.${FLEET_NAME}.supervisor" not in install

    capture_call = node.index("capture_darwin_launchd_prestate\n")
    pre_manifest = node.index('write_deploy_manifest "pre"', capture_call)
    service_stop = node.index("stop_existing_services_for_deploy\n", pre_manifest)
    assert capture_call < pre_manifest < service_stop


def test_darwin_rollback_restores_the_original_system_launchd_scope():
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    rollback = node.split("write_rollback_script() {", 1)[1].split(
        "backup_existing_artifacts() {", 1
    )[0]

    assert "DARWIN_SYSTEM_PLIST_BACKUP='$DARWIN_SYSTEM_PLIST_BACKUP'" in rollback
    assert "DARWIN_SYSTEM_LAUNCHD_ACTIVE='$DARWIN_SYSTEM_LAUNCHD_ACTIVE'" in rollback
    assert (
        '[ "\\$DARWIN_SYSTEM_PLIST_MUTATED" != 1 ] \\\n'
        '      || restore_file_or_remove "\\$DARWIN_SYSTEM_PLIST_BACKUP" '
        '"/Library/LaunchDaemons/\\$MAC_LAUNCHD_LABEL.plist" system' in rollback
    )
    assert 'if [ "\\$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ]; then' in rollback
    assert 'elif [ "\\$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ]; then' in rollback
    rollback_stop = rollback.index(
        "verified_contract_call \\\n"
        '  python "\\$ROLLBACK_SUPERVISOR_HELPER" '
        '"\\$ROLLBACK_SUPERVISOR_HELPER_SHA256" quiesce'
    )
    restore_source = rollback.index(
        'restore_dir_or_keep_prior "\\$SRC_BACKUP" "\\$SRC_DIR" "\\$SRC_ROLLBACK_STATE"',
        rollback_stop,
    )
    assert rollback_stop < restore_source
    assert '--launchd-system-supervisor "\\$DARWIN_SYSTEM_SUPERVISOR_LABEL"' in rollback
    assert "--launchd-control-system-plist "
    '"/Library/LaunchDaemons/\\$MAC_LAUNCHD_LABEL.plist"' in rollback
    system_restore = rollback.index(
        'restore_file_or_remove "\\$DARWIN_SYSTEM_PLIST_BACKUP" '
        '"/Library/LaunchDaemons/\\$MAC_LAUNCHD_LABEL.plist" system',
        restore_source,
    )
    supervisor_restore = rollback.index(
        "verified_contract_call \\\n"
        '  python "\\$ROLLBACK_SUPERVISOR_HELPER" '
        '"\\$ROLLBACK_SUPERVISOR_HELPER_SHA256" restore',
        system_restore,
    )
    assert restore_source < system_restore < supervisor_restore


def test_deploy_manifest_records_system_launchd_backup_and_original_scope():
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert 'DARWIN_SYSTEM_PLIST_BACKUP="$DARWIN_SYSTEM_PLIST_BACKUP"' in node
    assert 'DARWIN_SYSTEM_LAUNCHD_ACTIVE="$DARWIN_SYSTEM_LAUNCHD_ACTIVE"' in node
    assert '"mac_system_plist": os.environ.get("DARWIN_SYSTEM_PLIST_BACKUP")' in node
    assert '"mac_system_launchd_was_active": (' in node
    assert '"control_plane_system": probe(' in node


def test_remote_deploy_payload_is_materialized_before_execution():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")

    # The deploy body is a standalone script streamed atomically only after the
    # same pinned SSH session proves the exact remote deployment fence.
    assert "fleet-node-install.sh" in deploy
    assert "fenced_remote_upload" in deploy
    assert "scp" not in deploy.lower()
    assert "remote_node_script" in deploy
    # The node install script must begin with set -euo pipefail.
    assert node.splitlines()[1] == "set -euo pipefail"
    # The old stdin-based payload approach must no longer be present.
    assert 'payload="$(mktemp "${TMPDIR:-/tmp}/mac-deploy.XXXXXX")"' not in deploy
    assert "${remote_env[*]} bash -s" not in deploy


def test_supervisord_rotates_each_gateway_implementation_log_before_classification():
    text = script_text()
    function = text.split("install_supervisord_service() {", 1)[1].split(
        "install_darwin_service() {", 1
    )[0]

    assert '"$LOG_DIR/hermes-gateway.log" "$LOG_DIR/openclaw-gateway.log"' in function
    assert 'sudo truncate -s 0 "$gateway_log"' in function


def test_recovery_route_failure_propagates_from_conditional_context(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    recovery = (
        "recover_active_cohort_transaction_v2() {"
        + deploy.split("recover_active_cohort_transaction_v2() {", 1)[1].split(
            "\n}\n\nrecover_incomplete_cohort_transaction_before_deploy", 1
        )[0]
        + "\n}"
    )
    events = tmp_path / "events"
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
PYTHON_BIN={shlex.quote(sys.executable)}
DEPLOY_CONTROLLER_NONCE=controller
COHORT_JOURNAL_ACTIVE=1
COHORT_JOURNAL_REVISION=0
RECOVERY_POLICY=retain-forward
cohort_journal() {{
  case "$1" in
    status) printf '%s\n' '{{"journal":{{"revision":1,"fleet":"mac"}}}}' ;;
    recovery) printf '%s\n' '{{"direction":"retain_forward"}}' ;;
  esac
}}
verify_cohort_recovery_routes() {{ printf '%s\n' verify >> {shlex.quote(str(events))}; return 42; }}
{recovery}
set +e
if recover_active_cohort_transaction_v2 epoch controller; then result=0; else result=$?; fi
printf '%s|%s\n' "$result" "$COHORT_JOURNAL_ACTIVE"
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1|1"
    assert events.read_text(encoding="utf-8").splitlines() == ["verify"]


def test_recovery_route_accepts_endpoint_helper_comparison_envelope(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    verify = (
        "verify_cohort_recovery_routes() {"
        + deploy.split("verify_cohort_recovery_routes() {", 1)[1].split(
            "\n}\n\nrecover_committed_cohort_node", 1
        )[0]
        + "\n}"
    )
    digest = "a" * 64
    identity = {
        "schema": "mac.fleet_endpoint_identity.v1",
        "adapter": "ssh-machine",
        "authority": {
            "ssh_host_key_sha256": digest,
            "instance_id_kind": "machine-id",
            "instance_id_sha256": digest,
        },
        "observation": {},
    }
    hub_identity = {
        "schema": "mac.fleet_endpoint_identity.v1",
        "adapter": "ssh-hub",
        "authority": {
            "ssh_host_key_sha256": digest,
            "instance_id_kind": "ioplatformuuid",
            "instance_id_sha256": digest,
            "durable_store_uuid_sha256": digest,
        },
        "observation": {},
    }
    status = tmp_path / "status.json"
    recovery = tmp_path / "recovery.json"
    observed = tmp_path / "observed.json"
    hub_observed = tmp_path / "hub-observed.json"
    control_masters = tmp_path / "control-masters"
    diagnostics = tmp_path / "diagnostics"
    status.write_text(
        json.dumps(
            {
                "journal": {
                    "epoch_id": "fixture-epoch",
                    "source_commit": "b" * 40,
                }
            }
        ),
        encoding="utf-8",
    )
    recovery.write_text(
        json.dumps(
            {
                "direction": "rollback",
                "hub_recovery": {
                    "action": "none",
                    "agent_name": "rocky",
                    "route_identity": hub_identity,
                },
                "candidates": [{"agent_name": "natasha", "route_identity": identity}],
            }
        ),
        encoding="utf-8",
    )
    observed.write_text(json.dumps(identity), encoding="utf-8")
    observed.chmod(0o600)
    hub_observed.write_text(json.dumps(hub_identity), encoding="utf-8")
    hub_observed.chmod(0o600)
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
PYTHON_BIN={shlex.quote(sys.executable)}
ENDPOINT_IDENTITY_HELPER={shlex.quote(str(ROOT / "deploy" / "fleet-endpoint-identity.py"))}
OBSERVED_IDENTITY={shlex.quote(str(observed))}
HUB_OBSERVED_IDENTITY={shlex.quote(str(hub_observed))}
SSH_CONTROL_REQUIRED=0
start_ssh_control_master() {{ printf '%s\\n' "$1" >> {shlex.quote(str(control_masters))}; }}
write_live_endpoint_identity() {{
  if [ "$1" = rocky ]; then
    cp -f "$HUB_OBSERVED_IDENTITY" "$2"
  else
    cp -f "$OBSERVED_IDENTITY" "$2"
  fi
  chmod 0600 "$2"
}}
read_recovery_hub_authority() {{
  printf '%s\\n' '{{"schema":"mac.fleet_release_hub_authority.v1","hub_authority_id":"123e4567-e89b-12d3-a456-426614174000"}}' > "$3"
}}
validate_hub_authority_output() {{ printf '%s\\n' '123e4567-e89b-12d3-a456-426614174000'; }}
persist_cohort_recovery_route_mismatch() {{ printf '%s\n' "$*" >> {shlex.quote(str(diagnostics))}; }}
{verify}
set +e
if verify_cohort_recovery_routes {shlex.quote(str(status))} {shlex.quote(str(recovery))}; then first=0; else first=$?; fi
if verify_cohort_recovery_routes {shlex.quote(str(status))} {shlex.quote(str(recovery))}; then second=0; else second=$?; fi
printf '%s|%s|%s\n' "$first" "$second" "$SSH_CONTROL_REQUIRED"
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0|0|1", result.stderr
    assert not diagnostics.exists()
    assert control_masters.read_text(encoding="utf-8").splitlines() == [
        "rocky",
        "natasha",
        "rocky",
        "natasha",
    ]


@pytest.mark.parametrize(
    ("observed_host_digest", "database_authority_id", "expected_success"),
    [
        ("a" * 64, "123e4567-e89b-12d3-a456-426614174000", True),
        ("b" * 64, "123e4567-e89b-12d3-a456-426614174000", False),
        ("a" * 64, "123e4567-e89b-12d3-a456-426614174999", False),
    ],
    ids=("hub-down-match", "wrong-ssh-host", "wrong-database-uuid"),
)
def test_aborted_hub_route_uses_postgres_authority_and_keeps_exact_identity_gate(
    tmp_path, observed_host_digest, database_authority_id, expected_success
):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    validate = (
        "validate_hub_authority_output() {"
        + deploy.split("validate_hub_authority_output() {", 1)[1].split(
            "\n}\n\nread_aborted_hub_authority_from_postgres", 1
        )[0]
        + "\n}"
    )
    recovery_authority = (
        "read_recovery_hub_authority() {"
        + deploy.split("read_recovery_hub_authority() {", 1)[1].split(
            "\n}\n\nhub_epoch_client_request", 1
        )[0]
        + "\n}"
    )
    verify = (
        "verify_cohort_recovery_routes() {"
        + deploy.split("verify_cohort_recovery_routes() {", 1)[1].split(
            "\n}\n\nrecover_committed_cohort_node", 1
        )[0]
        + "\n}"
    )
    authority_id = "123e4567-e89b-12d3-a456-426614174000"
    store_digest = hashlib.sha256(authority_id.encode()).hexdigest()
    hub_identity = {
        "schema": "mac.fleet_endpoint_identity.v1",
        "adapter": "ssh-hub",
        "authority": {
            "ssh_host_key_sha256": "a" * 64,
            "instance_id_kind": "machine-id",
            "instance_id_sha256": "c" * 64,
            "durable_store_uuid_sha256": store_digest,
        },
        "observation": {},
    }
    status = tmp_path / "status.json"
    recovery = tmp_path / "recovery.json"
    events = tmp_path / "events"
    status.write_text(
        json.dumps(
            {
                "journal": {
                    "epoch_id": "fixture-epoch",
                    "source_commit": "d" * 40,
                    "hub_state": "aborted",
                }
            }
        ),
        encoding="utf-8",
    )
    recovery.write_text(
        json.dumps(
            {
                "direction": "rollback",
                "hub_recovery": {
                    "action": "none",
                    "agent_name": "rocky",
                    "route_identity": hub_identity,
                },
                "candidates": [],
            }
        ),
        encoding="utf-8",
    )
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
PYTHON_BIN={shlex.quote(sys.executable)}
ENDPOINT_IDENTITY_HELPER={shlex.quote(str(ROOT / "deploy" / "fleet-endpoint-identity.py"))}
SSH_CONTROL_REQUIRED=0
EVENTS={shlex.quote(str(events))}
start_ssh_control_master() {{ :; }}
hub_epoch_client_read() {{ printf '%s\\n' api >> "$EVENTS"; return 1; }}
read_aborted_hub_authority_from_postgres() {{
  printf '%s\\n' postgres >> "$EVENTS"
  printf '%s\\n' '{{"schema":"mac.fleet_release_hub_authority.v1","hub_authority_id":"{database_authority_id}"}}' > "$2"
}}
write_live_endpoint_identity() {{
  "$PYTHON_BIN" - "$2" "$3" <<'PY'
import hashlib,json,sys
identity={{
  "schema":"mac.fleet_endpoint_identity.v1",
  "adapter":"ssh-hub",
  "authority":{{
    "ssh_host_key_sha256":"{observed_host_digest}",
    "instance_id_kind":"machine-id",
    "instance_id_sha256":"{"c" * 64}",
    "durable_store_uuid_sha256":hashlib.sha256(sys.argv[2].lower().encode()).hexdigest(),
  }},
  "observation":{{}},
}}
with open(sys.argv[1],"w",encoding="utf-8") as stream: json.dump(identity,stream)
PY
  chmod 0600 "$2"
}}
persist_cohort_recovery_route_mismatch() {{ printf '%s\\n' "$TMPDIR_LOCAL/mismatch.json"; }}
{validate}
{recovery_authority}
{verify}
set +e
verify_cohort_recovery_routes {shlex.quote(str(status))} {shlex.quote(str(recovery))}
printf '%s\\n' "$?"
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("0" if expected_success else "1"), result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == ["api", "postgres"]
    if not expected_success:
        assert "recovery route mismatch diagnostic retained" in result.stderr


def test_recovery_hub_authority_prefers_live_api_without_postgres_fallback(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    validate = (
        "validate_hub_authority_output() {"
        + deploy.split("validate_hub_authority_output() {", 1)[1].split(
            "\n}\n\nread_aborted_hub_authority_from_postgres", 1
        )[0]
        + "\n}"
    )
    recovery_authority = (
        "read_recovery_hub_authority() {"
        + deploy.split("read_recovery_hub_authority() {", 1)[1].split(
            "\n}\n\nhub_epoch_client_request", 1
        )[0]
        + "\n}"
    )
    status = tmp_path / "status.json"
    output = tmp_path / "authority.json"
    events = tmp_path / "events"
    status.write_text(json.dumps({"journal": {"hub_state": "aborted"}}), encoding="utf-8")
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
PYTHON_BIN={shlex.quote(sys.executable)}
EVENTS={shlex.quote(str(events))}
hub_epoch_client_read() {{
  printf '%s\\n' api >> "$EVENTS"
  printf '%s\\n' '{{"schema":"mac.fleet_release_hub_authority.v1","hub_authority_id":"123e4567-e89b-12d3-a456-426614174000"}}' > "$2"
}}
read_aborted_hub_authority_from_postgres() {{ printf '%s\\n' postgres >> "$EVENTS"; return 1; }}
{validate}
{recovery_authority}
read_recovery_hub_authority rocky {shlex.quote(str(status))} {shlex.quote(str(output))}
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert events.read_text(encoding="utf-8").splitlines() == ["api"]


def test_aborted_hub_authority_reader_is_postgres_only_and_secret_safe():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    reader = deploy.split("read_aborted_hub_authority_from_postgres() {", 1)[1].split(
        "\n}\n\nread_recovery_hub_authority", 1
    )[0]

    assert "from mac.deploy_env import parse_env_text" in reader
    assert "SELECT authority_id::text FROM hub_authority_identity" in reader
    assert 'dsn.startswith(("postgres://", "postgresql://"))' in reader
    assert "SQLite fallback is forbidden" in reader
    assert "sqlite3" not in reader.lower()
    assert "psycopg.connect(" in reader
    assert "connect_timeout=10" in reader
    assert "statement_timeout=10000" in reader
    assert "ssh_target" in reader
    assert '"$HOME/.mac/venv/bin/python" -' in reader
    assert "$dsn" not in reader


def test_recovery_route_mismatch_persists_wrapped_helper_comparison(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    persist = (
        "persist_cohort_recovery_route_mismatch() {"
        + deploy.split("persist_cohort_recovery_route_mismatch() {", 1)[1].split(
            "\n}\n\nverify_cohort_recovery_routes", 1
        )[0]
        + "\n}"
    )
    status = tmp_path / "status.json"
    comparison = tmp_path / "comparison.json"
    status.write_text(
        json.dumps(
            {
                "journal": {
                    "epoch_id": "fixture-epoch",
                    "source_commit": "b" * 40,
                }
            }
        ),
        encoding="utf-8",
    )
    comparison.write_text(
        json.dumps(
            {
                "ok": True,
                "comparison": {
                    "schema": "mac.fleet_endpoint_identity_comparison.v1",
                    "adapter": "ssh-machine",
                    "same_resource": False,
                    "same_observation": True,
                    "recovery_allowed": False,
                    "generic_route_recovery_allowed": False,
                    "requires_workload_adapter": False,
                    "mismatches": ["authority.ssh_host_key_sha256"],
                },
            }
        ),
        encoding="utf-8",
    )
    tmp_path.chmod(0o700)
    snippet = f"""set -u
PYTHON_BIN={shlex.quote(sys.executable)}
COHORT_JOURNAL_DIR={shlex.quote(str(tmp_path))}
{persist}
persist_cohort_recovery_route_mismatch {shlex.quote(str(status))} {shlex.quote(str(comparison))} node natasha
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    diagnostic = Path(result.stdout.strip())
    assert diagnostic.parent == tmp_path
    assert diagnostic.stat().st_mode & 0o777 == 0o600
    payload = json.loads(diagnostic.read_text(encoding="utf-8"))
    assert payload == {
        "schema": "mac.fleet_recovery_route_mismatch.v1",
        "epoch_id": "fixture-epoch",
        "source_commit": "b" * 40,
        "role": "node",
        "agent": "natasha",
        "adapter": "ssh-machine",
        "same_resource": False,
        "same_observation": True,
        "recovery_allowed": False,
        "generic_route_recovery_allowed": False,
        "requires_workload_adapter": False,
        "mismatches": ["authority.ssh_host_key_sha256"],
        "observed_at": payload["observed_at"],
    }


def test_typed_prepare_and_composite_rollback_are_journal_ordered():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    typed = deploy.split("run_typed_cohort() {", 1)[1].split("\n}\n\nmain()", 1)[0]
    prepare_intent = typed.index("cohort_journal_mutate phase1-prepare-start")
    remote_prepare = typed.index(
        'run_bounded_node_phase "$selected_specs_file" phase1-prepare',
        prepare_intent,
    )
    armed = typed.index("cohort_journal_mutate phase1-armed", remote_prepare)
    assert prepare_intent < remote_prepare < armed
    worker = deploy.split("typed_phase1_prepare_worker() {", 1)[1].split(
        "\n}\n\nstart_control_master_worker", 1
    )[0]
    assert "prepare_remote_phase1_restore_contract" in worker

    recovery = deploy.split("recover_cohort_node() {", 1)[1].split(
        "\n}\n\nrecover_active_cohort_transaction", 1
    )[0]
    phase2 = recovery.index("rollback_remote_phase2_generation")
    phase1 = recovery.index("restore_remote_phase1_generation", phase2)
    composite = recovery.index("write_cohort_composite_rollback_evidence", phase1)
    aborted = recovery.index("cohort_journal_mutate aborted-node", composite)
    assert phase2 < phase1 < composite < aborted


def test_phase1_recovery_replays_retained_helper_and_reviewed_cli_identity(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    restore_function = (
        "restore_remote_phase1_generation() {"
        + deploy.split("restore_remote_phase1_generation() {", 1)[1].split(
            "\n}\n\nrollback_remote_phase2_generation", 1
        )[0]
        + "\n}"
    )
    mac_home = tmp_path / ".mac"
    retained_dir = mac_home / "retained"
    retained_dir.mkdir(parents=True)
    generation = "generation-1"
    revision = "a" * 40
    receipt = mac_home / f"phase1-cohort-restore-{generation}.json"
    marker = tmp_path / "restore-env.json"
    retained = retained_dir / "phase1-restore"
    retained.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
"$PY" - <<'PY'
import json
import os
from pathlib import Path
required = {
    "helper": os.environ["MAC_PHASE1_HELPER_SOURCE"],
    "version": os.environ["MAC_DEPLOY_REVIEWED_OPENSHELL_VERSION"],
    "asset": os.environ["MAC_DEPLOY_REVIEWED_OPENSHELL_ASSET_SHA256"],
    "cli": os.environ["MAC_DEPLOY_REVIEWED_OPENSHELL_CLI_SHA256"],
    "receipt": os.environ["MAC_DEPLOY_REVIEWED_OPENSHELL_RECEIPT_SHA256"],
}
Path(os.environ["EXPECTED_MARKER"]).write_text(json.dumps(required), encoding="utf-8")
payload = {
    "schema": "mac.phase1_cohort_restore.v1",
    "status": "restored",
    "agent": os.environ["AGENT"],
    "generation": os.environ["DEPLOY_GENERATION"],
    "revision": os.environ["DEPLOY_REV"],
    "source_contract_sha256": os.environ["MAC_PHASE1_RESTORE_CONTRACT_SHA256"],
}
path = Path(os.environ["EXPECTED_RECEIPT"])
path.write_text(json.dumps(payload), encoding="utf-8")
path.chmod(0o600)
PY
""",
        encoding="utf-8",
    )
    retained.chmod(0o700)
    reviewed = {
        "schema": "mac.reviewed_openshell_cli.v1",
        "version": "0.0.test",
        "asset_sha256": "1" * 64,
        "cli_sha256": "2" * 64,
        "receipt_sha256": "3" * 64,
    }
    daemon_contract = {
        "schema": "mac.daemon_resource_restore_contract.v1",
        "generation": generation,
        "revision": revision,
        "openclaw": {"reviewed_openshell_cli": reviewed},
    }
    daemon_path = mac_home / f"daemon-resource-restore-contract-{generation}.json"
    daemon_raw = json.dumps(daemon_contract, sort_keys=True).encode()
    daemon_path.write_bytes(daemon_raw)
    daemon_path.chmod(0o600)
    contract = {
        "schema": "mac.phase1_cohort_restore_contract.v1",
        "agent": "rocky",
        "generation": generation,
        "revision": revision,
        "rollback_capable": True,
        "restore_receipt": str(receipt),
        "restore_executable": {
            "path": str(retained),
            "sha256": hashlib.sha256(retained.read_bytes()).hexdigest(),
            "argv": [str(retained), "restore"],
        },
        "daemon_restore_contract": {
            "path": str(daemon_path),
            "sha256": hashlib.sha256(daemon_raw).hexdigest(),
        },
    }
    contract_path = mac_home / f"phase1-cohort-restore-contract-{generation}.json"
    contract_raw = json.dumps(contract, sort_keys=True).encode()
    contract_path.write_bytes(contract_raw)
    contract_path.chmod(0o600)
    snippet = f"""set -euo pipefail
PYTHON_BIN={sys.executable!s}
TEST_HOME={shlex.quote(str(tmp_path))}
EXPECTED_MARKER={shlex.quote(str(marker))}
EXPECTED_RECEIPT={shlex.quote(str(receipt))}
export TEST_HOME EXPECTED_MARKER EXPECTED_RECEIPT
run_fenced_remote_python() {{
  shift 2
  code="$1"
  shift
  HOME="$TEST_HOME" "$PYTHON_BIN" -c "$code" "$@"
}}
{restore_function}
restore_remote_phase1_generation rocky {shlex.quote(generation)} {revision} mac darwin launchd {hashlib.sha256(contract_raw).hexdigest()}
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    restored = json.loads(result.stdout)
    assert restored["status"] == "restored"
    observed = json.loads(marker.read_text(encoding="utf-8"))
    assert observed == {
        "helper": str(retained),
        "version": reviewed["version"],
        "asset": reviewed["asset_sha256"],
        "cli": reviewed["cli_sha256"],
        "receipt": reviewed["receipt_sha256"],
    }


def test_phase1_prepare_upload_failure_releases_exact_lock_in_conditional_context(
    tmp_path,
):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    cleanup = (
        "cleanup_failed_phase1_prepare_lock() {"
        + deploy.split("cleanup_failed_phase1_prepare_lock() {", 1)[1].split(
            "\n}\n\nprepare_remote_phase1_restore_contract", 1
        )[0]
        + "\n}"
    )
    prepare = (
        "prepare_remote_phase1_restore_contract() {"
        + deploy.split("prepare_remote_phase1_restore_contract() {", 1)[1].split(
            "\n}\n\nquiesce_remote_agent_for_cohort", 1
        )[0]
        + "\n}"
    )
    events = tmp_path / "events"
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
DEPLOY_CONTROLLER_NONCE=controller
PHASE1_QUIESCE_HELPER=/fixture/helper
PHASE1_DAEMON_FUNCTIONS=/fixture/functions
stable_worker_agent_id() {{ printf '%s\n' agent_fixture; }}
phase1_restore_contract_file_for_agent() {{ printf '%s\n' {shlex.quote(str(tmp_path / "contract.json"))}; }}
acquire_remote_deployment_lock() {{ printf '%s\n' "acquire:$1:$2" >> {shlex.quote(str(events))}; }}
fenced_remote_upload() {{ printf '%s\n' "upload:$3" >> {shlex.quote(str(events))}; return 9; }}
release_remote_deployment_lock() {{ printf '%s\n' "release:$1:$2" >> {shlex.quote(str(events))}; }}
{cleanup}
{prepare}
set +e
if prepare_remote_phase1_restore_contract fixture exact-generation systemd mac linux; then result=0; else result=$?; fi
printf '%s\n' "$result"
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "1"
    assert events.read_text(encoding="utf-8").splitlines() == [
        "acquire:fixture:exact-generation",
        "upload:/fixture/helper",
        "release:fixture:exact-generation",
    ]


def test_phase1_quiesce_remote_failure_stays_failed_in_conditional_context(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    quiesce = (
        "quiesce_remote_agent_for_cohort() {"
        + deploy.split("quiesce_remote_agent_for_cohort() {", 1)[1].split(
            "\n}\n\nprepare_remote_mac_agent_deployment", 1
        )[0]
        + "\n}"
    )
    events = tmp_path / "events"
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
DEPLOY_CONTROLLER_NONCE=controller
PHASE1_QUIESCE_HELPER=/fixture/helper
PHASE1_DAEMON_FUNCTIONS=/fixture/functions
OPENSHELL_REVIEWED_CLI_VERSION=fixture-version
GIT_REV=fixture-revision
PYTHON_BIN={shlex.quote(sys.executable)}
stable_worker_agent_id() {{ printf '%s\n' agent_fixture; }}
phase1_restore_contract_digest_for_agent() {{ printf '%s\n' restore-sha; }}
reviewed_openshell_cli_status_value() {{ printf '%s\n' "$2-sha"; }}
fenced_remote_upload() {{ printf '%s\n' "upload:$3" >> {shlex.quote(str(events))}; }}
ssh_target_args() {{ printf '%s\\0' fixture-target; }}
remote_deployment_fenced_exec() {{ printf '%s\n' fixture-fence; }}
shell_quote() {{ printf '%q' "$1"; }}
ssh() {{ printf '%s\n' ssh >> {shlex.quote(str(events))}; return 23; }}
{quiesce}
set +e
if quiesce_remote_agent_for_cohort fixture exact-generation systemd mac linux; then result=0; else result=$?; fi
printf '%s\n' "$result"
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "1"
    assert events.read_text(encoding="utf-8").splitlines() == [
        "upload:/fixture/helper",
        "upload:/fixture/functions",
        "ssh",
    ]


def test_phase1_quiesce_forwards_daemon_timeout_overrides_to_the_node(tmp_path, monkeypatch):
    # Regression: an operator-set MAC_DEPLOY_DAEMON_COMMAND_TIMEOUT_SECONDS
    # override reached deploy_host's phase-2 SSH command (see
    # test_daemon_and_openclaw_timeouts_are_forwarded_only_when_set) but NOT
    # this phase-1 quiesce SSH command -- the one that actually stops the
    # daemon and raises "managed OpenClaw stop wrapper timed out" when the
    # node's 20s default is too tight. An override that never reaches the
    # step it exists for is worthless; observed live on 2026-09-03: setting
    # the override made no difference across repeated deploy attempts,
    # every one failing at this exact quiesce step with the node's
    # unmodified 20s default.
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    quiesce = (
        "quiesce_remote_agent_for_cohort() {"
        + deploy.split("quiesce_remote_agent_for_cohort() {", 1)[1].split(
            "\n}\n\nprepare_remote_mac_agent_deployment", 1
        )[0]
        + "\n}"
    )
    ssh_command_file = tmp_path / "ssh-command"
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
DEPLOY_CONTROLLER_NONCE=controller
PHASE1_QUIESCE_HELPER=/fixture/helper
PHASE1_DAEMON_FUNCTIONS=/fixture/functions
OPENSHELL_REVIEWED_CLI_VERSION=fixture-version
GIT_REV=fixture-revision
MAC_DEPLOY_DAEMON_COMMAND_TIMEOUT_SECONDS=300
MAC_DEPLOY_DAEMON_QUIESCENCE_TIMEOUT_SECONDS=600
stable_worker_agent_id() {{ printf '%s\n' agent_fixture; }}
phase1_restore_contract_digest_for_agent() {{ printf '%s\n' restore-sha; }}
reviewed_openshell_cli_status_value() {{ printf '%s\n' "$2-sha"; }}
fenced_remote_upload() {{ return 0; }}
ssh_target_args() {{ printf '%s\\0' fixture-target; }}
remote_deployment_fenced_exec() {{ printf '%s\n' fixture-fence; }}
shell_quote() {{ printf '%q' "$1"; }}
ssh() {{ printf '%s\n' "${{@: -1}}" > {shlex.quote(str(ssh_command_file))}; return 23; }}
{quiesce}
set +e
quiesce_remote_agent_for_cohort fixture exact-generation systemd mac linux
true
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    command = ssh_command_file.read_text(encoding="utf-8")
    assert "MAC_DEPLOY_DAEMON_COMMAND_TIMEOUT_SECONDS=300" in command
    assert "MAC_DEPLOY_DAEMON_QUIESCENCE_TIMEOUT_SECONDS=600" in command
    # Unset overrides must not appear as empty assignments (would fail-close
    # bounded_number() on the node).
    assert "MAC_DEPLOY_DAEMON_PRESERVATION_TIMEOUT_SECONDS=" not in command
    assert "MAC_DEPLOY_DAEMON_LEASE_DRAIN_TIMEOUT_SECONDS=" not in command
    assert "MAC_DEPLOY_DAEMON_QUIESCENCE_POLL_SECONDS=" not in command
    assert "MAC_DEPLOY_DAEMON_TOTAL_TIMEOUT_SECONDS=" not in command


def test_recovery_aborts_unmutated_pre_route_without_route_attestation(tmp_path):
    # The journal classifier can report direction=abort_unmutated for a
    # transaction that never bound a route identity or mutated the hub/nodes
    # (e.g. the first hub-route reachability check failed just after journal
    # creation).  The controller must abort it directly, never invoking the
    # route attestation gate that would otherwise raise "recovery mutation
    # lacks a journal-bound endpoint identity".
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    recovery = (
        "recover_active_cohort_transaction_v2() {"
        + deploy.split("recover_active_cohort_transaction_v2() {", 1)[1].split(
            "\n}\n\nrecover_incomplete_cohort_transaction_before_deploy", 1
        )[0]
        + "\n}"
    )
    events = tmp_path / "events"
    snippet = f"""set -u
TMPDIR_LOCAL={shlex.quote(str(tmp_path))}
PYTHON_BIN={shlex.quote(sys.executable)}
DEPLOY_CONTROLLER_NONCE=controller
COHORT_JOURNAL_ACTIVE=1
COHORT_JOURNAL_REVISION=0
RECOVERY_POLICY=retain-forward
cohort_journal() {{
  case "$1" in
    status) printf '%s\\n' '{{"journal":{{"revision":0,"fleet":"mac"}}}}' ;;
    recovery) printf '%s\\n' '{{"direction":"abort_unmutated","hub_recovery":{{"action":"none"}},"candidates":[]}}' ;;
  esac
}}
verify_cohort_recovery_routes() {{ printf '%s\\n' verify >> {shlex.quote(str(events))}; return 1; }}
cohort_journal_mutate() {{ printf '%s\\n' "mutate:$1" >> {shlex.quote(str(events))}; return 0; }}
confirm_cohort_journal_terminal() {{ printf '%s\\n' "confirm:$2" >> {shlex.quote(str(events))}; return 0; }}
{recovery}
set +e
if recover_active_cohort_transaction_v2 epoch controller; then result=0; else result=$?; fi
printf '%s|%s\\n' "$result" "$COHORT_JOURNAL_ACTIVE"
"""
    result = subprocess.run(["bash", "-c", snippet], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "0|0"
    assert "unmutated pre-route cohort transaction was safely aborted" in result.stdout
    recorded = events.read_text(encoding="utf-8").splitlines()
    # Route attestation is never invoked for the provably unmutated case.
    assert "verify" not in recorded
    # The journal is aborted directly and confirmed terminal as aborted.
    assert "mutate:abort" in recorded
    assert "confirm:aborted" in recorded
