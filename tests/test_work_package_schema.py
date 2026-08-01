from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from mac.models import ValidationError, WorkPackageEpoch
from mac.store import Store, StoreError
from mac.test_support import all_index_names, column_names, ephemeral_dsn, ephemeral_store, store_on, table_names
from mac.work_package_store import (
    get_work_package_task_link,
    guard_generic_task_mutation,
    swap_work_package_epoch,
)


WORK_PACKAGE_TABLES = {
    "work_packages",
    "work_package_plan_versions",
    "work_package_epochs",
    "work_package_task_links",
    "work_package_node_lineage",
    "work_package_assignment_audit",
    "work_package_node_candidates",
    "work_package_wip_tokens",
    "work_package_integration_batches",
    "work_package_batch_inputs",
    "work_package_certifications",
    "work_package_landing_streams",
    "work_package_landing_intents",
    "work_package_landing_attempts",
    "work_package_landing_receipts",
    "work_package_lease_expiry_repairs",
    "work_package_ref_retirement_intents",
    "work_package_ref_retirement_attempts",
    "work_package_ref_retirement_receipts",
    "work_package_history",
    "evidence_attempt_links",
    "evidence_attempt_verifications",
}


# A real timestamp: several columns are TIMESTAMPTZ and parse their input,
# so the old readable placeholder ("later") is no longer a legal value.
_LATER = "2099-01-01T00:00:00+00:00"

def _insert_package(store: Store, package_id: str = "wp_1") -> None:
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "repo_1",
            "test-repository",
            "/tmp/test-repository.git",
            "/tmp/test-repository.git",
            "mac",
            "[]",
            1,
            60,
            "{}",
            "now",
            "now",
        ),
    )
    store.execute(
        "INSERT INTO work_packages ("
        "id, repository_id, goal, state, current_plan_version, current_epoch, "
        "metadata, created_by, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            package_id,
            "repo_1",
            "coordinate work",
            "draft",
            0,
            0,
            "{}",
            "human",
            "now",
            "now",
        ),
    )
    store.execute(
        "INSERT INTO work_package_plan_versions ("
        "package_id, version, parent_version, definition, plan_digest, reason, "
        "created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (package_id, 1, None, "{}", "sha256:one", "initial", "human", "now"),
    )
    store.execute(
        "INSERT INTO work_package_epochs ("
        "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, status, reason, "
        "created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            package_id,
            1,
            1,
            "refs/heads/main",
            "a" * 40,
            "active",
            "initial",
            "human",
            "now",
        ),
    )
    store.execute(
        "UPDATE work_packages SET state = ?, current_plan_version = ?, current_epoch = ? "
        "WHERE id = ?",
        ("admitted", 1, 1, package_id),
    )
    store.execute(
        "UPDATE work_packages SET state = ? WHERE id = ?",
        ("active", package_id),
    )


def _insert_task(store: Store, task_id: str) -> None:
    store.execute(
        "INSERT INTO tasks ("
        "id, title, description, priority, state, required_capabilities, dependencies, "
        "metadata, attempt_count, max_attempts, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, task_id, "", 0, "open", "[]", "[]", "{}", 0, 3, "now", "now"),
    )


def _insert_evidence(store: Store, evidence_id: str, task_id: str) -> None:
    store.execute(
        "INSERT INTO evidence ("
        "id, task_id, kind, uri, summary, metadata, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (evidence_id, task_id, "test", "memory://evidence", "ok", "{}", "agent", "now"),
    )


def _insert_attempt_link(
    store: Store,
    *,
    evidence_id: str,
    task_id: str,
    lease_id: str,
    attempt_number: int = 1,
    attempt_ref: str = "refs/mac/attempts/wp-1/e1/node-1/a1-lease-1",
    declared_effects_digest: str = "sha256:effects",
    controller_verified: bool = True,
    publish_verification: bool | None = None,
) -> None:
    store.execute(
        "INSERT INTO evidence_attempt_links ("
        "evidence_id, task_id, lease_id, agent_id, attempt_number, attempt_ref, "
        "attempt_base_sha, attempt_head_sha, artifact_digest, "
        "declared_effects_digest, observed_effects_digest, protected_ref, "
        "controller_verified, controller_verifier, controller_verified_at, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evidence_id,
            task_id,
            lease_id,
            "agent_1",
            attempt_number,
            attempt_ref,
            "a" * 40,
            "c" * 40,
            "sha256:artifact",
            declared_effects_digest,
            "sha256:observed",
            1 if controller_verified else 0,
            1 if controller_verified else 0,
            "controller" if controller_verified else None,
            "now" if controller_verified else None,
            "now",
        ),
    )
    assignment = store.query_one(
        "SELECT 1 FROM work_package_assignment_audit WHERE lease_id = ?",
        (lease_id,),
    )
    should_publish = (
        controller_verified if publish_verification is None else publish_verification
    )
    if should_publish and assignment is not None:
        receipt_hash = hashlib.sha256(evidence_id.encode("utf-8")).hexdigest()
        store.execute(
            "INSERT INTO evidence_attempt_verifications ("
            "id, evidence_id, task_id, lease_id, agent_id, attempt_number, "
            "repository_id, attempt_ref, attempt_base_sha, attempt_head_sha, "
            "tree_digest, declared_effects_digest, observed_effects_digest, "
            "changed_paths, changes, verifier, verifier_version, verified_at, "
            "receipt_digest"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "verification_" + evidence_id,
                evidence_id,
                task_id,
                lease_id,
                "agent_1",
                attempt_number,
                "repo_1",
                attempt_ref,
                "a" * 40,
                "c" * 40,
                "sha256:" + "1" * 64,
                declared_effects_digest,
                "sha256:" + "2" * 64,
                "[]",
                "[]",
                "git-attempt-output",
                "work-package-output-verifier-v1",
                "now",
                "sha256:" + receipt_hash,
            ),
        )


