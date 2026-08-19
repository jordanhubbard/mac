"""AgentBus control-plane message schemas and payload builders.

Defines the versioned schema identifiers, topics, and content types for the
repository-update, artifact-publish, and agent-reflection AgentBus events, along
with helpers that assemble their JSON payloads.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

JsonDict = Dict[str, Any]

REPO_UPDATE_SCHEMA = "mac.agentbus.repo_update.v1"
REPO_UPDATE_TOPIC = "mac.repo.update.v1"
REPO_UPDATE_CONTENT_TYPE = "application/vnd.mac.repo-update+json"

REPO_UPDATE_RESULT_SCHEMA = "mac.agentbus.repo_update_result.v1"
REPO_UPDATE_RESULT_TOPIC = "mac.repo.update.result.v1"
REPO_UPDATE_RESULT_CONTENT_TYPE = "application/vnd.mac.repo-update-result+json"

ARTIFACT_PUBLISH_SCHEMA = "mac.agentbus.artifact_publish.v1"
ARTIFACT_PUBLISH_TOPIC = "mac.artifact.publish.v1"
ARTIFACT_PUBLISH_CONTENT_TYPE = "application/vnd.mac.artifact-publish+json"

AGENT_REFLECTION_SCHEMA = "mac.agentbus.agent_reflection.v2"
AGENT_REFLECTION_TOPIC = "mac.agent.reflect.v1"
AGENT_REFLECTION_CONTENT_TYPE = "application/vnd.mac.agent-reflection+json"

DEBUG_TERMINAL_OPEN_SCHEMA = "mac.agentbus.debug_terminal_open.v1"
DEBUG_TERMINAL_OPEN_TOPIC = "mac.debug.terminal.open.v1"
DEBUG_TERMINAL_OPEN_CONTENT_TYPE = "application/vnd.mac.debug-terminal-open+json"

DEBUG_TERMINAL_INPUT_SCHEMA = "mac.agentbus.debug_terminal_input.v1"
DEBUG_TERMINAL_INPUT_TOPIC = "mac.debug.terminal.input.v1"
DEBUG_TERMINAL_INPUT_CONTENT_TYPE = "application/vnd.mac.debug-terminal-input+json"

DEBUG_TERMINAL_OUTPUT_SCHEMA = "mac.agentbus.debug_terminal_output.v1"
DEBUG_TERMINAL_OUTPUT_TOPIC = "mac.debug.terminal.output.v1"
DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE = "application/vnd.mac.debug-terminal-output+json"

REFLECT_REQUEST_SCHEMA = "mac.agentbus.reflect_request.v1"
REFLECT_REQUEST_TOPIC = "mac.reflect.request.v1"
REFLECT_REQUEST_CONTENT_TYPE = "application/vnd.mac.reflect-request+json"

REFLECT_RESULT_SCHEMA = "mac.agentbus.reflect_result.v1"
REFLECT_RESULT_TOPIC = "mac.reflect.result.v1"
REFLECT_RESULT_CONTENT_TYPE = "application/vnd.mac.reflect-result+json"

# Agent-to-agent peer messaging (task_c6f02f06, worker-directable Phase 0).
# These strings match src/mac/api.py (AgentBusPeerMessage defaults) and the
# schema names enforced in src/mac/agentbus_schemas.py — keep them in sync.
PEER_MESSAGE_TOPIC = "peer.message.v1"
PEER_MESSAGE_SCHEMA = "mac.agent.peer_message.v1"
PEER_MESSAGE_CONTENT_TYPE = "application/vnd.mac.agent-peer+json"

PEER_REPLY_TOPIC = "peer.reply.v1"
PEER_REPLY_SCHEMA = "mac.agent.peer_reply.v1"
PEER_REPLY_CONTENT_TYPE = "application/vnd.mac.agent-peer-reply+json"

# ---------------------------------------------------------------------------
# Lifecycle vocabulary (v2): the verbs every agent understands, and that a
# human speaks too.
#
# WHY A SHARED VOCABULARY. The console carries a few controls -- stand down,
# resume -- for when agents go off the rails. The retired dashboard did that by
# calling POST /agents/bulk: a privileged control-plane mutation that bypassed
# the agents entirely. These are REQUESTS on a channel every participant can
# see, which each agent observes and honours. That is what lets the console
# keep an honest invariant: it never mutates the control plane directly, it
# speaks on the bus like any other participant.
#
# WHY v2 AND NOT v1. There are no v1 agents; this fleet is the only deployment
# of mac. Rather than add a lifecycle namespace alongside the existing v1
# control events and carry two shapes forever, the lifecycle verbs start at v2
# and the version is stated so a later reader can see where the break was.
#
# STAND-DOWN IS NOT ABORT, and collapsing them is the mistake this vocabulary
# exists to prevent. A single button labelled "stop" that means ABORT destroys
# work in flight; one that means PAUSE does not stop a runaway. Most
# off-the-rails moments want stand-down. Both verbs ship, named honestly.
LIFECYCLE_VERSION = "v2"

#: Stop claiming new work; finish whatever is already held.
LIFECYCLE_STAND_DOWN = "stand_down"
#: Drop current work now. DESTRUCTIVE: in-flight work is lost.
LIFECYCLE_ABORT = "abort"
#: Stop claiming new work (same as stand_down for a worker, but scoped and
#: reversible by ``resume``; stand_down is the urgent form and may be honoured
#: mid-step).
LIFECYCLE_PAUSE = "pause"
#: Resume claiming work after a pause or stand-down.
LIFECYCLE_RESUME = "resume"
#: Report what you are doing, now. The liveness question a heartbeat cannot
#: answer: a wedged agent still heartbeats.
LIFECYCLE_STATUS = "status"

LIFECYCLE_VERBS = (
    LIFECYCLE_STAND_DOWN,
    LIFECYCLE_ABORT,
    LIFECYCLE_PAUSE,
    LIFECYCLE_RESUME,
    LIFECYCLE_STATUS,
)

#: The verbs that destroy work in flight. A caller that offers these must say
#: so; a UI must not put them behind the same control as the reversible ones.
LIFECYCLE_DESTRUCTIVE_VERBS = frozenset({LIFECYCLE_ABORT})

#: Who a directive is aimed at. ``fleet`` is everyone on the bus, ``project``
#: everyone working a named project, ``agent`` one named agent, and ``task``
#: whoever is working one named task.
#:
#: ``task`` WAS MISSING AND THAT WAS THE EXPENSIVE GAP. The first three scope by
#: WHO IS WORKING; the runaway case is TASK-shaped. On 2026-08-18 agent_rocky
#: opened EIGHT pull requests for one task, one per lease, roughly every thirty
#: minutes: publish, PR opens, nothing merges it, no canonical integration
#: proof, lease expires, re-claim, redo. Three attempts, max_attempts, failed --
#: 12,223,209 tokens across 218 provider attempts on work that was completed
#: correctly eight times and landed zero.
#:
#: The hub SAW it. `high_token_work_without_publication` was raised against that
#: task while it was happening. The observer was not short of detection; it had
#: no way to say "everyone stop working on this one", because that sentence had
#: no scope to be said in. Stopping the agent would have been wrong (it had
#: other work) and stopping the project wider still.
LIFECYCLE_SCOPES = ("fleet", "project", "agent", "task")

#: What each verb means when the scope is a TASK. Written down rather than left
#: to the receiver: "stand down" is unambiguous about an agent and ambiguous
#: about a task, and a directive whose meaning is inferred is one every
#: implementation infers differently.
LIFECYCLE_TASK_SEMANTICS = {
    #: Stop claiming this task. An attempt already running finishes; the work
    #: in flight is not thrown away. This is the runaway remedy -- it ends the
    #: republish loop without discarding the attempt that may yet land.
    LIFECYCLE_STAND_DOWN: "stop claiming this task; finish the attempt in flight",
    #: Drop the running attempt now. DESTRUCTIVE, and rarely what a runaway
    #: needs: the loop above was producing correct work, so aborting it would
    #: have destroyed eight good attempts to stop one bad pattern.
    LIFECYCLE_ABORT: "drop the running attempt now; in-flight work is lost",
    #: Stop dispatching it, keep it claimable later. Reversible by `resume`.
    LIFECYCLE_PAUSE: "stop dispatching this task; it stays claimable later",
    LIFECYCLE_RESUME: "dispatch this task again",
    #: Who holds it, on which lease, since when -- the question an operator
    #: asks before deciding which of the above to send.
    LIFECYCLE_STATUS: "report who is working this task, on which lease, since when",
}

LIFECYCLE_SCHEMA = "mac.agentbus.lifecycle.v2"
LIFECYCLE_TOPIC = "mac.lifecycle.directive.v2"
LIFECYCLE_CONTENT_TYPE = "application/vnd.mac.lifecycle+json"

LIFECYCLE_ACK_SCHEMA = "mac.agentbus.lifecycle_ack.v2"
LIFECYCLE_ACK_TOPIC = "mac.lifecycle.ack.v2"
LIFECYCLE_ACK_CONTENT_TYPE = "application/vnd.mac.lifecycle-ack+json"


class LifecycleVerbError(ValueError):
    """An unknown lifecycle verb or scope.

    Raised rather than ignored. A directive nobody understands must fail where
    it is built, not travel the bus and be silently dropped by every receiver --
    that is indistinguishable from a fleet that heard it and did nothing.
    """


def lifecycle_payload(
    *,
    verb: str,
    scope: str = "fleet",
    target: Optional[str] = None,
    reason: str = "",
    issued_by: str = "human",
    correlation_id: Optional[str] = None,
) -> JsonDict:
    """Build a lifecycle directive.

    ``correlation_id`` is what makes the acknowledgement loop possible: every
    ack names the directive it answers, so a caller can tell WHO HEARD IT from
    who did not. That distinction is the whole point -- see
    :func:`lifecycle_ack_payload`.
    """
    verb = str(verb or "").strip()
    if verb not in LIFECYCLE_VERBS:
        raise LifecycleVerbError(
            "unknown lifecycle verb %r; known verbs are %s"
            % (verb, ", ".join(LIFECYCLE_VERBS))
        )
    scope = str(scope or "").strip()
    if scope not in LIFECYCLE_SCOPES:
        raise LifecycleVerbError(
            "unknown lifecycle scope %r; known scopes are %s"
            % (scope, ", ".join(LIFECYCLE_SCOPES))
        )
    if scope in ("project", "agent", "task") and not str(target or "").strip():
        raise LifecycleVerbError(
            "scope %r requires a target; without one this would address the "
            "whole fleet, which is the opposite of what was asked" % scope
        )
    payload: JsonDict = {
        "schema": LIFECYCLE_SCHEMA,
        "verb": verb,
        "scope": scope,
        "issued_by": str(issued_by or "human"),
        "destructive": verb in LIFECYCLE_DESTRUCTIVE_VERBS,
    }
    if scope != "fleet":
        payload["target"] = str(target).strip()
    if reason:
        payload["reason"] = str(reason)
    if correlation_id:
        payload["correlation_id"] = str(correlation_id)
    return payload


def lifecycle_ack_payload(
    *,
    correlation_id: str,
    agent_id: str,
    verb: str,
    honoured: bool,
    detail: str = "",
) -> JsonDict:
    """Build an agent's answer to a lifecycle directive.

    ACKNOWLEDGEMENT IS NOT OPTIONAL DECORATION. A bus directive is best-effort:
    a wedged agent -- one that is not draining its subscription -- never hears
    "stand down", and that is precisely the agent the directive exists for. So
    a caller must be able to distinguish HEARD from NOT HEARD, and a UI must
    never report "stopped" when the truth is "asked". Without acks there is no
    denominator and the control reports success while enforcing nothing.

    ``honoured=False`` is a real answer, not a failure: an agent may decline
    (mid-commit, holding a lease) and saying so is more useful than silence.
    """
    return {
        "schema": LIFECYCLE_ACK_SCHEMA,
        "correlation_id": str(correlation_id),
        "agent_id": str(agent_id),
        "verb": str(verb),
        "honoured": bool(honoured),
        "detail": str(detail or ""),
    }


CONTROL_STREAM_TYPES = {
    (REPO_UPDATE_TOPIC, REPO_UPDATE_CONTENT_TYPE),
    (REFLECT_REQUEST_TOPIC, REFLECT_REQUEST_CONTENT_TYPE),
    (LIFECYCLE_TOPIC, LIFECYCLE_CONTENT_TYPE),
}


def is_control_stream(topic: str, content_type: str) -> bool:
    """Return whether the topic and content type identify a control stream."""
    base_content_type = str(content_type or "").split(";", 1)[0]
    return (str(topic or ""), base_content_type) in CONTROL_STREAM_TYPES


def repo_update_payload(
    *,
    repo_path: Optional[str] = None,
    remote: str = "origin",
    branch: str = "main",
    restart: bool = True,
    restart_services: Optional[List[str]] = None,
    request_id: Optional[str] = None,
    target_sha: Optional[str] = None,
    desired_generation: Optional[int] = None,
    release_id: Optional[str] = None,
) -> JsonDict:
    """Build a repository-update control payload."""
    payload: JsonDict = {
        "schema": REPO_UPDATE_SCHEMA,
        "remote": remote,
        "branch": branch,
        "restart": bool(restart),
    }
    if repo_path:
        payload["repo_path"] = repo_path
    if restart_services:
        payload["restart_services"] = list(restart_services)
    if request_id:
        payload["request_id"] = request_id
    if target_sha:
        payload["target_sha"] = target_sha
    if desired_generation is not None:
        payload["desired_generation"] = int(desired_generation)
    if release_id:
        payload["release_id"] = release_id
    return payload


def artifact_publish_payload(
    *,
    operation: str,
    artifact: Optional[JsonDict] = None,
    artifacts: Optional[list[JsonDict]] = None,
    publish_dir: Optional[str] = None,
    public_url: Optional[str] = None,
    path: Optional[str] = None,
    request_id: Optional[str] = None,
) -> JsonDict:
    """Build an artifact-publish control payload."""
    payload: JsonDict = {
        "schema": ARTIFACT_PUBLISH_SCHEMA,
        "operation": operation,
    }
    if artifact is not None:
        payload["artifact"] = artifact
    if artifacts is not None:
        payload["artifacts"] = artifacts
    if publish_dir:
        payload["publish_dir"] = publish_dir
    if public_url:
        payload["public_url"] = public_url
    if path:
        payload["path"] = path
    if request_id:
        payload["request_id"] = request_id
    return payload


_REFLECTION_MAX_WORDS = 500


def _normalize_narrative(value: Optional[str]) -> Optional[str]:
    """Return a word-count-bounded narrative string, or None when empty/absent.

    - None or empty/whitespace-only input -> None (field omitted)
    - Non-empty strings are stripped; if the word count exceeds
      _REFLECTION_MAX_WORDS the text is truncated at the word boundary and a
      truncation marker is appended.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    words = text.split()
    if len(words) > _REFLECTION_MAX_WORDS:
        text = " ".join(words[:_REFLECTION_MAX_WORDS]) + " [...]"
    return text


