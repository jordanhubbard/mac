"""Contract tests for the read-only unified-home auditor.

The audit runs against real fixture trees rather than mocks, because the one
property that matters most -- that it touches nothing -- is only provable
against a real filesystem. ``test_the_audit_writes_nothing`` snapshots the tree
(listing plus mtimes plus sizes) before and after and asserts it is unchanged.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mac.mac_home_audit import (
    CANONICAL_BUCKETS,
    CLASSIFICATION_CANONICAL,
    CLASSIFICATION_DRIFT,
    CLASSIFICATION_LEGACY,
    GENERATION_MIXED,
    GENERATION_PRE_PHASE2,
    GENERATION_UNIFIED,
    GENERATION_UNKNOWN,
    LEGACY_TOP_LEVEL,
    MAC_HOME_AUDIT_SCHEMA,
    audit_mac_home,
)

# --- fixture builders -------------------------------------------------------


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


def _canonical_root(tmp_path: Path) -> Path:
    """The approved §4 layout, fully populated."""
    root = tmp_path / "unified"
    for bucket in CANONICAL_BUCKETS:
        (root / bucket.name).mkdir(parents=True, exist_ok=True)
        for expected in bucket.expected:
            if expected.kind == "dir":
                (root / bucket.name / expected.name).mkdir(parents=True, exist_ok=True)
            else:
                _touch(root / bucket.name / expected.name)
    return root


def _legacy_root(tmp_path: Path) -> Path:
    """Today's pre-Phase-2 shape: the flat root the fleet actually runs on."""
    root = tmp_path / "legacy"
    root.mkdir()
    for name in ("mac.db", "mac.env", "fleets.yaml", "client-principals.json", ".env"):
        _touch(root / name)
    for name in ("openclaw", "journal", "backups", "archive", "bin", "venv", "src"):
        (root / name).mkdir()
    return root


def _entry(report: dict, path: str) -> dict:
    matches = [item for item in report["entries"] if item["path"] == path]
    assert matches, "no entry for %s in %s" % (path, [i["path"] for i in report["entries"]])
    return matches[0]


def _snapshot(root: Path) -> list[tuple[str, int, int]]:
    """Path, mtime_ns and size for every node under ``root`` (symlinks not followed)."""
    snapshot = []
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        snapshot.append((str(path.relative_to(root)), stat.st_mtime_ns, stat.st_size))
    return snapshot


# --- canonical layout -------------------------------------------------------


def test_a_fully_canonical_root_reports_no_drift_and_nothing_missing(tmp_path):
    report = audit_mac_home(_canonical_root(tmp_path))

    assert report["status"] == {"state": "ok", "detail": None}
    assert report["root_exists"] is True
    assert report["drift"] == []
    assert report["missing_expected"] == []
    assert report["layout"]["generation_detected"] == GENERATION_UNIFIED
    assert report["layout"]["buckets_present"] == [b.name for b in CANONICAL_BUCKETS]


def test_every_bucket_is_recognised_as_canonical(tmp_path):
    report = audit_mac_home(_canonical_root(tmp_path))

    for bucket in CANONICAL_BUCKETS:
        entry = _entry(report, bucket.name)
        assert entry["classification"] == CLASSIFICATION_CANONICAL
        assert entry["generation"] == GENERATION_UNIFIED
        assert entry["bucket"] == bucket.name
        assert entry["canonical_target"] is None


def test_expected_paths_inside_a_bucket_are_canonical(tmp_path):
    report = audit_mac_home(_canonical_root(tmp_path))

    ledger = _entry(report, "ledger/mac.db")
    assert ledger["classification"] == CLASSIFICATION_CANONICAL
    assert ledger["bucket"] == "ledger"
    assert ledger["kind"] == "file"
    assert _entry(report, "runtime/journal")["kind"] == "dir"


def test_gateway_owns_openclaw_in_the_target_layout(tmp_path):
    report = audit_mac_home(_canonical_root(tmp_path))

    assert _entry(report, "gateway/openclaw")["classification"] == CLASSIFICATION_CANONICAL


