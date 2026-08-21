"""Durable deploy-generation retirement records in the hub store.

A deploy generation (``<revision>:<agent>:<timestamp>``) is the fence token a
deploy stamps into a node's ``mac.env``; the worker echoes it back on every
heartbeat. The hub treats that echo as a per-heartbeat proof and deliberately
does not keep it as sticky state, so the fact that a generation is FINISHED had
no durable home on the hub -- it lived in the deploy controller's process and
died with it.

These tests pin the substrate that gives it one: the table, its constraints,
and the ``StoreHelpersMixin`` surface. They cover the three properties the
record has to have to be worth calling durable:

  * it survives a store restart (a new connection reads the same fact),
  * it is idempotent under replay, and first-writer-wins, and
  * it cannot be silently rewritten after the fact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac.store import Store, StoreError
from mac.test_support import (
    all_index_names,
    column_names,
    ephemeral_store,
    table_names,
)


GENERATION = "abc1234:agent_bullwinkle:20260821T141200Z"
SUCCESSOR = "def5678:agent_bullwinkle:20260821T151200Z"


def _register_agent(store: Store, agent_id: str) -> None:
    store.execute(
        "INSERT INTO machines (id, hostname, labels, resources, trusted, "
        "created_at, updated_at, last_seen_at) "
        "VALUES (?, ?, '{}', '{}', 1, 'created', 'updated', 'seen')",
        ("machine_" + agent_id, agent_id),
    )
    store.execute(
        "INSERT INTO agents (id, machine_id, name, capabilities, resources, "
        "status, health_status, created_at, updated_at, last_seen_at) "
        "VALUES (?, ?, ?, '[]', '{}', 'idle', 'healthy', 'created', "
        "'updated', 'seen')",
        (agent_id, "machine_" + agent_id, agent_id),
    )


@pytest.fixture()
def store() -> Store:
    created: Store = ephemeral_store()
    _register_agent(created, "agent_bullwinkle")
    yield created
    created.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_table_and_indexes_exist_on_a_fresh_database(store: Store) -> None:
    assert "deploy_generation_retirements" in table_names(store)
    indexes = all_index_names(store)
    assert "idx_deploy_generation_retirements_agent_time" in indexes
    assert "idx_deploy_generation_retirements_epoch" in indexes


def test_columns_are_the_declared_ones(store: Store) -> None:
    assert set(column_names(store, "deploy_generation_retirements")) == {
        "agent_id",
        "generation",
        "reason",
        "retired_by",
        "retired_at",
        "superseded_by_generation",
        "epoch_id",
        "metadata",
        "created_at",
    }


def test_bundled_ddl_declares_the_table() -> None:
    schema_sql = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "mac"
        / "data"
        / "postgres"
        / "schema.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS deploy_generation_retirements" in schema_sql


# ---------------------------------------------------------------------------
# Writing the record
# ---------------------------------------------------------------------------


def test_record_returns_true_and_persists_every_field(store: Store) -> None:
    created = store.record_deploy_generation_retirement(
        agent_id="agent_bullwinkle",
        generation=GENERATION,
        reason="superseded",
        retired_by="fleet-deploy",
        retired_at="2026-08-21T15:12:00.000000+00:00",
        superseded_by_generation=SUCCESSOR,
        epoch_id="epoch_1",
        metadata_json='{"revision":"def5678"}',
    )
    assert created is True

    row = store.get_deploy_generation_retirement("agent_bullwinkle", GENERATION)
    assert row is not None
    assert row["reason"] == "superseded"
    assert row["retired_by"] == "fleet-deploy"
    assert row["retired_at"] == "2026-08-21T15:12:00.000000+00:00"
    assert row["superseded_by_generation"] == SUCCESSOR
    assert row["epoch_id"] == "epoch_1"
    assert row["metadata"] == '{"revision":"def5678"}'


def test_retired_at_defaults_to_now(store: Store) -> None:
    store.record_deploy_generation_retirement(
        agent_id="agent_bullwinkle",
        generation=GENERATION,
        reason="quiesced",
        retired_by="operator",
    )
    row = store.get_deploy_generation_retirement("agent_bullwinkle", GENERATION)
    assert row["retired_at"]
    # created_at and retired_at come from the same clock read, so an audit that
    # sorts on either sees the same order.
    assert row["created_at"] == row["retired_at"]


def test_a_successorless_retirement_is_allowed(store: Store) -> None:
    """Decommission and abort retire a generation with nothing to replace it."""
    assert store.record_deploy_generation_retirement(
        agent_id="agent_bullwinkle",
        generation=GENERATION,
        reason="decommissioned",
        retired_by="operator",
    )
    row = store.get_deploy_generation_retirement("agent_bullwinkle", GENERATION)
    assert row["superseded_by_generation"] is None


def test_write_can_join_a_caller_owned_transaction(store: Store) -> None:
    """Retirement is one step of a cutover; it must commit with the rest."""
    with store.transaction() as conn:
        assert store.record_deploy_generation_retirement(
            agent_id="agent_bullwinkle",
            generation=GENERATION,
            reason="superseded",
            retired_by="fleet-deploy",
            superseded_by_generation=SUCCESSOR,
            conn=conn,
        )
    assert store.is_deploy_generation_retired("agent_bullwinkle", GENERATION)


def test_a_rolled_back_transaction_leaves_no_retirement(store: Store) -> None:
    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with store.transaction() as conn:
            store.record_deploy_generation_retirement(
                agent_id="agent_bullwinkle",
                generation=GENERATION,
                reason="superseded",
                retired_by="fleet-deploy",
                conn=conn,
            )
            raise Boom
    assert not store.is_deploy_generation_retired("agent_bullwinkle", GENERATION)


# ---------------------------------------------------------------------------
# Durability, idempotency, immutability
# ---------------------------------------------------------------------------


def test_replay_is_idempotent_and_first_writer_wins(store: Store) -> None:
    assert store.record_deploy_generation_retirement(
        agent_id="agent_bullwinkle",
        generation=GENERATION,
        reason="superseded",
        retired_by="fleet-deploy",
        retired_at="2026-08-21T15:12:00.000000+00:00",
        superseded_by_generation=SUCCESSOR,
    )
    # A retry of the same cutover step, with different facts attached.
    assert not store.record_deploy_generation_retirement(
        agent_id="agent_bullwinkle",
        generation=GENERATION,
        reason="rolled_back",
        retired_by="someone-else",
        retired_at="2026-08-21T16:00:00.000000+00:00",
        superseded_by_generation=None,
    )
    row = store.get_deploy_generation_retirement("agent_bullwinkle", GENERATION)
    assert row["reason"] == "superseded"
    assert row["retired_by"] == "fleet-deploy"
    assert row["retired_at"] == "2026-08-21T15:12:00.000000+00:00"
    assert row["superseded_by_generation"] == SUCCESSOR
    assert len(store.list_deploy_generation_retirements()) == 1


def test_record_is_readable_from_a_second_connection(store: Store) -> None:
    """The point of the table: the fact outlives the process that wrote it.

    Every store.execute() borrows a different pooled connection, so reading the
    row back through the store at all proves it left the writer's session. A
    transaction forces the check to be about commit rather than pool luck.
    """
    with store.transaction() as conn:
        store.record_deploy_generation_retirement(
            agent_id="agent_bullwinkle",
            generation=GENERATION,
            reason="superseded",
            retired_by="fleet-deploy",
            conn=conn,
        )
    with store.transaction() as other:
        rows = other.execute(
            "SELECT generation FROM deploy_generation_retirements "
            "WHERE agent_id = ?",
            ("agent_bullwinkle",),
        ).fetchall()
    assert [r["generation"] for r in rows] == [GENERATION]


def test_an_existing_retirement_cannot_be_updated(store: Store) -> None:
    """A retirement is terminal; the engine refuses to rewrite one."""
    store.record_deploy_generation_retirement(
        agent_id="agent_bullwinkle",
        generation=GENERATION,
        reason="superseded",
        retired_by="fleet-deploy",
    )
    with pytest.raises(StoreError):
        store.execute(
            "UPDATE deploy_generation_retirements SET reason = ? "
            "WHERE agent_id = ? AND generation = ?",
            ("rolled_back", "agent_bullwinkle", GENERATION),
        )
    row = store.get_deploy_generation_retirement("agent_bullwinkle", GENERATION)
    assert row["reason"] == "superseded"


# ---------------------------------------------------------------------------
# Rejected input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"generation": "  "}, "generation is required"),
        ({"agent_id": ""}, "agent_id is required"),
        ({"reason": "obsolete"}, "unknown retirement reason"),
        (
            {"superseded_by_generation": GENERATION},
            "cannot supersede itself",
        ),
    ],
)
def test_bad_input_is_rejected_before_the_write(
    store: Store, kwargs: dict, message: str
) -> None:
    call = {
        "agent_id": "agent_bullwinkle",
        "generation": GENERATION,
        "reason": "superseded",
        "retired_by": "fleet-deploy",
    }
    call.update(kwargs)
    with pytest.raises(ValueError, match=message):
        store.record_deploy_generation_retirement(**call)
    assert store.list_deploy_generation_retirements() == []


def test_every_declared_reason_is_accepted(store: Store) -> None:
    for index, reason in enumerate(
        store.DEPLOY_GENERATION_RETIREMENT_REASONS
    ):
        assert store.record_deploy_generation_retirement(
            agent_id="agent_bullwinkle",
            generation="%s-%d" % (GENERATION, index),
            reason=reason,
            retired_by="fleet-deploy",
        )
    assert len(store.list_deploy_generation_retirements()) == len(
        store.DEPLOY_GENERATION_RETIREMENT_REASONS
    )


# ---------------------------------------------------------------------------
# Reading it back
# ---------------------------------------------------------------------------


def test_is_retired_is_false_for_an_unknown_generation(store: Store) -> None:
    assert not store.is_deploy_generation_retired(
        "agent_bullwinkle", "never-deployed"
    )


def test_an_empty_generation_is_not_retired(store: Store) -> None:
    """Absence of a fence token is not a spent one.

    A node that predates generation stamping heartbeats without one. Treating
    that as retired would fence out the whole pre-stamping fleet.
    """
    assert not store.is_deploy_generation_retired("agent_bullwinkle", "")
    assert not store.is_deploy_generation_retired("agent_bullwinkle", "   ")


def test_retirement_is_scoped_to_its_agent(store: Store) -> None:
    _register_agent(store, "agent_rocky")
    store.record_deploy_generation_retirement(
        agent_id="agent_bullwinkle",
        generation=GENERATION,
        reason="superseded",
        retired_by="fleet-deploy",
    )
    assert store.is_deploy_generation_retired("agent_bullwinkle", GENERATION)
    assert not store.is_deploy_generation_retired("agent_rocky", GENERATION)


def test_list_orders_most_recent_first_and_filters(store: Store) -> None:
    _register_agent(store, "agent_rocky")
    rows = [
        ("agent_bullwinkle", "gen-a", "superseded", "epoch_1", "2026-08-21T10:00:00+00:00"),
        ("agent_bullwinkle", "gen-b", "rolled_back", "epoch_2", "2026-08-21T12:00:00+00:00"),
        ("agent_rocky", "gen-c", "superseded", "epoch_1", "2026-08-21T11:00:00+00:00"),
    ]
    for agent_id, generation, reason, epoch_id, retired_at in rows:
        store.record_deploy_generation_retirement(
            agent_id=agent_id,
            generation=generation,
            reason=reason,
            retired_by="fleet-deploy",
            retired_at=retired_at,
            epoch_id=epoch_id,
        )

    assert [
        r["generation"] for r in store.list_deploy_generation_retirements()
    ] == ["gen-b", "gen-c", "gen-a"]
    assert [
        r["generation"]
        for r in store.list_deploy_generation_retirements(
            agent_id="agent_bullwinkle"
        )
    ] == ["gen-b", "gen-a"]
    assert [
        r["generation"]
        for r in store.list_deploy_generation_retirements(epoch_id="epoch_1")
    ] == ["gen-c", "gen-a"]
    assert [
        r["generation"]
        for r in store.list_deploy_generation_retirements(reason="rolled_back")
    ] == ["gen-b"]
    assert [
        r["generation"]
        for r in store.list_deploy_generation_retirements(
            since="2026-08-21T11:00:00+00:00",
            until="2026-08-21T12:00:00+00:00",
        )
    ] == ["gen-c"]
    assert [
        r["generation"]
        for r in store.list_deploy_generation_retirements(limit=1)
    ] == ["gen-b"]


def test_store_protocol_declares_the_retirement_surface() -> None:
    """A backend missing one of these must fail the protocol check.

    That is the whole reason StoreHelpersMixin exists: sixteen helpers were
    once absent from the backend the fleet runs while isinstance() still passed.
    """
    for name in (
        "record_deploy_generation_retirement",
        "get_deploy_generation_retirement",
        "is_deploy_generation_retired",
        "list_deploy_generation_retirements",
    ):
        assert hasattr(Store, name), name
    assert isinstance(ephemeral_store(), Store)
