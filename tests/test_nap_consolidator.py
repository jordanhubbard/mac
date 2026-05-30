"""mem-08: nap consolidator tests.

The interesting one is ``test_consolidate_and_recall_end_to_end``:
write a handful of memory_records the way an agent would, run the
consolidator, then ask the vector recall for the nap_summary by its
own content. Proves the full pipeline:

    memory_records → nap consolidator → summary memory_record →
    vector writer → Qdrant → recall

with no real network or LLM calls (fake-Qdrant transport + default
hash embedder + default string-concat summarizer).
"""
from __future__ import annotations

import pytest

from mac.nap_consolidator import NapConsolidatorService
from mac.services import ControlPlane
from mac.vector_writer_service import VectorWriterService

from tests.test_vector_writer_service import _FakeQdrant  # reuse the stub


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


@pytest.fixture()
def fake_qdrant() -> _FakeQdrant:
    return _FakeQdrant()


@pytest.fixture()
def writer(cp, fake_qdrant) -> VectorWriterService:
    return VectorWriterService(
        memory=cp.memory,
        qdrant_url="http://fake.invalid:6333",
        embedding_dim=64,
        transport=fake_qdrant,
    )


def _add_memory_as_agent(cp, agent_id, content, **kwargs):
    return cp.add_memory(
        task_id=kwargs.get("task_id"),
        subject_type=kwargs.get("subject_type", "topic"),
        subject_id=kwargs.get("subject_id"),
        record_type=kwargs.get("record_type", "note"),
        content=content,
        evidence_id=None,
        created_by=agent_id,
    )


# ---------------------------------------------------------------------------
# Unit behavior
# ---------------------------------------------------------------------------


def test_consolidate_groups_by_task_id(cp):
    """One summary per distinct task_id the agent touched."""
    agent_id = "agent_rocky"
    cp.create_task("T1")  # we only need ids; not the full lifecycle.
    cp.create_task("T2")
    tasks = sorted(t.id for t in cp.list_tasks() if t.title in ("T1", "T2"))
    _add_memory_as_agent(cp, agent_id, "task one fact A", task_id=tasks[0])
    _add_memory_as_agent(cp, agent_id, "task one fact B", task_id=tasks[0])
    _add_memory_as_agent(cp, agent_id, "task two fact X", task_id=tasks[1])
    consolidator = NapConsolidatorService(store=cp.store, memory=cp.memory)
    report = consolidator.consolidate_agent(agent_id, embed_into_medium=False)
    assert report["records_considered"] == 3
    assert report["groups"] == 2
    assert report["summaries_written"] == 2
    summaries = [cp.memory.get_memory(mid) for mid in report["summary_memory_ids"]]
    assert all(s.record_type == "nap_summary" for s in summaries)
    assert all(s.subject_id == agent_id for s in summaries)
    # Each summary references its task_id.
    summary_task_ids = {s.task_id for s in summaries}
    assert summary_task_ids == set(tasks)


def test_consolidate_skips_prior_nap_summaries(cp):
    """The consolidator must NOT summarize summaries — a second pass
    over already-summarized records writes nothing new."""
    agent_id = "agent_rocky"
    _add_memory_as_agent(cp, agent_id, "fact A")
    _add_memory_as_agent(cp, agent_id, "fact B")
    consolidator = NapConsolidatorService(store=cp.store, memory=cp.memory)
    first = consolidator.consolidate_agent(agent_id, embed_into_medium=False)
    assert first["summaries_written"] == 1
    second = consolidator.consolidate_agent(agent_id, embed_into_medium=False)
    # No fresh records (the only newer rows are nap_summary, which the
    # consolidator filters out), so nothing to summarize.
    assert second["records_considered"] == 0
    assert second["summaries_written"] == 0


