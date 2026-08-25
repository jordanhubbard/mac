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
    assert _required_scope("GET", "/agents/agent_1/directives/effective") == "agent"
    # Self-only policy distribution, deliberately NOT the generic "read" scope a
    # GET would otherwise fall through to: the policy names the fleet's hub and
    # gateway hosts plus the binaries permitted to reach them.
    assert _required_scope("GET", "/agents/agent_1/openshell/policy") == "agent"
    assert (
        _required_scope("POST", "/agents/agent_1/directive-activations/activation_1/ack") == "agent"
    )
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


def test_memory_rewrite_routes_are_admin_not_agent():
    """The two memory routes that rewrite the vector store are admin-gated.

    They sit under /v1 with the rest of the memory surface, where the blanket
    /v1 rule would give them the same `agent` scope as model inference. But
    promotion can retire medium-tier points and reconciliation re-embeds an
    entire collection, so an ordinary bound agent token must not be able to
    fire either one.
    """
    assert _required_scope("POST", "/v1/memory/promote") == "admin"
    assert _required_scope("POST", "/v1/memory/reconcile-embeddings") == "admin"
    # The read-only neighbours keep the surrounding /v1 behaviour.
    assert _required_scope("GET", "/v1/memory/health") == "agent"
    assert _required_scope("GET", "/v1/memory/recall") == "agent"
