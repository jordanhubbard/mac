"""AgentBus broadcast (observation) channel.

The bus has always been *addressed*: a sender opens a stream toward a
recipient and only those two (or the named group) can read it. That makes the
fleet a set of private conversations. What it does not give any agent is the
one thing concurrent agents most need — awareness of what the others are
doing right now, in the same repositories, on the same branches.

This module adds the other half: a fleet-readable, typed event feed. An agent
announces what it is doing (``task.claimed``, ``git.worktree_added``,
``git.pushed``); every other agent, and the hub, can hear it.

It also answers the question a bus has to answer before any of that is
useful: ``roll_call()`` — who is connected, and what can each of them do.

Three properties are deliberate:

* **Everyone hears everything.** A broadcast is not addressed and confers no
  special standing on any listener. Acting on what you hear is allowed and is
  the point; the fleet convention is only that an agent does not *answer* a
  question until addressed by name, which is a convention and not enforced
  here.
* **A closed vocabulary.** ``BROADCAST_EVENT_TYPES`` is a small fixed set.
  Unknown types are rejected rather than silently accepted, because a bus
  whose vocabulary anyone can extend at publish time cannot be consumed.
* **Bounded volume.** This is a firehose candidate and the precedent is bad:
  ``action_events`` reached 10.4M rows / 16GB and wedged the hub. So events
  are per-agent rate-limited, coalesced, payload-capped, and — critically —
  stored in ``observability_events``, which is an EXISTING retention record
  class with an enabled default policy (see
  ``ControlPlane._RETENTION_TICK_CLASSES``). No new unreachable class is
  introduced; retention already prunes this data on the schedule operators
  already configured.

Concurrency note: the rate-limiter state is guarded by a short-lived
in-process lock that is released BEFORE any database write. No lock is held
across I/O.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from mac.models import (
    JsonDict,
    ValidationError,
    json_dumps,
    utcnow,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The closed set of broadcast event types. Resist growing this: the value of a
#: shared vocabulary is that every consumer already understands all of it.
BROADCAST_EVENT_TYPES: Tuple[str, ...] = (
    # What work an agent has taken, is making progress on, or has given back.
    "task.claimed",
    "task.progress",
    "task.released",
    # Coarse "which project am I in" signal, for agents that only need to know
    # whether anyone else is in the same tree.
    "project.attention",
    # Git as a shared workspace: the events that make one agent's private
    # checkout legible to the others.
    "git.branch_created",
    "git.worktree_added",
    "git.pushed",
    "git.merge_conflict",
    "git.force_push",
    # ...and the TERMINAL half of the same story. Everything above describes
    # work starting or colliding; without these, nothing on the bus ever says
    # a change FINISHED, so a task could not learn that its own work had
    # already landed. That gap is not theoretical: eight duplicate pull
    # requests (#405, #437, #442, #443, #445-448) were opened against work
    # that was already merged.
    "git.pr_opened",
    # ``tree_sha`` is the load-bearing field on ``git.merged``. Every merge
    # this fleet performs is a SQUASH, which mints a new commit sha, so a
    # consumer keyed on the commit sha would miss every one of them. Tree
    # identity is what the native merge queue lands on (see
    # ``native_merge_queue.landing_is_safe``) and it survives squashing.
    "git.merged",
    # The canonical branch moved. Distinct from ``git.merged``: the merge is
    # about ONE task's work, this is about the trunk every other worktree was
    # cut from. A worker that hears it and recognises its own base knows it
    # must rebase before it pushes, instead of discovering it at push time.
    "git.canonical_advanced",
    # Capacity pressure, consumed by the HGX autoscaler.
    "capacity.saturated",
    # The sandbox guardrail moved. ``sandbox.policy_changed`` says WHICH
    # direction it moved in (see mac.openshell_policy_diff); a worker that
    # hears a revocation must not start new work in the sandbox it built from
    # the superseded policy.
    "sandbox.policy_changed",
    # ...and the terminating event for the wait that follows. Without it,
    # "wait for the new policy" has no completion signal and degrades into an
    # arbitrary timeout poll — the worker would either resume too early, under
    # the policy it was told to stop using, or sit out a fixed sleep that has
    # nothing to do with when the policy actually landed.
    "sandbox.policy_published",
)

BROADCAST_EVENT_TYPE_SET = frozenset(BROADCAST_EVENT_TYPES)

#: Observability layer every broadcast is filed under. One layer keeps the feed
#: query a single indexed predicate and keeps broadcasts distinguishable from
#: the ordinary worker/control-plane telemetry sharing the table.
BROADCAST_LAYER = "agentbus.broadcast"

#: Observability event-name prefix (``bcast.git.pushed``, ...).
BROADCAST_NAME_PREFIX = "bcast."

BROADCAST_SCHEMA = "mac.agentbus.broadcast.v1"

#: Roll call: the bus-wide roster of who is connected and what they can do.
ROLL_CALL_SCHEMA = "mac.agentbus.roll_call.v1"

# ---------------------------------------------------------------------------
# Volume bounds
# ---------------------------------------------------------------------------

#: Serialized payload cap. Broadcasts are announcements, not transport; a big
#: body belongs on an addressed stream where exactly one reader pays for it.
BROADCAST_MAX_PAYLOAD_BYTES = 2048
#: Maximum payload keys retained.
BROADCAST_MAX_PAYLOAD_KEYS = 24
#: Maximum length of any single string payload value.
BROADCAST_MAX_VALUE_CHARS = 256

#: Per-agent token bucket: at most this many events per window.
BROADCAST_RATE_LIMIT_EVENTS = 60
BROADCAST_RATE_LIMIT_WINDOW_SECONDS = 60.0

#: Identical events (same agent, type, and coalesce key) inside this window
#: collapse into the first one. A worker looping on progress in a tight retry
#: cannot turn that loop into rows.
BROADCAST_COALESCE_SECONDS = 10.0

#: Payload fields that identify "the same event happening again".
#:
#: ``policy_id``/``to_checksum``/``change_kind`` are here for the sandbox
#: policy events: they carry none of the git-shaped fields, so without them
#: every policy change inside the coalesce window would look like a repeat of
#: the first one and the later — possibly restricting — change would be
#: dropped. Coalescing may suppress noise; it must never suppress a distinct
#: guardrail decision.
#:
#: ``tree_sha``/``pr_number``/``repository``/``canonical_branch`` are here for
#: the terminal git events for the same reason. Two squash merges landing on
#: the same branch inside the window are DIFFERENT facts, and the field that
#: distinguishes them for a consumer is the resulting tree — the commit sha is
#: minted fresh by the squash, which is precisely why tree identity is what
#: the merge queue trusts.
BROADCAST_COALESCE_FIELDS = (
    "project",
    "task_id",
    "branch",
    "sha",
    "worktree",
    "policy_id",
    "to_checksum",
    "change_kind",
    "tree_sha",
    "pr_number",
    "repository",
    "canonical_branch",
)

#: Emitter id for announcements the HUB makes about itself rather than on
#: behalf of an agent. It is not an agent row and cannot be, so system
#: announcements take :meth:`BroadcastService.publish_system` — an in-process
#: seam with no HTTP route behind it, so no token can publish as the hub.
BROADCAST_SYSTEM_AGENT_ID = "hub"

#: Event types the hub derives a ledger fact from. Deliberately the
#: low-frequency, high-consequence git events: one per publication attempt,
#: never per poll. The terminal pair belongs here for the same reason it
#: belongs on the bus at all — "this task's work landed" is exactly the fact
#: the ledger was missing when eight duplicate pull requests were opened.
LEDGER_DERIVING_EVENT_TYPES = frozenset(
    {"git.pushed", "git.merge_conflict", "git.pr_opened", "git.merged"}
)


def _coerce_scalar(value: Any) -> Tuple[Any, bool]:
    """Return ``(bounded_value, was_truncated)``.

    Truncation is reported rather than silent: a consumer that cannot tell a
    complete payload from a clipped one will eventually trust a clipped one.
    """
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value, False
    if value is None:
        return None, False
    text = str(value)
    if len(text) <= BROADCAST_MAX_VALUE_CHARS:
        return text, False
    return text[:BROADCAST_MAX_VALUE_CHARS], True


class BroadcastService:
    """Publishes and serves the fleet-readable broadcast feed.

    ``store`` is the mac Store; ``observability`` an ObservabilityService.
    ``derive_ledger_fact`` is an optional callable
    ``(event: JsonDict) -> Optional[str]`` invoked AFTER the event is durable,
    letting the hub act as a listener on its own bus.
    """

    def __init__(
        self,
        store: Any,
        observability: Any,
        *,
        derive_ledger_fact: Any = None,
        list_agents: Any = None,
    ) -> None:
        self.store = store
        self.observability = observability
        self._derive_ledger_fact = derive_ledger_fact
        self._list_agents_fn = list_agents
        # Rate-limiter state. Guarded by _lock, which is NEVER held across a
        # database write, a network call, or a git operation.
        self._lock = threading.Lock()
        self._window_start: Dict[str, float] = {}
        self._window_count: Dict[str, int] = {}
        self._coalesce_seen: Dict[Tuple[str, str, str], float] = {}
        self._dropped: Dict[str, int] = {}

    # -- publishing ------------------------------------------------------

    def publish(
        self,
        agent_id: str,
        event_type: str,
        *,
        project: Optional[str] = None,
        task_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> JsonDict:
        """Announce ``event_type`` to the fleet.

        Returns an envelope describing what happened to the announcement:
        ``accepted`` false with a ``reason`` of ``coalesced`` or
        ``rate_limited`` is a NORMAL outcome, not an error — the caller is
        telemetry-shaped and must not treat suppression as failure.
        """
        return self._publish(
            agent_id,
            event_type,
            project=project,
            task_id=task_id,
            payload=payload,
            require_agent=True,
        )

    def publish_system(
        self,
        event_type: str,
        *,
        project: Optional[str] = None,
        task_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> JsonDict:
        """Announce something the HUB did, as the hub.

        Used for facts no agent is in a position to state — a guardrail policy
        being republished or reassigned happens in the control plane, and
        attributing it to the affected agent would make ``agent_id`` a lie and
        make the affected agent skip its own "echo".

        Deliberately not reachable over HTTP: the route calls :meth:`publish`,
        which still requires a real agent row, so a token cannot speak as the
        hub.
        """
        return self._publish(
            BROADCAST_SYSTEM_AGENT_ID,
            event_type,
            project=project,
            task_id=task_id,
            payload=payload,
            require_agent=False,
        )

    def _publish(
        self,
        agent_id: str,
        event_type: str,
        *,
        project: Optional[str],
        task_id: Optional[str],
        payload: Optional[Dict[str, Any]],
        require_agent: bool,
    ) -> JsonDict:
        event_type = str(event_type or "").strip()
        if event_type not in BROADCAST_EVENT_TYPE_SET:
            raise ValidationError(
                "unknown broadcast event_type: %s (allowed: %s)"
                % (event_type, ", ".join(BROADCAST_EVENT_TYPES))
            )
        if require_agent:
            self._require_agent(agent_id)
        project_value = (str(project).strip() or None) if project else None
        task_value = (str(task_id).strip() or None) if task_id else None
        body = self._bounded_payload(payload, project_value, task_value)

        decision, reason = self._admit(agent_id, event_type, body)
        if not decision:
            return {
                "schema": BROADCAST_SCHEMA,
                "accepted": False,
                "reason": reason,
                "event_type": event_type,
                "agent_id": agent_id,
            }

        detail = {
            "schema": BROADCAST_SCHEMA,
            "event_type": event_type,
            "agent_id": agent_id,
            "project": project_value,
            "task_id": task_value,
            "payload": body,
        }
        # The lock is released by now: the write below is the only I/O.
        event = self.observability.record_log(
            BROADCAST_NAME_PREFIX + event_type,
            layer=BROADCAST_LAYER,
            source=agent_id,
            subject_type="task" if task_value else "agent",
            subject_id=task_value or agent_id,
            detail=detail,
        )
        envelope: JsonDict = {
            "schema": BROADCAST_SCHEMA,
            "accepted": True,
            "reason": "",
            "event_type": event_type,
            "agent_id": agent_id,
            "project": project_value,
            "task_id": task_value,
            "payload": body,
            "sequence": int(getattr(event, "sequence", 0) or 0) if event else 0,
            "created_at": getattr(event, "created_at", None) if event else utcnow(),
        }
        envelope["derived"] = self._derive(envelope)
        return envelope

    def _derive(self, envelope: JsonDict) -> List[str]:
        """Let the hub take a fact off the wire.

        The hub is just another listener, and this is the point of the
        exercise: it learns that a branch was published from HEARING the
        worker say so, not from a second call the worker had to remember to
        make.
        """
        if self._derive_ledger_fact is None:
            return []
        if envelope.get("event_type") not in LEDGER_DERIVING_EVENT_TYPES:
            return []
        if not envelope.get("task_id"):
            return []
        try:
            derived = self._derive_ledger_fact(envelope)
        except Exception:  # noqa: BLE001 - listening must never break publishing.
            return []
        if not derived:
            return []
        return [str(derived)]

    # -- reading ---------------------------------------------------------

    #: How far a filtered read scans forward before giving up for this call.
    #: Bounded so one request cannot walk the whole table.
    MAX_FEED_SCAN_PAGES = 10

    def read(
        self,
        observer_agent_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        event_types: Optional[List[str]] = None,
        project: Optional[str] = None,
    ) -> List[JsonDict]:
        """The broadcast feed as ``observer_agent_id`` sees it.

        Entries carry ``self_emitted`` so a consumer can skip its own echo.
        Nothing here restricts what a listener may do with what it hears: a
        broadcast is a fact about the fleet, and an agent that learns a peer
        holds the branch it was about to force-push SHOULD act on that.

        Filters are applied after the read, so a narrow filter over a busy feed
        can find nothing in the first page. We scan forward across a bounded
        number of pages rather than returning empty: a caller resuming from its
        last RETURNED sequence would otherwise re-read the same filtered block
        forever and never reach the events it asked for.
        """
        self._require_agent(observer_agent_id)
        wanted = set(event_types or [])
        for name in wanted:
            if name not in BROADCAST_EVENT_TYPE_SET:
                raise ValidationError("unknown broadcast event_type: %s" % name)
        page_size = max(1, min(int(limit), 500))
        cursor = max(0, int(after_sequence))
        feed: List[JsonDict] = []
        for _page in range(self.MAX_FEED_SCAN_PAGES):
            events = self.observability.list_observability(
                layer=BROADCAST_LAYER,
                after_sequence=cursor,
                limit=page_size,
            )
            if not events:
                break
            cursor = int(events[-1].sequence or cursor)
            for event in events:
                detail = event.detail if isinstance(event.detail, dict) else {}
                event_type = str(detail.get("event_type") or "")
                if wanted and event_type not in wanted:
                    continue
                if project and str(detail.get("project") or "") != project:
                    continue
                emitter = str(detail.get("agent_id") or event.source or "")
                feed.append(
                    {
                        "schema": BROADCAST_SCHEMA,
                        "sequence": int(event.sequence or 0),
                        "created_at": event.created_at,
                        "event_type": event_type,
                        "agent_id": emitter,
                        "project": detail.get("project"),
                        "task_id": detail.get("task_id"),
                        "payload": detail.get("payload") or {},
                        "self_emitted": emitter == observer_agent_id,
                    }
                )
            if feed or len(events) < page_size:
                break
        return feed[:page_size]

    # -- roll call -------------------------------------------------------

    def roll_call(self, *, include_departed: bool = False) -> JsonDict:
        """Who is on the bus, and what can each of them do.

        The hub has always known this; no caller could ask. That mattered
        while the hub matched capabilities to agents, because only the hub
        needed the answer. It stops being true the moment agents PULL work and
        decide for themselves what they can take: an agent that cannot ask who
        else is out there, and what they can do, cannot make that decision --
        it can only wait to be told, which is the arrangement being replaced.

        Built on ``agent_reflection_payload``'s inventory shape (the existing
        per-agent description) rather than a second, divergent format.
        """
        from mac.agentbus_control import agent_reflection_payload

        agents = self._list_agents(include_departed=include_departed)
        roster: List[JsonDict] = []
        for agent in agents:
            record = agent.to_dict() if hasattr(agent, "to_dict") else dict(agent)
            inventory = agent_reflection_payload(agent=record)["agent"]
            roster.append(inventory)
        return {
            "schema": ROLL_CALL_SCHEMA,
            "counted_at": utcnow(),
            "agent_count": len(roster),
            "agents": roster,
        }

    # -- bounding --------------------------------------------------------

    def _bounded_payload(
        self,
        payload: Optional[Dict[str, Any]],
        project: Optional[str],
        task_id: Optional[str],
    ) -> JsonDict:
        truncated = False
        if payload is None:
            body: JsonDict = {}
        elif isinstance(payload, dict):
            body = {}
            keys = list(payload)
            truncated = len(keys) > BROADCAST_MAX_PAYLOAD_KEYS
            for key in keys[:BROADCAST_MAX_PAYLOAD_KEYS]:
                value, clipped = _coerce_scalar(payload[key])
                truncated = truncated or clipped
                body[str(key)[:64]] = value
        else:
            raise ValidationError("broadcast payload must be an object")
        if truncated:
            body["truncated"] = True
        if project and "project" not in body:
            body["project"] = project
        if task_id and "task_id" not in body:
            body["task_id"] = task_id
        if len(json_dumps(body).encode("utf-8")) <= BROADCAST_MAX_PAYLOAD_BYTES:
            return body
        # Over the cap: keep the identifying fields, drop the rest, and SAY so.
        # Silent truncation would make a consumer trust a partial payload.
        kept: JsonDict = {
            key: body[key] for key in BROADCAST_COALESCE_FIELDS if key in body
        }
        kept["truncated"] = True
        return kept

    def _admit(
        self, agent_id: str, event_type: str, body: JsonDict
    ) -> Tuple[bool, str]:
        """Decide whether this announcement gets a row. Lock-scoped and pure."""
        key = (
            agent_id,
            event_type,
            json_dumps(
                {
                    field: body.get(field)
                    for field in BROADCAST_COALESCE_FIELDS
                    if body.get(field) is not None
                }
            ),
        )
        now = time.monotonic()
        with self._lock:
            seen_at = self._coalesce_seen.get(key)
            if seen_at is not None and now - seen_at < BROADCAST_COALESCE_SECONDS:
                self._dropped[agent_id] = self._dropped.get(agent_id, 0) + 1
                return False, "coalesced"
            started = self._window_start.get(agent_id)
            if started is None or now - started >= BROADCAST_RATE_LIMIT_WINDOW_SECONDS:
                self._window_start[agent_id] = now
                self._window_count[agent_id] = 0
            if self._window_count.get(agent_id, 0) >= BROADCAST_RATE_LIMIT_EVENTS:
                self._dropped[agent_id] = self._dropped.get(agent_id, 0) + 1
                return False, "rate_limited"
            self._window_count[agent_id] = self._window_count.get(agent_id, 0) + 1
            self._coalesce_seen[key] = now
            self._prune_coalesce_state(now)
            return True, ""

    def _prune_coalesce_state(self, now: float) -> None:
        """Keep the dedupe map from becoming the leak it exists to prevent.

        Called with the lock held; O(n) only once the map is large.
        """
        if len(self._coalesce_seen) < 4096:
            return
        cutoff = now - BROADCAST_COALESCE_SECONDS
        for key in [k for k, seen in self._coalesce_seen.items() if seen < cutoff]:
            self._coalesce_seen.pop(key, None)

    def suppressed_count(self, agent_id: str) -> int:
        """How many announcements from ``agent_id`` the bounds have absorbed."""
        with self._lock:
            return int(self._dropped.get(agent_id, 0))

    # -- helpers ---------------------------------------------------------

    def _list_agents(self, *, include_departed: bool) -> List[Any]:
        if self._list_agents_fn is not None:
            return list(self._list_agents_fn(include_deleted=include_departed))
        sql = "SELECT * FROM agents"
        if not include_departed:
            sql += " WHERE deleted_at IS NULL"
        return list(self.store.query_all(sql + " ORDER BY name, id"))

    def _require_agent(self, agent_id: str) -> None:
        from mac.models import NotFoundError

        if not self.store.query_one("SELECT id FROM agents WHERE id = ?", (agent_id,)):
            raise NotFoundError("agent not found: %s" % agent_id)
