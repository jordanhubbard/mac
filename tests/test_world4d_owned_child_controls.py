"""Portable mocked custody/protocol controls. No new baseline assertions change."""

import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mac import world4d_owned_child as owned
from mac import world4d_service as service
from mac import world4d_client as client


def terminal(pid, status=0):
    return SimpleNamespace(si_pid=pid, si_code=os.CLD_EXITED, si_status=status)


class Selector:
    def __init__(self):
        self.records = {}
        self.events = []

    def register(self, fd, _events, data=None):
        self.records[fd] = data

    def unregister(self, fd):
        self.records.pop(fd)

    def select(self, _timeout):
        result, self.events = self.events, []
        return [(SimpleNamespace(data=self.records[fd]), 1) for fd in result]


class Kernel:
    def __init__(self, states):
        self.states = states
        self.sent = []
        self.groups = []
        self.reaped = []
        self.group_error = None

    def children(self):
        return set(self.states)

    def observe(self, pid):
        if pid not in self.states:
            raise ChildProcessError(errno.ECHILD, "not owned")
        return self.states[pid]

    def child_signal(self, pid, decide):
        if self.observe(pid) is None:
            signum = decide()
            self.sent.append((pid, signum))
            return signum
        return None

    def group_signal(self, pid, decide):
        self.observe(pid)
        signum = decide()
        self.groups.append((pid, signum))
        if self.group_error is not None:
            raise OSError(self.group_error, "forced fixture")

    def reap(self, pid):
        value = self.states.pop(pid)
        self.reaped.append(pid)
        return value

    def empty_wait(self):
        if self.states:
            raise owned.CustodyError("children remain")


@pytest.fixture
def keeper(monkeypatch):
    selector = Selector()
    monkeypatch.setattr(owned.selectors, "DefaultSelector", lambda: selector)
    k = owned._Keeper(100, 101, kernel=Kernel({200: None}), clock=lambda: 10.0)
    k.payload = 200
    k.retained = {200}
    k.phase = "running"
    k.group_proven = True
    k.send_status = Mock()
    k.event = Mock()
    monkeypatch.setattr(owned.os, "close", Mock())
    return k


@pytest.mark.parametrize("outcome", [None, errno.EPERM, errno.ESRCH])
def test_every_group_outcome_accounts_for_adopted_direct_children(keeper, outcome):
    keeper.kernel.states[201] = None
    keeper.kernel.group_error = outcome
    keeper.stop(10.0)
    keeper.signal_children(keeper.observe())
    assert keeper.kernel.sent == [(200, signal.SIGTERM), (201, signal.SIGTERM)]
    assert keeper.retained == {200, 201}


def test_foreign_pid_is_never_a_child_send(monkeypatch):
    kernel = owned._Kernel()
    monkeypatch.setattr(
        owned.os, "waitid", Mock(side_effect=ChildProcessError(errno.ECHILD, "not ours"))
    )
    send = Mock()
    monkeypatch.setattr(owned.os, "kill", send)
    with pytest.raises(ChildProcessError):
        kernel.child_signal(999, lambda: signal.SIGKILL)
    send.assert_not_called()


def test_terminal_child_is_not_a_signal_target(monkeypatch):
    kernel = owned._Kernel()
    monkeypatch.setattr(kernel, "observe", lambda pid: terminal(pid))
    send = Mock()
    monkeypatch.setattr(owned.os, "kill", send)
    kernel.child_signal(200, lambda: signal.SIGKILL)
    send.assert_not_called()


def test_wrong_waitid_identity_refuses_send(monkeypatch):
    monkeypatch.setattr(owned.os, "waitid", lambda *_args: terminal(999))
    send = Mock()
    monkeypatch.setattr(owned.os, "kill", send)
    with pytest.raises(owned.CustodyError):
        owned._Kernel().child_signal(200, lambda: signal.SIGKILL)
    send.assert_not_called()


def test_retained_pid_disappearance_cannot_be_reused(keeper):
    keeper.kernel.states = {201: None}
    with pytest.raises(owned.CustodyError, match="disappeared"):
        keeper.observe()
    assert not keeper.kernel.sent


def test_first_stop_is_global_and_force_never_adds_budget(keeper):
    keeper.stop(8.0)
    keeper.stop(9.0, force=True)
    assert keeper.first_stop == 8.0
    assert keeper.phase == "kill"
    assert keeper.first_stop + owned.TERM_SECONDS + owned.KILL_SECONDS == 18.0
    assert keeper.send_status.call_count == 1


