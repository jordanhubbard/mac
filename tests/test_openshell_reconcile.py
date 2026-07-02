from __future__ import annotations

import pytest

from mac.openshell_reconcile import (
    fleet_agent_names,
    reconcile_openshell_agents,
)
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

POLICY_UPDATED = """version: 1
network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: 127.0.0.1
        port: 8789
        protocol: rest
  github:
    name: github
    endpoints:
      - host: github.com
        port: 443
        protocol: rest
"""


def _agent(cp: ControlPlane, name: str = "rocky"):
    machine = cp.register_machine("host-%s" % name)
    return cp.register_agent(
        machine.id,
        name,
        capabilities=["python", "ops"],
        resources={
            "hardware": {"accelerator": "gpu"},
            "commands": {"available": ["git", "python3"]},
        },
        agent_id="agent_%s" % name,
    )


def test_openshell_reconcile_dry_run_does_not_mutate_agent_resources():
    cp = ControlPlane.in_memory()
    agent = _agent(cp)

    out = reconcile_openshell_agents(
        cp,
        agent_selectors=[agent.name],
        policy_text=POLICY,
        apply=False,
        validated=True,
    )

    refreshed = cp.get_agent(agent.id)
    assert "openshell_required" not in refreshed.resources
    assert out["dry_run"] is True
    assert out["policy"]["action"] == "create"
    assert out["agents"][0]["actions"] == [
        "set_resources.openshell_required",
        "assign_policy",
        "report_status",
    ]


def test_openshell_reconcile_apply_preserves_resources_and_reports_active_status():
    cp = ControlPlane.in_memory()
    agent = _agent(cp, "natasha")

    out = reconcile_openshell_agents(
        cp,
        agent_selectors=[agent.id],
        policy_text=POLICY,
        apply=True,
        actor="operator",
        validated=True,
        sandbox_id="smoke-1",
        validation_summary="openshell smoke passed",
    )

    refreshed = cp.get_agent(agent.id)
    assert refreshed.resources["openshell_required"] is True
    assert refreshed.resources["hardware"] == {"accelerator": "gpu"}
    assert refreshed.resources["commands"] == {"available": ["git", "python3"]}
    status = cp.get_openshell_status(agent.id)
    assert status["required"] is True
    assert status["effective"] == {
        "assigned": True,
        "deployed": True,
        "fail_closed": False,
    }
    assert status["deployed_status"]["detail"]["validation"] == "openshell smoke passed"
    assert out["agents"][0]["after"]["effective"]["deployed"] is True


def test_reregistration_and_heartbeat_cannot_clear_openshell_requirement():
    cp = ControlPlane.in_memory()
    agent = _agent(cp, "sticky")
    original = dict(agent.resources)
    original["openshell_required"] = True
    cp.update_agent(agent.id, resources=original, actor="operator")

    reregistered = cp.register_agent(
        agent.machine_id,
        agent.name,
        capabilities=agent.capabilities,
        resources={"hardware": {"accelerator": "new"}, "openshell_required": False},
        agent_id=agent.id,
    )
    assert reregistered.resources["openshell_required"] is True
    assert reregistered.resources["hardware"] == {"accelerator": "new"}

    heartbeat = cp.heartbeat_agent(
        agent.id,
        resources={"commands": {"available": ["git"]}, "openshell_required": False},
    )
    assert heartbeat.resources["openshell_required"] is True
    assert heartbeat.resources["commands"] == {"available": ["git"]}

    resources = dict(heartbeat.resources)
    resources["openshell_required"] = False
    assert cp.update_agent(agent.id, resources=resources).resources["openshell_required"] is False


def test_openshell_reconcile_rerun_reuses_policy_and_assignment():
    cp = ControlPlane.in_memory()
    agent = _agent(cp, "bullwinkle")

    reconcile_openshell_agents(
        cp,
        agent_selectors=[agent.name],
        policy_text=POLICY,
        apply=True,
        validated=True,
    )
    first = cp.get_openshell_status(agent.id)
    first_assignment = first["assignment"]["id"]

    out = reconcile_openshell_agents(
        cp,
        agent_selectors=[agent.name],
        policy_text=POLICY,
        apply=True,
        validated=True,
    )

    second = cp.get_openshell_status(agent.id)
    assert out["policy"]["action"] == "reuse"
    assert "assign_policy" not in out["agents"][0]["actions"]
    assert second["assignment"]["id"] == first_assignment


def test_openshell_reconcile_dry_run_policy_update_predicts_reassignment():
    cp = ControlPlane.in_memory()
    agent = _agent(cp, "worker")

    reconcile_openshell_agents(
        cp,
        agent_selectors=[agent.name],
        policy_text=POLICY,
        apply=True,
        validated=True,
    )
    current = cp.get_openshell_status(agent.id)

    out = reconcile_openshell_agents(
        cp,
        agent_selectors=[agent.name],
        policy_text=POLICY_UPDATED,
        apply=False,
        validated=True,
    )

    assert out["policy"]["action"] == "update"
    assert out["policy"]["version"] == 2
    assert out["policy"]["checksum"] != current["policy"]["checksum"]
    assert "assign_policy" in out["agents"][0]["actions"]
    assert cp.get_openshell_status(agent.id)["assignment"]["id"] == current["assignment"]["id"]


def test_openshell_reconcile_can_skip_missing_fleet_agents():
    cp = ControlPlane.in_memory()
    agent = _agent(cp, "present")

    out = reconcile_openshell_agents(
        cp,
        agent_selectors=[agent.name, "missing"],
        policy_text=POLICY,
        apply=False,
        validated=True,
        allow_missing_agents=True,
    )

    assert out["missing_agents"] == ["missing"]
    assert [row["agent_name"] for row in out["agents"]] == ["present"]


def test_openshell_reconcile_requires_validation_before_reporting_active():
    cp = ControlPlane.in_memory()
    agent = _agent(cp)

    with pytest.raises(ValueError, match="validated"):
        reconcile_openshell_agents(
            cp,
            agent_selectors=[agent.name],
            policy_text=POLICY,
            apply=True,
            validated=False,
        )


def test_fleet_agent_names_uses_enabled_linux_agents_only():
    cfg = {
        "fleets": {
            "main": {
                "default": True,
                "agents": [
                    {"name": "rocky", "enabled": True, "os": "linux"},
                    {"name": "disabled", "enabled": False, "os": "linux"},
                    {"name": "desktop", "enabled": True, "os": "darwin"},
                    {"name": "natasha"},
                ],
            }
        }
    }

    assert fleet_agent_names(cfg) == ["rocky", "natasha"]
