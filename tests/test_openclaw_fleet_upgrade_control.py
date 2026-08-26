from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "deploy/openclaw/plugins/mac-continuity/index.js"
INSTALLER = ROOT / "deploy/openclaw/install-openclaw-gateway.sh"
FLEET_DEPLOY = ROOT / "deploy/deploy-mac-fleet.sh"


def test_openclaw_has_request_status_and_pre_mutation_cancel_only():
    source = PLUGIN.read_text(encoding="utf-8")

    assert 'name: "mac_fleet_upgrade_request"' in source
    assert 'name: "mac_fleet_upgrade_status"' in source
    assert 'name: "mac_fleet_upgrade_cancel"' in source
    assert 'upgradeApi(api, "POST", "/fleet-upgrades"' in source
    assert "/fleet-upgrades/${upgradeId}/events" in source
    assert "/stage" not in source[source.index('name: "mac_fleet_upgrade_request"') :]
    assert "/arm" not in source[source.index('name: "mac_fleet_upgrade_request"') :]


def test_openclaw_upgrade_credential_is_distinct_and_not_deploy_authority():
    source = PLUGIN.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    fleet_deploy = FLEET_DEPLOY.read_text(encoding="utf-8")

    assert "MAC_OPENCLAW_UPGRADE_TOKEN" in source
    assert "MAC_OPENCLAW_UPGRADE_TOKEN" in installer
    assert "add_remote_env MAC_OPENCLAW_UPGRADE_TOKEN " not in fleet_deploy
    assert (
        'add_remote_secret_env MAC_OPENCLAW_UPGRADE_TOKEN "${MAC_OPENCLAW_UPGRADE_TOKEN:-}"'
        in fleet_deploy
    )
    assert "cfg.upgradeToken" in source
    assert (
        "cfg.token"
        not in source[
            source.index("async function upgradeApi") : source.index("async function fleetAgents")
        ]
    )
