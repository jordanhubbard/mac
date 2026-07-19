from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "fleet-release-epoch-material.py"
SPEC = importlib.util.spec_from_file_location("fleet_release_epoch_material", HELPER)
assert SPEC is not None and SPEC.loader is not None
material = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(material)
COMMIT = "a" * 40
IDENTITY = "sha256:" + ("b" * 64)


def open_material() -> dict:
    return {
        "schema": "mac.fleet_epoch_open_material.v1",
        "epoch_id": "epoch-one",
        "source_commit": COMMIT,
        "require_release_all_selected": True,
        "successor_hold_reason": "canary hold",
        "desired_worker_credential_mode": "compatibility",
        "agents": [
            {
                "agent_id": "agent_one",
                "generation": "generation-one",
                "deployment_id": "deployment-one",
                "participant_state": {
                    "schema": "mac.fleet_release_participant_state.v1",
                    "agent_id": "agent_one",
                    "baseline_seen": "2100-01-01T00:00:00Z",
                    "expected_dispatch_hold": False,
                    "expected_hold_reason": None,
                    "expected_hold_at": None,
                },
                "principal_id": "principal-one",
                "attestation_candidate_key": "candidate-" + ("x" * 40),
                "report_executor_action": "revoke",
                "report_executor_attestation": None,
            }
        ],
    }


def test_open_separates_secret_request_from_journal_plan() -> None:
    plan, request = material.build_open(open_material())
    encoded_plan = json.dumps(plan)
    encoded_request = json.dumps(request)
    assert "candidate-" not in encoded_plan
    assert "principal-one" not in encoded_plan
    assert "candidate-" in encoded_request
    assert request["participants"][0]["principal_id"] == "principal-one"
    assert plan["agents"][0]["deployment_id"] == "deployment-one"


def test_open_rejects_hold_and_report_action_ambiguity() -> None:
    changed = open_material()
    changed["agents"][0]["participant_state"]["expected_hold_reason"] = "stray"
    with pytest.raises(material.MaterialError, match="ownership"):
        material.build_open(changed)
    changed = open_material()
    changed["agents"][0]["report_executor_action"] = "approve"
    with pytest.raises(material.MaterialError, match="attestation"):
        material.build_open(changed)


def test_prove_and_release_bind_exact_prepared_cohort() -> None:
    receipt = {"schema": "mac.worker_credential_install_receipt.v1", "installed": True}
    prove_plan, prove_request = material.build_prove(
        {
            "schema": "mac.fleet_epoch_prove_material.v1",
            "epoch_id": "epoch-one",
            "source_commit": COMMIT,
            "identity_sha256": IDENTITY,
            "agents": [
                {
                    "agent_id": "agent_one",
                    "generation": "generation-one",
                    "deployment_id": "deployment-one",
                    "prepared_evidence_sha256": "c" * 64,
                    "install_receipt": receipt,
                    "attestation_proof": None,
                    "report_executor_startup_timestamp": None,
                }
            ],
        }
    )
    assert prove_plan["agents"][0]["prepared_evidence_sha256"] == "c" * 64
    assert prove_request["proofs"][0]["install_receipt"] == receipt

    release_plan, commit_request = material.build_release(
        {
            "schema": "mac.fleet_epoch_release_material.v1",
            "epoch_id": "epoch-one",
            "source_commit": COMMIT,
            "identity_sha256": IDENTITY,
            "require_release_all_selected": True,
            "successor_hold_reason": "canary hold",
            "agents": [
                {
                    "agent_id": "agent_one",
                    "generation": "generation-one",
                    "deployment_id": "deployment-one",
                }
            ],
        }
    )
    assert release_plan["schema"] == "mac.fleet_release_epoch.v1"
    assert commit_request == {"identity_sha256": IDENTITY}


def test_cli_requires_private_material_and_writes_private_outputs(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    source = private / "material.json"
    source.write_text(json.dumps(open_material()), encoding="utf-8")
    source.chmod(0o600)
    plan = private / "plan.json"
    request = private / "request.json"
    assert material.main(
        [
            "open",
            "--material",
            str(source),
            "--plan-out",
            str(plan),
            "--request-out",
            str(request),
        ]
    ) == 0
    assert stat.S_IMODE(plan.stat().st_mode) == 0o600
    assert stat.S_IMODE(request.stat().st_mode) == 0o600
    source.chmod(0o644)
    assert material.main(
        [
            "open",
            "--material",
            str(source),
            "--plan-out",
            str(plan),
            "--request-out",
            str(request),
        ]
    ) == 2


def test_helper_is_executable() -> None:
    assert os.access(HELPER, os.X_OK)
