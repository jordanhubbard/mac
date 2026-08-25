"""Group communication on AgentBus (task_588b67fd, AgentBus audit 5/7).

Multi-participant streams: one conversation lives in one stream. Membership
governs both reads and appends; a NULL participants column preserves the
legacy sender/recipient pair semantics exactly.
"""

from __future__ import annotations

import pytest

from mac.models import AuthorizationError, ValidationError
from mac.services import ControlPlane


@pytest.fixture()
def cp() -> ControlPlane:
    return ControlPlane.in_memory()


def _agents(cp: ControlPlane, *names: str):
    machine = cp.register_machine("group-host")
    return [cp.register_agent(machine.id, name, agent_id="agent_%s" % name) for name in names]


def test_group_publish_opens_shared_stream_all_members_converse(
    cp: ControlPlane,
) -> None:
    natasha, rocky, bullwinkle, outsider = _agents(cp, "natasha", "rocky", "bullwinkle", "outsider")
    published = cp.publish_agentbus_content(
        sender_agent_id=natasha.id,
        participant_agent_ids=[natasha.id, rocky.id, bullwinkle.id],
        topic="peer.message.v1",
        content_type="application/vnd.mac.agent-peer+json",
        payload={
            "schema": "mac.agent.peer_message.v1",
            "message": "rerun the benchmark and report numbers",
        },
    )
    stream = published["stream"]
    assert set(stream["participants"]) == {natasha.id, rocky.id, bullwinkle.id}
    # A group publish OPENS a conversation: replies land on the same stream.
    assert stream["status"] == "open"

    # Any member appends; everyone reads the whole thread.
    cp.append_agentbus_chunk(
        stream["id"],
        rocky.id,
        payload={"schema": "mac.agent.peer_reply.v1", "reply": "on it"},
    )
    cp.append_agentbus_chunk(
        stream["id"],
        bullwinkle.id,
        payload={"schema": "mac.agent.peer_reply.v1", "reply": "5090 numbers inbound"},
    )
    chunks = cp.read_agentbus_chunks(bullwinkle.id, stream["id"])
    assert [chunk.sender_agent_id for chunk in chunks] == [
        natasha.id,
        rocky.id,
        bullwinkle.id,
    ]

    # Membership is ADDRESSING: every member sees the stream in their own
    # listing and an outsider does not, because it is not their conversation.
    for member in (natasha, rocky, bullwinkle):
        assert any(item.id == stream["id"] for item in cp.list_agentbus_streams(agent_id=member.id))
    assert not any(
        item.id == stream["id"] for item in cp.list_agentbus_streams(agent_id=outsider.id)
    )
    # It is NOT access: an outsider may read the conversation (the bus is not
    # confidential) but may not speak into it.
    assert [
        chunk.sender_agent_id for chunk in cp.read_agentbus_chunks(outsider.id, stream["id"])
    ] == [natasha.id, rocky.id, bullwinkle.id]
    with pytest.raises(AuthorizationError):
        cp.append_agentbus_chunk(stream["id"], outsider.id, payload={"nope": True})

    # Only the opener closes the conversation.
    with pytest.raises(AuthorizationError):
        cp.close_agentbus_stream(stream["id"], rocky.id)
    closed = cp.close_agentbus_stream(stream["id"], natasha.id)
    assert closed.status == "closed"


def test_group_stream_validation_and_legacy_pair_semantics(cp: ControlPlane) -> None:
    natasha, rocky = _agents(cp, "natasha", "rocky")
    # Opener alone is not a group.
    with pytest.raises(ValidationError, match="at least one participant"):
        cp.open_agentbus_stream(natasha.id, participant_agent_ids=[natasha.id])
    # Unknown members are refused.
    with pytest.raises(Exception):
        cp.open_agentbus_stream(natasha.id, participant_agent_ids=[rocky.id, "agent_ghost"])
    # Legacy pair publish is untouched: one-shot, closed, pair-authorized.
    published = cp.publish_agentbus_content(
        sender_agent_id=natasha.id,
        recipient_agent_id=rocky.id,
        payload={"ping": True},
    )
    assert published["stream"]["participants"] is None
    assert published["stream"]["status"] == "closed"
    with pytest.raises(AuthorizationError):
        cp.append_agentbus_chunk(published["stream"]["id"], rocky.id, payload={"pong": True})
