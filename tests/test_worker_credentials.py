"""Per-agent worker bearer tokens: lifecycle, principal resolution and HTTP auth."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from mac.api import TokenPrincipal, create_app
from mac.models import AuthorizationError
from mac.services import ControlPlane
from mac.test_support import ephemeral_dsn, store_on
from mac.worker_credentials import (
    WORKER_SCOPES,
    WorkerCredentialError,
    WorkerCredentialLifecycle,
    WorkerCredentialPrincipalProvider,
    _token_hash,
    _utcnow,
    evaluate_worker_actor,
)


def _plane() -> ControlPlane:
    cp = ControlPlane(
        store_on(ephemeral_dsn(), initialize=True),
        secret_key="worker-credential-test-key-with-32-bytes",
    )
    machine = cp.register_machine("worker-host", machine_id="machine_worker", labels={})
    for name in ("alpha", "beta"):
        cp.register_agent(machine.id, name, ["python"], resources={}, agent_id="agent_" + name)
    return cp


def _active(lifecycle: WorkerCredentialLifecycle, agent_id: str = "agent_alpha", **kwargs):
    issued = lifecycle.issue(agent_id, **kwargs)
    lifecycle.activate(agent_id, issued.record["id"])
    return issued


def test_issue_stores_only_the_hash_and_the_pending_token_already_authenticates() -> None:
    cp = _plane()
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issued = lifecycle.issue("agent_alpha", actor="tester")

    row = cp.store.query_one(
        "SELECT * FROM worker_credentials WHERE id = ?", (issued.record["id"],)
    )
    assert row["token_hash"] == _token_hash(issued.token)
    assert issued.token not in json.dumps(dict(row), default=str)
    assert row["state"] == "pending_install" and row["created_by"] == "tester"
    assert all("token_hash" not in item for item in lifecycle.list())

    projected = WorkerCredentialPrincipalProvider(cp.store).tokens()
    assert projected[row["token_hash"]] == {
        "scopes": list(WORKER_SCOPES),
        "client_id": issued.record["id"],
        "agent_id": "agent_alpha",
        "principal_kind": "worker",
        "credential_fingerprint": issued.record["token_fingerprint"],
        "worker_credential_version": 1,
        "worker_credential_state": "pending_install",
    }
    with pytest.raises(WorkerCredentialError, match="at least 60 seconds"):
        lifecycle.issue("agent_alpha", expires_in=59)
    with pytest.raises(WorkerCredentialError, match="registered agent"):
        lifecycle.issue("agent_missing")


def test_activation_supersedes_every_other_live_version() -> None:
    cp = _plane()
    lifecycle = WorkerCredentialLifecycle(cp.store)
    first = _active(lifecycle)
    pending = lifecycle.issue("agent_alpha")
    third = lifecycle.issue("agent_alpha")
    other_agent = _active(lifecycle, "agent_beta")

    lifecycle.activate("agent_alpha", third.record["id"])

    states = {item["id"]: item for item in lifecycle.list(agent_id="agent_alpha")}
    assert states[third.record["id"]]["state"] == "active"
    for old in (first, pending):
        assert states[old.record["id"]]["state"] == "superseded"
        assert states[old.record["id"]]["superseded_by"] == third.record["id"]
    projected = WorkerCredentialPrincipalProvider(cp.store).tokens()
    assert set(projected) == {_token_hash(third.token), _token_hash(other_agent.token)}
    with pytest.raises(WorkerCredentialError, match="no longer an unexpired pending"):
        lifecycle.activate("agent_alpha", first.record["id"])


def test_revoking_a_failed_install_keeps_the_previous_token() -> None:
    cp = _plane()
    lifecycle = WorkerCredentialLifecycle(cp.store)
    current = _active(lifecycle)
    failed = lifecycle.issue("agent_alpha")

    revoked = lifecycle.revoke("agent_alpha", failed.record["id"])

    assert revoked["state"] == "revoked"
    projected = WorkerCredentialPrincipalProvider(cp.store).tokens()
    assert set(projected) == {_token_hash(current.token)}
    with pytest.raises(WorkerCredentialError, match="no longer an unexpired pending"):
        lifecycle.revoke("agent_alpha", current.record["id"])
    with pytest.raises(WorkerCredentialError, match="does not exist"):
        lifecycle.revoke("agent_beta", current.record["id"])


def test_expired_and_deleted_agent_tokens_do_not_resolve() -> None:
    cp = _plane()
    lifecycle = WorkerCredentialLifecycle(cp.store)
    alpha = _active(lifecycle, expires_in=120)
    beta = _active(lifecycle, "agent_beta")
    provider = WorkerCredentialPrincipalProvider(cp.store)

    assert _token_hash(alpha.token) not in provider.tokens(now=_utcnow() + timedelta(seconds=300))
    cp.delete_agent("agent_beta", actor="operator")
    assert _token_hash(beta.token) not in provider.tokens()
    assert lifecycle.list(agent_id="agent_beta")[0]["state"] == "revoked"


def test_actor_binding_accepts_only_the_bound_agent_or_an_unbound_principal() -> None:
    assert evaluate_worker_actor(principal_agent_id="a", claimed_agent_id="a").allowed
    assert evaluate_worker_actor(principal_agent_id=None, claimed_agent_id="a").allowed
    mismatch = evaluate_worker_actor(principal_agent_id="a", claimed_agent_id="b")
    assert not mismatch.allowed and mismatch.reason == "agent_principal_mismatch"

    TokenPrincipal(scopes=frozenset({"write"}), agent_id="a").assert_actor("a")
    with pytest.raises(AuthorizationError, match="bound to agent a"):
        TokenPrincipal(scopes=frozenset({"write"}), agent_id="a").assert_actor("b")


def test_http_auth_accepts_live_worker_tokens_and_rejects_the_rest() -> None:
    cp = _plane()
    lifecycle = WorkerCredentialLifecycle(cp.store)
    superseded = _active(lifecycle)
    current = _active(lifecycle)
    failed = lifecycle.issue("agent_alpha")
    lifecycle.revoke("agent_alpha", failed.record["id"])
    beta = _active(lifecycle, "agent_beta", expires_in=60)
    cp.store.execute(
        "UPDATE worker_credentials SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", beta.record["id"]),
    )
    task = cp.create_task("worker token claim")
    app = create_app(control_plane=cp, auth_tokens={"static-admin": {"scopes": ["admin"]}})

    def heartbeat(token: str, agent_id: str = "agent_alpha"):
        return client.post(
            "/agents/%s/heartbeat" % agent_id,
            headers={"Authorization": "Bearer " + token} if token else {},
            json={"status": "idle"},
        )

    with TestClient(app) as client:
        missing = heartbeat("")
        assert missing.status_code == 403
        assert missing.json()["detail"] == "missing bearer token"
        for rejected in (superseded, failed, beta):
            response = heartbeat(rejected.token, rejected.record["agent_id"])
            assert response.status_code == 403
            assert response.json()["detail"] == "unknown bearer token"
        assert heartbeat("mac_worker_forged").status_code == 403
        peer = heartbeat(current.token, "agent_beta")
        assert peer.status_code == 403
        assert "bound to agent agent_alpha" in peer.json()["detail"]

        assert heartbeat(current.token).status_code == 200
        claimed = client.post(
            "/tasks/%s/claim" % task.id,
            headers={"Authorization": "Bearer " + current.token},
            params={"agent_id": "agent_alpha"},
        )
        assert claimed.status_code == 200
        assert claimed.json()["task"]["owner_agent_id"] == "agent_alpha"
        # A static MAC_API_TOKENS entry still authenticates alongside them.
        assert heartbeat("static-admin", "agent_beta").status_code == 200
