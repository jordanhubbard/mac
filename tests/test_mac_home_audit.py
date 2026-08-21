"""Contract tests for the read-only unified-home auditor.

The module under test models docs/home-consolidation.md §4 as data, so these
tests read that same spec (``CANONICAL_LAYOUT`` / ``LEGACY_ROOT_ENTRIES``)
rather than restating the layout — a bucket added to the plan must not need a
parallel edit here to stay covered.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mac import mac_home_audit as audit
from mac import mac_paths


# --------------------------------------------------------------------------
# Fixture trees
# --------------------------------------------------------------------------


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    return path


def _make_unified(root: Path) -> Path:
    """A root fully migrated to the §4 target layout."""
    for item in audit.CANONICAL_LAYOUT:
        (root / item.name).mkdir(parents=True, exist_ok=True)
        for entry in item.contents:
            target = root / item.name / entry.name
            if entry.kind == audit.DIR:
                target.mkdir(parents=True, exist_ok=True)
            else:
                _touch(target)
    return root


def _make_pre_phase2(root: Path) -> Path:
    """Today's flat root: the six things the migration has not moved yet."""
    root.mkdir(parents=True, exist_ok=True)
    _touch(root / "mac.db")
    _touch(root / "mac.env")
    _touch(root / "fleets.yaml")
    (root / "openclaw").mkdir()
    (root / "journal").mkdir()
    (root / "backups").mkdir()
    return root


def _snapshot(root: Path) -> dict[str, tuple[int, int, int]]:
    """Every path under ``root`` with the metadata a write would disturb."""
    seen: dict[str, tuple[int, int, int]] = {}
    for path in sorted(root.rglob("*")) + [root]:
        stat = path.lstat()
        seen[str(path)] = (stat.st_mtime_ns, stat.st_size, stat.st_mode)
    return seen


def _by_path(report: dict) -> dict[str, dict]:
    return {entry["path"]: entry for entry in report["entries"]}


# --------------------------------------------------------------------------
# The spec is data, and it is self-consistent
# --------------------------------------------------------------------------


def test_the_spec_names_exactly_the_documented_buckets():
    assert [item.name for item in audit.CANONICAL_LAYOUT] == [
        "ledger",
        "secrets",
        "fleet",
        "runtime",
        "gateway",
        "toolchain",
    ]
    assert audit.BUCKET_NAMES == {item.name for item in audit.CANONICAL_LAYOUT}


def test_gateway_bucket_contains_openclaw():
    gateway = audit.bucket("gateway")
    assert gateway is not None
    assert gateway.expected_names() == {"openclaw"}
    assert audit.bucket("no-such-bucket") is None


def test_canonical_paths_enumerates_buckets_then_their_contents():
    paths = audit.canonical_paths()
    assert paths[0] == "ledger"
    assert "ledger/mac.db" in paths
    assert "gateway/openclaw" in paths
    assert "toolchain/hermes-agent" in paths
    # Every bucket entry is present exactly once and is bucket-qualified.
    assert len(paths) == len(set(paths))
    for item in audit.CANONICAL_LAYOUT:
        for entry in item.contents:
            assert "%s/%s" % (item.name, entry.name) in paths


def test_every_legacy_target_points_at_a_real_canonical_path():
    canonical = set(audit.canonical_paths())
    for entry in audit.LEGACY_ROOT_ENTRIES:
        if entry.canonical_target is not None:
            assert entry.canonical_target in canonical, entry.name


def test_every_declared_legacy_location_is_reachable_from_the_root():
    """`legacy_location` is root-relative, so it must never be bucket-qualified."""
    for item in audit.CANONICAL_LAYOUT:
        for entry in item.contents:
            if entry.legacy_location is not None:
                assert "/" not in entry.legacy_location


def test_legacy_entries_are_unique_and_never_shadow_a_bucket_name():
    names = [entry.name for entry in audit.LEGACY_ROOT_ENTRIES]
    assert len(names) == len(set(names))
    assert not audit.BUCKET_NAMES.intersection(names)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_a_fully_migrated_root_is_all_canonical_with_nothing_missing(tmp_path):
    report = audit.audit_mac_home(_make_unified(tmp_path / "home"))

    assert report["status"] == audit.STATUS_OK
    assert report["drift"] == []
    assert report["legacy_accepted"] == []
    assert report["missing_expected"] == []
    assert set(report["canonical"]) == set(audit.canonical_paths())
    assert report["layout"]["generation_detected"] == audit.GENERATION_UNIFIED
    assert all(
        entry["generation"] == audit.GENERATION_UNIFIED for entry in report["entries"]
    )


