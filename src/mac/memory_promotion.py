"""Medium → long tier promotion: the writer ``mac_memory_long`` never had.

mem-08 shipped the consolidator that writes nap summaries into the medium
tier, and the tier→collection registry in ``mac.models`` has declared a
``long`` tier since mem-06. Nothing in the tree ever wrote to it. The
2026-08-21 audit of the live instance found ``mac_memory_long`` at zero points nearly three months
after the collection was created — the tier existed as a name and a Qdrant
collection, and as nothing else. A tier nothing writes to is not a tier; it
advertises a capability the fleet does not have, and every reader that
routes a query to it gets a confident empty answer.

This module is that writer.

What promotion means here
=========================

A memory that is still being embedded is working set; a memory that has sat
in the medium tier past a retention horizon without being re-embedded is
settled. Promotion moves the settled ones into ``mac_memory_long``:

* **Selection** is by the *ledger*, not by Qdrant. ``vector_refs`` is the
  durable record of what was embedded where, so a medium-tier ref older than
  ``min_age_days`` is the promotion candidate. Reading Qdrant instead would
  make promotion depend on the store it is trying to fill.
* **Writing** reuses :meth:`mac.vector_writer_service.VectorWriterService.embed_memory`
  with ``tier="long"``. Point ids are a deterministic hash of the memory id,
  so promotion is idempotent: re-running it upserts the same point and
  reuses the same ref.
* **Retiring the medium copy** is opt-in (``drop_medium``). Copy-then-verify
  is the safe default because the long tier is new and unproven on any live
  fleet; an operator who wants the medium tier to actually shrink asks for
  it, and only points whose long-tier write already succeeded are dropped.

Deliberately not here: re-summarizing the promoted records into denser
long-tier artifacts. That needs a summarizer and a quality bar, and shipping
it inside the fix for "nothing writes to this tier" would put an unevaluated
LLM step on the critical path. Promotion first makes the tier real; better
content for it is a separate, measurable change.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from mac.config_coercion import bounded_env_number
from mac.models import (
    MAC_MEMORY_COLLECTIONS,
    JsonDict,
    MacMemoryTier,
    NotFoundError,
    ValidationError,
)

MEMORY_PROMOTION_SCHEMA = "mac.memory_promotion.v1"

#: A month of sitting in the medium tier without a re-embed is the line
#: between "still being worked on" and "settled". Short enough that the long
#: tier fills on a fleet that has been running a while, long enough that an
#: actively re-embedded memory is never promoted out from under the writer.
DEFAULT_MIN_AGE_DAYS = 30.0
MIN_AGE_DAYS_FLOOR = 0.0
MIN_AGE_DAYS_CEILING = 3650.0

#: Promotion runs inside the nap cycle, which has an agent in DRAINING while
#: it works. Bounded per pass so a fleet with a large backlog drains it over
#: several naps instead of holding one agent down for the whole sweep.
DEFAULT_MAX_PER_PASS = 50
MAX_PER_PASS_CEILING = 10_000

_ENABLED_ENV = "MAC_MEMORY_PROMOTION_ENABLED"
_MIN_AGE_ENV = "MAC_MEMORY_PROMOTION_MIN_AGE_DAYS"
_MAX_PER_PASS_ENV = "MAC_MEMORY_PROMOTION_MAX_PER_PASS"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def promotion_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the nap cycle should promote. Default on.

    Defaulting off would reproduce the disease: a promotion path that exists
    and never runs leaves ``mac_memory_long`` empty exactly as before, and
    the ``unwritten_memory_tier`` alert would keep firing at a fleet that has
    the fix installed. It is safe on because a pass with nothing old enough
    is a no-op, and failures inside the nap cycle are captured, never raised.
    """

    env = os.environ if environ is None else environ
    raw = str(env.get(_ENABLED_ENV) or "").strip().lower()
    if raw in _FALSE:
        return False
    return True


