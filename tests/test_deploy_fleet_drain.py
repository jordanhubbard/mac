import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"
NODE_INSTALL_SCRIPT = ROOT / "deploy" / "fleet-node-install.sh"


def script_text():
    return (
        DEPLOY_SCRIPT.read_text(encoding="utf-8")
        + "\n"
        + NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    )


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
        'chmod 0600 "$TMPDIR_LOCAL/fleets-source.yaml" '
        '"$TMPDIR_LOCAL/fleet-defaults-source.yaml"'
    )
    registry_rebind = snapshot.index(
        'FLEET_REGISTRY_CONFIG="$TMPDIR_LOCAL/fleets-source.yaml"'
    )
    defaults_rebind = snapshot.index(
        'FLEET_CONFIG="$TMPDIR_LOCAL/fleet-defaults-source.yaml"'
    )
    assert chmod < registry_rebind < defaults_rebind
    assert (
        "readonly FLEET_REGISTRY_CONFIG FLEET_CONFIG "
        "FLEET_REGISTRY_SOURCE FLEET_CONFIG_SOURCE"
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


def test_main_prepares_entire_cohort_before_first_node_deploy():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    main = deploy.split("main() {", 1)[1].rsplit("\n}\n\nmain", 1)[0]
    phase_one = main.split(
        'echo "==> fleet: phase 1/3 holding and draining all', 1
    )[1].split('echo "==> fleet: phase 2/3 deploying and proving', 1)[0]
    phase_two = main.split(
        'echo "==> fleet: phase 2/3 deploying and proving', 1
    )[1].split('echo "==> fleet: phase 3/3 atomically releasing', 1)[0]
    phase_three = main.split(
        'echo "==> fleet: phase 3/3 atomically releasing', 1
    )[1]

    assert "prepare_remote_mac_agent_deployment" in phase_one
    assert '"$agent" "$(deployment_id_for_agent "$agent")"' in phase_one
    assert "deploy_host " not in phase_one
    assert 'deploy_host "$spec"' in phase_two
    assert "provision_bound_worker_credential" in phase_two
    assert "prepare_remote_mac_agent_deployment" not in phase_two
    assert "commit_fleet_release_epoch" in phase_three
    assert '"$deployed_count" "$hub_agent" "$hold_adoption_plan"' in phase_three
    assert '"$REQUIRE_RELEASE_ALL_SELECTED"' in phase_three


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
    assert 'row.get("health_status") == "healthy"' in hub_gate
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


def test_each_remote_restart_establishes_a_fresh_generation_and_durable_hold():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    service_control = deploy.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]
    pre_service_start = service_control.split('case "$supervisor" in', 1)[0]

    # This code is inside the helper, so every restart invocation gets a new
    # nonce instead of reusing the generation installed by the node transaction.
    assert "import secrets" in pre_service_start
    assert "secrets.token_hex(" in pre_service_start
    assert "MAC_WORKER_DEPLOY_GENERATION" in pre_service_start
    assert "MAC_WORKER_DEPLOY_BARRIER_FILE" in pre_service_start
    assert "write_env_file" in pre_service_start
    assert "barrier_tmp.write_text" in pre_service_start
    assert "barrier_tmp.chmod(0o600)" in pre_service_start

    # A durable hub hold is acquired before drain/lease proof and before any
    # service manager can start the replacement worker.
    hub_gate = deploy.split("hub_agent_restart_gate() {", 1)[1].split(
        "remote_deployment_hold_state() {", 1
    )[0]
    prepare = hub_gate.split('if phase == "prepare":', 1)[1].split(
        'elif phase == "verify":', 1
    )[0]
    hold_pos = prepare.index("cas_hold(")
    drain_pos = prepare.index("post_drain()")
    tasks_pos = prepare.index("active_work()")
    assert hold_pos < drain_pos < tasks_pos
    assert 'task.get("owner_agent_id") == agent_id' in hub_gate
    assert 'for state in ("claimed", "running")' in hub_gate
    assert 'task.get("lease_id")' in hub_gate
    assert "active work did not drain before worker restart" in hub_gate
    service_start = service_control.index('sudo supervisorctl start "$program"')
    assert service_control.index("hub_agent_restart_gate prepare") < service_start


