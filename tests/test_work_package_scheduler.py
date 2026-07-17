from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from mac.landing_service import LandingServiceConfig
from mac.models import TransitionError, ValidationError
from mac.services import ControlPlane
from mac.store import SQLiteStore
from mac.work_package_pipeline import WorkPackagePipelineConfig
from mac.work_package_pipeline_runtime import WorkPackagePipelineRuntimeConfig
from mac.work_package_models import WORK_PACKAGE_PLAN_SCHEMA
from mac.work_package_scheduler import WorkPackageClaimGate
from mac.work_package_service import RepositoryBaseAttestation, WorkPackageService
from mac.worker_credentials import (
    AUTHENTICATED_PROOF_SCHEMA,
    MODE_COMPATIBILITY,
    PACKAGE_CAPABILITY,
    RESOURCE_PROOF_SCHEMA,
    WorkerCredentialLifecycle,
    build_readiness_inventory,
    write_policy_state,
)


class _Verifier:
    def verify(self, repository, *, planning_base_ref, planning_base_sha):
        return RepositoryBaseAttestation(
            repository_id=repository["id"],
            planning_base_ref=planning_base_ref,
            planning_base_sha=planning_base_sha,
            canonical_ref_sha=planning_base_sha,
            source_kind="test",
            verified_at="attested",
            resource_namespace={
                "status": "resolved",
                "case_sensitive": True,
                "unicode_normalization": "NFC",
                "symlink_resolution": "resolved",
            },
        )


def _register_repository(store: SQLiteStore) -> None:
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "projectrepo_mac",
            "mac",
            "/tmp/mac",
            "git@example.invalid:mac.git",
            "mac",
            "[]",
            1,
            60,
            "{}",
            "created",
            "updated",
        ),
    )


