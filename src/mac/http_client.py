"""HTTP client for talking to a mac hub.

Used by :mod:`mac.dispatch.RemoteDispatch` to translate ControlPlane
method calls into HTTP requests. Has no CLI surface — the `mac` CLI is
the only documented way to drive this.

The transport hook (``transport`` argument) lets tests intercept calls
without round-tripping through ``urllib``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional


JsonDict = Dict[str, Any]
from mac import __version__

Transport = Callable[[str, str, Optional[JsonDict], Optional[str]], Any]


_VERSION_WARNED = False


def _note_hub_version(hub_version: Optional[str]) -> None:
    """Warn once if this client is older than the hub it is talking to.

    A stale client does not fail with "you are out of date"; it fails with
    whatever internal seam happens to break first. When the publication lane
    was removed the hub stopped returning `publication_route`, an older CLI
    took its backfill path, and `mac task list` died with:

        `mac task_publication_routes` is not yet supported in hub mode.

    Nothing in that names the actual problem. One line here does.

    Deliberately NOT an error and never a refusal: an old client mostly works,
    and breaking it outright is worse than the skew. Warn once per process, on
    stderr so it cannot corrupt `--json` output, and suppressible for anyone
    who has decided to live with it.
    """
    global _VERSION_WARNED
    if _VERSION_WARNED or not hub_version:
        return
    hub = str(hub_version).strip()
    if not hub or hub == __version__:
        return
    if os.environ.get("MAC_SUPPRESS_VERSION_WARNING") == "1":
        _VERSION_WARNED = True
        return
    _VERSION_WARNED = True
    print(
        "mac: this client is %s but the hub is %s. Re-run `make install` in "
        "your mac checkout if commands start failing in ways that name "
        "internal methods. (MAC_SUPPRESS_VERSION_WARNING=1 to silence.)" % (__version__, hub),
        file=sys.stderr,
    )


class HubClientError(RuntimeError):
    """Raised for transport-level failures (HTTP errors, network errors)."""


class HubClient:
    """Minimal JSON-over-HTTP client for the mac hub API."""

    def __init__(
        self,
        base_url: str,
        *,
        token: Optional[str] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._transport = transport or self._urllib_transport

    def request(self, method: str, path: str, body: Optional[JsonDict] = None) -> Any:
        return self._transport(method, self.base_url + path, body, self.token)

    def stream_lines(self, path: str, *, timeout: float = 600.0) -> Any:
        """Open a streaming (NDJSON) endpoint and return an iterator of lines.

        Separate from :meth:`request` on purpose: that one reads the whole body
        before returning, which for a follow stream means returning when the
        stream ENDS -- never, for a feed.

        Deliberately NOT a generator function. A generator body does not run
        until first iteration, so the request would not be issued when this is
        called, and a caller writing ``except HubClientError`` around the call
        would catch nothing: a refused connection or a 404 from a hub without
        this endpoint would surface later, somewhere else. Every other method on
        this client makes its request on call, and so does this one.
        """
        headers = {"Accept": "application/x-ndjson"}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        request = urllib.request.Request(self.base_url + path, headers=headers, method="GET")
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HubClientError("HTTP %s %s: %s" % (exc.code, exc.reason, detail))
        except urllib.error.URLError as exc:
            raise HubClientError(str(exc.reason))
        except OSError as exc:
            raise HubClientError(str(exc))
        return self._iter_stream_lines(response)

    def _iter_stream_lines(self, response: Any) -> Any:
        try:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    yield line
        except OSError as exc:
            raise HubClientError(str(exc))
        finally:
            response.close()

    def _urllib_transport(
        self,
        method: str,
        url: str,
        body: Optional[JsonDict],
        token: Optional[str],
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer %s" % token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
                # getattr, not response.headers: a transport is anything with
                # .read(), and tests/test_infrastructure_coverage.py passes a
                # fake that has no headers at all. A version check must not be
                # able to break a request -- see _note_hub_version.
                headers = getattr(response, "headers", None)
                if headers is not None:
                    _note_hub_version(headers.get("X-MAC-Version"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HubClientError("HTTP %s %s: %s" % (exc.code, exc.reason, detail))
        except urllib.error.URLError as exc:
            raise HubClientError(str(exc.reason))
        except OSError as exc:
            # Socket-level transient failures — a read timeout, connection reset,
            # or broken pipe — are NOT wrapped in URLError by http.client. A
            # timeout while READING the response raises a bare TimeoutError
            # (TimeoutError/ConnectionError are OSError subclasses). Left
            # unwrapped it escapes every caller's ``except HubClientError`` guard;
            # observed killing mac-agent-service when the startup heartbeat
            # surfaced a bare ``TimeoutError`` instead of ``HubClientError``.
            # Wrapping it here makes a transient hub failure uniformly recoverable
            # so callers retry instead of crashing.
            raise HubClientError("transient transport error: %s" % exc) from exc
        return json.loads(payload) if payload else None
