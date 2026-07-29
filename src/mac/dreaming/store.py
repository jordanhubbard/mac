"""Persistence for dream runs and their candidate stores.

Copy-on-write is enforced structurally here: :func:`save_run` only ever writes
to ``dream_runs`` / ``dream_candidate_entries``. The live ``memory_records``
table is touched by exactly one function, :func:`promote_run`, and only after
a run has passed its gates and someone has asked for it.

That is the inversion relative to the old cycle, which wrote its conclusions
straight into ``memory_records`` on every nap with no review step and no way
to undo them.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Sequence

from mac.dreaming.models import (
    DreamPolicy,
    DreamResult,
    MemoryCandidate,
    MemoryKind,
    RunStatus,
    StoreState,
)
from mac.models import (
    JsonDict,
    NotFoundError,
    ValidationError,
    json_dumps,
    json_loads,
    utcnow,
)

PROMOTED_RECORD_PREFIX = "dream_memory"
PROMOTED_SCHEMA = "mac.dream_memory.v2"

#: Ceiling on live rows a single promotion may retire. Retirement is the only
#: irreversible step in the pipeline and runs unattended under auto-promotion,
#: so one bad run must not be able to delete the store.
#:
#: Measured against real hub data, a 1,500-record input retires 455 rows, so
#: live per-agent volumes (3,000+) will reach this cap routinely. That is the
#: intended safe outcome -- retirement halts, the promotion still completes,
#: and later runs finish the compaction -- but it is tunable via
#: ``MAC_DREAM_MAX_RETIRE_PER_RUN`` for operators who want faster convergence.
MAX_RETIRE_PER_RUN = 500
MAX_RETIRE_ENV = "MAC_DREAM_MAX_RETIRE_PER_RUN"
_MIN_RETIRE_CAP = 1
_MAX_RETIRE_CAP = 100000


def resolve_retire_cap(environ: Optional[Mapping[str, str]] = None) -> int:
    """Retirement ceiling from the environment, clamped to a sane range."""

    from mac.config_coercion import bounded_env_int

    env = os.environ if environ is None else environ
    errors: List[str] = []
    return bounded_env_int(
        env,
        MAX_RETIRE_ENV,
        MAX_RETIRE_PER_RUN,
        _MIN_RETIRE_CAP,
        _MAX_RETIRE_CAP,
        errors=errors,
    )

_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS dream_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    state TEXT NOT NULL,
    agent_id TEXT,
    project TEXT,
    extractor TEXT,
    policy TEXT NOT NULL,
    gates TEXT NOT NULL,
    stats TEXT NOT NULL,
    reflections TEXT NOT NULL,
    errors TEXT NOT NULL,
    input_record_count INTEGER NOT NULL DEFAULT 0,
    input_session_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    created_at TEXT NOT NULL,
    promoted_at TEXT
)
"""

_ENTRIES_DDL = """
CREATE TABLE IF NOT EXISTS dream_candidate_entries (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    statement TEXT NOT NULL,
    scope TEXT,
    project TEXT,
    agent_id TEXT,
    applies_when TEXT,
    confidence TEXT,
    confidence_score REAL,
    source_count INTEGER,
    sources TEXT,
    supersedes TEXT,
    contradicts TEXT,
    promoted_memory_id TEXT,
    created_at TEXT NOT NULL
)
"""

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_dream_entries_run ON dream_candidate_entries (run_id)",
    "CREATE INDEX IF NOT EXISTS idx_dream_runs_state ON dream_runs (state, created_at)",
)


def ensure_schema(store: Any) -> None:
    """Create the candidate-store tables if they are missing.

    SQL is SQLite-shaped by house convention; the Postgres backend translates.
    """

    store.execute(_RUNS_DDL)
    store.execute(_ENTRIES_DDL)
    for statement in _INDEX_DDL:
        store.execute(statement)


def save_run(
    store: Any,
    result: DreamResult,
    policy: DreamPolicy,
    *,
    agent_id: Optional[str] = None,
    project: Optional[str] = None,
    created_by: str = "dreaming",
    input_record_count: int = 0,
    input_session_count: int = 0,
) -> str:
    """Persist a completed run and its candidates. Never touches live memory."""

    ensure_schema(store)
    now = utcnow()
    store.execute(
        """
        INSERT INTO dream_runs (
            id, status, state, agent_id, project, extractor, policy, gates,
            stats, reflections, errors, input_record_count,
            input_session_count, created_by, created_at, promoted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            result.run_id,
            RunStatus.COMPLETED.value,
            result.state.value,
            agent_id,
            project,
            result.extractor,
            json_dumps(policy.to_dict()),
            json_dumps([gate.to_dict() for gate in result.gates]),
            json_dumps(dict(result.stats)),
            json_dumps([reflection.to_dict() for reflection in result.reflections]),
            json_dumps(list(result.errors)),
            int(input_record_count),
            int(input_session_count),
            created_by,
            now,
        ),
    )
    for candidate in result.candidates:
        store.execute(
            """
            INSERT INTO dream_candidate_entries (
                id, run_id, kind, statement, scope, project, agent_id,
                applies_when, confidence, confidence_score, source_count,
                sources, supersedes, contradicts, promoted_memory_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                candidate.id,
                result.run_id,
                candidate.kind.value,
                candidate.statement,
                candidate.scope,
                candidate.project,
                candidate.agent_id,
                candidate.applies_when,
                candidate.confidence,
                candidate.confidence_score,
                candidate.source_count,
                json_dumps([ref.to_dict() for ref in candidate.sources]),
                json_dumps(list(candidate.supersedes)),
                json_dumps(list(candidate.contradicts)),
                candidate.created_at,
            ),
        )
    return result.run_id


