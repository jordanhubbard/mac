"""th-merge-02: the in-mac router's /v1 surface is auth-gated as an agent action."""

from __future__ import annotations

from mac.api import _required_scope


def test_v1_router_requires_agent_scope_any_method():
    # Inference is an agent action: the OpenAI front door requires agent scope
    # (not the broad `write`), regardless of HTTP method, so it is never an open
    # proxy when the API is bound to a network interface.
    assert _required_scope("POST", "/v1/chat/completions") == "agent"
    assert _required_scope("POST", "/v1/embeddings") == "agent"
    assert _required_scope("GET", "/v1/models") == "agent"
    assert _required_scope("GET", "/v1") == "agent"


def test_non_v1_paths_unchanged():
    assert _required_scope("GET", "/health") is None
    assert _required_scope("GET", "/tasks") == "read"
    assert _required_scope("POST", "/tasks") == "write"
    assert _required_scope("POST", "/agentbus") == "agent"
    assert _required_scope("POST", "/action-events") == "agent"
    assert _required_scope("POST", "/agents/agent_1/openshell/status") == "agent"
    assert _required_scope("GET", "/optimizer/status") == "read"
    assert _required_scope("POST", "/optimizer/tick") == "admin"
    assert _required_scope("POST", "/optimizer/policies") == "admin"


def test_secret_resolve_requires_secret_scope():
    # th-merge-07: audited reveal-by-name (Slack fetcher) is gated on `secret`.
    assert _required_scope("POST", "/secrets/slack.rocky.omgjkh.bot/resolve") == "secret"
    assert _required_scope("POST", "/secrets/github.token/resolve") == "secret"


def test_evidence_artifact_content_requires_secret_scope():
    assert _required_scope("GET", "/evidence/ev_123/artifacts") == "read"
    assert _required_scope("GET", "/evidence/ev_123/artifacts/eva_456") == "secret"
