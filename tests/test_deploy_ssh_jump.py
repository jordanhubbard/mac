"""Canonical SSH routing for the shell fleet deploy.

The deploy consumes NUL-delimited argv from :mod:`mac.fleet_ssh`, so per-agent
ports, jumps, identities, and host-key policy work without ambient ssh config.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SCRIPT = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")


def test_fleet_setup_persists_ssh_jump_into_defaults():
    from mac.fleet_setup import build_setup_plan

    spec = {
        "schema": "mac.fleet_setup.v1", "fleet_name": "jordanh-gke", "hub": "jordanh-hub",
        "hub_url": "http://jordanh-hub:8789", "supervisor": "supervisord",
        "ssh_jump": "horde@bastion.horde-gke.nvidia.com:2222",
        "ssh_strict_host_key_checking": False,
        "identity_file": "~/.ssh/gke-operator",
        "ssh_known_hosts_file": "~/.ssh/gke-known-hosts",
        "router": {"backend": "inproc", "providers": [{"id": "nvidia"}]},
        "agents": [{"name": "jordanh-hub", "target": "horde@jordanh-hub", "os": "linux", "supervisor": "supervisord"}],
        "deploy_agents": ["jordanh-hub"],
    }
    plan = build_setup_plan(spec, root=ROOT, fleets_config=Path("/tmp/_x.yaml"), env_file=Path("/tmp/_x.env"))
    d = plan["fleet_config"]["defaults"]
    assert plan["errors"] == []
    assert d["ssh_jump"] == "horde@bastion.horde-gke.nvidia.com:2222"
    assert d["ssh_strict_host_key_checking"] is False
    assert d["ssh_host_key_policy"] == "accept-new"
    assert d["identity_file"] == "~/.ssh/gke-operator"
    assert d["ssh_known_hosts_file"] == "~/.ssh/gke-known-hosts"


def test_default_setup_keeps_strict_on_and_jump_empty():
    from mac.fleet_setup import build_setup_plan

    spec = {
        "schema": "mac.fleet_setup.v1", "fleet_name": "f", "hub": "h", "hub_url": "http://h:8789",
        "router": {"backend": "inproc", "providers": [{"id": "nvidia"}]},
        "agents": [{"name": "h", "target": "u@h", "os": "linux"}], "deploy_agents": ["h"],
    }
    d = build_setup_plan(spec, root=ROOT, fleets_config=Path("/tmp/_y.yaml"), env_file=Path("/tmp/_y.env"))["fleet_config"]["defaults"]
    assert d["ssh_jump"] == "" and d["ssh_strict_host_key_checking"] is True


def test_deploy_script_wires_proxyjump():
    assert "fleet_ssh_route_args" in SCRIPT
    assert "-m mac.fleet_ssh" in SCRIPT
    assert '--kind "$kind"' in SCRIPT
    assert "--nul" in SCRIPT
    assert 'ssh_target_args "$agent"' in SCRIPT
    assert "pinned_fleet_route_args" in SCRIPT
    assert '-S "$control_path" -O proxy' in SCRIPT
    assert "fenced_remote_upload" in SCRIPT
    assert "scp -O" not in SCRIPT
    assert "$SSH_CONN_OPTS" not in SCRIPT


def test_shared_resolver_emits_proxyjump_and_safe_host_key_policy(monkeypatch, tmp_path):
    from mac.fleet_ssh import resolve_fleet_ssh, route_argv

    monkeypatch.setenv("HOME", str(tmp_path))
    config = {
        "fleets": {
            "gke": {
                "hub_agent": "hub",
                "defaults": {
                    "ssh_jump": "horde@bastion:2222",
                    "ssh_strict_host_key_checking": False,
                },
                "agents": [{"name": "hub", "target": "horde@hub"}],
            }
        }
    }
    argv = route_argv(resolve_fleet_ssh(config, "gke"))

    assert "ProxyJump=horde@bastion:2222" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "StrictHostKeyChecking=no" not in argv
