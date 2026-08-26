"""Fast unit contracts for the PostgreSQL migration runner."""

from __future__ import annotations

import json

import pytest

from mac.schema_migrations import Migration
from mac.store import StoreError


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
