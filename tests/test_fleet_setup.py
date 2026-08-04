from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from mac import fleet_deploy
from mac.fleet_setup import build_setup_plan, public_plan

# imports relocated from test_fleet_setup_edges.py
import builtins
import pytest
from mac import fleet_setup


ROOT = Path(__file__).resolve().parents[1]


def _spec() -> dict:
    return {
        "schema": "mac.fleet_setup.v1",
        "fleet": {
            "name": "horde",
            "hub": "horde-hub",
            "hub_url": "http://horde-hub:8789",
        },
        "agents": [
            {
                "name": "horde-hub",
                "target": "ubuntu@10.0.0.10:2201",
                "os": "linux",
                "model": "nvidia/test-model",
                "worker": {"mode": "loop"},
            },
            {
                "name": "horde-worker",
                "target": "ubuntu@10.0.0.11",
                "os": "linux",
                "worker": {"mode": "heartbeat"},
            },
        ],
        "router": {
            "backend": "inproc",
            "providers": [{"id": "nvidia", "key_env": "NVIDIA_API_KEY"}],
        },
        "network": {"provider": "none"},
    }


def test_declarative_setup_plan_builds_existing_fleet_registry_shape(tmp_path):
    plan = build_setup_plan(
        _spec(),
        root=ROOT,
        fleets_config=tmp_path / "fleets.yaml",
        env_file=tmp_path / ".env",
        env={"NVIDIA_API_KEY": "nv-secret"},
    )

    assert plan["status"] == "pass"
    assert plan["hub"] == "horde-hub"
    assert plan["fleet_config"]["sample"] is False
    assert plan["fleet_config"]["hub_agent"] == "horde-hub"
    assert plan["fleet_config"]["agents"][0]["target"] == "ubuntu@10.0.0.10:2201"
    assert plan["fleet_config"]["agents"][0]["control_bind_host"] == "0.0.0.0"
    assert plan["env_values"]["MAC_ROUTER_BACKEND"] == "inproc"
    assert (
        "nvidia=https://inference-api.nvidia.com/v1,0,key=secret:nvidia-upstream"
        in plan["env_values"]["MAC_ROUTER_PROVIDERS"]
    )
    assert plan["env_values"]["NVIDIA_API_KEY"] == "nv-secret"
    assert 'make deploy HUB=horde-hub ARGS="horde-hub"' in plan["next_steps"][0]

    redacted = public_plan(plan)
    assert redacted["env_values"]["NVIDIA_API_KEY"] == "<set>"
    assert redacted["env_values"]["MAC_API_TOKEN"] == "<set>"


def test_declarative_setup_canonicalizes_local_target_and_implicit_hub_url(
    tmp_path, monkeypatch
):
    spec = _spec()
    spec["fleet"].pop("hub_url")
    spec["agents"] = [
        {
            "name": "horde-hub",
            "target": "ubuntu@horde-hub.local:2201",
            "os": "linux",
            "model": "nvidia/test-model",
        }
    ]
    spec["network"] = {"provider": "tailscale"}
    monkeypatch.setattr(
        fleet_deploy,
        "_tailscale_status",
        lambda: {
            "BackendState": "Running",
            "Self": {},
            "Peer": {
                "node": {
                    "HostName": "horde-hub",
                    "DNSName": "horde-hub.example.ts.net.",
                    "TailscaleIPs": ["100.80.0.10"],
                }
            },
        },
    )

    plan = build_setup_plan(
        spec,
        root=ROOT,
        fleets_config=tmp_path / "fleets.yaml",
        env={"NVIDIA_API_KEY": "nv-secret"},
    )

    assert plan["status"] == "pass"
    assert plan["fleet_config"]["agents"][0]["target"] == "ubuntu@100.80.0.10:2201"
    assert plan["fleet_config"]["hub_url"] == "http://100.80.0.10:8789"


