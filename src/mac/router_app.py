"""th-merge-02: the in-mac OpenAI-compatible front door.

Makes the routing brain (th-merge-03 `ProviderRouter`) load-bearing: a
`/v1/chat/completions` + `/v1/embeddings` surface that selects a provider,
forwards the request, **fails over** to the next provider on a provider-side
failure, **fails fast** when every provider is down, and feeds success/failure
back into the recovering circuit breaker.

It is **opt-in**: mounted only when `MAC_ROUTER_BACKEND=inproc` (default
`tokenhub`, so an existing fleet routes through the standalone service unchanged
until this is validated and flipped). Provider keys come from env for now; the
encrypted vault is th-merge-04.

Two things make it a genuine TokenHub replacement (not just a proxy), both
required because the real upstream — unlike TokenHub — does NOT accept
`model="*"` and the agent loop streams:

* **Wildcard resolution.** The agent sends `model="*"`; the upstream rejects it
  (401). `default_model` (`MAC_ROUTER_DEFAULT_MODEL`) resolves `"*"`/empty to a
  concrete model id the provider serves before forwarding. Provider *selection*
  still uses the requested model string. (Full per-model ladder failover —
  rank-0 down → rank-1 — is th-merge-06; this resolves to rank-0.)
* **Streaming passthrough.** The interactive gateway runs the agent loop through
  `responses.stream()` → SSE. `stream_complete` opens the upstream as a stream,
  applies failover/breaker on the *status line* (before any bytes flow), then
  passes the `text/event-stream` chunks straight through.

Failure semantics (what trips the breaker / triggers failover):
* network error / timeout (status ``None``), HTTP 429, or HTTP >= 500 → the
  provider failed: record_failure + try the next one.
* 2xx or other 4xx → the provider *answered* (a 4xx is a bad request, not a
  health problem): record_success and return it as-is (no pointless failover).

The transport is injected (`forward_fn` / `stream_forward_fn`) so the proxy
logic is unit-testable without a live provider.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, Tuple

from mac.provider_router import Provider, ProviderRouter, providers_from_env

__all__ = [
    "ProviderProxy",
    "resolve_provider_key",
    "urllib_forwarder",
    "urllib_stream_forwarder",
    "build_proxy_from_env",
    "mount_router",
]

# forward_fn(provider, path, payload, *, timeout) -> (status_code|None, body)
ForwardFn = Callable[..., Tuple[Optional[int], Any]]
# stream_forward_fn(provider, path, payload, *, timeout) -> (status|None, iter[bytes] | error-body)
StreamForwardFn = Callable[..., Tuple[Optional[int], Any]]
# secret_resolver(name) -> plaintext | None  (th-merge-04: audited hub key escrow)
SecretResolver = Callable[[str], Optional[str]]

_SECRET_PREFIX = "secret:"


def resolve_provider_key(provider: Provider, secret_resolver: Optional[SecretResolver] = None) -> str:
    """Resolve a provider's bearer key. ``provider.api_key_env`` is either an env
    var name (``NVIDIA_API_KEY``) or, for escrowed upstream keys, ``secret:<name>``
    which is fetched from the in-mac encrypted key store (SecretsService) via the
    injected ``secret_resolver`` — so the upstream credential is never stored in
    plaintext env. Returns "" when unresolved (forwarder sends no Authorization)."""
    spec = (provider.api_key_env or "").strip()
    if not spec:
        return ""
    if spec.startswith(_SECRET_PREFIX):
        if secret_resolver is None:
            return ""
        return secret_resolver(spec[len(_SECRET_PREFIX):]) or ""
    return os.environ.get(spec, "")


def _is_provider_failure(status: Optional[int]) -> bool:
    """A provider-health failure (failover + breaker) vs. a request the provider
    answered. None = unreachable/timeout; 429 = rate-limited; >=500 = upstream."""
    return status is None or status == 429 or status >= 500


class ProviderProxy:
    def __init__(
        self,
        router: ProviderRouter,
        forward_fn: ForwardFn,
        *,
        stream_forward_fn: Optional[StreamForwardFn] = None,
        default_model: str = "",
        timeout: float = 60.0,
        stream_timeout: float = 300.0,
    ) -> None:
        self._router = router
        self._forward = forward_fn
        self._stream_forward = stream_forward_fn
        self._default_model = (default_model or "").strip()
        self._timeout = timeout
        self._stream_timeout = stream_timeout

    # -- model resolution ----------------------------------------------------

    def _resolve_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve a wildcard/empty model to the configured concrete model so
        the upstream (which rejects ``model="*"``) accepts it. A concrete model
        is forwarded untouched."""
        model = str(payload.get("model") or "").strip()
        if model in ("", "*") and self._default_model:
            return {**payload, "model": self._default_model}
        return payload

    # -- non-streaming -------------------------------------------------------

    def complete(self, path: str, payload: Dict[str, Any]) -> Tuple[int, Any]:
        """Route one request. ``path`` is the suffix after the provider base_url
        (e.g. ``/chat/completions``). Returns ``(status_code, body)``."""
        select_model = str(payload.get("model") or "*")
        outgoing = self._resolve_payload(payload)
        attempts = []
        # Bounded: at most one try per provider (+1 to allow a half-open probe
        # to be re-selected after another provider is tried).
        for _ in range(len(self._router.provider_names()) + 1):
            provider = self._router.select(select_model)
            if provider is None:
                break
            status, body = self._forward(provider, path, outgoing, timeout=self._timeout)
            if not _is_provider_failure(status):
                self._router.record_success(provider.name)
                return int(status), body
            self._router.record_failure(provider.name)
            attempts.append({"provider": provider.name, "status": status})
        return 503, self._failfast_body(select_model, attempts)

    # -- streaming -----------------------------------------------------------

    def stream_complete(self, path: str, payload: Dict[str, Any]) -> Tuple[int, Any]:
        """Route one streaming request. On 2xx the second element is an iterator
        of raw upstream byte chunks (pass straight to the client as
        ``text/event-stream``); otherwise it is an error/body dict.

        Failover + breaker decisions are made on the *status line* — before any
        bytes are streamed to the client. Once a 2xx stream has started we are
        committed to it (the client is already receiving bytes)."""
        if self._stream_forward is None:
            # No streaming transport wired: degrade to a buffered completion so a
            # stream=true request still gets an answer rather than failing.
            return self.complete(path, {**payload, "stream": False})
        select_model = str(payload.get("model") or "*")
        outgoing = self._resolve_payload({**payload, "stream": True})
        attempts = []
        for _ in range(len(self._router.provider_names()) + 1):
            provider = self._router.select(select_model)
            if provider is None:
                break
            status, obj = self._stream_forward(provider, path, outgoing, timeout=self._stream_timeout)
            if not _is_provider_failure(status):
                self._router.record_success(provider.name)
                return int(status), obj
            self._router.record_failure(provider.name)
            attempts.append({"provider": provider.name, "status": status})
        return 503, self._failfast_body(select_model, attempts)

    @staticmethod
    def _failfast_body(model: str, attempts) -> Dict[str, Any]:
        return {
            "error": {
                "message": "no provider could serve model=%s" % model,
                "type": "all_providers_unavailable",
                "attempts": attempts,
            }
        }


