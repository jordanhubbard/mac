"""A failing loop must back off and escalate once, not narrate forever.

Observed in a contract gate run: review_tick_loop_failures reached 172, one
stderr line per iteration, no backoff and no ceiling. Cosmetic that day. But it
is the same shape as the hub self-tick crash loop that made the entire backlog
undispatchable -- a loop that has failed 172 times is not going to succeed on
173, and nothing was going to tell a human otherwise.
"""

from __future__ import annotations

import logging

import pytest

from mac.k8s.orchestrator import (
    LOOP_BACKOFF_CEILING_SECONDS,
    LOOP_ESCALATE_AFTER,
    _LoopBackoff,
)


@pytest.fixture()
def log(caplog):
    caplog.set_level(logging.DEBUG)
    return logging.getLogger("test-loop")


def test_the_first_failure_waits_the_normal_interval(log):
    backoff = _LoopBackoff(10.0, "review-tick", log)

    assert backoff.failure(1) == 10.0


def test_delay_grows_with_consecutive_failures(log):
    """Polling a broken endpoint every interval forever helps nobody."""
    backoff = _LoopBackoff(10.0, "review-tick", log)

    delays = [backoff.failure(n) for n in range(1, 5)]

    assert delays == [10.0, 20.0, 40.0, 80.0]


def test_the_delay_is_capped(log):
    """It must keep TRYING -- the cause may be a hub restart that resolves --
    just not faster than the ceiling."""
    backoff = _LoopBackoff(10.0, "review-tick", log)

    for n in range(1, 40):
        delay = backoff.failure(n)

    assert delay == LOOP_BACKOFF_CEILING_SECONDS


def test_success_resets_the_delay(log):
    """A loop that fails, recovers, and fails again is healthy-ish; it should
    not inherit the previous episode's penalty."""
    backoff = _LoopBackoff(10.0, "review-tick", log)
    for n in range(1, 5):
        backoff.failure(n)

    backoff.success()

    assert backoff.failure(5) == 10.0


def test_it_escalates_exactly_once_per_episode(log, caplog):
    """The 172-line stderr wall is what this replaces."""
    backoff = _LoopBackoff(1.0, "review-tick", log)

    for n in range(1, 60):
        backoff.failure(n)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, "escalation should be one signal, not one per iteration"


def test_the_escalation_says_it_is_not_recovering(log, caplog):
    backoff = _LoopBackoff(1.0, "review-tick", log)

    for n in range(1, LOOP_ESCALATE_AFTER + 2):
        backoff.failure(n)

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert "not recovering" in errors[0]


def test_warnings_stop_once_escalated(log, caplog):
    """Otherwise escalation adds a line rather than replacing the stream."""
    backoff = _LoopBackoff(1.0, "review-tick", log)
    for n in range(1, LOOP_ESCALATE_AFTER + 1):
        backoff.failure(n)
    caplog.clear()

    for n in range(LOOP_ESCALATE_AFTER + 1, LOOP_ESCALATE_AFTER + 20):
        backoff.failure(n)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []


def test_recovery_after_an_escalation_is_announced(log, caplog):
    """An operator paged by the escalation otherwise has no way to learn it
    cleared except by going to look."""
    backoff = _LoopBackoff(1.0, "review-tick", log)
    for n in range(1, LOOP_ESCALATE_AFTER + 2):
        backoff.failure(n)
    caplog.clear()

    backoff.success()

    assert any("recovered" in r.getMessage() for r in caplog.records)


def test_a_quiet_recovery_is_not_announced(log, caplog):
    """A single blip that resolves must not page anyone."""
    backoff = _LoopBackoff(1.0, "review-tick", log)
    backoff.failure(1)
    caplog.clear()

    backoff.success()

    assert not any("recovered" in r.getMessage() for r in caplog.records)


def test_the_loop_itself_uses_the_backoff(monkeypatch):
    """The wiring, not the helper. A backoff nothing calls is decoration.

    The loop must sleep for the BACKED-OFF delay, not the fixed interval.
    """
    from mac.k8s import orchestrator

    slept: list = []

    class _AlwaysFails:
        def post(self, *args, **kwargs):
            raise RuntimeError("hub down")

    def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 4:
            raise KeyboardInterrupt  # break the infinite loop

    with pytest.raises(KeyboardInterrupt):
        orchestrator._run_review_tick_loop_forever(
            _AlwaysFails(), 5.0, 1, "tester", logging.getLogger("wire"), fake_sleep
        )

    assert slept == [5.0, 10.0, 20.0, 40.0], slept
