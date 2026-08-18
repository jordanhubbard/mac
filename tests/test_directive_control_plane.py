from __future__ import annotations

from mac.services import ControlPlane


def test_control_plane_pins_effective_snapshot_on_new_tasks(monkeypatch) -> None:
    monkeypatch.setenv("MAC_DIRECTIVES_ENABLED", "1")
    cp = ControlPlane.in_memory()
    proposed = cp.propose_directive(
        {
            "schema": "mac.directive.v1",
            "name": "review.require-independent",
            "description": "Require an independent review for new work.",
            "scope": "fleet",
            "set": {"review.independent_required": True},
        },
        actor="operator",
    )
    version = proposed["versions"][0]
    checked = cp.check_directive(proposed["id"], actor="operator")
    cp.approve_directive(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        check_id=checked["id"],
        actor="operator",
    )
    activation = cp.activate_directive(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        actor="operator",
    )
    assert activation["state"] == "active"  # no live workers in the cohort

    task = cp.create_task("Document the policy snapshot")
    snapshot = task.metadata["directive_snapshot"]
    assert snapshot["schema"] == "mac.directive.snapshot.v1"
    assert snapshot["set"]["review.independent_required"] is True
    assert snapshot["set"]["verification.tests_required"] is True
    assert snapshot["epoch"] == activation["epoch"]


def test_dispatch_gate_rejects_unacknowledged_policy_epoch(monkeypatch) -> None:
    monkeypatch.setenv("MAC_DIRECTIVES_ENABLED", "1")
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker")
    task = cp.create_task("Wait for policy convergence")
    proposed = cp.propose_directive(
        {
            "schema": "mac.directive.v1",
            "name": "review.require-independent",
            "description": "Require an independent review for new work.",
            "scope": "fleet",
            "set": {"review.independent_required": True},
        },
        actor="operator",
    )
    version = proposed["versions"][0]
    checked = cp.check_directive(proposed["id"], actor="operator")
    cp.approve_directive(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        check_id=checked["id"],
        actor="operator",
    )
    activation = cp.activate_directive(
        proposed["id"],
        version=1,
        directive_digest=version["digest"],
        actor="operator",
    )

    eligible, reason = cp._agent_availability_for_task(agent, task)
    assert eligible is False
    assert reason == "directive_policy_unacknowledged"

    cp.acknowledge_directive_activation(
        agent.id, activation["id"], digest=version["digest"]
    )
    _eligible_after, reason_after = cp._agent_availability_for_task(agent, task)
    assert reason_after != "directive_policy_unacknowledged"
