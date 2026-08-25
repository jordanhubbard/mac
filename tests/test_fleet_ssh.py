from __future__ import annotations

import json

import pytest

from mac import fleet_ssh


REGISTRY = {
    "version": 1,
    "fleets": {
        "rocky": {
            "fleet_name": "mac",
            "hub_agent": "hub",
            "control_port": 8789,
            "defaults": {
                "ssh_jump": "jump@bastion:2222",
                "identity_file": "~/.ssh/fleet-default",
                "ssh_known_hosts_file": "~/.ssh/fleet-known-hosts",
                "ssh_host_key_policy": "strict",
                "supervisor": "systemd",
            },
            "agents": [
                {"name": "hub", "target": "ops@hub.example:2201", "os": "linux"},
                {
                    "name": "worker",
                    "target": "worker.example",
                    "os": "darwin",
                    "ssh_port": 2223,
                    "ssh_jump": "worker-jump@example",
                    "identity_file": "~/.ssh/worker",
                    "ssh_host_key_policy": "accept-new",
                },
            ],
        }
    },
}


def test_resolve_hub_route_is_explicit_and_secret_free(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    spec = fleet_ssh.resolve_fleet_ssh(REGISTRY, "mac")

    assert spec.fleet == "rocky"
    assert spec.agent == "hub"
    assert spec.target == "ops@hub.example"
    assert spec.port == 2201
    assert spec.proxy_jump == "jump@bastion:2222"
    assert spec.identity_file == str(tmp_path / ".ssh" / "fleet-default")
    assert spec.known_hosts_file == str(tmp_path / ".ssh" / "fleet-known-hosts")
    serialized = json.dumps(spec.to_dict()).lower()
    assert "private_key" not in serialized
    assert "token" not in serialized


def test_per_agent_values_override_fleet_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    spec = fleet_ssh.resolve_fleet_ssh(REGISTRY, "rocky", "worker")

    assert spec.port == 2223
    assert spec.proxy_jump == "worker-jump@example"
    assert spec.identity_file == str(tmp_path / ".ssh" / "worker")
    assert spec.host_key_policy == "accept-new"
    assert spec.known_hosts_file == str(tmp_path / ".ssh" / "fleet-known-hosts")


def test_mixed_topology_routes_lan_direct_and_gke_through_bastion():
    config = {
        "fleets": {
            "mixed": {
                "hub_agent": "lan",
                "defaults": {"ssh_host_key_policy": "strict"},
                "agents": [
                    {
                        "name": "lan",
                        "target": "ops@lan.local",
                        "identity_file": "~/.ssh/lan",
                        "ssh_known_hosts_file": "~/.ssh/lan-known-hosts",
                    },
                    {
                        "name": "gke",
                        "target": "horde@gke.svc.cluster.local",
                        "ssh_jump": "horde@bastion:2222",
                        "identity_file": "~/.ssh/gke",
                        "ssh_known_hosts_file": "~/.ssh/gke-known-hosts",
                    },
                ],
            }
        }
    }

    lan = fleet_ssh.resolve_fleet_ssh(config, "mixed", "lan", portable=True)
    gke = fleet_ssh.resolve_fleet_ssh(config, "mixed", "gke", portable=True)

    assert lan.proxy_jump is None
    assert gke.proxy_jump == "horde@bastion:2222"
    assert "ProxyJump=horde@bastion:2222" in fleet_ssh.ssh_argv(gke)
    assert not any("ProxyJump" in value for value in fleet_ssh.ssh_argv(lan))


def test_ssh_and_scp_argv_ignore_ambient_ssh_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = fleet_ssh.resolve_fleet_ssh(REGISTRY, "rocky")

    ssh = fleet_ssh.ssh_argv(spec, "echo ok")
    scp = fleet_ssh.scp_argv(spec, ["a"], "ops@hub.example:/tmp/a")

    assert ssh[:3] == ["ssh", "-F", "/dev/null"]
    assert "StrictHostKeyChecking=yes" in ssh
    assert "UserKnownHostsFile=%s" % (tmp_path / ".ssh" / "fleet-known-hosts") in ssh
    assert "IdentitiesOnly=yes" in ssh
    assert "ProxyJump=jump@bastion:2222" in ssh
    assert ssh[-2:] == ["ops@hub.example", "echo ok"]
    assert "-P" in scp and "2201" in scp


def test_legacy_false_host_key_boolean_maps_to_tofu_not_insecure():
    config = json.loads(json.dumps(REGISTRY))
    defaults = config["fleets"]["rocky"]["defaults"]
    defaults.pop("ssh_host_key_policy")
    defaults["ssh_strict_host_key_checking"] = False

    spec = fleet_ssh.resolve_fleet_ssh(config, "rocky")

    assert spec.host_key_policy == "accept-new"
    argv = fleet_ssh.ssh_argv(spec)
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "UserKnownHostsFile=/dev/null" not in argv


def test_portable_validation_rejects_ambient_identity_and_host_keys():
    config = {
        "fleets": {
            "one": {
                "hub_agent": "hub",
                "agents": [{"name": "hub", "target": "hub.example"}],
            }
        }
    }

    with pytest.raises(fleet_ssh.FleetSshError, match="explicit identity"):
        fleet_ssh.resolve_fleet_ssh(config, "one", portable=True)


def test_module_cli_emits_nul_delimited_route(tmp_path, capsysbinary):
    path = tmp_path / "fleets.yaml"
    path.write_text(
        "fleets:\n  one:\n    hub_agent: hub\n    agents:\n      - name: hub\n        target: ops@hub.example:2201\n",
        encoding="utf-8",
    )

    rc = fleet_ssh.main(["--config", str(path), "--fleet", "one", "--kind", "ssh", "--nul"])

    assert rc == 0
    values = capsysbinary.readouterr().out.rstrip(b"\0").split(b"\0")
    assert values[-1] == b"ops@hub.example"
    assert b"-F" in values
