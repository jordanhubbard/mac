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
