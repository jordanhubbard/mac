"""A slow request should name itself, and a normal one should cost nothing.

Point-in-time probing kept lying about why the hub was slow. `mac task ready`
timing out was investigated three separate times and blamed on, in order: the
allocator, request-threadpool exhaustion, and connection-pool exhaustion. Each
was measured while the hub happened to be idle and each measured clean --
16 idle threadpool workers, 12 of 100 connections, 1.7% CPU. The pool DID
exhaust later ("couldn't get a connection after 30.00 sec"), under a load that
was gone by the time anyone went looking.

A request that reports its own duration when it exceeds a threshold removes the
need to be watching at the right moment.

The counterweight is the reason this is threshold-triggered and not
per-request: the hub used to write a uvicorn access line for EVERY request,
synchronously, on the event loop. That log reached 626MB / 5.4M lines, and a
thread dump caught the loop inside logging flush() rather than serving --
which is what got the hub restarted mid-publication. Observability that runs on
every request is how the last outage happened, so the normal path must stay
silent.
"""

from __future__ import annotations

import inspect

from mac import api


def _middleware_module_source() -> str:
    return inspect.getsource(api)


def test_the_normal_path_writes_nothing():
    """A per-request log line on the event loop is what took the hub down."""
    source = _middleware_module_source()

    assert "def _log_slow_request" in source
    # The early return must come before any logging call.
    helper = source[source.index("def _log_slow_request") :]
    helper = helper[: helper.index("\n    @app.middleware")]

    guard = helper.index("if elapsed < threshold:")
    emit = helper.index("_log.warning")
    assert guard < emit, "fast requests must return before anything is logged"


def test_the_threshold_is_tunable_and_can_be_disabled():
    source = _middleware_module_source()
    helper = source[source.index("def _log_slow_request") :]
    helper = helper[: helper.index("\n    @app.middleware")]

    assert "MAC_SLOW_REQUEST_SECONDS" in helper
    assert "if threshold <= 0:" in helper, "operators must be able to turn it off"


def test_a_bad_threshold_does_not_break_requests():
    """A typo in an env var must not take the hub's request path with it."""
    source = _middleware_module_source()
    helper = source[source.index("def _log_slow_request") :]
    helper = helper[: helper.index("\n    @app.middleware")]

    assert "except ValueError" in helper


def test_the_line_says_which_request_and_how_long():
    """"something was slow" is what the previous three investigations already
    knew. The route and the duration are the parts that were missing."""
    source = _middleware_module_source()
    helper = source[source.index("def _log_slow_request") :]
    helper = helper[: helper.index("\n    @app.middleware")]

    assert "request.method" in helper
    assert "request.url.path" in helper
    assert "elapsed" in helper


def test_the_client_timeout_is_above_a_slow_hub_not_at_it():
    """10s made a loaded hub indistinguishable from a dead one: rocky failed to
    register while the hub was answering /tasks in 12s, so the fleet lost the
    agent that owns the review tick."""
    from mac.api_client import MacApiClient

    assert MacApiClient.DEFAULT_TIMEOUT_SECONDS >= 30.0


def test_the_client_timeout_is_overridable(monkeypatch):
    from mac.api_client import MacApiClient

    monkeypatch.setenv("MAC_API_TIMEOUT_SECONDS", "45")
    assert MacApiClient("http://example.invalid").timeout == 45.0

    monkeypatch.setenv("MAC_API_TIMEOUT_SECONDS", "not-a-number")
    assert (
        MacApiClient("http://example.invalid").timeout
        == MacApiClient.DEFAULT_TIMEOUT_SECONDS
    ), "a malformed override must fall back, not crash the client"


def test_an_explicit_timeout_still_wins():
    """Callers with a genuine reason to be impatient keep control."""
    from mac.api_client import MacApiClient

    assert MacApiClient("http://example.invalid", timeout=5).timeout == 5
