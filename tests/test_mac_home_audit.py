"""Contract for the read-only ``$MAC_HOME`` auditor.

Covers the two layout generations the fleet straddles mid-migration (the §4
unified target and today's pre-Phase-2 root), drift on unknown entries, the
missing-expected-path report, degenerate roots, the schema/summary shape, and —
the property the whole module exists to guarantee — that auditing a tree does
not touch it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mac import mac_paths
from mac.mac_home_audit import (
    CANONICAL_BUCKETS,
    CLASSIFICATION_CANONICAL,
    CLASSIFICATION_DRIFT,
    CLASSIFICATION_LEGACY,
    GATEWAY_HOME_ENTRIES,
    LAYOUT_PRE_PHASE_2,
    LAYOUT_UNIFIED,
    LAYOUT_UNKNOWN,
    LEGACY_ROOT_ENTRIES,
    MAC_HOME_AUDIT_SCHEMA,
    STATUS_MISSING_ROOT,
    STATUS_NOT_A_DIRECTORY,
    STATUS_OK,
    audit_mac_home,
    canonical_bucket_names,
    container_allow_list,
    expected_paths,
    legacy_target_for,
)


# --- Fixture trees ----------------------------------------------------------


def _touch(path: Path, body: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _canonical_tree(root: Path) -> Path:
    """The §4 target layout, fully populated."""
    for bucket in CANONICAL_BUCKETS:
        (root / bucket.name).mkdir(parents=True, exist_ok=True)
        for entry in bucket.entries:
            target = root / bucket.name / entry.name
            if entry.kind == "dir":
                target.mkdir(parents=True, exist_ok=True)
            else:
                _touch(target)
    return root


def _legacy_tree(root: Path) -> Path:
    """Today's pre-Phase-2 shape: the data sits directly at the root."""
    root.mkdir(parents=True, exist_ok=True)
    for name in ("mac.db", "mac.env", ".env", "fleets.yaml", "client-principals.json"):
        _touch(root / name)
    for name in ("openclaw", "journal", "backups", "archive", "bin", "venv", "src"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def _entry_by_path(report: dict, path: str) -> dict:
    matches = [record for record in report["entries"] if record["path"] == path]
    assert matches, f"{path} not in report entries: {[r['path'] for r in report['entries']]}"
    return matches[0]


def _snapshot(root: Path) -> list[tuple[str, bool, int, int]]:
    """Listing + type + size + mtime_ns of every path under ``root``."""
    seen: list[tuple[str, bool, int, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            path = Path(dirpath) / name
            info = path.lstat()
            seen.append((str(path.relative_to(root)), path.is_dir(), info.st_size, info.st_mtime_ns))
    seen.sort()
    return seen


# --- Spec shape -------------------------------------------------------------


def test_spec_models_the_section_4_buckets():
    assert canonical_bucket_names() == (
        "ledger",
        "secrets",
        "fleet",
        "runtime",
        "gateway",
        "toolchain",
    )
    paths = expected_paths()
    for expected in (
        "ledger/mac.db",
        "ledger/backups",
        "ledger/archive",
        "secrets/mac.env",
        "secrets/.env",
        "secrets/client-principals.json",
        "fleet/fleets.yaml",
        "fleet/specs",
        "runtime/mac-runtime-context.json",
        "runtime/mac-runtime-context.md",
        "runtime/mac-memory-topology.json",
        "runtime/journal",
        "gateway/openclaw",
        "toolchain/src",
        "toolchain/venv",
        "toolchain/bin",
        "toolchain/hermes-agent",
    ):
        assert expected in paths


def test_legacy_spec_names_canonical_targets():
    assert legacy_target_for("mac.db") == "ledger/mac.db"
    assert legacy_target_for("mac.env") == "secrets/mac.env"
    assert legacy_target_for("fleets.yaml") == "fleet/fleets.yaml"
    assert legacy_target_for("journal") == "runtime/journal"
    assert legacy_target_for("openclaw") == "gateway/openclaw"
    assert legacy_target_for("backups") == "ledger/backups"
    # Recognised current state that §4 does not place stays target-less rather
    # than being invented into a bucket.
    assert legacy_target_for("qdrant") is None
    assert legacy_target_for("definitely-not-a-mac-thing") is None
    # Every named target must exist in the canonical spec.
    canonical = set(expected_paths())
    for entry in LEGACY_ROOT_ENTRIES:
        if entry.canonical_target:
            assert entry.canonical_target in canonical


def test_gateway_allow_list_is_shared_between_both_positions():
    assert container_allow_list("gateway/openclaw") is GATEWAY_HOME_ENTRIES
    assert container_allow_list("openclaw") is GATEWAY_HOME_ENTRIES
    assert container_allow_list("ledger") == CANONICAL_BUCKETS[0].entries
    assert container_allow_list("runtime/journal") is None


# --- Canonical layout -------------------------------------------------------


def test_canonical_layout_is_recognised_without_drift(tmp_path):
    root = _canonical_tree(tmp_path / "mac-home")
    report = audit_mac_home(root)

    assert report["status"] == STATUS_OK
    assert report["root_exists"] is True
    assert report["layout_generation"] == LAYOUT_UNIFIED
    assert report["drift"] == []
    assert report["missing_expected"] == []
    assert set(report["canonical"]) >= set(expected_paths())
    assert report["legacy_accepted"] == []
    assert all(bucket["present"] for bucket in report["buckets"])

    ledger = _entry_by_path(report, "ledger")
    assert ledger["classification"] == CLASSIFICATION_CANONICAL
    assert ledger["layout_generation"] == LAYOUT_UNIFIED
    assert ledger["bucket"] == "ledger"
    assert ledger["kind"] == "dir"


def test_canonical_gateway_home_contents_are_audited(tmp_path):
    root = _canonical_tree(tmp_path / "mac-home")
    gateway = root / "gateway" / "openclaw"
    _touch(gateway / "SOUL.md")
    (gateway / "sessions").mkdir()
    (gateway / "not-a-gateway-thing").mkdir()

    report = audit_mac_home(root)

    assert _entry_by_path(report, "gateway/openclaw/SOUL.md")["classification"] == (
        CLASSIFICATION_CANONICAL
    )
    assert _entry_by_path(report, "gateway/openclaw/sessions")["classification"] == (
        CLASSIFICATION_CANONICAL
    )
    stray = _entry_by_path(report, "gateway/openclaw/not-a-gateway-thing")
    assert stray["classification"] == CLASSIFICATION_DRIFT
    assert stray["container"] == "gateway/openclaw"
    assert stray["depth"] == 3
    assert "gateway/openclaw/not-a-gateway-thing" in report["drift"]


def test_kind_mismatch_is_reported_without_reclassifying(tmp_path):
    root = tmp_path / "mac-home"
    (root / "ledger").mkdir(parents=True)
    (root / "ledger" / "mac.db").mkdir()  # a directory where a file belongs

    report = audit_mac_home(root)

    record = _entry_by_path(report, "ledger/mac.db")
    assert record["classification"] == CLASSIFICATION_CANONICAL
    assert record["kind_mismatch"] is True
    assert report["summary"]["kind_mismatches"] == 1


# --- Legacy layout ----------------------------------------------------------


def test_legacy_tree_classifies_as_accepted_and_never_hard_fails(tmp_path):
    root = _legacy_tree(tmp_path / "mac-home")
    report = audit_mac_home(root)

    assert report["status"] == STATUS_OK
    assert report["layout_generation"] == LAYOUT_PRE_PHASE_2
    assert report["drift"] == []
    assert set(report["legacy_accepted"]) >= {
        "mac.db",
        "mac.env",
        "fleets.yaml",
        "openclaw",
        "journal",
        "backups",
    }
    assert report["canonical"] == []

    ledger_db = _entry_by_path(report, "mac.db")
    assert ledger_db["classification"] == CLASSIFICATION_LEGACY
    assert ledger_db["layout_generation"] == LAYOUT_PRE_PHASE_2
    assert ledger_db["canonical_target"] == "ledger/mac.db"
    assert ledger_db["bucket"] == "ledger"


def test_legacy_gateway_home_at_the_root_uses_the_same_allow_list(tmp_path):
    root = _legacy_tree(tmp_path / "mac-home")
    _touch(root / "openclaw" / "config.yaml")
    _touch(root / "openclaw" / "config.yaml.bak-mac-home-sync")

    report = audit_mac_home(root)

    assert _entry_by_path(report, "openclaw/config.yaml")["classification"] == (
        CLASSIFICATION_CANONICAL
    )
    assert _entry_by_path(report, "openclaw/config.yaml.bak-mac-home-sync")[
        "classification"
    ] == CLASSIFICATION_DRIFT


def test_mixed_tree_reports_mixed_generation(tmp_path):
    root = _legacy_tree(tmp_path / "mac-home")
    (root / "secrets").mkdir()
    _touch(root / "secrets" / "mac.env")

    report = audit_mac_home(root)

    assert report["layout_generation"] == "mixed"
    assert "secrets" in report["canonical"]
    assert "mac.env" in report["legacy_accepted"]


# --- Drift ------------------------------------------------------------------


def test_unknown_top_level_entries_are_drift(tmp_path):
    root = _legacy_tree(tmp_path / "mac-home")
    _touch(root / "stray-notes.txt")
    (root / "abandoned-metadata").mkdir()

    report = audit_mac_home(root)

    assert sorted(report["drift"]) == ["abandoned-metadata", "stray-notes.txt"]
    stray = _entry_by_path(report, "stray-notes.txt")
    assert stray["classification"] == CLASSIFICATION_DRIFT
    assert stray["layout_generation"] == LAYOUT_UNKNOWN
    assert stray["canonical_target"] is None
    assert report["summary"]["drift"] == 2


def test_symlinks_are_reported_not_followed_into(tmp_path):
    root = _legacy_tree(tmp_path / "mac-home")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "compat-link").symlink_to(outside)

    report = audit_mac_home(root)

    record = _entry_by_path(report, "compat-link")
    assert record["is_symlink"] is True
    assert record["symlink_target"] == str(outside)
    assert record["classification"] == CLASSIFICATION_DRIFT


# --- Missing expected paths -------------------------------------------------


def test_missing_expected_paths_are_reported_with_their_legacy_source(tmp_path):
    root = _legacy_tree(tmp_path / "mac-home")
    report = audit_mac_home(root)

    missing = {record["path"]: record for record in report["missing_expected"]}
    assert set(missing) == set(expected_paths())

    assert missing["ledger/mac.db"]["legacy_present"] is True
    assert missing["ledger/mac.db"]["legacy_path"] == "mac.db"
    assert missing["ledger/mac.db"]["required"] is True
    assert missing["gateway/openclaw"]["legacy_path"] == "openclaw"

    # Nothing supplies fleet/specs in this fixture, so it is genuinely absent.
    assert missing["fleet/specs"]["legacy_present"] is False
    assert missing["fleet/specs"]["legacy_path"] is None

    assert report["summary"]["missing_expected"] == len(expected_paths())
    assert report["summary"]["missing_with_legacy_source"] >= 6
    assert report["summary"]["buckets_present"] == 0


def test_partially_migrated_bucket_reports_only_the_absent_members(tmp_path):
    root = tmp_path / "mac-home"
    (root / "ledger").mkdir(parents=True)
    _touch(root / "ledger" / "mac.db")

    report = audit_mac_home(root)

    missing = {record["path"] for record in report["missing_expected"]}
    assert "ledger" not in missing
    assert "ledger/mac.db" not in missing
    assert {"ledger/backups", "ledger/archive", "secrets", "secrets/mac.env"} <= missing


# --- Degenerate roots -------------------------------------------------------


def test_missing_root_is_a_status_not_an_exception(tmp_path):
    root = tmp_path / "nope"
    report = audit_mac_home(root)

    assert report["status"] == STATUS_MISSING_ROOT
    assert report["root_exists"] is False
    assert report["root_path"] == str(root)
    assert report["entries"] == []
    assert report["missing_expected"] == []
    assert report["layout_generation"] == LAYOUT_UNKNOWN
    assert report["summary"]["entries"] == 0
    assert report["schema"] == MAC_HOME_AUDIT_SCHEMA


def test_root_that_is_a_file_is_reported(tmp_path):
    root = _touch(tmp_path / "mac-home")
    report = audit_mac_home(root)

    assert report["root_exists"] is True
    assert report["status"] == STATUS_NOT_A_DIRECTORY
    assert report["entries"] == []


def test_empty_root_is_quiet(tmp_path):
    root = tmp_path / "mac-home"
    root.mkdir()
    report = audit_mac_home(root)

    assert report["status"] == STATUS_OK
    assert report["entries"] == []
    assert report["drift"] == []
    assert report["layout_generation"] == LAYOUT_UNKNOWN
    assert report["summary"]["missing_expected"] == len(expected_paths())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unreadable_container_is_reported_not_raised(tmp_path):
    root = _canonical_tree(tmp_path / "mac-home")
    locked = root / "ledger"
    locked.chmod(0o000)
    try:
        report = audit_mac_home(root)
    finally:
        locked.chmod(0o700)

    assert report["status"] == STATUS_OK
    assert [record["path"] for record in report["unreadable_paths"]] == ["ledger"]
    assert report["summary"]["unreadable_paths"] == 1


# --- Schema / summary shape -------------------------------------------------


def test_report_shape_is_stable_and_reserves_duplicate_and_orphan_keys(tmp_path):
    report = audit_mac_home(_canonical_tree(tmp_path / "mac-home"))

    assert report["schema"] == MAC_HOME_AUDIT_SCHEMA == "mac.mac_home_audit.v1"
    assert set(report) == {
        "schema",
        "root_path",
        "audited_at",
        "root_exists",
        "status",
        "status_detail",
        "layout_generation",
        "roots",
        "buckets",
        "entries",
        "canonical",
        "legacy_accepted",
        "drift",
        "missing_expected",
        "unreadable_paths",
        "duplicates",
        "orphans",
        "summary",
    }
    # Reserved for the sibling detectors; stable and empty here.
    assert report["duplicates"] == []
    assert report["orphans"] == []
    assert report["summary"]["duplicates"] == 0
    assert report["summary"]["orphans"] == 0

    assert set(report["summary"]) == {
        "status",
        "root_exists",
        "layout_generation",
        "entries",
        "top_level_entries",
        "canonical",
        "legacy_accepted",
        "drift",
        "kind_mismatches",
        "buckets_present",
        "buckets_expected",
        "missing_expected",
        "missing_required",
        "missing_with_legacy_source",
        "unreadable_paths",
        "duplicates",
        "orphans",
    }
    assert report["summary"]["buckets_expected"] == len(CANONICAL_BUCKETS)
    assert report["summary"]["canonical"] == len(report["canonical"])
    assert report["summary"]["entries"] == len(report["entries"])

    # ISO-8601 UTC.
    assert report["audited_at"].endswith("+00:00")


def test_roots_resolve_through_mac_paths(tmp_path, monkeypatch):
    home = tmp_path / "relocated"
    _canonical_tree(home)
    monkeypatch.setenv("MAC_HOME", str(home))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("MAC_OPENCLAW_HOST_DIR", raising=False)

    report = audit_mac_home()

    assert report["root_path"] == str(mac_paths.mac_home()) == str(home)
    assert report["roots"]["gateway_home"] == str(mac_paths.gateway_home())
    assert report["roots"]["openclaw_home"] == str(mac_paths.openclaw_home())
    assert report["roots"]["gateway_under_mac_home"] is True
    assert report["roots"]["openclaw_under_mac_home"] is True
    assert report["status"] == STATUS_OK


def test_explicit_root_overrides_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "not-this-one"))
    explicit = _canonical_tree(tmp_path / "explicit")

    report = audit_mac_home(explicit)

    assert report["root_path"] == str(explicit)
    assert report["status"] == STATUS_OK


