"""The durable "this deploy generation is no longer live" record.

`fleet_release_epoch_agents.generation` is the exact string a deploy writes into
the node-local barrier file (`$MAC_HOME/deploy-start-barrier`) and that
`worker.py::_deployment_barrier_state` reads back before it decides whether to
keep draining. Nothing recorded that a generation had stopped being live once
its epoch reached a terminal state, so a worker holding a barrier from an
ABORTED epoch had no authority to consult and drained forever.

These tests cover the persistence layer that gives it one, and only that:

* fresh create -- a real `PostgresStore.initialize()` leaves the table, its
  columns, its key and its (agent_id, generation) index behind;
* migration -- initialize() upgrades a database that already carries an earlier,
  narrower version of the table, where `CREATE TABLE IF NOT EXISTS` is a no-op;
* accessors -- record/lookup round-trip, including in-transaction enlistment,
  newest-wins ordering, idempotent replay and input rejection.

Abort and commit behaviour is deliberately untouched by the change under test,
so nothing here asserts anything about it.

There is one store backend (PostgreSQL); the SQLite implementation was removed
from `src/mac/store.py`. `ephemeral_store()` is therefore the whole backend
matrix, and it needs a live database exactly as the rest of the store suite
does -- tests/conftest.py refuses to run without one.
"""

from __future__ import annotations

import pytest

from mac.store import StoreError
from mac.test_support import ephemeral_store


TABLE = "fleet_release_generation_retirements"

# Ordered so a caller can walk it top-down and satisfy every parent.
_PREPARED_AT = "2026-08-01T00:00:00+00:00"
_RETIRED_AT = "2026-08-01T00:05:00+00:00"


@pytest.fixture()
def store():
    s = ephemeral_store()
    yield s
    s.close()


def _participant(
    store,
    *,
    agent_id: str = "agent_alpha",
    epoch_id: str = "epoch_1",
    generation: str = "gen-1",
    ordinal: int = 0,
) -> None:
    """Insert the real parent chain for one epoch participant.

    The retirement row references `fleet_release_epoch_agents(epoch_id,
    agent_id)`, which reaches back through worker credentials, agents and
    machines. Building it for real (rather than suspending foreign keys) is what
    proves the reference is satisfiable by the caller that will write these rows
    -- the terminal transition of an epoch that has participants.

    `open_state = 0` because a retired generation belongs to an epoch that has
    already ended; it also keeps the `uniq_fleet_release_open_agent` partial
    unique index free when a test needs a second participation for the same
    agent.
    """
    now = "2026-07-31T00:00:00+00:00"
    machine_id = "machine_%s" % agent_id
    store.execute(
        "INSERT INTO machines (id, hostname, labels, resources, trusted, "
        "created_at, updated_at, last_seen_at) "
        "VALUES (?, ?, '{}', '{}', 1, ?, ?, ?) ON CONFLICT DO NOTHING",
        (machine_id, "%s-host" % agent_id, now, now, now),
    )
    store.execute(
        "INSERT INTO agents (id, machine_id, name, capabilities, resources, "
        "status, health_status, created_at, updated_at, last_seen_at) "
        "VALUES (?, ?, ?, '[]', '{}', 'idle', 'healthy', ?, ?, ?) "
        "ON CONFLICT DO NOTHING",
        (agent_id, machine_id, agent_id, now, now, now),
    )
    principal_id = "wc_%s" % agent_id
    store.execute(
        "INSERT INTO worker_credentials (id, agent_id, credential_version, "
        "token_hash, token_fingerprint, scopes, environment, state, issued_at, "
        "expires_at, created_by, updated_at) "
        "VALUES (?, ?, 1, ?, ?, '[]', 'vm', 'active', ?, ?, 'test', ?) "
        "ON CONFLICT DO NOTHING",
        (
            principal_id,
            agent_id,
            "hash_%s" % agent_id,
            "fp_%s" % agent_id,
            now,
            "2100-01-01T00:00:00+00:00",
            now,
        ),
    )
    store.execute(
        "INSERT INTO fleet_release_epochs (epoch_id, request_sha256, "
        "identity_sha256, identity_payload, state, policy_snapshot, actor, "
        "prepared_at) VALUES (?, ?, ?, '{}', 'aborted', '{}', 'test', ?) "
        "ON CONFLICT DO NOTHING",
        (epoch_id, "req_%s" % epoch_id, "id_%s" % epoch_id, _PREPARED_AT),
    )
    store.execute(
        "INSERT INTO fleet_release_epoch_agents ("
        "  epoch_id, agent_id, ordinal, open_state, prior_dispatch_hold,"
        "  epoch_hold_reason, epoch_hold_at, prior_active_service_claim_ids,"
        "  generation, baseline_seen, principal_id, principal_version,"
        "  principal_fingerprint, prior_live_principal_ids,"
        "  prior_attestation_ciphertext_sha256, report_executor_action,"
        "  prior_report_executor_projection_sha256, created_at"
        ") VALUES (?, ?, ?, 0, 0, 'release', ?, '[]', ?, ?, ?, 1, ?, '[]', '', "
        "'preserve', '', ?) ON CONFLICT DO NOTHING",
        (
            epoch_id,
            agent_id,
            ordinal,
            now,
            generation,
            now,
            principal_id,
            "fp_%s" % agent_id,
            now,
        ),
    )


