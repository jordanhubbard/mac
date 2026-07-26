"""Durable, replayable observations for review-strategy experiments.

The task ledger remains the source of truth.  Experiment assignments and
operator-labelled outcomes live in task metadata, while executor/reviewer
behavior is reconstructed from signed evidence, reviews, history, and
publications.  Reports are therefore derivable again after the reporting code
changes; there is no second telemetry database to drift away from the ledger.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from mac.models import ValidationError


ASSIGNMENT_SCHEMA = "mac.review_experiment.v1"
OUTCOME_SCHEMA = "mac.review_outcome.v1"
OBSERVATION_SCHEMA = "mac.review_observation.v1"
REPORT_SCHEMA = "mac.review_policy_report.v1"

_FINAL_TASK_STATES = {"completed", "failed", "cancelled"}
_LABELLED_OUTCOME_STATUSES = {"confirmed", "refuted"}
_OUTCOME_STATUSES = _LABELLED_OUTCOME_STATUSES | {"pending"}
_MAX_OUTCOMES = 200
_MAX_DETAIL_BYTES = 24 * 1024


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _object(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite_nonnegative(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("%s must be a number" % field) from exc
    if not math.isfinite(number) or number < 0:
        raise ValidationError("%s must be finite and non-negative" % field)
    return number


def _integer_threshold(value: Any, field: str, *, minimum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("%s must be an integer" % field) from exc
    if number < minimum:
        raise ValidationError("%s must be at least %d" % (field, minimum))
    return number


def normalize_arm_weights(raw: Mapping[str, Any]) -> Dict[str, float]:
    """Normalize raw arm weights into a probability distribution."""
    weights: Dict[str, float] = {}
    for name, value in raw.items():
        arm = _text(name)
        if not arm:
            raise ValidationError("review experiment arm names must be non-empty")
        weight = _finite_nonnegative(value, "review experiment arm weight")
        if weight > 0:
            weights[arm] = weight
    total = sum(weights.values())
    if total <= 0:
        raise ValidationError("review experiment arms require at least one positive weight")
    return {name: weights[name] / total for name in sorted(weights)}


def choose_weighted_arm(
    task_id: str,
    experiment_id: str,
    policy_version: str,
    arms: Mapping[str, Any],
) -> Tuple[str, float, Dict[str, float]]:
    """Deterministically choose a weighted experiment arm for the task."""
    distribution = normalize_arm_weights(arms)
    seed = "%s|%s|%s" % (experiment_id, task_id, policy_version)
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    point = int.from_bytes(digest[:8], "big") / float(1 << 64)
    cumulative = 0.0
    selected = next(reversed(distribution))
    for arm, probability in distribution.items():
        cumulative += probability
        if point < cumulative:
            selected = arm
            break
    return selected, distribution[selected], distribution


def build_assignment(
    *,
    task_id: str,
    experiment_id: str,
    arm: Optional[str] = None,
    arms: Optional[Mapping[str, Any]] = None,
    assignment_probability: Optional[float] = None,
    blind: bool = False,
    blind_arms: Optional[Iterable[str]] = None,
    policy_version: str = "v1",
    hypothesis: str = "",
    stratum: str = "",
    assigned_by: str = "human",
    assigned_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a review experiment assignment record."""
    experiment = _text(experiment_id)
    if not experiment:
        raise ValidationError("review experiment id is required")
    explicit_arm = _text(arm)
    if explicit_arm and arms:
        raise ValidationError("provide either arm or arms, not both")
    if not explicit_arm and not arms:
        raise ValidationError("review experiment arm or weighted arms are required")
    if arms and assignment_probability is not None:
        raise ValidationError(
            "assignment_probability is computed for weighted arms"
        )

    distribution: Dict[str, float]
    if arms:
        chosen, probability, distribution = choose_weighted_arm(
            task_id, experiment, _text(policy_version) or "v1", arms
        )
        method = "deterministic_weighted"
    else:
        chosen = explicit_arm
        probability = 1.0 if assignment_probability is None else _finite_nonnegative(
            assignment_probability, "assignment_probability"
        )
        if probability <= 0 or probability > 1:
            raise ValidationError("assignment_probability must be greater than 0 and at most 1")
        distribution = {chosen: probability}
        method = "explicit"

    blind_arm_names = sorted({_text(value) for value in (blind_arms or []) if _text(value)})
    unknown_blind_arms = sorted(set(blind_arm_names) - set(distribution))
    if unknown_blind_arms:
        raise ValidationError(
            "blind_arms are not present in the arm distribution: %s"
            % ", ".join(unknown_blind_arms)
        )
    return {
        "schema": ASSIGNMENT_SCHEMA,
        "experiment_id": experiment,
        "arm": chosen,
        "assignment_method": method,
        "assignment_probability": probability,
        "arm_distribution": distribution,
        "blind": bool(blind or chosen in blind_arm_names),
        "blind_arms": blind_arm_names,
        "policy_version": _text(policy_version) or "v1",
        "hypothesis": _text(hypothesis),
        "stratum": _text(stratum),
        "assigned_by": _text(assigned_by) or "human",
        "assigned_at": _text(assigned_at) or _utcnow(),
    }


