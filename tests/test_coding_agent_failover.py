"""One coding agent running dry must not stop the fleet.

Claude Code and Codex authenticate against a subscription. Subscriptions run
out, and providers have outages. Both CLIs are installed in the task image and
either can do the work, so a task should move to the other one rather than
burn its attempts on a provider that has already said no.

Selection-time failover already worked: resolve_coding_agent walks
AGENT_PRIORITY and a route whose in-sandbox preflight fails is skipped. Two
things were missing.

  1. Credit exhaustion was not recognised AT ALL. The classifier knew about
     429s and 5xx but not "credit balance too low" or "insufficient_quota", so
     an exhausted subscription arrived as the opaque probe_failed -- a class
     that reads like a broken route and steers nobody toward the working one.

  2. There was no failover once a route had been chosen. A verified route is
     cached for five minutes, so a subscription that ran dry one minute after
     passing its preflight kept being selected for the next four, with every
     task in that window failing on it.
"""

from __future__ import annotations

import subprocess

import pytest

from mac import coding_agent as ca
from mac import executor_sandbox as es


class TestCreditExhaustionIsItsOwnAnswer:
    """Waiting does not help and the route is not broken. Only "run somewhere
    else" is useful, and a class nobody can act on is how that gets missed."""

    @pytest.mark.parametrize(
        "message",
        [
            "Your credit balance is too low to access the Anthropic API",
            "429 insufficient_quota: You exceeded your current quota",
            "You've hit your usage limit for this month",
            "Error: quota exceeded for this organization",
        ],
    )
    def test_an_exhausted_subscription_is_named(self, message):
        assert es._classify_coding_agent_preflight_failure(1, message) == "credit_exhausted"

    def test_throttling_is_still_throttling(self):
        """A 429 that is rate limiting, not exhaustion, must keep its own class:
        the remedy is backoff on the same route, not moving to another one."""
        assert (
            es._classify_coding_agent_preflight_failure(1, "429 Too Many Requests")
            == "rate_limited"
        )

    def test_the_binary_still_counts_as_present(self):
        """The CLI ran and reached its provider. Reporting the executable as
        missing would send an operator to rebuild an image that is fine."""
        assert es._coding_agent_binary_status(False, "credit_exhausted") == "present"


class TestTheResolverCanBeToldNotThatOne:
    def test_an_excluded_agent_is_not_selected(self, monkeypatch):
        """The caller that watched a route fail is the only one who knows. To
        the resolver the route still looks perfectly available, so without this
        it re-selects it immediately."""
        seen = []

        def _detector(env, home, which):
            seen.append("called")
            return True, "/usr/bin/stub", "stub", "stub: available"

        monkeypatch.setitem(ca._DETECTORS, "claude", _detector)

        choice = ca.resolve_coding_agent(
            env={"PATH": "/usr/bin"},
            which=lambda name: "/usr/bin/" + name,
            exclude=("claude",),
        )

        assert choice.agent != "claude"

    def test_excluding_everything_fails_closed(self):
        """Better no route than a route the caller has just proven cannot work."""
        choice = ca.resolve_coding_agent(
            env={"PATH": "/usr/bin"},
            which=lambda name: "/usr/bin/" + name,
            exclude=ca.AGENT_PRIORITY,
        )

        assert not choice.available


class TestAFailedRouteIsNotReused:
    def test_the_cached_proof_is_dropped(self):
        """A verified route is cached for five minutes. Keeping the proof after
        the provider refuses means every task for the rest of that window picks
        the same dead route."""
        es._SANDBOX_PREFLIGHT_CACHE["route:probe"] = {"verified": True}

        es._forget_coding_agent_route("route:probe")

        assert "route:probe" not in es._SANDBOX_PREFLIGHT_CACHE

    def test_forgetting_an_unknown_route_is_harmless(self):
        """Runs on the failure path; raising there would replace a reported
        provider failure with an unreported crash."""
        es._forget_coding_agent_route("")
        es._forget_coding_agent_route("route:never-seen")


class TestOnlyProviderRefusalsFailOver:
    def _result(self, returncode, text):
        return subprocess.CompletedProcess([], returncode, text, "")

    @pytest.mark.parametrize(
        "text",
        [
            "Your credit balance is too low",
            "503 Service Unavailable",
            "429 rate limit exceeded",
        ],
    )
    def test_a_provider_refusal_asks_for_another_route(self, text):
        assert es._route_failover_class(self._result(1, text))

    def test_a_successful_run_asks_for_nothing(self):
        assert es._route_failover_class(self._result(0, "")) == ""

    def test_the_agents_own_failure_is_not_a_routing_problem(self):
        """A task that failed on its merits must not be silently re-run on a
        second provider: that doubles the cost and hides the real failure
        behind a different agent's output."""
        result = self._result(1, "AssertionError: expected 3, got 4")

        assert es._route_failover_class(result) == ""
