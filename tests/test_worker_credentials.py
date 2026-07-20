from __future__ import annotations

import base64
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mac.api import TokenPrincipal, create_app
from mac.deploy_env import read_env_file
from mac.services import ControlPlane
from mac.store import SQLiteStore
from mac.worker_credentials import (
    AUTHENTICATED_PROOF_SCHEMA,
    DESTINATION_VERIFICATION_SCHEMA,
    FLEET_SOURCE_RUNTIME_SCHEMA,
    INSTALL_MANIFEST_SCHEMA,
    MODE_COMPATIBILITY,
    MODE_ENFORCED,
    PACKAGE_CAPABILITY,
    WorkerCredentialError,
    WorkerCredentialLifecycle,
    WorkerCredentialPolicyProvider,
    WorkerCredentialPrincipalProvider,
    apply_kubernetes_secret,
    authenticated_credential_resource,
    build_kubernetes_secret,
    build_readiness_inventory,
    credential_resource_from_env,
    evaluate_worker_actor,
    ensure_fleet_source_runtime,
    install_vm_manifest,
    installation_manifest,
    main,
    package_worker_readiness,
    read_policy_state,
    write_policy_state,
)


def _plane(path: Path) -> ControlPlane:
    cp = ControlPlane(
        SQLiteStore(str(path)),
        secret_key="worker-credential-test-key-with-32-bytes",
    )
    machine = cp.register_machine(
        "worker-host",
        machine_id="machine_worker",
        labels={},
        resources={},
        trusted=True,
    )
    cp.register_agent(
        machine.id,
        "alpha",
        [PACKAGE_CAPABILITY, "python"],
        resources={},
        agent_id="agent_alpha",
    )
    return cp


def _package_issue(
    lifecycle: WorkerCredentialLifecycle,
    agent_id: str = "agent_alpha",
    *,
    environment: str = "vm",
):
    return lifecycle.issue(
        agent_id,
        fleet="test",
        environment=environment,
        expected_source_commit="a" * 40,
        expected_runtime_digest="sha256:runtime-a",
        required_capabilities=[PACKAGE_CAPABILITY, "python"],
        package_capable=True,
    )


def _ready_resources(issue, env_values):
    agent_id = issue.record["agent_id"]
    return {
        "source_state": {
            "schema": "mac.worker_source_state.v1",
            "commit_sha": "a" * 40,
            "dirty": False,
        },
        "worker_credential": credential_resource_from_env(agent_id, env_values),
        "worker_credential_authenticated": authenticated_credential_resource(
            agent_id=agent_id,
            principal_id=issue.record["id"],
            token_fingerprint=issue.record["token_fingerprint"],
            credential_version=issue.worker_version,
        ),
    }


def _observe(cp: ControlPlane, issue, env_values) -> None:
    cp.store.execute(
        "UPDATE agents SET capabilities = ?, resources = ?, running_digest = ?, "
        "status = ?, health_status = ? WHERE id = ?",
        (
            json.dumps([PACKAGE_CAPABILITY, "python"]),
            json.dumps(_ready_resources(issue, env_values)),
            "sha256:runtime-a",
            "idle",
            "healthy",
            issue.record["agent_id"],
        ),
    )


def _activate_vm(cp: ControlPlane, issue, path: Path):
    manifest = installation_manifest(issue)
    receipt = install_vm_manifest(
        manifest,
        path,
        expected_agent_id=issue.record["agent_id"],
    )
    _observe(cp, issue, read_env_file(path))
    WorkerCredentialLifecycle(cp.store).activate(
        issue.record["agent_id"],
        issue.record["id"],
        receipt=receipt,
    )
    return manifest, receipt


def _agents(cp: ControlPlane):
    return [agent.to_dict() for agent in cp.list_agents()]


