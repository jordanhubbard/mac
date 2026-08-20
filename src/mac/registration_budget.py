"""Make a refused registration a hub-visible state, not a journal line.

The ``machine.resources`` / ``agent.resources`` size limit is enforced in the
``POST /machines`` and ``POST /agents`` handlers *before any row is written*.
That is correct — the whole point is to keep the oversized blob out of the
database — but it means the hub retains no trace of the attempt. From the
operator's side the worker is ``offline``, which is exactly what a powered-off
machine looks like, while on the worker systemd restarts a process that exits 1
every few seconds and reports ``active`` the whole time.

This module closes that gap without adding a table. Three things:

* :func:`enforce` measures the payload, records what it found, and raises with
  the size and the largest contributors in the message instead of a bare
  "exceeds 65536-byte limit".
* Pressure is recorded **on band change only** (ok -> warn -> critical and
  back). A gauge that writes a row per registration is how ``observability_events``
  reached 3.1GB before (see the mem-04 note in ``observability_service``); a
  gauge that writes on transitions is a handful of rows per worker per month.
* :func:`list_refusals` and :func:`annotate_agent_rows` turn those records back
  into an agent-shaped answer, so ``mac agent list`` and the console can show
  *refused* as its own state next to *absent*.

Refusals live in ``observability_events`` rather than a new table on purpose:
they are bounded, they are already retained and pruned by an owner that
understands retention, and the alternative — a row in ``machines`` — would
persist exactly the oversized payload the limit exists to refuse.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from mac.models import ValidationError
from mac.payload_budget import (
    BAND_OK,
    BAND_WARN,
    DEFAULT_LIMIT_BYTES,
    PayloadBudget,
    band_is_at_least,
    measure,
)

__all__ = [
    "PRESSURE_EVENT",
    "REFUSAL_EVENT",
    "REFUSAL_WINDOW_SECONDS",
    "annotate_agent_rows",
    "enforce",
    "list_refusals",
    "observe_pressure",
    "refusal_subject_id",
]

#: ``kind=log``, ``level=error``. One row per refused registration attempt: a
#: crash-looping worker writes one every restart, which is the signal.
REFUSAL_EVENT = "registration.refused"

#: ``kind=metric``, value = utilization. Written only when the band changes.
PRESSURE_EVENT = "registration.payload_pressure"

#: How far back ``mac agent list`` looks for refusals. A worker in a systemd
#: restart loop re-refuses every few seconds, so a short window is enough to
#: catch a live one and short enough that a fixed worker stops being reported.
REFUSAL_WINDOW_SECONDS = 15 * 60

#: How many refusal events to scan when aggregating. A crash loop produces
#: many rows for one subject, so this is generous relative to fleet size.
_REFUSAL_SCAN_LIMIT = 500


def refusal_subject_id(agent_id: Optional[str], hostname: Optional[str]) -> str:
    """Stable identity for a registrant that may have no row yet.

    Prefer the agent id the worker asked for; fall back to the hostname. Either
    is enough for an operator to reach the right host, which is the only thing
    this identity is for.
    """

    return str(agent_id or hostname or "(unidentified)")


def _iso_seconds_ago(seconds: int) -> Optional[str]:
    from datetime import datetime, timedelta, timezone

    return (
        datetime.now(timezone.utc) - timedelta(seconds=max(0, int(seconds)))
    ).isoformat()


def _record(cp: Any, **kwargs: Any) -> None:
    """Record an observation, never failing the caller.

    Registration must not break because the observability write did. The limit
    check itself still raises; only the *reporting* is best-effort here.
    """

    try:
        cp.record_observation(**kwargs)
    except Exception:  # noqa: BLE001 - reporting must not break registration
        pass


def _last_pressure_band(cp: Any, subject_type: str, subject_id: str) -> str:
    """The band we last recorded for this subject, or ``ok`` if we never did.

    Served by ``idx_observability_events_subject_sequence``
    ``(kind, name, subject_type, subject_id, sequence DESC)``.
    """

    try:
        events = cp.list_observability(
            kind="metric",
            name=PRESSURE_EVENT,
            subject_type=subject_type,
            subject_id=subject_id,
            limit=1,
        )
    except Exception:  # noqa: BLE001 - an unreadable history means "unknown"
        return BAND_OK
    if not events:
        return BAND_OK
    detail = getattr(events[0], "detail", None) or {}
    return str(detail.get("band") or BAND_OK)


def enforce(
    cp: Any,
    value: Any,
    field_name: str,
    *,
    subject_type: str,
    agent_id: Optional[str] = None,
    hostname: Optional[str] = None,
    machine_id: Optional[str] = None,
    limit_bytes: int = DEFAULT_LIMIT_BYTES,
    actor: str = "registration",
) -> PayloadBudget:
    """Measure a registration payload, report it, and refuse it if oversized.

    Returns the budget so the handler can attach it to its response. Raises
    :class:`ValidationError` when over the limit — the same exception and the
    same leading clause as before, now followed by the diagnosis an operator
    would otherwise have had to ssh for.
    """

    budget = measure(value, field_name=field_name, limit_bytes=limit_bytes)
    subject_id = refusal_subject_id(agent_id, hostname or machine_id)
    detail: Dict[str, Any] = {
        "field": field_name,
        "agent_id": agent_id,
        "hostname": hostname,
        "machine_id": machine_id,
        "actor": actor,
        **budget.to_dict(),
    }

    if budget.over_limit:
        _record(
            cp,
            kind="log",
            name=REFUSAL_EVENT,
            layer="control_plane",
            source="mac",
            level="error",
            subject_type=subject_type,
            subject_id=subject_id,
            detail={**detail, "message": budget.describe()},
        )
        raise ValidationError(
            "%s exceeds %d-byte limit -- %s"
            % (field_name, budget.limit_bytes, budget.describe())
        )

    previous = _last_pressure_band(cp, subject_type, subject_id)
    if budget.band != previous and (
        band_is_at_least(budget.band, BAND_WARN)
        or band_is_at_least(previous, BAND_WARN)
    ):
        _record(
            cp,
            kind="metric",
            name=PRESSURE_EVENT,
            layer="control_plane",
            source="mac",
            level="warning" if band_is_at_least(budget.band, BAND_WARN) else "info",
            value=float(budget.utilization),
            unit="ratio",
            subject_type=subject_type,
            subject_id=subject_id,
            detail={
                **detail,
                "previous_band": previous,
                "message": budget.describe(),
            },
        )
    return budget


def observe_pressure(
    cp: Any,
    value: Any,
    field_name: str,
    *,
    subject_type: str,
    subject_id: str,
    limit_bytes: int = DEFAULT_LIMIT_BYTES,
) -> PayloadBudget:
    """Report a payload's pressure WITHOUT enforcing a limit on it.

    For paths that write resources but do not gate on size — the heartbeat is
    the one that matters. Registration happens on restart; the heartbeat happens
    every couple of seconds, so this is where a worker's growth is actually
    observed between one restart and the next. Enforcing here instead would take
    a working agent offline for a payload the hub already accepted, which is the
    incident again with a different trigger.

    Costs nothing in the healthy case: below the warn band it does not touch the
    database at all. The price is that recovery (warn -> ok) is only recorded on
    the registration path, where the history read is already affordable.
    """

    budget = measure(value, field_name=field_name, limit_bytes=limit_bytes)
    if not band_is_at_least(budget.band, BAND_WARN):
        return budget
    previous = _last_pressure_band(cp, subject_type, subject_id)
    if budget.band == previous:
        return budget
    _record(
        cp,
        kind="metric",
        name=PRESSURE_EVENT,
        layer="control_plane",
        source="mac",
        level="warning",
        value=float(budget.utilization),
        unit="ratio",
        subject_type=subject_type,
        subject_id=subject_id,
        detail={
            "field": field_name,
            "agent_id": subject_id if subject_type == "agent" else None,
            "previous_band": previous,
            "message": budget.describe(),
            **budget.to_dict(),
        },
    )
    return budget


def list_refusals(
    cp: Any,
    within_seconds: int = REFUSAL_WINDOW_SECONDS,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Recent refused registrations, one entry per registrant, newest first.

    A crash loop writes one event per restart; an operator needs "this host has
    been refused 47 times in 15 minutes because ``commands`` is 59KB", not 47
    rows. So the events are folded per subject and the *count* is kept — it is
    the difference between a one-off and a loop.
    """

    since = _iso_seconds_ago(within_seconds)
    try:
        events = cp.list_observability(
            kind="log",
            name=REFUSAL_EVENT,
            since=since,
            limit=_REFUSAL_SCAN_LIMIT,
        )
    except Exception:  # noqa: BLE001 - never break a list command on this
        return []

    folded: Dict[str, Dict[str, Any]] = {}
    for event in events:  # newest first
        subject_id = str(getattr(event, "subject_id", "") or "")
        if not subject_id:
            continue
        detail = dict(getattr(event, "detail", None) or {})
        created_at = str(getattr(event, "created_at", "") or "")
        entry = folded.get(subject_id)
        if entry is None:
            folded[subject_id] = {
                "schema": "mac.registration_refusal.v1",
                "subject_type": str(getattr(event, "subject_type", "") or ""),
                "subject_id": subject_id,
                "agent_id": detail.get("agent_id"),
                "hostname": detail.get("hostname"),
                "machine_id": detail.get("machine_id"),
                "field": detail.get("field"),
                "size_bytes": detail.get("size_bytes"),
                "limit_bytes": detail.get("limit_bytes"),
                "utilization": detail.get("utilization"),
                "band": detail.get("band"),
                "top_contributors": detail.get("top_contributors") or [],
                "message": detail.get("message"),
                "last_refused_at": created_at,
                "first_refused_at": created_at,
                "refusal_count": 1,
            }
            continue
        entry["refusal_count"] = int(entry["refusal_count"]) + 1
        if created_at and created_at < str(entry["first_refused_at"]):
            entry["first_refused_at"] = created_at
    ordered = sorted(
        folded.values(), key=lambda item: str(item["last_refused_at"]), reverse=True
    )
    return ordered[: max(1, int(limit))]