def _insert_link_and_assignment(
    store: Store,
    *,
    task_id: str = "task_node",
    lease_id: str = "lease_1",
    node_key: str = "node_1",
    link_effects_digest: str = "sha256:effects",
    assignment_effects_digest: str = "sha256:effects",
) -> None:
    _insert_task(store, task_id)
    store.execute(
        "INSERT INTO work_package_task_links ("
        "task_id, package_id, plan_version, epoch, node_key, node_generation, "
        "declared_effects_digest, contract_digest, input_digest, node_state, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            "wp_1",
            1,
            1,
            node_key,
            1,
            link_effects_digest,
            "sha256:contract",
            "sha256:input",
            "planned",
            "now",
        ),
    )
    store.execute(
        "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
        ("ready", task_id),
    )
    store.execute(
        "INSERT INTO leases ("
        "id, task_id, agent_id, expires_at, status, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lease_id, task_id, "agent_1", _LATER, "active", "now", "now"),
    )
    store.execute(
        "INSERT INTO work_package_assignment_audit ("
        "lease_id, package_id, plan_version, epoch, node_key, task_id, agent_id, "
        "attempt_number, attempt_ref, attempt_base_ref, attempt_base_sha, "
        "declared_effects_digest, allocator, allocator_version, score, rationale, "
        "decision, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lease_id,
            "wp_1",
            1,
            1,
            node_key,
            task_id,
            "agent_1",
            1,
            "refs/mac/attempts/wp-1/e1/node-1/a1-lease-1",
            "refs/heads/main",
            "a" * 40,
            assignment_effects_digest,
            "allocator",
            "v1",
            1.0,
            "best eligible worker",
            "{}",
            "now",
        ),
    )


def test_executable_ordinary_task_cannot_be_linked_into_package() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        _insert_task(store, "legacy_claimed_task")
        store.execute(
            "INSERT INTO leases ("
            "id, task_id, agent_id, expires_at, status, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy_claimed_lease",
                "legacy_claimed_task",
                "legacy_agent",
                _LATER,
                "active",
                "now",
                "now",
            ),
        )
        store.execute(
            "UPDATE tasks SET state = 'claimed', owner_agent_id = ?, lease_id = ?, "
            "leased_until = ?, attempt_count = 1 WHERE id = ?",
            (
                "legacy_agent",
                "legacy_claimed_lease",
                _LATER,
                "legacy_claimed_task",
            ),
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="executable task cannot be linked without package claim authority",
        ):
            store.execute(
                "INSERT INTO work_package_task_links ("
                "task_id, package_id, plan_version, epoch, node_key, "
                "node_generation, declared_effects_digest, contract_digest, "
                "input_digest, node_state, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy_claimed_task",
                    "wp_1",
                    1,
                    1,
                    "legacy_node",
                    1,
                    "sha256:legacy-effects",
                    "sha256:legacy-contract",
                    "sha256:legacy-input",
                    "planned",
                    "now",
                ),
            )
        assert store.query_one(
            "SELECT 1 FROM work_package_task_links WHERE task_id = ?",
            ("legacy_claimed_task",),
        ) is None
    finally:
        store.close()


def test_a_fresh_database_has_all_work_package_tables_and_indexes() -> None:
    store = ephemeral_store()
    try:
        tables = table_names(store)
        indexes = all_index_names(store)
        assert WORK_PACKAGE_TABLES <= tables
        assert {
            "uniq_work_package_active_epoch",
            "idx_work_package_task_links_package",
            "idx_work_package_batches_queue",
            "idx_work_package_certifications_status",
            "idx_work_package_landing_stream_lease",
            "idx_work_package_landing_receipts_target",
            "idx_work_package_expiry_repairs_node",
            "idx_work_package_ref_retirement_due",
            "idx_work_package_ref_retirement_attempts_intent",
            "idx_evidence_attempt_verifications_lease",
            "idx_work_package_history_package",
        } <= indexes
    finally:
        store.close()


def test_ref_retirement_intent_is_append_only_and_failed_attempt_is_retryable() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        _insert_link_and_assignment(store)
        store.execute(
            "INSERT INTO work_package_ref_retirement_intents ("
            "id, repository_id, ref_kind, ref, expected_sha, task_id, batch_id, "
            "terminal_state, terminal_at, eligible_after, created_by, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "intent_1",
                "repo_1",
                "attempt",
                "refs/mac/attempts/wp-1/e1/node-1/a1-lease-1",
                "c" * 40,
                "task_node",
                None,
                "candidate:rejected",
                "2026-01-01T00:00:00+00:00",
                "2026-01-08T00:00:00+00:00",
                "reconciler",
                "2026-01-08T00:00:00+00:00",
            ),
        )
        for attempt_id in ("attempt_failed_1", "attempt_failed_2"):
            store.execute(
                "INSERT INTO work_package_ref_retirement_attempts ("
                "id, intent_id, outcome, error, created_at) VALUES (?, ?, ?, ?, ?)",
                (attempt_id, "intent_1", "failed", "remote unavailable", _LATER),
            )
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_ref_retirement_attempts "
            "WHERE intent_id = ?",
            ("intent_1",),
        )["n"] == 2
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_ref_retirement_receipts"
        )["n"] == 0
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="immutable"):
            store.execute(
                "UPDATE work_package_ref_retirement_intents SET expected_sha = ? "
                "WHERE id = ?",
                ("d" * 40, "intent_1"),
            )
        store.execute(
            "INSERT INTO work_package_ref_retirement_receipts ("
            "id, intent_id, outcome, completed_at) VALUES (?, ?, ?, ?)",
            ("receipt_1", "intent_1", "missing", _LATER),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="append-only"):
            store.execute(
                "DELETE FROM work_package_ref_retirement_receipts WHERE id = ?",
                ("receipt_1",),
            )
    finally:
        store.close()