def promotion_settings(
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Resolve ``(enabled, min_age_days, max_per_pass)`` from the environment."""

    env = os.environ if environ is None else environ
    errors: List[str] = []
    min_age = bounded_env_number(
        env,
        _MIN_AGE_ENV,
        DEFAULT_MIN_AGE_DAYS,
        MIN_AGE_DAYS_FLOOR,
        MIN_AGE_DAYS_CEILING,
        errors=errors,
    )
    max_per_pass = bounded_env_number(
        env,
        _MAX_PER_PASS_ENV,
        float(DEFAULT_MAX_PER_PASS),
        1.0,
        float(MAX_PER_PASS_CEILING),
        errors=errors,
    )
    return {
        "enabled": promotion_enabled(env),
        "min_age_days": float(min_age),
        "max_per_pass": int(max_per_pass),
        "configuration_errors": errors,
    }


def _parse_iso(raw: Any) -> Optional[datetime]:
    """Parse a ledger timestamp; unparseable means "do not promote"."""

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


class MemoryPromotionService:
    """Promote settled medium-tier memories into the long tier."""

    def __init__(self, *, memory: Any, vector_writer: Any) -> None:
        if memory is None:
            raise ValidationError("MemoryPromotionService requires a memory service")
        if vector_writer is None:
            raise ValidationError("MemoryPromotionService requires a vector writer")
        self._memory = memory
        self._vector_writer = vector_writer

    @property
    def medium_collection(self) -> str:
        return MAC_MEMORY_COLLECTIONS[MacMemoryTier.MEDIUM.value]

    @property
    def long_collection(self) -> str:
        return MAC_MEMORY_COLLECTIONS[MacMemoryTier.LONG.value]

    def candidates(
        self,
        *,
        min_age_days: float = DEFAULT_MIN_AGE_DAYS,
        now: Optional[datetime] = None,
    ) -> List[Any]:
        """Medium-tier refs old enough to promote and not already promoted.

        Oldest first, so a bounded pass always drains the backlog from the
        far end rather than re-visiting the same recent slice.
        """

        reference = now or datetime.now(tz=timezone.utc)
        cutoff = reference - timedelta(days=max(0.0, float(min_age_days)))
        promoted = {
            ref.memory_id for ref in self._memory.list_vector_refs(collection=self.long_collection)
        }
        out: List[Any] = []
        for ref in self._memory.list_vector_refs(collection=self.medium_collection):
            if ref.memory_id in promoted:
                continue
            created = _parse_iso(ref.created_at)
            if created is None or created > cutoff:
                continue
            out.append(ref)
        out.sort(key=lambda ref: (ref.created_at, ref.id))
        return out

    def promote(
        self,
        *,
        min_age_days: float = DEFAULT_MIN_AGE_DAYS,
        limit: Optional[int] = DEFAULT_MAX_PER_PASS,
        drop_medium: bool = False,
        dry_run: bool = False,
        created_by: str = "memory-promotion",
        now: Optional[datetime] = None,
    ) -> JsonDict:
        """Run one promotion pass and return a report.

        Never raises for a single bad record: one memory whose content no
        longer embeds must not stop the rest of the backlog, so per-record
        failures are collected and reported.
        """

        selected = self.candidates(min_age_days=min_age_days, now=now)
        if limit is not None:
            selected = selected[: max(0, int(limit))]

        promoted: List[str] = []
        dropped: List[str] = []
        orphaned: List[str] = []
        failures: List[Dict[str, Any]] = []

        for ref in selected:
            if dry_run:
                promoted.append(ref.memory_id)
                continue
            try:
                self._vector_writer.embed_memory(
                    ref.memory_id,
                    tier=MacMemoryTier.LONG.value,
                    created_by=created_by,
                )
            except NotFoundError:
                # The memory row is gone but its medium ref survived. Report
                # it; cleaning up dangling refs is a different decision.
                orphaned.append(ref.memory_id)
                continue
            except Exception as exc:  # noqa: BLE001 - per-record best-effort
                failures.append({"memory_id": ref.memory_id, "error": str(exc)})
                continue
            promoted.append(ref.memory_id)
            if drop_medium:
                try:
                    self._vector_writer.delete_point(self.medium_collection, ref.point_id)
                    self._memory.delete_vector_ref(ref.id)
                    dropped.append(ref.memory_id)
                except Exception as exc:  # noqa: BLE001
                    # The long-tier copy landed, so the memory is safe; the
                    # medium point simply stays until the next pass.
                    failures.append(
                        {
                            "memory_id": ref.memory_id,
                            "phase": "drop_medium",
                            "error": str(exc),
                        }
                    )

        return {
            "schema": MEMORY_PROMOTION_SCHEMA,
            "source_collection": self.medium_collection,
            "target_collection": self.long_collection,
            "min_age_days": float(min_age_days),
            "limit": limit,
            "dry_run": dry_run,
            "drop_medium": drop_medium,
            "candidates": len(selected),
            "promoted": len(promoted),
            "promoted_memory_ids": promoted,
            "dropped_from_medium": dropped,
            "orphaned_refs": orphaned,
            "failures": failures,
        }
