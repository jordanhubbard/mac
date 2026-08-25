from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mac import environment_contract as contract
from mac import fleet_learning, provisioning_service
from mac.models import NotFoundError, ValidationError
from mac.services import ControlPlane


def test_environment_preflight_autodetect_warning_and_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contract, "_detect_command_version", lambda *_a: "invalid")
    monkeypatch.setattr(contract.shutil, "which", lambda _name: None)
    payload = {
        "runtime_versions": {"python_min": "3.11", "pnpm_min": "9"},
        "native_build": {"required": True},
    }
    checked = contract.validate_environment_contract(payload)
    assert checked["preflight"]["status"] == "fail"
    assert any(item["name"] == "c_compiler" for item in checked["preflight"]["checks"])

    warned = contract.validate_environment_contract(
        {
            "runtime_versions": {"python_min": "invalid"},
            "native_build": {"required": False},
        },
        python_version="invalid",
    )
    assert warned["preflight"]["status"] == "warn"
    summary = contract.environment_contract_summary(
        {
            "runtime_versions": {"pnpm_min": "9", "python_min": "3.11"},
            "native_build": {"required": False},
            "egress": {"hosts": []},
            "preflight": {"status": "pass"},
        }
    )
    assert "pnpm>=9" in summary and "Python>=3.11" in summary


def test_environment_file_native_and_version_helper_edges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = tmp_path / "package.json"
    package.write_text("not-json", encoding="utf-8")
    assert contract._read_json(package) is None
    assert contract._read_text_head(tmp_path, 2) == ""
    assert contract._extract_semver_floor("unversioned") is None

    package.write_text(
        json.dumps(
            {
                "packageManager": "npm@10",
                "dependencies": [],
                "devDependencies": {},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-workspace.yaml").write_text(
        "onlyBuiltDependencies:\n  - sharp\n", encoding="utf-8"
    )
    derived = contract.derive_environment_contract(tmp_path)
    assert derived["native_build"]["required"] is True
    package.write_text(
        json.dumps({"name": "edge", "dependencies": ["not-a-mapping"]}),
        encoding="utf-8",
    )
    assert contract._derive_native_build(tmp_path)[0] is True

    monkeypatch.setattr(contract.shutil, "which", lambda _name: None)
    assert contract._detect_command_version("missing") is None
    monkeypatch.setattr(contract.shutil, "which", lambda _name: "/bin/tool")
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("failed")),
    )
    assert contract._detect_command_version("tool") is None
    assert contract._check_version_floor("x", required="1", detected=None)["status"] == "fail"
    assert (
        contract._check_version_floor("x", required="invalid", detected="invalid")["status"]
        == "warn"
    )
    with pytest.raises(ValueError, match="no digits"):
        contract._version_tuple("invalid")

    hosts: set[str] = set()
    contract._extract_lockfile_hosts(tmp_path, hosts)
    assert hosts == set()
    assert contract._is_internal_host("localhost")
    assert contract._is_internal_host("service.internal")
    assert contract._is_internal_host("10.0.0.1")


def test_fleet_learning_remote_transport_failure_and_payload_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert fleet_learning.repository_host("file:///tmp/repo") == "local"
    assert fleet_learning.repository_host("not-a-remote") == ""
    assert fleet_learning.repository_transport("") == "unknown"
    assert fleet_learning.repository_transport("git@example.com:org/repo.git") == "ssh"
    assert fleet_learning.repository_transport("./repo") == "local"
    assert fleet_learning.repository_transport("other") == "unknown"

    assert (
        fleet_learning._credential_source_for_http("https://user@example.com/repo", {})
        == "embedded"
    )
    assert fleet_learning._credential_source_for_http("not a url", {}) == "ambient:https"

    monkeypatch.setattr(
        fleet_learning,
        "detect_host",
        lambda _remote: (_ for _ in ()).throw(ValueError("unknown")),
    )
    assert (
        fleet_learning._credential_source_for_http("https://example.invalid/repo", {})
        == "ambient:https"
    )

    assert (
        fleet_learning.resolve_git_remote_access(
            "git@example.com:org/repo.git", environ={}
        ).credential_source
        == "ssh-agent-or-key"
    )
    assert (
        fleet_learning.resolve_git_remote_access("/tmp/repo", environ={}).credential_source
        == "local"
    )
    assert (
        fleet_learning.resolve_git_remote_access(
            "git://example.com/repo", environ={}
        ).credential_source
        == "anonymous"
    )
    assert (
        fleet_learning.resolve_git_remote_access("other", environ={}).credential_source
        == "ambient:unknown"
    )

    assert fleet_learning.classify_repository_access_failure("permission denied") == "authorization"
    assert (
        fleet_learning.classify_repository_access_failure("repository not found")
        == "repository_missing"
    )
    assert fleet_learning.classify_repository_access_failure("connection refused") == "network"
    assert fleet_learning.classify_repository_access_failure("non-fast-forward") == "conflict"
    assert "recent successful peer" in fleet_learning._recommendation(
        outcome="failure",
        failure_class="other",
        credential_source="unknown",
        host="example.com",
        operation="push",
    )
    with pytest.raises(ValueError, match="agent_id"):
        fleet_learning.build_repository_access_memory_payload({})


