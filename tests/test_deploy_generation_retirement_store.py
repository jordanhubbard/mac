"""The durable record that lets the hub overrule a resurrected deploy barrier.

A worker calls itself draining while its local barrier file holds its own
``MAC_WORKER_DEPLOY_GENERATION`` (``worker._deployment_barrier_state``). The
file is node-local, so a rollback or a restored service can put a finished
generation's barrier back on disk and the worker heartbeats ``draining`` /
``degraded`` forever for a rollout nobody is running.

``deploy_generation_retirements`` is the control-plane fact that outranks the
file. These tests pin the two properties that make it usable for that: it is
idempotent (a deployment controller may retry a retirement), and it is
append-only (nothing can unretire a generation and hand a resurrected barrier
its authority back).
"""

from __future__ import annotations

import pytest

from mac.store import StoreError
from mac.test_support import ephemeral_store


@pytest.fixture()
def store():
    s = ephemeral_store()
    yield s
    s.close()


def _record(store, agent="agent_bullwinkle", generation="gen-1", **kwargs) -> str:
    return store.record_deploy_generation_retirement(
        agent_id=agent, generation=generation, **kwargs
    )


# -- recording ---------------------------------------------------------------


def test_recording_a_retirement_makes_the_generation_retired(store) -> None:
    assert store.is_deploy_generation_retired("agent_bullwinkle", "gen-1") is False
    _record(store)
    assert store.is_deploy_generation_retired("agent_bullwinkle", "gen-1") is True


def test_the_stored_row_carries_the_provenance_the_caller_supplied(store) -> None:
    record_id = _record(
        store,
        deployment_id="deploy_42",
        retired_by="agent_rocky",
        reason="rollout completed",
        retired_at="2026-08-21T12:00:00.000000+00:00",
        detail={"node": "mini-3"},
    )
    row = store.get_deploy_generation_retirement("agent_bullwinkle", "gen-1")
    assert row is not None
    assert row["id"] == record_id
    assert row["agent_id"] == "agent_bullwinkle"
    assert row["generation"] == "gen-1"
    assert row["deployment_id"] == "deploy_42"
    assert row["retired_by"] == "agent_rocky"
    assert row["reason"] == "rollout completed"
    assert row["retired_at"] == "2026-08-21T12:00:00.000000+00:00"
    assert row["detail"] == '{"node":"mini-3"}'
    # created_at is the hub's own clock, not the caller's retired_at.
    assert row["created_at"] and row["created_at"] != row["retired_at"]


def test_retired_at_defaults_to_now_and_optional_fields_default_to_empty(
    store,
) -> None:
    _record(store)
    row = store.get_deploy_generation_retirement("agent_bullwinkle", "gen-1")
    assert row["retired_at"] == row["created_at"]
    assert row["deployment_id"] == ""
    assert row["retired_by"] == ""
    assert row["reason"] == ""
    assert row["detail"] == "{}"


def test_the_record_id_is_derived_from_the_pair(store) -> None:
    """Same pair, same id -- so a retry does not need to read the row back."""
    expected = store.deploy_generation_retirement_id("agent_bullwinkle", "gen-1")
    assert _record(store) == expected
    other = store.deploy_generation_retirement_id("agent_bullwinkle", "gen-2")
    assert other != expected


@pytest.mark.parametrize(
    "agent,generation",
    [("", "gen-1"), ("agent_bullwinkle", ""), ("   ", "gen-1"), ("a", "   ")],
)
def test_recording_requires_both_an_agent_and_a_generation(
    store, agent, generation
) -> None:
    with pytest.raises(ValueError):
        store.record_deploy_generation_retirement(
            agent_id=agent, generation=generation
        )


def test_identifiers_are_stored_stripped(store) -> None:
    _record(store, agent="  agent_bullwinkle  ", generation="  gen-1  ")
    assert store.is_deploy_generation_retired("agent_bullwinkle", "gen-1") is True


