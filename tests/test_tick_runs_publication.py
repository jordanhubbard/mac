"""Publication must run somewhere that can afford to wait.

The contract gate clones into a sandbox, bootstraps dependencies, and runs a
test suite. That is minutes. There are two places it could run:

  * the hub tick -- a background thread (api.py `_loop`)
  * _maybe_advance_reviews_on_heartbeat -- an agent's HTTP request

The tick once advanced reviews without running the publication gate, leaving
the heartbeat as the only path that actually published. Measured on the fleet hub 2026-08-15, by the slow-request log added
in the same session:

    slow request: POST /agents/agent_rocky/heartbeat 200 in 249.7s
    slow request: POST /agents/agent_rocky/heartbeat 200 in 315.5s
    slow request: POST /agents/agent_rocky/heartbeat 200 in 276.5s

The agent's own client gives up at 30s and retries, so every attempt started
another overlapping publication that could never converge, and three approved
canaries sat unpublished for hours.

Blocking the tick delays the next tick. Blocking a heartbeat costs a worker --
and when that worker is MAC_REVIEW_TICK_HUB_AGENT, it costs the publication
path itself. The tick is the right place.

This is the narrow version of task_fad95a2b; the full fix is a bounded
publication worker so neither the tick nor a request waits on a sandboxed run.
"""

from __future__ import annotations

import inspect

from mac import api

from mac import services


def _tick_source() -> str:
    return inspect.getsource(services.ControlPlane.tick)


def test_the_tick_can_still_own_the_review_sweep():
    """An operator must be able to restore the inline sweep on the tick."""
    source = _tick_source()

    assert "MAC_TICK_RUNS_REVIEW_SWEEP" in source
    assert "_advance_default_review_sweep_page" in source


def test_publication_happens_by_default_on_the_publication_worker():
    """A default that cannot publish is how this went unnoticed: reviews kept
    advancing, nothing ever landed, and the state that resulted -- approved and
    unpublished -- looks like work in progress rather than a stall.

    The bounded publication worker (api._start_publication_worker) now owns
    the sweep. What must NOT change is that publication happens BY DEFAULT
    somewhere. If every path is off by default, reviews accumulate silently.
    """
    worker = inspect.getsource(api._start_publication_worker)
    assert "_advance_default_review_sweep_page" in worker
    assert '"30" if tick_interval > 0 else "0"' in worker, (
        "the worker must default ON for a hub that runs the dispatch tick. "
        "With the tick and heartbeat paths both off by default, a worker that "
        "is also off by default means NOTHING publishes -- reviews would "
        "accumulate with no error anywhere."
    )


def test_the_heartbeat_is_not_the_only_publisher():
    """The pairing that caused the outage: a tick that will not publish and a
    heartbeat that will. If the heartbeat hook is ever disabled -- it has its
    own switch, MAC_REVIEW_TICK_ON_HEARTBEAT -- publication must not stop with
    it."""
    heartbeat = inspect.getsource(services.ControlPlane._maybe_advance_reviews_on_heartbeat)

    assert "MAC_REVIEW_TICK_ON_HEARTBEAT" in heartbeat
    # The tick must be able to publish independently of that switch.
    assert "MAC_REVIEW_TICK_ON_HEARTBEAT" not in _tick_source()
