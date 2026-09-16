"""th-merge-02: the in-mac router's /v1 surface is auth-gated as an agent action."""

from __future__ import annotations

from mac.api import TokenPrincipal, _required_scope
from mac.worker_credentials import WORKER_SCOPES


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


def test_review_tick_requires_review_advance_not_bare_admin():
    # Regression: this used to be flat "admin", which meant no ordinary fleet
    # worker credential could ever call it -- every worker's own post-verdict
    # /reviews/default/tick call was rejected with "token lacks required
    # scope: admin", so the REVIEWING queue only drained when an admin token
    # happened to invoke it by hand. `review:advance` is minted into every
    # worker credential (WORKER_SCOPES) instead.
    assert _required_scope("POST", "/reviews/default/tick") == "review:advance"
    assert _required_scope("POST", "/reviews/default/tick?limit=10") == "review:advance"


def test_worker_credential_carries_review_advance_scope():
    assert "review:advance" in WORKER_SCOPES


def test_review_advance_scope_semantics():
    # Admin inherits every scope, including the new one.
    assert TokenPrincipal(scopes=frozenset({"admin"})).has_scope("review:advance")
    # A worker-shaped token (WORKER_SCOPES) has it directly.
    assert TokenPrincipal(scopes=frozenset(WORKER_SCOPES)).has_scope("review:advance")
    # A plain `write` token does NOT inherit it -- deliberately narrower than
    # the {"roles", "workflow"} write-inherited scopes, since the review tick
    # is the closest thing the swarm has to an auto-merge button.
    assert not TokenPrincipal(scopes=frozenset({"write"})).has_scope("review:advance")
    # `read` alone obviously doesn't have it either.
    assert not TokenPrincipal(scopes=frozenset({"read"})).has_scope("review:advance")
