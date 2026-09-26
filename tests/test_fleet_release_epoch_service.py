from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.deploy_env import read_env_file
from mac.fleet_release_epoch_service import (
    ATTESTATION_PROOF_PURPOSE,
    ATTESTATION_PROOF_SCHEMA,
)
from mac.models import (
    REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY,
    REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY,
    REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY,
    TransitionError,
    ValidationError,
    agent_has_read_only_report_repository_executor,
    read_only_report_repository_executor_approval,
    read_only_report_repository_executor_attestation,
    read_only_report_repository_executor_resource,
    parse_time,
    utcnow,
)
from mac.services import ControlPlane, sign_verification_manifest
from mac.test_support import ephemeral_store, store_on
from mac.worker_credentials import (
    MODE_COMPATIBILITY,
    MODE_ENFORCED,
    PACKAGE_CAPABILITY,
    WorkerCredentialError,
    WorkerCredentialLifecycle,
    WorkerCredentialPrincipalProvider,
    authenticated_credential_resource,
    credential_resource_from_env,
    install_vm_manifest,
    installation_manifest,
    read_policy_state,
    write_policy_state,
)


SECRET_KEY = "fleet-release-epoch-test-secret-with-32-bytes"
SOURCE_SHA = "a" * 40
RUNTIME_SHA = "sha256:runtime-a"
FUTURE_SEEN = "2100-01-01T00:00:00+00:00"
APPLIED_SEEN = "2100-01-02T00:00:00+00:00"


def _plane(path: Path, names: tuple[str, ...] = ("alpha",)) -> ControlPlane:
    cp = ControlPlane(ephemeral_store(), secret_key=SECRET_KEY)
    for name in names:
        machine_id = "machine_%s" % name
        agent_id = "agent_%s" % name
        cp.register_machine(
            "%s-host" % name,
            machine_id=machine_id,
            labels={},
            resources={},
            trusted=True,
        )
        cp.register_agent(
            machine_id,
            name,
            [PACKAGE_CAPABILITY, "python"],
            resources={},
            agent_id=agent_id,
        )
    return cp


def _issue(cp: ControlPlane, agent_id: str):
    return WorkerCredentialLifecycle(cp.store).issue(
        agent_id,
        fleet="test",
        environment="vm",
        expected_source_commit=SOURCE_SHA,
        expected_runtime_digest=RUNTIME_SHA,
        required_capabilities=[PACKAGE_CAPABILITY, "python"],
        package_capable=True,
    )


def _observe(
    cp: ControlPlane,
    issue,
    env_path: Path,
    *,
    generation: str,
    extra_resources: dict | None = None,
    seen_at: str = FUTURE_SEEN,
) -> None:
    resources = {
        "source_state": {
            "schema": "mac.worker_source_state.v1",
            "commit_sha": SOURCE_SHA,
            "dirty": False,
        },
        "worker_credential": credential_resource_from_env(
            issue.record["agent_id"], read_env_file(env_path)
        ),
        "worker_credential_authenticated": authenticated_credential_resource(
            agent_id=issue.record["agent_id"],
            principal_id=issue.record["id"],
            token_fingerprint=issue.record["token_fingerprint"],
            credential_version=issue.worker_version,
        ),
        "deployment_generation": generation,
        **dict(extra_resources or {}),
    }
    cp.store.execute(
        "UPDATE agents SET capabilities = ?, resources = ?, running_digest = ?, "
        "status = 'idle', health_status = 'healthy', last_seen_at = ? "
        "WHERE id = ?",
        (
            json.dumps([PACKAGE_CAPABILITY, "python"]),
            json.dumps(resources),
            RUNTIME_SHA,
            seen_at,
            issue.record["agent_id"],
        ),
    )


def _bootstrap_active(cp: ControlPlane, agent_id: str, root: Path):
    issue = _issue(cp, agent_id)
    env_path = root / (agent_id + "-old.env")
    receipt = install_vm_manifest(
        installation_manifest(issue), env_path, expected_agent_id=agent_id
    )
    _observe(cp, issue, env_path, generation="prior-generation")
    WorkerCredentialLifecycle(cp.store).activate(agent_id, issue.record["id"], receipt=receipt)
    return issue


def _report_attestation() -> dict:
    digest = "sha256:" + ("b" * 64)
    return read_only_report_repository_executor_attestation(
        runtime_image_ref=("ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + ("c" * 64)),
        policy_sha256=digest,
        openshell_bin_path="/usr/local/bin/openshell",
        openshell_bin_sha256=digest,
        executor_path="/opt/mac/executor",
        executor_sha256=digest,
        platform="linux",
        isolation_posture="landlock_enforced",
        python_path="/usr/bin/python3",
        python_sha256=digest,
        executor_script_path="/opt/mac/executor.py",
        executor_script_sha256=digest,
        source_root="/opt/mac/source",
        source_bundle_sha256=digest,
    )


def _report_resources(agent_id: str, attestation: dict, timestamp: str) -> dict:
    return {
        "openshell_required": True,
        REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY: attestation,
        "startup_self_test": {
            "schema": "mac.agent_startup_self_test.v1",
            "agent_id": agent_id,
            "timestamp": timestamp,
            "status": "passed",
            "blocking_problems": [],
            "checks": {
                "openshell_executor_config": True,
                "report_repository_executor_attestation": True,
            },
            "report_repository_executor_attestation": attestation,
        },
    }


def _approved_report_projection(attestation: dict) -> dict:
    arguments = {
        key: attestation[key]
        for key in (
            "runtime_image_ref",
            "policy_sha256",
            "openshell_bin_path",
            "openshell_bin_sha256",
            "executor_path",
            "executor_sha256",
            "platform",
            "isolation_posture",
            "python_path",
            "python_sha256",
            "executor_script_path",
            "executor_script_sha256",
            "source_root",
            "source_bundle_sha256",
        )
    }
    return {
        REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY: (
            read_only_report_repository_executor_approval(**arguments)
        ),
        REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY: (
            read_only_report_repository_executor_resource(**arguments)
        ),
    }


def _candidate_fingerprint(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def _candidate_proof(
    *,
    key: str,
    epoch_id: str,
    agent_id: str,
    generation: str,
    principal_id: str,
) -> dict:
    challenge = {
        "schema": ATTESTATION_PROOF_SCHEMA,
        "purpose": ATTESTATION_PROOF_PURPOSE,
        "epoch_id": epoch_id,
        "agent_id": agent_id,
        "generation": generation,
        "principal_id": principal_id,
        "candidate_fingerprint": _candidate_fingerprint(key),
        "nonce": "n" * 40,
    }
    return {
        "challenge": challenge,
        "signature": sign_verification_manifest(key, challenge),
    }


def _prepare_item(
    pending,
    *,
    generation: str,
    baseline_seen: str,
    candidate_key: str | None,
    expected_dispatch_hold: bool = False,
    expected_hold_reason: str | None = None,
    expected_hold_at: str | None = None,
    report_action: str = "preserve",
    report_attestation: dict | None = None,
    principal_mode: str = "pending",
) -> dict:
    return {
        "agent_id": pending.record["agent_id"],
        "expected_dispatch_hold": expected_dispatch_hold,
        "expected_hold_reason": expected_hold_reason,
        "expected_hold_at": expected_hold_at,
        "generation": generation,
        "baseline_seen": baseline_seen,
        "principal_id": pending.record["id"],
        "principal_mode": principal_mode,
        "attestation_candidate": ({"key": candidate_key} if candidate_key is not None else None),
        "report_executor_action": report_action,
        "report_executor_attestation": report_attestation,
    }


def _apply_pending(
    cp: ControlPlane,
    pending,
    root: Path,
    *,
    generation: str,
    extra_resources: dict | None = None,
):
    env_path = root / (pending.record["agent_id"] + "-pending.env")
    receipt = install_vm_manifest(
        installation_manifest(pending),
        env_path,
        expected_agent_id=pending.record["agent_id"],
    )
    _observe(
        cp,
        pending,
        env_path,
        generation=generation,
        extra_resources=extra_resources,
        seen_at=APPLIED_SEEN,
    )
    return receipt


def _proof_item(
    pending,
    receipt: dict,
    *,
    candidate_key: str | None,
    epoch_id: str,
    generation: str,
    report_timestamp: str | None = None,
) -> dict:
    return {
        "agent_id": pending.record["agent_id"],
        "install_receipt": receipt,
        "attestation_proof": (
            _candidate_proof(
                key=candidate_key,
                epoch_id=epoch_id,
                agent_id=pending.record["agent_id"],
                generation=generation,
                principal_id=pending.record["id"],
            )
            if candidate_key is not None
            else None
        ),
        "report_executor_startup_timestamp": report_timestamp,
    }


def test_heartbeat_ttl_pins_epoch_identity_until_explicit_quarantine_release(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    cp.update_agent("agent_alpha", instance_kind="fungible")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    baseline = cp.get_agent("agent_alpha").last_seen_at
    epoch_id = "epoch-ttl-identity-pin"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-next",
                baseline_seen=baseline,
                candidate_key=None,
            )
        ],
    )
    assert opened["agents"][0]["principal_mode"] == "pending"
    stale = (parse_time(utcnow()) - timedelta(hours=2)).isoformat(timespec="microseconds")
    cp.store.execute(
        "UPDATE agents SET last_seen_at = ? WHERE id = ?",
        (stale, "agent_alpha"),
    )

    assert cp.expire_ephemeral_agents() == []
    retained = cp.get_agent("agent_alpha")
    assert retained.deleted_at is None
    assert retained.status == "offline"
    assert retained.health_status == "degraded"
    marker = retained.resources["deployment_availability"]
    assert marker == {
        "schema": "mac.deployment_availability.v1",
        "state": "deployment_unavailable",
        "epoch_id": epoch_id,
        "reason": "heartbeat_ttl_expired",
        "observed_at": marker["observed_at"],
        "last_seen_at": stale,
        "ttl_seconds": cp.FUNGIBLE_DEFAULT_TTL_SECONDS,
        "hold_reason": opened["agents"][0]["epoch_hold_reason"],
    }
    with pytest.raises(ValidationError, match="reserved by an open fleet release epoch"):
        cp.delete_agent("agent_alpha")

    # A worker inventory refresh cannot forge away hub-owned deployment state,
    # nor can it report healthy while the epoch still owns the outage.
    heartbeating = cp.heartbeat_agent(
        "agent_alpha",
        status="idle",
        health_status="healthy",
        resources={"capacity": 1},
    )
    assert heartbeating.resources["deployment_availability"] == marker
    assert heartbeating.health_status == "degraded"

    cp.fleet_release_epochs.abort(
        epoch_id,
        opened["identity_sha256"],
        reason="worker unavailable during release",
    )
    quarantined = cp.get_agent("agent_alpha")
    quarantine = quarantined.resources["deployment_availability"]
    assert quarantine["state"] == "deployment_quarantined"
    assert quarantine["hold_reason"] == quarantined.dispatch_hold_reason

    replaced, quarantined = cp.acquire_agent_dispatch_hold(
        "agent_alpha",
        "operator: inspect unavailable release member",
        expected_dispatch_hold=True,
        expected_reason=quarantined.dispatch_hold_reason,
    )
    assert replaced is True
    assert (
        quarantined.resources["deployment_availability"]["hold_reason"]
        == quarantined.dispatch_hold_reason
    )

    cp.store.execute(
        "UPDATE agents SET last_seen_at = ? WHERE id = ?",
        (stale, "agent_alpha"),
    )
    assert cp.expire_ephemeral_agents() == []
    assert cp.get_agent("agent_alpha").deleted_at is None

    released, _agent = cp.release_agent_dispatch_hold(
        "agent_alpha", quarantined.dispatch_hold_reason
    )
    assert released is True
    assert "deployment_availability" not in cp.get_agent("agent_alpha").resources
    assert [item.id for item in cp.expire_ephemeral_agents()] == ["agent_alpha"]
    assert cp.get_agent("agent_alpha").deleted_at is not None