def test_fleet_learning_parse_time_and_state_filter_edges() -> None:
    learning = fleet_learning.build_repository_access_learning(
        project="project",
        remote="https://example.com/repo",
        operation="push",
        agent_id="agent",
        outcome="success",
        credential_source="ambient:https",
        at="2026-01-01T00:00:00",
    )
    assert fleet_learning.parse_repository_access_learning(learning) == learning
    invalid_kind = {**learning, "kind": "other"}
    invalid_outcome = {**learning, "outcome": "maybe"}
    assert fleet_learning.parse_repository_access_learning(invalid_kind) is None
    assert fleet_learning.parse_repository_access_learning(invalid_outcome) is None
    assert fleet_learning._parse_timestamp("") is None
    assert fleet_learning._parse_timestamp("not-a-date") is None
    assert fleet_learning._parse_timestamp("2026-01-01T00:00:00").tzinfo is not None

    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    records = [
        {"content": "not-json", "created_at": now.isoformat()},
        {"content": {**learning, "project": "other"}, "created_at": now.isoformat()},
        {"content": {**learning, "repository_host": "other"}, "created_at": now.isoformat()},
        {"content": {**learning, "operation": "fetch"}, "created_at": now.isoformat()},
        {
            "content": learning,
            "created_at": (now - timedelta(days=10)).isoformat(),
        },
    ]
    state, latest = fleet_learning.repository_access_state(
        records,
        project="project",
        host="example.com",
        operation="push",
        failure_cooldown_seconds=60,
        success_ttl_seconds=1,
        now=now,
    )
    assert state == "unknown" and latest is not None


def test_provisioning_command_inventory_filters_and_lifecycle_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert provisioning_service._metadata_string_list(" command ") == ["command"]
    assert provisioning_service._metadata_string_list(" ") == []
    names = provisioning_service._agent_resource_command_names(
        {
            "commands": {
                "available": ["python"],
                "commands": ["git", {"name": "rg"}, {}, None],
                "paths": {"make": "/usr/bin/make", "": "ignored"},
            },
            "command_inventory": ["node", {"name": "pnpm"}, {}, None],
        }
    )
    assert names == {"python", "git", "rg", "make", "node", "pnpm"}

    cp = ControlPlane.in_memory()
    cp.register_tenant("tenant", tenant_id="tenant")
    request = cp.provisioning.request_agent(
        reason="need tools",
        role_slug="qa",
        tenant_id="tenant",
        detail={"required_commands": ["python"]},
    )
    assert cp.provisioning.list_requests(role_slug="qa", tenant_id="tenant")[0].id == request.id
    assert cp.provisioning.fail_request(request.id, reason="unavailable").status == "failed"

    machine = cp.register_machine("host")
    agent = cp.register_agent(machine.id, "agent", capabilities=[])
    role_request = cp.provisioning.request_agent(reason="need role", role_slug="qa")
    with pytest.raises(ValidationError, match="no assigned role"):
        cp.provisioning._assert_agent_matches_request(role_request, agent.id)
    with pytest.raises(NotFoundError, match="agent not found"):
        cp.provisioning._assert_agent_matches_request(role_request, "missing")

    command_request = cp.provisioning.request_agent(
        reason="need command", detail={"required_commands": ["python"]}
    )
    with pytest.raises(NotFoundError, match="agent not found"):
        cp.provisioning._assert_agent_commands_match_request(command_request, "missing")

    no_caps = cp.provisioning.request_agent(reason="bad caps")
    original = provisioning_service.json_loads

    def fail_json(value, default):
        if default == []:
            raise ValueError("bad")
        return original(value, default)

    monkeypatch.setattr(provisioning_service, "json_loads", fail_json)
    cp.provisioning._assert_agent_matches_request(no_caps, agent.id)


def test_provisioning_row_without_requested_by_is_backward_compatible() -> None:
    cp = ControlPlane.in_memory()
    row = {
        "id": "request",
        "status": "pending",
        "reason": "reason",
        "role_slug": None,
        "capabilities": "[]",
        "hardware": "{}",
        "task_id": None,
        "tenant_id": None,
        "detail": "{}",
        "fulfilled_agent_id": None,
        "created_at": "now",
        "updated_at": "now",
        "closed_at": None,
    }
    assert cp.provisioning._from_row(row).requested_by is None