def _plan(
    *,
    package_id: str = "wp_scheduler",
    max_in_flight: int = 2,
    max_mutation_wip: int = 2,
    second_exclusive: Optional[str] = None,
    second_write: str = "src/two",
) -> dict:
    second_effects = {"writes": [second_write]}
    if second_exclusive:
        second_effects["exclusive"] = [second_exclusive]
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": package_id,
        "goal": "Run two legal mutations and assemble them",
        "project": "mac",
        "repository_id": "projectrepo_mac",
        "resource_namespace": {
            "case_sensitive": True,
            "unicode_normalization": "NFC",
            "symlink_resolution": "resolved",
        },
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": "a" * 40,
        "plan_generation": 1,
        "max_in_flight": max_in_flight,
        "mutation_wip": {"max_tokens": max_mutation_wip},
        "nodes": [
            {
                "node_key": "one",
                "title": "Mutation one",
                "node_type": "mutation",
                "effects": {"writes": ["src/one"]},
                "expected_outputs": ["one-candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            },
            {
                "node_key": "two",
                "title": "Mutation two",
                "node_type": "mutation",
                "effects": second_effects,
                "expected_outputs": ["two-candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            },
            {
                "node_key": "assemble",
                "title": "Assemble",
                "node_type": "integration",
                "depends_on": ["one", "two"],
                "expected_outputs": ["tree"],
                "verification": {"profile": "integration-default"},
            },
        ],
    }


def _admit_and_activate(
    store: SQLiteStore, plan: dict, *, register_repository: bool = True
) -> dict[str, str]:
    if register_repository:
        _register_repository(store)
    service = WorkPackageService(store, repository_verifier=_Verifier())
    result = service.admit(plan, actor="controller", reason="test")
    service.activate(
        result.package.id,
        expected_plan_version=1,
        expected_epoch=1,
        actor="operator",
    )
    return {
        row["node_key"]: row["task_id"]
        for row in store.query_all(
            "SELECT node_key, task_id FROM work_package_task_links"
        )
    }


def _provision_package_worker(store: SQLiteStore, agent_id: str) -> None:
    """Seed the exact DB authority a package claim requires.

    Credential lifecycle/install/activation behavior has its own focused tests;
    scheduler tests deliberately seed the resulting active, secret-free state.
    """

    cp = ControlPlane(
        store=store,
        secret_key="scheduler-worker-credential-test-key-0001",
    )
    agent = store.query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
    if agent is None:
        machine = cp.register_machine(
            "%s-host" % agent_id,
            machine_id="machine_%s" % agent_id,
            trusted=True,
        )
        cp.register_agent(
            machine.id,
            agent_id,
            capabilities=["python", PACKAGE_CAPABILITY],
            agent_id=agent_id,
        )
    active = store.query_one(
        "SELECT id FROM worker_credentials WHERE agent_id = ? AND state = ?",
        (agent_id, "active"),
    )
    if active is None:
        lifecycle = WorkerCredentialLifecycle(store)
        issue = lifecycle.issue(
            agent_id,
            environment="vm",
            expected_source_commit="a" * 40,
            expected_runtime_digest="sha256:scheduler-runtime",
            required_capabilities=["python", PACKAGE_CAPABILITY],
            package_capable=True,
            actor="scheduler-test",
        )
        identity = {
            "agent_id": agent_id,
            "principal_id": issue.record["id"],
            "worker_credential_version": issue.worker_version,
            "token_fingerprint": issue.record["token_fingerprint"],
        }
        resources = {
            "source_state": {
                "schema": "mac.worker_source_state.v1",
                "commit_sha": "a" * 40,
                "dirty": False,
            },
            "worker_credential": {
                "schema": RESOURCE_PROOF_SCHEMA,
                "mode": "bound",
                **identity,
            },
            "worker_credential_authenticated": {
                "schema": AUTHENTICATED_PROOF_SCHEMA,
                **identity,
            },
        }
        store.execute(
            "UPDATE worker_credentials SET state = ?, activated_at = ?, updated_at = ? "
            "WHERE id = ?",
            ("active", "activated", "activated", issue.record["id"]),
        )
        store.execute(
            "UPDATE agents SET capabilities = ?, resources = ?, running_digest = ?, "
            "status = ?, health_status = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(["python", PACKAGE_CAPABILITY]),
                json.dumps(resources),
                "sha256:scheduler-runtime",
                "idle",
                "healthy",
                "observed",
                agent_id,
            ),
        )
    lifecycle = WorkerCredentialLifecycle(store)
    inventory = build_readiness_inventory(
        [agent.to_dict() for agent in cp.list_agents()], lifecycle.records()
    )
    write_policy_state(
        MODE_COMPATIBILITY,
        inventory=inventory,
        store=store,
        actor="scheduler-test",
    )


def _claim_with_gate(
    store: SQLiteStore,
    *,
    task_id: str,
    agent_id: str,
    lease_id: str,
    provision_worker: bool = True,
) -> None:
    if provision_worker:
        _provision_package_worker(store, agent_id)
    gate = WorkPackageClaimGate()
    with store.transaction() as conn:
        task = conn.execute(
            "SELECT attempt_count FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        attempt = int(task["attempt_count"]) + 1
        conn.execute(
            "INSERT INTO leases ("
            "id, task_id, agent_id, expires_at, status, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lease_id, task_id, agent_id, "later", "active", "now", "now"),
        )
        gate.admit_claim(
            conn,
            task_id=task_id,
            agent_id=agent_id,
            lease_id=lease_id,
            attempt_number=attempt,
            now="now",
            score=1.0,
            decision={"advisor_revision": "r1"},
            prepared_task=True,
        )
        updated = conn.execute(
            "UPDATE tasks SET state = ?, owner_agent_id = ?, lease_id = ?, "
            "leased_until = ?, attempt_count = ?, updated_at = ? "
            "WHERE id = ? AND state = ? AND lease_id IS NULL",
            (
                "claimed",
                agent_id,
                lease_id,
                "later",
                attempt,
                "now",
                task_id,
                "open",
            ),
        )
        assert updated.rowcount == 1


def test_claim_gate_atomically_records_exact_assignment_and_product_wip() -> None:
    store = SQLiteStore(":memory:")
    try:
        tasks = _admit_and_activate(store, _plan())
        _claim_with_gate(
            store,
            task_id=tasks["one"],
            agent_id="agent_one",
            lease_id="lease_one",
        )

        assignment = store.query_one(
            "SELECT * FROM work_package_assignment_audit WHERE lease_id = ?",
            ("lease_one",),
        )
        assert assignment["task_id"] == tasks["one"]
        assert assignment["agent_id"] == "agent_one"
        assert assignment["attempt_number"] == 1
        assert assignment["attempt_base_sha"] == "a" * 40
        assert assignment["attempt_ref"].startswith("refs/mac/attempts/")
        link = store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (tasks["one"],),
        )
        assert link["node_state"] == "executing"
        tokens = store.query_all(
            "SELECT token_kind, stage, state FROM work_package_wip_tokens "
            "WHERE task_id = ? ORDER BY token_kind",
            (tasks["one"],),
        )
        assert {row["token_kind"] for row in tokens} == {
            "mutation_capacity",
            "writes",
        }
        assert {(row["stage"], row["state"]) for row in tokens} == {
            ("mutation", "held")
        }
        assert store.query_all("PRAGMA foreign_key_check") == []
    finally:
        store.close()


def test_linked_task_direct_sql_claim_without_assignment_is_rejected() -> None:
    """A legacy hub cannot claim a package task through the task table alone."""

    store = SQLiteStore(":memory:")
    try:
        tasks = _admit_and_activate(store, _plan())
        with pytest.raises(
            sqlite3.IntegrityError,
            match="work package task claim lacks exact assignment authority",
        ):
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO leases ("
                    "id, task_id, agent_id, expires_at, status, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "legacy_lease",
                        tasks["one"],
                        "legacy_agent",
                        "later",
                        "active",
                        "now",
                        "now",
                    ),
                )
                conn.execute(
                    "UPDATE tasks SET state = 'claimed', owner_agent_id = ?, "
                    "lease_id = ?, leased_until = ?, attempt_count = 1 "
                    "WHERE id = ? AND state = 'open'",
                    ("legacy_agent", "legacy_lease", "later", tasks["one"]),
                )
        task = store.query_one("SELECT * FROM tasks WHERE id = ?", (tasks["one"],))
        assert task["state"] == "open"
        assert task["lease_id"] is None
        assert store.query_one(
            "SELECT 1 FROM leases WHERE id = ?", ("legacy_lease",)
        ) is None
    finally:
        store.close()


def test_claim_gate_fences_unsafe_topology_after_activation() -> None:
    store = SQLiteStore(":memory:")
    try:
        tasks = _admit_and_activate(store, _plan())
        row = store.query_one(
            "SELECT definition FROM work_package_plan_versions "
            "WHERE package_id = ? AND version = 1",
            ("wp_scheduler",),
        )
        definition = json.loads(row["definition"])
        two = next(node for node in definition["nodes"] if node["node_key"] == "two")
        two["depends_on"] = ["one"]
        # Model an unsafe accepted plan surviving a mixed-version rollout.
        store.execute("DROP TRIGGER trg_work_package_plan_versions_immutable")
        store.execute(
            "UPDATE work_package_plan_versions SET definition = ? "
            "WHERE package_id = ? AND version = 1",
            (json.dumps(definition), "wp_scheduler"),
        )

        with pytest.raises(ValidationError, match="flat mutation wave"):
            _claim_with_gate(
                store,
                task_id=tasks["two"],
                agent_id="agent_two",
                lease_id="lease_unsafe",
            )

        task = store.query_one("SELECT * FROM tasks WHERE id = ?", (tasks["two"],))
        assert task["state"] == "open"
        assert task["attempt_count"] == 0
        assert store.query_one(
            "SELECT id FROM leases WHERE id = ?", ("lease_unsafe",)
        ) is None
        assert store.query_one(
            "SELECT lease_id FROM work_package_assignment_audit WHERE lease_id = ?",
            ("lease_unsafe",),
        ) is None
    finally:
        store.close()


def test_control_plane_claim_runs_package_gate_in_same_transaction() -> None:
    store = SQLiteStore(":memory:")
    try:
        tasks = _admit_and_activate(store, _plan())
        cp = ControlPlane(store=store, secret_key="scheduler-test-secret-key-value-0001")
        machine = cp.register_machine("scheduler-host")
        agent = cp.register_agent(
            machine.id,
            "scheduler-worker",
            capabilities=["python", "work_package_v1"],
        )
        with pytest.raises(TransitionError, match="downstream release gate is closed"):
            cp.claim_task(tasks["one"], agent.id, sync_beads=False)
        assert cp.get_task(tasks["one"]).attempt_count == 0

        cp._work_package_downstream_activation_readiness = lambda _described: {
            "ready": True,
            "code": "ready",
            "reason": "",
        }
        cp._work_package_downstream_release_gate = lambda *_args, **_kwargs: {
            "ready": True,
            "code": "ready",
            "reason": "",
        }
        _provision_package_worker(store, agent.id)

        claimed, lease = cp.claim_task(
            tasks["one"], agent.id, sync_beads=False
        )

        assert claimed.lease_id == lease.id
        assignment = store.query_one(
            "SELECT * FROM work_package_assignment_audit WHERE lease_id = ?",
            (lease.id,),
        )
        assert assignment is not None
        assert assignment["attempt_number"] == claimed.attempt_count == 1
        projection = claimed.metadata["work_package_assignment"]
        assert projection["schema"] == "mac.work_package.assignment_projection.v1"
        assert projection["lease_id"] == lease.id
        assert projection["task_id"] == claimed.id
        assert projection["agent_id"] == agent.id
        assert projection["attempt_ref"] == assignment["attempt_ref"]
        assert projection["attempt_base_ref"] == assignment["attempt_base_ref"]
        assert projection["attempt_base_sha"] == assignment["attempt_base_sha"]
        assert (
            projection["declared_effects_digest"]
            == assignment["declared_effects_digest"]
        )
        assert projection["attempt_number"] == assignment["attempt_number"]
        assert store.query_one(
            "SELECT node_state FROM work_package_task_links WHERE task_id = ?",
            (claimed.id,),
        )["node_state"] == "executing"
    finally:
        store.close()


def test_claim_rechecks_downstream_gate_after_preflight_before_writing_lease() -> None:
    store = SQLiteStore(":memory:")
    try:
        tasks = _admit_and_activate(store, _plan())
        cp = ControlPlane(store=store, secret_key="scheduler-race-test-key-value-0001")
        machine = cp.register_machine("scheduler-race-host")
        agent = cp.register_agent(
            machine.id,
            "scheduler-race-worker",
            capabilities=["python", "work_package_v1"],
        )
        _provision_package_worker(store, agent.id)
        cp.work_package_pipeline_runtime_config = WorkPackagePipelineRuntimeConfig(
            pipeline=WorkPackagePipelineConfig(enabled=True),
            landing=LandingServiceConfig(enabled=True),
            bundle_dir=Path("/tmp/mac-scheduler-race-bundles"),
        )

        def validate_contract(repository_id, *, source=None):
            row = (
                store.query_one(
                    "SELECT enabled FROM project_repositories WHERE id = ?",
                    (repository_id,),
                )
                if source is None
                else source.execute(
                    "SELECT enabled FROM project_repositories WHERE id = ?",
                    (repository_id,),
                ).fetchone()
            )
            if row is None or int(row["enabled"]) != 1:
                raise ValidationError("certification repository is unavailable")

        cp.work_package_certifications.validate_repository_contract = validate_contract
        original_available = cp._agent_available_for

        def close_gate_after_preflight(*args, **kwargs):
            available = original_available(*args, **kwargs)
            store.execute(
                "UPDATE project_repositories SET enabled = 0 "
                "WHERE id = 'projectrepo_mac'"
            )
            return available

        cp._agent_available_for = close_gate_after_preflight

        with pytest.raises(TransitionError, match="downstream release gate is closed"):
            cp.claim_task(tasks["one"], agent.id, sync_beads=False)

        task = store.query_one("SELECT * FROM tasks WHERE id = ?", (tasks["one"],))
        assert task["state"] == "open"
        assert task["attempt_count"] == 0
        assert task["lease_id"] is None
        assert store.query_one(
            "SELECT id FROM leases WHERE task_id = ?", (tasks["one"],)
        ) is None
        assert store.query_one(
            "SELECT lease_id FROM work_package_assignment_audit WHERE task_id = ?",
            (tasks["one"],),
        ) is None
    finally:
        store.close()


def test_claim_gate_refuses_controller_owned_station_even_if_hold_is_lost() -> None:
    store = SQLiteStore(":memory:")
    try:
        tasks = _admit_and_activate(store, _plan())
        # Simulate a mixed-version/legacy controller incorrectly exposing the
        # integration task as ordinary OPEN work.  The authoritative claim
        # transaction remains the final safety boundary.
        row = store.query_one(
            "SELECT metadata FROM tasks WHERE id = ?", (tasks["assemble"],)
        )
        metadata = json.loads(row["metadata"])
        metadata.pop("no_dispatch", None)
        store.execute(
            "UPDATE tasks SET state = 'open', metadata = ? WHERE id = ?",
            (json.dumps(metadata), tasks["assemble"]),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = 'ready' WHERE task_id = ?",
            (tasks["assemble"],),
        )

        with pytest.raises(TransitionError, match="controller-owned stations"):
            _claim_with_gate(
                store,
                task_id=tasks["assemble"],
                agent_id="agent_worker",
                lease_id="lease_controller_station",
            )

        task = store.query_one(
            "SELECT state, owner_agent_id, lease_id FROM tasks WHERE id = ?",
            (tasks["assemble"],),
        )
        assert dict(task) == {
            "state": "open",
            "owner_agent_id": None,
            "lease_id": None,
        }
        assert store.query_one(
            "SELECT id FROM leases WHERE id = ?", ("lease_controller_station",)
        ) is None
    finally:
        store.close()


def test_claim_gate_rejects_unbound_worker_even_in_compatibility_mode() -> None:
    store = SQLiteStore(":memory:")
    try:
        tasks = _admit_and_activate(store, _plan())
        with pytest.raises(TransitionError, match="package worker is missing"):
            _claim_with_gate(
                store,
                task_id=tasks["one"],
                agent_id="agent_unbound",
                lease_id="lease_unbound",
                provision_worker=False,
            )
        task = store.query_one("SELECT * FROM tasks WHERE id = ?", (tasks["one"],))
        assert task["state"] == "open"
        assert task["lease_id"] is None
    finally:
        store.close()


def test_claim_next_can_exclude_package_linked_tasks_for_legacy_identity() -> None:
    store = SQLiteStore(":memory:")
    try:
        _admit_and_activate(store, _plan())
        cp = ControlPlane(store=store, secret_key="scheduler-test-secret-key-value-0002")
        machine = cp.register_machine("legacy-host")
        agent = cp.register_agent(
            machine.id,
            "legacy-worker",
            capabilities=["python", "work_package_v1"],
        )
        ordinary = cp.create_task("ordinary fast lane")

        assignment = cp.claim_next_for_agent(
            agent.id,
            allow_package_linked=False,
            sync_beads=False,
        )

        assert assignment is not None
        assert assignment["task"]["id"] == ordinary.id
        assert store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_assignment_audit"
        )["n"] == 0
    finally:
        store.close()


