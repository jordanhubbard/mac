"""Transport-neutral client for the MAC HTTP API.

This module deliberately has no gateway/runtime dependencies.  Compatibility
surfaces such as :mod:`mac.hermes_adapter` may re-export these names, but core
workers and orchestrators should import them from here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional


JsonDict = Dict[str, Any]
Transport = Callable[[str, str, Optional[JsonDict]], Any]


class MacApiError(RuntimeError):
    """Raised when a MAC API operation cannot be completed."""


class MacApiClient:
    """Small HTTP client shared by workers, gateways, and orchestrators.

    The optional transport hook is intentionally narrow so tests and embedded
    adapters can exercise the public API contract without depending on a live
    HTTP server.
    """

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        timeout: float = 10.0,
        transport: Optional[Transport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.transport = transport

    def get(self, path: str) -> Any:
        return self.request("GET", path, None)

    def post(self, path: str, payload: JsonDict) -> Any:
        return self.request("POST", path, payload)

    def put(self, path: str, payload: JsonDict) -> Any:
        return self.request("PUT", path, payload)

    def request(self, method: str, path: str, payload: Optional[JsonDict]) -> Any:
        if self.transport is not None:
            return self.transport(method, path, payload)
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8")
            raise MacApiError("mac API %s %s failed: %s" % (method, path, detail)) from exc
        except urllib.error.URLError as exc:
            raise MacApiError("mac API %s %s failed: %s" % (method, path, exc.reason)) from exc
        except OSError as exc:
            # Socket-level transient failures — a read timeout, connection reset,
            # or broken pipe — are NOT wrapped in URLError by http.client. A
            # timeout while READING the response raises a bare TimeoutError
            # (TimeoutError/ConnectionError are OSError subclasses). Left
            # unwrapped it escapes every caller's ``except MacApiError`` guard;
            # observed 2026-07-14 killing a worker's lease-renewal thread on a
            # hub blip, which cascaded into lease loss and a wedged claim loop.
            # Wrapping it here makes a transient hub failure uniformly recoverable
            # (e.g. _assignment_is_current fails safe, renewal retries next tick).
            raise MacApiError(
                "mac API %s %s failed (transient transport error): %s"
                % (method, path, exc)
            ) from exc
        return json.loads(raw) if raw else None