def test_todays_flat_root_is_accepted_not_drift(tmp_path):
    report = audit.audit_mac_home(_make_pre_phase2(tmp_path / "home"))

    assert report["status"] == audit.STATUS_OK
    assert report["drift"] == []
    assert set(report["legacy_accepted"]) == {
        "mac.db",
        "mac.env",
        "fleets.yaml",
        "openclaw",
        "journal",
        "backups",
    }
    assert report["layout"]["generation_detected"] == audit.GENERATION_PRE_PHASE2


def test_a_legacy_entry_names_its_canonical_target(tmp_path):
    report = audit.audit_mac_home(_make_pre_phase2(tmp_path / "home"))
    entries = _by_path(report)

    assert entries["mac.db"]["classification"] == audit.LEGACY_ACCEPTED
    assert entries["mac.db"]["canonical_target"] == "ledger/mac.db"
    assert entries["mac.db"]["generation"] == audit.GENERATION_PRE_PHASE2
    assert entries["openclaw"]["canonical_target"] == "gateway/openclaw"
    assert entries["journal"]["canonical_target"] == "runtime/journal"
    assert "mac_paths.ledger_db" in entries["mac.db"]["detail"]


def test_a_recognised_entry_the_plan_never_placed_is_reported_without_a_target(tmp_path):
    root = _make_pre_phase2(tmp_path / "home")
    (root / "qdrant").mkdir()

    entry = _by_path(audit.audit_mac_home(root))["qdrant"]

    assert entry["classification"] == audit.LEGACY_ACCEPTED
    assert entry["canonical_target"] is None
    assert "assigns no bucket" in entry["detail"]


def test_an_unknown_top_level_entry_is_drift(tmp_path):
    root = _make_pre_phase2(tmp_path / "home")
    (root / "leftover-from-2024").mkdir()
    _touch(root / "notes.txt")

    report = audit.audit_mac_home(root)

    assert set(report["drift"]) == {"leftover-from-2024", "notes.txt"}
    entry = _by_path(report)["notes.txt"]
    assert entry["generation"] == audit.GENERATION_UNKNOWN
    assert entry["kind"] == audit.FILE
    assert _by_path(report)["leftover-from-2024"]["kind"] == audit.DIR


def test_drift_is_also_detected_one_level_inside_a_bucket(tmp_path):
    root = _make_unified(tmp_path / "home")
    _touch(root / "ledger" / "mac.db.bak-2026-01-01")
    (root / "secrets" / "old-tokens").mkdir()

    report = audit.audit_mac_home(root)

    assert set(report["drift"]) == {
        "ledger/mac.db.bak-2026-01-01",
        "secrets/old-tokens",
    }
    entry = _by_path(report)["ledger/mac.db.bak-2026-01-01"]
    assert entry["parent"] == "ledger"
    assert entry["detail"] == "not an expected entry of the ledger bucket"


