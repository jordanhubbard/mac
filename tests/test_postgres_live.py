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
from pathlib import Path

import pytest

pytestmark = pytest.mark.postgres

from mac.store import Store, StoreError  # noqa: E402


_CONTROL_PLANE_TEST_SECRET = "postgres-concurrency-test-secret-32-bytes"


def _enable_work_package_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAC_WORK_PACKAGE_PIPELINE_ENABLED", "true")
    monkeypatch.setenv("MAC_WORK_PACKAGE_LANDING_ENABLED", "true")
    monkeypatch.setenv(
        "MAC_WORK_PACKAGE_BUNDLE_DIR",
        "/tmp/mac-postgres-work-package-bundles",
    )


def _install_pipeline_repository_contract(
    store,
    repository_id: str,
    remote_url: str,
) -> None:
    from tests.test_repository_contract_certification import _contract

    contract = _contract()
    contract["canonical_remote_url"] = remote_url
    changed = store.execute(
        "UPDATE project_repositories SET metadata = ? WHERE id = ?",
        (json.dumps({"repository_contract": contract}), repository_id),
    )
    assert changed.rowcount == 1


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


def test_delegated_agent_foreign_key_is_installed_after_agents(postgres_store) -> None:
    row = postgres_store.query_one(
        "SELECT count(*) AS n FROM pg_constraint "
        "WHERE conrelid = 'leases'::regclass "
        "AND conname = ? AND contype = ?",
        ("leases_delegated_agent_id_fkey", "f"),
    )
    assert row["n"] == 1


def test_postgres_work_package_schema_enforces_core_invariants(
    postgres_store,
) -> None:
    with pytest.raises(
        StoreError, match="must start draft|current epoch/version is incoherent"
    ):
        postgres_store.execute(
            "INSERT INTO work_packages ("
            "id, goal, state, current_plan_version, current_epoch, metadata, "
            "created_by, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            ("wp_bad", "bad", "active", 1, 1, "{}", "human", "now", "now"),
        )

    postgres_store.execute(
        "INSERT INTO work_packages ("
        "id, goal, state, current_plan_version, current_epoch, metadata, "
        "created_by, created_at, updated_at"
        ") VALUES (?,?,?,?,?,?,?,?,?)",
        ("wp_pg", "coordinate", "draft", 0, 0, "{}", "human", "now", "now"),
    )
    postgres_store.execute(
        "INSERT INTO work_package_plan_versions ("
        "package_id, version, parent_version, definition, plan_digest, reason, "
        "created_by, created_at"
        ") VALUES (?,?,?,?,?,?,?,?)",
        ("wp_pg", 1, None, "{}", "sha256:plan", "initial", "human", "now"),
    )
    postgres_store.execute(
        "INSERT INTO work_package_epochs ("
        "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, "
        "status, reason, created_by, created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "wp_pg",
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
    postgres_store.execute(
        "UPDATE work_packages SET state = ?, current_plan_version = ?, "
        "current_epoch = ? WHERE id = ?",
        ("admitted", 1, 1, "wp_pg"),
    )
    postgres_store.execute(
        "UPDATE work_packages SET state = ? WHERE id = ?", ("active", "wp_pg")
    )

    with pytest.raises(StoreError, match="uniq_work_package_active_epoch"):
        postgres_store.execute(
            "INSERT INTO work_package_epochs ("
            "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, "
            "status, reason, created_by, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "wp_pg",
                2,
                1,
                "refs/heads/main",
                "b" * 40,
                "active",
                "duplicate",
                "human",
                "now",
            ),
        )
    with pytest.raises(StoreError, match="plan versions are immutable"):
        postgres_store.execute(
            "UPDATE work_package_plan_versions SET reason = ? "
            "WHERE package_id = ? AND version = ?",
            ("rewritten", "wp_pg", 1),
        )