def test_a_known_but_unrequired_gateway_file_is_not_drift(tmp_path):
    root = _canonical_root(tmp_path)
    _touch(root / "gateway" / "SOUL.md")

    report = audit_mac_home(root)

    assert _entry(report, "gateway/SOUL.md")["classification"] == CLASSIFICATION_CANONICAL
    assert report["drift"] == []


# --- legacy (pre-Phase-2) layout -------------------------------------------


def test_the_current_flat_root_is_accepted_not_drift(tmp_path):
    report = audit_mac_home(_legacy_root(tmp_path))

    assert report["drift"] == []
    assert report["layout"]["generation_detected"] == GENERATION_PRE_PHASE2
    assert set(report["legacy_accepted"]) == {
        "mac.db",
        "mac.env",
        "fleets.yaml",
        "client-principals.json",
        ".env",
        "openclaw",
        "journal",
        "backups",
        "archive",
        "bin",
        "venv",
        "src",
    }


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("mac.db", "ledger/mac.db"),
        ("backups", "ledger/backups"),
        ("mac.env", "secrets/mac.env"),
        ("fleets.yaml", "fleet/fleets.yaml"),
        ("journal", "runtime/journal"),
        ("openclaw", "gateway/openclaw"),
        ("src", "toolchain/src"),
    ],
)
def test_a_legacy_entry_names_its_canonical_target(tmp_path, name, target):
    entry = _entry(audit_mac_home(_legacy_root(tmp_path)), name)

    assert entry["classification"] == CLASSIFICATION_LEGACY
    assert entry["generation"] == GENERATION_PRE_PHASE2
    assert entry["canonical_target"] == target
    assert entry["bucket"] == target.split("/")[0]


def test_a_recognised_entry_the_plan_has_not_placed_is_reported_as_such(tmp_path):
    root = _legacy_root(tmp_path)
    (root / "qdrant").mkdir()

    report = audit_mac_home(root)
    entry = _entry(report, "qdrant")

    assert entry["classification"] == CLASSIFICATION_LEGACY
    assert entry["canonical_target"] is None
    assert entry["bucket"] is None
    assert report["legacy_without_canonical_target"] == ["qdrant"]
    assert report["summary"]["legacy_without_canonical_target"] == 1


def test_a_half_migrated_root_is_reported_as_mixed(tmp_path):
    root = _legacy_root(tmp_path)
    (root / "ledger").mkdir()
    _touch(root / "ledger" / "mac.db")

    report = audit_mac_home(root)

    assert report["layout"]["generation_detected"] == GENERATION_MIXED
    assert _entry(report, "ledger")["classification"] == CLASSIFICATION_CANONICAL
    assert _entry(report, "mac.db")["classification"] == CLASSIFICATION_LEGACY


# --- drift ------------------------------------------------------------------


def test_an_unknown_top_level_entry_is_drift(tmp_path):
    root = _legacy_root(tmp_path)
    (root / "stray-thing").mkdir()
    _touch(root / "leftover.tmp")

    report = audit_mac_home(root)

    assert set(report["drift"]) == {"stray-thing", "leftover.tmp"}
    assert _entry(report, "leftover.tmp")["generation"] == GENERATION_UNKNOWN
    assert "not a §4 bucket" in _entry(report, "stray-thing")["detail"]


def test_an_unknown_entry_inside_a_canonical_bucket_is_drift(tmp_path):
    root = _canonical_root(tmp_path)
    _touch(root / "ledger" / "mac.db.bak")

    report = audit_mac_home(root)

    assert report["drift"] == ["ledger/mac.db.bak"]
    entry = _entry(report, "ledger/mac.db.bak")
    assert entry["bucket"] == "ledger"
    assert entry["detail"] == "not named by §4 for this bucket"


def test_drift_is_only_detected_one_level_deep(tmp_path):
    root = _canonical_root(tmp_path)
    _touch(root / "ledger" / "backups" / "whatever-2026-01-01.sql")

    assert audit_mac_home(root)["drift"] == []


def test_a_legacy_directory_is_not_descended_into(tmp_path):
    root = _legacy_root(tmp_path)
    _touch(root / "journal" / "not-a-bucket-member")

    report = audit_mac_home(root)

    assert report["drift"] == []
    assert [e["path"] for e in report["entries"] if e["path"].startswith("journal/")] == []


