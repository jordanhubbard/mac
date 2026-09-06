"""A fenced-write conflict on POST .../start is transient by construction
(the server detected a concurrent, valid write racing this one and refused
rather than corrupt state) -- the worker must retry it a bounded number of
times instead of treating it as a fatal worker_exception.

Confirmed live: an unrelated concurrent operator call (`mac task update
--dependencies`) racing an agent's claim+start on the same task produced
`mac API POST /tasks/<id>/start?... failed:
{"detail":"task state changed during fenced write; retry"}` -- HTTP 400 --
and the worker transitioned the task straight to permanently `blocked`
rather than retrying what the server's own error message says is
retryable.
"""

from __future__ import annotations

import pytest

from mac.api_client import MacApiError
from mac.worker import (
    FENCED_WRITE_RETRY_ATTEMPTS,
    _is_fenced_write_conflict,
    _post_with_fenced_write_retry,
)


def _fenced_write_error() -> MacApiError:
    detail = '{"detail":"task state changed during fenced write; retry"}'
    return MacApiError(
        "mac API POST /tasks/task_x/start?... failed: %s" % detail,
        status_code=400,
        detail=detail,
    )


def _ordinary_400() -> MacApiError:
    detail = '{"detail":"lease_id does not match the active lease"}'
    return MacApiError(
        "mac API POST /tasks/task_x/start?... failed: %s" % detail,
        status_code=400,
        detail=detail,
    )


def test_is_fenced_write_conflict_matches_only_the_specific_400():
    assert _is_fenced_write_conflict(_fenced_write_error()) is True
    assert _is_fenced_write_conflict(_ordinary_400()) is False


def test_retries_and_succeeds_on_a_transient_fenced_write_conflict():
    calls = {"count": 0}

    class _Client:
        def post(self, path, payload):
            calls["count"] += 1
            if calls["count"] < 2:
                raise _fenced_write_error()
            return {"ok": True, "path": path}

    result = _post_with_fenced_write_retry(
        _Client(), "/tasks/task_x/start", {}, sleep=lambda _seconds: None
    )

    assert result == {"ok": True, "path": "/tasks/task_x/start"}
    assert calls["count"] == 2


def test_gives_up_after_the_bounded_attempt_count():
    calls = {"count": 0}

    class _Client:
        def post(self, path, payload):
            calls["count"] += 1
            raise _fenced_write_error()

    with pytest.raises(MacApiError):
        _post_with_fenced_write_retry(_Client(), "/tasks/task_x/start", {}, sleep=lambda _s: None)

    assert calls["count"] == FENCED_WRITE_RETRY_ATTEMPTS


def test_an_ordinary_400_is_not_retried():
    calls = {"count": 0}

    class _Client:
        def post(self, path, payload):
            calls["count"] += 1
            raise _ordinary_400()

    with pytest.raises(MacApiError):
        _post_with_fenced_write_retry(_Client(), "/tasks/task_x/start", {}, sleep=lambda _s: None)

    assert calls["count"] == 1
