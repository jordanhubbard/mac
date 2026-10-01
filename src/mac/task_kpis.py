"""Per-task KPI projection over the task ledger and router evidence.

``derive_task_kpis`` projects one task detail (task, reviews, publications)
plus its attributed LLM route records into canonical quality, cycle, time and
cost metrics. ``ControlPlane._task_execution_profile`` uses it to build the
read-only execution profile operators see on a task. It is a pure function:
it never queries the store and never calls a model.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from mac.models import utcnow


TASK_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


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