def test_consolidate_uses_pluggable_summarizer(cp):
    """Operators can swap a real LLM in. The contract is just a
    Callable[[List[MemoryRecord], dict], str]."""
    agent_id = "agent_rocky"
    _add_memory_as_agent(cp, agent_id, "first")
    _add_memory_as_agent(cp, agent_id, "second")

    def stub_llm(records, context):
        return "AGENT %s saw %d things" % (context["agent_id"], len(records))

    consolidator = NapConsolidatorService(
        store=cp.store,
        memory=cp.memory,
        summarizer_fn=stub_llm,
    )
    report = consolidator.consolidate_agent(agent_id, embed_into_medium=False)
    summary = cp.memory.get_memory(report["summary_memory_ids"][0])
    assert summary.content == "AGENT agent_rocky saw 2 things"


def test_consolidate_embeds_when_writer_provided(cp, writer, fake_qdrant):
    """When a VectorWriterService is provided, every summary is embedded
    into the medium tier — visible in fake Qdrant and vector_refs."""
    agent_id = "agent_rocky"
    _add_memory_as_agent(cp, agent_id, "alpha")
    _add_memory_as_agent(cp, agent_id, "beta")
    consolidator = NapConsolidatorService(
        store=cp.store,
        memory=cp.memory,
        vector_writer=writer,
    )
    report = consolidator.consolidate_agent(agent_id)
    assert report["summaries_embedded"] == report["summaries_written"]
    assert report["summaries_embedded"] >= 1
    refs = cp.memory.list_vector_refs(collection="mac_memory_medium")
    summary_ids = set(report["summary_memory_ids"])
    embedded_for_summaries = [r for r in refs if r.memory_id in summary_ids]
    assert len(embedded_for_summaries) == report["summaries_embedded"]


def test_consolidate_handles_no_records_gracefully(cp):
    """An agent that has authored nothing returns an empty report —
    no error, no orphan summary, idempotent."""
    consolidator = NapConsolidatorService(store=cp.store, memory=cp.memory)
    report = consolidator.consolidate_agent("agent_lonely", embed_into_medium=False)
    assert report["records_considered"] == 0
    assert report["summaries_written"] == 0


# ---------------------------------------------------------------------------
# End-to-end: consolidate, embed, recall
# ---------------------------------------------------------------------------


def test_nap_cycle_runs_full_arc_with_consolidation(cp, writer, fake_qdrant):
    """mem-08 autonomy: run_nap_cycle does begin + consolidate +
    complete in one shot. Agent ends IDLE; nap_run is COMPLETED."""
    from mac.models import AgentStatus, NapStatus, TaskState

    machine = cp.register_machine("h1")
    agent = cp.register_agent(machine.id, "agent-cycle", capabilities=[])
    _add_memory_as_agent(cp, agent.id, "first thing the agent did")
    _add_memory_as_agent(cp, agent.id, "second thing the agent did")
    out = cp.run_nap_cycle(agent.id, vector_writer=writer)
    # Run reached COMPLETED.
    assert out["nap_run"]["status"] == NapStatus.COMPLETED.value
    # Agent is back to IDLE.
    refreshed = cp.get_agent(agent.id)
    assert refreshed.status == AgentStatus.IDLE.value
    # Consolidation produced a summary that was embedded.
    assert out["consolidation"]["summaries_written"] >= 1
    assert out["consolidation"]["summaries_embedded"] >= 1
    assert out["consolidation_error"] is None


def test_nap_cycle_completes_even_when_consolidate_fails(cp):
    """A consolidation failure must not strand the agent in DRAINING —
    complete_nap runs in a finally block so the lifecycle always
    resolves."""
    from mac.models import AgentStatus, NapStatus
    from unittest.mock import patch

    machine = cp.register_machine("h1")
    agent = cp.register_agent(machine.id, "agent-x", capabilities=[])
    with patch.object(cp, "consolidate_nap", side_effect=RuntimeError("boom")):
        out = cp.run_nap_cycle(agent.id)
    assert out["consolidation_error"] == "boom"
    assert out["nap_run"]["status"] == NapStatus.COMPLETED.value
    assert cp.get_agent(agent.id).status == AgentStatus.IDLE.value