def test_an_oversized_detail_is_refused_rather_than_stored(store) -> None:
    oversized = "x" * (store.DEPLOY_GENERATION_RETIREMENT_MAX_DETAIL_BYTES + 1)
    with pytest.raises(ValueError, match="exceeds"):
        _record(store, detail={"blob": oversized})
    assert store.get_deploy_generation_retirement("agent_bullwinkle", "gen-1") is None


def test_a_retirement_can_be_written_inside_a_caller_transaction(store) -> None:
    with store.transaction() as conn:
        _record(store, conn=conn)
    assert store.is_deploy_generation_retired("agent_bullwinkle", "gen-1") is True


def test_a_caller_transaction_that_rolls_back_retires_nothing(store) -> None:
    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with store.transaction() as conn:
            _record(store, conn=conn)
            raise _Boom("rollback")
    assert store.is_deploy_generation_retired("agent_bullwinkle", "gen-1") is False


# -- idempotence -------------------------------------------------------------


def test_re_recording_the_same_retirement_is_a_no_op(store) -> None:
    """A deployment controller retrying its retirement must not fail or fork."""
    first = _record(store, reason="rollout completed")
    second = _record(store, reason="retry")
    assert first == second
    rows = store.list_deploy_generation_retirements(agent_id="agent_bullwinkle")
    assert len(rows) == 1


def test_the_first_retirement_is_the_one_that_is_kept(store) -> None:
    """The record is history, so a later caller does not get to rewrite it."""
    _record(store, reason="rollout completed", retired_by="agent_rocky")
    _record(store, reason="something else", retired_by="agent_natasha")
    row = store.get_deploy_generation_retirement("agent_bullwinkle", "gen-1")
    assert row["reason"] == "rollout completed"
    assert row["retired_by"] == "agent_rocky"


def test_retirements_are_scoped_per_agent_and_per_generation(store) -> None:
    _record(store, agent="agent_bullwinkle", generation="gen-1")
    assert store.is_deploy_generation_retired("agent_rocky", "gen-1") is False
    assert store.is_deploy_generation_retired("agent_bullwinkle", "gen-2") is False


# -- append-only -------------------------------------------------------------


def test_a_retirement_cannot_be_updated(store) -> None:
    """Unretiring by UPDATE would hand a resurrected barrier its authority back."""
    _record(store)
    with pytest.raises(StoreError):
        store.execute(
            "UPDATE deploy_generation_retirements SET generation = ? "
            "WHERE agent_id = ?",
            ("gen-2", "agent_bullwinkle"),
        )
    assert store.is_deploy_generation_retired("agent_bullwinkle", "gen-1") is True


def test_a_retirement_cannot_be_deleted(store) -> None:
    _record(store)
    with pytest.raises(StoreError):
        store.execute(
            "DELETE FROM deploy_generation_retirements WHERE agent_id = ?",
            ("agent_bullwinkle",),
        )
    assert store.is_deploy_generation_retired("agent_bullwinkle", "gen-1") is True


