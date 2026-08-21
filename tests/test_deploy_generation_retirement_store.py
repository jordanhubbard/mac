"""Durable deploy-generation retirement records in the hub store.

A deploy generation is the exact rollout witness the controller writes into an
agent's ``mac.env`` (``MAC_DEPLOY_GENERATION``) and the worker reads back as
``MAC_WORKER_DEPLOY_GENERATION``. The worker stays drained until a local barrier
file holds that same string, so admission is decided by a file on the agent's
own disk -- and files outlive the deploy that wrote them.

These tests pin the property that makes the hub able to overrule such a file:
retirement is durable, idempotent, and cannot be undone. The append-only trigger
is not decoration; it is what lets the admission path treat a hit as final.
"""

from __future__ import annotations

import pytest

from mac.store import Store, StoreError
from mac.test_support import ephemeral_store


@pytest.fixture()
def store() -> Store:
    s: Store = ephemeral_store()
    yield s
    s.close()


def _retire(store: Store, agent: str, generation: str, **kwargs) -> bool:
    kwargs.setdefault("reason", "superseded")
    return store.record_deploy_generation_retirement(
        agent_id=agent, generation=generation, **kwargs
    )


# -- The record itself -------------------------------------------------------


def test_a_retired_generation_is_remembered(store: Store) -> None:
    assert store.is_deploy_generation_retired("agent_a", "gen-1") is False
    assert _retire(store, "agent_a", "gen-1") is True
    assert store.is_deploy_generation_retired("agent_a", "gen-1") is True


def test_every_field_the_caller_supplied_survives_the_write(store: Store) -> None:
    _retire(
        store,
        "agent_a",
        "gen-1",
        reason="rolled_back",
        deployment_id="deploy_17",
        successor_generation="gen-2",
        retired_by="deploy-controller",
        retired_at="2026-08-21T00:00:00.000000+00:00",
        metadata_json='{"phase":"quiesce"}',
    )
    row = store.get_deploy_generation_retirement("agent_a", "gen-1")
    assert row is not None
    assert row["agent_id"] == "agent_a"
    assert row["generation"] == "gen-1"
    assert row["reason"] == "rolled_back"
    assert row["deployment_id"] == "deploy_17"
    assert row["successor_generation"] == "gen-2"
    assert row["retired_by"] == "deploy-controller"
    assert row["retired_at"] == "2026-08-21T00:00:00.000000+00:00"
    assert row["metadata"] == '{"phase":"quiesce"}'


def test_retirement_is_scoped_to_one_agent(store: Store) -> None:
    """A generation string is only meaningful next to the agent it was stamped on.

    A rollout hands the same generation to every host it touches, so retiring it
    for the node that failed must not drain the nodes that succeeded.
    """
    _retire(store, "agent_a", "shared-gen")
    assert store.is_deploy_generation_retired("agent_a", "shared-gen") is True
    assert store.is_deploy_generation_retired("agent_b", "shared-gen") is False


def test_an_unrelated_generation_on_the_same_agent_stays_admissible(
    store: Store,
) -> None:
    _retire(store, "agent_a", "gen-1")
    assert store.is_deploy_generation_retired("agent_a", "gen-2") is False


def test_retired_at_defaults_to_now(store: Store) -> None:
    _retire(store, "agent_a", "gen-1")
    row = store.get_deploy_generation_retirement("agent_a", "gen-1")
    assert row["retired_at"].startswith("20")
    # created_at and retired_at describe the same event on the default path.
    assert row["created_at"] == row["retired_at"]


def test_a_missing_pair_reads_as_none_not_an_error(store: Store) -> None:
    assert store.get_deploy_generation_retirement("nobody", "nothing") is None


def test_a_blank_lookup_is_admissible_rather_than_an_error(store: Store) -> None:
    """An unstamped worker has no generation, so it cannot have a retired one.

    Failing closed on the empty string would drain every agent that is not part
    of a generation-fenced rollout at all.
    """
    assert store.is_deploy_generation_retired("", "") is False
    assert store.is_deploy_generation_retired("agent_a", "") is False
    assert store.get_deploy_generation_retirement("", "gen-1") is None


