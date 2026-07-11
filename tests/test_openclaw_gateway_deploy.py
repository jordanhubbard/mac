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


def _seed_hermes_identity(home: Path, name: str = "Test Agent") -> None:
    hermes = home / ".hermes"
    memories = hermes / "memories"
    memories.mkdir(parents=True)
    (hermes / "SOUL.md").write_text(f"# {name}\n\nDistinct test soul.\n", encoding="utf-8")
    (memories / "USER.md").write_text("# User\n\nLearn preferences from evidence.\n", encoding="utf-8")
    (memories / "MEMORY.md").write_text("# Memory\n\nContinuity seed.\n", encoding="utf-8")


def test_stock_openclaw_artifacts_are_pinned_and_do_not_invoke_nemoclaw() -> None:
    assert INSTALLER.stat().st_mode & 0o111
    container = CONTAINERFILE.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "ghcr.io/openclaw/openclaw:2026.6.11@sha256:" in container
    assert 'OPENCLAW_SLACK_PLUGIN_VERSION="2026.6.11"' in container
    assert 'ARG MAC_OPENCLAW_IMAGE_REVISION=' in container
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
    assert 'OPENCLAW_IMAGE_REVISION="10"' in installer
    assert 'OPENCLAW_IMAGE="localhost/mac-openclaw:${OPENCLAW_VERSION}-mac.${OPENCLAW_IMAGE_REVISION}"' in installer
    assert "/Applications/Docker.app/Contents/Resources/bin/docker" in installer
    assert 'docker_bin="$(find_docker)"' in installer
    assert 'docker_path="$(dirname "$docker_bin"):$PATH"' in installer
    assert 'PATH="$docker_path" "$docker_bin" build --pull' in installer
    assert 'BUILD_CONTEXT="${MAC_OPENCLAW_BUILD_CONTEXT:-$MAC_SRC}"' in installer
    assert '"$BUILD_CONTEXT"' in installer
    assert "USER sandbox" in container
    assert "install -m 0644 -o sandbox -g sandbox /dev/null /home/sandbox/.profile" in container
    assert "install -m 0644 -o sandbox -g sandbox /dev/null /home/sandbox/.bashrc" in container
    assert "nemoclaw gateway" not in container.lower()
    assert "/nemoclaw" not in container.lower()
    # The cutover audit must name the legacy service to prove it is inactive,
    # but the stock installer must never execute a NemoClaw binary.
    assert "/usr/local/bin/nemoclaw" not in installer.lower()
    assert "exec nemoclaw" not in installer.lower()
    assert 'image_revision" = "$OPENCLAW_IMAGE_REVISION"' in installer
    assert installer.count("/bin/bash --noprofile --norc -c") >= 2
    assert "/usr/local/bin/mac-verify-bash-contract" in installer
    assert "migrate-hermes-continuity.py" in installer
    assert "apply-cron-plan.mjs" in container
    assert "curiosity-sidecar.py /usr/local/bin/curiosity" in container
    assert "/opt/mac-openclaw/plugins/mac-continuity" in container


def test_openclaw_policy_is_deny_by_default_and_narrowly_allows_required_services() -> None:
    text = POLICY.read_text(encoding="utf-8")
    assert "run_as_user: sandbox" in text
    read_only, read_write = text.split("  read_write:", maxsplit=1)
    assert "/home/sandbox/.config/mac-openclaw" not in read_only
    assert "/home/sandbox/.config/mac-openclaw" in read_write
    assert "- /sandbox" in read_write
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


