"""Agents are told the bus exists, and told to use it narrowly.

AgentBus has been shippable for a long time and nothing in an agent's context
mentioned it — the same failure mood-01 fixed for mood overlays, whose docstring
says it plainly: "mac already stores per-agent mood overlays ... but nothing ever
rendered them into the agent's prompt, so a set mood had no effect."

Two populations, two separate surfaces, neither of which previously said
anything about coordinating:

* `mac-runtime-context.md` — injected into every Hermes session;
* `build_task_prompt` — the coding CLI the executor launches per task.

The scope is the point. This fleet's recorded collision is two agents editing
the same checkout (CLAUDE.md: one nearly swept ~1,200 lines of another's work
into an unrelated commit; a second did). So agents are told to announce *the
repository and paths they are about to modify* — a fact a peer can act on — and
explicitly told NOT to narrate progress. Every message is durable and audited
into action_events; an inbox of status updates is one everybody learns to
ignore, which would make the whole channel worthless.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac.executor_prompt import build_task_prompt

TASK = {
    "id": "task_1",
    "metadata": {"origin": {"repository_contract": {"schema": "mac.repository_contract.v1"}}},
}


def _prompt(monkeypatch, agent_id: str | None) -> str:
    if agent_id is None:
        monkeypatch.delenv("MAC_AGENT_ID", raising=False)
    else:
        monkeypatch.setenv("MAC_AGENT_ID", agent_id)
    return build_task_prompt(TASK)


# --- the executor prompt --------------------------------------------------


def test_the_executor_is_told_it_is_one_of_several_agents(monkeypatch):
    text = _prompt(monkeypatch, "agent_abc")
    assert "Coordination:" in text
    assert "one of several agents" in text


def test_it_names_the_actual_command_with_the_agents_own_id(monkeypatch):
    """A generic 'coordinate with peers' instruction invites invented commands."""
    text = _prompt(monkeypatch, "agent_abc")
    assert "mac admin agentbus wait agent_abc" in text
    assert "--after-cursor" in text


def test_it_is_absent_without_an_agent_identity(monkeypatch):
    """Without MAC_AGENT_ID the agent cannot address the bus or watch its own
    inbox. An instruction it cannot follow is worse than silence."""
    assert "Coordination:" not in _prompt(monkeypatch, None)
    assert "agentbus" not in _prompt(monkeypatch, None)


def test_the_announcement_is_scoped_to_paths_not_status(monkeypatch):
    """The recorded collision is two agents editing the same checkout, so the
    announcement is the repo and paths — and progress narration is refused."""
    text = _prompt(monkeypatch, "agent_abc")
    assert "paths" in text
    assert "Do not narrate progress" in text


def test_it_tells_the_agent_a_message_may_be_a_correction(monkeypatch):
    """Delivery is pointless if the agent treats an inbound message as trivia."""
    text = _prompt(monkeypatch, "agent_abc")
    assert "correction" in text


def test_it_does_not_disturb_the_rest_of_the_prompt(monkeypatch):
    """The coordination block is additive; the standing contract must survive."""
    text = _prompt(monkeypatch, "agent_abc")
    for expected in (
        "You are running as a MAC fleet worker",
        "Repository runtime contract:",
        "Read the full task from:",
    ):
        assert expected in text, expected


# --- the Hermes runtime context ------------------------------------------


def _runtime_markdown(agent_id: str = "agent_xyz") -> str:
    from mac.hermes_runtime import build_runtime_context, render_runtime_markdown

    context = build_runtime_context(
        agent_name="worker-1",
        fleet_name="test-fleet",
        mac_url="http://127.0.0.1:8789",
        hermes_home=Path("/tmp/hermes-home"),
        mac_home=Path("/tmp/mac-home"),
        agent_id=agent_id,
    )
    return render_runtime_markdown(context)


def test_every_hermes_session_gets_a_coordination_section():
    text = _runtime_markdown()
    assert "## Coordination" in text
    assert "one of several agents" in text


def test_the_runtime_section_names_the_command_with_the_agents_id():
    assert "mac admin agentbus wait agent_xyz" in _runtime_markdown("agent_xyz")


def test_the_runtime_section_also_refuses_progress_narration():
    assert "Do not narrate" in _runtime_markdown()


def test_it_sits_beside_the_existing_sections_without_replacing_them():
    text = _runtime_markdown()
    for heading in ("## Identity", "## Authority", "## Coordination"):
        assert heading in text, heading
    # Identity must still come first: the agent has to know who it is before it
    # is told to speak as itself on a shared bus.
    assert text.index("## Identity") < text.index("## Coordination")


@pytest.mark.parametrize("phrase", ["status update", "narrate"])
def test_both_surfaces_warn_against_noise(monkeypatch, phrase):
    """Said in both places deliberately. A bus that fills with narration is one
    agents learn to ignore, and the messages are durable and audited."""
    combined = _prompt(monkeypatch, "agent_abc") + _runtime_markdown()
    assert phrase in combined
