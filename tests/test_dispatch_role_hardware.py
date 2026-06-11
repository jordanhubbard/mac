"""Dispatch matcher honors role and hardware constraints.

Phase 2 of the agent-roles feature: the existing capability set check
still wins for un-roled agents and tasks, but when a task carries
``required_role`` / ``hardware`` metadata or an agent carries a
``role_id``, the dispatcher must consult the role's hardware
requirements and stack the role's ``required_capabilities`` onto the
task's set.

``dispatch_once`` returns at most one assignment per call (or None), so
these tests call it repeatedly when checking multi-agent eligibility.
"""

from __future__ import annotations

import pytest

from mac.services import ControlPlane
from tests.conftest import bind_soul


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _machine(cp, *, hostname="host-x", hardware=None):
    return cp.register_machine(hostname, hardware=hardware or {})


def test_un_roled_dispatch_unaffected_by_role_check(cp):
    machine = _machine(cp)
    agent = cp.register_agent(machine.id, "hosta", capabilities=["python"])
    task = cp.create_task("plain", required_capabilities=["python"])
    assignment = cp.dispatch_once(lease_seconds=300)
    assert assignment is not None
    assert assignment["task"]["id"] == task.id
    assert assignment["agent"]["id"] == agent.id


def test_agent_without_required_role_is_ineligible(cp):
    machine = _machine(cp)
    cp.register_agent(machine.id, "hosta", capabilities=["python", "review"])
    cp.create_task(
        "needs-reviewer",
        required_capabilities=["python"],
        metadata={"required_role": "code-reviewer"},
    )
    assert cp.dispatch_once(lease_seconds=300) is None


def test_agent_with_required_role_matches(cp):
    machine = _machine(cp)
    cp.roles.create_role(
        slug="code-reviewer",
        name="Code Reviewer",
        description="d",
        system_prompt="p",
        level="ic",
        default_capabilities=["review"],
        required_capabilities=["review"],
    )
    soul = bind_soul(
        cp, persona_name="Reviewer Soul", allowed_role_slugs=["code-reviewer"]
    )
    agent = cp.register_agent(
        machine.id, "hosta", capabilities=["python"], hermes_instance_id=soul
    )
    cp.roles.assign_role(agent.id, "code-reviewer")
    task = cp.create_task(
        "needs-reviewer",
        required_capabilities=["python"],
        metadata={"required_role": "code-reviewer"},
    )
    assignment = cp.dispatch_once(lease_seconds=300)
    assert assignment is not None
    assert assignment["task"]["id"] == task.id
    assert assignment["agent"]["id"] == agent.id


def test_role_required_capabilities_stack_onto_task_set(cp):
    cp.roles.create_role(
        slug="ops",
        name="Ops",
        description="d",
        system_prompt="p",
        level="ic",
        required_capabilities=["sudo"],
    )
    # Agent has python but not sudo. Even though the task only asks for
    # python, the role's required_capabilities stack on top — the agent
    # is ineligible until sudo lands on its set too.
    machine = _machine(cp)
    soul = bind_soul(cp, persona_name="Ops Soul", allowed_role_slugs=["ops"])
    agent = cp.register_agent(
        machine.id, "hosta", capabilities=["python"], hermes_instance_id=soul
    )
    cp.roles.assign_role(agent.id, "ops")
    cp.create_task("py-task", required_capabilities=["python"])
    assert cp.dispatch_once(lease_seconds=300) is None

    # Re-register adding `sudo` to the agent's capability set.
    cp.register_agent(
        machine.id,
        "hosta",
        capabilities=["python", "sudo"],
        agent_id=agent.id,
        hermes_instance_id=soul,
    )
    cp.roles.assign_role(agent.id, "ops")
    assignment = cp.dispatch_once(lease_seconds=300)
    assert assignment is not None
    assert assignment["agent"]["id"] == agent.id


def test_agent_hardware_mismatch_filters_out(cp):
    cp.roles.create_role(
        slug="gpu-runner",
        name="GPU Runner",
        description="d",
        system_prompt="p",
        level="ic",
        hardware_requirements={"cpu_arch": ["arm64"]},
    )
    soul = bind_soul(cp, persona_name="GPU Soul", allowed_role_slugs=["gpu-runner"])
    arm = _machine(cp, hostname="arm", hardware={"cpu_arch": "arm64"})
    arm_agent = cp.register_agent(arm.id, "arm-runner", hermes_instance_id=soul)
    cp.roles.assign_role(arm_agent.id, "gpu-runner")

    cp.create_task("compile", metadata={"required_role": "gpu-runner"})
    assignment = cp.dispatch_once(lease_seconds=300)
    assert assignment is not None
    assert assignment["agent"]["id"] == arm_agent.id


def test_task_hardware_constraint_filters_machines(cp):
    big = _machine(cp, hostname="big", hardware={"memory_gb": 64})
    small = _machine(cp, hostname="small", hardware={"memory_gb": 8})
    cp.register_agent(small.id, "small-host")
    big_agent = cp.register_agent(big.id, "big-host")

    # Task hardware constraint (set by the workflow runtime in later
    # phases) filters out the under-spec machine even without a role.
    cp.create_task("memory-heavy", metadata={"hardware": {"memory_gb_min": 32}})
    assignment = cp.dispatch_once(lease_seconds=300)
    assert assignment is not None
    assert assignment["agent"]["id"] == big_agent.id


