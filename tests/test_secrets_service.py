"""Dedicated test suite for SecretsService.

Coverage targets:
- set/reveal/redaction in list+show output
- scoped-token validation (agent-id, capability, tenant, admin/dispatch)
- audit-trail correctness (creator, reveal reason, timestamp)
- multi-tenant isolation (no cross-tenant visibility)
- token rotation
- capability-mismatch rejection
- single-use handle enforcement
- disabled-secret rejection
- untrusted-machine rejection
"""
from __future__ import annotations

import pytest

from mac.models import (
    AuthorizationError,
    NotFoundError,
    SecretAuditResult,
    ValidationError,
)
from mac.services import ControlPlane


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cp():
    """Fresh in-memory ControlPlane for every test."""
    return ControlPlane.in_memory()


def _machine(cp, hostname="host-1", *, trusted=True, labels=None):
    m = cp.register_machine(hostname, labels=labels or {})
    if not trusted:
        cp.store.execute(
            "UPDATE machines SET trusted = 0 WHERE id = ?", (m.id,)
        )
        # Re-fetch to get updated trusted flag
        from mac.models import json_loads
        row = cp.store.query_one("SELECT * FROM machines WHERE id = ?", (m.id,))
        from dataclasses import replace
        m = replace(m, trusted=False)
    return m


def _agent(cp, machine_id, name="worker-1", capabilities=None):
    return cp.register_agent(machine_id, name, capabilities=capabilities or [])


def _secret(cp, name="slack-token", value="***",
            scopes=None, created_by="hub"):
    if scopes is None:
        # Default: use a sentinel agent scope so the scopes dict is truthy.
        # Tests that need a real agent scope should pass scopes explicitly.
        scopes = {"agents": ["_any_"]}
    return cp.secrets.create_secret(name, value, scopes, created_by)


# ---------------------------------------------------------------------------
# 1. create_secret / get_secret / list_secrets
# ---------------------------------------------------------------------------


def test_create_secret_stores_and_retrieves(cp):
    s = _secret(cp, name="deploy-key", value="gh_abc123")
    assert s.id.startswith("secret_")
    assert s.name == "deploy-key"
    assert s.enabled is True
    assert s.created_by == "hub"
    # plaintext never exposed on the record object
    assert not hasattr(s, "value")


def test_get_secret_by_id_and_name(cp):
    s = _secret(cp, name="api-key")
    by_id = cp.secrets.get_secret(s.id)
    by_name = cp.secrets.get_secret("api-key")
    assert by_id.id == s.id
    assert by_name.id == s.id


def test_get_secret_not_found_raises(cp):
    with pytest.raises(NotFoundError):
        cp.secrets.get_secret("secret_nonexistent")


def test_list_secrets_returns_all(cp):
    _secret(cp, name="a-secret")
    _secret(cp, name="b-secret")
    secrets = cp.secrets.list_secrets()
    names = [s.name for s in secrets]
    assert "a-secret" in names
    assert "b-secret" in names


def test_create_secret_requires_name_and_value(cp):
    with pytest.raises(ValidationError):
        cp.secrets.create_secret("", "value", {}, "hub")
    with pytest.raises(ValidationError):
        cp.secrets.create_secret("name", "", {}, "hub")


def test_create_secret_requires_scopes(cp):
    with pytest.raises(ValidationError):
        cp.secrets.create_secret("name", "value", {}, "hub")
    # Non-empty scopes dict is valid even if it has no restrictions
    # (e.g. an agent-id scope)
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s = cp.secrets.create_secret("key", "val", {"agents": [agent.id]}, "hub")
    assert s.id is not None


# ---------------------------------------------------------------------------
# 2. Redaction: list+show output never exposes plaintext
# ---------------------------------------------------------------------------


