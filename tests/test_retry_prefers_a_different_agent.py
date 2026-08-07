"""A retry should prefer a different agent — and must never strand the task.

Measured on the live ledger 2026-08-06. Three tasks failed 3/3 and every
attempt landed on the same agent:

    task_c5407d23   agent_rocky            x3
    task_30929062   agent_jordanh-worker6  x3
    task_91ae3698   agent_rocky            (pinned, so expected)

task_30929062 was NOT pinned. It failed because that pod could not reach
another host's filesystem, so attempts 2 and 3 were guaranteed waste before
they started.

The allocator already ranks with ``prior_participation``, a SOFT signal in its
sort key. Nothing ever put "this agent already failed this task" into
``avoid_agent_ids``, so the signal was there and unfed.

Softness is the whole design, and it is not a detail to trade away later.
``_coordination_excluded_agent_ids`` documents what hard exclusion did: in a
finite pool, accumulated exclusions ratchet a task family into a permanent
no-eligible-agent deadlock. A retry on the same agent is worse than a retry
elsewhere and far better than a task that can never run again. So these tests
assert BOTH directions, because a change that only satisfied the first would
be a regression dressed as a fix.
"""

from __future__ import annotations

import pytest

from mac.allocator import AllocationAgent, AllocationTask, AuthoritativeAllocator


def _task(task_id="task_1", *, avoid=frozenset(), capabilities=frozenset(), project=None):
    return AllocationTask(
        id=task_id,
        priority=1,
        created_at="2026-08-06T00:00:00Z",
        required_capabilities=frozenset(capabilities),
        avoid_agent_ids=frozenset(avoid),
        project=project,
    )


def _agent(agent_id, *, capabilities=("python",), capacity=1, active=0, projects=None):
    return AllocationAgent(
        id=agent_id,
        capabilities=frozenset(capabilities),
        capacity=capacity,
        active_leases=active,
        preferred_projects=frozenset(projects or ()),
    )


def _rank(allocator, task, agents):
    """Order the agents the way the allocator would consider them."""
    return sorted(agents, key=lambda a: allocator._agent_sort_key(task, a, {}))


@pytest.fixture()
def allocator():
    return AuthoritativeAllocator()


def test_an_agent_that_already_failed_is_ranked_below_a_fresh_one(allocator):
    """The regression: worker6 got all three attempts."""
    failed = _agent("agent_jordanh-worker6")
    fresh = _agent("agent_jordanh-worker5")
    task = _task(avoid={"agent_jordanh-worker6"})

    order = _rank(allocator, task, [failed, fresh])

    assert order[0].id == "agent_jordanh-worker5", (
        "the agent that just failed this task was still ranked first"
    )


def test_the_failed_agent_remains_eligible(allocator):
    """Soft, not excluded. This is the half that prevents a deadlock."""
    failed = _agent("agent_rocky")
    task = _task(avoid={"agent_rocky"})

    order = _rank(allocator, task, [failed])

    assert order == [failed], (
        "the only capable agent was ranked away entirely; in a finite pool "
        "that is how a task becomes permanently undispatchable"
    )


def test_a_sole_avoided_agent_is_still_chosen_over_nothing(allocator):
    """A retry on the same host beats a task that can never run again."""
    failed = _agent("agent_rocky", capabilities=("python", "metal"))
    incapable = _agent("agent_worker", capabilities=("python",))
    task = _task(avoid={"agent_rocky"}, capabilities={"metal"})

    eligible = [a for a in (failed, incapable) if task.required_capabilities <= a.capabilities]

    assert [a.id for a in eligible] == ["agent_rocky"]
    assert _rank(allocator, task, eligible)[0].id == "agent_rocky"


def test_avoidance_does_not_outrank_project_affinity(allocator):
    """Affinity sorts before prior participation, and should stay that way.

    An agent bound to the task's project is a stronger signal than "has not
    tried yet": moving work off its owning project to dodge a retry would
    trade a small win for a larger loss.
    """
    affine_but_failed = _agent("agent_a", projects=("mac",))
    fresh_elsewhere = _agent("agent_b", projects=("other",))
    task = _task(avoid={"agent_a"}, project="mac")

    order = _rank(allocator, task, [affine_but_failed, fresh_elsewhere])

    assert order[0].id == "agent_a"


def test_two_previously_failed_agents_still_rank_by_load(allocator):
    """When everything has failed, fall back to the ordinary signals."""
    busy = _agent("agent_a", capacity=2, active=2)
    idle = _agent("agent_b", capacity=2, active=0)
    task = _task(avoid={"agent_a", "agent_b"})

    order = _rank(allocator, task, [busy, idle])

    assert order[0].id == "agent_b", "avoidance swamped the load signal"


# --------------------------------------------------------------------------
# The wiring, which is the part that was actually missing.
#
# The ranking above already worked; nothing fed it. These tests fail if the
# union in task_lifecycle is removed, which the ranking tests do not -- they
# pass happily against the unfixed code, so on their own they would have been
# a fix-shaped test of an unfixed system.
# --------------------------------------------------------------------------


def _worker(cp, name):
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(machine.id, name, capabilities=["python"])


def test_an_agent_that_leased_this_task_lands_in_the_avoid_set():
    """A lease is the durable record that this agent already had a go."""
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    agent = _worker(cp, "worker-a")
    task = cp.create_task("do useful work", project="mac", required_capabilities=["python"])

    assert cp._prior_attempt_agent_ids(task) == set(), "no attempts yet"

    cp.claim_task_v2(task.id, agent.id, lease_seconds=120)

    assert cp._prior_attempt_agent_ids(cp.get_task(task.id)) == {agent.id}, (
        "the agent that just attempted this task is not recorded, so a retry "
        "has no way to prefer anyone else"
    )


def test_a_task_nobody_has_touched_avoids_nobody():
    """Avoidance must not fire on a first dispatch."""
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    _worker(cp, "worker-a")
    task = cp.create_task("fresh work", project="mac", required_capabilities=["python"])

    assert cp._prior_attempt_agent_ids(task) == set()


def test_prior_attempts_reach_the_allocation_snapshot():
    """End to end: a leased task carries its prior agent into dispatch input.

    This is the assertion that fails if the union in task_lifecycle is dropped.
    """
    from mac.services import ControlPlane

    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    agent = _worker(cp, "worker-a")
    _worker(cp, "worker-b")
    task = cp.create_task("do useful work", project="mac", required_capabilities=["python"])
    cp.claim_task_v2(task.id, agent.id, lease_seconds=120)

    reloaded = cp.get_task(task.id)
    avoided = cp._coordination_excluded_agent_ids(reloaded) | cp._prior_attempt_agent_ids(reloaded)

    assert agent.id in avoided, (
        "the failed agent never reaches avoid_agent_ids, so the allocator's "
        "prior_participation signal stays unfed and retries repeat the host"
    )