def _record(store, **overrides) -> None:
    kwargs = dict(
        epoch_id="epoch_1",
        agent_id="agent_alpha",
        generation="gen-1",
        outcome="aborted",
        retired_at=_RETIRED_AT,
        prepared_at=_PREPARED_AT,
        disposition="release_participants",
        reason="proof deadline exceeded",
    )
    kwargs.update(overrides)
    store.record_fleet_release_generation_retirement(**kwargs)


# ----------------------------------------------------------------------
# Fresh create
# ----------------------------------------------------------------------


def test_initialize_creates_the_retirement_table(store) -> None:
    rows = store.query_all(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = ?",
        (TABLE,),
    )
    assert [r["table_name"] for r in rows] == [TABLE]


def test_fresh_table_has_every_column_the_accessors_write(store) -> None:
    rows = store.query_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ?",
        (TABLE,),
    )
    assert {r["column_name"] for r in rows} == {
        "epoch_id",
        "agent_id",
        "generation",
        "outcome",
        "disposition",
        "reason",
        "prepared_at",
        "retired_at",
        "created_at",
    }


def test_the_key_is_epoch_agent_and_generation(store) -> None:
    rows = store.query_all(
        "SELECT kcu.column_name, kcu.ordinal_position "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON kcu.constraint_name = tc.constraint_name "
        " AND kcu.table_schema = tc.table_schema "
        "WHERE tc.table_schema = current_schema() "
        "  AND tc.table_name = ? AND tc.constraint_type = 'PRIMARY KEY' "
        "ORDER BY kcu.ordinal_position",
        (TABLE,),
    )
    assert [r["column_name"] for r in rows] == [
        "epoch_id",
        "agent_id",
        "generation",
    ]


def test_the_worker_lookup_has_an_index(store) -> None:
    """The read this table exists to serve is by (agent_id, generation).

    Without an index the worker's question is a sequential scan of every
    retirement the fleet has ever recorded, and the primary key -- which leads
    with epoch_id -- cannot answer it.
    """
    rows = store.query_all(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = current_schema() AND tablename = ?",
        (TABLE,),
    )
    defs = [r["indexdef"].replace('"', "") for r in rows]
    assert any(
        "agent_id, generation" in d and "PRIMARY" not in d for d in defs
    ), "no (agent_id, generation) index on %s: %s" % (TABLE, defs)


def test_outcome_is_constrained_to_the_terminal_states(store) -> None:
    _participant(store)
    with pytest.raises(StoreError):
        store.execute(
            "INSERT INTO %s (epoch_id, agent_id, generation, outcome, "
            "retired_at, created_at) VALUES (?, ?, ?, ?, ?, ?)" % TABLE,
            (
                "epoch_1",
                "agent_alpha",
                "gen-1",
                "open",
                _RETIRED_AT,
                _RETIRED_AT,
            ),
        )


def test_a_retirement_must_name_a_real_epoch_participant(store) -> None:
    """The generation is only meaningful as one participant's barrier string."""
    with pytest.raises(StoreError):
        _record(store, agent_id="agent_never_registered")


