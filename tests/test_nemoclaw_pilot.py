"""Contract tests for the NemoClaw pilot install (deploy/nemoclaw/).

Checks cover:
  - install-nemoclaw-pilot.sh structure, safety, and secret hygiene.
  - config.yaml template validity (provider, static headers, port, Slack).
  - docker-compose.yaml structure (digest pin, isolation, runtime context env).
  - slack-account.example.json format (Socket Mode).
  - AGENTS.md injection documentation (runtime context mechanism).
  - RUNBOOK.md completeness (Node install, Slack setup, coexistence).
  - No operator identity in checked-in files (role/placeholder only).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
NEMOCLAW_DIR = ROOT / "deploy" / "nemoclaw"
INSTALL_SCRIPT = NEMOCLAW_DIR / "install-nemoclaw-pilot.sh"
CONFIG_YAML = NEMOCLAW_DIR / "config.yaml"
COMPOSE_YAML = NEMOCLAW_DIR / "docker-compose.yaml"
SLACK_EXAMPLE = NEMOCLAW_DIR / "slack-account.example.json"
AGENTS_MD = NEMOCLAW_DIR / "AGENTS.md"
RUNBOOK_MD = NEMOCLAW_DIR / "RUNBOOK.md"


# ---------------------------------------------------------------------------
# install-nemoclaw-pilot.sh
# ---------------------------------------------------------------------------


def test_install_script_exists_and_is_executable():
    assert INSTALL_SCRIPT.exists(), "install-nemoclaw-pilot.sh must exist"
    assert INSTALL_SCRIPT.stat().st_mode & 0o111, "install-nemoclaw-pilot.sh must be executable"


def test_install_script_is_bash_and_strict():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash"), "script must start with #!/usr/bin/env bash"
    assert "set -euo pipefail" in text, "script must use set -euo pipefail"


def test_install_script_installs_node_22_locally():
    """Node 22 must be installed task-local, never with sudo."""
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    # Must reference node version 22 or higher.
    assert re.search(r"NODE.*VERSION.*22|NEMOCLAW_NODE_VERSION.*22|node.*22", text), (
        "install script must target Node 22"
    )
    # Must not use sudo for Node install.
    # The only allowed sudo usage would be for service install, not Node.
    node_install_section = text.split("install_node_local")[1].split("install_nemoclaw_cli")[0]
    assert "sudo" not in node_install_section, "Node install must not use sudo"


def test_install_script_installs_node_task_local_not_global():
    """Node must land under MAC_HOME, not /usr/local or system paths."""
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "NEMOCLAW_NODE_PREFIX" in text, "script must define a task-local node prefix"
    assert "MAC_HOME" in text and "nemoclaw-node" in text, (
        "node prefix must be under MAC_HOME (task-local)"
    )
    # Must not install to /usr/local or use global npm install.
    assert "npm install -g" not in text, "must not use global npm install (-g)"
    # Must use --prefix to scope npm install (script uses "${npm_bin}" install --prefix ...).
    assert "install --prefix" in text, "nemoclaw CLI must be installed with npm --prefix"


def test_install_script_installs_nemoclaw_cli():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "nemoclaw" in text, "install script must install the nemoclaw CLI"
    assert "install_nemoclaw_cli" in text, "install script must define install_nemoclaw_cli()"


def test_install_script_configures_one_slack_workspace():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "configure_slack_workspace" in text, "must define configure_slack_workspace()"
    assert "slack_accounts.json" in text, "must write slack_accounts.json"
    assert "bot_token" in text, "must configure bot_token"
    assert "app_token" in text, "must configure app_token"
    # Must validate token shapes.
    assert "xoxb-" in text, "must validate bot_token starts with xoxb-"
    assert "xapp-" in text, "must validate app_token starts with xapp-"


def test_install_script_never_logs_secrets():
    """Tokens must come from env vars and must never be echoed or printed."""
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    # Tokens are read from environment variables, not hardcoded.
    assert "MAC_NEMOCLAW_SLACK_BOT_TOKEN" in text
    assert "MAC_NEMOCLAW_SLACK_APP_TOKEN" in text
    # The script must not echo or log the token values.
    for sensitive in ("bot_token", "app_token", "xoxb-", "xapp-"):
        # Allowed in validation comparisons; must NOT appear in log/echo lines.
        for line in text.splitlines():
            stripped = line.strip()
            if re.search(r"\blog\b.*" + re.escape(sensitive), stripped, re.IGNORECASE):
                assert False, (
                    f"Secret token '{sensitive}' must not be passed to log(): {stripped!r}"
                )


def test_install_script_points_provider_at_mac_router():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "MAC_NEMOCLAW_HUB_URL" in text, "must reference MAC_NEMOCLAW_HUB_URL"
    assert "x-mac-agent-id" in text, "must set x-mac-agent-id static header"
    assert "x-mac-hermes-instance-id" in text, "must set x-mac-hermes-instance-id static header"
    assert "MAC_NEMOCLAW_AGENT_ID" in text, "must reference MAC_NEMOCLAW_AGENT_ID"
    assert "MAC_NEMOCLAW_INSTANCE_ID" in text, "must reference MAC_NEMOCLAW_INSTANCE_ID"


def test_install_script_injects_mac_runtime_context():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "write_runtime_context" in text, "must define write_runtime_context()"
    assert "mac-runtime-context.md" in text, "must write mac-runtime-context.md"
    assert "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN" in text, (
        "must reference MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN env var"
    )
    # Runtime context must include agent identity, fleet, role, hub.
    ctx_section = text.split("write_runtime_context")[1].split("install_openshell_policy")[0]
    assert "Agent:" in ctx_section, "runtime context must include Agent field"
    assert "Fleet:" in ctx_section, "runtime context must include Fleet field"
    assert "Role: nemoclaw-gateway" in ctx_section, (
        "runtime context must declare nemoclaw-gateway role"
    )
    assert "Hub:" in ctx_section, "runtime context must include Hub field"


def test_install_script_preserves_existing_hermes_gateway():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    # Must have a step that verifies the existing gateway.
    assert "verify_existing_gateway" in text, "must define verify_existing_gateway()"
    # Must NOT stop or modify the existing gateway.
    assert "systemctl stop hermes" not in text, "must not stop the existing hermes gateway"
    assert "systemctl restart hermes" not in text, "must not restart the existing hermes gateway"
    # The NemoClaw pilot uses a distinct port.
    assert "18765" in text, "NemoClaw pilot must use port 18765 (distinct from existing gateway)"


def test_install_script_validates_required_env():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "validate_env" in text, "must define validate_env()"
    for var in (
        "MAC_NEMOCLAW_HUB_URL",
        "MAC_NEMOCLAW_AGENT_ID",
        "MAC_NEMOCLAW_INSTANCE_ID",
        "MAC_NEMOCLAW_SLACK_BOT_TOKEN",
        "MAC_NEMOCLAW_SLACK_APP_TOKEN",
        "MAC_NEMOCLAW_SLACK_WORKSPACE",
    ):
        assert var in text, f"install script must validate required env var {var}"


def test_install_script_installs_openshell_policy():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "install_openshell_policy" in text, "must define install_openshell_policy()"
    assert "mac-hermes-policy.yaml" in text, "must reference the policy template"
    assert "openshell" in text, "must write policy to openshell directory"


def test_install_script_includes_health_checks():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "health_check" in text, "must define health_check()"
    # Health check must verify key files exist.
    assert "config.yaml" in text, "health check must verify config.yaml"
    assert "slack_accounts.json" in text, "health check must verify slack_accounts.json"
    assert "mac-runtime-context.md" in text, "health check must verify mac-runtime-context.md"


def test_install_script_references_openclaw_digest_pin():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    # The script must reference the digest variable for the openclaw image.
    assert "NEMOCLAW_IMAGE_DIGEST" in text, (
        "install script must reference NEMOCLAW_IMAGE_DIGEST for the openclaw image"
    )
    assert "localhost/mac-hermes:net" in text, (
        "install script must reference the openclaw sandbox image tag"
    )


def test_install_script_no_operator_identity():
    """Checked-in scripts must use role names and placeholders, not real fleet identities."""
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    _FORBIDDEN = re.compile(
        r"(?<![A-Za-z0-9])(?:rocky|natasha|bullwinkle|madmax|puck|sparky|"
        r"worker2|jkh|hosta|hostb|hostc|hostd|hoste|hostf|devuser|agentuser)(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _FORBIDDEN.search(line)
        assert not m, (
            f"deploy/nemoclaw/install-nemoclaw-pilot.sh:{lineno}: "
            f"operator identity '{m.group(0)}' must not appear in a checked-in script"
        )


# ---------------------------------------------------------------------------
# config.yaml
# ---------------------------------------------------------------------------


def test_config_yaml_is_valid_yaml():
    text = CONFIG_YAML.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text)
    assert cfg is not None, "config.yaml must be valid YAML"


def test_config_yaml_has_custom_provider():
    text = CONFIG_YAML.read_text(encoding="utf-8")
    assert "provider: custom" in text, "config.yaml must set provider: custom"
    assert "mac-router" in text, "config.yaml must define mac-router custom provider"


def test_config_yaml_has_static_headers():
    text = CONFIG_YAML.read_text(encoding="utf-8")
    assert "x-mac-agent-id" in text, "config.yaml must declare x-mac-agent-id static header"
    assert "x-mac-hermes-instance-id" in text, (
        "config.yaml must declare x-mac-hermes-instance-id static header"
    )


def test_config_yaml_uses_placeholder_hub_url():
    text = CONFIG_YAML.read_text(encoding="utf-8")
    # Must reference the hub URL as a placeholder, not a real URL or token.
    assert "hub.example.internal" in text or "<" in text, (
        "config.yaml hub URL must be a placeholder"
    )


def test_config_yaml_has_nemoclaw_gateway_port():
    text = CONFIG_YAML.read_text(encoding="utf-8")
    assert "18765" in text, "config.yaml must configure NemoClaw on port 18765"


def test_config_yaml_has_slack_platform():
    text = CONFIG_YAML.read_text(encoding="utf-8")
    assert "slack:" in text, "config.yaml must configure Slack platform"
    assert "require_mention" in text, "config.yaml must set require_mention"
    assert "strict_mention" in text, "config.yaml must set strict_mention"


def test_config_yaml_no_hardcoded_secrets():
    text = CONFIG_YAML.read_text(encoding="utf-8")
    # Must not contain real tokens.
    assert "xoxb-" not in text, "config.yaml must not contain a real bot token"
    assert "xapp-" not in text, "config.yaml must not contain a real app token"
    for bad_pattern in (r"sk-[A-Za-z0-9]{20,}", r"Bearer [A-Za-z0-9._-]{20,}"):
        assert not re.search(bad_pattern, text), (
            f"config.yaml must not contain a hardcoded secret matching {bad_pattern}"
        )


# ---------------------------------------------------------------------------
# docker-compose.yaml
# ---------------------------------------------------------------------------


def test_docker_compose_is_valid_yaml():
    text = COMPOSE_YAML.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text)
    assert cfg is not None
    assert "services" in cfg


def test_docker_compose_has_nemoclaw_service():
    cfg = yaml.safe_load(COMPOSE_YAML.read_text(encoding="utf-8"))
    assert "nemoclaw-gateway" in cfg["services"], (
        "docker-compose.yaml must define a 'nemoclaw-gateway' service"
    )


def test_docker_compose_uses_openclaw_image():
    cfg = yaml.safe_load(COMPOSE_YAML.read_text(encoding="utf-8"))
    svc = cfg["services"]["nemoclaw-gateway"]
    assert svc.get("image") == "localhost/mac-hermes:net", (
        "nemoclaw-gateway must use the openclaw sandbox image localhost/mac-hermes:net"
    )


def test_docker_compose_has_digest_pin_comment():
    text = COMPOSE_YAML.read_text(encoding="utf-8")
    assert "sha256" in text, (
        "docker-compose.yaml must include a sha256 digest pin comment for the base image"
    )


def test_docker_compose_has_pilot_port():
    text = COMPOSE_YAML.read_text(encoding="utf-8")
    assert "18765" in text, "docker-compose.yaml must expose the pilot gateway port 18765"


def test_docker_compose_has_isolated_hermes_home():
    cfg = yaml.safe_load(COMPOSE_YAML.read_text(encoding="utf-8"))
    svc = cfg["services"]["nemoclaw-gateway"]
    env = svc.get("environment", {})
    env_str = COMPOSE_YAML.read_text(encoding="utf-8")
    assert "hermes-nemoclaw" in env_str, (
        "docker-compose.yaml must use an isolated HERMES_HOME (hermes-nemoclaw)"
    )


def test_docker_compose_sets_mac_router_env():
    text = COMPOSE_YAML.read_text(encoding="utf-8")
    assert "MAC_HERMES_GATEWAY_BASE_URL" in text or "OPENAI_BASE_URL" in text, (
        "docker-compose.yaml must set the MAC router base URL env var"
    )
    assert "MAC_HERMES_INSTANCE_ID" in text, "docker-compose.yaml must set MAC_HERMES_INSTANCE_ID"


def test_docker_compose_sets_runtime_context_env():
    text = COMPOSE_YAML.read_text(encoding="utf-8")
    assert "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN" in text, (
        "docker-compose.yaml must set MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN"
    )
    assert "MAC_HERMES_RUNTIME_CONTEXT_REQUIRED" in text, (
        "docker-compose.yaml must set MAC_HERMES_RUNTIME_CONTEXT_REQUIRED"
    )


def test_docker_compose_does_not_bind_host_hermes_home():
    """Pilot must not bind-mount the existing gateway's ~/.hermes."""
    text = COMPOSE_YAML.read_text(encoding="utf-8")
    # The existing hermes home is ~/.hermes; the pilot uses ~/.hermes-nemoclaw.
    # The volumes should only reference hermes-nemoclaw, not the bare .hermes.
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        # Allow hermes-nemoclaw; reject a bare /.hermes: pattern (host mount of main hermes home)
        if re.search(r"source:.*[/\"]\.hermes[^-]", line):
            assert False, (
                f"docker-compose.yaml:{lineno}: must not bind-mount the existing ~/.hermes; "
                f"use ~/.hermes-nemoclaw for isolation"
            )


