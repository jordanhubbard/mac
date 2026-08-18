"""Live-Postgres integration tests for PostgresStore (K8s Phase 3.6).

Skipped unless MAC_TEST_PG_URL points at a writable database. Codifies
the manual smoke test from the Phase 3.4 commit so the same end-to-end
behaviors are regression-protected going forward:

  - The bundled schema applies clean to a fresh schema.
  - PostgresStore satisfies the Store protocol.
  - The `?` placeholder translator handles INSERT, SELECT, WHERE, IN.
  - `ON CONFLICT(id) DO UPDATE SET ...` upserts work.
  - Row indexing supports both name and position (sqlite3.Row parity).
  - `json_extract` SQL shim filters TEXT-JSON columns.
  - The task-state PL/pgSQL trigger rejects bad INSERTs and UPDATEs.
  - `transaction()` commits on clean exit and rolls back on exception.
  - The `events` view projects the same shape across underlying tables.

Run with: MAC_TEST_PG_URL=postgresql://postgres:test@127.0.0.1:55432/mac \
          uv run --extra dev pytest -q -m postgres tests/test_postgres_live.py
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.postgres

from mac.models import TaskState  # noqa: E402
from mac.services import ControlPlane  # noqa: E402
from mac.store import Store, StoreError  # noqa: E402


_CONTROL_PLANE_TEST_SECRET = "postgres-concurrency-test-secret-32-bytes"


def _insert_task(store, **overrides) -> str:
    cols = {
        "id": "task-1",
        "title": "first",
        "description": "d",
        "priority": 1,
        "state": "open",
        "required_capabilities": "[]",
        "dependencies": "[]",
        "metadata": "{}",
        "attempt_count": 0,
        "max_attempts": 3,
        "created_at": "2026-05-28T00:00:00Z",
        "updated_at": "2026-05-28T00:00:00Z",
    }
    cols.update(overrides)
    store.execute(
        "INSERT INTO tasks (id, title, description, priority, state, "
        "required_capabilities, dependencies, metadata, attempt_count, "
        "max_attempts, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cols["id"],
            cols["title"],
            cols["description"],
            cols["priority"],
            cols["state"],
            cols["required_capabilities"],
            cols["dependencies"],
            cols["metadata"],
            cols["attempt_count"],
            cols["max_attempts"],
            cols["created_at"],
            cols["updated_at"],
        ),
    )
    return cols["id"]


def test_postgres_store_satisfies_protocol(postgres_store) -> None:
    assert isinstance(postgres_store, Store)


def test_postgres_successor_hold_epoch_converges_under_concurrent_retry(
    postgres_store,
) -> None:
    """Two same-epoch controllers commit one continuously held outcome."""

    from mac.services import ControlPlane

    first_cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    second_cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    machine = first_cp.register_machine("postgres-successor-hold-host")
    first = first_cp.register_agent(
        machine.id,
        "postgres-successor-hold-first",
        agent_id="agent_postgres_successor_hold_first",
    )
    second = first_cp.register_agent(
        machine.id,
        "postgres-successor-hold-second",
        agent_id="agent_postgres_successor_hold_second",
    )
    first_cp.set_agent_dispatch_hold(first.id, "deployment-first")
    first_cp.set_agent_dispatch_hold(second.id, "deployment-second")
    holds = ((first.id, "deployment-first"), (second.id, "deployment-second"))
    barrier = threading.Barrier(2)
    results = []
    errors = []
    result_lock = threading.Lock()

    def transition(control_plane, requested_holds) -> None:
        try:
            barrier.wait(timeout=10)
            result = control_plane.release_agent_dispatch_holds_batch(
                requested_holds,
                epoch_id="postgres-successor-hold-concurrent-epoch",
                successor_reason="synchronized successor hold",
            )
            with result_lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=transition, args=(first_cp, holds)),
        threading.Thread(target=transition, args=(second_cp, tuple(reversed(holds)))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert not errors
    assert len(results) == 2
    for result in results:
        assert {agent.id for agent in result} == {first.id, second.id}
        assert all(agent.dispatch_hold is True for agent in result)
        assert {agent.dispatch_hold_reason for agent in result} == {
            "synchronized successor hold"
        }
    assert (
        postgres_store.query_one(
            "SELECT COUNT(*) AS count FROM agent_lifecycle_events WHERE event_type = ?",
            ("agent.dispatch_hold_epoch_committed",),
        )["count"]
        == 1
    )
    assert (
        postgres_store.query_one(
            "SELECT COUNT(*) AS count FROM agent_lifecycle_events WHERE event_type = ?",
            ("agent.dispatch_hold_epoch_transitioned",),
        )["count"]
        == 2
    )


def test_postgres_dispatch_hold_epoch_status_is_exact_and_read_only(
    postgres_store,
) -> None:
    from mac.services import ControlPlane

    control_plane = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    machine = control_plane.register_machine("postgres-epoch-status-host")
    agent = control_plane.register_agent(
        machine.id,
        "postgres-epoch-status-agent",
        agent_id="agent_postgres_epoch_status",
    )
    holds = ((agent.id, "postgres-epoch-status-hold"),)
    epoch_id = "postgres-epoch-status"
    control_plane.set_agent_dispatch_hold(agent.id, holds[0][1])
    control_plane.release_agent_dispatch_holds_batch(holds, epoch_id=epoch_id)
    identity = control_plane._dispatch_hold_epoch_identity_payload(
        epoch_id=epoch_id,
        normalized=holds,
        requested_expectations=(),
        successor_reason=None,
    )
    digest = control_plane._dispatch_hold_epoch_identity_sha256(identity)
    event_count = postgres_store.query_one(
        "SELECT COUNT(*) AS count FROM agent_lifecycle_events"
    )["count"]

    status = control_plane.agent_dispatch_hold_epoch_status(epoch_id, digest)
    assert status["status"] == "committed"
    assert status["agent_ids"] == [agent.id]
    assert (
        control_plane.agent_dispatch_hold_epoch_status(epoch_id, "f" * 64)["status"]
        == "mismatch"
    )
    assert (
        postgres_store.query_one(
            "SELECT COUNT(*) AS count FROM agent_lifecycle_events"
        )["count"]
        == event_count
    )


def test_postgres_fleet_release_open_is_unique_and_transactional(
    postgres_store,
) -> None:
    """Competing opens select one owner; a later cohort failure rolls back all."""

    from mac.models import ValidationError
    from mac.services import ControlPlane
    from mac.worker_credentials import WorkerCredentialLifecycle

    first_cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    second_cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    assert (
        first_cp.fleet_release_epochs.hub_authority_id
        == second_cp.fleet_release_epochs.hub_authority_id
    )
    machine = first_cp.register_machine("postgres-fleet-release-open-host")
    alpha = first_cp.register_agent(
        machine.id,
        "postgres-fleet-release-open-alpha",
        agent_id="agent_postgres_fleet_release_open_alpha",
    )
    beta = first_cp.register_agent(
        machine.id,
        "postgres-fleet-release-open-beta",
        agent_id="agent_postgres_fleet_release_open_beta",
    )

    def issue(agent_id: str):
        return WorkerCredentialLifecycle(postgres_store).issue(
            agent_id,
            environment="vm",
        )

    def participant(agent, pending) -> dict:
        return {
            "agent_id": agent.id,
            "expected_dispatch_hold": False,
            "expected_hold_reason": None,
            "expected_hold_at": None,
            "generation": "postgres-open-generation",
            "baseline_seen": first_cp.get_agent(agent.id).last_seen_at,
            "principal_id": pending.record["id"],
            "attestation_candidate": None,
            "report_executor_action": "preserve",
            "report_executor_attestation": None,
        }

    first_pending = issue(alpha.id)
    second_pending = issue(alpha.id)
    barrier = threading.Barrier(2)
    results = []
    errors = []
    result_lock = threading.Lock()

    def open_same_epoch(control_plane) -> None:
        try:
            barrier.wait(timeout=10)
            result = control_plane.fleet_release_epochs.open_epoch(
                "postgres-open-same-epoch",
                [participant(alpha, first_pending)],
            )
            with result_lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - asserted below.
            with result_lock:
                errors.append(exc)

    threads = [
        threading.Thread(
            target=open_same_epoch,
            args=(first_cp,),
        ),
        threading.Thread(
            target=open_same_epoch,
            args=(second_cp,),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert not errors
    assert len(results) == 2
    assert results[0] == results[1]
    with pytest.raises(ValidationError, match="reserved"):
        second_cp.fleet_release_epochs.open_epoch(
            "postgres-open-competing",
            [participant(alpha, second_pending)],
        )
    assert (
        postgres_store.query_one(
            "SELECT COUNT(*) AS count FROM fleet_release_epoch_agents "
            "WHERE agent_id = ? AND open_state = 1",
            (alpha.id,),
        )["count"]
        == 1
    )
    partial_index = postgres_store.query_one(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
        "AND indexname = 'uniq_fleet_release_open_agent'"
    )
    assert partial_index is not None
    assert "open_state = 1" in partial_index["indexdef"]

    winner = results[0]
    first_cp.fleet_release_epochs.abort(
        winner["epoch_id"],
        winner["identity_sha256"],
        reason="release Postgres concurrency fixture",
    )
    alpha_pending = issue(alpha.id)
    beta_pending = issue(beta.id)
    first_cp.set_agent_dispatch_hold(beta.id, "unexpected concurrent hold")
    with pytest.raises(ValidationError, match="lost expected prior hold"):
        first_cp.fleet_release_epochs.open_epoch(
            "postgres-open-atomic-failure",
            [participant(alpha, alpha_pending), participant(beta, beta_pending)],
        )
    assert (
        postgres_store.query_one(
            "SELECT 1 FROM fleet_release_epochs WHERE epoch_id = ?",
            ("postgres-open-atomic-failure",),
        )
        is None
    )
    assert first_cp.get_agent(alpha.id).dispatch_hold is False
    assert (
        postgres_store.query_one(
            "SELECT COUNT(*) AS count FROM fleet_release_epoch_agents "
            "WHERE agent_id = ? AND open_state = 1",
            (alpha.id,),
        )["count"]
        == 0
    )


def test_postgres_fleet_source_runtime_registration_converges_concurrently(
    postgres_store,
) -> None:
    from mac.worker_credentials import ensure_fleet_source_runtime

    source_commit = "d" * 40
    barrier = threading.Barrier(2)
    results = []
    errors = []
    result_lock = threading.Lock()

    def register() -> None:
        try:
            barrier.wait(timeout=10)
            result = ensure_fleet_source_runtime(postgres_store, source_commit)
            with result_lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(exc)

    threads = [threading.Thread(target=register) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert not errors
    assert len(results) == 2
    assert {result["created"] for result in results} == {True, False}
    assert len({result["runtime_id"] for result in results}) == 1
    assert len({result["runtime_digest"] for result in results}) == 1
    assert (
        postgres_store.query_one(
            "SELECT COUNT(*) AS count FROM runtime_environments WHERE name = ?",
            (results[0]["runtime_name"],),
        )["count"]
        == 1
    )


def test_postgres_delete_agent_serializes_before_credential_revocation(
    postgres_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issuance cannot land a live bearer behind an in-flight tombstone.

    Force issuance to hold the agent-row fence while deletion enters its
    transaction.  Deletion must block on that same fence *before* inspecting
    worker credentials.  Once issuance commits, deletion sees and revokes the
    new principal in the same transaction that writes the tombstone.

    This interleaving regresses the old credential-row -> agent-row delete
    order, which could both deadlock with issuance and leave a pending bearer
    valid after the agent was decommissioned.
    """
    from mac.services import ControlPlane
    from mac.worker_credentials import WorkerCredentialLifecycle

    cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    machine = cp.register_machine("postgres-delete-issue-host")
    agent = cp.register_agent(
        machine.id,
        "postgres-delete-issue-worker",
        capabilities=["python"],
        agent_id="agent_postgres_delete_issue",
    )
    lifecycle = WorkerCredentialLifecycle(postgres_store)

    issue_has_agent_lock = threading.Event()
    permit_issue_to_commit = threading.Event()
    delete_attempted_agent_lock = threading.Event()
    delete_touched_credentials_first = threading.Event()
    original_transaction = postgres_store.transaction

    class _InstrumentedConnection:
        def __init__(self, connection) -> None:
            self._connection = connection

        def execute(self, sql, params=()):
            statement = " ".join(str(sql).lower().split())
            thread_name = threading.current_thread().name
            touches_agent_row = (
                statement.startswith("update agents ")
                or (statement.startswith("select ") and " from agents " in statement)
                and " for update" in statement
            )

            if thread_name == "credential-issuer" and touches_agent_row:
                result = self._connection.execute(sql, params)
                issue_has_agent_lock.set()
                if not permit_issue_to_commit.wait(timeout=10):
                    raise AssertionError("test did not release credential issuance")
                return result

            if thread_name == "agent-deleter":
                if (
                    "worker_credentials" in statement
                    and not delete_attempted_agent_lock.is_set()
                ):
                    delete_touched_credentials_first.set()
                if touches_agent_row:
                    # Signal before executing: the real PostgreSQL call blocks
                    # here until the issuer releases its row lock.
                    delete_attempted_agent_lock.set()
            return self._connection.execute(sql, params)

    @contextmanager
    def instrumented_transaction():
        with original_transaction() as connection:
            yield _InstrumentedConnection(connection)

    monkeypatch.setattr(postgres_store, "transaction", instrumented_transaction)

    issued = []
    errors = []
    result_lock = threading.Lock()

    def issue_credential() -> None:
        try:
            result = lifecycle.issue(agent.id, environment="vm", actor="postgres-test")
            with result_lock:
                issued.append(result)
        except Exception as exc:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(exc)

    def delete_agent() -> None:
        try:
            cp.delete_agent(agent.id, actor="postgres-test")
        except Exception as exc:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(exc)

    issuer = threading.Thread(target=issue_credential, name="credential-issuer")
    deleter = threading.Thread(target=delete_agent, name="agent-deleter")
    issuer.start()
    assert issue_has_agent_lock.wait(timeout=10)
    deleter.start()
    delete_reached_fence = delete_attempted_agent_lock.wait(timeout=10)
    permit_issue_to_commit.set()

    for thread in (issuer, deleter):
        thread.join(timeout=20)
        assert not thread.is_alive(), "delete/issue transaction deadlocked"

    assert delete_reached_fence
    assert not delete_touched_credentials_first.is_set()
    assert not errors
    assert len(issued) == 1
    assert cp.get_agent(agent.id).deleted_at is not None
    credential = postgres_store.query_one(
        "SELECT state FROM worker_credentials WHERE id = ?",
        (issued[0].record["id"],),
    )
    assert credential["state"] == "revoked"
    assert (
        postgres_store.query_one(
            "SELECT COUNT(*) AS n FROM worker_credentials "
            "WHERE agent_id = ? AND state IN ('pending_install', 'active')",
            (agent.id,),
        )["n"]
        == 0
    )