def test_postgres_ref_retirement_records_are_append_only(postgres_store) -> None:
    """The retained exact refs have an immutable intent/attempt/receipt log."""

    _insert_task(postgres_store, id="ref-task")
    postgres_store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "repo-ref-retirement",
            "ref-retirement",
            "/tmp/ref-retirement",
            "git@example.invalid:ref-retirement.git",
            "mac",
            "[]",
            1,
            60,
            "{}",
            "2026-07-17T00:00:00+00:00",
            "2026-07-17T00:00:00+00:00",
        ),
    )
    postgres_store.execute(
        "INSERT INTO work_package_ref_retirement_intents ("
        "id, repository_id, ref_kind, ref, expected_sha, task_id, batch_id, "
        "terminal_state, terminal_at, eligible_after, created_by, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ref-intent",
            "repo-ref-retirement",
            "attempt",
            "refs/mac/attempts/wp/e1/node/a1-lease",
            "a" * 40,
            "ref-task",
            None,
            "completed",
            "2026-07-17T00:00:00+00:00",
            "2026-07-24T00:00:00+00:00",
            "ref-reconciler",
            "2026-07-17T00:00:00+00:00",
        ),
    )
    postgres_store.execute(
        "INSERT INTO work_package_ref_retirement_attempts ("
        "id, intent_id, outcome, error, created_at"
        ") VALUES (?, ?, ?, ?, ?)",
        (
            "ref-attempt",
            "ref-intent",
            "failed",
            "transient remote failure",
            "2026-07-24T00:00:01+00:00",
        ),
    )
    postgres_store.execute(
        "INSERT INTO work_package_ref_retirement_receipts ("
        "id, intent_id, outcome, completed_at"
        ") VALUES (?, ?, ?, ?)",
        (
            "ref-receipt",
            "ref-intent",
            "missing",
            "2026-07-24T00:00:02+00:00",
        ),
    )

    for table, row_id in (
        ("work_package_ref_retirement_intents", "ref-intent"),
        ("work_package_ref_retirement_attempts", "ref-attempt"),
        ("work_package_ref_retirement_receipts", "ref-receipt"),
    ):
        with pytest.raises(StoreError, match="append-only"):
            postgres_store.execute(
                "UPDATE %s SET id = id WHERE id = ?" % table,
                (row_id,),
            )
        with pytest.raises(StoreError, match="append-only"):
            postgres_store.execute("DELETE FROM %s WHERE id = ?" % table, (row_id,))