def test_docker_compose_no_hardcoded_secrets():
    text = COMPOSE_YAML.read_text(encoding="utf-8")
    assert "xoxb-" not in text, "docker-compose.yaml must not contain a real bot token"
    assert "xapp-" not in text, "docker-compose.yaml must not contain a real app token"
    # API key placeholders are allowed but must be redacted.
    for bad in (r"sk-[A-Za-z0-9]{20,}",):
        assert not re.search(bad, text), (
            f"docker-compose.yaml must not contain a real API key matching {bad}"
        )


# ---------------------------------------------------------------------------
# slack-account.example.json
# ---------------------------------------------------------------------------


def test_slack_example_is_valid_json():
    data = json.loads(SLACK_EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, list), "slack-account.example.json must be a JSON array"
    assert len(data) >= 1, "slack-account.example.json must have at least one example entry"


def test_slack_example_has_required_fields():
    data = json.loads(SLACK_EXAMPLE.read_text(encoding="utf-8"))
    entry = data[0]
    assert "name" in entry, "Slack example entry must have a 'name' field"
    assert "bot_token" in entry, "Slack example entry must have a 'bot_token' field"
    assert "app_token" in entry, "Slack example entry must have an 'app_token' field"


def test_slack_example_uses_placeholder_tokens():
    """Example file must not contain real tokens — only recognizable placeholders."""
    data = json.loads(SLACK_EXAMPLE.read_text(encoding="utf-8"))
    for entry in data:
        bot = entry.get("bot_token", "")
        app = entry.get("app_token", "")
        # A real bot token starts xoxb- followed by many digits/alphanumerics.
        # The placeholder must clearly not be a real token.
        assert not re.match(r"xoxb-[0-9]{10,}", bot), (
            "slack-account.example.json bot_token must be a placeholder, not a real token"
        )
        assert not re.match(r"xapp-[0-9]{10,}", app), (
            "slack-account.example.json app_token must be a placeholder, not a real token"
        )
        # The example should start with the right prefix to guide the reader.
        assert bot.startswith("xoxb-") or "REPLACE" in bot or "TOKEN" in bot or "R..." in bot, (
            "bot_token placeholder should start with xoxb- or contain REPLACE/TOKEN"
        )


