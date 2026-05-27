"""Rollout orchestration service.

A rollout takes an artifact through a state machine — PLANNED → CANARYING →
PROMOTING → RELEASED — with optional pause/rescue branches. Promotion of a
canary requires a passing health gate and (if configured) a passing eval
run; both checks read inside the same transaction that commits the status
change, so concurrent writers cannot land a failing result between the
gate read and the rollout UPDATE.

Health failures open a rescue task and flip the rollout to RESCUING. The
``_in_flight_rescue_task`` lookup makes the failure path idempotent — a
second failure during rescue records the additional event but does not
spawn another rescue task.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from mac.models import (
    EvalSet,
    EvalTargetKind,
    JsonDict,
    NotFoundError,
    ROLLOUT_ACTIONS,
    Rollout,
    RolloutStatus,
    RolloutStrategy,
    RuntimeEnvironment,
    Task,
    TaskState,
    Tenant,
    TransitionError,
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


class RolloutService:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        *,
        get_tenant: Callable[[str], Tenant],
        get_runtime: Callable[[str], RuntimeEnvironment],
        get_eval_set: Callable[[str], EvalSet],
        create_task: Callable[..., Task],
        add_memory: Callable[..., Any],
        task_from_row: Callable[[Any], Task],
    ) -> None:
        self.store = store
        self.observability = observability
        self._get_tenant = get_tenant
        self._get_runtime = get_runtime
        self._get_eval_set = get_eval_set
        self._create_task = create_task
        self._add_memory = add_memory
        self._task_from_row = task_from_row

    # Rollout lifecycle -------------------------------------------------

    def create_rollout(
        self,
        version: str,
        strategy: str,
        target_percent: int,
        created_by: str,
        tenant_id: Optional[str] = None,
        channel: str = "fleet",
        runtime_environment_id: Optional[str] = None,
        artifact_uri: Optional[str] = None,
        artifact_hash: Optional[str] = None,
        health_policy: Optional[Dict[str, Any]] = None,
        required_eval_set_id: Optional[str] = None,
    ) -> Rollout:
        if not version:
            raise ValidationError("rollout version is required")
        if tenant_id is not None:
            self._get_tenant(tenant_id)
        channel = (channel or "fleet").strip()
        if not channel:
            raise ValidationError("rollout channel is required")
        strategy_value = _state_value(strategy)
        try:
            RolloutStrategy(strategy_value)
        except ValueError:
            raise ValidationError("unsupported rollout strategy: %s" % strategy_value)
        if int(target_percent) < 0 or int(target_percent) > 100:
            raise ValidationError("rollout target percent must be between 0 and 100")
        if runtime_environment_id is not None:
            self._get_runtime(runtime_environment_id)
        if bool(artifact_uri) != bool(artifact_hash):
            raise ValidationError("artifact_uri and artifact_hash must be provided together")
        if artifact_hash is not None:
            self._validate_artifact_hash(artifact_hash)
        if required_eval_set_id is not None:
            self._get_eval_set(required_eval_set_id)
        policy = ensure_json_object(health_policy)
        # mac-jmjc: a rollout without an explicit required_checks list
        # would let the health gate trivially pass on any caller input.
        # Default to ["runtime"] when the caller didn't specify, and
        # reject health_policy that explicitly sets required_checks=[].
        if "required_checks" in policy:
            required_value = policy.get("required_checks") or []
            if not isinstance(required_value, list) or not required_value:
                raise ValidationError(
                    "rollout health_policy.required_checks must be a non-empty list"
                )
        else:
            policy = dict(policy)
            policy["required_checks"] = ["runtime"]
        now = utcnow()
        rollout_id = new_id("rollout")
        self.store.execute(
            """
            INSERT INTO rollouts (
                id, version, strategy, status, target_percent, tenant_id, channel,
                runtime_environment_id, artifact_uri, artifact_hash, health_policy,
                required_eval_set_id, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rollout_id,
                version,
                strategy_value,
                RolloutStatus.PLANNED.value,
                int(target_percent),
                tenant_id,
                channel,
                runtime_environment_id,
                artifact_uri,
                artifact_hash,
                json_dumps(policy),
                required_eval_set_id,
                created_by,
                now,
                now,
            ),
        )
        self._record_event(
            rollout_id,
            "rollout.created",
            created_by,
            {
                "target_percent": int(target_percent),
                "tenant_id": tenant_id,
                "channel": channel,
                "runtime_environment_id": runtime_environment_id,
                "artifact_uri": artifact_uri,
                "artifact_hash": artifact_hash,
            },
        )
        if artifact_uri and artifact_hash:
            self._record_event(
                rollout_id,
                "rollout.artifact_verified",
                created_by,
                {"artifact_uri": artifact_uri, "artifact_hash": artifact_hash},
            )
        return self.get_rollout(rollout_id)

    def get_rollout(self, rollout_id: str) -> Rollout:
        row = self.store.query_one("SELECT * FROM rollouts WHERE id = ?", (rollout_id,))
        if row is None:
            raise NotFoundError("rollout not found: %s" % rollout_id)
        return self._from_row(row)

    def list_rollouts(
        self,
        tenant_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> List[Rollout]:
        clauses = []
        params: List[Any] = []
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        sql = "SELECT * FROM rollouts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        return [self._from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def list_rollout_events(self, rollout_id: str) -> List[JsonDict]:
        self.get_rollout(rollout_id)
        rows = self.store.query_all(
            "SELECT * FROM rollout_events WHERE rollout_id = ? ORDER BY created_at, id",
            (rollout_id,),
        )
        return [
            {
                "id": row["id"],
                "rollout_id": row["rollout_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "detail": json_loads(row["detail"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def verify_rollout_artifact(
        self,
        rollout_id: str,
        artifact_uri: str,
        artifact_hash: str,
        actor: str,
    ) -> Rollout:
        rollout = self.get_rollout(rollout_id)
        if rollout.status not in {RolloutStatus.PLANNED.value, RolloutStatus.PAUSED.value}:
            raise TransitionError("artifact can only be verified before install or while paused")
        if not artifact_uri:
            raise ValidationError("artifact_uri is required")
        self._validate_artifact_hash(artifact_hash)
        artifact_changed = (
            artifact_uri != rollout.artifact_uri
            or artifact_hash != rollout.artifact_hash
        )
        now = utcnow()
        self.store.execute(
            """
            UPDATE rollouts
            SET artifact_uri = ?, artifact_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (artifact_uri, artifact_hash, now, rollout_id),
        )
        self._record_event(
            rollout_id,
            "rollout.artifact_verified",
            actor,
            {"artifact_uri": artifact_uri, "artifact_hash": artifact_hash},
        )
        # mac-vh9h: when the artifact actually changes while PAUSED, the
        # prior health check (and any eval run) was for a different
        # artifact. Invalidate health so resume/promote must re-gate
        # against the new artifact.
        if artifact_changed and rollout.status == RolloutStatus.PAUSED.value:
            self._record_event(
                rollout_id,
                "rollout.health_checked",
                actor,
                {
                    "status": "invalidated",
                    "reason": "artifact_swap_requires_recheck",
                    "previous_artifact_hash": rollout.artifact_hash,
                    "new_artifact_hash": artifact_hash,
                },
            )
        return self.get_rollout(rollout_id)

    def advance_rollout(
        self,
        rollout_id: str,
        action: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Rollout:
        rollout = self.get_rollout(rollout_id)
        detail = detail or {}
        rule = ROLLOUT_ACTIONS.get(action)
        if rule is None:
            raise ValidationError("unsupported rollout action: %s" % action)
        if rollout.status not in rule["from"]:
            raise TransitionError(
                "rollout action %s not allowed from status %s" % (action, rollout.status)
            )
        if action in {"start_canary", "promote"}:
            self._install_ready(rollout)
        if (
            action == "promote"
            and rollout.strategy == RolloutStrategy.CANARY.value
            and rollout.status == RolloutStatus.PLANNED.value
        ):
            raise TransitionError("canary rollout must start canary before promotion")
        if (
            action == "promote"
            and rollout.strategy == RolloutStrategy.CANARY.value
            and rollout.status in {RolloutStatus.CANARYING.value, RolloutStatus.PAUSED.value}
            and not self._latest_health_passed(rollout.id)
        ):
            raise ValidationError("canary promotion requires a passing health gate")
        status = rule["to"]
        if "target_percent" in rule:
            detail.setdefault("target_percent", rule["target_percent"])
        target_percent = int(detail.get("target_percent", rollout.target_percent))
        now = utcnow()

        # The eval gate is read inside the transaction that commits the rollout
        # status change. BEGIN IMMEDIATE blocks concurrent writers (including
        # record_eval_run), so a failing run cannot land between gate-read and
        # commit. The conditional UPDATE on status ensures no other writer
        # advanced the rollout out from under us.
        with self.store.transaction() as conn:
            # mac-wfct: the eval gate must also fire on start_canary so
            # canary traffic is never sent to an artifact whose evals
            # haven't passed. Previously only promote consulted the
            # gate, allowing half-bypass for INSTANT or start_canary.
            eval_gated_actions = {"promote", "start_canary"}
            if action in eval_gated_actions and rollout.required_eval_set_id is not None:
                eval_set_row = conn.execute(
                    "SELECT id FROM eval_sets WHERE id = ?",
                    (rollout.required_eval_set_id,),
                ).fetchone()
                if eval_set_row is None:
                    raise ValidationError(
                        "rollout promote blocked: required eval_set %s no longer exists"
                        % rollout.required_eval_set_id
                    )
                run_row = conn.execute(
                    """
                    SELECT id, score, delta, threshold, passed
                    FROM eval_runs
                    WHERE eval_set_id = ? AND target_kind = ? AND target_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (
                        rollout.required_eval_set_id,
                        EvalTargetKind.ROLLOUT_VERSION.value,
                        rollout.version,
                    ),
                ).fetchone()
                if run_row is None:
                    raise ValidationError(
                        "rollout promote requires an eval_run against %s for version %s"
                        % (rollout.required_eval_set_id, rollout.version)
                    )
                if not bool(run_row["passed"]):
                    raise ValidationError(
                        "rollout %s blocked: latest eval_run %s did not pass (score=%s delta=%s threshold=%s)"
                        % (action, run_row["id"], run_row["score"], run_row["delta"], run_row["threshold"])
                    )
                detail.setdefault("eval_run_id", run_row["id"])
                detail.setdefault("eval_score", run_row["score"])
            cursor = conn.execute(
                """
                UPDATE rollouts
                SET status = ?, target_percent = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (status, target_percent, now, rollout_id, rollout.status),
            )
            if cursor.rowcount != 1:
                raise TransitionError(
                    "rollout %s status changed during advance; retry" % rollout_id
                )
            conn.execute(
                """
                INSERT INTO rollout_events (id, rollout_id, event_type, actor, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (new_id("revt"), rollout_id, "rollout.%s" % action, actor, json_dumps(detail), now),
            )
            self.observability.insert_observation(
                conn,
                "log",
                "rollout.%s" % action,
                "control_plane",
                "rollout",
                "info",
                None,
                "",
                "rollout",
                rollout_id,
                {"actor": actor, **detail},
                now,
            )
        return self.get_rollout(rollout_id)

    def evaluate_rollout_health(
        self,
        rollout_id: str,
        checks: Dict[str, Any],
        actor: str,
    ) -> JsonDict:
        rollout = self.get_rollout(rollout_id)
        checks_obj = ensure_json_object(checks)
        required = self._required_checks(rollout, checks_obj)
        failed = [
            check
            for check in required
            if not self._check_passed(checks_obj.get(check))
        ]
        detail = {
            "checks": checks_obj,
            "required_checks": required,
            "failed_checks": failed,
            "status": "failed" if failed else "healthy",
        }
        self._record_event(rollout_id, "rollout.health_checked", actor, detail)
        if failed:
            # Idempotency: if the rollout is already RESCUING, don't open
            # another rescue task. Record that the additional failure
            # happened and return the in-flight rescue task.
            if rollout.status == RolloutStatus.RESCUING.value:
                self._record_event(
                    rollout_id,
                    "rollout.health_failure_during_rescue",
                    actor,
                    {"failed_checks": failed, "checks": checks_obj},
                )
                in_flight = self._in_flight_rescue_task(rollout_id)
                return {
                    "healthy": False,
                    "failed_checks": failed,
                    "rollout": rollout.to_dict(),
                    "rescue_task": in_flight.to_dict() if in_flight is not None else None,
                }
            rescued, task = self.rescue_rollout(
                rollout_id,
                actor,
                "health gate failed: %s" % ", ".join(failed),
                detail={"failed_checks": failed, "checks": checks_obj},
            )
            return {
                "healthy": False,
                "failed_checks": failed,
                "rollout": rescued.to_dict(),
                "rescue_task": task.to_dict(),
            }
        return {
            "healthy": True,
            "failed_checks": [],
            "rollout": self.get_rollout(rollout_id).to_dict(),
            "rescue_task": None,
        }

    def rescue_rollout(
        self,
        rollout_id: str,
        actor: str,
        reason: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Rollout, Task]:
        rollout = self.get_rollout(rollout_id)
        now = utcnow()
        self.store.execute(
            "UPDATE rollouts SET status = ?, target_percent = ?, updated_at = ? WHERE id = ?",
            (RolloutStatus.RESCUING.value, 0, now, rollout_id),
        )
        rescue_detail = {"reason": reason}
        rescue_detail.update(ensure_json_object(detail))
        self._record_event(rollout_id, "rollout.rescue_started", actor, rescue_detail)
        task = self._create_task(
            "Rescue rollout %s" % rollout.version,
            description=reason,
            project="rollout",
            priority=100,
            required_capabilities=["ops"],
            metadata={
                "rollout_id": rollout_id,
                "rescue": True,
                "tenant_id": rollout.tenant_id,
                "channel": rollout.channel,
                "failed_checks": rescue_detail.get("failed_checks", []),
            },
            actor=actor,
        )
        self._add_memory(
            task.id,
            "rollout",
            rollout_id,
            "rescue",
            "Rescue path opened for rollout %s: %s" % (rollout.version, reason),
            None,
            actor,
        )
        return self.get_rollout(rollout_id), task

    # Internal helpers --------------------------------------------------

    def _record_event(
        self,
        rollout_id: str,
        event_type: str,
        actor: str,
        detail: Dict[str, Any],
    ) -> None:
        when = utcnow()
        self.store.execute(
            """
            INSERT INTO rollout_events (id, rollout_id, event_type, actor, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("revt"), rollout_id, event_type, actor, json_dumps(detail), when),
        )
        self.observability.insert_observation(
            self.store,
            "log",
            event_type,
            "control_plane",
            "rollout",
            "info",
            None,
            "",
            "rollout",
            rollout_id,
            {"actor": actor, **detail},
            when,
        )

    def _install_ready(self, rollout: Rollout) -> None:
        if not rollout.runtime_environment_id:
            raise ValidationError("rollout requires a runtime environment before install")
        self._get_runtime(rollout.runtime_environment_id)
        if not rollout.artifact_uri or not rollout.artifact_hash:
            raise ValidationError("rollout artifact must be verified before install")
        self._validate_artifact_hash(rollout.artifact_hash)

    def _latest_health_passed(self, rollout_id: str) -> bool:
        row = self.store.query_one(
            """
            SELECT detail FROM rollout_events
            WHERE rollout_id = ? AND event_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (rollout_id, "rollout.health_checked"),
        )
        if row is None:
            return False
        detail = json_loads(row["detail"], {})
        return detail.get("status") == "healthy"

    def _required_checks(self, rollout: Rollout, checks: JsonDict) -> List[str]:
        required = rollout.health_policy.get("required_checks")
        if required:
            return [str(check) for check in required]
        # mac-jmjc: without an explicit required_checks list the gate
        # previously degraded to "whatever keys the caller supplied",
        # which makes ``evaluate_rollout_health(rollout_id, {})`` always
        # report healthy. Refuse to evaluate health without policy.
        raise ValidationError(
            "rollout health policy must declare required_checks; refusing to evaluate health for %s"
            % rollout.id
        )

    def _check_passed(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in {"ok", "pass", "passed", "healthy", "success"}
        if isinstance(value, dict):
            return self._check_passed(value.get("status"))
        return False

    def _validate_artifact_hash(self, artifact_hash: str) -> None:
        if not artifact_hash or not artifact_hash.startswith("sha256:"):
            raise ValidationError("artifact_hash must be a sha256:<digest> value")
        digest = artifact_hash.removeprefix("sha256:")
        if len(digest) < 6:
            raise ValidationError("artifact_hash digest is too short")

    def _in_flight_rescue_task(self, rollout_id: str) -> Optional[Task]:
        row = self.store.query_one(
            """
            SELECT * FROM tasks
            WHERE project = 'rollout'
              AND state NOT IN (?, ?, ?)
              AND json_extract(metadata, '$.rollout_id') = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (
                TaskState.COMPLETED.value,
                TaskState.FAILED.value,
                TaskState.CANCELLED.value,
                rollout_id,
            ),
        )
        return self._task_from_row(row) if row is not None else None

    # Row hydration -----------------------------------------------------

    def _from_row(self, row: Any) -> Rollout:
        keys = row.keys() if hasattr(row, "keys") else []
        required_eval_set_id = (
            row["required_eval_set_id"] if "required_eval_set_id" in keys else None
        )
        return Rollout(
            row["id"],
            row["version"],
            row["strategy"],
            row["status"],
            row["target_percent"],
            row["tenant_id"],
            row["channel"],
            row["runtime_environment_id"],
            row["artifact_uri"],
            row["artifact_hash"],
            json_loads(row["health_policy"], {}),
            required_eval_set_id,
            row["created_by"],
            row["created_at"],
            row["updated_at"],
        )
