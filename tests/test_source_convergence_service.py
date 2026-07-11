from __future__ import annotations

from pathlib import Path

from mac.models import json_dumps, new_id, utcnow
from mac.services import ControlPlane


OLD_SHA = "1" * 40
NEW_SHA = "2" * 40


def _fixture(*, action: str = "source_restart"):
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(
        machine.id,
        "worker",
        capabilities=["python"],
        resources={
            "source_state": {
                "schema": "mac.worker_source_state.v1",
                "commit_sha": OLD_SHA,
                "tree_sha": "a" * 40,
                "dirty": False,
            }
        },
    )
    fleet = cp.create_fleet("primary", agent_ids=[agent.id])
    now = utcnow()
    release_id = new_id("release")
    desired_id = new_id("dss")
    cp.store.execute(
        """
        INSERT INTO source_releases (
            id, repository_id, repository_name, canonical_remote_url,
            commit_sha, canonical_ref, tree_digest, artifact_digest,
            image_digest, created_by, created_by_task_id,
            review_evidence_id, publication_evidence_id, status, metadata,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, ?, ?, ?, ?)
        """,
        (
            release_id,
            "repo_mac",
            "mac",
            "git@github.com:example/mac.git",
            NEW_SHA,
            NEW_SHA,
            "sha256:" + "b" * 64,
            agent.id,
            "published",
            json_dumps({"convergence_action": action, "branch": "main"}),
            now,
            now,
        ),
    )
    cp.store.execute(
        """
        INSERT INTO fleet_desired_source_states (
            id, fleet_id, environment_id, generation, release_id,
            rollout_policy, actor, reason, prior_generation, paused,
            request_id, created_at, updated_at
        ) VALUES (?, ?, NULL, 1, ?, 'immediate', ?, 'test', NULL, 0, ?, ?, ?)
        """,
        (desired_id, fleet.id, release_id, agent.id, new_id("request"), now, now),
    )
    return cp, fleet, agent


def test_controller_holds_dispatches_exact_sha_and_clears_after_attestation(
    monkeypatch,
):
    cp, fleet, agent = _fixture()
    monkeypatch.setenv("MAC_REVIEW_TICK_HUB_AGENT", agent.name)

    result = cp.tick_source_convergence()

    assert result["dispatched"] == 1
    held = cp.get_agent(agent.id)
    assert held.dispatch_hold is True
    assert held.dispatch_hold_reason.startswith("source_convergence:")
    node = cp.source_convergence_status(fleet_id=fleet.id)["nodes"][0]
    assert node["phase"] == "dispatched"
    assert node["desired_sha"] == NEW_SHA
    streams = cp.list_agentbus_streams(agent_id=agent.id)
    update = next(stream for stream in streams if stream.topic == "mac.repo.update.v1")
    payload = cp.read_agentbus_chunks(agent.id, update.id)[0].payload
    assert payload["target_sha"] == NEW_SHA
    assert payload["desired_generation"] == 1

    # A normal hub tick cannot dispatch new work to the stale node and cannot
    # publish another update until the durable retry deadline.
    second = cp.tick_source_convergence()
    assert second["dispatched"] == 0
    assert (
        len(
            [
                stream
                for stream in cp.list_agentbus_streams(agent_id=agent.id)
                if stream.topic == "mac.repo.update.v1"
            ]
        )
        == 1
    )

    cp.heartbeat_agent(
        agent.id,
        status="idle",
        resources={
            **held.resources,
            "source_state": {
                "schema": "mac.worker_source_state.v1",
                "commit_sha": NEW_SHA,
                "tree_sha": "c" * 40,
                "dirty": False,
            },
        },
    )
    converged = cp.tick_source_convergence()
    assert converged["converged"] == 1
    assert cp.get_agent(agent.id).dispatch_hold is False
    assert (
        cp.source_convergence_status(fleet_id=fleet.id)["nodes"][0]["phase"]
        == "converged"
    )


