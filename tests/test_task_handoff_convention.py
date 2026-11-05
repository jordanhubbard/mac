"""Executable example of the task-reference handoff convention."""

from __future__ import annotations

from pathlib import Path

from mac.test_support import control_plane_on, dsn_for


ROOT = Path(__file__).resolve().parents[1]


def test_receiver_retrieves_handoff_context_from_task_reference(tmp_path):
    cp = control_plane_on(dsn_for(tmp_path))
    machine = cp.register_machine("host")
    sender = cp.register_agent(machine.id, "sender")
    receiver = cp.register_agent(machine.id, "receiver")
    source = cp.create_task("source investigation", project="mac")

    for record_type, content in (
        ("decision", "Use task-scoped records as the first handoff substrate."),
        ("observation", "Graph expansion is not available in MAC today."),
    ):
        cp.add_memory(
            task_id=source.id,
            subject_type="task",
            subject_id=source.id,
            record_type=record_type,
            content=content,
            evidence_id=None,
            created_by=sender.id,
        )

    envelope = {
        "schema": "mac.task_handoff.v1",
        "task_ref": source.id,
        "retrieval": {
            "substrate": "mac.memory_records",
            "task_id": source.id,
            "order": "desc",
            "limit": 20,
            "record_type_prefix": None,
            "expansion_hops": 0,
        },
    }
    stream = cp.agentbus.open_stream(
        sender.id,
        receiver.id,
        task_id=source.id,
        topic="task.handoff",
        content_type="application/json",
    )
    cp.agentbus.append_chunk(stream.id, sender.id, envelope, final=True)

    message = cp.read_agentbus_inbox(receiver.id)[0].payload
    retrieval = message["retrieval"]
    assert message["schema"] == "mac.task_handoff.v1"
    assert message["task_ref"] == retrieval["task_id"]
    assert retrieval["substrate"] == "mac.memory_records"
    assert retrieval["expansion_hops"] == 0

    records = cp.search_memory(
        task_id=retrieval["task_id"],
        record_type_prefix=retrieval["record_type_prefix"],
        order=retrieval["order"],
        limit=retrieval["limit"],
    )
    assert {record.content for record in records} == {
        "Use task-scoped records as the first handoff substrate.",
        "Graph expansion is not available in MAC today.",
    }
    assert "summary" not in message


def test_convention_defines_three_handoff_before_after_validation():
    text = (ROOT / "docs" / "task-handoff-convention.md").read_text(encoding="utf-8")
    assert "first three cross-agent handoffs" in text
    assert '"cohort_position": 1' in text
    assert '"before":' in text and '"after":' in text
    assert "clarifying questions" in text
    assert "awaiting_receiver_outcome" in text


def test_memory_write_skill_rejects_generic_relation_types():
    text = (ROOT / "skills" / "record-user-directed-work" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Use specific memory relations" in text
    assert "Do not use catch-all" in text
    for relation in ("implements", "depends_on", "contradicts", "supersedes", "evidenced_by"):
        assert relation in text
