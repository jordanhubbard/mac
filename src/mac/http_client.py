"""HTTP client for talking to a mac hub.

Used by :mod:`mac.dispatch.RemoteDispatch` to translate ControlPlane
method calls into HTTP requests. Has no CLI surface — the `mac` CLI is
the only documented way to drive this.

The transport hook (``transport`` argument) lets tests intercept calls
without round-tripping through ``urllib``.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional


JsonDict = Dict[str, Any]
Transport = Callable[[str, str, Optional[JsonDict], Optional[str]], Any]


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
        """Yield decoded lines from a streaming (NDJSON) endpoint.

        Separate from :meth:`request` on purpose: that one reads the whole body
        before returning, which for a follow stream means it returns when the
        stream ENDS -- exactly never, for a feed. This yields as lines arrive.

        Raises :class:`HubClientError` like the rest of the client, so callers
        that fall back to polling on an older hub can catch one thing.
        """
        headers = {"Accept": "application/x-ndjson"}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        request = urllib.request.Request(
            self.base_url + path, headers=headers, method="GET"
        )
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HubClientError("HTTP %s %s: %s" % (exc.code, exc.reason, detail))
        except urllib.error.URLError as exc:
            raise HubClientError(str(exc.reason))
        except OSError as exc:
            raise HubClientError(str(exc))
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
            raise HubClientError(
                "transient transport error: %s" % exc
            ) from exc
        return json.loads(payload) if payload else None
