from pathlib import Path
import json
import os
import re
import subprocess
import sys

from mac.deploy_env import (
    ControlConfig,
    DEFAULT_WORKER_CAPABILITIES,
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


def test_fleet_deploy_syncs_hermes_chat_config_from_mac_env():
    # The Hermes runtime reads ~/.hermes/.env + config.yaml (not mac.env) for its
    # chat provider; the deploy must sync those from mac.env or agents dial the
    # retired TokenHub :8090 / send a stale bearer (403) and self-test degrades.
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    assert "sync_hermes_chat_config()" in script
    assert "-m mac.hermes_chat_config" in script
    assert '--mac-env "$ENV_FILE"' in script
    # runs in the main flow right after the gateway runtime shim, before services
    assert "apply_hermes_gateway_runtime_shim\nsync_hermes_chat_config\n" in script


def test_fleet_deploy_exports_python_bin_to_remote():
    # PYTHON_BIN is used in the remote-executed deploy (e.g. install_github_review_key),
    # so it must be in the `export` list shipped to the remote env — like PY — or the
    # remote aborts under `set -u` with "PYTHON_BIN: unbound variable".
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    export_line = next(
        (ln for ln in script.splitlines() if ln.startswith("export AGENT FLEET_NAME")),
        "",
    )
    assert "PYTHON_BIN" in export_line.split(), "PYTHON_BIN must be exported to the remote deploy env"
    # Export alone is insufficient: the remote payload (the `bash -s` heredoc) must
    # also ASSIGN it — `resolve_python_bin` only runs in the local driver. Isolate the
    # one-time-deploy payload and require a PYTHON_BIN assignment within it.
    payload = script.split('"$remote_cmd" <<\'REMOTE\'', 1)[-1].split("\nREMOTE\n", 1)[0]
    assert re.search(r"^\s*PYTHON_BIN=", payload, re.MULTILINE), \
        "remote payload must ASSIGN PYTHON_BIN (e.g. PYTHON_BIN=\"$PY\"), not just export it"


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
    assert DEFAULT_WORKER_CAPABILITIES == expected
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
    # loop-01: the reviewer prompt that asks for a signed review_verdict moved
    # into the extracted mac.task_executor module.
    executor_module = (ROOT / "src" / "mac" / "task_executor.py").read_text(encoding="utf-8")
    assert "evidence_type=review_verdict" in executor_module


def test_fleet_deploy_bootstraps_hub_fleet_record(tmp_path):
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    values = build_mac_env(
        {},
        deploy_env_config(tmp_path, fleet_name="test-fleet"),
        environ={},
    )
    assert values["MAC_FLEET_NAME"] == "test-fleet"
    assert values["MAC_FLEET_TENANT_ID"] == "tenant_test-fleet"
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
    assert "mac-hermes-task-executor" in script
    assert "_load_mac_runtime_context" in runtime_patch.read_text(encoding="utf-8")
    assert "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN" in runtime_patch.read_text(encoding="utf-8")
    assert "Shutdown chat notifications disabled by MAC deployment policy." in quench_patch.read_text(
        encoding="utf-8"
    )


def test_fleet_deploy_declares_shared_memory_and_supervision_contract(tmp_path):
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
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
    assert "systemd|launchd|supervisord" in script
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
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
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
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    linux_service = script.split("install_linux_service() {", 1)[1].split(
        "install_supervisord_service() {", 1
    )[0]

    assert "install_mac_control_wrapper" in linux_service
    assert 'export PATH="$HOME/.mac/bin:$HOME/.mac/venv/bin:$PATH"' in script
    assert "ExecStart=$MAC_HOME/bin/mac-service" in linux_service
    assert "ExecStart=$VENV/bin/uvicorn" not in linux_service


def test_fleet_deploy_routes_provider_secrets_through_in_mac_router(tmp_path):
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    startup = (ROOT / "src" / "mac" / "hermes_startup.py").read_text(encoding="utf-8")
    gateway_wrapper = script.split("install_hermes_gateway_wrapper() {", 1)[1].split(
        "install_mac_agent_wrapper() {", 1
    )[0]
    executor_wrapper = script.split('cat > "$executor" <<', 1)[1].split(
        'cat > "$executor_py" <<', 1
    )[0]

    # th-merge-07: TokenHub is retired. Messaging config (Slack secrets +
    # identity + home channels) is synced as one unit AFTER the mac.service
    # (re)start, before the gateway comes up.
    assert "sync_messaging_config()" in script
    assert (
        "  reload_mac_env\n"
        "  fetch_slack_secrets_from_vault\n"
        "  reload_mac_env\n"
        "  sync_hermes_slack_identity_env\n"
        "  sync_hermes_home_channels"
    ) in script
    assert "fetch_slack_secrets_from_vault()" in script
    assert "scripts/mac-fetch-slack-secrets.py" in script
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
    assert "mac-hermes-task-executor" in script
    executor_module = (ROOT / "src" / "mac" / "task_executor.py").read_text(encoding="utf-8")
    assert '"chat", "--query", prompt, "--quiet", "--accept-hooks", "--yolo"' in executor_module
    assert "def write_fallback_evidence_manifest(" in executor_module
    # autonomy-loop fix (preserved through the extraction): the fallback must
    # never fabricate verified completion — UNVERIFIED operator_result only,
    # never a fake repo_change/test and never a synthetic passing check.
    assert '"evidence_type": "operator_result",' in executor_module
    assert '"name": "hermes_chat_query"' not in executor_module
    # telemetry path + memory feed (deployment gets smarter over time)
    assert 'name": "executor.%s"' in executor_module or '"executor.%s"' in executor_module
    assert "def recall_deployment_lessons(" in executor_module
    assert "def record_deployment_learning(" in executor_module
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
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    qdrant_validator = script.split("validate_qdrant_endpoint() {", 1)[1].split("\n}", 1)[0]
    firecrawl_validator = script.split("validate_firecrawl_endpoint() {", 1)[1].split("\n}", 1)[0]
    for validator in (qdrant_validator, firecrawl_validator):
        assert 'degraded="${MAC_DEPLOY_ALLOW_DEGRADED_SERVICES:-0}"' in validator
        # Both the unreachable-endpoint and missing-endpoint branches must offer a
        # degraded early-return guarded by the flag.
        assert validator.count('if [ "$degraded" = "1" ]; then') == 2
        assert "proceeding degraded (first deploy" in validator
    # The flag is plumbed into the remote deploy env and consumed by the
    # post-deploy reconnect path in main().
    assert 'add_remote_env MAC_DEPLOY_ALLOW_DEGRADED_SERVICES "${allow_degraded_services:-0}"' in script
    assert '[ "${allow_degraded_services:-0}" = "1" ]' in script


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


def test_env_writer_spoke_without_hub_token_leaves_gateway_unconfigured(tmp_path):
    # No configured hub token → MAC_WORKER_TOKEN degenerates to the local token.
    out = _run_env_writer(
        tmp_path, agent="natasha", hub_agent="rocky",
        hub_url="http://hub.example:8789", hub_token="",
        extra_env={**_ROUTER_ENV, "NVIDIA_API_KEY": ""},
        existing_env_text=(
            "OPENAI_BASE_URL=https://old.example/v1\n"
            "OPENAI_API_KEY=OLD_OPENAI\n"
            "MAC_HERMES_GATEWAY_BASE_URL=https://old.example/v1\n"
            "MAC_HERMES_GATEWAY_API_KEY=OLD_GATEWAY\n"
            "NVIDIA_API_KEY=OLD_NVIDIA\n"
            "MAC_ROUTER_PROVIDERS=nvidia=https://old.example/v1,0,key=old\n"
        ),
    )
    # do NOT point the gateway at the hub with a broken local-token credential
    assert out.get("OPENAI_BASE_URL") != "http://hub.example:8789/v1"
    local = out.get("MAC_API_TOKEN")
    assert out.get("OPENAI_API_KEY") != local  # never the local token
    assert out.get("MAC_HERMES_GATEWAY_API_KEY") != local
    assert "OLD_OPENAI" not in out.values()
    assert "OLD_GATEWAY" not in out.values()
    assert "OLD_NVIDIA" not in out.values()
    assert "MAC_ROUTER_PROVIDERS" not in out


def _extract_bash_fn(name):
    import re as _re
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
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
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    # symmetric with the hub escrow: escrow (hub) then scrub (spoke) before messaging,
    # in each of the three service flows (systemd, launchd, supervisord)
    assert script.count("\n  escrow_router_provider_keys\n  scrub_spoke_provider_secrets\n  sync_messaging_config\n") == 3


def test_deploy_host_blanks_provider_keys_for_spokes():
    # Stream B: deploy_host must NOT ship upstream provider keys to spokes (only
    # the hub keeps them). It blanks them before building the remote SSH command.
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
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
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
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
    # also escrows the image-gen key (NVIDIA_API_KEY -> secret:nvidia-image) so the
    # hub /v1/genai proxy can resolve it (Phase 1, image-gen via the hub)
    assert 'image_key_spec = (os.environ.get("MAC_ROUTER_IMAGE_KEY")' in fn
    assert 'ivalue = (os.environ.get("NVIDIA_API_KEY")' in fn
    # failure is loud but non-fatal (chat won't route until the key is escrowed)
    assert "router provider key escrow failed" in fn
    # invoked on the hub after the API is up, before the gateway, in all three
    # service flows (systemd, launchd, supervisord)
    assert script.count("\n  escrow_router_provider_keys\n") == 3


def test_network_none_spoke_uses_tunnel_forwarded_service_ports():
    # gketun-02: cross-pod service ports are blocked under network=none, so a spoke
    # must reach hub Qdrant/Firecrawl via the reverse tunnel's localhost forwards
    # (16333/13002), not the hub FQDN.
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    assert (
        'if [ "$NETWORK_PROVIDER" = "none" ] && [ "$AGENT" != "$SHARED_SERVICES_MANAGER_AGENT" ]; then'
        in script
    )
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

    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    fn = script.split("install_omniverse_gpu_skills() {", 1)[1].split("\ninitialize_hermes_home() {", 1)[0]
    assert "nvidia-smi -L" in fn  # GPU gate
    assert 'deploy/skills/omniverse-skills.tar.gz' in fn
    assert '"$HOME/.hermes/skills"' in fn
    # invoked in the agent setup flow (right after the fleet-wide skill install)
    assert "\ninstall_fleet_skills\ninstall_omniverse_gpu_skills\n" in script


def test_reverse_tunnel_program_keeps_retrying_until_key_authorized():
    # gketun-01: install_reverse_tunnel_on_hub runs before the spoke authorizes the
    # hub key, so the tunnel program must keep retrying instead of going FATAL after
    # the default 3 attempts.
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
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
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert "$HOME/.mac/fleets.yaml" in script
    assert "MAC_DEPLOY_FLEETS_CONFIG" in script
    assert "--fleets-config" in script
    assert "--hub <hub-node>" in script
    assert "multiple fleets are configured" in script
    assert "--site-config" not in script
    assert "MAC_DEPLOY_FLEET_SITE_CONFIG" not in script
    assert "FLEET_SITE_CONFIG" not in script


def test_fleet_deploy_network_provider_contract_is_explicit(tmp_path):
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
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
    deploy = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    assert 'fleet_scoped_env MAC_ROUTER_BACKEND' in deploy
    assert 'fleet_scoped_env MAC_ROUTER_PROVIDERS' in deploy
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


def test_setup_entrypoints_are_python_driven_and_make_exposed():
    script = (ROOT / "setup.sh").read_text(encoding="utf-8")
    setup_py = (ROOT / "setup.py").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")

    assert script.startswith("#!/bin/sh")
    assert "exec \"$PYTHON\" \"$ROOT/setup.py\" \"$@\"" in script
    assert "BASH_SOURCE" not in script
    assert "read -r -d" not in script
    assert "DEPLOY_FLEET = ROOT / \"deploy\" / \"deploy-mac-fleet.sh\"" in setup_py
    assert "DEFAULT_ENV_FILE = Path.home() / \".mac\" / \".env\"" in setup_py
    assert "def parse_setup_args" in setup_py
    assert "def configure_then_deploy" in setup_py
    assert "def deploy_env" in setup_py
    assert "PYTHON ?= $(shell for candidate in python3 python" in makefile
    assert "sys.version_info >= (3, 9)" in makefile
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


def test_worker_wrapper_runs_agent_side_startup_self_test(tmp_path):
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    generated_env = build_mac_env({}, deploy_env_config(tmp_path), environ={})
    wrapper = script.split("install_mac_agent_wrapper() {", 1)[1].split(
        'cat > "$executor" <<', 1
    )[0]
    selftest = script.split('cat > "$selftest" <<', 1)[1].split(
        'cat > "$executor" <<', 1
    )[0]

    assert generated_env["MAC_AGENT_STARTUP_SELF_TEST"] == "1"
    assert '"$HOME/.mac/bin/mac-agent-startup-self-test"' in wrapper
    assert 'resolve_runtime_provider(' in selftest
    assert "MAC_REQUIRE_QDRANT_MEMORY must be true" in selftest
    assert "MAC_REQUIRE_FIRECRAWL must be true" in selftest
    assert '"mandatory_services": {' in selftest
    assert '[python_bin, "-m", "hermes_cli.main", "chat", "--query", prompt, "--quiet"]' in selftest
    assert "def output_text" in selftest
    assert "output_text(exc.stdout)" in selftest
    assert "classify_hermes_chat_failure" in selftest
    assert '"hermes_failure_class": hermes_failure_class' in selftest
    assert '"blocking_problems": blocking_problems' in selftest
    assert '"status": "offline" if blocking_problems else "idle"' in selftest
    assert "sys.exit(1 if blocking_problems else 0)" in selftest
    assert '"resources": {"startup_self_test": report}' in selftest
    assert '"health_status": "degraded" if problems else "healthy"' in selftest
    assert '"health_status": "degraded"' in selftest


def test_executor_prompt_includes_repository_runtime_contract():
    # loop-01: the executor (and its prompts) live in mac.task_executor now.
    script = (ROOT / "src" / "mac" / "task_executor.py").read_text(encoding="utf-8")

    assert "def repository_contract_section(task: Dict[str, Any]) -> str:" in script
    assert "Repository runtime contract:" in script
    assert "metadata.runtime.repository_worktree" in script
    assert "origin.repository_path / $MAC_TASK_REPO_SOURCE as read-only" in script
    assert "bootstrap.command" in script
    assert "test.command" in script
    assert "returncode=0, status=pass, result=passed" in script


def test_reviewer_prompt_includes_verdict_contract():
    script = (ROOT / "src" / "mac" / "task_executor.py").read_text(encoding="utf-8")

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