def test_postgres_delete_agent_and_credential_activation_use_agent_first_order(
    postgres_store,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Activation and deletion cannot deadlock on inverted row-lock order."""

    from mac.deploy_env import read_env_file
    from mac.services import ControlPlane
    from mac.worker_credentials import (
        WorkerCredentialLifecycle,
        authenticated_credential_resource,
        credential_resource_from_env,
        install_vm_manifest,
        installation_manifest,
    )

    cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    machine = cp.register_machine("postgres-delete-activate-host")
    agent = cp.register_agent(
        machine.id,
        "postgres-delete-activate-worker",
        capabilities=["python"],
        agent_id="agent_postgres_delete_activate",
    )
    lifecycle = WorkerCredentialLifecycle(postgres_store)
    issue = lifecycle.issue(agent.id, environment="vm", actor="postgres-test")
    env_path = tmp_path / "worker.env"
    receipt = install_vm_manifest(
        installation_manifest(issue), env_path, expected_agent_id=agent.id
    )
    env_values = read_env_file(env_path)
    cp.heartbeat_agent(
        agent.id,
        status="idle",
        health_status="healthy",
        resources={
            "worker_credential": credential_resource_from_env(agent.id, env_values),
            "worker_credential_authenticated": authenticated_credential_resource(
                agent_id=agent.id,
                principal_id=issue.record["id"],
                token_fingerprint=issue.record["token_fingerprint"],
                credential_version=issue.worker_version,
            ),
        },
    )

    activation_has_agent_lock = threading.Event()
    permit_activation_to_commit = threading.Event()
    delete_attempted_agent_lock = threading.Event()
    activation_touched_credentials_first = threading.Event()
    original_transaction = postgres_store.transaction

    class _InstrumentedConnection:
        def __init__(self, connection) -> None:
            self._connection = connection

        def execute(self, sql, params=()):
            statement = " ".join(str(sql).lower().split())
            thread_name = threading.current_thread().name
            touches_agent_row = statement.startswith("update agents ")
            if thread_name == "credential-activator":
                if (
                    "worker_credentials" in statement
                    and not activation_has_agent_lock.is_set()
                ):
                    activation_touched_credentials_first.set()
                if touches_agent_row:
                    result = self._connection.execute(sql, params)
                    activation_has_agent_lock.set()
                    if not permit_activation_to_commit.wait(timeout=10):
                        raise AssertionError(
                            "test did not release credential activation"
                        )
                    return result
            if thread_name == "agent-deleter" and touches_agent_row:
                delete_attempted_agent_lock.set()
            return self._connection.execute(sql, params)

    @contextmanager
    def instrumented_transaction():
        with original_transaction() as connection:
            yield _InstrumentedConnection(connection)

    monkeypatch.setattr(postgres_store, "transaction", instrumented_transaction)

    activated = []
    errors = []
    result_lock = threading.Lock()

    def activate_credential() -> None:
        try:
            result = lifecycle.activate(
                agent.id, issue.record["id"], receipt=receipt, actor="postgres-test"
            )
            with result_lock:
                activated.append(result)
        except Exception as exc:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(exc)

    def delete_agent() -> None:
        try:
            cp.delete_agent(agent.id, actor="postgres-test")
        except Exception as exc:  # pragma: no cover - asserted below
            with result_lock:
                errors.append(exc)

    activator = threading.Thread(
        target=activate_credential, name="credential-activator"
    )
    deleter = threading.Thread(target=delete_agent, name="agent-deleter")
    activator.start()
    assert activation_has_agent_lock.wait(timeout=10)
    deleter.start()
    delete_reached_fence = delete_attempted_agent_lock.wait(timeout=10)
    permit_activation_to_commit.set()

    for thread in (activator, deleter):
        thread.join(timeout=20)
        assert not thread.is_alive(), "delete/activation transaction deadlocked"

    assert delete_reached_fence
    assert not activation_touched_credentials_first.is_set()
    assert not errors
    assert len(activated) == 1
    assert cp.get_agent(agent.id).deleted_at is not None
    assert lifecycle.list(agent_id=agent.id)[0]["state"] == "revoked"


def test_schema_applied_with_all_bundled_base_tables(postgres_store) -> None:
    schema_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "mac"
        / "data"
        / "postgres"
        / "schema.sql"
    )
    expected = len(
        set(
            re.findall(
                r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(",
                schema_path.read_text(),
            )
        )
    )
    row = postgres_store.query_one(
        "SELECT count(*) AS n FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = ?",
        ("BASE TABLE",),
    )
    assert row["n"] == expected


def test_postgres_telemetry_data_migrations_are_append_only(postgres_store) -> None:
    marker_version = "postgres-live-append-only-probe"
    postgres_store.execute(
        "INSERT INTO telemetry_data_migrations ("
        "version, component, detail, applied_at) VALUES (?, ?, ?, ?)",
        (marker_version, "contract-test", "{}", "2026-07-17T00:00:00Z"),
    )
    assert (
        postgres_store.query_one(
            "SELECT * FROM telemetry_data_migrations WHERE version = ?",
            (marker_version,),
        )
        is not None
    )

    # A recorded one-time migration is a receipt: it can never be rewritten or
    # erased, or a migration could silently run twice.
    with pytest.raises(StoreError, match="immutable"):
        postgres_store.execute(
            "UPDATE telemetry_data_migrations SET component = ? WHERE version = ?",
            ("changed", marker_version),
        )
    with pytest.raises(StoreError, match="append-only"):
        postgres_store.execute(
            "DELETE FROM telemetry_data_migrations WHERE version = ?",
            (marker_version,),
        )


def test_delegated_agent_foreign_key_is_installed_after_agents(postgres_store) -> None:
    row = postgres_store.query_one(
        "SELECT count(*) AS n FROM pg_constraint "
        "WHERE conrelid = 'leases'::regclass "
        "AND conname = ? AND contype = ?",
        ("leases_delegated_agent_id_fkey", "f"),
    )
    assert row["n"] == 1


def test_postgres_task_create_idempotency_reservation_is_exact_and_portable(
    postgres_store,
) -> None:
    from mac.models import ValidationError
    from mac.services import ControlPlane

    cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    request = {
        "description": "same request",
        "idempotency_key": "postgres-retry-1",
        "_idempotency_scope": "postgres-client:test",
    }

    first = cp.create_task("Postgres retry-safe task", **request)
    retry = cp.create_task("Postgres retry-safe task", **request)

    assert retry.id == first.id
    assert (
        postgres_store.query_one(
            "SELECT COUNT(*) AS n FROM tasks WHERE id = ?", (first.id,)
        )["n"]
        == 1
    )
    with pytest.raises(ValidationError, match="already bound to a different request"):
        cp.create_task("Changed Postgres request", **request)


def test_placeholder_translation_handles_in_clause(postgres_store) -> None:
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("t1", "acme", "{}", "now", "now"),
    )
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("t2", "globex", "{}", "now", "now"),
    )
    rows = postgres_store.query_all(
        "SELECT id FROM tenants WHERE id IN (?, ?) ORDER BY id", ("t1", "t2")
    )
    assert [r["id"] for r in rows] == ["t1", "t2"]


def test_on_conflict_upsert(postgres_store) -> None:
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        ("t1", "first", "{}", "now", "now"),
    )
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        ("t1", "second", "{}", "now", "now"),
    )
    row = postgres_store.query_one("SELECT name FROM tenants WHERE id = ?", ("t1",))
    assert row["name"] == "second"


def test_row_supports_named_and_positional_access(postgres_store) -> None:
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("t1", "acme", "{}", "now", "now"),
    )
    row = postgres_store.query_one("SELECT id, name FROM tenants WHERE id = ?", ("t1",))
    assert row["id"] == "t1"
    assert row["name"] == "acme"
    assert row[0] == "t1"
    assert row[1] == "acme"


def test_json_extract_filters_text_json_column(postgres_store) -> None:
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("t1", "acme", '{"plan":"free"}', "now", "now"),
    )
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("t2", "globex", '{"plan":"pro"}', "now", "now"),
    )
    row = postgres_store.query_one(
        "SELECT name FROM tenants WHERE json_extract(metadata, '$.plan') = ?",
        ("pro",),
    )
    assert row is not None
    assert row["name"] == "globex"


def test_task_state_trigger_rejects_bad_insert(postgres_store) -> None:
    with pytest.raises(StoreError) as exc:
        _insert_task(postgres_store, id="bad", state="NOPE")
    assert "invalid task state" in str(exc.value)


def test_task_state_trigger_rejects_bad_update(postgres_store) -> None:
    _insert_task(postgres_store, id="ok")
    with pytest.raises(StoreError) as exc:
        postgres_store.execute(
            "UPDATE tasks SET state = ? WHERE id = ?", ("NOPE", "ok")
        )
    assert "invalid task state" in str(exc.value)


def test_partial_unique_index_active_lease_per_task(postgres_store) -> None:
    _insert_task(postgres_store, id="task-1")
    postgres_store.execute(
        "INSERT INTO leases (id, task_id, agent_id, expires_at, status, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("l1", "task-1", "a1", "now", "active", "now", "now"),
    )
    with pytest.raises(StoreError) as exc:
        postgres_store.execute(
            "INSERT INTO leases (id, task_id, agent_id, expires_at, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("l2", "task-1", "a2", "now", "active", "now", "now"),
        )
    # Postgres reports the unique-constraint name.
    assert "uniq_leases_active_per_task" in str(exc.value)


def test_transaction_commit_on_clean_exit(postgres_store) -> None:
    with postgres_store.transaction() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            ("t-commit", "ok", "{}", "now", "now"),
        )
    row = postgres_store.query_one("SELECT id FROM tenants WHERE id = ?", ("t-commit",))
    assert row is not None


def test_transaction_rollback_on_exception(postgres_store) -> None:
    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with postgres_store.transaction() as conn:
            conn.execute(
                "INSERT INTO tenants (id, name, metadata, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                ("t-rollback", "nope", "{}", "now", "now"),
            )
            raise _Boom("rollback me")
    assert (
        postgres_store.query_one("SELECT id FROM tenants WHERE id = ?", ("t-rollback",))
        is None
    )


def test_events_view_projects_task_history(postgres_store) -> None:
    _insert_task(postgres_store, id="task-1")
    postgres_store.execute(
        "INSERT INTO task_history (id, task_id, event_type, actor, from_state, "
        "to_state, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("h1", "task-1", "created", "op", None, "open", "{}", "now"),
    )
    row = postgres_store.query_one(
        "SELECT id, subject_type, subject_id, event_type, actor, detail "
        "FROM events WHERE id = ?",
        ("h1",),
    )
    assert row is not None
    assert row["subject_type"] == "task"
    assert row["subject_id"] == "task-1"
    assert row["event_type"] == "created"
    # detail is text-encoded jsonb; both states present, NULL serialized as null.
    detail = row["detail"]
    assert "from_state" in detail
    assert "to_state" in detail
    assert '"open"' in detail


def test_ensure_column_adds_missing_column(postgres_store) -> None:
    postgres_store.ensure_column("tenants", "extra_label", "extra_label TEXT")
    # Re-run is idempotent.
    postgres_store.ensure_column("tenants", "extra_label", "extra_label TEXT")
    postgres_store.execute(
        "INSERT INTO tenants (id, name, metadata, extra_label, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("t1", "with-extra", "{}", "hello", "now", "now"),
    )
    row = postgres_store.query_one(
        "SELECT extra_label FROM tenants WHERE id = ?", ("t1",)
    )
    assert row["extra_label"] == "hello"


def test_postgres_backend_identity_names_and_redacts(postgres_store) -> None:
    identity = postgres_store.backend_identity()
    assert identity["backend"] == "postgres"
    assert identity["in_memory"] is False
    # The DSN is surfaced so operators can identify the cluster, but any
    # password must be redacted before it lands in a diagnostics report.
    location = identity["location"]
    assert isinstance(location, str) and location
    assert "password=" not in location or "password=***" in location


def test_postgres_diagnostics_report_runs_against_authoritative_backend(
    postgres_store,
) -> None:
    cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)

    report = cp.diagnostics_report()
    assert report["schema"] == "mac.diagnostics.report.v1"
    # Database reachability confirms the checks executed against Postgres, not
    # a local SQLite fallback.
    reachable = [f for f in report["findings"] if f["check"] == "database-reachable"]
    assert reachable and reachable[0]["severity"] == "ok"
    # The machine-readable identity block names the Postgres backend.
    assert report["data_source"]["backend"] == "postgres"
    assert report["data_source"]["authoritative"] is True

    # A narrowed selection still carries the identity block and runs the check
    # against the same authority (SQLite-dialect SQL translated for Postgres).
    subset = cp.diagnostics_report(names=["failed-tasks"])
    assert {f["check"] for f in subset["findings"]} == {"failed-tasks"}
    assert subset["data_source"]["backend"] == "postgres"


def test_postgres_supervised_dependencies_are_non_cascading(postgres_store) -> None:
    cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    parent = cp.create_task(
        title="Postgres supervised dependency parent",
        description="exercise production dependency semantics",
        project="mac",
    )
    result = cp.add_child_tasks(
        parent.id,
        [
            {"node_id": "base", "title": "Prepare shared input"},
            {
                "node_id": "critical",
                "title": "Run fallible operation",
                "depends_on": ["base"],
            },
            {
                "node_id": "downstream",
                "title": "Consume fallible output",
                "depends_on": ["critical"],
            },
            {"node_id": "independent", "title": "Independent work"},
        ],
    )
    by_node = {
        child["metadata"]["coordination"]["plan_node_id"]: child["id"]
        for child in result["children"]
    }

    cp.force_complete_task(by_node["base"], "test", reason="done")
    cp._transition_task_internal(
        by_node["critical"],
        TaskState.FAILED.value,
        "test",
        {"reason": "executor_failed", "returncode": 124},
    )
    cp._unblock_ready_tasks(limit=100)

    assert cp.get_task(by_node["critical"]).state == TaskState.FAILED.value
    assert cp.get_task(by_node["downstream"]).state == TaskState.BLOCKED.value
    assert cp.get_task(by_node["independent"]).state == TaskState.OPEN.value
    assert cp.get_task(parent.id).state == TaskState.WAITING.value

    cp.force_complete_task(by_node["independent"], "test", reason="done")
    cp._unblock_ready_tasks(limit=100)

    integration = cp.get_task(parent.id)
    assert integration.state == TaskState.OPEN.value
    child_outputs = integration.metadata["coordination"]["child_outputs"]
    assert {item["state"] for item in child_outputs} == {
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
        TaskState.BLOCKED.value,
    }


def test_re_registering_an_artifact_returns_what_it_just_wrote(postgres_store) -> None:
    """register_artifact must not hand back the row it just replaced.

    The augment path opens a transaction, UPDATEs the existing row, then read
    the artifact back. That read borrowed a *different* pooled connection,
    which cannot see the not-yet-committed UPDATE -- so on Postgres the caller
    received the pre-update uri and signers while the database held the new
    ones. POST /artifacts served that stale row in production.

    SQLite could never catch this: one serialized connection sees its own
    uncommitted writes, so the whole suite agreed with an engine the fleet
    does not run.
    """
    cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    first = cp.register_artifact(
        kind="image",
        digest="sha256:staleread",
        uri="artifact://registry/mac:1.0",
        created_by="ci",
        signers=["ci"],
    )
    second = cp.register_artifact(
        kind="image",
        digest="sha256:staleread",
        uri="artifact://registry/mac:1.0-public",
        created_by="ci",
        signers=["release"],
    )

    assert second.id == first.id
    # What the caller is handed must equal what was committed.
    assert second.uri == "artifact://registry/mac:1.0-public"
    assert second.signers == ["ci", "release"]
    committed = cp.get_artifact(first.id)
    assert (second.uri, second.signers) == (committed.uri, committed.signers)
