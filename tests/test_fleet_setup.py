from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from mac.fleet_setup import build_setup_plan, public_plan


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
