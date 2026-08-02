"""The live backup path must match the authority it claims to protect.

On 2026-08-02 the hub (rocky, ``MAC_DATABASE_URL=postgresql:///mac``) had no
usable backup of its authority, and every layer reported success:

* ``ledger_backup_scheduler`` ran every 15 minutes against ``~/.mac/mac.db``,
  a 0-byte file left behind by the Postgres migration. It produced a
  structurally valid but EMPTY SQLite snapshot, wrote a correct sha256
  manifest, passed its own integrity check, retained 14 of them, and rsynced
  the result to the standby as ``mac-latest.db``.
* ``pg_backup_scheduler`` had the right target but failed hourly with
  ``[Errno 2] No such file or directory: 'pg_dump'`` -- recorded faithfully as
  ``pg.backup.failed`` in observability_events, and unnoticed, because the
  other backup kept reporting ok. The hub runs under launchd with
  ``PATH=...:/usr/bin:/bin:/usr/sbin:/sbin``, which excludes Homebrew.

Two independent defects with the same shape: a mechanism reporting success
while protecting nothing. These tests pin both.
"""

from __future__ import annotations

import stat

import pytest

from mac import pg_backup
from mac.ledger_backup_scheduler import LedgerBackupConfig
from mac.pg_backup_scheduler import PgBackupConfig

PG_DSN = "postgresql:///mac"


# --- exactly one backup path is live -------------------------------------


def test_sqlite_ledger_backup_is_off_when_the_authority_is_postgres():
    """The empty-snapshot bug: the SQLite scheduler must not run on a PG hub."""
    env = {"MAC_DATABASE_URL": PG_DSN}
    assert LedgerBackupConfig.from_env(env).enabled is False


def test_postgres_backup_is_on_for_the_same_hub():
    """...and the path that does match the authority is the one that runs."""
    env = {"MAC_DATABASE_URL": PG_DSN}
    assert PgBackupConfig.from_env(env).enabled is True


def test_exactly_one_scheduler_is_enabled_for_any_authority():
    """The two are mutually exclusive, which is the property that matters.

    Neither "both on" (backing up a stale file next to the real one) nor
    "both off" (no backup at all) is ever correct.
    """
    for env in (
        {"MAC_DATABASE_URL": PG_DSN},                       # Postgres authority
        {"MAC_DATABASE_URL": ""},                           # SQLite authority
        {},                                                 # unconfigured -> SQLite
        {"MAC_PG_BACKUP_URL": PG_DSN},                      # PG via the backup-specific var
    ):
        live = [
            name
            for name, enabled in (
                ("ledger", LedgerBackupConfig.from_env(env).enabled),
                ("postgres", PgBackupConfig.from_env(env).enabled),
            )
            if enabled
        ]
        assert len(live) == 1, "env %r produced backup paths %r" % (env, live)


def test_a_client_node_still_backs_up_nothing():
    """A non-authoritative node is the one case where neither should run."""
    env = {"MAC_DATABASE_URL": PG_DSN, "MAC_CONTROL_PLANE_ROLE": "client"}
    assert LedgerBackupConfig.from_env(env).enabled is False
    assert PgBackupConfig.from_env(env).enabled is False


# --- the client binaries are found regardless of the supervisor's PATH ----


def _fake_pg_tree(root, name="pg_dump"):
    binary = root / name
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def test_pg_dump_is_found_when_it_is_not_on_path(tmp_path, monkeypatch):
    """The launchd case: installed, but absent from the service's PATH."""
    install = tmp_path / "opt" / "postgresql@17" / "bin"
    install.mkdir(parents=True)
    _fake_pg_tree(install)

    monkeypatch.setattr(pg_backup, "_PG_BIN_SEARCH", (str(install),))
    # A PATH that genuinely lacks the client, which is the condition under test.
    # The hub's real launchd PATH ends in /usr/bin:/bin:/usr/sbin:/sbin, and
    # Linux runners ship /usr/bin/pg_dump -- so using it verbatim resolved on
    # PATH and never exercised the fallback at all.
    empty = tmp_path / "path-without-postgres"
    empty.mkdir()

    resolved = pg_backup.pg_binary("pg_dump", {"PATH": str(empty)})

    assert resolved == str(install / "pg_dump")


def test_path_wins_when_the_binary_is_on_it(tmp_path, monkeypatch):
    on_path = tmp_path / "path-bin"
    on_path.mkdir()
    _fake_pg_tree(on_path)
    fallback = tmp_path / "fallback-bin"
    fallback.mkdir()
    _fake_pg_tree(fallback)

    monkeypatch.setattr(pg_backup, "_PG_BIN_SEARCH", (str(fallback),))

    assert pg_backup.pg_binary("pg_dump", {"PATH": str(on_path)}) == str(on_path / "pg_dump")


def test_the_newest_major_version_is_preferred(tmp_path, monkeypatch):
    """pg_dump must be >= the server's major version, so prefer the highest.

    rocky serves 17.10; a 14 client would abort with a version mismatch.
    """
    for version in (14, 17, 9):
        d = tmp_path / ("postgresql@%d" % version) / "bin"
        d.mkdir(parents=True)
        _fake_pg_tree(d)

    monkeypatch.setattr(
        pg_backup, "_PG_BIN_SEARCH", (str(tmp_path / "postgresql@*" / "bin"),)
    )

    resolved = pg_backup.pg_binary("pg_dump", {"PATH": ""})

    assert "postgresql@17" in resolved


def test_an_explicit_bin_dir_overrides_everything(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    _fake_pg_tree(explicit)
    other = tmp_path / "other"
    other.mkdir()
    _fake_pg_tree(other)

    monkeypatch.setattr(pg_backup, "_PG_BIN_SEARCH", (str(other),))

    resolved = pg_backup.pg_binary(
        "pg_dump", {"PATH": str(other), pg_backup.BIN_DIR_ENV: str(explicit)}
    )

    assert resolved == str(explicit / "pg_dump")


def test_a_missing_binary_explains_itself(tmp_path, monkeypatch):
    """The old failure was '[Errno 2] ... pg_dump' with no hint of the cause."""
    monkeypatch.setattr(pg_backup, "_PG_BIN_SEARCH", (str(tmp_path / "nowhere"),))

    with pytest.raises(pg_backup.PgBackupError) as excinfo:
        pg_backup.pg_binary("pg_dump", {"PATH": str(tmp_path / "empty")})

    message = str(excinfo.value)
    assert "pg_dump" in message
    assert pg_backup.BIN_DIR_ENV in message
    assert "cannot back up its own authority" in message


def test_an_injected_runner_does_not_require_the_binaries(monkeypatch):
    """The runner indirection must keep working where pg tools are absent.

    pg_backup's unit tests model the client binaries with a fake runner; if
    resolution were eager they would need a real PostgreSQL install to run.
    """
    monkeypatch.setattr(pg_backup, "_PG_BIN_SEARCH", ())
    monkeypatch.setattr(
        pg_backup,
        "pg_binary",
        lambda *a, **k: pytest.fail("must not resolve a real binary for a fake runner"),
    )

    assert pg_backup._binary_for("pg_dump", {"PATH": ""}, runner=lambda argv, env: None) == "pg_dump"
