from __future__ import annotations

from mac.agentbus_control import (
    REFLECT_REQUEST_CONTENT_TYPE,
    REFLECT_REQUEST_SCHEMA,
    REFLECT_REQUEST_TOPIC,
    REFLECT_RESULT_CONTENT_TYPE,
    REFLECT_RESULT_SCHEMA,
    REFLECT_RESULT_TOPIC,
    reflect_request_payload,
    reflect_result_payload,
)


def test_reflect_request_wire_constants() -> None:
    assert REFLECT_REQUEST_SCHEMA == "mac.agentbus.reflect_request.v1"
    assert REFLECT_REQUEST_TOPIC == "mac.reflect.request.v1"
    assert (
        REFLECT_REQUEST_CONTENT_TYPE
        == "application/vnd.mac.reflect-request+json"
    )


def test_reflect_result_wire_constants() -> None:
    assert REFLECT_RESULT_SCHEMA == "mac.agentbus.reflect_result.v1"
    assert REFLECT_RESULT_TOPIC == "mac.reflect.result.v1"
    assert (
        REFLECT_RESULT_CONTENT_TYPE == "application/vnd.mac.reflect-result+json"
    )


def test_reflect_request_payload_uses_schema_and_required_fields() -> None:
    assert reflect_request_payload(
        sender_agent_id="agent-sender",
        query="Summarize your runtime identity.",
    ) == {
        "schema": REFLECT_REQUEST_SCHEMA,
        "sender_agent_id": "agent-sender",
        "query": "Summarize your runtime identity.",
    }


def test_reflect_request_payload_includes_request_id() -> None:
    assert reflect_request_payload(
        sender_agent_id="agent-sender",
        query="What are you doing?",
        request_id="reflect-1",
    ) == {
        "schema": REFLECT_REQUEST_SCHEMA,
        "sender_agent_id": "agent-sender",
        "query": "What are you doing?",
        "request_id": "reflect-1",
    }


def test_reflect_result_payload_uses_schema_and_result_fields() -> None:
    assert reflect_result_payload(
        request_id="reflect-1",
        agent_id="agent-responder",
        response="I am processing assigned work.",
        word_count=5,
    ) == {
        "schema": REFLECT_RESULT_SCHEMA,
        "request_id": "reflect-1",
        "agent_id": "agent-responder",
        "response": "I am processing assigned work.",
        "word_count": 5,
    }


def test_reflect_result_payload_normalizes_word_count() -> None:
    payload = reflect_result_payload(
        request_id="reflect-1",
        agent_id="agent-responder",
        response="Done.",
        word_count="1",  # type: ignore[arg-type]
    )

    assert payload["word_count"] == 1
