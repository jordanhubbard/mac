"""Publication must run somewhere that can afford to wait.

The contract gate clones into a sandbox, bootstraps dependencies, and runs a
test suite. That is minutes. There are two places it could run:

  * the hub tick -- a background thread (api.py `_loop`)
  * _maybe_advance_reviews_on_heartbeat -- an agent's HTTP request

The tick passed allow_blocking_hub_verify=False, so it advanced reviews but
never ran the verify, leaving the heartbeat as the only path that actually
published. Measured on the fleet hub 2026-08-15, by the slow-request log added
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

from mac import services


def _tick_source() -> str:
    return inspect.getsource(services.ControlPlane.tick)


def test_the_tick_is_allowed_to_run_the_publication_gate():
    """With this hard-coded False the tick cannot publish, and the only path
    left is an agent heartbeat."""
    source = _tick_source()

    assert "allow_blocking_hub_verify=False" not in source
    assert "MAC_TICK_BLOCKING_HUB_VERIFY" in source, (
        "an operator must be able to restore the non-blocking tick"
    )


def test_the_operator_switch_defaults_to_publishing():
    """A default that cannot publish is how this went unnoticed: reviews kept
    advancing, nothing ever landed, and the state that resulted -- approved and
    unpublished -- looks like work in progress rather than a stall."""
    source = _tick_source()

    assert '_truthy_env("MAC_TICK_BLOCKING_HUB_VERIFY", "1")' in source


def test_the_heartbeat_is_not_the_only_publisher():
    """The pairing that caused the outage: a tick that will not publish and a
    heartbeat that will. If the heartbeat hook is ever disabled -- it has its
    own switch, MAC_REVIEW_TICK_ON_HEARTBEAT -- publication must not stop with
    it."""
    heartbeat = inspect.getsource(
        services.ControlPlane._maybe_advance_reviews_on_heartbeat
    )

    assert "MAC_REVIEW_TICK_ON_HEARTBEAT" in heartbeat
    # The tick must be able to publish independently of that switch.
    assert "MAC_REVIEW_TICK_ON_HEARTBEAT" not in _tick_source()
