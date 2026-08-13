"""A data directory left behind must not cost the whole gate.

scripts/start-test-postgres.sh can start a server from installed binaries when
nothing else is available, which is what a task sandbox has. That path assumed
a clean slate: if the data directory already existed it went straight to
`pg_ctl start`, and pg_ctl refuses to start over a postmaster.pid naming a PID
that is gone --

    FATAL: lock file "postmaster.pid" already exists
    HINT:  Is another postmaster (PID 84) running in data directory ...?

-- so the helper exited 1, the gate ran with no MAC_TEST_PG_URL, and the
failure surfaced as hundreds of unrelated test errors with the real cause
hundreds of lines above them. Observed live in a hub verification sandbox on
its second run.

Directories survive wherever TMPDIR does: a reused sandbox, a developer's
second invocation, a CI job with a warm workspace.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start-test-postgres.sh"


def _pg_bin() -> Path | None:
    """Locate server binaries the way the script does."""
    candidates = []
    found = shutil.which("pg_ctl")
    if found:
        candidates.append(Path(found))
    candidates += [Path(p) for p in sorted(glob.glob("/usr/lib/postgresql/*/bin/pg_ctl"))]
    candidates.append(Path("/usr/local/pgsql/bin/pg_ctl"))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            if (candidate.parent / "initdb").is_file():
                return candidate.parent
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


pytestmark = pytest.mark.skipif(
    _pg_bin() is None,
    reason="no PostgreSQL server binaries; the binary-start path is unreachable here",
)



@pytest.fixture()
def short_tmp():
    """A short temp dir: a unix socket path is capped near 104 bytes, and
    pytest's tmp_path on macOS is deeper than that all by itself."""
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="/tmp/macpg-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_a_socket_only_server_does_not_cost_the_gate_its_database(short_tmp, tmp_path):
    """The live failure, exactly.

    A previous run in the same environment left a server attached to the data
    directory that listens on its unix socket but NOT on the TCP port the
    suite is told to use. `pg_isready -h 127.0.0.1` therefore fails, so the
    "already listening" branch does not fire -- and the server still holds the
    lock, so starting our own dies with:

        FATAL: lock file "postmaster.pid" already exists

    The helper exits 1, the gate runs with no MAC_TEST_PG_URL, and hundreds of
    database-backed tests error with the real cause far above them.
    """
    pg_bin = _pg_bin()
    assert pg_bin is not None
    tmp_path = short_tmp
    datadir = tmp_path / "pgdata"
    port = _free_port()

    initdb = subprocess.run(
        [str(pg_bin / "initdb"), "-D", str(datadir), "-U", os.environ.get("USER") or "postgres",
         "--auth=trust"],
        capture_output=True, text=True,
    )
    assert initdb.returncode == 0, initdb.stderr

    # Socket-only: no listen_addresses, so nothing answers on the TCP port.
    started = subprocess.run(
        [str(pg_bin / "pg_ctl"), "-D", str(datadir), "-l", str(tmp_path / "pg.log"),
         "-w", "-t", "60", "start",
         "-o", "-p %d -c listen_addresses= -c unix_socket_directories=%s" % (port, datadir)],
        capture_output=True, text=True,
    )
    assert started.returncode == 0, started.stderr + (tmp_path / "pg.log").read_text()

    # Force the binary-start path, which is the code under test and only runs
    # where nothing else can serve. Dropping the engine from PATH is not enough
    # -- GitHub runners keep docker in /usr/bin alongside everything the script
    # needs -- so shadow it with shims that fail, which is what `docker info`
    # does on a machine with no daemon.
    shims = tmp_path / "shims"
    shims.mkdir()
    for engine in ("docker", "podman"):
        shim = shims / engine
        shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        shim.chmod(0o755)
    env = {
        **os.environ,
        "PATH": "%s:%s:/usr/bin:/bin" % (shims, pg_bin),
        "MAC_TEST_PG_DATADIR": str(datadir),
        "MAC_TEST_PG_PORT": str(port),
        "MAC_TEST_PG_DB": "mac_socket_only_probe",
    }
    env.pop("MAC_TEST_PG_URL", None)

    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=180
    )
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "export MAC_TEST_PG_URL=" in result.stdout
        # And the DSN has to actually answer, which is the whole point.
        ready = subprocess.run(
            [str(pg_bin / "pg_isready"), "-h", "127.0.0.1", "-p", str(port)],
            capture_output=True, text=True,
        )
        assert ready.returncode == 0, ready.stdout + ready.stderr
    finally:
        subprocess.run(
            [str(pg_bin / "pg_ctl"), "-D", str(datadir), "-m", "immediate", "stop"],
            capture_output=True, text=True,
        )
