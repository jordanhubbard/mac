from __future__ import annotations

import hashlib
import json

import pytest

from mac.landing_service import LandingServiceConfig
from mac.models import ValidationError
from mac.services import _normalize_repository_contract
from mac.test_support import ephemeral_store
from mac.work_package_certification_service import (
    CERTIFICATION_CONTRACT_SCHEMA,
    WorkPackageCertificationService,
)
from mac.work_package_pipeline import PipelineSnapshot
from mac.work_package_pipeline_runtime import RepositoryPipelineReleaseGateResolver
from tests.certifier_phase_profile_fixtures import mac_phase_profile


POLICY_TEXT = """\
version: 1
filesystem_policy:
  include_workdir: true
  read_only:
    - /usr
    - /bin
    - /etc
  read_write:
    - /tmp
    - /dev
landlock:
  compatibility: hard_requirement
process:
  run_as_user: sandbox
  run_as_group: sandbox
network_policies: {}
"""


def _contract() -> dict:
    policy_id = "mac-work-package-v1"
    return {
        "schema": "mac.repository_contract.v1",
        "project": "mac",
        "canonical_remote_url": "git@github.com:jordanhubbard/mac.git",
        "platforms": ["darwin", "linux"],
        "toolchain": {"required_commands": ["python3", "git"]},
        "bootstrap": {"command": "true", "creates": []},
        "test": {"command": "scripts/run-contract-tests.sh"},
        "evidence": {"required": ["tests"]},
        "landing_certification_policy_id": policy_id,
        "work_package_certification": {
            "schema": CERTIFICATION_CONTRACT_SCHEMA,
            "policy": {
                "policy_id": policy_id,
                "version": 1,
                "checksum": "sha256:"
                + hashlib.sha256(POLICY_TEXT.encode("utf-8")).hexdigest(),
            },
            "policy_text": POLICY_TEXT,
            "phase_profile": mac_phase_profile(),
            "image_ref": "registry.invalid/mac-certifier@sha256:" + "a" * 64,
            "controller_commands": [
                {
                    "command_id": "contract-tests",
                    "argv": ["/opt/mac-certifier/bin/run-contract-tests"],
                    "timeout_seconds": 900,
                }
            ],
        },
    }


def test_repository_loader_preserves_valid_certification_extension() -> None:
    source = _contract()
    normalized = _normalize_repository_contract(source, ".mac/project.yaml")

    assert (
        normalized["landing_certification_policy_id"]
        == source["landing_certification_policy_id"]
    )
    assert (
        normalized["work_package_certification"] == source["work_package_certification"]
    )


def test_repository_loader_rejects_partial_or_mutable_certification() -> None:
    partial = _contract()
    partial.pop("work_package_certification")
    with pytest.raises(ValidationError, match="incomplete"):
        _normalize_repository_contract(partial, ".mac/project.yaml")

    mutable = _contract()
    mutable["work_package_certification"]["image_ref"] = "repo/image:latest"
    with pytest.raises(ValidationError, match="certification contract is invalid"):
        _normalize_repository_contract(mutable, ".mac/project.yaml")

    candidate_owned = _contract()
    candidate_owned["work_package_certification"]["controller_commands"][0]["argv"] = [
        "scripts/run-contract-tests.sh"
    ]
    with pytest.raises(ValidationError, match="certification command is invalid"):
        _normalize_repository_contract(candidate_owned, ".mac/project.yaml")

    repository_base = _contract()
    repository_base["work_package_certification"]["controller_commands"][0]["argv"] += [
        "--base-sha",
        "b" * 40,
    ]
    with pytest.raises(ValidationError, match="reserved for the controller"):
        _normalize_repository_contract(repository_base, ".mac/project.yaml")

    missing_profile = _contract()
    missing_profile["work_package_certification"].pop("phase_profile")
    with pytest.raises(ValidationError, match="fields"):
        _normalize_repository_contract(missing_profile, ".mac/project.yaml")

    typo_mode = _contract()
    profile = typo_mode["work_package_certification"]["phase_profile"]
    profile["selection_modes"]["documentation_fast_lnae"] = profile[
        "selection_modes"
    ].pop("documentation_fast_lane")
    with pytest.raises(ValidationError, match="phase profile"):
        _normalize_repository_contract(typo_mode, ".mac/project.yaml")

    credential_remote = _contract()
    credential_remote["canonical_remote_url"] = (
        "https://token@github.com/jordanhubbard/mac.git"
    )
    with pytest.raises(ValidationError, match="must not embed credentials"):
        _normalize_repository_contract(credential_remote, ".mac/project.yaml")


def test_release_gate_rejects_unpinned_image_before_wip_transfer() -> None:
    store = ephemeral_store()
    mutable = _contract()
    mutable["work_package_certification"]["image_ref"] = "repo/image:latest"
    now = "2026-07-17T12:00:00.000000+00:00"
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "repo_activation",
            "activation",
            "/tmp/activation",
            "git@github.com:jordanhubbard/mac.git",
            "mac",
            "[]",
            1,
            60,
            json.dumps({"repository_contract": mutable}),
            now,
            now,
        ),
    )
    store.execute(
        "INSERT INTO work_packages ("
        "id, project, repository_id, goal, state, current_plan_version, "
        "current_epoch, metadata, created_by, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "wp_activation",
            "mac",
            "repo_activation",
            "activation gate",
            "draft",
            0,
            0,
            "{}",
            "planner",
            now,
            now,
        ),
    )
    store.execute(
        "INSERT INTO work_package_plan_versions ("
        "package_id, version, definition, plan_digest, reason, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "wp_activation",
            1,
            "{}",
            "sha256:" + "1" * 64,
            "activation test",
            "planner",
            now,
        ),
    )
    store.execute(
        "INSERT INTO work_package_epochs ("
        "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, "
        "status, reason, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "wp_activation",
            1,
            1,
            "refs/heads/main",
            "a" * 40,
            "active",
            "activation test",
            "planner",
            now,
        ),
    )
    store.execute(
        "UPDATE work_packages SET state = 'admitted', current_plan_version = 1, "
        "current_epoch = 1 WHERE id = 'wp_activation'"
    )
    store.execute(
        "UPDATE work_packages SET state = 'active' WHERE id = 'wp_activation'"
    )
    certification = WorkPackageCertificationService(store)
    resolver = RepositoryPipelineReleaseGateResolver(
        store,
        validate_certification_contract=certification.validate_repository_contract,
        landing_config=LandingServiceConfig(enabled=True),
    )
    snapshot = PipelineSnapshot(
        key="wp_activation:1:1:integrate",
        package_id="wp_activation",
        plan_version=1,
        epoch=1,
        integration_node_key="integrate",
        integration_task_id="task_integrate",
        integration_node_state="ready",
        certification_node_key="certify",
        certification_task_id="task_certify",
        certification_node_state="waiting",
    )

    gate = resolver.resolve(snapshot)

    assert gate.ready is False
    assert gate.code == "certification_contract_unavailable"
