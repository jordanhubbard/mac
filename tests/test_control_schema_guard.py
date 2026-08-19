"""The control message types have contracts on agentbus.

`messages` and agentbus were separate mechanisms. Consolidating onto agentbus
must not downgrade a validated control message -- one with required fields --
into an opaque blob, so the seven types the `messages` table carried are
registered here with the fields MESSAGE_TYPE_REQUIRED_FIELDS enforced.

These tests deliberately do NOT check payload key names. The old channel
refused keys spelled like execution verbs (command/exec/script/shell); that
guard predates OpenShell and was removed with it, because containment is
enforced by the sandbox -- which commands an agent may run, which endpoints it
may reach -- not by inspecting a key's spelling.
"""

from __future__ import annotations

import pytest

from mac.agentbus_schemas import (
    AGENTBUS_SCHEMA_REGISTRY,
    is_registered,
    validate_payload,
)

CONTROL_TYPES = (
    "nudge",
    "review_request",
    "review_result",
    "status_update",
    "help_request",
    "evidence_request",
    "decision_record",
)


@pytest.mark.parametrize("kind", CONTROL_TYPES)
def test_every_control_message_type_is_registered(kind):
    assert is_registered("mac.control.%s.v1" % kind)


@pytest.mark.parametrize("kind", CONTROL_TYPES)
def test_required_fields_are_enforced(kind):
    """A control message missing a required field is reported, not accepted."""
    name = "mac.control.%s.v1" % kind
    required = [f for f in AGENTBUS_SCHEMA_REGISTRY[name]["required"] if f != "schema"]
    assert required, "%s declares no required field beyond schema" % name
    _, problems = validate_payload({"schema": name})
    for field in required:
        assert "missing required field: %s" % field in problems


@pytest.mark.parametrize("kind", CONTROL_TYPES)
def test_a_well_formed_control_message_validates(kind):
    name = "mac.control.%s.v1" % kind
    spec = AGENTBUS_SCHEMA_REGISTRY[name]
    payload = {"schema": name}
    for field in spec["required"]:
        if field != "schema":
            payload[field] = "x"
    assert validate_payload(payload) == (name, [])


def test_wrong_field_types_are_reported():
    _, problems = validate_payload(
        {"schema": "mac.control.nudge.v1", "task_id": ["not", "a", "string"]}
    )
    assert any("wrong type" in problem for problem in problems), problems


@pytest.mark.parametrize("key", ["command", "exec", "script", "shell", "argv", "code"])
def test_payload_keys_are_not_filtered_by_name(key):
    """The pre-OpenShell execution-key guard is gone, on purpose.

    Containment is the sandbox's job. A payload key named `command` is data;
    nothing on this path executes it. If this test starts failing because a
    guard came back, the question to ask is what boundary it actually enforces
    that the sandbox does not.
    """
    assert validate_payload({"schema": "mac.control.nudge.v1", "task_id": "t", key: "x"}) == (
        "mac.control.nudge.v1",
        [],
    )
    assert validate_payload({key: "x"}) == (None, [])
