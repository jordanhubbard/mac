"""Measured yield gate for automated task generators.

A generator is any code path that files tasks without a human asking for
them. Measured across 7,781 ledger tasks on 2026-08-02, several of them
produced almost nothing that ever completed:

    self_heal                      54 tasks    0.0%
    dream_low_confidence_repair 1,396 tasks    0.3%   (generator deleted)
    crash_observer                 50 tasks    2.0%
    task_system_reset             137 tasks    2.9%
    curiosity_adjudication         11 tasks    0.0%
    backlog_grooming                5 tasks    0.0%

against 20.0% for operator-filed work. The precedent for deleting one is
already set: mac.dreaming records in its own source that the scanner it
replaced "filed 1,259 investigation tasks of which 4 completed".

Deleting named generators one at a time does not hold. Three of the
zero-yield origins above were not in the ticket that asked for this, and a
generator added next month would be ungated again. So the rule is stated
once, here, and applied to every origin that is not a human asking:

    a generator that cannot show its own yield does not get to file, and
    one whose measured yield stays below the floor stops filing.

A generator is never judged before it has filed ``min_sample`` tasks --
there is nothing to measure yet, and a new generator must be allowed to
earn its record. That is a deliberate hole: a generator can always file its
first ``min_sample`` tasks. It is bounded, and it is the price of not
blocking work on a statistic that does not exist.

The gate is advisory-off by default in tests (``ControlPlane.in_memory``
has no history to measure), and enforcing wherever a real ledger exists.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from mac.env_config import env_bool, env_float, env_int

GENERATOR_YIELD_SCHEMA = "mac.generator_yield.v1"

#: Origins that record a human (or an external system acting for one) asking
#: for work. These are never gated: a person filing a task is not a generator,
#: and a low completion rate on operator-filed work is a statement about the
#: work, not about a machine that should stop.
HUMAN_ORIGIN_TYPES = frozenset(
    {
        "direct_task",
        "operator_directive",
        "operator_observation",
        "hermes_interaction",
        "beads",
        "github_ingest",
    }
)

DEFAULT_MIN_SAMPLE = 40
DEFAULT_YIELD_FLOOR = 0.05
DEFAULT_CACHE_TTL_SECONDS = 300


class GeneratorSuppressed(Exception):
    """Raised when a generator is not permitted to file.

    Subclasses of this are caught by generator call sites, which log and skip
    rather than crash: a suppressed generator is the gate working, not an
    error in the caller.
    """


@dataclass(frozen=True)
class YieldPolicy:
    """Thresholds that decide whether a generator may keep filing."""

    enabled: bool = True
    min_sample: int = DEFAULT_MIN_SAMPLE
    floor: float = DEFAULT_YIELD_FLOOR
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "YieldPolicy":
        return cls(
            enabled=env_bool("MAC_GENERATOR_YIELD_GATE", True, environ=environ),
            min_sample=env_int(
                "MAC_GENERATOR_YIELD_MIN_SAMPLE",
                DEFAULT_MIN_SAMPLE,
                minimum=1,
                environ=environ,
            ),
            floor=env_float(
                "MAC_GENERATOR_YIELD_FLOOR",
                DEFAULT_YIELD_FLOOR,
                minimum=0.0,
                maximum=1.0,
                environ=environ,
            ),
            cache_ttl_seconds=env_int(
                "MAC_GENERATOR_YIELD_CACHE_TTL_SECONDS",
                DEFAULT_CACHE_TTL_SECONDS,
                minimum=0,
                environ=environ,
            ),
        )


@dataclass(frozen=True)
class OriginYield:
    """One generator's measured record."""

    origin_type: str
    filed: int
    completed: int

    @property
    def rate(self) -> float:
        return (self.completed / self.filed) if self.filed else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "origin_type": self.origin_type,
            "filed": self.filed,
            "completed": self.completed,
            "yield": round(self.rate, 4),
        }


def is_generator_origin(origin_type: str) -> bool:
    """Whether ``origin_type`` is an automated generator rather than a human."""

    normalized = str(origin_type or "").strip()
    return bool(normalized) and normalized not in HUMAN_ORIGIN_TYPES


def origin_type_of(metadata: Any) -> str:
    """Extract ``metadata.origin.type``, tolerating any malformed shape."""

    if not isinstance(metadata, Mapping):
        return ""
    origin = metadata.get("origin")
    if not isinstance(origin, Mapping):
        return ""
    return str(origin.get("type") or "").strip()


_YIELD_SQL = """
SELECT json_extract(metadata, '$.origin.type') AS origin_type,
       COUNT(*) AS filed,
       SUM(CASE WHEN state = 'completed' THEN 1 ELSE 0 END) AS completed
FROM tasks
WHERE json_extract(metadata, '$.origin.type') IS NOT NULL
  AND json_extract(metadata, '$.origin.type') != ''
GROUP BY json_extract(metadata, '$.origin.type')
"""


