from __future__ import annotations

from mac.openshell_collector import normalize_openshell_event
from mac.openshell_supervisor import build_supervisor_argv
from mac.services import ControlPlane


POLICY = """version: 1
network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: 127.0.0.1
        port: 8789
        protocol: rest
"""


def _agent(cp: ControlPlane):
    # openshell_required is data-driven from the agent's own resources (no
    # hardcoded name allowlist), so the agent record must carry the flag.
    machine = cp.register_machine("rocky", resources={"openshell_required": True})
    return cp.register_agent(
        machine.id, "rocky", capabilities=["python"], resources={"openshell_required": True}
    )


def test_openshell_policy_crud_versions_assignment_and_status():
    cp = ControlPlane.in_memory()
    agent = _agent(cp)

    policy = cp.create_openshell_policy("default", POLICY, created_by="operator")
    assert policy.version == 1
    assert policy.checksum.startswith("sha256:")
    assert policy.parsed_metadata["network_policy_names"] == ["mac_hub"]

    updated = cp.update_openshell_policy(
        policy.id,
        policy_text=POLICY + "\n# comment\n",
        updated_by="operator",
    )
    assert updated.version == 2
    assert [version.version for version in cp.list_openshell_policy_versions(policy.id)] == [2, 1]

    assignment = cp.assign_openshell_policy(policy.id, target_type="agent", target_id=agent.id)
    assert assignment.policy_version == 2
    status = cp.report_openshell_status(
        agent.id,
        status="active",
        sandbox_id="sandbox-1",
        policy_id=policy.id,
        policy_version=2,
        checksum=updated.checksum,
    )
    assert status.status == "active"

    detail = cp.get_openshell_status(agent.id)
    assert detail["required"] is True
    assert detail["effective"]["deployed"] is True
    assert detail["assignment"]["id"] == assignment.id


def test_active_status_with_no_sandbox_id_is_not_deployed_and_fails_closed():
    """Live-found on rocky/natasha/bullwinkle (2026-09-03): retain-forward
    recovery can leave a status row reporting status="active" with
    sandbox_id=null after the sandbox itself never actually came back up.
    effective.deployed must require a real recorded sandbox identity, not
    just the historical status string -- otherwise a required-OpenShell
    agent with no live sandbox is reported as deployed and not fail-closed."""
    cp = ControlPlane.in_memory()
    agent = _agent(cp)

    cp.report_openshell_status(agent.id, status="active", sandbox_id=None)

    detail = cp.get_openshell_status(agent.id)
    assert detail["deployed_status"]["status"] == "active"
    assert detail["deployed_status"]["sandbox_id"] is None
    assert detail["required"] is True
    assert detail["effective"]["deployed"] is False
    assert detail["effective"]["fail_closed"] is True


def test_action_events_project_command_audit_export_and_memory_summary():
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    task = cp.create_task("action task", required_capabilities=["python"])

    cp.record_command_audit(
        agent.id,
        "completed",
        ["pytest", "--token", "secret-value"],
        "/repo",
        task_id=task.id,
        command_id="cmd-1",
        returncode=0,
    )
    events = cp.list_action_events(action_type="command", task_id=task.id)
    assert events
    assert events[0].command_id == "cmd-1"
    assert events[0].outcome == "success"
    assert "<redacted>" in events[0].attributes["argv"]

    exported = cp.export_action_events_otlp(task_id=task.id)
    assert exported["event_count"] >= 1
    assert exported["resourceSpans"][0]["scopeSpans"][0]["spans"]

    summary = cp.summarize_actions_to_memory(agent_id=agent.id, write=True)
    assert summary["memory"]["record_type"] == "action_summary"


def test_openshell_collector_normalizes_denials_and_supervisor_command():
    event = normalize_openshell_event(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "category": "network",
            "operation": "connect",
            "disposition": "denied",
            "policy_id": "ospol_1",
        },
        agent_id="agent_rocky",
        sandbox_id="sandbox-1",
    )
    assert event["outcome"] == "denied"
    assert event["severity"] == "warning"
    assert event["action_type"] == "openshell.network"
    assert event["sandbox_id"] == "sandbox-1"

    argv = build_supervisor_argv(
        agent_id="agent_rocky",
        policy_path="/etc/mac/policy.yaml",
        child_argv=["mac-hermes-gateway"],
        openshell_bin="/usr/bin/openshell",
    )
    assert argv[:5] == [
        "/usr/bin/openshell",
        "sandbox",
        "create",
        "--no-auto-providers",
        "--policy",
    ]
    assert "--name" in argv
    assert argv[argv.index("--") + 1 :] == ["mac-hermes-gateway"]