def test_expired_budget_forbids_every_numeric_send(keeper):
    keeper.stop(0.0)
    with pytest.raises(owned.CustodyError, match="deadline"):
        keeper.signal_children(keeper.observe())
    assert not keeper.kernel.groups and not keeper.kernel.sent


def test_late_adoption_uses_same_kill_phase_and_deadline(keeper):
    keeper.stop(5.0)
    keeper.kernel.states[201] = None
    keeper.signal_children(keeper.observe())
    assert keeper.first_stop == 5.0
    assert all(sig == signal.SIGKILL for _pid, sig in keeper.kernel.sent)


def test_no_signal_after_final_reap_and_payload_status_is_integer(keeper):
    keeper.kernel.states = {200: terminal(200, 7), 201: terminal(201, 0)}
    assert keeper.final_reap(keeper.observe())
    assert keeper.kernel.reaped == [200, 201]
    keeper.send_status.assert_called_once_with({"state": "drained", "returncode": 7})
    with pytest.raises(owned.CustodyError, match="phase"):
        keeper.signal_children({200: None})
    assert not keeper.kernel.sent


def test_new_generation_prevents_final_reap(keeper):
    keeper.kernel.states = {200: terminal(200)}
    initial = keeper.observe()
    keeper.kernel.states[201] = None
    assert keeper.final_reap(initial) is False
    assert not keeper.reaping and not keeper.kernel.reaped


def test_echild_is_required_after_last_reap(keeper):
    keeper.kernel.states = {200: terminal(200)}
    keeper.kernel.empty_wait = Mock(side_effect=owned.CustodyError("ECHILD missing"))
    with pytest.raises(owned.CustodyError, match="ECHILD"):
        keeper.final_reap(keeper.observe())
    assert keeper.reaping
    keeper.send_status.assert_not_called()


def test_missing_payload_or_unknown_exit_mode_cannot_drain(keeper):
    with pytest.raises(owned.CustodyError):
        keeper.final_reap({201: terminal(201)})
    bad = terminal(200)
    bad.si_code = 999
    keeper.kernel.states[200] = bad
    with pytest.raises(owned.CustodyError):
        keeper.observe()
    keeper.send_status.assert_not_called()


def test_failure_is_sticky_but_closes_eof_watchers_once(keeper):
    keeper.fail(OSError(errno.EPERM, "private text"))
    keeper.fail(OSError(errno.EIO, "private text"))
    assert keeper.failed and keeper.force
    assert keeper.control is None and not keeper.selector.records
    keeper.send_status.assert_called_once_with({"state": "failed"})


def test_failure_notifications_cannot_escape_postlaunch_custody(keeper):
    keeper.send_status = Mock(side_effect=BrokenPipeError())
    keeper.event = Mock(side_effect=owned.CustodyError("sink failed"))
    keeper.fail(OSError(errno.EIO, "private text"))
    assert keeper.failed and keeper.first_stop == 10.0


def test_failed_cleanup_never_sends_false_drained(keeper):
    keeper.fail(OSError(errno.EIO, "private text"))
    keeper.kernel.states[200] = terminal(200)
    assert keeper.final_reap(keeper.observe())
    assert keeper.send_status.call_args_list == [(({"state": "failed"},), {})]


def test_child_signal_denial_disables_retries(keeper):
    keeper.stop(10.0)
    keeper.kernel.child_signal = Mock(side_effect=PermissionError(errno.EPERM, "denied"))
    with pytest.raises(PermissionError):
        keeper.signal_children(keeper.observe())
    assert keeper.signals_disabled
    with pytest.raises(owned.CustodyError):
        keeper.signal_children(keeper.observe())
    assert keeper.kernel.child_signal.call_count == 1


def parent_handle():
    status_r, status_w = os.pipe()
    control_r, control_w = os.pipe()
    os.set_blocking(status_r, False)
    os.set_blocking(control_w, False)
    process = SimpleNamespace(
        pid=999, returncode=None, poll=lambda: None, stdin=None, stdout=None, stderr=None
    )
    child = owned.OwnedChild(process, control_w, status_r)
    return child, (status_r, status_w, control_r, control_w)