# --- Read-only proof --------------------------------------------------------


def test_audit_does_not_touch_the_tree(tmp_path):
    root = _legacy_tree(tmp_path / "mac-home")
    _canonical_tree(root)
    _touch(root / "openclaw" / "SOUL.md")
    _touch(root / "drifted.json")

    before = _snapshot(root)
    report = audit_mac_home(root)
    after = _snapshot(root)

    assert report["status"] == STATUS_OK
    assert before == after, "audit_mac_home mutated the audited tree"
    # And it created nothing outside the tree either.
    assert not (tmp_path / "mac-home.lock").exists()


def test_audit_does_not_create_a_missing_root(tmp_path):
    root = tmp_path / "absent"
    audit_mac_home(root)
    assert not root.exists()
    assert sorted(child.name for child in tmp_path.iterdir()) == []


def test_module_does_not_import_vendored_hermes():
    source = Path(__file__).resolve().parent.parent / "src" / "mac" / "mac_home_audit.py"
    text = source.read_text(encoding="utf-8")
    assert "_hermes" not in text
    # No write-capable filesystem verbs in the auditor.
    for verb in ("mkdir(", "write_text(", "write_bytes(", "chmod(", "unlink(", "rmdir(", "open("):
        assert verb not in text, f"read-only auditor must not call {verb}"
