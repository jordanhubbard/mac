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
    assert 'OPENCLAW_SLACK_PLUGIN_VERSION="2026.6.11"' in container
    assert 'MAC_OPENCLAW_IMAGE_REVISION="5"' in container
    assert "/etc/mac-openclaw-image-revision" in container
    # OpenShell's sandbox supervisor creates an isolated network namespace
    # inside the image and fails closed when no trusted `ip` helper exists.
    assert "apt-get install -y --no-install-recommends bash iproute2" in container
    assert (
        "COPY deploy/verify-bash-contract.sh "
        "/usr/local/bin/mac-verify-bash-contract" in container
    )
    assert container.count("/usr/local/bin/mac-verify-bash-contract") >= 2
    assert "RUN /bin/bash -c" in container
    assert '"npm:@openclaw/slack@${OPENCLAW_SLACK_PLUGIN_VERSION}"' in container
    assert 'OPENCLAW_VERSION="2026.6.11"' in installer
    assert 'OPENCLAW_IMAGE_REVISION="5"' in installer
    assert 'OPENCLAW_IMAGE="localhost/mac-openclaw:${OPENCLAW_VERSION}-mac.${OPENCLAW_IMAGE_REVISION}"' in installer
    assert "/Applications/Docker.app/Contents/Resources/bin/docker" in installer
    assert 'docker_bin="$(find_docker)"' in installer
    assert 'docker_path="$(dirname "$docker_bin"):$PATH"' in installer
    assert 'PATH="$docker_path" "$docker_bin" build --pull' in installer
    assert 'BUILD_CONTEXT="${MAC_OPENCLAW_BUILD_CONTEXT:-$MAC_SRC}"' in installer
    assert '"$BUILD_CONTEXT"' in installer
    assert "USER sandbox" in container
    assert "nemoclaw gateway" not in container.lower()
    assert "/nemoclaw" not in container.lower()
    # The cutover audit must name the legacy service to prove it is inactive,
    # but the stock installer must never execute a NemoClaw binary.
    assert "/usr/local/bin/nemoclaw" not in installer.lower()
    assert "exec nemoclaw" not in installer.lower()
    assert 'image_revision" = "$OPENCLAW_IMAGE_REVISION"' in installer
    assert installer.count("/bin/bash -lc") >= 2
    assert "/usr/local/bin/mac-verify-bash-contract" in installer