def _matches(row: Mapping[str, Any], refusal: Mapping[str, Any]) -> bool:
    """Does this agent row name the registrant that was refused?"""

    subject_id = str(refusal.get("subject_id") or "")
    candidates = {
        str(row.get("id") or ""),
        str(row.get("name") or ""),
        str(row.get("machine_id") or ""),
    }
    candidates.discard("")
    if subject_id in candidates:
        return True
    for key in ("agent_id", "machine_id"):
        value = str(refusal.get(key) or "")
        if value and value in candidates:
            return True
    return False


def _synthetic_row(refusal: Mapping[str, Any]) -> Dict[str, Any]:
    """An agent-shaped row for a registrant the hub has never admitted.

    ``registered`` is the field that answers the question the incident asked:
    ``False`` here means "this host is trying and being turned away", which is
    a different fact from an ``offline`` row, and a very different fact from no
    row at all.
    """

    subject_id = str(refusal.get("subject_id") or "(unidentified)")
    return {
        "id": subject_id,
        "name": str(refusal.get("hostname") or subject_id),
        "machine_id": refusal.get("machine_id"),
        "capabilities": [],
        "resources": {},
        "status": "refused",
        "health_status": "refused",
        "current_task_id": None,
        "running_digest": None,
        "created_at": refusal.get("first_refused_at"),
        "updated_at": refusal.get("last_refused_at"),
        "last_seen_at": refusal.get("last_refused_at"),
        "registered": False,
        "registration_state": "refused",
        "registration_refusal": dict(refusal),
    }


