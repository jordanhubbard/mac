from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.client_principals import (
    ClientPrincipalError,
    ClientPrincipalProvider,
    ClientPrincipalStore,
    enrollment_manifest,
)
from mac.services import ControlPlane


def test_enrollment_stores_only_hash_and_audits_without_token(tmp_path):
    registry = tmp_path / "hub" / "client-principals.json"
    store = ClientPrincipalStore(registry)

    issued = store.enroll("laptop", fleet="rocky")
    manifest = enrollment_manifest(issued)

    assert manifest["credential"]["token"] == issued.token
    assert "MAC_SECRET_KEY" not in json.dumps(manifest)
    assert "provider" not in json.dumps(manifest).lower()
    raw_registry = registry.read_text(encoding="utf-8")
    raw_audit = store.audit_path.read_text(encoding="utf-8")
    assert issued.token not in raw_registry
    assert issued.token not in raw_audit
    assert "sha256:" in raw_registry
    if registry.stat().st_mode & 0o777:
        assert registry.stat().st_mode & 0o777 == 0o600
        assert registry.parent.stat().st_mode & 0o777 == 0o700


def test_two_clients_are_distinct_and_revocation_is_independent(tmp_path):
    store = ClientPrincipalStore(tmp_path / "principals.json")
    first = store.enroll("first")
    second = store.enroll("second")
    provider = ClientPrincipalProvider(store.path)

    assert first.token != second.token
    assert len(provider.tokens()) == 2

    store.revoke("first")

    values = provider.tokens()
    assert len(values) == 1
    assert second.record["token_hash"] in values
    assert first.record["token_hash"] not in values


def test_renew_rotates_immediately_and_duplicate_enroll_is_defined(tmp_path):
    store = ClientPrincipalStore(tmp_path / "principals.json")
    first = store.enroll("laptop")

    with pytest.raises(ClientPrincipalError, match="already exists"):
        store.enroll("laptop")

    renewed = store.renew("laptop")
    values = ClientPrincipalProvider(store.path).tokens()

    assert renewed.token != first.token
    assert first.record["token_hash"] not in values
    assert renewed.record["token_hash"] in values
    assert renewed.record["credential_version"] == 2


def test_elevated_scopes_require_explicit_acknowledgement(tmp_path):
    store = ClientPrincipalStore(tmp_path / "principals.json")

    with pytest.raises(ClientPrincipalError, match="allow-elevated"):
        store.enroll("admin-client", scopes=["read", "secret"])

    issued = store.enroll(
        "admin-client",
        scopes=["read", "secret"],
        allow_elevated=True,
    )
    assert issued.record["scopes"] == ["read", "secret"]


def test_api_hot_reloads_issue_and_revoke_without_restart(tmp_path):
    registry = tmp_path / "principals.json"
    store = ClientPrincipalStore(registry)
    first = store.enroll("first")
    second = store.enroll("second")
    app = create_app(
        control_plane=ControlPlane.in_memory(),
        auth_tokens={"recovery-admin": ["admin"]},
        client_principals_path=str(registry),
    )
    client = TestClient(app)

    assert (
        client.get("/agents", headers={"Authorization": "Bearer %s" % first.token}).status_code
        == 200
    )
    assert (
        client.post(
            "/secrets",
            json={"name": "nope", "value": "nope"},
            headers={"Authorization": "Bearer %s" % first.token},
        ).status_code
        == 403
    )

    store.revoke("first")

    assert (
        client.get("/agents", headers={"Authorization": "Bearer %s" % first.token}).status_code
        == 403
    )
    assert (
        client.get("/agents", headers={"Authorization": "Bearer %s" % second.token}).status_code
        == 200
    )


def test_registry_with_broad_permissions_fails_closed(tmp_path):
    store = ClientPrincipalStore(tmp_path / "principals.json")
    issued = store.enroll("laptop")
    store.path.chmod(0o644)

    assert ClientPrincipalProvider(store.path).tokens() == {}
    assert issued.token not in json.dumps(ClientPrincipalProvider(store.path).tokens())