def test_controller_fails_closed_without_impact_plan(monkeypatch):
    cp, fleet, agent = _fixture(action="operator_direction_required")
    monkeypatch.setenv("MAC_REVIEW_TICK_HUB_AGENT", agent.id)

    result = cp.tick_source_convergence()

    assert result["blocked"] == 1
    node = cp.source_convergence_status(fleet_id=fleet.id)["nodes"][0]
    assert node["phase"] == "blocked"
    assert node["blocker_code"] == "operator_direction_required"
    assert cp.get_agent(agent.id).dispatch_hold is True
    assert cp.list_agentbus_streams(agent_id=agent.id) == []


def test_controller_preserves_unrelated_operator_hold(monkeypatch):
    cp, fleet, agent = _fixture()
    monkeypatch.setenv("MAC_REVIEW_TICK_HUB_AGENT", agent.id)
    cp.set_agent_dispatch_hold(agent.id, "operator quarantine")

    cp.tick_source_convergence()

    current = cp.get_agent(agent.id)
    assert current.dispatch_hold is True
    assert current.dispatch_hold_reason == "operator quarantine"
    node = cp.source_convergence_status(fleet_id=fleet.id)["nodes"][0]
    assert node["blocker_code"] == "external_dispatch_hold"


def test_control_plane_tick_places_source_hold_before_dispatch(monkeypatch):
    cp, _fleet, agent = _fixture()
    monkeypatch.setenv("MAC_REVIEW_TICK_HUB_AGENT", agent.id)
    task = cp.create_task(
        "must not run on stale source", required_capabilities=["python"]
    )

    result = cp.tick(limit=1)

    assert result["source_convergence"]["dispatched"] == 1
    assert result["assignments"] == []
    assert cp.get_task(task.id).state == "open"
    assert cp.get_agent(agent.id).dispatch_hold is True


def test_controller_schema_exists_in_sqlite_and_postgres():
    cp = ControlPlane.in_memory()
    tables = {
        row["name"]
        for row in cp.store.query_all(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "source_convergence_nodes" in tables
    assert "source_convergence_controller_leases" in tables
    schema = Path("src/mac/data/postgres/schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS source_convergence_nodes" in schema
    assert "CREATE TABLE IF NOT EXISTS source_convergence_controller_leases" in schema


def test_controller_persists_intent_before_send_and_resumes_same_request(monkeypatch):
    cp, fleet, agent = _fixture()
    monkeypatch.setenv("MAC_REVIEW_TICK_HUB_AGENT", agent.id)
    service = cp.source_convergence
    real_publish = service._publish_exact_source_intent

    def crash_before_send(**_kwargs):
        row = cp.store.query_one(
            "SELECT * FROM source_convergence_nodes WHERE fleet_id = ? AND agent_id = ?",
            (fleet.id, agent.id),
        )
        assert row["phase"] == "dispatching"
        assert row["request_id"]
        raise RuntimeError("simulated hub crash")

    monkeypatch.setattr(service, "_publish_exact_source_intent", crash_before_send)
    failed = cp.tick_source_convergence()
    assert failed["dispatched"] == 0
    first = cp.store.query_one(
        "SELECT * FROM source_convergence_nodes WHERE fleet_id = ? AND agent_id = ?",
        (fleet.id, agent.id),
    )
    request_id = first["request_id"]
    cp.store.execute(
        "UPDATE source_convergence_nodes SET next_retry_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", first["id"]),
    )

    monkeypatch.setattr(service, "_publish_exact_source_intent", real_publish)
    resumed = cp.tick_source_convergence()

    assert resumed["dispatched"] == 1
    node = cp.store.query_one(
        "SELECT * FROM source_convergence_nodes WHERE fleet_id = ? AND agent_id = ?",
        (fleet.id, agent.id),
    )
    assert node["request_id"] == request_id
    assert node["attempt"] == 1
    assert node["phase"] == "dispatched"
    assert (
        len(
            [
                stream
                for stream in cp.list_agentbus_streams(agent_id=agent.id)
                if stream.topic == "mac.repo.update.v1"
            ]
        )
        == 1
    )
