"""Nap consolidator service (mem-08).

When an agent enters its nap window, this service walks the memory
records that agent has produced since its last successful nap, groups
them by task / project, summarizes each group, and writes the result
back as a new `memory_records` row with `record_type='nap_summary'`.
The summary record is then embedded into the medium tier (mem-07) so
the recall API (mem-09) can surface it directly.

Design notes
============

* **Pluggable summarizer.** The default joins record contents with a
  short provenance header — fine for proving the pipeline works
  without depending on an LLM. Production callers pass
  ``summarizer_fn=token_hub_summarize`` for real condensation. The
  contract is ``Callable[[List[MemoryRecord], dict], str]``.

* **"Since-last-nap" scoping.** The consolidator queries the
  ``nap_runs`` table for the agent's most recent completed run; if
  there is none, it falls back to "everything the agent has touched".
  Either way, the same record never gets summarized twice because
  the summary itself is a `nap_summary` record that the next pass
  skips (it filters out summaries of summaries).

* **Idempotency.** Re-running the consolidator against the same
  window with the same memories produces the same summary content
  (modulo embedded timestamps), but writes a NEW memory_records row
  every time. The dedupe responsibility is at the nap-schedule layer:
  the schedule fires once per window, the consolidator runs once per
  nap. Operators triggering manually with `mac nap consolidate` are
  expected to know what they're doing.

* **Vector handoff.** When a `VectorWriterService` is provided, each
  summary gets embedded into the medium tier immediately. Failures
  there don't roll back the summary — the next backfill catches it.
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from mac.models import (
    JsonDict,
    MacMemoryTier,
    MemoryRecord,
    NotFoundError,
    ValidationError,
    utcnow,
)

# A summarizer takes a list of records + a context dict (e.g., the
# group key, agent_id, time window) and returns the summary string.
SummarizerFn = Callable[[List[MemoryRecord], Dict[str, Any]], str]


def _default_summarizer(records: List[MemoryRecord], context: Dict[str, Any]) -> str:
    """Concatenate record contents with a provenance header.

    Honest about being a stub: it's not condensation, it's
    aggregation. Sufficient to prove the pipeline; swap for an LLM
    via the summarizer_fn arg.
    """
    if not records:
        return ""
    header_lines = [
        "# Nap summary for agent %s" % context.get("agent_id", "?"),
        "## Group: %s" % context.get("group_label", "?"),
        "## Records: %d  Window: %s → %s"
        % (
            len(records),
            context.get("window_start", "?"),
            context.get("window_end", "?"),
        ),
        "",
    ]
    body_lines: List[str] = []
    for rec in records:
        body_lines.append(
            "- [%s] %s (rec_type=%s): %s"
            % (
                rec.created_at,
                rec.id,
                rec.record_type,
                rec.content[:500] + ("…" if len(rec.content) > 500 else ""),
            )
        )
    return "\n".join(header_lines + body_lines)


class NapConsolidatorService:
    """Walks an agent's memory_records since its last nap and writes
    one `nap_summary` row per (task_id, project) group."""

    def __init__(
        self,
        store: Any,
        memory: Any,
        *,
        summarizer_fn: Optional[SummarizerFn] = None,
        vector_writer: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.memory = memory
        self._summarizer_fn = summarizer_fn or _default_summarizer
        self._vector_writer = vector_writer

    # -- Public API ---------------------------------------------------------

    def consolidate_agent(
        self,
        agent_id: str,
        *,
        since: Optional[str] = None,
        nap_run_id: Optional[str] = None,
        embed_into_medium: bool = True,
        created_by: Optional[str] = None,
    ) -> JsonDict:
        """Consolidate everything the agent has written since `since`.

        Returns a report dict: how many records were considered, how
        many summary records were written, how many vector_refs were
        created, and any per-group errors.
        """
        agent_id = (agent_id or "").strip()
        if not agent_id:
            raise ValidationError("agent_id is required")
        window_start = since or self._latest_nap_window_end(agent_id) or ""
        window_end = utcnow()
        creator = created_by or "nap-consolidator:%s" % agent_id

        records = self._records_for_agent_since(agent_id, window_start)
        groups = self._group_records(records)
        summary_ids: List[str] = []
        embedded_count = 0
        per_group_errors: List[JsonDict] = []

        for group_key, group_records in groups.items():
            try:
                context = {
                    "agent_id": agent_id,
                    "nap_run_id": nap_run_id,
                    "group_label": self._group_label(group_key),
                    "window_start": window_start or "<beginning>",
                    "window_end": window_end,
                    "task_id": group_key[0],
                    "project": group_key[1],
                }
                content = self._summarizer_fn(group_records, context)
                if not content.strip():
                    continue
                summary = self.memory.add_memory(
                    task_id=group_key[0],
                    subject_type="nap_summary",
                    subject_id=agent_id,
                    record_type="nap_summary",
                    content=content,
                    evidence_id=None,
                    created_by=creator,
                )
                summary_ids.append(summary.id)
                if embed_into_medium and self._vector_writer is not None:
                    try:
                        self._vector_writer.embed_memory(
                            summary.id,
                            tier=MacMemoryTier.MEDIUM.value,
                            created_by=creator,
                        )
                        embedded_count += 1
                    except Exception as exc:  # noqa: BLE001
                        per_group_errors.append(
                            {
                                "group": list(group_key),
                                "phase": "embed",
                                "summary_id": summary.id,
                                "error": str(exc),
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                per_group_errors.append(
                    {
                        "group": list(group_key),
                        "phase": "summarize_or_write",
                        "error": str(exc),
                    }
                )
        return {
            "agent_id": agent_id,
            "nap_run_id": nap_run_id,
            "window_start": window_start,
            "window_end": window_end,
            "records_considered": len(records),
            "groups": len(groups),
            "summaries_written": len(summary_ids),
            "summary_memory_ids": summary_ids,
            "summaries_embedded": embedded_count,
            "errors": per_group_errors,
        }

    # -- Internals ----------------------------------------------------------

    def _records_for_agent_since(
        self, agent_id: str, since: str
    ) -> List[MemoryRecord]:
        """Memory records the agent authored after `since`, excluding
        prior nap_summary rows (don't summarize summaries)."""
        clauses = ["created_by = ?", "record_type != ?"]
        params: List[Any] = [agent_id, "nap_summary"]
        if since:
            clauses.append("created_at > ?")
            params.append(since)
        sql = (
            "SELECT * FROM memory_records WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at, id"
        )
        rows = self.store.query_all(sql, tuple(params))
        out: List[MemoryRecord] = []
        for row in rows:
            out.append(
                MemoryRecord(
                    id=row["id"],
                    task_id=row["task_id"],
                    subject_type=row["subject_type"],
                    subject_id=row["subject_id"],
                    record_type=row["record_type"],
                    content=row["content"],
                    evidence_id=row["evidence_id"],
                    created_by=row["created_by"],
                    created_at=row["created_at"],
                )
            )
        return out

    def _latest_nap_window_end(self, agent_id: str) -> Optional[str]:
        """Lower bound for "what to summarize this pass".

        Prefer the most recent nap_summary memory written for this
        agent: that timestamp is the strongest "we've already
        consolidated up to here" marker because operators may run the
        consolidator without coordinating with the nap_run lifecycle.
        Fall back to the most recent completed nap_run, so a
        consolidator invoked AFTER a clean nap-begin/complete cycle
        still scopes correctly.
        """
        summary_row = self.store.query_one(
            """
            SELECT created_at FROM memory_records
            WHERE created_by = ? AND record_type = 'nap_summary'
            ORDER BY created_at DESC LIMIT 1
            """,
            ("nap-consolidator:%s" % agent_id,),
        )
        if summary_row is not None:
            return summary_row["created_at"]
        run_row = self.store.query_one(
            """
            SELECT completed_at FROM nap_runs
            WHERE agent_id = ? AND status = 'completed' AND completed_at IS NOT NULL
            ORDER BY completed_at DESC LIMIT 1
            """,
            (agent_id,),
        )
        return run_row["completed_at"] if run_row is not None else None

    def _group_records(
        self, records: List[MemoryRecord]
    ) -> "Dict[tuple, List[MemoryRecord]]":
        """Group by (task_id, project-as-best-guess). For ambient
        records that don't belong to a task, the group key is (None,
        subject_id) which buckets project-level facts together."""
        groups: Dict[tuple, List[MemoryRecord]] = defaultdict(list)
        for rec in records:
            if rec.task_id:
                key = (rec.task_id, None)
            else:
                # Subject id is the closest stable grouping key for
                # ambient records (e.g., subject_type=project).
                key = (None, rec.subject_id or rec.subject_type)
            groups[key].append(rec)
        return groups

    def _group_label(self, key: tuple) -> str:
        task_id, ambient = key
        if task_id:
            return "task=%s" % task_id
        return "ambient=%s" % (ambient or "<unscoped>")