def test_deploy_restarts_under_hold_then_releases_only_after_activation():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    keep_call = (
        'set_remote_mac_agent_service "$agent" "$supervisor" '
        '"$fleet_name" restart keep'
    )
    release_call = (
        'set_remote_mac_agent_service "$agent" "$supervisor" '
        '"$fleet_name" release'
    )

    deploy_host = deploy.split("deploy_host() {", 1)[1].split(
        "\n}\n\nhub_target()", 1
    )[0]
    credential = deploy.split("provision_bound_worker_credential() (", 1)[1].split(
        "\n)\n\nenforce_bound_worker_credentials()", 1
    )[0]
    assert deploy_host.count(keep_call) == 1
    assert keep_call in credential
    assert release_call in credential

    legacy_override = credential.split(
        "MAC_DEPLOY_ALLOW_LEGACY_WORKER_TOKEN", 1
    )[1].split("esac", 1)[0]
    assert release_call in legacy_override
    assert legacy_override.index(release_call) < legacy_override.index("return 0")

    credentialed_restart = credential.index(keep_call)
    activate = credential.index("worker_credentials activate", credentialed_restart)
    activation_proof = credential.index("activate_ok=1", activate)
    final_release = credential.rindex(release_call)
    assert credentialed_restart < activate < activation_proof < final_release

    # Tunnel setup must not restart a worker behind the generation/barrier
    # protocol. It may invoke the helper again, but never supervisorctl directly.
    assert "supervisorctl restart '$agent_prog'" not in deploy


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

    deploy_host = deploy.split("deploy_host() {", 1)[1].split(
        "\n}\n\nhub_target()", 1
    )[0]
    assert "run_openshell_bootstrap" not in deploy_host
    assert (
        'set_remote_mac_agent_service "$agent" "$supervisor" '
        '"$fleet_name" stop'
    ) not in deploy_host
    restart = (
        'set_remote_mac_agent_service "$agent" "$supervisor" '
        '"$fleet_name" restart keep'
    )
    assert deploy_host.count(restart) == 1
    reconcile = deploy_host.index('reconcile_remote_deploy "$agent" "$target"')
    assert reconcile < deploy_host.index(restart, reconcile)


def test_deploy_restarts_agent_only_after_post_manifest_reconciliation():
    text = script_text()

    assert 'DEFER_AGENT_RESTART="${MAC_DEPLOY_DEFER_AGENT_RESTART:-0}"' in text
    assert text.count("deferring mac-agent restart until post-manifest reconciliation") == 3
    assert "An intentionally STOPPED supervisord program makes" in text
    assert "restart deferred until post-manifest reconciliation" in text
    assert "Keep executing the remote deployment" in text
    reconcile_pos = text.index('reconcile_remote_deploy "$agent" "$target"')
    external_restart_pos = text.index(
        'restarting mac-agent after post-manifest reconciliation', reconcile_pos
    )
    assert reconcile_pos < external_restart_pos

    service_control = text.split("set_remote_mac_agent_service() {", 1)[1].split(
        "validate_router_topology_spec() {", 1
    )[0]
    assert 'plist="$HOME/Library/LaunchAgents/${label}.plist"' in service_control
    assert 'if ! launchctl print "$domain/$label"' in service_control
    assert 'launchctl bootstrap "$domain" "$plist"' in service_control
    assert 'launchctl kickstart "$domain/$label"' in service_control


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


