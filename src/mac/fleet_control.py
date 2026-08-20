"""Fleet-wide stop / drain / start / status as first-class operations.

Stopping the fleet was possible long before this module existed -- the
authority was never the problem. What was missing was vocabulary. An operator
who wanted the fleet stopped had to know to compose it: snapshot every agent's
``dispatch_hold`` by hand so it could be restored later, iterate
``mac agent hold`` over every agent, discover that a hold does NOT drain, chase
the still-running tasks individually, and then answer "is it actually stopped?"
by piping ``mac agent list --json`` through a script. That is an improvisation
the operator must get right under pressure, which is exactly when improvisation
fails.

Three ideas make the composition unnecessary:

**Hold is not drain.** ``dispatch_hold`` stops NEW work being dispatched to an
agent; it says nothing about the task the agent is already executing. Those are
two different operations and this module keeps them separate:
:func:`fleet_stop` closes the door, and ``--drain`` additionally waits for the
agents already inside to finish, naming what it is waiting on the whole time.

**The snapshot lives on the hold, not beside it.** Restoring the pre-stop state
needs to know which agents were deliberately held BEFORE the stop, so that
``fleet start`` leaves those held. Keeping that in a file next to the operator
would strand it on one workstation and rot the moment anyone held an agent by
hand; keeping it in a new hub table would need a schema, a route and a
migration for one boolean per agent. Instead the fleet stop marks the holds it
places with :data:`FLEET_STOP_REASON_PREFIX`, and the marker IS the snapshot: a
hold without the marker was somebody else's decision and is left alone. That is
durable, shared by every operator, and self-repairing -- a hold placed by hand
during a stop is simply not ours to release.

**Everything composes over four primitives.** ``list_agents``,
``list_tasks``, ``set_agent_dispatch_hold`` and ``clear_agent_dispatch_hold``
exist identically on the local and remote planes, so these operations behave
the same against a database and against a hub, with no new route to keep in
sync.

The full pre-stop state is still reported (``snapshot`` in the stop result) so
the operator has the record; it is evidence, not the mechanism.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from mac.models import utcnow


#: Marker written into ``dispatch_hold_reason`` by :func:`fleet_stop`. A hold
#: carrying it was placed by a fleet stop and may be released by
#: :func:`fleet_start`; a hold without it was placed for some other reason and
#: outlives the stop.
FLEET_STOP_REASON_PREFIX = "fleet-stop"

STOP_SCHEMA = "mac.fleet_stop.v1"
START_SCHEMA = "mac.fleet_start.v1"
STATUS_SCHEMA = "mac.fleet_status.v1"
SNAPSHOT_SCHEMA = "mac.fleet_stop_snapshot.v1"

#: Task states in which an agent is actually executing work. ``needs_review``
#: and ``needs_input`` are deliberately absent: they are parked awaiting a
#: human, so a drain that waited for them would never finish.
IN_FLIGHT_TASK_STATES = ("claimed", "running")

DEFAULT_DRAIN_TIMEOUT_SECONDS = 900.0
DEFAULT_DRAIN_POLL_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _row(obj: Any) -> Dict[str, Any]:
    """Normalize a plane result to a plain dict.

    The local plane returns dataclasses, the remote plane returns dict-alikes;
    every caller here only ever reads fields.
    """
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return dict(obj)
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return dict(result) if isinstance(result, Mapping) else {}
    try:
        return dict(obj)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return {}


def fleet_stop_reason(reason: str) -> str:
    """Render the ``dispatch_hold_reason`` a fleet stop writes.

    The reason must round-trip through :func:`is_fleet_stop_reason`, because
    that is what tells ``fleet start`` which holds are its own to release.
    """
    text = str(reason or "").strip()
    if not text:
        text = "fleet stopped by operator"
    return "%s: %s" % (FLEET_STOP_REASON_PREFIX, text)


def is_fleet_stop_reason(reason: Optional[str]) -> bool:
    """True when *reason* names a hold placed by :func:`fleet_stop`."""
    return str(reason or "").strip().startswith(FLEET_STOP_REASON_PREFIX + ":")


def _agent_rows(plane: Any) -> List[Dict[str, Any]]:
    return [_row(agent) for agent in plane.list_agents()]


def agent_snapshot(agents: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Record every agent's pre-stop dispatch state.

    Reported rather than relied upon: the restore decision is driven by the
    hold marker (see the module docstring). This exists so the operator can
    still answer "what did it look like before I touched it?" from the output
    of the command that touched it.
    """
    return {
        "schema": SNAPSHOT_SCHEMA,
        "captured_at": utcnow(),
        "agents": [
            {
                "agent_id": str(row.get("id") or ""),
                "name": str(row.get("name") or ""),
                "status": str(row.get("status") or ""),
                "dispatch_hold": bool(row.get("dispatch_hold")),
                "dispatch_hold_reason": row.get("dispatch_hold_reason"),
                "dispatch_hold_at": row.get("dispatch_hold_at"),
                "current_task_id": row.get("current_task_id"),
            }
            for row in agents
        ],
    }


