from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from typing import Any, List, Optional

from mac.agentbus_control import (
    REPO_UPDATE_CONTENT_TYPE,
    REPO_UPDATE_RESULT_TOPIC,
    REPO_UPDATE_TOPIC,
    repo_update_payload,
)
from mac.env_config import resolve_hub_agent
from mac.models import (
    AgentStatus,
    JsonDict,
    NotFoundError,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
)


SOURCE_CONVERGENCE_SCHEMA = "mac.source_convergence.v1"
_HOLD_PREFIX = "source_convergence:"
_SUCCESS_RESULTS = {"updated", "no_update", "service_restarted"}
_TERMINAL_FAILURE_RESULTS = {"rolled_back", "skipped"}


def _source_action(metadata: JsonDict) -> tuple[str, str]:
    """Return the strongest safe action explicitly established by release evidence.

    The complete impact planner is a separate workstream.  Until that planner has
    emitted a versioned action, this controller only accepts an explicit action or
    a documentation-only changed-file set.  Guessing that an unknown change is a
    process restart would turn the reconciler into an unsafe ``git pull`` loop.
    """
    explicit = str(metadata.get("convergence_action") or "").strip().lower()
    aliases = {
        "source": "source_restart",
        "restart": "source_restart",
        "source_restart": "source_restart",
        "docs": "source_sync",
        "source_sync": "source_sync",
        "full_redeploy": "full_redeploy_required",
        "full_redeploy_required": "full_redeploy_required",
        "operator_direction_required": "operator_direction_required",
    }
    if explicit in aliases:
        return aliases[explicit], "release_metadata"

    changed = metadata.get("changed_files")
    if isinstance(changed, list) and changed:
        paths = [str(item).strip() for item in changed if str(item).strip()]
        if paths and all(
            path.startswith(("docs/", ".github/"))
            or path.lower().endswith((".md", ".rst", ".txt"))
            for path in paths
        ):
            return "source_sync", "documentation_only"
    return "operator_direction_required", "impact_plan_missing"


