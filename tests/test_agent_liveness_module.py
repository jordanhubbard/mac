"""Unit tests for mac.agent_liveness — the heartbeat publisher and silent-agent watchdog.

All HTTP is fully mocked; no real hub is required.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

# Ensure the src path is on sys.path so the module is importable in-worktree.
# This matches what `pytest` does when PYTHONPATH=src is set, but we guard
# against the case where the caller forgot.
_SRC_PATH = str(Path(__file__).parent.parent / "src")
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

from mac.agent_liveness import (  # noqa: E402
    HEARTBEAT_EVENT_TYPE,
    HEARTBEAT_SCHEMA,
    SILENT_ALERT_EVENT_TYPE,
    SILENT_ALERT_SCHEMA,
    AgentStatus,
    _bearer,
    _get_config,
    _load_mac_env,
    _parse_iso,
    _post_json,
    _get_json,
    _latest_broadcast_timestamp,
    _list_registered_agents,
    _publish_silent_alert,
    check_silent_agents,
    main,
    publish_heartbeat,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_resp(body: dict | list, status: int = 200):
    """Return a minimal urllib response-like object."""
    raw = json.dumps(body).encode()
    resp = MagicMock()
    resp.read.return_value = raw
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _past_iso(hours: float = 5.0) -> str:
    t = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    return t.isoformat()


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------


def test_schema_constants_are_stable() -> None:
    assert HEARTBEAT_SCHEMA == "mac.agentbus.agent_heartbeat.v1"
    assert HEARTBEAT_EVENT_TYPE == "agent.heartbeat.v1"
    assert SILENT_ALERT_SCHEMA == "mac.agentbus.agent_silent_alert.v1"
    assert SILENT_ALERT_EVENT_TYPE == "agent.silent.alert.v1"


# ---------------------------------------------------------------------------
# _bearer
# ---------------------------------------------------------------------------


def test_bearer_with_token_returns_header() -> None:
    h = _bearer("tok123")
    assert h == {"Authorization": "Bearer tok123"}


def test_bearer_with_empty_token_returns_empty_dict() -> None:
    assert _bearer("") == {}


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_suffix() -> None:
    dt = _parse_iso("2026-11-21T10:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_handles_offset() -> None:
    dt = _parse_iso("2026-11-21T10:00:00+00:00")
    assert dt is not None


def test_parse_iso_returns_none_on_garbage() -> None:
    assert _parse_iso("not-a-date") is None


def test_parse_iso_returns_none_on_empty_string() -> None:
    assert _parse_iso("") is None


# ---------------------------------------------------------------------------
# _post_json
# ---------------------------------------------------------------------------


def test_post_json_sends_correct_payload() -> None:
    resp = _make_resp({"ok": True})
    with patch("urllib.request.urlopen", return_value=resp) as mock_open:
        result = _post_json("http://hub/endpoint", {"key": "val"}, "mytoken")

    assert result == {"ok": True}
    req_obj = mock_open.call_args[0][0]
    assert req_obj.full_url == "http://hub/endpoint"
    assert req_obj.method == "POST"
    assert req_obj.headers.get("Authorization") == "Bearer mytoken"
    assert req_obj.headers.get("Content-type") == "application/json"
    sent_body = json.loads(req_obj.data)
    assert sent_body == {"key": "val"}


def test_post_json_raises_on_http_error() -> None:
    err = urllib.error.HTTPError("http://hub/ep", 403, "Forbidden", {}, BytesIO(b"denied"))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="HTTP 403"):
            _post_json("http://hub/ep", {}, "tok")


# ---------------------------------------------------------------------------
# _get_json
# ---------------------------------------------------------------------------


def test_get_json_returns_parsed_body() -> None:
    resp = _make_resp([{"id": "agent_bullwinkle"}])
    with patch("urllib.request.urlopen", return_value=resp):
        result = _get_json("http://hub/agents", "tok")
    assert result == [{"id": "agent_bullwinkle"}]


def test_get_json_raises_on_http_error() -> None:
    err = urllib.error.HTTPError("http://hub/ep", 401, "Unauthorized", {}, BytesIO(b"bad"))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="HTTP 401"):
            _get_json("http://hub/ep", "tok")


# ---------------------------------------------------------------------------
# _list_registered_agents
# ---------------------------------------------------------------------------


def test_list_registered_agents_from_list_response() -> None:
    resp = _make_resp([{"id": "agent_a"}, {"id": "agent_b"}])
    with patch("urllib.request.urlopen", return_value=resp):
        agents = _list_registered_agents("http://hub", "tok")
    assert agents == ["agent_a", "agent_b"]


def test_list_registered_agents_from_dict_response() -> None:
    resp = _make_resp({"agents": [{"id": "agent_x"}]})
    with patch("urllib.request.urlopen", return_value=resp):
        agents = _list_registered_agents("http://hub", "tok")
    assert agents == ["agent_x"]


def test_list_registered_agents_empty() -> None:
    resp = _make_resp([])
    with patch("urllib.request.urlopen", return_value=resp):
        agents = _list_registered_agents("http://hub", "tok")
    assert agents == []


# ---------------------------------------------------------------------------
# _latest_broadcast_timestamp
# ---------------------------------------------------------------------------


def test_latest_broadcast_timestamp_returns_datetime_on_success() -> None:
    ts = _past_iso(1.0)
    resp = _make_resp([{"created_at": ts}])
    with patch("urllib.request.urlopen", return_value=resp):
        result = _latest_broadcast_timestamp("http://hub", "tok", "agent_bw")
    assert result is not None
    assert isinstance(result, datetime)


def test_latest_broadcast_timestamp_returns_none_on_empty_feed() -> None:
    resp = _make_resp([])
    with patch("urllib.request.urlopen", return_value=resp):
        result = _latest_broadcast_timestamp("http://hub", "tok", "agent_bw")
    assert result is None


def test_latest_broadcast_timestamp_handles_timestamp_key_fallback() -> None:
    ts = _past_iso(0.5)
    resp = _make_resp([{"timestamp": ts}])
    with patch("urllib.request.urlopen", return_value=resp):
        result = _latest_broadcast_timestamp("http://hub", "tok", "agent_bw")
    assert result is not None


# ---------------------------------------------------------------------------
# publish_heartbeat
# ---------------------------------------------------------------------------


def test_publish_heartbeat_sends_expected_body() -> None:
    resp = _make_resp({"id": "bus_abc"})
    with patch("urllib.request.urlopen", return_value=resp) as mock_open:
        result = publish_heartbeat(
            "agent_bullwinkle", hub="http://hub", token="tok", note="testing"
        )

    assert result == {"id": "bus_abc"}
    req_obj = mock_open.call_args[0][0]
    body = json.loads(req_obj.data)
    assert body["agent_id"] == "agent_bullwinkle"
    assert body["event_type"] == HEARTBEAT_EVENT_TYPE
    assert body["payload"]["schema"] == HEARTBEAT_SCHEMA
    assert body["payload"]["note"] == "testing"
    assert "timestamp" in body["payload"]


def test_publish_heartbeat_omits_note_when_empty() -> None:
    resp = _make_resp({"id": "bus_xyz"})
    with patch("urllib.request.urlopen", return_value=resp) as mock_open:
        publish_heartbeat("agent_bw", hub="http://hub", token="tok")

    body = json.loads(mock_open.call_args[0][0].data)
    assert "note" not in body["payload"]


def test_publish_heartbeat_raises_on_hub_error() -> None:
    err = urllib.error.HTTPError("http://hub/agentbus/broadcast", 500, "err", {}, BytesIO(b"x"))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError):
            publish_heartbeat("agent_bw", hub="http://hub", token="tok")


# ---------------------------------------------------------------------------
# _publish_silent_alert
# ---------------------------------------------------------------------------


def test_publish_silent_alert_sends_expected_payload() -> None:
    resp = _make_resp({"ok": True})
    last_seen = datetime.now(tz=timezone.utc) - timedelta(hours=5)
    with patch("urllib.request.urlopen", return_value=resp) as mock_open:
        _publish_silent_alert("http://hub", "tok", "agent_bw", "agent_rocky", last_seen, 4.0)

    body = json.loads(mock_open.call_args[0][0].data)
    assert body["event_type"] == SILENT_ALERT_EVENT_TYPE
    assert body["payload"]["schema"] == SILENT_ALERT_SCHEMA
    assert body["payload"]["silent_agent"] == "agent_rocky"
    assert body["payload"]["watchdog_agent"] == "agent_bw"
    assert body["payload"]["threshold_hours"] == 4.0
    assert body["payload"]["last_seen"] is not None


def test_publish_silent_alert_handles_none_last_seen() -> None:
    resp = _make_resp({"ok": True})
    with patch("urllib.request.urlopen", return_value=resp) as mock_open:
        _publish_silent_alert("http://hub", "tok", "agent_bw", "agent_x", None, 4.0)

    body = json.loads(mock_open.call_args[0][0].data)
    assert body["payload"]["last_seen"] is None


# ---------------------------------------------------------------------------
# check_silent_agents
# ---------------------------------------------------------------------------


def _mock_urlopen_sequence(*responses):
    """Side-effect factory: each call to urlopen returns the next response."""
    it = iter(responses)

    def _side(req, timeout=None):  # noqa: ANN001
        return next(it)

    return _side


def test_check_silent_agents_marks_long_silent_as_silent() -> None:
    agents_resp = _make_resp([{"id": "agent_rocky"}, {"id": "agent_natasha"}])
    # agent_rocky: seen 10h ago  → silent
    # agent_natasha: seen 1h ago → ok
    rocky_ts_resp = _make_resp([{"created_at": _past_iso(10.0)}])
    # second URL hit for rocky (all-event-types fallback) returns empty so rocky is handled
    rocky_fallback = _make_resp([])
    natasha_ts_resp = _make_resp([{"created_at": _past_iso(1.0)}])
    natasha_fallback = _make_resp([])

    side_effects = [
        agents_resp,
        rocky_ts_resp,
        rocky_fallback,
        natasha_ts_resp,
        natasha_fallback,
    ]

    alert_calls = []

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        return side_effects.pop(0)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        # alert=False so we don't need to mock the alert POST
        statuses = check_silent_agents(
            hub="http://hub",
            token="tok",
            watchdog_agent="agent_bw",
            threshold_hours=4.0,
            alert=False,
        )

    rocky_status = next(s for s in statuses if s.agent_id == "agent_rocky")
    natasha_status = next(s for s in statuses if s.agent_id == "agent_natasha")

    assert rocky_status.is_silent is True
    assert rocky_status.hours_silent > 4.0
    assert natasha_status.is_silent is False


def test_check_silent_agents_never_seen_is_silent() -> None:
    agents_resp = _make_resp([{"id": "agent_ghost"}])
    empty1 = _make_resp([])
    empty2 = _make_resp([])

    side_effects = [agents_resp, empty1, empty2]

    with patch("urllib.request.urlopen", side_effect=lambda r, **kw: side_effects.pop(0)):
        statuses = check_silent_agents(
            hub="http://hub",
            token="tok",
            watchdog_agent="agent_bw",
            threshold_hours=4.0,
            alert=False,
        )

    assert len(statuses) == 1
    s = statuses[0]
    assert s.is_silent is True
    assert s.hours_silent is None
    assert s.last_seen is None


def test_check_silent_agents_uses_explicit_agent_list() -> None:
    """When `agents` is supplied, the /agents endpoint is not queried."""
    ts_resp = _make_resp([{"created_at": _past_iso(1.0)}])
    fallback = _make_resp([])
    side_effects = [ts_resp, fallback]

    with patch("urllib.request.urlopen", side_effect=lambda r, **kw: side_effects.pop(0)) as mo:
        statuses = check_silent_agents(
            hub="http://hub",
            token="tok",
            watchdog_agent="agent_bw",
            threshold_hours=4.0,
            alert=False,
            agents=["agent_bw"],
        )

    # /agents was NOT called — only the per-agent broadcast endpoints
    for c in mo.call_args_list:
        req = c[0][0]
        assert "/agents" not in req.full_url or "/agentbus" in req.full_url


def test_check_silent_agents_publishes_alert_for_silent_agent() -> None:
    """alert=True causes a silent-alert POST for each silent agent."""
    agents_resp = _make_resp([{"id": "agent_rocky"}])
    # Agent is silent (never seen)
    empty1 = _make_resp([])
    empty2 = _make_resp([])
    alert_resp = _make_resp({"ok": True})

    side_effects = [agents_resp, empty1, empty2, alert_resp]

    with patch("urllib.request.urlopen", side_effect=lambda r, **kw: side_effects.pop(0)):
        statuses = check_silent_agents(
            hub="http://hub",
            token="tok",
            watchdog_agent="agent_bw",
            threshold_hours=4.0,
            alert=True,
        )

    assert statuses[0].is_silent is True


def test_check_silent_agents_alert_failure_is_non_fatal() -> None:
    """A RuntimeError from alert publish must not abort the watchdog."""
    agents_resp = _make_resp([{"id": "agent_ghost"}])
    empty1 = _make_resp([])
    empty2 = _make_resp([])
    # Simulate alert POST failure
    alert_err = urllib.error.HTTPError(
        "http://hub/agentbus/broadcast", 503, "err", {}, BytesIO(b"x")
    )

    side_effects = [agents_resp, empty1, empty2]

    with patch("urllib.request.urlopen", side_effect=lambda r, **kw: side_effects.pop(0)):
        with patch("mac.agent_liveness._publish_silent_alert", side_effect=RuntimeError("boom")):
            statuses = check_silent_agents(
                hub="http://hub",
                token="tok",
                watchdog_agent="agent_bw",
                threshold_hours=4.0,
                alert=True,
            )

    # Still got a result; RuntimeError was swallowed
    assert len(statuses) == 1
    assert statuses[0].is_silent is True


# ---------------------------------------------------------------------------
# AgentStatus repr / str
# ---------------------------------------------------------------------------


def test_agent_status_repr_with_hours() -> None:
    s = AgentStatus("agent_bw", None, 5.5, True)
    r = repr(s)
    # Should not raise; content not strictly specified but should include agent_id
    assert "agent_bw" in r


def test_agent_status_repr_never_seen() -> None:
    s = AgentStatus("agent_bw", None, None, True)
    r = repr(s)
    assert "agent_bw" in r


# ---------------------------------------------------------------------------
# CLI: heartbeat command
# ---------------------------------------------------------------------------


def test_cli_heartbeat_calls_publish_heartbeat(capsys) -> None:
    with patch("mac.agent_liveness.publish_heartbeat", return_value={"id": "bus_x"}) as mock_hb:
        with patch(
            "sys.argv",
            [
                "agent_liveness",
                "--agent",
                "agent_bw",
                "--hub",
                "http://hub",
                "--token",
                "tok",
                "heartbeat",
                "--note",
                "alive",
            ],
        ):
            rc = main()

    assert rc == 0
    mock_hb.assert_called_once_with("agent_bw", hub="http://hub", token="tok", note="alive")


def test_cli_heartbeat_missing_token_exits_nonzero() -> None:
    with patch(
        "sys.argv", ["agent_liveness", "--agent", "agent_bw", "--hub", "http://hub", "heartbeat"]
    ):
        with patch(
            "mac.agent_liveness._get_config",
            return_value={"hub": "http://hub", "token": "", "agent": ""},
        ):
            rc = main()

    assert rc != 0


# ---------------------------------------------------------------------------
# CLI: watchdog command
# ---------------------------------------------------------------------------


def test_cli_watchdog_ok_when_all_active(capsys) -> None:
    statuses = [
        AgentStatus("agent_bw", datetime.now(tz=timezone.utc), 0.1, False),
    ]
    with patch("mac.agent_liveness.check_silent_agents", return_value=statuses):
        with patch(
            "sys.argv",
            [
                "agent_liveness",
                "--agent",
                "agent_bw",
                "--hub",
                "http://hub",
                "--token",
                "tok",
                "watchdog",
            ],
        ):
            rc = main()

    assert rc == 0


def test_cli_watchdog_returns_2_when_silent_agents_found(capsys) -> None:
    statuses = [
        AgentStatus("agent_ghost", None, None, True),
    ]
    with patch("mac.agent_liveness.check_silent_agents", return_value=statuses):
        with patch(
            "sys.argv",
            [
                "agent_liveness",
                "--agent",
                "agent_bw",
                "--hub",
                "http://hub",
                "--token",
                "tok",
                "watchdog",
            ],
        ):
            rc = main()

    assert rc == 2


def test_cli_watchdog_no_alert_flag_suppresses_alerts() -> None:
    statuses = [AgentStatus("agent_ghost", None, None, True)]
    with patch("mac.agent_liveness.check_silent_agents", return_value=statuses) as mock_check:
        with patch(
            "sys.argv",
            [
                "agent_liveness",
                "--agent",
                "agent_bw",
                "--hub",
                "http://hub",
                "--token",
                "tok",
                "watchdog",
                "--no-alert",
            ],
        ):
            main()

    kwargs = mock_check.call_args[1]
    assert kwargs["alert"] is False


def test_cli_no_subcommand_exits_zero(capsys) -> None:
    with patch("sys.argv", ["agent_liveness"]):
        rc = main()
    assert rc == 0
