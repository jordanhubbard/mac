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
  nap. Operators triggering manually with `mac admin nap consolidate` are
  expected to know what they're doing.

* **Vector handoff.** When a `VectorWriterService` is provided, each
  summary gets embedded into the medium tier immediately. Failures
  there don't roll back the summary — the next backfill catches it.

* **Dream-candidate lineage filter.** ``_default_dreamer`` still runs on
  the manual/API nap-consolidate path (``emit_dream_artifacts=True``).
  A ``mac.deployment_learning.v1`` closure of a dream-repair
  investigation task is not independent failure evidence: on its own it
  must not mint a new low-confidence ``failure_pattern``. Mixed groups
  drop only those self-referential rows. Scheduled ``run_nap_cycle``
  already disables this dreamer; ``mac.dream_scanner``,
  ``mac.dream_cycle_classifier``, and ``mac.dream_repair_tasks`` are
  gone and are not reintroduced here.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from mac.models import (
    JsonDict,
    MacMemoryTier,
    MemoryRecord,
    ValidationError,
    json_dumps,
    json_loads,
    utcnow,
)

# A summarizer takes a list of records + a context dict (e.g., the
# group key, agent_id, time window) and returns the summary string.
SummarizerFn = Callable[[List[MemoryRecord], Dict[str, Any]], str]
DreamerFn = Callable[[List[MemoryRecord], Dict[str, Any]], List[JsonDict]]

DREAM_SCHEMA = "mac.dream.v1"
DREAM_RECORD_PREFIX = "dream"
DREAM_KINDS = {
    "decision_rule",
    "failure_pattern",
    "knowledge_snippet",
    "tool_pattern",
    "routing_signal",
}
DREAM_SCOPES = {"agent", "project", "fleet"}
_CONFIDENCE_SCORES = {"low": 0.35, "medium": 0.65, "high": 0.9}


#: Marker carrying a summary's body digest, appended to stored content so
#: duplicate detection is an exact match rather than a windowed comparison.
_DIGEST_MARKER = "nap-digest:"


def _summary_digest(content: str) -> str:
    """Digest of a summary's body, ignoring its per-pass provenance header.

    The header carries the window timestamps, which differ on every nap; only
    what follows the first blank line is the actual content.
    """

    body = str(content or "").split("\n\n", 1)[-1].strip()
    if not body:
        return ""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def _stamp_digest(content: str, digest: str) -> str:
    """Append the digest marker so later passes can match this body exactly."""

    if not digest:
        return content
    return "%s\n\n<!-- %s%s -->" % (content, _DIGEST_MARKER, digest)


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


def _record_payload(record: MemoryRecord) -> JsonDict:
    try:
        loaded = json_loads(record.content, {})
    except Exception:  # noqa: BLE001 - malformed memories are still evidence
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _record_observation(record: MemoryRecord) -> str:
    payload = _record_payload(record)
    if payload.get("schema") == "mac.deployment_learning.v1":
        outcome = str(payload.get("outcome") or "?")
        title = str(payload.get("task_title") or payload.get("task_id") or "task")
        evidence_type = str(payload.get("evidence_type") or "?")
        error = str(payload.get("error_signature") or "").strip()
        line = "[%s] %s (%s)" % (outcome, title, evidence_type)
        if error:
            line += " failed with %s" % error
        return line[:300]
    content = (record.content or "").strip().replace("\n", " ")
    return content[:300] if content else "%s memory %s" % (record.record_type, record.id)


def _evidence_for_records(records: List[MemoryRecord]) -> List[JsonDict]:
    return [
        {
            "memory_id": record.id,
            "task_id": record.task_id,
            "record_type": record.record_type,
            "subject_type": record.subject_type,
            "subject_id": record.subject_id,
            "created_at": record.created_at,
        }
        for record in records
    ]


def _dream_kind(records: List[MemoryRecord]) -> str:
    joined = "\n".join("%s\n%s" % (record.record_type, record.content) for record in records).lower()
    if "failure" in joined or "failed" in joined or "error" in joined:
        return "failure_pattern"
    if any(record.record_type.startswith("deployment_learning") for record in records):
        return "decision_rule"
    if "tool" in joined or "command" in joined:
        return "tool_pattern"
    return "knowledge_snippet"


