"""`GET /events/stream` — follow the event stream instead of re-asking for it.

`GET /events` answers "what happened up to now", so following it costs a
client round-trip per interval, and the interval is a straight latency/load
trade the client cannot win. This moves that loop to the hub, where it is a
local query.

The failure that matters is not "it returns the wrong events" -- it is that a
follow endpoint can appear to work while delivering nothing, or never close.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _plane_and_client():
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    return cp, TestClient(create_app(control_plane=cp))


def _records(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_the_stream_delivers_events_that_already_happened(client_setup=None):
    """A follower that only sees events created after it connects would miss
    everything between the caller's listing and its subscribe."""
    cp, client = _plane_and_client()
    cp.create_task("something", project="mac")

    response = client.get(
        "/events/stream",
        params={"subject_type": "task", "timeout_seconds": 1, "poll_interval_seconds": 0.1},
    )

    assert response.status_code == 200
    assert _records(response), "the stream produced nothing at all"


def test_the_stream_closes_on_its_own(client_setup=None):
    """Bounded by the timeout so one client cannot hold a worker forever, and
    so a connection wedged behind a proxy is eventually reaped."""
    _cp, client = _plane_and_client()

    response = client.get(
        "/events/stream",
        params={"timeout_seconds": 1, "poll_interval_seconds": 0.1},
    )

    assert response.status_code == 200


def test_it_is_newline_delimited_json_not_an_array():
    """An array is not readable until the stream ends, which for a feed is
    never -- that is the whole reason this endpoint exists."""
    cp, client = _plane_and_client()
    cp.create_task("something", project="mac")

    response = client.get(
        "/events/stream",
        params={"timeout_seconds": 1, "poll_interval_seconds": 0.1},
    )

    assert "x-ndjson" in response.headers["content-type"]
    for line in response.text.splitlines():
        if line.strip():
            json.loads(line)


def test_records_arrive_oldest_first():
    """list_events is newest-first; a follower wants the order things
    happened, or a client tracking a cursor walks backwards."""
    cp, client = _plane_and_client()
    cp.create_task("first", project="mac")
    cp.create_task("second", project="mac")

    response = client.get(
        "/events/stream",
        params={"subject_type": "task", "timeout_seconds": 1, "poll_interval_seconds": 0.1},
    )

    stamps = [record["created_at"] for record in _records(response)]
    assert stamps == sorted(stamps)


def test_the_same_event_is_not_delivered_twice_within_one_connection():
    """The cursor is a timestamp, so the boundary instant is re-queried every
    tick. Without de-duplication a quiet stream repeats its last record
    forever, and a client counting events sees work that never happened."""
    cp, client = _plane_and_client()
    cp.create_task("only one", project="mac")

    response = client.get(
        "/events/stream",
        params={"subject_type": "task", "timeout_seconds": 2, "poll_interval_seconds": 0.1},
    )

    ids = [record["id"] for record in _records(response)]
    assert len(ids) == len(set(ids))


def test_a_since_cursor_excludes_what_came_before():
    cp, client = _plane_and_client()
    cp.create_task("early", project="mac")
    events = cp.list_events(subject_type="task", limit=10)
    cutoff = events[0]["created_at"]

    response = client.get(
        "/events/stream",
        params={
            "subject_type": "task",
            "since": cutoff,
            "timeout_seconds": 1,
            "poll_interval_seconds": 0.1,
        },
    )

    assert all(record["created_at"] >= cutoff for record in _records(response))


def test_the_event_type_filter_is_honoured():
    """The stream carries lease renewals and much else; a waiter asking for
    transitions must not have to filter the firehose itself."""
    cp, client = _plane_and_client()
    cp.create_task("something", project="mac")

    response = client.get(
        "/events/stream",
        params={
            "event_type": "task.created",
            "timeout_seconds": 1,
            "poll_interval_seconds": 0.1,
        },
    )

    assert all(record["event_type"] == "task.created" for record in _records(response))


def test_an_absurd_timeout_is_clamped_rather_than_honoured():
    """An unbounded follow is a resource leak one query string away.

    Asserted against the clamp rather than by issuing the request: the first
    version of this test DID issue it, and because the ceiling was an hour, the
    test dutifully waited an hour. A test that proves a bound by waiting for it
    is only as fast as the bound is wrong.
    """
    from mac.api import STREAM_MAX_TIMEOUT_SECONDS, clamp_stream_timeout

    assert clamp_stream_timeout(10**9) == STREAM_MAX_TIMEOUT_SECONDS
    assert clamp_stream_timeout(-5) > 0
    assert clamp_stream_timeout("nonsense") == STREAM_MAX_TIMEOUT_SECONDS


def test_a_follow_connection_is_not_held_for_hours():
    """A follower reconnects with the cursor it already has, so a long ceiling
    buys nothing and costs a pinned worker per idle client."""
    from mac.api import STREAM_MAX_TIMEOUT_SECONDS

    assert STREAM_MAX_TIMEOUT_SECONDS <= 300.0


def test_the_poll_interval_is_clamped_too():
    from mac.api import clamp_stream_poll_interval

    assert clamp_stream_poll_interval(10**6) == 30.0
    assert clamp_stream_poll_interval(0) > 0