def test_package_state_and_single_active_epoch_are_database_invariants() -> None:
    store = ephemeral_store()
    try:
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(
                "INSERT INTO work_packages ("
                "id, goal, state, metadata, created_by, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("wp_bad", "bad", "running-ish", "{}", "human", "now", "now"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(
                "INSERT INTO work_packages ("
                "id, goal, state, metadata, created_by, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("wp_zero", "bad", "active", "{}", "human", "now", "now"),
            )

        _insert_package(store)
        epoch_model = WorkPackageEpoch(
            **dict(
                store.query_one(
                    "SELECT * FROM work_package_epochs WHERE package_id = ? AND epoch = ?",
                    ("wp_1", 1),
                )
            )
        )
        assert epoch_model.planning_base_sha == "a" * 40
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(
                "INSERT INTO work_package_epochs ("
                "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, status, reason, "
                "created_by, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "wp_1",
                    2,
                    1,
                    "refs/heads/main",
                    "b" * 40,
                    "active",
                    "retry",
                    "human",
                    "now",
                ),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="deactivate"):
            store.execute(
                "UPDATE work_package_epochs SET status = ? "
                "WHERE package_id = ? AND epoch = ?",
                ("superseded", "wp_1", 1),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="state transition"):
            store.execute(
                "UPDATE work_packages SET state = ? WHERE id = ?", ("draft", "wp_1")
            )
        store.execute(
            "UPDATE work_packages SET state = ? WHERE id = ?", ("completed", "wp_1")
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="state transition"):
            store.execute(
                "UPDATE work_packages SET state = ? WHERE id = ?", ("active", "wp_1")
            )
    finally:
        store.close()


def test_plan_versions_form_an_immutable_parent_chain() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        store.execute(
            "INSERT INTO work_package_plan_versions ("
            "package_id, version, parent_version, definition, plan_digest, reason, "
            "created_by, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("wp_1", 2, 1, "{}", "sha256:two", "replan", "human", "now"),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(
                "INSERT INTO work_package_plan_versions ("
                "package_id, version, parent_version, definition, plan_digest, reason, "
                "created_by, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("wp_1", 3, 2, "{}", "sha256:one", "duplicate", "human", "now"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(
                "UPDATE work_package_plan_versions SET parent_version = 2 "
                "WHERE package_id = ? AND version = 1",
                ("wp_1",),
            )
    finally:
        store.close()


def test_epoch_swap_is_atomic_and_failed_swap_rolls_back() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        store.execute(
            "INSERT INTO work_package_plan_versions ("
            "package_id, version, parent_version, definition, plan_digest, reason, "
            "created_by, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("wp_1", 2, 1, "{}", "sha256:two", "replan", "human", "now"),
        )
        epoch = swap_work_package_epoch(
            store,
            package_id="wp_1",
            expected_plan_version=1,
            expected_epoch=1,
            new_plan_version=2,
            new_epoch=2,
            planning_base_ref="refs/heads/main",
            planning_base_sha="b" * 40,
            actor="human",
            reason="canonical moved",
        )
        assert epoch.status == "active"
        package = store.query_one(
            "SELECT state, current_plan_version, current_epoch FROM work_packages "
            "WHERE id = ?",
            ("wp_1",),
        )
        assert dict(package) == {
            "state": "active",
            "current_plan_version": 2,
            "current_epoch": 2,
        }
        assert {
            row["epoch"]: row["status"]
            for row in store.query_all(
                "SELECT epoch, status FROM work_package_epochs WHERE package_id = ?",
                ("wp_1",),
            )
        } == {1: "superseded", 2: "active"}

        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            swap_work_package_epoch(
                store,
                package_id="wp_1",
                expected_plan_version=2,
                expected_epoch=2,
                new_plan_version=99,
                new_epoch=3,
                planning_base_ref="refs/heads/main",
                planning_base_sha="c" * 40,
                actor="human",
                reason="invalid missing plan",
            )
        package = store.query_one(
            "SELECT state, current_plan_version, current_epoch FROM work_packages "
            "WHERE id = ?",
            ("wp_1",),
        )
        assert dict(package) == {
            "state": "active",
            "current_plan_version": 2,
            "current_epoch": 2,
        }
        assert (
            store.query_one(
                "SELECT 1 FROM work_package_epochs WHERE package_id = ? AND epoch = ?",
                ("wp_1", 3),
            )
            is None
        )
    finally:
        store.close()


def test_history_epoch_and_plan_version_must_identify_the_same_snapshot() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        store.execute(
            "INSERT INTO work_package_plan_versions ("
            "package_id, version, parent_version, definition, plan_digest, reason, "
            "created_by, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("wp_1", 2, 1, "{}", "sha256:two", "replan", "human", "now"),
        )
        store.execute(
            "INSERT INTO work_package_epochs ("
            "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, "
            "status, reason, created_by, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "wp_1",
                2,
                2,
                "refs/heads/main",
                "b" * 40,
                "staged",
                "replan",
                "human",
                "now",
            ),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(
                "INSERT INTO work_package_history ("
                "id, package_id, seq, event_type, actor, plan_version, epoch, detail, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("history_bad", "wp_1", 1, "bad", "human", 2, 1, "{}", "now"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(
                "INSERT INTO work_package_history ("
                "id, package_id, seq, event_type, actor, plan_version, epoch, detail, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("history_half", "wp_1", 1, "bad", "human", 1, None, "{}", "now"),
            )
    finally:
        store.close()


