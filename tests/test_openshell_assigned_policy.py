"""Hub-side delivery of an assigned OpenShell policy to the agent that runs it.

`mac openshell policy assign` used to record intent only: the hub had no route
an agent could pull its own policy from, so the executor kept resolving whatever
bootstrap-openshell.sh wrote at provision time.
"""

from __future__ import annotations

import pytest

from mac.models import NotFoundError
from mac.services import ControlPlane

POLICY_TEXT = """version: 1

network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: hub.example.com
        port: 8789
"""


def _agent(cp: ControlPlane, name="worker"):
    machine = cp.register_machine("host-%s" % name)
    return cp.register_agent(machine.id, name, capabilities=[])


def test_assigned_policy_carries_the_text_and_its_checksum() -> None:
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    policy = cp.openshell.create_policy("fleet", POLICY_TEXT)
    cp.openshell.assign_policy(policy.id, target_type="agent", target_id=agent.id)

    assigned = cp.assigned_openshell_policy(agent.id)

    assert assigned["schema"] == "mac.openshell.assigned_policy.v1"
    assert assigned["policy_id"] == policy.id
    assert assigned["policy_text"] == policy.policy_text
    assert assigned["checksum"] == policy.checksum
    assert assigned["version"] == policy.version


def test_unassigned_agent_raises_rather_than_returning_an_empty_policy() -> None:
    """An empty policy would read as "no confinement required" at the far end."""
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    with pytest.raises(NotFoundError, match="no OpenShell policy assigned"):
        cp.assigned_openshell_policy(agent.id)


def test_reassignment_is_reflected_immediately() -> None:
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    first = cp.openshell.create_policy("first", POLICY_TEXT)
    second = cp.openshell.create_policy(
        "second", POLICY_TEXT.replace("hub.example.com", "hub2.example.com")
    )
    cp.openshell.assign_policy(first.id, target_type="agent", target_id=agent.id)
    assert cp.assigned_openshell_policy(agent.id)["policy_id"] == first.id

    cp.openshell.assign_policy(second.id, target_type="agent", target_id=agent.id)
    assigned = cp.assigned_openshell_policy(agent.id)
    assert assigned["policy_id"] == second.id
    assert "hub2.example.com" in assigned["policy_text"]


def test_policy_update_is_reflected_with_a_new_checksum() -> None:
    """The worker skips the write when checksums match, so an in-place policy
    update has to move the checksum or it would never be delivered."""
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    policy = cp.openshell.create_policy("fleet", POLICY_TEXT)
    cp.openshell.assign_policy(policy.id, target_type="agent", target_id=agent.id)
    before = cp.assigned_openshell_policy(agent.id)

    cp.openshell.update_policy(policy.id, policy_text=POLICY_TEXT.replace("8789", "9999"))
    after = cp.assigned_openshell_policy(agent.id)

    assert after["checksum"] != before["checksum"]
    assert "9999" in after["policy_text"]


def test_materialize_and_assigned_policy_agree() -> None:
    """Two delivery paths, one assignment resolution — they must not be able to
    disagree about which policy is current."""
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    policy = cp.openshell.create_policy("fleet", POLICY_TEXT)
    cp.openshell.assign_policy(policy.id, target_type="agent", target_id=agent.id)

    assigned = cp.assigned_openshell_policy(agent.id)
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "policy.yaml"
        materialized = cp.openshell.materialize_assigned_policy(agent.id, target)
        assert materialized["checksum"] == assigned["checksum"]
        assert materialized["policy_id"] == assigned["policy_id"]
        assert target.read_text(encoding="utf-8") == assigned["policy_text"]
        assert target.stat().st_mode & 0o777 == 0o600