def test_openclaw_policy_is_deny_by_default_and_narrowly_allows_required_services() -> None:
    text = POLICY.read_text(encoding="utf-8")
    assert "run_as_user: sandbox" in text
    read_only, read_write = text.split("  read_write:", maxsplit=1)
    assert "/home/sandbox/.config/mac-openclaw" not in read_only
    assert "/home/sandbox/.config/mac-openclaw" in read_write
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
    openclaw_home = mac_home / "openclaw"
    openclaw_home.mkdir(parents=True)
    (openclaw_home / "slack_home_channels.json").write_text(
        json.dumps(
            [
                {
                    "name": "offtera",
                    "team_id": "T123",
                    "channel_id": "C123HOME",
                    "channel_name": "#rockyandfriends",
                },
                {
                    "name": "other",
                    "team_id": "T456",
                    "channel_id": "C456HOME",
                    "channel_name": "#rockyandfriends",
                },
            ]
        ),
        encoding="utf-8",
    )
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
        "MAC_OPENCLAW_PUBLIC_IDENTITY": "mac-hive",
        "MAC_OPENCLAW_SLACK_ACCOUNT_ID": "offtera",
        "MAC_OPENCLAW_HOME_CHANNEL": "rockyandfriends",
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
    stop_wrapper_path = mac_home / "bin" / "openclaw-gateway-stop"
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
    slack = config["channels"]["slack"]["accounts"]["offtera"]
    telegram = config["channels"]["telegram"]["accounts"]["default"]
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
    assert config["plugins"] == {
        "allow": ["slack", "telegram"],
        "enabled": True,
        "entries": {
            "slack": {"enabled": True},
            "telegram": {"enabled": True},
        },
    }
    assert config["gateway"]["auth"]["token"]["id"] == "OPENCLAW_GATEWAY_TOKEN"
    assert all(secret not in config_path.read_text(encoding="utf-8") for secret in secrets)
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert runtime_path.stat().st_mode & 0o777 == 0o600
    assert wrapper_path.stat().st_mode & 0o777 == 0o700
    assert stop_wrapper_path.stat().st_mode & 0o777 == 0o700
    assert (mac_home / "bin" / "openclaw-message").stat().st_mode & 0o777 == 0o700
    assert (mac_home / "bin" / "openclaw-agent").stat().st_mode & 0o777 == 0o700
    assert (mac_home / "openclaw" / "home-channel-target").read_text(
        encoding="utf-8"
    ).strip() == "channel:C123HOME"
    wrapper = wrapper_path.read_text(encoding="utf-8")
    stop_wrapper = stop_wrapper_path.read_text(encoding="utf-8")
    message_wrapper = (mac_home / "bin" / "openclaw-message").read_text(
        encoding="utf-8"
    )
    agent_wrapper = (mac_home / "bin" / "openclaw-agent").read_text(
        encoding="utf-8"
    )
    managed_entrypoint = (managed / "entrypoint.sh").read_text(encoding="utf-8")
    assert "sandbox create" in wrapper
    assert "-- /bin/bash /home/sandbox/.config/mac-openclaw/entrypoint.sh" in wrapper
    assert managed_entrypoint.startswith("#!/bin/bash\nset -euo pipefail\n")
    assert "sandbox delete" in stop_wrapper
    assert "pgrep -x openclaw" not in stop_wrapper
    assert "trap cleanup EXIT" in wrapper
    assert "stop_gateway" in wrapper
    subprocess.run(["bash", "-n", str(wrapper_path)], check=True, timeout=10)
    subprocess.run(["bash", "-n", str(stop_wrapper_path)], check=True, timeout=10)
    assert "set -a; . /home/sandbox/.config/mac-openclaw/runtime.env; set +a" in (
        message_wrapper
    )
    assert "/bin/bash -lc" in message_wrapper
    assert "/usr/local/bin/openclaw agent" in agent_wrapper
    assert "/bin/bash -lc" in agent_wrapper
    assert "set -a; . /home/sandbox/.config/mac-openclaw/runtime.env; set +a" in (
        INSTALLER.read_text(encoding="utf-8")
    )
    assert "nemoclaw" not in wrapper.lower()
    assert all(secret not in wrapper for secret in secrets)

    rendered_policy = (mac_home / "openclaw" / "openclaw-policy.yaml").read_text(
        encoding="utf-8"
    )
    assert "host: 100.64.0.1" in rendered_policy
    assert "port: 8789" in rendered_policy
    assert "__MAC_" not in rendered_policy


def test_prepare_migrates_legacy_slack_routing_into_openclaw_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    legacy_home = home / ".hermes"
    legacy_home.mkdir(parents=True)
    (legacy_home / "slack_home_channels.json").write_text(
        json.dumps(
            [
                {
                    "name": "offtera",
                    "team_id": "T123",
                    "channel_id": "C123HOME",
                    "channel_name": "#rockyandfriends",
                    "ignored_secret": "must-not-migrate",
                }
            ]
        ),
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_LIVE_CANARY": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_PUBLIC_IDENTITY": "mac-hive",
        "MAC_OPENCLAW_SLACK_ACCOUNT_ID": "offtera",
        "MAC_OPENCLAW_HOME_CHANNEL": "rockyandfriends",
        "MAC_OPENCLAW_SLACK_BOT_TOKEN": "xoxb-placeholder",
        "MAC_OPENCLAW_SLACK_APP_TOKEN": "xapp-placeholder",
    }

    result = subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    migrated = mac_home / "openclaw" / "slack_home_channels.json"
    rows = json.loads(migrated.read_text(encoding="utf-8"))
    assert rows == [
        {
            "channel_id": "C123HOME",
            "channel_name": "#rockyandfriends",
            "name": "offtera",
            "team_id": "T123",
        }
    ]
    assert migrated.stat().st_mode & 0o777 == 0o600
    assert "ignored_secret" not in migrated.read_text(encoding="utf-8")
    assert "migrated legacy Slack channel routing" in result.stdout
    assert (mac_home / "openclaw" / "home-channel-target").read_text(
        encoding="utf-8"
    ).strip() == "channel:C123HOME"