def test_open_prove_commit_promotes_all_authority_atomically(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    old = _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    old_attestation_key = cp._agent_attestation_key("agent_alpha")
    baseline = cp.get_agent("agent_alpha").last_seen_at
    candidate_key = "candidate-attestation-key-" + ("x" * 32)
    generation = "generation-next"
    report_attestation = _report_attestation()
    report_timestamp = "2100-01-01T00:00:01+00:00"
    epoch_id = "epoch-complete-authority"

    open_items = [
        _prepare_item(
            pending,
            generation=generation,
            baseline_seen=baseline,
            candidate_key=candidate_key,
            report_action="approve",
            report_attestation=report_attestation,
        )
    ]
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        open_items,
        successor_hold_reason="synchronized successor hold",
        desired_policy_mode=MODE_ENFORCED,
    )
    assert (
        cp.fleet_release_epochs.open_epoch(
            epoch_id,
            open_items,
            successor_hold_reason="synchronized successor hold",
            desired_policy_mode=MODE_ENFORCED,
        )
        == opened
    )
    changed_items = json.loads(json.dumps(open_items))
    changed_items[0]["generation"] = "different-generation"
    with pytest.raises(ValidationError, match="different request"):
        cp.fleet_release_epochs.open_epoch(
            epoch_id,
            changed_items,
            successor_hold_reason="synchronized successor hold",
            desired_policy_mode=MODE_ENFORCED,
        )
    assert opened["status"] == "open"
    assert cp.fleet_release_epochs.active_publication_barrier() == {
        "schema": "mac.fleet_release_publication_barrier.v1",
        "epoch_id": epoch_id,
        "state": "open",
        "prepared_at": opened["prepared_at"],
        "proved_at": None,
    }
    assert cp.get_agent("agent_alpha").dispatch_hold_reason.startswith("mac:fleet-release:")
    assert cp._agent_attestation_key("agent_alpha") == old_attestation_key
    states = {
        item["id"]: item["state"]
        for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")
    }
    assert states == {
        old.record["id"]: "active",
        pending.record["id"]: "pending_install",
    }
    assert candidate_key not in json.dumps(opened)

    receipt = _apply_pending(
        cp,
        pending,
        tmp_path,
        generation=generation,
        extra_resources=_report_resources("agent_alpha", report_attestation, report_timestamp),
    )
    proof = _proof_item(
        pending,
        receipt,
        candidate_key=candidate_key,
        epoch_id=epoch_id,
        generation=generation,
        report_timestamp=report_timestamp,
    )
    proved = cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof])
    assert proved["status"] == "proved"
    assert cp.fleet_release_epochs.active_publication_barrier()["state"] == "proved"
    assert cp._agent_attestation_key("agent_alpha") == old_attestation_key
    assert cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof]) == proved

    committed = cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
    assert committed["status"] == "committed"
    assert cp.fleet_release_epochs.active_publication_barrier() is None
    assert cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"]) == committed
    assert cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof]) == committed
    changed_proof = json.loads(json.dumps(proof))
    changed_proof["install_receipt"]["installed_at"] = "2100-01-03T00:00:00+00:00"
    with pytest.raises(ValidationError, match="different evidence"):
        cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [changed_proof])
    with pytest.raises(ValidationError, match="identity digest"):
        cp.fleet_release_epochs.commit(epoch_id, "sha256:" + ("e" * 64))
    with pytest.raises(TransitionError, match="cannot abort"):
        cp.fleet_release_epochs.abort(
            epoch_id,
            opened["identity_sha256"],
            reason="commit already won",
        )
    agent = cp.get_agent("agent_alpha")
    assert agent.dispatch_hold is True
    assert agent.dispatch_hold_reason == "synchronized successor hold"
    assert cp._agent_attestation_key("agent_alpha") == candidate_key
    assert cp._agent_attestation_prev_key("agent_alpha") == old_attestation_key
    assert agent_has_read_only_report_repository_executor(agent.resources)
    assert read_policy_state(store=cp.store)["mode"] == MODE_ENFORCED
    states = {
        item["id"]: item["state"]
        for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")
    }
    assert states == {old.record["id"]: "superseded", pending.record["id"]: "active"}
    assert (
        cp.store.query_one(
            "SELECT 1 FROM fleet_release_attestation_candidates WHERE epoch_id = ?",
            (epoch_id,),
        )
        is None
    )
    assert cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"]) == committed
    assert cp.fleet_release_epochs.status(epoch_id, "sha256:" + ("f" * 64))["status"] == "mismatch"
    cp.store.execute(
        "DELETE FROM agent_lifecycle_events WHERE id = ?",
        (cp.fleet_release_epochs._marker_id(epoch_id),),
    )
    assert (
        cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"])["status"] == "mismatch"
    )
    with pytest.raises(TransitionError, match="marker is incomplete"):
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])


