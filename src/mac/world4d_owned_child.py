"""Linux-only owned process guardian; also copied as a standalone stdlib module.

The observing parent owns pipes, never a numeric signal target. The dedicated
single-thread subreaper retains every child unreaped until its final drain.
Candidate source: execution requires independent exact-source review.
"""

from __future__ import annotations

import ctypes
import errno

try:
    import fcntl
except ImportError:
    fcntl = None
import json
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid

TERM_SECONDS = 5.0
KILL_SECONDS = 5.0
POLL_SECONDS = 0.02
START_SECONDS = 5.0
WALL = 0x40000000
MAX_CHILDREN = 4096
MAX_RECORD = 1024
MAX_CONFIG = 64 * 1024 * 1024
_HELPER = Path(__file__).resolve()


class CustodyError(RuntimeError):
    """No verified drain: callers must not restart onto possibly occupied ports."""


def _integer(value):
    return type(value) is int


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _record(data):
    return json.dumps(data, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def _pipe():
    pair = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    held = set(pair)
    result = []
    try:
        for descriptor in pair:
            if descriptor < 3:
                duplicate = fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 3)
                held.add(duplicate)
                held.remove(descriptor)
                os.close(descriptor)
                descriptor = duplicate
            result.append(descriptor)
        return tuple(result)
    except BaseException:
        for descriptor in held:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