# --- missing expected paths -------------------------------------------------


def test_missing_canonical_paths_name_the_legacy_location_that_covers_them(tmp_path):
    report = audit_mac_home(_legacy_root(tmp_path))

    missing = {item["path"]: item for item in report["missing_expected"]}
    assert missing["ledger/mac.db"]["satisfied_by_legacy"] == "mac.db"
    assert missing["gateway/openclaw"]["satisfied_by_legacy"] == "openclaw"
    assert missing["fleet/specs"]["satisfied_by_legacy"] is None
    assert missing["ledger/mac.db"]["bucket"] == "ledger"
    assert missing["ledger/mac.db"]["kind"] == "file"


def test_an_empty_root_is_audited_without_error(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()

    report = audit_mac_home(root)

    expected_total = sum(len(bucket.expected) for bucket in CANONICAL_BUCKETS)
    assert report["entries"] == []
    assert report["layout"]["generation_detected"] == GENERATION_UNKNOWN
    assert report["summary"]["missing_expected"] == expected_total
    assert report["status"]["state"] == "ok"


def test_a_partially_populated_bucket_reports_only_what_is_absent(tmp_path):
    root = tmp_path / "partial"
    (root / "ledger").mkdir(parents=True)
    _touch(root / "ledger" / "mac.db")

    missing = {item["path"] for item in audit_mac_home(root)["missing_expected"]}

    assert "ledger/mac.db" not in missing
    assert {"ledger/backups", "ledger/archive"} <= missing


# --- hostile / degenerate roots --------------------------------------------


def test_a_missing_root_is_reported_never_raised(tmp_path):
    report = audit_mac_home(tmp_path / "does-not-exist")

    assert report["root_exists"] is False
    assert report["status"] == {"state": "missing", "detail": "root does not exist"}
    assert report["entries"] == []
    assert report["missing_expected"] == []
    assert report["summary"]["entries"] == 0


def test_a_root_that_is_a_file_is_reported_not_a_directory(tmp_path):
    root = tmp_path / "not-a-dir"
    _touch(root)

    report = audit_mac_home(root)

    assert report["status"]["state"] == "not_a_directory"
    assert report["root_exists"] is True
    assert report["entries"] == []


def test_an_unreadable_root_is_reported_never_raised(tmp_path, monkeypatch):
    root = tmp_path / "sealed"
    root.mkdir()
    _fail(monkeypatch, "iterdir", "sealed")

    report = audit_mac_home(root)

    assert report["status"]["state"] == "unreadable"
    assert report["status"]["detail"] == "Permission denied"
    assert report["entries"] == []


def test_an_unreadable_bucket_is_reported_on_its_own_entry(tmp_path, monkeypatch):
    root = _canonical_root(tmp_path)
    _fail(monkeypatch, "iterdir", "secrets")

    report = audit_mac_home(root)

    assert _entry(report, "secrets")["detail"] == "bucket is unreadable: Permission denied"
    assert [e for e in report["entries"] if e["path"].startswith("secrets/")] == []


def test_a_symlinked_bucket_is_reported_but_never_followed(tmp_path):
    root = _canonical_root(tmp_path)
    elsewhere = tmp_path / "another-tree"
    (elsewhere / "surprise").mkdir(parents=True)
    (root / "toolchain").rename(tmp_path / "moved-toolchain")
    (root / "toolchain").symlink_to(elsewhere, target_is_directory=True)

    report = audit_mac_home(root)
    entry = _entry(report, "toolchain")

    assert entry["kind"] == "symlink"
    assert entry["detail"] == "bucket not descended (kind=symlink)"
    assert [e for e in report["entries"] if e["path"].startswith("toolchain/")] == []


def test_a_root_that_is_a_symlink_to_a_directory_is_followed(tmp_path):
    real = _canonical_root(tmp_path)
    link = tmp_path / "compat-link"
    link.symlink_to(real, target_is_directory=True)

    report = audit_mac_home(link)

    assert report["status"]["state"] == "ok"
    assert report["layout"]["generation_detected"] == GENERATION_UNIFIED


def test_an_entry_that_is_neither_file_nor_directory_is_still_reported(tmp_path):
    root = _legacy_root(tmp_path)
    os.mkfifo(root / "a-pipe")

    assert _entry(audit_mac_home(root), "a-pipe")["kind"] == "other"


def test_an_entry_that_cannot_be_stat_ed_is_reported_as_unknown(tmp_path, monkeypatch):
    root = _legacy_root(tmp_path)
    _fail(monkeypatch, "is_symlink", "mac.db")

    assert _entry(audit_mac_home(root), "mac.db")["kind"] == "unknown"


def test_an_unstattable_expected_path_counts_as_missing(tmp_path, monkeypatch):
    root = _canonical_root(tmp_path)
    _fail(monkeypatch, "exists", "mac.db")

    missing = {item["path"] for item in audit_mac_home(root)["missing_expected"]}

    assert "ledger/mac.db" in missing


def test_an_unstattable_root_is_not_a_directory(tmp_path, monkeypatch):
    root = _canonical_root(tmp_path)
    _fail(monkeypatch, "is_dir", "unified")

    assert audit_mac_home(root)["status"]["state"] == "not_a_directory"


def _fail(monkeypatch, method: str, name: str) -> None:
    """Make ``Path.<method>`` raise PermissionError for entries called ``name``."""
    original = getattr(Path, method)

    def patched(self, *args, **kwargs):
        if self.name == name:
            raise PermissionError(13, "Permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, patched)


# --- schema, summary and serialisation --------------------------------------


def test_the_report_carries_the_v1_schema_string(tmp_path):
    assert MAC_HOME_AUDIT_SCHEMA == "mac.mac_home_audit.v1"
    assert audit_mac_home(_legacy_root(tmp_path))["schema"] == "mac.mac_home_audit.v1"


def test_the_report_shape_is_stable(tmp_path):
    report = audit_mac_home(_legacy_root(tmp_path))

    assert set(report) == {
        "schema",
        "root_path",
        "audited_at",
        "root_exists",
        "status",
        "resolvers",
        "layout",
        "entries",
        "canonical",
        "legacy_accepted",
        "drift",
        "legacy_without_canonical_target",
        "missing_expected",
        "duplicates",
        "orphans",
        "summary",
    }
    assert set(report["entries"][0]) == {
        "name",
        "path",
        "kind",
        "classification",
        "generation",
        "bucket",
        "canonical_target",
        "detail",
    }


def test_duplicate_and_orphan_keys_are_reserved_and_empty(tmp_path):
    report = audit_mac_home(_legacy_root(tmp_path))

    assert report["duplicates"] == []
    assert report["orphans"] == []
    assert report["summary"]["duplicates"] == 0
    assert report["summary"]["orphans"] == 0


def test_the_summary_counts_match_the_lists(tmp_path):
    root = _legacy_root(tmp_path)
    (root / "stray").mkdir()
    (root / "ledger").mkdir()

    report = audit_mac_home(root)
    summary = report["summary"]

    assert summary["entries"] == len(report["entries"])
    assert summary["canonical"] == len(report["canonical"])
    assert summary["legacy_accepted"] == len(report["legacy_accepted"])
    assert summary["drift"] == len(report["drift"])
    assert summary["missing_expected"] == len(report["missing_expected"])
    assert summary["buckets_present"] == len(report["layout"]["buckets_present"]) == 1
    assert summary["entries"] == summary["canonical"] + summary["legacy_accepted"] + summary["drift"]


def test_the_report_is_json_serialisable(tmp_path):
    assert json.loads(json.dumps(audit_mac_home(_legacy_root(tmp_path))))["schema"] == (
        MAC_HOME_AUDIT_SCHEMA
    )


def test_audited_at_is_iso_8601_utc(tmp_path):
    stamp = datetime.fromisoformat(audit_mac_home(_legacy_root(tmp_path))["audited_at"])

    assert stamp.tzinfo is not None
    assert stamp.utcoffset() == timedelta(0)


def test_an_explicit_timestamp_is_normalised_to_utc(tmp_path):
    moment = datetime(2026, 8, 21, 12, 0, tzinfo=timezone(timedelta(hours=2)))

    report = audit_mac_home(_legacy_root(tmp_path), now=moment)

    assert report["audited_at"] == "2026-08-21T10:00:00+00:00"


# --- root resolution --------------------------------------------------------


def test_the_default_root_comes_from_the_sanctioned_resolver(tmp_path, monkeypatch):
    root = _legacy_root(tmp_path)
    monkeypatch.setenv("MAC_HOME", str(root))

    report = audit_mac_home()

    assert report["root_path"] == str(root)
    assert report["resolvers"]["mac_home"] == str(root)


def test_a_string_root_and_a_user_relative_root_are_accepted(tmp_path, monkeypatch):
    root = _legacy_root(tmp_path)
    monkeypatch.setenv("HOME", str(root.parent))

    assert audit_mac_home(str(root))["root_path"] == str(root)
    assert audit_mac_home("~/%s" % root.name)["root_path"] == str(root)


def test_the_resolvers_block_says_whether_the_gateway_lives_in_this_root(tmp_path, monkeypatch):
    root = _legacy_root(tmp_path)
    monkeypatch.setenv("MAC_HOME", str(root))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("MAC_OPENCLAW_HOST_DIR", raising=False)

    inside = audit_mac_home(root)["resolvers"]
    assert inside["gateway_home"] == str(root / "openclaw")
    assert inside["gateway_home_inside_root"] is True
    assert inside["openclaw_home_inside_root"] is True

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "somewhere-else"))
    assert audit_mac_home(root)["resolvers"]["gateway_home_inside_root"] is False