# ---------------------------------------------------------------------------
# AGENTS.md
# ---------------------------------------------------------------------------


def test_agents_md_exists():
    assert AGENTS_MD.exists(), "deploy/nemoclaw/AGENTS.md must exist"


def test_agents_md_documents_runtime_context_mechanism():
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN" in text, (
        "AGENTS.md must document the MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN env var"
    )
    assert "mac-runtime-context.md" in text, (
        "AGENTS.md must reference the mac-runtime-context.md file"
    )


def test_agents_md_explains_coexistence():
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "hermes gateway" in text.lower(), (
        "AGENTS.md must mention coexistence with the existing hermes gateway"
    )
    assert "18765" in text, "AGENTS.md must mention the NemoClaw pilot port (18765)"


# ---------------------------------------------------------------------------
# RUNBOOK.md
# ---------------------------------------------------------------------------


def test_runbook_md_exists():
    assert RUNBOOK_MD.exists(), "deploy/nemoclaw/RUNBOOK.md must exist"


def test_runbook_md_covers_slack_setup():
    text = RUNBOOK_MD.read_text(encoding="utf-8")
    assert "bot_token" in text, "RUNBOOK.md must document bot_token"
    assert "app_token" in text, "RUNBOOK.md must document app_token"
    assert "Socket Mode" in text, "RUNBOOK.md must mention Socket Mode"