def test_mac_continuity_plugin_registers_runtime_hook_and_tools() -> None:
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").as_uri()
    script = f"""
      const mod = await import({json.dumps(plugin)});
      const hooks = new Map();
      const tools = new Map();
      globalThis.fetch = async () => ({{
        ok: true,
        json: async () => ({{
          mood_prompt: "Current mood: warm",
          memories: [{{summary: "durable fact", score: 0.9}}],
        }}),
      }});
      const api = {{
        pluginConfig: {{maxMemories: 5, timeoutMs: 1000}},
        logger: {{warn: () => {{}}}},
        on: (name, handler) => hooks.set(name, handler),
        registerTool: (tool) => tools.set(tool.name, tool),
      }};
      mod.default.register(api);
      if (!hooks.has("before_prompt_build")) process.exit(2);
      if (!["mac_memory_recall", "mac_memory_store", "mac_mood_current", "mac_mood_set", "mac_mood_clear", "mac_config_flag_list", "mac_config_flag_set", "mac_config_flag_clear", "mac_image_generate", "curiosity_candidate_submit", "curiosity_candidates_list", "curiosity_abuse_frame"].every((name) => tools.has(name))) process.exit(3);
      const result = await hooks.get("before_prompt_build")({{prompt: "what matters?"}});
      if (!result.prependContext.includes("Current mood: warm")) process.exit(4);
      if (!result.prependContext.includes("durable fact")) process.exit(5);
    """
    env = {
        **os.environ,
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_CONTROL_URL": "http://hub:8789",
        "MAC_OPENCLAW_ROUTER_API_KEY": "test-token",
    }
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_mac_image_generate_posts_to_hub_media_router_and_writes_png() -> None:
    # 1x1 PNG, base64 — what the hub /v1/media/image.generate returns.
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").as_uri()
    script = f"""
      const {{readFileSync}} = await import("node:fs");
      const mod = await import({json.dumps(plugin)});
      const tools = new Map();
      let captured = null;
      globalThis.fetch = async (url, opts) => {{
        captured = {{url: String(url), method: opts.method, body: JSON.parse(opts.body)}};
        return {{ok: true, json: async () => ({{
          artifacts: [{{base64: {json.dumps(png_b64)}}}],
          provider: "nvidia", model: "black-forest-labs/flux.1-schnell",
        }})}};
      }};
      const api = {{
        pluginConfig: {{}}, logger: {{warn: () => {{}}}},
        on: () => {{}}, registerTool: (t) => tools.set(t.name, t),
      }};
      mod.default.register(api);
      const tool = tools.get("mac_image_generate");
      if (!tool) process.exit(2);
      const out = await tool.execute("id", {{prompt: "a red circle"}});
      // Must hit the hub media router, NOT any nvidia endpoint directly.
      if (!captured.url.endsWith("/v1/media/image.generate")) process.exit(3);
      if (captured.url.includes("nvidia") || captured.url.includes("build.nvidia")) process.exit(4);
      if (captured.body.prompt !== "a red circle") process.exit(5);
      const payload = JSON.parse(out.content[0].text);
      if (!payload.ok || !payload.path.endsWith(".png")) process.exit(6);
      const bytes = readFileSync(payload.path);
      if (bytes.length < 60 || bytes[0] !== 0x89 || bytes[1] !== 0x50) process.exit(7);
    """
    env = {
        **os.environ,
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_CONTROL_URL": "http://hub:8789",
        "MAC_OPENCLAW_ROUTER_API_KEY": "test-token",
    }
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        env=env, check=True, text=True, capture_output=True, timeout=10,
    )


