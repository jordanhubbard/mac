from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

import pytest

from mac.models import TransitionError, json_dumps
from mac.store import SQLiteStore
from mac.work_package_publication_finalizer import (
    PublicationReceiptError,
    StalePublicationError,
    WorkPackagePublicationFinalizer,
)
from mac.work_package_pipeline import (
    PipelineSnapshot,
    WorkPackagePipelineConfig,
    WorkPackagePipelineController,
)


NOW = "2026-07-17T12:00:00.000000+00:00"
LATER = "2026-07-17T12:01:00.000000+00:00"
FUTURE = "2099-01-01T00:00:00.000000+00:00"
TARGET_REF = "refs/heads/main"
ATTEMPT_REF = "refs/mac/attempts/wp-final/e1/mutate/a1-lease-mutate"
CANDIDATE_REF = "refs/mac/integration/wp-final/e1/assemble/batch-final"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
TREE_DIGEST = "git-tree:" + "3" * 40


def _digest(label: str) -> str:
    return "sha256:%s" % hashlib.sha256(label.encode("utf-8")).hexdigest()


def _task_metadata(node_key: str, node_type: str) -> str:
    return json_dumps(
        {
            "no_dispatch": True,
            "work_package": {
                "schema": "mac.work_package.task.v1",
                "package_id": "wp_final",
                "plan_version": 1,
                "epoch": 1,
                "node_key": node_key,
                "node_generation": 1,
                "node_type": node_type,
                "planning_base_ref": TARGET_REF,
                "planning_base_sha": BASE_SHA,
            },
        }
    )


def _insert_task(
    store: SQLiteStore,
    task_id: str,
    node_key: str,
    node_type: str,
    *,
    state: str,
    dependencies: list[str],
) -> None:
    store.execute(
        "INSERT INTO tasks (id, title, description, priority, state, "
        "required_capabilities, dependencies, metadata, attempt_count, max_attempts, "
        "created_at, updated_at, completed_at) "
        "VALUES (?, ?, '', 0, ?, '[]', ?, ?, 0, 3, ?, ?, ?)",
        (
            task_id,
            task_id,
            state,
            json_dumps(dependencies),
            _task_metadata(node_key, node_type),
            NOW,
            NOW,
            NOW if state == "completed" else None,
        ),
    )
    store.execute(
        "INSERT INTO work_package_task_links (task_id, package_id, plan_version, "
        "epoch, node_key, node_generation, declared_effects_digest, contract_digest, "
        "input_digest, node_state, created_at) "
        "VALUES (?, 'wp_final', 1, 1, ?, 1, ?, ?, ?, 'planned', ?)",
        (
            task_id,
            node_key,
            _digest("effects:" + node_key),
            _digest("contract:" + node_key),
            _digest("input:" + node_key),
            NOW,
        ),
    )


def _append_completed_transition(
    store: SQLiteStore,
    task_id: str,
    station_kind: str,
    receipt_id: str,
) -> None:
    detail = json_dumps(
        {
            "schema": "mac.work_package.controller_task_transition.v1",
            "station_kind": station_kind,
            "controller_station_receipt_id": receipt_id,
        }
    )
    store.execute(
        "INSERT INTO task_history (id, task_id, event_type, actor, from_state, "
        "to_state, detail, created_at) VALUES (?, ?, 'task.transitioned', "
        "'controller', 'waiting', 'completed', ?, ?)",
        ("history_" + station_kind, task_id, detail, NOW),
    )
    store.execute(
        "INSERT INTO task_transition_outbox (id, task_id, event_type, actor, "
        "from_state, to_state, detail, status, attempts, created_at) "
        "VALUES (?, ?, 'task.lifecycle', 'controller', 'waiting', 'completed', ?, "
        "'pending', 0, ?)",
        ("outbox_" + station_kind, task_id, detail, NOW),
    )


