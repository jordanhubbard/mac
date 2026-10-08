"""Classification contract for coding-agent sandbox preflight failures.

A failed preflight is classed from its exit status and from structured (JSON)
error objects only. Free-text output is never searched: substring matching
over whole transcripts produced false positives such as a sandbox named
``mac-task-429907755059`` classed ``rate_limited``.
"""

from __future__ import annotations

import importlib

import pytest

executor_sandbox = importlib.import_module("mac.executor_sandbox")
_classify = executor_sandbox._classify_coding_agent_preflight_failure
_binary_status = executor_sandbox._coding_agent_binary_status


@pytest.mark.parametrize(
    ("returncode", "output", "expected"),
    [
        (124, "", "timeout"),
        (137, "", "timeout"),
        (127, "bash: opencode: command not found", "agent_binary_missing"),
        (126, "permission denied", "agent_binary_missing"),
        (
            1,
            'HTTP 403 {"error":"policy_denied","detail":"POST '
            'host.openshell.internal:8789/v1/chat/completions not permitted by policy"}',
            "sandbox_policy_denied",
        ),
        (1, '{"error": {"code": "invalid_api_key", "message": "x"}}', "authentication_failed"),
        (1, '{"error": {"type": "rate_limit_error"}}', "rate_limited"),
        (1, '{"error": "upstream failed", "status": 502}', "provider_server_error"),
        (1, '{"error": {"message": "nope", "status": 401}}', "authentication_failed"),
        (0, "some other text without the sentinel", "sentinel_missing"),
        (1, "totally opaque failure", "probe_failed"),
    ],
)
def test_classifies_on_exit_status_and_structured_errors(returncode, output, expected) -> None:
    assert _classify(returncode, output) == expected


@pytest.mark.parametrize(
    "output",
    [
        # The live false positive: a sandbox name containing "429".
        "Created sandbox mac-task-429907755059",
        "HTTP 429 Too Many Requests",
        "HTTP 401 Unauthorized",
        "502 Bad Gateway",
        "request timed out after 180s",
        "the agent wrote: rate limit exceeded is handled in retry.py",
        "POST host.openshell.internal:8789/v1/responses not permitted by policy",
    ],
)
def test_free_text_is_never_searched(output) -> None:
    assert _classify(1, output) == "probe_failed"


def test_a_json_line_without_an_error_member_is_not_an_error() -> None:
    assert _classify(1, '{"status": 429, "note": "quoted from a test"}') == "probe_failed"


@pytest.mark.parametrize(
    ("verified", "failure_class", "expected"),
    [
        (True, "", "present"),
        (False, "authentication_failed", "present"),
        (False, "sandbox_policy_denied", "present"),
        (False, "rate_limited", "present"),
        (False, "sentinel_missing", "present"),
        (False, "agent_binary_missing", "missing"),
        (False, "timeout", "unverified"),
        (False, "probe_failed", "unverified"),
    ],
)
def test_binary_status_tracks_what_the_sandbox_probe_proved(
    verified, failure_class, expected
) -> None:
    assert _binary_status(verified, failure_class) == expected
