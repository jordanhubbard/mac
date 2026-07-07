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
    monkeypatch.setattr(instance, "_publish_debug_terminal_output", lambda *a, **k: published.append((a, k)))

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
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(worker.os, "close", closed.append)
    monkeypatch.setattr(instance, "_publish_debug_terminal_output", lambda *a, **k: published.append((a, k)))

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
    monkeypatch.setattr(worker.os, "set_blocking", lambda *_a: (_ for _ in ()).throw(OSError("unsupported")))
    monkeypatch.setattr(worker.os, "close", closed.append)
    monkeypatch.setattr(instance, "_append_debug_terminal_output", lambda *a, **k: output.append((a, k)))

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
    monkeypatch.setattr(instance, "_append_debug_terminal_output", lambda *a, **k: emitted.append((a, k)))

    monkeypatch.setattr(worker.select, "select", lambda *_a: (_ for _ in ()).throw(ValueError("bad fd")))
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
    monkeypatch.setattr(instance, "_append_debug_terminal_output", lambda *a, **k: emitted.append((a, k)))
    monkeypatch.setattr(instance, "_close_debug_terminal_session", lambda *a, **k: closed.append((a, k)))

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
    monkeypatch.setattr(instance, "_close_debug_terminal_session", lambda *a, **k: close_calls.append((a, k)))

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
    monkeypatch.setattr(instance, "_poll_debug_terminal_session", lambda *_a: (_ for _ in ()).throw(RuntimeError("bad")))
    instance._poll_debug_terminal_sessions()
    assert close_calls[-1][1]["event"] == "error"