def test_certification_is_bound_to_exact_batch_candidate() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        _insert_task(store, "task_certify")
        _insert_task(store, "task_review")
        _insert_evidence(store, "ev_tests", "task_certify")
        _insert_evidence(store, "ev_review", "task_review")
        store.execute(
            "INSERT INTO work_package_integration_batches ("
            "id, package_id, plan_version, epoch, repository_id, target_ref, assembly_base_sha, "
            "landing_base_sha, input_digest, state, integration_task_id, lease_owner, "
            "lease_expires_at, lease_fence, metadata, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "batch_1",
                "wp_1",
                1,
                1,
                "repo_1",
                "refs/heads/main",
                "a" * 40,
                "b" * 40,
                "sha256:inputs",
                "queued",
                "task_certify",
                "integrator",
                _LATER,
                1,
                "{}",
                "now",
                "now",
            ),
        )
        store.execute(
            "UPDATE work_package_integration_batches SET state = ? WHERE id = ?",
            ("assembling", "batch_1"),
        )
        store.execute(
            "UPDATE work_package_integration_batches SET candidate_sha = ?, "
            "candidate_tree_digest = ?, candidate_ref = ?, candidate_fence = ? "
            "WHERE id = ?",
            (
                "c" * 40,
                "sha256:candidate-tree",
                "refs/mac/integration/batch_1",
                1,
                "batch_1",
            ),
        )
        cert_sql = (
            "INSERT INTO work_package_certifications ("
            "id, batch_id, package_id, plan_version, epoch, candidate_sha, "
            "assembly_base_sha, landing_base_sha, target_ref, status, "
            "verification_digest, verification, certification_task_id, "
            "tests_evidence_id, review_task_id, review_evidence_id, certified_by, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        cert_params = (
            "cert_1",
            "batch_1",
            "wp_1",
            1,
            1,
            "c" * 40,
            "a" * 40,
            "b" * 40,
            "refs/heads/main",
            "passed",
            "sha256:tests",
            "{}",
            "task_certify",
            "ev_tests",
            "task_review",
            "ev_review",
            "agent_test",
            "now",
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="verifying batch"):
            store.execute(cert_sql, cert_params)
        store.execute(
            "UPDATE work_package_integration_batches SET state = ? WHERE id = ?",
            ("verifying", "batch_1"),
        )
        store.execute(cert_sql, cert_params)
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="identity"):
            store.execute(
                "UPDATE work_package_certifications SET verification_digest = ? "
                "WHERE id = ?",
                ("sha256:forged", "cert_1"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(
                "INSERT INTO work_package_certifications ("
                "id, batch_id, package_id, plan_version, epoch, candidate_sha, "
                "assembly_base_sha, landing_base_sha, target_ref, status, "
                "verification_digest, verification, certification_task_id, "
                "tests_evidence_id, review_task_id, review_evidence_id, "
                "certified_by, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "cert_bad",
                    "batch_1",
                    "wp_1",
                    1,
                    1,
                    "d" * 40,
                    "a" * 40,
                    "b" * 40,
                    "refs/heads/main",
                    "passed",
                    "sha256:other",
                    "{}",
                    "task_certify",
                    "ev_tests",
                    "task_review",
                    "ev_review",
                    "agent_test",
                    "now",
                ),
            )
    finally:
        store.close()


def test_generic_mutation_guard_and_task_link_identity_are_fail_closed() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        _insert_link_and_assignment(store)
        link = get_work_package_task_link(store, "task_node")
        assert link is not None
        assert link.declared_effects_digest == "sha256:effects"
        with pytest.raises(ValidationError, match="package-aware transaction"):
            guard_generic_task_mutation(store, "task_node", "update_task")
        guard_generic_task_mutation(store, "ordinary_task", "update_task")
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="identity is immutable"):
            store.execute(
                "UPDATE work_package_task_links SET input_digest = ? WHERE task_id = ?",
                ("sha256:changed", "task_node"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="immutable"):
            store.execute(
                "UPDATE work_package_assignment_audit SET attempt_ref = ? "
                "WHERE lease_id = ?",
                ("refs/mac/forged", "lease_1"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            _insert_link_and_assignment(
                store,
                task_id="task_bad_digest",
                lease_id="lease_bad_digest",
                node_key="node_bad_digest",
                assignment_effects_digest="sha256:forged",
            )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("executing", "task_node"),
        )
    finally:
        store.close()


def test_wip_token_survives_execution_lease_expiry() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        _insert_link_and_assignment(store)
        store.execute(
            "INSERT INTO work_package_wip_tokens ("
            "id, package_id, plan_version, epoch, node_key, task_id, resource_key, "
            "token_kind, stage, state, generation, capacity_units, "
            "acquired_by_assignment_lease_id, acquired_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "wip_1",
                "wp_1",
                1,
                1,
                "node_1",
                "task_node",
                "repo:mac:slot:1",
                "mutation",
                "mutation",
                "held",
                1,
                1,
                "lease_1",
                "now",
            ),
        )
        store.execute(
            "UPDATE leases SET status = ? WHERE id = ?", ("expired", "lease_1")
        )
        assert (
            store.query_one(
                "SELECT state FROM work_package_wip_tokens WHERE id = ?", ("wip_1",)
            )["state"]
            == "held"
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(
                "INSERT INTO work_package_wip_tokens ("
                "id, package_id, plan_version, epoch, node_key, task_id, resource_key, "
                "token_kind, stage, state, generation, capacity_units, "
                "acquired_by_assignment_lease_id, acquired_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "wip_conflict",
                    "wp_1",
                    1,
                    1,
                    "node_1",
                    "task_node",
                    "repo:mac:slot:1",
                    "mutation",
                    "candidate_buffer",
                    "held",
                    2,
                    1,
                    "lease_1",
                    "now",
                ),
            )
        store.execute(
            "UPDATE work_package_wip_tokens SET state = ?, released_at = ?, "
            "release_reason = ? WHERE id = ?",
            ("released", "now", "candidate buffered", "wip_1"),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(
                "UPDATE work_package_wip_tokens SET state = ? WHERE id = ?",
                ("held", "wip_1"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="metadata"):
            store.execute(
                "UPDATE work_package_wip_tokens SET release_reason = ? WHERE id = ?",
                ("rewritten", "wip_1"),
            )
    finally:
        store.close()


def _prepare_expired_package_execution(
    store: Store,
    *,
    target_state: str,
) -> str:
    _insert_package(store)
    _insert_link_and_assignment(store)
    store.execute(
        "UPDATE work_package_task_links SET node_state = 'executing' "
        "WHERE task_id = 'task_node'"
    )
    store.execute(
        "UPDATE tasks SET state = 'running', owner_agent_id = 'agent_1', "
        "lease_id = 'lease_1', leased_until = 'later', attempt_count = 1 "
        "WHERE id = 'task_node'"
    )
    store.execute(
        "INSERT INTO work_package_wip_tokens ("
        "id, package_id, plan_version, epoch, node_key, task_id, resource_key, "
        "token_kind, stage, state, generation, capacity_units, "
        "acquired_by_assignment_lease_id, acquired_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "wip_1",
            "wp_1",
            1,
            1,
            "node_1",
            "task_node",
            "repo:mac:slot:1",
            "mutation",
            "mutation",
            "held",
            1,
            1,
            "lease_1",
            "now",
        ),
    )
    decision = (
        '{"schema":"mac.lease_expiry_decision.v1","lease_id":"lease_1",'
        '"task_id":"task_node","target_state":"%s",'
        '"reset_attempt_count":false,"detail":{}}' % target_state
    )
    store.execute(
        "UPDATE leases SET status = 'expired', expiry_finalizer_token = 'fence_1', "
        "expiry_finalizer_claimed_at = 'now', expiry_finalization_decision = ? "
        "WHERE id = 'lease_1'",
        (decision,),
    )
    return decision


def _insert_expiry_repair(
    store: Store,
    *,
    decision: str,
    target_task_state: str,
) -> None:
    terminal = target_task_state in {"failed", "cancelled"}
    store.execute(
        "INSERT INTO work_package_lease_expiry_repairs ("
        "id, lease_id, package_id, plan_version, epoch, node_key, "
        "node_generation, task_id, agent_id, attempt_number, source_task_state, "
        "target_task_state, source_node_state, target_node_state, "
        "wip_disposition, held_wip_count, held_wip_ids, finalizer_token, "
        "decision, decision_digest, reason, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "repair_lease_1",
            "lease_1",
            "wp_1",
            1,
            1,
            "node_1",
            1,
            "task_node",
            "agent_1",
            1,
            "running",
            target_task_state,
            "executing",
            "cancelled" if terminal else "ready",
            "cancel" if terminal else "retain",
            1,
            '["wip_1"]',
            "fence_1",
            decision,
            "sha256:" + hashlib.sha256(decision.encode("utf-8")).hexdigest(),
            "lease expired under dispatcher fence",
            "dispatcher",
            "now",
        ),
    )


def test_expired_package_task_requeue_requires_receipt_and_retains_exact_wip() -> None:
    store = ephemeral_store()
    try:
        decision = _prepare_expired_package_execution(store, target_state="open")
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="repair receipt"):
            store.execute(
                "UPDATE tasks SET state = 'open', owner_agent_id = NULL, "
                "lease_id = NULL, leased_until = NULL WHERE id = 'task_node'"
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="exact lease-expiry repair"):
            store.execute(
                "UPDATE work_package_task_links SET node_state = 'ready' "
                "WHERE task_id = 'task_node'"
            )

        _insert_expiry_repair(
            store,
            decision=decision,
            target_task_state="open",
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="exact lease-expiry repair"):
            store.execute(
                "UPDATE work_package_task_links SET node_state = 'ready' "
                "WHERE task_id = 'task_node'"
            )
        store.execute(
            "UPDATE tasks SET state = 'open', owner_agent_id = NULL, lease_id = NULL, "
            "leased_until = NULL WHERE id = 'task_node'"
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = 'ready' "
            "WHERE task_id = 'task_node'"
        )
        assert store.query_one(
            "SELECT state FROM work_package_wip_tokens WHERE id = 'wip_1'"
        )["state"] == "held"
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="immutable"):
            store.execute(
                "UPDATE work_package_lease_expiry_repairs SET reason = 'forged' "
                "WHERE id = 'repair_lease_1'"
            )
    finally:
        store.close()


