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
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

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
        # Hub-side writers attach learning to the *task* while using their own
        # service actor as created_by (mac-task-executor, hub-review-workflow,
        # ...) and the *project* as subject_id. Matching only created_by /
        # subject_id against an agent id therefore selects nothing at all: the
        # first live runs of this pipeline returned 0 candidates for exactly
        # that reason. Treat task ownership and task history as the agent
        # relationship, mirroring mac.nap_consolidator._records_for_agent_since.
        clauses.append(
            """
            (
                created_by = ?
                OR subject_id = ?
                OR (
                    task_id IS NOT NULL
                    AND (
                        task_id IN (SELECT id FROM tasks WHERE owner_agent_id = ?)
                        OR task_id IN (
                            SELECT task_id FROM task_history WHERE actor = ?
                        )
                    )
                )
            )
            """
        )
        params.extend([agent_id, agent_id, agent_id, agent_id])
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


def load_sessions(
    store: Any,
    *,
    agent_id: Optional[str] = None,
    since: str = "",
    max_messages: int = 600,
    max_sessions: int = 20,
    max_turns: int = 60,
) -> List[InputSession]:
    """Reconstruct conversations from the hub's ``messages`` ledger.

    Mining transcripts is the point of dreaming, but ``run_dream_cycle``
    originally passed an empty session list, so the extractor was asked to
    reflect on "(no transcripts supplied)". Agent-to-agent messages keyed by
    ``task_id`` are the closest thing the hub holds to a session, and grouping
    them per task gives both material to mine and a unit for
    :class:`SessionReflection` to judge objective-met against.

    Best-effort: a store without a ``messages`` table returns no sessions
    rather than failing the run.
    """

    clauses = ["task_id IS NOT NULL"]
    params: List[Any] = []
    if agent_id:
        clauses.append("(sender_agent_id = ? OR recipient_agent_id = ?)")
        params.extend([agent_id, agent_id])
    if since:
        clauses.append("created_at > ?")
        params.append(since)
    params.append(int(max_messages))
    sql = (
        "SELECT id, sender_agent_id, recipient_agent_id, task_id, message_type,"
        " payload, created_at FROM messages WHERE " + " AND ".join(clauses) +
        " ORDER BY created_at DESC LIMIT ?"
    )
    try:
        rows = store.query_all(sql, tuple(params))
    except Exception:  # noqa: BLE001 - no messages table is not a run failure
        return []

    grouped: Dict[str, List[Dict[str, str]]] = {}
    started: Dict[str, str] = {}
    for row in reversed(list(rows)):  # oldest first within each conversation
        task_id = str(row["task_id"] or "").strip()
        if not task_id:
            continue
        turns = grouped.setdefault(task_id, [])
        if len(turns) >= max_turns:
            continue
        turns.append(
            {
                "role": str(row["sender_agent_id"] or row["message_type"] or "?"),
                "text": str(row["payload"] or ""),
            }
        )
        started.setdefault(task_id, str(row["created_at"] or ""))

    sessions = [
        InputSession(id=task_id, turns=turns, started_at=started.get(task_id, ""))
        for task_id, turns in grouped.items()
        if turns
    ]
    sessions.sort(key=lambda session: session.started_at, reverse=True)
    return sessions[:max_sessions]


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
    timeout = _bounded_float(environ, MODEL_TIMEOUT_ENV, 90.0, 5.0, 600.0)
    budget = _bounded_float(environ, MODEL_RETRY_BUDGET_ENV, 240.0, 5.0, 1800.0)
    try:
        caller = router_model_caller(router_url, token=token, timeout=timeout)
    except Exception:  # noqa: BLE001
        return "", None
    return model, _retrying(caller, budget=budget)


#: Seconds one extract call may take before the router client gives up. The
#: default in ``router_model_caller`` is 60s; a dream extract is a large
#: synthesis over ~25KB of evidence, so it gets more headroom.
MODEL_TIMEOUT_ENV = "MAC_DREAM_MODEL_TIMEOUT_SECONDS"
#: Total wall-clock a single extract may spend across retries. Bounded so a
#: nap cycle cannot stall behind an unavailable provider.
MODEL_RETRY_BUDGET_ENV = "MAC_DREAM_MODEL_RETRY_BUDGET_SECONDS"

#: Failures worth retrying: the upstream provider is intermittently
#: unreachable rather than the request being wrong. Measured on the live hub,
#: the router answers such calls with an immediate
#: ``503 all_providers_unavailable`` ("no provider could serve model=...",
#: attempts=[{provider: nvidia, status: null}]), and sometimes hangs until the
#: client timeout instead. Both are transient; a wrong model name or a bad
#: token is not, and must fail straight through to the fallback.
_TRANSIENT_MARKERS = (
    "503",
    "service unavailable",
    "all_providers_unavailable",
    "no provider could serve",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "bad gateway",
    "502",
    "504",
)


def _bounded_float(
    environ: Mapping[str, str], name: str, default: float, low: float, high: float
) -> float:
    try:
        value = float(str(environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _is_transient(exc: BaseException) -> bool:
    text = ("%s %s" % (type(exc).__name__, exc)).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _retrying(caller: ModelCaller, *, budget: float) -> ModelCaller:
    """Retry an extract call while the failure looks like provider flakiness.

    The provider serving the dream model is intermittently unavailable, which
    cost every affected run its model extraction and pushed it onto the
    heuristic fallback -- 6 of 17 runs in one sample. Retrying inside the
    budget converts most of those into successful model runs without changing
    anything upstream.

    This is mitigation, not a fix: if the provider is genuinely down the
    retries are exhausted and the run still falls back, which is the correct
    outcome. ``budget`` bounds total wall-clock so a nap cannot stall behind a
    dead provider.
    """

    def call(model_id: str, question: str, context: str):
        started = time.monotonic()
        attempt = 0
        last: Optional[BaseException] = None
        while True:
            attempt += 1
            try:
                return caller(model_id, question, context)
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                if not _is_transient(exc):
                    raise
                elapsed = time.monotonic() - started
                # Back off a little so an immediate 503 storm does not become a
                # tight loop, but stay inside the budget.
                delay = min(2.0 * attempt, 10.0)
                if elapsed + delay >= budget:
                    raise
                time.sleep(delay)
        raise last  # pragma: no cover - loop only exits via return/raise

    return call


__all__ = [
    "dream",
    "load_existing_memories",
    "load_records",
    "load_sessions",
    "resolve_model_caller",
]
