from __future__ import annotations

from pathlib import Path

import pytest

from mac.models import AuthorizationError, NotFoundError, ValidationError
from mac.openshell_service import OpenShellService, parse_policy_metadata
from mac.services import ControlPlane


def _agents(cp: ControlPlane):
    machine = cp.register_machine("host")
    return (
        cp.register_agent(machine.id, "sender", capabilities=[]),
        cp.register_agent(machine.id, "recipient", capabilities=[]),
        cp.register_agent(machine.id, "other", capabilities=[]),
    )


def test_agentbus_open_append_close_and_lookup_edges() -> None:
    cp = ControlPlane.in_memory()
    sender, recipient, other = _agents(cp)
    task = cp.create_task("Bus task")
    stream = cp.agentbus.open_stream(
        sender.id,
        recipient.id,
        task_id=task.id,
        stream_id="stream-1",
    )
    with pytest.raises(ValidationError, match="already exists"):
        cp.agentbus.open_stream(sender.id, recipient.id, stream_id="stream-1")
    with pytest.raises(NotFoundError, match="stream not found"):
        cp.agentbus.append_chunk("missing", sender.id, {"x": 1})
    with pytest.raises(AuthorizationError, match="only the stream sender"):
        cp.agentbus.append_chunk(stream.id, other.id, {"x": 1})
    with pytest.raises(AuthorizationError, match="only the stream sender"):
        cp.agentbus.close_stream(stream.id, other.id)
    with pytest.raises(ValidationError, match="cannot be open"):
        cp.agentbus.close_stream(stream.id, sender.id, status="open")
    with pytest.raises(ValidationError, match="unsupported"):
        cp.agentbus.close_stream(stream.id, sender.id, status="unknown")

    closed = cp.agentbus.close_stream(stream.id, sender.id, status="closed")
    assert cp.agentbus.close_stream(stream.id, sender.id, status="closed") == closed
    with pytest.raises(ValidationError, match="already closed"):
        cp.agentbus.close_stream(stream.id, sender.id, status="aborted")
    with pytest.raises(NotFoundError, match="chunk not found"):
        cp.agentbus.get_chunk("missing")
    with pytest.raises(ValidationError, match="unsupported"):
        cp.agentbus.list_streams(status="unknown")
    with pytest.raises(NotFoundError, match="task not found"):
        cp.agentbus._require_task("missing")


def test_agentbus_validation_and_payload_serialization_edges() -> None:
    cp = ControlPlane.in_memory()
    for content_type in ["", "bad content type"]:
        with pytest.raises(ValidationError):
            cp.agentbus._validate_content_type(content_type)
    for topic in ["", "bad topic?"]:
        with pytest.raises(ValidationError):
            cp.agentbus._validate_topic(topic)
    with pytest.raises(ValidationError, match="unsupported"):
        cp.agentbus._serialize_payload("x", "binary")
    with pytest.raises(ValidationError, match="must be a string"):
        cp.agentbus._serialize_payload({}, "text")
    with pytest.raises(ValidationError, match="base64 payload is invalid"):
        cp.agentbus._serialize_payload("not-base64", "base64")
    with pytest.raises(ValidationError, match="JSON serializable"):
        cp.agentbus._serialize_payload({1, 2}, "json")


def _policy_text() -> str:
    return "version: 1\nnetwork_policies: {}\nfilesystem_policy: {}\n"


def test_openshell_policy_validation_assignment_and_materialization(
    tmp_path: Path,
) -> None:
    cp = ControlPlane.in_memory()
    sender, _, _ = _agents(cp)
    with pytest.raises(ValidationError, match="invalid OpenShell policy YAML"):
        parse_policy_metadata("root: [unterminated")
    with pytest.raises(ValidationError, match="YAML object"):
        parse_policy_metadata("- item\n")
    with pytest.raises(NotFoundError, match="policy not found"):
        cp.openshell.get_policy("missing")
    with pytest.raises(ValidationError, match="name is required"):
        cp.openshell.create_policy("", _policy_text())
    with pytest.raises(ValidationError, match="name is too long"):
        cp.openshell.create_policy("x" * 121, _policy_text())
    with pytest.raises(ValidationError, match="policy text is required"):
        cp.openshell.create_policy("policy", "")

    policy = cp.openshell.create_policy("policy", _policy_text())
    rendered = cp.openshell.render_policy(policy.id, hub_host="hub.example")
    assert rendered["schema"] == "mac.openshell.policy.render.v1"
    with pytest.raises(ValidationError, match="target_type"):
        cp.openshell.assign_policy(policy.id, target_type="unknown", target_id="x")
    with pytest.raises(ValidationError, match="target_id"):
        cp.openshell.assign_policy(policy.id, target_type="host", target_id="")
    with pytest.raises(NotFoundError, match="assignment not found"):
        cp.openshell.get_assignment("missing")
    with pytest.raises(NotFoundError, match="no OpenShell policy assigned"):
        cp.openshell.materialize_assigned_policy(sender.id, tmp_path / "none.yaml")

    cp.openshell.assign_policy(policy.id, target_type="agent", target_id=sender.id)
    target = tmp_path / "policy" / "active.yaml"
    result = cp.openshell.materialize_assigned_policy(sender.id, target)
    assert target.read_text(encoding="utf-8") == _policy_text().strip()
    assert result["policy_id"] == policy.id
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValidationError, match="unsupported OpenShell status"):
        cp.openshell.report_agent_status(sender.id, status="invalid")
    assert cp.openshell.agent_requires_openshell("missing-agent") in {True, False}


def test_openshell_file_fallback_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cp = ControlPlane.in_memory()
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(_policy_text(), encoding="utf-8")
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(explicit))
    assert cp.openshell.file_fallback_policy() == explicit

    monkeypatch.delenv("MAC_OPENSHELL_POLICY")
    home = tmp_path / "home"
    deployed = home / ".mac" / "openshell-policy.yaml"
    deployed.parent.mkdir(parents=True)
    deployed.write_text(_policy_text(), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    assert cp.openshell.file_fallback_policy() == deployed

    deployed.unlink()
    bundled = cp.openshell.file_fallback_policy()
    assert bundled is not None and bundled.name == "default-policy.yaml"


def test_openshell_status_write_missing_row_is_reported() -> None:
    class Store:
        def query_one(self, *_args):
            return None

        def execute(self, *_args) -> None:
            return None

    service = OpenShellService(Store(), get_agent=lambda _agent: object())
    with pytest.raises(NotFoundError, match="status not found"):
        service.report_agent_status("agent", status="active", required=False)
