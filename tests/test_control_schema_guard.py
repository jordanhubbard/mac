"""The control channel stays strict when it moves onto agentbus.

`messages` and agentbus were separate mechanisms with deliberately different
rules: control messages refused execution-verb keys so an agent could not
smuggle a job spec through a channel consumers treat as data, while agentbus
carried opaque content blobs where a field named ``command`` is legitimate
(a patch payload, for instance).

Consolidating onto one transport is only safe if BOTH regimes survive. These
tests pin that, so a later simplification cannot quietly flatten them to
whichever is more convenient.
"""

from __future__ import annotations

import pytest

from mac.agentbus_schemas import (
    CONTROL_SCHEMA_PREFIX,
    FORBIDDEN_EXECUTION_KEYS,
    AGENTBUS_SCHEMA_REGISTRY,
    is_registered,
    validate_payload,
)


CONTROL_SCHEMAS = sorted(
    name for name in AGENTBUS_SCHEMA_REGISTRY if name.startswith(CONTROL_SCHEMA_PREFIX)
)


def test_the_control_message_types_are_registered():
    """Every type the old `messages` table carried has a contract here."""
    for kind in (
        "nudge",
        "review_request",
        "review_result",
        "status_update",
        "help_request",
        "evidence_request",
        "decision_record",
    ):
        name = "%s%s.v1" % (CONTROL_SCHEMA_PREFIX, kind)
        assert is_registered(name), name


@pytest.mark.parametrize("schema", CONTROL_SCHEMAS)
@pytest.mark.parametrize("key", sorted(FORBIDDEN_EXECUTION_KEYS))
def test_control_payloads_refuse_execution_keys(schema, key):
    spec = AGENTBUS_SCHEMA_REGISTRY[schema]
    if key in spec.get("fields", {}):
        pytest.skip("%s declares %s as a real field" % (schema, key))
    payload = {"schema": schema, key: "rm -rf /"}
    _, problems = validate_payload(payload)
    assert any("execution key" in problem for problem in problems), problems


def test_the_guard_is_recursive():
    """A top-level-only check is defeated by one level of nesting."""
    _, problems = validate_payload(
        {"schema": "mac.control.nudge.v1", "task_id": "t1", "d": {"script": "x"}}
    )
    assert ["payload cannot contain execution key: d.script"] == problems

    _, listed = validate_payload(
        {"schema": "mac.control.nudge.v1", "task_id": "t1", "a": [{"exec": "x"}]}
    )
    assert ["payload cannot contain execution key: a.0.exec"] == listed


def test_content_streams_stay_permissive():
    """agentbus is not a control channel and must not inherit its strictness.

    A patch blob may legitimately carry a field named ``command``; the stream
    stores it, nothing executes it.
    """
    assert validate_payload({"command": "stored-not-executed"}) == (None, [])
    assert validate_payload(
        {"schema": "mac.media.share.v1", "command": "stored"}
    )[1] == []


def test_a_declared_field_named_like_a_verb_is_still_legal():
    """`mac.agentbus.error.v1` carries an error `code`, not executable code."""
    _, problems = validate_payload(
        {
            "schema": "mac.agentbus.error.v1",
            "code": "agent_unreachable",
            "message": "m",
            "detail": "d",
        }
    )
    assert problems == []


def test_a_control_message_cannot_be_laundered_by_nesting_the_verb_deeper():
    """The exemption is top-level and per-declared-field, never inherited."""
    _, problems = validate_payload(
        {
            "schema": "mac.agentbus.error.v1",
            "code": "ok",
            "message": "m",
            "detail": "d",
            "nested": {"code": "smuggled"},
        }
    )
    # error.v1 is not a control schema, so it is permissive by design.
    assert problems == []

    _, control = validate_payload(
        {"schema": "mac.control.status_update.v1", "status": "ok", "n": {"code": "x"}}
    )
    assert control == ["payload cannot contain execution key: n.code"]