# -- Idempotence -------------------------------------------------------------


def test_recording_the_same_retirement_twice_is_a_no_op(store: Store) -> None:
    assert _retire(store, "agent_a", "gen-1") is True
    assert _retire(store, "agent_a", "gen-1") is False
    rows = store.list_deploy_generation_retirements(agent_id="agent_a")
    assert len(rows) == 1


def test_a_retry_cannot_rewrite_what_the_first_write_committed(store: Store) -> None:
    """DO NOTHING, not upsert: the first retirement is the one that stands.

    A controller retrying a rollout step re-sends whatever it believes now,
    which may be a later, less accurate story than the one recorded at the
    moment the generation actually stopped being admissible.
    """
    _retire(
        store,
        "agent_a",
        "gen-1",
        reason="failed",
        retired_at="2026-08-21T00:00:00.000000+00:00",
        metadata_json='{"attempt":1}',
    )
    assert (
        _retire(
            store,
            "agent_a",
            "gen-1",
            reason="superseded",
            retired_at="2026-08-21T09:00:00.000000+00:00",
            metadata_json='{"attempt":2}',
        )
        is False
    )
    row = store.get_deploy_generation_retirement("agent_a", "gen-1")
    assert row["reason"] == "failed"
    assert row["retired_at"] == "2026-08-21T00:00:00.000000+00:00"
    assert row["metadata"] == '{"attempt":1}'


def test_the_row_id_is_derived_from_the_pair_it_is_unique_on(store: Store) -> None:
    _retire(store, "agent_a", "gen-1")
    _retire(store, "agent_b", "gen-1")
    ids = {
        store.get_deploy_generation_retirement("agent_a", "gen-1")["id"],
        store.get_deploy_generation_retirement("agent_b", "gen-1")["id"],
    }
    assert len(ids) == 2
    assert all(row_id.startswith("dgr_") for row_id in ids)


def test_identifiers_are_stripped_before_they_are_stored(store: Store) -> None:
    assert _retire(store, "  agent_a  ", "  gen-1  ") is True
    # The padded write and the clean one are the same fact, not two rows.
    assert _retire(store, "agent_a", "gen-1") is False
    assert store.is_deploy_generation_retired("agent_a", "gen-1") is True


# -- Append-only -------------------------------------------------------------


def test_a_retirement_cannot_be_deleted(store: Store) -> None:
    _retire(store, "agent_a", "gen-1")
    with pytest.raises(StoreError, match="append-only"):
        store.execute(
            "DELETE FROM deploy_generation_retirements WHERE agent_id = ?",
            ("agent_a",),
        )
    assert store.is_deploy_generation_retired("agent_a", "gen-1") is True


def test_a_retirement_cannot_be_updated(store: Store) -> None:
    _retire(store, "agent_a", "gen-1")
    with pytest.raises(StoreError, match="immutable"):
        store.execute(
            "UPDATE deploy_generation_retirements SET reason = ? WHERE agent_id = ?",
            ("superseded", "agent_a"),
        )


# -- Validation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "generation"),
    [("", "gen-1"), ("agent_a", ""), ("   ", "gen-1"), ("agent_a", "   ")],
)
def test_an_empty_identifier_is_rejected(
    store: Store, agent: str, generation: str
) -> None:
    with pytest.raises(ValueError, match="agent_id and generation"):
        _retire(store, agent, generation)


def test_an_unknown_reason_is_rejected_before_it_reaches_the_database(
    store: Store,
) -> None:
    """The helper's enum and the CHECK constraint must agree.

    Catching it here is the difference between a ValueError in the controller
    and a constraint violation nobody sees until the live hub rejects the write.
    """
    with pytest.raises(ValueError, match="unknown deploy generation"):
        _retire(store, "agent_a", "gen-1", reason="because")
    assert store.get_deploy_generation_retirement("agent_a", "gen-1") is None


