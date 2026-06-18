"""Tests for the gateway and vector-memory provenance seams.

mac doesn't implement Slack or Qdrant — those live on the Hermes side. But
mac records the *pointers* so cross-process flow is auditable.
"""

import pytest

from mac.models import NotFoundError, ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _setup_binding(cp):
    tenant = cp.register_tenant("ops")
    hermes = cp.register_hermes_instance(tenant.id, "rocky")
    return cp.register_platform_binding(
        tenant.id, hermes.id, "slack", "T123/C456", display_name="#ops"
    )


def test_track_conversation_is_idempotent_and_touches_last_seen(cp):
    binding = _setup_binding(cp)
    first = cp.track_conversation(
        binding.id,
        "1700000000.000100",
        summary="user asked about deploy failure",
        metadata={"user_id": "U99"},
    )
    second = cp.track_conversation(
        binding.id,
        "1700000000.000100",
        summary="updated summary",
        metadata={"latest_message": "still failing"},
    )
    assert second.id == first.id
    assert second.summary == "updated summary"
    assert second.metadata["user_id"] == "U99"
    assert second.metadata["latest_message"] == "still failing"
    assert second.first_seen_at == first.first_seen_at
    assert second.last_seen_at >= first.last_seen_at


def test_track_conversation_links_latest_task(cp):
    binding = _setup_binding(cp)
    task = cp.create_task("investigate", required_capabilities=["ops"])
    thread = cp.track_conversation(
        binding.id,
        "thread-99",
        latest_task_id=task.id,
        summary="opened ticket",
    )
    assert thread.latest_task_id == task.id


def test_track_conversation_rejects_unknown_binding_and_task(cp):
    with pytest.raises(NotFoundError):
        cp.track_conversation("binding_does_not_exist", "thread-1")
    binding = _setup_binding(cp)
    with pytest.raises(NotFoundError):
        cp.track_conversation(binding.id, "thread-1", latest_task_id="task_does_not_exist")


def test_track_conversation_requires_external_thread_id(cp):
    binding = _setup_binding(cp)
    with pytest.raises(ValidationError):
        cp.track_conversation(binding.id, "")


def test_list_conversation_threads_filters_by_binding(cp):
    binding_a = _setup_binding(cp)
    tenant_b = cp.register_tenant("other")
    hermes_b = cp.register_hermes_instance(tenant_b.id, "natasha")
    binding_b = cp.register_platform_binding(
        tenant_b.id, hermes_b.id, "telegram", "chat-7"
    )
    cp.track_conversation(binding_a.id, "a-1")
    cp.track_conversation(binding_a.id, "a-2")
    cp.track_conversation(binding_b.id, "b-1")

    a_threads = cp.list_conversation_threads(platform_binding_id=binding_a.id)
    assert {t.external_thread_id for t in a_threads} == {"a-1", "a-2"}


def test_record_vector_ref_links_memory_to_external_index(cp):
    cp.register_tenant("ops")
    task = cp.create_task("captured", required_capabilities=["ops"])
    memory = cp.add_memory(
        task.id,
        subject_type="task",
        subject_id=task.id,
        record_type="decision",
        content="Approved canary at 10%.",
        evidence_id=None,
        created_by="human",
    )
    ref = cp.record_vector_ref(
        memory_id=memory.id,
        vector_db="qdrant",
        collection="hermes-memory",
        point_id="point-abc-123",
        embedding_model="text-embedding-3-large",
        metadata={"dims": 3072},
    )
    assert ref.memory_id == memory.id
    assert ref.point_id == "point-abc-123"
    # Looked up via the canonical filter set used by audits.
    listing = cp.list_vector_refs(memory_id=memory.id)
    assert [r.id for r in listing] == [ref.id]
    by_collection = cp.list_vector_refs(vector_db="qdrant", collection="hermes-memory")
    assert ref.id in {r.id for r in by_collection}


def test_record_vector_ref_rejects_unknown_memory_and_blank_fields(cp):
    with pytest.raises(NotFoundError):
        cp.record_vector_ref("memory_does_not_exist", "qdrant", "c", "p")
    task = cp.create_task("dummy", required_capabilities=["ops"])
    memory = cp.add_memory(
        task.id, "task", task.id, "decision", "x", None, "human"
    )
    with pytest.raises(ValidationError):
        cp.record_vector_ref(memory.id, "", "c", "p")
    with pytest.raises(ValidationError):
        cp.record_vector_ref(memory.id, "qdrant", "", "p")
    with pytest.raises(ValidationError):
        cp.record_vector_ref(memory.id, "qdrant", "c", "")


def test_conversation_thread_summary_is_length_capped(cp):
    binding = _setup_binding(cp)
    huge = "x" * (cp.memory.CONVERSATION_SUMMARY_MAX_CHARS + 1)
    with pytest.raises(ValidationError):
        cp.track_conversation(binding.id, "thread-cap", summary=huge)


def test_conversation_threads_surface_in_unified_events_stream(cp):
    binding = _setup_binding(cp)
    thread = cp.track_conversation(
        binding.id,
        "t-1",
        summary="user asked about deploy",
    )
    events = cp.list_events(subject_type="conversation_thread", subject_id=thread.id)
    assert events
    assert events[0]["event_type"] == "gateway.thread_tracked"
    assert events[0]["detail"]["platform_binding_id"] == binding.id
    assert events[0]["detail"]["external_thread_id"] == "t-1"


def test_vector_refs_surface_in_unified_events_stream(cp):
    task = cp.create_task("vec", required_capabilities=["ops"])
    memory = cp.add_memory(task.id, "task", task.id, "decision", "approve", None, "human")
    ref = cp.record_vector_ref(
        memory_id=memory.id,
        vector_db="qdrant",
        collection="hermes",
        point_id="pt-1",
        embedding_model="model-x",
    )
    events = cp.list_events(subject_type="vector_ref", subject_id=memory.id)
    assert events
    assert events[0]["event_type"] == "vector.indexed"
    assert events[0]["detail"]["vector_db"] == "qdrant"
    assert events[0]["detail"]["point_id"] == "pt-1"
    # Make sure the per-ref id can also be retrieved via the generic list.
    all_events = cp.list_events(event_type="vector.indexed")
    assert ref.id in {e["id"] for e in all_events}


def test_vector_ref_unique_on_point_per_collection(cp):
    task = cp.create_task("dummy", required_capabilities=["ops"])
    memory = cp.add_memory(task.id, "task", task.id, "fact", "y", None, "human")
    cp.record_vector_ref(memory.id, "qdrant", "c", "p1")
    # Re-recording the same (db, collection, point) violates the uniqueness
    # constraint. The contract: vector indexers must update their own point,
    # not re-register it under mac.
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        cp.record_vector_ref(memory.id, "qdrant", "c", "p1")
