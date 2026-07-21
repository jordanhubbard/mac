from pathlib import Path
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

import pytest

from mac.deploy_env import (
    ControlConfig,
    DEFAULT_WORKER_CAPABILITIES,
    LEGACY_WORKER_CAPABILITIES,
    DeployEnvConfig,
    DeployIdentity,
    DeployPaths,
    GatewayConfig,
    SharedServicesConfig,
    WorkerConfig,
    build_mac_env,
    config_from_legacy_args,
    parse_env_text,
    render_env,
    normalize_worker_capabilities,
)
from mac.fleet_deploy import cleanup_path_strings, parse_ssh_target
from mac.providers import ROUTER_PROVIDERS, provider_key_env, spoke_scrub_env_vars, upstream_provider_env_vars
import yaml


ROOT = Path(__file__).resolve().parents[1]


def parse_env(path: Path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def load_sample_fleet_config():
    return yaml.safe_load((ROOT / "deploy" / "fleet" / "config.yaml").read_text(encoding="utf-8"))


def deploy_script_text():
    deploy = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    node = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    return deploy + "\n" + node


def fleet_config_query_source() -> str:
    script = deploy_script_text()
    match = re.search(
        r"fleet_config_query\(\) \{.*?<<'PY'\n(?P<source>.*?)\nPY\n\}",
        script,
        re.DOTALL,
    )
    assert match is not None
    return match.group("source")


def gateway_log_classifier_source() -> str:
    script = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    match = re.search(
        r"classify_gateway_logs\(\) \{.*?<<'PY'\n(?P<source>.*?)\nPY\n\}",
        script,
        re.DOTALL,
    )
    assert match is not None
    return match.group("source")


def run_gateway_log_classifier(tmp_path: Path, text: str):
    input_path = tmp_path / "gateway.log"
    output_path = tmp_path / "summary.json"
    input_path.write_text(text, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            gateway_log_classifier_source(),
            str(input_path),
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result, json.loads(output_path.read_text(encoding="utf-8"))


def deploy_env_config(
    tmp_path,
    *,
    agent="rocky",
    hub_agent="rocky",
    hub_url="http://127.0.0.1:8789",
    hub_token="HUBTOK",
    worker_mode="loop",
    network_provider="tailscale",
    fleet_name="mac",
):
    return DeployEnvConfig(
        paths=DeployPaths(
            env_file=tmp_path / "mac.env",
            mac_home=tmp_path / ".mac",
            home=tmp_path,
        ),
        control=ControlConfig(
            port="8789",
            hub_url=hub_url,
            hub_token=hub_token,
            bind_host="127.0.0.1",
            supervisor_kind="systemd",
            network_provider=network_provider,
        ),
        gateway=GatewayConfig(
            home_channel="home",
            model="",
            provider="custom",
            base_url="",
        ),
        worker=WorkerConfig(
            mode=worker_mode,
            capabilities=DEFAULT_WORKER_CAPABILITIES,
            allowed_projects="",
            required_metadata="",
            require_canary="1",
        ),
        services=SharedServicesConfig(
            qdrant_url="",
            qdrant_port="6333",
            firecrawl_url="",
            firecrawl_port="3002",
        ),
        identity=DeployIdentity(
            agent=agent,
            shared_services_manager=hub_agent,
            fleet_name=fleet_name,
        ),
    )


def test_deploy_env_render_round_trips_shell_quoted_values():
    values = {
        "PLAIN": "abc_123",
        "WITH_SPACE": "one two",
        "WITH_SINGLE_QUOTE": "one'two",
        "WITH_HASH": "abc#def",
        "EMPTY": "",
    }

    assert parse_env_text(render_env(values)) == values
    assert parse_env_text("export FOO='bar baz'\nQUOTED='one'\"'\"'two'\n") == {
        "FOO": "bar baz",
        "QUOTED": "one'two",
    }


def test_parse_env_text_skips_malformed_quoted_lines():
    # A line with unbalanced shell quoting is corrupt: it must be skipped, not
    # stored as a half-parsed value, and must not poison the good lines around it.
    text = (
        "GOOD=ok\n"
        'BROKEN="unterminated\n'   # unbalanced double quote
        "ALSO_BROKEN=it's mine\n"  # unbalanced single quote
        "NEXT=fine\n"
    )
    parsed = parse_env_text(text)
    assert parsed == {"GOOD": "ok", "NEXT": "fine"}
    assert "BROKEN" not in parsed
    assert "ALSO_BROKEN" not in parsed


def test_parse_env_text_trailing_unquoted_tokens_take_leading_assignment():
    # Documented fallback semantics: with trailing unquoted tokens the leading
    # KEY=val wins and the rest is ignored (render_env never emits this — unsafe
    # values are quoted — so it only arises from a hand-edited file).
    assert parse_env_text("KEY=val extra garbage\n") == {"KEY": "val"}
    assert parse_env_text("export RAW=plainvalue\n") == {"RAW": "plainvalue"}


def test_build_mac_env_advertises_enabled_webdav_publish_service(tmp_path):
    base = deploy_env_config(tmp_path)
    cfg = DeployEnvConfig(
        paths=base.paths,
        control=base.control,
        gateway=base.gateway,
        worker=base.worker,
        services=SharedServicesConfig(
            qdrant_url="",
            qdrant_port="6333",
            firecrawl_url="",
            firecrawl_port="3002",
            webdav_enabled="1",
            webdav_url="http://principal.example:8790/artifacts/",
            webdav_root="/srv/mac-artifacts",
            webdav_public_path="artifacts",
        ),
        identity=base.identity,
    )

    env = build_mac_env(
        {
            "MAC_PUBLISH_WEBDAV_URL": "http://stale.example/artifacts/",
            "MAC_WEBDAV_WRITE_TOKEN": "old-token",
        },
        cfg,
        environ={
            "MAC_DEPLOY_WEBDAV_MAX_UPLOAD_BYTES": "1024",
        },
    )

    assert env["MAC_PUBLISH_WEBDAV_ENABLED"] == "1"
    assert env["MAC_PUBLISH_DIR"] == "/srv/mac-artifacts"
    assert env["MAC_PUBLISH_METHOD"] == "hub_directory_http"
    assert env["MAC_PUBLISH_PUBLIC_URL"] == "http://principal.example:8790/artifacts"
    assert env["MAC_PUBLISH_WEBDAV_URL"] == "http://principal.example:8790/artifacts"
    assert env["MAC_WEBDAV_PUBLIC_URL"] == "http://principal.example:8790/artifacts"
    assert env["MAC_WEBDAV_PUBLIC_PATH"] == "/artifacts/"
    assert env["MAC_WEBDAV_ROOT"] == "/srv/mac-artifacts"
    assert "MAC_WEBDAV_WRITE_TOKEN" not in env
    assert env["MAC_WEBDAV_MAX_UPLOAD_BYTES"] == "1024"


def test_build_mac_env_defaults_relay_observability_on(tmp_path):
    """New nodes come up relay-active by default (nemo-relay ships via the
    deploy's relay extra + worker reconcile)."""
    cfg = deploy_env_config(tmp_path)
    env = build_mac_env({}, cfg, environ={})
    assert env["MAC_RELAY_OBSERVABILITY"] == "1"


def test_build_mac_env_preserves_explicit_relay_opt_out(tmp_path):
    """An operator's explicit MAC_RELAY_OBSERVABILITY=0 survives redeploys
    (setdefault, not overwrite)."""
    cfg = deploy_env_config(tmp_path)
    env = build_mac_env({"MAC_RELAY_OBSERVABILITY": "0"}, cfg, environ={})
    assert env["MAC_RELAY_OBSERVABILITY"] == "0"


def test_deploy_env_import_is_dependency_light():
    # Regression: deploy-mac-fleet.sh runs `python -m mac.deploy_env write-mac-env`
    # on the bootstrap python BEFORE the deploy venv exists, so importing
    # mac.deploy_env must NOT transitively pull in mac.services (which needs
    # yaml, cryptography, …). A lazy mac/__init__ keeps the package import light.
    # (A bullwinkle redeploy died with ModuleNotFoundError: 'yaml' before this.)
    code = (
        "import sys, mac.deploy_env\n"
        "assert 'mac.services' not in sys.modules, "
        "'mac.deploy_env import pulled in mac.services (heavy deps)'\n"
        "from mac import ControlPlane\n"  # lazy re-export still resolves
        "assert ControlPlane.__name__ == 'ControlPlane'\n"
        "print('OK')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_supervisord_resource_health_uses_long_running_loop():
    script = deploy_script_text()
    watchdog = (ROOT / "deploy" / "agent-resource-health.sh").read_text(
        encoding="utf-8"
    )

    assert "command=$MAC_HOME/bin/agent-resource-health --loop" in script
    assert 'MAC_RESOURCE_HEALTH_INTERVAL_SECONDS="300"' in script
    assert "--loop)" in watchdog
    assert 'sleep "$interval"' in watchdog


def test_codegraph_init_is_asynchronous_bounded_and_observable():
    script = deploy_script_text()

    assert "MAC_DEPLOY_CODEGRAPH_INIT_TIMEOUT_SECONDS:-300" in script
    assert 'nohup "$PY"' in script
    assert 'status_file="$LOG_DIR/codegraph-init-source.json"' in script
    assert '"schema": "mac.codegraph_background_init.v1"' in script
    assert 'write_status("completed"' in script
    assert "start_new_session=True" in script
    assert "os.killpg(process.pid, signal.SIGTERM)" in script
    assert "os.killpg(process.pid, signal.SIGKILL)" in script


def test_fleet_deploy_syncs_hermes_chat_config_from_mac_env():
    # The Hermes runtime reads ~/.hermes/.env + config.yaml (not mac.env) for its
    # chat provider; the deploy must sync those from mac.env or agents dial the
    # retired TokenHub :8090 / send a stale bearer (403) and self-test degrades.
    script = deploy_script_text()
    assert "sync_hermes_chat_config()" in script
    assert "-m mac.hermes_chat_config" in script
    assert '--mac-env "$ENV_FILE"' in script
    # Legacy one-shot deploys still repair durable Hermes state in-order. Typed
    # phase 2 consumes the prerequisite receipt and does not rewrite it.
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    legacy = installer.split(
        'if [ "$NODE_ACTION" = legacy-one-shot ]; then\n  initialize_hermes_home', 1
    )[1].split("\nelse\n  log \"typed phase 2 retained", 1)[0]
    assert legacy.index("apply_hermes_gateway_runtime_shim") < legacy.index(
        "sync_hermes_chat_config"
    ) < legacy.index("install_fleet_skills")


def test_fleet_deploy_exports_python_bin_to_remote():
    # PYTHON_BIN is used in the remote-executed deploy (e.g. install_github_review_key),
    # so it must be in the `export` list shipped to the remote env — like PY — or the
    # remote aborts under `set -u` with "PYTHON_BIN: unbound variable".
    script = deploy_script_text()
    export_line = next(
        (ln for ln in script.splitlines() if ln.startswith("export AGENT FLEET_NAME")),
        "",
    )
    assert "PYTHON_BIN" in export_line.split(), "PYTHON_BIN must be exported to the remote deploy env"
    # Export alone is insufficient: the remote payload (fleet-node-install.sh) must
    # also ASSIGN it — `resolve_python_bin` only runs in the local driver.
    payload = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    assert re.search(r"^\s*PYTHON_BIN=", payload, re.MULTILINE), \
        'remote payload must ASSIGN PYTHON_BIN (e.g. PYTHON_BIN="$PY"), not just export it'



def test_sample_fleet_config_is_generic_and_externalized():
    cfg = load_sample_fleet_config()
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    rendered = "\n".join(
        [
            (ROOT / "deploy" / "fleet" / "config.yaml").read_text(encoding="utf-8"),
            (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8"),
            (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8"),
            (ROOT / "deploy" / "systemd" / "mac.env.example").read_text(encoding="utf-8"),
            (ROOT / "scripts" / "setup-fleet.py").read_text(encoding="utf-8"),
        ]
    )

    assert cfg["sample"] is True
    assert cfg["hub_agent"] == "hub"
    assert cfg["shared_services_manager_agent"] == "hub"
    assert not (ROOT / "deploy" / "fleet" / "config-site.yaml").exists()
    assert "config-site" not in gitignore
    assert "config-site" not in rendered
    assert "~/.mac/fleets.yaml" in rendered
    assert "--hub <hub-node>" in rendered
    assert "deploy/agents/" not in rendered
    assert "rocky" not in rendered.lower()
    assert "natasha" not in rendered.lower()
    assert "bullwinkle" not in rendered.lower()
    assert "100.125.137.89" not in rendered


def test_sample_fleet_config_supports_home_channel_and_model_diversity():
    cfg = load_sample_fleet_config()
    assert cfg["defaults"]["hermes"]["slack_home_channel_name"] == ""
    assert cfg["defaults"]["hermes"]["gateway_provider"] == "custom"
    assert cfg["defaults"]["network"]["provider"] == "none"
    assert cfg["defaults"]["network"]["headscale"]["manage"] is False
    assert cfg["defaults"]["network"]["headscale"]["preauth_key_env"] == "MAC_DEPLOY_HEADSCALE_PREAUTHKEY"

    models = [
        agent.get("hermes", {}).get("gateway_model")
        for agent in cfg["agents"]
        if agent.get("hermes", {}).get("gateway_model")
    ]
    assert len(models) >= 3
    assert len(set(models)) == len(models)


def test_fleet_agent_configs_enable_review_capability_by_default():
    script = deploy_script_text()
    cfg = load_sample_fleet_config()
    expected = (
        "ops,python,openclaw,review,api,architecture,cli,docs,security,testing,"
        "typescript,ui,web_search,web_extract,web_crawl,firecrawl,work_package_v1"
    )

    assert "worker_capabilities_field(worker.get(\"capabilities\"))" in script
    assert f'DEFAULT_WORKER_CAPABILITIES = "{expected}"' in script
    assert f'WORKER_CAPABILITIES="${{MAC_DEPLOY_WORKER_CAPABILITIES:-{expected}}}"' in script
    assert DEFAULT_WORKER_CAPABILITIES == expected
    assert f'capabilities="${{MAC_WORKER_CAPABILITIES:-{expected}}}"' in script
    assert cfg["defaults"]["worker"]["capabilities"] == [
        "ops",
        "python",
        "openclaw",
        "review",
        "api",
        "architecture",
        "cli",
        "docs",
        "security",
        "testing",
        "typescript",
        "ui",
        "web_search",
        "web_extract",
        "web_crawl",
        "firecrawl",
        "work_package_v1",
    ]

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "setup_fleet_capabilities", ROOT / "scripts" / "setup-fleet.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._default_worker_capabilities() == expected.split(",")

    assert normalize_worker_capabilities(LEGACY_WORKER_CAPABILITIES) == expected
    assert normalize_worker_capabilities("python,custom") == "python,custom"


def test_fleet_deploy_persists_or_recovers_worker_attestation_key():
    script = deploy_script_text()

    assert '--attestation-key-env "$HOME/.mac/mac.env"' in script
    assert "--rotate-missing-attestation-key" not in script
    assert "--rotate-invalid-attestation-key" not in script
    assert "reconcile_bound_worker_attestation_key" in script
    assert "mac.deployment_attestation probe" in script
    assert "/attestation-key/recover" in script
    assert "mac.deployment_attestation install" in script
    assert "post-install attestation key proof did not verify" in script
    # loop-01: the reviewer prompt that asks for a signed review_verdict moved
    # into the extracted mac.task_executor module.
    executor_prompt = (ROOT / "src" / "mac" / "executor_prompt.py").read_text(
        encoding="utf-8"
    )
    assert "evidence_type=review_verdict" in executor_prompt


def test_fleet_deploy_bootstraps_hub_fleet_record(tmp_path):
    script = deploy_script_text()

    values = build_mac_env(
        {},
        deploy_env_config(tmp_path, fleet_name="test-fleet"),
        environ={},
    )
    assert values["MAC_FLEET_NAME"] == "test-fleet"
    assert values["MAC_FLEET_TENANT_ID"] == "tenant_test-fleet"
    assert "if agent == shared_services_manager:" in script
    assert "cp.create_fleet(" in script
    assert "DEFAULT_HUB_REVIEWER_AGENT_NAME" in script
    assert "cp.register_machine(" in script
    assert "cp.register_agent(" in script
    assert "HUB_REVIEW_VERIFIER_RESOURCE_SCHEMA" in script
    assert "registered_configured_agent_ids.append(reviewer.id)" in script
    # Idempotent get-or-create: the id is derived once via stable_id (which
    # lowercases the name) and the fleet is looked up by both name and that id,
    # so a re-deploy under different name case reconciles instead of colliding.
    assert 'fleet_fid = stable_id("fleet", fleet)' in script
    assert "fleet_id=fleet_fid" in script
    assert "for _key in (fleet, fleet_fid):" in script
    assert 'description="Auto-registered deployment fleet"' in script


def test_fleet_deploy_distributes_registry_and_reconciles_configured_membership():
    script = deploy_script_text()

    assert "fleet_config_query sanitized-registry" in script
    assert "MAC_DEPLOY_FLEET_REGISTRY_FILE" in script
    assert 'cp -f "$FLEET_REGISTRY_FILE" "$MAC_HOME/fleets.yaml"' in script
    assert "fleet_config_query configured-agent-ids" in script
    assert "MAC_DEPLOY_CONFIGURED_AGENT_IDS" in script
    assert "registered_configured_agent_ids" in script


def test_fleet_deploy_drain_agent_lookup_uses_file_for_large_json_payload():
    script = deploy_script_text()
    agent_id_for_drain = script.split("agent_id_for_drain() {", 1)[1].split(
        "wait_for_agent_active_leases() {", 1
    )[0]

    assert 'mac_api_json GET "/agents" > "$response_file"' in agent_id_for_drain
    assert '"$PY" - "$AGENT" "$response_file"' in agent_id_for_drain
    assert "agents = json.load(handle)" in agent_id_for_drain
    assert 'mac_api_json GET "/agents" |' not in agent_id_for_drain




def test_fleet_deploy_verifies_onboarded_github_cli_before_phase2_mutation():
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    function = _deploy_function(installer, "install_github_cli", "install_codegraph_cli")
    pre_mutation = installer.split(
        'if [ "$NODE_ACTION" = arm-phase2 ] || [ "$NODE_ACTION" = apply-phase2 ]; then',
        1,
    )[1].split("capture_darwin_launchd_prestate", 1)[0]

    assert "install_github_cli()" in installer
    assert "GitHub CLI is missing; complete node onboarding before phase 2" in function
    assert "onboarded_command_path gh" in function
    assert '"$existing" --version' in function
    assert "brew install" not in function
    assert "apt-get" not in function
    assert pre_mutation.index("validate_typed_prerequisite_bundle") < pre_mutation.index(
        "install_github_cli"
    )


def test_node_installer_resolves_onboarded_tools_from_one_trusted_path():
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    assert 'ONBOARDED_COMMAND_PATH="$MAC_HOME/bin:/opt/homebrew/bin:' in installer
    resolver = _deploy_function(
        installer, "onboarded_command_path", "install_fleet_registry"
    )
    assert 'PATH="$ONBOARDED_COMMAND_PATH" command -v "$name"' in resolver
    github = _deploy_function(
        installer, "configure_github_https_credentials", "wait_for_required_services"
    )
    assert "onboarded_command_path gh" in github


def test_fleet_deploy_never_forces_an_unverified_github_review_key():
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    function = installer.split("install_github_review_key() {", 1)[1].split(
        "\n}\n\nconfigure_github_https_credentials", 1
    )[0]

    assert "github_ssh_auth_succeeds()" in installer
    assert '[ -f "$key_file" ] && [ ! -L "$key_file" ]' in function
    assert 'github_ssh_auth_succeeds "$key_file"' in function
    assert "verified onboarded ambient GitHub SSH identity" in function
    assert "the hub cannot authenticate to github.com for review publication" in function
    assert "Install and authorize the GitHub review identity during onboarding" in function
    assert "ssh-keygen" not in function
    assert "IdentityFile" not in function
    assert "remove_managed_github_review_key_config" not in function
    assert 'ssh_args+=(-o IdentitiesOnly=yes -i "$key_file")' in installer


def test_fleet_deploy_installs_and_initializes_codegraph_for_workers():
    script = deploy_script_text()
    assets = (ROOT / "deploy" / "reviewed-tool-assets.sh").read_text(encoding="utf-8")
    install_window = script.split('mv "$SRC_DIR.new" "$SRC_DIR"', 1)[1].split(
        'log "creating/updating mac environment file"', 1
    )[0]

    assert "install_codegraph_cli()" in script
    assert "initialize_codegraph_repository()" in script
    assert "mac_install_reviewed_codegraph" in assets
    assert 'MAC_REVIEWED_CODEGRAPH_VERSION="v1.1.6"' in assets
    for name in (
        "codegraph-linux-x64.tar.gz",
        "codegraph-linux-arm64.tar.gz",
        "codegraph-darwin-x64.tar.gz",
        "codegraph-darwin-arm64.tar.gz",
    ):
        assert name in assets
    assert "codegraph/main/install.sh" not in script
    assert "astral.sh/uv/install.sh" not in script
    assert "| sh" not in assets
    assert 'env -i PATH="${PATH:-/usr/bin:/bin}"' in assets
    assert "GH_TOKEN=" not in assets
    assert "MAC_DEPLOY_HUB_TOKEN=" not in assets
    assert 'ln -s "$bundle/bin/codegraph"' in assets
    assert "run_without_deploy_credentials" in script
    assert "codegraph init" in script
    assert 'initialize_codegraph_repository "$SRC_DIR"' in install_window
    assert "typed phase 2 defers CodeGraph indexing to post-commit maintenance" in install_window
    assert 'initialize_codegraph_repository "$SRC_DIR" || true' not in install_window
    verifier = _deploy_function(script, "install_codegraph_cli", "ensure_codegraph_git_exclude")
    assert "reviewed CodeGraph bundle is missing" in verifier
    assert "onboarded CodeGraph version differs" in verifier
    assert 'bundle="$MAC_HOME/lib/codegraph/versions/$MAC_REVIEWED_CODEGRAPH_VERSION"' in verifier
    assert '"$(readlink "$target" 2>/dev/null || true)" = "$binary"' in verifier
    assert '[ -x "$node" ] && [ ! -L "$node" ]' in verifier
    assert "mac_install_reviewed_codegraph" not in verifier
    assert "mac.codegraph_background_init.v1" in script
    assert "CodeGraph index initialization queued" in script
    assert 'grep -qxF ".codegraph/"' in script


def test_fleet_deploy_transports_reviewed_tool_contract_outside_secret_stdin():
    driver = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )

    assert "copying reviewed native-tool checksum contract" in driver
    assert 'reviewed_tool_assets="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/reviewed-tool-assets.sh"' in driver
    assert "MAC_DEPLOY_REVIEWED_TOOL_ASSETS=" in driver
    assert r'_mac_tool_assets=\$2' in driver
    assert r'\$_mac_tool_assets' in driver
    assert 'REVIEWED_TOOL_ASSETS="${MAC_DEPLOY_REVIEWED_TOOL_ASSETS:-' in installer
    assert '. "$REVIEWED_TOOL_ASSETS"' in installer
    assert 'MAC_REVIEWED_UV_VERSION="0.8.22"' in (
        ROOT / "deploy" / "reviewed-tool-assets.sh"
    ).read_text(encoding="utf-8")
    assert 'MAC_REVIEWED_PYTHON_VERSION="3.12.11"' in (
        ROOT / "deploy" / "reviewed-tool-assets.sh"
    ).read_text(encoding="utf-8")


def _deploy_function(script: str, name: str, next_name: str) -> str:
    body = script.split(f"{name}() {{", 1)[1].split(f"\n{next_name}() {{", 1)[0]
    return f"{name}() {{{body}\n"


def test_codegraph_phase2_verifier_accepts_onboarded_pinned_binary(tmp_path):
    script = deploy_script_text()
    runner = tmp_path / "run-install.sh"
    runner.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'MAC_HOME="$PWD/mac-home"',
                'HOME="$PWD/home"',
                'LOG_DIR="$PWD/logs"',
                'bundle="$MAC_HOME/lib/codegraph/versions/v1.1.6"',
                "mkdir -p \"$MAC_HOME/bin\" \"$bundle/bin\" \"$HOME\" \"$LOG_DIR\"",
                'log() { printf "%s\\n" "$*" >> "$LOG_DIR/log.txt"; }',
                'die() { printf "%s\\n" "$*" >&2; return 1; }',
                'run_without_deploy_credentials() { "$@"; }',
                'MAC_REVIEWED_CODEGRAPH_VERSION="v1.1.6"',
                "cat > \"$bundle/bin/codegraph\" <<'EOF'",
                "#!/bin/sh",
                '[ "$1" = --version ] && echo 1.1.6',
                "EOF",
                "cat > \"$bundle/node\" <<'EOF'",
                "#!/bin/sh",
                "exit 0",
                "EOF",
                'chmod +x "$bundle/bin/codegraph" "$bundle/node"',
                'ln -s "$bundle/bin/codegraph" "$MAC_HOME/bin/codegraph"',
                _deploy_function(script, "install_codegraph_cli", "ensure_codegraph_git_exclude"),
                "install_codegraph_cli",
                'test -L "$MAC_HOME/bin/codegraph"',
                'grep -q "verified onboarded CodeGraph" "$LOG_DIR/log.txt"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    runner.chmod(0o755)

    result = subprocess.run(
        [str(runner)],
        cwd=tmp_path,
        env={**os.environ, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "verified onboarded CodeGraph v1.1.6" in (
        tmp_path / "logs" / "log.txt"
    ).read_text(encoding="utf-8")


def test_codegraph_phase2_verifier_fails_when_onboarding_is_missing(tmp_path):
    script = deploy_script_text()
    runner = tmp_path / "run-install-fail.sh"
    runner.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'MAC_HOME="$PWD/mac-home"',
                'HOME="$PWD/home"',
                'LOG_DIR="$PWD/logs"',
                "mkdir -p \"$MAC_HOME\" \"$HOME\" \"$LOG_DIR\"",
                'log() { printf "%s\\n" "$*" >> "$LOG_DIR/log.txt"; }',
                'die() { printf "%s\\n" "$*" >&2; return 1; }',
                'run_without_deploy_credentials() { "$@"; }',
                'MAC_REVIEWED_CODEGRAPH_VERSION="v1.1.6"',
                _deploy_function(script, "install_codegraph_cli", "ensure_codegraph_git_exclude"),
                "install_codegraph_cli",
                "",
            ]
        ),
        encoding="utf-8",
    )
    runner.chmod(0o755)

    result = subprocess.run(
        [str(runner)],
        cwd=tmp_path,
        env={**os.environ, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "reviewed CodeGraph bundle is missing" in result.stderr


def test_reviewed_tool_asset_checksum_mismatch_fails_closed(tmp_path):
    assets = ROOT / "deploy" / "reviewed-tool-assets.sh"
    payload = tmp_path / "asset.tgz"
    payload.write_bytes(b"not the reviewed release")

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            '. "$1"; mac_verify_reviewed_asset "$2" "$3"',
            "bash",
            str(assets),
            str(payload),
            "0" * 64,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "SHA-256 mismatch for reviewed asset" in result.stderr


@pytest.mark.parametrize(
    ("tool", "os_name", "architecture", "filename"),
    [
        ("uv", "Linux", "x86_64", "uv-x86_64-unknown-linux-gnu.tar.gz"),
        ("uv", "Linux", "aarch64", "uv-aarch64-unknown-linux-gnu.tar.gz"),
        ("uv", "Darwin", "x86_64", "uv-x86_64-apple-darwin.tar.gz"),
        ("uv", "Darwin", "arm64", "uv-aarch64-apple-darwin.tar.gz"),
        ("codegraph", "Linux", "x86_64", "codegraph-linux-x64.tar.gz"),
        ("codegraph", "Linux", "aarch64", "codegraph-linux-arm64.tar.gz"),
        ("codegraph", "Darwin", "x86_64", "codegraph-darwin-x64.tar.gz"),
        ("codegraph", "Darwin", "arm64", "codegraph-darwin-arm64.tar.gz"),
    ],
)
def test_reviewed_tool_asset_matrix_covers_fleet_platforms(
    tool, os_name, architecture, filename
):
    assets = ROOT / "deploy" / "reviewed-tool-assets.sh"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            '. "$1"; mac_reviewed_asset_spec "$2" "$3" "$4"',
            "bash",
            str(assets),
            tool,
            os_name,
            architecture,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    observed_name, digest, url, root = result.stdout.strip().split()
    assert observed_name == filename
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert url.startswith("https://github.com/") and url.endswith(filename)
    assert root == filename.removesuffix(".tar.gz")


@pytest.mark.parametrize(
    ("tool", "os_name", "architecture"),
    [("uv", "Plan9", "x86_64"), ("codegraph", "Linux", "riscv64")],
)
def test_reviewed_tool_asset_unsupported_platform_fails_closed(
    tool, os_name, architecture
):
    assets = ROOT / "deploy" / "reviewed-tool-assets.sh"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            '. "$1"; mac_reviewed_asset_spec "$2" "$3" "$4"',
            "bash",
            str(assets),
            tool,
            os_name,
            architecture,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unsupported reviewed-tool" in result.stderr


def test_codegraph_init_function_skips_archive_source_without_git_worktree(tmp_path):
    script = deploy_script_text()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codegraph = bin_dir / "codegraph"
    codegraph.write_text("#!/bin/sh\necho codegraph init should not run >&2\nexit 99\n", encoding="utf-8")
    codegraph.chmod(0o755)
    source_dir = tmp_path / "archive-source"
    source_dir.mkdir()
    runner = tmp_path / "run-init-non-git.sh"
    runner.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'MAC_HOME="$PWD/mac-home"',
                'HOME="$PWD/home"',
                'LOG_DIR="$PWD/logs"',
                "mkdir -p \"$MAC_HOME\" \"$HOME\" \"$LOG_DIR\"",
                'log() { printf "%s\\n" "$*" >> "$LOG_DIR/log.txt"; }',
                _deploy_function(script, "ensure_codegraph_git_exclude", "initialize_codegraph_repository"),
                _deploy_function(script, "initialize_codegraph_repository", "normalize_hermes_redaction_env"),
                f"initialize_codegraph_repository {source_dir}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    runner.chmod(0o755)

    result = subprocess.run(
        [str(runner)],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "not a git worktree" in (tmp_path / "logs" / "log.txt").read_text(
        encoding="utf-8"
    )


def test_fleet_deploy_does_not_print_worker_token_in_systemd_status():
    script = deploy_script_text()
    agent_service = script.split("install_linux_agent_service() {", 1)[1].split(
        "install_darwin_service() {", 1
    )[0]

    assert 'systemctl show "$MAC_AGENT_SERVICE_NAME"' in agent_service
    assert 'systemctl --no-pager -l status "$MAC_AGENT_SERVICE_NAME"' not in agent_service
    assert "-p ActiveState" in agent_service
    assert "-p MainPID" in agent_service


def test_darwin_service_wrappers_raise_file_descriptor_limit():
    script = deploy_script_text()
    gateway_wrapper = script.split("install_hermes_gateway_wrapper() {", 1)[1].split(
        "install_mac_agent_wrapper() {", 1
    )[0]
    agent_wrapper = script.split("install_mac_agent_wrapper() {", 1)[1].split(
        "install_mac_hermes_task_executor() {", 1
    )[0]

    expected = 'ulimit -n "${MAC_SERVICE_NOFILE_LIMIT:-4096}" 2>/dev/null || true'
    assert expected in gateway_wrapper
    assert expected in agent_wrapper


def test_fleet_deploy_applies_hermes_patch_set():
    script = deploy_script_text()
    quench_patch = ROOT / "deploy" / "hermes" / "disable-shutdown-chat-notices.patch"
    runtime_patch = ROOT / "deploy" / "hermes" / "mac-runtime-context-prompt.patch"

    # ADR 0001 hu-04: the runtime is vendored in-tree (patches folded into the
    # snapshot); the deploy no longer clones upstream or applies patches at
    # deploy time. The .patch files are retained for re-vendoring (asserted below).
    assert "NousResearch/hermes-agent.git" not in script
    assert "vendored in-tree Hermes runtime" in script
    assert 'HERMES_VENDORED="$SRC_DIR/src/mac/_hermes"' in script
    assert "verify_hermes_prompt_bridge()" in script
    assert "prompt_builder.build_context_files_prompt" in script
    assert "First-Class Objects" in script
    assert "Project Bridge" in script
    assert "Agent View" in script
    assert "Dashboard Views" in script
    assert "/ui?view=work" in script
    assert "mac-hermes tasks" in script
    assert "mac-hermes projects" in script
    assert "shell_execution" in script
    assert "workspace_file_access" in script
    assert "mac-task-executor" in script
    assert "_load_mac_runtime_context" in runtime_patch.read_text(encoding="utf-8")
    assert "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN" in runtime_patch.read_text(encoding="utf-8")
    assert "Shutdown chat notifications disabled by MAC deployment policy." in quench_patch.read_text(
        encoding="utf-8"
    )


def test_fleet_deploy_declares_shared_memory_and_supervision_contract(tmp_path):
    script = deploy_script_text()
    qdrant_installer = (ROOT / "deploy" / "install-qdrant-service.sh").read_text(
        encoding="utf-8"
    )
    firecrawl_installer = (ROOT / "deploy" / "install-firecrawl-gateway.sh").read_text(
        encoding="utf-8"
    )
    env_example = parse_env(ROOT / "deploy" / "systemd" / "mac.env.example")
    cfg = load_sample_fleet_config()
    generated_env = build_mac_env(
        {},
        deploy_env_config(tmp_path, agent="spoke-a", hub_agent="hub-a", fleet_name="fleet-a"),
        environ={},
    )
    tunnel_env = build_mac_env(
        {},
        deploy_env_config(
            tmp_path,
            agent="spoke-a",
            hub_agent="hub-a",
            network_provider="none",
        ),
        environ={},
    )

    assert 'SUPERVISOR_REQUESTED="${MAC_DEPLOY_SUPERVISOR:-auto}"' in script
    assert "detect_supervisor()" in script
    assert "MAC_DEPLOY_SUPERVISOR=systemd, launchd, or supervisord" in script
    assert "install_supervisord_service()" in script
    assert "write_hermes_memory_topology()" in script
    assert "write_hermes_runtime_context()" in script
    assert "register_hermes_runtime_identity()" in script
    assert "ensure_hermes_identity_memory_continuity()" in script
    assert generated_env["SLACK_ALLOWED_USERS"] == "*"
    assert generated_env["SLACK_STRICT_MENTION"] == "true"
    assert "mac.hermes.runtime_context.v1" in (ROOT / "src" / "mac" / "hermes_runtime.py").read_text(
        encoding="utf-8"
    )
    assert generated_env["MAC_HERMES_INSTANCE_ID"] == "hermes_spoke-a"
    assert generated_env["MAC_WORKER_HERMES_INSTANCE_ID"] == generated_env["MAC_HERMES_INSTANCE_ID"]
    assert 'common+=(--hermes-instance-id "${MAC_WORKER_HERMES_INSTANCE_ID:-${MAC_HERMES_INSTANCE_ID:-}}")' in script
    assert 'export PATH="$HOME/.mac/bin:$HOME/.mac/venv/bin:$PATH"' in script
    assert "install_or_validate_shared_services" in script
    assert "mac.hermes.memory_topology.v1" in script
    assert '"long_term_memory": "memories/MEMORY.md"' in script
    assert '"legacy_long_term_memory": "MEMORY.md"' in script
    assert "QDRANT_FLEET_URL" in script
    assert 'QDRANT_REQUIRE="1"' in script
    assert generated_env["MAC_REQUIRE_QDRANT_MEMORY"] == "1"
    assert '"MAC_REQUIRE_QDRANT_MEMORY": "1",' in script
    assert '"mandatory": True,' in script
    assert 'updates["QDRANT_URL"] = None' not in script
    assert "Optional Qdrant shared memory" not in script
    assert "127.0.0.1:16333:127.0.0.1:6333" in script
    assert tunnel_env["QDRANT_URL"] == "http://127.0.0.1:16333"
    assert tunnel_env["FIRECRAWL_API_URL"] == "http://127.0.0.1:13002"
    assert '"qdrant_shared_memory": False' in script
    assert 'probe_http(qdrant_url, "/collections", qdrant_headers)' in script
    assert generated_env["MAC_REVIEW_TICK_HUB_AGENT"] == "hub-a"
    assert "mac-qdrant.service" in qdrant_installer
    # The installer derives the env path via an intermediate ENV_CONF_DIR so the
    # macOS branch can override it; assert the fleet-scoped result either way.
    assert 'ENV_CONF_DIR="/etc/${FLEET_NAME}"' in qdrant_installer
    assert 'ENV_DEST="$ENV_CONF_DIR/qdrant.env"' in qdrant_installer
    assert 's|/etc/mac/qdrant.env|${env_dest_sed}|g' in qdrant_installer
    assert 'com.${FLEET_NAME}.qdrant' in qdrant_installer
    assert '[program:${FLEET_NAME}-qdrant]' in qdrant_installer
    assert cfg["defaults"]["supervisor"] == "auto"
    assert cfg["shared_services_manager_agent"] == "hub"
    assert cfg["defaults"]["qdrant"]["install"] == "auto"
    assert cfg["defaults"]["qdrant"]["required"] is True
    assert env_example["MAC_REQUIRE_QDRANT_MEMORY"] == "1"
    assert env_example["MAC_QDRANT_MEMORY_ROLE"] == "shared_level2"
    assert "MAC_QDRANT_MEMORY_ALLOW_DEGRADED" not in env_example
    assert env_example["MAC_HERMES_RUNTIME_CONTEXT_REQUIRED"] == "1"
    assert env_example["MAC_WORKER_HERMES_INSTANCE_ID"] == "hermes_example"
    assert env_example["MAC_WORKER_EXECUTOR"] == "/home/mac/.mac/bin/mac-task-executor"
    assert env_example["MAC_HERMES_WORKSPACE"] == "/home/mac/.mac/src/mac"
    assert env_example["SLACK_ALLOWED_USERS"] == "*"
    assert env_example["SLACK_STRICT_MENTION"] == "true"
    assert env_example["MAC_PROJECT_CONTRACT_FILE"] == "/home/mac/.mac/src/mac/.mac/project.yaml"
    assert '--workspace "$SRC_DIR"' in script
    assert cfg["defaults"]["firecrawl"]["install"] == "auto"
    assert cfg["defaults"]["firecrawl"]["required"] is True
    assert cfg["defaults"]["firecrawl"]["port"] == 3002
    assert "mac.firecrawl_gateway" in firecrawl_installer
    # Same ENV_CONF_DIR indirection as the qdrant installer (macOS overrides it).
    assert 'ENV_CONF_DIR="/etc/${FLEET_NAME}"' in firecrawl_installer
    assert 'ENV_DEST="$ENV_CONF_DIR/firecrawl-gateway.env"' in firecrawl_installer
    assert "Firecrawl-compatible web search gateway" in firecrawl_installer
    assert env_example["MAC_REQUIRE_FIRECRAWL"] == "1"
    assert env_example["FIRECRAWL_API_URL"] == "http://hub.example.internal:3002"
    assert "MAC_FIRECRAWL_ALLOW_DEGRADED" not in env_example
    assert "NVIDIA_API_BASE" in upstream_provider_env_vars()
    assert "LLM_KEY" in upstream_provider_env_vars()
    assert "QDRANT_API_KEY" in spoke_scrub_env_vars()
    assert 'nvidia_api_key="$(fleet_scoped_env NVIDIA_API_KEY "$agent")"' in script
    # TokenHub is retired (the in-mac router cutover replaced it): no installer,
    # no fleet-config contract, no TokenHub env in the sample. Chat now routes
    # through the agent's own local /v1 router.
    assert "tokenhub" not in cfg["defaults"]
    assert "install_or_validate_tokenhub_service" not in script
    assert not (ROOT / "deploy" / "install-tokenhub-service.sh").exists()
    assert "MAC_REQUIRE_TOKENHUB" not in env_example
    assert "TOKENHUB_URL" not in env_example
    assert env_example["MAC_ROUTER_BACKEND"] == "inproc"
    assert env_example["MAC_HERMES_GATEWAY_API_KEY"] == "mac_REPLACE_ME"
    assert env_example["OPENAI_BASE_URL"] == "http://127.0.0.1:8789/v1"


def test_fleet_deploy_configures_firecrawl_for_hermes_and_worker_capabilities(tmp_path):
    script = deploy_script_text()
    generated_env = build_mac_env({}, deploy_env_config(tmp_path), environ={})

    assert "firecrawl = merge_dicts" in script
    assert 'os.environ.get("MAC_DEPLOY_FIRECRAWL_URL") or text_field(firecrawl.get("url"))' in script
    assert (
        'os.environ.get("MAC_DEPLOY_FIRECRAWL_INSTALL") '
        'or text_field(firecrawl.get("install") or "auto")'
    ) in script
    assert 'FIRECRAWL_REQUIRE="1"' in script
    assert generated_env["MAC_REQUIRE_FIRECRAWL"] == "1"
    assert '"MAC_REQUIRE_FIRECRAWL": "1",' in script
    assert "Optional Firecrawl web search" not in script
    assert "install_or_validate_web_search_service()" in script
    assert "write_hermes_web_search_config()" in script
    assert "install_hermes_web_deps()" in script
    assert "initialize_hermes_home()" in script
    assert "from hermes_cli.config import ensure_hermes_home" in script
    assert "firecrawl-py==4.17.0" in script
    assert "FIRECRAWL_API_URL" in script
    assert 'web["search_backend"] = "firecrawl"' in script
    assert '"role": "shared_web_search"' in script
    assert '"firecrawl_web_search": False' in script
    assert 'probe_http(firecrawl_url, "/health", firecrawl_headers)' in script


def test_fleet_deploy_linux_control_plane_uses_service_wrapper():
    script = deploy_script_text()
    linux_service = script.split("install_linux_service() {", 1)[1].split(
        "install_supervisord_service() {", 1
    )[0]

    assert "install_mac_control_wrapper" in linux_service
    assert "if control_plane_enabled; then" in linux_service
    assert 'disable_systemd_service_if_present "$MAC_SERVICE_NAME"' in linux_service
    assert 'export PATH="$HOME/.mac/bin:$HOME/.mac/venv/bin:$PATH"' in script
    assert "ExecStart=$MAC_HOME/bin/mac-service" in linux_service
    assert "ExecStart=$VENV/bin/uvicorn" not in linux_service


def test_supervisord_pure_worker_has_no_gateway_program_or_restart():
    script = deploy_script_text()
    supervisor = script.split("install_supervisord_service() {", 1)[1].split(
        "install_darwin_service() {", 1
    )[0]

    assert '  none)\n    # A pure worker must not retain or start either chat-gateway program.' in supervisor
    assert '    active_gateway_program=""\n    gateway_program=""' in supervisor
    assert (
        '  if [ -n "$active_gateway_program" ]; then\n'
        '    if [ "${HERMES_GATEWAY_IMPL:-hermes}" = "openclaw" ]; then\n'
        '      prepare_openclaw_gateway\n'
        '    fi\n'
        '    start_supervisord_program "$active_gateway_program"'
    ) in supervisor
    assert (
        '    log "gateway_impl=none: pure worker; skipping gateway program '
        'install/restart"'
    ) in supervisor
    assert 'run_supervisorctl status "$AGENT_SUPERVISORD_PROG"' in supervisor


def test_fleet_context_systemd_unit_does_not_require_a_local_control_plane():
    unit = (ROOT / "deploy" / "systemd" / "mac-fleet-context.service").read_text(
        encoding="utf-8"
    )
    unit_section = unit.split("[Service]", 1)[0]

    assert "After=network-online.target" in unit_section
    assert "Wants=network-online.target" in unit_section
    assert not re.search(
        r"^(?:After|Requires)=.*\bmac\.service\b", unit_section, re.MULTILINE
    )


def test_fleet_spokes_have_no_local_control_plane_or_database(tmp_path):
    script = deploy_script_text()
    hub_env = build_mac_env(
        {},
        deploy_env_config(tmp_path, agent="hub-a", hub_agent="hub-a"),
        environ={},
    )
    spoke_env = build_mac_env(
        {
            "MAC_DB": "/legacy/spoke.db",
            "MAC_DATABASE_URL": "postgresql://legacy/spoke",
        },
        deploy_env_config(tmp_path, agent="spoke-a", hub_agent="hub-a"),
        environ={},
    )

    assert hub_env["MAC_CONTROL_PLANE_ROLE"] == "hub"
    assert hub_env["MAC_DB"].endswith("/.mac/mac.db")
    assert spoke_env["MAC_CONTROL_PLANE_ROLE"] == "client"
    assert "MAC_DB" not in spoke_env
    assert "MAC_DATABASE_URL" not in spoke_env
    assert "retire_spoke_local_control_plane_database()" in script
    assert "refusing to strand them" in script
    assert 'mac_authority migrate acc "$ACC_DB"' in script
    assert 'curl -fsS "$MAC_HUB_URL/health"' in script


def test_fleet_deploy_routes_provider_secrets_through_in_mac_router(tmp_path):
    script = deploy_script_text()
    startup = (ROOT / "src" / "mac" / "hermes_startup.py").read_text(encoding="utf-8")
    gateway_wrapper = script.split("install_hermes_gateway_wrapper() {", 1)[1].split(
        "install_mac_agent_wrapper() {", 1
    )[0]
    executor_wrapper = script.split('cat > "$executor" <<', 1)[1].split(
        'cat > "$executor_py" <<', 1
    )[0]

    # Messaging state is synchronized after mac.service starts. OpenClaw reads
    # only its identity-scoped credentials file; the Hermes files are touched
    # only on the explicit rollback implementation.
    assert "sync_messaging_config()" in script
    # A pure worker (gateway_impl=none) skips the Slack/Hermes block entirely,
    # so the secret fetch + identity/home-channel sync live in the else branch
    # of the gateway_impl guard.
    assert (
        "    fetch_slack_secrets_from_vault\n"
        "    reload_mac_env\n"
        '    if [ "${HERMES_GATEWAY_IMPL:-hermes}" != "openclaw" ]; then\n'
        "      sync_hermes_slack_identity_env\n"
        "      sync_hermes_home_channels"
    ) in script
    assert "fetch_slack_secrets_from_vault()" in script
    assert "scripts/mac-fetch-slack-secrets.py" in script
    assert "scripts/mac-fetch-openclaw-secrets.py" in script
    assert "must never refresh the retained rollback gateway" in script
    assert "-m mac.deploy_env write-mac-env" in script

    # The TokenHub install/sync/runtime machinery is gone — no installer call, no
    # client-env sync, no credential-pool sync, no TokenHub env injected.
    assert "install_or_validate_tokenhub_service" not in script
    assert "sync_hermes_tokenhub_client_env" not in script
    assert "sync_tokenhub_credential_pool" not in script
    assert 'values["TOKENHUB_URL"]' not in script
    assert "TOKENHUB_API_KEY" not in script

    # Stream B: the in-mac router runs ONLY on the hub (keys centralized there);
    # spokes route through the hub's /v1 with their hub-facing token.
    hub_env = build_mac_env(
        {},
        deploy_env_config(tmp_path, agent="rocky", hub_agent="rocky"),
        environ={**_ROUTER_ENV, "NVIDIA_API_KEY": "nvapi-SECRET"},
    )
    spoke_env = build_mac_env(
        {},
        deploy_env_config(
            tmp_path,
            agent="natasha",
            hub_agent="rocky",
            hub_url="http://hub.example:8789",
            hub_token="HUBTOK",
        ),
        environ={**_ROUTER_ENV, "NVIDIA_API_KEY": "nvapi-SECRET"},
    )
    assert hub_env["MAC_ROUTER_BACKEND"] == "inproc"
    assert hub_env["MAC_HERMES_GATEWAY_BASE_URL"] == "http://127.0.0.1:8789/v1"
    assert hub_env["MAC_HERMES_GATEWAY_API_KEY"] == hub_env["MAC_API_TOKEN"]
    assert spoke_env["MAC_HERMES_GATEWAY_BASE_URL"] == "http://hub.example:8789/v1"
    assert spoke_env["MAC_HERMES_GATEWAY_API_KEY"] == "HUBTOK"
    assert "MAC_ROUTER_PROVIDERS" not in spoke_env
    assert "MAC_ROUTER_BACKEND" not in spoke_env
    assert "nvapi-SECRET" not in "\n".join(spoke_env.values())

    # loop-01: the executor logic was extracted from a ~500-line bash heredoc
    # into the tested mac.task_executor module. The deploy now writes only a
    # shim that delegates to it.
    assert "from mac.task_executor import main" in script
    assert "raise SystemExit(main())" in script
    assert "mac-task-executor" in script
    executor_entrypoint = (ROOT / "src" / "mac" / "task_executor.py").read_text(
        encoding="utf-8"
    )
    executor_sandbox = (ROOT / "src" / "mac" / "executor_sandbox.py").read_text(
        encoding="utf-8"
    )
    executor_finalizer = (ROOT / "src" / "mac" / "executor_finalizer.py").read_text(
        encoding="utf-8"
    )
    assert "from mac import executor_sandbox as _implementation" in executor_entrypoint
    assert "def _hermes_argv(" not in executor_sandbox
    assert '"hermes_cli.main", "chat"' not in executor_sandbox
    assert '"coding-agent-required"' in executor_sandbox
    assert "def write_fallback_evidence_manifest(" in executor_finalizer
    # autonomy-loop fix (preserved through the extraction): the fallback must
    # never fabricate verified completion — UNVERIFIED operator_result only,
    # never a fake repo_change/test and never a synthetic passing check.
    assert '"evidence_type": "operator_result",' in executor_finalizer
    assert '"name": "hermes_chat_query"' not in executor_finalizer
    # telemetry path + memory feed (deployment gets smarter over time)
    executor_memory = (ROOT / "src" / "mac" / "executor_memory.py").read_text(
        encoding="utf-8"
    )
    assert 'name": "executor.%s"' in executor_memory or '"executor.%s"' in executor_memory
    assert "def recall_deployment_lessons(" in executor_memory
    assert "def record_deployment_learning(" in executor_memory
    # ADR 0001 hu-03: the gateway provider/model override is owned, in-process
    # code (mac.agent_provider) — not runtime string-surgery of an upstream
    # checkout. Verify the owned mechanism survives the TokenHub retirement.
    agent_provider = (ROOT / "src" / "mac" / "agent_provider.py").read_text(encoding="utf-8")
    assert "mac-gateway-explicit" in agent_provider
    assert '[ -f "$HOME/.acc/.env" ]' not in gateway_wrapper
    assert '[ -f "$HOME/.acc/.env" ]' not in executor_wrapper
    assert 'or os.environ.get("NVIDIA_API_KEY")' not in startup
    assert 'or os.environ.get("NVIDIA_API_BASE")' not in startup


def test_first_deploy_validators_honor_allow_degraded_services_flag():
    # A brand-new spoke reaches the hub's Qdrant/Firecrawl through a reverse
    # tunnel that is not established until the first deploy authorizes the tunnel
    # key. main() sets MAC_DEPLOY_ALLOW_DEGRADED_SERVICES=1 for that first deploy;
    # the remote validators MUST honor it (warn + proceed) instead of hard-exiting,
    # or the deploy dies before main()'s post-deploy tunnel-reconnect path runs.
    script = deploy_script_text()
    qdrant_validator = script.split("validate_qdrant_endpoint() {", 1)[1].split("\n}", 1)[0]
    firecrawl_validator = script.split("validate_firecrawl_endpoint() {", 1)[1].split("\n}", 1)[0]
    for validator in (qdrant_validator, firecrawl_validator):
        assert 'degraded="${MAC_DEPLOY_ALLOW_DEGRADED_SERVICES:-0}"' in validator
        # Both the unreachable-endpoint and missing-endpoint branches must offer a
        # degraded early-return guarded by the flag.
        assert validator.count('if [ "$degraded" = "1" ]; then') == 2
        assert "proceeding degraded (first deploy" in validator
    # Legacy compatibility still transports the flag. Typed cohorts do not use
    # it: exact prerequisite receipts must pass before the hub epoch opens.
    assert 'add_remote_env MAC_DEPLOY_ALLOW_DEGRADED_SERVICES "${allow_degraded_services:-0}"' in script
    typed = script.split("run_typed_cohort() {", 1)[1].split("\n}\n\nmain()", 1)[0]
    arm_worker = script.split("typed_phase2_arm_worker() {", 1)[1].split(
        "\n}\n\ntyped_phase2_apply_worker", 1
    )[0]
    apply_worker = script.split("typed_phase2_apply_worker() {", 1)[1].split(
        "\n}\n\ntyped_finalize_worker", 1
    )[0]
    assert 'deploy_host "$spec" "$hub_token" "$hub_tunnel_pubkey" 0' in arm_worker
    assert 'deploy_host "$spec" "$hub_token" "$hub_tunnel_pubkey" 0' in apply_worker
    assert 'run_bounded_node_phase "$selected_specs_file" phase2-arm' in typed
    assert 'typed_phase2_apply_worker "$spec"' in typed
    assert 'run_bounded_node_phase "$selected_specs_file" phase2-apply' not in typed
    assert 'run_bounded_node_phase "$selected_specs_file" prerequisites' in typed
    prerequisite_worker = script.split("typed_prerequisite_worker() {", 1)[1].split(
        "\n}\n\ntyped_staging_worker", 1
    )[0]
    assert "prepare_remote_prerequisite_bundle" in prerequisite_worker
    assert "MAC_DEPLOY_ALLOW_DEGRADED_SERVICES" not in typed


def _run_env_writer(
    tmp_path,
    *,
    agent,
    hub_agent,
    hub_url,
    hub_token,
    extra_env,
    existing_env_text="",
):
    """Run the deploy env model and return the mac.env values it would write."""
    mac_home = tmp_path / ".mac"
    mac_home.mkdir(parents=True, exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "MAC_SECRET_KEY": "x" * 40}
    env.update(extra_env)
    return build_mac_env(
        parse_env_text(existing_env_text),
        deploy_env_config(
            tmp_path,
            agent=agent,
            hub_agent=hub_agent,
            hub_url=hub_url,
            hub_token=hub_token,
            worker_mode="loop",
        ),
        environ=env,
    )


_ROUTER_ENV = {
    "MAC_DEPLOY_ROUTER_BACKEND": "inproc",
    "MAC_DEPLOY_ROUTER_PROVIDERS": "nvidia=https://inference-api.nvidia.com/v1,0,key=secret:nvidia-upstream",
    "MAC_DEPLOY_NETWORK_PROVIDER": "tailscale",
}


def test_env_writer_hub_runs_router_locally(tmp_path):
    out = _run_env_writer(
        tmp_path, agent="rocky", hub_agent="rocky",
        hub_url="http://127.0.0.1:8789", hub_token="HUBTOK",
        extra_env={**_ROUTER_ENV, "NVIDIA_API_KEY": "nvapi-SECRET"},
    )
    assert out.get("MAC_ROUTER_BACKEND") == "inproc"
    assert "key=secret:nvidia-upstream" in out.get("MAC_ROUTER_PROVIDERS", "")
    assert out.get("OPENAI_BASE_URL") == "http://127.0.0.1:8789/v1"
    assert out.get("MAC_HERMES_GATEWAY_BASE_URL") == "http://127.0.0.1:8789/v1"
    # the hub's own gateway presents the hub's LOCAL mac token to its local /v1
    assert out.get("OPENAI_API_KEY") == out.get("MAC_API_TOKEN")
    # the hub mounts the image proxy (it has an image key, NVIDIA_API_KEY, to escrow)
    assert out.get("MAC_ROUTER_IMAGE_UPSTREAM") == "https://ai.api.nvidia.com/v1/genai"
    assert out.get("MAC_ROUTER_IMAGE_KEY") == "secret:nvidia-image"


def test_env_writer_spoke_routes_via_hub_with_no_provider_keys(tmp_path):
    out = _run_env_writer(
        tmp_path, agent="natasha", hub_agent="rocky",
        hub_url="http://hub.example:8789", hub_token="HUBTOK",
        # provider key passed UNBLANKED to prove the env-writer never persists it
        # for a spoke (defense-in-depth on top of deploy_host's hub-only gating).
        extra_env={**_ROUTER_ENV, "NVIDIA_API_KEY": "nvapi-SECRET"},
    )
    # no local router / providers on the spoke
    assert "MAC_ROUTER_PROVIDERS" not in out
    assert "MAC_ROUTER_BACKEND" not in out
    # gateway routes through the HUB's /v1 with the HUB token (never the local one)
    assert out.get("OPENAI_BASE_URL") == "http://hub.example:8789/v1"
    assert out.get("MAC_HERMES_GATEWAY_BASE_URL") == "http://hub.example:8789/v1"
    assert out.get("OPENAI_API_KEY") == "HUBTOK"
    assert out.get("OPENAI_API_KEY") != out.get("MAC_API_TOKEN")
    # image-gen routes via the hub too: NVIDIA_API_KEY is the HUB TOKEN (the bearer
    # the NIM tool sends to the hub image proxy), NVIDIA_IMAGE_BASE_URL points at
    # the hub's /v1/genai — the real upstream image key never reaches the spoke.
    assert out.get("NVIDIA_API_KEY") == "HUBTOK"
    assert out.get("NVIDIA_IMAGE_BASE_URL") == "http://hub.example:8789/v1/genai"
    assert "nvapi-SECRET" not in "\n".join(out.values())


def test_env_writer_hub_standalone_router_runs_on_its_own_port(tmp_path):
    out = _run_env_writer(
        tmp_path, agent="rocky", hub_agent="rocky",
        hub_url="http://127.0.0.1:8789", hub_token="HUBTOK",
        extra_env={
            **_ROUTER_ENV,
            "MAC_DEPLOY_ROUTER_BACKEND": "standalone",
            "MAC_DEPLOY_ROUTER_PORT": "8790",
            "NVIDIA_API_KEY": "nvapi-SECRET",
        },
    )
    # The ledger API must NOT mount /v1 (backend != inproc) — the router runs
    # as its own service on MAC_ROUTER_PORT and the hub's gateway points there.
    assert out.get("MAC_ROUTER_BACKEND") == "standalone"
    assert out.get("MAC_ROUTER_PORT") == "8790"
    assert out.get("OPENAI_BASE_URL") == "http://127.0.0.1:8790/v1"
    assert out.get("MAC_HERMES_GATEWAY_BASE_URL") == "http://127.0.0.1:8790/v1"
    assert out.get("OPENAI_API_KEY") == out.get("MAC_API_TOKEN")
    assert "key=secret:nvidia-upstream" in out.get("MAC_ROUTER_PROVIDERS", "")


def test_env_writer_spoke_router_url_override_points_wing_at_replica(tmp_path):
    out = _run_env_writer(
        tmp_path, agent="jordanh-worker1", hub_agent="rocky",
        hub_url="http://hub.example:8789", hub_token="HUBTOK",
        extra_env={
            **_ROUTER_ENV,
            "MAC_DEPLOY_ROUTER_URL": "http://router.gke-wing.internal:8790/v1",
            "NVIDIA_API_KEY": "nvapi-SECRET",
        },
    )
    # Model traffic goes to the nearby replica; the hub-facing token contract
    # and the no-upstream-keys posture are unchanged.
    assert out.get("OPENAI_BASE_URL") == "http://router.gke-wing.internal:8790/v1"
    assert out.get("MAC_HERMES_GATEWAY_BASE_URL") == "http://router.gke-wing.internal:8790/v1"
    assert out.get("NVIDIA_IMAGE_BASE_URL") == "http://router.gke-wing.internal:8790/v1/genai"
    assert out.get("OPENAI_API_KEY") == "HUBTOK"
    assert "MAC_ROUTER_PROVIDERS" not in out
    assert "nvapi-SECRET" not in "\n".join(out.values())
    # Control-plane traffic still targets the hub — only inference moved.
    assert out.get("MAC_HUB_URL") == "http://hub.example:8789"


def test_env_writer_hub_postgres_dsn_replaces_sqlite_authority(tmp_path):
    out = _run_env_writer(
        tmp_path, agent="rocky", hub_agent="rocky",
        hub_url="http://127.0.0.1:8789", hub_token="HUBTOK",
        extra_env={
            **_ROUTER_ENV,
            "MAC_DEPLOY_DATABASE_URL": "postgresql://mac:pw@db.internal:5432/mac",
        },
    )
    assert out.get("MAC_DATABASE_URL") == "postgresql://mac:pw@db.internal:5432/mac"
    # Exactly one durable authority: the DSN displaces the SQLite path.
    assert "MAC_DB" not in out


def test_env_writer_hub_rejects_non_postgres_dsn(tmp_path):
    with pytest.raises(ValueError, match="postgres"):
        _run_env_writer(
            tmp_path, agent="rocky", hub_agent="rocky",
            hub_url="http://127.0.0.1:8789", hub_token="HUBTOK",
            extra_env={
                **_ROUTER_ENV,
                "MAC_DEPLOY_DATABASE_URL": "mysql://nope",
            },
        )


def test_env_writer_hub_gets_evidence_blob_dir_and_spoke_does_not(tmp_path):
    hub = _run_env_writer(
        tmp_path, agent="rocky", hub_agent="rocky",
        hub_url="http://127.0.0.1:8789", hub_token="HUBTOK",
        extra_env=_ROUTER_ENV,
    )
    assert hub.get("MAC_EVIDENCE_BLOB_DIR", "").endswith("evidence-blobs")
    # Option C: hub-side review verification is enabled on the hub only.
    assert hub.get("MAC_REVIEW_HUB_VERIFY") == "1"
    assert hub.get("MAC_HUB_REVIEWER_AUTO_REGISTER") == "1"
    assert hub.get("MAC_HUB_REVIEWER_AGENT_NAME") == "hub-reviewer"
    assert hub.get("MAC_HUB_REVIEWER_AGENT_ID") == "agent_hub-reviewer"
    assert hub.get("MAC_HUB_REVIEWER_MACHINE_ID") == "machine_operator_review"
    spoke = _run_env_writer(
        tmp_path, agent="natasha", hub_agent="rocky",
        hub_url="http://hub.example:8789", hub_token="HUBTOK",
        extra_env=_ROUTER_ENV,
    )
    assert "MAC_EVIDENCE_BLOB_DIR" not in spoke
    assert "MAC_REVIEW_HUB_VERIFY" not in spoke
    assert "MAC_HUB_REVIEWER_AUTO_REGISTER" not in spoke
    assert "MAC_HUB_REVIEWER_AGENT_NAME" not in spoke


def test_env_writer_spoke_without_hub_token_fails_fast(tmp_path):
    with pytest.raises(ValueError, match="hub-facing token distinct"):
        _run_env_writer(
            tmp_path, agent="natasha", hub_agent="rocky",
            hub_url="http://hub.example:8789", hub_token="",
            extra_env={**_ROUTER_ENV, "NVIDIA_API_KEY": ""},
        )


def test_env_writer_hub_without_providers_fails_fast(tmp_path):
    with pytest.raises(ValueError, match="MAC_DEPLOY_ROUTER_PROVIDERS"):
        _run_env_writer(
            tmp_path, agent="rocky", hub_agent="rocky",
            hub_url="http://127.0.0.1:8789", hub_token="HUBTOK",
            extra_env={
                "MAC_DEPLOY_ROUTER_BACKEND": "inproc",
                "MAC_DEPLOY_ROUTER_PROVIDERS": "",
            },
        )


def test_direct_fleet_deploy_loads_authoritative_env_before_defaults():
    script = deploy_script_text()

    assert 'DEPLOY_ENV_FILE="${MAC_DEPLOY_ENV_FILE:-$HOME/.mac/.env}"' in script
    # The env-file load now goes through load_env_file_with_caller_precedence so
    # caller-supplied variables always win over file defaults.
    call = 'load_env_file_with_caller_precedence "$DEPLOY_ENV_FILE"'
    assert 'load_env_file_with_caller_precedence()' in script, (
        "load_env_file_with_caller_precedence function must be defined in deploy-mac-fleet.sh"
    )
    assert call in script, (
        "deploy-mac-fleet.sh must call load_env_file_with_caller_precedence to load the env file"
    )
    assert script.index(call) < script.index('GIT_BRANCH="${MAC_DEPLOY_GIT_BRANCH:-main}"')


def test_direct_fleet_deploy_cutover_values_override_env_file_without_secret_output(
    tmp_path,
):
    script = deploy_script_text()
    function = script.split("load_env_file_with_caller_precedence() {", 1)[1].split(
        "\n}\n\n# Resolve the GitHub credential", 1
    )[0]
    function = "load_env_file_with_caller_precedence() {" + function + "\n}"
    env_file = tmp_path / "deploy.env"
    env_file.write_text(
        "\n".join(
            (
                "MAC_DEPLOY_OPENSHELL=0",
                "MAC_DEPLOY_OPENSHELL_ARGS=from-file",
                "MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE=from-file-runtime",
                "MAC_DEPLOY_OPENSHELL_RUNTIME_INPUT_SHA256=sha256:from-file-runtime-input",
                "MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD=1",
                "MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED=0",
                "MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED=0",
                "MAC_DEPLOY_WORK_PACKAGE_BUNDLE_DIR=/from/file",
                "MAC_DEPLOY_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT=http://127.0.0.1:1",
                "MAC_DEPLOY_HUB_TICK_INTERVAL_SECONDS=0",
                "MAC_DEPLOY_EXECUTION_COHORT_REVISION=99",
                "MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT=0",
                "MAC_DEPLOY_EXECUTION_COHORT_SEED=file-secret-must-never-be-printed",
                "MAC_DEPLOY_SUCCESSOR_HOLD_REASON=from-file-successor",
                "MAC_DEPLOY_GH_TOKEN=file-github-secret-must-never-be-printed",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    expected = {
        "MAC_DEPLOY_OPENSHELL": "1",
        "MAC_DEPLOY_OPENSHELL_ARGS": "--enable --fail-closed",
        "MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE": "caller-runtime",
        "MAC_DEPLOY_OPENSHELL_RUNTIME_INPUT_SHA256": "sha256:caller-runtime-input",
        "MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD": "0",
        "MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED": "1",
        "MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED": "1",
        "MAC_DEPLOY_WORK_PACKAGE_BUNDLE_DIR": "/caller/bundles",
        "MAC_DEPLOY_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT": "http://127.0.0.1:17671",
        "MAC_DEPLOY_HUB_TICK_INTERVAL_SECONDS": "30",
        "MAC_DEPLOY_EXECUTION_COHORT_REVISION": "1",
        "MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT": "50",
        "MAC_DEPLOY_EXECUTION_COHORT_SEED": "caller-secret-must-never-be-printed",
        "MAC_DEPLOY_SUCCESSOR_HOLD_REASON": "caller-successor-hold",
        "MAC_DEPLOY_GH_TOKEN": "caller-github-secret-must-never-be-printed",
    }
    assertions = "\n".join(
        '[ "${%s}" = %s ]' % (name, shlex.quote(value))
        for name, value in expected.items()
    )
    result = subprocess.run(
        [
            shutil.which("bash") or "bash",
            "-c",
            "set -e\n"
            + function
            + '\nload_env_file_with_caller_precedence "$1"\n'
            + assertions
            + "\nprintf '%s\\n' precedence-ok\n",
            "bash",
            str(env_file),
        ],
        env={"PATH": "/usr/bin:/bin", **expected},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "precedence-ok\n"
    combined = result.stdout + result.stderr
    assert "secret-must-never-be-printed" not in combined

    precedence = script.split("local -a _PRECEDENCE_VARS=(", 1)[1].split(")", 1)[0]
    for name in expected:
        assert name in precedence


def test_empty_caller_successor_hold_clears_env_file_default(tmp_path):
    script = deploy_script_text()
    function = script.split("load_env_file_with_caller_precedence() {", 1)[1].split(
        "\n}\n\n# Resolve the GitHub credential", 1
    )[0]
    function = "load_env_file_with_caller_precedence() {" + function + "\n}"
    env_file = tmp_path / "deploy.env"
    env_file.write_text(
        "MAC_DEPLOY_SUCCESSOR_HOLD_REASON=stale-file-successor\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            shutil.which("bash") or "bash",
            "-c",
            "set -e\n"
            + function
            + '\nload_env_file_with_caller_precedence "$1"\n'
            + '[ -z "${MAC_DEPLOY_SUCCESSOR_HOLD_REASON}" ]\n',
            "bash",
            str(env_file),
        ],
        env={"PATH": "/usr/bin:/bin", "MAC_DEPLOY_SUCCESSOR_HOLD_REASON": ""},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    initialization = script.split(
        'SUCCESSOR_HOLD_REASON_RAW="${MAC_DEPLOY_SUCCESSOR_HOLD_REASON:-}"', 1
    )[0]
    assert 'if [ -n "${MAC_DEPLOY_SUCCESSOR_HOLD_REASON:-}" ]; then' in initialization


def test_fleet_deploy_reuses_gh_keyring_token_with_explicit_precedence(tmp_path):
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    match = re.search(
        r"resolve_github_deploy_token\(\) \{.*?\n\}", script, re.DOTALL
    )
    assert match is not None
    function = match.group(0)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/bin/sh\nprintf '%s\\n' keyring-token\n", encoding="utf-8")
    fake_gh.chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "MAC_DEPLOY_GH_TOKEN": "explicit-token",
        "GH_TOKEN": "standard-token",
        "GITHUB_TOKEN": "github-token",
    }
    command = (
        function
        + "\nresolve_github_deploy_token\n"
        + "printf '%s|%s' \"$MAC_DEPLOY_GH_TOKEN\" \"$GITHUB_DEPLOY_CREDENTIAL_SOURCE\""
    )
    explicit = subprocess.run(
        ["bash", "-c", command], env=env, capture_output=True, text=True, check=False
    )
    assert explicit.returncode == 0, explicit.stderr
    assert explicit.stdout == "explicit-token|env:MAC_DEPLOY_GH_TOKEN"

    keyring = subprocess.run(
        ["bash", "-c", command],
        env={"PATH": env["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert keyring.returncode == 0, keyring.stderr
    assert keyring.stdout == "keyring-token|gh-keyring:github.com"


def test_router_topology_preflight_runs_before_remote_mutation():
    script = deploy_script_text()
    main_body = script.split("main() {", 1)[1].split("\n}\n\nmain", 1)[0]

    assert "validate_router_topology_spec()" in script
    validation = main_body.index('validate_router_topology_spec "$spec" "$hub_token"')
    assert validation < main_body.index('run_typed_cohort "$selected_specs_file"')


def test_agent_service_reads_worker_token_from_environment_not_process_argv():
    script = deploy_script_text()
    wrapper = script.split("install_mac_agent_wrapper() {", 1)[1].split(
        "\n}\n", 1
    )[0]

    assert ': "${MAC_WORKER_TOKEN:?MAC_WORKER_TOKEN is required}"' in wrapper
    assert '--token "$MAC_WORKER_TOKEN"' not in wrapper


def _extract_bash_fn(name):
    import re as _re
    script = deploy_script_text()
    m = _re.search(r"\n%s\(\) \{\n(.*?)\n\}\n" % _re.escape(name), script, _re.S)
    assert m, "function %s not found" % name
    return "%s() {\n%s\n}\n" % (name, m.group(1))


def _run_scrub(tmp_path, *, agent, hub_agent, hermes_env_text):
    import subprocess as _sp
    (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
    henv = tmp_path / ".hermes" / ".env"
    henv.write_text(hermes_env_text, encoding="utf-8")
    fn = _extract_bash_fn("scrub_spoke_provider_secrets")
    script = (
        "set -euo pipefail\n"
        "log() { :; }\n"
        'DEPLOY_TS=test; DEPLOY_LOG=/dev/null\n'
        + ('PY=%r\n' % sys.executable)
        + ('HOME=%r; AGENT=%r; SHARED_SERVICES_MANAGER_AGENT=%r\n' % (str(tmp_path), agent, hub_agent))
        + fn
        + "scrub_spoke_provider_secrets\n"
    )
    r = _sp.run(["bash", "-c", script], text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    keys = set()
    for line in henv.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            keys.add(line.split("=", 1)[0].replace("export ", "").strip())
    return keys


_HERMES_ENV = (
    "SLACK_BOT_TOKEN=xoxb-real\n"
    "SLACK_APP_TOKEN=xapp-real\n"
    "MATTERMOST_BOT_TOKEN=mm-real\n"
    "OPENAI_API_KEY=sk-stale-provider\n"
    "NVIDIA_API_KEY=nvapi-stale\n"
    "FAL_KEY=fal-stale\n"
    "ANTHROPIC_API_KEY=sk-ant-stale\n"
    "PERPLEXITY_API_KEY=pplx-stale\n"
    "FIRECRAWL_API_KEY=none\n"
    "MESSAGING_CWD=/home/jkh/.mac/src/mac\n"
    "MAC_HERMES_GATEWAY_API_KEY=hub-token\n"
)


def test_scrub_spoke_provider_secrets_clean_invariant(tmp_path):
    # Re-deploy must converge a spoke's gateway env to a clean invariant: NO
    # upstream provider keys, but messaging tokens + gateway creds preserved.
    keys = _run_scrub(tmp_path, agent="natasha", hub_agent="rocky", hermes_env_text=_HERMES_ENV)
    # upstream provider keys stripped
    for gone in ("OPENAI_API_KEY", "NVIDIA_API_KEY", "FAL_KEY", "ANTHROPIC_API_KEY",
                 "PERPLEXITY_API_KEY", "FIRECRAWL_API_KEY"):
        assert gone not in keys, "%s should be scrubbed from a spoke gateway env" % gone
    # messaging connections + gateway creds + config preserved
    for kept in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "MATTERMOST_BOT_TOKEN",
                 "MESSAGING_CWD", "MAC_HERMES_GATEWAY_API_KEY"):
        assert kept in keys, "%s must be preserved" % kept


def test_scrub_spoke_provider_secrets_is_noop_on_hub(tmp_path):
    # The hub legitimately holds provider keys (it runs the router) — never scrub it.
    keys = _run_scrub(tmp_path, agent="rocky", hub_agent="rocky", hermes_env_text=_HERMES_ENV)
    assert "NVIDIA_API_KEY" in keys and "OPENAI_API_KEY" in keys


def test_scrub_called_for_all_service_flows():
    script = deploy_script_text()
    # Each supervisor flow conditionally escrows on the hub, then always scrubs
    # spoke provider state and syncs messaging against the selected hub.
    assert len(re.findall(r"(?m)^\s+escrow_router_provider_keys$", script)) == 3
    assert len(re.findall(r"(?m)^\s+scrub_spoke_provider_secrets$", script)) == 3
    assert len(re.findall(r"(?m)^\s+sync_messaging_config$", script)) == 3


def test_deploy_host_blanks_provider_keys_for_spokes():
    # Stream B: deploy_host must NOT ship upstream provider keys to spokes (only
    # the hub keeps them). It blanks them before building the remote SSH command.
    script = deploy_script_text()
    deploy_host = script.split("deploy_host() {", 1)[1].split("\nmain() {", 1)[0]
    assert 'router_backend_lc="$(printf' in deploy_host
    condition = 'if [ "$agent" != "$shared_services_manager" ] && [ "$router_backend_lc" = "inproc" ]; then'
    assert condition in deploy_host
    gate = deploy_host.split(condition, 1)[1].split("fi", 1)[0]
    for var in ("nvidia_api_key", "openai_api_key", "anthropic_api_key", "perplexity_api_key"):
        assert ('%s=""' % var) in gate


def test_hub_escrows_router_provider_keys_into_vault():
    # Stream B (B2): the hub escrows each router provider's upstream key into its
    # encrypted vault under the secret:<name> the provider spec references, so the
    # router resolves it from secure storage — keys never plaintext-spread to spokes.
    script = deploy_script_text()
    assert "escrow_router_provider_keys() {" in script
    fn = script.split("escrow_router_provider_keys() {", 1)[1].split(
        "\nsync_messaging_config() {", 1
    )[0]
    # HUB-only, and only when a provider references a vault secret
    assert '[ "$WORKER_MODE" = "loop" ] && [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ] || return 0' in fn
    assert 'case "${MAC_DEPLOY_ROUTER_PROVIDERS:-}" in *key=secret:*)' in fn
    # idempotent: skip names already in the vault
    assert "already in vault; skip" in fn
    # explicit provider -> env-var map (not a brittle derivation); warns loudly on
    # an unmapped provider rather than skipping silently
    assert "from mac.providers import provider_key_env" in fn
    assert "PROVIDER_KEY_ENV = provider_key_env()" in fn
    assert provider_key_env()["nvidia"] == "NVIDIA_API_KEY"
    assert "env_var = PROVIDER_KEY_ENV.get(pid)" in fn
    assert "WARNING no source env var mapped for provider" in fn
    assert '"http://127.0.0.1:%s/secrets" % port' in fn
    assert '"created_by": "deploy"' in fn
    # also escrows the modality-proxy keys (image/audio/video) so the hub's
    # /v1/{genai,audio,video} proxies can resolve them. The IMAGE key is DISTINCT
    # from the chat key (NVIDIA_IMAGE_API_KEY, falling back to NVIDIA_API_KEY).
    assert '"MAC_ROUTER_IMAGE_KEY"' in fn and '"MAC_ROUTER_AUDIO_KEY"' in fn and '"MAC_ROUTER_VIDEO_KEY"' in fn
    assert 'NVIDIA_IMAGE_API_KEY' in fn and 'NVIDIA_AUDIO_API_KEY' in fn and 'NVIDIA_VIDEO_API_KEY' in fn
    assert 'post_secret(_name, _value, ["router-upstream", _modality])' in fn
    # failure is loud but non-fatal (chat won't route until the key is escrowed)
    assert "router provider key escrow failed" in fn
    # invoked on the hub after the API is up, before the gateway, in all three
    # service flows (systemd, launchd, supervisord)
    assert len(re.findall(r"(?m)^\s+escrow_router_provider_keys$", script)) == 3


def test_network_none_spoke_uses_tunnel_forwarded_service_ports():
    # gketun-02: a network=none spoke without a proven direct route reaches hub
    # Qdrant/Firecrawl via the reverse tunnel's localhost forwards. A reachable
    # direct private route must retain its configured service URLs instead.
    script = deploy_script_text()
    start = script.index('if [ "$NETWORK_PROVIDER" = "none" ]')
    block = script[start : start + 180]
    assert '[ "$DEPLOY_DIRECT_HUB" != "1" ]' in block
    assert 'QDRANT_URL_CONFIGURED="http://127.0.0.1:16333"' in script
    assert 'FIRECRAWL_URL_CONFIGURED="http://127.0.0.1:13002"' in script


def test_omniverse_gpu_skills_installed_only_on_gpu_nodes():
    # Omniverse/physical-AI 3D skills are vendored + installed GPU-only (nvidia-smi
    # gate), durably re-extracted on every deploy.
    import tarfile

    asset = ROOT / "deploy" / "skills" / "omniverse-skills.tar.gz"
    assert asset.exists(), "vendored omniverse-skills.tar.gz must be present"
    with tarfile.open(asset) as tf:
        skills = sorted(n.split("/")[1] for n in tf.getnames() if n.endswith("SKILL.md"))
    for expected in (
        "omniverse-kit-app",
        "omniverse-realtime-viewer",
        "omniverse-cad-to-simready",
        "omniverse-usd-performance-tuning",
        "physical-ai-neural-reconstruction",
        "physical-ai-defect-image-generation",
        "physical-ai-video-data-augmentation",
        "physical-ai-infrastructure-setup-and-resilient-scaling",
    ):
        assert expected in skills, f"{expected} missing from vendored skills asset"

    script = deploy_script_text()
    fn = script.split("install_omniverse_gpu_skills() {", 1)[1].split("\ninitialize_hermes_home() {", 1)[0]
    assert "nvidia-smi -L" in fn  # GPU gate
    assert 'deploy/skills/omniverse-skills.tar.gz' in fn
    assert '"$HOME/.hermes/skills"' in fn
    # Invoked only by the onboarding/legacy preparation branch. Typed phase 2
    # retains the exact receipt-proved skills state.
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    legacy = installer.split(
        'if [ "$NODE_ACTION" = legacy-one-shot ]; then\n  initialize_hermes_home', 1
    )[1].split("\nelse\n  log \"typed phase 2 retained", 1)[0]
    assert legacy.index("install_fleet_skills") < legacy.index(
        "install_omniverse_gpu_skills"
    )


def test_reverse_tunnel_program_keeps_retrying_until_key_authorized():
    # gketun-01: install_reverse_tunnel_on_hub runs before the spoke authorizes the
    # hub key, so the tunnel program must keep retrying instead of going FATAL after
    # the default 3 attempts.
    script = deploy_script_text()
    fn = script.split("install_reverse_tunnel_on_hub() {", 1)[1].split("\nuses_direct_mesh_hub", 1)[0]
    assert "startretries=1000" in fn


def test_setup_fleet_build_router_provider_spec():
    # The wizard wires the in-mac router from the providers it collects, using the
    # plain env-var key form the router resolves at use (no vault escrow needed).
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "setup_fleet_mod", ROOT / "scripts" / "setup-fleet.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    build = mod.build_router_provider_spec

    # Stream B (B2): keys are referenced as secret:<name> (resolved from the hub
    # vault, escrowed by the deploy), never plaintext-spread as env-var names.
    assert [p.id for p in ROUTER_PROVIDERS] == list(mod._KNOWN_PROVIDERS)
    assert mod.router_secret_name("nvidia") == "nvidia-upstream"
    assert build({}) == ""
    assert build({"OPENAI_API_KEY": "sk"}) == "openai=https://api.openai.com/v1,1,key=secret:openai-upstream"
    # nvidia is preferred (priority 0); a custom base url is honored.
    assert build(
        {"NVIDIA_API_KEY": "k", "OPENAI_API_KEY": "sk", "OPENAI_BASE_URL": "https://x/v1"}
    ) == (
        "nvidia=https://inference-api.nvidia.com/v1,0,key=secret:nvidia-upstream"
        ";openai=https://x/v1,1,key=secret:openai-upstream"
    )
    # The spec round-trips through the router's own parser.
    from mac.provider_router import providers_from_env

    providers = providers_from_env(
        {"MAC_ROUTER_PROVIDERS": build({"NVIDIA_API_KEY": "k"})}
    )
    assert [p.name for p in providers] == ["nvidia"]
    assert providers[0].api_key_env == "secret:nvidia-upstream"


def test_fleet_deploy_uses_home_scoped_registry_not_legacy_site_config():
    script = deploy_script_text()

    assert "$HOME/.mac/fleets.yaml" in script
    assert "MAC_DEPLOY_FLEETS_CONFIG" in script
    assert "--fleets-config" in script
    assert "--hub <hub-node>" in script
    assert "multiple fleets are configured" in script


def test_agent_startup_self_test_rejects_unsafe_openshell_create_args():
    script = deploy_script_text()
    selftest = script.split('cat > "$selftest" <<', 1)[1].split(
        'cat > "$executor" <<', 1
    )[0]

    assert '"openshell_executor_config": False' in script
    assert "MAC_OPENSHELL_CREATE_ARGS contains forbidden executor arguments" in script
    assert 'arg in {"--env", "--"}' in script
    assert "import shlex" in selftest
    assert "import shutil" in selftest
    assert "OpenShell sandbox is enabled but MAC_OPENSHELL_BIN is not executable" in selftest
    assert "shutil.which(openshell_bin) is None" in selftest
    assert "--site-config" not in script
    assert "MAC_DEPLOY_FLEET_SITE_CONFIG" not in script
    assert "FLEET_SITE_CONFIG" not in script


def test_fleet_deploy_network_provider_contract_is_explicit(tmp_path):
    script = deploy_script_text()
    sample = (ROOT / "deploy" / "fleet" / "config.yaml").read_text(encoding="utf-8")
    legacy_args = [
        str(tmp_path / "mac.env"),
        str(tmp_path / ".mac"),
        str(tmp_path),
        "8789",
        "home",
        "",
        "custom",
        "",
        "http://mesh-hub.example:8789",
        "HUBTOK",
        "127.0.0.1",
        "loop",
        DEFAULT_WORKER_CAPABILITIES,
        "",
        "",
        "1",
        "spoke",
        "systemd",
        "hub",
        "",
        "1",
        "6333",
        "",
        "1",
        "3002",
        "1",
        "http://principal.example:8790/artifacts/",
        "8790",
        "",
        "/artifacts/",
    ]
    hub_env = build_mac_env(
        {},
        deploy_env_config(
            tmp_path,
            agent="hub",
            hub_agent="hub",
            hub_url="http://mesh-hub.example:8789",
            network_provider="tailscale",
        ),
        environ={},
    )
    mesh_spoke_env = build_mac_env(
        {},
        deploy_env_config(
            tmp_path,
            agent="spoke",
            hub_agent="hub",
            hub_url="http://mesh-hub.example:8789",
            network_provider="tailscale",
        ),
        environ={},
    )
    tunnel_spoke_env = build_mac_env(
        {},
        deploy_env_config(
            tmp_path,
            agent="spoke",
            hub_agent="hub",
            hub_url="http://mesh-hub.example:8789",
            network_provider="none",
        ),
        environ={},
    )

    assert "network_provider = text_field(network.get(\"provider\"))" in script
    assert "network.provider must be tailscale, headscale, or none" in script
    assert "Headscale provider requires network.headscale.login_server" in script
    assert "HEADSCALE_HEALTH_URL" in script
    assert "MAC_DEPLOY_HEADSCALE_PREAUTH_KEY_SOURCE" in script
    assert config_from_legacy_args(legacy_args, {}).control.network_provider == "tailscale"
    assert config_from_legacy_args(legacy_args, {}).services.webdav_enabled == "1"
    assert config_from_legacy_args(legacy_args, {}).services.webdav_url == "http://principal.example:8790/artifacts/"
    assert (
        config_from_legacy_args(legacy_args, {"MAC_DEPLOY_NETWORK_PROVIDER": "none"})
        .control.network_provider
        == "none"
    )
    assert (
        config_from_legacy_args(legacy_args, {"NETWORK_PROVIDER": "headscale"})
        .control.network_provider
        == "headscale"
    )
    assert hub_env["MAC_HUB_URL"] == "http://127.0.0.1:8789"
    assert mesh_spoke_env["MAC_HUB_URL"] == "http://mesh-hub.example:8789"
    assert tunnel_spoke_env["MAC_HUB_URL"] == "http://127.0.0.1:18789"
    assert '[ "$WORKER_MODE" = "loop" ] && [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]' in script
    assert "uses_direct_mesh_hub()" in script
    assert 'uses_direct_mesh_hub "$network_provider" "$hub_url"' in script
    assert "skipping reverse-tunnel wait" in script
    assert "network:" in sample
    assert "provider: none" in sample
    assert "provider: headscale" in sample
    assert "webdav:" in sample
    assert "public_path: \"/artifacts/\"" in sample


def test_setup_fleet_wizard_writes_fleet_registry_and_env(tmp_path):
    fleets_config = tmp_path / ".mac" / "fleets.yaml"
    env_file = tmp_path / ".mac" / ".env"
    answers = "\n".join(
        [
            "n",
            "hub",
            "test-fleet",
            "hub",
            "operator@hub.example.internal",
            "",
            "",
            "",
            "",
            "ops",
            "provider/family/hub-model",
            "",
            "n",
            "",
            "",
            "",
            "",
            "n",
            "",
            "",
            "",
            "openai",
            "sk-test",
            "",
            "",
            "n",
            "n",
            "",
        ]
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "setup-fleet.py"),
            "--force",
            "--fleets-config",
            str(fleets_config),
            "--env-file",
            str(env_file),
        ],
        input=answers + "\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    registry = yaml.safe_load(fleets_config.read_text(encoding="utf-8"))
    cfg = registry["fleets"]["hub"]
    env = env_file.read_text(encoding="utf-8")
    assert registry["version"] == 1
    assert cfg["sample"] is False
    assert cfg["fleet_name"] == "test-fleet"
    assert cfg["hub_agent"] == "hub"
    assert cfg["agents"][0]["target"] == "operator@hub.example.internal"
    assert cfg["defaults"]["hermes"]["slack_home_channel_name"] == "ops"
    assert cfg["defaults"]["qdrant"]["required"] is True
    assert cfg["defaults"]["qdrant"]["url"] == "http://hub.example.internal:6333"
    assert cfg["defaults"]["firecrawl"]["required"] is True
    assert cfg["defaults"]["firecrawl"]["url"] == "http://hub.example.internal:3002"
    assert cfg["defaults"]["webdav"]["enabled"] is False
    assert cfg["defaults"]["webdav"]["dns_name"] == ""
    assert cfg["defaults"]["webdav"]["public_host"] == "hub.example.internal"
    assert cfg["defaults"]["webdav"]["public_path"] == "/artifacts/"
    # TokenHub is retired — the wizard no longer writes a tokenhub config block.
    assert "tokenhub" not in cfg["defaults"]
    assert cfg["defaults"]["network"]["provider"] == "tailscale"
    assert cfg["defaults"]["network"]["install"] == "auto"
    assert cfg["defaults"]["network"]["headscale"]["manage"] is False
    assert "MAC_DEPLOY_FLEETS_CONFIG=" in env
    assert "MAC_DEPLOY_HUB_AGENT=hub" in env
    assert "MAC_DEPLOY_FLEET_SITE_CONFIG=" not in env
    assert "MAC_DEPLOY_HUB_URL=" not in env
    assert "MAC_SECRET_KEY" not in env
    # KEY INSIGHT fix: the wizard must wire the in-mac router (TokenHub's
    # replacement) from the collected providers, or a fresh fleet has no chat
    # routing. The test adds the "openai" provider with key "sk-test".
    # The wizard writes the RUNTIME names the deploy reads via fleet_scoped_env —
    # NOT the MAC_DEPLOY_*-prefixed names (those would be inert; the deploy never
    # reads them on the operator side). Cross-check both ends so the names can't
    # silently diverge again.
    assert "MAC_ROUTER_BACKEND=inproc" in env
    assert "MAC_ROUTER_PROVIDERS=openai=https://api.openai.com/v1,1,key=secret:openai-upstream" in env
    assert "OPENAI_API_KEY=sk-test" in env
    assert "MAC_DEPLOY_ROUTER_BACKEND=" not in env and "MAC_DEPLOY_ROUTER_PROVIDERS=" not in env
    deploy = deploy_script_text()
    assert 'fleet_scoped_env MAC_ROUTER_BACKEND' in deploy
    assert 'fleet_scoped_env MAC_ROUTER_PROVIDERS' in deploy
    assert "MAC_API_TOKEN" not in env


def test_deploy_accepts_canonical_mapping_shaped_agent_registry(tmp_path):
    registry = tmp_path / "fleets.yaml"
    registry.write_text(
        """
fleets:
  default:
    sample: false
    fleet_name: default
    hub_agent: hub
    control_port: 8789
    agents:
      hub:
        target: operator@hub.example.internal
        os: darwin
""".lstrip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-",
            "specs",
            str(ROOT / "deploy" / "fleet" / "config.yaml"),
            str(registry),
            "hub",
            "hub",
        ],
        input=fleet_config_query_source(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split("|", 2)[:2] == [
        "hub",
        "operator@hub.example.internal",
    ]


@pytest.mark.parametrize(
    "agents_yaml",
    [
        """
agents:
  hub:
    target: operator@hub.example.internal
    os: darwin
""",
        """
agents:
  - name: hub
    target: operator@hub.example.internal
    os: darwin
""",
    ],
    ids=["mapping-agents", "list-agents"],
)
def test_deploy_accepts_flat_single_fleet_registry(tmp_path, agents_yaml):
    registry = tmp_path / "fleets.yaml"
    registry.write_text(
        (
            """
sample: false
fleet_name: default
hub_agent: hub
control_port: 8789
"""
            + agents_yaml
        ).lstrip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-",
            "specs",
            str(ROOT / "deploy" / "fleet" / "config.yaml"),
            str(registry),
            "hub",
            "hub",
        ],
        input=fleet_config_query_source(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split("|", 2)[:2] == [
        "hub",
        "operator@hub.example.internal",
    ]


def test_deploy_rejects_flat_route_only_registry_before_building_specs(tmp_path):
    registry = tmp_path / "fleets.yaml"
    registry.write_text(
        """
hub_agent: hub
agents:
  hub:
    target: operator@hub.example.internal
    os: darwin
""".lstrip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-",
            "specs",
            str(ROOT / "deploy" / "fleet" / "config.yaml"),
            str(registry),
            "hub",
            "hub",
        ],
        input=fleet_config_query_source(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "route-only target registry" in result.stderr
    assert "sample: false" in result.stderr


def test_deploy_rejects_route_only_registry_before_building_specs(tmp_path):
    registry = tmp_path / "fleets.yaml"
    registry.write_text(
        """
fleets:
  default:
    hub_agent: hub
    agents:
      hub:
        target: operator@hub.example.internal
        os: darwin
""".lstrip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-",
            "specs",
            str(ROOT / "deploy" / "fleet" / "config.yaml"),
            str(registry),
            "hub",
            "hub",
        ],
        input=fleet_config_query_source(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "route-only target registry" in result.stderr
    assert "sample: false" in result.stderr


def test_setup_fleet_wizard_can_write_explicit_headscale_provider(tmp_path):
    fleets_config = tmp_path / ".mac" / "fleets.yaml"
    env_file = tmp_path / ".mac" / ".env"
    answers = "\n".join(
        [
            "n",
            "hub",
            "headscale-fleet",
            "hub",
            "operator@hub.example.internal",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "n",
            "",
            "",
            "",
            "",
            "n",
            "headscale",
            "external",
            "https://headscale.example.internal",
            "",
            "",
            "",
            "hs-preauth-key",
            "",
            "",
            "",
            "n",
            "n",
            "openai",
            "sk-test",
            "",
            "",
            "n",
            "n",
            "",
        ]
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "setup-fleet.py"),
            "--force",
            "--fleets-config",
            str(fleets_config),
            "--env-file",
            str(env_file),
        ],
        input=answers + "\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    registry = yaml.safe_load(fleets_config.read_text(encoding="utf-8"))
    network = registry["fleets"]["hub"]["defaults"]["network"]
    env = env_file.read_text(encoding="utf-8")

    assert network["provider"] == "headscale"
    assert network["headscale"]["manage"] is False
    assert network["headscale"]["login_server"] == "https://headscale.example.internal"
    assert network["headscale"]["health_url"] == "https://headscale.example.internal/health"
    assert network["headscale"]["preauth_key_source"] == "env"
    assert network["headscale"]["preauth_key_env"] == "MAC_DEPLOY_HEADSCALE_PREAUTHKEY"
    assert network["headscale"]["dns"] == "magicdns"
    assert "MAC_DEPLOY_HEADSCALE_PREAUTHKEY=hs-preauth-key" in env


def test_ssh_target_parser_supports_inline_and_explicit_ports():
    target = parse_ssh_target("horde@20.115.163.162:2201")
    assert target.user_host == "horde@20.115.163.162"
    assert target.port == 2201
    assert target.ssh_args() == ["-p", "2201"]
    assert target.scp_args() == ["-P", "2201"]

    override = parse_ssh_target("operator@hub.example.internal", port=2222)
    assert override.user_host == "operator@hub.example.internal"
    assert override.port == 2222


def test_setup_fleet_wizard_new_hub_is_noninteractive_and_custom_port_aware(tmp_path):
    fleets_config = tmp_path / ".mac" / "fleets.yaml"
    env_file = tmp_path / ".mac" / ".env"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "setup-fleet.py"),
            "--force",
            "--new-hub",
            "horde",
            "--target",
            "horde@20.115.163.162:2201",
            "--fleet-name",
            "horde-fleet",
            "--fleets-config",
            str(fleets_config),
            "--env-file",
            str(env_file),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    registry = yaml.safe_load(fleets_config.read_text(encoding="utf-8"))
    cfg = registry["fleets"]["horde"]
    assert cfg["fleet_name"] == "horde-fleet"
    assert cfg["hub_agent"] == "horde"
    assert cfg["agents"][0]["target"] == "horde@20.115.163.162:2201"
    assert cfg["agents"][0]["worker"]["mode"] == "loop"
    assert cfg["agents"][0]["control_bind_host"] == "0.0.0.0"
    assert "MAC_SECRET_KEY=" in env_file.read_text(encoding="utf-8")


def test_setup_entrypoints_are_python_driven_and_make_exposed():
    script = (ROOT / "setup.sh").read_text(encoding="utf-8")
    setup_py = (ROOT / "setup.py").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    deploy = deploy_script_text()

    assert script.startswith("#!/bin/sh")
    assert "exec \"$PYTHON\" \"$ROOT/setup.py\" \"$@\"" in script
    assert "BASH_SOURCE" not in script
    assert "read -r -d" not in script
    assert "DEPLOY_FLEET = ROOT / \"deploy\" / \"deploy-mac-fleet.sh\"" in setup_py
    assert "DEFAULT_ENV_FILE = Path.home() / \".mac\" / \".env\"" in setup_py
    assert "def parse_setup_args" in setup_py
    assert "def configure_then_deploy" in setup_py
    assert "def deploy_env" in setup_py
    assert 'PYTHON ?= $(shell for candidate in "$(VENV)/bin/python" python3.11 python3 python' in makefile
    assert "sys.version_info >= (3, 11)" in makefile
    assert "setup: require-python" in makefile
    assert "deploy: require-python" in makefile
    assert "--(hub|new-hub)" in makefile
    assert "resolve_python_bin" in deploy
    assert "\"$PYTHON_BIN\" \"${setup_args[@]}\"" in deploy
    assert "--fleet-name)" in deploy
    assert "setup_args+=(--fleet-name" in deploy
    assert "setup_args+=(--control-port" in deploy
    assert "setup_args+=(--network-provider" in deploy


def test_setup_fleet_writes_deploy_plan_for_new_hub(tmp_path):
    fleets_config = tmp_path / "fleets.yaml"
    env_file = tmp_path / ".env"
    plan_file = tmp_path / "deploy-plan.json"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "setup-fleet.py"),
            "--force",
            "--new-hub",
            "hub1",
            "--target",
            "ops@example.internal:2222",
            "--fleets-config",
            str(fleets_config),
            "--env-file",
            str(env_file),
            "--deploy-plan-file",
            str(plan_file),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    assert plan["hub"] == "hub1"
    assert plan["agents"] == ["hub1"]
    assert plan["env_file"] == str(env_file)


def test_fleet_deploy_handles_custom_ssh_ports_reconciliation_and_disk_hygiene():
    script = deploy_script_text()
    cleanup_plan = "\n".join(cleanup_path_strings(Path.home(), Path.home() / ".mac"))

    assert "--ssh-port <port>" in script
    assert "fleet_ssh_route_args()" in script
    assert "--port-override" in script
    assert "fenced_remote_upload()" in script
    assert '-S "$control_path"' in script
    assert "ProxyCommand=/usr/bin/false" in script
    assert "printf '%s\\0' -S \"$control_path\" -O proxy" not in script
    assert "scp -O" not in script
    assert "ssh -A -o BatchMode=yes -o ConnectTimeout=10" in script
    assert script.count("-o ServerAliveInterval=30 -o ServerAliveCountMax=6") >= 10
    assert "reconcile_remote_deploy()" in script
    assert "remote reconciliation succeeded" in script
    assert "disk_hygiene_report" in script
    assert "cleanup_obsolete_deploy_artifacts" in script
    assert "obsolete ACC-derived artifact" in script
    assert "disk-before-cleanup" in script
    assert "disk_after_cleanup" in script
    assert "generated MAC deploy backups" in cleanup_plan
    assert ".acc/build" in cleanup_plan
    assert ".acc/deploy" in cleanup_plan
    assert ".acc/logs" in cleanup_plan
    assert ".acc/hermes-agent" in cleanup_plan


def _run_reconcile_remote_deploy(
    tmp_path,
    *,
    deploy_ts="20260707T182907Z",
    deploy_rev="a" * 40,
    clear_repo_update_blocker=False,
):
    script = deploy_script_text()
    function_text = "reconcile_remote_deploy() {" + script.split(
        "reconcile_remote_deploy() {", 1
    )[1].split("\nset_remote_mac_agent_service() {", 1)[0]
    fence_function = "remote_deployment_fenced_exec() {" + script.split(
        "remote_deployment_fenced_exec() {", 1
    )[1].split("\n}\n\nstream_file_after_remote_fence() {", 1)[0] + "\n}"
    mac_home = tmp_path / ".mac"
    (mac_home / "logs").mkdir(parents=True, exist_ok=True)
    lock_dir = mac_home / "deploy-controller.lock"
    lock_dir.mkdir()
    deployment_nonce = "reconcile-test-nonce"
    (lock_dir / "owner.json").write_text(
        json.dumps(
            {
                "deployment_id": (
                    f"{deploy_rev}:rocky:{deploy_ts}:{deployment_nonce}"
                )
            }
        ),
        encoding="utf-8",
    )
    snippet = f"""
set -euo pipefail
TS={shlex.quote(deploy_ts)}
GIT_REV={shlex.quote(deploy_rev)}
DEPLOY_CONTROLLER_NONCE={shlex.quote(deployment_nonce)}
shell_quote() {{
  python3 - "$1" <<'PY'
import shlex
import sys
print(shlex.quote(sys.argv[1]))
PY
}}
ssh_target_args() {{
  printf '%s\\0' fake-host
}}
ssh() {{
  local remote_cmd="${{!#}}"
  bash -c "$remote_cmd"
}}
{fence_function}
{function_text}
reconcile_remote_deploy rocky fake-target {int(clear_repo_update_blocker)}
"""
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "MAC_HOME": str(mac_home),
        "MAC_DEPLOY_RECONCILE_MAX_RETRIES": "1",
        "PATH": os.environ.get("PATH", ""),
    }
    script_path = tmp_path / "reconcile-remote-deploy-test.sh"
    script_path.write_text(snippet, encoding="utf-8")
    return subprocess.run(
        ["bash", str(script_path)],
        text=True,
        capture_output=True,
        env=env,
    )


def test_remote_deploy_reconciliation_fails_when_zero_exit_left_no_post_manifest(tmp_path):
    result = _run_reconcile_remote_deploy(tmp_path)

    assert result.returncode != 0
    assert "missing post manifest" in result.stderr


def test_remote_deploy_reconciliation_validates_latest_manifest_structure(tmp_path):
    deploy_ts = "20260707T182907Z"
    deploy_rev = "a" * 40
    mac_home = _write_reconciliation_evidence(tmp_path, deploy_ts, deploy_rev)
    log_dir = mac_home / "logs"
    manifest = json.loads(
        (log_dir / f"deploy-manifest-{deploy_ts}-post.json").read_text(
            encoding="utf-8"
        )
    )
    (log_dir / "deploy-manifest-latest.json").write_text(
        json.dumps({**manifest, "stage": "pre"}), encoding="utf-8"
    )

    result = _run_reconcile_remote_deploy(
        tmp_path, deploy_ts=deploy_ts, deploy_rev=deploy_rev
    )

    assert result.returncode != 0
    assert "latest manifest stage is 'pre'" in result.stderr


def test_remote_deploy_reconciliation_accepts_matching_post_and_latest_manifests(tmp_path):
    deploy_ts = "20260707T182907Z"
    deploy_rev = "b" * 40
    mac_home = _write_reconciliation_evidence(tmp_path, deploy_ts, deploy_rev)
    # Spokes have a mac.env but intentionally do not run a loopback control
    # plane. Reconciliation must rely on the role-aware remote health gate and
    # matching durable manifests instead of probing 127.0.0.1:8789.
    (mac_home / "mac.env").write_text("MAC_PORT=9\n", encoding="utf-8")

    result = _run_reconcile_remote_deploy(
        tmp_path, deploy_ts=deploy_ts, deploy_rev=deploy_rev
    )

    assert result.returncode == 0, result.stderr
    assert "remote reconciliation succeeded for rocky" in result.stdout


def test_remote_deploy_reconciliation_rejects_media_readiness_divergence(tmp_path):
    deploy_ts = "20260707T182907Z"
    deploy_rev = "9" * 40
    mac_home = _write_reconciliation_evidence(tmp_path, deploy_ts, deploy_rev)
    latest_path = mac_home / "logs" / "deploy-manifest-latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    latest["media_runtime_readiness"]["resources"].reverse()
    latest_path.write_text(json.dumps(latest), encoding="utf-8")

    result = _run_reconcile_remote_deploy(
        tmp_path, deploy_ts=deploy_ts, deploy_rev=deploy_rev
    )

    assert result.returncode != 0
    assert "manifest media runtime readiness diverged" in result.stderr


def _write_reconciliation_evidence(tmp_path, deploy_ts, deploy_rev):
    mac_home = tmp_path / ".mac"
    log_dir = mac_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    generation = f"{deploy_rev}:rocky:{deploy_ts}:reconcile-test-nonce"
    quiescence = {
        "schema": "mac.daemon_resource_quiescence_manifest.v1",
        "status": "proved",
        "generation": generation,
        "revision": deploy_rev,
        "sha256": "1" * 64,
        "required_phases": ["pre_source"],
        "proved_phases": ["pre_source"],
        "container_runtimes": [],
        "gateway_implementation": "none",
    }
    media_resources = [
        {
            "name": name,
            "prior_state": "active",
            "state": "active",
            "enabled_state": "enabled",
            "stable_observations": 2,
        }
        for name in (
            "mac-gen-server.service",
            "mac-gen-audio-server.service",
            "mac-gen-video-server.service",
        )
    ]
    phase1 = {
        "schema": "mac.phase1_cohort_quiescence_manifest.v1",
        "status": "proved",
        "generation": generation,
        "revision": deploy_rev,
        "sha256": "2" * 64,
        "supervisor": {"manager": "systemd"},
        "daemon_resource_receipt": {
            "schema": "mac.daemon_resource_quiescence.v1",
            "proof_phase": "pre_source",
            "sha256": "3" * 64,
            "function_block_sha256": "4" * 64,
        },
    }
    gateway = {
        "schema": "mac.gateway_readiness_manifest.v1",
        "status": "proved",
        "generation": generation,
        "revision": deploy_rev,
        "sha256": "5" * 64,
        "stable_observations": 2,
        "implementation": "none",
        "supervisor": "systemd",
        "identities": {},
        "state": {},
    }
    restore_contract = {
        "schema": "mac.phase1_cohort_restore_contract.v1",
        "status": "prepared",
        "agent": "rocky",
        "generation": generation,
        "revision": deploy_rev,
        "supervisor": {"manager": "systemd"},
    }
    restore_path = mac_home / f"phase1-cohort-restore-contract-{generation}.json"
    restore_raw = json.dumps(restore_contract, sort_keys=True).encode()
    restore_path.write_bytes(restore_raw)
    restore_path.chmod(0o600)
    restore_digest = hashlib.sha256(restore_raw).hexdigest()
    media_receipt = {
        "schema": "mac.phase1_supervisor_media_resume.v1",
        "agent": "rocky",
        "fleet": "mac",
        "os_kind": "linux",
        "generation": generation,
        "revision": deploy_rev,
        "source_contract_sha256": restore_digest,
        "supervisor": {
            "manager": "systemd",
            "media_resources": media_resources,
        },
    }
    media_path = mac_home / f"phase1-supervisor-resume_media-{generation}.json"
    media_raw = json.dumps(media_receipt, sort_keys=True).encode()
    media_path.write_bytes(media_raw)
    media_path.chmod(0o600)
    media = {
        "schema": "mac.media_runtime_readiness_manifest.v1",
        "status": "proved",
        "path": str(media_path),
        "sha256": hashlib.sha256(media_raw).hexdigest(),
        "source_contract_sha256": restore_digest,
        "manager": "systemd",
        "resources": media_resources,
    }
    manifest = {
        "stage": "post",
        "agent": "rocky",
        "deploy": {"timestamp": deploy_ts, "mac_git_rev": deploy_rev},
        "daemon_resource_quiescence": quiescence,
        "phase1_cohort_quiescence": phase1,
        "media_runtime_readiness": media,
        "gateway_readiness": gateway,
    }
    (log_dir / f"deploy-manifest-{deploy_ts}-post.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (log_dir / "deploy-manifest-latest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (log_dir / f"deploy-{deploy_ts}.log").write_text(
        "deploy complete\n", encoding="utf-8"
    )
    (mac_home / "deployed-source-revision").write_text(
        deploy_rev + "\n", encoding="utf-8"
    )
    return mac_home


def test_remote_deploy_reconciliation_clears_holds_only_after_exact_revision(tmp_path):
    deploy_ts = "20260707T182907Z"
    deploy_rev = "c" * 40
    mac_home = _write_reconciliation_evidence(tmp_path, deploy_ts, deploy_rev)
    default_blocker = mac_home / "repo-update-dispatch-blocked.json"
    configured_blocker = mac_home / "state" / "custom blocker.json"
    configured_blocker.parent.mkdir()
    default_blocker.write_text("blocked\n", encoding="utf-8")
    configured_blocker.write_text("blocked\n", encoding="utf-8")
    (mac_home / "mac.env").write_text(
        'MAC_REPO_UPDATE_DISPATCH_BLOCKER_FILE="state/custom blocker.json"\n',
        encoding="utf-8",
    )

    result = _run_reconcile_remote_deploy(
        tmp_path,
        deploy_ts=deploy_ts,
        deploy_rev=deploy_rev,
        clear_repo_update_blocker=True,
    )

    assert result.returncode == 0, result.stderr
    assert not default_blocker.exists()
    assert not configured_blocker.exists()
    assert "dispatch hold cleared after exact deployment reconciliation" in result.stdout


def test_remote_deploy_reconciliation_preserves_hold_on_revision_mismatch(tmp_path):
    deploy_ts = "20260707T182907Z"
    deploy_rev = "d" * 40
    mac_home = _write_reconciliation_evidence(tmp_path, deploy_ts, deploy_rev)
    blocker = mac_home / "repo-update-dispatch-blocked.json"
    blocker.write_text("blocked\n", encoding="utf-8")
    (mac_home / "deployed-source-revision").write_text("e" * 40 + "\n", encoding="utf-8")

    result = _run_reconcile_remote_deploy(
        tmp_path,
        deploy_ts=deploy_ts,
        deploy_rev=deploy_rev,
        clear_repo_update_blocker=True,
    )

    assert result.returncode != 0
    assert "deployed source revision does not match requested revision" in result.stderr
    assert blocker.exists()


def test_remote_deploy_reconciliation_preserves_hold_when_env_file_cannot_load(tmp_path):
    deploy_ts = "20260707T182907Z"
    deploy_rev = "f" * 40
    mac_home = _write_reconciliation_evidence(tmp_path, deploy_ts, deploy_rev)
    blocker = mac_home / "repo-update-dispatch-blocked.json"
    blocker.write_text("blocked\n", encoding="utf-8")
    (mac_home / "mac.env").write_text("this is not valid shell (\n", encoding="utf-8")

    result = _run_reconcile_remote_deploy(
        tmp_path,
        deploy_ts=deploy_ts,
        deploy_rev=deploy_rev,
        clear_repo_update_blocker=True,
    )

    assert result.returncode != 0
    assert "could not resolve repository-update blocker path" in result.stderr
    assert blocker.exists()


def test_fleet_deploy_validates_post_manifest_after_zero_exit_ssh():
    # Secret input is opened only after the exact deployment fence emits READY
    # on the same pinned SSH session. Reconciliation still runs after either a
    # zero or non-zero remote install exit.
    deploy = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    deploy_host = deploy.split("deploy_host() {", 1)[1].split(
        "\n}\n\nhub_target()", 1
    )[0]
    ssh_section = deploy_host.split("local remote_cmd", 1)[1]
    deploy_host_tail = ssh_section.split('ssh -A -o BatchMode=yes', 1)[1]

    assert "printf '%s\\n' \"${remote_secret_env[@]}\" > \"$local_secret_payload\"" in ssh_section
    assert 'stream_file_after_remote_fence "$local_secret_payload"' in ssh_section
    assert '"MAC_DEPLOY_FENCE_READY:${deploy_generation}"' in ssh_section
    assert "ssh -A -o BatchMode=yes" in ssh_section
    assert 'echo "==> ${agent}: validating remote post-deploy manifest"' in deploy_host_tail
    assert (
        'if ! reconcile_remote_deploy "$agent" "$target" '
        '"$openshell_disable_requested"; then' in deploy_host_tail
    )
    assert "remote deploy returned success but post manifest validation failed" in deploy_host_tail
    assert 'echo "==> ${agent}: ssh exited non-zero; reconciling remote deploy state"' in deploy_host_tail


def test_pure_worker_deploy_requires_openshell_and_github_credentials(tmp_path):
    base = {
        "sample": True,
        "fleet_name": "example",
        "hub_agent": "hub",
        "hub_url": "http://hub.example:8789",
        "agents": [],
    }
    registry = {
        "version": 1,
        "fleets": {
            "hub": {
                "sample": False,
                "fleet_name": "test",
                "hub_agent": "hub",
                "hub_url": "http://hub.example:8789",
                "agents": {
                    "hub": {
                        "target": "operator@hub.example",
                        "os": "linux",
                        "hermes": {"gateway_impl": "openclaw"},
                    },
                    "worker-1": {
                        "target": "operator@worker.example",
                        "os": "linux",
                        "supervisor": "supervisord",
                        "hermes": {"gateway_impl": "none"},
                        "worker": {"mode": "loop"},
                    },
                },
            }
        },
    }
    base_path = tmp_path / "base.yaml"
    registry_path = tmp_path / "fleets.yaml"
    base_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    registry_path.write_text(yaml.safe_dump(registry), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            fleet_config_query_source(),
            "specs",
            str(base_path),
            str(registry_path),
            "hub",
            "worker-1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("|")[-2:] == ["1", "1"]

    deploy = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    assert 'add_remote_env MAC_DEPLOY_OPENSHELL_REQUIRED "$openshell_required"' in deploy
    assert (
        'add_remote_env MAC_DEPLOY_GITHUB_CREDENTIALS_REQUIRED '
        '"$github_credentials_required"'
    ) in deploy
    assert 'add_remote_env MAC_DEPLOY_OPENSHELL_ENABLED "$openshell_enabled"' in deploy
    assert (
        'add_remote_env MAC_DEPLOY_OPENSHELL_EFFECTIVE_ARGS '
        '"$effective_openshell_args"'
    ) in deploy
    assert (
        'add_remote_env MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE '
        '"${MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE:-}"'
    ) in deploy
    assert (
        'add_remote_env MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD '
        '"${MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD:-0}"'
    ) in deploy
    assert "run_openshell_bootstrap" not in deploy
    assert '*" --enable "*' in deploy
    assert '*" --fail-closed "*' in deploy


def test_fleet_deploy_treats_unconfigured_discord_startup_as_benign():
    script = deploy_script_text()
    classifier = script.split("classify_gateway_logs() {", 1)[1].split(
        "verify_hub_registration() {", 1
    )[0]

    assert "discord_missing_token_unconfigured" in classifier
    assert r"\[Discord\] No bot token configured" in classifier
    assert "actionable_text" in classifier
    assert 'if spec["severity"] != "info"' in classifier
    assert 'if spec["severity"] == "info"' in classifier


def test_gateway_log_classifier_accepts_exact_recovered_rocky_startup(tmp_path):
    request_id = "a9ee718d-708b-4426-b155-9f28c3c29f92"
    truncated_request_id = "a9ee718d-708b-4426-b155-9f28c3c29"
    deferred_block = [
        (
            "openclaw cron deferred until device approval: gateway connect failed: "
            "GatewayClientRequestError: scope upgrade pending approval "
            f"(requestId: {request_id})"
        ),
        (
            "GatewayTransportError: gateway closed (1008): pairing required: "
            "device is asking for more scopes than currently approved "
            f"(requestId: {truncated_request_id}"
        ),
        "Gateway target: ws://127.0.0.1:18789",
        "Source: local loopback",
        "Config: /home/sandbox/.config/mac-openclaw/openclaw.json",
        "Bind: lan",
    ]
    result, summary = run_gateway_log_classifier(
        tmp_path,
        "\n".join(
            [
                "Error:   × sandbox 'mac-openclaw-rocky' already exists",
                "Error:   × sandbox 'mac-openclaw-rocky' already exists",
                (
                    "\x1b[1m\x1b[36mCreated sandbox:\x1b[39m\x1b[0m "
                    "\x1b[1mmac-openclaw-rocky\x1b[0m"
                ),
                "2026-07-18T01:02:03.456Z [gateway] ready",
                *deferred_block,
                *deferred_block,
                *deferred_block,
                *deferred_block,
            ]
        )
        + "\n",
    )

    assert result.returncode == 0, result.stderr
    assert summary["actionable_count"] == 0
    assert summary["classes"] == [
        {"count": 2, "name": "openclaw_sandbox_create_recovered", "severity": "info"},
        {"count": 4, "name": "openclaw_cron_device_approval_deferred", "severity": "info"},
    ]


def test_gateway_log_classifier_classifies_sanitized_cron_deferrals(tmp_path):
    result, summary = run_gateway_log_classifier(
        tmp_path,
        "\n".join(
            [
                "openclaw cron deferred until device approval: scope_upgrade_pending_approval",
                "openclaw cron deferred until device approval: pairing_required",
            ]
        )
        + "\n",
    )

    assert result.returncode == 0, result.stderr
    assert summary["actionable_count"] == 0
    assert summary["classes"] == [
        {"count": 2, "name": "openclaw_cron_device_approval_deferred", "severity": "info"},
    ]


def test_gateway_log_classifier_requires_later_create_and_readiness(tmp_path):
    result, summary = run_gateway_log_classifier(
        tmp_path,
        "\n".join(
            [
                "Created sandbox: mac-openclaw-rocky",
                "2026-07-18T01:02:03.456Z [gateway] ready",
                "Error:   × sandbox 'mac-openclaw-rocky' already exists",
                "Error:   × sandbox 'mac-openclaw-natasha' already exists",
                "Created sandbox: mac-openclaw-natasha",
            ]
        )
        + "\n",
    )

    assert result.returncode == 1
    assert summary["actionable_count"] == 1
    assert {item["name"] for item in summary["classes"]} == {"traceback"}
    assert next(item for item in summary["classes"] if item["name"] == "traceback")[
        "count"
    ] == 2


def test_gateway_log_classifier_only_blanks_positionally_recovered_collision(tmp_path):
    result, summary = run_gateway_log_classifier(
        tmp_path,
        "\n".join(
            [
                "Error:   × sandbox 'mac-openclaw-rocky' already exists",
                "Created sandbox: mac-openclaw-rocky",
                "2026-07-18T01:02:03.456Z [gateway] ready",
                "Error:   × sandbox 'mac-openclaw-rocky' already exists",
            ]
        )
        + "\n",
    )

    assert result.returncode == 1
    assert summary["actionable_count"] == 1
    assert summary["classes"] == [
        {"count": 1, "name": "openclaw_sandbox_create_recovered", "severity": "info"},
        {"count": 1, "name": "traceback", "severity": "error"},
    ]


def test_gateway_log_classifier_rejects_mismatched_or_tainted_cron_deferral(tmp_path):
    result, summary = run_gateway_log_classifier(
        tmp_path,
        "\n".join(
            [
                (
                    "openclaw cron deferred until device approval: gateway connect failed: "
                    "GatewayClientRequestError: scope upgrade pending approval "
                    "(requestId: a9ee718d-708b-4426-b155-9f28c3c29f92)"
                ),
                (
                    "GatewayTransportError: gateway closed (1008): pairing required: "
                    "device is asking for more scopes than currently approved "
                    "(requestId: deadbeef-708b-4426-b155-9f28c3c29"
                ),
                (
                    "openclaw cron deferred until device approval: gateway connect failed: "
                    "GatewayClientRequestError: scope upgrade pending approval "
                    "(requestId: a9ee718d-708b-4426-b155-9f28c3c29f92) "
                    "ERROR database failed"
                ),
                (
                    "GatewayTransportError: gateway closed (1008): pairing required: "
                    "device is asking for more scopes than currently approved "
                    "(requestId: a9ee718d-708b-4426-b155-9f28c3c29"
                ),
                "ERROR pairing required outside the owned warning",
                "Exception scope upgrade pending approval outside the owned warning",
                "Traceback (most recent call last):",
            ]
        )
        + "\n",
    )

    assert result.returncode == 1
    assert summary["actionable_count"] == 1
    assert {item["name"] for item in summary["classes"]} == {"traceback"}


def test_darwin_openclaw_launchd_bootstrap_starts_gateway_once():
    script = deploy_script_text()
    installer = script.split("install_darwin_openclaw_service() {", 1)[1].split(
        "install_darwin_hermes_service() {", 1
    )[0]

    assert "<key>RunAtLoad</key><true/>" in installer
    assert 'mac_launchd_bootstrap_job \\\n    "gui/$uid" "$plist" "gui/$uid/$OPENCLAW_LAUNCHD_LABEL"' in installer
    assert 'launchctl kickstart -k "gui/$uid/$OPENCLAW_LAUNCHD_LABEL"' not in installer


def test_launchd_worker_wrapper_marks_agent_offline_on_controlled_shutdown():
    script = deploy_script_text()
    wrapper = script.split("install_mac_agent_wrapper() {", 1)[1].split(
        'cat > "$executor" <<', 1
    )[0]

    assert "mark_worker_offline()" in wrapper
    assert "stable_agent_id()" in wrapper
    assert 'trap mark_worker_offline TERM INT' in wrapper
    assert '{"status":"offline","health_status":"degraded"}' in wrapper


def test_worker_wrapper_runs_agent_side_startup_self_test(tmp_path):
    script = deploy_script_text()
    generated_env = build_mac_env({}, deploy_env_config(tmp_path), environ={})
    wrapper = script.split("install_mac_agent_wrapper() {", 1)[1].split(
        'cat > "$executor" <<', 1
    )[0]
    selftest = script.split('cat > "$selftest" <<', 1)[1].split(
        'cat > "$executor" <<', 1
    )[0]

    assert generated_env["MAC_AGENT_STARTUP_SELF_TEST"] == "1"
    assert '"$HOME/.mac/bin/mac-agent-startup-self-test"' in wrapper
    assert 'openclaw_config["models"]["providers"]["mac-router"]' in selftest
    assert "MAC_REQUIRE_QDRANT_MEMORY must be true" in selftest
    assert "MAC_REQUIRE_FIRECRAWL must be true" in selftest
    assert '"mandatory_services": {' in selftest
    assert 'str(openclaw_agent_bin)' in selftest
    assert '"MAC_OPENCLAW_STARTUP_OK" in raw_agent_output' in selftest
    assert '"exclusive_service_owner"' in selftest
    assert 'runtime["confinement"].get("provider") != "openshell"' in selftest
    assert "def output_text" in selftest
    assert "output_text(exc.stdout)" in selftest
    assert "classify_openclaw_agent_failure" in selftest
    assert '"openclaw_failure_class": openclaw_failure_class' in selftest
    assert '"blocking_problems": blocking_problems' in selftest
    assert 'payload = {"resources": {"startup_self_test": report}}' in selftest
    assert "if blocking_problems:" in selftest
    assert 'payload.update({"status": "offline", "health_status": "degraded"})' in selftest
    assert '"status": "idle"' not in selftest
    assert "sys.exit(1 if blocking_problems else 0)" in selftest
    assert '"resources": {"startup_self_test": report}' in selftest
    assert '"health_status": "healthy"' not in selftest
    assert '"health_status": "degraded"' in selftest


def test_openshell_bootstrap_supports_noninteractive_macos_path():
    script = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )

    assert "/Applications/Docker.app/Contents/Resources/bin" in script
    assert "/opt/homebrew/bin" in script


def test_executor_prompt_includes_repository_runtime_contract():
    script = (ROOT / "src" / "mac" / "executor_prompt.py").read_text(encoding="utf-8")

    assert "def repository_contract_section(task: Dict[str, Any]) -> str:" in script
    assert "Repository runtime contract:" in script
    assert "metadata.runtime.repository_worktree" in script
    assert "origin.repository_path / $MAC_TASK_REPO_SOURCE as read-only" in script
    assert "Agent ownership ends with tested task-worktree changes" in script
    assert "deterministic host finalizer exclusively owns fetching" in script
    assert "bootstrap.command" in script
    assert "test.command" in script
    assert ".mac-executor-policy.txt" in script
    policy = (ROOT / "src" / "mac" / "executor-policy.txt").read_text(
        encoding="utf-8"
    )
    assert policy.startswith("mac.executor_policy.v1")
    assert "Write $MAC_TASK_WORKSPACE/mac-evidence.json" in policy


def test_reviewer_prompt_includes_verdict_contract():
    script = (ROOT / "src" / "mac" / "executor_prompt.py").read_text(encoding="utf-8")

    assert "MAC_TASK_REPO_WORKTREE" in script
    assert "local review checkout" in script
    assert "run the repository contract test command" in script
    assert "repo copied from the executor verification repo object" in script
    assert "worktree_digest as sha256" in script
    assert "reviewed_evidence_id=%s" in script


def test_mac_repository_contract_test_command_uses_hermetic_runner():
    contract = yaml.safe_load((ROOT / ".mac" / "project.yaml").read_text(encoding="utf-8"))
    runner = ROOT / "scripts" / "run-contract-tests.sh"

    assert contract["test"]["command"] == "scripts/run-contract-tests.sh"
    assert "gh" in contract["toolchain"]["required_commands"]
    text = runner.read_text(encoding="utf-8")
    assert 'unset "${!MAC_@}"' in text
    # Interpreter is discovered (repo .venv on dev hosts; /opt/mac-venv in the
    # OpenShell task sandbox), not hardcoded to .venv — a hardcoded
    # .venv/bin/python is rc 127 in-sandbox and blocked every code task.
    # Do not require ``exec`` here: focused runs use a temporary HOME that the
    # runner must remove after pytest returns.  The contract is interpreter
    # discovery plus lossless forwarding of the caller's pytest arguments.
    assert '"$PY" -m pytest "$@"' in text
    assert "/opt/mac-venv/bin/python" in text


def test_source_install_pins_exact_rev_not_ff_only():
    """The remote source install must pin the worktree to the operator's exact
    $DEPLOY_REV via fetch+reset. `git merge --ff-only $DEPLOY_REV` aborts with
    "Not possible to fast-forward" when origin/<branch> advanced past the
    operator's local HEAD mid-session, leaving the spoke half-deployed."""
    script = deploy_script_text()
    assert 'reset --hard --quiet "$DEPLOY_REV"' in script
    # the ff-only merge *command* must be gone (a comment mentioning it is fine)
    assert 'merge --ff-only "$DEPLOY_REV"' not in script
    assert "install_archive_source=1" in script
    assert "using the exact deployment archive" in script
    assert 'actual_rev" = "$DEPLOY_REV"' in script
    assert 'deployed_source_revision_file="$MAC_HOME/deployed-source-revision"' in script
    assert 'printf \'%s\\n\' "$DEPLOY_REV" > "$deployed_source_revision_tmp"' in script
    assert 'chmod 0600 "$deployed_source_revision_tmp"' in script


def test_deploy_archive_is_pinned_to_captured_revision():
    """Archive fallback and deploy proof must describe the same immutable tree.

    The deploy driver captures GIT_REV once.  Archiving symbolic HEAD after
    that point races with concurrent commits in the shared operator checkout
    and can otherwise label different source as the captured revision.
    """
    script = deploy_script_text()
    assert (
        'git -C "$ROOT" archive --format=tar.gz --output="$ARCHIVE" "$GIT_REV"'
        in script
    )
    assert 'git -C "$ROOT" archive --format=tar.gz --output="$ARCHIVE" HEAD' not in script


def test_media_key_escrow_has_no_chat_key_fallback():
    """Media (image/audio/video) upstream keys are SEPARATE entitlements from the
    chat key; the deploy escrow must source only the per-modality var and never
    fall back to NVIDIA_API_KEY — escrowing the chat key under e.g. nvidia-image
    yields a runtime 401 from NVIDIA's genai API instead of a clean disabled."""
    script = deploy_script_text()
    assert '("image", "MAC_ROUTER_IMAGE_KEY", "NVIDIA_IMAGE_API_KEY")' in script
    # the old chat-key fallback must be gone
    assert 'os.environ.get("NVIDIA_API_KEY") or ""' not in script.split("media_status", 1)[-1]
    # and the deploy reports which media ops are enabled vs disabled
    assert "media ops:" in script
    assert "DISABLED: set" in script


def test_build_mac_env_passes_through_local_gen_advertisement(tmp_path):
    """media-01 durable advertisement: deploy-supplied MAC_DEPLOY_AGENT_GEN_*
    flow into mac.env as MAC_AGENT_GEN_* (the agent self-advertises, GPU-gated)."""
    values = build_mac_env(
        {},
        deploy_env_config(tmp_path, fleet_name="test-fleet"),
        environ={
            "MAC_DEPLOY_AGENT_GEN_MODEL": "sdxl-turbo",
            "MAC_DEPLOY_AGENT_GEN_PORT": "8189",
            "MAC_DEPLOY_AGENT_GEN_BASE_URL": "http://100.87.229.125:8189/v1",
        },
    )
    assert values["MAC_AGENT_GEN_MODEL"] == "sdxl-turbo"
    assert values["MAC_AGENT_GEN_PORT"] == "8189"
    assert values["MAC_AGENT_GEN_BASE_URL"] == "http://100.87.229.125:8189/v1"
    # absent when not supplied
    bare = build_mac_env({}, deploy_env_config(tmp_path, fleet_name="t2"), environ={})
    assert "MAC_AGENT_GEN_MODEL" not in bare


def test_deploy_passes_local_gen_env_to_agent():
    script = deploy_script_text()
    assert 'add_remote_env MAC_DEPLOY_AGENT_GEN_MODEL' in script
    assert 'add_remote_env MAC_DEPLOY_AGENT_GEN_BASE_URL' in script


def test_deploy_installs_gpu_gen_server_service():
    """Part A: a GPU-gated, systemd, durable mac-gen-server service is installed
    by the deploy (replacing the hand-launched nohup) and gated like Omniverse."""
    script = deploy_script_text()
    # function defined + invoked after the supervisor dispatch
    assert "install_gpu_gen_server() {" in script
    assert "install_gpu_gen_server || true" in script
    # service name derived from the fleet
    assert 'MAC_GEN_SERVICE_NAME="${FLEET_NAME}-gen-server.service"' in script
    # GPU + systemd + gen-model gates (non-fatal skips)
    assert "no NVIDIA GPU on $AGENT; skipping (GPU-only)" in script
    assert 'no MAC_AGENT_GEN_MODEL/AUDIO_MODELS/VIDEO_MODELS set; skipping' in script
    assert 'SUPERVISOR_KIND" != "systemd"' in script
    # CUDA wheel-index knob carried to the remote + used for torch(+vision)
    assert "add_remote_env MAC_DEPLOY_AGENT_GEN_TORCH_INDEX_URL" in script
    assert "pip install torch torchvision --index-url" in script
    # catalog id (sdxl-turbo) is resolved to its HF repo and baked into the
    # wrapper — the gen venv lacks the mac package so it can't resolve it itself
    assert "from mac.local_gen_catalog import get_model" in script
    assert 'export LOCAL_GEN_MODEL="$gen_repo"' in script
    # the unit runs the shipped server in the gen venv on the advertised port
    assert "deploy/local-gen/openai_image_server.py" in script
    assert "ExecStart=$MAC_HOME/bin/${wrapper_name}" in script
    assert "TimeoutStartSec=900" in script


def test_deploy_installs_audio_and_video_gen_units():
    """Part B1b: the deploy installs audio (:8190) + video (:8191) gen servers as
    GPU-gated systemd units alongside image, and carries their model lists."""
    script = deploy_script_text()
    assert 'MAC_GEN_AUDIO_SERVICE_NAME="${FLEET_NAME}-gen-audio-server.service"' in script
    assert 'MAC_GEN_VIDEO_SERVICE_NAME="${FLEET_NAME}-gen-video-server.service"' in script
    assert "_install_gen_unit() {" in script
    assert "deploy/local-gen/audio_server.py" in script
    assert "deploy/local-gen/video_server.py" in script
    assert 'MAC_AGENT_GEN_AUDIO_PORT:-8190' in script
    assert 'MAC_AGENT_GEN_VIDEO_PORT:-8191' in script
    # the audio/video model lists are carried to the remote agent
    assert "add_remote_env MAC_DEPLOY_AGENT_GEN_AUDIO_MODELS" in script
    assert "add_remote_env MAC_DEPLOY_AGENT_GEN_VIDEO_MODELS" in script


def test_deploy_rehydrates_agent_footprint():
    """Part C3: the deploy pulls the agent's installed_packages footprint from
    the hub and re-installs it (pip into the venv, npm into the local prefix),
    idempotent + non-fatal, and puts node_modules/.bin on the agent PATH."""
    script = deploy_script_text()
    assert "install_agent_footprint() {" in script
    assert "install_agent_footprint || true" in script
    # reads the per-agent footprint endpoint + re-installs both managers
    assert "/agents/%s" in script  # GET /agents/<stable_id>
    assert "installed_packages" in script
    assert 'npm", "install", "--prefix"' in script
    # disable knob + npm bin on PATH for self-installed CLI tools
    assert "MAC_AGENT_FOOTPRINT_REINSTALL" in script
    assert "$HOME/.mac/node_modules/.bin" in script


def test_build_mac_env_passes_through_gh_token(tmp_path):
    """Agents get a git credential: MAC_DEPLOY_GH_TOKEN -> GH_TOKEN in mac.env
    (overrides any stale platform-injected token, since the wrapper sources
    mac.env after the pod env)."""
    values = build_mac_env(
        {}, deploy_env_config(tmp_path, fleet_name="gke"),
        environ={"MAC_DEPLOY_GH_TOKEN": "ghp_test123"},
    )
    assert values["GH_TOKEN"] == "ghp_test123"
    bare = build_mac_env({}, deploy_env_config(tmp_path, fleet_name="gke2"), environ={})
    assert "GH_TOKEN" not in bare
    script = deploy_script_text()
    assert "add_remote_env MAC_DEPLOY_GH_TOKEN" not in script
    assert 'add_remote_secret_env MAC_DEPLOY_GH_TOKEN "${MAC_DEPLOY_GH_TOKEN:-}"' in script
    assert "remote_secret_env" in script
    assert "mac-node-install-${agent}-${TS}.env" not in script
    assert "_mac_secret_file" not in script
    assert ". /dev/stdin" in script
    assert "printf '%s\\n' \"${remote_secret_env[@]}\" > \"$local_secret_payload\"" in script
    assert 'stream_file_after_remote_fence "$local_secret_payload"' in script


def test_required_github_credentials_fail_before_worker_drain():
    script = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    function = script.split("configure_github_https_credentials() {", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert 'if [ "$GITHUB_CREDENTIALS_REQUIRED" = "1" ]' in function
    assert "GH_TOKEN absent on a node that requires" in function
    assert '"$gh_bin" auth status --hostname github.com' in function

    pre_mutation = script.split(
        'if [ "$NODE_ACTION" = arm-phase2 ] || [ "$NODE_ACTION" = apply-phase2 ]; then',
        1,
    )[1].split("capture_darwin_launchd_prestate", 1)[0]
    assert pre_mutation.index("validate_typed_prerequisite_bundle") < pre_mutation.index(
        "configure_github_https_credentials"
    )
    assert "configure_github_https_credentials" not in script.split(
        'write_deploy_manifest "pre" "$MANIFEST_PRE"', 1
    )[1]


def test_hub_env_includes_all_option_c_env_vars(tmp_path):
    """Option C end-to-end: the hub env must carry all four vars needed for the
    hub-side review-verification path (MAC_REVIEW_HUB_VERIFY=1 tells the worker
    to defer the contract test; the other three wire up the auto-registered
    hub-reviewer agent that runs the test in the hub's own OpenShell sandbox).
    Spokes must NOT receive any of these vars — they do not run the hub reviewer
    and must not pretend to."""
    hub = build_mac_env(
        {},
        deploy_env_config(tmp_path, agent="rocky", hub_agent="rocky"),
        environ={},
    )
    # All four required Option C env vars must be present on the hub.
    assert hub.get("MAC_REVIEW_HUB_VERIFY") == "1", (
        "MAC_REVIEW_HUB_VERIFY must be '1' on hub nodes (Option C deferred path)"
    )
    assert hub.get("MAC_HUB_REVIEWER_AUTO_REGISTER") == "1", (
        "MAC_HUB_REVIEWER_AUTO_REGISTER must be '1' on hub (auto-registers hub-reviewer agent)"
    )
    assert hub.get("MAC_HUB_REVIEWER_AGENT_NAME") == "hub-reviewer", (
        "MAC_HUB_REVIEWER_AGENT_NAME must be 'hub-reviewer' (stable reviewer agent name)"
    )
    assert hub.get("MAC_HUB_REVIEWER_AGENT_ID") == "agent_hub-reviewer", (
        "MAC_HUB_REVIEWER_AGENT_ID must be 'agent_hub-reviewer' (stable reviewer agent id)"
    )

    spoke = build_mac_env(
        {},
        deploy_env_config(tmp_path, agent="natasha", hub_agent="rocky"),
        environ={},
    )
    # Option C vars must NOT be set on spoke nodes.
    for var in (
        "MAC_REVIEW_HUB_VERIFY",
        "MAC_HUB_REVIEWER_AUTO_REGISTER",
        "MAC_HUB_REVIEWER_AGENT_NAME",
        "MAC_HUB_REVIEWER_AGENT_ID",
    ):
        assert var not in spoke, (
            "%s must not be set on spoke nodes (Option C is hub-only)" % var
        )


def test_fleet_deploy_forwards_repository_ref_reconciler_overrides():
    script = deploy_script_text()

    for name in (
        "MAC_DEPLOY_REPOSITORY_REF_RECONCILER_MODE",
        "MAC_DEPLOY_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS",
        "MAC_DEPLOY_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS",
        "MAC_DEPLOY_REPOSITORY_REF_RECONCILER_GRACE_DAYS",
    ):
        assert 'add_remote_env %s "${%s:-}"' % (name, name) in script


def test_fleet_deploy_forwards_fail_closed_work_package_activation():
    script = deploy_script_text()

    for name in (
        "MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED",
        "MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED",
        "MAC_DEPLOY_WORK_PACKAGE_BUNDLE_DIR",
        "MAC_DEPLOY_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT",
    ):
        assert 'add_remote_env %s "${%s:-}"' % (name, name) in script

    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    assert "prepare_work_package_pipeline_storage" in installer
    assert "work-package bundle directory mode is not 0700" in installer
    assert "work-package pipeline may run only on the control-plane hub" in installer

    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )
    assert 'python3 "$helper" install-archive' in bootstrap
    assert 'ln -sf "$cli" "$MAC_HOME/bin/openshell"' not in bootstrap


def test_fleet_deploy_reconciles_explicit_optional_openshell_disable():
    script = deploy_script_text()
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )

    assert 'add_remote_env MAC_DEPLOY_OPENSHELL "${MAC_DEPLOY_OPENSHELL:-}"' in script
    assert "reconcile_disabled_optional_openshell" in installer
    legacy = installer.split(
        'reload_mac_env\nif [ "$NODE_ACTION" = legacy-one-shot ]; then', 1
    )[1].split("\nelse\n  log \"typed phase 2 consumed infrastructure receipts", 1)[0]
    assert legacy.index("reconcile_disabled_optional_openshell") < legacy.index(
        "prepare_work_package_pipeline_storage"
    )
    for owned_state in (
        "openshell-gw",
        "openshell-gateway.service",
        "/etc/supervisor/conf.d/openshell-gateway.conf",
        "mac-openshell-firewall.service",
        "MAC_OPENSH_GW",
        '"$openshell_dir/runtime-image-ref"',
        '"$openshell_dir/runtime-input-sha256"',
        '"$openshell_dir/runtime-image-build-revision"',
        '"$openshell_dir/image-source-sha"',
    ):
        assert owned_state in installer
    disable_function = installer.split(
        "reconcile_disabled_optional_openshell() {", 1
    )[1].split("\n}\n\nprepare_work_package_pipeline_storage", 1)[0]
    darwin_block = disable_function.split('if [ "$OS_KIND" = "darwin" ]; then', 1)[
        1
    ]
    assert 'mac.owner" }}:{{ index .Config.Labels "mac.kind' in darwin_block
    assert 'log "leaving non-MAC Docker container named openshell-gw untouched"' in darwin_block
    assert '"$cli" gateway list --output json' in disable_function
    assert 'item.get("endpoint") == "http://127.0.0.1:17670"' in disable_function
    assert '"$cli" gateway remove openshell' in disable_function
    assert "gateway remove --all" not in disable_function
    assert 'pgrep -f "$gateway_process_pattern"' in disable_function
    assert "re.escape(sys.argv[1])" in disable_function
    assert 'parts[0]="-D"' in installer


@pytest.mark.parametrize(
    ("requested", "required", "expected_disable"),
    [
        (" false ", " off ", True),
        (" NO ", "", True),
        (" false ", " TRUE ", False),
        ("", "false", False),
        ("1", "false", False),
    ],
)
def test_optional_openshell_disable_guard_normalizes_boolean_tokens(
    requested, required, expected_disable
):
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    start = installer.index("openshell_disable_requested() {")
    end = installer.index("\n}\n\nremove_openshell_firewall_chain", start) + len("\n}\n")
    helper = installer[start:end]
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            helper + "\nopenshell_disable_requested\n",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if key != "MAC_OPENSHELL_REQUIRED"
            },
            "MAC_DEPLOY_OPENSHELL": requested,
            "MAC_DEPLOY_OPENSHELL_REQUIRED": required,
        },
    )

    assert (result.returncode == 0) is expected_disable, result.stderr


def test_optional_openshell_disable_leaves_unowned_darwin_container(tmp_path):
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    start = installer.index("openshell_disable_requested() {")
    end = installer.index("\n}\n\nprepare_work_package_pipeline_storage", start) + len("\n}\n")
    helpers = installer[start:end]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "docker-calls"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CALLS"
case "$1" in
  info) exit 0 ;;
  inspect)
    if [ "$2" = "--format" ]; then
      printf '%s\\n' 'someone-else:unmanaged'
    fi
    exit 0
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    mac_home = tmp_path / "mac-home"
    mac_home.mkdir()
    snippet = f"""
set -euo pipefail
MAC_HOME={shlex.quote(str(mac_home))}
OS_KIND=darwin
PY={shlex.quote(sys.executable)}
log() {{ printf '%s\\n' "$*"; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
{helpers}
reconcile_disabled_optional_openshell
"""

    result = subprocess.run(
        ["/bin/bash", "-c", snippet],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "MAC_DEPLOY_OPENSHELL": " false ",
            "MAC_DEPLOY_OPENSHELL_REQUIRED": " no ",
            "DOCKER_CALLS": str(calls),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "leaving non-MAC Docker container named openshell-gw untouched" in result.stdout
    assert "rm -f openshell-gw" not in calls.read_text(encoding="utf-8")


def test_optional_openshell_disable_is_idempotent_without_managed_state(tmp_path):
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    start = installer.index("openshell_disable_requested() {")
    end = installer.index("\n}\n\nprepare_work_package_pipeline_storage", start) + len("\n}\n")
    helpers = installer[start:end]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    docker.chmod(0o755)
    mac_home = tmp_path / "mac-home"
    mac_home.mkdir()
    snippet = f"""
set -euo pipefail
MAC_HOME={shlex.quote(str(mac_home))}
OS_KIND=darwin
PY={shlex.quote(sys.executable)}
log() {{ :; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
{helpers}
reconcile_disabled_optional_openshell
reconcile_disabled_optional_openshell
"""

    result = subprocess.run(
        ["/bin/bash", "-c", snippet],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "MAC_DEPLOY_OPENSHELL": " OFF ",
            "MAC_DEPLOY_OPENSHELL_REQUIRED": "false",
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        },
    )

    assert result.returncode == 0, result.stderr


def test_optional_openshell_disable_removes_labeled_darwin_gateway(tmp_path):
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    start = installer.index("openshell_disable_requested() {")
    end = installer.index("\n}\n\nprepare_work_package_pipeline_storage", start) + len("\n}\n")
    helpers = installer[start:end]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "docker-calls"
    removed = tmp_path / "docker-removed"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CALLS"
case "$1" in
  info) exit 0 ;;
  inspect)
    [ ! -e "$DOCKER_REMOVED" ] || exit 1
    if [ "$2" = "--format" ]; then
      printf '%s\\n' 'mac:openshell-gateway'
    else
      printf '%s\\n' '[]'
    fi
    exit 0
    ;;
  rm)
    : > "$DOCKER_REMOVED"
    exit 0
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    mac_home = tmp_path / "mac-home"
    mac_home.mkdir()
    snippet = f"""
set -euo pipefail
MAC_HOME={shlex.quote(str(mac_home))}
OS_KIND=darwin
PY={shlex.quote(sys.executable)}
log() {{ :; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
{helpers}
reconcile_disabled_optional_openshell
"""

    result = subprocess.run(
        ["/bin/bash", "-c", snippet],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "MAC_DEPLOY_OPENSHELL": " no ",
            "MAC_DEPLOY_OPENSHELL_REQUIRED": "0",
            "DOCKER_CALLS": str(calls),
            "DOCKER_REMOVED": str(removed),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert removed.exists()
    assert "rm -f openshell-gw" in calls.read_text(encoding="utf-8")


def test_optional_openshell_disable_preserves_marker_when_owned_gateway_rm_fails(
    tmp_path,
):
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    start = installer.index("openshell_disable_requested() {")
    end = installer.index("\n}\n\nprepare_work_package_pipeline_storage", start) + len("\n}\n")
    helpers = installer[start:end]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
case "$1" in
  info) exit 0 ;;
  inspect)
    if [ "$2" = "--format" ]; then
      printf '%s\\n' 'mac:openshell-gateway'
    else
      printf '%s\\n' '[]'
    fi
    exit 0
    ;;
  rm) exit 42 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    mac_home = tmp_path / "mac-home"
    marker = mac_home / "openshell" / "gateway.toml"
    marker.parent.mkdir(parents=True)
    marker.write_text("managed\n", encoding="utf-8")
    snippet = f"""
set -euo pipefail
MAC_HOME={shlex.quote(str(mac_home))}
OS_KIND=darwin
PY={shlex.quote(sys.executable)}
log() {{ :; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
{helpers}
reconcile_disabled_optional_openshell
"""

    result = subprocess.run(
        ["/bin/bash", "-c", snippet],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "MAC_DEPLOY_OPENSHELL": "false",
            "MAC_DEPLOY_OPENSHELL_REQUIRED": "false",
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        },
    )

    assert result.returncode != 0
    assert marker.exists()


def test_owned_legacy_openshell_firewall_rule_cleanup_executes_exact_delete(tmp_path):
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    start = installer.index("remove_openshell_firewall_chain() {")
    end = installer.index("\n}\n\nopenshell_firewall_state_present", start) + len("\n}\n")
    helper = installer[start:end]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "iptables-calls"
    state = tmp_path / "legacy-rule-present"
    state.write_text("present\n", encoding="utf-8")
    sudo = fake_bin / "sudo"
    sudo.write_text(
        """#!/bin/sh
[ "$1" = "-n" ] && shift
exec "$@"
""",
        encoding="utf-8",
    )
    sudo.chmod(0o755)
    iptables_script = """#!/bin/sh
printf '%s\\n' "$*" >> "$IPTABLES_CALLS"
if [ "$1" = "-C" ]; then
  exit 1
fi
if [ "$1" = "-S" ] && [ "$#" = 1 ]; then
  if [ -e "$IPTABLES_STATE" ]; then
    printf '%s\\n' '-A INPUT -i eth0 -p tcp --dport 17670 -j DROP'
  fi
  printf '%s\\n' '-A INPUT -i eth0 -p tcp --dport 9999 -j DROP'
  exit 0
fi
if [ "$1" = "-D" ] && [ "$2" = "INPUT" ] \
    && [ "$7" = "--dport" ] && [ "$8" = "17670" ]; then
  rm -f "$IPTABLES_STATE"
  exit 0
fi
exit 1
"""
    # Keep this test hermetic on Linux CI hosts where a real ip6tables binary
    # may be installed.  The production helper deliberately inspects both
    # families when present, so both commands must belong to the fake host.
    for name in ("iptables", "ip6tables"):
        path = fake_bin / name
        path.write_text(iptables_script, encoding="utf-8")
        path.chmod(0o755)
    snippet = f"""
set -euo pipefail
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
{helper}
remove_openshell_firewall_chain
"""

    result = subprocess.run(
        ["/bin/bash", "-c", snippet],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "IPTABLES_CALLS": str(calls),
            "IPTABLES_STATE": str(state),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert not state.exists()
    recorded = calls.read_text(encoding="utf-8")
    assert "-D INPUT -i eth0 -p tcp --dport 17670 -j DROP" in recorded
    assert "-D INPUT -i eth0 -p tcp --dport 9999 -j DROP" not in recorded


def _run_linux_optional_openshell_disable(
    tmp_path,
    *,
    listener=False,
    historical_systemd_gateway=False,
    firewall_inspection_fails=False,
    managed_firewall=True,
):
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    start = installer.index("openshell_disable_requested() {")
    end = installer.index(
        "\n}\n\nprepare_work_package_pipeline_storage", start
    ) + len("\n}\n")
    helpers = installer[start:end]

    test_root = tmp_path / "root"
    systemd_runtime = test_root / "run/systemd/system"
    systemd_runtime.mkdir(parents=True)
    systemd_firewall = test_root / "etc/systemd/system/mac-openshell-firewall.service"
    supervisor_gateway = test_root / "etc/supervisor/conf.d/openshell-gateway.conf"
    supervisor_firewall = (
        test_root / "etc/supervisor/conf.d/mac-openshell-firewall.conf"
    )
    firewall_script = test_root / "usr/local/sbin/mac-openshell-firewall.sh"
    for original, replacement in (
        (
            'systemd_firewall="/etc/systemd/system/mac-openshell-firewall.service"',
            "systemd_firewall=%s" % shlex.quote(str(systemd_firewall)),
        ),
        (
            'supervisor_gateway="/etc/supervisor/conf.d/openshell-gateway.conf"',
            "supervisor_gateway=%s" % shlex.quote(str(supervisor_gateway)),
        ),
        (
            'supervisor_firewall="/etc/supervisor/conf.d/mac-openshell-firewall.conf"',
            "supervisor_firewall=%s" % shlex.quote(str(supervisor_firewall)),
        ),
        (
            'firewall_script="/usr/local/sbin/mac-openshell-firewall.sh"',
            "firewall_script=%s" % shlex.quote(str(firewall_script)),
        ),
        (
            "/run/systemd/system",
            shlex.quote(str(systemd_runtime)),
        ),
    ):
        helpers = helpers.replace(original, replacement)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    supervisor_calls = tmp_path / "supervisor-calls"
    iptables_calls = tmp_path / "iptables-calls"
    firewall_state = tmp_path / "legacy-rule-present"
    if managed_firewall:
        firewall_state.write_text("present\n", encoding="utf-8")

    iptables_script = """#!/bin/sh
printf '%s\n' "$*" >> "$IPTABLES_CALLS"
if [ "$1" = "-S" ]; then
  [ "$FIREWALL_INSPECTION_FAILS" = 0 ] || exit 73
  if [ -e "$IPTABLES_STATE" ]; then
    printf '%s\n' '-A INPUT -i eth0 -p tcp --dport 17670 -j DROP'
  fi
  exit 0
fi
if [ "$1" = "-C" ]; then
  exit 1
fi
if [ "$1" = "-D" ] && [ "$2" = "INPUT" ]; then
  rm -f "$IPTABLES_STATE"
  exit 0
fi
exit 1
"""
    scripts = {
        "sudo": """#!/bin/sh
[ "$1" = "-n" ] && shift
exec "$@"
""",
        "supervisorctl": """#!/bin/sh
printf '%s\n' "$*" >> "$SUPERVISOR_CALLS"
exit 91
""",
        "pgrep": "#!/bin/sh\nexit 1\n",
        "pkill": "#!/bin/sh\nexit 0\n",
        # GitHub's Linux runners have a live /run/systemd/system and a real
        # systemctl.  Exercise the systemd branch without talking to the host
        # manager: mutation/reload succeeds and the post-stop probe is inactive.
        "systemctl": """#!/bin/sh
case " $* " in
  *" is-active "*) exit 3 ;;
esac
exit 0
""",
        "ss": (
            "#!/bin/sh\nprintf '%s\\n' "
            + shlex.quote(
                "LISTEN 0 4096 0.0.0.0:17670 0.0.0.0:*" if listener else ""
            )
            + "\n"
        ),
        "iptables": iptables_script,
        "ip6tables": iptables_script,
    }
    for name, body in scripts.items():
        path = fake_bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    home = tmp_path / "home"
    mac_home = home / ".mac"
    openshell_dir = mac_home / "openshell"
    openshell_dir.mkdir(parents=True)
    marker = openshell_dir / "gateway.toml"
    marker.write_text("managed\n", encoding="utf-8")
    systemd_gateway = home / ".config/systemd/user/openshell-gateway.service"
    if historical_systemd_gateway:
        systemd_gateway.parent.mkdir(parents=True)
        systemd_gateway.write_text(
            "[Service]\n"
            "ExecStart=%h/.local/bin/openshell-gateway "
            "--config %h/.mac/openshell/gateway.toml\n",
            encoding="utf-8",
        )
    if managed_firewall:
        systemd_firewall.parent.mkdir(parents=True)
        systemd_firewall.write_text(
            "[Service]\nExecStart=/usr/local/sbin/mac-openshell-firewall.sh\n",
            encoding="utf-8",
        )
        firewall_script.parent.mkdir(parents=True)
        firewall_script.write_text(
            "#!/bin/sh\niptables -I INPUT -i eth0 -p tcp --dport 17670 -j DROP\n",
            encoding="utf-8",
        )

    snippet = f"""
set -euo pipefail
MAC_HOME={shlex.quote(str(mac_home))}
OS_KIND=linux
PY={shlex.quote(sys.executable)}
log() {{ :; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
run_user_systemctl() {{ systemctl --user "$@"; }}
run_systemctl() {{ systemctl "$@"; }}
{helpers}
reconcile_disabled_optional_openshell
"""
    result = subprocess.run(
        ["/bin/bash", "-c", snippet],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "MAC_DEPLOY_OPENSHELL": " false ",
            "MAC_DEPLOY_OPENSHELL_REQUIRED": "false",
            "FIREWALL_INSPECTION_FAILS": (
                "1" if firewall_inspection_fails else "0"
            ),
            "IPTABLES_CALLS": str(iptables_calls),
            "IPTABLES_STATE": str(firewall_state),
            "SUPERVISOR_CALLS": str(supervisor_calls),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
    )
    return {
        "result": result,
        "firewall_script": firewall_script,
        "firewall_state": firewall_state,
        "iptables_calls": iptables_calls,
        "marker": marker,
        "supervisor_calls": supervisor_calls,
        "systemd_firewall": systemd_firewall,
        "systemd_gateway": systemd_gateway,
    }


def test_systemd_owned_optional_disable_does_not_touch_inactive_supervisor(tmp_path):
    evidence = _run_linux_optional_openshell_disable(
        tmp_path, historical_systemd_gateway=True
    )

    result = evidence["result"]
    assert result.returncode == 0, result.stderr
    assert not evidence["supervisor_calls"].exists()
    assert not evidence["systemd_gateway"].exists()
    assert not evidence["systemd_firewall"].exists()
    assert not evidence["firewall_script"].exists()
    assert not evidence["firewall_state"].exists()
    assert not evidence["marker"].exists()


def test_optional_disable_preserves_firewall_when_listener_remains(tmp_path):
    evidence = _run_linux_optional_openshell_disable(tmp_path, listener=True)

    result = evidence["result"]
    assert result.returncode != 0
    assert "while TCP/17670 is listening" in result.stderr
    assert evidence["systemd_firewall"].exists()
    assert evidence["firewall_script"].exists()
    assert evidence["firewall_state"].exists()
    assert evidence["marker"].exists()
    assert "-D INPUT" not in evidence["iptables_calls"].read_text(encoding="utf-8")


def test_managed_optional_disable_rejects_listener_without_owned_firewall(tmp_path):
    evidence = _run_linux_optional_openshell_disable(
        tmp_path, listener=True, managed_firewall=False
    )

    result = evidence["result"]
    assert result.returncode != 0
    assert "while TCP/17670 is listening" in result.stderr
    assert evidence["marker"].exists()
    assert "-D INPUT" not in evidence["iptables_calls"].read_text(encoding="utf-8")


def test_optional_disable_preserves_firewall_when_inspection_fails(tmp_path):
    evidence = _run_linux_optional_openshell_disable(
        tmp_path, firewall_inspection_fails=True
    )

    result = evidence["result"]
    assert result.returncode != 0
    assert "could not inspect existing OpenShell firewall state" in result.stderr
    assert evidence["systemd_firewall"].exists()
    assert evidence["firewall_script"].exists()
    assert evidence["firewall_state"].exists()
    assert evidence["marker"].exists()
    assert "-D INPUT" not in evidence["iptables_calls"].read_text(encoding="utf-8")


def test_required_worker_forces_openshell_despite_explicit_zero():
    script = deploy_script_text()
    required_block = script.split(
        'case "$openshell_required_normalized" in', 1
    )[1].split("  esac\n  if [ \"$openshell_enabled\"", 1)[0]

    assert "openshell_enabled=1" in required_block
    assert "openshell_disable_requested=0" in required_block
    assert "--enable" in required_block
    assert "--fail-closed" in required_block

    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    disable_guard = installer.split("openshell_disable_requested() {", 1)[1].split(
        "\n}", 1
    )[0]
    assert "MAC_DEPLOY_OPENSHELL_REQUIRED" in disable_guard
    assert "1|true|yes|on) return 1" in disable_guard


def test_fleet_deploy_forwards_execution_cohort_only_to_hub_and_hides_seed():
    script = deploy_script_text()
    pilot_block = script.split(
        "# The randomized execution pilot belongs to the control-plane hub only.", 1
    )[1].split('  local img_key="', 1)[0]

    assert 'if [ "$agent" = "$shared_services_manager" ]; then' in pilot_block
    assert (
        'add_remote_env MAC_DEPLOY_EXECUTION_COHORT_REVISION '
        '"${MAC_DEPLOY_EXECUTION_COHORT_REVISION:-1}"'
    ) in pilot_block
    assert (
        'add_remote_env MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT '
        '"${MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT:-50}"'
    ) in pilot_block
    assert (
        'add_remote_secret_env MAC_DEPLOY_EXECUTION_COHORT_SEED '
        '"${MAC_DEPLOY_EXECUTION_COHORT_SEED:-}"'
    ) in pilot_block
    assert "add_remote_env MAC_DEPLOY_EXECUTION_COHORT_SEED" not in script
    precedence = script.split("local -a _PRECEDENCE_VARS=(", 1)[1].split(")", 1)[0]
    for name in (
        "MAC_DEPLOY_EXECUTION_COHORT_REVISION",
        "MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT",
        "MAC_DEPLOY_EXECUTION_COHORT_SEED",
    ):
        assert name in precedence


def test_fleet_deploy_pins_one_openshell_runtime_digest_across_nodes():
    script = deploy_script_text()
    bootstrap = (ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh").read_text(
        encoding="utf-8"
    )

    assert "MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE" in script
    assert "mac-openshell-runtime@sha256:" in script
    assert (
        'add_remote_env MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE '
        '"${MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE:-}"'
    ) in script
    assert (
        'add_remote_env MAC_DEPLOY_OPENSHELL_RUNTIME_INPUT_SHA256 '
        '"${MAC_DEPLOY_OPENSHELL_RUNTIME_INPUT_SHA256:-}"'
    ) in script
    assert 'OSH_RUNTIME_IMAGE_REF="$OPENSHELL_RUNTIME_IMAGE"' in script
    assert 'OSH_RUNTIME_INPUT_SHA256="$OPENSHELL_RUNTIME_INPUT_SHA256"' in script
    assert 'MAC_DEPLOY_OPENSHELL_EFFECTIVE_ARGS "$effective_openshell_args"' in script
    assert "OSH_RUNTIME_IMAGE_REF=$(shell_quote" not in script
    assert "OSH_RUNTIME_IMAGE_REF" in bootstrap
    assert '"$OSH_DOCKER_BIN" pull "$OSH_RUNTIME_IMAGE_REF"' in bootstrap
    assert 'image_source_sha="$(resolve_deployed_source_revision)" || return 1' in bootstrap
    assert "DEPLOYED_SOURCE_REVISION_FILE" in bootstrap
    assert "runtime image frozen-input identity does not match reviewed publication" in bootstrap
    assert '"io.mac.frozen-inputs.sha256"' in bootstrap
    assert 'runtime_input_file="$OSH_DIR/runtime-input-sha256"' in bootstrap
    assert 'runtime_build_file="$OSH_DIR/runtime-image-build-revision"' in bootstrap
    assert '"$OSH_DOCKER_BIN" tag "$OSH_RUNTIME_IMAGE_REF" "$OSH_IMAGE_TAG"' in bootstrap
    assert 'runtime_ref_file="$OSH_DIR/runtime-image-ref"' in bootstrap
    assert 'printf \'%s\\n\' "$OSH_RUNTIME_IMAGE_REF" > "$runtime_ref_tmp"' in bootstrap
    assert "MAC_DEPLOY_ALLOW_LOCAL_OPENSHELL_IMAGE_BUILD" in script
    assert "production OpenShell deployment requires MAC_DEPLOY_OPENSHELL_RUNTIME_IMAGE" in script
    assert "--skip-image is incompatible with a digest-managed OpenShell deployment" in script


def test_node_openshell_bootstrap_uses_exact_runtime_and_reviewed_argument_vector(
    tmp_path,
):
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(
        encoding="utf-8"
    )
    start = installer.index("bootstrap_enabled_openshell() {")
    end = installer.index(
        "\n}\n\nverify_managed_openshell_runtime", start
    ) + len("\n}\n")
    helper = installer[start:end]

    source = tmp_path / "source"
    bootstrap = source / "deploy" / "openshell" / "bootstrap-openshell.sh"
    bootstrap.parent.mkdir(parents=True)
    calls = tmp_path / "bootstrap-calls"
    bootstrap.write_text(
        "#!/bin/bash\n"
        "{\n"
        "  printf 'runtime=%s\\n' \"${OSH_RUNTIME_IMAGE_REF:-}\"\n"
        "  printf 'input=%s\\n' \"${OSH_RUNTIME_INPUT_SHA256:-}\"\n"
        "  printf 'expected=%s\\n' \"${MAC_OPENSH_EXPECTED_OPENCLAW_SANDBOX:-}\"\n"
        "  printf 'argc=%s\\n' \"$#\"\n"
        "  for arg in \"$@\"; do printf 'arg=%s\\n' \"$arg\"; done\n"
        "} >> \"$MAC_TEST_BOOTSTRAP_CALLS\"\n"
        "exit \"${MAC_TEST_BOOTSTRAP_RC:-0}\"\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o755)
    digest = "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64
    input_sha256 = "sha256:" + "b" * 64
    snippet = (
        "set -euo pipefail\n"
        "truthy() { case \"$(printf '%s' \"${1:-}\" | tr '[:upper:]' '[:lower:]')\" "
        "in 1|true|yes|on) return 0;; *) return 1;; esac; }\n"
        "die() { printf '%s\\n' \"$*\" >&2; return 1; }\n"
        "log() { :; }\n"
        + helper
        + "\nbootstrap_enabled_openshell\n"
    )
    base_env = {
        **os.environ,
        "SRC_DIR": str(source),
        "PY": sys.executable,
        "AGENT": "bullwinkle",
        "OPENSHELL_LOCAL_IMAGE_BUILD": "0",
        "OPENSHELL_RUNTIME_IMAGE": digest,
        "OPENSHELL_RUNTIME_INPUT_SHA256": input_sha256,
        "MAC_TEST_BOOTSTRAP_CALLS": str(calls),
    }

    # Empty reviewed args must invoke the bootstrap with zero argv entries. This
    # specifically protects macOS Bash 3.2, where an empty array expansion under
    # set -u aborts even though the array was declared.
    empty = subprocess.run(
        ["/bin/bash", "-c", snippet],
        env={
            **base_env,
            "OPENSHELL_DEPLOY_ENABLED": "1",
            "OPENSHELL_EFFECTIVE_ARGS": "",
            "HERMES_GATEWAY_IMPL": "hermes",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert empty.returncode == 0, empty.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        f"runtime={digest}",
        f"input={input_sha256}",
        "expected=",
        "argc=0",
    ]

    calls.unlink()
    required = subprocess.run(
        ["/bin/bash", "-c", snippet],
        env={
            **base_env,
            "MAC_DEPLOY_OPENSHELL": "0",
            "MAC_DEPLOY_OPENSHELL_REQUIRED": "true",
            "OPENSHELL_DEPLOY_ENABLED": "0",
            "OPENSHELL_EFFECTIVE_ARGS": "",
            "HERMES_GATEWAY_IMPL": "openclaw",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert required.returncode == 0, required.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        f"runtime={digest}",
        f"input={input_sha256}",
        "expected=mac-openclaw-bullwinkle",
        "argc=2",
        "arg=--enable",
        "arg=--fail-closed",
    ]

    calls.unlink()
    skip_image = subprocess.run(
        ["/bin/bash", "-c", snippet],
        env={
            **base_env,
            "OPENSHELL_DEPLOY_ENABLED": "1",
            "OPENSHELL_EFFECTIVE_ARGS": "--skip-image",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert skip_image.returncode != 0
    assert "--skip-image is incompatible" in skip_image.stderr
    assert not calls.exists()

    failed_bootstrap = subprocess.run(
        ["/bin/bash", "-c", snippet],
        env={
            **base_env,
            "OPENSHELL_DEPLOY_ENABLED": "1",
            "OPENSHELL_EFFECTIVE_ARGS": "--enable",
            "MAC_TEST_BOOTSTRAP_RC": "42",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed_bootstrap.returncode == 42


def _startup_self_test_source() -> str:
    """Extract the embedded mac-agent-startup-self-test Python (the inner PY heredoc)."""
    script = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    match = re.search(
        r"exec \"\$selftest_python\" - <<'PY'\n(?P<source>.*?)\nPY\n",
        script,
        re.DOTALL,
    )
    assert match, "self-test PY heredoc not found in fleet-node-install.sh"
    return match.group(1)


def _run_startup_self_test(tmp_path, monkeypatch, *, install_gateway):
    """Exec the startup self-test in-process with reachable shared services stubbed.

    ``install_gateway`` controls whether the OpenClaw gateway artifacts
    (service-advertisement.json + openclaw-agent binary) exist on disk; both
    scenarios advertise MAC_CHAT_GATEWAY_IMPL=openclaw. Returns (exit_code, report).
    """
    import urllib.request
    import subprocess as _subprocess

    home = tmp_path / "home"
    mac_home = home / ".mac"
    (mac_home / "openclaw" / "managed").mkdir(parents=True, exist_ok=True)
    (mac_home / "bin").mkdir(parents=True, exist_ok=True)
    (mac_home / "logs").mkdir(parents=True, exist_ok=True)
    report_path = mac_home / "logs" / "mac-agent-startup-self-test.json"

    if install_gateway:
        # A genuinely gateway-serving node: artifacts present but broken (the
        # advertisement is missing its runtime/ownership proof), so it must fail hard.
        (mac_home / "openclaw" / "service-advertisement.json").write_text(
            json.dumps({"openclaw_runtime": {}, "gateway_ownership": {}}), encoding="utf-8"
        )
        agent_bin = mac_home / "bin" / "openclaw-agent"
        agent_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        agent_bin.chmod(0o755)

    env = {
        "MAC_CHAT_GATEWAY_IMPL": "openclaw",
        "MAC_WORKER_AGENT_NAME": "worker1",
        "MAC_AGENT_ID": "agent_worker1",
        "MAC_HERMES_INSTANCE_ID": "hermes-1",
        "MAC_HERMES_PERSONA_ID": "persona-1",
        "MAC_FLEET_TENANT_ID": "tenant-1",
        "MAC_REQUIRE_QDRANT_MEMORY": "1",
        "QDRANT_URL": "http://qdrant.local:6333",
        "MAC_REQUIRE_FIRECRAWL": "1",
        "FIRECRAWL_API_URL": "http://firecrawl.local:3002",
        "MAC_AGENT_STARTUP_SELF_TEST_REPORT": str(report_path),
    }
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args, **kwargs):
            return b"{}"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    # No openclaw-agent invocation should ever run for a gateway-less worker; for
    # the installed case the runtime advertisement already fails before the binary.
    monkeypatch.setattr(
        _subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("openclaw-agent must not run")),
    )

    namespace = {
        "__name__": "__mac_selftest__",
        "os": __import__("os"),
    }
    saved_environ = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    exit_code = 0
    try:
        exec(compile(_startup_self_test_source(), "<selftest>", "exec"), namespace)
    except SystemExit as exc:
        exit_code = exc.code or 0
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    return exit_code, report


def test_gatewayless_worker_does_not_hard_crash_on_missing_openclaw_gateway(tmp_path, monkeypatch):
    # Regression for crash_b24c6ac41f854074b6ea49cabbc24090: a pure worker with
    # MAC_CHAT_GATEWAY_IMPL=openclaw but no installed gateway (missing
    # service-advertisement.json + openclaw-agent) must degrade, not exit 1.
    exit_code, report = _run_startup_self_test(tmp_path, monkeypatch, install_gateway=False)
    assert exit_code == 0, report["blocking_problems"]
    assert report["status"] == "degraded"
    assert report["blocking_problems"] == []
    assert report["openclaw_gateway"]["impl_advertised"] is True
    assert report["openclaw_gateway"]["installed"] is False
    assert report["openclaw_gateway"]["serves_gateway"] is False
    assert any(p.startswith("OpenClaw") for p in report["non_blocking_problems"])


def test_gateway_serving_node_still_fails_hard_when_gateway_broken(tmp_path, monkeypatch):
    # A node that actually installed the gateway artifacts but whose advertisement
    # is broken must still fail hard (exit 1) — the decoupling relief is only for
    # gateway-less workers.
    exit_code, report = _run_startup_self_test(tmp_path, monkeypatch, install_gateway=True)
    assert exit_code == 1
    assert report["status"] == "failed"
    assert report["openclaw_gateway"]["installed"] is True
    assert report["openclaw_gateway"]["serves_gateway"] is True
    assert any(p.startswith("OpenClaw") for p in report["blocking_problems"])
