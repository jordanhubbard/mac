"""AgentBus payload schema registry (task_0d50e190, audit 7/7).

The bus has always carried self-describing payloads (``"schema":
"mac.agent.peer_message.v1"``) — but the names were pure convention: nothing
validated a payload against anything, so a malformed peer message was
discovered by the CONSUMER at turn time instead of the PRODUCER at publish
time. This registry is the contract layer's first piece: a closed set of
known schema names with compact structural specs, enforced at publish for
declared schemas, advisory (observability warning) for unknown ones so
ad-hoc experimentation stays possible.

The validator is deliberately dependency-free (no jsonschema): a spec is a
dict of field -> type spec, where a type spec is one of ``str`` / ``int`` /
``bool`` / ``dict`` / ``list`` (isinstance check), a tuple of those, or the
sentinel ``ANY``. Fields listed in ``required`` must be present. Unknown
payload fields are allowed (schemas evolve additively).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from mac.models import ValidationError

ANY = object()

SchemaSpec = Dict[str, Any]

# Compact structural contracts for the established mac.*.v1 payload family.
AGENTBUS_SCHEMA_REGISTRY: Dict[str, SchemaSpec] = {
    "mac.agent.peer_message.v1": {
        "required": ["schema", "message"],
        "fields": {
            "schema": str,
            "correlation_id": str,
            "from_agent_id": str,
            "to_agent_id": str,
            "to_agent_ids": list,
            "message": str,
        },
    },
    "mac.agent.peer_reply.v1": {
        "required": ["schema", "reply"],
        "fields": {
            "schema": str,
            "correlation_id": str,
            "in_reply_to": str,
            "in_reply_to_sequence": int,
            "from_agent_id": str,
            "to_agent_id": str,
            "status": str,
            # Structured turn-execution outcome (mac.agentbus_outcomes.TURN_*).
            # Distinct from ``status``: it names WHY a non-ok status was chosen
            # (turn_timeout / output_truncated / tool_failed / model_failed /
            # refused / error) so a consumer never parses ``reply`` prose.
            "turn_outcome": str,
            # True when this reply arrived after the caller's wait budget; it
            # stays correlated to the original stream and is surfaced as late,
            # not lost or duplicated.
            "late": bool,
            "reply": str,
        },
    },
    "mac.media.share.v1": {
        "required": ["schema"],
        "fields": {
            "schema": str,
            "filename": str,
            "mime": str,
            "note": str,
            "total_bytes": int,
            "chunk_count": int,
        },
    },
    "mac.agentbus.error.v1": {
        "required": ["schema", "code", "detail"],
        "fields": {
            "schema": str,
            "code": str,
            "retryable": bool,
            "detail": str,
            "in_reply_to": str,
            "correlation_id": str,
        },
    },
    "mac.fleet_conversation_mirror.v1": {
        "required": ["schema"],
        "fields": {
            "schema": str,
            "stream_id": str,
            "sender_agent_id": str,
            # Provenance (task_60be7f29): the rendered Slack text is a
            # model-written summary, never verbatim or execution evidence.
            "summary_is_model_generated": bool,
            "is_execution_evidence": bool,
            "source_stream_id": str,
            "source_status": str,
            "reply_status": str,
            "turn_binding": str,
            "summarizer_model": str,
        },
    },
    "mac.human.directive.v1": {
        "required": ["schema", "message"],
        "fields": {
            "schema": str,
            "message": str,
            "issued_by": str,
            "correlation_id": str,
            "to_agent_id": str,
        },
    },
    "mac.directive.activation.v1": {
        "required": [
            "schema",
            "activation_id",
            "directive_id",
            "version",
            "epoch",
            "digest",
        ],
        "fields": {
            "schema": str,
            "activation_id": str,
            "directive_id": str,
            "version": int,
            "epoch": int,
            "digest": str,
        },
    },
}

# Standard error taxonomy codes for mac.agentbus.error.v1 chunks. Callers can
# extend detail freely; the code set is closed so consumers can switch on it.
AGENTBUS_ERROR_CODES = frozenset(
    {
        "timeout",  # deadline elapsed before a reply
        "refused",  # recipient declined (authorization/policy)
        "failed",  # recipient attempted and errored
        "unavailable",  # recipient unreachable/departed
        "invalid",  # request payload failed contract validation
    }
)


def error_payload(
    code: str,
    detail: str,
    *,
    retryable: bool = False,
    correlation_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a standard mac.agentbus.error.v1 payload."""
    if code not in AGENTBUS_ERROR_CODES:
        raise ValidationError(
            "unknown agentbus error code: %s (allowed: %s)"
            % (code, ", ".join(sorted(AGENTBUS_ERROR_CODES)))
        )
    payload: Dict[str, Any] = {
        "schema": "mac.agentbus.error.v1",
        "code": code,
        "retryable": bool(retryable),
        "detail": str(detail or "")[:2000],
    }
    if correlation_id:
        payload["correlation_id"] = correlation_id
    if in_reply_to:
        payload["in_reply_to"] = in_reply_to
    return payload


def _type_matches(value: Any, expected: Any) -> bool:
    if expected is ANY:
        return True
    if isinstance(expected, tuple):
        return any(_type_matches(value, item) for item in expected)
    if expected is int:
        # bool is an int subclass; a declared int must not accept True.
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def validate_payload(payload: Any) -> Tuple[Optional[str], List[str]]:
    """Validate a payload against its declared schema, if registered.

    Returns ``(schema_name, problems)``. ``schema_name`` is None when the
    payload declares no schema; ``problems`` is empty when the payload is
    valid or the declared schema is unregistered (advisory mode — the
    caller decides whether to warn).
    """
    if not isinstance(payload, dict):
        return None, []
    declared = payload.get("schema")
    if not isinstance(declared, str) or not declared:
        return None, []
    spec = AGENTBUS_SCHEMA_REGISTRY.get(declared)
    if spec is None:
        return declared, []
    problems: List[str] = []
    for field in spec.get("required", []):
        if field not in payload:
            problems.append("missing required field: %s" % field)
    for field, expected in spec.get("fields", {}).items():
        if field in payload and not _type_matches(payload[field], expected):
            problems.append(
                "field %s has wrong type %s" % (field, type(payload[field]).__name__)
            )
    return declared, problems


def is_registered(schema_name: str) -> bool:
    """Return whether the schema name is present in the agentbus registry."""
    return schema_name in AGENTBUS_SCHEMA_REGISTRY