def _describe_in_flight(
    agents: Sequence[Mapping[str, Any]], tasks: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """Merge the two independent views of "work is executing right now".

    Both are needed. A task row is authoritative about the task's state but a
    task can be ``running`` while the agent field lags; an agent's
    ``current_task_id`` is authoritative about that agent but says nothing
    about work claimed by an agent that has since been removed. Taking the
    union means a drain never declares quiescence because it looked at only
    half the ledger.
    """
    by_agent = {
        str(row.get("id") or ""): row
        for row in agents
        if row.get("id")
    }
    entries: Dict[str, Dict[str, Any]] = {}

    for task in tasks:
        task_id = str(task.get("id") or "")
        if not task_id:
            continue
        agent_id = str(task.get("owner_agent_id") or "")
        entries[task_id] = {
            "task_id": task_id,
            "state": str(task.get("state") or ""),
            "title": str(task.get("title") or ""),
            "agent_id": agent_id or None,
            "agent_name": str(_row(by_agent.get(agent_id)).get("name") or "") or None,
        }

    for row in agents:
        task_id = str(row.get("current_task_id") or "")
        if not task_id:
            continue
        entry = entries.setdefault(
            task_id,
            {"task_id": task_id, "state": "", "title": "", "agent_id": None, "agent_name": None},
        )
        entry["agent_id"] = str(row.get("id") or "") or entry.get("agent_id")
        entry["agent_name"] = str(row.get("name") or "") or entry.get("agent_name")

    return sorted(entries.values(), key=lambda item: item["task_id"])


def _in_flight_tasks(plane: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for state in IN_FLIGHT_TASK_STATES:
        for task in plane.list_tasks(state=state) or []:
            rows.append(_row(task))
    return rows


def classify_state(dispatchable: int, in_flight_count: int) -> str:
    """Name the fleet's condition in one word.

    ``running`` whenever any agent can still be handed new work -- a partial
    hold is not a stop, and calling it one is how an operator concludes the
    fleet is safe when it is not. Once nothing can be dispatched, the fleet is
    ``draining`` while work it already accepted is still executing, and
    ``stopped`` only when it is not.
    """
    if dispatchable > 0:
        return "running"
    if in_flight_count > 0:
        return "draining"
    return "stopped"


def summarize(state: str, counts: Mapping[str, int], in_flight_count: int) -> str:
    """One operator-readable line; the answer to "is the fleet stopped?"."""
    return (
        "%s: %d/%d agents dispatchable, %d held (%d by fleet stop), "
        "%d task(s) in flight"
        % (
            state,
            int(counts.get("dispatchable", 0)),
            int(counts.get("total", 0)),
            int(counts.get("held", 0)),
            int(counts.get("held_by_fleet_stop", 0)),
            in_flight_count,
        )
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def fleet_status(plane: Any) -> Dict[str, Any]:
    """Answer "is the fleet stopped?" without composing anything."""
    agents = _agent_rows(plane)
    in_flight = _describe_in_flight(agents, _in_flight_tasks(plane))
    held = [row for row in agents if row.get("dispatch_hold")]
    counts = {
        "total": len(agents),
        "held": len(held),
        "held_by_fleet_stop": sum(
            1 for row in held if is_fleet_stop_reason(row.get("dispatch_hold_reason"))
        ),
        "dispatchable": len(agents) - len(held),
    }
    state = classify_state(counts["dispatchable"], len(in_flight))
    return {
        "schema": STATUS_SCHEMA,
        "state": state,
        "summary": summarize(state, counts, len(in_flight)),
        "agents": counts,
        "in_flight_count": len(in_flight),
        "in_flight": in_flight,
        "observed_at": utcnow(),
    }


def _drain(
    plane: Any,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    on_progress: Optional[Callable[[str], None]],
) -> Dict[str, Any]:
    """Wait for in-flight work to finish, naming what it waits on.

    A drain that reports only "still draining" is barely better than no drain:
    the operator's next question is always "waiting on what?", and answering it
    used to mean going back to the ledger by hand. Every poll therefore carries
    the task ids and the agents running them.
    """
    started = monotonic()
    polls = 0
    waiting_on: List[Dict[str, Any]] = []
    while True:
        polls += 1
        waiting_on = _describe_in_flight(_agent_rows(plane), _in_flight_tasks(plane))
        elapsed = monotonic() - started
        if not waiting_on:
            if on_progress is not None:
                on_progress("drain complete after %.1fs (%d poll(s))" % (elapsed, polls))
            return {
                "requested": True,
                "complete": True,
                "polls": polls,
                "waited_seconds": round(elapsed, 3),
                "waiting_on": [],
            }
        if on_progress is not None:
            on_progress(
                "draining (%.1fs elapsed): waiting on %s"
                % (elapsed, ", ".join(_render_wait(item) for item in waiting_on))
            )
        if elapsed >= timeout_seconds:
            return {
                "requested": True,
                "complete": False,
                "polls": polls,
                "waited_seconds": round(elapsed, 3),
                "waiting_on": waiting_on,
            }
        # Never sleep past the deadline: a long poll interval must not turn a
        # 60s timeout into a 5-minute one.
        sleep(max(0.0, min(poll_seconds, timeout_seconds - elapsed)))


def _render_wait(item: Mapping[str, Any]) -> str:
    agent = item.get("agent_name") or item.get("agent_id")
    state = item.get("state") or "in flight"
    if agent:
        return "%s (%s on %s)" % (item.get("task_id"), state, agent)
    return "%s (%s)" % (item.get("task_id"), state)


def fleet_stop(
    plane: Any,
    *,
    reason: str,
    drain: bool = False,
    timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_DRAIN_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Hold every agent, record why, and report the pre-stop state.

    Agents already held keep the reason they were held for -- overwriting it
    would destroy the only record of why, and ``fleet start`` would then
    release an agent somebody quarantined on purpose.

    With *drain*, additionally wait for in-flight work. The result's ``state``
    is ``draining`` rather than ``stopped`` whenever work is still executing,
    including when a drain times out, because a stop that reports success while
    tasks run is the failure this whole module exists to remove.
    """
    agents = _agent_rows(plane)
    snapshot = agent_snapshot(agents)
    hold_reason = fleet_stop_reason(reason)

    held: List[Dict[str, Any]] = []
    already_held: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for row in agents:
        agent_id = str(row.get("id") or "")
        if not agent_id:
            continue
        if row.get("dispatch_hold"):
            already_held.append(
                {
                    "agent_id": agent_id,
                    "name": str(row.get("name") or ""),
                    "reason": row.get("dispatch_hold_reason"),
                    "by_fleet_stop": is_fleet_stop_reason(row.get("dispatch_hold_reason")),
                }
            )
            continue
        try:
            plane.set_agent_dispatch_hold(agent_id, hold_reason)
        except Exception as exc:  # noqa: BLE001 - one unreachable agent is a partial stop, not a crash
            failed.append(
                {
                    "agent_id": agent_id,
                    "name": str(row.get("name") or ""),
                    "error": str(exc),
                }
            )
            continue
        held.append({"agent_id": agent_id, "name": str(row.get("name") or "")})

    status = fleet_status(plane)
    drain_result: Optional[Dict[str, Any]] = None
    if drain:
        drain_result = _drain(
            plane,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            sleep=sleep,
            monotonic=monotonic,
            on_progress=on_progress,
        )
        status = fleet_status(plane)

    return {
        "schema": STOP_SCHEMA,
        "stopped_at": snapshot["captured_at"],
        "reason": str(reason or "").strip(),
        "hold_reason": hold_reason,
        "held": held,
        "already_held": already_held,
        "failed": failed,
        "snapshot": snapshot,
        "drain": drain_result,
        "state": status["state"],
        "summary": status["summary"],
        "agents": status["agents"],
        "in_flight_count": status["in_flight_count"],
        "in_flight": status["in_flight"],
    }


def fleet_start(plane: Any, *, release_all: bool = False) -> Dict[str, Any]:
    """Restore the pre-stop state: release the fleet stop's holds, only those.

    An agent held before the stop -- quarantined for a bad disk, a bad build, a
    running investigation -- must stay held. Restoring every agent to "not
    held" is a different operation, and the usual outcome of performing it by
    accident is that the quarantined agent quietly starts taking work again.
    It is available as *release_all* because it is occasionally what you want,
    but it has to be asked for by name.
    """
    agents = _agent_rows(plane)
    released: List[Dict[str, Any]] = []
    kept_held: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for row in agents:
        agent_id = str(row.get("id") or "")
        if not agent_id or not row.get("dispatch_hold"):
            continue
        reason = row.get("dispatch_hold_reason")
        if not release_all and not is_fleet_stop_reason(reason):
            kept_held.append(
                {
                    "agent_id": agent_id,
                    "name": str(row.get("name") or ""),
                    "reason": reason,
                }
            )
            continue
        try:
            plane.clear_agent_dispatch_hold(agent_id)
        except Exception as exc:  # noqa: BLE001 - report the partial restore
            failed.append(
                {
                    "agent_id": agent_id,
                    "name": str(row.get("name") or ""),
                    "error": str(exc),
                }
            )
            continue
        released.append(
            {
                "agent_id": agent_id,
                "name": str(row.get("name") or ""),
                "reason": reason,
            }
        )

    status = fleet_status(plane)
    return {
        "schema": START_SCHEMA,
        "started_at": utcnow(),
        "release_all": bool(release_all),
        "released": released,
        "kept_held": kept_held,
        "failed": failed,
        "state": status["state"],
        "summary": status["summary"],
        "agents": status["agents"],
        "in_flight_count": status["in_flight_count"],
    }