# --- the read-only contract -------------------------------------------------


def test_the_audit_writes_nothing(tmp_path):
    root = _legacy_root(tmp_path)
    (root / "ledger").mkdir()
    _touch(root / "ledger" / "mac.db")
    (root / "stray").mkdir()
    before = _snapshot(root)

    report = audit_mac_home(root)

    assert report["summary"]["entries"] > 0
    assert _snapshot(root) == before


def test_auditing_a_missing_root_does_not_create_it(tmp_path):
    root = tmp_path / "absent"

    audit_mac_home(root)

    assert not root.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_the_module_never_calls_a_mutating_filesystem_api():
    """A static check to back the snapshot test: no write-shaped call appears."""
    source = Path(__file__).resolve().parent.parent / "src" / "mac" / "mac_home_audit.py"
    text = source.read_text(encoding="utf-8")

    for forbidden in (
        "mkdir",
        "touch",
        "write_text",
        "write_bytes",
        "unlink",
        "rmdir",
        "chmod",
        "rename",
        "replace",
        "symlink_to",
        "open(",
        "shutil",
        "os.remove",
    ):
        assert forbidden not in text, "mutating call %r in a read-only auditor" % forbidden


def test_the_auditor_does_not_depend_on_the_vendored_gateway():
    source = Path(__file__).resolve().parent.parent / "src" / "mac" / "mac_home_audit.py"

    assert "_hermes" not in source.read_text(encoding="utf-8")


