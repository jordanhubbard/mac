"""The lifecycle verbs humans and agents share.

The console is getting controls -- stand down, resume -- for when agents go off
the rails. The retired dashboard did that with POST /agents/bulk: a privileged
control-plane mutation that bypassed the agents entirely. These are REQUESTS on
a channel every participant can see, which each agent observes and honours.

Two properties are load-bearing and both are tested here:

1. STAND-DOWN IS NOT ABORT. One button labelled "stop" that means abort
   destroys work in flight; one that means pause does not stop a runaway. The
   vocabulary keeps them distinct and marks which one destroys.

2. ASKED IS NOT STOPPED. A bus directive is best-effort. A wedged agent -- one
   not draining its subscription -- never hears "stand down", and that is
   exactly the agent the directive exists for. The ack carries a correlation
   id so a caller can tell who heard it from who did not. Without that there is
   no denominator, and the control reports success while enforcing nothing --
   the failure this repository has now produced four separate times.
"""

from __future__ import annotations

import pytest

from mac.agentbus_control import (
    CONTROL_STREAM_TYPES,
    LIFECYCLE_ABORT,
    LIFECYCLE_ACK_SCHEMA,
    LIFECYCLE_CONTENT_TYPE,
    LIFECYCLE_DESTRUCTIVE_VERBS,
    LIFECYCLE_PAUSE,
    LIFECYCLE_RESUME,
    LIFECYCLE_SCHEMA,
    LIFECYCLE_SCOPES,
    LIFECYCLE_STAND_DOWN,
    LIFECYCLE_STATUS,
    LIFECYCLE_TOPIC,
    LIFECYCLE_VERBS,
    LifecycleVerbError,
    is_control_stream,
    lifecycle_ack_payload,
    lifecycle_payload,
)


# --------------------------------------------------------------------------
# stand-down is not abort
# --------------------------------------------------------------------------


def test_stand_down_and_abort_are_different_verbs():
    assert LIFECYCLE_STAND_DOWN != LIFECYCLE_ABORT
    assert {LIFECYCLE_STAND_DOWN, LIFECYCLE_ABORT} <= set(LIFECYCLE_VERBS)


def test_only_abort_is_marked_destructive():
    """A UI must be able to ask which verbs lose work, rather than knowing.

    If stand_down ever became destructive, or abort stopped being so, a caller
    that renders them behind the same control would silently start destroying
    work in flight.
    """
    assert LIFECYCLE_DESTRUCTIVE_VERBS == {LIFECYCLE_ABORT}
    assert lifecycle_payload(verb=LIFECYCLE_ABORT)["destructive"] is True
    for verb in (LIFECYCLE_STAND_DOWN, LIFECYCLE_PAUSE, LIFECYCLE_RESUME, LIFECYCLE_STATUS):
        assert lifecycle_payload(verb=verb)["destructive"] is False


# --------------------------------------------------------------------------
# a directive nobody understands must fail where it is built
# --------------------------------------------------------------------------


def test_an_unknown_verb_is_refused_not_sent():
    """Travelling the bus and being dropped by every receiver is
    indistinguishable from a fleet that heard it and did nothing."""
    with pytest.raises(LifecycleVerbError) as exc:
        lifecycle_payload(verb="halt")

    assert "halt" in str(exc.value)
    assert LIFECYCLE_STAND_DOWN in str(exc.value), "the error must name the real verbs"


def test_an_unknown_scope_is_refused():
    with pytest.raises(LifecycleVerbError):
        lifecycle_payload(verb=LIFECYCLE_PAUSE, scope="everyone")


@pytest.mark.parametrize("scope", ["project", "agent"])
def test_a_targeted_scope_without_a_target_is_refused(scope):
    """Defaulting a missing target to the whole fleet is the opposite of what
    was asked, and it is the direction that does the damage."""
    with pytest.raises(LifecycleVerbError) as exc:
        lifecycle_payload(verb=LIFECYCLE_STAND_DOWN, scope=scope)

    assert "whole fleet" in str(exc.value)