def test_prepare_renders_valid_secret_ref_config_without_log_leaks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    openclaw_home.mkdir(parents=True)
    _seed_hermes_identity(home)
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
                    "name": "omgjkh",
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
        "xox" + "b-second-workspace-placeholder",
        "xap" + "p-second-workspace-placeholder",
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
        "MAC_OPENCLAW_SLACK_ACCOUNT_IDS": "offtera,omgjkh",
        "MAC_OPENCLAW_HOME_CHANNEL": "rockyandfriends",
        "MAC_OPENCLAW_SLACK_BOT_TOKEN": secrets[1],
        "MAC_OPENCLAW_SLACK_APP_TOKEN": secrets[2],
        "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN": secrets[4],
        "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN": secrets[5],
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
    second_slack = config["channels"]["slack"]["accounts"]["omgjkh"]
    telegram = config["channels"]["telegram"]["accounts"]["default"]
    # Per-tool-call progress narration ("Tidepooling… Exec") is suppressed —
    # it's noise in a human chat channel.
    assert config["channels"]["slack"]["streaming"]["progress"]["toolProgress"] is False
    assert config["channels"]["slack"]["streaming"]["preview"]["toolProgress"] is False
    assert config["channels"]["telegram"]["streaming"]["progress"]["toolProgress"] is False
    assert provider["apiKey"] == "${MAC_OPENCLAW_ROUTER_API_KEY}"
    assert provider["headers"] == {
        "x-mac-agent-id": "agent_test",
        "x-mac-hermes-instance-id": "hermes_test",
    }
    assert slack["botToken"]["id"] == "MAC_OPENCLAW_SLACK_OFFTERA_BOT_TOKEN"
    assert slack["appToken"]["id"] == "MAC_OPENCLAW_SLACK_OFFTERA_APP_TOKEN"
    assert second_slack["botToken"]["id"] == "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN"
    assert second_slack["appToken"]["id"] == "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN"
    assert telegram["botToken"]["id"] == "MAC_OPENCLAW_TELEGRAM_BOT_TOKEN"
    assert telegram["dmPolicy"] == "pairing"
    assert telegram["groupPolicy"] == "allowlist"
    assert config["plugins"]["allow"] == ["mac-continuity", "slack", "telegram"]
    assert config["plugins"]["entries"]["slack"] == {"enabled": True}
    assert config["plugins"]["entries"]["telegram"] == {"enabled": True}
    assert config["plugins"]["entries"]["mac-continuity"]["enabled"] is True
    assert config["plugins"]["entries"]["mac-continuity"]["hooks"] == {
        "allowConversationAccess": True,
        "allowPromptInjection": True,
    }
    assert config["plugins"]["load"]["paths"] == [
        "/opt/mac-openclaw/plugins/mac-continuity"
    ]
    assert config["agents"]["defaults"]["workspace"] == "/sandbox/workspace"
    assert "memorySearch" not in config["agents"]["defaults"]
    assert config["plugins"]["slots"]["memory"] == "mac-continuity"
    assert config["plugins"]["entries"]["mac-continuity"]["config"] == {
        "maxMemories": 5,
        "timeoutMs": 10000,
    }
    assert config["tools"]["sessions"]["visibility"] == "agent"
    assert config["gateway"]["auth"]["token"]["id"] == "OPENCLAW_GATEWAY_TOKEN"
    assert all(secret not in config_path.read_text(encoding="utf-8") for secret in secrets)
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert runtime_path.stat().st_mode & 0o777 == 0o600
    runtime_keys = {
        line.split("=", 1)[0]
        for line in runtime_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert {
        "MAC_OPENCLAW_AGENT_ID",
        "MAC_OPENCLAW_CONTROL_URL",
        "MAC_OPENCLAW_WORKSPACE",
        "MAC_OPENCLAW_SLACK_OFFTERA_APP_TOKEN",
        "MAC_OPENCLAW_SLACK_OFFTERA_BOT_TOKEN",
        "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN",
        "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN",
        "MAC_OPENCLAW_TELEGRAM_BOT_TOKEN",
    } <= runtime_keys
    assert {
        "SLACK_APP_TOKEN",
        "SLACK_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    }.isdisjoint(runtime_keys)
    assert wrapper_path.stat().st_mode & 0o777 == 0o700
    assert stop_wrapper_path.stat().st_mode & 0o777 == 0o700
    curiosity_wrapper = mac_home / "bin" / "curiosity"
    assert curiosity_wrapper.stat().st_mode & 0o777 == 0o700
    assert "/usr/local/bin/curiosity" in curiosity_wrapper.read_text(encoding="utf-8")
    cron_plan = json.loads((managed / "cron-plan.json").read_text(encoding="utf-8"))
    curiosity_job = next(
        job for job in cron_plan["jobs"] if job["name"] == "MAC continuous curiosity review"
    )
    assert curiosity_job["enabled"] is True
    assert "curiosity_candidate_submit" in curiosity_job["message"]
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
    # GPU passthrough is self-detecting per host: --gpu on CUDA machines, a
    # no-op on GPU-less hosts (Apple Silicon). Scalar (not array) so an empty
    # value under `set -u` doesn't abort bash 3.2 on macOS.
    assert "nvidia-smi -L" in wrapper
    assert "GPU_ARG=--gpu" in wrapper
    assert "sandbox create $GPU_ARG" in wrapper
    assert "-- env HOME=/tmp BASH_ENV=/dev/null /bin/bash --noprofile --norc /home/sandbox/.config/mac-openclaw/entrypoint.sh" in wrapper
    assert managed_entrypoint.startswith("#!/bin/bash\nset -euo pipefail\n")
    assert "sandbox delete" in stop_wrapper
    assert "sandbox download" in stop_wrapper
    assert "/sandbox/workspace" in stop_wrapper
    assert "/sandbox/state" in stop_wrapper
    assert "pgrep -x openclaw" not in stop_wrapper
    assert "trap cleanup EXIT" in wrapper
    assert "stop_gateway" in wrapper
    assert '--upload "$WORKSPACE:/sandbox"' in wrapper
    assert '--upload "$STATE:/sandbox"' in wrapper
    subprocess.run(["bash", "-n", str(wrapper_path)], check=True, timeout=10)
    subprocess.run(["bash", "-n", str(stop_wrapper_path)], check=True, timeout=10)
    assert "set -a; . /home/sandbox/.config/mac-openclaw/runtime.env; set +a" in (
        message_wrapper
    )
    assert "env HOME=/tmp BASH_ENV=/dev/null /bin/bash --noprofile --norc -c" in message_wrapper
    assert "/usr/local/bin/openclaw agent" in agent_wrapper
    assert "env HOME=/tmp BASH_ENV=/dev/null /bin/bash --noprofile --norc -c" in agent_wrapper
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
    calls = tmp_path / "calls"
    openclaw_home = mac_home / "openclaw"
    openclaw_home.mkdir(parents=True)
    _seed_hermes_identity(home)
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
                    "name": "omgjkh",
                    "team_id": "T456",
                    "channel_id": "C456HOME",
                    "channel_name": "#rockyandfriends",
                },
            ]
        ),
        encoding="utf-8",
    )
    openshell = bin_dir / "openshell"
    openshell.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$MAC_TEST_CALLS\"\n"
        "counter=${MAC_TEST_ATTEMPTS:?}\n"
        "attempts=$(cat \"$counter\" 2>/dev/null || echo 0)\n"
        "case \"$1:$2\" in\n"
        "  sandbox:get)\n"
        "    attempts=$((attempts + 1)); echo \"$attempts\" > \"$counter\"\n"
        "    [ \"$attempts\" -ge 3 ]\n"
        "    ;;\n"
        "  sandbox:exec)\n"
        "    case \"$*\" in\n"
        "      *'channels status'*) printf '%s\\n' '{\"channelAccounts\": {\"slack\": [{\"accountId\": \"offtera\", \"enabled\": true, \"configured\": true, \"probe\": {\"ok\": true, \"team\": {\"id\": \"T123\"}}}, {\"accountId\": \"omgjkh\", \"enabled\": true, \"configured\": true, \"probe\": {\"ok\": true, \"team\": {\"id\": \"T456\"}}}]}, \"channelDefaultAccountId\": {\"slack\": \"offtera\"}}' ;;\n"
        "      *'plugins inspect mac-continuity'*) printf '%s\\n' '{\"plugin\": {\"imported\": true, \"status\": \"loaded\", \"toolNames\": [\"memory_search\", \"memory_get\", \"memory_store\", \"mac_memory_recall\", \"mac_memory_store\", \"mac_mood_current\", \"mac_mood_set\", \"mac_mood_clear\", \"mac_config_flag_list\", \"mac_config_flag_set\", \"mac_config_flag_clear\", \"mac_image_generate\", \"curiosity_candidate_submit\", \"curiosity_candidates_list\", \"curiosity_abuse_frame\"], \"hookNames\": [\"before_prompt_build\"]}}' ;;\n"
        "      *'curiosity verify'*) printf '%s\\n' '{\"valid\": true, \"events\": 0}' ;;\n"
        "      *'curiosity abuse-frame'*) printf '%s\\n' '{\"possible_false_equivalence\": true}' ;;\n"
        "      *'memory status'*) printf '%s\\n' '{\"files\": 3}' ;;\n"
        "      *'memory search'*) printf '%s\\n' \"$*\" ;;\n"
        "      *'Read your workspace IDENTITY.md'*) printf '%s\\n' '{\"result\": \"mac-hive\"}' ;;\n"
        "      *'openclaw agent'*) printf '%s\\n' '{\"result\": \"MAC_OPENCLAW_CANARY_OK\"}' ;;\n"
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
        "MAC_OPENCLAW_PUBLIC_IDENTITY": "mac-hive",
        "MAC_OPENCLAW_SLACK_ACCOUNT_ID": "offtera",
        "MAC_OPENCLAW_SLACK_ACCOUNT_IDS": "offtera,omgjkh",
        "MAC_OPENCLAW_HOME_CHANNEL": "rockyandfriends",
        "MAC_OPENCLAW_SLACK_BOT_TOKEN": "xoxb-primary-placeholder",
        "MAC_OPENCLAW_SLACK_APP_TOKEN": "xapp-primary-placeholder",
        "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN": "xoxb-second-placeholder",
        "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN": "xapp-second-placeholder",
        "MAC_OPENCLAW_LIVE_CANARY": "1",
        "MAC_OPENCLAW_VERIFY_STARTUP_TIMEOUT": "3",
        "MAC_OPENCLAW_VERIFY_STARTUP_INTERVAL": "0",
        "MAC_TEST_ATTEMPTS": str(counter),
        "MAC_TEST_CALLS": str(calls),
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
    assert "verified stock OpenClaw runtime" in result.stdout
    pending = json.loads(
        (mac_home / "openclaw" / "verification-pending.json").read_text(
            encoding="utf-8"
        )
    )
    assert pending["openclaw_runtime"]["verified"] is True
    assert pending["chat_gateway"]["channels"]["slack"] == {
        "account_id": "offtera",
        "account_ids": ["offtera", "omgjkh"],
        "enabled": True,
        "transport": "socket",
    }
    calls_text = calls.read_text(encoding="utf-8")
    assert "--account offtera --target channel:C123HOME" in calls_text
    assert "--account omgjkh --target channel:C456HOME" in calls_text


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
    _seed_hermes_identity(home, "Headless Test")
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
    assert config["plugins"]["entries"]["slack"] == {"enabled": False}
    assert config["plugins"]["entries"]["telegram"] == {"enabled": False}
    assert config["plugins"]["entries"]["mac-continuity"]["enabled"] is True
    assert "SLACK_APP_TOKEN" not in runtime
    assert "SLACK_BOT_TOKEN" not in runtime
    assert "TELEGRAM_" not in runtime
    assert "Representation mode: delegated" in workspace
    # SOUL.md is wired as the authoritative persona, first and prominently, so
    # the OpenClaw runtime actually embodies the agent's personality instead of
    # leaving SOUL.md an orphaned workspace file (rocky-personality-flattening).
    assert "SOUL.md" in workspace
    who, modes = workspace.split("## Modes you can invoke", maxsplit=1)
    assert "Who you are" in who
    assert "authoritative" in who
    # The curiosity / moral-clarity block is demoted from default temperament to
    # invocable modes — it must NOT read as "this is how you always are."
    assert "Be endlessly curious, ruthless toward bad data" not in workspace
    assert "not your default temperament" in workspace
    assert "Angry Librarian mode" in modes
    assert "false equivalence" in modes
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


