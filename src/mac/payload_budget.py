"""Measure and bound registration-style JSON payloads.

The control plane refuses ``machine.resources`` / ``agent.resources`` above
``64 KB`` (:data:`mac.api.MAX_REGISTRATION_PAYLOAD_BYTES`). That limit is a
*fuse*: it protects the SQLite blob columns from a single chatty client. Until
this module existed the fleet had the fuse and no gauge — a worker accumulated
hardware probes, capability advertisements and a command inventory across weeks,
crossed the line, and was refused at registration with the reason visible only
in its own journal.

Two properties are needed to make that survivable, and both are pure functions
over the payload, so they live here rather than in the hub or the worker:

``measure``
    What the payload weighs, how close to the limit it is (a *band*, so callers
    can act at 75% instead of at 100%), and **which top-level keys account for
    it**. "resources is 65,194 bytes, 91% of which is ``commands``" is an
    actionable operator sentence; "exceeds 65536-byte limit" is not.

``shed_to_budget``
    Drop the heaviest unprotected blocks until the payload fits, and say what
    was dropped and why. A worker that sheds its command inventory and registers
    is in the fleet in a degraded, *visible* state; a worker that exits 1 is in
    a systemd restart loop that reads as ``active`` while it never joins.

Stdlib only, no imports from ``mac`` — the hub, the worker and the CLI all call
it, and the worker calls it on a path that must not fail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "BAND_CRITICAL",
    "BAND_OK",
    "BAND_OVER",
    "BAND_WARN",
    "BANDS",
    "CRITICAL_UTILIZATION",
    "DEFAULT_LIMIT_BYTES",
    "PayloadBudget",
    "SHED_SCHEMA",
    "WARN_UTILIZATION",
    "band_for",
    "band_is_at_least",
    "encoded_size",
    "measure",
    "shed_to_budget",
]

#: Mirrors ``mac.api.MAX_REGISTRATION_PAYLOAD_BYTES``. Duplicated as a literal
#: because this module must not import from the hub — the worker uses it too.
DEFAULT_LIMIT_BYTES = 64 * 1024

#: The gauge. ``warn`` is deliberately far from the fuse: a worker's payload
#: grows by kilobytes per deploy, not per second, so 75% is days-to-weeks of
#: warning rather than minutes. ``critical`` is the last band in which shedding
#: is still a choice rather than a consequence.
WARN_UTILIZATION = 0.75
CRITICAL_UTILIZATION = 0.90

BAND_OK = "ok"
BAND_WARN = "warn"
BAND_CRITICAL = "critical"
BAND_OVER = "over"

#: Ascending severity. Callers compare positions, never string equality, so a
#: future band inserted in the middle does not silently reorder any check.
BANDS: Tuple[str, ...] = (BAND_OK, BAND_WARN, BAND_CRITICAL, BAND_OVER)

SHED_SCHEMA = "mac.payload_shed.v1"

#: Number of top-level keys named in a measurement. Enough to answer "what is
#: consuming it" without turning the report into another unbounded payload.
TOP_CONTRIBUTORS = 5

#: Contributor key names are echoed into an observability detail blob and into
#: an error message. The payload being measured is by definition untrusted and
#: possibly enormous, and a 60 KB *key* is a legal JSON object, so the name that
#: comes back out is truncated rather than repeated.
MAX_CONTRIBUTOR_KEY_CHARS = 120


def _encode(value: Any) -> str:
    """JSON-encode exactly the way the hub's limit check does."""

    return json.dumps(value, separators=(",", ":"), default=str)


def encoded_size(value: Any) -> int:
    """Return the UTF-8 byte length of ``value`` encoded as compact JSON.

    Non-serializable values are stringified rather than raising: this is a
    measurement, and a measurement that explodes on a bad payload is useless on
    exactly the payloads worth measuring. The hub's separate serializability
    check still rejects them.
    """

    try:
        return len(_encode(value).encode("utf-8"))
    except (TypeError, ValueError):  # pragma: no cover - default=str covers ~all
        return len(repr(value).encode("utf-8"))


def band_for(utilization: float) -> str:
    """Map a 0..1+ utilization onto a band name."""

    if utilization > 1.0:
        return BAND_OVER
    if utilization >= CRITICAL_UTILIZATION:
        return BAND_CRITICAL
    if utilization >= WARN_UTILIZATION:
        return BAND_WARN
    return BAND_OK


def band_is_at_least(band: str, floor: str) -> bool:
    """True when ``band`` is at or above ``floor`` in severity."""

    try:
        return BANDS.index(band) >= BANDS.index(floor)
    except ValueError:
        return False


def _contributors(value: Any, limit: int) -> List[Dict[str, Any]]:
    """Attribute the payload's weight to its top-level keys, heaviest first.

    Sizes are per-entry (``"key":value`` plus its separator), so they sum to
    approximately — not exactly — the whole payload. The point is ranking and
    order of magnitude, not accounting.
    """

    if not isinstance(value, Mapping):
        return []
    sized: List[Tuple[int, str]] = []
    for key in value:
        name = str(key)
        try:
            entry = len(_encode(name).encode("utf-8")) + 1 + encoded_size(value[key])
        except Exception:  # noqa: BLE001 - a bad key must not break the gauge
            continue
        sized.append((entry, name))
    sized.sort(key=lambda item: (-item[0], item[1]))
    total = sum(entry for entry, _ in sized) or 1
    return [
        {
            "key": name[:MAX_CONTRIBUTOR_KEY_CHARS],
            "bytes": entry,
            "share": round(entry / total, 4),
        }
        for entry, name in sized[:limit]
    ]


