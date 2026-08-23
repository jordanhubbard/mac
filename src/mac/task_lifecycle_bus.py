"""Task lifecycle as bus traffic (task_7faf8e56).

The fleet exists to move tasks between states, and until now that was the one
thing the bus never carried. A topic census taken 2026-08-21 found eleven
distinct topics in fleet-wide traffic -- reflect requests, peer messages, repo
updates, human directives -- and no ``task.*`` topic at all. Nothing published
claimed / running / completed / failed / needs_review, so nothing could
subscribe to it, so every observer polled. An operator session registered as an
agent spent a working session re-running ``mac task stats`` and ``mac agent
list``; eleven tasks were filed and claimed within the hour and the operator
learned it only by asking again.

This module is the publisher side of the fix. Every task transition already
enqueues a ``task.lifecycle`` row on the transition outbox
(``TaskTransitionService._transition_task_impl``), which until now was drained
and dropped. That row is the seam: it is written inside the transition's
transaction, so it exists if and only if the transition committed, and it is
processed after commit, so a notification can never describe a state the
database does not hold.

Three decisions worth stating, because each had a plausible alternative:

**The topic is derived, not mapped.** ``task.<state>`` for every
:class:`~mac.models.TaskState`. A hand-written map (``running`` ->
``task.started``) reads better in prose and drifts the first time a state is
added, at which point a consumer subscribes to a topic that is never published.
Derivation makes the vocabulary closed by construction:
:data:`TASK_LIFECYCLE_TOPICS` is exactly the set of states.

**The sender is the operator persona, not the actor.** The hub has no agent
identity of its own -- ``agentbus_streams.sender_agent_id`` is a NOT NULL
foreign key into ``agents`` -- and most transitions are performed by actors that
are not agents at all (``human``, ``allocator``, ``outbox``, the host
finalizer). ``ControlPlane._ensure_operator_persona`` already exists for exactly
this problem and is already used to address directive activations from the hub,
so lifecycle records reuse it rather than mint a second virtual identity. The
true actor is never lost: it is a required field of the payload.

**Delivery is addressed, not broadcast.** The broadcast channel
(:mod:`mac.agentbus_broadcast`) is fleet-readable observation, retained on the
observability table and rate-limited. "Your task was cancelled" is not
observation -- it is addressed at someone who has to act, and the thing that
makes it retrievable by :meth:`AgentBusService.read_inbox` is a stream whose
recipient is that someone. So this opens a real stream toward the task's owner
and any watcher.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from mac.models import (
    JsonDict,
    NotFoundError,
    TaskState,
    ValidationError,
    ensure_json_object,
    json_dumps,
    utcnow,
)

#: Payload contract, registered in :mod:`mac.agentbus_schemas` so a malformed
#: lifecycle record is refused at publish time rather than discovered by a
#: consumer.
TASK_LIFECYCLE_SCHEMA = "mac.task.lifecycle.v1"

#: Content type carried by every lifecycle stream. Distinct from plain
#: ``application/json`` so a consumer can filter on the envelope alone.
TASK_LIFECYCLE_CONTENT_TYPE = "application/vnd.mac.task-lifecycle+json"

#: Topic prefix. Every lifecycle topic starts with this, so a subscriber can
#: match the whole family with one prefix test.
TASK_LIFECYCLE_TOPIC_PREFIX = "task."

#: The closed lifecycle vocabulary: one topic per task state, derived from the
#: state enum so the two cannot drift.
TASK_LIFECYCLE_TOPICS: Dict[str, str] = {
    state.value: TASK_LIFECYCLE_TOPIC_PREFIX + state.value + ".v1"
    for state in TaskState
}

TASK_LIFECYCLE_TOPIC_SET = frozenset(TASK_LIFECYCLE_TOPICS.values())

#: Task metadata key holding additional agent ids to notify.
#:
#: "Watcher" had no prior representation in this repository. It is defined here
#: as the smallest thing that answers the operator's complaint: a list of agent
#: ids under ``metadata["watchers"]``. An operator session that files a task and
#: wants to hear about it adds itself; nothing else in the system reads or
#: writes the key, so an unknown or stale entry costs one skipped recipient and
#: never an error.
TASK_WATCHERS_METADATA_KEY = "watchers"

#: Cap on notified recipients per transition. ``open_stream`` refuses group
#: streams above 32 participants, and the sender occupies one slot.
MAX_LIFECYCLE_RECIPIENTS = 31

#: Stream-id prefix. The id is derived from the outbox row so redelivery of the
#: same row collides on the primary key instead of publishing a duplicate.
LIFECYCLE_STREAM_ID_PREFIX = "tasklc."

#: Payload budget, deliberately well under the bus's 256 KB chunk ceiling.
#: Stated locally rather than imported so this module keeps depending on
#: ``mac.models`` alone, and so the margin is visible: the point is to trim the
#: detail bag long before the serializer would refuse the whole notification.
MAX_LIFECYCLE_PAYLOAD_BYTES = 64 * 1024

#: Outbox-detail key carrying who to notify, captured at ENQUEUE time.
#:
#: This exists because of the transitions that matter most. A terminal or
#: blocked transition clears ``tasks.owner_agent_id`` as part of releasing the
#: agent, and the outbox row is processed AFTER that commit -- so a publisher
#: that read the owner from the task would find None on exactly the events an
#: operator needs ("your task failed", "your task was blocked") and correctly
#: conclude there was nobody to tell. The audience is therefore resolved inside
#: the transition's own transaction, where the outgoing owner is still on the
#: row, and travels with the outbox row.
LIFECYCLE_AUDIENCE_KEY = "_lifecycle_audience"


def lifecycle_topic(state: Any) -> str:
    """The ``task.*`` topic for a destination state.

    Raises rather than inventing a topic for an unknown state: a consumer
    subscribes to a closed vocabulary, and silently minting ``task.<typo>``
    would produce traffic nothing is listening for.
    """
    value = state.value if hasattr(state, "value") else str(state or "")
    topic = TASK_LIFECYCLE_TOPICS.get(value)
    if topic is None:
        raise ValidationError("unknown task lifecycle state: %r" % (state,))
    return topic


def lifecycle_stream_id(outbox_id: str) -> str:
    """The deterministic stream id for one outbox row.

    The transition outbox is at-least-once: a row that fails mid-processing is
    retried, and ``drain`` can be called concurrently from more than one place.
    Deriving the stream id from the row id turns "publish twice" into a primary
    key collision the publisher can recognise and skip, which is cheaper and
    more honest than a dedupe table.
    """
    value = str(outbox_id or "").strip()
    if not value:
        raise ValidationError("task lifecycle stream id requires an outbox id")
    return LIFECYCLE_STREAM_ID_PREFIX + value


def task_watchers(metadata: Any) -> List[str]:
    """Agent ids listed under ``metadata["watchers"]``, order-stable and deduped.

    Tolerant by construction: task metadata is caller-supplied and a malformed
    watcher list must cost a missing notification, never a failed transition.
    """
    bag = ensure_json_object(metadata)
    raw = bag.get(TASK_WATCHERS_METADATA_KEY)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    seen: Dict[str, None] = {}
    for item in raw:
        candidate = str(item or "").strip()
        if candidate:
            seen.setdefault(candidate, None)
    return list(seen)


def lifecycle_recipients(
    owner_agent_id: Optional[str],
    watchers: Iterable[str],
    *,
    exclude: Iterable[str] = (),
) -> List[str]:
    """Who to address, owner first, deduped, minus ``exclude``.

    Owner first because a pair stream keeps ``recipient_agent_id`` as the
    primary addressee and that should be the agent whose work moved, not
    whichever watcher happened to register first.
    """
    excluded = {str(item or "").strip() for item in exclude}
    ordered: Dict[str, None] = {}
    for candidate in [owner_agent_id, *watchers]:
        value = str(candidate or "").strip()
        if value and value not in excluded:
            ordered.setdefault(value, None)
    return list(ordered)[:MAX_LIFECYCLE_RECIPIENTS]


def lifecycle_outbox_detail(detail: Optional[Dict[str, Any]], task: Any) -> JsonDict:
    """Stamp the notification audience onto an outgoing outbox detail bag.

    Called from inside the transition's transaction, with the task row as it
    stood BEFORE the transition, so a transition that releases the owner still
    knows who to tell. See :data:`LIFECYCLE_AUDIENCE_KEY`.
    """
    stamped = dict(ensure_json_object(detail))
    owner = str(getattr(task, "owner_agent_id", "") or "")
    watchers = task_watchers(getattr(task, "metadata", None))
    if owner or watchers:
        stamped[LIFECYCLE_AUDIENCE_KEY] = {
            "owner_agent_id": owner,
            "watchers": watchers,
        }
    return stamped


def pop_lifecycle_audience(detail: Any) -> Tuple[Optional[JsonDict], JsonDict]:
    """Split a stamped detail bag into ``(audience, detail_without_audience)``.

    The audience is plumbing, not part of the published record: the payload
    already states ``owner_agent_id`` and ``recipients`` in their own fields,
    and echoing an internal key into every consumer's copy would make it look
    like contract.
    """
    bag = dict(ensure_json_object(detail))
    audience = bag.pop(LIFECYCLE_AUDIENCE_KEY, None)
    if not isinstance(audience, dict):
        return None, bag
    owner = str(audience.get("owner_agent_id") or "") or None
    raw_watchers = audience.get("watchers")
    watchers = [
        str(item).strip()
        for item in (raw_watchers if isinstance(raw_watchers, (list, tuple)) else [])
        if str(item or "").strip()
    ]
    return {"owner_agent_id": owner, "watchers": watchers}, bag


def lifecycle_payload(
    *,
    task_id: str,
    topic: str,
    from_state: Optional[str],
    to_state: Optional[str],
    actor: str,
    owner_agent_id: Optional[str],
    recipients: List[str],
    project: Optional[str] = None,
    title: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    at: Optional[str] = None,
) -> JsonDict:
    """Build the ``mac.task.lifecycle.v1`` payload.

    ``detail`` is the transition's own detail bag, copied through unbounded in
    shape but not in size. The chunk serializer refuses a payload over 256 KB,
    and ``publish`` swallows that refusal -- so an oversized detail bag would
    cost the notification ENTIRELY rather than costing the bag. Detail is the
    least load-bearing field here (state, actor and task id carry the meaning),
    so it is the one that gets dropped when it does not fit.
    """
    payload: JsonDict = {
        "schema": TASK_LIFECYCLE_SCHEMA,
        "task_id": str(task_id),
        "topic": topic,
        "from_state": str(from_state or ""),
        "to_state": str(to_state or ""),
        "actor": str(actor or "unknown"),
        "owner_agent_id": str(owner_agent_id or ""),
        "recipients": list(recipients),
        "at": at or utcnow(),
    }
    if project:
        payload["project"] = str(project)
    if title:
        payload["title"] = str(title)[:500]
    detail_obj = ensure_json_object(detail)
    if detail_obj:
        payload["detail"] = detail_obj
        if len(json_dumps(payload).encode("utf-8")) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            payload["detail"] = {
                "omitted": "transition detail exceeded the lifecycle payload budget",
                "keys": sorted(str(key) for key in detail_obj)[:50],
            }
    return payload


class TaskLifecycleBusPublisher:
    """Turns a drained ``task.lifecycle`` outbox row into addressed bus traffic.

    Best-effort by contract. The outbox drain also advances dependency
    resolution and workflow runs; a bus write that fails must not take those
    down with it, and a transition that committed must not be re-run because
    nobody could be told about it. Every failure is recorded to observability
    and swallowed, and :meth:`publish` reports what it did rather than raising.
    """

    def __init__(self, control_plane: Any) -> None:
        self.control_plane = control_plane

    # -- public ---------------------------------------------------------

    def publish(
        self,
        *,
        outbox_id: str,
        task: Any,
        actor: str,
        from_state: Optional[str],
        to_state: Optional[str],
        detail: Optional[Dict[str, Any]] = None,
    ) -> JsonDict:
        """Publish one transition. Returns a status document, never raises."""
        try:
            return self._publish(
                outbox_id=outbox_id,
                task=task,
                actor=actor,
                from_state=from_state,
                to_state=to_state,
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 - notification must not fail a transition.
            self._record(
                "task.lifecycle.publish_failed",
                level="warning",
                task_id=getattr(task, "id", ""),
                detail={"error": str(exc), "outbox_id": str(outbox_id)},
            )
            return {"status": "error", "error": str(exc)}

    # -- internals ------------------------------------------------------

    def _publish(
        self,
        *,
        outbox_id: str,
        task: Any,
        actor: str,
        from_state: Optional[str],
        to_state: Optional[str],
        detail: Optional[Dict[str, Any]],
    ) -> JsonDict:
        if from_state and to_state and from_state == to_state:
            # A lifecycle record says work MOVED. The same row is also enqueued
            # to backfill a cancelled task's repository-ref disposition, where
            # nothing transitioned; publishing "task.cancelled" again for a
            # metadata correction would teach consumers to distrust the topic.
            return {"status": "skipped", "reason": "no_transition"}
        topic = lifecycle_topic(to_state or getattr(task, "state", ""))
        audience, detail = pop_lifecycle_audience(detail)
        if audience is None:
            # No stamp: an outbox row enqueued before this shipped, or by a
            # path that does not stamp. The live task is the best available
            # answer and is correct for every non-releasing transition.
            audience = {
                "owner_agent_id": getattr(task, "owner_agent_id", None),
                "watchers": task_watchers(getattr(task, "metadata", None)),
            }
        owner = audience.get("owner_agent_id")
        watchers = audience.get("watchers") or []
        recipients = [
            agent_id
            for agent_id in lifecycle_recipients(owner, watchers)
            if self._is_live_agent(agent_id)
        ]
        # Every operation is a fleet fact even when nobody is addressed. The
        # bounded broadcast table already has retention; addressed streams are
        # the notification copy for owners/watchers.
        self.control_plane.agentbus_broadcast.publish_system(
            "task.transitioned.v1",
            project=getattr(task, "project", None),
            task_id=getattr(task, "id", None),
            payload={
                "topic": topic,
                "from_state": str(from_state or ""),
                "to_state": str(to_state or ""),
                "actor": str(actor or ""),
            },
        )
        if not recipients:
            return {"status": "broadcast", "topic": topic, "recipients": []}

        sender = self._sender_agent_id()
        recipients = [agent_id for agent_id in recipients if agent_id != sender]
        if not recipients:
            return {"status": "broadcast", "topic": topic, "recipients": []}

        stream_id = lifecycle_stream_id(outbox_id)
        payload = lifecycle_payload(
            task_id=getattr(task, "id", ""),
            topic=topic,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            owner_agent_id=owner,
            recipients=recipients,
            project=getattr(task, "project", None),
            title=getattr(task, "title", None),
            detail=detail,
        )
        agentbus = self.control_plane.agentbus
        try:
            agentbus.open_stream(
                sender_agent_id=sender,
                recipient_agent_id=recipients[0],
                content_type=TASK_LIFECYCLE_CONTENT_TYPE,
                topic=topic,
                task_id=getattr(task, "id", None),
                stream_id=stream_id,
                participant_agent_ids=recipients[1:] or None,
                headers={
                    "task_id": str(getattr(task, "id", "")),
                    "to_state": str(to_state or ""),
                    "actor": str(actor or ""),
                },
            )
        except ValidationError as exc:
            if "already exists" not in str(exc):
                raise
            # Redelivered outbox row. Usually the record is already on the bus
            # and saying so is the correct outcome -- but not always: opening
            # the stream and appending its chunk are two writes, and a failure
            # between them leaves an EMPTY stream. Reporting that as a
            # duplicate would strand the notification forever behind an id
            # that can never be re-opened. So the empty case falls through and
            # finishes the job.
            if agentbus.read_chunks(
                sender, stream_id, 0, 1, record_observation=False
            ):
                return {
                    "status": "duplicate",
                    "stream_id": stream_id,
                    "topic": topic,
                    "recipients": recipients,
                }
        chunk = agentbus.append_chunk(
            stream_id,
            sender,
            payload=payload,
            content_type=TASK_LIFECYCLE_CONTENT_TYPE,
            final=True,
        )
        self._record(
            "task.lifecycle.published",
            level="info",
            task_id=str(getattr(task, "id", "")),
            detail={
                "stream_id": stream_id,
                "topic": topic,
                "recipients": recipients,
                "to_state": str(to_state or ""),
            },
        )
        return {
            "status": "published",
            "stream_id": stream_id,
            "chunk_id": chunk.id,
            "topic": topic,
            "recipients": recipients,
        }

    def _sender_agent_id(self) -> str:
        """The hub's virtual identity, materialized on first use."""
        persona = self.control_plane._ensure_operator_persona()
        return str(persona.id)

    def _is_live_agent(self, agent_id: str) -> bool:
        try:
            agent = self.control_plane.get_agent(agent_id)
        except NotFoundError:
            return False
        return not getattr(agent, "deleted_at", None)

    def _record(
        self,
        name: str,
        *,
        level: str,
        task_id: str,
        detail: Dict[str, Any],
    ) -> None:
        try:
            self.control_plane.record_log(
                name,
                layer="agentbus",
                source="task-lifecycle",
                level=level,
                subject_type="task" if task_id else None,
                subject_id=task_id or None,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - telemetry may be down; the caller still proceeds.
            pass