# ---------------------------------------------------------------------------
# Dispatcher (role_id IS NULL) gate: the mac-runner identity carries the
# UNION of role capabilities and re-attributes work to a role-specific
# identity in the Job spec (job-per-task-roles-spec §6.1 Option B). The
# agent-side `required_role` gate must therefore accept a roleless
# dispatcher whose capabilities cover the target role's
# `required_capabilities`, while preserving the strict slug match for
# role-bound agents.
# ---------------------------------------------------------------------------


def test_dispatcher_without_role_id_can_claim_task_with_required_role(cp):
    """mac-runner (the dispatcher) has no role_id but holds the union of
    role capabilities. It MUST be allowed to claim a task whose
    metadata.required_role points at a known role, so long as its own
    capabilities cover that role's required_capabilities."""
    cp.roles.create_role(
        slug="python-coder",
        name="Python Coder",
        description="d",
        system_prompt="p",
        level="ic",
        required_capabilities=["python"],
    )
    machine = _machine(cp)
    dispatcher = cp.register_agent(
        machine.id, "mac-runner", capabilities=["python", "review", "ops"]
    )
    assert dispatcher.role_id is None
    task = cp.create_task(
        "specialised",
        required_capabilities=["python"],
        metadata={"required_role": "python-coder"},
    )

    assert cp._agent_available_for(dispatcher, task) is True

    assignment = cp.dispatch_once(lease_seconds=300)
    assert assignment is not None
    assert assignment["task"]["id"] == task.id
    assert assignment["agent"]["id"] == dispatcher.id


def test_agent_with_role_id_still_strict_when_role_matches(cp):
    """Backward-compat: role-bound agents whose role slug matches the
    task's required_role must continue to be eligible."""
    cp.roles.create_role(
        slug="python-coder",
        name="Python Coder",
        description="d",
        system_prompt="p",
        level="ic",
        required_capabilities=["python"],
    )
    soul = bind_soul(
        cp, persona_name="Coder Soul", allowed_role_slugs=["python-coder"]
    )
    machine = _machine(cp)
    agent = cp.register_agent(
        machine.id, "py-coder", capabilities=["python"], hermes_instance_id=soul
    )
    cp.roles.assign_role(agent.id, "python-coder")
    task = cp.create_task(
        "specialised",
        required_capabilities=["python"],
        metadata={"required_role": "python-coder"},
    )

    refreshed = cp.get_agent(agent.id)
    assert refreshed.role_id is not None
    assert cp._agent_available_for(refreshed, task) is True


def test_agent_with_role_id_still_strict_when_role_mismatches(cp):
    """Backward-compat: a role-bound agent whose slug does NOT match the
    task's required_role must still be rejected, even if its capability
    set technically covers the target role's required_capabilities."""
    cp.roles.create_role(
        slug="python-coder",
        name="Python Coder",
        description="d",
        system_prompt="p",
        level="ic",
        required_capabilities=["python"],
    )
    cp.roles.create_role(
        slug="python-reviewer",
        name="Python Reviewer",
        description="d",
        system_prompt="p",
        level="ic",
        required_capabilities=["review"],
    )
    soul = bind_soul(
        cp, persona_name="Reviewer Soul", allowed_role_slugs=["python-reviewer"]
    )
    machine = _machine(cp)
    # Agent has every capability the coder role would need, but is bound
    # to the reviewer role. Strict slug match must still reject it.
    agent = cp.register_agent(
        machine.id,
        "py-reviewer",
        capabilities=["python", "review"],
        hermes_instance_id=soul,
    )
    cp.roles.assign_role(agent.id, "python-reviewer")
    task = cp.create_task(
        "specialised",
        required_capabilities=["python"],
        metadata={"required_role": "python-coder"},
    )

    refreshed = cp.get_agent(agent.id)
    assert refreshed.role_id is not None
    assert cp._agent_available_for(refreshed, task) is False


def test_dispatcher_rejected_if_capabilities_insufficient_for_role(cp):
    """A roleless dispatcher whose capability set does NOT cover the
    target role's required_capabilities must still be rejected. This
    keeps the "operator forgot to update AGENT_CAPABILITIES" misconfig
    visible rather than letting the dispatcher silently claim work it
    can't actually execute (spec §13 Q6)."""
    cp.roles.create_role(
        slug="python-coder",
        name="Python Coder",
        description="d",
        system_prompt="p",
        level="ic",
        required_capabilities=["python"],
    )
    machine = _machine(cp)
    dispatcher = cp.register_agent(
        machine.id, "mac-runner", capabilities=["ops"]
    )
    assert dispatcher.role_id is None
    task = cp.create_task(
        "specialised",
        # Note: task.required_capabilities deliberately empty so the
        # ONLY thing forcing the python requirement is the role's
        # required_capabilities. Confirms the dispatcher gate consults
        # the role record, not just the task's capability list.
        metadata={"required_role": "python-coder"},
    )

    assert cp._agent_available_for(dispatcher, task) is False


def test_unknown_required_role_still_rejected(cp):
    """A task that names a role that doesn't exist in the agent_roles
    table can't be served by anyone — dispatcher or role-bound. The
    unknown-role check must fail closed."""
    machine = _machine(cp)
    dispatcher = cp.register_agent(
        machine.id, "mac-runner", capabilities=["python", "review", "ops"]
    )
    assert dispatcher.role_id is None
    task = cp.create_task(
        "specialised",
        required_capabilities=["python"],
        metadata={"required_role": "nope-not-a-real-role"},
    )

    assert cp._agent_available_for(dispatcher, task) is False
