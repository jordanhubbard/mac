"""Boundary coverage for worker control messages and terminal sessions."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac import worker


class _Client:
    def __init__(self) -> None:
        self.get_value = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, _path: str):
        return self.get_value

    def post(self, path: str, payload: dict):
        self.posts.append((path, payload))
        return {}


class _Process:
    def __init__(self, returncode=None, *, fail_wait=False, fail_kill=False) -> None:
        self.returncode = returncode
        self.fail_wait = fail_wait
        self.fail_kill = fail_kill
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        if self.fail_wait:
            raise TimeoutError("still running")
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.killed = True
        if self.fail_kill:
            raise OSError("already gone")


def _instance(tmp_path: Path, client=None) -> worker.MacWorker:
    return worker.MacWorker(
        client or _Client(),
        "agent",
        tmp_path / "workspace",
        lambda _task, _path: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path,
    )


def _terminal_request(**extra):
    request = {
        "schema": worker.DEBUG_TERMINAL_OPEN_SCHEMA,
        "session_id": "session",
        "input_stream_id": "input",
        "output_stream_id": "output",
        "sender_agent_id": "sender",
    }
    request.update(extra)
    return request


def _session(process=None) -> worker.DebugTerminalSession:
    return worker.DebugTerminalSession(
        session_id="session",
        input_stream_id="input",
        output_stream_id="output",
        output_recipient_agent_id="sender",
        process=process or _Process(),
        master_fd=101,
        expires_at_monotonic=999999999.0,
    )


@pytest.mark.parametrize(
    ("payload", "enabled", "summary"),
    [
        ({"schema": "future"}, True, "unsupported"),
        (_terminal_request(), False, "disabled"),
        ({"schema": worker.DEBUG_TERMINAL_OPEN_SCHEMA}, True, "missing"),
    ],
)
def test_debug_terminal_open_rejects_invalid_requests(
    monkeypatch, tmp_path, payload, enabled, summary
) -> None:
    instance = _instance(tmp_path)
    instance.debug_terminal_enabled = enabled
    published = []
    monkeypatch.setattr(
        instance, "_publish_debug_terminal_output", lambda *a, **k: published.append((a, k))
    )

    result = instance._execute_debug_terminal_open(payload, "stream")

    assert result["status"] == "error"
    assert summary in result["summary"]
    assert published


def test_debug_terminal_open_rejects_duplicate(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    instance._debug_terminal_sessions["session"] = _session(_Process(0))
    monkeypatch.setattr(instance, "_publish_debug_terminal_output", lambda *_a, **_k: None)
    result = instance._execute_debug_terminal_open(_terminal_request(), "stream")
    assert result["status"] == "error"
    assert "already exists" in result["summary"]


def test_debug_terminal_open_spawn_failure_closes_descriptors(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    closed = []
    published = []
    monkeypatch.setattr(worker.pty, "openpty", lambda: (100, 101))
    monkeypatch.setattr(instance, "_set_debug_terminal_size", lambda *_a: None)
    monkeypatch.setattr(
        worker.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom"))
    )
    monkeypatch.setattr(worker.os, "close", closed.append)
    monkeypatch.setattr(
        instance, "_publish_debug_terminal_output", lambda *a, **k: published.append((a, k))
    )

    result = instance._execute_debug_terminal_open(_terminal_request(), "stream")

    assert result["status"] == "error"
    assert "boom" in result["summary"]
    assert set(closed) == {100, 101}
    assert published[-1][1]["close"] is True


def test_debug_terminal_open_success_normalizes_shell_cwd_and_bounds(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    process = _Process()
    sizes = []
    output = []
    closed = []
    monkeypatch.setattr(worker.pty, "openpty", lambda: (100, 101))
    monkeypatch.setattr(instance, "_set_debug_terminal_size", lambda *a: sizes.append(a))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(
        worker.os, "set_blocking", lambda *_a: (_ for _ in ()).throw(OSError("unsupported"))
    )
    monkeypatch.setattr(worker.os, "close", closed.append)
    monkeypatch.setattr(
        instance, "_append_debug_terminal_output", lambda *a, **k: output.append((a, k))
    )

    result = instance._execute_debug_terminal_open(
        _terminal_request(shell="relative", cwd=str(tmp_path), rows=1, cols=999, ttl_seconds="bad"),
        "stream",
    )

    assert result["status"] == "opened"
    assert result["shell"] == "/bin/sh"
    assert result["cwd"] == str(tmp_path.resolve())
    assert result["ttl_seconds"] == 900
    assert sizes == [(101, 8, 240)]
    assert closed == [101]
    assert instance._debug_terminal_sessions["session"].process is process
    assert output[0][0][1] == "opened"


def test_debug_terminal_path_size_and_result_helpers(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    assert instance._debug_terminal_shell("relative") == "/bin/sh"
    assert instance._debug_terminal_shell("/definitely/missing") == "/bin/sh"
    assert instance._debug_terminal_cwd(str(tmp_path)) == tmp_path.resolve()
    assert instance._debug_terminal_cwd(str(tmp_path / "missing")) == instance.workspace
    monkeypatch.setattr(worker.fcntl, "ioctl", lambda *_a: (_ for _ in ()).throw(OSError("no tty")))
    instance._set_debug_terminal_size(1, 24, 80)
    result = instance._debug_terminal_result("s", {}, "error", "x" * 5000, detail="y" * 5000)
    assert len(result["summary"]) == 4000
    assert len(result["detail"]) == 4000


def test_debug_terminal_drain_output_boundaries(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    session = _session(_Process())
    emitted = []
    monkeypatch.setattr(
        instance, "_append_debug_terminal_output", lambda *a, **k: emitted.append((a, k))
    )

    monkeypatch.setattr(
        worker.select, "select", lambda *_a: (_ for _ in ()).throw(ValueError("bad fd"))
    )
    instance._drain_debug_terminal_output(session)
    monkeypatch.setattr(worker.select, "select", lambda *_a: ([], [], []))
    instance._drain_debug_terminal_output(session)
    monkeypatch.setattr(worker.select, "select", lambda *_a: ([101], [], []))
    monkeypatch.setattr(worker.os, "read", lambda *_a: (_ for _ in ()).throw(BlockingIOError()))
    instance._drain_debug_terminal_output(session)
    monkeypatch.setattr(worker.os, "read", lambda *_a: (_ for _ in ()).throw(OSError("closed")))
    instance._drain_debug_terminal_output(session)
    monkeypatch.setattr(worker.os, "read", lambda *_a: b"")
    instance._drain_debug_terminal_output(session)
    reads = iter([b"hello", b""])
    monkeypatch.setattr(worker.os, "read", lambda *_a: next(reads))
    instance._drain_debug_terminal_output(session)
    assert emitted[-1][0][1] == "output"
    assert emitted[-1][1]["data"] == b"hello"


def test_debug_terminal_input_filters_resizes_writes_and_closes(monkeypatch, tmp_path) -> None:
    client = _Client()
    instance = _instance(tmp_path, client)
    session = _session(_Process())
    client.get_value = [
        "bad",
        {"sequence": "bad", "payload": {"schema": "future"}},
        {"sequence": 2, "payload": {"resize": {"rows": 4, "cols": 500}}},
        {"sequence": 3, "payload": {"data_b64": "%%%"}},
        {"sequence": 4, "payload": {"data_b64": base64.b64encode(b"hello").decode()}},
        {"sequence": 5, "payload": {"close": True}},
    ]
    sizes = []
    emitted = []
    closed = []
    monkeypatch.setattr(instance, "_set_debug_terminal_size", lambda *a: sizes.append(a))
    monkeypatch.setattr(worker.os, "write", lambda *_a: (_ for _ in ()).throw(OSError("full")))
    monkeypatch.setattr(
        instance, "_append_debug_terminal_output", lambda *a, **k: emitted.append((a, k))
    )
    monkeypatch.setattr(
        instance, "_close_debug_terminal_session", lambda *a, **k: closed.append((a, k))
    )

    instance._apply_debug_terminal_input(session)

    assert session.next_input_sequence == 5
    assert sizes == [(101, 8, 240)]
    assert emitted[-1][0][1] == "error"
    assert closed[-1][1]["event"] == "closed"
    client.get_value = {}
    instance._apply_debug_terminal_input(session)


def test_debug_terminal_poll_exit_expiry_and_failure(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    close_calls = []
    monkeypatch.setattr(instance, "_drain_debug_terminal_output", lambda *_a: None)
    monkeypatch.setattr(instance, "_apply_debug_terminal_input", lambda *_a: None)
    monkeypatch.setattr(
        instance, "_close_debug_terminal_session", lambda *a, **k: close_calls.append((a, k))
    )

    closed = _session(_Process())
    closed.closed = True
    instance._poll_debug_terminal_session(closed)
    exited = _session(_Process(7))
    instance._poll_debug_terminal_session(exited)
    assert close_calls[-1][1]["exit_code"] == 7
    expired = _session(_Process())
    expired.expires_at_monotonic = 0
    instance._poll_debug_terminal_session(expired)
    assert close_calls[-1][1]["event"] == "expired"

    failing = _session(_Process())
    instance._debug_terminal_sessions["session"] = failing
    monkeypatch.setattr(
        instance,
        "_poll_debug_terminal_session",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("bad")),
    )
    instance._poll_debug_terminal_sessions()
    assert close_calls[-1][1]["event"] == "error"


def test_debug_terminal_close_termination_fallbacks(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    process = _Process(fail_wait=True)
    session = _session(process)
    instance._debug_terminal_sessions["session"] = session
    output = []
    monkeypatch.setattr(
        instance, "_append_debug_terminal_output", lambda *a, **k: output.append((a, k))
    )
    monkeypatch.setattr(worker.os, "close", lambda *_a: (_ for _ in ()).throw(OSError("closed")))
    instance._close_debug_terminal_session(session, event="closed", message="bye", terminate=True)
    assert process.terminated and process.killed
    assert "session" not in instance._debug_terminal_sessions
    assert output[-1][1]["close"] is True
    instance._close_debug_terminal_session(
        session, event="again", message="ignored", terminate=True
    )

    process = _Process(fail_wait=True, fail_kill=True)
    session = _session(process)
    instance._debug_terminal_sessions["session"] = session
    instance._close_all_debug_terminal_sessions()
    assert session.closed


def test_debug_terminal_output_publish_is_best_effort(tmp_path) -> None:
    client = _Client()
    instance = _instance(tmp_path, client)
    instance._publish_debug_terminal_output({}, "ignored")
    instance._publish_debug_terminal_output(_terminal_request(), "error", message="bad", close=True)
    assert client.posts[-1][0].endswith("/agentbus/streams/output/chunks")
    client.post = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("offline"))
    instance._append_debug_terminal_output_to_stream("session", "output", "error")


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_worker_source_state_reports_commit_tree_and_dirty(monkeypatch, tmp_path) -> None:
    results = iter(
        [
            _cp(stdout="a" * 40),
            _cp(stdout="b" * 40),
            _cp(stdout=" M local.py"),
        ]
    )
    monkeypatch.setattr(worker, "_run_git", lambda *_a, **_k: next(results))

    state = worker._worker_source_state(tmp_path)

    assert state["schema"] == "mac.worker_source_state.v1"
    assert state["repository_name"] == tmp_path.name
    assert state["commit_sha"] == "a" * 40
    assert state["tree_sha"] == "b" * 40
    assert state["dirty"] is True


def test_resolve_openshell_docker_bin_supports_launchd_paths(monkeypatch, tmp_path) -> None:
    configured = tmp_path / "configured-docker"
    configured.write_text("#!/bin/sh\n")
    configured.chmod(0o755)
    monkeypatch.setenv("MAC_OPENSHELL_DOCKER_BIN", str(configured))
    monkeypatch.setattr(worker.shutil, "which", lambda *_a: None)
    assert worker._resolve_openshell_docker_bin() == str(configured)

    monkeypatch.delenv("MAC_OPENSHELL_DOCKER_BIN")
    monkeypatch.setattr(
        worker.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    assert worker._resolve_openshell_docker_bin() == "/usr/bin/docker"
