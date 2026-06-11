"""Evaluation domain service.

Owns ``eval_sets``, ``eval_runs``, and ``eval_set_events``. An eval set is
a named scoring rubric (with optional baseline + regression threshold);
an eval run is a single scored measurement against a target (typically a
rollout). Rollouts consult ``latest_eval_run`` as a promotion gate.

The ``passed`` field on a run is frozen at insert time. Changing the
baseline does NOT re-compute historical runs — the baseline-change event
in ``eval_set_events`` exists so operators can explain why a re-evaluated
run reads differently from a historical one.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from mac.models import (
    EvalRun,
    EvalScoringDirection,
    EvalSet,
    EvalTargetKind,
    Evidence,
    JsonDict,
    NotFoundError,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.observability_service import ObservabilityService


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


class EvalService:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        *,
        get_evidence: Callable[[str], Evidence],
    ) -> None:
        self.store = store
        self.observability = observability
        self._get_evidence = get_evidence

    # Eval sets ---------------------------------------------------------

    def create_eval_set(
        self,
        name: str,
        scoring: str = EvalScoringDirection.HIGHER_IS_BETTER.value,
        description: str = "",
        baseline_score: Optional[float] = None,
        regression_threshold: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
        created_by: str = "human",
    ) -> EvalSet:
        name = (name or "").strip()
        if not name:
            raise ValidationError("eval_set name is required")
        scoring_value = _state_value(scoring)
        try:
            EvalScoringDirection(scoring_value)
        except ValueError:
            raise ValidationError("unsupported eval scoring direction: %s" % scoring_value)
        if regression_threshold < 0:
            raise ValidationError("regression_threshold must be >= 0")
        now = utcnow()
        eval_id = new_id("evalset")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO eval_sets (
                    id, name, description, scoring, baseline_score, regression_threshold,
                    metadata, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    eval_id,
                    name,
                    description,
                    scoring_value,
                    None if baseline_score is None else float(baseline_score),
                    float(regression_threshold),
                    json_dumps(ensure_json_object(metadata)),
                    created_by,
                    now,
                    now,
                ),
            )
            self.insert_event(
                conn,
                eval_id,
                "eval_set.created",
                created_by,
                {
                    "scoring": scoring_value,
                    "baseline_score": baseline_score,
                    "regression_threshold": float(regression_threshold),
                },
                now,
            )
        return self.get_eval_set(eval_id)

    def get_eval_set(self, eval_set_id_or_name: str) -> EvalSet:
        row = self.store.query_one(
            "SELECT * FROM eval_sets WHERE id = ? OR name = ?",
            (eval_set_id_or_name, eval_set_id_or_name),
        )
        if row is None:
            raise NotFoundError("eval_set not found: %s" % eval_set_id_or_name)
        return self._set_from_row(row)

    def list_eval_sets(self) -> List[EvalSet]:
        rows = self.store.query_all("SELECT * FROM eval_sets ORDER BY name")
        return [self._set_from_row(row) for row in rows]

    def update_eval_set_baseline(
        self,
        eval_set_id_or_name: str,
        baseline_score: float,
        actor: str = "human",
    ) -> EvalSet:
        eval_set = self.get_eval_set(eval_set_id_or_name)
        new_baseline = float(baseline_score)
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE eval_sets SET baseline_score = ?, updated_at = ? WHERE id = ?",
                (new_baseline, now, eval_set.id),
            )
            self.insert_event(
                conn,
                eval_set.id,
                "eval_set.baseline_changed",
                actor,
                {
                    "previous_baseline_score": eval_set.baseline_score,
                    "new_baseline_score": new_baseline,
                },
                now,
            )
        return self.get_eval_set(eval_set.id)

    def list_eval_set_events(self, eval_set_id_or_name: str) -> List[JsonDict]:
        eval_set = self.get_eval_set(eval_set_id_or_name)
        rows = self.store.query_all(
            "SELECT * FROM eval_set_events WHERE eval_set_id = ? ORDER BY created_at, id",
            (eval_set.id,),
        )
        return [
            {
                "id": row["id"],
                "eval_set_id": row["eval_set_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "detail": json_loads(row["detail"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # Eval runs ---------------------------------------------------------

    def record_eval_run(
        self,
        eval_set_id_or_name: str,
        target_kind: str,
        target_id: str,
        score: float,
        detail: Optional[Dict[str, Any]] = None,
        evidence_id: Optional[str] = None,
        created_by: str = "human",
    ) -> EvalRun:
        eval_set = self.get_eval_set(eval_set_id_or_name)
        target_kind_value = _state_value(target_kind)
        try:
            EvalTargetKind(target_kind_value)
        except ValueError:
            raise ValidationError("unsupported eval target_kind: %s" % target_kind_value)
        if not target_id:
            raise ValidationError("eval run target_id is required")
        # mac-tk1b: eval_runs gate rollout promotions but the table had
        # no signer/auth binding — any caller could mint passing runs.
        # Tighten the contract by requiring evidence_id for any run
        # whose score is considered to "pass" against the baseline (or
        # whose target is a rollout). The evidence must be authored by
        # the caller (created_by) so a compromised dispatcher can't
        # both create the eval evidence and the run referencing it.
        created_by_value = (created_by or "").strip()
        if evidence_id is not None:
            evidence = self._get_evidence(evidence_id)
            if evidence.kind != "eval":
                raise ValidationError(
                    "eval run evidence must have kind='eval' (got '%s')" % evidence.kind
                )
            if evidence.created_by and created_by_value and evidence.created_by != created_by_value:
                raise ValidationError(
                    "eval run evidence.created_by (%s) must match run created_by (%s)"
                    % (evidence.created_by, created_by_value)
                )
        elif target_kind_value == EvalTargetKind.ROLLOUT_VERSION.value:
            # A rollout-gating run with no evidence is allowed for now
            # (existing behavior), but record it so audits can spot the
            # gap. The audit/log makes it visible to operators while we
            # roll out a hard requirement.
            self.observability.record_log(
                "eval.run_without_evidence",
                level="warning",
                layer="control_plane",
                source="eval",
                subject_type="eval_set",
                subject_id=eval_set.id,
                detail={
                    "target_kind": target_kind_value,
                    "target_id": target_id,
                    "created_by": created_by_value,
                    "note": "rollout-gating eval_run lacks signed evidence (mac-tk1b)",
                },
            )
        score_f = float(score)
        baseline = eval_set.baseline_score
        threshold = eval_set.regression_threshold
        if baseline is None:
            delta = None
            passed = True
        else:
            delta = score_f - baseline
            if eval_set.scoring == EvalScoringDirection.HIGHER_IS_BETTER.value:
                passed = delta >= -threshold
            else:
                passed = delta <= threshold
        now = utcnow()
        run_id = new_id("evalrun")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO eval_runs (
                    id, eval_set_id, target_kind, target_id, score, baseline_score,
                    delta, threshold, passed, detail, evidence_id, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    eval_set.id,
                    target_kind_value,
                    target_id,
                    score_f,
                    baseline,
                    delta,
                    threshold,
                    1 if passed else 0,
                    json_dumps(ensure_json_object(detail)),
                    evidence_id,
                    created_by,
                    now,
                ),
            )
            self.insert_event(
                conn,
                eval_set.id,
                "eval_set.run_recorded",
                created_by,
                {
                    "run_id": run_id,
                    "target_kind": target_kind_value,
                    "target_id": target_id,
                    "score": score_f,
                    "passed": bool(passed),
                    "evidence_id": evidence_id,
                },
                now,
            )
        return self.get_eval_run(run_id)

    def get_eval_run(self, run_id: str) -> EvalRun:
        row = self.store.query_one("SELECT * FROM eval_runs WHERE id = ?", (run_id,))
        if row is None:
            raise NotFoundError("eval_run not found: %s" % run_id)
        return self._run_from_row(row)

    def latest_eval_run(
        self,
        eval_set_id: str,
        target_kind: str,
        target_id: str,
    ) -> Optional[EvalRun]:
        row = self.store.query_one(
            """
            SELECT * FROM eval_runs
            WHERE eval_set_id = ? AND target_kind = ? AND target_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (eval_set_id, _state_value(target_kind), target_id),
        )
        return self._run_from_row(row) if row is not None else None

    def list_eval_runs(
        self,
        eval_set_id: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> List[EvalRun]:
        clauses: List[str] = []
        params: List[Any] = []
        if eval_set_id is not None:
            clauses.append("eval_set_id = ?")
            params.append(eval_set_id)
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(target_id)
        sql = "SELECT * FROM eval_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        return [self._run_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    # Transactional event insertion -------------------------------------

    def insert_event(
        self,
        conn: Any,
        eval_set_id: str,
        event_type: str,
        actor: str,
        detail: Dict[str, Any],
        when: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO eval_set_events (id, eval_set_id, event_type, actor, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("eevt"), eval_set_id, event_type, actor, json_dumps(detail), when),
        )
        self.observability.insert_observation(
            conn,
            "log",
            event_type,
            "control_plane",
            "eval",
            "info",
            None,
            "",
            "eval_set",
            eval_set_id,
            {"actor": actor, **detail},
            when,
        )

    # Row hydration -----------------------------------------------------

    def _set_from_row(self, row: Any) -> EvalSet:
        return EvalSet(
            row["id"],
            row["name"],
            row["description"],
            row["scoring"],
            row["baseline_score"],
            row["regression_threshold"],
            json_loads(row["metadata"], {}),
            row["created_by"],
            row["created_at"],
            row["updated_at"],
        )

    def _run_from_row(self, row: Any) -> EvalRun:
        return EvalRun(
            row["id"],
            row["eval_set_id"],
            row["target_kind"],
            row["target_id"],
            row["score"],
            row["baseline_score"],
            row["delta"],
            row["threshold"],
            bool(row["passed"]),
            json_loads(row["detail"], {}),
            row["evidence_id"],
            row["created_by"],
            row["created_at"],
        )
