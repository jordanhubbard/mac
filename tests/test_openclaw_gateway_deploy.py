"""Contract tests for MAC's stock OpenClaw/OpenShell gateway deployment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
OPENCLAW_DIR = ROOT / "deploy" / "openclaw"
INSTALLER = OPENCLAW_DIR / "install-openclaw-gateway.sh"
CONTAINERFILE = OPENCLAW_DIR / "OpenClaw.Containerfile"
POLICY = OPENCLAW_DIR / "openclaw-policy.yaml"
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"
FLEET_CONFIG = ROOT / "deploy" / "fleet" / "config.yaml"
SYSTEMD_UNIT = ROOT / "deploy" / "systemd" / "mac-openclaw-gateway.service"


def test_stock_openclaw_artifacts_are_pinned_and_do_not_invoke_nemoclaw() -> None:
    assert INSTALLER.stat().st_mode & 0o111
    container = CONTAINERFILE.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "ghcr.io/openclaw/openclaw:2026.6.11@sha256:" in container
    assert 'OPENCLAW_VERSION="2026.6.11"' in installer
    assert 'OPENCLAW_IMAGE="localhost/mac-openclaw:${OPENCLAW_VERSION}"' in installer
    assert "USER sandbox" in container
    assert "nemoclaw gateway" not in container.lower()
    assert "/nemoclaw" not in container.lower()
    assert "nemoclaw gateway" not in installer.lower()
    assert "/nemoclaw" not in installer.lower()


def test_openclaw_policy_is_deny_by_default_and_narrowly_allows_required_services() -> None:
    text = POLICY.read_text(encoding="utf-8")
    assert "run_as_user: sandbox" in text
    assert "__MAC_ROUTER_HOST__" in text
    assert "__MAC_ROUTER_PORT__" in text
    for host in (
        "slack.com",
        "api.slack.com",
        "hooks.slack.com",
        "wss-primary.slack.com",
        "wss-backup.slack.com",
    ):
        assert f"host: {host}" in text
    assert "protocol: websocket" in text
    assert "host: api.telegram.org" in text
    assert 'path: "/bot*/**"' in text
    assert 'path: "/file/bot*/**"' in text
    assert "host: '*'" not in text
    assert "0.0.0.0/0" not in text


def test_prepare_renders_valid_secret_ref_config_without_log_leaks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    secrets = (
        "router-secret-value",
        "xox" + "b-test-placeholder",
        "xap" + "p-test-placeholder",
        "123456:test-telegram-placeholder",
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "hermes_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": secrets[0],
        "MAC_OPENCLAW_MODEL": "azure/anthropic/claude-sonnet-4-6",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SLACK_BOT_TOKEN": secrets[1],
        "MAC_OPENCLAW_SLACK_APP_TOKEN": secrets[2],
        "MAC_OPENCLAW_TELEGRAM_BOT_TOKEN": secrets[3],
    }
    result = subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    output = result.stdout + result.stderr
    assert all(secret not in output for secret in secrets)

    managed = mac_home / "openclaw" / "managed"
    config_path = managed / "openclaw.json"
    runtime_path = managed / "runtime.env"
    wrapper_path = mac_home / "bin" / "openclaw-gateway"
    first_runtime = runtime_path.read_text(encoding="utf-8")
    subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert runtime_path.read_text(encoding="utf-8") == first_runtime
    config = json.loads(config_path.read_text(encoding="utf-8"))
    provider = config["models"]["providers"]["mac-router"]
    slack = config["channels"]["slack"]
    telegram = config["channels"]["telegram"]
    assert provider["apiKey"] == "${MAC_OPENCLAW_ROUTER_API_KEY}"
    assert provider["headers"] == {
        "x-mac-agent-id": "agent_test",
        "x-mac-hermes-instance-id": "hermes_test",
    }
    assert slack["botToken"]["id"] == "SLACK_BOT_TOKEN"
    assert slack["appToken"]["id"] == "SLACK_APP_TOKEN"
    assert telegram["botToken"]["id"] == "TELEGRAM_BOT_TOKEN"
    assert telegram["dmPolicy"] == "pairing"
    assert telegram["groupPolicy"] == "allowlist"
    assert config["gateway"]["auth"]["token"]["id"] == "OPENCLAW_GATEWAY_TOKEN"
    assert all(secret not in config_path.read_text(encoding="utf-8") for secret in secrets)
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert runtime_path.stat().st_mode & 0o777 == 0o600
    assert wrapper_path.stat().st_mode & 0o777 == 0o700
    wrapper = wrapper_path.read_text(encoding="utf-8")
    assert "sandbox create" in wrapper and "sandbox exec" in wrapper
    assert "nemoclaw" not in wrapper.lower()
    assert all(secret not in wrapper for secret in secrets)

    rendered_policy = (mac_home / "openclaw" / "openclaw-policy.yaml").read_text(
        encoding="utf-8"
    )
    assert "host: 100.64.0.1" in rendered_policy
    assert "port: 8789" in rendered_policy
    assert "__MAC_" not in rendered_policy


def test_fleet_deploy_selects_stock_openclaw_on_every_supervisor() -> None:
    config = FLEET_CONFIG.read_text(encoding="utf-8")
    deploy = DEPLOY.read_text(encoding="utf-8")
    unit = SYSTEMD_UNIT.read_text(encoding="utf-8")
    assert "gateway_impl: openclaw" in config
    assert "openclaw)\n      install_linux_openclaw_service" in deploy
    assert "install_darwin_openclaw_service" in deploy
    assert "OPENCLAW_SUPERVISORD_PROG" in deploy
    assert "verify_openclaw_gateway" in deploy
    assert "rollback_openclaw_gateway" in deploy
    assert "MAC_DEPLOY_OPENCLAW_LIVE_CANARY" in deploy
    assert "MAC_WORKER_RESOURCES_FILE" in deploy
    assert "disable --now \"$HERMES_SERVICE_NAME\"" in deploy
    assert "ExecStart=__MAC_HOME__/bin/openclaw-gateway" in unit
    assert "User=__MAC_USER__" in unit


def test_openclaw_verification_requires_both_channels_and_advertises_only_after_proof() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "validate-openclaw-channel-status.py" in installer
    assert "service-advertisement.json" in installer
    assert '"schema": "mac.chat_gateway_service.v1"' in installer
    assert '"provider": "openshell"' in installer
    assert "--channel slack" in installer
    assert "--channel telegram" in installer
    assert 'rm -f "$OPENCLAW_HOST_DIR/service-advertisement.json"' in installer


def test_shell_artifacts_parse() -> None:
    for script in (INSTALLER, DEPLOY):
        subprocess.run(["bash", "-n", str(script)], check=True, timeout=10)