def _seed(
    *, unfinished: bool = False, wip_tamper: str | None = None
) -> SQLiteStore:
    if wip_tamper not in {None, "acceptance_provenance", "mutation_lineage"}:
        raise ValueError("unsupported WIP tamper fixture")
    store = SQLiteStore(":memory:")
    store.execute(
        "INSERT INTO project_repositories (id, name, path, source, project, "
        "required_capabilities, enabled, poll_interval_seconds, metadata, created_at, "
        "updated_at) VALUES ('repo_final', 'final', '/tmp/final', '/tmp/final', "
        "'mac', '[]', 1, 60, '{}', ?, ?)",
        (NOW, NOW),
    )
    store.execute(
        "INSERT INTO work_packages (id, project, repository_id, goal, state, "
        "current_plan_version, current_epoch, metadata, created_by, created_at, "
        "updated_at) VALUES ('wp_final', 'mac', 'repo_final', 'final product', "
        "'draft', 0, 0, '{}', 'planner', ?, ?)",
        (NOW, NOW),
    )
    nodes: list[dict[str, Any]] = [
        {"node_key": "mutate", "kind": "mutation", "depends_on": []},
        {
            "node_key": "assemble",
            "kind": "integration",
            "depends_on": ["mutate"],
        },
        {
            "node_key": "certify",
            "kind": "certification",
            "depends_on": ["assemble"],
        },
    ]
    if unfinished:
        nodes.append(
            {
                "node_key": "postcheck",
                "kind": "analysis",
                "depends_on": ["certify"],
            }
        )
    definition = {
        "schema": "mac.work_package.plan.v1",
        "package_id": "wp_final",
        "repository_id": "repo_final",
        "planning_base_ref": TARGET_REF,
        "planning_base_sha": BASE_SHA,
        "nodes": nodes,
    }
    store.execute(
        "INSERT INTO work_package_plan_versions (package_id, version, definition, "
        "plan_digest, reason, created_by, created_at) "
        "VALUES ('wp_final', 1, ?, ?, 'test', 'planner', ?)",
        (json_dumps(definition), _digest("plan-1"), NOW),
    )
    store.execute(
        "INSERT INTO work_package_epochs (package_id, epoch, plan_version, "
        "planning_base_ref, planning_base_sha, status, reason, created_by, created_at) "
        "VALUES ('wp_final', 1, 1, ?, ?, 'active', 'test', 'planner', ?)",
        (TARGET_REF, BASE_SHA, NOW),
    )
    store.execute(
        "UPDATE work_packages SET state = 'admitted', current_plan_version = 1, "
        "current_epoch = 1 WHERE id = 'wp_final'"
    )
    store.execute("UPDATE work_packages SET state = 'active' WHERE id = 'wp_final'")

    _insert_task(
        store, "task_mutate", "mutate", "mutation", state="completed", dependencies=[]
    )
    _insert_task(
        store,
        "task_integration",
        "assemble",
        "integration",
        state="waiting",
        dependencies=["task_mutate"],
    )
    _insert_task(
        store,
        "task_certification",
        "certify",
        "certification",
        state="waiting",
        dependencies=["task_integration"],
    )
    if unfinished:
        _insert_task(
            store,
            "task_postcheck",
            "postcheck",
            "analysis",
            state="waiting",
            dependencies=["task_certification"],
        )

    # Exact accepted mutation input.
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'ready' "
        "WHERE task_id = 'task_mutate'"
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'executing' "
        "WHERE task_id = 'task_mutate'"
    )
    store.execute(
        "INSERT INTO leases (id, task_id, agent_id, expires_at, status, created_at, "
        "updated_at) VALUES ('lease_mutate', 'task_mutate', 'agent_mutate', ?, "
        "'active', ?, ?)",
        (FUTURE, NOW, NOW),
    )
    effects = _digest("effects:mutate")
    store.execute(
        "INSERT INTO work_package_assignment_audit (lease_id, package_id, "
        "plan_version, epoch, node_key, task_id, agent_id, attempt_number, "
        "attempt_ref, attempt_base_ref, attempt_base_sha, declared_effects_digest, "
        "allocator, allocator_version, score, rationale, decision, created_at) "
        "VALUES ('lease_mutate', 'wp_final', 1, 1, 'mutate', 'task_mutate', "
        "'agent_mutate', 1, ?, ?, ?, ?, 'test', 'v1', 1.0, 'test', '{}', ?)",
        (ATTEMPT_REF, TARGET_REF, BASE_SHA, effects, NOW),
    )
    store.execute(
        "INSERT INTO evidence (id, task_id, kind, uri, summary, metadata, created_by, "
        "created_at) VALUES ('evidence_mutate', 'task_mutate', 'artifact', "
        "'artifact://mutate', 'exact output', '{}', 'agent_mutate', ?)",
        (NOW,),
    )
    store.execute(
        "INSERT INTO evidence_attempt_links (evidence_id, task_id, lease_id, agent_id, "
        "attempt_number, attempt_ref, attempt_base_sha, attempt_head_sha, "
        "artifact_digest, declared_effects_digest, protected_ref, created_at) "
        "VALUES ('evidence_mutate', 'task_mutate', 'lease_mutate', 'agent_mutate', "
        "1, ?, ?, ?, ?, ?, 1, ?)",
        (ATTEMPT_REF, BASE_SHA, HEAD_SHA, _digest("artifact"), effects, NOW),
    )
    store.execute(
        "INSERT INTO evidence_attempt_verifications (id, evidence_id, task_id, "
        "lease_id, agent_id, attempt_number, repository_id, attempt_ref, "
        "attempt_base_sha, attempt_head_sha, tree_digest, declared_effects_digest, "
        "observed_effects_digest, changed_paths, changes, verifier, verifier_version, "
        "verified_at, receipt_digest) VALUES ('verify_mutate', 'evidence_mutate', "
        "'task_mutate', 'lease_mutate', 'agent_mutate', 1, 'repo_final', ?, ?, ?, ?, "
        "?, ?, '[\"product.txt\"]', '[]', 'controller', 'v1', ?, ?)",
        (
            ATTEMPT_REF,
            BASE_SHA,
            HEAD_SHA,
            _digest("tree"),
            effects,
            _digest("observed-effects"),
            NOW,
            _digest("verification-receipt"),
        ),
    )
    store.execute(
        "INSERT INTO work_package_node_candidates (id, task_id, package_id, "
        "plan_version, epoch, node_key, node_generation, assignment_lease_id, "
        "attempt_number, evidence_id, status, submitted_at) VALUES "
        "('candidate_mutate', 'task_mutate', 'wp_final', 1, 1, 'mutate', 1, "
        "'lease_mutate', 1, 'evidence_mutate', 'submitted', ?)",
        (NOW,),
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'candidate_submitted' "
        "WHERE task_id = 'task_mutate'"
    )
    store.execute(
        "UPDATE work_package_node_candidates SET status = 'accepted', accepted_at = ?, "
        "accepted_by = 'review-controller' WHERE id = 'candidate_mutate'",
        (NOW,),
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'candidate_accepted' "
        "WHERE task_id = 'task_mutate'"
    )

    # Frozen batch plus the exact WIP transfer chain.
    store.execute(
        "INSERT INTO work_package_integration_batches (id, package_id, plan_version, "
        "epoch, repository_id, target_ref, assembly_base_sha, landing_base_sha, "
        "input_digest, state, integration_task_id, metadata, created_at, updated_at) "
        "VALUES ('batch_final', 'wp_final', 1, 1, 'repo_final', ?, ?, ?, ?, "
        "'queued', 'task_integration', ?, ?, ?)",
        (
            TARGET_REF,
            BASE_SHA,
            BASE_SHA,
            _digest("batch-input"),
            json_dumps(
                {
                    "schema": "mac.work_package.integration_batch.v1",
                    "integration_node_key": "assemble",
                }
            ),
            NOW,
            NOW,
        ),
    )
    store.execute(
        "INSERT INTO work_package_batch_inputs (id, batch_id, package_id, "
        "plan_version, epoch, ordinal, node_key, node_generation, task_id, "
        "candidate_id, candidate_status, assignment_lease_id, attempt_number, "
        "evidence_id, created_at) VALUES ('input_mutate', 'batch_final', 'wp_final', "
        "1, 1, 0, 'mutate', 1, 'task_mutate', 'candidate_mutate', 'accepted', "
        "'lease_mutate', 1, 'evidence_mutate', ?)",
        (NOW,),
    )
    store.execute(
        "INSERT INTO work_package_wip_tokens (id, package_id, plan_version, epoch, "
        "node_key, task_id, resource_key, token_kind, stage, state, generation, "
        "capacity_units, reservation_key, acquired_by_assignment_lease_id, acquired_at) "
        "VALUES ('wip_mutation', 'wp_final', 1, 1, 'mutate', 'task_mutate', "
        "'repo:path:product.txt', 'mutation', 'mutation', 'held', 1, 1, "
        "'assignment:lease_mutate', 'lease_mutate', ?)",
        (NOW,),
    )
    store.execute(
        "UPDATE work_package_wip_tokens SET state = 'released', released_at = ?, "
        "release_reason = ? "
        "WHERE id = 'wip_mutation'",
        (
            NOW,
            "candidate_transfer:%s"
            % (
                "candidate_other"
                if wip_tamper == "mutation_lineage"
                else "candidate_mutate"
            ),
        ),
    )
    store.execute(
        "INSERT INTO work_package_wip_tokens (id, package_id, plan_version, epoch, "
        "node_key, task_id, resource_key, token_kind, stage, state, generation, "
        "capacity_units, reservation_key, predecessor_token_id, "
        "acquired_by_assignment_lease_id, acquired_at) VALUES ('wip_candidate', "
        "'wp_final', 1, 1, 'mutate', 'task_mutate', 'repo:path:product.txt', "
        "'mutation', 'candidate_buffer', 'held', 2, 1, 'assignment:lease_mutate', "
        "'wip_mutation', 'lease_mutate', ?)",
        (NOW,),
    )
    store.execute(
        "UPDATE work_package_wip_tokens SET state = 'released', released_at = ?, "
        "release_reason = ? "
        "WHERE id = 'wip_candidate'",
        (
            NOW,
            json_dumps(
                {
                    "schema": "mac.work_package.wip_resolution.v1",
                    "decision": "accepted",
                    "candidate_id": (
                        "candidate_other"
                        if wip_tamper == "acceptance_provenance"
                        else "candidate_mutate"
                    ),
                    "evidence_id": "evidence_mutate",
                    "actor": "acceptance-controller",
                    "successor_token_id": "wip_fan_in",
                    "resolved_at": NOW,
                }
            ),
        ),
    )
    store.execute(
        "INSERT INTO work_package_wip_tokens (id, package_id, plan_version, epoch, "
        "node_key, task_id, resource_key, token_kind, stage, state, generation, "
        "capacity_units, reservation_key, predecessor_token_id, "
        "acquired_by_assignment_lease_id, acquired_at) VALUES ('wip_fan_in', "
        "'wp_final', 1, 1, 'mutate', 'task_mutate', 'repo:path:product.txt', "
        "'mutation', 'fan_in_reservation', 'held', 3, 1, "
        "'assignment:lease_mutate', 'wip_candidate', 'lease_mutate', ?)",
        (NOW,),
    )
    store.execute(
        "UPDATE work_package_wip_tokens SET state = 'released', released_at = ?, "
        "release_reason = 'integration_transfer:batch_final' "
        "WHERE id = 'wip_fan_in'",
        (NOW,),
    )
    store.execute(
        "INSERT INTO work_package_wip_tokens (id, package_id, plan_version, epoch, "
        "node_key, task_id, resource_key, token_kind, stage, state, generation, "
        "capacity_units, reservation_key, predecessor_token_id, "
        "acquired_by_assignment_lease_id, acquired_at) VALUES ('wip_integration', "
        "'wp_final', 1, 1, 'mutate', 'task_mutate', 'repo:path:product.txt', "
        "'mutation', 'integration', 'held', 4, 1, 'batch_final', 'wip_fan_in', "
        "'lease_mutate', ?)",
        (NOW,),
    )

    store.execute(
        "UPDATE work_package_integration_batches SET state = 'assembling', "
        "lease_owner = 'integrator', lease_expires_at = ?, lease_fence = 1 "
        "WHERE id = 'batch_final'",
        (FUTURE,),
    )
    store.execute(
        "UPDATE work_package_integration_batches SET candidate_sha = ?, "
        "candidate_tree_digest = ?, candidate_ref = ?, candidate_fence = 1 "
        "WHERE id = 'batch_final'",
        (HEAD_SHA, TREE_DIGEST, CANDIDATE_REF),
    )
    store.execute(
        "UPDATE work_package_integration_batches SET state = 'verifying', "
        "lease_owner = NULL, lease_expires_at = NULL WHERE id = 'batch_final'"
    )

    # Integration receipt authorizes the controller-only terminal link.
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'ready' "
        "WHERE task_id = 'task_integration'"
    )
    store.execute(
        "INSERT INTO work_package_controller_station_receipts (id, station_kind, "
        "task_id, package_id, plan_version, epoch, node_key, batch_id, outcome, "
        "provenance_digest, actor, detail, created_at) VALUES ('station_integration', "
        "'integration', 'task_integration', 'wp_final', 1, 1, 'assemble', "
        "'batch_final', 'integrated', ?, 'integrator', '{}', ?)",
        (_digest("station-integration"), NOW),
    )
    store.execute(
        "UPDATE tasks SET state = 'completed', completed_at = ?, updated_at = ? "
        "WHERE id = 'task_integration'",
        (NOW, NOW),
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'integrated' "
        "WHERE task_id = 'task_integration'"
    )
    _append_completed_transition(
        store, "task_integration", "integration", "station_integration"
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'ready' "
        "WHERE task_id = 'task_certification'"
    )

    # Durable external certification job, result, and controller station receipt.
    job_definition = {
        "certification_task_id": "task_certification",
        "certification_node_key": "certify",
    }
    store.execute(
        "INSERT INTO work_package_certification_jobs (id, batch_id, package_id, "
        "plan_version, epoch, repository_id, candidate_sha, candidate_tree_digest, "
        "candidate_ref, candidate_fence, assembly_base_sha, landing_base_sha, "
        "target_ref, policy_id, policy_version, policy_checksum, image_ref, "
        "image_digest, bundle_digest, commands_digest, job_digest, definition, state, "
        "created_at, updated_at) VALUES ('job_final', 'batch_final', 'wp_final', 1, "
        "1, 'repo_final', ?, ?, ?, 1, ?, ?, ?, 'policy-final', 1, ?, ?, ?, ?, ?, ?, "
        "?, 'queued', ?, ?)",
        (
            HEAD_SHA,
            TREE_DIGEST,
            CANDIDATE_REF,
            BASE_SHA,
            BASE_SHA,
            TARGET_REF,
            _digest("policy"),
            "image@" + _digest("image"),
            _digest("image"),
            _digest("bundle"),
            _digest("commands"),
            _digest("job"),
            json_dumps(job_definition),
            NOW,
            NOW,
        ),
    )
    store.execute(
        "UPDATE work_package_certification_jobs SET state = 'running', "
        "lease_owner = 'certifier', lease_expires_at = ?, lease_fence = 1, "
        "updated_at = ? WHERE id = 'job_final'",
        (FUTURE, NOW),
    )
    store.execute(
        "INSERT INTO evidence (id, task_id, kind, uri, summary, checksum, metadata, "
        "created_by, created_at) VALUES ('evidence_cert_tests', 'task_certification', "
        "'test', 'certification://job_final/tests', 'passed', ?, '{}', 'certifier', ?)",
        (_digest("cert-result"), NOW),
    )
    store.execute(
        "INSERT INTO evidence (id, task_id, kind, uri, summary, checksum, metadata, "
        "created_by, created_at) VALUES ('evidence_cert_review', 'task_certification', "
        "'review', 'certification://job_final/result', 'passed', ?, '{}', "
        "'certifier', ?)",
        (_digest("cert-result"), NOW),
    )
    store.execute(
        "INSERT INTO work_package_certifications (id, batch_id, package_id, "
        "plan_version, epoch, candidate_sha, assembly_base_sha, landing_base_sha, "
        "target_ref, status, verification_digest, verification, certification_task_id, "
        "tests_evidence_id, review_task_id, review_evidence_id, certified_by, "
        "created_at) VALUES ('cert_final', 'batch_final', 'wp_final', 1, 1, ?, ?, ?, "
        "?, 'passed', ?, '{}', 'task_certification', 'evidence_cert_tests', "
        "'task_certification', 'evidence_cert_review', 'certifier', ?)",
        (HEAD_SHA, BASE_SHA, BASE_SHA, TARGET_REF, _digest("cert-result"), NOW),
    )
    store.execute(
        "UPDATE work_package_certification_jobs SET state = 'completed', "
        "lease_owner = NULL, lease_expires_at = NULL, result_digest = ?, "
        "certification_id = 'cert_final', completed_at = ?, updated_at = ? "
        "WHERE id = 'job_final'",
        (_digest("cert-result"), NOW, NOW),
    )
    store.execute(
        "INSERT INTO work_package_controller_station_receipts (id, station_kind, "
        "task_id, package_id, plan_version, epoch, node_key, batch_id, "
        "certification_job_id, certification_id, outcome, result_digest, "
        "provenance_digest, actor, detail, created_at) VALUES ('station_certification', "
        "'certification', 'task_certification', 'wp_final', 1, 1, 'certify', "
        "'batch_final', 'job_final', 'cert_final', 'certified', ?, ?, 'certifier', "
        "'{}', ?)",
        (_digest("cert-result"), _digest("station-certification"), NOW),
    )
    store.execute(
        "UPDATE tasks SET state = 'completed', completed_at = ?, updated_at = ? "
        "WHERE id = 'task_certification'",
        (NOW, NOW),
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'certified' "
        "WHERE task_id = 'task_certification'"
    )
    _append_completed_transition(
        store, "task_certification", "certification", "station_certification"
    )
    store.execute(
        "UPDATE work_package_integration_batches SET state = 'certified' "
        "WHERE id = 'batch_final'"
    )

    # Exact landing intent/attempt/receipt, followed by its batch projection.
    store.execute(
        "INSERT INTO work_package_landing_streams (repository_id, target_ref, "
        "lease_owner, lease_expires_at, lease_fence, created_at, updated_at) "
        "VALUES ('repo_final', ?, 'lander', ?, 1, ?, ?)",
        (TARGET_REF, FUTURE, NOW, NOW),
    )
    store.execute(
        "INSERT INTO work_package_landing_intents (id, batch_id, package_id, "
        "plan_version, epoch, repository_id, target_ref, candidate_sha, candidate_ref, "
        "assembly_base_sha, landing_base_sha, certification_id, stream_fence, "
        "created_by, created_at) VALUES ('intent_final', 'batch_final', 'wp_final', "
        "1, 1, 'repo_final', ?, ?, ?, ?, ?, 'cert_final', 1, 'lander', ?)",
        (TARGET_REF, HEAD_SHA, CANDIDATE_REF, BASE_SHA, BASE_SHA, NOW),
    )
    store.execute(
        "INSERT INTO work_package_landing_attempts (id, intent_id, attempt_number, "
        "repository_id, target_ref, candidate_sha, expected_remote_sha, stream_fence, "
        "created_by, created_at) VALUES ('attempt_final', 'intent_final', 1, "
        "'repo_final', ?, ?, ?, 1, 'lander', ?)",
        (TARGET_REF, HEAD_SHA, BASE_SHA, NOW),
    )
    store.execute(
        "INSERT INTO work_package_landing_receipts (id, intent_id, attempt_id, "
        "batch_id, repository_id, target_ref, candidate_sha, observed_sha, recovered, "
        "recovery, attempt_stream_fence, recording_stream_fence, recorded_by, "
        "recorded_at, receipt_digest) VALUES ('landing_final', 'intent_final', "
        "'attempt_final', 'batch_final', 'repo_final', ?, ?, ?, 0, '', 1, 1, "
        "'lander', ?, ?)",
        (TARGET_REF, HEAD_SHA, HEAD_SHA, NOW, _digest("landing-receipt")),
    )
    store.execute(
        "UPDATE work_package_integration_batches SET state = 'published', "
        "completed_at = ?, updated_at = ? WHERE id = 'batch_final'",
        (NOW, NOW),
    )
    assert store.query_all("PRAGMA foreign_key_check") == []
    return store


