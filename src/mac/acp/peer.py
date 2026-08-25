"""A transport-agnostic JSON-RPC 2.0 peer for the Agent Client Protocol.

The :class:`Peer` is the heart of the ACP wiring: it owns request/response
correlation by ``id``, dispatches inbound notifications and inbound requests to
registered handlers, and frames messages as newline-delimited JSON (one JSON
object per line -- ACP's stdio framing).

It is **transport-agnostic and synchronously drivable**: construct it with a
``send`` callable (anything that accepts ``bytes``), push inbound bytes in via
:meth:`feed`, and call :meth:`pump` to process complete frames. There is no
asyncio and no third-party dependency, so it can be exercised directly in unit
tests by pairing two peers over in-memory buffers.

A thin stdio binding (:func:`stdio_peer`) is provided for the real local-
subprocess case, but it is strictly a convenience wrapper around the pure core.
"""

from __future__ import annotations

import itertools
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional, Union

from .protocol import (
    ERROR_INTERNAL,
    ERROR_METHOD_NOT_FOUND,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    decode_message,
    json_dumps,
)


__all__ = ["Peer", "PendingRequest", "RemoteError", "stdio_peer", "DEFERRED"]


class _Deferred:
    """Sentinel a request handler returns to defer its response.

    A synchronous handler normally returns its result, which the peer writes back
    immediately. Returning :data:`DEFERRED` instead tells the peer the handler
    will send the response later -- via :meth:`Peer.respond_result` /
    :meth:`Peer.respond_error` with the request id -- so a long-running handler
    can hand off to a worker thread without blocking the inbound pump loop (which
    must stay free to deliver responses/notifications arriving *during* the work).
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "DEFERRED"


#: Singleton sentinel; see :class:`_Deferred`.
DEFERRED = _Deferred()


SendFn = Callable[[bytes], None]

#: A request handler maps inbound ``params`` to a result (returned to the peer)
#: or raises to produce a JSON-RPC error. Handlers are synchronous.
RequestHandler = Callable[[Optional[Dict[str, Any]]], Any]

#: A notification handler consumes inbound ``params``; its return value (if any)
#: is ignored, since notifications have no response.
NotificationHandler = Callable[[Optional[Dict[str, Any]]], None]

#: A raw request handler receives the full request (so it can read the ``id``)
#: and returns a result, raises for an error, or returns :data:`DEFERRED` to
#: send the response itself later.
RawRequestHandler = Callable[["JSONRPCRequest"], Any]


class RemoteError(Exception):
    """Raised when a peer returns a JSON-RPC error response to our request."""

    def __init__(self, error: JSONRPCError) -> None:
        super().__init__("%s (code %s)" % (error.message, error.code))
        self.error = error


class PendingRequest:
    """A future-like handle for an outbound request awaiting its response.

    Deliberately synchronous: :meth:`result` blocks on a
    :class:`threading.Event` so the peer works both in single-threaded test
    drivers (where the response is fed before ``result`` is called, or another
    thread pumps) and in the threaded stdio binding.
    """

    def __init__(self, request_id: Union[str, int]) -> None:
        self.request_id = request_id
        self._event = threading.Event()
        self._response: Optional[JSONRPCResponse] = None

    def _fulfill(self, response: JSONRPCResponse) -> None:
        self._response = response
        self._event.set()

    def done(self) -> bool:
        return self._event.is_set()

    def result(self, timeout: Optional[float] = None) -> Any:
        """Block until the response arrives; return its ``result`` payload.

        Raises :class:`RemoteError` if the peer returned an error, or
        :class:`TimeoutError` if ``timeout`` elapses first.
        """

        if not self._event.wait(timeout):
            raise TimeoutError(
                "no response for request id=%r within %s s" % (self.request_id, timeout)
            )
        assert self._response is not None  # set alongside the event
        if self._response.is_error:
            assert self._response.error is not None
            raise RemoteError(self._response.error)
        return self._response.result


class Peer:
    """A synchronous JSON-RPC 2.0 endpoint over newline-delimited frames."""

    def __init__(self, send: SendFn) -> None:
        self._send = send
        self._id_counter = itertools.count(1)
        self._pending: Dict[Union[str, int], PendingRequest] = {}
        self._request_handlers: Dict[str, RequestHandler] = {}
        self._raw_request_handlers: Dict[str, RawRequestHandler] = {}
        self._notification_handlers: Dict[str, NotificationHandler] = {}
        self._inbox = bytearray()
        self._lock = threading.Lock()

    # -- handler registration ------------------------------------------------

    def on_request(self, method: str, handler: RequestHandler) -> None:
        """Register a handler for inbound requests with the given ``method``.

        The handler receives the request ``params`` and returns the result (or
        raises to produce a JSON-RPC error). To defer the response to a worker
        thread, register via :meth:`on_request_raw` instead, or return
        :data:`DEFERRED` and arrange to call :meth:`respond_result` /
        :meth:`respond_error` -- but only :meth:`on_request_raw` hands you the
        request id needed to do so.
        """

        self._request_handlers[method] = handler

    def on_request_raw(self, method: str, handler: "RawRequestHandler") -> None:
        """Register a handler that receives the full :class:`JSONRPCRequest`.

        Unlike :meth:`on_request`, the handler sees the request ``id`` (and may
        return :data:`DEFERRED` to suppress the auto-written response and reply
        later via :meth:`respond_result` / :meth:`respond_error`). This is the
        seam a server uses to run long work off the pump thread.
        """

        self._raw_request_handlers[method] = handler

    def on_notification(self, method: str, handler: NotificationHandler) -> None:
        """Register a handler for inbound notifications with ``method``."""

        self._notification_handlers[method] = handler

    # -- outbound ------------------------------------------------------------

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> PendingRequest:
        """Send a request and return a :class:`PendingRequest` to await it."""

        with self._lock:
            request_id = next(self._id_counter)
            pending = PendingRequest(request_id)
            self._pending[request_id] = pending
        self._write(JSONRPCRequest(id=request_id, method=method, params=params))
        return pending

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Send a notification (fire-and-forget, no response correlation)."""

        self._write(JSONRPCNotification(method=method, params=params))

    def respond_result(self, request_id: Union[str, int], result: Any) -> None:
        """Send a success response for a previously-deferred inbound request.

        Used together with a handler that returns :data:`DEFERRED`: the handler
        captures ``request.id`` (or its params), does work off the pump thread,
        then calls this to deliver the result.
        """

        self._write(JSONRPCResponse(id=request_id, result=result))

    def respond_error(self, request_id: Union[str, int], error: JSONRPCError) -> None:
        """Send an error response for a previously-deferred inbound request."""

        self._write(JSONRPCResponse(id=request_id, error=error))

    def _write(self, message: Any) -> None:
        line = json_dumps(message.to_dict()) + "\n"
        self._send(line.encode("utf-8"))

    # -- inbound -------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Append raw inbound bytes; complete lines are processed by :meth:`pump`."""

        self._inbox.extend(data)

    def pump(self) -> int:
        """Process every complete newline-delimited frame currently buffered.

        Returns the number of frames processed. Partial trailing data is
        retained for the next call. Safe to call repeatedly.
        """

        processed = 0
        while True:
            idx = self._inbox.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._inbox[:idx])
            del self._inbox[: idx + 1]
            stripped = raw.strip()
            if not stripped:
                continue
            self._dispatch(stripped)
            processed += 1
        return processed

    def feed_and_pump(self, data: bytes) -> int:
        """Convenience: :meth:`feed` then :meth:`pump`."""

        self.feed(data)
        return self.pump()

    def _dispatch(self, raw: bytes) -> None:
        message = decode_message(raw)
        if isinstance(message, JSONRPCResponse):
            self._handle_response(message)
        elif isinstance(message, JSONRPCRequest):
            self._handle_request(message)
        elif isinstance(message, JSONRPCNotification):
            self._handle_notification(message)

    def _handle_response(self, response: JSONRPCResponse) -> None:
        with self._lock:
            pending = self._pending.pop(response.id, None)
        if pending is not None:
            pending._fulfill(response)

    def _handle_request(self, request: JSONRPCRequest) -> None:
        raw_handler = self._raw_request_handlers.get(request.method)
        handler = self._request_handlers.get(request.method)
        if raw_handler is None and handler is None:
            self._write(
                JSONRPCResponse(
                    id=request.id,
                    error=JSONRPCError(
                        code=ERROR_METHOD_NOT_FOUND,
                        message="method not found: %s" % request.method,
                    ),
                )
            )
            return
        try:
            if raw_handler is not None:
                result = raw_handler(request)
            else:
                assert handler is not None
                result = handler(request.params)
        except RemoteError as exc:  # handler chose to surface a wire error
            self._write(JSONRPCResponse(id=request.id, error=exc.error))
            return
        except Exception as exc:  # noqa: BLE001 -- map any handler failure to a JSON-RPC error
            self._write(
                JSONRPCResponse(
                    id=request.id,
                    error=JSONRPCError(code=ERROR_INTERNAL, message=str(exc)),
                )
            )
            return
        if result is DEFERRED:
            # The handler will deliver the response later via respond_result /
            # respond_error; do not auto-write one now.
            return
        self._write(JSONRPCResponse(id=request.id, result=result))

    def _handle_notification(self, notification: JSONRPCNotification) -> None:
        handler = self._notification_handlers.get(notification.method)
        if handler is not None:
            handler(notification.params)


