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


def test_retention_runs_before_the_review_sweep(cp, monkeypatch):
    seen = _order_of_calls(cp, monkeypatch)

    cp.tick()

    assert "retention" in seen, "the tick never reached retention at all"
    assert "review_sweep" in seen, (
        "the review sweep did not run; this test would pass vacuously"
    )
    assert seen.index("retention") < seen.index("review_sweep"), (
        "retention ran AFTER the review sweep (%s). The sweep clones a "
        "repository and runs a contract gate inline, so anything ordered after "
        "it can be starved for minutes -- which is exactly what happened on "
        "2026-08-17, when the hub emitted no retention events for 48 minutes "
        "while 235k rows sat past the cutoff." % seen
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

    logs = cp.observability.list_observability(
        name="retention.tick_ran", limit=500
    )
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

    logs = cp.observability.list_observability(
        name="retention.tick_ran", limit=500
    )
    assert len(logs) == 1, (
        "expected the heartbeat to be throttled to one emission per "
        "RETENTION_HEARTBEAT_SECONDS, got %d across three ticks" % len(logs)
    )
