from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "fleet-cohort-transaction.py"
MATERIAL_SCRIPT = ROOT / "deploy" / "fleet-release-epoch-material.py"
MATERIAL_SPEC = importlib.util.spec_from_file_location(
    "mac_fleet_release_epoch_material", MATERIAL_SCRIPT
)
assert MATERIAL_SPEC is not None and MATERIAL_SPEC.loader is not None
EPOCH_MATERIAL = importlib.util.module_from_spec(MATERIAL_SPEC)
MATERIAL_SPEC.loader.exec_module(EPOCH_MATERIAL)
SOURCE_COMMIT = "a" * 40
EPOCH = f"{SOURCE_COMMIT}:20260719:controller"
RELEASE_EPOCH = f"{EPOCH}:{'b' * 64}"
OWNER_NONCE = "controller-nonce"
RESTORE_DIGEST = "c" * 64
ROLLBACK_DIGEST = "d" * 64
FINALIZER_DIGEST = "9" * 64
HUB_AUTHORITY_ID = "123e4567-e89b-12d3-a456-426614174000"
HUB_IDENTITY_SHA256 = "sha256:" + "e" * 64
HUB_PROOF_SHA256 = "sha256:" + "f" * 64


@pytest.fixture
def journal_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "mac_fleet_cohort_transaction", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_cli(
    directory: Path,
    *args: str,
    check: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--directory", str(directory), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    stream = result.stdout if result.returncode == 0 else result.stderr
    payload = json.loads(stream)
    if check:
        assert result.returncode == 0, payload
        assert payload["ok"] is True
    return result, payload


def write_json(path: Path, value: Any, *, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def cohort(count: int = 1) -> list[dict[str, str]]:
    return [
        {
            "name": f"node-{index}",
            "stable_id": f"agent_{index}",
            "generation": f"generation-{index}",
            "deployment_id": f"deployment-{index}",
            "os": "linux",
            "supervisor": "systemd",
        }
        for index in range(count)
    ]


def ssh_identity(
    label: str,
    *,
    hub: bool = False,
    store_uuid: str = HUB_AUTHORITY_ID,
) -> dict[str, Any]:
    authority = {
        "ssh_host_key_sha256": digest(f"{label}:host-key"),
        "instance_id_kind": "linux-machine-id",
        "instance_id_sha256": digest(f"{label}:machine-id"),
    }
    adapter = "ssh-machine"
    if hub:
        adapter = "ssh-hub"
        authority["durable_store_uuid_sha256"] = digest(store_uuid.lower())
    return {
        "schema": "mac.fleet_endpoint_identity.v1",
        "adapter": adapter,
        "authority": authority,
        "observation": {},
    }


def k8s_identity(label: str) -> dict[str, Any]:
    return {
        "schema": "mac.fleet_endpoint_identity.v1",
        "adapter": "kubernetes-workload",
        "authority": {
            "cluster_uid_sha256": digest(f"{label}:cluster"),
            "workload_kind": "statefulset",
            "workload_uid_sha256": digest(f"{label}:workload"),
        },
        "observation": {"pod_uid_sha256": digest(f"{label}:pod")},
    }


def operation(
    command: str,
    revision: int,
    operation_id: str,
    *,
    epoch: str = EPOCH,
    owner_nonce: str = OWNER_NONCE,
) -> list[str]:
    return [
        command,
        "--epoch",
        epoch,
        "--expected-revision",
        str(revision),
        "--operation-id",
        operation_id,
        "--owner-nonce",
        owner_nonce,
    ]


def node_args(node: dict[str, str]) -> list[str]:
    return [
        "--agent-name",
        node["name"],
        "--stable-id",
        node["stable_id"],
        "--generation",
        node["generation"],
    ]


class Scenario:
    def __init__(
        self,
        tmp_path: Path,
        *,
        node_count: int = 1,
        owner_nonce: str = OWNER_NONCE,
        owner_pid: int | None = None,
        successor_hold: str = "synchronized successor hold",
        desired_worker_credential_mode: str | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.directory = tmp_path / "journal"
        self.nodes = cohort(node_count)
        self.owner_nonce = owner_nonce
        self.desired_worker_credential_mode = desired_worker_credential_mode
        self.revision = 0
        self.sequence = 0
        self.hub_identity = ssh_identity("hub", hub=True)
        self.node_identities = {
            node["name"]: ssh_identity(node["name"]) for node in self.nodes
        }
        cohort_file = write_json(tmp_path / "cohort.json", self.nodes)
        _result, payload = run_cli(
            self.directory,
            "init",
            "--epoch",
            EPOCH,
            "--source-commit",
            SOURCE_COMMIT,
            "--deploy-ts",
            "20260719T054500Z",
            "--fleet",
            "rocky",
            "--hub-agent",
            "rocky",
            "--cohort-file",
            str(cohort_file),
            "--successor-hold",
            successor_hold,
            "--require-release-all-selected",
            "--owner-nonce",
            owner_nonce,
            "--owner-pid",
            str(owner_pid or os.getpid()),
        )
        self.journal = payload["journal"]

    def evidence(self, label: str, *, secret: str | None = None) -> Path:
        value = {"schema": "mac.test_evidence.v1", "result": "passed"}
        if secret is not None:
            value["private_test_value"] = secret
        return write_json(self.tmp_path / f"{label}.json", value)

    def identity_file(self, label: str, identity: dict[str, Any] | None = None) -> Path:
        return write_json(
            self.tmp_path / f"identity-{label}.json",
            identity or self.node_identities[label],
        )

    def hub_open_plan(self) -> Path:
        plan, _request = EPOCH_MATERIAL.build_open(
            {
                "schema": "mac.fleet_epoch_open_material.v1",
                "epoch_id": EPOCH,
                "source_commit": SOURCE_COMMIT,
                "require_release_all_selected": True,
                "successor_hold_reason": "synchronized successor hold",
                "desired_worker_credential_mode": (
                    self.desired_worker_credential_mode
                ),
                "agents": [
                    {
                        "agent_id": node["stable_id"],
                        "generation": node["generation"],
                        "deployment_id": node["deployment_id"],
                        "participant_state": {
                            "schema": "mac.fleet_release_participant_state.v1",
                            "agent_id": node["stable_id"],
                            "baseline_seen": "2026-07-19T05:40:00Z",
                            "expected_dispatch_hold": False,
                            "expected_hold_reason": None,
                            "expected_hold_at": None,
                        },
                        "principal_id": f"principal-{index}",
                        "attestation_candidate_key": None,
                        "report_executor_action": "preserve",
                        "report_executor_attestation": None,
                    }
                    for index, node in enumerate(self.nodes)
                ],
            }
        )
        return write_json(
            self.tmp_path / "hub-open.json",
            plan,
        )

    def hub_prove_plan(self) -> Path:
        plan, _request = EPOCH_MATERIAL.build_prove(
            {
                "schema": "mac.fleet_epoch_prove_material.v1",
                "epoch_id": EPOCH,
                "source_commit": SOURCE_COMMIT,
                "identity_sha256": HUB_IDENTITY_SHA256,
                "agents": [
                    {
                        "agent_id": node["stable_id"],
                        "generation": node["generation"],
                        "deployment_id": node["deployment_id"],
                        "prepared_evidence_sha256": self.journal["cohort"][index][
                            "prepared_evidence"
                        ]["sha256"],
                        "install_receipt": {
                            "schema": "mac.worker_credential_install_receipt.v1",
                            "installed": True,
                        },
                        "attestation_proof": None,
                        "report_executor_startup_timestamp": None,
                    }
                    for index, node in enumerate(self.nodes)
                ],
            }
        )
        return write_json(
            self.tmp_path / "hub-prove.json",
            plan,
        )

    def hub_receipt(self, status: str, *, label: str | None = None) -> Path:
        value: dict[str, Any] = {
            "schema": "mac.fleet_release_epoch_receipt.v1",
            "status": status,
            "epoch_id": EPOCH,
            "hub_authority_id": HUB_AUTHORITY_ID,
            "identity_sha256": HUB_IDENTITY_SHA256,
            "cohort_size": len(self.nodes),
            "successor_hold_reason": "synchronized successor hold",
            "desired_worker_credential_mode": self.desired_worker_credential_mode,
            "prepared_at": "2026-07-19T05:50:00.000000+00:00",
            "agents": [
                {
                    "agent_id": node["stable_id"],
                    "prior_dispatch_hold": False,
                    "prior_hold_reason": None,
                    "prior_hold_at": None,
                    "epoch_hold_reason": f"epoch-hold-{index}",
                    "epoch_hold_at": "2026-07-19T05:50:00.000000+00:00",
                    "generation": node["generation"],
                    "principal_id": f"principal-{index}",
                    "principal_version": 2,
                    "principal_fingerprint": f"fingerprint-{index}",
                    "attestation_candidate_fingerprint": None,
                    "report_executor_action": "preserve",
                }
                for index, node in enumerate(self.nodes)
            ],
        }
        if status in {"proved", "committed"} or (
            status == "aborted" and self.journal["hub_proved_evidence"] is not None
        ):
            value["proof_sha256"] = HUB_PROOF_SHA256
            value["proved_at"] = "2026-07-19T06:00:00.000000+00:00"
        if status == "committed":
            value["committed_at"] = "2026-07-19T06:05:00.000000+00:00"
        if status == "aborted":
            value["aborted_at"] = "2026-07-19T06:05:00.000000+00:00"
            value["abort_reason"] = "coordinator rollback"
            value["abort_disposition"] = "auto"
        return write_json(self.tmp_path / f"hub-receipt-{label or status}.json", value)

    def release_plan(self, *, epoch_id: str = RELEASE_EPOCH) -> Path:
        plan, _request = EPOCH_MATERIAL.build_release(
            {
                "schema": "mac.fleet_epoch_release_material.v1",
                "epoch_id": epoch_id,
                "source_commit": SOURCE_COMMIT,
                "identity_sha256": HUB_IDENTITY_SHA256,
                "require_release_all_selected": True,
                "successor_hold_reason": "synchronized successor hold",
                "agents": [
                    {
                        "agent_id": node["stable_id"],
                        "generation": node["generation"],
                        "deployment_id": node["deployment_id"],
                    }
                    for node in self.nodes
                ],
            }
        )
        return write_json(
            self.tmp_path / "release-plan.json",
            plan,
        )

    def commit_not_applied(self) -> Path:
        return self.hub_receipt("proved", label="commit-not-applied")

    def call(
        self,
        command: str,
        *,
        node: dict[str, str] | None = None,
        identity_file: Path | None = None,
        evidence_file: Path | None = None,
        open_plan: Path | None = None,
        prove_plan: Path | None = None,
        release_plan: Path | None = None,
        recovery_action: str | None = None,
        expected_revision: int | None = None,
        operation_id: str | None = None,
        check: bool = True,
    ) -> dict[str, Any]:
        self.sequence += 1
        op_id = operation_id or f"op-{self.sequence}-{command}"
        args = operation(
            command,
            self.revision if expected_revision is None else expected_revision,
            op_id,
            owner_nonce=self.owner_nonce,
        )
        if node is not None:
            args += node_args(node)
        if identity_file is not None:
            args += ["--identity-file", str(identity_file)]
        if evidence_file is not None:
            args += ["--evidence-file", str(evidence_file)]
        if open_plan is not None:
            args += ["--open-plan-file", str(open_plan)]
        if prove_plan is not None:
            args += ["--prove-plan-file", str(prove_plan)]
        if release_plan is not None:
            args += ["--release-plan-file", str(release_plan)]
        if command == "phase1-armed":
            args += ["--restore-contract-sha256", RESTORE_DIGEST]
        if command == "phase2-armed":
            args += [
                "--rollback-intent-sha256",
                ROLLBACK_DIGEST,
                "--finalizer-sha256",
                FINALIZER_DIGEST,
            ]
        if recovery_action is not None:
            args += ["--recovery-action", recovery_action]
        _result, payload = run_cli(self.directory, *args, check=check)
        if check:
            self.journal = payload["journal"]
            self.revision = self.journal["revision"]
        return payload

    def bind_routes(self) -> None:
        self.call(
            "hub-route-bound",
            identity_file=self.identity_file("hub", self.hub_identity),
        )
        for node in self.nodes:
            self.call(
                "route-bound",
                node=node,
                identity_file=self.identity_file(node["name"]),
            )

    def arm_phase1(self) -> None:
        for node in self.nodes:
            self.call("phase1-prepare-start", node=node)
            self.call(
                "phase1-armed",
                node=node,
                evidence_file=self.evidence(f"phase1-{node['name']}"),
            )

    def open_hub(self) -> None:
        self.call("hub-open-start", open_plan=self.hub_open_plan())
        self.call("hub-opened", evidence_file=self.hub_receipt("open"))

    def quiesce(self) -> None:
        for node in self.nodes:
            self.call("quiesce-start", node=node)
            self.call(
                "quiesced",
                node=node,
                evidence_file=self.evidence(f"quiesced-{node['name']}"),
            )

    def arm_phase2(self) -> None:
        for node in self.nodes:
            self.call(
                "phase2-armed",
                node=node,
                evidence_file=self.evidence(f"phase2-{node['name']}"),
            )

    def deploy(self) -> None:
        for node in self.nodes:
            self.call("phase2-start", node=node)
            self.call(
                "prepared",
                node=node,
                evidence_file=self.evidence(f"prepared-{node['name']}"),
            )

    def reach_prepared(self) -> None:
        self.bind_routes()
        self.arm_phase1()
        self.open_hub()
        self.quiesce()
        self.arm_phase2()
        self.deploy()
        self.prove_hub()

    def prove_hub(self) -> None:
        self.call("hub-prove-start", prove_plan=self.hub_prove_plan())
        self.call("hub-proved", evidence_file=self.hub_receipt("proved"))

    def begin_commit(self) -> None:
        self.call("commit-start", release_plan=self.release_plan())

    def commit_hub(self) -> None:
        self.call("commit", evidence_file=self.hub_receipt("committed"))

    def absence_receipt(
        self,
        *,
        status: str = "absent",
        epoch: str = EPOCH,
        hub_authority_id: str = HUB_AUTHORITY_ID,
        identity_sha256: str = HUB_IDENTITY_SHA256,
        label: str = "absence",
    ) -> Path:
        return write_json(
            self.tmp_path / f"hub-{label}.json",
            {
                "schema": "mac.fleet_release_epoch_status.v1",
                "status": status,
                "epoch_id": epoch,
                "hub_authority_id": hub_authority_id,
                "identity_sha256": identity_sha256,
            },
        )

    def quiescence_bundle(
        self,
        *,
        epoch: str = EPOCH,
        idle: bool = True,
        healthy: bool = True,
        active_work: bool = False,
        deployment_lock_held: bool = True,
        generations: dict[str, str] | None = None,
        label: str = "quiescence",
    ) -> Path:
        generations = generations or {}
        return write_json(
            self.tmp_path / f"orphan-{label}.json",
            {
                "schema": "mac.fleet_orphan_quiescence.v1",
                "epoch_id": epoch,
                "nodes": [
                    {
                        "stable_id": node["stable_id"],
                        "generation": generations.get(
                            node["stable_id"], node["generation"]
                        ),
                        "deployment_lock_held": deployment_lock_held,
                        "startup_attestation_sha256": digest(
                            f"startup:{node['stable_id']}"
                        ),
                        "idle": idle,
                        "healthy": healthy,
                        "active_work": active_work,
                    }
                    for node in self.nodes
                ],
            },
        )

    def orphan(
        self,
        *,
        absence_file: Path | None = None,
        quiescence_file: Path | None = None,
        check: bool = True,
    ) -> dict[str, Any]:
        self.sequence += 1
        op_id = f"op-{self.sequence}-hub-orphaned"
        args = operation("hub-orphaned", self.revision, op_id, owner_nonce=self.owner_nonce)
        args += ["--absence-file", str(absence_file or self.absence_receipt())]
        args += ["--quiescence-file", str(quiescence_file or self.quiescence_bundle())]
        _result, payload = run_cli(self.directory, *args, check=check)
        if check:
            self.journal = payload["journal"]
            self.revision = self.journal["revision"]
        return payload


def recovery(
    scenario: Scenario, *, policy: str = "retain-forward"
) -> dict[str, Any]:
    _result, payload = run_cli(
        scenario.directory,
        "recovery",
        "--epoch",
        EPOCH,
        "--policy",
        policy,
    )
    return payload


def advance_one_node_to(scenario: Scenario, checkpoint: str) -> None:
    node = scenario.nodes[0]
    if checkpoint == "init":
        return
    scenario.call(
        "hub-route-bound",
        identity_file=scenario.identity_file("hub", scenario.hub_identity),
    )
    if checkpoint == "hub-route-bound":
        return
    scenario.call(
        "route-bound",
        node=node,
        identity_file=scenario.identity_file(node["name"]),
    )
    if checkpoint == "route-bound":
        return
    scenario.call("phase1-prepare-start", node=node)
    if checkpoint == "phase1-prepare-start":
        return
    scenario.call(
        "phase1-armed",
        node=node,
        evidence_file=scenario.evidence("phase1"),
    )
    if checkpoint == "phase1-armed":
        return
    scenario.call("hub-open-start", open_plan=scenario.hub_open_plan())
    if checkpoint == "hub-open-start":
        return
    scenario.call("hub-opened", evidence_file=scenario.hub_receipt("open"))
    if checkpoint == "hub-opened":
        return
    scenario.call("quiesce-start", node=node)
    if checkpoint == "quiesce-start":
        return
    scenario.call("quiesced", node=node, evidence_file=scenario.evidence("quiesced"))
    if checkpoint == "quiesced":
        return
    scenario.call("phase2-armed", node=node, evidence_file=scenario.evidence("phase2"))
    if checkpoint == "phase2-armed":
        return
    scenario.call("phase2-start", node=node)
    if checkpoint == "phase2-start":
        return
    scenario.call("prepared", node=node, evidence_file=scenario.evidence("prepared"))
    if checkpoint == "prepared":
        return
    scenario.call("hub-prove-start", prove_plan=scenario.hub_prove_plan())
    if checkpoint == "hub-prove-start":
        return
    scenario.call("hub-proved", evidence_file=scenario.hub_receipt("proved"))
    if checkpoint == "hub-proved":
        return
    scenario.begin_commit()
    if checkpoint == "commit-start":
        return
    scenario.commit_hub()
    if checkpoint == "commit":
        return
    scenario.call("finalize-start", node=node)
    if checkpoint == "finalize-start":
        return
    scenario.call(
        "finalized-node", node=node, evidence_file=scenario.evidence("finalized")
    )
    if checkpoint == "finalized-node":
        return
    scenario.call("finalize")
    assert checkpoint == "finalize"


@pytest.mark.parametrize(
    (
        "checkpoint",
        "direction",
        "hub_action",
        "rollback_action",
        "finalization_count",
        "required",
    ),
    [
        # A freshly created journal has bound no route and mutated nothing, so
        # recovery classifies it for a route-free abort of the unmutated
        # pre-route transaction rather than a rollback/retain-forward.
        ("init", "abort_unmutated", "none", None, 0, True),
        ("hub-route-bound", "retain_forward", "none", None, 0, True),
        ("route-bound", "retain_forward", "none", None, 0, True),
        ("phase1-prepare-start", "retain_forward", "none", "retain_forward", 0, True),
        ("phase1-armed", "retain_forward", "none", "retain_forward", 0, True),
        (
            "hub-open-start",
            "retain_forward",
            "resolve_open",
            "retain_forward",
            0,
            True,
        ),
        ("hub-opened", "retain_forward", "abort_epoch", "retain_forward", 0, True),
        ("quiesce-start", "retain_forward", "abort_epoch", "retain_forward", 0, True),
        ("quiesced", "retain_forward", "abort_epoch", "retain_forward", 0, True),
        ("phase2-armed", "retain_forward", "abort_epoch", "retain_forward", 0, True),
        ("phase2-start", "retain_forward", "abort_epoch", "retain_forward", 0, True),
        ("prepared", "retain_forward", "abort_epoch", "retain_forward", 0, True),
        (
            "hub-prove-start",
            "retain_forward",
            "resolve_prove",
            "retain_forward",
            0,
            True,
        ),
        ("hub-proved", "retain_forward", "abort_epoch", "retain_forward", 0, True),
        ("commit-start", "resolve_commit", "resolve_commit", None, 0, True),
        ("commit", "finalize", "none", None, 1, True),
        ("finalize-start", "finalize", "none", None, 1, True),
        ("finalized-node", "finalize", "none", None, 0, True),
        ("finalize", "none", "none", None, 0, False),
    ],
)
def test_every_durable_transition_has_deterministic_crash_recovery(
    tmp_path: Path,
    checkpoint: str,
    direction: str,
    hub_action: str,
    rollback_action: str | None,
    finalization_count: int,
    required: bool,
) -> None:
    scenario = Scenario(tmp_path)
    advance_one_node_to(scenario, checkpoint)

    result = recovery(scenario)

    assert result["recovery_required"] is required
    assert result["direction"] == direction
    assert result["hub_recovery"]["action"] == hub_action
    assert len(result["finalization_candidates"]) == finalization_count
    if rollback_action is None:
        assert result["candidates"] == []
    else:
        assert [item["recovery_action"] for item in result["candidates"]] == [
            rollback_action
        ]
        assert (
            result["candidates"][0]["route_identity"]
            == scenario.node_identities["node-0"]
        )


def test_forward_lifecycle_is_durable_secret_free_and_requires_finalization(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path)
    secret = "must-not-appear-in-journal"
    scenario.bind_routes()
    scenario.call("phase1-prepare-start", node=scenario.nodes[0])
    scenario.call(
        "phase1-armed",
        node=scenario.nodes[0],
        evidence_file=scenario.evidence("phase1", secret=secret),
    )
    scenario.open_hub()
    scenario.quiesce()
    scenario.arm_phase2()
    scenario.deploy()
    scenario.prove_hub()
    scenario.begin_commit()

    assert scenario.journal["state"] == "commit_intent"
    assert recovery(scenario)["replay_release_plan"] is True
    scenario.commit_hub()
    assert scenario.journal["state"] == "hub_committed"
    assert scenario.journal["phase"] == "finalizing"
    assert recovery(scenario)["recovery_required"] is True
    scenario.call("finalize-start", node=scenario.nodes[0])
    scenario.call(
        "finalized-node",
        node=scenario.nodes[0],
        evidence_file=scenario.evidence("finalized"),
    )
    scenario.call("finalize")

    assert scenario.journal["state"] == "finalized"
    assert scenario.journal["schema"] == "mac.fleet_cohort_transaction.v2"
    commit_evidence = scenario.journal["hub_commit_evidence"]
    assert commit_evidence["status"] == "committed"
    assert commit_evidence["identity_sha256"] == HUB_IDENTITY_SHA256
    assert commit_evidence["proof_sha256"] == HUB_PROOF_SHA256
    assert (
        commit_evidence["release_plan_sha256"]
        == scenario.journal["release_plan"]["sha256"]
    )
    assert len(scenario.journal["operations"]) == scenario.revision
    journal_file = next(scenario.directory.glob("transaction-*.json"))
    serialized = journal_file.read_text(encoding="utf-8")
    assert secret not in serialized
    assert HUB_AUTHORITY_ID not in serialized
    assert stat.S_IMODE(scenario.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal_file.stat().st_mode) == 0o600
    assert recovery(scenario)["recovery_required"] is False


def test_phase1_intents_can_be_batched_before_parallel_preparation(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path, node_count=3)
    scenario.bind_routes()

    for node in scenario.nodes:
        scenario.call("phase1-prepare-start", node=node)

    assert [node["state"] for node in scenario.journal["cohort"]] == [
        "phase1_prepare_started",
        "phase1_prepare_started",
        "phase1_prepare_started",
    ]

    for node in scenario.nodes:
        scenario.call(
            "phase1-armed",
            node=node,
            evidence_file=scenario.evidence("phase1-%s" % node["name"]),
        )

    assert [node["state"] for node in scenario.journal["cohort"]] == [
        "phase1_armed",
        "phase1_armed",
        "phase1_armed",
    ]


def test_typed_material_plans_accept_exact_journal_epoch_through_finalization(
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        tmp_path,
        node_count=2,
        desired_worker_credential_mode="compatibility",
    )
    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.open_hub()
    scenario.quiesce()
    scenario.arm_phase2()
    scenario.deploy()
    scenario.prove_hub()
    scenario.call("commit-start", release_plan=scenario.release_plan(epoch_id=EPOCH))
    scenario.commit_hub()
    for node in scenario.nodes:
        scenario.call("finalize-start", node=node)
        scenario.call(
            "finalized-node",
            node=node,
            evidence_file=scenario.evidence(f"typed-finalized-{node['name']}"),
        )
    scenario.call("finalize")

    assert scenario.journal["state"] == "finalized"
    assert scenario.journal["release_plan"]["epoch_id"] == EPOCH


def test_release_epoch_binding_accepts_typed_and_legacy_but_rejects_other_suffix(
    journal_module: Any,
) -> None:
    assert journal_module._release_epoch_matches(EPOCH, EPOCH) is True
    assert journal_module._release_epoch_matches(EPOCH, RELEASE_EPOCH) is True
    assert journal_module._release_epoch_matches(EPOCH, f"{EPOCH}:unrelated") is False
    assert (
        journal_module._release_epoch_matches(
            EPOCH,
            f"{SOURCE_COMMIT}:another-controller:{'b' * 64}",
        )
        is False
    )


def test_typed_exact_epoch_commit_start_is_rejected_before_hub_proof(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path)
    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.open_hub()
    scenario.quiesce()
    scenario.arm_phase2()
    scenario.deploy()

    rejected = scenario.call(
        "commit-start",
        release_plan=scenario.release_plan(epoch_id=EPOCH),
        check=False,
    )

    assert rejected["error"]["code"] == "invalid_transition"
    assert scenario.journal["hub_state"] == "open"
    assert scenario.journal["release_plan"] is None


def test_typed_prove_plan_rejects_wrong_prepared_hash_after_preparation(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path)
    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.open_hub()
    scenario.quiesce()
    scenario.arm_phase2()
    scenario.deploy()
    prove_plan = json.loads(scenario.hub_prove_plan().read_text(encoding="utf-8"))
    prove_plan["agents"][0]["prepared_evidence_sha256"] = "0" * 64

    rejected = scenario.call(
        "hub-prove-start",
        prove_plan=write_json(tmp_path / "typed-wrong-prepared.json", prove_plan),
        check=False,
    )

    assert rejected["error"]["code"] == "release_plan_binding_conflict"
    assert scenario.journal["hub_state"] == "open"
    assert scenario.journal["hub_prove_plan"] is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("source_commit", "f" * 40),
        ("successor_hold_reason", "unrelated successor hold"),
        ("generation", "unrelated-generation"),
        ("deployment_id", "unrelated-deployment"),
    ],
)
def test_typed_exact_epoch_release_plan_preserves_other_bindings(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    scenario = Scenario(tmp_path)
    scenario.reach_prepared()
    plan = json.loads(
        scenario.release_plan(epoch_id=EPOCH).read_text(encoding="utf-8")
    )
    if field in {"generation", "deployment_id"}:
        plan["agents"][0][field] = replacement
    else:
        plan[field] = replacement

    rejected = scenario.call(
        "commit-start",
        release_plan=write_json(tmp_path / f"typed-wrong-{field}.json", plan),
        check=False,
    )

    assert rejected["error"]["code"] == "release_plan_binding_conflict"
    assert scenario.journal["hub_state"] == "proved"
    assert scenario.journal["release_plan"] is None


def test_commit_requires_exact_bound_committed_receipt_and_never_bare_flip(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path)
    scenario.reach_prepared()
    scenario.begin_commit()

    bare = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--directory",
            str(scenario.directory),
            *operation("commit", scenario.revision, "bare-commit"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert bare.returncode == 2
    assert "--evidence-file" in bare.stderr

    wrong_status = scenario.hub_receipt("proved", label="wrong-commit-status")
    rejected = scenario.call("commit", evidence_file=wrong_status, check=False)
    assert rejected["error"]["code"] == "invalid_evidence"

    mismatched = json.loads(
        scenario.hub_receipt("committed", label="mismatched-commit").read_text(
            encoding="utf-8"
        )
    )
    mismatched["hub_authority_id"] = "123e4567-e89b-12d3-a456-426614174999"
    mismatch_file = write_json(tmp_path / "hub-receipt-mismatch.json", mismatched)
    rejected = scenario.call("commit", evidence_file=mismatch_file, check=False)
    assert rejected["error"]["code"] == "evidence_binding_conflict"

    _result, status = run_cli(scenario.directory, "status", "--epoch", EPOCH)
    assert status["journal"]["state"] == "commit_intent"
    assert status["journal"]["hub_commit_evidence"] is None

    receipt = scenario.hub_receipt("committed")
    scenario.call("commit", evidence_file=receipt, operation_id="commit-receipt")
    revision = scenario.revision
    retry = scenario.call(
        "commit",
        evidence_file=receipt,
        operation_id="commit-receipt",
        expected_revision=revision - 1,
    )
    assert retry["changed"] is False
    assert retry["journal"]["revision"] == revision


def test_commit_not_applied_requires_exact_matching_proved_get_receipt(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path)
    scenario.reach_prepared()
    scenario.begin_commit()

    wrong_status = scenario.hub_receipt("open", label="not-applied-open")
    rejected = scenario.call(
        "commit-not-applied", evidence_file=wrong_status, check=False
    )
    assert rejected["error"]["code"] == "invalid_evidence"

    absent = write_json(
        tmp_path / "not-applied-absent.json",
        {
            "status": "absent",
            "epoch_id": EPOCH,
            "hub_authority_id": HUB_AUTHORITY_ID,
            "identity_sha256": HUB_IDENTITY_SHA256,
        },
    )
    rejected = scenario.call("commit-not-applied", evidence_file=absent, check=False)
    assert rejected["error"]["code"] == "invalid_evidence"

    mismatched = json.loads(scenario.commit_not_applied().read_text(encoding="utf-8"))
    mismatched["identity_sha256"] = "sha256:" + "0" * 64
    mismatch_file = write_json(tmp_path / "not-applied-mismatch.json", mismatched)
    rejected = scenario.call(
        "commit-not-applied", evidence_file=mismatch_file, check=False
    )
    assert rejected["error"]["code"] == "evidence_binding_conflict"

    _result, status = run_cli(scenario.directory, "status", "--epoch", EPOCH)
    assert status["journal"]["state"] == "commit_intent"
    assert status["journal"]["commit_not_applied_evidence"] is None

    scenario.call("commit-not-applied", evidence_file=scenario.commit_not_applied())
    assert scenario.journal["state"] == "aborting"
    assert scenario.journal["hub_state"] == "proved"


def test_hub_open_receipt_must_match_exact_prior_hold_ownership(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path)
    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.call("hub-open-start", open_plan=scenario.hub_open_plan())

    mismatched = json.loads(
        scenario.hub_receipt("open", label="ownership-mismatch").read_text(
            encoding="utf-8"
        )
    )
    mismatched["agents"][0]["prior_dispatch_hold"] = True
    mismatched["agents"][0]["prior_hold_reason"] = "another-controller"
    mismatched["agents"][0]["prior_hold_at"] = "2026-07-19T05:40:00+00:00"
    mismatch_file = write_json(tmp_path / "ownership-mismatch.json", mismatched)
    rejected = scenario.call("hub-opened", evidence_file=mismatch_file, check=False)
    assert rejected["error"]["code"] == "evidence_binding_conflict"
    assert scenario.journal["hub_state"] == "open_intent"

    scenario.call("hub-opened", evidence_file=scenario.hub_receipt("open"))
    assert scenario.journal["hub_state"] == "open"


def test_explicit_rollback_is_reverse_order_and_uses_durable_intent_not_a_probe(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path, node_count=2)
    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.open_hub()
    scenario.quiesce()
    scenario.arm_phase2()
    first, second = scenario.nodes
    scenario.call("phase2-start", node=first)
    scenario.call(
        "prepared", node=first, evidence_file=scenario.evidence("prepared-first")
    )

    result = recovery(scenario, policy="rollback")
    assert [item["agent_name"] for item in result["candidates"]] == [
        second["name"],
        first["name"],
    ]
    assert [item["recovery_action"] for item in result["candidates"]] == [
        "phase1_restore",
        "phase2_rollback",
    ]

    scenario.call("hub-aborted", evidence_file=scenario.hub_receipt("aborted"))
    for node, action in ((second, "phase1_restore"), (first, "phase2_rollback")):
        scenario.call("abort-start", node=node, recovery_action=action)
        scenario.call(
            "aborted-node",
            node=node,
            evidence_file=scenario.evidence(f"aborted-{node['name']}"),
        )
    scenario.call("abort")

    assert scenario.journal["state"] == "aborted"
    assert [node["abort_kind"] for node in scenario.journal["cohort"]] == [
        "phase2_rollback",
        "phase1_restore",
    ]
    assert recovery(scenario, policy="rollback")["recovery_required"] is False


def test_default_recovery_retains_every_mutated_node_and_binds_that_policy(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path, node_count=2)
    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.open_hub()
    scenario.quiesce()
    scenario.arm_phase2()
    first, second = scenario.nodes
    scenario.call("phase2-start", node=first)

    result = recovery(scenario)
    assert result["direction"] == "retain_forward"
    assert [item["agent_name"] for item in result["candidates"]] == [
        second["name"],
        first["name"],
    ]
    assert {item["recovery_action"] for item in result["candidates"]} == {
        "retain_forward"
    }

    scenario.call("hub-aborted", evidence_file=scenario.hub_receipt("aborted"))
    scenario.call("abort-start", node=second, recovery_action="retain_forward")
    _result, conflict = run_cli(
        scenario.directory,
        "recovery",
        "--epoch",
        EPOCH,
        "--policy",
        "rollback",
        check=False,
    )
    assert conflict["error"]["code"] == "recovery_policy_conflict"

    for node in (second, first):
        if node is not second:
            scenario.call("abort-start", node=node, recovery_action="retain_forward")
        scenario.call(
            "aborted-node",
            node=node,
            evidence_file=scenario.evidence(f"retained-{node['name']}"),
        )
    scenario.call("abort")

    assert scenario.journal["state"] == "aborted"
    assert [node["abort_kind"] for node in scenario.journal["cohort"]] == [
        "retain_forward",
        "retain_forward",
    ]
    assert recovery(scenario)["recovery_required"] is False


def test_proved_not_committed_receipt_is_required_before_commit_can_roll_back(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path)
    scenario.reach_prepared()
    scenario.begin_commit()

    rejected = scenario.call(
        "hub-aborted",
        evidence_file=scenario.hub_receipt("aborted", label="premature-abort"),
        check=False,
    )
    assert rejected["error"]["code"] == "invalid_transition"

    scenario.call("commit-not-applied", evidence_file=scenario.commit_not_applied())
    assert scenario.journal["state"] == "aborting"
    scenario.call("hub-aborted", evidence_file=scenario.hub_receipt("aborted"))
    scenario.call(
        "abort-start",
        node=scenario.nodes[0],
        recovery_action="phase2_rollback",
    )
    scenario.call(
        "aborted-node",
        node=scenario.nodes[0],
        evidence_file=scenario.evidence("node-aborted"),
    )
    scenario.call("abort")
    assert scenario.journal["commit_not_applied_evidence"] is not None
    assert scenario.journal["state"] == "aborted"


def test_exact_abort_receipt_resolves_durable_prove_intent(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.open_hub()
    scenario.quiesce()
    scenario.arm_phase2()
    scenario.deploy()
    scenario.call("hub-prove-start", prove_plan=scenario.hub_prove_plan())

    receipt = scenario.hub_receipt("aborted")
    scenario.call("hub-aborted", evidence_file=receipt)
    scenario.call("hub-aborted", evidence_file=receipt)

    assert scenario.journal["hub_state"] == "aborted"
    assert scenario.journal["state"] == "aborting"
    assert scenario.journal["hub_abort_evidence"] is not None


def test_abort_receipt_rejects_unknown_disposition(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.open_hub()
    receipt = scenario.hub_receipt("aborted", label="invalid-disposition")
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["abort_disposition"] = "erase_everything"
    write_json(receipt, payload)

    rejected = scenario.call(
        "hub-aborted",
        evidence_file=receipt,
        check=False,
    )

    assert rejected["error"]["code"] == "invalid_evidence"
    assert "abort disposition" in rejected["error"]["message"]


def test_quiesce_and_phase2_start_intents_close_both_mutation_kill_windows(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path)
    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.open_hub()
    node = scenario.nodes[0]

    scenario.call("quiesce-start", node=node)
    candidate = recovery(scenario, policy="rollback")["candidates"][0]
    assert candidate["state"] == "quiesce_started"
    assert candidate["recovery_action"] == "phase1_restore"
    scenario.call("quiesced", node=node, evidence_file=scenario.evidence("quiesced"))
    scenario.arm_phase2()
    scenario.call("phase2-start", node=node)
    candidate = recovery(scenario, policy="rollback")["candidates"][0]
    assert candidate["state"] == "phase2_started"
    assert candidate["recovery_action"] == "phase2_rollback"
    assert candidate["rollback_intent_sha256"] == ROLLBACK_DIGEST


def test_finalization_failure_remains_an_active_recoverable_epoch(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path)
    scenario.reach_prepared()
    scenario.begin_commit()
    scenario.commit_hub()
    scenario.call("finalize-start", node=scenario.nodes[0])

    _result, discovered = run_cli(scenario.directory, "discover")
    assert discovered["active"]["state"] == "hub_committed"
    assert discovered["active"]["finalization_candidates"] == 1
    duplicate_cohort = write_json(tmp_path / "cohort-duplicate.json", cohort())
    _result, conflict = run_cli(
        scenario.directory,
        "init",
        "--epoch",
        f"{SOURCE_COMMIT}:20260719:second",
        "--source-commit",
        SOURCE_COMMIT,
        "--deploy-ts",
        "20260719T070000Z",
        "--fleet",
        "rocky",
        "--hub-agent",
        "rocky",
        "--cohort-file",
        str(duplicate_cohort),
        "--owner-nonce",
        OWNER_NONCE,
        "--owner-pid",
        str(os.getpid()),
        check=False,
    )
    assert conflict["error"]["code"] == "active_epoch_conflict"


def test_route_identity_is_adapter_typed_and_hub_binds_store_authority(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path)
    plain_hub = scenario.identity_file("plain-hub", ssh_identity("hub"))
    rejected = scenario.call("hub-route-bound", identity_file=plain_hub, check=False)
    assert rejected["error"]["code"] == "invalid_evidence"

    scenario.call(
        "hub-route-bound",
        identity_file=scenario.identity_file("hub", scenario.hub_identity),
    )
    hub_as_node = scenario.identity_file("hub-as-node", scenario.hub_identity)
    rejected = scenario.call(
        "route-bound",
        node=scenario.nodes[0],
        identity_file=hub_as_node,
        check=False,
    )
    assert rejected["error"]["code"] == "invalid_evidence"

    k8s = k8s_identity("workload-a")
    scenario.node_identities["node-0"] = k8s
    scenario.call(
        "route-bound",
        node=scenario.nodes[0],
        identity_file=scenario.identity_file("node-0", k8s),
    )
    scenario.call("phase1-prepare-start", node=scenario.nodes[0])
    scenario.call(
        "phase1-armed",
        node=scenario.nodes[0],
        evidence_file=scenario.evidence("phase1"),
    )
    candidate = recovery(scenario)["candidates"][0]
    assert candidate["route_identity"]["adapter"] == "kubernetes-workload"


def test_stale_cas_cannot_strand_hub_open_auxiliary_plan(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    scenario.bind_routes()
    scenario.arm_phase1()
    plan = scenario.hub_open_plan()

    rejected = scenario.call(
        "hub-open-start",
        open_plan=plan,
        expected_revision=scenario.revision - 1,
        check=False,
    )
    assert rejected["error"]["code"] == "cas_conflict"
    assert list(scenario.directory.glob("hub-open-plan-*.json")) == []
    scenario.call("hub-open-start", open_plan=plan)
    assert len(list(scenario.directory.glob("hub-open-plan-*.json"))) == 1


def test_cas_and_operation_id_retries_are_fenced_and_idempotent(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    identity = scenario.identity_file("hub", scenario.hub_identity)
    args = operation("hub-route-bound", 0, "fixed-op") + [
        "--identity-file",
        str(identity),
    ]
    _result, first = run_cli(scenario.directory, *args)
    assert first["changed"] is True
    _result, retry = run_cli(scenario.directory, *args)
    assert retry["changed"] is False
    assert retry["journal"]["revision"] == 1

    changed_identity = scenario.identity_file(
        "other-hub", ssh_identity("different-hub", hub=True)
    )
    conflict_args = operation("hub-route-bound", 1, "fixed-op") + [
        "--identity-file",
        str(changed_identity),
    ]
    _result, conflict = run_cli(scenario.directory, *conflict_args, check=False)
    assert conflict["error"]["code"] == "operation_conflict"

    stale_args = operation("hub-route-bound", 0, "new-op") + [
        "--identity-file",
        str(identity),
    ]
    _result, stale = run_cli(scenario.directory, *stale_args, check=False)
    assert stale["error"]["code"] == "cas_conflict"


def test_owner_is_bound_to_boot_and_process_incarnation_and_can_be_adopted(
    tmp_path: Path,
) -> None:
    old_owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        scenario = Scenario(
            tmp_path,
            owner_nonce="old-owner",
            owner_pid=old_owner.pid,
        )
        owner = scenario.journal["owner"]
        assert owner["pid"] == old_owner.pid
        assert len(owner["boot_id_sha256"]) == 64
        assert len(owner["process_start_sha256"]) == 64
    finally:
        old_owner.terminate()
        old_owner.wait(timeout=10)

    _result, status = run_cli(scenario.directory, "status", "--epoch", EPOCH)
    assert status["summary"]["owner"]["alive"] is False
    adopt_args = [
        "adopt",
        "--epoch",
        EPOCH,
        "--expected-revision",
        "0",
        "--operation-id",
        "adopt-owner",
        "--previous-owner-nonce",
        "old-owner",
        "--new-owner-nonce",
        OWNER_NONCE,
        "--new-owner-pid",
        str(os.getpid()),
    ]
    _result, adopted = run_cli(scenario.directory, *adopt_args)
    assert adopted["journal"]["owner"]["nonce"] == OWNER_NONCE
    assert (
        adopted["journal"]["owner"]["process_start_sha256"]
        != owner["process_start_sha256"]
    )


def test_process_identity_mismatch_fences_mutation_even_when_pid_is_live(
    tmp_path: Path,
) -> None:
    scenario = Scenario(tmp_path)
    journal_file = next(scenario.directory.glob("transaction-*.json"))
    tampered = json.loads(journal_file.read_text(encoding="utf-8"))
    tampered["owner"]["process_start_sha256"] = "f" * 64
    write_json(journal_file, tampered)

    identity = scenario.identity_file("hub", scenario.hub_identity)
    rejected = scenario.call("hub-route-bound", identity_file=identity, check=False)
    assert rejected["error"]["code"] == "owner_dead"


@pytest.mark.parametrize("attack", ["mode", "symlink", "hardlink", "oversize"])
def test_evidence_reader_rejects_unsafe_files(tmp_path: Path, attack: str) -> None:
    scenario = Scenario(tmp_path)
    scenario.bind_routes()
    scenario.call("phase1-prepare-start", node=scenario.nodes[0])
    candidate = scenario.evidence("unsafe")
    if attack == "mode":
        candidate.chmod(0o644)
    elif attack == "symlink":
        link = tmp_path / "unsafe-link.json"
        link.symlink_to(candidate)
        candidate = link
    elif attack == "hardlink":
        os.link(candidate, tmp_path / "unsafe-hardlink.json")
    else:
        candidate.write_bytes(b"{" + b" " * (256 * 1024) + b"}")
        candidate.chmod(0o600)

    rejected = scenario.call(
        "phase1-armed",
        node=scenario.nodes[0],
        evidence_file=candidate,
        check=False,
    )
    assert rejected["error"]["code"] in {"insecure_evidence", "invalid_evidence"}


def test_auxiliary_plan_tamper_is_detected_on_status(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.call("hub-open-start", open_plan=scenario.hub_open_plan())
    plan = next(scenario.directory.glob("hub-open-plan-*.json"))
    plan.write_text("{}\n", encoding="utf-8")
    plan.chmod(0o600)

    _result, rejected = run_cli(
        scenario.directory, "status", "--epoch", EPOCH, check=False
    )
    assert rejected["error"]["code"] in {
        "invalid_release_plan",
        "release_plan_binding_conflict",
    }


def test_invalid_transition_order_fails_closed(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path)
    node = scenario.nodes[0]
    rejected = scenario.call("quiesce-start", node=node, check=False)
    assert rejected["error"]["code"] == "invalid_transition"
    rejected = scenario.call("phase2-start", node=node, check=False)
    assert rejected["error"]["code"] == "invalid_transition"
    rejected = scenario.call(
        "commit-start", release_plan=scenario.release_plan(), check=False
    )
    assert rejected["error"]["code"] == "invalid_transition"

    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.open_hub()
    premature_prove = write_json(
        tmp_path / "premature-prove.json",
        {
            "schema": "mac.fleet_epoch_prove_intent.v1",
            "epoch_id": EPOCH,
            "source_commit": SOURCE_COMMIT,
            "identity_sha256": HUB_IDENTITY_SHA256,
            "agents": [
                {
                    "agent_id": node["stable_id"],
                    "generation": node["generation"],
                    "deployment_id": node["deployment_id"],
                    "prepared_evidence_sha256": "0" * 64,
                }
            ],
        },
    )
    rejected = scenario.call("hub-prove-start", prove_plan=premature_prove, check=False)
    assert rejected["error"]["code"] == "invalid_transition"


def test_directory_must_be_owner_private(tmp_path: Path) -> None:
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    cohort_file = write_json(tmp_path / "cohort.json", cohort())
    _result, rejected = run_cli(
        directory,
        "init",
        "--epoch",
        EPOCH,
        "--source-commit",
        SOURCE_COMMIT,
        "--deploy-ts",
        "20260719T054500Z",
        "--fleet",
        "rocky",
        "--hub-agent",
        "rocky",
        "--cohort-file",
        str(cohort_file),
        "--owner-nonce",
        OWNER_NONCE,
        "--owner-pid",
        str(os.getpid()),
        check=False,
    )
    assert rejected["error"]["code"] == "insecure_directory"


# The deploy controller's recovery route gate (verify_cohort_recovery_routes in
# deploy/deploy-mac-fleet.sh) extracts one route record per recovering hub/node
# and raises when a record lacks a journal-bound endpoint identity.  This is a
# faithful transcription of that gate so the classifier contract can be tested
# without a live fleet.
def _recovery_route_records(recovery: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    hub = recovery.get("hub_recovery") or {}
    direction = recovery.get("direction")
    records: list[tuple[str, Any, Any]] = []
    if hub.get("action") != "none" or direction in {
        "resolve_commit",
        "rollback",
        "retain_forward",
    }:
        records.append(("hub", hub.get("agent_name"), hub.get("route_identity")))
    for key in ("candidates", "finalization_candidates"):
        for item in recovery.get(key) or []:
            records.append(("node", item.get("agent_name"), item.get("route_identity")))
    return records


def _gate_requires_bound_identities(recovery: dict[str, Any]) -> list[tuple[str, str]]:
    seen: set[tuple[str, Any]] = set()
    verified: list[tuple[str, str]] = []
    for role, agent, identity in _recovery_route_records(recovery):
        key = (role, agent)
        if key in seen:
            continue
        seen.add(key)
        if not agent or not isinstance(identity, dict):
            raise SystemExit(
                "recovery mutation lacks a journal-bound endpoint identity"
            )
        verified.append((role, agent))
    return verified


def test_hub_unreachable_immediately_after_journal_creation_self_recovers(
    tmp_path: Path,
) -> None:
    # A fresh fungible deploy opens the journal, then the very first hub-route
    # reachability check fails before an endpoint identity is bound.  The
    # journal is preparing/routing, the hub epoch is unopened, and every node
    # is still planned -- nothing was mutated.
    scenario = Scenario(tmp_path, node_count=2)
    assert scenario.journal["state"] == "preparing"
    assert scenario.journal["hub_state"] == "unopened"
    assert scenario.journal["hub_route_identity"] is None
    assert all(node["state"] == "planned" for node in scenario.journal["cohort"])

    plan = recovery(scenario)
    # Recovery classifies the provably unmutated pre-route transaction for a
    # route-free abort instead of demanding an endpoint identity that was never
    # journalled.
    assert plan["direction"] == "abort_unmutated"
    assert plan["hub_recovery"]["action"] == "none"
    assert plan["hub_recovery"]["route_identity"] is None
    assert plan["candidates"] == []

    # The controller's route gate produces no records to attest and therefore
    # does not raise "recovery mutation lacks a journal-bound endpoint
    # identity".
    assert _gate_requires_bound_identities(plan) == []

    # The classification remains route-free regardless of recovery policy.
    assert recovery(scenario, policy="rollback")["direction"] == "abort_unmutated"

    # The abort transition completes directly and durably.
    scenario.call("abort")
    assert scenario.journal["state"] == "aborted"
    assert recovery(scenario)["recovery_required"] is False


def test_mutated_transaction_still_fails_closed_without_bound_identity(
    tmp_path: Path,
) -> None:
    # Contrast: the moment any node/hub mutation occurs, the journal leaves the
    # unmutated pre-route window and recovery must re-arm the exact
    # endpoint-identity requirement.
    scenario = Scenario(tmp_path)
    scenario.bind_routes()
    scenario.arm_phase1()

    plan = recovery(scenario)
    assert plan["direction"] != "abort_unmutated"
    assert plan["direction"] == "retain_forward"
    # A route was bound before the mutation, so the gate has a real identity to
    # attest against and does not short-circuit.
    assert plan["hub_recovery"]["route_identity"] is not None
    verified = _gate_requires_bound_identities(plan)
    verified_roles = {role for role, _agent in verified}
    assert "hub" in verified_roles
    assert "node" in verified_roles

    # If a mutated journal is ever missing its exact endpoint identity, the
    # controller gate fails closed with the historical error rather than
    # silently recovering.
    stripped = json.loads(json.dumps(plan))
    stripped["hub_recovery"]["route_identity"] = None
    for candidate in stripped["candidates"]:
        candidate["route_identity"] = None
    with pytest.raises(SystemExit) as excinfo:
        _gate_requires_bound_identities(stripped)
    assert "recovery mutation lacks a journal-bound endpoint identity" in str(
        excinfo.value
    )


def test_pre_route_classifier_rejects_any_hub_or_node_mutation(
    journal_module: Any, tmp_path: Path
) -> None:
    # Direct unit coverage of the classifier predicate: it must be true only in
    # the unmutated pre-route window and false after any recorded mutation.
    scenario = Scenario(tmp_path)
    fresh = scenario.journal
    assert journal_module._is_unmutated_pre_route(fresh) is True

    scenario.bind_routes()
    routed = scenario.journal
    assert journal_module._is_unmutated_pre_route(routed) is False


# ---------------------------------------------------------------------------
# Orphan-authority recovery: retire only a proven-absent hub barrier after the
# hub loses authority (404 for the release epoch, no pending credential row).
# ---------------------------------------------------------------------------


def _open_epoch(tmp_path: Path, **kwargs: Any) -> Scenario:
    scenario = Scenario(tmp_path, **kwargs)
    scenario.bind_routes()
    scenario.arm_phase1()
    scenario.open_hub()
    return scenario


def test_orphan_recovery_retires_absent_barrier_then_abort_closes_journal(
    tmp_path: Path,
) -> None:
    scenario = _open_epoch(tmp_path)
    assert scenario.journal["hub_state"] == "open"
    successor_hold = scenario.journal["successor_hold"]

    payload = scenario.orphan()
    journal = payload["journal"]
    assert journal["hub_state"] == "aborted"
    assert journal["state"] == "aborting"
    record = journal["hub_orphan_evidence"]
    assert record is not None
    assert record["schema"] == "mac.fleet_hub_orphan_abort_evidence.v1"
    assert record["from_hub_state"] == "open"
    assert record["identity_sha256"] == HUB_IDENTITY_SHA256
    # The unrelated successor/operator hold is never touched.
    assert journal["successor_hold"] == successor_hold

    # No pending credential row is reconstructed: the retired-orphan journal
    # carries no principal identity, fingerprint, or version anywhere.
    serialized = json.dumps(journal)
    assert "principal_id" not in serialized
    assert "principal_fingerprint" not in serialized
    assert "principal_version" not in serialized

    # The existing rollback path now retires the matching cohort nodes and
    # closes the journal -- orphan recovery only unblocked the hub barrier.
    report = recovery(scenario)
    for candidate in report["candidates"]:
        node = next(
            item for item in scenario.nodes if item["name"] == candidate["agent_name"]
        )
        scenario.call(
            "abort-start", node=node, recovery_action=candidate["recovery_action"]
        )
        scenario.call(
            "aborted-node",
            node=node,
            evidence_file=scenario.evidence(f"aborted-{node['name']}"),
        )
    abort = scenario.call("abort")
    assert abort["journal"]["state"] == "aborted"


def test_orphan_recovery_is_idempotent(tmp_path: Path) -> None:
    scenario = _open_epoch(tmp_path)
    first = scenario.orphan()
    revision = first["journal"]["revision"]
    record = first["journal"]["hub_orphan_evidence"]

    replay = scenario.orphan(
        absence_file=scenario.absence_receipt(label="replay"),
        quiescence_file=scenario.quiescence_bundle(label="replay"),
    )
    assert replay["changed"] is False
    assert replay["journal"]["revision"] == revision
    assert replay["journal"]["hub_orphan_evidence"] == record


def test_orphan_recovery_works_after_prove(tmp_path: Path) -> None:
    scenario = _open_epoch(tmp_path)
    scenario.quiesce()
    scenario.arm_phase2()
    scenario.deploy()
    scenario.prove_hub()
    assert scenario.journal["hub_state"] == "proved"

    payload = scenario.orphan()
    assert payload["journal"]["hub_state"] == "aborted"
    assert payload["journal"]["hub_orphan_evidence"]["from_hub_state"] == "proved"


def test_orphan_recovery_rejects_transport_or_mismatch_status(tmp_path: Path) -> None:
    scenario = _open_epoch(tmp_path)
    # A mismatch is a live, contradicting epoch -- never an orphan.
    payload = scenario.orphan(
        absence_file=scenario.absence_receipt(status="mismatch", label="mismatch"),
        check=False,
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "evidence_binding_conflict"
    assert scenario.journal["hub_state"] == "open"


def test_orphan_recovery_binds_absence_to_this_authority_and_identity(
    tmp_path: Path,
) -> None:
    scenario = _open_epoch(tmp_path)
    other_authority = "00000000-0000-4000-8000-000000000000"
    wrong_authority = scenario.orphan(
        absence_file=scenario.absence_receipt(
            hub_authority_id=other_authority, label="other-authority"
        ),
        check=False,
    )
    assert wrong_authority["ok"] is False
    assert wrong_authority["error"]["code"] == "evidence_binding_conflict"

    wrong_identity = scenario.orphan(
        absence_file=scenario.absence_receipt(
            identity_sha256="sha256:" + ("0" * 64), label="other-identity"
        ),
        check=False,
    )
    assert wrong_identity["ok"] is False
    assert wrong_identity["error"]["code"] == "evidence_binding_conflict"
    assert scenario.journal["hub_state"] == "open"


def test_orphan_recovery_binds_absence_to_this_epoch(tmp_path: Path) -> None:
    scenario = _open_epoch(tmp_path)
    payload = scenario.orphan(
        absence_file=scenario.absence_receipt(
            epoch=f"{SOURCE_COMMIT}:20260719:other", label="other-epoch"
        ),
        check=False,
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "evidence_binding_conflict"


def test_orphan_recovery_requires_full_cohort_quiescence(tmp_path: Path) -> None:
    scenario = _open_epoch(tmp_path, node_count=2)

    busy = scenario.orphan(
        quiescence_file=scenario.quiescence_bundle(active_work=True, label="busy"),
        check=False,
    )
    assert busy["ok"] is False
    assert busy["error"]["code"] == "invalid_transition"

    not_idle = scenario.orphan(
        quiescence_file=scenario.quiescence_bundle(idle=False, label="not-idle"),
        check=False,
    )
    assert not_idle["ok"] is False
    assert not_idle["error"]["code"] == "invalid_transition"

    unhealthy = scenario.orphan(
        quiescence_file=scenario.quiescence_bundle(healthy=False, label="unhealthy"),
        check=False,
    )
    assert unhealthy["ok"] is False

    no_lock = scenario.orphan(
        quiescence_file=scenario.quiescence_bundle(
            deployment_lock_held=False, label="no-lock"
        ),
        check=False,
    )
    assert no_lock["ok"] is False
    assert no_lock["error"]["code"] == "invalid_transition"
    assert scenario.journal["hub_state"] == "open"


def test_orphan_recovery_rejects_wrong_generation(tmp_path: Path) -> None:
    scenario = _open_epoch(tmp_path)
    stable_id = scenario.nodes[0]["stable_id"]
    payload = scenario.orphan(
        quiescence_file=scenario.quiescence_bundle(
            generations={stable_id: "generation-rogue"}, label="rogue-gen"
        ),
        check=False,
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "evidence_binding_conflict"


def test_orphan_recovery_refused_before_open_and_after_commit(tmp_path: Path) -> None:
    (tmp_path / "unopened").mkdir()
    unopened = Scenario(tmp_path / "unopened")
    unopened.bind_routes()
    assert unopened.journal["hub_state"] == "unopened"
    payload = unopened.orphan(check=False)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_transition"

    (tmp_path / "committed").mkdir()
    committed = Scenario(tmp_path / "committed")
    committed.reach_prepared()
    committed.begin_commit()
    committed.commit_hub()
    assert committed.journal["hub_state"] == "committed"
    blocked = committed.orphan(check=False)
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "invalid_transition"


def test_recovery_classifier_flags_orphan_recoverable_barrier(tmp_path: Path) -> None:
    scenario = _open_epoch(tmp_path)
    report = recovery(scenario)
    assert report["hub_recovery"]["action"] == "abort_epoch"
    assert report["hub_recovery"]["orphan_recoverable"] is True

    scenario.orphan()
    resolved = recovery(scenario)
    assert resolved["hub_recovery"]["action"] == "none"
    assert resolved["hub_recovery"]["orphan_recoverable"] is False