def test_debug_terminal_close_termination_fallbacks(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    process = _Process(fail_wait=True)
    session = _session(process)
    instance._debug_terminal_sessions["session"] = session
    output = []
    monkeypatch.setattr(instance, "_append_debug_terminal_output", lambda *a, **k: output.append((a, k)))
    monkeypatch.setattr(worker.os, "close", lambda *_a: (_ for _ in ()).throw(OSError("closed")))
    instance._close_debug_terminal_session(session, event="closed", message="bye", terminate=True)
    assert process.terminated and process.killed
    assert "session" not in instance._debug_terminal_sessions
    assert output[-1][1]["close"] is True
    instance._close_debug_terminal_session(session, event="again", message="ignored", terminate=True)

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


@pytest.mark.parametrize(
    ("payload", "status", "summary"),
    [
        ({"schema": "future"}, "error", "unsupported"),
        ({"repo_path": "/different"}, "error", "does not match"),
        ({"restart_services": ["../bad"]}, "error", "invalid systemd"),
        ({"remote": "--bad"}, "error", "invalid git remote"),
        ({"branch": "bad branch"}, "error", "invalid git branch"),
    ],
)
def test_repo_update_rejects_invalid_requests(tmp_path, payload, status, summary) -> None:
    result = _instance(tmp_path)._execute_repo_update(payload, "stream")
    assert result["status"] == status
    assert summary in result["summary"]


def test_repo_update_missing_and_git_validation_paths(monkeypatch, tmp_path) -> None:
    missing = worker.MacWorker(
        _Client(), "agent", tmp_path / "workspace", lambda *_a: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path / "missing",
    )
    assert missing._execute_repo_update({}, "s")["status"] == "skipped"

    instance = _instance(tmp_path)
    monkeypatch.setattr(worker, "_run_git", lambda *_a, **_k: _cp(1, stderr="not git"))
    assert "not a git" in instance._execute_repo_update({}, "s")["summary"]

    results = iter([_cp(stdout="true"), _cp(1, stderr="status failed")])
    monkeypatch.setattr(worker, "_run_git", lambda *_a, **_k: next(results))
    assert "inspect git status" in instance._execute_repo_update({}, "s")["summary"]

    results = iter([_cp(stdout="true"), _cp(stdout=" M file")])
    monkeypatch.setattr(worker, "_run_git", lambda *_a, **_k: next(results))
    assert "local modifications" in instance._execute_repo_update({}, "s")["summary"]


def test_repo_update_pull_failure_current_and_updated(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    old = "a" * 40
    new = "b" * 40
    results = iter([
        _cp(stdout="true"), _cp(), _cp(stdout=old), _cp(1, "pull out", "pull err")
    ])
    monkeypatch.setattr(worker, "_run_git", lambda *_a, **_k: next(results))
    result = instance._execute_repo_update({"branch": "main"}, "s")
    assert result["status"] == "error" and result["before_sha"] == old

    results = iter([_cp(stdout="true"), _cp(), _cp(1), _cp(stdout="ok"), _cp(1)])
    monkeypatch.setattr(worker, "_run_git", lambda *_a, **_k: next(results))
    assert instance._execute_repo_update({}, "s")["status"] == "no_update"

    results = iter([_cp(stdout="true"), _cp(), _cp(stdout=old), _cp(stdout="ok"), _cp(stdout=new)])
    monkeypatch.setattr(worker, "_run_git", lambda *_a, **_k: next(results))
    monkeypatch.setattr(instance, "_maybe_rebuild_openshell_image_after_update", lambda *_a: {"status": "rebuilt"})
    result = instance._execute_repo_update(
        {"restart": True, "restart_services": ["one.service", "one.service"]}, "s"
    )
    assert result["status"] == "updated"
    assert result["restart_requested"] is True
    assert result["restart_services"] == ["one.service"]
    assert "openshell image rebuilt" in result["summary"]


def test_openshell_image_rebuild_gates_and_drift(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    assert instance._maybe_rebuild_openshell_image_after_update(tmp_path, "a", "b") is None
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_REBUILD_ON_SOURCE_UPDATE", "0")
    assert instance._maybe_rebuild_openshell_image_after_update(tmp_path, "a", "b") is None
    monkeypatch.setenv("MAC_OPENSHELL_REBUILD_ON_SOURCE_UPDATE", "1")
    marker = tmp_path / "image-source-sha"
    marker.write_text("b\n")
    monkeypatch.setenv("MAC_OPENSHELL_IMAGE_SOURCE_SHA_FILE", str(marker))
    assert instance._maybe_rebuild_openshell_image_after_update(tmp_path, "a", "b") is None
    marker.unlink()
    monkeypatch.setattr(worker, "_resolve_openshell_docker_bin", lambda: None)
    assert instance._maybe_rebuild_openshell_image_after_update(tmp_path, "a", "b")["status"] == "drift"


def test_openshell_image_rebuild_failures_and_success(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    containerfile = tmp_path / worker._OPENSHELL_CONTAINERFILE_RELPATH
    containerfile.parent.mkdir(parents=True)
    containerfile.write_text("FROM scratch\n")
    image_builder = tmp_path / "deploy/openshell/build-runtime-image.sh"
    image_builder.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_IMAGE_TAG", "custom:tag")
    monkeypatch.setenv("MAC_OPENSHELL_IMAGE_SOURCE_SHA_FILE", str(tmp_path / "image-source-sha"))
    monkeypatch.setattr(worker, "_resolve_openshell_docker_bin", lambda: "/docker")
    monkeypatch.setattr(worker.shutil, "which", lambda name: None if name == "podman" else "/docker")
    monkeypatch.setattr(worker.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError("daemon down")))
    assert instance._maybe_rebuild_openshell_image_after_update(tmp_path, "a", "b")["status"] == "failed"

    monkeypatch.setattr(worker.subprocess, "run", lambda *_a, **_k: _cp(2, stderr="build failed"))
    result = instance._maybe_rebuild_openshell_image_after_update(tmp_path, "a", "b")
    assert result == {"status": "failed", "tag": "custom:tag", "returncode": 2}

    monkeypatch.setattr(worker.subprocess, "run", lambda *_a, **_k: _cp())
    assert instance._maybe_rebuild_openshell_image_after_update(tmp_path, "a", "b") == {
        "status": "rebuilt", "tag": "custom:tag"
    }

    calls = []
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: calls.append((a, k)) or _cp())
    instance._maybe_rebuild_openshell_image_after_update(tmp_path, "a", "b")
    assert calls[0][0][0] == ["/bin/bash", str(image_builder)]
    assert calls[0][1]["env"]["MAC_SRC"] == str(tmp_path)
    assert calls[0][1]["env"]["OSH_IMAGE_TAG"] == "custom:tag"


def test_openshell_image_podman_mirror_success_and_failure(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    containerfile = tmp_path / worker._OPENSHELL_CONTAINERFILE_RELPATH
    containerfile.parent.mkdir(parents=True)
    containerfile.write_text("FROM scratch\n")
    image_builder = tmp_path / "deploy/openshell/build-runtime-image.sh"
    image_builder.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_IMAGE_SOURCE_SHA_FILE", str(tmp_path / "image-source-sha"))
    monkeypatch.setattr(worker, "_resolve_openshell_docker_bin", lambda: "/docker")
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/" + name)
    calls = []
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: calls.append((a, k)) or _cp())
    pipe = SimpleNamespace(close=lambda: calls.append(("close", {})))
    save = SimpleNamespace(stdout=pipe, wait=lambda **_k: calls.append(("wait", {})))
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_a, **_k: save)
    assert instance._maybe_rebuild_openshell_image_after_update(tmp_path, "a", "b")["status"] == "rebuilt"
    assert any(call[0] == "close" for call in calls)

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(OSError("save failed")))
    assert instance._maybe_rebuild_openshell_image_after_update(tmp_path, "a", "b")["status"] == "rebuilt"


def test_resolve_openshell_docker_bin_supports_launchd_paths(monkeypatch, tmp_path) -> None:
    configured = tmp_path / "configured-docker"
    configured.write_text("#!/bin/sh\n")
    configured.chmod(0o755)
    monkeypatch.setenv("MAC_OPENSHELL_DOCKER_BIN", str(configured))
    monkeypatch.setattr(worker.shutil, "which", lambda *_a: None)
    assert worker._resolve_openshell_docker_bin() == str(configured)

    monkeypatch.delenv("MAC_OPENSHELL_DOCKER_BIN")
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    assert worker._resolve_openshell_docker_bin() == "/usr/bin/docker"


def test_repo_update_service_restart_results(monkeypatch, tmp_path) -> None:
    instance = _instance(tmp_path)
    assert instance._run_repo_update_service_restarts({}) is None
    assert instance._run_repo_update_service_restarts({"service_restart_requested": True}) is None
    monkeypatch.setattr(worker, "_restart_systemd_service", lambda service: {"service": service, "status": "restarted"})
    result = instance._run_repo_update_service_restarts({
        "service_restart_requested": True,
        "restart_services": ["one.service"],
        "stream_id": "s",
    })
    assert result["status"] == "service_restarted"
    monkeypatch.setattr(worker, "_restart_systemd_service", lambda service: {"service": service, "status": "error"})
    result = instance._run_repo_update_service_restarts({
        "service_restart_requested": True,
        "restart_services": ["one.service"],
    })
    assert result["status"] == "service_restart_error"
