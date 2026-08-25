from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from mac.dispatch_advisor import DispatchAdvisor, DispatchScoreSnapshot
from mac.fleet_learning import (
    build_repository_access_learning,
    build_repository_access_memory_payload,
)
from mac.models import parse_time, utcnow
from mac.services import ControlPlane
from mac.test_support import ephemeral_store


def test_agent_scoring_prefers_lower_load_then_capability_best_fit() -> None:
    store = ephemeral_store()
    try:
        cp = ControlPlane(store=store, secret_key="assignment-score-secret-key-value-0001")
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
        advisor = DispatchAdvisor(store)

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
    store = ephemeral_store()
    try:
        cp = ControlPlane(store=store, secret_key="assignment-order-secret-key-value-0001")
        older = cp.create_task("older", priority=0)
        high = cp.create_task("higher", priority=1)
        created_at = (parse_time(utcnow()) - timedelta(hours=6)).isoformat(timespec="microseconds")
        store.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (created_at, older.id))

        ordered = cp._dispatch_ordered_tasks()

        assert [task.id for task in ordered[:2]] == [high.id, older.id]
    finally:
        store.close()


def test_agent_score_ties_use_stable_agent_id() -> None:
    store = ephemeral_store()
    try:
        cp = ControlPlane(store=store, secret_key="assignment-tie-secret-key-value-0001")
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

        ranked = DispatchAdvisor(store).rank_agents(
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
    store = ephemeral_store()
    try:
        cp = ControlPlane(store=store, secret_key="assignment-learning-secret-key-value-0001")
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
                "repository_contract": {"canonical_remote_url": "git@github.com:example/repo.git"}
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
        advisor = DispatchAdvisor(store)

        ranked = advisor.rank_agents(
            task=task,
            eligible_agents=[unknown, known],
            snapshot=advisor.score_snapshot([unknown, known]),
            route="test",
        )

        assert ranked[0].agent.id == known.id
        assert ranked[0].decision["agent_score"]["repository_access_learning"] == "success"
        assert ranked[0].score <= 1_000_001
    finally:
        store.close()


def test_dispatch_snapshots_batch_queries_and_bound_learning_per_agent(
    monkeypatch,
) -> None:
    store = ephemeral_store()
    try:
        calls: list[str] = []
        original_query_all = store.query_all

        def counted_query_all(sql, params=()):
            calls.append(sql)
            return original_query_all(sql, params)

        monkeypatch.setattr(store, "query_all", counted_query_all)
        advisor = DispatchAdvisor(store)
        agents = [SimpleNamespace(id="agent_%04d" % index) for index in range(801)]

        advisor.score_snapshot(agents)

        # 801 agents at a 400-id chunk size is three bounded learning reads,
        # not one query per agent.
        assert sum("ROW_NUMBER() OVER" in sql for sql in calls) == 3
        assert sum("COUNT(*) AS active_count" in sql for sql in calls) == 1
        assert all(
            "dispatch_learning_rank <= ?" in sql for sql in calls if "ROW_NUMBER() OVER" in sql
        )
    finally:
        store.close()


def test_no_eligible_agent_records_capacity_demand_without_assigning(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MAC_OBSERVABILITY_VERBOSE_POLL", "1")
    store = ephemeral_store()
    try:
        cp = ControlPlane(store=store, secret_key="assignment-unclaimed-secret-key-value-0001")
        task = cp.create_task("needs a worker", required_capabilities=["python"])

        assert cp.dispatch_once() is None

        requests = cp.list_provisioning_requests()
        assert [request.task_id for request in requests] == [task.id]
    finally:
        store.close()