class SourceConvergenceService:
    """Durable hub-owned reconciler for fleet desired source generations."""

    def __init__(self, control_plane: Any) -> None:
        self.cp = control_plane
        self.store = control_plane.store
        self.owner_id = new_id("source-controller")

    def tick(self, *, limit: int = 100) -> JsonDict:
        limit_value = max(1, min(int(limit), 1000))
        summary: JsonDict = {
            "schema": SOURCE_CONVERGENCE_SCHEMA,
            "desired_states": 0,
            "nodes_examined": 0,
            "held": 0,
            "dispatched": 0,
            "converged": 0,
            "blocked": 0,
            "waiting": 0,
            "errors": [],
        }
        states = self.store.query_all(
            """
            SELECT d.*, r.commit_sha, r.canonical_ref, r.metadata AS release_metadata,
                   r.status AS release_status
            FROM fleet_desired_source_states d
            JOIN source_releases r ON r.id = d.release_id
            WHERE d.fleet_id IS NOT NULL AND d.paused = 0
            ORDER BY d.updated_at, d.id
            LIMIT ?
            """,
            (limit_value,),
        )
        summary["desired_states"] = len(states)
        self._consume_results()
        remaining = limit_value
        for state in states:
            if remaining <= 0:
                break
            fleet_id = str(state["fleet_id"])
            if not self._acquire_lease("fleet:%s" % fleet_id):
                summary["waiting"] += 1
                continue
            try:
                used = self._reconcile_fleet(state, remaining, summary)
                remaining -= used
            except Exception as exc:  # noqa: BLE001 - one fleet cannot stop all convergence.
                summary["errors"].append(
                    {"fleet_id": fleet_id, "error": str(exc)[:500]}
                )
        summary["errors"] = summary["errors"][:20]
        return summary

    def status(self, *, fleet_id: Optional[str] = None, limit: int = 250) -> JsonDict:
        clauses = " WHERE n.fleet_id = ?" if fleet_id else ""
        params: List[Any] = [fleet_id] if fleet_id else []
        params.append(max(1, min(int(limit), 1000)))
        rows = self.store.query_all(
            """
            SELECT n.*, a.name AS agent_name, a.status AS agent_status,
                   a.dispatch_hold, a.dispatch_hold_reason
            FROM source_convergence_nodes n
            JOIN agents a ON a.id = n.agent_id
            %s
            ORDER BY n.fleet_id, a.name, n.agent_id
            LIMIT ?
            """
            % clauses,
            tuple(params),
        )
        return {
            "schema": SOURCE_CONVERGENCE_SCHEMA,
            "nodes": [self._node_dict(row) for row in rows],
        }

    def _reconcile_fleet(self, state: Any, limit: int, summary: JsonDict) -> int:
        fleet_id = str(state["fleet_id"])
        agents = self.store.query_all(
            """
            SELECT a.* FROM agents a
            JOIN fleet_agents fa ON fa.agent_id = a.id
            WHERE fa.fleet_id = ?
            ORDER BY a.name, a.id LIMIT ?
            """,
            (fleet_id, limit),
        )
        metadata = json_loads(state["release_metadata"], {})
        metadata = metadata if isinstance(metadata, dict) else {}
        action, plan_reason = _source_action(metadata)
        desired_sha = str(state["commit_sha"])
        generation = int(state["generation"])
        for row in agents:
            summary["nodes_examined"] += 1
            agent_id = str(row["id"])
            resources = json_loads(row["resources"], {})
            source_state = (
                resources.get("source_state") if isinstance(resources, dict) else {}
            )
            source_state = source_state if isinstance(source_state, dict) else {}
            actual_sha = str(source_state.get("commit_sha") or "")
            dirty = bool(source_state.get("dirty"))
            existing = self.store.query_one(
                "SELECT * FROM source_convergence_nodes WHERE fleet_id = ? AND agent_id = ?",
                (fleet_id, agent_id),
            )

            if actual_sha == desired_sha and not dirty:
                self._write_node(
                    state,
                    row,
                    action,
                    plan_reason,
                    "converged",
                    actual_sha,
                    request_id=None,
                    stream_id=None,
                    blocker_code=None,
                    blocker_detail=None,
                    attempt=0,
                )
                self._clear_owned_hold(row)
                summary["converged"] += 1
                continue

            hold_reason = str(row["dispatch_hold_reason"] or "")
            if bool(row["dispatch_hold"]) and not hold_reason.startswith(_HOLD_PREFIX):
                self._write_node(
                    state,
                    row,
                    action,
                    plan_reason,
                    "blocked",
                    actual_sha,
                    request_id=None,
                    stream_id=None,
                    blocker_code="external_dispatch_hold",
                    blocker_detail=hold_reason
                    or "agent has an operator-owned dispatch hold",
                    attempt=int(existing["attempt"] or 0) if existing else 0,
                )
                summary["blocked"] += 1
                continue

            self.cp.set_agent_dispatch_hold(
                agent_id,
                "%sgeneration=%d:desired=%s"
                % (_HOLD_PREFIX, generation, desired_sha[:12]),
            )
            summary["held"] += 1
            if dirty:
                self._write_node(
                    state,
                    row,
                    action,
                    plan_reason,
                    "blocked",
                    actual_sha,
                    request_id=None,
                    stream_id=None,
                    blocker_code="dirty_checkout",
                    blocker_detail="worker checkout has local modifications",
                    attempt=int(existing["attempt"] or 0) if existing else 0,
                )
                summary["blocked"] += 1
                continue
            if str(state["release_status"]) != "published":
                self._write_node(
                    state,
                    row,
                    action,
                    plan_reason,
                    "blocked",
                    actual_sha,
                    request_id=None,
                    stream_id=None,
                    blocker_code="release_not_published",
                    blocker_detail="desired release is not in published state",
                    attempt=int(existing["attempt"] or 0) if existing else 0,
                )
                summary["blocked"] += 1
                continue
            if action not in {"source_restart", "source_sync"}:
                self._write_node(
                    state,
                    row,
                    action,
                    plan_reason,
                    "blocked",
                    actual_sha,
                    request_id=None,
                    stream_id=None,
                    blocker_code=action,
                    blocker_detail=(
                        "a deterministic impact plan is required before mutation"
                        if action == "operator_direction_required"
                        else "release requires the full self-redeploy action"
                    ),
                    attempt=int(existing["attempt"] or 0) if existing else 0,
                )
                summary["blocked"] += 1
                continue
            if (
                str(row["current_task_id"] or "")
                or str(row["status"]) == AgentStatus.BUSY.value
            ):
                self._write_node(
                    state,
                    row,
                    action,
                    plan_reason,
                    "waiting_busy",
                    actual_sha,
                    request_id=None,
                    stream_id=None,
                    blocker_code=None,
                    blocker_detail="active task must finish before source mutation",
                    attempt=int(existing["attempt"] or 0) if existing else 0,
                )
                summary["waiting"] += 1
                continue
            if str(row["status"]) == AgentStatus.OFFLINE.value:
                self._write_node(
                    state,
                    row,
                    action,
                    plan_reason,
                    "waiting_offline",
                    actual_sha,
                    request_id=None,
                    stream_id=None,
                    blocker_code=None,
                    blocker_detail="agent is offline",
                    attempt=int(existing["attempt"] or 0) if existing else 0,
                )
                summary["waiting"] += 1
                continue
            if existing and self._retry_not_due(existing, generation, desired_sha):
                summary["waiting"] += 1
                continue
            if (
                existing
                and int(existing["desired_generation"]) == generation
                and str(existing["desired_sha"]) == desired_sha
                and int(existing["attempt"] or 0) >= 5
            ):
                self._write_node(
                    state,
                    row,
                    action,
                    plan_reason,
                    "blocked",
                    actual_sha,
                    request_id=None,
                    stream_id=None,
                    blocker_code="retry_exhausted",
                    blocker_detail="five exact-release apply attempts were exhausted",
                    attempt=int(existing["attempt"] or 0),
                )
                summary["blocked"] += 1
                continue

            sender_id = self._sender_agent_id()
            if not sender_id:
                self._write_node(
                    state,
                    row,
                    action,
                    plan_reason,
                    "blocked",
                    actual_sha,
                    request_id=None,
                    stream_id=None,
                    blocker_code="controller_sender_unavailable",
                    blocker_detail="configure a registered MAC_REVIEW_TICK_HUB_AGENT",
                    attempt=int(existing["attempt"] or 0) if existing else 0,
                )
                summary["blocked"] += 1
                continue
            recovering_dispatch = bool(
                existing
                and str(existing["phase"]) == "dispatching"
                and int(existing["desired_generation"]) == generation
                and str(existing["desired_sha"]) == desired_sha
                and str(existing["request_id"] or "")
            )
            attempt = (
                int(existing["attempt"] or 1)
                if recovering_dispatch
                else int(existing["attempt"] or 0) + 1
                if existing
                else 1
            )
            request_id = (
                str(existing["request_id"])
                if recovering_dispatch
                else "source-convergence:%s:%d:%s:%d"
                % (fleet_id, generation, agent_id, attempt)
            )
            payload = repo_update_payload(
                remote=str(metadata.get("remote") or "origin"),
                branch=str(metadata.get("branch") or "main"),
                restart=action == "source_restart",
                request_id=request_id,
                target_sha=desired_sha,
                desired_generation=generation,
                release_id=str(state["release_id"]),
            )
            # Commit intent before touching AgentBus. The deterministic stream
            # ID lets a replacement hub resume rather than duplicate mutation.
            self._write_node(
                state,
                row,
                action,
                plan_reason,
                "dispatching",
                actual_sha,
                request_id=request_id,
                stream_id=None,
                blocker_code=None,
                blocker_detail=None,
                attempt=attempt,
            )
            try:
                stream_id = self._publish_exact_source_intent(
                    sender_id=sender_id,
                    recipient_id=agent_id,
                    request_id=request_id,
                    payload=payload,
                )
            except Exception as exc:  # noqa: BLE001 - durable state makes this retryable.
                summary["errors"].append(
                    {
                        "fleet_id": fleet_id,
                        "agent_id": agent_id,
                        "error": str(exc)[:500],
                    }
                )
                summary["waiting"] += 1
                continue
            self.store.execute(
                """
                UPDATE source_convergence_nodes
                SET phase = 'dispatched', stream_id = ?, updated_at = ?
                WHERE fleet_id = ? AND agent_id = ? AND request_id = ?
                  AND phase = 'dispatching'
                """,
                (stream_id, utcnow(), fleet_id, agent_id, request_id),
            )
            summary["dispatched"] += 1
        return len(agents)

    def _publish_exact_source_intent(
        self,
        *,
        sender_id: str,
        recipient_id: str,
        request_id: str,
        payload: JsonDict,
    ) -> str:
        stream_id = (
            "srcconv_%s" % hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:40]
        )
        try:
            stream = self.cp.get_agentbus_stream(stream_id)
        except NotFoundError:
            stream = self.cp.open_agentbus_stream(
                sender_agent_id=sender_id,
                recipient_agent_id=recipient_id,
                content_type=REPO_UPDATE_CONTENT_TYPE,
                topic=REPO_UPDATE_TOPIC,
                stream_id=stream_id,
            )
        if (
            stream.sender_agent_id != sender_id
            or stream.recipient_agent_id != recipient_id
        ):
            raise RuntimeError(
                "deterministic source-convergence stream identity collision"
            )
        chunks = self.cp.read_agentbus_chunks(sender_id, stream_id, 0, 10)
        if not chunks:
            self.cp.append_agentbus_chunk(
                stream_id,
                sender_id,
                payload=payload,
                content_type=REPO_UPDATE_CONTENT_TYPE,
                payload_encoding="json",
                final=True,
            )
        return stream_id

    def _consume_results(self) -> None:
        rows = self.store.query_all(
            """
            SELECT c.payload FROM agentbus_chunks c
            JOIN agentbus_streams s ON s.id = c.stream_id
            WHERE s.topic = ?
            ORDER BY c.created_at DESC LIMIT 500
            """,
            (REPO_UPDATE_RESULT_TOPIC,),
        )
        for row in rows:
            payload = json_loads(row["payload"], {})
            if not isinstance(payload, dict):
                continue
            request_id = str(payload.get("request_id") or "")
            if not request_id:
                continue
            node = self.store.query_one(
                "SELECT * FROM source_convergence_nodes WHERE request_id = ?",
                (request_id,),
            )
            if node is None or str(node["phase"]) not in {
                "dispatching",
                "dispatched",
                "awaiting_attestation",
            }:
                continue
            status = str(payload.get("status") or "")
            after_sha = str(payload.get("after_sha") or "")
            now = utcnow()
            if status in _SUCCESS_RESULTS and after_sha == str(node["desired_sha"]):
                self.store.execute(
                    """
                    UPDATE source_convergence_nodes
                    SET phase = 'awaiting_attestation', actual_sha = ?, result = ?,
                        blocker_code = NULL, blocker_detail = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (after_sha, json_dumps(payload), now, node["id"]),
                )
            elif status in _TERMINAL_FAILURE_RESULTS or status == "error":
                next_retry = (
                    parse_time(now)
                    + timedelta(
                        seconds=min(
                            900, 30 * (2 ** min(5, int(node["attempt"] or 1) - 1))
                        )
                    )
                ).isoformat()
                self.store.execute(
                    """
                    UPDATE source_convergence_nodes
                    SET phase = 'retry_wait', result = ?, blocker_code = ?,
                        blocker_detail = ?, next_retry_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dumps(payload),
                        "repo_update_%s" % (status or "failed"),
                        str(payload.get("summary") or "repo update failed")[:1000],
                        next_retry,
                        now,
                        node["id"],
                    ),
                )

    def _write_node(
        self,
        state: Any,
        agent: Any,
        action: str,
        plan_reason: str,
        phase: str,
        actual_sha: str,
        *,
        request_id: Optional[str],
        stream_id: Optional[str],
        blocker_code: Optional[str],
        blocker_detail: Optional[str],
        attempt: int,
    ) -> None:
        now = utcnow()
        plan_digest = hashlib.sha256(
            json_dumps(
                {
                    "schema": SOURCE_CONVERGENCE_SCHEMA,
                    "generation": int(state["generation"]),
                    "release_id": str(state["release_id"]),
                    "desired_sha": str(state["commit_sha"]),
                    "action": action,
                    "reason": plan_reason,
                }
            ).encode("utf-8")
        ).hexdigest()
        next_retry = (
            (
                parse_time(now)
                + timedelta(seconds=min(900, 30 * (2 ** min(5, max(0, attempt - 1)))))
            ).isoformat()
            if phase in {"dispatching", "dispatched"}
            else None
        )
        self.store.execute(
            """
            INSERT INTO source_convergence_nodes (
                id, desired_source_state_id, fleet_id, agent_id, desired_generation,
                release_id, desired_sha, actual_sha, action, plan_digest, phase,
                attempt, request_id, stream_id, next_retry_at, blocker_code,
                blocker_detail, result, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
            ON CONFLICT(fleet_id, agent_id) DO UPDATE SET
                desired_source_state_id = excluded.desired_source_state_id,
                desired_generation = excluded.desired_generation,
                release_id = excluded.release_id,
                desired_sha = excluded.desired_sha,
                actual_sha = excluded.actual_sha,
                action = excluded.action,
                plan_digest = excluded.plan_digest,
                phase = excluded.phase,
                attempt = excluded.attempt,
                request_id = excluded.request_id,
                stream_id = excluded.stream_id,
                next_retry_at = excluded.next_retry_at,
                blocker_code = excluded.blocker_code,
                blocker_detail = excluded.blocker_detail,
                updated_at = excluded.updated_at
            """,
            (
                new_id("srcnode"),
                state["id"],
                state["fleet_id"],
                agent["id"],
                int(state["generation"]),
                state["release_id"],
                state["commit_sha"],
                actual_sha or None,
                action,
                plan_digest,
                phase,
                attempt,
                request_id,
                stream_id,
                next_retry,
                blocker_code,
                blocker_detail,
                now,
                now,
            ),
        )

    def _retry_not_due(self, row: Any, generation: int, desired_sha: str) -> bool:
        if (
            int(row["desired_generation"]) != generation
            or str(row["desired_sha"]) != desired_sha
        ):
            return False
        if str(row["phase"]) in {
            "awaiting_attestation",
            "dispatching",
            "dispatched",
        }:
            retry_at = str(row["next_retry_at"] or "")
            return bool(retry_at and parse_time(retry_at) > parse_time(utcnow()))
        if str(row["phase"]) == "retry_wait":
            retry_at = str(row["next_retry_at"] or "")
            return bool(retry_at and parse_time(retry_at) > parse_time(utcnow()))
        return False

    def _sender_agent_id(self) -> str:
        configured = (
            resolve_hub_agent("MAC_REVIEW_TICK_HUB_AGENT")
            or os.environ.get("MAC_AGENT_ID", "").strip()
        )
        if not configured:
            return ""
        row = self.store.query_one(
            "SELECT id FROM agents WHERE id = ? OR name = ? ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END LIMIT 1",
            (configured, configured, configured),
        )
        return str(row["id"]) if row else ""

    def _clear_owned_hold(self, row: Any) -> None:
        if bool(row["dispatch_hold"]) and str(
            row["dispatch_hold_reason"] or ""
        ).startswith(_HOLD_PREFIX):
            self.cp.clear_agent_dispatch_hold(str(row["id"]))

    def _acquire_lease(self, scope_key: str, seconds: int = 30) -> bool:
        now = utcnow()
        expires = (parse_time(now) + timedelta(seconds=max(5, seconds))).isoformat()
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO source_convergence_controller_leases (
                    scope_key, owner_id, expires_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                WHERE source_convergence_controller_leases.expires_at <= ?
                   OR source_convergence_controller_leases.owner_id = ?
                """,
                (scope_key, self.owner_id, expires, now, now, self.owner_id),
            )
            row = conn.execute(
                "SELECT owner_id FROM source_convergence_controller_leases WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
        return bool(row and str(row["owner_id"]) == self.owner_id)

    @staticmethod
    def _node_dict(row: Any) -> JsonDict:
        keys = row.keys() if hasattr(row, "keys") else []
        result = {key: row[key] for key in keys}
        result["attempt"] = int(result.get("attempt") or 0)
        result["desired_generation"] = int(result.get("desired_generation") or 0)
        result["dispatch_hold"] = bool(result.get("dispatch_hold"))
        result["result"] = json_loads(result.get("result"), {})
        return result
