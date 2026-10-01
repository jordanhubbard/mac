from __future__ import annotations

import pytest

from mac.task_kpis import _parse_time, derive_task_kpis, estimate_route_cost


def test_kpis_capture_quality_cycles_latency_tokens_and_known_cost() -> None:
    detail = {
        "task": {
            "id": "task_1",
            "project": "demo",
            "state": "completed",
            "attempt_count": 2,
            "created_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:02+00:00",
            "metadata": {"review_outcomes": [{"kind": "clean_window", "status": "confirmed"}]},
        },
        "reviews": [{"status": "rejected"}, {"status": "approved"}],
        "publications": [{}],
    }
    metrics = derive_task_kpis(
        detail,
        [
            {
                "id": "route_1",
                "detail": {
                    "schema": "mac.llm_route.v1",
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "duration_ms": 50,
                    "cost_usd": 0.0125,
                },
            }
        ],
    )
    assert metrics["accepted_success"] == 1.0
    assert metrics["delayed_quality_success"] == 1.0
    assert metrics["cycles_to_accept"] == 3.0
    assert metrics["lead_time_ms"] == 2000.0
    assert metrics["total_tokens"] == 125.0
    assert metrics["model_latency_ms"] == 50.0
    assert metrics["cost_known"] is True
    assert metrics["cost_usd"] == 0.0125


def test_route_cost_uses_native_models_catalog(monkeypatch) -> None:
    class CatalogModel:
        cost_input = 2.0
        cost_output = 8.0
        cost_cache_read = 0.5

        @staticmethod
        def has_cost_data() -> bool:
            return True

    from mac import models_catalog

    monkeypatch.setattr(models_catalog, "get_model_info", lambda *_args: CatalogModel())
    cost, known = estimate_route_cost(
        {
            "response_model": "openai/test-model",
            "provider": "openai",
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 100,
                "cached_tokens": 500,
            },
        }
    )
    assert known is True
    assert cost == pytest.approx(0.00205)


def test_kpi_failure_edges() -> None:
    assert _parse_time("") is None
    assert _parse_time("not-a-date") is None
    assert _parse_time("2026-01-01T00:00:00Z") is not None

    metrics = derive_task_kpis(
        {
            "task": {
                "id": "task_failed",
                "project": "demo",
                "state": "failed",
                "created_at": "bad",
                "completed_at": "also-bad",
                "metadata": {
                    "review_outcomes": [
                        {
                            "kind": "escaped_defect",
                            "status": "confirmed",
                            "severity_weight": 3,
                        }
                    ]
                },
            },
            "reviews": [None, {"status": "rejected"}],
            "publications": [None],
        },
        [
            {"id": "duplicate", "detail": {"schema": "not-a-route"}},
            {"id": "duplicate", "detail": {"schema": "mac.llm_route.v1"}},
        ],
    )
    assert metrics["quality_source"] == "escaped_defect"
    assert metrics["quality_validated"] is True
    assert metrics["accepted_success"] == 0.0
    assert metrics["escaped_defect_severity"] == 3.0
    assert metrics["lead_time_ms"] == 0.0
    assert estimate_route_cost({"input_tokens": 100}) == (0.0, False)
