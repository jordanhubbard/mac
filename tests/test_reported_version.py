"""Unit tests for the reported-version → fleet-target-pin adapter."""
from __future__ import annotations

import pytest

from mac import fleet_target as ft
from mac import reported_version as rv


# ---------------------------------------------------------------------------
# Normalization: reported document -> pin-aligned fields
# ---------------------------------------------------------------------------

def test_source_only_report_normalizes():
    report = rv.ReportedVersion.from_report({"source": "0F55D49B1FAF"})
    # commit is lower-cased to match the pin's normalization
    assert report.source == "0f55d49b1faf"
    assert report.gateway_unknown is True
    assert report.to_dict() == {"source": "0f55d49b1faf", "openclaw": "unknown"}


def test_full_gateway_report_normalizes_into_pin_fields():
    report = rv.ReportedVersion.from_report(
        {
            "source": "abc1234def",
            "openclaw": {"version": "2026.6.11", "revision": "19"},
        }
    )
    assert report.source == "abc1234def"
    assert not report.gateway_unknown
    assert report.openclaw == rv.ReportedOpenClaw(version="2026.6.11", revision="19")
    assert report.to_dict()["openclaw"] == {"version": "2026.6.11", "revision": "19"}


def test_alias_field_names_are_accepted():
    report = rv.ReportedVersion.from_report(
        {
            "source_commit": "abcdef1",
            "gateway": {"gateway_version": "2026.6.11", "image_revision": "deadbee"},
        }
    )
    assert report.source == "abcdef1"
    assert report.openclaw.version == "2026.6.11"
    # A commit-hash image revision round-trips as a string (no info loss).
    assert report.openclaw.revision == "deadbee"


def test_flat_gateway_keys_when_openclaw_slot_absent():
    report = rv.ReportedVersion.from_report(
        {
            "source": "abc1234",
            "gateway_version": "2026.6.11",
            "gateway_revision": "19",
        }
    )
    assert not report.gateway_unknown
    assert report.openclaw.version == "2026.6.11"
    assert report.openclaw.revision == "19"


# ---------------------------------------------------------------------------
# Normalization: the unknown / unreported gateway case
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "doc",
    [
        {"source": "abc1234"},  # openclaw omitted entirely
        {"source": "abc1234", "openclaw": None},
        {"source": "abc1234", "openclaw": "unknown"},
        {"source": "abc1234", "openclaw": ""},
        {"source": "abc1234", "openclaw": "none"},
        {"source": "abc1234", "openclaw": "unreported"},
    ],
)
def test_missing_gateway_is_explicit_unknown_not_absent(doc):
    report = rv.ReportedVersion.from_report(doc)
    assert report.gateway_unknown is True
    assert report.openclaw is rv.UNKNOWN
    # It is *representable* (serializes to an explicit token), never dropped.
    assert report.to_dict()["openclaw"] == "unknown"


def test_nested_empty_version_and_revision_is_unknown():
    report = rv.ReportedVersion.from_report(
        {"source": "abc1234", "openclaw": {"version": "", "revision": ""}}
    )
    assert report.gateway_unknown is True


# ---------------------------------------------------------------------------
# Normalization: rejection of malformed input
# ---------------------------------------------------------------------------

def test_missing_source_is_rejected():
    with pytest.raises(rv.ReportedVersionError):
        rv.ReportedVersion.from_report({"openclaw": {"version": "1", "revision": "2"}})


def test_non_commit_source_is_rejected():
    with pytest.raises(rv.ReportedVersionError):
        rv.ReportedVersion.from_report({"source": "HEAD"})


def test_partial_gateway_report_is_rejected():
    with pytest.raises(rv.ReportedVersionError):
        rv.ReportedVersion.from_report(
            {"source": "abc1234", "openclaw": {"version": "2026.6.11"}}
        )


