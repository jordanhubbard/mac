"""The nap window must advance, and the completion event must say what happened.

task_8f7d9e1f was filed on the observation that all 10 sampled
``agent.nap_completed`` events carried ``summary_evidence_id: null``, and that
project=mac's memory store held only hand-authored notes across nine days of
continuous fleet operation. Two separate things were wrong, and neither was the
one the event suggested.

FIRST: ``summary_evidence_id`` is not the loop's output channel. ``run_nap_cycle``
passes ``None`` unconditionally (services.py) because a nap's durable output is
memory records written by ``consolidate_agent``, not an Evidence row. The field
is an optional operator-supplied hook on ``mac nap complete``. So the event's
only observable field was always null by construction, and a nap that
consolidated 744 records looked exactly like a nap that did nothing.

SECOND, and the real defect: ``_latest_nap_window_end`` looked up the agent's
last summary by ``created_by = 'nap-consolidator:<agent>'``. That is the ACTOR,
and it only takes that value when an operator runs the consolidator directly;
every nap the fleet drives itself writes ``nap-cycle:<agent>`` or the ticker's
actor. So for any agent carrying one old operator-run summary, the lookup kept
returning that ancient row and the window never moved.

Measured on the live hub 2026-08-07, agent_rocky's last six naps:

    window_start: 2026-05-30T03:48:54   (identical on all six)
    records_considered: 741-744, groups: 433-435
    summaries_written: 0, 1, 2, 0, 0, 0
    summaries_skipped_duplicate: 433-435

Over two months of re-reading the same records into the same groups. The
duplicate guard -- which exists for a good reason, 154,324 rows carrying 4,540
distinct bodies -- was the only thing stopping the write, and in doing its job
correctly it hid the stuck window completely.

The other four agents showed ``window_start`` within the hour, because they have
no such row and correctly fall through to the nap_run fallback. Any test that
only covered the fallback would have passed against the broken build.
"""

from __future__ import annotations

import pytest

from mac.agent_state_service import _nap_completion_outcome
from mac.nap_consolidator import NapConsolidatorService
from mac.models import utcnow
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


@pytest.fixture()
def consolidator(cp):
    """The consolidator the hub builds per nap (services.consolidate_nap).

    vector_writer=None is summary-only mode -- these tests are about which
    records are selected, not about embedding them.
    """
    return NapConsolidatorService(store=cp.store, memory=cp.memory, vector_writer=None)


@pytest.fixture()
def agent(cp):
    machine = cp.register_machine("nap-host")
    return cp.register_agent(machine.id, "napper", capabilities=["python"])


def _summary(cp, agent_id, *, created_by, created_at=None):
    """A stored nap summary, exactly as consolidate_agent writes one.

    ``subject_type``/``subject_id`` identify the agent; ``created_by`` records
    whoever ran the pass. The bug was reading identity out of the second one.
    """
    record = cp.memory.add_memory(
        task_id=None,
        subject_type="nap_summary",
        subject_id=agent_id,
        record_type="nap_summary",
        content="a summary body",
        evidence_id=None,
        created_by=created_by,
    )
    if created_at is not None:
        cp.store.execute(
            "UPDATE memory_records SET created_at = ? WHERE id = ?",
            (created_at, record.id),
        )
    return record


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


def test_a_cycle_written_summary_moves_the_window(cp, consolidator, agent):
    """The regression. This is what every fleet-driven nap writes.

    Against the old lookup this returns None and the window silently resets to
    the nap_run fallback -- or, when an older operator-run summary exists, to
    that ancient timestamp. Either way the agent's own consolidation history is
    invisible to it.
    """
    _summary(
        cp, agent.id, created_by="nap-cycle:%s" % agent.id, created_at="2026-08-07T05:00:00+00:00"
    )

    window = consolidator._latest_nap_window_end(agent.id)

    assert window == "2026-08-07T05:00:00+00:00", (
        "a summary written by the nap cycle did not advance the window, so the "
        "next nap re-reads everything it already consolidated"
    )


def test_a_ticker_written_summary_moves_the_window(cp, consolidator, agent):
    """The ticker supplies its own actor; that must not hide the summary."""
    _summary(cp, agent.id, created_by="nap-ticker", created_at="2026-08-07T05:00:00+00:00")

    assert consolidator._latest_nap_window_end(agent.id) == "2026-08-07T05:00:00+00:00"


def test_an_operator_run_summary_still_moves_the_window(cp, consolidator, agent):
    """The one case the old lookup did handle must keep working."""
    _summary(
        cp,
        agent.id,
        created_by="nap-consolidator:%s" % agent.id,
        created_at="2026-08-07T05:00:00+00:00",
    )

    assert consolidator._latest_nap_window_end(agent.id) == "2026-08-07T05:00:00+00:00"


def test_the_newest_summary_wins_regardless_of_who_wrote_it(cp, consolidator, agent):
    """agent_rocky's exact shape: one old operator row, newer cycle rows.

    The old lookup saw ONLY the operator row and pinned the window to May while
    the cycle wrote fresher summaries every hour. This is the assertion that
    fails against the unfixed code even though the two single-writer tests
    above could be made to pass by a narrower change.
    """
    _summary(
        cp,
        agent.id,
        created_by="nap-consolidator:%s" % agent.id,
        created_at="2026-05-30T03:48:54+00:00",
    )
    _summary(
        cp,
        agent.id,
        created_by="nap-cycle:%s" % agent.id,
        created_at="2026-08-07T05:00:00+00:00",
    )

    window = consolidator._latest_nap_window_end(agent.id)

    assert window == "2026-08-07T05:00:00+00:00", (
        "the window pinned to the stale operator-written summary; on the live "
        "hub that froze agent_rocky at 2026-05-30 for over two months"
    )


