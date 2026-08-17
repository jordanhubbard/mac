"""An agent has an owner, and ownership does not decide what it may work on.

`visibility` describes a COMMUNICATION boundary: the hub does not talk to
anyone but the owner unless the owner grants permission, and a private agent
collaborates with the outside world through the git repository -- code, PRs,
issues -- like any other contributor.

It is NOT a dispatch gate. Access to a fleet is boolean and decided outside
mac: you can reach the hub and the collaborating repo, or you cannot. Anyone
who can file a task can file it under any name, so refusing work on
created_by_human authorized nothing while creating a real deadlock -- on
2026-08-17 every bare-metal worker was private, most work carried no filer,
and the hub sat with 18 ready tasks, 3 idle agents and 0 assignments.

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


def test_a_private_agent_runs_another_persons_task():
    """Visibility is not a dispatch gate.

    It used to be: a private agent refused any task whose created_by_human was
    not its owner. That conflated a COMMUNICATION boundary with a WORK
    boundary. Access to this fleet is boolean and decided outside mac -- you
    can reach the hub and the collaborating repository or you cannot -- and
    anyone who can file a task can file it under any name, so the check
    authorized nothing.
    """
    result = _pair(visibility="private", owner="human-a", filer="human-b")

    assert result.allowed, result.rejections


def test_a_private_agent_still_runs_its_owners_task():
    result = _pair(visibility="private", owner="human-a", filer="human-a")

    assert result.allowed, result.rejections


def test_a_shared_agent_runs_anyones_task():
    result = _pair(visibility="shared", owner="human-a", filer="human-b")

    assert result.allowed, result.rejections


def test_an_unowned_private_agent_is_still_capacity():
    """Previously "fail closed": private with no owner matched no filer.

    That is what took the entire bare-metal tier out of service on
    2026-08-17 -- every worker private, most work filed with no
    created_by_human, and the hub sat at 18 ready / 3 idle / 0 assignments.
    """
    result = _pair(visibility="private", owner=None, filer="human-a")

    assert result.allowed, result.rejections


def test_an_unowned_private_agent_runs_unattributed_work():
    """The deadlock in its purest form: nobody claimed the agent, nobody is
    recorded as filing the task, and the pair used to reject."""
    result = _pair(visibility="private", owner=None, filer=None)

    assert result.allowed, result.rejections


def test_no_pairing_is_rejected_for_ownership_any_more():
    """Nothing emits the retired code, whatever the ownership combination."""
    from mac.allocator import AGENT_PRIVATE_TO_OTHER_OWNER

    for visibility in ("private", "shared"):
        for owner in (None, "human-a"):
            for filer in (None, "human-a", "human-b"):
                result = _pair(visibility=visibility, owner=owner, filer=filer)
                assert AGENT_PRIVATE_TO_OTHER_OWNER not in result.rejections, (
                    "visibility=%s owner=%s filer=%s" % (visibility, owner, filer)
                )


def test_the_retired_code_stays_classified_for_stored_rejections():
    """Rejections recorded before the change must still read correctly."""
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
