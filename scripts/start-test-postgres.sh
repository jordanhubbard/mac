#!/usr/bin/env bash
# Start (or find) a Postgres for the test suite and print the DSN to stdout.
#
# The suite runs against Postgres because the fleet does; see
# src/mac/test_support.py for why an engine-accurate suite is not optional.
#
# Usage:  eval "$(scripts/start-test-postgres.sh)"
set -euo pipefail

DB="${MAC_TEST_PG_DB:-mac_test}"
CONTAINER="${MAC_TEST_PG_CONTAINER:-mac-test-postgres}"
PORT="${MAC_TEST_PG_PORT:-5432}"
# Match the server major to the installed client. pg_dump refuses to dump a
# server newer than itself ("aborting because of server version mismatch"), so
# a pinned postgres:17 breaks the backup drill on any host whose client is
# older -- which includes the GitHub runners.
if [ -z "${MAC_TEST_PG_IMAGE:-}" ] && command -v pg_dump >/dev/null 2>&1; then
  _client_major=$(pg_dump --version | grep -oE '[0-9]+' | head -1)
fi
IMAGE="${MAC_TEST_PG_IMAGE:-docker.io/library/postgres:${_client_major:-17}}"
# Each test gets its own schema, and applying the full DDL takes one lock per
# object in a single transaction. At the 64 default, parallel workers exhaust
# the lock table and the suite fails with "out of shared memory" rather than
# anything resembling a test failure.
LOCKS="${MAC_TEST_PG_MAX_LOCKS:-1024}"
# Each test opens one or more pooled stores, so a parallel run holds far more
# connections than the 100 default allows; exhaustion surfaces as an opaque
# "couldn't get a connection after 30s" rather than a test failure.
CONNS="${MAC_TEST_PG_MAX_CONNECTIONS:-400}"

emit() { echo "export MAC_TEST_PG_URL=$1"; }

# 1. Already configured -- respect it.
if [ -n "${MAC_TEST_PG_URL:-}" ]; then
  emit "$MAC_TEST_PG_URL"
  exit 0
fi

# 2. A server already listening locally (brew services, a running container).
if command -v pg_isready >/dev/null 2>&1 && pg_isready -h 127.0.0.1 -p "$PORT" >/dev/null 2>&1; then
  createdb -h 127.0.0.1 -p "$PORT" "$DB" 2>/dev/null || true
  for setting in "max_locks_per_transaction:$LOCKS:out of shared memory" \
                 "max_connections:$CONNS:couldn't get a connection"; do
    name="${setting%%:*}"; rest="${setting#*:}"; want="${rest%%:*}"; symptom="${rest#*:}"
    current=$(psql -h 127.0.0.1 -p "$PORT" -d "$DB" -tAc "show $name" 2>/dev/null || echo 0)
    if [ "${current:-0}" -lt "$want" ]; then
      echo "warning: $name=$current (< $want); a parallel run may fail with '$symptom'." >&2
      echo "         psql -d $DB -c 'ALTER SYSTEM SET $name = $want;' && restart postgres" >&2
    fi
  done
  emit "postgresql://$(whoami)@127.0.0.1:$PORT/$DB"
  exit 0
fi

# 3. Otherwise run one in a container.
for engine in docker podman; do
  if command -v "$engine" >/dev/null 2>&1 && "$engine" info >/dev/null 2>&1; then
    if ! "$engine" inspect "$CONTAINER" >/dev/null 2>&1; then
      if ! run_log=$("$engine" run -d --name "$CONTAINER" \
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB="$DB" \
        -p "$PORT:5432" "$IMAGE" \
        -c max_locks_per_transaction="$LOCKS" \
        -c max_connections="$CONNS" 2>&1); then
        echo "error: $engine could not start $CONTAINER:" >&2
        echo "$run_log" >&2
        continue
      fi
    else
      "$engine" start "$CONTAINER" >/dev/null 2>&1 || true
    fi
    for _ in $(seq 1 60); do
      if "$engine" exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1; then
        emit "postgresql://postgres:test@127.0.0.1:$PORT/$DB"
        exit 0
      fi
      sleep 1
    done
    echo "error: $CONTAINER did not become ready in 60s ($engine)" >&2
    "$engine" logs "$CONTAINER" >&2 2>/dev/null || true
    "$engine" rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
done

