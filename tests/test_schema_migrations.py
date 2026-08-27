"""Fast unit contracts for the PostgreSQL migration runner."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from mac.schema_migrations import Migration
from mac.store import StoreError


EXPECTED_LEGACY_PRUNABLE_TABLES = {
    "evidence_attempt_links",
    "evidence_attempt_verifications",
    "execution_cohort_assignments",
    "execution_cohort_configurations",
    "work_package_assignment_audit",
    "work_package_batch_inputs",
    "work_package_certification_jobs",
    "work_package_certifications",
    "work_package_controller_outcomes",
    "work_package_controller_station_receipts",
    "work_package_epochs",
    "work_package_finalization_outcomes",
    "work_package_history",
    "work_package_integration_batches",
    "work_package_landing_attempts",
    "work_package_landing_intents",
    "work_package_landing_receipts",
    "work_package_landing_streams",
    "work_package_lease_expiry_repairs",
    "work_package_node_candidates",
    "work_package_node_lineage",
    "work_package_plan_versions",
    "work_package_publication_finalizations",
    "work_package_ref_retirement_attempts",
    "work_package_ref_retirement_intents",
    "work_package_ref_retirement_receipts",
    "work_package_station_attempts",
    "work_package_task_links",
    "work_package_telemetry_health",
    "work_package_wip_tokens",
    "work_packages",
}


def test_legacy_prune_allowlist_is_exact_and_has_no_runtime_sql_users() -> None:
    from mac.schema_migrations import (
        LEGACY_PRUNABLE_FUNCTIONS,
        LEGACY_PRUNABLE_TABLES,
        LEGACY_PRUNABLE_TASK_TRIGGERS,
    )

    assert LEGACY_PRUNABLE_TABLES == EXPECTED_LEGACY_PRUNABLE_TABLES
    root = Path(__file__).resolve().parents[1]
    historical_drop = (root / "migrations" / "2026-08-17-drop-work-package-tables.sql").read_text(
        encoding="utf-8"
    )
    assert (
        set(re.findall(r"DROP TABLE IF EXISTS\s+(\w+)\s+CASCADE", historical_drop))
        == LEGACY_PRUNABLE_TABLES
    )
    assert (
        set(re.findall(r"DROP FUNCTION IF EXISTS\s+(\w+)\s*\(\)\s+CASCADE", historical_drop))
        == LEGACY_PRUNABLE_FUNCTIONS
    )
    migration_drop = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "mac"
        / "data"
        / "postgres"
        / "migrations"
        / "0003_drop_leftover_work_package_triggers.sql"
    ).read_text(encoding="utf-8")
    assert (
        set(re.findall(r"DROP FUNCTION IF EXISTS\s+(\w+)\s*\(\)\s+CASCADE", migration_drop))
        == LEGACY_PRUNABLE_FUNCTIONS
    )
    assert (
        set(re.findall(r"DROP TRIGGER IF EXISTS\s+(\w+)\s+ON tasks", migration_drop))
        == LEGACY_PRUNABLE_TASK_TRIGGERS
    )

    runtime_root = root / "src" / "mac"
    sql_use = re.compile(
        r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|FROM|JOIN)\s+"
        r"[\"']?(%s)\b" % "|".join(sorted(LEGACY_PRUNABLE_TABLES)),
        re.IGNORECASE,
    )
    users = []
    for path in runtime_root.rglob("*.py"):
        if path.name == "schema_migrations.py":
            continue
        if sql_use.search(path.read_text(encoding="utf-8")):
            users.append(path.relative_to(runtime_root).as_posix())
    assert users == []


def test_migration_chain_rejects_empty_duplicate_malformed_and_out_of_order() -> None:
    from mac.schema_migrations import _validate_chain

    cases = (
        ((), "no PostgreSQL schema migrations"),
        (
            (Migration("0001_one", ""), Migration("0001_one", "")),
            "duplicate PostgreSQL migration IDs",
        ),
        ((Migration("one", ""),), "NNNN_stable_name"),
        (
            (Migration("0002_two", ""), Migration("0001_one", "")),
            "missing or out of order",
        ),
    )
    for migrations, message in cases:
        with pytest.raises(StoreError, match=message):
            _validate_chain(migrations)


def test_missing_packaged_migration_is_a_store_error(tmp_path) -> None:
    from mac.schema_migrations import _load_sql

    with pytest.raises(StoreError, match="packaged PostgreSQL migration is missing"):
        _load_sql(tmp_path / "missing.sql")


def test_render_bootstrap_schema_normalizes_trailing_newlines() -> None:
    from mac.schema_migrations import render_bootstrap_schema

    assert (
        render_bootstrap_schema(
            (Migration("0001_one", "SELECT 1;"), Migration("0002_two", "SELECT 2;\n"))
        )
        == "SELECT 1;\nSELECT 2;\n"
    )


def test_cli_status_and_apply_delegate_without_starting_control_plane(monkeypatch, capsys) -> None:
    from mac import schema_migrations as module
    from mac import store_postgres

    instances = []

    class FakeStore:
        def __init__(self, dsn):
            self.dsn = dsn
            self.closed = False
            self.calls = []
            instances.append(self)

        def migration_status(self):
            self.calls.append(("status",))
            return {"status": "current"}

        def apply_migrations(self, **kwargs):
            self.calls.append(("apply", kwargs))
            return {"status": "migrated"}

        def close(self):
            self.closed = True

    monkeypatch.setattr(store_postgres, "PostgresStore", FakeStore)
    assert module.main(["--database-url", "postgresql://test", "--status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "current"}
    assert instances[-1].calls == [("status",)]
    assert instances[-1].closed is True

    assert (
        module.main(
            [
                "--database-url",
                "postgresql://test",
                "--applied-by",
                "pytest",
                "--authorize-existing-baseline",
                "--authorize-legacy-schema-prune",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"status": "migrated"}
    assert instances[-1].calls == [
        (
            "apply",
            {
                "applied_by": "pytest",
                "authorize_existing_baseline": True,
                "authorize_legacy_schema_prune": True,
            },
        )
    ]
    assert instances[-1].closed is True


def test_cli_closes_store_when_migration_fails(monkeypatch) -> None:
    from mac import schema_migrations as module
    from mac import store_postgres

    instances = []

    class FailingStore:
        def __init__(self, _dsn):
            self.closed = False
            instances.append(self)

        def apply_migrations(self, **_kwargs):
            raise StoreError("refused")

        def close(self):
            self.closed = True

    monkeypatch.setattr(store_postgres, "PostgresStore", FailingStore)
    with pytest.raises(SystemExit) as exc:
        module.main(["--database-url", "postgresql://test", "--applied-by", "pytest"])
    assert exc.value.code == 1
    assert instances[-1].closed is True


def test_cli_requires_database_and_application_identity(monkeypatch) -> None:
    from mac import schema_migrations as module

    monkeypatch.delenv("MAC_DATABASE_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    with pytest.raises(SystemExit) as exc:
        module.main(["--status"])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        module.main(["--database-url", "postgresql://test"])
    assert exc.value.code == 2
