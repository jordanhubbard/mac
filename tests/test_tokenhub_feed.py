"""Tests for the TokenHub decision-feed consumer (ADR 0001, hu-05)."""

import json

from mac.tokenhub_feed import (
    event_to_record,
    iter_sse_events,
    record_event,
)


def _sse(*blocks: str) -> list:
    """Build raw SSE lines from event blocks like 'route_success {json}'."""
    out = []
    for b in blocks:
        etype, payload = b.split(" ", 1)
        out += ["event: %s" % etype, "data: %s" % payload, ""]
    return out


def test_iter_sse_events_parses_event_and_data():
    lines = _sse('route_success {"model_id": "nvidia/x", "provider_id": "nvidia"}')
    events = list(iter_sse_events(lines))
    assert events == [("route_success", {"model_id": "nvidia/x", "provider_id": "nvidia"})]


def test_iter_sse_events_handles_connected_and_multiple():
    lines = [
        "event: connected", 'data: {"status":"ok"}', "",
        "event: route_success", 'data: {"model_id":"a"}', "",
        ": this is a comment",
        "event: route_error", 'data: {"error_class":"rate_limited"}', "",
    ]
    events = list(iter_sse_events(lines))
    assert [e[0] for e in events] == ["connected", "route_success", "route_error"]


def test_iter_sse_events_accepts_bytes_and_skips_bad_json():
    lines = [b"event: route_success", b"data: {not json}", b"", b"event: route_success", b'data: {"model_id":"ok"}', b""]
    events = list(iter_sse_events(lines))
    # bad-json event yields empty dict; good one parses
    assert events[0] == ("route_success", {})
    assert events[1] == ("route_success", {"model_id": "ok"})


def test_event_to_record_route_success_attributes_to_agent():
    data = {
        "model_id": "nvidia/qwen3", "provider_id": "nvidia", "latency_ms": 812.0,
        "cost_usd": 0.0007, "total_tokens": 1234, "request_id": "req-1",
        "api_key_name": "natasha", "mode": "normal", "reason": "thompson_sample",
    }
    rec = event_to_record("route_success", data)
    assert rec["name"] == "tokenhub.route.success"
    assert rec["level"] == "info"
    assert rec["layer"] == "tokenhub"
    assert rec["subject_type"] == "agent" and rec["subject_id"] == "natasha"
    assert rec["detail"]["model_id"] == "nvidia/qwen3"
    assert rec["detail"]["reason"] == "thompson_sample"
    assert rec["detail"]["schema"] == "mac.tokenhub_decision.v1"


def test_event_to_record_levels():
    assert event_to_record("route_error", {"error_class": "fatal"})["level"] == "error"
    assert event_to_record("escalation", {"reason": "primary down"})["level"] == "warning"
    # health_change to a non-healthy state escalates to warning
    assert event_to_record("health_change", {"new_state": "down"})["level"] == "warning"
    assert event_to_record("health_change", {"new_state": "healthy"})["level"] == "info"


def test_event_to_record_skips_uninteresting_events():
    for et in ("connected", "heartbeat", "stream_started", "workflow_started", "message"):
        assert event_to_record(et, {}) is None


def test_event_to_record_omits_blank_fields_and_unattributed_subject():
    rec = event_to_record("route_success", {"model_id": "m", "latency_ms": 0.0, "cost_usd": 0})
    # zero/blank fields are dropped; no api_key_name -> no subject attribution
    assert "latency_ms" not in rec["detail"] and "cost_usd" not in rec["detail"]
    assert rec["subject_type"] is None and rec["subject_id"] is None


def test_record_event_emits_via_observability():
    captured = {}

    class FakeObs:
        def record_observation(self, **kwargs):
            captured.update(kwargs)
            return kwargs

    n = record_event(FakeObs(), "route_success", {"model_id": "m", "api_key_name": "rocky"})
    assert n is not None
    assert captured["name"] == "tokenhub.route.success"
    assert captured["subject_id"] == "rocky"


def test_record_event_tolerates_skips_and_no_observability():
    assert record_event(None, "route_success", {"model_id": "m"}) is None

    class FakeObs:
        def record_observation(self, **kwargs):
            raise AssertionError("should not be called for skipped event")

    assert record_event(FakeObs(), "heartbeat", {}) is None