# 4. No engine -- start a server from local binaries. This is the task sandbox:
# it carries the postgresql packages (the derived BOM installs them) but has no
# container engine to nest and no server already running, so without this branch
# an environment holding a complete Postgres still reports "no local Postgres"
# and every code task fails its gate. Debian keeps the server binaries off PATH,
# under /usr/lib/postgresql/<major>/bin, so look there as well as in PATH.
PGBIN=""
for candidate in "$(command -v pg_ctl 2>/dev/null || true)" \
                 /usr/lib/postgresql/*/bin/pg_ctl \
                 /usr/local/pgsql/bin/pg_ctl; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] \
      && [ -x "$(dirname "$candidate")/initdb" ]; then
    PGBIN="$(dirname "$candidate")"
    break
  fi
done
if [ -n "$PGBIN" ]; then
  DATADIR="${MAC_TEST_PG_DATADIR:-${TMPDIR:-/tmp}/mac-test-pgdata}"
  LOGFILE="$DATADIR.log"
  if [ ! -s "$DATADIR/PG_VERSION" ]; then
    rm -rf "$DATADIR"
    # -E UTF8 is not optional here. initdb takes its encoding from the locale,
    # and an OpenShell sandbox has no locale at all -- it strips the image's
    # LANG -- so initdb picks locale "C" and creates a SQL_ASCII cluster.
    # psycopg then encodes every statement as ASCII and any non-ASCII character
    # in the run kills it:
    #
    #     UnicodeEncodeError: 'ascii' codec can't encode character '\xa7'
    #
    # Agent evidence is full of non-ASCII (box drawing, check marks, em
    # dashes, ordinary prose), so the gate fails on the CONTENT of the work
    # rather than on the work. Observed live: a hub review rejected because
    # the evidence contained a section sign.
    #
    # C.UTF-8 is always present on Debian, which is what the sandbox image is;
    # a host without it (macOS/brew) falls back to the locale-derived default,
    # which on such a host is already UTF-8.
    initdb_log=""
    if ! initdb_log=$(
      LC_ALL=C.UTF-8 LANG=C.UTF-8 "$PGBIN/initdb" -D "$DATADIR" \
        -U "$(id -un)" --auth=trust -E UTF8 --locale=C.UTF-8 2>&1
    ); then
      rm -rf "$DATADIR"
      if ! initdb_log=$("$PGBIN/initdb" -D "$DATADIR" -U "$(id -un)" --auth=trust 2>&1); then
        echo "error: initdb failed in $DATADIR:" >&2
        echo "$initdb_log" >&2
        exit 1
      fi
    fi
  fi
  # A data directory that survived an earlier run in the same environment is
  # the common case wherever TMPDIR persists -- a reused sandbox, a developer's
  # second invocation. Two states have to be told apart, because they lead
  # opposite ways:
  #
  #   already running -> use it, and say nothing
  #   stopped, but holding a postmaster.pid naming a PID that is gone ->
  #       pg_ctl refuses to start ("lock file postmaster.pid already exists"),
  #       and the whole gate then fails for want of a database that is sitting
  #       right there
  #
  # Observed live inside a hub verification sandbox: the second run in one
  # sandbox failed with exactly that, and the failure surfaced as hundreds of
  # unrelated test errors.
  if "$PGBIN/pg_ctl" -D "$DATADIR" status >/dev/null 2>&1; then
    # A server is attached to this directory. If it answers on the port we are
    # about to advertise, it IS the answer.
    if command -v pg_isready >/dev/null 2>&1 \
        && pg_isready -h 127.0.0.1 -p "$PORT" >/dev/null 2>&1; then
      "$PGBIN/createdb" -h 127.0.0.1 -p "$PORT" "$DB" 2>/dev/null || true
      emit "postgresql://$(id -un)@127.0.0.1:$PORT/$DB"
      exit 0
    fi
    # Running, but not reachable at 127.0.0.1:$PORT -- a server started with
    # different options (socket-only is the one seen live) answers nothing we
    # can hand to the suite, while still holding the lock that stops us
    # starting our own. Stop it and start one we can describe.
    "$PGBIN/pg_ctl" -D "$DATADIR" -m fast stop >/dev/null 2>&1 || true
  fi
  # -w waits for readiness, so returning success means the emitted DSN resolves.
  if ! start_log=$("$PGBIN/pg_ctl" -D "$DATADIR" -l "$LOGFILE" -w -t 60 start \
      -o "-p $PORT -c listen_addresses=127.0.0.1 -c unix_socket_directories=$DATADIR -c max_locks_per_transaction=$LOCKS -c max_connections=$CONNS" 2>&1); then
    echo "error: pg_ctl could not start a server in $DATADIR:" >&2
    echo "$start_log" >&2
    tail -20 "$LOGFILE" >&2 2>/dev/null || true
    exit 1
  fi
  "$PGBIN/createdb" -h 127.0.0.1 -p "$PORT" "$DB" 2>/dev/null || true
  # The superuser is named for the invoking user, not "postgres", so a second
  # call -- which finds this server listening and takes the branch above --
  # emits a DSN that authenticates instead of "role does not exist".
  emit "postgresql://$(id -un)@127.0.0.1:$PORT/$DB"
  exit 0
fi

echo "error: no local Postgres, no postgres server binaries, and no usable" >&2
echo "       container engine (tried podman, docker)." >&2
echo "Start Docker/Podman, or 'brew services start postgresql@17'." >&2
exit 1