def get_run(store: Any, run_id: str) -> Optional[JsonDict]:
    ensure_schema(store)
    row = store.query_one("SELECT * FROM dream_runs WHERE id = ?", (run_id,))
    if row is None:
        return None
    run = _run_row_to_dict(row)
    run["candidates"] = [
        _entry_row_to_dict(entry)
        for entry in store.query_all(
            "SELECT * FROM dream_candidate_entries WHERE run_id = ? ORDER BY source_count DESC, id",
            (run_id,),
        )
    ]
    return run


def list_runs(
    store: Any,
    *,
    state: Optional[str] = None,
    limit: int = 20,
) -> List[JsonDict]:
    ensure_schema(store)
    if state:
        rows = store.query_all(
            "SELECT * FROM dream_runs WHERE state = ? ORDER BY created_at DESC LIMIT ?",
            (state, int(limit)),
        )
    else:
        rows = store.query_all(
            "SELECT * FROM dream_runs ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        )
    return [_run_row_to_dict(row) for row in rows]


def promote_run(
    store: Any,
    memory_service: Any,
    run_id: str,
    *,
    actor: str = "dreaming",
    retire_superseded: bool = True,
    vector_writer: Any = None,
    max_retire: Optional[int] = None,
) -> JsonDict:
    """Adopt a reviewed run into live memory.

    Two things happen, in this order:

    1. Each candidate is written as a ``dream_memory:<kind>`` record and, when
       a vector writer is supplied, embedded so recall can actually reach it.
       (The old cycle wrote 154,273 artifacts and embedded 253 of them, so
       essentially none were retrievable.)
    2. The input rows each candidate supersedes are retired, which is what
       makes the store shrink rather than grow.

    Refuses to promote a run that did not pass its gates.

    ``max_retire`` bounds how many live rows one promotion may delete. Step 2
    is the only irreversible thing this pipeline does, and it becomes unattended
    once auto-promotion is on, so a single malformed run cannot quietly remove
    thousands of records. Hitting the cap stops retirement and is reported in
    ``retire_capped``; the promotion itself still succeeds.
    """

    run = get_run(store, run_id)
    if run is None:
        raise NotFoundError("unknown dream run: %s" % run_id)
    if run.get("state") == StoreState.PROMOTED.value:
        return {"schema": "mac.dream_promotion.v2", "run_id": run_id, "status": "already_promoted"}
    if run.get("state") != StoreState.READY_FOR_REVIEW.value:
        raise ValidationError(
            "run %s is %s; only ready_for_review runs may be promoted"
            % (run_id, run.get("state"))
        )

    if max_retire is None:
        max_retire = resolve_retire_cap()
    promoted: List[JsonDict] = []
    retired = 0
    embedded = 0
    retire_capped = False
    errors: List[JsonDict] = []
    for entry in run.get("candidates") or []:
        payload = {
            "schema": PROMOTED_SCHEMA,
            "kind": entry.get("kind"),
            "statement": entry.get("statement"),
            "applies_when": entry.get("applies_when"),
            "confidence": entry.get("confidence"),
            "source_count": entry.get("source_count"),
            "sources": entry.get("sources"),
            "run_id": run_id,
        }
        try:
            memory = memory_service.add_memory(
                task_id=None,
                subject_type="dream_memory",
                subject_id=entry.get("project") or entry.get("agent_id"),
                record_type="%s:%s" % (PROMOTED_RECORD_PREFIX, entry.get("kind")),
                content=json_dumps(payload),
                evidence_id=None,
                created_by=actor,
            )
        except Exception as exc:  # noqa: BLE001 - one bad entry must not abort
            errors.append({"entry_id": entry.get("id"), "phase": "add_memory", "error": str(exc)[:300]})
            continue
        store.execute(
            "UPDATE dream_candidate_entries SET promoted_memory_id = ? WHERE id = ?",
            (memory.id, entry.get("id")),
        )
        promoted.append({"entry_id": entry.get("id"), "memory_id": memory.id})
        if vector_writer is not None:
            try:
                vector_writer.embed_memory(memory.id, tier="medium", created_by=actor)
                embedded += 1
            except Exception as exc:  # noqa: BLE001 - memory persists unembedded
                errors.append({"memory_id": memory.id, "phase": "embed", "error": str(exc)[:300]})
        if retire_superseded:
            # Already parsed into a list by _entry_row_to_dict.
            for superseded_id in entry.get("supersedes") or []:
                if retired >= max_retire:
                    retire_capped = True
                    break
                try:
                    store.execute(
                        "DELETE FROM memory_records WHERE id = ?", (str(superseded_id),)
                    )
                    store.execute(
                        "DELETE FROM vector_refs WHERE memory_id = ?", (str(superseded_id),)
                    )
                    retired += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {"memory_id": superseded_id, "phase": "retire", "error": str(exc)[:300]}
                    )

    store.execute(
        "UPDATE dream_runs SET state = ?, promoted_at = ? WHERE id = ?",
        (StoreState.PROMOTED.value, utcnow(), run_id),
    )
    return {
        "schema": "mac.dream_promotion.v2",
        "run_id": run_id,
        "status": "promoted",
        "promoted_count": len(promoted),
        "embedded_count": embedded,
        "retired_count": retired,
        "retire_capped": retire_capped,
        "retire_cap": int(max_retire),
        "net_change": len(promoted) - retired,
        "promoted": promoted,
        "errors": errors,
    }