def close_fds(descriptors):
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            pass


def test_close_then_real_fd_reuse_does_not_redirect_force():
    child, descriptors = parent_handle()
    reused = child._control
    other_r, other_w = os.pipe()
    try:
        child.request_stop(force=True)
        os.dup2(other_w, reused)
        child.request_stop(force=True)
        os.set_blocking(other_r, False)
        with pytest.raises(BlockingIOError):
            os.read(other_r, 1)
        os.fstat(reused)
    finally:
        close_fds((*descriptors, other_r, other_w, reused))


def test_concurrent_force_is_close_once(monkeypatch):
    child, descriptors = parent_handle()
    real_close = os.close
    closed = []

    def record_close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(owned.os, "close", record_close)
    try:
        threads = [
            threading.Thread(target=child.request_stop, kwargs={"force": True}) for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)
            assert not thread.is_alive()
        assert closed.count(descriptors[3]) == 1
    finally:
        monkeypatch.setattr(owned.os, "close", real_close)
        close_fds(descriptors)


def test_full_stop_pipe_escalates_by_owned_eof(monkeypatch):
    child, descriptors = parent_handle()
    real_write = os.write
    monkeypatch.setattr(owned.os, "write", Mock(side_effect=BlockingIOError(errno.EAGAIN, "full")))
    try:
        child.request_stop()
        assert child._control is None
        assert os.read(descriptors[2], 1) == b""
    finally:
        monkeypatch.setattr(owned.os, "write", real_write)
        close_fds(descriptors)


@pytest.mark.parametrize(
    "message",
    [
        {"state": "drained", "returncode": None},
        {"state": "drained", "returncode": True},
        {"state": "ready", "pid": True},
    ],
)
def test_malformed_drain_or_ready_refuses(message):
    child, descriptors = parent_handle()
    try:
        os.write(descriptors[1], owned._record(message))
        with pytest.raises(owned.CustodyError):
            child.poll()
    finally:
        close_fds(descriptors)


def test_observer_pid_reuse_never_becomes_stop_authority(monkeypatch):
    child, descriptors = parent_handle()
    send = Mock(side_effect=AssertionError("parent must never signal a PID"))
    monkeypatch.setattr(owned.os, "kill", send)
    monkeypatch.setattr(owned.os, "killpg", send)
    child._process.pid = 123456
    child._process.poll = lambda: 0
    try:
        child.request_stop(force=True)
        send.assert_not_called()
    finally:
        close_fds(descriptors)


