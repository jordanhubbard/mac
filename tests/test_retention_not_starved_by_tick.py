"""Retention must not be starved by slow work later in the same tick.

`ControlPlane.tick()` runs its stages sequentially on one background thread.
`_advance_default_review_sweep_page` sits in that sequence and can block for
minutes: it clones a repository and runs a contract gate inline (the tick's own
comment says "Blocking here delays the next tick"). Retention used to be called
*after* it, so a hub whose review sweep was busy simply never pruned.

Observed live on 2026-08-17: in the 48 minutes after a hub restart the control
plane emitted ZERO retention events of any kind, while 235,615
observability_events and 5,576 action_events sat past the 7-day cutoff. The
retention code itself was correct -- verified against real PostgreSQL -- it was
never reached.

The failure is invisible by construction, which is what makes it expensive.
`RetentionService.prune()` deliberately emits nothing when there is no work, so
the audit row never becomes the retained data. That means "alive, nothing to do"
and "never ran" produce identical silence. This is the same blind spot that let
the store grow to 16GB / 10.4M rows while retention was believed to be wired.

Two guarantees are pinned here:

1. Retention is called BEFORE the review sweep, so slow publication work cannot
   starve it.
2. A `retention.tick_ran` heartbeat is emitted, so dormancy is observable
   without waiting for a deletion to happen.
"""

from __future__ import annotations

import pytest

from mac.services import ControlPlane


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _order_of_calls(cp, monkeypatch):
    """Record the order in which the tick reaches its stages."""
    seen: list[str] = []

    real_retention = cp.retention_prune_tick
    real_sweep = cp._advance_default_review_sweep_page

    def spy_retention(*a, **k):
        seen.append("retention")
        return real_retention(*a, **k)

    def spy_sweep(*a, **k):
        seen.append("review_sweep")
        return real_sweep(*a, **k)

    monkeypatch.setattr(cp, "retention_prune_tick", spy_retention)
    monkeypatch.setattr(cp, "_advance_default_review_sweep_page", spy_sweep)
    return seen


def test_the_tick_no_longer_runs_the_review_sweep_at_all(cp, monkeypatch):
    """Superseded, in the right direction.

    This test used to assert retention ran BEFORE the review sweep inside
    tick(). That ordering was the #392 fix, and it was only half a solution:
    retention still ran once per tick, and the tick's period was the sweep's
    duration, so retention pruned once and then not again for 27 minutes
    (measured live).

    The sweep has since moved to its own worker
    (api._start_publication_worker), so the ordering question is gone: the tick
    does not run it at all. What matters now is that retention still runs on
    the tick path AND that the sweep no longer blocks it.
    """
    seen = _order_of_calls(cp, monkeypatch)
    monkeypatch.delenv("MAC_TICK_RUNS_REVIEW_SWEEP", raising=False)

    cp.tick()

    assert "retention" in seen, "the tick never reached retention at all"
    assert "review_sweep" not in seen, (
        "tick() still runs the review sweep inline (%s); it clones a "
        "repository and runs a contract gate, so every stage after it is "
        "starved -- which is what this whole chain of fixes was about" % seen
    )


def test_a_blocking_review_sweep_cannot_starve_retention(cp, monkeypatch):
    """The real-world shape: the sweep is slow, retention must still have run."""
    seen = _order_of_calls(cp, monkeypatch)

    real_sweep = cp._advance_default_review_sweep_page

    def slow_sweep(*a, **k):
        seen.append("review_sweep")
        # Stand in for "clone a repo and run the contract suite". We do not
        # actually sleep -- ordering is the property under test, and a sleeping
        # test is a slow test that proves the same thing.
        raise RuntimeError("sweep is busy cloning a repository")

    monkeypatch.setattr(cp, "_advance_default_review_sweep_page", slow_sweep)

    try:
        cp.tick()
    except RuntimeError:
        # The tick may or may not swallow the sweep's failure; either way the
        # question is whether retention already ran by that point.
        pass

    assert "retention" in seen, (
        "retention never ran because the review sweep blocked first -- the "
        "starvation this ordering exists to prevent"
    )


def test_the_tick_emits_a_retention_heartbeat(cp):
    """Dormancy must be observable without waiting for a deletion."""
    cp.tick()

    logs = cp.observability.list_observability(name="retention.tick_ran", limit=500)
    assert logs, (
        "no retention.tick_ran heartbeat. Without it, a retention tick that "
        "never runs is indistinguishable from one that ran and found nothing, "
        "because prune() is deliberately silent when idle."
    )

    detail = logs[0].detail or {}
    assert "enabled_classes" in detail, (
        "the heartbeat must report which policies are enabled -- that is the "
        "datum distinguishing 'retention is off' from 'retention is on and the "
        "backlog is empty'"
    )
    assert detail["enabled_classes"], (
        "no retention policy is enabled; the default policies should cover "
        "action_events and observability_events"
    )


def test_the_heartbeat_is_throttled(cp):
    """The heartbeat must not become the firehose it exists to bound."""
    cp.tick()
    cp.tick()
    cp.tick()

    logs = cp.observability.list_observability(name="retention.tick_ran", limit=500)
    assert len(logs) == 1, (
        "expected the heartbeat to be throttled to one emission per "
        "RETENTION_HEARTBEAT_SECONDS, got %d across three ticks" % len(logs)
    )


def test_the_heartbeat_throttle_survives_repeated_ticks(cp):
    """The throttle must not throw after the first emission.

    The first version compared `utcnow()` values, which are ISO *strings*, so
    ``now - last`` raised TypeError on every call after the first. It threw 18
    times on the live hub before it was caught. The prune work runs BEFORE this
    block, so retention kept working -- what broke was the signal that it was
    working, and the symptom is silence, which is indistinguishable from
    retention being dead. A liveness probe that dies quietly is worse than none.
    """
    for _ in range(4):
        cp.retention_prune_tick()  # must not raise

    logs = cp.observability.list_observability(name="retention.tick_ran", limit=500)
    assert len(logs) == 1, (
        "expected exactly one throttled heartbeat across four ticks, got %d" % len(logs)
    )
