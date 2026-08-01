"""Tests for `make_store_from_env` backend selection."""

from __future__ import annotations

import pytest

from mac.store import Store, StoreError, make_store_from_env
from mac.test_support import ephemeral_store


def test_factory_uses_explicit_mac_db_when_database_url_unset(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("MAC_DATABASE_URL", raising=False)
    monkeypatch.setenv("MAC_DB", str(tmp_path / "explicit.db"))
    s = make_store_from_env()
    try:
        assert isinstance(s, Store)
        assert s.path.endswith("explicit.db")
    finally:
        s.close()


def test_factory_uses_passed_sqlite_path(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MAC_DATABASE_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    target = tmp_path / "passed.db"
    s = make_store_from_env(sqlite_path=str(target))
    try:
        assert isinstance(s, Store)
        assert s.path == str(target)
    finally:
        s.close()


def test_factory_ignores_blank_database_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MAC_DATABASE_URL", "   ")
    monkeypatch.setenv("MAC_DB", str(tmp_path / "blank-url.db"))
    s = make_store_from_env()
    try:
        assert isinstance(s, Store)
    finally:
        s.close()


def test_factory_rejects_non_postgres_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MAC_DATABASE_URL", "mysql://user@host/db")
    monkeypatch.setenv("MAC_DB", str(tmp_path / "non-pg.db"))
    with pytest.raises(StoreError, match="unsupported MAC_DATABASE_URL"):
        make_store_from_env()


def test_factory_requires_explicit_database_and_does_not_create_home_db(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAC_DATABASE_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)

    with pytest.raises(StoreError, match="control-plane database is not configured"):
        make_store_from_env()

    assert not (tmp_path / ".mac").exists()


def test_client_role_refuses_database_even_when_stale_path_is_present(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MAC_CONTROL_PLANE_ROLE", "client")
    monkeypatch.setenv("MAC_DB", str(tmp_path / "stale.db"))
    monkeypatch.delenv("MAC_DATABASE_URL", raising=False)

    with pytest.raises(StoreError, match="client cannot own a database"):
        make_store_from_env()

    assert not (tmp_path / "stale.db").exists()


def test_sqlite_store_requires_explicit_path() -> None:
    with pytest.raises(StoreError, match="requires an explicit database path"):
        Store()


def test_default_control_plane_and_api_require_explicit_database(
    monkeypatch, tmp_path
) -> None:
    from mac.api import create_app
    from mac.services import ControlPlane

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAC_DATABASE_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)

    with pytest.raises(StoreError, match="control-plane database is not configured"):
        ControlPlane()
    with pytest.raises(StoreError, match="control-plane database is not configured"):
        create_app()

    assert not (tmp_path / ".mac").exists()


def test_factory_takes_postgres_branch(monkeypatch) -> None:
    """Verify the factory dispatches to PostgresStore for postgres URLs
    without standing up a live database. We patch PostgresStore with a
    test double so the assertion is fast and deterministic."""
    pytest.importorskip("psycopg")
    import mac.store_postgres as pg_mod

    seen: list = []

    class _FakePG:
        def __init__(self, dsn: str, *, pool_size: int = 10) -> None:
            seen.append({"dsn": dsn, "pool_size": pool_size})
            self.path = dsn

        def initialize(self) -> None:
            seen.append({"initialize": True})

    monkeypatch.setattr(pg_mod, "PostgresStore", _FakePG)
    monkeypatch.setenv(
        "MAC_DATABASE_URL", "postgresql://user@host:5432/macdb"
    )
    monkeypatch.setenv("MAC_PG_POOL_SIZE", "7")

    s = make_store_from_env()

    assert isinstance(s, _FakePG)
    assert seen[0] == {
        "dsn": "postgresql://user@host:5432/macdb",
        "pool_size": 7,
    }
    assert {"initialize": True} in seen


def test_factory_can_attach_to_existing_postgres_without_schema_ddl(
    monkeypatch,
) -> None:
    """Data-plane helpers must not replay schema DDL against live traffic."""
    pytest.importorskip("psycopg")
    import mac.store_postgres as pg_mod

    seen: list = []

    class _FakePG:
        def __init__(self, dsn: str, *, pool_size: int = 10) -> None:
            seen.append({"dsn": dsn, "pool_size": pool_size})
            self.path = dsn

        def initialize(self) -> None:
            seen.append({"initialize": True})

    monkeypatch.setattr(pg_mod, "PostgresStore", _FakePG)
    monkeypatch.setenv("MAC_DATABASE_URL", "postgresql://user@host/macdb")

    s = make_store_from_env(initialize_schema=False)

    assert isinstance(s, _FakePG)
    assert seen == [{"dsn": "postgresql://user@host/macdb", "pool_size": 10}]


def test_factory_can_attach_to_existing_sqlite_without_schema_writes(
    monkeypatch, tmp_path
) -> None:
    database = tmp_path / "authority.db"
    initialized = ephemeral_store()
    initialized.close()
    monkeypatch.delenv("MAC_DATABASE_URL", raising=False)
    monkeypatch.setenv("MAC_DB", str(database))

    attached = make_store_from_env(initialize_schema=False)
    try:
        assert isinstance(attached, Store)
        assert attached.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0
    finally:
        attached.close()


def test_factory_supports_postgres_scheme_alias(monkeypatch) -> None:
    """Both ``postgresql://`` and the legacy ``postgres://`` alias work."""
    pytest.importorskip("psycopg")
    import mac.store_postgres as pg_mod

    class _FakePG:
        def __init__(self, dsn: str, *, pool_size: int = 10) -> None:
            self.path = dsn

        def initialize(self) -> None:
            pass

    monkeypatch.setattr(pg_mod, "PostgresStore", _FakePG)
    monkeypatch.setenv("MAC_DATABASE_URL", "postgres://h/d")
    s = make_store_from_env()
    assert isinstance(s, _FakePG)


def test_module_exports_factory() -> None:
    import mac

    assert hasattr(mac, "make_store_from_env")
    assert mac.make_store_from_env is make_store_from_env