def parse_assignment(metadata: Any) -> Optional[Dict[str, Any]]:
    """Parse a review experiment assignment from task metadata."""
    block = _object(_object(metadata).get("review_experiment"))
    if block.get("schema") != ASSIGNMENT_SCHEMA:
        return None
    if not _text(block.get("experiment_id")) or not _text(block.get("arm")):
        return None
    return block


def build_outcome(
    *,
    kind: str,
    status: str,
    observed_by: str,
    finding_id: str = "",
    severity_weight: float = 1.0,
    source: str = "operator",
    detail: Optional[Mapping[str, Any]] = None,
    observed_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a review experiment outcome record."""
    kind_value = _text(kind)
    status_value = _text(status).lower()
    if not kind_value:
        raise ValidationError("review outcome kind is required")
    if not status_value:
        raise ValidationError("review outcome status is required")
    if status_value not in _OUTCOME_STATUSES:
        raise ValidationError(
            "review outcome status must be confirmed, refuted, or pending"
        )
    detail_value = _object(detail)
    if len(json.dumps(detail_value, sort_keys=True).encode("utf-8")) > _MAX_DETAIL_BYTES:
        raise ValidationError("review outcome detail exceeds 24 KiB")
    payload = {
        "schema": OUTCOME_SCHEMA,
        "kind": kind_value,
        "status": status_value,
        "finding_id": _text(finding_id),
        "severity_weight": _finite_nonnegative(severity_weight, "severity_weight"),
        "source": _text(source) or "operator",
        "detail": detail_value,
        "observed_by": _text(observed_by) or "human",
        "observed_at": _text(observed_at) or _utcnow(),
    }
    identity = dict(payload)
    identity.pop("observed_at", None)
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    payload["id"] = "reviewoutcome_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return payload


def append_outcome(metadata: Any, outcome: Mapping[str, Any]) -> Dict[str, Any]:
    """Append an outcome to the task review-outcomes metadata."""
    next_metadata = _object(metadata)
    current = [
        _object(item)
        for item in _list(next_metadata.get("review_outcomes"))
        if _object(item).get("schema") == OUTCOME_SCHEMA
    ]
    outcome_value = _object(outcome)
    outcome_id = _text(outcome_value.get("id"))
    current = [item for item in current if _text(item.get("id")) != outcome_id]
    current.append(outcome_value)
    next_metadata["review_outcomes"] = current[-_MAX_OUTCOMES:]
    return next_metadata


def _verification(evidence: Mapping[str, Any]) -> Dict[str, Any]:
    return _object(_object(evidence.get("metadata")).get("verification"))


def _model_identity(manifest: Mapping[str, Any]) -> Dict[str, str]:
    llm = _object(manifest.get("llm"))
    model = _text(llm.get("model"))
    if not model:
        for key in ("llm_model", "opencode_model", "gateway_model"):
            model = _text(manifest.get(key))
            if model:
                break
    normalized_model = model.lower()
    provider = _text(llm.get("provider") or manifest.get("llm_provider"))
    family = _text(llm.get("family") or manifest.get("llm_family"))
    if not provider:
        for candidate in ("anthropic", "openai", "google", "xai", "qwen", "deepseek"):
            if candidate in normalized_model:
                provider = candidate
                break
    if not family:
        for candidate in ("claude", "gpt", "gemini", "grok", "qwen", "deepseek", "llama", "mistral"):
            if candidate in normalized_model:
                family = candidate
                break
    return {
        "model": model,
        "family": family,
        "provider": provider,
        "identity_source": (
            "declared"
            if llm.get("family") or llm.get("provider") or manifest.get("llm_family") or manifest.get("llm_provider")
            else "derived_from_model_id"
        ),
        "tool": _text(llm.get("tool")),
        "agent": _text(llm.get("agent")),
    }


def _numeric(mapping: Mapping[str, Any], *paths: str) -> float:
    for path in paths:
        value: Any = mapping
        for part in path.split("."):
            value = _object(value).get(part)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return 0.0


def _usage(manifest: Mapping[str, Any]) -> Dict[str, float]:
    return {
        "input_tokens": _numeric(manifest, "usage.input_tokens", "usage.prompt_tokens"),
        "output_tokens": _numeric(manifest, "usage.output_tokens", "usage.completion_tokens"),
        "cost_usd": _numeric(manifest, "usage.cost_usd", "cost_usd"),
        "latency_ms": _numeric(manifest, "usage.latency_ms", "latency_ms"),
    }


def _finding_items(value: Any) -> List[Any]:
    """Accept both canonical list findings and common id-keyed manifests."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, Mapping):
        items: List[Any] = []
        for key, raw in value.items():
            if isinstance(raw, Mapping):
                item = dict(raw)
                item.setdefault("id", _text(key))
                items.append(item)
            else:
                items.append({"id": _text(key), "summary": _text(raw)})
        return items
    return []


def _route_summary(
    routes: Iterable[Mapping[str, Any]],
    *,
    agent_id: str,
    since: str = "",
    until: str = "",
) -> Dict[str, Any]:
    """Aggregate task-attributed router observations for one worker interval."""
    selected: List[Dict[str, Any]] = []
    for raw in routes:
        route = _object(raw)
        detail = _object(route.get("detail"))
        created_at = _text(route.get("created_at"))
        route_agent = _text(detail.get("agent_id") or route.get("source"))
        if agent_id and route_agent != agent_id:
            continue
        if since and created_at and created_at < since:
            continue
        if until and created_at and created_at > until:
            continue
        if _text(detail.get("schema")) != "mac.llm_route.v1":
            continue
        selected.append(detail)

    models = sorted(
        {
            _text(item.get("response_model") or item.get("resolved_model"))
            for item in selected
            if _text(item.get("response_model") or item.get("resolved_model"))
        }
    )
    providers = sorted(
        {_text(item.get("provider")) for item in selected if _text(item.get("provider"))}
    )
    if len(models) == 1:
        model = models[0]
    elif models:
        model = "mixed:" + ",".join(models)
    else:
        model = ""
    provider = providers[0] if len(providers) == 1 else ""
    identity = _model_identity(
        {
            "llm": {
                "model": model,
                "provider": provider,
                "tool": "mac-router",
                "agent": agent_id,
            }
        }
    )
    usage = {
        "input_tokens": sum(
            _numeric(_object(item.get("usage")), "prompt_tokens", "input_tokens")
            for item in selected
        ),
        "output_tokens": sum(
            _numeric(_object(item.get("usage")), "completion_tokens", "output_tokens")
            for item in selected
        ),
        "cost_usd": sum(_numeric(item, "cost_usd", "usage.cost_usd") for item in selected),
        "latency_ms": sum(_numeric(item, "duration_ms") for item in selected),
    }
    return {
        "model": identity,
        "usage": usage,
        "route_count": len(selected),
        "resolved_models": models,
        "providers": providers,
    }


def _finding_payload(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        payload = {"summary": _text(value)}
    summary = _text(
        payload.get("summary")
        or payload.get("message")
        or payload.get("note")
        or payload.get("title")
    )
    payload.setdefault("summary", summary)
    return payload


def finding_fingerprint(
    task_id: str,
    review_evidence_id: str,
    index: int,
    finding: Mapping[str, Any],
) -> str:
    """Compute a stable fingerprint for a review finding."""
    stable = {
        "task_id": task_id,
        "review_evidence_id": review_evidence_id,
        "index": index,
        "category": _text(finding.get("category") or finding.get("kind")),
        "path": _text(finding.get("path") or finding.get("file") or finding.get("location")),
        "line": finding.get("line"),
        "summary": _text(finding.get("summary") or finding.get("message") or finding.get("note")),
    }
    digest = hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "reviewfinding_" + digest[:24]


def _strategy(executor: Mapping[str, str], reviewer: Mapping[str, str]) -> str:
    executor_model = _text(executor.get("model")).lower()
    reviewer_model = _text(reviewer.get("model")).lower()
    if not executor_model or not reviewer_model:
        return "unknown"
    if executor_model == reviewer_model:
        return "same_model"
    executor_family = _text(executor.get("family")).lower()
    reviewer_family = _text(reviewer.get("family")).lower()
    if executor_family and reviewer_family and executor_family != reviewer_family:
        return "cross_family"
    executor_provider = _text(executor.get("provider")).lower()
    reviewer_provider = _text(reviewer.get("provider")).lower()
    if executor_provider and reviewer_provider and executor_provider != reviewer_provider:
        return "cross_provider"
    return "cross_model"


def _outcomes(metadata: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [
        _object(item)
        for item in _list(metadata.get("review_outcomes"))
        if _object(item).get("schema") == OUTCOME_SCHEMA
    ]


def build_observation(
    task_detail: Mapping[str, Any],
    *,
    llm_routes: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a review experiment observation from task detail."""
    task = _object(task_detail.get("task"))
    task_id = _text(task.get("id"))
    metadata = _object(task.get("metadata"))
    assignment = parse_assignment(metadata)
    evidence = [_object(item) for item in _list(task_detail.get("evidence"))]
    evidence_by_id = {_text(item.get("id")): item for item in evidence}
    reviews = [_object(item) for item in _list(task_detail.get("reviews"))]
    routes = list(llm_routes or [])
    review_by_evidence = {
        _text(item.get("evidence_id")): item
        for item in reviews
        if _text(item.get("evidence_id"))
    }
    outcome_items = _outcomes(metadata)
    protocol_invalidations = [
        item
        for item in outcome_items
        if _text(item.get("kind")) == "protocol_invalid"
        and _text(item.get("status")) == "confirmed"
    ]
    outcomes_by_finding: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for outcome in outcome_items:
        finding_id = _text(outcome.get("finding_id"))
        if finding_id:
            outcomes_by_finding[finding_id].append(outcome)

    review_passes: List[Dict[str, Any]] = []
    seen_review_evidence: set[str] = set()
    for review_evidence in evidence:
        manifest = _verification(review_evidence)
        if _text(manifest.get("evidence_type")).lower() != "review_verdict":
            continue
        review_evidence_id = _text(review_evidence.get("id"))
        seen_review_evidence.add(review_evidence_id)
        executor_evidence_id = _text(manifest.get("reviewed_evidence_id"))
        executor_evidence = evidence_by_id.get(executor_evidence_id, {})
        executor_manifest = _verification(executor_evidence)
        executor_model = _model_identity(executor_manifest)
        reviewer_model = _model_identity(manifest)
        review_record = review_by_evidence.get(review_evidence_id, {})
        executor_route = _route_summary(
            routes,
            agent_id=_text(
                executor_evidence.get("created_by")
                or executor_manifest.get("signed_by")
            ),
            since=_text(task.get("created_at")),
            until=_text(executor_evidence.get("created_at")),
        )
        reviewer_route = _route_summary(
            routes,
            agent_id=_text(
                review_record.get("reviewer_agent_id")
                or review_evidence.get("created_by")
                or manifest.get("signed_by")
            ),
            since=_text(review_record.get("created_at")),
            until=_text(review_record.get("completed_at"))
            or _text(review_evidence.get("created_at")),
        )
        if not _text(executor_model.get("model")):
            executor_model = _object(executor_route.get("model"))
        if not _text(reviewer_model.get("model")):
            reviewer_model = _object(reviewer_route.get("model"))
        findings: List[Dict[str, Any]] = []
        for index, raw_finding in enumerate(_finding_items(manifest.get("findings"))):
            finding = _finding_payload(raw_finding)
            finding_id = _text(finding.get("id")) or finding_fingerprint(
                task_id, review_evidence_id, index, finding
            )
            labels = outcomes_by_finding.get(finding_id, [])
            finding["id"] = finding_id
            finding["outcomes"] = labels
            finding["validation_status"] = (
                _text(labels[-1].get("status")) if labels else "unresolved"
            )
            findings.append(finding)
        manifest_usage = _usage(manifest)
        if not any(manifest_usage.values()):
            manifest_usage = _object(reviewer_route.get("usage"))
        experiment_protocol = copy.deepcopy(
            _object(manifest.get("review_experiment"))
        )
        if protocol_invalidations:
            protocol = _object(experiment_protocol.get("protocol"))
            protocol.update(
                {
                    "protocol_compliant": False,
                    "operator_invalidated": True,
                    "invalidations": [
                        {
                            "id": _text(item.get("id")),
                            "finding_id": _text(item.get("finding_id")),
                            "source": _text(item.get("source")),
                            "detail": _object(item.get("detail")),
                        }
                        for item in protocol_invalidations
                    ],
                }
            )
            experiment_protocol["protocol"] = protocol
        review_passes.append(
            {
                "review_id": _text(manifest.get("review_id") or review_record.get("id")),
                "review_evidence_id": review_evidence_id,
                "executor_evidence_id": executor_evidence_id,
                "review_status": _text(review_record.get("status")),
                "verdict": _text(manifest.get("verdict")).lower(),
                "semantic_verdict": _text(manifest.get("semantic_verdict")).lower(),
                "executor_model": executor_model,
                "reviewer_model": reviewer_model,
                "actual_strategy": _strategy(executor_model, reviewer_model),
                "experiment_protocol": experiment_protocol,
                "independent_findings": _list(manifest.get("independent_findings")),
                "findings": findings,
                "usage": manifest_usage,
                "llm_routes": {
                    "executor": executor_route,
                    "reviewer": reviewer_route,
                },
                "created_at": _text(review_evidence.get("created_at")),
            }
        )

    # Pending reviews have no verdict evidence yet, but belong in lifecycle
    # observability so an experiment cannot silently count only successes.
    pending_reviews = [
        {
            "review_id": _text(review.get("id")),
            "reviewer_agent_id": _text(review.get("reviewer_agent_id")),
            "status": _text(review.get("status")),
            "created_at": _text(review.get("created_at")),
        }
        for review in reviews
        if not _text(review.get("evidence_id"))
        or _text(review.get("evidence_id")) not in seen_review_evidence
    ]

    findings = [finding for item in review_passes for finding in item["findings"]]
    confirmed = sum(1 for item in findings if item["validation_status"] == "confirmed")
    refuted = sum(1 for item in findings if item["validation_status"] == "refuted")

    # executor_attempt_count: number of distinct executor evidence items (evidence
    # whose verification.evidence_type is not review_verdict or publication).
    # Each represents one run of the executor patch.  When the task struct carries
    # attempt_count directly (the common case from hub API responses), prefer that
    # authoritative counter; fall back to counting evidence rows so the function
    # works correctly when only a partial task_detail is supplied.
    raw_attempt_count = task.get("attempt_count")
    if raw_attempt_count is not None:
        try:
            executor_attempt_count = int(raw_attempt_count)
        except (TypeError, ValueError):
            executor_attempt_count = None
    else:
        executor_attempt_count = None
    if executor_attempt_count is None:
        _executor_evidence_types = {"repo_change", "operator_result", "plan_decomposed"}
        executor_attempt_count = sum(
            1
            for item in evidence
            if _text(_verification(item).get("evidence_type")).lower()
            in _executor_evidence_types
        )

    # review_attempt_count: total number of review records (approved, rejected,
    # retracted, pending).  Retracted reviews caused by protocol failure count
    # because they represent real reviewer execution budget spent, even though they
    # did NOT consume an executor attempt.  This counter is distinct from
    # review_passes (which counts only successful verdict evidence rows) and makes
    # it possible to audit how many reviewer invocations occurred vs executor runs.
    review_attempt_count = len(reviews)

    return {
        "schema": OBSERVATION_SCHEMA,
        "task_id": task_id,
        "project": _text(task.get("project")),
        "task_state": _text(task.get("state")),
        "terminal": _text(task.get("state")) in _FINAL_TASK_STATES,
        "sample_valid": not protocol_invalidations,
        "protocol_invalidations": protocol_invalidations,
        "created_at": _text(task.get("created_at")),
        "completed_at": _text(task.get("completed_at")),
        "experiment": assignment,
        "review_passes": review_passes,
        "pending_reviews": pending_reviews,
        "outcomes": outcome_items,
        "totals": {
            "review_passes": len(review_passes),
            "findings": len(findings),
            "independent_findings": sum(
                len(_list(item.get("independent_findings")))
                for item in review_passes
            ),
            "confirmed_findings": confirmed,
            "refuted_findings": refuted,
            "unresolved_findings": len(findings) - confirmed - refuted,
            "escaped_defects": sum(
                1
                for item in outcome_items
                if _text(item.get("kind")) == "escaped_defect"
                and _text(item.get("status")) == "confirmed"
            ),
            "protocol_invalidations": len(protocol_invalidations),
            "executor_attempt_count": executor_attempt_count,
            "review_attempt_count": review_attempt_count,
        },
    }


def _empty_arm(arm: str) -> Dict[str, Any]:
    return {
        "arm": arm,
        "tasks": 0,
        "terminal_tasks": 0,
        "completed_tasks": 0,
        "failed_tasks": 0,
        "cancelled_tasks": 0,
        "review_passes": 0,
        "approved_verdicts": 0,
        "rejected_verdicts": 0,
        "findings": 0,
        "independent_findings": 0,
        "confirmed_findings": 0,
        "refuted_findings": 0,
        "unresolved_findings": 0,
        "escaped_defects": 0,
        "validated_outcomes": 0,
        "confirmed_severity": 0.0,
        "refuted_severity": 0.0,
        "escaped_severity": 0.0,
        "cost_usd": 0.0,
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "latency_ms": 0.0,
        "discovery_duration_ms": 0.0,
        "protocol_compliant_passes": 0,
        "protocol_noncompliant_passes": 0,
        "protocol_invalid_tasks": 0,
        "actual_strategies": {},
        "executor_attempt_count": 0,
        "review_attempt_count": 0,
    }


def build_report(
    experiment_id: str,
    observations: Iterable[Mapping[str, Any]],
    *,
    min_tasks_per_arm: int = 5,
    min_validated_outcomes_per_arm: int = 3,
) -> Dict[str, Any]:
    """Build an aggregated review experiment report from observations."""
    experiment = _text(experiment_id)
    task_threshold = _integer_threshold(
        min_tasks_per_arm, "min_tasks_per_arm", minimum=1
    )
    outcome_threshold = _integer_threshold(
        min_validated_outcomes_per_arm,
        "min_validated_outcomes_per_arm",
        minimum=0,
    )
    arms: Dict[str, Dict[str, Any]] = {}
    included: List[Dict[str, Any]] = []
    for raw in observations:
        observation = _object(raw)
        assignment = _object(observation.get("experiment"))
        if _text(assignment.get("experiment_id")) != experiment:
            continue
        arm_name = _text(assignment.get("arm")) or "unassigned"
        arm = arms.setdefault(arm_name, _empty_arm(arm_name))
        included.append(observation)
        arm["tasks"] += 1
        state = _text(observation.get("task_state"))
        if observation.get("terminal"):
            arm["terminal_tasks"] += 1
        if state == "completed":
            arm["completed_tasks"] += 1
        elif state == "failed":
            arm["failed_tasks"] += 1
        elif state == "cancelled":
            arm["cancelled_tasks"] += 1
        totals = _object(observation.get("totals"))
        for key in (
            "review_passes",
            "findings",
            "independent_findings",
            "confirmed_findings",
            "refuted_findings",
            "unresolved_findings",
            "escaped_defects",
            "executor_attempt_count",
            "review_attempt_count",
        ):
            arm[key] += int(totals.get(key) or 0)
        if observation.get("sample_valid") is False:
            arm["protocol_invalid_tasks"] += 1
        for review_pass in _list(observation.get("review_passes")):
            review_value = _object(review_pass)
            verdict = _text(review_value.get("verdict"))
            if verdict == "approved":
                arm["approved_verdicts"] += 1
            elif verdict == "rejected":
                arm["rejected_verdicts"] += 1
            strategy = _text(review_value.get("actual_strategy")) or "unknown"
            arm["actual_strategies"][strategy] = arm["actual_strategies"].get(strategy, 0) + 1
            experiment_protocol = _object(review_value.get("experiment_protocol"))
            protocol = _object(experiment_protocol.get("protocol"))
            if protocol.get("protocol_compliant") is True:
                arm["protocol_compliant_passes"] += 1
            else:
                arm["protocol_noncompliant_passes"] += 1
            arm["discovery_duration_ms"] += float(
                protocol.get("discovery_duration_ms") or 0
            )
            usage = _object(review_value.get("usage"))
            for key in ("cost_usd", "input_tokens", "output_tokens", "latency_ms"):
                arm[key] += float(usage.get(key) or 0)
        for outcome in _list(observation.get("outcomes")):
            outcome_value = _object(outcome)
            status = _text(outcome_value.get("status"))
            kind = _text(outcome_value.get("kind"))
            severity = float(outcome_value.get("severity_weight") or 0)
            if status in _LABELLED_OUTCOME_STATUSES and kind in {
                "finding_validation",
                "escaped_defect",
                "clean_window",
            }:
                arm["validated_outcomes"] += 1
            if kind == "finding_validation" and status == "confirmed":
                arm["confirmed_severity"] += severity
            elif kind == "finding_validation" and status == "refuted":
                arm["refuted_severity"] += severity
            elif kind == "escaped_defect" and status == "confirmed":
                arm["escaped_severity"] += severity

    for arm in arms.values():
        tasks = max(1, int(arm["tasks"]))
        labelled = int(arm["confirmed_findings"]) + int(arm["refuted_findings"])
        arm["completion_rate"] = arm["completed_tasks"] / tasks
        arm["finding_precision"] = (
            arm["confirmed_findings"] / labelled if labelled else None
        )
        arm["score_per_task"] = (
            arm["confirmed_severity"]
            - arm["refuted_severity"]
            - (5.0 * arm["escaped_severity"])
        ) / tasks

    ordered_arms = [arms[key] for key in sorted(arms)]
    insufficient = [
        arm["arm"]
        for arm in ordered_arms
        if arm["terminal_tasks"] < task_threshold
        or arm["review_passes"] < task_threshold
        or arm["validated_outcomes"] < outcome_threshold
        or arm["protocol_noncompliant_passes"] > 0
    ]
    if len(ordered_arms) < 2:
        policy = {
            "status": "insufficient_evidence",
            "reason": "at least two experiment arms are required",
            "candidate_arm": None,
        }
    elif insufficient:
        policy = {
            "status": "insufficient_evidence",
            "reason": "minimum terminal-task, completed-review, validated-outcome, or protocol-compliance threshold not met",
            "insufficient_arms": insufficient,
            "candidate_arm": None,
        }
    else:
        ranked = sorted(
            ordered_arms,
            key=lambda item: (float(item["score_per_task"]), float(item["completion_rate"])),
            reverse=True,
        )
        score_margin = float(ranked[0]["score_per_task"]) - float(
            ranked[1]["score_per_task"]
        )
        if score_margin <= 0:
            policy = {
                "status": "inconclusive",
                "reason": "the leading arms have no positive quality-score separation",
                "candidate_arm": None,
                "score_margin": score_margin,
            }
        else:
            policy = {
                "status": "candidate",
                "reason": "observed severity-weighted utility; promotion still requires an operator or a separately configured statistical gate",
                "candidate_arm": ranked[0]["arm"],
                "score_margin": score_margin,
            }

    return {
        "schema": REPORT_SCHEMA,
        "experiment_id": experiment,
        "generated_at": _utcnow(),
        "task_count": len(included),
        "min_tasks_per_arm": task_threshold,
        "min_validated_outcomes_per_arm": outcome_threshold,
        "arms": ordered_arms,
        "policy": policy,
        "observations": included,
    }


__all__ = [
    "ASSIGNMENT_SCHEMA",
    "OBSERVATION_SCHEMA",
    "OUTCOME_SCHEMA",
    "REPORT_SCHEMA",
    "append_outcome",
    "build_assignment",
    "build_observation",
    "build_outcome",
    "build_report",
    "choose_weighted_arm",
    "finding_fingerprint",
    "normalize_arm_weights",
    "parse_assignment",
]
