from __future__ import annotations

import hashlib
import json
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
    read_only_report_repository_executor_attestation,
    utcnow,
)
from mac.services import ControlPlane, sign_verification_manifest
from mac.store import SQLiteStore
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
    cp = ControlPlane(SQLiteStore(str(path)), secret_key=SECRET_KEY)
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
    WorkerCredentialLifecycle(cp.store).activate(
        agent_id, issue.record["id"], receipt=receipt
    )
    return issue


def _report_attestation() -> dict:
    digest = "sha256:" + ("b" * 64)
    return read_only_report_repository_executor_attestation(
        runtime_image_ref=(
            "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + ("c" * 64)
        ),
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
) -> dict:
    return {
        "agent_id": pending.record["agent_id"],
        "expected_dispatch_hold": expected_dispatch_hold,
        "expected_hold_reason": expected_hold_reason,
        "expected_hold_at": expected_hold_at,
        "generation": generation,
        "baseline_seen": baseline_seen,
        "principal_id": pending.record["id"],
        "attestation_candidate": (
            {"key": candidate_key} if candidate_key is not None else None
        ),
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
    assert cp.get_agent("agent_alpha").dispatch_hold_reason.startswith(
        "mac:fleet-release:"
    )
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
        extra_resources=_report_resources(
            "agent_alpha", report_attestation, report_timestamp
        ),
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
    assert cp._agent_attestation_key("agent_alpha") == old_attestation_key
    assert (
        cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof])
        == proved
    )

    committed = cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
    assert committed["status"] == "committed"
    assert (
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"]) == committed
    )
    assert (
        cp.fleet_release_epochs.prove(epoch_id, opened["identity_sha256"], [proof])
        == committed
    )
    changed_proof = json.loads(json.dumps(proof))
    changed_proof["install_receipt"]["installed_at"] = "2100-01-03T00:00:00+00:00"
    with pytest.raises(ValidationError, match="different evidence"):
        cp.fleet_release_epochs.prove(
            epoch_id, opened["identity_sha256"], [changed_proof]
        )
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
    assert (
        cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"]) == committed
    )
    assert (
        cp.fleet_release_epochs.status(epoch_id, "sha256:" + ("f" * 64))["status"]
        == "mismatch"
    )
    cp.store.execute(
        "DELETE FROM agent_lifecycle_events WHERE id = ?",
        (cp.fleet_release_epochs._marker_id(epoch_id),),
    )
    assert (
        cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"])["status"]
        == "mismatch"
    )
    with pytest.raises(TransitionError, match="marker is incomplete"):
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])


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
        cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"])["status"]
        == "mismatch"
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
        cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"])["status"]
        == "mismatch"
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
                    expected_hold_reason=cp.get_agent(
                        "agent_alpha"
                    ).dispatch_hold_reason,
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

    aborted = cp.fleet_release_epochs.abort(
        epoch_id,
        opened["identity_sha256"],
        reason="node apply was rolled back",
    )
    assert aborted["status"] == "aborted"
    assert (
        cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"]) == aborted
    )
    assert (
        cp.fleet_release_epochs.abort(
            epoch_id,
            opened["identity_sha256"],
            reason="node apply was rolled back",
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


def test_abort_accepts_prior_unheld_snapshot_already_restored_exactly(
    tmp_path: Path,
) -> None:
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
    assert restored.dispatch_hold is False
    assert restored.dispatch_hold_reason is None
    assert restored.dispatch_hold_at is None
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
    superseding = cp.set_agent_dispatch_hold(
        "agent_beta", "operator superseded epoch"
    )
    with pytest.raises(ValidationError, match="epoch-owned hold"):
        cp.fleet_release_epochs.commit("epoch-two-agent", opened["identity_sha256"])
    for name in ("alpha", "beta"):
        states = {
            item["id"]: item["state"]
            for item in WorkerCredentialLifecycle(cp.store).list(
                agent_id="agent_%s" % name
            )
        }
        assert states == {
            old[name].record["id"]: "active",
            pending[name].record["id"]: "pending_install",
        }
        assert cp._agent_attestation_key("agent_%s" % name) != candidates[name]
    aborted = cp.fleet_release_epochs.abort(
        "epoch-two-agent",
        opened["identity_sha256"],
        reason="preserve later operator safety hold",
    )
    assert aborted["status"] == "aborted"
    alpha = cp.get_agent("agent_alpha")
    assert alpha.dispatch_hold is False
    beta = cp.get_agent("agent_beta")
    assert beta.dispatch_hold is True
    assert beta.dispatch_hold_reason == "operator superseded epoch"
    assert beta.dispatch_hold_at == superseding.dispatch_hold_at
    for name in ("alpha", "beta"):
        states = {
            item["id"]: item["state"]
            for item in WorkerCredentialLifecycle(cp.store).list(
                agent_id="agent_%s" % name
            )
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
        cp.fleet_release_epochs.prove(
            epoch_id, opened["identity_sha256"], [secret_bearing]
        )
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
    assert (
        REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY in cp.get_agent("agent_alpha").resources
    )
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
    drifted_resources[REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY] = {
        "concurrent": "replacement"
    }
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
    peer = ControlPlane(SQLiteStore(str(path)), secret_key=SECRET_KEY)
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[Exception] = []
    result_lock = threading.Lock()

    def finish(action: str, plane: ControlPlane) -> None:
        try:
            barrier.wait(timeout=10)
            if action == "commit":
                result = plane.fleet_release_epochs.commit(
                    epoch_id, opened["identity_sha256"]
                )
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
    assert (
        cp.fleet_release_epochs.status(epoch_id, opened["identity_sha256"])["status"]
        == winner
    )
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
        "UPDATE worker_credentials SET state = 'pending_install', revoked_at = NULL "
        "WHERE id = ?",
        (pending.record["id"],),
    )
    write_policy_state(MODE_COMPATIBILITY, store=cp.store, actor="concurrent")
    with pytest.raises(ValidationError, match="policy changed"):
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])
    cp.store.execute(
        "DELETE FROM worker_credential_policy_state WHERE singleton_key = 'fleet'"
    )
    assert (
        cp.fleet_release_epochs.commit(epoch_id, opened["identity_sha256"])["status"]
        == "committed"
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
    elif drift == "health":
        cp.store.execute(
            "UPDATE agents SET health_status = 'degraded' WHERE id = 'agent_alpha'"
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


def test_hub_authority_uuid_is_durable_and_status_exposes_it(tmp_path: Path) -> None:
    path = tmp_path / "mac.db"
    cp = _plane(path)
    authority_id = cp.fleet_release_epochs.hub_authority_id
    absent = cp.fleet_release_epochs.status("absent-epoch", "sha256:" + ("0" * 64))
    assert absent["hub_authority_id"] == authority_id
    cp.store.close()
    restarted = ControlPlane(SQLiteStore(str(path)), secret_key=SECRET_KEY)
    assert restarted.fleet_release_epochs.hub_authority_id == authority_id
    assert (
        restarted.store.query_one(
            "SELECT COUNT(*) AS count FROM hub_authority_identity"
        )["count"]
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