def test_runbook_md_covers_coexistence():
    text = RUNBOOK_MD.read_text(encoding="utf-8")
    assert "18765" in text, "RUNBOOK.md must mention NemoClaw port 18765"
    assert "8765" in text, "RUNBOOK.md must mention existing hermes gateway port (8765)"


def test_runbook_md_covers_provider_config():
    text = RUNBOOK_MD.read_text(encoding="utf-8")
    assert "config.yaml" in text, "RUNBOOK.md must reference config.yaml"


def test_runbook_md_covers_runtime_context():
    text = RUNBOOK_MD.read_text(encoding="utf-8")
    assert "mac-runtime-context.md" in text, (
        "RUNBOOK.md must document the mac-runtime-context.md setup step"
    )


def test_runbook_md_uses_placeholders_not_real_identities():
    """RUNBOOK.md must use generic placeholders, not real fleet identities."""
    text = RUNBOOK_MD.read_text(encoding="utf-8")
    _FORBIDDEN = re.compile(
        r"(?<![A-Za-z0-9])(?:rocky|natasha|bullwinkle|madmax|puck|sparky|"
        r"worker2|jkh|hosta|hostb|hostc|hostd|hoste|hostf|devuser|agentuser)(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _FORBIDDEN.search(line)
        assert not m, (
            f"deploy/nemoclaw/RUNBOOK.md:{lineno}: "
            f"operator identity '{m.group(0)}' must not appear in checked-in docs"
        )
