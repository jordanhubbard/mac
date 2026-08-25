"""Debug-terminal subsystem extracted from worker.py.

Contains:
  - DebugTerminalSession: dataclass representing an active PTY session
  - DebugTerminalMixin: mixin that provides all _debug_terminal_* methods
    for MacWorker

These are imported back into worker.py; callers that import from mac.worker
see no change.
"""

from __future__ import annotations

import base64
import fcntl
import os
import pty
import select
import socket
import struct
import subprocess
import termios
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode

from mac.agentbus_control import (
    DEBUG_TERMINAL_INPUT_SCHEMA,
    DEBUG_TERMINAL_OPEN_SCHEMA,
    DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE,
    debug_terminal_output_payload,
)

JsonDict = Dict[str, Any]


def _bounded_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


@dataclass
class DebugTerminalSession:
    session_id: str
    input_stream_id: str
    output_stream_id: str
    output_recipient_agent_id: str
    process: subprocess.Popen[bytes]
    master_fd: int
    next_input_sequence: int = 0
    expires_at_monotonic: float = 0.0
    closed: bool = False


class DebugTerminalMixin:
    """Mixin that provides the debug-terminal subsystem to MacWorker.

    Relies on the following attributes being set by MacWorker.__init__:
      self.client, self.agent_id, self.workspace,
      self.debug_terminal_enabled, self._debug_terminal_sessions
    """

    def _handle_debug_terminal_open_stream(self, stream: JsonDict) -> JsonDict:
        stream_id = str(stream.get("id") or "")
        chunks = self.client.get(
            "/agentbus/streams/%s/chunks?%s"
            % (
                quote(stream_id, safe=""),
                urlencode({"agent_id": self.agent_id, "after_sequence": 0, "limit": 10}),
            )
        )
        payload: Any = None
        if isinstance(chunks, list) and chunks:
            payload = chunks[-1].get("payload") if isinstance(chunks[-1], dict) else None
        try:
            return self._execute_debug_terminal_open(payload, stream_id)
        except Exception as exc:  # noqa: BLE001 - failed terminal requests must be observable.
            result = self._debug_terminal_result(stream_id, payload, "error", str(exc))
            self._publish_debug_terminal_output(payload, "error", message=str(exc), close=True)
            return result

    def _execute_debug_terminal_open(self, payload: Any, stream_id: str) -> JsonDict:
        request: JsonDict = payload if isinstance(payload, dict) else {}
        if request.get("schema") not in {None, "", DEBUG_TERMINAL_OPEN_SCHEMA}:
            result = self._debug_terminal_result(
                stream_id,
                request,
                "error",
                "unsupported debug terminal schema: %s" % request.get("schema"),
            )
            self._publish_debug_terminal_output(
                request, "error", message=result["summary"], close=True
            )
            return result
        if not self.debug_terminal_enabled:
            result = self._debug_terminal_result(
                stream_id,
                request,
                "error",
                "debug terminal is disabled on this worker",
            )
            self._publish_debug_terminal_output(
                request, "error", message=result["summary"], close=True
            )
            return result

        session_id = str(request.get("session_id") or "").strip()
        input_stream_id = str(request.get("input_stream_id") or "").strip()
        output_stream_id = str(request.get("output_stream_id") or "").strip()
        output_recipient = str(request.get("sender_agent_id") or "").strip()
        if not session_id or not input_stream_id or not output_stream_id or not output_recipient:
            result = self._debug_terminal_result(
                stream_id,
                request,
                "error",
                "debug terminal request is missing session or stream identifiers",
            )
            self._publish_debug_terminal_output(
                request, "error", message=result["summary"], close=True
            )
            return result
        if session_id in self._debug_terminal_sessions:
            result = self._debug_terminal_result(
                stream_id,
                request,
                "error",
                "debug terminal session already exists",
            )
            self._publish_debug_terminal_output(
                request, "error", message=result["summary"], close=True
            )
            return result

        rows = _bounded_int(request.get("rows"), 8, 80, 32)
        cols = _bounded_int(request.get("cols"), 40, 240, 120)
        ttl_seconds = _bounded_int(request.get("ttl_seconds"), 30, 3600, 900)
        shell = self._debug_terminal_shell(str(request.get("shell") or ""))
        cwd = self._debug_terminal_cwd(str(request.get("cwd") or ""))
        self.workspace.mkdir(parents=True, exist_ok=True)

        master_fd: Optional[int] = None
        slave_fd: Optional[int] = None
        try:
            master_fd, slave_fd = pty.openpty()
            self._set_debug_terminal_size(slave_fd, rows, cols)
            env = os.environ.copy()
            env.setdefault("TERM", "xterm-256color")
            env["MAC_DEBUG_TERMINAL_SESSION_ID"] = session_id
            process = subprocess.Popen(
                [shell],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(cwd),
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as exc:
            for fd in (master_fd, slave_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            result = self._debug_terminal_result(
                stream_id,
                request,
                "error",
                "failed to open debug terminal: %s" % exc,
            )
            self._publish_debug_terminal_output(
                request, "error", message=result["summary"], close=True
            )
            return result
        finally:
            if slave_fd is not None:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass

        assert master_fd is not None
        try:
            os.set_blocking(master_fd, False)
        except OSError:
            pass
        session = DebugTerminalSession(
            session_id=session_id,
            input_stream_id=input_stream_id,
            output_stream_id=output_stream_id,
            output_recipient_agent_id=output_recipient,
            process=process,
            master_fd=master_fd,
            expires_at_monotonic=time.monotonic() + float(ttl_seconds),
        )
        self._debug_terminal_sessions[session_id] = session
        self._append_debug_terminal_output(
            session,
            "opened",
            message="debug terminal opened on %s" % socket.gethostname(),
        )
        return self._debug_terminal_result(
            stream_id,
            request,
            "opened",
            "debug terminal opened",
            shell=shell,
            cwd=str(cwd),
            ttl_seconds=ttl_seconds,
        )

    def _debug_terminal_result(
        self,
        stream_id: str,
        request: Any,
        status: str,
        summary: str,
        **extra: Any,
    ) -> JsonDict:
        payload = request if isinstance(request, dict) else {}
        result: JsonDict = {
            "schema": "mac.agentbus.debug_terminal_open_result.v1",
            "status": status,
            "summary": summary[:4000],
            "agent_id": self.agent_id,
            "stream_id": stream_id,
            "request_id": payload.get("request_id"),
            "session_id": payload.get("session_id"),
            "input_stream_id": payload.get("input_stream_id"),
            "output_stream_id": payload.get("output_stream_id"),
            "restart_requested": False,
        }
        for key, value in extra.items():
            result[key] = value[:4000] if isinstance(value, str) else value
        return result

    def _debug_terminal_shell(self, requested: str) -> str:
        candidate = (requested or os.environ.get("SHELL") or "/bin/sh").strip()
        if not candidate.startswith("/"):
            candidate = "/bin/sh"
        path = Path(candidate)
        if path.exists() and os.access(path, os.X_OK):
            return str(path)
        return "/bin/sh"

    def _debug_terminal_cwd(self, requested: str) -> Path:
        if requested:
            try:
                path = Path(requested).expanduser().resolve()
                if path.is_dir():
                    return path
            except OSError:
                pass
        return self.workspace

    def _set_debug_terminal_size(self, fd: int, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(
                fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", int(rows), int(cols), 0, 0),
            )
        except OSError:
            pass

    def _poll_debug_terminal_sessions(self) -> None:
        if not self._debug_terminal_sessions:
            return
        for session in list(self._debug_terminal_sessions.values()):
            try:
                self._poll_debug_terminal_session(session)
            except Exception as exc:  # noqa: BLE001 - terminal sessions must not break task polling.
                self._observe_log(
                    "worker.debug_terminal.poll_failed",
                    level="warning",
                    detail={"session_id": session.session_id, "error": str(exc)},
                )
                self._close_debug_terminal_session(
                    session,
                    event="error",
                    message="terminal poll failed: %s" % exc,
                    terminate=True,
                )

    def _poll_debug_terminal_session(self, session: DebugTerminalSession) -> None:
        if session.closed:
            return
        self._drain_debug_terminal_output(session)
        self._apply_debug_terminal_input(session)
        self._drain_debug_terminal_output(session)
        returncode = session.process.poll()
        if returncode is not None:
            self._drain_debug_terminal_output(session)
            self._close_debug_terminal_session(
                session,
                event="exit",
                message="debug terminal exited",
                terminate=False,
                exit_code=int(returncode),
            )
            return
        if time.monotonic() >= session.expires_at_monotonic:
            self._close_debug_terminal_session(
                session,
                event="expired",
                message="debug terminal TTL expired",
                terminate=True,
            )

    def _drain_debug_terminal_output(self, session: DebugTerminalSession) -> None:
        for _ in range(32):
            try:
                ready, _, _ = select.select([session.master_fd], [], [], 0)
            except (OSError, ValueError):
                return
            if not ready:
                return
            try:
                data = os.read(session.master_fd, 8192)
            except BlockingIOError:
                return
            except OSError:
                return
            if not data:
                return
            self._append_debug_terminal_output(session, "output", data=data)

    def _apply_debug_terminal_input(self, session: DebugTerminalSession) -> None:
        chunks = self.client.get(
            "/agentbus/streams/%s/chunks?%s"
            % (
                quote(session.input_stream_id, safe=""),
                urlencode(
                    {
                        "agent_id": self.agent_id,
                        "after_sequence": session.next_input_sequence,
                        "limit": 50,
                    }
                ),
            )
        )
        if not isinstance(chunks, list):
            return
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            try:
                session.next_input_sequence = max(
                    session.next_input_sequence,
                    int(chunk.get("sequence") or 0),
                )
            except (TypeError, ValueError):
                pass
            payload = chunk.get("payload") if isinstance(chunk.get("payload"), dict) else {}
            if payload.get("schema") not in {None, "", DEBUG_TERMINAL_INPUT_SCHEMA}:
                continue
            resize = payload.get("resize") if isinstance(payload.get("resize"), dict) else None
            if resize:
                rows = _bounded_int(resize.get("rows"), 8, 80, 32)
                cols = _bounded_int(resize.get("cols"), 40, 240, 120)
                self._set_debug_terminal_size(session.master_fd, rows, cols)
            data_b64 = str(payload.get("data_b64") or "")
            if data_b64:
                try:
                    raw = base64.b64decode(data_b64.encode("ascii"), validate=True)
                except Exception:
                    raw = b""
                if raw:
                    try:
                        os.write(session.master_fd, raw)
                    except (BlockingIOError, OSError):
                        self._append_debug_terminal_output(
                            session,
                            "error",
                            message="terminal input write failed",
                        )
            if payload.get("close"):
                self._close_debug_terminal_session(
                    session,
                    event="closed",
                    message="debug terminal closed",
                    terminate=True,
                )
                return

    def _append_debug_terminal_output(
        self,
        session: DebugTerminalSession,
        event: str,
        *,
        data: bytes = b"",
        message: Optional[str] = None,
        close: bool = False,
        exit_code: Optional[int] = None,
    ) -> None:
        self._append_debug_terminal_output_to_stream(
            session.session_id,
            session.output_stream_id,
            event,
            data=data,
            message=message,
            close=close,
            exit_code=exit_code,
        )

    def _append_debug_terminal_output_to_stream(
        self,
        session_id: str,
        output_stream_id: str,
        event: str,
        *,
        data: bytes = b"",
        message: Optional[str] = None,
        close: bool = False,
        exit_code: Optional[int] = None,
    ) -> None:
        payload = debug_terminal_output_payload(
            session_id=session_id,
            event=event,
            data_b64=base64.b64encode(data).decode("ascii") if data else None,
            message=message,
            exit_code=exit_code,
        )
        try:
            self.client.post(
                "/agentbus/streams/%s/chunks" % quote(output_stream_id, safe=""),
                {
                    "sender_agent_id": self.agent_id,
                    "content_type": DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE,
                    "payload": payload,
                    "final": bool(close),
                },
            )
        except Exception as exc:  # noqa: BLE001 - losing terminal output must not stop worker polling.
            self._observe_log(
                "worker.debug_terminal.output_failed",
                level="warning",
                detail={"session_id": session_id, "error": str(exc)},
            )

    def _publish_debug_terminal_output(
        self,
        request: Any,
        event: str,
        *,
        message: Optional[str] = None,
        close: bool = False,
        exit_code: Optional[int] = None,
    ) -> None:
        payload = request if isinstance(request, dict) else {}
        session_id = str(payload.get("session_id") or "")
        output_stream_id = str(payload.get("output_stream_id") or "")
        if not session_id or not output_stream_id:
            return
        self._append_debug_terminal_output_to_stream(
            session_id,
            output_stream_id,
            event,
            message=message,
            close=close,
            exit_code=exit_code,
        )

    def _close_debug_terminal_session(
        self,
        session: DebugTerminalSession,
        *,
        event: str,
        message: str,
        terminate: bool,
        exit_code: Optional[int] = None,
    ) -> None:
        if session.closed:
            return
        session.closed = True
        if terminate and session.process.poll() is None:
            try:
                session.process.terminate()
                session.process.wait(timeout=0.5)
            except Exception:
                try:
                    session.process.kill()
                except Exception:
                    pass
        if exit_code is None and session.process.poll() is not None:
            exit_code = int(session.process.returncode)
        self._append_debug_terminal_output(
            session,
            event,
            message=message,
            close=True,
            exit_code=exit_code,
        )
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        self._debug_terminal_sessions.pop(session.session_id, None)

    def _close_all_debug_terminal_sessions(self) -> None:
        for session in list(self._debug_terminal_sessions.values()):
            self._close_debug_terminal_session(
                session,
                event="worker_shutdown",
                message="worker is shutting down",
                terminate=True,
            )