def test_env_is_popen_env_and_private_config_not_helper_argv(monkeypatch):
    captured = {}
    duplicate = []
    monkeypatch.setattr(owned.sys, "platform", "linux")

    def portable_pipe():
        pair = os.pipe()
        for fd in pair:
            os.set_blocking(fd, False)
        return pair

    monkeypatch.setattr(owned, "_pipe", portable_pipe)

    class Writable:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def register(self, *_args):
            return None

        def select(self, _timeout):
            return [(None, None)]

    monkeypatch.setattr(owned.selectors, "DefaultSelector", Writable)

    def popen(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        duplicate.append(os.dup(int(argv[4])))
        os.write(int(argv[6]), owned._record({"state": "ready", "pid": 123}))
        return SimpleNamespace(pid=456, stdin=None, stdout=None, stderr=None, poll=lambda: None)

    monkeypatch.setattr(owned.subprocess, "Popen", popen)
    child = None
    try:
        child = owned.OwnedChild.start(
            ["payload", "PRIVATE_ARGUMENT"], env={"CANARY": "FAKE_SECRET"}
        )
        assert captured["kwargs"]["env"] == {"CANARY": "FAKE_SECRET"}
        assert "FAKE_SECRET" not in repr(captured["argv"])
        assert "PRIVATE_ARGUMENT" not in repr(captured["argv"])
        config = json.loads(os.read(duplicate[0], 4096))
        assert config["argv"] == ["payload", "PRIVATE_ARGUMENT"] and "env" not in config
        assert child.pid == 123 and child._process.pid == 456
    finally:
        if child is not None:
            with child._lock:
                child._force_locked()
                child._close_status_locked()
        close_fds(duplicate)


def test_second_pipe_allocation_failure_closes_first_pair(monkeypatch):
    monkeypatch.setattr(owned.sys, "platform", "linux")
    pair = os.pipe()
    calls = iter([pair, OSError(errno.EMFILE, "fixture")])

    def pipe():
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(owned, "_pipe", pipe)
    with pytest.raises(OSError):
        owned.OwnedChild.start(["payload"])
    for fd in pair:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_portable_allowed_killpg_stop_path_unchanged(monkeypatch):
    process = SimpleNamespace(pid=333, wait=Mock())
    group = Mock()
    monkeypatch.setattr(service.os, "killpg", group)
    service.stop_process(process)
    assert group.call_args_list == [((333, signal.SIGTERM), {}), ((333, signal.SIGKILL), {})]
    assert process.wait.call_count == 2


def test_client_nonlinux_rpc_uses_original_subprocess_run(monkeypatch):
    monkeypatch.setattr(client.sys, "platform", "darwin")
    run = Mock(return_value=subprocess.CompletedProcess(["rpc"], 0, '{"id":1,"result":4}\n', ""))
    monkeypatch.setattr(client.subprocess, "run", run)
    assert client.rpc(["rpc"], "method") == 4
    assert run.call_args.kwargs["timeout"] == 30


def test_linux_wait_modes_include_clone_children_without_runtime_clone_claim(monkeypatch):
    calls = []

    def waitid(kind, pid, options):
        calls.append((kind, pid, options))
        return terminal(pid)

    monkeypatch.setattr(owned.os, "waitid", waitid)
    kernel = owned._Kernel()
    kernel.observe(200)
    kernel.reap(200)
    assert all(options & owned.WALL for _kind, _pid, options in calls)
    assert calls[0][2] & os.WNOWAIT
    assert not calls[1][2] & os.WNOWAIT


def test_final_echild_none_or_other_errno_is_not_success(monkeypatch):
    monkeypatch.setattr(owned.os, "waitid", lambda *_a: None)
    with pytest.raises(owned.CustodyError):
        owned._Kernel().empty_wait()
    monkeypatch.setattr(
        owned.os, "waitid", Mock(side_effect=ChildProcessError(errno.EPERM, "fixture"))
    )
    with pytest.raises(ChildProcessError):
        owned._Kernel().empty_wait()


def test_status_fd_reuse_after_drain_is_not_read(monkeypatch):
    child, descriptors = parent_handle()
    old = child._status
    other_r, other_w = os.pipe()
    try:
        os.write(
            descriptors[1],
            owned._record({"state": "ready", "pid": 200})
            + owned._record({"state": "drained", "returncode": 0}),
        )
        assert child.poll() == 0
        assert child._status is None
        os.dup2(other_r, old)
        os.write(other_w, b"not status data")
        assert child.poll() == 0
        assert os.read(old, 15) == b"not status data"
    finally:
        close_fds((*descriptors, other_r, other_w, old))


def test_malformed_status_closes_stop_authority_before_error():
    child, descriptors = parent_handle()
    try:
        os.write(descriptors[1], b"not-json\n")
        with pytest.raises(owned.CustodyError):
            child.poll()
        assert child._control is None and child._status is None
    finally:
        close_fds(descriptors)


def test_ready_failure_closes_every_pipe_and_payload_stream(monkeypatch):
    monkeypatch.setattr(owned.sys, "platform", "linux")
    allocated = []

    def pipe():
        pair = os.pipe()
        for fd in pair:
            os.set_blocking(fd, False)
        allocated.extend(pair)
        return pair

    monkeypatch.setattr(owned, "_pipe", pipe)
    streams = [Mock(), Mock(), Mock()]

    def popen(argv, **_kwargs):
        os.write(int(argv[6]), owned._record({"state": "failed"}))
        return SimpleNamespace(
            stdin=streams[0], stdout=streams[1], stderr=streams[2], poll=lambda: None
        )

    monkeypatch.setattr(owned.subprocess, "Popen", popen)
    with pytest.raises(owned.CustodyError, match="configuration closed before READY"):
        owned.OwnedChild.start(["payload"])
    for stream in streams:
        stream.close.assert_called_once()
    for fd in allocated:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_stop_at_deadline_returns_failure_without_extra_wait(monkeypatch):
    child, descriptors = parent_handle()
    now = [20.0]
    monkeypatch.setattr(owned.time, "monotonic", lambda: now[0])
    child._first_stop = 10.0
    try:
        with pytest.raises(owned.CustodyError, match="deadline"):
            child.stop()
        assert child._failed and child._control is None and child._status is None
    finally:
        close_fds(descriptors)


def test_observed_terminal_count_and_payload_status_are_actual(keeper):
    keeper.kernel.states = {200: None, 201: terminal(201)}
    keeper.observe()
    assert keeper.payload_status is None
    assert keeper.observed_terminal == {201}
    keeper.kernel.states[200] = terminal(200, 0)
    keeper.observe()
    assert keeper.payload_status == 0 and keeper.observed_terminal == {200, 201}


def test_primitive_errno_receipts_are_fixed_and_deduplicated(keeper):
    failure = Mock(side_effect=PermissionError(errno.EPERM, "private text"))
    for _ in range(2):
        with pytest.raises(PermissionError):
            keeper.primitive("observe", failure)
    assert keeper.event.call_args_list == [(("observe",), {"error": errno.EPERM})]


def test_linux_storage_routes_to_guardian_with_real_env_argument_only(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "OwnedChild", owned.OwnedChild)
    for suffix, value in [
        ("HOST", "host"),
        ("PORT", "5432"),
        ("DB", "world"),
        ("USER", "user"),
        ("PASS", "FAKE_CANARY"),
    ]:
        monkeypatch.setenv("W4D_" + suffix, value)
    run = Mock(return_value=subprocess.CompletedProcess(["psql"], 0, "ok\n", ""))
    monkeypatch.setattr(service.OwnedChild, "run", run)
    assert service.StorageGuard().sql("SELECT 1") == "ok"
    assert run.call_args.kwargs["env"]["PGPASSWORD"] == "FAKE_CANARY"
    assert "FAKE_CANARY" not in repr(run.call_args.args)
    assert run.call_args.kwargs["input"] == "SELECT 1"


def test_world_linux_launch_reuses_liveness_only_in_guardian(tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "OwnedChild", owned.OwnedChild)
    handle = SimpleNamespace(pid=200)
    start = Mock(return_value=handle)
    monkeypatch.setattr(service.OwnedChild, "start", start)
    world = service.World(
        {"name": "fleet", "port": 39002},
        {"root": str(tmp_path), "python": "python", "source": str(tmp_path), "monitor_port": 39001},
    )
    try:
        world.start()
        argv = start.call_args.args[0]
        assert argv[3:5] == ["_guardian-child", "-1"]
        assert type(start.call_args.kwargs["parent_reader"]) is int
        assert start.call_args.kwargs["telemetry_dir"] == str(tmp_path)
    finally:
        world.log.close()
        os.close(world.parent_writer)


def test_snapshot_stop_is_idempotent_for_already_drained_handle(tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "OwnedChild", owned.OwnedChild)
    child = object.__new__(owned.OwnedChild)
    child.communicate = Mock(return_value=("snapshot", ""))
    child.poll = lambda: 0
    child.stop = Mock(return_value=0)
    monkeypatch.setattr(service.OwnedChild, "start", Mock(return_value=child))
    assert (
        service.render_snapshot(
            {
                "python": "python",
                "source": str(tmp_path),
                "root": str(tmp_path),
                "worlds": [{"name": "fleet", "port": 39002}],
            }
        )
        == "snapshot"
    )
    child.communicate.assert_called_once_with(timeout=45)
    child.stop.assert_called_once()


def test_linux_rpc_routes_to_guardian_without_operator_identity(monkeypatch):
    monkeypatch.setattr(client.sys, "platform", "linux")
    monkeypatch.setattr(client, "OwnedChild", owned.OwnedChild)
    run = Mock(
        return_value=subprocess.CompletedProcess(["rpc"], 0, '{"id":1,"result":{"ok":true}}\n', "")
    )
    monkeypatch.setattr(client.OwnedChild, "run", run)
    assert client.rpc(["rpc"], "method") == {"ok": True}
    assert run.call_args.kwargs["timeout"] == 30
    assert json.loads(run.call_args.kwargs["input"])["method"] == "method"


def test_valid_ready_cannot_clear_sticky_failed_status():
    child, descriptors = parent_handle()
    try:
        os.write(
            descriptors[1],
            owned._record({"state": "failed"}) + owned._record({"state": "ready", "pid": 200}),
        )
        with pytest.raises(owned.CustodyError):
            child.poll()
        assert child._failed and not child._ready
    finally:
        close_fds(descriptors)


def test_status_after_drain_is_not_a_second_valid_receipt():
    child, descriptors = parent_handle()
    try:
        os.write(
            descriptors[1],
            owned._record({"state": "ready", "pid": 200})
            + owned._record({"state": "drained", "returncode": 0})
            + owned._record({"state": "stopping", "t0": 0}),
        )
        with pytest.raises(owned.CustodyError):
            child.poll()
        assert child._failed
    finally:
        close_fds(descriptors)


def test_retained_child_budget_forces_controlled_shutdown_without_reaper(keeper, monkeypatch):
    monkeypatch.setattr(owned, "MAX_CHILDREN", 4)
    keeper.kernel.states = {200: None, 201: None}
    keeper.launch = lambda _config: None

    def finish_child(pid, decide):
        signum = decide()
        keeper.kernel.sent.append((pid, signum))
        keeper.kernel.states[pid] = terminal(pid)
        return signum

    keeper.kernel.child_signal = finish_child
    assert keeper.run({}) == 0
    assert keeper.force
    assert keeper.kernel.sent == [(200, signal.SIGKILL), (201, signal.SIGKILL)]
    assert keeper.reaping and keeper.drain_proven


def test_interrupted_startup_without_session_proof_uses_only_own_children(keeper):
    keeper.group_proven = False
    keeper.stop(force=True)
    keeper.signal_children(keeper.observe())
    assert not keeper.kernel.groups
    assert keeper.kernel.sent == [(200, signal.SIGKILL)]


@pytest.fixture(autouse=True)
def mocked_linux_symbols_when_host_lacks_them(monkeypatch):
    # Portable control tests never invoke these missing Linux primitives. Mock
    # symbols let an actual Windows interpreter import/test the safety logic;
    # an unmocked use fails rather than silently emulating a kernel operation.
    if owned.fcntl is None:
        monkeypatch.setattr(
            owned,
            "fcntl",
            SimpleNamespace(
                F_DUPFD_CLOEXEC=0,
                fcntl=Mock(side_effect=AssertionError("unmocked Linux descriptor operation")),
            ),
        )
    for name, value in {
        "CLD_EXITED": 1,
        "CLD_KILLED": 2,
        "CLD_DUMPED": 3,
        "P_ALL": 0,
        "P_PID": 1,
        "WEXITED": 4,
        "WNOHANG": 1,
        "WNOWAIT": 0x01000000,
    }.items():
        if not hasattr(os, name):
            monkeypatch.setattr(os, name, value, raising=False)
    for name in ("waitid", "killpg"):
        if not hasattr(os, name):
            monkeypatch.setattr(
                os,
                name,
                Mock(side_effect=AssertionError("unmocked Linux operation")),
                raising=False,
            )
    if not hasattr(signal, "SIGKILL"):
        monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)