def test_deploy_preserves_current_credential_across_commit_and_abort(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    current = _bootstrap_active(cp, "agent_alpha", tmp_path)
    lifecycle = WorkerCredentialLifecycle(cp.store)
    generation = "generation-without-credential-rotation"
    epoch_id = "epoch-current-credential"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                current,
                generation=generation,
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
                principal_mode="current",
            )
        ],
    )
    assert opened["agents"][0]["principal_mode"] == "current"
    # The successor source/runtime may change while the same bearer continues
    # to authenticate. Credential validation must not classify that as a key
    # failure or demand a new principal.
    _observe(
        cp,
        current,
        tmp_path / "agent_alpha-old.env",
        generation=generation,
        seen_at=APPLIED_SEEN,
    )
    proof = _proof_item(
        current,
        None,
        candidate_key=None,
        epoch_id=epoch_id,
        generation=generation,
    )
    with pytest.raises(ValidationError, match="cannot carry an install receipt"):
        changed = dict(proof)
        changed["install_receipt"] = {"schema": "unexpected-rotation"}
        cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [changed])
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={"admin": ["admin"]},
        )
    )
    proved = client.post(
        f"/agents/dispatch-hold/epochs/{epoch_id}/prove",
        headers={"Authorization": "Bearer admin"},
        json={"identity_sha256": opened["identity_sha256"], "proofs": [proof]},
    )
    assert proved.status_code == 200
    assert proved.json()["status"] == "proved"
    assert cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof]) == (
        proved.json()
    )
    cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
    assert (
        cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof])["status"]
        == "committed"
    )
    assert [(row["id"], row["state"]) for row in lifecycle.list(agent_id="agent_alpha")] == [
        (current.record["id"], "active")
    ]

    second = cp.fleet_release_epochs.open_epoch(
        "epoch-current-credential-abort",
        [
            _prepare_item(
                current,
                generation="generation-aborted",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
                principal_mode="current",
            )
        ],
    )
    cp.fleet_release_epochs.abort(
        "epoch-current-credential-abort",
        second["identity_sha256"],
        reason="injected successor deployment failure",
    )
    assert [(row["id"], row["state"]) for row in lifecycle.list(agent_id="agent_alpha")] == [
        (current.record["id"], "active")
    ]


def test_current_principal_mode_fails_closed_on_authenticated_heartbeat_identity(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    current = _bootstrap_active(cp, "agent_alpha", tmp_path)
    row = cp.store.query_one("SELECT resources FROM agents WHERE id = ?", ("agent_alpha",))
    resources = json.loads(row["resources"])
    resources.pop("worker_credential_authenticated")
    cp.store.execute(
        "UPDATE agents SET resources = ? WHERE id = ?",
        (json.dumps(resources), "agent_alpha"),
    )

    with pytest.raises(
        ValidationError, match="current worker credential lacks authenticated heartbeat proof"
    ):
        cp.fleet_release_epochs.open_epoch(
            "epoch-current-heartbeat-mismatch",
            [
                _prepare_item(
                    current,
                    generation="generation-current-heartbeat-mismatch",
                    baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                    candidate_key=None,
                    principal_mode="current",
                )
            ],
        )

    assert (
        cp.store.query_one(
            "SELECT epoch_id FROM fleet_release_epochs WHERE epoch_id = ?",
            ("epoch-current-heartbeat-mismatch",),
        )
        is None
    )
    assert WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")[0]["state"] == (
        "active"
    )