def test_terminal_expiry_repair_cancels_wip_before_node() -> None:
    store = ephemeral_store()
    try:
        decision = _prepare_expired_package_execution(store, target_state="failed")
        _insert_expiry_repair(
            store,
            decision=decision,
            target_task_state="failed",
        )
        store.execute(
            "UPDATE tasks SET state = 'failed', owner_agent_id = NULL, lease_id = NULL, "
            "leased_until = NULL WHERE id = 'task_node'"
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="cancel exact held WIP"):
            store.execute(
                "UPDATE work_package_task_links SET node_state = 'cancelled' "
                "WHERE task_id = 'task_node'"
            )
        store.execute(
            "UPDATE work_package_wip_tokens SET state = 'cancelled', "
            "released_at = 'now', release_reason = 'terminal lease expiry' "
            "WHERE id = 'wip_1'"
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = 'cancelled' "
            "WHERE task_id = 'task_node'"
        )
        store.execute(
            "UPDATE leases SET expiry_finalized_at = 'now', "
            "expiry_finalizer_token = NULL WHERE id = 'lease_1'"
        )
        assert store.query_one(
            "SELECT state FROM work_package_wip_tokens WHERE id = 'wip_1'"
        )["state"] == "cancelled"
        assert store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = 'task_node'"
        )["node_state"] == "cancelled"
    finally:
        store.close()


