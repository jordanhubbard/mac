"""Tests for fleet_brain_bus_sync - AgentBus -> fleet_brain.bus_messages sync module.

Uses psycopg3 (not psycopg2) and targets fleet_brain.bus_messages/agent_cursors.
All PG and HTTP calls are mocked; no live database required.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from mac.fleet_brain_bus_sync import (
    _get_token,
    _load_mac_env,
    _normalise_agent,
    sync,
)


# ---------------------------------------------------------------------------
# _get_token
# ---------------------------------------------------------------------------


class TestGetToken:
    def test_prefers_mac_worker_token(self) -> None:
        env = {"MAC_WORKER_TOKEN": "tok1", "MAC_API_TOKEN": "tok2"}
        assert _get_token(env) == "tok1"

    def test_falls_back_to_api_token(self) -> None:
        env = {"MAC_API_TOKEN": "tok2"}
        assert _get_token(env) == "tok2"

    def test_prefers_scoped_worker_token(self) -> None:
        env = {"MAC_WORKER_TOKEN__MAC": "scopedtok", "MAC_API_TOKEN": "plain"}
        assert _get_token(env) == "scopedtok"

    def test_returns_empty_when_missing(self) -> None:
        assert _get_token({}) == ""


# ---------------------------------------------------------------------------
# _load_mac_env
# ---------------------------------------------------------------------------


class TestLoadMacEnv:
    def test_returns_dict(self) -> None:
        result = _load_mac_env()
        assert isinstance(result, dict)

    def test_includes_mac_vars_from_os_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAC_HUB_URL", "http://test-hub:1234")
        result = _load_mac_env()
        assert result.get("MAC_HUB_URL") == "http://test-hub:1234"

    def test_includes_fleet_vars_from_os_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FLEET_BRAIN_DSN", "dbname=test_fleet")
        result = _load_mac_env()
        assert result.get("FLEET_BRAIN_DSN") == "dbname=test_fleet"


# ---------------------------------------------------------------------------
# _normalise_agent
# ---------------------------------------------------------------------------


class TestNormaliseAgent:
    def test_strips_agent_prefix(self) -> None:
        assert _normalise_agent("agent_bullwinkle") == "bullwinkle"

    def test_already_short(self) -> None:
        assert _normalise_agent("rocky") == "rocky"

    def test_unknown_agent_returns_none(self) -> None:
        assert _normalise_agent("agent_unknown_xyz") is None

    def test_none_returns_none(self) -> None:
        assert _normalise_agent(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _normalise_agent("") is None


# ---------------------------------------------------------------------------
# sync() - unit tests with mocked PG and HTTP
# ---------------------------------------------------------------------------


def _make_events(n: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "sequence": i + 1,
            "event_id": f"evt_{i:04d}",
            "agent_id": "agent_bullwinkle",
            "event_type": "task.created",
            "payload": {"task_id": f"task_{i:04d}"},
            "created_at": "2026-11-21T00:00:00Z",
            "task_id": f"task_{i:04d}",
            "project": "mac",
        }
        for i in range(n)
    ]


def _make_conn(cursor_row: Any = None) -> MagicMock:
    """Return a minimal mock psycopg3 connection."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    # fetchone for cursor read returns cursor_row; for MAX(id) returns max id row
    cur.fetchone.side_effect = [cursor_row, {"max_id": 42}] * 10
    cur.rowcount = 1  # simulates INSERT inserted a row
    conn.cursor.return_value = cur
    return conn


_ENV_WITH_TOKEN: dict[str, str] = {
    "MAC_WORKER_TOKEN": "test-token",
    "MAC_HUB_URL": "http://hub.test:8789",
    "FLEET_BRAIN_DSN": "dbname=test_fleet",
}