def annotate_agent_rows(
    rows: Iterable[Mapping[str, Any]],
    refusals: Sequence[Mapping[str, Any]],
    *,
    limit_bytes: int = DEFAULT_LIMIT_BYTES,
    include_unregistered: bool = True,
    measure_resources: bool = True,
) -> List[Dict[str, Any]]:
    """Attach registration state (and the resources gauge) to agent rows.

    Every row gains ``registration_state`` — ``accepted`` or ``refused`` — so
    the distinction exists in the data and not only in a rendering. Refusals
    that match no row are appended as ``registered: False`` rows when
    ``include_unregistered``, because the worst case in the incident was the
    host that never got a row at all.
    """

    annotated: List[Dict[str, Any]] = []
    matched: set = set()
    for row in rows:
        item = dict(row)
        item.setdefault("registered", True)
        hit = next(
            (
                refusal
                for refusal in refusals
                if _matches(item, refusal)
            ),
            None,
        )
        if hit is not None:
            matched.add(str(hit.get("subject_id") or ""))
            item["registration_state"] = "refused"
            item["registration_refusal"] = dict(hit)
        else:
            item["registration_state"] = "accepted"
            item["registration_refusal"] = None
        if measure_resources and isinstance(item.get("resources"), Mapping):
            budget = measure(
                item["resources"], field_name="agent.resources", limit_bytes=limit_bytes
            )
            item["resources_bytes"] = budget.size_bytes
            item["resources_utilization"] = budget.utilization
            item["resources_band"] = budget.band
        annotated.append(item)

    if include_unregistered:
        for refusal in refusals:
            if str(refusal.get("subject_id") or "") in matched:
                continue
            annotated.append(_synthetic_row(refusal))
    return annotated