def urllib_forwarder(
    provider: Provider,
    path: str,
    payload: Dict[str, Any],
    *,
    timeout: float = 60.0,
    secret_resolver: Optional[SecretResolver] = None,
):
    """Real HTTP forward to ``provider.base_url + path`` with its bearer key.
    Returns ``(status_code|None, body)``; status None means unreachable/timeout."""
    url = provider.base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = resolve_provider_key(provider, secret_resolver)
    if key:
        headers["Authorization"] = "Bearer %s" % key
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-configured upstream)
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(detail) if detail.strip() else {"error": {"message": exc.reason}}
        except Exception:
            body = {"error": {"message": detail[:500] or exc.reason}}
        return exc.code, body
    except Exception as exc:  # noqa: BLE001 - connection/timeout -> treat as provider failure
        return None, {"error": {"message": str(exc), "type": "upstream_unreachable"}}


def _drain_chunks(resp: Any, chunk_size: int = 8192) -> Iterator[bytes]:
    """Yield raw bytes from an open urllib response, then close it. Kept as a
    generator so the connection stays open for the lifetime of the stream."""
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


def urllib_stream_forwarder(
    provider: Provider,
    path: str,
    payload: Dict[str, Any],
    *,
    timeout: float = 300.0,
    secret_resolver: Optional[SecretResolver] = None,
):
    """Streaming HTTP forward. On a 2xx status line returns
    ``(status, iterator_of_bytes)`` with the connection held open; on an HTTP
    error returns ``(code, body)``; on a transport failure ``(None, body)``.

    The status line is available as soon as ``urlopen`` returns (headers
    received), which is exactly where the breaker/failover decision must be made
    — before the client sees any bytes."""
    url = provider.base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    key = resolve_provider_key(provider, secret_resolver)
    if key:
        headers["Authorization"] = "Bearer %s" % key
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 (operator-configured upstream); not `with` — stream stays open
        return resp.status, _drain_chunks(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(detail) if detail.strip() else {"error": {"message": exc.reason}}
        except Exception:
            body = {"error": {"message": detail[:500] or exc.reason}}
        return exc.code, body
    except Exception as exc:  # noqa: BLE001 - connection/timeout -> treat as provider failure
        return None, {"error": {"message": str(exc), "type": "upstream_unreachable"}}


def build_proxy_from_env(
    env: Optional[Dict[str, str]] = None,
    *,
    secret_resolver: Optional[SecretResolver] = None,
) -> Optional[ProviderProxy]:
    """Build a ProviderProxy from MAC_ROUTER_* env; None when no providers are
    configured. ``secret_resolver`` (th-merge-04) lets a provider's ``key=`` be
    ``secret:<name>``, resolved from the in-mac encrypted key store at use."""
    env = env or os.environ
    providers = providers_from_env(env)
    if not providers:
        return None

    def _f(name: str, default: float) -> float:
        try:
            return float(env.get(name) or default)
        except ValueError:
            return default

    def _fwd(provider, path, payload, *, timeout=60.0):
        return urllib_forwarder(provider, path, payload, timeout=timeout, secret_resolver=secret_resolver)

    def _sfwd(provider, path, payload, *, timeout=300.0):
        return urllib_stream_forwarder(provider, path, payload, timeout=timeout, secret_resolver=secret_resolver)

    router = ProviderRouter(
        providers,
        failure_threshold=int(_f("MAC_ROUTER_FAILURE_THRESHOLD", 3)),
        cooldown_seconds=_f("MAC_ROUTER_COOLDOWN_SECONDS", 30.0),
    )
    return ProviderProxy(
        router,
        _fwd,
        stream_forward_fn=_sfwd,
        default_model=(env.get("MAC_ROUTER_DEFAULT_MODEL") or "").strip(),
        timeout=_f("MAC_ROUTER_TIMEOUT", 60.0),
        stream_timeout=_f("MAC_ROUTER_STREAM_TIMEOUT", 300.0),
    )


def mount_router(
    app: Any,
    *,
    env: Optional[Dict[str, str]] = None,
    proxy: Optional[ProviderProxy] = None,
    secret_resolver: Optional[SecretResolver] = None,
) -> bool:
    """Mount /v1/chat/completions + /v1/embeddings on ``app`` when
    MAC_ROUTER_BACKEND=inproc. Returns True if mounted. Default backend is
    'tokenhub' → no-op, so an existing fleet is unchanged until flipped."""
    env = env or os.environ
    if (env.get("MAC_ROUTER_BACKEND") or "tokenhub").strip().lower() != "inproc":
        return False
    proxy = proxy or build_proxy_from_env(env, secret_resolver=secret_resolver)
    if proxy is None:
        return False
    from fastapi.responses import JSONResponse, StreamingResponse

    @app.post("/v1/chat/completions")
    def _chat(body: Dict[str, Any]) -> Any:  # noqa: ANN401
        if body.get("stream"):
            status, obj = proxy.stream_complete("/chat/completions", body)
            if status == 200 and not isinstance(obj, dict):
                return StreamingResponse(obj, media_type="text/event-stream")
            return JSONResponse(obj if isinstance(obj, dict) else {}, status_code=status)
        status, out = proxy.complete("/chat/completions", body)
        return JSONResponse(out, status_code=status)

    @app.post("/v1/embeddings")
    def _embeddings(body: Dict[str, Any]) -> Any:  # noqa: ANN401
        status, out = proxy.complete("/embeddings", body)
        return JSONResponse(out, status_code=status)

    return True
