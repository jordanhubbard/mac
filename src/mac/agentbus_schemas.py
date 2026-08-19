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

#: Payload keys that are refused everywhere on the bus, at any nesting depth.
#:
#: Ported from ``messaging_service.FORBIDDEN_MESSAGE_KEYS`` when the control
#: channel was consolidated onto agentbus. The control channel carried small
#: schema-validated messages and refused these keys so a misbehaving agent
#: could not smuggle a job spec through a channel consumers treat as data;
#: agentbus carried opaque blobs and had no equivalent. Moving the traffic
#: without moving this check would have quietly dropped the guarantee.
#:
#: Enforced for CONTROL payloads only -- those declaring a ``mac.control.*``
#: schema. Agentbus content streams are deliberately permissive: a patch blob
#: may legitimately carry a field named ``command``, and
#: test_agentbus_streams_typed_content_without_weakening_control_messages
#: pins that. Consolidating the two mechanisms therefore puts two validation
#: regimes on one transport rather than flattening them to the stricter one.
#:
#: The strictness lives with the SCHEMA, not the caller, so a control message
#: cannot be laundered into a permissive one by routing it differently: the
#: control sender always stamps ``mac.control.*`` and validation follows it.
#: Schemas under this prefix are control messages and are validated strictly.
CONTROL_SCHEMA_PREFIX = "mac.control."

FORBIDDEN_EXECUTION_KEYS = frozenset(
    {
        "argv",
        "cmd",
        "code",
        "command",
        "exec",
        "executable",
        "powershell",
        "script",
        "shell",
    }
)

# Compact structural contracts for the established mac.*.v1 payload family.
AGENTBUS_SCHEMA_REGISTRY: Dict[str, SchemaSpec] = {
    # --- control channel -------------------------------------------------
    # The message types that used to live in the `messages` table, with the
    # required fields MESSAGE_TYPE_REQUIRED_FIELDS enforced there. Registered
    # here so consolidating the two mechanisms does not downgrade a validated
    # control message into an opaque blob.
    "mac.control.nudge.v1": {
        "required": ["schema", "task_id"],
        "fields": {"schema": str, "task_id": str, "reason": str, "review_id": str},
    },
    "mac.control.review_request.v1": {
        "required": ["schema", "task_id", "review_id"],
        "fields": {"schema": str, "task_id": str, "review_id": str, "note": str},
    },
    "mac.control.review_result.v1": {
        "required": ["schema", "task_id", "status"],
        "fields": {"schema": str, "task_id": str, "status": str, "note": str},
    },
    "mac.control.status_update.v1": {
        "required": ["schema", "status"],
        "fields": {"schema": str, "status": str, "task_id": str, "detail": str},
    },
    "mac.control.help_request.v1": {
        "required": ["schema", "question"],
        "fields": {"schema": str, "question": str, "task_id": str},
    },
    "mac.control.evidence_request.v1": {
        "required": ["schema", "task_id"],
        "fields": {"schema": str, "task_id": str, "note": str},
    },
    "mac.control.decision_record.v1": {
        "required": ["schema", "summary"],
        "fields": {"schema": str, "summary": str, "task_id": str, "detail": str},
    },
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


def _execution_key_problems(
    value: Any,
    path: Tuple[str, ...] = (),
    exempt: Optional[frozenset] = None,
) -> List[str]:
    """Report execution-verb keys anywhere in a payload.

    Recursive, because a guard that only inspects top-level keys is defeated
    by one level of nesting.

    ``exempt`` names the fields the declared schema itself declares, and
    applies only at the top level: a registered schema is a reviewed contract,
    so a control schema that declares ``code`` may carry one, while a ``code``
    key nobody declared is refused.
    """
    problems: List[str] = []
    allowed = exempt or frozenset()
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                problems.append("payload keys must be strings at %s" % ".".join(path))
                continue
            key_path = path + (key,)
            if key.lower() in FORBIDDEN_EXECUTION_KEYS and not (
                not path and key in allowed
            ):
                problems.append(
                    "payload cannot contain execution key: %s" % ".".join(key_path)
                )
            problems.extend(_execution_key_problems(nested, key_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            problems.extend(_execution_key_problems(item, path + (str(index),)))
    return problems


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
    declared_name = declared if isinstance(declared, str) and declared else None
    spec = AGENTBUS_SCHEMA_REGISTRY.get(declared_name) if declared_name else None
    # Control payloads only -- see FORBIDDEN_EXECUTION_KEYS.
    execution_problems = (
        _execution_key_problems(payload, exempt=frozenset((spec or {}).get("fields", {})))
        if declared_name and declared_name.startswith(CONTROL_SCHEMA_PREFIX)
        else []
    )
    if declared_name is None:
        return None, execution_problems
    if spec is None:
        return declared_name, execution_problems
    problems: List[str] = list(execution_problems)
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
