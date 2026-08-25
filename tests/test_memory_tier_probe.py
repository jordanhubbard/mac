"""Qdrant-side memory-tier probe: the three failure modes the 2026-08-21
audit found, encoded as alerts.

The audit measured the live fleet instance and found:

  mac_memory_medium   667 points, newest embedded_at 2026-07-25T20:16:47Z
  mac_memory_long       0 points, never written
  and inside mac_memory_medium, 601 points from one embedding model
  and 66 from another

None of that was visible through ``points_count``, which is all
``memory_health`` used to ask for. These tests drive the probe with a fake
transport shaped like Qdrant's REST responses, so every branch is covered
without a live instance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from mac.memory_tier_probe import (
    DEFAULT_INGESTION_MAX_AGE_HOURS,
    SCAN_PAGE_SIZE,
    evaluate_qdrant_alerts,
    parse_timestamp,
    probe_collection,
    probe_collections,
)


NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)


def _point(embedded_at: Optional[str], model: Optional[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if embedded_at is not None:
        payload["embedded_at"] = embedded_at
    if model is not None:
        payload["embedding_model"] = model
    return {"id": len(payload), "payload": payload}


class FakeQdrant:
    """Minimal stand-in for the two REST calls the probe makes."""

    def __init__(self, collections: Dict[str, Dict[str, Any]]):
        # name -> {"points": [...], "points_count": int|None, "raises": exc}
        self.collections = collections
        self.calls: List[str] = []

    def __call__(
        self, method: str, url: str, body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        self.calls.append("%s %s" % (method, url))
        name = url.split("/collections/")[1].split("/")[0]
        spec = self.collections.get(name)
        if spec is None:
            raise RuntimeError("HTTP Error 404: Not Found")
        if spec.get("raises") is not None and not url.endswith("/scroll"):
            raise spec["raises"]
        if not url.endswith("/scroll"):
            return {"result": {"points_count": spec.get("points_count")}}
        if spec.get("scroll_raises") is not None:
            raise spec["scroll_raises"]
        points = spec.get("points") or []
        offset = 0 if body is None or body.get("offset") is None else int(body["offset"])
        limit = int((body or {}).get("limit") or SCAN_PAGE_SIZE)
        page = points[offset : offset + limit]
        nxt = offset + limit
        return {
            "result": {
                "points": page,
                "next_page_offset": nxt if nxt < len(points) else None,
            }
        }


# --------------------------------------------------------------------------
# parse_timestamp
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-07-25T20:16:47Z", datetime(2026, 7, 25, 20, 16, 47, tzinfo=timezone.utc)),
        (
            "2026-07-25T20:16:47+00:00",
            datetime(2026, 7, 25, 20, 16, 47, tzinfo=timezone.utc),
        ),
        # Naive timestamps are read as UTC rather than rejected.
        ("2026-07-25T20:16:47", datetime(2026, 7, 25, 20, 16, 47, tzinfo=timezone.utc)),
    ],
)
def test_parse_timestamp_accepts_the_shapes_qdrant_payloads_carry(raw, expected):
    assert parse_timestamp(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "not-a-date", None, 17, {"a": 1}])
def test_parse_timestamp_returns_none_rather_than_raising(raw):
    """One malformed point must not blind the whole snapshot."""
    assert parse_timestamp(raw) is None


# --------------------------------------------------------------------------
# probe_collection
# --------------------------------------------------------------------------


def test_probe_reports_freshness_and_model_breakdown():
    """The audit's mac_memory_medium, reproduced in miniature."""
    fake = FakeQdrant(
        {
            "mac_memory_medium": {
                "points_count": 3,
                "points": [
                    _point("2026-07-25T20:16:47Z", "nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2"),
                    _point("2026-07-25T20:16:40Z", "nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2"),
                    _point("2026-07-20T10:00:00Z", "azure/openai/text-embedding-3-large"),
                ],
            }
        }
    )
    entry = probe_collection(
        "http://qdrant.internal:6333/",
        "mac_memory_medium",
        tier="medium",
        transport=fake,
        now=NOW,
    )
    assert entry["tier"] == "medium"
    assert entry["points_count"] == 3
    assert entry["newest_embedded_at"] == "2026-07-25T20:16:47Z"
    assert entry["payload_scanned"] == 3
    assert entry["payload_scan_truncated"] is False
    assert entry["embedding_models"] == {
        "nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2": 2,
        "azure/openai/text-embedding-3-large": 1,
    }
    # 2026-07-25T20:16:47Z -> 2026-08-21T12:00Z is ~26.7 days.
    assert entry["ingestion_age_hours"] == pytest.approx(639.72, abs=0.05)


