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
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, Tuple

from fastapi import Body, Request

from mac.provider_router import Provider, ProviderRouter, providers_from_env

# Per-route visibility: every routed request logs which provider served it, so
# "when did the hub use hostb?" is answerable from the mac service journal
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
    "mac_route_context_headers",
]

# forward_fn(provider, path, payload, *, timeout) -> (status_code|None, body)
ForwardFn = Callable[..., Tuple[Optional[int], Any]]
# stream_forward_fn(provider, path, payload, *, timeout) -> (status|None, iter[bytes] | error-body)
StreamForwardFn = Callable[..., Tuple[Optional[int], Any]]
# secret_resolver(name) -> plaintext | None  (th-merge-04: audited hub key escrow)
SecretResolver = Callable[[str], Optional[str]]
# route_observer(detail) -> None. Receives only routing metadata, never prompt content.
RouteObserver = Callable[[Dict[str, Any]], None]

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


# th-merge: params some OpenAI-compatible upstreams reject with
# 400 "unknown_parameter" (e.g. opencode sends `reasoningSummary` for GPT-5
# reasoning models). The router is the single chokepoint that sanitizes the
# request body, so no per-agent config has to know each upstream's quirks.
# Overridable via MAC_ROUTER_DROP_PARAMS (comma-separated) at build time.
_DEFAULT_DROP_PARAMS = ("reasoningSummary", "reasoning_summary")
_INTERNAL_BODY_PARAMS = ("_mac_context", "mac_context")
_DEFAULT_STRONG_WILDCARD_MODEL = "azure/anthropic/claude-sonnet-4-6"
_FORBIDDEN_WILDCARD_MODELS = ("gpt-4.1-mini",)


def _forbidden_wildcard_model(model: str) -> bool:
    normalized = str(model or "").strip().lower().replace("_", "-")
    return any(forbidden in normalized for forbidden in _FORBIDDEN_WILDCARD_MODELS)


def _strong_wildcard_default(env: Dict[str, str]) -> str:
    for key in ("MAC_HERMES_GATEWAY_MODEL", "HERMES_INFERENCE_MODEL", "ACC_LLM_MODEL"):
        value = (env.get(key) or "").strip()
        if value and value != "*" and not _forbidden_wildcard_model(value):
            return value
    return _DEFAULT_STRONG_WILDCARD_MODEL


def _wildcard_models_from_env(env: Dict[str, str]) -> Tuple[str, ...]:
    raw_models = [
        m.strip()
        for m in (env.get("MAC_ROUTER_WILDCARD_MODELS") or "").split("|")
        if m.strip()
    ]
    models = [model for model in raw_models if not _forbidden_wildcard_model(model)]
    if raw_models and not models:
        models = [_strong_wildcard_default(env)]
    return tuple(models)


def _default_model_from_env(env: Dict[str, str]) -> str:
    value = (env.get("MAC_ROUTER_DEFAULT_MODEL") or "").strip()
    if value and value != "*" and not _forbidden_wildcard_model(value):
        return value
    return _strong_wildcard_default(env)


def _normalize_payload(payload: Dict[str, Any], drop_params: Tuple[str, ...]) -> Dict[str, Any]:
    """Strip known-unsupported top-level params before forwarding upstream.
    Leaves everything else untouched. Returns a new dict (does not mutate)."""
    if not drop_params:
        return payload
    drop = set(drop_params) | set(_INTERNAL_BODY_PARAMS)
    if not any(k in payload for k in drop):
        return payload
    return {k: v for k, v in payload.items() if k not in drop}


def _ensure_max_tokens_floor(payload: Dict[str, Any], path: str) -> Dict[str, Any]:
    """Raise an agent's chat ``max_tokens`` to a floor so large single-turn
    generations don't get capped (mac: the taskbrain build's CLI task hit
    ``finish_reason=length`` at a low default, which truncated the response
    mid tool-call and wrote no files). ``max_tokens`` is a CAP, not a target —
    raising it never forces longer output; short completions still stop at
    ``finish_reason=stop`` — so this only rescues large generations. Chat
    completions only; never touches embeddings. Floor is configurable via
    ``MAC_ROUTER_MAX_TOKENS_FLOOR`` (0 disables); default 32000."""
    if "chat/completions" not in path:
        return payload
    try:
        floor = int(os.environ.get("MAC_ROUTER_MAX_TOKENS_FLOOR", "32000") or "32000")
    except ValueError:
        floor = 32000
    if floor <= 0:
        return payload
    try:
        current = int(payload.get("max_tokens") or 0)
    except (TypeError, ValueError):
        current = 0
    if current >= floor:
        return payload
    out = dict(payload)
    out["max_tokens"] = floor
    return out


