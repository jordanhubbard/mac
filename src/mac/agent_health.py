"""One definition of "is this agent dispatch-ready".

There were three, and they drifted, which is the whole reason this module
exists.

An agent's startup self-test can report a problem that is advisory rather than
blocking -- an OpenClaw probe that cannot find a stale sandbox, say. An agent
in that state is `health_status == "degraded"` and is still perfectly able to
run a coding task, so the fleet has always intended to dispatch to it. Three
places implemented that intention independently:

* ``ControlPlane._advisory_health_dispatch_ready`` -- the allocator's gate;
* the executor-release check in ``services`` -- accepted ``passed`` too;
* ``AllocationAgent.from_record`` in ``allocator`` -- inlined the rule again.

The allocator's own comment anticipated the hazard exactly: "disagreeing here
would let the hub offer an agent that the policy layer then refuses, once per
round, forever." It then duplicated the rule anyway, and on 2026-08-20 they did
disagree: two were fixed to accept a ``passed`` self-test and the third was
not, so a worker whose self-test PASSED with no blocking problems stayed
benched beside 80 open tasks. Fixing two of three implementations changed
nothing observable, which is the worst outcome available -- the work looked
done.

So: one predicate, imported by everyone. This module deliberately sits below
both ``services`` and ``allocator`` (``services`` imports ``allocator``, so the
shared rule cannot live in either) and imports nothing from mac, which is what
keeps it importable from anywhere.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = [
    "SELF_TEST_SCHEMA",
    "SELF_TEST_CLEARING_STATUSES",
    "HEALTHY",
    "DEGRADED",
    "startup_self_test_clears_dispatch",
    "advisory_health_dispatch_ready",
]

SELF_TEST_SCHEMA = "mac.agent_startup_self_test.v1"

HEALTHY = "healthy"
DEGRADED = "degraded"

#: A self-test that PASSED is strictly safer than one that came back degraded,
#: so accepting only "degraded" rejects the healthiest possible result. That
#: was the bug: a worker reporting `passed` with no blocking problems failed
#: the gate written to RELEASE degraded workers.
SELF_TEST_CLEARING_STATUSES = frozenset({"passed", DEGRADED})


def startup_self_test_clears_dispatch(
    startup: Any, *, agent_id: str
) -> bool:
    """True when this startup self-test clears its agent for dispatch.

    Requires the reviewed schema, a self-test belonging to THIS agent, a
    clearing status, and no blocking problems. Anything else -- a stale schema,
    another agent's report, a failure, or any blocking problem -- is refused.
    """

    if not isinstance(startup, Mapping):
        return False
    return bool(
        startup.get("schema") == SELF_TEST_SCHEMA
        and startup.get("agent_id") == agent_id
        and startup.get("status") in SELF_TEST_CLEARING_STATUSES
        and startup.get("blocking_problems") == []
    )


def advisory_health_dispatch_ready(
    health_status: Optional[str],
    resources: Any,
    *,
    agent_id: str,
) -> bool:
    """Whether an agent's health permits dispatching a task to it.

    Healthy always qualifies. Degraded qualifies only when the agent's own
    startup self-test clears it. Every other health value is refused.

    This is the WHOLE rule, so callers pass raw fields rather than reproducing
    any part of it -- reproducing part of it is how the three copies happened.
    """

    health = str(health_status or "").strip().lower()
    if health == HEALTHY:
        return True
    if health != DEGRADED:
        return False
    startup = resources.get("startup_self_test") if isinstance(resources, Mapping) else None
    return startup_self_test_clears_dispatch(startup, agent_id=agent_id)