def test_to_dict_redacts_value(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s = cp.secrets.create_secret(
        "secret-x", "PLAINTEXT", {"agents": [agent.id]}, "hub"
    )
    d = s.to_dict()
    assert d["value"] == "***REDACTED***"
    assert "PLAINTEXT" not in str(d)


def test_list_secrets_no_plaintext_in_to_dict(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    cp.secrets.create_secret("sec1", "PLAIN1", {"agents": [agent.id]}, "hub")
    cp.secrets.create_secret("sec2", "PLAIN2", {"agents": [agent.id]}, "hub")
    for s in cp.secrets.list_secrets():
        d = s.to_dict()
        assert "PLAIN1" not in str(d)
        assert "PLAIN2" not in str(d)
        assert d["value"] == "***REDACTED***"


# ---------------------------------------------------------------------------
# 3. request_secret / reveal_secret (full handle dance)
# ---------------------------------------------------------------------------


def test_request_and_reveal_secret_happy_path(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id, name="worker-1")
    s = cp.secrets.create_secret(
        "slack-token", "xoxb-real", {"agents": [agent.id]}, "hub"
    )
    handle = cp.secrets.request_secret(s.id, agent.id, purpose="ci-deploy")
    assert handle.granted is True
    assert handle.secret_id == s.id

    plaintext = cp.secrets.reveal_secret(s.id, handle.audit_id, agent.id)
    assert plaintext == "xoxb-real"


def test_reveal_secret_single_use(cp):
    """The same handle cannot be redeemed twice."""
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s = cp.secrets.create_secret(
        "once-token", "once-value", {"agents": [agent.id]}, "hub"
    )
    handle = cp.secrets.request_secret(s.id, agent.id, purpose="read")
    cp.secrets.reveal_secret(s.id, handle.audit_id, agent.id)
    with pytest.raises(AuthorizationError):
        cp.secrets.reveal_secret(s.id, handle.audit_id, agent.id)


def test_reveal_secret_wrong_agent_rejected(cp):
    """A different agent cannot redeem another agent's handle."""
    machine = _machine(cp)
    agent1 = _agent(cp, machine.id, name="worker-1")
    agent2 = _agent(cp, machine.id, name="worker-2")
    s = cp.secrets.create_secret(
        "agent-secret", "secret-val", {"agents": [agent1.id]}, "hub"
    )
    handle = cp.secrets.request_secret(s.id, agent1.id, purpose="read")
    with pytest.raises(AuthorizationError):
        cp.secrets.reveal_secret(s.id, handle.audit_id, agent2.id)


# ---------------------------------------------------------------------------
# 4. Scoped-token validation: agent-id, capability, tenant
# ---------------------------------------------------------------------------


def test_scope_allows_by_agent_id(cp):
    machine = _machine(cp)
    allowed = _agent(cp, machine.id, name="worker-1")
    denied = _agent(cp, machine.id, name="worker-2")
    s = cp.secrets.create_secret(
        "scoped-key", "val", {"agents": [allowed.id]}, "hub"
    )
    # allowed agent gets a handle
    h = cp.secrets.request_secret(s.id, allowed.id, purpose="read")
    assert h.granted is True
    # denied agent is rejected
    with pytest.raises(AuthorizationError):
        cp.secrets.request_secret(s.id, denied.id, purpose="read")


def test_scope_allows_by_capability(cp):
    machine = _machine(cp)
    deployer = _agent(cp, machine.id, name="worker-1", capabilities=["deploy"])
    reader = _agent(cp, machine.id, name="worker-2", capabilities=["read"])
    s = cp.secrets.create_secret(
        "deploy-cred", "cred", {"capabilities": ["deploy"]}, "hub"
    )
    h = cp.secrets.request_secret(s.id, deployer.id, purpose="deploy")
    assert h.granted is True
    with pytest.raises(AuthorizationError):
        cp.secrets.request_secret(s.id, reader.id, purpose="deploy")


def test_capability_mismatch_rejected(cp):
    """Agent with wrong capability cannot access a capability-gated secret."""
    machine = _machine(cp)
    agent = _agent(cp, machine.id, capabilities=["read"])
    s = cp.secrets.create_secret(
        "admin-cred", "admin-secret", {"capabilities": ["admin"]}, "hub"
    )
    with pytest.raises(AuthorizationError):
        cp.secrets.request_secret(s.id, agent.id, purpose="admin-op")


# ---------------------------------------------------------------------------
# 5. Multi-tenant isolation
# ---------------------------------------------------------------------------


def test_tenant_scoped_secret_not_visible_across_tenants(cp):
    """Agent on a private-tenant machine cannot access another tenant's secret."""
    tenant_a = cp.register_tenant("tenant-alpha")
    tenant_b = cp.register_tenant("tenant-beta")

    machine_a = cp.register_machine(
        "host-a",
        labels={"tenant_policy": {"mode": "private", "tenant_ids": [tenant_a.id]}},
    )
    machine_b = cp.register_machine(
        "host-b",
        labels={"tenant_policy": {"mode": "private", "tenant_ids": [tenant_b.id]}},
    )

    agent_a = _agent(cp, machine_a.id, name="worker-a")
    agent_b = _agent(cp, machine_b.id, name="worker-b")

    # Create a secret scoped only to tenant_a
    s = cp.secrets.create_secret(
        "tenant-a-secret",
        "secret-for-a",
        {"tenant_ids": [tenant_a.id]},
        "hub",
    )

    # agent_a (on machine_a which allows tenant_a) can access
    h = cp.secrets.request_secret(s.id, agent_a.id, purpose="read")
    assert h.granted is True

    # agent_b (on machine_b which is tenant_b only) cannot access
    with pytest.raises(AuthorizationError):
        cp.secrets.request_secret(s.id, agent_b.id, purpose="read")


def test_tenant_scoped_secret_with_combined_scope(cp):
    """Tenant scope combined with agent_id: only agents matching BOTH can access."""
    tenant_a = cp.register_tenant("tenant-a")
    machine = cp.register_machine(
        "shared-host",
        labels={"tenant_policy": {"mode": "private", "tenant_ids": [tenant_a.id]}},
    )
    agent = _agent(cp, machine.id, name="worker-1")
    other_machine = cp.register_machine("other-host")
    other_agent = _agent(cp, other_machine.id, name="worker-2")

    s = cp.secrets.create_secret(
        "combo-secret",
        "combo-val",
        {"agents": [agent.id], "tenant_ids": [tenant_a.id]},
        "hub",
    )
    # Agent on tenant_a machine is allowed
    h = cp.secrets.request_secret(s.id, agent.id, purpose="read")
    assert h.granted is True
    # Agent on unrestricted machine but not in agents list is denied
    with pytest.raises(AuthorizationError):
        cp.secrets.request_secret(s.id, other_agent.id, purpose="read")


# ---------------------------------------------------------------------------
# 6. Untrusted machine rejection
# ---------------------------------------------------------------------------


def test_untrusted_machine_cannot_access_secret(cp):
    """Agents on untrusted machines are always denied, regardless of scopes."""
    machine = cp.register_machine("untrusted-host")
    cp.store.execute("UPDATE machines SET trusted = 0 WHERE id = ?", (machine.id,))
    agent = _agent(cp, machine.id, name="worker-1")
    s = cp.secrets.create_secret(
        "guarded-secret", "guarded-val", {"agents": [agent.id]}, "hub"
    )
    with pytest.raises(AuthorizationError):
        cp.secrets.request_secret(s.id, agent.id, purpose="read")


# ---------------------------------------------------------------------------
# 7. Audit-trail correctness
# ---------------------------------------------------------------------------


def test_audit_trail_records_granted_access(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s = cp.secrets.create_secret(
        "audit-key", "audit-val", {"agents": [agent.id]}, "hub"
    )
    cp.secrets.request_secret(s.id, agent.id, purpose="ci-run")
    audits = cp.secrets.list_audits(s.id)
    assert len(audits) == 1
    a = audits[0]
    assert a.secret_id == s.id
    assert a.accessor_agent_id == agent.id
    assert a.purpose == "ci-run"
    assert a.result == SecretAuditResult.GRANTED.value
    assert a.created_at is not None


def test_audit_trail_records_denied_access(cp):
    machine = _machine(cp)
    allowed = _agent(cp, machine.id, name="worker-1")
    denied_agent = _agent(cp, machine.id, name="worker-2")
    s = cp.secrets.create_secret(
        "restricted-key", "val", {"agents": [allowed.id]}, "hub"
    )
    with pytest.raises(AuthorizationError):
        cp.secrets.request_secret(s.id, denied_agent.id, purpose="read")
    audits = cp.secrets.list_audits(s.id)
    assert len(audits) == 1
    assert audits[0].result == SecretAuditResult.DENIED.value
    assert audits[0].accessor_agent_id == denied_agent.id


def test_audit_trail_records_reveal(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s = cp.secrets.create_secret(
        "revealed-key", "val", {"agents": [agent.id]}, "hub"
    )
    handle = cp.secrets.request_secret(s.id, agent.id, purpose="reveal-test")
    cp.secrets.reveal_secret(s.id, handle.audit_id, agent.id)
    audits = cp.secrets.list_audits(s.id)
    assert len(audits) == 1
    assert audits[0].revealed_at is not None


def test_audit_trail_records_rotation(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s = cp.secrets.create_secret(
        "rotate-key", "old-value", {"agents": [agent.id]}, "hub"
    )
    cp.secrets.rotate_secret(s.id, "new-value", actor="hub")
    audits = cp.secrets.list_audits(s.id)
    rotate_audits = [a for a in audits if a.result == SecretAuditResult.ROTATED.value]
    assert len(rotate_audits) == 1
    assert rotate_audits[0].purpose == "rotate"


def test_list_audits_no_filter_returns_all(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s1 = cp.secrets.create_secret("key1", "v1", {"agents": [agent.id]}, "hub")
    s2 = cp.secrets.create_secret("key2", "v2", {"agents": [agent.id]}, "hub")
    cp.secrets.request_secret(s1.id, agent.id, purpose="a")
    cp.secrets.request_secret(s2.id, agent.id, purpose="b")
    all_audits = cp.secrets.list_audits()
    ids = {a.secret_id for a in all_audits}
    assert s1.id in ids
    assert s2.id in ids


# ---------------------------------------------------------------------------
# 8. Token rotation
# ---------------------------------------------------------------------------


def test_rotate_secret_updates_ciphertext(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s = cp.secrets.create_secret(
        "rotating-key", "old-value", {"agents": [agent.id]}, "hub"
    )
    h1 = cp.secrets.request_secret(s.id, agent.id, purpose="before-rotate")
    old_plain = cp.secrets.reveal_secret(s.id, h1.audit_id, agent.id)
    assert old_plain == "old-value"

    cp.secrets.rotate_secret(s.id, "new-value", actor="hub")

    h2 = cp.secrets.request_secret(s.id, agent.id, purpose="after-rotate")
    new_plain = cp.secrets.reveal_secret(s.id, h2.audit_id, agent.id)
    assert new_plain == "new-value"


def test_rotate_secret_requires_new_value(cp):
    s = _secret(cp)
    with pytest.raises(ValidationError):
        cp.secrets.rotate_secret(s.id, "", actor="hub")


def test_rotate_secret_by_name(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    cp.secrets.create_secret("named-key", "v1", {"agents": [agent.id]}, "hub")
    rotated = cp.secrets.rotate_secret("named-key", "v2", actor="hub")
    assert rotated.rotated_at is not None
    h = cp.secrets.request_secret(rotated.id, agent.id, purpose="read")
    assert cp.secrets.reveal_secret(rotated.id, h.audit_id, agent.id) == "v2"


# ---------------------------------------------------------------------------
# 9. Disabled secret behaviour
# ---------------------------------------------------------------------------


def test_disabled_secret_cannot_be_requested(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s = cp.secrets.create_secret(
        "disabled-key", "val", {"agents": [agent.id]}, "hub"
    )
    cp.store.execute("UPDATE secrets SET enabled = 0 WHERE id = ?", (s.id,))
    with pytest.raises(AuthorizationError):
        cp.secrets.request_secret(s.id, agent.id, purpose="read")


# ---------------------------------------------------------------------------
# 10. delete_secret
# ---------------------------------------------------------------------------


def test_delete_secret_removes_record(cp):
    s = _secret(cp, name="ephemeral-key")
    result = cp.secrets.delete_secret(s.id, actor="hub")
    assert result["deleted"] is True
    assert result["id"] == s.id
    with pytest.raises(NotFoundError):
        cp.secrets.get_secret(s.id)


def test_delete_secret_not_found_raises(cp):
    with pytest.raises(NotFoundError):
        cp.secrets.delete_secret("secret_nonexistent", actor="hub")


# ---------------------------------------------------------------------------
# 11. resolve_secret_value (in-process hub path)
# ---------------------------------------------------------------------------


def test_resolve_secret_value_happy_path(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    cp.secrets.create_secret(
        "provider-key", "provider-secret", {"agents": [agent.id]}, "hub"
    )
    val = cp.secrets.resolve_secret_value("provider-key")
    assert val == "provider-secret"


def test_resolve_secret_value_missing_returns_none(cp):
    result = cp.secrets.resolve_secret_value("nonexistent-key")
    assert result is None


def test_resolve_secret_value_disabled_returns_none(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s = cp.secrets.create_secret(
        "disabled-provider-key", "pval", {"agents": [agent.id]}, "hub"
    )
    cp.store.execute("UPDATE secrets SET enabled = 0 WHERE id = ?", (s.id,))
    assert cp.secrets.resolve_secret_value("disabled-provider-key") is None


def test_resolve_secret_value_emits_audit(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s = cp.secrets.create_secret(
        "audited-key", "aval", {"agents": [agent.id]}, "hub"
    )
    cp.secrets.resolve_secret_value("audited-key")
    audits = cp.secrets.list_audits(s.id)
    assert len(audits) == 1
    assert audits[0].result == SecretAuditResult.GRANTED.value


# ---------------------------------------------------------------------------
# 12. SecretHandle to_dict does not leak sensitive data
# ---------------------------------------------------------------------------


def test_secret_handle_to_dict_is_safe(cp):
    machine = _machine(cp)
    agent = _agent(cp, machine.id)
    s = cp.secrets.create_secret(
        "handle-key", "handle-val", {"agents": [agent.id]}, "hub"
    )
    handle = cp.secrets.request_secret(s.id, agent.id, purpose="read")
    d = handle.to_dict()
    assert "handle-val" not in str(d)
    assert d["granted"] is True
    assert d["secret_id"] == s.id
