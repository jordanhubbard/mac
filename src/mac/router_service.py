"""Standalone MAC LLM router service.

Historically the router only existed as routes mounted *inside* the hub ledger
API process (``MAC_ROUTER_BACKEND=inproc``). That couples inference to
coordination: a hub API restart drops every in-flight LLM stream, ledger lock
pressure and model traffic share one event loop, and any network pathology on
the path to the hub severs the fleet's access to models even when the ledger
itself is healthy.

This module gives the same router (``mac.router_app`` — provider selection,
failover, circuit breaker, streaming) its own process:

- **Hub host, own service** (``MAC_ROUTER_BACKEND=standalone``): the deploy
  points agents at ``http://<hub>:<MAC_ROUTER_PORT>/v1`` and the hub API stops
  mounting ``/v1`` (any backend value other than ``inproc`` is a no-op there).
  Ledger restarts no longer touch model streams.
- **Per-site replica**: the same entrypoint runs on a remote wing (e.g. next
  to the GKE workers) with its own provider config, so agents there reach
  models without traversing the hub's network path. Provider credentials stay
  wherever the replica's operator puts them — ``secret:`` refs resolve from a
  co-located control-plane store (hub host), plain env keys or ``key=none``
  private endpoints work anywhere; worker nodes still never receive hub vault
  material automatically.

Caller auth is self-contained: bearer tokens from ``MAC_ROUTER_TOKENS``
(comma-separated) plus the node's own ``MAC_API_TOKEN`` / ``MAC_WORKER_TOKEN``,
compared in constant time. ``llm.route`` observations are written to the
service log (structured JSON); a router co-located with the hub store also
records them into the ledger, preserving today's observability.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("mac.router_service")

DEFAULT_PORT = 8790
PORT_ENV = "MAC_ROUTER_PORT"
TOKENS_ENV = "MAC_ROUTER_TOKENS"

_UNAUTHENTICATED_PATHS = {"/healthz"}


def _configured_tokens(env: Dict[str, str]) -> List[str]:
    tokens: List[str] = []
    for raw in (env.get(TOKENS_ENV) or "").split(","):
        value = raw.strip()
        if value:
            tokens.append(value)
    for key in ("MAC_API_TOKEN", "MAC_WORKER_TOKEN"):
        value = (env.get(key) or "").strip()
        if value:
            tokens.append(value)
    return tokens


def _token_matches(presented: str, tokens: List[str]) -> bool:
    matched = False
    for token in tokens:
        # Constant-time compare on every candidate; no early exit on match.
        if hmac.compare_digest(presented.encode(), token.encode()):
            matched = True
    return matched


def _store_backed_secret_resolver(
    env: Dict[str, str],
) -> "tuple[Optional[Callable[[str], Optional[str]]], Any]":
    """Vault ``secret:`` resolution when co-located with the control-plane store.

    Only attempted when this process can legitimately own the store (hub host
    with ``MAC_DB``/``MAC_DATABASE_URL``). Elsewhere the resolver is None and
    providers must use env keys or ``key=none`` private endpoints — the same
    posture the deploy already enforces for non-hub nodes. Returns
    ``(secret_resolver, control_plane)``; both None in env-key mode.
    """
    if not ((env.get("MAC_DB") or "").strip() or (env.get("MAC_DATABASE_URL") or "").strip()):
        return None, None
    try:
        from mac.services import ControlPlane
        from mac.store import make_store_from_env

        # The standalone router is a data-plane sidecar, not a schema owner.
        # Hub API startup owns schema preparation; replaying DDL here can
        # contend with the live task and lease paths merely because the router
        # restarted.
        cp = ControlPlane(make_store_from_env(initialize_schema=False))
    except Exception as exc:  # noqa: BLE001 - degrade to env-key mode, loudly
        log.warning("router vault resolver unavailable (%s); using env keys only", exc)
        return None, None

    def _resolve(name: str) -> Optional[str]:
        return cp.secrets.resolve_secret_value(name, purpose="router-upstream")

    return _resolve, cp


def _route_observer_for(cp: Any) -> Callable[[Dict[str, Any]], None]:
    def _observe(detail: Dict[str, Any]) -> None:
        try:
            if cp is not None:
                agent_id = str(detail.get("agent_id") or "").strip()
                task_id = str(detail.get("task_id") or "").strip()
                cp.record_log(
                    "llm.route",
                    layer="router",
                    source=agent_id or "router",
                    level="info" if detail.get("outcome") == "success" else "warning",
                    subject_type="task" if task_id else "agent" if agent_id else None,
                    subject_id=task_id or agent_id or None,
                    detail=detail,
                )
            else:
                log.info("llm.route %s", json.dumps(detail, sort_keys=True, default=str))
        except Exception:  # noqa: BLE001 - observability must never break routing
            pass

    return _observe


def build_router_app(env: Optional[Dict[str, str]] = None):
    """FastAPI app carrying ONLY the router routes + bearer auth + /healthz."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from mac.router_app import mount_router

    source_env = os.environ if env is None else env
    # mount_router gates on MAC_ROUTER_BACKEND=inproc; the deployed value for
    # this topology is "standalone" (so the hub API does NOT mount). Force the
    # gate open for our own process without mutating the real environment.
    router_env: Dict[str, str] = {k: str(v) for k, v in source_env.items()}
    router_env["MAC_ROUTER_BACKEND"] = "inproc"

    tokens = _configured_tokens(router_env)
    if not tokens:
        raise RuntimeError(
            "standalone router requires at least one bearer token "
            "(MAC_ROUTER_TOKENS, MAC_API_TOKEN, or MAC_WORKER_TOKEN)"
        )

    secret_resolver, cp = _store_backed_secret_resolver(router_env)

    app = FastAPI(title="mac-router", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _authenticate(request: Request, call_next):  # noqa: ANN001
        if request.url.path in _UNAUTHENTICATED_PATHS:
            return await call_next(request)
        header = request.headers.get("authorization") or ""
        presented = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not presented or not _token_matches(presented, tokens):
            return JSONResponse(
                {"error": {"message": "invalid or missing bearer token", "type": "auth"}},
                status_code=401,
            )
        return await call_next(request)

    @app.get("/healthz")
    def _healthz() -> Dict[str, Any]:
        return {"status": "ok", "service": "mac-router"}

    media_agent_table_provider = None
    if cp is not None:
        # Same live-agent media-capability composition the hub API uses, so a
        # GPU agent's self-advertised media routes keep working when the
        # router moves out of the ledger process.
        def media_agent_table_provider() -> Dict[str, Any]:  # noqa: F811
            from mac.media_routing import media_bindings_from_agents

            return media_bindings_from_agents(a.to_dict() for a in cp.list_agents())

    mounted = mount_router(
        app,
        env=router_env,
        secret_resolver=secret_resolver,
        route_observer=_route_observer_for(cp),
        media_agent_table_provider=media_agent_table_provider,
    )
    if not mounted:
        raise RuntimeError(
            "no router routes mounted — configure MAC_ROUTER_PROVIDERS "
            "(and modality upstreams as needed) for this node"
        )
    return app


def main(argv: Optional[List[str]] = None) -> int:
    """Run the MAC router service entry point and return its exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="mac-router")
    parser.add_argument("--host", default=os.environ.get("MAC_ROUTER_BIND_HOST") or "127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=int((os.environ.get(PORT_ENV) or "").strip() or DEFAULT_PORT),
    )
    ns = parser.parse_args(argv)
    import uvicorn

    uvicorn.run(build_router_app(), host=ns.host, port=ns.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
