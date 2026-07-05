"""Inbound A2A service: maps A2A JSON-RPC methods onto mac's task ledger.

This is the agent<->agent server (ACP roadmap Phase 4). An *external* A2A
client discovers mac via its AgentCard (:mod:`mac.a2a.card`) and delegates work
by calling A2A JSON-RPC methods at ``POST /a2a``. Each method maps onto mac's
existing task ledger via :class:`~mac.services.ControlPlane` -- there is **no
parallel task store**:

* ``message/send`` -- the message text becomes a new mac task (title +
  description); we return an A2A :class:`~mac.a2a.protocol.Task` in the
  ``submitted`` state whose ``id`` is the mac task id.
* ``tasks/get`` -- look up the mac task and project its state onto the A2A
  ``TaskState`` enum.
* ``tasks/cancel`` -- transition the mac task to ``cancelled`` and return the
  updated A2A Task.

:meth:`A2AService.handle_rpc` is the dispatcher: it returns a JSON-RPC 2.0
result/error envelope (a plain dict). It performs no transport I/O; the HTTP
layer in ``api.py`` reads the request body and writes the returned dict.

Deferred (out of scope this phase): ``message/stream`` (SSE streaming),
``tasks/resubscribe``, push notifications, and the *outbound* A2A client (mac
delegating to other agents).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from mac.models import MACError, NotFoundError, TransitionError, utcnow
from mac.models import TaskState as MacTaskState
from mac.services import ControlPlane

from .protocol import (
    ERROR_INTERNAL,
    ERROR_INVALID_PARAMS,
    ERROR_METHOD_NOT_FOUND,
    ERROR_TASK_NOT_FOUND,
    Message,
    Method,
    Role,
    Task,
    TaskState,
    TaskStatus,
    new_message_id,
    rpc_error,
    rpc_result,
    text_part,
)


__all__ = ["A2AService", "MAC_STATE_TO_A2A", "map_task_state"]


#: Projection of mac's ledger ``TaskState`` onto the A2A ``TaskState`` enum.
#:
#: mac's lifecycle is richer than A2A's, so several mac states collapse onto one
#: A2A state:
#:   open / (no deps yet)      -> submitted   (accepted, not yet being worked)
#:   blocked                   -> submitted   (accepted, waiting on deps)
#:   claimed / running         -> working     (an agent is actively on it)
#:   needs_review / reviewing  -> working     (still in flight from the peer's view)
#:   completed                 -> completed
#:   failed                    -> failed
#:   cancelled                 -> canceled    (A2A uses the single-'l' spelling)
#:
#: A mac state of ``blocked`` that represents "waiting for the caller" would map
#: to ``input-required``; mac's ``blocked`` today means dependency-blocked, so
#: it maps to ``submitted``. ``input-required`` is reserved for a future
#: needs-input signal carried in task metadata (see :func:`map_task_state`).
MAC_STATE_TO_A2A: Dict[str, str] = {
    MacTaskState.OPEN.value: TaskState.SUBMITTED,
    MacTaskState.BLOCKED.value: TaskState.SUBMITTED,
    MacTaskState.CLAIMED.value: TaskState.WORKING,
    MacTaskState.RUNNING.value: TaskState.WORKING,
    MacTaskState.NEEDS_REVIEW.value: TaskState.WORKING,
    MacTaskState.REVIEWING.value: TaskState.WORKING,
    MacTaskState.COMPLETED.value: TaskState.COMPLETED,
    MacTaskState.FAILED.value: TaskState.FAILED,
    MacTaskState.CANCELLED.value: TaskState.CANCELED,
}


def map_task_state(task: Any) -> str:
    """Project a mac :class:`~mac.models.Task` onto an A2A ``TaskState`` value.

    Honors an explicit ``metadata.a2a.needs_input`` flag: a task an agent has
    parked pending caller input surfaces as ``input-required`` regardless of its
    ledger state, so an A2A peer knows to send another ``message/send``. Unknown
    ledger states fall back to ``submitted`` (accepted but not classified).
    """

    metadata = getattr(task, "metadata", None) or {}
    a2a_meta = metadata.get("a2a") if isinstance(metadata, Mapping) else None
    if isinstance(a2a_meta, Mapping) and a2a_meta.get("needs_input"):
        state = getattr(task, "state", "")
        if state not in {
            MacTaskState.COMPLETED.value,
            MacTaskState.FAILED.value,
            MacTaskState.CANCELLED.value,
        }:
            return TaskState.INPUT_REQUIRED
    return MAC_STATE_TO_A2A.get(getattr(task, "state", ""), TaskState.SUBMITTED)


class A2AService:
    """Maps A2A JSON-RPC methods onto a :class:`~mac.services.ControlPlane`.

    Stateless beyond the injected control plane; safe to construct per request.
    """

    #: A2A "context" for tasks created over this transport. A2A allows a server
    #: to assign a ``contextId`` grouping related tasks; mac uses one stable
    #: federation context unless the caller supplies its own ``contextId``.
    DEFAULT_CONTEXT_ID = "a2a"

    #: Actor recorded in the mac ledger for A2A-originated tasks.
    ACTOR = "a2a"

    #: Cap on how much history we surface back to a peer per ``tasks/get``.
    HISTORY_LIMIT = 50

    def __init__(self, control_plane: ControlPlane) -> None:
        self.cp = control_plane

    # -- dispatcher ---------------------------------------------------------

    def handle_rpc(
        self, method: str, params: Optional[Mapping[str, Any]], rpc_id: Any = None
    ) -> Dict[str, Any]:
        """Dispatch one A2A JSON-RPC call, returning a result/error envelope.

        Translates mac control-plane exceptions into JSON-RPC errors:
        :class:`~mac.models.NotFoundError` -> task-not-found (-32001); other
        :class:`~mac.models.MACError` (validation / transition) -> invalid
        params (-32602); anything else -> internal error (-32603).
        """

        params = dict(params or {})
        try:
            if method == Method.MESSAGE_SEND:
                return rpc_result(rpc_id, self.message_send(params))
            if method == Method.TASKS_GET:
                return rpc_result(rpc_id, self.tasks_get(params))
            if method == Method.TASKS_CANCEL:
                return rpc_result(rpc_id, self.tasks_cancel(params))
            if method == Method.MESSAGE_STREAM:
                # Declared in the card as unsupported (capabilities.streaming =
                # false); a peer that ignores that and calls it gets a clear
                # method-not-found rather than a silent hang.
                return rpc_error(
                    rpc_id,
                    ERROR_METHOD_NOT_FOUND,
                    "streaming is not supported by this agent (message/stream deferred)",
                )
            return rpc_error(
                rpc_id, ERROR_METHOD_NOT_FOUND, "method not found: %s" % method
            )
        except NotFoundError as exc:
            return rpc_error(rpc_id, ERROR_TASK_NOT_FOUND, str(exc))
        except MACError as exc:
            return rpc_error(rpc_id, ERROR_INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 - never leak a stack to the peer
            return rpc_error(rpc_id, ERROR_INTERNAL, "internal error: %s" % exc)

    # -- methods ------------------------------------------------------------

    def message_send(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        """``message/send``: create a mac task from the message, return a Task.

        The message's text parts form the task: the first non-empty line is the
        title, the full text is the description. We record the inbound message
        in ``metadata.a2a`` so ``tasks/get`` can replay it as history. The
        returned A2A Task has ``id`` == the mac task id and state ``submitted``.
        """

        message = Message.from_dict(params.get("message"))
        text = message.text().strip()
        if not text:
            raise _InvalidParams("message/send requires at least one non-empty text part")

        context_id = (
            message.context_id
            or str(params.get("contextId") or "").strip()
            or self.DEFAULT_CONTEXT_ID
        )
        title = text.splitlines()[0].strip()[:200] or "A2A delegated task"

        task = self.cp.create_task(
            title=title,
            description=text,
            actor=self.ACTOR,
            metadata={
                "a2a": {
                    "context_id": context_id,
                    "incoming_message_id": message.message_id,
                    "history": [message.to_dict()],
                }
            },
        )
        return self._task_to_a2a(task, context_id=context_id).to_dict()

    def tasks_get(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        """``tasks/get``: look up the mac task and return its A2A Task view."""

        task_id = self._require_task_id(params)
        task = self.cp.get_task(task_id)
        return self._task_to_a2a(task).to_dict()

    def tasks_cancel(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        """``tasks/cancel``: cancel the mac task, return the updated Task.

        Mac refuses to cancel an already-terminal task (the ledger has no
        completed/failed -> cancelled edge). Per A2A, that surfaces as an
        invalid-params error rather than silently lying about the state.
        """

        task_id = self._require_task_id(params)
        # Surfaces NotFoundError for an unknown id before we attempt the
        # transition (handle_rpc maps it to task-not-found).
        self.cp.get_task(task_id)
        try:
            task = self.cp.transition_task(
                task_id,
                MacTaskState.CANCELLED.value,
                actor=self.ACTOR,
                detail={
                    "source": "a2a.tasks/cancel",
                    "reason": "A2A client requested cancellation",
                },
            )
        except TransitionError as exc:
            # e.g. completed/failed tasks are not cancelable.
            raise _InvalidParams(str(exc)) from exc
        return self._task_to_a2a(task).to_dict()

    # -- helpers ------------------------------------------------------------

    def _task_to_a2a(self, task: Any, context_id: Optional[str] = None) -> Task:
        """Build an A2A :class:`Task` from a mac ledger task."""

        metadata = getattr(task, "metadata", None) or {}
        a2a_meta = metadata.get("a2a") if isinstance(metadata, Mapping) else None
        if context_id is None:
            context_id = (
                a2a_meta.get("context_id")
                if isinstance(a2a_meta, Mapping)
                else None
            ) or self.DEFAULT_CONTEXT_ID

        history = self._history_from_metadata(a2a_meta)
        status = TaskStatus(
            state=map_task_state(task),
            timestamp=getattr(task, "updated_at", None) or utcnow(),
        )
        return Task(
            id=getattr(task, "id"),
            context_id=context_id,
            status=status,
            history=history,
        )

    def _history_from_metadata(self, a2a_meta: Any) -> List[Message]:
        if not isinstance(a2a_meta, Mapping):
            return []
        raw = a2a_meta.get("history")
        if not isinstance(raw, (list, tuple)):
            return []
        return [Message.from_dict(m) for m in raw[: self.HISTORY_LIMIT] if isinstance(m, Mapping)]

    @staticmethod
    def _require_task_id(params: Mapping[str, Any]) -> str:
        task_id = params.get("id") or params.get("taskId")
        task_id = str(task_id or "").strip()
        if not task_id:
            raise _InvalidParams("params.id (the task id) is required")
        return task_id

    @staticmethod
    def agent_message(text: str, *, context_id: Optional[str] = None) -> Message:
        """Build an agent-authored A2A message (used for status messages)."""

        return Message(
            role=Role.AGENT,
            parts=[text_part(text)],
            message_id=new_message_id(),
            context_id=context_id,
        )


class _InvalidParams(MACError):
    """Internal: a bad-params condition that maps to JSON-RPC -32602.

    A subclass of :class:`~mac.models.MACError` so ``handle_rpc`` catches it on
    the generic-MACError path (-> invalid params) without a dedicated branch.
    """