class OwnedChild:
    """Popen-compatible observation, with pipe-only, close-once stop authority.

    No parent-side kill/killpg exists, including launch and protocol failures.
    A pipe/guardian failure is explicit; the handle never fabricates success.
    """

    def __init__(self, process, control, status, args=None):
        self._process = process
        self._args = args
        self._control = control
        self._status = status
        self._lock = threading.RLock()
        self._buffer = b""
        self._ready = False
        self._drained = False
        self._failed = False
        self._exit = None
        self._payload_pid = None
        self._first_stop = None
        self._stop_sent = False
        self._status_eof = False
        self.telemetry_path = None

    @classmethod
    def start(
        cls,
        argv,
        *,
        env=None,
        cwd=None,
        stdin=None,
        stdout=None,
        stderr=None,
        text=False,
        parent_reader=None,
        telemetry_dir=None,
    ):
        if not sys.platform.startswith("linux") or fcntl is None:
            raise CustodyError("owned guardian requires Linux descriptor control")
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or not all(isinstance(x, str) for x in argv)
        ):
            raise ValueError("owned payload requires an argument list")
        payload = _record({"argv": list(argv), "cwd": os.fspath(cwd) if cwd is not None else None})
        # Private bounded pipe transport; payload args/env NEVER go on helper argv.
        if len(payload) > MAX_CONFIG:
            raise ValueError("owned payload configuration too large")
        descriptors = set()
        process = child = None
        try:
            config_r, config_w = _pipe()
            descriptors.update((config_r, config_w))
            control_r, control_w = _pipe()
            descriptors.update((control_r, control_w))
            status_r, status_w = _pipe()
            descriptors.update((status_r, status_w))
            live = None
            if parent_reader is not None:
                live = fcntl.fcntl(parent_reader, fcntl.F_DUPFD_CLOEXEC, 3)
                descriptors.add(live)
            telemetry = None
            telemetry_path = None
            if telemetry_dir is not None:
                directory = Path(telemetry_dir)
                telemetry_path = directory / ("guardian-" + uuid.uuid4().hex + ".jsonl")
                telemetry = os.open(
                    telemetry_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
                if telemetry < 3:
                    duplicate = fcntl.fcntl(telemetry, fcntl.F_DUPFD_CLOEXEC, 3)
                    os.close(telemetry)
                    telemetry = duplicate
                descriptors.add(telemetry)
                if not stat.S_ISREG(os.fstat(telemetry).st_mode):
                    raise CustodyError("invalid guardian telemetry sink")
            passed = (
                (config_r, control_r, status_w)
                + (() if live is None else (live,))
                + (() if telemetry is None else (telemetry,))
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-ISu",
                    str(_HELPER),
                    "_keeper",
                    str(config_r),
                    str(control_r),
                    str(status_w),
                    str(live if live is not None else -1),
                    str(telemetry if telemetry is not None else -1),
                ],
                env=env,
                cwd=cwd,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                text=text,
                pass_fds=passed,
                start_new_session=True,
            )
            child = cls(process, control_w, status_r, argv)
            child.telemetry_path = telemetry_path
            descriptors.difference_update((control_w, status_r))
            for descriptor in passed:
                os.close(descriptor)
                descriptors.remove(descriptor)
            until = time.monotonic() + START_SECONDS
            with selectors.DefaultSelector() as writable:
                writable.register(config_w, selectors.EVENT_WRITE)
                offset = 0
                while offset < len(payload):
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        raise CustodyError("guardian configuration deadline")
                    if writable.select(min(POLL_SECONDS, remaining)):
                        try:
                            offset += os.write(config_w, payload[offset : offset + 4096])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            raise CustodyError(
                                "guardian configuration closed before READY"
                            ) from None
            os.close(config_w)
            descriptors.remove(config_w)
            while time.monotonic() < until:
                with child._lock:
                    child._read_locked()
                    if child._failed or child._status_eof:
                        raise CustodyError("guardian did not become ready")
                    if child._ready:
                        return child
                time.sleep(POLL_SECONDS)
            raise CustodyError("guardian startup deadline")
        except BaseException:
            if child is not None:
                # EOF is our only recovery authority. Keeper retains custody.
                with child._lock:
                    for close in (child._force_locked, child._close_status_locked):
                        try:
                            close()
                        except OSError:
                            pass
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                try:
                    process.poll()  # observation only; never numeric recovery
                except OSError:
                    pass
            raise
        finally:
            # Linux close errors must not make us close a now-recycled FD or
            # abandon cleanup of the remaining, still-owned descriptors.
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @property
    def pid(self):
        # Preserve Popen's caller-visible PAYLOAD identity. It is diagnostic,
        # never stop authority; the private Popen observes the keeper itself.
        return self._payload_pid

    @property
    def args(self):
        # Original argv remains parent-local: caller-visible error semantics do
        # not put it on the helper command line/status/telemetry channels.
        return self._args

    @property
    def returncode(self):
        return self.poll()

    def _close_status_locked(self):
        if self._status is not None:
            descriptor, self._status = self._status, None
            os.close(descriptor)

    def _force_locked(self):
        if self._control is not None:
            descriptor, self._control = self._control, None
            os.close(descriptor)

    def _read_locked(self):
        if self._status is None:
            return
        try:
            data = os.read(self._status, MAX_RECORD + 1)
        except BlockingIOError:
            return
        if not data:
            self._status_eof = True
            self._close_status_locked()
            if not self._drained:
                self._failed = True
            return
        self._buffer += data
        if len(self._buffer) > MAX_RECORD:
            self._failed = True
            raise CustodyError("invalid guardian status")
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeError):
                self._failed = True
                raise CustodyError("invalid guardian status") from None
            if type(message) is not dict:
                raise CustodyError("invalid guardian status")
            if self._drained:
                self._failed = True
                raise CustodyError("status after guardian drain")
            kind = message.get("state")
            if (
                kind == "ready"
                and set(message) == {"state", "pid"}
                and _integer(message["pid"])
                and 0 < message["pid"] <= 2_147_483_647
                and not self._ready
                and not self._failed
            ):
                self._payload_pid = message["pid"]
                self._ready = True
            elif kind == "stopping" and set(message) == {"state", "t0"} and _time(message["t0"]):
                self._first_stop = (
                    message["t0"]
                    if self._first_stop is None
                    else min(self._first_stop, message["t0"])
                )
            elif (
                kind == "drained"
                and set(message) == {"state", "returncode"}
                and _integer(message["returncode"])
                and -signal.NSIG < message["returncode"] <= 255
                and self._ready
                and not self._failed
            ):
                self._drained, self._exit = True, message["returncode"]
                self._force_locked()
            elif kind == "failed" and set(message) == {"state"}:
                self._failed = True
            else:
                self._failed = True
                raise CustodyError("invalid guardian status")
        if self._drained:
            if self._buffer:
                self._failed = True
                raise CustodyError("invalid guardian terminal status")
            self._close_status_locked()

    def poll(self):
        with self._lock:
            try:
                self._read_locked()
            except BaseException:
                self._failed = True
                self._force_locked()
                self._close_status_locked()
                raise
            self._process.poll()
            if self._failed or (self._process.returncode is not None and not self._drained):
                self._force_locked()
                self._close_status_locked()
                raise CustodyError("guardian custody failed")
            return self._exit if self._drained else None

    def request_stop(self, *, force=False):
        with self._lock:
            try:
                self._read_locked()
            except BaseException:
                self._failed = True
                self._force_locked()
                self._close_status_locked()
                raise
            if self._drained:
                return
            if self._first_stop is None:
                self._first_stop = time.monotonic()
            if force:
                self._force_locked()  # EOF cannot be lost to a full control pipe
            elif not self._stop_sent and self._control is not None:
                self._stop_sent = True
                data = _record({"op": "stop", "t0": self._first_stop})
                try:
                    if os.write(self._control, data) != len(data):
                        self._force_locked()
                except BlockingIOError:
                    self._force_locked()
                except BrokenPipeError:
                    self._force_locked()
                    self._failed = True
            if self._failed:
                raise CustodyError("guardian custody failed")

    def stop(self, *, force=False):
        self.request_stop(force=force)
        while True:
            code = self.poll()
            if code is not None:
                return code
            with self._lock:
                final = self._first_stop + TERM_SECONDS + KILL_SECONDS
            remaining = final - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self._force_locked()
                    self._close_status_locked()
                    self._failed = True
                raise CustodyError("guardian drain deadline")
            time.sleep(min(POLL_SECONDS, remaining))

    def wait(self, timeout=None):
        until = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            if until is not None and time.monotonic() >= until:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(
                POLL_SECONDS
                if until is None
                else min(POLL_SECONDS, max(0, until - time.monotonic()))
            )

    def _close_io(self):
        # Only our Popen PIPE wrappers are exposed here. Nonblocking close
        # prevents a TextIO flush from adding an unbounded cleanup wait.
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None and not stream.closed:
                try:
                    os.set_blocking(stream.fileno(), False)
                except OSError:
                    pass
                stream.close()

    def communicate(self, input=None, timeout=None):
        operation_deadline = None if timeout is None else time.monotonic() + timeout
        started = False
        timed_out = False
        latest_output = latest_stderr = None
        timeout_output = timeout_stderr = None
        while True:
            # Observe failure even when the payload keeps stdout open forever
            # or the caller requested communicate(None/a long timeout).
            try:
                self.poll()
            except BaseException:
                self._close_io()
                raise
            now = time.monotonic()
            if not timed_out and operation_deadline is not None and now >= operation_deadline:
                timed_out = True
                timeout_output, timeout_stderr = latest_output, latest_stderr
                try:
                    self.request_stop()
                except BaseException:
                    self._close_io()
                    raise
            with self._lock:
                # A just-arrived STOPPING can reveal an EARLIER keeper latch.
                # Update first, then derive the remaining shared deadline.
                try:
                    self._read_locked()
                except BaseException:
                    self._force_locked()
                    self._close_status_locked()
                    self._close_io()
                    raise
                final = (
                    None
                    if self._first_stop is None
                    else self._first_stop + TERM_SECONDS + KILL_SECONDS
                )
                failed = self._failed
            if failed:
                with self._lock:
                    self._force_locked()
                    self._close_status_locked()
                self._close_io()
                raise CustodyError("guardian custody failed")
            now = time.monotonic()
            if final is not None and now >= final:
                with self._lock:
                    self._force_locked()
                    self._close_status_locked()
                    self._failed = True
                self._close_io()
                raise CustodyError("guardian drain deadline")
            interval = POLL_SECONDS
            if final is not None:
                interval = min(interval, max(0, final - now))
            if not timed_out and operation_deadline is not None:
                interval = min(interval, max(0, operation_deadline - now))
            try:
                output = self._process.communicate(
                    input=input if not started else None, timeout=interval
                )
            except subprocess.TimeoutExpired as exc:
                started = True
                if exc.output is not None:
                    latest_output = exc.output
                if exc.stderr is not None:
                    latest_stderr = exc.stderr
                if timed_out and self._drained:
                    # All descendants are already drained. No extra wait for a
                    # keeper/IO anomaly; preserve the ORIGINAL timeout snapshot.
                    self._close_io()
                    raise subprocess.TimeoutExpired(
                        self.args, timeout, output=timeout_output, stderr=timeout_stderr
                    ) from None
                continue
            try:
                self.poll()
            except BaseException:
                self._close_io()
                raise
            if not self._drained:
                raise CustodyError("guardian exited without drain")
            if timed_out:
                raise subprocess.TimeoutExpired(
                    self.args, timeout, output=timeout_output, stderr=timeout_stderr
                ) from None
            return output

    @classmethod
    def run(cls, argv, *, input=None, timeout=None, capture_output=False, text=False, **kwargs):
        child = cls.start(
            argv,
            stdin=subprocess.PIPE if input is not None else None,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=text,
            **kwargs,
        )
        try:
            out, err = child.communicate(input=input, timeout=timeout)
            return subprocess.CompletedProcess(argv, child.returncode, out, err)
        finally:
            child.stop()


