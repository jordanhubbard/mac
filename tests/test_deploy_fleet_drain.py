from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"


def script_text():
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


def test_deploy_drains_worker_before_stopping_services():
    text = script_text()

    assert "drain_mac_agent_before_deploy()" in text
    assert "wait_for_agent_active_leases" in text
    assert "MAC_DEPLOY_DRAIN_MODE" in text
    assert "MAC_DEPLOY_DRAIN_TIMEOUT_SECONDS" in text
    assert 'add_remote_env MAC_DEPLOY_DRAIN_MODE "${MAC_DEPLOY_DRAIN_MODE:-}"' in text
    assert 'add_remote_env MAC_DEPLOY_DEFER_CLEAR_DRAIN "$openshell_enabled"' in text
    assert "add_remote_env MAC_DEPLOY_DEFER_AGENT_RESTART 1" in text
    assert 'timeout = float(os.environ.get("MAC_DEPLOY_API_TIMEOUT_SECONDS") or "30")' in text
    assert 'health_status":"degraded' in text

    drain_pos = text.index("drain_mac_agent_before_deploy")
    stop_call_pos = text.index("stop_existing_services_for_deploy", text.index('write_deploy_manifest "pre"'))
    assert drain_pos < stop_call_pos


def test_deploy_clears_worker_drain_after_restart():
    text = script_text()

    assert "clear_mac_agent_drain_after_deploy()" in text
    assert 'health_status":"healthy' in text
    verify_pos = text.index("verify_hub_registration")
    clear_pos = text.index("clear_mac_agent_drain_after_deploy", verify_pos)
    post_manifest_pos = text.index('write_deploy_manifest "post"')
    assert verify_pos < clear_pos < post_manifest_pos


def test_openshell_deploy_holds_drain_until_bootstrap_succeeds():
    text = script_text()

    assert "keeping drain state until post-deploy OpenShell validation completes" in text
    assert "keeping mac-agent stopped while OpenShell validates" in text
    assert 'set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" stop' in text
    assert 'set_remote_mac_agent_service "$agent" "$supervisor" "$fleet_name" restart' in text
    assert 'if [ "$supervisor" = "auto" ]; then' in text
    assert "supervisor=systemd" in text
    assert "supervisor=launchd" in text
    assert "supervisor=supervisord" in text
    assert "OpenShell bootstrap failed; mac-agent remains stopped and drained" in text


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


def test_remote_deploy_payload_is_materialized_before_execution():
    text = script_text()

    assert "Feeding the body directly to ``bash -s`` lets" in text
    assert 'payload="$(mktemp "${TMPDIR:-/tmp}/mac-deploy.XXXXXX")"' in text
    assert 'cat > "$payload"' in text
    assert 'bash "$payload"' in text
    assert 'remote_cmd="${remote_env[*]} bash -c $(shell_quote "$remote_payload_runner")"' in text
    assert '${remote_env[*]} bash -s' not in text
