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
        return json.loads(payload) if payload else None