class TestSync:
    @pytest.fixture(autouse=True)
    def _patch_env(self) -> None:
        with patch("mac.fleet_brain_bus_sync._load_mac_env", return_value=_ENV_WITH_TOKEN):
            yield

    @patch("mac.fleet_brain_bus_sync.psycopg.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts", return_value=[])
    def test_no_token_returns_error(self, _fetch: MagicMock, _connect: MagicMock) -> None:
        with patch(
            "mac.fleet_brain_bus_sync._load_mac_env",
            return_value={"MAC_HUB_URL": "http://hub.test:8789"},
        ):
            result = sync()
        assert result["ok"] is False
        assert "no MAC token" in result["error"]

    @patch("mac.fleet_brain_bus_sync.psycopg.connect")
    def test_pg_connect_failure_returns_error(self, mock_connect: MagicMock) -> None:
        import psycopg

        mock_connect.side_effect = psycopg.OperationalError("conn refused")
        result = sync()
        assert result["ok"] is False
        assert "PG connect failed" in result["error"]

    @patch("mac.fleet_brain_bus_sync.psycopg.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts")
    def test_sync_inserts_new_events(self, mock_fetch: MagicMock, mock_connect: MagicMock) -> None:
        events = _make_events(3)
        mock_fetch.return_value = events
        conn = _make_conn()
        mock_connect.return_value = conn

        result = sync()

        assert result["ok"] is True
        assert result["fetched"] == 3
        assert result["inserted"] == 3

    @patch("mac.fleet_brain_bus_sync.psycopg.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts", return_value=[])
    def test_empty_broadcast_returns_ok(self, _fetch: MagicMock, mock_connect: MagicMock) -> None:
        conn = _make_conn()
        mock_connect.return_value = conn
        result = sync()
        assert result["ok"] is True
        assert result["fetched"] == 0
        assert result["inserted"] == 0

    @patch("mac.fleet_brain_bus_sync.psycopg.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts")
    def test_reset_cursor_uses_sequence_zero(
        self, mock_fetch: MagicMock, mock_connect: MagicMock
    ) -> None:
        mock_fetch.return_value = []
        conn = _make_conn()
        mock_connect.return_value = conn
        sync(reset_cursor=True)
        # _fetch_broadcasts should have been called with after_sequence=0
        _, kwargs = mock_fetch.call_args
        assert (
            kwargs.get(
                "after_sequence",
                mock_fetch.call_args.args[2] if len(mock_fetch.call_args.args) > 2 else -1,
            )
            == 0
        )

    @patch("mac.fleet_brain_bus_sync.psycopg.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts")
    def test_uses_fleet_brain_dsn(self, mock_fetch: MagicMock, mock_connect: MagicMock) -> None:
        mock_fetch.return_value = []
        conn = _make_conn()
        mock_connect.return_value = conn
        sync()
        args, kwargs = mock_connect.call_args
        assert args == ("dbname=test_fleet",)
        assert "row_factory" in kwargs

    @patch("mac.fleet_brain_bus_sync.psycopg.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts", side_effect=RuntimeError("HTTP 503"))
    def test_fetch_error_returns_error(self, _fetch: MagicMock, mock_connect: MagicMock) -> None:
        conn = _make_conn()
        mock_connect.return_value = conn
        result = sync()
        assert result["ok"] is False
        assert "HTTP 503" in result["error"]

    @patch("mac.fleet_brain_bus_sync.psycopg.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts")
    def test_custom_agent_id_forwarded(
        self, mock_fetch: MagicMock, mock_connect: MagicMock
    ) -> None:
        mock_fetch.return_value = []
        conn = _make_conn()
        mock_connect.return_value = conn
        sync(agent_id="natasha")
        # Hub agent ID should be "agent_natasha"
        args = mock_fetch.call_args.args
        assert "agent_natasha" in args or mock_fetch.call_args.args[2] == "agent_natasha"

    @patch("mac.fleet_brain_bus_sync.psycopg.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts")
    def test_skipped_count_when_rowcount_zero(
        self, mock_fetch: MagicMock, mock_connect: MagicMock
    ) -> None:
        events = _make_events(2)
        mock_fetch.return_value = events
        conn = _make_conn()
        # Simulate all inserts returning 0 (already present)
        conn.cursor.return_value.rowcount = 0
        mock_connect.return_value = conn
        result = sync()
        assert result["ok"] is True
        assert result["fetched"] == 2
        assert result["skipped"] == 2

    @patch("mac.fleet_brain_bus_sync.psycopg.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts")
    def test_max_sequence_tracked(self, mock_fetch: MagicMock, mock_connect: MagicMock) -> None:
        events = _make_events(5)
        mock_fetch.return_value = events
        conn = _make_conn()
        mock_connect.return_value = conn
        result = sync()
        assert result["max_sequence"] == 5
