"""mac-nyx7: refresh TokenHub's wildcard model ladder from MAC.

Hermes keeps requesting ``model="*"`` through its TokenHub key; TokenHub
resolves ``"*"`` to an ordered ladder of concrete models by current
availability / quality / cost. That ladder goes stale as providers change.
This module lets MAC poll TokenHub's ``/admin/v1/wildcard-models`` admin API on
a schedule (a weekly systemd timer — see ``deploy/install-wildcard-refresh-service.sh``)
to refresh the ladder and record it into mac observability for visibility.

Gated, like the SSE feed (hu-05): a clean no-op unless ``TOKENHUB_URL`` (or
``MAC_TOKENHUB_WILDCARD_URL``) **and** the admin token
(``MAC_TOKENHUB_ADMIN_TOKEN`` / ``TOKENHUB_ADMIN_TOKEN``) are both set. The
admin token is required because ``/admin/v1`` needs admin auth, not the agent
chat key.

stdlib-only; the pure parser/mapping stay unit-testable without a live
TokenHub (``_opener`` is injectable).
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional

from mac.tokenhub_feed import admin_token_from_env

__all__ = [
    "wildcard_url_from_env",
    "fetch_wildcard_ladder",
    "extract_ladder",
    "ladder_to_record",
    "refresh_wildcard_ladder",
]


def wildcard_url_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve the wildcard-models admin URL: explicit
    ``MAC_TOKENHUB_WILDCARD_URL``, else ``TOKENHUB_URL`` + ``/admin/v1/wildcard-models``."""
    env = env or os.environ
    explicit = (env.get("MAC_TOKENHUB_WILDCARD_URL") or "").strip()
    if explicit:
        return explicit
    base = (env.get("TOKENHUB_URL") or "").strip().rstrip("/")
    return base + "/admin/v1/wildcard-models" if base else ""


def _wildcard_method(env: Optional[Mapping[str, str]] = None) -> str:
    """HTTP method for the refresh call. Default GET (fetch + record); set
    ``MAC_TOKENHUB_WILDCARD_METHOD=POST`` if the deployed TokenHub treats the
    endpoint as a recompute trigger."""
    env = env or os.environ
    method = (env.get("MAC_TOKENHUB_WILDCARD_METHOD") or "GET").strip().upper()
    return method if method in ("GET", "POST") else "GET"


def fetch_wildcard_ladder(
    url: str,
    token: Optional[str] = None,
    *,
    method: str = "GET",
    timeout: float = 30.0,
    _opener: Optional[Callable[..., Any]] = None,
) -> Any:
    """Call the wildcard-models admin endpoint and return the parsed JSON.

    ``_opener`` is injectable for tests so this stays runnable without a live
    TokenHub.
    """
    data = b"{}" if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if method == "POST":
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    opener = _opener or urllib.request.urlopen
    with opener(req, timeout=timeout) as resp:  # noqa: S310 (operator-configured URL)
        raw = resp.read()
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    return json.loads(text) if text and text.strip() else {}


def extract_ladder(payload: Any) -> List[Dict[str, Any]]:
    """Normalize the several shapes the endpoint might return into an ordered
    list of ``{rank, model_id, ...}`` entries.

    Accepts a bare list, or a dict wrapping the list under ``models`` /
    ``ladder`` / ``wildcard_models`` / ``data``. Entries may be bare model-id
    strings or dicts.
    """
    if isinstance(payload, dict):
        for key in ("models", "ladder", "wildcard_models", "data"):
            seq = payload.get(key)
            if isinstance(seq, list):
                payload = seq
                break
    if not isinstance(payload, list):
        return []
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(payload):
        if isinstance(item, str):
            out.append({"rank": index, "model_id": item})
        elif isinstance(item, dict):
            entry: Dict[str, Any] = {"rank": item.get("rank", index)}
            for k in (
                "model_id",
                "provider_id",
                "provider",
                "quality",
                "cost",
                "cost_usd",
                "available",
                "latency_ms",
            ):
                if k in item:
                    entry[k] = item[k]
            entry.setdefault("model_id", item.get("id") or item.get("model"))
            out.append(entry)
    return out


def ladder_to_record(payload: Any) -> Dict[str, Any]:
    """Map the endpoint payload to ``record_observation`` kwargs."""
    ladder = extract_ladder(payload)
    return {
        "kind": "log",
        "name": "tokenhub.wildcard.refresh",
        "layer": "tokenhub",
        "source": "tokenhub-wildcard-refresh",
        "level": "info",
        "subject_type": "environment",
        "subject_id": "tokenhub:wildcard-ladder",
        "detail": {
            "schema": "mac.tokenhub_wildcard.v1",
            "count": len(ladder),
            # Cap the persisted ladder so one observation row can't bloat
            # (cf. the observability-bloat audit).
            "ladder": ladder[:50],
        },
    }


def refresh_wildcard_ladder(
    observability: Any,
    *,
    url: Optional[str] = None,
    token: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: float = 30.0,
    _opener: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Fetch the wildcard ladder and record it. Gated: returns a ``skipped``
    summary (no exception) when the URL or admin token is absent, so the timer
    is a clean no-op until the operator provides the admin token.
    """
    env = env or os.environ
    url = url or wildcard_url_from_env(env)
    if token is None:
        token = admin_token_from_env(env) or None
    if not url or not token:
        return {
            "status": "skipped",
            "reason": "TOKENHUB_URL/MAC_TOKENHUB_WILDCARD_URL and an admin token are both required",
            "have_url": bool(url),
            "have_token": bool(token),
        }
    payload = fetch_wildcard_ladder(
        url, token, method=_wildcard_method(env), timeout=timeout, _opener=_opener
    )
    record = ladder_to_record(payload)
    if observability is not None:
        observability.record_observation(**record)
    return {
        "status": "refreshed",
        "url": url,
        "count": record["detail"]["count"],
    }