def test_claim_gate_enforces_execution_capacity_in_the_claim_transaction() -> None:
    store = SQLiteStore(":memory:")
    try:
        tasks = _admit_and_activate(store, _plan(max_in_flight=1))
        _claim_with_gate(
            store,
            task_id=tasks["one"],
            agent_id="agent_one",
            lease_id="lease_one",
        )
        with pytest.raises(TransitionError, match="execution capacity"):
            _claim_with_gate(
                store,
                task_id=tasks["two"],
                agent_id="agent_two",
                lease_id="lease_two",
            )
        second = store.query_one("SELECT * FROM tasks WHERE id = ?", (tasks["two"],))
        assert second["state"] == "open"
        assert second["lease_id"] is None
        assert store.query_one(
            "SELECT id FROM leases WHERE id = ?", ("lease_two",)
        ) is None
    finally:
        store.close()


def test_claim_gate_enforces_mutation_wip_separately_from_execution_slots() -> None:
    store = SQLiteStore(":memory:")
    try:
        tasks = _admit_and_activate(
            store, _plan(max_in_flight=2, max_mutation_wip=1)
        )
        _claim_with_gate(
            store,
            task_id=tasks["one"],
            agent_id="agent_one",
            lease_id="lease_one",
        )
        with pytest.raises(TransitionError, match="mutation WIP"):
            _claim_with_gate(
                store,
                task_id=tasks["two"],
                agent_id="agent_two",
                lease_id="lease_two",
            )
    finally:
        store.close()