_CONTEXT_HEADER_NAMES = {
    "agent_id": "x-mac-agent-id",
    "task_id": "x-mac-task-id",
    "lease_id": "x-mac-lease-id",
    "command_id": "x-mac-command-id",
    "hermes_instance_id": "x-mac-hermes-instance-id",
    "request_id": "x-mac-request-id",
    "fleet": "x-mac-fleet",
}
_CONTEXT_BODY_KEYS = ("_mac_context", "mac_context")


def _string_value(value: Any) -> str:
    text = str(value or "").strip()
    return text[:256]


def _body_route_context(body: Dict[str, Any]) -> Dict[str, str]:
    contexts = []
    for key in _CONTEXT_BODY_KEYS:
        value = body.get(key)
        if isinstance(value, dict):
            contexts.append(value)
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for key in ("mac", "mac_context", "_mac_context"):
            value = metadata.get(key)
            if isinstance(value, dict):
                contexts.append(value)
    out: Dict[str, str] = {}
    for context in contexts:
        for key in _CONTEXT_HEADER_NAMES:
            value = _string_value(context.get(key))
            if value and key not in out:
                out[key] = value
    return out


def _route_context_from_request(request: Any, body: Dict[str, Any]) -> Dict[str, str]:
    headers = getattr(request, "headers", {}) or {}
    header_context = {
        key: _string_value(headers.get(header_name))
        for key, header_name in _CONTEXT_HEADER_NAMES.items()
        if _string_value(headers.get(header_name))
    }
    body_context = _body_route_context(body)
    context = {**body_context, **header_context}
    principal = getattr(getattr(request, "state", None), "principal", None)
    principal_agent_id = _string_value(getattr(principal, "agent_id", None))
    if principal_agent_id:
        claimed_agent_id = context.get("agent_id")
        if claimed_agent_id and claimed_agent_id != principal_agent_id:
            context["claimed_agent_id"] = claimed_agent_id
            # Log every mismatch unconditionally — ops needs to know that
            # spoofed-agent headers are reaching the router even before the
            # hard-rejection gate (MAC_ROUTER_REJECT_MISMATCHED_PRINCIPAL) is
            # enabled.  This is the first line of the audit trail.
            logger.warning(
                "route context mismatch: token bound to agent_id=%s "
                "but request header claimed agent_id=%s",
                principal_agent_id,
                claimed_agent_id,
            )
        context["agent_id"] = principal_agent_id
    return context


# ---------------------------------------------------------------------------
# Principal-mismatch rejection gate
# ---------------------------------------------------------------------------

# Env-var that enables hard rejection of requests whose X-MAC-Agent-ID header
# disagrees with the bearer-token principal.  Off by default so hubs that have
# not yet enforced token binding are unaffected; flip to "1" once all clients
# stamp headers via mac_route_context_headers().
_REJECT_MISMATCH_ENV = "MAC_ROUTER_REJECT_MISMATCHED_PRINCIPAL"