def test_outer_hold_cannot_be_cleared_by_a_failed_transaction_rollback():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    prepare = deploy.split("prepare_remote_mac_agent_deployment() {", 1)[1].split(
        "set_remote_mac_agent_service() {", 1
    )[0]
    main = deploy.split("main() {", 1)[1].rsplit("\n}\n\nmain", 1)[0]

    assert 'set_remote_mac_startup_hold_policy "$agent" 0' in prepare
    assert "MAC_STARTUP_CLEAR_HOLD" in deploy
    # The fail-closed startup policy lands before personality/source/runtime
    # mutation, so the node backup and rollback both preserve it.
    barrier = main.index("prepare_remote_mac_agent_deployment")
    deploy_call = main.index('deploy_host "$spec"', barrier)
    assert barrier < deploy_call
    assert 'values["MAC_STARTUP_CLEAR_HOLD"] = "0"' in (
        ROOT / "src" / "mac" / "deploy_env.py"
    ).read_text(encoding="utf-8")
    backup = node.index("backup_existing_artifacts")
    rollback = node.index("write_rollback_script", backup)
    assert backup < rollback


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
    service_start = service_control.index('sudo supervisorctl start "$program"')
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
    service_start = service_control.index('sudo supervisorctl start "$program"')
    prepare_new = service_control.index(
        "hub_agent_restart_gate prepare-new", service_start
    )
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
    ready_record = service.index("mac.deploy_release_ready.v1", arm)
    assert arm < ready_record
    commit = deploy.split("commit_fleet_release_epoch() {", 1)[1].split(
        "enforce_bound_worker_credentials() {", 1
    )[0]
    batch_release = commit.index('"/agents/dispatch-hold/release-batch"')
    lock_release = commit.index("finalize_remote_deployment_release", batch_release)
    assert batch_release < lock_release