def test_a_fleet_directive_carries_no_target():
    payload = lifecycle_payload(verb=LIFECYCLE_PAUSE, scope="fleet")

    assert "target" not in payload


def test_a_targeted_directive_carries_its_target():
    payload = lifecycle_payload(
        verb=LIFECYCLE_PAUSE, scope="agent", target="agent_rocky"
    )

    assert payload["target"] == "agent_rocky"
    assert payload["scope"] == "agent"


# --------------------------------------------------------------------------
# asked is not stopped
# --------------------------------------------------------------------------


def test_a_directive_can_carry_a_correlation_id():
    payload = lifecycle_payload(verb=LIFECYCLE_STAND_DOWN, correlation_id="abc123")

    assert payload["correlation_id"] == "abc123"


def test_an_ack_names_the_directive_it_answers():
    """Without this there is no denominator: a caller cannot tell an agent that
    heard and complied from one that never heard at all."""
    ack = lifecycle_ack_payload(
        correlation_id="abc123",
        agent_id="agent_rocky",
        verb=LIFECYCLE_STAND_DOWN,
        honoured=True,
    )

    assert ack["schema"] == LIFECYCLE_ACK_SCHEMA
    assert ack["correlation_id"] == "abc123"
    assert ack["agent_id"] == "agent_rocky"
    assert ack["honoured"] is True


def test_declining_is_a_real_answer_not_a_failure():
    """An agent mid-commit or holding a lease may decline. Saying so is more
    useful than silence, and must be distinguishable from silence."""
    ack = lifecycle_ack_payload(
        correlation_id="abc123",
        agent_id="agent_natasha",
        verb=LIFECYCLE_ABORT,
        honoured=False,
        detail="holding a publication lease",
    )

    assert ack["honoured"] is False
    assert ack["detail"] == "holding a publication lease"


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_the_lifecycle_topic_is_a_control_stream():
    assert is_control_stream(LIFECYCLE_TOPIC, LIFECYCLE_CONTENT_TYPE)
    assert (LIFECYCLE_TOPIC, LIFECYCLE_CONTENT_TYPE) in CONTROL_STREAM_TYPES


def test_the_content_type_survives_a_charset_parameter():
    assert is_control_stream(LIFECYCLE_TOPIC, LIFECYCLE_CONTENT_TYPE + "; charset=utf-8")


def test_the_vocabulary_is_v2_and_says_so():
    """There are no v1 agents and this fleet is the only deployment, so the
    lifecycle namespace starts at v2 rather than carrying two shapes. The
    version is stated so a later reader can see where the break was."""
    assert LIFECYCLE_SCHEMA.endswith(".v2")
    assert LIFECYCLE_TOPIC.endswith(".v2")
    assert LIFECYCLE_ACK_SCHEMA.endswith(".v2")


def test_every_verb_and_scope_builds():
    """No verb in the published tuple may be unbuildable -- a vocabulary that
    advertises a verb it cannot express is worse than a shorter one."""
    for verb in LIFECYCLE_VERBS:
        assert lifecycle_payload(verb=verb)["verb"] == verb
    for scope in LIFECYCLE_SCOPES:
        target = None if scope == "fleet" else "x"
        assert lifecycle_payload(
            verb=LIFECYCLE_STATUS, scope=scope, target=target
        )["scope"] == scope


def test_the_issuer_is_recorded():
    """A human speaks the same verbs on the same bus as an agent, so the
    payload has to say which one spoke."""
    assert lifecycle_payload(verb=LIFECYCLE_PAUSE)["issued_by"] == "human"
    assert (
        lifecycle_payload(verb=LIFECYCLE_PAUSE, issued_by="agent_rocky")["issued_by"]
        == "agent_rocky"
    )
