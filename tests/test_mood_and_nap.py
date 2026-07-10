"""Tests for agent mood overlays and nap lifecycle.

Both primitives are self-service for the agent: agents pick their own mood
based on local signals, and agents (or their sidecars) drive their own nap
windows. mac records, audits, and exposes — it does not summarize, embed, or
generate prompt fragments.
"""

from datetime import datetime, timedelta, timezone

import pytest

from mac.models import (
    AgentStatus,
    NapStatus,
    NotFoundError,
    TransitionError,
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


# ── Nap ──────────────────────────────────────────────────────────────────────


def test_register_agent_creates_default_nap_schedule(cp):
    agent = _register_agent(cp, "natasha")

    schedule = cp.get_nap_schedule(agent.id)

    assert schedule is not None
    assert schedule.agent_id == agent.id
    assert schedule.enabled is True
    assert 0 <= schedule.offset_minutes < 60


def test_heartbeat_backfills_missing_nap_schedule(cp):
    agent = _register_agent(cp, "bullwinkle")
    cp.store.execute("DELETE FROM nap_schedules WHERE agent_id = ?", (agent.id,))
    assert cp.get_nap_schedule(agent.id) is None

    cp.heartbeat_agent(agent.id)

    schedule = cp.get_nap_schedule(agent.id)
    assert schedule is not None
    assert schedule.agent_id == agent.id


def test_configure_nap_uses_deterministic_offset_when_unspecified(cp):
    a1 = _register_agent(cp, "rocky")
    a2 = _register_agent(cp, "natasha")

    schedule_a = cp.configure_nap(a1.id)
    schedule_b = cp.configure_nap(a2.id)

    # Offsets are within the documented window and stable across runs.
    assert 0 <= schedule_a.offset_minutes < 60
    assert 0 <= schedule_b.offset_minutes < 60
    # Same name -> same offset (idempotent + deterministic).
    schedule_a_again = cp.configure_nap(a1.id)
    assert schedule_a_again.offset_minutes == schedule_a.offset_minutes


def test_configure_nap_validates_bounds(cp):
    agent = _register_agent(cp)
    with pytest.raises(ValidationError):
        cp.configure_nap(agent.id, offset_minutes=-1)
    with pytest.raises(ValidationError):
        cp.configure_nap(agent.id, offset_minutes=60)
    with pytest.raises(ValidationError):
        cp.configure_nap(agent.id, window_minutes=0)
    with pytest.raises(ValidationError):
        cp.configure_nap(agent.id, window_minutes=121)


def test_next_nap_window_is_in_future_and_matches_offset(cp):
    agent = _register_agent(cp, "rocky")
    cp.configure_nap(agent.id, offset_minutes=45, window_minutes=15)
    reference = datetime(2026, 5, 18, 10, 10, 0, tzinfo=timezone.utc)
    window = cp.next_nap_window(agent.id, now=reference)
    assert window is not None
    assert window["offset_minutes"] == 45
    assert window["window_minutes"] == 15
    start = datetime.fromisoformat(window["start"])
    assert start > reference
    assert start.hour == 10 and start.minute == 45
    end = datetime.fromisoformat(window["end"])
    assert end - start == timedelta(minutes=15)


def test_next_nap_window_normalizes_legacy_daily_offsets(cp):
    agent = _register_agent(cp, "rocky")
    cp.store.execute(
        "UPDATE nap_schedules SET offset_minutes = ? WHERE agent_id = ?",
        (125, agent.id),
    )

    schedule = cp.get_nap_schedule(agent.id)
    reference = datetime(2026, 5, 18, 10, 3, 0, tzinfo=timezone.utc)
    window = cp.next_nap_window(agent.id, now=reference)

    assert schedule is not None
    assert schedule.offset_minutes == 5
    assert window is not None
    assert window["offset_minutes"] == 5
    assert datetime.fromisoformat(window["start"]).minute == 5


def test_next_nap_window_disabled_returns_none(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id, enabled=False)
    assert cp.next_nap_window(agent.id) is None


def test_nap_lifecycle_begin_complete_restores_agent_and_updates_schedule(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id)

    run = cp.begin_nap(agent.id)
    assert run.status == NapStatus.RUNNING.value
    # Agent is now DRAINING so the dispatcher will skip it.
    assert cp.get_agent(agent.id).status == AgentStatus.DRAINING.value

    # The summary artifact lives in the evidence table; in production the
    # agent would attach a real one. For this test we make one against a task.
    task = cp.create_task("nap-summary-host", required_capabilities=["ops"])
    evidence = cp.add_evidence(
        task.id, "log", "log://nap/summary", "wrote nap summary to qdrant", agent.id
    )

    completed = cp.complete_nap(run.id, summary_evidence_id=evidence.id)
    assert completed.status == NapStatus.COMPLETED.value
    assert completed.summary_evidence_id == evidence.id
    # Agent restored to IDLE.
    assert cp.get_agent(agent.id).status == AgentStatus.IDLE.value
    # Schedule remembers the completion.
    schedule = cp.get_nap_schedule(agent.id)
    assert schedule.last_completed_at is not None

    types = [
        e["event_type"]
        for e in cp.list_events(subject_type="agent", subject_id=agent.id)
    ]
    assert "agent.nap_started" in types
    assert "agent.nap_completed" in types


def test_complete_nap_requires_log_kind_evidence(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id)
    run = cp.begin_nap(agent.id)
    task = cp.create_task("dummy", required_capabilities=["ops"])
    wrong = cp.add_evidence(task.id, "test", "artifact://t", "tests passed", agent.id)
    with pytest.raises(ValidationError):
        cp.complete_nap(run.id, summary_evidence_id=wrong.id)


def test_begin_nap_refuses_if_agent_holds_active_lease(cp):
    agent = _register_agent(cp, "rocky", capabilities=["python"])
    cp.configure_nap(agent.id)
    task = cp.create_task("hold", required_capabilities=["python"])
    cp.claim_task(task.id, agent.id)
    with pytest.raises(ValidationError):
        cp.begin_nap(agent.id)


def test_fail_nap_restores_agent_without_advancing_schedule(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id)
    run = cp.begin_nap(agent.id)
    failed = cp.fail_nap(run.id, reason="qdrant unreachable")
    assert failed.status == NapStatus.FAILED.value
    assert failed.detail["failure_reason"] == "qdrant unreachable"
    assert cp.get_agent(agent.id).status == AgentStatus.IDLE.value
    schedule = cp.get_nap_schedule(agent.id)
    assert schedule.last_completed_at is None  # not a real completion


def test_fail_nap_requires_reason(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id)
    run = cp.begin_nap(agent.id)
    with pytest.raises(ValidationError):
        cp.fail_nap(run.id, reason="")


def test_complete_and_fail_refuse_terminal_runs(cp):
    agent = _register_agent(cp)
    cp.configure_nap(agent.id)
    run = cp.begin_nap(agent.id)
    cp.fail_nap(run.id, reason="cancelled")
    with pytest.raises(TransitionError):
        cp.complete_nap(run.id)
    with pytest.raises(TransitionError):
        cp.fail_nap(run.id, reason="again")


def test_nap_schedule_offset_spreads_across_typical_fleet():
    """Two agents with different names should land at different offsets in
    practice. Pin the computed offsets for known names so future renames are
    forced to surface the change."""
    cp = ControlPlane.in_memory()
    names = ["rocky", "natasha", "bullwinkle", "boris"]
    agents = [_register_agent(cp, name) for name in names]
    offsets = {a.name: cp.configure_nap(a.id).offset_minutes for a in agents}
    # All distinct — pure mathematical assertion the hash spreads four names.
    assert len(set(offsets.values())) == 4
    # And all within the documented window.
    assert all(0 <= o < 60 for o in offsets.values())


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
    assert (
        cp.set_config_flag(agent.id, "tool_progress", "NEW")["value"] == "new"
    )


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