def test_deployment_controller_lock_assertion_executes_and_renews(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    function = deploy.split("assert_remote_deployment_lock() {", 1)[1].split(
        "release_remote_deployment_lock() {", 1
    )[0]
    embedded_python = function.split("python3 - <<'PY'\n", 1)[1].split(
        '\nPY"', 1
    )[0]
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
    embedded_python = function.split("python3 - <<'PY'\n", 1)[1].split(
        "\nPY", 1
    )[0]
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
    assert "trap stop_deployment_lock_renewer EXIT" in node


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
    holder_code = r'''
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
'''
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
    acquire_python = function.split("python3 - <<'PY'\n", 1)[1].split(
        '\nPY"', 1
    )[0]
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
    holder_code = r'''
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
'''
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
    assert json.loads(owner.read_text(encoding="utf-8"))["deployment_id"] == (
        "live-controller"
    )
    assert not list(mac_home.glob("deploy-controller.lock.stale.*"))


def test_secret_stream_waits_for_same_session_exact_fence(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    shell_quote = "shell_quote() {" + deploy.split("shell_quote() {", 1)[1].split(
        "\n}\n\n# Resolve every operator-side", 1
    )[0] + "\n}"
    fence_helpers = "remote_deployment_fenced_exec() {" + deploy.split(
        "remote_deployment_fenced_exec() {", 1
    )[1].split("\n}\n\nfenced_remote_upload() {", 1)[0] + "\n}"
    mac_home = tmp_path / ".mac"
    lock = mac_home / "deploy-controller.lock"
    lock.mkdir(parents=True)
    (lock / "owner.json").write_text(
        json.dumps({"deployment_id": "expected-controller"}), encoding="utf-8"
    )
    source = tmp_path / "secret.env"
    output = tmp_path / "received.env"
    source.write_text("TOKEN=not-a-real-secret\n", encoding="utf-8")
    snippet = f'''set -euo pipefail
PYTHON_BIN={sys.executable!s}
{shell_quote}
{fence_helpers}
remote_cmd="$(remote_deployment_fenced_exec expected-controller 1 sh -c 'cat > {output}')"
stream_file_after_remote_fence {source} MAC_DEPLOY_FENCE_READY:expected-controller sh -c "$remote_cmd"
'''
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
    wrong = f'''set -euo pipefail
PYTHON_BIN={sys.executable!s}
{shell_quote}
{fence_helpers}
remote_cmd="$(remote_deployment_fenced_exec wrong-controller 1 sh -c 'cat >/dev/null')"
stream_file_after_remote_fence {missing} MAC_DEPLOY_FENCE_READY:wrong-controller sh -c "$remote_cmd"
'''
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


def test_all_deploy_credentials_use_the_fenced_stdin_secret_channel():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    secret_names = (
        "MAC_DEPLOY_GITHUB_REVIEW_KEY_B64",
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
    assert 'base64.b64decode(sys.argv[2])' not in node
    assert 'base64.b64decode(sys.stdin.buffer.read())' in node


def test_control_master_and_fenced_streams_are_required_before_phase_one():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    main = deploy.split("main() {", 1)[1]
    masters = main.index('start_ssh_control_master "$hub_agent"')
    required = main.index("SSH_CONTROL_REQUIRED=1", masters)
    remote_secret_read = main.index('hub_token="$(read_hub_token)"', required)
    phase_one = main.index("phase 1/3 holding", required)
    assert masters < required < remote_secret_read < phase_one
    assert "-o ControlMaster=yes -o ControlPersist=no" in deploy
    assert "-S \"$control_path\" -O proxy" in deploy
    pinned = deploy.split("pinned_fleet_route_args() {", 1)[1].split(
        "\n}\n\nssh_target_args() {", 1
    )[0]
    assert "pinned SSH control socket is unavailable" in pinned
    assert pinned.index("pinned SSH control socket is unavailable") < pinned.index(
        '-S "$control_path" -O proxy'
    )
    assert "return 1" not in pinned
    assert "scp -O" not in deploy
    assert "MAC_DEPLOY_FENCE_READY:" in deploy
    credential = deploy.split("provision_bound_worker_credential() (", 1)[1].split(
        "\n)\n\nfinalize_remote_deployment_release()", 1
    )[0]
    assert "fenced_remote_upload" in credential
    assert "worker credential manifest" in credential


def test_required_openshell_image_is_rejected_before_any_remote_fleet_action():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    normalize = "normalize_boolean_token() {" + deploy.split(
        "normalize_boolean_token() {", 1
    )[1].split("\n}\n\nresolve_python_bin() {", 1)[0] + "\n}"
    preflight = "validate_openshell_runtime_image_spec() {" + deploy.split(
        "validate_openshell_runtime_image_spec() {", 1
    )[1].split("\n}\n\ndeploy_host() {", 1)[0] + "\n}"
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
    first_hold = main.index("phase 1/3 holding")
    assert local_preflight < first_remote < first_hold


def test_reverse_tunnel_manager_mutation_holds_the_exact_hub_fence():
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    tunnel = deploy.split("install_reverse_tunnel_on_hub() {", 1)[1].split(
        "\n}\n\nuses_direct_mesh_hub() {", 1
    )[0]

    deployment_id = tunnel.index(
        'hub_deployment_id="$(deployment_id_for_agent "$hub_agent")"'
    )
    assertion = tunnel.index("assert_remote_deployment_lock", deployment_id)
    temporary_acquire = tunnel.index("acquire_remote_deployment_lock", assertion)
    fence = tunnel.index(
        'remote_deployment_fenced_exec "$hub_deployment_id" 0 bash -s',
        temporary_acquire,
    )
    hub_script = tunnel.index("<<'HUBSCRIPT'", fence)
    temporary_release = tunnel.index("release_remote_deployment_lock", hub_script)
    assert deployment_id < assertion < temporary_acquire < fence < hub_script
    assert hub_script < temporary_release
    assert 'TUNNEL_FLEET_NAME=$(shell_quote "$fleet_name_local") $fence_exec' in tunnel


def test_reverse_tunnel_definitions_adopt_only_exact_legacy_mac_shape(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    hub_script = deploy.split("<<'HUBSCRIPT'\n", 1)[1].split("\nHUBSCRIPT", 1)[0]
    helpers = "managed_marker=" + hub_script.split("managed_marker=", 1)[1].split(
        'if [ "$(uname -s)" = "Darwin" ]; then', 1
    )[0]
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
    setup = f'''set -euo pipefail
fleet_name=mac
worker_agent=worker1
managed_marker=mac.managed-reverse-tunnel.v1:mac:worker1
definition_tmp=""
sudo() {{ "$@"; }}
{helpers}
'''
    ssh_bin = subprocess.check_output(
        ["bash", "-c", "command -v ssh"], text=True
    ).strip()

    def ssh_arguments(binary: str) -> list[str]:
        args = [
            binary,
            "-N",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-i", f"{tmp_path}/.ssh/mac_tunnel_id",
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
        (launchd_legacy, "<!-- mac.managed-reverse-tunnel.v1:mac:worker1 -->", "launchd"),
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
        ("launchd", "<!-- mac.managed-reverse-tunnel.v1:mac:worker1 -->", b"not a plist\n"),
        ("systemd", "# mac.managed-reverse-tunnel.v1:mac:worker1", b"[Service]\nExecStart=/operator\n"),
        ("supervisor", "; mac.managed-reverse-tunnel.v1:mac:worker1", b"[program:mac-tunnel-worker1]\ncommand=/operator\n"),
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
    hub_script = deploy.split("<<'HUBSCRIPT'\n", 1)[1].split("\nHUBSCRIPT", 1)[0]
    assert "mac.managed-reverse-tunnel.v1:" in hub_script
    assert 'sudo test -L "$path"' in hub_script

    launchd = hub_script.split('if [ "$(uname -s)" = "Darwin" ]; then', 1)[1].split(
        "\nfi\nif command -v systemctl", 1
    )[0]
    assert launchd.index('assert_managed_definition_or_absent "$plist"') < launchd.index(
        'stage_managed_definition "$plist"'
    )
    assert 'one("\\tpath = ") != plist' in launchd
    assert 'lines[0] != f"system/{label} = {{"' in launchd
    assert 'args[:-1] != expected' in launchd
    assert 'MAC_MANAGED_REVERSE_TUNNEL => {marker}' in launchd
    assert "launchd_loaded_legacy=1" in launchd
    assert 'sudo launchctl bootout system "$plist"' in launchd
    assert 'sudo launchctl bootstrap system "$plist"' in launchd
    assert 'sudo launchctl enable "system/${label}"' in launchd
    assert 'sudo launchctl kickstart -k "system/${label}"' in launchd

    systemd = hub_script.split(
        "if command -v systemctl >/dev/null 2>&1", 1
    )[1].split("\nfi\nconf_dir=", 1)[0]
    fragment_preflight = systemd.index('systemctl show -p FragmentPath --value')
    stage = systemd.index('stage_managed_definition "$unit"')
    restart = systemd.index('systemctl restart "$service"')
    assert fragment_preflight < stage < restart
    assert systemd.count('systemctl show -p LoadState --value') >= 2
    assert systemd.count('systemctl show -p FragmentPath --value') >= 3
    assert systemd.count('systemctl show -p DropInPaths --value') >= 3
    assert 'systemctl show -p Restart --value' in systemd

    supervisor = hub_script.split("\nfi\nconf_dir=", 1)[1]
    assert supervisor.index('preexisting_status="$(supervisor_status_output)"') < supervisor.index(
        'stage_managed_definition "$conf"'
    )
    assert supervisor.count('assert_no_duplicate_supervisor_program "$conf"') >= 2
    assert "refusing same-name supervisor program from another include" in supervisor
    final_status = supervisor.split('status="$(supervisor_status_output)"', 1)[1]
    assert "*RUNNING*|*STARTING*|*BACKOFF*)" in final_status
    assert "*STOPPED*)" not in final_status


def test_launchd_interrupted_adoption_is_exact_and_retryable(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    hub_script = deploy.split("<<'HUBSCRIPT'\n", 1)[1].split("\nHUBSCRIPT", 1)[0]
    state_function = "launchd_state_has_managed_identity() {" + hub_script.split(
        "launchd_state_has_managed_identity() {", 1
    )[1].split("\n  }\n  launchd_state=", 1)[0] + "\n}"
    ssh_bin = subprocess.check_output(
        ["bash", "-c", "command -v ssh"], text=True
    ).strip()
    plist = tmp_path / "com.mac.tunnel-worker1.plist"
    marker = "mac.managed-reverse-tunnel.v1:mac:worker1"
    marker_line = f"<!-- {marker} -->"
    plist.write_text(marker_line + "\n", encoding="utf-8")
    args = [
        ssh_bin,
        "-N",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-i", f"{tmp_path}/.ssh/mac_tunnel_id",
        "-R", "127.0.0.1:18789:127.0.0.1:8789",
        "-R", "127.0.0.1:18090:127.0.0.1:8090",
        "-R", "127.0.0.1:16333:127.0.0.1:6333",
        "-R", "127.0.0.1:13002:127.0.0.1:3002",
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

    setup = f'''set -euo pipefail
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
'''
    interrupted = subprocess.run(
        [
            "bash",
            "-c",
            setup
            + '''state="$1"
if launchd_state_has_managed_identity managed "$state"; then
  :
elif launchd_state_has_managed_identity legacy "$state"; then
  [ "$launchd_file_legacy" = 1 ] || grep -Fqx "$marker_line" "$plist"
  launchd_loaded_legacy=1
else
  exit 1
fi
[ "$launchd_loaded_legacy" = 1 ]''',
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
            "bash", "-c", setup + 'launchd_state_has_managed_identity managed "$1"',
            "managed-state", launchd_state(plist, marked=True),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert managed.returncode == 0, managed.stderr
    wrong_path = subprocess.run(
        [
            "bash", "-c", setup + 'launchd_state_has_managed_identity legacy "$1"',
            "wrong-path", launchd_state(Path(str(plist) + ".backup"), marked=False),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert wrong_path.returncode != 0


def test_supervisor_duplicate_program_include_is_rejected(tmp_path):
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    hub_script = deploy.split("<<'HUBSCRIPT'\n", 1)[1].split("\nHUBSCRIPT", 1)[0]
    duplicate_function = "assert_no_duplicate_supervisor_program() {" + hub_script.split(
        "assert_no_duplicate_supervisor_program() {", 1
    )[1].split("\n}\nsupervisor_status_output() {", 1)[0] + "\n}"
    include_a = tmp_path / "etc" / "supervisor" / "conf.d"
    include_b = tmp_path / "etc" / "supervisord.d"
    include_a.mkdir(parents=True)
    include_b.mkdir(parents=True)
    expected = include_a / "mac-tunnel-worker1.conf"
    expected.write_text("[program:mac-tunnel-worker1]\ncommand=ssh\n", encoding="utf-8")
    duplicate = include_b / "operator.conf"
    duplicate.write_text("[program:mac-tunnel-worker1]\ncommand=/operator\n", encoding="utf-8")
    snippet = f'''set -euo pipefail
program=mac-tunnel-worker1
sudo() {{ "$@"; }}
{duplicate_function}
assert_no_duplicate_supervisor_program "$1"
'''
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
    deferred = service.split(
        'if [ "$release_commit_mode" = deferred ]; then', 1
    )[1]
    assert "release_gate_phase=arm" in deferred
    assert "mac.deploy_release_ready.v1" in deferred
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
    assert '"generation": item["generation"]' in commit
    assert '"baseline_seen": item["baseline_seen"]' in commit
    assert '"principal_id": item.get("principal_id") or None' in commit
    assert '"require_authenticated": bool(item.get("require_authenticated"))' in commit
    batch_release = commit.index('"/agents/dispatch-hold/release-batch"')
    cleanup = commit.index("finalize_remote_deployment_release", batch_release)
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
    arm = credential.index(
        'keep authenticated "$principal_id" deferred', activate
    )
    assert extract < activate < arm
    assert "if require_authenticated and not expected_principal_id" in hub_gate
    assert (
        'authenticated.get("principal_id") == expected_principal_id'
        in hub_gate
    )
    assert '"principal_id": principal_id' in deploy
    assert (
        'authenticated.get("principal_id") == item.get("principal_id")'
        in commit
    )


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

    assert 'row.get("health_status") == "healthy"' in release
    assert 'api("PUT", "/agents/%s" % agent_id, {"health_status": "healthy"})' not in release
    assert '"degraded" if barrier_active else "healthy"' in worker
    assert 'registration_resources["startup_self_test"] = report' in node
    wrapper_start = node.index('if [ "${MAC_AGENT_STARTUP_SELF_TEST:-1}" != "0" ]')
    resource_read = node.index('worker_resources="${MAC_WORKER_RESOURCES:-}"', wrapper_start)
    assert wrapper_start < resource_read


def test_darwin_deploy_preserves_an_active_system_control_plane_domain():
    node = NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")

    capture = node.split("capture_darwin_launchd_prestate() {", 1)[1].split(
        "wait_for_system_launchd_job_unloaded() {", 1
    )[0]
    assert 'if ! sudo -n true; then' in capture
    assert 'DARWIN_SYSTEM_LAUNCHD_ACTIVE=1' in capture
    assert 'sudo -n test -r "$system_plist"' in capture
    assert 'DARWIN_GUI_LAUNCHD_ACTIVE=1' in capture
    assert 'DARWIN_SYSTEM_SUPERVISOR_LAUNCHD_ACTIVE=1' in capture
    assert 'control plane is loaded in both system and GUI launchd domains' in capture
    assert "write_rollback_script" in capture

    stop = node.split("stop_existing_services_for_deploy() {", 1)[1].split(
        "load_drain_api_env() {", 1
    )[0]
    supervisor_bootout = stop.index(
        'sudo -n launchctl bootout "system/$DARWIN_SYSTEM_SUPERVISOR_LABEL"'
    )
    control_bootout = stop.index(
        'sudo -n launchctl bootout "system/$MAC_LAUNCHD_LABEL"',
        supervisor_bootout,
    )
    stopped = stop.index("wait_for_local_control_plane_stop", control_bootout)
    assert supervisor_bootout < control_bootout < stopped
    assert '|| [ "$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ]' in stop
    assert '|| [ "$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ]' in stop

    install = node.split("install_darwin_service() {", 1)[1].split(
        "install_darwin_openclaw_service() {", 1
    )[0]
    assert 'sudo -n launchctl bootstrap system "$system_plist"' in install
    assert 'sudo -n launchctl kickstart -k "system/$MAC_LAUNCHD_LABEL"' in install
    assert 'wait_for_local_control_plane_health' in install
    assert 'sudo -n launchctl bootstrap system "$system_supervisor_plist"' in install
    atomic_install = install.index(
        'sudo -n install -o root -g wheel -m 0644 '
        '"$system_plist_staging" "$system_plist_tmp"'
    )
    atomic_replace = install.index(
        'sudo -n mv -f "$system_plist_tmp" "$system_plist"', atomic_install
    )
    control_bootstrap = install.index(
        'sudo -n launchctl bootstrap system "$system_plist"', atomic_replace
    )
    healthy = install.index("wait_for_local_control_plane_health", control_bootstrap)
    supervisor_bootstrap = install.index(
        'sudo -n launchctl bootstrap system "$system_supervisor_plist"', healthy
    )
    health_recheck = install.index(
        "wait_for_local_control_plane_health", supervisor_bootstrap
    )
    assert atomic_install < atomic_replace < control_bootstrap < healthy < supervisor_bootstrap
    assert supervisor_bootstrap < health_recheck
    assert "com.${FLEET_NAME}.supervisor" not in install

    capture_call = node.index('capture_darwin_launchd_prestate\n')
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
        'sudo -n cp -f "\\$DARWIN_SYSTEM_PLIST_BACKUP" '
        '"/Library/LaunchDaemons/\\$MAC_LAUNCHD_LABEL.plist"'
        in rollback
    )
    assert 'if [ "\\$DARWIN_SYSTEM_LAUNCHD_ACTIVE" = 1 ]; then' in rollback
    assert 'elif [ "\\$DARWIN_GUI_LAUNCHD_ACTIVE" = 1 ]; then' in rollback
    rollback_stop = rollback.index("wait_control_plane_stopped\n")
    restore_source = rollback.index(
        'restore_dir "\\$SRC_BACKUP" "\\$SRC_DIR"', rollback_stop
    )
    assert rollback_stop < restore_source
    assert 'wait_system_job_unloaded "\\$DARWIN_SYSTEM_SUPERVISOR_LABEL"' in rollback
    assert 'wait_system_job_unloaded "\\$MAC_LAUNCHD_LABEL"' in rollback
    system_bootstrap = rollback.index(
        'sudo -n launchctl bootstrap system '
        '"/Library/LaunchDaemons/\\$MAC_LAUNCHD_LABEL.plist"'
    )
    gui_fallback = rollback.index(
        'launchctl bootstrap "gui/\\$uid" '
        '"\\$HOME/Library/LaunchAgents/\\$MAC_LAUNCHD_LABEL.plist"',
        system_bootstrap,
    )
    assert system_bootstrap < gui_fallback


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
    assert '${remote_env[*]} bash -s' not in deploy


def test_supervisord_rotates_each_gateway_implementation_log_before_classification():
    text = script_text()
    function = text.split("install_supervisord_service() {", 1)[1].split(
        "install_darwin_service() {", 1
    )[0]

    assert '"$LOG_DIR/hermes-gateway.log" "$LOG_DIR/openclaw-gateway.log"' in function
    assert 'sudo truncate -s 0 "$gateway_log"' in function
