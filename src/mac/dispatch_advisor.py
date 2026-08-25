"""Deterministic dispatch advice for ready tasks.

The advice in this module is deliberately not authorization.  It orders the
bounded dispatcher candidate window and ranks workers that have already passed
the ordinary hard eligibility checks.  ``ControlPlane.claim_task`` remains the
transactional authority.

Formerly ``mac.work_package_assignment``.  It was named for the work-package
pipeline that has since been removed, but the ranking itself always applied to
every ordinary task -- only the (now deleted) compiled-plan critical-path rank
was package-specific.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from mac.fleet_learning import (
    REPOSITORY_ACCESS_RECORD_TYPE,
    repository_access_state,
    repository_host,
    task_repository_remote,
)
from mac.models import Agent, JsonDict, Task, json_loads


DISPATCH_ASSIGNMENT_ADVISOR_VERSION = "work-package-allocator-v2"
_QUERY_CHUNK_SIZE = 400
_LEARNING_ROWS_PER_AGENT = 50
_LOAD_SCALE = 1_000_000


@dataclass
class DispatchScoreSnapshot:
    """One dispatch-pass snapshot, preventing per-task/per-agent DB reads."""

    active_lease_counts: Dict[str, int]
    learning_records: Dict[str, Tuple[Mapping[str, Any], ...]]
    learning_states: Dict[Tuple[str, str, str], str] = field(default_factory=dict)

    def record_assignment(self, agent_id: str) -> None:
        self.active_lease_counts[agent_id] = int(self.active_lease_counts.get(agent_id, 0)) + 1


@dataclass(frozen=True)
class DispatchAssignmentAdvice:
    """Secret-free, advisory explanation persisted with a package claim."""

    agent: Agent
    score: float
    rationale: str
    decision: JsonDict


class DispatchAdvisor:
    """Build bounded deterministic task and worker ranking snapshots."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def score_snapshot(self, agents: Iterable[Agent]) -> DispatchScoreSnapshot:
        """Read current load and recent learning once for a dispatch pass."""

        agent_ids = sorted({str(agent.id) for agent in agents if str(agent.id)})
        active_counts = {agent_id: 0 for agent_id in agent_ids}
        if agent_ids:
            rows = self.store.query_all(
                "SELECT lease.agent_id, COUNT(*) AS active_count FROM leases AS lease "
                "JOIN tasks AS task ON task.lease_id = lease.id "
                "WHERE lease.status = ? AND task.owner_agent_id = lease.agent_id "
                "GROUP BY lease.agent_id",
                ("active",),
            )
            for row in rows:
                agent_id = str(row["agent_id"] or "")
                if agent_id in active_counts:
                    active_counts[agent_id] = int(row["active_count"])

        learning: Dict[str, List[Mapping[str, Any]]] = {agent_id: [] for agent_id in agent_ids}
        for agent_id_chunk in _chunks(agent_ids, _QUERY_CHUNK_SIZE):
            placeholders = ",".join("?" for _ in agent_id_chunk)
            rows = self.store.query_all(
                "SELECT * FROM ("
                "SELECT memory.id, memory.subject_id, memory.content, "
                "memory.created_at, ROW_NUMBER() OVER ("
                "PARTITION BY memory.subject_id "
                "ORDER BY memory.created_at DESC, memory.id DESC"
                ") AS dispatch_learning_rank "
                "FROM memory_records AS memory "
                "WHERE memory.subject_type = ? AND memory.record_type = ? "
                "AND memory.subject_id IN (%s)"
                ") AS ranked WHERE dispatch_learning_rank <= ? "
                "ORDER BY subject_id, created_at DESC, id DESC" % placeholders,
                (
                    "agent",
                    REPOSITORY_ACCESS_RECORD_TYPE,
                    *agent_id_chunk,
                    _LEARNING_ROWS_PER_AGENT,
                ),
            )
            for row in rows:
                value = dict(row)
                agent_id = str(value.get("subject_id") or "")
                if agent_id in learning:
                    learning[agent_id].append(value)
        return DispatchScoreSnapshot(
            active_lease_counts=active_counts,
            learning_records={key: tuple(value) for key, value in learning.items()},
        )

    def rank_agents(
        self,
        *,
        task: Task,
        eligible_agents: Sequence[Agent],
        snapshot: DispatchScoreSnapshot,
        route: str,
        allow_cooperative_reuse: bool = False,
    ) -> List[DispatchAssignmentAdvice]:
        """Rank already-eligible agents; never turn advice into authority."""

        candidates = []
        required_capabilities = set(task.required_capabilities or [])
        for agent in eligible_agents:
            capacity = _agent_capacity(agent)
            active = max(0, int(snapshot.active_lease_counts.get(agent.id, 0)))
            bounded_active = min(active, capacity)
            headroom_ppm = (capacity - bounded_active) * _LOAD_SCALE // capacity
            capability_surplus = len(set(agent.capabilities or []) - required_capabilities)
            best_fit_ppm = _LOAD_SCALE // (1 + capability_surplus)
            learning_state = self._learning_state(task, agent.id, snapshot)
            learning_tier = {"failure": 0, "unknown": 1, "success": 2}[learning_state]
            idle = agent.status == "idle"
            # Fixed-point components make the float bounded and interpretable.
            # One ppm of load headroom always dominates every softer signal.
            score = float(headroom_ppm + (learning_tier / 10.0) + (best_fit_ppm / 100_000_000.0))
            sort_key = (
                -headroom_ppm,
                -learning_tier,
                -best_fit_ppm,
                0 if idle else 1,
                agent.id,
            )
            candidates.append(
                (
                    sort_key,
                    agent,
                    score,
                    {
                        "active_leases": active,
                        "capacity": capacity,
                        "normalized_load": active / capacity,
                        "headroom_ppm": headroom_ppm,
                        "repository_access_learning": learning_state,
                        "repository_access_operation": "review_clone",
                        "required_capability_count": len(required_capabilities),
                        "capability_surplus": capability_surplus,
                        "best_fit_ppm": best_fit_ppm,
                        "idle": idle,
                    },
                )
            )
        candidates.sort(key=lambda candidate: candidate[0])

        task_order = {
            "source": "ordinary_task_fallback",
            "critical_path_rank": None,
            "order_signal": 0.0,
        }
        result = []
        for selected_rank, (_key, agent, score, components) in enumerate(candidates, start=1):
            rationale = (
                "deterministic advisory: load=%d/%d; repository_access=%s; "
                "capability_surplus=%d; stable_tie_break=agent_id"
                % (
                    components["active_leases"],
                    components["capacity"],
                    components["repository_access_learning"],
                    components["capability_surplus"],
                )
            )
            result.append(
                DispatchAssignmentAdvice(
                    agent=agent,
                    score=score,
                    rationale=rationale,
                    decision={
                        "schema": "mac.dispatch.assignment_advice.v1",
                        "allocator_version": DISPATCH_ASSIGNMENT_ADVISOR_VERSION,
                        "advisory_only": True,
                        "hard_gates_rechecked_in_claim": True,
                        "route": str(route),
                        "worker_identity_fixed": route == "worker_pull",
                        "audit_behavior": ("persist_with_exact_lease_if_claim_succeeds"),
                        "allow_cooperative_reuse": bool(allow_cooperative_reuse),
                        "task_order": task_order,
                        "agent_score": components,
                        "eligible_agent_count": len(candidates),
                        "selected_rank": selected_rank,
                        "tie_breaker": "agent_id",
                    },
                )
            )
        return result

    def _learning_state(
        self,
        task: Task,
        agent_id: str,
        snapshot: DispatchScoreSnapshot,
    ) -> str:
        remote = task_repository_remote(task)
        host = repository_host(remote)
        if not host or host == "local":
            return "unknown"
        project = task.project or "default"
        cache_key = (agent_id, project, host)
        cached = snapshot.learning_states.get(cache_key)
        if cached is not None:
            return cached
        state, _learning = repository_access_state(
            snapshot.learning_records.get(agent_id, ()),
            project=project,
            host=host,
            operation="review_clone",
            failure_cooldown_seconds=_nonnegative_int_env(
                "MAC_REPOSITORY_ACCESS_FAILURE_COOLDOWN_SECONDS", 1800
            ),
            success_ttl_seconds=_nonnegative_int_env(
                "MAC_REPOSITORY_ACCESS_SUCCESS_TTL_SECONDS", 86400
            ),
        )
        snapshot.learning_states[cache_key] = state
        return state


def _agent_capacity(agent: Agent) -> int:
    for key in ("capacity", "max_concurrent_tasks"):
        value = agent.resources.get(key)
        if value is not None:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                return 1
    return 1


def _nonnegative_int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return int(default)


def _chunks(values: Sequence[str], size: int) -> Iterable[Tuple[str, ...]]:
    for offset in range(0, len(values), max(1, int(size))):
        yield tuple(values[offset : offset + size])