class GeneratorYieldGate:
    """Measures generator yield from the ledger and decides who may file.

    The measurement is a single grouped aggregate over ``tasks`` and is cached
    for ``policy.cache_ttl_seconds``; a task-creation path must not pay a table
    scan per call. The cache is intentionally coarse -- yield moves over days,
    and a generator that just crossed the floor may file for one more TTL.
    """

    def __init__(self, store: Any, policy: Optional[YieldPolicy] = None) -> None:
        self.store = store
        self.policy = policy or YieldPolicy.from_env()
        self._lock = threading.Lock()
        self._cache: Optional[Dict[str, OriginYield]] = None
        self._cached_at = 0.0

    # -- measurement ------------------------------------------------------

    def measure(self, *, refresh: bool = False) -> Dict[str, OriginYield]:
        """Return every origin's record, from cache unless it has expired."""

        with self._lock:
            fresh_enough = (
                self._cache is not None
                and not refresh
                and (time.monotonic() - self._cached_at) < self.policy.cache_ttl_seconds
            )
            if fresh_enough:
                return dict(self._cache or {})
        measured = self._query()
        with self._lock:
            self._cache = measured
            self._cached_at = time.monotonic()
        return dict(measured)

    def _query(self) -> Dict[str, OriginYield]:
        try:
            rows = self.store.query_all(_YIELD_SQL)
        except Exception:  # noqa: BLE001 - measurement must never block filing
            return {}
        measured: Dict[str, OriginYield] = {}
        for row in rows or []:
            origin_type = str(row["origin_type"] or "").strip()
            if not origin_type:
                continue
            measured[origin_type] = OriginYield(
                origin_type=origin_type,
                filed=int(row["filed"] or 0),
                completed=int(row["completed"] or 0),
            )
        return measured

    # -- decision ---------------------------------------------------------

    def evaluate(self, origin_type: str) -> Dict[str, Any]:
        """Decide whether ``origin_type`` may file, and say why.

        Always returns a verdict rather than raising, so callers can report a
        generator's standing without attempting to file.
        """

        verdict: Dict[str, Any] = {
            "schema": GENERATOR_YIELD_SCHEMA,
            "origin_type": origin_type,
            "allowed": True,
            "reason": "not_a_generator",
            "floor": self.policy.floor,
            "min_sample": self.policy.min_sample,
            "filed": 0,
            "completed": 0,
            "yield": None,
        }
        if not is_generator_origin(origin_type):
            return verdict
        if not self.policy.enabled:
            verdict["reason"] = "gate_disabled"
            return verdict

        record = self.measure().get(origin_type)
        if record is None or record.filed < self.policy.min_sample:
            verdict.update(
                {
                    "reason": "insufficient_sample",
                    "filed": record.filed if record else 0,
                    "completed": record.completed if record else 0,
                    "yield": round(record.rate, 4) if record else None,
                }
            )
            return verdict

        verdict.update(
            {
                "filed": record.filed,
                "completed": record.completed,
                "yield": round(record.rate, 4),
            }
        )
        if record.rate < self.policy.floor:
            verdict["allowed"] = False
            verdict["reason"] = "below_yield_floor"
        else:
            verdict["reason"] = "above_yield_floor"
        return verdict

    def enforce(self, metadata: Any) -> Optional[Dict[str, Any]]:
        """Raise ``GeneratorSuppressed`` when this origin may not file.

        Returns the verdict when filing is permitted (or is not a generator at
        all), so the caller can record it.
        """

        origin_type = origin_type_of(metadata)
        if not is_generator_origin(origin_type):
            return None
        verdict = self.evaluate(origin_type)
        if verdict["allowed"]:
            return verdict
        raise GeneratorSuppressed(
            "generator %r is suppressed: %d of %d filed tasks completed "
            "(%.1f%%), below the %.1f%% floor. Fix what it files or retire it; "
            "raise MAC_GENERATOR_YIELD_FLOOR only with a reason."
            % (
                origin_type,
                verdict["completed"],
                verdict["filed"],
                100.0 * (verdict["yield"] or 0.0),
                100.0 * self.policy.floor,
            )
        )

    def report(self) -> List[Dict[str, Any]]:
        """Every origin's record and standing, worst yield first.

        This is the "show its own yield" half of the rule: a generator that
        cannot appear here is not measurable and should not be filing.
        """

        measured = self.measure(refresh=True)
        rows = []
        for origin_type, record in measured.items():
            verdict = self.evaluate(origin_type)
            entry = record.to_dict()
            entry.update(
                {
                    "generator": is_generator_origin(origin_type),
                    "allowed": verdict["allowed"],
                    "reason": verdict["reason"],
                }
            )
            rows.append(entry)
        rows.sort(key=lambda entry: (not entry["generator"], entry["yield"]))
        return rows
