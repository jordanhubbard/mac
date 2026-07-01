from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from mac import fleet_setup


def _base_spec() -> dict:
    return {
        "schema": fleet_setup.SETUP_SPEC_SCHEMA,
        "hub": {"name": "hub", "target": "ops@hub.example"},
        "agents": [{"name": "hub", "target": "ops@hub.example"}],
        "router": {
            "providers": [{"id": "nvidia", "key": "inline-secret"}]
        },
        "network": {"provider": "none"},
    }


def _build(tmp_path: Path, spec: dict) -> dict:
    return fleet_setup.build_setup_plan(
        spec,
        root=tmp_path,
        fleets_config=tmp_path / "fleets.yaml",
        env={},
    )


def test_load_setup_spec_json_non_mapping_and_missing_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    json_path = tmp_path / "spec.json"
    json_path.write_text(json.dumps(["not", "mapping"]), encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        fleet_setup.load_setup_spec(json_path)

    yaml_path = tmp_path / "spec.yaml"
    yaml_path.write_text("schema: x\n", encoding="utf-8")
    original_import = builtins.__import__

    def fail_yaml(name: str, *args, **kwargs):
        if name == "yaml":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_yaml)
    with pytest.raises(RuntimeError, match="PyYAML"):
        fleet_setup.load_setup_spec(yaml_path)


def test_bad_spec_collects_validation_errors_and_router_warning(tmp_path: Path) -> None:
    spec = {
        "schema": "wrong",
        "fleet": {"hub": "hub"},
        "supervisor": "bad",
        "ssh_host_key_policy": "maybe",
        "agents": [{"name": "worker", "target": "host"}],
        "router": {"providers": [{"id": "unknown"}]},
        "network": {"provider": "invalid"},
    }
    plan = _build(tmp_path, spec)
    joined = "; ".join(plan["errors"])
    assert "schema" in joined
    assert "supervisor" in joined
    assert "ssh_host_key_policy" in joined
    assert "include the hub" in joined
    assert "network.provider" in joined
    assert "at least one" in joined
    assert plan["warnings"] == ["unknown router provider skipped: unknown"]


def test_tailscale_and_headscale_secret_branches(tmp_path: Path) -> None:
    tailscale = _base_spec()
    tailscale["network"] = {
        "provider": "tailscale",
        "tailscale": {"auth_key_env": "TS_AUTH"},
    }
    tailscale["require_mesh_auth"] = True
    missing = _build(tmp_path, tailscale)
    assert "TS_AUTH" in missing["required_env"]

    tailscale["secrets"] = {"tailscale_auth_key": "tail-secret"}
    supplied = _build(tmp_path, tailscale)
    assert supplied["env_values"]["TS_AUTH"] == "tail-secret"

    headscale = _base_spec()
    headscale["network"] = {
        "provider": "headscale",
        "headscale": {
            "login_server": "https://headscale.example",
            "preauth_key_env": "HS_KEY",
        },
    }
    headscale["secrets"] = {
        "headscale_preauth_key": "head-secret",
        "hub_token": "hub-token",
    }
    head = _build(tmp_path, headscale)
    assert head["env_values"]["HS_KEY"] == "head-secret"
    assert head["env_values"]["MAC_DEPLOY_HUB_TOKEN"] == "hub-token"


def test_agent_config_validation_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    errors: list[str] = []

    def normalize(target: str, *, port=None):
        if target == "bad-target":
            raise ValueError("invalid target")
        return target

    monkeypatch.setattr(fleet_setup, "normalize_ssh_target", normalize)
    agents = fleet_setup._agent_configs(
        {
            "agents": [
                {},
                {"name": "dup", "target": "host"},
                {"name": "dup", "target": "host"},
                {
                    "name": "invalid",
                    "target": "bad-target",
                    "os": "windows",
                    "supervisor": "bad",
                },
            ]
        },
        hub_name="hub",
        supervisor="auto",
        errors=errors,
        hermes_defaults={},
        worker_defaults={},
    )
    assert len(agents) == 2
    joined = "; ".join(errors)
    assert "needs a name" in joined
    assert "duplicate" in joined
    assert "target invalid" in joined
    assert "os must" in joined
    assert "supervisor invalid" in joined


def test_router_network_webdav_and_helper_edges() -> None:
    router, values, required, warnings = fleet_setup._router_env(
        {
            "providers": [
                {
                    "id": "nvidia",
                    "key": "secret",
                    "base_url": "https://custom.example/v1",
                }
            ],
            "router": {},
        },
        {},
    )
    assert router["providers"].startswith("nvidia=")
    assert values["NVIDIA_API_KEY"] == "secret"
    assert values["NVIDIA_BASE_URL"] == "https://custom.example/v1"
    assert not required and not warnings

    errors: list[str] = []
    fleet_setup._network_config(
        {"network": {"provider": "headscale", "headscale": {}}}, {}, errors
    )
    assert "login_server" in errors[0]

    errors = []
    webdav = fleet_setup._webdav_config(
        {
            "webdav": {
                "enabled": True,
                "dns_name": "example.com",
                "url": "http://example.com/files",
            }
        },
        {},
        {},
        errors,
    )
    assert webdav["enabled"] is True
    assert "https" in errors[0]
    assert not fleet_setup._valid_dns_name("127.0.0.1")
    assert fleet_setup._normalize_public_path("files") == "/files/"
    assert fleet_setup._host_from_target("user@example.com:2222") == "example.com"
    assert fleet_setup._status([], [{"status": "warn"}]) == "warn"
    assert fleet_setup._list((1, 2)) == [1, 2]
    assert fleet_setup._list("one") == ["one"]
    assert fleet_setup._optional_int("5") == 5
    assert fleet_setup._optional_int("bad") is None