def discard_run(store: Any, run_id: str, *, reason: str = "") -> JsonDict:
    """Mark a run as discarded. The candidates stay readable for inspection."""

    ensure_schema(store)
    store.execute(
        "UPDATE dream_runs SET state = ? WHERE id = ?",
        (StoreState.DISCARDED.value, run_id),
    )
    return {
        "schema": "mac.dream_discard.v2",
        "run_id": run_id,
        "status": "discarded",
        "reason": reason,
    }


#: Runs to keep per state before pruning. Promoted runs are the audit trail
#: for memories that are live, so they are kept longest; quarantined runs are
#: diagnostic and go first.
DEFAULT_RETENTION = {
    StoreState.PROMOTED.value: 200,
    StoreState.READY_FOR_REVIEW.value: 50,
    StoreState.QUARANTINED.value: 50,
    StoreState.DISCARDED.value: 20,
}


def prune_runs(
    store: Any,
    *,
    retention: Optional[Dict[str, int]] = None,
) -> JsonDict:
    """Bound the size of the run history.

    The nap cycle calls a dream per agent per nap, so without this the run
    tables grow without limit — which is precisely the failure this rewrite
    exists to fix. Keeping the most recent N per state preserves the audit
    trail for anything live while capping the diagnostic tail.

    A promoted run is never pruned while any of its candidates is still the
    provenance for a live memory, so promoted retention is the largest.
    """

    ensure_schema(store)
    limits = dict(DEFAULT_RETENTION)
    limits.update(retention or {})
    deleted_runs = 0
    deleted_entries = 0
    for state, keep in limits.items():
        # Rank empty runs as stale ahead of productive ones. Pruning purely by
        # recency let a stream of zero-candidate runs evict the only run that
        # had produced anything: the live ledger reached 50 runs holding 0
        # candidate entries because the one run with 198 of them aged out.
        rows = store.query_all(
            """
            SELECT r.id,
                   (SELECT count(*) FROM dream_candidate_entries e
                     WHERE e.run_id = r.id) AS entry_count
              FROM dream_runs r
             WHERE r.state = ?
             ORDER BY r.created_at DESC
            """,
            (state,),
        )
        productive = [row["id"] for row in rows if int(row["entry_count"] or 0) > 0]
        empty = [row["id"] for row in rows if not int(row["entry_count"] or 0)]
        keep_count = max(0, int(keep))
        # Productive runs claim the retention budget first; empty runs keep
        # only whatever is left over, so a burst of them cannot displace real
        # output. When productive runs fill the budget, every empty one goes.
        stale = productive[keep_count:] + empty[max(0, keep_count - len(productive)) :]
        for run_id in stale:
            store.execute(
                "DELETE FROM dream_candidate_entries WHERE run_id = ?", (run_id,)
            )
            deleted_entries += 1
            store.execute("DELETE FROM dream_runs WHERE id = ?", (run_id,))
            deleted_runs += 1
    return {
        "schema": "mac.dream_prune.v2",
        "deleted_runs": deleted_runs,
        "deleted_entry_batches": deleted_entries,
        "retention": limits,
    }


def _run_row_to_dict(row: Any) -> JsonDict:
    data = dict(row)
    for key in ("policy", "gates", "stats", "reflections", "errors"):
        data[key] = json_loads(data.get(key) or "null", None)
    return data


def _entry_row_to_dict(row: Any) -> JsonDict:
    data = dict(row)
    for key in ("sources", "supersedes", "contradicts"):
        data[key] = json_loads(data.get(key) or "[]", [])
    return data


def promoted_record_types() -> List[str]:
    """Record types a promoted dream memory can carry — used by recall."""

    return ["%s:%s" % (PROMOTED_RECORD_PREFIX, kind.value) for kind in MemoryKind]


__all__ = [
    "MAX_RETIRE_ENV",
    "MAX_RETIRE_PER_RUN",
    "PROMOTED_RECORD_PREFIX",
    "PROMOTED_SCHEMA",
    "discard_run",
    "ensure_schema",
    "get_run",
    "list_runs",
    "promote_run",
    "promoted_record_types",
    "prune_runs",
    "resolve_retire_cap",
    "save_run",
]
