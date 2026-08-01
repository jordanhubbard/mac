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
IMAGE="${MAC_TEST_PG_IMAGE:-docker.io/library/postgres:17}"
# Each test gets its own schema, and applying the full DDL takes one lock per
# object in a single transaction. At the 64 default, parallel workers exhaust
# the lock table and the suite fails with "out of shared memory" rather than
# anything resembling a test failure.
LOCKS="${MAC_TEST_PG_MAX_LOCKS:-1024}"

emit() { echo "export MAC_TEST_PG_URL=$1"; }

# 1. Already configured -- respect it.
if [ -n "${MAC_TEST_PG_URL:-}" ]; then
  emit "$MAC_TEST_PG_URL"
  exit 0
fi

# 2. A server already listening locally (brew services, a running container).
if command -v pg_isready >/dev/null 2>&1 && pg_isready -h 127.0.0.1 -p "$PORT" >/dev/null 2>&1; then
  createdb -h 127.0.0.1 -p "$PORT" "$DB" 2>/dev/null || true
  current=$(psql -h 127.0.0.1 -p "$PORT" -d "$DB" -tAc "show max_locks_per_transaction" 2>/dev/null || echo 0)
  if [ "${current:-0}" -lt "$LOCKS" ]; then
    echo "warning: max_locks_per_transaction=$current (< $LOCKS); a parallel run may fail with 'out of shared memory'." >&2
    echo "         psql -d $DB -c 'ALTER SYSTEM SET max_locks_per_transaction = $LOCKS;' && restart postgres" >&2
  fi
  emit "postgresql://$(whoami)@127.0.0.1:$PORT/$DB"
  exit 0
fi

# 3. Otherwise run one in a container.
for engine in podman docker; do
  if command -v "$engine" >/dev/null 2>&1 && "$engine" info >/dev/null 2>&1; then
    if ! "$engine" inspect "$CONTAINER" >/dev/null 2>&1; then
      "$engine" run -d --name "$CONTAINER" \
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB="$DB" \
        -p "$PORT:5432" "$IMAGE" \
        -c max_locks_per_transaction="$LOCKS" >/dev/null
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
    echo "error: $CONTAINER did not become ready in 60s" >&2
    exit 1
  fi
done

echo "error: no local Postgres and no usable container engine (tried podman, docker)." >&2
echo "Start Docker/Podman, or 'brew services start postgresql@17'." >&2
exit 1
