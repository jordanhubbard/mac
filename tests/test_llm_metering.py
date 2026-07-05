"""LLM cost metering: streamed usage capture + client-side attribution.

Before this, ~90% of fleet traffic (streams) recorded no token counts and no
agent/task identity — duration_ms was the only cost proxy. These tests pin
the two halves of the fix: the router's usage-scanning stream wrapper, and
the vendored client's env-gated X-MAC attribution headers.
"""
from __future__ import annotations

import pytest

from mac.provider_router import Provider, ProviderRouter
from mac.router_app import ProviderProxy, _usage_capturing_stream


def _router():
    return ProviderRouter(
        [Provider("primary", "http://p/v1", priority=0)],
        failure_threshold=1,
        cooldown_seconds=1000.0,
    )


def _sse(events):
    return b"".join(b"data: " + e + b"\n\n" for e in events)


def test_usage_capturing_stream_passes_bytes_and_emits_final_usage():
    emitted = []
    chunks = [
        _sse([b'{"choices":[{"delta":{"content":"hel"}}]}']),
        _sse([b'{"choices":[{"delta":{"content":"lo"}}]}']),
        # usage frame split across two chunks (line broken mid-JSON)
        b'data: {"choices":[],"usage":{"prompt_tokens":100,'
        , b'"completion_tokens":25,"total_tokens":125}}\n\ndata: [DONE]\n\n',
    ]
    out = list(_usage_capturing_stream(iter(chunks), emitted.append))
    assert b"".join(out) == b"".join(chunks)  # byte-identical passthrough
    assert emitted == [
        {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125}
    ]


def test_usage_capturing_stream_emits_none_when_no_usage_frame():
    emitted = []
    list(_usage_capturing_stream(iter([_sse([b'{"choices":[]}']), b"data: [DONE]\n\n"]), emitted.append))
    assert emitted == [None]


def test_usage_capturing_stream_emits_on_client_disconnect():
    emitted = []

    def chunks():
        yield _sse([b'{"choices":[],"usage":{"total_tokens":7,"prompt_tokens":5,"completion_tokens":2}}'])
        yield b"data: more\n\n"

    gen = _usage_capturing_stream(chunks(), emitted.append)
    next(gen)
    gen.close()  # GeneratorExit mid-stream
    assert emitted == [{"total_tokens": 7, "prompt_tokens": 5, "completion_tokens": 2}]