def test_non_object_report_is_rejected():
    with pytest.raises(rv.ReportedVersionError):
        rv.ReportedVersion.from_report(["not", "a", "mapping"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Comparison: reported vs pinned target
# ---------------------------------------------------------------------------

def _gateway_target() -> ft.RoleTarget:
    return ft.RoleTarget(
        source="0f55d49b1faf1fd2e6a884fc8b4f80f625ca7031",
        openclaw=ft.OpenClawTrack(version="2026.6.11", revision="19"),
    )


def test_matching_report_vs_pin():
    report = rv.ReportedVersion.from_report(
        {
            "source": "0f55d49b1faf1fd2e6a884fc8b4f80f625ca7031",
            "openclaw": {"version": "2026.6.11", "revision": "19"},
        }
    )
    result = rv.compare_to_target("gateway", report, _gateway_target())
    assert result.matches is True
    assert result.source_matches and result.openclaw_matches
    assert result.openclaw_unknown is False


def test_short_sha_report_matches_full_sha_pin():
    # commit-hash normalization: a short SHA report matches a full-SHA pin.
    report = rv.ReportedVersion.from_report(
        {"source": "0f55d49", "openclaw": {"version": "2026.6.11", "revision": "19"}}
    )
    result = rv.compare_to_target("gateway", report, _gateway_target())
    assert result.source_matches is True
    assert result.matches is True


def test_full_sha_report_matches_short_sha_pin():
    target = ft.RoleTarget(
        source="0f55d49",
        openclaw=ft.OpenClawTrack(version="2026.6.11", revision="19"),
    )
    report = rv.ReportedVersion.from_report(
        {
            "source": "0f55d49b1faf1fd2e6a884fc8b4f80f625ca7031",
            "openclaw": {"version": "2026.6.11", "revision": "19"},
        }
    )
    assert rv.compare_to_target("gateway", report, target).source_matches is True


def test_source_mismatch_is_detected():
    report = rv.ReportedVersion.from_report(
        {"source": "deadbeef", "openclaw": {"version": "2026.6.11", "revision": "19"}}
    )
    result = rv.compare_to_target("gateway", report, _gateway_target())
    assert result.source_matches is False
    assert result.matches is False


def test_gateway_revision_mismatch_is_detected():
    report = rv.ReportedVersion.from_report(
        {
            "source": "0f55d49b1faf1fd2e6a884fc8b4f80f625ca7031",
            "openclaw": {"version": "2026.6.11", "revision": "18"},
        }
    )
    result = rv.compare_to_target("gateway", report, _gateway_target())
    assert result.openclaw_matches is False
    assert result.openclaw_unknown is False
    assert result.matches is False


def test_unreported_gateway_against_gateway_pin_is_unknown_not_mismatch():
    # The 7-of-10 case: node should run a gateway but reported no image.
    report = rv.ReportedVersion.from_report({"source": "0f55d49"})
    result = rv.compare_to_target("gateway", report, _gateway_target())
    assert result.openclaw_unknown is True
    assert result.openclaw_matches is False
    assert result.matches is False  # unknown never counts as a match
    assert result.to_dict()["openclaw_unknown"] is True


def test_worker_only_pin_matches_worker_report():
    target = ft.RoleTarget(source="abc1234def")
    report = rv.ReportedVersion.from_report({"source": "abc1234def"})
    result = rv.compare_to_target("worker", report, target)
    assert result.matches is True
    assert result.openclaw_matches is True
    assert result.openclaw_unknown is False


def test_worker_only_pin_but_agent_reports_gateway_is_mismatch():
    target = ft.RoleTarget(source="abc1234def")
    report = rv.ReportedVersion.from_report(
        {"source": "abc1234def", "openclaw": {"version": "2026.6.11", "revision": "19"}}
    )
    result = rv.compare_to_target("worker", report, target)
    assert result.openclaw_matches is False
    assert result.matches is False


def test_comparison_to_dict_shape():
    report = rv.ReportedVersion.from_report({"source": "abc1234def"})
    target = ft.RoleTarget(source="abc1234def")
    data = rv.compare_to_target("worker", report, target).to_dict()
    assert set(data) == {
        "role",
        "matches",
        "source_matches",
        "openclaw_matches",
        "openclaw_unknown",
    }
    assert data["role"] == "worker"