def test_the_table_refuses_an_empty_agent_or_generation_at_the_database(store) -> None:
    """The helper validates, but the column CHECKs are the durable guarantee."""
    for agent, generation in (("", "gen-1"), ("agent_bullwinkle", "")):
        with pytest.raises(StoreError):
            store.execute(
                """
                INSERT INTO deploy_generation_retirements (
                    id, agent_id, generation, retired_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("dgr_raw", agent, generation, "t", "t"),
            )


# -- lookup and listing ------------------------------------------------------


def test_an_unretired_generation_reads_as_absent_not_as_an_error(store) -> None:
    assert store.get_deploy_generation_retirement("agent_bullwinkle", "gen-1") is None
    assert store.is_deploy_generation_retired("agent_bullwinkle", "gen-1") is False


@pytest.mark.parametrize(
    "agent,generation", [("", "gen-1"), ("agent_bullwinkle", ""), ("", "")]
)
def test_an_incomplete_lookup_is_not_retired_rather_than_raising(
    store, agent, generation
) -> None:
    _record(store)
    assert store.get_deploy_generation_retirement(agent, generation) is None
    assert store.is_deploy_generation_retired(agent, generation) is False


def test_listing_returns_most_recently_retired_first(store) -> None:
    _record(store, generation="gen-1", retired_at="2026-08-01T00:00:00+00:00")
    _record(store, generation="gen-2", retired_at="2026-08-03T00:00:00+00:00")
    _record(store, generation="gen-3", retired_at="2026-08-02T00:00:00+00:00")
    rows = store.list_deploy_generation_retirements()
    assert [r["generation"] for r in rows] == ["gen-2", "gen-3", "gen-1"]


def test_listing_can_be_scoped_to_one_agent(store) -> None:
    _record(store, agent="agent_bullwinkle", generation="gen-1")
    _record(store, agent="agent_rocky", generation="gen-1")
    rows = store.list_deploy_generation_retirements(agent_id="agent_bullwinkle")
    assert [r["agent_id"] for r in rows] == ["agent_bullwinkle"]


def test_listing_bounds_the_window_half_open_on_retired_at(store) -> None:
    _record(store, generation="gen-1", retired_at="2026-08-01T00:00:00+00:00")
    _record(store, generation="gen-2", retired_at="2026-08-02T00:00:00+00:00")
    _record(store, generation="gen-3", retired_at="2026-08-03T00:00:00+00:00")
    rows = store.list_deploy_generation_retirements(
        since="2026-08-02T00:00:00+00:00", until="2026-08-03T00:00:00+00:00"
    )
    assert [r["generation"] for r in rows] == ["gen-2"]


def test_listing_honours_a_limit(store) -> None:
    for index in range(5):
        _record(store, generation="gen-%d" % index)
    assert len(store.list_deploy_generation_retirements(limit=2)) == 2


def test_listing_an_agent_with_nothing_retired_is_empty(store) -> None:
    _record(store, agent="agent_bullwinkle")
    assert store.list_deploy_generation_retirements(agent_id="agent_rocky") == []


# -- the barrier decision this record exists to make -------------------------


def test_a_resurrected_barrier_is_stale_once_its_generation_is_retired(
    store, tmp_path
) -> None:
    """The end-to-end shape: file says draining, the hub says that is over.

    The barrier file is what `worker._deployment_barrier_state` reads, and a
    rollback can restore it verbatim. With the retirement recorded, the same
    file contents now resolve to "stale", which is the whole point.
    """
    barrier = tmp_path / "deploy-barrier"
    barrier.write_text("gen-1\n", encoding="utf-8")
    observed = barrier.read_text(encoding="utf-8").strip()

    assert store.is_deploy_generation_retired("agent_bullwinkle", observed) is False
    _record(store, generation="gen-1", reason="rollout completed")
    assert store.is_deploy_generation_retired("agent_bullwinkle", observed) is True


def test_a_re_registered_agent_does_not_resurrect_a_retired_generation(store) -> None:
    """The record carries no FK to `agents`, so deleting the agent cannot
    cascade the retirement away and re-arm a stale barrier on that node."""
    now = "2026-08-21T00:00:00+00:00"
    store.execute(
        """
        INSERT INTO machines (
            id, hostname, labels, resources, trusted,
            created_at, updated_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("mach_1", "mini-3", "[]", "{}", 1, now, now, now),
    )
    store.execute(
        """
        INSERT INTO agents (
            id, machine_id, name, capabilities, resources,
            status, health_status, last_seen_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "agent_bullwinkle", "mach_1", "bullwinkle", "[]", "{}",
            "idle", "healthy", now, now, now,
        ),
    )
    _record(store)
    store.execute("DELETE FROM agents WHERE id = ?", ("agent_bullwinkle",))
    assert store.is_deploy_generation_retired("agent_bullwinkle", "gen-1") is True
