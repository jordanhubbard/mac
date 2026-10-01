"""Tests for agent mood overlays and config flags.

Mood is self-service for the agent: agents pick their own mood based on local
signals. mac records, audits, and exposes — it does not generate prompt
fragments.
"""

import pytest

from mac.models import (
    NotFoundError,
    ValidationError,
)
from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _register_agent(cp, name="rocky", capabilities=None):
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(machine.id, name, capabilities=capabilities or ["ops"])


# ── Mood ─────────────────────────────────────────────────────────────────────


def test_set_mood_records_overlay_and_event(cp):
    agent = _register_agent(cp)
    overlay = cp.set_mood(
        agent.id,
        "warm",
        reason="three reviews approved in a row",
    )
    assert overlay.mode == "warm"
    assert overlay.set_by == agent.id  # defaults to agent_id
    assert overlay.reason == "three reviews approved in a row"
    assert overlay.cleared_at is None

    current = cp.get_current_mood(agent.id)
    assert current is not None
    assert current.id == overlay.id

    events = cp.list_events(subject_type="agent", subject_id=agent.id)
    types = [e["event_type"] for e in events]
    assert "agent.mood_set" in types


def test_set_mood_replaces_prior_active_overlay_atomically(cp):
    agent = _register_agent(cp)
    first = cp.set_mood(agent.id, "warm")
    second = cp.set_mood(agent.id, "irritated", reason="task timed out")

    # First overlay is no longer active.
    cleared_first = cp.get_mood_overlay(first.id)
    assert cleared_first.cleared_at is not None
    assert cleared_first.cleared_reason == "replaced"

    current = cp.get_current_mood(agent.id)
    assert current is not None
    assert current.id == second.id
    assert current.mode == "irritated"

    # History preserves both rows newest-first.
    history = cp.list_mood_history(agent.id)
    assert [h.id for h in history] == [second.id, first.id]


def test_set_mood_rejects_unknown_mode(cp):
    agent = _register_agent(cp)
    with pytest.raises(ValidationError):
        cp.set_mood(agent.id, "ecstatic")


def test_set_mood_with_ttl_expires_from_current_mood(cp):
    agent = _register_agent(cp)
    overlay = cp.set_mood(agent.id, "angry", ttl_seconds=3600)
    assert overlay.expires_at is not None

    # Force-expire by writing a past timestamp directly.
    cp.store.execute(
        "UPDATE mood_overlays SET expires_at = '1970-01-01T00:00:00+00:00' WHERE id = ?",
        (overlay.id,),
    )
    assert cp.get_current_mood(agent.id) is None
    # History still sees it.
    assert overlay.id in {h.id for h in cp.list_mood_history(agent.id)}


def test_set_mood_rejects_non_positive_ttl(cp):
    agent = _register_agent(cp)
    with pytest.raises(ValidationError):
        cp.set_mood(agent.id, "warm", ttl_seconds=0)
    with pytest.raises(ValidationError):
        cp.set_mood(agent.id, "warm", ttl_seconds=-5)


def test_clear_mood_ends_overlay_and_records_event(cp):
    agent = _register_agent(cp)
    overlay = cp.set_mood(agent.id, "sad")
    cleared = cp.clear_mood(agent.id, reason="recovered after rest")
    assert cleared is not None
    assert cleared.id == overlay.id
    assert cleared.cleared_at is not None
    assert cleared.cleared_reason == "recovered after rest"
    assert cp.get_current_mood(agent.id) is None

    events = cp.list_events(subject_type="agent", subject_id=agent.id)
    types = [e["event_type"] for e in events]
    assert "agent.mood_cleared" in types


def test_clear_mood_is_noop_when_nothing_active(cp):
    agent = _register_agent(cp)
    assert cp.clear_mood(agent.id) is None


def test_mood_unknown_agent_rejected(cp):
    with pytest.raises(NotFoundError):
        cp.set_mood("agent_does_not_exist", "warm")
    with pytest.raises(NotFoundError):
        cp.get_current_mood("agent_does_not_exist")


