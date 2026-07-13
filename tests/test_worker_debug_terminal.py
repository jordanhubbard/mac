"""Isolation tests for the extracted debug-terminal mixin."""

from __future__ import annotations

import sys
from pathlib import Path

from mac.worker_debug_terminal import DebugTerminalMixin, _bounded_int


class _Client:
    def __init__(self) -> None:
        self.posts = []

    def post(self, path, body):
        self.posts.append((path, body))
        return {}


class _Worker(DebugTerminalMixin):
    def __init__(self, workspace: Path) -> None:
        self.agent_id = "agent_test"
        self.workspace = workspace
        self.client = _Client()
        self._debug_terminal_sessions = {}
        self.logs = []

    def _observe_log(self, name, **kwargs):
        self.logs.append((name, kwargs))


def test_bounded_int_defaults_and_clamps() -> None:
    assert _bounded_int("bad", 8, 80, 32) == 32
    assert _bounded_int(2, 8, 80, 32) == 8
    assert _bounded_int(200, 8, 80, 32) == 80


def test_debug_result_is_bounded_and_keeps_identifiers(tmp_path) -> None:
    worker = _Worker(tmp_path)
    result = worker._debug_terminal_result(
        "stream-1",
        {"request_id": "req", "session_id": "session"},
        "error",
        "x" * 5000,
        detail="y" * 5000,
    )
    assert result["agent_id"] == "agent_test"
    assert result["request_id"] == "req"
    assert len(result["summary"]) == 4000
    assert len(result["detail"]) == 4000


def test_shell_accepts_executable_absolute_path_and_rejects_relative(tmp_path) -> None:
    worker = _Worker(tmp_path)
    assert worker._debug_terminal_shell(sys.executable) == sys.executable
    assert worker._debug_terminal_shell("bash") == "/bin/sh"


def test_cwd_uses_existing_directory_or_workspace(tmp_path) -> None:
    worker = _Worker(tmp_path)
    child = tmp_path / "child"
    child.mkdir()
    assert worker._debug_terminal_cwd(str(child)) == child.resolve()
    assert worker._debug_terminal_cwd(str(tmp_path / "missing")) == tmp_path


def test_publish_without_session_or_stream_is_a_noop(tmp_path) -> None:
    worker = _Worker(tmp_path)
    worker._publish_debug_terminal_output({}, "error", message="ignored")
    assert worker.client.posts == []


def test_append_output_encodes_data_and_final_flag(tmp_path) -> None:
    worker = _Worker(tmp_path)
    worker._append_debug_terminal_output_to_stream(
        "session", "output-stream", "output", data=b"hello", close=True
    )
    path, body = worker.client.posts[0]
    assert path.endswith("/output-stream/chunks")
    assert body["final"] is True
    assert body["payload"]["data_b64"] == "aGVsbG8="