def test_postgres_work_package_admission_activation_and_claim_are_portable(
    postgres_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the controller's real package SQL through PostgresStore."""

    from tests.test_work_package_scheduler import (
        _admit_and_activate,
        _plan,
        _provision_package_worker,
    )

    from mac.services import ControlPlane

    _enable_work_package_pipeline(monkeypatch)
    tasks = _admit_and_activate(postgres_store, _plan(package_id="wp_pg_claim"))
    _install_pipeline_repository_contract(
        postgres_store,
        "projectrepo_mac",
        "ssh://git@example.invalid/mac.git",
    )
    cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    machine = cp.register_machine("postgres-package-host")
    agent = cp.register_agent(
        machine.id,
        "postgres-package-worker",
        capabilities=["python", "work_package_v1"],
    )
    _provision_package_worker(postgres_store, agent.id)

    claimed, lease = cp.claim_task(tasks["one"], agent.id, sync_beads=False)

    assignment = postgres_store.query_one(
        "SELECT * FROM work_package_assignment_audit WHERE lease_id = ?",
        (lease.id,),
    )
    assert assignment is not None
    assert assignment["task_id"] == claimed.id
    assert assignment["attempt_number"] == claimed.attempt_count == 1
    assert assignment["attempt_ref"].startswith("refs/mac/attempts/")
    assert (
        claimed.metadata["work_package_assignment"]["attempt_ref"]
        == assignment["attempt_ref"]
    )
    tokens = postgres_store.query_all(
        "SELECT token_kind, stage, state FROM work_package_wip_tokens "
        "WHERE task_id = ? ORDER BY token_kind",
        (claimed.id,),
    )
    assert {row["token_kind"] for row in tokens} == {
        "mutation_capacity",
        "writes",
    }
    assert {(row["stage"], row["state"]) for row in tokens} == {
        ("mutation", "held")
    }


def test_postgres_legacy_direct_sql_package_claim_is_rejected(
    postgres_store,
) -> None:
    """The schema, not one hub binary, owns package claim authorization."""

    from tests.test_work_package_scheduler import _admit_and_activate, _plan

    tasks = _admit_and_activate(
        postgres_store,
        _plan(package_id="wp_pg_legacy_claim"),
    )
    with pytest.raises(
        StoreError,
        match="work package task claim lacks exact assignment authority",
    ):
        with postgres_store.transaction() as conn:
            conn.execute(
                "INSERT INTO leases ("
                "id, task_id, agent_id, expires_at, status, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "lease_pg_legacy",
                    tasks["one"],
                    "agent_pg_legacy",
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
                (
                    "agent_pg_legacy",
                    "lease_pg_legacy",
                    "later",
                    tasks["one"],
                ),
            )
    task = postgres_store.query_one(
        "SELECT state, lease_id FROM tasks WHERE id = ?", (tasks["one"],)
    )
    assert task["state"] == "open"
    assert task["lease_id"] is None

    # The inverse ordering is guarded too: an old writer cannot claim an
    # ordinary task first and attach the package link afterward.
    legacy_task_id = _insert_task(
        postgres_store,
        id="task_pg_link_after_claim",
    )
    postgres_store.execute(
        "INSERT INTO leases ("
        "id, task_id, agent_id, expires_at, status, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "lease_pg_link_after_claim",
            legacy_task_id,
            "agent_pg_legacy",
            "later",
            "active",
            "now",
            "now",
        ),
    )
    postgres_store.execute(
        "UPDATE tasks SET state = 'claimed', owner_agent_id = ?, lease_id = ?, "
        "leased_until = ?, attempt_count = 1 WHERE id = ?",
        (
            "agent_pg_legacy",
            "lease_pg_link_after_claim",
            "later",
            legacy_task_id,
        ),
    )
    with pytest.raises(
        StoreError,
        match="executable task cannot be linked without package claim authority",
    ):
        postgres_store.execute(
            "INSERT INTO work_package_task_links ("
            "task_id, package_id, plan_version, epoch, node_key, node_generation, "
            "declared_effects_digest, contract_digest, input_digest, node_state, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                legacy_task_id,
                "wp_pg_legacy_claim",
                1,
                1,
                "legacy_link_after_claim",
                1,
                "sha256:legacy-effects",
                "sha256:legacy-contract",
                "sha256:legacy-input",
                "planned",
                "now",
            ),
        )