# ----------------------------------------------------------------------
# Migration from an existing database
# ----------------------------------------------------------------------


def _narrow_table(store) -> None:
    """Recreate the table as an earlier, narrower revision of itself.

    A live hub that already has the table gets nothing from `CREATE TABLE IF
    NOT EXISTS`, which is precisely how `reviews.findings` reached production
    declared-but-absent. Dropping and recreating without the later columns
    reproduces that database.
    """
    store.execute("DROP TABLE IF EXISTS %s" % TABLE)
    store.execute(
        "CREATE TABLE %s ("
        "  epoch_id TEXT NOT NULL,"
        "  agent_id TEXT NOT NULL,"
        "  generation TEXT NOT NULL,"
        "  outcome TEXT NOT NULL,"
        "  created_at TEXT NOT NULL,"
        "  PRIMARY KEY (epoch_id, agent_id, generation)"
        ")" % TABLE
    )


def test_initialize_adds_the_missing_columns_to_an_existing_table(store) -> None:
    _narrow_table(store)
    store.initialize()
    rows = store.query_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ?",
        (TABLE,),
    )
    live = {r["column_name"] for r in rows}
    assert {"disposition", "reason", "prepared_at", "retired_at"} <= live


def test_migration_preserves_rows_already_in_the_narrow_table(store) -> None:
    _narrow_table(store)
    store.execute(
        "INSERT INTO %s (epoch_id, agent_id, generation, outcome, created_at) "
        "VALUES ('epoch_0', 'agent_alpha', 'gen-0', 'committed', ?)" % TABLE,
        (_RETIRED_AT,),
    )
    store.initialize()
    row = store.query_one(
        "SELECT * FROM %s WHERE generation = 'gen-0'" % TABLE
    )
    assert row is not None
    assert row["outcome"] == "committed"
    # The added NOT NULL column takes its declared default on the existing row
    # rather than failing the migration.
    assert row["retired_at"] == ""
    assert row["disposition"] is None


def test_migration_is_idempotent(store) -> None:
    _narrow_table(store)
    store.initialize()
    store.initialize()
    _participant(store)
    _record(store)
    assert store.get_fleet_release_generation_retirement(
        "agent_alpha", "gen-1"
    ) is not None


def test_initialize_creates_the_table_when_it_is_absent(store) -> None:
    """The other half of the migration: a database predating the table."""
    store.execute("DROP TABLE IF EXISTS %s" % TABLE)
    store.initialize()
    _participant(store)
    _record(store)
    assert store.get_fleet_release_generation_retirement(
        "agent_alpha", "gen-1"
    ) is not None


# ----------------------------------------------------------------------
# Accessor round-trip
# ----------------------------------------------------------------------


def test_record_then_read_back_every_field(store) -> None:
    _participant(store)
    _record(store)
    row = store.get_fleet_release_generation_retirement("agent_alpha", "gen-1")
    assert row is not None
    assert row["epoch_id"] == "epoch_1"
    assert row["agent_id"] == "agent_alpha"
    assert row["generation"] == "gen-1"
    assert row["outcome"] == "aborted"
    assert row["disposition"] == "release_participants"
    assert row["reason"] == "proof deadline exceeded"
    assert row["prepared_at"] == _PREPARED_AT
    assert row["retired_at"] == _RETIRED_AT
    assert row["created_at"]


def test_committed_is_a_retirement_too(store) -> None:
    """A committed epoch retires its generation as surely as an aborted one.

    Both are terminal; in both cases the barrier string the worker is holding
    stops being the live one, so the lookup must answer for either.
    """
    _participant(store)
    _record(store, outcome="committed", disposition="cutover_complete")
    row = store.get_fleet_release_generation_retirement("agent_alpha", "gen-1")
    assert row["outcome"] == "committed"


def test_unretired_generation_reads_as_none(store) -> None:
    _participant(store)
    assert (
        store.get_fleet_release_generation_retirement("agent_alpha", "gen-1")
        is None
    )