def test_streamed_route_observation_carries_usage_and_full_duration():
    observed = []
    usage_frame = b'data: {"usage":{"prompt_tokens":11,"completion_tokens":3,"total_tokens":14}}\n\n'

    def fwd(provider, path, payload, *, timeout=60.0):
        return 200, iter([b"data: {}\n\n", usage_frame, b"data: [DONE]\n\n"])

    proxy = ProviderProxy(
        _router(),
        fwd,
        stream_forward_fn=fwd,
        default_model="m",
        route_observer=observed.append,
    )
    status, stream = proxy.stream_complete("/chat/completions", {"model": "*", "stream": True})
    assert status == 200
    # Observation is deferred until the stream is actually consumed.
    assert observed == []
    consumed = b"".join(stream)
    assert usage_frame in consumed
    assert len(observed) == 1
    detail = observed[0]
    assert detail["stream"] is True
    assert detail["usage"] == {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
    assert detail["outcome"] == "success"


def test_non_stream_observation_unchanged():
    observed = []

    def fwd(provider, path, payload, *, timeout=60.0):
        return 200, {"model": "m", "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}

    proxy = ProviderProxy(_router(), fwd, default_model="m", route_observer=observed.append)
    status, body = proxy.complete("/chat/completions", {"model": "*"})
    assert status == 200 and isinstance(body, dict)
    assert observed[0]["usage"]["total_tokens"] == 3


# ---------------------------------------------------------------------------
# Contract: llm.route event token fields (input_tokens / output_tokens /
# total_tokens) — the basis for real cost accounting and runaway-task detection.
# ---------------------------------------------------------------------------


def test_non_stream_proxied_completion_stamps_token_fields():
    """Proxied (non-streaming) completion with a usage block must produce an
    llm.route event with input_tokens, output_tokens, total_tokens set at the
    top level, not only buried under the nested usage dict."""
    observed = []

    def fwd(provider, path, payload, *, timeout=60.0):
        return 200, {
            "model": "m",
            "usage": {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125},
        }

    proxy = ProviderProxy(_router(), fwd, default_model="m", route_observer=observed.append)
    status, _ = proxy.complete("/chat/completions", {"model": "*"})
    assert status == 200
    assert len(observed) == 1
    ev = observed[0]
    # Top-level canonical token fields must be present and correct.
    assert ev.get("input_tokens") == 100, "input_tokens missing from llm.route event"
    assert ev.get("output_tokens") == 25, "output_tokens missing from llm.route event"
    assert ev.get("total_tokens") == 125, "total_tokens missing from llm.route event"
    # Nested usage dict is still preserved for back-compat.
    assert ev["usage"]["prompt_tokens"] == 100


def test_streamed_proxied_completion_stamps_token_fields():
    """Streaming proxied completion whose terminal usage frame carries token
    counts must produce an llm.route event with input_tokens, output_tokens,
    total_tokens populated at the top level."""
    observed = []
    usage_frame = (
        b'data: {"choices":[],"usage":{"prompt_tokens":50,"completion_tokens":10,"total_tokens":60}}\n\n'
    )

    def fwd(provider, path, payload, *, timeout=60.0):
        return 200, iter([b"data: {}\n\n", usage_frame, b"data: [DONE]\n\n"])

    proxy = ProviderProxy(
        _router(),
        fwd,
        stream_forward_fn=fwd,
        default_model="m",
        route_observer=observed.append,
    )
    status, stream = proxy.stream_complete("/chat/completions", {"model": "*", "stream": True})
    assert status == 200
    b"".join(stream)  # consume stream to trigger observation
    assert len(observed) == 1
    ev = observed[0]
    assert ev.get("input_tokens") == 50, "input_tokens missing from streamed llm.route event"
    assert ev.get("output_tokens") == 10, "output_tokens missing from streamed llm.route event"
    assert ev.get("total_tokens") == 60, "total_tokens missing from streamed llm.route event"
    # stream_no_usage must NOT be set when usage was available.
    assert not ev.get("stream_no_usage")


def test_streamed_proxied_completion_records_null_when_no_usage_frame():
    """Streaming proxied completion whose upstream does not send a usage frame
    must record null token fields (not omit them) so consumers can distinguish
    'no usage metered' from 'zero tokens used'."""
    observed = []

    def fwd(provider, path, payload, *, timeout=60.0):
        return 200, iter([b"data: {}\n\n", b"data: [DONE]\n\n"])

    proxy = ProviderProxy(
        _router(),
        fwd,
        stream_forward_fn=fwd,
        default_model="m",
        route_observer=observed.append,
    )
    status, stream = proxy.stream_complete("/chat/completions", {"model": "*", "stream": True})
    assert status == 200
    b"".join(stream)
    assert len(observed) == 1
    ev = observed[0]
    # Fields must be present (not missing) but null.
    assert "input_tokens" in ev, "input_tokens key must be present even when null"
    assert "output_tokens" in ev, "output_tokens key must be present even when null"
    assert "total_tokens" in ev, "total_tokens key must be present even when null"
    assert ev["input_tokens"] is None
    assert ev["output_tokens"] is None
    assert ev["total_tokens"] is None
    assert ev.get("stream_no_usage") is True


def test_anthropic_style_usage_keys_mapped_correctly():
    """Some upstreams (Anthropic native) return input_tokens/output_tokens
    directly instead of the OpenAI prompt_tokens/completion_tokens convention.
    The router must map both to the canonical top-level fields."""
    observed = []

    def fwd(provider, path, payload, *, timeout=60.0):
        return 200, {
            "model": "claude",
            "usage": {"input_tokens": 30, "output_tokens": 7, "total_tokens": 37},
        }

    proxy = ProviderProxy(_router(), fwd, default_model="claude", route_observer=observed.append)
    proxy.complete("/chat/completions", {"model": "*"})
    ev = observed[0]
    assert ev.get("input_tokens") == 30
    assert ev.get("output_tokens") == 7
    assert ev.get("total_tokens") == 37


# --- client-side attribution (vendored hermes) ------------------------------


def _agent_init_headers(monkeypatch, base_url: str, env: dict):
    import sys
    from pathlib import Path

    vendored = str(Path(__file__).resolve().parents[1] / "src" / "mac" / "_hermes")
    if vendored not in sys.path:
        sys.path.insert(0, vendored)
    from agent.agent_init import _mac_route_context_default_headers

    for key in (
        "MAC_AGENT_ID", "MAC_TASK_ID", "MAC_LEASE_ID", "MAC_HERMES_INSTANCE_ID",
        "MAC_FLEET_NAME", "MAC_HERMES_GATEWAY_BASE_URL", "CUSTOM_BASE_URL",
        "OPENAI_BASE_URL", "MAC_HUB_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return _mac_route_context_default_headers(base_url)


def test_attribution_headers_stamped_toward_mac_router(monkeypatch):
    headers = _agent_init_headers(
        monkeypatch,
        "http://100.72.16.110:8789/v1/",
        {
            "MAC_AGENT_ID": "agent_w1",
            "MAC_TASK_ID": "task_abc",
            "MAC_LEASE_ID": "lease_1",
            "MAC_HERMES_GATEWAY_BASE_URL": "http://100.72.16.110:8789/v1",
        },
    )
    assert headers["x-mac-agent-id"] == "agent_w1"
    assert headers["x-mac-task-id"] == "task_abc"
    assert headers["x-mac-lease-id"] == "lease_1"


def test_attribution_headers_never_sent_to_third_parties(monkeypatch):
    headers = _agent_init_headers(
        monkeypatch,
        "https://api.openai.com/v1",
        {
            "MAC_AGENT_ID": "agent_w1",
            "MAC_TASK_ID": "task_abc",
            "MAC_HERMES_GATEWAY_BASE_URL": "http://100.72.16.110:8789/v1",
        },
    )
    assert headers == {}


def test_attribution_headers_absent_outside_fleet_runs(monkeypatch):
    headers = _agent_init_headers(
        monkeypatch,
        "http://100.72.16.110:8789/v1",
        {"MAC_HERMES_GATEWAY_BASE_URL": "http://100.72.16.110:8789/v1"},
    )
    assert headers == {}
