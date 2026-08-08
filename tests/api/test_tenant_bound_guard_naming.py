"""`refuse_tenant_bound` is a multi-tenancy check, not an authorization gate.

Named `require_global_fleet` it read as "require fleet-level authority" and was
taken that way at 61 call sites. Two holes came from that reading: an ordinary
`write` token could open a debug shell on any fleet host (PR #300), and could
author the OpenShell guardrail policy every `--yolo` agent runs under (PR #303).

The behaviour was always correct and is unchanged here. These tests pin the
semantics the name now advertises, so the next reader cannot re-acquire the
assumption.
"""

from __future__ import annotations

import pytest

from mac.api import TokenPrincipal
from mac.models import AuthorizationError


def _principal(**kw) -> TokenPrincipal:
    return TokenPrincipal(**kw)


def test_the_misleading_name_is_gone_with_no_alias():
    """An alias would preserve the exact name this change exists to remove."""
    assert hasattr(TokenPrincipal, "refuse_tenant_bound")
    assert not hasattr(TokenPrincipal, "require_global_fleet")


def test_an_untenanted_non_admin_token_passes():
    """The whole point: this does NOT confer privilege. An ordinary write token
    sails through, which is why it can never be a route's only gate."""
    _principal(scopes=("write",), client_id="ci").refuse_tenant_bound()
    _principal(scopes=("read",), client_id="dash").refuse_tenant_bound()
    _principal(scopes=("deploy",), client_id="cd").refuse_tenant_bound()


def test_a_tenant_bound_non_admin_token_is_refused():
    """The one thing it does do, and should keep doing."""
    with pytest.raises(AuthorizationError, match="bound to a tenant"):
        _principal(scopes=("write",), tenant_id="tenant-a").refuse_tenant_bound()


def test_a_tenant_bound_admin_token_passes():
    _principal(scopes=("admin",), tenant_id="tenant-a").refuse_tenant_bound()


def test_docstring_states_it_is_not_an_authorization_gate():
    """The prose is the deliverable here as much as the name."""
    doc = TokenPrincipal.refuse_tenant_bound.__doc__ or ""
    assert "NOT an authorization gate" in doc
    assert "_required_scope" in doc