def test_declarative_webdav_requires_dns_name_and_derives_https_url(tmp_path):
    spec = _spec()
    spec["webdav"] = {"enabled": True, "dns_name": "jordanhubbard.net", "public_host": "146.190.134.110"}
    plan = build_setup_plan(
        spec,
        root=ROOT,
        fleets_config=tmp_path / "fleets.yaml",
        env_file=tmp_path / ".env",
        env={"NVIDIA_API_KEY": "nv-secret"},
    )

    webdav = plan["fleet_config"]["defaults"]["webdav"]
    assert webdav["port"] == 80
    assert webdav["dns_name"] == "jordanhubbard.net"
    assert webdav["url"] == "https://jordanhubbard.net/artifacts/"


def test_declarative_webdav_enabled_without_dns_name_fails(tmp_path):
    spec = _spec()
    spec["webdav"] = {"enabled": True, "public_host": "146.190.134.110"}
    plan = build_setup_plan(
        spec,
        root=ROOT,
        fleets_config=tmp_path / "fleets.yaml",
        env_file=tmp_path / ".env",
        env={"NVIDIA_API_KEY": "nv-secret"},
    )

    assert plan["status"] == "fail"
    assert "webdav.enabled requires webdav.dns_name" in "; ".join(plan["errors"])


def test_declarative_setup_plan_reports_missing_provider_env(tmp_path):
    env_file = tmp_path / ".env"
    plan = build_setup_plan(
        _spec(),
        root=ROOT,
        fleets_config=tmp_path / "fleets.yaml",
        env_file=env_file,
        env={},
    )

    assert not env_file.exists()
    assert plan["status"] == "fail"
    assert plan["required_env"] == ["NVIDIA_API_KEY"]
    env_check = [check for check in plan["checks"] if check["name"] == "env.required"][0]
    assert env_check["status"] == "fail"
    assert "NVIDIA_API_KEY" in env_check["detail"]


def test_declarative_setup_plan_reads_required_provider_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("NVIDIA_API_KEY=env-file-secret\n", encoding="utf-8")

    plan = build_setup_plan(
        _spec(),
        root=ROOT,
        fleets_config=tmp_path / "fleets.yaml",
        env_file=env_file,
        env={},
    )

    assert plan["status"] == "pass"
    assert plan["env_values"]["NVIDIA_API_KEY"] == "env-file-secret"
    rendered = json.dumps(public_plan(plan))
    assert "env-file-secret" not in rendered
    assert public_plan(plan)["env_values"]["NVIDIA_API_KEY"] == "<set>"


def test_live_environment_overrides_persisted_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("NVIDIA_API_KEY=persisted-secret\n", encoding="utf-8")
    monkeypatch.setenv("NVIDIA_API_KEY", "live-secret")

    plan = build_setup_plan(
        _spec(),
        root=ROOT,
        fleets_config=tmp_path / "fleets.yaml",
        env_file=env_file,
    )

    assert plan["status"] == "pass"
    assert plan["env_values"]["NVIDIA_API_KEY"] == "live-secret"


def test_setup_fleet_spec_mode_writes_registry_and_env(tmp_path):
    spec_path = tmp_path / "fleet.yaml"
    fleets_config = tmp_path / "fleets.yaml"
    env_file = tmp_path / ".env"
    spec_path.write_text(yaml.safe_dump(_spec()), encoding="utf-8")
    env = {**os.environ, "NVIDIA_API_KEY": "nv-secret"}

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "setup-fleet.py"),
            "--spec",
            str(spec_path),
            "--fleets-config",
            str(fleets_config),
            "--env-file",
            str(env_file),
            "--force",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    registry = yaml.safe_load(fleets_config.read_text(encoding="utf-8"))
    assert registry["fleets"]["horde-hub"]["hub_url"] == "http://horde-hub:8789"
    assert registry["fleets"]["horde-hub"]["agents"][1]["name"] == "horde-worker"
    env_text = env_file.read_text(encoding="utf-8")
    assert "MAC_ROUTER_BACKEND=inproc" in env_text
    assert "MAC_ROUTER_PROVIDERS=" in env_text
    assert "NVIDIA_API_KEY=nv-secret" in env_text
    assert 'make deploy HUB=horde-hub ARGS="horde-hub"' in result.stdout


