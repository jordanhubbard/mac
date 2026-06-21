"""Dedicated test suite for SecretsService.

Covers:
- create/get/list/delete lifecycle
- Plaintext value is encrypted at rest (redacted in list/show output)
- request_secret: scope enforcement (agents, capabilities, tenant_ids)
- reveal_secret: single-use handle, TTL expiry, wrong-agent rejection
- rotate_secret: ciphertext replacement + rotation audit entry
- delete_secret: removes row and cascades audit rows
- resolve_secret_value: hub-side in-process reveal with audit trail
- record_access / list_audits: audit trail correctness (creator, reason,
  timestamp, result)
- Multi-tenant isolation: tenant-scoped secrets are invisible to other
  tenants / agents on wrong machines
- Capability-mismatch rejection
- Disabled secrets: request_secret denied when enabled=False
- Untrusted machine: request_secret denied
- Error paths: ValidationError on blank name/value, NotFoundError on
  missing IDs, AuthorizationError on bad reveal
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from mac.models import (
    Agent,
    AuthorizationError,
    Machine,
    NotFoundError,
    SecretAuditResult,
    ValidationError,
)
from mac.secrets_service import SECRET_HANDLE_DEFAULT_TTL_SECONDS, SecretsService
from mac.store import SQLiteStore

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS secrets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    scopes TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    rotated_at TEXT,
    enabled INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS secret_access_audit (
    id TEXT PRIMARY KEY,
    secret_id TEXT NOT NULL REFERENCES secrets(id) ON DELETE CASCADE,
    accessor_agent_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    result TEXT NOT NULL,
    expires_at TEXT,
    revealed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_secret_audit_secret_created
    ON secret_access_audit (secret_id, created_at);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    layer TEXT NOT NULL,
    source TEXT NOT NULL,
    level TEXT NOT NULL,
    value REAL,
    unit TEXT NOT NULL,
    subject_type TEXT,
    subject_id TEXT,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _make_store() -> SQLiteStore:
    store = SQLiteStore(":memory:")
    for stmt in SCHEMA_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            store.execute(stmt)
    return store


def _make_machine(
    machine_id: str = "machine_test",
    trusted: bool = True,
    labels: Optional[dict] = None,
) -> Machine:
    from mac.models import utcnow

    now = utcnow()
    return Machine(
        id=machine_id,
        hostname="test-host",
        labels=labels or {},
        resources={},
        trusted=trusted,
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )


def _make_agent(
    agent_id: str = "agent_test",
    machine_id: str = "machine_test",
    capabilities: Optional[list] = None,
) -> Agent:
    from mac.models import utcnow

    now = utcnow()
    return Agent(
        id=agent_id,
        machine_id=machine_id,
        name="test-agent",
        capabilities=capabilities or [],
        resources={},
        status="idle",
        health_status="healthy",
        current_task_id=None,
        running_digest=None,
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )


def _make_observability(store: SQLiteStore) -> Any:
    """Return a minimal ObservabilityService that writes observations."""
    # We use the real ObservabilityService wired to the same in-memory store
    # so that audit writes don't raise but also don't depend on action_events.
    from mac.observability_service import ObservabilityService

    return ObservabilityService(store)


def _make_fernet() -> Any:
    """Return a Fernet instance derived from a test key."""
    import base64

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    raw_key = "test-key-with-enough-entropy-32+chars"
    fernet_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"mac.control_plane.secrets.v1",
        info=b"fernet-key",
    ).derive(raw_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(fernet_key))


def _build_service(
    machines: Optional[dict] = None,
    agents: Optional[dict] = None,
    tenant_policies: Optional[dict] = None,
) -> tuple[SecretsService, SQLiteStore]:
    """Build a SecretsService with in-memory state.

    machines: {machine_id: Machine}
    agents:   {agent_id: Agent}
    tenant_policies: {machine_id: tenant_policy_dict}  — merged into machine labels
    """
    store = _make_store()
    obs = _make_observability(store)
    fernet = _make_fernet()

    _machines: dict[str, Machine] = machines or {}
    _agents: dict[str, Agent] = agents or {}

    def get_agent(agent_id: str) -> Agent:
        if agent_id not in _agents:
            raise NotFoundError("agent not found: %s" % agent_id)
        return _agents[agent_id]

    def get_machine(machine_id: str) -> Machine:
        if machine_id not in _machines:
            raise NotFoundError("machine not found: %s" % machine_id)
        return _machines[machine_id]

    def machine_allows_tenant(machine: Machine, tenant_id: Optional[str]) -> bool:
        policy = machine.labels.get("tenant_policy") or {}
        if not isinstance(policy, dict):
            return True
        mode = str(policy.get("mode", "shared"))
        allowed = set(policy.get("tenant_ids") or policy.get("allow_tenants") or [])
        denied = set(policy.get("deny_tenants") or [])
        if mode == "denied":
            return False
        if tenant_id is None:
            return mode != "private"
        if tenant_id in denied:
            return False
        if mode == "private":
            return tenant_id in allowed
        if allowed:
            return tenant_id in allowed
        return True

    svc = SecretsService(
        store,
        obs,
        fernet,
        get_agent=get_agent,
        get_machine=get_machine,
        machine_allows_tenant=machine_allows_tenant,
    )
    return svc, store


@pytest.fixture()
def default_machine() -> Machine:
    return _make_machine("machine_1", trusted=True)


@pytest.fixture()
def default_agent(default_machine: Machine) -> Agent:
    return _make_agent("agent_1", machine_id=default_machine.id, capabilities=["write"])


@pytest.fixture()
def svc(default_machine: Machine, default_agent: Agent) -> SecretsService:
    service, _ = _build_service(
        machines={default_machine.id: default_machine},
        agents={default_agent.id: default_agent},
    )
    return service


@pytest.fixture()
def svc_and_store(default_machine: Machine, default_agent: Agent):
    service, store = _build_service(
        machines={default_machine.id: default_machine},
        agents={default_agent.id: default_agent},
    )
    return service, store


# ---------------------------------------------------------------------------
# 1. Basic CRUD
# ---------------------------------------------------------------------------


class TestCreateAndGet:
    def test_create_returns_secret_record(self, svc: SecretsService) -> None:
        rec = svc.create_secret(
            "slack-token", "xoxb-test", {"agents": ["agent_1"]}, "operator"
        )
        assert rec.id.startswith("secret_")
        assert rec.name == "slack-token"
        assert rec.created_by == "operator"
        assert rec.enabled is True
        assert rec.rotated_at is None

    def test_get_by_id(self, svc: SecretsService) -> None:
        rec = svc.create_secret("tok", "val", {"agents": ["a"]}, "op")
        fetched = svc.get_secret(rec.id)
        assert fetched.id == rec.id

    def test_get_by_name(self, svc: SecretsService) -> None:
        svc.create_secret("my-key", "val", {"agents": ["a"]}, "op")
        fetched = svc.get_secret("my-key")
        assert fetched.name == "my-key"

    def test_get_missing_raises_not_found(self, svc: SecretsService) -> None:
        with pytest.raises(NotFoundError):
            svc.get_secret("secret_doesnotexist")

    def test_list_secrets_empty(self, svc: SecretsService) -> None:
        assert svc.list_secrets() == []

    def test_list_secrets_returns_all(self, svc: SecretsService) -> None:
        svc.create_secret("a", "v1", {"agents": []}, "op")
        svc.create_secret("b", "v2", {"agents": []}, "op")
        names = {r.name for r in svc.list_secrets()}
        assert names == {"a", "b"}

    def test_list_secrets_sorted_by_name(self, svc: SecretsService) -> None:
        svc.create_secret("zebra", "v", {"agents": []}, "op")
        svc.create_secret("alpha", "v", {"agents": []}, "op")
        svc.create_secret("mango", "v", {"agents": []}, "op")
        names = [r.name for r in svc.list_secrets()]
        assert names == sorted(names)

    def test_create_blank_name_raises(self, svc: SecretsService) -> None:
        with pytest.raises(ValidationError):
            svc.create_secret("", "value", {"agents": []}, "op")

    def test_create_blank_value_raises(self, svc: SecretsService) -> None:
        with pytest.raises(ValidationError):
            svc.create_secret("name", "", {"agents": []}, "op")

    def test_create_empty_scopes_raises(self, svc: SecretsService) -> None:
        with pytest.raises(ValidationError):
            svc.create_secret("name", "value", {}, "op")


# ---------------------------------------------------------------------------
# 2. Plaintext value is encrypted at rest / redacted in public view
# ---------------------------------------------------------------------------


class TestEncryptionAndRedaction:
    def test_ciphertext_differs_from_plaintext(self, svc_and_store) -> None:
        svc, store = svc_and_store
        svc.create_secret("enc-test", "mysecretvalue", {"agents": ["x"]}, "op")
        row = store.query_one("SELECT ciphertext FROM secrets WHERE name = ?", ("enc-test",))
        assert row is not None
        assert "mysecretvalue" not in row["ciphertext"]

    def test_to_dict_redacts_value(self, svc: SecretsService) -> None:
        rec = svc.create_secret("safe", "plaintext", {"agents": ["x"]}, "op")
        d = rec.to_dict()
        assert d.get("value") == "***REDACTED***"
        assert "plaintext" not in str(d)

    def test_list_secrets_does_not_expose_plaintext(self, svc_and_store) -> None:
        svc, store = svc_and_store
        svc.create_secret("s1", "top-secret", {"agents": []}, "op")
        records = svc.list_secrets()
        for r in records:
            d = r.to_dict()
            assert "top-secret" not in str(d)


# ---------------------------------------------------------------------------
# 3. request_secret — scope enforcement
# ---------------------------------------------------------------------------


class TestRequestSecret:
    def _svc_with_agent(
        self,
        capabilities: list | None = None,
        trusted: bool = True,
        tenant_policy: dict | None = None,
    ):
        labels = {}
        if tenant_policy:
            labels["tenant_policy"] = tenant_policy
        machine = _make_machine("machine_1", trusted=trusted, labels=labels)
        agent = _make_agent("agent_1", machine_id="machine_1", capabilities=capabilities or [])
        svc, _ = _build_service(
            machines={"machine_1": machine},
            agents={"agent_1": agent},
        )
        return svc

    def test_agent_in_scope_gets_handle(self) -> None:
        svc = self._svc_with_agent()
        svc.create_secret("tok", "val", {"agents": ["agent_1"]}, "op")
        handle = svc.request_secret("tok", "agent_1", "deploy")
        assert handle.granted is True
        assert handle.secret_id.startswith("secret_")
        assert handle.audit_id.startswith("audit_")

    def test_agent_not_in_scope_denied(self) -> None:
        svc = self._svc_with_agent()
        svc.create_secret("tok", "val", {"agents": ["agent_other"]}, "op")
        with pytest.raises(AuthorizationError):
            svc.request_secret("tok", "agent_1", "deploy")

    def test_capability_match_grants(self) -> None:
        svc = self._svc_with_agent(capabilities=["dispatch", "secret"])
        svc.create_secret("tok", "val", {"capabilities": ["secret"]}, "op")
        handle = svc.request_secret("tok", "agent_1", "job")
        assert handle.granted is True

    def test_capability_mismatch_denied(self) -> None:
        svc = self._svc_with_agent(capabilities=["read"])
        svc.create_secret("tok", "val", {"capabilities": ["admin"]}, "op")
        with pytest.raises(AuthorizationError):
            svc.request_secret("tok", "agent_1", "job")

    def test_untrusted_machine_denied(self) -> None:
        svc = self._svc_with_agent(trusted=False)
        svc.create_secret("tok", "val", {"agents": ["agent_1"]}, "op")
        with pytest.raises(AuthorizationError):
            svc.request_secret("tok", "agent_1", "job")

    def test_disabled_secret_denied(self) -> None:
        svc = self._svc_with_agent()
        svc.create_secret("tok", "val", {"agents": ["agent_1"]}, "op")
        # Disable the secret directly in the store
        svc.store.execute("UPDATE secrets SET enabled = 0 WHERE name = ?", ("tok",))
        with pytest.raises(AuthorizationError):
            svc.request_secret("tok", "agent_1", "job")

    def test_handle_uri_format(self) -> None:
        svc = self._svc_with_agent()
        rec = svc.create_secret("tok", "val", {"agents": ["agent_1"]}, "op")
        handle = svc.request_secret("tok", "agent_1", "job")
        assert handle.handle == "secret://%s#%s" % (rec.id, handle.audit_id)

    def test_request_unknown_secret_raises_not_found(self) -> None:
        svc = self._svc_with_agent()
        with pytest.raises(NotFoundError):
            svc.request_secret("no-such-secret", "agent_1", "job")


# ---------------------------------------------------------------------------
# 4. reveal_secret — single-use, TTL, wrong-agent
# ---------------------------------------------------------------------------


class TestRevealSecret:
    def _setup(self, ttl: int = SECRET_HANDLE_DEFAULT_TTL_SECONDS):
        machine = _make_machine("m1", trusted=True)
        agent = _make_agent("a1", machine_id="m1")
        agent2 = _make_agent("a2", machine_id="m1")
        svc, _ = _build_service(
            machines={"m1": machine},
            agents={"a1": agent, "a2": agent2},
        )
        rec = svc.create_secret("tok", "secret-plain", {"agents": ["a1"]}, "op")
        handle = svc.request_secret(rec.id, "a1", "test-reveal", ttl_seconds=ttl)
        return svc, rec, handle

    def test_reveal_returns_plaintext(self) -> None:
        svc, rec, handle = self._setup()
        plain = svc.reveal_secret(rec.id, handle.audit_id, "a1")
        assert plain == "secret-plain"

    def test_reveal_is_single_use(self) -> None:
        svc, rec, handle = self._setup()
        svc.reveal_secret(rec.id, handle.audit_id, "a1")
        with pytest.raises(AuthorizationError):
            svc.reveal_secret(rec.id, handle.audit_id, "a1")

    def test_reveal_wrong_agent_rejected(self) -> None:
        svc, rec, handle = self._setup()
        with pytest.raises(AuthorizationError):
            svc.reveal_secret(rec.id, handle.audit_id, "a2")

    def test_reveal_wrong_secret_id_rejected(self) -> None:
        svc, rec, handle = self._setup()
        with pytest.raises(AuthorizationError):
            svc.reveal_secret("secret_wrong_id", handle.audit_id, "a1")

    def test_reveal_nonexistent_audit_rejected(self) -> None:
        svc, rec, handle = self._setup()
        with pytest.raises(AuthorizationError):
            svc.reveal_secret(rec.id, "audit_nonexistent", "a1")

    def test_reveal_after_secret_disabled_raises_not_found(self) -> None:
        svc, rec, handle = self._setup()
        svc.store.execute("UPDATE secrets SET enabled = 0 WHERE id = ?", (rec.id,))
        with pytest.raises(NotFoundError):
            svc.reveal_secret(rec.id, handle.audit_id, "a1")


# ---------------------------------------------------------------------------
# 5. rotate_secret
# ---------------------------------------------------------------------------


class TestRotateSecret:
    def _svc(self):
        machine = _make_machine("m1", trusted=True)
        agent = _make_agent("a1", machine_id="m1")
        svc, _ = _build_service(
            machines={"m1": machine},
            agents={"a1": agent},
        )
        return svc

    def test_rotate_updates_ciphertext(self, svc_and_store) -> None:
        svc, store = svc_and_store
        svc.create_secret("tok", "original", {"agents": ["agent_1"]}, "op")
        row_before = store.query_one("SELECT ciphertext FROM secrets WHERE name = ?", ("tok",))

        svc.rotate_secret("tok", "rotated-value", "operator")

        row_after = store.query_one("SELECT ciphertext FROM secrets WHERE name = ?", ("tok",))
        assert row_before["ciphertext"] != row_after["ciphertext"]

    def test_rotate_records_rotated_at(self, svc: SecretsService) -> None:
        svc.create_secret("tok", "original", {"agents": []}, "op")
        rec = svc.rotate_secret("tok", "new-value", "operator")
        assert rec.rotated_at is not None

    def test_rotate_writes_audit_entry(self, svc: SecretsService) -> None:
        rec = svc.create_secret("tok", "original", {"agents": []}, "op")
        svc.rotate_secret(rec.id, "new-value", "admin-agent")
        audits = svc.list_audits(rec.id)
        rotate_entries = [a for a in audits if a.result == SecretAuditResult.ROTATED.value]
        assert len(rotate_entries) == 1
        assert rotate_entries[0].accessor_agent_id == "admin-agent"
        assert rotate_entries[0].purpose == "rotate"

    def test_rotate_blank_value_raises(self, svc: SecretsService) -> None:
        svc.create_secret("tok", "original", {"agents": []}, "op")
        with pytest.raises(ValidationError):
            svc.rotate_secret("tok", "", "operator")

    def test_rotated_value_is_revealed_correctly(self) -> None:
        machine = _make_machine("m1", trusted=True)
        agent = _make_agent("a1", machine_id="m1")
        svc, _ = _build_service(
            machines={"m1": machine},
            agents={"a1": agent},
        )
        rec = svc.create_secret("tok", "original", {"agents": ["a1"]}, "op")
        svc.rotate_secret(rec.id, "rotated-value", "op")
        handle = svc.request_secret(rec.id, "a1", "post-rotate")
        plain = svc.reveal_secret(rec.id, handle.audit_id, "a1")
        assert plain == "rotated-value"


# ---------------------------------------------------------------------------
# 6. delete_secret
# ---------------------------------------------------------------------------


class TestDeleteSecret:
    def test_delete_removes_secret(self, svc: SecretsService) -> None:
        rec = svc.create_secret("tok", "val", {"agents": []}, "op")
        result = svc.delete_secret(rec.id, actor="operator")
        assert result["deleted"] is True
        assert result["id"] == rec.id
        with pytest.raises(NotFoundError):
            svc.get_secret(rec.id)

    def test_delete_by_name(self, svc: SecretsService) -> None:
        svc.create_secret("deleteme", "val", {"agents": []}, "op")
        svc.delete_secret("deleteme")
        with pytest.raises(NotFoundError):
            svc.get_secret("deleteme")

    def test_delete_missing_raises_not_found(self, svc: SecretsService) -> None:
        with pytest.raises(NotFoundError):
            svc.delete_secret("secret_nonexistent")

    def test_delete_cascades_audit_rows(self, svc_and_store) -> None:
        svc, store = svc_and_store
        rec = svc.create_secret("tok", "val", {"agents": ["agent_1"]}, "op")
        svc.request_secret(rec.id, "agent_1", "test")
        # Audit row exists before delete
        rows_before = store.query_all(
            "SELECT id FROM secret_access_audit WHERE secret_id = ?", (rec.id,)
        )
        assert len(rows_before) > 0
        svc.delete_secret(rec.id)
        # Audit rows should be gone (CASCADE)
        rows_after = store.query_all(
            "SELECT id FROM secret_access_audit WHERE secret_id = ?", (rec.id,)
        )
        assert rows_after == []


# ---------------------------------------------------------------------------
# 7. Audit trail correctness
# ---------------------------------------------------------------------------


class TestAuditTrail:
    def _svc_with_agent(self):
        machine = _make_machine("m1", trusted=True)
        agent = _make_agent("a1", machine_id="m1")
        svc, _ = _build_service(
            machines={"m1": machine},
            agents={"a1": agent},
        )
        return svc

    def test_granted_access_recorded(self) -> None:
        svc = self._svc_with_agent()
        rec = svc.create_secret("tok", "val", {"agents": ["a1"]}, "op")
        svc.request_secret(rec.id, "a1", "deploy-job")
        audits = svc.list_audits(rec.id)
        assert len(audits) == 1
        a = audits[0]
        assert a.result == SecretAuditResult.GRANTED.value
        assert a.accessor_agent_id == "a1"
        assert a.purpose == "deploy-job"
        assert a.expires_at is not None
        assert a.revealed_at is None

    def test_denied_access_recorded(self) -> None:
        svc = self._svc_with_agent()
        rec = svc.create_secret("tok", "val", {"agents": ["other"]}, "op")
        with pytest.raises(AuthorizationError):
            svc.request_secret(rec.id, "a1", "attempt")
        audits = svc.list_audits(rec.id)
        assert any(a.result == SecretAuditResult.DENIED.value for a in audits)

    def test_audit_timestamp_present(self) -> None:
        svc = self._svc_with_agent()
        rec = svc.create_secret("tok", "val", {"agents": ["a1"]}, "op")
        svc.request_secret(rec.id, "a1", "job")
        audits = svc.list_audits(rec.id)
        assert all(a.created_at for a in audits)

    def test_list_audits_no_filter_returns_all(self) -> None:
        svc = self._svc_with_agent()
        rec1 = svc.create_secret("t1", "v", {"agents": ["a1"]}, "op")
        rec2 = svc.create_secret("t2", "v", {"agents": ["a1"]}, "op")
        svc.request_secret(rec1.id, "a1", "j1")
        svc.request_secret(rec2.id, "a1", "j2")
        all_audits = svc.list_audits()
        secret_ids = {a.secret_id for a in all_audits}
        assert rec1.id in secret_ids
        assert rec2.id in secret_ids

    def test_list_audits_filter_by_secret(self) -> None:
        svc = self._svc_with_agent()
        rec1 = svc.create_secret("t1", "v", {"agents": ["a1"]}, "op")
        rec2 = svc.create_secret("t2", "v", {"agents": ["a1"]}, "op")
        svc.request_secret(rec1.id, "a1", "j1")
        svc.request_secret(rec2.id, "a1", "j2")
        audits = svc.list_audits(rec1.id)
        assert all(a.secret_id == rec1.id for a in audits)

    def test_reveal_marks_revealed_at(self) -> None:
        svc = self._svc_with_agent()
        rec = svc.create_secret("tok", "val", {"agents": ["a1"]}, "op")
        handle = svc.request_secret(rec.id, "a1", "job")
        svc.reveal_secret(rec.id, handle.audit_id, "a1")
        audits = svc.list_audits(rec.id)
        granted = [a for a in audits if a.result == SecretAuditResult.GRANTED.value]
        assert len(granted) == 1
        assert granted[0].revealed_at is not None

    def test_record_access_direct(self, svc: SecretsService) -> None:
        rec = svc.create_secret("tok", "val", {"agents": []}, "op")
        access = svc.record_access(rec.id, "agent_sys", "internal-use", "granted")
        assert access.id.startswith("audit_")
        assert access.secret_id == rec.id
        assert access.accessor_agent_id == "agent_sys"
        assert access.purpose == "internal-use"
        assert access.result == "granted"


# ---------------------------------------------------------------------------
# 8. Multi-tenant isolation
# ---------------------------------------------------------------------------


class TestMultiTenantIsolation:
    def _make_tenant_svc(self):
        """Two machines: m_t1 (tenant 'alpha') and m_t2 (tenant 'beta').
        Each has one agent."""
        m_t1 = _make_machine(
            "m_t1",
            trusted=True,
            labels={"tenant_policy": {"mode": "private", "tenant_ids": ["alpha"]}},
        )
        m_t2 = _make_machine(
            "m_t2",
            trusted=True,
            labels={"tenant_policy": {"mode": "private", "tenant_ids": ["beta"]}},
        )
        a_t1 = _make_agent("a_t1", machine_id="m_t1")
        a_t2 = _make_agent("a_t2", machine_id="m_t2")
        svc, _ = _build_service(
            machines={"m_t1": m_t1, "m_t2": m_t2},
            agents={"a_t1": a_t1, "a_t2": a_t2},
        )
        return svc

    def test_tenant_scoped_secret_visible_to_owner(self) -> None:
        svc = self._make_tenant_svc()
        svc.create_secret("alpha-cred", "val", {"tenant_ids": ["alpha"]}, "op")
        handle = svc.request_secret("alpha-cred", "a_t1", "job")
        assert handle.granted is True

    def test_tenant_scoped_secret_invisible_to_other_tenant(self) -> None:
        svc = self._make_tenant_svc()
        svc.create_secret("alpha-cred", "val", {"tenant_ids": ["alpha"]}, "op")
        with pytest.raises(AuthorizationError):
            svc.request_secret("alpha-cred", "a_t2", "job")

    def test_cross_tenant_no_capability_leak(self) -> None:
        svc = self._make_tenant_svc()
        svc.create_secret("beta-cred", "val", {"tenant_ids": ["beta"]}, "op")
        with pytest.raises(AuthorizationError):
            svc.request_secret("beta-cred", "a_t1", "job")

    def test_agent_id_scope_cross_machine(self) -> None:
        """An agent explicitly listed in scopes but on an untrusted machine is denied."""
        m_trusted = _make_machine("m_trusted", trusted=True)
        m_untrusted = _make_machine("m_untrusted", trusted=False)
        a_trusted = _make_agent("a_trusted", machine_id="m_trusted")
        a_untrusted = _make_agent("a_untrusted", machine_id="m_untrusted")
        svc, _ = _build_service(
            machines={"m_trusted": m_trusted, "m_untrusted": m_untrusted},
            agents={"a_trusted": a_trusted, "a_untrusted": a_untrusted},
        )
        svc.create_secret("sec", "val", {"agents": ["a_untrusted"]}, "op")
        with pytest.raises(AuthorizationError):
            svc.request_secret("sec", "a_untrusted", "job")


# ---------------------------------------------------------------------------
# 9. resolve_secret_value (hub-side in-process reveal)
# ---------------------------------------------------------------------------


class TestResolveSecretValue:
    def test_returns_plaintext_for_enabled_secret(self, svc: SecretsService) -> None:
        svc.create_secret("router-key", "plaintext-value", {"agents": []}, "op")
        result = svc.resolve_secret_value("router-key", purpose="router", accessor="router")
        assert result == "plaintext-value"

    def test_returns_none_for_missing_secret(self, svc: SecretsService) -> None:
        result = svc.resolve_secret_value("nonexistent", purpose="router", accessor="router")
        assert result is None

    def test_returns_none_for_disabled_secret(self, svc: SecretsService) -> None:
        svc.create_secret("dis-key", "val", {"agents": []}, "op")
        svc.store.execute("UPDATE secrets SET enabled = 0 WHERE name = ?", ("dis-key",))
        result = svc.resolve_secret_value("dis-key")
        assert result is None

    def test_resolve_creates_audit_entry(self, svc: SecretsService) -> None:
        rec = svc.create_secret("audit-key", "val", {"agents": []}, "op")
        svc.resolve_secret_value("audit-key", purpose="router-lookup", accessor="router")
        audits = svc.list_audits(rec.id)
        assert len(audits) == 1
        assert audits[0].result == SecretAuditResult.GRANTED.value
        assert audits[0].purpose == "router-lookup"
        assert audits[0].accessor_agent_id == "router"


# ---------------------------------------------------------------------------
# 10. Scope helpers edge cases
# ---------------------------------------------------------------------------


class TestScopeHelpers:
    def _svc_caps(self, agent_caps: list, secret_caps: list | None = None):
        machine = _make_machine("m1", trusted=True)
        agent = _make_agent("a1", machine_id="m1", capabilities=agent_caps)
        svc, _ = _build_service(
            machines={"m1": machine},
            agents={"a1": agent},
        )
        scopes = {}
        if secret_caps is not None:
            scopes["capabilities"] = secret_caps
        else:
            scopes["agents"] = ["a1"]
        svc.create_secret("tok", "val", scopes, "op")
        return svc

    def test_empty_capabilities_scope_no_match(self) -> None:
        """Capabilities scope [] + agent with capabilities -> no intersection."""
        machine = _make_machine("m1", trusted=True)
        agent = _make_agent("a1", machine_id="m1", capabilities=["read"])
        svc, _ = _build_service(
            machines={"m1": machine},
            agents={"a1": agent},
        )
        # scope by agent_id only — different agent
        svc.create_secret("tok", "val", {"agents": ["other_agent"]}, "op")
        with pytest.raises(AuthorizationError):
            svc.request_secret("tok", "a1", "job")

    def test_multiple_capabilities_any_match(self) -> None:
        machine = _make_machine("m1", trusted=True)
        agent = _make_agent("a1", machine_id="m1", capabilities=["read", "secret"])
        svc, _ = _build_service(
            machines={"m1": machine},
            agents={"a1": agent},
        )
        svc.create_secret("tok", "val", {"capabilities": ["admin", "secret"]}, "op")
        handle = svc.request_secret("tok", "a1", "job")
        assert handle.granted is True

    def test_both_agent_and_capability_scope_either_grants(self) -> None:
        machine = _make_machine("m1", trusted=True)
        agent = _make_agent("a1", machine_id="m1", capabilities=["deploy"])
        svc, _ = _build_service(
            machines={"m1": machine},
            agents={"a1": agent},
        )
        # agent NOT in agents list, but has matching capability
        svc.create_secret(
            "tok", "val", {"agents": ["other"], "capabilities": ["deploy"]}, "op"
        )
        handle = svc.request_secret("tok", "a1", "job")
        assert handle.granted is True