def _is_principal_mismatch_rejected(env: Optional[Dict[str, str]] = None) -> bool:
    """Return True when ``MAC_ROUTER_REJECT_MISMATCHED_PRINCIPAL`` is truthy.

    When True the router responds with HTTP 403 to any request whose
    ``X-MAC-Agent-ID`` header does not match the bearer-token principal.
    Disabled by default; enable once all Hermes/worker clients have been
    updated to stamp headers via :func:`mac_route_context_headers`.
    """
    _env: Dict[str, str] = env if env is not None else dict(os.environ)
    val = str(_env.get(_REJECT_MISMATCH_ENV) or "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _strip_internal_route_context(body: Dict[str, Any]) -> Dict[str, Any]:
    if not any(key in body for key in _CONTEXT_BODY_KEYS):
        return body
    return {key: value for key, value in body.items() if key not in _CONTEXT_BODY_KEYS}


def mac_route_context_headers(
    *,
    agent_id: Optional[str] = None,
    task_id: Optional[str] = None,
    lease_id: Optional[str] = None,
    hermes_instance_id: Optional[str] = None,
    command_id: Optional[str] = None,
    fleet: Optional[str] = None,
    request_id: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Build a dict of X-MAC-* HTTP headers for stamping on LLM completion requests.

    Hermes/worker clients that route through the hub's /v1 surface should call this
    and include the result as default_headers (OpenAI SDK) or merge them into
    the request headers before forwarding. The hub's _route_context_from_request
    extracts them and records them in the llm.route observability event, making
    every completion row attributable to an agent and task without depending on the
    bearer token's optional agent binding.

    When explicit values are not supplied, the function falls back to env-vars set by
    the worker/executor runtime (MAC_AGENT_ID, MAC_TASK_ID, MAC_LEASE_ID,
    MAC_HERMES_INSTANCE_ID). Callers that override those env-vars for a specific
    task run do not need to pass explicit arguments.

    Only non-empty values produce a header. Call sites should treat an empty return
    dict as "no context available" (single-user interactive sessions, local dev) and
    skip adding headers rather than sending blanks.
    """
    _env = os.environ if env is None else env

    def _pick(explicit: Optional[str], env_var: str) -> str:
        v = str(explicit or "").strip()
        if v:
            return v[:256]
        v = str(_env.get(env_var) or "").strip()
        return v[:256]

    candidates = {
        "x-mac-agent-id": _pick(agent_id, "MAC_AGENT_ID"),
        "x-mac-task-id": _pick(task_id, "MAC_TASK_ID"),
        "x-mac-lease-id": _pick(lease_id, "MAC_LEASE_ID"),
        "x-mac-hermes-instance-id": _pick(hermes_instance_id, "MAC_HERMES_INSTANCE_ID"),
        "x-mac-command-id": _pick(command_id, "MAC_COMMAND_ID"),
        "x-mac-fleet": _pick(fleet, "MAC_FLEET"),
        "x-mac-request-id": _pick(request_id, ""),
    }
    return {k: v for k, v in candidates.items() if v}


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
        drop_params: Tuple[str, ...] = _DEFAULT_DROP_PARAMS,
        route_observer: Optional[RouteObserver] = None,
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
        self._drop_params = tuple(drop_params)
        self._route_observer = route_observer

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

    def _route(
        self,
        path: str,
        payload: Dict[str, Any],
        forward,
        timeout: float,
        *,
        route_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any]:
        """Shared routing for streaming + non-streaming. Walks the candidate
        models (wildcard ladder); for each, walks providers with failover +
        breaker. Returns ``(status, body_or_iterator)``."""
        started = time.monotonic()
        route_context = route_context or {}
        candidates = self._candidate_models(payload)
        last: Optional[Tuple[int, Any]] = None
        last_model = "*"
        last_provider = ""
        retried_401 = False  # per-request: at most one transient-401 retry
        route_attempts = []
        for idx, model in enumerate(candidates):
            is_last = idx == len(candidates) - 1
            outgoing = _ensure_max_tokens_floor(
                {**_normalize_payload(payload, self._drop_params), "model": model}, path
            )
            attempts = []
            provider_answered = False
            # Bounded: one try per provider (+1 so a half-open probe can be
            # re-selected after another provider is tried).
            for _ in range(len(self._router.provider_names()) + 1):
                provider = self._router.select(model)
                if provider is None:
                    break
                status, obj = forward(provider, path, outgoing, timeout=timeout)
                route_attempts.append({"provider": provider.name, "model": model, "status": status})
                if _is_provider_failure(status):
                    self._router.record_failure(provider.name)
                    attempts.append({"provider": provider.name, "status": status})
                    route_attempts[-1]["outcome"] = "provider_failure"
                    logger.info("route model=%s provider=%s status=%s failover", model, provider.name, status)
                    continue
                # The provider answered (healthy), so close its breaker.
                self._router.record_success(provider.name)
                provider_answered = True
                route_attempts[-1]["outcome"] = "answered"
                logger.info("route model=%s provider=%s status=%s", model, provider.name, status)
                # A 401 from upstream is usually a real auth problem, but some
                # gateways briefly mislabel a transient backend hiccup (e.g.
                # "can't reach key-verification DB") as 401. One same-provider
                # retry turns that seconds-long blip into a non-event; a genuine
                # bad key 401s again and is returned. Bounded to a single retry.
                if int(status) == 401 and not retried_401:
                    retried_401 = True
                    logger.info("route model=%s provider=%s status=401 transient-retry", model, provider.name)
                    status, obj = forward(provider, path, outgoing, timeout=timeout)
                    route_attempts.append(
                        {
                            "provider": provider.name,
                            "model": model,
                            "status": status,
                            "outcome": "transient_retry",
                        }
                    )
                    if _is_provider_failure(status):
                        self._router.record_failure(provider.name)
                        attempts.append({"provider": provider.name, "status": status})
                        route_attempts[-1]["outcome"] = "provider_failure_after_retry"
                        logger.info("route model=%s provider=%s status=%s failover", model, provider.name, status)
                        continue
                if int(status) in _MODEL_RETRY_CODES and not is_last:
                    last = (int(status), obj)  # this model is unusable; substitute the next
                    last_model = model
                    last_provider = provider.name
                    route_attempts[-1]["outcome"] = "model_retry"
                    break
                return self._observed_return(
                    int(status),
                    obj,
                    path=path,
                    payload=payload,
                    resolved_model=model,
                    provider=provider.name,
                    started=started,
                    route_context=route_context,
                    attempts=route_attempts,
                )
            if not provider_answered:
                # Every eligible provider failed or is open for this model. The
                # same providers serve the other models, so fail fast rather than
                # walk the rest of the ladder against dead providers.
                body = self._failfast_body(model, attempts)
                return self._observed_return(
                    503,
                    body,
                    path=path,
                    payload=payload,
                    resolved_model=model,
                    provider="",
                    started=started,
                    route_context=route_context,
                    attempts=route_attempts,
                    outcome="all_providers_unavailable",
                )
            # provider answered with a model-retry code and more models remain:
            # fall through to the next candidate.
        if last is not None:
            return self._observed_return(
                int(last[0]),
                last[1],
                path=path,
                payload=payload,
                resolved_model=last_model,
                provider=last_provider,
                started=started,
                route_context=route_context,
                attempts=route_attempts,
                outcome="model_unavailable",
            )
        body = self._failfast_body("*", [])
        return self._observed_return(
            503,
            body,
            path=path,
            payload=payload,
            resolved_model="*",
            provider="",
            started=started,
            route_context=route_context,
            attempts=route_attempts,
            outcome="all_providers_unavailable",
        )

    def complete(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        route_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any]:
        """Route one request. ``path`` is the suffix after the provider base_url
        (e.g. ``/chat/completions``). Returns ``(status_code, body)``."""
        return self._route(path, payload, self._forward, self._timeout, route_context=route_context)

    def stream_complete(
        self,
        path: str,
        payload: Dict[str, Any],
        *,
        route_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any]:
        """Route one streaming request. On 2xx the second element is an iterator
        of raw upstream byte chunks (pass straight to the client as
        ``text/event-stream``); otherwise it is an error/body dict.

        Failover + breaker decisions are made on the *status line* — before any
        bytes are streamed to the client. Once a 2xx stream has started we are
        committed to it (the client is already receiving bytes)."""
        if self._stream_forward is None:
            # No streaming transport wired: degrade to a buffered completion so a
            # stream=true request still gets an answer rather than failing.
            return self.complete(path, {**payload, "stream": False}, route_context=route_context)
        return self._route(
            path,
            {**payload, "stream": True},
            self._stream_forward,
            self._stream_timeout,
            route_context=route_context,
        )

    def _observed_return(
        self,
        status: int,
        obj: Any,
        *,
        path: str,
        payload: Dict[str, Any],
        resolved_model: str,
        provider: str,
        started: float,
        route_context: Dict[str, Any],
        attempts: Iterable[Dict[str, Any]],
        outcome: str = "",
    ) -> Tuple[int, Any]:
        self._emit_route_observation(
            path=path,
            payload=payload,
            resolved_model=resolved_model,
            provider=provider,
            status=status,
            body=obj,
            started=started,
            route_context=route_context,
            attempts=attempts,
            outcome=outcome or self._outcome_for_status(status),
        )
        return status, obj

    def _emit_route_observation(
        self,
        *,
        path: str,
        payload: Dict[str, Any],
        resolved_model: str,
        provider: str,
        status: int,
        body: Any,
        started: float,
        route_context: Dict[str, Any],
        attempts: Iterable[Dict[str, Any]],
        outcome: str,
    ) -> None:
        if self._route_observer is None:
            return
        detail: Dict[str, Any] = {
            "schema": "mac.llm_route.v1",
            "path": path,
            "stream": bool(payload.get("stream")),
            "requested_model": str(payload.get("model") or ""),
            "resolved_model": resolved_model,
            "provider": provider,
            "status_code": status,
            "outcome": outcome,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
            "attempts": list(attempts),
        }
        for key in (
            "agent_id",
            "task_id",
            "lease_id",
            "command_id",
            "hermes_instance_id",
            "request_id",
            "fleet",
            "claimed_agent_id",
        ):
            value = route_context.get(key)
            if value:
                detail[key] = value
        if isinstance(body, dict):
            response_model = body.get("model")
            if response_model:
                detail["response_model"] = str(response_model)
            usage = body.get("usage")
            if isinstance(usage, dict):
                detail["usage"] = {
                    str(k): v
                    for k, v in usage.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                }
        try:
            self._route_observer(detail)
        except Exception:  # noqa: BLE001 - observability must not break inference
            logger.warning("route observer failed", exc_info=True)

    @staticmethod
    def _outcome_for_status(status: int) -> str:
        if 200 <= status < 300:
            return "success"
        if status == 429 or status >= 500:
            return "provider_error"
        if 400 <= status < 500:
            return "client_error"
        return "answered"

    @staticmethod
    def _failfast_body(model: str, attempts) -> Dict[str, Any]:
        return {
            "error": {
                "message": "no provider could serve model=%s" % model,
                "type": "all_providers_unavailable",
                "attempts": attempts,
            }
        }


def _encode_multipart(spec: Dict[str, Any]) -> tuple:
    """Encode a ``{"fields": {...}, "file": {name,filename,content_type,b64}}``
    spec as multipart/form-data. Returns ``(content_type_header, body_bytes)``."""
    import base64
    import secrets

    boundary = "----macmedia%s" % secrets.token_hex(12)
    crlf = b"\r\n"
    parts: List[bytes] = []
    for key, value in (spec.get("fields") or {}).items():
        parts.append(
            ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s'
             % (boundary, key, value)).encode("utf-8") + crlf
        )
    f = spec.get("file")
    if isinstance(f, dict):
        header = (
            '--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            'Content-Type: %s\r\n\r\n'
            % (boundary, f.get("name", "file"), f.get("filename", "upload.bin"),
               f.get("content_type", "application/octet-stream"))
        ).encode("utf-8")
        parts.append(header + base64.b64decode(f.get("b64") or "") + crlf)
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return "multipart/form-data; boundary=%s" % boundary, b"".join(parts)


def urllib_forwarder(
    provider: Provider,
    path: str,
    payload: Dict[str, Any],
    *,
    timeout: float = 60.0,
    secret_resolver: Optional[SecretResolver] = None,
    auth_scheme: str = "Bearer",
    binary_ok: bool = False,
):
    """Real HTTP forward to ``provider.base_url + path`` with its bearer key.
    Returns ``(status_code|None, body)``; status None means unreachable/timeout.
    ``auth_scheme`` is the Authorization scheme word (default ``Bearer``; FAL's
    media API needs ``Key``). When ``binary_ok`` (audio/video ops), a non-JSON
    upstream body is returned wrapped as ``{"__media_bytes__": <base64>,
    "content_type": ...}`` instead of being utf-8/json decoded."""
    url = provider.base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = resolve_provider_key(provider, secret_resolver)
    if key:
        headers["Authorization"] = "%s %s" % (auth_scheme or "Bearer", key)
    if isinstance(payload, dict) and "__multipart__" in payload:
        # ASR-style file upload: encode multipart/form-data instead of JSON.
        headers["Content-Type"], data = _encode_multipart(payload["__multipart__"])
    else:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-configured upstream)
            if binary_ok:
                ctype = resp.headers.get("Content-Type") or ""
                raw_bytes = resp.read()
                if "json" not in ctype.lower():
                    import base64

                    return resp.status, {
                        "__media_bytes__": base64.b64encode(raw_bytes).decode("ascii"),
                        "content_type": ctype.split(";", 1)[0].strip() or "application/octet-stream",
                        "bytes": len(raw_bytes),
                    }
                raw = raw_bytes.decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip() else {})
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
    route_observer: Optional[RouteObserver] = None,
) -> Optional[ProviderProxy]:
    """Build a ProviderProxy from MAC_ROUTER_* env; None when no providers are
    configured. ``secret_resolver`` (th-merge-04) lets a provider's ``key=`` be
    ``secret:<name>``, resolved from the in-mac encrypted key store at use."""
    env = os.environ if env is None else env
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
    wildcard_models = _wildcard_models_from_env(env)
    drop_raw = env.get("MAC_ROUTER_DROP_PARAMS")
    drop_params = (
        tuple(p.strip() for p in drop_raw.split(",") if p.strip())
        if drop_raw is not None
        else _DEFAULT_DROP_PARAMS
    )
    return ProviderProxy(
        router,
        _fwd,
        stream_forward_fn=_sfwd,
        default_model=_default_model_from_env(env),
        wildcard_models=wildcard_models,
        timeout=_f("MAC_ROUTER_TIMEOUT", 60.0),
        stream_timeout=_f("MAC_ROUTER_STREAM_TIMEOUT", 300.0),
        drop_params=drop_params,
        route_observer=route_observer,
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
    env = os.environ if env is None else env
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


def urllib_get_json(
    provider: Provider,
    path: str,
    *,
    timeout: float = 60.0,
    secret_resolver: Optional[SecretResolver] = None,
    auth_scheme: str = "Bearer",
):
    """GET ``provider.base_url + path`` and parse JSON (for async job polling).
    Returns ``(status|None, body)``; status None means unreachable/timeout."""
    url = provider.base_url.rstrip("/") + path
    headers = {"Accept": "application/json"}
    key = resolve_provider_key(provider, secret_resolver)
    if key:
        headers["Authorization"] = "%s %s" % (auth_scheme or "Bearer", key)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(detail) if detail.strip() else {"error": {"message": exc.reason}}
        except Exception:
            body = {"error": {"message": detail[:500] or exc.reason}}
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return None, {"error": {"message": str(exc), "type": "upstream_unreachable"}}


def mount_media_router(
    app: Any,
    *,
    env: Optional[Dict[str, str]] = None,
    secret_resolver: Optional[SecretResolver] = None,
    agent_table_provider: Optional[Callable[[], Dict[str, Any]]] = None,
) -> bool:
    """media-01: mount the canonical ``POST /v1/media/{op}`` endpoint.

    Resolves an ordered list of provider bindings for the operation, adapts the
    single canonical request to each provider's wire format, forwards, and falls
    back to the next binding on failure (missing key → 401 → next, non-2xx, or
    unreachable). The flat ``/v1/genai`` etc. proxies stay mounted for
    back-compat; this is the operation-keyed layer above them.

    Bindings are composed per request: ``agent_table_provider`` (live GPU agents
    that self-advertised ``resources["media_routes"]``) is consulted FIRST so the
    fleet prefers an on-prem GPU when one is up, then the static config table
    (:func:`mac.media_routing.build_media_table`) as cloud fallback. So a GPU
    agent that announces a capability is used with zero operator config, and a
    down one falls over automatically. No-op when neither side can bind anything."""
    from mac.media_routing import (
        ADAPTERS, build_media_table, compose_media_table, dispatch_order, op_is_async, op_is_binary,
    )

    env = os.environ if env is None else env
    static_table = build_media_table(env)
    if not static_table and agent_table_provider is None:
        return False
    from fastapi.responses import JSONResponse

    # In-flight request counts per upstream (base_url), for least-loaded balancing
    # across same-tier GPUs. Process-local (the single hub router process).
    inflight: Dict[str, int] = {}
    inflight_lock = threading.Lock()
    # Async media jobs (e.g. video): hub job id -> upstream poll handle, so a slow
    # render is submitted then polled via GET /v1/media/jobs/{id}.
    media_jobs: Dict[str, Dict[str, Any]] = {}
    media_jobs_lock = threading.Lock()

    @app.post("/v1/media/{op}")
    def _media(op: str, body: Dict[str, Any] = Body(...)) -> Any:  # noqa: ANN401
        agent_table: Dict[str, Any] = {}
        if agent_table_provider is not None:
            try:
                agent_table = agent_table_provider() or {}
            except Exception:  # noqa: BLE001 - never let registry hiccups break routing
                agent_table = {}
        bindings = compose_media_table(static_table, agent_table, op)
        if not bindings:
            return JSONResponse(
                {"error": {"message": "no provider bound for media op %r" % op,
                           "type": "not_configured"}},
                status_code=404,
            )
        # Least-loaded across the top accelerator tier (snapshot in-flight under
        # the lock), then VRAM/priority order, then lower tiers + cloud failover.
        with inflight_lock:
            ordered = dispatch_order(bindings, dict(inflight))
        # A caller may request a specific model; it overrides the binding's
        # default but stays confined to the binding's fixed upstream host — it's
        # a model id, never a path (`..`/leading-slash rejected so it can't
        # escape /v1/genai/<model>).
        requested_model = str(body.get("model") or "").strip()
        if ".." in requested_model or requested_model.startswith("/"):
            requested_model = ""
        last_status: int = 502
        last_body: Dict[str, Any] = {"error": {"message": "no binding produced a response"}}
        for binding in ordered:
            to_provider, from_provider = ADAPTERS.get(binding.adapter, ADAPTERS["passthrough"])
            model = requested_model or binding.model
            path, provider_body = to_provider(op, body, model)
            provider = Provider(name=binding.provider, base_url=binding.base_url, api_key_env=binding.key_spec)
            with inflight_lock:
                inflight[binding.base_url] = inflight.get(binding.base_url, 0) + 1
            try:
                status, resp = urllib_forwarder(
                    provider, path, provider_body, timeout=binding.timeout,
                    secret_resolver=secret_resolver, auth_scheme=binding.auth_scheme,
                    binary_ok=op_is_binary(op),
                )
            finally:
                with inflight_lock:
                    inflight[binding.base_url] = max(0, inflight.get(binding.base_url, 0) - 1)
            canonical = from_provider(status, resp)
            if status and 200 <= status < 300:
                if op_is_async(op) and isinstance(canonical, dict) and canonical.get("job_id"):
                    # Async submit: remap the upstream job to a hub job id so the
                    # poll endpoint knows which upstream/binding to query.
                    import secrets as _secrets

                    hub_job_id = "mjob_" + _secrets.token_hex(8)
                    with media_jobs_lock:
                        media_jobs[hub_job_id] = {
                            "base_url": binding.base_url, "provider": binding.provider,
                            "key_spec": binding.key_spec, "auth_scheme": binding.auth_scheme,
                            "timeout": binding.timeout, "upstream_job_id": str(canonical["job_id"]),
                        }
                    return JSONResponse(
                        {"job_id": hub_job_id, "status": canonical.get("status", "running"),
                         "provider": binding.provider, "model": model},
                        status_code=status,
                    )
                return JSONResponse(
                    {**canonical, "provider": binding.provider, "model": model},
                    status_code=status,
                )
            last_status, last_body = (status or 502), (canonical if isinstance(canonical, dict) else {})
            # non-2xx / unreachable -> try the next binding (priority failover)
        return JSONResponse(last_body, status_code=last_status)

    @app.get("/v1/media/jobs/{job_id}")
    def _media_job(job_id: str) -> Any:  # noqa: ANN401
        """Poll an async media job: forward to the upstream that owns it."""
        with media_jobs_lock:
            job = media_jobs.get(job_id)
        if not job:
            return JSONResponse({"error": {"message": "unknown media job %r" % job_id}}, status_code=404)
        from urllib.parse import quote as _quote

        provider = Provider(name=job["provider"], base_url=job["base_url"], api_key_env=job["key_spec"])
        status, resp = urllib_get_json(
            provider, "/video/jobs/" + _quote(job["upstream_job_id"], safe=""),
            timeout=job.get("timeout", 60.0), secret_resolver=secret_resolver, auth_scheme=job["auth_scheme"],
        )
        out = resp if isinstance(resp, dict) else {}
        return JSONResponse({**out, "job_id": job_id, "provider": job["provider"]}, status_code=status or 502)

    return True


def mount_router(
    app: Any,
    *,
    env: Optional[Dict[str, str]] = None,
    proxy: Optional[ProviderProxy] = None,
    secret_resolver: Optional[SecretResolver] = None,
    route_observer: Optional[RouteObserver] = None,
    media_agent_table_provider: Optional[Callable[[], Dict[str, Any]]] = None,
) -> bool:
    """Mount /v1/chat/completions + /v1/embeddings (+ /v1/genai image proxy) on
    ``app`` when MAC_ROUTER_BACKEND=inproc. Returns True if anything mounted.
    Default backend is 'tokenhub' → no-op, so an existing fleet is unchanged until
    flipped. ``media_agent_table_provider`` (optional) lets the media router
    compose bindings from live self-advertising agents (capability auto-routing)."""
    env = os.environ if env is None else env
    if (env.get("MAC_ROUTER_BACKEND") or "tokenhub").strip().lower() != "inproc":
        return False
    _configure_route_logging()
    # The image proxy is independent of the chat providers (a fixed upstream, no
    # failover), so mount it even when no chat providers are configured.
    mounted = mount_image_proxy(app, env=env, secret_resolver=secret_resolver)
    # media-01: operation-keyed canonical media routing (POST /v1/media/{op}),
    # layered above the flat /v1/genai proxy. Independent of chat providers.
    if mount_media_router(
        app, env=env, secret_resolver=secret_resolver,
        agent_table_provider=media_agent_table_provider,
    ):
        mounted = True
    proxy = proxy or build_proxy_from_env(env, secret_resolver=secret_resolver, route_observer=route_observer)
    if proxy is None:
        return mounted
    from fastapi.responses import JSONResponse, StreamingResponse

    # Evaluate once at mount time so the gate is consistent for the lifetime of
    # the process (no per-request os.environ lookups; flipping the env var
    # requires a process restart, which is the expected ops pattern).
    _reject_mismatch = _is_principal_mismatch_rejected(env)

    def _mismatch_response(route_context: Dict[str, Any]) -> Optional[Any]:
        """Return a 403 JSONResponse when principal mismatch rejection is active
        and the request carries a mismatched X-MAC-Agent-ID; None otherwise."""
        if _reject_mismatch and route_context.get("claimed_agent_id"):
            return JSONResponse(
                {
                    "error": {
                        "message": (
                            "principal mismatch: token is bound to agent_id=%s "
                            "but request header claimed agent_id=%s"
                            % (route_context.get("agent_id"), route_context.get("claimed_agent_id"))
                        ),
                        "type": "principal_mismatch",
                    }
                },
                status_code=403,
            )
        return None

    @app.post("/v1/chat/completions")
    def _chat(request: Request, body: Dict[str, Any] = Body(...)) -> Any:  # noqa: ANN401
        route_context = _route_context_from_request(request, body)
        mismatch = _mismatch_response(route_context)
        if mismatch is not None:
            return mismatch
        body = _strip_internal_route_context(body)
        if body.get("stream"):
            status, obj = proxy.stream_complete("/chat/completions", body, route_context=route_context)
            if status == 200 and not isinstance(obj, dict):
                return StreamingResponse(obj, media_type="text/event-stream")
            return JSONResponse(obj if isinstance(obj, dict) else {}, status_code=status)
        status, out = proxy.complete("/chat/completions", body, route_context=route_context)
        return JSONResponse(out, status_code=status)

    @app.post("/v1/embeddings")
    def _embeddings(request: Request, body: Dict[str, Any] = Body(...)) -> Any:  # noqa: ANN401
        route_context = _route_context_from_request(request, body)
        mismatch = _mismatch_response(route_context)
        if mismatch is not None:
            return mismatch
        status, out = proxy.complete(
            "/embeddings",
            _strip_internal_route_context(body),
            route_context=route_context,
        )
        return JSONResponse(out, status_code=status)

    return True