def agent_reflection_payload(
    *,
    agent: JsonDict,
    narrative: Optional[str] = None,
    request_id: Optional[str] = None,
) -> JsonDict:
    """Build an agent-reflection bus payload.

    Parameters
    ----------
    agent:
        Raw agent record dict (inventory fields).
    narrative:
        Optional free-text runtime narrative produced by the agent's soul /
        memory query.  When provided and non-empty the payload gains a
        top-level ``reflection`` key containing the bounded text (up to
        _REFLECTION_MAX_WORDS words).  Empty or whitespace-only values are
        silently dropped so callers may pass an empty string without changing
        the wire shape.
    request_id:
        Optional correlation identifier; included verbatim when truthy.
    """
    capabilities = list(agent.get("capabilities") or [])
    resources = agent.get("resources") if isinstance(agent.get("resources"), dict) else {}
    agent_inventory: JsonDict = {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "capabilities": capabilities,
        "resources": resources,
        "status": agent.get("status"),
        "health_status": agent.get("health_status"),
        "current_task_id": agent.get("current_task_id"),
        "running_digest": agent.get("running_digest"),
        "role_id": agent.get("role_id"),
        "persona_instance_id": agent.get("persona_instance_id"),
        "installed_packages": agent.get("installed_packages") or {},
        "last_seen_at": agent.get("last_seen_at"),
        "updated_at": agent.get("updated_at"),
    }
    payload: JsonDict = {
        "schema": AGENT_REFLECTION_SCHEMA,
        "agent_id": agent.get("id"),
        "agent": agent_inventory,
        "summary": "agent %s (%s) is %s/%s; capabilities: %s"
        % (
            agent.get("name") or "",
            agent.get("id") or "",
            agent.get("status") or "unknown",
            agent.get("health_status") or "unknown",
            ", ".join(str(item) for item in capabilities) or "none",
        ),
    }
    normalized = _normalize_narrative(narrative)
    if normalized is not None:
        payload["reflection"] = normalized
    if request_id:
        payload["request_id"] = request_id
    return payload