def test_batch_input_is_bound_to_exact_assignment_and_evidence_attempt() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        _insert_link_and_assignment(store)
        _insert_evidence(store, "ev_attempt", "task_node")
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="does not match"):
            _insert_attempt_link(
                store,
                evidence_id="ev_attempt",
                task_id="task_node",
                lease_id="lease_1",
                attempt_ref="refs/mac/forged",
            )
        _insert_attempt_link(
            store,
            evidence_id="ev_attempt",
            task_id="task_node",
            lease_id="lease_1",
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="immutable"):
            store.execute(
                "UPDATE evidence_attempt_links SET attempt_ref = ? WHERE evidence_id = ?",
                ("refs/mac/other", "ev_attempt"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="immutable"):
            store.execute(
                "UPDATE evidence_attempt_verifications SET verifier = ? "
                "WHERE evidence_id = ?",
                ("forged", "ev_attempt"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="append-only"):
            store.execute(
                "DELETE FROM evidence_attempt_verifications WHERE evidence_id = ?",
                ("ev_attempt",),
            )
        store.execute(
            "INSERT INTO work_package_integration_batches ("
            "id, package_id, plan_version, epoch, repository_id, target_ref, assembly_base_sha, "
            "landing_base_sha, input_digest, state, metadata, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "batch_inputs",
                "wp_1",
                1,
                1,
                "repo_1",
                "refs/heads/main",
                "a" * 40,
                "b" * 40,
                "sha256:inputs",
                "queued",
                "{}",
                "now",
                "now",
            ),
        )
        store.execute(
            "INSERT INTO work_package_node_candidates ("
            "id, task_id, package_id, plan_version, epoch, node_key, node_generation, "
            "assignment_lease_id, attempt_number, evidence_id, status, submitted_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "candidate_1",
                "task_node",
                "wp_1",
                1,
                1,
                "node_1",
                1,
                "lease_1",
                1,
                "ev_attempt",
                "submitted",
                "now",
            ),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("executing", "task_node"),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("candidate_submitted", "task_node"),
        )
        batch_input_sql = (
            "INSERT INTO work_package_batch_inputs ("
            "id, batch_id, package_id, plan_version, epoch, ordinal, node_key, "
            "node_generation, task_id, candidate_id, candidate_status, "
            "assignment_lease_id, attempt_number, evidence_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        batch_input_params = (
            "input_1",
            "batch_inputs",
            "wp_1",
            1,
            1,
            0,
            "node_1",
            1,
            "task_node",
            "candidate_1",
            "accepted",
            "lease_1",
            1,
            "ev_attempt",
            "now",
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(batch_input_sql, batch_input_params)
        store.execute(
            "UPDATE work_package_node_candidates SET status = ?, accepted_at = ?, "
            "accepted_by = ? WHERE id = ?",
            ("accepted", "now", "integrator", "candidate_1"),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("candidate_accepted", "task_node"),
        )
        store.execute(batch_input_sql, batch_input_params)
        provenance = store.query_one(
            "SELECT verification.attempt_ref, verification.tree_digest, "
            "verification.attempt_head_sha, verification.declared_effects_digest, "
            "verification.observed_effects_digest "
            "FROM work_package_batch_inputs AS input "
            "JOIN evidence_attempt_verifications AS verification "
            "ON verification.evidence_id = input.evidence_id "
            "WHERE input.id = ?",
            ("input_1",),
        )
        assert dict(provenance) == {
            "attempt_ref": "refs/mac/attempts/wp-1/e1/node-1/a1-lease-1",
            "tree_digest": "sha256:" + "1" * 64,
            "attempt_head_sha": "c" * 40,
            "declared_effects_digest": "sha256:effects",
            "observed_effects_digest": "sha256:" + "2" * 64,
        }
        batch_input_columns = {
            row["name"]
            for row in [{"name": c} for c in column_names(store, "work_package_batch_inputs")]
        }
        assert (
            not {
                "attempt_ref",
                "artifact_digest",
                "attempt_head_sha",
                "declared_effects_digest",
                "observed_effects_digest",
            }
            & batch_input_columns
        )
        store.execute(
            "UPDATE work_package_integration_batches SET state = ? WHERE id = ?",
            ("assembling", "batch_inputs"),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="state transition"):
            store.execute(
                "UPDATE work_package_integration_batches SET state = ? WHERE id = ?",
                ("queued", "batch_inputs"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="immutable"):
            store.execute(
                "UPDATE work_package_batch_inputs SET ordinal = 2 WHERE id = ?",
                ("input_1",),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="immutable"):
            store.execute(
                "DELETE FROM work_package_batch_inputs WHERE id = ?", ("input_1",)
            )
    finally:
        store.close()


def test_integration_batch_fence_is_monotonic() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        store.execute(
            "INSERT INTO work_package_integration_batches ("
            "id, package_id, plan_version, epoch, repository_id, target_ref, assembly_base_sha, "
            "landing_base_sha, input_digest, state, lease_owner, lease_expires_at, "
            "lease_fence, metadata, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "batch_fence",
                "wp_1",
                1,
                1,
                "repo_1",
                "refs/heads/main",
                "a" * 40,
                "b" * 40,
                "sha256:inputs",
                "queued",
                "integrator_1",
                _LATER,
                4,
                "{}",
                "now",
                "now",
            ),
        )
        store.execute(
            "UPDATE work_package_integration_batches SET state = ? WHERE id = ?",
            ("assembling", "batch_fence"),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="identity"):
            store.execute(
                "UPDATE work_package_integration_batches SET target_ref = ? WHERE id = ?",
                ("refs/heads/other", "batch_fence"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="fence cannot decrease"):
            store.execute(
                "UPDATE work_package_integration_batches SET lease_fence = 3 WHERE id = ?",
                ("batch_fence",),
            )
        store.execute(
            "UPDATE work_package_integration_batches "
            "SET lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
            ("batch_fence",),
        )
        assert (
            store.query_one(
                "SELECT lease_fence FROM work_package_integration_batches WHERE id = ?",
                ("batch_fence",),
            )["lease_fence"]
            == 4
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="new fence"):
            store.execute(
                "UPDATE work_package_integration_batches "
                "SET lease_owner = ?, lease_expires_at = ? WHERE id = ?",
                ("integrator_2", _LATER, "batch_fence"),
            )
        store.execute(
            "UPDATE work_package_integration_batches "
            "SET lease_owner = ?, lease_expires_at = ?, lease_fence = ? WHERE id = ?",
            ("integrator_2", _LATER, 5, "batch_fence"),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="current fence"):
            store.execute(
                "UPDATE work_package_integration_batches SET candidate_sha = ?, "
                "candidate_tree_digest = ?, candidate_ref = ?, candidate_fence = ? "
                "WHERE id = ?",
                (
                    "c" * 40,
                    "sha256:tree",
                    "refs/mac/integration/batch_fence",
                    4,
                    "batch_fence",
                ),
            )
        store.execute(
            "UPDATE work_package_integration_batches SET candidate_sha = ?, "
            "candidate_tree_digest = ?, candidate_ref = ?, candidate_fence = ? "
            "WHERE id = ?",
            (
                "c" * 40,
                "sha256:tree",
                "refs/mac/integration/batch_fence",
                5,
                "batch_fence",
            ),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="current fence"):
            store.execute(
                "UPDATE work_package_integration_batches SET candidate_sha = ? "
                "WHERE id = ?",
                ("d" * 40, "batch_fence"),
            )
        store.execute(
            "UPDATE work_package_integration_batches SET state = ? WHERE id = ?",
            ("verifying", "batch_fence"),
        )
    finally:
        store.close()


def test_node_candidate_requires_same_task_lease_and_evidence_attempt() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        _insert_link_and_assignment(store)
        _insert_evidence(store, "ev_candidate", "task_node")
        _insert_attempt_link(
            store,
            evidence_id="ev_candidate",
            task_id="task_node",
            lease_id="lease_1",
        )

        _insert_task(store, "task_other")
        store.execute(
            "INSERT INTO leases ("
            "id, task_id, agent_id, expires_at, status, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("lease_other", "task_other", "agent_1", _LATER, "expired", "now", "now"),
        )
        _insert_evidence(store, "ev_other", "task_other")
        _insert_attempt_link(
            store,
            evidence_id="ev_other",
            task_id="task_other",
            lease_id="lease_other",
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError)):
            store.execute(
                "INSERT INTO work_package_node_candidates ("
                "id, task_id, package_id, plan_version, epoch, node_key, node_generation, "
                "assignment_lease_id, attempt_number, evidence_id, status, submitted_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "candidate_bad",
                    "task_node",
                    "wp_1",
                    1,
                    1,
                    "node_1",
                    1,
                    "lease_1",
                    1,
                    "ev_other",
                    "submitted",
                    "now",
                ),
            )

        store.execute(
            "INSERT INTO work_package_node_candidates ("
            "id, task_id, package_id, plan_version, epoch, node_key, node_generation, "
            "assignment_lease_id, attempt_number, evidence_id, status, submitted_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "candidate_1",
                "task_node",
                "wp_1",
                1,
                1,
                "node_1",
                1,
                "lease_1",
                1,
                "ev_candidate",
                "submitted",
                "now",
            ),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("executing", "task_node"),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("candidate_submitted", "task_node"),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="metadata"):
            store.execute(
                "UPDATE work_package_node_candidates SET status = ?, accepted_at = ? "
                "WHERE id = ?",
                ("accepted", "now", "candidate_1"),
            )
        store.execute(
            "UPDATE work_package_node_candidates SET status = ?, accepted_at = ?, "
            "accepted_by = ? WHERE id = ?",
            ("accepted", "now", "integrator", "candidate_1"),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("candidate_accepted", "task_node"),
        )
        assert get_work_package_task_link(store, "task_node").node_state == (
            "candidate_accepted"
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="metadata"):
            store.execute(
                "UPDATE work_package_node_candidates SET accepted_by = ? WHERE id = ?",
                ("forged", "candidate_1"),
            )
    finally:
        store.close()


