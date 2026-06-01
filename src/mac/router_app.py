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

Failure semantics (what trips the breaker / triggers failover):
* network error / timeout (status ``None``), HTTP 429, or HTTP >= 500 → the
  provider failed: record_failure + try the next one.
* 2xx or other 4xx → the provider *answered* (a 4xx is a bad request, not a
  health problem): record_success and return it as-is (no pointless failover).

The transport is injected (`forward_fn`) so the proxy logic is unit-testable
without a live provider.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from mac.provider_router import Provider, ProviderRouter, providers_from_env

__all__ = ["ProviderProxy", "urllib_forwarder", "build_proxy_from_env", "mount_router"]

# forward_fn(provider, path, payload, *, timeout) -> (status_code|None, body)
ForwardFn = Callable[..., Tuple[Optional[int], Any]]


def _is_provider_failure(status: Optional[int]) -> bool:
    """A provider-health failure (failover + breaker) vs. a request the provider
    answered. None = unreachable/timeout; 429 = rate-limited; >=500 = upstream."""
    return status is None or status == 429 or status >= 500


class ProviderProxy:
    def __init__(self, router: ProviderRouter, forward_fn: ForwardFn, *, timeout: float = 60.0) -> None:
        self._router = router
        self._forward = forward_fn
        self._timeout = timeout

    def complete(self, path: str, payload: Dict[str, Any]) -> Tuple[int, Any]:
        """Route one request. ``path`` is the suffix after the provider base_url
        (e.g. ``/chat/completions``). Returns ``(status_code, body)``."""
        model = str(payload.get("model") or "*")
        attempts = []
        # Bounded: at most one try per provider (+1 to allow a half-open probe
        # to be re-selected after another provider is tried).
        for _ in range(len(self._router.provider_names()) + 1):
            provider = self._router.select(model)
            if provider is None:
                break
            status, body = self._forward(provider, path, payload, timeout=self._timeout)
            if not _is_provider_failure(status):
                self._router.record_success(provider.name)
                return int(status), body
            self._router.record_failure(provider.name)
            attempts.append({"provider": provider.name, "status": status})
        return 503, {
            "error": {
                "message": "no provider could serve model=%s" % model,
                "type": "all_providers_unavailable",
                "attempts": attempts,
            }
        }


def urllib_forwarder(provider: Provider, path: str, payload: Dict[str, Any], *, timeout: float = 60.0):
    """Real HTTP forward to ``provider.base_url + path`` with its bearer key.
    Returns ``(status_code|None, body)``; status None means unreachable/timeout."""
    url = provider.base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if provider.api_key_env:
        key = os.environ.get(provider.api_key_env, "")
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


def build_proxy_from_env(env: Optional[Dict[str, str]] = None) -> Optional[ProviderProxy]:
    """Build a ProviderProxy from MAC_ROUTER_* env; None when no providers are
    configured."""
    env = env or os.environ
    providers = providers_from_env(env)
    if not providers:
        return None

    def _f(name: str, default: float) -> float:
        try:
            return float(env.get(name) or default)
        except ValueError:
            return default

    router = ProviderRouter(
        providers,
        failure_threshold=int(_f("MAC_ROUTER_FAILURE_THRESHOLD", 3)),
        cooldown_seconds=_f("MAC_ROUTER_COOLDOWN_SECONDS", 30.0),
    )
    return ProviderProxy(router, urllib_forwarder, timeout=_f("MAC_ROUTER_TIMEOUT", 60.0))


def mount_router(app: Any, *, env: Optional[Dict[str, str]] = None, proxy: Optional[ProviderProxy] = None) -> bool:
    """Mount /v1/chat/completions + /v1/embeddings on ``app`` when
    MAC_ROUTER_BACKEND=inproc. Returns True if mounted. Default backend is
    'tokenhub' → no-op, so an existing fleet is unchanged until flipped."""
    env = env or os.environ
    if (env.get("MAC_ROUTER_BACKEND") or "tokenhub").strip().lower() != "inproc":
        return False
    proxy = proxy or build_proxy_from_env(env)
    if proxy is None:
        return False
    from fastapi.responses import JSONResponse

    @app.post("/v1/chat/completions")
    def _chat(body: Dict[str, Any]) -> Any:  # noqa: ANN401
        status, out = proxy.complete("/chat/completions", body)
        return JSONResponse(out, status_code=status)

    @app.post("/v1/embeddings")
    def _embeddings(body: Dict[str, Any]) -> Any:  # noqa: ANN401
        status, out = proxy.complete("/embeddings", body)
        return JSONResponse(out, status_code=status)

    return True