def test_lookup_is_scoped_to_the_asking_agent(store) -> None:
    """Two nodes share a generation string; one retirement must not free both.

    The barrier file is node-local, so the generation alone is not an identity.
    """
    _participant(store)
    _participant(store, agent_id="agent_beta", epoch_id="epoch_1", ordinal=1)
    _record(store)
    assert (
        store.get_fleet_release_generation_retirement("agent_beta", "gen-1")
        is None
    )


def test_lookup_is_scoped_to_the_generation(store) -> None:
    _participant(store)
    _record(store)
    assert (
        store.get_fleet_release_generation_retirement("agent_alpha", "gen-2")
        is None
    )


def test_newest_retirement_wins(store) -> None:
    """The same generation string can be prepared again by a later epoch."""
    _participant(store)
    _participant(
        store, epoch_id="epoch_2", generation="gen-1", ordinal=0
    )
    _record(store, epoch_id="epoch_1", retired_at="2026-08-01T00:05:00+00:00")
    _record(
        store,
        epoch_id="epoch_2",
        outcome="committed",
        retired_at="2026-08-02T00:05:00+00:00",
    )
    row = store.get_fleet_release_generation_retirement("agent_alpha", "gen-1")
    assert row["epoch_id"] == "epoch_2"
    assert row["outcome"] == "committed"


def test_replaying_the_same_retirement_is_idempotent(store) -> None:
    _participant(store)
    _record(store)
    _record(store)
    rows = store.query_all(
        "SELECT * FROM %s WHERE agent_id = ? AND generation = ?" % TABLE,
        ("agent_alpha", "gen-1"),
    )
    assert len(rows) == 1


def test_replay_updates_the_recorded_verdict(store) -> None:
    _participant(store)
    _record(store, reason="first")
    _record(store, reason="second", retired_at="2026-08-03T00:00:00+00:00")
    row = store.get_fleet_release_generation_retirement("agent_alpha", "gen-1")
    assert row["reason"] == "second"
    assert row["retired_at"] == "2026-08-03T00:00:00+00:00"


def test_optional_fields_may_be_omitted(store) -> None:
    _participant(store)
    store.record_fleet_release_generation_retirement(
        epoch_id="epoch_1",
        agent_id="agent_alpha",
        generation="gen-1",
        outcome="aborted",
        retired_at=_RETIRED_AT,
    )
    row = store.get_fleet_release_generation_retirement("agent_alpha", "gen-1")
    assert row["disposition"] is None
    assert row["reason"] is None
    assert row["prepared_at"] is None


# -- transactional enlistment ------------------------------------------


def test_record_enlists_in_a_caller_transaction(store) -> None:
    _participant(store)
    with store.transaction() as conn:
        _record(store, conn=conn)
    assert (
        store.get_fleet_release_generation_retirement("agent_alpha", "gen-1")
        is not None
    )


def test_a_rolled_back_transition_records_no_retirement(store) -> None:
    """The whole point of the `conn` parameter.

    Retiring a generation is part of the terminal transition that decided it. A
    retirement that survives a rolled-back abort would release a worker whose
    barrier is still live.
    """
    _participant(store)

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with store.transaction() as conn:
            _record(store, conn=conn)
            raise _Boom()

    assert (
        store.get_fleet_release_generation_retirement("agent_alpha", "gen-1")
        is None
    )


# -- input rejection ---------------------------------------------------


@pytest.mark.parametrize(
    "field", ["epoch_id", "agent_id", "generation"]
)
def test_identifying_fields_are_required(store, field: str) -> None:
    with pytest.raises(ValueError):
        _record(store, **{field: "  "})


def test_outcome_must_be_a_terminal_epoch_state(store) -> None:
    """`open` and `proved` are not terminal, so they retire nothing."""
    with pytest.raises(ValueError):
        _record(store, outcome="proved")


def test_retired_at_is_required(store) -> None:
    with pytest.raises(ValueError):
        _record(store, retired_at="")


def test_lookup_rejects_nothing_and_returns_none_for_blanks(store) -> None:
    """A worker with no barrier file asks with an empty generation.

    That is a routine read, not an error, and the answer is "no retirement".
    """
    assert store.get_fleet_release_generation_retirement("", "gen-1") is None
    assert (
        store.get_fleet_release_generation_retirement("agent_alpha", "") is None
    )
