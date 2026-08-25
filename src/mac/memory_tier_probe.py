"""Qdrant-side introspection for the memory-tier health snapshot (mem-10).

``ControlPlane.memory_health`` used to ask Qdrant one question per tier —
"how many points?" — and three real failure modes hid behind a healthy
looking count:

* **Silently stopped ingestion.** A collection keeps its points forever, so
  ``points_count`` stays reassuring long after the writer died. The 2026-08-21
  audit found ``mac_memory_medium`` frozen since 2026-07-25: 667 points, newest
  ``embedded_at`` 27 days old, and nothing anywhere said so.
* **A tier nothing writes to.** ``mac_memory_long`` has never received a point.
  Nothing in the tree promotes medium → long, so the collection advertises a
  capability that does not exist.
* **Two embedding spaces in one collection.** A model switch left 601 points
  from one embedder and 66 from another in ``mac_memory_medium``. Vectors from
  different models are not comparable, so similarity search mixes two spaces
  and returns wrong neighbours without raising anything.

All three are answerable from the payload every point already carries
(``embedded_at`` + ``embedding_model``, see :class:`mac.models.MacVectorPayload`),
which is what this module reads.

Everything here is best-effort and side-effect free: an unreachable endpoint
becomes an ``error`` string on the collection entry, never an exception, so the
operator still gets the database-side numbers.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


__all__ = [
    "DEFAULT_INGESTION_MAX_AGE_HOURS",
    "DEFAULT_SCAN_LIMIT",
    "SCAN_PAGE_SIZE",
    "evaluate_qdrant_alerts",
    "parse_timestamp",
    "probe_collection",
    "probe_collections",
    "urllib_transport",
]


# An ingestion pipeline that has embedded nothing for a day is not slow, it is
# stopped. The audit that motivated this module found the gap at 27 days.
DEFAULT_INGESTION_MAX_AGE_HOURS = 24.0

# Freshness and per-model counts come from a payload scan, because neither
# question can be answered from collection metadata alone. The scan is bounded
# so `mac memory health` stays a snapshot command rather than a full table
# walk; past the bound we report what we could not see instead of guessing.
DEFAULT_SCAN_LIMIT = 20_000
SCAN_PAGE_SIZE = 512

_SCAN_LIMIT_ENV = "MAC_MEMORY_HEALTH_SCAN_LIMIT"

# The two payload keys the alerts need. Asking for these by name (rather than
# `with_payload: true`) keeps summaries and tags off the wire.
_PAYLOAD_KEYS = ("embedded_at", "embedding_model")


# A transport is `(method, url, body_or_None) -> parsed JSON dict`. Injecting
# one is how the tests exercise every branch without a live Qdrant.
Transport = Callable[[str, str, Optional[Dict[str, Any]]], Dict[str, Any]]


def urllib_transport(timeout: float = 5.0) -> Transport:
    """Return the default stdlib-backed :data:`Transport`."""

    def _call(method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return _call


def _scan_limit(explicit: Optional[int]) -> int:
    """Resolve the payload-scan bound: argument, then env, then default."""

    if explicit is not None:
        return max(0, int(explicit))
    raw = os.environ.get(_SCAN_LIMIT_ENV)
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_SCAN_LIMIT


def parse_timestamp(raw: Any) -> Optional[datetime]:
    """Parse an ISO-8601 payload timestamp into an aware datetime.

    Qdrant payloads are written by :func:`mac.models.utcnow`, which emits a
    trailing ``Z``. Anything unparseable returns None rather than raising —
    one malformed point must not blind the whole snapshot.
    """

    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _scan_payloads(
    transport: Transport,
    base: str,
    collection: str,
    *,
    scan_limit: int,
) -> Dict[str, Any]:
    """Page through a collection's payloads, bounded by ``scan_limit``.

    Returns the newest ``embedded_at`` seen, the per-model point counts, how
    many points were read, and whether the bound cut the scan short.
    """

    newest_raw: Optional[str] = None
    newest_dt: Optional[datetime] = None
    models: Dict[str, int] = {}
    scanned = 0
    offset: Any = None
    # A zero bound means we never look, which is not the same as "we looked
    # and there was no more" — say so rather than reporting a clean scan.
    truncated = scan_limit <= 0

    while scanned < scan_limit:
        page_size = min(SCAN_PAGE_SIZE, scan_limit - scanned)
        body: Dict[str, Any] = {
            "limit": page_size,
            "with_payload": list(_PAYLOAD_KEYS),
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        result = (
            transport("POST", "%s/collections/%s/points/scroll" % (base, collection), body).get(
                "result"
            )
            or {}
        )
        points = result.get("points") or []
        for point in points:
            scanned += 1
            payload = point.get("payload") if isinstance(point, dict) else None
            if not isinstance(payload, dict):
                continue
            model = payload.get("embedding_model")
            if isinstance(model, str) and model.strip():
                models[model] = models.get(model, 0) + 1
            stamp = parse_timestamp(payload.get("embedded_at"))
            if stamp is not None and (newest_dt is None or stamp > newest_dt):
                newest_dt = stamp
                newest_raw = str(payload.get("embedded_at"))
        offset = result.get("next_page_offset")
        if offset is None or not points:
            break
    else:
        # Loop hit the bound with the cursor still live: more points exist.
        truncated = offset is not None

    return {
        "newest_raw": newest_raw,
        "newest_dt": newest_dt,
        "models": models,
        "scanned": scanned,
        "truncated": truncated,
    }


def probe_collection(
    url: str,
    collection: str,
    *,
    tier: str,
    transport: Optional[Transport] = None,
    scan_limit: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return the health entry for one Qdrant collection.

    On any transport failure the entry carries ``error`` and the caller keeps
    going: a single unreachable collection must not cost the operator the rest
    of the snapshot.
    """

    call = transport or urllib_transport()
    base = url.rstrip("/")
    now_dt = now or datetime.now(tz=timezone.utc)
    entry: Dict[str, Any] = {"tier": tier}

    try:
        info = call("GET", "%s/collections/%s" % (base, collection), None).get("result") or {}
    except Exception as exc:  # noqa: BLE001 - best-effort probe
        entry["error"] = str(exc)
        return entry

    points = info.get("points_count")
    entry["points_count"] = int(points) if points is not None else None

    # An empty collection has no payloads to scan, and scanning one that
    # failed to report a count would be guesswork.
    if not entry["points_count"]:
        entry["newest_embedded_at"] = None
        entry["ingestion_age_hours"] = None
        entry["embedding_models"] = {}
        entry["payload_scanned"] = 0
        entry["payload_scan_truncated"] = False
        return entry

    try:
        scan = _scan_payloads(call, base, collection, scan_limit=_scan_limit(scan_limit))
    except Exception as exc:  # noqa: BLE001 - best-effort probe
        entry["scan_error"] = str(exc)
        entry["newest_embedded_at"] = None
        entry["ingestion_age_hours"] = None
        entry["embedding_models"] = {}
        entry["payload_scanned"] = 0
        entry["payload_scan_truncated"] = False
        return entry

    entry["payload_scanned"] = scan["scanned"]
    entry["payload_scan_truncated"] = scan["truncated"]
    entry["embedding_models"] = scan["models"]

    # Scroll returns points in id order, not embedded_at order, so a scan the
    # bound cut short has not necessarily seen the newest point. Reporting the
    # sample maximum as "newest" would invent a stall that may not exist, so we
    # withhold the freshness numbers and say why instead.
    if scan["truncated"]:
        entry["newest_embedded_at"] = None
        entry["ingestion_age_hours"] = None
        return entry

    entry["newest_embedded_at"] = scan["newest_raw"]
    if scan["newest_dt"] is None:
        entry["ingestion_age_hours"] = None
    else:
        age = (now_dt - scan["newest_dt"]).total_seconds() / 3600.0
        entry["ingestion_age_hours"] = round(age, 2)
    return entry


