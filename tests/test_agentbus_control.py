from __future__ import annotations

from mac.agentbus_control import (
    AGENT_REFLECTION_SCHEMA,
    REFLECT_REQUEST_CONTENT_TYPE,
    REFLECT_REQUEST_SCHEMA,
    REFLECT_REQUEST_TOPIC,
    REFLECT_RESULT_CONTENT_TYPE,
    REFLECT_RESULT_SCHEMA,
    REFLECT_RESULT_TOPIC,
    _REFLECTION_MAX_WORDS,
    _normalize_narrative,
    agent_reflection_payload,
    reflect_request_payload,
    reflect_result_payload,
)

# ---------------------------------------------------------------------------
# reflect_request / reflect_result wire-constants
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# agent_reflection_payload — helpers
# ---------------------------------------------------------------------------


def test_normalize_narrative_returns_none_for_none() -> None:
    assert _normalize_narrative(None) is None


def test_normalize_narrative_returns_none_for_empty_string() -> None:
    assert _normalize_narrative("") is None


def test_normalize_narrative_returns_none_for_whitespace_only() -> None:
    assert _normalize_narrative("   \t\n  ") is None


def test_normalize_narrative_strips_surrounding_whitespace() -> None:
    result = _normalize_narrative("  hello world  ")
    assert result == "hello world"


def test_normalize_narrative_passes_short_text_unchanged() -> None:
    text = "I am agent_alpha processing task 42."
    assert _normalize_narrative(text) == text


def test_normalize_narrative_truncates_at_word_boundary() -> None:
    # Build a string that exceeds the cap by a handful of words.
    long_text = " ".join(f"word{i}" for i in range(_REFLECTION_MAX_WORDS + 10))
    result = _normalize_narrative(long_text)
    assert result is not None
    # Must end with the truncation marker.
    assert result.endswith(" [...]")
    # Word count of the payload text (excluding the marker token) must equal cap.
    words_before_marker = result[: -len(" [...]")].split()
    assert len(words_before_marker) == _REFLECTION_MAX_WORDS


def test_normalize_narrative_exactly_at_cap_is_not_truncated() -> None:
    at_cap = " ".join(f"w{i}" for i in range(_REFLECTION_MAX_WORDS))
    result = _normalize_narrative(at_cap)
    assert result is not None
    assert "[...]" not in result
    assert len(result.split()) == _REFLECTION_MAX_WORDS


# ---------------------------------------------------------------------------
# agent_reflection_payload — inventory-only (backward-compatible)
# ---------------------------------------------------------------------------

_SAMPLE_AGENT: dict = {
    "id": "agent_alpha",
    "name": "alpha",
    "capabilities": ["code", "review"],
    "resources": {"cpu": 4},
    "status": "idle",
    "health_status": "healthy",
    "current_task_id": None,
    "running_digest": None,
    "role_id": "worker",
    "hermes_instance_id": "hermes-1",
    "installed_packages": {"mac": "0.9.0"},
    "last_seen_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


def test_agent_reflection_payload_inventory_only_shape() -> None:
    """Without a narrative the payload contains agent inventory but no reflection key."""
    payload = agent_reflection_payload(agent=_SAMPLE_AGENT)

    assert payload["schema"] == AGENT_REFLECTION_SCHEMA
    assert payload["agent_id"] == "agent_alpha"
    assert "agent" in payload
    assert "summary" in payload
    # No narrative -> no reflection key on the wire.
    assert "reflection" not in payload
    # No request_id supplied -> key absent.
    assert "request_id" not in payload


def test_agent_reflection_payload_inventory_agent_fields() -> None:
    """The agent sub-dict must mirror the inventory fields from the agent record."""
    payload = agent_reflection_payload(agent=_SAMPLE_AGENT)
    inv = payload["agent"]

    assert inv["id"] == "agent_alpha"
    assert inv["name"] == "alpha"
    assert inv["capabilities"] == ["code", "review"]
    assert inv["resources"] == {"cpu": 4}
    assert inv["status"] == "idle"
    assert inv["health_status"] == "healthy"
    assert inv["role_id"] == "worker"
    assert inv["hermes_instance_id"] == "hermes-1"
    assert inv["installed_packages"] == {"mac": "0.9.0"}


def test_agent_reflection_payload_summary_format() -> None:
    payload = agent_reflection_payload(agent=_SAMPLE_AGENT)
    assert payload["summary"] == (
        "agent alpha (agent_alpha) is idle/healthy; capabilities: code, review"
    )


def test_agent_reflection_payload_includes_request_id() -> None:
    payload = agent_reflection_payload(
        agent=_SAMPLE_AGENT,
        request_id="req-xyz",
    )
    assert payload["request_id"] == "req-xyz"
    assert "reflection" not in payload


def test_agent_reflection_payload_minimal_agent() -> None:
    """All optional agent fields absent; function must not raise."""
    payload = agent_reflection_payload(agent={"id": "agent_bare"})

    assert payload["agent_id"] == "agent_bare"
    assert payload["agent"]["capabilities"] == []
    assert payload["agent"]["resources"] == {}
    assert payload["agent"]["installed_packages"] == {}
    assert "reflection" not in payload


# ---------------------------------------------------------------------------
# agent_reflection_payload — narrative-included case
# ---------------------------------------------------------------------------


def test_agent_reflection_payload_with_narrative_adds_reflection_key() -> None:
    """When a non-empty narrative is supplied the payload includes payload.reflection."""
    narrative = "Currently idle; last completed task was a code review for PR #42."
    payload = agent_reflection_payload(agent=_SAMPLE_AGENT, narrative=narrative)

    assert "reflection" in payload
    assert payload["reflection"] == narrative
    # Core inventory fields must still be present.
    assert "agent" in payload
    assert "summary" in payload
    assert payload["agent_id"] == "agent_alpha"


def test_agent_reflection_payload_narrative_and_request_id() -> None:
    payload = agent_reflection_payload(
        agent=_SAMPLE_AGENT,
        narrative="Processing task 99.",
        request_id="req-1",
    )
    assert payload["reflection"] == "Processing task 99."
    assert payload["request_id"] == "req-1"


def test_agent_reflection_payload_empty_narrative_omits_reflection() -> None:
    """Empty-string narrative must not add the reflection key (empty-value normalization)."""
    payload = agent_reflection_payload(agent=_SAMPLE_AGENT, narrative="")
    assert "reflection" not in payload


def test_agent_reflection_payload_whitespace_narrative_omits_reflection() -> None:
    """Whitespace-only narrative must be treated as absent."""
    payload = agent_reflection_payload(agent=_SAMPLE_AGENT, narrative="   \n\t  ")
    assert "reflection" not in payload


def test_agent_reflection_payload_narrative_truncated_when_overlong() -> None:
    """Narrative exceeding _REFLECTION_MAX_WORDS is bounded and marked."""
    long_narrative = " ".join(f"word{i}" for i in range(_REFLECTION_MAX_WORDS + 20))
    payload = agent_reflection_payload(agent=_SAMPLE_AGENT, narrative=long_narrative)

    assert "reflection" in payload
    reflection = payload["reflection"]
    assert reflection.endswith(" [...]")
    words_before_marker = reflection[: -len(" [...]")].split()
    assert len(words_before_marker) == _REFLECTION_MAX_WORDS


def test_agent_reflection_payload_narrative_strips_whitespace() -> None:
    payload = agent_reflection_payload(
        agent=_SAMPLE_AGENT, narrative="  trimmed narrative  "
    )
    assert payload["reflection"] == "trimmed narrative"
