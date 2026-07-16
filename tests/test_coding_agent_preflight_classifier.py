"""Classification contract for coding-agent sandbox preflight failures.

The dream-cycle repair pipeline and the fleet learning store both key off the
``failure_class`` recorded by a failed in-sandbox preflight. A run that collapses
into the generic ``probe_failed`` carries no recovery signal, so these tests pin
the narrower classes that steer an operator (or an automated retry) to the right
repair: transient throttling, a missing CLI, or an unavailable sandbox must not
be reported as a broken endpoint route.
"""
from __future__ import annotations

import importlib

import pytest

executor_sandbox = importlib.import_module("mac.executor_sandbox")
_classify = executor_sandbox._classify_coding_agent_preflight_failure


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (124, "", "timeout"),
        (137, "", "timeout"),
        (1, "request timed out after 180s", "timeout"),
        (1, "HTTP 429 Too Many Requests", "rate_limited"),
        (1, "provider returned rate limit exceeded", "rate_limited"),
        (1, "connection refused", "endpoint_unreachable"),
        (1, "curl: failed to connect to host", "endpoint_unreachable"),
        (1, "HTTP 401 Unauthorized", "authentication_failed"),
        (1, "403 forbidden", "authentication_failed"),
        (1, "HTTP 500 Internal Server Error", "provider_server_error"),
        (1, "502 Bad Gateway", "provider_server_error"),
        (1, "provider returned 503 service unavailable", "provider_server_error"),
        (1, "upstream returned 504", "provider_server_error"),
        (127, "bash: codex: command not found", "agent_binary_missing"),
        (127, "exec: claude: no such file or directory", "agent_binary_missing"),
        (1, "openshell: failed to create sandbox", "sandbox_unavailable"),
        (1, "sandbox create returned non-zero", "sandbox_unavailable"),
        (1, "HTTP 404 model not found", "endpoint_protocol_mismatch"),
        (1, "unsupported protocol", "endpoint_protocol_mismatch"),
        (0, "some other text without the sentinel", "sentinel_missing"),
        (1, "totally opaque failure", "probe_failed"),
    ],
)
def test_classifies_known_preflight_failures(returncode, output, expected) -> None:
    assert _classify(returncode, output) == expected


def test_missing_binary_wins_over_generic_not_found() -> None:
    # "command not found" also contains "not found"; the binary-missing class
    # must take precedence so the operator repairs the image, not the route.
    assert _classify(127, "bash: claude: command not found") == "agent_binary_missing"


def test_rate_limit_is_distinct_from_generic_probe_failure() -> None:
    assert _classify(1, "429 too many requests") != "probe_failed"


def test_empty_output_nonzero_returncode_is_probe_failed() -> None:
    assert _classify(1, "") == "probe_failed"


def test_provider_server_error_is_distinct_from_generic_probe_failure() -> None:
    # A transient upstream 5xx must carry its own recovery signal (retry with
    # backoff) rather than collapsing into the opaque probe_failed catch-all.
    assert _classify(1, "500 internal server error") == "provider_server_error"
    assert _classify(1, "502 bad gateway") != "probe_failed"


def test_server_error_does_not_shadow_specific_classes() -> None:
    # Throttling and auth failures must still win over the 5xx bucket.
    assert _classify(1, "HTTP 429 Too Many Requests") == "rate_limited"
    assert _classify(1, "HTTP 401 Unauthorized") == "authentication_failed"