class _Kernel:
    """Only the keeper calls this. WNOWAIT pins direct children until final reap."""

    def admit(self):
        import resource

        if threading.active_count() != 1 or len(os.listdir("/proc/self/task")) != 1:
            raise CustodyError("guardian requires exclusive single-thread custody")
        self.payload_sigchld = signal.getsignal(signal.SIGCHLD)
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.prctl.restype = ctypes.c_int
        # Documented libc prctl interface; never a raw syscall/filter bypass.
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "guardian admission failed")
        enabled = ctypes.c_int()
        if libc.prctl(37, ctypes.addressof(enabled), 0, 0, 0) != 0 or enabled.value != 1:
            raise CustodyError("guardian admission unproven")
        limit, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if not _integer(limit) or not 3 <= limit <= 1_048_576:
            raise CustodyError("guardian descriptor bound unavailable")
        self.fd_limit = limit
        if self.children():
            raise CustodyError("guardian did not start with empty custody")
        try:
            os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT | WALL)
        except ChildProcessError as exc:
            if exc.errno != errno.ECHILD:
                raise
        else:
            raise CustodyError("guardian initial ECHILD unproven")

    def children(self):
        # Fixed own-kernel path, single thread, no scan of peer processes.
        descriptor = os.open(f"/proc/self/task/{os.getpid()}/children", os.O_RDONLY | os.O_CLOEXEC)
        try:
            chunks = []
            size = 0
            while True:
                chunk = os.read(descriptor, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_CHILDREN * 12:
                    raise CustodyError("guardian child bound exceeded")
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
        if len(raw) > MAX_CHILDREN * 12:
            raise CustodyError("guardian child bound exceeded")
        fields = raw.split()
        if len(fields) > MAX_CHILDREN or any(not field.isdigit() for field in fields):
            raise CustodyError("guardian child membership malformed")
        result = {int(field) for field in fields}
        if len(result) != len(fields) or any(pid <= 0 or pid == os.getpid() for pid in result):
            raise CustodyError("guardian child membership malformed")
        return result

    def observe(self, pid):
        result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT | WALL)
        if result is not None and result.si_pid != pid:
            raise CustodyError("guardian child identity mismatch")
        return result

    def group_signal(self, payload, decide):
        self.observe(payload)
        signum = decide()  # fresh phase/deadline AFTER the ownership observation
        os.killpg(payload, signum)
        return signum

    def child_signal(self, pid, decide):
        # ECHILD propagates; None still proves a waitable direct child. No reaper
        # or parallel thread exists between this check and the positive send.
        if self.observe(pid) is None:
            signum = decide()  # last userspace step before the numeric send
            os.kill(pid, signum)
            return signum
        return None

    def reap(self, pid):
        result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | WALL)
        if result is None or result.si_pid != pid:
            raise CustodyError("guardian final reap identity mismatch")
        return result

    def empty_wait(self):
        try:
            os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT | WALL)
        except ChildProcessError as exc:
            if exc.errno == errno.ECHILD:
                return
            raise
        raise CustodyError("guardian final ECHILD unproven")