def _service(store: SQLiteStore, *, fault_hook=None) -> WorkPackagePublicationFinalizer:
    return WorkPackagePublicationFinalizer(
        store,
        now=lambda: LATER,
        fault_hook=fault_hook,
    )


def test_finalizes_exact_landing_receipt_and_is_idempotent() -> None:
    store = _seed()
    try:
        first = _service(store).finalize_landed_batch(
            "batch_final", actor="pipeline", receipt_id="landing_final"
        )
        assert first.created is True
        assert first.package_state == "completed"
        assert first.epoch_status == "completed"
        assert first.released_wip_ids == ("wip_integration",)
        assert first.controller_station_receipt_ids == (
            "station_integration",
            "station_certification",
        )
        assert store.query_one(
            "SELECT state FROM work_packages WHERE id = 'wp_final'"
        )["state"] == "completed"
        assert store.query_one(
            "SELECT status FROM work_package_epochs WHERE package_id = 'wp_final' "
            "AND epoch = 1"
        )["status"] == "completed"
        wip = store.query_one(
            "SELECT state, predecessor_token_id, release_reason "
            "FROM work_package_wip_tokens WHERE id = 'wip_integration'"
        )
        assert dict(wip) == {
            "state": "released",
            "predecessor_token_id": "wip_fan_in",
            "release_reason": "publication_finalized:landing_final",
        }
        counts = {
            "finalizations": store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_publication_finalizations"
            )["n"],
            "package_history": store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_history "
                "WHERE event_type = 'work_package.publication_finalized'"
            )["n"],
            "task_history": store.query_one(
                "SELECT COUNT(*) AS n FROM task_history "
                "WHERE event_type = 'task.work_package_product_finalized'"
            )["n"],
        }
        second = _service(store).finalize_landed_batch(
            "batch_final", actor="pipeline-retry", receipt_id="landing_final"
        )
        assert second.created is False
        assert second.finalization_id == first.finalization_id
        assert {
            "finalizations": store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_publication_finalizations"
            )["n"],
            "package_history": store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_history "
                "WHERE event_type = 'work_package.publication_finalized'"
            )["n"],
            "task_history": store.query_one(
                "SELECT COUNT(*) AS n FROM task_history "
                "WHERE event_type = 'task.work_package_product_finalized'"
            )["n"],
        } == counts == {
            "finalizations": 1,
            "package_history": 1,
            "task_history": 2,
        }
        assert store.query_all("PRAGMA foreign_key_check") == []
    finally:
        store.close()


