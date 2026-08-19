"""Tests for the strictly read-only unified ``$MAC_HOME`` auditor.

Covers canonical-layout recognition, legacy-accepted classification (with the
canonical target named), drift on unknown top-level entries, missing-expected
reporting, missing-root handling, schema/summary shape, non-standard nested
detection, resolution through ``mac.mac_paths``, and a read-only assertion that
snapshots the fixture tree (listing + mtimes) before and after the audit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mac import mac_home_audit
from mac.mac_home_audit import (
    CANONICAL_LAYOUT,
    MAC_HOME_AUDIT_SCHEMA,
    audit_mac_home,
)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


def _snapshot(root: Path) -> dict[str, tuple[bool, int, int]]:
    """Map every path under ``root`` to (is_dir, size, mtime_ns)."""
    snap: dict[str, tuple[bool, int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + filenames:
            p = Path(dirpath) / name
            st = p.stat()
            snap[str(p)] = (p.is_dir(), st.st_size, st.st_mtime_ns)
    return snap


@pytest.fixture()
def canonical_root(tmp_path: Path) -> Path:
    root = tmp_path / ".mac"
    (root / "ledger").mkdir(parents=True)
    _touch(root / "ledger" / "mac.db")
    (root / "ledger" / "backups").mkdir()
    (root / "ledger" / "archive").mkdir()
    (root / "secrets").mkdir()
    _touch(root / "secrets" / "mac.env")
    _touch(root / "secrets" / "client-principals.json")
    (root / "fleet").mkdir()
    _touch(root / "fleet" / "fleets.yaml")
    (root / "runtime").mkdir()
    (root / "runtime" / "journal").mkdir()
    (root / "gateway").mkdir()
    (root / "gateway" / "openclaw").mkdir()
    (root / "toolchain").mkdir()
    (root / "toolchain" / "src").mkdir()
    return root


@pytest.fixture()
def legacy_root(tmp_path: Path) -> Path:
    """Today's pre-Phase-2 flat root."""
    root = tmp_path / ".mac"
    root.mkdir(parents=True)
    _touch(root / "mac.db")
    _touch(root / "mac.env")
    _touch(root / "fleets.yaml")
    (root / "openclaw").mkdir()
    (root / "journal").mkdir()
    (root / "backups").mkdir()
    return root


def test_schema_and_summary_shape(canonical_root: Path) -> None:
    report = audit_mac_home(canonical_root, now="2026-01-01T00:00:00Z")
    assert report["schema"] == MAC_HOME_AUDIT_SCHEMA == "mac.mac_home_audit.v1"
    assert report["root_path"] == str(canonical_root)
    assert report["audited_at"] == "2026-01-01T00:00:00Z"
    assert report["root_exists"] is True
    assert report["root_status"] == "ok"
    # Reserved sibling keys are present and stable.
    assert report["duplicates"] == []
    assert report["orphans"] == []
    summary = report["summary"]
    assert set(summary) == {
        "total_entries",
        "canonical",
        "legacy_accepted",
        "drift",
        "missing_expected",
        "nonstandard_nested",
    }
    assert summary["total_entries"] == len(report["entries"])


def test_canonical_layout_recognised(canonical_root: Path) -> None:
    report = audit_mac_home(canonical_root, now="2026-01-01T00:00:00Z")
    by_name = {e["name"]: e for e in report["entries"]}
    for spec in CANONICAL_LAYOUT:
        assert by_name[spec.name]["classification"] == "canonical"
        assert by_name[spec.name]["generation"] == "canonical"
    assert report["summary"]["canonical"] == len(CANONICAL_LAYOUT)
    assert report["summary"]["drift"] == 0
    assert report["summary"]["missing_expected"] == 0