def test_principal_mode_never_falls_back_between_current_and_pending(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    current = _bootstrap_active(cp, "agent_alpha", tmp_path)
    current_as_pending = _prepare_item(
        current,
        generation="generation-explicit-pending",
        baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
        candidate_key=None,
    )
    current_as_pending["principal_mode"] = "pending"
    with pytest.raises(ValidationError, match="requires an unexpired pending principal"):
        cp.fleet_release_epochs.open_epoch(
            "epoch-explicit-pending-with-current", [current_as_pending]
        )

    pending = _issue(cp, "agent_alpha")
    pending_as_current = _prepare_item(
        pending,
        generation="generation-explicit-current",
        baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
        candidate_key=None,
    )
    pending_as_current["principal_mode"] = "current"
    with pytest.raises(ValidationError, match="current worker principal is not active"):
        cp.fleet_release_epochs.open_epoch(
            "epoch-explicit-current-with-pending", [pending_as_current]
        )


def test_open_epoch_pins_fungible_participant_against_ttl_expiry(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    cp.store.execute(
        "UPDATE agents SET instance_kind = 'fungible', last_seen_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", "agent_alpha"),
    )
    opened = cp.fleet_release_epochs.open_epoch(
        "epoch-ttl-pin",
        [
            _prepare_item(
                pending,
                generation="generation-ttl-pin",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )

    assert cp.expire_ephemeral_agents() == []
    assert cp.get_agent("agent_alpha").deleted_at is None

    cp.fleet_release_epochs.abort(
        "epoch-ttl-pin",
        opened["identity_sha256"],
        reason="test completed",
    )
    # Abort explicitly quarantines an unavailable participant.  Merely ending
    # the epoch is not authority for the TTL sweeper to destroy its identity.
    assert cp.expire_ephemeral_agents() == []
    quarantined = cp.get_agent("agent_alpha")
    assert quarantined.resources["deployment_availability"]["state"] == ("deployment_quarantined")
    released, _ = cp.release_agent_dispatch_hold("agent_alpha", quarantined.dispatch_hold_reason)
    assert released is True
    expired = cp.expire_ephemeral_agents()
    assert [agent.id for agent in expired] == ["agent_alpha"]
    assert cp.get_agent("agent_alpha").deleted_at is not None


def test_open_rejects_same_reason_hold_reacquired_after_review(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    reviewed = cp.set_agent_dispatch_hold("agent_alpha", "same owner label")
    cp.store.execute(
        "UPDATE agents SET dispatch_hold_at = ? WHERE id = 'agent_alpha'",
        ("2100-02-01T00:00:00+00:00",),
    )
    with pytest.raises(ValidationError, match="lost expected prior hold"):
        cp.fleet_release_epochs.open_epoch(
            "epoch-stale-hold-owner",
            [
                _prepare_item(
                    pending,
                    generation="generation-stale-hold",
                    baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                    candidate_key=None,
                    expected_dispatch_hold=True,
                    expected_hold_reason="same owner label",
                    expected_hold_at=reviewed.dispatch_hold_at,
                )
            ],
        )
    assert (
        cp.store.query_one(
            "SELECT 1 FROM fleet_release_epochs WHERE epoch_id = ?",
            ("epoch-stale-hold-owner",),
        )
        is None
    )


def test_epoch_open_fails_fast_while_runtime_publication_owns_barrier(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    entered = threading.Event()
    release = threading.Event()

    def _hold_publication() -> None:
        with cp.fleet_release_epochs.publication_serialization():
            entered.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=_hold_publication, daemon=True)
    holder.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(
            TransitionError,
            match="runtime-source publication is in progress",
        ):
            cp.fleet_release_epochs.open_epoch(
                "epoch-must-not-overtake-publication",
                [],
            )
    finally:
        release.set()
        holder.join(timeout=5)
    assert not holder.is_alive()


def test_publication_barrier_path_uses_relocatable_mac_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    relocated = tmp_path / "relocated-mac-home"
    monkeypatch.setenv("MAC_HOME", str(relocated))
    cp = ControlPlane(ephemeral_store(), secret_key=SECRET_KEY)

    assert cp.fleet_release_epochs._publication_lock_path() == (
        relocated / "fleet-release-publication.lock"
    )


def test_status_and_terminal_actions_reject_corrupt_participant_identity(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-corrupt-identity"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-integrity",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    cp.store.execute(
        "UPDATE fleet_release_epoch_agents SET generation = ? "
        "WHERE epoch_id = ? AND agent_id = 'agent_alpha'",
        ("manually-corrupted-generation", epoch_id),
    )
    assert (
        cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"])["status"] == "mismatch"
    )
    with pytest.raises(TransitionError, match="identity storage is corrupt"):
        cp.fleet_release_epochs.abort(
            epoch_id,
            opened["identity_sha256"],
            reason="corruption must not acquire authority",
        )


def test_status_and_commit_reject_corrupt_proof_projection(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-corrupt-proof"
    generation = "generation-proof-integrity"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation=generation,
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    receipt = _apply_pending(cp, pending, tmp_path, generation=generation)
    cp.fleet_release_epochs.prove(
        epoch_id,
        opened["identity_sha256"],
        [
            _proof_item(
                pending,
                receipt,
                candidate_key=None,
                epoch_id=epoch_id,
                generation=generation,
            )
        ],
    )
    cp.store.execute(
        "UPDATE fleet_release_epoch_agents SET install_receipt_sha256 = ? "
        "WHERE epoch_id = ? AND agent_id = 'agent_alpha'",
        ("sha256:" + ("0" * 64), epoch_id),
    )
    assert (
        cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"])["status"] == "mismatch"
    )
    with pytest.raises(TransitionError, match="proof storage is corrupt"):
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])


@pytest.mark.parametrize("prove_before_abort", [False, True])
def test_open_is_pre_mutation_and_abort_restores_exact_prior_hold(
    tmp_path: Path, prove_before_abort: bool
) -> None:
    cp = _plane(tmp_path / "mac.db")
    old = _bootstrap_active(cp, "agent_alpha", tmp_path)
    prior_pending = _issue(cp, "agent_alpha")
    pending = _issue(cp, "agent_alpha")
    prior = cp.set_agent_dispatch_hold("agent_alpha", "operator maintenance")
    candidate_key = "abort-candidate-key-" + ("q" * 40)
    epoch_id = "epoch-abort-restore"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-abort",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=candidate_key,
                expected_dispatch_hold=True,
                expected_hold_reason="operator maintenance",
                expected_hold_at=prior.dispatch_hold_at,
            )
        ],
    )
    assert opened["status"] == "open"
    assert cp.get_agent("agent_alpha").dispatch_hold_reason != "operator maintenance"
    assert cp._agent_attestation_key("agent_alpha") is not None
    with pytest.raises(WorkerCredentialError, match="reserved"):
        _issue(cp, "agent_alpha")
    with pytest.raises(ValidationError, match="reserved"):
        cp.fleet_release_epochs.open_epoch(
            "competing-epoch",
            [
                _prepare_item(
                    pending,
                    generation="generation-abort",
                    baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                    candidate_key=None,
                    expected_dispatch_hold=True,
                    expected_hold_reason=cp.get_agent("agent_alpha").dispatch_hold_reason,
                    expected_hold_at=cp.get_agent("agent_alpha").dispatch_hold_at,
                )
            ],
        )

    if prove_before_abort:
        receipt = _apply_pending(
            cp,
            pending,
            tmp_path,
            generation="generation-abort",
        )
        cp.fleet_release_epochs.prove(
            epoch_id,
            opened["identity_sha256"],
            [
                _proof_item(
                    pending,
                    receipt,
                    candidate_key=candidate_key,
                    epoch_id=epoch_id,
                    generation="generation-abort",
                )
            ],
        )

    abort_disposition = "auto"
    if prove_before_abort:
        # The pending credential is installed and heartbeating; a default abort
        # must refuse the destructive revoke that caused the 403 outage until an
        # explicit recovery disposition is chosen.
        with pytest.raises(TransitionError, match="installed pending credentials"):
            cp.fleet_release_epochs.abort(
                epoch_id,
                opened["identity_sha256"],
                reason="node apply was rolled back",
            )
        abort_disposition = "discard_installed"
    aborted = cp.fleet_release_epochs.abort(
        epoch_id,
        opened["identity_sha256"],
        reason="node apply was rolled back",
        disposition=abort_disposition,
    )
    assert aborted["status"] == "aborted"
    assert cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"]) == aborted
    assert (
        cp.fleet_release_epochs.abort(
            epoch_id,
            opened["identity_sha256"],
            reason="node apply was rolled back",
            disposition=abort_disposition,
        )
        == aborted
    )
    with pytest.raises(ValidationError, match="different reason"):
        cp.fleet_release_epochs.abort(
            epoch_id,
            opened["identity_sha256"],
            reason="different rollback reason",
        )
    with pytest.raises(TransitionError, match="cannot commit"):
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
    restored = cp.get_agent("agent_alpha")
    assert restored.dispatch_hold is True
    assert restored.dispatch_hold_reason == "operator maintenance"
    assert restored.dispatch_hold_at == prior.dispatch_hold_at
    assert cp.fleet_release_epochs._restored_prior_hold_matches(
        {
            "dispatch_hold": restored.dispatch_hold,
            "dispatch_hold_reason": restored.dispatch_hold_reason,
            "dispatch_hold_at": restored.dispatch_hold_at,
        },
        {
            "prior_dispatch_hold": True,
            "prior_hold_reason": "operator maintenance",
            "prior_hold_at": prior.dispatch_hold_at,
        },
    )
    assert cp.fleet_release_epochs._restored_prior_hold_matches(
        {
            "dispatch_hold": False,
            "dispatch_hold_reason": None,
            "dispatch_hold_at": None,
        },
        {"prior_dispatch_hold": False},
    )
    states = {
        item["id"]: item["state"]
        for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")
    }
    assert states == {
        old.record["id"]: "active",
        prior_pending.record["id"]: "pending_install",
        pending.record["id"]: "revoked",
    }
    projected = WorkerCredentialPrincipalProvider(cp.store).tokens()
    assert old.record["token_hash"] in projected
    assert prior_pending.record["token_hash"] in projected
    assert pending.record["token_hash"] not in projected
    assert (
        cp.store.query_one(
            "SELECT 1 FROM fleet_release_attestation_candidates WHERE epoch_id = ?",
            (epoch_id,),
        )
        is None
    )


def test_abort_retain_installed_preserves_proven_predecessor_projection(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    old = _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    candidate_key = "retain-candidate-key-" + ("q" * 40)
    epoch_id = "epoch-abort-retain"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-retain",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=candidate_key,
            )
        ],
    )
    receipt = _apply_pending(cp, pending, tmp_path, generation="generation-retain")
    cp.fleet_release_epochs.prove(
        epoch_id,
        opened["identity_sha256"],
        [
            _proof_item(
                pending,
                receipt,
                candidate_key=candidate_key,
                epoch_id=epoch_id,
                generation="generation-retain",
            )
        ],
    )

    # A default abort must refuse to revoke the installed credential.
    with pytest.raises(TransitionError, match="installed pending credentials"):
        cp.fleet_release_epochs.abort(
            epoch_id,
            opened["identity_sha256"],
            reason="node apply is being rolled back",
        )

    aborted = cp.fleet_release_epochs.abort(
        epoch_id,
        opened["identity_sha256"],
        reason="node apply is being rolled back",
        disposition="retain_installed",
    )
    assert aborted["status"] == "aborted"
    assert aborted["abort_disposition"] == "retain_installed"
    assert cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"]) == aborted

    # The installed successor stays pending_install so the node keeps
    # authenticating with the credential already written into its mac.env.
    states = {
        item["id"]: item["state"]
        for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")
    }
    assert states == {
        old.record["id"]: "active",
        pending.record["id"]: "pending_install",
    }
    projected = WorkerCredentialPrincipalProvider(cp.store).tokens()
    assert old.record["token_hash"] in projected
    assert pending.record["token_hash"] in projected

    # Replaying with a conflicting disposition is rejected; the recorded one
    # replays identically.
    with pytest.raises(ValidationError, match="different disposition"):
        cp.fleet_release_epochs.abort(
            epoch_id,
            opened["identity_sha256"],
            reason="node apply is being rolled back",
            disposition="discard_installed",
        )
    assert (
        cp.fleet_release_epochs.abort(
            epoch_id,
            opened["identity_sha256"],
            reason="node apply is being rolled back",
            disposition="retain_installed",
        )
        == aborted
    )