def probe_collections(
    url: str,
    collections: Dict[str, str],
    *,
    transport: Optional[Transport] = None,
    scan_limit: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """Probe every ``tier -> collection`` pair, keyed by collection name."""

    call = transport or urllib_transport()
    out: Dict[str, Dict[str, Any]] = {}
    for tier, collection in collections.items():
        out[collection] = probe_collection(
            url,
            collection,
            tier=tier,
            transport=call,
            scan_limit=scan_limit,
            now=now,
        )
    return out


def evaluate_qdrant_alerts(
    collections: Dict[str, Dict[str, Any]],
    *,
    ingestion_max_age_hours: float = DEFAULT_INGESTION_MAX_AGE_HOURS,
) -> List[Dict[str, Any]]:
    """Turn probe entries into memory_health alerts.

    Three rules, one per failure mode the audit found. Each is written so the
    message alone tells the operator what broke and what to do about it —
    an alert that needs a second investigation to interpret is a to-do.
    """

    alerts: List[Dict[str, Any]] = []
    written = [name for name, entry in collections.items() if (entry.get("points_count") or 0) > 0]

    for name in sorted(collections):
        entry = collections[name]
        if entry.get("error"):
            continue
        points = entry.get("points_count") or 0

        # 1. Ingestion stopped. The points are still there; nothing new has
        #    joined them.
        age = entry.get("ingestion_age_hours")
        if points > 0 and age is not None and age > ingestion_max_age_hours:
            alerts.append(
                {
                    "severity": "critical",
                    "code": "stalled_vector_ingestion",
                    "message": (
                        "%s holds %d points but nothing has been embedded for "
                        "%.1fh (newest embedded_at=%s, threshold %.1fh); the "
                        "writer is stopped, not slow."
                        % (
                            name,
                            points,
                            age,
                            entry.get("newest_embedded_at"),
                            ingestion_max_age_hours,
                        )
                    ),
                    "collection": name,
                    "tier": entry.get("tier"),
                }
            )
        elif points > 0 and age is None and entry.get("payload_scan_truncated"):
            # Truthful non-answer: too many points to bound-scan, and Qdrant
            # scroll is id-ordered, so freshness is genuinely unknown here.
            alerts.append(
                {
                    "severity": "warning",
                    "code": "vector_ingestion_age_unknown",
                    "message": (
                        "%s has more than %d points, so the bounded payload "
                        "scan could not establish the newest embedded_at; "
                        "raise %s or add an embedded_at payload index to "
                        "restore freshness alerting."
                        % (name, entry.get("payload_scanned") or 0, _SCAN_LIMIT_ENV)
                    ),
                    "collection": name,
                    "tier": entry.get("tier"),
                }
            )

        # 2. A declared tier that nothing has ever written to. Only meaningful
        #    when a sibling tier *is* being written: an entirely idle fleet is
        #    not the same defect.
        if points == 0 and written:
            alerts.append(
                {
                    "severity": "critical",
                    "code": "unwritten_memory_tier",
                    "message": (
                        "%s (tier=%s) holds zero points while %s is populated; "
                        "no code path promotes into this tier, so it advertises "
                        "a capability the fleet does not have."
                        % (name, entry.get("tier"), ", ".join(sorted(written)))
                    ),
                    "collection": name,
                    "tier": entry.get("tier"),
                }
            )

        # 3. Two embedding spaces in one collection. A sample proves mixing
        #    even when the scan was truncated — seeing two models is positive
        #    evidence; seeing one is only the absence of evidence.
        models = entry.get("embedding_models") or {}
        if len(models) > 1:
            breakdown = ", ".join(
                "%s=%d" % (model, count)
                for model, count in sorted(models.items(), key=lambda kv: -kv[1])
            )
            alerts.append(
                {
                    "severity": "critical",
                    "code": "mixed_embedding_spaces",
                    "message": (
                        "%s mixes %d embedding models (%s); vectors from "
                        "different models are not comparable, so similarity "
                        "search silently returns wrong neighbours. Re-embed to "
                        "one model or split the collection per model."
                        % (name, len(models), breakdown)
                    ),
                    "collection": name,
                    "tier": entry.get("tier"),
                    "embedding_models": dict(models),
                }
            )

    return alerts
