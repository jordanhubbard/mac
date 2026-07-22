"""Parsing and router-topology coverage for deploy environment generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from mac import deploy_env


def _cfg(tmp_path: Path, *, agent="hub", manager="hub"):
    return deploy_env.DeployEnvConfig(
        paths=deploy_env.DeployPaths(tmp_path / "mac.env", tmp_path / ".mac", tmp_path),
        control=deploy_env.ControlConfig(
            port="8789", hub_url="https://hub.example:8789", hub_token="hub-token",
            bind_host="127.0.0.1", supervisor_kind="systemd", network_provider="tailscale",
        ),
        gateway=deploy_env.GatewayConfig("home", "model", "provider", "https://llm"),
        worker=deploy_env.WorkerConfig("loop", "python", "", "", "1"),
        services=deploy_env.SharedServicesConfig("", "6333", "", "3002"),
        identity=deploy_env.DeployIdentity(agent, manager, "fleet"),
    )


def test_env_assignment_parser_fallbacks_and_file_io(tmp_path) -> None:
    assert deploy_env._raw_env_assignment("missing") is None
    assert deploy_env._raw_env_assignment("export KEY=value") == ("KEY", "value")
    assert deploy_env._parse_env_assignment("KEY='unterminated") is None
    assert deploy_env._parse_env_assignment("KEY=value extra") == ("KEY", "value")
    assert deploy_env._parse_env_assignment("export KEY='a b'") == ("KEY", "a b")
    assert deploy_env._parse_env_assignment("export") is None
    assert deploy_env.read_env_file(tmp_path / "missing") == {}
    path = tmp_path / "env"
    deploy_env.write_env_file(path, {"SAFE": "value", "QUOTED": "a b"})
    assert deploy_env.read_env_file(path) == {"SAFE": "value", "QUOTED": "a b"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_service_url_configured_invalid_ipv6_and_offset() -> None:
    assert deploy_env._service_url(
        configured_url="https://service/", hub_url="", configured_port="1", native_port="2"
    ) == "https://service"
    assert deploy_env._service_url(
        configured_url="", hub_url="not-a-url", configured_port="6333", native_port="8789"
    ) == ""
    assert deploy_env._service_url(
        configured_url="", hub_url="http://[::1]:8789", configured_port="6333", native_port="8789"
    ) == "http://[::1]:6333"
    assert deploy_env._service_url(
        configured_url="", hub_url="http://hub:18789", configured_port="6333", native_port="8789"
    ) == "http://hub:16333"


def test_inproc_hub_validation_and_modality_configuration(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    values = {"MAC_API_TOKEN": "local"}
    with pytest.raises(ValueError, match="requires MAC_DEPLOY_ROUTER_PROVIDERS"):
        deploy_env._apply_inproc_router(values, cfg, {"MAC_DEPLOY_ROUTER_BACKEND": "inproc"})
    with pytest.raises(ValueError, match="key=secret"):
        deploy_env._apply_inproc_router(values, cfg, {
            "MAC_DEPLOY_ROUTER_BACKEND": "inproc", "MAC_DEPLOY_ROUTER_PROVIDERS": "p=https://x,key=raw"
        })
    with pytest.raises(ValueError, match="private endpoint"):
        deploy_env._apply_inproc_router(values, cfg, {
            "MAC_DEPLOY_ROUTER_BACKEND": "inproc",
            "MAC_DEPLOY_ROUTER_PROVIDERS": "p=https://api.example.com,key=none",
        })
    private_values = {"MAC_API_TOKEN": "local"}
    deploy_env._apply_inproc_router(private_values, cfg, {
        "MAC_DEPLOY_ROUTER_BACKEND": "inproc",
        "MAC_DEPLOY_ROUTER_PROVIDERS": "madmax=http://100.121.27.109:8000/v1,0,models=x",
    })
    assert private_values["MAC_ROUTER_PROVIDERS"].endswith(",key=none")
    env = {
        "MAC_DEPLOY_ROUTER_BACKEND": "inproc",
        "MAC_DEPLOY_ROUTER_PROVIDERS": "p=https://x,key=secret:provider",
        "MAC_DEPLOY_ROUTER_DEFAULT_MODEL": "default",
        "MAC_DEPLOY_ROUTER_WILDCARD_MODELS": "one|two",
        "NVIDIA_IMAGE_API_KEY": "image",
        "NVIDIA_AUDIO_API_KEY": "audio",
        "MAC_DEPLOY_ROUTER_AUDIO_UPSTREAM": "https://audio",
    }
    deploy_env._apply_inproc_router(values, cfg, env)
    assert values["OPENAI_API_KEY"] == "local"
    assert values["MAC_ROUTER_IMAGE_KEY"] == "secret:nvidia-image"
    assert values["MAC_ROUTER_AUDIO_UPSTREAM"] == "https://audio"
    assert "MAC_ROUTER_VIDEO_UPSTREAM" not in values


def test_inproc_spoke_validation_and_configuration(tmp_path) -> None:
    cfg = _cfg(tmp_path, agent="spoke", manager="hub")
    cfg = deploy_env.DeployEnvConfig(
        cfg.paths,
        deploy_env.ControlConfig("8789", "", "", "127.0.0.1", "systemd", "tailscale"),
        cfg.gateway, cfg.worker, cfg.services, cfg.identity,
    )
    env = {"MAC_DEPLOY_ROUTER_BACKEND": "inproc"}
    with pytest.raises(ValueError, match="requires MAC_HUB_URL"):
        deploy_env._apply_inproc_router({"MAC_API_TOKEN": "local"}, cfg, env)
    values = {"MAC_API_TOKEN": "local", "MAC_HUB_URL": "https://hub"}
    with pytest.raises(ValueError, match="hub-facing token"):
        deploy_env._apply_inproc_router(values, cfg, env)
    values["MAC_WORKER_TOKEN"] = "local"
    with pytest.raises(ValueError, match="distinct"):
        deploy_env._apply_inproc_router(values, cfg, env)
    values["MAC_WORKER_TOKEN"] = "remote"
    deploy_env._apply_inproc_router(values, cfg, env)
    assert values["OPENAI_BASE_URL"] == "https://hub/v1"
    assert values["NVIDIA_IMAGE_BASE_URL"] == "https://hub/v1/genai"


def test_non_inproc_router_copies_all_optional_values() -> None:
    values = {}
    deploy_env._apply_non_inproc_router(values, {
        "MAC_DEPLOY_ROUTER_BACKEND": "tokenhub",
        "MAC_DEPLOY_ROUTER_PROVIDERS": "providers",
        "MAC_DEPLOY_ROUTER_DEFAULT_MODEL": "model",
        "MAC_DEPLOY_ROUTER_WILDCARD_MODELS": "one|two",
    })
    assert values == {
        "MAC_ROUTER_BACKEND": "tokenhub",
        "MAC_ROUTER_PROVIDERS": "providers",
        "MAC_ROUTER_DEFAULT_MODEL": "model",
        "MAC_ROUTER_WILDCARD_MODELS": "one|two",
    }


def test_gateway_values_build_env_passthrough_and_write(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    gateway = deploy_env._gateway_values(cfg)
    assert gateway["CUSTOM_BASE_URL"] == "https://llm"
    environ = {
        "MAC_DEPLOY_MEMORY_EMBED_MODEL": "embed",
        "MAC_DEPLOY_AGENT_GEN_MODEL": "gen",
        "MAC_DEPLOY_SERVICE_ROLE_OPS": "image.generate",
    }
    values = deploy_env.build_mac_env({}, cfg, environ=environ)
    assert values["MAC_MEMORY_EMBED_MODEL"] == "embed"
    assert values["MAC_AGENT_GEN_MODEL"] == "gen"
    assert values["MAC_SERVICE_ROLE_OPS"] == "image.generate"
    written = deploy_env.write_mac_env_file(cfg, environ=environ)
    assert deploy_env.read_env_file(cfg.paths.env_file)["MAC_AGENT_GEN_MODEL"] == "gen"
    assert written["MAC_SECRET_KEY"]


def test_bound_worker_credential_never_falls_back_to_hub_admin_token(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg = deploy_env.DeployEnvConfig(
        cfg.paths,
        cfg.control,
        cfg.gateway,
        deploy_env.WorkerConfig(
            "loop",
            "python,work_package_v1",
            "",
            "",
            "1",
            token="mac_worker_distinct",
            credential_id="worker-principal-v1",
            credential_version="1",
            credential_agent_id="agent_hub",
            credential_fingerprint="0123456789ab",
            credential_source_commit="a" * 40,
            credential_runtime_digest="runtime-digest",
        ),
        cfg.services,
        cfg.identity,
    )

    values = deploy_env.build_mac_env(
        {"MAC_API_TOKEN": "hub-admin-token", "MAC_SECRET_KEY": "s" * 32},
        cfg,
        environ={},
    )

    assert values["MAC_API_TOKEN"] == "hub-admin-token"
    assert values["MAC_WORKER_TOKEN"] == "mac_worker_distinct"
    assert values["MAC_WORKER_TOKEN"] != values["MAC_API_TOKEN"]
    assert values["MAC_WORKER_CREDENTIAL_AGENT_ID"] == "agent_hub"
    assert values["MAC_WORKER_IDENTITY_MODE"] == "bound"
    assert values["MAC_WORKER_RUNNING_DIGEST"] == "runtime-digest"


def test_incomplete_worker_credential_stays_explicitly_in_compatibility_mode(tmp_path) -> None:
    cfg = _cfg(tmp_path, agent="spoke", manager="hub")
    cfg = deploy_env.DeployEnvConfig(
        cfg.paths,
        cfg.control,
        cfg.gateway,
        deploy_env.WorkerConfig(
            "loop",
            "python",
            "",
            "",
            "1",
            token="per-agent-but-not-confirmed",
        ),
        cfg.services,
        cfg.identity,
    )

    values = deploy_env.build_mac_env(
        {"MAC_API_TOKEN": "local-control", "MAC_SECRET_KEY": "s" * 32},
        cfg,
        environ={},
    )

    assert values["MAC_WORKER_TOKEN"] == "per-agent-but-not-confirmed"
    assert values["MAC_WORKER_IDENTITY_MODE"] == "compatibility"
    assert "MAC_WORKER_CREDENTIAL_ID" not in values


def test_openclaw_worker_advertisement_uses_verified_runtime_file(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    stale = str(tmp_path / "stale.json")
    openclaw = deploy_env.build_mac_env(
        {"MAC_WORKER_RESOURCES_FILE": stale},
        cfg,
        environ={"HERMES_GATEWAY_IMPL": "openclaw"},
    )
    assert openclaw["MAC_CHAT_GATEWAY_IMPL"] == "openclaw"
    assert openclaw["MAC_WORKER_RESOURCES_FILE"] == str(
        tmp_path / ".mac" / "openclaw" / "service-advertisement.json"
    )

    rollback = deploy_env.build_mac_env(
        openclaw,
        cfg,
        environ={"HERMES_GATEWAY_IMPL": "hermes"},
    )
    assert rollback["MAC_CHAT_GATEWAY_IMPL"] == "hermes"
    assert rollback["MAC_WORKER_RESOURCES_FILE"] == str(
        tmp_path / ".mac" / "worker-resources.json"
    )


def test_gateway_impl_none_is_a_pure_worker_no_openclaw(tmp_path) -> None:
    # Worker/gateway decoupling: gateway_impl=none => MAC_CHAT_GATEWAY_IMPL=none,
    # no OpenClaw advertisement. It still gets a generic worker-resources file
    # so its startup health verdict is present in first registration.
    cfg = _cfg(tmp_path)
    worker = deploy_env.build_mac_env(
        {}, cfg, environ={"HERMES_GATEWAY_IMPL": "none"},
    )
    assert worker["MAC_CHAT_GATEWAY_IMPL"] == "none"
    assert worker["MAC_WORKER_RESOURCES_FILE"] == str(
        tmp_path / ".mac" / "worker-resources.json"
    )
    assert not any(k.startswith("MAC_OPENCLAW_") for k in worker)


def test_repository_ref_reconciler_defaults_to_daily_prune_on_hub_only(tmp_path):
    hub = deploy_env.build_mac_env({}, _cfg(tmp_path), environ={})
    assert hub["MAC_CONTROL_PLANE_ROLE"] == "hub"
    assert hub["MAC_DB"] == str(tmp_path / ".mac" / "mac.db")
    assert "MAC_CLIENT_PRINCIPALS_FILE" in hub
    assert hub["MAC_REPOSITORY_REF_RECONCILER_MODE"] == "prune"
    assert hub["MAC_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS"] == "86400"
    assert hub["MAC_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS"] == "300"
    # Agent task-branches are ephemeral: prune on merge (grace 0), not after a week.
    assert hub["MAC_REPOSITORY_REF_RECONCILER_GRACE_DAYS"] == "0"

    spoke = deploy_env.build_mac_env(
        {},
        _cfg(tmp_path, agent="spoke", manager="hub"),
        environ={},
    )
    assert spoke["MAC_REPOSITORY_REF_RECONCILER_MODE"] == "off"
    assert spoke["MAC_CONTROL_PLANE_ROLE"] == "client"
    assert "MAC_DB" not in spoke
    assert "MAC_DATABASE_URL" not in spoke
    assert "MAC_CLIENT_PRINCIPALS_FILE" not in spoke


def test_spoke_env_removes_stale_local_control_plane_configuration(tmp_path):
    spoke = deploy_env.build_mac_env(
        {
            "MAC_DB": "/old/local.db",
            "MAC_DATABASE_URL": "postgresql://old/local",
            "MAC_CLIENT_PRINCIPALS_FILE": "/old/clients.json",
            "MAC_HUB_TICK_INTERVAL_SECONDS": "5",
        },
        _cfg(tmp_path, agent="spoke", manager="hub"),
        environ={},
    )

    assert spoke["MAC_CONTROL_PLANE_ROLE"] == "client"
    assert "MAC_DB" not in spoke
    assert "MAC_DATABASE_URL" not in spoke
    assert "MAC_CLIENT_PRINCIPALS_FILE" not in spoke
    assert "MAC_HUB_TICK_INTERVAL_SECONDS" not in spoke


def test_repository_ref_reconciler_deploy_overrides_and_existing_values_win(tmp_path):
    configured = deploy_env.build_mac_env(
        {},
        _cfg(tmp_path),
        environ={
            "MAC_DEPLOY_REPOSITORY_REF_RECONCILER_MODE": "audit",
            "MAC_DEPLOY_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS": "7200",
            "MAC_DEPLOY_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS": "10",
            "MAC_DEPLOY_REPOSITORY_REF_RECONCILER_GRACE_DAYS": "14",
        },
    )
    assert configured["MAC_REPOSITORY_REF_RECONCILER_MODE"] == "audit"
    assert configured["MAC_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS"] == "7200"
    assert configured["MAC_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS"] == "10"
    assert configured["MAC_REPOSITORY_REF_RECONCILER_GRACE_DAYS"] == "14"

    preserved = deploy_env.build_mac_env(
        {"MAC_REPOSITORY_REF_RECONCILER_MODE": "off"},
        _cfg(tmp_path),
        environ={},
    )
    assert preserved["MAC_REPOSITORY_REF_RECONCILER_MODE"] == "off"


def test_work_package_pipeline_deploy_is_hub_only_and_default_off(tmp_path):
    hub = deploy_env.build_mac_env({}, _cfg(tmp_path), environ={})
    assert hub["MAC_WORK_PACKAGE_PIPELINE_ENABLED"] == "0"
    assert hub["MAC_WORK_PACKAGE_LANDING_ENABLED"] == "0"
    assert hub["MAC_WORK_PACKAGE_BUNDLE_DIR"] == str(
        tmp_path / ".mac" / "work-package-bundles"
    )

    configured = deploy_env.build_mac_env(
        {},
        _cfg(tmp_path),
        environ={
            "MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED": "1",
            "MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED": "1",
            "MAC_DEPLOY_WORK_PACKAGE_BUNDLE_DIR": "/srv/mac/work-package-bundles",
            "MAC_DEPLOY_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT": "http://127.0.0.1:17671",
        },
    )
    assert configured["MAC_WORK_PACKAGE_PIPELINE_ENABLED"] == "1"
    assert configured["MAC_WORK_PACKAGE_LANDING_ENABLED"] == "1"
    assert (
        configured["MAC_WORK_PACKAGE_BUNDLE_DIR"]
        == "/srv/mac/work-package-bundles"
    )
    assert (
        configured["MAC_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT"]
        == "http://127.0.0.1:17671"
    )

    spoke = deploy_env.build_mac_env(
        {
            "MAC_WORK_PACKAGE_PIPELINE_ENABLED": "1",
            "MAC_WORK_PACKAGE_LANDING_ENABLED": "1",
            "MAC_WORK_PACKAGE_BUNDLE_DIR": "/stale/hub/path",
            "MAC_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT": "http://127.0.0.1:17671",
        },
        _cfg(tmp_path, agent="spoke", manager="hub"),
        environ={
            "MAC_DEPLOY_WORK_PACKAGE_PIPELINE_ENABLED": "1",
            "MAC_DEPLOY_WORK_PACKAGE_LANDING_ENABLED": "1",
        },
    )
    assert spoke["MAC_WORK_PACKAGE_PIPELINE_ENABLED"] == "0"
    assert spoke["MAC_WORK_PACKAGE_LANDING_ENABLED"] == "0"
    assert "MAC_WORK_PACKAGE_BUNDLE_DIR" not in spoke
    assert "MAC_CERTIFIER_OPENSHELL_GATEWAY_ENDPOINT" not in spoke


def test_explicit_optional_openshell_disable_scrubs_stale_runtime_env(tmp_path):
    stale = {name: "stale" for name in deploy_env.OPENSHELL_MANAGED_RUNTIME_KEYS}
    stale["MAC_OPENSHELL_SANDBOX"] = "1"
    stale["MAC_ALLOW_UNSANDBOXED_YOLO"] = "0"

    values = deploy_env.build_mac_env(
        stale,
        _cfg(tmp_path, agent="spoke", manager="hub"),
        environ={
            "MAC_DEPLOY_OPENSHELL": " FaLsE ",
            "MAC_DEPLOY_OPENSHELL_REQUIRED": " off ",
        },
    )

    assert values["MAC_OPENSHELL_REQUIRED"] == "off"
    assert not (set(deploy_env.OPENSHELL_MANAGED_RUNTIME_KEYS) & set(values))


def test_required_worker_cannot_be_weakened_by_explicit_openshell_disable(tmp_path):
    stale = {
        "MAC_OPENSHELL_SANDBOX": "1",
        "MAC_OPENSHELL_BIN": "/managed/openshell",
        "MAC_OPENSHELL_POLICY": "/managed/policy.yaml",
        "MAC_OPENSHELL_CREATE_ARGS": "--from managed-runtime",
        "MAC_OPENSHELL_RUNTIME_IMAGE_REF_FILE": "/managed/runtime-image-ref",
    }

    values = deploy_env.build_mac_env(
        stale,
        _cfg(tmp_path, agent="worker", manager="hub"),
        environ={
            "MAC_DEPLOY_OPENSHELL": "0",
            "MAC_DEPLOY_OPENSHELL_REQUIRED": "true",
        },
    )

    assert values["MAC_OPENSHELL_REQUIRED"] == "true"
    assert values["MAC_OPENSHELL_BIN"] == str(
        tmp_path / ".mac" / "bin" / "openshell"
    )
    for name, expected in stale.items():
        if name == "MAC_OPENSHELL_BIN":
            continue
        assert values[name] == expected


def test_execution_cohort_pilot_deploy_is_hub_only_and_secret_safe(tmp_path):
    seed = "stable-pilot-assignment-seed-32-bytes-minimum"
    deploy_values = {
        "MAC_DEPLOY_EXECUTION_COHORT_REVISION": "7",
        "MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT": "40",
        "MAC_DEPLOY_EXECUTION_COHORT_SEED": seed,
    }
    hub = deploy_env.build_mac_env(
        {"MAC_DEPLOY_EXECUTION_COHORT_SEED": "must-not-be-persisted"},
        _cfg(tmp_path),
        environ=deploy_values,
    )
    assert hub["MAC_EXECUTION_COHORT_REVISION"] == "7"
    assert hub["MAC_EXECUTION_COHORT_TREATMENT_PERCENT"] == "40"
    assert hub["MAC_EXECUTION_COHORT_SEED"] == seed
    assert not any(name in hub for name in deploy_values)

    spoke = deploy_env.build_mac_env(
        {
            "MAC_EXECUTION_COHORT_REVISION": "6",
            "MAC_EXECUTION_COHORT_TREATMENT_PERCENT": "90",
            "MAC_EXECUTION_COHORT_SEED": "stale-hub-seed-that-must-be-removed",
            "MAC_DEPLOY_EXECUTION_COHORT_SEED": "stale-deploy-seed",
        },
        _cfg(tmp_path, agent="spoke", manager="hub"),
        environ=deploy_values,
    )
    for name in (*deploy_env.EXECUTION_COHORT_DEPLOY_KEYS, *deploy_env.EXECUTION_COHORT_RUNTIME_KEYS):
        assert name not in spoke


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"MAC_DEPLOY_EXECUTION_COHORT_REVISION": "0"}, "must be positive"),
        (
            {"MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT": "101"},
            "must be between 0 and 100",
        ),
        (
            {"MAC_DEPLOY_EXECUTION_COHORT_SEED": "too-short"},
            "must be at least 32 characters",
        ),
    ],
)
def test_execution_cohort_pilot_deploy_rejects_invalid_hub_config(
    tmp_path, environ, message
):
    with pytest.raises(ValueError, match=message):
        deploy_env.build_mac_env({}, _cfg(tmp_path), environ=environ)


def test_deploy_generation_is_projected_to_exact_worker_barrier(tmp_path):
    cfg = _cfg(tmp_path)
    values = deploy_env.build_mac_env(
        {
            "MAC_WORKER_DEPLOY_GENERATION": "stale-generation",
            "MAC_WORKER_DEPLOY_BARRIER_FILE": "/stale/barrier",
        },
        cfg,
        environ={"MAC_DEPLOY_GENERATION": "revision:hub:attempt-2"},
    )

    assert values["MAC_WORKER_DEPLOY_GENERATION"] == "revision:hub:attempt-2"
    assert values["MAC_WORKER_DEPLOY_BARRIER_FILE"] == str(
        cfg.paths.mac_home / "deploy-start-barrier"
    )
    assert "MAC_DEPLOY_GENERATION" not in values

    cleared = deploy_env.build_mac_env(values, cfg, environ={})
    assert "MAC_WORKER_DEPLOY_GENERATION" not in cleared
    assert "MAC_WORKER_DEPLOY_BARRIER_FILE" not in cleared


@pytest.mark.parametrize(
    "environ",
    (
        {"MAC_DEPLOY_OPENSHELL_ENABLED": "1"},
        {"MAC_DEPLOY_OPENSHELL_REQUIRED": "true"},
        {"HERMES_GATEWAY_IMPL": "openclaw"},
    ),
)
def test_active_openshell_rebinds_stale_runtime_cli_to_reviewed_path(
    tmp_path, environ
):
    cfg = _cfg(tmp_path)
    values = deploy_env.build_mac_env(
        {"MAC_OPENSHELL_BIN": str(tmp_path / ".local/bin/openshell")},
        cfg,
        environ=environ,
    )

    assert values["MAC_OPENSHELL_BIN"] == str(
        cfg.paths.mac_home / "bin" / "openshell"
    )


def test_explicit_openshell_teardown_clears_stale_runtime_cli(tmp_path):
    values = deploy_env.build_mac_env(
        {"MAC_OPENSHELL_BIN": str(tmp_path / ".local/bin/openshell")},
        _cfg(tmp_path),
        environ={
            "MAC_DEPLOY_OPENSHELL": "0",
            "MAC_DEPLOY_OPENSHELL_ENABLED": "0",
            "HERMES_GATEWAY_IMPL": "hermes",
        },
    )

    assert "MAC_OPENSHELL_BIN" not in values


def test_legacy_argument_arity_defaults_and_main(monkeypatch, tmp_path) -> None:
    with pytest.raises(SystemExit, match="expects 30"):
        deploy_env.LegacyDeployArgs.from_argv([])
    args = [
        str(tmp_path / "env"), str(tmp_path / ".mac"), str(tmp_path), "8789",
        "#home", "*", "provider", "https://base", "", "heartbeat", "", "", "", "",
        "6333", "", "3002", "1", "hub", "hub", "", "", "127.0.0.1", "systemd",
        "1", "", "80", "", "/artifacts/", "token",
    ]
    assert len(args) == 30
    cfg = deploy_env.config_from_legacy_args(args, {"FLEET_NAME": "fleet"})
    assert cfg.gateway.model == ""
    assert cfg.identity.fleet_name == "fleet"
    called = []
    monkeypatch.setattr(deploy_env, "write_mac_env_file", lambda cfg: called.append(cfg) or {})
    assert deploy_env.main(["write-mac-env", *args]) == 0
    assert called