def test_abort_accepts_principal_loss_for_uninstalled_participant(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    old = _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-abort-uninstalled-principal-loss"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-uninstalled-principal-loss",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    cp.store.execute(
        "UPDATE worker_credentials SET state = 'revoked', revoked_at = ? WHERE id IN (?, ?)",
        (utcnow(), old.record["id"], pending.record["id"]),
    )

    aborted = cp.fleet_release_epochs.abort(
        epoch_id,
        opened["identity_sha256"],
        reason="TTL cleanup retired untouched participant credentials",
    )

    assert aborted["status"] == "aborted"
    assert cp.get_agent("agent_alpha").dispatch_hold is True


def test_abort_rejects_principal_loss_for_installed_participant(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-abort-installed-principal-loss"
    generation = "generation-installed-principal-loss"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation=generation,
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    receipt = _apply_pending(cp, pending, tmp_path, generation=generation)
    cp.fleet_release_epochs.prove(
        epoch_id,
        opened["identity_sha256"],
        [
            _proof_item(
                pending,
                receipt,
                candidate_key=None,
                epoch_id=epoch_id,
                generation=generation,
            )
        ],
    )
    cp.store.execute(
        "UPDATE worker_credentials SET state = 'revoked', revoked_at = ? WHERE id = ?",
        (utcnow(), pending.record["id"]),
    )

    with pytest.raises(ValidationError, match="principal set changed"):
        cp.fleet_release_epochs.abort(
            epoch_id,
            opened["identity_sha256"],
            reason="installed credential continuity must remain exact",
            disposition="retain_installed",
        )


def test_abort_rejects_unknown_disposition(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-abort-bad-disposition"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-bad-disposition",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    with pytest.raises(ValidationError, match="disposition is invalid"):
        cp.fleet_release_epochs.abort(
            epoch_id,
            opened["identity_sha256"],
            reason="rollback",
            disposition="teleport",
        )


def test_abort_accepts_prior_operator_hold_already_restored_exactly(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    old = _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    prior = cp.set_agent_dispatch_hold("agent_alpha", "operator maintenance")
    epoch_id = "epoch-abort-prior-hold-restored"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-abort-restored",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
                expected_dispatch_hold=True,
                expected_hold_reason="operator maintenance",
                expected_hold_at=prior.dispatch_hold_at,
            )
        ],
    )
    cp.store.execute(
        "UPDATE agents SET dispatch_hold = 1, dispatch_hold_reason = ?, "
        "dispatch_hold_at = ? WHERE id = ?",
        ("operator maintenance", prior.dispatch_hold_at, "agent_alpha"),
    )

    aborted = cp.fleet_release_epochs.abort(
        epoch_id,
        opened["identity_sha256"],
        reason="coordinator recovered exact prior hold before abort",
    )

    assert aborted["status"] == "aborted"
    restored = cp.get_agent("agent_alpha")
    assert restored.dispatch_hold is True
    assert restored.dispatch_hold_reason == "operator maintenance"
    assert restored.dispatch_hold_at == prior.dispatch_hold_at
    states = {
        item["id"]: item["state"]
        for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")
    }
    assert states == {old.record["id"]: "active", pending.record["id"]: "revoked"}


@pytest.mark.parametrize("allocator_v2", [False, True])
def test_aborted_epoch_fences_real_claims_across_worker_restart(tmp_path: Path, allocator_v2):
    cp = _plane(tmp_path / "mac.db", names=("alpha", "beta"))
    items = []
    for name in ("alpha", "beta"):
        agent_id = "agent_" + name
        _bootstrap_active(cp, agent_id, tmp_path)
        items.append(
            _prepare_item(
                _issue(cp, agent_id),
                generation="interrupted-deployment",
                baseline_seen=cp.get_agent(agent_id).last_seen_at,
                candidate_key=None,
            )
        )
    opened = cp.fleet_release_epochs.open_epoch("epoch-interrupted", items)
    task = cp.create_task(
        "Unrelated work arriving during compensation", required_capabilities=["python"]
    )
    aborted = cp.fleet_release_epochs.abort(
        "epoch-interrupted", opened["identity_sha256"], reason="phase-one quiescence rejected"
    )
    for participant in opened["agents"]:
        agent_id = participant["agent_id"]
        for restarted in (False, True):
            if restarted:
                # A supervisor can restore its process before compensation
                # finishes. Exercise the real registration and heartbeat paths.
                before = cp.get_agent(agent_id)
                cp.register_agent(
                    before.machine_id, before.name, before.capabilities, agent_id=agent_id
                )
                cp.heartbeat_agent(agent_id, status="idle", health_status="healthy")
            with pytest.raises(ValidationError, match="agent_dispatch_held"):
                cp.claim_task(task.id, agent_id, authoritative_allocator_v2=allocator_v2)
            current = cp.get_agent(agent_id)
            assert current.dispatch_hold is True
            assert current.dispatch_hold_reason == participant["epoch_hold_reason"]
            assert current.dispatch_hold_at == participant["epoch_hold_at"]
            unchanged = cp.get_task(task.id)
            assert unchanged.state == "open"
            assert unchanged.lease_id is None
            assert unchanged.owner_agent_id is None
            assert unchanged.attempt_count == 0
    assert (
        cp.fleet_release_epochs.abort(
            "epoch-interrupted", opened["identity_sha256"], reason="phase-one quiescence rejected"
        )
        == aborted
    )
    # Positive control: it was the retained fence, not an unrelated eligibility
    # failure, that prevented assignment. An explicit exact-owner release works.
    participant = opened["agents"][0]
    released, _ = cp.release_agent_dispatch_hold(
        participant["agent_id"], participant["epoch_hold_reason"]
    )
    assert released is True
    claimed, lease = cp.claim_task(
        task.id, participant["agent_id"], authoritative_allocator_v2=allocator_v2
    )
    assert claimed.lease_id == lease.id
    assert claimed.owner_agent_id == participant["agent_id"]
    assert claimed.attempt_count == 1


def test_abort_accepts_prior_unheld_snapshot_already_restored_exactly(
    tmp_path: Path,
) -> None:
    # Accept the legacy snapshot as recoverable, but reestablish its fence:
    # an unheld snapshot does not prove that node compensation has finished.
    cp = _plane(tmp_path / "mac.db")
    old = _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-abort-prior-unheld-restored"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-abort-unheld",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    cp.store.execute(
        "UPDATE agents SET dispatch_hold = 0, dispatch_hold_reason = NULL, "
        "dispatch_hold_at = NULL WHERE id = ?",
        ("agent_alpha",),
    )

    aborted = cp.fleet_release_epochs.abort(
        epoch_id,
        opened["identity_sha256"],
        reason="coordinator recovered exact prior unheld snapshot before abort",
    )

    assert aborted["status"] == "aborted"
    restored = cp.get_agent("agent_alpha")
    assert restored.dispatch_hold is True
    assert restored.dispatch_hold_reason == opened["agents"][0]["epoch_hold_reason"]
    assert restored.dispatch_hold_at == opened["agents"][0]["epoch_hold_at"]
    states = {
        item["id"]: item["state"]
        for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")
    }
    assert states == {old.record["id"]: "active", pending.record["id"]: "revoked"}


def test_full_cohort_commit_failure_rolls_back_early_promotions(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db", ("alpha", "beta"))
    old: dict[str, object] = {}
    pending: dict[str, object] = {}
    candidates: dict[str, str] = {}
    prepare_items = []
    for name in ("alpha", "beta"):
        agent_id = "agent_%s" % name
        old[name] = _bootstrap_active(cp, agent_id, tmp_path)
        pending[name] = _issue(cp, agent_id)
        candidates[name] = "candidate-%s-%s" % (name, "z" * 40)
        prepare_items.append(
            _prepare_item(
                pending[name],
                generation="generation-cohort",
                baseline_seen=cp.get_agent(agent_id).last_seen_at,
                candidate_key=candidates[name],
            )
        )
    opened = cp.fleet_release_epochs.open_epoch("epoch-two-agent", prepare_items)
    proofs = []
    for name in ("alpha", "beta"):
        receipt = _apply_pending(
            cp,
            pending[name],
            tmp_path,
            generation="generation-cohort",
        )
        proofs.append(
            _proof_item(
                pending[name],
                receipt,
                candidate_key=candidates[name],
                epoch_id="epoch-two-agent",
                generation="generation-cohort",
            )
        )
    cp.fleet_release_epochs.prove("epoch-two-agent", opened["identity_sha256"], proofs)
    superseding = cp.set_agent_dispatch_hold("agent_beta", "operator superseded epoch")
    with pytest.raises(ValidationError, match="epoch-owned hold"):
        cp.fleet_release_epochs.commit("epoch-two-agent", opened["identity_sha256"])
    for name in ("alpha", "beta"):
        states = {
            item["id"]: item["state"]
            for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_%s" % name)
        }
        assert states == {
            old[name].record["id"]: "active",
            pending[name].record["id"]: "pending_install",
        }
        assert cp._agent_attestation_key("agent_%s" % name) != candidates[name]
    # The cohort proved, so both pending credentials are installed and
    # heartbeating. A default abort must now refuse the destructive revoke and
    # force an explicit recovery disposition.
    with pytest.raises(TransitionError, match="installed pending credentials"):
        cp.fleet_release_epochs.abort(
            "epoch-two-agent",
            opened["identity_sha256"],
            reason="preserve later operator safety hold",
        )
    aborted = cp.fleet_release_epochs.abort(
        "epoch-two-agent",
        opened["identity_sha256"],
        reason="preserve later operator safety hold",
        disposition="discard_installed",
    )
    assert aborted["status"] == "aborted"
    assert aborted["abort_disposition"] == "discard_installed"
    alpha = cp.get_agent("agent_alpha")
    assert alpha.dispatch_hold is True
    assert alpha.dispatch_hold_reason == opened["agents"][0]["epoch_hold_reason"]
    beta = cp.get_agent("agent_beta")
    assert beta.dispatch_hold is True
    assert beta.dispatch_hold_reason == "operator superseded epoch"
    assert beta.dispatch_hold_at == superseding.dispatch_hold_at
    for name in ("alpha", "beta"):
        states = {
            item["id"]: item["state"]
            for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_%s" % name)
        }
        assert states == {
            old[name].record["id"]: "active",
            pending[name].record["id"]: "revoked",
        }


def test_proof_rejects_secret_bearing_receipt_and_wrong_candidate(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    candidate_key = "proof-key-" + ("r" * 40)
    epoch_id = "epoch-proof-validation"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-proof",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=candidate_key,
            )
        ],
    )
    receipt = _apply_pending(cp, pending, tmp_path, generation="generation-proof")
    proof = _proof_item(
        pending,
        receipt,
        candidate_key=candidate_key,
        epoch_id=epoch_id,
        generation="generation-proof",
    )
    secret_bearing = json.loads(json.dumps(proof))
    secret_bearing["install_receipt"]["token"] = pending.token
    with pytest.raises(ValidationError, match="unexpected or missing fields"):
        cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [secret_bearing])
    wrong = json.loads(json.dumps(proof))
    wrong["attestation_proof"]["signature"] = sign_verification_manifest(
        "other-key-" + ("w" * 40),
        wrong["attestation_proof"]["challenge"],
    )
    with pytest.raises(ValidationError, match="signature"):
        cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [wrong])
    row = cp.store.query_one(
        "SELECT state, proof_sha256 FROM fleet_release_epochs WHERE epoch_id = ?",
        (epoch_id,),
    )
    assert dict(row) == {"state": "open", "proof_sha256": None}


