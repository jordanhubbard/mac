"""The router's Anthropic-shaped front door: ``POST /v1/messages``.

Claude Code speaks the Anthropic Messages API, not the OpenAI chat API the
rest of the router serves. A task sandbox gets a per-task inference token, not
the hub's Anthropic key, so the hub forwards the request to the configured
Anthropic provider and adds the key itself (the same arrangement opencode has
through ``/v1/chat/completions``).

The body is passed through unchanged apart from the model id, which goes
through the provider's aliases (``azure/anthropic/claude-opus-4-8`` ->
``claude-opus-4-8``). Streaming responses are relayed byte for byte.

The provider is the one named by ``MAC_ROUTER_ANTHROPIC_PROVIDER``, or else the
first configured provider whose base URL is on ``api.anthropic.com``. With
neither, the routes are not mounted, so a hub without an Anthropic key answers
404 rather than pretending.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from fastapi import Request

from mac.provider_router import Provider, providers_from_env

logger = logging.getLogger("mac.router")

ANTHROPIC_HOST = "api.anthropic.com"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
#: Request headers relayed upstream. Everything else, in particular the
#: caller's Authorization, stays at the hub.
FORWARDED_HEADERS = ("anthropic-version", "anthropic-beta")
PATHS = ("/messages", "/messages/count_tokens")

SecretResolver = Callable[[str], Optional[str]]


def select_anthropic_provider(
    providers: List[Provider], env: Mapping[str, str]
) -> Optional[Provider]:
    """The provider that serves /v1/messages, or None."""
    wanted = str(env.get("MAC_ROUTER_ANTHROPIC_PROVIDER") or "").strip()
    if wanted:
        for provider in providers:
            if provider.name == wanted and provider.enabled:
                return provider
        return None
    for provider in sorted(providers, key=lambda p: (p.priority, p.name)):
        if provider.enabled and (urlparse(provider.base_url).hostname or "") == ANTHROPIC_HOST:
            return provider
    return None


def _upstream_headers(provider: Provider, key: str, incoming: Mapping[str, str]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    for name in FORWARDED_HEADERS:
        value = incoming.get(name)
        if value:
            headers[name] = value
    headers.setdefault("anthropic-version", DEFAULT_ANTHROPIC_VERSION)
    if key:
        headers["x-api-key"] = key
    return headers


def _drain(resp: Any, chunk_size: int = 8192) -> Iterator[bytes]:
    try:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def forward(
    provider: Provider,
    path: str,
    payload: Dict[str, Any],
    incoming_headers: Mapping[str, str],
    *,
    key: str,
    stream: bool,
    timeout: float,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Tuple[int, Any, str]:
    """Send one request upstream. Returns ``(status, body_or_chunks, media_type)``.

    On a 2xx streaming reply the body is an iterator of raw bytes; otherwise it
    is a decoded JSON object. A transport failure is reported as 502.
    """
    body = dict(payload)
    model = str(body.get("model") or "").strip()
    if model:
        body["model"] = provider.upstream_model(model)
    url = provider.base_url.rstrip("/") + path
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=_upstream_headers(provider, key, incoming_headers),
        method="POST",
    )
    try:
        resp = opener(request, timeout=timeout)  # noqa: S310 (operator-configured upstream)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail) if detail.strip() else {}
        except ValueError:
            parsed = {}
        if not isinstance(parsed, dict) or not parsed:
            parsed = {"type": "error", "error": {"type": "api_error", "message": detail[:500]}}
        return exc.code, parsed, "application/json"
    except Exception as exc:  # noqa: BLE001 - unreachable/timeout
        return (
            502,
            {"type": "error", "error": {"type": "api_error", "message": "upstream: %s" % exc}},
            "application/json",
        )
    if stream:
        return resp.status, _drain(resp), "text/event-stream"
    try:
        raw = resp.read().decode("utf-8", "replace")
    finally:
        resp.close()
    return resp.status, (json.loads(raw) if raw.strip() else {}), "application/json"


def mount_anthropic_messages(
    app: Any,
    *,
    env: Optional[Mapping[str, str]] = None,
    secret_resolver: Optional[SecretResolver] = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> bool:
    """Mount ``POST /v1/messages`` and ``/v1/messages/count_tokens``."""
    import os

    from fastapi.responses import JSONResponse, StreamingResponse

    from mac.router_app import resolve_provider_key

    env = os.environ if env is None else env
    provider = select_anthropic_provider(providers_from_env(dict(env)), env)
    if provider is None:
        return False
    try:
        timeout = float(env.get("MAC_ROUTER_ANTHROPIC_TIMEOUT_SECONDS") or 600)
    except ValueError:
        timeout = 600.0

    def _handle(path: str, request: Request, payload: Dict[str, Any]) -> Any:
        principal = getattr(request.state, "principal", None)
        stream = bool(payload.get("stream")) and path == "/messages"
        status, body, media_type = forward(
            provider,
            path,
            payload,
            request.headers,
            key=resolve_provider_key(provider, secret_resolver),
            stream=stream,
            timeout=timeout,
            opener=opener,
        )
        logger.info(
            "router: route path=/v1%s provider=%s model=%s status=%s agent=%s",
            path,
            provider.name,
            payload.get("model") or "",
            status,
            getattr(principal, "agent_id", None) or "",
        )
        if media_type == "text/event-stream" and 200 <= status < 300:
            return StreamingResponse(body, media_type=media_type)
        return JSONResponse(body if isinstance(body, dict) else {}, status_code=status)

    async def _read(request: Request) -> Dict[str, Any]:
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        return payload if isinstance(payload, dict) else {}

    @app.post("/v1/messages")
    async def _messages(request: Request) -> Any:  # noqa: ANN401
        payload = await _read(request)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(_handle, "/messages", request, payload)

    @app.post("/v1/messages/count_tokens")
    async def _count_tokens(request: Request) -> Any:  # noqa: ANN401
        payload = await _read(request)
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(_handle, "/messages/count_tokens", request, payload)

    return True