def test_another_agents_summary_does_not_move_this_agents_window(cp, consolidator, agent):
    """Identity still has to mean something after the change."""
    machine = cp.register_machine("other-host")
    other = cp.register_agent(machine.id, "other-napper", capabilities=["python"])
    _summary(
        cp, other.id, created_by="nap-cycle:%s" % other.id, created_at="2026-08-07T05:00:00+00:00"
    )

    assert consolidator._latest_nap_window_end(agent.id) is None


def test_with_no_summary_it_falls_back_to_the_last_completed_nap(cp, consolidator, agent):
    """Four of the five live agents run entirely on this path."""
    run = cp.begin_nap(agent.id)
    cp.complete_nap(run.id if hasattr(run, "id") else run["nap_run"]["id"])

    window = consolidator._latest_nap_window_end(agent.id)

    assert window is not None


def test_with_no_history_at_all_the_window_is_open(cp, consolidator, agent):
    assert consolidator._latest_nap_window_end(agent.id) is None


def test_consecutive_consolidations_do_not_reconsider_the_same_records(cp, consolidator, agent):
    """End to end: the property the window exists to provide.

    ``created_by`` is the cycle's actor, which matters and is not incidental.
    An earlier version of this test omitted it and got ``consolidate_agent``'s
    default of ``nap-consolidator:<agent>`` -- the one actor the broken lookup
    DID match -- so it passed against the unfixed code while claiming to catch
    the defect. Passing what the hub passes (services.py: ``created_by=actor or
    "nap-cycle:%s" % agent_id``) is what makes this a real regression test.
    """
    cp.memory.add_memory(
        task_id=None,
        subject_type="agent",
        subject_id=agent.id,
        record_type="observation",
        content="something learned",
        evidence_id=None,
        created_by=agent.id,
    )

    cycle_actor = "nap-cycle:%s" % agent.id
    first = consolidator.consolidate_agent(
        agent.id,
        embed_into_medium=False,
        emit_dream_artifacts=False,
        created_by=cycle_actor,
    )
    assert first["records_considered"] == 1
    assert first["summaries_written"] == 1

    second = consolidator.consolidate_agent(
        agent.id,
        embed_into_medium=False,
        emit_dream_artifacts=False,
        created_by=cycle_actor,
    )

    assert second["records_considered"] == 0, (
        "the second pass re-read records the first pass already consolidated; "
        "on the live hub that meant 744 records re-grouped every nap for two "
        "months, with the duplicate guard silently absorbing all of it"
    )


# --------------------------------------------------------------------------
# The completion event
# --------------------------------------------------------------------------


def test_the_outcome_carries_the_consolidation_counters():
    outcome = _nap_completion_outcome(
        {
            "consolidation": {
                "records_considered": 744,
                "groups": 435,
                "summaries_written": 0,
                "summaries_skipped_duplicate": 435,
                "window_start": "2026-05-30T03:48:54",
                "summary_memory_ids": ["mem_1", "mem_2"],
            }
        }
    )

    assert outcome["records_considered"] == 744
    assert outcome["summaries_written"] == 0
    assert outcome["summaries_skipped_duplicate"] == 435
    assert outcome["window_start"] == "2026-05-30T03:48:54"


def test_the_outcome_does_not_carry_the_id_lists():
    """The event stream is the firehose that once put 16GB of mac.db on the floor.

    Counters are fixed-width; ``summary_memory_ids`` grows with the work done,
    which is exactly the shape that caused that.
    """
    outcome = _nap_completion_outcome(
        {"consolidation": {"summary_memory_ids": ["mem_%d" % i for i in range(500)]}}
    )

    assert "summary_memory_ids" not in outcome
    assert "dream_memory_ids" not in outcome


def test_a_consolidation_failure_reaches_the_event():
    """A nap that threw must not complete looking clean."""
    outcome = _nap_completion_outcome(
        {"consolidation": {}, "consolidation_error": "vector writer unreachable"}
    )

    assert outcome["consolidation_error"] == "vector writer unreachable"


def test_per_group_errors_are_counted_not_inlined():
    outcome = _nap_completion_outcome(
        {"consolidation": {"errors": [{"phase": "embed_summary"}, {"phase": "embed_dream"}]}}
    )

    assert outcome["consolidation_group_errors"] == 2


@pytest.mark.parametrize(
    "detail", [None, {}, {"consolidation": None}, {"consolidation": "x"}, "junk"]
)
def test_absent_or_malformed_detail_is_empty_not_an_exception(detail):
    """This annotates a completion; it must never be why a nap fails to finish.

    A raise here would propagate out of ``complete_nap`` and strand the agent
    in DRAINING -- trading a reporting gap for an offline agent.
    """
    assert _nap_completion_outcome(detail) == {}


def test_the_completion_event_reports_the_outcome(cp, agent):
    """End to end: the event an observer actually reads.

    The filing was written from ``agent.nap_completed`` alone, and that event
    could not distinguish a working loop from a dead one. It has to now.
    """
    run = cp.begin_nap(agent.id)
    run_id = run.id if hasattr(run, "id") else run["nap_run"]["id"]

    cp.complete_nap(
        run_id,
        summary_evidence_id=None,
        detail={
            "consolidation": {
                "records_considered": 12,
                "groups": 3,
                "summaries_written": 3,
                "summaries_skipped_duplicate": 0,
                "window_start": utcnow(),
            }
        },
    )

    events = cp.observability.list_observability(name="agent.nap_completed", limit=5)
    detail = events[0].detail
    assert detail["records_considered"] == 12
    assert detail["summaries_written"] == 3
    assert detail["summary_evidence_id"] is None, (
        "still null, and that is correct -- the point is that it is no longer "
        "the only thing the event says"
    )