@dataclass(frozen=True)
class PayloadBudget:
    """What a payload weighs against its limit, and what is consuming it."""

    field: str
    size_bytes: int
    limit_bytes: int
    utilization: float
    band: str
    contributors: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def over_limit(self) -> bool:
        return self.size_bytes > self.limit_bytes

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.limit_bytes - self.size_bytes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "mac.payload_budget.v1",
            "field": self.field,
            "size_bytes": self.size_bytes,
            "limit_bytes": self.limit_bytes,
            "remaining_bytes": self.remaining_bytes,
            "utilization": self.utilization,
            "band": self.band,
            "over_limit": self.over_limit,
            "top_contributors": list(self.contributors),
        }

    def describe(self) -> str:
        """One operator-readable sentence, safe for a log line or a CLI cell."""

        head = "%s is %s bytes, %s%% of the %s-byte limit (%s)" % (
            self.field,
            format(self.size_bytes, ","),
            round(self.utilization * 100, 1),
            format(self.limit_bytes, ","),
            self.band,
        )
        if not self.contributors:
            return head
        largest = ", ".join(
            "%s=%s" % (item["key"], format(int(item["bytes"]), ","))
            for item in self.contributors[:3]
        )
        return "%s; largest: %s" % (head, largest)


def measure(
    value: Any,
    field_name: str = "payload",
    limit_bytes: int = DEFAULT_LIMIT_BYTES,
    top_contributors: int = TOP_CONTRIBUTORS,
) -> PayloadBudget:
    """Measure ``value`` against ``limit_bytes``.

    ``None`` measures as an empty payload rather than raising, so callers can
    hand through an optional field without branching.
    """

    limit = max(1, int(limit_bytes))
    size = 0 if value is None else encoded_size(value)
    utilization = size / limit
    return PayloadBudget(
        field=field_name,
        size_bytes=size,
        limit_bytes=limit,
        utilization=round(utilization, 6),
        band=band_for(utilization),
        contributors=_contributors(value, max(0, int(top_contributors))),
    )


def shed_to_budget(
    payload: Mapping[str, Any],
    limit_bytes: int = DEFAULT_LIMIT_BYTES,
    target_utilization: float = 0.75,
    protected: Sequence[str] = (),
    reason: str = "registration payload over budget",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Drop the heaviest unprotected top-level keys until ``payload`` fits.

    Returns ``(reduced_payload, report)``. The report is small by construction
    (key names and sizes, never the dropped values) and is meant to be attached
    to the payload that *is* sent, so the hub can say what the worker withheld.

    ``protected`` keys are never dropped: identity, credentials and dispatch
    policy are what registration is *for*, so a payload that only fits without
    them does not fit. When shedding everything sheddable still leaves the
    payload over the limit, the report says ``fits=False`` and the caller
    decides — this function does not mutilate protected state to force a fit.
    """

    reduced: Dict[str, Any] = dict(payload or {})
    limit = max(1, int(limit_bytes))
    target = max(1, int(limit * max(0.0, min(1.0, target_utilization))))
    protected_keys = {str(key) for key in protected}
    shed: List[Dict[str, Any]] = []

    if encoded_size(reduced) <= target:
        return reduced, {
            "schema": SHED_SCHEMA,
            "shed": shed,
            "shed_bytes": 0,
            "fits": True,
            "reason": None,
            "size_bytes": encoded_size(reduced),
            "limit_bytes": limit,
            "target_bytes": target,
        }

    shed_bytes = 0
    while encoded_size(reduced) > target:
        candidates = [
            (encoded_size(reduced[key]), str(key))
            for key in reduced
            if str(key) not in protected_keys
        ]
        if not candidates:
            break
        candidates.sort(key=lambda item: (-item[0], item[1]))
        size, key = candidates[0]
        reduced.pop(key, None)
        shed_bytes += size
        shed.append({"key": key, "bytes": size})

    final_size = encoded_size(reduced)
    return reduced, {
        "schema": SHED_SCHEMA,
        "shed": shed,
        "shed_bytes": shed_bytes,
        "fits": final_size <= limit,
        "reason": reason if shed else None,
        "size_bytes": final_size,
        "limit_bytes": limit,
        "target_bytes": target,
    }


def bounded_string_list(
    names: Iterable[str],
    max_bytes: int,
    max_entries: Optional[int] = None,
) -> Tuple[List[str], int]:
    """Take names in iteration order until either bound is reached.

    Returns ``(kept, omitted)``. This is the primitive behind the worker's
    command inventory: a *count* cap (10,000 executable names) says nothing
    about bytes, which is the dimension the hub actually rejects on.
    """

    kept: List[str] = []
    omitted = 0
    used = 2  # the enclosing "[]"
    budget = max(2, int(max_bytes))
    for name in names:
        if max_entries is not None and len(kept) >= max_entries:
            omitted += 1
            continue
        cost = len(_encode(str(name)).encode("utf-8")) + (1 if kept else 0)
        if used + cost > budget:
            omitted += 1
            continue
        kept.append(str(name))
        used += cost
    return kept, omitted
