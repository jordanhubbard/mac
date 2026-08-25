"""Autonomous, evidence-gated optimization of MAC execution policies.

This module closes the control loop between task outcomes and future task
configuration without allowing learned policy to weaken MAC's safety gates.
Policies may tune only an explicit allowlist of task metadata consumed by the
worker.  Authorization, sandboxing, verification, review, publication and
deployment rules remain outside this service.

The task ledger remains the outcome authority.  Experiments and assignments
are durable rows; observations are replayable projections of task/review/router
evidence.  A treatment can be promoted automatically only when its primary KPI
is statistically better and every quality guardrail is non-inferior.
"""

from __future__ import annotations

import copy
import hashlib
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from mac.models import (
    NotFoundError,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)


POLICY_SCHEMA = "mac.scientific_policy.v1"
EXPERIMENT_SCHEMA = "mac.scientific_experiment.v1"
ASSIGNMENT_SCHEMA = "mac.scientific_assignment.v1"
OBSERVATION_SCHEMA = "mac.scientific_observation.v1"
DECISION_SCHEMA = "mac.scientific_decision.v1"
SERVICE_SCHEMA = "mac.scientific_optimizer_service.v1"
BASELINE_CACHE_SECONDS = 900.0

POLICY_STATUSES = frozenset({"candidate", "active", "retired"})
EXPERIMENT_STATES = frozenset(
    {
        "draft",
        "running",
        "candidate",
        "monitoring",
        "paused",
        "completed",
        "rejected",
        "rolled_back",
    }
)
TERMINAL_EXPERIMENT_STATES = frozenset({"completed", "rejected", "rolled_back"})
TASK_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})

# The optimizer can tune these worker-consumed task metadata fields and nothing
# else.  In particular there is intentionally no knob for required checks,
# sandbox policy, review requirement, publication, signatures, or deployment.
POLICY_PARAMETER_TYPES: Dict[str, str] = {
    "model": "model",
    "review_model": "model",
    "model_strength": "strength",
    "review_model_strength": "strength",
    "max_iterations": "iterations",
    "review_max_iterations": "iterations",
    "plan_first": "bool",
    "review_mode": "review_mode",
}

