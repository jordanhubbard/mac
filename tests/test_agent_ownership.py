"""An agent has an owner, and a private agent is not fleet capacity.

A static worker on its owner's own network is reachable only by them.
Advertising it fleet-wide is not a scheduling inefficiency -- it is a false
claim, and the allocator will place work on a machine the rest of the fleet
cannot reach. An internet-reachable worker may equally hold data its owner
will register against their own virtual fleet and no further.

The visibility COLUMN defaults to 'shared' so that adding it strands no
existing agent; the safe default (private, when an owner is named) lives in
the service layer where it governs new registrations only.
"""

from __future__ import annotations

import pytest

from mac.models import ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


@pytest.fixture()
def alice(cp):
    return cp.register_human(username="alice", display_name="Alice")


def _machine(cp, name="host"):
    return cp.register_machine(name)


def test_an_owned_agent_defaults_to_private(cp, alice):
    """The safe default: registering your own hardware must not silently add it
    to everyone's capacity."""
    machine = _machine(cp)

    agent = cp.register_agent(machine.id, "mine", owner_human_id="alice")

    assert agent.owner_human_id == alice.id
    assert agent.visibility == "private"


def test_an_unowned_agent_stays_shared(cp):
    """Existing fleet behaviour is unchanged when nobody claims the agent."""
    machine = _machine(cp)

    agent = cp.register_agent(machine.id, "pool")

    assert agent.owner_human_id is None
    assert agent.visibility == "shared"


def test_an_owner_may_publish_their_agent(cp, alice):
    machine = _machine(cp)

    agent = cp.register_agent(
        machine.id, "lent", owner_human_id="alice", visibility="shared"
    )

    assert agent.visibility == "shared" and agent.owner_human_id == alice.id


def test_a_private_agent_without_an_owner_is_refused(cp):
    """Nobody could use it -- including whoever registered it."""
    machine = _machine(cp)

    with pytest.raises(ValidationError) as excinfo:
        cp.register_agent(machine.id, "orphan", visibility="private")

    assert "needs an owner" in str(excinfo.value)


def test_an_unknown_owner_is_refused(cp):
    """Ownership must point at a real principal, not a free-text string."""
    machine = _machine(cp)

    with pytest.raises(ValidationError) as excinfo:
        cp.register_agent(machine.id, "a", owner_human_id="nobody")

    assert "no such human" in str(excinfo.value)


def test_an_invalid_visibility_is_refused(cp, alice):
    machine = _machine(cp)

    with pytest.raises(ValidationError):
        cp.register_agent(
            machine.id, "a", owner_human_id="alice", visibility="semi-public"
        )


def test_the_owner_is_stored_as_a_stable_id(cp, alice):
    """A username can be changed; a stored username would silently re-point
    ownership at somebody else."""
    machine = _machine(cp)

    agent = cp.register_agent(machine.id, "mine", owner_human_id="alice")

    assert agent.owner_human_id == alice.id != "alice"


def test_existing_agents_are_readable_without_the_new_columns(cp):
    """Hydration must tolerate a row shape predating the columns, or every
    pre-existing agent becomes unreadable."""
    machine = _machine(cp)
    agent = cp.register_agent(machine.id, "legacy")

    reloaded = cp.get_agent(agent.id)

    assert reloaded.visibility in {"shared", "private"}


# --------------------------------------------------------------------------
# Allocator enforcement. The column is only a comment until placement honours
# it -- the recurring defect in this codebase is a correct decision with no
# consumer, so these tests exist to give the attribute one.
# --------------------------------------------------------------------------


def _pair(*, visibility="shared", owner=None, filer=None):
    from mac.allocator import AllocationAgent, AllocationTask, evaluate_pair

    task = AllocationTask(
        id="task_owned",
        priority=5,
        created_at="2026-08-02T00:00:00+00:00",
        required_capabilities=frozenset({"python"}),
        created_by_human=filer,
    )
    agent = AllocationAgent(
        id="worker",
        capabilities=frozenset({"python"}),
        visibility=visibility,
        owner_human_id=owner,
    )
    return evaluate_pair(task, agent)


def test_a_private_agent_refuses_another_persons_task():
    from mac.allocator import AGENT_PRIVATE_TO_OTHER_OWNER

    result = _pair(visibility="private", owner="human-a", filer="human-b")

    assert AGENT_PRIVATE_TO_OTHER_OWNER in result.rejections


def test_a_private_agent_still_runs_its_owners_task():
    result = _pair(visibility="private", owner="human-a", filer="human-a")

    assert result.allowed, result.rejections


def test_a_shared_agent_runs_anyones_task():
    """The fleet's GKE workers are shared; gating them on ownership would idle
    the only capacity that is reachable by everyone."""
    result = _pair(visibility="shared", owner="human-a", filer="human-b")

    assert result.allowed, result.rejections


def test_an_unclaimed_private_agent_is_nobodys_capacity():
    """Fail closed: private with no owner recorded matches no filer, rather
    than matching every filer."""
    from mac.allocator import AGENT_PRIVATE_TO_OTHER_OWNER

    result = _pair(visibility="private", owner=None, filer="human-a")

    assert AGENT_PRIVATE_TO_OTHER_OWNER in result.rejections


def test_the_refusal_is_authorization_not_a_capability_gap():
    """Capability gaps drive the 'become capable' machinery -- an agent that
    tried to LEARN its way past an ownership boundary would retry forever."""
    from mac.allocator import (
        AGENT_PRIVATE_TO_OTHER_OWNER,
        AUTHORIZATION_REJECTIONS,
        TRANSIENT_REJECTIONS,
    )

    assert AGENT_PRIVATE_TO_OTHER_OWNER in AUTHORIZATION_REJECTIONS
    assert AGENT_PRIVATE_TO_OTHER_OWNER not in TRANSIENT_REJECTIONS


# --------------------------------------------------------------------------
# The wiring. evaluate_pair honouring the attribute proves nothing if the
# snapshot builders never populate it -- the pair evaluation would compare
# "shared" against None forever and the gate would be dead code that passes
# its own unit tests.
# --------------------------------------------------------------------------


def test_the_agent_snapshot_carries_ownership_from_the_hub_record(cp, alice):
    from mac.allocator import AllocationAgent

    machine = _machine(cp, "owner-wiring-host")
    agent = cp.register_agent(
        machine.id, "private-worker", owner_human_id=alice.id, visibility="private"
    )

    snapshot = AllocationAgent.from_hub_record(
        agent, online=True, capacity=1, active_leases=0, machine_trusted=True
    )

    assert snapshot.visibility == "private"
    assert snapshot.owner_human_id == alice.id


def test_the_task_snapshot_carries_its_filer(cp, alice):
    """Read through the lifecycle builder, not constructed by hand: the gate
    compares against this field, so if the builder drops it every private
    agent silently refuses every task."""
    task = cp.create_task("owned work", project="mac", created_by_human=alice.id)

    snapshot = cp.dispatch._v2_snapshot_task(
        cp.get_task(task.id), projects={}, agent_ids_by_name={}
    )

    assert snapshot.created_by_human == alice.id
