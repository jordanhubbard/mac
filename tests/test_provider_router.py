"""th-merge-03: provider routing + recovering circuit breaker.

The bug this prevents (observed live): one provider, breaker trips, never
recovers, completions hang. These tests lock in multi-provider failover, the
half-open re-probe recovery, and fail-fast-when-all-down.
"""

from __future__ import annotations

import pytest

from mac.provider_router import (
    AllProvidersDownError,
    BreakerState,
    Provider,
    ProviderRouter,
    providers_from_env,
)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _router(**kw):
    clk = _Clock()
    r = ProviderRouter(
        [
            Provider("primary", "http://p/v1", priority=0),
            Provider("secondary", "http://s/v1", priority=1),
        ],
        failure_threshold=kw.get("failure_threshold", 2),
        cooldown_seconds=kw.get("cooldown_seconds", 30.0),
        half_open_max_probes=kw.get("half_open_max_probes", 1),
        clock=clk,
    )
    return r, clk


def test_select_prefers_lower_priority():
    r, _ = _router()
    assert r.select().name == "primary"


def test_model_filtering_skips_providers_that_dont_serve_it():
    r = ProviderRouter(
        [
            Provider("img", "http://i/v1", priority=0, models=("dall-e",)),
            Provider("chat", "http://c/v1", priority=1, models=("*",)),
        ]
    )
    assert r.select("gpt-5").name == "chat"  # img doesn't serve it
    assert r.select("dall-e").name == "img"  # img wins by priority for its model


def test_breaker_opens_after_threshold_and_fails_over():
    r, _ = _router(failure_threshold=2)
    r.record_failure("primary")
    assert r.select().name == "primary"  # one failure: still closed
    r.record_failure("primary")  # second failure: opens
    assert r.status()["primary"]["state"] == "open"
    assert r.select().name == "secondary"  # failover to the next provider


def test_all_open_fails_fast():
    r, _ = _router(failure_threshold=1)
    r.record_failure("primary")
    r.record_failure("secondary")
    assert r.select() is None  # fail-fast, never hang
    with pytest.raises(AllProvidersDownError):
        r.select_or_raise()


def test_half_open_probe_then_success_recovers():
    # This is the exact thing TokenHub was missing.
    r, clk = _router(failure_threshold=1, cooldown_seconds=30.0)
    r.record_failure("primary")
    assert r.select().name == "secondary"  # primary open
    clk.advance(31.0)  # cooldown elapsed
    chosen = r.select()  # primary half-opens + is offered as the probe
    assert chosen.name == "primary"
    r.record_success("primary")  # probe succeeds -> closed (recovered)
    assert r.status()["primary"]["state"] == "closed"
    assert r.select().name == "primary"


def test_half_open_probe_failure_reopens_and_restarts_cooldown():
    r, clk = _router(failure_threshold=1, cooldown_seconds=30.0)
    r.record_failure("primary")
    clk.advance(31.0)
    assert r.select().name == "primary"  # half-open probe handed out
    r.record_failure("primary")  # probe fails -> reopen
    assert r.status()["primary"]["state"] in {"open", "half_open"}
    # immediately after reopen, before the new cooldown, primary is skipped
    assert r.select().name == "secondary"


def test_half_open_limits_concurrent_probes():
    r, clk = _router(failure_threshold=1, cooldown_seconds=10.0, half_open_max_probes=1)
    r.record_failure("primary")
    clk.advance(11.0)
    first = r.select()  # consumes the single half-open probe
    assert first.name == "primary"
    # a second concurrent select must NOT also probe primary -> falls to secondary
    assert r.select().name == "secondary"


def test_success_resets_failure_count():
    r, _ = _router(failure_threshold=3)
    r.record_failure("primary")
    r.record_failure("primary")
    r.record_success("primary")
    assert r.status()["primary"]["consecutive_failures"] == 0
    r.record_failure("primary")  # count restarted; still closed
    assert r.status()["primary"]["state"] == "closed"


def test_providers_from_env_parses_spec():
    env = {
        "MAC_ROUTER_PROVIDERS": "nvidia=https://inf/v1,0,models=*;openai=https://api/v1,1,models=gpt-5|gpt-4"
    }
    ps = providers_from_env(env)
    assert [p.name for p in ps] == ["nvidia", "openai"]
    assert ps[0].base_url == "https://inf/v1" and ps[0].priority == 0
    assert ps[1].models == ("gpt-5", "gpt-4") and ps[1].priority == 1
    assert providers_from_env({}) == []