def _confidence_for_records(records: List[MemoryRecord]) -> Tuple[str, float]:
    support = len(records)
    if support >= 3:
        return "high", _CONFIDENCE_SCORES["high"]
    if support == 2:
        return "medium", _CONFIDENCE_SCORES["medium"]
    return "low", _CONFIDENCE_SCORES["low"]


#: Title fragments that mark a task as a dream-repair investigation rather
#: than an independent operational failure. Matched case-insensitively
#: against ``mac.deployment_learning.v1`` ``task_title``.
_DREAM_REPAIR_INVESTIGATION_TITLE_MARKERS = (
    "dream finding",
    "dream-repair",
    "dream repair",
    "dreamrepair",
)


def _is_dream_repair_investigation_learning(record: MemoryRecord) -> bool:
    """True when ``record`` is the outcome memory of a dream-repair investigation.

    Predicate: schema is ``mac.deployment_learning.v1`` (or the record type
    is a ``deployment_learning:*`` lesson) **and** the task title names a
    dream-repair / dream-finding investigation. Those closures echo the
    investigation's own ``failure``/``failed`` outcome into
    ``_dream_kind``, which would otherwise mint a new ``failure_pattern``
    about the same lineage. Unrelated deployment lessons are not matched.
    """
    payload = _record_payload(record)
    is_learning = payload.get("schema") == "mac.deployment_learning.v1" or str(
        record.record_type or ""
    ).startswith("deployment_learning")
    if not is_learning:
        return False
    title = str(payload.get("task_title") or "").lower()
    return any(marker in title for marker in _DREAM_REPAIR_INVESTIGATION_TITLE_MARKERS)


def _independent_dream_support(records: List[MemoryRecord]) -> List[MemoryRecord]:
    """Records that may count as evidence for a new dream candidate.

    Dream-repair investigation closures are dropped from ``failure_pattern``
    support. If a group is *only* those closures, the result is empty and
    candidate manufacture stops. Non-failure kinds keep the original list
    so existing decision-rule / tool / knowledge behaviour is unchanged.
    """
    independent = [record for record in records if not _is_dream_repair_investigation_learning(record)]
    if independent:
        return independent
    if records and _dream_kind(records) == "failure_pattern":
        return []
    return list(records)


def _default_dreamer(records: List[MemoryRecord], context: Dict[str, Any]) -> List[JsonDict]:
    """Emit one structured, evidence-backed dream artifact per group.

    This is deliberately conservative: it does not pretend to do deep
    meta-reasoning without an LLM. It builds a typed, recall-friendly
    artifact with provenance so production callers can swap in a richer
    ``dreamer_fn`` without changing storage, embedding, or retrieval.
    Self-referential dream-repair investigation closures are excluded
    from failure-pattern support via :func:`_independent_dream_support`.
    """
    records = _independent_dream_support(records)
    if not records:
        return []
    kind = _dream_kind(records)
    confidence, confidence_score = _confidence_for_records(records)
    project = context.get("project")
    scope = "project" if project else "agent"
    record_types = Counter(record.record_type for record in records)
    observations = [_record_observation(record) for record in records[:5]]
    title = "%s for %s" % (kind.replace("_", " "), context.get("group_label") or "group")
    summary = (
        "%s. Supported by %d memory record(s): %s"
        % (title, len(records), "; ".join(observations[:3]))
    )
    query_terms = sorted(
        {
            kind,
            scope,
            str(project or ""),
            str(context.get("agent_id") or ""),
            *record_types.keys(),
        }
        - {""}
    )
    return [
        {
            "kind": kind,
            "scope": scope,
            "confidence": confidence,
            "confidence_score": confidence_score,
            "summary": summary[:1200],
            "observations": observations,
            "record_type_counts": dict(sorted(record_types.items())),
            "retrieval": {
                "agent_id": context.get("agent_id"),
                "project": project,
                "scope": scope,
                "kinds": [kind],
                "query_terms": query_terms,
                "min_confidence": confidence,
            },
        }
    ]


