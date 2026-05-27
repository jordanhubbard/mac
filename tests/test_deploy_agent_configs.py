from pathlib import Path
import json
import subprocess
import sys

from mac.fleet_deploy import cleanup_path_strings, parse_ssh_target
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


def test_sample_fleet_config_is_generic_and_externalized():
    cfg = load_sample_fleet_config()
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    rendered = "\n".join(
        [
            (ROOT / "deploy" / "fleet" / "config.yaml").read_text(encoding="utf-8"),
            (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8"),
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
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    cfg = load_sample_fleet_config()
    expected = "ops,python,hermes,review,web_search,web_extract,web_crawl,firecrawl"

    assert f'text_field(worker.get("capabilities") or "{expected}")' in script
    assert f'WORKER_CAPABILITIES="${{MAC_DEPLOY_WORKER_CAPABILITIES:-{expected}}}"' in script
    assert f'configured_worker_capabilities = sys.argv[13].strip() or "{expected}"' in script
    assert f'capabilities="${{MAC_WORKER_CAPABILITIES:-{expected}}}"' in script
    assert cfg["defaults"]["worker"]["capabilities"] == [
        "ops",
        "python",
        "hermes",
        "review",
        "web_search",
        "web_extract",
        "web_crawl",
        "firecrawl",
    ]


def test_fleet_deploy_persists_or_recovers_worker_attestation_key():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert '--attestation-key-env "$HOME/.mac/mac.env"' in script
    assert "--rotate-missing-attestation-key" in script
    assert "--rotate-invalid-attestation-key" in script
    assert "evidence_type=review_verdict" in script


def test_fleet_deploy_bootstraps_hub_fleet_record():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert 'values["MAC_FLEET_NAME"] = os.environ.get("FLEET_NAME") or "mac"' in script
    assert "if agent == shared_services_manager:" in script
    assert "cp.create_fleet(" in script
    assert 'fleet_id=stable_id("fleet", fleet)' in script
    assert 'description="Auto-registered deployment fleet"' in script


def test_fleet_deploy_drain_agent_lookup_does_not_pipe_json_into_python_stdin():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    agent_id_for_drain = script.split("agent_id_for_drain() {", 1)[1].split(
        "wait_for_agent_active_leases() {", 1
    )[0]

    assert 'response="$(mac_api_json GET "/agents")"' in agent_id_for_drain
    assert "json.loads(sys.argv[2])" in agent_id_for_drain
    assert 'mac_api_json GET "/agents" |' not in agent_id_for_drain


def test_fleet_deploy_bootstraps_beads_cli_for_bridge():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "BEADS_REPO_URL=\"${MAC_DEPLOY_BEADS_REPO_URL:-https://github.com/gastownhall/beads.git}\"" in script
    assert "install_beads_cli()" in script
    assert '"$HOME/.local/bin/bd"' in script
    assert '"$HOME/bin/bd"' in script
    assert "bootstrap_beads_repositories()" in script
    assert "restore_beads_tracked_exports()" in script
    assert 'values.setdefault("MAC_BEADS_RESTORE_TRACKED_EXPORTS", "1")' in script
    assert 'values.setdefault("MAC_BEADS_BRIDGE_ROOT", str(mac_home / "beads-checkouts"))' in script
    assert 'bootstrap --yes' in script
    assert 'dolt pull' in script
    assert 'chmod 700 "$repo_path/.beads"' in script
    assert 'git -C "$repo_path" config beads.role maintainer' in script
    source_install_block = script.split('mv "$SRC_DIR.new" "$SRC_DIR"', 1)[1].split(
        'log "creating/updating mac environment file"', 1
    )[0]
    assert "install_beads_cli" in source_install_block
    env_source_block = script.split('. "$ENV_FILE"', 1)[1].split(
        'log "installing mac Python package"', 1
    )[0]
    assert "bootstrap_beads_repositories" in env_source_block
    assert "restore_beads_tracked_exports" in env_source_block
    assert 'values["MAC_BEADS_CLI"] = str(mac_home / "bin" / "bd")' in script
    services_text = (ROOT / "src" / "mac" / "services.py").read_text(encoding="utf-8")
    assert "BeadsBridgeService(_beads_cli, runner=_run_beads_command)" in services_text
    assert 'self.beads_bridge.run(["ready", "--json"]' in services_text


def test_fleet_deploy_installs_github_cli_for_workers():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "install_github_cli()" in script
    assert 'install_github_cli' in script.split('mv "$SRC_DIR.new" "$SRC_DIR"', 1)[1].split(
        'log "creating/updating mac environment file"', 1
    )[0]
    assert 'brew install gh' in script
    assert 'sudo apt-get install -y gh' in script
    assert 'https://cli.github.com/packages' in script
    assert 'export PATH="$HOME/.mac/bin:$HOME/.mac/venv/bin:$PATH"' in script


def test_fleet_deploy_does_not_print_worker_token_in_systemd_status():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    agent_service = script.split("install_linux_agent_service() {", 1)[1].split(
        "install_darwin_service() {", 1
    )[0]

    assert 'systemctl show "$MAC_AGENT_SERVICE_NAME"' in agent_service
    assert 'systemctl --no-pager -l status "$MAC_AGENT_SERVICE_NAME"' not in agent_service
    assert "-p ActiveState" in agent_service
    assert "-p MainPID" in agent_service


def test_darwin_service_wrappers_raise_file_descriptor_limit():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
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
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    quench_patch = ROOT / "deploy" / "hermes" / "disable-shutdown-chat-notices.patch"
    runtime_patch = ROOT / "deploy" / "hermes" / "mac-runtime-context-prompt.patch"

    assert "multi-slack-mvp.patch" in script
    assert "mac-runtime-context-prompt.patch" in script
    assert "disable-shutdown-chat-notices.patch" in script
    assert "upstream plus mac-managed patches" in script
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
    assert "mac-hermes-task-executor" in script
    assert "_load_mac_runtime_context" in runtime_patch.read_text(encoding="utf-8")
    assert "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN" in runtime_patch.read_text(encoding="utf-8")
    assert "Shutdown chat notifications disabled by MAC deployment policy." in quench_patch.read_text(
        encoding="utf-8"
    )


def test_fleet_deploy_declares_shared_memory_and_supervision_contract():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    qdrant_installer = (ROOT / "deploy" / "install-qdrant-service.sh").read_text(
        encoding="utf-8"
    )
    firecrawl_installer = (ROOT / "deploy" / "install-firecrawl-gateway.sh").read_text(
        encoding="utf-8"
    )
    tokenhub_installer = (ROOT / "deploy" / "install-tokenhub-service.sh").read_text(
        encoding="utf-8"
    )
    env_example = parse_env(ROOT / "deploy" / "systemd" / "mac.env.example")
    cfg = load_sample_fleet_config()

    assert 'SUPERVISOR_REQUESTED="${MAC_DEPLOY_SUPERVISOR:-auto}"' in script
    assert "detect_supervisor()" in script
    assert "systemd|launchd|supervisord" in script
    assert "install_supervisord_service()" in script
    assert "write_hermes_memory_topology()" in script
    assert "write_hermes_runtime_context()" in script
    assert "register_hermes_runtime_identity()" in script
    assert "ensure_hermes_identity_memory_continuity()" in script
    assert 'values.setdefault("SLACK_ALLOWED_USERS", "*")' in script
    assert 'values.setdefault("SLACK_STRICT_MENTION", "true")' in script
    assert "mac.hermes.runtime_context.v1" in (ROOT / "src" / "mac" / "hermes_runtime.py").read_text(
        encoding="utf-8"
    )
    assert 'values["MAC_HERMES_INSTANCE_ID"] = stable_id("hermes", agent_name)' in script
    assert 'values["MAC_WORKER_HERMES_INSTANCE_ID"] = values["MAC_HERMES_INSTANCE_ID"]' in script
    assert 'common+=(--hermes-instance-id "${MAC_WORKER_HERMES_INSTANCE_ID:-${MAC_HERMES_INSTANCE_ID:-}}")' in script
    assert 'export PATH="$HOME/.mac/bin:$HOME/.mac/venv/bin:$PATH"' in script
    assert "install_or_validate_shared_services" in script
    assert "install_or_validate_tokenhub_service" in script
    assert "mac.hermes.memory_topology.v1" in script
    assert '"long_term_memory": "memories/MEMORY.md"' in script
    assert '"legacy_long_term_memory": "MEMORY.md"' in script
    assert "QDRANT_FLEET_URL" in script
    assert 'QDRANT_REQUIRE="1"' in script
    assert 'configured_qdrant_required = "1"' in script
    assert 'values["MAC_REQUIRE_QDRANT_MEMORY"] = "1"' in script
    assert '"MAC_REQUIRE_QDRANT_MEMORY": "1",' in script
    assert '"mandatory": True,' in script
    assert 'updates["QDRANT_URL"] = None' not in script
    assert "Optional Qdrant shared memory" not in script
    assert "127.0.0.1:16333:127.0.0.1:6333" in script
    assert "qd_port += 10000" in script
    assert '"qdrant_shared_memory": False' in script
    assert 'probe_http(qdrant_url, "/collections", qdrant_headers)' in script
    assert 'values.setdefault("MAC_REVIEW_TICK_HUB_AGENT", shared_services_manager)' in script
    assert "mac-qdrant.service" in qdrant_installer
    assert 'ENV_DEST="/etc/${FLEET_NAME}/qdrant.env"' in qdrant_installer
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
    assert env_example["MAC_WORKER_EXECUTOR"] == "/home/mac/.mac/bin/mac-hermes-task-executor"
    assert env_example["MAC_HERMES_WORKSPACE"] == "/home/mac/.mac/src/mac"
    assert env_example["SLACK_ALLOWED_USERS"] == "*"
    assert env_example["SLACK_STRICT_MENTION"] == "true"
    assert env_example["MAC_PROJECT_CONTRACT_FILE"] == "/home/mac/.mac/src/mac/.mac/project.yaml"
    assert '--workspace "$SRC_DIR"' in script
    assert cfg["defaults"]["firecrawl"]["install"] == "auto"
    assert cfg["defaults"]["firecrawl"]["required"] is True
    assert cfg["defaults"]["firecrawl"]["port"] == 3002
    assert "mac.firecrawl_gateway" in firecrawl_installer
    assert 'ENV_DEST="/etc/${FLEET_NAME}/firecrawl-gateway.env"' in firecrawl_installer
    assert "Firecrawl-compatible web search gateway" in firecrawl_installer
    assert env_example["MAC_REQUIRE_FIRECRAWL"] == "1"
    assert env_example["FIRECRAWL_API_URL"] == "http://hub.example.internal:3002"
    assert "MAC_FIRECRAWL_ALLOW_DEGRADED" not in env_example
    assert cfg["defaults"]["tokenhub"]["install"] == "auto"
    assert cfg["defaults"]["tokenhub"]["required"] is True
    assert cfg["defaults"]["tokenhub"]["port"] == 8090
    assert "provider_secret_keys" in script
    assert "values.pop(key, None)" in script
    assert "install-tokenhub-service.sh" in script
    assert "Before=${FLEET_NAME}.service" in tokenhub_installer
    assert 'TOKENHUB_VAULT_ENABLED="${TOKENHUB_VAULT_ENABLED:-true}"' in tokenhub_installer
    assert 'health_urls+=("http://${TOKENHUB_BIND_ADDR}:${TOKENHUB_PORT}/healthz")' in tokenhub_installer
    assert 'base.startswith("tokenhubctl-")' in tokenhub_installer
    assert 'base.startswith("tokenhub-")' in tokenhub_installer
    assert 'all((bin_dir / name).exists() for name in names)' in tokenhub_installer
    assert "OPENAI_API_KEY" in tokenhub_installer
    assert "MAC_HERMES_GATEWAY_API_KEY" in tokenhub_installer
    assert env_example["MAC_REQUIRE_TOKENHUB"] == "1"
    assert env_example["TOKENHUB_URL"] == "http://hub.example.internal:8090"
    assert env_example["MAC_HERMES_GATEWAY_API_KEY"] == "tokenhub_REPLACE_ME"
    assert env_example["OPENAI_BASE_URL"] == "http://hub.example.internal:8090/v1"


def test_fleet_deploy_configures_firecrawl_for_hermes_and_worker_capabilities():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "firecrawl = merge_dicts" in script
    assert 'os.environ.get("MAC_DEPLOY_FIRECRAWL_URL") or text_field(firecrawl.get("url"))' in script
    assert (
        'os.environ.get("MAC_DEPLOY_FIRECRAWL_INSTALL") '
        'or text_field(firecrawl.get("install") or "auto")'
    ) in script
    assert 'FIRECRAWL_REQUIRE="1"' in script
    assert 'configured_firecrawl_required = "1"' in script
    assert 'values["MAC_REQUIRE_FIRECRAWL"] = "1"' in script
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
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    linux_service = script.split("install_linux_service() {", 1)[1].split(
        "install_supervisord_service() {", 1
    )[0]

    assert "install_mac_control_wrapper" in linux_service
    assert 'export PATH="$HOME/.mac/bin:$HOME/.mac/venv/bin:$PATH"' in script
    assert "ExecStart=$MAC_HOME/bin/mac-service" in linux_service
    assert "ExecStart=$VENV/bin/uvicorn" not in linux_service


def test_fleet_deploy_uses_tokenhub_instead_of_direct_provider_secret_paths():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    startup = (ROOT / "src" / "mac" / "hermes_startup.py").read_text(encoding="utf-8")
    gateway_wrapper = script.split("install_hermes_gateway_wrapper() {", 1)[1].split(
        "install_mac_agent_wrapper() {", 1
    )[0]
    executor_wrapper = script.split('cat > "$executor" <<', 1)[1].split(
        'cat > "$executor_py" <<', 1
    )[0]

    assert (
        "reload_mac_env\n"
        "install_or_validate_tokenhub_service\n"
        "reload_mac_env\n"
        "sync_hermes_tokenhub_client_env\n"
        "reload_mac_env\n"
        "sync_hermes_slack_identity_env\n"
        '[ -x "$MAC_HOME/bin/bd" ] && bootstrap_beads_repositories'
    ) in script
    assert 'unset TOKENHUB_URL' in script
    assert "provider_secret_keys" in script
    assert 'values["TOKENHUB_URL"] = derived_tokenhub_url' in script
    assert 'values["OPENAI_API_KEY"] = configured_tokenhub_api_key' in script
    assert 'values["MAC_HERMES_GATEWAY_API_KEY"] = configured_tokenhub_api_key' in script
    assert 'updates["TOKENHUB_API_KEY"] = tokenhub_key' in script
    assert 'updates["MAC_HERMES_GATEWAY_API_KEY"] = tokenhub_key' in script
    assert 'write_env(target_path, updates)' in script
    assert "sync_tokenhub_credential_pool" in script
    assert 'pool["custom:tokenhub"] = entries' in script
    assert '"source": "mac-tokenhub-sync"' in script
    assert "sync_hermes_tokenhub_runtime_config()" in script
    assert 'model_config["provider"] = "tokenhub"' in script
    assert 'model_config.pop("api_key", None)' in script
    assert '"key_env": "TOKENHUB_API_KEY"' in script
    assert 'tokenhub_provider.pop("api_key", None)' in script
    assert '"chat", "--query", prompt, "--quiet", "--accept-hooks"' in script
    assert "write_fallback_evidence_manifest(task_workspace, task, result, review_context)" in script
    assert '"evidence_type": task_evidence_type(task)' in script
    assert 'runtime_kwargs["api_key"] = mac_gateway_api_key' in startup
    assert 'runtime_kwargs["base_url"] = mac_gateway_base_url.rstrip("/")' in startup
    assert '[ -f "$HOME/.acc/.env" ]' not in gateway_wrapper
    assert '[ -f "$HOME/.acc/.env" ]' not in executor_wrapper
    assert 'or os.environ.get("NVIDIA_API_KEY")' not in startup
    assert 'or os.environ.get("NVIDIA_API_BASE")' not in startup


def test_fleet_deploy_uses_home_scoped_registry_not_legacy_site_config():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "$HOME/.mac/fleets.yaml" in script
    assert "MAC_DEPLOY_FLEETS_CONFIG" in script
    assert "--fleets-config" in script
    assert "--hub <hub-node>" in script
    assert "multiple fleets are configured" in script
    assert "--site-config" not in script
    assert "MAC_DEPLOY_FLEET_SITE_CONFIG" not in script
    assert "FLEET_SITE_CONFIG" not in script


def test_fleet_deploy_network_provider_contract_is_explicit():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    sample = (ROOT / "deploy" / "fleet" / "config.yaml").read_text(encoding="utf-8")

    assert "network_provider = text_field(network.get(\"provider\"))" in script
    assert "network.provider must be tailscale, headscale, or none" in script
    assert "Headscale provider requires network.headscale.login_server" in script
    assert "HEADSCALE_HEALTH_URL" in script
    assert "MAC_DEPLOY_HEADSCALE_PREAUTH_KEY_SOURCE" in script
    assert 'os.environ.get("MAC_DEPLOY_NETWORK_PROVIDER")' in script
    assert 'or os.environ.get("NETWORK_PROVIDER")' in script
    assert 'or "tailscale"' in script
    assert 'if configured_worker_mode == "loop" and agent_name == shared_services_manager:' in script
    assert 'elif network_provider in {"tailscale", "headscale"} and configured_hub_url:' in script
    assert 'values["MAC_HUB_URL"] = configured_hub_url.rstrip("/")' in script
    assert '[ "$WORKER_MODE" = "loop" ] && [ "$AGENT" = "$SHARED_SERVICES_MANAGER_AGENT" ]' in script
    assert "uses_direct_mesh_hub()" in script
    assert 'uses_direct_mesh_hub "$network_provider_field" "$hub_url_field"' in script
    assert "skipping reverse tunnel" in script
    assert "network:" in sample
    assert "provider: none" in sample
    assert "provider: headscale" in sample


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
    assert cfg["defaults"]["tokenhub"]["required"] is True
    assert cfg["defaults"]["tokenhub"]["url"] == "http://hub.example.internal:8090"
    assert cfg["defaults"]["network"]["provider"] == "tailscale"
    assert cfg["defaults"]["network"]["install"] == "auto"
    assert cfg["defaults"]["network"]["headscale"]["manage"] is False
    assert "MAC_DEPLOY_FLEETS_CONFIG=" in env
    assert "MAC_DEPLOY_HUB_AGENT=hub" in env
    assert "MAC_DEPLOY_FLEET_SITE_CONFIG=" not in env
    assert "MAC_DEPLOY_HUB_URL=" not in env
    assert "MAC_SECRET_KEY" not in env
    assert "MAC_API_TOKEN" not in env


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


def test_setup_sh_new_hub_path_delegates_to_deploy_wrapper():
    script = (ROOT / "setup.sh").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "deploy/deploy-mac-fleet.sh" in script
    assert "--hub|--hub=*|--new-hub|--new-hub=*" in script
    assert "--configure-only|--no-deploy" in script
    assert "--deploy-plan-file" in script
    assert 'set -a' in script
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
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    cleanup_plan = "\n".join(cleanup_path_strings(Path.home(), Path.home() / ".mac"))

    assert "--ssh-port <port>" in script
    assert "parse_ssh_target_fields()" in script
    assert 'scp -q -o BatchMode=yes -o ConnectTimeout=10 "${scp_args[@]}"' in script
    assert 'ssh -A -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "${ssh_args[@]}"' in script
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


def test_fleet_deploy_treats_unconfigured_discord_startup_as_benign():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    classifier = script.split("classify_gateway_logs() {", 1)[1].split(
        "verify_hub_registration() {", 1
    )[0]

    assert "discord_missing_token_unconfigured" in classifier
    assert r"\[Discord\] No bot token configured" in classifier
    assert "actionable_text" in classifier
    assert 'if spec["severity"] != "info"' in classifier
    assert 'if spec["severity"] == "info"' in classifier


def test_launchd_worker_wrapper_marks_agent_offline_on_controlled_shutdown():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    wrapper = script.split("install_mac_agent_wrapper() {", 1)[1].split(
        'cat > "$executor" <<', 1
    )[0]

    assert "mark_worker_offline()" in wrapper
    assert "stable_agent_id()" in wrapper
    assert 'trap mark_worker_offline TERM INT' in wrapper
    assert '{"status":"offline","health_status":"degraded"}' in wrapper


def test_worker_wrapper_runs_agent_side_startup_self_test():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    wrapper = script.split("install_mac_agent_wrapper() {", 1)[1].split(
        'cat > "$executor" <<', 1
    )[0]
    selftest = script.split('cat > "$selftest" <<', 1)[1].split(
        'cat > "$executor" <<', 1
    )[0]

    assert 'values.setdefault("MAC_AGENT_STARTUP_SELF_TEST", "1")' in script
    assert '"$HOME/.mac/bin/mac-agent-startup-self-test"' in wrapper
    assert 'resolve_runtime_provider(' in selftest
    assert "MAC_REQUIRE_QDRANT_MEMORY must be true" in selftest
    assert "MAC_REQUIRE_FIRECRAWL must be true" in selftest
    assert '"mandatory_services": {' in selftest
    assert '"key_matches_env": key_matches_env' in selftest
    assert '[python_bin, hermes_script, "chat", "--query", prompt, "--quiet"]' in selftest
    assert "def output_text" in selftest
    assert "output_text(exc.stdout)" in selftest
    assert "classify_hermes_chat_failure" in selftest
    assert '"hermes_failure_class": hermes_failure_class' in selftest
    assert '"blocking_problems": blocking_problems' in selftest
    assert '"status": "offline" if blocking_problems else "idle"' in selftest
    assert "sys.exit(1 if blocking_problems else 0)" in selftest
    assert '"resources": {"startup_self_test": report}' in selftest
    assert '"health_status": "degraded"' in selftest


def test_executor_prompt_includes_repository_runtime_contract():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "def repository_contract_section(task: dict) -> str:" in script
    assert "Repository runtime contract:" in script
    assert "metadata.runtime.repository_worktree" in script
    assert "origin.repository_path / $MAC_TASK_REPO_SOURCE as read-only" in script
    assert "bootstrap.command" in script
    assert "test.command" in script
    assert "returncode=0, status=pass, result=passed" in script


def test_reviewer_prompt_includes_verdict_contract():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

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
    assert 'exec .venv/bin/python -m pytest "$@"' in text
