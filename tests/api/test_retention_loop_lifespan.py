"""Retention runs on its own timer, not behind the dispatch tick.

#392 moved retention ahead of the default-review sweep inside
``ControlPlane.tick()``, which fixed the case where it never ran at all. But it
still ran only ONCE PER TICK, and the tick's real period is the sweep's
duration -- the sweep clones a repository and runs a contract gate inline, so a
tick can take tens of minutes.

Measured on the hub 2026-08-17 after #392 shipped:

    t+0    obs_backlog=238,918  act_backlog=902   prunes=7
    t+3min obs_backlog=238,937  act_backlog=909   prunes=7
    t+6min obs_backlog=239,063  act_backlog=975   prunes=7
    t+9min obs_backlog=239,188  act_backlog=1045  prunes=7

One prune burst at startup, then nothing for nine minutes while both backlogs
grew. Alive, and still losing ground.

Retention is bounded and touches only the two disposable telemetry classes, so
it needs none of the tick's ordering guarantees. These tests pin that it has its
own thread, that the thread is gated, and that the lifespan can stop it -- an
unstoppable pruning daemon is its own hazard.
"""

from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI

from mac.api import _start_retention_loop, _stop_retention_loop
from mac.services import ControlPlane


def _named_threads() -> set[str]:
    return {t.name for t in threading.enumerate()}


@pytest.fixture()
def app_and_cp():
    app = FastAPI()
    cp = ControlPlane.in_memory()
    yield app, cp
    _stop_retention_loop(app)


def test_the_retention_thread_starts_when_enabled(app_and_cp, monkeypatch):
    app, cp = app_and_cp
    monkeypatch.setenv("MAC_RETENTION_INTERVAL_SECONDS", "30")

    before = _named_threads()
    _start_retention_loop(app, cp)
    started = _named_threads() - before

    assert "mac-retention" in started, (
        "no dedicated retention thread; retention would still be queued behind "
        "the dispatch tick, which is as slow as the review sweep"
    )
    assert app.state.retention_thread.is_alive()


def test_it_is_off_by_default_without_a_hub_tick(app_and_cp, monkeypatch):
    """The CLI, the test suite and stateless replicas must not each prune."""
    app, cp = app_and_cp
    monkeypatch.delenv("MAC_RETENTION_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("MAC_HUB_TICK_INTERVAL_SECONDS", raising=False)

    before = _named_threads()
    _start_retention_loop(app, cp)

    assert "mac-retention" not in (_named_threads() - before), (
        "retention started with no hub tick configured; every CLI invocation "
        "and test process would spawn a competing pruner"
    )


def test_a_hub_that_ticks_gets_retention_by_default(app_and_cp, monkeypatch):
    """On a real hub the default must be ON -- opt-out, not opt-in.

    The failure this whole change addresses was retention silently not running.
    Requiring an operator to set one more variable to get it back would
    reproduce exactly that.
    """
    app, cp = app_and_cp
    monkeypatch.delenv("MAC_RETENTION_INTERVAL_SECONDS", raising=False)
    monkeypatch.setenv("MAC_HUB_TICK_INTERVAL_SECONDS", "30")

    before = _named_threads()
    _start_retention_loop(app, cp)

    assert "mac-retention" in (_named_threads() - before), (
        "a hub running the dispatch tick did not get a retention timer by "
        "default; retention must be opt-out on a hub, never opt-in"
    )


def test_the_lifespan_can_stop_it(app_and_cp, monkeypatch):
    app, cp = app_and_cp
    monkeypatch.setenv("MAC_RETENTION_INTERVAL_SECONDS", "30")
    _start_retention_loop(app, cp)
    thread = app.state.retention_thread

    _stop_retention_loop(app)

    assert not thread.is_alive(), (
        "the retention thread outlived the lifespan; a pruning daemon that "
        "cannot be joined keeps deleting against a control plane the app has "
        "already torn down"
    )
    assert app.state.retention_thread is None


def test_starting_twice_does_not_spawn_a_second_pruner(app_and_cp, monkeypatch):
    app, cp = app_and_cp
    monkeypatch.setenv("MAC_RETENTION_INTERVAL_SECONDS", "30")

    _start_retention_loop(app, cp)
    first = app.state.retention_thread
    _start_retention_loop(app, cp)

    assert app.state.retention_thread is first, (
        "a second retention thread was started; two pruners racing on the same "
        "tables is how a bounded batch stops being bounded"
    )
    assert len([t for t in threading.enumerate() if t.name == "mac-retention"]) == 1