def test_probe_skips_the_payload_scan_for_an_empty_collection():
    """mac_memory_long: zero points, so there is nothing to scroll."""
    fake = FakeQdrant({"mac_memory_long": {"points_count": 0, "points": []}})
    entry = probe_collection(
        "http://qdrant.internal:6333",
        "mac_memory_long",
        tier="long",
        transport=fake,
        now=NOW,
    )
    assert entry["points_count"] == 0
    assert entry["newest_embedded_at"] is None
    assert entry["ingestion_age_hours"] is None
    assert entry["embedding_models"] == {}
    assert not any(call.endswith("/scroll") for call in fake.calls)


def test_probe_pages_through_a_multi_page_collection():
    """next_page_offset is followed until it comes back null."""
    points = [
        _point("2026-08-21T0%d:00:00Z" % (i % 10), "model-a") for i in range(SCAN_PAGE_SIZE + 7)
    ]
    fake = FakeQdrant({"mac_memory_medium": {"points_count": len(points), "points": points}})
    entry = probe_collection(
        "http://q:6333", "mac_memory_medium", tier="medium", transport=fake, now=NOW
    )
    assert entry["payload_scanned"] == len(points)
    assert entry["payload_scan_truncated"] is False
    assert entry["embedding_models"] == {"model-a": len(points)}
    assert sum(1 for c in fake.calls if c.endswith("/scroll")) == 2


def test_probe_withholds_freshness_when_the_scan_bound_truncates():
    """Scroll is id-ordered, not embedded_at-ordered, so a truncated scan
    has not necessarily seen the newest point. Reporting the sample maximum
    would invent a stall, so the probe reports "unknown" instead."""
    points = [_point("2026-01-0%dT00:00:00Z" % (i % 9 + 1), "model-a") for i in range(50)]
    fake = FakeQdrant({"c": {"points_count": 50, "points": points}})
    entry = probe_collection(
        "http://q:6333", "c", tier="medium", transport=fake, scan_limit=10, now=NOW
    )
    assert entry["payload_scanned"] == 10
    assert entry["payload_scan_truncated"] is True
    assert entry["newest_embedded_at"] is None
    assert entry["ingestion_age_hours"] is None


def test_probe_scan_limit_reads_the_environment(monkeypatch):
    points = [_point("2026-08-21T00:00:00Z", "model-a") for _ in range(50)]
    fake = FakeQdrant({"c": {"points_count": 50, "points": points}})
    monkeypatch.setenv("MAC_MEMORY_HEALTH_SCAN_LIMIT", "5")
    entry = probe_collection("http://q:6333", "c", tier="medium", transport=fake, now=NOW)
    assert entry["payload_scanned"] == 5
    assert entry["payload_scan_truncated"] is True


def test_probe_scan_limit_ignores_a_junk_environment_value(monkeypatch):
    points = [_point("2026-08-21T00:00:00Z", "model-a") for _ in range(3)]
    fake = FakeQdrant({"c": {"points_count": 3, "points": points}})
    monkeypatch.setenv("MAC_MEMORY_HEALTH_SCAN_LIMIT", "not-a-number")
    entry = probe_collection("http://q:6333", "c", tier="medium", transport=fake, now=NOW)
    assert entry["payload_scanned"] == 3
    assert entry["payload_scan_truncated"] is False


def test_probe_reports_an_unreachable_collection_as_an_error_not_a_crash():
    fake = FakeQdrant({})
    entry = probe_collection("http://q:6333", "missing", tier="long", transport=fake)
    assert entry["tier"] == "long"
    assert "404" in entry["error"]
    assert "points_count" not in entry


def test_probe_survives_a_scroll_that_fails_after_the_count_succeeded():
    fake = FakeQdrant({"c": {"points_count": 9, "scroll_raises": RuntimeError("scroll exploded")}})
    entry = probe_collection("http://q:6333", "c", tier="medium", transport=fake)
    assert entry["points_count"] == 9
    assert entry["scan_error"] == "scroll exploded"
    assert entry["newest_embedded_at"] is None


