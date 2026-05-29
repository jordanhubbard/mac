
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import quote

import httpx

MAC_URL_ENV = "MAC_URL"
MAC_HUB_URL_ENV = "MAC_HUB_URL"
MAC_TOKEN_ENVS = ("MAC_WORKER_TOKEN", "MAC_API_TOKEN", "MAC_TOKEN")
HERMES_INSTANCE_ENV = "MAC_HERMES_INSTANCE_ID"

REQUEST_ID_HEADER = "X-Request-ID"
CLIENT_TIMEOUT_SECONDS = 15.0
CLIENT_RETRY_ATTEMPTS = 2
CLIENT_RETRY_BACKOFF_SECONDS = 0.4
HEALTH_CHECK_TIMEOUT_SECONDS = 2.0

_RETRY_STATUSES = {429, 502, 503, 504}
_HTTP_TO_TOOL_ERROR = {
    400: "request.invalid_json",
    401: "auth.invalid_key",
    403: "auth.forbidden",
    404: "resource.not_found",
    409: "resource.conflict",
    422: "request.schema_validation_error",
    429: "service.unavailable",
    500: "service.internal_error",
    502: "integration.unavailable",
    503: "service.unavailable",
    504: "service.unavailable",
}


@dataclass
class MacClient:
    """Single mac-api HTTP client per asyncio loop."""

    base_url: str
    token: str = ""
    transport: httpx.AsyncBaseTransport | None = None
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    async def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        retry: bool = True,
    ) -> str:
        method = method.upper()
        try:
            path, args = _expand_path(path, dict(body or {}))
        except ValueError as exc:
            return _error_json("request.invalid_json", str(exc), 400)

        headers = {REQUEST_ID_HEADER: f"req_{uuid.uuid4().hex}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra_headers:
            headers.update(extra_headers)

        last_error: Exception | None = None
        retry_attempts = CLIENT_RETRY_ATTEMPTS if retry else 0
        for attempt in range(retry_attempts + 1):
            try:
                request_kwargs: dict[str, Any] = {
                    "params": args if method == "GET" else None,
                    "json": args if method != "GET" else None,
                    "headers": headers,
                }
                if timeout is not None:
                    request_kwargs["timeout"] = timeout
                response = await self._http().request(method, path, **request_kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < retry_attempts:
                    await asyncio.sleep(CLIENT_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                return _error_json("service.unavailable", "mac-api is unreachable", 503)

            if response.status_code in _RETRY_STATUSES and attempt < retry_attempts:
                await asyncio.sleep(CLIENT_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            return _response_json(response)

        message = str(last_error) if last_error else "mac-api is unreachable"
        return _error_json("service.unavailable", message, 503)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                timeout=CLIENT_TIMEOUT_SECONDS,
                transport=self.transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _expand_path(path: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    while "{" in path and "}" in path:
        start = path.index("{")
        end = path.index("}", start)
        name = path[start + 1 : end]
        if name not in args or args[name] in (None, ""):
            raise ValueError(f"Missing required path parameter: {name}")
        value = quote(str(args.pop(name)), safe="")
        path = f"{path[:start]}{value}{path[end + 1 :]}"
    return path, {key: value for key, value in args.items() if value is not None}


def _response_json(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if 200 <= response.status_code < 300:
        return json.dumps(payload)

    detail = payload if isinstance(payload, dict) else {}
    message = ""
    if isinstance(detail.get("detail"), str):
        message = detail["detail"]
    elif isinstance(detail.get("error"), dict):
        message = detail["error"].get("message") or ""
    if not message:
        message = response.reason_phrase or ""
    return _error_json(
        _HTTP_TO_TOOL_ERROR.get(response.status_code, "service.internal_error"),
        message,
        response.status_code,
        response.headers.get(REQUEST_ID_HEADER, ""),
    )


def _error_json(code: str, message: str, status: int, request_id: str = "") -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "status": status,
                "request_id": request_id,
            },
        }
    )


def env_ready() -> bool:
    """All required env vars are set with non-empty values."""
    if not (os.environ.get(MAC_URL_ENV) or os.environ.get(MAC_HUB_URL_ENV)):
        return False
    if not _resolve_token():
        return False
    if not os.environ.get(HERMES_INSTANCE_ENV):
        return False
    return True


def check_mac_available() -> bool:
    if not env_ready():
        return False
    base = (os.environ.get(MAC_URL_ENV) or os.environ.get(MAC_HUB_URL_ENV, "")).rstrip("/")
    request = urllib.request.Request(f"{base}/health", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HEALTH_CHECK_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _resolve_token() -> str:
    for name in MAC_TOKEN_ENVS:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def hermes_instance_id() -> str:
    return os.environ.get(HERMES_INSTANCE_ENV, "")


_CLIENTS: dict[int, MacClient] = {}


def get_client() -> MacClient:
    loop = asyncio.get_running_loop()
    key = id(loop)
    client = _CLIENTS.get(key)
    if client is None:
        base = os.environ.get(MAC_URL_ENV) or os.environ.get(MAC_HUB_URL_ENV, "")
        client = MacClient(base_url=base, token=_resolve_token())
        _CLIENTS[key] = client
    return client
