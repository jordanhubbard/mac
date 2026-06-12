"""Tests for the NeMo Relay observability adapter (relay-01).

Cover the two parts that are exercisable without the (optional, pre-1.0)
``nemo_relay`` binding installed: the no-op degradation of the scope/event API,
and the pure OpenShell OCSF -> mac observation translation.
"""

from __future__ import annotations

import json

import pytest

from mac import relay_observability as ro


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("MAC_RELAY_OBSERVABILITY", raising=False)
    yield


# ---------------------------------------------------------------------------
# enable gating + no-op safety
# ---------------------------------------------------------------------------


def test_disabled_by_default():
    assert ro.enabled() is False


def test_enabled_requires_binding(monkeypatch):
    """Even opted-in, enabled() is False unless nemo_relay is importable."""
    monkeypatch.setenv("MAC_RELAY_OBSERVABILITY", "1")
    assert ro.enabled() is (ro.relay_available())


def test_record_event_noop_when_disabled():
    assert ro.record_event("x", data={"a": 1}) is False


def test_scope_yields_none_when_disabled():
    with ro.scope("agent-run", "Agent", task="t1") as handle:
        assert handle is None


def test_flush_noop_does_not_raise():
    ro.flush()  # must not raise when disabled


# ---------------------------------------------------------------------------
# OCSF -> observation translation (pure)
# ---------------------------------------------------------------------------


def test_network_denied_bumped_to_warning():
    rec = {
        "class_uid": 4001,
        "severity_id": 1,  # Informational
        "action": "Denied",
        "disposition": "Blocked",
        "dst_endpoint": {"domain": "httpbin.org", "port": 443},
    }
    obs = ro.ocsf_to_observation(rec)
    assert obs is not None
    assert obs["kind"] == "log"
    assert obs["layer"] == "sandbox"
    assert obs["source"] == "openshell"
    assert obs["name"] == "sandbox.network"
    assert obs["level"] == "warning"  # bumped up from info because Denied
    assert obs["detail"] is rec


def test_process_info_stays_info():
    obs = ro.ocsf_to_observation({"class_uid": 1007, "severity_id": 1, "action": "Allowed"})
    assert obs["name"] == "sandbox.process"
    assert obs["level"] == "info"


@pytest.mark.parametrize(
    "severity_id,expected",
    [(0, "info"), (1, "info"), (2, "info"), (3, "warning"), (4, "error"), (5, "critical"), (6, "critical")],
)
def test_severity_mapping(severity_id, expected):
    obs = ro.ocsf_to_observation({"class_uid": 2004, "severity_id": severity_id})
    assert obs["level"] == expected
    assert obs["name"] == "sandbox.finding"


def test_unknown_class_uses_generic_name():
    obs = ro.ocsf_to_observation({"class_uid": 9999, "severity_id": 1})
    assert obs["name"] == "sandbox.event"


def test_non_dict_returns_none():
    assert ro.ocsf_to_observation("not-a-dict") is None
    assert ro.ocsf_to_observation(None) is None


def test_iter_skips_non_dicts():
    records = [{"class_uid": 1007, "severity_id": 1}, "garbage", 42, {"class_uid": 4002, "severity_id": 1}]
    out = list(ro.iter_ocsf_observations(records))
    assert [o["name"] for o in out] == ["sandbox.process", "sandbox.http"]


# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------


def test_parse_ocsf_lines_filters_noise():
    lines = [
        "",  # blank
        "not json at all",  # human shorthand line
        json.dumps([1, 2, 3]),  # JSON but not an object
        json.dumps({"class_uid": 4001, "severity_id": 4, "action": "Allowed"}),
        json.dumps({"class_uid": 4001, "severity_id": 1, "action": "Denied"}),
    ]
    out = ro.parse_ocsf_lines(lines)
    assert len(out) == 2
    assert out[0]["level"] == "error"  # severity 4 (High)
    assert out[1]["level"] == "warning"  # denied bump from info
    assert all(o["layer"] == "sandbox" for o in out)
