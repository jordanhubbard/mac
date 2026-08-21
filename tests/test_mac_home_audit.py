"""The unified-home auditor must describe a mid-migration tree honestly.

Two properties carry the weight here and both are asserted directly rather than
inferred: the auditor is **read-only** (a byte-for-byte listing + mtime snapshot
survives an audit), and it is **total** (a missing, unreadable, or not-a-directory
root produces a well-formed report instead of an exception). Everything else is
classification: canonical / legacy_accepted / drift, against the declarative
spec in ``mac.mac_home_audit.CANONICAL_LAYOUT`` rather than against a duplicated
list of names, so a spec change moves the tests with it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mac import mac_home_audit
from mac.mac_home_audit import (
    CANONICAL,
    CANONICAL_LAYOUT,
    DRIFT,
    LEGACY_ACCEPTED,
    LEGACY_ROOT_TARGETS,
    MAC_HOME_AUDIT_SCHEMA,
    audit_mac_home,
    canonical_bucket_names,
    classify_root_name,
    gateway_layout_position,
)

_HOME_ENV = ["MAC_HOME", "HERMES_HOME", "MAC_OPENCLAW_HOST_DIR"]


@pytest.fixture()
def clean_home(tmp_path, monkeypatch):
    for var in _HOME_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _entry(report, relative_path):
    for entry in report["entries"]:
        if entry["relative_path"] == relative_path:
            return entry
    raise AssertionError(f"{relative_path!r} not in report: {report['by_classification']}")


def _build(root: Path, *relative_paths: str, dirs: tuple[str, ...] = ()) -> Path:
    """Materialise a fixture tree. Directories listed in ``dirs`` are made empty."""
    root.mkdir(parents=True, exist_ok=True)
    for rel in dirs:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel in relative_paths:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    return root


def _snapshot(root: Path) -> list[tuple[str, bool, int]]:
    """Every path under ``root`` with its kind and mtime_ns, for read-only proof."""
    snap = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            path = Path(dirpath) / name
            stat = os.stat(path, follow_symlinks=False)
            snap.append((str(path.relative_to(root)), path.is_dir(), stat.st_mtime_ns))
    return sorted(snap)


# --- the spec itself -------------------------------------------------------


def test_the_canonical_layout_names_the_six_approved_buckets():
    # docs/home-consolidation.md §4.
    assert canonical_bucket_names() == (
        "ledger",
        "secrets",
        "fleet",
        "runtime",
        "gateway",
        "toolchain",
    )
    assert set(CANONICAL_LAYOUT) == set(canonical_bucket_names())
    for name, spec in CANONICAL_LAYOUT.items():
        assert spec.name == name
        assert spec.entries, f"{name} enumerates no expected paths"
        assert set(spec.legacy_at_root) <= set(spec.entries)


def test_every_accepted_legacy_root_name_maps_to_exactly_one_bucket():
    # The root is flat, so two buckets claiming the same pre-migration name
    # would make classification ambiguous and silently lose one of them.
    claimed = [entry for spec in CANONICAL_LAYOUT.values() for entry in spec.legacy_at_root]
    assert len(claimed) == len(set(claimed))
    assert set(LEGACY_ROOT_TARGETS) == set(claimed)
    for name, target in LEGACY_ROOT_TARGETS.items():
        assert target.endswith(f"/{name}")


def test_the_gateway_bucket_only_accepts_openclaw_at_the_mac_root():
    # SOUL.md et al. live in the *gateway* home, a different legacy tree; if the
    # gateway bucket accepted them at the MAC root, a stray SOUL.md there would
    # be reported as an expected migration source instead of as drift.
    assert CANONICAL_LAYOUT["gateway"].legacy_at_root == ("openclaw",)
    assert classify_root_name("SOUL.md")[0] == DRIFT


@pytest.mark.parametrize(
    "name,expected,bucket,target",
    [
        ("ledger", CANONICAL, "ledger", None),
        ("mac.db", LEGACY_ACCEPTED, "ledger", "ledger/mac.db"),
        ("fleets.yaml", LEGACY_ACCEPTED, "fleet", "fleet/fleets.yaml"),
        ("openclaw", LEGACY_ACCEPTED, "gateway", "gateway/openclaw"),
        ("qdrant", DRIFT, None, None),
    ],
)
def test_classify_root_name_reports_the_canonical_target(name, expected, bucket, target):
    assert classify_root_name(name) == (expected, bucket, target)


# --- canonical layout ------------------------------------------------------


def test_a_fully_canonical_tree_reports_no_drift_and_nothing_missing(tmp_path):
    root = tmp_path / "unified"
    root.mkdir()
    for spec in CANONICAL_LAYOUT.values():
        for entry in spec.entries:
            (root / spec.name / entry).parent.mkdir(parents=True, exist_ok=True)
            (root / spec.name / entry).write_text("x", encoding="utf-8")

    report = audit_mac_home(root)

    assert report["status"] == "ok"
    assert report["layout_generation"] == "unified"
    assert report["by_classification"][DRIFT] == []
    assert report["missing_expected"] == []
    assert sorted(report["by_classification"][CANONICAL]) == sorted(
        list(canonical_bucket_names())
        + [f"{spec.name}/{entry}" for spec in CANONICAL_LAYOUT.values() for entry in spec.entries]
    )
    assert all(e["generation"] == "unified" for e in report["entries"])


def test_a_non_standard_entry_inside_a_canonical_bucket_is_drift(tmp_path):
    root = _build(tmp_path / "unified", "ledger/mac.db", "ledger/stray-notes.txt")

    report = audit_mac_home(root)

    assert _entry(report, "ledger/mac.db")["classification"] == CANONICAL
    stray = _entry(report, "ledger/stray-notes.txt")
    assert stray["classification"] == DRIFT
    assert stray["depth"] == 2
    assert stray["bucket"] == "ledger"


# --- legacy (pre-Phase-2) layout -------------------------------------------


def test_the_current_pre_migration_tree_is_accepted_not_flagged(tmp_path):
    # The shape every host actually has today: data directly at the root.
    root = _build(
        tmp_path / "dot-mac",
        "mac.db",
        "mac.env",
        "fleets.yaml",
        dirs=("openclaw", "journal", "backups"),
    )

    report = audit_mac_home(root)

    assert report["status"] == "ok"
    assert report["layout_generation"] == "pre_migration"
    assert report["by_classification"][DRIFT] == []
    assert sorted(report["by_classification"][LEGACY_ACCEPTED]) == [
        "backups",
        "fleets.yaml",
        "journal",
        "mac.db",
        "mac.env",
        "openclaw",
    ]
    entry = _entry(report, "mac.db")
    assert entry["canonical_target"] == "ledger/mac.db"
    assert entry["bucket"] == "ledger"
    assert entry["generation"] == "pre_migration"
    assert entry["kind"] == "file"
    assert _entry(report, "journal")["kind"] == "dir"


def test_a_legacy_entry_satisfies_its_expectation_so_it_is_not_missing(tmp_path):
    root = _build(tmp_path / "dot-mac", "mac.db")

    report = audit_mac_home(root)

    expectation = next(e for e in report["expectations"] if e["canonical_path"] == "ledger/mac.db")
    assert expectation["status"] == LEGACY_ACCEPTED
    assert expectation["legacy_path"] == "mac.db"
    assert "ledger/mac.db" not in report["missing_expected"]


def test_a_half_migrated_tree_is_reported_as_mixed(tmp_path):
    root = _build(tmp_path / "dot-mac", "ledger/mac.db", "fleets.yaml")

    report = audit_mac_home(root)

    assert report["layout_generation"] == "mixed"
    assert _entry(report, "ledger")["classification"] == CANONICAL
    assert _entry(report, "fleets.yaml")["classification"] == LEGACY_ACCEPTED


def test_an_empty_root_has_an_unknown_generation(tmp_path):
    root = tmp_path / "dot-mac"
    root.mkdir()

    report = audit_mac_home(root)

    assert report["status"] == "ok"
    assert report["layout_generation"] == "unknown"
    assert report["entries"] == []


# --- drift -----------------------------------------------------------------


def test_an_unknown_top_level_entry_is_drift_with_no_bucket(tmp_path):
    root = _build(tmp_path / "dot-mac", "mac.db", "mystery.sqlite", dirs=("leftover-2024",))

    report = audit_mac_home(root)

    assert sorted(report["by_classification"][DRIFT]) == ["leftover-2024", "mystery.sqlite"]
    drifted = _entry(report, "mystery.sqlite")
    assert drifted["bucket"] is None
    assert drifted["canonical_target"] is None
    assert drifted["generation"] == "unknown"
    assert report["summary"]["drift_count"] == 2


def test_a_symlink_is_described_without_being_followed(tmp_path):
    root = _build(tmp_path / "dot-mac", "mac.db")
    (root / "compat-link").symlink_to(tmp_path / "nowhere")

    report = audit_mac_home(root)

    entry = _entry(report, "compat-link")
    assert entry["kind"] == "symlink"
    assert entry["classification"] == DRIFT


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFO support")
def test_a_non_file_non_directory_entry_is_described_as_other(tmp_path):
    # A stray socket/FIFO in the home is exactly the abandoned-metadata shape
    # this auditor exists to surface; it must be listed, not crash the walk.
    root = _build(tmp_path / "dot-mac", "mac.db")
    os.mkfifo(root / "stale.sock")

    report = audit_mac_home(root)

    assert _entry(report, "stale.sock")["kind"] == "other"


# --- missing expected paths ------------------------------------------------


def test_a_path_absent_from_both_locations_is_reported_missing(tmp_path):
    root = _build(tmp_path / "dot-mac", "mac.db")

    report = audit_mac_home(root)

    assert "ledger/mac.db" not in report["missing_expected"]
    assert "secrets/mac.env" in report["missing_expected"]
    assert "toolchain/venv" in report["missing_expected"]
    assert report["summary"]["missing_expected_count"] == len(report["missing_expected"])
    # Every enumerated canonical path is accounted for exactly once.
    assert len(report["expectations"]) == sum(
        len(spec.entries) for spec in CANONICAL_LAYOUT.values()
    )


def test_a_gateway_only_path_has_no_legacy_root_alternative(tmp_path):
    root = _build(tmp_path / "dot-mac", "SOUL.md")

    report = audit_mac_home(root)

    expectation = next(e for e in report["expectations"] if e["canonical_path"] == "gateway/SOUL.md")
    assert expectation["legacy_path"] is None
    assert expectation["status"] == "missing"
    # ...and the stray copy at the MAC root is drift, not a migration source.
    assert _entry(report, "SOUL.md")["classification"] == DRIFT


# --- hostile roots: reported, never raised ---------------------------------


def test_a_missing_root_is_reported_in_status(tmp_path):
    report = audit_mac_home(tmp_path / "does-not-exist")

    assert report["status"] == "missing_root"
    assert report["root_exists"] is False
    assert report["entries"] == []
    assert report["schema"] == MAC_HOME_AUDIT_SCHEMA
    # Shape is identical to a successful audit, so consumers need no branch.
    assert len(report["missing_expected"]) == len(report["expectations"])


def test_a_root_that_is_a_file_is_reported_not_raised(tmp_path):
    root = tmp_path / "dot-mac"
    root.write_text("not a directory", encoding="utf-8")

    report = audit_mac_home(root)

    assert report["status"] == "root_not_a_directory"
    assert report["root_exists"] is True
    assert report["entries"] == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_an_unreadable_root_is_reported_with_detail(tmp_path):
    root = _build(tmp_path / "dot-mac", "mac.db")
    os.chmod(root, 0o000)
    try:
        report = audit_mac_home(root)
    finally:
        os.chmod(root, 0o700)

    assert report["status"] == "unreadable_root"
    assert report["root_exists"] is True
    assert "PermissionError" in report["status_detail"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_an_unreadable_bucket_is_recorded_without_failing_the_audit(tmp_path):
    root = _build(tmp_path / "dot-mac", "ledger/mac.db", "fleets.yaml")
    os.chmod(root / "ledger", 0o000)
    try:
        report = audit_mac_home(root)
    finally:
        os.chmod(root / "ledger", 0o700)

    assert report["status"] == "ok"
    assert [err["path"] for err in report["read_errors"]] == ["ledger"]
    assert report["summary"]["read_error_count"] == 1
    # The rest of the tree was still audited.
    assert _entry(report, "fleets.yaml")["classification"] == LEGACY_ACCEPTED


# --- report shape ----------------------------------------------------------


def test_the_report_has_the_v1_schema_and_a_complete_summary(tmp_path):
    root = _build(tmp_path / "dot-mac", "mac.db", "mystery.bin")

    report = audit_mac_home(root)

    assert report["schema"] == MAC_HOME_AUDIT_SCHEMA == "mac.mac_home_audit.v1"
    assert report["root_path"] == str(root)
    assert report["audited_at"].startswith("20") and "T" in report["audited_at"]
    assert report["audited_at"].endswith("+00:00")
    assert set(report) == {
        "schema",
        "root_path",
        "audited_at",
        "root_exists",
        "status",
        "status_detail",
        "layout_generation",
        "entries",
        "by_classification",
        "expectations",
        "missing_expected",
        "read_errors",
        "duplicates",
        "orphans",
        "summary",
    }
    assert report["summary"] == {
        "entry_count": 2,
        "top_level_count": 2,
        "canonical_count": 0,
        "legacy_accepted_count": 1,
        "drift_count": 1,
        "missing_expected_count": len(report["missing_expected"]),
        "read_error_count": 0,
        "duplicate_count": 0,
        "orphan_count": 0,
    }


def test_duplicate_and_orphan_keys_are_reserved_and_empty(tmp_path):
    # The sibling detectors populate these; the shape is pinned from v1 so they
    # can land without a schema bump.
    report = audit_mac_home(_build(tmp_path / "dot-mac", "mac.db"))

    assert report["duplicates"] == []
    assert report["orphans"] == []


def test_the_report_is_json_serialisable(tmp_path):
    import json

    report = audit_mac_home(_build(tmp_path / "dot-mac", "mac.db", "ledger/backups/x"))

    assert json.loads(json.dumps(report)) == report


# --- read-only guarantee ---------------------------------------------------


def test_the_audit_does_not_touch_the_tree(tmp_path):
    root = _build(
        tmp_path / "dot-mac",
        "mac.db",
        "mac.env",
        "ledger/backups/old.db",
        "mystery.bin",
        dirs=("journal", "openclaw", "secrets"),
    )
    before = _snapshot(root)

    audit_mac_home(root)
    audit_mac_home(root)

    assert _snapshot(root) == before


def test_the_audit_creates_nothing_under_a_missing_root(tmp_path):
    root = tmp_path / "absent"

    audit_mac_home(root)

    assert not root.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_module_never_imports_the_vendored_hermes_runtime():
    source = Path(mac_home_audit.__file__).read_text(encoding="utf-8")

    assert "_hermes" not in source
    # Read-only means no write verbs anywhere in the module.
    for verb in ("mkdir(", "write_text(", "write_bytes(", "os.remove", "shutil.", "chmod("):
        assert verb not in source, f"{verb} is a write; this module must be read-only"


# --- resolution goes through mac_paths -------------------------------------


def test_the_default_root_is_the_resolved_mac_home(clean_home, monkeypatch):
    relocated = _build(clean_home / "relocated", "mac.db")
    monkeypatch.setenv("MAC_HOME", str(relocated))

    report = audit_mac_home()

    assert report["root_path"] == str(relocated)
    assert _entry(report, "mac.db")["canonical_target"] == "ledger/mac.db"


def test_a_string_root_is_accepted(tmp_path):
    root = _build(tmp_path / "dot-mac", "mac.db")

    assert audit_mac_home(str(root))["status"] == "ok"


def test_gateway_position_is_inside_the_root_by_default(clean_home, monkeypatch):
    root = clean_home / "dot-mac"
    monkeypatch.setenv("MAC_HOME", str(root))
    (root / "openclaw").mkdir(parents=True)

    position = gateway_layout_position()

    assert position["root_path"] == str(root)
    assert position["gateway"]["inside_root"] is True
    assert position["gateway"]["relative_path"] == "openclaw"
    assert position["gateway"]["exists"] is True
    assert position["openclaw"]["relative_path"] == "openclaw"


def test_an_un_migrated_gateway_home_is_reported_as_outside_the_root(clean_home, monkeypatch):
    monkeypatch.setenv("MAC_HOME", str(clean_home / "dot-mac"))
    monkeypatch.setenv("HERMES_HOME", str(clean_home / "elsewhere"))

    position = gateway_layout_position()

    assert position["gateway"]["inside_root"] is False
    assert position["gateway"]["relative_path"] is None
    assert position["gateway"]["exists"] is False