@pytest.mark.parametrize(
    "reason",
    ["superseded", "rolled_back", "failed", "quiesced", "decommissioned"],
)
def test_every_declared_reason_is_accepted_by_the_constraint(
    store: Store, reason: str
) -> None:
    assert _retire(store, "agent_a", "gen-%s" % reason, reason=reason) is True


def test_a_generation_cannot_supersede_itself(store: Store) -> None:
    """Otherwise the row records the live rollout as its own replacement."""
    with pytest.raises(ValueError, match="own successor"):
        _retire(store, "agent_a", "gen-1", successor_generation="gen-1")


def test_blank_optional_fields_are_stored_as_null(store: Store) -> None:
    _retire(
        store,
        "agent_a",
        "gen-1",
        deployment_id="  ",
        successor_generation="",
        retired_by="   ",
    )
    row = store.get_deploy_generation_retirement("agent_a", "gen-1")
    assert row["deployment_id"] is None
    assert row["successor_generation"] is None
    assert row["retired_by"] is None


# -- Listing -----------------------------------------------------------------


def test_listing_returns_most_recent_first(store: Store) -> None:
    for index in range(3):
        _retire(
            store,
            "agent_a",
            "gen-%d" % index,
            retired_at="2026-08-2%dT00:00:00.000000+00:00" % index,
        )
    rows = store.list_deploy_generation_retirements(agent_id="agent_a")
    assert [row["generation"] for row in rows] == ["gen-2", "gen-1", "gen-0"]


def test_listing_filters_are_additive(store: Store) -> None:
    _retire(
        store,
        "agent_a",
        "gen-1",
        reason="failed",
        deployment_id="deploy_1",
        retired_at="2026-08-21T01:00:00.000000+00:00",
    )
    _retire(
        store,
        "agent_a",
        "gen-2",
        reason="superseded",
        deployment_id="deploy_2",
        retired_at="2026-08-21T02:00:00.000000+00:00",
    )
    _retire(
        store,
        "agent_b",
        "gen-3",
        reason="failed",
        deployment_id="deploy_1",
        retired_at="2026-08-21T03:00:00.000000+00:00",
    )

    by_agent = store.list_deploy_generation_retirements(agent_id="agent_a")
    assert {row["generation"] for row in by_agent} == {"gen-1", "gen-2"}

    by_deployment = store.list_deploy_generation_retirements(
        deployment_id="deploy_1"
    )
    assert {row["generation"] for row in by_deployment} == {"gen-1", "gen-3"}

    narrowed = store.list_deploy_generation_retirements(
        agent_id="agent_a", reason="failed"
    )
    assert [row["generation"] for row in narrowed] == ["gen-1"]

    windowed = store.list_deploy_generation_retirements(
        since="2026-08-21T02:00:00.000000+00:00",
        until="2026-08-21T03:00:00.000000+00:00",
    )
    assert [row["generation"] for row in windowed] == ["gen-2"]

    assert len(store.list_deploy_generation_retirements(limit=2)) == 2


def test_listing_an_agent_with_nothing_retired_is_empty(store: Store) -> None:
    assert store.list_deploy_generation_retirements(agent_id="agent_a") == []


# -- Transaction participation ----------------------------------------------


def test_a_retirement_rolls_back_with_the_step_that_recorded_it(
    store: Store,
) -> None:
    """The controller retires a generation as part of a larger rollout step.

    If that step aborts the generation was never actually retired, and a record
    saying otherwise would drain a node that is still serving.
    """

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with store.transaction() as conn:
            store.record_deploy_generation_retirement(
                agent_id="agent_a",
                generation="gen-1",
                reason="superseded",
                conn=conn,
            )
            raise _Boom("rollout step failed")
    assert store.is_deploy_generation_retired("agent_a", "gen-1") is False


def test_a_retirement_commits_with_the_step_that_recorded_it(store: Store) -> None:
    with store.transaction() as conn:
        assert (
            store.record_deploy_generation_retirement(
                agent_id="agent_a",
                generation="gen-1",
                reason="superseded",
                conn=conn,
            )
            is True
        )
    assert store.is_deploy_generation_retired("agent_a", "gen-1") is True
