"""Tests for the owned, in-process provider resolution (ADR 0001 keystone).

These verify that mac can decide the per-agent Hermes provider override from
the environment, produce the override contract the gateway needs, emit a
legible/secret-free decision, and do all of it without importing Hermes or
rewriting any source file.
"""

from mac.agent_provider import (
    ProviderDecision,
    record_provider_decision,
    resolve_agent_provider,
)


def test_no_override_yields_hermes_default():
    decision = resolve_agent_provider(env={})
    assert decision.override_active is False
    assert decision.requested_provider == ""
    assert decision.source == "hermes-default"
    # Standalone override contract is empty when nothing is configured.
    assert decision.override_kwargs() == {}
    assert any("Hermes default" in line for line in decision.rationale)


def test_model_alone_triggers_override_with_custom_provider():
    decision = resolve_agent_provider(env={"MAC_HERMES_GATEWAY_MODEL": "claude-opus-4-8"})
    assert decision.override_active is True
    assert decision.model == "claude-opus-4-8"
    # No provider configured -> defaults to "custom", matching the old shim.
    assert decision.requested_provider == "custom"
    assert decision.won_by["model"] == "MAC_HERMES_GATEWAY_MODEL"
    assert any("defaulted to 'custom'" in line for line in decision.rationale)


def test_env_precedence_first_key_wins():
    env = {
        "MAC_HERMES_GATEWAY_MODEL": "primary-model",
        "HERMES_INFERENCE_MODEL": "fallback-model",
        "ACC_LLM_MODEL": "legacy-model",
    }
    decision = resolve_agent_provider(env=env)
    assert decision.model == "primary-model"
    assert decision.won_by["model"] == "MAC_HERMES_GATEWAY_MODEL"


def test_blank_values_are_skipped_to_next_key():
    env = {
        "MAC_HERMES_GATEWAY_PROVIDER": "   ",  # blank -> skipped
        "HERMES_INFERENCE_PROVIDER": "anthropic",
    }
    decision = resolve_agent_provider(env=env)
    assert decision.requested_provider == "anthropic"
    assert decision.won_by["provider"] == "HERMES_INFERENCE_PROVIDER"


def test_tokenhub_url_is_suffixed_with_v1():
    decision = resolve_agent_provider(env={"TOKENHUB_URL": "http://hub.internal:8080/"})
    assert decision.override_active is True
    assert decision.base_url == "http://hub.internal:8080/v1"
    assert decision.won_by["base_url"] == "TOKENHUB_URL"
    assert any("+/v1" in line for line in decision.rationale)


def test_explicit_base_url_is_not_suffixed():
    decision = resolve_agent_provider(
        env={"MAC_HERMES_GATEWAY_BASE_URL": "https://api.example.com/v2"}
    )
    assert decision.base_url == "https://api.example.com/v2"


def test_api_key_alone_does_not_open_an_override():
    # Matches the shim: the override condition is model/provider/base_url, not key.
    decision = resolve_agent_provider(env={"OPENAI_API_KEY": "sk-secret"})
    assert decision.override_active is False
    assert decision.api_key_present is True


def test_override_kwargs_layers_onto_hermes_resolve_result():
    env = {
        "MAC_HERMES_GATEWAY_PROVIDER": "custom",
        "TOKENHUB_URL": "http://hub:9000",
        "TOKENHUB_AGENT_KEY": "th-agent-key",
    }
    decision = resolve_agent_provider(env=env)
    # Simulate what Hermes' resolve_runtime_provider would have returned.
    base = {
        "provider": "custom",
        "api_mode": "chat_completions",
        "base_url": "ignored",
        "api_key": "stale",
        "source": "pool",
    }
    kwargs = decision.override_kwargs(base)
    assert kwargs["base_url"] == "http://hub:9000/v1"
    assert kwargs["api_key"] == "th-agent-key"
    assert kwargs["source"] == "mac-gateway-explicit"
    # Hermes-derived fields it does not override are preserved.
    assert kwargs["api_mode"] == "chat_completions"


def test_observable_never_leaks_the_api_key():
    env = {"MAC_HERMES_GATEWAY_MODEL": "m", "OPENAI_API_KEY": "sk-DO-NOT-LEAK"}
    decision = resolve_agent_provider(env=env)
    observable = decision.observable()
    serialized = repr(observable) + repr(decision) + "\n".join(decision.rationale)
    assert "sk-DO-NOT-LEAK" not in serialized
    assert observable["api_key_present"] is True
    assert observable["won_by"]["api_key"] == "OPENAI_API_KEY"
    assert observable["schema"] == "mac.agent_provider.decision.v1"


def test_record_provider_decision_emits_secret_free_observation():
    captured = {}

    class FakeObs:
        def record_observation(self, **kwargs):
            captured.update(kwargs)
            return kwargs

    env = {"MAC_HERMES_GATEWAY_MODEL": "claude-opus-4-8", "OPENAI_API_KEY": "sk-secret"}
    decision = resolve_agent_provider(env=env)
    record_provider_decision(FakeObs(), decision, agent_id="agent_42")

    assert captured["name"] == "hermes.provider.resolved"
    assert captured["layer"] == "hermes"
    assert captured["subject_type"] == "agent"
    assert captured["subject_id"] == "agent_42"
    assert captured["level"] == "info"  # override active
    assert "sk-secret" not in repr(captured)
    assert captured["detail"]["model"] == "claude-opus-4-8"


def test_record_provider_decision_tolerates_no_observability():
    decision = resolve_agent_provider(env={})
    assert record_provider_decision(None, decision) is None


def test_decision_is_frozen_dataclass():
    decision = resolve_agent_provider(env={})
    assert isinstance(decision, ProviderDecision)
    try:
        decision.override_active = True  # type: ignore[misc]
    except Exception as exc:  # FrozenInstanceError
        assert "cannot assign" in str(exc) or "frozen" in type(exc).__name__.lower()
    else:
        raise AssertionError("ProviderDecision should be immutable")