def test_report_executor_revoke_is_staged_until_commit(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    cp.store.execute(
        "UPDATE agents SET resources = ? WHERE id = 'agent_alpha'",
        (
            json.dumps(
                {
                    REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY: {"old": "approval"},
                    REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY: {"old": "marker"},
                }
            ),
        ),
    )
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-report-revoke"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-revoke",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
                report_action="revoke",
            )
        ],
    )
    assert REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY in cp.get_agent("agent_alpha").resources
    receipt = _apply_pending(
        cp,
        pending,
        tmp_path,
        generation="generation-revoke",
        extra_resources={
            REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY: {"old": "approval"},
            REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY: {"old": "marker"},
        },
    )
    proof = _proof_item(
        pending,
        receipt,
        candidate_key=None,
        epoch_id=epoch_id,
        generation="generation-revoke",
    )
    cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof])
    proved_resources = cp.get_agent("agent_alpha").resources
    drifted_resources = dict(proved_resources)
    drifted_resources[REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY] = {"concurrent": "replacement"}
    cp.store.execute(
        "UPDATE agents SET resources = ? WHERE id = 'agent_alpha'",
        (json.dumps(drifted_resources),),
    )
    with pytest.raises(ValidationError, match="report executor authority changed"):
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
    cp.store.execute(
        "UPDATE agents SET resources = ? WHERE id = 'agent_alpha'",
        (json.dumps(proved_resources),),
    )
    cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
    resources = cp.get_agent("agent_alpha").resources
    assert REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY not in resources
    assert REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY not in resources


@pytest.mark.parametrize("report_action", ["revoke", "approve"])
def test_staged_report_action_accepts_only_derived_marker_loss(
    tmp_path: Path, report_action: str
) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    old_attestation = _report_attestation()
    old_projection = _approved_report_projection(old_attestation)
    cp.store.execute(
        "UPDATE agents SET resources = ? WHERE id = 'agent_alpha'",
        (
            json.dumps(
                {
                    **_report_resources(
                        "agent_alpha",
                        old_attestation,
                        "2099-01-01T00:00:00+00:00",
                    ),
                    **old_projection,
                }
            ),
        ),
    )
    pending = _issue(cp, "agent_alpha")
    generation = "generation-marker-loss"
    epoch_id = "epoch-marker-loss-%s" % report_action
    new_attestation = dict(old_attestation)
    new_attestation["source_bundle_sha256"] = "sha256:" + ("d" * 64)
    report_timestamp = "2100-01-01T00:00:01+00:00"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation=generation,
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
                report_action=report_action,
                report_attestation=(new_attestation if report_action == "approve" else None),
            )
        ],
    )
    current_resources = {
        **_report_resources("agent_alpha", new_attestation, report_timestamp),
        REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY: old_projection[
            REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY
        ],
    }
    receipt = _apply_pending(
        cp,
        pending,
        tmp_path,
        generation=generation,
        extra_resources=current_resources,
    )
    proof = _proof_item(
        pending,
        receipt,
        candidate_key=None,
        epoch_id=epoch_id,
        generation=generation,
        report_timestamp=(report_timestamp if report_action == "approve" else None),
    )

    assert (
        cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof])["status"]
        == "proved"
    )
    assert (
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])["status"] == "committed"
    )
    resources = cp.get_agent("agent_alpha").resources
    if report_action == "revoke":
        assert REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY not in resources
        assert REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY not in resources
    else:
        assert agent_has_read_only_report_repository_executor(resources)
        assert (
            resources[REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY]["source_bundle_sha256"]
            == new_attestation["source_bundle_sha256"]
        )


def test_staged_report_action_rejects_approval_change_after_marker_loss(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    old_attestation = _report_attestation()
    old_projection = _approved_report_projection(old_attestation)
    cp.store.execute(
        "UPDATE agents SET resources = ? WHERE id = 'agent_alpha'",
        (json.dumps(old_projection),),
    )
    pending = _issue(cp, "agent_alpha")
    generation = "generation-approval-drift"
    epoch_id = "epoch-approval-drift"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation=generation,
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
                report_action="revoke",
            )
        ],
    )
    changed_approval = dict(old_projection[REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY])
    changed_approval["source_bundle_sha256"] = "sha256:" + ("e" * 64)
    receipt = _apply_pending(
        cp,
        pending,
        tmp_path,
        generation=generation,
        extra_resources={
            REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY: changed_approval,
        },
    )
    proof = _proof_item(
        pending,
        receipt,
        candidate_key=None,
        epoch_id=epoch_id,
        generation=generation,
    )

    with pytest.raises(ValidationError, match="report executor authority changed"):
        cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof])


def test_report_preserve_rejects_derived_marker_loss(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    attestation = _report_attestation()
    prior_projection = _approved_report_projection(attestation)
    cp.store.execute(
        "UPDATE agents SET resources = ? WHERE id = 'agent_alpha'",
        (json.dumps(prior_projection),),
    )
    pending = _issue(cp, "agent_alpha")
    generation = "generation-preserve-marker-loss"
    epoch_id = "epoch-preserve-marker-loss"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation=generation,
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    receipt = _apply_pending(
        cp,
        pending,
        tmp_path,
        generation=generation,
        extra_resources={
            REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY: prior_projection[
                REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY
            ]
        },
    )
    proof = _proof_item(
        pending,
        receipt,
        candidate_key=None,
        epoch_id=epoch_id,
        generation=generation,
    )

    with pytest.raises(ValidationError, match="report executor authority changed"):
        cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof])