def test_mac_fleet_doctor_prints_llm_setup_report(tmp_path):
    spec_path = tmp_path / "fleet.yaml"
    env_file = tmp_path / ".env"
    spec_path.write_text(yaml.safe_dump(_spec()), encoding="utf-8")
    env_file.write_text("NVIDIA_API_KEY=doctor-file-secret\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("NVIDIA_API_KEY", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mac.cli",
            "--json",
            "fleet",
            "doctor",
            "--spec",
            str(spec_path),
            "--fleets-config",
            str(tmp_path / "fleets.yaml"),
            "--env-file",
            str(env_file),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema"] == "mac.fleet_setup_doctor.v1"
    assert report["status"] == "pass"
    assert report["hub"] == "horde-hub"
    assert any(check["name"] == "router.providers" for check in report["checks"])


def test_mac_fleet_validate_reads_and_redacts_env_file(tmp_path):
    spec_path = tmp_path / "fleet.yaml"
    env_file = tmp_path / ".env"
    spec_path.write_text(yaml.safe_dump(_spec()), encoding="utf-8")
    env_file.write_text("NVIDIA_API_KEY=validate-file-secret\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("NVIDIA_API_KEY", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mac.cli",
            "--json",
            "fleet",
            "validate",
            "--spec",
            str(spec_path),
            "--fleets-config",
            str(tmp_path / "fleets.yaml"),
            "--env-file",
            str(env_file),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "validate-file-secret" not in result.stdout
    assert "validate-file-secret" not in result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "pass"
    assert report["env_values"]["NVIDIA_API_KEY"] == "<set>"


def test_spec_path_materializes_default_model_never_blank(tmp_path):
    """The --spec planner must record a concrete gateway_model for every agent:
    an agent with an explicit model keeps it; one with none gets the default.
    A blank model is what silently sent the fleet to gpt-4.1-mini."""
    from mac.fleet_setup import DEFAULT_GATEWAY_MODEL

    plan = build_setup_plan(
        _spec(),
        root=ROOT,
        fleets_config=tmp_path / "fleets.yaml",
        env_file=tmp_path / ".env",
        env={"NVIDIA_API_KEY": "nv-secret"},
    )
    models = {a["name"]: a["hermes"]["gateway_model"] for a in plan["fleet_config"]["agents"]}
    assert models["horde-hub"] == "nvidia/test-model"  # explicit model preserved
    assert models["horde-worker"] == DEFAULT_GATEWAY_MODEL  # blank -> default, not ""
    assert all(v for v in models.values())  # nothing blank


# --- relocated from test_fleet_setup_edges.py (coverage companion folded in) ---

def _base_spec() -> dict:
    return {'schema': fleet_setup.SETUP_SPEC_SCHEMA, 'hub': {'name': 'hub', 'target': 'ops@hub.example'}, 'agents': [{'name': 'hub', 'target': 'ops@hub.example'}], 'router': {'providers': [{'id': 'nvidia', 'key': 'inline-secret'}]}, 'network': {'provider': 'none'}}


def _build(tmp_path: Path, spec: dict) -> dict:
    return fleet_setup.build_setup_plan(spec, root=tmp_path, fleets_config=tmp_path / 'fleets.yaml', env={})


def test_load_setup_spec_json_non_mapping_and_missing_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    json_path = tmp_path / 'spec.json'
    json_path.write_text(json.dumps(['not', 'mapping']), encoding='utf-8')
    with pytest.raises(ValueError, match='mapping'):
        fleet_setup.load_setup_spec(json_path)
    yaml_path = tmp_path / 'spec.yaml'
    yaml_path.write_text('schema: x\n', encoding='utf-8')
    original_import = builtins.__import__

    def fail_yaml(name: str, *args, **kwargs):
        if name == 'yaml':
            raise ImportError('missing')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', fail_yaml)
    with pytest.raises(RuntimeError, match='PyYAML'):
        fleet_setup.load_setup_spec(yaml_path)


def test_bad_spec_collects_validation_errors_and_router_warning(tmp_path: Path) -> None:
    spec = {'schema': 'wrong', 'fleet': {'hub': 'hub'}, 'supervisor': 'bad', 'ssh_host_key_policy': 'maybe', 'agents': [{'name': 'worker', 'target': 'host'}], 'router': {'providers': [{'id': 'unknown'}]}, 'network': {'provider': 'invalid'}}
    plan = _build(tmp_path, spec)
    joined = '; '.join(plan['errors'])
    assert 'schema' in joined
    assert 'supervisor' in joined
    assert 'ssh_host_key_policy' in joined
    assert 'include the hub' in joined
    assert 'network.provider' in joined
    assert 'at least one' in joined
    assert plan['warnings'] == ['unknown router provider skipped: unknown']


def test_gateway_impl_runtime_selector_allowlist(tmp_path: Path) -> None:
    """The persona/Slack runtime selector accepts hermes, openclaw, or none.

    `hermes` was removed from this allow-list on 2026-07-26 (ab7a5020) when
    OpenClaw was to be the sole gateway. That migration was HALTED on
    2026-08-04 after all three of its premises measured false -- see
    docs/hermes-retirement-premises.md. The vendored runtime, the
    mac-hermes-gateway console script and the launcher were never removed, so
    restoring the selector restores a choice, not an implementation.

    An unknown value must still fail loudly (mirrors network.provider), which
    is the property this test exists to protect.
    """
    for accepted in ('hermes', 'openclaw', 'none'):
        spec = _base_spec()
        spec['gateway_impl'] = accepted
        assert not any(
            'gateway_impl' in e for e in _build(tmp_path, spec)['errors']
        ), accepted
    bad = _base_spec()
    bad['gateway_impl'] = 'nemoclaw'
    assert any(
        'gateway_impl must be hermes, openclaw, or none' in e
        for e in _build(tmp_path, bad)['errors']
    )


def test_openclaw_defaults_block_read_with_hermes_fallback(tmp_path: Path) -> None:
    """Runtime defaults read from the OpenClaw block, and the legacy hermes key
    still resolves for backward compatibility."""
    spec = _base_spec()
    spec['defaults'] = {'openclaw': {'gateway_provider': 'newclaw', 'slack_home_channel_name': 'chan'}}
    defaults = _build(tmp_path, spec)['fleet_config']['defaults']['hermes']
    assert defaults['gateway_provider'] == 'newclaw'
    assert defaults['slack_home_channel_name'] == 'chan'
    legacy = _base_spec()
    legacy['defaults'] = {'hermes': {'gateway_provider': 'legacyclaw'}}
    assert _build(tmp_path, legacy)['fleet_config']['defaults']['hermes']['gateway_provider'] == 'legacyclaw'


def test_tailscale_and_headscale_secret_branches(tmp_path: Path) -> None:
    tailscale = _base_spec()
    tailscale['network'] = {'provider': 'tailscale', 'tailscale': {'auth_key_env': 'TS_AUTH'}}
    tailscale['require_mesh_auth'] = True
    missing = _build(tmp_path, tailscale)
    assert 'TS_AUTH' in missing['required_env']
    tailscale['secrets'] = {'tailscale_auth_key': 'tail-secret'}
    supplied = _build(tmp_path, tailscale)
    assert supplied['env_values']['TS_AUTH'] == 'tail-secret'
    headscale = _base_spec()
    headscale['network'] = {'provider': 'headscale', 'headscale': {'login_server': 'https://headscale.example', 'preauth_key_env': 'HS_KEY'}}
    headscale['secrets'] = {'headscale_preauth_key': 'head-secret', 'hub_token': 'hub-token'}
    head = _build(tmp_path, headscale)
    assert head['env_values']['HS_KEY'] == 'head-secret'
    assert head['env_values']['MAC_DEPLOY_HUB_TOKEN'] == 'hub-token'


def test_tailscale_auth_key_format_validation() -> None:
    valid = [
        'tskey-auth-k123456789CNTRL-abcdefghij0123456789',
        'tskey-abcdefghij0123',
        '  tskey-auth-kAbc123CNTRL-zzzzzzzzzz  ',
    ]
    invalid = ['tail-secret', 'tskey-', 'tskey-auth-x', '', 'placeholder-key']
    for value in valid:
        assert fleet_setup._looks_like_tailscale_auth_key(value) is True
    for value in invalid:
        assert fleet_setup._looks_like_tailscale_auth_key(value) is False


def test_tailscale_stale_auth_key_emits_rotation_warning(tmp_path: Path) -> None:
    spec = _base_spec()
    spec['network'] = {'provider': 'tailscale', 'tailscale': {'auth_key_env': 'TS_AUTH'}}
    spec['secrets'] = {'tailscale_auth_key': 'stale-placeholder'}
    stale = _build(tmp_path, spec)
    assert stale['env_values']['TS_AUTH'] == 'stale-placeholder'
    assert any('rotate it before deploy' in w for w in stale['warnings'])

    spec['secrets'] = {'tailscale_auth_key': 'tskey-auth-kAbc123CNTRL-zzzzzzzzzz'}
    fresh = _build(tmp_path, spec)
    assert fresh['env_values']['TS_AUTH'] == 'tskey-auth-kAbc123CNTRL-zzzzzzzzzz'
    assert not any('rotate it before deploy' in w for w in fresh['warnings'])


def test_agent_config_validation_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    errors: list[str] = []

    def normalize(target: str, *, port=None):
        if target == 'bad-target':
            raise ValueError('invalid target')
        return target
    monkeypatch.setattr(fleet_setup, 'normalize_ssh_target', normalize)
    agents = fleet_setup._agent_configs({'agents': [{}, {'name': 'dup', 'target': 'host'}, {'name': 'dup', 'target': 'host'}, {'name': 'invalid', 'target': 'bad-target', 'os': 'windows', 'supervisor': 'bad'}]}, hub_name='hub', supervisor='auto', errors=errors, openclaw_defaults={}, worker_defaults={})
    assert len(agents) == 2
    joined = '; '.join(errors)
    assert 'needs a name' in joined
    assert 'duplicate' in joined
    assert 'target invalid' in joined
    assert 'os must' in joined
    assert 'supervisor invalid' in joined


def test_router_network_webdav_and_helper_edges() -> None:
    router, values, required, warnings = fleet_setup._router_env({'providers': [{'id': 'nvidia', 'key': 'secret', 'base_url': 'https://custom.example/v1'}], 'router': {}}, {})
    assert router['providers'].startswith('nvidia=')
    assert values['NVIDIA_API_KEY'] == 'secret'
    assert values['NVIDIA_BASE_URL'] == 'https://custom.example/v1'
    assert not required and (not warnings)
    errors: list[str] = []
    fleet_setup._network_config({'network': {'provider': 'headscale', 'headscale': {}}}, {}, errors)
    assert 'login_server' in errors[0]
    errors = []
    webdav = fleet_setup._webdav_config({'webdav': {'enabled': True, 'dns_name': 'example.com', 'url': 'http://example.com/files'}}, {}, {}, errors)
    assert webdav['enabled'] is True
    assert 'https' in errors[0]
    assert not fleet_setup._valid_dns_name('127.0.0.1')
    assert fleet_setup._normalize_public_path('files') == '/files/'
    assert fleet_setup._host_from_target('user@example.com:2222') == 'example.com'
    assert fleet_setup._status([], [{'status': 'warn'}]) == 'warn'
    assert fleet_setup._list((1, 2)) == [1, 2]
    assert fleet_setup._list('one') == ['one']
    assert fleet_setup._optional_int('5') == 5
    assert fleet_setup._optional_int('bad') is None
