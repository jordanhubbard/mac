"""The hub must not vouch for an agent it merely decided to use.

Three HGX runners were stopped and their pods deleted. The hub went on
reporting them `busy`, with `last_seen_at` values seconds old and real tasks
attached -- tasks that could not run, on hosts that no longer existed.

The loop:

    a dead worker still looks idle (its silence has not aged out yet)
      -> the tick assigns it a task
        -> the assignment stamps last_seen_at = now
          -> the staleness sweep never fires, because the agent looks fresh
            -> it keeps drawing work, forever

Assignment is the hub acting. It is not evidence about the agent. Only the
agent can supply that: its own heartbeat, or a lease renewal it asked for.
"""

from __future__ import annotations

import pytest

from mac.services import ControlPlane


@pytest.fixture()
def cp():
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


def _agent(cp, name="probe"):
    machine = cp.register_machine("host-%s" % name)
    return cp.register_agent(machine.id, name)


def test_hub_assignment_does_not_refresh_liveness(cp):
    """The live failure. Without this the hub's own dispatch decision is
    laundered into evidence that the worker is alive."""
    agent = _agent(cp)
    task = cp.create_task(title="probe", project="mac")
    before = cp.get_agent(agent.id).last_seen_at

    cp.claim_task(
        task.id,
        agent.id,
        sync_beads=False,
        assignment_allocator="authoritative-hub",
    )

    after = cp.get_agent(agent.id).last_seen_at
    assert after == before, (
        "hub-side assignment refreshed the agent's liveness; a worker whose "
        "host is gone would keep looking alive and keep drawing work"
    )


def test_an_agent_initiated_claim_still_refreshes_liveness(cp):
    """An agent that claims is demonstrably there. Withholding the refresh
    would age out workers that are working."""
    agent = _agent(cp, name="selfclaim")
    task = cp.create_task(title="probe", project="mac")
    before = cp.get_agent(agent.id).last_seen_at

    cp.claim_task(task.id, agent.id, sync_beads=False)

    after = cp.get_agent(agent.id).last_seen_at
    assert after >= before
    assert after != before or before is None


def test_the_agent_still_becomes_busy_either_way(cp):
    """Liveness and assignment are separate facts; only the first is in
    dispute. The task still has to be attached."""
    agent = _agent(cp, name="busycheck")
    task = cp.create_task(title="probe", project="mac")

    cp.claim_task(
        task.id,
        agent.id,
        sync_beads=False,
        assignment_allocator="authoritative-hub",
    )

    refreshed = cp.get_agent(agent.id)
    assert refreshed.current_task_id == task.id
    assert str(refreshed.status) == "busy"