def test_result_projection_passes_pipeline_product_finalization_validator() -> None:
    store = _seed()
    try:
        result = _service(store).finalize_landed_batch(
            "batch_final", actor="pipeline"
        )

        class _Finalization:
            def finalize_landed_batch(self, batch_id: str, *, actor: str):
                assert batch_id == "batch_final"
                assert actor == "pipeline-test"
                return result

        controller = WorkPackagePipelineController(
            inventory=object(),  # type: ignore[arg-type]
            release_gates=object(),  # type: ignore[arg-type]
            bundles=object(),  # type: ignore[arg-type]
            integration=object(),  # type: ignore[arg-type]
            certification=object(),  # type: ignore[arg-type]
            landing=object(),  # type: ignore[arg-type]
            finalization=_Finalization(),
            rejection=object(),  # type: ignore[arg-type]
            config=WorkPackagePipelineConfig(actor="pipeline-test"),
        )
        snapshot = PipelineSnapshot(
            key="wp_final:1:1:assemble",
            package_id="wp_final",
            plan_version=1,
            epoch=1,
            integration_node_key="assemble",
            integration_task_id="task_integration",
            integration_node_state="integrated",
            certification_node_key="certify",
            certification_task_id="task_certification",
            certification_node_state="certified",
            batch_id="batch_final",
            batch_state="published",
            certification_job_id="job_final",
            certification_job_state="completed",
            certification_id="cert_final",
        )

        outcome = controller._finalize_product(snapshot)

        assert outcome.status == "advanced"
        assert outcome.code == "station_advanced"
        assert outcome.detail["station_status"] == "completed"
        assert result.to_dict()["held_wip_count"] == 0
        assert result.to_dict()["provenance_verified"] is True
    finally:
        store.close()