def test_candidate_acceptance_ignores_legacy_mutable_verification_claims() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        _insert_link_and_assignment(store)
        _insert_evidence(store, "ev_unverified", "task_node")
        _insert_attempt_link(
            store,
            evidence_id="ev_unverified",
            task_id="task_node",
            lease_id="lease_1",
            controller_verified=True,
            publish_verification=False,
        )
        store.execute(
            "INSERT INTO work_package_node_candidates ("
            "id, task_id, package_id, plan_version, epoch, node_key, node_generation, "
            "assignment_lease_id, attempt_number, evidence_id, status, submitted_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "candidate_unverified",
                "task_node",
                "wp_1",
                1,
                1,
                "node_1",
                1,
                "lease_1",
                1,
                "ev_unverified",
                "submitted",
                "now",
            ),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("executing", "task_node"),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("candidate_submitted", "task_node"),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="controller-verified"):
            store.execute(
                "UPDATE work_package_node_candidates SET status = ?, accepted_at = ?, "
                "accepted_by = ? WHERE id = ?",
                ("accepted", "now", "integrator", "candidate_unverified"),
            )
    finally:
        store.close()


def test_rejected_candidate_is_immutable_and_new_attempt_gets_new_candidate() -> None:
    store = ephemeral_store()
    try:
        _insert_package(store)
        _insert_link_and_assignment(store)
        _insert_evidence(store, "ev_attempt_1", "task_node")
        _insert_attempt_link(
            store,
            evidence_id="ev_attempt_1",
            task_id="task_node",
            lease_id="lease_1",
        )
        store.execute(
            "INSERT INTO work_package_node_candidates ("
            "id, task_id, package_id, plan_version, epoch, node_key, node_generation, "
            "assignment_lease_id, attempt_number, evidence_id, status, submitted_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "candidate_attempt_1",
                "task_node",
                "wp_1",
                1,
                1,
                "node_1",
                1,
                "lease_1",
                1,
                "ev_attempt_1",
                "submitted",
                "now",
            ),
        )
        for state in ("executing", "candidate_submitted"):
            store.execute(
                "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
                (state, "task_node"),
            )
        store.execute(
            "UPDATE work_package_node_candidates SET status = ?, rejection_reason = ? "
            "WHERE id = ?",
            ("rejected", "tests failed", "candidate_attempt_1"),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("rejected", "task_node"),
        )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="newer bounded assignment"):
            store.execute(
                "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
                ("executing", "task_node"),
            )
        with pytest.raises((StoreError, sqlite3.IntegrityError), match="metadata"):
            store.execute(
                "UPDATE work_package_node_candidates SET rejection_reason = ? WHERE id = ?",
                ("forged", "candidate_attempt_1"),
            )

        store.execute(
            "UPDATE leases SET status = ? WHERE id = ?", ("expired", "lease_1")
        )
        store.execute(
            "INSERT INTO leases ("
            "id, task_id, agent_id, expires_at, status, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("lease_2", "task_node", "agent_1", _LATER, "active", "now", "now"),
        )
        store.execute(
            "INSERT INTO work_package_assignment_audit ("
            "lease_id, package_id, plan_version, epoch, node_key, task_id, agent_id, "
            "attempt_number, attempt_ref, attempt_base_ref, attempt_base_sha, "
            "declared_effects_digest, allocator, allocator_version, score, rationale, "
            "decision, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "lease_2",
                "wp_1",
                1,
                1,
                "node_1",
                "task_node",
                "agent_1",
                2,
                "refs/mac/attempts/wp-1/e1/node-1/a2-lease-2",
                "refs/heads/main",
                "a" * 40,
                "sha256:effects",
                "allocator",
                "v1",
                1.0,
                "bounded rework",
                "{}",
                "now",
            ),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("executing", "task_node"),
        )
        _insert_evidence(store, "ev_attempt_2", "task_node")
        _insert_attempt_link(
            store,
            evidence_id="ev_attempt_2",
            task_id="task_node",
            lease_id="lease_2",
            attempt_number=2,
            attempt_ref="refs/mac/attempts/wp-1/e1/node-1/a2-lease-2",
        )
        store.execute(
            "INSERT INTO work_package_node_candidates ("
            "id, task_id, package_id, plan_version, epoch, node_key, node_generation, "
            "assignment_lease_id, attempt_number, evidence_id, status, submitted_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "candidate_attempt_2",
                "task_node",
                "wp_1",
                1,
                1,
                "node_1",
                1,
                "lease_2",
                2,
                "ev_attempt_2",
                "submitted",
                _LATER,
            ),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = ? WHERE task_id = ?",
            ("candidate_submitted", "task_node"),
        )
        rows = store.query_all(
            "SELECT id, status FROM work_package_node_candidates "
            "WHERE task_id = ? ORDER BY attempt_number",
            ("task_node",),
        )
        assert [dict(row) for row in rows] == [
            {"id": "candidate_attempt_1", "status": "rejected"},
            {"id": "candidate_attempt_2", "status": "submitted"},
        ]
    finally:
        store.close()


