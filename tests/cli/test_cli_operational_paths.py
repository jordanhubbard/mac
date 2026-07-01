"""Behavioral CLI coverage for host-moving and fleet-state workflows."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mac import cli
from mac.models import MACError


def _registry(tmp_path: Path) -> Path:
    identity = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("private-key-placeholder", encoding="utf-8")
    known_hosts.write_text("host ssh-ed25519 key", encoding="utf-8")
    path = tmp_path / "fleets.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "fleets": {
                    "source": {
                        "fleet_name": "source-name",
                        "hub_url": "http://source.internal:8789",
                        "hub_agent": "rocky",
                        "shared_services_manager_agent": "rocky",
                        "defaults": {
                            "identity_file": str(identity),
                            "ssh_known_hosts_file": str(known_hosts),
                            "ssh_host_key_policy": "strict",
                        },
                        "agents": [
                            {"name": "rocky", "target": "mac@source", "os": "linux"},
                            {"name": "worker", "target": "mac@worker", "os": "linux"},
                        ],
                    },
                    "target": {
                        "fleet_name": "target-name",
                        "hub_url": "http://target.internal:8789",
                        "hub_agent": "target-hub",
                        "defaults": {
                            "identity_file": str(identity),
                            "ssh_known_hosts_file": str(known_hosts),
                            "ssh_host_key_policy": "strict",
                        },
                        "agents": [
                            {"name": "target-hub", "target": "mac@target", "os": "linux"}
                        ],
                    },
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_agent_migrate_dry_run_and_execute(tmp_path, monkeypatch, capsys):
    from mac import agent_migrate
    from mac import hermes_config_surface

    registry = _registry(tmp_path)
    monkeypatch.setattr(hermes_config_surface, "registry_path", lambda: registry)
    assert cli.main(
        [
            "agent",
            "migrate",
            "rocky",
            "--to",
            "mac@destination:2222",
            "--to-os",
            "darwin",
            "--to-proxy-jump",
            "jump@bastion:22",
            "--to-identity-file",
            str(tmp_path / "id_ed25519"),
            "--to-known-hosts-file",
            str(tmp_path / "known_hosts"),
            "--to-host-key-policy",
            "strict",
            "--keep-source",
            "--retire-source-agent",
            "agent_old",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "HUB migration" in output
    assert "migration plan" in output

    monkeypatch.setattr(
        agent_migrate,
        "execute_migration",
        lambda name, steps: {"agent": name, "ok": True, "steps": len(steps)},
    )
    assert cli.main(
        [
            "agent",
            "migrate",
            "worker",
            "--fleet",
            "source",
            "--from",
            "mac@override-source:2200",
            "--to",
            "mac@destination",
            "--to-os",
            "linux",
            "--src-os",
            "darwin",
            "--no-hub",
            "--execute",
        ]
    ) == 0
    assert "retargeted worker" in capsys.readouterr().out
    updated = yaml.safe_load(registry.read_text(encoding="utf-8"))
    worker_entry = next(
        item for item in updated["fleets"]["source"]["agents"] if item["name"] == "worker"
    )
    assert worker_entry["target"] == "mac@destination"
    assert list(tmp_path.glob("fleets.yaml.bak.*"))


def test_agent_migrate_reports_registry_and_route_errors(tmp_path, monkeypatch):
    from mac import hermes_config_surface

    registry = _registry(tmp_path)
    monkeypatch.setattr(hermes_config_surface, "registry_path", lambda: registry)
    with pytest.raises(SystemExit, match="not found in any fleet"):
        cli.main(["agent", "migrate", "missing", "--to", "mac@destination"])
    with pytest.raises(SystemExit, match="not in fleet"):
        cli.main(
            ["agent", "migrate", "missing", "--fleet", "source", "--to", "mac@destination"]
        )

    data = yaml.safe_load(registry.read_text(encoding="utf-8"))
    worker = next(
        item for item in data["fleets"]["source"]["agents"] if item["name"] == "worker"
    )
    worker["target"] = ""
    registry.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="no source target"):
        cli.main(
            ["agent", "migrate", "worker", "--fleet", "source", "--to", "mac@destination"]
        )

    with pytest.raises(SystemExit, match="could not resolve migration SSH routes"):
        cli.main(
            [
                "agent",
                "migrate",
                "worker",
                "--fleet",
                "source",
                "--from",
                "mac@source",
                "--to",
                "mac@destination",
                "--to-identity-file",
                "",
            ]
        )


def test_fleet_move_agent_dry_run_success_idempotence_and_failure(
    tmp_path, monkeypatch, capsys
):
    from mac import fleet_move, hermes_config_surface

    registry = _registry(tmp_path)
    monkeypatch.setattr(hermes_config_surface, "registry_path", lambda: registry)
    assert cli.main(["fleet", "move-agent", "--agent", "worker", "--to", "target-name"]) == 0
    output = capsys.readouterr().out
    assert "auto-detected source fleet" in output
    assert "move-agent plan" in output.lower()

    results = iter(
        [
            {
                "ok": True,
                "backup": "/tmp/backup",
                "registry_written": str(registry),
                "redeployed": True,
                "target_hub_url": "http://target.internal:8789",
                "db_reconcile": "updated",
                "next_steps": ["verify"],
            },
            {"ok": True, "idempotent": True, "message": "already moved"},
            {
                "ok": False,
                "registry_written": str(registry),
                "backup": "/tmp/backup",
                "redeploy_returncode": 1,
                "redeploy_cmd": "deploy now",
                "error": "deploy failed",
            },
            {"ok": False, "error": "validation failed"},
        ]
    )
    monkeypatch.setattr(fleet_move, "execute_fleet_move", lambda *_args, **_kwargs: next(results))

    base = [
        "fleet",
        "move-agent",
        "--agent",
        "worker",
        "--from",
        "source-name",
        "--to",
        "target-name",
        "--hub-url",
        "http://override.internal:8789",
        "--no-db-reconcile",
        "--no-redeploy",
        "--execute",
    ]
    assert cli.main(base) == 0
    output = capsys.readouterr().out
    assert "resolved fleets" in output and "registry backed up" in output
    assert "next: verify" in output
    assert cli.main(base) == 0
    assert "already moved" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="deploy failed"):
        cli.main(base)
    assert "redeploy FAILED" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="validation failed"):
        cli.main(base)


def test_fleet_move_agent_validates_source_target_and_hub(tmp_path, monkeypatch):
    from mac import hermes_config_surface

    registry = _registry(tmp_path)
    monkeypatch.setattr(hermes_config_surface, "registry_path", lambda: registry)
    with pytest.raises(SystemExit, match="source fleet"):
        cli.main(
            ["fleet", "move-agent", "--agent", "worker", "--from", "missing", "--to", "target"]
        )
    with pytest.raises(SystemExit, match="target fleet"):
        cli.main(["fleet", "move-agent", "--agent", "worker", "--to", "missing"])
    with pytest.raises(SystemExit, match="not found in any fleet"):
        cli.main(["fleet", "move-agent", "--agent", "missing", "--to", "target"])

    data = yaml.safe_load(registry.read_text(encoding="utf-8"))
    data["fleets"]["target"]["hub_url"] = ""
    registry.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="has no hub_url"):
        cli.main(["fleet", "move-agent", "--agent", "worker", "--to", "target"])


def test_fleet_soul_pull_and_push_use_current_routes(tmp_path, monkeypatch, capsys):
    from mac import hermes_config_surface, soul_snapshot

    registry = _registry(tmp_path)
    destination = tmp_path / "snapshot"
    destination.mkdir()
    monkeypatch.setattr(hermes_config_surface, "registry_path", lambda: registry)

    def pull(agents, dest, transport, **kwargs):
        assert len(transport._routes) == len(agents)
        return {
            "fleet": kwargs["fleet"],
            "agents": {
                name: {
                    "target": target,
                    "files": {"SOUL.md": {"present": True}},
                    "memory": {"memory.db": {"present": True, "bytes": 12}},
                }
                for name, target in agents
            },
        }

    monkeypatch.setattr(soul_snapshot, "pull_snapshot", pull)
    monkeypatch.setattr(
        soul_snapshot,
        "capture_hub_state",
        lambda _hub, ids, _dest, **_kw: {
            "agents": {
                name: {"persona": {"present": True}, "mood": {"present": False}}
                for name, _agent_id in ids
            }
        },
    )

    class Hub:
        def list_agents(self):
            return [{"name": "rocky", "id": "agent_rocky"}]

    monkeypatch.setattr(cli, "_plane", lambda _args: Hub())
    assert cli.main(
        [
            "fleet",
            "soul-pull",
            "--fleet",
            "source",
            "--into",
            str(destination),
            "--fleets-config",
            str(registry),
            "--memory-checksum",
            "--with-hub",
        ]
    ) == 0
    assert (destination / "manifest.yaml").is_file()
    assert '"hub"' in capsys.readouterr().out

    change = SimpleNamespace(
        agent="rocky",
        relpath="SOUL.md",
        status="changed",
        applied=False,
        backup_path=None,
    )
    monkeypatch.setattr(
        soul_snapshot,
        "plan_and_push",
        lambda *_args, **kwargs: SimpleNamespace(
            dry_run=kwargs["dry_run"], changes=[change], to_apply=[change]
        ),
    )
    assert cli.main(
        [
            "fleet",
            "soul-push",
            "--from",
            str(destination),
            "--fleets-config",
            str(registry),
            "--dry-run",
            "--agent",
            "rocky",
        ]
    ) == 0
    assert '"to_apply"' in capsys.readouterr().out


def test_fleet_soul_setup_and_push_fail_closed(tmp_path, monkeypatch):
    from mac import soul_snapshot

    empty = tmp_path / "empty.yaml"
    empty.write_text("fleets: {}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="no fleet found"):
        cli.main(["fleet", "soul-pull", "--into", str(tmp_path / "out"), "--fleets-config", str(empty)])

    registry = _registry(tmp_path)
    source = tmp_path / "snapshot"
    source.mkdir()
    (source / "manifest.yaml").write_text(
        yaml.safe_dump(
            {"fleet": "source", "agents": {"removed": {"target": "mac@old", "files": {}}}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="no longer present"):
        cli.main(
            ["fleet", "soul-push", "--from", str(source), "--fleets-config", str(registry)]
        )

    monkeypatch.setattr(soul_snapshot, "load_fleet_agents", lambda *_args: [("worker", "mac@worker")])
    with pytest.raises(SystemExit, match="snapshot has no fleet"):
        (source / "manifest.yaml").write_text("agents: {}\n", encoding="utf-8")
        cli.main(
            ["fleet", "soul-push", "--from", str(source), "--fleets-config", str(registry)]
        )


def test_sender_agent_id_requires_explicit_control_identity(monkeypatch):
    monkeypatch.delenv("MAC_AGENT_ID", raising=False)
    monkeypatch.delenv("MAC_WORKER_AGENT_ID", raising=False)
    with pytest.raises(MACError, match="sender agent id"):
        cli._sender_agent_id(SimpleNamespace(sender_agent_id=None))
    monkeypatch.setenv("MAC_WORKER_AGENT_ID", "agent_worker")
    assert cli._sender_agent_id(SimpleNamespace(sender_agent_id=None)) == "agent_worker"
