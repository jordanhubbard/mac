"""Wait for a project's work to finish, reporting each transition as it happens.

`mac task list` answers "what is the state now". Nothing answered "tell me when
this project is done", so the only way to find out was to poll the list by hand
and squint at it -- which is what a person does while a fleet works, and it is
both tedious and easy to get wrong when a task decomposes into children.

WHAT IS WAITED ON

The wait set is every task that is active or can BECOME active. Three states
are excluded, and each for a different reason:

* ``cancelled`` / ``completed`` / ``failed`` -- terminal. Nothing more happens.
* ``blocked`` -- it cannot proceed until something outside this wait changes.
* ``needs_input`` -- it is waiting on a HUMAN. Waiting on it would mean waiting
  on the person who is running the wait.

``waiting`` is deliberately NOT excluded. It reads like "waiting for input" and
is not: it is a dependency wait, which resolves on its own as the dependency
finishes. Excluding it would drop exactly the tasks a wait exists to watch.

A task leaves the set on the same conditions, so the set only shrinks and the
wait terminates. A task that becomes blocked or needs_input leaves rather than
stalling the wait forever -- with its departure reported, because "we stopped
waiting on this" is information the caller needs, not a detail to swallow.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

WAIT_SCHEMA = "mac.task_wait.v1"

#: Terminal: the task is finished, whatever the outcome.
FINISHED_STATES = frozenset({"completed", "failed", "cancelled"})

#: Not finished, but not going to progress on its own either. Waiting on these
#: would mean waiting on something this wait cannot observe -- a dependency
#: outside the project, or the person running the command.
STALLED_STATES = frozenset({"blocked", "needs_input"})

#: Everything the wait actually watches: active, or able to become active.
WAITABLE_STATES = frozenset({"open", "waiting", "claimed", "running", "needs_review", "reviewing"})

#: Why a task left the wait set, kept apart because they mean different things
#: to whoever is waiting.
LEFT_FINISHED = "finished"
LEFT_STALLED = "stalled"


def is_waitable(state: Any) -> bool:
    return str(state or "").strip() in WAITABLE_STATES


def departure_reason(state: Any) -> Optional[str]:
    """Why this state leaves the wait set, or None if it stays."""
    text = str(state or "").strip()
    if text in FINISHED_STATES:
        return LEFT_FINISHED
    if text in STALLED_STATES:
        return LEFT_STALLED
    return None


def waitable_tasks(tasks: Iterable[Any], *, project: Optional[str] = None) -> Dict[str, str]:
    """The initial wait set: task id -> state."""
    pending: Dict[str, str] = {}
    for task in tasks:
        record = task.to_dict() if hasattr(task, "to_dict") else task
        if not isinstance(record, Mapping):
            continue
        if project is not None and str(record.get("project") or "") != project:
            continue
        task_id = str(record.get("id") or "").strip()
        state = str(record.get("state") or "").strip()
        if task_id and is_waitable(state):
            pending[task_id] = state
    return pending


class TaskWait:
    """The wait set, advanced by events and by rescans.

    Deliberately holds no I/O. How events arrive -- polling, SSE, a websocket --
    is a transport question, and keeping it out of here means the termination
    rules can be tested without one.
    """

    def __init__(
        self,
        pending: Mapping[str, str],
        *,
        follow_new: bool = True,
    ) -> None:
        self.pending: Dict[str, str] = dict(pending)
        # Tasks that were in the set and have left. Kept so a rescan does not
        # re-add a task that already finished, which would make the wait
        # oscillate and never return.
        self.departed: Dict[str, str] = {}
        self.follow_new = bool(follow_new)

    @property
    def done(self) -> bool:
        return not self.pending

    def observe(self, task_id: str, state: str) -> Optional[Dict[str, Any]]:
        """Record a task's current state. Returns an update, or None if nothing
        the caller needs to hear about changed."""
        task_id = str(task_id or "").strip()
        state = str(state or "").strip()
        if not task_id or task_id in self.departed:
            return None
        known = self.pending.get(task_id)
        if known is None:
            # A task the wait has not seen. Only join if it is waitable and the
            # caller asked to follow new work: a task that decomposes into
            # children mid-wait would otherwise let the wait return while its
            # children are still running.
            if not (self.follow_new and is_waitable(state)):
                return None
            self.pending[task_id] = state
            return {"task_id": task_id, "state": state, "event": "joined"}
        reason = departure_reason(state)
        if reason is not None:
            del self.pending[task_id]
            self.departed[task_id] = reason
            return {
                "task_id": task_id,
                "state": state,
                "event": "left",
                "reason": reason,
                "from_state": known,
            }
        if state != known:
            self.pending[task_id] = state
            return {
                "task_id": task_id,
                "state": state,
                "event": "transitioned",
                "from_state": known,
            }
        return None

    def apply_event(self, event: Any) -> Optional[Dict[str, Any]]:
        """Advance the set from one ``task.transitioned`` event."""
        record = event.to_dict() if hasattr(event, "to_dict") else event
        if not isinstance(record, Mapping):
            return None
        if str(record.get("event_type") or "") != "task.transitioned":
            return None
        detail = record.get("detail")
        if not isinstance(detail, Mapping):
            return None
        to_state = str(detail.get("to_state") or "").strip()
        if not to_state:
            return None
        update = self.observe(str(record.get("subject_id") or ""), to_state)
        if update is not None:
            update["at"] = record.get("created_at")
            update["actor"] = record.get("actor")
        return update

    def rescan(
        self, tasks: Iterable[Any], *, project: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Reconcile against authoritative state.

        Events can be missed -- a hub restart, a dropped poll, a transition
        recorded without an event. Without a periodic rescan the wait would
        hang forever on a task that finished while nobody was listening, which
        is the failure mode that makes people distrust a wait command and go
        back to polling by hand.
        """
        updates: List[Dict[str, Any]] = []
        for task in tasks:
            record = task.to_dict() if hasattr(task, "to_dict") else task
            if not isinstance(record, Mapping):
                continue
            if project is not None and str(record.get("project") or "") != project:
                continue
            update = self.observe(str(record.get("id") or ""), str(record.get("state") or ""))
            if update is not None:
                update["source"] = "rescan"
                updates.append(update)
        return updates

    def summary(self) -> Dict[str, Any]:
        finished = sorted(k for k, v in self.departed.items() if v == LEFT_FINISHED)
        stalled = sorted(k for k, v in self.departed.items() if v == LEFT_STALLED)
        return {
            "schema": WAIT_SCHEMA,
            "done": self.done,
            "finished": finished,
            # Surfaced, never buried: the wait returned, but these did not
            # finish -- they stopped being things this wait could observe.
            "stalled": stalled,
            "still_pending": sorted(self.pending),
        }


def dedupe_events(events: Sequence[Any], seen: Set[str]) -> List[Any]:
    """Events not yet applied, marking them seen.

    The cursor is a timestamp, so a poll that resumes from the newest event's
    time re-delivers everything sharing that timestamp. Advancing past it
    instead would drop events recorded in the same tick.
    """
    fresh = []
    for event in events:
        record = event.to_dict() if hasattr(event, "to_dict") else event
        if not isinstance(record, Mapping):
            continue
        event_id = str(record.get("id") or "").strip()
        if not event_id or event_id in seen:
            continue
        seen.add(event_id)
        fresh.append(record)
    return fresh
