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
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, Tuple

from mac.provider_router import Provider, ProviderRouter, providers_from_env

# Per-route visibility: every routed request logs which provider served it, so
# "when did the hub use madmax?" is answerable from the mac service journal
# (`journalctl -u mac | grep 'router: route'`). Attributable per provider, unlike
# a provider's own /metrics which can't tell hub traffic from direct traffic.
logger = logging.getLogger("mac.router")


def _configure_route_logging() -> None:
    """Make ``router: route …`` lines reach the mac service journal.

    uvicorn's ``--log-level info`` only configures uvicorn's own loggers; a
    non-uvicorn logger like ``mac.router`` propagates to the root, whose
    last-resort handler emits WARNING+ only — so INFO route lines were dropped.
    Attach a dedicated INFO stderr handler (once) when the router mounts so the
    per-route provider attribution is visible in ``journalctl -u mac``."""
    if getattr(logger, "_mac_route_logging_configured", False):
        return
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    logger._mac_route_logging_configured = True


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


# th-merge-06: provider-healthy but THIS model is unusable (not found / unprocessable).
# For a wildcard request the router substitutes the next model in the ladder; the
# provider is NOT penalised. 400 is intentionally excluded (a genuine bad request
# should be returned, not retried across the whole ladder).
_MODEL_RETRY_CODES = frozenset({404, 422})


class ProviderProxy:
    def __init__(
        self,
        router: ProviderRouter,
        forward_fn: ForwardFn,
        *,
        stream_forward_fn: Optional[StreamForwardFn] = None,
        default_model: str = "",
        wildcard_models: Tuple[str, ...] = (),
        timeout: float = 60.0,
        stream_timeout: float = 300.0,
    ) -> None:
        self._router = router
        self._forward = forward_fn
        self._stream_forward = stream_forward_fn
        self._default_model = (default_model or "").strip()
        # The wildcard ladder: ordered concrete models a "*" request resolves to,
        # tried rank-0 first, substituting the next on a model-level failure.
        self._wildcard_models = tuple(m for m in wildcard_models if m)
        self._timeout = timeout
        self._stream_timeout = stream_timeout

    # -- model resolution ----------------------------------------------------

    def _candidate_models(self, payload: Dict[str, Any]) -> list:
        """Ordered concrete models to try. A concrete request is a single
        candidate (no substitution). A wildcard/empty request expands to the
        ladder, else the single default model, else ``"*"`` itself (passthrough
        — e.g. wrapping TokenHub, which resolves the wildcard upstream)."""
        model = str(payload.get("model") or "").strip()
        if model and model != "*":
            return [model]
        if self._wildcard_models:
            return list(self._wildcard_models)
        if self._default_model:
            return [self._default_model]
        return ["*"]

    def _route(self, path: str, payload: Dict[str, Any], forward, timeout: float) -> Tuple[int, Any]:
        """Shared routing for streaming + non-streaming. Walks the candidate
        models (wildcard ladder); for each, walks providers with failover +
        breaker. Returns ``(status, body_or_iterator)``."""
        candidates = self._candidate_models(payload)
        last: Optional[Tuple[int, Any]] = None
        for idx, model in enumerate(candidates):
            is_last = idx == len(candidates) - 1
            outgoing = {**payload, "model": model}
            attempts = []
            provider_answered = False
            # Bounded: one try per provider (+1 so a half-open probe can be
            # re-selected after another provider is tried).
            for _ in range(len(self._router.provider_names()) + 1):
                provider = self._router.select(model)
                if provider is None:
                    break
                status, obj = forward(provider, path, outgoing, timeout=timeout)
                if _is_provider_failure(status):
                    self._router.record_failure(provider.name)
                    attempts.append({"provider": provider.name, "status": status})
                    logger.info("route model=%s provider=%s status=%s failover", model, provider.name, status)
                    continue
                # The provider answered (healthy), so close its breaker.
                self._router.record_success(provider.name)
                provider_answered = True
                logger.info("route model=%s provider=%s status=%s", model, provider.name, status)
                if int(status) in _MODEL_RETRY_CODES and not is_last:
                    last = (int(status), obj)  # this model is unusable; substitute the next
                    break
                return int(status), obj
            if not provider_answered:
                # Every eligible provider failed or is open for this model. The
                # same providers serve the other models, so fail fast rather than
                # walk the rest of the ladder against dead providers.
                return 503, self._failfast_body(model, attempts)
            # provider answered with a model-retry code and more models remain:
            # fall through to the next candidate.
        return last if last is not None else (503, self._failfast_body("*", []))

    def complete(self, path: str, payload: Dict[str, Any]) -> Tuple[int, Any]:
        """Route one request. ``path`` is the suffix after the provider base_url
        (e.g. ``/chat/completions``). Returns ``(status_code, body)``."""
        return self._route(path, payload, self._forward, self._timeout)

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
        return self._route(path, {**payload, "stream": True}, self._stream_forward, self._stream_timeout)

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
    wildcard_models = tuple(
        m.strip() for m in (env.get("MAC_ROUTER_WILDCARD_MODELS") or "").split("|") if m.strip()
    )
    return ProviderProxy(
        router,
        _fwd,
        stream_forward_fn=_sfwd,
        default_model=(env.get("MAC_ROUTER_DEFAULT_MODEL") or "").strip(),
        wildcard_models=wildcard_models,
        timeout=_f("MAC_ROUTER_TIMEOUT", 60.0),
        stream_timeout=_f("MAC_ROUTER_STREAM_TIMEOUT", 300.0),
    )


