"""
Tests for scripts/fleet_brain_bus_sync.py

Tests the pure-Python logic (agent ID mapping, insert SQL generation,
cursor read/write helpers) using subprocess mocking — no live DB or hub
connection required.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch


# ---------------------------------------------------------------------------
# Load the script as a module (it lives in scripts/, not a package)
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "fleet_brain_bus_sync.py"


def _load_bus_sync() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("fleet_brain_bus_sync", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


_bus = _load_bus_sync()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHubToLocalAgentMapping(unittest.TestCase):
    """Agent ID mapping table is correct and covers all fleet agents."""

    def test_known_agents_map_to_local_id(self) -> None:
        assert _bus.HUB_TO_LOCAL_AGENT_ID["agent_bullwinkle"] == "bullwinkle"
        assert _bus.HUB_TO_LOCAL_AGENT_ID["agent_rocky"] == "rocky"
        assert _bus.HUB_TO_LOCAL_AGENT_ID["agent_natasha"] == "natasha"
        assert _bus.HUB_TO_LOCAL_AGENT_ID["agent_asa"] == "asa"

    def test_hub_system_maps_to_none(self) -> None:
        assert _bus.HUB_TO_LOCAL_AGENT_ID["hub"] is None


class TestGetCursor(unittest.TestCase):
    """get_cursor returns int from psql output or 0 on failure."""

    def test_returns_int_from_psql_output(self) -> None:
        with patch("subprocess.check_output", return_value="12345\n") as m:
            result = _bus.get_cursor({})
            assert result == 12345
            # Verify psql was called with the right DB and query fragment
            args = m.call_args[0][0]
            assert "psql" in args
            assert "-d" in args
            assert "fleet_brain" in args

    def test_returns_zero_on_empty_output(self) -> None:
        with patch("subprocess.check_output", return_value="\n"):
            assert _bus.get_cursor({}) == 0

    def test_returns_zero_on_subprocess_error(self) -> None:
        import subprocess

        with patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "psql"),
        ):
            assert _bus.get_cursor({}) == 0


class TestSetCursor(unittest.TestCase):
    """set_cursor calls psql with an UPSERT for the new sequence."""

    def test_calls_psql_with_correct_sequence(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _bus.set_cursor(99999)
            args = mock_run.call_args[0][0]
            # Must reference bullwinkle and pass the new cursor value
            sql_arg = " ".join(args)
            assert "bullwinkle" in sql_arg
            assert "99999" in sql_arg


class TestInsertMessages(unittest.TestCase):
    """insert_messages builds correct SQL and counts successful inserts."""

    def _make_event(self, seq: int, agent_id: str = "agent_bullwinkle") -> dict:
        return {
            "sequence": seq,
            "agent_id": agent_id,
            "event_type": "task.created.v1",
            "created_at": "2026-11-13T01:00:00Z",
            "payload": {"task_id": "t1"},
        }

    def test_dry_run_counts_without_calling_psql(self) -> None:
        events = [self._make_event(1), self._make_event(2)]
        with patch("subprocess.run") as mock_run:
            count = _bus.insert_messages(events, dry_run=True)
            assert count == 2
            mock_run.assert_not_called()

    def test_inserts_known_agent_with_local_id(self) -> None:
        event = self._make_event(100, agent_id="agent_rocky")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="INSERT 0 1\n", stderr="")
            count = _bus.insert_messages([event], dry_run=False)
            assert count == 1
            sql = mock_run.call_args[0][0]
            # 'rocky' (not 'agent_rocky') must appear in the INSERT
            assert any("rocky" in str(arg) for arg in sql)

    def test_skips_hub_events_gracefully(self) -> None:
        """Hub events have from_agent=None; they should not cause FK errors."""
        event = self._make_event(200, agent_id="hub")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="INSERT 0 1\n", stderr="")
            # Should not raise
            _bus.insert_messages([event], dry_run=False)

    def test_returns_zero_on_empty_input(self) -> None:
        with patch("subprocess.run") as mock_run:
            count = _bus.insert_messages([], dry_run=False)
            assert count == 0
            mock_run.assert_not_called()

    def test_counts_only_successful_inserts(self) -> None:
        events = [self._make_event(i) for i in range(3)]
        responses = [
            MagicMock(returncode=0, stdout="INSERT 0 1\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="ERROR: duplicate"),
            MagicMock(returncode=0, stdout="INSERT 0 1\n", stderr=""),
        ]
        with patch("subprocess.run", side_effect=responses):
            count = _bus.insert_messages(events, dry_run=False)
            assert count == 2  # only 2 of 3 succeeded


class TestFetchBroadcasts(unittest.TestCase):
    """fetch_broadcasts returns parsed list or [] on error."""

    def test_returns_parsed_json_on_success(self) -> None:
        payload = json.dumps([{"sequence": 1, "event_type": "task.created.v1"}])
        with patch("subprocess.check_output", return_value=payload) as mock_co:
            result = _bus.fetch_broadcasts(0, 10, {"MAC_WORKER_TOKEN": "tok"})
            assert len(result) == 1
            assert result[0]["sequence"] == 1
            # Verify the right CLI command was constructed
            args = mock_co.call_args[0][0]
            assert "mac" in args
            assert "agentbus" in args

    def test_returns_empty_on_bad_json(self) -> None:
        with patch("subprocess.check_output", return_value="not json"):
            result = _bus.fetch_broadcasts(0, 5, {})
            assert result == []

    def test_returns_empty_on_subprocess_error(self) -> None:
        import subprocess

        with patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "mac"),
        ):
            result = _bus.fetch_broadcasts(0, 5, {})
            assert result == []


if __name__ == "__main__":
    unittest.main()