def test_probe_tolerates_points_with_missing_or_malformed_payload_fields():
    fake = FakeQdrant(
        {
            "c": {
                "points_count": 4,
                "points": [
                    _point("2026-08-21T11:00:00Z", "model-a"),
                    _point(None, "model-a"),
                    _point("garbage", None),
                    {"id": 99},  # no payload key at all
                ],
            }
        }
    )
    entry = probe_collection("http://q:6333", "c", tier="medium", transport=fake, now=NOW)
    assert entry["payload_scanned"] == 4
    assert entry["embedding_models"] == {"model-a": 2}
    assert entry["newest_embedded_at"] == "2026-08-21T11:00:00Z"
    assert entry["ingestion_age_hours"] == pytest.approx(1.0, abs=0.01)


def test_probe_collections_keys_by_collection_name_and_carries_the_tier():
    fake = FakeQdrant(
        {
            "mac_memory_medium": {"points_count": 1, "points": [_point(None, None)]},
            "mac_memory_long": {"points_count": 0, "points": []},
        }
    )
    out = probe_collections(
        "http://q:6333",
        {"medium": "mac_memory_medium", "long": "mac_memory_long"},
        transport=fake,
        now=NOW,
    )
    assert set(out) == {"mac_memory_medium", "mac_memory_long"}
    assert out["mac_memory_long"]["tier"] == "long"


# --------------------------------------------------------------------------
# evaluate_qdrant_alerts
# --------------------------------------------------------------------------


def _codes(alerts):
    return [a["code"] for a in alerts]


def test_stalled_ingestion_fires_on_the_audits_27_day_gap():
    alerts = evaluate_qdrant_alerts(
        {
            "mac_memory_medium": {
                "tier": "medium",
                "points_count": 667,
                "newest_embedded_at": "2026-07-25T20:16:47Z",
                "ingestion_age_hours": 639.7,
                "embedding_models": {"model-a": 667},
                "payload_scan_truncated": False,
            }
        }
    )
    assert _codes(alerts) == ["stalled_vector_ingestion"]
    alert = alerts[0]
    assert alert["severity"] == "critical"
    assert alert["collection"] == "mac_memory_medium"
    assert "2026-07-25T20:16:47Z" in alert["message"]
    assert "stopped, not slow" in alert["message"]


def test_fresh_ingestion_raises_nothing():
    alerts = evaluate_qdrant_alerts(
        {
            "mac_memory_medium": {
                "tier": "medium",
                "points_count": 667,
                "ingestion_age_hours": 0.5,
                "newest_embedded_at": "2026-08-21T11:30:00Z",
                "embedding_models": {"model-a": 667},
                "payload_scan_truncated": False,
            }
        }
    )
    assert alerts == []


def test_stalled_ingestion_threshold_is_tunable():
    entry = {
        "mac_memory_medium": {
            "tier": "medium",
            "points_count": 10,
            "ingestion_age_hours": 6.0,
            "newest_embedded_at": "2026-08-21T06:00:00Z",
            "embedding_models": {"model-a": 10},
            "payload_scan_truncated": False,
        }
    }
    assert evaluate_qdrant_alerts(entry) == []  # 6h < 24h default
    assert _codes(evaluate_qdrant_alerts(entry, ingestion_max_age_hours=4.0)) == [
        "stalled_vector_ingestion"
    ]


def test_truncated_scan_downgrades_to_an_explicit_unknown():
    """Better a warning that names the limitation than a critical the
    operator cannot trust."""
    alerts = evaluate_qdrant_alerts(
        {
            "big": {
                "tier": "medium",
                "points_count": 5_000_000,
                "ingestion_age_hours": None,
                "newest_embedded_at": None,
                "embedding_models": {"model-a": 20_000},
                "payload_scanned": 20_000,
                "payload_scan_truncated": True,
            }
        }
    )
    assert _codes(alerts) == ["vector_ingestion_age_unknown"]
    assert alerts[0]["severity"] == "warning"
    assert "MAC_MEMORY_HEALTH_SCAN_LIMIT" in alerts[0]["message"]


def test_unwritten_tier_fires_when_a_sibling_tier_is_populated():
    """mac_memory_long has never received a point while medium has 667."""
    alerts = evaluate_qdrant_alerts(
        {
            "mac_memory_medium": {
                "tier": "medium",
                "points_count": 667,
                "ingestion_age_hours": 0.1,
                "embedding_models": {"model-a": 667},
                "payload_scan_truncated": False,
            },
            "mac_memory_long": {
                "tier": "long",
                "points_count": 0,
                "ingestion_age_hours": None,
                "embedding_models": {},
                "payload_scan_truncated": False,
            },
        }
    )
    assert _codes(alerts) == ["unwritten_memory_tier"]
    assert alerts[0]["tier"] == "long"
    assert "mac_memory_medium" in alerts[0]["message"]


