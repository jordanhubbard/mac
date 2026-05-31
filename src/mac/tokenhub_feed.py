"""Make TokenHub's routing decisions legible in mac observability (ADR 0001, hu-05).

TokenHub stays a separate Go service — it is mature infra (Thompson-sampling
router, encrypted vault, Temporal). The premise's "inexplicable provider
behavior" dark spot is an *observability gap*, not a boundary problem: TokenHub
already broadcasts every routing decision over an SSE event bus at ``/events``
(`event: <type>\\ndata: <json>\\n\\n`), but mac/agents never consume it. So when
an agent ends up on an unexpected model, nothing can explain why.

This module consumes that feed and maps each event to a mac observability
record in the ``tokenhub`` layer, attributed to the agent via the request's
``api_key_name`` where present. An operator (or the agent) can then answer
"why am I on provider X / model Y right now?" — failover, Thompson sample,
budget/health filter, or an in-band directive.

The SSE parser and the event→record mapping are pure and unit-tested;
``stream_decisions`` is the thin stdlib-only runtime loop.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable, Dict, Iterable, Iterator, Mapping, Optional, Tuple

__all__ = [
    "iter_sse_events",
    "event_to_record",
    "record_event",
    "stream_decisions",
    "events_url_from_env",
    "admin_token_from_env",
    "start_background_consumer",
    "ROUTE_EVENT_NAMES",
]

# TokenHub EventType (internal/events/bus.go) -> mac observation name + level.
# Events not listed (heartbeat, connected, stream_started, workflow_*) are
# skipped to keep the table lean (cf. the mem-04 observability-bloat lesson).
ROUTE_EVENT_NAMES: Dict[str, Tuple[str, str]] = {
    "route_success": ("tokenhub.route.success", "info"),
    "route_error": ("tokenhub.route.error", "error"),
    "escalation": ("tokenhub.route.escalation", "warning"),
    "health_change": ("tokenhub.provider.health_change", "info"),
    "key_rotation_expired": ("tokenhub.key.rotation_expired", "warning"),
}

# Secret-free subset of the TokenHub Event we surface (never the payload/keys).
_DETAIL_FIELDS = (
    "model_id", "provider_id", "alias_from", "latency_ms", "cost_usd",
    "input_tokens", "output_tokens", "total_tokens", "error_class", "error_msg",
    "reason", "old_state", "new_state", "request_id", "api_key_name", "mode",
)


def iter_sse_events(lines: Iterable[str]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Parse an SSE byte/line stream into ``(event_type, data_dict)`` pairs.

    Handles the standard SSE framing: ``event:`` / ``data:`` fields, multi-line
    ``data`` concatenated with newlines, blank line terminates an event. Lines
    may be ``str`` or ``bytes``. Malformed JSON payloads are skipped.
    """
    event_type = "message"
    data_parts: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.rstrip("\n").rstrip("\r")
        if line == "":
            if data_parts:
                payload = "\n".join(data_parts)
                try:
                    data = json.loads(payload)
                except (ValueError, TypeError):
                    data = {}
                if isinstance(data, dict):
                    yield event_type, data
            event_type = "message"
            data_parts = []
            continue
        if line.startswith(":"):
            continue  # SSE comment
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_parts.append(line[len("data:"):].lstrip(" "))


def event_to_record(event_type: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a TokenHub event to ``ObservabilityService.record_observation`` kwargs.

    Returns ``None`` for events we intentionally don't persist. The agent is
    attributed via ``api_key_name`` (the per-agent TokenHub key) so a decision
    is tied to whoever caused it.
    """
    mapping = ROUTE_EVENT_NAMES.get(event_type)
    if mapping is None:
        return None
    name, level = mapping
    if event_type == "health_change" and data.get("new_state") not in (None, "", "healthy", "up"):
        level = "warning"

    detail = {k: data[k] for k in _DETAIL_FIELDS if data.get(k) not in (None, "", 0, 0.0)}
    detail["schema"] = "mac.tokenhub_decision.v1"
    detail["event"] = event_type

    api_key_name = str(data.get("api_key_name") or "").strip()
    return {
        "kind": "log",
        "name": name,
        "layer": "tokenhub",
        "source": "tokenhub",
        "level": level,
        "subject_type": "agent" if api_key_name else None,
        "subject_id": api_key_name or None,
        "detail": detail,
    }


def record_event(observability: Any, event_type: str, data: Dict[str, Any]) -> Any:
    """Emit one TokenHub event into mac observability (best-effort). Returns the
    recorded event, or ``None`` if skipped / no observability."""
    if not observability:
        return None
    record = event_to_record(event_type, data)
    if record is None:
        return None
    return observability.record_observation(**record)


def stream_decisions(
    url: str,
    observability: Any,
    *,
    token: Optional[str] = None,
    timeout: float = 30.0,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Connect to TokenHub's ``/events`` SSE feed and record decisions until the
    stream ends or ``should_stop()`` returns True. Returns the count recorded.

    Thin stdlib-only runtime wrapper around the pure parser/mapping above so the
    interesting logic stays unit-testable without a live TokenHub.
    """
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    recorded = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-configured URL)
        def _lines() -> Iterator[str]:
            for raw in resp:
                if should_stop is not None and should_stop():
                    return
                yield raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw

        for event_type, data in iter_sse_events(_lines()):
            if record_event(observability, event_type, data) is not None:
                recorded += 1
            if should_stop is not None and should_stop():
                break
    return recorded


def events_url_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve the TokenHub SSE feed URL: explicit ``MAC_TOKENHUB_EVENTS_URL``,
    else derive from ``TOKENHUB_URL`` + ``/admin/v1/events`` (the SSE route)."""
    env = env or os.environ
    explicit = (env.get("MAC_TOKENHUB_EVENTS_URL") or "").strip()
    if explicit:
        return explicit
    base = (env.get("TOKENHUB_URL") or "").strip().rstrip("/")
    return base + "/admin/v1/events" if base else ""


def admin_token_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    """The TokenHub *admin* token (the /admin/v1 feed needs admin auth, not the
    agent chat key)."""
    env = env or os.environ
    for key in ("MAC_TOKENHUB_ADMIN_TOKEN", "TOKENHUB_ADMIN_TOKEN"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    return ""


def start_background_consumer(
    observability: Any,
    *,
    url: Optional[str] = None,
    token: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    reconnect_delay: float = 5.0,
    _spawn: Optional[Callable[[Callable[[], None]], Any]] = None,
) -> Any:
    """Launch a daemon thread that streams TokenHub decisions into ``observability``,
    reconnecting on drop. **No-op (returns None)** if no events URL is configured
    or no observability — so it is safe to call unconditionally at control-plane
    startup. The mac store is thread-safe (lock + ``check_same_thread=False``),
    so writing observations from this thread is fine.

    ``_spawn`` is injectable for tests (so we can verify gating/derivation without
    starting a real thread or connecting to TokenHub).
    """
    env = env or os.environ
    url = url or events_url_from_env(env)
    if not url or not observability:
        return None
    if token is None:
        token = admin_token_from_env(env) or None

    def _run() -> None:
        import time

        while True:
            try:
                stream_decisions(url, observability, token=token)
            except Exception:
                pass
            time.sleep(reconnect_delay)

    if _spawn is not None:
        return _spawn(_run)

    import threading

    thread = threading.Thread(target=_run, name="tokenhub-feed", daemon=True)
    thread.start()
    return thread