def test_legacy_accepted_classification_names_target(legacy_root: Path) -> None:
    report = audit_mac_home(legacy_root, now="2026-01-01T00:00:00Z")
    by_name = {e["name"]: e for e in report["entries"]}
    assert by_name["mac.db"]["classification"] == "legacy_accepted"
    assert by_name["mac.db"]["generation"] == "legacy"
    assert by_name["mac.db"]["canonical_target"] == "ledger/mac.db"
    assert by_name["mac.env"]["canonical_target"] == "secrets/mac.env"
    assert by_name["fleets.yaml"]["canonical_target"] == "fleet/fleets.yaml"
    assert by_name["journal"]["canonical_target"] == "runtime/journal"
    assert by_name["openclaw"]["canonical_target"] == "gateway/openclaw"
    assert by_name["backups"]["canonical_target"] == "ledger/backups"
    assert report["summary"]["legacy_accepted"] == 6
    assert report["summary"]["drift"] == 0


def test_drift_on_unknown_top_level(legacy_root: Path) -> None:
    (legacy_root / "totally-unexpected").mkdir()
    _touch(legacy_root / "mystery.txt")
    report = audit_mac_home(legacy_root, now="2026-01-01T00:00:00Z")
    by_name = {e["name"]: e for e in report["entries"]}
    assert by_name["totally-unexpected"]["classification"] == "drift"
    assert by_name["totally-unexpected"]["generation"] == "unknown"
    assert by_name["mystery.txt"]["classification"] == "drift"
    assert report["summary"]["drift"] == 2


def test_nonstandard_nested_entries_flagged(canonical_root: Path) -> None:
    _touch(canonical_root / "ledger" / "surprise.log")
    report = audit_mac_home(canonical_root, now="2026-01-01T00:00:00Z")
    nested = report["nonstandard_nested"]
    assert any(
        n["container"] == "ledger" and n["name"] == "surprise.log" and n["classification"] == "drift"
        for n in nested
    )
    assert report["summary"]["nonstandard_nested"] >= 1


def test_missing_expected_path_reporting(legacy_root: Path) -> None:
    report = audit_mac_home(legacy_root, now="2026-01-01T00:00:00Z")
    missing = {m["name"]: m for m in report["missing_expected"]}
    # All six canonical buckets are absent from a flat legacy root.
    assert set(missing) == {spec.name for spec in CANONICAL_LAYOUT}
    # ledger's legacy stand-ins (mac.db, backups) are noted as present.
    assert "mac.db" in missing["ledger"]["legacy_present"]
    assert "backups" in missing["ledger"]["legacy_present"]
    assert report["summary"]["missing_expected"] == len(CANONICAL_LAYOUT)


def test_missing_root_is_reported_not_raised(tmp_path: Path) -> None:
    absent = tmp_path / "nope" / ".mac"
    report = audit_mac_home(absent, now="2026-01-01T00:00:00Z")
    assert report["root_exists"] is False
    assert report["root_status"] == "missing"
    assert report["entries"] == []
    # Every canonical bucket is reported missing.
    assert report["summary"]["missing_expected"] == len(CANONICAL_LAYOUT)


def test_root_that_is_a_file_is_reported(tmp_path: Path) -> None:
    f = tmp_path / "root-as-file"
    f.write_text("not a dir", encoding="utf-8")
    report = audit_mac_home(f, now="2026-01-01T00:00:00Z")
    assert report["root_exists"] is True
    assert report["root_status"] == "not_a_directory"
    assert report["entries"] == []


def test_resolves_through_mac_paths(canonical_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAC_HOME", str(canonical_root))
    report = audit_mac_home(now="2026-01-01T00:00:00Z")
    assert report["root_path"] == str(canonical_root)
    assert report["root_status"] == "ok"


def test_audit_is_read_only(canonical_root: Path) -> None:
    _touch(canonical_root / "ledger" / "surprise.log")
    (canonical_root / "weird-dir").mkdir()
    before = _snapshot(canonical_root)
    audit_mac_home(canonical_root, now="2026-01-01T00:00:00Z")
    after = _snapshot(canonical_root)
    assert before == after


def test_no_hermes_private_imports() -> None:
    src = Path(mac_home_audit.__file__).read_text(encoding="utf-8")
    assert "_hermes" not in src
