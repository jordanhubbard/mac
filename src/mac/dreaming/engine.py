"""Orchestration: run one dream end to end.

    freeze -> extract -> resolve -> compress -> gate -> ready_for_review
                                                     \\-> quarantined

The shape follows the reference pseudocode exactly, including the property
that a gate failure produces a *quarantined* store rather than an exception.
A quarantined run is kept and readable — you want to look at what the pipeline
wanted to write and why it was rejected.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

from mac.dreaming.gates import gate_summary, run_all_gates
from mac.dreaming.models import (
    DreamPolicy,
    DreamResult,
    InputRecord,
    InputSession,
    MemoryKind,
    Snapshot,
    StoreState,
    outcome_counts,
)
from mac.dreaming.pipeline import (
    ModelCaller,
    compress_with_provenance,
    extract_candidates,
    freeze_inputs,
    resolve_duplicates_and_conflicts,
)
from mac.models import JsonDict, new_id


def dream(
    records: Iterable[InputRecord],
    sessions: Iterable[InputSession] = (),
    *,
    existing: Iterable[InputRecord] = (),
    policy: Optional[DreamPolicy] = None,
    model: str = "",
    model_caller: Optional[ModelCaller] = None,
) -> DreamResult:
    """Run the pipeline over frozen inputs and return the candidate store.

    Pure with respect to the hub: nothing is read or written outside the
    supplied inputs. :func:`mac.dreaming.store.save_run` persists the result;
    :func:`mac.dreaming.store.promote_run` adopts it.
    """

    policy = policy or DreamPolicy()
    run_id = new_id("dreamrun")
    errors: List[str] = []

    snapshot = freeze_inputs(records, sessions, existing)

    try:
        raw_candidates, reflections, extractor = extract_candidates(
            snapshot, policy, model=model, model_caller=model_caller
        )
    except Exception as exc:  # noqa: BLE001 - a failed extract is a failed run
        return DreamResult(
            run_id=run_id,
            state=StoreState.QUARANTINED,
            stats={
                "input_store_size": snapshot.input_size,
                "input_sessions": len(snapshot.sessions),
            },
            errors=["extract: %s" % str(exc)[:300]],
            extractor="none",
        )

    resolved = resolve_duplicates_and_conflicts(raw_candidates, snapshot, policy)
    candidates = compress_with_provenance(resolved, policy)
    gates = run_all_gates(candidates, reflections, snapshot, policy)
    passed = all(gate.passed for gate in gates)

    stats: JsonDict = {
        "input_records": len(snapshot.records),
        "input_existing_memories": len(snapshot.existing),
        "input_store_size": snapshot.input_size,
        "input_sessions": len(snapshot.sessions),
        "extracted": len(raw_candidates),
        "after_resolve": len(resolved),
        "output": len(candidates),
        "merged_away": max(0, len(raw_candidates) - len(resolved)),
        "kinds": _kind_counts(candidates),
        "session_outcomes": outcome_counts(reflections),
        "supersedes_total": sum(len(candidate.supersedes) for candidate in candidates),
        "gates": gate_summary(gates),
    }
    return DreamResult(
        run_id=run_id,
        state=StoreState.READY_FOR_REVIEW if passed else StoreState.QUARANTINED,
        candidates=candidates,
        reflections=reflections,
        gates=gates,
        stats=stats,
        errors=errors,
        extractor=extractor,
    )


def _kind_counts(candidates: Sequence[Any]) -> Dict[str, int]:
    counts = {kind.value: 0 for kind in MemoryKind}
    for candidate in candidates:
        counts[candidate.kind.value] = counts.get(candidate.kind.value, 0) + 1
    return {name: count for name, count in counts.items() if count}


# ---------------------------------------------------------------------------
# Hub adapters — turning ledger rows into pipeline inputs
# ---------------------------------------------------------------------------

#: Record types the pipeline should never read back as input. Its own promoted
#: output is excluded so a dream cannot feed on its own conclusions; that
#: self-referential loop is what four separate investigations in ``docs/``
#: identified as the source of unactionable findings.
_EXCLUDED_INPUT_TYPES = (
    "nap_summary",
    "dream:%",
    "dream_memory:%",
)


def load_records(
    store: Any,
    *,
    project: Optional[str] = None,
    agent_id: Optional[str] = None,
    since: str = "",
    limit: int = 2000,
) -> List[InputRecord]:
    """Read candidate input rows out of ``memory_records``."""

    clauses = ["record_type NOT LIKE ?" for _ in _EXCLUDED_INPUT_TYPES]
    params: List[Any] = list(_EXCLUDED_INPUT_TYPES)
    if since:
        clauses.append("created_at > ?")
        params.append(since)
    if agent_id:
        clauses.append("(created_by = ? OR subject_id = ?)")
        params.extend([agent_id, agent_id])
    sql = (
        "SELECT id, record_type, content, task_id, subject_id, created_at "
        "FROM memory_records WHERE " + " AND ".join(clauses) +
        " ORDER BY created_at DESC LIMIT ?"
    )
    params.append(int(limit))
    rows = store.query_all(sql, tuple(params))
    records: List[InputRecord] = []
    for row in rows:
        record_project = project or _project_from_record_type(row["record_type"])
        records.append(
            InputRecord(
                id=row["id"],
                record_type=row["record_type"],
                content=row["content"] or "",
                task_id=row["task_id"],
                project=record_project,
                subject_id=row["subject_id"],
                created_at=row["created_at"] or "",
            )
        )
    return records


def load_existing_memories(
    store: Any,
    *,
    project: Optional[str] = None,
    limit: int = 500,
) -> List[InputRecord]:
    """Read previously promoted dream memories — the store being re-curated.

    These are handed to the extractor as prior state to supersede, never as
    evidence to mine. Without this a dream could not revise its own earlier
    conclusions, and the curated store would accumulate stale entries: the
    same disease as the old cycle, only slower.
    """

    params: List[Any] = ["dream_memory:%"]
    clauses = ["record_type LIKE ?"]
    if project:
        clauses.append("subject_id = ?")
        params.append(project)
    params.append(int(limit))
    rows = store.query_all(
        "SELECT id, record_type, content, task_id, subject_id, created_at "
        "FROM memory_records WHERE " + " AND ".join(clauses) +
        " ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    return [
        InputRecord(
            id=row["id"],
            record_type=row["record_type"],
            content=row["content"] or "",
            task_id=row["task_id"],
            project=project or row["subject_id"],
            subject_id=row["subject_id"],
            created_at=row["created_at"] or "",
        )
        for row in rows
    ]


def _project_from_record_type(record_type: Any) -> Optional[str]:
    """``deployment_learning:mac`` -> ``mac``.

    Repo-agnostic on purpose: the project comes from the data, not from a
    hardcoded pattern table. The old classifier matched a fixed list of
    ``mac.*`` module paths, so findings from any other repository classified
    as no-affected-area and were silently dropped.
    """

    text = str(record_type or "")
    if ":" not in text:
        return None
    suffix = text.split(":", 1)[1].strip()
    return suffix or None


def resolve_model_caller(env: Optional[Dict[str, str]] = None) -> tuple:
    """Build ``(model, caller)`` from the environment, or ``("", None)``.

    Reuses the router seam the executor already uses for lesson curation, so
    dreaming inherits the fleet's routing, keys and fallbacks rather than
    inventing a second path to a model.
    """

    environ = os.environ if env is None else env
    router_url = (
        environ.get("MAC_ROUTER_URL")
        or environ.get("MAC_ROUTER_INTERNAL_URL")
        or environ.get("OPENAI_BASE_URL")
        or ""
    ).strip()
    model = (
        environ.get("MAC_DREAM_MODEL")
        or environ.get("MAC_LESSON_CURATION_MODEL")
        or environ.get("MAC_TASK_MODEL")
        or ""
    ).strip()
    if not router_url or not model:
        return "", None
    try:
        from mac.eval_runner import router_model_caller
    except Exception:  # noqa: BLE001 - optional dependency path
        return "", None
    token = (environ.get("MAC_API_TOKEN") or "").strip()
    try:
        return model, router_model_caller(router_url, token=token)
    except Exception:  # noqa: BLE001
        return "", None


__all__ = [
    "dream",
    "load_existing_memories",
    "load_records",
    "resolve_model_caller",
]