def test_an_existing_authority_acquires_the_work_package_schema() -> None:
    dsn = ephemeral_dsn()
    store = store_on(dsn)
    for table in (
        "work_package_certifications",
        "work_package_batch_inputs",
        "work_package_wip_tokens",
        "work_package_node_candidates",
        "work_package_assignment_audit",
        "work_package_node_lineage",
        "work_package_history",
        "work_package_integration_batches",
        "work_package_task_links",
        "work_package_epochs",
        "work_package_plan_versions",
        "work_packages",
        "evidence_attempt_links",
    ):
        store.execute("DROP TABLE IF EXISTS %s CASCADE" % table)
    store.close()

    upgraded = store_on(dsn, initialize=True)
    try:
        tables = table_names(upgraded)
        assert WORK_PACKAGE_TABLES <= tables
    finally:
        upgraded.close()


def test_postgres_schema_contains_equivalent_work_package_contract() -> None:
    root = Path(__file__).resolve().parent.parent
    schema = (root / "src" / "mac" / "data" / "postgres" / "schema.sql").read_text()
    for table in WORK_PACKAGE_TABLES:
        assert "CREATE TABLE IF NOT EXISTS %s (" % table in schema
    assert "uniq_work_package_active_epoch" in schema
    assert "score DOUBLE PRECISION" in schema
    assert "REFERENCES work_package_integration_batches (" in schema
    assert "candidate_sha, assembly_base_sha, landing_base_sha, target_ref" in schema
    assert "WHERE id = parent_batch_id\n      FOR UPDATE" in schema
    assert "cert cannot race a verifying -> terminal transition" in schema
    work_packages_ddl = schema.split("CREATE TABLE IF NOT EXISTS work_packages (", 1)[
        1
    ].split(");", 1)[0]
    assert "UNIQUE (id, repository_id)" in work_packages_ddl
    assert "trg_work_package_batch_fence_monotonic" in schema
