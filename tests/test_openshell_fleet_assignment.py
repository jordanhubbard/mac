"""Fleet-scoped OpenShell assignments must actually reach the agent.

A fleet assignment used to be accepted, listed by `policy assignments`, and
enforced nothing: resolution queried ``target_type = 'agent'`` only, so the
worker fell back to whatever local policy file it already had. On a confinement
boundary that made "assigned" and "enforced" indistinguishable from outside.
"""

from __future__ import annotations

import pytest

from mac.models import NotFoundError, ValidationError
from mac.services import ControlPlane

POLICY_TEXT = """version: 1

network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: hub.example.com
        port: 8789
"""


def _policy_text(host: str) -> str:
    return POLICY_TEXT.replace("hub.example.com", host)


def _agent(cp: ControlPlane, name: str = "worker"):
    machine = cp.register_machine("host-%s" % name)
    return cp.register_agent(machine.id, name, capabilities=[])


def test_fleet_assignment_delivers_the_policy_text_to_a_member_agent() -> None:
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    fleet = cp.create_fleet("edge", agent_ids=[agent.id])
    policy = cp.openshell.create_policy("edge-policy", _policy_text("fleet.example.com"))

    cp.openshell.assign_policy(policy.id, target_type="fleet", target_id=fleet.id)

    assigned = cp.assigned_openshell_policy(agent.id)
    assert assigned["policy_id"] == policy.id
    assert assigned["policy_text"] == policy.policy_text
    assert "fleet.example.com" in assigned["policy_text"]
    assert assigned["checksum"] == policy.checksum


def test_fleet_assignment_does_not_leak_to_non_members() -> None:
    cp = ControlPlane.in_memory()
    member = _agent(cp, "member")
    outsider = _agent(cp, "outsider")
    fleet = cp.create_fleet("edge", agent_ids=[member.id])
    policy = cp.openshell.create_policy("edge-policy", _policy_text("fleet.example.com"))
    cp.openshell.assign_policy(policy.id, target_type="fleet", target_id=fleet.id)

    assert cp.assigned_openshell_policy(member.id)["policy_id"] == policy.id
    with pytest.raises(NotFoundError, match="no OpenShell policy assigned"):
        cp.assigned_openshell_policy(outsider.id)


def test_agent_scoped_assignment_beats_fleet_scoped() -> None:
    """The more specific target is the more deliberate one: pinning one agent
    must override the fleet default without editing the fleet."""
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    fleet = cp.create_fleet("edge", agent_ids=[agent.id])
    fleet_policy = cp.openshell.create_policy("fleet-policy", _policy_text("fleet.example.com"))
    agent_policy = cp.openshell.create_policy("agent-policy", _policy_text("pinned.example.com"))

    cp.openshell.assign_policy(fleet_policy.id, target_type="fleet", target_id=fleet.id)
    cp.openshell.assign_policy(agent_policy.id, target_type="agent", target_id=agent.id)

    assigned = cp.assigned_openshell_policy(agent.id)
    assert assigned["policy_id"] == agent_policy.id
    assert "pinned.example.com" in assigned["policy_text"]
    assert "fleet.example.com" not in assigned["policy_text"]


def test_fleet_assignment_accepts_a_fleet_name_but_stores_the_id() -> None:
    """A rename must not silently disarm the assignment."""
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    fleet = cp.create_fleet("edge", agent_ids=[agent.id])
    policy = cp.openshell.create_policy("edge-policy", _policy_text("fleet.example.com"))

    assignment = cp.openshell.assign_policy(policy.id, target_type="fleet", target_id="edge")
    assert assignment.target_id == fleet.id

    cp.update_fleet(fleet.id, name="edge-renamed")
    assert cp.assigned_openshell_policy(agent.id)["policy_id"] == policy.id


def test_assigning_to_an_unknown_fleet_is_refused() -> None:
    cp = ControlPlane.in_memory()
    policy = cp.openshell.create_policy("edge-policy", POLICY_TEXT)
    with pytest.raises(NotFoundError, match="fleet not found"):
        cp.openshell.assign_policy(policy.id, target_type="fleet", target_id="no-such-fleet")


def test_two_fleets_naming_different_policies_fail_loud() -> None:
    """`whichever row sorts first` would make confinement depend on insert order."""
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    left = cp.create_fleet("left", agent_ids=[agent.id])
    right = cp.create_fleet("right", agent_ids=[agent.id])
    left_policy = cp.openshell.create_policy("left-policy", _policy_text("left.example.com"))
    right_policy = cp.openshell.create_policy("right-policy", _policy_text("right.example.com"))
    cp.openshell.assign_policy(left_policy.id, target_type="fleet", target_id=left.id)
    cp.openshell.assign_policy(right_policy.id, target_type="fleet", target_id=right.id)

    with pytest.raises(ValidationError, match="conflicting OpenShell policy assignments"):
        cp.assigned_openshell_policy(agent.id)


def test_two_fleets_naming_the_same_policy_resolve() -> None:
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    left = cp.create_fleet("left", agent_ids=[agent.id])
    right = cp.create_fleet("right", agent_ids=[agent.id])
    policy = cp.openshell.create_policy("shared-policy", _policy_text("shared.example.com"))
    cp.openshell.assign_policy(policy.id, target_type="fleet", target_id=left.id)
    cp.openshell.assign_policy(policy.id, target_type="fleet", target_id=right.id)

    assigned = cp.assigned_openshell_policy(agent.id)
    assert assigned["policy_id"] == policy.id
    assert "shared.example.com" in assigned["policy_text"]