def test_list_due_nap_agents_finds_opened_windows(cp):
    """An agent whose window has opened today and not completed is due."""
    from datetime import datetime, timedelta, timezone

    machine = cp.register_machine("h1")
    a = cp.register_agent(machine.id, "agent-a", capabilities=[])
    b = cp.register_agent(machine.id, "agent-b", capabilities=[])
    # Both agents pick offsets within the nap-window-cap [0, 360).
    # agent-a: opens at midnight UTC; agent-b: opens at 5h UTC. Test at
    # noon means both have already opened today; neither has been
    # completed → both are due.
    cp.configure_nap(a.id, offset_minutes=0, window_minutes=30)
    cp.configure_nap(b.id, offset_minutes=300, window_minutes=15)
    as_of = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    due = cp.list_due_nap_agents(as_of=as_of)
    due_ids = {item["agent_id"] for item in due}
    assert a.id in due_ids
    assert b.id in due_ids


def test_list_due_nap_agents_skips_already_completed_windows(cp):
    """Once last_completed_at >= window_start, the agent isn't due."""
    from datetime import datetime, timezone

    machine = cp.register_machine("h1")
    a = cp.register_agent(machine.id, "agent-a", capabilities=[])
    cp.configure_nap(a.id, offset_minutes=0, window_minutes=30)
    # Stamp a recent completion at today's window (midnight or after).
    today_midnight = datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc)
    cp.store.execute(
        "UPDATE nap_schedules SET last_completed_at = ? WHERE agent_id = ?",
        ((today_midnight).isoformat(), a.id),
    )
    as_of = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    due_ids = {item["agent_id"] for item in cp.list_due_nap_agents(as_of=as_of)}
    assert a.id not in due_ids


def test_consolidate_and_recall_end_to_end(cp, writer, fake_qdrant):
    """The 'does this memory tier actually do its job?' test.

    1. Two agents write memory_records about distinct topics.
    2. Each agent's consolidator runs → one nap_summary per agent.
    3. Both summaries get embedded into the medium tier.
    4. Recall against one summary's text returns that summary as the
       top hit, distinct from the other agent's summary.

    Uses the deterministic hash embedder (no semantic similarity);
    the exact-text match still scores 1.0 because identical text
    hashes to identical vectors. Swap embed_fn for a real model and
    the same test runs end-to-end with real semantic recall.
    """
    rocky = "agent_rocky"
    natasha = "agent_natasha"
    _add_memory_as_agent(cp, rocky, "Rocky reviewed the slack notifier last Tuesday")
    _add_memory_as_agent(cp, rocky, "Rocky also looked at the prune timer")
    _add_memory_as_agent(cp, natasha, "Natasha worked on the Qdrant collection schema")

    consolidator = NapConsolidatorService(
        store=cp.store, memory=cp.memory, vector_writer=writer,
    )
    rocky_report = consolidator.consolidate_agent(rocky)
    natasha_report = consolidator.consolidate_agent(natasha)
    assert rocky_report["summaries_embedded"] == 1
    assert natasha_report["summaries_embedded"] == 1

    rocky_summary = cp.memory.get_memory(rocky_report["summary_memory_ids"][0])
    natasha_summary = cp.memory.get_memory(natasha_report["summary_memory_ids"][0])

    # Recall against rocky's summary content → rocky's summary is the
    # top hit (score = 1.0 because same text hashes to same vector).
    hits = writer.recall(rocky_summary.content, limit=5)
    assert hits, "recall should not be empty"
    top = hits[0]
    assert top["payload"]["memory_id"] == rocky_summary.id
    assert top["score"] > 0.99
    # Natasha's summary must rank lower — they're about different
    # topics, so their hashed vectors are uncorrelated.
    natasha_hits = [h for h in hits if h["payload"]["memory_id"] == natasha_summary.id]
    if natasha_hits:
        assert natasha_hits[0]["score"] < top["score"]
