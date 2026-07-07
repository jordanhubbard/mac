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
    assert "MAC_WORKER_RESOURCES_FILE" not in rollback


def test_repository_ref_reconciler_defaults_to_daily_prune_on_hub_only(tmp_path):
    hub = deploy_env.build_mac_env({}, _cfg(tmp_path), environ={})
    assert hub["MAC_CONTROL_PLANE_ROLE"] == "hub"
    assert hub["MAC_DB"] == str(tmp_path / ".mac" / "mac.db")
    assert "MAC_CLIENT_PRINCIPALS_FILE" in hub
    assert hub["MAC_REPOSITORY_REF_RECONCILER_MODE"] == "prune"
    assert hub["MAC_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS"] == "86400"
    assert hub["MAC_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS"] == "300"
    assert hub["MAC_REPOSITORY_REF_RECONCILER_GRACE_DAYS"] == "7"

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