def test_an_agent_scoped_pin_resolves_a_conflict_between_fleets() -> None:
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    left = cp.create_fleet("left", agent_ids=[agent.id])
    right = cp.create_fleet("right", agent_ids=[agent.id])
    left_policy = cp.openshell.create_policy("left-policy", _policy_text("left.example.com"))
    right_policy = cp.openshell.create_policy("right-policy", _policy_text("right.example.com"))
    pinned = cp.openshell.create_policy("pinned-policy", _policy_text("pinned.example.com"))
    cp.openshell.assign_policy(left_policy.id, target_type="fleet", target_id=left.id)
    cp.openshell.assign_policy(right_policy.id, target_type="fleet", target_id=right.id)
    cp.openshell.assign_policy(pinned.id, target_type="agent", target_id=agent.id)

    assert "pinned.example.com" in cp.assigned_openshell_policy(agent.id)["policy_text"]


def test_reassigning_a_fleet_supersedes_the_previous_policy() -> None:
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    fleet = cp.create_fleet("edge", agent_ids=[agent.id])
    first = cp.openshell.create_policy("first", _policy_text("first.example.com"))
    second = cp.openshell.create_policy("second", _policy_text("second.example.com"))
    cp.openshell.assign_policy(first.id, target_type="fleet", target_id=fleet.id)
    assert "first.example.com" in cp.assigned_openshell_policy(agent.id)["policy_text"]

    cp.openshell.assign_policy(second.id, target_type="fleet", target_id=fleet.id)
    assigned = cp.assigned_openshell_policy(agent.id)
    assert assigned["policy_id"] == second.id
    assert "second.example.com" in assigned["policy_text"]


def test_dropping_membership_returns_the_agent_to_no_hub_policy() -> None:
    """Deactivation semantics: falling out of the fleet falls through to the
    next matching rule — here, none — rather than pinning the last policy."""
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    other = _agent(cp, "other")
    fleet = cp.create_fleet("edge", agent_ids=[agent.id, other.id])
    policy = cp.openshell.create_policy("edge-policy", _policy_text("fleet.example.com"))
    cp.openshell.assign_policy(policy.id, target_type="fleet", target_id=fleet.id)
    assert cp.assigned_openshell_policy(agent.id)["policy_id"] == policy.id

    cp.update_fleet(fleet.id, agent_ids=[other.id])

    with pytest.raises(NotFoundError, match="no OpenShell policy assigned"):
        cp.assigned_openshell_policy(agent.id)
    assert cp.assigned_openshell_policy(other.id)["policy_id"] == policy.id


def test_deactivated_fleet_assignment_leaves_no_hub_policy() -> None:
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    fleet = cp.create_fleet("edge", agent_ids=[agent.id])
    fleet_policy = cp.openshell.create_policy("fleet-policy", _policy_text("fleet.example.com"))
    cp.openshell.assign_policy(fleet_policy.id, target_type="fleet", target_id=fleet.id)
    assert "fleet.example.com" in cp.assigned_openshell_policy(agent.id)["policy_text"]

    cp.store.execute(
        "UPDATE openshell_policy_assignments SET active = 0 "
        "WHERE target_type = ? AND target_id = ?",
        ("fleet", fleet.id),
    )

    with pytest.raises(NotFoundError, match="no OpenShell policy assigned"):
        cp.assigned_openshell_policy(agent.id)


def test_runtime_observation_alone_does_not_confer_a_fleet_policy() -> None:
    """An agent must not be able to observe itself into someone else's policy."""
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    fleet = cp.create_fleet("edge")
    policy = cp.openshell.create_policy("edge-policy", _policy_text("fleet.example.com"))
    cp.openshell.assign_policy(policy.id, target_type="fleet", target_id=fleet.id)

    cp.observe_fleet_agent(fleet.id, agent.id)

    with pytest.raises(NotFoundError, match="no OpenShell policy assigned"):
        cp.assigned_openshell_policy(agent.id)


def test_host_scoped_assignment_is_refused_rather_than_silently_inert() -> None:
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    policy = cp.openshell.create_policy("edge-policy", POLICY_TEXT)

    with pytest.raises(ValidationError, match="host-scoped OpenShell assignments are not enforced"):
        cp.openshell.assign_policy(policy.id, target_type="host", target_id=agent.machine_id)

    assert cp.openshell.list_assignments(target_type="host") == []


def test_agent_status_reports_the_fleet_assignment() -> None:
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    fleet = cp.create_fleet("edge", agent_ids=[agent.id])
    policy = cp.openshell.create_policy("edge-policy", _policy_text("fleet.example.com"))
    cp.openshell.assign_policy(policy.id, target_type="fleet", target_id=fleet.id)

    status = cp.get_openshell_status(agent.id)
    assert status["effective"]["assigned"] is True
    assert status["assignment"]["target_type"] == "fleet"
    assert status["assignment"]["target_id"] == fleet.id
    assert status["policy"]["id"] == policy.id