def test_commit_rejects_new_service_claim_without_partial_promotion(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    old = _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    cp.seed_service_roles(["image.generate"])
    role = cp.service_roles.get_role_by_slug("media:image.generate")
    prior_claim = cp.service_roles.claim_service(role.id, "agent_alpha")
    epoch_id = "epoch-service-claim-cas"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-service-claim",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    assert cp.service_roles.list_active_claims(agent_id="agent_alpha") == []
    receipt = _apply_pending(
        cp,
        pending,
        tmp_path,
        generation="generation-service-claim",
    )
    cp.fleet_release_epochs.prove(
        epoch_id,
        opened["identity_sha256"],
        [
            _proof_item(
                pending,
                receipt,
                candidate_key=None,
                epoch_id=epoch_id,
                generation="generation-service-claim",
            )
        ],
    )
    cp.store.execute(
        "UPDATE service_claims SET status = 'active' WHERE id = ?",
        (prior_claim.id,),
    )
    with pytest.raises(ValidationError, match="new active service claims"):
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
    states = {
        item["id"]: item["state"]
        for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")
    }
    assert states == {
        old.record["id"]: "active",
        pending.record["id"]: "pending_install",
    }


def test_commit_and_abort_serialize_to_one_terminal_winner(tmp_path: Path) -> None:
    path = tmp_path / "mac.db"
    cp = _plane(path)
    old = _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-terminal-race"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-terminal-race",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    receipt = _apply_pending(
        cp,
        pending,
        tmp_path,
        generation="generation-terminal-race",
    )
    cp.fleet_release_epochs.prove(
        epoch_id,
        opened["identity_sha256"],
        [
            _proof_item(
                pending,
                receipt,
                candidate_key=None,
                epoch_id=epoch_id,
                generation="generation-terminal-race",
            )
        ],
    )
    # Same database as cp: the race under test is two writers serializing on
    # one epoch row. Separate schemas cannot contend.
    peer = ControlPlane(store_on(str(cp.store.path)), secret_key=SECRET_KEY)
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[Exception] = []
    result_lock = threading.Lock()

    def finish(action: str, plane: ControlPlane) -> None:
        try:
            barrier.wait(timeout=10)
            if action == "commit":
                result = plane.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
            else:
                result = plane.fleet_release_epochs.abort(
                    epoch_id,
                    opened["identity_sha256"],
                    reason="race selected rollback",
                )
            with result_lock:
                results.append(result)
        except Exception as exc:  # noqa: BLE001 - terminal loser is asserted.
            with result_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=finish, args=("commit", cp)),
        threading.Thread(target=finish, args=("abort", peer)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], TransitionError)
    winner = results[0]["status"]
    assert winner in {"committed", "aborted"}
    assert cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"])["status"] == winner
    states = {
        item["id"]: item["state"]
        for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")
    }
    expected = (
        {old.record["id"]: "superseded", pending.record["id"]: "active"}
        if winner == "committed"
        else {old.record["id"]: "active", pending.record["id"]: "revoked"}
    )
    assert states == expected
    peer.store.close()


def test_principal_inventory_and_policy_are_commit_cas_inputs(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-authority-cas"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-authority-cas",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
        desired_policy_mode=MODE_ENFORCED,
    )
    receipt = _apply_pending(
        cp,
        pending,
        tmp_path,
        generation="generation-authority-cas",
    )
    cp.fleet_release_epochs.prove(
        epoch_id,
        opened["identity_sha256"],
        [
            _proof_item(
                pending,
                receipt,
                candidate_key=None,
                epoch_id=epoch_id,
                generation="generation-authority-cas",
            )
        ],
    )
    cp.store.execute(
        "UPDATE worker_credentials SET state = 'revoked', revoked_at = ? WHERE id = ?",
        (utcnow(), pending.record["id"]),
    )
    with pytest.raises(ValidationError, match="principal set changed"):
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
    cp.store.execute(
        "UPDATE worker_credentials SET state = 'pending_install', revoked_at = NULL WHERE id = ?",
        (pending.record["id"],),
    )
    write_policy_state(MODE_COMPATIBILITY, store=cp.store, actor="concurrent")
    with pytest.raises(ValidationError, match="policy changed"):
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
    cp.store.execute("DELETE FROM worker_credential_policy_state WHERE singleton_key = 'fleet'")
    assert (
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])["status"] == "committed"
    )


def test_report_executor_preserve_leaves_authority_projection_unchanged(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    prior_projection = {
        REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY: {"prior": "approval"},
        REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY: {"prior": "marker"},
    }
    cp.store.execute(
        "UPDATE agents SET resources = ? WHERE id = 'agent_alpha'",
        (json.dumps(prior_projection),),
    )
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-report-preserve"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation="generation-preserve",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    receipt = _apply_pending(
        cp,
        pending,
        tmp_path,
        generation="generation-preserve",
        extra_resources=prior_projection,
    )
    proof = _proof_item(
        pending,
        receipt,
        candidate_key=None,
        epoch_id=epoch_id,
        generation="generation-preserve",
    )
    cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof])
    cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
    resources = cp.get_agent("agent_alpha").resources
    assert resources[REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY] == {"prior": "approval"}
    assert resources[REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY] == {"prior": "marker"}


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("active_task", "active work"),
        ("health", "node readiness"),
        ("health_blocking", "node readiness"),
        ("health_wrong_agent", "node readiness"),
        ("generation", "node readiness"),
    ],
)
def test_prove_rejects_active_work_and_node_readiness_drift(
    tmp_path: Path, drift: str, message: str
) -> None:
    cp = _plane(tmp_path / ("%s.db" % drift))
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-readiness-%s" % drift
    generation = "generation-readiness"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation=generation,
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    receipt = _apply_pending(cp, pending, tmp_path, generation=generation)
    if drift == "active_task":
        cp.store.execute(
            "UPDATE agents SET current_task_id = 'task-raced' WHERE id = 'agent_alpha'"
        )
    elif drift.startswith("health"):
        resources = cp.get_agent("agent_alpha").resources
        if drift != "health":
            resources["startup_self_test"] = {
                "schema": "mac.agent_startup_self_test.v1",
                "agent_id": ("agent_other" if drift == "health_wrong_agent" else "agent_alpha"),
                "status": "degraded",
                "blocking_problems": (
                    ["executor unavailable"] if drift == "health_blocking" else []
                ),
            }
        cp.store.execute(
            "UPDATE agents SET resources = ?, health_status = 'degraded' WHERE id = 'agent_alpha'",
            (json.dumps(resources),),
        )
    else:
        resources = cp.get_agent("agent_alpha").resources
        resources["deployment_generation"] = "generation-raced"
        cp.store.execute(
            "UPDATE agents SET resources = ? WHERE id = 'agent_alpha'",
            (json.dumps(resources),),
        )
    with pytest.raises(ValidationError, match=message):
        cp.fleet_release_epochs.prove(
            epoch_id,
            opened["identity_sha256"],
            [
                _proof_item(
                    pending,
                    receipt,
                    candidate_key=None,
                    epoch_id=epoch_id,
                    generation=generation,
                )
            ],
        )


def test_prove_and_commit_accept_advisory_degraded_startup_health(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "advisory-degraded.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-advisory-degraded"
    generation = "generation-advisory-degraded"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation=generation,
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    receipt = _apply_pending(
        cp,
        pending,
        tmp_path,
        generation=generation,
        extra_resources={
            "startup_self_test": {
                "schema": "mac.agent_startup_self_test.v1",
                "agent_id": "agent_alpha",
                "status": "degraded",
                "blocking_problems": [],
                "non_blocking_problems": ["transient model route unavailable"],
            }
        },
    )
    cp.store.execute("UPDATE agents SET health_status = 'degraded' WHERE id = 'agent_alpha'")
    cp.fleet_release_epochs.prove(
        epoch_id,
        opened["identity_sha256"],
        [
            _proof_item(
                pending,
                receipt,
                candidate_key=None,
                epoch_id=epoch_id,
                generation=generation,
            )
        ],
    )
    assert (
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])["status"] == "committed"
    )