def test_unwritten_tier_stays_quiet_when_no_tier_has_been_written():
    """A brand-new install with two empty collections is not the same
    defect as a tier nothing promotes into."""
    alerts = evaluate_qdrant_alerts(
        {
            "mac_memory_medium": {"tier": "medium", "points_count": 0},
            "mac_memory_long": {"tier": "long", "points_count": 0},
        }
    )
    assert alerts == []


def test_mixed_embedding_spaces_fires_and_names_both_models():
    alerts = evaluate_qdrant_alerts(
        {
            "mac_memory_medium": {
                "tier": "medium",
                "points_count": 667,
                "ingestion_age_hours": 0.1,
                "payload_scan_truncated": False,
                "embedding_models": {
                    "nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2": 601,
                    "azure/openai/text-embedding-3-large": 66,
                },
            }
        }
    )
    assert _codes(alerts) == ["mixed_embedding_spaces"]
    message = alerts[0]["message"]
    # Ordered by descending count so the dominant space reads first.
    assert message.index("llama-3.2") < message.index("text-embedding-3-large")
    assert "=601" in message and "=66" in message
    assert alerts[0]["embedding_models"]["azure/openai/text-embedding-3-large"] == 66


def test_mixed_embedding_spaces_still_fires_on_a_truncated_scan():
    """Seeing two models in a sample is positive evidence of mixing; only
    the single-model conclusion needs a complete scan."""
    alerts = evaluate_qdrant_alerts(
        {
            "big": {
                "tier": "medium",
                "points_count": 5_000_000,
                "ingestion_age_hours": None,
                "payload_scanned": 20_000,
                "payload_scan_truncated": True,
                "embedding_models": {"model-a": 19_000, "model-b": 1_000},
            }
        }
    )
    assert "mixed_embedding_spaces" in _codes(alerts)


def test_errored_collection_contributes_no_alerts():
    """An unreachable collection is unknown, not broken; claiming otherwise
    would page an operator over a network blip."""
    alerts = evaluate_qdrant_alerts(
        {
            "mac_memory_medium": {"tier": "medium", "error": "timed out"},
            "mac_memory_long": {"tier": "long", "error": "timed out"},
        }
    )
    assert alerts == []


def test_all_three_failure_modes_report_together():
    """The live instance on 2026-08-21, end to end."""
    alerts = evaluate_qdrant_alerts(
        {
            "mac_memory_medium": {
                "tier": "medium",
                "points_count": 667,
                "newest_embedded_at": "2026-07-25T20:16:47Z",
                "ingestion_age_hours": 639.7,
                "payload_scan_truncated": False,
                "embedding_models": {
                    "nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2": 601,
                    "azure/openai/text-embedding-3-large": 66,
                },
            },
            "mac_memory_long": {
                "tier": "long",
                "points_count": 0,
                "ingestion_age_hours": None,
                "payload_scan_truncated": False,
                "embedding_models": {},
            },
        }
    )
    assert set(_codes(alerts)) == {
        "stalled_vector_ingestion",
        "unwritten_memory_tier",
        "mixed_embedding_spaces",
    }
    assert all(a["severity"] == "critical" for a in alerts)


def test_default_threshold_is_a_day():
    """A pipeline silent for a day is stopped. The audit found 27 days."""
    assert DEFAULT_INGESTION_MAX_AGE_HOURS == 24.0


def test_probe_and_evaluate_compose_end_to_end():
    """No hand-built entries: scrape a fake Qdrant, then alert on it."""
    stale = (NOW - timedelta(days=27)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake = FakeQdrant(
        {
            "mac_memory_medium": {
                "points_count": 2,
                "points": [_point(stale, "model-a"), _point(stale, "model-b")],
            },
            "mac_memory_long": {"points_count": 0, "points": []},
        }
    )
    collections = probe_collections(
        "http://q:6333",
        {"medium": "mac_memory_medium", "long": "mac_memory_long"},
        transport=fake,
        now=NOW,
    )
    assert set(_codes(evaluate_qdrant_alerts(collections))) == {
        "stalled_vector_ingestion",
        "unwritten_memory_tier",
        "mixed_embedding_spaces",
    }