# ---------------------------------------------------------------------------
# stdio binding (thin convenience wrapper around the pure core)
# ---------------------------------------------------------------------------


class _StdioPeer(Peer):
    """A :class:`Peer` bound to a subprocess's stdin/stdout.

    A background reader thread streams the child's stdout into :meth:`feed` and
    drives :meth:`pump`, so callers interact purely through the
    :class:`Peer` API (``request``/``notify`` + registered handlers).
    """

    def __init__(self, argv: List[str], **popen_kwargs: Any) -> None:
        self._proc = subprocess.Popen(  # noqa: S603 -- argv supplied by caller/config
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            **popen_kwargs,
        )
        assert self._proc.stdin is not None and self._proc.stdout is not None
        super().__init__(send=self._send_to_stdin)
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _send_to_stdin(self, data: bytes) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def _read_loop(self) -> None:
        stdout = self._proc.stdout
        assert stdout is not None
        while not self._stop.is_set():
            line = stdout.readline()
            if not line:  # EOF -- child closed stdout / exited
                break
            self.feed(line if isinstance(line, bytes) else line.encode("utf-8"))
            self.pump()

    def close(self) -> None:
        """Stop the reader and terminate the subprocess."""

        self._stop.set()
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def stdio_peer(argv: List[str], **popen_kwargs: Any) -> _StdioPeer:
    """Spawn ``argv`` as a subprocess and return a :class:`Peer` wired to its
    stdio (newline-delimited JSON-RPC). Caller is responsible for calling
    ``close()`` (or using it as a context-manager-style resource)."""

    return _StdioPeer(argv, **popen_kwargs)