def test_verify_waits_for_new_sandbox_and_gateway_health(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    counter = tmp_path / "attempts"
    openshell = bin_dir / "openshell"
    openshell.write_text(
        "#!/bin/sh\n"
        "counter=${MAC_TEST_ATTEMPTS:?}\n"
        "attempts=$(cat \"$counter\" 2>/dev/null || echo 0)\n"
        "case \"$1:$2\" in\n"
        "  sandbox:get)\n"
        "    attempts=$((attempts + 1)); echo \"$attempts\" > \"$counter\"\n"
        "    [ \"$attempts\" -ge 3 ]\n"
        "    ;;\n"
        "  sandbox:exec)\n"
        "    case \"$*\" in\n"
        "      *'channels status'*) printf '%s\\n' '{\"channelAccounts\": {}}' ;;\n"
        "      *) : ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    openshell.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": str(openshell),
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_headless",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_headless",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_VERIFY_STARTUP_TIMEOUT": "3",
        "MAC_OPENCLAW_VERIFY_STARTUP_INTERVAL": "0",
        "MAC_TEST_ATTEMPTS": str(counter),
    }
    subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    result = subprocess.run(
        [str(INSTALLER), "verify"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    assert int(counter.read_text(encoding="utf-8")) >= 3
    assert "verified stock OpenClaw headless runtime" in result.stdout
    pending = json.loads(
        (mac_home / "openclaw" / "verification-pending.json").read_text(
            encoding="utf-8"
        )
    )
    assert pending["openclaw_runtime"]["verified"] is True


def test_fleet_deploy_selects_stock_openclaw_on_every_supervisor() -> None:
    config = FLEET_CONFIG.read_text(encoding="utf-8")
    deploy = DEPLOY.read_text(encoding="utf-8")
    unit = SYSTEMD_UNIT.read_text(encoding="utf-8")
    assert "gateway_impl: openclaw" in config
    assert "openclaw)\n      install_linux_openclaw_service" in deploy
    assert "install_darwin_openclaw_service" in deploy
    assert "OPENCLAW_SUPERVISORD_PROG" in deploy
    assert "verify_openclaw_gateway" in deploy
    assert "finalize_openclaw_gateway" in deploy
    assert "rollback_openclaw_gateway" in deploy
    assert "MAC_DEPLOY_OPENCLAW_LIVE_CANARY" in deploy
    assert "MAC_WORKER_RESOURCES_FILE" in deploy
    assert "representation_mode: delegated" in config
    assert "OPENCLAW_REPRESENTATION_MODE" in deploy
    assert "disable --now \"$HERMES_SERVICE_NAME\"" in deploy
    assert "ExecStart=__MAC_HOME__/bin/openclaw-gateway" in unit
    assert "ExecStop=__MAC_HOME__/bin/openclaw-gateway-stop" in unit
    assert "ExecStopPost=__MAC_HOME__/bin/openclaw-gateway-stop" in unit
    assert "User=__MAC_USER__" in unit


def test_openclaw_verification_probes_then_advertises_after_exclusive_cutover() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "validate-openclaw-channel-status.py" in installer
    assert "service-advertisement.json" in installer
    assert '"schema": "mac.chat_gateway_service.v1"' in installer
    assert '"provider": "openshell"' in installer
    assert "--channel slack" in installer
    assert "--channel telegram" in installer
    assert "--dry-run --json" in installer
    assert 'rm -f "$ADVERTISEMENT_PATH" "$VERIFICATION_RECORD_PATH"' in installer
    assert '"schema": "mac.gateway_ownership.v1"' in installer
    assert '"exclusive_channel_owner"' in installer
    assert 'finalize) finalize' in installer
    assert '"endpoint": "openshell://%s"' in installer
    assert '"access": "sandbox_exec"' in installer
    assert "http://127.0.0.1:${GATEWAY_PORT}/healthz" not in installer


def test_finalize_publishes_only_after_legacy_gateways_are_inactive(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    bin_dir = tmp_path / "bin"
    openclaw_home.mkdir(parents=True)
    bin_dir.mkdir()
    pending = {
        "openclaw_runtime": {"implementation": "openclaw", "verified": True},
        "chat_gateway": {"implementation": "openclaw", "verified": True},
    }
    (openclaw_home / "verification-pending.json").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    sudo = bin_dir / "sudo"
    sudo.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    sudo.chmod(0o700)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *-openclaw-gateway.service) echo active; exit 0 ;;\n"
        "  *) echo inactive; exit 3 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SUPERVISOR": "systemd",
    }

    subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    advertisement = json.loads(
        (openclaw_home / "service-advertisement.json").read_text(encoding="utf-8")
    )
    assert advertisement["gateway_ownership"]["exclusive"] is True
    assert advertisement["gateway_ownership"]["services"] == {
        "openclaw": "active",
        "hermes": "inactive",
        "nemoclaw": "inactive",
    }
    assert advertisement["openclaw_runtime"]["exclusive_service_owner"] is True
    assert advertisement["chat_gateway"]["exclusive_channel_owner"] is True
    assert not (openclaw_home / "verification-pending.json").exists()


def test_prepare_supports_verified_headless_openclaw_runtime(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_headless",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_headless",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_REPRESENTED_BY": "mac-hive",
        # Stale rollback credentials must not activate channels without a
        # logical public identity assignment.
        "SLACK_BOT_TOKEN": "xoxb-stale",
        "SLACK_APP_TOKEN": "xapp-stale",
    }
    subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    config = json.loads(
        (mac_home / "openclaw" / "managed" / "openclaw.json").read_text()
    )
    runtime = (mac_home / "openclaw" / "managed" / "runtime.env").read_text()
    workspace = (mac_home / "openclaw" / "workspace" / "AGENTS.md").read_text()
    assert config["channels"] == {}
    assert config["plugins"]["entries"] == {
        "slack": {"enabled": False},
        "telegram": {"enabled": False},
    }
    assert "SLACK_" not in runtime
    assert "TELEGRAM_" not in runtime
    assert "Representation mode: delegated" in workspace
    installer = INSTALLER.read_text(encoding="utf-8")
    assert 'MAC_OPENCLAW_CHANNELS=""' in installer
    assert "local channels=()" not in installer
    assert 'printf \'%s\' "${channels[*]}"' not in installer


def test_public_identity_without_any_channel_credentials_fails_closed(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(home / ".mac"),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_no_channels",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_no_channels",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_PUBLIC_IDENTITY": "mac-hive",
    }

    result = subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert result.returncode != 0
    assert "has no configured channel credentials" in result.stderr


def test_shell_artifacts_parse() -> None:
    for script in (INSTALLER, DEPLOY):
        subprocess.run(["bash", "-n", str(script)], check=True, timeout=10)


def test_finalize_supervisord_nemoclaw_no_such_process_yields_not_installed(
    tmp_path: Path,
) -> None:
    """supervisorctl returning 'no such process' for the nemoclaw program must
    set services.nemoclaw == not_installed and leave exclusive == True."""
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    bin_dir = tmp_path / "bin"
    openclaw_home.mkdir(parents=True)
    bin_dir.mkdir()
    pending = {
        "openclaw_runtime": {"implementation": "openclaw", "verified": True},
        "chat_gateway": {"implementation": "openclaw", "verified": True},
    }
    (openclaw_home / "verification-pending.json").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    sudo = bin_dir / "sudo"
    sudo.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    sudo.chmod(0o700)
    supervisorctl = bin_dir / "supervisorctl"
    supervisorctl.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *-openclaw-gateway) echo 'mac-openclaw-gateway     RUNNING   pid 1234'; exit 0 ;;\n"
        "  *-hermes-gateway)   echo 'mac-hermes-gateway       STOPPED'; exit 0 ;;\n"
        "  *-nemoclaw-gateway) echo 'mac-nemoclaw-gateway: ERROR (no such process)'; exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    supervisorctl.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SUPERVISOR": "supervisord",
    }

    subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    advertisement = json.loads(
        (openclaw_home / "service-advertisement.json").read_text(encoding="utf-8")
    )
    assert advertisement["gateway_ownership"]["exclusive"] is True
    assert advertisement["gateway_ownership"]["services"]["nemoclaw"] == "not_installed"
    assert not (openclaw_home / "verification-pending.json").exists()