def reflect_request_payload(
    *,
    sender_agent_id: str,
    query: str,
    request_id: Optional[str] = None,
) -> JsonDict:
    """Build a reflect-request control payload."""
    payload: JsonDict = {
        "schema": REFLECT_REQUEST_SCHEMA,
        "sender_agent_id": sender_agent_id,
        "query": query,
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


def reflect_result_payload(
    *,
    request_id: str,
    agent_id: str,
    response: str,
    word_count: int,
) -> JsonDict:
    """Build a reflect-result control payload."""
    return {
        "schema": REFLECT_RESULT_SCHEMA,
        "request_id": request_id,
        "agent_id": agent_id,
        "response": response,
        "word_count": int(word_count),
    }


def peer_reply_payload(
    *,
    from_agent_id: str,
    to_agent_id: str,
    reply: str,
    status: str = "ok",
    correlation_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    turn_outcome: Optional[str] = None,
    late: bool = False,
) -> JsonDict:
    """Build a peer.reply.v1 payload (mac.agent.peer_reply.v1).

    Mirrors the shape produced by the OpenClaw mac-continuity plugin's
    publishPeerReply so consumers see an identical wire contract regardless of
    which side (gateway plugin or directable worker) produced the reply.

    ``turn_outcome`` (mac.agentbus_outcomes.TURN_*) names the structured
    turn-execution outcome so a consumer distinguishes turn-timeout, output
    truncation, tool failure, model failure, refusal, and ordinary completion
    without parsing ``reply`` prose. ``late`` marks a reply that arrived after
    the caller's wait budget; it stays correlated to the original stream.
    """
    payload: JsonDict = {
        "schema": PEER_REPLY_SCHEMA,
        "from_agent_id": from_agent_id,
        "to_agent_id": to_agent_id,
        "status": status,
        "reply": str(reply or "")[:32000],
    }
    if turn_outcome:
        payload["turn_outcome"] = str(turn_outcome)
    if late:
        payload["late"] = True
    if correlation_id:
        payload["correlation_id"] = correlation_id
    if in_reply_to:
        payload["in_reply_to"] = in_reply_to
    return payload


def debug_terminal_open_payload(
    *,
    session_id: str,
    input_stream_id: str,
    output_stream_id: str,
    sender_agent_id: str,
    shell: Optional[str] = None,
    cwd: Optional[str] = None,
    rows: int = 32,
    cols: int = 120,
    ttl_seconds: int = 900,
    request_id: Optional[str] = None,
) -> JsonDict:
    """Build a debug-terminal open control payload."""
    payload: JsonDict = {
        "schema": DEBUG_TERMINAL_OPEN_SCHEMA,
        "session_id": session_id,
        "input_stream_id": input_stream_id,
        "output_stream_id": output_stream_id,
        "sender_agent_id": sender_agent_id,
        "rows": int(rows),
        "cols": int(cols),
        "ttl_seconds": int(ttl_seconds),
    }
    if shell:
        payload["shell"] = shell
    if cwd:
        payload["cwd"] = cwd
    if request_id:
        payload["request_id"] = request_id
    return payload


def debug_terminal_input_payload(
    *,
    session_id: str,
    data_b64: Optional[str] = None,
    close: bool = False,
    rows: Optional[int] = None,
    cols: Optional[int] = None,
) -> JsonDict:
    """Build a debug-terminal input control payload."""
    payload: JsonDict = {
        "schema": DEBUG_TERMINAL_INPUT_SCHEMA,
        "session_id": session_id,
    }
    if data_b64 is not None:
        payload["data_b64"] = data_b64
    if close:
        payload["close"] = True
    if rows is not None or cols is not None:
        payload["resize"] = {
            "rows": int(rows or 0),
            "cols": int(cols or 0),
        }
    return payload


def debug_terminal_output_payload(
    *,
    session_id: str,
    event: str,
    data_b64: Optional[str] = None,
    message: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> JsonDict:
    """Build a debug-terminal output control payload."""
    payload: JsonDict = {
        "schema": DEBUG_TERMINAL_OUTPUT_SCHEMA,
        "session_id": session_id,
        "event": event,
    }
    if data_b64 is not None:
        payload["data_b64"] = data_b64
    if message:
        payload["message"] = message
    if exit_code is not None:
        payload["exit_code"] = int(exit_code)
    return payload