def test_db_issuance_stores_only_hash_and_projects_exact_agent(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issue = _package_issue(lifecycle)

    row = cp.store.query_one(
        "SELECT * FROM worker_credentials WHERE id = ?", (issue.record["id"],)
    )
    events = cp.store.query_all("SELECT detail FROM worker_credential_events")
    assert row["token_hash"].startswith("sha256:")
    assert issue.token not in json.dumps(dict(row))
    assert all(issue.token not in str(event["detail"]) for event in events)

    projected = WorkerCredentialPrincipalProvider(cp.store).tokens()
    assert projected[row["token_hash"]] == {
        "scopes": ["agent", "dispatch", "read", "write"],
        "client_id": issue.record["id"],
        "agent_id": "agent_alpha",
        "principal_kind": "worker",
        "credential_fingerprint": issue.record["token_fingerprint"],
        "worker_credential_version": 1,
        "worker_credential_state": "pending_install",
    }
    with pytest.raises(WorkerCredentialError, match="at least 60 seconds"):
        lifecycle.issue("agent_alpha", environment="vm", expires_in=59)


def test_recovery_discards_only_exact_unreserved_pending_issuance(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    lifecycle = WorkerCredentialLifecycle(cp.store)
    active = _package_issue(lifecycle)
    _activate_vm(cp, active, tmp_path / "active.env")
    orphan = lifecycle.issue(
        "agent_alpha",
        fleet="test",
        environment="vm",
        actor="fleet-release:epoch-a",
    )
    unrelated = lifecycle.issue(
        "agent_alpha",
        fleet="test",
        environment="vm",
        actor="fleet-release:epoch-b",
    )

    discarded = lifecycle.discard_unreserved_pending(
        "agent_alpha", created_by="fleet-release:epoch-a"
    )

    assert [item["id"] for item in discarded] == [orphan.record["id"]]
    states = {item["id"]: item["state"] for item in lifecycle.list(agent_id="agent_alpha")}
    assert states[active.record["id"]] == "active"
    assert states[orphan.record["id"]] == "revoked"
    assert states[unrelated.record["id"]] == "pending_install"
    assert (
        lifecycle.discard_unreserved_pending(
            "agent_alpha", created_by="fleet-release:epoch-a"
        )
        == []
    )


def test_deleted_agent_rejects_issued_token_issue_and_activation(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issue = _package_issue(lifecycle)
    receipt = install_vm_manifest(
        installation_manifest(issue),
        tmp_path / "mac.env",
        expected_agent_id="agent_alpha",
    )
    app = create_app(
        control_plane=cp,
        auth_tokens={"shared-admin": {"scopes": ["admin"]}},
    )
    cp.delete_agent("agent_alpha", actor="operator")

    with TestClient(app) as client:
        rejected = client.post(
            "/agents",
            headers={"Authorization": "Bearer " + issue.token},
            json={
                "machine_id": "machine_worker",
                "name": "alpha",
                "capabilities": [PACKAGE_CAPABILITY, "python"],
                "agent_id": "agent_alpha",
            },
        )
        with pytest.raises(WorkerCredentialError, match="registered agent"):
            _package_issue(lifecycle)
        cp.register_agent(
            "machine_worker",
            "alpha",
            [PACKAGE_CAPABILITY, "python"],
            resources={},
            agent_id="agent_alpha",
            allow_resurrection=True,
        )
        replayed_after_resurrection = client.post(
            "/agents",
            headers={"Authorization": "Bearer " + issue.token},
            json={
                "machine_id": "machine_worker",
                "name": "alpha",
                "capabilities": [PACKAGE_CAPABILITY, "python"],
                "agent_id": "agent_alpha",
            },
        )
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "unknown bearer token"
    assert replayed_after_resurrection.status_code == 403
    assert replayed_after_resurrection.json()["detail"] == "unknown bearer token"
    assert cp.get_agent("agent_alpha").deleted_at is None
    assert lifecycle.list(agent_id="agent_alpha")[0]["state"] == "revoked"
    events = cp.store.query_all(
        "SELECT event_type, detail FROM worker_credential_events "
        "WHERE principal_id = ? ORDER BY created_at",
        (issue.record["id"],),
    )
    assert [event["event_type"] for event in events] == [
        "worker_credential.issued",
        "worker_credential.revoked",
    ]
    assert all(issue.token not in str(event["detail"]) for event in events)

    fresh = _package_issue(lifecycle)
    assert fresh.worker_version == issue.worker_version + 1
    with pytest.raises(WorkerCredentialError, match="revoked or expired"):
        lifecycle.activate("agent_alpha", issue.record["id"], receipt=receipt)


def test_activation_requires_destination_readback_and_live_authenticated_heartbeat(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issue = _package_issue(lifecycle)
    manifest = installation_manifest(issue)
    receipt = install_vm_manifest(
        manifest, tmp_path / "mac.env", expected_agent_id="agent_alpha"
    )
    assert receipt["destination_verification"]["schema"] == DESTINATION_VERIFICATION_SCHEMA

    with pytest.raises(WorkerCredentialError, match="authenticated heartbeat"):
        lifecycle.activate("agent_alpha", issue.record["id"], receipt=receipt)

    forged = json.loads(json.dumps(receipt))
    forged["destination_verification"]["verified"] = False
    _observe(cp, issue, read_env_file(tmp_path / "mac.env"))
    with pytest.raises(WorkerCredentialError, match="destination readback"):
        lifecycle.activate("agent_alpha", issue.record["id"], receipt=forged)

    lifecycle.activate("agent_alpha", issue.record["id"], receipt=receipt)
    assert lifecycle.list(agent_id="agent_alpha")[0]["state"] == "active"


def test_activation_binds_receipt_to_exact_issued_environment(tmp_path: Path) -> None:
    cp = _plane(tmp_path / "mac.db")
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issue = _package_issue(lifecycle)
    receipt = install_vm_manifest(
        installation_manifest(issue),
        tmp_path / "mac.env",
        expected_agent_id="agent_alpha",
    )
    _observe(cp, issue, read_env_file(tmp_path / "mac.env"))

    mismatched = json.loads(json.dumps(receipt))
    mismatched["destination"] = "k8s_secret:mac/worker"
    mismatched["destination_verification"]["destination"] = mismatched["destination"]
    with pytest.raises(WorkerCredentialError, match="credential environment"):
        lifecycle.activate("agent_alpha", issue.record["id"], receipt=mismatched)

    inconsistent = json.loads(json.dumps(receipt))
    inconsistent["destination"] = "vm_env:other"
    with pytest.raises(WorkerCredentialError, match="destination verification"):
        lifecycle.activate("agent_alpha", issue.record["id"], receipt=inconsistent)

    wrong_version = json.loads(json.dumps(receipt))
    wrong_version["worker_credential_version"] = 99
    wrong_version["destination_verification"]["worker_credential_version"] = 99
    with pytest.raises(WorkerCredentialError, match="version.*issuance"):
        lifecycle.activate("agent_alpha", issue.record["id"], receipt=wrong_version)


def test_rotation_overlaps_until_verified_activation_then_revokes_old(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    lifecycle = WorkerCredentialLifecycle(cp.store)
    first = _package_issue(lifecycle)
    _activate_vm(cp, first, tmp_path / "first.env")

    second = _package_issue(lifecycle)
    during = WorkerCredentialPrincipalProvider(cp.store).tokens()
    assert first.record["token_hash"] in during
    assert second.record["token_hash"] in during

    _activate_vm(cp, second, tmp_path / "second.env")
    after = WorkerCredentialPrincipalProvider(cp.store).tokens()
    assert first.record["token_hash"] not in after
    assert second.record["token_hash"] in after
    states = {row["credential_version"]: row["state"] for row in lifecycle.list()}
    assert states == {1: "superseded", 2: "active"}


def test_vm_install_is_private_does_not_chmod_existing_parent_and_handles_bad_version(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    issue = _package_issue(WorkerCredentialLifecycle(cp.store))
    manifest = installation_manifest(issue)
    assert manifest["schema"] == INSTALL_MANIFEST_SCHEMA

    parent = tmp_path / "existing"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    env_path = parent / "mac.env"
    receipt = install_vm_manifest(manifest, env_path, expected_agent_id="agent_alpha")
    values = read_env_file(env_path)
    assert values["MAC_WORKER_TOKEN"] == issue.token
    assert values["MAC_WORKER_RUNNING_DIGEST"] == "sha256:runtime-a"
    assert issue.token not in json.dumps(receipt)
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert parent.stat().st_mode & 0o777 == 0o755

    bad = dict(values)
    bad["MAC_WORKER_CREDENTIAL_VERSION"] = "not-an-integer"
    proof = credential_resource_from_env("agent_alpha", bad)
    assert proof["mode"] == "invalid_credential_version"


def test_vm_install_removes_shared_hub_bearer_aliases_without_touching_upstream_key(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    issue = _package_issue(WorkerCredentialLifecycle(cp.store))
    env_path = tmp_path / "mac.env"
    shared = "shared-hub-bootstrap-token"
    env_path.write_text(
        "\n".join(
            (
                "MAC_WORKER_TOKEN=%s" % shared,
                "OPENAI_API_KEY=%s" % shared,
                "MAC_HERMES_GATEWAY_API_KEY=%s" % shared,
                "ACC_HERMES_GATEWAY_API_KEY=%s" % shared,
                "NVIDIA_API_KEY=real-upstream-provider-key",
                "",
            )
        ),
        encoding="utf-8",
    )

    install_vm_manifest(
        installation_manifest(issue), env_path, expected_agent_id="agent_alpha"
    )
    values = read_env_file(env_path)
    assert values["MAC_WORKER_TOKEN"] == issue.token
    assert values["OPENAI_API_KEY"] == issue.token
    assert values["MAC_HERMES_GATEWAY_API_KEY"] == issue.token
    assert values["ACC_HERMES_GATEWAY_API_KEY"] == issue.token
    assert values["NVIDIA_API_KEY"] == "real-upstream-provider-key"
    assert shared not in env_path.read_text(encoding="utf-8")


def test_kubernetes_apply_verifies_secret_readback_without_token_in_argv(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issue = _package_issue(lifecycle, environment="k8s")
    manifest = installation_manifest(issue)
    secret = build_kubernetes_secret(manifest)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ["kubectl", "apply"]:
            return SimpleNamespace(returncode=0, stdout="configured", stderr="")
        data = {
            key: base64.b64encode(value.encode()).decode()
            for key, value in secret["stringData"].items()
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": data}), stderr="")

    receipt = apply_kubernetes_secret(manifest, runner=run)
    assert calls[0][0] == ["kubectl", "apply", "-f", "-"]
    assert calls[1][0][:4] == ["kubectl", "get", "secret", secret["metadata"]["name"]]
    assert issue.token not in json.dumps(calls[0][0])
    assert issue.token in calls[0][1]["input"]
    assert issue.token not in json.dumps(receipt)
    assert receipt["destination_verification"]["method"] == "k8s_secret_readback"
    _observe(cp, issue, secret["stringData"])
    lifecycle.activate("agent_alpha", issue.record["id"], receipt=receipt)
    assert lifecycle.list(agent_id="agent_alpha")[0]["state"] == "active"

    vm_issue = _package_issue(lifecycle)
    with pytest.raises(WorkerCredentialError, match="different environment"):
        build_kubernetes_secret(installation_manifest(vm_issue))


def test_inventory_and_package_membership_require_live_authenticated_exact_state(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issue = _package_issue(lifecycle)
    _activate_vm(cp, issue, tmp_path / "mac.env")
    inventory = build_readiness_inventory(_agents(cp), lifecycle.records())
    assert inventory["all_ready"] is True
    assert inventory["workers"][0]["credential_bound"] is True
    # Readiness is worker eligibility, not the task's publication lane.  The
    # same ready worker may execute managed or grandfathered legacy work.
    assert inventory["workers"][0]["managed_lane_eligible"] is True
    assert inventory["workers"][0]["external_certifier_capable"] is True
    assert "publication_lane" not in inventory["workers"][0]

    # Activation alone is insufficient: reviewed membership is a durable,
    # replica-shared control-plane decision.
    assert package_worker_readiness(cp.store, "agent_alpha")["ready"] is False
    write_policy_state(MODE_COMPATIBILITY, inventory=inventory, store=cp.store)
    assert package_worker_readiness(cp.store, "agent_alpha")["ready"] is True

    resources = cp.get_agent("agent_alpha").resources
    resources.pop("worker_credential_authenticated")
    cp.store.execute(
        "UPDATE agents SET resources = ? WHERE id = ?",
        (json.dumps(resources), "agent_alpha"),
    )
    assert package_worker_readiness(cp.store, "agent_alpha")["ready"] is False
    degraded = build_readiness_inventory(_agents(cp), lifecycle.records())
    assert "authenticated_credential_not_observed" in degraded["workers"][0]["blockers"]
    assert degraded["workers"][0]["managed_lane_eligible"] is False
    assert degraded["workers"][0]["external_certifier_capable"] is False


def test_enforcement_policy_is_shared_across_replicas_and_refuses_partial(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mac.db"
    cp = _plane(db)
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issue = _package_issue(lifecycle)
    _activate_vm(cp, issue, tmp_path / "mac.env")
    inventory = build_readiness_inventory(_agents(cp), lifecycle.records())

    not_ready = json.loads(json.dumps(inventory))
    not_ready["all_ready"] = False
    not_ready["readiness_percent"] = 0.0
    with pytest.raises(WorkerCredentialError, match="stale|not 100% ready"):
        write_policy_state(MODE_ENFORCED, inventory=not_ready, store=cp.store)

    peer_store = SQLiteStore(str(db), initialize_schema=False)
    first = WorkerCredentialPolicyProvider(cp.store)
    second = WorkerCredentialPolicyProvider(peer_store)
    assert first.mode == second.mode == MODE_COMPATIBILITY
    policy = write_policy_state(MODE_ENFORCED, inventory=inventory, store=cp.store)
    assert first.mode == second.mode == MODE_ENFORCED
    assert read_policy_state(store=peer_store)["revision"] == policy["revision"]
    assert issue.token not in json.dumps(policy)


def test_actor_policy_requires_binding_and_current_package_readiness() -> None:
    legacy_ordinary = evaluate_worker_actor(
        mode=MODE_COMPATIBILITY,
        principal_agent_id=None,
        claimed_agent_id="agent_legacy",
        package_linked=False,
    )
    legacy_package = evaluate_worker_actor(
        mode=MODE_COMPATIBILITY,
        principal_agent_id=None,
        claimed_agent_id="agent_legacy",
        package_linked=True,
    )
    bound_not_ready = evaluate_worker_actor(
        mode=MODE_COMPATIBILITY,
        principal_agent_id="agent_alpha",
        claimed_agent_id="agent_alpha",
        package_linked=True,
        package_ready=False,
    )
    bound_ready = evaluate_worker_actor(
        mode=MODE_ENFORCED,
        principal_agent_id="agent_alpha",
        claimed_agent_id="agent_alpha",
        package_linked=True,
        package_ready=True,
    )
    assert legacy_ordinary.allowed and legacy_ordinary.legacy
    assert not legacy_package.allowed
    assert not bound_not_ready.allowed
    assert bound_ready.allowed

    principal = TokenPrincipal(
        scopes=frozenset({"agent"}),
        agent_id="agent_alpha",
        worker_identity_mode=MODE_ENFORCED,
    )
    with pytest.raises(Exception, match="readiness membership"):
        principal.assert_actor(
            "agent_alpha", package_linked=True, package_ready=False
        )


def test_cli_activation_consumes_one_time_manifest_only_after_success(
    tmp_path: Path, capsys
) -> None:
    db = tmp_path / "mac.db"
    cp = _plane(db)
    manifest_path = tmp_path / "manifest.json"
    receipt_path = tmp_path / "receipt.json"
    env_path = tmp_path / "mac.env"
    assert main(
        [
            "--db",
            str(db),
            "issue",
            "--agent-id",
            "agent_alpha",
            "--environment",
            "vm",
            "--expected-source-commit",
            "a" * 40,
            "--expected-runtime-digest",
            "sha256:runtime-a",
            "--capability",
            PACKAGE_CAPABILITY,
            "--capability",
            "python",
            "--package-capable",
            "--manifest-out",
            str(manifest_path),
        ]
    ) == 0
    manifest = json.loads(manifest_path.read_text())
    assert main(
        [
            "install-vm",
            "--manifest",
            str(manifest_path),
            "--agent-id",
            "agent_alpha",
            "--env-file",
            str(env_path),
            "--receipt-out",
            str(receipt_path),
        ]
    ) == 0
    issue = SimpleNamespace(
        record={
            "id": manifest["principal_id"],
            "agent_id": "agent_alpha",
            "token_fingerprint": manifest["token_fingerprint"],
        },
        worker_version=manifest["worker_credential_version"],
    )
    activation_args = [
        "--db",
        str(db),
        "activate",
        "--agent-id",
        "agent_alpha",
        "--principal-id",
        manifest["principal_id"],
        "--receipt",
        str(receipt_path),
        "--manifest",
        str(manifest_path),
    ]
    # A failed activation retains the hub-side retry authority. Only the
    # successful, heartbeat-proved activation below consumes it.
    assert main(activation_args) == 1
    assert manifest_path.exists()
    _observe(cp, issue, read_env_file(env_path))
    assert main(activation_args) == 0
    assert not manifest_path.exists()
    assert main(
        [
            "--db",
            str(db),
            "set-mode",
            MODE_COMPATIBILITY,
            "--review-live",
        ]
    ) == 0
    assert read_policy_state(store=cp.store)["ready_agent_ids"] == ["agent_alpha"]
    output = capsys.readouterr().out
    assert manifest["credential"]["token"] not in output


def test_authenticated_proof_schema_is_secret_free() -> None:
    proof = authenticated_credential_resource(
        agent_id="agent_alpha",
        principal_id="worker-abc-v0001",
        token_fingerprint="0123456789ab",
        credential_version=1,
    )
    assert proof["schema"] == AUTHENTICATED_PROOF_SCHEMA
    assert "mac_worker_" not in json.dumps(proof)
    assert set(proof) == {
        "schema",
        "agent_id",
        "principal_id",
        "worker_credential_version",
        "token_fingerprint",
        "authenticated_at",
    }
    assert credential_resource_from_env("agent_alpha", {}) == {}


def test_fleet_deploy_completes_bound_vm_credential_rollout() -> None:
    root = Path(__file__).resolve().parents[1]
    script_path = root / "deploy" / "deploy-mac-fleet.sh"
    script = script_path.read_text(encoding="utf-8")
    syntax = subprocess.run(
        ["bash", "-n", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr
    rollout = script.split("provision_bound_worker_credential() (", 1)[1].split(
        "enforce_bound_worker_credentials() {", 1
    )[0]
    assert "mac.worker_credentials ensure-runtime" in rollout
    assert "mac.worker_credentials issue" in rollout
    assert "mac.worker_credentials install-vm" in rollout
    assert "mac.worker_credentials activate" in rollout
    assert "set-mode compatibility --review-live" in rollout
    assert "chmod 0600" in rollout
    assert "trap cleanup_worker_relay EXIT" in rollout
    assert "--manifest-out" in rollout
    assert "--package-capable" in rollout
    assert "MAC_WORKER_TOKEN" not in rollout
    assert "mac-source:" not in rollout
    assert rollout.index("mac.worker_credentials ensure-runtime") < rollout.index(
        "mac.worker_credentials issue"
    )

    main_body = script.split("main() {", 1)[1]
    typed = script.split("run_typed_cohort() {", 1)[1].split(
        "\n}\n\nmain()", 1
    )[0]
    hub_open = script.split("build_and_open_hub_epoch() {", 1)[1].split(
        "\n}\n\nprove_and_commit_hub_epoch", 1
    )[0]
    apply_worker = script.split("typed_phase2_apply_worker() {", 1)[1].split(
        "\n}\n\ntyped_finalize_worker", 1
    )[0]
    assert "provision_bound_worker_credential" not in main_body
    assert "enforce_bound_worker_credentials" not in main_body
    assert "issue_pending_worker_credential" in hub_open
    apply_phase = 'run_bounded_node_phase "$selected_specs_file" phase2-apply'
    assert "install_pending_worker_credential" in apply_worker
    assert typed.index("build_and_open_hub_epoch") < typed.index(apply_phase)
    assert typed.index(apply_phase) < typed.index("prove_and_commit_hub_epoch")
    assert "set-mode enforced --review-live" in script
    assert 'add_remote_secret_env MAC_DEPLOY_HUB_TOKEN "$hub_token"' in script
    assert 'add_remote_env MAC_DEPLOY_HUB_TOKEN "$hub_token"' not in script


def test_fleet_source_runtime_registration_is_idempotent_and_fail_closed(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    source_commit = "b" * 40

    first = ensure_fleet_source_runtime(cp.store, source_commit)
    second = ensure_fleet_source_runtime(cp.store, source_commit)
    normal_runtime = cp.create_runtime(
        "fleet-source-digest-parity",
        {
            "schema": FLEET_SOURCE_RUNTIME_SCHEMA,
            "source_commit": source_commit,
            "kind": "mac-fleet-source",
            "provisioner_contract": "mac-fleet-deploy-v1",
        },
        "test",
    )

    assert first["schema"] == FLEET_SOURCE_RUNTIME_SCHEMA
    assert first["status"] == "ready"
    assert first["created"] is True
    assert second == {**first, "created": False}
    assert len(first["runtime_digest"]) == 64
    assert first["runtime_digest"] == normal_runtime.digest
    assert cp.store.query_one(
        "SELECT COUNT(*) AS count FROM runtime_environments WHERE name = ?",
        (first["runtime_name"],),
    )["count"] == 1
    row = cp.store.query_one(
        "SELECT manifest, digest FROM runtime_environments WHERE id = ?",
        (first["runtime_id"],),
    )
    assert json.loads(row["manifest"]) == {
        "schema": FLEET_SOURCE_RUNTIME_SCHEMA,
        "source_commit": source_commit,
        "kind": "mac-fleet-source",
        "provisioner_contract": "mac-fleet-deploy-v1",
    }
    assert row["digest"] == first["runtime_digest"]

    cp.store.execute(
        "UPDATE runtime_environments SET digest = ? WHERE id = ?",
        ("0" * 64, first["runtime_id"]),
    )
    with pytest.raises(WorkerCredentialError, match="different identity"):
        ensure_fleet_source_runtime(cp.store, source_commit)
    cp.store.execute(
        "UPDATE runtime_environments SET digest = ? WHERE id = ?",
        (first["runtime_digest"], first["runtime_id"]),
    )
    cp.store.execute(
        "UPDATE runtime_environments SET manifest = ? WHERE id = ?",
        (json.dumps({"schema": "conflict"}), first["runtime_id"]),
    )
    with pytest.raises(WorkerCredentialError, match="different identity"):
        ensure_fleet_source_runtime(cp.store, source_commit)


def test_fleet_source_runtime_concurrent_replays_create_one_row(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mac.db"
    cp = _plane(db)
    source_commit = "d" * 40
    barrier = threading.Barrier(8)
    results = []
    errors = []
    result_lock = threading.Lock()

    def register() -> None:
        store = SQLiteStore(str(db), initialize_schema=False)
        try:
            barrier.wait(timeout=10)
            result = ensure_fleet_source_runtime(store, source_commit)
            with result_lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=register) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert not errors
    assert len(results) == 8
    assert [result["created"] for result in results].count(True) == 1
    assert len({result["runtime_id"] for result in results}) == 1
    assert len({result["runtime_digest"] for result in results}) == 1
    assert cp.store.query_one(
        "SELECT COUNT(*) AS count FROM runtime_environments WHERE name = ?",
        (results[0]["runtime_name"],),
    )["count"] == 1


@pytest.mark.parametrize("source_commit", ["", "A" * 40, "a" * 39, "g" * 40])
def test_fleet_source_runtime_rejects_noncanonical_commit(
    tmp_path: Path,
    source_commit: str,
) -> None:
    cp = _plane(tmp_path / ("invalid-%s.db" % (len(source_commit) or 0)))
    with pytest.raises(WorkerCredentialError, match="lowercase 40-character"):
        ensure_fleet_source_runtime(cp.store, source_commit)


def test_ensure_runtime_cli_emits_only_registered_runtime_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "mac.db"
    _plane(db).store.close()
    source_commit = "c" * 40

    assert main(
        [
            "--db",
            str(db),
            "ensure-runtime",
            "--source-commit",
            source_commit,
            "--created-by",
            "test-deploy",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == FLEET_SOURCE_RUNTIME_SCHEMA
    assert payload["status"] == "ready"
    assert payload["source_commit"] == source_commit
    assert set(payload) == {
        "schema",
        "status",
        "source_commit",
        "runtime_id",
        "runtime_name",
        "runtime_digest",
        "created",
    }


def test_api_authenticates_db_worker_heartbeat_and_enforcement_blocks_shared_actor(
    tmp_path: Path,
) -> None:
    cp = _plane(tmp_path / "mac.db")
    lifecycle = WorkerCredentialLifecycle(cp.store)
    runtime = ensure_fleet_source_runtime(cp.store, "a" * 40)
    issue = lifecycle.issue(
        "agent_alpha",
        fleet="test",
        environment="vm",
        expected_source_commit="a" * 40,
        expected_runtime_digest=runtime["runtime_digest"],
        required_capabilities=[PACKAGE_CAPABILITY, "python"],
        package_capable=True,
    )
    manifest = installation_manifest(issue)
    receipt = install_vm_manifest(
        manifest, tmp_path / "mac.env", expected_agent_id="agent_alpha"
    )
    values = read_env_file(tmp_path / "mac.env")
    app = create_app(
        control_plane=cp,
        auth_tokens={"shared-admin": {"scopes": ["admin"]}},
    )
    with TestClient(app) as client:
        heartbeat = client.post(
            "/agents/agent_alpha/heartbeat",
            headers={"Authorization": "Bearer " + issue.token},
            json={
                "status": "idle",
                "running_digest": runtime["runtime_digest"],
                "resources": {
                    "source_state": {
                        "commit_sha": "a" * 40,
                        "dirty": False,
                    },
                    "worker_credential": credential_resource_from_env(
                        "agent_alpha", values
                    ),
                },
            },
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["running_digest"] == runtime["runtime_digest"]
        authenticated = heartbeat.json()["resources"][
            "worker_credential_authenticated"
        ]
        assert authenticated["principal_id"] == issue.record["id"]

        lifecycle.activate("agent_alpha", issue.record["id"], receipt=receipt)
        inventory = build_readiness_inventory(_agents(cp), lifecycle.records())
        write_policy_state(MODE_ENFORCED, inventory=inventory, store=cp.store)
        task = cp.create_task("enforced actor boundary")
        shared = client.post(
            "/tasks/%s/claim" % task.id,
            headers={"Authorization": "Bearer shared-admin"},
            params={"agent_id": "agent_alpha"},
        )
        bound = client.post(
            "/tasks/%s/claim" % task.id,
            headers={"Authorization": "Bearer " + issue.token},
            params={"agent_id": "agent_alpha"},
        )
    assert shared.status_code == 403
    assert bound.status_code == 200
