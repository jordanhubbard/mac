"""The review sweep runs on its own worker, not on the tick or a heartbeat.

`_advance_default_review_sweep_page` clones a repository and runs a sandboxed
contract gate inline. It used to run in two places, both wrong:

  * `ControlPlane.tick()` -- making the tick's period a git clone plus a test
    run instead of MAC_HUB_TICK_INTERVAL_SECONDS. A manual POST /dispatch/tick
    on the live hub did not return within 120 seconds, and every stage ordered
    after it was starved. Retention is the one that got measured: ZERO events in
    48 minutes (#392), then one burst per tick after being reordered (#393).
    Nothing about retention was special -- each later stage had the same
    problem, silently.

  * `_maybe_advance_reviews_on_heartbeat` -- running it on a WORKER'S HEARTBEAT
    REQUEST THREAD, so the cost landed on the worker: heartbeats of 250-315
    seconds and 33-second lease renewals, observed live.

A single dedicated worker is strictly SAFER than what it replaces. The sweep
already serialises through `reconciliation.claim("default-review-sweep")`;
previously the tick thread and any number of heartbeat threads could all enter
it and contend for that claim. Now one thread drives it.
"""

from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI

from mac.api import _start_publication_worker, _stop_publication_worker
from mac.services import ControlPlane


def _named() -> set[str]:
    return {t.name for t in threading.enumerate()}


@pytest.fixture()
def app_and_cp():
    app = FastAPI()
    cp = ControlPlane.in_memory()
    yield app, cp
    _stop_publication_worker(app)


def test_the_tick_no_longer_runs_the_sweep_inline(monkeypatch):
    """The tick must not block on a repository clone."""
    cp = ControlPlane.in_memory()
    called = []
    monkeypatch.setattr(
        cp,
        "_advance_default_review_sweep_page",
        lambda **kw: called.append(kw) or {},
    )
    monkeypatch.delenv("MAC_TICK_RUNS_REVIEW_SWEEP", raising=False)

    cp.tick()

    assert not called, (
        "tick() still runs the review sweep inline; its period becomes a git "
        "clone plus a contract-gate run, and every stage after it starves"
    )


def test_the_inline_behaviour_is_still_available_as_an_escape_hatch(monkeypatch):
    cp = ControlPlane.in_memory()
    called = []
    monkeypatch.setattr(
        cp,
        "_advance_default_review_sweep_page",
        lambda **kw: called.append(kw) or {},
    )
    monkeypatch.setenv("MAC_TICK_RUNS_REVIEW_SWEEP", "1")

    cp.tick()

    assert called, (
        "MAC_TICK_RUNS_REVIEW_SWEEP=1 must restore the inline sweep for an "
        "operator who wants the tick to own it"
    )


def test_a_heartbeat_does_not_run_the_sweep_by_default(monkeypatch):
    """A worker's request thread must never pay for someone else's test run."""
    cp = ControlPlane.in_memory()
    called = []
    monkeypatch.setattr(
        cp,
        "_advance_default_review_sweep_page",
        lambda **kw: called.append(kw) or {},
    )
    monkeypatch.delenv("MAC_REVIEW_TICK_ON_HEARTBEAT", raising=False)

    cp._maybe_advance_reviews_on_heartbeat(
        type("A", (), {"id": "agent_x", "name": "x"})()
    )

    assert not called, (
        "the heartbeat path still runs the sweep by default; this is what "
        "produced 250-315 second heartbeats and 33-second lease renewals"
    )


def test_the_worker_starts_when_a_hub_tick_is_configured(app_and_cp, monkeypatch):
    """Opt-out, not opt-in: the sweep must still run on a real hub."""
    app, cp = app_and_cp
    monkeypatch.delenv("MAC_PUBLICATION_WORKER_INTERVAL_SECONDS", raising=False)
    monkeypatch.setenv("MAC_HUB_TICK_INTERVAL_SECONDS", "30")

    before = _named()
    _start_publication_worker(app, cp)

    assert "mac-publication" in (_named() - before), (
        "a hub running the dispatch tick got no publication worker; with the "
        "tick and heartbeat paths both off by default, nothing would advance "
        "reviews at all"
    )


def test_it_is_off_without_a_hub_tick(app_and_cp, monkeypatch):
    app, cp = app_and_cp
    monkeypatch.delenv("MAC_PUBLICATION_WORKER_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("MAC_HUB_TICK_INTERVAL_SECONDS", raising=False)

    before = _named()
    _start_publication_worker(app, cp)

    assert "mac-publication" not in (_named() - before), (
        "the worker started with no hub tick configured; every CLI invocation "
        "and test process would spawn a competing sweeper that pushes to main"
    )


def test_the_lifespan_can_stop_it(app_and_cp, monkeypatch):
    app, cp = app_and_cp
    monkeypatch.setenv("MAC_PUBLICATION_WORKER_INTERVAL_SECONDS", "30")
    _start_publication_worker(app, cp)
    thread = app.state.publication_worker_thread

    _stop_publication_worker(app)

    assert not thread.is_alive(), (
        "the publication worker outlived the lifespan. This one pushes to "
        "main, so a sweeper that cannot be joined keeps merging against a "
        "control plane the app has already torn down."
    )
    assert app.state.publication_worker_thread is None


def test_starting_twice_does_not_spawn_a_second_sweeper(app_and_cp, monkeypatch):
    app, cp = app_and_cp
    monkeypatch.setenv("MAC_PUBLICATION_WORKER_INTERVAL_SECONDS", "30")

    _start_publication_worker(app, cp)
    first = app.state.publication_worker_thread
    _start_publication_worker(app, cp)

    assert app.state.publication_worker_thread is first
    assert len([t for t in threading.enumerate() if t.name == "mac-publication"]) == 1
