"""When the repository verifier fails, say what happened.

Three canary tasks failed with the identical line:

    OpenShell repository verifier did not start within 120.0s

Every one had run its work successfully first -- the agents did the job, and
the result was discarded at the gate. The message named a timeout and nothing
else, so the previous response was to raise the timeout (45s -> 120s), which
changed nothing, because the verifier was not slow.

Both output streams were captured all along. The failure path returned without
reading either.
"""

from __future__ import annotations

import io

from mac.executor_sandbox import _verifier_output_excerpt


class _Stream(io.StringIO):
    pass


def test_stderr_is_reported():
    excerpt = _verifier_output_excerpt(_Stream(""), _Stream("openshell: no such image"))

    assert "openshell: no such image" in excerpt
    assert excerpt.startswith("stderr=")


def test_stdout_is_reported_when_stderr_is_empty():
    """A launcher that fails politely may say so on stdout."""
    excerpt = _verifier_output_excerpt(_Stream("could not resolve sandbox"), _Stream(""))

    assert "could not resolve sandbox" in excerpt


def test_silence_produces_no_noise():
    """Nothing to add is better than an empty `stderr=` suffix on every line."""
    assert _verifier_output_excerpt(_Stream(""), _Stream("   ")) == ""


def test_a_flood_is_truncated():
    """The message lands in task evidence and a diagnosis event; a megabyte of
    output there helps nobody and costs everybody."""
    excerpt = _verifier_output_excerpt(_Stream(""), _Stream("x" * 10000))

    assert len(excerpt) < 1000
    assert "truncated" in excerpt


def test_newlines_are_flattened():
    """Evidence is read one line at a time in `mac task show`."""
    excerpt = _verifier_output_excerpt(_Stream(""), _Stream("first\nsecond"))

    assert "\n" not in excerpt
    assert "first" in excerpt and "second" in excerpt


def test_a_broken_stream_does_not_raise():
    """Diagnostics run on the failure path; raising there would replace a
    reported failure with an unreported crash."""

    class Broken:
        def seek(self, _pos):
            raise OSError("closed")

        def read(self):
            raise OSError("closed")

    assert _verifier_output_excerpt(Broken(), Broken()) == ""


def test_both_spellings_are_retryable_infrastructure():
    """The failure is the same fault whichever way it is reported. Without
    this, making the message more accurate would reclassify a retryable
    infrastructure failure as a permanent one and burn the task's attempts."""
    from mac.services import _OPENSHELL_VERIFIER_INFRASTRUCTURE_MARKERS

    for message in (
        "OpenShell repository verifier did not start within 120.0s",
        "OpenShell repository verifier exited immediately (rc=1 after 0.3s, "
        "start budget 120.0s): stderr=openshell: no such image",
    ):
        assert any(
            marker in message.lower()
            for marker in _OPENSHELL_VERIFIER_INFRASTRUCTURE_MARKERS
        ), message
