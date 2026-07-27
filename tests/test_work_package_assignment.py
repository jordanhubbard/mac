from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from mac.fleet_learning import (
    build_repository_access_learning,
    build_repository_access_memory_payload,
)
from mac.models import TaskState, parse_time, utcnow
from mac.services import ControlPlane
from mac.store import SQLiteStore
from mac.work_package_assignment import (
    DispatchScoreSnapshot,
    WORK_PACKAGE_ASSIGNMENT_ADVISOR_VERSION,
    WorkPackageDispatchAdvisor,
)
from mac.work_package_models import WORK_PACKAGE_PLAN_SCHEMA
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
            "projectrepo_allocator",
            "allocator",
            "/tmp/allocator",
            "git@example.invalid:allocator.git",
            "allocator",
            "[]",
            1,
            60,
            "{}",
            "created",
            "updated",
        ),
    )


def _critical_path_plan() -> dict:
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_allocator",
        "goal": "Prefer the mutation on the longer remaining path",
        "project": "allocator",
        "repository_id": "projectrepo_allocator",
        "resource_namespace": {
            "case_sensitive": True,
            "unicode_normalization": "NFC",
            "symlink_resolution": "resolved",
        },
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": "a" * 40,
        "plan_generation": 1,
        "max_in_flight": 2,
        "mutation_wip": {"max_tokens": 2},
        "nodes": [
            {
                "node_key": "short",
                "title": "Short mutation",
                "node_type": "mutation",
                "effects": {"writes": ["src/short"]},
                "expected_outputs": ["short-candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"duration_seconds": 1, "confidence": "high"},
            },
            {
                "node_key": "long",
                "title": "Long mutation",
                "node_type": "mutation",
                "effects": {"writes": ["src/long"]},
                "expected_outputs": ["long-candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"duration_seconds": 20, "confidence": "high"},
            },
            {
                "node_key": "assemble",
                "title": "Assemble",
                "node_type": "integration",
                "depends_on": ["short", "long"],
                "expected_outputs": ["tree"],
                "verification": {"profile": "integration-default"},
                "estimates": {"duration_seconds": 2, "confidence": "high"},
            },
        ],
    }


def _active_package(store: SQLiteStore) -> tuple[ControlPlane, dict[str, str]]:
    _register_repository(store)
    service = WorkPackageService(store, repository_verifier=_Verifier())
    result = service.admit(_critical_path_plan(), actor="controller", reason="test")
    service.activate(
        result.package.id,
        expected_plan_version=1,
        expected_epoch=1,
        actor="operator",
    )
    links = {
        str(row["node_key"]): str(row["task_id"])
        for row in store.query_all(
            "SELECT node_key, task_id FROM work_package_task_links"
        )
    }
    control_plane = ControlPlane(
        store=store,
        secret_key="assignment-advisor-test-secret-key-0001",
    )
    control_plane._work_package_downstream_activation_readiness = lambda _described: {
        "ready": True,
        "code": "ready",
        "reason": "",
    }
    control_plane._work_package_downstream_release_gate = lambda *_args, **_kwargs: {
        "ready": True,
        "code": "ready",
        "reason": "",
    }
    return (control_plane, links)


def _provision_package_worker(cp: ControlPlane, agent_id: str) -> None:
    lifecycle = WorkerCredentialLifecycle(cp.store)
    issue = lifecycle.issue(
        agent_id,
        environment="vm",
        expected_source_commit="a" * 40,
        expected_runtime_digest="sha256:allocator-runtime",
        required_capabilities=[PACKAGE_CAPABILITY],
        package_capable=True,
        actor="allocator-test",
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
    cp.store.execute(
        "UPDATE worker_credentials SET state = ?, activated_at = ?, updated_at = ? "
        "WHERE id = ?",
        ("active", "activated", "activated", issue.record["id"]),
    )
    cp.store.execute(
        "UPDATE agents SET resources = ?, running_digest = ?, updated_at = ? "
        "WHERE id = ?",
        (
            json.dumps(resources),
            "sha256:allocator-runtime",
            "observed",
            agent_id,
        ),
    )
    inventory = build_readiness_inventory(
        [agent.to_dict() for agent in cp.list_agents()], lifecycle.records()
    )
    write_policy_state(
        MODE_COMPATIBILITY,
        inventory=inventory,
        store=cp.store,
        actor="allocator-test",
    )


def test_current_compiled_critical_path_rank_orders_equal_priority_tasks() -> None:
    store = SQLiteStore(":memory:")
    try:
        cp, links = _active_package(store)

        ordered = cp._dispatch_ordered_tasks()

        assert [task.id for task in ordered[:2]] == [links["long"], links["short"]]
    finally:
        store.close()


def test_agent_scoring_prefers_lower_load_then_capability_best_fit() -> None:
    store = SQLiteStore(":memory:")
    try:
        cp = ControlPlane(
            store=store, secret_key="assignment-score-secret-key-value-0001"
        )
        machine = cp.register_machine("allocator-score-host")
        exact = cp.register_agent(
            machine.id,
            "exact",
            capabilities=["python"],
            resources={"capacity": 2},
            agent_id="agent_exact",
        )
        broad = cp.register_agent(
            machine.id,
            "broad",
            capabilities=["gpu", "python"],
            resources={"capacity": 2},
            agent_id="agent_broad",
        )
        task = cp.create_task("python", required_capabilities=["python"])
        advisor = WorkPackageDispatchAdvisor(store)

        lower_load = advisor.rank_agents(
            task=task,
            eligible_agents=[exact, broad],
            snapshot=DispatchScoreSnapshot(
                active_lease_counts={exact.id: 1, broad.id: 0},
                learning_records={exact.id: (), broad.id: ()},
            ),
            route="test",
        )
        assert lower_load[0].agent.id == broad.id

        equal_load = advisor.rank_agents(
            task=task,
            eligible_agents=[broad, exact],
            snapshot=DispatchScoreSnapshot(
                active_lease_counts={exact.id: 0, broad.id: 0},
                learning_records={exact.id: (), broad.id: ()},
            ),
            route="test",
        )
        assert equal_load[0].agent.id == exact.id
        assert equal_load[0].decision["agent_score"]["capability_surplus"] == 0
    finally:
        store.close()


def test_ordinary_task_order_falls_back_to_priority_aging_and_age() -> None:
    store = SQLiteStore(":memory:")
    try:
        cp = ControlPlane(
            store=store, secret_key="assignment-order-secret-key-value-0001"
        )
        older = cp.create_task("older", priority=0)
        high = cp.create_task("higher", priority=1)
        created_at = (parse_time(utcnow()) - timedelta(hours=6)).isoformat(
            timespec="microseconds"
        )
        store.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?", (created_at, older.id)
        )

        ordered = cp._dispatch_ordered_tasks()

        assert [task.id for task in ordered[:2]] == [high.id, older.id]
    finally:
        store.close()


def test_agent_score_ties_use_stable_agent_id() -> None:
    store = SQLiteStore(":memory:")
    try:
        cp = ControlPlane(
            store=store, secret_key="assignment-tie-secret-key-value-0001"
        )
        machine = cp.register_machine("allocator-tie-host")
        later = cp.register_agent(
            machine.id,
            "same-z",
            capabilities=["python"],
            agent_id="agent_z",
        )
        earlier = cp.register_agent(
            machine.id,
            "same-a",
            capabilities=["python"],
            agent_id="agent_a",
        )
        task = cp.create_task("python", required_capabilities=["python"])
        snapshot = DispatchScoreSnapshot(
            active_lease_counts={later.id: 0, earlier.id: 0},
            learning_records={later.id: (), earlier.id: ()},
        )

        ranked = WorkPackageDispatchAdvisor(store).rank_agents(
            task=task,
            eligible_agents=[later, earlier],
            snapshot=snapshot,
            route="test",
        )

        assert [advice.agent.id for advice in ranked] == ["agent_a", "agent_z"]
        assert ranked[0].decision["tie_breaker"] == "agent_id"
    finally:
        store.close()


def test_recent_repository_access_success_is_a_bounded_advisory_signal() -> None:
    store = SQLiteStore(":memory:")
    try:
        cp = ControlPlane(
            store=store, secret_key="assignment-learning-secret-key-value-0001"
        )
        machine = cp.register_machine("allocator-learning-host")
        unknown = cp.register_agent(
            machine.id,
            "unknown",
            capabilities=["python"],
            agent_id="agent_a_unknown",
        )
        known = cp.register_agent(
            machine.id,
            "known",
            capabilities=["python"],
            agent_id="agent_z_known",
        )
        task = cp.create_task(
            "repository work",
            project="allocator",
            required_capabilities=["python"],
            metadata={
                "repository_contract": {
                    "canonical_remote_url": "git@github.com:example/repo.git"
                }
            },
        )
        cp.add_memory(
            **build_repository_access_memory_payload(
                build_repository_access_learning(
                    project="allocator",
                    remote="git@github.com:example/repo.git",
                    operation="review_clone",
                    agent_id=known.id,
                    outcome="success",
                    credential_source="agent-bound:ssh",
                )
            )
        )
        advisor = WorkPackageDispatchAdvisor(store)

        ranked = advisor.rank_agents(
            task=task,
            eligible_agents=[unknown, known],
            snapshot=advisor.score_snapshot([unknown, known]),
            route="test",
        )

        assert ranked[0].agent.id == known.id
        assert (
            ranked[0].decision["agent_score"]["repository_access_learning"]
            == "success"
        )
        assert ranked[0].score <= 1_000_001
    finally:
        store.close()


def test_dispatch_snapshots_batch_queries_and_bound_learning_per_agent(
    monkeypatch,
) -> None:
    store = SQLiteStore(":memory:")
    try:
        calls: list[str] = []
        original_query_all = store.query_all

        def counted_query_all(sql, params=()):
            calls.append(sql)
            return original_query_all(sql, params)

        monkeypatch.setattr(store, "query_all", counted_query_all)
        advisor = WorkPackageDispatchAdvisor(store)
        tasks = [SimpleNamespace(id="task_%04d" % index) for index in range(801)]
        agents = [SimpleNamespace(id="agent_%04d" % index) for index in range(801)]

        advisor.task_rank_snapshot(tasks)
        advisor.score_snapshot(agents)

        assert sum("work_package_task_links" in sql for sql in calls) == 3
        assert sum("ROW_NUMBER() OVER" in sql for sql in calls) == 3
        assert sum("COUNT(*) AS active_count" in sql for sql in calls) == 1
        assert all(
            "dispatch_learning_rank <= ?" in sql
            for sql in calls
            if "ROW_NUMBER() OVER" in sql
        )
    finally:
        store.close()


def test_no_eligible_agent_records_intentional_absence_of_assignment_audit(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MAC_OBSERVABILITY_VERBOSE_POLL", "1")
    store = SQLiteStore(":memory:")
    try:
        cp = ControlPlane(
            store=store, secret_key="assignment-unclaimed-secret-key-value-0001"
        )
        task = cp.create_task("needs a worker", required_capabilities=["python"])

        assert cp.dispatch_once() is None

        observations = cp.list_observability(
            name="dispatcher.assignment.unclaimed",
            subject_type="task",
            subject_id=task.id,
            limit=1,
        )
        assert observations
        assert observations[0].detail["reason"] == "no_authoritative_claim_succeeded"
        assert (
            observations[0].detail["assignment_audit_behavior"]
            == "intentionally_absent_without_exact_lease"
        )
        assert store.query_one(
            "SELECT 1 FROM work_package_assignment_audit LIMIT 1"
        ) is None
    finally:
        store.close()


def test_worker_pull_persists_exact_assignment_score_and_rationale() -> None:
    store = SQLiteStore(":memory:")
    try:
        cp, links = _active_package(store)
        machine = cp.register_machine("allocator-worker-host")
        agent = cp.register_agent(
            machine.id,
            "allocator-worker",
            capabilities=[PACKAGE_CAPABILITY],
        )
        _provision_package_worker(cp, agent.id)

        assignment = cp.claim_next_for_agent(agent.id, sync_beads=False)

        assert assignment is not None
        assert assignment["task"]["id"] == links["long"]
        assert cp.get_task(links["long"]).state == TaskState.CLAIMED.value
        audit = store.query_one(
            "SELECT allocator, allocator_version, score, rationale, decision "
            "FROM work_package_assignment_audit WHERE lease_id = ?",
            (assignment["lease"]["id"],),
        )
        assert audit["allocator"] == "deterministic-worker-pull"
        assert audit["allocator_version"] == WORK_PACKAGE_ASSIGNMENT_ADVISOR_VERSION
        assert audit["score"] is not None
        assert "stable_tie_break=agent_id" in audit["rationale"]
        decision = json.loads(audit["decision"])
        assert decision["route"] == "worker_pull"
        assert decision["worker_identity_fixed"] is True
        assert decision["audit_behavior"] == (
            "persist_with_exact_lease_if_claim_succeeds"
        )
        assert decision["advisory_only"] is True
        assert decision["hard_gates_rechecked_in_claim"] is True
        assert decision["task_order"]["node_key"] == "long"
        assert decision["task_order"]["critical_path_rank"] == 22.0
        assert decision["task_candidate_rank"] == 1
    finally:
        store.close()