def test_finalize_systemd_nemoclaw_unknown_unit_yields_not_installed(
    tmp_path: Path,
) -> None:
    """systemctl is-active returning 'unknown' for the nemoclaw unit must be
    normalized to not_installed in the service advertisement."""
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    bin_dir = tmp_path / "bin"
    openclaw_home.mkdir(parents=True)
    bin_dir.mkdir()
    pending = {
        "openclaw_runtime": {"implementation": "openclaw", "verified": True},
        "chat_gateway": {"implementation": "openclaw", "verified": True},
    }
    (openclaw_home / "verification-pending.json").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    sudo = bin_dir / "sudo"
    sudo.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    sudo.chmod(0o700)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *-openclaw-gateway.service) echo active; exit 0 ;;\n"
        "  *-hermes-gateway.service)   echo inactive; exit 3 ;;\n"
        "  *-nemoclaw-gateway.service) echo unknown; exit 4 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SUPERVISOR": "systemd",
    }

    subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    advertisement = json.loads(
        (openclaw_home / "service-advertisement.json").read_text(encoding="utf-8")
    )
    assert advertisement["gateway_ownership"]["exclusive"] is True
    assert advertisement["gateway_ownership"]["services"]["nemoclaw"] == "not_installed"
    assert not (openclaw_home / "verification-pending.json").exists()