def _exitcode(result):
    if (
        result.si_code == os.CLD_EXITED
        and _integer(result.si_status)
        and 0 <= result.si_status <= 255
    ):
        return result.si_status
    if (
        result.si_code in (os.CLD_KILLED, os.CLD_DUMPED)
        and _integer(result.si_status)
        and 0 < result.si_status < signal.NSIG
    ):
        return -result.si_status
    raise CustodyError("guardian terminal status malformed")


class _Keeper:
    def __init__(
        self, control, status, live=None, telemetry=None, *, kernel=None, clock=time.monotonic
    ):
        self.kernel = kernel or _Kernel()
        self.clock = clock
        self.control, self.status, self.live, self.telemetry = control, status, live, telemetry
        self.selector = selectors.DefaultSelector()
        self.selector.register(control, selectors.EVENT_READ, "control")
        if live is not None:
            self.selector.register(live, selectors.EVENT_READ, "live")
        self.control_data = b""
        self.payload = None
        self.retained = set()
        self.first_stop = None
        self.force = False
        self.failed = False
        self.reaping = False
        self.drain_proven = False
        self.phase = "starting"
        self.sent = {}
        self.group_sent = set()
        self.group_proven = False
        self.failure_notified = False
        self.signals_disabled = False
        self.payload_status = None
        self.observed_terminal = set()
        self.reported_errors = set()
        self.ready = False

    def event(self, operation, *, error=0):
        # This strict sink survives supervisor death. No arbitrary strings,
        # command arguments, paths, exception text or environment are accepted.
        if self.telemetry is None:
            return
        operations = {
            "admit",
            "launch",
            "ready",
            "eof",
            "stop",
            "observe",
            "group",
            "child",
            "failure",
            "reap",
            "echild",
            "drained",
        }
        if operation not in operations or self.phase not in {
            "starting",
            "running",
            "term",
            "kill",
            "failed",
            "reap",
            "drained",
        }:
            raise CustodyError("invalid guardian telemetry field")
        if not _integer(error) or not 0 <= error <= 4095:
            raise CustodyError("invalid guardian telemetry errno")
        if operation == "echild" and (
            not self.drain_proven
            or not self.reaping
            or self.phase != "reap"
            or self.retained
            or self.observed_terminal
            or not _integer(self.payload_status)
        ):
            raise CustodyError("unproven final ECHILD telemetry")
        # Counts describe this keeper's retained/observed custody snapshot, not
        # a scan or assertion about any unobserved peer process.
        data = _record(
            {
                "phase": self.phase,
                "operation": operation,
                "errno": error,
                "children": len(self.retained),
                "terminal": len(self.observed_terminal),
                "deadline": None
                if self.first_stop is None
                else self.first_stop + TERM_SECONDS + KILL_SECONDS,
                "payload_status_present": self.payload_status is not None,
            }
        )
        # Regular private file, bounded fixed events. Failure is a custody error,
        # not an exception printed into payload stderr. No event flood on loops.
        if os.write(self.telemetry, data) != len(data):
            raise CustodyError("guardian telemetry write failed")

    def primitive(self, operation, function, *args):
        try:
            return function(*args)
        except BaseException as exc:
            number = (
                exc.errno
                if isinstance(exc, OSError) and _integer(exc.errno) and 0 <= exc.errno <= 4095
                else 0
            )
            key = (operation, number)
            if key not in self.reported_errors:
                self.reported_errors.add(key)
                try:
                    self.event(operation, error=number)
                except BaseException:
                    pass
            raise

    def send_status(self, data):
        raw = _record(data)
        if len(raw) > MAX_RECORD:
            raise CustodyError("guardian status bound")
        if os.write(self.status, raw) != len(raw):
            raise CustodyError("guardian status incomplete")

    def drop_input(self, which):
        descriptor = getattr(self, which)
        if descriptor is not None:
            self.selector.unregister(descriptor)
            setattr(self, which, None)
            os.close(descriptor)

    def stop(self, t0=None, *, force=False):
        now = self.clock()
        if t0 is not None and (not _time(t0) or t0 > now + 0.05):
            raise CustodyError("guardian stop time invalid")
        first = now if t0 is None else t0
        if self.first_stop is None:
            self.first_stop = first
            self.send_status({"state": "stopping", "t0": first})
        else:
            self.first_stop = min(self.first_stop, first)
        self.force |= force
        if not self.failed and not self.reaping:
            self.phase = "kill" if self.force or now >= self.first_stop + TERM_SECONDS else "term"
        self.event("stop")

    def inputs(self, timeout):
        for key, _events in self.selector.select(timeout):
            which = key.data
            descriptor = getattr(self, which)
            try:
                raw = os.read(descriptor, MAX_RECORD + 1)
            except BlockingIOError:
                continue
            if not raw:
                self.drop_input(which)
                self.stop(force=True)
                self.event("eof")
                continue
            if which == "live":
                raise CustodyError("guardian liveness protocol invalid")
            self.control_data += raw
            if len(self.control_data) > MAX_RECORD:
                raise CustodyError("guardian control bound")
            while b"\n" in self.control_data:
                line, self.control_data = self.control_data.split(b"\n", 1)
                request = json.loads(line)
                if (
                    type(request) is not dict
                    or set(request) != {"op", "t0"}
                    or request["op"] != "stop"
                ):
                    raise CustodyError("guardian control malformed")
                self.stop(request["t0"])

    def observe(self):
        current = self.primitive("observe", self.kernel.children)
        if not self.retained <= current:
            raise CustodyError("guardian child disappeared without final reap")
        previous_retained = set(self.retained)
        previous_terminal = set(self.observed_terminal)
        self.retained |= current
        result = {
            pid: self.primitive("observe", self.kernel.observe, pid) for pid in sorted(current)
        }
        # Each retained PID is pinned by a still-waitable own direct child.
        for value in result.values():
            if value is not None:
                _exitcode(value)
        self.observed_terminal = {pid for pid, value in result.items() if value is not None}
        if self.payload in result and result[self.payload] is not None:
            self.payload_status = _exitcode(result[self.payload])
        if self.retained != previous_retained or self.observed_terminal != previous_terminal:
            self.event("observe")
        return result

    def phase_for_send(self):
        if (
            self.reaping
            or self.signals_disabled
            or self.first_stop is None
            or self.phase not in {"term", "kill", "failed"}
        ):
            raise CustodyError("guardian signal phase invalid")
        now = self.clock()
        if now >= self.first_stop + TERM_SECONDS + KILL_SECONDS:
            raise CustodyError("guardian signal deadline")
        phase = (
            "kill" if self.failed or self.force or now >= self.first_stop + TERM_SECONDS else "term"
        )
        if not self.failed:
            self.phase = phase
        return phase

    def signal_for_numeric(self):
        return signal.SIGKILL if self.phase_for_send() == "kill" else signal.SIGTERM

    def signal_children(self, observed):
        phase = self.phase_for_send()
        if self.group_proven and phase not in self.group_sent:
            if self.payload not in observed:
                raise CustodyError("guardian payload custody lost")
            attempted = []

            def decide_group():
                signum = self.signal_for_numeric()
                attempted.append(signum)
                return signum

            try:
                self.primitive("group", self.kernel.group_signal, self.payload, decide_group)
                self.event("group")
            except OSError as exc:
                if exc.errno not in (errno.EPERM, errno.ESRCH):
                    raise
                self.event("group", error=exc.errno)
            finally:
                if attempted:
                    self.group_sent.add("kill" if attempted[-1] == signal.SIGKILL else "term")
            # Success, EPERM AND ESRCH all require fresh adopted-child accounting.
            observed = self.observe()
        for pid in sorted(observed):
            phase = self.phase_for_send()
            if observed[pid] is None and self.sent.get(pid) != phase:
                try:
                    signum = self.primitive(
                        "child", self.kernel.child_signal, pid, self.signal_for_numeric
                    )
                except BaseException:
                    self.signals_disabled = True
                    raise
                if signum is not None:
                    self.sent[pid] = "kill" if signum == signal.SIGKILL else "term"
                    self.event("child")

    def final_reap(self, observed):
        if (
            self.payload is None
            or self.payload not in observed
            or any(value is None for value in observed.values())
        ):
            raise CustodyError("guardian final reap not proven")
        repeated = self.observe()
        if repeated.keys() != observed.keys() or any(value is None for value in repeated.values()):
            return False
        if (
            not self.failed
            and self.first_stop is not None
            and self.clock() >= self.first_stop + TERM_SECONDS + KILL_SECONDS
        ):
            raise CustodyError("guardian cleanup deadline")
        # This transition is permanent. No code path may signal after a reap.
        self.reaping = True
        self.phase = "reap"
        payload_status = None
        for pid in sorted(repeated):
            expected = _exitcode(repeated[pid])
            result = self.primitive("reap", self.kernel.reap, pid)
            if _exitcode(result) != expected:
                raise CustodyError("guardian final exit status changed")
            self.retained.remove(pid)
            self.observed_terminal.discard(pid)
            if pid == self.payload:
                payload_status = expected
        self.primitive("reap", self.kernel.empty_wait)
        if self.retained or not _integer(payload_status):
            raise CustodyError("guardian payload exit status unproven")
        self.drain_proven = True
        self.event("echild")
        self.event("reap")
        if not self.failed:
            if (
                self.first_stop is not None
                and self.clock() >= self.first_stop + TERM_SECONDS + KILL_SECONDS
            ):
                raise CustodyError("guardian cleanup deadline")
            self.phase = "drained"
            self.event("drained")
            self.send_status({"state": "drained", "returncode": payload_status})
        return True

    def fail(self, exc):
        self.failed = True
        self.phase = "failed"
        if self.first_stop is None:
            self.first_stop = self.clock()
        self.force = True
        # Stop watching EOF/control entirely; no ready-FD failure spin, repeated
        # status flood, repeated kill or unguarded membership read in recovery.
        for which in ("control", "live"):
            try:
                self.drop_input(which)
            except BaseException:
                pass
        if not self.failure_notified:
            self.failure_notified = True
            try:
                self.send_status({"state": "failed"})
            except BaseException:
                pass
            try:
                self.event(
                    "failure",
                    error=exc.errno if isinstance(exc, OSError) and _integer(exc.errno) else 0,
                )
            except BaseException:
                pass

    def launch(self, config):
        self.primitive("admit", self.kernel.admit)
        self.event("admit")
        self.inputs(0)
        if self.first_stop is not None:
            raise CustodyError("guardian parent stopped before launch")
        exec_r, exec_w = _pipe()
        # Popen passes only these known descriptors plus stdio. A caller can
        # inherit descriptors above a subsequently lowered soft RLIMIT_NOFILE;
        # close those too without changing any resource limit or policy.
        inherited = [self.control, self.status, exec_r, exec_w]
        inherited += [fd for fd in (self.live, self.telemetry) if fd is not None]
        self.kernel.fd_limit = max(self.kernel.fd_limit, max(inherited) + 1)
        try:
            pid = os.fork()
        except BaseException:
            os.close(exec_r)
            os.close(exec_w)
            raise
        if pid == 0:
            try:
                os.close(exec_r)
                os.setsid()
                # Close every non-stdio descriptor except the CLOEXEC error
                # writer BEFORE exec. No control/liveness/status/telemetry copy
                # survives in the payload, even if it forks before exec.
                os.closerange(3, exec_w)
                os.closerange(exec_w + 1, self.kernel.fd_limit)
                # Match ordinary Popen restore_signals for a non-Python
                # payload; our intermediate interpreter must not leak Python's
                # ignored SIGPIPE/SIGXFZ/SIGXFSZ into psql/ssh/shell programs.
                signal.signal(signal.SIGCHLD, self.kernel.payload_sigchld)
                for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
                    if hasattr(signal, name):
                        signal.signal(getattr(signal, name), signal.SIG_DFL)
                # Private fork-stub acknowledgement AFTER successful setsid.
                # EOF alone could also mean death before exec/setsid; it is
                # insufficient proof for numeric group authority.
                if os.write(exec_w, b"S\n") != 2:
                    raise CustodyError("guardian session acknowledgement failed")
                os.execvpe(config["argv"][0], config["argv"], os.environ)
            except BaseException as exc:
                number = (
                    exc.errno if isinstance(exc, OSError) and _integer(exc.errno) else errno.EIO
                )
                try:
                    os.write(exec_w, b"E" + str(number).encode() + b"\n")
                except OSError:
                    pass
                os._exit(127)
        self.payload = pid
        self.retained.add(pid)
        os.close(exec_w)
        # Forked payload owns stdio; keeper must not keep its duplicate stream
        # ends alive or consume stdin. No Python stream API is used afterwards.
        for descriptor in (0, 1, 2):
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
        try:
            until = self.clock() + START_SECONDS
            with selectors.DefaultSelector() as admission:
                admission.register(exec_r, selectors.EVENT_READ)
                admission_data = b""
                while True:
                    self.inputs(0)
                    if self.first_stop is not None or self.clock() >= until:
                        raise CustodyError("guardian exec admission interrupted")
                    if admission.select(min(POLL_SECONDS, max(0, until - self.clock()))):
                        fragment = os.read(exec_r, 32)
                        admission_data += fragment
                        if len(admission_data) > 32:
                            raise CustodyError("guardian exec admission malformed")
                        if admission_data.startswith(b"S\n"):
                            self.group_proven = True
                            error = admission_data[2:]
                        else:
                            error = admission_data
                        if error.startswith(b"E") and error.endswith(b"\n"):
                            value = error[1:-1]
                            number = (
                                int(value)
                                if value.isdigit() and 0 < int(value) <= 4095
                                else errno.EIO
                            )
                            self.event("launch", error=number)
                            raise OSError(number, "owned payload exec failed")
                        if not fragment:
                            if admission_data != b"S\n":
                                raise CustodyError("guardian exec/session admission unproven")
                            break
        finally:
            os.close(exec_r)
        self.phase = "running"
        self.event("launch")
        self.send_status({"state": "ready", "pid": self.payload})
        self.ready = True

    def run(self, config):
        try:
            self.launch(config)
            while True:
                observed = self.observe()
                if self.payload not in observed:
                    raise CustodyError("guardian payload missing")
                if len(self.retained) >= MAX_CHILDREN // 2 and self.first_stop is None:
                    # Retain identities instead of hiding a background reaper;
                    # bounded controlled jobs shut down before sustained churn
                    # can accumulate an unbounded zombie population.
                    self.stop(force=True)
                if observed and all(value is not None for value in observed.values()):
                    if self.final_reap(observed):
                        return 0
                if observed[self.payload] is not None and self.first_stop is None:
                    self.stop()
                if self.first_stop is not None:
                    now = self.clock()
                    if now >= self.first_stop + TERM_SECONDS + KILL_SECONDS:
                        raise CustodyError("guardian cleanup deadline")
                    self.phase = (
                        "kill" if self.force or now >= self.first_stop + TERM_SECONDS else "term"
                    )
                    self.signal_children(observed)
                self.inputs(POLL_SECONDS)
        except BaseException as exc:
            self.fail(exc)
        if self.payload is None or self.drain_proven:
            return 1
        # Failure is sticky and observable. A coherent own-child observation may
        # still finish cleanup within the ORIGINAL latched budget. A denied
        # child send disables further sends; after deadline we only observe.
        # No extra parent wait; a kernel-stuck job retains this keeper forever.
        while True:
            within_budget = self.clock() < self.first_stop + TERM_SECONDS + KILL_SECONDS
            time.sleep(POLL_SECONDS if within_budget else 1)
            try:
                if self.drain_proven:
                    return 1
                if self.reaping:
                    # A partial/unproven final reap cannot restart observation
                    # or signaling with recycled PID identities. Retain keeper.
                    continue
                observed = self.observe()
                if (
                    observed
                    and all(value is not None for value in observed.values())
                    and self.final_reap(observed)
                ):
                    return 1
                if within_budget and not self.signals_disabled:
                    self.signal_children(observed)
            except BaseException:
                # Explicit failure was already sent. Re-observe only on the
                # next bounded-rate tick; never print private exception text.
                continue