METRIC_DIRECTIONS: Dict[str, str] = {
    "accepted_success": "maximize",
    "delayed_quality_success": "maximize",
    "cycles_to_accept": "minimize",
    "executor_attempts": "minimize",
    "review_attempts": "minimize",
    "lead_time_ms": "minimize",
    "model_latency_ms": "minimize",
    "input_tokens": "minimize",
    "output_tokens": "minimize",
    "total_tokens": "minimize",
    "cost_usd": "minimize",
    "escaped_defect_severity": "minimize",
}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bounded_float(value: Any, name: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("%s must be numeric" % name) from exc
    if not math.isfinite(number) or number < low or number > high:
        raise ValidationError("%s must be between %s and %s" % (name, low, high))
    return number


def _bounded_int(value: Any, name: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("%s must be an integer" % name) from exc
    if number < low or number > high:
        raise ValidationError("%s must be between %d and %d" % (name, low, high))
    return number


def validate_policy_parameters(parameters: Any) -> Dict[str, Any]:
    if not isinstance(parameters, Mapping):
        raise ValidationError("scientific policy parameters must be an object")
    unknown = sorted(set(parameters) - set(POLICY_PARAMETER_TYPES))
    if unknown:
        raise ValidationError(
            "scientific policy cannot change non-allowlisted field(s): %s" % ", ".join(unknown)
        )
    normalized: Dict[str, Any] = {}
    for key, value in parameters.items():
        kind = POLICY_PARAMETER_TYPES[key]
        if kind == "model":
            text = str(value or "").strip()
            if not text or len(text) > 256:
                raise ValidationError("%s must be a non-empty model id up to 256 characters" % key)
            normalized[key] = text
        elif kind == "strength":
            normalized[key] = _bounded_int(value, key, 1, 10)
        elif kind == "iterations":
            normalized[key] = _bounded_int(value, key, 1, 500)
        elif kind == "bool":
            if not isinstance(value, bool):
                raise ValidationError("%s must be boolean" % key)
            normalized[key] = value
        elif kind == "review_mode":
            mode = str(value or "").strip().lower()
            if mode not in {"standard", "blind"}:
                raise ValidationError("review_mode must be standard or blind")
            normalized[key] = mode
    return normalized


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _elapsed_ms(start: Any, end: Any) -> float:
    a = _parse_time(start)
    b = _parse_time(end)
    if a is None or b is None or b < a:
        return 0.0
    return (b - a).total_seconds() * 1000.0


def _numeric(mapping: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        value: Any = mapping
        for part in key.split("."):
            value = value.get(part) if isinstance(value, Mapping) else None
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return 0.0


def _route_detail(item: Any) -> Dict[str, Any]:
    value = dict(item) if isinstance(item, Mapping) else {}
    detail = value.get("detail")
    return dict(detail) if isinstance(detail, Mapping) else value


def _catalog_prices(model_id: str, provider_hint: str = "") -> Optional[Tuple[float, float, float]]:
    """Return input/output/cache-read prices in USD per million tokens."""
    try:
        from mac import models_catalog
    except Exception:
        return None
    mid = str(model_id or "").strip()
    if not mid:
        return None
    segments = [part for part in mid.split("/") if part]
    candidates: List[Tuple[str, str]] = []
    if provider_hint:
        candidates.append((provider_hint, mid))
        if segments:
            candidates.append((provider_hint, segments[-1]))
    for index in range(max(0, len(segments) - 1)):
        candidates.append((segments[index], "/".join(segments[index + 1 :])))
        candidates.append((segments[index], segments[-1]))
    bare = segments[-1] if segments else mid
    for provider in (
        "anthropic",
        "openai",
        "google",
        "xai",
        "deepseek",
        "meta",
        "mistral",
        "qwen",
        "nvidia",
    ):
        candidates.append((provider, bare))
    seen: set[Tuple[str, str]] = set()
    for provider, model in candidates:
        key = (str(provider).strip(), str(model).strip())
        if not all(key) or key in seen:
            continue
        seen.add(key)
        try:
            info = models_catalog.get_model_info(*key)
        except Exception:
            info = None
        has_cost_data = getattr(info, "has_cost_data", None) if info is not None else None
        if info is not None and callable(has_cost_data) and bool(has_cost_data()):
            return (
                float(getattr(info, "cost_input", 0.0) or 0.0),
                float(getattr(info, "cost_output", 0.0) or 0.0),
                float(getattr(info, "cost_cache_read", 0.0) or 0.0),
            )
    return None


def estimate_route_cost(detail: Mapping[str, Any]) -> Tuple[float, bool]:
    explicit = _numeric(detail, "cost_usd", "usage.cost_usd")
    if explicit > 0:
        return explicit, True
    usage = detail.get("usage") if isinstance(detail.get("usage"), Mapping) else {}
    input_tokens = _numeric(detail, "input_tokens", "usage.input_tokens", "usage.prompt_tokens")
    output_tokens = _numeric(
        detail, "output_tokens", "usage.output_tokens", "usage.completion_tokens"
    )
    cached_tokens = _numeric(
        usage,
        "cached_tokens",
        "prompt_tokens_details.cached_tokens",
        "input_tokens_details.cached_tokens",
    )
    prices = _catalog_prices(
        str(detail.get("response_model") or detail.get("resolved_model") or ""),
        str(detail.get("provider") or ""),
    )
    if prices is None or (input_tokens <= 0 and output_tokens <= 0):
        return 0.0, False
    input_price, output_price, cache_price = prices
    uncached = max(0.0, input_tokens - cached_tokens)
    cost = (
        uncached * input_price
        + cached_tokens * (cache_price if cache_price > 0 else input_price)
        + output_tokens * output_price
    ) / 1_000_000.0
    return cost, True


def derive_task_kpis(
    task_detail: Mapping[str, Any],
    llm_routes: Iterable[Mapping[str, Any]] = (),
    *,
    outcome_horizon_seconds: float = 86400.0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Project one task into canonical quality, cycle, time and cost KPIs."""
    task = task_detail.get("task") if isinstance(task_detail.get("task"), Mapping) else {}
    metadata = task.get("metadata") if isinstance(task.get("metadata"), Mapping) else {}
    state = str(task.get("state") or "")
    reviews = [item for item in task_detail.get("reviews", []) if isinstance(item, Mapping)]
    publications = [
        item for item in task_detail.get("publications", []) if isinstance(item, Mapping)
    ]
    routes: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in llm_routes:
        record = dict(raw) if isinstance(raw, Mapping) else {}
        identity = str(record.get("id") or "")
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        detail = _route_detail(record)
        if str(detail.get("schema") or "") == "mac.llm_route.v1":
            routes.append(detail)

    input_tokens = sum(
        _numeric(item, "input_tokens", "usage.input_tokens", "usage.prompt_tokens")
        for item in routes
    )
    output_tokens = sum(
        _numeric(item, "output_tokens", "usage.output_tokens", "usage.completion_tokens")
        for item in routes
    )
    cached_tokens = sum(
        _numeric(
            item.get("usage") if isinstance(item.get("usage"), Mapping) else {},
            "cached_tokens",
            "prompt_tokens_details.cached_tokens",
            "input_tokens_details.cached_tokens",
        )
        for item in routes
    )
    cost_usd = 0.0
    priced_routes = 0
    for item in routes:
        cost, known = estimate_route_cost(item)
        cost_usd += cost
        priced_routes += int(known)

    outcomes = [
        item
        for item in metadata.get("review_outcomes", [])
        if isinstance(item, Mapping) and str(item.get("status") or "") == "confirmed"
    ]
    escaped = [item for item in outcomes if str(item.get("kind") or "") == "escaped_defect"]
    clean = [item for item in outcomes if str(item.get("kind") or "") == "clean_window"]
    escaped_severity = sum(float(item.get("severity_weight") or 0.0) for item in escaped)
    terminal = state in TASK_TERMINAL_STATES
    accepted = state == "completed"
    completed_at = _parse_time(task.get("completed_at"))
    clock = now or datetime.now(timezone.utc)
    horizon_elapsed = bool(
        completed_at is not None
        and (clock - completed_at).total_seconds() >= max(0.0, outcome_horizon_seconds)
    )
    quality_validated = bool(
        escaped or clean or (accepted and horizon_elapsed) or state in {"failed", "cancelled"}
    )
    delayed_success = 1.0 if accepted and not escaped and (bool(clean) or horizon_elapsed) else 0.0
    quality_source = (
        "escaped_defect"
        if escaped
        else "operator_clean_window"
        if clean
        else "terminal_clean_window"
        if accepted and horizon_elapsed
        else "terminal_failure"
        if state in {"failed", "cancelled"}
        else "pending"
    )
    executor_attempts = int(task.get("attempt_count") or 0)
    review_attempts = len(reviews)
    rejected_reviews = sum(1 for item in reviews if str(item.get("status") or "") == "rejected")
    lead_time_ms = _elapsed_ms(task.get("created_at"), task.get("completed_at"))
    return {
        "schema": "mac.task_kpis.v1",
        "task_id": str(task.get("id") or ""),
        "project": str(task.get("project") or ""),
        "state": state,
        "terminal": terminal,
        "accepted_success": 1.0 if accepted else 0.0,
        "delayed_quality_success": delayed_success,
        "quality_validated": quality_validated,
        "quality_source": quality_source,
        "executor_attempts": float(executor_attempts),
        "review_attempts": float(review_attempts),
        "rejected_reviews": float(rejected_reviews),
        "cycles_to_accept": float(executor_attempts + rejected_reviews),
        "lead_time_ms": lead_time_ms,
        "model_latency_ms": sum(_numeric(item, "duration_ms") for item in routes),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": round(cost_usd, 8),
        "cost_known": bool(routes) and priced_routes == len(routes),
        "priced_routes": priced_routes,
        "route_count": len(routes),
        "escaped_defect_severity": escaped_severity,
        "publication_count": len(publications),
        "observed_at": utcnow(),
    }


def _stable_point(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _bootstrap_mean_difference(
    control: Sequence[float],
    treatment: Sequence[float],
    *,
    seed: str,
    iterations: int = 2000,
    alpha: float = 0.05,
) -> Dict[str, float]:
    """Deterministic two-sample bootstrap CI for treatment minus control."""
    c = [float(value) for value in control]
    t = [float(value) for value in treatment]
    if not c or not t:
        return {"difference": 0.0, "ci_lower": float("-inf"), "ci_upper": float("inf")}
    # Keep enough resamples to represent the requested tail probability.  The
    # cap bounds scheduler work for very conservative sequential corrections.
    iterations = max(int(iterations), min(100_000, int(math.ceil(20.0 / alpha))))
    state = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big") or 1

    def rand_index(size: int) -> int:
        nonlocal state
        state = (6364136223846793005 * state + 1442695040888963407) & ((1 << 64) - 1)
        return state % size

    diffs: List[float] = []
    for _ in range(max(200, int(iterations))):
        c_mean = sum(c[rand_index(len(c))] for _ in c) / len(c)
        t_mean = sum(t[rand_index(len(t))] for _ in t) / len(t)
        diffs.append(t_mean - c_mean)
    diffs.sort()
    lower_index = max(0, min(len(diffs) - 1, int((alpha / 2.0) * len(diffs))))
    upper_index = max(0, min(len(diffs) - 1, int((1.0 - alpha / 2.0) * len(diffs)) - 1))
    return {
        "difference": (sum(t) / len(t)) - (sum(c) / len(c)),
        "ci_lower": diffs[lower_index],
        "ci_upper": diffs[upper_index],
    }


@dataclass(frozen=True)
class ScientificOptimizerConfig:
    enabled: bool = False
    interval_seconds: float = 300.0
    initial_delay_seconds: float = 60.0
    auto_propose: bool = True
    auto_promote: bool = True
    auto_improve: bool = True
    min_baseline_tasks: int = 10
    default_min_samples_per_arm: int = 8
    default_max_samples_per_arm: int = 40
    default_exploration_fraction: float = 0.2
    outcome_horizon_seconds: float = 86400.0
    improvement_cooldown_seconds: float = 604800.0

    @property
    def active(self) -> bool:
        return self.enabled

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "active": self.active}

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "ScientificOptimizerConfig":
        env = os.environ if environ is None else environ
        return cls(
            enabled=_truthy(env.get("MAC_SCIENTIFIC_OPTIMIZER_ENABLED")),
            interval_seconds=_bounded_float(
                env.get("MAC_SCIENTIFIC_OPTIMIZER_INTERVAL_SECONDS") or 300,
                "MAC_SCIENTIFIC_OPTIMIZER_INTERVAL_SECONDS",
                10,
                86400,
            ),
            initial_delay_seconds=_bounded_float(
                env.get("MAC_SCIENTIFIC_OPTIMIZER_INITIAL_DELAY_SECONDS") or 60,
                "MAC_SCIENTIFIC_OPTIMIZER_INITIAL_DELAY_SECONDS",
                0,
                86400,
            ),
            auto_propose=str(env.get("MAC_SCIENTIFIC_OPTIMIZER_AUTO_PROPOSE") or "1").lower()
            not in {"0", "false", "no", "off"},
            auto_promote=str(env.get("MAC_SCIENTIFIC_OPTIMIZER_AUTO_PROMOTE") or "1").lower()
            not in {"0", "false", "no", "off"},
            auto_improve=str(env.get("MAC_SCIENTIFIC_OPTIMIZER_AUTO_IMPROVE") or "1").lower()
            not in {"0", "false", "no", "off"},
            min_baseline_tasks=_bounded_int(
                env.get("MAC_SCIENTIFIC_OPTIMIZER_MIN_BASELINE_TASKS") or 10,
                "MAC_SCIENTIFIC_OPTIMIZER_MIN_BASELINE_TASKS",
                2,
                10000,
            ),
            default_min_samples_per_arm=_bounded_int(
                env.get("MAC_SCIENTIFIC_OPTIMIZER_MIN_SAMPLES_PER_ARM") or 8,
                "MAC_SCIENTIFIC_OPTIMIZER_MIN_SAMPLES_PER_ARM",
                2,
                10000,
            ),
            default_max_samples_per_arm=_bounded_int(
                env.get("MAC_SCIENTIFIC_OPTIMIZER_MAX_SAMPLES_PER_ARM") or 40,
                "MAC_SCIENTIFIC_OPTIMIZER_MAX_SAMPLES_PER_ARM",
                2,
                100000,
            ),
            default_exploration_fraction=_bounded_float(
                env.get("MAC_SCIENTIFIC_OPTIMIZER_EXPLORATION_FRACTION") or 0.2,
                "MAC_SCIENTIFIC_OPTIMIZER_EXPLORATION_FRACTION",
                0.01,
                1.0,
            ),
            outcome_horizon_seconds=_bounded_float(
                env.get("MAC_SCIENTIFIC_OPTIMIZER_OUTCOME_HORIZON_SECONDS") or 86400,
                "MAC_SCIENTIFIC_OPTIMIZER_OUTCOME_HORIZON_SECONDS",
                0,
                365 * 86400,
            ),
            improvement_cooldown_seconds=_bounded_float(
                env.get("MAC_SCIENTIFIC_OPTIMIZER_IMPROVEMENT_COOLDOWN_SECONDS") or 604800,
                "MAC_SCIENTIFIC_OPTIMIZER_IMPROVEMENT_COOLDOWN_SECONDS",
                3600,
                365 * 86400,
            ),
        )


class ScientificOptimizerService:
    def __init__(
        self,
        store: Any,
        observability: Any,
        *,
        get_task: Callable[[str], Any],
        task_detail: Callable[[str], Mapping[str, Any]],
        list_observability: Callable[..., Iterable[Any]],
        create_task: Optional[Callable[..., Any]] = None,
        config: Optional[ScientificOptimizerConfig] = None,
    ) -> None:
        self.store = store
        self.observability = observability
        self._get_task = get_task
        self._task_detail = task_detail
        self._list_observability = list_observability
        self._create_task = create_task
        self.config = config or ScientificOptimizerConfig.from_env()
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_report: Optional[Dict[str, Any]] = None
        self._owner_id = new_id("optimizer")
        self._baseline_cache: Dict[
            str, Tuple[Tuple[Tuple[str, str], ...], float, List[Dict[str, Any]]]
        ] = {}

    # Policies ---------------------------------------------------------

    def create_policy(
        self,
        name: str,
        project: str,
        parameters: Mapping[str, Any],
        *,
        description: str = "",
        created_by: str = "human",
    ) -> Dict[str, Any]:
        policy_name = str(name or "").strip()
        project_name = str(project or "").strip()
        if not policy_name or not project_name:
            raise ValidationError("policy name and project are required")
        normalized = validate_policy_parameters(parameters)
        now = utcnow()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS version FROM scientific_policies WHERE project = ? AND name = ?",
                (project_name, policy_name),
            ).fetchone()
            version = int((row["version"] if row is not None else 0) or 0) + 1
            policy_id = new_id("policy")
            conn.execute(
                """
                INSERT INTO scientific_policies (
                    id, schema_version, project, name, version, description, status,
                    parameters, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?)
                """,
                (
                    policy_id,
                    POLICY_SCHEMA,
                    project_name,
                    policy_name,
                    version,
                    str(description or "")[:2000],
                    json_dumps(normalized),
                    str(created_by or "human"),
                    now,
                    now,
                ),
            )
            self._insert_event(
                conn,
                "policy",
                policy_id,
                "policy.created",
                created_by,
                {"project": project_name, "version": version},
                now,
            )
        return self.get_policy(policy_id)

    def get_policy(self, policy_id: str) -> Dict[str, Any]:
        row = self.store.query_one("SELECT * FROM scientific_policies WHERE id = ?", (policy_id,))
        if row is None:
            raise NotFoundError("scientific policy not found: %s" % policy_id)
        return self._policy_from_row(row)

    def list_policies(
        self, project: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if status:
            if status not in POLICY_STATUSES:
                raise ValidationError("unsupported policy status: %s" % status)
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM scientific_policies"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY project, name, version"
        return [self._policy_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def active_policy(self, project: str) -> Optional[Dict[str, Any]]:
        row = self.store.query_one(
            "SELECT * FROM scientific_policies WHERE project = ? AND status = 'active' ORDER BY updated_at DESC LIMIT 1",
            (project,),
        )
        return self._policy_from_row(row) if row is not None else None

    def ensure_baseline_policy(
        self, project: str, actor: str = "scientific-optimizer"
    ) -> Dict[str, Any]:
        active = self.active_policy(project)
        if active is not None:
            return active
        baseline = self.create_policy(
            "baseline",
            project,
            {},
            description="Observed fleet defaults before scientific optimization",
            created_by=actor,
        )
        return self.promote_policy(baseline["id"], actor=actor, reason="bootstrap baseline")

    def promote_policy(
        self, policy_id: str, *, actor: str = "operator", reason: str = ""
    ) -> Dict[str, Any]:
        policy = self.get_policy(policy_id)
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE scientific_policies SET status = 'retired', updated_at = ? WHERE project = ? AND status = 'active' AND id != ?",
                (now, policy["project"], policy_id),
            )
            conn.execute(
                "UPDATE scientific_policies SET status = 'active', updated_at = ? WHERE id = ?",
                (now, policy_id),
            )
            self._insert_event(
                conn,
                "policy",
                policy_id,
                "policy.promoted",
                actor,
                {"project": policy["project"], "reason": str(reason or "")[:1000]},
                now,
            )
        self._observe(
            "optimizer.policy.promoted",
            "info",
            policy_id,
            {"project": policy["project"], "reason": reason},
        )
        return self.get_policy(policy_id)

    def rollback_policy(
        self, project: str, policy_id: str, *, actor: str = "operator", reason: str = ""
    ) -> Dict[str, Any]:
        policy = self.get_policy(policy_id)
        if policy["project"] != project:
            raise ValidationError("rollback policy belongs to a different project")
        return self.promote_policy(
            policy_id,
            actor=actor,
            reason="rollback: %s" % (reason or "operator request"),
        )

    # Experiments ------------------------------------------------------

    def create_experiment(
        self,
        name: str,
        project: str,
        hypothesis: str,
        control_policy_id: str,
        treatment_policy_id: str,
        *,
        primary_metric: str,
        direction: Optional[str] = None,
        min_effect: float = 0.0,
        quality_margin: float = 0.05,
        min_samples_per_arm: Optional[int] = None,
        max_samples_per_arm: Optional[int] = None,
        exploration_fraction: Optional[float] = None,
        outcome_horizon_seconds: Optional[float] = None,
        guardrails: Optional[Mapping[str, Any]] = None,
        auto_promote: Optional[bool] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        created_by: str = "human",
    ) -> Dict[str, Any]:
        experiment_name = str(name or "").strip()
        project_name = str(project or "").strip()
        hypothesis_text = str(hypothesis or "").strip()
        if not experiment_name or not project_name or not hypothesis_text:
            raise ValidationError("experiment name, project, and hypothesis are required")
        if primary_metric not in METRIC_DIRECTIONS:
            raise ValidationError("unsupported primary metric: %s" % primary_metric)
        metric_direction = str(direction or METRIC_DIRECTIONS[primary_metric]).strip().lower()
        if metric_direction not in {"maximize", "minimize"}:
            raise ValidationError("direction must be maximize or minimize")
        control = self.get_policy(control_policy_id)
        treatment = self.get_policy(treatment_policy_id)
        if control_policy_id == treatment_policy_id:
            raise ValidationError("control and treatment policies must differ")
        if control["project"] != project_name or treatment["project"] != project_name:
            raise ValidationError("experiment policies must belong to the experiment project")
        minimum = _bounded_int(
            self.config.default_min_samples_per_arm
            if min_samples_per_arm is None
            else min_samples_per_arm,
            "min_samples_per_arm",
            2,
            10000,
        )
        maximum = _bounded_int(
            self.config.default_max_samples_per_arm
            if max_samples_per_arm is None
            else max_samples_per_arm,
            "max_samples_per_arm",
            minimum,
            100000,
        )
        fraction = _bounded_float(
            self.config.default_exploration_fraction
            if exploration_fraction is None
            else exploration_fraction,
            "exploration_fraction",
            0.01,
            1.0,
        )
        horizon = _bounded_float(
            self.config.outcome_horizon_seconds
            if outcome_horizon_seconds is None
            else outcome_horizon_seconds,
            "outcome_horizon_seconds",
            0,
            365 * 86400,
        )
        normalized_guardrails = self._normalize_guardrails(guardrails, quality_margin)
        now = utcnow()
        experiment_id = new_id("experiment")
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO scientific_experiments (
                    id, schema_version, project, name, hypothesis, state, running_slot,
                    control_policy_id, treatment_policy_id, primary_metric, direction,
                    min_effect, quality_margin, min_samples_per_arm, max_samples_per_arm,
                    exploration_fraction, outcome_horizon_seconds, guardrails,
                    auto_promote, metadata, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'draft', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    EXPERIMENT_SCHEMA,
                    project_name,
                    experiment_name,
                    hypothesis_text[:4000],
                    control_policy_id,
                    treatment_policy_id,
                    primary_metric,
                    metric_direction,
                    _bounded_float(min_effect, "min_effect", 0.0, 1_000_000_000.0),
                    _bounded_float(quality_margin, "quality_margin", 0.0, 1.0),
                    minimum,
                    maximum,
                    fraction,
                    horizon,
                    json_dumps(normalized_guardrails),
                    1
                    if (self.config.auto_promote if auto_promote is None else bool(auto_promote))
                    else 0,
                    json_dumps(ensure_json_object(metadata)),
                    str(created_by or "human"),
                    now,
                    now,
                ),
            )
            self._insert_event(
                conn,
                "experiment",
                experiment_id,
                "experiment.created",
                created_by,
                {"project": project_name},
                now,
            )
        return self.get_experiment(experiment_id)

    def get_experiment(self, experiment_id: str) -> Dict[str, Any]:
        row = self.store.query_one(
            "SELECT * FROM scientific_experiments WHERE id = ?", (experiment_id,)
        )
        if row is None:
            raise NotFoundError("scientific experiment not found: %s" % experiment_id)
        return self._experiment_from_row(row)

    def list_experiments(
        self, project: Optional[str] = None, state: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if state:
            if state not in EXPERIMENT_STATES:
                raise ValidationError("unsupported experiment state: %s" % state)
            clauses.append("state = ?")
            params.append(state)
        sql = "SELECT * FROM scientific_experiments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        return [self._experiment_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def start_experiment(self, experiment_id: str, *, actor: str = "operator") -> Dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        if experiment["state"] not in {"draft", "paused"}:
            raise ValidationError("experiment can only start from draft or paused")
        active = self.active_policy(experiment["project"])
        if active is None:
            active = self.promote_policy(
                experiment["control_policy_id"],
                actor=actor,
                reason="experiment control baseline",
            )
        if active["id"] != experiment["control_policy_id"]:
            raise ValidationError("experiment control policy must be the project's active policy")
        now = utcnow()
        try:
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE scientific_experiments SET state = 'running', running_slot = ?, updated_at = ? WHERE id = ?",
                    (experiment["project"], now, experiment_id),
                )
                self._insert_event(
                    conn,
                    "experiment",
                    experiment_id,
                    "experiment.started",
                    actor,
                    {},
                    now,
                )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper() or "duplicate" in str(exc).lower():
                raise ValidationError(
                    "another scientific experiment is already active for project %s"
                    % experiment["project"]
                ) from exc
            raise
        return self.get_experiment(experiment_id)

    def pause_experiment(
        self, experiment_id: str, *, actor: str = "operator", reason: str = ""
    ) -> Dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        if experiment["state"] not in {"running", "monitoring", "candidate"}:
            raise ValidationError("only active experiments can be paused")
        self._set_experiment_state(experiment_id, "paused", actor, reason, release_slot=True)
        return self.get_experiment(experiment_id)

    def promote_experiment(
        self,
        experiment_id: str,
        *,
        actor: str = "operator",
        reason: str = "",
    ) -> Dict[str, Any]:
        """Promote a treatment only after a recorded superiority decision.

        This is the human gate used when ``auto_promote`` is disabled.  It is
        deliberately not an override: an operator may trigger another analysis,
        but cannot bypass the experiment's evidence and guardrail contract.
        """
        experiment = self.get_experiment(experiment_id)
        if experiment["state"] == "running":
            self.refresh_experiment(experiment_id)
            decision = self.analyze_experiment(experiment_id, actor=actor)
        elif experiment["state"] == "candidate":
            row = self.store.query_one(
                "SELECT decision FROM scientific_decisions "
                "WHERE experiment_id = ? AND status = 'promote' "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (experiment_id,),
            )
            decision = json_loads(row["decision"], {}) if row is not None else {}
        else:
            raise ValidationError("experiment can only be promoted from running or candidate")
        if decision.get("status") != "promote":
            raise ValidationError(
                "experiment has no evidence-backed promote decision: %s"
                % (decision.get("reason") or "minimum evidence not reached")
            )
        self._promote_experiment(
            experiment,
            decision,
            actor=actor,
            reason=reason or str(decision.get("reason") or "evidence gate passed"),
        )
        return self.get_experiment(experiment_id)

    # Assignment -------------------------------------------------------

    def prepare_task_assignment(
        self,
        task_id: str,
        project: Optional[str],
        metadata: Mapping[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """Return policy-stamped metadata and an assignment to persist atomically."""
        project_name = str(project or "").strip()
        original = copy.deepcopy(dict(metadata))
        if not project_name or bool(original.get("optimizer_exempt")):
            return original, None
        active = self.active_policy(project_name)
        experiment_row = self.store.query_one(
            "SELECT * FROM scientific_experiments WHERE project = ? AND state IN ('running', 'monitoring') ORDER BY created_at LIMIT 1",
            (project_name,),
        )
        experiment = (
            self._experiment_from_row(experiment_row) if experiment_row is not None else None
        )
        if experiment is None:
            return self._apply_policy(original, active, None), None
        origin = original.get("origin") if isinstance(original.get("origin"), Mapping) else {}
        if str(origin.get("type") or "") in {
            "scientific_optimizer",
            "backlog_grooming",
        }:
            return self._apply_policy(original, active, None), None
        execution = (
            original.get("execution_contract")
            if isinstance(original.get("execution_contract"), Mapping)
            else {}
        )
        if str(execution.get("type") or "") != "repository":
            return self._apply_policy(original, active, None), None

        control = self.get_policy(experiment["control_policy_id"])
        treatment = self.get_policy(experiment["treatment_policy_id"])
        changed_keys = {
            key
            for key in set(control["parameters"]) | set(treatment["parameters"])
            if control["parameters"].get(key) != treatment["parameters"].get(key)
        }
        if any(key in original for key in changed_keys):
            return self._apply_policy(original, active, None), None

        phase = "monitor" if experiment["state"] == "monitoring" else "experiment"
        sample_probability = (
            1.0 if phase == "monitor" else float(experiment["exploration_fraction"])
        )
        if _stable_point(experiment["id"], task_id, "sample", phase) >= sample_probability:
            return self._apply_policy(original, active, None), None
        treatment_probability = 0.9 if phase == "monitor" else 0.5
        treatment_selected = (
            _stable_point(experiment["id"], task_id, "arm", phase) < treatment_probability
        )
        arm = "treatment" if treatment_selected else "control"
        policy = treatment if treatment_selected else control
        propensity = sample_probability * (
            treatment_probability if treatment_selected else 1.0 - treatment_probability
        )
        assignment = {
            "schema": ASSIGNMENT_SCHEMA,
            "experiment_id": experiment["id"],
            "task_id": task_id,
            "arm": arm,
            "policy_id": policy["id"],
            "phase": phase,
            "propensity": propensity,
            "stratum": self._task_stratum(original),
            "assigned_at": utcnow(),
        }
        return self._apply_policy(
            original, policy, assignment, hypothesis=experiment["hypothesis"]
        ), assignment

    def insert_assignment(self, conn: Any, assignment: Mapping[str, Any]) -> None:
        value = dict(assignment)
        conn.execute(
            """
            INSERT INTO scientific_assignments (
                experiment_id, task_id, arm, policy_id, phase, propensity,
                stratum, assignment, assigned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO NOTHING
            """,
            (
                value["experiment_id"],
                value["task_id"],
                value["arm"],
                value["policy_id"],
                value["phase"],
                float(value["propensity"]),
                value.get("stratum") or "",
                json_dumps(value),
                value["assigned_at"],
            ),
        )
        self._insert_event(
            conn,
            "experiment",
            value["experiment_id"],
            "experiment.task_assigned",
            "scientific-optimizer",
            {
                "task_id": value["task_id"],
                "arm": value["arm"],
                "phase": value["phase"],
                "propensity": value["propensity"],
            },
            value["assigned_at"],
        )

    # Observation and decisions --------------------------------------

    def observe_task(self, experiment_id: str, task_id: str) -> Dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        assignment_row = self.store.query_one(
            "SELECT * FROM scientific_assignments WHERE experiment_id = ? AND task_id = ?",
            (experiment_id, task_id),
        )
        if assignment_row is None:
            raise NotFoundError("task is not assigned to experiment: %s" % task_id)
        detail = self._task_detail(task_id)
        routes = self._llm_routes_for_task(task_id, detail)
        metrics = derive_task_kpis(
            detail,
            routes,
            outcome_horizon_seconds=float(experiment["outcome_horizon_seconds"]),
        )
        assignment = self._assignment_from_row(assignment_row)
        observation = {
            "schema": OBSERVATION_SCHEMA,
            "experiment_id": experiment_id,
            "task_id": task_id,
            "arm": assignment["arm"],
            "phase": assignment["phase"],
            "policy_id": assignment["policy_id"],
            "propensity": assignment["propensity"],
            "stratum": assignment["stratum"],
            "terminal": bool(metrics["terminal"]),
            "quality_validated": bool(metrics["quality_validated"]),
            "metrics": metrics,
            "observed_at": utcnow(),
        }
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO scientific_observations (
                    experiment_id, task_id, arm, phase, terminal, quality_validated,
                    metrics, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id, task_id) DO UPDATE SET
                    arm = excluded.arm, phase = excluded.phase,
                    terminal = excluded.terminal,
                    quality_validated = excluded.quality_validated,
                    metrics = excluded.metrics, observed_at = excluded.observed_at
                """,
                (
                    experiment_id,
                    task_id,
                    assignment["arm"],
                    assignment["phase"],
                    1 if metrics["terminal"] else 0,
                    1 if metrics["quality_validated"] else 0,
                    json_dumps(metrics),
                    observation["observed_at"],
                ),
            )
        return observation

    def refresh_experiment(self, experiment_id: str) -> List[Dict[str, Any]]:
        rows = self.store.query_all(
            "SELECT task_id FROM scientific_assignments WHERE experiment_id = ? ORDER BY assigned_at, task_id",
            (experiment_id,),
        )
        observations: List[Dict[str, Any]] = []
        for row in rows:
            try:
                observations.append(self.observe_task(experiment_id, str(row["task_id"])))
            except Exception as exc:
                self._observe(
                    "optimizer.observation.failed",
                    "warning",
                    experiment_id,
                    {"task_id": str(row["task_id"]), "error": str(exc)[:500]},
                )
        return observations

    def analyze_experiment(
        self, experiment_id: str, *, actor: str = "scientific-optimizer"
    ) -> Dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        rows = self.store.query_all(
            "SELECT * FROM scientific_observations WHERE experiment_id = ? ORDER BY observed_at, task_id",
            (experiment_id,),
        )
        observations = [self._observation_from_row(row) for row in rows]
        phase = "monitor" if experiment["state"] == "monitoring" else "experiment"
        metric = experiment["primary_metric"]
        validated = [
            item
            for item in observations
            if item["phase"] == phase and item["terminal"] and item["quality_validated"]
        ]
        # Unknown model prices are missing data, not free inference.  A cost
        # experiment therefore uses only complete-cost observations in either
        # arm and reports both the validated and metric-eligible sample counts.
        usable = [
            item
            for item in validated
            if metric != "cost_usd" or bool(item["metrics"].get("cost_known"))
        ]
        by_arm = {
            arm: [item for item in usable if item["arm"] == arm] for arm in ("control", "treatment")
        }
        minimum = int(experiment["min_samples_per_arm"])
        maximum = int(experiment["max_samples_per_arm"])
        counts = {arm: len(items) for arm, items in by_arm.items()}
        decision: Dict[str, Any] = {
            "schema": DECISION_SCHEMA,
            "experiment_id": experiment_id,
            "state": experiment["state"],
            "phase": phase,
            "sample_counts": counts,
            "validated_sample_counts": {
                arm: sum(1 for item in validated if item["arm"] == arm)
                for arm in ("control", "treatment")
            },
            "primary_metric": metric,
            "status": "collecting",
            "reason": "minimum validated terminal samples not reached",
            "generated_at": utcnow(),
        }
        if min(counts.values()) >= minimum:
            # The service may examine results after every new sample.  Use a
            # Bonferroni alpha-spending bound over every planned interim look
            # and every tested endpoint, preventing repeated peeking from
            # silently inflating the false-positive rate.
            planned_looks = max(1, maximum - minimum + 1)
            family_size = max(1, 1 + len(experiment["guardrails"]))
            alpha = 0.05 / float(planned_looks * family_size)
            comparison = self._compare_metric(
                experiment_id,
                metric,
                by_arm,
                alpha=alpha,
            )
            guardrail_results = [
                self._compare_guardrail(
                    experiment_id,
                    name,
                    spec,
                    by_arm,
                    alpha=alpha,
                )
                for name, spec in experiment["guardrails"].items()
            ]
            guardrails_pass = all(item["noninferior"] for item in guardrail_results)
            direction = experiment["direction"]
            min_effect = float(experiment["min_effect"])
            primary_better = (
                comparison["ci_lower"] > min_effect
                if direction == "maximize"
                else comparison["ci_upper"] < -min_effect
            )
            primary_regressed = (
                comparison["ci_upper"] < -min_effect
                if direction == "maximize"
                else comparison["ci_lower"] > min_effect
            )
            decision.update(
                {
                    "comparison": comparison,
                    "guardrails": guardrail_results,
                    "guardrails_pass": guardrails_pass,
                    "primary_better": primary_better,
                    "primary_regressed": primary_regressed,
                    "inference": {
                        "method": "deterministic_two_sample_bootstrap",
                        "familywise_alpha": 0.05,
                        "per_test_alpha": alpha,
                        "planned_interim_looks": planned_looks,
                        "tested_endpoints": family_size,
                    },
                }
            )
            if not guardrails_pass:
                decision.update(
                    status="rollback" if phase == "monitor" else "reject",
                    reason="quality guardrail failed",
                )
            elif phase == "monitor" and primary_regressed:
                decision.update(
                    status="rollback",
                    reason="the promoted treatment materially regressed its primary KPI",
                )
            elif primary_better:
                decision.update(
                    status="retain" if phase == "monitor" else "promote",
                    reason="treatment is statistically better and guardrails are non-inferior",
                )
            elif min(counts.values()) >= maximum:
                decision.update(
                    status="retain" if phase == "monitor" else "reject",
                    reason="maximum sample budget reached without superiority",
                )
            else:
                decision.update(status="collecting", reason="effect remains inconclusive")
        self._record_decision(experiment, decision, actor)
        return decision

    def tick(self, *, trigger: str = "scheduled") -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"schema": SERVICE_SCHEMA, "status": "busy", "trigger": trigger}
        results: List[Dict[str, Any]] = []
        proposals: List[Dict[str, Any]] = []
        try:
            if not self._claim_tick_lease():
                return {
                    "schema": SERVICE_SCHEMA,
                    "status": "busy",
                    "trigger": trigger,
                    "reason": "another hub replica owns the optimizer lease",
                }
            for experiment in self.list_experiments():
                if experiment["state"] not in {"running", "monitoring"}:
                    continue
                self.refresh_experiment(experiment["id"])
                decision = self.analyze_experiment(experiment["id"])
                results.append(decision)
                if decision["status"] == "promote" and experiment["auto_promote"]:
                    self._promote_experiment(
                        experiment,
                        decision,
                        actor="scientific-optimizer",
                        reason=str(decision.get("reason") or "evidence gate passed"),
                    )
                elif decision["status"] == "reject":
                    self._set_experiment_state(
                        experiment["id"],
                        "rejected",
                        "scientific-optimizer",
                        decision["reason"],
                        release_slot=True,
                    )
                elif decision["status"] == "rollback":
                    self.rollback_policy(
                        experiment["project"],
                        experiment["control_policy_id"],
                        actor="scientific-optimizer",
                        reason=decision["reason"],
                    )
                    self._set_experiment_state(
                        experiment["id"],
                        "rolled_back",
                        "scientific-optimizer",
                        decision["reason"],
                        release_slot=True,
                    )
                elif decision["status"] == "retain" and experiment["state"] == "monitoring":
                    self._set_experiment_state(
                        experiment["id"],
                        "completed",
                        "scientific-optimizer",
                        decision["reason"],
                        release_slot=True,
                    )
            if self.config.auto_propose:
                projects = [
                    str(row["project"])
                    for row in self.store.query_all(
                        "SELECT DISTINCT project FROM tasks WHERE project IS NOT NULL AND project != '' ORDER BY project"
                    )
                ]
                for project in projects:
                    baseline: Optional[List[Dict[str, Any]]] = None
                    if not self._project_has_active_experiment(project):
                        baseline = self._project_baseline(project)
                    proposal = self.propose_next_experiment(project, baseline=baseline)
                    if proposal is not None:
                        proposals.append(proposal)
                    elif self.config.auto_improve:
                        improvement = self.propose_improvement_task(project, baseline=baseline)
                        if improvement is not None:
                            proposals.append(improvement)
            report = {
                "schema": SERVICE_SCHEMA,
                "status": "ok",
                "trigger": trigger,
                "decisions": results,
                "proposals": proposals,
                "generated_at": utcnow(),
            }
            with self._state_lock:
                self._last_report = copy.deepcopy(report)
            self._observe(
                "optimizer.tick",
                "info",
                "scientific-optimizer",
                {
                    "decisions": len(results),
                    "proposals": len(proposals),
                    "trigger": trigger,
                },
            )
            return report
        finally:
            self._run_lock.release()

    def _project_has_active_experiment(self, project: str) -> bool:
        return (
            self.store.query_one(
                "SELECT id FROM scientific_experiments WHERE project = ? AND state IN ('running', 'candidate', 'monitoring') LIMIT 1",
                (project,),
            )
            is not None
        )

    def propose_next_experiment(
        self,
        project: str,
        *,
        baseline: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        if self._project_has_active_experiment(project):
            return None
        policy = self.ensure_baseline_policy(project)
        if baseline is None:
            baseline = self._project_baseline(project)
        if len(baseline) < self.config.min_baseline_tasks:
            return None
        params = dict(policy["parameters"])
        treatment_params = dict(params)
        primary_metric = "cycles_to_accept"
        hypothesis = ""
        costs_known = any(item["cost_known"] and item["cost_usd"] > 0 for item in baseline)
        if float(params.get("model_strength") or 0) > 1 and costs_known:
            treatment_params["model_strength"] = int(params["model_strength"]) - 1
            primary_metric = "cost_usd"
            hypothesis = "One lower model-strength rung is quality-noninferior while reducing accepted-task cost."
        elif "model_strength" not in params and costs_known and self._strength_ladder_ready():
            # The control remains the observed fleet default.  Pinning rung 9
            # creates the first falsifiable step down without pretending we
            # know that the default is a particular named model.
            treatment_params["model_strength"] = 9
            primary_metric = "cost_usd"
            hypothesis = "Strength rung 9 is quality-noninferior to the observed fleet default while reducing accepted-task cost."
        elif int(params.get("review_max_iterations") or 0) > 4:
            treatment_params["review_max_iterations"] = max(
                4, int(params["review_max_iterations"]) - 2
            )
            primary_metric = "total_tokens"
            hypothesis = "A smaller reviewer turn budget is quality-noninferior while reducing tokens per accepted task."
        elif sum(item["cycles_to_accept"] for item in baseline) / len(baseline) > 1.5 and not bool(
            params.get("plan_first")
        ):
            treatment_params["plan_first"] = True
            primary_metric = "cycles_to_accept"
            hypothesis = "Planning repository tasks before execution is quality-noninferior while reducing rework cycles."
        else:
            return None
        candidate = self.create_policy(
            "auto-%s" % primary_metric.replace("_", "-"),
            project,
            treatment_params,
            description=hypothesis,
            created_by="scientific-optimizer",
        )
        experiment = self.create_experiment(
            "auto-%s-%s"
            % (
                primary_metric.replace("_", "-"),
                datetime.now(timezone.utc).strftime("%Y%m%d"),
            ),
            project,
            hypothesis,
            policy["id"],
            candidate["id"],
            primary_metric=primary_metric,
            direction="minimize",
            quality_margin=0.05,
            guardrails={"accepted_success": {"direction": "maximize", "margin": 0.05}},
            auto_promote=self.config.auto_promote,
            metadata={
                "source": "autonomous_hypothesis",
                "baseline_task_count": len(baseline),
                "baseline_task_ids": [item["task_id"] for item in baseline],
            },
            created_by="scientific-optimizer",
        )
        return self.start_experiment(experiment["id"], actor="scientific-optimizer")

    def propose_improvement_task(
        self,
        project: str,
        *,
        baseline: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """File normal reviewed work when measured friction needs a code change."""
        if self._create_task is None:
            return None
        if self._project_has_active_experiment(project):
            return None
        if baseline is None:
            baseline = self._project_baseline(project)
        if len(baseline) < self.config.min_baseline_tasks:
            return None
        means = {
            metric: sum(float(item.get(metric) or 0.0) for item in baseline) / len(baseline)
            for metric in (
                "cycles_to_accept",
                "executor_attempts",
                "review_attempts",
                "lead_time_ms",
                "total_tokens",
                "cost_usd",
            )
        }
        if means["cycles_to_accept"] <= 1.5:
            return None
        hypothesis_key = "reduce-unexplained-rework"
        title = "Scientific optimizer: reduce unexplained rework cycles"
        open_task = self.store.query_one(
            "SELECT id FROM tasks WHERE project = ? AND title = ? "
            "AND state NOT IN ('completed', 'failed', 'cancelled') LIMIT 1",
            (project, title),
        )
        if open_task is not None or self._improvement_in_cooldown(project, hypothesis_key):
            return None
        baseline_ids = [str(item.get("task_id") or "") for item in baseline]
        description = (
            "The autonomous scientific optimizer found %.3f mean rework cycles "
            "across %d recent, terminal repository tasks in project %s. Existing "
            "allowlisted execution-policy treatments are exhausted or already "
            "active. Identify the dominant causal mechanism, add durable "
            "instrumentation if attribution is incomplete, and implement one "
            "bounded candidate treatment. Pre-register its expected direction, "
            "sample budget, and quality guardrails through the scientific "
            "optimizer; do not weaken sandbox, tests, CodeGraph, review, "
            "signature, publication, or deployment gates.\n\nBaseline means:\n%s"
            % (
                means["cycles_to_accept"],
                len(baseline),
                project,
                json_dumps(means),
            )
        )
        task = self._create_task(
            title,
            description=description,
            project=project,
            priority=1,
            metadata={
                "optimizer_exempt": True,
                "origin": {
                    "type": "scientific_optimizer",
                    "schema": "mac.scientific_improvement_hypothesis.v1",
                    "hypothesis_key": hypothesis_key,
                },
                "scientific_hypothesis": {
                    "metric": "cycles_to_accept",
                    "direction": "minimize",
                    "baseline_means": means,
                    "baseline_task_ids": baseline_ids,
                    "required_result": "pre_registered_experiment",
                },
            },
            actor="scientific-optimizer",
        )
        task_dict = task.to_dict() if hasattr(task, "to_dict") else dict(task)
        now = utcnow()
        with self.store.transaction() as conn:
            self._insert_event(
                conn,
                "project",
                project,
                "improvement_task.created",
                "scientific-optimizer",
                {
                    "task_id": task_dict.get("id"),
                    "hypothesis_key": hypothesis_key,
                    "baseline_task_count": len(baseline),
                },
                now,
            )
        return {
            "schema": "mac.scientific_improvement_hypothesis.v1",
            "status": "task_created",
            "project": project,
            "hypothesis_key": hypothesis_key,
            "task": task_dict,
        }

    def experiment_evidence(
        self,
        experiment_id: str,
        *,
        limit: int = 500,
    ) -> Dict[str, Any]:
        """Return the durable, replayable protocol record for one experiment."""
        experiment = self.get_experiment(experiment_id)
        bounded = max(1, min(int(limit), 5000))
        assignments = [
            self._assignment_from_row(row)
            for row in self.store.query_all(
                "SELECT * FROM scientific_assignments WHERE experiment_id = ? "
                "ORDER BY assigned_at, task_id LIMIT ?",
                (experiment_id, bounded),
            )
        ]
        observations = [
            self._observation_from_row(row)
            for row in self.store.query_all(
                "SELECT * FROM scientific_observations WHERE experiment_id = ? "
                "ORDER BY observed_at, task_id LIMIT ?",
                (experiment_id, bounded),
            )
        ]
        decisions = [
            {
                "id": row["id"],
                "status": row["status"],
                "decision": json_loads(row["decision"], {}),
                "actor": row["actor"],
                "created_at": row["created_at"],
            }
            for row in self.store.query_all(
                "SELECT * FROM scientific_decisions WHERE experiment_id = ? "
                "ORDER BY created_at, id LIMIT ?",
                (experiment_id, bounded),
            )
        ]
        events = [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "detail": json_loads(row["detail"], {}),
                "created_at": row["created_at"],
            }
            for row in self.store.query_all(
                "SELECT * FROM scientific_optimizer_events "
                "WHERE subject_type = 'experiment' AND subject_id = ? "
                "ORDER BY created_at, id LIMIT ?",
                (experiment_id, bounded),
            )
        ]
        return {
            "schema": "mac.scientific_experiment_evidence.v1",
            "experiment": experiment,
            "assignments": assignments,
            "observations": observations,
            "decisions": decisions,
            "events": events,
            "truncated": any(
                len(items) >= bounded for items in (assignments, observations, decisions, events)
            ),
        }

    # Runtime lifecycle ------------------------------------------------

    def start(self) -> bool:
        if not self.config.active:
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="mac-scientific-optimizer", daemon=True
            )
            self._thread.start()
        self._observe(
            "optimizer.started",
            "info",
            "scientific-optimizer",
            {"config": self.config.to_dict()},
        )
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        return thread is None or not thread.is_alive()

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            last = copy.deepcopy(self._last_report)
        lease = self.store.query_one(
            "SELECT * FROM scientific_optimizer_locks WHERE name = ?",
            ("scheduler",),
        )
        return {
            "schema": SERVICE_SCHEMA,
            "config": self.config.to_dict(),
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "run_active": self._run_lock.locked(),
            "scheduler_lease": dict(lease) if lease is not None else None,
            "active_experiments": self.list_experiments(state="running")
            + self.list_experiments(state="monitoring"),
            "last_report": last,
        }

    def _loop(self) -> None:
        if self._stop.wait(max(0.0, self.config.initial_delay_seconds)):
            return
        while not self._stop.is_set():
            try:
                self.tick(trigger="scheduled")
            except Exception:
                self._observe("optimizer.tick.failed", "error", "scientific-optimizer", {})
            if self._stop.wait(max(1.0, self.config.interval_seconds)):
                return

    # Internal helpers -------------------------------------------------

    def _claim_tick_lease(self) -> bool:
        """Claim the singleton scheduler slot using portable transactional SQL."""
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=max(30.0, self.config.interval_seconds))).isoformat()
        with self.store.transaction() as conn:
            cursor = conn.execute(
                "UPDATE scientific_optimizer_locks "
                "SET owner_id = ?, lease_expires_at = ?, updated_at = ? "
                "WHERE name = ? AND (lease_expires_at <= ? OR owner_id = ?)",
                (self._owner_id, expires, now, "scheduler", now, self._owner_id),
            )
            updated = int(getattr(cursor, "rowcount", 0) or 0)
            if not updated:
                conn.execute(
                    "INSERT INTO scientific_optimizer_locks "
                    "(name, owner_id, lease_expires_at, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(name) DO NOTHING",
                    ("scheduler", self._owner_id, expires, now),
                )
            row = conn.execute(
                "SELECT owner_id FROM scientific_optimizer_locks WHERE name = ?",
                ("scheduler",),
            ).fetchone()
        return row is not None and str(row["owner_id"]) == self._owner_id

    def _llm_routes_for_task(
        self,
        task_id: str,
        detail: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        review_subject_ids = [
            "review_%s" % item.get("id")
            for item in detail.get("reviews", [])
            if isinstance(item, Mapping) and item.get("id")
        ]
        routes: List[Dict[str, Any]] = []
        for subject_id in [task_id, *review_subject_ids]:
            for record in self._list_observability(
                kind="log",
                name="llm.route",
                subject_type="task",
                subject_id=subject_id,
                limit=1000,
            ):
                to_dict = getattr(record, "to_dict", None)
                routes.append(to_dict() if callable(to_dict) else dict(record))
        return routes

    def _project_baseline(self, project: str) -> List[Dict[str, Any]]:
        sample_limit = min(
            50,
            max(self.config.min_baseline_tasks * 2, 20),
        )
        rows = self.store.query_all(
            "SELECT id, updated_at FROM tasks WHERE project = ? "
            "AND state IN ('completed', 'failed', 'cancelled') "
            "AND json_extract(metadata, '$.execution_contract.type') = 'repository' "
            # json_extract disagrees across backends for JSON booleans: SQLite
            # yields 1/0, Postgres yields 'true'/'false'. Comparing to the
            # integer 1 worked on SQLite and made this whole query fail on
            # Postgres with "COALESCE types text and integer cannot be
            # matched", so the optimizer could not sample anything in
            # production. Compare as text, accepting both spellings.
            "AND COALESCE(CAST(json_extract(metadata, '$.optimizer_exempt') AS TEXT), '') "
            "NOT IN ('1', 'true') "
            "AND COALESCE(json_extract(metadata, '$.origin.type'), '') "
            "NOT IN ('scientific_optimizer', 'backlog_grooming') "
            "ORDER BY COALESCE(completed_at, updated_at) DESC, id LIMIT ?",
            (project, sample_limit),
        )
        cursor = tuple((str(row["id"]), str(row["updated_at"] or "")) for row in rows)
        cached = self._baseline_cache.get(project)
        if cached is not None and cached[0] == cursor and time.monotonic() < cached[1]:
            return copy.deepcopy(cached[2])
        baseline: List[Dict[str, Any]] = []
        for row in rows:
            try:
                task_id = str(row["id"])
                detail = self._task_detail(task_id)
                task = detail.get("task") if isinstance(detail.get("task"), Mapping) else {}
                metadata = task.get("metadata") if isinstance(task.get("metadata"), Mapping) else {}
                execution = (
                    metadata.get("execution_contract")
                    if isinstance(metadata.get("execution_contract"), Mapping)
                    else {}
                )
                origin = (
                    metadata.get("origin") if isinstance(metadata.get("origin"), Mapping) else {}
                )
                # Keep the Python guard as a compatibility check for stores
                # whose JSON query implementation is less strict than
                # SQLite's. The SQL predicate above is the hot-path filter.
                if (
                    str(execution.get("type") or "") != "repository"
                    or bool(metadata.get("optimizer_exempt"))
                    or str(origin.get("type") or "") in {"scientific_optimizer", "backlog_grooming"}
                ):
                    continue
                baseline.append(
                    derive_task_kpis(
                        detail,
                        self._llm_routes_for_task(task_id, detail),
                        outcome_horizon_seconds=0,
                    )
                )
            except Exception:
                continue
        self._baseline_cache[project] = (
            cursor,
            time.monotonic() + BASELINE_CACHE_SECONDS,
            copy.deepcopy(baseline),
        )
        return baseline

    def _improvement_in_cooldown(self, project: str, hypothesis_key: str) -> bool:
        rows = self.store.query_all(
            "SELECT detail, created_at FROM scientific_optimizer_events "
            "WHERE subject_type = 'project' AND subject_id = ? "
            "AND event_type = 'improvement_task.created' "
            "ORDER BY created_at DESC LIMIT 20",
            (project,),
        )
        now = datetime.now(timezone.utc)
        for row in rows:
            detail = json_loads(row["detail"], {})
            if str(detail.get("hypothesis_key") or "") != hypothesis_key:
                continue
            created = _parse_time(row["created_at"])
            if created is None:
                continue
            return (now - created).total_seconds() < float(self.config.improvement_cooldown_seconds)
        return False

    @staticmethod
    def _strength_ladder_ready() -> bool:
        try:
            from mac.model_selection import read_active

            active = read_active()
        except Exception:
            return False
        ladder = active.get("ladder") if isinstance(active, Mapping) else None
        return isinstance(ladder, list) and len(ladder) >= 2

    def _apply_policy(
        self,
        metadata: Mapping[str, Any],
        policy: Optional[Mapping[str, Any]],
        assignment: Optional[Mapping[str, Any]],
        *,
        hypothesis: str = "",
    ) -> Dict[str, Any]:
        result = copy.deepcopy(dict(metadata))
        if policy is None:
            return result
        parameters = validate_policy_parameters(policy.get("parameters") or {})
        for key, value in parameters.items():
            if key != "review_mode" and key not in result:
                result[key] = value
        result["scientific_policy"] = {
            "schema": POLICY_SCHEMA,
            "policy_id": policy["id"],
            "name": policy["name"],
            "version": policy["version"],
        }
        if assignment is not None:
            result["scientific_optimizer"] = dict(assignment)
        review_mode = parameters.get("review_mode")
        if review_mode and "review_experiment" not in result:
            from mac.review_experiments import build_assignment

            experiment_id = str(
                (assignment or {}).get("experiment_id") or "policy:%s" % policy["id"]
            )
            arm = str((assignment or {}).get("arm") or "active")
            result["review_experiment"] = build_assignment(
                task_id=str((assignment or {}).get("task_id") or "policy"),
                experiment_id=experiment_id,
                arm=arm,
                blind=review_mode == "blind",
                hypothesis=hypothesis,
                assigned_by="scientific-optimizer",
            )
        return result

    @staticmethod
    def _task_stratum(metadata: Mapping[str, Any]) -> str:
        estimate = (
            metadata.get("scope_estimate")
            if isinstance(metadata.get("scope_estimate"), Mapping)
            else {}
        )
        size = str(estimate.get("size") or "unknown")
        execution = (
            metadata.get("execution_contract")
            if isinstance(metadata.get("execution_contract"), Mapping)
            else {}
        )
        quality = str(execution.get("quality") or "unknown")
        return "%s:%s" % (size, quality)

    @staticmethod
    def _normalize_guardrails(
        raw: Optional[Mapping[str, Any]], quality_margin: float
    ) -> Dict[str, Any]:
        # These quality endpoints are immutable parts of the optimizer safety
        # contract. Callers may tighten their margins but cannot omit them.
        source: Dict[str, Any] = {
            "accepted_success": {"direction": "maximize", "margin": quality_margin},
            "delayed_quality_success": {
                "direction": "maximize",
                "margin": quality_margin,
            },
            "escaped_defect_severity": {"direction": "minimize", "margin": 0.0},
        }
        source.update(dict(raw or {}))
        normalized: Dict[str, Any] = {}
        for metric, spec_raw in source.items():
            if metric not in METRIC_DIRECTIONS:
                raise ValidationError("unsupported guardrail metric: %s" % metric)
            spec = dict(spec_raw) if isinstance(spec_raw, Mapping) else {}
            direction = str(spec.get("direction") or METRIC_DIRECTIONS[metric]).lower()
            if direction not in {"maximize", "minimize"}:
                raise ValidationError("guardrail direction must be maximize or minimize")
            normalized[metric] = {
                "direction": direction,
                "margin": _bounded_float(
                    spec.get("margin", quality_margin),
                    "guardrail margin",
                    0.0,
                    1_000_000_000.0,
                ),
            }
        return normalized

    def _compare_metric(
        self,
        experiment_id: str,
        metric: str,
        by_arm: Mapping[str, List[Dict[str, Any]]],
        *,
        alpha: float,
    ) -> Dict[str, Any]:
        control = [float(item["metrics"].get(metric) or 0.0) for item in by_arm["control"]]
        treatment = [float(item["metrics"].get(metric) or 0.0) for item in by_arm["treatment"]]
        result = _bootstrap_mean_difference(
            control,
            treatment,
            seed="%s|%s" % (experiment_id, metric),
            alpha=alpha,
        )
        return {
            "metric": metric,
            "control_mean": sum(control) / len(control),
            "treatment_mean": sum(treatment) / len(treatment),
            **result,
        }

    def _compare_guardrail(
        self,
        experiment_id: str,
        metric: str,
        spec: Mapping[str, Any],
        by_arm: Mapping[str, List[Dict[str, Any]]],
        *,
        alpha: float,
    ) -> Dict[str, Any]:
        result = self._compare_metric(
            experiment_id,
            metric,
            by_arm,
            alpha=alpha,
        )
        margin = float(spec.get("margin") or 0.0)
        direction = str(spec.get("direction") or METRIC_DIRECTIONS[metric])
        noninferior = (
            result["ci_lower"] >= -margin
            if direction == "maximize"
            else result["ci_upper"] <= margin
        )
        return {
            **result,
            "direction": direction,
            "margin": margin,
            "noninferior": noninferior,
        }

    def _promote_experiment(
        self,
        experiment: Mapping[str, Any],
        decision: Mapping[str, Any],
        *,
        actor: str,
        reason: str,
    ) -> None:
        self.promote_policy(
            experiment["treatment_policy_id"],
            actor=actor,
            reason="experiment %s: %s" % (experiment["id"], reason),
        )
        metadata = dict(experiment.get("metadata") or {})
        metadata["promoted_at"] = utcnow()
        metadata["promotion_decision"] = dict(decision)
        now = utcnow()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE scientific_experiments SET state = 'monitoring', metadata = ?, updated_at = ? WHERE id = ?",
                (json_dumps(metadata), now, experiment["id"]),
            )
            self._insert_event(
                conn,
                "experiment",
                experiment["id"],
                "experiment.promoted_to_monitoring",
                actor,
                {"reason": reason},
                now,
            )

    def _record_decision(
        self, experiment: Mapping[str, Any], decision: Mapping[str, Any], actor: str
    ) -> bool:
        """Persist a materially new decision and suppress unchanged scheduler polls."""
        now = utcnow()
        decision_id = new_id("decision")
        with self.store.transaction() as conn:
            previous_row = conn.execute(
                "SELECT decision FROM scientific_decisions "
                "WHERE experiment_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (experiment["id"],),
            ).fetchone()
            if previous_row is not None:
                previous = json_loads(previous_row["decision"], {})
                if self._decision_fingerprint(previous) == self._decision_fingerprint(decision):
                    return False
            conn.execute(
                "INSERT INTO scientific_decisions (id, experiment_id, status, decision, actor, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    experiment["id"],
                    decision["status"],
                    json_dumps(dict(decision)),
                    actor,
                    now,
                ),
            )
            if decision["status"] == "promote" and not experiment["auto_promote"]:
                conn.execute(
                    "UPDATE scientific_experiments SET state = 'candidate', updated_at = ? WHERE id = ?",
                    (now, experiment["id"]),
                )
            self._insert_event(
                conn,
                "experiment",
                experiment["id"],
                "experiment.decision",
                actor,
                {"decision_id": decision_id, "status": decision["status"]},
                now,
            )
        return True

    @staticmethod
    def _decision_fingerprint(decision: Mapping[str, Any]) -> str:
        """Return stable decision content, excluding poll-time bookkeeping."""
        stable = copy.deepcopy(dict(decision))
        stable.pop("generated_at", None)
        return hashlib.sha256(json_dumps(stable).encode("utf-8")).hexdigest()

    def _set_experiment_state(
        self,
        experiment_id: str,
        state: str,
        actor: str,
        reason: str,
        *,
        release_slot: bool,
    ) -> None:
        if state not in EXPERIMENT_STATES:
            raise ValidationError("unsupported experiment state: %s" % state)
        now = utcnow()
        experiment = self.get_experiment(experiment_id)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE scientific_experiments SET state = ?, running_slot = ?, updated_at = ? WHERE id = ?",
                (
                    state,
                    None if release_slot else experiment["project"],
                    now,
                    experiment_id,
                ),
            )
            self._insert_event(
                conn,
                "experiment",
                experiment_id,
                "experiment.%s" % state,
                actor,
                {"reason": str(reason or "")[:1000]},
                now,
            )

    def _insert_event(
        self,
        conn: Any,
        subject_type: str,
        subject_id: str,
        event_type: str,
        actor: str,
        detail: Mapping[str, Any],
        when: str,
    ) -> None:
        conn.execute(
            "INSERT INTO scientific_optimizer_events (id, subject_type, subject_id, event_type, actor, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("optevt"),
                subject_type,
                subject_id,
                event_type,
                str(actor or "scientific-optimizer"),
                json_dumps(dict(detail)),
                when,
            ),
        )

    def _observe(self, name: str, level: str, subject_id: str, detail: Mapping[str, Any]) -> None:
        try:
            self.observability.record_log(
                name,
                level=level,
                layer="control_plane",
                source="scientific-optimizer",
                subject_type="service" if subject_id == "scientific-optimizer" else "experiment",
                subject_id=subject_id,
                detail=dict(detail),
            )
        except Exception:
            pass

    @staticmethod
    def _policy_from_row(row: Any) -> Dict[str, Any]:
        return {
            "schema": POLICY_SCHEMA,
            "id": row["id"],
            "project": row["project"],
            "name": row["name"],
            "version": int(row["version"]),
            "description": row["description"],
            "status": row["status"],
            "parameters": json_loads(row["parameters"], {}),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _experiment_from_row(row: Any) -> Dict[str, Any]:
        return {
            "schema": EXPERIMENT_SCHEMA,
            "id": row["id"],
            "project": row["project"],
            "name": row["name"],
            "hypothesis": row["hypothesis"],
            "state": row["state"],
            "control_policy_id": row["control_policy_id"],
            "treatment_policy_id": row["treatment_policy_id"],
            "primary_metric": row["primary_metric"],
            "direction": row["direction"],
            "min_effect": float(row["min_effect"]),
            "quality_margin": float(row["quality_margin"]),
            "min_samples_per_arm": int(row["min_samples_per_arm"]),
            "max_samples_per_arm": int(row["max_samples_per_arm"]),
            "exploration_fraction": float(row["exploration_fraction"]),
            "outcome_horizon_seconds": float(row["outcome_horizon_seconds"]),
            "guardrails": json_loads(row["guardrails"], {}),
            "auto_promote": bool(row["auto_promote"]),
            "metadata": json_loads(row["metadata"], {}),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _assignment_from_row(row: Any) -> Dict[str, Any]:
        payload = json_loads(row["assignment"], {})
        return {
            "schema": ASSIGNMENT_SCHEMA,
            "experiment_id": row["experiment_id"],
            "task_id": row["task_id"],
            "arm": row["arm"],
            "policy_id": row["policy_id"],
            "phase": row["phase"],
            "propensity": float(row["propensity"]),
            "stratum": row["stratum"],
            "assigned_at": payload.get("assigned_at") or row["assigned_at"],
        }

    @staticmethod
    def _observation_from_row(row: Any) -> Dict[str, Any]:
        return {
            "schema": OBSERVATION_SCHEMA,
            "experiment_id": row["experiment_id"],
            "task_id": row["task_id"],
            "arm": row["arm"],
            "phase": row["phase"],
            "terminal": bool(row["terminal"]),
            "quality_validated": bool(row["quality_validated"]),
            "metrics": json_loads(row["metrics"], {}),
            "observed_at": row["observed_at"],
        }


__all__ = [
    "ASSIGNMENT_SCHEMA",
    "DECISION_SCHEMA",
    "EXPERIMENT_SCHEMA",
    "METRIC_DIRECTIONS",
    "POLICY_PARAMETER_TYPES",
    "POLICY_SCHEMA",
    "ScientificOptimizerConfig",
    "ScientificOptimizerService",
    "derive_task_kpis",
    "estimate_route_cost",
    "validate_policy_parameters",
]
