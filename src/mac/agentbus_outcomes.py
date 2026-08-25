"""Honest, structured turn-outcome and provenance semantics for AgentBus.

Incident task_60be7f29: AgentBus *delivery* succeeded while the underlying
peer turn had actually failed — an embedded turn hit its OpenClaw turn limit
and returned "LLM request failed / timed out", a real find-tool failure was
embedded in the reply, and an output-length stop truncated the answer. Every
one of those was published as ``peer.reply.v1`` with ``status: "ok"`` and then
mirrored to Slack as a model-written summary that read like success. Callers
had no structured way to tell "the bus delivered" from "the peer turn
succeeded", or a late asynchronous reply from a lost one.

This module is the single, dependency-free source of truth both the directable
worker (Python) and the OpenClaw mac-continuity plugin (JS mirrors these exact
names/rules) use to answer three separate questions honestly:

  1. TurnOutcome — what actually happened inside the peer's one-shot turn
     (ordinary completion vs. turn-limit timeout, output-limit truncation,
     tool failure, or model failure). :func:`classify_turn_result` derives it
     from the runtime result's structured fields *and* the reply prose, so an
     error embedded only in text is still caught. Error outcomes NEVER map to
     ``status: "ok"`` — see :func:`reply_status_for_outcome`.

  2. DeliveryOutcome — the caller/transport view: acknowledged delivery,
     synchronous wait-budget expiry, a late asynchronous reply that arrived
     after the budget (correlated, surfaced as ``late`` — not lost/duplicated),
     or a fully replied exchange.

  3. Mirror provenance — :func:`mirror_provenance` stamps every
     ``mac.fleet_conversation_mirror.v1`` record with the fact that the visible
     text is a model-generated summary, the source stream id, the source/reply
     status, and whether the turn was persona-only or task-executor-bound, so a
     mirror can never be mistaken for task-execution evidence.

Kept free of network, clock, and worker dependencies so the hub, the worker,
the plugin, and the tests all agree on one contract.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

JsonDict = Dict[str, Any]


# --------------------------------------------------------------------------- #
# Turn-execution outcomes (what happened inside the peer's one-shot turn)
# --------------------------------------------------------------------------- #
# Closed set. A consumer can switch on these without parsing prose.
TURN_COMPLETED = "completed"  # ordinary successful completion
TURN_TIMEOUT = "turn_timeout"  # embedded turn/step limit or deadline hit
TURN_OUTPUT_TRUNCATED = "output_truncated"  # output/length stop cut the answer
TURN_TOOL_FAILED = "tool_failed"  # a tool the turn invoked errored
TURN_MODEL_FAILED = "model_failed"  # the model/LLM request itself failed
TURN_REFUSED = "refused"  # the turn declined (policy/safety/verify)
TURN_ERROR = "error"  # any other structured failure

TURN_OUTCOMES = frozenset(
    {
        TURN_COMPLETED,
        TURN_TIMEOUT,
        TURN_OUTPUT_TRUNCATED,
        TURN_TOOL_FAILED,
        TURN_MODEL_FAILED,
        TURN_REFUSED,
        TURN_ERROR,
    }
)

# Every non-completed turn outcome is a failure; only a genuine completion may
# ever be signed as a peer.reply status of "ok". Error text is NEVER "ok".
_OUTCOME_TO_REPLY_STATUS: Dict[str, str] = {
    TURN_COMPLETED: "ok",
    TURN_TIMEOUT: "timeout",
    TURN_OUTPUT_TRUNCATED: "truncated",
    TURN_TOOL_FAILED: "failed",
    TURN_MODEL_FAILED: "failed",
    TURN_REFUSED: "refused",
    TURN_ERROR: "error",
}

# peer.reply.v1 status values that mean the turn did not honestly succeed.
NON_OK_REPLY_STATUSES = frozenset(
    value for value in _OUTCOME_TO_REPLY_STATUS.values() if value != "ok"
)


def reply_status_for_outcome(outcome: str) -> str:
    """Map a TurnOutcome code to a peer.reply.v1 ``status``.

    Only :data:`TURN_COMPLETED` becomes ``"ok"``; every failure outcome maps to
    a distinct non-ok status so error text can never be signed as success.
    """
    return _OUTCOME_TO_REPLY_STATUS.get(str(outcome or ""), "error")


# Textual fingerprints of failures the runtime historically embedded in the
# reply prose instead of a structured field (the exact incident cases).
_TIMEOUT_TEXT = re.compile(
    r"\b(turn limit|max(?:imum)? turns?|step limit|timed?\s*out|timeout|"
    r"deadline (?:exceeded|elapsed)|exceeded the (?:turn|time) )",
    re.IGNORECASE,
)
_MODEL_FAIL_TEXT = re.compile(
    r"\b(llm request failed|model (?:request )?failed|completion failed|"
    r"inference (?:request )?failed|request to the model failed)\b",
    re.IGNORECASE,
)
_TOOL_FAIL_TEXT = re.compile(
    r"\b(tool (?:call )?(?:failed|error)|find(?:-| )tool (?:failed|error)|"
    r"command (?:failed|not found)|no such tool|tool .* (?:failed|errored))\b",
    re.IGNORECASE,
)
_TRUNCATED_TEXT = re.compile(
    r"\b(output (?:length|limit)|max(?:imum)?[_ ]?(?:output[_ ]?)?tokens|"
    r"length limit|response truncated|truncated (?:output|response))\b",
    re.IGNORECASE,
)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _stop_reason_outcome(stop_reason: str) -> Optional[str]:
    """Map a runtime stop/finish reason to a turn outcome, if recognizable."""
    reason = _norm(stop_reason)
    if not reason:
        return None
    if reason in {"length", "max_tokens", "output_limit", "max_output_tokens", "token_limit"}:
        return TURN_OUTPUT_TRUNCATED
    if reason in {
        "turn_limit",
        "max_turns",
        "step_limit",
        "max_steps",
        "timeout",
        "timed_out",
        "deadline",
        "deadline_exceeded",
    }:
        return TURN_TIMEOUT
    if reason in {"tool_error", "tool_failed", "tool_failure"}:
        return TURN_TOOL_FAILED
    if reason in {"model_error", "model_failed", "llm_error", "provider_error"}:
        return TURN_MODEL_FAILED
    if reason in {"refused", "declined", "policy", "safety"}:
        return TURN_REFUSED
    if reason in {"stop", "end_turn", "complete", "completed", "done", "eos"}:
        return TURN_COMPLETED
    return None


def classify_turn_result(
    result: Any,
    reply_text: str = "",
    *,
    timed_out: bool = False,
) -> str:
    """Classify a runtime turn result into a structured :data:`TURN_OUTCOMES` code.

    Precedence: an explicit hard timeout wins; then structured signals on the
    result (``error``/``failure`` objects, ``stop_reason``/``finish_reason``,
    ``tool_error`` / ``tool_status``); finally the reply prose is scanned for
    the exact textual failure fingerprints the runtime historically embedded.
    A clean result with plain completion text is :data:`TURN_COMPLETED`.
    """
    if timed_out:
        return TURN_TIMEOUT

    text = str(reply_text or "")

    if isinstance(result, dict):
        # An explicit structured outcome/status wins if it names a known code.
        for key in (
            "turn_outcome",
            "outcome",
            "stop_reason",
            "finish_reason",
            "stopReason",
            "finishReason",
        ):
            mapped = _stop_reason_outcome(result.get(key))
            if mapped is not None and not (
                mapped == TURN_COMPLETED and _has_embedded_failure(text)
            ):
                if mapped != TURN_COMPLETED:
                    return mapped

        # A structured error/failure object never reads as success.
        error_obj = result.get("error") or result.get("failure")
        if error_obj:
            kind = ""
            detail = ""
            if isinstance(error_obj, dict):
                kind = _norm(
                    error_obj.get("kind") or error_obj.get("type") or error_obj.get("code")
                )
                detail = str(error_obj.get("message") or error_obj.get("detail") or "")
            else:
                detail = str(error_obj)
            if "timeout" in kind or "turn_limit" in kind or "deadline" in kind:
                return TURN_TIMEOUT
            if "tool" in kind:
                return TURN_TOOL_FAILED
            if "model" in kind or "llm" in kind or "provider" in kind:
                return TURN_MODEL_FAILED
            classified = _classify_text(detail or text)
            return classified or TURN_ERROR

        # A tool-status field that reports failure.
        tool_error = result.get("tool_error") or result.get("toolError")
        tool_status = _norm(result.get("tool_status") or result.get("toolStatus"))
        if tool_error or tool_status in {"failed", "error", "not_found"}:
            return TURN_TOOL_FAILED

        if result.get("timed_out") or result.get("timedOut"):
            return TURN_TIMEOUT

    # Nothing structured said failure; fall back to scanning the reply prose for
    # the exact failure text the runtime historically embedded.
    classified = _classify_text(text)
    if classified is not None:
        return classified
    return TURN_COMPLETED


def _has_embedded_failure(text: str) -> bool:
    return _classify_text(text) is not None


def _classify_text(text: str) -> Optional[str]:
    if not text:
        return None
    if _MODEL_FAIL_TEXT.search(text):
        return TURN_MODEL_FAILED
    if _TIMEOUT_TEXT.search(text):
        return TURN_TIMEOUT
    if _TRUNCATED_TEXT.search(text):
        return TURN_OUTPUT_TRUNCATED
    if _TOOL_FAIL_TEXT.search(text):
        return TURN_TOOL_FAILED
    return None


# --------------------------------------------------------------------------- #
# Delivery / caller-side outcomes (transport view, distinct from the turn)
# --------------------------------------------------------------------------- #
# Closed set so a caller can distinguish delivery from execution without prose.
DELIVERY_ACKNOWLEDGED = "acknowledged"  # published/queued; no wait requested
DELIVERY_REPLIED = "replied"  # a reply arrived within the wait budget
DELIVERY_WAIT_EXPIRED = "wait_expired"  # sync wait budget elapsed, no reply yet
DELIVERY_LATE_REPLY = "late"  # reply arrived AFTER the budget (not lost)

DELIVERY_OUTCOMES = frozenset(
    {
        DELIVERY_ACKNOWLEDGED,
        DELIVERY_REPLIED,
        DELIVERY_WAIT_EXPIRED,
        DELIVERY_LATE_REPLY,
    }
)


def delivery_outcome(
    *,
    wait_budget_seconds: float,
    reply_present: bool,
    reply_within_budget: bool,
) -> str:
    """Classify the transport-level outcome of a request/reply exchange.

    - No wait requested -> ``acknowledged`` (fire-and-forget delivery only).
    - Reply within the budget -> ``replied``.
    - Reply present but after the budget -> ``late`` (correlated, not lost).
    - No reply yet within the budget -> ``wait_expired``.
    """
    if wait_budget_seconds <= 0:
        return DELIVERY_ACKNOWLEDGED
    if reply_present and reply_within_budget:
        return DELIVERY_REPLIED
    if reply_present and not reply_within_budget:
        return DELIVERY_LATE_REPLY
    return DELIVERY_WAIT_EXPIRED


# --------------------------------------------------------------------------- #
# Mirror provenance (mac.fleet_conversation_mirror.v1)
# --------------------------------------------------------------------------- #
def mirror_provenance(
    *,
    source_stream_id: str,
    source_status: str = "ok",
    reply_status: str = "ok",
    task_executor_bound: bool = False,
    summarizer_model: Optional[str] = None,
) -> JsonDict:
    """Build the concise provenance block for a conversation-mirror record.

    Every mirror carries ``summary_is_model_generated: True`` — the rendered
    Slack text is a model-written summary, NEVER verbatim messages or execution
    evidence. ``turn_binding`` records whether the mirrored turn was a
    persona-only chat turn or a task-executor-bound directive, so a mirror can
    never be accepted as proof a task executor did the work.
    """
    provenance: JsonDict = {
        "summary_is_model_generated": True,
        "is_execution_evidence": False,
        "source_stream_id": str(source_stream_id or ""),
        "source_status": str(source_status or "ok"),
        "reply_status": str(reply_status or "ok"),
        "turn_binding": "task_executor" if task_executor_bound else "persona",
    }
    if summarizer_model:
        provenance["summarizer_model"] = str(summarizer_model)
    return provenance