def test_finalize_systemd_hermes_failed_state_normalized_to_inactive(
    tmp_path: Path,
) -> None:
    """systemctl is-active returning 'failed' (exit 3) for the hermes unit must
    be normalized to inactive; finalize() must succeed and publish
    services.hermes == 'inactive' (the normalized value) with exclusive == True."""
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
    # hermes returns "failed" with exit 3 — exactly what systemd emits when a
    # unit stopped with an error but reset-failed was not yet called.
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *-openclaw-gateway.service) echo active; exit 0 ;;\n"
        "  *-hermes-gateway.service)   echo failed; exit 3 ;;\n"
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

    result = subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, (
        f"finalize() must succeed when hermes is in 'failed' state (normalized to inactive);\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    advertisement = json.loads(
        (openclaw_home / "service-advertisement.json").read_text(encoding="utf-8")
    )
    assert advertisement["gateway_ownership"]["exclusive"] is True
    # The installer normalizes failed -> inactive before recording state.
    assert advertisement["gateway_ownership"]["services"]["hermes"] == "inactive"
    assert advertisement["gateway_ownership"]["services"]["nemoclaw"] == "not_installed"
    assert not (openclaw_home / "verification-pending.json").exists()


def test_finalize_systemd_hermes_active_still_dies(
    tmp_path: Path,
) -> None:
    """When systemctl is-active returns 'active' for the hermes unit, finalize()
    must exit with a non-zero code and must NOT publish a service advertisement."""
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
    # hermes is still active — OpenClaw cutover has not completed.
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *-openclaw-gateway.service) echo active; exit 0 ;;\n"
        "  *-hermes-gateway.service)   echo active; exit 0 ;;\n"
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

    result = subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode != 0, (
        "finalize() must fail (non-zero exit) when hermes is still active after OpenClaw cutover"
    )
    assert not (openclaw_home / "service-advertisement.json").exists(), (
        "service-advertisement.json must NOT be written when hermes is still active"
    )