# ── Config flags ─────────────────────────────────────────────────────────────
#
# Allowlisted runtime-settable flags: the conversational "show us your
# reasoning in this channel" path. Resolution is channel row -> agent-global
# row -> registry default; unknown flags and off-registry values are refused.


def test_set_config_flag_records_value_and_audit_event(cp):
    agent = _register_agent(cp)
    result = cp.set_config_flag(
        agent.id,
        "show_reasoning",
        True,
        channel="slack:C123",
        set_by="user:jkh",
        reason="asked in #rockyandfriends",
    )
    assert result["value"] is True
    assert result["channel"] == "slack:C123"
    assert result["source"] == "channel"

    events = cp.list_events(subject_type="agent", subject_id=agent.id)
    types = [e["event_type"] for e in events]
    assert "agent.config_flag_set" in types


def test_config_flag_resolution_channel_beats_agent_beats_default(cp):
    agent = _register_agent(cp)
    # Default when nothing is set.
    default = cp.get_config_flag(agent.id, "show_reasoning", channel="slack:C123")
    assert default["value"] is False
    assert default["source"] == "default"

    cp.set_config_flag(agent.id, "show_reasoning", True)  # agent-global
    inherited = cp.get_config_flag(agent.id, "show_reasoning", channel="slack:C123")
    assert inherited["value"] is True
    assert inherited["source"] == "agent"

    cp.set_config_flag(agent.id, "show_reasoning", False, channel="slack:C123")
    overridden = cp.get_config_flag(agent.id, "show_reasoning", channel="slack:C123")
    assert overridden["value"] is False
    assert overridden["source"] == "channel"

    # A different channel still inherits the agent-global value.
    other = cp.get_config_flag(agent.id, "show_reasoning", channel="slack:C999")
    assert other["value"] is True
    assert other["source"] == "agent"


def test_config_flag_rejects_unknown_flags_and_bad_values(cp):
    agent = _register_agent(cp)
    with pytest.raises(ValidationError):
        cp.set_config_flag(agent.id, "sandbox_policy", "off")
    with pytest.raises(ValidationError):
        cp.set_config_flag(agent.id, "tool_progress", "everything")
    with pytest.raises(ValidationError):
        cp.set_config_flag(agent.id, "show_reasoning", "maybe")
    with pytest.raises(ValidationError):
        cp.get_config_flag(agent.id, "not_a_flag")


def test_config_flag_accepts_conversational_value_forms(cp):
    agent = _register_agent(cp)
    assert cp.set_config_flag(agent.id, "show_reasoning", "on")["value"] is True
    assert cp.set_config_flag(agent.id, "show_reasoning", "false")["value"] is False
    assert cp.set_config_flag(agent.id, "tool_progress", "NEW")["value"] == "new"


def test_list_config_flags_covers_registry_with_descriptions(cp):
    agent = _register_agent(cp)
    cp.set_config_flag(agent.id, "verbose_status_updates", True, channel="slack:C1")
    flags = {f["flag"]: f for f in cp.list_config_flags(agent.id, channel="slack:C1")}
    from mac.config_flags import CONFIG_FLAG_REGISTRY

    assert set(flags) == set(CONFIG_FLAG_REGISTRY)
    assert flags["verbose_status_updates"]["value"] is True
    assert flags["verbose_status_updates"]["source"] == "channel"
    assert flags["show_reasoning"]["source"] == "default"
    assert all(f["description"] for f in flags.values())
    assert flags["tool_progress"]["values"] == ["off", "new", "all", "verbose"]


def test_clear_config_flag_falls_back_and_audits(cp):
    agent = _register_agent(cp)
    cp.set_config_flag(agent.id, "show_reasoning", True, channel="slack:C1")
    assert cp.clear_config_flag(agent.id, "show_reasoning", channel="slack:C1") is True
    assert cp.clear_config_flag(agent.id, "show_reasoning", channel="slack:C1") is False

    resolved = cp.get_config_flag(agent.id, "show_reasoning", channel="slack:C1")
    assert resolved["value"] is False
    assert resolved["source"] == "default"

    events = cp.list_events(subject_type="agent", subject_id=agent.id)
    types = [e["event_type"] for e in events]
    assert "agent.config_flag_cleared" in types