def test_pre_prove_readiness_uses_pending_credential_evidence_without_mutation(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "readiness.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-pre-prove-readiness"
    generation = "generation-pre-prove-readiness"
    opened = cp.fleet_release_epochs.open_epoch(
        epoch_id,
        [
            _prepare_item(
                pending,
                generation=generation,
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    )
    _apply_pending(cp, pending, tmp_path, generation=generation)

    readiness = cp.fleet_release_epochs.pre_prove_readiness(epoch_id, opened["identity_sha256"])
    assert readiness == {
        "schema": "mac.fleet_release_pre_prove_readiness.v1",
        "status": "ready",
        "epoch_id": epoch_id,
        "hub_authority_id": cp.fleet_release_epochs.hub_authority_id,
        "identity_sha256": opened["identity_sha256"],
        "cohort_size": 1,
        "agents": [
            {
                "agent_id": "agent_alpha",
                "credential_version": pending.worker_version,
            }
        ],
    }
    stored = cp.store.query_one(
        "SELECT state, proof_sha256 FROM fleet_release_epochs WHERE epoch_id = ?",
        (epoch_id,),
    )
    assert dict(stored) == {"state": "open", "proof_sha256": None}

    resources = cp.get_agent("agent_alpha").resources
    del resources["worker_credential"]
    cp.store.execute(
        "UPDATE agents SET resources = ? WHERE id = 'agent_alpha'",
        (json.dumps(resources),),
    )
    with pytest.raises(
        ValidationError, match="activation requires live authenticated heartbeat proof"
    ):
        cp.fleet_release_epochs.pre_prove_readiness(epoch_id, opened["identity_sha256"])
    stored = cp.store.query_one(
        "SELECT state, proof_sha256 FROM fleet_release_epochs WHERE epoch_id = ?",
        (epoch_id,),
    )
    assert dict(stored) == {"state": "open", "proof_sha256": None}


def test_hub_authority_uuid_is_durable_and_status_exposes_it(tmp_path: Path) -> None:
    path = tmp_path / "mac.db"
    cp = _plane(path)
    authority_id = cp.fleet_release_epochs.hub_authority_id
    absent = cp.fleet_release_epochs.status("absent-epoch", "sha256:" + ("0" * 64))
    assert absent["hub_authority_id"] == authority_id
    dsn = str(cp.store.path)
    cp.store.close()
    # Reopen the SAME schema: ephemeral_store() would be a brand-new database,
    # in which a fresh authority id is the correct answer and the test proves
    # nothing. Under SQLite this was one file path opened twice.
    restarted = ControlPlane(store_on(dsn), secret_key=SECRET_KEY)
    assert restarted.fleet_release_epochs.hub_authority_id == authority_id
    assert (
        restarted.store.query_one("SELECT COUNT(*) AS count FROM hub_authority_identity")["count"]
        == 1
    )


def test_epoch_http_routes_are_admin_only_and_redact_candidate(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    candidate_key = "api-candidate-" + ("s" * 40)
    body = {
        "epoch_id": "epoch-api",
        "participants": [
            _prepare_item(
                pending,
                generation="generation-api",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=candidate_key,
            )
        ],
    }
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={"admin": ["admin"], "reader": ["read"]},
        )
    )
    rejected = client.post(
        "/agents/dispatch-hold/epochs/open",
        headers={"Authorization": "Bearer reader"},
        json=body,
    )
    assert rejected.status_code == 403
    authority_rejected = client.get(
        "/agents/dispatch-hold/authority",
        headers={"Authorization": "Bearer reader"},
    )
    assert authority_rejected.status_code == 403
    authority = client.get(
        "/agents/dispatch-hold/authority",
        headers={"Authorization": "Bearer admin"},
    )
    assert authority.status_code == 200
    assert authority.json() == {
        "schema": "mac.fleet_release_hub_authority.v1",
        "hub_authority_id": cp.fleet_release_epochs.hub_authority_id,
    }
    opened = client.post(
        "/agents/dispatch-hold/epochs/open",
        headers={"Authorization": "Bearer admin"},
        json=body,
    )
    assert opened.status_code == 200
    assert candidate_key not in opened.text
    receipt = opened.json()
    status = client.get(
        "/agents/dispatch-hold/epochs/epoch-api",
        params={"identity_sha256": receipt["identity_sha256"]},
        headers={"Authorization": "Bearer admin"},
    )
    assert status.status_code == 200
    assert status.json()["status"] == "open"
    assert candidate_key not in status.text
    stored = "\n".join(
        str(row["detail"])
        for row in cp.store.query_all(
            "SELECT detail FROM agent_lifecycle_events "
            "WHERE event_type = 'agent.fleet_release_epoch.opened'"
        )
    )
    assert candidate_key not in stored

    install_receipt = _apply_pending(
        cp,
        pending,
        tmp_path,
        generation="generation-api",
    )
    readiness_rejected = client.get(
        "/agents/dispatch-hold/epochs/epoch-api/readiness",
        params={"identity_sha256": receipt["identity_sha256"]},
        headers={"Authorization": "Bearer reader"},
    )
    assert readiness_rejected.status_code == 403
    readiness = client.get(
        "/agents/dispatch-hold/epochs/epoch-api/readiness",
        params={"identity_sha256": receipt["identity_sha256"]},
        headers={"Authorization": "Bearer admin"},
    )
    assert readiness.status_code == 200
    assert readiness.json()["status"] == "ready"
    assert readiness.json()["agents"] == [
        {
            "agent_id": "agent_alpha",
            "credential_version": pending.worker_version,
        }
    ]
    assert candidate_key not in readiness.text
    proof = _proof_item(
        pending,
        install_receipt,
        candidate_key=candidate_key,
        epoch_id="epoch-api",
        generation="generation-api",
    )
    proved = client.post(
        "/agents/dispatch-hold/epochs/epoch-api/prove",
        headers={"Authorization": "Bearer admin"},
        json={"identity_sha256": receipt["identity_sha256"], "proofs": [proof]},
    )
    assert proved.status_code == 200
    assert proved.json()["status"] == "proved"
    assert candidate_key not in proved.text
    committed = client.post(
        "/agents/dispatch-hold/epochs/epoch-api/commit",
        headers={"Authorization": "Bearer admin"},
        json={"identity_sha256": receipt["identity_sha256"]},
    )
    assert committed.status_code == 200
    assert committed.json()["status"] == "committed"
    assert candidate_key not in committed.text

    abort_pending = _issue(cp, "agent_alpha")
    abort_body = {
        "epoch_id": "epoch-api-abort",
        "participants": [
            _prepare_item(
                abort_pending,
                generation="generation-api-abort",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=None,
            )
        ],
    }
    abort_opened = client.post(
        "/agents/dispatch-hold/epochs/open",
        headers={"Authorization": "Bearer admin"},
        json=abort_body,
    )
    assert abort_opened.status_code == 200
    aborted = client.post(
        "/agents/dispatch-hold/epochs/epoch-api-abort/abort",
        headers={"Authorization": "Bearer admin"},
        json={
            "identity_sha256": abort_opened.json()["identity_sha256"],
            "reason": "HTTP abort contract test",
        },
    )
    assert aborted.status_code == 200
    assert aborted.json()["status"] == "aborted"
    abort_record = {
        item["id"]: item
        for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")
    }[abort_pending.record["id"]]
    assert abort_record["state"] == "revoked"


def test_open_epoch_replay_is_idempotent_and_pre_mutation(tmp_path: Path) -> None:
    """Re-opening the same epoch replays the receipt without re-mutating.

    Idempotency is a named cutover requirement: a hub retry of ``open_epoch``
    with the identical request must return the same receipt, must not stage a
    second pending principal or re-hold the cohort, and must reject the epoch
    id if the request differs.
    """

    cp = _plane(tmp_path / "mac.db")
    _bootstrap_active(cp, "agent_alpha", tmp_path)
    pending = _issue(cp, "agent_alpha")
    epoch_id = "epoch-idempotent-open"
    candidate_key = "idempotent-candidate-key-" + ("z" * 40)

    def _request() -> list[dict]:
        return [
            _prepare_item(
                pending,
                generation="generation-idempotent",
                baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                candidate_key=candidate_key,
            )
        ]

    opened = cp.fleet_release_epochs.open_epoch(epoch_id, _request())
    assert opened["status"] == "open"

    held = cp.get_agent("agent_alpha")
    staged_after_open = {
        item["id"]: item["state"]
        for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")
    }
    candidate_rows_after_open = cp.store.query_one(
        "SELECT COUNT(*) AS count FROM fleet_release_attestation_candidates WHERE epoch_id = ?",
        (epoch_id,),
    )["count"]

    replay = cp.fleet_release_epochs.open_epoch(epoch_id, _request())
    assert replay == opened

    # A pre-mutation replay must not stage a second principal, re-hold the
    # agent, or duplicate the encrypted attestation candidate.
    reheld = cp.get_agent("agent_alpha")
    assert reheld.dispatch_hold is True
    assert reheld.dispatch_hold_reason == held.dispatch_hold_reason
    assert reheld.dispatch_hold_at == held.dispatch_hold_at
    assert {
        item["id"]: item["state"]
        for item in WorkerCredentialLifecycle(cp.store).list(agent_id="agent_alpha")
    } == staged_after_open
    assert (
        cp.store.query_one(
            "SELECT COUNT(*) AS count FROM fleet_release_attestation_candidates WHERE epoch_id = ?",
            (epoch_id,),
        )["count"]
        == candidate_rows_after_open
    )
    assert (
        cp.store.query_one(
            "SELECT COUNT(*) AS count FROM fleet_release_epoch_agents WHERE epoch_id = ?",
            (epoch_id,),
        )["count"]
        == 1
    )

    # The status projection is the same receipt, and it never leaks the raw
    # symmetric attestation candidate.
    status = cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"])
    assert status == opened
    assert candidate_key not in json.dumps(replay)
    assert candidate_key not in json.dumps(status)

    # Reusing the epoch id for a different request is rejected, not replayed.
    with pytest.raises(ValidationError, match="different request"):
        cp.fleet_release_epochs.open_epoch(
            epoch_id,
            [
                _prepare_item(
                    pending,
                    generation="generation-idempotent-drift",
                    baseline_seen=cp.get_agent("agent_alpha").last_seen_at,
                    candidate_key=candidate_key,
                )
            ],
        )