def test_postgres_concurrent_work_package_wip_claims_are_fenced(
    postgres_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two hub replicas must not over-admit one package's mutation WIP."""

    from tests.test_work_package_scheduler import (
        _admit_and_activate,
        _plan,
        _provision_package_worker,
    )

    from mac.models import TransitionError, ValidationError
    from mac.services import ControlPlane

    _enable_work_package_pipeline(monkeypatch)
    tasks = _admit_and_activate(
        postgres_store,
        _plan(
            package_id="wp_pg_concurrent",
            max_in_flight=2,
            max_mutation_wip=1,
        ),
    )
    _install_pipeline_repository_contract(
        postgres_store,
        "projectrepo_mac",
        "ssh://git@example.invalid/mac.git",
    )
    first_cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    second_cp = ControlPlane(postgres_store, secret_key=_CONTROL_PLANE_TEST_SECRET)
    agents = []
    for suffix in ("one", "two"):
        machine = first_cp.register_machine("postgres-package-%s-host" % suffix)
        agent = first_cp.register_agent(
            machine.id,
            "postgres-package-%s-worker" % suffix,
            capabilities=["python", "work_package_v1"],
        )
        _provision_package_worker(postgres_store, agent.id)
        agents.append(agent)

    barrier = threading.Barrier(2)

    def simultaneous_preflight(agent, task, *, allow_cooperative_reuse=False):
        barrier.wait(timeout=10)
        return True

    monkeypatch.setattr(first_cp, "_agent_available_for", simultaneous_preflight)
    monkeypatch.setattr(second_cp, "_agent_available_for", simultaneous_preflight)
    results = []
    result_lock = threading.Lock()

    def claim(control_plane, task_id: str, agent_id: str) -> None:
        try:
            _task, lease = control_plane.claim_task(
                task_id,
                agent_id,
                sync_beads=False,
            )
            result = ("ok", lease.id)
        except (TransitionError, ValidationError, StoreError) as exc:
            result = ("error", str(exc))
        with result_lock:
            results.append(result)

    threads = [
        threading.Thread(target=claim, args=(first_cp, tasks["one"], agents[0].id)),
        threading.Thread(target=claim, args=(second_cp, tasks["two"], agents[1].id)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    assert [result[0] for result in results].count("ok") == 1
    assert [result[0] for result in results].count("error") == 1
    assert "mutation WIP is exhausted" in next(
        result[1] for result in results if result[0] == "error"
    )
    assert postgres_store.query_one(
        "SELECT COUNT(*) AS n FROM work_package_assignment_audit"
    )["n"] == 1
    assert postgres_store.query_one(
        "SELECT COUNT(*) AS n FROM work_package_wip_tokens "
        "WHERE token_kind = ? AND state = ?",
        ("mutation_capacity", "held"),
    )["n"] == 1
    assert postgres_store.query_one(
        "SELECT COUNT(*) AS n FROM leases WHERE status = ?", ("active",)
    )["n"] == 1


@pytest.mark.parametrize(
    ("passed", "expire_first_claim"),
    [(True, False), (True, True), (False, False)],
    ids=["success", "expired-claim-retry", "certification-andon"],
)
def test_postgres_full_work_package_assembly_line_is_portable(
    postgres_store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    passed: bool,
    expire_first_claim: bool,
) -> None:
    """Run the real Git assembly line on PostgreSQL, including both terminals."""

    from tests import test_work_package_assembly_line_e2e as assembly_e2e

    # The E2E builder deliberately owns its SQLite store in the ordinary suite.
    # Inject this test's schema-scoped PostgreSQL authority while preserving the
    # same service sequence and real local Git remote.
    monkeypatch.setattr(
        assembly_e2e,
        "SQLiteStore",
        lambda _path: postgres_store,
    )
    _enable_work_package_pipeline(monkeypatch)
    line = assembly_e2e._run_to_certification(
        tmp_path,
        monkeypatch,
        passed=passed,
        expire_first_claim=expire_first_claim,
    )
    endpoint = assembly_e2e.RepositoryEndpoint("repo_e2e", str(line.remote))

    if passed:
        landing = assembly_e2e.LandingService(
            postgres_store,
            owner="postgres-landing-controller",
            config=assembly_e2e.LandingServiceConfig(enabled=True),
        )
        certified = landing.accept_certification(
            line.batch_id,
            endpoint,
            certification_id=line.certification.certification_id,
        )
        assert certified.status == "certified"
        landed = landing.land(line.batch_id, endpoint)
        assert landed.status == "landed"
        finalized = assembly_e2e.WorkPackagePublicationFinalizer(
            postgres_store
        ).finalize_landed_batch(
            line.batch_id,
            actor="postgres-publication-finalizer",
            receipt_id=str(landed.detail["id"]),
        )
        assert finalized.created is True
        assert postgres_store.query_one(
            "SELECT state FROM work_packages WHERE id = ?", (line.package_id,)
        )["state"] == "completed"
        assert postgres_store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_wip_tokens "
            "WHERE package_id = ? AND state = ?",
            (line.package_id, "held"),
        )["n"] == 0
        if expire_first_claim:
            assert line.expired_lease_id is not None
            assert line.worker_lease_id != line.expired_lease_id
    else:
        proof = assembly_e2e.WorkPackageCertificationService(
            postgres_store,
            owner="postgres-rejection-controller",
        ).reject_failed_certification(
            line.batch_id,
            certification_id=line.certification.certification_id,
            actor="postgres-pipeline-controller",
        )
        assert proof["batch_state"] == "rejected"
        assert proof["package_state"] == "paused"
        assert proof["wip_disposition"] == "quarantined"
        assert postgres_store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_intents"
        )["n"] == 0
        assert postgres_store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_landing_receipts"
        )["n"] == 0


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