# Modality reverse-proxies the hub exposes beyond chat/embeddings. Each is a
# transparent ``POST <prefix>/{path}`` forwarder to a configurable upstream with a
# vault-resolved key, so spokes route through the HUB and never hold the upstream
# key locally (Stream B "hub serves them"). Each is INDEPENDENT and gated on its
# own UPSTREAM+KEY (no-op otherwise) — the upstream URL + key are supplied at
# cluster init (distinct from the chat/OpenAI key), since hosted paths differ by
# account/NIM. (name, route_prefix, upstream_env, key_env, timeout_env, default_timeout)
_MODALITY_PROXIES = (
    ("image", "/v1/genai", "MAC_ROUTER_IMAGE_UPSTREAM", "MAC_ROUTER_IMAGE_KEY", "MAC_ROUTER_IMAGE_TIMEOUT", 180.0),
    ("audio", "/v1/audio", "MAC_ROUTER_AUDIO_UPSTREAM", "MAC_ROUTER_AUDIO_KEY", "MAC_ROUTER_AUDIO_TIMEOUT", 120.0),
    ("video", "/v1/video", "MAC_ROUTER_VIDEO_UPSTREAM", "MAC_ROUTER_VIDEO_KEY", "MAC_ROUTER_VIDEO_TIMEOUT", 600.0),
)


def _mount_one_modality_proxy(
    app: Any,
    *,
    name: str,
    route_prefix: str,
    upstream_env: str,
    key_env: str,
    timeout_env: str,
    default_timeout: float,
    env: Dict[str, str],
    secret_resolver: Optional[SecretResolver],
) -> bool:
    """Mount ``POST {route_prefix}/{path}`` → ``env[upstream_env]`` with the
    vault key ``env[key_env]`` (``secret:<name>``). The /v1/* prefix is
    agent-scoped (api._required_scope), so a spoke presents its hub token and the
    hub swaps in the vault key on the way upstream. No-op if either is unset."""
    upstream = (env.get(upstream_env) or "").strip().rstrip("/")
    key_spec = (env.get(key_env) or "").strip()
    if not upstream or not key_spec:
        return False
    try:
        timeout = float(env.get(timeout_env) or default_timeout)
    except ValueError:
        timeout = default_timeout
    provider = Provider(name=name, base_url=upstream, api_key_env=key_spec)
    from fastapi.responses import JSONResponse

    @app.post(route_prefix + "/{path:path}")
    def _proxy(path: str, body: Dict[str, Any]) -> Any:  # noqa: ANN401
        status, out = urllib_forwarder(
            provider, "/" + path, body, timeout=timeout, secret_resolver=secret_resolver
        )
        return JSONResponse(out if isinstance(out, dict) else {}, status_code=status or 502)

    return True


def mount_image_proxy(
    app: Any,
    *,
    env: Optional[Dict[str, str]] = None,
    secret_resolver: Optional[SecretResolver] = None,
) -> bool:
    """Mount the hub's modality reverse-proxies (image `/v1/genai`, speech
    `/v1/audio` for ASR/TTS, and video `/v1/video`). Synchronous POSTs returning
    inline payloads, so transparent reverse-proxies suffice. Returns True if any
    mounted. Kept named for back-compat; each modality is independently gated."""
    env = env or os.environ
    mounted = False
    for name, route_prefix, upstream_env, key_env, timeout_env, default_timeout in _MODALITY_PROXIES:
        if _mount_one_modality_proxy(
            app,
            name=name,
            route_prefix=route_prefix,
            upstream_env=upstream_env,
            key_env=key_env,
            timeout_env=timeout_env,
            default_timeout=default_timeout,
            env=env,
            secret_resolver=secret_resolver,
        ):
            mounted = True
    return mounted


def mount_router(
    app: Any,
    *,
    env: Optional[Dict[str, str]] = None,
    proxy: Optional[ProviderProxy] = None,
    secret_resolver: Optional[SecretResolver] = None,
) -> bool:
    """Mount /v1/chat/completions + /v1/embeddings (+ /v1/genai image proxy) on
    ``app`` when MAC_ROUTER_BACKEND=inproc. Returns True if anything mounted.
    Default backend is 'tokenhub' → no-op, so an existing fleet is unchanged until
    flipped."""
    env = env or os.environ
    if (env.get("MAC_ROUTER_BACKEND") or "tokenhub").strip().lower() != "inproc":
        return False
    _configure_route_logging()
    # The image proxy is independent of the chat providers (a fixed upstream, no
    # failover), so mount it even when no chat providers are configured.
    mounted = mount_image_proxy(app, env=env, secret_resolver=secret_resolver)
    proxy = proxy or build_proxy_from_env(env, secret_resolver=secret_resolver)
    if proxy is None:
        return mounted
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
