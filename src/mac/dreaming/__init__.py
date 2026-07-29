"""Dreaming: asynchronous memory curation.

A dream reads a memory store plus past session transcripts and produces a
*new, smaller* candidate store — duplicates merged, contradictions resolved,
stale entries superseded, wins and pitfalls both recorded — which a human or a
policy then promotes or discards. The input is never modified.

This replaces ``mac.dream_scanner``, ``mac.dream_cycle_classifier`` and
``mac.dream_repair_tasks``, which implemented something different: a keyword
scanner that appended failure findings to the live store on every nap. In forty
days of production that produced 154,273 artifacts carrying 4,414 distinct
statements, zero of them recording anything that worked, and filed 1,259
investigation tasks of which 4 completed.

The four properties this module is built to satisfy:

* **asynchronous** — runs off the request path, in the nap window
* **state-transforming** — merges, resolves and prunes; :func:`gates.compression_gate`
  makes shrinking the store a hard requirement rather than an aspiration
* **reviewable** — produces a candidate store with provenance, never a silent
  mutation of live memory
* **selective** — keeps facts, practices, pitfalls, preferences and unresolved
  obligations; drops transient noise

Typical use::

    from mac import dreaming

    records = dreaming.load_records(store, project="mac", limit=500)
    model, caller = dreaming.resolve_model_caller()
    result = dreaming.dream(records, sessions, model=model, model_caller=caller)
    dreaming.save_run(store, result, dreaming.DreamPolicy())
    if result.state is dreaming.StoreState.READY_FOR_REVIEW:
        dreaming.promote_run(store, memory_service, result.run_id)
"""

from mac.dreaming.engine import (
    dream,
    load_existing_memories,
    load_records,
    load_sessions,
    resolve_model_caller,
)
from mac.dreaming.gates import gate_summary, run_all_gates
from mac.dreaming.models import (
    CANDIDATE_SCHEMA,
    DREAM_SCHEMA_VERSION,
    DreamPolicy,
    DreamResult,
    GateResult,
    InputRecord,
    InputSession,
    MemoryCandidate,
    MemoryKind,
    RunStatus,
    SessionOutcome,
    SessionReflection,
    Snapshot,
    SourceRef,
    StoreState,
    confidence_for,
)
from mac.dreaming.redact import redact
from mac.dreaming.store import (
    PROMOTED_RECORD_PREFIX,
    PROMOTED_SCHEMA,
    discard_run,
    ensure_schema,
    get_run,
    list_runs,
    promote_run,
    promoted_record_types,
    prune_runs,
    save_run,
)

__all__ = [
    "CANDIDATE_SCHEMA",
    "DREAM_SCHEMA_VERSION",
    "PROMOTED_RECORD_PREFIX",
    "PROMOTED_SCHEMA",
    "DreamPolicy",
    "DreamResult",
    "GateResult",
    "InputRecord",
    "InputSession",
    "MemoryCandidate",
    "MemoryKind",
    "RunStatus",
    "SessionOutcome",
    "SessionReflection",
    "Snapshot",
    "SourceRef",
    "StoreState",
    "confidence_for",
    "discard_run",
    "dream",
    "ensure_schema",
    "gate_summary",
    "get_run",
    "list_runs",
    "load_existing_memories",
    "load_records",
    "load_sessions",
    "promote_run",
    "promoted_record_types",
    "prune_runs",
    "redact",
    "resolve_model_caller",
    "run_all_gates",
    "save_run",
]