def test_fault_after_wip_release_rolls_back_and_retry_completes() -> None:
    store = _seed()
    try:
        def crash(stage: str, _detail: dict[str, Any]) -> None:
            if stage == "after_release":
                raise RuntimeError("simulated controller crash")

        with pytest.raises(RuntimeError, match="simulated controller crash"):
            _service(store, fault_hook=crash).finalize_landed_batch(
                "batch_final", actor="pipeline"
            )
        assert store.query_one(
            "SELECT state FROM work_package_wip_tokens WHERE id = 'wip_integration'"
        )["state"] == "held"
        assert store.query_one(
            "SELECT state FROM work_packages WHERE id = 'wp_final'"
        )["state"] == "active"
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_publication_finalizations"
        )["n"] == 0

        result = _service(store).finalize_landed_batch(
            "batch_final", actor="pipeline-recovery"
        )
        assert result.created is True
    finally:
        store.close()


def test_rejects_wrong_receipt_missing_wip_and_unfinished_graph() -> None:
    wrong = _seed()
    try:
        with pytest.raises(PublicationReceiptError, match="does not belong"):
            _service(wrong).finalize_landed_batch(
                "batch_final", actor="pipeline", receipt_id="landing_other"
            )
        assert wrong.query_one(
            "SELECT state FROM work_package_wip_tokens WHERE id = 'wip_integration'"
        )["state"] == "held"
    finally:
        wrong.close()

    missing = _seed()
    try:
        missing.execute(
            "UPDATE work_package_wip_tokens SET state = 'released', released_at = ?, "
            "release_reason = 'unexpected_consumer' WHERE id = 'wip_integration'",
            (LATER,),
        )
        with pytest.raises(TransitionError, match="integration WIP"):
            _service(missing).finalize_landed_batch(
                "batch_final", actor="pipeline"
            )
        assert missing.query_one(
            "SELECT state FROM work_packages WHERE id = 'wp_final'"
        )["state"] == "active"
    finally:
        missing.close()

    unfinished = _seed(unfinished=True)
    try:
        with pytest.raises(TransitionError, match="unfinished nodes"):
            _service(unfinished).finalize_landed_batch(
                "batch_final", actor="pipeline"
            )
        assert unfinished.query_one(
            "SELECT state FROM work_package_wip_tokens WHERE id = 'wip_integration'"
        )["state"] == "held"
        assert unfinished.query_one(
            "SELECT state FROM work_packages WHERE id = 'wp_final'"
        )["state"] == "active"
    finally:
        unfinished.close()


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("acceptance_provenance", "exact acceptance provenance"),
        ("mutation_lineage", "exact mutation WIP lineage"),
    ],
)
def test_rejects_tampered_acceptance_or_mutation_wip_lineage(
    tamper: str, message: str
) -> None:
    store = _seed(wip_tamper=tamper)
    try:
        with pytest.raises(TransitionError, match=message):
            _service(store).finalize_landed_batch(
                "batch_final", actor="pipeline"
            )
        assert store.query_one(
            "SELECT state FROM work_package_wip_tokens WHERE id = 'wip_integration'"
        )["state"] == "held"
        assert store.query_one(
            "SELECT state FROM work_packages WHERE id = 'wp_final'"
        )["state"] == "active"
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_publication_finalizations"
        )["n"] == 0
    finally:
        store.close()