# --- the spec itself --------------------------------------------------------


def test_the_layout_spec_matches_the_documented_target(tmp_path):
    assert [bucket.name for bucket in CANONICAL_BUCKETS] == [
        "ledger",
        "secrets",
        "fleet",
        "runtime",
        "gateway",
        "toolchain",
    ]
    ledger = {item.name for item in CANONICAL_BUCKETS[0].expected}
    assert ledger == {"mac.db", "backups", "archive"}


def test_every_legacy_target_names_a_real_canonical_path():
    canonical = {
        "%s/%s" % (bucket.name, expected.name)
        for bucket in CANONICAL_BUCKETS
        for expected in bucket.expected
    }

    for entry in LEGACY_TOP_LEVEL:
        if entry.canonical_target is not None:
            assert entry.canonical_target in canonical, entry.name


def test_the_spec_has_no_duplicate_names():
    legacy_names = [entry.name for entry in LEGACY_TOP_LEVEL]
    bucket_names = [bucket.name for bucket in CANONICAL_BUCKETS]

    assert len(set(legacy_names)) == len(legacy_names)
    assert len(set(bucket_names)) == len(bucket_names)
    assert set(legacy_names).isdisjoint(bucket_names)


def test_the_spec_is_immutable():
    with pytest.raises(AttributeError):
        CANONICAL_BUCKETS[0].expected[0].name = "nope"