def test_bucket_contents_are_not_scanned_when_the_bucket_is_a_file(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    _touch(root / "ledger")

    report = audit.audit_mac_home(root)

    assert report["canonical"] == ["ledger"]
    assert not [entry for entry in report["entries"] if entry["parent"]]


def test_a_half_migrated_root_reports_a_mixed_generation(tmp_path):
    root = _make_pre_phase2(tmp_path / "home")
    (root / "ledger").mkdir()

    report = audit.audit_mac_home(root)

    assert report["layout"]["generation_detected"] == "mixed"
    assert "ledger" in report["canonical"]


def test_an_empty_root_has_an_unknown_generation(tmp_path):
    root = tmp_path / "home"
    root.mkdir()

    report = audit.audit_mac_home(root)

    assert report["entries"] == []
    assert report["layout"]["generation_detected"] == audit.GENERATION_UNKNOWN
    assert report["summary"]["missing_expected"] == len(report["missing_expected"])


def test_drift_inside_a_bucket_does_not_change_the_root_generation(tmp_path):
    root = _make_pre_phase2(tmp_path / "home")
    (root / "ledger").mkdir()
    _touch(root / "ledger" / "stray")

    assert audit.audit_mac_home(root)["layout"]["generation_detected"] == "mixed"


# --------------------------------------------------------------------------
# Missing expected paths
# --------------------------------------------------------------------------


def test_an_empty_root_reports_every_canonical_path_as_missing(tmp_path):
    root = tmp_path / "home"
    root.mkdir()

    missing = {item["path"] for item in audit.audit_mac_home(root)["missing_expected"]}

    assert missing == set(audit.canonical_paths())


def test_missing_expected_names_the_legacy_copy_that_still_satisfies_it(tmp_path):
    root = _make_pre_phase2(tmp_path / "home")

    missing = {item["path"]: item for item in audit.audit_mac_home(root)["missing_expected"]}

    assert missing["ledger/mac.db"]["legacy_location"] == "mac.db"
    assert missing["ledger/mac.db"]["legacy_present"] is True
    assert missing["ledger/archive"]["legacy_location"] == "archive"
    assert missing["ledger/archive"]["legacy_present"] is False


def test_the_phase1_runtime_files_have_no_legacy_location_at_the_root(tmp_path):
    """§2 item 3: those three are written into the gateway home, not here."""
    root = tmp_path / "home"
    root.mkdir()

    missing = {item["path"]: item for item in audit.audit_mac_home(root)["missing_expected"]}

    for name in (
        "mac-runtime-context.json",
        "mac-runtime-context.md",
        "mac-memory-topology.json",
    ):
        item = missing["runtime/%s" % name]
        assert item["legacy_location"] is None
        assert item["legacy_present"] is False


def test_a_present_bucket_with_a_missing_entry_reports_only_the_entry(tmp_path):
    root = tmp_path / "home"
    (root / "fleet").mkdir(parents=True)
    _touch(root / "fleet" / "fleets.yaml")

    missing = {item["path"] for item in audit.audit_mac_home(root)["missing_expected"]}

    assert "fleet" not in missing
    assert "fleet/fleets.yaml" not in missing
    assert "fleet/specs" in missing


def test_missing_entries_carry_their_kind_and_purpose(tmp_path):
    root = tmp_path / "home"
    root.mkdir()

    missing = {item["path"]: item for item in audit.audit_mac_home(root)["missing_expected"]}

    assert missing["ledger/mac.db"]["kind"] == audit.FILE
    assert missing["ledger/backups"]["kind"] == audit.DIR
    assert missing["ledger"]["kind"] == audit.DIR
    assert missing["ledger"]["purpose"] == audit.bucket("ledger").purpose


# --------------------------------------------------------------------------
# Hostile roots: reported, never raised
# --------------------------------------------------------------------------


def test_a_missing_root_is_reported_not_raised(tmp_path):
    report = audit.audit_mac_home(tmp_path / "nope")

    assert report["status"] == audit.STATUS_MISSING
    assert report["root_exists"] is False
    assert report["status_detail"] == "no such path"
    assert report["entries"] == []
    assert report["missing_expected"] == []
    assert report["summary"]["entries"] == 0


def test_a_root_that_is_a_file_is_reported_not_raised(tmp_path):
    report = audit.audit_mac_home(_touch(tmp_path / "home"))

    assert report["status"] == audit.STATUS_NOT_A_DIRECTORY
    assert report["root_exists"] is True
    assert report["entries"] == []


def test_an_unreadable_root_is_reported_not_raised(tmp_path, monkeypatch):
    root = _make_pre_phase2(tmp_path / "home")
    _deny(monkeypatch, root)

    report = audit.audit_mac_home(root)

    assert report["status"] == audit.STATUS_UNREADABLE
    assert report["root_exists"] is True
    assert report["status_detail"] == "Permission denied"


def test_a_root_whose_stat_fails_is_reported_not_raised(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    original = Path.exists

    def exploding(self):
        if self == root:
            raise OSError(5, "Input/output error")
        return original(self)

    monkeypatch.setattr(Path, "exists", exploding)

    report = audit.audit_mac_home(root)

    assert report["status"] == audit.STATUS_UNREADABLE
    assert report["root_exists"] is False
    assert report["status_detail"] == "Input/output error"


def test_an_unreadable_bucket_is_reported_per_path(tmp_path, monkeypatch):
    root = _make_unified(tmp_path / "home")
    _deny(monkeypatch, root / "secrets")

    report = audit.audit_mac_home(root)

    assert report["unreadable"] == [{"path": "secrets", "error": "Permission denied"}]
    assert report["summary"]["unreadable"] == 1
    # The bucket itself is still classified, and the rest of the tree is walked.
    assert "secrets" in report["canonical"]
    assert "ledger/mac.db" in report["canonical"]


def test_an_unreadable_bucket_still_reports_its_entries_as_missing(tmp_path, monkeypatch):
    root = _make_unified(tmp_path / "home")
    _deny(monkeypatch, root / "secrets")

    report = audit.audit_mac_home(root)

    assert not [path for path in report["canonical"] if path.startswith("secrets/")]
    assert not [item for item in report["missing_expected"] if item["path"] == "secrets"]


def test_a_bucket_whose_type_cannot_be_read_is_neither_walked_nor_fatal(tmp_path, monkeypatch):
    root = _make_unified(tmp_path / "home")
    blocked = root / "ledger"
    original = Path.is_dir

    def exploding(self):
        if self == blocked:
            raise OSError(5, "Input/output error")
        return original(self)

    monkeypatch.setattr(Path, "is_dir", exploding)

    report = audit.audit_mac_home(root)
    entries = _by_path(report)

    # Classified from its name, reported as a file because the type is unknown,
    # and its contents are not walked.
    assert entries["ledger"]["classification"] == audit.CANONICAL
    assert entries["ledger"]["kind"] == audit.FILE
    assert not [path for path in report["canonical"] if path.startswith("ledger/")]
    assert "secrets/mac.env" in report["canonical"]


def test_a_canonical_path_whose_existence_cannot_be_read_counts_as_missing(tmp_path, monkeypatch):
    root = _make_unified(tmp_path / "home")
    blocked = root / "ledger" / "mac.db"
    original = Path.exists

    def exploding(self):
        if self == blocked:
            raise OSError(5, "Input/output error")
        return original(self)

    monkeypatch.setattr(Path, "exists", exploding)

    missing = {item["path"] for item in audit.audit_mac_home(root)["missing_expected"]}

    assert missing == {"ledger/mac.db"}


def test_an_error_without_a_strerror_still_produces_a_detail(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    original = Path.iterdir

    def exploding(self):
        if self == root:
            raise OSError()
        return original(self)

    monkeypatch.setattr(Path, "iterdir", exploding)

    assert audit.audit_mac_home(root)["status_detail"] == "OSError"


def _deny(monkeypatch, blocked: Path) -> None:
    """Make ``blocked.iterdir()`` raise PermissionError.

    Patching beats ``chmod 0o000``: the contract suite runs as root in the
    container images, where mode bits do not deny anything and the test would
    silently assert nothing.
    """
    original = Path.iterdir

    def guarded(self):
        if self == blocked:
            raise PermissionError(13, "Permission denied")
        return original(self)

    monkeypatch.setattr(Path, "iterdir", guarded)


# --------------------------------------------------------------------------
# Report shape
# --------------------------------------------------------------------------


def test_the_schema_string_is_the_published_one():
    assert audit.MAC_HOME_AUDIT_SCHEMA == "mac.mac_home_audit.v1"


@pytest.mark.parametrize(
    "key",
    [
        "schema",
        "root_path",
        "audited_at",
        "root_exists",
        "status",
        "status_detail",
        "layout",
        "entries",
        "canonical",
        "legacy_accepted",
        "drift",
        "missing_expected",
        "unreadable",
        "duplicates",
        "orphans",
        "summary",
    ],
)
def test_every_report_key_is_present_even_for_a_missing_root(tmp_path, key):
    assert key in audit.audit_mac_home(tmp_path / "nope")


def test_the_report_is_stamped_with_the_schema_and_an_iso_utc_instant(tmp_path):
    report = audit.audit_mac_home(_make_pre_phase2(tmp_path / "home"))

    assert report["schema"] == audit.MAC_HOME_AUDIT_SCHEMA
    assert report["root_path"] == str(tmp_path / "home")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", report["audited_at"])


def test_duplicate_and_orphan_keys_are_reserved_and_empty(tmp_path):
    report = audit.audit_mac_home(_make_pre_phase2(tmp_path / "home"))

    assert report["duplicates"] == []
    assert report["orphans"] == []
    assert report["summary"]["duplicates"] == 0
    assert report["summary"]["orphans"] == 0


def test_the_summary_counts_agree_with_the_lists(tmp_path):
    root = _make_pre_phase2(tmp_path / "home")
    (root / "ledger").mkdir()
    _touch(root / "ledger" / "stray")
    _touch(root / "unknown-thing")

    report = audit.audit_mac_home(root)
    summary = report["summary"]

    assert summary["entries"] == len(report["entries"])
    assert summary["canonical"] == len(report["canonical"])
    assert summary["legacy_accepted"] == len(report["legacy_accepted"])
    assert summary["drift"] == len(report["drift"])
    assert summary["missing_expected"] == len(report["missing_expected"])
    assert summary["unreadable"] == len(report["unreadable"])
    assert summary["entries"] == summary["canonical"] + summary["legacy_accepted"] + summary["drift"]


def test_every_entry_carries_the_full_per_entry_contract(tmp_path):
    root = _make_pre_phase2(tmp_path / "home")
    _touch(root / "unknown-thing")

    for entry in audit.audit_mac_home(root)["entries"]:
        assert set(entry) == {
            "path",
            "name",
            "parent",
            "kind",
            "classification",
            "generation",
            "canonical_target",
            "detail",
        }
        assert entry["classification"] in {
            audit.CANONICAL,
            audit.LEGACY_ACCEPTED,
            audit.DRIFT,
        }
        assert entry["kind"] in {audit.DIR, audit.FILE}
        assert entry["path"].endswith(entry["name"])


def test_bucket_children_are_reported_with_their_parent(tmp_path):
    root = _make_unified(tmp_path / "home")

    entries = _by_path(audit.audit_mac_home(root))

    assert entries["ledger"]["parent"] == ""
    assert entries["ledger/mac.db"]["parent"] == "ledger"
    assert entries["ledger/mac.db"]["name"] == "mac.db"


# --------------------------------------------------------------------------
# Resolution goes through mac_paths, and the audit writes nothing
# --------------------------------------------------------------------------


def test_the_default_root_is_whatever_mac_paths_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_HOME", str(_make_pre_phase2(tmp_path / "relocated")))

    report = audit.audit_mac_home()

    assert report["root_path"] == str(mac_paths.mac_home())
    assert report["root_path"] == str(tmp_path / "relocated")
    assert report["status"] == audit.STATUS_OK


def test_a_string_root_is_accepted_and_user_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _make_pre_phase2(tmp_path / ".mac")

    report = audit.audit_mac_home("~/.mac")

    assert report["root_path"] == str(tmp_path / ".mac")
    assert report["status"] == audit.STATUS_OK


def test_the_module_never_names_a_home_literal_itself():
    """The ratchet in tests/test_mac_paths_no_hardcode.py, asserted directly."""
    source = (Path(audit.__file__)).read_text(encoding="utf-8")

    assert not re.search(r"""home\(\)\s*/\s*["']\.(mac|hermes)["']""", source)
    assert "_hermes" not in source


def test_the_audit_does_not_touch_the_tree(tmp_path):
    root = _make_pre_phase2(tmp_path / "home")
    (root / "ledger").mkdir()
    _touch(root / "ledger" / "mac.db")
    _touch(root / "unknown-thing")
    before = _snapshot(root)

    report = audit.audit_mac_home(root)

    after = _snapshot(root)
    assert after == before, "the auditor is read-only; it must not create or modify anything"
    assert report["summary"]["entries"] > 0


def test_auditing_a_missing_root_does_not_create_it(tmp_path):
    root = tmp_path / "absent"

    audit.audit_mac_home(root)

    assert not root.exists()
    assert list(tmp_path.iterdir()) == []


def test_repeated_audits_are_identical_apart_from_the_timestamp(tmp_path):
    root = _make_pre_phase2(tmp_path / "home")

    first = audit.audit_mac_home(root)
    second = audit.audit_mac_home(root)

    first.pop("audited_at")
    second.pop("audited_at")
    assert first == second