def test_rejects_stale_epoch_and_competing_batch() -> None:
    stale = _seed()
    try:
        stale.execute("UPDATE work_packages SET state = 'replanning' WHERE id = 'wp_final'")
        stale.execute(
            "INSERT INTO work_package_plan_versions (package_id, version, parent_version, "
            "definition, plan_digest, reason, created_by, created_at) "
            "SELECT package_id, 2, 1, definition, ?, 'replan', 'planner', ? "
            "FROM work_package_plan_versions WHERE package_id = 'wp_final' AND version = 1",
            (_digest("plan-2"), LATER),
        )
        stale.execute(
            "UPDATE work_package_epochs SET status = 'superseded', superseded_at = ? "
            "WHERE package_id = 'wp_final' AND epoch = 1",
            (LATER,),
        )
        stale.execute(
            "INSERT INTO work_package_epochs (package_id, epoch, plan_version, "
            "planning_base_ref, planning_base_sha, status, reason, created_by, created_at) "
            "VALUES ('wp_final', 2, 2, ?, ?, 'active', 'replan', 'planner', ?)",
            (TARGET_REF, HEAD_SHA, LATER),
        )
        stale.execute(
            "UPDATE work_packages SET state = 'active', current_plan_version = 2, "
            "current_epoch = 2 WHERE id = 'wp_final'"
        )
        with pytest.raises(StalePublicationError, match="active package"):
            _service(stale).finalize_landed_batch(
                "batch_final", actor="pipeline"
            )
    finally:
        stale.close()

    competing = _seed()
    try:
        competing.execute(
            "INSERT INTO work_package_integration_batches (id, package_id, "
            "plan_version, epoch, repository_id, target_ref, assembly_base_sha, "
            "landing_base_sha, input_digest, state, integration_task_id, metadata, "
            "created_at, updated_at) VALUES ('batch_competing', 'wp_final', 1, 1, "
            "'repo_final', ?, ?, ?, ?, 'queued', 'task_integration', ?, ?, ?)",
            (
                TARGET_REF,
                BASE_SHA,
                BASE_SHA,
                _digest("competing"),
                json_dumps({"integration_node_key": "assemble"}),
                LATER,
                LATER,
            ),
        )
        with pytest.raises(TransitionError, match="competing actionable batch"):
            _service(competing).finalize_landed_batch(
                "batch_final", actor="pipeline"
            )
        assert competing.query_one(
            "SELECT state FROM work_package_wip_tokens WHERE id = 'wip_integration'"
        )["state"] == "held"
    finally:
        competing.close()


def test_append_only_receipts_and_projection_tamper_are_detected() -> None:
    store = _seed()
    try:
        result = _service(store).finalize_landed_batch(
            "batch_final", actor="pipeline"
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.execute(
                "UPDATE work_package_publication_finalizations SET finalized_by = 'x' "
                "WHERE id = ?",
                (result.finalization_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.execute(
                "DELETE FROM work_package_publication_finalizations WHERE id = ?",
                (result.finalization_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.execute(
                "UPDATE work_package_landing_receipts SET recorded_by = 'x' "
                "WHERE id = 'landing_final'"
            )

        store.execute(
            "UPDATE work_package_integration_batches SET metadata = ? "
            "WHERE id = 'batch_final'",
            (json_dumps({"integration_node_key": "assemble"}),),
        )
        with pytest.raises(PublicationReceiptError, match="projection"):
            _service(store).finalize_landed_batch(
                "batch_final", actor="pipeline-retry"
            )
    finally:
        store.close()
