"""An accepting socket is not proof that the requested test database works."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/start-test-postgres.sh"


@pytest.fixture
def helper(tmp_path):
    shims = tmp_path / "bin"
    shims.mkdir()
    log = tmp_path / "commands"
    shim = """#!/bin/bash
name="${0##*/}"
printf '%s %s\\n' "$name" "$*" >> "$PROBE_LOG"
case "$name" in
  pg_dump) echo 'pg_dump (PostgreSQL) 17.0';;
  pg_isready) exit "$PROBE_READY_RC";;
  createdb) exit 0;;
  psql)
    case "$*" in
      *'SELECT 1'*)
        if [ "$PROBE_SQL_RC" != 0 ]; then
          echo 'FATAL: could not open file global/pg_filenode.map' >&2
          exit "$PROBE_SQL_RC"
        fi
        echo 1;;
      *max_connections*) echo 400;;
      *) echo 1024;;
    esac;;
  pg_ctl) case "$*" in *status*) exit 1;; *) exit 0;; esac;;
  initdb) echo 'unexpected initdb invocation' >&2; exit 1;;
  docker|podman)
    [ "${PROBE_CONTAINER:-0}" = 1 ] || exit 1
    case "$*" in *psql*) exit "$PROBE_SQL_RC";; esac;;
  sleep) exit 0;;
esac
"""
    for name in (
        "pg_dump",
        "pg_isready",
        "psql",
        "createdb",
        "pg_ctl",
        "initdb",
        "docker",
        "podman",
        "sleep",
    ):
        path = shims / name
        path.write_text(shim)
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{shims}:/usr/bin:/bin",
        "PROBE_LOG": str(log),
        "PROBE_READY_RC": "0",
        "PROBE_SQL_RC": "0",
        "MAC_TEST_PG_DATADIR": str(tmp_path / "data"),
        "MAC_TEST_PG_DB": "mac_readiness_probe",
        "MAC_TEST_PG_PORT": "15432",
    }
    env.pop("MAC_TEST_PG_URL", None)

    def run(**overrides):
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env={**env, **overrides},
            capture_output=True,
            text=True,
            timeout=10,
        )

    return run, log, Path(env["MAC_TEST_PG_DATADIR"])


def test_accepting_but_unusable_local_server_does_not_emit_a_dsn(helper):
    run, log, _ = helper
    result = run(PROBE_SQL_RC="2")
    assert result.returncode != 0
    assert "MAC_TEST_PG_URL=" not in result.stdout
    assert "SQL" in result.stderr
    assert "docker" not in log.read_text()
    assert "initdb" not in log.read_text()


def test_healthy_local_database_requires_successful_sql(helper):
    run, log, _ = helper
    result = run()
    assert result.returncode == 0, result.stderr
    assert "@127.0.0.1:15432/mac_readiness_probe" in result.stdout
    assert "SELECT 1" in log.read_text()


def test_explicit_database_configuration_is_passed_through_without_probe(helper):
    run, log, _ = helper
    url = "postgresql://user:private-probe-password@127.0.0.1:15432/specified_database"
    result = run(MAC_TEST_PG_URL=url)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"export MAC_TEST_PG_URL={url}"
    assert "private-probe-password" not in result.stderr + log.read_text()
    assert "psql" not in log.read_text()


def test_missing_version_does_not_authorize_deleting_existing_storage(helper):
    run, log, data = helper
    data.mkdir()
    sentinel = data / "postmaster.pid"
    sentinel.write_text("424242\noperator-owned evidence\n")
    result = run(PROBE_READY_RC="1")
    assert result.returncode != 0
    assert sentinel.read_text() == "424242\noperator-owned evidence\n"
    assert "initdb" not in log.read_text()
    assert "preserv" in result.stderr.lower()


def test_native_start_success_does_not_replace_sql_readiness(helper):
    run, log, data = helper
    data.mkdir()
    (data / "PG_VERSION").write_text("17\n")
    result = run(PROBE_READY_RC="1", PROBE_SQL_RC="2")
    assert result.returncode != 0
    assert not result.stdout
    assert "SELECT 1" in log.read_text()
    assert (data / "PG_VERSION").read_text() == "17\n"


def test_accepting_existing_container_needs_sql_and_is_not_deleted_on_failure(helper):
    run, log, data = helper
    data.mkdir()
    sentinel = data / "preserve-me"
    sentinel.write_text("unrelated storage")
    result = run(PROBE_READY_RC="1", PROBE_SQL_RC="2", PROBE_CONTAINER="1")
    assert result.returncode != 0
    assert not result.stdout
    commands = log.read_text()
    assert "psql -X -w -U postgres -d mac_readiness_probe" in commands
    assert "docker rm" not in commands
    assert "podman rm" not in commands
    assert sentinel.read_text() == "unrelated storage"


def test_healthy_existing_container_queries_requested_database(helper):
    run, log, _ = helper
    result = run(PROBE_READY_RC="1", PROBE_CONTAINER="1")
    assert result.returncode == 0, result.stderr
    assert "postgresql://postgres:test@127.0.0.1:15432/mac_readiness_probe" in result.stdout
    assert "psql -X -w -U postgres -d mac_readiness_probe" in log.read_text()