def test_claim_gate_serializes_hard_exclusive_effects() -> None:
    store = SQLiteStore(":memory:")
    try:
        plan = _plan(second_exclusive="src/one")
        tasks = _admit_and_activate(store, plan)
        _claim_with_gate(
            store,
            task_id=tasks["one"],
            agent_id="agent_one",
            lease_id="lease_one",
        )
        with pytest.raises(TransitionError, match="hard effect conflict"):
            _claim_with_gate(
                store,
                task_id=tasks["two"],
                agent_id="agent_two",
                lease_id="lease_two",
            )
    finally:
        store.close()


def test_claim_gate_serializes_overlapping_write_effects() -> None:
    store = SQLiteStore(":memory:")
    try:
        tasks = _admit_and_activate(store, _plan(second_write="src/one"))
        _claim_with_gate(
            store,
            task_id=tasks["one"],
            agent_id="agent_one",
            lease_id="lease_one",
        )
        with pytest.raises(TransitionError, match="hard effect conflict.*write"):
            _claim_with_gate(
                store,
                task_id=tasks["two"],
                agent_id="agent_two",
                lease_id="lease_two",
            )
    finally:
        store.close()


def test_claim_gate_serializes_hard_effects_across_packages_in_one_repository() -> None:
    store = SQLiteStore(":memory:")
    try:
        first = _plan(package_id="wp_first")
        first["nodes"][0]["effects"] = {"exclusive": ["src/shared"]}
        first_tasks = _admit_and_activate(store, first)

        second = _plan(package_id="wp_second")
        second["nodes"][0]["effects"] = {"writes": ["src/shared"]}
        second_tasks = _admit_and_activate(
            store, second, register_repository=False
        )
        _claim_with_gate(
            store,
            task_id=first_tasks["one"],
            agent_id="agent_one",
            lease_id="lease_one",
        )
        with pytest.raises(TransitionError, match="hard effect conflict"):
            _claim_with_gate(
                store,
                task_id=second_tasks["one"],
                agent_id="agent_two",
                lease_id="lease_two",
            )
    finally:
        store.close()


def test_claim_gate_rejects_a_stale_or_paused_package_epoch() -> None:
    store = SQLiteStore(":memory:")
    try:
        tasks = _admit_and_activate(store, _plan())
        store.execute(
            "UPDATE work_packages SET state = ? WHERE id = ?",
            ("paused", "wp_scheduler"),
        )
        with pytest.raises(TransitionError, match="current epoch"):
            _claim_with_gate(
                store,
                task_id=tasks["one"],
                agent_id="agent_one",
                lease_id="lease_one",
            )
        task = store.query_one("SELECT * FROM tasks WHERE id = ?", (tasks["one"],))
        assert task["state"] == "open"
        assert task["lease_id"] is None
    finally:
        store.close()


def test_claim_gate_is_a_noop_for_unmanaged_tasks() -> None:
    store = SQLiteStore(":memory:")
    try:
        with store.transaction() as conn:
            assert WorkPackageClaimGate().admit_claim(
                conn,
                task_id="ordinary",
                agent_id="agent",
                lease_id="lease",
                attempt_number=1,
                now="now",
            ) is None
    finally:
        store.close()
