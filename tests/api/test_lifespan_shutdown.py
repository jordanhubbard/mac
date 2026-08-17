"""Lifespan start/stop is a closed loop: nothing survives the context manager.

Two defects motivated these tests (audit 2026-08-17):

* ``ControlPlane.enable_event_driven_review_advance`` started an unstoppable
  ``while True`` daemon whose Thread object was never retained. Its work clones
  a repo, runs the merge gate, and ``git push``es to main before the publication
  row commits, so a hub torn down with a nudge in flight can land a push whose
  bookkeeping never lands.
* Every ``start()`` in the lifespan ran OUTSIDE the ``try``, so one raising
  service skipped the ``finally`` entirely and left already-started daemon
  tickers running against a control plane the app had abandoned.

Every assertion here is scoped to the threads the individual test started.
pytest-xdist gives each worker ONE process shared by many test modules, so
"no thread named X exists anywhere" is not a claim a test in this suite can
make -- another module's leak would fail it for reasons that have nothing to
do with the lifespan. The contract is "the threads THIS lifespan started are
gone when it exits", and that is what these check.
"""

from __future__ import annotations

import threading
import time
from typing import Set

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _threads_named(name: str) -> Set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == name and t.is_alive()}


def _wait_all_stopped(
    threads: Set[threading.Thread], timeout: float = 10.0
) -> Set[threading.Thread]:
    """Return whichever of ``threads`` are still alive after ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = {t for t in threads if t.is_alive()}
        if not alive:
            return set()
        time.sleep(0.05)
    return {t for t in threads if t.is_alive()}


def test_review_advance_consumer_does_not_outlive_the_lifespan(monkeypatch):
    monkeypatch.setenv("MAC_HUB_TICK_INTERVAL_SECONDS", "999")
    app = create_app(control_plane=ControlPlane.in_memory())
    before = _threads_named("mac-review-advance")

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        started = _threads_named("mac-review-advance") - before
        assert started, (
            "the hub tick gate should have enabled the review-advance consumer"
        )

    assert _wait_all_stopped(started) == set(), (
        "the review-advance consumer is still alive after the lifespan exited; "
        "it pushes to main, so it must not outlive the app"
    )


def test_review_advance_disable_is_idempotent_and_stops_nudges():
    cp = ControlPlane.in_memory()
    before = _threads_named("mac-review-advance")
    cp.enable_event_driven_review_advance()
    started = _threads_named("mac-review-advance") - before
    assert started

    cp.disable_event_driven_review_advance()
    assert _wait_all_stopped(started) == set()
    # A nudge after shutdown must not start a fresh clone/push.
    cp._nudge_review_workflow("task_never_advanced")
    assert cp._advance_queue is None
    assert _threads_named("mac-review-advance") - before == set()
    # Idempotent: a second stop is a no-op, not an error.
    cp.disable_event_driven_review_advance()


def test_failed_service_start_unwinds_the_services_already_started(monkeypatch):
    monkeypatch.setenv("MAC_HUB_TICK_INTERVAL_SECONDS", "999")
    app = create_app(control_plane=ControlPlane.in_memory())
    before_tick = _threads_named("mac-hub-tick")
    before_advance = _threads_named("mac-review-advance")

    # The autoscaler starts late in the sequence, so a failure there proves the
    # earlier services (hub tick + review advance among them) are unwound.
    autoscaler = app.state.hgx_autoscaler

    def _explode() -> None:
        raise RuntimeError("simulated service start failure")

    monkeypatch.setattr(autoscaler, "start", _explode)

    with pytest.raises(RuntimeError, match="simulated service start failure"):
        with TestClient(app):
            pass  # pragma: no cover - startup must fail before the body runs

    assert _wait_all_stopped(_threads_named("mac-hub-tick") - before_tick) == set(), (
        "hub ticker survived a failed lifespan startup"
    )
    assert _wait_all_stopped(
        _threads_named("mac-review-advance") - before_advance
    ) == set(), "review-advance consumer survived a failed lifespan startup"
    assert getattr(app.state, "hub_tick_thread", None) is None
