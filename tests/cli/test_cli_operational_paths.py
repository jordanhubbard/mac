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
                        "agents": [{"name": "target-hub", "target": "mac@target", "os": "linux"}],
                    },
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


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
    assert (
        cli.main(
            [
                "admin",
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
        )
        == 0
    )
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
    assert (
        cli.main(
            [
                "admin",
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
        )
        == 0
    )
    assert '"to_apply"' in capsys.readouterr().out


def test_fleet_soul_setup_and_push_fail_closed(tmp_path, monkeypatch):
    from mac import soul_snapshot

    empty = tmp_path / "empty.yaml"
    empty.write_text("fleets: {}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="no fleet found"):
        cli.main(
            [
                "admin",
                "fleet",
                "soul-pull",
                "--into",
                str(tmp_path / "out"),
                "--fleets-config",
                str(empty),
            ]
        )

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
            ["admin", "fleet", "soul-push", "--from", str(source), "--fleets-config", str(registry)]
        )

    monkeypatch.setattr(
        soul_snapshot, "load_fleet_agents", lambda *_args: [("worker", "mac@worker")]
    )
    with pytest.raises(SystemExit, match="snapshot has no fleet"):
        (source / "manifest.yaml").write_text("agents: {}\n", encoding="utf-8")
        cli.main(
            ["admin", "fleet", "soul-push", "--from", str(source), "--fleets-config", str(registry)]
        )


def test_fleet_soul_audit_happy_path(tmp_path, monkeypatch, capsys):
    from mac import soul_snapshot

    registry = _registry(tmp_path)

    audit_result = {
        "schema": "mac.hermes_salvage_audit.v1",
        "agent": "rocky",
        "target": "mac@source",
        "audited_at": "20260101T000000Z",
        "files": ["SOUL.md", "USER.md"],
        "file_count": 2,
        "error": None,
    }
    monkeypatch.setattr(soul_snapshot, "hermes_salvage_audit", lambda *_a, **_kw: audit_result)

    result = cli.main(
        [
            "admin",
            "fleet",
            "soul-audit",
            "--agent",
            "rocky",
            "--fleet",
            "source",
            "--fleets-config",
            str(registry),
        ]
    )
    assert result == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["agent"] == "rocky"


def test_fleet_soul_audit_unknown_agent_raises(tmp_path, monkeypatch):
    from mac import soul_snapshot

    registry = _registry(tmp_path)

    with pytest.raises(SystemExit, match="not found in fleet"):
        cli.main(
            [
                "admin",
                "fleet",
                "soul-audit",
                "--agent",
                "no-such-agent",
                "--fleet",
                "source",
                "--fleets-config",
                str(registry),
            ]
        )