def test_nonlinux_imports_do_not_require_fcntl_or_guardian(monkeypatch):
    import builtins
    import runpy

    actual_import = builtins.__import__
    imported_helpers = []

    def checked_import(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("fcntl deliberately unavailable")
        if name.endswith("world4d_owned_child"):
            imported_helpers.append(name)
        return actual_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.syspath_prepend(str(Path(service.__file__).parent))
    helper = runpy.run_path(owned.__file__, run_name="private_import_control")
    assert helper["fcntl"] is None
    imported_helpers.clear()
    imported_client = runpy.run_path(client.__file__, run_name="private_client_import")
    imported_service = runpy.run_path(service.__file__, run_name="private_service_import")
    assert imported_client["OwnedChild"] is None and imported_service["OwnedChild"] is None
    assert not imported_helpers


def test_communicate_timeout_preserves_original_cmd_and_partial_streams(monkeypatch):
    child, descriptors = parent_handle()
    child._args = ["payload", "PRIVATE_ARGUMENT"]
    now = [0.0]
    calls = []
    monkeypatch.setattr(owned.time, "monotonic", lambda: now[0])

    def communicate(*, input, timeout):
        calls.append((input, timeout))
        if len(calls) == 1:
            now[0] = 1.0
            raise subprocess.TimeoutExpired(
                ["helper-fds"], timeout, output=b"partial-out", stderr=b"partial-err"
            )
        child._drained = True
        child._exit = 0
        return ("partial-out", "partial-err")

    child._process.communicate = communicate
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            child.communicate(input="request", timeout=1)
        assert caught.value.cmd == ["payload", "PRIVATE_ARGUMENT"]
        assert caught.value.timeout == 1
        assert caught.value.output == b"partial-out" and caught.value.stderr == b"partial-err"
        assert calls[0][0] == "request" and calls[1][0] is None
        assert all(interval <= owned.POLL_SECONDS for _input, interval in calls)
    finally:
        close_fds(descriptors)


def test_long_or_none_communicate_observes_failure_without_waiting_for_eof(monkeypatch):
    for timeout in (None, 120):
        child, descriptors = parent_handle()
        child._args = ["payload"]
        now = [0.0]
        calls = []
        monkeypatch.setattr(owned.time, "monotonic", lambda: now[0])

        def communicate(*, input, timeout):
            calls.append(timeout)
            now[0] += timeout
            os.write(descriptors[1], owned._record({"state": "failed"}))
            raise subprocess.TimeoutExpired(["helper-fds"], timeout, output=b"prefix")

        child._process.communicate = communicate
        try:
            with pytest.raises(owned.CustodyError):
                child.communicate(timeout=timeout)
            assert calls == [owned.POLL_SECONDS]
            assert now[0] == owned.POLL_SECONDS
        finally:
            close_fds(descriptors)


def test_status_refresh_shortens_deadline_before_communicate_slice(monkeypatch):
    child, descriptors = parent_handle()
    child._first_stop = 20.0
    monkeypatch.setattr(owned.time, "monotonic", lambda: 20.0)
    reads = []

    def refresh():
        reads.append(1)
        if len(reads) == 2:
            child._first_stop = 5.0

    child._read_locked = refresh
    child._process.communicate = Mock(side_effect=AssertionError("expired budget must not wait"))
    try:
        with pytest.raises(owned.CustodyError, match="deadline"):
            child.communicate(timeout=None)
        child._process.communicate.assert_not_called()
    finally:
        close_fds(descriptors)


def test_healthy_communicate_keeps_requested_operation_budget(monkeypatch):
    child, descriptors = parent_handle()
    child._args = ["payload"]
    now = [0.0]
    calls = []
    monkeypatch.setattr(owned.time, "monotonic", lambda: now[0])

    def communicate(*, input, timeout):
        calls.append(timeout)
        now[0] += timeout
        if len(calls) < 3:
            raise subprocess.TimeoutExpired(["helper-fds"], timeout, output=b"prefix")
        child._drained = True
        child._exit = 0
        return ("complete", "stderr")

    child._process.communicate = communicate
    try:
        assert child.communicate(timeout=30) == ("complete", "stderr")
        assert child._first_stop is None
        assert len(calls) == 3 and now[0] < 30
    finally:
        close_fds(descriptors)


def test_child_observation_crossing_term_boundary_uses_fresh_kill(keeper, monkeypatch):
    now = [10.0]
    keeper.clock = lambda: now[0]
    keeper.stop(10.0)
    keeper.group_proven = False
    kernel = owned._Kernel()

    def delayed(_pid):
        now[0] = 15.01
        return None

    monkeypatch.setattr(kernel, "observe", delayed)
    keeper.kernel.child_signal = kernel.child_signal
    send = Mock()
    monkeypatch.setattr(owned.os, "kill", send)
    keeper.signal_children(keeper.observe())
    send.assert_called_once_with(200, signal.SIGKILL)
    assert keeper.sent[200] == "kill" and keeper.first_stop == 10.0


def test_child_observation_crossing_total_deadline_never_sends(keeper, monkeypatch):
    now = [10.0]
    keeper.clock = lambda: now[0]
    keeper.stop(10.0)
    keeper.group_proven = False
    kernel = owned._Kernel()

    def delayed(_pid):
        now[0] = 20.01
        return None

    monkeypatch.setattr(kernel, "observe", delayed)
    keeper.kernel.child_signal = kernel.child_signal
    send = Mock()
    monkeypatch.setattr(owned.os, "kill", send)
    with pytest.raises(owned.CustodyError, match="deadline"):
        keeper.signal_children(keeper.observe())
    send.assert_not_called()


def test_group_observation_crossing_term_boundary_uses_fresh_kill(keeper, monkeypatch):
    now = [10.0]
    keeper.clock = lambda: now[0]
    keeper.stop(10.0)
    kernel = owned._Kernel()

    def delayed(_pid):
        now[0] = 15.01
        return None

    monkeypatch.setattr(kernel, "observe", delayed)
    keeper.kernel.group_signal = kernel.group_signal
    send = Mock()
    monkeypatch.setattr(owned.os, "killpg", send)
    keeper.signal_children(keeper.observe())
    send.assert_called_once_with(200, signal.SIGKILL)
    assert keeper.group_sent == {"kill"}


def test_group_observation_crossing_total_deadline_never_sends(keeper, monkeypatch):
    now = [10.0]
    keeper.clock = lambda: now[0]
    keeper.stop(10.0)
    kernel = owned._Kernel()

    def delayed(_pid):
        now[0] = 20.01
        return None

    monkeypatch.setattr(kernel, "observe", delayed)
    keeper.kernel.group_signal = kernel.group_signal
    send = Mock()
    monkeypatch.setattr(owned.os, "killpg", send)
    with pytest.raises(owned.CustodyError, match="deadline"):
        keeper.signal_children(keeper.observe())
    send.assert_not_called()


def test_reap_transition_during_observation_forbids_numeric_send(keeper, monkeypatch):
    keeper.stop(10.0)
    keeper.group_proven = False
    kernel = owned._Kernel()

    def transition(_pid):
        keeper.reaping = True
        return None

    monkeypatch.setattr(kernel, "observe", transition)
    keeper.kernel.child_signal = kernel.child_signal
    send = Mock()
    monkeypatch.setattr(owned.os, "kill", send)
    with pytest.raises(owned.CustodyError, match="phase"):
        keeper.signal_children(keeper.observe())
    send.assert_not_called()


def test_reap_crossing_total_deadline_cannot_emit_late_drained(keeper):
    now = [10.0]
    keeper.clock = lambda: now[0]
    keeper.stop(10.0)
    keeper.kernel.states = {200: terminal(200)}
    old = keeper.kernel.empty_wait

    def delayed_echild():
        old()
        now[0] = 20.01

    keeper.kernel.empty_wait = delayed_echild
    with pytest.raises(owned.CustodyError, match="deadline"):
        keeper.final_reap(keeper.observe())
    assert keeper.drain_proven and keeper.reaping
    assert not any(
        call.args[0].get("state") == "drained" for call in keeper.send_status.call_args_list
    )


def test_final_echild_event_is_fixed_and_requires_proven_zero_custody(keeper, monkeypatch):
    rows = []

    def write(_fd, raw):
        rows.append(json.loads(raw))
        return len(raw)

    monkeypatch.setattr(owned.os, "write", write)
    keeper.telemetry = 102
    keeper.phase = "reap"
    with pytest.raises(owned.CustodyError, match="unproven final ECHILD"):
        type(keeper).event(keeper, "echild")
    keeper.reaping = True
    keeper.drain_proven = True
    keeper.retained.clear()
    keeper.observed_terminal.clear()
    keeper.payload_status = 0
    type(keeper).event(keeper, "echild")
    assert len(rows) == 1
    assert rows[0]["operation"] == "echild" and rows[0]["phase"] == "reap"
    assert type(rows[0]["children"]) is int and rows[0]["children"] == 0
    assert type(rows[0]["terminal"]) is int and rows[0]["terminal"] == 0
    assert rows[0]["payload_status_present"] is True
    assert set(rows[0]) == {
        "phase",
        "operation",
        "errno",
        "children",
        "terminal",
        "deadline",
        "payload_status_present",
    }


def test_final_echild_telemetry_write_error_never_sends_drained(keeper, monkeypatch):
    keeper.kernel.states = {200: terminal(200)}
    observed = keeper.observe()
    keeper.telemetry = 102

    def event(operation, **kwargs):
        return type(keeper).event(keeper, operation, **kwargs)

    keeper.event = event
    monkeypatch.setattr(
        owned.os, "write", Mock(side_effect=OSError(errno.EIO, "fixture sink failure"))
    )
    with pytest.raises(OSError):
        keeper.final_reap(observed)
    assert not any(
        call.args[0].get("state") == "drained" for call in keeper.send_status.call_args_list
    )
    assert keeper.reaping  # permanent no-more-signals latch survives telemetry failure
    with pytest.raises(owned.CustodyError):
        keeper.signal_children({200: None})