def _entry(argv):
    # Fixed FD-only helper invocation; configuration travels on its private pipe.
    if len(argv) != 5 or any(not item.lstrip("-").isdigit() for item in argv):
        return 1
    config_r, control, status, live, telemetry = map(int, argv)
    supplied = [fd for fd in (config_r, control, status, live, telemetry) if fd >= 0]
    if (
        any(fd < 3 for fd in (config_r, control, status))
        or live < -1
        or telemetry < -1
        or any(fd < 3 for fd in supplied)
        or len(supplied) != len(set(supplied))
    ):
        return 1
    if any(
        not stat.S_ISFIFO(os.fstat(fd).st_mode)
        for fd in (config_r, control, status) + (() if live == -1 else (live,))
    ):
        return 1
    if telemetry != -1 and (
        not stat.S_ISREG(os.fstat(telemetry).st_mode)
        or stat.S_IMODE(os.fstat(telemetry).st_mode) != 0o600
    ):
        return 1
    keeper = _Keeper(
        control, status, None if live == -1 else live, None if telemetry == -1 else telemetry
    )
    data = bytearray()
    try:
        with selectors.DefaultSelector() as readable:
            readable.register(config_r, selectors.EVENT_READ)
            until = time.monotonic() + START_SECONDS
            while not data.endswith(b"\n"):
                if time.monotonic() >= until:
                    raise CustodyError("guardian configuration deadline")
                if readable.select(POLL_SECONDS):
                    chunk = os.read(config_r, 4096)
                    if not chunk:
                        raise CustodyError("guardian configuration missing")
                    data.extend(chunk)
                    if len(data) > MAX_CONFIG:
                        raise CustodyError("guardian configuration bound")
        config = json.loads(data)
        if (
            type(config) is not dict
            or set(config) != {"argv", "cwd"}
            or not isinstance(config["argv"], list)
            or not config["argv"]
            or not all(isinstance(x, str) for x in config["argv"])
            or config["cwd"] is not None
            and not isinstance(config["cwd"], str)
        ):
            raise CustodyError("guardian configuration malformed")
    except BaseException as exc:
        keeper.fail(exc)
        return 1
    finally:
        os.close(config_r)
    return keeper.run(config)


if __name__ == "__main__":
    # os._exit avoids Python/Popen/stdio finalizers acquiring or reaping child
    # identities behind the dedicated keeper's custody state machine.
    try:
        result = _entry(sys.argv[2:]) if len(sys.argv) >= 2 and sys.argv[1] == "_keeper" else 1
    except BaseException:
        result = 1
    os._exit(result)
