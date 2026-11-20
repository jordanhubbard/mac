"""Tests for fleet_brain_bus_sync — AgentBus → agent_messages sync module."""
from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from mac.fleet_brain_bus_sync import _get_token, _load_mac_env, sync


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


class TestLoadMacEnv:
    def test_returns_dict(self) -> None:
        result = _load_mac_env()
        assert isinstance(result, dict)

    def test_includes_mac_vars_from_os_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAC_HUB_URL", "http://test-hub:1234")
        result = _load_mac_env()
        assert result.get("MAC_HUB_URL") == "http://test-hub:1234"


class TestSync:
    """Unit tests for sync() using mocked PG and HTTP."""

    def _make_conn(self) -> MagicMock:
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = None  # no existing cursor
        conn.cursor.return_value = cur
        return conn

    def _make_broadcasts(self, n: int = 3) -> list[Dict[str, Any]]:
        return [
            {
                "sequence": i + 1,
                "event_id": f"evt_{i:04d}",
                "agent_id": "agent_natasha",
                "event_type": "task.created",
                "payload": {"task_id": f"task_{i:04d}"},
                "created_at": "2026-11-19T00:00:00Z",
            }
            for i in range(n)
        ]

    @patch("mac.fleet_brain_bus_sync.psycopg2.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts")
    def test_sync_inserts_new_events(
        self, mock_fetch: MagicMock, mock_connect: MagicMock
    ) -> None:
        conn = self._make_conn()
        mock_connect.return_value = conn

        broadcasts = self._make_broadcasts(3)
        mock_fetch.return_value = broadcasts

        # _upsert_message returns True (new insert)
        conn.cursor.return_value.fetchone.side_effect = [
            None,  # _read_cursor — no existing cursor
            None,  # _upsert_message first event
            None,
            None,
        ]

        with patch("mac.fleet_brain_bus_sync._upsert_message", return_value=True) as mock_up:
            result = sync(limit=10)

        assert result["ok"] is True
        assert result["fetched"] == 3
        assert result["inserted"] == 3
        assert result["skipped"] == 0

    @patch("mac.fleet_brain_bus_sync.psycopg2.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts")
    def test_sync_skips_duplicates(
        self, mock_fetch: MagicMock, mock_connect: MagicMock
    ) -> None:
        conn = self._make_conn()
        mock_connect.return_value = conn

        broadcasts = self._make_broadcasts(2)
        mock_fetch.return_value = broadcasts

        with patch("mac.fleet_brain_bus_sync._upsert_message", return_value=False):
            result = sync(limit=10)

        assert result["ok"] is True
        assert result["fetched"] == 2
        assert result["inserted"] == 0
        assert result["skipped"] == 2

    @patch("mac.fleet_brain_bus_sync.psycopg2.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts")
    def test_sync_empty_feed(
        self, mock_fetch: MagicMock, mock_connect: MagicMock
    ) -> None:
        conn = self._make_conn()
        mock_connect.return_value = conn
        mock_fetch.return_value = []

        result = sync(limit=10)

        assert result["ok"] is True
        assert result["fetched"] == 0
        assert result["inserted"] == 0

    @patch("mac.fleet_brain_bus_sync.psycopg2.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts", side_effect=Exception("network error"))
    def test_sync_returns_error_on_fetch_failure(
        self, mock_fetch: MagicMock, mock_connect: MagicMock
    ) -> None:
        conn = self._make_conn()
        mock_connect.return_value = conn

        result = sync(limit=10)

        assert result["ok"] is False
        assert "error" in result

    @patch("mac.fleet_brain_bus_sync.psycopg2.connect")
    @patch("mac.fleet_brain_bus_sync._fetch_broadcasts")
    def test_sync_advances_cursor(
        self, mock_fetch: MagicMock, mock_connect: MagicMock
    ) -> None:
        conn = self._make_conn()
        mock_connect.return_value = conn
        broadcasts = self._make_broadcasts(3)
        mock_fetch.return_value = broadcasts

        with patch("mac.fleet_brain_bus_sync._upsert_message", return_value=True):
            with patch("mac.fleet_brain_bus_sync._write_cursor") as mock_wc:
                result = sync(limit=10)

        # cursor_after should be max sequence (3)
        assert result["cursor_after"] == 3
        mock_wc.assert_called_once_with(conn, 3)