class NapConsolidatorService:
    """Walk recent agent memory and write summaries plus typed dream artifacts."""

    def __init__(
        self,
        store: Any,
        memory: Any,
        *,
        summarizer_fn: Optional[SummarizerFn] = None,
        dreamer_fn: Optional[DreamerFn] = None,
        vector_writer: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.memory = memory
        self._summarizer_fn = summarizer_fn or _default_summarizer
        self._dreamer_fn = dreamer_fn or _default_dreamer
        self._vector_writer = vector_writer

    # -- Public API ---------------------------------------------------------

    def consolidate_agent(
        self,
        agent_id: str,
        *,
        since: Optional[str] = None,
        nap_run_id: Optional[str] = None,
        embed_into_medium: bool = True,
        emit_dream_artifacts: bool = True,
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
        dream_ids: List[str] = []
        skipped_duplicate_summaries = 0
        embedded_count = 0
        dream_embedded_count = 0
        per_group_errors: List[JsonDict] = []

        for group_key, group_records in groups.items():
            context = {
                "agent_id": agent_id,
                "nap_run_id": nap_run_id,
                "group_label": self._group_label(group_key),
                "window_start": window_start or "<beginning>",
                "window_end": window_end,
                "task_id": group_key[0],
                "project": group_key[1],
                "record_count": len(group_records),
            }
            try:
                content = self._summarizer_fn(group_records, context)
                digest = _summary_digest(content)
                if not content.strip():
                    summary = None
                elif self._summary_already_stored(agent_id, digest):
                    # The window header changes every pass but the body often
                    # does not, so an unguarded write re-stored the same
                    # summary on every nap: 154,324 rows carrying 4,540
                    # distinct bodies in the live ledger before this check.
                    summary = None
                    skipped_duplicate_summaries += 1
                else:
                    summary = self.memory.add_memory(
                        task_id=group_key[0],
                        subject_type="nap_summary",
                        subject_id=agent_id,
                        record_type="nap_summary",
                        content=_stamp_digest(content, digest),
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
                                    "phase": "embed_summary",
                                    "summary_id": summary.id,
                                    "error": str(exc),
                                }
                            )
            except Exception as exc:  # noqa: BLE001
                per_group_errors.append(
                    {
                        "group": list(group_key),
                        "phase": "summarize_or_write_summary",
                        "error": str(exc),
                    }
                )

            if not emit_dream_artifacts:
                continue
            try:
                artifacts = self._normalized_dream_artifacts(group_records, context)
                for artifact in artifacts:
                    dream = self.memory.add_memory(
                        task_id=group_key[0],
                        subject_type="dream",
                        subject_id=self._dream_subject_id(artifact),
                        record_type="%s:%s" % (DREAM_RECORD_PREFIX, artifact["kind"]),
                        content=json_dumps(artifact),
                        evidence_id=None,
                        created_by=creator,
                    )
                    dream_ids.append(dream.id)
                    if embed_into_medium and self._vector_writer is not None:
                        try:
                            self._vector_writer.embed_memory(
                                dream.id,
                                tier=MacMemoryTier.MEDIUM.value,
                                created_by=creator,
                            )
                            dream_embedded_count += 1
                        except Exception as exc:  # noqa: BLE001
                            per_group_errors.append(
                                {
                                    "group": list(group_key),
                                    "phase": "embed_dream",
                                    "dream_id": dream.id,
                                    "error": str(exc),
                                }
                            )
            except Exception as exc:  # noqa: BLE001
                per_group_errors.append(
                    {
                        "group": list(group_key),
                        "phase": "dream_or_write_artifacts",
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
            "summaries_skipped_duplicate": skipped_duplicate_summaries,
            "summaries_embedded": embedded_count,
            "dream_artifacts_written": len(dream_ids),
            "dream_memory_ids": dream_ids,
            "dream_artifacts_embedded": dream_embedded_count,
            "errors": per_group_errors,
        }

    # -- Internals ----------------------------------------------------------

    def _records_for_agent_since(
        self, agent_id: str, since: str
    ) -> List[MemoryRecord]:
        """Memory records the agent touched after `since`, excluding
        prior nap_summary rows (don't summarize summaries).

        Deployment-learning and other hub-side writers often attach
        records to the task while using their own service actor as
        ``created_by``. Treat task ownership/history as the agent
        relationship so those records feed the owning agent's nap
        without making every ambient project record visible to every
        agent.
        """
        clauses = [
            """
            (
                created_by = ?
                OR (
                    task_id IS NOT NULL
                    AND (
                        task_id IN (
                            SELECT id FROM tasks WHERE owner_agent_id = ?
                        )
                        OR task_id IN (
                            SELECT task_id FROM task_history WHERE actor = ?
                        )
                    )
                )
            )
            """,
            "record_type != ?",
            "record_type NOT LIKE ?",
        ]
        params: List[Any] = [
            agent_id,
            agent_id,
            agent_id,
            "nap_summary",
            "%s:%%" % DREAM_RECORD_PREFIX,
        ]
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

    def _summary_already_stored(self, agent_id: str, digest: str) -> bool:
        """True when this agent already stored a summary with this body.

        The first implementation compared bodies across the agent's 25 most
        recent summaries. That window is far too shallow: with many agents and
        many groups an identical body older than 25 rows slipped straight
        through, and the live ledger went back to 5.2:1 duplication within a
        day of being garbage collected.

        Instead each stored summary carries a digest of its body, and the
        lookup matches that digest directly. The subject columns are indexed
        (``idx_memory_subject``), so this narrows to one agent's summaries
        before scanning for the marker rather than reading a fixed window.

        A read failure returns False: writing a possible duplicate is a much
        smaller problem than dropping a real summary.
        """

        if not digest:
            return False
        try:
            row = self.store.query_one(
                """
                SELECT 1 FROM memory_records
                WHERE subject_type = 'nap_summary'
                  AND subject_id = ?
                  AND content LIKE ?
                LIMIT 1
                """,
                (agent_id, "%%%s%s%%" % (_DIGEST_MARKER, digest)),
            )
        except Exception:  # noqa: BLE001 - dedup is an optimisation, not a gate
            return False
        return row is not None

    def _latest_nap_window_end(self, agent_id: str) -> Optional[str]:
        """Lower bound for "what to summarize this pass".

        Prefer the most recent nap_summary memory written for this
        agent: that timestamp is the strongest "we've already
        consolidated up to here" marker because operators may run the
        consolidator without coordinating with the nap_run lifecycle.
        Fall back to the most recent completed nap_run, so a
        consolidator invoked AFTER a clean nap-begin/complete cycle
        still scopes correctly.

        Identify the agent's summaries by ``subject_type``/``subject_id``,
        which is how :meth:`consolidate_agent` writes them and how
        :meth:`_summary_already_stored` reads them. Matching on ``created_by``
        instead does not work: that column holds the ACTOR, which is
        ``nap-consolidator:<agent>`` only when an operator invokes the
        consolidator directly, and ``nap-cycle:<agent>`` (or the ticker's own
        actor) for every nap the fleet drives itself.

        That mismatch was live for over two months. agent_rocky had one
        operator-run summary from 2026-05-30; every ticker-driven nap since
        wrote a differently-attributed row, so this lookup kept returning the
        May timestamp and the window never moved. Measured on the hub
        2026-08-07, its last six naps all reported
        ``window_start: 2026-05-30T03:48:54`` and re-read the same ~744 records
        into the same 435 groups, of which the duplicate guard correctly
        discarded 433-435. The loop looked like it was working -- it was doing
        the work, and the dedup guard was the only reason nothing was written.
        """
        summary_row = self.store.query_one(
            """
            SELECT created_at FROM memory_records
            WHERE subject_type = 'nap_summary' AND subject_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (agent_id,),
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
        task_projects: Dict[str, Optional[str]] = {}
        for rec in records:
            if rec.task_id:
                if rec.task_id not in task_projects:
                    task_projects[rec.task_id] = self._project_for_task(rec.task_id)
                key = (rec.task_id, task_projects[rec.task_id])
            else:
                # Subject id is the closest stable grouping key for
                # ambient records (e.g., subject_type=project).
                if rec.subject_type == "project" and rec.subject_id:
                    key = (None, rec.subject_id)
                else:
                    key = (None, rec.subject_id or rec.subject_type)
            groups[key].append(rec)
        return groups

    def _group_label(self, key: tuple) -> str:
        task_id, project = key
        if task_id:
            suffix = " project=%s" % project if project else ""
            return "task=%s%s" % (task_id, suffix)
        return "ambient=%s" % (project or "<unscoped>")

    def _project_for_task(self, task_id: str) -> Optional[str]:
        row = self.store.query_one("SELECT project FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            return None
        return row["project"]

    def _normalized_dream_artifacts(
        self,
        records: List[MemoryRecord],
        context: Dict[str, Any],
    ) -> List[JsonDict]:
        candidates = self._dreamer_fn(records, context)
        if not candidates:
            return []
        if not isinstance(candidates, list):
            raise ValidationError("dreamer_fn must return a list of objects")
        artifacts: List[JsonDict] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValidationError("dream artifact candidate must be an object")
            artifact = self._normalize_dream_candidate(candidate, records, context)
            if artifact is not None:
                artifacts.append(artifact)
        return artifacts

    def _normalize_dream_candidate(
        self,
        candidate: JsonDict,
        records: List[MemoryRecord],
        context: Dict[str, Any],
    ) -> Optional[JsonDict]:
        summary = str(candidate.get("summary") or candidate.get("lesson") or "").strip()
        if not summary:
            return None
        kind = str(candidate.get("kind") or _dream_kind(records)).strip().lower()
        if kind not in DREAM_KINDS:
            raise ValidationError("unknown dream artifact kind: %s" % kind)
        default_scope = "project" if context.get("project") else "agent"
        scope = str(candidate.get("scope") or default_scope).strip().lower()
        if scope not in DREAM_SCOPES:
            raise ValidationError("unknown dream artifact scope: %s" % scope)
        confidence = str(candidate.get("confidence") or "low").strip().lower()
        if confidence not in _CONFIDENCE_SCORES:
            raise ValidationError("unknown dream artifact confidence: %s" % confidence)
        confidence_score = candidate.get("confidence_score")
        if confidence_score is None:
            confidence_score = _CONFIDENCE_SCORES[confidence]
        confidence_score = max(0.0, min(1.0, float(confidence_score)))
        evidence = candidate.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            evidence = _evidence_for_records(records)
        retrieval = candidate.get("retrieval")
        if not isinstance(retrieval, dict):
            retrieval = {}
        project = candidate.get("project") or context.get("project") or retrieval.get("project")
        agent_id = candidate.get("agent_id") or context.get("agent_id") or retrieval.get("agent_id")
        query_terms = retrieval.get("query_terms")
        if not isinstance(query_terms, list):
            query_terms = []
        query_terms = sorted(
            {
                str(term).strip()
                for term in [
                    *query_terms,
                    kind,
                    scope,
                    project or "",
                    agent_id or "",
                ]
                if str(term).strip()
            }
        )
        normalized: JsonDict = {
            "schema": DREAM_SCHEMA,
            "kind": kind,
            "scope": scope,
            "confidence": confidence,
            "confidence_score": confidence_score,
            "salience": float(candidate.get("salience") or confidence_score),
            "summary": summary[:2000],
            "agent_id": agent_id,
            "project": project,
            "task_id": candidate.get("task_id") or context.get("task_id"),
            "nap_run_id": candidate.get("nap_run_id") or context.get("nap_run_id"),
            "source_window": {
                "start": context.get("window_start"),
                "end": context.get("window_end"),
            },
            "evidence": evidence,
            "retrieval": {
                **retrieval,
                "agent_id": agent_id,
                "project": project,
                "scope": scope,
                "kinds": [kind],
                "query_terms": query_terms,
                "min_confidence": confidence,
            },
            "created_at": context.get("window_end"),
        }
        for optional_key in ("observations", "record_type_counts", "decision_rule", "avoid", "apply_when"):
            if optional_key in candidate:
                normalized[optional_key] = candidate[optional_key]
        return normalized

    def _dream_subject_id(self, artifact: JsonDict) -> str:
        if artifact.get("scope") == "project" and artifact.get("project"):
            return "project:%s" % artifact["project"]
        if artifact.get("scope") == "fleet":
            return "fleet"
        return "agent:%s" % (artifact.get("agent_id") or "unknown")
